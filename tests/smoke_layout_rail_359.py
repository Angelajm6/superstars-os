#!/usr/bin/env python3
"""#359 — collapsible sidebar rail + layout base. Guard.

Boots the real app on an ISOLATED database. Proves:
  * shell.js offers the rail on the company shell and on an <aside data-rail> only,
    remembers it under ssc.side.rail, and turns labels into tooltips
  * widgets.css keeps every rail rule inside the desktop media query (the phone Menu
    drawer is untouched) and the rail button is hidden by default
  * every internal page applies a remembered rail in <head>, before shell.js, so a
    collapsed rail never flashes open; the client portal has no rail
  * all eight shells share ONE widgets.css key and ONE shell.js key
  * the served project dashboard opts in (admin and pm), its views fill the window
    width (no 1440 px cap since 2026-10-10), and the dead .topbar rules are gone
Run:  venv/bin/python tests/smoke_layout_rail_359.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_rail_359_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "rail359"
PW = "RailLayout2026"
P = "RAIL359-P"
INTERNAL = ("admin_labor_rates.html", "admin_projects.html", "admin_users.html", "company-dashboard.html",
            "company_portal.html", "dashboard-static.html", "estimating.html")
PRE = "document.documentElement.classList.add('side-rail')"


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed():
    conn = _db()
    try:
        purge(conn, "pm_project_assignment", "project_code", [P])
        purge(conn, "projects", "project_code", [P])
        conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, 'Rail359', '1 Rail St', 'active')", (P,))
        ids = {}
        for role in ("admin", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Smoke {role} Rail"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, '2026-09-29')", (ids["pm"], P))
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        purge(conn, "pm_project_assignment", "project_code", [P])
        purge(conn, "projects", "project_code", [P])
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


def main() -> int:
    passed = 0

    # ---- shell.js
    js = (ROOT / "static" / "js" / "shell.js").read_text(encoding="utf-8")
    assert "ssc.side.rail" in js and "ssc-rail-btn" in js
    assert "side.classList.contains('ssc-side') || side.hasAttribute('data-rail')" in js, "opt-in: company shell or data-rail"
    assert "aria-label" in js and "data-rail-tip" in js, "labels become tooltips / accessible names"
    try:
        r = subprocess.run(["node", "--check", str(ROOT / "static" / "js" / "shell.js")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    except FileNotFoundError:
        pass   # no node on this box — the gate's other runners still parse the file in the browser tests
    passed += 1; print("  shell.js ok")

    # ---- widgets.css: rail rules only above the phone breakpoint
    css = (ROOT / "static" / "css" / "widgets.css").read_text(encoding="utf-8")
    i = css.index("#359 — collapsible icon rail")
    block = css[css.index(".ssc-rail-btn {", i):]          # from the first rule (the header comment names the class too)
    assert re.search(r"\.ssc-rail-btn \{ display: none;", block), "hidden by default (phones)"
    media = block.index("@media (min-width: 861px) {")
    rules = [m.start() for m in re.finditer(r"html\.side-rail", block)]
    assert rules and all(r > media for r in rules), "every rail rule sits inside the desktop media query"
    assert "html.side-rail .ssc-shell { grid-template-columns: 68px" in block
    assert "html.side-rail:not(.side-peek) .sidebar[data-rail] { width: 68px" in block
    assert "html.side-rail .sidebar " not in block and "html.side-rail:not(.side-peek) .sidebar " not in block, "only opted-in .sidebar"
    passed += 1; print("  widgets.css ok")

    # ---- pages: pre-paint in <head> before shell.js; one key each
    wkeys, skeys = set(), set()
    for page in INTERNAL + ("portal_shell.html",):
        html = (ROOT / page).read_text(encoding="utf-8")
        wkeys.add(re.search(r"widgets\.css\?v=(\w+)", html).group(1))
        skeys.add(re.search(r"shell\.js\?v=(\w+)", html).group(1))
        if page == "portal_shell.html":
            assert PRE not in html, "the client portal has no rail"
            continue
        head_end, pre, shell = html.index("</head>"), html.index(PRE), html.index("shell.js?v=")
        assert pre < shell < head_end, page
    assert len(wkeys) == 1 and len(skeys) == 1, (wkeys, skeys)
    passed += 1; print("  pages ok")

    # ---- served dashboard (admin + pm)
    _seed()
    for role in ("admin", "pm"):
        page = _login(role).get(f"/projects/{P}").get_data(as_text=True)
        assert '<aside class="sidebar" data-rail>' in page, role
        assert PRE in page, role
    dash = (ROOT / "dashboard-static.html").read_text(encoding="utf-8")
    # full window width since 2026-10-10 (the 1440px cap was removed on request); overview cards stay at 3 columns max
    assert re.search(r"\.view \{ display: none; padding: [^}]*\}", dash) and "max-width: 1440px" not in dash
    assert "minmax(max(340px, calc((100% - 32px) / 3)), 1fr)" in dash
    assert '.view[data-view="drawings"] { padding: 0; max-width: none; }' in dash, "the markup canvas stays full-bleed"
    assert ".topbar {" not in dash and ".topbar-meta" not in dash, "dead top-bar rules removed"
    passed += 1; print("  dashboard ok")

    print(f"PASS smoke_layout_rail_359 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_layout_rail_359: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
