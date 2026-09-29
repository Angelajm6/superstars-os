#!/usr/bin/env python3
"""#356 — billing type per project (AIA / SOV · lump sum · T&M · unit price). Guard.

Boots the real app on an ISOLATED database. Proves:
  * ensure() is idempotent; backfill marks jobs that already have billing_sov rows as
    aia_sov and never overwrites a type someone picked
  * PUT /api/admin/projects/<code>/billing-type: company roles only (pm 403), bad value 400,
    unknown project 404; a job with AIA pay applications can't leave AIA (409)
  * AIA writes (save SOV, import, new application) refuse on a job billed another way (409)
    and still work on a job whose type isn't set
  * onboarding meta offers the types and a bad type is refused before anything is created
  * the Projects list carries billing_type / label / short
  * the project page carries <body data-billing-type> and hides the Schedule of Values link
    on a non-AIA job; a pm's page has no SOV link at all
Run:  venv/bin/python tests/smoke_billing_type_356.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_bt_356_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import billing_type  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "bt356"
PW = "BillingType2026"
A, L, N = "BT356-A", "BT356-L", "BT356-N"      # has SOV rows (backfill) · lump sum · not set
T = date.today().isoformat()


def _db():
    return db_layer.connect(pragma_fk=False)


def _purge(conn):
    ids = [r["id"] for r in conn.execute("SELECT id FROM pay_application WHERE project_code IN (?,?,?)", (A, L, N)).fetchall()]
    purge(conn, "pay_application_line", "application_id", ids)
    inv = [r["invoice_id"] for r in conn.execute("SELECT invoice_id FROM pay_application WHERE project_code IN (?,?,?)",
                                                 (A, L, N)).fetchall() if r["invoice_id"]]
    purge(conn, "pay_application", "project_code", [A, L, N])
    purge(conn, "invoice", "id", inv)
    purge(conn, "billing_sov", "project_code", [A, L, N])
    purge(conn, "pm_project_assignment", "project_code", [A, L, N])
    purge(conn, "projects", "project_code", [A, L, N])


def _seed():
    conn = _db()
    try:
        _purge(conn)
        for code in (A, L, N):
            conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, ?, '1 Bt St', 'active')",
                         (code, f"Bt356 {code}"))
        conn.execute("INSERT INTO billing_sov (project_code, item_no, description, scheduled_value, kind, sort, created_at, updated_at) "
                     "VALUES (?, '1', 'Facade', 100000, 'base', 1, ?, ?)", (A, T, T))
        conn.execute("UPDATE projects SET billing_type = NULL WHERE project_code IN (?,?,?)", (A, L, N))
        conn.execute("UPDATE projects SET billing_type = 'lump_sum' WHERE project_code = ?", (L,))
        ids = {}
        for role in ("admin", "c_suite", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Smoke {role} Bt"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        for code in (A, L, N):
            conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, ?)", (ids["pm"], code, T))
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        _purge(conn)
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


def _bt(code):
    conn = _db()
    try:
        return conn.execute("SELECT billing_type FROM projects WHERE project_code = ?", (code,)).fetchone()["billing_type"]
    finally:
        conn.close()


def main() -> int:
    _seed()
    passed = 0
    admin, cs, pm = _login("admin"), _login("c_suite"), _login("pm")

    # ---- ensure + backfill
    conn = _db()
    try:
        billing_type.ensure(conn); billing_type.ensure(conn)
    finally:
        conn.close()
    assert _bt(A) == "aia_sov" and _bt(L) == "lump_sum" and _bt(N) is None, (_bt(A), _bt(L), _bt(N))
    passed += 1; print("  ensure + backfill ok")

    # ---- set: gates, validation, the pay-application lock
    url = lambda c: f"/api/admin/projects/{c}/billing-type"   # noqa: E731
    assert pm.put(url(N), json={"billing_type": "lump_sum"}).status_code == 403
    assert cs.put(url(N), json={"billing_type": "nonsense"}).status_code == 400
    assert cs.put(url("BT356-NOPE"), json={"billing_type": "lump_sum"}).status_code == 404
    r = cs.put(url(N), json={"billing_type": "time_materials"})
    assert r.status_code == 200 and r.get_json()["data"]["billing_short"] == "T&M", r.get_json()
    assert cs.put(url(N), json={"billing_type": "unit_price"}).status_code == 200
    conn = _db()
    try:
        conn.execute("UPDATE projects SET billing_type = NULL WHERE project_code = ?", (N,)); conn.commit()
    finally:
        conn.close()
    passed += 1; print("  set + gates ok")

    # ---- AIA writes: refused on lump sum, allowed on AIA and on not-set
    sov = {"rows": [{"item_no": "1", "description": "Facade", "scheduled_value": 50000, "kind": "base"}]}
    r = cs.put(f"/api/projects/{L}/billing/sov", json=sov)
    assert r.status_code == 409 and "lump sum" in r.get_json()["error"], r.get_json()
    assert cs.post(f"/api/projects/{L}/billing/sov/import").status_code == 409
    assert cs.post(f"/api/projects/{L}/billing/applications", json={}).status_code == 409
    assert cs.put(f"/api/projects/{N}/billing/sov", json=sov).status_code == 200, "a not-set job keeps working"
    r = cs.post(f"/api/projects/{A}/billing/applications", json={"period_to": T, "application_date": T})
    assert r.status_code == 201, r.get_json()
    r = cs.put(url(A), json={"billing_type": "lump_sum"})
    assert r.status_code == 409 and "pay application" in r.get_json()["error"], r.get_json()
    assert _bt(A) == "aia_sov"
    passed += 1; print("  AIA guards ok")

    # ---- onboarding meta + bad type refused before anything is created
    m = cs.get("/api/admin/projects/onboard-meta").get_json()["data"]
    assert [b["code"] for b in m["billing_types"]] == ["aia_sov", "lump_sum", "time_materials", "unit_price"]
    conn = _db()
    try:
        n_est = conn.execute("SELECT COUNT(*) FROM estimate").fetchone()[0]
    finally:
        conn.close()
    r = cs.post("/api/admin/projects/onboard", json={"est_type": "FR", "borough": "BX", "address": "1 Bt356 Onboard St",
                                                     "new_org_name": "Bt356 Org", "billing_type": "barter"})
    assert r.status_code == 400 and "billed" in r.get_json()["error"], r.get_json()
    conn = _db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM estimate").fetchone()[0] == n_est, "nothing created on a bad type"
    finally:
        conn.close()
    passed += 1; print("  onboarding ok")

    # ---- Projects list + project page
    rows = {r["project_code"]: r for r in cs.get("/api/company/portal/projects?status=all").get_json()["data"]}
    assert rows[A]["billing_type"] == "aia_sov" and rows[A]["billing_short"] == "AIA"
    assert rows[L]["billing_label"] == "Lump sum" and rows[N]["billing_type"] is None and rows[N]["billing_short"] == "Not set"
    page_l = cs.get(f"/projects/{L}").get_data(as_text=True)
    assert '<body data-billing-type="lump_sum">' in page_l
    assert '<a class="nav-link" data-view="sov" hidden>' in page_l, "the SOV link hides on a lump-sum job"
    page_a = cs.get(f"/projects/{A}").get_data(as_text=True)
    assert '<body data-billing-type="aia_sov">' in page_a and '<a class="nav-link" data-view="sov">' in page_a
    page_pm = pm.get(f"/projects/{A}").get_data(as_text=True)
    assert '<a class="nav-link" data-view="sov"' not in page_pm and '<section class="view" data-view="sov">' not in page_pm \
        and 'id="ps-billing"' in page_pm, "pm: no money nav / SOV view, read-only billing in Settings"
    prof = pm.get(f"/api/projects/{L}/profile").get_json()["data"]
    assert prof["billing_label"] == "Lump sum" and len(prof["billing_options"]) == 4
    passed += 1; print("  list + page ok")

    print(f"PASS smoke_billing_type_356 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_billing_type_356: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
