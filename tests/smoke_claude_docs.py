#!/usr/bin/env python3
"""Documents Claude makes (accepted prototype 2026-10-20, https://claude.ai/artifact/SPTSgJ8bv7N8ENWMbzrXrE).

Proves against an ISOLATED database (no model call, no PDF engine):
  * make_document / edit_document / read_document are tools every team role has (no approval card: they change
    no record); Claude's HTML is cleaned (no scripts, handlers, outside URLs)
  * the chat view carries the document card on Claude's reply (made, then updated)
  * the owner opens it in edit mode, saves edits and a new name; another person gets 404
  * Word download is a real .docx (zip with word/document.xml holding the text and the table); PDF goes through the
    engine; save to a job puts the PDF in that job's documents (admin / C-suite only)
  * saving again to the same job files a NEW VERSION of that copy (old one superseded, kept in history, folder kept);
    an edit after saving marks the job copy out of date; Claude's edit_document says so and save_document_to_job (an
    approval card, office roles) replaces the copy, or a document named by job_documents; old saves are linked by backfill
  * the instructions say Claude CAN make documents; past proposals reach the drafting prompt; the page and the
    Claude window carry the pieces
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import io
import json
import shutil
import sys
import tempfile
import types
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_cdocs_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key()})

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import claude_docs  # noqa: E402
import db_layer  # noqa: E402

TAG = "cdc"
PW = "ClaudeDocsSmokePassword2026"
JOB = "FR-MN-CDC1"


def _db():
    return db_layer.connect(pragma_fk=True)


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ca.ensure(conn)
        claude_docs.ensure(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        chats = [r["id"] for r in conn.execute(f"SELECT id FROM claude_chat WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "claude_doc_ref", "chat_id", chats)
        docs = [r["id"] for r in conn.execute(f"SELECT id FROM claude_doc WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "claude_doc_filing", "doc_id", docs)
        conn.execute("DELETE FROM project_documents WHERE project_code = ?", (JOB,))
        purge(conn, "claude_doc", "user_id", uids)
        purge(conn, "claude_chat_message", "chat_id", chats)
        purge(conn, "claude_chat", "id", chats)
        conn.execute("DELETE FROM projects WHERE project_code = ?", (JOB,))
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    _cleanup()
    conn = _db()
    try:
        for role in ("admin", "pm"):
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 0)", (f"{TAG}.{role}@superstarscontracting.com", auth.hash_password(PW), role, f"Cdc {role}"))
        conn.execute("INSERT INTO projects (project_code, name, status) VALUES (?, 'Cdc job', 'active')", (JOB,))
        conn.commit()
        ids = {r["role"]: r["id"] for r in conn.execute("SELECT id, role FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()}
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("INSERT INTO claude_chat (user_id, surface, title, created_at, updated_at) VALUES (?, 'office', 'CD5', ?, ?)", (ids["admin"], now, now))
        conn.commit()
        chat = conn.execute("SELECT MAX(id) AS m FROM claude_chat WHERE user_id = ?", (ids["admin"],)).fetchone()["m"]
        # Claude's turn: a reply that calls make_document, then a text reply
        mid1 = ca._add_msg(conn, chat, "assistant", "text", [{"type": "tool_use", "id": "tu1", "name": "make_document", "input": {}}])
        ctx = ca.Ctx(conn, {"id": ids["admin"], "role": "admin"}, "office")
        r = ca._tool_result(ctx, chat, mid1, {"type": "tool_use", "id": "tu1", "name": "make_document", "input": {
            "title": "CD5 – 410 E 58th St",
            "html": "<h1>CD5</h1><script>alert(1)</script><p onclick=\"x()\">Building: 410 E 58th St</p><img src=\"https://evil.example/x.png\">"
                    "<h2>Conditions</h2><table><tr><th>Elevation</th><th>Condition</th></tr><tr><td>East</td><td>Six lintels</td></tr></table>"}})
        assert not r.get("is_error"), r
        did = json.loads(r["content"])["doc_id"]
        ca._add_msg(conn, chat, "assistant", "text", [{"type": "text", "text": "I filled the CD5."}])
        pmctx = ca.Ctx(conn, {"id": ids["pm"], "role": "pm"}, "field")
        assert ca.BY_NAME["make_document"].allowed("pm", "field") and not ca.BY_NAME["make_document"].write
        d = claude_docs.get(conn, ids["admin"], did)
        assert "<script" not in d["html"] and "onclick" not in d["html"] and "evil.example" not in d["html"] and "Six lintels" in d["html"]
        view = ca._chat_view(conn, ca._one(conn, "SELECT * FROM claude_chat WHERE id = ?", (chat,)))
        last = [m for m in view["messages"] if m["role"] == "assistant"][-1]
        assert last["text"] == "I filled the CD5." and last["docs"][0]["id"] == did and last["docs"][0]["kind"] == "created", last
        assert "Document" not in last["looked"]
        try:
            ca._tool_result(pmctx, chat, mid1, {"type": "tool_use", "id": "tu2", "name": "edit_document", "input": {"doc_id": did, "html": "<p>x</p>"}})
            r2 = ca._tool_result(pmctx, chat, mid1, {"type": "tool_use", "id": "tu2", "name": "edit_document", "input": {"doc_id": did, "html": "<p>x</p>"}})
            assert r2.get("is_error"), "another person can't change it"
        finally:
            pass
    finally:
        conn.close()
    passed = 1; print("  tools + chat view ok")

    app = Flask("cdc", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    ca.register(app)
    claude_docs.register(app)

    def login(role):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@superstarscontracting.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.98.0.{len(role)}"})
        assert r.status_code == 200
        return c
    try:
        a, pm = login("admin"), login("pm")
        g = a.get(f"/api/claude/docs/{did}").get_json()["data"]
        assert 'contenteditable="true"' in g["html"] and "Six lintels" in g["html"]
        assert pm.get(f"/api/claude/docs/{did}").status_code == 404
        p = a.patch(f"/api/claude/docs/{did}", json={"title": "CD5 final", "html": "<h1>CD5</h1><p>Building: 410 E 58th St, Block 1350</p>"
                                                     "<table><tr><th>Elevation</th></tr><tr><td>North</td></tr></table>"})
        assert p.status_code == 200 and p.get_json()["data"]["title"] == "CD5 final"
        dx = a.get(f"/api/claude/docs/{did}/docx")
        assert dx.status_code == 200 and 'CD5 final.docx' in dx.headers["Content-Disposition"]
        z = zipfile.ZipFile(io.BytesIO(dx.data))
        xml = z.read("word/document.xml").decode("utf-8")
        assert "Block 1350" in xml and "<w:tbl>" in xml and "North" in xml and "Heading1" in xml
        real = claude_docs.pdf_bytes
        claude_docs.pdf_bytes = lambda d: b"%PDF-1.4 cdc"
        saved = []

        def _insert(conn, code, fs, form, supersedes_id=None):   # the job-documents row, as server._projdoc_insert writes it
            saved.append((code, form["category"], fs.filename, fs.read()))
            conn.execute("INSERT INTO project_documents (project_code, category, requirement_key, title, file_path, file_name, "
                         "notes, superseded, uploaded_by_uid, uploaded_at, supersedes_id) VALUES (?,?,?,?,?,?,?,0,?,?,?)",
                         (code, form["category"], form.get("requirement_key") or None, form["title"], "/dev/null", fs.filename,
                          form.get("notes"), ids["admin"], datetime.now().isoformat(), supersedes_id))
            return conn.execute("SELECT MAX(id) AS m FROM project_documents").fetchone()["m"]
        fake = types.ModuleType("server")
        fake._projdoc_insert = _insert
        sys.modules["server"] = fake
        try:
            pdf = a.get(f"/api/claude/docs/{did}/pdf")
            assert pdf.status_code == 200 and pdf.data == b"%PDF-1.4 cdc"
            assert pm.post(f"/api/claude/docs/{did}/save", json={"project_code": JOB, "category": "INSPECTIONS"}).status_code == 403
            assert a.post(f"/api/claude/docs/{did}/save", json={"project_code": JOB, "category": "nope"}).status_code == 400
            s = a.post(f"/api/claude/docs/{did}/save", json={"project_code": JOB, "category": "INSPECTIONS"})
            assert s.status_code == 200, s.get_json()
            assert saved == [(JOB, "INSPECTIONS", "CD5 final.pdf", b"%PDF-1.4 cdc")]
            first = s.get_json()["data"]
            assert first["replaced"] is None and [f["document_id"] for f in first["filed"]] == [first["id"]]
            assert [j["project_code"] for j in a.get("/api/claude/docs/jobs").get_json()["data"]].count(JOB) == 1
            g = a.get(f"/api/claude/docs/{did}").get_json()["data"]
            assert g["filed"][0]["project_code"] == JOB and not g["filed"][0]["stale"]

            # a new design after saving: the job copy is out of date until it is updated
            conn = _db()
            try:
                conn.execute("UPDATE claude_doc SET updated_at = '2000-01-01T00:00:00' WHERE id = ?", (did,))
                conn.execute("UPDATE claude_doc_filing SET filed_at = '2000-01-01T00:00:00' WHERE doc_id = ?", (did,))
                conn.commit()
            finally:
                conn.close()
            p = a.patch(f"/api/claude/docs/{did}", json={"html": "<h1>CD5</h1><p>New look</p>"}).get_json()["data"]
            assert p["filed"][0]["stale"], p
            s2 = a.post(f"/api/claude/docs/{did}/save", json={"project_code": JOB}).get_json()["data"]   # no folder: kept
            assert s2["replaced"] == first["id"] and saved[-1][1] == "INSPECTIONS", s2
            assert [f["document_id"] for f in s2["filed"]] == [s2["id"]] and not s2["filed"][0]["stale"]
            conn = _db()
            try:
                rows = {r["id"]: r for r in conn.execute("SELECT id, superseded, supersedes_id FROM project_documents "
                                                        "WHERE project_code = ?", (JOB,)).fetchall()}
                assert rows[first["id"]]["superseded"] == 1 and rows[s2["id"]]["superseded"] == 0
                assert rows[s2["id"]]["supersedes_id"] == first["id"]

                # Claude: edit_document says the job copy is out of date; save_document_to_job replaces it (approval card)
                conn.execute("UPDATE claude_doc_filing SET filed_at = '2000-01-01T00:00:00' WHERE doc_id = ?", (did,))
                conn.commit()
                actx = ca.Ctx(conn, {"id": ids["admin"], "role": "admin"}, "office")
                r = ca._tool_result(actx, chat, mid1, {"type": "tool_use", "id": "tu3", "name": "edit_document",
                                                      "input": {"doc_id": did, "html": "<h1>CD5</h1><p>Newer look</p>"}})
                out = json.loads(r["content"])
                assert out["filed_in"] == [JOB] and "save_document_to_job" in out["note"], out
                tool = ca.BY_NAME["save_document_to_job"]
                assert tool.write and tool.allowed("admin", "office") and not tool.allowed("pm", "field")
                inp = {"doc_id": did, "project_code": JOB}
                assert tool.card(actx, inp)["title"] == "Update the job's copy"
                r = ca._tool_result(actx, chat, mid1, {"type": "tool_use", "id": "tu4", "name": "save_document_to_job", "input": inp})
                assert not r.get("is_error") and conn.execute("SELECT status FROM claude_action WHERE tool_use_id = 'tu4'").fetchone()["status"] == "pending"
                tool.run(actx, inp)
                res = tool.execute(actx, inp)
                assert "earlier one is in its history" in res["summary"], res
                cur = [d for d in claude_docs.filings(conn, did)]
                assert len(cur) == 1 and cur[0]["document_id"] == res["id"] and not cur[0]["stale"]
                listed = ca._t_job_docs(actx, {"project_code": JOB})["documents"]
                assert [x["document_id"] for x in listed] == [res["id"]] and listed[0]["made_with_claude"]
                # replace a document named by id (made in another chat): only a current document of that job
                bad = dict(inp, replaces_document_id=first["id"])
                try:
                    tool.run(actx, bad); raise AssertionError("an old version can't be replaced")
                except ValueError:
                    pass

                # a copy saved before the link existed is found again by its title
                conn.execute("DELETE FROM claude_doc_filing WHERE doc_id = ?", (did,))
                conn.commit()
                claude_docs.ensure(conn); claude_docs.ensure(conn)
                assert [f["document_id"] for f in claude_docs.filings(conn, did)] == [res["id"]]
            finally:
                conn.close()
        finally:
            claude_docs.pdf_bytes = real
            sys.modules.pop("server", None)
        passed += 1; print("  open / edit / Word / PDF / save ok")

        assert "Never say you cannot create" in ca.SYSTEM and "make_document" in ca.SYSTEM
        import proposal_studio
        src = (ROOT / "proposal_engine.py").read_text(encoding="utf-8")   # 2026-10-21: one engine writes every proposal
        assert "<closest_past_proposals>" in src and "Copy the closest past" in src
        js = (ROOT / "static/js/claude_drawer.js").read_text(encoding="utf-8")
        for needle in ("function docCards(docs)", "async function openDoc(id, flash)", "'/api/claude/docs/' + id + '/docx'", "Save to a job\\u2026", "function docFiled(pick)", "Update the job copy",
                       'sandbox="allow-same-origin"', "docFollow(v)"):
            assert needle in js, needle
        html = (ROOT / "proposal_studio.html").read_text(encoding="utf-8")
        assert "House style Claude follows" not in html and "Your past proposals" in html
        passed += 1; print("  wiring ok")
    finally:
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: claude docs ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
