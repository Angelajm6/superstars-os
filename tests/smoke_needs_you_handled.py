#!/usr/bin/env python3
"""Needs you clears itself when the team finishes the work (team_handled.py, 2026-11-03).

Proves against an ISOLATED database (fictional people only):
  * the same email in two mailboxes is recognized by its fingerprint, including emails stored before the column existed
  * a draft started from a teammate's copy moves the card to Today; sending it closes the card, onto Handled
  * the wording says what happened and names no teammate
  * an older proposal at the same address never closes the card, even one filed on this email
  * a proposal marked as sent (noon that day) after the email still closes it; an old one adopted today doesn't
  * not linked but same building: "Same request?". No keeps it open, Yes closes it
  * Reopen brings a card back and that proposal never closes it again
  * a teammate's reply withdraws a reply draft; a proposal request gets the question instead
  * approving a stale card creates nothing (409)
  * a reply from Studio links the new proposal even when the email already named an older one
  * only the card's owner can answer; other roles get 403
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
_TMP = tempfile.mkdtemp(prefix="ssc_nyh_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(Path(_TMP) / "mail"), "SSC_GMAIL_POLL": "0",
                   "SSC_REMINDERS": "0", "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(), "SSC_ASSISTANT_FAKE": "1"})

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import inbox  # noqa: E402
import proposal_template  # noqa: E402
import proposals  # noqa: E402
import proposal_studio  # noqa: E402
import assistant  # noqa: E402
import team_handled  # noqa: E402
import today_feed  # noqa: E402

TAG = "nyh"
PW = "NeedsYouSmokePassword2026"
ME = f"{TAG}.me@superstarscontracting.com"
MATE = f"{TAG}.mate@superstarscontracting.com"
CLIENT = "Nyh Smoke Northbeam Engineers"
NOW = time.time()


def _db():
    return db_layer.connect(pragma_fk=True)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def gmsg(gid, thread, rfc, minutes_ago, labels=("INBOX", "UNREAD"), frm="Dana Nyhsmoke <dana@nyh-north.example>", to=ME):
    hs = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": "Nyh smoke " + rfc},
          {"name": "Message-ID", "value": f"<{rfc}@nyh.example>"}]
    return {"id": gid, "threadId": thread, "labelIds": list(labels), "internalDate": str(int((NOW - minutes_ago * 60) * 1000)),
            "snippet": "Please send a proposal", "payload": {"mimeType": "text/plain", "headers": hs, "body": {"data": _b64("Please send a proposal")}}}


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        for m in (ca, inbox, today_feed):
            m.ensure(conn)
        proposals.ensure_schema(conn)
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
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph(ests)})", ests).fetchall()]
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate_ira", "estimate_id", ests)
        purge(conn, "estimate", "id", ests)
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM building WHERE address_line LIKE '%Nyh Terrace%'")
        conn.execute("DELETE FROM crm_organization WHERE name = ?", (CLIENT,))
        conn.commit()
        purge_users(conn, uids)
    finally:
        conn.close()


def _users():
    conn = _db()
    try:
        ids = {}
        for key, role in (("me", "admin"), ("mate", "admin"), ("pm", "pm")):
            conn.execute("INSERT INTO users (email, password_hash, full_name, role, status, must_reset_password, created_at) "
                         "VALUES (?,?,?,?,'active',0,?)", (f"{TAG}.{key}@superstarscontracting.com", auth.hash_password(PW), f"Nyh {key.title()}", role,
                                                           datetime.now().isoformat(timespec="seconds")))
            conn.commit()
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.{key}@superstarscontracting.com",)).fetchone()["id"]
        ids["org"] = crm.create_org(conn, name=CLIENT, relationship_type="client", created_by=ids["me"])
        return ids
    finally:
        conn.close()


def _card(conn, uid, mid, tool="draft_proposal", addr="118 Nyh Terrace, New York, NY", org=None) -> int:
    m = inbox.message(conn, mid)
    chat = inbox._mail_chat(conn, m)
    inp = {"mail_id": mid, "client_org_id": org, "building_address": addr, "what": "Rope access inspection of the north elevation only."}
    if tool == "reply_email":
        inp = {"mail_id": mid, "body": "Hi Dana, thanks for the note."}
    conn.execute("INSERT INTO claude_action (chat_id, message_id, tool_use_id, user_id, tool, input, summary, status, created_at) "
                 "VALUES (?, 0, ?, ?, ?, ?, 'Card', 'pending', ?)", (chat, f"tu-{mid}-{tool}", uid, tool, json.dumps(inp),
                                                                     datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return conn.execute("SELECT MAX(id) AS m FROM claude_action WHERE user_id = ?", (uid,)).fetchone()["m"]


def _proposal(conn, uid, org, addr="118 Nyh Terrace, New York, NY", status="draft", sent=None, made=None, source=None) -> int:
    est = estimates.create_estimate(conn, est_type="FR", borough="MN", client_org_id=org, building_address=addr, created_by=uid)
    pid = proposals.create_from_estimate(conn, est["id"], created_by=uid)["id"]
    conn.execute("UPDATE proposal SET status = ?, sent_at = ?, source_mail_id = ?, created_at = COALESCE(?, created_at) WHERE id = ?",
                 (status, sent, source, made, pid))
    conn.commit()
    return pid


def _set(conn, pid, status, sent) -> None:
    conn.execute("UPDATE proposal SET status = ?, sent_at = ? WHERE id = ?", (status, sent, pid))
    conn.commit()


def _ago(**kw) -> str:
    return (datetime.now() - timedelta(**kw)).isoformat(timespec="seconds")


def main() -> int:
    app = Flask("nyh", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    ca.register(app)
    inbox.register(app)
    today_feed.register(app)
    proposal_template.register(app)
    proposals.register(app)
    proposal_studio.register(app)
    _cleanup()
    ids = _users()

    def login(key):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@superstarscontracting.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.96.0.{len(key)}"})
        assert r.status_code == 200, (key, r.status_code)
        return c

    me, mate, pm = login("me"), login("mate"), login("pm")

    def today():
        d = me.get("/api/company/portal/today").get_json()["data"]
        return {c["key"]: c for c in d["needs"]}, d["handled"]

    def status_of(conn, aid):
        return conn.execute("SELECT status FROM claude_action WHERE id = ?", (aid,)).fetchone()["status"]

    def same_email(conn, rfc, minutes_ago=60):
        """One email in both mailboxes, under each mailbox's own thread id."""
        mine = inbox.ingest(conn, ids["me"], gmsg(f"g-me-{rfc}", f"t-me-{rfc}", rfc, minutes_ago), ME)
        theirs = inbox.ingest(conn, ids["mate"], gmsg(f"g-mate-{rfc}", f"t-mate-{rfc}", rfc, minutes_ago, to=MATE), MATE)
        return mine, theirs

    passed = 0
    conn = _db()
    try:
        # ---- one email, two mailboxes
        mine, theirs = same_email(conn, "a1")
        a, b = [conn.execute("SELECT rfc_hash, thread_id FROM mail_message WHERE id = ?", (i,)).fetchone() for i in (mine, theirs)]
        assert a["rfc_hash"] and a["rfc_hash"] == b["rfc_hash"] and a["thread_id"] != b["thread_id"]
        conn.execute("UPDATE mail_message SET rfc_hash = NULL WHERE id = ?", (theirs,))
        conn.commit()
        assert team_handled.backfill(conn) >= 1
        assert conn.execute("SELECT rfc_hash FROM mail_message WHERE id = ?", (theirs,)).fetchone()["rfc_hash"] == a["rfc_hash"]
        passed += 1; print("  fingerprint + backfill ok")

        # ---- a teammate writes it from their copy, then sends it
        aid = _card(conn, ids["me"], mine, org=ids["org"])
        cards, handled = today()
        assert cards[f"claude:{aid}"]["urgency"] == "now" and cards[f"claude:{aid}"]["actions"][0]["type"] == "studio" and not handled
        pid = _proposal(conn, ids["mate"], ids["org"], source=theirs)
        cards, _ = today()
        c = cards[f"claude:{aid}"]
        assert c["urgency"] == "today" and c["pill"] == "Being written" and c["href"] == f"/proposal-studio#p/{pid}"
        assert c["actions"][0]["label"] == "See the draft" and not c["draft"]
        _set(conn, pid, "sent", _ago(minutes=1))
        cards, handled = today()
        assert f"claude:{aid}" not in cards and status_of(conn, aid) == "declined"
        h = [x for x in handled if x["action_id"] == aid][0]
        assert f"was sent to {CLIENT}" in h["note"] and h["title"].startswith("Dana Nyhsmoke asked for a proposal: 118 Nyh Terrace")
        blob = json.dumps(h)
        assert "Nyh Mate" not in blob and "nyh.mate" not in blob, "names no teammate and shows none of their mail"
        passed += 1; print("  draft -> Being written, sent -> Handled, no names ok")

        # ---- Reopen sticks
        assert me.post("/api/company/portal/today/handled", json={"action_id": aid, "answer": "reopen"}).status_code == 200
        cards, handled = today()
        assert cards[f"claude:{aid}"]["pill"] == "Reopened by you" and cards[f"claude:{aid}"]["urgency"] == "now"
        assert not [x for x in handled if x["action_id"] == aid] and status_of(conn, aid) == "pending"
        conn.execute("UPDATE claude_action SET status = 'declined' WHERE id = ?", (aid,))
        conn.commit()
        passed += 1; print("  reopen sticks ok")

        # ---- an older proposal at the address never counts, even one filed on this email
        m2, _ = same_email(conn, "a2")
        addr2 = "22 Nyh Terrace, New York, NY"
        older = _proposal(conn, ids["mate"], ids["org"], addr=addr2, status="sent", sent=_ago(days=3), made=_ago(days=4))
        conn.execute("INSERT INTO mail_link (message_id, entity_type, entity_id, linked_by, linked_at) VALUES (?, 'proposal', ?, ?, ?)",
                     (m2, str(older), ids["me"], _ago(minutes=1)))
        conn.commit()
        a2 = _card(conn, ids["me"], m2, addr=addr2, org=ids["org"])
        cards, _ = today()
        c = cards[f"claude:{a2}"]
        assert c["urgency"] == "now" and c["actions"][0]["type"] == "studio" and "was sent before this email" in c["checked"], c
        passed += 1; print("  older proposal stays out ok")

        # ---- marked as sent reads noon that day: made after the email it counts; adopted from before the app it doesn't
        m3, t3 = same_email(conn, "a3")
        addr3 = "33 Nyh Terrace, New York, NY"
        a3 = _card(conn, ids["me"], m3, addr=addr3, org=ids["org"])
        day = datetime.now().date().isoformat()
        p3 = _proposal(conn, ids["mate"], ids["org"], addr=addr3, source=t3)
        _set(conn, p3, "sent", day + "T00:00:00")
        cards, _ = today()
        assert f"claude:{a3}" not in cards and status_of(conn, a3) == "declined"
        m3b, _ = same_email(conn, "a3b")
        addr3b = "34 Nyh Terrace, New York, NY"
        a3b = _card(conn, ids["me"], m3b, addr=addr3b, org=ids["org"])
        _proposal(conn, ids["mate"], ids["org"], addr=addr3b, status="sent", sent=_ago(days=30))
        cards, _ = today()
        assert cards[f"claude:{a3b}"]["pill"] == "Proposal requested"
        passed += 1; print("  mark as sent and adopted proposals ok")

        # ---- not linked, same building: a person decides
        m4, _ = same_email(conn, "a4")
        addr4 = "44 Nyh Terrace, New York, NY"
        a4 = _card(conn, ids["me"], m4, addr=addr4, org=ids["org"])
        p4 = _proposal(conn, ids["mate"], ids["org"], addr="44 NYH TERRACE", status="sent", sent=_ago(minutes=2))
        label = proposals._label(proposals.get(conn, p4))
        cards, _ = today()
        c = cards[f"claude:{a4}"]
        assert c["pill"] == "Same request?" and label in c["why"] and len(c["compare"]) == 2 and status_of(conn, a4) == "pending"
        assert [x["answer"] for x in c["actions"]] == ["yes", "no"] and c["actions"][1]["show"]
        assert me.post("/api/company/portal/today/handled", json={"action_id": a4, "answer": "no"}).status_code == 200
        cards, _ = today()
        assert cards[f"claude:{a4}"]["actions"][0]["type"] == "studio" and f"You said Proposal {label}" in cards[f"claude:{a4}"]["checked"]
        p4b = _proposal(conn, ids["mate"], ids["org"], addr=addr4, status="sent", sent=_ago(minutes=1))
        cards, _ = today()
        assert proposals._label(proposals.get(conn, p4b)) in cards[f"claude:{a4}"]["why"], "a different proposal is a new question"
        assert me.post("/api/company/portal/today/handled", json={"action_id": a4, "answer": "yes"}).status_code == 200
        cards, handled = today()
        assert f"claude:{a4}" not in cards and "You confirmed" in [x for x in handled if x["action_id"] == a4][0]["note"]
        passed += 1; print("  same request? yes / no ok")

        # ---- a teammate replied
        m5, t5 = same_email(conn, "a5")
        a5r = _card(conn, ids["me"], m5, tool="reply_email")
        cards, _ = today()
        assert f"claude:{a5r}" in cards
        inbox.ingest(conn, ids["mate"], gmsg("g-mate-a5-out", "t-mate-a5", "a5-out", 5, labels=("SENT",), frm=MATE, to="dana@nyh-north.example"), MATE)
        a5p = _card(conn, ids["me"], m5, addr="55 Nyh Terrace, New York, NY", org=ids["org"])
        cards, handled = today()
        assert f"claude:{a5r}" not in cards and status_of(conn, a5r) == "declined"
        assert [x for x in handled if x["action_id"] == a5r][0]["note"] == "Answered in the conversation"
        c = cards[f"claude:{a5p}"]
        assert c["pill"] == "Same request?" and c["why"].startswith("A reply went out") and len(c["compare"]) == 1
        passed += 1; print("  teammate reply ok")

        # ---- approving a stale card starts nothing
        m6, t6 = same_email(conn, "a6")
        addr6 = "66 Nyh Terrace, New York, NY"
        a6 = _card(conn, ids["me"], m6, addr=addr6, org=ids["org"])
        p6 = _proposal(conn, ids["mate"], ids["org"], addr=addr6, source=t6)
        _set(conn, p6, "sent", _ago(minutes=1))
        before = conn.execute("SELECT COUNT(*) AS n FROM proposal").fetchone()["n"]
        r = me.post(f"/api/claude/actions/{a6}/approve", json={"values": {}})
        assert r.status_code == 409 and "was sent to" in json.dumps(r.get_json()), r.get_json()
        assert conn.execute("SELECT COUNT(*) AS n FROM proposal").fetchone()["n"] == before and status_of(conn, a6) == "declined"
        passed += 1; print("  stale approve refused ok")

        # ---- a reply from Studio files the new proposal even when the email named an older one
        m7, t7 = same_email(conn, "a7")
        old7 = _proposal(conn, ids["mate"], ids["org"], addr="77 Nyh Terrace", status="sent", sent=_ago(days=5), made=_ago(days=6))
        new7 = _proposal(conn, ids["mate"], ids["org"], addr="77 Nyh Terrace")
        conn.execute("UPDATE mail_message SET proposal_id = ? WHERE id = ?", (old7, t7))
        conn.commit()
        inbox.link_record(conn, t7, "proposal", new7, ids["mate"])
        inbox.link_record(conn, t7, "proposal", new7, ids["mate"])
        n = conn.execute("SELECT COUNT(*) AS n FROM mail_link WHERE message_id = ? AND entity_type = 'proposal' AND entity_id = ?", (t7, str(new7))).fetchone()["n"]
        assert n == 1
        a7 = _card(conn, ids["me"], m7, addr="77 Nyh Terrace", org=ids["org"])
        _set(conn, new7, "sent", _ago(seconds=1))
        cards, _ = today()
        assert f"claude:{a7}" not in cards, "the new proposal closed it across mailboxes"
        passed += 1; print("  studio reply links the new proposal ok")

        # ---- access
        m8, _ = same_email(conn, "a8")
        a8 = _card(conn, ids["me"], m8, addr="88 Nyh Terrace", org=ids["org"])
        _proposal(conn, ids["mate"], ids["org"], addr="88 Nyh Terrace", status="sent", sent=_ago(seconds=30))
        assert mate.post("/api/company/portal/today/handled", json={"action_id": a8, "answer": "yes"}).status_code == 404
        assert pm.post("/api/company/portal/today/handled", json={"action_id": a8, "answer": "yes"}).status_code == 403
        assert app.test_client().post("/api/company/portal/today/handled", json={"action_id": a8, "answer": "yes"}).status_code == 401
        assert me.post("/api/company/portal/today/handled", json={"action_id": a8, "answer": "maybe"}).status_code == 400
        assert me.post("/api/company/portal/today/handled", json={"action_id": a2, "answer": "yes"}).status_code == 409, "not a question"
        conn.execute("UPDATE claude_action SET created_at = ? WHERE id = ?", (_ago(hours=49), a8))
        conn.commit()
        assert me.post("/api/company/portal/today/handled", json={"action_id": a8, "answer": "yes"}).status_code == 409, "past 48 hours"
        passed += 1; print("  access ok")

        # ---- the page
        html = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        for needle in ("function tdHandled()", "data-td-reopen", "a.type === 'answer'", "a.type === 'link'", "td-compare",
                       "Claude adds things here and clears what your team finishes", "'/today/handled'"):
            assert needle in html, needle
        passed += 1; print("  page ok")
    finally:
        conn.close()
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: needs you clears itself ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
