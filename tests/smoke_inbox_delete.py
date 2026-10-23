#!/usr/bin/env python3
"""Inbox: Delete as in Gmail + conversations that always load in full (2026-10-22).

Proves against an ISOLATED database with a fake Gmail (fictional people only):
  * Delete needs Gmail's modify permission: without it the Inbox says to reconnect (status can_delete, 409)
  * Delete moves the whole conversation to Gmail's Trash (POST /threads/<id>/trash): it leaves the list, the thread
    no longer opens, the "needs action" count drops; Undo (restore) brings it back with its status untouched
  * Gmail not answering: nothing is marked deleted (502)
  * deleted / restored in Gmail itself follows here: from the change history and when a conversation is opened
  * a conversation keeps an email of any category (Promotions...), as Gmail shows it
  * a long email Gmail hands as a separate download is fetched, so it shows in full
  * Gmail calls are tried again after a blip (503, dropped connection) and a stale token (401); a send is never repeated
  * an email of the thread that did not come through is asked for again on the next open (no minute's wait)
  * the page carries the trash buttons, the selection bar, the # shortcut and Undo
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import base64
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_del_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(Path(_TMP) / "mail"), "SSC_GMAIL_POLL": "0",
                   "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key()})
os.environ.pop("SSC_INBOX_ATTACHMENT_FAKE", None)   # this test's fake Gmail answers

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import db_layer  # noqa: E402
import gcal_sync  # noqa: E402
import gmail_suggest  # noqa: E402
import inbox  # noqa: E402

TAG = "idel"
PW = "DeleteSmokePassword2026"
ME = f"{TAG}.admin@superstarscontracting.com"
DANA = "Dana Idelsmoke <dana@idel-harbor.example>"
NOW = time.time()


def _db():
    return db_layer.connect(pragma_fk=True)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def gmsg(gid, thread, days, text, labels=("INBOX",), frm=DANA, html_att=None):
    hs = [{"name": "From", "value": frm}, {"name": "To", "value": ME}, {"name": "Subject", "value": "Idel smoke " + thread},
          {"name": "Message-ID", "value": f"<{gid}@idel.example>"}]
    html = {"mimeType": "text/html", "body": {"attachmentId": html_att, "size": 90000}} if html_att else \
        {"mimeType": "text/html", "body": {"data": _b64(f"<p>{text}</p>")}}
    return {"id": gid, "threadId": thread, "labelIds": list(labels), "internalDate": str(int((NOW - days * 86400) * 1000)),
            "snippet": text[:80], "payload": {"mimeType": "multipart/alternative", "headers": hs, "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64(text)}}, html]}}


MSGS = [gmsg("a1", "del-a", 2, "First in A."), gmsg("a2", "del-a", 1, "Second in A."),
        gmsg("b1", "del-b", 1, "Only in B."),
        gmsg("c1", "del-c", 5, "Vendor promo reply.", labels=("CATEGORY_PROMOTIONS",)),   # archived, categorized
        gmsg("c2", "del-c", 4, "Long one.", labels=(), html_att="att-long"),              # body as a download
        gmsg("c3", "del-c", 1, "Latest in C.")]
BY_ID = {m["id"]: m for m in MSGS}
CALLS, FAIL = [], {"post": 0, "full": set()}
HISTORY = {"body": {}}


def fake_gmail(conn, link, method, path, **kw):
    CALLS.append((method, path))
    if method == "POST" and path.endswith(("/trash", "/untrash")):
        if FAIL["post"]:
            return 500, {}
        t = path.split("/")[2]
        for m in MSGS:   # as Gmail: every email of the conversation gets (or loses) the TRASH label
            if m["threadId"] == t:
                m["labelIds"] = [x for x in m["labelIds"] if x != "TRASH"] + (["TRASH"] if path.endswith("/trash") else [])
        return 200, {}
    if path.startswith("/threads/"):
        t = path.split("/")[2].split("?")[0]
        msgs = [{"id": m["id"], "threadId": m["threadId"], "labelIds": m["labelIds"]} for m in MSGS if m["threadId"] == t]
        return (200, {"id": t, "messages": msgs}) if msgs else (404, {})
    if "/attachments/att-long" in path:
        return 200, {"data": _b64("<div>The whole long email, as written.</div>"), "size": 90000}
    if path.startswith("/history?"):
        return 200, HISTORY["body"]
    if path.startswith("/messages/") and "format=full" in path:
        g = path.split("/")[2].split("?")[0]
        if g in FAIL["full"]:
            return 503, {}
        return (200, BY_ID[g]) if g in BY_ID else (404, {})
    return 500, {}


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


def _q(sql, params=()):
    conn = _db()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _mid(gid):
    return _q("SELECT id FROM mail_message WHERE gmail_id = ?", (gid,))[0]["id"]


def main() -> int:
    _cleanup()
    conn = _db()
    try:
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                     "VALUES (?, ?, 'admin', 'Idel Admin', 1, 'active', 0, 0)", (ME, auth.hash_password(PW)))
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE email = ?", (ME,)).fetchone()["id"]
        conn.execute("INSERT INTO gmail_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) VALUES (?,?,?, 'active', ?, ?)",
                     (uid, ME, secret_box.encrypt_text("r"), "2026-10-22T08:00:00", "openid email " + inbox.SEND_SCOPE))
        for m in (MSGS[0], MSGS[1], MSGS[2], MSGS[5]):
            assert inbox.ingest(conn, uid, m, ME)
        conn.execute("UPDATE mail_message SET ai_state = 'triaged', status = 'seen' WHERE user_id = ?", (uid,))
        conn.execute("UPDATE mail_message SET needs_action = 1, status = 'done' WHERE gmail_id = 'a2'")
        conn.execute("UPDATE mail_message SET needs_action = 1 WHERE gmail_id = 'b1'")
        conn.commit()
    finally:
        conn.close()

    real = inbox._gmail
    inbox._gmail = fake_gmail
    app = Flask("idel", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    ca.register(app)
    inbox.register(app)
    passed = 0
    try:
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": ME, "password": PW}, environ_base={"REMOTE_ADDR": "10.98.0.2"})
        assert r.status_code == 200, r.status_code
        a2, b1 = _mid("a2"), _mid("b1")

        # ---- Delete needs Gmail's modify permission
        assert "can_delete" in c.get("/api/inbox/status").get_json()["data"]
        assert inbox.can_delete(_q("SELECT scopes FROM gmail_link WHERE user_id = ?", (uid,))[0]) is False
        r = c.post("/api/inbox/trash", json={"ids": [a2]})
        assert r.status_code == 409 and "Reconnect" in r.get_json()["error"]
        assert not [x for x in CALLS if x[0] == "POST"]
        conn = _db()
        try:
            conn.execute("UPDATE gmail_link SET scopes = ? WHERE user_id = ?", ("openid email " + inbox.SEND_SCOPE + " " + inbox.MODIFY_SCOPE, uid))
            conn.commit()
        finally:
            conn.close()
        assert inbox.can_delete(_q("SELECT scopes FROM gmail_link WHERE user_id = ?", (uid,))[0]) is True
        assert c.post("/api/inbox/trash", json={"ids": []}).status_code == 400
        passed += 1; print("  permission gate ok")

        # ---- Delete: the conversation goes to Gmail's Trash and leaves the Inbox; Undo brings it back
        before = c.get("/api/inbox/status").get_json()["data"]["needs_action"]
        r = c.post("/api/inbox/trash", json={"ids": [a2, b1]})
        assert r.status_code == 200 and r.get_json()["data"] == {"ids": [a2, b1], "failed": 0}, r.get_json()
        assert ("POST", "/threads/del-a/trash") in CALLS and ("POST", "/threads/del-b/trash") in CALLS
        assert all(x["trashed_at"] for x in _q("SELECT trashed_at FROM mail_message WHERE thread_id IN ('del-a', 'del-b')"))
        ids = {x["id"] for x in c.get("/api/inbox/messages").get_json()["data"]}
        assert a2 not in ids and b1 not in ids and _mid("c3") in ids
        assert c.get(f"/api/inbox/threads/{a2}").status_code == 404
        assert c.get("/api/inbox/status").get_json()["data"]["needs_action"] == before - 1   # b1 needed action; a2 was done
        r = c.post("/api/inbox/trash", json={"ids": [a2], "restore": True})
        assert r.status_code == 200 and ("POST", "/threads/del-a/untrash") in CALLS
        assert a2 in {x["id"] for x in c.get("/api/inbox/messages").get_json()["data"]}
        assert _q("SELECT status FROM mail_message WHERE id = ?", (a2,))[0]["status"] == "done"   # Undo keeps the status
        assert len(c.get(f"/api/inbox/threads/{a2}").get_json()["data"]["messages"]) == 2
        passed += 1; print("  delete + undo ok")

        # ---- Gmail not answering: nothing marked
        FAIL["post"] = 1
        r = c.post("/api/inbox/trash", json={"ids": [a2]})
        assert r.status_code == 502 and not _q("SELECT trashed_at FROM mail_message WHERE id = ?", (a2,))[0]["trashed_at"]
        FAIL["post"] = 0
        passed += 1; print("  gmail down: nothing deleted ok")

        # ---- deleted / restored in Gmail itself follows here (change history)
        conn = _db()
        try:
            conn.execute("INSERT INTO mail_sync (user_id, history_id, last_sync_at) VALUES (?, '300', ?)", (uid, "2026-10-22T08:00:00"))
            conn.commit()
            HISTORY["body"] = {"historyId": "301", "history": [{"labelsAdded": [{"message": {"id": "a1"}, "labelIds": ["TRASH"]}]},
                                                                 {"labelsAdded": [{"message": {"id": "a2"}, "labelIds": ["TRASH"]}]}]}
            assert inbox.sync_user(conn, uid) == {"new": 0}
            assert all(x["trashed_at"] for x in _q("SELECT trashed_at FROM mail_message WHERE thread_id = 'del-a'"))
            HISTORY["body"] = {"historyId": "302", "history": [{"labelsRemoved": [{"message": {"id": "a2"}, "labelIds": ["TRASH"]}]}]}
            inbox.sync_user(conn, uid)
            assert not _q("SELECT trashed_at FROM mail_message WHERE gmail_id = 'a2'")[0]["trashed_at"]
            assert any("historyTypes=labelAdded" in p for _m, p in CALLS)
        finally:
            conn.close()
        passed += 1; print("  trash in Gmail followed ok")

        # ---- opening a conversation: every category kept, a long email fetched, a failed email asked again
        FAIL["full"] = {"c1"}
        c3 = _mid("c3")
        t = c.get(f"/api/inbox/threads/{c3}").get_json()["data"]
        assert [m["snippet"] for m in t["messages"]] == ["Long one.", "Latest in C."], [m["snippet"] for m in t["messages"]]
        assert "The whole long email, as written." in t["messages"][0]["doc"]
        assert any("/attachments/att-long" in p for _m, p in CALLS)
        FAIL["full"] = set()
        n = len(CALLS)
        t = c.get(f"/api/inbox/threads/{c3}").get_json()["data"]   # within the minute, but one was missing: asked again
        assert [p for _m, p in CALLS[n:] if p.startswith("/threads/del-c")]
        assert [m["snippet"] for m in t["messages"]] == ["Vendor promo reply.", "Long one.", "Latest in C."]
        n = len(CALLS)
        c.get(f"/api/inbox/threads/{c3}")                         # complete now: the minute's wait applies again
        assert not [p for _m, p in CALLS[n:] if p.startswith("/threads/")]
        conn = _db()
        try:   # a stored email trashed in Gmail: the thread stubs say so on the next look
            BY_ID["c1"]["labelIds"] = ["CATEGORY_PROMOTIONS", "TRASH"]
            inbox.sync_thread(conn, uid, "del-c", force=True)
            assert _q("SELECT trashed_at FROM mail_message WHERE gmail_id = 'c1'")[0]["trashed_at"]
        finally:
            conn.close()
        passed += 1; print("  full thread ok")
    finally:
        inbox._gmail = real

    try:
        # ---- the Gmail call itself: blips and stale tokens are tried again; a send never twice
        import requests
        real_http, real_tok, real_sleep = gcal_sync._http, gmail_suggest._access_token, time.sleep
        script, seen = [], []

        def http(method, url, **kw):
            seen.append((method, kw.get("token")))
            step = script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step, {}
        toks = iter(f"tok{i}" for i in range(100))
        gcal_sync._http, gmail_suggest._access_token, time.sleep = http, (lambda conn, link: next(toks)), (lambda s: None)
        try:
            link = {"user_id": -1}
            script[:] = [503, 200]
            assert inbox._gmail(None, link, "GET", "/threads/x")[0] == 200 and len(seen) == 2
            seen.clear(); script[:] = [requests.ConnectionError(), 200]
            assert inbox._gmail(None, link, "GET", "/messages/x")[0] == 200 and len(seen) == 2
            seen.clear(); script[:] = [401, 200]
            assert inbox._gmail(None, link, "GET", "/messages/x")[0] == 200 and seen[0][1] != seen[1][1]   # a new token
            seen.clear(); script[:] = [500, 500, 500]
            assert inbox._gmail(None, link, "POST", "/threads/x/trash")[0] == 500 and len(seen) == 3
            seen.clear(); script[:] = [503]
            assert inbox._gmail(None, link, "POST", "/messages/send")[0] == 503 and len(seen) == 1       # a send: once
            seen.clear(); script[:] = [requests.Timeout()]
            try:
                inbox._gmail(None, link, "POST", "/messages/send")
                raise AssertionError("a send must not be repeated")
            except requests.Timeout:
                pass
            assert len(seen) == 1
        finally:
            gcal_sync._http, gmail_suggest._access_token, time.sleep = real_http, real_tok, real_sleep
        passed += 1; print("  retries ok")

        cp = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        for needle in ("async function ibTrash(ids)", 'id="ib-del"', "data-ibck", 'id="ib-selbar"', "data-ibdel", "e.key !== '#'",
                       "restore: true", 'id="ib-retry"', "st.can_delete", ".cp-toast.show a, .cp-toast.show button"):
            assert needle in cp, needle
        gs = (ROOT / "gmail_suggest.py").read_text(encoding="utf-8")
        assert "inbox.MODIFY_SCOPE" in gs
        passed += 1; print("  page ok")
    finally:
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: inbox delete ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
