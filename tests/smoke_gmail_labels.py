#!/usr/bin/env python3
"""Labels in Gmail (gmail_labels.py, prototype accepted 2026-11-09).

Proves against an ISOLATED database with a fake Gmail (fictional people only):
  * who the email was for: from / to / cc / other (Bcc, a list); a colleague is "from the team"
  * the sorting prompt names the Cc line, how this mailbox was on the email and the owner picked (names, no addresses)
  * off by default: sorting puts nothing on Gmail; switching on needs Gmail's modify permission (409 without it)
  * on: a copied-only email gets its kind + "FYI (cc)"; an email asking for the owner gets its kind + "For <first name>"
    + "Needs reply"; labels are made once (with Gmail's palette colours), a label of the person's own with the same name
    is used (not doubled), personal mail gets nothing, sent mail gets nothing
  * "Needs reply" comes off when a colleague answers in the conversation, when this mailbox answers, and on Done
  * a new owner renames "For ..." in Gmail; a label deleted in Gmail is made again
  * "Label my last 90 days": older email is stored for sorting only (no "Needs reply" in the app, no Claude cards),
    gets its kind + FYI but never "For ..." / "Needs reply"; new mail is sorted first; progress counts; then done
  * "Remove all labels": the labels the app made are deleted, the person's own label is only taken off the app's emails,
    nothing is left recorded, labels switch off
  * in the app (prototype accepted 2026-11-10), with Gmail labels off: each conversation in the Inbox list carries its
    kind / FYI / "For" labels; the "For" filter and the Label menu narrow the list; a label fixed by hand sticks (the
    sorting leaves it) and reaches Gmail when labels are on; a C-suite person with nobody picked sees "Needs my OK";
    "Sort my last 90 days" runs with the Gmail switch off and fills in who older stored email was for
  * routes are admin / C-suite only; the account page has the card; the Inbox has the filter, menu and fix
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
_TMP = tempfile.mkdtemp(prefix="ssc_lbl_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(Path(_TMP) / "mail"), "SSC_GMAIL_POLL": "0",
                   "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key()})
for _k in ("ANTHROPIC_API_KEY", "SSC_ASSISTANT_FAKE", "SSC_INBOX_TRIAGE_FAKE", "SSC_ASSISTANT_DAILY_CAP_USD",
           "SSC_ASSISTANT_MONTHLY_BUDGET_USD", "SSC_ASSISTANT_USER_DAILY_USD", "GMAIL_PUBSUB_TOPIC"):
    os.environ.pop(_k, None)

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import db_layer  # noqa: E402
import gmail_labels as gl  # noqa: E402
import inbox  # noqa: E402

TAG = "lblsm"
PW = "LabelSmokePassword2026"
ME = f"{TAG}.admin@superstarscontracting.com"
MATE = f"Kim Lblsmoke <{TAG}.kim@superstarscontracting.com>"
DANA = "Dana Lblsmoke <dana@lblsmoke-harbor.example>"
NOW = time.time()
FULL = "openid email " + inbox.SEND_SCOPE + " " + inbox.MODIFY_SCOPE


def _db():
    return db_layer.connect(pragma_fk=True)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


GMAIL: dict = {}
LABELS: dict = {"INBOX": {"id": "INBOX", "name": "INBOX", "type": "system"},
                "Label_own_cq": {"id": "Label_own_cq", "name": "Client question", "type": "user"}}
CALLS: list = []
LIST_IDS: list = []
SEQ = {"n": 0}


def gmsg(gid, thread, minutes_ago, text, labels=("INBOX", "UNREAD"), frm=DANA, to=ME, cc=""):
    hs = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": "Lbl smoke " + thread},
          {"name": "Message-ID", "value": f"<{gid}@lblsmoke.example>"}]
    if cc:
        hs.append({"name": "Cc", "value": cc})
    m = {"id": gid, "threadId": thread, "labelIds": list(labels), "internalDate": str(int((NOW - minutes_ago * 60) * 1000)),
         "snippet": text[:80], "payload": {"mimeType": "multipart/alternative", "headers": hs, "parts": [
             {"mimeType": "text/plain", "body": {"data": _b64(text)}}]}}
    GMAIL[gid] = m
    return m


def fake_gmail(conn, link, method, path, **kw):
    CALLS.append((method, path, kw.get("json_body")))
    b = kw.get("json_body") or {}
    if method == "GET" and path == "/labels":
        return 200, {"labels": [dict(v) for v in LABELS.values()]}
    if method == "POST" and path == "/labels":
        if any(v["name"].lower() == b["name"].lower() for v in LABELS.values()):
            return 409, {}
        SEQ["n"] += 1
        lid = f"Label_app_{SEQ['n']}"
        LABELS[lid] = {"id": lid, "name": b["name"], "type": "user", "color": b.get("color")}
        return 200, dict(LABELS[lid])
    if method == "PATCH" and path.startswith("/labels/"):
        lid = path.split("/")[2]
        if lid not in LABELS:
            return 404, {}
        LABELS[lid]["name"] = b["name"]
        return 200, dict(LABELS[lid])
    if method == "DELETE" and path.startswith("/labels/"):
        lid = path.split("/")[2]
        LABELS.pop(lid, None)
        for m in GMAIL.values():
            m["labelIds"] = [x for x in m["labelIds"] if x != lid]
        return 204, {}
    if method == "POST" and path == "/messages/batchModify":
        if any(x not in LABELS and x not in ("INBOX", "UNREAD") for x in b["addLabelIds"] + b["removeLabelIds"]):
            return 400, {}
        for g in b["ids"]:
            if g in GMAIL:
                GMAIL[g]["labelIds"] = [x for x in GMAIL[g]["labelIds"] if x not in b["removeLabelIds"]] + \
                    [x for x in b["addLabelIds"] if x not in GMAIL[g]["labelIds"]]
        return 204, {}
    if method == "GET" and path.startswith("/messages?q="):
        return 200, {"messages": [{"id": g} for g in LIST_IDS]}
    if method == "GET" and path.startswith("/messages/") and "format=full" in path:
        g = path.split("/")[2].split("?")[0]
        return (200, json.loads(json.dumps(GMAIL[g]))) if g in GMAIL else (404, {})
    if path.startswith("/threads/"):
        return 404, {}
    return 500, {}


def names_on(gid) -> set:
    return {LABELS[x]["name"] for x in GMAIL[gid]["labelIds"] if x in LABELS and LABELS[x]["type"] == "user"}


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
        for t in ("gmail_link", "mail_sendas", "mail_sync", "mail_label_pref", "mail_label_map"):
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


def _plant(kind, needs, owner=False, business=True):
    os.environ["SSC_INBOX_TRIAGE_FAKE"] = json.dumps({"business": business, "kind": kind, "summary": "Dana writes",
                                                      "needs_action": needs, "for_owner": owner})
    os.environ["SSC_ASSISTANT_FAKE"] = json.dumps([{"text": "Nothing to propose."}])
    ca._FAKE["step"] = 0


def _take(uid, msg):
    conn = _db()
    try:
        mid = inbox.ingest(conn, uid, msg, ME)
        inbox.process(conn, mid)
        return mid
    finally:
        conn.close()


def _user(conn, email, role, name):
    conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                 "VALUES (?, ?, ?, ?, 1, 'active', 0, 0)", (email, auth.hash_password(PW), role, name))
    conn.commit()
    return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


def main() -> int:
    _cleanup()
    passed = 0

    # ---- who the email was for
    assert gl.addressed(ME, DANA, ME, "") == "to"
    assert gl.addressed(ME, DANA, "Arch <a@x.example>", f"Me <{ME}>") == "cc"
    assert gl.addressed(ME, DANA, "team@x.example", "") == "other"
    assert gl.addressed(ME, f"Me <{ME}>", DANA, "") == "from"
    assert gl.from_team(ME, MATE) and not gl.from_team(ME, DANA)
    passed += 1; print("  who the email was for ok")

    conn = _db()
    try:
        uid = _user(conn, ME, "admin", "Lbl Admin")
        owner_id = _user(conn, f"{TAG}.robin@superstarscontracting.com", "c_suite", "Robin Lblsmoke")
        _user(conn, f"{TAG}.pm@superstarscontracting.com", "pm", "Pat Lblsmoke")
        conn.execute("INSERT INTO gmail_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) VALUES (?,?,?, 'active', ?, ?)",
                     (uid, ME, secret_box.encrypt_text("r"), "2026-11-09T08:00:00", "openid email " + inbox.SEND_SCOPE))
        conn.commit()
    finally:
        conn.close()

    real = inbox._gmail
    inbox._gmail = fake_gmail
    app = Flask("lblsm", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    ca.register(app)
    inbox.register(app)
    gl.register(app)
    try:
        c = app.test_client()
        assert c.post("/api/auth/login", json={"email": ME, "password": PW}, environ_base={"REMOTE_ADDR": "10.98.1.3"}).status_code == 200

        # ---- off by default; on needs Gmail's modify permission
        d = c.get("/api/inbox/labels").get_json()["data"]
        assert d["enabled"] is False and d["can_label"] is False and d["owner"] is None
        assert [x["name"] for x in d["labels"]][:2] == ["For the owner", "Needs reply"] and d["labels"][-1]["name"] == "FYI (cc)"
        assert [x["name"] for x in d["owner_choices"]] == ["Robin Lblsmoke"]          # admin / C-suite, not me, not the PM
        _plant("scheduling", False)
        a0 = _take(uid, gmsg("a0", "lbl-a0", 40, "Crane booked for Tuesday."))
        assert not [x for x in CALLS if x[1].startswith("/labels") or x[1] == "/messages/batchModify"]
        assert _row("a0")["gmail_labels"] is None and _row("a0")["kind"] == "scheduling"
        assert c.put("/api/inbox/labels", json={"enabled": True}).status_code == 409
        conn = _db()
        try:
            conn.execute("UPDATE gmail_link SET scopes = ? WHERE user_id = ?", (FULL, uid))
            conn.commit()
        finally:
            conn.close()
        assert c.put("/api/inbox/labels", json={"owner_user_id": 99999}).status_code == 400
        r = c.put("/api/inbox/labels", json={"enabled": True, "owner_user_id": owner_id})
        assert r.status_code == 200 and r.get_json()["data"]["owner"]["first"] == "Robin"
        assert r.get_json()["data"]["labels"][0]["name"] == "For Robin"
        time.sleep(0.6)   # switching on labels what is already sorted (in the background)
        conn = _db()
        try:
            gl.relabel_sorted(conn, uid)
        finally:
            conn.close()
        assert names_on("a0") == {"Scheduling", "Needs reply"} or names_on("a0") == {"Scheduling"}, names_on("a0")
        passed += 1; print("  off by default, on with Gmail's permission ok")

        # ---- the prompt
        conn = _db()
        try:
            m = inbox.message(conn, inbox.ingest(conn, uid, gmsg("p1", "lbl-p", 30, "Please confirm.", to="Arch <a@x.example>", cc=ME), ME))
        finally:
            conn.close()
        p = inbox.triage_prompt(m)
        assert "Cc: " + ME in p and "only copied (Cc)" in p and "Robin Lblsmoke" in p and "for_owner" in p
        assert "for_owner" in inbox.TRIAGE_SCHEMA["required"]
        passed += 1; print("  sorting prompt: Cc, how this mailbox was on it, the owner ok")

        # ---- copied only: kind + FYI
        _plant("scheduling", False)
        conn = _db()
        try:
            inbox.process(conn, m["id"])
        finally:
            conn.close()
        assert names_on("p1") == {"Scheduling", "FYI (cc)"}, names_on("p1")
        assert _row("p1")["gmail_labels"] == "scheduling,fyi" and _row("p1")["addressed"] == "cc"
        made = {v["name"]: v for v in LABELS.values() if v["id"].startswith("Label_app_")}
        assert made["FYI (cc)"]["color"] == {"backgroundColor": "#efefef", "textColor": "#666666"}
        passed += 1; print("  copied only: kind + FYI (cc) ok")

        # ---- for the owner + needs reply; own same-name label used, not doubled
        _plant("client_question", True, owner=True)
        b1 = _take(uid, gmsg("b1", "lbl-b", 25, "Can we add the coping repair? Needs Robin's OK."))
        assert names_on("b1") == {"Client question", "For Robin", "Needs reply"}, names_on("b1")
        assert "Label_own_cq" in GMAIL["b1"]["labelIds"]
        assert sum(1 for v in LABELS.values() if v["name"] == "Client question") == 1
        assert _q("SELECT created FROM mail_label_map WHERE user_id = ? AND label_key = 'client_question'", (uid,))[0]["created"] == 0
        _plant("payment", True, business=False)                                         # personal mail: nothing
        _take(uid, gmsg("x1", "lbl-x", 24, "Your gym receipt."))
        assert names_on("x1") == set()
        conn = _db()
        try:
            inbox.ingest(conn, uid, gmsg("s1", "lbl-s", 23, "Sent note.", labels=("SENT",), frm=ME, to=DANA), ME)
            gl.relabel(conn, uid, [_row("s1")["id"]])
        finally:
            conn.close()
        assert names_on("s1") == set()                                                   # sent mail: nothing
        passed += 1; print("  for the owner + needs reply, own label used, personal / sent mail left alone ok")

        # ---- needs reply comes off: a colleague answers / this mailbox answers / Done
        conn = _db()
        try:
            inbox.ingest(conn, uid, gmsg("b2", "lbl-b", 10, "Done, sent the change order.", frm=MATE, to=DANA, cc=ME), ME)
        finally:
            conn.close()
        assert names_on("b1") == {"Client question", "For Robin"}, names_on("b1")
        _plant("scheduling", True)
        _take(uid, gmsg("c1", "lbl-c", 20, "When is the walkthrough?"))
        assert "Needs reply" in names_on("c1")
        conn = _db()
        try:
            inbox.ingest(conn, uid, gmsg("c2", "lbl-c", 5, "Thursday 9 AM.", labels=("SENT",), frm=ME, to=DANA), ME)
        finally:
            conn.close()
        assert names_on("c1") == {"Scheduling"}, names_on("c1")
        _plant("documents", True)
        e1 = _take(uid, gmsg("e1", "lbl-e", 15, "Here is the COI, please confirm."))
        assert "Needs reply" in names_on("e1")
        assert c.post(f"/api/inbox/messages/{e1}/done", json={"done": True}).status_code == 200
        assert names_on("e1") == {"Documents"}, names_on("e1")
        passed += 1; print("  needs reply comes off: colleague answer, own answer, Done ok")

        # ---- a new owner renames the label; a label deleted in Gmail is made again
        owner2 = None
        conn = _db()
        try:
            owner2 = _user(conn, f"{TAG}.sam@superstarscontracting.com", "admin", "Sam Lblsmoke")
        finally:
            conn.close()
        assert c.put("/api/inbox/labels", json={"owner_user_id": owner2}).status_code == 200
        conn = _db()
        try:
            gl.relabel(conn, uid, [b1])
            gl.label_ids(conn, uid, inbox._link(conn, uid), {"owner"})
        finally:
            conn.close()
        assert "For Sam" in names_on("b1") and not any(v["name"] == "For Robin" for v in LABELS.values())
        fyi_id = _q("SELECT gmail_label_id FROM mail_label_map WHERE user_id = ? AND label_key = 'fyi'", (uid,))[0]["gmail_label_id"]
        LABELS.pop(fyi_id)
        _plant("vendor", False)
        _take(uid, gmsg("v1", "lbl-v", 12, "Price list attached.", to="Ops <ops@x.example>", cc=ME))
        assert names_on("v1") == {"Vendor", "FYI (cc)"}, names_on("v1")
        passed += 1; print("  new owner renames, deleted label made again ok")

        # ---- "Label my last 90 days"
        gmsg("o1", "lbl-o1", 60 * 24 * 20, "Old: can Robin sign the CO?")                       # 20 days ago, to me
        gmsg("o2", "lbl-o2", 60 * 24 * 40, "Old: FYI the shed is down.", to="Ops <ops@x.example>", cc=ME)
        LIST_IDS[:] = ["o1", "o2", "b1"]                                                       # b1 is already here
        conn = _db()
        try:
            assert gl.start_backfill(conn, uid)
        finally:
            conn.close()
        for _ in range(50):
            if not gl._RUNNING.get(uid):
                break
            time.sleep(0.1)
        o1 = _row("o1")
        assert o1["ai_state"] == "backfill" and o1["backfilled_at"] and o1["needs_action"] == 0
        d = c.get("/api/inbox/labels").get_json()["data"]["backfill"]
        assert d["state"] == "sorting" and d["total"] == 2 and d["left"] == 2
        _plant("proposal_reply", True, owner=True)
        conn = _db()
        try:
            n1 = inbox.ingest(conn, uid, gmsg("n1", "lbl-n", 1, "New today."), ME)
            import gmail_suggest
            real_ur, gmail_suggest.unavailable_reason = gmail_suggest.unavailable_reason, lambda: None
            try:
                inbox.claude_round()                                                            # new mail first, then older
            finally:
                gmail_suggest.unavailable_reason = real_ur
        finally:
            conn.close()
        assert _row("n1")["ai_state"] in ("triaged", "suggested") and n1
        assert _row("o1")["ai_state"] == "triaged" and _row("o1")["needs_action"] == 0 and _row("o1")["triage_needs"] == 1
        assert names_on("o1") == {"Proposal reply"}, names_on("o1")                             # never For / Needs reply
        assert names_on("o2") == {"Proposal reply"}, names_on("o2")                             # it asked, so not FYI
        assert not _q("SELECT a.id FROM claude_action a JOIN claude_chat ch ON ch.id = a.chat_id JOIN mail_message x ON x.id = ch.mail_id "
                      "WHERE x.gmail_id IN ('o1', 'o2')")
        d = c.get("/api/inbox/labels").get_json()["data"]["backfill"]
        assert d["state"] == "done" and d["sorted"] == 2 and d["left"] == 0
        passed += 1; print("  label my last 90 days ok")

        # ---- remove all
        mine_before = dict(LABELS["Label_own_cq"])
        r = c.post("/api/inbox/labels/remove")
        assert r.status_code == 200, r.get_json()
        assert not [v for v in LABELS.values() if v["id"].startswith("Label_app_")], [(v["name"], [c for c in CALLS if v["id"] in json.dumps(c)][:3]) for v in LABELS.values() if v["id"].startswith("Label_app_")]
        assert LABELS["Label_own_cq"] == mine_before and "Label_own_cq" not in GMAIL["b1"]["labelIds"]
        assert not _q("SELECT id FROM mail_message WHERE user_id = ? AND gmail_labels IS NOT NULL", (uid,))
        assert not _q("SELECT id FROM mail_label_map WHERE user_id = ?", (uid,))
        assert r.get_json()["data"]["enabled"] is False
        n = len(CALLS)
        _plant("vendor", False)
        _take(uid, gmsg("z1", "lbl-z", 1, "After removal.", to="Ops <ops@x.example>", cc=ME))
        assert not [x for x in CALLS[n:] if x[1].startswith("/labels") or x[1] == "/messages/batchModify"]
        passed += 1; print("  remove all labels ok")

        # ---- labels in the app (Gmail labels are off from here on)
        def lst(q=""):
            j = c.get("/api/inbox/messages?" + q).get_json()
            return j, {r["id"]: r for r in j["data"]}
        j, rows = lst("filter=all")
        assert j["label_names"]["owner"] == "For Sam" and j["label_names"]["permit_notice"] == "Permit & notice"
        rid = lambda gid: _row(gid)["id"]
        b_row = rid("b2")                                                                       # the conversation's newest email
        assert rows[rid("p1")]["labels"]["kind"] == "scheduling" and rows[rid("p1")]["labels"]["fyi"] is True
        assert rows[b_row]["labels"]["owner"] is True and rows[b_row]["labels"]["kind"] == "proposal_reply"   # newest sorted email
        assert rows[b_row]["labels"]["mid"] == b_row and rows[rid("o1")]["labels"]["owner"] is False   # older mail: never "For"
        _j, own = lst("filter=own")
        assert set(own) == {b_row, rid("n1")}, set(own)
        _j, fyi = lst("filter=all&label=fyi")
        assert {rid("p1"), rid("z1")} <= set(fyi) and b_row not in fyi
        _j, sch = lst("filter=all&label=scheduling")
        assert rid("a0") in sch and rid("p1") in sch and rid("b2") not in sch
        n = len(CALLS)
        assert c.post(f"/api/inbox/messages/{a0}/label", json={"kind": "nope"}).status_code == 400
        assert c.post(f"/api/inbox/messages/{a0}/label", json={}).status_code == 400
        assert c.post("/api/inbox/messages/999999/label", json={"kind": "payment"}).status_code == 404
        r = c.post(f"/api/inbox/messages/{a0}/label", json={"kind": "payment"})
        assert r.status_code == 200 and r.get_json()["data"]["kind"] == "payment"
        assert _row("a0")["label_manual"] == 1 and _row("a0")["kind"] == "payment"
        _plant("scheduling", True)
        conn = _db()
        try:
            inbox.process(conn, a0, force=True)                                                 # Claude sorts it again
        finally:
            conn.close()
        assert _row("a0")["kind"] == "payment"                                                   # the fix by hand stays
        assert c.post(f"/api/inbox/messages/{a0}/label", json={"owner": True}).get_json()["data"]["owner"] is True
        assert c.post(f"/api/inbox/messages/{b_row}/label", json={"owner": False}).get_json()["data"]["owner"] is False
        _j, own = lst("filter=own")
        assert set(own) == {rid("a0"), rid("n1")}, set(own)
        assert not [x for x in CALLS[n:] if x[1].startswith("/labels") or x[1] == "/messages/batchModify"]   # Gmail off: untouched
        t = c.get(f"/api/inbox/threads/{a0}").get_json()["data"]
        assert t["labels"]["kind"] == "payment" and t["labels"]["owner"] is True and t["label_names"]["owner"] == "For Sam"
        # "Sort my last 90 days" with the Gmail switch off: who older stored email was for is filled in
        conn = _db()
        try:
            conn.execute("UPDATE mail_message SET addressed = NULL WHERE gmail_id = 'p1'")
            conn.commit()
        finally:
            conn.close()
        LIST_IDS[:] = ["p1"]
        assert c.post("/api/inbox/labels/backfill").status_code == 202
        for _ in range(50):
            if not gl._RUNNING.get(uid):
                break
            time.sleep(0.1)
        assert _row("p1")["addressed"] == "cc"
        # with Gmail labels on, a fix by hand reaches Gmail
        assert c.put("/api/inbox/labels", json={"enabled": True}).status_code == 200
        time.sleep(0.6)
        assert c.post(f"/api/inbox/messages/{a0}/label", json={"kind": "vendor"}).status_code == 200
        assert "Vendor" in names_on("a0") and "Payment" not in names_on("a0"), names_on("a0")
        # a C-suite person who picked nobody: "Needs my OK"
        boss = app.test_client()
        assert boss.post("/api/auth/login", json={"email": f"{TAG}.robin@superstarscontracting.com", "password": PW},
                         environ_base={"REMOTE_ADDR": "10.98.1.5"}).status_code == 200
        assert boss.get("/api/inbox/labels").get_json()["data"]["labels"][0]["name"] == "Needs my OK"
        assert boss.post(f"/api/inbox/messages/{a0}/label", json={"kind": "payment"}).status_code == 404   # not their email
        passed += 1; print("  labels in the app: list, filters, fix by hand, Needs my OK, sort with Gmail off ok")

        # ---- office only; the page
        pm = app.test_client()
        assert pm.post("/api/auth/login", json={"email": f"{TAG}.pm@superstarscontracting.com", "password": PW},
                       environ_base={"REMOTE_ADDR": "10.98.1.4"}).status_code == 200
        assert pm.get("/api/inbox/labels").status_code == 403
        assert pm.post("/api/inbox/labels/backfill").status_code == 403
        page = (ROOT / "account.html").read_text(encoding="utf-8")
        for s in ('id="lb-card"', "/api/inbox/labels", "Sort my last", "Remove all labels", "Keep them", 'class="btn-primary"',
                  "Email labels", "in your Inbox"):
            assert s in page, s
        portal = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        for s in ('data-ibf="own"', 'id="ib-lsel"', "/label'", "ib-lmenu", "None of these", "ib-lbl own", "ib-lbl fyi"):
            assert s in portal, s
        assert pm.post(f"/api/inbox/messages/{a0}/label", json={"kind": "payment"}).status_code == 403
        passed += 1; print("  office only + account page ok")
    finally:
        inbox._gmail = real
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"smoke_gmail_labels: {passed} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
