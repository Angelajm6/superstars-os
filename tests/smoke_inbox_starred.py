#!/usr/bin/env python3
"""Gmail's star in the Inbox (prototype accepted 2026-11-10: https://claude.ai/artifact/QBkCoZGgpPNGo2aT2k1EQk).

Proves against an ISOLATED database with a fake Gmail (fictional people only):
  * an email already starred in Gmail comes in starred; a conversation shows starred when any of its emails is
  * starring here stars the newest email in Gmail; taking the star off takes it off every email of the conversation
  * starred / unstarred in Gmail itself (history and a re-read conversation) shows here; the live check notices
  * the Starred filter keeps a conversation after Done (out of the Inbox), like Gmail
  * without Gmail's modify permission only this side changes (gmail: false)
  * the 90-day clean-up keeps starred email
  * admin / C-suite only, own email only; the Inbox page has the star, the filter and the open-email button
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_star_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(Path(_TMP) / "mail"), "SSC_GMAIL_POLL": "0",
                   "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key()})
for _k in ("ANTHROPIC_API_KEY", "SSC_ASSISTANT_FAKE", "SSC_INBOX_TRIAGE_FAKE", "GMAIL_PUBSUB_TOPIC"):
    os.environ.pop(_k, None)

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import db_layer  # noqa: E402
import inbox  # noqa: E402

TAG = "starsm"
PW = "StarSmokePassword2026"
ME = f"{TAG}.admin@superstarscontracting.com"
DANA = "Dana Starsmoke <dana@starsmoke-harbor.example>"
NOW = time.time()
READ_ONLY = "openid email " + inbox.SEND_SCOPE
FULL = READ_ONLY + " " + inbox.MODIFY_SCOPE
GMAIL: dict = {}
CALLS: list = []
HISTORY = {"body": {"historyId": "100", "history": []}}


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def gmsg(gid, thread, minutes_ago, text, labels=("INBOX",)):
    m = {"id": gid, "threadId": thread, "labelIds": list(labels), "internalDate": str(int((NOW - minutes_ago * 60) * 1000)),
         "snippet": text, "payload": {"mimeType": "text/plain", "headers": [
             {"name": "From", "value": DANA}, {"name": "To", "value": ME}, {"name": "Subject", "value": "Star smoke " + thread},
             {"name": "Message-ID", "value": f"<{gid}@starsmoke.example>"}], "body": {"data": _b64(text)}}}
    GMAIL[gid] = m
    return m


def fake_gmail(conn, link, method, path, **kw):
    CALLS.append((method, path, kw.get("json_body")))
    if method == "POST" and path == "/messages/batchModify":
        b = kw["json_body"]
        for g in b["ids"]:
            if g in GMAIL:
                GMAIL[g]["labelIds"] = [x for x in GMAIL[g]["labelIds"] if x not in b["removeLabelIds"]] + \
                    [x for x in b["addLabelIds"] if x not in GMAIL[g]["labelIds"]]
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


def _db():
    return db_layer.connect(pragma_fk=True)


def _q(sql, params=()):
    conn = _db()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _row(gid):
    return _q("SELECT * FROM mail_message WHERE gmail_id = ?", (gid,))[0]


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ca.ensure(conn)
        inbox.ensure(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = ",".join("?" * len(uids)) or "NULL"
        mids = [r["id"] for r in conn.execute(f"SELECT id FROM mail_message WHERE user_id IN ({ph})", uids).fetchall()]
        purge(conn, "mail_link", "message_id", mids)
        purge(conn, "mail_message", "id", mids)
        for t in ("gmail_link", "mail_sendas", "mail_sync"):
            purge(conn, t, "user_id", uids)
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()


def _user(conn, email, role, name):
    conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                 "VALUES (?, ?, ?, ?, 1, 'active', 0, 0)", (email, auth.hash_password(PW), role, name))
    conn.commit()
    return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


def _ingest(uid, msg):
    conn = _db()
    try:
        mid = inbox.ingest(conn, uid, msg, ME, quiet=False)
        conn.execute("UPDATE mail_message SET ai_state = 'triaged' WHERE id = ?", (mid,))
        conn.commit()
        return mid
    finally:
        conn.close()


def main() -> int:
    _cleanup()
    passed = 0
    conn = _db()
    try:
        uid = _user(conn, ME, "admin", "Lee Starsmoke")
        _user(conn, f"{TAG}.sam@superstarscontracting.com", "admin", "Sam Starsmoke")
        _user(conn, f"{TAG}.pm@superstarscontracting.com", "pm", "Pat Starsmoke")
        conn.execute("INSERT INTO gmail_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) VALUES (?,?,?, 'active', ?, ?)",
                     (uid, ME, secret_box.encrypt_text("r"), "2026-11-10T08:00:00", READ_ONLY))
        conn.execute("INSERT INTO mail_sync (user_id, history_id, last_sync_at) VALUES (?, '100', ?)", (uid, "2026-11-10T08:00:00"))
        conn.commit()
    finally:
        conn.close()
    real = inbox._gmail
    inbox._gmail = fake_gmail
    app = Flask("starsm", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    ca.register(app)
    inbox.register(app)
    try:
        c = app.test_client()
        assert c.post("/api/auth/login", json={"email": ME, "password": PW}, environ_base={"REMOTE_ADDR": "10.96.1.3"}).status_code == 200

        def rows(f="inbox"):
            return {r["id"]: r for r in c.get("/api/inbox/messages?filter=" + f).get_json()["data"]}

        # ---- comes in starred; a conversation is starred when any email is
        a1 = _ingest(uid, gmsg("a1", "st-a", 60, "First.", labels=("INBOX", "STARRED")))
        a2 = _ingest(uid, gmsg("a2", "st-a", 50, "Second."))
        b1 = _ingest(uid, gmsg("b1", "st-b", 40, "Other conversation."))
        assert _row("a1")["starred"] == 1 and _row("a2")["starred"] == 0
        r = rows()
        assert r[a2]["starred"] is True and r[b1]["starred"] is False                      # the row is the newest email
        assert set(rows("starred")) == {a2}
        assert c.get(f"/api/inbox/threads/{a2}").get_json()["data"]["starred"] is True
        passed += 1; print("  starred from Gmail, per conversation ok")

        # ---- without Gmail's modify permission: only here
        n = len(CALLS)
        d = c.post(f"/api/inbox/messages/{b1}/star", json={"starred": True}).get_json()["data"]
        assert d["starred"] is True and d["gmail"] is False and _row("b1")["starred"] == 1
        assert not [x for x in CALLS[n:] if x[1] == "/messages/batchModify"]
        assert c.post(f"/api/inbox/messages/{b1}/star", json={"starred": False}).get_json()["data"]["starred"] is False
        passed += 1; print("  without Gmail's permission: this side only ok")

        # ---- with it: on = the newest email, off = every starred email of the conversation
        conn = _db()
        try:
            conn.execute("UPDATE gmail_link SET scopes = ? WHERE user_id = ?", (FULL, uid))
            conn.commit()
        finally:
            conn.close()
        d = c.post(f"/api/inbox/messages/{b1}/star", json={"starred": True}).get_json()["data"]
        assert d["gmail"] is True and "STARRED" in GMAIL["b1"]["labelIds"]
        c.post(f"/api/inbox/messages/{a2}/star", json={"starred": True})
        assert _row("a2")["starred"] == 1 and "STARRED" in GMAIL["a2"]["labelIds"]
        d = c.post(f"/api/inbox/messages/{a2}/star", json={"starred": False}).get_json()["data"]
        assert d["gmail"] is True and _row("a1")["starred"] == 0 and _row("a2")["starred"] == 0
        assert "STARRED" not in GMAIL["a1"]["labelIds"] and "STARRED" not in GMAIL["a2"]["labelIds"]
        assert set(rows("starred")) == {b1}
        assert c.post("/api/inbox/messages/999999/star", json={"starred": True}).status_code == 404
        passed += 1; print("  star here = star in Gmail (newest on, all off) ok")

        # ---- starred / unstarred in Gmail itself
        sig = c.get("/api/inbox/changes").get_json()["data"]["sig"] if "data" in c.get("/api/inbox/changes").get_json() else None
        HISTORY["body"] = {"historyId": "110", "history": [{"labelsAdded": [{"message": {"id": "a1"}, "labelIds": ["STARRED"]}]},
                                                           {"labelsRemoved": [{"message": {"id": "b1"}, "labelIds": ["STARRED"]}]}]}
        conn = _db()
        try:
            inbox.sync_user(conn, uid)
        finally:
            conn.close()
        assert _row("a1")["starred"] == 1 and _row("b1")["starred"] == 0
        assert set(rows("starred")) == {a2}
        if sig is not None:
            assert c.get("/api/inbox/changes").get_json()["data"]["sig"] != sig                 # an open Inbox reloads
        GMAIL["a1"]["labelIds"] = ["INBOX"]                                                       # and a re-read conversation
        conn = _db()
        try:
            inbox.sync_thread(conn, uid, "st-a", force=True)
        finally:
            conn.close()
        assert _row("a1")["starred"] == 0
        passed += 1; print("  starred in Gmail itself shows here (history, re-read conversation) ok")

        # ---- Starred keeps it after Done
        c.post(f"/api/inbox/messages/{b1}/star", json={"starred": True})
        assert c.post(f"/api/inbox/messages/{b1}/done", json={"done": True}).status_code == 200
        assert b1 not in rows("inbox") and b1 in rows("starred")
        passed += 1; print("  Starred keeps an email after Done ok")

        # ---- the 90-day clean-up keeps starred email
        old = _ingest(uid, gmsg("o1", "st-o", 60 * 24 * 120, "Old and starred.", labels=("INBOX", "STARRED")))
        _ingest(uid, gmsg("o2", "st-o2", 60 * 24 * 120, "Old, not starred."))
        HISTORY["body"] = {"historyId": "120", "history": []}
        import gmail_suggest
        real_ur, gmail_suggest.unavailable_reason = gmail_suggest.unavailable_reason, lambda: None
        inbox._LOOP["last_prune"], inbox._LOOP["last_watch"] = 0.0, time.time()
        try:
            inbox.sync_round()
        finally:
            gmail_suggest.unavailable_reason = real_ur
        assert _q("SELECT id FROM mail_message WHERE id = ?", (old,)) and not _q("SELECT id FROM mail_message WHERE gmail_id = 'o2'")
        passed += 1; print("  clean-up keeps starred email ok")

        # ---- who; the page
        sam = app.test_client()
        assert sam.post("/api/auth/login", json={"email": f"{TAG}.sam@superstarscontracting.com", "password": PW},
                        environ_base={"REMOTE_ADDR": "10.96.1.4"}).status_code == 200
        assert sam.post(f"/api/inbox/messages/{b1}/star", json={"starred": False}).status_code == 404
        assert _row("b1")["starred"] == 1
        pm = app.test_client()
        assert pm.post("/api/auth/login", json={"email": f"{TAG}.pm@superstarscontracting.com", "password": PW},
                       environ_base={"REMOTE_ADDR": "10.96.1.5"}).status_code == 200
        assert pm.post(f"/api/inbox/messages/{b1}/star", json={"starred": False}).status_code == 403
        page = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        for s in ('data-ibf="starred"', "data-ibstar", 'id="ib-star"', "/star'", "No starred email", ".ib-star"):
            assert s in page, s
        passed += 1; print("  own email only, office only, page ok")
    finally:
        inbox._gmail = real
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: inbox starred ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
