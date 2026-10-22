#!/usr/bin/env python3
"""Documents drop zone: behavioral guard.

Boots the real app (`import server`) on an ISOLATED database with a scratch data root.
Claude's read is the DOC_SCAN_FAKE seam (a canned answer), so no API key is needed.
Proves:

  * schema idempotent; migration script runs; the type list is coherent (every type's
    folder is a real category; every checklist item has a type)
  * pure read: a valid type tag decides the checklist item; a tag from another folder
    falls back + is flagged; fields / key facts are cleaned and capped
  * gates: anon -> 401; client -> 403; a PM not on the job -> 403; unknown job -> 404
  * no AI: a dropped file still waits ('unread', with a reason) and survives a reload
  * with AI: the card carries the tag, summary, key facts and every field; NO *_path;
    the waiting file opens inline; nothing is filed before approval
  * approve: needs a valid tag; files under the tag's folder + checklist item; the
    COI requirement ticks on_file; Claude's read is stored and returned on the doc;
    a changed tag is recorded as set by the user; the intake row is gone; bad dates 400
  * newer version: approve with supersedes_id moves the old one to History and pulls
    its client share
  * remove: deletes the waiting row and its file; filed documents untouched
  * several per item: our COI + each subcontractor's COI all stay current on one checklist
    item (`docs`); party comes from Claude's read or the approver; PATCH edits it; nothing
    filed goes missing from the payload
  * suggestions: a subcontract suggests that sub's COI / lien waiver, words on the pages and
    Claude's `related` add more; "Required" only for a missing checklist item; filing it or
    "Not needed" removes it; gates on dismiss
  * the Documents tab no longer carries the Specifications table or the permits & insurance card

Run:  venv/bin/python tests/smoke_doc_dropzone.py
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
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_dropzone_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("DOC_SCAN_FAKE", None)

import auth  # noqa: E402
import db_layer  # noqa: E402
import doc_intake  # noqa: E402
import doc_suggest  # noqa: E402
import doc_types  # noqa: E402
import document_scanner as scanner  # noqa: E402
import server  # noqa: E402  (boots the whole app)
import visibility  # noqa: E402

TAG = "dz358"
PW = "DropZoneSmokePassword2026"
ROLES = ("admin", "pm", "client")
PROJ = "DZ-SMOKE-A"
TODAY = date.today()
FAKE = Path(_TMP) / "scan.json"
CATS = set(server._PROJDOC_CATEGORIES)


def D(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


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
        conn.execute("DELETE FROM projects WHERE project_code = ?", (PROJ,))
        conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, 'DZ Facade', "
                     "'1 Dropzone Way', 'active')", (PROJ,))
        conn.commit()   # the PM is deliberately NOT assigned to this job
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        doc_ids = [r["id"] for r in conn.execute("SELECT id FROM project_documents WHERE project_code = ?", (PROJ,)).fetchall()]
        for t in ("item_visibility", "item_redflag", "visibility_audit"):
            purge(conn, t, "item_id", doc_ids)
        purge(conn, "project_documents", "id", doc_ids)
        purge(conn, "project_doc_intake", "project_code", [PROJ])
        purge(conn, "project_doc_suggest_dismissed", "project_code", [PROJ])
        conn.execute("DELETE FROM projects WHERE project_code = ?", (PROJ,))
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
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


def _drop(c, name="ACORD 25.pdf", body=b"%PDF-1.4 dropzone %%EOF"):
    return c.post(f"/api/projects/{PROJ}/documents/intake",
                  data={"file": (io.BytesIO(body), name)}, content_type="multipart/form-data")


def _data(r):
    j = r.get_json() or {}
    return j.get("data", j)


def _checklist(c):
    d = _data(c.get(f"/api/projects/{PROJ}/documents"))
    return {it["requirement_key"]: it for cat in d["categories"] for it in cat["items"]}


def _fake(**over):
    base = {"title": "Doc", "doc_type": "x", "category": "CONTRACTS", "requirement_key": None,
            "type_key": "contract_other", "effective_date": None, "expiry_date": None, "confidence": 0.95,
            "warnings": [], "summary": "", "key_facts": [], "fields": []}
    base.update(over)
    FAKE.write_text(json.dumps(base), encoding="utf-8")


def _payload(c):
    return _data(c.get(f"/api/projects/{PROJ}/documents"))


def main() -> int:
    checks = 0
    _seed()

    # ---- schema + taxonomy
    conn = _db()
    try:
        doc_intake.ensure_schema(conn)
        doc_intake.ensure_schema(conn)
        reqs = {(r["category"], r["requirement_key"]) for r in conn.execute(
            "SELECT category, requirement_key FROM document_requirements").fetchall()}
    finally:
        conn.close()
    r = subprocess.run([sys.executable, str(ROOT / "apply_doc_intake.py")], cwd=str(ROOT),
                       env=dict(os.environ), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert all(t["category"] in CATS for t in doc_types.BY_KEY.values())
    covered = {(t["category"], t["requirement_key"]) for t in doc_types.BY_KEY.values() if t["requirement_key"]}
    assert reqs <= covered, f"checklist items with no type: {sorted(reqs - covered)}"
    assert set(doc_types.FALLBACK_BY_CATEGORY) == CATS
    checks += 1

    # ---- pure read
    rbc = {}
    for cat, key in reqs:
        rbc.setdefault(cat, set()).add(key)
    p = scanner.process_scan_result({"category": "CONTRACTS", "requirement_key": "sov", "type_key": "aia_g702",
                                     "confidence": 0.9}, CATS, rbc)
    assert p["type_key"] == "aia_g702" and p["requirement_key"] is None   # a pay app never ticks the SOV
    p = scanner.process_scan_result({"category": "PERMITS", "requirement_key": None, "type_key": "coi",
                                     "confidence": 0.9}, CATS, rbc)
    assert p["type_key"] == "permit_other" and "type_key" in p["fields_to_check"]
    p = scanner.process_scan_result({"category": "PERMITS", "requirement_key": "pw2", "confidence": 0.9}, CATS, rbc)
    assert p["type_key"] == "pw2"   # older reads without a tag still get one
    p = scanner.process_scan_result({"category": "SAFETY", "requirement_key": None, "type_key": "sds", "confidence": 0.9,
                                     "summary": "x" * 5000,
                                     "fields": [{"label": "L", "value": "v" * 900}, {"label": "", "value": "drop"}, "junk"]
                                     + [{"label": f"F{i}", "value": "1"} for i in range(200)]}, CATS, rbc)
    assert p["requirement_key"] == "sds" and len(p["summary"]) == scanner.SUMMARY_MAX
    assert len(p["fields"]) == scanner.FIELDS_MAX and len(p["fields"][0]["value"]) == scanner.FIELD_VALUE_MAX
    assert "type_key" in scanner.SCAN_SCHEMA["required"] and "fields" in scanner.SCAN_SCHEMA["required"]
    checks += 1

    admin, pm, client = _login("admin"), _login("pm"), _login("client")
    anon = server.app.test_client()

    # ---- gates
    assert anon.get(f"/api/projects/{PROJ}/documents/intake").status_code == 401
    assert client.get(f"/api/projects/{PROJ}/documents/intake").status_code == 403
    assert pm.get(f"/api/projects/{PROJ}/documents/intake").status_code == 403
    assert _drop(pm).status_code == 403
    assert admin.get("/api/projects/NOPE-404/documents/intake").status_code == 404
    assert admin.post(f"/api/projects/{PROJ}/documents/intake", data={},
                      content_type="multipart/form-data").status_code == 400
    assert _drop(admin, name="virus.exe").status_code == 400
    types = _data(admin.get("/api/documents/types"))
    assert any(t["key"] == "cd5" for t in types["types"]) and len(types["categories"]) == 6
    checks += 1

    # ---- no AI: the file still waits, as unread, and survives a reload
    r = _drop(admin, name="scan_0042.pdf")
    assert r.status_code == 201, r.get_data(as_text=True)[:300]
    unread = _data(r)
    assert unread["status"] == "unread" and unread["suggestion"] is None and unread["error"]
    items = _data(admin.get(f"/api/projects/{PROJ}/documents/intake"))["items"]
    assert [i["id"] for i in items] == [unread["id"]]
    _no_paths(items, "intake list")
    checks += 1

    # ---- with AI (the seam): a COI read
    FAKE.write_text(json.dumps({
        "title": "COI — General Liability 2026-27", "doc_type": "Certificate of Insurance",
        "category": "CONTRACTS", "requirement_key": "coi", "type_key": "coi",
        "effective_date": D(-5), "expiry_date": D(360), "confidence": 0.95, "warnings": [],
        "summary": "ACORD 25 certificate for general liability and umbrella coverage.",
        "key_facts": [{"label": "Expires", "value": "next year"}, {"label": "Each occurrence", "value": "$1,000,000"}],
        "fields": [{"label": "Form", "value": "ACORD 25"}, {"label": "Each occurrence", "value": "$1,000,000"},
                   {"label": "Additional insured", "value": "Owner"}],
    }), encoding="utf-8")
    os.environ["DOC_SCAN_FAKE"] = str(FAKE)
    r = _drop(admin, body=b"%PDF-1.4 the coi %%EOF")
    assert r.status_code == 201
    card = _data(r)
    s = card["suggestion"]
    assert card["status"] == "ready" and s["type_key"] == "coi" and s["requirement_key"] == "coi"
    assert s["summary"].startswith("ACORD 25") and len(s["fields"]) == 3 and len(s["key_facts"]) == 2
    _no_paths(card, "intake card")
    f = admin.get(card["file_url"])
    assert f.status_code == 200 and f.data == b"%PDF-1.4 the coi %%EOF"
    assert "attachment" not in (f.headers.get("Content-Disposition") or "")
    assert client.get(card["file_url"]).status_code == 403
    assert _checklist(admin)["coi"]["status"] == "missing"   # nothing filed before approval
    checks += 1

    # ---- approve
    url = f"/api/documents/intake/{card['id']}/approve"
    assert admin.post(url, json={}).status_code == 400
    assert admin.post(url, json={"type_key": "not-a-type"}).status_code == 400
    assert admin.post(url, json={"type_key": "coi", "expiry_date": "11/02/2027"}).status_code == 400
    assert pm.post(url, json={"type_key": "coi"}).status_code == 403
    r = admin.post(url, json={"type_key": "coi"})
    assert r.status_code == 201, r.get_data(as_text=True)[:300]
    doc = _data(r)
    assert doc["category"] == "CONTRACTS" and doc["requirement_key"] == "coi" and doc["type_key"] == "coi"
    assert doc["type_label"] == "Certificate of Insurance (COI)" and doc["expiry_date"] == D(360)
    assert doc["ai"]["set_by"] == "ai" and len(doc["ai"]["fields"]) == 3 and doc["ai"]["summary"]
    _no_paths(doc, "approved doc")
    ck = _checklist(admin)["coi"]
    assert ck["status"] == "on_file" and ck["doc"]["id"] == doc["id"] and ck["doc"]["ai"]["fields"]
    assert admin.get(doc["file_url"]).data == b"%PDF-1.4 the coi %%EOF"
    assert admin.post(url, json={"type_key": "coi"}).status_code == 404   # already filed
    ids = [i["id"] for i in _data(admin.get(f"/api/projects/{PROJ}/documents/intake"))["items"]]
    assert card["id"] not in ids
    # the unread one: the user picks the tag; dates typed by the user win
    r = admin.post(f"/api/documents/intake/{unread['id']}/approve",
                   json={"type_key": "aia_g702", "title": "Pay App #5", "effective_date": D(-1)})
    assert r.status_code == 201
    pay = _data(r)
    assert pay["requirement_key"] is None and pay["category"] == "CONTRACTS" and pay["effective_date"] == D(-1)
    assert pay["title"] == "Pay App #5" and pay["ai"] is None
    checks += 1

    # ---- newer version: supersedes the COI, pulls its client share
    conn = _db()
    try:
        assert visibility.share(conn, "document", doc["id"], "client", None).get("ok")
        conn.commit()
    finally:
        conn.close()
    r = _drop(admin, body=b"%PDF-1.4 renewed coi %%EOF")
    card2 = _data(r)
    r = admin.post(f"/api/documents/intake/{card2['id']}/approve",
                   json={"type_key": "coi", "supersedes_id": doc["id"]})
    assert r.status_code == 201
    doc2 = _data(r)
    ck = _checklist(admin)["coi"]
    assert ck["doc"]["id"] == doc2["id"] and ck["doc"]["history_count"] == 1
    conn = _db()
    try:
        assert not visibility.audiences_for(conn, "document", doc["id"])
    finally:
        conn.close()
    r = _drop(admin)
    card3 = _data(r)
    assert admin.post(f"/api/documents/intake/{card3['id']}/approve",
                      json={"type_key": "coi", "supersedes_id": doc["id"]}).status_code == 409
    # a changed tag is recorded as the user's
    r = admin.post(f"/api/documents/intake/{card3['id']}/approve", json={"type_key": "bond"})
    assert _data(r)["ai"]["set_by"] == "user" and _data(r)["requirement_key"] == "bond"
    checks += 1

    # ---- remove a waiting file
    card4 = _data(_drop(admin, name="wrong.pdf"))
    conn = _db()
    try:
        path = conn.execute("SELECT file_path FROM project_doc_intake WHERE id = ?", (card4["id"],)).fetchone()["file_path"]
    finally:
        conn.close()
    assert Path(path).exists()
    assert client.delete(f"/api/documents/intake/{card4['id']}").status_code == 403
    assert admin.delete(f"/api/documents/intake/{card4['id']}").status_code == 200
    assert not Path(path).exists()
    assert admin.delete(f"/api/documents/intake/{card4['id']}").status_code == 404
    assert admin.get(doc2["file_url"]).status_code == 200
    checks += 1

    # ---- pure: party + related cleaned off Claude's read; party helpers
    p = scanner.process_scan_result({"category": "CONTRACTS", "type_key": "coi", "confidence": 0.9,
                                     "party": "Subcontractor", "party_name": "  ABC   Masonry ",
                                     "related": [{"type_key": "lien_waiver", "reason": "Exhibit C"},
                                                 {"type_key": "nope", "reason": "x"},
                                                 {"type_key": "coi", "reason": "itself"},
                                                 {"type_key": "lien_waiver", "reason": "dup"}, "junk"]}, CATS, rbc)
    assert p["party"] == "subcontractor" and p["party_name"] == "ABC Masonry"
    assert p["related"] == [{"type_key": "lien_waiver", "reason": "Exhibit C"}]
    p = scanner.process_scan_result({"category": "CONTRACTS", "type_key": "coi", "confidence": 0.9,
                                     "party": "company", "party_name": "Us"}, CATS, rbc)
    assert p["party"] == "company" and p["party_name"] is None
    assert scanner.process_scan_result({"category": "CONTRACTS", "confidence": 0.9, "party": "unknown"},
                                       CATS, rbc)["party"] is None
    assert {"party", "party_name", "related"} <= set(scanner.SCAN_SCHEMA["required"])
    assert doc_intake.clean_party("OWNER", " Compass ") == ("owner", "Compass")
    assert doc_intake.clean_party("company", "x") == ("company", None)
    assert doc_intake.clean_party("boss", "x") == (None, None)
    assert doc_intake.same_party(None, None, "company", "") and not doc_intake.same_party("subcontractor", "A", "subcontractor", "B")
    assert doc_suggest.suggest([], set()) == []
    # a legacy row (no type tag, only the checklist item) counts as filed
    legacy = [{"id": 1, "title": "Pay App", "type_key": "aia_g702", "category": "CONTRACTS"},
              {"id": 2, "title": "SOV", "type_key": None, "category": "CONTRACTS", "requirement_key": "sov"}]
    assert "sov" not in {g["type_key"] for g in doc_suggest.suggest(legacy, set())}
    checks += 1

    # ---- several documents on one checklist item (ours + each subcontractor's COI)
    _fake(title="COI — ABC Masonry", category="CONTRACTS", requirement_key="coi", type_key="coi",
          expiry_date=D(200), party="subcontractor", party_name="ABC Masonry")
    sub_card = _data(_drop(admin, name="abc coi.pdf"))
    assert sub_card["suggestion"]["party"] == "subcontractor" and sub_card["suggestion"]["party_name"] == "ABC Masonry"
    r = admin.post(f"/api/documents/intake/{sub_card['id']}/approve", json={"type_key": "coi"})   # party from the read
    assert r.status_code == 201
    sub_coi = _data(r)
    assert sub_coi["party"] == "subcontractor" and sub_coi["party_name"] == "ABC Masonry"
    _fake(title="COI", category="CONTRACTS", requirement_key="coi", type_key="coi", party="unknown")
    xyz = _data(_drop(admin, name="xyz.pdf"))
    r = admin.post(f"/api/documents/intake/{xyz['id']}/approve",
                   json={"type_key": "coi", "party": "subcontractor", "party_name": "XYZ Steel"})   # the approver's pick wins
    xyz_coi = _data(r)
    assert xyz_coi["party_name"] == "XYZ Steel"
    ck = _checklist(admin)["coi"]
    held = {d["id"] for d in ck["docs"]}
    assert held == {doc2["id"], sub_coi["id"], xyz_coi["id"]}, held   # all three current, none superseded
    assert ck["doc"]["id"] in held and ck["status"] == "on_file"
    assert all("shared_client" in d for d in ck["docs"])
    _no_paths(ck, "checklist item")
    # nothing current is missing from the payload (items + extras)
    pay = _payload(admin)
    shown = {d["id"] for c in pay["categories"] for it in c["items"] for d in it["docs"]} | \
            {d["id"] for c in pay["categories"] for d in c["extras"]}
    conn = _db()
    try:
        current = {r["id"] for r in conn.execute("SELECT id FROM project_documents WHERE project_code=? AND superseded=0",
                                                 (PROJ,)).fetchall()}
    finally:
        conn.close()
    assert current <= shown, current - shown
    # a sub's renewal supersedes only that sub's COI
    _fake(title="COI — ABC Masonry 2027", category="CONTRACTS", requirement_key="coi", type_key="coi",
          party="subcontractor", party_name="ABC Masonry")
    ren = _data(_drop(admin))
    assert admin.post(f"/api/documents/intake/{ren['id']}/approve",
                      json={"type_key": "coi", "supersedes_id": sub_coi["id"]}).status_code == 201
    ck = _checklist(admin)["coi"]
    assert sub_coi["id"] not in {d["id"] for d in ck["docs"]} and {doc2["id"], xyz_coi["id"]} <= {d["id"] for d in ck["docs"]}
    # PATCH edits the party
    r = admin.patch(f"/api/documents/{xyz_coi['id']}", json={"party": "owner", "party_name": "Compass Point"})
    assert r.status_code == 200 and _data(r)["party"] == "owner" and _data(r)["party_name"] == "Compass Point"
    checks += 1

    # ---- suggestions read off what is filed
    _fake(title="Subcontract — Delta Roofing", category="CONTRACTS", type_key="subcontract",
          party="subcontractor", party_name="Delta Roofing",
          summary="Roofing subcontract. Sub to furnish a performance bond; after-hours work allowed.",
          related=[{"type_key": "sds", "reason": "Section 4 requires an SDS for every coating used."}])
    sc = _data(_drop(admin, name="delta subcontract.pdf"))
    assert admin.post(f"/api/documents/intake/{sc['id']}/approve", json={"type_key": "subcontract"}).status_code == 201
    sugs = {g["key"]: g for g in _payload(admin)["suggestions"]}
    _no_paths(list(sugs.values()), "suggestions")
    coi_k = "coi|subcontractor|delta roofing"
    assert coi_k in sugs and sugs[coi_k]["origin"] == "rule" and sugs[coi_k]["sources"][0]["title"].startswith("Subcontract")
    assert sugs[coi_k]["on_checklist"] is False            # the COI item is already on file — so only "may be needed"
    assert "lien_waiver|subcontractor|delta roofing" in sugs
    bond = sugs["bond|subcontractor|delta roofing"]        # from the words on the page
    assert bond["origin"] == "content" and bond["on_checklist"] is False   # the job's bond item is ours, not the sub's
    ah = sugs["after_hours|company|"]
    assert ah["origin"] == "content" and ah["on_checklist"] is True   # a missing checklist item: verified required
    sds = sugs["sds|company|"]
    assert sds["origin"] == "claude" and sds["on_checklist"] is True and "SDS" in sds["reason"]
    assert "coi|company|" not in sugs                      # ours is on file
    order = [g["on_checklist"] for g in _payload(admin)["suggestions"]]
    assert order == sorted(order, reverse=True)            # required ones first
    # filing the suggested document clears it
    _fake(title="COI — Delta Roofing", category="CONTRACTS", requirement_key="coi", type_key="coi",
          party="subcontractor", party_name="Delta Roofing")
    dc = _data(_drop(admin))
    assert admin.post(f"/api/documents/intake/{dc['id']}/approve", json={"type_key": "coi"}).status_code == 201
    assert coi_k not in {g["key"] for g in _payload(admin)["suggestions"]}
    # "Not needed"
    url = f"/api/projects/{PROJ}/documents/suggestions/dismiss"
    assert anon.post(url, json={"key": ah["key"]}).status_code == 401
    assert client.post(url, json={"key": ah["key"]}).status_code == 403
    assert pm.post(url, json={"key": ah["key"]}).status_code == 403
    assert admin.post(url, json={"key": "nonsense|x|"}).status_code == 400
    assert admin.post(url, json={}).status_code == 400
    assert admin.post("/api/projects/NOPE-404/documents/suggestions/dismiss", json={"key": ah["key"]}).status_code == 404
    assert admin.post(url, json={"key": ah["key"]}).status_code == 200
    assert admin.post(url, json={"key": ah["key"]}).status_code == 200   # idempotent
    keys = {g["key"] for g in _payload(admin)["suggestions"]}
    assert ah["key"] not in keys and sds["key"] in keys
    checks += 1

    # ---- the tab: Specifications + permits & insurance sections are gone; suggestions + party are in
    html = (ROOT / "dashboard-static.html").read_text(encoding="utf-8")
    for gone in ("proj-specs-tbody", "PROJ_loadAttachedSpecs", 'id="pd-package"', 'id="pk-overlay"', "renderPackage"):
        assert gone not in html, gone
    for there in ('id="pd-sugg"', "renderSuggest", "data-dzparty", "suggestions/dismiss"):
        assert there in html, there
    checks += 1

    print(f"PASS: documents drop zone — schema + taxonomy, pure read, gates, unread wait, AI card, approve, "
          f"newer version + share revoke, remove, party read, several per item, suggestions + dismiss, "
          f"tab sections ({checks} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
