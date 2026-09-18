#!/usr/bin/env python3
"""#310 — the payroll week is Monday through Sunday. Behavioral guard.

Boots the REAL app (server.app) in-process on an ISOLATED database. Proves:

  * helpers: week_dates(monday) is 7 days Mon..Sun; last_completed_week resolves
    to the prior Mon..Sun from ANY day of the current week (Sunday included)
  * /api/payroll/hours?week_start=<monday>: 7 dates, 7 totals_by_day, week_end is
    the Sunday; Saturday and Sunday sign-ins count in the worker's weekly total,
    the day totals and the grand total; comp overlay (admin) prices weekend hours
  * the default week (no week_start) is last_completed_week's Mon..Sun
  * CSV export carries 7 date columns and the weekend hours
  * the sign-in/DCR divergence scan for a week_start covers Monday..Sunday
  * week_start that is not a Monday -> 400 (unchanged)

Run:  venv/bin/python tests/smoke_payroll_week_310.py
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_wk_310_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ["SSC_STRICT_ROUTES"] = "1"
for k in ("SSC_TRUSTED_PROXY", "GOOGLE_OAUTH_CLIENT_ID"):
    os.environ.pop(k, None)

import server  # noqa: E402
import auth  # noqa: E402
import db_layer  # noqa: E402
import payroll_hours as ph  # noqa: E402

TAG = "wk310"
PW = "PayrollWeekSmoke2026"
PROJ = "W310-A"
EMP, WID = "E-93100", "W-9310"
NOW = datetime.now().isoformat(timespec="seconds")
# a synthetic week far in the future so nothing real can collide (2030-03-04 is a Monday)
MON = date(2030, 3, 4)
DAYS = [MON + timedelta(days=i) for i in range(7)]
SAT, SUN = DAYS[5], DAYS[6]


def _db():
    return db_layer.connect(pragma_fk=True)


def _seed():
    conn = _db()
    try:
        for role in ("admin", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), role, f"Smoke {role}"))
        conn.execute("DELETE FROM projects WHERE project_code = ?", (PROJ,))
        conn.execute("INSERT INTO projects (project_code, name, address, status, created_at, updated_at) "
                     "VALUES (?, 'Smoke week', '1 Test St', 'active', ?, ?)", (PROJ, NOW, NOW))
        conn.execute("DELETE FROM sign_in_log WHERE employee_id = ?", (EMP,))
        conn.execute("DELETE FROM project_assignments WHERE employee_id = ?", (EMP,))
        conn.execute("DELETE FROM labor_worker_state WHERE employee_id = ?", (EMP,))
        conn.execute("DELETE FROM employees WHERE employee_id = ?", (EMP,))
        conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at) "
                     "VALUES (?, ?, 'Smoke Weekend310', 'mason', ?, ?)", (EMP, WID, NOW, NOW))
        conn.execute("INSERT INTO labor_worker_state (worker_id, employee_id, trade, current_rate, status, "
                     "effective_date, created_at, updated_at) VALUES (?, ?, 'mason', 10.0, 'active', ?, ?, ?)",
                     (WID, EMP, MON.isoformat(), NOW, NOW))
        conn.execute("INSERT INTO project_assignments (project_code, employee_id, role_on_project, start_date, status, "
                     "created_at, updated_at) VALUES (?, ?, 'mason', ?, 'active', ?, ?)", (PROJ, EMP, MON.isoformat(), NOW, NOW))
        # the per-day comp overlay reads worker_rates (placeholder $10.00, synthetic worker)
        conn.execute("DELETE FROM worker_rates WHERE employee_id = ?", (EMP,))
        conn.execute("INSERT INTO worker_rates (employee_id, hourly_rate, effective_from, effective_to) VALUES (?, 10.0, ?, NULL)",
                     (EMP, (MON - timedelta(days=30)).isoformat()))
        # Mon 8h, Wed 8h, Sat 8h, Sun 4.5h (07:00-12:00 minus lunch) = 28.5h
        for d, t_in, t_out in ((DAYS[0], "07:00", "15:30"), (DAYS[2], "07:00", "15:30"),
                               (SAT, "07:00", "15:30"), (SUN, "07:00", "12:00")):
            conn.execute("INSERT INTO sign_in_log (date, employee_id, project_code, time_in, time_out) VALUES (?, ?, ?, ?, ?)",
                         (d.isoformat(), EMP, PROJ, t_in, t_out))
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        conn.execute("DELETE FROM sign_in_log WHERE employee_id = ?", (EMP,))
        conn.execute("DELETE FROM worker_rates WHERE employee_id = ?", (EMP,))
        conn.execute("DELETE FROM project_assignments WHERE employee_id = ?", (EMP,))
        conn.execute("DELETE FROM labor_worker_state WHERE employee_id = ?", (EMP,))
        conn.execute("DELETE FROM employees WHERE employee_id = ?", (EMP,))
        conn.execute("DELETE FROM projects WHERE project_code = ?", (PROJ,))
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.execute("DELETE FROM login_audit WHERE ip LIKE '10.31.%'")
        conn.commit()
        purge_users(conn, ids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW},
               environ_base={"REMOTE_ADDR": "10.31.0.1"})
    assert r.status_code == 200, (role, r.status_code)
    return c


def main() -> int:
    passed = 0
    # ---- helpers
    assert ph.WEEK_DAYS == 7
    wd = ph.week_dates(MON)
    assert len(wd) == 7 and wd[0].weekday() == 0 and wd[-1].weekday() == 6 and wd[-1] == SUN
    for today, want in ((date(2026, 5, 20), (date(2026, 5, 11), date(2026, 5, 17))),   # Wednesday
                        (date(2026, 5, 22), (date(2026, 5, 11), date(2026, 5, 17))),   # Friday
                        (date(2026, 5, 24), (date(2026, 5, 11), date(2026, 5, 17))),   # Sunday — still this week
                        (date(2026, 5, 25), (date(2026, 5, 18), date(2026, 5, 24)))):  # Monday
        assert ph.last_completed_week(today) == want, (today, ph.last_completed_week(today))
    passed += 1; print("  helpers ok")

    _seed()
    admin = _login("admin")

    # ---- the grid
    r = admin.get(f"/api/payroll/hours?week_start={MON.isoformat()}")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    g = r.get_json()["data"]
    assert g["week_start"] == MON.isoformat() and g["week_end"] == SUN.isoformat()
    assert g["dates"] == [d.isoformat() for d in DAYS] and len(g["totals_by_day"]) == 7
    w = next(x for x in g["workers"] if x["employee_id"] == EMP)
    hours = {d["date"]: d["hours"] for d in w["days"]}
    assert hours[SAT.isoformat()] == 8.0 and hours[SUN.isoformat()] == 4.5
    assert w["weekly_total"] == 28.5 and g["totals_by_day"][5] == 8.0 and g["totals_by_day"][6] == 4.5
    assert g["grand_total"] == 28.5
    assert g["rates_visible"] is True and w["amount_owed"] == 285.0   # weekend hours priced at the rate
    assert admin.get("/api/payroll/hours?week_start=" + DAYS[1].isoformat()).status_code == 400
    pm = _login("pm")
    gp = pm.get(f"/api/payroll/hours?week_start={MON.isoformat()}").get_json()["data"]
    assert gp["rates_visible"] is False and "amount_owed" not in next(x for x in gp["workers"] if x["employee_id"] == EMP)
    passed += 1; print("  grid ok")

    # ---- default week = last completed Mon..Sun
    g0 = admin.get("/api/payroll/hours").get_json()["data"]
    m0, s0 = ph.last_completed_week(date.today())
    assert g0["week_start"] == m0.isoformat() and g0["week_end"] == s0.isoformat() and len(g0["dates"]) == 7
    passed += 1; print("  default week ok")

    # ---- CSV
    r = admin.get(f"/api/payroll/hours.csv?week_start={MON.isoformat()}")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    rows = list(csv.reader(io.StringIO(r.get_data(as_text=True))))
    head = rows[0]
    assert head[4:11] == [d.isoformat() for d in DAYS] and head[11] == "weekly_total"
    mine = next(x for x in rows if x[1] == EMP)
    assert mine[4 + 5] == "8.0" and mine[4 + 6] == "4.5" and mine[11] == "28.5"
    assert f"to-{SUN.isoformat()}" in r.headers.get("Content-Disposition", "")
    passed += 1; print("  csv ok")

    # ---- divergence scan covers the seven days
    r = admin.get(f"/api/projects/{PROJ}/signin-dcr-divergence?week_start={MON.isoformat()}")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    d = r.get_json()["data"]
    assert d["start_date"] == MON.isoformat() and d["end_date"] == SUN.isoformat()
    passed += 1; print("  divergence window ok")

    print(f"PASS smoke_payroll_week_310 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
