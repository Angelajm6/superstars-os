#!/usr/bin/env python3
"""Claude across the platform, phase 2 (2026-10-14): client notes, client emails, proposals and
daily report entries — the BEHAVIORAL guard for the new claude_agent tools.

Proves, through a Flask test client against an ISOLATED database (planted model replies only):
  * who is offered what: a pm gets daily report reads and entries only; office roles get the rest
  * client_detail never shows Claude a contact's email or phone; the email card shows the real
    address to the person; a contact with no email is refused before any card
  * email_client: nothing is sent until Approve; the edited subject / message is what goes out,
    from the company with Reply-To = the approver; the client's activity records it
  * add_client_note: lands on the client's activity as the approver
  * create_proposal / update_proposal / send_proposal: a draft from the estimate, edited text
    and price, then sent with its link (fake mail folder); a sent proposal cannot be edited
  * add_daily_report_entry: a pm on a job they cannot open is refused; an admin entry lands
    in the day's issues; undo removes it
  * logs carry ids / counts only (no names, no addresses, no message text)

Database: honors SSC_DB_URL like every gate suite; otherwise a throwaway SQLite snapshot.
Run:  venv/bin/python tests/smoke_claude_actions.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import json
import logging
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_claude_actions_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
MAIL = Path(_TMP) / "mail"
os.environ["SSC_MAIL_FAKE_DIR"] = str(MAIL)
os.environ["APP_BASE_URL"] = "http://test.local"
for _k in ("ANTHROPIC_API_KEY", "SSC_ASSISTANT_FAKE", "SSC_ASSISTANT_MODEL", "SSC_ASSISTANT_DAILY_CAP_USD", "SENDGRID_API_KEY"):
    os.environ.pop(_k, None)

from flask import Flask  # noqa: E402

import assistant  # noqa: E402
import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import proposals  # noqa: E402
import team_items  # noqa: E402

TAG = "clac"
PW = "ClaudeActionsSmokePassword2026"
ORG = "Clac Smoke Harbor Owners"
EMAIL = "clac.board.chair@example.com"
JOB = "CLAC-1"
TODAY = date.today().isoformat()


class LogTrap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _db():
    return db_layer.connect(pragma_fk=True)


def _q(sql, args=()):
    conn = _db()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ca.ensure(conn)
        team_items.ensure(conn)
        proposals.ensure_schema(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        chats = [r["id"] for r in conn.execute(f"SELECT id FROM claude_chat WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "claude_action", "chat_id", chats)
        purge(conn, "claude_chat_message", "chat_id", chats)
        purge(conn, "claude_chat", "id", chats)
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name = ?", (ORG,)).fetchall()]
        ests = [r["id"] for r in conn.execute("SELECT id FROM estimate WHERE building_address LIKE '% Clac Smoke %'").fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph(ests)})", ests).fetchall()]
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate_document", "estimate_id", ests)
        purge(conn, "estimate", "id", ests)
        conn.execute("DELETE FROM building WHERE address_key LIKE '% clac smoke %'")
        conn.execute("DELETE FROM issues WHERE project_code = ?", (JOB,))
        conn.execute("DELETE FROM projects WHERE project_code = ?", (JOB,))
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
            conn.execute(f"DELETE FROM crm_task WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM crm_organization WHERE name = ?", (ORG,))
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()


def _seed() -> dict:
    _cleanup()
    conn = _db()
    ids = {}
    try:
        for role in ("admin", "pm"):
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, "
                         "is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 0)",
                         (f"{TAG}.{role}@example.com", auth.hash_password(PW), role, f"Clac {role.title()}"))
        conn.commit()
        ids.update({r["role"]: r["id"] for r in conn.execute("SELECT id, role FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()})
        ids["org"] = crm.create_org(conn, name=ORG, relationship_type="client")
        ids["chair"] = crm.create_contact(conn, full_name="Clac Chair Person", org_id=ids["org"], email=EMAIL, phone="212-555-0177")
        ids["nomail"] = crm.create_contact(conn, full_name="Clac No Email", org_id=ids["org"])
        e = estimates.create_estimate(conn, est_type="FR", borough="MN", client_org_id=ids["org"], contact_id=ids["chair"],
                                      building_address="5 Clac Smoke Place", created_by=ids["admin"])
        conn.execute("UPDATE estimate SET final_amount = 48500 WHERE id = ?", (e["id"],))
        ids["est"], ids["code"] = e["id"], e["code"]
        conn.execute("INSERT INTO projects (project_code, name, address) VALUES (?, ?, ?)", (JOB, "Clac smoke job", "5 Clac Smoke Place"))
        conn.commit()
    finally:
        conn.close()
    return ids


def _script(steps):
    os.environ["SSC_ASSISTANT_FAKE"] = json.dumps(steps)
    ca._FAKE["step"] = 0


def _cards(client, chat_id):
    v = client.get(f"/api/claude/chats/{chat_id}").get_json()["data"]
    return [x for m in v["messages"] if m["role"] == "assistant" for x in m["actions"]]


def _results(chat_id):
    return [b for m in ca._history(_db(), chat_id) if m["role"] == "user" for b in m["content"] if b.get("type") == "tool_result"]


def main() -> int:
    app = Flask("clac", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    ca.register(app)
    ca.RUN_INLINE = True
    trap = LogTrap()
    lg = logging.getLogger("ssc.claude_agent")
    lg.addHandler(trap)
    lg.setLevel(logging.INFO)

    def login(role):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.95.0.{len(role)}"})
        assert r.status_code == 200, (role, r.status_code)
        return c

    ids = _seed()
    passed = 0
    try:
        a, pm = login("admin"), login("pm")
        # ---- offers
        names = lambda role, s: {t.name for t in ca.tools_for(role, s)}  # noqa: E731
        assert {"daily_report", "add_daily_report_entry"} <= names("pm", "field")
        assert not {"email_client", "add_client_note", "client_detail", "create_proposal", "send_proposal"} & names("pm", "office")
        assert {"email_client", "add_client_note", "client_detail", "proposals", "proposal_detail", "create_proposal",
                "update_proposal", "send_proposal"} <= names("admin", "office")
        passed += 1; print("  offers ok")

        # ---- client detail hides emails; email card shows it to the person; no-email contact refused
        chat = a.post("/api/claude/chats", json={"surface": "office"}).get_json()["data"]["chat"]
        _script([{"tools": [{"name": "client_detail", "input": {"client_id": ids["org"]}},
                            {"name": "email_client", "input": {"contact_id": ids["nomail"], "subject": "Hi", "body": "Hello"}}]},
                 {"tools": [{"name": "email_client", "input": {"contact_id": ids["chair"], "subject": "Walkthrough Thursday",
                                                               "body": "Hello,\n\nCan we walk the roof Thursday at 9?\n\nAngela"}}]},
                 {"text": "The email is ready for you to check."}])
        a.post(f"/api/claude/chats/{chat['id']}/messages", json={"text": "email the board chair", "page": {"surface": "office"}})
        res = _results(chat["id"])
        detail = res[0]["content"]
        assert "Clac Chair Person" in detail and EMAIL not in detail and "555-0177" not in detail and '"can_be_emailed": true' in detail
        assert res[1].get("is_error") and "no email address" in res[1]["content"]
        cards = _cards(a, chat["id"])
        assert len(cards) == 1 and cards[0]["card"]["title"] == "Email to a client"
        to = next(f for f in cards[0]["card"]["fields"] if f["key"] == "contact_id")
        assert EMAIL in to["hint"] and ORG in to["hint"]
        assert not list(MAIL.glob("claude-email-*.eml")) if MAIL.exists() else True
        r = a.post(f"/api/claude/actions/{cards[0]['id']}/approve",
                   json={"values": {"subject": "Roof walkthrough Thursday 9 AM", "body": "Hello,\n\nCan we walk the roof Thursday at 9?\n\nThanks,\nAngela", "contact_id": "999"}})
        assert r.status_code == 200 and r.get_json()["data"]["status"] == "approved", r.get_json()
        mails = sorted(MAIL.glob("claude-email-*.eml"))
        assert len(mails) == 1
        txt = mails[0].read_text(encoding="utf-8")
        assert txt.startswith(f"To: {EMAIL}\nReply-To: {TAG}.admin@example.com\nSubject: Roof walkthrough Thursday 9 AM")
        acts = _q("SELECT activity_type, summary, author_user_id FROM crm_activity WHERE entity_type = 'organization' AND entity_id = ? "
                  "AND activity_type = 'email'", (ids["org"],))
        assert acts and acts[-1]["author_user_id"] == ids["admin"] and "Roof walkthrough" in acts[-1]["summary"]
        passed += 1; print("  client email ok (address hidden from Claude, approved edit sent)")

        # ---- client note
        _script([{"tools": [{"name": "add_client_note", "input": {"client_id": ids["org"], "kind": "call",
                                                                  "note": "Clac smoke call: board wants the walkthrough moved"}}]},
                 {"text": "Note card ready."}])
        a.post(f"/api/claude/chats/{chat['id']}/messages", json={"text": "log that call", "page": {"surface": "office"}})
        card = [c for c in _cards(a, chat["id"]) if c["status"] == "pending"][0]
        assert not _q("SELECT id FROM crm_activity WHERE summary LIKE 'Clac smoke call%'")
        assert a.post(f"/api/claude/actions/{card['id']}/approve", json={}).status_code == 200
        row = _q("SELECT activity_type, author_user_id FROM crm_activity WHERE summary LIKE 'Clac smoke call%'")
        assert row == [{"activity_type": "call", "author_user_id": ids["admin"]}]
        passed += 1; print("  client note ok")

        # ---- proposals: create, edit, send
        _script([{"tools": [{"name": "create_proposal", "input": {"estimate_id": ids["est"]}}]}, {"text": "Draft card ready."}])
        a.post(f"/api/claude/chats/{chat['id']}/messages", json={"text": "start the proposal", "page": {"surface": "office"}})
        card = [c for c in _cards(a, chat["id"]) if c["status"] == "pending"][0]
        assert not _q("SELECT id FROM proposal WHERE estimate_id = ?", (ids["est"],))
        assert a.post(f"/api/claude/actions/{card['id']}/approve", json={}).status_code == 200
        pid = _q("SELECT id FROM proposal WHERE estimate_id = ?", (ids["est"],))[0]["id"]
        _script([{"tools": [{"name": "update_proposal", "input": {"proposal_id": pid, "scope": "• Repoint parapet\n• Rebuild 3 lintels",
                                                                  "price": 51200}},
                            {"name": "update_proposal", "input": {"proposal_id": pid}}]},
                 {"text": "Edit card ready."}])
        a.post(f"/api/claude/chats/{chat['id']}/messages", json={"text": "write the scope", "page": {"surface": "office"}})
        assert _results(chat["id"])[-1].get("is_error") and "nothing to change" in _results(chat["id"])[-1]["content"]
        card = [c for c in _cards(a, chat["id"]) if c["status"] == "pending"][0]
        assert a.post(f"/api/claude/actions/{card['id']}/approve", json={"values": {"price": "52,000"}}).status_code == 200
        p = proposals.get(_db(), pid)
        assert p["scope_summary"] == "• Repoint parapet\n• Rebuild 3 lintels" and float(p["price"]) == 52000.0
        _script([{"tools": [{"name": "send_proposal", "input": {"proposal_id": pid, "message": "Here is our proposal."}}]},
                 {"text": "Send card ready."}])
        a.post(f"/api/claude/chats/{chat['id']}/messages", json={"text": "send it", "page": {"surface": "office"}})
        card = [c for c in _cards(a, chat["id"]) if c["status"] == "pending"][0]
        assert any(EMAIL in (f.get("hint") or "") for f in card["card"]["fields"])
        assert proposals.get(_db(), pid)["status"] == "draft"
        assert a.post(f"/api/claude/actions/{card['id']}/approve", json={}).status_code == 200
        assert proposals.get(_db(), pid)["status"] == "sent" and list(MAIL.glob("proposal-*.eml"))
        _script([{"tools": [{"name": "update_proposal", "input": {"proposal_id": pid, "title": "x"}}]}, {"text": "Can't."}])
        a.post(f"/api/claude/chats/{chat['id']}/messages", json={"text": "change the title", "page": {"surface": "office"}})
        assert "only a draft" in _results(chat["id"])[-1]["content"]
        passed += 1; print("  proposals ok (draft, edit, send; sent is locked)")

        # ---- daily report entries
        pchat = pm.post("/api/claude/chats", json={"surface": "field"}).get_json()["data"]["chat"]
        _script([{"tools": [{"name": "add_daily_report_entry", "input": {"project_code": JOB, "description": "Rain stopped work"}}]},
                 {"text": "I can't open that job."}])
        pm.post(f"/api/claude/chats/{pchat['id']}/messages", json={"text": "log rain", "page": {"surface": "field"}})
        assert _results(pchat["id"])[-1].get("is_error") and not _q("SELECT id FROM issues WHERE project_code = ?", (JOB,))
        _script([{"tools": [{"name": "add_daily_report_entry", "input": {"project_code": JOB, "category": "Weather",
                                                                         "description": "Rain stopped work at 11", "time_lost_hrs": 3.5}}]},
                 {"text": "Entry ready."}])
        a.post(f"/api/claude/chats/{chat['id']}/messages", json={"text": "log rain on the job", "page": {"surface": "office"}})
        card = [c for c in _cards(a, chat["id"]) if c["status"] == "pending"][0]
        r = a.post(f"/api/claude/actions/{card['id']}/approve", json={"values": {"status": "resolved"}})
        assert r.status_code == 200 and r.get_json()["data"]["can_undo"]
        row = _q("SELECT date, category, description, time_lost_hrs, status FROM issues WHERE project_code = ?", (JOB,))
        assert row == [{"date": TODAY, "category": "Weather", "description": "Rain stopped work at 11", "time_lost_hrs": 3.5,
                        "status": "resolved"}], row
        conn = _db()
        try:
            day = ca._t_daily(ca.Ctx(conn, {"id": ids["admin"], "role": "admin"}, "office"), {"project_code": JOB})
            assert day["issues_and_notes"] and day["issued"] is False
        finally:
            conn.close()
        assert a.post(f"/api/claude/actions/{card['id']}/undo", json={}).status_code == 200
        assert not _q("SELECT id FROM issues WHERE project_code = ?", (JOB,))
        passed += 1; print("  daily report entries ok")

        # ---- logs
        logs = "\n".join(trap.lines)
        assert trap.lines and EMAIL not in logs and "Clac Chair" not in logs and "Roof walkthrough" not in logs and "Rain stopped" not in logs
        passed += 1; print("  logs ok")
    finally:
        os.environ.pop("SSC_ASSISTANT_FAKE", None)
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"smoke_claude_actions: {passed}/6 groups passed")
    return 0 if passed == 6 else 1


if __name__ == "__main__":
    sys.exit(main())
