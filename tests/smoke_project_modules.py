#!/usr/bin/env python3
"""Per-job dashboard modules (project_modules.py) — behavioral guard.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * storage: projects.enabled_modules is added idempotently; NULL = every module on (legacy jobs
    keep their full dashboard); a saved list is canonical-ordered and de-duplicated
  * new jobs start with core tabs only; onboarding can name modules, a bad one is a 400 and leaves
    nothing behind
  * the served dashboard hides exactly the switched-off modules' nav links (and tags <body
    data-mod-off>), never a core tab, and a legacy job's page is untouched
  * API: GET for internal roles; PUT admin / c_suite only (pm 403, anon 401); unknown project 404;
    bad payloads 400
  * registry drift guard: every module view exists as a nav link in the dashboard, no core view
    is switchable, and the Settings card / router guard are present
Run:  venv/bin/python tests/smoke_project_modules.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_pmod_")

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
import project_modules as pmod  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "pmod"
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
                         (email, auth.hash_password(PW), role, f"Smoke {role} Pmod", 0 if role == "pm" else 1))
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Pmod %'")
        ids["org"] = crm.create_org(conn, name="Pmod Owners", relationship_type="client")
        ids["pm"] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.pm@example.com",)).fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Pmod %'").fetchall()]
        ph = ",".join("?" * len(org_ids)) or "NULL"
        est_ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM estimate WHERE client_org_id IN ({ph}) OR building_address LIKE '% Pmod %'", org_ids).fetchall()]
        codes = {r["project_code"] for r in conn.execute(
            f"SELECT project_code FROM projects WHERE client_org_id IN ({ph}) OR address LIKE '% Pmod %'", org_ids).fetchall()}
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


def _nav_hidden(html, view):
    m = re.search(r'<a class="nav-link[^"]*"[^>]*?\bdata-view="%s"[^>]*>' % re.escape(view), html)
    assert m, f"nav link for {view} not found in the served dashboard"
    return " hidden" in m.group(0)


def _body_off(html):
    m = re.search(r'<body[^>]*data-mod-off="([^"]*)"', html)
    return set(m.group(1).split()) if m else None


def main() -> int:
    ids = _seed()
    try:
        cs, pm, anon = _login("c_suite"), _login("pm"), server.app.test_client()
        base = {"client_org_id": ids["org"], "est_type": "FR", "borough": "BX", "billing_type": "lump_sum"}

        # ---- storage: idempotent column
        conn = _db()
        try:
            pmod.ensure(conn); pmod.ensure(conn)
            ok("enabled_modules column exists after ensure() x2", pmod._has_column(conn, "projects", "enabled_modules"))
        finally:
            conn.close()

        # ---- a new job starts with every tab on (never customized), like every existing job
        r = cs.post("/api/admin/projects/onboard", json={**base, "address": "1 Pmod Way"})
        ok("onboard ok", r.status_code == 201)
        new = r.get_json()["data"]
        code = new["project_code"]
        ok("new job reports every module on", new["modules"] == list(pmod.KEYS))
        conn = _db()
        try:
            conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_by, assigned_at) VALUES (?,?,?,?)",
                         (ids["pm"], code, ids["pm"], "2026-01-01T00:00:00"))
            conn.commit()
        finally:
            conn.close()
        p = cs.get(f"/api/projects/{code}/modules").get_json()["data"]
        ok("profile lists every module", [m["key"] for m in p["modules"]] == list(pmod.KEYS))
        ok("every module is on for a new job", all(m["enabled"] for m in p["modules"]))
        conn = _db()
        try:
            ok("a new job stores nothing (NULL = never customized)", conn.execute("SELECT enabled_modules FROM projects WHERE project_code = ?", (code,)).fetchone()["enabled_modules"] is None)
        finally:
            conn.close()

        html = cs.get(f"/projects/{code}").get_data(as_text=True)
        optional_views = [v for m in pmod.MODULES for v in m["views"]]
        ok("new job: every optional tab is visible", not any(_nav_hidden(html, v) for v in optional_views))
        ok("new job: no core tab is hidden", not any(_nav_hidden(html, v) for v in CORE_VIEWS))
        ok("new job: no module listed as off", _body_off(html) in (None, set()))

        # ---- turn modules on
        r = cs.put(f"/api/admin/projects/{code}/modules", json={"modules": ["rfis", "drops", "rfis"]})
        ok("c_suite can save modules", r.status_code == 200)
        saved = {m["key"] for m in r.get_json()["data"]["modules"] if m["enabled"]}
        ok("duplicates collapse, canonical set saved", saved == {"drops", "rfis"})
        conn = _db()
        try:
            raw = conn.execute("SELECT enabled_modules FROM projects WHERE project_code = ?", (code,)).fetchone()["enabled_modules"]
        finally:
            conn.close()
        ok("stored in canonical registry order", raw == '["drops", "rfis"]')
        html = cs.get(f"/projects/{code}").get_data(as_text=True)
        ok("enabled modules' tabs are visible", not _nav_hidden(html, "rfis") and not _nav_hidden(html, "drop-report") and not _nav_hidden(html, "drop-plans"))
        ok("other optional tabs stay hidden", _nav_hidden(html, "toolbox-talks") and _nav_hidden(html, "weekly-summary") and _nav_hidden(html, "violations"))
        ok("body off-list excludes the enabled modules", _body_off(html) == set(pmod.KEYS) - {"drops", "rfis"})
        ok("pm sees the same filtered dashboard", not _nav_hidden(pm.get(f"/projects/{code}").get_data(as_text=True), "rfis")
           and _nav_hidden(pm.get(f"/projects/{code}").get_data(as_text=True), "toolbox-talks"))

        # all modules on => no filtering markup at all
        cs.put(f"/api/admin/projects/{code}/modules", json={"modules": list(pmod.KEYS)})
        html = cs.get(f"/projects/{code}").get_data(as_text=True)
        ok("all modules on: nothing hidden, no off-list", _body_off(html) is None and not any(_nav_hidden(html, v) for v in optional_views))
        r = cs.put(f"/api/admin/projects/{code}/modules", json={"modules": []})
        ok("an empty list is valid (core only)", r.status_code == 200 and not any(m["enabled"] for m in r.get_json()["data"]["modules"]))

        # ---- legacy job: never customized => everything on, page untouched
        conn = _db()
        try:
            conn.execute("UPDATE projects SET enabled_modules = NULL WHERE project_code = ?", (code,))
            conn.commit()
        finally:
            conn.close()
        html = cs.get(f"/projects/{code}").get_data(as_text=True)
        ok("NULL (legacy) job shows every tab", _body_off(html) is None and not any(_nav_hidden(html, v) for v in optional_views))
        ok("NULL (legacy) job profile is all on", all(m["enabled"] for m in cs.get(f"/api/projects/{code}/modules").get_json()["data"]["modules"]))
        conn = _db()
        try:
            conn.execute("UPDATE projects SET enabled_modules = 'not json' WHERE project_code = ?", (code,))
            conn.commit()
        finally:
            conn.close()
        ok("corrupt stored value falls back to everything on", all(m["enabled"] for m in cs.get(f"/api/projects/{code}/modules").get_json()["data"]["modules"]))

        # ---- validation + permissions
        for bad in (None, "rfis", {"rfis": True}, ["rfis", "bogus"], [1], ["RFIS"]):
            r = cs.put(f"/api/admin/projects/{code}/modules", json={"modules": bad})
            ok(f"bad payload {bad!r} -> 400", r.status_code == 400)
        ok("missing body -> 400", cs.put(f"/api/admin/projects/{code}/modules", json={}).status_code == 400)
        ok("unknown project -> 404", cs.put("/api/admin/projects/NOPE-XX-999/modules", json={"modules": []}).status_code == 404)
        ok("unknown project GET -> 404", cs.get("/api/projects/NOPE-XX-999/modules").status_code == 404)
        ok("pm cannot change modules (403)", pm.put(f"/api/admin/projects/{code}/modules", json={"modules": ["rfis"]}).status_code == 403)
        ok("pm can read modules", pm.get(f"/api/projects/{code}/modules").status_code == 200)
        ok("anonymous PUT -> 401", anon.put(f"/api/admin/projects/{code}/modules", json={"modules": []}).status_code == 401)
        ok("anonymous GET -> 401", anon.get(f"/api/projects/{code}/modules").status_code == 401)

        # ---- onboarding with modules
        r = cs.post("/api/admin/projects/onboard", json={**base, "address": "2 Pmod Way", "modules": ["rfis"]})
        ok("onboard can start a job with named modules", r.status_code == 201 and r.get_json()["data"]["modules"] == ["rfis"])
        r = cs.post("/api/admin/projects/onboard", json={**base, "address": "3 Pmod Way", "modules": ["bogus"]})
        ok("onboard with an unknown module -> 400", r.status_code == 400)
        # a refused onboarding leaves no brand-new client company behind
        for bad in ({"modules": ["bogus"]}, {"billing_type": "barter"}, {"pm_user_id": 999999999}):
            body = {k: v for k, v in base.items() if k != "client_org_id"}
            body.update({"new_org_name": "Pmod Orphan Check", "address": "4 Pmod Way", **bad})
            r = cs.post("/api/admin/projects/onboard", json=body)
            c2 = _db()
            try:
                left = c2.execute("SELECT COUNT(*) FROM crm_organization WHERE name = 'Pmod Orphan Check'").fetchone()[0]
            finally:
                c2.close()
            ok(f"refused onboarding ({list(bad)[0]}) creates no client ({r.status_code}, left={left})", r.status_code in (400, 404) and left == 0)
        # the console's "Create project" link (?new=1) waits for the deferred form script;
        # drill-throughs to switched-off tabs are pruned on the job dashboard
        ap = cs.get("/admin/projects").get_data(as_text=True)
        ok("?new=1 opens the form after DOMContentLoaded", "if(q.get('new') !== '1') return;" in ap and "addEventListener('DOMContentLoaded', go" in ap)
        dash = cs.get(f"/projects/{code}").get_data(as_text=True)
        ok("dashboard prunes drill-throughs to hidden tabs", "function pruneGotos(root)" in dash and "window.SSC_viewOn" in dash)
        conn = _db()
        try:
            ok("a rejected onboard leaves nothing behind", not conn.execute("SELECT 1 FROM estimate WHERE building_address LIKE '3 Pmod Way%'").fetchone())
        finally:
            conn.close()

        # ---- registry drift guard
        dash = (ROOT / "dashboard-static.html").read_text(encoding="utf-8")
        for v in optional_views:
            ok(f"module view '{v}' has a nav link", re.search(r'<a class="nav-link[^"]*"[^>]*?\bdata-view="%s"' % re.escape(v), dash) is not None)
        ok("no core view is switchable", not (set(CORE_VIEWS) & set(optional_views)))
        ok("sov follows billing type, not a module", "sov" not in optional_views)
        ok("Settings card present", 'id="ps-mod-list"' in dash and 'id="ps-mod-save"' in dash)
        ok("router refuses hidden tabs", "if (link.hidden) return;" in dash)
        ok("KPIs honor switched-off modules", "!OFF.includes('drops')" in dash)
        ok("operations panel present; no schedule card (Oct 7 notes)", 'id="jx-schedule"' not in dash and "jx-ops-bar" in dash)
    finally:
        _cleanup()

    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s) failed")
        return 1
    print("PASS: project modules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
