#!/usr/bin/env python3
"""#360 — the Client portal tab + Company / Settings split. Guard.

Boots the real app on an ISOLATED database. Proves:
  * the company sidebar: Client portal is its own tab; Company keeps company records only
    (no Team & access); Settings owns Account + Team & access, lights up on
    /admin/users, and its page no longer draws the Company sub-tabs or the old placeholder
  * the old client-access cards are gone from Projects → Assignments (one place to manage)
  * GET /api/company/portal-access: company roles only; one row per client with the
    setup state in the order it has to happen (inactive → link a project → nothing
    unlocked → invited → live), the linked project + its client company, sections, the
    matching preset, counts; architects listed separately with what their role opens;
    only an admin may create logins; no email reaches the log
  * the tab's writes reuse the owning endpoints (a project link through pm-assignments
    clears grants; a preset shows up as that preset)
Run:  venv/bin/python tests/smoke_client_portal_tab_360.py
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_cpt_360_")

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

TAG = "cpt360"
PW = "ClientPortalTab2026"
P, Q = "CPT360-P", "CPT360-Q"
API = "/api/company/portal-access"


def _db():
    return db_layer.connect(pragma_fk=False)


def _purge(conn):
    uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
    purge(conn, "client_section_grant", "user_id", uids)
    purge(conn, "pm_project_assignment", "project_code", [P, Q])
    purge(conn, "projects", "project_code", [P, Q])
    oids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE ?", (f"{TAG}%",)).fetchall()]
    purge(conn, "crm_organization", "id", oids)
    return uids


def _seed():
    conn = _db()
    try:
        purge_users(conn, _purge(conn))
        org = crm.create_org(conn, name=f"{TAG} Owner LLC", relationship_type="client")
        conn.execute("INSERT INTO projects (project_code, name, address, status, client_org_id) VALUES (?, 'Cpt360 Job', '1 Cpt St', 'active', ?)", (P, org))
        conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, 'Cpt360 Other', '2 Cpt St', 'active')", (Q,))
        ids = {}
        # role, key, active, must_reset, last_login
        people = [("admin", "admin", 1, 0, "2026-09-01T08:00:00"), ("c_suite", "c_suite", 1, 0, "2026-09-01T08:00:00"),
                  ("pm", "pm", 1, 0, "2026-09-01T08:00:00"),
                  ("client", "noproj", 1, 1, None), ("client", "locked", 1, 1, None), ("client", "invited", 1, 1, None),
                  ("client", "live", 1, 0, "2026-09-20T09:30:00"), ("client", "gone", 0, 0, None),
                  ("architect", "arch", 1, 0, None)]
        for role, key, active, must, last in people:
            email = f"{TAG}.{key}@example.com"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, display_name, is_active, status, must_reset_password, last_login_at, is_system) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                         (email, auth.hash_password(PW), role, f"{TAG} {key}", f"{TAG} {key}", active,
                          "active" if active else "deactivated", must, last))
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        for key in ("locked", "invited", "live", "gone", "arch"):
            conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, '2026-09-01')", (ids[key], P))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, '2026-09-01')", (ids["pm"], P))
        for key, secs in (("invited", ("progress",)), ("live", ("progress", "photos", "daily"))):
            for sct in secs:
                conn.execute("INSERT INTO client_section_grant (user_id, project_code, section, granted_at) VALUES (?, ?, ?, '2026-09-01')", (ids[key], P, sct))
        conn.commit()
        return ids
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        uids = _purge(conn)
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(key):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@example.com", "password": PW})
    assert r.status_code == 200, (key, r.status_code)
    return c


def main() -> int:
    ids = _seed()
    passed = 0

    # ---- the sidebar definition
    nav = (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")
    tabs_src, _, pinned_src = nav.partition("var PINNED")
    labels = re.findall(r"\{ key: '[a-z]+', label: '([^']+)'", tabs_src)
    assert labels == ["Home", "Dashboard", "Projects", "Clients", "Proposals", "Finance", "Workforce", "Company"], labels
    # Client portal is a sub-tab of Clients (not a top-level tab), reachable at /company#portal
    assert "sub: [['All clients', '/company#clients'], ['Client portal', '/company#portal']]" in nav
    assert "at: [['/company', 'clients'], ['/company', 'portal']]" in nav
    assert "key: 'portal'" not in nav
    company = tabs_src[tabs_src.index("{ key: 'company'"):tabs_src.index("\n  ];")]
    assert "Team & access" not in company and "/admin/users" not in company, "Company keeps company records only"
    assert "at: [['/account', null], ['/admin/users', null], ['/company', 'usage'], ['/company', 'claude']]" in pinned_src
    assert "sub: [['Account', '/account'], ['Team & access', '/admin/users'], ['Usage & job tools', '/company#usage'], ['Claude approvals', '/company#claude']]" in pinned_src
    assert "if (bar.getAttribute('data-subtabs') === PINNED.key) tab = PINNED;" in nav
    passed += 1; print("  sidebar ok")

    # ---- the pages
    admin, cs, pm = _login("admin"), _login("c_suite"), _login("pm")
    con = cs.get("/").get_data(as_text=True)
    # Settings > Account replaced the console panel (and its Interface card)
    assert 'data-panel="settings"' not in con and 'id="interface-section"' not in con
    assert "coming next" not in con and "api-keys.txt" not in con, "the placeholder is gone"
    acc = cs.get("/account").get_data(as_text=True)
    assert 'data-subtabs="settings"' in acc and 'data-subtabs="company"' not in acc, "Settings draws its own sub-tabs"
    assert 'data-subtabs="settings"' in cs.get("/admin/users").get_data(as_text=True)
    ap = cs.get("/admin/projects").get_data(as_text=True)
    assert 'id="client-list"' not in ap and "toggleSection" not in ap and "/api/admin/client-grants" not in ap
    assert 'href="/company#portal"' in ap, "Projects → Assignments points to the new tab"
    cp = cs.get("/company").get_data(as_text=True)
    assert 'id="view-portal"' in cp and 'id="pa-table"' in cp and "portal: loadPortalAccess" in cp
    assert "'/company/portal-access'" in cp
    passed += 1; print("  pages ok")

    # ---- the read: gates + rows
    assert pm.get(API).status_code == 403
    buf = io.StringIO(); h = logging.StreamHandler(buf); logging.getLogger().addHandler(h)
    try:
        d = cs.get(API).get_json()["data"]
    finally:
        logging.getLogger().removeHandler(h)
    assert "@example.com" not in buf.getvalue(), "no email in the log"
    rows = {r["user_id"]: r for r in d["clients"]}
    st = lambda k: rows[ids[k]]["state"]   # noqa: E731
    assert (st("gone"), st("noproj"), st("locked"), st("invited"), st("live")) == \
        ("inactive", "no_project", "locked", "invited", "live"), {k: st(k) for k in ("gone", "noproj", "locked", "invited", "live")}
    live = rows[ids["live"]]
    assert live["project_code"] == P and live["project_name"] == "Cpt360 Job" and live["org_name"] == f"{TAG} Owner LLC"
    assert live["sections"] == ["progress", "photos", "daily"] and live["preset"] == "standard", live
    assert rows[ids["invited"]]["preset"] == "minimal" and rows[ids["locked"]]["preset"] is None
    assert ids["arch"] not in rows and any(o["user_id"] == ids["arch"] and o["sections"] == ["documents", "drawing", "rfis"] for o in d["others"])
    mine = [r for r in d["clients"] if r["email"].startswith(TAG)]
    active = [r for r in mine if r["active"]]
    assert len(active) == 4 and sum(r["state"] == "live" for r in active) == 1
    assert d["counts"]["clients"] >= 4 and d["counts"]["needs_setup"] >= 2
    assert "weekly" not in d["sections"] and "rfis" in d["sections"], "only served sections are offered"
    assert any(p["project_code"] == Q for p in d["projects"])
    assert d["can_create"] is False and admin.get(API).get_json()["data"]["can_create"] is True
    passed += 1; print("  read ok")

    # ---- the writes the tab makes go through the owning endpoints
    r = cs.put("/api/admin/pm-assignments", json={"user_id": ids["live"], "project_codes": [Q]})
    assert r.status_code == 200, r.get_json()
    row = next(x for x in cs.get(API).get_json()["data"]["clients"] if x["user_id"] == ids["live"])
    assert row["project_code"] == Q and row["sections"] == [] and row["state"] == "locked", "moving a client locks every section"
    assert cs.post("/api/admin/client-grants/preset", json={"user_id": ids["live"], "preset": "full"}).status_code == 200
    row = next(x for x in cs.get(API).get_json()["data"]["clients"] if x["user_id"] == ids["live"])
    assert row["preset"] == "full" and row["state"] == "live"
    assert cs.post("/api/admin/users", json={"display_name": "x", "email": f"{TAG}.new@example.com", "role": "client"}).status_code == 403, \
        "creating a login stays admin-only"
    passed += 1; print("  writes ok")

    print(f"PASS smoke_client_portal_tab_360 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_client_portal_tab_360: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
