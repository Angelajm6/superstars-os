#!/usr/bin/env python3
"""#357 — project contacts (ownership group, property managers, consultants, site supervisors). Guard.

Boots the real app on an ISOLATED database. Proves:
  * ensure() is idempotent
  * add: pick an existing CRM contact; a NEW person + NEW company (the company gets the
    role's relationship type); one of our workers as site supervisor
  * validation: bad role, consultant without discipline, our worker on a non-supervisor
    role, both/neither person, an unknown contact -> 400; duplicate -> 409; nothing is
    created in the CRM when the request is refused
  * one main contact per role and side; making another main clears the first; removing
    the main promotes the next; removal is soft (the CRM person stays)
  * access: assigned pm / super read (with phone + email), unassigned pm 403, pm / super
    writes 403, c_suite writes; the lookup never returns email or phone; nothing personal
    is logged
  * documents print the live people: DCR superintendent = our main supervisor, project
    manager = the assigned PM, client = the client company
  * the CRM accepts the new relationship types; the page carries the Contacts view
Run:  venv/bin/python tests/smoke_project_contacts_357.py
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_pc_357_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import project_contacts  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "pc357"
PW = "ProjectContacts2026"
P, P2 = "PC357-P", "PC357-Q"
EMP = ("E-93570", "W-9570")
T = date.today().isoformat()
SECRET_PHONE, SECRET_EMAIL = "555-0100-357", "pm.secret357@example.com"


def _db():
    return db_layer.connect(pragma_fk=False)


def _purge(conn):
    purge(conn, "project_contact", "project_code", [P, P2])
    cids = [r["id"] for r in conn.execute("SELECT id FROM crm_contact WHERE full_name LIKE ?", (f"{TAG}%",)).fetchall()]
    purge(conn, "crm_contact", "id", cids)
    oids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE ?", (f"{TAG}%",)).fetchall()]
    purge(conn, "pm_project_assignment", "project_code", [P, P2])
    purge(conn, "projects", "project_code", [P, P2])
    purge(conn, "crm_organization", "id", oids)
    purge(conn, "employees", "employee_id", [EMP[0]])


def _seed():
    conn = _db()
    try:
        _purge(conn)
        org = crm.create_org(conn, name=f"{TAG} Owner LLC", relationship_type="client")
        conn.execute("INSERT INTO projects (project_code, name, address, status, client_org_id) VALUES (?, 'Pc357', '1 Pc St', 'active', ?)", (P, org))
        conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, 'Pc357 other', '2 Pc St', 'active')", (P2,))
        conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at) VALUES (?, ?, ?, 'foreman', ?, ?)",
                     (EMP[0], EMP[1], f"{TAG} Foreman", T, T))
        ids = {}
        for role in ("admin", "c_suite", "pm", "super"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"{TAG} {role} Person"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, ?)", (ids["pm"], P, T))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, ?)", (ids["super"], P, T))
        conn.commit()
        pmc = crm.create_contact(conn, full_name=f"{TAG} Pat Manager", org_id=None, email=SECRET_EMAIL, phone=SECRET_PHONE,
                                 title="Property manager", relationship_type="property_manager")
        return org, pmc
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


def _count(sql, args=()):
    conn = _db()
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def main() -> int:
    org, pm_contact = _seed()
    passed = 0
    cs, pm, sup = _login("c_suite"), _login("pm"), _login("super")
    API = f"/api/projects/{P}/contacts"
    buf = io.StringIO(); h = logging.StreamHandler(buf); logging.getLogger().addHandler(h)
    try:
        # ---- ensure idempotent
        conn = _db()
        try:
            project_contacts.ensure(conn); project_contacts.ensure(conn)
        finally:
            conn.close()

        # ---- validation (nothing created in the CRM when refused)
        n_c = _count("SELECT COUNT(*) FROM crm_contact"); n_o = _count("SELECT COUNT(*) FROM crm_organization")
        bad = [({"role": "janitor", "contact_id": pm_contact}, 400),
               ({"role": "consultant", "contact_id": pm_contact}, 400),
               ({"role": "owner", "employee_id": EMP[0]}, 400),
               ({"role": "owner"}, 400),
               ({"role": "owner", "contact_id": pm_contact, "employee_id": EMP[0]}, 400),
               ({"role": "owner", "contact_id": 99999999}, 400),
               ({"role": "consultant", "discipline": "architect", "new_contact": {"full_name": ""}, "new_org": f"{TAG} Arch"}, 400)]
        for body, code in bad:
            r = cs.post(API, json=body)
            assert r.status_code == code, (body, r.status_code, r.get_json())
        assert _count("SELECT COUNT(*) FROM crm_contact") == n_c and _count("SELECT COUNT(*) FROM crm_organization") == n_o
        passed += 1; print("  validation ok")

        # ---- add: pick existing, new person + new company, our worker
        r = cs.post(API, json={"role": "property_manager", "contact_id": pm_contact})
        assert r.status_code == 201, r.get_json()
        assert cs.post(API, json={"role": "property_manager", "contact_id": pm_contact}).status_code == 409
        r = cs.post(API, json={"role": "consultant", "discipline": "structural_engineer",
                               "new_contact": {"full_name": f"{TAG} Sam Engineer", "title": "PE", "email": "sam357@example.com", "phone": "555-0199"},
                               "new_org": f"{TAG} Structures PC"})
        assert r.status_code == 201, r.get_json()
        conn = _db()
        try:
            rt = conn.execute("SELECT relationship_type FROM crm_organization WHERE name = ?", (f"{TAG} Structures PC",)).fetchone()[0]
        finally:
            conn.close()
        assert rt == "engineer", rt
        r = cs.post(API, json={"role": "site_supervisor", "employee_id": EMP[0], "title_on_job": "Superintendent"})
        assert r.status_code == 201, r.get_json()
        d = r.get_json()["data"]
        assert d["team"]["supervisors"][0]["name"] == f"{EMP[1]} — {TAG} Foreman" and d["team"]["supervisors"][0]["is_primary"]
        assert "phone" not in d["team"]["supervisors"][0], "an employee's phone is never shown"
        cons = d["groups"]["consultant"][0]
        assert cons["discipline_label"] == "Structural engineer" and cons["org"] == f"{TAG} Structures PC" and cons["phone"] == "555-0199"
        passed += 1; print("  add ok")

        # ---- primary rules + soft remove
        r = cs.post(API, json={"role": "property_manager", "new_contact": {"full_name": f"{TAG} Bea Super", "title": "Building super"}})
        pmg = r.get_json()["data"]["groups"]["property_manager"]
        first = next(x for x in pmg if x["name"] == f"{TAG} Pat Manager"); second = next(x for x in pmg if x["name"] == f"{TAG} Bea Super")
        assert first["is_primary"] and not second["is_primary"]
        pmg = cs.patch(f"{API}/{second['id']}", json={"is_primary": True}).get_json()["data"]["groups"]["property_manager"]
        assert [x["is_primary"] for x in pmg if x["id"] == first["id"]] == [False] and [x["is_primary"] for x in pmg if x["id"] == second["id"]] == [True]
        pmg = cs.delete(f"{API}/{second['id']}").get_json()["data"]["groups"]["property_manager"]
        assert len(pmg) == 1 and pmg[0]["id"] == first["id"] and pmg[0]["is_primary"], "the next one is promoted"
        assert _count("SELECT COUNT(*) FROM crm_contact WHERE full_name = ?", (f"{TAG} Bea Super",)) == 1, "the CRM person stays"
        assert _count("SELECT COUNT(*) FROM project_contact WHERE id = ? AND removed_at IS NOT NULL", (second["id"],)) == 1
        passed += 1; print("  primary + remove ok")

        # ---- access
        for who in (pm, sup):
            r = who.get(API)
            assert r.status_code == 200, r.status_code
            pmx = r.get_json()["data"]["groups"]["property_manager"][0]
            assert pmx["phone"] == SECRET_PHONE and pmx["email"] == SECRET_EMAIL, "internal roles can call / email"
            assert who.post(API, json={"role": "owner", "contact_id": pm_contact}).status_code == 403
            assert who.patch(f"{API}/{first['id']}", json={"is_primary": True}).status_code == 403
            assert who.get(f"{API}/lookup?q=pc357").status_code == 403
        assert pm.get(f"/api/projects/{P2}/contacts").status_code == 403, "unassigned pm"
        look = cs.get(f"{API}/lookup?q={TAG}&kind=contact").get_json()["data"]
        assert look and all("email" not in x and "phone" not in x for x in look), look
        assert any(x["name"].endswith("Foreman") for x in cs.get(f"{API}/lookup?q={TAG}&kind=employee").get_json()["data"])
        passed += 1; print("  access ok")

        # ---- the DCR header prints the live people
        import dcr_aggregator
        dcr = dcr_aggregator.aggregate_dcr(P, T, "internal")
        proj = dcr["project"]
        assert proj["superintendent"] == f"{TAG} Foreman", proj
        assert proj["project_manager"] == f"{TAG} pm Person", proj
        assert proj["owner_client"] == f"{TAG} Owner LLC", proj
        assert dcr["signoff"]["pm_name"] == f"{TAG} pm Person"
        passed += 1; print("  DCR header ok")

        # ---- CRM types + page
        conn = _db()
        try:
            oid = crm.create_org(conn, name=f"{TAG} PM Co", relationship_type="property_manager")
            assert oid
        finally:
            conn.close()
        page = cs.get(f"/projects/{P}").get_data(as_text=True)
        assert '<a class="nav-link" data-view="contacts">' in page and '<section class="view" data-view="contacts">' in page
        assert '<section class="view" data-view="contacts">' in pm.get(f"/projects/{P}").get_data(as_text=True)
        passed += 1; print("  CRM types + page ok")
    finally:
        logging.getLogger().removeHandler(h)
    logs = buf.getvalue()
    assert SECRET_PHONE not in logs and SECRET_EMAIL not in logs, "contact phone / email must never be logged"
    print(f"PASS smoke_project_contacts_357 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_project_contacts_357: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
