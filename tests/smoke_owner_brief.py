#!/usr/bin/env python3
"""The owner's morning brief (gmail_brief.py, stage 2 of the prototype accepted 2026-11-09).

Proves against an ISOLATED database with a fake Gmail and planted Claude answers (fictional people only):
  * settings: off by default; turning it on needs a "For" person; time and days take only the listed values
  * a brief: one item per conversation that got "For <owner>" since the last brief; sorted lines, groups and deadlines
    from Claude's small model (its cost counted); personal mail, older email from "Sort my last 90 days" and FYI are
    never items; FYI is counted
  * the email to the owner: greeting, groups, one line per item with job code and deadline, notes, the FYI count
  * items: note, Tomorrow, Remove (the email loses "For"), Undo (it gets it back), "<owner> answered", an item of your own
  * Send: from the person's Gmail to the owner's address; needs Gmail's send permission
  * next morning: what is still open comes back under "Still waiting", Tomorrow's go back to their group, an item the
    owner answered in the conversation closes by itself, removed / answered ones stay gone
  * the clock: made once at the ready time on brief days only (weekdays by default)
  * the bell says the brief is ready until it is sent
  * admin / C-suite only; nobody reaches another person's brief; the Inbox and Settings pages have it
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
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_brief_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(Path(_TMP) / "mail"), "SSC_GMAIL_POLL": "0",
                   "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key()})
for _k in ("ANTHROPIC_API_KEY", "SSC_ASSISTANT_FAKE", "SSC_INBOX_TRIAGE_FAKE", "SSC_BRIEF_FAKE", "SSC_ASSISTANT_DAILY_CAP_USD",
           "SSC_ASSISTANT_MONTHLY_BUDGET_USD", "SSC_ASSISTANT_USER_DAILY_USD", "GMAIL_PUBSUB_TOPIC"):
    os.environ.pop(_k, None)

from flask import Flask  # noqa: E402

import activity_feed  # noqa: E402
import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import db_layer  # noqa: E402
import gmail_brief as gb  # noqa: E402
import gmail_labels as gl  # noqa: E402
import inbox  # noqa: E402

TAG = "brfsm"
PW = "BriefSmokePassword2026"
ME = f"{TAG}.admin@superstarscontracting.com"
OWNER = f"{TAG}.robin@superstarscontracting.com"
DANA = "Dana Brfsmoke <dana@brfsmoke-harbor.example>"
NOW = time.time()
FULL = "openid email " + inbox.SEND_SCOPE + " " + inbox.MODIFY_SCOPE


def _db():
    return db_layer.connect(pragma_fk=True)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def gmsg(gid, thread, minutes_ago, text, frm=DANA, to=ME, cc="", subject=None):
    hs = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject or "Brf smoke " + thread},
          {"name": "Message-ID", "value": f"<{gid}@brfsmoke.example>"}]
    if cc:
        hs.append({"name": "Cc", "value": cc})
    return {"id": gid, "threadId": thread, "labelIds": ["INBOX", "UNREAD"], "internalDate": str(int((NOW - minutes_ago * 60) * 1000)),
            "snippet": text[:80], "payload": {"mimeType": "multipart/alternative", "headers": hs, "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64(text)}}]}}


def fake_gmail(conn, link, method, path, **kw):
    return 500, {}   # Gmail labels stay off in this suite; nothing should reach Gmail but the send (patched below)


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
        for t in ("gmail_link", "mail_sendas", "mail_sync", "mail_label_pref", "mail_label_map", "mail_brief_pref", "mail_brief_item",
                  "mail_brief", "activity_seen"):
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


def _row(gid):
    return _q("SELECT * FROM mail_message WHERE gmail_id = ?", (gid,))[0]


def _take(uid, msg, kind, owner, needs=True, business=True, backfill=False):
    os.environ["SSC_INBOX_TRIAGE_FAKE"] = json.dumps({"business": business, "kind": kind, "summary": "Sorting summary " + msg["id"],
                                                      "needs_action": needs, "for_owner": owner})
    os.environ["SSC_ASSISTANT_FAKE"] = json.dumps([{"text": "Nothing to propose."}])
    ca._FAKE["step"] = 0
    conn = _db()
    try:
        mid = inbox.ingest(conn, uid, msg, ME, backfill=backfill)
        inbox.process(conn, mid)
        return mid
    finally:
        conn.close()


def _user(conn, email, role, name):
    conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                 "VALUES (?, ?, ?, ?, 1, 'active', 0, 0)", (email, auth.hash_password(PW), role, name))
    conn.commit()
    return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


def _build(uid, now):
    conn = _db()
    try:
        return gb.build(conn, uid, now)
    finally:
        conn.close()


def main() -> int:
    _cleanup()
    passed = 0
    conn = _db()
    try:
        uid = _user(conn, ME, "admin", "Lee Brfsmoke")
        owner_id = _user(conn, OWNER, "c_suite", "Robin Brfsmoke")
        other = _user(conn, f"{TAG}.sam@superstarscontracting.com", "admin", "Sam Brfsmoke")
        _user(conn, f"{TAG}.pm@superstarscontracting.com", "pm", "Pat Brfsmoke")
        conn.execute("INSERT INTO gmail_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) VALUES (?,?,?, 'active', ?, ?)",
                     (uid, ME, secret_box.encrypt_text("r"), "2026-11-09T08:00:00", "openid email"))
        conn.commit()
    finally:
        conn.close()

    real_gmail, real_compose = inbox._gmail, inbox.compose
    SENT: list = []
    inbox._gmail = fake_gmail
    inbox.compose = lambda conn, u, to, cc, subject, body, attachments=None: SENT.append((u, to, subject, body)) or {"sent": True}
    app = Flask("brfsm", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    ca.register(app)
    inbox.register(app)
    gl.register(app)
    gb.register(app)
    try:
        c = app.test_client()
        assert c.post("/api/auth/login", json={"email": ME, "password": PW}, environ_base={"REMOTE_ADDR": "10.97.1.3"}).status_code == 200

        # ---- settings
        d = c.get("/api/inbox/brief").get_json()["data"]
        assert d["enabled"] is False and d["owner"] is None and d["today"] is None and d["ready_at"] == "07:00" and d["days"] == "weekdays"
        assert c.put("/api/inbox/brief/settings", json={"enabled": True}).status_code == 409          # nobody picked yet
        assert c.post("/api/inbox/brief/build").status_code == 409
        assert c.put("/api/inbox/labels", json={"owner_user_id": owner_id}).status_code == 200
        assert c.put("/api/inbox/brief/settings", json={"ready_at": "09:15"}).status_code == 400
        assert c.put("/api/inbox/brief/settings", json={"days": "weekends"}).status_code == 400
        d = c.put("/api/inbox/brief/settings", json={"enabled": True, "ready_at": "07:30", "days": "weekdays"}).get_json()["data"]
        assert d["enabled"] is True and d["ready_at"] == "07:30" and d["owner"]["first"] == "Robin" and d["today"] is None
        assert d["next_label"].endswith("at 7:30 AM")
        passed += 1; print("  settings ok")

        # ---- the email
        a1 = _take(uid, gmsg("a1", "brf-a", 50, "Invoice 44817 for $7,820, please OK."), "payment", True)
        b1 = _take(uid, gmsg("b1", "brf-b", 40, "Can we add the coping repair?"), "client_question", True)
        _take(uid, gmsg("b2", "brf-b", 30, "Following up on the coping."), "client_question", True)           # same conversation
        _take(uid, gmsg("c1", "brf-c", 25, "Bids due Thursday 2 PM. Go or no-go?"), "bid_invitation", True)
        _take(uid, gmsg("f1", "brf-f", 20, "FYI the shed is down.", to="Ops <ops@x.example>", cc=ME), "scheduling", False, needs=False)
        _take(uid, gmsg("x1", "brf-x", 15, "Your gym receipt."), "payment", True, business=False)              # personal: never
        _take(uid, gmsg("o1", "brf-o", 10, "Old: sign the CO?"), "proposal_reply", True, backfill=True)        # older email: never
        assert _row("b2")["for_owner"] == 1 and _row("o1")["backfilled_at"]
        os.environ["SSC_BRIEF_FAKE"] = json.dumps({"items": [                                                 # newest first: c, b, a
            {"n": 1, "group": "dead", "line": "Bid on 88 Ocean Pkwy: go or no-go?", "due": "Bids due Thu, 2:00 PM"},
            {"n": 2, "group": "decide", "line": "Add the coping repair to the current job?", "due": ""},
            {"n": 3, "group": "money", "line": "Coastline invoice 44817, $7,820.", "due": "Due Thu"}]})
        day1 = datetime.now().replace(microsecond=0)
        bid = _build(uid, day1)
        assert bid and _build(uid, day1) == bid                                                                # once a day
        assert _q("SELECT id FROM claude_chat WHERE user_id = ? AND source = 'brief'", (uid,))                  # cost counted
        v = c.get("/api/inbox/brief").get_json()["data"]["today"]
        assert v and v["fyi"] == 1, v and v["fyi"]
        items = {i["line"]: i for i in v["items"]}
        assert set(items) == {"Bid on 88 Ocean Pkwy: go or no-go?", "Add the coping repair to the current job?", "Coastline invoice 44817, $7,820."}
        assert items["Bid on 88 Ocean Pkwy: go or no-go?"]["group"] == "dead" and items["Bid on 88 Ocean Pkwy: go or no-go?"]["due"] == "Bids due Thu, 2:00 PM"
        assert items["Add the coping repair to the current job?"]["message_id"] == _row("b2")["id"]          # the newest email of it
        assert items["Coastline invoice 44817, $7,820."]["code"] == "Payment" and items["Coastline invoice 44817, $7,820."]["from"] == "Dana Brfsmoke"
        raw = _q("SELECT line_enc FROM mail_brief_item WHERE brief_id = ?", (bid,))
        assert all("44817" not in (r["line_enc"] or "") for r in raw)                                          # encrypted at rest
        m = v["email"]
        assert m["to"] == OWNER and m["to_name"] == "Robin Brfsmoke" and m["subject"].startswith("Today: 3 things that need you, ")
        for s in ("Good morning Robin,", "DECIDE", "MONEY", "DEADLINES", "1. Bid on 88 Ocean Pkwy: go or no-go? (New bid, Bids due Thu, 2:00 PM)",
                  "1 other email was FYI only.", "\nLee\n"):
            assert s in m["body"], (s, m["body"])
        passed += 1; print("  a brief: one item per conversation, lines + groups + deadlines, FYI counted, the email ok")

        # ---- items
        by = {i["line"][:8]: i["id"] for i in v["items"]}
        bid_item, cope, inv = by["Bid on 8"], by["Add the "], by["Coastlin"]
        assert c.post(f"/api/inbox/brief/items/{cope}", json={"action": "note", "note": "I already called them."}).status_code == 200
        assert c.post(f"/api/inbox/brief/items/{inv}", json={"action": "remove"}).get_json()["data"]["state"] == "removed"
        assert _row("a1")["for_owner"] == 0                                                                   # not for the owner
        assert c.post(f"/api/inbox/brief/items/{inv}", json={"action": "undo"}).get_json()["data"]["state"] == "open"
        assert _row("a1")["for_owner"] == 1                                                                   # back on
        assert c.post(f"/api/inbox/brief/items/{bid_item}", json={"action": "tomorrow"}).status_code == 200
        assert c.post(f"/api/inbox/brief/items/{bid_item}", json={"action": "fly"}).status_code == 404
        assert c.post(f"/api/inbox/brief/{bid}/items", json={"text": "  "}).status_code == 400
        own = c.post(f"/api/inbox/brief/{bid}/items", json={"text": "Lunch with the Halsey board at 1."}).get_json()["data"]
        assert own["message_id"] is None and own["group"] == "decide"
        v = c.get("/api/inbox/brief").get_json()["data"]["today"]
        assert "   Note: I already called them." in v["email"]["body"] and "Lunch with the Halsey board at 1." in v["email"]["body"]
        assert "Bid on 88" not in v["email"]["body"] and v["email"]["subject"].startswith("Today: 3 things")
        passed += 1; print("  items: note, remove + undo (For off / on), tomorrow, your own item ok")

        # ---- send
        r = c.post(f"/api/inbox/brief/{bid}/send", json={"subject": "Today", "body": "Hi"})
        assert r.status_code == 409 and not SENT                                                               # no send permission yet
        conn = _db()
        try:
            conn.execute("UPDATE gmail_link SET scopes = ? WHERE user_id = ?", (FULL, uid))
            conn.commit()
        finally:
            conn.close()
        assert c.post(f"/api/inbox/brief/{bid}/send", json={"subject": "", "body": "Hi"}).status_code == 400
        r = c.post(f"/api/inbox/brief/{bid}/send", json={"subject": v["email"]["subject"], "body": v["email"]["body"] + "Edited line.\n"})
        assert r.status_code == 200 and r.get_json()["data"]["today"]["sent_at"]
        assert SENT[-1][0] == uid and SENT[-1][1] == OWNER and SENT[-1][3].endswith("Edited line.\n")
        passed += 1; print("  send: from your Gmail to the owner, needs send permission ok")

        # ---- next morning
        conn = _db()
        try:                                                                                                   # the owner answers the coping email
            inbox.ingest(conn, uid, gmsg("b3", "brf-b", 5, "Yes, add it.", frm=f"Robin Brfsmoke <{OWNER}>", to=DANA, cc=ME), ME)
        finally:
            conn.close()
        assert c.post(f"/api/inbox/brief/items/{own['id']}", json={"action": "answered"}).status_code == 200
        _take(uid, gmsg("d1", "brf-d", -30, "Please sign the subcontract."), "proposal_reply", True)   # after the first brief
        os.environ["SSC_BRIEF_FAKE"] = json.dumps({"items": [{"n": 1, "group": "approve", "line": "Sign the rope access subcontract.", "due": ""}]})
        bid2 = _build(uid, day1 + timedelta(days=1))
        assert bid2 != bid
        conn = _db()
        try:
            v2 = {i["line"]: i for i in (gb._public_item(r) for r in conn.execute("SELECT * FROM mail_brief_item WHERE brief_id = ?", (bid2,)).fetchall())}
        finally:
            conn.close()
        assert set(v2) == {"Coastline invoice 44817, $7,820.", "Bid on 88 Ocean Pkwy: go or no-go?", "Sign the rope access subcontract."}, set(v2)
        assert v2["Coastline invoice 44817, $7,820."]["group"] == "wait"                                       # sent, still open
        assert v2["Bid on 88 Ocean Pkwy: go or no-go?"]["group"] == "dead"                                    # held a day: its own group
        assert v2["Sign the rope access subcontract."]["group"] == "approve"
        assert _q("SELECT state FROM mail_brief_item WHERE id = ?", (cope,))[0]["state"] == "answered", _q("SELECT state FROM mail_brief_item WHERE id = ?", (cope,))       # the owner wrote back
        assert v2["Coastline invoice 44817, $7,820."]["first_in"] == items["Coastline invoice 44817, $7,820."]["received_at"]
        passed += 1; print("  next morning: still waiting, held one back, owner's answer closes it ok")

        # ---- the clock
        conn = _db()
        try:
            base = datetime(2026, 11, 14, 8, 0)                                                                # a Saturday
            assert gb.tick(conn, base) == 0
            assert gb.tick(conn, datetime(2026, 11, 16, 7, 0)) == 0                                           # Monday, before 7:30
            assert gb.tick(conn, datetime(2026, 11, 16, 7, 31)) == 1
            assert gb.tick(conn, datetime(2026, 11, 16, 9, 0)) == 0                                           # once
        finally:
            conn.close()
        assert c.put("/api/inbox/brief/settings", json={"days": "daily"}).status_code == 200
        conn = _db()
        try:
            assert gb.tick(conn, datetime(2026, 11, 21, 7, 45)) == 1                                          # Saturday, every day
        finally:
            conn.close()
        passed += 1; print("  the clock: ready time, weekdays / every day, once a day ok")

        # ---- the bell
        conn = _db()
        try:
            conn.execute("UPDATE mail_brief SET created_at = ? WHERE brief_date = ? AND user_id = ?",
                         (datetime.now().replace(microsecond=0).isoformat(), "2026-11-21", uid))
            conn.commit()
            activity_feed.ensure(conn)
            feed = activity_feed.build(conn, uid)
        finally:
            conn.close()
        bells = [i for i in feed["items"] if i["kind"] == "brief"]
        assert bells and bells[0]["title"] == "Robin's brief is ready" and bells[0]["href"] == "/company#inbox/brief"
        assert not [i for i in feed["items"] if i["id"] == f"brief:{bid}"]                                    # sent: gone from the bell
        passed += 1; print("  bell ok")

        # ---- who
        sam = app.test_client()
        conn = _db()
        try:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (auth.hash_password(PW), other))
            conn.commit()
        finally:
            conn.close()
        assert sam.post("/api/auth/login", json={"email": f"{TAG}.sam@superstarscontracting.com", "password": PW},
                        environ_base={"REMOTE_ADDR": "10.97.1.5"}).status_code == 200
        assert sam.post(f"/api/inbox/brief/items/{inv}", json={"action": "remove"}).status_code == 404
        assert sam.post(f"/api/inbox/brief/{bid}/send", json={"subject": "x", "body": "y"}).status_code == 404
        assert sam.get("/api/inbox/brief").get_json()["data"]["today"] is None
        pm = app.test_client()
        assert pm.post("/api/auth/login", json={"email": f"{TAG}.pm@superstarscontracting.com", "password": PW},
                       environ_base={"REMOTE_ADDR": "10.97.1.4"}).status_code == 200
        assert pm.get("/api/inbox/brief").status_code == 403
        assert pm.post("/api/inbox/brief/build").status_code == 403
        portal = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        for s in ('data-ibtab="brief"', 'id="ib-brief"', "/inbox/brief/build", "Review email to", "answered</button>", "Tomorrow",
                  "Still waiting on", "taken off today"):
            assert s in portal, s
        page = (ROOT / "account.html").read_text(encoding="utf-8")
        for s in ('id="br-on"', "/api/inbox/brief/settings", "Eastern", "Every day", "Nothing is sent by itself"):
            assert s in page, s
        passed += 1; print("  office only, own brief only, pages ok")
    finally:
        inbox._gmail, inbox.compose = real_gmail, real_compose
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"smoke_owner_brief: {passed} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
