#!/usr/bin/env python3
"""Proposal Studio (accepted prototype 2026-10-20): the BEHAVIORAL guard for proposal_studio.py.

Proves, through a Flask test client against an ISOLATED database (fake model, no Google, no PDF engine):
  * gating: anonymous 401, pm 403 on the page and every route; another person can't read a draft job
  * asking: a request with too little text, or no client, is refused in plain words; a draft for a NEW client
    creates the client + an estimate + a DRAFT proposal with Claude's lines, sources and email
  * editing: the document comes back in edit mode (every text marked data-f / data-lf); fields, lines, hidden and
    added sections save; Claude's change is applied with the list of what changed and the snapshot for Undo
  * sending: a new email from Gmail with the PDF attached; the proposal is Sent, a follow-up task exists, and a
    second send is refused; a new version copies everything Studio added
  * house style: learn from the sent proposals (fake model) and correct an item by hand
  * the page and the Proposals sub-tab exist

Run:  venv/bin/python tests/smoke_proposal_studio.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import email
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_studio_")

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
import db_layer  # noqa: E402
import inbox  # noqa: E402
import proposal_studio  # noqa: E402
import proposal_template  # noqa: E402
import proposals  # noqa: E402
import team_items  # noqa: E402

TAG = "pst"
PW = "StudioSmokePassword2026"
ORG = "Pst Smoke Rosen Properties"


def _db():
    return db_layer.connect(pragma_fk=True)


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ca.ensure(conn)
        inbox.ensure(conn)
        team_items.ensure(conn)
        proposals.ensure_schema(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        for t in ("gmail_link", "mail_sendas"):
            purge(conn, t, "user_id", uids)
        chats = [r["id"] for r in conn.execute(f"SELECT id FROM claude_chat WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "claude_chat_message", "chat_id", chats)
        purge(conn, "claude_chat", "id", chats)
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE ?", (ORG + "%",)).fetchall()]
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph(ests)})", ests).fetchall()]
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate_document", "estimate_id", ests)
        purge(conn, "estimate_ira", "estimate_id", ests)   # rope access drafts are IRA estimates (Postgres enforces the FK)
        purge(conn, "estimate", "id", ests)
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM crm_organization WHERE name LIKE ?", (ORG + "%",))
        conn.execute("DELETE FROM crm_task WHERE title LIKE 'Follow up on proposal %Pst Smoke%'")
        conn.execute("DELETE FROM app_settings WHERE key IN ('proposal_template', 'proposal_house_style', 'proposal_house', 'proposal_library')")
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()


def _seed() -> dict:
    _cleanup()
    conn = _db()
    try:
        for role in ("admin", "c_suite", "pm"):
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, "
                         "is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 0)",
                         (f"{TAG}.{role}@superstarscontracting.com", auth.hash_password(PW), role, f"Pst {role.title()}"))
        conn.commit()
        ids = {r["role"]: r["id"] for r in conn.execute("SELECT id, role FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()}
        conn.execute("INSERT INTO gmail_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) "
                     "VALUES (?, ?, ?, 'active', ?, ?)", (ids["admin"], f"{TAG}.admin@superstarscontracting.com",
                                                          secret_box.encrypt_text("r"), "2026-10-20T08:00:00",
                                                          "openid email https://www.googleapis.com/auth/gmail.send"))
        conn.commit()
    finally:
        conn.close()
    return ids


def main() -> int:
    app = Flask("pst", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    ca.register(app)
    inbox.register(app)
    proposals.register(app)
    proposal_template.register(app)
    proposal_studio.register(app)

    def login(role):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@superstarscontracting.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.96.0.{len(role)}"})
        assert r.status_code == 200, (role, r.status_code)
        return c

    ids = _seed()
    passed = 0
    try:
        anon = app.test_client()
        a, cs, pm = login("admin"), login("c_suite"), login("pm")
        for path in ("/api/proposal-studio/proposals", "/api/proposal-studio/house-style", "/api/proposal-studio/1"):
            assert anon.get(path).status_code == 401, path
            assert pm.get(path).status_code == 403, path
        assert pm.get("/proposal-studio").status_code in (302, 403)
        assert a.get("/proposal-studio").status_code == 200
        passed += 1; print("  gating ok")

        # ---- asking
        r = a.post("/api/proposal-studio/drafts", json={"what": "short", "client_name": ORG})
        assert r.status_code == 400 and "a bit more" in r.get_json()["error"]
        r = a.post("/api/proposal-studio/drafts", json={"what": "Repoint the east wall and replace six lintels."})
        assert r.status_code == 400 and "client" in r.get_json()["error"]
        r = a.post("/api/proposal-studio/drafts", json={"what": "Repoint the east wall and replace six lintels, sidewalk shed needed.",
                                                        "client_name": ORG, "building_address": "410 E 58th St, New York, NY"})
        assert r.status_code == 202, r.get_json()
        job = r.get_json()["data"]["job"]
        assert len(r.get_json()["data"]["steps"]) == 5
        assert cs.get(f"/api/proposal-studio/jobs/{job}").status_code == 404, "another person's job"
        for _ in range(100):
            j = a.get(f"/api/proposal-studio/jobs/{job}").get_json()["data"]
            if j["state"] != "working":
                break
            time.sleep(0.05)
        assert j["state"] == "done", j
        pid = j["proposal_id"]
        passed += 1; print("  draft ok")

        # ---- the Claude window / an email card: draft_proposal carries only the facts; approving starts the same engine
        tool = ca.BY_NAME["draft_proposal"]
        conn = _db()
        try:
            ctx = ca.Ctx(conn, {"id": ids["admin"], "role": "admin", "name": "Pst Admin"}, "office")
            try:
                tool.run(ctx, {"what": "short"})
                raise AssertionError("too little to go on")
            except ValueError:
                pass
            facts = {"what": "Six rope access drops at 21 West St for SSX, $4,500 a drop.", "client_name": ORG + " Two",
                     "building_address": "21 West Street, New York, NY", "job_type": "Rope access inspection"}
            tool.run(ctx, facts)
            card = tool.card(ctx, facts)
            assert card["title"] == "Write a proposal in Proposal Studio" and any(f["key"] == "what" for f in card["fields"])
            res = tool.execute(ctx, facts)
            assert res["link"].startswith("/proposal-studio#job/"), res
            old = {"title": "Lintels", "scope_summary": "Replace lintels.", "lines": [{"description": "Lintel", "qty": 6, "unit_price": 2400}],
                   "client_name": ORG}
            assert "Lintel 6" in ca._draft_facts(old)["what"], "cards from before keep working"
        finally:
            conn.close()
        job2 = res["link"].rsplit("/", 1)[1]
        for _ in range(100):
            j2 = a.get(f"/api/proposal-studio/jobs/{job2}").get_json()["data"]
            if j2["state"] != "working":
                break
            time.sleep(0.05)
        assert j2["state"] == "done", j2
        v2 = a.get(f"/api/proposal-studio/{j2['proposal_id']}").get_json()["data"]
        assert v2["layout"] == "ssc" and v2["content"]["job_type"] == "Rope access inspection"
        passed += 1; print("  claude window ok")

        # ---- editing (the locked company layout, 2026-10-21)
        v = a.get(f"/api/proposal-studio/{pid}").get_json()["data"]
        p = v["proposal"]
        assert v["layout"] == "ssc" and v["label"].isdigit() and int(v["label"]) >= 1042
        assert p["status"] == "draft" and p["client_name"] == ORG and v["editable"] and float(p["price"]) == 38200
        assert len(v["lines"]) == 4 and v["sources"] and p["email_message"]
        for needle in ('data-f="title"', 'data-f="overview"', 'data-f="notes"', "data-lf='lead'", "data-lf='unit_price'", "data-li='3'",
                       "Proposal Valid: 30 Days", "Arun Mal, Vice President", "reserves the right to withdraw this proposal",
                       "Any work not specifically listed above.", "50% deposit ($19,100.00) required to schedule", "BASE BID TOTAL"):
            assert needle in v["html"], needle
        assert not [c for c in v["checks"] if c["level"] == "block"], v["checks"]
        assert v["new_clauses"], "Claude's wording that isn't in the library yet is flagged"
        c = v["content"]
        lines = c["lines"] + [{"lead": "Pst extra", "detail": "smoke item", "qty": 2, "unit": "EA", "unit_price": 100}]
        v = a.patch(f"/api/proposal-studio/{pid}", json={"content": {"title": "Pst 21 West", "heading": "PROPOSAL"}, "lines": lines}).get_json()["data"]
        assert v["proposal"]["title"] == "Pst 21 West" and float(v["proposal"]["price"]) == 38400 and ">PROPOSAL<" in v["html"]
        assert v["content"]["lines"][-1] == {"lead": "Pst extra", "detail": "smoke item", "qty": 2, "unit": "EA", "unit_price": 100}
        bad = dict(lines[-1], unit="bags")
        v = a.patch(f"/api/proposal-studio/{pid}", json={"content": {}, "lines": lines[:-1] + [bad]}).get_json()["data"]
        assert any(x["level"] == "block" and "unit" in x["text"] for x in v["checks"])
        blocked = a.post(f"/api/proposal-studio/{pid}/send", json={"how": "new", "to": "x@y.example", "message": "x"})
        assert blocked.status_code == 400 and "checklist" in blocked.get_json()["error"], "Send refuses a red checklist"
        v = a.patch(f"/api/proposal-studio/{pid}", json={"content": {}, "lines": lines}).get_json()["data"]
        assert not [x for x in v["checks"] if x["level"] == "block"]
        r = a.post(f"/api/proposal-studio/{pid}/revise", json={"instruction": "add a 10% contingency"}).get_json()["data"]
        assert r["changed"] == ["notes"] and "10% contingency" in r["content"]["notes"][-1] and len(r["before"]["notes"]) == 2
        u = a.post(f"/api/proposal-studio/{pid}/undo", json={"before": r["before"]}).get_json()["data"]
        assert u["content"]["notes"] == r["before"]["notes"] and len(u["content"]["lines"]) == 5
        assert a.post(f"/api/proposal-studio/{pid}/revise", json={"instruction": ""}).status_code == 400
        kept = u["new_clauses"][0]
        k = a.post(f"/api/proposal-studio/{pid}/keep", json={"text": kept}).get_json()["data"]
        assert kept not in k["new_clauses"], "an OK'd clause joins the library"
        assert a.post(f"/api/proposal-studio/{pid}/keep", json={"text": "not in it"}).status_code == 400
        # the SUBMITTED BY block of the older layout stays editable per proposal (2026-10-21)
        import proposal_template as _pt
        t = "<td><b>SUBMITTED BY</b><br>{{company_name}}<br>{{company_address}}<br>{{company_phone}} | {{company_email}}</td>"
        assert _pt.with_submitted_by(t) == "<td><b>SUBMITTED BY</b><br>{{submitted_by}}</td>", _pt.with_submitted_by(t)
        assert _pt.with_submitted_by("<p>Submitted by</p><p>{{company_name}}</p>") == "<p>Submitted by</p><p>{{submitted_by}}</p>"
        passed += 1; print("  editing ok")

        # ---- fixed wording + library
        hw = a.get("/api/proposal-studio/house").get_json()["data"]
        assert hw["house"]["valid_text"] == "30 Days" and hw["house"]["deposit_pct"] == 50 and "Rope access inspection" in hw["job_types"]
        assert a.put("/api/proposal-studio/house", json={"house": {"deposit_pct": 140}}).status_code == 400
        hw = a.put("/api/proposal-studio/house", json={"house": {"valid_text": "45 Days", "hours": "8 AM to 4 PM, Monday to Friday."}}).get_json()["data"]
        assert hw["house"]["valid_text"] == "45 Days"
        h2 = a.get(f"/api/proposal-studio/{pid}").get_json()["data"]["html"]
        assert "Proposal Valid: 45 Days" in h2 and "8 AM to 4 PM, Monday to Friday." in h2
        a.put("/api/proposal-studio/house", json={"house": {"valid_text": "30 Days", "hours": "9 AM to 5 PM, Monday to Friday, excluding legal holidays."}})
        lib = a.post("/api/proposal-studio/library", json={"add": {"kind": "exclusions", "text": "Pst smoke exclusion."}}).get_json()["data"]
        assert any(x["text"] == "Pst smoke exclusion." and x["added"] for x in lib["exclusions"])
        lib = a.post("/api/proposal-studio/library", json={"hide": "Pst smoke exclusion."}).get_json()["data"]
        assert not any(x["text"] == "Pst smoke exclusion." for x in lib["exclusions"])
        assert pm.get("/api/proposal-studio/house").status_code == 403
        passed += 1; print("  fixed wording ok")

        # ---- sending: PDF attached to a new email from Gmail
        pdf = Path(_TMP) / "p.pdf"
        pdf.write_bytes(b"%PDF-1.4 pst")
        real = proposals.render_pdf, proposals.pdf_file
        proposals.render_pdf = lambda conn, i, actor=None: {"ok": True}
        proposals.pdf_file = lambda conn, i: pdf
        try:
            assert a.post(f"/api/proposal-studio/{pid}/send", json={"how": "new", "to": "Dana <dana@rosen-pst.example>",
                                                                    "subject": "Proposal", "message": ""}).status_code == 400
            r = a.post(f"/api/proposal-studio/{pid}/send", json={"how": "new", "to": "Dana <dana@rosen-pst.example>",
                                                                 "subject": "Proposal for 410 E 58th", "message": "Hi Dana, attached."})
            assert r.status_code == 200, r.get_json()
            again = a.post(f"/api/proposal-studio/{pid}/send", json={"how": "new", "to": "x@y.example", "message": "x"})
            assert again.status_code == 400 and "new version" in again.get_json()["error"]
        finally:
            proposals.render_pdf, proposals.pdf_file = real
        out = r.get_json()["data"]
        assert out["follow_up_task"] and out["number"].isdigit()
        emls = sorted(MAIL.glob("gmail-new-*.eml"))
        assert len(emls) == 1
        msg = email.message_from_bytes(emls[0].read_bytes())
        assert msg["To"] == "Dana <dana@rosen-pst.example>" and msg["Subject"] == "Proposal for 410 E 58th"
        atts = [x for x in msg.walk() if x.get_content_disposition() == "attachment"]
        assert len(atts) == 1 and atts[0].get_content_type() == "application/pdf" and atts[0].get_payload(decode=True) == b"%PDF-1.4 pst"
        assert atts[0].get_filename() == f"Proposal {out['number']} - 410 E 58th St.pdf", atts[0].get_filename()
        v = a.get(f"/api/proposal-studio/{pid}").get_json()["data"]
        assert v["proposal"]["status"] == "sent" and not v["editable"] and v["proposal"]["recipient_email"] == "dana@rosen-pst.example"
        nv = a.post(f"/api/proposal-studio/{pid}/new-version", json={}).get_json()["data"]
        v2 = a.get(f"/api/proposal-studio/{nv['id']}").get_json()["data"]
        assert v2["editable"] and v2["proposal"]["version"] == 2 and v2["label"] == f"{out['number']}-2"
        assert v2["content"]["title"] == "Pst 21 West" and len(v2["content"]["lines"]) == 5 and v2["content"]["lines"][0]["lead"] == "Mobilization"
        passed += 1; print("  send + new version ok")

        # ---- house style
        rl = a.post("/api/proposal-studio/house-style/learn", json={})
        assert rl.status_code == 200, rl.get_json()
        h = rl.get_json()["data"]
        assert h["state"] == "ready" and h["learned_from"] >= 1
        opening = next(i for i in h["items"] if i["key"] == "opening")
        assert opening["text"] and "proposal" in opening["source"]
        h = a.put("/api/proposal-studio/house-style", json={"items": [{"key": "words", "text": "Say repoint, never tuckpoint."}],
                                                            "show_sources": False}).get_json()["data"]
        w = next(i for i in h["items"] if i["key"] == "words")
        assert w["text"] == "Say repoint, never tuckpoint." and w["source"] == "edited by you" and h["show_sources"] is False
        assert a.get(f"/api/proposal-studio/{nv['id']}").get_json()["data"]["sources"] == []
        h = a.post("/api/proposal-studio/house-style/learn", json={}).get_json()["data"]
        assert next(i for i in h["items"] if i["key"] == "words")["text"] == "Say repoint, never tuckpoint.", "a hand edit is kept"
        passed += 1; print("  house style ok")

        # ---- saved templates (2026-10-20): every upload kept; one in use; view, sample, use, remove
        conn = _db()
        try:   # the one-template shape from before reads as a list of one
            conn.execute("DELETE FROM app_settings WHERE key = 'proposal_template'")
            conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES ('proposal_template', ?, '2026-10-19T10:00:00')",
                         ('{"name": "Old template.pdf", "path": "data_room/proposal_template/old.pdf", "ext": ".pdf", "state": "ready", "html": "<html><body>{{scope}}<table>{{line_items_rows}}</table>{{total}}</body></html>"}',))
            conn.commit()
            t0 = proposal_template.public(conn)
            assert len(t0["templates"]) == 1 and t0["templates"][0]["in_use"] and t0["active"]
            a1 = proposal_template.start_read(conn, "Template A.pdf", b"%PDF-1.4 A", ids["admin"], background=False)
            a2 = proposal_template.start_read(conn, "Template B.pdf", b"%PDF-1.4 B", ids["c_suite"], background=False)
        finally:
            conn.close()
        lst = a.get("/api/proposal-template").get_json()["data"]
        names = [t["name"] for t in lst["templates"]]
        assert names == ["Template B.pdf", "Template A.pdf", "Old template.pdf"], names
        tb, ta = lst["templates"][0], lst["templates"][1]
        # since 2026-10-21 an upload is read into the proposal structure (examples + library); the old layout stays in use
        assert not tb["in_use"] and tb["learned"] and tb["learned"]["lines"] == 1 and tb["uploaded_by_name"] == "Pst C_Suite", tb
        assert lst["templates"][2]["in_use"]
        conn = _db()
        try:
            import proposal_engine
            assert any(x["text"] == "Facade repairs of any kind." for x in proposal_engine.library(conn)["exclusions"])
        finally:
            conn.close()
        assert a.post(f"/api/proposal-template/{ta['id']}/use", json={}).get_json()["data"]["active_id"] == ta["id"]
        f = a.get(f"/api/proposal-template/{ta['id']}/file")
        assert f.status_code == 200 and f.data == b"%PDF-1.4 A" and f.mimetype == "application/pdf"
        assert a.get(f"/api/proposal-template/{tb['id']}/preview").status_code == 200
        lst = a.delete(f"/api/proposal-template/{tb['id']}").get_json()["data"]
        assert [t["name"] for t in lst["templates"]] == ["Template A.pdf", "Old template.pdf"] and lst["active_id"] == ta["id"]
        lst = a.delete("/api/proposal-template").get_json()["data"]
        assert not lst["active"] and len(lst["templates"]) == 2, "the built-in layout keeps the saved list"
        assert pm.get("/api/proposal-template").status_code == 403 and a.get("/api/proposal-template/9999/file").status_code == 404
        passed += 1; print("  saved templates ok")

        # ---- the archive (2026-10-21): uploads are proposals from before the app, listed with the app's own
        ta_arch = next(t for t in lst["templates"] if t["id"] == ta["id"])["archive"]
        assert ta_arch["number"] == "1001" and ta_arch["date"] == "2025-03-14" and ta_arch["total"] == 27000.0 and not ta_arch["edited"], ta_arch
        old_tpl = next(t for t in lst["templates"] if t["name"] == "Old template.pdf")
        assert old_tpl["archive"] is None, "an upload read before the archive has no facts until it is read again"
        e1 = a.patch(f"/api/proposal-template/{ta['id']}", json={"number": "1001-B", "date": "03/15/2025", "client": ORG, "total": "$27,500.00"})
        assert e1.status_code == 200, e1.get_json()
        ta_arch = next(t for t in e1.get_json()["data"]["templates"] if t["id"] == ta["id"])["archive"]
        assert ta_arch["number"] == "1001-B" and ta_arch["date"] == "2025-03-15" and ta_arch["total"] == 27500.0 and ta_arch["edited"]
        assert ta_arch["client_org_id"], "the client is matched by name"
        assert a.patch(f"/api/proposal-template/{ta['id']}", json={"date": "next spring"}).status_code == 400
        assert a.patch("/api/proposal-template/9999", json={"number": "x"}).status_code == 404
        allp = a.get("/api/proposals?include=archive").get_json()
        arch = [r for r in allp["data"] if r.get("archived")]
        assert [r["label"] for r in arch] == ["1001-B"] and arch[0]["pdf_url"] == f"/api/proposal-template/{ta['id']}/file"
        assert arch[0]["status"] == "archived" and arch[0]["price"] == 27500.0 and allp["meta"]["counts"]["archived"] == 1
        assert any(not r.get("archived") for r in allp["data"]), "the app's proposals are listed too"
        only = a.get("/api/proposals?status=archived").get_json()["data"]
        assert [r["label"] for r in only] == ["1001-B"]
        assert a.get("/api/proposals?status=archived&q=1001-b").get_json()["data"] and not a.get("/api/proposals?status=archived&q=zzz").get_json()["data"]
        assert not [r for r in a.get("/api/proposals").get_json()["data"] if r.get("archived")], "only when asked"
        conn = _db()
        try:   # reading it again keeps a person's corrections
            proposal_template._read_job(ta["id"], next(t for t in proposal_template._store(conn)["templates"] if t["id"] == ta["id"])["path"],
                                        ".pdf", "Template A.pdf", ids["admin"])
            assert proposal_template.archive_rows(conn)[0]["label"] == "1001-B"
        finally:
            conn.close()
        passed += 1; print("  archive ok")

        # ---- the page
        html = (ROOT / "proposal_studio.html").read_text(encoding="utf-8")
        for needle in ('id="doc"', 'sandbox="allow-same-origin"', "'/proposal-studio/drafts'", "'/proposal-studio/' + S.id + '/revise'",
                       "'/proposal-studio/' + S.id + '/send'", 'data-subtabs="proposals"', "/api/proposal-template",
                       # 2026-10-20 simpler Studio: one box to start, Send in a pop-up, past proposals (many, dropped or picked)
                       'id="ask-form"', 'id="send-m"', "$('e-send').addEventListener('click'", 'id="v-past"', "$('pp-drop').addEventListener('drop'",
                       "files.forEach(f => fd.append('file', f))", "'/api/proposal-template/' + v.dataset.ppdf + '/file'", "data-step=\"past\">Archive<",
                       'id="e-view"', "pdf_view.js",
                       # 2026-10-21 locked layout: job type + heading, the checklist, fixed wording + library, Claude-window drafts
                       'id="a-type"', 'id="a-head"', 'id="ck"', 'id="v-wording"', "'/proposal-studio/house'", "/^job\\/([a-f0-9]+)$/"):
            assert needle in html, needle
        assert "allow-scripts" not in html, "the document frame never runs scripts"
        nav = (ROOT / "static/js/company_nav.js").read_text(encoding="utf-8")
        assert "['Proposal Studio', '/proposal-studio', COMPANY]" in nav and "['/proposal-studio', null]" in nav
        cp = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        assert 'href="/proposal-studio#new"' in cp
        passed += 1; print("  page ok")
    finally:
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: proposal studio ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
