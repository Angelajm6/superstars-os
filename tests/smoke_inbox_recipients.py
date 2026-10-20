#!/usr/bin/env python3
"""Inbox: Gmail-style recipients (accepted prototype 2026-10-20, https://claude.ai/artifact/FWLPeNAYGMBaSzSnomCNr7).

Proves against an ISOLATED database (no Google; the test mail folder):
  * a reply with no recipients given still goes to the sender (the old behavior)
  * To / Cc / Bcc given: exactly those people, in the same thread (In-Reply-To), Bcc carried for Gmail to deliver
  * a typo is refused with the bad address named; no To is refused
  * Forward: "Fwd:" subject, the forwarded-message block, the email's own attachment along
  * people search: a client contact and a team member by name; pm gets 403
  * the page carries the recipients editor (reply type menu, chips, Cc / Bcc, subject)
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import email
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_rcpt_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

MAIL = Path(_TMP) / "mail"
os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(MAIL), "SSC_GMAIL_POLL": "0",
                   "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(), "SSC_INBOX_ATTACHMENT_FAKE": "%PDF-1.4 rcpt"})

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import inbox  # noqa: E402

TAG = "rcp"
PW = "RecipientsSmokePassword2026"
ORG = "Rcp Smoke Rosen"


def _db():
    return db_layer.connect(pragma_fk=True)


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ca.ensure(conn)
        inbox.ensure(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        mids = [r["id"] for r in conn.execute(f"SELECT id FROM mail_message WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "mail_link", "message_id", mids)
        purge(conn, "mail_message", "id", mids)
        for t in ("gmail_link", "mail_sendas"):
            purge(conn, t, "user_id", uids)
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name = ?", (ORG,)).fetchall()]
        if orgs:
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM crm_organization WHERE name = ?", (ORG,))
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    _cleanup()
    conn = _db()
    try:
        for role in ("admin", "pm"):
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, "
                         "is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 0)",
                         (f"{TAG}.{role}@superstarscontracting.com", auth.hash_password(PW), role, f"Rcp {role.title()} Teammate"))
        conn.commit()
        ids = {r["role"]: r["id"] for r in conn.execute("SELECT id, role FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()}
        org = crm.create_org(conn, name=ORG, relationship_type="client")
        crm.create_contact(conn, full_name="Mark Rcpengineer", org_id=org, email="mark@rcp-eng.example")
        conn.execute("INSERT INTO gmail_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) VALUES (?,?,?, 'active', ?, ?)",
                     (ids["admin"], f"{TAG}.admin@superstarscontracting.com", secret_box.encrypt_text("r"), "2026-10-20T08:00:00",
                      "openid email https://www.googleapis.com/auth/gmail.send"))
        e_ = inbox._enc
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("INSERT INTO mail_message (user_id, gmail_id, thread_id, rfc_id_enc, direction, received_at, from_enc, to_enc, cc_enc, "
                     "subject_enc, body_enc, snippet_enc, attachments_enc, needs_action, status, ai_state, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,'new','done',?)",
                     (ids["admin"], "g-rcp-1", "thr-rcp-1", e_("<rcp-1@mail.example>"), "in", now, e_("Dana Rosen <dana@rcp-rosen.example>"),
                      e_(f"{TAG}.admin@superstarscontracting.com"), e_("Mark Rcpengineer <mark@rcp-eng.example>"), e_("Pricing for the east wall"),
                      e_("Could you send pricing?"), e_("Could you"),
                      e_(json.dumps([{"name": "FISP report.pdf", "mime": "application/pdf", "size": 12, "att_id": "a1"}])), now))
        conn.commit()
        mid = conn.execute("SELECT id FROM mail_message WHERE gmail_id = 'g-rcp-1'").fetchone()["id"]
    finally:
        conn.close()

    app = Flask("rcp", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    ca.register(app)
    inbox.register(app)

    def login(role):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@superstarscontracting.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.97.0.{len(role)}"})
        assert r.status_code == 200, r.status_code
        return c

    def last_eml():
        return email.message_from_bytes(sorted(MAIL.glob("gmail-*.eml"), key=lambda p: p.stat().st_mtime)[-1].read_bytes())

    passed = 0
    try:
        a, pm = login("admin"), login("pm")
        # ---- the old behavior: no recipients given -> the sender
        assert a.post(f"/api/inbox/messages/{mid}/reply", json={"body": "Hi Dana"}).status_code == 200
        m = last_eml()
        assert m["To"] == "Dana Rosen <dana@rcp-rosen.example>" and m["Cc"] is None and m["In-Reply-To"] == "<rcp-1@mail.example>"
        passed += 1; print("  plain reply ok")

        # ---- exactly the people given
        r = a.post(f"/api/inbox/messages/{mid}/reply", json={"body": "Adding Sam", "to": "Dana Rosen <dana@rcp-rosen.example>",
                                                             "cc": ["Mark Rcpengineer <mark@rcp-eng.example>", "sam@rcp-rosen.example"],
                                                             "bcc": "boss@superstars.example", "subject": "Re: east wall, with Sam"})
        assert r.status_code == 200, r.get_json()
        m = last_eml()
        assert m["To"] == "Dana Rosen <dana@rcp-rosen.example>" and "sam@rcp-rosen.example" in m["Cc"] and "mark@rcp-eng.example" in m["Cc"]
        assert m["Bcc"] == "boss@superstars.example" and m["Subject"] == "Re: east wall, with Sam" and m["In-Reply-To"] == "<rcp-1@mail.example>"
        bad = a.post(f"/api/inbox/messages/{mid}/reply", json={"body": "x", "to": "dana@rcp-rosen.example", "cc": "sam@typo"})
        assert bad.status_code == 400 and "sam@typo" in bad.get_json()["error"]
        none = a.post(f"/api/inbox/messages/{mid}/reply", json={"body": "x", "to": "", "cc": "sam@rcp-rosen.example"})
        assert none.status_code == 400 and "To" in none.get_json()["error"]
        passed += 1; print("  recipients ok")

        # ---- forward, with the email's own file
        r = a.post(f"/api/inbox/messages/{mid}/reply", json={"body": "FYI", "forward": True, "to": ["Luis <luis@superstars.example>"]})
        assert r.status_code == 200, r.get_json()
        m = last_eml()
        assert m["Subject"] == "Fwd: Pricing for the east wall" and m["To"] == "Luis <luis@superstars.example>"
        text = "".join(p.get_payload(decode=True).decode("utf-8", "replace") for p in m.walk() if p.get_content_type() == "text/plain")
        assert "---------- Forwarded message ---------" in text and "Could you send pricing?" in text
        atts = [p for p in m.walk() if p.get_content_disposition() == "attachment"]
        assert len(atts) == 1 and atts[0].get_filename() == "FISP report.pdf"
        passed += 1; print("  forward ok")

        # ---- people search
        rows = a.get("/api/inbox/people?q=rcpengineer").get_json()["data"]
        assert rows and rows[0]["email"] == "mark@rcp-eng.example" and ORG in rows[0]["where"]
        rows = a.get("/api/inbox/people?q=teammate").get_json()["data"]
        assert any(x["email"] == f"{TAG}.pm@superstarscontracting.com" and x["where"] == "Your team" for x in rows)
        assert pm.get("/api/inbox/people?q=x").status_code == 403
        passed += 1; print("  people ok")

        cp = (ROOT / "company_portal.html").read_text(encoding="utf-8")
        for needle in ("function ibRecipients(host, v, kind)", '<button type="button" data-k="fwd">→ Forward</button>', "data-show=\"cc\"",
                       "'/inbox/people?q='", "forward: R.kind === 'fwd'", "bcc: R.list('bcc')", 'id="ib-subj"'):
            assert needle in cp, needle
        passed += 1; print("  page ok")
    finally:
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: inbox recipients ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
