#!/usr/bin/env python3
"""Claude across the platform (accepted 2026-10-14): the BEHAVIORAL guard for claude_agent.py.

Proves, through a Flask test client against an ISOLATED database (planted model replies only):
  * gating: anonymous 401; a client 403; admin / pm / super get the window
  * a pm is offered no payroll, no estimates and no create tools; an admin on a JOB DASHBOARD
    is offered no payroll (field-reachable surface); an admin on an office page is
  * conversations are private (another person gets 404) and an office conversation is never
    opened from a job dashboard
  * a read tool runs straight away and its result reaches the model scrubbed (no emails,
    phones, paths)
  * a write tool NEVER runs from the model: it becomes a pending card; nothing is created
    until Approve; the card's fields are editable and the edited values are what get saved
  * an invalid proposal (bad date) goes back to the model as an error, no card
  * decline changes nothing; a decided card cannot be decided again; only its owner decides
  * approve runs with the approver's permissions; undo cancels the new event
  * an expired card (48 h) cannot be approved, and it is deleted (gone from the log)
  * a decline can be undone by its owner within the 48 h, then decided again
  * a tool the role may not use is refused even if the model asks for it
  * payroll summary returns totals only (no per-person pay) and matches hours x rate
  * the daily budget (shared with the Proposals panel) stops new turns (429)
  * history replays append-only; tool results lead their turn
  * the real request: tools, Opus 5.5, medium effort, refusal fallback, cached instructions;
    a fallback marker's declined blocks are not replayed
  * logs carry ids / tool names / counts only; the drawer is on every page and the
    Claude approvals view is in Settings

Database: honors SSC_DB_URL like every gate suite; otherwise a throwaway SQLite snapshot.
Run:  venv/bin/python tests/smoke_claude_agent.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import json
import logging
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_claude_agent_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
for _k in ("ANTHROPIC_API_KEY", "SSC_ASSISTANT_FAKE", "SSC_ASSISTANT_MODEL", "SSC_ASSISTANT_DAILY_CAP_USD", "SSC_ASSISTANT_MONTHLY_BUDGET_USD", "SSC_ASSISTANT_USER_DAILY_USD", "SSC_CLAUDE_MODEL", "SSC_CLAUDE_EFFORT"):
    os.environ.pop(_k, None)

from flask import Flask  # noqa: E402

import assistant  # noqa: E402
import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import db_layer  # noqa: E402
import team_items  # noqa: E402

TAG = "clag"
PW = "ClaudeAgentSmokePassword2026"
SECRET = "Smoke clag secret phrase"
NEXT = (date.today() + timedelta(days=3)).isoformat()


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


def _x(sql, args=()):
    conn = _db()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ca.ensure(conn)
        team_items.ensure(conn)
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = ",".join("?" * len(ids)) or "NULL"
        chats = [r["id"] for r in conn.execute(f"SELECT id FROM claude_chat WHERE user_id IN ({ph})", ids).fetchall()]
        purge(conn, "claude_action", "chat_id", chats)
        purge(conn, "claude_chat_message", "chat_id", chats)
        purge(conn, "claude_chat", "id", chats)
        conn.execute("DELETE FROM crm_task WHERE title LIKE 'Clag smoke%'")
        conn.commit()
        purge_users(conn, ids)
        conn.commit()
    finally:
        conn.close()


def _seed() -> dict:
    _cleanup()
    conn = _db()
    try:
        for role in ("admin", "pm", "super", "client"):
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, "
                         "is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 0)",
                         (f"{TAG}.{role}@example.com", auth.hash_password(PW), role, f"Clag {role.title()}"))
        conn.commit()
        return {r["role"]: r["id"] for r in conn.execute("SELECT id, role FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()}
    finally:
        conn.close()


def _script(steps):
    os.environ["SSC_ASSISTANT_FAKE"] = json.dumps(steps)
    ca._FAKE["step"] = 0


def main() -> int:
    app = Flask("clag", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    ca.register(app)
    ca.RUN_INLINE = True
    trap = LogTrap()
    for name in ("ssc.claude_agent", "ssc.team_items"):
        lg = logging.getLogger(name)
        lg.addHandler(trap)
        lg.setLevel(logging.INFO)

    def login(role):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.96.0.{len(role)}"})
        assert r.status_code == 200, (role, r.status_code)
        return c

    ids = _seed()
    passed = 0
    try:
        # ---- gating + what each person is offered
        assert app.test_client().get("/api/claude/status").status_code == 401
        assert login("client").get("/api/claude/status").status_code == 403
        a, pm = login("admin"), login("pm")
        st = a.get("/api/claude/status?surface=office").get_json()["data"]
        assert "spent_today_usd" not in st and "cap_usd" not in st
        assert st["available"] is False and "Weekly payroll" in st["looks_up"] and "New event" in st["proposes"]
        stf = a.get("/api/claude/status?surface=field").get_json()["data"]
        assert "Weekly payroll" not in stf["looks_up"] and "New event" in stf["proposes"]
        sp = pm.get("/api/claude/status?surface=office").get_json()["data"]
        assert "Weekly payroll" not in sp["looks_up"] and "Estimates" not in sp["looks_up"] and "Hours log" not in sp["looks_up"]
        assert sp["proposes"] == ["Mark done", "Daily report entry"], sp["proposes"]
        names = lambda role, s: [t.name for t in ca.tools_for(role, s)]  # noqa: E731
        assert "payroll_summary" in names("c_suite", "office") and "payroll_summary" not in names("c_suite", "field")
        assert not {"create_event", "create_task", "payroll_summary"} & set(names("super", "office"))
        passed += 1; print("  gating + offers ok")

        # ---- no key: refused, nothing stored
        chat = a.post("/api/claude/chats", json={"surface": "office"}).get_json()["data"]["chat"]
        r = a.post(f"/api/claude/chats/{chat['id']}/messages", json={"text": "hi"})
        assert r.status_code == 503 and not _q("SELECT id FROM claude_chat_message WHERE chat_id = ?", (chat["id"],))
        passed += 1; print("  off without a key ok")

        # ---- privacy + surfaces
        assert pm.get(f"/api/claude/chats/{chat['id']}").status_code == 404
        assert a.get(f"/api/claude/chats/{chat['id']}?surface=field").status_code == 200   # a chat follows the person (2026-10-22)
        passed += 1; print("  private + surfaces ok")

        # ---- a read runs; a write becomes a card; nothing created yet
        _script([{"tools": [{"name": "team_people", "input": {}}, {"name": "calendar", "input": {"from": date.today().isoformat()}}]},
                 {"tools": [{"name": "create_event", "input": {"title": "Clag smoke walkthrough", "date": NEXT, "start_time": "09:00",
                                                                "end_time": "10:00", "address": "212 E 47th St",
                                                                "assignee_user_id": ids["pm"]}}],
                  "text": "Here is the event; approve it below."},
                 {"text": "The card is ready for you."}])
        r = a.post(f"/api/claude/chats/{chat['id']}/messages", json={"text": SECRET + ": add a walkthrough",
                                                                        "page": {"surface": "office", "label": "Proposals", "path": "/company#proposals"}})
        assert r.status_code == 202, r.get_json()
        v = a.get(f"/api/claude/chats/{chat['id']}").get_json()["data"]
        assert v["busy"] is False and v["error"] is None, v
        assert v["messages"][0] == dict(v["messages"][0], role="user", text=SECRET + ": add a walkthrough")
        cards = [x for m in v["messages"] if m["role"] == "assistant" for x in m["actions"]]
        assert len(cards) == 1 and cards[0]["status"] == "pending" and cards[0]["card"]["title"] == "New event"
        assert any("Team" in m.get("looked", []) and "Calendar" in m.get("looked", []) for m in v["messages"] if m["role"] == "assistant")
        assert not _q("SELECT id FROM crm_task WHERE title LIKE 'Clag smoke%'"), "a write ran without approval"
        hist = ca._history(_db(), chat["id"])
        assert [m["role"] for m in hist] == ["user", "assistant", "user", "assistant", "user", "assistant"]
        assert hist[0]["content"][0]["text"].startswith("[Page: Proposals")
        assert hist[2]["content"][0]["type"] == "tool_result" and hist[4]["content"][0]["type"] == "tool_result"
        assert "NOT done yet" in hist[4]["content"][0]["content"]
        people_result = hist[2]["content"][0]["content"]
        assert "Clag Pm" in people_result and "@example.com" not in people_result
        passed += 1; print("  read runs, write waits ok")

        # ---- approve with an edit; only the owner; once only
        aid = cards[0]["id"]
        assert pm.post(f"/api/claude/actions/{aid}/approve", json={}).status_code == 404
        r = a.post(f"/api/claude/actions/{aid}/approve", json={"values": {"title": "Clag smoke walkthrough (edited)", "start_time": "09:30",
                                                                         "item_id": "999"}})
        assert r.status_code == 200, r.get_json()
        d = r.get_json()["data"]
        assert d["status"] == "approved" and d["can_undo"] and d["result"]["link"].endswith(NEXT)
        row = _q("SELECT * FROM crm_task WHERE title LIKE 'Clag smoke%'")
        assert len(row) == 1 and row[0]["title"] == "Clag smoke walkthrough (edited)" and row[0]["start_time"] == "09:30"
        assert row[0]["kind"] == "event" and row[0]["start_date"] == NEXT and row[0]["assignee_user_id"] == ids["pm"]
        assert row[0]["created_by"] == ids["admin"]
        assert a.post(f"/api/claude/actions/{aid}/approve", json={}).status_code == 409
        assert a.post(f"/api/claude/actions/{aid}/decline", json={}).status_code == 409
        note = _q("SELECT content FROM claude_chat_message WHERE chat_id = ? AND kind = 'note'", (chat["id"],))
        assert note and "approved card A-" in note[-1]["content"]
        # undo cancels it
        r = a.post(f"/api/claude/actions/{aid}/undo", json={})
        assert r.status_code == 200 and r.get_json()["data"]["status"] == "undone"
        assert _q("SELECT status FROM crm_task WHERE id = ?", (row[0]["id"],))[0]["status"] == "cancelled"
        passed += 1; print("  approve (edited) + undo ok")

        # ---- an invalid proposal goes back as an error; decline changes nothing; expiry
        _script([{"tools": [{"name": "create_task", "input": {"title": "Clag smoke bad", "due_date": "next week"}}]},
                 {"tools": [{"name": "create_task", "input": {"title": "Clag smoke call the GC", "due_date": NEXT}},
                            {"name": "create_task", "input": {"title": "Clag smoke old card", "due_date": NEXT}}]},
                 {"text": "Two cards for you."}])
        a.post(f"/api/claude/chats/{chat['id']}/messages", json={"text": "and two tasks", "page": {"surface": "office"}})
        hist = ca._history(_db(), chat["id"])
        errs = [b for m in hist if m["role"] == "user" for b in m["content"] if b.get("type") == "tool_result" and b.get("is_error")]
        assert errs and "date" in errs[-1]["content"]
        pend = _q("SELECT id, input FROM claude_action WHERE chat_id = ? AND status = 'pending' ORDER BY id", (chat["id"],))
        assert len(pend) == 2
        r = a.post(f"/api/claude/actions/{pend[0]['id']}/decline", json={})
        assert r.status_code == 200 and r.get_json()["data"]["status"] == "declined"
        _x("UPDATE claude_action SET created_at = ? WHERE id = ?", ((datetime.now() - timedelta(hours=49)).isoformat(timespec="seconds"), pend[1]["id"]))
        assert a.post(f"/api/claude/actions/{pend[1]['id']}/approve", json={}).status_code == 409
        assert not _q("SELECT id FROM crm_task WHERE title IN ('Clag smoke call the GC', 'Clag smoke old card', 'Clag smoke bad')")
        # an unanswered card past 48 h is deleted, not logged
        log_ids = [x["id"] for x in a.get("/api/claude/actions?scope=all").get_json()["data"]]
        assert pend[1]["id"] not in log_ids and pend[0]["id"] in log_ids
        assert not _q("SELECT id FROM claude_action WHERE id = ?", (pend[1]["id"],))
        passed += 1; print("  invalid / decline / expiry + purge ok")

        # ---- undo a decline: owner only, inside the 48 h, then it can be approved
        did = pend[0]["id"]
        row = next(x for x in a.get("/api/claude/actions?scope=mine").get_json()["data"] if x["id"] == did)
        assert row["can_reopen"] and row["expires_at"] and row["mine"]
        assert pm.post(f"/api/claude/actions/{did}/reopen", json={}).status_code == 404
        r = a.post(f"/api/claude/actions/{did}/reopen", json={})
        d = r.get_json()["data"]
        assert r.status_code == 200 and d["status"] == "pending" and not d["decided_by"] and not d["can_reopen"]
        assert a.post(f"/api/claude/actions/{did}/reopen", json={}).status_code == 409
        note = _q("SELECT content FROM claude_chat_message WHERE chat_id = ? AND kind = 'note'", (chat["id"],))
        assert "undid the decline of card A-" in note[-1]["content"]
        assert a.post(f"/api/claude/actions/{did}/decline", json={}).status_code == 200
        _x("UPDATE claude_action SET created_at = ? WHERE id = ?", ((datetime.now() - timedelta(hours=49)).isoformat(timespec="seconds"), did))
        assert a.post(f"/api/claude/actions/{did}/reopen", json={}).status_code == 409
        row = next(x for x in a.get("/api/claude/actions?scope=mine").get_json()["data"] if x["id"] == did)
        assert row["status"] == "declined" and not row["can_reopen"]   # declined cards stay in the log
        assert not _q("SELECT id FROM crm_task WHERE title = 'Clag smoke call the GC'")
        passed += 1; print("  undo decline ok")

        # ---- a tool the role may not use is refused even when the model asks
        pchat = pm.post("/api/claude/chats", json={"surface": "office"}).get_json()["data"]["chat"]
        _script([{"tools": [{"name": "payroll_summary", "input": {}}, {"name": "create_event", "input": {"title": "Clag smoke pm", "date": NEXT}}]},
                 {"text": "I can't do those."}])
        pm.post(f"/api/claude/chats/{pchat['id']}/messages", json={"text": "payroll please", "page": {"surface": "office"}})
        res = [b for m in ca._history(_db(), pchat["id"]) if m["role"] == "user" for b in m["content"] if b.get("type") == "tool_result"]
        assert len(res) == 2 and all(b.get("is_error") and "not available" in b["content"] for b in res)
        assert not _q("SELECT id FROM claude_action WHERE chat_id = ?", (pchat["id"],))
        passed += 1; print("  role limits ok")

        # ---- payroll summary: totals only
        conn = _db()
        try:
            ctx = ca.Ctx(conn, {"id": ids["admin"], "role": "admin"}, "office")
            p = ca._t_payroll(ctx, {"from": (date.today() - timedelta(days=13)).isoformat(), "to": date.today().isoformat()})
            assert {"weeks", "total_gross_pay", "total_hours_worked"} <= set(p) and len(p["weeks"]) in (2, 3)
            text = json.dumps(p)
            assert "employee_id" not in text and "hourly_rate" not in text and "name" not in text
            try:
                ca._range({"from": "2026-01-01", "to": "2026-12-31"})
                raise AssertionError("a year-long range was allowed")
            except ValueError:
                pass
        finally:
            conn.close()
        passed += 1; print("  payroll totals only ok")

        # ---- scrub
        s = ca.scrub({"name": "A", "email": "x@y.z", "contact_phone": "1", "photo_path": "/p", "thumb_url": "/u",
                      "people": [{"name": "B", "phone": "2"}], "address": "212 E 47th St"})
        assert s == {"name": "A", "people": [{"name": "B"}], "address": "212 E 47th St"}, s
        passed += 1; print("  scrub ok")

        # ---- History: saved chats, search, rename, remove (owner only); an empty chat is not history
        empty = a.post("/api/claude/chats", json={"surface": "office"}).get_json()["data"]["chat"]
        lst = a.get("/api/claude/chats?surface=office").get_json()["data"]
        assert [c["id"] for c in lst] == [chat["id"]] and lst[0]["asked"] == 2 and lst[0]["starred"] is False
        assert pm.patch(f"/api/claude/chats/{chat['id']}", json={"starred": True}).status_code == 404
        assert a.patch(f"/api/claude/chats/{chat['id']}", json={"title": "   "}).status_code == 400
        r = a.patch(f"/api/claude/chats/{chat['id']}", json={"title": "Thursday walkthrough", "starred": True})
        assert r.status_code == 200 and r.get_json()["data"]["chat"] == dict(r.get_json()["data"]["chat"], title="Thursday walkthrough", starred=True)
        assert [c["id"] for c in a.get("/api/claude/chats?surface=office&saved=1").get_json()["data"]] == [chat["id"]]
        assert [c["id"] for c in a.get("/api/claude/chats?surface=office&q=secret%20phrase").get_json()["data"]] == [chat["id"]]
        assert a.get("/api/claude/chats?surface=office&q=nothing-like-this").get_json()["data"] == []
        assert a.patch(f"/api/claude/chats/{empty['id']}", json={"archived": True}).get_json()["data"]["chat"]["archived"] is True
        r = a.patch(f"/api/claude/chats/{chat['id']}", json={"archived": True})
        assert r.get_json()["data"]["chat"]["archived"] is True and a.get("/api/claude/chats?surface=office").get_json()["data"] == []
        assert _q("SELECT COUNT(*) AS n FROM claude_action WHERE chat_id = ?", (chat["id"],))[0]["n"] >= 2   # decided cards kept (the unanswered one was purged)
        a.patch(f"/api/claude/chats/{chat['id']}", json={"archived": False})
        passed += 1; print("  history ok")

        # ---- the daily budget stops new turns
        os.environ["SSC_ASSISTANT_DAILY_CAP_USD"] = "0.01"
        r = a.post(f"/api/claude/chats/{chat['id']}/messages", json={"text": "more", "page": {"surface": "office"}})
        assert r.status_code == 429 and r.get_json()["cap_reached"] is True and "$" not in r.get_json()["error"]
        os.environ.pop("SSC_ASSISTANT_DAILY_CAP_USD")
        assert assistant.spent_today_micro(_db()) >= ca.spent_today_micro(_db()) > 0
        passed += 1; print("  budget ok")

        # ---- one conversation follows the person; pay keeps it on office pages (2026-10-22)
        fchat = a.post("/api/claude/chats", json={"surface": "office"}).get_json()["data"]["chat"]
        _script([{"text": "Here is today."}])
        r = a.post(f"/api/claude/chats/{fchat['id']}/messages", json={
            "text": "What needs me on @FR-CLAG today?", "page": {"surface": "office", "label": "Company dashboard › Today"},
            "quote": "Invoice 2031 is late; call 212-555-0199 or ap@example.com",
            "refs": [{"kind": "job", "id": "FR-CLAG", "tag": "FR-CLAG"}, {"kind": "client", "id": 3, "tag": "Not In Text"},
                     {"kind": "evil", "id": 1, "tag": "FR-CLAG"}]})
        assert r.status_code == 202, r.get_json()
        first = lambda cid: json.loads(_q("SELECT content FROM claude_chat_message WHERE chat_id = ? AND role = 'user' "  # noqa: E731
                                          "AND kind = 'text' ORDER BY id", (cid,))[-1]["content"])[0]["text"]
        head = first(fchat["id"])
        assert head.startswith("[Page: Company dashboard › Today; today ") and "job dashboard" not in head
        assert "[Quoted from the page: Invoice 2031 is late; call [phone] or [email]]" in head
        assert "[Points at: job FR-CLAG]\n" in head and "Not In Text" not in head and "evil" not in head
        v = a.get(f"/api/claude/chats/{fchat['id']}?surface=field").get_json()["data"]
        u = v["messages"][0]
        assert u["text"] == "What needs me on @FR-CLAG today?" and u["page"] == "Company dashboard › Today"
        assert u["quote"] == "Invoice 2031 is late; call [phone] or [email]" and v["chat"]["office_only"] is None
        assert fchat["id"] in [c["id"] for c in a.get("/api/claude/chats?surface=field").get_json()["data"]]
        assert a.get("/api/claude/chats/latest?surface=field").get_json()["data"]["chat"]["id"] == fchat["id"]
        # on a job dashboard, payroll is refused and the chat stays open there
        _script([{"tools": [{"name": "payroll_summary", "input": {}}]}, {"text": "That is on office pages."}])
        r = a.post(f"/api/claude/chats/{fchat['id']}/messages", json={
            "text": "and payroll?", "page": {"surface": "field", "label": "FR-CLAG · Job dashboard", "project_code": "FR-CLAG"}})
        assert r.status_code == 202, r.get_json()
        assert "job FR-CLAG; a job dashboard (seen in the field" in first(fchat["id"])
        res = [b for m in ca._history(_db(), fchat["id"]) if m["role"] == "user" for b in m["content"] if b.get("type") == "tool_result"]
        assert len(res) == 1 and res[0].get("is_error") and "office pages" in res[0]["content"]
        assert _q("SELECT office_only FROM claude_chat WHERE id = ?", (fchat["id"],))[0]["office_only"] is None
        # on an office page it runs, and from then on the chat opens on office pages only
        _script([{"tools": [{"name": "payroll_summary", "input": {}}]}, {"text": "Totals for the two weeks."}])
        assert a.post(f"/api/claude/chats/{fchat['id']}/messages", json={"text": "payroll now", "page": {"surface": "office"}}).status_code == 202
        assert _q("SELECT office_only FROM claude_chat WHERE id = ?", (fchat["id"],))[0]["office_only"] == "pay"
        assert _q("SELECT id FROM claude_chat_message WHERE chat_id = ? AND role = 'assistant' AND content LIKE ?",   # the boot backfill's match
                  (fchat["id"], '%"name": "payroll_summary"%'))
        r = a.get(f"/api/claude/chats/{fchat['id']}?surface=field")
        assert r.status_code == 403 and r.get_json()["locked"] == "pay" and "pay details" in r.get_json()["error"]
        r = a.post(f"/api/claude/chats/{fchat['id']}/messages", json={"text": "more", "page": {"surface": "field"}})
        assert r.status_code == 403 and r.get_json()["locked"] == "pay"
        assert fchat["id"] not in [c["id"] for c in a.get("/api/claude/chats?surface=field").get_json()["data"]]
        assert fchat["id"] in [c["id"] for c in a.get("/api/claude/chats?surface=office").get_json()["data"]]
        assert a.get(f"/api/claude/chats/{fchat['id']}?surface=office").get_json()["data"]["chat"]["office_only"] == "pay"
        lf = a.get("/api/claude/chats/latest?surface=field").get_json()["data"]
        assert lf is None or lf["chat"]["id"] != fchat["id"]
        assert a.get("/api/claude/chats/latest?surface=office").get_json()["data"]["chat"]["id"] == fchat["id"]
        # the person hid the page
        _script([{"text": "ok"}])
        a.post(f"/api/claude/chats/{fchat['id']}/messages", json={"text": "no page", "page": {"surface": "office", "label": "Secret page", "shared": False}})
        assert first(fchat["id"]).startswith("[Page: not shared; today ") and "Secret page" not in first(fchat["id"])
        assert a.get(f"/api/claude/chats/{fchat['id']}").get_json()["data"]["messages"][-2]["page"] is None
        # @: the same search Claude uses; nothing under 2 letters; signed-out refused
        assert app.test_client().get("/api/claude/mentions?q=ab").status_code == 401
        assert a.get("/api/claude/mentions?q=c").get_json()["data"] == []
        conn = _db()
        try:
            import crm
            org = crm.create_org(conn, name=f"Clag Mention Org {TAG}", relationship_type="client")
        finally:
            conn.close()
        ms = a.get("/api/claude/mentions?q=clag%20mention&surface=office").get_json()["data"]
        assert {"kind": "client", "label": "Client", "tag": f"Clag Mention Org {TAG}", "id": org, "sub": "client"} in ms, ms
        assert all("@" not in m["tag"] for m in a.get("/api/claude/mentions?q=clag").get_json()["data"])
        _x("DELETE FROM crm_organization WHERE id = ?", (org,))
        passed += 1; print("  follows you + office-only lock + quote / @ ok")

        # ---- the real request shape + fallback cleanup
        import anthropic
        seen = {}

        class _Blk:
            def __init__(self, d):
                self.d = d

            def model_dump(self, **kw):
                return dict(self.d)

        class _Resp:
            content = [_Blk({"type": "thinking", "thinking": "", "signature": "s1"}), _Blk({"type": "fallback", "from": {"model": "x"}}),
                       _Blk({"type": "text", "text": "ok"})]
            stop_reason, model = "end_turn", "claude-opus-5-5"
            usage = type("U", (), {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})()

        class _Client:
            def __init__(self, *a, **k):
                self.beta = type("B", (), {"messages": type("M", (), {"create": staticmethod(lambda **kw: seen.update(kw) or _Resp())})()})()

        real, fake = anthropic.Anthropic, os.environ.pop("SSC_ASSISTANT_FAKE")
        anthropic.Anthropic = _Client
        try:
            out = ca.call_model("sys", ca.tools_for("admin", "office"), [{"role": "user", "content": "hi"}])
            first = dict(seen)
            seen.clear()
            os.environ.update(SSC_CLAUDE_MODEL="claude-haiku-5-5", SSC_CLAUDE_EFFORT="medium")
            ca.call_model("sys", ca.tools_for("admin", "office"), [{"role": "user", "content": "hi"}])
        finally:
            anthropic.Anthropic = real
            os.environ["SSC_ASSISTANT_FAKE"] = fake
            os.environ.pop("SSC_CLAUDE_MODEL", None)
            os.environ.pop("SSC_CLAUDE_EFFORT", None)
        # a cheaper model by env: no refusal fallback (Haiku has none; sending it would be a 400)
        assert seen["model"] == "claude-haiku-5-5" and seen["output_config"] == {"effort": "medium"}
        assert "fallbacks" not in seen and "betas" not in seen
        seen = first
        assert out["content"] == [{"type": "text", "text": "ok"}], out["content"]
        assert seen["model"] == "claude-opus-5-5" and seen["output_config"] == {"effort": "low"} and seen["fallbacks"] == "default"
        assert seen["betas"] == ["server-side-fallback-2026-07-01"] and "thinking" not in seen and "tool_choice" not in seen
        assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert {t["name"] for t in seen["tools"]} == set(names("admin", "office"))
        assert all("Needs approval" in t["description"] for t in seen["tools"] if t["name"].startswith(("create_", "update_", "mark_", "change_")))
        passed += 1; print("  request shape ok")

        # ---- logs + pages
        logs = "\n".join(trap.lines)
        assert trap.lines and SECRET not in logs and "Clag smoke" not in logs and "212 E 47th" not in logs
        for f in ("company_portal.html", "company-dashboard.html", "dashboard-static.html", "account.html", "admin_users.html",
                  "admin_projects.html", "admin_labor_rates.html", "estimating.html"):
            assert "/files/static/js/claude_drawer.js?v=" in (ROOT / f).read_text(encoding="utf-8"), f
        assert "worker-app" not in "".join(p.name for p in ROOT.glob("*.html") if "claude_drawer.js" in p.read_text(encoding="utf-8", errors="ignore"))
        js = (ROOT / "static/js/claude_drawer.js").read_text(encoding="utf-8")
        assert "var FIELD = /^\\/projects\\//" in js and "Changes always wait for your approval" in js
        assert "clx-spend" not in js and "spent_today_usd" not in js and "id=\"clx-grip\"" in js and "ssc.claude.width" in js
        assert "Knows you are on" not in js and "function loadHistory()" in js and "\u2606 Save" in js
        cp = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        assert 'id="view-claude"' in cp and "claude: loadClaudeLog" in cp
        assert "['Claude approvals', '/company#claude']" in (ROOT / "static/js/company_nav.js").read_text(encoding="utf-8")
        passed += 1; print("  logs + pages ok")
    finally:
        os.environ.pop("SSC_ASSISTANT_FAKE", None)
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"smoke_claude_agent: {passed}/15 groups passed")
    return 0 if passed == 15 else 1


if __name__ == "__main__":
    sys.exit(main())
