#!/usr/bin/env python3
"""Claude sees where things stand before it suggests anything on an email (inbox.live_state, 2026-11-18).

Proves against an ISOLATED database (fictional people only):
  * an email from a client's contact puts that client's proposals (status, dates, price), estimates / bids and jobs
    (with the documents filed) in Claude's prompt, with the rule to trust it over the email
  * a signed proposal reads "signed <date> · sent <date>", so "mark as sent" is never proposed for it
  * deleted / superseded proposals, superseded documents and another client's records never show
  * what was already approved in this conversation shows
  * an email that matches nothing gets no block; a failing read never stops the prompt
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
_TMP = tempfile.mkdtemp(prefix="ssc_live_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(Path(_TMP) / "mail"), "SSC_GMAIL_POLL": "0",
                   "SSC_REMINDERS": "0", "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(), "SSC_ASSISTANT_FAKE": "1"})

import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import inbox  # noqa: E402
import proposals  # noqa: E402

TAG = "lvst"
ME = f"{TAG}.me@superstarscontracting.com"
CLIENT = "Lvst Smoke Harborview Owners"
OTHER = "Lvst Smoke Other Holdings"
JOB = "FR-BK-LV1"
NOW = time.time()


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def gmsg(n: int, frm: str, thread: str) -> dict:
    hs = [{"name": "From", "value": frm}, {"name": "To", "value": ME}, {"name": "Subject", "value": f"Lvst smoke {n}"},
          {"name": "Message-ID", "value": f"<lvst{n}@lvst.example>"}]
    return {"id": f"g-lvst-{n}", "threadId": thread, "labelIds": ["INBOX", "UNREAD"], "internalDate": str(int((NOW - 600) * 1000)),
            "snippet": "Signed copy attached", "payload": {"mimeType": "text/plain", "headers": hs, "body": {"data": _b64("Signed copy attached")}}}


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        for m in (ca, inbox):
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
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name IN (?, ?)", (CLIENT, OTHER)).fetchall()]
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph(ests)})", ests).fetchall()]
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate_ira", "estimate_id", ests)
        purge(conn, "estimate", "id", ests)
        conn.execute("DELETE FROM project_documents WHERE project_code = ?", (JOB,))
        conn.execute("DELETE FROM projects WHERE project_code = ?", (JOB,))
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM building WHERE address_line LIKE '%Lvst Street%'")
        conn.execute("DELETE FROM crm_organization WHERE name IN (?, ?)", (CLIENT, OTHER))
        conn.commit()
        purge_users(conn, uids)
    finally:
        conn.close()


def main() -> int:
    _cleanup()
    conn = db_layer.connect(pragma_fk=True)
    passed = 0
    try:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("INSERT INTO users (email, password_hash, full_name, role, status, must_reset_password, created_at) "
                     "VALUES (?,?,?,?,'active',0,?)", (ME, auth.hash_password("LiveStateSmoke2026pw"), "Lvst Me", "admin", now))
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE email = ?", (ME,)).fetchone()["id"]
        org = crm.create_org(conn, name=CLIENT, relationship_type="client", created_by=uid)
        other = crm.create_org(conn, name=OTHER, relationship_type="client", created_by=uid)
        crm.create_contact(conn, org_id=org, full_name="Pat Lvstsmoke", email="pat@lvst-harbor.example", created_by=uid)

        def proposal(o, addr, status, price=None, sent=None, signed=None, title=None):
            est = estimates.create_estimate(conn, est_type="FR", borough="BK", client_org_id=o, building_address=addr, created_by=uid)
            pid = proposals.create_from_estimate(conn, est["id"], created_by=uid)["id"]
            conn.execute("UPDATE proposal SET status = ?, price = ?, sent_at = ?, signed_at = ?, title = COALESCE(?, title) WHERE id = ?",
                         (status, price, sent, signed, title, pid))
            conn.commit()
            return est, proposals.get(conn, pid)

        e1, p1 = proposal(org, "823 Lvst Street, Brooklyn, NY", "signed", 25500, "2026-11-16T09:00:00", "2026-11-17T15:00:00",
                          "823 Lvst Street - Garage fireproofing")
        _, p2 = proposal(org, "825 Lvst Street, Brooklyn, NY", "deleted", 900, None, None, "Lvst deleted one")
        _, p3 = proposal(other, "9 Lvst Street, Brooklyn, NY", "sent", 4000, "2026-11-10T09:00:00", None, "Lvst other client")
        conn.execute("UPDATE estimate SET converted_project_code = ? WHERE id = ?", (JOB, e1["id"]))
        conn.execute("INSERT INTO projects (project_code, name, address, client_org_id, status) VALUES (?,?,?,?,?)",
                     (JOB, "Lvst Garage", "823 Lvst Street", org, "active"))
        for title, sup in (("Signed proposal 1255", 0), ("Old COI", 1), ("Shop drawings rev B", 0)):
            conn.execute("INSERT INTO project_documents (project_code, category, title, file_path, superseded, uploaded_at) "
                         "VALUES (?, 'CONTRACTS', ?, '/nowhere', ?, ?)", (JOB, title, sup, now))
        conn.commit()

        mid = inbox.ingest(conn, uid, gmsg(1, "Pat Lvstsmoke <pat@lvst-harbor.example>", "t-lvst-1"), ME)
        m = inbox.message(conn, mid)
        assert m["client_org_id"] == org, "the contact's email matches the client"
        prompt = inbox.suggest_prompt(conn, m, "Lvst Me")
        label = proposals._label(p1)
        assert "Where things stand right now" in prompt and "never propose a step that is already done" in prompt
        assert (f"Proposal {label} (proposal_id {p1['id']}) · 823 Lvst Street - Garage fireproofing · signed 2026-11-17 · "
                f"sent 2026-11-16 · $25,500.00") in prompt, prompt
        assert f"became job {JOB}" in prompt and f"(estimate_id {e1['id']})" in prompt
        assert f"Job {JOB} · Lvst Garage · active · 2 documents filed: " in prompt
        assert "Signed proposal 1255" in prompt and "Shop drawings rev B" in prompt and "Old COI" not in prompt
        assert "Lvst deleted one" not in prompt and "Lvst other client" not in prompt and f"proposal_id {p3['id']}" not in prompt
        assert prompt.index("Where things stand") < prompt.index("<email>"), "the state comes before the email text"
        passed += 1; print("  client state ok")

        # ---- already approved in this conversation
        chat = inbox._mail_chat(conn, m)
        conn.execute("INSERT INTO claude_action (chat_id, message_id, tool_use_id, user_id, tool, input, summary, status, created_at, decided_at) "
                     "VALUES (?, 0, 'tu-lvst-1', ?, 'add_client_note', '{}', 'Note: signed copy received', 'approved', ?, ?)",
                     (chat, uid, now, now))
        conn.commit()
        mid2 = inbox.ingest(conn, uid, gmsg(2, "Pat Lvstsmoke <pat@lvst-harbor.example>", "t-lvst-1"), ME)
        prompt2 = inbox.suggest_prompt(conn, inbox.message(conn, mid2), "Lvst Me")
        assert f"Already approved in this conversation {now[:10]}: add_client_note: Note: signed copy received" in prompt2
        passed += 1; print("  this conversation ok")

        # ---- nothing matched; a failing read
        mid3 = inbox.ingest(conn, uid, gmsg(3, "Stranger <nobody@lvst-none.example>", "t-lvst-3"), ME)
        p_none = inbox.suggest_prompt(conn, inbox.message(conn, mid3), "Lvst Me")
        assert "Where things stand" not in p_none and "<email>" in p_none
        real = inbox.live_state
        inbox.live_state = lambda c, mm: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            p_fail = inbox.suggest_prompt(conn, m, "Lvst Me")
        finally:
            inbox.live_state = real
        assert "Where things stand" not in p_fail and "<email>" in p_fail
        passed += 1; print("  no match / failure ok")
    finally:
        conn.close()
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: Claude sees where things stand ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
