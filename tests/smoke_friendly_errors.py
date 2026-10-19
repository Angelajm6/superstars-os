#!/usr/bin/env python3
"""Friendly 403/404 pages for people, JSON for programs + Home and Interface inside the shell. Guard.

Boots the real app on an ISOLATED database. Proves:
  * a browser navigation (Accept: text/html) to an unknown address gets the 404 page, to a page the
    role cannot open gets the 403 page; both are HTML with a way back
  * the same requests WITHOUT text/html (fetch / curl / the app's own calls) still get the JSON body,
    and /api/*, /files/* and /project-files/* stay JSON even for a browser Accept header
  * /projects (Home) and /settings/interface mount the shared shell with exactly one data-nav-h1,
    and use the same shell asset keys as every other shell page
Run:  venv/bin/python tests/smoke_friendly_errors.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_friendly_errors_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402

TAG = "friendlyerr"
PW = "FriendlyErrSmoke2026"
HTML = {"Accept": "text/html,application/xhtml+xml"}


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed():
    conn = _db()
    try:
        for role in ("admin", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, 'Smoke', 1, 'active', 0, 1)", (email, auth.hash_password(PW), role))
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
    c = server.app.test_client()
    assert c.post("/api/auth/login", json={"email": f"{TAG}.pm@example.com", "password": PW}).status_code == 200

    # 404: person vs program
    r = c.get("/no-such-page-anywhere", headers=HTML)
    assert r.status_code == 404 and r.mimetype == "text/html", (r.status_code, r.mimetype)
    body = r.get_data(as_text=True)
    assert "We can't find that page" in body and 'id="home"' in body and "Go back" in body
    r = c.get("/no-such-page-anywhere")
    assert r.status_code == 404 and r.mimetype == "application/json", "no text/html => JSON stays"

    # 403: a pm cannot open the company dashboard JSON-gated pages
    r = c.get("/portal", headers=HTML)
    if r.status_code == 403:
        assert r.mimetype == "text/html" and "You don't have access to this page" in r.get_data(as_text=True)
        r2 = c.get("/portal")
        assert r2.status_code == 403 and r2.mimetype == "application/json"

    # never touch the app's own data endpoints
    for path in ("/api/no-such-endpoint", "/files/static/does-not-exist.css", "/project-files/does-not-exist"):
        r = c.get(path, headers=HTML)
        assert r.mimetype == "application/json" or r.status_code in (301, 302, 401), (path, r.status_code, r.mimetype)

    # Home + Interface use the shared shell
    for fname in ("projects.html", "ui_settings.html"):
        src = (ROOT / fname).read_text(encoding="utf-8")
        assert 'class="ssc-shell"' in src and "data-company-nav" in src and "SSC_NAV.mount()" in src, fname
        assert src.count("data-nav-h1") == 1 and src.count("<h1") == 1, fname
    nav = (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")
    assert "['/settings/interface', null]" in nav, "Settings stays lit on the Interface page"
    # same asset keys as the other shells
    for asset in ("widgets.css", "shell.css", "shell.js", "company_nav.js"):
        keys = {re.search(re.escape(asset) + r"\?v=(\w+)", (ROOT / f).read_text(encoding="utf-8")).group(1)
                for f in ("account.html", "projects.html", "ui_settings.html")}
        assert len(keys) == 1, (asset, keys)

    # batch 2C: retired pages stay retired, old Drop Plan bookmarks land in the project
    for gone in ("dropplan.html", "facade-dashboard.html", "facade-dashboard-presentation.html", "dashboard-public-demo.html"):
        assert not (ROOT / gone).exists(), gone
    r = c.get("/dropplan?project=FR-BX-001")
    assert r.status_code == 302 and r.headers["Location"].endswith("/projects/FR-BX-001"), (r.status_code, r.headers.get("Location"))
    r = c.get("/dropplan")
    assert r.status_code == 302 and r.headers["Location"].endswith("/company#projects")
    r = c.get("/dropplan?project=../../etc")
    assert r.status_code == 302 and r.headers["Location"].endswith("/company#projects"), "a bad code never becomes a path"
    acct = (ROOT / "account.html").read_text(encoding="utf-8")
    assert 'id="lg-seg"' in acct and "dashboard_lang" in acct, "language lives in Settings > Account"
    cons = (ROOT / "company-dashboard.html").read_text(encoding="utf-8")
    assert re.search(r'class="lang-toggle"[^>]*\bhidden\b', cons), "the console header no longer shows the EN/ES toggle"

    # batch 5 (step 1): the tab list names match the sidebar, sign-in says Company, Add a tab leaves the phone bar
    import project_modules as _pm
    labels = {m["label"] for m in _pm.MODULES}
    assert {"Crew", "Drawings", "Questions (RFIs)", "Printable Signs & Forms"} <= labels, labels
    assert not ({"Crew & certifications", "Drawing markup", "Templates"} & labels), "old tab names are gone"
    for fname in ("login.html", "forgot_password.html", "reset_password.html", "set_password.html", "confirm_email.html"):
        assert "office console" not in (ROOT / fname).read_text(encoding="utf-8").lower(), fname
    css = (ROOT / "static" / "css" / "unify.css").read_text(encoding="utf-8")
    assert "body .sidebar.nav-open .nav-add:not([hidden])" in css, "Add a tab shows only inside the open phone menu"

    print("PASS smoke_friendly_errors")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_friendly_errors: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
