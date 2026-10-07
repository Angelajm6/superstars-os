#!/usr/bin/env python3
"""One title per page + the pinnable sidebar. Guard.

Every company page used to say its title twice in the top bar (a small "DASHBOARD"
label over "Dashboard"; "CLIENTS" over "Clients · Client portal"), and two pages opened
with a heading that repeated the sub-tab under it. And the sidebar could only be
collapsed for good. Boots the real app on an ISOLATED database. Proves:

  * titles: no page carries the small repeating label (data-nav-crumb / .ssc-crumb);
    every company page has exactly ONE data-nav-h1; company_nav sets it to the sidebar
    section; the portal router's titles are sidebar section names only (the sub-tab
    row names the page); no portal view opens with a heading equal to one of its own
    tab's sub-tab labels
  * pin: shell.js offers a pin (aria-pressed, "Pin sidebar open" / "Unpin sidebar");
    unpinned = html.side-rail; hover or keyboard focus slides it open as html.side-peek,
    leaving / focus out / Escape folds it back; the choice is still ssc.side.rail
  * widgets.css: every collapse rule is html.side-rail:not(.side-peek) and inside the
    desktop media query; the peek rules overlay the page (68 px column / negative margin
    keep content still); the pin hides in the closed rail
  * every shell shares one widgets.css / shell.js / company_nav.js / shell.css key, moved
    off the pre-change keys so browsers fetch the new files
Run:  venv/bin/python tests/smoke_shell_titles_pin.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_titles_pin_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "titlepin"
PW = "TitlesPinSmoke2026"
COMPANY_PAGES = {"/company": "company_portal.html", "/": "company-dashboard.html", "/admin/users": "admin_users.html",
                 "/admin/projects": "admin_projects.html", "/admin/labor-rates": "admin_labor_rates.html",
                 "/estimating": "estimating.html"}
SHELLS = ("admin_labor_rates.html", "admin_projects.html", "admin_users.html", "company-dashboard.html",
          "company_portal.html", "dashboard-static.html", "estimating.html", "portal_shell.html")


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed():
    conn = _db()
    try:
        email = f"{TAG}.admin@example.com"
        conn.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                     "VALUES (?, ?, 'admin', 'Smoke admin', 1, 'active', 0, 1)", (email, auth.hash_password(PW)))
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def main() -> int:
    _seed()
    passed = 0
    nav = (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")
    tabs_src = nav[:nav.index("var PINNED")]
    sections = set(re.findall(r"\{ key: '[a-z]+', label: '([^']+)'", tabs_src)) | {"Settings"}

    # ---- titles: one per page
    c = server.app.test_client()
    assert c.post("/api/auth/login", json={"email": f"{TAG}.admin@example.com", "password": PW}).status_code == 200
    for path, fname in COMPANY_PAGES.items():
        src = (ROOT / fname).read_text(encoding="utf-8")
        served = c.get(path).get_data(as_text=True)
        for html, where in ((src, fname), (served, path)):
            assert "data-nav-crumb" not in html and "ssc-crumb" not in html, (where, "no small label repeating the title")
        assert src.count("data-nav-h1") == 1, (fname, src.count("data-nav-h1"))
    fn = nav[nav.index("function renderTitles()"):nav.index("function refresh()")]
    assert "crumb" not in fn and "h1.textContent = cur ? cur.label" in fn, "the h1 is the sidebar section"
    assert "ssc-crumb" not in (ROOT / "static" / "css" / "shell.css").read_text(encoding="utf-8")
    portal = (ROOT / "company_portal.html").read_text(encoding="utf-8")
    titles = dict(re.findall(r"(\w+): '([^']+)'", re.search(r"const TITLES = \{([^}]*)\}", portal).group(1)))
    assert titles and set(titles.values()) <= sections, set(titles.values()) - sections
    assert not any("·" in v for v in titles.values()), "no 'Section · Page' titles"
    # a portal view must not open with a heading equal to one of its tab's sub-tab labels
    subs = {k: [s for s in re.findall(r"\['([^']+)', '[^']+'", body)]
            for k, body in re.findall(r"\{ key: '([a-z]+)'[^\n]*\n(?:[^\n]*\n){0,3}?\s*sub: \[(.*?)\] \}", tabs_src)}
    assert subs.get("finance") and subs.get("workforce"), subs
    for key, labels in subs.items():
        for m in re.finditer(r'<section class="cp-view" id="view-([a-z]+)"[^>]*>(.*?)</section>', portal, re.S):
            if m.group(1) not in (key,) and not (key == "finance" and m.group(1) == "expenses"):
                continue
            heads = re.findall(r"<h2[^>]*>([^<]+)</h2>", m.group(2))
            dup = [h for h in heads if h.strip().lower() in {x.lower() for x in labels}]
            assert not dup, (m.group(1), dup)
    assert "<h2>Next 30 days</h2>" in portal and "<h2>Roster</h2>" in portal
    passed += 1; print("  titles ok")

    # ---- pin + slide-open
    js = (ROOT / "static" / "js" / "shell.js").read_text(encoding="utf-8")
    for needle in ("'Pin sidebar open'", "'Unpin sidebar'", "aria-pressed", "side-peek", "'mouseenter'", "'mouseleave'",
                   "'focusin'", "'focusout'", "e.key === 'Escape'", "ssc.side.rail"):
        assert needle in js, needle
    assert "Collapse sidebar" not in js and "Expand sidebar" not in js, "the old collapse wording is gone"
    try:
        r = subprocess.run(["node", "--check", str(ROOT / "static" / "js" / "shell.js")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        r = subprocess.run(["node", "--check", str(ROOT / "static" / "js" / "company_nav.js")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    except FileNotFoundError:
        pass
    passed += 1; print("  pin ok")

    # ---- css
    css = (ROOT / "static" / "css" / "widgets.css").read_text(encoding="utf-8")
    i = css.index("#359 — collapsible icon rail")
    block = css[css.index("@media (min-width: 861px) {", i):]
    block = block[:block.index("\n}\n") + 2]
    collapse = [ln for ln in block.splitlines() if "html.side-rail" in ln and "side-peek" not in ln]
    assert collapse == ["  html.side-rail .ssc-shell { grid-template-columns: 68px minmax(0, 1fr); }"], collapse
    assert "html.side-rail:not(.side-peek) .ssc-rail-btn { display: none; }" in block
    assert "html.side-rail.side-peek .ssc-side { width: 236px; z-index: 40;" in block
    assert "html.side-rail.side-peek .sidebar[data-rail] { width: 256px; margin-right: -188px; z-index: 40;" in block
    passed += 1; print("  css ok")

    # ---- one fresh key per asset across the shells
    for asset, old in (("widgets.css", "359"), ("shell.js", "359")):
        keys = {re.search(re.escape(asset) + r"\?v=(\w+)", (ROOT / f).read_text(encoding="utf-8")).group(1) for f in SHELLS}
        assert len(keys) == 1 and old not in keys, (asset, keys)
    for asset, old in (("company_nav.js", "360"), ("shell.css", "341")):
        keys = {m for f in SHELLS for m in re.findall(re.escape(asset) + r"\?v=(\w+)", (ROOT / f).read_text(encoding="utf-8"))}
        assert len(keys) == 1 and old not in keys, (asset, keys)
    passed += 1; print("  asset keys ok")

    print(f"PASS smoke_shell_titles_pin ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_shell_titles_pin: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
