#!/usr/bin/env python3
"""The activity bell and banners (activity_feed.py, phase 2 of the accepted prototype).

Proves against an ISOLATED database (fictional people only):
  * the real write paths appear: proposal started, sent, signed, declined; client added; project created; documents; payment
  * wording says what was done and never names the teammate
  * your own actions are left out, for every kind
  * the first look starts clean; a new event makes it unread; read and banner marks only move forward
  * banners: at most three, once each, none for uploads, payments, a started draft, or anything over 24 hours old
  * a send closes your card and offers Undo; Undo reopens it through the existing Reopen; a send with no card offers nothing
  * admins only: PM 403, signed out 401
  * the page: auth_menu loads the bell for admins, and no script writes a teammate's name
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
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_bell_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(Path(_TMP) / "mail"), "SSC_GMAIL_POLL": "0",
                   "SSC_REMINDERS": "0", "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(), "SSC_ASSISTANT_FAKE": "1"})

from flask import Flask  # noqa: E402

import activity_feed as af  # noqa: E402
import assistant  # noqa: E402
import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import inbox  # noqa: E402
import invoices  # noqa: E402
import proposals  # noqa: E402
import today_feed  # noqa: E402

TAG = "abell"
PW = "ActivityBellSmokePassword2026"
ME = f"{TAG}.me@superstarscontracting.com"
MATE = f"{TAG}.mate@superstarscontracting.com"
MATE_NAME = "Abell Mate"
CLIENT = "Abell Smoke Northbeam Engineers"
ADDR = "118 Abell Terrace, New York, NY"
NOW = time.time()


def _db():
    return db_layer.connect(pragma_fk=True)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def gmsg(gid, thread, rfc, minutes_ago, to=ME):
    hs = [{"name": "From", "value": "Dana Abellsmoke <dana@abell-north.example>"}, {"name": "To", "value": to},
          {"name": "Subject", "value": "Abell smoke " + rfc}, {"name": "Message-ID", "value": f"<{rfc}@abell.example>"}]
    return {"id": gid, "threadId": thread, "labelIds": ["INBOX", "UNREAD"], "internalDate": str(int((NOW - minutes_ago * 60) * 1000)),
            "snippet": "Please send a proposal", "payload": {"mimeType": "text/plain", "headers": hs, "body": {"data": _b64("Please send a proposal")}}}


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        for m in (ca, inbox, today_feed, af):
            m.ensure(conn)
        proposals.ensure_schema(conn)
        invoices.ensure_schema(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        mids = [r["id"] for r in conn.execute(f"SELECT id FROM mail_message WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "mail_link", "message_id", mids)
        purge(conn, "mail_message", "id", mids)
        chats = [r["id"] for r in conn.execute(f"SELECT id FROM claude_chat WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "claude_action", "chat_id", chats)
        purge(conn, "claude_chat_message", "chat_id", chats)
        purge(conn, "claude_chat", "id", chats)
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE ?", ("Abell Smoke%",)).fetchall()]
        invs = [r["id"] for r in conn.execute(f"SELECT id FROM invoice WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        purge(conn, "invoice_payment", "invoice_id", invs)
        purge(conn, "invoice", "id", invs)
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        codes = [r["code"] for r in conn.execute(f"SELECT code FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph(ests)})", ests).fetchall()]
        purge(conn, "crm_task", "entity_id", orgs)
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "project_documents", "project_code", codes + ["ABELL-DOCS"])
        purge(conn, "projects", "project_code", codes + ["ABELL-DOCS"])
        purge(conn, "estimate_ira", "estimate_id", ests)
        purge(conn, "estimate", "id", ests)
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM building WHERE address_line LIKE '%Abell Terrace%'")
        conn.execute("DELETE FROM crm_organization WHERE name LIKE ?", ("Abell Smoke%",))
        conn.commit()
        purge_users(conn, uids)
    finally:
        conn.close()


def _users():
    conn = _db()
    try:
        ids = {}
        for key, role, name in (("me", "admin", "Abell Me"), ("mate", "admin", MATE_NAME), ("pm", "pm", "Abell Pm")):
            conn.execute("INSERT INTO users (email, password_hash, full_name, role, status, must_reset_password, created_at) "
                         "VALUES (?,?,?,?,'active',0,?)", (f"{TAG}.{key}@superstarscontracting.com", auth.hash_password(PW), name, role,
                                                           datetime.now().isoformat(timespec="seconds")))
            conn.commit()
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.{key}@superstarscontracting.com",)).fetchone()["id"]
        ids["org"] = crm.create_org(conn, name=CLIENT, relationship_type="client", created_by=ids["me"])
        return ids
    finally:
        conn.close()


def _proposal(conn, uid, org, addr=ADDR, source=None) -> int:
    est = estimates.create_estimate(conn, est_type="FR", borough="MN", client_org_id=org, building_address=addr, created_by=uid)
    pid = proposals.create_from_estimate(conn, est["id"], created_by=uid)["id"]
    conn.execute("UPDATE proposal SET source_mail_id = ?, price = 100 WHERE id = ?", (source, pid))
    conn.commit()
    return pid


def _doc(conn, code, uid, category="PERMITS", at=None):
    conn.execute("INSERT INTO project_documents (project_code, category, title, doc_type, file_path, file_name, superseded, "
                 "uploaded_by_uid, uploaded_at) VALUES (?,?,?,?,?,?,0,?,?)",
                 (code, category, "Abell doc", "other", "/nope", "abell.pdf", uid, at or datetime.now().isoformat()))
    conn.commit()


def _age_everything(conn, seconds: int) -> None:
    """Everything from the set-up happened before the first look, as it would have in real life."""
    for table, col in (("proposal_event", "at"), ("crm_organization", "created_at"), ("projects", "created_at"),
                       ("project_documents", "uploaded_at"), ("invoice_payment", "created_at")):
        rows = conn.execute(f"SELECT {'id' if table != 'projects' else 'project_code'} AS k, {col} AS v FROM {table}").fetchall()
        for r in rows:
            try:
                new = (datetime.fromisoformat(str(r["v"])) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
            except ValueError:
                continue
            key = "id" if table != "projects" else "project_code"
            conn.execute(f"UPDATE {table} SET {col} = ? WHERE {key} = ?", (new, r["k"]))
    conn.commit()


def main() -> int:
    app = Flask("abell", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    ca.register(app)
    inbox.register(app)
    today_feed.register(app)
    af.register(app)
    _cleanup()
    ids = _users()

    def login(key):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@superstarscontracting.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.97.0.{len(key)}"})
        assert r.status_code == 200, (key, r.status_code)
        return c

    me, mate, pm = login("me"), login("mate"), login("pm")

    def feed(client=None):
        r = (client or me).get("/api/activity")
        assert r.status_code == 200, r.status_code
        return r.get_json()["data"]

    def seen(what, through, client=None):
        return (client or me).post("/api/activity/seen", json={"what": what, "through": through})

    def titled(d, needle):
        return [i for i in d["items"] if needle in i["title"]]

    passed = 0
    conn = _db()
    try:
        # ---- access
        assert pm.get("/api/activity").status_code == 403 and pm.post("/api/activity/seen", json={}).status_code == 403
        assert app.test_client().get("/api/activity").status_code == 401
        passed += 1; print("  access ok")

        # ---- the first look starts clean, then a teammate's work shows up unread
        first = feed()
        assert first["unread"] == 0 and first["banners"] == [], "turning it on sets nothing off"
        feed(mate)
        _age_everything(conn, 5)
        time.sleep(1.1)   # the first-look mark is stamped to the second

        pid = _proposal(conn, ids["mate"], ids["org"])
        d = feed()
        started = titled(d, f"A proposal for {ADDR.split(',')[0]}") or titled(d, "was started")
        assert started and started[0]["kind"] == "started" and started[0]["unread"], d["items"]
        assert not [b for b in d["banners"] if b["kind"] == "started"], "a started draft waits in the bell"
        assert not feed(mate)["unread"] and not [i for i in feed(mate)["items"] if i["kind"] == "started"], "your own work is left out"
        passed += 1; print("  first look + started ok")

        # ---- sent: the real write path, the wording, once only as a banner
        proposals.mark_sent_outside(conn, pid, actor=ids["mate"])
        d = feed()
        sent = titled(d, "was sent to")
        label = proposals._label(proposals.get(conn, pid))
        assert sent and sent[0]["title"] == f"Proposal {label} was sent to {CLIENT}" and sent[0]["kind"] == "sent"
        assert [b for b in d["banners"] if b["kind"] == "sent"] and not titled(d, "was started"), "the draft line gave way to sent"
        assert "undo_action_id" not in d["banners"][0], "no card closed, so nothing to undo"
        blob = json.dumps(d)
        assert MATE_NAME not in blob and f"{TAG}.mate" not in blob, "names no teammate"
        assert seen("banners", d["banners"][0]["at"]).status_code == 200
        assert not feed()["banners"], "a banner pops once"
        assert titled(feed(), "was sent to"), "it stays in the bell"
        passed += 1; print("  sent, wording, banner once ok")

        # ---- signed + project + client + documents + payment: each through its own real path
        time.sleep(1.1)
        proposals.mark_signed_offline(conn, pid, name="Dana Abellsmoke", actor=ids["mate"])
        code = proposals.get(conn, pid)["estimate_code"]
        est = proposals.get(conn, pid)["estimate_id"]
        estimates.convert_to_project(conn, est, actor_user_id=ids["mate"])
        org2 = crm.create_org(conn, name="Abell Smoke Halden Group", relationship_type="client", created_by=ids["mate"])
        pid2 = _proposal(conn, ids["mate"], org2, addr="40 Abell Terrace, Brooklyn, NY")
        org3 = crm.create_org(conn, name="Abell Smoke Solo Owner", relationship_type="client", created_by=ids["mate"])
        crm.create_org(conn, name="Abell Smoke From Books", relationship_type="client", notes=af.QUICKBOOKS_CLIENT_NOTE)
        for cat in ("PERMITS", "PERMITS", "DRAWINGS"):
            _doc(conn, code, ids["mate"], cat)
        inv = invoices.create(conn, {"client_org_id": ids["org"], "kind": "other", "amount": 6200, "description": "Abell smoke"}, created_by=ids["mate"])
        invoices.issue(conn, inv["id"], issued_on=date.today().isoformat(), due_on=date.today().isoformat(), actor=ids["mate"])
        invoices.record_payment(conn, inv["id"], amount=1200, actor=ids["mate"])
        invoices.record_payment(conn, inv["id"], amount=5000, actor=ids["mate"])
        d = feed()
        assert titled(d, f"was signed by {CLIENT}")[0]["kind"] == "signed"
        assert titled(d, f"Project {code} was created")[0]["href"] == f"/projects/{code}"
        added = [w for w in d["waiting"] if "New client added: Abell Smoke Halden Group" in w["title"]][0]   # it came with a proposal: it waits
        assert added["sub"].startswith("Added when Proposal ") and added["href"] == f"/company#clients/{org2}", added
        assert titled(d, "New client added: Abell Smoke Solo Owner")[0]["sub"] == "Added to your client list", org3
        assert not titled(d, "From Books"), "a QuickBooks sync can add dozens: not news"
        docs = titled(d, f"3 documents were uploaded to {code}")
        assert docs and docs[0]["sub"] == "Permits 2 · Drawings 1", docs
        paid = titled(d, f"Invoice {inv['number']} was paid")
        assert paid and "$6,200" in paid[0]["title"] and paid[0]["href"] == f"/company#invoice/{inv['id']}", paid
        assert titled(d, f"A $1,200 payment was recorded on invoice {inv['number']}")
        assert MATE_NAME not in json.dumps(d)
        passed += 1; print("  signed, project, clients, documents, payment ok")

        # ---- banners: three at most, never uploads or payments
        assert len(d["banners"]) == af.MAX_BANNERS, len(d["banners"])
        assert not [b for b in d["banners"] if b["kind"] in ("docs", "paid", "started")]
        all_banner_kinds = {i["kind"] for i in d["items"]}
        assert {"docs", "paid", "signed", "project", "client"} <= all_banner_kinds, all_banner_kinds
        seen("banners", d["latest"])
        assert not feed()["banners"]
        passed += 1; print("  banner limits ok")

        # ---- declined + a teammate's deleted proposal disappears
        pid3 = _proposal(conn, ids["mate"], ids["org"], addr="9 Abell Terrace, Queens, NY")
        proposals.mark_sent_outside(conn, pid3, actor=ids["mate"])
        proposals.mark_declined_offline(conn, pid3, actor=ids["mate"])
        assert titled(feed(), f"Proposal {proposals._label(proposals.get(conn, pid3))} was declined")
        lab2 = proposals._label(proposals.get(conn, pid2))
        assert titled(feed(), "40 Abell Terrace") and lab2
        conn.execute("UPDATE proposal SET status = 'deleted' WHERE id = ?", (pid2,))
        conn.commit()
        assert not titled(feed(), "40 Abell Terrace"), "a deleted proposal leaves the bell"
        passed += 1; print("  declined + deleted ok")

        # ---- your own actions, for every kind
        own = feed(mate)
        assert not titled(own, f"Project {code}") and not titled(own, "documents were uploaded") and not titled(own, "was paid"), own["items"]
        assert not titled(own, "was sent to") and not titled(own, "was signed") and not titled(own, "was declined"), own["items"]
        assert not titled(own, "Halden Group"), "the client they added"
        assert titled(own, f"New client added: {CLIENT}"), "the client someone else added"
        assert not titled(feed(), f"New client added: {CLIENT}"), "and the person who added it doesn't see it"
        passed += 1; print("  own actions left out ok")

        # ---- marks: read clears the badge and only moves forward; bad input refused; never past now
        d = feed()
        assert d["unread"] > 0
        assert seen("read", d["latest"]).status_code == 200
        d2 = feed()
        assert d2["unread"] == 0 and not [i for i in d2["items"] if i["unread"]] and d2["items"], "read stays listed"
        assert seen("read", "2020-01-01T00:00:00").status_code == 200 and feed()["unread"] == 0, "marks never move back"
        assert seen("nope", d["latest"]).status_code == 400 and seen("read", "not a time").status_code == 400
        assert seen("read", None).status_code == 400
        seen("read", "2999-01-01T00:00:00")
        future = conn.execute("SELECT read_at FROM activity_seen WHERE user_id = ?", (ids["me"],)).fetchone()["read_at"]
        assert future[:4] != "2999", "never past now"
        passed += 1; print("  marks ok")

        # ---- old news doesn't pop; a later upload the same day counts again
        seen("banners", feed()["latest"])   # the declined proposal above popped; only the upload is new from here
        time.sleep(1.1)
        _doc(conn, code, ids["mate"], "DRAWINGS")
        d = feed()
        assert titled(d, f"4 documents were uploaded to {code}")[0]["unread"]
        assert not d["banners"], "uploads never pop"
        conn.execute("UPDATE activity_seen SET banner_at = '2000-01-01T00:00:00' WHERE user_id = ?", (ids["me"],))
        conn.execute("UPDATE crm_organization SET created_at = ? WHERE id = ?", ((datetime.now() - timedelta(hours=30)).isoformat(timespec="seconds"), org3))
        conn.commit()
        d = feed()
        assert titled(d, "Solo Owner") and not [b for b in d["banners"] if "Solo Owner" in b["title"]], "over 24 hours: bell only"
        conn.execute("UPDATE activity_seen SET banner_at = ? WHERE user_id = ?", (datetime.now().isoformat(timespec="seconds"), ids["me"]))
        conn.commit()
        passed += 1; print("  old news + later upload ok")

        # ---- a send closes MY card: the banner says so and Undo reopens it
        time.sleep(1.1)
        mine = inbox.ingest(conn, ids["me"], gmsg("g-me-u1", "t-me-u1", "u1", 60), ME)
        theirs = inbox.ingest(conn, ids["mate"], gmsg("g-mate-u1", "t-mate-u1", "u1", 60, to=MATE), MATE)
        chat = inbox._mail_chat(conn, inbox.message(conn, mine))
        conn.execute("INSERT INTO claude_action (chat_id, message_id, tool_use_id, user_id, tool, input, summary, status, created_at) "
                     "VALUES (?, 0, 'tu-u1', ?, 'draft_proposal', ?, 'Card', 'pending', ?)",
                     (chat, ids["me"], json.dumps({"mail_id": mine, "client_org_id": ids["org"], "building_address": "7 Abell Terrace, New York, NY", "what": "Rope access."}),
                      datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        aid = conn.execute("SELECT MAX(id) AS m FROM claude_action WHERE user_id = ?", (ids["me"],)).fetchone()["m"]
        pid4 = _proposal(conn, ids["mate"], ids["org"], addr="7 Abell Terrace, New York, NY", source=theirs)
        proposals.mark_sent_outside(conn, pid4, actor=ids["mate"])
        d = feed()
        b = [x for x in d["banners"] if x["kind"] == "sent" and x["proposal_id"] == pid4]
        assert b and b[0]["undo_action_id"] == aid and "card is closed" in b[0]["note"], d["banners"]
        assert conn.execute("SELECT status FROM claude_action WHERE id = ?", (aid,)).fetchone()["status"] == "declined", "closed from the bell, without the dashboard open"
        assert "Abell smoke u1" not in json.dumps(b[0]) and "abell-north" not in json.dumps(b[0]), "no mail contents in a banner"
        r = me.post("/api/company/portal/today/handled", json={"action_id": b[0]["undo_action_id"], "answer": "reopen"})
        assert r.status_code == 200
        needs = me.get("/api/company/portal/today").get_json()["data"]["needs"]
        card = [c for c in needs if c.get("action_id") == aid]
        assert card and card[0]["pill"] == "Reopened by you", card
        assert proposals.get(conn, pid4)["status"] == "sent", "the proposal stays sent"
        assert mate.post("/api/company/portal/today/handled", json={"action_id": aid, "answer": "reopen"}).status_code == 404, "someone else's card"
        passed += 1; print("  card closes, Undo reopens ok")

        # ---- the pages
        am = (ROOT / "static/js/auth_menu.js").read_text(encoding="utf-8")
        assert "body.user.role === 'admin') loadBell()" in am and "activity_bell.js?v=" in am and "activity_bell.css?v=" in am
        js = (ROOT / "static/js/activity_bell.js").read_text(encoding="utf-8")
        for needle in ("'/api/activity'", "'/api/activity/seen'", "esc(it.title)", "esc(it.href)", "Your own actions don’t appear here", "document.hidden"):
            assert needle in js, needle
        portal = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        assert "view === 'invoice' && arg" in portal and "'ssc:activity-changed'" in portal
        for page in ROOT.glob("*.html"):
            t = page.read_text(encoding="utf-8")
            assert not any(f"auth_menu.js?v={v}" in t for v in (1010, 1011, 1012)), f"{page.name} keeps a cached script without the bell"
        passed += 1; print("  pages ok")
    finally:
        conn.close()
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: activity bell ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
