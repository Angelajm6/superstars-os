#!/usr/bin/env python3
"""Team tasks & events (the dashboard to-do list, synced onto the calendar). Guard.

Boots the real app on an ISOLATED database. Proves:
  * schema: crm_task gains the task/event columns, idempotently
  * gates: anonymous 401; a client 403; pm / super READ the list and the people;
    only admin / c_suite create, edit and delete
  * create: task with no deadline; a deadline time needs a date; blank title, bad kind,
    an assignee who is not an active team member (client, test account) -> 400; an
    unknown job -> 404. Event: needs a date; ends before it starts / end time before
    start time -> 400; multi-day events are fine
  * list: open / done / mine; a from-to window holds a task by its deadline and an event
    by any of its days; a deleted item is gone from the list and the calendar
  * edit: c_suite edits; task -> event keeps the deadline as the event day; event -> task
  * done: the assignee marks their own item done and reopens it; another pm or a super
    who is not the assignee -> 403; admin may
  * the company calendar feed carries the items; titles never reach the log
  * address: kept on tasks and events (survives task <-> event), comes back with a Google Maps
    directions link; too long -> 400; suggestions = the jobs' own addresses + NYC GeoSearch
    (faked here, never the network), best-effort when the lookup fails; team roles only
  * the dashboard: two KPI tiles, the Tasks & events card, the shared dialog reading the
    date picker through dateGet / dateSet, chips on the week strip and the calendar;
    the shared date picker opens on the FIRST click (focus + click no longer cancel)
  * one day: every week-strip and calendar day is clickable (data-day; job bars, chips and
    links keep their own clicks) and opens that day: jobs running (on site + daily report
    for today and past days), its tasks & events with directions, what is due; "+ Task or
    event on this day" opens the dialog on that date
Run:  venv/bin/python tests/smoke_team_items.py
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_team_items_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)
import team_items  # noqa: E402

TAG = "teamitems"
PW = "TeamItemsSmoke2026"
P = "TEAMIT-P"
T = date.today()
D = lambda n: (T + timedelta(days=n)).isoformat()   # noqa: E731
SECRET_TITLE = "Call Zebediah about the teamitems scaffold"   # must never reach the log


def _db():
    return db_layer.connect(pragma_fk=False)


def _uids(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]


def _seed() -> dict:
    conn = _db()
    try:
        uids = _uids(conn)
        purge(conn, "crm_task", "created_by", uids)
        purge(conn, "projects", "project_code", [P])
        conn.commit()
        purge_users(conn, uids)
        conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, 'Teamitems Job', '1 Team St', 'active')", (P,))
        ids = {}
        # key, role, is_system (0 = a real team member who can be assigned)
        for key, role, system in (("admin", "admin", 0), ("cs", "c_suite", 0), ("pm", "pm", 0), ("pm2", "pm", 0),
                                  ("super", "super", 0), ("client", "client", 0), ("fixture", "pm", 1)):
            email = f"{TAG}.{key}@example.com"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, display_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, ?, 1, 'active', 0, ?)",
                         (email, auth.hash_password(PW), role, f"Teamitems {key}", f"Teamitems {key}", system))
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.commit()
        return ids
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        uids = _uids(conn)
        purge(conn, "crm_task", "created_by", uids)
        purge(conn, "projects", "project_code", [P])
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(key):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@example.com", "password": PW})
    assert r.status_code == 200, (key, r.status_code)
    return c


def _items(c, q="status=all"):
    r = c.get("/api/team/items?" + q)
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return {i["id"]: i for i in r.get_json()["data"] if i["title"].startswith(("Teamitems", "Call Zebediah"))}


def main() -> int:
    ids = _seed()
    passed = 0

    # ---- schema (idempotent)
    conn = _db()
    try:
        team_items.ensure(conn); team_items.ensure(conn)
        cols = team_items._columns(conn, "crm_task")
        assert all(name in cols for name, _ in team_items.COLUMNS), cols
    finally:
        conn.close()
    passed += 1; print("  schema ok")

    # ---- gates
    admin, cs, pm, pm2, sup, cl = (_login(k) for k in ("admin", "cs", "pm", "pm2", "super", "client"))
    anon = server.app.test_client()
    assert anon.get("/api/team/items").status_code == 401
    assert cl.get("/api/team/items").status_code == 403 and cl.get("/api/team/people").status_code == 403
    for c in (pm, sup):
        assert c.get("/api/team/items").status_code == 200 and c.get("/api/team/people").status_code == 200
        assert c.post("/api/team/items", json={"title": "Teamitems nope"}).status_code == 403
    people = {p["id"]: p for p in pm.get("/api/team/people").get_json()["data"]}
    assert ids["pm"] in people and ids["super"] in people and ids["cs"] in people
    assert ids["client"] not in people and ids["fixture"] not in people, "no clients, no test accounts"
    assert set(people[ids["pm"]]) == {"id", "name", "role"}, "names and roles only"
    passed += 1; print("  gates ok")

    # ---- create: validation
    bad = [({"title": "  "}, 400), ({"title": "Teamitems x", "kind": "meeting"}, 400),
           ({"title": "Teamitems x", "due_time": "14:00"}, 400), ({"title": "Teamitems x", "due_date": "10/07/2026"}, 400),
           ({"title": "Teamitems x", "assignee_user_id": ids["client"]}, 400), ({"title": "Teamitems x", "assignee_user_id": ids["fixture"]}, 400),
           ({"title": "Teamitems x", "project_code": "NOPE-0"}, 404),
           ({"title": "Teamitems ev", "kind": "event"}, 400),
           ({"title": "Teamitems ev", "kind": "event", "start_date": D(3), "end_date": D(2)}, 400),
           ({"title": "Teamitems ev", "kind": "event", "start_date": D(3), "start_time": "10:00", "end_time": "09:00"}, 400),
           ({"title": "Teamitems ev", "kind": "event", "start_date": D(3), "end_time": "09:00"}, 400),
           ({"title": "x" * 201}, 400)]
    for body, code in bad:
        r = admin.post("/api/team/items", json=body)
        assert r.status_code == code, (body, r.status_code, r.get_json())
    assert _items(admin) == {}, "a refused create stores nothing"

    buf = io.StringIO(); h = logging.StreamHandler(buf); root = logging.getLogger(); lvl = root.level
    root.setLevel(logging.INFO); root.addHandler(h)
    try:
        r = admin.post("/api/team/items", json={"title": SECRET_TITLE, "assignee_user_id": ids["pm"], "due_date": D(0), "due_time": "14:00",
                                                "project_code": P, "detail": "ask for the gate code"})
        assert r.status_code == 201, r.get_json()
        task = r.get_json()["data"]["id"]
        r = cs.post("/api/team/items", json={"title": "Teamitems no deadline"})
        assert r.status_code == 201
        nodl = r.get_json()["data"]["id"]
        r = cs.post("/api/team/items", json={"kind": "event", "title": "Teamitems walkthrough", "start_date": D(2), "start_time": "09:00",
                                             "end_time": "10:00", "location": "On site", "assignee_user_id": ids["super"]})
        assert r.status_code == 201, r.get_json()
        ev = r.get_json()["data"]["id"]
        r = cs.post("/api/team/items", json={"kind": "event", "title": "Teamitems trade show", "start_date": D(5), "end_date": D(7)})
        assert r.status_code == 201
        multi = r.get_json()["data"]["id"]
    finally:
        root.removeHandler(h); root.setLevel(lvl)
    log = buf.getvalue()
    assert f"team item created id={task}" in log and "Zebediah" not in log and "gate code" not in log, "ids only in the log"
    passed += 1; print("  create ok")

    # ---- list views
    allv = _items(pm)
    t, e, m, n = allv[task], allv[ev], allv[multi], allv[nodl]
    assert t["kind"] == "task" and t["due_date"] == D(0) and t["due_time"] == "14:00" and t["project_name"] == "Teamitems Job"
    assert t["assignee_name"] == "Teamitems pm" and t["today"] and not t["overdue"] and t["mine"] is True
    assert t["can_edit"] is False and t["can_complete"] is True, "a pm reads and completes their own; editing is admin/c_suite"
    assert e["kind"] == "event" and e["start_date"] == D(2) and e["start_time"] == "09:00" and e["location"] == "On site" and e["due_date"] is None
    assert m["calendar_from"] == D(5) and m["calendar_to"] == D(7) and n["calendar_from"] is None
    assert set(_items(pm, "status=open&mine=1")) == {task}
    assert set(_items(sup, "status=open&mine=1")) == {ev}
    win = _items(pm, f"status=all&from={D(6)}&to={D(6)}")
    assert set(win) == {multi}, "a multi-day event sits on every one of its days"
    win = _items(pm, f"status=all&from={D(0)}&to={D(2)}")
    assert set(win) == {task, ev}, "tasks by deadline, events by day; no-deadline tasks stay off the calendar"
    assert pm.get(f"/api/team/items?from={D(2)}").status_code == 400 and pm.get(f"/api/team/items?from={D(3)}&to={D(1)}").status_code == 400
    passed += 1; print("  list ok")

    # ---- edit
    assert pm.put(f"/api/team/items/{task}", json={"title": "Teamitems hijack"}).status_code == 403
    r = cs.put(f"/api/team/items/{nodl}", json={"kind": "event", "start_date": D(1)})
    assert r.status_code == 200, r.get_json()
    r = cs.put(f"/api/team/items/{task}", json={"kind": "event"})
    assert r.status_code == 200, r.get_json()
    got = _items(cs)[task]
    assert got["kind"] == "event" and got["start_date"] == D(0) and got["due_time"] is None, "the deadline became the event day"
    assert got["title"] == SECRET_TITLE and got["assignee_user_id"] == ids["pm"], "untouched fields stay"
    r = cs.put(f"/api/team/items/{task}", json={"kind": "task", "due_date": D(0), "due_time": "15:30"})
    assert r.status_code == 200 and _items(cs)[task]["due_time"] == "15:30" and _items(cs)[task]["start_date"] is None
    assert cs.put(f"/api/team/items/{ev}", json={"end_date": D(1)}).status_code == 400, "a refused edit"
    assert _items(cs)[ev]["end_date"] is None, "changes nothing"
    assert cs.put("/api/team/items/999999", json={"title": "x"}).status_code == 404
    passed += 1; print("  edit ok")

    # ---- done
    assert pm2.post(f"/api/team/items/{task}/done", json={"done": True}).status_code == 403
    assert sup.post(f"/api/team/items/{task}/done", json={"done": True}).status_code == 403
    assert pm.post(f"/api/team/items/{task}/done", json={"done": True}).status_code == 200
    assert _items(pm, "status=done")[task]["status"] == "done" and task not in _items(pm, "status=open")
    assert pm.post(f"/api/team/items/{task}/done", json={"done": False}).status_code == 200
    assert task in _items(pm, "status=open")
    assert admin.post(f"/api/team/items/{ev}/done", json={}).status_code == 200, "admin may close anyone's"
    passed += 1; print("  done ok")

    # ---- calendar + delete
    cal = cs.get(f"/api/company/portal/calendar?from={D(0)}&to={D(7)}").get_json()["data"]
    on_cal = {i["id"] for i in cal["items"]}
    assert {task, ev, multi, nodl} <= on_cal, on_cal
    assert pm.get(f"/api/company/portal/calendar?from={D(0)}&to={D(7)}").status_code == 403, "the company calendar stays company-only"
    assert pm.delete(f"/api/team/items/{multi}").status_code == 403
    assert cs.delete(f"/api/team/items/{multi}").status_code == 200
    assert multi not in _items(cs) and cs.delete(f"/api/team/items/{multi}").status_code == 404
    cal = cs.get(f"/api/company/portal/calendar?from={D(0)}&to={D(7)}").get_json()["data"]
    assert multi not in {i["id"] for i in cal["items"]}
    assert cs.post(f"/api/team/items/{multi}/done", json={}).status_code == 404
    passed += 1; print("  calendar + delete ok")

    # ---- address + directions + suggestions
    r = cs.post("/api/team/items", json={"title": "Teamitems site visit", "address": "890 East 135 Street, Bronx, NY", "due_date": D(1)})
    assert r.status_code == 201, r.get_json()
    visit = r.get_json()["data"]["id"]
    got = _items(pm)[visit]
    assert got["address"] == "890 East 135 Street, Bronx, NY"
    assert got["directions_url"] == "https://www.google.com/maps/dir/?api=1&destination=890%20East%20135%20Street%2C%20Bronx%2C%20NY"
    assert cs.put(f"/api/team/items/{visit}", json={"kind": "event"}).status_code == 200 and _items(cs)[visit]["address"], "kept on task -> event"
    assert cs.put(f"/api/team/items/{visit}", json={"address": ""}).status_code == 200 and _items(cs)[visit]["directions_url"] is None
    assert cs.post("/api/team/items", json={"title": "Teamitems x", "address": "x" * 301}).status_code == 400
    calls = []
    def fake(q):
        calls.append(q)
        return ["1 TEAM STREET, Manhattan, NY, USA", "1 TEAM PLACE, Bronx, NY, USA"]
    real = team_items._geo_fetch
    team_items._geo_fetch = fake
    team_items._SUGGEST_CACHE.clear()
    try:
        sug = pm.get("/api/team/address-suggest?q=1%20team").get_json()["data"]
        assert sug[0] == {"label": "1 Team St", "source": "job", "job": "Teamitems Job"}, sug
        assert {"label": "1 Team Street, Manhattan, NY", "source": "nyc", "job": None} in sug, sug
        assert pm.get("/api/team/address-suggest?q=1%20team").status_code == 200 and len(calls) == 1, "cached"
        assert pm.get("/api/team/address-suggest?q=1t").get_json()["data"] == [] and len(calls) == 1, "too short: no lookup"
        def down(q):
            raise TimeoutError("slow")
        team_items._geo_fetch = down
        team_items._SUGGEST_CACHE.clear()
        sug = sup.get("/api/team/address-suggest?q=1%20team").get_json()["data"]
        assert [x["source"] for x in sug] == ["job"], "the jobs still come back when the lookup is down"
    finally:
        team_items._geo_fetch = real
        team_items._SUGGEST_CACHE.clear()
    assert cl.get("/api/team/address-suggest?q=1%20team").status_code == 403 and anon.get("/api/team/address-suggest?q=x").status_code == 401
    passed += 1; print("  address ok")

    # ---- the dashboard + the date picker
    html = (ROOT / "company_portal.html").read_text(encoding="utf-8")
    assert 'id="tk-location"' not in html and "Place name" not in html, "events have the address only (Oct 13)"
    for needle in ('id="tk-card"', 'id="tk-overlay"', 'id="tk-kind-task"', 'id="tk-kind-event"', 'id="tk-assignee"', 'id="tk-project"',
                   'id="tk-due"', 'id="tk-due-time"', 'id="tk-start"', 'id="tk-end"', 'id="tk-start-time"', 'id="tk-end-time"',
                   'id="tk-delete"', 'id="tk-address"', 'id="tk-addr-list"', 'data-tk-dir',
                   "'https://www.google.com/maps/dir/?api=1&destination='", "address: $('tk-address').value.trim() || null", 'data-tk-filter="mine"', 'id="cal-new-item"',
                   "dateGet($('tk-due'))", "dateGet($('tk-start'))", "dateSet($('tk-due'), it.due_date)", "wireItemChips($('cc-week-row'), d);",
                   "wireItemChips($('cal-grid'), d);", "'/team/items/' + it.id + '/done'",
                   'id="dy-overlay"', 'id="dy-add"', "function openDay(iso, d)", "dayClicks($('cc-week-row'), () => DAY.week);",
                   "dayClicks($('cal-grid'), () => CAL.data);", 'data-day="${k}"', "openItem(null, { date: iso })",
                   "if (e.target.closest('a, button, [data-item], [data-job], [data-more], [data-ms], .cal-bar, .cc-week-bar, .cal-grip')) return;",
                   "past ? (info.headcount || 0) + ' on site' : ''"):
        assert needle in html, needle
    ov = html[html.index("async function loadOverview()"):html.index("// ---------- TASKS & EVENTS")]
    assert re.findall(r"kpi\('([^']+)'", ov) == ["Active projects", "Workforce"]
    assert "Money at a glance" not in html and "ov-money" not in html
    dp = (ROOT / "static" / "js" / "datepicker.js").read_text(encoding="utf-8")
    assert "e.type === 'click' && Date.now() - openedByFocus < 500" in dp, "the first click opens the picker"
    keys = {m for f in ROOT.glob("*.html") for m in re.findall(r"datepicker\.js\?v=(\w+)", f.read_text(encoding="utf-8"))}
    assert len(keys) == 1, keys
    passed += 1; print("  dashboard ok")

    print(f"PASS smoke_team_items ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_team_items: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
