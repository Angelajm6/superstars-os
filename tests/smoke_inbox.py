#!/usr/bin/env python3
"""Inbox (accepted 2026-10-14): the BEHAVIORAL guard for inbox.py and its Claude cards.

Proves, through a Flask test client against an ISOLATED database (no Google, no model call):
  * gating: anonymous 401; pm 403; admin / c_suite 200; Google's push route is public but only
    answers with the right token
  * sync: the first run reads the last days, later runs read Gmail's change history; a history
    that expired starts over; nothing is stored twice
  * storing: promotions, newsletters and no-reply senders are never stored; sent mail is "out";
    subject, sender and text are encrypted at rest
  * matching: a contact's email or company domain -> the client; an estimate / proposal number
    in the subject -> that record; a job code in the text -> that job
  * sorting + suggesting: business email that needs action gets Claude's cards (a hidden
    conversation, never in History); nothing is created or sent before Approve; the sorting
    counts toward the daily limit, and over the limit suggestions pause
  * approving: a bid from an invitation saves the email's PDFs to the new bid and files the
    email on the bid and the client; a reply goes out in the same thread (In-Reply-To) to the
    sender; a proposal emailed from Gmail is logged as sent; a read-only connection is told to
    reconnect before anything is sent
  * the record pages list filed emails; another person never sees someone else's mail
  * the Gmail-like view: the email as written (html, scripts stripped, inline pictures), one row per
    conversation, attachments, Reply / Reply all, Claude's reply draft sent only by the person's Send,
    Compose, and Refresh
  * logs carry ids / counts only; the pages carry the Inbox and no "Check now"

Database: honors SSC_DB_URL like every gate suite; otherwise a throwaway SQLite snapshot.
Run:  venv/bin/python tests/smoke_inbox.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import base64
import json
import logging
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_inbox_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

MAIL = Path(_TMP) / "mail"
os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(MAIL), "SSC_GMAIL_POLL": "0",
                   "GOOGLE_OAUTH_CLIENT_ID": "test-client.apps.googleusercontent.com", "GOOGLE_OAUTH_CLIENT_SECRET": "test-secret",
                   "GOOGLE_OAUTH_REDIRECT_URI": "https://app.example.test/auth/google/callback",
                   "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(), "GMAIL_PUSH_TOKEN": "push-token-for-tests",
                   "SSC_INBOX_ATTACHMENT_FAKE": "%PDF-1.4 smoke"})
for _k in ("ANTHROPIC_API_KEY", "SSC_ASSISTANT_FAKE", "SSC_INBOX_TRIAGE_FAKE", "SSC_MAIL_FAKE_SENDAS", "SSC_ASSISTANT_DAILY_CAP_USD", "SSC_ASSISTANT_MONTHLY_BUDGET_USD", "SSC_ASSISTANT_USER_DAILY_USD", "SSC_CLAUDE_MODEL", "SSC_CLAUDE_EFFORT", "GMAIL_PUBSUB_TOPIC"):
    os.environ.pop(_k, None)

from flask import Flask  # noqa: E402

import assistant  # noqa: E402
import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import gmail_suggest  # noqa: E402
import inbox  # noqa: E402
import proposals  # noqa: E402
import team_items  # noqa: E402

TAG = "ibx"
PW = "InboxSmokePassword2026"
ORG = "Ibx Smoke Tishman GC"
CONTACT = "dana.ibx@tishman-ibx.example"
SECRET_SUBJECT = "ITB Ibx Smoke 1 Hudson Sq facade"
JOB = "IBX-1"


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


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def gmsg(gid, *, frm, to, subject, text, labels=("INBOX",), headers=(), atts=(), when="1792000000000"):
    parts = [{"mimeType": "text/plain", "body": {"data": _b64(text)}}]
    parts += [{"mimeType": "application/pdf" if n.endswith(".pdf") else "application/zip", "filename": n,
               "body": {"attachmentId": "att-" + n, "size": 2048}} for n in atts]
    hs = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
          {"name": "Message-ID", "value": f"<{gid}@mail.example>"}] + [{"name": k, "value": v} for k, v in headers]
    return {"id": gid, "threadId": "thr-" + gid, "labelIds": list(labels), "internalDate": when, "snippet": text[:80],
            "payload": {"mimeType": "multipart/mixed", "headers": hs, "parts": parts}}


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ca.ensure(conn)
        inbox.ensure(conn)
        team_items.ensure(conn)
        proposals.ensure_schema(conn)
        gmail_suggest.ensure(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        mids = [r["id"] for r in conn.execute(f"SELECT id FROM mail_message WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "mail_link", "message_id", mids)
        purge(conn, "mail_message", "id", mids)
        for t in ("mail_sync", "mail_sendas", "gmail_link", "gmail_seen", "gmail_suggestion"):
            purge(conn, t, "user_id", uids)
        chats = [r["id"] for r in conn.execute(f"SELECT id FROM claude_chat WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "claude_action", "chat_id", chats)
        purge(conn, "claude_chat_message", "chat_id", chats)
        purge(conn, "claude_chat", "id", chats)
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name = ?", (ORG,)).fetchall()]
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph(ests)})", ests).fetchall()]
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate_document", "estimate_id", ests)
        purge(conn, "estimate", "id", ests)
        conn.execute("DELETE FROM building WHERE address_key LIKE '% ibx smoke %' OR address_key LIKE '%hudson sq ibx%'")
        conn.execute("DELETE FROM crm_task WHERE title LIKE 'Ibx smoke%'")
        conn.execute("DELETE FROM projects WHERE project_code = ?", (JOB,))
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM crm_organization WHERE name = ?", (ORG,))
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()


def _seed() -> dict:
    _cleanup()
    conn = _db()
    ids = {}
    try:
        for role in ("admin", "c_suite", "pm"):
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, "
                         "is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 0)",
                         (f"{TAG}.{role}@superstarscontracting.com", auth.hash_password(PW), role, f"Ibx {role.title()}"))
        conn.commit()
        ids.update({r["role"]: r["id"] for r in conn.execute("SELECT id, role FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()})
        ids["org"] = crm.create_org(conn, name=ORG, relationship_type="client")
        ids["contact"] = crm.create_contact(conn, full_name="Dana Ibx", org_id=ids["org"], email=CONTACT)
        e = estimates.create_estimate(conn, est_type="FR", borough="MN", client_org_id=ids["org"], contact_id=ids["contact"],
                                      building_address="9 Ibx Smoke Place", created_by=ids["admin"])
        conn.execute("UPDATE estimate SET final_amount = 61000 WHERE id = ?", (e["id"],))
        conn.commit()
        p = proposals.create_from_estimate(conn, e["id"], created_by=ids["admin"])
        ids.update(est=e["id"], code=e["code"], prop=p["id"], number=p["number"])
        conn.execute("INSERT INTO projects (project_code, name, address) VALUES (?, ?, ?)", (JOB, "Ibx smoke job", "9 Ibx Smoke Place"))
        conn.execute("INSERT INTO gmail_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) "
                     "VALUES (?, ?, ?, 'active', ?, ?)", (ids["admin"], f"{TAG}.admin@superstarscontracting.com",
                                                          secret_box.encrypt_text("refresh-x"), "2026-10-14T09:00:00",
                                                          "openid email https://www.googleapis.com/auth/gmail.readonly"))
        conn.commit()
    finally:
        conn.close()
    return ids


def main() -> int:
    app = Flask("ibx", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    ca.register(app)
    inbox.register(app)
    ca.RUN_INLINE = True
    trap = LogTrap()
    for name in ("ssc.inbox", "ssc.claude_agent"):
        lg = logging.getLogger(name)
        lg.addHandler(trap)
        lg.setLevel(logging.INFO)

    def login(role):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@superstarscontracting.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.94.0.{len(role)}"})
        assert r.status_code == 200, (role, r.status_code)
        return c

    ids = _seed()
    me = ids["admin"]
    passed = 0
    try:
        # ---- gating + push
        anon = app.test_client()
        assert anon.get("/api/inbox/status").status_code == 401
        a, cs, pm = login("admin"), login("c_suite"), login("pm")
        assert pm.get("/api/inbox/status").status_code == 403 and pm.get("/api/inbox/messages").status_code == 403
        st = a.get("/api/inbox/status").get_json()["data"]
        assert st["connected"] is True and st["can_send"] is False and st["live"] is False and st["every_seconds"] == 60
        assert anon.post("/gmail/push", json={}).status_code == 404
        assert anon.post("/gmail/push?token=wrong", json={}).status_code == 404
        data = base64.b64encode(json.dumps({"emailAddress": "x@y.z", "historyId": "5"}).encode()).decode()
        assert anon.post("/gmail/push?token=push-token-for-tests", json={"message": {"data": data}}).status_code == 204
        assert "/gmail/push" in auth._PUBLIC_EXACT
        passed += 1; print("  gating + push ok")

        # ---- sync through a fake Gmail
        msgs = {
            "m1": gmsg("m1", frm=f"Dana Ibx <{CONTACT}>", to=f"{TAG}.admin@superstarscontracting.com", subject=SECRET_SUBJECT,
                       text="Please bid 1 Hudson Sq. Bids due Oct 24. Walkthrough Tue 10 AM.", atts=("plans.pdf", "photos.zip")),
            "m2": gmsg("m2", frm="Deals <deals@shop.example>", to="x", subject="Sale", text="50% off", labels=("INBOX", "CATEGORY_PROMOTIONS")),
            "m3": gmsg("m3", frm="Robot <no-reply@vendor.example>", to="x", subject="Your receipt", text="Thanks"),
            "m4": gmsg("m4", frm="News <news@list.example>", to="x", subject="Weekly", text="hi", headers=(("List-Unsubscribe", "<mailto:x>"),)),
            "m5": gmsg("m5", frm=f"{TAG}.admin@superstarscontracting.com", to=f"Dana Ibx <{CONTACT}>",
                       subject=f"Proposal {ids['number']}", text=f"Attached is proposal {ids['number']} for job {JOB}: $64,500.",
                       labels=("SENT",), atts=("Proposal.pdf",)),
            "m6": gmsg("m6", frm="Pat Other <pat@tishman-ibx.example>", to="x", subject="Site access", text="Lobby closed Friday"),
        }
        calls = []

        def fake_gmail(conn, link, method, path, **kw):
            calls.append(path)
            if path == "/profile":
                return 200, {"historyId": "100"}
            if path.startswith("/messages?q="):
                return 200, {"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}, {"id": "m4"}]}
            if path.startswith("/history?startHistoryId=100"):
                return 200, {"historyId": "120", "history": [{"messagesAdded": [{"message": {"id": "m5", "labelIds": ["SENT"]}},
                                                                                {"message": {"id": "m6", "labelIds": ["INBOX"]}},
                                                                                {"message": {"id": "m7", "labelIds": ["DRAFT"]}}]}]}
            if path.startswith("/history?startHistoryId=120"):
                return 404, {}
            if path.startswith("/messages/") and "format=full" in path:
                gid = path.split("/")[2].split("?")[0]
                return (200, msgs[gid]) if gid in msgs else (404, {})
            return 500, {}
        real = inbox._gmail
        inbox._gmail = fake_gmail
        try:
            conn = _db()
            try:
                assert inbox.sync_user(conn, me) == {"new": 1}          # m1 kept; promo, no-reply, newsletter skipped
                assert inbox.sync_user(conn, me) == {"new": 2}          # history: m5 (sent) + m6; the draft ignored
                assert _q("SELECT history_id FROM mail_sync WHERE user_id = ?", (me,))[0]["history_id"] == "120"
                assert inbox.sync_user(conn, me) == {"new": 0}          # history expired: starts over, nothing twice
                assert inbox.sync_user(conn, ids["pm"]) == {"skipped": True}
            finally:
                conn.close()
        finally:
            inbox._gmail = real
        rows = _q("SELECT * FROM mail_message WHERE user_id = ? ORDER BY id", (me,))
        assert [r["gmail_id"] for r in rows] == ["m1", "m5", "m6"]
        r1, r5, r6 = rows
        assert r1["direction"] == "in" and r5["direction"] == "out"
        assert SECRET_SUBJECT not in json.dumps(r1) and CONTACT not in json.dumps(r1) and "Hudson" not in json.dumps(r1)
        assert r1["client_org_id"] == ids["org"] and r1["contact_id"] == ids["contact"]
        assert r5["proposal_id"] == ids["prop"] and r5["estimate_id"] == ids["est"] and r5["project_code"] == JOB
        assert r6["client_org_id"] == ids["org"] and r6["contact_id"] is None          # same company domain
        passed += 1; print("  sync, storing, matching ok")

        # ---- sorting + suggestions (planted replies)
        os.environ["SSC_INBOX_TRIAGE_FAKE"] = json.dumps({"business": True, "kind": "bid_invitation",
                                                          "summary": "Tishman invites a bid on 1 Hudson Sq", "needs_action": True})
        os.environ["SSC_ASSISTANT_FAKE"] = json.dumps([
            {"tools": [{"name": "create_estimate", "input": {"kind": "bid", "est_type": "FR", "borough": "MN", "client_org_id": ids["org"],
                                                             "building_address": "1 Hudson Sq Ibx", "bid_due_date": (date.today() + timedelta(days=9)).isoformat(),
                                                             "mail_id": r1["id"]}},
                       {"name": "reply_email", "input": {"mail_id": r1["id"], "body": "Hi Dana,\n\nWe will bid. See you Tuesday.\n\nIbx Admin"}}]},
            {"text": "An invitation to bid; a new bid and a reply are ready for you."}])
        ca._FAKE["step"] = 0
        conn = _db()
        try:
            assert inbox.process(conn, r1["id"]) == "suggested"
        finally:
            conn.close()
        m = a.get(f"/api/inbox/messages/{r1['id']}").get_json()["data"]
        assert m["chat_id"] and "New bid" in m["tags"] and m["needs_action"] is True and m["pending_cards"] == 2
        assert all(c["id"] != m["chat_id"] for c in a.get("/api/claude/chats?surface=office").get_json()["data"])   # not in History
        view = a.get(f"/api/claude/chats/{m['chat_id']}").get_json()["data"]
        cards = {x["tool"]: x for msg in view["messages"] for x in msg.get("actions", [])}
        assert set(cards) == {"create_estimate", "reply_email"}
        docs = next(f for f in cards["create_estimate"]["card"]["fields"] if f["key"] == "mail_id")
        assert "plans.pdf" in docs["hint"] and "photos.zip" not in docs["hint"]
        to = next(f for f in cards["reply_email"]["card"]["fields"] if f["key"] == "mail_id")
        assert CONTACT in to["hint"]
        assert not _q("SELECT id FROM estimate WHERE building_address = '1 Hudson Sq Ibx'") and not list(MAIL.glob("gmail-reply-*.eml"))
        assert _q("SELECT COUNT(*) AS n FROM claude_chat_message WHERE chat_id = ? AND kind = 'triage'", (m["chat_id"],))[0]["n"] == 1
        lst = a.get("/api/inbox/messages?filter=act").get_json()["data"]
        assert [x["id"] for x in lst][0] == r1["id"] or r1["id"] in [x["id"] for x in lst]
        passed += 1; print("  sorting + suggestions ok (nothing done before Approve)")

        # ---- approve: the bid (documents + filing) and the reply (same thread)
        r = a.post(f"/api/claude/actions/{cards['create_estimate']['id']}/approve", json={})
        assert r.status_code == 200, r.get_json()
        assert "1 document saved" in r.get_json()["data"]["result"]["summary"]
        est = _q("SELECT id FROM estimate WHERE building_address = '1 Hudson Sq Ibx'")[0]["id"]
        assert [d["file_name"] for d in _q("SELECT file_name FROM estimate_document WHERE estimate_id = ?", (est,))] == ["plans.pdf"]
        links = {(x["entity_type"], x["entity_id"]) for x in _q("SELECT entity_type, entity_id FROM mail_link WHERE message_id = ?", (r1["id"],))}
        assert ("estimate", str(est)) in links and ("client", str(ids["org"])) in links
        r = a.post(f"/api/claude/actions/{cards['reply_email']['id']}/approve", json={"values": {"body": "Hi Dana,\n\nWe will bid. See you Tuesday at 10.\n\nIbx Admin"}})
        assert r.status_code == 200, r.get_json()
        eml = sorted(MAIL.glob("gmail-reply-*.eml"))[-1].read_text(encoding="utf-8")
        assert f"To: Dana Ibx <{CONTACT}>" in eml and "In-Reply-To: <m1@mail.example>" in eml and "Subject: Re: " + SECRET_SUBJECT in eml
        assert "See you Tuesday at 10." in eml
        linked = a.get(f"/api/inbox/linked?type=estimate&id={est}").get_json()["data"]
        assert [x["id"] for x in linked] == [r1["id"]] and SECRET_SUBJECT == linked[0]["subject"]
        assert [x["id"] for x in a.get(f"/api/inbox/linked?type=client&id={ids['org']}").get_json()["data"]] == [r1["id"]]
        passed += 1; print("  approve: bid + documents + filing + threaded reply ok")

        # ---- a proposal emailed from Gmail
        os.environ["SSC_INBOX_TRIAGE_FAKE"] = json.dumps({"business": True, "kind": "proposal_reply", "summary": "You sent the proposal",
                                                          "needs_action": False})
        os.environ["SSC_ASSISTANT_FAKE"] = json.dumps([
            {"tools": [{"name": "log_proposal_sent", "input": {"proposal_id": ids["prop"], "mail_id": r5["id"], "price": 64500}}]},
            {"text": "You sent a proposal from Gmail."}])
        ca._FAKE["step"] = 0
        conn = _db()
        try:
            assert inbox.process(conn, r5["id"]) == "suggested"
        finally:
            conn.close()
        m5 = a.get(f"/api/inbox/messages/{r5['id']}").get_json()["data"]
        card = [x for msg in a.get(f"/api/claude/chats/{m5['chat_id']}").get_json()["data"]["messages"] for x in msg.get("actions", [])][0]
        assert proposals.get(_db(), ids["prop"])["status"] == "draft"
        assert a.post(f"/api/claude/actions/{card['id']}/approve", json={}).status_code == 200
        p = proposals.get(_db(), ids["prop"])
        assert p["status"] == "sent" and float(p["price"]) == 64500.0 and p["sent_at"] == r5["received_at"]
        assert ("proposal", str(ids["prop"])) in {(x["entity_type"], x["entity_id"]) for x in _q("SELECT entity_type, entity_id FROM mail_link WHERE message_id = ?", (r5["id"],))}
        passed += 1; print("  proposal sent from Gmail logged ok")

        # ---- the Gmail-like view: the email as written, threads, Reply all, Claude's draft in the box, Compose, Refresh
        html = ('<div style="color:#333">Hi team,<br>Scaffold <b>drawings</b> attached.<script>alert(1)</script>'
                '<a href="javascript:alert(2)" onclick="steal()">x</a><img src="cid:logo123"><iframe src="https://x.example"></iframe></div>')
        m8 = {"id": "m8", "threadId": "thr-site", "labelIds": ["INBOX"], "internalDate": "1792000100000", "snippet": "Hi team, Scaffold drawings &lt;PDF&gt;",
              "payload": {"mimeType": "multipart/mixed", "headers": [
                  {"name": "From", "value": f"Dana Ibx <{CONTACT}>"}, {"name": "To", "value": f"{TAG}.admin@superstarscontracting.com"},
                  {"name": "Cc", "value": "Lee Ibx <lee@tishman-ibx.example>"}, {"name": "Subject", "value": "Ibx scaffold drawings"},
                  {"name": "Message-ID", "value": "<m8@mail.example>"}],
                  "parts": [{"mimeType": "multipart/alternative", "parts": [
                      {"mimeType": "text/plain", "body": {"data": _b64("Hi team, Scaffold drawings attached.")}},
                      {"mimeType": "text/html", "body": {"data": _b64(html)}}]},
                      {"mimeType": "image/png", "filename": "", "headers": [{"name": "Content-ID", "value": "<logo123>"},
                                                                            {"name": "Content-Disposition", "value": "inline"}],
                       "body": {"attachmentId": "att-logo", "size": 900}},
                      {"mimeType": "application/pdf", "filename": "scaffold.pdf", "body": {"attachmentId": "att-scf", "size": 4096}}]}}
        m9 = gmsg("m9", frm="Lee Ibx <lee@tishman-ibx.example>", to=f"{TAG}.admin@superstarscontracting.com, Dana Ibx <{CONTACT}>",
                  subject="Re: Ibx scaffold drawings", text="Can you confirm the start date?", when="1792000200000")
        m9["threadId"] = "thr-site"
        conn = _db()
        try:
            i8 = inbox.ingest(conn, me, m8, f"{TAG}.admin@superstarscontracting.com")
            i9 = inbox.ingest(conn, me, m9, f"{TAG}.admin@superstarscontracting.com")
            full8 = inbox.message(conn, i8, me)
            assert [a["name"] for a in full8["attachments"]] == ["scaffold.pdf"] and len(full8["inline_images"]) == 1
            conn.execute("UPDATE mail_message SET ai_state = 'triaged' WHERE id IN (?, ?)", (i8, i9))
            conn.commit()
        finally:
            conn.close()
        assert "drawings" not in json.dumps(_q("SELECT * FROM mail_message WHERE id = ?", (i8,)))       # html encrypted at rest
        rows_all = a.get("/api/inbox/messages?filter=all").get_json()["data"]
        site = [x for x in rows_all if x["id"] in (i8, i9)]
        assert len(site) == 1 and site[0]["id"] == i9 and site[0]["count"] == 2 and site[0]["unread"] is True   # one row per conversation
        assert "summary" not in site[0] or site[0]["snippet"]
        assert pm.get(f"/api/inbox/threads/{i9}").status_code == 403 and cs.get(f"/api/inbox/threads/{i9}").status_code == 404
        t = a.get(f"/api/inbox/threads/{i9}").get_json()["data"]
        assert [x["id"] for x in t["messages"]] == [i8, i9] and t["last_id"] == i9 and t["draft"] is None
        assert t["messages"][0]["snippet"].endswith("<PDF>")                                      # Gmail's escaped snippet, shown as text
        doc = t["messages"][0]["doc"]
        assert "<b>drawings</b>" in doc and "Content-Security-Policy" in doc and '<base target="_blank">' in doc
        assert "<script" not in doc and "alert(1)" not in doc and "onclick" not in doc and "javascript:" not in doc and "<iframe" not in doc
        assert "cid:logo123" not in doc and f"/api/inbox/messages/{i8}/attachments/0" in doc              # picture inside the email: loads as shown
        assert "img-src 'self'" in doc and a.get(f"/api/inbox/messages/{i8}/attachments/0").status_code == 200
        assert [x["name"] for x in t["messages"][0]["attachments"]] == ["scaffold.pdf"]
        n = t["messages"][0]["attachments"][0]["n"]
        r = a.get(f"/api/inbox/messages/{i8}/attachments/{n}")
        assert r.status_code == 200 and r.data.startswith(b"%PDF") and r.headers["Content-Disposition"].startswith("inline")
        assert "sandbox" in r.headers["Content-Security-Policy"]
        assert a.get(f"/api/inbox/messages/{i8}/attachments/{n}?download=1").headers["Content-Disposition"].startswith("attachment")
        assert cs.get(f"/api/inbox/messages/{i8}/attachments/{n}").status_code == 404
        assert t["reply_to"]["to"].endswith("lee@tishman-ibx.example>") and t["reply_to"]["cc"] == ""
        assert CONTACT in t["reply_all_to"]["cc"] and f"{TAG}.admin" not in t["reply_all_to"]["cc"]
        assert _q("SELECT status FROM mail_message WHERE id = ?", (i9,))[0]["status"] == "seen"          # opening marks it read
        # Draft with Claude: even email the sorter calls "nothing to do" gets a reply when the person asks
        os.environ["SSC_INBOX_TRIAGE_FAKE"] = json.dumps({"business": False, "kind": "other", "summary": "x", "needs_action": False})
        os.environ["SSC_ASSISTANT_FAKE"] = json.dumps([
            {"tools": [{"name": "reply_email", "input": {"mail_id": i9, "reply_all": True, "body": "Hi Lee,\n\nWe start Monday.\n\nIbx Admin"}}]},
            {"text": "A reply is ready."}])
        ca._FAKE["step"] = 0
        conn = _db()
        try:
            assert inbox.process(conn, i9, force=True) == "triaged"                     # not asked: nothing drafted
            assert inbox.process(conn, i9, force=True, want_reply=True) == "suggested"
        finally:
            conn.close()
        assert not list(MAIL.glob("gmail-reply-*.eml"))[1:]                             # still only the earlier approved reply
        before = len(list(MAIL.glob("gmail-reply-*.eml")))
        # the person's Gmail name + signature go on everything sent from the software, as Gmail adds them
        os.environ["SSC_MAIL_FAKE_SENDAS"] = json.dumps({"displayName": "Ibx Admin Formal",
                                                         "signature": '<div><b style="color:#c00">Ibx Admin</b><br>Ops Manager<br>1 Ibx Way</div>'})
        conn = _db()
        try:
            conn.execute("DELETE FROM mail_sendas WHERE user_id = ?", (me,))
            conn.commit()
        finally:
            conn.close()
        t = a.get(f"/api/inbox/threads/{i9}").get_json()["data"]
        assert t["sender"]["from"] == f"Ibx Admin Formal <{TAG}.admin@superstarscontracting.com>"
        assert "Ops Manager" in t["sender"]["signature_doc"] and "Content-Security-Policy" in t["sender"]["signature_doc"]
        assert a.get("/api/inbox/status").get_json()["data"]["sender"]["from"].startswith("Ibx Admin Formal <")
        conn = _db()
        try:
            ask = inbox.suggest_prompt(conn, inbox.message(conn, i9, me), "Ibx Admin")
            assert "signature with name, title, company and address is added under it automatically" in ask and "signed as" not in ask
        finally:
            conn.close()
        assert t["draft"] and t["draft"]["reply_all"] is True and "We start Monday." in t["draft"]["body"]
        assert t["card_chats"] and all("reply_email" != x for x in t["card_chats"])
        r = a.post(f"/api/inbox/messages/{i9}/reply", json={"body": "Hi Lee,\n\nWe start Monday at 7.\n\nIbx Admin", "reply_all": True,
                                                             "draft_action_id": t["draft"]["action_id"]})
        assert r.status_code == 200, r.get_json()
        eml = sorted(MAIL.glob("gmail-reply-*.eml"))[-1].read_text(encoding="utf-8")
        assert len(list(MAIL.glob("gmail-reply-*.eml"))) == before + 1
        assert "To: Lee Ibx <lee@tishman-ibx.example>" in eml and f"Cc: Dana Ibx <{CONTACT}>" in eml and "In-Reply-To: <m9@mail.example>" in eml
        assert "We start Monday at 7." in eml and "Subject: Re: Ibx scaffold drawings" in eml
        import email as _email
        from email import policy as _policy
        sent = _email.message_from_string(eml, policy=_policy.default)
        assert sent["From"] == f"Ibx Admin Formal <{TAG}.admin@superstarscontracting.com>"                 # name, not the bare address
        hp, tp = sent.get_body(("html",)).get_content(), sent.get_body(("plain",)).get_content()
        assert "We start Monday at 7." in hp and 'class="gmail_signature"' in hp and "Ops Manager" in hp      # the signature, as html
        assert 'class="gmail_quote"' in hp and "wrote:" in hp and "Can you confirm the start date?" in hp     # the earlier email quoted
        assert hp.index("We start Monday") < hp.index("Ops Manager") < hp.index("gmail_quote")
        assert "Ops Manager" in tp and "> Can you confirm the start date?" in tp
        assert _q("SELECT status FROM claude_action WHERE id = ?", (t["draft"]["action_id"],))[0]["status"] == "approved"
        assert {x["status"] for x in _q("SELECT status FROM mail_message WHERE id IN (?, ?)", (i8, i9))} == {"done"}
        assert a.get(f"/api/inbox/threads/{i9}").get_json()["data"]["draft"] is None
        assert a.post(f"/api/inbox/messages/{i9}/reply", json={"body": "  "}).status_code == 400
        # Compose
        assert a.post("/api/inbox/compose", json={"to": "not-an-address", "subject": "x", "body": "y"}).status_code == 400
        assert a.post("/api/inbox/compose", json={"to": "", "subject": "x", "body": "y"}).status_code == 400
        assert pm.post("/api/inbox/compose", json={"to": "a@b.example", "body": "y"}).status_code == 403
        r = a.post("/api/inbox/compose", json={"to": "Pat <pat@tishman-ibx.example>", "cc": "lee@tishman-ibx.example",
                                               "subject": "Ibx site visit", "body": "See you Thursday."})
        assert r.status_code == 200, r.get_json()
        new = sorted(MAIL.glob("gmail-new-*.eml"))[-1].read_text(encoding="utf-8")
        assert "To: Pat <pat@tishman-ibx.example>" in new and "Cc: lee@tishman-ibx.example" in new and "See you Thursday." in new
        nm = _email.message_from_string(new, policy=_policy.default)
        assert nm["From"].startswith("Ibx Admin Formal <") and "Ops Manager" in nm.get_body(("html",)).get_content()
        assert "gmail_quote" not in nm.get_body(("html",)).get_content()
        # no Gmail signature read (test folder, nothing planted): the name falls back to the person's name in the software
        os.environ.pop("SSC_MAIL_FAKE_SENDAS")
        conn = _db()
        try:
            conn.execute("DELETE FROM mail_sendas WHERE user_id = ?", (me,))
            conn.commit()
            idn = inbox.identity(conn, me)
            assert idn["name"] == "Ibx Admin" and idn["signature"] == ""
        finally:
            conn.close()
        # Refresh: checks Gmail right now
        real = inbox._gmail
        inbox._gmail = lambda conn, link, method, path, **kw: ((200, {"historyId": "130"}) if path.startswith(("/history", "/profile"))
                                                               else (200, {"messages": []}))
        try:
            assert a.post("/api/inbox/refresh").get_json()["data"] == {"new": 0}
            assert pm.post("/api/inbox/refresh").status_code == 403
        finally:
            inbox._gmail = real
        passed += 1; print("  Gmail-like view: as written, threads, reply all, Claude draft, compose, refresh ok")

        # ---- a read-only connection must reconnect before sending; done / filters; privacy
        os.environ.pop("SSC_MAIL_FAKE_DIR")
        conn = _db()
        try:
            try:
                inbox.send_reply(conn, me, r6["id"], "hello")
                raise AssertionError("sent without the send permission")
            except ValueError as e:
                assert "Reconnect" in str(e)
        finally:
            conn.close()
            os.environ["SSC_MAIL_FAKE_DIR"] = str(MAIL)
        assert a.post(f"/api/inbox/messages/{r1['id']}/done", json={"done": True}).get_json()["data"]["needs_action"] is False
        assert r1["id"] not in [x["id"] for x in a.get("/api/inbox/messages?filter=act").get_json()["data"]]
        assert r1["id"] in [x["id"] for x in a.get("/api/inbox/messages?filter=filed").get_json()["data"]]
        assert cs.get(f"/api/inbox/messages/{r1['id']}").status_code == 404
        assert cs.get("/api/inbox/messages?filter=all").get_json()["data"] == []
        passed += 1; print("  reconnect-to-send, done, filters, privacy ok")

        # ---- the daily limit pauses suggestions; email still arrives
        os.environ["SSC_ASSISTANT_DAILY_CAP_USD"] = "0.000001"
        conn = _db()
        try:
            assert inbox.process(conn, r6["id"]) == "paused"
        finally:
            conn.close()
            os.environ.pop("SSC_ASSISTANT_DAILY_CAP_USD")
        passed += 1; print("  daily limit pauses suggestions ok")

        # ---- logs + pages
        logs = "\n".join(trap.lines)
        assert trap.lines and SECRET_SUBJECT not in logs and CONTACT not in logs and "Hudson" not in logs and "Dana" not in logs
        cp = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        assert 'id="view-inbox"' in cp and "inbox: loadInbox" in cp and "emailsCard($('est-emails'), 'estimate', e.id)" in cp
        assert "{ key: 'inbox', label: 'Inbox', href: '/company#inbox'" in (ROOT / "static/js/company_nav.js").read_text(encoding="utf-8")
        gm = (ROOT / "static/js/gmail_suggest.js").read_text(encoding="utf-8")
        assert "data-check>" not in gm and "Check now" not in gm and "/company#inbox" in gm
        assert "mountCards: mountCards" in (ROOT / "static/js/claude_drawer.js").read_text(encoding="utf-8")
        ib = cp[cp.index("// ---------- INBOX (inbox.py)"):cp.index("// a record's emails")]
        assert 'id="ib-refresh"' in cp and 'id="ib-new"' in cp and 'id="ib-compose"' in cp and "Draft with Claude" in ib
        assert 'data-mrep="one"' in ib and "bar.classList.add('btns')" in ib and ".ib-replybar.btns { position: sticky; bottom: 0;" in cp   # Reply always in reach
        assert "f.srcdoc = doc" in ib and "allow-scripts" not in ib and "skip: ['reply_email']" in ib and "r.summary" not in ib and "m.summary" not in ib
        passed += 1; print("  logs + pages ok")
    finally:
        for k in ("SSC_ASSISTANT_FAKE", "SSC_INBOX_TRIAGE_FAKE", "SSC_MAIL_FAKE_SENDAS"):
            os.environ.pop(k, None)
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"smoke_inbox: {passed}/9 groups passed")
    return 0 if passed == 9 else 1


if __name__ == "__main__":
    sys.exit(main())
