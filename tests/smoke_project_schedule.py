#!/usr/bin/env python3
"""The job dashboard's Schedule window (accepted prototype 2026-10-23).

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * GET /api/projects/<code>/schedule returns job (dates, days left, time vs drops %, pace), the two-week
    board, the job's tasks / meetings in the window, the site pin and has_drops
  * the board is drafted from THIS week's Drop Plan progress: paging two weeks ahead still shows the drop's
    later stages (never an empty board), and a past window gets no invented auto bars
  * a reload keeps the drafted rows' ids; a stage edited by hand is never drafted twice
  * a job with no drops: has_drops False, no auto rows, its meeting still listed
  * PATCH / DELETE of an activity by id check the activity's job: a PM not on it -> 403 (before, any PM
    could edit any job's activity); the job's own PM -> 200; a manual edit survives the next load
  * the served page has the new view (job strip, board, phone agenda, edit sheet) and none of the old one
Run:  venv/bin/python tests/smoke_project_schedule.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_sched_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ["SSC_MAIL_FAKE_DIR"] = str(Path(_TMP) / "mail")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import project_schedule  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "sch"
PW = "ScheduleSmoke2026"
A, B = "SCH-A", "SCH-B"
DROP = "SCH-A-DP1"
TODAY = date.today()
MON = project_schedule.this_monday(TODAY)


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed() -> dict:
    conn = _db()
    ids = {}
    try:
        _clean(conn)
        for role in ("c_suite", "pm", "pm2"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), "pm" if role == "pm2" else role, f"Smoke {role} Sch"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        for code, name in ((A, "Sch facade"), (B, "Sch garage")):
            conn.execute("INSERT INTO projects (project_code, name, address, status, start_date, target_completion, site_lat, site_lng) "
                         "VALUES (?, ?, '1 Sch St', 'active', ?, ?, ?, ?)",
                         (code, name, (TODAY - timedelta(days=30)).isoformat(), (TODAY + timedelta(days=60)).isoformat(),
                          40.81 if code == A else None, -73.91 if code == A else None))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, ?)", (ids["pm"], A, "2026-10-01T00:00:00"))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, ?)", (ids["pm2"], B, "2026-10-01T00:00:00"))
        conn.execute("INSERT INTO drops (drop_id, project_code, elevation, sequence_no, lifecycle) VALUES (?, ?, 'North', 1, 'scaffold_active')", (DROP, A))
        conn.execute("INSERT INTO stage_templates (project_code, name) VALUES (?, 'Sch template')", (A,))
        tid = conn.execute("SELECT template_id FROM stage_templates WHERE project_code = ?", (A,)).fetchone()["template_id"]
        # six 3-day stages = 18 working days: more than one two-week window
        for n in range(1, 7):
            conn.execute("INSERT INTO stage_template_steps (template_id, step_no, name, default_working_days, is_signoff_gate) "
                         "VALUES (?, ?, ?, 3, ?)", (tid, n, f"Stage {n}", 1 if n == 6 else 0))
            conn.execute("INSERT INTO drop_stage_status (drop_id, step_no, status, started_on) VALUES (?, ?, ?, ?)",
                         (DROP, n, "in_progress" if n == 1 else "not_started", MON.isoformat() if n == 1 else None))
        for code, title in ((A, "Sch owner meeting"), (B, "Sch deck walk")):
            conn.execute("INSERT INTO crm_task (title, kind, status, project_code, start_date, start_time, created_at) "
                         "VALUES (?, 'meeting', 'open', ?, ?, '10:00', ?)", (title, code, (MON + timedelta(days=2)).isoformat(), "2026-10-01T00:00:00"))
        conn.commit()
        return ids
    finally:
        conn.close()


def _clean(conn) -> None:
    codes = [A, B]
    purge(conn, "drop_stage_status", "drop_id", [DROP])
    tids = [r["template_id"] for r in conn.execute("SELECT template_id FROM stage_templates WHERE project_code IN (?, ?)", codes).fetchall()]
    purge(conn, "stage_template_steps", "template_id", tids)
    for t in ("stage_templates", "drops", "lookahead_activity", "crm_task", "pm_project_assignment", "projects"):
        purge(conn, t, "project_code", codes)
    uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%@example.com",)).fetchall()]
    purge_users(conn, uids)
    conn.commit()


def _login(role):
    c = server.app.test_client()
    assert c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW}).status_code == 200, role
    return c


def _acts(board):
    return [a for g in board["groups"] for a in g["activities"]] + list(board["general"])


def main() -> int:
    _seed()
    passed = 0
    cs, pm, pm2 = _login("c_suite"), _login("pm"), _login("pm2")
    try:
        r = cs.get(f"/api/projects/{A}/schedule?start={MON.isoformat()}")
        assert r.status_code == 200, r.get_json()
        d = r.get_json()["data"]
        assert set(d) >= {"job", "lookahead", "items", "site", "has_drops"}, sorted(d)
        j = d["job"]
        assert j["days_left"] == 60 and j["drops_total"] == 1 and j["drops_complete"] == 0 and j["drops_pct"] == 0, j
        assert j["time_pct"] and j["pace"] == "behind", j
        assert d["site"] == {"lat": 40.81, "lng": -73.91} and d["has_drops"] is True
        assert [i["title"] for i in d["items"]] == ["Sch owner meeting"], d["items"]
        assert d["items"][0]["time"] == "10:00" and d["items"][0]["kind"] == "meeting"
        g = d["lookahead"]["groups"]
        assert len(g) == 1 and g[0]["drop_id"] == DROP, g
        names = [a["name"] for a in g[0]["activities"] if a["activity_type"] == "stage"]
        assert names[:3] == ["Stage 1", "Stage 2", "Stage 3"], names
        assert all(a["source"] == "auto" for a in g[0]["activities"])
        again = cs.get(f"/api/projects/{A}/schedule?start={MON.isoformat()}").get_json()["data"]
        assert sorted(a["id"] for a in _acts(again["lookahead"])) == sorted(a["id"] for a in _acts(d["lookahead"])), \
            "a reload keeps every drafted row's id"
        passed += 1; print("  payload ok")

        nxt = (MON + timedelta(days=14)).isoformat()
        d2 = cs.get(f"/api/projects/{A}/schedule?start={nxt}").get_json()["data"]
        later = [a["name"] for a in _acts(d2["lookahead"]) if a["grid"]]
        assert "Stage 5" in later and "Stage 1" not in later, later   # projected from this week, not restarted
        past = (MON - timedelta(days=28)).isoformat()
        d3 = cs.get(f"/api/projects/{A}/schedule?start={past}").get_json()["data"]
        assert not [a for a in _acts(d3["lookahead"]) if a["grid"] and a["source"] == "auto"], "no invented past bars"
        passed += 1; print("  paging ahead / back ok")

        db = cs.get(f"/api/projects/{B}/schedule").get_json()["data"]
        assert db["has_drops"] is False and db["site"] is None and not _acts(db["lookahead"]), db["lookahead"]
        assert [i["title"] for i in db["items"]] == ["Sch deck walk"], db["items"]
        assert db["lookahead"]["window_start"] == MON.isoformat(), "default start is this Monday"
        assert cs.get("/api/projects/SCH-NOPE/schedule").status_code in (403, 404)
        passed += 1; print("  job without drops ok")

        add = cs.post(f"/api/projects/{A}/lookahead/activities", json={
            "name": "Sch rain day cleanup", "activity_type": "work",
            "planned_start": MON.isoformat(), "planned_finish": MON.isoformat()})
        assert add.status_code == 201, add.get_json()
        aid = add.get_json()["data"]["id"]
        auto_id = next(a["id"] for a in _acts(d["lookahead"]) if a["source"] == "auto")
        # the other job's PM: refused on both, nothing changed
        assert pm2.patch(f"/api/lookahead/activities/{aid}", json={"name": "hijack"}).status_code == 403
        assert pm2.delete(f"/api/lookahead/activities/{aid}").status_code == 403
        assert pm2.get(f"/api/projects/{A}/schedule").status_code == 403
        assert cs.patch("/api/lookahead/activities/999999", json={"name": "x"}).status_code == 404
        assert cs.delete("/api/lookahead/activities/999999").status_code == 404
        # this job's PM edits; a manual edit survives the next (re-drafting) load
        new_start = (MON + timedelta(days=1)).isoformat()
        r = pm.patch(f"/api/lookahead/activities/{aid}", json={"planned_start": new_start, "planned_finish": new_start, "crew": "Crew B"})
        assert r.status_code == 200 and r.get_json()["data"]["crew"] == "Crew B", r.get_json()
        assert pm.patch(f"/api/lookahead/activities/{auto_id}", json={"crew": "Crew A"}).status_code == 200
        d4 = pm.get(f"/api/projects/{A}/schedule?start={MON.isoformat()}").get_json()["data"]
        acts = {a["id"]: a for a in _acts(d4["lookahead"])}
        assert aid in acts and acts[aid]["planned_start"] == new_start and acts[aid]["source"] == "manual", acts.get(aid)
        assert auto_id in acts and acts[auto_id]["source"] == "manual" and acts[auto_id]["crew"] == "Crew A", "dragged auto row locked"
        step = acts[auto_id]["source_step"]
        same = [a for a in acts.values() if a["drop_id"] == DROP and a["activity_type"] == "stage" and a["source_step"] == step]
        assert len(same) == 1, "an edited stage is not drafted a second time"
        assert pm.delete(f"/api/lookahead/activities/{aid}").status_code == 200
        d5 = pm.get(f"/api/projects/{A}/lookahead?start={MON.isoformat()}").get_json()["data"]
        assert aid not in {a["id"] for a in _acts(d5)}
        passed += 1; print("  scope + edits ok")

        html = cs.get(f"/projects/{A}").get_data(as_text=True)
        for needle in ('data-view="schedule"', 'id="sc-job"', 'id="sc-board"', 'id="sc-agenda"', 'id="sc-sheet"',
                       "/schedule?start=", "/api/weather/week?lat=", "SSCDatePicker.getISO", 'class="sc-top"'):
            assert needle in html, needle
        for gone in ('id="la-board"', 'id="la-modal"', "la-cflag", "&refresh=1", "lam-save"):
            assert gone not in html, gone
        passed += 1; print("  page ok")
    finally:
        conn = _db()
        try:
            _clean(conn)
        finally:
            conn.close()
    print(f"smoke_project_schedule: {passed}/5 groups passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
