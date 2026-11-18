#!/usr/bin/env python3
"""Claude updates a client's record from an email, as one card (client_updates.py, 2026-11-18).

Proves against an ISOLATED database (fictional people only):
  * one card groups every change by person, each row with what is on file now; values already on file are dropped
    (an email in another case, a phone written differently), a "new" person already on file becomes that contact
  * a field it may not touch (deposit), a contact of another client, or a card with nothing new is refused
  * Approve saves only the ticked rows, with the person's edits; a new contact gets its roles; the client's billing email
    is set; the email is filed on the client; the client's activity says what changed
  * Undo puts the old values back and removes the contact it added
  * a card whose changes were already made by hand closes as already done; Needs you shows what will change
  * Claude sees the client's contacts by name and title only (never their email or phone)
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
_TMP = tempfile.mkdtemp(prefix="ssc_cupd_")

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
import client_updates as cu  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import inbox  # noqa: E402
import today_feed  # noqa: E402

TAG = "cupd"
PW = "ClientUpdatesSmoke2026pw"
ME = f"{TAG}.me@superstarscontracting.com"
CLIENT = "Cupd Smoke Harborview Owners"
OTHER = "Cupd Smoke Elsewhere LLC"
NOW = time.time()


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def gmsg(n: int) -> dict:
    hs = [{"name": "From", "value": "Jordan Cupdsmoke <jordan@cupd-harbor.example>"}, {"name": "To", "value": ME},
          {"name": "Cc", "value": "Sam Cupdokafor <sam@cupd-harbor.example>"},
          {"name": "Subject", "value": f"Cupd smoke {n}"}, {"name": "Message-ID", "value": f"<cupd{n}@cupd.example>"}]
    body = "Please send invoices to ap@cupd-harbor.example and copy Sam, our AP manager.\n\nJordan\nDirector of Building Operations\n(718) 555-0199"
    return {"id": f"g-cupd-{n}", "threadId": f"t-cupd-{n}", "labelIds": ["INBOX", "UNREAD"], "internalDate": str(int((NOW - 600) * 1000)),
            "snippet": body[:60], "payload": {"mimeType": "text/plain", "headers": hs, "body": {"data": _b64(body)}}}


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        for m in (ca, inbox, today_feed):
            m.ensure(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        mids = [r["id"] for r in conn.execute(f"SELECT id FROM mail_message WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "mail_link", "message_id", mids)
        purge(conn, "mail_message", "id", mids)
        chats = [r["id"] for r in conn.execute(f"SELECT id FROM claude_chat WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "claude_action", "chat_id", chats)
        purge(conn, "claude_chat_message", "chat_id", chats)
        purge(conn, "claude_chat", "id", chats)
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name IN (?, ?)", (CLIENT, OTHER)).fetchall()]
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM crm_organization WHERE name IN (?, ?)", (CLIENT, OTHER))
        conn.commit()
        purge_users(conn, uids)
    finally:
        conn.close()


def main() -> int:
    app = Flask("cupd", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    ca.register(app)
    inbox.register(app)
    today_feed.register(app)
    _cleanup()
    conn = db_layer.connect(pragma_fk=True)
    passed = 0
    try:
        conn.execute("INSERT INTO users (email, password_hash, full_name, role, status, must_reset_password, created_at) "
                     "VALUES (?,?,?,?,'active',0,?)", (ME, auth.hash_password(PW), "Cupd Me", "admin",
                                                       datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE email = ?", (ME,)).fetchone()["id"]
        org = crm.create_org(conn, name=CLIENT, relationship_type="client", created_by=uid)
        other = crm.create_org(conn, name=OTHER, relationship_type="client", created_by=uid)
        jordan = crm.create_contact(conn, org_id=org, full_name="Jordan Cupdsmoke", email="jordan@cupd-harbor.example",
                                    phone="(718) 555-0142", title="Facilities Coordinator", created_by=uid)
        stranger = crm.create_contact(conn, org_id=other, full_name="Not Theirs", created_by=uid)
        me = app.test_client()
        assert me.post("/api/auth/login", json={"email": ME, "password": PW}, environ_base={"REMOTE_ADDR": "10.98.0.1"}).status_code == 200

        tool = ca.BY_NAME["update_client_record"]
        ctx = ca.Ctx(conn, {"id": uid, "role": "admin"}, "office")
        mid = inbox.ingest(conn, uid, gmsg(1), ME)
        changes = [
            {"contact_id": jordan, "field": "title", "value": "Director of Building Operations", "why": "Signature changed"},
            {"contact_id": jordan, "field": "phone", "value": "718-555-0199", "why": "my new direct line"},
            {"contact_id": jordan, "field": "email", "value": "JORDAN@cupd-harbor.example"},          # already on file
            {"new_contact": "Jordan Cupdsmoke", "field": "phone", "value": "(718) 555-0142"},        # on file, same phone
            {"new_contact": "Sam Cupdokafor", "field": "email", "value": "sam@cupd-harbor.example", "why": "From the Cc line"},
            {"new_contact": "Sam Cupdokafor", "field": "title", "value": "AP Manager", "why": "our AP manager"},
            {"new_contact": "Sam Cupdokafor", "field": "roles", "value": "Billing / AP", "why": "copy Sam"},
            {"field": "billing_email", "value": "ap@cupd-harbor.example", "why": "send invoices to"},
        ]
        inp = {"client_org_id": org, "mail_id": mid, "changes": changes}

        # ---- the card
        tool.run(ctx, inp)
        c = tool.card(ctx, inp)
        rows = {r["key"]: r for g in c["groups"] for r in g["rows"]}
        titles = [g["title"] for g in c["groups"]]
        assert titles == ["Jordan Cupdsmoke", "Sam Cupdokafor", CLIENT], titles
        assert set(rows) == {f"c{jordan}.title", f"c{jordan}.phone", "n0.full_name", "n0.email", "n0.title", "n0.roles", "org.billing_email"}, rows
        assert rows[f"c{jordan}.phone"]["old"] == "(718) 555-0142" and rows[f"c{jordan}.phone"]["new"] == "718-555-0199"
        assert rows["org.billing_email"]["old"] == "" and rows["n0.roles"]["new"] == "billing"
        assert c["groups"][1]["is_new"] and len(c["fields"]) == 7 and all(f["type"] == "change" for f in c["fields"])
        assert "Phone: (718) 555-0142 → 718-555-0199" in cu.today_text(conn, inp)
        passed += 1; print("  card ok")

        # ---- refused
        for bad, why in (({"field": "deposit_pct", "value": "50"}, "can't be changed"),
                         ({"contact_id": stranger, "field": "title", "value": "X"}, "is not on"),
                         ({"field": "billing_email", "value": "not-an-email"}, "doesn't look like"),
                         ({"contact_id": jordan, "field": "title", "value": "facilities coordinator"}, "already has all")):
            try:
                tool.run(ctx, {"client_org_id": org, "changes": [bad]})
                raise AssertionError(f"accepted {bad}")
            except (ValueError, LookupError) as e:
                assert why in str(e), (why, str(e))
        passed += 1; print("  refusals ok")

        # ---- approve: Jordan's title unticked, Sam's title edited
        chat = inbox._mail_chat(conn, inbox.message(conn, mid))
        conn.execute("INSERT INTO claude_action (chat_id, message_id, tool_use_id, user_id, tool, input, summary, status, created_at) "
                     "VALUES (?, 0, 'tu-cupd-1', ?, 'update_client_record', ?, 'Update Cupd client', 'pending', ?)",
                     (chat, uid, json.dumps(inp), datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        aid = conn.execute("SELECT MAX(id) AS m FROM claude_action WHERE user_id = ?", (uid,)).fetchone()["m"]
        needs = me.get("/api/company/portal/today").get_json()["data"]["needs"]
        nc = next(x for x in needs if x.get("action_id") == aid)
        assert nc["pill"] == "Client details" and "Sam Cupdokafor · Email" in nc["draft"]["text"], nc
        vals = {k: rows[k]["new"] for k in rows}
        vals[f"c{jordan}.title"] = ""
        vals["n0.title"] = "Accounts Payable Manager"
        r = me.post(f"/api/claude/actions/{aid}/approve", json={"values": vals})
        assert r.status_code == 200, r.get_json()
        res = r.get_json()["data"]["result"]
        assert res["summary"] == f"{CLIENT} updated · 6 changes" and res["undo"], res
        j = crm.get_contact(conn, jordan)
        assert j["phone"] == "718-555-0199" and j["title"] == "Facilities Coordinator"
        sam = conn.execute("SELECT * FROM crm_contact WHERE org_id = ? AND full_name = 'Sam Cupdokafor'", (org,)).fetchone()
        assert sam and sam["email"] == "sam@cupd-harbor.example" and sam["title"] == "Accounts Payable Manager" and sam["roles"] == "billing"
        assert crm.get_org(conn, org)["billing_email"] == "ap@cupd-harbor.example"
        acts = [a["summary"] for a in crm.list_activity(conn, entity_type="organization", entity_id=org)]
        assert any(a.startswith("Updated from Jordan Cupdsmoke's email: ") and "Sam Cupdokafor: added" in a for a in acts), acts
        assert conn.execute("SELECT 1 FROM mail_link WHERE message_id = ? AND entity_type = 'client' AND entity_id = ?",
                            (mid, str(org))).fetchone(), "the email is filed on the client"
        passed += 1; print("  approve ok")

        # ---- undo
        assert me.post(f"/api/claude/actions/{aid}/undo").status_code == 200
        j = crm.get_contact(conn, jordan)
        assert j["phone"] == "(718) 555-0142" and j["title"] == "Facilities Coordinator"
        assert not conn.execute("SELECT 1 FROM crm_contact WHERE org_id = ? AND full_name = 'Sam Cupdokafor'", (org,)).fetchone()
        assert not crm.get_org(conn, org).get("billing_email")
        passed += 1; print("  undo ok")

        # ---- done by hand first: the waiting card closes
        mid2 = inbox.ingest(conn, uid, gmsg(2), ME)
        chat2 = inbox._mail_chat(conn, inbox.message(conn, mid2))
        inp2 = {"client_org_id": org, "mail_id": mid2, "changes": [{"contact_id": jordan, "field": "phone", "value": "(718) 555-0199"}]}
        conn.execute("INSERT INTO claude_action (chat_id, message_id, tool_use_id, user_id, tool, input, summary, status, created_at) "
                     "VALUES (?, 0, 'tu-cupd-2', ?, 'update_client_record', ?, 'Phone', 'pending', ?)",
                     (chat2, uid, json.dumps(inp2), datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        a2 = conn.execute("SELECT MAX(id) AS m FROM claude_action WHERE user_id = ?", (uid,)).fetchone()["m"]
        crm.update_contact(conn, jordan, phone="718.555.0199")
        ca.sweep_moot(conn)
        row = conn.execute("SELECT status, result FROM claude_action WHERE id = ?", (a2,)).fetchone()
        assert row["status"] == "declined" and "record already matches this email" in row["result"], dict(row)
        passed += 1; print("  already done ok")

        # ---- what Claude sees; the page
        prompt = inbox.suggest_prompt(conn, inbox.message(conn, mid), "Cupd Me")
        assert f"Contacts on file: Jordan Cupdsmoke (contact_id {jordan}, Facilities Coordinator)" in prompt
        state = inbox.live_state(conn, inbox.message(conn, mid))
        assert "555" not in state and "jordan@" not in state, "never a contact's phone or email"
        assert "ONE update_client_record" in prompt and "update_client_record" in [t.name for t in ca.TOOLS]
        js = (ROOT / "static/js/claude_drawer.js").read_text(encoding="utf-8")
        for needle in ("function changeRows(a, c)", "el.disabled ? '' : v", "data-tick", "data-chip", "'Approve ' + n + ' change'"):
            assert js.count(needle) >= 1, needle
        assert js.count("el.disabled ? '' : v") == 2, "both Approve paths skip unticked rows"
        v = re.search(r"claude_drawer\.js\?v=(\d+)", (ROOT / "claude.html").read_text(encoding="utf-8")).group(1)
        stale = [f.name for f in ROOT.glob("*.html") if "claude_drawer.js?v=" in f.read_text(encoding="utf-8")
                 and f"claude_drawer.js?v={v}\"" not in f.read_text(encoding="utf-8")]
        assert not stale, stale
        passed += 1; print("  prompt / page ok")
    finally:
        conn.close()
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: Claude updates client records ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
