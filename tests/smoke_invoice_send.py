#!/usr/bin/env python3
"""Email an invoice from your Gmail (2026-11-12): a thread with the client, To / Cc / Bcc, Claude writes it, job files.

Flask test client on an ISOLATED database (honors SSC_DB_URL; builds a throwaway SQLite snapshot when unset). Gmail is the
test mail folder (SSC_MAIL_FAKE_DIR), Claude the fake model. Proves:

  * gates: the four routes are admin / C-suite (pm -> 403); a draft invoice can't be sent
  * options: the client's people, the company's people, the conversations with the client, the job's files, Gmail state
  * thread: a reply goes to the sender and everyone else on it, never back to the sender's own address
  * draft (fake model): greeting by first name, the number, the amount open
  * send refusals: no confirm, nobody in To, no PDF and no link, a reviewed amount that changed, a file from another job,
    no Gmail connected (C-suite without a link) — and nothing went out for any of them
  * reply in the thread: To / Cc / Bcc headers, In-Reply-To, the PDF + the job file attached, the new private link in the body
    opens the client's page; sent_count / sent_to recorded; the email is filed on the client and the invoice
  * a send that fails keeps the client's old link working
  * chase: a task on the chosen day; a second send replaces it; paying the invoice in full closes it
  * new email with the link only: no attachments, Bcc kept
  * marked sent: your sent mail matched by the number, refusals, nothing emailed, chase, filed; undo; by hand
  * page needles: the editor's Send window, the job page and the Finance panel open it

Run:  venv/bin/python tests/smoke_invoice_send.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import email
import json
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_inv_send_")
MAIL = Path(_TMP) / "mail"

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(MAIL), "SSC_GMAIL_POLL": "0",
                   "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(), "SSC_ASSISTANT_FAKE": "1",
                   "APP_BASE_URL": "http://test.local"})
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import inbox  # noqa: E402
import invoice_docs  # noqa: E402
import invoice_editor  # noqa: E402
import invoice_send  # noqa: E402
import invoices  # noqa: E402
import role_containment  # noqa: E402
import ssc_paths  # noqa: E402
import team_items  # noqa: E402

TAG = "invsend"
ORG = "InvSend Harbor"
PW = "InvoiceSendSmokePassword2026"
JOB, OTHER = "INVSND-A", "INVSND-B"


def _db():
    return db_layer.connect(pragma_fk=True)


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ca.ensure(conn)
        inbox.ensure(conn)
        team_items.ensure(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        mids = [r["id"] for r in conn.execute(f"SELECT id FROM mail_message WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "mail_link", "message_id", mids)
        purge(conn, "mail_message", "id", mids)
        for t in ("gmail_link", "mail_sendas"):
            purge(conn, t, "user_id", uids)
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE ?", (ORG + "%",)).fetchall()]
        invs = [r["id"] for r in conn.execute(f"SELECT id FROM invoice WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        purge(conn, "invoice_payment", "invoice_id", invs)
        purge(conn, "invoice", "id", invs)
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM crm_organization WHERE name LIKE ?", (ORG + "%",))
        conn.execute("DELETE FROM crm_task WHERE title LIKE ?", (invoice_send.CHASE_PREFIX + "%" + ORG + "%",))
        conn.execute("DELETE FROM project_documents WHERE project_code IN (?, ?)", (JOB, OTHER))
        conn.execute("DELETE FROM projects WHERE project_code IN (?, ?)", (JOB, OTHER))
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
                         (f"{TAG}.{role}@superstarscontracting.com", auth.hash_password(PW), role, f"Snd {role.title()}"))
        conn.commit()
        ids = {r["role"]: r["id"] for r in conn.execute("SELECT id, role FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()}
        conn.execute("INSERT INTO gmail_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) VALUES (?, ?, ?, 'active', ?, ?)",
                     (ids["admin"], f"{TAG}.admin@superstarscontracting.com", secret_box.encrypt_text("r"), "2026-11-01T08:00:00",
                      "openid email https://www.googleapis.com/auth/gmail.send"))
        org = crm.create_org(conn, name=ORG + " Owners", relationship_type="client")
        org = org["id"] if isinstance(org, dict) else org
        for n, e in (("Dana Rosen", "dana@invsnd-harbor.example"), ("Accounts Payable", "ap@invsnd-harbor.example")):
            conn.execute("INSERT INTO crm_contact (org_id, full_name, email, created_at) VALUES (?, ?, ?, ?)",
                         (org, n, e, datetime.now().isoformat(timespec="seconds")))
        for code in (JOB, OTHER):
            conn.execute("INSERT INTO projects (project_code, name, address) VALUES (?, ?, ?)", (code, f"InvSend {code}", "9 InvSend Quay, Brooklyn"))
        docs = {}
        for code, name in ((JOB, "lien-waiver.pdf"), (OTHER, "other-job.pdf")):
            d = ssc_paths.under_root("data_room", "project_docs", code)
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_bytes(b"%PDF-1.4 " + name.encode())
            conn.execute("INSERT INTO project_documents (project_code, category, title, doc_type, file_path, file_name, file_size, mime, "
                         "uploaded_at) VALUES (?, 'CONTRACTS', ?, 'PDF', ?, ?, ?, 'application/pdf', ?)",
                         (code, name, str(Path("data_room", "project_docs", code, name)), name, 20, datetime.now().isoformat(timespec="seconds")))
            docs[code] = conn.execute("SELECT MAX(id) AS m FROM project_documents").fetchone()["m"]
        e_ = inbox._enc
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("INSERT INTO mail_message (user_id, gmail_id, thread_id, rfc_id_enc, direction, received_at, from_enc, to_enc, cc_enc, "
                     "subject_enc, body_enc, snippet_enc, needs_action, status, ai_state, client_org_id, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,'new','done',?,?)",
                     (ids["admin"], "g-snd-1", "thr-snd-1", e_("<snd-1@mail.example>"), "in", now, e_("Dana Rosen <dana@invsnd-harbor.example>"),
                      e_(f"{TAG}.admin@superstarscontracting.com"), e_("Accounts Payable <ap@invsnd-harbor.example>"),
                      e_("Roof probes: access Tuesday?"), e_("Send the invoice over when the probes are done."), e_("Send the invoice"),
                      org, now))
        conn.commit()
        mid = conn.execute("SELECT id FROM mail_message WHERE gmail_id = 'g-snd-1'").fetchone()["id"]
        return {**ids, "org": org, "mid": mid, "doc": docs[JOB], "doc_other": docs[OTHER]}
    finally:
        conn.close()


def _mails(kind="") -> list:
    return sorted(MAIL.glob(f"gmail-{kind}*.eml")) if MAIL.exists() else []


def main() -> int:
    app = Flask("invsend", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    role_containment.register(app)
    inbox.register(app)
    invoices.register(app)
    invoice_docs.register(app)
    invoice_editor.register(app)
    invoice_send.register(app)
    ids = _seed()

    def login(role):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@superstarscontracting.com", "password": PW})
        assert r.status_code == 200, (role, r.status_code)
        return c

    a, cs, pm = login("admin"), login("c_suite"), login("pm")
    checks = 0
    conn = _db()
    try:
        due = (date.today() + timedelta(days=20)).isoformat()
        inv = invoices.create(conn, {"kind": "progress", "client_org_id": ids["org"], "amount": 6000, "project_code": JOB,
                                     "description": "Two roof probes"})
        iid = inv["id"]

        # ---- gates + draft
        assert pm.get(f"/api/invoices/{iid}/send-options").status_code == 403
        assert pm.post(f"/api/invoices/{iid}/send-gmail", json={"confirm": True}).status_code == 403
        d = a.get(f"/api/invoices/{iid}/send-options")
        assert d.status_code == 400 and "Issue" in d.get_json()["error"]
        invoices.issue(conn, iid, due_on=due)
        checks += 1

        # ---- options + thread + draft
        o = a.get(f"/api/invoices/{iid}/send-options").get_json()["data"]
        assert o["gmail"]["can_send"] and o["gmail"]["email"].startswith(TAG)
        assert {c["email"] for c in o["contacts"]} == {"dana@invsnd-harbor.example", "ap@invsnd-harbor.example"}
        assert any(t["email"] == f"{TAG}.c_suite@superstarscontracting.com" for t in o["team"])
        assert not any(t["email"] == f"{TAG}.admin@superstarscontracting.com" for t in o["team"])   # not yourself
        assert [t["id"] for t in o["threads"]] == [ids["mid"]]
        assert [x["id"] for x in o["documents"]] == [ids["doc"]] and "file_path" not in o["documents"][0]
        assert o["attachment"].startswith(f"Invoice {inv['number']}") and o["attachment"].endswith(".pdf")
        assert not cs.get(f"/api/invoices/{iid}/send-options").get_json()["data"]["gmail"]["can_send"]
        t = a.get(f"/api/invoices/{iid}/send-thread/{ids['mid']}").get_json()["data"]
        assert "dana@invsnd-harbor.example" in t["to"] and "ap@invsnd-harbor.example" in t["cc"] and TAG not in t["to"] + t["cc"]
        assert t["subject"] == "Re: Roof probes: access Tuesday?"
        assert cs.get(f"/api/invoices/{iid}/send-thread/{ids['mid']}").status_code == 404   # someone else's inbox
        dr = a.post(f"/api/invoices/{iid}/send-draft", json={"style": "first", "to_name": "Dana Rosen"}).get_json()["data"]
        assert dr["message"].startswith("Hi Dana,") and inv["number"] in dr["message"] and "$6,000.00" in dr["message"]
        dr2 = a.post(f"/api/invoices/{iid}/send-draft", json={"to_name": "ap@x.example"}).get_json()["data"]
        assert dr2["style"] == "first" and dr2["message"].startswith("Hello,")
        checks += 1

        # ---- refusals: nothing goes out
        base = {"how": "reply", "mail_id": ids["mid"], "to": "Dana Rosen <dana@invsnd-harbor.example>",
                "cc": "ap@invsnd-harbor.example", "bcc": f"{TAG}.c_suite@superstarscontracting.com", "subject": t["subject"],
                "message": dr["message"], "attach_pdf": True, "include_link": True, "documents": [ids["doc"]],
                "follow_up": "due", "expected_balance": 6000, "confirm": True}
        for change, word in (({"confirm": None}, "confirm"), ({"to": ""}, "To"), ({"attach_pdf": False, "include_link": False}, "PDF"),
                             ({"expected_balance": 5000}, "amount open"), ({"documents": [ids["doc_other"]]}, "isn't on this job"),
                             ({"message": " "}, "message")):
            r = a.post(f"/api/invoices/{iid}/send-gmail", json={**base, **change})
            assert r.status_code == 400 and word in r.get_json()["error"], (change, r.get_json())
        r = cs.post(f"/api/invoices/{iid}/send-gmail", json={**base, "how": "new"})
        assert r.status_code == 400 and "Connect your Gmail" in r.get_json()["error"]
        assert not _mails()
        checks += 1

        # ---- reply in the thread
        pdf = Path(_TMP) / "inv.pdf"
        pdf.write_bytes(b"%PDF-1.4 invoice")
        real = invoice_docs.render_pdf, invoice_docs.pdf_file
        invoice_docs.render_pdf = lambda conn, i, actor=None: {"ok": True}
        invoice_docs.pdf_file = lambda conn, i: pdf
        try:
            r = a.post(f"/api/invoices/{iid}/send-gmail", json=base)
            assert r.status_code == 200, r.get_json()
            out = r.get_json()["data"]
            assert out["how"] == "reply" and out["people"] == 3 and out["files"] == 2 and out["chase"] == due
            em = email.message_from_bytes(_mails("reply")[0].read_bytes())
            assert em["To"] == "Dana Rosen <dana@invsnd-harbor.example>" and em["Cc"] == "ap@invsnd-harbor.example"
            assert em["Bcc"] == f"{TAG}.c_suite@superstarscontracting.com" and em["In-Reply-To"] == "<snd-1@mail.example>"
            assert em["Subject"] == "Re: Roof probes: access Tuesday?"
            atts = [(x.get_filename(), x.get_payload(decode=True)) for x in em.walk() if x.get_content_disposition() == "attachment"]
            assert atts == [(o["attachment"], b"%PDF-1.4 invoice"), ("lien-waiver.pdf", b"%PDF-1.4 lien-waiver.pdf")], [a_[0] for a_ in atts]
            text = next(x for x in em.walk() if x.get_content_type() == "text/plain").get_payload(decode=True).decode()
            link = re.search(r"http://test\.local(/i/[\w-]+)", text).group(1)
            assert text.startswith("Hi Dana,") and "View the invoice and how to pay:" in text
            assert app.test_client().get(link).status_code == 200
            row = invoices.get(conn, iid)
            assert row["sent_count"] == 1 and row["sent_to"] == "dana@invsnd-harbor.example" and row["sent_name"] == "Dana Rosen"
            filed = {(x["entity_type"], x["entity_id"]) for x in conn.execute("SELECT entity_type, entity_id FROM mail_link WHERE message_id = ?",
                                                                              (ids["mid"],)).fetchall()}
            assert ("client", str(ids["org"])) in filed and ("invoice", str(iid)) in filed
            act = conn.execute("SELECT summary FROM crm_activity WHERE entity_type = 'organization' AND entity_id = ? ORDER BY id DESC",
                               (ids["org"],)).fetchone()["summary"]
            assert "from Gmail" in act and "2 more" in act
            checks += 1

            # ---- a send that fails keeps the old link
            real_reply = inbox.send_reply
            inbox.send_reply = lambda *x, **k: (_ for _ in ()).throw(ValueError("Gmail did not send it (500). Nothing went out"))
            try:
                r = a.post(f"/api/invoices/{iid}/send-gmail", json={**base, "documents": []})
                assert r.status_code == 400 and "Nothing went out" in r.get_json()["error"]
            finally:
                inbox.send_reply = real_reply
            assert app.test_client().get(link).status_code == 200 and invoices.get(conn, iid)["sent_count"] == 1
            checks += 1

            # ---- chase task: one per invoice, replaced by a new send, closed when paid
            title = f"{invoice_send.CHASE_PREFIX}{inv['number']} %"
            tasks = conn.execute("SELECT due_date, status, assignee_user_id FROM crm_task WHERE title LIKE ?", (title,)).fetchall()
            assert [(x["due_date"], x["status"], x["assignee_user_id"]) for x in tasks] == [(due, "open", ids["admin"])]
            r = a.post(f"/api/invoices/{iid}/send-gmail", json={**base, "how": "new", "mail_id": None, "to": "dana@invsnd-harbor.example",
                                                                 "cc": "", "attach_pdf": False, "documents": [], "follow_up": "after7",
                                                                 "subject": "Invoice reminder"})
            assert r.status_code == 200, r.get_json()
            em2 = email.message_from_bytes(_mails("new")[0].read_bytes())
            assert em2["Subject"] == "Invoice reminder" and em2["Bcc"] and not em2["In-Reply-To"]
            assert not [x for x in em2.walk() if x.get_content_disposition() == "attachment"]
            assert app.test_client().get(link).status_code != 200   # the new send made a new link
            later = (date.fromisoformat(due) + timedelta(days=7)).isoformat()
            st = sorted((x["due_date"], x["status"]) for x in conn.execute("SELECT due_date, status FROM crm_task WHERE title LIKE ?", (title,)).fetchall())
            assert st == [(due, "done"), (later, "open")], st
            o2 = a.get(f"/api/invoices/{iid}/send-options").get_json()["data"]
            assert o2["chase"]["due_date"] == later
            invoices.record_payment(conn, iid, amount=6000, paid_on=date.today().isoformat(), actor=ids["admin"])
            assert {x["status"] for x in conn.execute("SELECT status FROM crm_task WHERE title LIKE ?", (title,)).fetchall()} == {"done"}
            assert invoice_send.follow_up_date(invoices.get(conn, iid), "due") is None   # paid: nothing to chase
            dr3 = a.post(f"/api/invoices/{iid}/send-draft", json={"to_name": "Dana"}).get_json()["data"]
            assert dr3["style"] == "receipt" and "paid in full" in dr3["message"]
            checks += 1
        finally:
            invoice_docs.render_pdf, invoice_docs.pdf_file = real
        inv_late = {"status": "issued", "due_at": (date.today() - timedelta(days=30)).isoformat()}
        assert invoice_send.follow_up_date(inv_late, "due") == (date.today() + timedelta(days=1)).isoformat()   # never in the past

        # ---- marked sent (2026-11-13): sent from the Gmail app / Outlook / by hand; nothing is emailed
        tomorrow, today = (date.today() + timedelta(days=1)).isoformat(), date.today().isoformat()
        inv2 = invoices.create(conn, {"kind": "progress", "client_org_id": ids["org"], "amount": 2500, "project_code": JOB,
                                      "description": "Parapet probes"})
        i2 = inv2["id"]
        assert a.post(f"/api/invoices/{i2}/mark-sent", json={"confirm": True}).status_code == 400   # a draft
        invoices.issue(conn, i2, due_on=(date.today() + timedelta(days=5)).isoformat())
        conn.execute("UPDATE invoice SET due_at = ? WHERE id = ?", ((date.today() - timedelta(days=2)).isoformat(), i2))   # 2 days late
        conn.commit()
        e_, now = inbox._enc, datetime.now().isoformat(timespec="seconds")
        for gid, subj, files in (("g-out-1", f"Invoice {inv2['number']} · parapet probes", [{"name": f"{inv2['number']}.pdf"}]),
                                 ("g-out-2", "Lunch Friday?", []), ("g-out-3", "Scope question", [{"name": f"Invoice {inv2['number']}.pdf"}])):
            conn.execute("INSERT INTO mail_message (user_id, gmail_id, thread_id, direction, received_at, from_enc, to_enc, cc_enc, subject_enc, "
                         "snippet_enc, attachments_enc, needs_action, status, ai_state, created_at) VALUES (?,?,?,'out',?,?,?,?,?,?,?,0,'new','done',?)",
                         (ids["admin"], gid, "thr-" + gid, now, e_(f"{TAG}.admin@superstarscontracting.com"),
                          e_("Dana Rosen <dana@invsnd-harbor.example>"), e_("ap@invsnd-harbor.example"), e_(subj), e_("Attached."),
                          e_(json.dumps(files)), now))
        conn.commit()
        out_id = {r["gmail_id"]: r["id"] for r in conn.execute("SELECT id, gmail_id FROM mail_message WHERE gmail_id LIKE 'g-out-%'").fetchall()}
        assert pm.get(f"/api/invoices/{i2}/sent-matches").status_code == 403
        assert pm.post(f"/api/invoices/{i2}/mark-sent", json={"confirm": True, "mail_id": out_id["g-out-1"]}).status_code == 403
        sm = a.get(f"/api/invoices/{i2}/sent-matches").get_json()["data"]
        assert {m["id"] for m in sm["matches"]} == {out_id["g-out-1"], out_id["g-out-3"]}, sm["matches"]   # subject or file name
        assert all(m["client"] for m in sm["matches"]) and sm["gmail"]["email"]
        assert cs.get(f"/api/invoices/{i2}/sent-matches").get_json()["data"]["matches"] == []   # only your own sent mail
        n_mails, tok = len(_mails()), invoices.get(conn, i2).get("token_hash")
        for body, word in (({"mail_id": out_id["g-out-1"]}, "Confirm"), ({"confirm": True, "mail_id": ids["mid"]}, "sent mail"),
                           ({"confirm": True, "mail_id": out_id["g-out-2"]}, "doesn't mention"),
                           ({"confirm": True, "sent_on": tomorrow, "to": "Dana"}, "future"),
                           ({"confirm": True, "sent_on": "2020-01-02", "to": "Dana"}, "issued"),
                           ({"confirm": True, "sent_on": today, "to": " "}, "who it went to"), ({"confirm": True, "sent_on": "", "to": "Dana"}, "day")):
            r = a.post(f"/api/invoices/{i2}/mark-sent", json=body)
            assert r.status_code == 400 and word in r.get_json()["error"], (body, r.get_json())
        r = a.post(f"/api/invoices/{i2}/mark-sent", json={"confirm": True, "mail_id": out_id["g-out-1"], "follow_up": "tomorrow"})
        assert r.status_code == 200, r.get_json()
        mk = r.get_json()["data"]
        assert mk["how"] == "gmail_outside" and mk["chase"] == tomorrow
        row = invoices.get(conn, i2)
        assert (row["sent_at"], row["sent_to"], row["sent_name"], row["sent_count"], row["sent_how"]) == \
            (now, "dana@invsnd-harbor.example", "Dana Rosen", 1, "gmail_outside")
        assert row.get("token_hash") == tok and len(_mails()) == n_mails   # nothing emailed, no new link
        filed = {(x["entity_type"], x["entity_id"]) for x in conn.execute("SELECT entity_type, entity_id FROM mail_link WHERE message_id = ?",
                                                                          (out_id["g-out-1"],)).fetchall()}
        assert ("client", str(ids["org"])) in filed and ("invoice", str(i2)) in filed
        title2 = f"{invoice_send.CHASE_PREFIX}{inv2['number']} %"
        assert [(x["due_date"], x["status"]) for x in conn.execute("SELECT due_date, status FROM crm_task WHERE title LIKE ?", (title2,)).fetchall()] == [(tomorrow, "open")]
        assert "marked sent from Gmail" in conn.execute("SELECT summary FROM crm_activity WHERE entity_type = 'organization' AND entity_id = ? "
                                                        "ORDER BY id DESC", (ids["org"],)).fetchone()["summary"]
        r = a.post(f"/api/invoices/{i2}/mark-sent", json={"confirm": True, "sent_on": today, "to": "Dana"})
        assert r.status_code == 400 and "already" in r.get_json()["error"]
        # undo: back to not sent, the chase cancelled, the email no longer filed on the invoice (still on the client)
        r = a.post(f"/api/invoices/{i2}/unmark-sent", json={"mail_id": out_id["g-out-1"]})
        assert r.status_code == 200, r.get_json()
        row = invoices.get(conn, i2)
        assert not row["sent_at"] and not row["sent_to"] and row["sent_count"] == 0 and not row["sent_how"]
        assert {x["status"] for x in conn.execute("SELECT status FROM crm_task WHERE title LIKE ?", (title2,)).fetchall()} == {"cancelled"}
        filed = {(x["entity_type"], x["entity_id"]) for x in conn.execute("SELECT entity_type, entity_id FROM mail_link WHERE message_id = ?",
                                                                          (out_id["g-out-1"],)).fetchall()}
        assert ("invoice", str(i2)) not in filed and ("client", str(ids["org"])) in filed
        # another way: by hand, to a name with no email address
        r = a.post(f"/api/invoices/{i2}/mark-sent", json={"confirm": True, "sent_on": today, "to": "Dana Rosen", "how": "hand", "follow_up": ""})
        assert r.status_code == 200, r.get_json()
        row = invoices.get(conn, i2)
        assert row["sent_how"] == "hand" and row["sent_name"] == "Dana Rosen" and row["sent_to"] is None and row["sent_at"][:10] == today
        assert r.get_json()["data"]["chase"] is None
        conn.execute("UPDATE invoice SET sent_how = 'gmail', sent_count = 2 WHERE id = ?", (i2,))   # since emailed from the app
        conn.commit()
        r = a.post(f"/api/invoices/{i2}/unmark-sent", json={})
        assert r.status_code == 400 and "marked sent yourself" in r.get_json()["error"]
        assert invoices.get(conn, i2)["sent_at"]
        checks += 1
    finally:
        conn.close()

    # ---- pages
    ed = (ROOT / "invoice_editor.html").read_text(encoding="utf-8")
    for needle in ("'/invoices/' + ID + '/send-options'", "'/send-thread/'", "'/send-draft'", "'/send-gmail'", 'id="sx-in-cc"',
                   'id="sx-in-bcc"', 'data-how="reply"', "Write it with Claude", 'id="sx-fu"', "Send now from my Gmail", "get('send')",
                   'class="btn-primary" type="button" id="mail-go"'):
        assert needle in ed, needle
    # claude_drawer.js opens any clicked [data-doc] as a Claude document and cancels the click: a job-file checkbox
    # named that way could never be ticked (found on the phone check, 2026-11-12)
    assert "data-doc=" not in ed and "data-jobfile=" in ed
    job =(ROOT / "dashboard-static.html").read_text(encoding="utf-8")
    assert "'/edit?send=1'" in job
    # 2026-11-13: the job's invoice rows keep their actions in one ⋯ menu; Mark as sent opens the shared window
    for needle in ('class="pay-dots" data-menu="${r.id}"', "['Mark as sent…', 'mark']", "['Mark as not sent', 'unmark']",
                   "invoice_mark_sent.js", 'role="menuitem"'):
        assert needle in job, needle
    assert 'id="sx-already"' in ed and "SSCMarkSent.open(ID" in ed
    ms = (ROOT / "static/js/invoice_mark_sent.js").read_text(encoding="utf-8")
    for needle in ("/sent-matches'", "/mark-sent'", "/unmark-sent'", "confirm: true", "Nothing is emailed", "SSCDatePicker"):
        assert needle in ms, needle
    portal = (ROOT / "company_portal.html").read_text(encoding="utf-8")
    assert portal.count("/edit?send=1'") >= 2 and "Send from your Gmail" in portal
    checks += 1

    print(f"PASS: invoice send — gates, options, thread, Claude draft, refusals, reply with Cc/Bcc + files, link kept on failure, "
          f"chase task, new email, marked sent + undo, pages ({checks} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
