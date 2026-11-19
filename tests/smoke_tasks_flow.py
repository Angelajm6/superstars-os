#!/usr/bin/env python3
"""Tasks flow (accepted prototype 2026-11-19: https://claude.ai/artifact/KnHuD7Ys632eNf2MCtNpmU). Guard.

Boots the real app on an ISOLATED database. Proves:
  * several people per task: assignee_user_ids keeps all of them in order; the first is still
    crm_task.assignee_user_id (older readers); each sees it under Mine; the list shows every name
  * crews: a PM / super adds a task for themselves (nobody named = theirs) and for the people on their
    own jobs; anyone else -> 403; a job that is not theirs -> 403; they change / delete only what they
    added; admin / C-suite give a task to anyone and change anything
  * the editor's lists (GET /api/team/form): the whole team with can_assign for this person, and their jobs
  * pop-ups (GET /api/team/notices): "assigned" for each person it is for (never the one doing it); the New
    dot (item.new) until they open it (POST /seen, which writes "saw it" once); "popped" stops a pop-up
    showing again; a time change tells the people on it; taking someone off before they saw it leaves
    no trace, after -> "removed"
  * done: any one of them finishes it for everyone; whoever added it and the others are told; Undo (reopen)
    takes the "finished" pop-ups back; someone it is not for -> 403
  * delete + Undo: DELETE tells the people on it (or takes back an unseen assignment); /restore brings it back
  * Activity: added + assigned to, saw it, changed the time, marked done (names at read; ids in the table)
  * "I assigned" (by=1): what this person handed to someone else
  * a task made elsewhere with one person (assignee_user_id) still works and still pops up
  * reminders: a timed task for several people reminds each of them
  * pages: team_tasks.js + team_tasks.css on the dashboard and the Inbox; company_nav.js loads the pop-ups for the
    team roles; the shared window's element ids never clash with a page's (tsk- prefix); titles never reach the log
Run:  venv/bin/python tests/smoke_tasks_flow.py
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_tasks_flow_")

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

TAG = "tasksflow"
PW = "TasksFlowSmoke2026"
A, B = "TASKFL-A", "TASKFL-B"
T = date.today()
D = lambda n: (T + timedelta(days=n)).isoformat()   # noqa: E731
SECRET = "Tasksflow call Zebediah about the parapet"   # must never reach the log


def _db():
    return db_layer.connect(pragma_fk=False)


def _uids(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]


def _wipe(conn):
    uids = _uids(conn)
    tids = [r[0] for r in conn.execute("SELECT id FROM crm_task WHERE title LIKE 'Tasksflow%'").fetchall()]
    for t in ("crm_task_assignee", "crm_task_attendee", "team_item_notice", "team_item_log"):
        purge(conn, t, "task_id", tids)
    purge(conn, "crm_task", "id", tids)
    purge(conn, "pm_project_assignment", "project_code", [A, B])
    purge(conn, "projects", "project_code", [A, B])
    conn.commit()
    purge_users(conn, uids)
    conn.commit()


def _seed() -> dict:
    conn = _db()
    try:
        team_items.ensure(conn)
        _wipe(conn)
        for code, name in ((A, "Tasksflow Job A"), (B, "Tasksflow Job B")):
            conn.execute("INSERT INTO projects (project_code, name, address, city_zip, status) VALUES (?, ?, '1 Flow St', 'Bronx, NY', 'active')", (code, name))
        ids = {}
        for key, role in (("admin", "admin"), ("cs", "c_suite"), ("pm", "pm"), ("sup", "super"), ("fore", "super"), ("other", "super")):
            email = f"{TAG}.{key}@example.com"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, display_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, ?, 1, 'active', 0, 0)", (email, auth.hash_password(PW), role, f"Tasksflow {key}", f"Tasksflow {key}"))
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        for key, code in (("pm", A), ("sup", A), ("fore", A), ("other", B)):   # sup's crew = pm + fore; other is on B only
            conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, ?)", (ids[key], code, D(0)))
        conn.commit()
        return ids
    finally:
        conn.close()


def _login(key):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@example.com", "password": PW})
    assert r.status_code == 200, (key, r.status_code)
    return c


def _ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.get_data(as_text=True)[:300])
    return r.get_json()


def _one(c, tid):
    return _ok(c.get(f"/api/team/items/{tid}"))["data"]


def _pops(c):
    return _ok(c.get("/api/team/notices"))["data"]


def _kinds(c, tid=None):
    return sorted(n["kind"] for n in _pops(c)["pop"] if tid is None or n["item"]["id"] == tid)


def main() -> int:
    ids = _seed()
    passed = 0
    admin, cs, pm, sup, fore, other = (_login(k) for k in ("admin", "cs", "pm", "sup", "fore", "other"))
    buf = io.StringIO(); h = logging.StreamHandler(buf); root = logging.getLogger(); lvl = root.level
    root.setLevel(logging.INFO); root.addHandler(h)

    # ---- the editor's lists
    f = _ok(sup.get("/api/team/form"))["data"]
    can = {p["id"] for p in f["people"] if p["can_assign"]}
    assert f["me"] == ids["sup"] and f["boss"] is False
    assert {ids["sup"], ids["pm"], ids["fore"]} <= can and ids["other"] not in can and ids["admin"] not in can, can
    assert {j["project_code"] for j in f["jobs"]} == {A}, "a super links only their own jobs"
    fa = _ok(admin.get("/api/team/form"))["data"]
    assert fa["boss"] is True and {ids["other"], ids["sup"], ids["cs"]} <= {p["id"] for p in fa["people"] if p["can_assign"]}
    assert {A, B} <= {j["project_code"] for j in fa["jobs"]}
    assert server.app.test_client().get("/api/team/form").status_code == 401
    passed += 1; print("  form ok")

    # ---- crews: a super adds for themselves and their crew, nobody else
    r = _ok(sup.post("/api/team/items", json={"title": "Tasksflow own"}), 201)
    own = r["data"]["id"]
    assert _one(sup, own)["assignee_ids"] == [ids["sup"]], "nobody named = their own"
    assert sup.post("/api/team/items", json={"title": "Tasksflow nope", "assignee_user_ids": [ids["other"]]}).status_code == 403
    assert sup.post("/api/team/items", json={"title": "Tasksflow nope", "assignee_user_ids": [ids["sup"]], "project_code": B}).status_code == 403
    assert sup.post("/api/team/items", json={"kind": "meeting", "title": "Tasksflow nope", "start_date": D(1),
                                             "attendee_user_ids": [ids["other"]]}).status_code == 400, "meeting people: crew only"
    r = _ok(sup.post("/api/team/items", json={"title": SECRET, "assignee_user_ids": [ids["fore"], ids["pm"]], "project_code": A,
                                              "due_date": D(1), "due_time": "08:00"}), 201)
    shared = r["data"]["id"]
    it = _one(sup, shared)
    assert it["assignee_ids"] == [ids["fore"], ids["pm"]] and it["assignee_user_id"] == ids["fore"], it["assignee_ids"]
    assert it["assignee_names"] == ["Tasksflow fore", "Tasksflow pm"] and it["can_edit"] is True
    for c in (fore, pm):
        mine = {i["id"]: i for i in _ok(c.get("/api/team/items?status=open&mine=1"))["data"]}
        assert shared in mine and mine[shared]["new"] is True and mine[shared]["can_edit"] is False and mine[shared]["can_complete"] is True
    assert shared not in {i["id"] for i in _ok(other.get("/api/team/items?status=open&mine=1"))["data"]}
    # change: only whoever added it, or admin / C-suite
    assert fore.put(f"/api/team/items/{shared}", json={"title": "Tasksflow hijack"}).status_code == 403
    assert fore.delete(f"/api/team/items/{shared}").status_code == 403
    assert sup.put(f"/api/team/items/{shared}", json={"assignee_user_ids": [ids["fore"], ids["other"]]}).status_code == 403, "still crew only on edit"
    assert pm.post("/api/team/items", json={"title": "Tasksflow nope", "assignee_user_ids": [ids["other"]]}).status_code == 403
    passed += 1; print("  crews ok")

    # ---- pop-ups + the New dot
    assert _kinds(fore, shared) == ["assigned"] and _kinds(pm, shared) == ["assigned"] and _kinds(sup, shared) == [], "never the one doing it"
    n = [x for x in _pops(fore)["pop"] if x["item"]["id"] == shared][0]
    assert n["actor"] == "Tasksflow" and n["item"]["title"] == SECRET and _pops(fore)["new"] >= 1
    _ok(fore.post("/api/team/notices/popped", json={"ids": [n["id"]]}))
    assert _kinds(fore, shared) == [], "a closed pop-up never shows again"
    assert _one(fore, shared)["new"] is True, "closing the pop-up keeps the New dot"
    _ok(other.post("/api/team/notices/popped", json={"ids": [n["id"]]}))   # someone else's id: nothing happens
    _ok(fore.post(f"/api/team/items/{shared}/seen"))
    assert _one(fore, shared)["new"] is False, "opening it clears the dot"
    _ok(fore.post(f"/api/team/items/{shared}/seen"))
    acts = [a["text"] for a in _one(admin, shared)["activity"]]
    assert acts[-1] == "Tasksflow sup added it and assigned it to Tasksflow fore and Tasksflow pm", acts
    assert acts.count("Tasksflow fore saw it") == 1, acts
    # a time change tells the people on it (not the one changing it)
    _ok(sup.put(f"/api/team/items/{shared}", json={"due_time": "09:30"}))
    assert _kinds(fore, shared) == ["moved"] and _kinds(pm, shared) == ["assigned"], "an unseen assignment already shows the latest"
    assert "Tasksflow sup changed the time" in [a["text"] for a in _one(admin, shared)["activity"]]
    # taking someone off: before they saw it -> nothing; after -> "removed"
    _ok(sup.put(f"/api/team/items/{shared}", json={"assignee_user_ids": [ids["fore"]]}))
    assert _kinds(pm, shared) == [] and shared not in {i["id"] for i in _ok(pm.get("/api/team/items?status=open&mine=1"))["data"]}
    _ok(sup.put(f"/api/team/items/{shared}", json={"assignee_user_ids": [ids["fore"], ids["pm"]]}))
    assert _kinds(pm, shared) == ["assigned"]
    _ok(pm.post(f"/api/team/items/{shared}/seen"))
    _ok(sup.put(f"/api/team/items/{shared}", json={"assignee_user_ids": [ids["fore"]]}))
    assert _kinds(pm, shared) == ["removed"]
    _ok(sup.put(f"/api/team/items/{shared}", json={"assignee_user_ids": [ids["fore"], ids["pm"]]}))
    passed += 1; print("  pop-ups ok")

    # ---- done: one finishes it for everyone; the others hear; Undo takes it back
    for c in (fore, pm, sup):
        for x in _pops(c)["pop"]:
            _ok(c.post("/api/team/notices/popped", json={"ids": [x["id"]]}))
    assert other.post(f"/api/team/items/{shared}/done", json={"done": True}).status_code == 403
    _ok(pm.post(f"/api/team/items/{shared}/done", json={"done": True}))
    assert _one(fore, shared)["status"] == "done", "done for everyone"
    assert _kinds(sup, shared) == ["done"] and _kinds(fore, shared) == ["done"] and _kinds(pm, shared) == []
    _ok(pm.post(f"/api/team/items/{shared}/done", json={"done": False}))
    assert _kinds(sup, shared) == [] and _kinds(fore, shared) == [], "Undo takes the 'finished' pop-ups back"
    _ok(fore.post(f"/api/team/items/{shared}/done", json={"done": True}))
    pop = [x for x in _pops(sup)["pop"] if x["item"]["id"] == shared][0]
    assert pop["kind"] == "done" and pop["actor"] == "Tasksflow" and pop["item"]["status"] == "done"
    acts = [a["text"] for a in _one(admin, shared)["activity"]]
    assert acts[0] == "Tasksflow fore marked it done" and "Tasksflow pm reopened it" in acts, acts
    passed += 1; print("  done ok")

    # ---- I assigned
    by = {i["id"] for i in _ok(sup.get("/api/team/items?status=all&by=1"))["data"]}
    assert shared in by and own not in by, "handed to someone else, not their own"
    passed += 1; print("  I assigned ok")

    # ---- delete + Undo
    r = _ok(admin.post("/api/team/items", json={"title": "Tasksflow walkthrough", "assignee_user_ids": [ids["sup"], ids["other"]]}), 201)
    walk = r["data"]["id"]
    _ok(sup.post(f"/api/team/items/{walk}/seen"))
    assert sup.delete(f"/api/team/items/{walk}").status_code == 403, "not theirs to delete"
    _ok(admin.delete(f"/api/team/items/{walk}"))
    assert _kinds(other, walk) == [], "an assignment they never saw just goes"
    assert _kinds(sup, walk) == ["cancelled"], "someone who saw it hears it was deleted"
    assert sup.post(f"/api/team/items/{walk}/restore").status_code == 403
    _ok(admin.post(f"/api/team/items/{walk}/restore"))
    assert _kinds(sup, walk) == [] and _one(sup, walk)["status"] == "open"
    assert admin.post(f"/api/team/items/{walk}/restore").status_code == 404, "only a deleted one comes back"
    _ok(sup.delete(f"/api/team/items/{own}"))   # their own
    passed += 1; print("  delete + undo ok")

    # ---- a task made elsewhere with one person still works and pops up
    conn = _db()
    try:
        tid = team_items.create(conn, {"kind": "task", "title": "Tasksflow chase invoice", "assignee_user_id": ids["cs"], "due_date": D(2)},
                                actor=ids["admin"])
    finally:
        conn.close()
    it = _one(cs, tid)
    assert it["assignee_ids"] == [ids["cs"]] and it["new"] is True and _kinds(cs, tid) == ["assigned"]
    conn = _db()
    try:
        conn.execute("INSERT INTO crm_task (entity_type, title, assignee_user_id, status, created_by, created_at, kind, due_date) "
                     "VALUES ('none', 'Tasksflow legacy row', ?, 'open', ?, ?, 'task', ?)", (ids["pm"], ids["admin"], datetime.now().isoformat(timespec="seconds"), D(3)))
        conn.commit()
        legacy = conn.execute("SELECT MAX(id) FROM crm_task").fetchone()[0]
    finally:
        conn.close()
    li = _one(pm, legacy)
    assert li["assignee_ids"] == [ids["pm"]] and li["mine"] is True and li["can_complete"] is True, "a row with only assignee_user_id"
    assert any(a["text"] == "Tasksflow admin added it" for a in li["activity"])
    passed += 1; print("  older tasks ok")

    # ---- reminders: a timed task for several people reminds each
    import event_reminders
    conn = _db()
    try:
        due = [x for x in event_reminders._team_items(conn, D(0), D(3)) if x["id"] == shared]
    finally:
        conn.close()
    # shared is done now; make a fresh timed one
    r = _ok(sup.post("/api/team/items", json={"title": "Tasksflow timed", "assignee_user_ids": [ids["fore"], ids["pm"]], "due_date": D(1), "due_time": "07:00"}), 201)
    conn = _db()
    try:
        due = [x for x in event_reminders._team_items(conn, D(0), D(3)) if x["id"] == r["data"]["id"]]
    finally:
        conn.close()
    assert due and {ids["fore"], ids["pm"]} <= set(due[0]["people"]), due
    passed += 1; print("  reminders ok")

    # ---- the log never carries a title
    root.removeHandler(h); root.setLevel(lvl)
    log = buf.getvalue()
    assert "team item created id=" in log and "Zebediah" not in log, "ids only in the log"
    passed += 1; print("  log ok")

    # ---- pages
    portal = (ROOT / "company_portal.html").read_text(encoding="utf-8")
    inbox = (ROOT / "inbox_page.html").read_text(encoding="utf-8")
    nav = (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")
    tt = (ROOT / "static" / "js" / "team_tasks.js").read_text(encoding="utf-8")
    alerts = (ROOT / "static" / "js" / "task_alerts.js").read_text(encoding="utf-8")
    for page in (portal, inbox):
        assert "team_tasks.js?v=" in page and "team_tasks.css?v=" in page and "SSC_TASKS.quick(" in page
    assert "task_alerts.js?v=" in nav and "allowed(['admin', 'c_suite', 'pm', 'super'])" in nav
    assert portal.index('id="tk-card"') < portal.index('class="td-cols"'), "the task list sits under the week, not at the bottom"
    assert portal.index('id="td-quick"') < portal.index('id="ov-kpis"'), "add a task from the top of the dashboard"
    assert 'id="tk-overlay"' not in portal, "one shared window, not a second copy in the page"
    page_ids = set(re.findall(r'id="([\w-]+)"', portal + inbox))
    tt_ids = set(re.findall(r'id="([\w-]+)"', tt))
    assert tt_ids and all(i.startswith("tsk-") for i in tt_ids), sorted(i for i in tt_ids if not i.startswith("tsk-"))
    assert not (tt_ids & page_ids), tt_ids & page_ids
    assert "/api/team/notices" in alerts and "/team/notices/popped" in alerts and ".ab-banners" in alerts
    assert "SSC_TASKS.view(r)" in (ROOT / "static" / "js" / "inbox_schedule.js").read_text(encoding="utf-8")
    assert "type=\"time\"" not in tt, "the time box replaces the browser's hh:mm squares"
    passed += 1; print("  pages ok")

    print(f"PASS smoke_tasks_flow ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_tasks_flow: {e}")
        rc = 1
    finally:
        conn = _db()
        try:
            _wipe(conn)
        finally:
            conn.close()
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(rc)
