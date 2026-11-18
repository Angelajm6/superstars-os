#!/usr/bin/env python3
"""Job duration (v2) — Anticipated completion (target_completion), the original date kept by the
app (original_completion), and Actual completion. Behavioral guard.

Proves:
  * the migrations add the columns once; the v2 backfill moves the retired v1 baseline
    (anticipated_completion) into the original date — or into the date itself where a job had none —
    gives every dated job an original, empties the retired column, and is a no-op when run again
  * the first anticipated date is kept as the original; moving the date (Edit details, or Crew & site
    as the calendar drag does) never moves the original; clearing the date clears it; the retired
    `anticipated_completion` key is ignored
  * the duration is measured against the original: expected, current, moved, actual, variance
  * closing stamps actual_completion = today, or the date given; a date before the start is refused
    and changes nothing; reopening clears it; a closed job's actual date can't go into the future
  * the schedule's job block carries the original date and the duration
  * the Edit project window, job Settings and the Close confirmation carry the v2 fields
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

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import apply_completion_original as mig2  # noqa: E402
import apply_job_duration as mig  # noqa: E402
import auth  # noqa: E402
import db_layer  # noqa: E402
import project_details  # noqa: E402
import server  # noqa: E402  (boots the app — the ensures run in project_onboard.register)

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


def _purge(conn, email: str) -> None:
    uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchall()]
    purge_users(conn, uids)
    conn.commit()


def main() -> int:
    passed = 0

    # ---- migrations: columns once; the v2 backfill (scratch SQLite, as the column probe follows the backend)
    if not db_layer.is_postgres():
        sc = _Scratch(str(Path(_TMP) / "bare.db"))
        sc.execute("CREATE TABLE projects (project_code TEXT PRIMARY KEY, name TEXT, target_completion TEXT)")
        sc.commit()
        assert mig.ensure(sc)["added"] == ["anticipated_completion", "actual_completion"]
        rows = (("A", "2026-12-31", "2026-12-01"),   # v1: both -> original = the v1 baseline
                ("B", None, "2026-12-05"),           # v1: baseline only -> it becomes the date too
                ("C", "2026-12-20", None),           # dated, never touched -> original = the date
                ("E", None, None))
        for code, t, a in rows:
            sc.execute("INSERT INTO projects (project_code, target_completion, anticipated_completion) VALUES (?, ?, ?)", (code, t, a))
        sc.commit()
        assert mig2.ensure(sc)["added"] == ["original_completion"]
        want = {"A": ("2026-12-31", "2026-12-01"), "B": ("2026-12-05", "2026-12-05"), "C": ("2026-12-20", "2026-12-20"), "E": (None, None)}

        def state():
            return {r["project_code"]: (r["target_completion"], r["original_completion"]) for r in
                    sc.execute("SELECT project_code, target_completion, original_completion FROM projects").fetchall()}
        assert state() == want, state()
        assert sc.execute("SELECT COUNT(*) AS n FROM projects WHERE anticipated_completion IS NOT NULL").fetchone()["n"] == 0
        sc.execute("UPDATE projects SET target_completion = NULL, original_completion = NULL WHERE project_code = 'B'")   # cleared later
        sc.commit()
        assert mig2.ensure(sc)["added"] == []
        assert state()["B"] == (None, None), "a cleared date must not come back on the next boot"
    conn = db_layer.connect(pragma_fk=False)
    try:
        assert mig.ensure(conn)["added"] == [] and mig2.ensure(conn)["added"] == []
    finally:
        conn.close()
    assert mig.main() == 0 and mig2.main() == 0
    passed += 1; print("  migrations ok")

    # ---- a job and an admin
    conn = db_layer.connect(pragma_fk=False)
    try:
        email = f"{TAG}.admin@example.com"
        _purge(conn, email)
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                     "VALUES (?, ?, 'admin', 'Smoke admin Jobdur', 1, 'active', 0, 1)", (email, auth.hash_password(PW)))
        conn.execute("DELETE FROM projects WHERE project_code = ?", (CODE,))
        conn.execute("INSERT INTO projects (project_code, name, address, status, start_date) "
                     "VALUES (?, 'Jobdur Facade', '1 Jobdur St', 'active', ?)", (CODE, D(-100)))
        conn.commit()
    finally:
        conn.close()
    c = server.app.test_client()
    assert c.post("/api/auth/login", json={"email": email, "password": PW}).status_code == 200
    det = f"/api/admin/projects/{CODE}/details"

    def get():
        return c.get(f"/api/projects/{CODE}/details").get_json()["data"]

    # ---- the first anticipated date is kept; moving the date never moves it
    d = c.put(det, json={"target_completion": D(-20)}).get_json()["data"]
    assert d["original_completion"] == D(-20), d
    u = d["duration"]
    assert (u["expected_days"], u["current_days"], u["moved_days"], u["variance_days"]) == (80, 80, 0, 0), u
    d = c.put(det, json={"target_completion": D(10), "anticipated_completion": D(-50)}).get_json()["data"]
    assert d["target_completion"] == D(10) and d["original_completion"] == D(-20), d
    u = d["duration"]
    assert (u["expected_days"], u["current_days"], u["moved_days"], u["measured"]) == (80, 110, 30, "anticipated"), u
    assert (u["variance_days"], u["variance_pct"], u["so_far_days"], u["actual_days"]) == (30, 38, 100, None), u
    r = c.put(f"/api/admin/projects/{CODE}/crew", json={"start_date": D(-100), "target_completion": D(12)})   # the calendar drag
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert (get()["target_completion"], get()["original_completion"]) == (D(12), D(-20))
    d = c.put(det, json={"target_completion": ""}).get_json()["data"]
    assert d["target_completion"] is None and d["original_completion"] is None, d
    c.put(det, json={"target_completion": D(-20)})
    d = c.put(det, json={"target_completion": D(10)}).get_json()["data"]
    assert d["original_completion"] == D(-20), d
    assert c.put(det, json={"target_completion": D(-101)}).status_code == 400
    r = c.put(det, json={"actual_completion": D(-1)})
    assert r.status_code == 400 and "close the job" in r.get_json()["error"], r.get_data(as_text=True)[:200]
    passed += 1; print("  original date ok")

    # ---- close: today by default; a date before the start refused (nothing changes); a given date kept
    st = f"/api/projects/{CODE}/status"
    assert c.post(st, json={"status": "closed", "actual_completion": D(-101)}).status_code == 400
    assert get()["status"] == "active"
    r = c.post(st, json={"status": "closed"})
    assert r.status_code == 200 and r.get_json()["data"]["actual_completion"] == D(0), r.get_data(as_text=True)[:200]
    assert c.post(st, json={"status": "active"}).get_json()["data"]["actual_completion"] is None
    assert c.post(st, json={"status": "closed", "actual_completion": D(-8)}).get_json()["data"]["actual_completion"] == D(-8)
    u = get()["duration"]
    assert (u["actual_days"], u["measured"], u["variance_days"], u["so_far_days"]) == (92, "actual", 12, None), u
    assert c.put(det, json={"actual_completion": D(1)}).status_code == 400
    assert c.put(det, json={"actual_completion": D(-25)}).get_json()["data"]["duration"]["variance_days"] == -5
    passed += 1; print("  close / reopen ok")

    # ---- the schedule's job block carries it
    conn = db_layer.connect(pragma_fk=False)
    try:
        import project_schedule
        j = project_schedule._job(conn, CODE, TODAY)
        assert j["original_completion"] == D(-20) and j["duration"]["variance_days"] == -5, j
        assert project_details.duration(D(0), None, None, None, "active")["variance_days"] is None
        assert project_details.duration(D(0), None, D(5), None, "active")["expected_days"] == 5   # no original yet: the date itself
    finally:
        conn.close()
    passed += 1; print("  schedule ok")

    # ---- the UI carries the v2 fields
    modal = (ROOT / "static/js/new_project_modal.js").read_text()
    for needle in ('<label for="np-end">Anticipated completion</label>', 'id="np-end-hint"', 'id="np-act"', 'id="np-status"',
                   'id="np-dur"', "NP.cur.original", "/status'"):
        assert needle in modal, needle
    assert 'id="np-ant"' not in modal and "anticipated_completion" not in modal and "toISOString" not in modal
    dash = (ROOT / "dashboard-static.html").read_text()
    assert '<label for="ps-f-target">Anticipated completion</label>' in dash and "movedNote(d)" in dash
    assert "ps-f-ant" not in dash and "anticipated_completion" not in dash
    adm = (ROOT / "admin_projects.html").read_text()
    assert 'id="cf-act"' in adm and "actual_completion: actual" in adm and '<label for="cs-end">Anticipated completion</label>' in adm
    passed += 1; print("  ui ok")

    conn = db_layer.connect(pragma_fk=False)
    try:
        conn.execute("DELETE FROM projects WHERE project_code = ?", (CODE,))
        conn.commit()
        _purge(conn, email)   # sessions etc. first: Postgres enforces the users foreign keys
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS smoke_job_duration ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
