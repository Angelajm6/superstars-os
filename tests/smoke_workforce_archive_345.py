#!/usr/bin/env python3
"""#345 — Workforce: Active / Inactive, archive, reactivate, delete permanently. Behavioral guard.

Boots the real app (`import server`) on an ISOLATED database and a temp data root. Proves:
  * the workforce list splits Active / Inactive (archived OR labor-inactive), with counts;
    the headcount KPI and the Overview tile count ACTIVE workers only (roster rule #246)
  * archive (company roles) records the reason, is audited, refuses a second time;
    reactivate clears the archive AND an inactive labor state, refuses when already active
  * delete permanently (admin only) is BLOCKED by history (sign-ins, …) with the reasons;
    a history-free worker goes completely: employees row, labor state + rate changes
    (keyed by W-####), assignments, their records folder — audited with IDs/counts only
  * the old DELETE /api/employees route is company-only now and uses the same rules
  * pm → 403 everywhere; c_suite → 403 on delete; no names in the removal preview
Run:  venv/bin/python tests/smoke_workforce_archive_345.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_wf_345_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "wf345"
PW = "WorkforceArchive2026"
TODAY = date.today().isoformat()
# E-id, W-id, trade — synthetic, high numbers so they never meet real workers
A = ("E-93451", "W-9451", "mason")         # active, has sign-ins (history)
B = ("E-93452", "W-9452", "laborer")       # active, NO history -> deletable
C = ("E-93453", "W-9453", "rope_access")   # labor-inactive (PM-approved deactivation), history
D = ("E-93454", "W-9454", "mason")         # old route: has history -> archived
E = ("E-93455", "W-9455", "laborer")       # old route: no history -> hard delete
ALL = (A, B, C, D, E)
API = "/api/company/portal"


def _db():
    return db_layer.connect(pragma_fk=False)


def _folder(eid):
    return DATA_ROOT / "worker_records" / f"{eid}_Wf345_Synthetic"


def _seed():
    conn = _db()
    try:
        _purge_workers(conn)
        conn.execute("INSERT INTO projects (project_code, name, address, status, created_at, updated_at) "
                     "VALUES ('WF345-P', 'Wf345 Synthetic', '1 Test St', 'active', ?, ?)", (TODAY, TODAY))
        for eid, wid, trade in ALL:
            conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (eid, wid, f"Wf345 Synthetic {wid[-1]}", trade, TODAY, TODAY))
            status = "inactive" if eid == C[0] else "active"
            conn.execute("INSERT INTO labor_worker_state (worker_id, employee_id, trade, current_rate, status, "
                         "effective_date, created_at, updated_at) VALUES (?, ?, ?, 38.0, ?, ?, ?, ?)",
                         (wid, eid, trade, status, TODAY, TODAY, TODAY))
            conn.execute("INSERT INTO labor_rate_change (worker_id, employee_id, old_rate, new_rate, effective_date, "
                         "status, is_initial, submitted_at) VALUES (?, ?, NULL, 38.0, ?, 'approved', 1, ?)",
                         (wid, eid, TODAY, TODAY))
        for eid in (A[0], C[0], D[0]):
            conn.execute("INSERT INTO sign_in_log (date, employee_id, project_code, time_in, time_out, created_at, "
                         "updated_at) VALUES (?, ?, 'WF345-P', '07:00', '15:30', ?, ?)", (TODAY, eid, TODAY, TODAY))
        conn.execute("INSERT INTO project_assignments (project_code, employee_id, role_on_project, start_date, status) "
                     "VALUES ('WF345-P', ?, 'laborer', ?, 'active')", (B[0], TODAY))
        ids = {}
        for role in ("admin", "c_suite", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Smoke {role} Wf345"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    for eid in (B[0], E[0]):
        f = _folder(eid)
        f.mkdir(parents=True, exist_ok=True)
        (f / "face.jpg").write_bytes(b"synthetic")


def _purge_workers(conn):
    eids = [x[0] for x in ALL]
    wids = [x[1] for x in ALL]
    for t, c, vals in (("sign_in_log", "employee_id", eids), ("project_assignments", "employee_id", eids),
                       ("labor_rate_change", "worker_id", wids), ("labor_worker_state", "worker_id", wids),
                       ("audit_log", "target_id", eids), ("employees", "employee_id", eids),
                       ("projects", "project_code", ["WF345-P"])):
        purge(conn, t, c, vals)


def _cleanup():
    conn = _db()
    try:
        _purge_workers(conn)
        conn.commit()
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
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


def _ids(rows):
    return {r["employee_id"] for r in rows}


def _scalar(sql, args=()):
    conn = _db()
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def main() -> int:
    _seed()
    passed = 0
    admin, cs, pm = _login("admin"), _login("c_suite"), _login("pm")

    # ---- Active / Inactive split + counts; headcount = active only
    act = cs.get(f"{API}/workforce").get_json()["data"]
    ina = cs.get(f"{API}/workforce?status=inactive").get_json()["data"]
    assert act["status"] == "active" and ina["status"] == "inactive"
    assert {A[0], B[0], D[0], E[0]} <= _ids(act["workers"]) and C[0] not in _ids(act["workers"])
    assert C[0] in _ids(ina["workers"]) and not ({A[0], B[0]} & _ids(ina["workers"]))
    assert act["counts"] == ina["counts"] and act["summary"]["headcount"] == act["counts"]["active"]
    active_view = _scalar("SELECT COUNT(1) FROM v_active_workers")
    assert act["counts"]["active"] == active_view, (act["counts"], active_view)
    ov = cs.get(f"{API}/overview").get_json()["data"]
    assert ov["workforce"]["headcount"] == active_view, "Overview headcount must exclude inactive workers"
    c_row = next(w for w in ina["workers"] if w["employee_id"] == C[0])
    assert c_row["state"] == "inactive" and c_row["last_on_site"] == TODAY
    passed += 1; print("  active / inactive split ok")

    # ---- gates
    for path, method in ((f"{API}/workers/{A[0]}/archive", "post"), (f"{API}/workers/{A[0]}/reactivate", "post"),
                         (f"{API}/workers/{A[0]}/removal", "get"), (f"{API}/workers/{B[0]}", "delete"),
                         (f"/api/employees/{B[0]}", "delete")):
        assert getattr(pm, method)(path, json={}).status_code == 403, path
    assert cs.delete(f"{API}/workers/{B[0]}").status_code == 403, "delete permanently is admin-only"
    passed += 1; print("  gates ok")

    # ---- archive / reactivate
    r = cs.post(f"{API}/workers/{A[0]}/archive", json={"reason": "left the company"})
    assert r.status_code == 200 and r.get_json()["data"]["state"] == "archived", r.get_json()
    assert cs.post(f"{API}/workers/{A[0]}/archive", json={}).status_code == 409
    ina = cs.get(f"{API}/workforce?status=inactive").get_json()["data"]
    a_row = next(w for w in ina["workers"] if w["employee_id"] == A[0])
    assert a_row["state"] == "archived" and a_row["archived_reason"] == "left the company" and a_row["archived_at"]
    det = cs.get(f"{API}/workers/{A[0]}").get_json()["data"]["worker"]
    assert det["status"] == "archived" and det["archived_reason"] == "left the company"
    assert _scalar("SELECT COUNT(*) FROM audit_log WHERE action='worker_archive' AND target_id=?", (A[0],)) == 1
    r = cs.post(f"{API}/workers/{A[0]}/reactivate", json={})
    assert r.status_code == 200 and r.get_json()["data"]["state"] == "active"
    assert cs.post(f"{API}/workers/{A[0]}/reactivate", json={}).status_code == 409
    assert _scalar("SELECT archived_at FROM employees WHERE employee_id=?", (A[0],)) is None
    r = cs.post(f"{API}/workers/{C[0]}/reactivate", json={})
    assert r.status_code == 200, r.get_json()
    assert _scalar("SELECT status FROM labor_worker_state WHERE worker_id=?", (C[1],)) == "active", \
        "reactivate also lifts a PM-approved labor deactivation"
    passed += 1; print("  archive / reactivate ok")

    # ---- delete permanently: blocked by history, complete when history-free
    p = admin.get(f"{API}/workers/{A[0]}/removal").get_json()["data"]
    assert p["can_delete"] is False and p["blocking"].get("sign-ins") == 1, p
    assert "name" not in json.dumps(p), "no names in the removal preview"
    r = admin.delete(f"{API}/workers/{A[0]}")
    assert r.status_code == 409 and _scalar("SELECT COUNT(*) FROM employees WHERE employee_id=?", (A[0],)) == 1
    p = admin.get(f"{API}/workers/{B[0]}/removal").get_json()["data"]
    assert p["can_delete"] is True and p["folder_files"] == 1, p
    assert p["removes"].get("labor_worker_state", 0) >= 1 and p["removes"].get("labor_rate_change", 0) >= 1 \
        and p["removes"].get("project_assignments") == 1, p["removes"]
    r = admin.delete(f"{API}/workers/{B[0]}")
    assert r.status_code == 200 and r.get_json()["data"]["deleted"] is True, r.get_json()
    for sql in ("SELECT COUNT(*) FROM employees WHERE employee_id=?",
                "SELECT COUNT(*) FROM project_assignments WHERE employee_id=?",
                "SELECT COUNT(*) FROM labor_worker_state WHERE employee_id=?",
                "SELECT COUNT(*) FROM labor_rate_change WHERE employee_id=?"):
        assert _scalar(sql, (B[0],)) == 0, sql
    for sql in ("SELECT COUNT(*) FROM labor_worker_state WHERE worker_id=?",
                "SELECT COUNT(*) FROM labor_rate_change WHERE worker_id=?"):
        assert _scalar(sql, (B[1],)) == 0, sql
    assert not _folder(B[0]).exists(), "the worker's records folder goes too"
    aud = _scalar("SELECT before_json FROM audit_log WHERE action='worker_delete' AND target_id=?", (B[0],))
    assert aud and "Wf345" not in aud and json.loads(aud)["worker_id"] == B[1], "audit keeps IDs, not names"
    passed += 1; print("  delete permanently ok")

    # ---- the old route: company-only, same rules
    r = admin.delete(f"/api/employees/{D[0]}", json={"reason": "legacy path"})
    assert r.status_code == 200 and r.get_json()["data"]["deleted"] == "archived", r.get_json()
    r = admin.delete(f"/api/employees/{E[0]}")
    assert r.status_code == 200 and r.get_json()["data"]["deleted"] == "hard", r.get_json()
    assert _scalar("SELECT COUNT(*) FROM labor_worker_state WHERE worker_id=?", (E[1],)) == 0, \
        "the old hard-delete no longer leaves the labor state behind"
    assert not _folder(E[0]).exists()
    passed += 1; print("  legacy delete route ok")

    # ---- the served portal carries the new UI
    html = admin.get("/company").get_data(as_text=True)
    for needle in ('data-wf-status="active"', 'data-wf-status="inactive"', 'id="wf-n-inactive"',
                   "/workforce?status=", "data-wf-reactivate", "#wk-archive", "#wk-delete", "/removal'"):
        assert needle in html, needle
    passed += 1; print("  served page ok")

    print(f"PASS smoke_workforce_archive_345 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    sys.exit(code)
