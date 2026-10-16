#!/usr/bin/env python3
"""Job dashboard v2 preview (job_v2.py) — behavioral + drift guard.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * default (v1) requests are byte-identical: no v2 markup, no v2 assets
  * `?ui=2` serves the preview (body.jv2 + job.css + job.js + the switch-back bar) to company roles
    and a pm, and NOT to anonymous or when the SSC_UI_FORCE_V1 kill switch is set
  * SCOPE: only a project's own dashboard is ever previewed — 9 other pages (console, admin,\n    login, worker app, legacy /dashboard ...) and the pm projects list never are\n  * the v2 assets are served; JS parses (node --check when node exists)
  * drift guard: every sidebar view belongs to exactly one top tab (or is Settings), and every tab
    view exists in the sidebar — a new nav item cannot silently vanish from the preview
  * one font family and one type scale are declared in job.css (no Archivo, no second family)
Run:  venv/bin/python tests/smoke_job_v2_preview.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_jv2p_")

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

TAG = "jv2p"
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
                         (email, auth.hash_password(PW), role, f"Smoke {role} Jv2p", 0 if role == "pm" else 1))
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Jv2p %'")
        ids["org"] = crm.create_org(conn, name="Jv2p Owners", relationship_type="client")
        ids["pm"] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.pm@example.com",)).fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Jv2p %'").fetchall()]
        ph = ",".join("?" * len(org_ids)) or "NULL"
        est_ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM estimate WHERE client_org_id IN ({ph}) OR building_address LIKE '% Jv2p %'", org_ids).fetchall()]
        codes = {r["project_code"] for r in conn.execute(
            f"SELECT project_code FROM projects WHERE client_org_id IN ({ph}) OR address LIKE '% Jv2p %'", org_ids).fetchall()}
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
        cs, pm, anon = _login("c_suite"), _login("pm"), server.app.test_client()
        r = cs.post("/api/admin/projects/onboard", json={"client_org_id": ids["org"], "est_type": "FR", "borough": "BX",
                                                         "billing_type": "lump_sum", "address": "1 Jv2p Way"})
        ok("onboard ok", r.status_code == 201)
        code = r.get_json()["data"]["project_code"]
        conn = _db()
        try:
            conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_by, assigned_at) VALUES (?,?,?,?)",
                         (ids["pm"], code, ids["pm"], "2026-01-01T00:00:00"))
            conn.commit()
        finally:
            conn.close()

        # ---- v1 stays byte-identical
        v1 = cs.get(f"/projects/{code}").get_data(as_text=True)
        ok("default request has no v2 markup", "jv2" not in v1 and "/static/v2/job" not in v1 and "switch-back" not in v1)
        ok("?ui=1 is the classic page too", cs.get(f"/projects/{code}?ui=1").get_data(as_text=True) == v1)

        # ---- ?ui=2 serves the preview
        v2 = cs.get(f"/projects/{code}?ui=2").get_data(as_text=True)
        ok("?ui=2: body is scoped to jv2", re.search(r"<body[^>]*class=\"jv2\"", v2) is not None)
        ok("?ui=2: stylesheet + script + switch-back are loaded",
           "/files/static/v2/job.css" in v2 and "/files/static/v2/job.js" in v2 and "/files/static/v2/switch-back.js" in v2)
        ok("?ui=2: the classic sidebar is still in the page (tabs drive it)", 'class="sidebar"' in v2 and 'data-view="payments"' in v2)
        pv2 = pm.get(f"/projects/{code}?ui=2").get_data(as_text=True)
        ok("pm can preview too", 'class="jv2"' in pv2)
        ok("pm keeps its own back link in the preview", 'class="bl-projects"' in pv2 and 'href="/projects"' in pv2)
        ok("anonymous is refused (auth gate), never a v2 page", anon.get(f"/projects/{code}?ui=2").status_code in (302, 401))
        loc = anon.get(f"/projects/{code}?ui=2").headers.get("Location", "")
        ok("the real app's login redirect keeps ?ui=2 (so signing in lands on the preview)", "next=" in loc and "%3Fui%3D2" in loc)

        # ---- SCOPE: the preview is for a project's own dashboard ONLY
        pv = cs.get(f"/portal/{code}?ui=2", follow_redirects=True).get_data(as_text=True)
        ok("/portal/<code> (internal role) is the same project dashboard, so it previews", 'class="jv2"' in pv)
        others = ["/company", "/admin/projects", "/admin/users", "/admin/labor-rates", "/estimating", "/login", "/worker-app",
                  "/dashboard", "/files/static/js/company_nav.js"]
        leaked = []
        for path in others:
            for c_, who in ((cs, "c_suite"), (anon, "anon")):
                resp = c_.get(path + "?ui=2", follow_redirects=True)
                body = resp.get_data(as_text=True)
                if "jv2" in body or "/static/v2/job." in body:
                    leaked.append(f"{who}:{path}")
        ok("no other page picks up the preview (?ui=2 on 9 pages, signed in and out)", not leaked)
        if leaked:
            print("    leaked on:", leaked)
        pm_list = pm.get("/projects?ui=2", follow_redirects=True).get_data(as_text=True)
        ok("the pm's projects list is not previewed", "jv2" not in pm_list)
        ok("legacy /dashboard (no specific project) is not previewed", "jv2" not in cs.get("/dashboard?ui=2", follow_redirects=True).get_data(as_text=True))
        import job_v2 as _jv
        ok("apply() refuses a page with no project code", _jv.apply("<html><head></head><body></body></html>", "c_suite", None) == "<html><head></head><body></body></html>")
        refs = []
        for pth in list(ROOT.glob("*.html")) + list((ROOT / "templates").rglob("*.html")) + list((ROOT / "static").rglob("*.js")) + list((ROOT / "static").rglob("*.css")):
            if pth.name in ("job.js", "job.css"):
                continue
            if "v2/job.css" in pth.read_text(encoding="utf-8", errors="ignore") or "v2/job.js" in pth.read_text(encoding="utf-8", errors="ignore"):
                refs.append(pth.name)
        ok("no page or script references the preview assets (only job_v2.py injects them)", not refs)
        if refs:
            print("    referenced by:", refs)

        # ---- the kill switch wins over ?ui=2
        os.environ["SSC_UI_FORCE_V1"] = "1"
        try:
            ks = cs.get(f"/projects/{code}?ui=2").get_data(as_text=True)
        finally:
            os.environ.pop("SSC_UI_FORCE_V1", None)
        ok("SSC_UI_FORCE_V1 forces the classic page", "jv2" not in ks)

        # ---- assets
        css = cs.get("/files/static/v2/job.css"); js = cs.get("/files/static/v2/job.js")
        ok("job.css and job.js are served", css.status_code == 200 and js.status_code == 200)
        job_css = (ROOT / "static" / "v2" / "job.css").read_text(encoding="utf-8")
        job_js = (ROOT / "static" / "v2" / "job.js").read_text(encoding="utf-8")
        if shutil.which("node"):
            import subprocess
            ok("job.js parses (node --check)", subprocess.run(["node", "--check", str(ROOT / "static" / "v2" / "job.js")], capture_output=True).returncode == 0)

        # ---- drift guard: every nav view is in exactly one tab (or Settings)
        dash = (ROOT / "dashboard-static.html").read_text(encoding="utf-8")
        nav_views = re.findall(r'<a class="nav-link[^"]*"[^>]*?\bdata-view="([^"]+)"', dash)
        tabs_src = job_js[job_js.index("var TABS = ["): job_js.index("];", job_js.index("var TABS = ["))]
        tab_views = re.findall(r"'([a-z-]+)'", " ".join(re.findall(r"views: \[([^\]]*)\]", tabs_src)))
        ok("every sidebar view is placed in a tab (or Settings)", set(nav_views) - {"settings"} <= set(tab_views))
        ok("every tab view exists in the sidebar", set(tab_views) <= set(nav_views))
        ok("no view is in two tabs", len(tab_views) == len(set(tab_views)))
        ok("six tabs", len(re.findall(r"\{ key: '[a-z]+', label: '[A-Za-z]+', views:", tabs_src)) == 6)
        ok("Today tab holds the daily report, photos and toolbox talks", "key: 'today', label: 'Today', views: ['daily-report', 'field-photos', 'toolbox-talks']" in tabs_src)
        ok("labels come from the nav links except the named merges", "function label(view)" in job_js and "l.textContent" in job_js and "var RENAME =" in job_js)
        ok("merged views stay reachable (only violations and templates lose their pill)", "var NO_PILL = ['violations', 'templates']" in job_js)
        ok("+ New menu offers daily report, photo, delivery, RFI, expense, blank form", all(f"'{v}'" in job_js[job_js.index("var NEW_ITEMS"):job_js.index("var QUICK")] for v in ("daily-report", "field-photos", "materials", "rfis", "expenses", "templates")))
        ok("superintendents land on Today (once, never over a deep link)", "u.role === 'super'" in job_js and "!location.hash" in job_js)
        ok("phone bottom bar exists (four tabs + More past five)", "jv2-bottom" in job_js and "#jv2-bottom" in job_css and "max-width: 860px" in job_css)

        # ---- one family, one scale
        ok("one font family declared", job_css.count("--jv-font:") == 1 and "'Inter'" in job_css and "Archivo" not in job_css)
        ok("the family is forced across the page", "font-family: var(--jv-font) !important" in job_css)
        ok("the type scale is 12 / 14 / 16 / 20 / 28", all(f"--jv-t{n}: {n}px" in job_css for n in (12, 14, 16, 20, 28)))
        sizes = set(re.findall(r"font-size:\s*(\d+)px", job_css))
        ok("no raw font-size outside the scale", sizes <= {"12", "14", "16", "20", "28"})
        ok("only weights 400/500/600", set(re.findall(r"font-weight:\s*(\d+)", job_css)) <= {"400", "500", "600"})
        css_nocomment = re.sub(r"/\*.*?\*/", "", job_css, flags=re.S)
        unscoped = []
        for sel in re.findall(r"([^{}]+)\{", css_nocomment):
            sel = sel.strip()
            if sel.startswith("@media"):
                continue
            parts, depth, cur = [], 0, ""
            for ch in sel:                      # split on commas that are not inside (...)
                depth += (ch == "(") - (ch == ")")
                if ch == "," and depth == 0:
                    parts.append(cur); cur = ""
                else:
                    cur += ch
            parts.append(cur)
            for part in parts:
                if "jv2" not in part:
                    unscoped.append(part.strip()[:60])
        ok("every CSS selector is scoped to the preview (body.jv2 / #jv2-top / .jv2-*)", not unscoped)
        if unscoped:
            print("    unscoped selectors:", unscoped[:5])
    finally:
        _cleanup()

    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s) failed")
        return 1
    print("PASS: job dashboard v2 preview")
    return 0


if __name__ == "__main__":
    sys.exit(main())
