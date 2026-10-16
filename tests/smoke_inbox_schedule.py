#!/usr/bin/env python3
"""Inbox · Mine / All — the PERMISSION BOUNDARIES (inbox_schedule.py, calendar_import.py, team_items.py).

Boots the real app on an ISOLATED database and proves, through the HTTP routes:
  * who: anonymous 401; client, estimator 403 on both scopes; pm / super / admin / c_suite read Mine;
    ONLY admin / c_suite read All — pm and super get 403 even when they call the API directly
  * private calendars: each person's Mine carries their OWN Google events and nobody else's; no query
    value (user_id, uid, assignee, scope tricks) reaches another person's events; All never carries a
    private event (not for everyone, not filtered to the owner); the team calendar, the team list
    and the company calendar never carry them either
  * Mine: items assigned to me + meetings I am in + my open overdue tasks; not other people's items
  * All: assigned items only, with the assignee; filters by assignee (incl. meetings they are in),
    type and dates; bad input -> 400
  * meetings: admin / c_suite create them with people in them; a non-team person in the meeting
    -> 400; pm cannot create one (403); the people in it see it in Mine
  * the /inbox page: pm / super get it, admin / c_suite are sent to /company#inbox/mine, a client 403
  * private titles never reach the log
Run:  venv/bin/python tests/smoke_inbox_schedule.py
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_inbox_sched_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")
os.environ["SSC_GMAIL_POLL"] = "0"

import auth  # noqa: E402
import calendar_import as ci  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "inbsched"
PW = "InboxSchedSmoke2026"
T = date.today()
D = lambda n: (T + timedelta(days=n)).isoformat()   # noqa: E731
ROLES = (("admin", "admin"), ("cs", "c_suite"), ("pm", "pm"), ("pm2", "pm"), ("super", "super"),
         ("client", "client"), ("est", "estimator"))
PRIVATE = {"admin": "Inbsched PRIVATE admin dentist", "pm": "Inbsched PRIVATE pm parent night",
           "super": "Inbsched PRIVATE super truck pickup"}


class LogTrap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _db():
    return db_layer.connect(pragma_fk=False)


def _uids(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]


def _purge(conn):
    uids = _uids(conn)
    tasks = [r["id"] for r in conn.execute("SELECT id FROM crm_task WHERE title LIKE 'Inbsched%'").fetchall()]
    purge(conn, "crm_task_attendee", "task_id", tasks)
    purge(conn, "crm_task", "id", tasks)
    purge(conn, "user_calendar_event", "user_id", uids)
    conn.commit()
    purge_users(conn, uids)
    conn.commit()


def _seed() -> dict:
    conn = _db()
    try:
        ci.ensure(conn)
        _purge(conn)
        ids = {}
        for key, role in ROLES:
            email = f"{TAG}.{key}@example.com"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, display_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, ?, 1, 'active', 0, 0)", (email, auth.hash_password(PW), role, f"Inbsched {key}", f"Inbsched {key}"))
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.commit()
        for key, title in PRIVATE.items():   # each person's own Google Calendar, copied privately
            ci.apply_event(conn, ids[key], {"id": "same-google-id", "summary": title, "status": "confirmed", "updated": "u1",
                                            "location": "Inbsched private place", "start": {"date": D(1)}, "end": {"date": D(2)}})
        return ids
    finally:
        conn.close()


def _login(key):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@example.com", "password": PW})
    assert r.status_code == 200, (key, r.status_code)
    return c


def _get(c, url, code=200):
    r = c.get(url)
    assert r.status_code == code, (url, r.status_code, r.get_data(as_text=True)[:300])
    return r.get_json(silent=True) or {}


def _post(c, url, body, code):
    r = c.post(url, json=body)
    assert r.status_code == code, (url, r.status_code, r.get_data(as_text=True)[:300])
    return r.get_json(silent=True) or {}


def _titles(data):
    return {i["title"] for i in data.get("items", []) if i["title"].startswith("Inbsched")} | \
           {e["title"] for e in data.get("calendar", []) if e["title"].startswith("Inbsched")}


def main() -> int:
    trap = LogTrap()
    for name in ("ssc.inbox_schedule", "ssc.calendar_import", "ssc.team_items"):
        logging.getLogger(name).addHandler(trap)
        logging.getLogger(name).setLevel(logging.DEBUG)
    ids = _seed()
    passed = 0
    try:
        c = {k: _login(k) for k, _ in ROLES}
        win = f"&from={D(-1)}&to={D(10)}"

        # ---- team items to look at: created by admin through the real API
        mk = lambda body: _post(c["admin"], "/api/team/items", body, 201)["data"]["id"]   # noqa: E731
        t_pm = mk({"kind": "task", "title": "Inbsched task for pm", "assignee_user_id": ids["pm"], "due_date": D(2)})
        t_pm2 = mk({"kind": "task", "title": "Inbsched task for pm2", "assignee_user_id": ids["pm2"], "due_date": D(2)})
        t_late = mk({"kind": "task", "title": "Inbsched overdue for pm", "assignee_user_id": ids["pm"], "due_date": D(-5)})
        e_super = mk({"kind": "event", "title": "Inbsched walkthrough for super", "assignee_user_id": ids["super"], "start_date": D(3)})
        m_cs = mk({"kind": "meeting", "title": "Inbsched ops meeting", "assignee_user_id": ids["cs"], "start_date": D(4),
                   "start_time": "09:30", "end_time": "10:30", "attendee_user_ids": [ids["pm"], ids["super"], ids["cs"]]})
        mk({"kind": "task", "title": "Inbsched nobody yet", "due_date": D(2)})
        passed += 1; print("  seeded (private events + team items through the API)")

        # ---- who may read what
        anon = server.app.test_client()
        assert anon.get("/api/inbox/schedule?scope=mine").status_code == 401
        for k in ("client", "est"):
            _get(c[k], "/api/inbox/schedule?scope=mine", 403)
            _get(c[k], "/api/inbox/schedule?scope=all", 403)
        for k in ("pm", "pm2", "super"):
            _get(c[k], "/api/inbox/schedule?scope=mine" + win)
            _get(c[k], "/api/inbox/schedule?scope=all" + win, 403)
            _get(c[k], "/api/inbox/schedule?scope=all&assignee=" + str(ids[k]) + win, 403)
            _get(c[k], "/api/inbox/schedule?scope=ALL" + win, 403)
        for k in ("admin", "cs"):
            _get(c[k], "/api/inbox/schedule?scope=mine" + win)
            _get(c[k], "/api/inbox/schedule?scope=all" + win)
        passed += 1; print("  roles ok (All is admin / C-suite only, server-side)")

        # ---- private calendars: only the owner, ever
        for k in ("admin", "pm", "super"):
            d = _get(c[k], "/api/inbox/schedule?scope=mine" + win)["data"]
            assert [e["title"] for e in d["calendar"]] == [PRIVATE[k]], (k, d["calendar"])
            for other, title in PRIVATE.items():
                for trick in (f"&user_id={ids[other]}", f"&uid={ids[other]}", f"&assignee={ids[other]}", f"&me={ids[other]}"):
                    body = c[k].get("/api/inbox/schedule?scope=mine" + win + trick).get_data(as_text=True)
                    assert (title in body) == (other == k), (k, other, trick)
        for k in ("pm2", "cs"):
            assert _get(c[k], "/api/inbox/schedule?scope=mine" + win)["data"]["calendar"] == []
        for q in ("", f"&assignee={ids['pm']}", f"&assignee={ids['admin']}", "&type=event", "&type=task", "&type=meeting"):
            body = c["admin"].get("/api/inbox/schedule?scope=all" + win + q).get_data(as_text=True)
            assert "PRIVATE" not in body and "Inbsched private place" not in body, q
        _get(c["admin"], "/api/inbox/schedule?scope=all&type=calendar" + win, 400)
        for k in ("admin", "pm", "super"):
            for url in (f"/api/team/calendar?from={D(-1)}&to={D(10)}", "/api/team/items?status=all", f"/api/team/items?status=all&mine=1"):
                assert "PRIVATE" not in c[k].get(url).get_data(as_text=True), (k, url)
        assert "PRIVATE" not in c["admin"].get(f"/api/company/portal/calendar?from={D(-1)}&to={D(10)}").get_data(as_text=True)
        passed += 1; print("  private calendars ok (owner only; never in All, team lists or calendars)")

        # ---- Mine: assigned to me + meetings I am in + my overdue tasks
        assert _titles(_get(c["pm"], "/api/inbox/schedule?scope=mine" + win)["data"]) == {
            PRIVATE["pm"], "Inbsched task for pm", "Inbsched overdue for pm", "Inbsched ops meeting"}
        assert _titles(_get(c["pm2"], "/api/inbox/schedule?scope=mine" + win)["data"]) == {"Inbsched task for pm2"}
        assert _titles(_get(c["super"], "/api/inbox/schedule?scope=mine" + win)["data"]) == {
            PRIVATE["super"], "Inbsched walkthrough for super", "Inbsched ops meeting"}
        assert _titles(_get(c["cs"], "/api/inbox/schedule?scope=mine" + win)["data"]) == {"Inbsched ops meeting"}
        only_cal = _get(c["pm"], "/api/inbox/schedule?scope=mine&type=calendar" + win)["data"]
        assert only_cal["items"] == [] and [e["title"] for e in only_cal["calendar"]] == [PRIVATE["pm"]]
        meet = next(i for i in _get(c["pm"], "/api/inbox/schedule?scope=mine" + win)["data"]["items"] if i["id"] == m_cs)
        assert meet["kind"] == "meeting" and set(meet["attendee_ids"]) == {ids["pm"], ids["super"]} and meet["mine"]   # the assignee is not repeated
        assert not meet["can_edit"]
        assert _get(c["pm"], "/api/inbox/schedule?scope=mine" + win)["data"]["calendar_status"]["connected"] is False
        passed += 1; print("  Mine ok (assigned, meetings I'm in, overdue; nobody else's)")

        # ---- All: assigned work only, with the assignee; filters
        allr = _get(c["admin"], "/api/inbox/schedule?scope=all" + win)["data"]
        by = {i["title"]: i for i in allr["items"] if i["title"].startswith("Inbsched")}
        assert set(by) == {"Inbsched task for pm", "Inbsched task for pm2", "Inbsched walkthrough for super", "Inbsched ops meeting"}, set(by)
        assert by["Inbsched task for pm"]["assignee_name"] == "Inbsched pm"
        assert any(p["id"] == ids["pm"] for p in allr["people"]) and not any(p["id"] == ids["client"] for p in allr["people"])
        f = lambda q: {i["title"] for i in _get(c["cs"], "/api/inbox/schedule?scope=all" + q)["data"]["items"] if i["title"].startswith("Inbsched")}   # noqa: E731
        assert f(f"&assignee={ids['pm']}" + win) == {"Inbsched task for pm", "Inbsched ops meeting"}
        assert f("&type=meeting" + win) == {"Inbsched ops meeting"}
        assert f(f"&type=task&assignee={ids['pm2']}" + win) == {"Inbsched task for pm2"}
        assert f(f"&from={D(3)}&to={D(3)}") == {"Inbsched walkthrough for super"}
        assert f(f"&from={D(20)}&to={D(21)}") == set()
        for bad in ("scope=nope", "scope=all&type=lunch", "scope=all&assignee=abc", f"scope=all&from={D(5)}&to={D(1)}",
                    f"scope=all&from={D(0)}&to={D(200)}", "scope=all&from=2026-13-45"):
            _get(c["admin"], "/api/inbox/schedule?" + bad, 400)
        passed += 1; print("  All ok (assignee shown; assignee / type / date filters; bad input 400)")

        # ---- meetings: who may create, who may be in one
        _post(c["pm"], "/api/team/items", {"kind": "meeting", "title": "Inbsched pm meeting", "start_date": D(1)}, 403)
        _post(c["admin"], "/api/team/items", {"kind": "meeting", "title": "Inbsched bad people", "start_date": D(1),
                                              "attendee_user_ids": [ids["client"]]}, 400)
        _post(c["admin"], "/api/team/items", {"kind": "meeting", "title": "Inbsched bad people", "start_date": D(1),
                                              "attendee_user_ids": "everyone"}, 400)
        _post(c["admin"], "/api/team/items", {"kind": "meeting", "title": "Inbsched no day"}, 400)
        r = c["admin"].put(f"/api/team/items/{m_cs}", json={"attendee_user_ids": [ids["pm2"]]})
        assert r.status_code == 200
        assert "Inbsched ops meeting" not in _titles(_get(c["pm"], "/api/inbox/schedule?scope=mine" + win)["data"])
        assert "Inbsched ops meeting" in _titles(_get(c["pm2"], "/api/inbox/schedule?scope=mine" + win)["data"])
        _post(c["pm2"], f"/api/team/items/{t_pm}/done", {"done": True}, 403)   # not theirs
        _post(c["pm"], f"/api/team/items/{t_pm}/done", {"done": True}, 200)
        assert t_late and t_pm2 and e_super
        passed += 1; print("  meetings ok (admin-made, team people only, people in it see it)")

        # ---- the page
        assert c["pm"].get("/inbox").status_code == 200 and b"inbox_schedule.js" in c["pm"].get("/inbox").data
        assert c["super"].get("/inbox").status_code == 200
        r = c["admin"].get("/inbox")
        assert r.status_code == 302 and r.headers["Location"].endswith("/company#inbox/mine")
        assert c["client"].get("/inbox").status_code in (302, 403)
        nav = (ROOT / "static/js/company_nav.js").read_text(encoding="utf-8")
        assert "hrefFor: { pm: '/inbox', super: '/inbox' }" in nav
        portal = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        assert 'data-ibtab="mine"' in portal and 'data-ibtab="all"' in portal and "inbox_schedule.js" in portal
        passed += 1; print("  page + nav ok")

        logs = "\n".join(trap.lines)
        assert "PRIVATE" not in logs and "Inbsched private place" not in logs
        passed += 1; print("  logs ok")
    finally:
        conn = _db()
        try:
            _purge(conn)
        finally:
            conn.close()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"smoke_inbox_schedule: {passed}/8 groups passed")
    return 0 if passed == 8 else 1


if __name__ == "__main__":
    sys.exit(main())
