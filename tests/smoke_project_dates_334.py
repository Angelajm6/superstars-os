#!/usr/bin/env python3
"""#334 — projects.start_date / target_completion ensured on both backends; the calendar
never 500s on odd date values. Behavioral guard.

Proves:
  * `ensure` adds both columns to a `projects` table that lacks them and is a no-op on
    one that has them (a scratch SQLite database; the migration script runs on the
    fixture)
  * the real app (`import server`) boots with the ensure wired in, and the calendar
    survives a project whose start_date is '' and one whose start_date is not a date:
    both are reported as undated instead of breaking the page
Run:  venv/bin/python tests/smoke_project_dates_334.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_pd_334_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import apply_project_dates_334 as mig  # noqa: E402
import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app — the ensure runs in project_onboard.register)

TAG = "pd334"
PW = "ProjectDatesSmoke2026"
TODAY = date.today()


def D(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


class _Scratch:
    """A bare sqlite connection with the db_layer row shape, for the scratch-table check."""
    def __init__(self, path):
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        return self.c.execute(sql, params)

    def commit(self):
        self.c.commit()


def main() -> int:
    passed = 0

    # ---- a projects table without the columns gains them once (scratch SQLite; the
    #      column probe follows the CONFIGURED backend, so this part is SQLite-gate only)
    if not db_layer.is_postgres():
        scratch = str(Path(_TMP) / "bare.db")
        sc = _Scratch(scratch)
        sc.execute("CREATE TABLE projects (project_code TEXT PRIMARY KEY, name TEXT)")
        sc.commit()
        r1 = mig.ensure(sc)
        assert r1["added"] == ["start_date", "target_completion"], r1
        r2 = mig.ensure(sc)
        assert r2["added"] == [], r2
        cols = {r["name"] for r in sc.execute("PRAGMA table_info(projects)").fetchall()}
        assert {"start_date", "target_completion"} <= cols
    # on either backend: the fixture has them -> idempotent no-op, and the script exits 0
    conn = db_layer.connect(pragma_fk=False)
    try:
        assert mig.ensure(conn)["added"] == []
    finally:
        conn.close()
    assert mig.main() == 0
    passed += 1; print("  ensure ok")

    # ---- the live app: odd values never break the calendar
    conn = db_layer.connect(pragma_fk=False)
    try:
        email = f"{TAG}.c_suite@example.com"
        conn.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                     "VALUES (?, ?, 'c_suite', 'Smoke c_suite Pd334', 1, 'active', 0, 1)", (email, auth.hash_password(PW)))
        for code in ("PD334-BLANK", "PD334-ODD", "PD334-OK"):
            conn.execute("DELETE FROM projects WHERE project_code = ?", (code,))
        conn.execute("INSERT INTO projects (project_code, name, status, start_date) VALUES ('PD334-BLANK', 'Pd334 Blank', 'active', '')")
        conn.execute("INSERT INTO projects (project_code, name, status, start_date, target_completion) VALUES ('PD334-ODD', 'Pd334 Odd', 'active', 'TBD', 'soon')")
        conn.execute("INSERT INTO projects (project_code, name, status, start_date, target_completion) VALUES ('PD334-OK', 'Pd334 Ok', 'active', ?, ?)", (D(-1), D(3)))
        conn.commit()
    finally:
        conn.close()
    c = server.app.test_client()
    assert c.post("/api/auth/login", json={"email": email, "password": PW}).status_code == 200
    r = c.get(f"/api/company/portal/calendar?from={D(-7)}&to={D(7)}")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    d = r.get_json()["data"]
    codes = {j["project_code"] for j in d["jobs"]}
    assert "PD334-OK" in codes and "PD334-BLANK" not in codes and "PD334-ODD" not in codes
    und = {u["project_code"] for u in d["undated"]}
    assert {"PD334-BLANK", "PD334-ODD"} <= und, und
    # the Projects tab reads the same columns
    assert c.get("/api/company/portal/projects?status=all&search=pd334").status_code == 200
    passed += 1; print("  odd values ok")

    print(f"PASS smoke_project_dates_334 ({passed} groups)")
    return 0


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        conn.execute("DELETE FROM projects WHERE project_code LIKE 'PD334-%'")
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
