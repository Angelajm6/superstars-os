#!/usr/bin/env python3
"""Proposals 2026-10-22: one amount everywhere, revisions only an admin approves, expiry follow-ups, reply in a thread.

Proves, through a Flask test client against an ISOLATED database (fake model, the test mail folder, a fake PDF engine
that writes the HTML it is given, so the PDF's text can be read):
  * amounts: the stored price, the editor's total, the document, the downloaded PDF, the emailed PDF and the deposit are
    the same figure, with cents that need rounding; a price set apart from the lines is ignored; an edit in the same
    second as the last PDF still gets a new PDF (the old timestamp check served the stale one); the older layout's price
    is the sum of the rounded line amounts
  * send: the Send pop-up's threads with the client (and search), the preview of a reply (recipients, subject, thread);
    nothing is sent without confirm, or when the total changed after the review; a reply goes in the thread
    (In-Reply-To), to the reviewed people, with the same PDF the download gives; the sent copy keeps its fixed wording
  * revisions: anyone on proposals asks; only an admin approves or rejects (403 for C-suite on the routes, and
    decide_revision itself refuses); approval drafts vN+1 while the sent copy stays live and unchanged; sending the
    revision supersedes it; a rejected request changes nothing; withdraw is the asker's (or an admin's); a revision
    can't go out once the client signed the version it replaces; C-suite can't make a new version directly
  * in-app alerts: a sent proposal near (or just past) its valid-until date is a "Needs you" card for the sender and the
    admins, with Open + Draft a follow-up (Claude, nothing sent); pending revisions for admins; the answer for the asker
  * paragraphs: exclude a note that doesn't apply (kept on the excluded list) and put it back
  * responsiveness: one shared Anthropic client; the chat poll's purge is throttled; a turn reads each attached file
    once; the editor's view no longer reads the inbox on every autosave
  * the pages carry the new controls

Run:  venv/bin/python tests/smoke_proposal_revisions.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import email
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_prev_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

MAIL = Path(_TMP) / "mail"
os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(MAIL), "SSC_GMAIL_POLL": "0",
                   "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(), "SSC_ASSISTANT_FAKE": "1",
                   "APP_BASE_URL": "http://test.local"})
os.environ.pop("ANTHROPIC_API_KEY", None)

from flask import Flask  # noqa: E402

import assistant  # noqa: E402
import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import claude_files  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import inbox  # noqa: E402
import pdf_export  # noqa: E402
import proposal_engine  # noqa: E402
import proposal_layout as L  # noqa: E402
import proposal_studio  # noqa: E402
import proposal_template  # noqa: E402
import proposals  # noqa: E402
import team_items  # noqa: E402
import today_feed  # noqa: E402

TAG = "prv"
PW = "RevisionsSmokePassword2026"
ORG = "Prv Smoke Rosen Properties"
DANA = "dana@prv-rosen.example"
TODAY = date.today()


def _db():
    return db_layer.connect(pragma_fk=True)


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ca.ensure(conn)
        inbox.ensure(conn)
        team_items.ensure(conn)
        proposals.ensure_schema(conn)
        today_feed.ensure(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        mids = [r["id"] for r in conn.execute(f"SELECT id FROM mail_message WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "mail_link", "message_id", mids)
        purge(conn, "mail_message", "id", mids)
        for t in ("gmail_link", "mail_sendas", "today_hidden"):
            purge(conn, t, "user_id", uids)
        purge(conn, "notification_log", "recipient_user_id", uids)
        chats = [r["id"] for r in conn.execute(f"SELECT id FROM claude_chat WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "claude_chat_message", "chat_id", chats)
        purge(conn, "claude_file", "user_id", uids)
        purge(conn, "claude_chat", "id", chats)
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE ?", (ORG + "%",)).fetchall()]
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph(ests)})", ests).fetchall()]
        purge(conn, "proposal_revision", "proposal_id", pids)
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "invoice", "proposal_id", pids)   # a signature drafts the deposit invoice
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate_document", "estimate_id", ests)
        purge(conn, "estimate_ira", "estimate_id", ests)
        purge(conn, "estimate", "id", ests)
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
            conn.execute(f"DELETE FROM crm_task WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM crm_organization WHERE name LIKE ?", (ORG + "%",))
        conn.execute("DELETE FROM crm_task WHERE title LIKE ?", (f"%{ORG}%",))
        conn.execute("DELETE FROM app_settings WHERE key IN ('proposal_template', 'proposal_house', 'proposal_library')")
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()


def _seed() -> dict:
    _cleanup()
    conn = _db()
    try:
        for who, role in (("admin", "admin"), ("cs", "c_suite"), ("cs2", "c_suite"), ("pm", "pm")):
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, "
                         "is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 0)",
                         (f"{TAG}.{who}@superstarscontracting.com", auth.hash_password(PW), role, f"Prv {who.title()}"))
        conn.commit()
        ids = {r["email"].split(".")[1].split("@")[0]: r["id"]
               for r in conn.execute("SELECT id, email FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()}
        for who in ("admin", "cs"):
            conn.execute("INSERT INTO gmail_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) "
                         "VALUES (?, ?, ?, 'active', ?, ?)", (ids[who], f"{TAG}.{who}@superstarscontracting.com", secret_box.encrypt_text("r"),
                                                              "2026-10-20T08:00:00", "openid email https://www.googleapis.com/auth/gmail.send"))
        org = crm.create_org(conn, name=ORG, relationship_type="client")
        crm.create_contact(conn, full_name="Dana Rosen", org_id=org, email=DANA)
        e_ = inbox._enc
        now = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
        for gid, thread, frm, subj in (("g-prv-1", "thr-prv-1", f"Dana Rosen <{DANA}>", "Pricing for the east wall"),
                                       ("g-prv-2", "thr-prv-2", "Someone Else <x@other-prv.example>", "Lunch on Friday")):
            conn.execute("INSERT INTO mail_message (user_id, gmail_id, thread_id, rfc_id_enc, direction, received_at, from_enc, to_enc, "
                         "cc_enc, subject_enc, body_enc, snippet_enc, needs_action, status, ai_state, created_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,'new','done',?)",
                         (ids["cs"], gid, thread, e_(f"<{gid}@mail.example>"), "in", now, e_(frm), e_(f"{TAG}.cs@superstarscontracting.com"),
                          None, e_(subj), e_("Could you send pricing for the drops?"), e_("Could you send pricing"), now))
        conn.commit()
        ids["org"] = org
        ids["mail"] = conn.execute("SELECT id FROM mail_message WHERE gmail_id = 'g-prv-1'").fetchone()["id"]
    finally:
        conn.close()
    return ids


def _draft(uid, what="Six rope access drops at 21 West St, flagman, report.") -> int:
    """A checklist-clean draft in the Superstars layout (the fake model's draft), as Proposal Studio makes it."""
    conn = _db()
    try:
        req = {"what": what, "client_name": ORG, "client_org_id": None, "building_address": "21 West Street, New York, NY 10006"}
        org = conn.execute("SELECT id FROM crm_organization WHERE name = ?", (ORG,)).fetchone()
        req["client_org_id"] = org["id"]
        return proposal_engine.create(conn, uid, req, proposal_engine.fake_draft(req))
    finally:
        conn.close()


# the money in these lines needs rounding: 8 days at $650.125 = $5,201.00; 1 report at $2,500.005 = $2,500.01
LINES = [{"lead": "Mobilization", "detail": "site safety plan and permits", "qty": 1, "unit": "LS", "unit_price": 3500},
         {"lead": "Rope access drops", "detail": "rigging and close-up physical inspection", "qty": 6, "unit": "EA", "unit_price": 4500},
         {"lead": "Flagman", "detail": "and ground control", "qty": 8, "unit": "DAY", "unit_price": 650.125},
         {"lead": "Inspection report", "detail": "deficiencies marked on PDF", "qty": 1, "unit": "LS", "unit_price": 2500.005}]


def _fake_pdf(html_path, pdf_path, timeout_sec=30):
    """The PDF engine, faked: the 'PDF' is the HTML it was given, so a test can read the amounts in it."""
    Path(pdf_path).write_bytes(b"%PDF-fake\n" + Path(html_path).read_bytes())
    return {"ok": True, "engine": "fake"}


def main() -> int:
    app = Flask("prv", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    ca.register(app)
    inbox.register(app)
    proposals.register(app)
    proposal_template.register(app)
    proposal_studio.register(app)
    today_feed.register(app)

    def login(who):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{who}@superstarscontracting.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.98.0.{len(who)}"})
        assert r.status_code == 200, (who, r.status_code)
        return c

    def emls():
        return sorted(MAIL.glob("gmail-*.eml"), key=lambda p: p.stat().st_mtime) if MAIL.exists() else []

    def cards(uid, prefix):
        conn = _db()
        try:
            return [c for c in today_feed.proposal_cards(conn, uid, TODAY) if c["key"].startswith(prefix)]
        finally:
            conn.close()

    ids = _seed()
    real_pdf = pdf_export.render_html_to_pdf
    pdf_export.render_html_to_pdf = _fake_pdf
    passed = 0
    try:
        a, cs, cs2, pm = login("admin"), login("cs"), login("cs2"), login("pm")

        # ---------------- amounts: one figure everywhere
        pid = _draft(ids["cs"])
        v = cs.patch(f"/api/proposal-studio/{pid}", json={"content": {}, "lines": LINES}).get_json()["data"]
        doc = {"lines": LINES}
        total, deposit = L.total(doc), L.deposit(doc, {"deposit_pct": 50})
        assert total == 38201.01, total
        assert v["amounts"]["total"] == total and float(v["proposal"]["price"]) == total and v["amounts"]["deposit"] == deposit
        assert L.money(total) in v["html"] and f"({L.money(deposit)})" in v["html"], "the editor draws the same total and deposit"
        assert not [c for c in v["checks"] if c["level"] == "block"], v["checks"]
        d = cs.get(f"/api/proposals/{pid}").get_json()["data"]
        assert d["price"] == total and d["amounts"]["total"] == total and d["deposit_amount"] == deposit
        r = cs.patch(f"/api/proposals/{pid}", json={"price": 9000})   # a price apart from the lines is not the price
        assert r.status_code == 200 and r.get_json()["data"]["price"] == total
        pdf1 = cs.get(f"/api/proposals/{pid}/pdf")
        assert pdf1.status_code == 200 and L.money(total).encode() in pdf1.data and b"BASE BID TOTAL" in pdf1.data
        # an edit in the SAME second as that PDF: the download is the new document, not the stale file
        more = [dict(li) for li in LINES]
        more[1]["qty"] = 7
        v = cs.patch(f"/api/proposal-studio/{pid}", json={"content": {}, "lines": more}).get_json()["data"]
        total7 = L.total({"lines": more})
        pdf2 = cs.get(f"/api/proposals/{pid}/pdf")
        assert L.money(total7).encode() in pdf2.data and L.money(total).encode() not in pdf2.data, "stale PDF after a same-second edit"
        assert v["amounts"]["total"] == total7 and float(v["proposal"]["price"]) == total7
        cs.patch(f"/api/proposal-studio/{pid}", json={"content": {}, "lines": LINES})
        assert L.money(total).encode() in cs.get(f"/api/proposals/{pid}/pdf").data
        # the older layout: the price is the sum of the line amounts as printed (each rounded to the cent)
        conn = _db()
        try:
            est = estimates.create_estimate(conn, est_type="FR", borough="MN", client_org_id=ids["org"], building_address="9 Legacy Pl",
                                            created_by=ids["cs"])
            leg = proposals.create_from_estimate(conn, est["id"], created_by=ids["cs"])
            conn.execute("UPDATE proposal SET layout = NULL WHERE id = ?", (leg["id"],))
            conn.commit()
            proposal_studio._apply(conn, leg["id"], {"lines": [{"description": f"Item {i}", "qty": 1, "unit": "ea", "unit_price": 0.005}
                                                               for i in range(3)]}, ids["cs"])
            r = proposals.get(conn, leg["id"])
            amts = [li["amount"] for li in proposals.lines(conn, leg["id"])]
            assert r["price"] == round(sum(amts), 2) == 0.03, (r["price"], amts)
            assert "$0.03" in proposals.render_document(conn, r)
        finally:
            conn.close()
        # a price read from an email sent outside the app: refused when the lines disagree (the document can't say otherwise)
        conn = _db()
        try:
            r = proposals.get(conn, pid)
            assert proposals.logged_price_problem(conn, r, total) is None
            assert "add up to" in (proposals.logged_price_problem(conn, r, 64500) or "")
            try:
                proposals.log_price(conn, pid, 64500)
                raise AssertionError("a logged price apart from the lines")
            except ValueError:
                pass
            assert proposals.get(conn, pid)["price"] == total
        finally:
            conn.close()
        passed += 1; print("  amounts ok")

        # ---------------- send: reply in the client's thread, reviewed, confirmed, same PDF
        v = cs.get(f"/api/proposal-studio/{pid}").get_json()["data"]
        assert "threads" not in v and "contacts" not in v, "the editor's view doesn't read the inbox on every save"
        o = cs.get(f"/api/proposal-studio/{pid}/send-options").get_json()["data"]
        assert [t["subject"] for t in o["threads"]] == ["Pricing for the east wall"] and o["threads"][0]["client"], o["threads"]
        assert any(c["email"] == DANA for c in o["contacts"]) and o["can_send"] and o["amounts"]["total"] == total
        found = cs.get(f"/api/proposal-studio/{pid}/send-options?q=lunch").get_json()["data"]["threads"]
        assert [t["subject"] for t in found] == ["Lunch on Friday"], "search finds a thread the app didn't match to the client"
        pv = cs.post(f"/api/proposal-studio/{pid}/send-preview", json={"how": "reply", "mail_id": ids["mail"]}).get_json()["data"]
        assert pv["to"] == f"Dana Rosen <{DANA}>" and pv["subject"] == "Re: Pricing for the east wall" and pv["thread"]["count"] == 1
        assert pv["attachment"].startswith("Proposal ") and pv["attachment"].endswith(".pdf") and pv["amounts"]["total"] == total
        assert cs2.post(f"/api/proposal-studio/{pid}/send-preview", json={"how": "reply", "mail_id": ids["mail"]}).status_code == 400, \
            "someone else's email is not offered"
        body = {"how": "reply", "mail_id": ids["mail"], "to": f"Dana Rosen <{DANA}>", "cc": "", "subject": pv["subject"],
                "message": "Hi Dana, our proposal is attached."}
        r = cs.post(f"/api/proposal-studio/{pid}/send", json=body)
        assert r.status_code == 400 and "confirm" in r.get_json()["error"] and not emls(), "nothing goes out without confirm"
        r = cs.post(f"/api/proposal-studio/{pid}/send", json=dict(body, confirm=True, expected_total=total + 100))
        assert r.status_code == 400 and "reviewed" in r.get_json()["error"] and not emls(), "a total that changed after the review"
        r = cs.post(f"/api/proposal-studio/{pid}/send", json=dict(body, confirm=True, expected_total=total))
        assert r.status_code == 200, r.get_json()
        sent = emls()
        assert len(sent) == 1
        m = email.message_from_bytes(sent[0].read_bytes())
        assert m["To"] == f"Dana Rosen <{DANA}>" and m["In-Reply-To"] == "<g-prv-1@mail.example>" and m["Subject"] == "Re: Pricing for the east wall"
        att = [x for x in m.walk() if x.get_content_disposition() == "attachment"][0].get_payload(decode=True)
        dl = cs.get(f"/api/proposals/{pid}/pdf").data
        assert att == dl and L.money(total).encode() in att, "the emailed PDF is the downloaded PDF, with the same total"
        conn = _db()
        try:
            assert conn.execute("SELECT proposal_id FROM mail_message WHERE id = ?", (ids["mail"],)).fetchone()["proposal_id"] == pid
            r = proposals.get(conn, pid)
            assert r["status"] == "sent" and r["sent_by"] == ids["cs"] and isinstance(proposal_engine.content(r).get("house"), dict)
        finally:
            conn.close()
        # the sent copy keeps the fixed wording it went out with
        a.put("/api/proposal-studio/house", json={"house": {"valid_text": "45 Days"}})
        assert cs.get(f"/api/proposals/{pid}/pdf").data == dl and b"Proposal Valid: 30 Days" in dl
        a.put("/api/proposal-studio/house", json={"house": {"valid_text": "30 Days"}})
        passed += 1; print("  reply in thread + review + confirm ok")

        # ---------------- revisions: anyone asks, only an admin decides
        assert pm.post(f"/api/proposals/{pid}/revisions", json={"reason": "x"}).status_code == 403
        assert cs2.post(f"/api/proposals/{pid}/revisions", json={"reason": ""}).status_code == 400
        r = cs2.post(f"/api/proposals/{pid}/revisions", json={"reason": "Client asked for a price review a week after we sent it.",
                                                              "price_review": True, "proposed_price": "$35,000"})
        assert r.status_code == 201, r.get_json()
        rv = r.get_json()["data"]
        assert rv["status"] == "pending" and rv["price_review"] and rv["proposed_price"] == 35000 and rv["requested_by_name"] == "Prv Cs2"
        assert cs.post(f"/api/proposals/{pid}/revisions", json={"reason": "again please"}).status_code == 409, "one at a time"
        for who in (cs, cs2):   # hiding the button is not the rule: the server refuses
            assert who.post(f"/api/proposals/revisions/{rv['id']}/approve", json={}).status_code == 403
            assert who.post(f"/api/proposals/revisions/{rv['id']}/reject", json={}).status_code == 403
            assert who.post(f"/api/proposal-studio/{pid}/new-version", json={}).status_code == 403
            assert who.post(f"/api/proposals/{pid}/new-version").status_code == 403
        assert pm.post(f"/api/proposals/revisions/{rv['id']}/approve", json={}).status_code == 403
        conn = _db()
        try:
            try:
                proposals.decide_revision(conn, rv["id"], actor=ids["cs"], actor_role="c_suite", approve=True)
                raise AssertionError("C-suite approved a revision")
            except PermissionError:
                pass
        finally:
            conn.close()
        assert [c["key"] for c in cards(ids["admin"], "proposal-revision:")] == [f"proposal-revision:{rv['id']}"]
        assert not cards(ids["cs2"], "proposal-revision:") and not cards(ids["cs"], "proposal-revision:"), "only admins decide"
        lst = cs.get(f"/api/proposals/{pid}/revisions").get_json()["data"]
        assert [x["status"] for x in lst] == ["pending"]
        r = a.post(f"/api/proposals/revisions/{rv['id']}/approve", json={"note": "OK, revise the drops."})
        assert r.status_code == 200, r.get_json()
        rv = r.get_json()["data"]
        v2 = rv["new_proposal_id"]
        assert rv["status"] == "approved" and v2 and rv["decided_by_name"] == "Prv Admin" and rv["decision_note"] == "OK, revise the drops."
        assert a.post(f"/api/proposals/revisions/{rv['id']}/approve", json={}).status_code == 409, "decided once"
        orig = cs.get(f"/api/proposals/{pid}").get_json()["data"]
        assert orig["status"] == "sent" and not orig["superseded_by"], "the sent copy stays the client's until v2 is sent"
        assert {"revision_requested", "revision_approved"} <= {e["event"] for e in orig["events"]}
        assert cs.get(f"/api/proposals/{pid}/pdf").data == dl, "the sent PDF is untouched"
        ans = cards(ids["cs2"], "proposal-revision-answer:")
        assert len(ans) == 1 and "approved" in ans[0]["title"] and ans[0]["href"] == f"/proposal-studio#p/{v2}"
        nv = cs.get(f"/api/proposal-studio/{v2}").get_json()["data"]
        assert nv["editable"] and nv["revises"]["id"] == pid and nv["proposal"]["revision_of"] == pid and nv["amounts"]["total"] == total
        assert cs.post(f"/api/proposals/{pid}/revisions", json={"reason": "one more"}).status_code == 409, "v2 is being edited"
        lower = [dict(li) for li in LINES]
        lower[1]["unit_price"] = 4000
        nv = cs.patch(f"/api/proposal-studio/{v2}", json={"content": {}, "lines": lower}).get_json()["data"]
        total2 = L.total({"lines": lower})
        assert nv["amounts"]["total"] == total2 == 35201.01
        r = cs.post(f"/api/proposal-studio/{v2}/send", json={"how": "new", "to": f"Dana Rosen <{DANA}>", "subject": "Revised proposal",
                                                             "message": "Revised as discussed.", "confirm": True, "expected_total": total2})
        assert r.status_code == 200, r.get_json()
        orig = cs.get(f"/api/proposals/{pid}").get_json()["data"]
        assert orig["status"] == "superseded" and orig["superseded_by"] == v2
        assert cs.get(f"/api/proposals/{pid}/pdf").data == dl, "the first version's PDF still says what was sent"
        assert L.money(total2).encode() in cs.get(f"/api/proposals/{v2}/pdf").data
        # rejected: nothing changes; withdraw is the asker's
        q = _draft(ids["cs"], "Facade survey of the north wall, two drops.")
        cs.patch(f"/api/proposal-studio/{q}", json={"content": {}, "lines": LINES})
        r = cs.post(f"/api/proposals/{q}/send", json={"to_email": DANA, "to_name": "Dana Rosen"})   # the link email
        assert r.status_code == 200, r.get_json()
        tok = [ln for ln in sorted(MAIL.glob("proposal-*.eml"))[-1].read_text().split() if "/p/" in ln][0].rsplit("/p/", 1)[1]
        assert f"Price: {L.money(total)}" in sorted(MAIL.glob("proposal-*.eml"))[-1].read_text(), "the link email's price is the total"
        rq = cs.post(f"/api/proposals/{q}/revisions", json={"reason": "Change the schedule."}).get_json()["data"]
        r = a.post(f"/api/proposals/revisions/{rq['id']}/reject", json={"note": "Schedule stays."})
        assert r.status_code == 200 and r.get_json()["data"]["status"] == "rejected" and not r.get_json()["data"]["new_proposal_id"]
        qd = cs.get(f"/api/proposals/{q}").get_json()["data"]
        assert qd["status"] == "sent" and "revision_rejected" in {e["event"] for e in qd["events"]}
        assert [c["title"] for c in cards(ids["cs"], "proposal-revision-answer:")] == [f"Revision of proposal {qd['label']} rejected"]
        rq = cs.post(f"/api/proposals/{q}/revisions", json={"reason": "Try again."}).get_json()["data"]
        assert cs2.post(f"/api/proposals/revisions/{rq['id']}/withdraw", json={}).status_code == 403
        assert cs.post(f"/api/proposals/revisions/{rq['id']}/withdraw", json={}).get_json()["data"]["status"] == "withdrawn"
        # signed meanwhile: the revision can't go out
        rq = cs.post(f"/api/proposals/{q}/revisions", json={"reason": "Add a drop."}).get_json()["data"]
        q2 = a.post(f"/api/proposals/revisions/{rq['id']}/approve", json={}).get_json()["data"]["new_proposal_id"]
        assert app.test_client().get(f"/p/{tok}").status_code == 200, "the client's link still works while v2 is drafted"
        assert app.test_client().post(f"/api/p/{tok}/accept", json={"name": "Dana Rosen", "agreed": True}).status_code == 200
        r = cs.post(f"/api/proposal-studio/{q2}/send", json={"how": "new", "to": DANA, "message": "x", "confirm": True})
        assert r.status_code == 400 and "signed" in r.get_json()["error"]
        assert cs.get(f"/api/proposal-studio/{q2}").get_json()["data"]["revision_problem"]
        # an admin can make a new version directly; it is recorded as a revision the admin approved
        z = _draft(ids["admin"])
        a.patch(f"/api/proposal-studio/{z}", json={"content": {}, "lines": LINES})
        a.post(f"/api/proposals/{z}/send", json={"to_email": DANA})
        r = a.post(f"/api/proposal-studio/{z}/new-version", json={})
        assert r.status_code == 200 and cs.get(f"/api/proposals/{z}").get_json()["data"]["status"] == "sent"
        assert [x["status"] for x in cs.get(f"/api/proposals/{z}/revisions").get_json()["data"]] == ["approved"]
        passed += 1; print("  revisions (admin-only) ok")

        # ---------------- in-app alerts: follow up before a proposal lapses (nothing is sent)
        conn = _db()
        try:
            x = _draft(ids["cs"], "Terracotta probes at the cornice, three locations.")
            conn.execute("UPDATE proposal SET status = 'sent', sent_at = ?, sent_by = ?, valid_until = ? WHERE id = ?",
                         (datetime.now().isoformat(timespec="seconds"), ids["cs"], (TODAY + timedelta(days=3)).isoformat(), x))
            conn.commit()
        finally:
            conn.close()
        before = len(list(MAIL.glob("*")))
        mine = [c for c in cards(ids["cs"], "proposal-expiry:") if c["proposal_id"] == x]
        assert len(mine) == 1 and "expires in 3 days" in mine[0]["title"] and mine[0]["urgency"] == "today", mine
        acts = {ac["type"]: ac for ac in mine[0]["actions"]}
        assert acts["open"]["primary"] and "don't send it" in acts["claude"]["prompt"] and mine[0]["href"] == f"/proposal-studio#p/{x}"
        assert not [c for c in cards(ids["cs2"], "proposal-expiry:") if c["proposal_id"] == x], "not the sender, not an admin"
        assert [c for c in cards(ids["admin"], "proposal-expiry:") if c["proposal_id"] == x]
        live = cs.get("/api/company/portal/today").get_json()["data"]["needs"]
        assert any(c["key"] == mine[0]["key"] for c in live), "it is on the Dashboard's Needs you"
        conn = _db()
        try:
            conn.execute("UPDATE proposal SET valid_until = ? WHERE id = ?", ((TODAY - timedelta(days=2)).isoformat(), x))
            conn.commit()
            lapsed = [c for c in today_feed.proposal_cards(conn, ids["cs"], TODAY) if c.get("proposal_id") == x]
            assert len(lapsed) == 1 and "expired 2 days ago" in lapsed[0]["title"] and lapsed[0]["urgency"] == "now"
            conn.execute("UPDATE proposal SET valid_until = ? WHERE id = ?", ((TODAY + timedelta(days=20)).isoformat(), x))
            conn.commit()
            assert not [c for c in today_feed.proposal_cards(conn, ids["cs"], TODAY) if c.get("proposal_id") == x]
        finally:
            conn.close()
        assert len(list(MAIL.glob("*"))) == before, "an alert never sends an email"
        sv = cs.get(f"/api/proposal-studio/{x}").get_json()["data"]
        assert sv["proposal"]["expires_in_days"] == 20
        passed += 1; print("  expiry follow-ups ok")

        # ---------------- paragraphs: exclude what doesn't apply, put it back
        dpid = _draft(ids["cs"])
        dv = cs.get(f"/api/proposal-studio/{dpid}").get_json()["data"]
        note = dv["content"]["notes"][-1]
        r = cs.post(f"/api/proposal-studio/{dpid}/exclude", json={"text": note})
        assert r.status_code == 200, r.get_json()
        ev = r.get_json()["data"]
        assert note not in ev["content"]["notes"] and ev["content"]["excluded"] == [{"kind": "notes", "text": note}]
        assert note not in ev["html"]
        assert cs.post(f"/api/proposal-studio/{dpid}/exclude", json={"text": note}).status_code == 400
        rv_ = cs.post(f"/api/proposal-studio/{dpid}/restore", json={"text": note}).get_json()["data"]
        assert rv_["content"]["notes"][-1] == note and rv_["content"]["excluded"] == []
        assert cs.post(f"/api/proposal-studio/{pid}/exclude", json={"text": "x"}).status_code == 400, "a sent proposal doesn't change"
        passed += 1; print("  paragraphs ok")

        # ---------------- mark as sent: it went out another way (the app sends nothing)
        ms = _draft(ids["cs"], "Masonry probes at the parapet, four locations.")
        lbl = cs.get(f"/api/proposal-studio/{ms}").get_json()["data"]["label"]
        for q in (lbl, f"Proposal No. {lbl}", f"#{lbl}"):   # the number people see finds it (Claude searched the code)
            assert [r["id"] for r in cs.get("/api/proposals", query_string={"q": q}).get_json()["data"]] == [ms], q
        assert len(cs.get("/api/proposals?q=proposal").get_json()["data"]) == 0, "a word is not a number"
        before = len(list(MAIL.glob("*")))
        assert pm.post(f"/api/proposals/{ms}/mark-sent", json={}).status_code == 403
        assert cs.post(f"/api/proposals/{ms}/mark-sent", json={"sent_on": (TODAY + timedelta(days=1)).isoformat()}).status_code == 400
        assert cs.post(f"/api/proposals/{ms}/mark-sent", json={"to_email": "not-an-email"}).status_code == 400
        ago = (TODAY - timedelta(days=2)).isoformat()
        r = cs.post(f"/api/proposals/{ms}/mark-sent", json={"sent_on": ago, "to_email": DANA, "to_name": "Dana Rosen", "how": "emailed from Outlook"})
        assert r.status_code == 200, r.get_json()
        d = r.get_json()["data"]
        assert d["status"] == "sent" and d["sent_at"] == f"{ago}T12:00:00" and d["recipient_email"] == DANA and not d["link_expires_at"]
        assert any(e["event"] == "sent" and "outside the app" in (e["detail"] or "") for e in d["events"])
        assert len(list(MAIL.glob("*"))) == before, "marking as sent emails no one"
        assert cs.post(f"/api/proposals/{ms}/mark-sent", json={}).status_code == 409
        # Claude: "mark this proposal as sent" with no Gmail email to link
        ms2 = _draft(ids["cs"], "Coating at the bulkhead, one location.")
        tool = ca.BY_NAME["log_proposal_sent"]
        conn = _db()
        try:
            ctx = ca.Ctx(conn, {"id": ids["cs"], "role": "c_suite", "name": "Prv Cs"}, "office")
            tool.run(ctx, {"proposal_id": ms2})
            card = tool.card(ctx, {"proposal_id": ms2})
            assert card["title"] == "Mark the proposal as sent" and {"sent_on", "to_email"} <= {f["key"] for f in card["fields"]}
            res = tool.execute(ctx, {"proposal_id": ms2, "sent_on": TODAY.isoformat(), "to_email": DANA})
            assert "marked as sent" in res["summary"] and proposals.get(conn, ms2)["status"] == "sent"
            try:
                tool.run(ctx, {"proposal_id": ms2})
                raise AssertionError("an already-sent proposal")
            except ValueError:
                pass
        finally:
            conn.close()
        html = (ROOT / "proposal_studio.html").read_text(encoding="utf-8")
        assert 'id="ms-m"' in html and "'/proposals/' + S.id + '/mark-sent'" in html and "data-marksent" in html
        assert 'id="pr-sent-overlay"' in (ROOT / "company_portal.html").read_text(encoding="utf-8")
        passed += 1; print("  mark as sent ok")

        # ---------------- responsiveness
        import anthropic
        os.environ["ANTHROPIC_API_KEY"] = "test-not-a-real-key"
        try:
            c1, c2 = assistant.client(), assistant.client()
            assert c1 is c2, "one shared client, not a new connection per call"
            os.environ["ANTHROPIC_API_KEY"] = "test-another-key"
            assert assistant.client() is not c1, "a new key gets a new client"
            assert isinstance(c1, anthropic.Anthropic)
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        conn = _db()
        try:
            ca.purge_expired(conn)
            assert ca.purge_expired(conn, throttle=True) == 0 and ca._PURGED["at"] > 0
            f = claude_files.store(conn, ids["cs"], "notes.txt", b"Drops at the north wall.")
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute("INSERT INTO claude_chat (user_id, surface, title, created_at, updated_at) VALUES (?, 'office', 'prv', ?, ?)",
                         (ids["cs"], now, now))
            conn.commit()
            chat = conn.execute("SELECT MAX(id) AS m FROM claude_chat WHERE user_id = ?", (ids["cs"],)).fetchone()["m"]
            ca._add_msg(conn, chat, "user", "text", [{"type": "ssc_file", "id": f["id"]}, {"type": "text", "text": "read this"}])
            calls, real_blocks = [], claude_files.blocks
            claude_files.blocks = lambda c, r: calls.append(r["id"]) or real_blocks(c, r)
            try:
                cache: dict = {}
                h1, h2 = ca._history(conn, chat, cache), ca._history(conn, chat, cache)
                assert h1 == h2 and calls == [f["id"]], calls
            finally:
                claude_files.blocks = real_blocks
        finally:
            conn.close()
        passed += 1; print("  responsiveness ok")

        # ---------------- the pages
        html = (ROOT / "proposal_studio.html").read_text(encoding="utf-8")
        for needle in ("confirm: true, expected_total", 'id="s-step2"', "'/send-options'", "'/send-preview'", 'id="rq-m"', 'id="dc-m"',
                       "data-pt=\"exclude\"", "'/proposal-studio/' + S.id + '/' + verb", 'id="ex-list"', 'id="chat-card"', "chatPanel(",
                       "$('e-pdf').addEventListener('click'", "await flushNow();", "markStep(j.step || 0)", 'id="e-expiry"', "SSC_CLAUDE.ask(q)",
                       "if (S.asking) return;", "'/proposals/revisions/' + S.decide.id"):
            assert needle in html, needle
        assert "setInterval(() => { shown" not in html, "the draft steps follow the server, not a timer"
        # a blocked Send says why (it was a greyed-out button with the reason below the fold)
        for needle in ('id="e-notready"', "function sendState(v)", "toast('Not ready to send: ' + why[0], true); showChecklist();", ".rail.blocked #ck-card{order:-1"):
            assert needle in html, needle
        assert "$('e-send').disabled =" not in html, "Send is never a silent dead button"
        cp = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        for needle in ('id="pr-rv-overlay"', "prExpiry(r)", "/proposal-studio#p/${p.id}/send", "canVersion = admin && revisable", "p.amounts.total"):
            assert needle in cp, needle
        js = (ROOT / "static/js/claude_drawer.js").read_text(encoding="utf-8")
        assert "S.polls <= 6 ? 700 : 1500" in js and "openChat(view.chat.id).then(refreshStatus)" not in js
        passed += 1; print("  pages ok")
    finally:
        pdf_export.render_html_to_pdf = real_pdf
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: proposal revisions + amounts ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
