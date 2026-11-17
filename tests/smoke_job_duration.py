#!/usr/bin/env python3
"""Job duration — anticipated vs. target vs. actual completion. Behavioral guard.

Proves:
  * the migration adds projects.anticipated_completion / actual_completion once and is a no-op after
  * PUT …/details saves the anticipated date, refuses one before the start, and refuses an actual
    completion on an open job or in the future
  * closing (POST …/status) stamps actual_completion = today, or the date given; a date before the
    start is refused and changes nothing; reopening clears it
  * GET …/details reports the durations in calendar days: expected, target, actual, and the variance
    against the anticipated end (target while open, actual once closed)
  * the schedule's job block carries the anticipated date and the duration
  * the Edit project window, job Settings and the Close confirmation carry the new fields
Run:  venv/bin/python tests/smoke_job_duration.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_jobdur_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import apply_job_duration as mig  # noqa: E402
import auth  # noqa: E402
import db_layer  # noqa: E402
import project_details  # noqa: E402
import server  # noqa: E402  (boots the app — the ensure runs in project_onboard.register)

TAG = "jobdur"
PW = "JobDurationSmoke2026"
CODE = "JD-SMK-001"
TODAY = date.today()


def D(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


class _Scratch:
    def __init__(self, path):
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        return self.c.execute(sql, params)

    def commit(self):
        self.c.commit()


def main() -> int:
    passed = 0

    # ---- migration: adds both columns once, then a no-op
    if not db_layer.is_postgres():
        sc = _Scratch(str(Path(_TMP) / "bare.db"))
        sc.execute("CREATE TABLE projects (project_code TEXT PRIMARY KEY, name TEXT)")
        sc.commit()
        assert mig.ensure(sc)["added"] == ["anticipated_completion", "actual_completion"]
        assert mig.ensure(sc)["added"] == []
    conn = db_layer.connect(pragma_fk=False)
    try:
        assert mig.ensure(conn)["added"] == []
    finally:
        conn.close()
    assert mig.main() == 0
    passed += 1; print("  migration ok")

    # ---- a job and an admin
    conn = db_layer.connect(pragma_fk=False)
    try:
        email = f"{TAG}.admin@example.com"
        conn.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                     "VALUES (?, ?, 'admin', 'Smoke admin Jobdur', 1, 'active', 0, 1)", (email, auth.hash_password(PW)))
        conn.execute("DELETE FROM projects WHERE project_code = ?", (CODE,))
        conn.execute("INSERT INTO projects (project_code, name, address, status, start_date, target_completion) "
                     "VALUES (?, 'Jobdur Facade', '1 Jobdur St', 'active', ?, ?)", (CODE, D(-100), D(10)))
        conn.commit()
    finally:
        conn.close()
    c = server.app.test_client()
    assert c.post("/api/auth/login", json={"email": email, "password": PW}).status_code == 200
    det = f"/api/admin/projects/{CODE}/details"

    # ---- edit: anticipated saved; bad dates refused
    r = c.put(det, json={"anticipated_completion": D(-20)})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    d = r.get_json()["data"]
    assert d["anticipated_completion"] == D(-20)
    u = d["duration"]
    assert (u["expected_days"], u["target_days"], u["measured"]) == (80, 110, "target"), u
    assert (u["variance_days"], u["variance_pct"]) == (30, 38), u
    assert u["actual_days"] is None and u["so_far_days"] == 100, u
    assert c.put(det, json={"anticipated_completion": D(-101)}).status_code == 400
    r = c.put(det, json={"actual_completion": D(-1)})
    assert r.status_code == 400 and "close the job" in r.get_json()["error"], r.get_data(as_text=True)[:200]
    passed += 1; print("  edit ok")

    # ---- close: today by default; a date before the start refused (nothing changes); a given date kept
    st = f"/api/projects/{CODE}/status"
    r = c.post(st, json={"status": "closed", "actual_completion": D(-101)})
    assert r.status_code == 400
    assert c.get(f"/api/projects/{CODE}/details").get_json()["data"]["status"] == "active"
    r = c.post(st, json={"status": "closed"})
    assert r.status_code == 200 and r.get_json()["data"]["actual_completion"] == D(0), r.get_data(as_text=True)[:200]
    r = c.post(st, json={"status": "active"})
    assert r.get_json()["data"]["actual_completion"] is None
    r = c.post(st, json={"status": "closed", "actual_completion": D(-8)})
    assert r.get_json()["data"]["actual_completion"] == D(-8)
    d = c.get(f"/api/projects/{CODE}/details").get_json()["data"]
    u = d["duration"]
    assert (u["actual_days"], u["measured"], u["variance_days"], u["so_far_days"]) == (92, "actual", 12, None), u
    # a closed job's actual date is editable here, never into the future
    assert c.put(det, json={"actual_completion": D(1)}).status_code == 400
    assert c.put(det, json={"actual_completion": D(-25)}).get_json()["data"]["duration"]["variance_days"] == -5
    passed += 1; print("  close / reopen ok")

    # ---- the schedule's job block carries it
    conn = db_layer.connect(pragma_fk=False)
    try:
        import project_schedule
        j = project_schedule._job(conn, CODE, TODAY)
        assert j["anticipated_completion"] == D(-20) and j["duration"]["variance_days"] == -5, j
        # the pure function: no anticipated date -> nothing to measure
        assert project_details.duration(D(0), None, D(5), None, "active")["variance_days"] is None
    finally:
        conn.close()
    passed += 1; print("  schedule ok")

    # ---- the UI carries the fields
    modal = (ROOT / "static/js/new_project_modal.js").read_text()
    for needle in ('id="np-ant"', 'id="np-act"', 'id="np-status"', 'id="np-dur"', "anticipated_completion:", "/status'"):
        assert needle in modal, needle
    assert "toISOString" not in modal
    dash = (ROOT / "dashboard-static.html").read_text()
    assert 'id="ps-f-ant"' in dash and "'Anticipated completion'" in dash and "durLine(d)" in dash
    adm = (ROOT / "admin_projects.html").read_text()
    assert 'id="cf-act"' in adm and "actual_completion: actual" in adm
    passed += 1; print("  ui ok")

    conn = db_layer.connect(pragma_fk=False)
    try:
        conn.execute("DELETE FROM projects WHERE project_code = ?", (CODE,))
        conn.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS smoke_job_duration ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
