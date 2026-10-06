#!/usr/bin/env python3
"""Edit projects and clients after they exist. Behavioral guard.

Boots the real app on an ISOLATED database. Proves:
  * GET /api/projects/<code>/details: internal roles (an unassigned pm is refused by the
    scoping hook), can_edit only for admin / c_suite; PUT is admin / c_suite only
  * validation refuses before anything is written (blank name, target before start, a
    non-number contract, unknown client) and a refused edit leaves no new client behind
  * a saved edit updates name / address / city / dates / contract value; a new address re-links
    the job AND its approved estimate to the right building; a new client moves the legacy
    client text, the estimate, and the building — unless another client still has a job there
  * an AIA job's contract is its schedule of values: editing it here is refused
  * every saved edit lands on the client timelines (old and new client), with no values logged
  * clients: a name can't be blanked; a client edit is logged on its timeline; contacts can be
    edited and removed (removed = inactive, hidden from the client page and its contact count)
  * the pages carry the edit UI (Settings → Project details; client page Edit client / contact)
Run:  venv/bin/python tests/smoke_edit_projects_clients.py
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_edit_pc_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "editpc"
PW = "EditProjects2026"


def _db():
    return db_layer.connect(pragma_fk=False)


def _orgs(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE ?", (f"{TAG}%",)).fetchall()]


def _purge(conn):
    codes = [r["project_code"] for r in conn.execute(
        "SELECT project_code FROM projects WHERE client_org_id IN (SELECT id FROM crm_organization WHERE name LIKE ?)", (f"{TAG}%",)).fetchall()]
    codes += [r["project_code"] for r in conn.execute("SELECT project_code FROM projects WHERE name LIKE ?", (f"{TAG}%",)).fetchall()]
    codes = sorted(set(codes))
    est = [r["id"] for r in conn.execute("SELECT id FROM estimate WHERE client_org_id IN (SELECT id FROM crm_organization WHERE name LIKE ?)", (f"{TAG}%",)).fetchall()]
    for t in ("billing_sov", "pm_project_assignment", "project_assignments"):
        purge(conn, t, "project_code", codes)
    purge(conn, "estimate", "converted_project_code", codes)
    purge(conn, "estimate", "id", est)
    purge(conn, "projects", "project_code", codes)
    bids = [r["id"] for r in conn.execute("SELECT id FROM building WHERE address_line LIKE ?", ("%Editpc%",)).fetchall()]
    purge(conn, "building", "id", bids)
    orgs = _orgs(conn)
    purge(conn, "crm_activity", "entity_id", orgs)
    purge(conn, "crm_contact", "org_id", orgs)
    purge(conn, "crm_organization", "id", orgs)


def _seed():
    conn = _db()
    try:
        _purge(conn)
        conn.commit()
        ids = {}
        for role in ("admin", "c_suite", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"{TAG} {role}"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        ids["org_a"] = crm.create_org(conn, name=f"{TAG} Owner A", relationship_type="client")
        ids["org_b"] = crm.create_org(conn, name=f"{TAG} Owner B", relationship_type="client")
        conn.commit()
        return ids
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


def _onboard(cs, org, address, **extra):
    r = cs.post("/api/admin/projects/onboard", json={"client_org_id": org, "est_type": "FR", "borough": "BX",
                                                     "billing_type": "lump_sum", "address": address, **extra})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["data"]["project_code"]


def _row(sql, *a):
    conn = _db()
    try:
        r = conn.execute(sql, a).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def main() -> int:
    ids = _seed()
    passed = 0
    admin, cs, pm = _login("admin"), _login("c_suite"), _login("pm")
    anon = server.app.test_client()
    A = _onboard(cs, ids["org_a"], "1 Editpc Way", name=f"{TAG} Job A", contract_value=100000)
    B = _onboard(cs, ids["org_a"], "9 Editpc Shared", name=f"{TAG} Job B")
    C = _onboard(cs, ids["org_b"], "9 Editpc Shared", name=f"{TAG} Job C")     # same building, other client
    url = lambda c: f"/api/admin/projects/{c}/details"                          # noqa: E731

    # ---- read + gates
    d = cs.get(f"/api/projects/{A}/details").get_json()["data"]
    assert d["can_edit"] is True and d["name"] == f"{TAG} Job A" and d["contract_value"] == 100000 and d["contract_editable"], d
    assert anon.get(f"/api/projects/{A}/details").status_code == 401
    assert pm.get(f"/api/projects/{A}/details").status_code == 403, "an unassigned pm is refused"
    conn = _db()
    try:
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, '2026-10-06')", (ids["pm"], A))
        conn.commit()
    finally:
        conn.close()
    assert pm.get(f"/api/projects/{A}/details").get_json()["data"]["can_edit"] is False
    assert pm.put(url(A), json={"name": "x"}).status_code == 403 and anon.put(url(A), json={"name": "x"}).status_code == 401
    assert cs.put(url("NOPE-EDIT-1"), json={"name": "x"}).status_code == 404
    passed += 1; print("  read + gates ok")

    # ---- validation refuses before anything is written
    for body, status, text in (({"name": "  "}, 400, "can't be blank"), ({"address": ""}, 400, "can't be blank"),
                               ({"start_date": "2026-11-01", "target_completion": "2026-10-01"}, 400, "before the start"),
                               ({"start_date": "11/01/2026"}, 400, "YYYY-MM-DD"), ({"contract_value": "lots"}, 400, "number"),
                               ({"contract_value": -5}, 400, "out of range"), ({"client_org_id": 999999999}, 404, "not found")):
        r = cs.put(url(A), json={**body, "new_org_name": f"{TAG} Orphan"} if status == 400 else body)
        assert r.status_code == status and text in r.get_json()["error"], (body, r.status_code, r.get_json())
    assert not _row("SELECT id FROM crm_organization WHERE name = ?", f"{TAG} Orphan"), "a refused edit creates no client"
    assert cs.get(f"/api/projects/{A}/details").get_json()["data"]["name"] == f"{TAG} Job A", "nothing changed"
    passed += 1; print("  validation ok")

    # ---- a saved edit: name / address / city / dates / contract; building + estimate follow the address
    buf = io.StringIO(); h = logging.StreamHandler(buf); logging.getLogger().addHandler(h)
    try:
        r = admin.put(url(A), json={"name": f"{TAG} Harbor View", "address": "250 Editpc Harbor Ave", "city_zip": "Bronx, NY 10454",
                                    "start_date": "2026-10-12", "target_completion": "2027-03-31", "contract_value": "$185,000"})
    finally:
        logging.getLogger().removeHandler(h)
    assert r.status_code == 200, r.get_json()
    d = r.get_json()["data"]
    assert (d["name"], d["address"], d["city_zip"], d["start_date"], d["target_completion"], d["contract_value"]) == \
        (f"{TAG} Harbor View", "250 Editpc Harbor Ave", "Bronx, NY 10454", "2026-10-12", "2027-03-31", 185000), d
    assert "Harbor View" not in buf.getvalue() and "185" not in buf.getvalue(), "values never go to the log"
    p = _row("SELECT building_id FROM projects WHERE project_code = ?", A)
    b = _row("SELECT address_line FROM building WHERE id = ?", p["building_id"])
    e = _row("SELECT building_id, final_amount FROM estimate WHERE converted_project_code = ?", A)
    assert b["address_line"] == "250 Editpc Harbor Ave" and e["building_id"] == p["building_id"] and float(e["final_amount"]) == 185000
    passed += 1; print("  saved edit ok")

    # ---- a new client: legacy text, estimate and building move; a shared building stays
    r = cs.put(url(A), json={"new_org_name": f"{TAG} Owner C"})
    assert r.status_code == 200 and r.get_json()["data"]["client_name"] == f"{TAG} Owner C", r.get_json()
    new_c = r.get_json()["data"]["client_org_id"]
    assert _row("SELECT client FROM projects WHERE project_code = ?", A)["client"] == f"{TAG} Owner C"
    assert _row("SELECT client_org_id FROM estimate WHERE converted_project_code = ?", A)["client_org_id"] == new_c
    assert _row("SELECT b.client_org_id FROM building b JOIN projects p ON p.building_id = b.id WHERE p.project_code = ?", A)["client_org_id"] == new_c
    assert cs.put(url(A), json={"new_org_name": f"{TAG} owner   c"}).get_json()["data"]["client_org_id"] == new_c, "typing an existing client reuses it"
    shared_before = _row("SELECT b.id, b.client_org_id FROM building b JOIN projects p ON p.building_id = b.id WHERE p.project_code = ?", B)
    assert cs.put(url(B), json={"client_org_id": ids["org_b"]}).status_code == 200
    shared_after = _row("SELECT client_org_id FROM building WHERE id = ?", shared_before["id"])
    assert shared_after["client_org_id"] == ids["org_b"], "B and C now both belong to Owner B -> the building follows"
    assert cs.put(url(C), json={"client_org_id": ids["org_a"]}).status_code == 200
    assert _row("SELECT client_org_id FROM building WHERE id = ?", shared_before["id"])["client_org_id"] == ids["org_b"], \
        "B (Owner B) still has a job there -> the building does not move with C"
    passed += 1; print("  client move ok")

    # ---- the timeline + the AIA rule
    conn = _db()
    try:
        tl_a = [x["summary"] for x in crm.list_activity(conn, entity_type="organization", entity_id=ids["org_a"])]
        tl_c = [x["summary"] for x in crm.list_activity(conn, entity_type="organization", entity_id=new_c)]
        conn.execute("INSERT INTO billing_sov (project_code, item_no, description, scheduled_value, kind, sort, created_at, updated_at) "
                     "VALUES (?, '1', 'Base', 50000, 'base', 1, '2026-10-06', '2026-10-06')", (C,))
        conn.commit()
    finally:
        conn.close()
    assert any(f"Project {A} updated: name, address" in s for s in tl_a) and any(f"Project {A} updated: client" in s for s in tl_a), tl_a
    assert any(f"Project {A} updated: client" in s for s in tl_c), tl_c
    d = cs.get(f"/api/projects/{C}/details").get_json()["data"]
    assert d["contract_source"] == "schedule_of_values" and d["contract_editable"] is False and d["contract_value"] == 50000
    r = cs.put(url(C), json={"contract_value": 1})
    assert r.status_code == 400 and "schedule of values" in r.get_json()["error"], r.get_json()
    passed += 1; print("  timeline + AIA rule ok")

    # ---- clients and contacts
    org = ids["org_b"]
    r = cs.put(f"/api/crm/organizations/{org}", json={"name": "  "})
    assert r.status_code == 400 and "can't be blank" in r.get_json()["error"], r.get_json()
    assert cs.put(f"/api/crm/organizations/{org}", json={"name": f"{TAG} Owner B Renamed", "notes": "Board meets Tuesdays"}).status_code == 200
    det = cs.get(f"/api/company/portal/clients/{org}").get_json()["data"]
    assert det["organization"]["name"] == f"{TAG} Owner B Renamed"
    assert any(x.get("summary") == "Client details updated: name, notes" for x in det["activity"]), det["activity"]
    assert pm.put(f"/api/crm/organizations/{org}", json={"name": "x"}).status_code == 403
    ct = cs.post("/api/crm/contacts", json={"full_name": f"{TAG} Contact", "org_id": org, "email": "c@example.com"}).get_json()["data"]
    cid = (ct.get("contact") or ct)["id"]
    assert cs.put(f"/api/crm/contacts/{cid}", json={"full_name": ""}).status_code == 400
    assert cs.put(f"/api/crm/contacts/{cid}", json={"status": "gone"}).status_code == 400
    assert cs.put(f"/api/crm/contacts/{cid}", json={"title": "Board president"}).status_code == 200
    det = cs.get(f"/api/company/portal/clients/{org}").get_json()["data"]
    assert [c["title"] for c in det["contacts"]] == ["Board president"]
    assert cs.put(f"/api/crm/contacts/{cid}", json={"status": "inactive"}).status_code == 200
    det = cs.get(f"/api/company/portal/clients/{org}").get_json()["data"]
    assert det["contacts"] == [], "a removed contact leaves the client page"
    rows = {r["id"]: r for r in cs.get("/api/company/portal/clients?type=client&search=" + TAG).get_json()["data"]}
    assert rows[org]["contacts"] == 0, "and its contact count"
    passed += 1; print("  clients + contacts ok")

    # ---- the pages
    dash = cs.get(f"/projects/{A}").get_data(as_text=True)
    assert 'id="ps-det-form"' in dash and "/details'" in dash and 'id="ps-det-edit"' in dash
    cp = cs.get("/company").get_data(as_text=True)
    for needle in ('id="ce-form"', 'id="cl-edit"', 'id="ct-remove"', "data-ct-edit", "openContactModal"):
        assert needle in cp, needle
    passed += 1; print("  pages ok")

    print(f"PASS smoke_edit_projects_clients ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_edit_projects_clients: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
