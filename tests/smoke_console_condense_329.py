#!/usr/bin/env python3
"""#329 — Office Console condensed to 12 sidebar entries: behavioral guard.

Boots the real app (`import server`) on an ISOLATED database. Proves, on the console
served to a c_suite user:
  * the sidebar has exactly 12 anchors and none of the removed ones (Estimating queue,
    Cert Health, Specs Library, Drop Plan, Interface)
  * one Certifications entry owns cert-health + cert-library; Compliance owns specs
    (data-panels) and both grouped panels carry the sub-tabs; the specs panel still exists
  * the Estimating queue link moved into the Estimates header; Interface into Settings
  * the cert-health placeholder panel is gone and the JS aliases its hash
  * the four admin pages' own sidebars match (Certifications; no Cert Health / Specs
    Library / Drop Plan; the queue entry is estimator-only) and /estimating still serves
Run:  venv/bin/python tests/smoke_console_condense_329.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_con_329_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "con329"
PW = "CondenseSmoke2026"
EMAIL = f"{TAG}.c_suite@example.com"


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed() -> None:
    conn = _db()
    try:
        conn.execute("DELETE FROM users WHERE email = ?", (EMAIL,))
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                     "must_reset_password, is_system) VALUES (?, ?, 'c_suite', 'Smoke c_suite', 1, 'active', 0, 1)",
                     (EMAIL, auth.hash_password(PW)))
        conn.commit()
    finally:
        conn.close()


def _cleanup() -> None:
    conn = _db()
    try:
        try:
            conn.execute("DELETE FROM login_audit WHERE ip LIKE '10.29.%'")
        except Exception:
            conn.rollback()
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email = ?", (EMAIL,)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _nav(html: str) -> str:
    m = re.search(r'<nav class="ssc-nav" id="ssc-nav">(.*?)</nav>', html, re.S)
    assert m, "console nav block"
    return m.group(1)


def main() -> int:
    _seed()
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": EMAIL, "password": PW}, environ_base={"REMOTE_ADDR": "10.29.0.1"})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    passed = 0

    html = c.get("/", follow_redirects=True).get_data(as_text=True)
    nav = _nav(html)
    anchors = re.findall(r"<a [^>]*>", nav)
    assert len(anchors) == 12, f"expected 12 sidebar entries, got {len(anchors)}"
    labels = [re.sub(r"<[^>]+>", "", a).strip() for a in re.findall(r"<a [^>]*>.*?</a>", nav, re.S)]
    for gone in ("Estimating queue", "Cert Health", "Cert Library", "Specs Library", "Drop Plan", "Interface"):
        assert gone not in labels, (gone, labels)
    for kept in ("Overview", "Company Portal", "CRM", "Estimates", "Workforce", "Hours Log", "Weekly Payroll",
                 "Pay Rates", "Projects & assignments", "Compliance", "Certifications", "Settings"):
        assert kept in labels or kept.replace("&", "&amp;") in labels, (kept, labels)
    assert 'href="/estimating"' not in nav and 'href="/dropplan"' not in nav and 'href="/settings/interface"' not in nav
    passed += 1; print("  sidebar: 12 entries ok")

    assert 'data-panel="cert-library" data-panels="cert-health,cert-library"' in nav
    assert 'data-panel="compliance" data-panels="compliance,specs"' in nav
    assert 'data-panel="cert-health"' not in html.replace(nav, ""), "cert-health placeholder panel removed"
    assert "if (target === 'cert-health') target = 'cert-library';" in html
    assert '<div class="panel" data-panel="specs">' in html and '<div class="panel" data-panel="compliance">' in html
    assert html.count('class="ssc-subtabs"') == 2, "both grouped panels carry the sub-tabs"
    assert 'class="ssc-subtab active" href="#compliance"' in html and 'class="ssc-subtab active" href="#specs"' in html
    passed += 1; print("  grouped panels ok")

    est = html[html.find('<div class="panel" data-panel="estimates">'):]
    est = est[:est.find('id="est-subtabs"')]
    assert 'href="/estimating"' in est, "queue link inside the Estimates header"
    st = html[html.find('<div class="panel" data-panel="settings">'):]
    st = st[:st.find('id="twofa-section"')]
    assert 'href="/settings/interface"' in st and 'id="interface-section"' in st
    passed += 1; print("  moved links ok")

    for page in ("/admin/projects", "/admin/users", "/estimating", "/admin/labor-rates"):
        h = c.get(page, follow_redirects=True).get_data(as_text=True)
        assert ">Certifications</a>" in h, page
        for gone in (">Cert Health</a>", ">Cert Library</a>", ">Specs Library</a>", ">Drop Plan</a>"):
            assert gone not in h, (page, gone)
        m = re.search(r'<a href="/estimating"[^>]*>Estimating queue</a>', h)
        assert m and 'data-roles="estimator"' in m.group(0), (page, m and m.group(0))
    assert c.get("/estimating").status_code == 200 and c.get("/settings/interface").status_code == 200
    passed += 1; print("  admin sidebars ok")

    print(f"PASS smoke_console_condense_329 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
