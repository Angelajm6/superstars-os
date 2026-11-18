#!/usr/bin/env python3
"""Claude's cards close themselves when the record already moved past them (claude_agent.sweep_moot, 2026-11-18).

The case that started it: a "Mark the proposal as sent" card stayed up after the proposal was signed.

Proves against an ISOLATED database (fictional people only):
  * a "mark as sent" card made while the proposal was a draft closes once it is signed: gone from Needs you, on
    Handled with the reason, shown as "Already done" (not "Declined"), no "Bring it back", Claude told in the chat
  * approving a card the record moved past changes nothing (409) and closes it
  * a card that only needs a field filled in (a draft with no price) stays open
  * an estimate already at the asked status closes; a proposal draft started from the estimate closes "New proposal"
  * kinds without a record state (a new event) are never touched; a broken input never closes a card
  * the drawer shows the closed note; every page loads the new drawer
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
_TMP = tempfile.mkdtemp(prefix="ssc_moot_")

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
import proposal_template  # noqa: E402
import proposals  # noqa: E402
import today_feed  # noqa: E402

TAG = "moot"
PW = "MootCardsSmokePassword2026"
ME = f"{TAG}.me@superstarscontracting.com"
CLIENT = "Moot Smoke Ridgeline Partners"
NOW = time.time()


def _db():
    return db_layer.connect(pragma_fk=True)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def gmsg(n: int) -> dict:
    hs = [{"name": "From", "value": "Robin Mootsmoke <robin@moot-ridge.example>"}, {"name": "To", "value": ME},
          {"name": "Subject", "value": f"Moot smoke {n}"}, {"name": "Message-ID", "value": f"<moot{n}@moot.example>"}]
    return {"id": f"g-moot-{n}", "threadId": f"t-moot-{n}", "labelIds": ["INBOX", "UNREAD"],
            "internalDate": str(int((NOW - 3600) * 1000)), "snippet": "Thanks for the proposal",
            "payload": {"mimeType": "text/plain", "headers": hs, "body": {"data": _b64("Thanks for the proposal")}}}


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
        conn.execute("DELETE FROM building WHERE address_line LIKE '%Moot Place%'")
        conn.execute("DELETE FROM crm_organization WHERE name = ?", (CLIENT,))
        conn.commit()
        purge_users(conn, uids)
    finally:
        conn.close()


def main() -> int:
    app = Flask("moot", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    ca.register(app)
    inbox.register(app)
    today_feed.register(app)
    proposal_template.register(app)
    proposals.register(app)
    _cleanup()
    conn = _db()
    passed = 0
    try:
        conn.execute("INSERT INTO users (email, password_hash, full_name, role, status, must_reset_password, created_at) "
                     "VALUES (?,?,?,?,'active',0,?)", (ME, auth.hash_password(PW), "Moot Me", "admin",
                                                       datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE email = ?", (ME,)).fetchone()["id"]
        org = crm.create_org(conn, name=CLIENT, relationship_type="client", created_by=uid)
        me = app.test_client()
        assert me.post("/api/auth/login", json={"email": ME, "password": PW},
                       environ_base={"REMOTE_ADDR": "10.97.0.1"}).status_code == 200

        n = [0]

        def card(tool: str, inp: dict) -> tuple:
            n[0] += 1
            m = inbox.ingest(conn, uid, gmsg(n[0]), ME)
            chat = inbox._mail_chat(conn, inbox.message(conn, m))
            conn.execute("INSERT INTO claude_action (chat_id, message_id, tool_use_id, user_id, tool, input, summary, status, created_at) "
                         "VALUES (?, 0, ?, ?, ?, ?, ?, 'pending', ?)", (chat, f"tu-moot-{n[0]}", uid, tool, json.dumps(inp),
                                                                         f"Card {n[0]}", datetime.now().isoformat(timespec="seconds")))
            conn.commit()
            return conn.execute("SELECT MAX(id) AS m FROM claude_action WHERE user_id = ?", (uid,)).fetchone()["m"], chat

        def proposal(addr: str, status: str = "draft") -> tuple:
            est = estimates.create_estimate(conn, est_type="FR", borough="MN", client_org_id=org, building_address=addr, created_by=uid)
            pid = proposals.create_from_estimate(conn, est["id"], created_by=uid)["id"]
            conn.execute("UPDATE proposal SET status = ? WHERE id = ?", (status, pid))
            conn.commit()
            return est["id"], pid

        def status_of(aid):
            return conn.execute("SELECT status FROM claude_action WHERE id = ?", (aid,)).fetchone()["status"]

        def needs():
            d = me.get("/api/company/portal/today").get_json()["data"]
            return {c.get("action_id") for c in d["needs"]}, d["handled"]

        def public(aid):
            rows = me.get("/api/claude/actions").get_json()["data"]
            return next(r for r in (rows if isinstance(rows, list) else rows["actions"]) if r["id"] == aid)

        # ---- the reported case: "mark as sent" made on a draft, then the proposal was signed
        _, pid = proposal("12 Moot Place, Brooklyn, NY")
        a1, chat1 = card("log_proposal_sent", {"proposal_id": pid, "sent_on": "2026-11-17", "price": 25500})
        open_now, _ = needs()
        assert a1 in open_now, "a draft's card is open"
        conn.execute("UPDATE proposal SET status = 'signed', sent_at = '2026-11-17T10:00:00', signed_at = '2026-11-17T15:00:00' WHERE id = ?", (pid,))
        conn.commit()
        open_now, handled = needs()
        assert a1 not in open_now and status_of(a1) == "declined", "signed: the card closes"
        h = next(x for x in handled if x["action_id"] == a1)
        assert "already signed (2026-11-17)" in h["note"] and h["title"] == "Card 1", h
        p = public(a1)
        assert p["closed_note"] and "already signed" in p["closed_note"] and not p["can_reopen"], p
        notes = [r["content"] for r in conn.execute("SELECT content FROM claude_chat_message WHERE chat_id = ?", (chat1,)).fetchall()]
        assert any(f"Card A-{a1} (log_proposal_sent) was closed without a change" in c for c in notes), "Claude is told"
        assert me.post(f"/api/claude/actions/{a1}/reopen").status_code in (200, 409)
        ca.sweep_moot(conn)
        assert status_of(a1) == "declined", "bringing it back just closes it again"
        passed += 1; print("  signed proposal closes 'mark as sent' ok")

        # ---- approving one the record moved past: nothing changes, it closes
        _, pid2 = proposal("14 Moot Place, Brooklyn, NY")
        a2, chat2 = card("log_proposal_sent", {"proposal_id": pid2, "sent_on": "2026-11-17"})
        conn.execute("UPDATE proposal SET status = 'sent', sent_at = '2026-11-16T09:00:00' WHERE id = ?", (pid2,))
        conn.commit()
        r = me.post(f"/api/claude/actions/{a2}/approve", json={"values": {}})
        assert r.status_code == 409 and "already sent (2026-11-16)" in r.get_json()["error"], r.get_json()
        assert status_of(a2) == "declined"
        assert conn.execute("SELECT sent_at FROM proposal WHERE id = ?", (pid2,)).fetchone()["sent_at"] == "2026-11-16T09:00:00"
        # opening the conversation sweeps too
        a2b, _ = card("update_proposal", {"proposal_id": pid2, "instruction": "shorter"})
        conn.execute("UPDATE claude_action SET chat_id = ? WHERE id = ?", (chat2, a2b))
        conn.commit()
        assert me.get(f"/api/claude/chats/{chat2}?surface=office").status_code == 200
        assert status_of(a2b) == "declined", "a sent proposal can't be edited: the card closes when the chat opens"
        passed += 1; print("  approve / chat view ok")

        # ---- a card that only needs a field stays open; other kinds are never touched
        _, pid3 = proposal("16 Moot Place, Brooklyn, NY")
        a3, _ = card("send_proposal", {"proposal_id": pid3})   # a draft with no price: an edit away, not moot
        a4, _ = card("create_event", {"title": "Walkthrough", "starts_at": "2026-11-20T09:00"})
        a5, _ = card("log_proposal_sent", {"proposal_id": "not a number"})
        ca.sweep_moot(conn)
        assert status_of(a3) == "pending" and status_of(a4) == "pending" and status_of(a5) == "pending"
        assert ca.moot_reason(conn, {"id": a5, "tool": "log_proposal_sent", "input": '{"proposal_id": "x"}'}) is None
        passed += 1; print("  editable / unrelated / broken cards stay ok")

        # ---- estimates: the status is already there; a proposal was started
        est4, _ = proposal("18 Moot Place, Brooklyn, NY")
        cur = estimates.get_estimate(conn, est4)["status"]
        a6, _ = card("change_estimate_status", {"estimate_id": est4, "status": cur})
        a7, _ = card("create_proposal", {"estimate_id": est4})
        nxt = estimates.allowed_transitions(cur)[0]
        a8, _ = card("change_estimate_status", {"estimate_id": est4, "status": nxt})
        ca.purge_expired(conn)   # the background sweep everyone's counts go through
        assert status_of(a6) == "declined" and status_of(a7) == "declined" and status_of(a8) == "pending"
        assert "is already draft" in public(a7)["closed_note"]
        passed += 1; print("  estimates ok")

        # ---- the page
        js = (ROOT / "static/js/claude_drawer.js").read_text(encoding="utf-8")
        assert "a.closed_note ? 'Already done'" in js and "esc(a.closed_note) + '. Nothing was changed.'" in js
        v = re.search(r"claude_drawer\.js\?v=(\d+)", (ROOT / "claude.html").read_text(encoding="utf-8")).group(1)
        stale = [f.name for f in ROOT.glob("*.html") if "claude_drawer.js?v=" in f.read_text(encoding="utf-8")
                 and f"claude_drawer.js?v={v}\"" not in f.read_text(encoding="utf-8")]
        assert not stale, stale
        prompt = (ROOT / "inbox.py").read_text(encoding="utf-8")
        assert "a signed, declined or sent proposal is never marked sent" in prompt
        passed += 1; print("  page ok")
    finally:
        conn.close()
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: cards close when the record moved on ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
