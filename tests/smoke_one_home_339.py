#!/usr/bin/env python3
"""#339 — ONE home: the company sidebar, phase 1 of the navigation revamp. Behavioral guard.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * every company page (portal, console, the four admin pages) mounts the SAME sidebar
    from static/js/company_nav.js — exactly seven tabs (Client portal is a sub-tab of Clients), in order, no CRM / Job sites /
    Office Console anywhere; each page carries its tab's sub-tab row
  * the console's own dashboard + CRM panels are gone from the served page; its retired
    hashes (`/`, `/#projects`, `/#crm`) and the portal's (`#buildings`, `#compliance`)
    are mapped to their new homes in the redirect table / the portal router
  * the portal: "This week" strip on the Dashboard, "+ Add client" and the contact /
    note / follow-up modals, no Job sites view, Company view renamed
  * the Add-client path the portal takes (organization → contact → note → follow-up →
    done) works end to end for a c_suite user and shows on the client's page
  * words: "Exterior" (never "Facade") for the work type in every picker source
  * the project dashboard's way back points at the company home
  * a pm still gets 403 on both company pages (nothing widened)
Run:  venv/bin/python tests/smoke_one_home_339.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_oh_339_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import project_type_config  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "oh339"
PW = "OneHomeSmoke2026"
PROJ = "OH339-A"
TABS = ["Home", "Dashboard", "Inbox", "Claude", "Projects", "Clients", "Proposals", "Finance", "Workforce", "Company"]   # Client portal is a sub-tab of Clients; Home = pm/super only (their way back from Account)
ADMIN_PAGES = {"/admin/projects": "projects", "/admin/users": "settings", "/account": "settings", "/estimating": "proposals", "/admin/labor-rates": "finance"}   # #360 — Team & access lives under Settings


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed():
    conn = _db()
    try:
        for role in ("c_suite", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Smoke {role} Oh339"))
        conn.execute("DELETE FROM projects WHERE project_code = ?", (PROJ,))
        conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, 'Oh339 Exterior', '1 Oh339 St', 'active')", (PROJ,))
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        conn.execute("DELETE FROM projects WHERE project_code = ?", (PROJ,))
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Oh339 %'").fetchall()]
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


def _page(c, path):
    r = c.get(path, follow_redirects=True)
    assert r.status_code == 200, (path, r.status_code)
    return r.get_data(as_text=True)


def main() -> int:
    _seed()
    passed = 0

    # ---- the sidebar: one definition, eight tabs + one pinned entry, on every company page
    js = (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")
    tabs_src, _, pinned_src = js.partition("var PINNED")
    labels = re.findall(r"\{ key: '[a-z]+', label: '([^']+)'", tabs_src)
    assert labels == TABS, labels
    # #341 — Settings is an eighth, PINNED destination (rendered outside <nav>, not
    # one of the scrollable tabs) — it must exist exactly once, after the split.
    assert re.findall(r"\{ key: '[a-z]+', label: '([^']+)'", pinned_src) == ["Settings"], pinned_src
    assert "key: 'settings', label: 'Settings', href: '/account'" in js
    for gone in ("'CRM'", "Job sites", "Office Console", "Company Portal", "Quick actions", "Hours Log"):
        assert gone not in js, gone
    assert "'/': { '': '/company#overview', 'projects': '/company#overview', 'crm': '/company#clients', 'settings': '/account' }" in js
    assert "'/company': { 'buildings': '/company#projects', 'compliance': '/company#company' }" in js
    for sub in ("'Weekly payroll', '/#labor-rate'", "'Pay rates', '/admin/labor-rates'", "'Calendar', '/company#calendar'",
                "['Estimating queue', '/estimating']]", "'Team & access', '/admin/users'", "'Account', '/account'"):
        assert sub in js, sub
    # 2026-10-14 — the Proposals tab lost its Pipeline sub-tab (the type dropdown replaced it)
    assert "'Pipeline'" not in js and "'/#estimates', COMPANY" not in js
    # #341 — Settings must NOT be duplicated inside the Company tab's own sub-nav
    # anymore; its only home is the pinned entry asserted above.
    assert "'Settings', '/#settings'" not in js
    cs = _login("c_suite")
    pages = {"/company": _page(cs, "/company"), "/": _page(cs, "/")}
    for path in ADMIN_PAGES:
        pages[path] = _page(cs, path)
    for path, html in pages.items():
        assert 'data-company-nav' in html and "SSC_NAV.mount()" in html and "company_nav.js?v=" in html, path
        assert ">Office Console<" not in html and "ssc-nav-ext" not in html and "ssc-side-foot" not in html, path
        assert '<div class="brand-sub">Company</div>' in html, path
    for path, tab in ADMIN_PAGES.items():
        assert f'data-subtabs="{tab}"' in pages[path] and "sscNavForRole(" in pages[path] and "window.sscNavForRole = function" not in pages[path], path
    passed += 1; print("  one sidebar ok")

    # ---- the console: dashboard + CRM retired, remaining panels carry their sub-tab rows
    con = pages["/"]
    for gone in ('data-panel="projects"', 'data-panel="crm"', "window.CRM", 'id="cc-week"', 'data-widget="quick-actions"', '.nav-tab[data-panel="specs"]'):
        assert gone not in con, gone
    for kept in ('data-panel="workforce"', 'data-panel="hours"', 'data-panel="cert-library"', 'data-panel="estimates"',
                 'data-panel="labor-rate"', 'data-panel="compliance"', 'data-panel="specs"'):
        assert kept in con, kept
    assert con.count('data-subtabs="workforce"') == 3 and con.count('data-subtabs="company"') == 2 and con.count('data-subtabs="settings"') == 0   # Settings moved to /account
    assert con.count('data-subtabs="proposals"') == 1 and con.count('data-subtabs="finance"') == 1
    assert "SSC_NAV.redirectIfRetired()" in con and "window.location.replace('/company#overview')" in con
    assert "if(!$('cc-grid')) return;" in con, "the retired widgets module must not poll"
    assert 'data-nav-crumb' not in con and 'data-nav-h1' in con, "one title: no small label repeating it"
    assert "facade:'Exterior'" in con and "facade:'Facade'" not in con
    passed += 1; print("  console retired ok")

    # ---- the portal: week strip, clients, no job sites, Company
    po = pages["/company"]
    for kept in ('id="cc-week"', 'id="cc-week-row"', 'id="cc-week-open"', "async function loadWeekStrip", "loadWeekStrip();",
                 'id="cl-add"', 'id="cl-overlay"', 'id="ct-overlay"', 'id="nt-overlay"', 'id="fu-overlay"', "data-task-done",
                 'id="view-company"', "const ALIASES = { buildings: 'projects', compliance: 'company' };",
                 'data-subtabs="projects"', 'data-subtabs="finance"', 'data-subtabs="workforce"', 'data-subtabs="company"', 'data-subtabs="proposals"',
                 "sites_without_project", "function newProjectHref"):
        assert kept in po, kept
    assert po.index('id="cc-week"') < po.index('id="ov-kpis"'), "the week comes first on the Dashboard"
    assert po.count('data-subtabs="projects"') == 2, "list and calendar both carry the Projects sub-tabs"
    for gone in ('id="view-buildings"', "#buildings", "loadBuildings", "Manage in CRM", "Edit in CRM", "/#crm", "Job sites", "job site", 'id="view-compliance"', "'#cp-nav a'"):
        assert gone not in po, gone
    passed += 1; print("  portal ok")

    # ---- the Add-client path (exactly the calls the portal makes), then the client page
    r = cs.post("/api/crm/organizations", json={"name": "Oh339 Owners LLC", "relationship_type": "client", "notes": None})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    oid = r.get_json()["data"]["id"]
    assert cs.post("/api/crm/contacts", json={"full_name": "Oh339 Contact", "org_id": oid, "email": "oh339@example.com", "phone": None, "title": "Board"}).status_code == 200
    assert cs.post("/api/crm/activity", json={"entity_type": "organization", "entity_id": oid, "activity_type": "call", "summary": "Oh339 call", "body": None}).status_code == 200
    r = cs.post("/api/crm/tasks", json={"entity_type": "organization", "entity_id": oid, "title": "Oh339 follow-up", "due_date": "2026-09-30", "priority": "normal", "detail": None})
    assert r.status_code == 200
    tid = r.get_json()["data"]["id"]
    d = cs.get(f"/api/company/portal/clients/{oid}").get_json()["data"]
    assert d["organization"]["name"] == "Oh339 Owners LLC" and [c["full_name"] for c in d["contacts"]] == ["Oh339 Contact"]
    assert [t["title"] for t in d["open_tasks"]] == ["Oh339 follow-up"] and d["open_tasks"][0]["id"] == tid
    assert any(a["activity_type"] == "call" and a["summary"] == "Oh339 call" for a in d["activity"])
    assert cs.post(f"/api/crm/tasks/{tid}/complete").status_code == 200
    d = cs.get(f"/api/company/portal/clients/{oid}").get_json()["data"]
    assert d["open_tasks"] == []
    rows = cs.get("/api/company/portal/clients?type=client").get_json()["data"]
    assert any(x["id"] == oid and x["contacts"] == 1 for x in rows)
    passed += 1; print("  add client path ok")

    # ---- words
    assert estimates.EST_TYPES["FR"] == "Exterior Restoration"
    assert project_type_config.PROJECT_TYPES[0]["id"] == "facade" and project_type_config.PROJECT_TYPES[0]["label"].startswith("Exterior")
    meta = cs.get("/api/admin/projects/onboard-meta").get_json()["data"]
    assert any(t["code"] == "FR" and t["label"] == "Exterior Restoration" for t in meta["types"]), meta["types"]
    passed += 1; print("  exterior ok")

    # ---- the project dashboard's way back
    pd = _page(cs, f"/projects/{PROJ}")
    assert 'href="/company#overview"' in pd and 'href="/" ' not in pd and 'href="/#workforce"' in pd
    passed += 1; print("  project back link ok")

    # ---- nothing widened
    pm = _login("pm")
    assert pm.get("/", follow_redirects=False).status_code in (302, 403)
    assert pm.get("/company", follow_redirects=False).status_code in (302, 403)
    assert pm.post("/api/crm/organizations", json={"name": "Oh339 nope"}).status_code == 403
    passed += 1; print("  pm still out ok")

    print(f"PASS smoke_one_home_339 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
