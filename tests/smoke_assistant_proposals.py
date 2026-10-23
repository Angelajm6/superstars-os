#!/usr/bin/env python3
"""Claude on estimates, bids and proposals + the Proposals tab switch (accepted 2026-10-14):
the BEHAVIORAL guard for assistant.py and its page wiring.

Proves, through a Flask test client against an ISOLATED database (no real model call, ever):
  * gating: anonymous 401; pm / super 403; admin 200
  * without an Anthropic key: status says unavailable, a message is refused (503) and nothing
    is stored; saved conversations stay readable
  * a conversation belongs to one estimate; a proposal from another estimate is refused
  * a message stores the question and Claude's reply, records tokens and cost, titles the
    conversation from the first line, and returns the reply split into drafts
  * split_drafts: allowed fields become drafts, unknown fields stay text
  * what Claude reads: the estimate, its walkthrough notes, the proposal being edited, past
    proposals of the same type (other jobs); never the client contact email or phone
  * documents: only this estimate's PDF / JPG / PNG, at most 3; a ticked PDF is sent
  * the daily budget: once spent, messages are refused (429) and nothing is stored
  * the real request (fake client): the default model, medium effort, refusal fallback on,
    the instructions cached, no disabled thinking
  * logs carry ids / counts / cost only — never message text or client names
  * pages: the Pipeline sub-tab is gone; the Estimates / Bids / Proposals dropdown, the
    estimate page, the Claude panel on both pages; the Estimating editor opens /#estimates/<id>

Database: honors SSC_DB_URL like every gate suite; otherwise a throwaway SQLite snapshot from
the versioned migrations — it NEVER opens the live superstars.db.
Run:  venv/bin/python tests/smoke_assistant_proposals.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")   # the planted Claude reply is a test-only seam
import logging
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_assistant_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
for _k in ("ANTHROPIC_API_KEY", "SSC_ASSISTANT_FAKE", "SSC_ASSISTANT_MODEL", "SSC_ASSISTANT_DAILY_CAP_USD", "SSC_ASSISTANT_MONTHLY_BUDGET_USD", "SSC_ASSISTANT_USER_DAILY_USD", "SSC_CLAUDE_MODEL", "SSC_CLAUDE_EFFORT"):
    os.environ.pop(_k, None)

from flask import Flask  # noqa: E402

import assistant  # noqa: E402
import auth  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import proposals  # noqa: E402

TAG = "asst"
PW = "AssistantSmokePassword2026"
ORG = "Asst Smoke Harbor Owners"
CONTACT_EMAIL = "asst.client.contact@example.com"
CONTACT_PHONE = "212-555-0147"
SECRET_Q = "Draft the scope for the Asst Smoke parapet"


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
        assistant.ensure(conn)
        proposals.ensure_schema(conn)
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name = ?", (ORG,)).fetchall()]
        est_ids = [r["id"] for r in conn.execute("SELECT id FROM estimate WHERE building_address LIKE '% Asst Smoke %'").fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"
        tids = [r["id"] for r in conn.execute(f"SELECT id FROM assistant_thread WHERE estimate_id IN ({ph(est_ids)})", est_ids).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph(est_ids)})", est_ids).fetchall()]
        purge(conn, "assistant_message", "thread_id", tids)
        purge(conn, "assistant_thread", "id", tids)
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate_document", "estimate_id", est_ids)
        try:
            purge(conn, "walkthrough_report", "estimate_id", est_ids)
        except Exception:
            conn.rollback()
        purge(conn, "estimate", "id", est_ids)
        conn.execute("DELETE FROM building WHERE address_key LIKE '% asst smoke %'")
        if org_ids:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(org_ids)})", org_ids)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph(org_ids)})", org_ids)
        conn.execute("DELETE FROM crm_organization WHERE name = ?", (ORG,))
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, ids)
        conn.commit()
    finally:
        conn.close()


def _seed() -> dict:
    _cleanup()
    ids = {}
    conn = _db()
    try:
        for role in ("admin", "pm", "super"):
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (f"{TAG}.{role}@example.com", auth.hash_password(PW), role, f"Smoke {TAG} {role}"))
        ids["admin"] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.admin@example.com",)).fetchone()["id"]
        ids["org"] = crm.create_org(conn, name=ORG, relationship_type="client")
        ids["contact"] = crm.create_contact(conn, full_name="Asst Smoke Board Chair", org_id=ids["org"],
                                            email=CONTACT_EMAIL, phone=CONTACT_PHONE)
        bid = estimates.create_estimate(conn, est_type="FR", borough="MN", client_org_id=ids["org"],
                                        contact_id=ids["contact"], building_address="7 Asst Smoke Place",
                                        notes="East parapet bulging at floors 6 to 8.", inquiry_kind="bid",
                                        bid_due_date="2026-10-30", created_by=ids["admin"])
        other = estimates.create_estimate(conn, est_type="FR", borough="MN", client_org_id=ids["org"],
                                          building_address="9 Asst Smoke Place", created_by=ids["admin"])
        ids["bid"], ids["bid_code"], ids["other"] = bid["id"], bid["code"], other["id"]
        conn.commit()
        try:
            conn.execute("INSERT INTO walkthrough_report (estimate_id, note, created_by, created_at) VALUES (?,?,?,?)",
                         (bid["id"], "Three lintels rusted through on the east bay.", ids["admin"], "2026-10-08T10:00:00"))
            conn.commit()
            ids["walkthrough"] = True
        except Exception:
            conn.rollback()
            ids["walkthrough"] = False
        proposals.ensure_schema(conn)
        p = proposals.create_from_estimate(conn, bid["id"], created_by=ids["admin"])
        ids["prop"], ids["prop_number"] = p["id"], p["number"]
        past = proposals.create_from_estimate(conn, other["id"], created_by=ids["admin"])
        conn.execute("UPDATE proposal SET status = 'sent', scope_summary = ?, exclusions_text = ? WHERE id = ?",
                     ("House style scope: repoint and rebuild.", "Permits billed at cost.", past["id"]))
        conn.commit()
        ids["past_number"] = past["number"]
        # a PDF and a spreadsheet on the bid
        folder = estimates._DOC_BASE / bid["code"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "fisp.pdf").write_bytes(b"%PDF-1.4\n% smoke\n%%EOF\n")
        (folder / "takeoff.xlsx").write_bytes(b"PK\x03\x04smoke")
        for name, mime, title in (("fisp.pdf", "application/pdf", "FISP report"),
                                  ("takeoff.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Takeoff")):
            conn.execute("INSERT INTO estimate_document (estimate_id, category, title, doc_type, file_path, file_name, "
                         "file_size, mime, uploaded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (bid["id"], "other", title, name.split(".")[1].upper(), str(folder / name), name,
                          (folder / name).stat().st_size, mime, "2026-10-09T09:00:00"))
        conn.commit()
        ids["pdf"] = conn.execute("SELECT id FROM estimate_document WHERE estimate_id = ? AND title = 'FISP report'", (bid["id"],)).fetchone()["id"]
        ids["xlsx"] = conn.execute("SELECT id FROM estimate_document WHERE estimate_id = ? AND title = 'Takeoff'", (bid["id"],)).fetchone()["id"]
    finally:
        conn.close()
    return ids


def main() -> int:
    app = Flask("asst", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    trap = LogTrap()
    lg = logging.getLogger("ssc.assistant")
    lg.addHandler(trap)
    lg.setLevel(logging.INFO)

    def login(role):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.97.0.{len(role)}"})
        assert r.status_code == 200, (role, r.status_code)
        return c

    ids = _seed()
    passed = 0
    try:
        # ---- gating
        assert app.test_client().get("/api/assistant/status").status_code == 401
        for role in ("pm", "super"):
            c = login(role)
            assert c.get("/api/assistant/status").status_code == 403, role
            assert c.post("/api/assistant/threads", json={"estimate_id": ids["bid"]}).status_code == 403, role
        a = login("admin")
        passed += 1; print("  gating ok")

        # ---- no key: unavailable, refused, nothing stored
        st = a.get("/api/assistant/status").get_json()["data"]
        assert st["available"] is False and st["reason"] == "no_api_key" and st["model"] == "claude-opus-5-5" and assistant.cap_usd() is None and assistant.monthly_budget_usd() == 100.0
        assert "cap_usd" not in st and "spent_today_usd" not in st and st["limit_reached"] is False   # no dollar figure leaves the server
        r = a.post("/api/assistant/threads", json={"estimate_id": ids["bid"], "proposal_id": ids["prop"]})
        assert r.status_code == 201, r.get_json()
        tid = r.get_json()["data"]["id"]
        r = a.post(f"/api/assistant/threads/{tid}/messages", json={"text": SECRET_Q})
        assert r.status_code == 503 and r.get_json()["ai_available"] is False
        assert not _q("SELECT id FROM assistant_message WHERE thread_id = ?", (tid,))
        assert a.get(f"/api/assistant/threads/{tid}").status_code == 200
        passed += 1; print("  off without a key ok")

        # ---- ownership checks
        assert a.post("/api/assistant/threads", json={}).status_code == 400
        assert a.post("/api/assistant/threads", json={"estimate_id": 99999999}).status_code == 404
        r = a.post("/api/assistant/threads", json={"estimate_id": ids["other"], "proposal_id": ids["prop"]})
        assert r.status_code == 400 and "does not belong" in r.get_json()["error"]
        passed += 1; print("  ownership ok")

        # ---- a message with the planted reply
        os.environ["SSC_ASSISTANT_FAKE"] = ('Here is a first pass.\n<draft field="scope">• Rebuild 3 lintels\n• Repoint east parapet</draft>\n'
                                            '<draft field="price">$1</draft>\nTell me the quantities.')
        assert a.get("/api/assistant/status").get_json()["data"]["available"] is True
        assert a.post(f"/api/assistant/threads/{tid}/messages", json={"text": "   "}).status_code == 400
        assert a.post(f"/api/assistant/threads/{tid}/messages", json={"text": "x" * (assistant.MAX_TEXT + 1)}).status_code == 400
        r = a.post(f"/api/assistant/threads/{tid}/messages", json={"text": SECRET_Q + "\nKeep it short.", "proposal_id": ids["prop"]})
        assert r.status_code == 200, r.get_json()
        d = r.get_json()["data"]
        msgs = d["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[0]["author"] == f"Smoke {TAG} admin"
        parts = msgs[1]["parts"]
        assert [p["type"] for p in parts] == ["text", "draft", "text", "text"], parts
        assert parts[1] == {"type": "draft", "field": "scope", "text": "• Rebuild 3 lintels\n• Repoint east parapet"}
        assert d["thread"]["title"] == SECRET_Q
        row = _q("SELECT * FROM assistant_message WHERE thread_id = ? AND role = 'assistant'", (tid,))[0]
        assert row["input_tokens"] == 1200 and row["output_tokens"] == 300
        assert row["cost_micro"] == assistant.cost_micro("claude-opus-5-5", {"input_tokens": 1200, "output_tokens": 300}) == 10800
        assert "spent_today_usd" not in d and "cap_usd" not in d
        assert assistant.spent_today_micro(_db()) >= 10800
        assert a.get("/api/assistant/counts").get_json()["data"][str(ids["bid"])] == 1
        lst = a.get(f"/api/assistant/threads?estimate_id={ids['bid']}&proposal_id={ids['prop']}").get_json()
        assert len(lst["data"]) == 1 and lst["data"][0]["messages"] == 2
        assert lst["reads"][0] == f"Bid {ids['bid_code']}" and f"Proposal {ids['prop_number']}" in lst["reads"]
        assert "1 past proposal" in lst["reads"]
        assert {x["id"]: x["attachable"] for x in lst["documents"]} == {ids["pdf"]: True, ids["xlsx"]: False}
        passed += 1; print("  message + drafts + cost ok")

        # ---- split_drafts edge cases
        assert assistant.split_drafts("plain") == [{"type": "text", "text": "plain"}]
        assert assistant.split_drafts('<draft field="terms">Net 30</draft>') == [{"type": "draft", "field": "terms", "text": "Net 30"}]
        assert assistant.split_drafts('<draft field="bogus">x</draft>') == [{"type": "text", "text": "x"}]
        passed += 1; print("  split_drafts ok")

        # ---- what Claude reads
        conn = _db()
        try:
            est = assistant._estimate_ctx(conn, ids["bid"])
            ctx = assistant.context_text(conn, est, assistant._proposal(conn, ids["prop"], ids["bid"]))
        finally:
            conn.close()
        assert ids["bid_code"] in ctx and ORG in ctx and "7 Asst Smoke Place" in ctx and "Bid due: 2026-10-30" in ctx
        assert f"PROPOSAL BEING EDITED: {ids['prop_number']}" in ctx
        assert ids["past_number"] in ctx and "House style scope" in ctx
        if ids["walkthrough"]:
            assert "Three lintels rusted through" in ctx
        assert CONTACT_EMAIL not in ctx and CONTACT_PHONE not in ctx and "555" not in ctx
        passed += 1; print(f"  context ok (no contact email / phone; walkthrough notes checked: {ids['walkthrough']})")

        # ---- documents
        for bad, why in (([ids["xlsx"]], "PDF, JPG and PNG"), ([99999999], "not on this estimate"),
                         ([ids["pdf"]] * 4, "at most 3")):
            r = a.patch(f"/api/assistant/threads/{tid}", json={"doc_ids": bad})
            assert r.status_code == 400 and why in r.get_json()["error"], (bad, r.get_json())
        r = a.patch(f"/api/assistant/threads/{tid}", json={"doc_ids": [ids["pdf"]], "proposal_id": ids["prop"]})
        assert r.status_code == 200 and "FISP report" in r.get_json()["data"]["reads"]
        conn = _db()
        try:
            blocks = assistant._doc_blocks(conn, ids["bid"], [ids["pdf"]])
            assert len(blocks) == 1 and blocks[0]["type"] == "document" and blocks[0]["source"]["media_type"] == "application/pdf"
            assert assistant._doc_blocks(conn, ids["other"], [ids["pdf"]]) == []   # another estimate's thread cannot pull it
        finally:
            conn.close()
        passed += 1; print("  documents ok")

        # ---- the real request shape, through a fake Anthropic client
        import anthropic
        seen = {}

        class _Resp:
            content = [type("B", (), {"type": "text", "text": "ok"})()]
            stop_reason = "end_turn"
            model = "claude-opus-5-5"
            usage = type("U", (), {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0})()

        class _Client:
            def __init__(self, *a, **k):
                self.beta = type("Beta", (), {"messages": type("M", (), {"create": staticmethod(lambda **kw: seen.update(kw) or _Resp())})()})()

        real, fake_seam = anthropic.Anthropic, os.environ.pop("SSC_ASSISTANT_FAKE")
        anthropic.Anthropic = _Client
        try:
            out = assistant._call_model([{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}], [{"role": "user", "content": "hi"}])
        finally:
            anthropic.Anthropic = real
            os.environ["SSC_ASSISTANT_FAKE"] = fake_seam
        assert out["text"] == "ok" and out["usage"]["input_tokens"] == 10
        assert seen["model"] == "claude-opus-5-5" and seen["output_config"] == {"effort": "medium"}
        assert seen["fallbacks"] == "default" and seen["betas"] == ["server-side-fallback-2026-07-01"]
        assert "thinking" not in seen and seen["max_tokens"] == assistant.MAX_TOKENS
        assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}
        passed += 1; print("  request shape ok")

        # ---- the daily budget
        os.environ["SSC_ASSISTANT_DAILY_CAP_USD"] = "0.01"
        n_before = len(_q("SELECT id FROM assistant_message WHERE thread_id = ?", (tid,)))
        r = a.post(f"/api/assistant/threads/{tid}/messages", json={"text": "One more?"})
        assert r.status_code == 429 and r.get_json()["cap_reached"] is True, r.get_json()
        assert "$" not in r.get_json()["error"] and "usage limit" in r.get_json()["error"]
        assert len(_q("SELECT id FROM assistant_message WHERE thread_id = ?", (tid,))) == n_before
        os.environ["SSC_ASSISTANT_DAILY_CAP_USD"] = "0"
        assert a.get("/api/assistant/status").get_json()["data"]["available"] is True   # the seam stays on in tests
        os.environ.pop("SSC_ASSISTANT_DAILY_CAP_USD")
        passed += 1; print("  daily budget ok")

        # ---- the monthly budget, the per-person daily guard, the admin alerts (2026-10-22)
        row = _q("SELECT billed_user_id FROM assistant_message WHERE thread_id = ? AND role = 'assistant'", (tid,))[0]
        assert row["billed_user_id"] == ids["admin"]
        conn = _db()
        try:
            pm_id = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.pm@example.com",)).fetchone()["id"]
            assert assistant.limit_reason(conn, ids["admin"]) is None
            os.environ["SSC_ASSISTANT_USER_DAILY_USD"] = "0.01"
            assert assistant.limit_reason(conn, ids["admin"]) == assistant.USER_LIMIT_MSG
            assert assistant.limit_reason(conn, pm_id) is None and assistant.limit_reason(conn) is None   # only the one who spent
        finally:
            conn.close()
        r = a.post(f"/api/assistant/threads/{tid}/messages", json={"text": "One more?"})
        assert r.status_code == 429 and r.get_json()["error"] == assistant.USER_LIMIT_MSG and "$" not in r.get_json()["error"]
        st = a.get("/api/assistant/status").get_json()["data"]
        assert st["limit_reached"] is True and st["limit_message"] == assistant.USER_LIMIT_MSG
        os.environ.pop("SSC_ASSISTANT_USER_DAILY_USD")
        os.environ["SSC_ASSISTANT_MONTHLY_BUDGET_USD"] = "0.01"
        r = a.post(f"/api/assistant/threads/{tid}/messages", json={"text": "One more?"})
        assert r.status_code == 429 and r.get_json()["error"] == assistant.MONTH_LIMIT_MSG and "$" not in r.get_json()["error"]
        os.environ["SSC_ASSISTANT_MONTHLY_BUDGET_USD"] = "0"
        assert assistant.unavailable_reason() is None and assistant.monthly_budget_usd() == 0   # the seam stays on in tests
        conn = _db()
        try:
            spent = assistant.spent_month_micro(conn)
            assert spent >= 10800
            os.environ["SSC_ASSISTANT_MONTHLY_BUDGET_USD"] = str(spent * 1.6 / 1_000_000)   # ~62% used
            alerts = lambda k: _q("SELECT id FROM notification_log WHERE kind = ? AND recipient_user_id = ?", (k, ids["admin"]))
            assistant.check_budget_alerts(conn)
            assistant.check_budget_alerts(conn)   # once per step per month
            assert len(alerts("claude_budget_50")) == 1 and not alerts("claude_budget_80") and not alerts("claude_budget_100")
            assert not _q("SELECT id FROM notification_log WHERE kind LIKE 'claude_budget_%' AND recipient_user_id = ?", (pm_id,))
        finally:
            conn.close()
            os.environ.pop("SSC_ASSISTANT_MONTHLY_BUDGET_USD")
        passed += 1; print("  monthly budget + per-person guard + alerts ok")

        # ---- the conversation request: system + documents cached an hour, the record on the newest message
        real, fake_seam = anthropic.Anthropic, os.environ.pop("SSC_ASSISTANT_FAKE")
        anthropic.Anthropic, os.environ["ANTHROPIC_API_KEY"] = _Client, "test-not-a-real-key"
        try:
            seen.clear()
            r = a.post(f"/api/assistant/threads/{tid}/messages", json={"text": "And the schedule?", "proposal_id": ids["prop"]})
            assert r.status_code == 200, r.get_json()
        finally:
            anthropic.Anthropic = real
            os.environ["SSC_ASSISTANT_FAKE"] = fake_seam
            os.environ.pop("ANTHROPIC_API_KEY")
        hour = {"type": "ephemeral", "ttl": "1h"}
        assert "cache_control" not in seen and len(seen["system"]) == 1 and seen["system"][0]["cache_control"] == hour
        msgs = seen["messages"]
        assert msgs[0]["content"][0]["type"] == "document" and msgs[0]["content"][0]["cache_control"] == hour
        assert msgs[-2]["role"] == "assistant" and msgs[-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
        assert msgs[-1]["content"][0]["text"].startswith("[Platform note] The record") and f"PROPOSAL BEING EDITED: {ids['prop_number']}" in msgs[-1]["content"][0]["text"]
        assert msgs[-1]["content"][-1]["text"] == "And the schedule?"
        assert not any("PROPOSAL BEING EDITED" in b.get("text", "") for m in msgs[:-1] for b in m["content"])
        assert sum(1 for m in msgs for b in m["content"] if "cache_control" in b) + 1 <= 4   # breakpoint slots
        passed += 1; print("  conversation caching ok")

        # ---- logs: ids / counts / cost only
        logs = "\n".join(trap.lines)
        assert trap.lines and "Asst Smoke" not in logs and "lintels" not in logs and ORG not in logs and CONTACT_EMAIL not in logs
        passed += 1; print("  logs ok")

        # ---- pages
        nav = (ROOT / "static/js/company_nav.js").read_text(encoding="utf-8")
        assert "'Pipeline'" not in nav and "['Estimates & proposals', '/company#proposals', COMPANY]" in nav
        cp = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        assert 'class="prx-seg" role="tablist" aria-label="Show" id="pr-kind"' in cp   # 2026-10-23: a switch, not a dropdown
        for opt in ('data-kind="proposal">Proposals', 'data-kind="estimate">Estimates', 'data-kind="bid">Bids'):
            assert opt in cp, opt
        assert "/#estimates\"" not in cp and "href=\"/#estimates\"" not in cp
        assert "function loadEstimateDetail(" in cp and "function openNewEstimate(" in cp and 'id="est-new-overlay"' in cp
        assert cp.count("mountClaude($('claude-host')") == 2
        assert "cl-spend" not in cp and "cl-meter" not in cp and "clSpend" not in cp
        assert "inquiry_kind: ESTNEW.kind === 'bid' ? 'bid' : 'undetermined'" in cp
        cd = (ROOT / "company-dashboard.html").read_text(encoding="utf-8")
        assert "open: function(id){ wireOnce(); openEst(id); }" in cd and "estimates\\/(\\d+)" in cd
        passed += 1; print("  pages ok")
    finally:
        os.environ.pop("SSC_ASSISTANT_FAKE", None)
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"smoke_assistant_proposals: {passed}/13 groups passed")
    return 0 if passed == 13 else 1


if __name__ == "__main__":
    sys.exit(main())
