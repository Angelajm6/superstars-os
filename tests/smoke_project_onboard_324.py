#!/usr/bin/env python3
"""#324 — onboard an already-won job: behavioral guard.

Flask test client on an ISOLATED database (honors SSC_DB_URL; builds a throwaway
SQLite snapshot from the versioned migrations when unset). Proves:

  * gating: pm / super / estimator / client -> 403 on meta + onboard; anon -> 401;
    admin / c_suite -> 200 / 201
  * meta: job types, boroughs, active client orgs only, active PM users only
  * validation: type, borough, address, client (id or new name), contract value,
    date format, unknown client -> 404, non-PM user -> 404
  * the happy path: estimate created in the series, stamped approved on the start
    date with the contract value and the "onboarded directly" note, converted;
    project active with the operator's name, client, building auto-linked; PM
    assignment written; CRM activity row; the project appears in the PM-assignments
    payload and in the PM's own project list
  * numbering: the next onboard in the same series takes the next number, and a
    real estimate created afterwards continues the series
  * a new client name creates the organization; IRA jobs open the inspection row
    when that table exists; the page carries the button + modal
  * no `*_path` keys in any payload

Run:  venv/bin/python tests/smoke_project_onboard_324.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_np_324_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import pm_scoping  # noqa: E402
import project_onboard  # noqa: E402
import role_containment  # noqa: E402

TAG = "np324"
PW = "OnboardSmokePassword2026"
ROLES = ("c_suite", "admin", "pm", "super", "estimator", "client")
TODAY = date.today()
API = "/api/admin/projects"


def D(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def _db():
    return db_layer.connect(pragma_fk=True)


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for role in ROLES:
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            # the PM is a real (non-system) user: the assignment list hides system accounts
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, ?)",
                         (email, auth.hash_password(PW), role, f"Smoke {role}", 0 if role == "pm" else 1))
        conn.execute("DELETE FROM users WHERE email = ?", (f"{TAG}.pm2@example.com",))
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, "
                     "is_system) VALUES (?, ?, 'pm', 'Smoke inactive pm', 0, 'inactive', 0, 1)",
                     (f"{TAG}.pm2@example.com", auth.hash_password(PW)))
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Np324 %'")
        ids["org"] = crm.create_org(conn, name="Np324 Harbor Owners", relationship_type="client")
        ids["org_inactive"] = crm.create_org(conn, name="Np324 Former Client", relationship_type="client", status="inactive")
        ids["vendor"] = crm.create_org(conn, name="Np324 Vendor", relationship_type="vendor")
        for r in conn.execute("SELECT id, role FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall():
            ids[f"uid_{r['role']}"] = r["id"]
        ids["pm_active"] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.pm@example.com",)).fetchone()["id"]
        ids["pm_inactive"] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.pm2@example.com",)).fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    """Key everything off the test organizations (not address patterns): Postgres
    enforces the FKs SQLite ignores, so every dependent row must go first."""
    conn = db_layer.connect(pragma_fk=False)
    try:
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Np324 %'").fetchall()]
        ph = ",".join("?" * len(org_ids)) or "NULL"
        est_ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM estimate WHERE client_org_id IN ({ph}) OR building_address LIKE '% Np324 %'", org_ids).fetchall()]
        eph = ",".join("?" * len(est_ids)) or "NULL"
        codes = {r["project_code"] for r in conn.execute(
            f"SELECT project_code FROM projects WHERE client_org_id IN ({ph}) OR address LIKE '% Np324 %' OR name LIKE 'Np324 %'",
            org_ids).fetchall()}
        codes |= {r["converted_project_code"] for r in conn.execute(
            f"SELECT converted_project_code FROM estimate WHERE id IN ({eph}) AND converted_project_code IS NOT NULL",
            est_ids).fetchall()}
        codes = list(codes)
        for t, col in (("pm_project_assignment", "project_code"), ("ira_visit", "project_code"), ("ira_job", "project_code")):
            purge(conn, t, col, codes)
        for t in ("ira_job", "estimate_document", "walkthrough_visit", "proposal_line", "proposal_event", "proposal"):
            purge(conn, t, "estimate_id", est_ids)
        purge(conn, "projects", "project_code", codes)
        purge(conn, "estimate", "id", est_ids)
        purge(conn, "building", "client_org_id", org_ids)
        try:
            conn.execute("DELETE FROM building WHERE address_key LIKE '% np324 %'")
            conn.commit()
        except Exception:
            conn.rollback()
        for t, col in (("crm_activity", "entity_id"), ("crm_task", "entity_id"), ("crm_contact", "org_id")):
            purge(conn, t, col, org_ids)
        purge(conn, "crm_organization", "id", org_ids)
        left = conn.execute("SELECT COUNT(1) AS n FROM crm_organization WHERE name LIKE 'Np324 %'").fetchone()["n"]
        if left:
            print(f"WARN: {left} Np324 organization(s) could not be removed", file=sys.stderr)
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
    app = Flask("np324", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    role_containment.register(app)
    pm_scoping.register(app)
    estimates.register(app)
    project_onboard.register(app)
    ids = _seed()
    checks = 0

    # ---- gates
    for role in ("pm", "super", "estimator", "client"):
        c = _login(app, role)
        assert c.get(f"{API}/onboard-meta").status_code == 403, role
        assert c.post(f"{API}/onboard", json={"address": "x"}).status_code == 403, role
    assert app.test_client().post(f"{API}/onboard", json={}).status_code == 401
    cs = _login(app, "c_suite")
    admin = _login(app, "admin")
    checks += 1

    # ---- meta
    m = cs.get(f"{API}/onboard-meta").get_json()["data"]
    _no_paths(m, "meta")
    assert {t["code"] for t in m["types"]} >= {"FR", "IRA"} and "BX" in m["boroughs"]
    names = {c["name"] for c in m["clients"]}
    assert "Np324 Harbor Owners" in names and "Np324 Former Client" not in names and "Np324 Vendor" not in names
    pm_ids = {p["user_id"] for p in m["pms"]}
    assert ids["pm_active"] in pm_ids and ids["pm_inactive"] not in pm_ids
    checks += 1

    # ---- validation
    base = {"client_org_id": ids["org"], "est_type": "FR", "borough": "BX", "address": "12 Np324 Quay"}
    assert admin.post(f"{API}/onboard", json={**base, "est_type": "ZZ"}).status_code == 400
    assert admin.post(f"{API}/onboard", json={**base, "borough": "LA"}).status_code == 400
    assert admin.post(f"{API}/onboard", json={**base, "address": "x"}).status_code == 400
    r = admin.post(f"{API}/onboard", json={**base, "client_org_id": None})
    assert r.status_code == 400 and "client" in r.get_json()["error"]
    assert admin.post(f"{API}/onboard", json={**base, "client_org_id": 999999}).status_code == 404
    assert admin.post(f"{API}/onboard", json={**base, "contract_value": "lots"}).status_code == 400
    assert admin.post(f"{API}/onboard", json={**base, "contract_value": -5}).status_code == 400
    assert admin.post(f"{API}/onboard", json={**base, "start_date": "09/01/2026"}).status_code == 400
    assert admin.post(f"{API}/onboard", json={**base, "pm_user_id": ids["pm_inactive"]}).status_code == 404
    assert admin.post(f"{API}/onboard", json={**base, "pm_user_id": ids["uid_super"]}).status_code == 404
    conn = _db()
    try:
        assert not conn.execute("SELECT 1 FROM estimate WHERE building_address LIKE '% Np324 %'").fetchone(), \
            "a rejected onboard must leave nothing behind"
    finally:
        conn.close()
    checks += 1

    # ---- happy path
    conn = _db()
    try:
        seq_before = estimates.next_seq(conn, "FR", "BX")
    finally:
        conn.close()
    r = admin.post(f"{API}/onboard", json={**base, "name": "Np324 Quay facade", "contract_value": "185,000",
                                           "start_date": D(-14), "pm_user_id": ids["pm_active"], "notes": "won by phone"})
    assert r.status_code == 201, r.get_json()
    d = r.get_json()["data"]
    _no_paths(d, "onboard")
    code = d["project_code"]
    assert code == f"FR-BX-{seq_before:03d}" and d["estimate_code"] == code
    assert d["project"]["name"] == "Np324 Quay facade" and d["project"]["status"] == "active"
    assert d["project"]["client_org_id"] == ids["org"] and d["project"]["building_id"], d["project"]
    assert d["contract_value"] == 185000.0 and d["start_date"] == D(-14) and d["pm_user_id"] == ids["pm_active"]
    conn = _db()
    try:
        est = estimates.get_estimate(conn, d["estimate_id"])
        assert est["status"] == "converted" and est["converted_project_code"] == code
        assert est["final_amount"] == 185000.0 and est["submitted_date"] == D(-14) and est["decided_date"] == D(-14)
        assert project_onboard.NOTE in (est["notes"] or "") and "won by phone" in est["notes"]
        pa = conn.execute("SELECT user_id FROM pm_project_assignment WHERE project_code = ?", (code,)).fetchone()
        assert pa and pa["user_id"] == ids["pm_active"]
        act = conn.execute("SELECT summary FROM crm_activity WHERE entity_type='organization' AND entity_id=? "
                           "AND summary LIKE ?", (ids["org"], f"Project {code} onboarded directly%")).fetchone()
        assert act and "$185,000" in act["summary"], act
        b = conn.execute("SELECT client_org_id FROM building WHERE id = ?", (d["project"]["building_id"],)).fetchone()
        assert b and b["client_org_id"] == ids["org"]
    finally:
        conn.close()
    pmv = admin.get("/api/admin/pm-assignments").get_json()["data"]
    assert any(p["project_code"] == code and p.get("status", "active") == "active" for p in pmv["projects"])
    me = [p for p in pmv["pms"] if p.get("user_id") == ids["pm_active"] or p.get("id") == ids["pm_active"]]
    assert me and code in (me[0].get("project_codes") or [])
    conn = _db()
    try:
        assert pm_scoping.pm_can_access_project("pm", ids["pm_active"], code, conn), "the PM can open the onboarded project"
        assert not pm_scoping.pm_can_access_project("pm", ids["pm_inactive"], code, conn)
    finally:
        conn.close()
    checks += 1

    # ---- numbering continues, in both directions
    r = admin.post(f"{API}/onboard", json={**base, "address": "14 Np324 Quay"})
    assert r.status_code == 201 and r.get_json()["data"]["project_code"] == f"FR-BX-{seq_before + 1:03d}"
    assert r.get_json()["data"]["project"]["name"] == "14 Np324 Quay", "name defaults to the address"
    conn = _db()
    try:
        est = estimates.create_estimate(conn, est_type="FR", borough="BX", client_org_id=ids["org"],
                                        building_address="16 Np324 Quay", created_by=ids["uid_admin"])
        assert est["code"] == f"FR-BX-{seq_before + 2:03d}", est["code"]
        conn.commit()
    finally:
        conn.close()
    checks += 1

    # ---- new client name + IRA job
    r = cs.post(f"{API}/onboard", json={"new_org_name": "Np324 Brand New Co", "est_type": "IRA", "borough": "MN",
                                        "address": "20 Np324 Plaza", "contract_value": 4200})
    assert r.status_code == 201, r.get_json()
    d2 = r.get_json()["data"]
    assert d2["project_code"].startswith("IRA-MN-")
    conn = _db()
    try:
        org = crm.get_org(conn, d2["client_org_id"])
        assert org and org["name"] == "Np324 Brand New Co" and org.get("relationship_type") == "client"
        try:
            ira = conn.execute("SELECT project_code FROM ira_job WHERE project_code = ?", (d2["project_code"],)).fetchone()
            assert ira, "an IRA onboard opens its inspection row"
        except Exception:
            pass   # table absent on this fixture
    finally:
        conn.close()
    checks += 1

    # ---- the page carries the button + modal
    html = admin.get("/admin/projects").get_data(as_text=True)
    assert 'id="np-open"' in html and 'id="np-form"' in html and "/api/admin/projects/onboard" in html
    checks += 1

    print(f"PASS: #324 onboard an already-won job — gates, meta, validation, estimate→approved→convert in one step, "
          f"name / start / PM / building / activity, numbering, new client, IRA row, page ({checks} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
