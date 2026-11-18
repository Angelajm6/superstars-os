#!/usr/bin/env python3
"""Claude files emailed documents into the job's Documents (job_filing.py, 2026-11-18).

Proves against an ISOLATED database (fictional people only, files in a temp data root):
  * converting an estimate to a job makes ONE card for the person who did it: the estimate's own files first, then the
    attachments in their threads linked to the estimate or naming the same address (sent and received); a signature
    logo, a .zip and another building's email are never offered; the Documents tab finds the card; Needs you shows it
  * Approve files only the ticked rows with the person's title and folder, remembers where each came from, files the
    email on the job; identical bytes already on the job are skipped
  * on an email for an existing job: a newer revision becomes the new version (the old one superseded); a file already
    on the job shows as "Already filed"; another person's email is refused
  * Undo removes what it filed and brings the old version back; a card whose files are all on the job closes itself
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import base64
import json
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_jfile_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(Path(_TMP) / "mail"), "SSC_GMAIL_POLL": "0",
                   "SSC_REMINDERS": "0", "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(), "SSC_ASSISTANT_FAKE": "1"})

from flask import Flask  # noqa: E402

import assistant  # noqa: E402
import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import inbox  # noqa: E402
import job_filing as jf  # noqa: E402
import ssc_paths  # noqa: E402
import today_feed  # noqa: E402

TAG = "jfile"
PW = "JobFilingSmoke2026pw"
ME = f"{TAG}.me@superstarscontracting.com"
MATE = f"{TAG}.mate@superstarscontracting.com"
CLIENT = "Jfile Smoke Harborview Owners"
NOW = time.time()
SIZES = {"A-201 Parapet details.pdf": 180000, "logo.png": 4000, "Schedule draft.xlsx": 48000, "site photos.zip": 900000,
         "Other building plans.pdf": 50000, "41JF Shop Drawings Rev C.pdf": 240000, "COI copy.pdf": 19000}


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def gmsg(n: int, subject: str, body: str, files: list, out: bool = False, to=ME) -> dict:
    frm = to if out else "Jordan Jfilesmoke <jordan@jfile-harbor.example>"
    rcpt = "Jordan Jfilesmoke <jordan@jfile-harbor.example>" if out else to
    hs = [{"name": "From", "value": frm}, {"name": "To", "value": rcpt}, {"name": "Subject", "value": subject},
          {"name": "Message-ID", "value": f"<jfile{n}@jfile.example>"}]
    parts = [{"mimeType": "text/plain", "body": {"data": _b64(body)}}]
    parts += [{"mimeType": "application/octet-stream", "filename": f, "headers": [{"name": "Content-Disposition", "value": f"attachment; filename={f}"}],
               "body": {"attachmentId": f"att-{n}-{i}", "size": SIZES[f]}} for i, f in enumerate(files)]
    return {"id": f"g-jfile-{n}", "threadId": f"t-jfile-{n}", "labelIds": ["SENT"] if out else ["INBOX", "UNREAD"],
            "internalDate": str(int((NOW - 3600 * n) * 1000)), "snippet": body[:60],
            "payload": {"mimeType": "multipart/mixed", "headers": hs, "parts": parts}}


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        for m in (ca, inbox, today_feed):
            m.ensure(conn)
        jf.ensure_schema(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        mids = [r["id"] for r in conn.execute(f"SELECT id FROM mail_message WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "mail_link", "message_id", mids)
        purge(conn, "mail_message", "id", mids)
        chats = [r["id"] for r in conn.execute(f"SELECT id FROM claude_chat WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "claude_action", "chat_id", chats)
        purge(conn, "claude_chat_message", "chat_id", chats)
        purge(conn, "claude_chat", "id", chats)
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name = ?", (CLIENT,)).fetchall()]
        ests = [r["id"] for r in conn.execute(f"SELECT id, converted_project_code FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        codes = [r["converted_project_code"] for r in conn.execute(f"SELECT converted_project_code FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall() if r["converted_project_code"]]
        codes += [r["project_code"] for r in conn.execute(f"SELECT project_code FROM projects WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        for c in set(codes):
            conn.execute("DELETE FROM project_documents WHERE project_code = ?", (c,))
            conn.execute("DELETE FROM job_file_seen WHERE project_code = ?", (c,))
            for t in ("project_buildings", "ira_job"):
                try:
                    conn.execute(f"DELETE FROM {t} WHERE project_code = ?", (c,))
                except Exception:
                    conn.rollback()
            conn.execute("DELETE FROM projects WHERE project_code = ?", (c,))
        purge(conn, "estimate_document", "estimate_id", ests)
        purge(conn, "estimate_ira", "estimate_id", ests)
        purge(conn, "estimate", "id", ests)
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM building WHERE address_line LIKE '%Jfile Terrace%'")
        conn.execute("DELETE FROM crm_organization WHERE name = ?", (CLIENT,))
        conn.commit()
        purge_users(conn, uids)
    finally:
        conn.close()


def main() -> int:
    app = Flask("jfile", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    ca.register(app)
    inbox.register(app)
    today_feed.register(app)
    _cleanup()
    real_bytes = inbox.attachment_bytes
    inbox.attachment_bytes = lambda conn, uid, m, att: ("bytes of " + att["name"]).encode("utf-8")   # distinct per file
    conn = db_layer.connect(pragma_fk=True)
    passed = 0
    try:
        ids = {}
        for key in ("me", "mate"):
            conn.execute("INSERT INTO users (email, password_hash, full_name, role, status, must_reset_password, created_at) "
                         "VALUES (?,?,?,?,'active',0,?)", (f"{TAG}.{key}@superstarscontracting.com", auth.hash_password(PW),
                                                           f"Jfile {key.title()}", "admin", datetime.now().isoformat(timespec="seconds")))
            conn.commit()
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.{key}@superstarscontracting.com",)).fetchone()["id"]
        uid = ids["me"]
        org = crm.create_org(conn, name=CLIENT, relationship_type="client", created_by=uid)
        crm.create_contact(conn, org_id=org, full_name="Jordan Jfilesmoke", email="jordan@jfile-harbor.example", created_by=uid)
        est = estimates.create_estimate(conn, est_type="FR", borough="BK", client_org_id=org,
                                        building_address="41 Jfile Terrace, Brooklyn, NY", created_by=uid)
        # the estimate's own file, on disk under the data root
        edir = ssc_paths.under_root("data_room", "estimates", "jfile")
        edir.mkdir(parents=True, exist_ok=True)
        (edir / "fisp.pdf").write_bytes(b"bytes of the FISP report")
        conn.execute("INSERT INTO estimate_document (estimate_id, category, title, doc_type, file_path, file_name, file_size, mime, "
                     "uploaded_by, uploaded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (est["id"], "drawing", "FISP report 2025", "PDF", str(edir / "fisp.pdf"), "FISP report 2025.pdf", 24, "application/pdf",
                      uid, datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        # 1: received, linked to the estimate (drawing + a signature logo + a zip)
        m1 = inbox.ingest(conn, uid, gmsg(1, "Bid invitation - parapets", "Please price the attached.",
                                          ["A-201 Parapet details.pdf", "logo.png", "site photos.zip"]), ME)
        # 2: sent, names the address (matched to the client)
        m2 = inbox.ingest(conn, uid, gmsg(2, "Schedule", "Draft schedule for 41 Jfile Terrace attached.", ["Schedule draft.xlsx"], out=True), ME)
        # 3: another building, same client: never offered
        m3 = inbox.ingest(conn, uid, gmsg(3, "Other job", "Plans for 900 Elsewhere Ave.", ["Other building plans.pdf"]), ME)
        # 4: a teammate's mailbox, linked to the estimate: never read for me
        m4 = inbox.ingest(conn, ids["mate"], gmsg(4, "Bid invitation - parapets", "fyi", ["A-201 Parapet details.pdf"],
                                                  to=MATE), MATE)
        conn.execute("UPDATE mail_message SET estimate_id = ? WHERE id IN (?, ?)", (est["id"], m1, m4))
        conn.execute("UPDATE mail_message SET client_org_id = ? WHERE id IN (?, ?, ?)", (org, m1, m2, m3))
        conn.execute("UPDATE estimate SET status = 'approved', final_amount = 25500, qb_estimate_ref = 'QB-1' WHERE id = ?", (est["id"],))
        conn.commit()

        # ---- the job is created: one card
        res = estimates.convert_to_project(conn, est["id"], actor_user_id=uid)
        code = res["project_code"]
        acts = conn.execute("SELECT * FROM claude_action WHERE tool = 'file_to_job' AND status = 'pending' AND input LIKE ?",
                            (f'%{code}%',)).fetchall()
        assert len(acts) == 1 and acts[0]["user_id"] == uid, [dict(a) for a in acts]
        a = dict(acts[0])
        inp = json.loads(a["input"])
        assert all(f.get("mail_id") != m4 for f in inp["files"]) and all(f.get("mail_id") != m3 for f in inp["files"])
        ctx = ca.Ctx(conn, {"id": uid, "role": "admin"}, "office")
        c = jf.card(ctx, inp)
        names = [(g["dir"], [r["name"] for r in g["rows"]]) for g in c["groups"]]
        assert names[0] == ("", ["FISP report 2025.pdf"]), names
        assert ("Received", ["A-201 Parapet details.pdf"]) in names and ("Sent", ["Schedule draft.xlsx"]) in names, names
        assert c["count"] == 3 and len(c["fields"]) == 6 and c["approve_word"] == "File" and "2 email threads" in c["subtitle"]
        row = {r["name"]: r for g in c["groups"] for r in g["rows"]}
        assert row["A-201 Parapet details.pdf"]["folder"] == "DRAWINGS" and row["Schedule draft.xlsx"]["folder"] == "CONTRACTS"
        me = app.test_client()
        assert me.post("/api/auth/login", json={"email": ME, "password": PW}, environ_base={"REMOTE_ADDR": "10.99.0.1"}).status_code == 200
        d = me.get(f"/api/claude/job-filing/{code}").get_json()["data"]
        assert d and d["action_id"] == a["id"] and d["count"] == 3, d
        needs = me.get("/api/company/portal/today").get_json()["data"]["needs"]
        nc = next(x for x in needs if x.get("action_id") == a["id"])
        assert nc["href"] == f"/projects/{code}#documents" and "A-201 Parapet details → Drawings & Specs" in nc["draft"]["text"], nc
        assert jf.offer_on_new_job(conn, est["id"], code, uid) is None, "never a second card for the same job"
        passed += 1; print("  new job card ok")

        # ---- approve: schedule unticked, A-201 renamed and moved
        k = {r["name"]: r["key"] for g in c["groups"] for r in g["rows"]}
        vals = {f["key"]: f["value"] for f in c["fields"]}
        vals[k["Schedule draft.xlsx"] + ".title"] = ""
        vals[k["Schedule draft.xlsx"] + ".folder"] = ""
        vals[k["A-201 Parapet details.pdf"] + ".title"] = "A-201 Parapet details (bid set)"
        vals[k["A-201 Parapet details.pdf"] + ".folder"] = "INSPECTIONS"
        r = me.post(f"/api/claude/actions/{a['id']}/approve", json={"values": vals})
        assert r.status_code == 200, r.get_json()
        result = r.get_json()["data"]["result"]
        assert result["summary"] == f"2 documents filed to {code}" and result["undo"], result
        docs = {x["title"]: dict(x) for x in conn.execute("SELECT * FROM project_documents WHERE project_code = ? AND source_key IS NOT NULL", (code,)).fetchall()}
        assert set(docs) == {"FISP report 2025", "A-201 Parapet details (bid set)"}, list(docs)
        assert docs["A-201 Parapet details (bid set)"]["category"] == "INSPECTIONS"
        assert docs["A-201 Parapet details (bid set)"]["source_key"].startswith("mail:g-jfile-1:")
        assert Path(docs["FISP report 2025"]["file_path"]).read_bytes() == b"bytes of the FISP report"
        assert conn.execute("SELECT 1 FROM mail_link WHERE message_id = ? AND entity_type = 'project' AND entity_id = ?", (m1, code)).fetchone()
        assert me.get(f"/api/claude/job-filing/{code}").get_json()["data"] is None
        passed += 1; print("  approve ok")

        # ---- on an email for the job: a new revision, an identical file, another person's email
        jdir = ssc_paths.under_root("data_room", "project_docs", code)
        (jdir / "revb.pdf").write_bytes(b"rev b")
        (jdir / "coi.pdf").write_bytes(b"bytes of COI copy.pdf")
        for title, fn, path in (("Shop drawings Rev B", "41JF shop drawings rev B.pdf", "revb.pdf"), ("Our COI", "SSC COI.pdf", "coi.pdf")):
            conn.execute("INSERT INTO project_documents (project_code, category, title, doc_type, file_path, file_name, file_size, mime, "
                         "superseded, uploaded_at) VALUES (?, 'DRAWINGS', ?, 'PDF', ?, ?, 5, 'application/pdf', 0, ?)",
                         (code, title, str(jdir / path), fn, datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        revb = conn.execute("SELECT id FROM project_documents WHERE project_code = ? AND title = 'Shop drawings Rev B'", (code,)).fetchone()["id"]
        m5 = inbox.ingest(conn, uid, gmsg(5, "Shop drawings", "Rev C attached with our COI.",
                                          ["41JF Shop Drawings Rev C.pdf", "COI copy.pdf"], out=True), ME)
        inp5 = {"project_code": code, "files": [{"mail_id": m5, "name": "41JF Shop Drawings Rev C.pdf", "folder": "DRAWINGS", "title": "Shop drawings Rev C"},
                                                 {"mail_id": m5, "name": "COI copy.pdf", "folder": "CONTRACTS", "title": "COI"}]}
        ca.BY_NAME["file_to_job"].run(ctx, inp5)
        c5 = jf.card(ctx, inp5)
        r5 = {r["name"]: r for g in c5["groups"] for r in g["rows"]}
        assert "New version of “Shop drawings Rev B”" in r5["41JF Shop Drawings Rev C.pdf"]["note"], r5
        try:
            ca.BY_NAME["file_to_job"].run(ca.Ctx(conn, {"id": ids["mate"], "role": "admin"}, "office"), inp5)
            raise AssertionError("another person's email was accepted")
        except LookupError:
            pass
        res5 = jf.execute(ctx, inp5)
        assert res5["summary"] == f"1 document filed to {code} · 1 already there", res5
        assert conn.execute("SELECT superseded FROM project_documents WHERE id = ?", (revb,)).fetchone()["superseded"] == 1
        c5b = jf.card(ctx, inp5)
        assert c5b["count"] == 0 and all(r["dupe"] for g in c5b["groups"] for r in g["rows"] if r["name"].startswith("41JF"))
        passed += 1; print("  existing job ok")

        # ---- undo; a card whose files are all on the job closes itself
        fake_action = {"id": 0}
        jf.undo(ctx, fake_action, res5)
        assert conn.execute("SELECT superseded FROM project_documents WHERE id = ?", (revb,)).fetchone()["superseded"] == 0
        assert not conn.execute("SELECT 1 FROM project_documents WHERE project_code = ? AND title = 'Shop drawings Rev C'", (code,)).fetchone()
        chat = inbox._mail_chat(conn, inbox.message(conn, m1))
        inp6 = {"project_code": code, "files": [{"mail_id": m1, "name": "A-201 Parapet details.pdf"}]}
        conn.execute("INSERT INTO claude_action (chat_id, message_id, tool_use_id, user_id, tool, input, summary, status, created_at) "
                     "VALUES (?, 0, 'tu-jfile-6', ?, 'file_to_job', ?, 'File', 'pending', ?)",
                     (chat, uid, json.dumps(inp6), datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        a6 = conn.execute("SELECT MAX(id) AS m FROM claude_action WHERE user_id = ?", (uid,)).fetchone()["m"]
        ca.sweep_moot(conn)
        row6 = conn.execute("SELECT status, result FROM claude_action WHERE id = ?", (a6,)).fetchone()
        assert row6["status"] == "declined" and f"Every file is already on {code}" in row6["result"], dict(row6)
        passed += 1; print("  undo / already filed ok")

        # ---- the pages and Claude's instructions
        js = (ROOT / "static/js/claude_drawer.js").read_text(encoding="utf-8")
        for needle in ("r.kind === 'file'", "'.folder\" aria-label=\"Folder for '", "row.querySelectorAll('[data-key]')", "function approveLabel(c, n)"):
            assert needle in js, needle
        page = (ROOT / "dashboard-static.html").read_text(encoding="utf-8")
        assert "async function pdClaude()" in page and "'#documents': 'documents'" in page and 'id="pd-claude"' in page
        v = re.search(r"claude_drawer\.js\?v=(\d+)", (ROOT / "claude.html").read_text(encoding="utf-8")).group(1)
        assert f"claude_drawer.js?v={v}\"" in page
        prompt = inbox.suggest_prompt(conn, inbox.message(conn, m5), "Jfile Me")
        assert "ONE file_to_job" in prompt
        passed += 1; print("  pages ok")
    finally:
        inbox.attachment_bytes = real_bytes
        conn.close()
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: Claude files emailed documents to the job ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
