#!/usr/bin/env python3
"""Inbox synced both ways with Gmail + one Claude suggestion per conversation (2026-10-23).

Proves against an ISOLATED database with a fake Gmail (fictional people only):
  * a reply sent from Gmail itself closes the conversation: the earlier emails leave "Needs reply", Claude's reply draft
    is withdrawn (declined by nobody, with the reason), the dashboard card goes, and the sent email gets no draft
  * answered before Claude got to it: sorted only, not "needs action", no cards
  * one reply draft per conversation: a newer email's draft replaces the older one; Claude is told what is waiting;
    the dashboard shows one card of each kind per conversation
  * read / unread / archived / moved back to the Inbox in Gmail follows here (history), and an email read or archived
    in Gmail before it arrived arrives that way
  * here -> Gmail: opening marks read, Done archives (Undo moves back), Mark unread; without Gmail's modify permission
    only this side changes
  * the open Inbox asks /api/inbox/changes: the fingerprint moves when anything changes; sync=1 syncs at most every 15 s
  * an email that starts with <meta http-equiv> (Apple Mail, Outlook) opens with its text (it opened blank)
  * the page: no Refresh button, Email / My calendar / Team, Inbox / Needs reply / Filed, the live line, the actions
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import base64
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_sync_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(Path(_TMP) / "mail"), "SSC_GMAIL_POLL": "0",
                   "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key()})
for _k in ("ANTHROPIC_API_KEY", "SSC_ASSISTANT_FAKE", "SSC_INBOX_TRIAGE_FAKE", "SSC_INBOX_ATTACHMENT_FAKE", "SSC_ASSISTANT_DAILY_CAP_USD",
           "SSC_ASSISTANT_MONTHLY_BUDGET_USD", "SSC_ASSISTANT_USER_DAILY_USD", "GMAIL_PUBSUB_TOPIC"):
    os.environ.pop(_k, None)

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import db_layer  # noqa: E402
import inbox  # noqa: E402

TAG = "isyn"
PW = "SyncSmokePassword2026"
ME = f"{TAG}.admin@superstarscontracting.com"
DANA = "Dana Isynsmoke <dana@isyn-harbor.example>"
NOW = time.time()
FULL = "openid email " + inbox.SEND_SCOPE + " " + inbox.MODIFY_SCOPE


def _db():
    return db_layer.connect(pragma_fk=True)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


GMAIL: dict = {}   # the fake mailbox: gmail id -> message (labels change as Gmail would)


def gmsg(gid, thread, minutes_ago, text, labels=("INBOX", "UNREAD"), frm=DANA, to=ME):
    hs = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": "Isyn smoke " + thread},
          {"name": "Message-ID", "value": f"<{gid}@isyn.example>"}]
    m = {"id": gid, "threadId": thread, "labelIds": list(labels), "internalDate": str(int((NOW - minutes_ago * 60) * 1000)),
         "snippet": text[:80], "payload": {"mimeType": "multipart/alternative", "headers": hs, "parts": [
             {"mimeType": "text/plain", "body": {"data": _b64(text)}}, {"mimeType": "text/html", "body": {"data": _b64(f"<p>{text}</p>")}}]}}
    GMAIL[gid] = m
    return m


def sent(gid, thread, minutes_ago, text):
    return gmsg(gid, thread, minutes_ago, text, labels=("SENT",), frm=ME, to=DANA)


CALLS: list = []
HISTORY = {"body": {}}


def fake_gmail(conn, link, method, path, **kw):
    CALLS.append((method, path, kw.get("json_body")))
    if method == "POST" and path == "/messages/batchModify":
        b = kw["json_body"]
        for g in b["ids"]:
            if g in GMAIL:
                GMAIL[g]["labelIds"] = [x for x in GMAIL[g]["labelIds"] if x not in b["removeLabelIds"]] + list(b["addLabelIds"])
        return 204, {}
    if path.startswith("/history?"):
        return 200, HISTORY["body"]
    if path.startswith("/threads/"):
        t = path.split("/")[2].split("?")[0]
        msgs = [{"id": m["id"], "threadId": t, "labelIds": list(m["labelIds"])} for m in GMAIL.values() if m["threadId"] == t]
        return (200, {"id": t, "messages": msgs}) if msgs else (404, {})
    if path.startswith("/messages/") and "format=full" in path:
        g = path.split("/")[2].split("?")[0]
        return (200, json.loads(json.dumps(GMAIL[g]))) if g in GMAIL else (404, {})
    return 500, {}


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ca.ensure(conn)
        inbox.ensure(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = ",".join("?" * len(uids)) or "NULL"
        mids = [r["id"] for r in conn.execute(f"SELECT id FROM mail_message WHERE user_id IN ({ph})", uids).fetchall()]
        chats = [r["id"] for r in conn.execute(f"SELECT id FROM claude_chat WHERE user_id IN ({ph})", uids).fetchall()]
        purge(conn, "claude_action", "chat_id", chats)
        purge(conn, "claude_chat_message", "chat_id", chats)
        purge(conn, "claude_chat", "id", chats)
        purge(conn, "mail_link", "message_id", mids)
        purge(conn, "mail_message", "id", mids)
        for t in ("gmail_link", "mail_sendas", "mail_sync"):
            purge(conn, t, "user_id", uids)
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()


def _q(sql, params=()):
    conn = _db()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _mid(gid):
    return _q("SELECT id FROM mail_message WHERE gmail_id = ?", (gid,))[0]["id"]


def _row(gid):
    return _q("SELECT * FROM mail_message WHERE gmail_id = ?", (gid,))[0]


def _card(conn, uid, mid, tool="reply_email", body="Hi Dana,\n\nYes.\n\nIsyn"):
    m = inbox.message(conn, mid)
    chat = inbox._mail_chat(conn, m)
    inp = {"mail_id": mid, "body": body} if tool == "reply_email" else {"title": "Call Dana back", "mail_id": mid}
    conn.execute("INSERT INTO claude_action (chat_id, message_id, tool_use_id, user_id, tool, input, summary, status, created_at) "
                 "VALUES (?, 0, ?, ?, ?, ?, ?, 'pending', ?)",
                 (chat, f"tu-{mid}-{tool}", uid, tool, json.dumps(inp), f"{tool} for {mid}", datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return conn.execute("SELECT MAX(id) AS m FROM claude_action WHERE chat_id = ?", (chat,)).fetchone()["m"]


def _pending(tool="reply_email"):
    return _q("SELECT a.id, c.mail_id FROM claude_action a JOIN claude_chat c ON c.id = a.chat_id JOIN users u ON u.id = a.user_id "
              "WHERE u.email = ? AND a.tool = ? AND a.status = 'pending' ORDER BY a.id", (ME, tool))


def _plant(tools):
    os.environ["SSC_INBOX_TRIAGE_FAKE"] = json.dumps({"business": True, "kind": "client_question", "summary": "Dana asks",
                                                      "needs_action": True})
    os.environ["SSC_ASSISTANT_FAKE"] = json.dumps([{"tools": tools}, {"text": "A reply is ready."}])
    ca._FAKE["step"] = 0


def main() -> int:
    _cleanup()
    conn = _db()
    try:
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                     "VALUES (?, ?, 'admin', 'Isyn Admin', 1, 'active', 0, 0)", (ME, auth.hash_password(PW)))
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE email = ?", (ME,)).fetchone()["id"]
        conn.execute("INSERT INTO gmail_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) VALUES (?,?,?, 'active', ?, ?)",
                     (uid, ME, secret_box.encrypt_text("r"), "2026-10-23T08:00:00", FULL))
        conn.execute("INSERT INTO mail_sync (user_id, history_id, last_sync_at) VALUES (?, '500', ?)", (uid, "2026-10-23T08:00:00"))
        conn.commit()
    finally:
        conn.close()

    real = inbox._gmail
    inbox._gmail = fake_gmail
    app = Flask("isyn", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    ca.register(app)
    inbox.register(app)
    passed = 0
    try:
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": ME, "password": PW}, environ_base={"REMOTE_ADDR": "10.98.0.3"})
        assert r.status_code == 200, r.status_code

        # ---- a reply sent from Gmail closes the conversation
        conn = _db()
        try:
            d1 = inbox.ingest(conn, uid, gmsg("d1", "isyn-d", 30, "Is the shed permit renewed?"), ME)
            conn.execute("UPDATE mail_message SET ai_state = 'suggested', needs_action = 1 WHERE id = ?", (d1,))
            conn.commit()
            draft = _card(conn, uid, d1)
        finally:
            conn.close()
        assert _row("d1")["status"] == "new"
        assert d1 in [x["id"] for x in c.get("/api/inbox/messages?filter=act").get_json()["data"]]
        assert [x["action_id"] for x in inbox.today_cards(_db(), uid)] == [draft]
        sent("d2", "isyn-d", 2, "Yes, renewed on Oct 20.")
        HISTORY["body"] = {"historyId": "501", "history": [{"messagesAdded": [{"message": {"id": "d2", "labelIds": ["SENT"]}}]}]}
        conn = _db()
        try:
            assert inbox.sync_user(conn, uid) == {"new": 1}
        finally:
            conn.close()
        assert _row("d1")["status"] == "done"
        a = _q("SELECT status, decided_by, result FROM claude_action WHERE id = ?", (draft,))[0]
        assert a["status"] == "declined" and a["decided_by"] is None and "You replied" in json.loads(a["result"])["summary"]
        assert not [x for x in c.get("/api/inbox/messages?filter=act").get_json()["data"] if x["id"] in (d1, _mid("d2"))]
        assert not inbox.today_cards(_db(), uid)
        d2 = _mid("d2")
        assert _row("d2")["status"] == "seen"                                      # your own email is never "unread"
        _plant([{"name": "reply_email", "input": {"mail_id": d2, "body": "Hi Dana, following up."}}])
        conn = _db()
        try:
            inbox.process(conn, d2)
        finally:
            conn.close()
        assert not _pending()                                                       # no draft for an email you sent
        passed += 1; print("  reply from Gmail closes the conversation ok")

        # ---- answered before Claude got to it: sorted only
        conn = _db()
        try:
            e1 = inbox.ingest(conn, uid, gmsg("e1", "isyn-e", 20, "Can you send the COI?"), ME)
            inbox.ingest(conn, uid, sent("e2", "isyn-e", 5, "Attached."), ME)
            _plant([{"name": "reply_email", "input": {"mail_id": e1, "body": "Hi Dana, attached."}}])
            assert inbox.process(conn, e1) == "triaged"
        finally:
            conn.close()
        assert _row("e1")["needs_action"] == 0 and _row("e1")["status"] == "done" and not _pending()
        passed += 1; print("  answered first: no suggestion ok")

        # ---- one reply draft per conversation; Claude told what is waiting; one card of each kind on the dashboard
        conn = _db()
        try:
            f1 = inbox.ingest(conn, uid, gmsg("f1", "isyn-f", 15, "When do you start?"), ME)
            conn.execute("UPDATE mail_message SET ai_state = 'suggested', needs_action = 1 WHERE id = ?", (f1,))
            conn.commit()
            old = _card(conn, uid, f1)
            _card(conn, uid, f1, tool="create_task")
            f2 = inbox.ingest(conn, uid, gmsg("f2", "isyn-f", 3, "Also, which crew?"), ME)
            _card(conn, uid, f2, tool="create_task")
            _plant([{"name": "reply_email", "input": {"mail_id": f2, "body": "Hi Dana,\n\nMonday, with the rope crew.\n\nIsyn"}}])
            assert inbox.process(conn, f2) == "suggested"
            chat2 = inbox.message(conn, f2)["chat_id"]
            ask = [json.loads(x["content"]) for x in conn.execute(
                "SELECT content FROM claude_chat_message WHERE chat_id = ? AND role = 'user'", (chat2,)).fetchall()]
        finally:
            conn.close()
        assert "Already waiting" in json.dumps(ask) and "reply_email" in json.dumps(ask)
        assert _q("SELECT status FROM claude_action WHERE id = ?", (old,))[0]["status"] == "declined"
        assert [x["mail_id"] for x in _pending()] == [f2]
        assert inbox.pending_reply(_db(), uid, [f1, f2])["mail_id"] == f2
        tools = sorted(x["tool"] for x in inbox.today_cards(_db(), uid) if x["href"] in (f"#inbox/{f1}", f"#inbox/{f2}"))
        assert tools == ["create_task", "reply_email"], tools
        passed += 1; print("  one draft per conversation ok")

        # ---- Gmail -> here: read, unread, archived, back to the Inbox; already read / archived on arrival
        conn = _db()
        try:
            inbox.ingest(conn, uid, gmsg("g1", "isyn-g", 10, "Site photos attached."), ME)
            inbox.ingest(conn, uid, gmsg("g2", "isyn-h", 9, "Read in Gmail first.", labels=("INBOX",)), ME)
            inbox.ingest(conn, uid, gmsg("g3", "isyn-i", 8, "Archived in Gmail first.", labels=()), ME)
            conn.execute("UPDATE mail_message SET needs_action = 1 WHERE gmail_id = 'g1'")
            conn.commit()

            def hist(n, added=(), removed=()):
                HISTORY["body"] = {"historyId": str(n), "history": [
                    {"labelsAdded": [{"message": {"id": g}, "labelIds": [lab]} for g, lab in added],
                     "labelsRemoved": [{"message": {"id": g}, "labelIds": [lab]} for g, lab in removed]}]}
                inbox.sync_user(conn, uid)
            assert _row("g2")["status"] == "seen" and _row("g3")["archived_at"] and not _row("g1")["archived_at"]
            hist(510, removed=[("g1", "UNREAD")])
            assert _row("g1")["status"] == "seen"
            hist(511, added=[("g1", "UNREAD")])
            assert _row("g1")["status"] == "new"
            inbox_ids = lambda: [x["id"] for x in c.get("/api/inbox/messages").get_json()["data"]]   # noqa: E731
            g1 = _mid("g1")
            assert g1 in inbox_ids() and _mid("g3") not in inbox_ids()
            hist(512, removed=[("g1", "INBOX")])
            assert _row("g1")["archived_at"] and _row("g1")["status"] == "done" and g1 not in inbox_ids()
            assert g1 not in [x["id"] for x in c.get("/api/inbox/messages?filter=act").get_json()["data"]]
            hist(513, added=[("g1", "INBOX")])
            assert not _row("g1")["archived_at"] and _row("g1")["status"] == "seen" and g1 in inbox_ids()
        finally:
            conn.close()
        passed += 1; print("  Gmail -> here (read, archive) ok")

        # ---- here -> Gmail: open marks read, Done archives, Undo moves back, Mark unread
        conn = _db()
        try:
            conn.execute("UPDATE mail_message SET status = 'new' WHERE gmail_id = 'g1'")
            conn.commit()
        finally:
            conn.close()
        GMAIL["g1"]["labelIds"] = ["INBOX", "UNREAD"]
        n = len(CALLS)
        assert c.get(f"/api/inbox/threads/{g1}").status_code == 200
        mods = [x[2] for x in CALLS[n:] if x[1] == "/messages/batchModify"]
        assert mods == [{"ids": ["g1"], "addLabelIds": [], "removeLabelIds": ["UNREAD"]}], mods
        assert "UNREAD" not in GMAIL["g1"]["labelIds"]
        r = c.post(f"/api/inbox/messages/{g1}/done", json={"done": True}).get_json()["data"]
        assert r["gmail"] is True and "INBOX" not in GMAIL["g1"]["labelIds"] and g1 not in inbox_ids()
        r = c.post(f"/api/inbox/messages/{g1}/done", json={"done": False}).get_json()["data"]
        assert r["gmail"] is True and "INBOX" in GMAIL["g1"]["labelIds"] and g1 in inbox_ids() and _row("g1")["status"] == "seen"
        r = c.post(f"/api/inbox/messages/{g1}/unread").get_json()["data"]
        assert r["gmail"] is True and "UNREAD" in GMAIL["g1"]["labelIds"] and _row("g1")["status"] == "new"
        assert c.post(f"/api/inbox/messages/{d2}/unread").status_code == 200                 # the thread's received email
        conn = _db()
        try:
            conn.execute("UPDATE gmail_link SET scopes = ? WHERE user_id = ?", ("openid email " + inbox.SEND_SCOPE, uid))
            conn.commit()
        finally:
            conn.close()
        n = len(CALLS)
        r = c.post(f"/api/inbox/messages/{g1}/done", json={"done": True}).get_json()["data"]
        assert r["gmail"] is False and g1 not in inbox_ids()                                  # this side only
        assert not [x for x in CALLS[n:] if x[0] == "POST"]
        c.get(f"/api/inbox/threads/{g1}")                                                     # no permission: no reconcile undo
        assert g1 not in inbox_ids()
        conn = _db()
        try:
            conn.execute("UPDATE gmail_link SET scopes = ? WHERE user_id = ?", (FULL, uid))
            conn.commit()
        finally:
            conn.close()
        passed += 1; print("  here -> Gmail (read, done, undo, unread) ok")

        # ---- the open Inbox: change fingerprint + background sync at most every 15 s
        s1 = c.get("/api/inbox/changes").get_json()["data"]
        assert s1["sig"] and "last_sync_at" in s1
        c.post(f"/api/inbox/messages/{g1}/done", json={"done": False})
        s2 = c.get("/api/inbox/changes").get_json()["data"]["sig"]
        assert s2 != s1["sig"] and c.get("/api/inbox/changes").get_json()["data"]["sig"] == s2
        inbox._KICKED.pop(uid, None)
        HISTORY["body"] = {"historyId": "520", "history": []}
        n = len(CALLS)
        c.get("/api/inbox/changes?sync=1")
        for _ in range(50):
            if any(x[1].startswith("/history?") for x in CALLS[n:]):
                break
            time.sleep(0.05)
        assert any(x[1].startswith("/history?") for x in CALLS[n:])
        assert inbox.kick(uid) is False                                                       # within 15 s: not again
        time.sleep(0.2)
        passed += 1; print("  changes + sync on demand ok")
    finally:
        inbox._gmail = real

    try:
        # ---- an email that starts with <meta http-equiv> (Apple Mail, Outlook) opens with its text, not blank
        conn = _db()
        try:
            apple = gmsg("k1", "isyn-k", 1, "plain")
            apple["payload"]["parts"][1]["body"]["data"] = _b64(
                '<html><head><meta http-equiv="content-type" content="text/html; charset=utf-8"><base href="https://x.example/"></head>'
                '<body dir="auto">Isyn: make the proposal out to the building.<script>alert(1)</script>'
                '<iframe src="https://x.example"></iframe><form action="https://x.example"><p>Still here.</p></form><script src="x.js"></body></html>')
            k1 = inbox.ingest(conn, uid, apple, ME)
            doc = inbox.render_doc(conn, uid, inbox.message(conn, k1, uid))
        finally:
            conn.close()
        assert "make the proposal out to the building." in doc and "Still here." in doc, doc
        assert "alert(1)" not in doc and "<script" not in doc and "<iframe" not in doc and "<form" not in doc
        assert 'href="https://x.example/"' not in doc and "content-type" not in doc
        passed += 1; print("  Apple Mail / Outlook emails open with their text ok")

        cp = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        assert 'id="ib-refresh"' not in cp and "↻ Refresh" not in cp
        for needle in ('data-ibtab="mail">Email<', 'data-ibtab="mine">My calendar<', 'Everyone\'s tasks, events and meetings">Team<',
                       'data-ibf="inbox"', ">Needs reply<", ">Filed<", 'id="ib-live"', "/inbox/changes?sync=1",
                       'id="ib-unread"', 'id="ib-done"', "visibilitychange"):
            assert needle in cp, needle
        page = (ROOT / "inbox_page.html").read_text(encoding="utf-8")
        assert "My calendar" in page
        passed += 1; print("  page ok")
    finally:
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: inbox sync ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
