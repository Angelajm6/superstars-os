#!/usr/bin/env python3
"""Projects tab → Ready to start (2026-10-23): only a SIGNED proposal suggests a project.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * the Projects list carries `meta.ready_to_start` = signed proposals whose estimate is not a project yet, and
    `meta.waiting_signature` = proposals out with the client; a sent (unsigned) proposal is never a suggestion
  * a signed proposal whose address got a project on/after the signing day (made by hand) is not offered
  * "Not a project": PM -> 403; C-suite -> 200 and it leaves the list; an unknown proposal -> 404
  * onboard with `from_proposal_id`: the job takes the proposal's estimate (no second estimate); a changed type /
    region moves that estimate to its series (new code, same estimate), the proposal's client wins over the payload, the deposit invoice moves onto the job, and it leaves the list;
    a second create from it, or one from an unsigned proposal -> 400
  * the served page has Ready to start (button, panel, Create / Not a project) and the form sends from_proposal_id
Run:  venv/bin/python tests/smoke_projects_ready.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_ready_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ["SSC_MAIL_FAKE_DIR"] = str(Path(_TMP) / "mail")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import buildings  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import proposals  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "rdy"
PW = "ReadyToStartSmoke2026"


def _db():
    return db_layer.connect(pragma_fk=False)


def _proposal(conn, org, address, price, admin):
    est = estimates.create_estimate(conn, est_type="FR", borough="BX", client_org_id=org, building_address=address,
                                    notes="Rdy scope", created_by=admin)
    estimates.change_status(conn, est["id"], "scoping", actor_user_id=admin)
    estimates.change_status(conn, est["id"], "submitted", final_amount=price, actor_user_id=admin)
    p = proposals.create_from_estimate(conn, est["id"], created_by=admin)
    return est, p["id"]


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for role in ("c_suite", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Smoke {role} Rdy"))
        admin = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.c_suite@example.com",)).fetchone()["id"]
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Rdy %'")
        ids["org"] = crm.create_org(conn, name="Rdy Gardens Co-op", relationship_type="client")
        ids["org2"] = crm.create_org(conn, name="Rdy Other Client", relationship_type="client")
        conn.commit()
        ids["estA"], ids["A"] = _proposal(conn, ids["org"], "1 Rdy Alpha Ave", 212400, admin)
        ids["estB"], ids["B"] = _proposal(conn, ids["org"], "2 Rdy Beta Ave", 67900, admin)
        ids["estC"], ids["C"] = _proposal(conn, ids["org"], "3 Rdy Gamma Ave", 10000, admin)
        ids["estD"], ids["D"] = _proposal(conn, ids["org"], "4 Rdy Delta Ave", 15000, admin)
        for k in ("A", "B", "D"):
            proposals.mark_signed_offline(conn, ids[k], name="Pat Signer", actor=admin)
        conn.execute("UPDATE proposal SET status = 'sent', sent_at = ? WHERE id = ?", ("2026-10-20T12:00:00", ids["C"]))
        # D: a job was already made by hand at its address after it was signed
        site = buildings.find_or_create(conn, "4 Rdy Delta Ave", client_org_id=ids["org"], city_zip="Bronx, NY 10454")
        conn.execute("UPDATE proposal SET building_id = ? WHERE id = ?", (site, ids["D"]))
        conn.execute("DELETE FROM projects WHERE project_code = 'RDY-HAND'")
        conn.execute("INSERT INTO projects (project_code, name, address, status, client_org_id, building_id, created_at, updated_at) "
                     "VALUES ('RDY-HAND', 'Rdy by hand', '4 Rdy Delta Ave', 'active', ?, ?, '2099-01-01T00:00:00', '2099-01-01T00:00:00')",
                     (ids["org"], site))
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = _db()
    try:
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Rdy %'").fetchall()]
        ph = ",".join("?" * len(org_ids)) or "NULL"
        est = conn.execute(f"SELECT id, code FROM estimate WHERE client_org_id IN ({ph})", org_ids).fetchall()
        est_ids, codes = [r["id"] for r in est], [r["code"] for r in est] + ["RDY-HAND"]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE client_org_id IN ({ph})", org_ids).fetchall()]
        inv = [r["id"] for r in conn.execute(f"SELECT id FROM invoice WHERE client_org_id IN ({ph})", org_ids).fetchall()]
        purge(conn, "invoice", "id", inv)
        for t in ("proposal_line", "proposal_event", "proposal_revision"):
            purge(conn, t, "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        for t in ("pm_project_assignment", "project_assignments", "project_modules"):
            try:
                purge(conn, t, "project_code", codes)
            except Exception:
                conn.rollback()
        purge(conn, "projects", "project_code", codes)
        purge(conn, "estimate", "id", est_ids)
        conn.execute("DELETE FROM building WHERE address_line LIKE '% Rdy %'")
        for t, col in (("crm_activity", "entity_id"), ("crm_task", "entity_id"), ("crm_contact", "org_id")):
            purge(conn, t, col, org_ids)
        purge(conn, "crm_organization", "id", org_ids)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(role):
    c = server.app.test_client()
    assert c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW}).status_code == 200, role
    return c


def main() -> int:
    ids = _seed()
    passed = 0
    cs, pm = _login("c_suite"), _login("pm")
    ready = lambda: {r["proposal_id"]: r for r in cs.get("/api/company/portal/projects?status=all").get_json()["meta"]["ready_to_start"]}   # noqa: E731

    meta = cs.get("/api/company/portal/projects?status=all").get_json()["meta"]
    rd = {r["proposal_id"]: r for r in meta["ready_to_start"]}
    assert ids["A"] in rd and ids["B"] in rd, rd.keys()
    assert ids["C"] not in rd, "a sent proposal is not a suggestion"
    assert ids["D"] not in rd, "a job made by hand at the address after signing hides it"
    assert meta["waiting_signature"] >= 1
    a = rd[ids["A"]]
    assert a["client_org_id"] == ids["org"] and a["client_name"] == "Rdy Gardens Co-op" and a["est_type"] == "FR" and a["borough"] == "BX"
    assert a["price"] == 212400 and a["deposit_invoice"], a
    assert "1 Rdy Alpha Ave" in (a["address"] + a["title"]), a
    passed += 1; print("  ready list ok")

    assert pm.post(f"/api/company/portal/projects/ready/{ids['B']}/dismiss").status_code == 403
    assert cs.post(f"/api/company/portal/projects/ready/{ids['B']}/dismiss").status_code == 200
    assert cs.post(f"/api/company/portal/projects/ready/{ids['C']}/dismiss").status_code == 404, "only a signed proposal"
    assert cs.post("/api/company/portal/projects/ready/999999/dismiss").status_code == 404
    assert ids["B"] not in ready() and ids["A"] in ready()
    passed += 1; print("  not a project ok")

    conn = _db()
    try:
        n_est = conn.execute("SELECT COUNT(1) AS n FROM estimate WHERE client_org_id = ?", (ids["org"],)).fetchone()["n"]
    finally:
        conn.close()
    body = {"from_proposal_id": ids["A"], "est_type": "FR", "borough": "QN", "client_org_id": ids["org2"],
            "address": "1 Rdy Alpha Ave", "name": "Rdy Alpha facade", "contract_value": "212400", "billing_type": "lump_sum"}
    r = cs.post("/api/admin/projects/onboard", json=body)
    assert r.status_code == 201, r.get_json()
    d = r.get_json()["data"]
    assert d["project_code"].startswith("FR-QN-") and d["project_code"] != ids["estA"]["code"], "the borough picked in the form wins"
    assert d["estimate_code"] == d["project_code"] and d["estimate_id"] == ids["estA"]["id"] and d["from_proposal_id"] == ids["A"], d
    assert d["client_org_id"] == ids["org"], "the proposal's client wins"
    conn = _db()
    try:
        assert conn.execute("SELECT COUNT(1) AS n FROM estimate WHERE client_org_id = ?", (ids["org"],)).fetchone()["n"] == n_est, "no second estimate"
        e = dict(conn.execute("SELECT status, converted_project_code, est_type, borough FROM estimate WHERE id = ?", (ids["estA"]["id"],)).fetchone())
        assert e == {"status": "converted", "converted_project_code": d["project_code"], "est_type": "FR", "borough": "QN"}, e
        inv = conn.execute("SELECT project_code FROM invoice WHERE proposal_id = ?", (ids["A"],)).fetchall()
        assert inv and all(x["project_code"] == d["project_code"] for x in inv), [dict(x) for x in inv]
        pj = dict(conn.execute("SELECT name, billing_type FROM projects WHERE project_code = ?", (d["project_code"],)).fetchone())
        assert pj == {"name": "Rdy Alpha facade", "billing_type": "lump_sum"}, pj
    finally:
        conn.close()
    assert ids["A"] not in ready()
    r = cs.post("/api/admin/projects/onboard", json=body)
    assert r.status_code == 400 and "already" in r.get_json()["error"], r.get_json()
    r = cs.post("/api/admin/projects/onboard", json={**body, "from_proposal_id": ids["C"], "address": "3 Rdy Gamma Ave"})
    assert r.status_code == 400 and "signed" in r.get_json()["error"], r.get_json()
    passed += 1; print("  create from proposal ok")

    html = cs.get("/company").get_data(as_text=True)
    for needle in ('id="pj-ready"', 'id="pj-drawer"', "data-pj-start", "data-pj-dismiss", "/dismiss'", "ready_to_start",
                   "waiting_signature", 'id="pj-seg"', 'id="pj-cards"', "Needs you", "from_proposal_id: r.proposal_id"):
        assert needle in html, needle
    assert 'id="pj-notice"' not in html and "sites_without_project" not in html
    js = cs.get("/files/static/js/new_project_modal.js").get_data(as_text=True)
    assert "NP.fromProposal = pre.from_proposal_id" in js and "from_proposal_id: NP.mode === 'new' ? NP.fromProposal : null" in js
    assert "!NP.fromProposal" in js, "the client check trusts the proposal's client"
    assert "$('np-type').disabled = !!" not in js and "$('np-borough').disabled = !!" not in js, "type and region stay editable"
    passed += 1; print("  page ok")

    print(f"OK smoke_projects_ready: {passed} checks")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        _cleanup()
    sys.exit(rc)
