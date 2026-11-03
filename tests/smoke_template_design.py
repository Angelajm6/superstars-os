#!/usr/bin/env python3
"""Company templates → Improve design (house style): behavioral guard.

Boots the real app (`import server`) on an ISOLATED database with a scratch data root. Claude's read
and redraw are the TEMPLATE_READ_FAKE / TEMPLATE_DESIGN_FAKE seams; the PDF engine is replaced, so
neither an API key nor a browser is needed. Proves:

  * pure: unknown block kinds and empty blocks dropped, sources checked, lists capped, table rows
    never fewer than the printed ones; the blanks of a design are listed once each
  * layout: every string is escaped (a <script> title, a quote in the footer string); the letterhead
    lines and folder print; job values fill their boxes; the page preview is one sheet; Word export opens
  * schema idempotent; company_template.layout_json exists
  * gates: anon 401, a PM 403 on list, draw, preview and approve; AI off -> 503
  * draw: a draft starts as drafting, then ready with Claude's changes; the preview renders; a second
    draw while one is drafting -> 409, a stuck one shows failed and no longer blocks
  * edit: needs a finished draft; the request is kept on the new draft
  * approve: only a ready draft; a PDF failure leaves nothing behind; the draft becomes version 2 (PDF,
    house style, its own blanks), the old version leaves the list but stays in History; the first
    approval sets the house style, the next one doesn't; drafts follow the new version
  * Word copy only for a redesigned template; discard hides a draft
  * page: the Improve design window is wired and its script version bumped

Run:  venv/bin/python tests/smoke_template_design.py
"""
from __future__ import annotations

import io
import json
import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")   # test-only seams are inert without this (runtime_seams.py)
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_tpl_design_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")
os.environ.pop("ANTHROPIC_API_KEY", None)
for _k in ("TEMPLATE_READ_FAKE", "TEMPLATE_DESIGN_FAKE"):
    os.environ.pop(_k, None)

import auth  # noqa: E402
import claude_docs  # noqa: E402
import company_templates as ct  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)
import template_design as td  # noqa: E402
import template_layout as TL  # noqa: E402

TAG = "tpd28"
PW = "TemplateDesignSmoke2026"
ROLES = ("admin", "pm")
READ = Path(_TMP) / "read.json"
DESIGN = Path(_TMP) / "design.json"
API = "/api/company/portal/templates"


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed():
    conn = _db()
    try:
        for role in ROLES:
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), role, f"Smoke {role}"))
        conn.execute("DELETE FROM app_settings WHERE key = ?", (td.HOUSE_KEY,))
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        tids = [r["id"] for r in conn.execute("SELECT id FROM company_template WHERE created_by IN (%s)" % (",".join("?" * len(uids)) or "NULL"), uids).fetchall()]
        purge(conn, "company_template_design", "template_id", tids)
        purge(conn, "company_template", "id", tids)
        conn.execute("DELETE FROM app_settings WHERE key = ?", (td.HOUSE_KEY,))
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code, r.get_data(as_text=True)[:200])
    return c


def _data(r):
    j = r.get_json() or {}
    return j.get("data", j)


def _blk(kind, **kw):
    b = {"kind": kind, "text": "", "fields": [], "columns": [], "rows": 0, "data": [], "items": [], "signers": []}
    b.update(kw)
    return b


DESIGN_RAW = {
    "title": "Site Safety Plan", "subtitle": "Rope access",
    "blocks": [
        _blk("fields", fields=[{"label": "Job address", "source": "job_address", "wide": True},
                               {"label": "Owner", "source": "owner", "wide": False},
                               {"label": "Rigging engineer", "source": "engineer", "wide": False}]),
        _blk("heading", text="1. Emergency contacts"),
        _blk("table", columns=["Name", "Phone"], data=[["Emergency services", "911"]], rows=4),
        _blk("checklist", items=["Anchors inspected", "  "]),
        _blk("signatures", signers=["Superintendent"]),
        _blk("paragraph", text="   "),
        {"kind": "video", "text": "dropped"},
    ],
    "changes": ["Blank lines became a labelled box of fields.", ""],
}


def _wait_ready(c, tid, did, secs=10):
    end = time.time() + secs
    while time.time() < end:
        d = next(x for x in _data(c.get(f"{API}/{tid}/designs")) if x["id"] == did)
        if d["status"] != "drafting":
            return d
        time.sleep(0.1)
    raise AssertionError("draft never finished")


def main() -> int:
    checks = 0
    _seed()

    # ---- pure
    content, changes = td.process_design(DESIGN_RAW)
    kinds = [b["kind"] for b in content["blocks"]]
    assert kinds == ["fields", "heading", "table", "checklist", "signatures"], kinds
    assert content["blocks"][0]["fields"][2]["source"] == "other"
    assert content["blocks"][2]["rows"] == 4 and content["blocks"][2]["data"] == [["Emergency services", "911"]]
    assert content["blocks"][3]["items"] == ["Anchors inspected"] and changes == ["Blank lines became a labelled box of fields."]
    t2, _ = td.process_design({"title": "x", "blocks": [_blk("table", columns=["A"], data=[["1"], ["2"], ["3"]], rows=1)]})
    assert t2["blocks"][0]["rows"] == 3
    assert td.process_design(None)[0]["blocks"] == []
    assert [f["label"] for f in td.content_fields(content)] == ["Job address", "Owner", "Rigging engineer"]
    assert "Keep EVERY word" in td.build_prompt() and "REVISION" in td.build_prompt("move it", {"title": "x"})
    checks += 8

    # ---- layout
    evil = {"title": 'Plan <script>alert(1)</script> "Q"', "blocks": content["blocks"]}
    doc = TL.render(evil, folder_name="Safety & Compliance", revision="2026-10",
                    letterhead={"company_address": "1 Example Ave"}, values={"job_address": "41-12 <Queens> Blvd"})
    assert "<script>alert" not in doc and "&lt;script&gt;" in doc and "41-12 &lt;Queens&gt; Blvd" in doc
    assert "1 Example Ave" in doc and "Safety &amp; Compliance" in doc and "counter(pages)" in doc
    assert '\\"Q\\"' in doc and "@bottom-left" in doc
    css, sheet = TL.render(content, mode="page")
    assert "tl-sheet" in sheet and "@page" not in css and "Page 1" in sheet
    docx = claude_docs.docx_bytes({"title": "x", "html": TL.word_html(content)})
    assert zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").count(b"Emergency contacts") == 1
    checks += 5

    # ---- schema
    conn = _db()
    try:
        ct.ensure_schema(conn)
        ct.ensure_schema(conn)
        assert td._has_column(conn, "company_template", "layout_json")
    finally:
        conn.close()
    checks += 1

    admin, pm = _login("admin"), _login("pm")
    anon = server.app.test_client()

    # ---- a filed template to redesign
    READ.write_text(json.dumps({"title": "Site Safety Plan, rope access", "folder": "SAFETY", "type_key": "site_safety_plan",
                                "summary": "x", "fields": [], "offer": {"access_methods": ["rope_access"]},
                                "design": {"verdict": "dated", "reasons": ["Typewriter font"]}}), encoding="utf-8")
    os.environ["TEMPLATE_READ_FAKE"] = str(READ)
    up = admin.post(API, data={"file": (io.BytesIO(b"%PDF-1.4 ssp %%EOF"), "SSP.pdf")}, content_type="multipart/form-data")
    assert up.status_code == 201, up.get_data(as_text=True)[:300]
    v1 = _data(up)
    assert admin.post(f"{API}/{v1['id']}/approve", json={}).status_code == 200
    checks += 1

    # ---- gates + AI off
    assert anon.get(f"{API}/{v1['id']}/designs").status_code == 401
    assert pm.get(f"{API}/{v1['id']}/designs").status_code == 403
    assert pm.post(f"{API}/{v1['id']}/designs", json={}).status_code == 403
    off = admin.post(f"{API}/{v1['id']}/designs", json={})
    assert off.status_code == 503 and "AI is off" in off.get_json()["error"]
    lst = admin.get(f"{API}/{v1['id']}/designs")
    assert _data(lst) == [] and lst.get_json()["meta"]["house_style"]["set"] is None
    assert lst.get_json()["meta"]["house_style"]["headings"] == "Archivo"
    assert admin.post(f"{API}/999999/designs", json={}).status_code in (404, 503)
    checks += 6

    # ---- draw
    DESIGN.write_text(json.dumps(DESIGN_RAW), encoding="utf-8")
    os.environ["TEMPLATE_DESIGN_FAKE"] = str(DESIGN)
    st = admin.post(f"{API}/{v1['id']}/designs", json={})
    assert st.status_code == 202, st.get_data(as_text=True)[:300]
    d1 = _wait_ready(admin, v1["id"], _data(st)["id"])
    assert d1["status"] == "ready" and d1["draft_no"] == 1 and d1["changes"] == ["Blank lines became a labelled box of fields."], d1
    pv = admin.get(f"{API}/designs/{d1['id']}/preview")
    assert pv.status_code == 200 and "tl-sheet" in _data(pv)["html"] and "Emergency contacts" in _data(pv)["html"]
    assert pm.get(f"{API}/designs/{d1['id']}/preview").status_code == 403
    conn = _db()
    try:   # a draft still drafting blocks a second one; a stuck one no longer does
        cur = conn.execute("INSERT INTO company_template_design (template_id, draft_no, status, created_at, updated_at) "
                           "VALUES (?, 9, 'drafting', ?, ?)", (v1["id"], td._now(), td._now()))
        busy_id = cur.lastrowid
        conn.commit()
        assert admin.post(f"{API}/{v1['id']}/designs", json={}).status_code == 409
        old = (datetime.now() - timedelta(minutes=td.STUCK_MINUTES + 1)).isoformat(timespec="seconds")
        conn.execute("UPDATE company_template_design SET updated_at = ? WHERE id = ?", (old, busy_id))
        conn.commit()
        stuck = next(x for x in _data(admin.get(f"{API}/{v1['id']}/designs")) if x["id"] == busy_id)
        assert stuck["status"] == "failed" and "too long" in stuck["error"]
        conn.execute("DELETE FROM company_template_design WHERE id = ?", (busy_id,))
        conn.commit()
    finally:
        conn.close()
    checks += 5

    # ---- edit
    assert admin.post(f"{API}/{v1['id']}/designs", json={"instruction": "Put contacts first"}).status_code == 400
    assert admin.post(f"{API}/{v1['id']}/designs", json={"from_id": d1["id"]}).status_code == 400
    e2 = admin.post(f"{API}/{v1['id']}/designs", json={"instruction": "Put   contacts first", "from_id": d1["id"]})
    assert e2.status_code == 202
    d2 = _wait_ready(admin, v1["id"], _data(e2)["id"])
    assert d2["draft_no"] == 2 and d2["instruction"] == "Put contacts first"
    checks += 3

    # ---- approve
    real_pdf = td.pdf_bytes
    try:
        def boom(_html):
            raise ValueError("The PDF couldn't be made right now. Try again in a minute.")
        td.pdf_bytes = boom
        before = len(_data(admin.get(API)))
        bad = admin.post(f"{API}/designs/{d1['id']}/approve")
        assert bad.status_code == 503 and len(_data(admin.get(API))) == before
        rendered = []
        td.pdf_bytes = lambda html: rendered.append(html) or b"%PDF-1.4 house style %%EOF"
        assert pm.post(f"{API}/designs/{d1['id']}/approve").status_code == 403
        ok = admin.post(f"{API}/designs/{d1['id']}/approve")
        assert ok.status_code == 200, ok.get_data(as_text=True)[:300]
        out = _data(ok)
        v2 = out["template"]
        assert out["house_style_set_now"] is True and v2["version"] == 2 and v2["redesigned"] and v2["has_history"]
        assert v2["mime"] == "application/pdf" and v2["design_verdict"] == "ok" and v2["folder"] == "SAFETY"
        assert v2["offer"]["access_methods"] == ["rope_access"] and [f["source"] for f in v2["fields"]] == ["job_address", "owner", "other"]
        assert "Site Safety Plan" in rendered[0] and "@page" in rendered[0]
        ids = [t["id"] for t in _data(admin.get(API))]
        assert v2["id"] in ids and v1["id"] not in ids
        fr = admin.get(v2["file_url"])
        assert fr.status_code == 200 and fr.data.startswith(b"%PDF-1.4 house")
        assert admin.post(f"{API}/designs/{d1['id']}/approve").status_code == 400   # already approved
        hist = _data(admin.get(f"{API}/{v2['id']}/history"))
        assert [h["version"] for h in hist] == [2, 1] and hist[1]["id"] == v1["id"]
        # the drafts moved with the template: draft 2 approves as version 3; the house style stays as set
        assert any(x["id"] == d2["id"] for x in _data(admin.get(f"{API}/{v2['id']}/designs")))
        set_at = admin.get(f"{API}/{v2['id']}/designs").get_json()["meta"]["house_style"]["set"]["set_at"]
        ok3 = admin.post(f"{API}/designs/{d2['id']}/approve")
        assert ok3.status_code == 200 and _data(ok3)["template"]["version"] == 3 and _data(ok3)["house_style_set_now"] is False
        v3 = _data(ok3)["template"]
        assert admin.get(f"{API}/{v3['id']}/designs").get_json()["meta"]["house_style"]["set"]["set_at"] == set_at
        assert [h["version"] for h in _data(admin.get(f"{API}/{v3['id']}/history"))] == [3, 2, 1]
    finally:
        td.pdf_bytes = real_pdf
    checks += 12

    # ---- Word + discard
    w = admin.get(f"{API}/{v3['id']}/word")
    assert w.status_code == 200 and w.data[:2] == b"PK" and "attachment" in w.headers["Content-Disposition"]
    assert admin.get(f"{API}/{v1['id']}/word").status_code == 404
    st = admin.post(f"{API}/{v3['id']}/designs", json={})
    d4 = _wait_ready(admin, v3["id"], _data(st)["id"])
    assert admin.delete(f"{API}/designs/{d4['id']}").status_code == 200
    assert all(x["id"] != d4["id"] for x in _data(admin.get(f"{API}/{v3['id']}/designs")))
    assert admin.delete(f"{API}/designs/{d4['id']}").status_code == 404
    checks += 4

    # ---- the page
    html = (ROOT / "company_portal.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "js" / "company_templates.js").read_text(encoding="utf-8")
    assert "company_templates.js?v=2" in html and ".tp-rd-scrim" in html
    assert 'data-redesign="${t.id}"' in js and "attachShadow" in js and "/designs/' + d.id + '/approve'" in js
    assert 'href="/company#brand"' in js
    checks += 3

    print(f"smoke_template_design: {checks} checks passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
