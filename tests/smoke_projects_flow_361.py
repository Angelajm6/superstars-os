#!/usr/bin/env python3
"""Projects navigation flow — behavioral guard.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * the job dashboard's back link goes to ALL PROJECTS for company roles (and stays "Back to
    Projects" for a pm), never only to the company dashboard
  * the console's Projects tab "+ New project" is a button that opens the shared form in place
    (no detour through /admin/projects), the module is loaded on both pages, and the Assignments
    page keeps working through the same module
  * Client portal lives under Clients (sub-tab bar on both views), not as a top-level tab
Run:  venv/bin/python tests/smoke_projects_flow_361.py
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_pflow_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "pflow"
PW = "ProjectModulesSmoke2026"
CORE_VIEWS = ("dashboard", "contacts", "documents", "field-photos", "daily-report", "schedule", "payments", "expenses", "settings")
FAILS = []


def ok(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for role in ("c_suite", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, ?)",
                         (email, auth.hash_password(PW), role, f"Smoke {role} Pflow", 0 if role == "pm" else 1))
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Pflow %'")
        ids["org"] = crm.create_org(conn, name="Pflow Owners", relationship_type="client")
        ids["pm"] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.pm@example.com",)).fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Pflow %'").fetchall()]
        ph = ",".join("?" * len(org_ids)) or "NULL"
        est_ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM estimate WHERE client_org_id IN ({ph}) OR building_address LIKE '% Pflow %'", org_ids).fetchall()]
        codes = {r["project_code"] for r in conn.execute(
            f"SELECT project_code FROM projects WHERE client_org_id IN ({ph}) OR address LIKE '% Pflow %'", org_ids).fetchall()}
        eph = ",".join("?" * len(est_ids)) or "NULL"
        codes |= {r["converted_project_code"] for r in conn.execute(
            f"SELECT converted_project_code FROM estimate WHERE id IN ({eph}) AND converted_project_code IS NOT NULL", est_ids).fetchall()}
        codes = list(codes)
        for t, col in (("pm_project_assignment", "project_code"), ("ira_visit", "project_code"), ("ira_job", "project_code")):
            purge(conn, t, col, codes)
        for t in ("ira_job", "estimate_document", "walkthrough_visit", "proposal_line", "proposal_event", "proposal"):
            purge(conn, t, "estimate_id", est_ids)
        purge(conn, "projects", "project_code", codes)
        purge(conn, "estimate", "id", est_ids)
        purge(conn, "building", "client_org_id", org_ids)
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
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code)
    return c


def main() -> int:
    ids = _seed()
    try:
        cs, pm = _login("c_suite"), _login("pm")
        r = cs.post("/api/admin/projects/onboard", json={"client_org_id": ids["org"], "est_type": "FR", "borough": "BX",
                                                         "billing_type": "lump_sum", "address": "1 Pflow Way"})
        ok("onboard ok", r.status_code == 201)
        code = r.get_json()["data"]["project_code"]
        conn = _db()
        try:
            conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_by, assigned_at) VALUES (?,?,?,?)",
                         (ids["pm"], code, ids["pm"], "2026-01-01T00:00:00"))
            conn.commit()
        finally:
            conn.close()

        # ---- job dashboard back link
        html = cs.get(f"/projects/{code}").get_data(as_text=True)
        ok("company roles: back link goes to all projects", 'href="/company#projects"' in html and "← All projects" in html)
        ok("company roles: the company dashboard is still one click away", 'href="/company#overview" class="bl-company"' in html and "← Back to Company" not in html)
        phtml = pm.get(f"/projects/{code}").get_data(as_text=True)
        ok("pm: back link still goes to their projects list", 'href="/projects"' in phtml and "← Back to Projects" in phtml)
        ok("pm: never offered the company console", 'href="/company#projects"' not in phtml)

        # ---- console Projects tab opens the form in place
        con = cs.get("/company").get_data(as_text=True)
        ok("console: + New project is a button, not a link to another page",
           'id="pj-new"' in con and 'href="/admin/projects?new=1" style="text-decoration:none">+ New project' not in con)
        ok("console loads the shared form", "new_project_modal.js" in con and "window.SSCNewProject.open()" in con)
        ok("console reacts to a created project", "ssc:project-created" in con)
        mod = cs.get("/files/static/js/new_project_modal.js")
        ok("the shared form is served", mod.status_code == 200 and "window.SSCNewProject" in mod.get_data(as_text=True))
        adm = cs.get("/admin/projects").get_data(as_text=True)
        ok("assignments page uses the same form", "new_project_modal.js" in adm and "SSCNewProject.open" in adm and 'id="np-form"' not in adm)
        ok("assignments page keeps Crew & site", 'id="cs-form"' in adm)

        # ---- Client portal under Clients
        ok("clients view has the sub-tab bar", re.search(r'id="view-clients" hidden>\s*<div class="ssc-subtabs" data-subtabs="clients">', con) is not None)
        ok("client portal view has the sub-tab bar", re.search(r'id="view-portal" hidden>\s*<div class="ssc-subtabs" data-subtabs="clients">', con) is not None)
        nav = (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")
        ok("no top-level Client portal tab", "key: 'portal'" not in nav)
        ok("Clients tab lights up on the portal hash", "['/company', 'portal']" in nav)
    finally:
        _cleanup()

    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s) failed")
        return 1
    print("PASS: projects navigation flow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
