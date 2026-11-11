#!/usr/bin/env python3
"""The invoice editor (2026-11-11): edit the whole invoice like a proposal; Download on every invoice.

Flask test client on an ISOLATED database (honors SSC_DB_URL; builds a throwaway SQLite snapshot when unset). Proves:

  * gates: the page and its API are admin / C-suite only (pm -> 403, anon -> not served)
  * one template: an invoice from before the editor is one line (its description + amount), drawn by the same
    standard layout; LAYOUT is 3 so older stored PDFs redraw
  * editor view: mode edit + the template in edit mode (outlined fields, line rows); preview is never editable
  * lines: saving lines sets amount (their sum) and description (first line); several lines print one row each
    + "Total This Invoice"; blank lines dropped; none / <= 0 / too many refused
  * the one-amount Edit form still works on a one-line invoice and is refused on a several-line one
  * wording: bill to / project / payment / thanks / client note print; blank puts the standard wording back;
    the office note (invoice.notes) never prints
  * issued: lines can't go below what was paid; the stored PDF is dropped on change
  * modes: AIA (G702 is the document) and paid are locked; a Claude design asks to switch first
  * Ask Claude (fake model): a line added, Undo through PATCH puts the old lines back
  * Download: ?download=1 saves the file (attachment); without it the PDF opens inline
  * page needles: job page Download / Edit link / #payments, company panel, the editor page

Run:  venv/bin/python tests/smoke_invoice_editor.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
os.environ["SSC_ASSISTANT_FAKE"] = "1"
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_inv_editor_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ["SSC_MAIL_FAKE_DIR"] = str(Path(_TMP) / "mail")
os.environ["APP_BASE_URL"] = "http://test.local"

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import invoice_docs  # noqa: E402
import invoice_editor  # noqa: E402
import invoices  # noqa: E402
import role_containment  # noqa: E402

TAG = "invedit"
PW = "InvoiceEditorSmokePassword2026"
ROLES = ("admin", "c_suite", "pm")


def _db():
    return db_layer.connect(pragma_fk=True)


def _seed() -> dict:
    conn = _db()
    try:
        for role in ROLES:
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), role, f"Smoke {role}"))
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'InvEdit %'")
        org = crm.create_org(conn, name="InvEdit Harbor Owners", relationship_type="client")
        conn.execute("DELETE FROM projects WHERE project_code = 'INVED-A'")
        conn.execute("INSERT INTO projects (project_code, name, address) VALUES ('INVED-A', 'InvEdit Quay job', '9 InvEdit Quay')")
        conn.commit()
        return {"org": org}
    finally:
        conn.close()


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'InvEdit %'").fetchall()]
        ph = ",".join("?" * len(org_ids)) or "NULL"
        inv_ids = [r["id"] for r in conn.execute(f"SELECT id FROM invoice WHERE client_org_id IN ({ph})", org_ids).fetchall()]
        purge(conn, "invoice_payment", "invoice_id", inv_ids)
        purge(conn, "invoice", "id", inv_ids)
        if org_ids:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph})", org_ids)
        conn.execute("DELETE FROM projects WHERE project_code = 'INVED-A'")
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'InvEdit %'")
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(app, role):
    c = app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code)
    return c


def main() -> int:
    app = Flask("invedit", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    role_containment.register(app)
    invoices.register(app)
    invoice_docs.register(app)
    invoice_editor.register(app)
    ids = _seed()
    admin = _login(app, "admin")
    checks = 0
    assert invoice_docs.LAYOUT == 3

    conn = _db()
    try:
        inv = invoices.create(conn, {"kind": "progress", "client_org_id": ids["org"], "amount": 1000,
                                     "project_code": "INVED-A", "description": "Old single line"})
        iid = inv["id"]

        # ---- gates
        pm = _login(app, "pm")
        assert pm.get(f"/invoices/{iid}/edit").status_code == 403
        assert pm.get(f"/api/invoices/{iid}/editor").status_code == 403
        assert app.test_client().get(f"/api/invoices/{iid}/editor").status_code == 401
        assert app.test_client().get(f"/invoices/{iid}/edit").status_code in (302, 401)
        page = admin.get(f"/invoices/{iid}/edit")
        assert page.status_code == 200 and b"Invoice editor" in page.data
        assert _login(app, "c_suite").get(f"/api/invoices/{iid}/editor").status_code == 200
        checks += 1

        # ---- one template: the old invoice is one line; editor = edit mode, preview never editable
        assert invoices.get(conn, iid)["lines"] == [{"description": "Old single line", "amount": 1000.0}]
        v = admin.get(f"/api/invoices/{iid}/editor").get_json()["data"]
        assert v["mode"] == "edit" and v["back"] == "/projects/INVED-A#payments"
        assert 'contenteditable="true"' in v["html"] and 'data-li="0"' in v["html"] and "Old single line" in v["html"]
        assert 'data-f="bill_to_text"' in v["html"] and 'data-f="doc_notes"' in v["html"]
        assert v["texts"]["bill_to_text"].startswith("InvEdit Harbor Owners")
        pv = admin.get(f"/api/invoices/{iid}/preview").get_data(as_text=True)
        assert "contenteditable" not in pv and "Old single line" in pv and "$1,000.00" in pv and "inv-word" in pv
        for k in ("design_html", "pdf_path", "token_hash"):
            assert k not in v["invoice"]
        checks += 1

        # ---- lines
        r = admin.patch(f"/api/invoices/{iid}", json={"lines": [
            {"description": "Brick repointing, front elevation", "amount": "17,575.00"},
            {"description": "", "amount": 0},
            {"description": "Lintel replacement, floors 3–5", "amount": 10400},
            {"description": "Credit: shed already billed", "amount": -1800}]})
        assert r.status_code == 200, r.get_json()
        d = r.get_json()["data"]
        assert d["amount"] == 26175.0 and d["description"] == "Brick repointing, front elevation"
        assert len(d["lines"]) == 3, "the blank line is dropped"
        pv = admin.get(f"/api/invoices/{iid}/preview").get_data(as_text=True)
        assert pv.count('class="ln"') == 3 and "Total This Invoice" in pv and "$26,175.00" in pv and "−" not in pv.split("inv-work")[1][:40]
        for bad, why in (([], "at least one line"), ([{"description": "x", "amount": -5}], "more than zero"),
                         ([{"description": "x", "amount": 1}] * 61, "up to 60"), ("nope", "must be a list"),
                         ([{"description": "x", "amount": "abc"}], "isn't an amount")):
            r = admin.patch(f"/api/invoices/{iid}", json={"lines": bad})
            assert r.status_code == 400 and why in r.get_json()["error"], (bad, r.get_json())
        # the one-amount form: refused on several lines, fine on one
        r = admin.patch(f"/api/invoices/{iid}", json={"amount": 500})
        assert r.status_code == 400 and "3 lines" in r.get_json()["error"]
        assert admin.patch(f"/api/invoices/{iid}", json={"amount": 26175, "notes": "office only"}).status_code == 200
        inv2 = invoices.create(conn, {"kind": "deposit", "client_org_id": ids["org"], "amount": 400, "description": "Deposit"})
        invoices.update(conn, inv2["id"], {"lines": [{"description": "Deposit 30%", "amount": 400}]})
        out = invoices.update(conn, inv2["id"], {"amount": 450, "description": "Deposit 35%"})
        assert out["lines"] == [{"description": "Deposit 35%", "amount": 450.0}]
        checks += 1

        # ---- wording
        r = admin.patch(f"/api/invoices/{iid}", json={
            "bill_to_text": "Harbor Owners LLC\nAttn: Accounts Payable\n1 Example Ave, Suite 4", "project_text": "Facade repairs\n9 Quay Street",
            "pay_text": "ACH: ask us for details.", "thanks_text": "Thanks for trusting us with 9 Quay Street.", "doc_notes": "Covers work through Nov 8."})
        assert r.status_code == 200
        pv = admin.get(f"/api/invoices/{iid}/preview").get_data(as_text=True)
        for needle in ("Harbor Owners LLC", "Attn: Accounts Payable", "Facade repairs", "9 Quay Street", "ACH: ask us for details.",
                       "Thanks for trusting us with 9 Quay Street.", "Covers work through Nov 8."):
            assert needle in pv, needle
        assert "office only" not in pv, "the office note never prints"
        assert admin.patch(f"/api/invoices/{iid}", json={"bill_to_text": "", "thanks_text": ""}).status_code == 200
        pv = admin.get(f"/api/invoices/{iid}/preview").get_data(as_text=True)
        assert "InvEdit Harbor Owners" in pv and "We appreciate your business" in pv
        checks += 1

        # ---- Ask Claude (fake model) + Undo
        r = admin.post(f"/api/invoices/{iid}/editor/revise", json={"instruction": "add the extra lintel at $2,600"})
        assert r.status_code == 200, r.get_json()
        out = r.get_json()["data"]
        assert out["changed"] == ["lines"] and out["view"]["invoice"]["amount"] == 28775.0
        assert admin.patch(f"/api/invoices/{iid}", json=out["before"]).status_code == 200
        assert invoices.get(conn, iid)["amount"] == 26175.0 and len(invoices.get(conn, iid)["lines"]) == 3
        assert invoices.get(conn, iid)["bill_to_text"] is None, "Undo keeps blank wording blank"
        assert admin.post(f"/api/invoices/{iid}/editor/revise", json={"instruction": ""}).status_code == 400
        checks += 1

        # ---- issued: can't go below what was paid; the PDF is dropped on change
        invoices.issue(conn, iid)
        invoices.record_payment(conn, iid, amount=20000, method="check")
        conn.execute("UPDATE invoice SET pdf_path = 'data_room/invoices/x.pdf' WHERE id = ?", (iid,)); conn.commit()
        r = admin.patch(f"/api/invoices/{iid}", json={"lines": [{"description": "Less", "amount": 1000}]})
        assert r.status_code == 400 and "already been paid" in r.get_json()["error"]
        r = admin.patch(f"/api/invoices/{iid}", json={"lines": [{"description": "Brick repointing", "amount": 17575},
                                                              {"description": "Lintels", "amount": 8600}]})
        assert r.status_code == 200 and r.get_json()["data"]["amount"] == 26175.0
        row = invoices.get(conn, iid)
        assert row["pdf_path"] is None and row["edited_at"]
        checks += 1

        # ---- modes
        import aia_billing
        real, real_doc = aia_billing.for_invoice, aia_billing.render_document
        aia_billing.for_invoice = lambda c, i: {"id": 1} if i == iid else None
        aia_billing.render_document = lambda c, app, inv: "<html>G702 / G703</html>"
        try:
            v = admin.get(f"/api/invoices/{iid}/editor").get_json()["data"]
            assert v["mode"] == "aia" and v["html"] == "<html>G702 / G703</html>"
            r = admin.patch(f"/api/invoices/{iid}", json={"doc_notes": "x"})
            assert r.status_code == 400 and "progress billing form" in r.get_json()["error"]
            assert admin.post(f"/api/invoices/{iid}/editor/revise", json={"instruction": "add x at $5"}).status_code == 400
        finally:
            aia_billing.for_invoice, aia_billing.render_document = real, real_doc
        conn.execute("UPDATE invoice SET design_html = '<p>A design</p>' WHERE id = ?", (iid,)); conn.commit()
        v = admin.get(f"/api/invoices/{iid}/editor").get_json()["data"]
        assert v["mode"] == "design" and "contenteditable" not in v["html"] and "A design" in v["html"]
        assert admin.delete(f"/api/invoices/{iid}/design").status_code == 200
        assert admin.get(f"/api/invoices/{iid}/editor").get_json()["data"]["mode"] == "edit"
        invoices.record_payment(conn, iid, amount=6175, method="check")
        v = admin.get(f"/api/invoices/{iid}/editor").get_json()["data"]
        assert v["mode"] == "locked" and "contenteditable" not in v["html"] and "Paid in full" in v["html"]
        checks += 1

        # ---- Download saves the file; plain PDF opens inline
        d = invoice_docs._pdf_dir(); d.mkdir(parents=True, exist_ok=True)
        fake = d / "smoke-download.pdf"; fake.write_bytes(b"%PDF-1.4 smoke")
        real_pdf = invoice_docs.pdf_file
        invoice_docs.pdf_file = lambda c, i: fake
        try:
            r = admin.get(f"/api/invoices/{iid}/pdf?download=1")
            assert r.status_code == 200 and "attachment" in r.headers.get("Content-Disposition", "") and inv["number"] in r.headers["Content-Disposition"]
            r = admin.get(f"/api/invoices/{iid}/pdf")
            assert r.status_code == 200 and "inline" in r.headers.get("Content-Disposition", "inline")
        finally:
            invoice_docs.pdf_file = real_pdf
        checks += 1
    finally:
        conn.close()

    # ---- page needles
    job = (ROOT / "dashboard-static.html").read_text(encoding="utf-8")
    for needle in ("data-dl=\"${r.id}\"", "/pdf?download=1", "href=\"/invoices/${r.id}/edit\"", "location.hash === '#payments'",
                   'data-edit="${r.id}"'):
        assert needle in job, needle
    portal = (ROOT / "company_portal.html").read_text(encoding="utf-8")
    assert "Edit invoice" in portal and "/pdf?download=1" in portal and "/invoices/${r.id}/edit" in portal
    ed = (ROOT / "invoice_editor.html").read_text(encoding="utf-8")
    for needle in ("sandbox=\"allow-same-origin\"", "'/invoices/' + ID + '/editor'", "+ Add a line", "Download PDF",
                   "Use the company template", "/editor/revise", 'type="date"', "class=\"btn-primary"):
        assert needle in ed, needle
    checks += 1

    print(f"PASS: invoice editor — gates, one template, lines, wording, Claude + Undo, issued rules, modes, download ({checks} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
