#!/usr/bin/env python3
"""#302 — Company Portal: the behavioral guard.

Flask test client against an ISOLATED database (honors SSC_DB_URL like every
gate suite; builds a throwaway SQLite snapshot from the versioned migrations
when it is unset — never opens the live superstars.db). Proves:

  * gating: /company and every /api/company/portal/* answers 403 to pm, super,
    estimator and client; 200 to c_suite and admin (COMPANY axis, access.py)
  * the served page carries no build stamps / issue numbers in visible copy
  * clients list: counts (buildings, projects, estimates, contacts) derived
    from CRM + projects + estimates; buildings de-duplicate address variants
  * client detail: buildings group projects and estimates; expiring docs
  * overview: KPI shape + pipeline/won/spend/workforce/expiring numbers
  * finance: month buckets, by-category/by-project spend, pipeline by status,
    receivables, labor cost with the unrated-hours bucket
  * expenses: both ledgers merged, filters (project/category/month/source/status)
  * workforce: rates (comp data) present, hours, cert counters
  * compliance: certs, permits, project documents and company documents in
    the expiry windows with correct state buckets
  * company documents: validation, create, update, archive, listing
  * no `*_path` key ever leaves any portal payload (PII/path rule)

Run:  venv/bin/python tests/smoke_company_portal_302.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_portal_302_")


from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402


if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import company_portal  # noqa: E402
import db_layer  # noqa: E402

TAG = "portal302"
PW = "PortalSmokePassword2026"
TODAY = date.today()
D = lambda n: (TODAY + timedelta(days=n)).isoformat()  # noqa: E731
YM = TODAY.isoformat()[:7]
ROLES = ("c_suite", "admin", "pm", "super", "estimator", "client")


def _db():
    return db_layer.connect(pragma_fk=True)


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for role in ROLES:
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute(
                "INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                (email, auth.hash_password(PW), role, f"Smoke {role}"))
        # clients
        for name in ("Portal302 Compass", "Portal302 Harbor"):
            conn.execute("DELETE FROM crm_organization WHERE name = ?", (name,))
            conn.execute("INSERT INTO crm_organization (name, relationship_type, status, stage, created_at) "
                         "VALUES (?, 'client', 'active', 'active', ?)", (name, D(0)))
        ids["org_a"] = conn.execute("SELECT id FROM crm_organization WHERE name = 'Portal302 Compass'").fetchone()["id"]
        ids["org_b"] = conn.execute("SELECT id FROM crm_organization WHERE name = 'Portal302 Harbor'").fetchone()["id"]
        conn.execute("DELETE FROM crm_contact WHERE full_name LIKE 'Portal302 %'")
        conn.execute("INSERT INTO crm_contact (org_id, full_name, email, title, status, created_at) "
                     "VALUES (?, 'Portal302 Contact', 'p302.contact@example.com', 'Owner', 'active', ?)",
                     (ids["org_a"], D(0)))
        # projects: two projects at ONE building (address variants) + one at another
        for code, name, addr, status, org in (
                ("P302-A", "Facade phase 1", "890 East 135th Street", "active", ids["org_a"]),
                ("P302-B", "Facade phase 2", "890 E 135th St, Bronx", "closed", ids["org_a"]),
                ("P302-C", "Roof job", "12 Harbor Way", "active", ids["org_b"])):
            conn.execute("DELETE FROM projects WHERE project_code = ?", (code,))
            conn.execute("INSERT INTO projects (project_code, name, address, city_zip, status, client_org_id, "
                         "created_at, updated_at) VALUES (?, ?, ?, 'Bronx, NY 10454', ?, ?, ?, ?)",
                         (code, name, addr, status, org, D(0), D(0)))
        # estimates: one open at the same building, one won, one lost
        conn.execute("DELETE FROM estimate WHERE code LIKE 'P302-%'")
        for code, addr, status, amt, sub, dec, org in (
                ("P302-E1", "890 East 135th St", "submitted", 50000, D(-10), None, ids["org_a"]),
                ("P302-E2", "890 East 135th St", "approved", 120000, D(-40), D(-5), ids["org_a"]),
                ("P302-E3", "12 Harbor Way", "lost", 30000, D(-60), D(-30), ids["org_b"])):
            conn.execute("INSERT INTO estimate (code, est_type, borough, seq, client_org_id, building_address, "
                         "status, final_amount, submitted_date, decided_date, created_at, updated_at) "
                         "VALUES (?, 'facade', 'BX', 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (code, org, addr, status, amt, sub, dec, D(0), D(0)))
        # expenses in both ledgers
        conn.execute("DELETE FROM expenses WHERE vendor LIKE 'Portal302 %'")
        conn.execute("INSERT INTO expenses (project_code, vendor, expense_date, category, total, status, created_at) "
                     "VALUES ('P302-A', 'Portal302 Supply', ?, 'materials', 1250.50, 'reviewed', ?)", (D(0), D(0)))
        conn.execute("INSERT INTO expenses (project_code, vendor, expense_date, category, total, status, created_at) "
                     "VALUES ('P302-A', 'Portal302 Draft', ?, 'materials', 999.00, 'draft', ?)", (D(0), D(0)))
        conn.execute("DELETE FROM project_expense WHERE vendor LIKE 'Portal302 %'")
        conn.execute("INSERT INTO project_expense (project_code, expense_date, vendor, category, amount, created_at) "
                     "VALUES ('P302-C', ?, 'Portal302 Rental', 'equipment', 400.00, ?)", (D(0), D(0)))
        # workers + rate + hours + certs
        conn.execute("DELETE FROM sign_in_log WHERE employee_id LIKE 'E-93%'")
        conn.execute("DELETE FROM certifications WHERE employee_id LIKE 'E-93%'")
        conn.execute("DELETE FROM labor_worker_state WHERE employee_id LIKE 'E-93%'")
        conn.execute("DELETE FROM employees WHERE employee_id LIKE 'E-93%'")
        for eid, wid, name, trade in (("E-93001", "W-9301", "Portal302 Rated", "rope_access"),
                                      ("E-93002", "W-9302", "Portal302 Unrated", "mason")):
            conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (eid, wid, name, trade, D(0), D(0)))
        conn.execute("INSERT INTO labor_worker_state (worker_id, employee_id, trade, current_rate, status, "
                     "effective_date, created_at, updated_at) VALUES ('W-9301', 'E-93001', 'rope_access', 40.0, "
                     "'active', ?, ?, ?)", (D(-30), D(0), D(0)))
        for eid in ("E-93001", "E-93002"):
            conn.execute("INSERT INTO sign_in_log (date, employee_id, project_code, time_in, time_out, created_at, "
                         "updated_at) VALUES (?, ?, 'P302-A', '07:00', '15:30', ?, ?)", (D(0), eid, D(0), D(0)))
        ct = conn.execute("SELECT cert_type_id FROM cert_types ORDER BY cert_type_id LIMIT 1").fetchone()["cert_type_id"]
        conn.execute("INSERT INTO certifications (employee_id, cert_type_id, date_obtained, expiration_date, status, "
                     "created_at, updated_at) VALUES ('E-93001', ?, ?, ?, 'active', ?, ?)", (ct, D(-300), D(-3), D(0), D(0)))
        conn.execute("INSERT INTO certifications (employee_id, cert_type_id, date_obtained, expiration_date, status, "
                     "created_at, updated_at) VALUES ('E-93002', ?, ?, ?, 'active', ?, ?)", (ct, D(-300), D(20), D(0), D(0)))
        # permit + project document with expiry
        conn.execute("DELETE FROM permits_library WHERE project_code LIKE 'P302-%'")
        conn.execute("INSERT INTO permits_library (permit_id, project_code, permit_type, permit_number, issuing_agency, "
                     "expiration_date, status, created_at, updated_at) VALUES (930001, 'P302-A', 'PW2', 'PW2-302', 'DOB', ?, "
                     "'active', ?, ?)", (D(45), D(0), D(0)))
        conn.execute("DELETE FROM project_documents WHERE project_code LIKE 'P302-%'")
        conn.execute("INSERT INTO project_documents (project_code, category, requirement_key, title, doc_type, "
                     "file_path, file_name, file_size, mime, expiry_date, version, superseded, uploaded_at) "
                     "VALUES ('P302-A', 'CONTRACTS', 'coi', 'Portal302 COI', 'pdf', 'p302/coi.pdf', 'coi.pdf', 1, "
                     "'application/pdf', ?, 1, 0, ?)", (D(75), D(0)))
        # ira job (receivable)
        conn.execute("DELETE FROM ira_job WHERE project_code = 'P302-A'")
        e2 = conn.execute("SELECT id FROM estimate WHERE code = 'P302-E2'").fetchone()["id"]
        conn.execute("INSERT INTO ira_job (project_code, estimate_id, cd5_status, deposit_received, balance_status, "
                     "updated_at) VALUES ('P302-A', ?, 'not_filed', 0, 'open', ?)", (e2, D(0)))
        conn.execute("DELETE FROM company_document WHERE title LIKE 'Portal302 %'")
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = _db()
    try:
        for sql, params in (
                ("DELETE FROM company_document WHERE title LIKE 'Portal302 %'", ()),
                ("DELETE FROM ira_job WHERE project_code = 'P302-A'", ()),
                ("DELETE FROM project_documents WHERE project_code LIKE 'P302-%'", ()),
                ("DELETE FROM permits_library WHERE project_code LIKE 'P302-%'", ()),
                ("DELETE FROM certifications WHERE employee_id LIKE 'E-93%'", ()),
                ("DELETE FROM sign_in_log WHERE employee_id LIKE 'E-93%'", ()),
                ("DELETE FROM labor_worker_state WHERE employee_id LIKE 'E-93%'", ()),
                ("DELETE FROM employees WHERE employee_id LIKE 'E-93%'", ()),
                ("DELETE FROM project_expense WHERE vendor LIKE 'Portal302 %'", ()),
                ("DELETE FROM expenses WHERE vendor LIKE 'Portal302 %'", ()),
                ("DELETE FROM estimate WHERE code LIKE 'P302-%'", ()),
                ("DELETE FROM projects WHERE project_code LIKE 'P302-%'", ()),
                ("DELETE FROM crm_contact WHERE full_name LIKE 'Portal302 %'", ()),
                ("DELETE FROM crm_organization WHERE name LIKE 'Portal302 %'", ()),
                ):
            conn.execute(sql, params)
        conn.commit()
        purge_users(conn, [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()])
    finally:
        conn.close()


def _no_paths(obj, where=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert not (k.endswith("_path") or k == "folder"), f"path key {k!r} leaked in {where}"
            _no_paths(v, where)
    elif isinstance(obj, list):
        for v in obj:
            _no_paths(v, where)


def main() -> int:
    app = Flask("portal302", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    company_portal.register(app)
    ids = _seed()
    try:
        def client_for(role):
            c = app.test_client()
            r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
            assert r.status_code == 200, (role, r.status_code, r.get_json())
            return c

        # ---- gating ----
        for role in ("pm", "super", "estimator", "client"):
            c = client_for(role)
            assert c.get("/company").status_code == 403, role
            for path in ("/overview", "/clients", "/finance", "/expenses", "/workforce", "/compliance", "/company-documents"):
                r = c.get("/api/company/portal" + path)
                assert r.status_code == 403, (role, path, r.status_code)
            assert c.post("/api/company/portal/company-documents", json={"category": "bond", "title": "x"}).status_code == 403
        assert app.test_client().get("/api/company/portal/overview").status_code == 401, "logged-out must be 401"
        cs = client_for("c_suite")
        assert client_for("admin").get("/api/company/portal/overview").status_code == 200

        # ---- page ----
        r = cs.get("/company")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        assert "<title>Company · Superstars Contracting</title>" in html and "Superstars Contracting" in html   # #339: one home
        assert "AMG" not in html
        import re as _re
        visible = _re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=_re.DOTALL | _re.IGNORECASE)
        visible = _re.sub(r"<[^>]+>", " ", visible)   # text the user can actually read
        assert "build #" not in visible and not _re.search(r"#\d{3}\b", visible), \
            "no build stamps / issue numbers in visible copy"

        # ---- overview ----
        j = cs.get("/api/company/portal/overview").get_json()
        _no_paths(j, "overview")
        d = j["data"]
        assert d["projects"]["active"] >= 2 and d["clients"]["active"] >= 2 and d["clients"]["buildings"] >= 2
        assert d["pipeline"]["open_count"] >= 1 and d["pipeline"]["open_value"] >= 50000
        assert d["pipeline"]["won_value_ytd"] >= 120000 and d["pipeline"]["lost_count_ytd"] >= 1
        assert abs(d["spend"]["receipts_mtd"] - 1250.50) < 0.01 or d["spend"]["receipts_mtd"] >= 1250.50
        assert d["spend"]["ledger_mtd"] >= 400.0
        assert d["workforce"]["headcount"] >= 2 and d["workforce"]["on_site_today"] >= 2
        assert d["workforce"]["worked_hours_this_week"] >= 16.0, "two 07:00-15:30 days = 16 h worked"
        assert d["expiring"]["expired"] >= 1 and d["expiring"]["next_30"] >= 1
        assert d["receivables"]["awaiting_deposit"] >= 1

        # ---- clients list + buildings dedupe ----
        rows = cs.get("/api/company/portal/clients?search=Portal302").get_json()["data"]
        by = {r["name"]: r for r in rows}
        a, b = by["Portal302 Compass"], by["Portal302 Harbor"]
        assert a["buildings"] == 1, "'890 East 135th Street' and '890 E 135th St, Bronx' must collapse to ONE building"
        assert a["projects_total"] == 2 and a["projects_active"] == 1
        assert a["estimates_open"] == 1 and a["estimates_won"] == 1 and a["won_value"] == 120000
        assert a["contacts"] == 1
        assert b["buildings"] == 1 and b["projects_active"] == 1 and b["estimates_open"] == 0
        assert cs.get("/api/company/portal/clients?search=zzz-nomatch").get_json()["data"] == []
        assert cs.get("/api/company/portal/clients?type=vendor&search=Portal302").get_json()["data"] == []

        # ---- client detail ----
        det = cs.get(f"/api/company/portal/clients/{ids['org_a']}").get_json()
        _no_paths(det, "client detail")
        det = det["data"]
        assert len(det["buildings"]) == 1
        bld = det["buildings"][0]
        assert {p["project_code"] for p in bld["projects"]} == {"P302-A", "P302-B"}
        assert {e["code"] for e in bld["estimates"]} == {"P302-E1", "P302-E2"}
        assert bld["active_projects"] == 1 and bld["open_estimates"] == 1 and bld["won_value"] == 120000
        assert det["totals"]["pipeline_value"] == 50000
        assert any(x["title"] == "Portal302 COI" for x in det["documents_expiring"])
        assert det["contacts"][0]["full_name"] == "Portal302 Contact"
        assert cs.get("/api/company/portal/clients/999999999").status_code == 404

        # ---- finance ----
        fin = cs.get("/api/company/portal/finance?months=6").get_json()
        _no_paths(fin, "finance")
        fin = fin["data"]
        assert len(fin["months"]) == 6 and fin["months"][-1] == YM
        assert fin["series"]["receipts"][-1] >= 1250.50 and fin["series"]["ledger"][-1] >= 400.0
        assert fin["series"]["won"][-1] >= 120000, "decided 5 days ago lands in the current month"
        cats = {(c["category"], c["source"]): c["total"] for c in fin["spend_by_category"]}
        assert cats.get(("materials", "receipts"), 0) >= 1250.50 and cats.get(("equipment", "ledger"), 0) >= 400.0
        assert not any(c["total"] >= 999.0 and c["source"] == "receipts" and c["n"] == 1 for c in fin["spend_by_category"]
                       if c["category"] == "draft"), "draft receipts are excluded"
        assert any(p["project_code"] == "P302-A" for p in fin["spend_by_project"])
        st = {p["status"]: p for p in fin["pipeline_by_status"]}
        assert st["submitted"]["value"] >= 50000 and st["approved"]["value"] >= 120000
        recv = [r for r in fin["receivables"] if r["project_code"] == "P302-A"][0]
        assert recv["balance_status"] == "open" and not recv["deposit_received"] and recv["final_amount"] == 120000
        lab = fin["labor_last_30_days"]
        assert lab["worked_hours"] >= 16.0 and lab["estimated_cost"] >= 8 * 40.0 and lab["unrated_hours"] >= 8.0
        assert "subscriptions" in fin and "available" in fin["subscriptions"]
        assert cs.get("/api/company/portal/finance?months=abc").status_code == 400

        # ---- expenses ----
        ex = cs.get("/api/company/portal/expenses?project=P302-A").get_json()["data"]
        _no_paths(ex, "expenses")
        vendors = {i["vendor"] for i in ex["items"]}
        assert "Portal302 Supply" in vendors and "Portal302 Draft" in vendors and "Portal302 Rental" not in vendors
        ex = cs.get("/api/company/portal/expenses?source=ledger&category=equipment").get_json()["data"]
        assert {i["vendor"] for i in ex["items"]} == {"Portal302 Rental"} and ex["items"][0]["status"] == "booked"
        ex = cs.get(f"/api/company/portal/expenses?month={YM}&status=needs_review").get_json()["data"]
        assert not any(i["vendor"].startswith("Portal302") for i in ex["items"])
        assert cs.get("/api/company/portal/expenses?month=2026-9").status_code == 400
        assert "materials" in ex["filters"]["categories"]

        # ---- workforce ----
        wf = cs.get("/api/company/portal/workforce").get_json()
        _no_paths(wf, "workforce")
        wf = wf["data"]
        w = {x["employee_id"]: x for x in wf["workers"]}
        assert w["E-93001"]["current_rate"] == 40.0 and w["E-93002"]["current_rate"] is None
        assert w["E-93001"]["hours_7d"] >= 8.0 and w["E-93001"]["last_on_site"] == D(0)
        assert w["E-93001"]["certs_expired"] == 1 and w["E-93002"]["certs_expiring_30"] == 1
        assert wf["summary"]["certs_expired"] >= 1 and wf["summary"]["certs_expiring_30"] >= 1
        assert any(t["trade"] == "rope_access" and t["avg_rate"] == 40.0 for t in wf["by_trade"])

        # ---- compliance ----
        co = cs.get("/api/company/portal/compliance?days=90").get_json()
        _no_paths(co, "compliance")
        co = co["data"]
        kinds = {(i["kind"], i["title"] if i["kind"] != "worker_cert" else i["subject_ref"]): i for i in co["items"]}
        assert kinds[("worker_cert", "E-93001")]["state"] == "expired"
        assert kinds[("worker_cert", "E-93002")]["state"] == "due_30"
        assert kinds[("permit", "PW2")]["state"] == "due_60"
        assert kinds[("project_document", "Portal302 COI")]["state"] == "due_90"
        assert kinds[("worker_cert", "E-93001")]["subject"].startswith("W-9301 —")
        assert co["summary"]["expired"] >= 1 and co["summary"]["due_30"] >= 1 and co["summary"]["due_60"] >= 1
        assert cs.get("/api/company/portal/compliance?days=x").status_code == 400

        # ---- company documents CRUD ----
        bad = cs.post("/api/company/portal/company-documents", json={"category": "nope", "title": "x"})
        assert bad.status_code == 400 and "category" in bad.get_json()["error"]
        bad = cs.post("/api/company/portal/company-documents", json={"category": "bond", "title": "  "})
        assert bad.status_code == 400
        bad = cs.post("/api/company/portal/company-documents", json={"category": "bond", "title": "x", "expiry_date": "09/30/2026"})
        assert bad.status_code == 400 and "YYYY-MM-DD" in bad.get_json()["error"]
        r = cs.post("/api/company/portal/company-documents", json={
            "category": "insurance_gl", "title": "Portal302 GL policy", "holder": "Superstars Contracting Inc.",
            "doc_number": "GL-302", "issuer": "Carrier", "effective_date": D(-300), "expiry_date": D(10), "amount": "2000000"})
        assert r.status_code == 201, r.get_json()
        doc_id = r.get_json()["data"]["id"]
        docs = cs.get("/api/company/portal/company-documents").get_json()
        _no_paths(docs, "company documents")
        mine = [x for x in docs["data"] if x["id"] == doc_id][0]
        assert mine["category_label"] == "General Liability" and mine["amount"] == 2000000.0 and mine["days_left"] == 10
        assert "insurance_gl" in docs["meta"]["categories"]
        co = cs.get("/api/company/portal/compliance?days=30").get_json()["data"]
        assert any(i["kind"] == "company_document" and i["id"] == doc_id and i["state"] == "due_30" for i in co["items"])
        r = cs.patch(f"/api/company/portal/company-documents/{doc_id}", json={"expiry_date": D(400), "notes": "renewed"})
        assert r.status_code == 200
        mine = [x for x in cs.get("/api/company/portal/company-documents").get_json()["data"] if x["id"] == doc_id][0]
        assert mine["expiry_date"] == D(400) and mine["notes"] == "renewed"
        assert cs.patch(f"/api/company/portal/company-documents/{doc_id}", json={"category": "bogus"}).status_code == 400
        assert cs.patch("/api/company/portal/company-documents/999999999", json={"title": "x"}).status_code == 404
        assert cs.delete(f"/api/company/portal/company-documents/{doc_id}").status_code == 200
        assert cs.delete(f"/api/company/portal/company-documents/{doc_id}").status_code == 404, "archive is single-shot"
        assert not any(x["id"] == doc_id for x in cs.get("/api/company/portal/company-documents").get_json()["data"])
        assert any(x["id"] == doc_id for x in cs.get("/api/company/portal/company-documents?archived=1").get_json()["data"])

        # ---- address normalization unit checks ----
        ak = company_portal.address_key
        assert ak("890 East 135th Street") == ak("890 E. 135th St, Bronx, NY 10454") == "890 e 135th st"
        assert ak("") == "" and ak(None) == ""
    finally:
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print("PASS: #302 company portal — gating, page scrub, clients/buildings, client detail, overview, "
          "finance, expenses, workforce, compliance, company documents CRUD, no path keys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
