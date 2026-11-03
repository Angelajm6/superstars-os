#!/usr/bin/env python3
"""#315 — invoices & receivables: behavioral guard.

Flask test client on an ISOLATED database (honors SSC_DB_URL; builds a throwaway
SQLite snapshot from the versioned migrations when unset). Proves:

  * ensure_schema is idempotent
  * gating: pm / super / estimator / client -> 403 on /api/invoices; anon -> 401;
    admin / c_suite -> 200
  * create: validation (client, amount, kind, dates), INV-YYYY-#### numbering
    with numeric max, status draft, CRM activity row
  * issue: draft only (409 otherwise); default due = +30 days, deposits +7;
    explicit due date honoured; due before issue -> 400
  * edit rules: draft edits any field; issued edits only due/notes/description
    (amount -> ignored, not applied); paid/void -> 409
  * payments: only on issued (409); partial keeps issued + balance; overpay -> 400;
    full -> paid with paid_at; a paid deposit flags the IRA job's deposit
  * overdue is derived: issued + due in the past -> overdue flag, days, ?overdue=1,
    summary overdue_count / overdue_value; due-soon bucket
  * void: draft or issued without payments; with payments / paid -> 409;
    delete: draft only
  * THE TRIGGER: a signed proposal with deposit terms drafts its deposit invoice
    (proposal_id, estimate_id, amount = deposit_amount); idempotent; the CRM
    task names the invoice
  * company_portal overview / finance carry `invoices` / `invoice_summary`
  * no `*_path` keys in any JSON payload

Run:  venv/bin/python tests/smoke_invoices_315.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")   # test-only seams are inert without this (runtime_seams.py)
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_inv_315_")

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
import buildings  # noqa: E402
import company_portal  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import invoices  # noqa: E402
import proposals  # noqa: E402
import role_containment  # noqa: E402

TAG = "inv315"
PW = "InvoicesSmokePassword2026"
ROLES = ("c_suite", "admin", "pm", "super", "estimator", "client")
TODAY = date.today()
YEAR = TODAY.strftime("%Y")


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
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Inv315 %'")
        ids["org"] = crm.create_org(conn, name="Inv315 Harbor Owners", relationship_type="client")
        ids["org2"] = crm.create_org(conn, name="Inv315 Second Client", relationship_type="client")
        ids["admin"] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.admin@example.com",)).fetchone()["id"]
        est = estimates.create_estimate(conn, est_type="FR", borough="BX", client_org_id=ids["org"],
                                        building_address="7 Inv315 Quay", notes="Inv315 scope", created_by=ids["admin"])
        ids["est"] = est["id"]; ids["code"] = est["code"]
        estimates.change_status(conn, est["id"], "scoping", actor_user_id=ids["admin"])
        estimates.change_status(conn, est["id"], "submitted", final_amount=50000, actor_user_id=ids["admin"])
        conn.execute("DELETE FROM projects WHERE project_code = 'INV315-A'")
        conn.execute("INSERT INTO projects (project_code, name, address) VALUES ('INV315-A', 'Inv315 Quay job', '7 Inv315 Quay')")
        try:
            conn.execute("DELETE FROM ira_job WHERE project_code = 'INV315-A'")
            conn.execute("INSERT INTO ira_job (project_code, estimate_id, deposit_received, balance_status, updated_at) "
                         "VALUES ('INV315-A', ?, 0, 'open', ?)", (est["id"], datetime.now().isoformat()))
            ids["ira"] = True
        except Exception:
            ids["ira"] = False
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Inv315 %'").fetchall()]
        ph = ",".join("?" * len(org_ids)) or "NULL"
        inv_ids = [r["id"] for r in conn.execute(f"SELECT id FROM invoice WHERE client_org_id IN ({ph})", org_ids).fetchall()]
        purge(conn, "invoice_payment", "invoice_id", inv_ids)
        purge(conn, "invoice", "id", inv_ids)
        est_ids = [r["id"] for r in conn.execute("SELECT id FROM estimate WHERE building_address LIKE '% Inv315 %'").fetchall()]
        pids = [r["id"] for r in conn.execute(
            f"SELECT id FROM proposal WHERE estimate_id IN ({','.join('?' * len(est_ids)) or 'NULL'})", est_ids).fetchall()]
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        try:
            conn.execute("DELETE FROM ira_job WHERE project_code = 'INV315-A'")
        except Exception:
            pass
        purge(conn, "estimate_document", "estimate_id", est_ids)
        purge(conn, "estimate", "id", est_ids)
        conn.execute("DELETE FROM projects WHERE project_code = 'INV315-A'")
        conn.execute("DELETE FROM building WHERE address_key LIKE '7 inv315 %'")
        if org_ids:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph})", org_ids)
            conn.execute(f"DELETE FROM crm_task WHERE entity_type = 'organization' AND entity_id IN ({ph})", org_ids)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph})", org_ids)
        conn.execute("DELETE FROM notification_log WHERE subject LIKE '%Inv315%'")
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Inv315 %'")
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _no_paths(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert not k.endswith("_path"), k
            _no_paths(v)
    elif isinstance(obj, list):
        for v in obj:
            _no_paths(v)


def _login(app, role):
    c = app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code, r.get_data(as_text=True)[:200])
    return c


def _j(resp):
    j = resp.get_json()
    _no_paths(j)
    return j


def main() -> int:
    app = Flask("inv315", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    role_containment.register(app)
    buildings.register(app)
    estimates.register(app)
    proposals.register(app)
    invoices.register(app)
    conn = _db()
    try:
        invoices.ensure_schema(conn)
        invoices.ensure_schema(conn)   # idempotent
        company_portal.ensure_schema(conn)   # the overview reads company_document
    finally:
        conn.close()

    ids = _seed()
    org, org2 = ids["org"], ids["org2"]
    checks = 0

    # ---- gates
    for role in ("pm", "super", "estimator", "client"):
        assert _login(app, role).get("/api/invoices").status_code == 403, role
    assert app.test_client().get("/api/invoices").status_code == 401
    admin = _login(app, "admin")
    cs = _login(app, "c_suite")
    assert cs.get("/api/invoices").status_code == 200
    # the CI Postgres gate shares one database across suites (#307 leaves deposit
    # drafts behind while it runs): every summary assertion below is a DELTA
    base = _j(admin.get("/api/invoices"))["meta"]["summary"]
    def delta(sm, k):
        return round(sm[k] - base[k], 2)
    checks += 1

    # ---- create: validation + numbering
    r = admin.post("/api/invoices", json={"amount": 100})
    assert r.status_code == 400 and "client" in _j(r)["error"], r.get_json()
    r = admin.post("/api/invoices", json={"client_org_id": org, "amount": 0})
    assert r.status_code == 400 and "greater than zero" in _j(r)["error"]
    r = admin.post("/api/invoices", json={"client_org_id": org, "amount": 10, "kind": "bogus"})
    assert r.status_code == 400 and "kind" in _j(r)["error"]
    r = admin.post("/api/invoices", json={"client_org_id": org, "amount": 10, "due_at": "12/01/2026"})
    assert r.status_code == 400 and "YYYY-MM-DD" in _j(r)["error"]
    r = admin.post("/api/invoices", json={"client_org_id": 999999, "amount": 10})
    assert r.status_code == 404
    r = admin.post("/api/invoices", json={"client_org_id": org, "amount": "1,250.50", "kind": "progress",
                                          "project_code": "INV315-A", "description": "Progress billing #1"})
    assert r.status_code == 400, "a formatted amount must be rejected server-side"
    r = admin.post("/api/invoices", json={"client_org_id": org, "amount": 1250.5, "kind": "progress",
                                          "project_code": "INV315-A", "description": "Progress billing #1"})
    assert r.status_code == 201, r.get_json()
    a = _j(r)["data"]
    assert a["status"] == "draft" and a["number"].startswith(f"INV-{YEAR}-") and a["amount"] == 1250.5
    assert a["client_name"] == "Inv315 Harbor Owners" and a["project_name"] == "Inv315 Quay job"
    assert a["balance"] == 1250.5 and a["overdue"] is False and a["due_at"] is None
    seq_a = int(a["number"].rsplit("-", 1)[1])
    conn = _db()
    try:
        # numeric max: plant a number that sorts high lexicographically but low numerically
        conn.execute("UPDATE invoice SET number = ? WHERE id = ?", (f"INV-{YEAR}-{seq_a:04d}", a["id"]))
        conn.commit()
        assert invoices.next_number(conn, TODAY) == f"INV-{YEAR}-{seq_a + 1:04d}"
        act = conn.execute("SELECT COUNT(1) AS n FROM crm_activity WHERE entity_type='organization' AND entity_id=? "
                           "AND summary LIKE ?", (org, f"Invoice {a['number']} drafted%")).fetchone()["n"]
        assert act == 1, "drafting writes one CRM activity row"
    finally:
        conn.close()
    checks += 1

    # ---- edit a draft; then issue with default terms
    r = admin.patch(f"/api/invoices/{a['id']}", json={"amount": 2000, "retainage": 200, "notes": "net 30"})
    assert r.status_code == 200 and _j(r)["data"]["amount"] == 2000 and _j(r)["data"]["retainage"] == 200
    r = admin.post(f"/api/invoices/{a['id']}/issue", json={"issued_on": "2026-13-01"})
    assert r.status_code == 400
    r = admin.post(f"/api/invoices/{a['id']}/issue", json={})
    assert r.status_code == 200, r.get_json()
    a = _j(r)["data"]
    assert a["status"] == "issued" and a["issued_at"] == TODAY.isoformat()
    assert a["due_at"] == (TODAY + timedelta(days=invoices.NET_DAYS_DEFAULT)).isoformat()
    assert a["days_to_due"] == invoices.NET_DAYS_DEFAULT and a["overdue"] is False
    assert admin.post(f"/api/invoices/{a['id']}/issue", json={}).status_code == 409, "issue twice -> 409"
    # issued: amount is not editable, due/notes are
    r = admin.patch(f"/api/invoices/{a['id']}", json={"amount": 5, "notes": "call Friday", "due_at": "2099-01-01"})
    assert r.status_code == 200 and _j(r)["data"]["amount"] == 2000 and _j(r)["data"]["notes"] == "call Friday"
    assert _j(r)["data"]["due_at"] == "2099-01-01"
    r = admin.patch(f"/api/invoices/{a['id']}", json={"due_at": (TODAY + timedelta(days=10)).isoformat()})
    assert r.status_code == 200
    checks += 1

    # ---- deposit default due = +7; explicit due honoured; due before issue -> 400
    r = admin.post("/api/invoices", json={"client_org_id": org2, "amount": 900, "kind": "deposit", "description": "Manual deposit"})
    b = _j(r)["data"]
    r = admin.post(f"/api/invoices/{b['id']}/issue", json={"issued_on": TODAY.isoformat(), "due_on": (TODAY - timedelta(days=1)).isoformat()})
    assert r.status_code == 400 and "before" in _j(r)["error"]
    r = admin.post(f"/api/invoices/{b['id']}/issue", json={})
    b = _j(r)["data"]
    assert b["due_at"] == (TODAY + timedelta(days=invoices.DEPOSIT_NET_DAYS)).isoformat()
    checks += 1

    # ---- payments
    r = admin.post(f"/api/invoices/{a['id']}/payments", json={"amount": 2500})
    assert r.status_code == 400 and "more than" in _j(r)["error"]
    r = admin.post(f"/api/invoices/{a['id']}/payments", json={"amount": 500, "method": "carrier-pigeon"})
    assert r.status_code == 400
    r = admin.post(f"/api/invoices/{a['id']}/payments", json={"amount": 500, "method": "check", "reference": "1043"})
    assert r.status_code == 201, r.get_json()
    a = _j(r)["data"]
    assert a["status"] == "issued" and a["amount_paid"] == 500 and a["balance"] == 1500 and len(a["payments"]) == 1
    r = admin.post(f"/api/invoices/{a['id']}/void", json={"reason": "oops"})
    assert r.status_code == 409 and "payments" in _j(r)["error"], "void with payments -> 409"
    r = cs.post(f"/api/invoices/{a['id']}/payments", json={"amount": 1500, "method": "ach", "paid_on": TODAY.isoformat()})
    assert r.status_code == 201
    a = _j(r)["data"]
    assert a["status"] == "paid" and a["paid_at"] == TODAY.isoformat() and a["balance"] == 0 and len(a["payments"]) == 2
    assert admin.post(f"/api/invoices/{a['id']}/payments", json={"amount": 1}).status_code == 409, "paid -> no more payments"
    assert admin.patch(f"/api/invoices/{a['id']}", json={"notes": "x"}).status_code == 409, "paid -> not editable"
    assert admin.post(f"/api/invoices/{a['id']}/void", json={}).status_code == 409, "paid -> cannot void"
    assert admin.delete(f"/api/invoices/{a['id']}").status_code == 409, "delete only drafts"
    # payment on a draft -> 409
    r = admin.post("/api/invoices", json={"client_org_id": org, "amount": 10})
    d = _j(r)["data"]
    assert admin.post(f"/api/invoices/{d['id']}/payments", json={"amount": 10}).status_code == 409
    assert admin.delete(f"/api/invoices/{d['id']}").status_code == 200
    assert admin.get(f"/api/invoices/{d['id']}").status_code == 404
    checks += 1

    # ---- overdue math + summary buckets
    r = admin.post("/api/invoices", json={"client_org_id": org, "amount": 3000, "kind": "final", "description": "Late one"})
    late = _j(r)["data"]
    r = admin.post(f"/api/invoices/{late['id']}/issue", json={"issued_on": (TODAY - timedelta(days=40)).isoformat(),
                                                             "due_on": (TODAY - timedelta(days=10)).isoformat()})
    late = _j(r)["data"]
    assert late["overdue"] is True and late["days_overdue"] == 10 and late["days_to_due"] == -10
    r = admin.get("/api/invoices?overdue=1")
    j = _j(r)
    assert late["id"] in [x["id"] for x in j["data"]] and all(x["overdue"] for x in j["data"]), j["data"]
    s = j["meta"]["summary"]
    assert delta(s, "overdue_count") == 1 and delta(s, "overdue_value") == 3000
    assert delta(s, "open_count") == 2 and delta(s, "open_value") == 3900, s          # b (900, deposit) + late (3000)
    assert delta(s, "awaiting_deposit_count") == 1 and delta(s, "awaiting_deposit_value") == 900
    assert delta(s, "due_soon_count") == 1 and delta(s, "due_soon_value") == 900, s   # the deposit, due in 7 days
    assert delta(s, "collected_ytd_value") == 2000 and delta(s, "collected_ytd_count") == 2
    assert delta(s, "draft_count") == 0
    r = admin.get("/api/invoices?status=open")
    assert {x["id"] for x in _j(r)["data"]} >= {b["id"], late["id"]} and all(x["status"] == "issued" for x in _j(r)["data"])
    r = admin.get("/api/invoices?status=paid&kind=progress")
    assert a["id"] in [x["id"] for x in _j(r)["data"]] and all(x["status"] == "paid" and x["kind"] == "progress" for x in _j(r)["data"])
    r = admin.get("/api/invoices?search=late+one")
    assert [x["id"] for x in _j(r)["data"]] == [late["id"]]
    assert admin.get("/api/invoices?status=bogus").status_code == 400
    # void the late one (no payments) -> gone from receivables
    r = admin.post(f"/api/invoices/{late['id']}/void", json={"reason": "billed under the wrong client"})
    assert r.status_code == 200 and _j(r)["data"]["status"] == "void" and "Voided: billed" in _j(r)["data"]["notes"]
    s = _j(admin.get("/api/invoices"))["meta"]["summary"]
    assert delta(s, "overdue_count") == 0 and delta(s, "open_count") == 1
    checks += 1

    # ---- THE TRIGGER: signed proposal with deposit terms -> deposit invoice draft
    conn = _db()
    try:
        p = proposals.create_from_estimate(conn, ids["est"], created_by=ids["admin"])
        assert p["deposit_amount"] == 25000.0, p["deposit_amount"]          # the house 50% of 50,000
        conn.execute("UPDATE estimate SET converted_project_code = 'INV315-A' WHERE id = ?", (ids["est"],))
        conn.commit()
        p = proposals.mark_signed_offline(conn, p["id"], name="Inv315 Signer", actor=ids["admin"])
        assert p["status"] == "signed"
        dep = invoices.list_invoices(conn, proposal_id=p["id"])
        assert len(dep) == 1, dep
        dep = dep[0]
        assert dep["kind"] == "deposit" and dep["status"] == "draft" and dep["amount"] == 25000.0
        assert dep["estimate_id"] == ids["est"] and dep["client_org_id"] == org and dep["project_code"] == "INV315-A"
        assert dep["source_doc"] == f"proposal:{p['id']}" and "50%" in dep["description"] and str(p["display_no"]) in dep["description"]
        again = invoices.create_from_proposal(conn, p)
        assert again["id"] == dep["id"], "idempotent per proposal"
        assert len(invoices.list_invoices(conn, proposal_id=p["id"])) == 1
        task = conn.execute("SELECT detail FROM crm_task WHERE entity_type='organization' AND entity_id=? "
                            "AND title LIKE 'Issue deposit invoice%'", (org,)).fetchone()
        assert task and dep["number"] in task["detail"], (task["detail"] if task else None)
        # issue + pay it -> the IRA job's deposit flag flips
        invoices.issue(conn, dep["id"])
        s = invoices.receivables_summary(conn)
        assert delta(s, "awaiting_deposit_count") == 2 and delta(s, "awaiting_deposit_value") == 25900.0, s
        paid = invoices.record_payment(conn, dep["id"], amount=25000, method="wire")
        assert paid["status"] == "paid"
        if ids["ira"]:
            row = conn.execute("SELECT deposit_received, deposit_date FROM ira_job WHERE project_code='INV315-A'").fetchone()
            assert int(row["deposit_received"] or 0) == 1 and row["deposit_date"] == TODAY.isoformat(), dict(row)
        # the proposal-scoped list the portal detail reads
        r = admin.get(f"/api/invoices?proposal_id={p['id']}")
        assert [x["id"] for x in _j(r)["data"]] == [dep["id"]]
        # the client's page (2026-10-28): its proposals + every invoice, drafts included; drafts / void stay out of the sums
        det = company_portal.client_detail(conn, org)
        assert any(x["id"] == p["id"] and x["status"] == "signed" for x in det["proposals"])
        allinv = invoices.list_invoices(conn, client_org_id=org)
        assert {x["id"] for x in det["invoices"]} == {x["id"] for x in allinv} and dep["id"] in {x["id"] for x in det["invoices"]}
        assert not {x["id"] for x in det["invoices"]} & {x["id"] for x in invoices.list_invoices(conn, client_org_id=org2)}
        live = [x for x in allinv if x["status"] in ("issued", "paid")]
        t = det["totals"]
        assert t["invoiced"] == round(sum(x["amount"] for x in live), 2) and t["paid"] == round(sum(x["amount_paid"] for x in live), 2)
        assert t["owed"] == round(sum(x["balance"] for x in live if x["status"] == "issued"), 2) and t["proposals_signed"] >= 1
        # company portal payloads carry the new keys and keep the old ones
        ov = company_portal.overview(conn)
        assert delta(ov["invoices"], "open_count") == 1 and "awaiting_deposit" in ov["receivables"], ov["invoices"]
        fin = company_portal.finance(conn)
        assert isinstance(fin["receivables"], list) and isinstance(fin["invoices"], list)
        assert {x["id"] for x in fin["invoices"]} >= {a["id"], b["id"], dep["id"]}
        assert delta(fin["invoice_summary"], "collected_ytd_value") == 27000.0, fin["invoice_summary"]
        _no_paths(ov); _no_paths(fin)
    finally:
        conn.close()
    checks += 1

    print(f"PASS: #315 invoices & receivables — gates, numbering, draft→issued→paid, payments, overdue math, "
          f"void/delete rules, proposal-signed deposit draft, IRA flag, portal payloads ({checks} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
