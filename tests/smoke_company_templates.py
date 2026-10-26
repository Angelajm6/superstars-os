#!/usr/bin/env python3
"""Company templates (step 1, the library): behavioral guard.

Boots the real app (`import server`) on an ISOLATED database with a scratch data root.
Claude's read is the TEMPLATE_READ_FAKE seam (a canned answer), so no API key is needed.
Proves:

  * schema idempotent; the migration script runs; it is on the ledger list and boot-ensured
  * pure read: the folder falls back to the type's folder; a type from another folder is
    dropped; unknown field sources become "other"; offer codes are filtered to the app's own;
    a design marked ok carries no notes; Word files go to Claude as their text
  * gates: anon -> 401; a PM -> 403 on list, upload and file
  * no AI: a dropped file still waits ('unread', with a reason), named after the file
  * with AI: the card carries Claude's name, folder, type, offer, blanks and design verdict;
    NO *_path anywhere; the same file dropped twice is one card; bad types / empty files 400
  * approve: needs a name; a type must belong to the folder; files it; can't approve twice
  * edit: PATCH changes the offer and folder; the list shows it
  * file: opens inline, no-store; another role can't
  * remove: a waiting upload is deleted with its file; a filed template leaves the list
  * page: the Templates view, its script, and the Company sub-tab

Run:  venv/bin/python tests/smoke_company_templates.py
"""
from __future__ import annotations

import io
import json
import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")   # test-only seams are inert without this (runtime_seams.py)
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_templates_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("TEMPLATE_READ_FAKE", None)

import auth  # noqa: E402
import company_templates as ct  # noqa: E402
import db_layer  # noqa: E402
import migrations  # noqa: E402
import preprod_fixture_bootstrap as pfb  # noqa: E402
import server  # noqa: E402  (boots the whole app)
import ssc_paths  # noqa: E402

TAG = "tpl26"
PW = "TemplatesSmokePassword2026"
ROLES = ("admin", "c_suite", "pm")
FAKE = Path(_TMP) / "read.json"
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
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        purge(conn, "company_template", "created_by", uids)
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _no_paths(obj, where=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert not k.endswith("_path"), f"{k!r} leaked in {where}"
            _no_paths(v, where)
    elif isinstance(obj, list):
        for v in obj:
            _no_paths(v, where)


def _login(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code, r.get_data(as_text=True)[:200])
    return c


def _drop(c, name="Site Safety Plan.pdf", body=b"%PDF-1.4 template smoke %%EOF"):
    return c.post(API, data={"file": (io.BytesIO(body), name)}, content_type="multipart/form-data")


def _data(r):
    j = r.get_json() or {}
    return j.get("data", j)


def _docx(text: str) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{w}"><w:body>'
           + "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text.split("\n"))
           + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("word/document.xml", date_time=(2026, 1, 1, 0, 0, 0)), xml)   # same bytes every time
    return buf.getvalue()


def _fake(**over):
    base = {"title": "Site Safety Plan, rope access", "folder": "SAFETY", "type_key": "site_safety_plan",
            "summary": "The plan the DOB asks for before rope access work starts.",
            "fields": [{"label": "Job Address", "source": "job_address"}, {"label": "Owner", "source": "owner"},
                       {"label": "Rigging engineer", "source": "engineer"}, {"label": "job address", "source": "job_address"}],
            "offer": {"job_types": [], "access_methods": ["rope_access"], "billing_types": [], "regions": []},
            "design": {"verdict": "dated", "reasons": ["Typewriter font", "Blank underscores instead of boxes"]}}
    base.update(over)
    FAKE.write_text(json.dumps(base), encoding="utf-8")


def main() -> int:
    checks = 0
    _seed()

    # ---- schema + migration
    conn = _db()
    try:
        ct.ensure_schema(conn)
        ct.ensure_schema(conn)
    finally:
        conn.close()
    env = dict(os.environ)
    out = subprocess.run([sys.executable, "apply_company_templates.py"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode == 0 and "company_template ready" in out.stdout, out.stderr[-400:]
    assert "apply_company_templates.py" in pfb.DUAL_BACKEND_MIGRATIONS
    assert migrations.BOOT_ENSURED.get("apply_company_templates.py") == ("company_template",)
    checks += 3

    # ---- pure read
    r = ct.process_read({"title": "  Lien   waiver ", "folder": "nope", "type_key": "lien_waiver", "summary": "x",
                         "fields": [{"label": "Owner", "source": "owner"}, {"label": "OWNER", "source": "owner"},
                                    {"label": "Amount", "source": "dollars"}, "junk"],
                         "offer": {"billing_types": ["aia_sov", "barter"], "regions": ["XX"], "job_types": "FR"},
                         "design": {"verdict": "ok", "reasons": ["should vanish"]}})
    assert r["title"] == "Lien waiver" and r["folder"] == "CLOSEOUT" and r["type_key"] == "lien_waiver", r
    assert r["fields"] == [{"label": "Owner", "source": "owner"}, {"label": "Amount", "source": "other"}], r["fields"]
    assert r["offer"] == {"job_types": [], "access_methods": [], "billing_types": ["aia_sov"], "regions": []}, r["offer"]
    assert r["design_verdict"] == "ok" and r["design_notes"] == []
    r = ct.process_read({"folder": "LETTERS", "type_key": "coi"})
    assert r["folder"] == "LETTERS" and r["type_key"] is None, r      # a COI type can't sit in Letters
    assert ct.process_read(None)["folder"] == "OTHER"
    assert ct.offer_label(ct.clean_offer({})) == "Every job"
    spec = ct._spec(_docx("SITE SAFETY PLAN\nJob Address: ____"), "SSP.docx")
    assert spec["type"] == "document" and "Job Address" in spec["source"]["data"], spec
    assert ct._spec(b"%PDF-1.4", "a.pdf")["source"]["media_type"] == "application/pdf"
    assert "rope_access" in ct.build_prompt() and "site_safety_plan" in ct.build_prompt()
    checks += 8

    admin, csuite, pm = _login("admin"), _login("c_suite"), _login("pm")
    anon = server.app.test_client()

    # ---- gates
    assert anon.get(API).status_code == 401
    assert pm.get(API).status_code == 403
    assert _drop(pm).status_code == 403
    assert csuite.get(API).status_code == 200
    checks += 4

    # ---- no AI: still waits, named after the file
    os.environ.pop("TEMPLATE_READ_FAKE", None)
    res = _drop(admin, "Toolbox Talk Sign-In.pdf", b"%PDF-1.4 toolbox %%EOF")
    assert res.status_code == 201, res.get_data(as_text=True)[:300]
    t0 = _data(res)
    assert t0["state"] == "waiting" and t0["read_status"] == "unread" and "Pick the folder" in t0["read_error"], t0
    assert t0["title"] == "Toolbox Talk Sign-In" and t0["folder"] == "OTHER" and t0["offer_label"] == "Every job"
    _no_paths(res.get_json(), "upload (no AI)")
    checks += 3

    # ---- with AI (planted read)
    _fake()
    os.environ["TEMPLATE_READ_FAKE"] = str(FAKE)
    res = _drop(admin, "SSP-RA-2023.docx", _docx("SITE SAFETY PLAN\nJob Address: ____\nOwner: ____"))
    assert res.status_code == 201, res.get_data(as_text=True)[:300]
    t1 = _data(res)
    assert t1["title"] == "Site Safety Plan, rope access" and t1["folder"] == "SAFETY", t1
    assert t1["type_key"] == "site_safety_plan" and t1["requirement_key"] == "site_safety_plan", t1
    assert t1["offer"]["access_methods"] == ["rope_access"] and t1["offer_label"] == "Rope access", t1
    assert [f["source"] for f in t1["fields"]] == ["job_address", "owner", "other"], t1["fields"]
    assert t1["design_verdict"] == "dated" and len(t1["design_notes"]) == 2 and t1["read_status"] == "ready"
    assert t1["mime"].endswith("wordprocessingml.document")
    _no_paths(res.get_json(), "upload (AI)")
    again = _drop(admin, "copy of SSP.docx", _docx("SITE SAFETY PLAN\nJob Address: ____\nOwner: ____"))
    assert again.status_code == 200 and again.get_json()["meta"]["duplicate_of"] == t1["id"]
    assert _drop(admin, "virus.exe", b"MZ").status_code == 400
    assert _drop(admin, "empty.pdf", b"").status_code == 400
    lst = admin.get(API)
    _no_paths(lst.get_json(), "list")
    meta = lst.get_json()["meta"]
    assert [f["code"] for f in meta["folders"]][:6] == list(ct.PROJECT_FOLDERS) and meta["offer_options"]["access_methods"]
    assert sorted(t["id"] for t in _data(lst)) == sorted([t0["id"], t1["id"]])
    checks += 10

    # ---- approve
    assert admin.post(f"{API}/{t1['id']}/approve", json={"title": "  "}).status_code == 400
    bad = admin.post(f"{API}/{t1['id']}/approve", json={"folder": "SAFETY", "type_key": "coi"})
    assert bad.status_code == 400 and "different folder" in bad.get_json()["error"]
    ok = admin.post(f"{API}/{t1['id']}/approve", json={"title": "Site Safety Plan (rope access)", "folder": "SAFETY",
                                                       "type_key": "site_safety_plan",
                                                       "offer": {"access_methods": ["rope_access", "hovercraft"]}})
    assert ok.status_code == 200, ok.get_data(as_text=True)[:300]
    f1 = _data(ok)
    assert f1["state"] == "filed" and f1["filed_at"] and f1["title"] == "Site Safety Plan (rope access)"
    assert f1["offer"]["access_methods"] == ["rope_access"]
    assert admin.post(f"{API}/{t1['id']}/approve", json={}).status_code == 400
    assert admin.post(f"{API}/999999/approve", json={}).status_code == 404
    checks += 5

    # ---- edit (C-suite too)
    ed = csuite.patch(f"{API}/{t1['id']}", json={"folder": "FIELD", "offer": {"billing_types": ["aia_sov"]}})
    assert ed.status_code == 200, ed.get_data(as_text=True)[:300]
    e1 = _data(ed)
    assert e1["folder"] == "FIELD" and e1["type_key"] is None and e1["offer_label"] == "AIA / Schedule of values", e1
    assert csuite.patch(f"{API}/{t1['id']}", json={"folder": "NOPE"}).status_code == 400
    checks += 2

    # ---- the file
    fr = admin.get(f"{API}/{t1['id']}/file")
    assert fr.status_code == 200 and fr.data[:2] == b"PK" and "no-store" in fr.headers.get("Cache-Control", "")
    assert pm.get(f"{API}/{t1['id']}/file").status_code == 403
    checks += 2

    # ---- remove
    conn = _db()
    try:
        p0 = ssc_paths.resolve_data_path(conn.execute("SELECT file_path FROM company_template WHERE id = ?",
                                                      (t0["id"],)).fetchone()["file_path"])
    finally:
        conn.close()
    assert p0.exists()
    assert admin.delete(f"{API}/{t0['id']}").status_code == 200 and not p0.exists()
    assert admin.delete(f"{API}/{t1['id']}").status_code == 200
    assert _data(admin.get(API)) == []
    assert admin.delete(f"{API}/{t1['id']}").status_code == 404
    checks += 3

    # ---- the page
    html = (ROOT / "company_portal.html").read_text(encoding="utf-8")
    nav = (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")
    js = (ROOT / "static" / "js" / "company_templates.js").read_text(encoding="utf-8")
    assert 'id="view-templates"' in html and 'src="/files/static/js/company_templates.js?v=' in html
    assert "templates: () => window.SSCTemplates.load()" in html and 'id="tp-file" multiple' in html
    assert "['Templates', '/company#templates']" in nav and "['/company', 'templates']" in nav
    assert "window.SSCTemplates = { load }" in js and "/api/company/portal/templates" in js
    page = admin.get("/company")
    assert page.status_code == 200 and b"company_templates.js" in page.data
    checks += 5

    print(f"smoke_company_templates: {checks} checks passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
