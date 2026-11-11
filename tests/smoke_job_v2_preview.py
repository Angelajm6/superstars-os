#!/usr/bin/env python3
"""Project page redesign (job_v2.py) — behavioral + drift guard.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * the redesign is the DEFAULT for staff (c_suite, pm) on a project's own page; a pm never
    receives the Money views; anonymous is refused
  * the way back: `?ui=1` (one request) and the SSC_UI_FORCE_V1 kill switch serve the classic
    page byte-for-byte
  * SCOPE: 9 other pages (console, admin, login, worker app, legacy /dashboard ...) and the pm
    projects list never pick it up; only job_v2.py injects the assets
  * drift guard: every sidebar view sits in exactly one of the six sections (or is Settings), every
    section view exists in the sidebar, every view has a label + icon
  * + New / Jump to only offer pages this person can open; phone bottom bar + round + New
  * look: Inter + Archivo, six section colors, primary buttons stay the accent, every selector scoped
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

        # ---- the redesign is the default for staff on a project's own page
        page = cs.get(f"/projects/{code}").get_data(as_text=True)
        ok("default request: body is scoped to jv2", re.search(r"<body[^>]*class=\"jv2\"", page) is not None)
        ok("default request: job.css + job.js are loaded (no switch-back bar)",
           "/files/static/v2/job.css" in page and "/files/static/v2/job.js" in page and "switch-back" not in page)
        ok("the classic sidebar is still in the page (the sections drive it)", 'class="sidebar"' in page and 'data-view="payments"' in page)
        ok("?ui=2 is the same redesigned page", 'class="jv2"' in cs.get(f"/projects/{code}?ui=2").get_data(as_text=True))
        pv2 = pm.get(f"/projects/{code}").get_data(as_text=True)
        ok("a pm gets it too", 'class="jv2"' in pv2)
        ok("pm keeps its own back link", 'class="bl-projects"' in pv2 and 'href="/projects"' in pv2)
        ok("pm never receives the Money section's views", 'data-view="payments"' not in pv2 and 'data-view="sov"' not in pv2)
        ok("anonymous is refused (auth gate), never the page", anon.get(f"/projects/{code}").status_code in (302, 401))

        # ---- the way back: ?ui=1 for one request, the kill switch for everyone
        classic = cs.get(f"/projects/{code}?ui=1").get_data(as_text=True)
        ok("?ui=1 serves the classic page (no jv2 markup or assets)", "jv2" not in classic and "/static/v2/job" not in classic)
        os.environ["SSC_UI_FORCE_V1"] = "1"
        try:
            ks = cs.get(f"/projects/{code}").get_data(as_text=True)
        finally:
            os.environ.pop("SSC_UI_FORCE_V1", None)
        ok("SSC_UI_FORCE_V1 forces the classic page", "jv2" not in ks)
        ok("the classic page is byte-identical either way", ks == classic)

        # ---- SCOPE: only a project's own page
        pv = cs.get(f"/portal/{code}", follow_redirects=True).get_data(as_text=True)
        ok("/portal/<code> (internal role) is the same project page, so it is redesigned", 'class="jv2"' in pv)
        others = ["/company", "/admin/projects", "/admin/users", "/admin/labor-rates", "/estimating", "/login", "/worker-app",
                  "/dashboard", "/files/static/js/company_nav.js"]
        leaked = []
        for path in others:
            for c_, who in ((cs, "c_suite"), (anon, "anon")):
                body = c_.get(path, follow_redirects=True).get_data(as_text=True)
                if "jv2" in body or "/static/v2/job." in body:
                    leaked.append(f"{who}:{path}")
        ok("no other page picks up the redesign (9 pages, signed in and out)", not leaked)
        if leaked:
            print("    leaked on:", leaked)
        ok("the pm's projects list is not redesigned", "jv2" not in pm.get("/projects", follow_redirects=True).get_data(as_text=True))
        import job_v2 as _jv
        ok("apply() refuses a page with no project code", _jv.apply("<html><head></head><body></body></html>", "c_suite", None) == "<html><head></head><body></body></html>")
        ok("apply() refuses an external role", _jv.apply("<html><head></head><body></body></html>", "client", "X-1") == "<html><head></head><body></body></html>")
        refs = []
        for pth in list(ROOT.glob("*.html")) + list((ROOT / "templates").rglob("*.html")) + list((ROOT / "static").rglob("*.js")) + list((ROOT / "static").rglob("*.css")):
            if pth.name in ("job.js", "job.css"):
                continue
            t = pth.read_text(encoding="utf-8", errors="ignore")
            if "v2/job.css" in t or "v2/job.js" in t:
                refs.append(pth.name)
        ok("no page or script references the assets (only job_v2.py injects them)", not refs)

        # ---- assets
        css = cs.get("/files/static/v2/job.css"); js = cs.get("/files/static/v2/job.js")
        ok("job.css and job.js are served", css.status_code == 200 and js.status_code == 200)
        job_css = (ROOT / "static" / "v2" / "job.css").read_text(encoding="utf-8")
        job_js = (ROOT / "static" / "v2" / "job.js").read_text(encoding="utf-8")
        if shutil.which("node"):
            import subprocess
            ok("job.js parses (node --check)", subprocess.run(["node", "--check", str(ROOT / "static" / "v2" / "job.js")], capture_output=True).returncode == 0)

        # ---- drift guard: every nav view is in exactly one section (or Settings)
        dash = (ROOT / "dashboard-static.html").read_text(encoding="utf-8")
        nav_views = re.findall(r'<a class="nav-link[^"]*"[^>]*?\bdata-view="([^"]+)"', dash)
        tabs_src = job_js[job_js.index("var TABS = ["): job_js.index("];", job_js.index("var TABS = ["))]
        tab_views = re.findall(r"'([a-z-]+)'", " ".join(re.findall(r"views: \[([^\]]*)\]", tabs_src)))
        ok("every sidebar view is placed in a section (or Settings)", set(nav_views) - {"settings"} <= set(tab_views))
        ok("every section view exists in the sidebar", set(tab_views) <= set(nav_views))
        ok("no view is in two sections", len(tab_views) == len(set(tab_views)))
        keys = re.findall(r"\{ key: '([a-z]+)', label: '[A-Za-z]+', icon: '[a-z]+', views:", tabs_src)
        ok("six sections: Overview, Today, Work, Documents, Money, Team", keys == ["overview", "today", "work", "documents", "money", "team"])
        ok("Today holds the daily report, photos and toolbox talks", "key: 'today', label: 'Today', icon: 'sun', views: ['daily-report', 'field-photos', 'toolbox-talks']" in tabs_src)
        ok("every view has a label and an icon", all(f"'{v}':" in job_js[job_js.index("var LABEL"):job_js.index("var ICON")] for v in tab_views)
           and all(f"'{v}':" in job_js[job_js.index("var ICON"):job_js.index("var NO_PILL")] for v in tab_views))
        ok("+ New offers daily report, photo, delivery, RFI, document, expense, sign or form",
           all(f"'{v}'" in job_js[job_js.index("var NEW_ITEMS"):job_js.index("var last")] for v in ("daily-report", "field-photos", "materials", "rfis", "documents", "expenses", "templates")))
        ok("+ New only offers what this person can open", "NEW_ITEMS.filter(function (i) { return visible(i[1]); })" in job_js)
        ok("Jump to (Ctrl/Cmd+K) lists only visible pages", "openJump" in job_js and "e.metaKey || e.ctrlKey" in job_js and "visibleViews(t).forEach" in job_js)
        ok("superintendents land on Today (once, never over a deep link)", "u.role === 'super'" in job_js and "!location.hash" in job_js)
        ok("phone: bottom section bar + round + New", "jv2-bottom" in job_js and "#jv2-bottom" in job_css and "jv2-fab" in job_css and "max-width: 860px" in job_css)
        ok("the + New button steps aside for a Daily Report step", "body.jv2.ds-active .jv2-fab { display: none; }" in job_css)
        ok("the Needs you list never invents money for a pm (reads S.money only when sent)", "M = S.money" in job_js and "if (M)" in job_js)

        # ---- look: fonts, colors, primary button
        ok("Inter for text, Archivo for titles (both self-hosted)", "--jv-font: 'Inter'" in job_css and "--jv-display: 'Archivo'" in job_css)
        ok("six section colors defined once", all(f"--jv-c-{k}:" in job_css for k in ("overview", "today", "work", "documents", "money", "team")))
        ok("primary buttons stay the one accent (DESIGN_SYSTEM §2)", "--jv-accent: var(--accent" in job_css and "background: var(--jv-accent)" in job_css
           and not re.search(r"btn-primary[^{}]*\{[^}]*background\s*:", job_css))
        css_nocomment = re.sub(r"/\*.*?\*/", "", job_css, flags=re.S)
        unscoped = []
        for sel in re.findall(r"([^{}]+)\{", css_nocomment):
            sel = sel.strip()
            if sel.startswith("@"):
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
        ok("every CSS selector is scoped (body.jv2 / #jv2-* / .jv2-*)", not unscoped)
        if unscoped:
            print("    unscoped selectors:", unscoped[:5])
    finally:
        _cleanup()

    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s) failed")
        return 1
    print("PASS: project page redesign")
    return 0


if __name__ == "__main__":
    sys.exit(main())
