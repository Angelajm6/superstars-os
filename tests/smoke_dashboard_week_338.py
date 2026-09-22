#!/usr/bin/env python3
"""#338 — "This week" strip on the company Dashboard (moved from the console by #339): behavioral guard.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * the served console carries the strip (section, weekday header, row, footer, the
    calendar link) with EN and ES strings, and reads the same calendar API the portal uses
  * the calendar API answers a Monday–Sunday window for a c_suite user with the fields
    the strip renders (jobs with colour / open_ended, days with headcount + milestones,
    undated)
  * the strip is NOT served to a pm (the console is company-only) — the page itself 403s
Run:  venv/bin/python tests/smoke_dashboard_week_338.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_dw_338_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "dw338"
PW = "DashboardWeekSmoke2026"
TODAY = date.today()


def D(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed():
    conn = _db()
    try:
        for role in ("c_suite", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Smoke {role} Dw338"))
        conn.execute("DELETE FROM projects WHERE project_code = 'DW338-A'")
        conn.execute("INSERT INTO projects (project_code, name, address, status, start_date, target_completion) VALUES ('DW338-A', 'Dw338 Facade', '1 Dw338 St', 'active', ?, ?)", (D(-2), D(9)))
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        conn.execute("DELETE FROM projects WHERE project_code = 'DW338-A'")
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code)
    return c


def main() -> int:
    _seed()
    passed = 0

    cs = _login("c_suite")
    html = cs.get("/company", follow_redirects=True).get_data(as_text=True)
    for needle in ('id="cc-week"', 'id="cc-week-dow"', 'id="cc-week-row"', 'id="cc-week-foot"', 'id="cc-week-open"',
                   "async function loadWeekStrip", "'/calendar?from=' + from + '&to=' + to", "href=\"/projects/", "'#calendar/' + from"):
        assert needle in html, needle
    # the strip opens the Dashboard, above the KPI tiles
    assert html.index('id="cc-week"') < html.index('id="ov-kpis"')
    assert 'id="cc-week"' not in cs.get("/", follow_redirects=True).get_data(as_text=True), "the console dashboard retired (#339)"
    passed += 1; print("  strip served ok")

    mon = TODAY - timedelta(days=TODAY.weekday())
    r = cs.get(f"/api/company/portal/calendar?from={mon.isoformat()}&to={(mon + timedelta(days=6)).isoformat()}")
    assert r.status_code == 200
    d = r.get_json()["data"]
    j = next(x for x in d["jobs"] if x["project_code"] == "DW338-A")
    assert 0 <= j["color"] < 8 and j["end_date"] == D(9) and not j["open_ended"] and "name" in j
    assert isinstance(d["days"], dict) and isinstance(d["undated"], list) and d["range"]["from"] == mon.isoformat()
    passed += 1; print("  week window ok")

    pm = _login("pm")
    for path in ("/", "/company"):
        r = pm.get(path, follow_redirects=False)
        assert r.status_code in (302, 403), (path, r.status_code)
    passed += 1; print("  company-only ok")

    print(f"PASS smoke_dashboard_week_338 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
