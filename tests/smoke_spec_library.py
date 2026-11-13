#!/usr/bin/env python3
"""Company → Specs revamp (2026-11-13): go-to list, notes, data sheets, the jobs a product is on, submittal package.

Flask test client on an ISOLATED database (honors SSC_DB_URL; builds a throwaway SQLite snapshot when unset), Claude is
the fake model. Proves:

  * schema: ensure_schema runs twice cleanly; the catalog is there; Sika lines carry their one-line summary
  * gates: the catalog routes are admin / C-suite (pm -> 403); a pm reads only an assigned job's products
  * catalog payload: files + jobs per product, never a file_path
  * add product: blank name, bad link, duplicate refused; a new one is created; go-to + notes save
  * data sheets: a non-PDF is refused; a PDF saves, reads back, a replace keeps one current file; remove works
  * a job: attach with "where it's used" (201, then 200 on repeat), edit it, read it with the job-scoped file link;
    a product not on the job has no file there; detach
  * submittal package: one PDF, cover sheet + the data sheet pages; refused for a job with no products
  * Claude: the fake read of a data sheet fills the form; the agent's spec tools find the product and its sheet
  * page needles: the Specs panel keeps the Company frame (same margins), the old table is gone, the job's
    Documents tab carries the products block

Run:  venv/bin/python tests/smoke_spec_library.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_specs_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_ASSISTANT_FAKE": "1"})
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)

import pypdf  # noqa: E402
from flask import Flask  # noqa: E402

import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import db_layer  # noqa: E402
import pm_scoping  # noqa: E402
import role_containment  # noqa: E402
import spec_library  # noqa: E402

TAG = "specsmoke"
MFR = "SpecSmoke Co"
PW = "SpecLibrarySmokePassword2026"
JOB, OTHER = "SPECSMK-A", "SPECSMK-B"


def _pdf(pages=1) -> bytes:
    w = pypdf.PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=612, height=792)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        spec_library.ensure_schema(conn)
        pids = [r["id"] for r in conn.execute("SELECT id FROM spec_products WHERE manufacturer = ?", (MFR,)).fetchall()]
        if pids:
            ph = ",".join("?" * len(pids))
            conn.execute(f"DELETE FROM spec_product_file WHERE spec_product_id IN ({ph})", pids)
            conn.execute(f"DELETE FROM project_document_specs WHERE spec_product_id IN ({ph})", pids)
            conn.execute(f"DELETE FROM spec_products WHERE id IN ({ph})", pids)
        conn.execute("DELETE FROM project_document_specs WHERE project_code IN (?, ?)", (JOB, OTHER))
        conn.execute("DELETE FROM pm_project_assignment WHERE project_code IN (?, ?)", (JOB, OTHER))
        conn.execute("DELETE FROM projects WHERE project_code IN (?, ?)", (JOB, OTHER))
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()


def _seed() -> dict:
    _cleanup()
    conn = db_layer.connect(pragma_fk=True)
    try:
        for role in ("admin", "c_suite", "pm"):
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, "
                         "is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 0)",
                         (f"{TAG}.{role}@superstarscontracting.com", auth.hash_password(PW), role, f"Spec {role.title()}"))
        for code in (JOB, OTHER):
            conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, ?, ?, 'active')",
                         (code, f"Spec {code}", "7 Specsmoke Pl, Bronx"))
        conn.commit()
        ids = {r["role"]: r["id"] for r in conn.execute("SELECT id, role FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()}
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, '2026-11-01')",
                     (ids["pm"], JOB))
        conn.commit()
        return ids
    finally:
        conn.close()


def main() -> int:
    app = Flask("specs", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    role_containment.register(app)
    pm_scoping.register(app)
    spec_library.register(app)
    ids = _seed()
    checks = 0

    def login(role):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@superstarscontracting.com", "password": PW})
        assert r.status_code == 200, (role, r.status_code)
        return c

    a, cs, pm = login("admin"), login("c_suite"), login("pm")
    conn = db_layer.connect()
    try:
        # ---- schema: twice, catalog there, line summaries
        spec_library.ensure_schema(conn)
        spec_library.ensure_schema(conn)
        n = conn.execute("SELECT COUNT(*) AS n FROM spec_products").fetchone()["n"]
        assert n >= 100, n
        r = conn.execute("SELECT description FROM spec_products WHERE manufacturer = 'Sika' AND product_line = 'Sika MonoTop'").fetchone()
        assert r and r["description"] == spec_library.LINE_SUMMARY["Sika MonoTop"], r
        checks += 1

        # ---- gates + payload
        assert pm.get("/api/spec-products").status_code == 403
        assert pm.post("/api/spec-products", json={"product_name": "x"}).status_code == 403
        assert pm.patch("/api/spec-products/1", json={"is_favorite": True}).status_code == 403
        d = a.get("/api/spec-products?manufacturer=").get_json()["data"]
        assert d["count"] == n and d["categories"] and "file_path" not in str(d)
        it = d["items"][0]
        assert set(it["files"]) == {"tds", "sds"} and isinstance(it["jobs"], list) and isinstance(it["is_favorite"], bool)
        assert cs.get("/api/spec-products").status_code == 200   # C-suite reads it too
        checks += 1

        # ---- add product
        cat = d["categories"][0]
        assert a.post("/api/spec-products", json={"manufacturer": MFR, "product_name": "  "}).status_code == 400
        bad = a.post("/api/spec-products", json={"manufacturer": MFR, "product_name": "Smoke Seal 1", "spec_url": "ftp://x"})
        assert bad.status_code == 400 and "https" in bad.get_json()["error"]
        assert a.post("/api/spec-products", json={"manufacturer": MFR, "product_name": "Smoke Seal 1", "category": "Made up"}).status_code == 400
        r = a.post("/api/spec-products", json={"manufacturer": MFR, "product_name": "Smoke Seal 1", "category": cat,
                                                "product_line": "Smoke", "description": "Test sealant",
                                                "spec_url": "https://example.com/smoke-seal"})
        assert r.status_code == 201, r.get_json()
        p1 = r.get_json()["data"]
        assert p1["product_name"] == "Smoke Seal 1" and p1["files"] == {"tds": None, "sds": None} and p1["jobs"] == []
        dup = a.post("/api/spec-products", json={"manufacturer": MFR.lower(), "product_name": "smoke seal 1", "category": cat})
        assert dup.status_code == 400 and "already" in dup.get_json()["error"]
        p2 = a.post("/api/spec-products", json={"manufacturer": MFR, "product_name": "Smoke Patch 2",
                                                 "category": spec_library.OTHER_CATEGORY}).get_json()["data"]
        u = a.patch(f"/api/spec-products/{p1['id']}", json={"is_favorite": True, "notes": "  Gun-grade on verticals  "}).get_json()["data"]
        assert u["is_favorite"] is True and u["notes"] == "Gun-grade on verticals"
        assert a.patch("/api/spec-products/999999999", json={"notes": "x"}).status_code == 404
        checks += 1

        # ---- data sheets
        up = lambda pid, data, name, kind="tds": a.post(f"/api/spec-products/{pid}/files", content_type="multipart/form-data",  # noqa: E731
                                                       data={"kind": kind, "file": (io.BytesIO(data), name)})
        assert up(p1["id"], b"hello", "sheet.txt").status_code == 400
        assert up(p1["id"], b"not really a pdf", "sheet.pdf").status_code == 400
        assert up(p1["id"], _pdf(), "sheet.pdf", kind="msds").status_code == 400
        r = up(p1["id"], _pdf(2), "Smoke-Seal_TDS.pdf")
        assert r.status_code == 201, r.get_json()
        f = r.get_json()["data"]
        assert f["file_name"] == "Smoke-Seal_TDS.pdf" and "file_path" not in f and f["file_url"].endswith(f"/{p1['id']}/files/tds")
        g = a.get(f["file_url"])
        assert g.status_code == 200 and g.data.startswith(b"%PDF-") and g.headers["Content-Type"] == "application/pdf"
        assert pm.get(f["file_url"]).status_code == 403
        assert up(p1["id"], _pdf(2), "Smoke-Seal_TDS_v2.pdf").status_code == 201   # replace
        rows = conn.execute("SELECT file_name FROM spec_product_file WHERE spec_product_id = ? AND kind = 'tds'", (p1["id"],)).fetchall()
        assert [x["file_name"] for x in rows] == ["Smoke-Seal_TDS_v2.pdf"], rows
        assert up(p1["id"], _pdf(), "Smoke-Seal_SDS.pdf", kind="sds").status_code == 201
        assert a.delete(f"/api/spec-products/{p1['id']}/files/sds").status_code == 200
        assert a.delete(f"/api/spec-products/{p1['id']}/files/sds").status_code == 404
        checks += 1

        # ---- a job's products
        r = a.post(f"/api/projects/{JOB}/document-specs", json={"spec_product_id": p1["id"], "where": "Window perimeter joints"})
        assert r.status_code == 201, r.get_json()
        assert a.post(f"/api/projects/{JOB}/document-specs", json={"spec_product_id": p1["id"]}).status_code == 200
        assert a.post(f"/api/projects/{JOB}/document-specs", json={"spec_product_id": 999999999}).status_code == 404
        assert a.post("/api/projects/NOPE-404/document-specs", json={"spec_product_id": p1["id"]}).status_code == 404
        assert pm.post(f"/api/projects/{JOB}/document-specs", json={"spec_product_id": p2["id"]}).status_code == 201   # pm on own job
        assert pm.post(f"/api/projects/{OTHER}/document-specs", json={"spec_product_id": p2["id"]}).status_code in (403, 404)
        assert a.patch(f"/api/projects/{JOB}/document-specs/{p1['id']}", json={"where": "Window + coping joints"}).status_code == 200
        lst = pm.get(f"/api/projects/{JOB}/document-specs").get_json()["data"]
        mine = {x["spec_product_id"]: x for x in lst}
        assert mine[p1["id"]]["where"] == "Window + coping joints" and "file_path" not in str(lst)
        jf = mine[p1["id"]]["files"]["tds"]
        assert jf and jf["file_url"] == f"/api/projects/{JOB}/document-specs/{p1['id']}/files/tds"
        assert pm.get(jf["file_url"]).status_code == 200                        # the pm reads the sheet through the job
        assert pm.get(f"/api/projects/{OTHER}/document-specs").status_code in (403, 404)
        assert a.get(f"/api/projects/{OTHER}/document-specs/{p1['id']}/files/tds").status_code == 404   # not on that job
        cat_items = {x["id"]: x for x in a.get("/api/spec-products?manufacturer=" + MFR).get_json()["data"]["items"]}
        assert cat_items[p1["id"]]["jobs"] == [{"project_code": JOB, "where": "Window + coping joints"}]
        checks += 1

        # ---- submittal package (the cover renderer is the headless-browser seam)
        orig = spec_library._render_cover
        seen = {}
        spec_library._render_cover = lambda html: (seen.setdefault("html", html), _pdf())[1]
        try:
            r = a.get(f"/api/projects/{JOB}/document-specs/submittal.pdf")
            assert r.status_code == 200 and r.headers["Content-Type"] == "application/pdf", r.status_code
            assert len(pypdf.PdfReader(io.BytesIO(r.data)).pages) == 1 + 2   # cover + the 2-page data sheet
            assert "Smoke Seal 1" in seen["html"] and "Window + coping joints" in seen["html"] and "Not on file" in seen["html"]
            assert pm.get(f"/api/projects/{JOB}/document-specs/submittal.pdf").status_code == 200
            assert a.get(f"/api/projects/{OTHER}/document-specs/submittal.pdf").status_code == 400   # nothing on it
        finally:
            spec_library._render_cover = orig
        checks += 1

        # ---- Claude: read a sheet (fake model) + the agent's tools
        r = a.post("/api/spec-products/read-datasheet", content_type="multipart/form-data",
                   data={"file": (io.BytesIO(_pdf()), "Sikaflex-11 FC.pdf")})
        assert r.status_code == 200 and r.get_json()["data"]["product_name"] == "Sikaflex-11 FC", r.get_json()
        assert a.post("/api/spec-products/read-datasheet", content_type="multipart/form-data",
                      data={"file": (io.BytesIO(b"nope"), "x.pdf")}).status_code == 400
        ctx = ca.Ctx(conn, {"id": ids["admin"], "role": "admin"}, "office")
        res = ca.BY_NAME["spec_products"].run(ctx, {"query": "gun-grade"})
        hit = [x for x in res["products"] if x["spec_product_id"] == p1["id"]]
        assert hit and hit[0]["data_sheet_on_file"] and hit[0]["go_to"] and hit[0]["jobs"][0]["project_code"] == JOB
        sheet = ca.BY_NAME["spec_datasheet"].run(ctx, {"spec_product_id": p1["id"]})
        assert sheet["on_file"] is True
        assert ca.BY_NAME["spec_datasheet"].run(ctx, {"spec_product_id": p2["id"]})["on_file"] is False
        assert not ca.BY_NAME["spec_products"].allowed("pm", "office")
        checks += 1

        # ---- detach
        assert a.delete(f"/api/projects/{JOB}/document-specs/{p1['id']}").status_code == 200
        assert a.delete(f"/api/projects/{JOB}/document-specs/{p1['id']}").status_code == 404
        checks += 1

        # ---- page needles
        html = (ROOT / "company-dashboard.html").read_text(encoding="utf-8")
        assert ('<div class="panel" data-panel="specs">\n    <div class="ssc-subtabs" data-subtabs="company"></div>\n'
                '    <div class="section spx">') in html   # same Company frame as the other tabs -> same margins
        assert "/files/static/js/spec_library.js?v=" in html and 'id="spx-results"' in html and 'id="spx-panel"' in html
        for gone in ("specs-attach-btn", 'id="specs-tbody"', "specs-core-only", "890-core only"):
            assert gone not in html, gone
        js = (ROOT / "static/js/spec_library.js").read_text(encoding="utf-8")
        for there in ("window.SPECS_loadCatalog", "submittal.pdf", "/read-datasheet", "SSC_CLAUDE", "data-spxstar"):
            assert there in js, there
        assert "toISOString" not in js   # local dates only (CLAUDE.md)
        # the deferred script starts itself when #specs opened before it loaded (2026-11-13: stuck on "Loading…")
        assert "classList.contains('active')) window.SPECS_loadCatalog()" in js
        dash = (ROOT / "dashboard-static.html").read_text(encoding="utf-8")
        assert 'id="pd-specs"' in dash and "window.PD_loadSpecs" in dash and "PD_loadSpecs(PROJECT_CODE)" in dash
        checks += 1
    finally:
        conn.close()
        _cleanup()
    print(f"PASS smoke_spec_library: {checks} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
