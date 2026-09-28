#!/usr/bin/env python3
"""#328 — navigation clarity: behavioral guard.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * the served project dashboard carries the sidebar identity block (code, name,
    address, client) filled server-side — for c_suite AND for an assigned pm — and
    no longer carries the "Labor Rate Tracker" link; the Financial group split (#262)
    is untouched
  * access.render_project_identity fills data-project-address, drops the address
    when it equals the name, and blanks everything for an unknown project
  * the Office Console shows "Pay Rates" and a visible "Weekly Payroll" tab (revealed
    for admin / c_suite by the same hook) and none of the old labels
  * the company portal nav / titles say "Projects" (hash routes unchanged)
  * /admin/labor-rates is headed "Pay Rates" and stays 403 for a pm
Run:  venv/bin/python tests/smoke_nav_clarity_328.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_nav_328_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import access  # noqa: E402
import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "nav328"
PW = "NavClaritySmoke2026"
ROLES = ("c_suite", "pm")
PROJ = "NAV328-A"
NAME = "Nav328 Facade Restoration"
ADDR = "12 Nav328 Avenue, Bronx"


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed() -> None:
    conn = _db()
    try:
        for role in ROLES:
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), role, f"Smoke {role}"))
        conn.execute("DELETE FROM projects WHERE project_code = ?", (PROJ,))
        conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, ?, ?, 'active')",
                     (PROJ, NAME, ADDR))
        pm_id = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.pm@example.com",)).fetchone()["id"]
        conn.execute("DELETE FROM pm_project_assignment WHERE project_code = ?", (PROJ,))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, '2026-01-01')",
                     (pm_id, PROJ))
        conn.commit()
    finally:
        conn.close()


def _cleanup() -> None:
    conn = _db()
    try:
        purge(conn, "pm_project_assignment", "project_code", [PROJ])
        conn.execute("DELETE FROM projects WHERE project_code = ?", (PROJ,))
        try:
            conn.execute("DELETE FROM login_audit WHERE ip LIKE '10.28.%'")
        except Exception:
            conn.rollback()
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(role, ip_last):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW},
               environ_base={"REMOTE_ADDR": f"10.28.0.{ip_last}"})
    assert r.status_code == 200, (role, r.status_code, r.get_data(as_text=True)[:200])
    return c


def _page(c, path):
    r = c.get(path, follow_redirects=True)
    assert r.status_code == 200, (path, r.status_code)
    return r.get_data(as_text=True)


def main() -> int:
    _seed()
    passed = 0

    # ---- renderer unit: address fill, de-dup, unknown project
    tpl = ('<div data-project-code>&nbsp;</div><div data-project-name>&nbsp;</div>'
           '<div data-project-address></div><div data-project-client>&nbsp;</div>')
    out = access.render_project_identity(tpl, {"project_code": "X-1", "name": "Tower", "address": "1 Main St", "client_name": "Acme"})
    assert "data-project-code>X-1<" in out and "data-project-name>Tower<" in out
    assert "data-project-address>1 Main St<" in out and "data-project-client>Acme<" in out
    out = access.render_project_identity(tpl, {"project_code": "X-1", "name": "1 Main St", "address": "1 main st"})
    assert "data-project-address><" in out, "address equal to the name must not print twice"
    out = access.render_project_identity(tpl, {"project_code": "X-2", "name": None})
    assert "data-project-name>X-2<" in out, "no name -> the code stands in"
    out = access.render_project_identity(tpl, None)
    assert "data-project-code>&nbsp;<" in out and "data-project-address><" in out
    passed += 1; print("  renderer ok")

    # ---- served project page: identity block on the sidebar, no LRT link
    cs = _login("c_suite", 1)
    pm = _login("pm", 2)
    for who, c in (("c_suite", cs), ("pm", pm)):
        html = _page(c, f"/projects/{PROJ}")
        assert 'id="proj-line"' in html, who
        assert f'data-project-code>{PROJ}<' in html, who
        assert f'data-project-name>{NAME}<' in html, who
        assert f'data-project-address>{ADDR}<' in html, who
        assert "proj-lrt-nav-link" not in html and ">Labor Rate Tracker" not in html, who
        assert "proj-lrt-entry-card" not in html, who
    cs_html = _page(cs, f"/projects/{PROJ}")
    pm_html = _page(pm, f"/projects/{PROJ}")
    assert 'data-group="financial"' in cs_html and 'data-group="financial"' not in pm_html, "#262 split untouched"
    passed += 1; print("  project identity block ok")

    # ---- the names live in the shared sidebar now (#339): Weekly payroll vs Pay rates, old labels gone
    nav = (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")
    assert "'Weekly payroll', '/#labor-rate'" in nav and "'Pay rates', '/admin/labor-rates'" in nav
    assert "Labor Rates" not in nav and "Labor Rate Tracker" not in nav
    html = _page(cs, "/")
    assert 'data-company-nav' in html and ">Labor Rate Tracker<" not in html and ">Labor Rates<" not in html
    assert "Nómina Semanal" in html
    passed += 1; print("  console naming ok")

    # ---- company portal: Projects, never Buildings / Job sites (#339 removed the job-sites page)
    html = _page(cs, "/company")
    assert "label: 'Projects'" in nav and "Buildings" not in nav
    assert "projects: 'Projects'" in html and "buildings: 'Job sites'" not in html and 'id="view-buildings"' not in html
    passed += 1; print("  portal label ok")

    # ---- pay rates page
    html = _page(cs, "/admin/labor-rates")
    assert "<h1>Pay Rates</h1>" in html and "<title>Pay Rates" in html
    assert pm.get("/admin/labor-rates").status_code == 403
    passed += 1; print("  pay rates page ok")

    # ---- stylesheet: the compact-bar rule shipped and the cache key moved
    css = (ROOT / "static" / "css" / "widgets.css").read_text(encoding="utf-8")
    assert ".sidebar .proj-line { display: flex" in css
    # the four shells share ONE cache key, moved at least to #328's (later sweeps move it on)
    import re
    keys = set()
    for page in ("dashboard-static.html", "company-dashboard.html", "company_portal.html", "portal_shell.html"):
        m = re.search(r"widgets\.css\?v=(\d+)", (ROOT / page).read_text(encoding="utf-8"))
        assert m and int(m.group(1)) >= 328, page
        keys.add(m.group(1))
    assert len(keys) == 1, keys
    passed += 1; print("  stylesheet ok")

    print(f"PASS smoke_nav_clarity_328 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
