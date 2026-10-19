#!/usr/bin/env python3
"""Today dashboard (accepted prototype 2026-10-19): the BEHAVIORAL guard for today_feed.py,
proposal_template.py and proposal_drafts.py.

Proves, through a Flask test client against an ISOLATED database (no Google, no model call, no PDF engine):
  * gating: anonymous 401, pm 403, admin / c_suite 200 on every new route
  * the four cards: active jobs by type, on site now, crew from the last workday not in yet, bills due
  * Your day: my task due today and today's bill, in order; Needs you: the late bill, the expiring
    insurance, the missing crew, my task, Claude's drafted proposal — each with the page it opens
  * Later / Dismiss hide a card for that person only; undo brings it back
  * weather reads a canned forecast (never the network) and names the rain / gusts
  * the template: Claude's HTML is stripped of scripts, handlers and outside URLs; a missing placeholder
    fails with a plain message; once ready, every proposal PDF is drawn in it
  * a drafted proposal: preview in the template, edits saved, Claude's change applied (fake model),
    and Approve & send creates the client + estimate + proposal, attaches the PDF to a reply in the
    same Gmail thread (In-Reply-To) and logs the card as approved; another person can't open it

Run:  venv/bin/python tests/smoke_today_dashboard.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import email
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_today_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

MAIL = Path(_TMP) / "mail"
os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(MAIL), "SSC_GMAIL_POLL": "0",
                   "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(), "SSC_ASSISTANT_FAKE": "1"})
os.environ.pop("ANTHROPIC_API_KEY", None)

from flask import Flask  # noqa: E402

import assistant  # noqa: E402
import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import company_portal  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import inbox  # noqa: E402
import payables  # noqa: E402
import proposal_drafts  # noqa: E402
import proposal_template  # noqa: E402
import proposals  # noqa: E402
import team_items  # noqa: E402
import today_feed  # noqa: E402

TAG = "tdy"
PW = "TodaySmokePassword2026"
JOB = "FR-MN-TDY1"
ORG_NEW = "Tdy Smoke Rosen Properties"
VENDOR = "Tdy Smoke Scaffold"
DOC = "Tdy smoke GL policy"
TODAY = date.today()


def _db():
    return db_layer.connect(pragma_fk=True)


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        for m in (ca, inbox, team_items, today_feed):
            m.ensure(conn) if hasattr(m, "ensure") else None
        proposals.ensure_schema(conn)
        payables.ensure_schema(conn)
        company_portal.ensure_schema(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        mids = [r["id"] for r in conn.execute(f"SELECT id FROM mail_message WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "mail_link", "message_id", mids)
        purge(conn, "mail_message", "id", mids)
        for t in ("gmail_link", "mail_sendas", "today_hidden"):
            purge(conn, t, "user_id", uids)
        chats = [r["id"] for r in conn.execute(f"SELECT id FROM claude_chat WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "claude_action", "chat_id", chats)
        purge(conn, "claude_chat_message", "chat_id", chats)
        purge(conn, "claude_chat", "id", chats)
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name = ?", (ORG_NEW,)).fetchall()]
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph(ests)})", ests).fetchall()]
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate_document", "estimate_id", ests)
        purge(conn, "estimate", "id", ests)
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM crm_organization WHERE name = ?", (ORG_NEW,))
        conn.execute("DELETE FROM crm_task WHERE title LIKE 'Tdy smoke%'")
        conn.execute("DELETE FROM payable WHERE vendor = ?", (VENDOR,))
        conn.execute("DELETE FROM company_document WHERE title = ?", (DOC,))
        conn.execute("DELETE FROM sign_in_log WHERE project_code = ?", (JOB,))
        conn.execute("DELETE FROM employees WHERE employee_id LIKE 'TDY-%'")
        conn.execute("DELETE FROM projects WHERE project_code = ?", (JOB,))
        conn.execute("DELETE FROM app_settings WHERE key = 'proposal_template'")
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
        for role in ("admin", "c_suite", "pm"):
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, "
                         "is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 0)",
                         (f"{TAG}.{role}@superstarscontracting.com", auth.hash_password(PW), role, f"Tdy {role.title()}"))
        conn.commit()
        ids.update({r["role"]: r["id"] for r in conn.execute("SELECT id, role FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()})
        conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, ?, ?, 'active')", (JOB, "Tdy smoke job", "1 Tdy Pl"))
        for e in ("TDY-1", "TDY-2", "TDY-3"):
            conn.execute("INSERT INTO employees (employee_id, name) VALUES (?, ?)", (e, "Tdy Worker " + e[-1]))
        prev = today_feed._previous_workday(TODAY).isoformat()
        for e in ("TDY-1", "TDY-2", "TDY-3"):
            conn.execute("INSERT INTO sign_in_log (date, employee_id, project_code, time_in) VALUES (?, ?, ?, '07:00')", (prev, e, JOB))
        conn.execute("INSERT INTO sign_in_log (date, employee_id, project_code, time_in) VALUES (?, 'TDY-1', ?, '07:00')", (TODAY.isoformat(), JOB))
        conn.execute("INSERT INTO sign_in_log (date, employee_id, project_code, time_in, time_out) VALUES (?, 'TDY-2', ?, '07:00', '11:00')",
                     (TODAY.isoformat(), JOB))
        conn.commit()
        ids["bill_today"] = payables.create(conn, {"vendor": VENDOR, "amount": "4820", "due_at": TODAY.isoformat(), "description": "Shed rental"})["id"]
        ids["bill_late"] = payables.create(conn, {"vendor": VENDOR, "amount": "1000", "due_at": (TODAY - timedelta(days=3)).isoformat()})["id"]
        ids["bill_soon"] = payables.create(conn, {"vendor": VENDOR, "amount": "250", "due_at": (TODAY + timedelta(days=4)).isoformat()})["id"]
        doc, err = company_portal.create_company_document(conn, {"category": "insurance_gl", "title": DOC,
                                                                 "expiry_date": (TODAY + timedelta(days=4)).isoformat()})
        assert not err, err
        ids["doc"] = doc["id"] if isinstance(doc, dict) else doc
        ids["task"] = team_items.create(conn, {"kind": "task", "title": "Tdy smoke call the broker", "due_date": TODAY.isoformat(),
                                               "due_time": "10:30", "assignee_user_id": ids["admin"]}, actor=ids["admin"])
        # an email asking for a price, and Claude's drafted proposal for it
        conn.execute("INSERT INTO gmail_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) "
                     "VALUES (?, ?, ?, 'active', ?, ?)", (ids["admin"], f"{TAG}.admin@superstarscontracting.com",
                                                          secret_box.encrypt_text("r"), "2026-10-19T08:00:00",
                                                          "openid email https://www.googleapis.com/auth/gmail.send"))
        e_ = inbox._enc
        conn.execute("INSERT INTO mail_message (user_id, gmail_id, thread_id, rfc_id_enc, direction, received_at, from_enc, to_enc, "
                     "cc_enc, subject_enc, body_enc, snippet_enc, needs_action, status, ai_state, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,'new','done',?)",
                     (ids["admin"], "g-tdy-1", "thr-tdy-1", e_("<tdy-1@mail.example>"), "in", datetime.now().isoformat(timespec="seconds"),
                      e_("Dana Rosen <dana@rosen-tdy.example>"), e_(f"{TAG}.admin@superstarscontracting.com"), e_(""),
                      e_("Pricing for east wall repairs"), e_("Could you send pricing for repointing and 6 lintels?"), e_("Could you"),
                      datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        ids["mail"] = conn.execute("SELECT id FROM mail_message WHERE gmail_id = 'g-tdy-1'").fetchone()["id"]
        m = inbox.message(conn, ids["mail"])
        chat = inbox._mail_chat(conn, m)
        inp = {"mail_id": ids["mail"], "client_name": ORG_NEW, "est_type": "FR", "borough": "MN",
               "building_address": "410 E 58th St", "title": "East wall repairs", "scope_summary": "Repoint and replace 6 lintels.",
               "lines": [{"description": "Repointing", "qty": 1850, "unit": "sf", "unit_price": 15},
                         {"description": "Lintels", "qty": 6, "unit": "ea", "unit_price": 2400}],
               "message": "Hi Dana, our proposal is attached. Arun"}
        ctx = ca.Ctx(conn, {"id": ids["admin"], "role": "admin"}, "office")
        ca.BY_NAME["draft_proposal"].run(ctx, inp)
        conn.execute("INSERT INTO claude_action (chat_id, message_id, tool_use_id, user_id, tool, input, summary, status, created_at) "
                     "VALUES (?, 0, 'tu-tdy', ?, 'draft_proposal', ?, 'Proposal from an email', 'pending', ?)",
                     (chat, ids["admin"], json.dumps(inp), datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        ids["action"] = conn.execute("SELECT MAX(id) AS m FROM claude_action WHERE user_id = ?", (ids["admin"],)).fetchone()["m"]
    finally:
        conn.close()
    return ids


def main() -> int:
    app = Flask("tdy", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    ca.register(app)
    inbox.register(app)
    today_feed.register(app)
    proposal_template.register(app)
    proposal_drafts.register(app)

    def login(role):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@superstarscontracting.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.95.0.{len(role)}"})
        assert r.status_code == 200, (role, r.status_code)
        return c

    ids = _seed()
    passed = 0
    try:
        # ---- gating
        anon = app.test_client()
        a, cs, pm = login("admin"), login("c_suite"), login("pm")
        for path in ("/api/company/portal/today", "/api/company/portal/today/weather", "/api/proposal-template",
                     f"/api/proposal-drafts/{ids['action']}"):
            assert anon.get(path).status_code == 401, path
            assert pm.get(path).status_code == 403, path
        assert pm.post("/api/company/portal/today/hide", json={"key": "x"}).status_code == 403
        assert cs.get(f"/api/proposal-drafts/{ids['action']}").status_code == 404, "only the person whose email it is"
        passed += 1; print("  gating ok")

        # ---- the feed
        d = a.get("/api/company/portal/today").get_json()["data"]
        c = d["cards"]
        assert c["jobs"]["active"] >= 1 and any(t["code"] == "FR" for t in c["jobs"]["by_type"])
        site = next(s for s in c["crew"]["sites"] if s["project_code"] == JOB)
        assert site["on_site"] == 1, site
        late_check = TODAY.weekday() < 5 and datetime.now().hour >= today_feed.CREW_CHECK_HOUR
        assert [m["employee_id"] for m in site["missing"]] == (["TDY-3"] if late_check else []), site
        assert c["payments"]["due_now_value"] >= 5820 and c["payments"]["overdue_count"] >= 1 and c["payments"]["soon_value"] >= 250
        day = d["day"]
        keys = [x["key"] for x in day]
        assert f"item:{ids['task']}" in keys and f"bill:{ids['bill_today']}" in keys, keys
        assert keys.index(f"bill:{ids['bill_today']}") < keys.index(f"item:{ids['task']}"), "all-day first, then by time"
        needs = {x["key"]: x for x in d["needs"]}
        assert needs[f"bill:{ids['bill_late']}"]["urgency"] == "now" and needs[f"bill:{ids['bill_late']}"]["href"] == "#finance"
        assert needs[f"bill:{ids['bill_today']}"]["urgency"] == "today" and needs[f"bill:{ids['bill_soon']}"]["urgency"] == "week"
        doc = needs[f"doc:company_document:{ids['doc']}"]
        assert doc["href"] == "#company" and doc["actions"][0]["type"] == "claude" and "expires" in doc["title"]
        assert needs[f"task:{ids['task']}"]["actions"][0]["type"] == "done"
        prop = needs[f"claude:{ids['action']}"]
        assert prop["tool"] == "draft_proposal" and prop["actions"][0]["type"] == "proposal" and prop["href"] == f"#inbox/{ids['mail']}"
        assert prop["draft"]["total"] == "$42,150" and prop["urgency"] == "now"
        if late_check:
            assert needs[f"crew:{JOB}:{TODAY.isoformat()}"]["href"] == "/projects/" + JOB
        order = [x["urgency"] for x in d["needs"]]
        assert order == sorted(order, key=lambda u: {"now": 0, "today": 1, "week": 2}[u]), order
        passed += 1; print("  cards, Your day, Needs you ok")

        # ---- Later / Dismiss / undo, per person
        k = f"bill:{ids['bill_soon']}"
        assert a.post("/api/company/portal/today/hide", json={"key": k, "for": "later"}).status_code == 200
        assert k not in {x["key"] for x in a.get("/api/company/portal/today").get_json()["data"]["needs"]}
        assert k in {x["key"] for x in cs.get("/api/company/portal/today").get_json()["data"]["needs"]}, "only for that person"
        a.post("/api/company/portal/today/hide", json={"key": k, "for": "undo"})
        assert k in {x["key"] for x in a.get("/api/company/portal/today").get_json()["data"]["needs"]}
        assert a.post("/api/company/portal/today/hide", json={"key": k, "for": "forever"}).status_code == 400
        passed += 1; print("  hide ok")

        # ---- weather (canned forecast)
        t = TODAY.isoformat()
        canned = {"days": [{"date": t, "label": "Rain", "temp_max": 61, "temp_min": 50, "gust_mph": 27, "precip_prob": 80,
                            "heads_up": ["Gusts up to 27 mph, check rigging and loose material"],
                            "hours": [{"hour": 7, "temp": 52, "precip_prob": 10}, {"hour": 13, "temp": 58, "precip_prob": 70}]}]}
        conn = _db()
        try:
            w = today_feed.weather(conn, TODAY, fetch=lambda lat, lng: canned)
        finally:
            conn.close()
        assert w["available"] and "Rain from 1 PM" in w["headline"] and "gusts 27 mph" in w["headline"] and w["warn"]
        passed += 1; print("  weather ok")

        # ---- the template
        dirty = ("Here it is:\n```html\n<!DOCTYPE html><html><head><link rel='stylesheet' href='https://evil.example/x.css'>"
                 "<style>@import url(https://evil.example/f.css); body{background:url(https://evil.example/b.png)}</style>"
                 "<script>alert(1)</script></head><body onload=\"steal()\"><img src=\"https://evil.example/t.gif\">"
                 "<a href=\"javascript:x()\">x</a>{{scope}}<table>{{line_items_rows}}</table>{{total}}</body></html>\n```")
        clean = proposal_template.sanitize(dirty)
        for bad in ("<script", "onload", "evil.example", "javascript:", "@import", "<link"):
            assert bad not in clean, bad
        assert proposal_template.check(clean) is None
        assert "missing: line_items_rows" in proposal_template.check("<html><body>{{scope}} {{total}}</body></html>")
        conn = _db()
        try:
            st = proposal_template.start_read(conn, "Our proposal.pdf", b"%PDF-1.4 tdy", ids["admin"], background=False)
        finally:
            conn.close()
        assert st["active"] and st["state"] == "ready" and st["name"] == "Our proposal.pdf", st
        r = a.get("/api/proposal-template/preview")
        assert r.status_code == 200 and "PROPOSAL SAMPLE-P1" in r.get_data(as_text=True) and "sandbox" in r.headers["Content-Security-Policy"]
        assert a.post("/api/proposal-template", data={}).status_code == 400
        conn = _db()
        try:
            n = conn.execute("SELECT COUNT(*) FROM claude_chat WHERE user_id = ? AND source = 'system'", (ids["admin"],)).fetchone()[0]
        finally:
            conn.close()
        assert n == 0, "the fake model records no usage"
        passed += 1; print("  template ok")

        # ---- the drafted proposal: view, preview in the template, save, Claude's change
        v = a.get(f"/api/proposal-drafts/{ids['action']}").get_json()["data"]
        assert v["new_client"] and v["client_name"] == ORG_NEW and v["email"]["from_email"] == "dana@rosen-tdy.example"
        assert v["template"]["active"] and v["reply_subject"] == "Re: Pricing for east wall repairs"
        vals = dict(v["values"], title="East wall repairs (rev)")
        pv = a.post(f"/api/proposal-drafts/{ids['action']}/preview", json={"values": vals}).get_json()["data"]
        assert "PROPOSAL DRAFT" in pv["html"] and "East wall repairs (rev)" in pv["html"] and pv["total"] == 42150
        bad = a.post(f"/api/proposal-drafts/{ids['action']}/preview", json={"values": dict(vals, lines=[])})
        assert bad.status_code == 400 and "priced line" in bad.get_json()["error"]
        sv = a.post(f"/api/proposal-drafts/{ids['action']}/save", json={"values": vals}).get_json()["data"]
        assert sv["values"]["title"] == "East wall repairs (rev)"
        rv = a.post(f"/api/proposal-drafts/{ids['action']}/revise", json={"values": vals, "instruction": "add a 10% contingency"}).get_json()["data"]
        assert rv["values"]["lines"][-1]["description"] == "Contingency (10%)" and rv["total"] == 46365
        passed += 1; print("  review panel ok")

        # ---- Approve & send: client + estimate + proposal, PDF attached in the same thread
        real_render, real_file = proposals.render_pdf, proposals.pdf_file
        pdf = Path(_TMP) / "p.pdf"
        pdf.write_bytes(b"%PDF-1.4 tdy proposal")
        proposals.render_pdf = lambda conn, pid, actor=None: {"ok": True}
        proposals.pdf_file = lambda conn, pid: pdf
        try:
            r = a.post(f"/api/proposal-drafts/{ids['action']}/send", json={"values": rv["values"]})
        finally:
            proposals.render_pdf, proposals.pdf_file = real_render, real_file
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["data"]["status"] == "approved"
        emls = sorted(MAIL.glob("gmail-reply-*.eml"))
        assert len(emls) == 1
        msg = email.message_from_bytes(emls[0].read_bytes())
        assert msg["In-Reply-To"] == "<tdy-1@mail.example>" and msg["To"].endswith("<dana@rosen-tdy.example>")
        atts = [p for p in msg.walk() if p.get_content_disposition() == "attachment"]
        assert len(atts) == 1 and atts[0].get_content_type() == "application/pdf" and atts[0].get_filename().endswith(".pdf")
        assert atts[0].get_payload(decode=True) == b"%PDF-1.4 tdy proposal"
        conn = _db()
        try:
            p = conn.execute("SELECT p.* FROM proposal p JOIN crm_organization o ON o.id = p.client_org_id WHERE o.name = ?", (ORG_NEW,)).fetchone()
            assert p and p["status"] == "sent" and float(p["price"]) == 46365 and p["recipient_email"] == "dana@rosen-tdy.example"
            assert conn.execute("SELECT COUNT(*) FROM proposal_line WHERE proposal_id = ?", (p["id"],)).fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM mail_link WHERE message_id = ? AND entity_type = 'proposal'", (ids["mail"],)).fetchone()[0] == 1
        finally:
            conn.close()
        assert a.post(f"/api/proposal-drafts/{ids['action']}/send", json={}).status_code == 409, "decided once"
        assert f"claude:{ids['action']}" not in {x["key"] for x in a.get("/api/company/portal/today").get_json()["data"]["needs"]}
        passed += 1; print("  approve & send ok")

        # ---- the page carries the Today dashboard
        html = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        for needle in ('id="td-needs"', 'id="td-day"', 'id="pd-drawer"', 'id="pd-frame" class="pd-frame" title="The proposal PDF" sandbox="allow-same-origin" referrerpolicy="no-referrer"',
                       "api('/today')", "api('/today/weather')", "'/proposal-drafts/' + PD.id + '/send'", "setInterval(", 'id="cc-week-range"',
                       "calNowLabel()", "'Mon – Sun'", "window.SSC_CLAUDE.ask("):
            assert needle in html, needle
        assert '>Today</button>' not in html, "the calendar shows the week, not 'Today'"
        js = (ROOT / "static/js/claude_drawer.js").read_text(encoding="utf-8")
        assert "ask: ask" in js
        passed += 1; print("  page ok")
    finally:
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: today dashboard ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
