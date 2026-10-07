#!/usr/bin/env python3
"""2b — the team HOME for PMs and supers (/projects) and the team calendar feed. Guard.

Prototype accepted by Angela on 2026-10-07: every dated job shows on the team week by name
and dates only (a job someone cannot open is drawn, never linked); PMs and supers read tasks
and tick their own done, they never create or edit; the calendar shows everyone's tasks and
events, the to-do list only theirs.

Boots the real app on an ISOLATED database. Proves:
  * GET /api/team/calendar: anonymous 401, a client 403; pm / super / c_suite 200; from + to
    required; a window over 120 days -> 400
  * jobs carry ONLY name, dates, colour, status and can_open / mine — no client, PM, crew,
    on-site counts, daily reports, document or invoice milestones (no money)
  * can_open follows the real project-access rule: a pm opens only their assigned active job,
    a super any active job, c_suite every job; `mine` = assigned to this person
  * items: everyone's tasks and events in the window, `mine` for the caller's own
  * the pm can tick their own task done and cannot create one
  * the page served at /projects is the home: to-do from /api/team/items?status=open&mine=1,
    the team calendar from /api/team/calendar, a details panel with Google Maps directions,
    "Mark as done" only for their own tasks, the read-only note; keeps the logo slot and the
    versioned account-menu script; LOCAL dates only (no toISOString)
Run:  venv/bin/python tests/smoke_team_home_2b.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_team_home_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "teamhome"
PW = "TeamHomeSmoke2026"
T = date.today()
D = lambda n: (T + timedelta(days=n)).isoformat()   # noqa: E731
MON = T - timedelta(days=T.weekday())
A, B, X = "TH2B-A", "TH2B-B", "TH2B-X"   # pm's job, someone else's job, a closed job
JOB_KEYS = {"project_code", "name", "start_date", "end_date", "open_ended", "color", "status", "today_on", "can_open", "mine"}


def _db():
    return db_layer.connect(pragma_fk=False)


def _uids(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]


def _seed() -> dict:
    conn = _db()
    try:
        uids = _uids(conn)
        purge(conn, "crm_task", "created_by", uids)
        purge(conn, "pm_project_assignment", "project_code", [A, B, X])
        purge(conn, "projects", "project_code", [A, B, X])
        conn.commit()
        purge_users(conn, uids)
        for code, name, status in ((A, "TeamHome Alpha", "active"), (B, "TeamHome Bravo", "active"), (X, "TeamHome Closed", "closed")):
            conn.execute("INSERT INTO projects (project_code, name, address, status, start_date, target_completion, updated_at) "
                         "VALUES (?, ?, '1 Home St', ?, ?, ?, ?)", (code, name, status, MON.isoformat(), D(30), D(0)))
        ids = {}
        for key, role in (("cs", "c_suite"), ("pm", "pm"), ("pm2", "pm"), ("super", "super"), ("client", "client")):
            email = f"{TAG}.{key}@example.com"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, display_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, ?, 1, 'active', 0, 0)", (email, auth.hash_password(PW), role, f"TeamHome {key}", f"TeamHome {key}"))
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, ?)", (ids["pm"], A, D(0)))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, ?)", (ids["pm2"], B, D(0)))
        conn.commit()
        return ids
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        uids = _uids(conn)
        purge(conn, "crm_task", "created_by", uids)
        purge(conn, "pm_project_assignment", "project_code", [A, B, X])
        purge(conn, "projects", "project_code", [A, B, X])
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


def main() -> int:
    ids = _seed()
    passed = 0
    cs, pm, sup, cl = (_login(k) for k in ("cs", "pm", "super", "client"))
    week = f"from={MON.isoformat()}&to={(MON + timedelta(days=6)).isoformat()}"

    # ---- gates
    assert server.app.test_client().get("/api/team/calendar?" + week).status_code == 401
    assert cl.get("/api/team/calendar?" + week).status_code == 403
    assert pm.get("/api/team/calendar").status_code == 400 and pm.get(f"/api/team/calendar?from={D(0)}").status_code == 400
    assert pm.get(f"/api/team/calendar?from={D(0)}&to={D(200)}").status_code == 400, "a window is at most 120 days"
    passed += 1; print("  gates ok")

    # ---- items from admin: one for the pm, one for the super
    r = cs.post("/api/team/items", json={"title": "TeamHome call the owner", "assignee_user_id": ids["pm"], "due_date": D(0),
                                         "project_code": A, "address": "1 Home St, Bronx, NY"})
    assert r.status_code == 201, r.get_json()
    t_pm = r.get_json()["data"]["id"]
    r = cs.post("/api/team/items", json={"kind": "event", "title": "TeamHome inspection", "assignee_user_id": ids["super"], "start_date": D(0)})
    t_sup = r.get_json()["data"]["id"]
    assert pm.post("/api/team/items", json={"title": "TeamHome nope"}).status_code == 403, "pm cannot create"
    passed += 1; print("  items ok")

    # ---- what each role gets
    def cal(c):
        r = c.get("/api/team/calendar?" + week)
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        d = r.get_json()["data"]
        assert set(d) == {"range", "jobs", "items"}, set(d)
        return {j["project_code"]: j for j in d["jobs"] if j["project_code"] in (A, B, X)}, {i["id"]: i for i in d["items"] if i["title"].startswith("TeamHome")}
    jobs, items = cal(pm)
    assert set(jobs) == {A, B, X}, "every dated job is on the team calendar"
    for j in jobs.values():
        assert set(j) == JOB_KEYS, set(j) ^ JOB_KEYS
    assert jobs[A]["can_open"] and jobs[A]["mine"], "the pm opens their own job"
    assert not jobs[B]["can_open"] and not jobs[B]["mine"], "someone else's job is drawn, not openable"
    assert not jobs[X]["can_open"], "a closed job never opens for a pm"
    assert set(items) == {t_pm, t_sup}, "everyone's tasks and events"
    assert items[t_pm]["mine"] and not items[t_sup]["mine"] and items[t_pm]["can_complete"] and not items[t_pm]["can_edit"]
    assert items[t_pm]["directions_url"].startswith("https://www.google.com/maps/dir/?api=1&destination=")
    jobs, items = cal(sup)
    assert jobs[A]["can_open"] and jobs[B]["can_open"] and not jobs[X]["can_open"], "a super opens any active job"
    assert not jobs[A]["mine"] and items[t_sup]["mine"]
    jobs, _ = cal(cs)
    assert all(j["can_open"] for j in jobs.values()), "c_suite opens every job"
    passed += 1; print("  calendar ok")

    # ---- the pm ticks their own task; the to-do list is theirs only
    mine = pm.get("/api/team/items?status=open&mine=1").get_json()["data"]
    assert [i["id"] for i in mine if i["title"].startswith("TeamHome")] == [t_pm]
    assert pm.post(f"/api/team/items/{t_sup}/done", json={"done": True}).status_code == 403
    assert pm.post(f"/api/team/items/{t_pm}/done", json={"done": True}).status_code == 200
    assert t_pm not in [i["id"] for i in pm.get("/api/team/items?status=open&mine=1").get_json()["data"]]
    passed += 1; print("  to-do ok")

    # ---- the page
    page = pm.get("/projects").get_data(as_text=True)
    for needle in ("'/api/team/items?status=open&mine=1'", "'/api/team/calendar?from='", "Get directions in Google Maps",
                   "Only admin and C-suite can change it.", "it.kind === 'task' && it.can_complete", "data-day=", "data-view=\"month\"",
                   '<img class="brand-mark" src="/files/static/brand/mark.svg"', "auth_menu.js?v=", "j.can_open ?", "'/api/projects'"):
        assert needle in page, needle
    assert "toISOString" not in page, "LOCAL dates only"
    assert "<title>Superstars Contracting · Home</title>" in page
    passed += 1; print("  page ok")

    print(f"PASS smoke_team_home_2b ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_team_home_2b: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
