#!/usr/bin/env python3
"""Inbox: a conversation reads exactly as in Gmail (2026-10-21).

The mailbox sync only brings the last 3 days of the inbox and Sent (no automated mail), so a long thread showed only
its newest emails. Proves against an ISOLATED database with a fake Gmail (fictional people only):
  * opening a thread fetches the emails it was missing: older ones, archived ones, your own sent ones and a robot reply;
    trash and drafts stay out; the earlier emails are quiet (read, never sorted by Claude), new business mail is not
  * asking Gmail again within a minute is skipped (opening / waiting on a thread re-reads it); force asks again
  * the mailbox sync completes the conversation new mail lands in, so the list count is Gmail's
  * the thread API: every email oldest first, Reply / Reply all targets per email, unread flags
  * the list API: one row with Gmail's count and participants ("Dana, me, Pat")
  * a plain-text reply's quoted earlier email is marked for folding (•••)
  * the page carries Gmail's folding, ••• trimming, per-email actions and the phone back arrow
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
_TMP = tempfile.mkdtemp(prefix="ssc_thr_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(Path(_TMP) / "mail"), "SSC_GMAIL_POLL": "0",
                   "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key()})
os.environ.pop("SSC_INBOX_ATTACHMENT_FAKE", None)   # this test's fake Gmail answers the thread calls

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import db_layer  # noqa: E402
import inbox  # noqa: E402

TAG = "thr"
PW = "ThreadsSmokePassword2026"
ME = f"{TAG}.admin@superstarscontracting.com"
DANA = "Dana Thrsmoke <dana@thr-harbor.example>"
PAT = "Pat Thrsmoke <pat@thr-eng.example>"
NOW = time.time()


def _db():
    return db_layer.connect(pragma_fk=True)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def gmsg(gid, thread, days, frm, to, text, cc="", labels=("INBOX",)):
    hs = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": "Re: 1 Thr Smoke Pl facade"},
          {"name": "Message-ID", "value": f"<{gid}@thr.example>"}] + ([{"name": "Cc", "value": cc}] if cc else [])
    return {"id": gid, "threadId": thread, "labelIds": list(labels), "internalDate": str(int((NOW - days * 86400) * 1000)),
            "snippet": text[:80], "payload": {"mimeType": "multipart/alternative", "headers": hs, "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64(text)}}, {"mimeType": "text/html", "body": {"data": _b64(f"<p>{text}</p>")}}]}}


THREAD = [
    gmsg("t0", "thr-a", 9, DANA, ME, "Please price the facade.", labels=()),                                  # archived
    gmsg("t1", "thr-a", 8, ME, "dana@thr-harbor.example", "We can walk it Thursday.", labels=("SENT",)),       # yours
    gmsg("t2", "thr-a", 7, "Portal <no-reply@thr-portal.example>", ME, "Dana shared a file with you."),       # a robot
    gmsg("t3", "thr-a", 6, PAT, ME, "Add the parapet alternate.", cc=DANA, labels=("INBOX",)),
    gmsg("tx", "thr-a", 5, DANA, ME, "deleted", labels=("TRASH",)),
    gmsg("td", "thr-a", 4, ME, DANA, "half-written", labels=("DRAFT",)),
    gmsg("t4", "thr-a", 1, DANA, ME, "Board approved. When can you start?", cc=PAT),                         # stored by the sync
    gmsg("t5", "thr-a", 0.01, DANA, ME, "One more question about the schedule.", cc=PAT),                    # brand new, not synced
]
OTHER = [gmsg("u0", "thr-b", 6, PAT, ME, "Older email in the second thread.", labels=()),
         gmsg("u1", "thr-b", 0.02, PAT, ME, "New email in the second thread.")]
BY_ID = {m["id"]: m for m in THREAD + OTHER}
CALLS = []


def fake_gmail(conn, link, method, path, **kw):
    CALLS.append(path)
    if path.startswith("/threads/"):
        t = path.split("/")[2].split("?")[0]
        msgs = [m for m in THREAD + OTHER if m["threadId"] == t]
        return (200, {"id": t, "messages": msgs}) if msgs else (404, {})
    if path.startswith("/history?"):
        return 200, {"historyId": "201", "history": [{"messagesAdded": [{"message": {"id": "u1", "labelIds": ["INBOX"]}}]}]}
    if path.startswith("/messages/") and "format=full" in path:
        g = path.split("/")[2].split("?")[0]
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


def main() -> int:
    _cleanup()
    conn = _db()
    try:
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                     "VALUES (?, ?, 'admin', 'Thr Admin', 1, 'active', 0, 0)", (ME, auth.hash_password(PW)))
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE email = ?", (ME,)).fetchone()["id"]
        conn.execute("INSERT INTO gmail_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) VALUES (?,?,?, 'active', ?, ?)",
                     (uid, ME, secret_box.encrypt_text("r"), "2026-10-21T08:00:00", "openid email " + inbox.SEND_SCOPE))
        conn.commit()
        assert inbox.ingest(conn, uid, THREAD[6], ME)   # what the 3-day sync left: the newest email only
        conn.execute("UPDATE mail_message SET ai_state = 'triaged', status = 'seen' WHERE user_id = ?", (uid,))
        conn.commit()
    finally:
        conn.close()

    real = inbox._gmail
    inbox._gmail = fake_gmail
    app = Flask("thr", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    ca.register(app)
    inbox.register(app)
    passed = 0
    try:
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": ME, "password": PW}, environ_base={"REMOTE_ADDR": "10.98.0.1"})
        assert r.status_code == 200, r.status_code

        # ---- opening the thread completes it from Gmail
        mid = _db().execute("SELECT id FROM mail_message WHERE gmail_id = 't4'").fetchone()["id"]
        t = c.get(f"/api/inbox/threads/{mid}").get_json()["data"]
        assert [m["snippet"] for m in t["messages"]] == ["Please price the facade.", "We can walk it Thursday.", "Dana shared a file with you.",
                                                         "Add the parapet alternate.", "Board approved. When can you start?",
                                                         "One more question about the schedule."], [m["snippet"] for m in t["messages"]]
        rows = {r["gmail_id"]: dict(r) for r in _db().execute("SELECT gmail_id, status, ai_state, direction FROM mail_message WHERE user_id = ?",
                                                                (uid,)).fetchall()}
        assert "tx" not in rows and "td" not in rows                                      # trash + drafts stay out
        for g in ("t0", "t1", "t2", "t3"):
            assert rows[g]["ai_state"] == "triaged", (g, rows[g])                          # earlier emails: never sorted by Claude
        assert rows["t1"]["direction"] == "out"
        assert rows["t5"]["ai_state"] == "pending"                                         # new business mail: the normal path
        passed += 1; print("  thread completed from Gmail ok")

        # ---- asked again within a minute: skipped; force asks
        n = len(CALLS)
        c.get(f"/api/inbox/threads/{mid}")
        assert not [p for p in CALLS[n:] if p.startswith("/threads/")]
        conn = _db()
        try:
            uid = conn.execute("SELECT id FROM users WHERE email = ?", (ME,)).fetchone()["id"]
            assert inbox.sync_thread(conn, uid, "thr-a", force=True) == 0 and CALLS[-1].startswith("/threads/thr-a")
        finally:
            conn.close()
        passed += 1; print("  re-check throttle ok")

        # ---- per-email reply targets, unread flags
        pat = t["messages"][3]
        assert pat["reply_to"]["to"] == PAT and DANA.split("<")[1][:-1] in pat["reply_all_to"]["cc"]
        assert t["messages"][0]["unread"] is False and t["messages"][-1]["unread"] is True
        assert all(m["subject"] for m in t["messages"])
        passed += 1; print("  per-email actions ok")

        # ---- the mailbox sync completes the thread new mail lands in
        conn = _db()
        try:
            conn.execute("INSERT INTO mail_sync (user_id, history_id, last_sync_at) VALUES (?, '200', ?)", (uid, "2026-10-21T08:00:00"))
            conn.commit()
            assert inbox.sync_user(conn, uid) == {"new": 1}
            assert {r["gmail_id"] for r in conn.execute("SELECT gmail_id FROM mail_message WHERE thread_id = 'thr-b'").fetchall()} == {"u0", "u1"}
        finally:
            conn.close()
        passed += 1; print("  sync completes threads ok")

        # ---- the list: Gmail's count + participants
        lst = c.get("/api/inbox/messages").get_json()["data"]
        a = next(x for x in lst if x["subject"].endswith("Thr Smoke Pl facade") and x["count"] == 6)
        assert a["people"] == ["Dana", "me", "Portal", "Pat"], a["people"]
        passed += 1; print("  list count + people ok")

        # ---- plain-text quote folds
        doc = inbox.render_doc(None, uid, {"full": "Sounds good.\n\nOn Mon, Oct 20, 2026 at 9:00 AM Dana <d@x.example> wrote:\n> earlier"})
        assert 'class="gmail_quote"' in doc and doc.index("Sounds good.") < doc.index("gmail_quote")
        assert "gmail_quote" not in inbox.render_doc(None, uid, {"full": "No quote here."})
        passed += 1; print("  plain-text quote ok")

        cp = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        for needle in ("function ibTrim(f, btn)", "className = 'ib-fold'", "keep = new Set([0, n - 2, n - 1])", 'data-mrep="fwd"',
                       'id="ib-back"', "function ibPeople(r)", "(t ? t.id : v.last_id) + '/reply'", 'id="ib-expand"'):
            assert needle in cp, needle
        passed += 1; print("  page ok")
    finally:
        inbox._gmail = real
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: inbox threads ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
