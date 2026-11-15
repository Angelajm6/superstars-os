#!/usr/bin/env python3
"""New-worker approval (2026-11-15): behavioral guard.

Boots the real app on an ISOLATED database + data root (synthetic workers only). Proves:

  * migration: employees.approval_status / approved_by_uid / approved_at exist, idempotent
  * legacy workers (approval_status NULL) read as approved and still sign in
  * POST /api/workers/create -> the new worker is 'pending'; the response says so
  * a pending worker's PIN is refused by the worker app (403, awaiting_approval)
  * who approves: admin / c_suite any worker; pm only on an assigned active job;
    super on an active job; a pm off the job -> 403; client -> 403; anon -> 401
  * approve flips to approved (by + at recorded), is idempotent, and sign-in then works
  * Workforce (intake-summary) and the job Crew view carry approval_status;
    the Crew view says whether the viewer can approve
  * pay rates unchanged: a pm still cannot approve a rate change

Run:  venv/bin/python tests/smoke_worker_approval.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_wapprove_")
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)
import worker_approval  # noqa: E402

TAG = "wapprove"
PW = "WorkerApproveSmoke2026"
ROLES = ("admin", "c_suite", "pm", "pm2", "super", "client")
JOB, OTHER = "WAP-001", "WAP-002"
TODAY = date.today().isoformat()


def _db():
    return db_layer.connect(pragma_fk=False)


def _cleanup_rows(conn):
    for sql in ("DELETE FROM worker_app_session WHERE employee_id IN (SELECT employee_id FROM employees WHERE name LIKE 'Wapprove %')",
                "DELETE FROM sign_in_log WHERE project_code LIKE 'WAP-%'",
                "DELETE FROM project_assignments WHERE project_code LIKE 'WAP-%'",
                "DELETE FROM pm_project_assignment WHERE project_code LIKE 'WAP-%'",
                "DELETE FROM labor_rate_change WHERE worker_id LIKE 'W-97%'",
                "DELETE FROM labor_worker_state WHERE worker_id LIKE 'W-97%'",
                "DELETE FROM employees WHERE name LIKE 'Wapprove %'",
                "DELETE FROM projects WHERE project_code LIKE 'WAP-%'"):
        try:
            conn.execute(sql)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for role in ROLES:
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), "pm" if role == "pm2" else role, f"Smoke {role}"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        _cleanup_rows(conn)
        conn.execute("DELETE FROM projects WHERE status = 'active' OR status IS NULL")   # isolated copy: only our jobs
        for code in (JOB, OTHER):
            conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, ?, 'x', 'active')",
                         (code, f"Wapprove {code}"))
        conn.execute("INSERT INTO pm_project_assignment (project_code, user_id) VALUES (?, ?)", (JOB, ids["pm"]))
        conn.execute("INSERT INTO pm_project_assignment (project_code, user_id) VALUES (?, ?)", (OTHER, ids["pm2"]))
        # a legacy worker from before the feature: approval_status NULL
        conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, pin, created_at, updated_at) "
                     "VALUES ('E-97001', 'W-9701', 'Wapprove Legacy', 'mason', '9701', ?, ?)", (TODAY, TODAY))
        conn.execute("INSERT INTO project_assignments (project_code, employee_id, start_date, status) "
                     "VALUES (?, 'E-97001', ?, 'active')", (JOB, TODAY))
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = _db()
    try:
        _cleanup_rows(conn)
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


def _worker_login(pin):
    c = server.app.test_client()
    return c.post("/api/worker/login", json={"phone_or_pin": pin, "latitude": 0, "longitude": 0},
                  environ_base={"REMOTE_ADDR": f"10.97.{int(pin) % 250}.1"})


def _create(cs, name, phone, jobs):
    r = cs.post("/api/workers/create", json={"name": name, "trade": "mason", "phone": phone, "project_codes": jobs})
    assert r.status_code == 200, (r.status_code, r.get_json())
    return r.get_json()["data"]


def main() -> int:
    checks = 0
    conn = _db()
    try:
        worker_approval.ensure(conn)
        worker_approval.ensure(conn)   # idempotent
        for col in ("approval_status", "approved_by_uid", "approved_at"):
            assert worker_approval._has_column(conn, "employees", col), col
    finally:
        conn.close()
    checks += 1
    ids = _seed()
    admin, cs, pm, pm2, sup, client = (_login(r) for r in ROLES)

    # legacy worker: approved, signs in
    r = _worker_login("9701")
    assert r.status_code == 200, ("legacy worker must still sign in", r.status_code, r.get_json())
    checks += 1

    # create -> pending, response says so; sign-in refused
    a = _create(cs, "Wapprove Alpha", "2125559702", [JOB])
    b = _create(admin, "Wapprove Bravo", "2125559703", [OTHER])
    loose = _create(admin, "Wapprove Loose", "2125559704", [OTHER])
    assert a["approval_status"] == "pending"
    conn = _db()
    try:
        assert worker_approval.is_pending(conn, a["employee_id"])
    finally:
        conn.close()
    r = _worker_login("9702")
    assert r.status_code == 403 and r.get_json().get("awaiting_approval") is True, (r.status_code, r.get_json())
    checks += 1

    # Workforce + Crew carry the status
    rows = {w["employee_id"]: w for w in cs.get("/api/workers/intake-summary").get_json()["data"]}
    assert rows[a["employee_id"]]["approval_status"] == "pending"
    assert rows["E-97001"]["approval_status"] == "approved"
    crew = pm.get(f"/api/projects/{JOB}/crew-compliance").get_json()["data"]
    assert crew["can_approve"] is True and crew["pending_count"] == 1
    assert {w["employee_id"]: w["approval_status"] for w in crew["workers"]}[a["employee_id"]] == "pending"
    checks += 1

    # who may approve
    url = lambda e: f"/api/workers/{e}/approve"   # noqa: E731
    assert server.app.test_client().post(url(a["employee_id"])).status_code == 401
    assert client.post(url(a["employee_id"])).status_code == 403
    assert pm2.post(url(a["employee_id"])).status_code == 403, "a PM off the job cannot approve"
    assert pm.post(url(b["employee_id"])).status_code == 403, "a PM cannot approve a worker on someone else's job"
    assert pm.post(url("E-00000-nope")).status_code == 404
    r = pm.post(url(a["employee_id"]))
    assert r.status_code == 200 and r.get_json()["data"]["approval_status"] == "approved", r.get_json()
    assert pm.post(url(a["employee_id"])).get_json()["data"]["already_approved"] is True
    assert sup.post(url(b["employee_id"])).status_code == 200, "a super approves on an active job"
    assert cs.post(url(loose["employee_id"])).status_code == 200, "C-suite approves anyone"
    conn = _db()
    try:
        row = conn.execute("SELECT approval_status, approved_by_uid, approved_at FROM employees WHERE employee_id = ?",
                           (a["employee_id"],)).fetchone()
        assert row["approval_status"] == "approved" and row["approved_by_uid"] == ids["pm"] and row["approved_at"]
    finally:
        conn.close()
    checks += 1

    # approved -> signs in
    r = _worker_login("9702")
    assert r.status_code == 200, ("approved worker signs in", r.status_code, r.get_json())
    checks += 1

    # pay rates unchanged: a PM still cannot approve a rate change
    conn = _db()
    try:
        conn.execute("INSERT INTO labor_worker_state (worker_id, employee_id, trade, current_rate, status, effective_date, "
                     "created_at, updated_at) VALUES ('W-9701', 'E-97001', 'mason', 30, 'active', ?, ?, ?)", (TODAY, TODAY, TODAY))
        conn.commit()
    finally:
        conn.close()
    r = admin.post("/api/labor-rates/changes", json={"worker_id": "W-9701", "new_rate": 31, "effective_date": TODAY})
    assert r.status_code == 201, r.get_json()
    cid = r.get_json()["data"]["change_id"]
    assert pm.post(f"/api/labor-rates/changes/{cid}/approve").status_code == 403
    assert sup.post(f"/api/labor-rates/changes/{cid}/approve").status_code == 403
    checks += 1

    print(f"PASS: new-worker approval — migration, legacy workers unaffected, create -> pending, sign-in blocked, "
          f"approver scoping (admin / C-suite / PM on own job / super), approve + sign-in, roster flags, pay rates unchanged ({checks} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
