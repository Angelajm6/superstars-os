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
  * an invoice's design: saving with invoice_id makes the document that invoice's document (Preview / PDF / client
    link), later saves follow it, a number in the text only preselects it, an amount it doesn't show is flagged, and
    DELETE /api/invoices/<id>/design puts the standard layout back
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
        try:
            conn.execute("DELETE FROM invoice WHERE project_code = ?", (JOB,))
        except Exception:
            conn.rollback()
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
    import invoice_docs
    invoice_docs.register(app)   # DELETE /api/invoices/<id>/design

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

                # the invoice's own document (Invoices & Payments): only when named, then it follows every save
                import invoices
                import invoice_docs
                invoices.ensure_schema(conn)
                now = datetime.now().isoformat(timespec="seconds")
                for num, st in (("INV-2026-9901", "issued"), ("INV-2026-9902", "draft")):
                    conn.execute("INSERT INTO invoice (number, kind, status, client_org_id, project_code, description, amount, "
                                 "created_at, updated_at) VALUES (?, 'deposit', ?, 0, ?, 'Deposit', 12500, ?, ?)", (num, st, JOB, now, now))
                conn.commit()
                inv1, inv2 = [r["id"] for r in conn.execute("SELECT id FROM invoice WHERE project_code = ? ORDER BY number", (JOB,)).fetchall()]
                claude_docs.update(conn, ids["admin"], did, html="<h1>Invoice INV-2026-9901</h1><p>Deposit</p><p>Total $12,500.00</p>")
                rows = {r["id"]: r for r in claude_docs.job_invoices(conn, did, JOB)}
                assert rows[inv1]["match"] and not rows[inv2]["match"]
                assert claude_docs.invoice_for(conn, did, JOB) is None and claude_docs.invoice_for(conn, did, JOB, suggest=True) == inv1
            finally:
                conn.close()
            picks = a.get(f"/api/claude/docs/{did}/invoices?project_code={JOB}").get_json()["data"]
            assert [x["id"] for x in picks if x["pick"]] == [inv1], picks
            assert pm.get(f"/api/claude/docs/{did}/invoices?project_code={JOB}").status_code == 403
            s3 = a.post(f"/api/claude/docs/{did}/save", json={"project_code": JOB, "invoice_id": inv1}).get_json()["data"]
            assert s3["invoice"]["number"] == "INV-2026-9901" and s3["invoice"]["warning"] is None, s3
            conn = _db()
            try:
                inv = invoices.get(conn, inv1)
                assert inv["design_doc_id"] == did and "Total $12,500.00" in inv["design_html"]
                doc = invoice_docs.render_document(conn, inv)
                assert "Total $12,500.00" in doc and "How to pay" not in doc and "<body>" in doc
                pub = invoice_docs.public(inv)
                assert pub["has_design"] and "design_html" not in pub
                assert "How to pay" in invoice_docs.render_document(conn, invoices.get(conn, inv2))   # the other one: standard
                # a later save follows the invoice without naming it; a design without the amount is flagged
                claude_docs.update(conn, ids["admin"], did, html="<h1>Invoice INV-2026-9901</h1><p>New look, total $9,000</p>")
                r4 = claude_docs.save_to_job(conn, ids["admin"], did, JOB)
                assert r4["invoice"]["id"] == inv1 and "$12,500.00" in r4["invoice"]["warning"], r4
                assert "New look" in invoices.get(conn, inv1)["design_html"]
                # a letter that mentions the invoice never becomes it
                letter = claude_docs.create(conn, ids["admin"], chat, None, "Reminder", "<p>Invoice INV-2026-9902 is due.</p>")["doc_id"]
                assert claude_docs.save_to_job(conn, ids["admin"], letter, JOB, "CONTRACTS")["invoice"] is None
                assert not invoices.get(conn, inv2).get("design_html")
                # Claude's card names the invoice; void / other jobs are refused
                actx = ca.Ctx(conn, {"id": ids["admin"], "role": "admin"}, "office")
                card = ca.BY_NAME["save_document_to_job"].card(actx, {"doc_id": did, "project_code": JOB, "invoice_id": inv1})
                assert any(f["key"] == "invoice_id" and "INV-2026-9901" in f["hint"] for f in card["fields"]), card
                assert any(i["number"] == "INV-2026-9901" and i["designed_with_claude_doc"] == did
                           for i in ca._t_job_docs(actx, {"project_code": JOB})["invoices"])
                try:
                    ca.BY_NAME["save_document_to_job"].run(actx, {"doc_id": did, "project_code": JOB, "invoice_id": 999999})
                    raise AssertionError("an invoice on another job can't be named")
                except LookupError:
                    pass
            finally:
                conn.close()
            d = a.delete(f"/api/invoices/{inv1}/design")
            assert d.status_code == 200, d.get_json()
            conn = _db()
            try:
                inv = invoices.get(conn, inv1)
                assert not inv.get("design_html") and not inv.get("design_doc_id")
                assert "How to pay" in invoice_docs.render_document(conn, inv)

                # titled as the invoice: every save path links it with nothing picked (Claude's card, the connector)
                titled = claude_docs.create(conn, ids["admin"], chat, None, "Invoice INV-2026-9902 - Cdc job",
                                            "<h1>Invoice</h1><p>Amount due $12,500.00</p>")["doc_id"]
                rt = claude_docs.save_to_job(conn, ids["admin"], titled, JOB, "CONTRACTS")
                assert rt["invoice"] and rt["invoice"]["id"] == inv2, rt
                assert invoices.get(conn, inv2)["design_doc_id"] == titled
                reminder = claude_docs.create(conn, ids["admin"], chat, None, "Payment reminder INV-2026-9901",
                                              "<p>Invoice INV-2026-9901 is due.</p>")["doc_id"]
                assert claude_docs.save_to_job(conn, ids["admin"], reminder, JOB, "CONTRACTS")["invoice"] is None
                assert not invoices.get(conn, inv1).get("design_html")
                # filed before the link existed: the next boot gives the invoice its design (PDF drawn on next open)
                invoice_docs.set_design(conn, inv2, None, None)
                conn.execute("DELETE FROM claude_doc_filing WHERE doc_id = ?", (titled,))
                conn.commit()
                claude_docs.ensure(conn)
                inv = invoices.get(conn, inv2)
                assert inv["design_doc_id"] == titled and "Amount due $12,500.00" in inv["design_html"] and not inv["pdf_path"]
                claude_docs.ensure(conn)   # idempotent
                assert invoices.get(conn, inv2)["design_doc_id"] == titled
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
        for needle in ("function docCards(docs)", "async function openDoc(id, flash)", "'/api/claude/docs/' + id + '/docx'", "Save to a job\\u2026", "function docFiled(pick)", "Update the job copy", "async function docInvoices()",
                       'sandbox="allow-same-origin"', "docFollow(v)"):
            assert needle in js, needle
        assert "Invoice <its number> - <place>" in ca.SYSTEM
        assert 'claude_docs.filed(conn, made["doc_id"], code, saved)' in (ROOT / "claude_connector.py").read_text(encoding="utf-8")
        assert "o.kind === 'page'" in (ROOT / "static/js/pdf_view.js").read_text(encoding="utf-8")   # Preview inside the app
        assert "data-preview=" in (ROOT / "dashboard-static.html").read_text(encoding="utf-8")
        assert "$('inv-preview').addEventListener" in (ROOT / "company_portal.html").read_text(encoding="utf-8")
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
