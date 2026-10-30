#!/usr/bin/env python3
"""Payroll hours rules (2026-10-30, Angela). Behavioral guard.

Boots the REAL app (server.app) in-process on an ISOLATED database. Proves:

  * trade: Weekly Payroll shows the Pay Rates trade, and a trade edit on Pay
    Rates shows there at once
  * history: a worker with hours who is then deactivated, archived and taken
    off the crew still appears on that week (labor_active false, hours intact);
    an inactive worker with no hours that week does not
  * one place at a time: overlapping times on a second job -> 409 overlap;
    non-overlapping is allowed
  * over 8 hours: no reason -> 422 needs_reason; with a reason -> saved, payroll
    pays 8 and flags the rest; a PM approves -> paid in full; an estimator is
    refused; raising the hours after approval needs a new reason and goes back
    to pending; reject -> 8; dropping back to 8 clears the request
  * weeks summary for the week picker counts paid hours and pending days
  * the CSV carries paid hours plus an overtime_pending column
  * worker app: clocking out past 8 hours needs a reason (422), then goes
    through as pending; a finished entry covering now blocks a clock-in
  * worker-app ISO timestamps count real hours (they used to count 0)
  * days before the rule started are paid as logged

Run:  venv/bin/python tests/smoke_payroll_rules.py
"""
from __future__ import annotations

import csv
import io
import os
import secrets
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_payrules_")

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

TAG = "payrules"
PW = "PayrollRulesSmoke2026"
P1, P2 = "PR-RULE-A", "PR-RULE-B"
A, AW = "E-93300", "W-9330"     # the main worker
B, BW = "E-93301", "W-9331"     # worked, then deactivated + off-rolled
C, CW = "E-93302", "W-9332"     # inactive, no hours
OLD, OLDW = "E-93303", "W-9333"  # a day before the rule started
EMPS = (A, B, C, OLD)
NOW = datetime.now().isoformat(timespec="seconds")
MON = date(2030, 3, 11)          # a synthetic Monday far from real data
TUE, WED = (MON + timedelta(days=1)).isoformat(), (MON + timedelta(days=2)).isoformat()
OLD_MON = date(2026, 10, 19)     # the week before RULE_START


def _db():
    return db_layer.connect(pragma_fk=True)


def _worker(conn, eid, wid, trade_emp, trade_rates, status="active"):
    conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (eid, wid, f"Smoke {wid}", trade_emp, NOW, NOW))
    conn.execute("INSERT INTO labor_worker_state (worker_id, employee_id, trade, current_rate, status, "
                 "effective_date, created_at, updated_at) VALUES (?, ?, ?, 10.0, ?, ?, ?, ?)",
                 (wid, eid, trade_rates, status, "2026-01-01", NOW, NOW))
    conn.execute("INSERT INTO worker_rates (employee_id, hourly_rate, effective_from, effective_to) "
                 "VALUES (?, 10.0, '2026-01-01', NULL)", (eid,))


def _seed():
    _purge()
    conn = _db()
    try:
        for role in ("admin", "pm", "estimator"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), role, f"Smoke {role}"))
        for code in (P1, P2):
            conn.execute("INSERT INTO projects (project_code, name, address, status, created_at, updated_at) "
                         "VALUES (?, 'Smoke rules', '1 Test St', 'active', ?, ?)", (code, NOW, NOW))
        _worker(conn, A, AW, "mason", "Laborer")
        _worker(conn, B, BW, "mason", "Mechanic")
        _worker(conn, C, CW, "mason", "Mechanic", status="inactive")
        _worker(conn, OLD, OLDW, "mason", "Mechanic")
        for eid in (A, B, OLD):
            conn.execute("INSERT INTO project_assignments (project_code, employee_id, start_date, status, "
                         "created_at, updated_at) VALUES (?, ?, '2026-01-01', 'active', ?, ?)", (P1, eid, NOW, NOW))
        conn.execute("INSERT INTO sign_in_log (date, employee_id, project_code, time_in, time_out) "
                     "VALUES (?, ?, ?, '07:00', '15:30')", (MON.isoformat(), B, P1))
        conn.execute("INSERT INTO sign_in_log (date, employee_id, project_code, time_in, time_out) "
                     "VALUES (?, ?, ?, '07:00', '17:30')", (OLD_MON.isoformat(), OLD, P1))
        conn.commit()
    finally:
        conn.close()


def _purge():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ph_in = ",".join("?" * len(EMPS))
        for t in ("sign_in_log", "sign_in_edit", "hours_overtime", "worker_rates", "project_assignments",
                  "labor_worker_state", "worker_app_session"):
            conn.execute(f"DELETE FROM {t} WHERE employee_id IN ({ph_in})", EMPS)
        conn.execute(f"DELETE FROM audit_log WHERE target_id IN ({ph_in})", EMPS)
        conn.execute(f"DELETE FROM employees WHERE employee_id IN ({ph_in})", EMPS)
        conn.execute("DELETE FROM projects WHERE project_code IN (?, ?)", (P1, P2))
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.execute("DELETE FROM login_audit WHERE ip LIKE '10.33.%'")
        conn.commit()
        purge_users(conn, ids)
        conn.commit()
    finally:
        conn.close()


def _login(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW},
               environ_base={"REMOTE_ADDR": "10.33.0.1"})
    assert r.status_code == 200, (role, r.status_code)
    return c


def _grid(c, monday=MON):
    r = c.get(f"/api/payroll/hours?week_start={monday.isoformat()}")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    return r.get_json()["data"]


def _row(g, eid):
    return next((w for w in g["workers"] if w["employee_id"] == eid), None)


def _day(g, eid, d):
    return next(x for x in _row(g, eid)["days"] if x["date"] == d)


def main() -> int:
    passed = 0
    # ---- pure helpers
    assert ph.compute_worked_hours("2030-03-11T07:00:12", "2030-03-11T15:30:40") == 8.0
    assert ph.to_hhmm("2030-03-11T07:05:00") == "07:05" and ph.to_hhmm("bad") is None
    assert ph.find_overlap([{"time_in": "07:00", "time_out": "15:30", "project_code": P1}], "15:30", "17:00") is None
    assert ph.find_overlap([{"time_in": "07:00", "time_out": None, "project_code": P1}], "16:00", "17:00")
    assert ph.payable_hours(10, None) == 8 and ph.payable_hours(10, {"status": "approved", "approved_hours": 10}) == 10
    assert ph.payable_hours(11, {"status": "approved", "approved_hours": 10}) == 10
    passed += 1; print("  helpers ok")

    _seed()
    admin, pm, est = _login("admin"), _login("pm"), _login("estimator")

    # ---- trade follows Pay Rates
    g = _grid(admin)
    assert _row(g, A)["trade"] == "Laborer", _row(g, A)["trade"]
    r = admin.post(f"/api/labor-rates/state/{AW}/trade", json={"trade": "Rope Access"})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert _row(_grid(admin), A)["trade"] == "Rope Access"
    passed += 1; print("  trade ok")

    # ---- history survives deactivation + off-roll
    conn = _db()
    try:
        conn.execute("UPDATE labor_worker_state SET status = 'inactive' WHERE worker_id = ?", (BW,))
        conn.execute("UPDATE employees SET archived_at = CURRENT_TIMESTAMP WHERE employee_id = ?", (B,))
        conn.execute("UPDATE project_assignments SET status = 'off-rolled' WHERE employee_id = ?", (B,))
        conn.commit()
    finally:
        conn.close()
    g = _grid(admin)
    rb = _row(g, B)
    assert rb and rb["weekly_total"] == 8.0 and rb["labor_active"] is False and rb.get("amount_owed") == 80.0, rb
    assert _row(g, C) is None
    passed += 1; print("  history ok")

    # ---- one place at a time
    r = admin.post("/api/sign-ins", json={"employee_id": A, "project_code": P1, "date": TUE,
                                          "time_in": "07:00", "time_out": "15:30"})
    assert r.status_code == 201, r.get_data(as_text=True)[:200]
    r = admin.post("/api/sign-ins", json={"employee_id": A, "project_code": P2, "date": TUE,
                                          "time_in": "08:00", "time_out": "12:00"})
    assert r.status_code == 409 and r.get_json()["code"] == "overlap", r.get_data(as_text=True)[:200]
    assert P1 in r.get_json()["error"]
    passed += 1; print("  overlap ok")

    # ---- over 8 hours
    body = {"employee_id": A, "project_code": P2, "date": TUE, "time_in": "16:00", "time_out": "18:00"}
    r = admin.post("/api/sign-ins", json=body)
    assert r.status_code == 422 and r.get_json()["needs_reason"] is True and r.get_json()["day_total"] == 9.5
    r = admin.post("/api/sign-ins", json={**body, "overtime_reason": "Finishing the drop before rain"})
    assert r.status_code == 201, r.get_data(as_text=True)[:200]
    p2_id = r.get_json()["data"]["id"]
    g = _grid(admin)
    d = _day(g, A, TUE)
    assert d["hours"] == 8.0 and d["hours_logged"] == 9.5 and d["overtime"]["status"] == "pending", d
    assert _row(g, A)["overtime_pending_hours"] == 1.5 and g["overtime_pending_count"] == 1
    assert _row(g, A)["amount_owed"] == 80.0

    r = est.post("/api/payroll/overtime/decide", json={"employee_id": A, "date": TUE, "decision": "approve"})
    assert r.status_code == 403, r.status_code
    r = pm.post("/api/payroll/overtime/decide", json={"employee_id": A, "date": TUE, "decision": "approve"})
    assert r.status_code == 200 and r.get_json()["data"]["hours"] == 9.5, r.get_data(as_text=True)[:200]
    d = _day(_grid(admin), A, TUE)
    assert d["hours"] == 9.5 and d["overtime"]["status"] == "approved"

    r = admin.put(f"/api/sign-ins/{p2_id}", json={"time_in": "16:00", "time_out": "19:00"})
    assert r.status_code == 422, r.status_code
    r = admin.put(f"/api/sign-ins/{p2_id}", json={"time_in": "16:00", "time_out": "19:00",
                                                  "overtime_reason": "Inspector stayed late"})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    d = _day(_grid(admin), A, TUE)
    assert d["hours"] == 8.0 and d["hours_logged"] == 10.5 and d["overtime"]["status"] == "pending", d

    r = admin.post("/api/payroll/overtime/decide", json={"employee_id": A, "date": TUE, "decision": "reject"})
    assert r.status_code == 200
    d = _day(_grid(admin), A, TUE)
    assert d["hours"] == 8.0 and d["overtime"]["status"] == "rejected"
    passed += 1; print("  over 8 ok")

    # ---- week picker summary + CSV
    r = admin.get(f"/api/payroll/weeks?from={MON.isoformat()}&to={(MON + timedelta(days=6)).isoformat()}")
    assert r.status_code == 200
    wk = r.get_json()["data"][MON.isoformat()]
    assert wk["hours"] == 16.0 and wk["pending"] == 0, wk     # B 8 + A 8 (extra rejected)
    assert est.get(f"/api/payroll/weeks?from={MON.isoformat()}&to={MON.isoformat()}").status_code == 403
    # a pending day shows in the summary
    r = admin.post("/api/sign-ins", json={"employee_id": A, "project_code": P1, "date": WED,
                                          "time_in": "06:00", "time_out": "16:30", "overtime_reason": "Pour"})
    assert r.status_code == 201
    wk = admin.get(f"/api/payroll/weeks?from={MON.isoformat()}&to={WED}").get_json()["data"][MON.isoformat()]
    assert wk["pending"] == 1 and wk["hours"] == 24.0, wk
    rows = list(csv.reader(io.StringIO(admin.get(f"/api/payroll/hours.csv?week_start={MON.isoformat()}")
                                        .get_data(as_text=True))))
    assert rows[0][-1] == "overtime_pending"
    arow = next(x for x in rows if x and x[1] == A)
    assert arow[-2] == "16.0" and arow[-1] == "2.0", arow
    passed += 1; print("  weeks + csv ok")

    # ---- dropping back to 8 clears a pending request
    r = admin.put(f"/api/sign-ins/{p2_id}", json={"time_in": "16:00", "time_out": "16:30"})
    assert r.status_code == 200
    conn = _db()
    try:
        assert ph.get_overtime(conn, A, TUE) is None
    finally:
        conn.close()
    passed += 1; print("  clear ok")

    # ---- worker app: clock-out past 8 needs a reason; clock-in can't overlap
    today = date.today().isoformat()
    token = secrets.token_urlsafe(32)
    conn = _db()
    try:
        cur = conn.execute("INSERT INTO sign_in_log (date, employee_id, project_code, time_in, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (today, A, P1, f"{today}T06:00:00", NOW, NOW))
        sid = cur.lastrowid
        conn.execute("INSERT INTO worker_app_session (token_hash, employee_id, project_code, issued_at, "
                     "expires_at, started_sign_in_id) VALUES (?, ?, ?, ?, ?, ?)",
                     (server._worker_token_hash(token), A, P1, NOW, f"{today}T23:59:59", sid))
        conn.commit()
    finally:
        conn.close()

    real_dt = server.datetime

    class _Frozen(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt.fromisoformat(f"{today}T17:30:00")
    server.datetime = _Frozen
    try:
        wc = server.app.test_client()
        r = wc.post("/api/worker/session/end", json={"employee_id": A, "session_token": token})
        assert r.status_code == 422 and r.get_json()["code"] == "needs_reason", r.get_data(as_text=True)[:200]
        r = wc.post("/api/worker/session/end", json={"employee_id": A, "session_token": token,
                                                      "reason": "Job ran late"})
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
    finally:
        server.datetime = real_dt
    conn = _db()
    try:
        ot = ph.get_overtime(conn, A, today)
        assert ot and ot["status"] == "pending" and ot["requested_by_role"] == "worker" and ot["logged_hours"] == 11.0, ot
        # a finished entry covering the rest of today blocks a second clock-in
        conn.execute("UPDATE sign_in_log SET time_in = '00:00', time_out = '23:59' WHERE id = ?", (sid,))
        token2 = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO worker_app_session (token_hash, employee_id, project_code, issued_at, expires_at) "
                     "VALUES (?, ?, '', ?, ?)", (server._worker_token_hash(token2), A, NOW, f"{today}T23:59:59"))
        conn.commit()
    finally:
        conn.close()
    r = server.app.test_client().post("/api/worker/session/start",
                                      json={"employee_id": A, "project_code": P1, "session_token": token2})
    assert r.status_code == 409 and r.get_json()["code"] == "overlap", (r.status_code, r.get_data(as_text=True)[:200])
    passed += 1; print("  worker app ok")

    # ---- before the rule started: paid as logged
    d = _day(_grid(admin, OLD_MON), OLD, OLD_MON.isoformat())
    assert d["hours"] == 10.0 and "overtime" not in d, d
    passed += 1; print("  rule start ok")

    print(f"\nsmoke_payroll_rules: {passed} checks passed")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        _purge()
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(rc)
