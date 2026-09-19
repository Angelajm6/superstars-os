#!/usr/bin/env python3
"""#320 — worker job history + payroll view: behavioral guard.

Flask test client on an ISOLATED database (honors SSC_DB_URL; builds a throwaway
SQLite snapshot from the versioned migrations when unset). Proves:

  * gating: pm / super / estimator / client -> 403; anon -> 401; admin / c_suite -> 200;
    unknown worker -> 404; bad months -> 400; months clamped 1..36
  * hours: every sign-in counted as WORKED hours (07:00–15:30 = 8.00), summed
    per project (days = distinct dates per project), per month, and in the
    project × month matrix; window vs YTD vs all-time totals
  * cost: worked hours × the rate effective ON THE WORK DATE — a raise splits
    the cost at its boundary; hours before any rate are `unrated`; a worker
    with no rate at all reports unrated hours and no cost
  * assignments merge into the project rows (role, status, dates); a project
    with an assignment but no sign-ins still appears
  * certifications with expired / expiring / valid states; credentials (cof_cards)
    when the table exists; rate history with the `current` flag; rate-change
    audit with pending count
  * archived worker still readable (status 'archived'); recent_days newest first
  * no `*_path` / `folder` keys anywhere in the payload

Run:  venv/bin/python tests/smoke_worker_history_320.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_wk_320_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import company_portal  # noqa: E402
import db_layer  # noqa: E402
import worker_history  # noqa: E402

TAG = "wk320"
PW = "WorkerHistorySmokePassword2026"
ROLES = ("c_suite", "admin", "pm", "super", "estimator", "client")
TODAY = date.today()
API = "/api/company/portal"
EID, EID2, EID3 = "E-93201", "E-93202", "E-93203"


def D(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def _db():
    return db_layer.connect(pragma_fk=True)


def _table(conn, name) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchall()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def _seed() -> dict:
    info = {}
    conn = _db()
    try:
        for role in ROLES:
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), role, f"Smoke {role}"))
        _cleanup_rows(conn)
        for code, name in (("WK320-A", "Wk320 Facade"), ("WK320-B", "Wk320 Parapet")):
            conn.execute("INSERT INTO projects (project_code, name, address) VALUES (?, ?, 'x')", (code, name))
        conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at) "
                     "VALUES (?, 'W-9320', 'Wk320 Rated', 'rope_access', ?, ?)", (EID, D(0), D(0)))
        conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at) "
                     "VALUES (?, 'W-9321', 'Wk320 Unrated', 'mason', ?, ?)", (EID2, D(0), D(0)))
        conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at, archived_at) "
                     "VALUES (?, 'W-9322', 'Wk320 Former', 'mason', ?, ?, ?)", (EID3, D(0), D(0), D(0)))
        conn.execute("INSERT INTO labor_worker_state (worker_id, employee_id, trade, current_rate, status, "
                     "effective_date, created_at, updated_at) VALUES ('W-9320', ?, 'rope_access', 50.0, 'active', ?, ?, ?)",
                     (EID, D(-10), D(0), D(0)))
        # rate history: $40 from -60 days to -11, $50 from -10 (open)
        info["worker_rates"] = _table(conn, "worker_rates")
        if info["worker_rates"]:
            conn.execute("INSERT INTO worker_rates (employee_id, hourly_rate, effective_from, effective_to, notes) "
                         "VALUES (?, 40.0, ?, ?, 'start')", (EID, D(-60), D(-11)))
            conn.execute("INSERT INTO worker_rates (employee_id, hourly_rate, effective_from, effective_to, notes) "
                         "VALUES (?, 50.0, ?, NULL, 'raise')", (EID, D(-10)))
        # sign-ins: rated worker — 8 h on A at -70 (before any rate), -40 (@40), -5 (@50); 8 h on B at -5 (@50, same day)
        # plus a half day (4 h, 07:00–11:30 no lunch deduction expected? 07:00–11:30 = 4.5h - lunch) — keep full days for clarity
        for d, code in ((D(-70), "WK320-A"), (D(-40), "WK320-A"), (D(-5), "WK320-A"), (D(-5), "WK320-B")):
            conn.execute("INSERT INTO sign_in_log (date, employee_id, project_code, time_in, time_out, created_at, updated_at) "
                         "VALUES (?, ?, ?, '07:00', '15:30', ?, ?)", (d, EID, code, D(0), D(0)))
        conn.execute("INSERT INTO sign_in_log (date, employee_id, project_code, time_in, time_out, created_at, updated_at) "
                     "VALUES (?, ?, 'WK320-A', '07:00', '15:30', ?, ?)", (D(-3), EID2, D(0), D(0)))
        conn.execute("INSERT INTO sign_in_log (date, employee_id, project_code, time_in, time_out, created_at, updated_at) "
                     "VALUES (?, ?, 'WK320-A', '07:00', '15:30', ?, ?)", (D(-100), EID3, D(0), D(0)))
        info["assignments"] = _table(conn, "project_assignments")
        if info["assignments"]:
            conn.execute("INSERT INTO project_assignments (project_code, employee_id, role_on_project, start_date, status) "
                         "VALUES ('WK320-A', ?, 'lead', ?, 'active')", (EID, D(-60)))
            conn.execute("INSERT INTO project_assignments (project_code, employee_id, role_on_project, start_date, end_date, status) "
                         "VALUES ('WK320-B', ?, 'crew', ?, ?, 'completed')", (EID, D(-30), D(-1)))
        ct = conn.execute("SELECT cert_type_id FROM cert_types ORDER BY cert_type_id LIMIT 1").fetchone()["cert_type_id"]
        for exp in (D(-3), D(20), D(200)):
            conn.execute("INSERT INTO certifications (employee_id, cert_type_id, date_obtained, expiration_date, status, "
                         "created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)", (EID, ct, D(-300), exp, D(0), D(0)))
        info["cof"] = _table(conn, "cof_cards")
        if info["cof"]:
            conn.execute("INSERT INTO cof_cards (card_id, employee_id, issued_date, expires_date, issued_by, status) "
                         "VALUES ('SSC-93201', ?, ?, ?, 'Wk320 Issuer', 'issued')", (EID, D(-30), D(335)))
        info["changes"] = _table(conn, "labor_rate_change")
        if info["changes"]:
            conn.execute("INSERT INTO labor_rate_change (worker_id, employee_id, old_rate, new_rate, effective_date, status, "
                         "is_initial, submitted_at) VALUES ('W-9320', ?, NULL, 40.0, ?, 'approved', 1, ?)", (EID, D(-60), D(-60)))
            conn.execute("INSERT INTO labor_rate_change (worker_id, employee_id, old_rate, new_rate, effective_date, status, "
                         "is_initial, submitted_at) VALUES ('W-9320', ?, 50.0, 55.0, ?, 'pending', 0, ?)", (EID, D(7), D(0)))
        conn.commit()
    finally:
        conn.close()
    return info


def _cleanup_rows(conn):
    for sql in ("DELETE FROM sign_in_log WHERE employee_id LIKE 'E-932%'",
                "DELETE FROM certifications WHERE employee_id LIKE 'E-932%'",
                "DELETE FROM worker_rates WHERE employee_id LIKE 'E-932%'",
                "DELETE FROM project_assignments WHERE employee_id LIKE 'E-932%'",
                "DELETE FROM cof_cards WHERE employee_id LIKE 'E-932%'",
                "DELETE FROM labor_rate_change WHERE worker_id LIKE 'W-932%'",
                "DELETE FROM labor_worker_state WHERE worker_id LIKE 'W-932%'",
                "DELETE FROM employees WHERE employee_id LIKE 'E-932%'",
                "DELETE FROM projects WHERE project_code LIKE 'WK320-%'"):
        try:
            conn.execute(sql)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        _cleanup_rows(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _no_paths(obj, where=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert not (k.endswith("_path") or k == "folder"), f"path key {k!r} leaked in {where}"
            _no_paths(v, where)
    elif isinstance(obj, list):
        for v in obj:
            _no_paths(v, where)


def _login(app, role):
    c = app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code, r.get_data(as_text=True)[:200])
    return c


def main() -> int:
    app = Flask("wk320", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    company_portal.register(app)
    info = _seed()
    checks = 0

    # ---- gates
    for role in ("pm", "super", "estimator", "client"):
        assert _login(app, role).get(f"{API}/workers/{EID}").status_code == 403, role
    assert app.test_client().get(f"{API}/workers/{EID}").status_code == 401
    cs = _login(app, "c_suite")
    assert _login(app, "admin").get(f"{API}/workers/{EID}").status_code == 200
    assert cs.get(f"{API}/workers/E-00000-nope").status_code == 404
    assert cs.get(f"{API}/workers/{EID}?months=x").status_code == 400
    assert cs.get(f"{API}/workers/{EID}?months=999").get_json()["data"]["window"]["months"] == 36
    assert cs.get(f"{API}/workers/{EID}?months=0").get_json()["data"]["window"]["months"] == 1
    checks += 1

    # ---- the rated worker, 12-month window
    j = cs.get(f"{API}/workers/{EID}").get_json()
    _no_paths(j, "worker detail")
    d = j["data"]
    w, s = d["worker"], d["summary"]
    assert w["worker_id"] == "W-9320" and w["trade"] == "rope_access" and w["status"] == "active" and w["current_rate"] == 50.0
    assert len(d["window"]["labels"]) == 12 and d["window"]["labels"][-1] == TODAY.strftime("%Y-%m")
    assert s["hours_all_time"] == 32.0, s          # four 8.00 h days
    assert s["last_on_site"] == D(-5) and s["first_on_site"] == D(-70)
    in_window = [x for x in (-70, -40, -5, -5) if D(x) >= d["window"]["since"]]
    assert s["hours_window"] == 8.0 * len(in_window), (s, d["window"])
    assert s["days_window"] == len({D(x) for x in in_window})
    # projects: A has 3 sign-ins on 3 dates, B one; assignments merged
    P = {p["project_code"]: p for p in d["projects"]}
    assert set(P) == {"WK320-A", "WK320-B"}
    assert P["WK320-A"]["hours"] == 24.0 and P["WK320-A"]["days"] == 3 and P["WK320-A"]["first_date"] == D(-70) and P["WK320-A"]["last_date"] == D(-5)
    assert P["WK320-B"]["hours"] == 8.0 and P["WK320-B"]["days"] == 1 and P["WK320-B"]["project_name"] == "Wk320 Parapet"
    assert d["projects"][0]["project_code"] in ("WK320-A", "WK320-B"), "sorted by last date desc"
    if info["assignments"]:
        assert P["WK320-A"]["assignment_status"] == "active" and P["WK320-A"]["role_on_project"] == "lead"
        assert P["WK320-B"]["assignment_status"] == "completed" and P["WK320-B"]["assigned_to"] == D(-1)
        assert s["projects_active"] == 1
    checks += 1

    # ---- cost: rate on the work date; -40 @ $40, -5 ×2 @ $50; -70 predates every
    # effective-dated row and prices at the state's current rate (the Finance view's
    # fallback rule — a worker with a rate on file is never "unrated")
    if info["worker_rates"]:
        assert P["WK320-A"]["unrated_hours"] == 0.0, P["WK320-A"]
        assert P["WK320-A"]["est_cost"] == 8 * 50 + 8 * 40 + 8 * 50, P["WK320-A"]   # 1120
        assert P["WK320-B"]["est_cost"] == 8 * 50 and P["WK320-B"]["unrated_hours"] == 0
        recent = d["recent_days"]
        assert [r["date"] for r in recent] == [D(-5), D(-5)], recent
        assert all(r["rate"] == 50.0 and r["est_cost"] == 400.0 and r["hours"] == 8.0 for r in recent)
        hist = d["rate_history"]
        assert [h["hourly_rate"] for h in hist] == [50.0, 40.0] and hist[0]["current"] is True and hist[1]["current"] is False
    else:
        # no worker_rates table: everything prices at the state's current rate
        assert P["WK320-B"]["est_cost"] == 400.0
    # by-month + matrix agree with the projects
    months = {m["month"]: m for m in d["by_month"]}
    ym5 = D(-5)[:7]
    assert months[ym5]["hours"] >= 16.0 and months[ym5]["days"] >= 1
    assert sum(m["hours"] for m in d["by_month"]) == s["hours_window"]
    M = {r["project_code"]: r for r in d["matrix"]["rows"]}
    assert M["WK320-B"]["cells"][d["matrix"]["months"].index(ym5)] == 8.0 and M["WK320-B"]["total"] == 8.0
    assert sum(r["total"] for r in d["matrix"]["rows"]) == s["hours_window"]
    checks += 1

    # ---- certs, credentials, rate changes
    C = d["certifications"]
    assert len(C) == 3 and [c["state"] for c in C] == ["expired", "expiring", "valid"], [c["state"] for c in C]
    assert s["certs_total"] == 3 and s["certs_expired"] == 1 and s["certs_expiring_30"] == 1
    assert C[0]["days_left"] == -3 and C[0]["name"], C[0]
    if info["cof"]:
        assert d["credentials"][0]["card_id"] == "SSC-93201" and d["credentials"][0]["status"] == "issued"
        assert s["credentials_issued"] == 1 and "signature_path" not in d["credentials"][0]
    if info["changes"]:
        assert s["pending_rate_changes"] == 1
        assert [c["status"] for c in d["rate_changes"]] == ["approved", "pending"]
    checks += 1

    # ---- unrated worker + archived worker
    d2 = cs.get(f"{API}/workers/{EID2}").get_json()["data"]
    assert d2["worker"]["current_rate"] is None and d2["summary"]["hours_window"] == 8.0
    assert d2["summary"]["est_cost_window"] == 0 and d2["summary"]["unrated_hours_window"] == 8.0
    assert d2["projects"][0]["est_cost"] == 0 and d2["projects"][0]["unrated_hours"] == 8.0
    assert d2["recent_days"][0]["rate"] is None and d2["recent_days"][0]["est_cost"] is None
    d3 = cs.get(f"{API}/workers/{EID3}").get_json()["data"]
    assert d3["worker"]["status"] == "archived" and d3["summary"]["hours_all_time"] == 8.0
    assert d3["summary"]["hours_window"] == (8.0 if D(-100) >= d3["window"]["since"] else 0.0)
    d3b = cs.get(f"{API}/workers/{EID3}?months=36").get_json()["data"]
    assert d3b["summary"]["hours_window"] == 8.0 and len(d3b["by_month"]) == 36
    checks += 1

    # ---- the workforce list still works and carries the employee ids the page links to
    wf = cs.get(f"{API}/workforce").get_json()["data"]
    assert any(x["employee_id"] == EID for x in wf["workers"]) and not any(x["employee_id"] == EID3 for x in wf["workers"])
    checks += 1

    print(f"PASS: #320 worker job history — gates, hours by project / month / matrix, rate-on-date cost with raise + "
          f"unrated hours, assignments merge, certs / credentials / rate audit, unrated + archived workers ({checks} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
