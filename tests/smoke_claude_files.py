#!/usr/bin/env python3
"""Claude page + attached files (accepted 2026-10-15): the BEHAVIORAL guard for claude_files.py.

Proves, through a Flask test client against an ISOLATED database (planted model replies only):
  * gating: anonymous 401; a client 403; the /claude page is served to admin / pm and refused to a client
  * attaching: only PDF / photos / Word / Excel / CSV / text, never empty, never over 20 MB, a broken
    PDF is refused with a plain message; each kind is read (pages, Word paragraphs, Excel cells)
  * at rest: the file on disk and its text in the database are encrypted (no readable bytes)
  * privacy: only the person who attached a file can open, send or remove it; a sent file stays
  * sending: up to 5 files per message, each sent once; the model receives PDFs and pictures as
    documents / images and Word / Excel / CSV as text, before the message, with an [Attached: …]
    line naming their ids; a message can be files only
  * the Files API path: a PDF is uploaded once (with an expiry) and then referred to by id; an
    over-100-page PDF goes as its text
  * saving to a bid only through an approval card; nothing is saved before Approve; a new bid can
    take the files too
  * files attached but never sent are removed after a day
  * logs carry ids / kinds / sizes only; the pages carry the Claude tab, Full page, 📎 and drop
Database: honors SSC_DB_URL like every gate suite; otherwise a throwaway SQLite snapshot.
Run:  venv/bin/python tests/smoke_claude_files.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import io
import json
import logging
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_claude_files_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key()})
for _k in ("ANTHROPIC_API_KEY", "SSC_ASSISTANT_FAKE", "SSC_ASSISTANT_MODEL", "SSC_ASSISTANT_DAILY_CAP_USD", "SSC_ASSISTANT_MONTHLY_BUDGET_USD", "SSC_ASSISTANT_USER_DAILY_USD", "SSC_CLAUDE_MODEL", "SSC_CLAUDE_EFFORT"):
    os.environ.pop(_k, None)

from flask import Flask  # noqa: E402

import assistant  # noqa: E402
import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import claude_files as cf  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import ssc_paths  # noqa: E402

TAG = "clfl"
PW = "ClaudeFilesSmokePassword2026"
ORG = "Clfl Smoke GC"
SECRET_NAME = "Clfl-secret-drawings.pdf"
WORD_LINE = "Clfl scope: replace 212 lintels on the east elevation"
CELL = "Clfl brick repointing"


class LogTrap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _db():
    return db_layer.connect(pragma_fk=True)


def _q(sql, args=()):
    conn = _db()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _x(sql, args=()):
    conn = _db()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ca.ensure(conn)
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = ",".join("?" * len(ids)) or "NULL"
        chats = [r["id"] for r in conn.execute(f"SELECT id FROM claude_chat WHERE user_id IN ({ph})", ids).fetchall()]
        purge(conn, "claude_action", "chat_id", chats)
        purge(conn, "claude_chat_message", "chat_id", chats)
        purge(conn, "claude_chat", "id", chats)
        purge(conn, "claude_file", "user_id", ids)
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name = ?", (ORG,)).fetchall()]
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({','.join('?' * len(orgs)) or 'NULL'})", orgs).fetchall()]
        purge(conn, "estimate_document", "estimate_id", ests)
        purge(conn, "estimate", "id", ests)
        conn.execute("DELETE FROM building WHERE address_key LIKE '%clfl smoke%'")
        conn.execute("DELETE FROM crm_organization WHERE name = ?", (ORG,))
        conn.commit()
        purge_users(conn, ids)
        conn.commit()
    finally:
        conn.close()


def _seed() -> dict:
    _cleanup()
    conn = _db()
    try:
        for role in ("admin", "pm", "client"):
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, "
                         "is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 0)",
                         (f"{TAG}.{role}@example.com", auth.hash_password(PW), role, f"Clfl {role.title()}"))
        conn.commit()
        ids = {r["role"]: r["id"] for r in conn.execute("SELECT id, role FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()}
        ids["org"] = crm.create_org(conn, name=ORG, relationship_type="client")
        e = estimates.create_estimate(conn, est_type="FR", borough="MN", client_org_id=ids["org"],
                                      building_address="7 Clfl Smoke Place", created_by=ids["admin"])
        ids["est"] = e["id"]
        return ids
    finally:
        conn.close()


def _pdf(pages=3) -> bytes:
    import pypdf
    w = pypdf.PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def _png() -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGBA", (3200, 1600), (200, 40, 40, 255)).save(out, "PNG")
    return out.getvalue()


def _docx() -> bytes:
    xml = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body><w:p><w:r><w:t>{WORD_LINE}</w:t></w:r></w:p><w:p><w:r><w:t>Second</w:t><w:tab/><w:t>line</w:t></w:r></w:p></w:body></w:document>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return out.getvalue()


def _xlsx() -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Line items"
    ws.append(["Item", "Qty", "Unit"])
    ws.append([CELL, 6400, "SF"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _script(steps):
    os.environ["SSC_ASSISTANT_FAKE"] = json.dumps(steps)
    ca._FAKE["step"] = 0


def main() -> int:
    app = Flask("clfl", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    ca.register(app)
    ca.RUN_INLINE = True
    trap = LogTrap()
    for name in ("ssc.claude_agent", "ssc.claude_files"):
        lg = logging.getLogger(name)
        lg.addHandler(trap)
        lg.setLevel(logging.INFO)

    def login(role):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.97.0.{len(role)}"})
        assert r.status_code == 200, (role, r.status_code)
        return c

    def up(client, name, data):
        return client.post("/api/claude/files", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")

    seen = []
    real_call = ca.call_model

    def spy(system_text, tools, messages):
        seen.append(json.loads(json.dumps(messages)))
        return real_call(system_text, tools, messages)
    ca.call_model = spy

    ids = _seed()
    me = ids["admin"]
    passed = 0
    try:
        # ---- gating + the page
        anon = app.test_client()
        assert up(anon, "a.txt", b"hi").status_code == 401
        a, pm, cl = login("admin"), login("pm"), login("client")
        assert up(cl, "a.txt", b"hi").status_code == 403
        page = a.get("/claude")
        assert page.status_code == 200 and 'id="clx-page"' in page.get_data(as_text=True)
        assert "no-store" in page.headers["Cache-Control"]
        assert pm.get("/claude").status_code == 200 and cl.get("/claude").status_code in (302, 403)
        passed += 1; print("  gating + page ok")

        # ---- what can be attached, and how each kind is read
        assert up(a, "setup.exe", b"MZ").status_code == 400
        assert "PDF, photos, Word, Excel" in up(a, "setup.exe", b"MZ").get_json()["error"]
        assert up(a, "empty.txt", b"").status_code == 400
        big = up(a, "big.txt", b"x" * (cf.MAX_BYTES + 1))
        assert big.status_code == 400 and "20 MB" in big.get_json()["error"]
        assert "could not be opened" in up(a, "broken.pdf", b"%PDF-1.4 not really").get_json()["error"]
        PDF3 = _pdf(3)
        r = up(a, SECRET_NAME, PDF3); assert r.status_code == 201, r.get_json()
        pdf = r.get_json()["data"]
        assert pdf["kind"] == "pdf" and pdf["label"].startswith("3 pages · ") and pdf["url"] == f"/api/claude/files/{pdf['id']}"
        img = up(a, "parapet.png", _png()).get_json()["data"]
        doc = up(a, "scope.docx", _docx()).get_json()["data"]
        xls = up(a, "bid form.xlsx", _xlsx()).get_json()["data"]
        csv = up(a, "rates.csv", b"item,rate\nlintel,410\n").get_json()["data"]
        assert [img["kind"], doc["kind"], xls["kind"], csv["kind"]] == ["image", "word", "excel", "text"]
        rows = {x["id"]: x for x in _q("SELECT * FROM claude_file WHERE user_id = ?", (me,))}
        disk = ssc_paths.resolve_data_path(rows[pdf["id"]]["path"]).read_bytes()
        assert disk.startswith(b"v1b:") and b"%PDF" not in disk                       # encrypted on disk
        assert WORD_LINE not in json.dumps(rows[doc["id"]]) and CELL not in json.dumps(rows[xls["id"]])   # and in the database
        conn = _db()
        try:
            assert WORD_LINE in cf.text_of(rows[doc["id"]]) and "Second\tline" in cf.text_of(rows[doc["id"]])
            assert "## Sheet: Line items" in cf.text_of(rows[xls["id"]]) and f"{CELL}\t6400\tSF" in cf.text_of(rows[xls["id"]])
        finally:
            conn.close()
        passed += 1; print("  attaching + reading each kind + encrypted at rest ok")

        # ---- privacy
        got = a.get(pdf["url"])
        assert got.status_code == 200 and got.data == PDF3                              # decrypted, byte for byte
        assert got.headers["Content-Disposition"].startswith("inline") and "sandbox" in got.headers["Content-Security-Policy"]
        assert a.get(xls["url"]).headers["Content-Disposition"].startswith("attachment")
        assert pm.get(pdf["url"]).status_code == 404 and pm.delete(pdf["url"]).status_code == 404
        tmp = up(a, "drop me.txt", b"remove me").get_json()["data"]
        assert a.delete(tmp["url"]).status_code == 200 and not _q("SELECT id FROM claude_file WHERE id = ?", (tmp["id"],))
        passed += 1; print("  privacy ok")

        # ---- sending: the model gets the files first, then the message with their ids
        _script([{"text": "The drawings show 3 pages."}])
        c = a.post("/api/claude/chats", json={"surface": "office"}).get_json()["data"]["chat"]["id"]
        theirs = up(pm, "pm.txt", b"pm file").get_json()["data"]
        assert a.post(f"/api/claude/chats/{c}/messages", json={"text": "x", "file_ids": [theirs["id"]]}).status_code == 404
        six = [up(a, f"n{i}.txt", b"note").get_json()["data"]["id"] for i in range(6)]
        assert a.post(f"/api/claude/chats/{c}/messages", json={"text": "x", "file_ids": six}).status_code == 400
        seen.clear()
        r = a.post(f"/api/claude/chats/{c}/messages", json={"text": "Start a bid from these", "page": {"surface": "office", "label": "Claude"},
                                                          "file_ids": [pdf["id"], img["id"], doc["id"], xls["id"]]})
        assert r.status_code == 202, r.get_json()
        first = seen[0][0]["content"]
        kinds = [b["type"] for b in first]
        assert kinds == ["document", "image", "document", "document", "text"], kinds
        assert first[0]["source"]["type"] == "base64" and first[0]["source"]["media_type"] == "application/pdf" and first[0]["title"] == SECRET_NAME
        assert first[1]["source"]["media_type"] == "image/jpeg"
        from PIL import Image
        assert max(Image.open(io.BytesIO(__import__("base64").b64decode(first[1]["source"]["data"]))).size) <= cf.IMG_MAX_EDGE
        assert first[2]["source"]["type"] == "text" and WORD_LINE in first[2]["source"]["data"]
        assert CELL in first[3]["source"]["data"]
        assert f'[Attached: file {pdf["id"]} "{SECRET_NAME}"' in first[4]["text"] and first[4]["text"].endswith("Start a bid from these")
        assert a.post(f"/api/claude/chats/{c}/messages", json={"text": "again", "file_ids": [pdf["id"]]}).status_code == 409
        view = a.get(f"/api/claude/chats/{c}").get_json()["data"]
        um = [m for m in view["messages"] if m["role"] == "user"][0]
        assert um["text"] == "Start a bid from these" and [f["id"] for f in um["files"]] == [pdf["id"], img["id"], doc["id"], xls["id"]]
        assert [f["name"] for f in view["files"]] == [SECRET_NAME, "parapet.png", "scope.docx", "bid form.xlsx"]
        # files only (no text), in a new conversation: named after the first file
        _script([{"text": "Got the rates."}])
        c2 = a.post("/api/claude/chats", json={"surface": "office"}).get_json()["data"]["chat"]["id"]
        r = a.post(f"/api/claude/chats/{c2}/messages", json={"text": "", "file_ids": [csv["id"]]})
        assert r.status_code == 202 and r.get_json()["data"]["chat"]["title"] == "rates.csv"
        assert a.post(f"/api/claude/chats/{c2}/messages", json={"text": ""}).status_code == 400
        passed += 1; print("  sending + what the model receives ok")

        # ---- the Files API path: uploaded once (with an expiry), then by id; a long PDF goes as text
        uploads = []
        real_upload, real_fake = cf._upload, assistant._fake
        cf._upload = lambda name, data, mime: uploads.append((name, mime, len(data))) or f"file_smoke_{len(uploads)}"
        assistant._fake = lambda: ""
        conn = _db()
        try:
            row = cf.owned(conn, me, [pdf["id"]])[0]
            b1 = cf.blocks(conn, dict(row))
            b2 = cf.blocks(conn, cf.owned(conn, me, [pdf["id"]])[0])
            assert b1[0]["source"] == {"type": "file", "file_id": "file_smoke_1"} == b2[0]["source"] and len(uploads) == 1
            assert uploads[0][1] == "application/pdf"
            exp = _q("SELECT api_expires_at FROM claude_file WHERE id = ?", (pdf["id"],))[0]["api_expires_at"]
            assert exp > __import__("datetime").datetime.now().isoformat()
            ib = cf.blocks(conn, cf.owned(conn, me, [img["id"]])[0])
            assert ib[0]["type"] == "image" and ib[0]["source"]["type"] == "file" and uploads[-1][1] == "image/jpeg"
        finally:
            conn.close()
            cf._upload, assistant._fake = real_upload, real_fake
        long_pdf = up(a, "long.pdf", _pdf(cf.PDF_PAGE_LIMIT + 1)).get_json()["data"]
        assert long_pdf["label"].endswith("read as text") and long_pdf["label"].startswith(f"{cf.PDF_PAGE_LIMIT + 1} pages")
        conn = _db()
        try:
            lb = cf.blocks(conn, cf.owned(conn, me, [long_pdf["id"]])[0])
            assert lb[0]["source"]["type"] == "text" and "--- page 1 ---" in lb[0]["source"]["data"]
        finally:
            conn.close()
        passed += 1; print("  Files API once + long PDF as text ok")

        # ---- saving to a bid: only through the card
        _script([{"tools": [{"name": "save_chat_files", "input": {"file_ids": [pdf["id"], doc["id"]], "estimate_id": ids["est"]}}]},
                 {"text": "Ready to save both files to the bid."}])
        a.post(f"/api/claude/chats/{c}/messages", json={"text": "Save the drawings and scope to the bid", "page": {"surface": "office"}})
        cards = [x for m in a.get(f"/api/claude/chats/{c}").get_json()["data"]["messages"] for x in m.get("actions", [])]
        card = [x for x in cards if x["tool"] == "save_chat_files"][0]
        files_field = [f for f in card["card"]["fields"] if f["key"] == "file_ids"][0]
        assert SECRET_NAME in files_field["hint"] and "scope.docx" in files_field["hint"]
        assert not _q("SELECT id FROM estimate_document WHERE estimate_id = ?", (ids["est"],))     # nothing before Approve
        r = a.post(f"/api/claude/actions/{card['id']}/approve", json={})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["data"]["result"]["summary"].endswith("saved to the bid")
        names = sorted(d["file_name"] for d in _q("SELECT file_name FROM estimate_document WHERE estimate_id = ?", (ids["est"],)))
        assert SECRET_NAME in names, names
        # someone else's file can't be put on a card
        _script([{"tools": [{"name": "save_chat_files", "input": {"file_ids": [theirs["id"]], "estimate_id": ids["est"]}}]}, {"text": "No."}])
        a.post(f"/api/claude/chats/{c}/messages", json={"text": "save theirs", "page": {"surface": "office"}})
        assert len([x for m in a.get(f"/api/claude/chats/{c}").get_json()["data"]["messages"] for x in m.get("actions", [])]) == len(cards)
        # a new bid takes the files too
        _script([{"tools": [{"name": "create_estimate", "input": {"kind": "bid", "est_type": "FR", "borough": "MN",
                                                                   "client_org_id": ids["org"], "building_address": "9 Clfl Smoke Way",
                                                                   "file_ids": [pdf["id"]]}}]}, {"text": "A bid is ready."}])
        a.post(f"/api/claude/chats/{c}/messages", json={"text": "make it a new bid", "page": {"surface": "office"}})
        nb = [x for m in a.get(f"/api/claude/chats/{c}").get_json()["data"]["messages"] for x in m.get("actions", []) if x["tool"] == "create_estimate"][0]
        assert any(f["key"] == "file_ids" and SECRET_NAME in f["hint"] for f in nb["card"]["fields"])
        r = a.post(f"/api/claude/actions/{nb['id']}/approve", json={})
        assert r.status_code == 200 and "1 document saved" in r.get_json()["data"]["result"]["summary"], r.get_json()
        passed += 1; print("  save to a bid only on Approve ok")

        # ---- attached, never sent: gone after a day
        old = up(a, "old.txt", b"old").get_json()["data"]
        _x("UPDATE claude_file SET created_at = '2020-01-01T00:00:00' WHERE id = ?", (old["id"],))
        oldp = ssc_paths.resolve_data_path(_q("SELECT path FROM claude_file WHERE id = ?", (old["id"],))[0]["path"])
        up(a, "new.txt", b"new")
        assert not _q("SELECT id FROM claude_file WHERE id = ?", (old["id"],)) and not oldp.exists()
        assert _q("SELECT id FROM claude_file WHERE id = ?", (pdf["id"],))                   # sent files stay
        passed += 1; print("  unsent files pruned ok")

        # ---- logs + pages
        logs = "\n".join(trap.lines)
        assert trap.lines and SECRET_NAME not in logs and WORD_LINE not in logs and CELL not in logs
        js = (ROOT / "static/js/claude_drawer.js").read_text(encoding="utf-8")
        assert "PAGE = !!HOST" in js and "file_ids: ready.map" in js and 'id="clx-clip"' in js and "Drop files to attach" in js
        assert '↗ Full page' in js and "'/claude' + (S.chat ? '#chat-'" in js and "Open in small window" in js
        nav = (ROOT / "static/js/company_nav.js").read_text(encoding="utf-8")
        assert "{ key: 'claude', label: 'Claude', href: '/claude', roles: ['admin', 'c_suite', 'pm', 'super']" in nav
        v = re.search(r"claude_drawer\.js\?v=(\d+)", (ROOT / "claude.html").read_text(encoding="utf-8")).group(1)
        for f in ("company_portal.html", "account.html", "dashboard-static.html", "company-dashboard.html", "estimating.html"):
            assert f"claude_drawer.js?v={v}\"" in (ROOT / f).read_text(encoding="utf-8"), f
        passed += 1; print("  logs + pages ok")
    finally:
        ca.call_model = real_call
        os.environ.pop("SSC_ASSISTANT_FAKE", None)
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"smoke_claude_files: {passed}/8 groups passed")
    return 0 if passed == 8 else 1


if __name__ == "__main__":
    sys.exit(main())
