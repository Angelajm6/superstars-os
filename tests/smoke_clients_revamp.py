#!/usr/bin/env python3
"""Clients revamp (2026-10-28) — the client is whoever pays us. Behavioral guard.

  * schema: crm_organization.client_kind + billing defaults, crm_contact.roles / is_primary; the
    migration is in the ledger list and boot-ensured
  * Add client: the kind is required and drives the billing defaults (a GC gets pay apps, 10%
    retainage, lien waivers), overrides win, a name already there is refused (409), the main
    contact keeps its roles, a board's managing agent is found by name or added to the Directory
  * the Clients list = clients + leads, each with its kind, main contact, what it owes / what's
    overdue (sent invoices), retainage held and next follow-up; search finds people and job numbers
  * the Directory = everyone else, grouped, a managing agent listing the boards it runs
  * Edit client sets the kind (bad kind refused) and logs it; contacts get roles + one main contact
  * gates: pm 403, anonymous 401; the page carries the new list, Directory and 3-step Add client

Run:  venv/bin/python tests/smoke_clients_revamp.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_clients_rv_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import migrations  # noqa: E402
import preprod_fixture_bootstrap as pfb  # noqa: E402
import server  # noqa: E402  (boots the whole app)
from apply_crm_266 import _columns  # noqa: E402

TAG = "clrv"
PW = "ClientsRevamp2026"
API = "/api/company/portal/clients"


def _db():
    return db_layer.connect(pragma_fk=False)


def _purge(conn):
    orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE LOWER(name) LIKE ?", (f"{TAG}%",)).fetchall()]
    codes = [r["project_code"] for r in conn.execute(
        f"SELECT project_code FROM projects WHERE client_org_id IN ({','.join('?' * len(orgs)) or 'NULL'})", orgs).fetchall()] if orgs else []
    for t in ("billing_sov", "pm_project_assignment", "project_assignments", "project_contact"):
        purge(conn, t, "project_code", codes)
    purge(conn, "estimate", "converted_project_code", codes)
    purge(conn, "estimate", "client_org_id", orgs)
    purge(conn, "projects", "project_code", codes)
    bids = [r["id"] for r in conn.execute("SELECT id FROM building WHERE LOWER(address_line) LIKE ?", (f"%{TAG}%",)).fetchall()]
    purge(conn, "building", "id", bids)
    purge(conn, "invoice", "client_org_id", orgs)
    purge(conn, "crm_task", "entity_id", orgs)
    purge(conn, "crm_activity", "entity_id", orgs)
    purge(conn, "crm_contact", "org_id", orgs)
    conn.execute(f"UPDATE crm_organization SET managing_agent_org_id = NULL WHERE LOWER(name) LIKE ?", (f"{TAG}%",))
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


def _org(oid):
    conn = _db()
    try:
        return dict(conn.execute("SELECT * FROM crm_organization WHERE id = ?", (oid,)).fetchone())
    finally:
        conn.close()


def main() -> int:
    _seed()
    passed = 0
    cs, pm, anon = _login("c_suite"), _login("pm"), server.app.test_client()

    # ---- schema + ledger
    conn = _db()
    try:
        assert set(crm.CLIENT_PROFILE_FIELDS) <= _columns(conn, "crm_organization")
        assert {"roles", "is_primary"} <= _columns(conn, "crm_contact")
        assert crm.ensure_client_profile(conn) is False, "idempotent: a second run changes nothing"
    finally:
        conn.close()
    assert "apply_client_profile.py" in pfb.DUAL_BACKEND_MIGRATIONS
    assert migrations.BOOT_ENSURED["apply_client_profile.py"] == ("crm_organization.client_kind", "crm_contact.roles")
    passed += 1; print("  schema ok")

    # ---- Add client: validation + kind defaults
    for body, code, text in (({"name": f"{TAG} X"}, 400, "kind"), ({"name": " ", "client_kind": "gc"}, 400, "name"),
                             ({"name": f"{TAG} X", "client_kind": "boss"}, 400, "kind"),
                             ({"name": f"{TAG} X", "client_kind": "gc", "contact": {"full_name": "A", "email": "nope"}}, 400, "email"),
                             ({"name": f"{TAG} X", "client_kind": "gc", "billing": {"retainage_pct": 150}}, 400, "between")):
        r = cs.post(API, json=body)
        assert r.status_code == code and text in r.get_json()["error"], (body, r.status_code, r.get_json())
    conn = _db()
    try:
        assert not conn.execute("SELECT id FROM crm_organization WHERE name = ?", (f"{TAG} X",)).fetchone(), "a refused add writes nothing"
    finally:
        conn.close()
    r = cs.post(API, json={"name": f"{TAG} Meridian Builders", "client_kind": "gc",
                           "contact": {"full_name": f"{TAG} Renee", "title": "PM", "phone": "(212) 555-0193",
                                       "email": "renee@example.com", "roles": ["pm", "bogus"]},
                           "billing": {"payment_terms_days": 45, "coi_wording": "Meridian; the owner"}})
    assert r.status_code == 200, r.get_json()
    gc = r.get_json()["data"]["id"]
    o = _org(gc)
    assert o["relationship_type"] == "client" and o["client_kind"] == "gc"
    assert (o["retainage_pct"], o["pay_apps"], o["lien_waivers"], o["deposit_pct"]) == (10, 1, 1, 0), "GC defaults"
    assert o["payment_terms_days"] == 45 and o["coi_wording"] == "Meridian; the owner", "overrides win"
    r = cs.post(API, json={"name": f"{TAG.upper()} MERIDIAN BUILDERS", "client_kind": "owner"})
    assert r.status_code == 409 and "already in" in r.get_json()["error"], "same name in any case is refused"
    home = cs.post(API, json={"name": f"{TAG} Rivera", "client_kind": "home", "lead": True}).get_json()["data"]["id"]
    o = _org(home)
    assert o["relationship_type"] == "lead" and o["deposit_pct"] == 25 and o["payment_terms_days"] == 0 and o["capital_improvement"] == 0
    passed += 1; print("  add client ok")

    # ---- a board and its managing agent (found by name, any case, or added to the Directory)
    b1 = cs.post(API, json={"name": f"{TAG} Park Terrace Owners Corp", "client_kind": "board",
                            "managing_agent": f"{TAG} Ellison PM", "contact": {"full_name": f"{TAG} Andre", "roles": ["agent"]}}).get_json()["data"]["id"]
    b2 = cs.post(API, json={"name": f"{TAG} Linden Hall Condo", "client_kind": "board", "managing_agent": f"{TAG} ELLISON pm"}).get_json()["data"]["id"]
    agent = _org(b1)["managing_agent_org_id"]
    assert agent and _org(b2)["managing_agent_org_id"] == agent, "the second board reuses the agent"
    assert _org(agent)["relationship_type"] == "property_manager"
    r = cs.post(API, json={"name": f"{TAG} Self", "client_kind": "board", "managing_agent": f"{TAG} self"})
    assert r.status_code == 400
    passed += 1; print("  managing agent ok")

    # ---- the list: kind, main contact, money, follow-ups, search
    today = date.today()
    conn = _db()
    try:
        now = today.isoformat() + "T09:00:00"
        for n, (st, amt, paid, ret, due) in enumerate((("issued", 48600, 0, 5400, (today - timedelta(days=4)).isoformat()),
                                                      ("issued", 27900, 0, 3100, (today + timedelta(days=20)).isoformat()),
                                                      ("paid", 52200, 52200, 5800, (today - timedelta(days=40)).isoformat()),
                                                      ("draft", 9999, 0, 0, None))):
            conn.execute("INSERT INTO invoice (number, kind, status, client_org_id, amount, retainage, amount_paid, issued_at, due_at, created_at, updated_at) "
                         "VALUES (?, 'progress', ?, ?, ?, ?, ?, ?, ?, ?, ?)", (f"INV-CLRV-{n}", st, gc, amt, ret, paid, now, due, now, now))
        conn.commit()
        crm.create_task(conn, title=f"{TAG} late one", entity_type="organization", entity_id=gc, due_date=(today - timedelta(days=1)).isoformat())
        crm.create_task(conn, title=f"{TAG} later one", entity_type="organization", entity_id=gc, due_date=(today + timedelta(days=9)).isoformat())
    finally:
        conn.close()
    code = cs.post("/api/admin/projects/onboard", json={"client_org_id": gc, "est_type": "FR", "borough": "BK", "billing_type": "lump_sum",
                                                        "address": f"88 {TAG} Ninth St", "name": f"{TAG} masonry"}).get_json()["data"]["project_code"]
    rows = {r["id"]: r for r in cs.get(API + "?type=clients&search=" + TAG).get_json()["data"]}
    assert set(rows) == {gc, home, b1, b2}, "clients + leads, never the agent"
    g = rows[gc]
    assert g["client_kind"] == "gc" and g["kind_label"] == "General contractor"
    assert g["owed"] == 76500 and g["overdue"] == 48600 and g["overdue_days"] == 4, g
    assert g["retainage_held"] == 14300, "issued + paid, not drafts"
    assert g["next_followup"]["title"] == f"{TAG} late one" and g["late_followups"] == 1
    assert g["main_contact"]["name"] == f"{TAG} Renee" and g["main_contact"]["phone"] == "(212) 555-0193"
    assert rows[b1]["managing_agent"] == f"{TAG} Ellison PM" and rows[home]["owed"] == 0 and rows[home]["next_followup"] is None
    for q, want in (("renee", gc), (code.lower(), gc), ("ninth st", gc), ("andre", b1)):
        got = [r["id"] for r in cs.get(API + "?type=clients&search=" + q).get_json()["data"] if r["name"].lower().startswith(TAG)]
        assert got == [want], (q, got)
    assert cs.get(API + "?type=client&search=" + TAG).get_json()["data"].__len__() == 3, "type=client still means clients only"
    passed += 1; print("  list ok")

    # ---- the Directory
    d = {r["id"]: r for r in cs.get(API + "?type=directory&search=" + TAG).get_json()["data"]}
    assert set(d) == {agent}, d.keys()
    assert d[agent]["group"] == "Managing agents and property managers"
    assert sorted(m["id"] for m in d[agent]["manages"]) == sorted([b1, b2])
    passed += 1; print("  directory ok")

    # ---- Edit client: kind + billing; contacts: roles + one main contact
    u = lambda oid, body: cs.put(f"/api/crm/organizations/{oid}", json=body)  # noqa: E731
    assert u(gc, {"client_kind": "nope"}).status_code == 400
    assert u(gc, {"client_kind": "cm", "retainage_pct": 5}).status_code == 200
    o = _org(gc)
    assert o["client_kind"] == "cm" and o["retainage_pct"] == 5
    det = cs.get(f"{API}/{gc}").get_json()["data"]
    assert det["organization"]["client_kind"] == "cm" and det["organization"]["pay_apps"] == 1
    assert any("kind" in (a["summary"] or "") and "billing setup" in (a["summary"] or "") for a in det["activity"]), det["activity"]
    assert u(gc, {"client_kind": None}).status_code == 200 and _org(gc)["client_kind"] is None, "the kind can be cleared"
    c2 = cs.post("/api/crm/contacts", json={"org_id": gc, "full_name": f"{TAG} AP", "roles": "billing", "is_primary": True}).get_json()["data"]["id"]
    det = {c["full_name"]: c for c in cs.get(f"{API}/{gc}").get_json()["data"]["contacts"]}
    assert det[f"{TAG} AP"]["is_primary"] == 1 and det[f"{TAG} Renee"]["is_primary"] == 0, "one main contact"
    assert det[f"{TAG} Renee"]["roles"] == "pm", "unknown roles are dropped"
    assert cs.put(f"/api/crm/contacts/{c2}", json={"roles": ["billing", "signs"]}).status_code == 200
    assert {c["full_name"]: c for c in cs.get(f"{API}/{gc}").get_json()["data"]["contacts"]}[f"{TAG} AP"]["roles"] == "billing,signs"
    passed += 1; print("  edit ok")

    # ---- gates
    assert pm.post(API, json={"name": f"{TAG} Nope", "client_kind": "gc"}).status_code == 403
    assert anon.post(API, json={"name": f"{TAG} Nope", "client_kind": "gc"}).status_code == 401
    assert pm.get(API + "?type=directory").status_code == 403 and anon.get("/api/company/portal/client-kinds").status_code == 401
    k = cs.get("/api/company/portal/client-kinds").get_json()["data"]
    assert set(k["kinds"]) == set(crm.BILLING_DEFAULTS) and k["defaults"]["gc"]["retainage_pct"] == 10
    passed += 1; print("  gates ok")

    # ---- the page
    page = (ROOT / "company_portal.html").read_text(encoding="utf-8")
    for needle in ('id="cl-sum"', 'id="cl-chips"', 'id="cl-rows"', 'id="view-directory"', 'id="dir-search"', 'id="cl-kinds"',
                   'id="cl-roles"', 'id="ce-kind"', "data-cv-open", "data-dir-open", "directory: loadDirectory", "directory: 'Clients'",
                   "api('/clients', { method: 'POST'", "Set the kind of client", "href=\"tel:"):
        assert needle in page, needle
    for gone in ('id="cl-type"', 'id="cl-inactive"', 'id="cl-table"', "$('cl-rel')"):
        assert gone not in page, gone
    nav = (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")
    assert "['Directory', '/company#directory']" in nav
    passed += 1; print("  page ok")

    print(f"PASS smoke_clients_revamp ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_clients_revamp: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
