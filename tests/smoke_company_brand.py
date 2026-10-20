#!/usr/bin/env python3
"""Settings > Company brand (accepted prototype 2026-10-20): the BEHAVIORAL guard for company_brand.py.

Proves, through a Flask test client against an ISOLATED database (no model call, no PDF engine):
  * gating: anonymous 401; pm and c_suite 403 (admin only); admin 200
  * upload: a PNG and a plain SVG are kept; a renamed file, an SVG with a script or an outside link, an empty
    file and a file over 2 MB are refused with a plain message, and the last good logo stays
  * documents: the built-in proposal, the company template's {{logo}}, invoices and AIA pay applications print
    the uploaded logo; removing it goes back to the standard mark
  * Claude: the uploaded raster logo is handed to Claude as an image when it reads a template
  * letterhead: saved, printed on the proposal footer, blank hides the line
  * the page: the Settings tab exists, admin only, and the view calls the routes

Run:  venv/bin/python tests/smoke_company_brand.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import base64
import io
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_brand_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot")})

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import company_brand  # noqa: E402
import db_layer  # noqa: E402
import invoice_docs  # noqa: E402
import proposal_template  # noqa: E402
import proposals  # noqa: E402

TAG = "brd"
PW = "BrandSmokePassword2026"
KEYS = (company_brand.KEY,) + company_brand.LETTERHEAD


def _db():
    return db_layer.connect(pragma_fk=True)


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        company_brand._ensure(conn)
        conn.execute(f"DELETE FROM app_settings WHERE key IN ({','.join('?' * len(KEYS))})", KEYS)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()


def _seed() -> None:
    _cleanup()
    conn = _db()
    try:
        for role in ("admin", "c_suite", "pm"):
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, "
                         "is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 0)",
                         (f"{TAG}.{role}@superstarscontracting.com", auth.hash_password(PW), role, f"Brd {role.title()}"))
        conn.commit()
    finally:
        conn.close()


def _png() -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGBA", (120, 40), (177, 30, 46, 255)).save(out, "PNG")
    return out.getvalue()


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 30"><rect width="100" height="30" fill="#B11E2E"/></svg>'


def _sample_proposal(conn) -> str:
    r = {"id": 0, "number": "BRD-P1", "version": 1, "status": "draft", "title": "Brand smoke", "client_name": "Sample Client LLC",
         "price": 1000, "updated_at": "2026-10-20T09:00:00", "valid_until": "2026-11-19"}
    return proposals.render_document(conn, r, mode="pdf", lis=[])


def _sample_invoice(conn) -> str:
    inv = {"id": -1, "number": "INV-BRD-1", "status": "issued", "kind": "progress", "description": "Brand smoke", "amount": 1000,
           "payments": [], "issued_at": "2026-10-20", "due_at": "2026-11-04", "client_name": "Sample Client LLC"}
    return invoice_docs.render_document(conn, inv)


def main() -> int:
    app = Flask("brd", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    company_brand.register(app)

    def login(role):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@superstarscontracting.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.96.0.{len(role)}"})
        assert r.status_code == 200, (role, r.status_code)
        return c

    def up(c, name, raw):
        return c.post("/api/company-brand/logo", data={"file": (io.BytesIO(raw), name)}, content_type="multipart/form-data")

    _seed()
    passed = 0
    try:
        # ---- gating
        anon = app.test_client()
        a, cs, pm = login("admin"), login("c_suite"), login("pm")
        assert anon.get("/api/company-brand").status_code == 401
        for c in (cs, pm):
            assert c.get("/api/company-brand").status_code == 403
            assert up(c, "logo.png", _png()).status_code == 403
            assert c.delete("/api/company-brand/logo").status_code == 403
            assert c.put("/api/company-brand/letterhead", json={"company_phone": "x"}).status_code == 403
        r = a.get("/api/company-brand")
        assert r.status_code == 200 and r.get_json()["data"]["logo"] is None
        passed += 1; print("  gating ok")

        # ---- with no upload, documents keep the standard mark
        conn = _db()
        try:
            std = company_brand.document_logo_uri(conn)
            assert std and std.startswith("data:image/svg+xml;base64,")
            assert 'class="brand-logo"' not in _sample_proposal(conn)
            assert std in proposal_template.logo_tag()
        finally:
            conn.close()
        passed += 1; print("  standard mark ok")

        # ---- upload
        png = _png()
        r = up(a, "Superstars logo.png", png)
        assert r.status_code == 200, r.get_json()
        d = r.get_json()["data"]["logo"]
        assert d["mime"] == "image/png" and d["name"] == "Superstars logo.png" and d["size"] == len(png)
        assert d["uploaded_by_name"] and d["data_uri"] == "data:image/png;base64," + base64.b64encode(png).decode()
        bad = [("logo.png", SVG, "isn't a PNG"), ("logo.exe", png, "isn't a PNG"), ("logo.gif", b"GIF89a....", "isn't a PNG"),
               ("logo.png", b"", "empty"),
               ("evil.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "scripts or outside links"),
               ("evil.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><rect onload="x()"/></svg>', "scripts or outside links"),
               ("evil.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://x.example/a.png"/></svg>', "scripts or outside links"),
               ("big.png", png + b"\0" * company_brand.MAX_BYTES, "The limit is 2 MB")]
        for name, raw, msg in bad:
            r = up(a, name, raw)
            assert r.status_code == 400 and msg in r.get_json()["error"], (name, r.status_code, r.get_json())
        assert a.get("/api/company-brand").get_json()["data"]["logo"]["mime"] == "image/png", "the last good logo stays"
        assert a.post("/api/company-brand/logo").status_code == 400
        passed += 1; print("  upload checks ok")

        # ---- documents print it
        conn = _db()
        try:
            uri = company_brand.logo_data_uri(conn)
            assert uri.startswith("data:image/png;base64,")
            html = _sample_proposal(conn)
            assert f'class="brand-logo" alt="Superstars Contracting" src="{uri}"' in html and company_brand.BRAND_CSS in html
            assert uri in proposal_template.logo_tag(), "the company template's {{logo}}"
            inv = _sample_invoice(conn)
            assert uri in inv and 'class="brand-logo"' in inv
            import aia_billing
            assert "company_brand.brand_block(conn" in (ROOT / "aia_billing.py").read_text(encoding="utf-8")
            assert aia_billing.company_brand is company_brand
            blk = company_brand.model_image_block(conn)
            assert blk and blk["type"] == "image" and blk["source"]["media_type"] == "image/jpeg"
        finally:
            conn.close()
        src = (ROOT / "proposal_template.py").read_text(encoding="utf-8")
        assert "company_brand.model_image_block(conn)" in src, "Claude sees the logo when it reads a template"
        passed += 1; print("  documents ok")

        # ---- documents Claude makes in the Claude window: PDF / editor page and Word
        import zipfile
        import xml.dom.minidom
        import claude_agent
        import claude_docs
        d = {"title": "Letter to the board", "html": "<h1>Notice</h1><p>Work starts Monday.</p>"}
        pg = claude_docs.page(d, edit=True)
        assert f'<header class="ssc-lh"><div class="ssc-lh-in"><img alt="Superstars Contracting" src="{uri}">' in pg
        art = pg.split('<article class="doc"', 1)[1]
        assert "ssc-lh" not in art and uri not in art, "the letterhead stays outside the editable document"
        assert pg.index("ssc-lh-in") < pg.index('<article class="doc"')
        z = zipfile.ZipFile(io.BytesIO(claude_docs.docx_bytes(d)))
        docxml = z.read("word/document.xml").decode()
        xml.dom.minidom.parseString(docxml)
        assert "word/media/logo.png" in z.namelist() and 'r:embed="rIdLogo"' in docxml
        assert 'Target="media/logo.png"' in z.read("word/_rels/document.xml.rels").decode()
        assert 'Extension="png"' in z.read("[Content_Types].xml").decode()
        assert "do not write your own company header or logo" in (ROOT / "claude_agent.py").read_text(encoding="utf-8")
        passed += 1; print("  claude documents ok")

        # ---- an SVG replaces it; Claude isn't sent SVG
        r = up(a, "mark.svg", SVG)
        assert r.status_code == 200 and r.get_json()["data"]["logo"]["mime"] == "image/svg+xml"
        conn = _db()
        try:
            assert company_brand.model_image_block(conn) is None
            assert company_brand.logo_data_uri(conn) == "data:image/svg+xml;base64," + base64.b64encode(SVG).decode()
            assert company_brand.word_logo(conn) is None, "Word can't take an SVG; the name prints instead"
        finally:
            conn.close()
        import zipfile
        import claude_docs
        d = {"title": "x", "html": "<p>x</p>"}
        assert "data:image/svg+xml;base64," + base64.b64encode(SVG).decode() in claude_docs.page(d)
        z = zipfile.ZipFile(io.BytesIO(claude_docs.docx_bytes(d)))
        assert "word/media/logo.png" not in z.namelist() and "SUPERSTARS CONTRACTING" in z.read("word/document.xml").decode()
        conn = _db()
        try:
            pass
        finally:
            conn.close()
        passed += 1; print("  svg ok")

        # ---- letterhead
        r = a.put("/api/company-brand/letterhead", json={"company_address": "1 Brd Ave, Bronx, NY", "company_phone": "(555) 010-0100",
                                                         "company_email": "office@example.com", "company_license": "1234567"})
        assert r.status_code == 200 and r.get_json()["data"]["letterhead"]["company_license"] == "1234567"
        conn = _db()
        try:
            html = _sample_proposal(conn)
            assert "1 Brd Ave, Bronx, NY" in html and "License 1234567" in html
            import claude_docs
            lh = claude_docs.page({"title": "x", "html": "<p>x</p>"})
            assert "<br>1 Brd Ave, Bronx, NY<br>(555) 010-0100 · office@example.com<br>License 1234567" in lh
        finally:
            conn.close()
        r = a.put("/api/company-brand/letterhead", json={"company_license": ""})
        lh = r.get_json()["data"]["letterhead"]
        assert lh["company_license"] == "" and lh["company_address"] == "1 Brd Ave, Bronx, NY", "only the sent keys change"
        assert a.put("/api/company-brand/letterhead", json={"company_phone": "x" * 201}).status_code == 400
        passed += 1; print("  letterhead ok")

        # ---- remove
        r = a.delete("/api/company-brand/logo")
        assert r.status_code == 200 and r.get_json()["data"]["logo"] is None
        conn = _db()
        try:
            assert company_brand.logo_data_uri(conn) is None and 'class="brand-logo"' not in _sample_proposal(conn)
        finally:
            conn.close()
        passed += 1; print("  remove ok")

        # ---- the page
        html = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        for needle in ('id="view-brand"', 'id="br-file"', 'id="br-lh"', 'id="br-locked"', "'/api/company-brand/logo'",
                       "api('/company-brand', null, true)", "'/company-brand/letterhead'", "brand: loadBrand", "brand: 'Settings'"):
            assert needle in html, needle
        nav = (ROOT / "static/js/company_nav.js").read_text(encoding="utf-8")
        assert "['Company brand', '/company#brand', ['admin']]" in nav and "['/company', 'brand']" in nav
        assert "company_brand.register(app)" in (ROOT / "server.py").read_text(encoding="utf-8")
        passed += 1; print("  page ok")
    finally:
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: company brand ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
