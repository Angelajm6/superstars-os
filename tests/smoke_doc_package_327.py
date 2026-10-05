#!/usr/bin/env python3
"""#327 — permits & insurance package: behavioral guard.

Boots the real app (`import server`) on an ISOLATED database with a scratch data root
and the fake mail seam. Proves:

  * schema idempotent; migration script runs
  * gates: client -> 403 on the package; anon -> 401; super may READ but not SEND;
    unknown project -> 404
  * package readiness: the COI / bond / permit requirements with missing / on_file /
    expiring / expired, keyed docs and un-keyed permit extras, no *_path anywhere
  * send: nothing on file -> 400; bad email -> 400; a document outside the package
    -> 400; default = every deliverable doc; the email in the fake dir lists the
    documents and carries /d/<token>; documents shared into the client portal
    (item_visibility) when asked, not when declined; a CRM activity row
  * public page: bad token -> 404; good token -> 200 listing the documents, first
    open recorded; each file downloads through /d/<token>/file/<id> (inline, the
    right bytes); a doc outside the package -> 404; reachable logged out and as a
    logged-in client (containment allowlist)
  * red flag: flagging a document removes it from the sent package's page and its
    file route at once; a superseded version is never served
  * a second send is its own package (older link keeps working until it expires);
    the package payload reports last_sent / sends

Run:  venv/bin/python tests/smoke_doc_package_327.py
"""
from __future__ import annotations

import io
import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")   # test-only seams are inert without this (runtime_seams.py)
import re
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_pkg_327_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
MAIL_DIR = Path(_TMP) / "mail"
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ["SSC_MAIL_FAKE_DIR"] = str(MAIL_DIR)
os.environ["APP_BASE_URL"] = "http://test.local"
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import doc_packages  # noqa: E402
import server  # noqa: E402  (boots the whole app)
import visibility  # noqa: E402

TAG = "pkg327"
PW = "PackageSmokePassword2026"
ROLES = ("c_suite", "admin", "pm", "super", "client")
TODAY = date.today()
PROJ = "PKG327-A"
API = f"/api/projects/{PROJ}/documents/package"


def D(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for role in ROLES:
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), role, f"Smoke {role}"))
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Pkg327 %'")
        ids["org"] = crm.create_org(conn, name="Pkg327 Owners", relationship_type="client")
        crm.create_contact(conn, full_name="Pkg327 Dana Owner", org_id=ids["org"], email="pkg327.dana@example.com")
        conn.execute("DELETE FROM projects WHERE project_code = ?", (PROJ,))
        conn.execute("INSERT INTO projects (project_code, name, address, status, client_org_id) VALUES (?, 'Pkg327 Facade', "
                     "'8 Pkg327 Way', 'active', ?)", (PROJ, ids["org"]))
        # the PM must be assigned to the project to pass the scoping gate
        pm_id = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.pm@example.com",)).fetchone()["id"]
        try:
            conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, ?)", (pm_id, PROJ, D(0)))
        except Exception:
            conn.rollback()
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = _db()
    try:
        doc_ids = [r["id"] for r in conn.execute("SELECT id FROM project_documents WHERE project_code = ?", (PROJ,)).fetchall()]
        pkg_ids = [r["id"] for r in conn.execute("SELECT id FROM document_package WHERE project_code = ?", (PROJ,)).fetchall()]
        purge(conn, "document_package_item", "package_id", pkg_ids)
        purge(conn, "document_package", "id", pkg_ids)
        for t in ("item_visibility", "item_redflag", "visibility_audit"):
            purge(conn, t, "item_id", doc_ids)
        purge(conn, "project_documents", "id", doc_ids)
        purge(conn, "pm_project_assignment", "project_code", [PROJ])
        conn.execute("DELETE FROM projects WHERE project_code = ?", (PROJ,))
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Pkg327 %'").fetchall()]
        for t, col in (("crm_activity", "entity_id"), ("crm_task", "entity_id"), ("crm_contact", "org_id")):
            purge(conn, t, col, org_ids)
        purge(conn, "crm_organization", "id", org_ids)
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
            assert not k.endswith("_path") and k != "token_hash", f"{k!r} leaked in {where}"
            _no_paths(v, where)
    elif isinstance(obj, list):
        for v in obj:
            _no_paths(v, where)


def _login(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code, r.get_data(as_text=True)[:200])
    return c


def _upload(c, *, category, key, title, expiry=None, name="doc.pdf", body=b"%PDF-1.4 pkg327 %%EOF"):
    data = {"file": (io.BytesIO(body), name), "category": category, "title": title}
    if key:
        data["requirement_key"] = key
    if expiry:
        data["expiry_date"] = expiry
    r = c.post(f"/api/projects/{PROJ}/documents", data=data, content_type="multipart/form-data")
    assert r.status_code in (200, 201), (r.status_code, r.get_data(as_text=True)[:300])
    j = r.get_json()
    d = j.get("data") or j
    return d["id"]


def _latest_mail() -> tuple:
    eml = sorted(MAIL_DIR.glob("docpkg-*.eml"))
    assert eml, "no package email written"
    body = eml[-1].read_text(encoding="utf-8")
    m = re.search(r"http://test\.local/d/([A-Za-z0-9_\-]+)", body)
    assert m, body[:300]
    return m.group(1), body, len(eml)


def main() -> int:
    conn = _db()
    try:
        doc_packages.ensure_schema(conn)
        doc_packages.ensure_schema(conn)   # idempotent
    finally:
        conn.close()
    import apply_doc_packages_327 as mig
    assert mig.main() == 0
    ids = _seed()
    checks = 0

    admin, cs, pm, sup, cli = (_login(r) for r in ("c_suite", "admin", "pm", "super", "client"))
    admin, cs = cs, admin  # names by role: `admin` is the admin client
    # ---- gates
    assert cli.get(API).status_code == 403
    assert server.app.test_client().get(API).status_code == 401
    assert admin.get("/api/projects/PKG327-NOPE/documents/package").status_code == 404
    assert sup.get(API).status_code == 200, "super reads the package"
    assert sup.post(API + "/send", json={}).status_code == 403, "super cannot send"
    checks += 1

    # ---- empty package
    j = admin.get(API).get_json()
    _no_paths(j, "empty package")
    d = j["data"]
    assert d["summary"]["required"] == len(doc_packages.PACKAGE_KEYS) and d["summary"]["on_file"] == 0
    assert all(it["status"] == "missing" for it in d["items"]) and d["last_sent"] is None and d["sends"] == 0
    assert d["client"]["name"] == "Pkg327 Owners" and d["contacts"][0]["email"] == "pkg327.dana@example.com"
    labels = {it["requirement_key"]: it["label"] for it in d["items"]}
    assert labels["coi"].startswith("Certificate of Insurance") and "pw2" in labels
    r = admin.post(API + "/send", json={})
    assert r.status_code == 400 and "nothing to send" in r.get_json()["error"]
    checks += 1

    # ---- paper on file: COI (expiring), PW2 (valid), an old bond (expired), an un-keyed permit extra, a drawing (outside)
    coi = _upload(admin, category="CONTRACTS", key="coi", title="Pkg327 COI 2026", expiry=D(12), name="coi.pdf", body=b"%PDF-1.4 COI-BYTES %%EOF")
    pw2 = _upload(admin, category="PERMITS", key="pw2", title="Pkg327 PW2", expiry=D(300), name="pw2.pdf", body=b"%PDF-1.4 PW2-BYTES %%EOF")
    bond = _upload(admin, category="CONTRACTS", key="bond", title="Pkg327 Bond", expiry=D(-3), name="bond.pdf")
    extra = _upload(admin, category="PERMITS", key=None, title="Pkg327 Street variance letter", name="var.pdf")
    drawing = _upload(admin, category="DRAWINGS", key="approved_arch", title="Pkg327 Arch set", name="arch.pdf")
    j = admin.get(API).get_json()
    _no_paths(j, "package")
    d = j["data"]
    st = {it["requirement_key"]: it["status"] for it in d["items"]}
    assert st["coi"] == "expiring" and st["pw2"] == "on_file" and st["bond"] == "expired" and st["sidewalk_shed"] == "missing", st
    assert d["summary"]["on_file"] == 1 and d["summary"]["attention"] == 2 and d["summary"]["missing"] == len(doc_packages.PACKAGE_KEYS) - 3
    assert [x["id"] for x in d["extras"]] == [extra], d["extras"]
    coi_item = [it for it in d["items"] if it["requirement_key"] == "coi"][0]
    assert coi_item["doc"]["file_url"] == f"/api/documents/{coi}/file" and coi_item["doc"]["shared_client"] is False
    checks += 1

    # ---- send rules + the email
    assert admin.post(API + "/send", json={"to_email": "nope"}).status_code == 400
    r = admin.post(API + "/send", json={"document_ids": [drawing]})
    assert r.status_code == 400 and "not part" in r.get_json()["error"]
    r = pm.post(API + "/send", json={"message": "Attached is everything the board asked for.", "share_in_portal": True})
    assert r.status_code == 201, r.get_json()
    s = r.get_json()["data"]
    _no_paths(r.get_json(), "send")
    assert s["sent_to"] == "pkg327.dana@example.com" and s["sent_name"] == "Pkg327 Dana Owner" and s["doc_count"] == 4
    assert s["shared_in_portal"] == 4
    token1, body, n = _latest_mail()
    assert n == 1 and "Attached is everything" in body and "Certificate of Insurance" in body and "Street variance" in body
    conn = _db()
    try:
        for did in (coi, pw2, bond, extra):
            assert visibility.is_shared(conn, "document", did, "client"), did
        assert not visibility.is_shared(conn, "document", drawing, "client")
        act = conn.execute("SELECT summary FROM crm_activity WHERE entity_type='organization' AND entity_id=? AND summary LIKE ?",
                           (ids["org"], "Permits & insurance package for%")).fetchone()
        assert act and "4 documents" in act["summary"], act
    finally:
        conn.close()
    d = admin.get(API).get_json()["data"]
    assert d["last_sent"]["sent_to"] == "pkg327.dana@example.com" and d["last_sent"]["viewed_at"] is None and d["sends"] == 1
    assert d["last_sent"]["link_active"] is True
    checks += 1

    # ---- public page + files
    anon = server.app.test_client()
    assert anon.get("/d/not-a-real-token-at-all-000").status_code == 404
    r = anon.get(f"/d/{token1}")
    assert r.status_code == 200, r.status_code
    page = r.get_data(as_text=True)
    assert "Pkg327 Facade" in page and "Certificate of Insurance" in page and "Street variance" in page and "Arch set" not in page
    assert "noindex" in (r.headers.get("X-Robots-Tag") or "")
    r = anon.get(f"/d/{token1}/file/{coi}")
    assert r.status_code == 200 and r.data.startswith(b"%PDF-1.4 COI-BYTES") and "attachment" not in (r.headers.get("Content-Disposition") or "")
    assert anon.get(f"/d/{token1}/file/{drawing}").status_code == 404
    assert cli.get(f"/d/{token1}").status_code == 200, "a logged-in client reaches the link"
    d = admin.get(API).get_json()["data"]
    assert d["last_sent"]["viewed_at"] and d["last_sent"]["view_count"] == 2   # anon open + the client's open
    checks += 1

    # ---- red flag pulls a document from the sent package; superseded is never served
    conn = _db()
    try:
        visibility.redflag(conn, "document", coi, None)
        conn.commit()
    finally:
        conn.close()
    page = anon.get(f"/d/{token1}").get_data(as_text=True)
    assert "Certificate of Insurance" not in page and "Pkg327 PW2" in page
    assert anon.get(f"/d/{token1}/file/{coi}").status_code == 404
    assert anon.get(f"/d/{token1}/file/{pw2}").status_code == 200
    d = admin.get(API).get_json()["data"]
    coi_item = [it for it in d["items"] if it["requirement_key"] == "coi"][0]
    assert coi_item["doc"]["flagged"] is True
    r = admin.post(API + "/send", json={"document_ids": [coi]})
    assert r.status_code == 400, "a flagged document cannot be sent"
    conn = _db()
    try:
        conn.execute("UPDATE project_documents SET superseded = 1 WHERE id = ?", (pw2,))
        conn.commit()
    finally:
        conn.close()
    assert anon.get(f"/d/{token1}/file/{pw2}").status_code == 404
    checks += 1

    # ---- second send: its own link, the first keeps working; share declined
    r = admin.post(API + "/send", json={"to_email": "pkg327.board@example.com", "to_name": "Board Secretary",
                                        "document_ids": [extra], "share_in_portal": False})
    assert r.status_code == 201 and r.get_json()["data"]["shared_in_portal"] == 0
    token2, body, n = _latest_mail()
    assert n == 2 and token2 != token1 and "Hello Board," in body
    assert anon.get(f"/d/{token2}").status_code == 200 and anon.get(f"/d/{token1}").status_code == 200
    d = admin.get(API).get_json()["data"]
    assert d["sends"] == 2 and d["last_sent"]["sent_to"] == "pkg327.board@example.com" and d["last_sent"]["doc_count"] == 1
    checks += 1

    print(f"PASS: #327 permits & insurance package — schema, gates, readiness, send rules + email + portal share, "
          f"public page + files + containment, red-flag revocation, superseded never served, resend ({checks} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
