#!/usr/bin/env python3
"""Projects → Calendar, easier to scan (2026-11-19, accepted prototype
https://claude.ai/artifact/Xs5kMdaim3nW3FzoBcSUEb). Behavioral guard.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * each job says what weather can stop it: rope access / suspended scaffold / boom lift -> wind,
    an exterior (FR) job -> rain, a garage (PG) -> neither; the access labels travel for the panel
  * the calendar read carries the open tasks already past their deadline (`overdue`): an open
    task from last week is there even though it is outside the window; a done one and a future
    one are not
  * the weather is New York City's whatever job has coordinates (never a job address)
  * the served page mounts static/js/jobs_calendar.js (+ css) on the Calendar tab and the
    dashboard's week strip opens days in the same panel; the old day modal is gone
  * the module: the views, NYC weather row, weather-risk + overdue flags, the assigned-to filter,
    and the private Google events are never printed
Run:  venv/bin/python tests/smoke_calendar_scan.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_cal_scan_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import portal_calendar  # noqa: E402
import server  # noqa: E402  (boots the whole app)
import team_items  # noqa: E402

TAG = "calscan"
PW = "CalendarScanSmoke2026"
A, B, C = "FR-CS-901", "PG-CS-902", "FR-CS-903"
TODAY = date.today()
API = "/api/company/portal/calendar"


def D(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def _db():
    return db_layer.connect(pragma_fk=False)


def _wipe(conn):
    tids = [r[0] for r in conn.execute("SELECT id FROM crm_task WHERE title LIKE 'Calscan%'").fetchall()]
    for t in ("crm_task_assignee", "crm_task_attendee", "team_item_notice", "team_item_log"):
        purge(conn, t, "task_id", tids)
    purge(conn, "crm_task", "id", tids)
    purge(conn, "projects", "project_code", [A, B, C])
    conn.commit()
    purge_users(conn, [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()])
    conn.commit()


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        _wipe(conn)
        for role in ("c_suite", "admin"):
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 0)", (f"{TAG}.{role}@example.com", auth.hash_password(PW), role, f"Calscan {role}"))
        ids["cs"] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.c_suite@example.com",)).fetchone()[0]
        ids["admin"] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.admin@example.com",)).fetchone()[0]
        rows = ((A, "Calscan Rope facade", '["rope_access"]', 41.0, -74.5), (B, "Calscan Garage", None, None, None), (C, "Calscan Plain facade", "[]", None, None))
        for code, name, acc, lat, lng in rows:
            conn.execute("INSERT INTO projects (project_code, name, status, start_date, target_completion, updated_at, access_methods, site_lat, site_lng) "
                         "VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?)", (code, name, D(-5), D(20), D(0), acc, lat, lng))
        conn.commit()
        ids["late"] = team_items.create(conn, {"kind": "task", "title": "Calscan late task", "assignee_user_id": ids["cs"], "due_date": D(-10)}, actor=ids["admin"])
        ids["done"] = team_items.create(conn, {"kind": "task", "title": "Calscan finished task", "assignee_user_id": ids["cs"], "due_date": D(-9)}, actor=ids["admin"])
        team_items.set_done(conn, ids["done"], True, actor=ids["admin"], actor_role="admin")
        ids["soon"] = team_items.create(conn, {"kind": "task", "title": "Calscan next week", "assignee_user_id": ids["cs"], "due_date": D(7)}, actor=ids["admin"])
        conn.commit()
    finally:
        conn.close()
    return ids


def _login(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code)
    return c


def main() -> int:
    ids = _seed()
    passed = 0
    cs = _login("c_suite")
    mon = TODAY - timedelta(days=TODAY.weekday())
    d = cs.get(API + f"?from={mon.isoformat()}&to={(mon + timedelta(days=6)).isoformat()}").get_json()["data"]
    jobs = {j["project_code"]: j for j in d["jobs"]}
    assert jobs[A]["weather_risk"] == {"wind": True, "rain": True, "access": ["Rope access"]}, jobs[A]
    assert jobs[B]["weather_risk"] == {"wind": False, "rain": False, "access": []}, jobs[B]
    assert jobs[C]["weather_risk"] == {"wind": False, "rain": True, "access": []}, jobs[C]
    assert portal_calendar.weather_risk("IRA-MN-002", [])["wind"] and not portal_calendar.weather_risk("IR-MN-002", ["boom_lift"])["rain"]
    passed += 1; print("  weather risk per job ok")

    od = {it["id"]: it for it in d["overdue"]}
    assert ids["late"] in od and od[ids["late"]]["overdue"] and od[ids["late"]]["assignee_ids"] == [ids["cs"]], d["overdue"]
    assert ids["done"] not in od and ids["soon"] not in od
    assert ids["late"] not in {it["id"] for it in d["items"]}, "the late task is outside the window; it comes back only in overdue"
    passed += 1; print("  overdue tasks ok")

    urls = []
    def fake_get(url):
        urls.append(url)
        return {"daily": {"time": [D(0)], "weather_code": [0], "temperature_2m_max": [50], "temperature_2m_min": [40],
                          "precipitation_probability_max": [5], "wind_speed_10m_max": [8], "wind_gusts_10m_max": [12]}}
    conn = _db()
    try:
        w = portal_calendar.weather(conn, TODAY, TODAY, get=fake_get)   # job A has coordinates far from the city
    finally:
        conn.close()
    assert "latitude=40.8083" in urls[0] and "longitude=-73.9162" in urls[0] and w["site"]["name"] == "New York City", urls
    passed += 1; print("  NYC weather ok")

    html = cs.get("/company").get_data(as_text=True)
    assert '/files/static/js/jobs_calendar.js' in html and '/files/static/css/jobs_calendar.css' in html
    assert "SSC_JOBCAL.mount(" in html and "JC.openDay(iso, d)" in html and 'id="jc-view"' in html and 'id="jc-layers"' in html
    assert 'id="dy-overlay"' not in html, "the old day modal is replaced by the panel"
    js = cs.get("/files/static/js/jobs_calendar.js").get_data(as_text=True)
    css = cs.get("/files/static/css/jobs_calendar.css").get_data(as_text=True)
    for needle in ("jc-wxrow", "weather_risk", "d.overdue", "Assigned to", "'/calendar/weather?from='", "My Google events",
                   "Swipe left or right", "isoOf("):
        assert needle in js, needle
    assert "toISOString" not in js, "local dates only"
    assert ".jc-chip.k-mine, .jc-ev.k-mine" in css and "@media print" in css, "private events never print"
    assert "milestones" not in js and "reports" not in js, "paperwork, money and daily reports stay off the calendar"
    passed += 1; print("  served page + module ok")

    print(f"PASS smoke_calendar_scan ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        conn = _db()
        try:
            _wipe(conn)
        finally:
            conn.close()
        shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
