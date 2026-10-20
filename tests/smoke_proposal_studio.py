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
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name = ?", (ORG,)).fetchall()]
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph(ests)})", ests).fetchall()]
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate_document", "estimate_id", ests)
        purge(conn, "estimate", "id", ests)
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM crm_organization WHERE name = ?", (ORG,))
        conn.execute("DELETE FROM crm_task WHERE title LIKE 'Follow up on proposal %Pst Smoke%'")
        conn.execute("DELETE FROM app_settings WHERE key IN ('proposal_template', 'proposal_house_style')")
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

        # ---- editing
        v = a.get(f"/api/proposal-studio/{pid}").get_json()["data"]
        p = v["proposal"]
        assert p["status"] == "draft" and p["client_name"] == ORG and v["editable"] and float(p["price"]) == 48550
        assert len(v["lines"]) == 3 and v["sources"] and p["email_message"]
        for needle in ('data-f="title"', 'data-f="scope_summary"', 'data-f="intro_text"', "data-lf='unit_price'", "data-li='2'"):
            assert needle in v["html"], needle
        lines = v["lines"] + [{"description": "Pst smoke extra", "qty": 2, "unit": "ea", "unit_price": 100}]
        v = a.patch(f"/api/proposal-studio/{pid}", json={"fields": {"title": "Pst east wall", "hidden_sections": ["schedule"],
                                                                     "extra_sections": [{"heading": "Access", "body": "Roof access by the super."}]},
                                                          "lines": lines}).get_json()["data"]
        assert v["proposal"]["title"] == "Pst east wall" and float(v["proposal"]["price"]) == 48750
        assert v["sections"]["hidden_sections"] == ["schedule"] and v["sections"]["extra_sections"][0]["heading"] == "Access"
        assert 'data-f="x0h"' in v["html"] and "Roof access by the super." in v["html"] and 'data-f="schedule_text"' not in v["html"]
        bad = a.patch(f"/api/proposal-studio/{pid}", json={"lines": [{"description": "x", "qty": -1, "unit_price": 5}]})
        assert bad.status_code == 400
        r = a.post(f"/api/proposal-studio/{pid}/revise", json={"instruction": "add a 10% contingency"}).get_json()["data"]
        assert r["changed"] == ["lines"] and r["lines"][-1]["description"] == "Contingency (10%)" and len(r["before"]["lines"]) == 4
        undo = a.patch(f"/api/proposal-studio/{pid}", json={"fields": {}, "lines": r["before"]["lines"]}).get_json()["data"]
        assert len(undo["lines"]) == 4
        assert a.post(f"/api/proposal-studio/{pid}/revise", json={"instruction": ""}).status_code == 400
        passed += 1; print("  editing ok")

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
        assert out["follow_up_task"]
        emls = sorted(MAIL.glob("gmail-new-*.eml"))
        assert len(emls) == 1
        msg = email.message_from_bytes(emls[0].read_bytes())
        assert msg["To"] == "Dana <dana@rosen-pst.example>" and msg["Subject"] == "Proposal for 410 E 58th"
        atts = [x for x in msg.walk() if x.get_content_disposition() == "attachment"]
        assert len(atts) == 1 and atts[0].get_content_type() == "application/pdf" and atts[0].get_payload(decode=True) == b"%PDF-1.4 pst"
        v = a.get(f"/api/proposal-studio/{pid}").get_json()["data"]
        assert v["proposal"]["status"] == "sent" and not v["editable"] and v["proposal"]["recipient_email"] == "dana@rosen-pst.example"
        nv = a.post(f"/api/proposal-studio/{pid}/new-version", json={}).get_json()["data"]
        v2 = a.get(f"/api/proposal-studio/{nv['id']}").get_json()["data"]
        assert v2["editable"] and v2["proposal"]["version"] == 2 and v2["sections"]["extra_sections"][0]["heading"] == "Access"
        assert v2["sections"]["hidden_sections"] == ["schedule"]
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

        # ---- the page
        html = (ROOT / "proposal_studio.html").read_text(encoding="utf-8")
        for needle in ('id="doc"', 'sandbox="allow-same-origin"', "'/proposal-studio/drafts'", "'/proposal-studio/' + S.id + '/revise'",
                       "'/proposal-studio/' + S.id + '/send'", 'data-subtabs="proposals"', "/api/proposal-template"):
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
