#!/usr/bin/env python3
"""#308 — the client's Proposals tab: behavioral guard.

Boots the REAL app (server.app) in-process on an ISOLATED database + scratch data
root + the fake mail seam. Proves:

  * catalog: 'proposals' is grantable, served, in the client matrix row and in the
    FULL preset (and in no other); the registry dataset exists and is clean
  * shell: with the grant the client's /portal/<code> renders the Proposals nav +
    view; without it neither appears and the API is 403 (matrix ∩ grant, per request)
  * scoping: the list holds ONLY proposals of the organization that owns the client's
    bound project, only sent/viewed/signed/declined (never draft / superseded); another
    client's org sees nothing; a by-id view/pdf of a foreign proposal is 404; the URL's
    project code must be the bound one (403 otherwise)
  * roles: pm/super/estimator -> 403; admin without ?preview_client -> 403; admin
    preview -> the same list, but accept/decline -> 403 (read-only preview)
  * payload: every field is registry-registered; no token / path / reason / ip keys
  * view: opening the document flips sent -> viewed for the real client (not the
    preview); accept: missing name / unchecked box -> 400; accept -> signed, the #307
    trigger fires (estimate approved at the proposal price, COI task); second accept
    -> 409; decline path on another proposal -> declined + follow-up task

Run:  venv/bin/python tests/smoke_client_proposals_308.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")   # test-only seams are inert without this (runtime_seams.py)
import re
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_cprop_308_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ["SSC_MAIL_FAKE_DIR"] = str(Path(_TMP) / "mail")
os.environ["APP_BASE_URL"] = "http://test.local"
os.environ["SSC_STRICT_ROUTES"] = "1"
for k in ("SSC_TRUSTED_PROXY", "GOOGLE_OAUTH_CLIENT_ID"):
    os.environ.pop(k, None)

import server  # noqa: E402  (boots the whole app; strict routes would SystemExit on a duplicate)
import auth  # noqa: E402
import client_grants  # noqa: E402
import client_registry as reg  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import portal_matrix  # noqa: E402
import proposals  # noqa: E402

TAG = "cprop308"
PW = "ClientProposalsSmoke2026"
ROLES = ("c_suite", "admin", "pm", "super", "estimator", "client", "client2")
PROJ_A, PROJ_B = "C308-A", "C308-B"
NOW = datetime.now().isoformat(timespec="seconds")
TODAY = date.today().isoformat()


def _db():
    return db_layer.connect(pragma_fk=True)


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for role in ROLES:
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), "client" if role.startswith("client") else role, f"Smoke {role}"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        for name in ("Cprop308 Riverside Owners", "Cprop308 Other Corp"):
            conn.execute("DELETE FROM crm_organization WHERE name = ?", (name,))
        ids["org"] = crm.create_org(conn, name="Cprop308 Riverside Owners", relationship_type="client")
        ids["org2"] = crm.create_org(conn, name="Cprop308 Other Corp", relationship_type="client")
        for code, org in ((PROJ_A, ids["org"]), (PROJ_B, ids["org2"])):
            conn.execute("DELETE FROM projects WHERE project_code = ?", (code,))
            conn.execute("INSERT INTO projects (project_code, name, address, status, client_org_id, created_at, updated_at) "
                         "VALUES (?, ?, '51 Cprop308 Terrace', 'active', ?, ?, ?)", (code, f"Smoke {code}", org, NOW, NOW))
        for role, code in (("client", PROJ_A), ("client2", PROJ_B), ("pm", PROJ_A)):
            conn.execute("DELETE FROM pm_project_assignment WHERE user_id = ?", (ids[role],))
            conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_by, assigned_at) "
                         "VALUES (?, ?, ?, ?)", (ids[role], code, ids["admin"], NOW))
        for role, code in (("client", PROJ_A), ("client2", PROJ_B)):
            conn.execute("DELETE FROM client_section_grant WHERE user_id = ?", (ids[role],))
            for sec in ("progress", "proposals"):
                conn.execute("INSERT INTO client_section_grant (user_id, project_code, section, granted_by, granted_at) "
                             "VALUES (?, ?, ?, ?, ?)", (ids[role], code, sec, ids["admin"], NOW))
        conn.commit()
        # three estimates for the org: A (sent), B (sent, to decline), C (draft proposal only)
        for key, addr in (("estA", "51 Cprop308 Terrace"), ("estB", "52 Cprop308 Terrace"), ("estC", "53 Cprop308 Terrace")):
            e = estimates.create_estimate(conn, est_type="FR", borough="BX", client_org_id=ids["org"],
                                          building_address=addr, created_by=ids["admin"])
            ids[key] = e["id"]
        estimates.change_status(conn, ids["estA"], "scoping", actor_user_id=ids["admin"])
        estimates.change_status(conn, ids["estA"], "submitted", final_amount=61000, actor_user_id=ids["admin"])
        # a foreign org's sent proposal
        ef = estimates.create_estimate(conn, est_type="FR", borough="BX", client_org_id=ids["org2"],
                                       building_address="99 Cprop308 Other St", created_by=ids["admin"])
        ids["estF"] = ef["id"]
        for key, price, send in (("estA", None, True), ("estB", 9000, True), ("estC", 500, False), ("estF", 7000, True)):
            p = proposals.create_from_estimate(conn, ids[key], created_by=ids["admin"])
            if price:
                proposals.update(conn, p["id"], {"price": price}, actor=ids["admin"])
            if send:
                proposals.send(conn, p["id"], to_email="cprop308.client@example.com", to_name="Dana", actor=ids["admin"])
            ids["p_" + key] = p["id"]
        # a superseded version must never reach the client: version estB then keep v2 as draft
        v2 = proposals.new_version(conn, ids["p_estB"], actor=ids["admin"])
        proposals.send(conn, v2["id"], actor=ids["admin"])
        ids["p_estB_v2"] = v2["id"]
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Cprop308 %'").fetchall()]
        est_ids = [r["id"] for r in conn.execute("SELECT id FROM estimate WHERE building_address LIKE '%Cprop308%'").fetchall()]
        pids = [r["id"] for r in conn.execute(
            f"SELECT id FROM proposal WHERE estimate_id IN ({','.join('?' * len(est_ids)) or 'NULL'})", est_ids).fetchall()]
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate_document", "estimate_id", est_ids)
        purge(conn, "estimate", "id", est_ids)
        conn.execute("DELETE FROM building WHERE address_key LIKE '%cprop308%'")
        if org_ids:
            ph = ",".join("?" * len(org_ids))
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph})", org_ids)
            conn.execute(f"DELETE FROM crm_task WHERE entity_type = 'organization' AND entity_id IN ({ph})", org_ids)
        conn.execute("DELETE FROM notification_log WHERE subject LIKE '%Cprop308%'")
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        purge(conn, "client_section_grant", "user_id", ids)
        purge(conn, "pm_project_assignment", "user_id", ids)
        conn.execute("DELETE FROM audit_log WHERE target_type = 'user' AND target_id IN (%s)" % (",".join("?" * len(ids)) or "NULL"), [str(i) for i in ids])
        conn.commit()
        purge_users(conn, ids)
        conn.execute("DELETE FROM projects WHERE project_code IN (?, ?)", (PROJ_A, PROJ_B))
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Cprop308 %'")
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW},
               environ_base={"REMOTE_ADDR": "10.38.0.1"})
    assert r.status_code == 200, (role, r.status_code, r.get_data(as_text=True)[:200])
    return c


FORBIDDEN = ("token", "token_hash", "decline_reason", "signed_ip", "recipient_email", "estimate_code", "html")


def _registry_only(rows):
    allowed = reg.registered_fields("portal.proposal")
    for r in rows:
        extra = set(r) - allowed
        assert not extra, extra
        for k in r:
            assert not k.endswith("_path") and k not in FORBIDDEN, k


def main() -> int:
    # ---- catalog
    assert "proposals" in client_grants.SECTIONS and "proposals" in client_grants.SERVED_SECTIONS
    assert client_grants.SECTION_LABELS["proposals"] == "Proposals"
    assert "proposals" in client_grants.PRESETS["full"]
    assert all("proposals" not in v for k, v in client_grants.PRESETS.items() if k != "full")
    assert portal_matrix.allowed("client", "proposals") and not portal_matrix.allowed("architect", "proposals")
    assert reg.registered_fields("portal.proposal")
    reg.assert_registry_clean()
    ids = _seed()
    base = f"/api/portal/{PROJ_A}/proposals"
    passed = 0
    print("  catalog ok"); passed += 1

    # ---- roles + shell
    for role in ("pm", "super", "estimator"):
        assert _login(role).get(base).status_code == 403, role
    admin = _login("admin")
    assert admin.get(base).status_code == 403                       # no preview target
    client = _login("client")
    page = client.get(f"/portal/{PROJ_A}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'data-portal-nav="proposals"' in html and 'data-portal-view="proposals"' in html
    assert "PORTAL_SECTION" not in html
    # the other client has the grant too but a different org — and cannot address PROJ_A
    client2 = _login("client2")
    assert client2.get(base).status_code == 403                    # project binding
    r = client2.get(f"/api/portal/{PROJ_B}/proposals")
    assert r.status_code == 200 and [x["number"] for x in r.get_json()["data"]["proposals"]]  # org2's own
    org2_numbers = [x["number"] for x in r.get_json()["data"]["proposals"]]
    print("  roles + shell ok"); passed += 1

    # ---- grant removal hides nav + 403s the API (per request)
    conn = _db()
    try:
        conn.execute("DELETE FROM client_section_grant WHERE user_id = ? AND section = 'proposals'", (ids["client"],))
        conn.commit()
    finally:
        conn.close()
    assert client.get(base).status_code == 403
    html = client.get(f"/portal/{PROJ_A}").get_data(as_text=True)
    assert 'data-portal-nav="proposals"' not in html and 'data-portal-view="proposals"' not in html
    conn = _db()
    try:
        conn.execute("INSERT INTO client_section_grant (user_id, project_code, section, granted_by, granted_at) "
                     "VALUES (?, ?, 'proposals', ?, ?)", (ids["client"], PROJ_A, ids["admin"], NOW))
        conn.commit()
    finally:
        conn.close()
    print("  grant toggle ok"); passed += 1

    # ---- list: scoping + registry-only fields
    r = client.get(base)
    assert r.status_code == 200 and r.headers.get("Cache-Control", "").startswith("no-store")
    rows = r.get_json()["data"]["proposals"]
    _registry_only(rows)
    numbers = {x["number"] for x in rows}
    conn = _db()
    try:
        pA = proposals.get(conn, ids["p_estA"]); pB1 = proposals.get(conn, ids["p_estB"])
        pB2 = proposals.get(conn, ids["p_estB_v2"]); pC = proposals.get(conn, ids["p_estC"]); pF = proposals.get(conn, ids["p_estF"])
    finally:
        conn.close()
    assert pA["number"] in numbers and pB2["number"] in numbers
    assert pB1["number"] not in numbers, "superseded leaked"
    assert pC["number"] not in numbers, "draft leaked"
    assert pF["number"] not in numbers and pF["number"] in org2_numbers, "foreign org leaked"
    a = next(x for x in rows if x["number"] == pA["number"])
    assert a["status"] == "open" and a["can_accept"] is True and a["price"] == 61000 and a["building"]
    assert a["view_url"] == f"{base}/{pA['id']}/view" and a["pdf_url"] == f"{base}/{pA['id']}/pdf"
    assert r.get_json()["data"]["open_count"] == 2
    # foreign / draft / superseded by id -> 404
    for pid in (pF["id"], pC["id"], pB1["id"]):
        assert client.get(f"{base}/{pid}/view").status_code == 404, pid
        assert client.get(f"{base}/{pid}/pdf").status_code == 404, pid
        assert client.post(f"{base}/{pid}/accept", json={"name": "Dana Client", "agreed": True}).status_code == 404, pid
    print("  scoping + payload ok"); passed += 1

    # ---- admin preview: same list, read-only
    pv = admin.get(f"{base}?preview_client={ids['client']}")
    assert pv.status_code == 200
    assert {x["number"] for x in pv.get_json()["data"]["proposals"]} == numbers
    assert admin.get(f"{base}/{pA['id']}/view?preview_client={ids['client']}").status_code == 200
    conn = _db()
    try:
        assert proposals.get(conn, pA["id"])["status"] == "sent", "preview must not mark viewed"
    finally:
        conn.close()
    assert admin.post(f"{base}/{pA['id']}/accept?preview_client={ids['client']}",
                      json={"name": "Dana Client", "agreed": True}).status_code == 403
    assert admin.post(f"{base}/{pA['id']}/decline?preview_client={ids['client']}", json={}).status_code == 403
    print("  preview read-only ok"); passed += 1

    # ---- view flips to viewed; accept fires the trigger
    v = client.get(f"{base}/{pA['id']}/view")
    assert v.status_code == 200 and b"Accept this proposal" not in v.data and pA["number"].encode() in v.data
    conn = _db()
    try:
        assert proposals.get(conn, pA["id"])["status"] == "viewed"
    finally:
        conn.close()
    assert client.post(f"{base}/{pA['id']}/accept", json={"name": "D", "agreed": True}).status_code == 400
    assert client.post(f"{base}/{pA['id']}/accept", json={"name": "Dana Client", "agreed": False}).status_code == 400
    r = client.post(f"{base}/{pA['id']}/accept", json={"name": "Dana Client", "title": "Board president", "agreed": True},
                    environ_base={"REMOTE_ADDR": "203.0.113.38"})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()["data"]; _registry_only([d])
    assert d["status"] == "signed" and d["signed_name"] == "Dana Client" and d["can_accept"] is False
    assert client.post(f"{base}/{pA['id']}/accept", json={"name": "Dana Client", "agreed": True}).status_code == 409
    conn = _db()
    try:
        est = estimates.get_estimate(conn, ids["estA"])
        assert est["status"] == "approved" and est["final_amount"] == 61000
        assert proposals.get(conn, pA["id"])["signed_ip"] == "203.0.113.38"
        tasks = [t["title"] for t in crm.list_tasks(conn, status="open") if t["entity_id"] == ids["org"]]
        assert any(t.startswith("Collect Certificate of Insurance") for t in tasks), tasks
    finally:
        conn.close()
    rows = client.get(base).get_json()["data"]["proposals"]
    assert next(x for x in rows if x["number"] == pA["number"])["status_label"] == "Accepted"
    print("  view + accept + trigger ok"); passed += 1

    # ---- decline the other one
    r = client.post(f"{base}/{pB2['id']}/decline", json={"reason": "Board chose another bidder."})
    assert r.status_code == 200 and r.get_json()["data"]["status"] == "declined"
    assert client.post(f"{base}/{pB2['id']}/accept", json={"name": "Dana Client", "agreed": True}).status_code == 409
    conn = _db()
    try:
        tasks = [t["title"] for t in crm.list_tasks(conn, status="open") if t["entity_id"] == ids["org"]]
        assert any(t.startswith("Follow up on declined proposal") for t in tasks), tasks
        assert "reason" not in str(client.get(base).get_json()).lower().replace("_reason", "")  # belt: no reason text in payload
    finally:
        conn.close()
    assert client.get(base).get_json()["data"]["open_count"] == 0
    pdf = client.get(f"{base}/{pA['id']}/pdf")
    assert pdf.status_code in (200, 503)
    print("  decline + pdf ok"); passed += 1

    print(f"PASS smoke_client_proposals_308 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
