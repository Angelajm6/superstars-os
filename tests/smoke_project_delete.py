#!/usr/bin/env python3
"""Delete a project — guard.

Boots the real app on an ISOLATED database. Proves:
  * DELETE /api/admin/projects/<code>: company roles only (pm 403), unknown job 404
  * a job made with "+ New project" and nothing filed goes, with its crew, PM assignment and IRA
    row; the estimate made for it goes too
  * a job with a daily report is refused (409, "1 daily report"), nothing changes
  * a job converted from a real bid sends that estimate back to approved, unconverted
  * the Projects tab no longer renders access-method chips; the Edit window offers Delete
Run:  venv/bin/python tests/smoke_project_delete.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_pdel_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "pdel"
PW = "ProjectDelete2026"
T = date.today().isoformat()
CODES: list = []
ORGS: list = []


def _db():
    return db_layer.connect(pragma_fk=False)


def _count(sql, params=()):
    conn = _db()
    try:
        return int(conn.execute(sql, params).fetchone()[0] or 0)
    finally:
        conn.close()


def _seed() -> dict:
    conn = _db()
    try:
        ids = {}
        for role in ("admin", "c_suite", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Smoke {role} Pdel"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.commit()
        return ids
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        purge(conn, "report_index", "project_code", CODES)
        purge(conn, "pm_project_assignment", "project_code", CODES)
        purge(conn, "ira_job", "project_code", CODES)
        purge(conn, "estimate", "converted_project_code", CODES)
        purge(conn, "projects", "project_code", CODES)
        purge(conn, "estimate", "client_org_id", ORGS)
        purge(conn, "crm_activity", "entity_id", ORGS)
        purge(conn, "crm_organization", "id", ORGS)
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


def _onboard(cs, pm_id, n):
    r = cs.post("/api/admin/projects/onboard", json={
        "est_type": "FR", "borough": "BX", "address": f"{n} Pdel Street", "name": f"Pdel job {n}",
        "new_org_name": f"Pdel Client {n}", "billing_type": "lump_sum", "start_date": T, "pm_user_id": pm_id,
        "access_methods": ["suspended_scaffold"]})
    assert r.status_code == 201, r.get_json()
    d = r.get_json()["data"]
    CODES.append(d["project_code"]); ORGS.append(d["client_org_id"])
    return d


def main() -> int:
    ids = _seed()
    passed = 0
    admin, cs, pm = _login("admin"), _login("c_suite"), _login("pm")
    url = lambda c: f"/api/admin/projects/{c}"   # noqa: E731

    # ---- gates
    a = _onboard(cs, ids["pm"], 1)
    code = a["project_code"]
    assert pm.delete(url(code)).status_code == 403
    assert cs.delete(url("FR-BX-NOPE")).status_code == 404
    passed += 1; print("  gates ok")

    # ---- a job with a daily report is refused, nothing changes
    conn = _db()
    try:
        conn.execute("INSERT INTO report_index (report_date, project_code, report_type, status) VALUES (?, ?, 'DCR', 'draft')", (T, code))
        conn.commit()
    finally:
        conn.close()
    r = cs.delete(url(code))
    assert r.status_code == 409 and "1 daily report" in r.get_json()["error"], r.get_json()
    assert _count("SELECT COUNT(1) FROM projects WHERE project_code = ?", (code,)) == 1
    assert _count("SELECT COUNT(1) FROM pm_project_assignment WHERE project_code = ?", (code,)) == 1
    passed += 1; print("  records block ok")

    # ---- nothing filed: the job, its setup and its on-the-fly estimate go
    conn = _db()
    try:
        conn.execute("DELETE FROM report_index WHERE project_code = ?", (code,)); conn.commit()
    finally:
        conn.close()
    r = admin.delete(url(code))
    assert r.status_code == 200 and r.get_json()["data"]["deleted"] == code, r.get_json()
    for t in ("projects", "pm_project_assignment", "ira_job", "project_assignments"):
        assert _count(f"SELECT COUNT(1) FROM {t} WHERE project_code = ?", (code,)) == 0, t
    assert _count("SELECT COUNT(1) FROM estimate WHERE id = ?", (a["estimate_id"],)) == 0
    assert admin.delete(url(code)).status_code == 404
    passed += 1; print("  delete ok")

    # ---- a job converted from a real bid: the bid goes back to approved
    conn = _db()
    try:
        est = estimates.create_estimate(conn, est_type="FR", borough="BX", client_org_id=ORGS[0],
                                        building_address="9 Pdel Bid Street", notes="A real bid", created_by=ids["admin"])
        conn.execute("UPDATE estimate SET status = 'approved', final_amount = 1000 WHERE id = ?", (est["id"],)); conn.commit()
        bid_code = estimates.convert_to_project(conn, est["id"], actor_user_id=ids["admin"])["project_code"]
        CODES.append(bid_code)
    finally:
        conn.close()
    assert cs.delete(url(bid_code)).status_code == 200
    conn = _db()
    try:
        row = dict(conn.execute("SELECT status, converted_project_code FROM estimate WHERE id = ?", (est["id"],)).fetchone())
    finally:
        conn.close()
    assert row == {"status": "approved", "converted_project_code": None}, row
    passed += 1; print("  real bid reverts ok")

    # ---- UI: no access chips on the Projects tab; Delete in the Edit window
    html = (ROOT / "company_portal.html").read_text(encoding="utf-8")
    assert "pj-acc" not in html and "access_labels" not in html
    assert "ssc:project-deleted" in html
    js = (ROOT / "static" / "js" / "new_project_modal.js").read_text(encoding="utf-8")
    assert 'id="np-del"' in js and "'DELETE', '/api/admin/projects/'" in js
    passed += 1; print("  ui ok")

    print(f"OK smoke_project_delete: {passed} checks")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_project_delete: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
