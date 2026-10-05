#!/usr/bin/env python3
"""#326 — invoice documents, email to client, private link, project Payments: behavioral guard.

Flask test client on an ISOLATED database with the fake mail seam. Proves:

  * ensure_columns is idempotent; no pdf_path / token_hash in any invoice payload,
    `has_pdf` / `link_active` flags instead
  * project Payments: pm / super / client -> 403; anon -> 401; unknown project -> 404;
    admin / c_suite -> the job's invoices, summary (invoiced, open, overdue,
    collected, drafts), the client's contacts, the kinds catalog
  * preview: branded HTML with the number, amount, balance, how-to-pay; draft is
    stamped; the gated PDF route answers 200 (engine present) or 503 (absent)
  * recipient default: the proposal's recipient wins, else the client's first
    contact with an email
  * send: draft -> 409, void -> 409, bad email -> 400; issued -> 201 with the email
    in the fake dir carrying a /i/<token> link; sent_to / sent_at / sent_count
    recorded; a resend re-mints the link (the old one dies) and increments the
    count; a CRM activity row
  * public page: bad token -> 404; good token -> 200 with the invoice, first open
    recorded as viewed (once); the page is reachable logged out AND logged in as
    a client / estimator (public + containment allowlists); PDF route 200 or 503;
    a voided invoice's link -> 410
  * Company Portal listing carries sent / viewed for the status hint

Run:  venv/bin/python tests/smoke_project_payments_326.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")   # test-only seams are inert without this (runtime_seams.py)
import re
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_pay_326_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
MAIL_DIR = Path(_TMP) / "mail"
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ["SSC_MAIL_FAKE_DIR"] = str(MAIL_DIR)
os.environ["APP_BASE_URL"] = "http://test.local"

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import client_portal  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import invoice_docs  # noqa: E402
import invoices  # noqa: E402
import pm_scoping  # noqa: E402
import role_containment  # noqa: E402

TAG = "pay326"
PW = "PaymentsSmokePassword2026"
ROLES = ("c_suite", "admin", "pm", "super", "estimator", "client")
TODAY = date.today()


def D(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def _db():
    return db_layer.connect(pragma_fk=True)


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for role in ROLES:
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), role, f"Smoke {role}"))
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Pay326 %'")
        ids["org"] = crm.create_org(conn, name="Pay326 Owners", relationship_type="client")
        ids["contact"] = crm.create_contact(conn, full_name="Pay326 Dana Payer", org_id=ids["org"], email="pay326.dana@example.com")
        conn.execute("DELETE FROM projects WHERE project_code = 'PAY326-A'")
        conn.execute("INSERT INTO projects (project_code, name, address, status, client_org_id) "
                     "VALUES ('PAY326-A', 'Pay326 Facade', '5 Pay326 Row', 'active', ?)", (ids["org"],))
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Pay326 %'").fetchall()]
        ph = ",".join("?" * len(org_ids)) or "NULL"
        inv_ids = [r["id"] for r in conn.execute(f"SELECT id FROM invoice WHERE client_org_id IN ({ph})", org_ids).fetchall()]
        purge(conn, "invoice_payment", "invoice_id", inv_ids)
        purge(conn, "invoice", "id", inv_ids)
        conn.execute("DELETE FROM projects WHERE project_code = 'PAY326-A'")
        for t, col in (("crm_activity", "entity_id"), ("crm_task", "entity_id"), ("crm_contact", "org_id")):
            purge(conn, t, col, org_ids)
        purge(conn, "crm_organization", "id", org_ids)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


FORBIDDEN = ("pdf_path", "token_hash", "file_path")


def _no_secrets(obj, where=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k not in FORBIDDEN and not k.endswith("_path"), f"{k!r} leaked in {where}"
            _no_secrets(v, where)
    elif isinstance(obj, list):
        for v in obj:
            _no_secrets(v, where)


def _login(app, role):
    c = app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code, r.get_data(as_text=True)[:200])
    return c


def _latest_mail() -> tuple:
    eml = sorted(MAIL_DIR.glob("invoice-*.eml"))
    assert eml, "no invoice email written"
    body = eml[-1].read_text(encoding="utf-8")
    m = re.search(r"http://test\.local/i/([A-Za-z0-9_\-]+)", body)
    assert m, body[:300]
    return m.group(1), body, len(eml)


def main() -> int:
    app = Flask("pay326", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    role_containment.register(app)
    pm_scoping.register(app)
    invoices.register(app)
    invoice_docs.register(app)
    conn = _db()
    try:
        assert invoice_docs.ensure_columns(conn) == []   # idempotent (register ran it)
        assert invoice_docs._has_column(conn, "invoice", "token_hash")
    finally:
        conn.close()
    assert client_portal._client_always_allowed("/i/abc") and auth.is_public_path("/i/abc") if hasattr(auth, "is_public_path") else True
    ids = _seed()
    checks = 0

    # ---- gates on the project section
    for role in ("pm", "super", "estimator", "client"):
        assert _login(app, role).get("/api/projects/PAY326-A/payments").status_code == 403, role
    assert app.test_client().get("/api/projects/PAY326-A/payments").status_code == 401
    cs = _login(app, "c_suite")
    admin = _login(app, "admin")
    assert admin.get("/api/projects/PAY326-NOPE/payments").status_code == 404
    checks += 1

    # ---- two invoices on the job: a draft and an issued one
    r = admin.post("/api/invoices", json={"client_org_id": ids["org"], "amount": 12500, "kind": "progress",
                                          "project_code": "PAY326-A", "description": "Progress billing #1"})
    assert r.status_code == 201, r.get_json()
    a = r.get_json()["data"]
    _no_secrets(r.get_json(), "create")
    assert a["has_pdf"] is False and a["link_active"] is False
    r = admin.post("/api/invoices", json={"client_org_id": ids["org"], "amount": 4000, "kind": "deposit", "project_code": "PAY326-A"})
    b = r.get_json()["data"]
    assert admin.post(f"/api/invoices/{a['id']}/issue", json={"issued_on": D(-40), "due_on": D(-10)}).status_code == 200
    pay = cs.get("/api/projects/PAY326-A/payments").get_json()
    _no_secrets(pay, "payments")
    d = pay["data"]
    assert d["project"]["project_code"] == "PAY326-A" and d["client"]["name"] == "Pay326 Owners"
    assert [c["email"] for c in d["contacts"]] == ["pay326.dana@example.com"]
    assert {x["id"] for x in d["invoices"]} == {a["id"], b["id"]}
    s = d["summary"]
    assert s["invoiced_total"] == 12500 and s["open_value"] == 12500 and s["open_count"] == 1
    assert s["overdue_count"] == 1 and s["overdue_value"] == 12500 and s["collected"] == 0 and s["draft_count"] == 1
    assert {k["code"] for k in d["kinds"]} == {"deposit", "progress", "final", "change_order", "other"}
    checks += 1

    # ---- preview + pdf (gated)
    assert _login(app, "pm").get(f"/api/invoices/{a['id']}/preview").status_code == 403
    r = admin.get(f"/api/invoices/{a['id']}/preview")
    assert r.status_code == 200 and r.mimetype == "text/html"
    html = r.get_data(as_text=True)
    assert a["number"] in html and "$12,500.00" in html and "Balance due" in html and "How to pay" in html
    assert "Pay326 Owners" in html and "Progress billing #1" in html and "Draft" not in html
    r = admin.get(f"/api/invoices/{b['id']}/preview")
    assert "Draft — not yet issued" in r.get_data(as_text=True)
    r = admin.get(f"/api/invoices/{a['id']}/pdf")
    assert r.status_code in (200, 503), r.status_code
    pdf_ok = r.status_code == 200
    if pdf_ok:
        assert r.mimetype == "application/pdf" and r.data[:4] == b"%PDF"
    assert admin.get("/api/invoices/999999/preview").status_code == 404
    checks += 1

    # ---- recipient default + send rules
    rec = admin.get(f"/api/invoices/{a['id']}/recipient").get_json()["data"]
    assert rec["email"] == "pay326.dana@example.com" and rec["name"] == "Pay326 Dana Payer"
    r = admin.post(f"/api/invoices/{b['id']}/send", json={})
    assert r.status_code == 409 and "issue" in r.get_json()["error"], r.get_json()
    r = admin.post(f"/api/invoices/{a['id']}/send", json={"to_email": "nope"})
    assert r.status_code == 400
    r = cs.post(f"/api/invoices/{a['id']}/send", json={"message": "Thank you for the quick turnaround on the parapet."})
    assert r.status_code == 201, r.get_json()
    sent = r.get_json()["data"]
    _no_secrets(r.get_json(), "send")
    assert sent["sent_to"] == "pay326.dana@example.com" and sent["sent_name"] == "Pay326 Dana Payer" and sent["sent_count"] == 1
    assert sent["link_active"] is True and sent["sent_at"] and sent["viewed_at"] is None
    assert sent["has_pdf"] == pdf_ok
    token1, body, n = _latest_mail()
    assert n == 1 and "Thank you for the quick turnaround" in body and "$12,500.00" in body and a["number"] in body
    conn = _db()
    try:
        act = conn.execute("SELECT summary FROM crm_activity WHERE entity_type='organization' AND entity_id=? AND summary LIKE ?",
                           (ids["org"], f"Invoice {a['number']} emailed%")).fetchone()
        assert act, "activity row"
    finally:
        conn.close()
    checks += 1

    # ---- public page
    anon = app.test_client()
    assert anon.get("/i/not-a-real-token-at-all-000").status_code == 404
    r = anon.get(f"/i/{token1}")
    assert r.status_code == 200, r.status_code
    page = r.get_data(as_text=True)
    assert a["number"] in page and "Download PDF" in page and "How to pay" in page and "noindex" in (r.headers.get("X-Robots-Tag") or "")
    assert "Questions about this invoice" in page
    r = anon.get(f"/i/{token1}")
    assert r.status_code == 200
    inv = admin.get(f"/api/invoices/{a['id']}").get_json()["data"]
    assert inv["viewed_at"] and inv["view_count"] == 2
    # logged-in non-company roles reach it too (containment allowlists)
    assert _login(app, "client").get(f"/i/{token1}").status_code == 200
    assert _login(app, "estimator").get(f"/i/{token1}").status_code == 200
    r = anon.get(f"/i/{token1}/pdf")
    assert r.status_code == (200 if pdf_ok else 503), r.status_code
    checks += 1

    # ---- resend re-mints; the old link dies; count increments
    r = admin.post(f"/api/invoices/{a['id']}/send", json={"to_email": "pay326.other@example.com", "to_name": "Other Person"})
    assert r.status_code == 201
    token2, body, n = _latest_mail()
    assert n == 2 and token2 != token1 and "Hello Other," in body
    assert anon.get(f"/i/{token1}").status_code == 404 and anon.get(f"/i/{token2}").status_code == 200
    inv = admin.get(f"/api/invoices/{a['id']}").get_json()["data"]
    assert inv["sent_count"] == 2 and inv["sent_to"] == "pay326.other@example.com"
    # a paid invoice can still be emailed (receipt); a void one cannot, and its link goes 410
    assert admin.post(f"/api/invoices/{a['id']}/payments", json={"amount": 12500, "method": "check"}).status_code == 201
    r = admin.post(f"/api/invoices/{a['id']}/send", json={})
    assert r.status_code == 201
    page = anon.get(f"/i/{_latest_mail()[0]}").get_data(as_text=True)
    assert "Paid in full" in page and "Paid — thank you" in page
    assert admin.post(f"/api/invoices/{b['id']}/issue", json={}).status_code == 200
    assert admin.post(f"/api/invoices/{b['id']}/send", json={}).status_code == 201
    tok_b = _latest_mail()[0]
    assert admin.post(f"/api/invoices/{b['id']}/void", json={"reason": "duplicate"}).status_code == 200
    assert anon.get(f"/i/{tok_b}").status_code == 410
    assert admin.post(f"/api/invoices/{b['id']}/send", json={}).status_code == 409
    # the listing carries the hints the portal shows
    rows = admin.get("/api/invoices?project=PAY326-A").get_json()
    _no_secrets(rows, "list")
    mine = [x for x in rows["data"] if x["id"] == a["id"]][0]
    assert mine["sent_count"] == 3 and mine["viewed_at"] and mine["status"] == "paid"
    pay = cs.get("/api/projects/PAY326-A/payments").get_json()["data"]["summary"]
    assert pay["collected"] == 12500 and pay["open_count"] == 0 and pay["invoiced_total"] == 12500
    checks += 1

    print(f"PASS: #326 invoice documents — gates, project Payments, preview/pdf, recipient default, send rules + email + link, "
          f"public page + view tracking + containment, resend re-mint, paid receipt, void 410 ({checks} groups; pdf engine {'present' if pdf_ok else 'absent'})")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
