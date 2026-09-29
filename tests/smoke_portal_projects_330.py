#!/usr/bin/env python3
"""#330 — Company Portal Projects tab lists jobs; a job's page shows only that job.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * gates: anon -> 401; pm / client -> 403; unknown code -> 404
  * list: one row per job with client, PM, crew count, contract (schedule of values
    beats the converted estimate), invoiced / open / overdue, last daily report, open
    RFIs; status + search filters
  * detail: identity, client + contacts, PM, crew (names / trades, no paths), job site,
    money (invoice list + summary), AIA summary, permits & insurance package readiness,
    documents expiring with days left, activity counts, links out; nothing from other
    jobs at the same address leaks in; no *_path anywhere
  * served portal: nav "Projects" routes to #projects, the view exists, titles updated
  * #343 — a job is one page: no in-portal summary page; a row opens /projects/<code>
    and #projects/<code> links forward there; the job dashboard's Project Health
    carries the contract/paperwork/crew card for c_suite, and a pm assigned to the
    same job receives none of it (stripped server-side) and still 403s the endpoint
Run:  venv/bin/python tests/smoke_portal_projects_330.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_pp_330_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import buildings  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import invoices  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "pp330"
PW = "PortalProjectsSmoke2026"
ROLES = ("c_suite", "pm", "client")
A, B = "PP330-A", "PP330-B"
TODAY = date.today()


def D(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for role in ROLES:
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), role, f"Smoke {role} Pp330"))
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Pp330 %'")
        ids["org"] = crm.create_org(conn, name="Pp330 Owners", relationship_type="client")
        crm.create_contact(conn, full_name="Pp330 Dana Owner", org_id=ids["org"], email="pp330.dana@example.com")
        for code in (A, B):
            conn.execute("DELETE FROM projects WHERE project_code = ?", (code,))
        # two jobs at the SAME address -> same job site; the page for A must not show B's money
        conn.execute("INSERT INTO projects (project_code, name, address, city_zip, project_type, status, client_org_id, start_date, "
                     "superintendent) VALUES (?, 'Pp330 Facade', '77 Pp330 Ave', 'Bronx, NY 10454', 'facade', 'active', ?, ?, 'Sam Super')",
                     (A, ids["org"], D(-40)))
        conn.execute("INSERT INTO projects (project_code, name, address, city_zip, project_type, status, client_org_id) "
                     "VALUES (?, 'Pp330 Roof', '77 Pp330 Ave', 'Bronx, NY 10454', 'generic', 'closed', ?)", (B, ids["org"]))
        conn.commit()
        for code in (A, B):
            buildings.autolink_project(conn, code)
        pm_id = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.pm@example.com",)).fetchone()["id"]
        conn.execute("DELETE FROM pm_project_assignment WHERE project_code IN (?, ?)", (A, B))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, ?)", (pm_id, A, D(-40)))
        conn.execute("DELETE FROM project_assignments WHERE project_code IN (?, ?)", (A, B))
        # Postgres enforces project_assignments.employee_id -> employees: seed the crew for real
        conn.execute("DELETE FROM employees WHERE employee_id LIKE 'E-9330%'")
        for eid, wid, name in (("E-93301", "W-9331", "Pp330 Mason One"), ("E-93302", "W-9332", "Pp330 Mason Two"),
                               ("E-93303", "W-9333", "Pp330 Gone")):
            conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, pin, created_at, updated_at) "
                         "VALUES (?, ?, ?, 'mason', '9330', ?, ?)", (eid, wid, name, D(0), D(0)))
        for emp in ("E-93301", "E-93302"):
            conn.execute("INSERT INTO project_assignments (project_code, employee_id, role_on_project, status) VALUES (?, ?, 'mason', 'active')", (A, emp))
        conn.execute("INSERT INTO project_assignments (project_code, employee_id, status) VALUES (?, 'E-93303', 'ended')", (A,))
        conn.execute("DELETE FROM estimate WHERE code LIKE 'PP330-%'")
        conn.execute("INSERT INTO estimate (code, est_type, borough, seq, client_org_id, building_address, status, final_amount, "
                     "converted_project_code, created_at, updated_at) VALUES ('PP330-EST-1', 'facade', 'BX', 933001, ?, '77 Pp330 Ave', "
                     "'converted', 150000, ?, ?, ?)", (ids["org"], A, D(-41) + "T09:00:00", D(-41) + "T09:00:00"))
        conn.execute("DELETE FROM project_documents WHERE project_code IN (?, ?)", (A, B))
        conn.execute("INSERT INTO project_documents (project_code, category, requirement_key, title, doc_type, file_path, expiry_date, "
                     "superseded, uploaded_at) VALUES (?, 'CONTRACTS', 'coi', 'Pp330 COI', 'pdf', 'x/y.pdf', ?, 0, ?)", (A, D(12), D(-1)))
        conn.execute("INSERT INTO project_documents (project_code, category, title, doc_type, file_path, expiry_date, superseded, uploaded_at) "
                     "VALUES (?, 'PERMITS', 'Pp330 Old permit', 'pdf', 'x/z.pdf', ?, 0, ?)", (B, D(3), D(-1)))
        conn.execute("DELETE FROM report_index WHERE project_code IN (?, ?)", (A, B))
        for n in (-2, -9, -45):
            conn.execute("INSERT INTO report_index (report_date, project_code, report_type, status) VALUES (?, ?, 'DCR', 'final')", (D(n), A))
        conn.execute("DELETE FROM rfi_log WHERE project_code IN (?, ?)", (A, B))
        conn.execute("INSERT INTO rfi_log (rfi_number, project_code, date_submitted, status, description) VALUES ('PP330-R1', ?, ?, 'open', 'x')", (A, D(-3)))
        conn.execute("INSERT INTO rfi_log (rfi_number, project_code, date_submitted, status, description) VALUES ('PP330-R2', ?, ?, 'closed', 'x')", (A, D(-3)))
        conn.commit()
        inv_a = invoices.create(conn, {"kind": "progress", "client_org_id": ids["org"], "project_code": A, "amount": 20000, "description": "Pp330 progress 1"})
        invoices.issue(conn, inv_a["id"], issued_on=D(-40), due_on=D(-10))     # overdue
        inv_b = invoices.create(conn, {"kind": "final", "client_org_id": ids["org"], "project_code": B, "amount": 5000, "description": "Pp330 roof final"})
        invoices.issue(conn, inv_b["id"], issued_on=D(-5), due_on=D(25))
        ids["inv_a"], ids["inv_b"] = inv_a["id"], inv_b["id"]
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = _db()
    try:
        for t in ("project_assignments", "pm_project_assignment", "project_documents", "report_index", "rfi_log", "billing_sov"):
            try:
                conn.execute(f"DELETE FROM {t} WHERE project_code IN (?, ?)", (A, B))
            except Exception:
                conn.rollback()
        for sql in ("DELETE FROM worker_app_session WHERE employee_id LIKE 'E-9330%'",
                    "DELETE FROM sign_in_log WHERE employee_id LIKE 'E-9330%'",
                    "DELETE FROM project_assignments WHERE employee_id LIKE 'E-9330%'",
                    "DELETE FROM labor_worker_state WHERE employee_id LIKE 'E-9330%'",
                    "DELETE FROM employees WHERE employee_id LIKE 'E-9330%'"):
            try:
                conn.execute(sql)
            except Exception:
                conn.rollback()
        conn.execute("DELETE FROM estimate WHERE code LIKE 'PP330-%'")
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Pp330 %'").fetchall()]
        ph = ",".join("?" * len(org_ids)) or "NULL"
        inv_ids = [r["id"] for r in conn.execute(f"SELECT id FROM invoice WHERE client_org_id IN ({ph})", org_ids).fetchall()]
        purge(conn, "invoice_payment", "invoice_id", inv_ids)
        purge(conn, "invoice", "id", inv_ids)
        conn.execute("DELETE FROM projects WHERE project_code IN (?, ?)", (A, B))
        conn.execute("DELETE FROM building WHERE address_line LIKE '77 Pp330%'")
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


def _no_paths(obj, where=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert not k.endswith("_path") and k != "token_hash", f"{k!r} leaked in {where}"
            _no_paths(v, where)
    elif isinstance(obj, list):
        for v in obj:
            _no_paths(v, where)


def _login(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code)
    return c


API = "/api/company/portal/projects"


def main() -> int:
    ids = _seed()
    passed = 0

    cs = _login("c_suite")
    assert server.app.test_client().get(API).status_code == 401
    assert _login("pm").get(API).status_code == 403 and _login("client").get(f"{API}/{A}").status_code == 403
    assert cs.get(f"{API}/NOPE-330").status_code == 404
    passed += 1; print("  gates ok")

    rows = cs.get(API + "?status=all&search=pp330").get_json()["data"]
    _no_paths(rows, "list")
    by = {r["project_code"]: r for r in rows}
    assert set(by) == {A, B}, set(by)
    a = by[A]
    assert a["client_name"] == "Pp330 Owners" and a["pms"][0]["name"].startswith("Smoke pm") and a["crew_count"] == 2
    assert a["contract"] == {"value": 150000.0, "source": "estimate", "estimate_code": "PP330-EST-1"}
    assert a["invoiced"] == 20000.0 and a["open_value"] == 20000.0 and a["overdue_value"] == 20000.0 and a["collected"] == 0.0
    assert a["last_report_date"] == D(-2) and a["open_rfis"] == 1 and a["status"] == "active"
    assert by[B]["status"] == "closed" and by[B]["overdue_value"] == 0.0 and by[B]["crew_count"] == 0
    active = {r["project_code"] for r in cs.get(API + "?search=pp330").get_json()["data"]}
    assert active == {A}, "default = active jobs only"
    assert {r["project_code"] for r in cs.get(API + "?status=closed&search=pp330").get_json()["data"]} == {B}
    assert {r["project_code"] for r in cs.get(API + "?status=all&search=roof").get_json()["data"]} == {B}
    passed += 1; print("  list ok")

    # the schedule of values wins over the estimate once it exists
    cs.put(f"/api/projects/{A}/billing/sov", json={"rows": [{"item_no": "1", "description": "Pp330 all", "scheduled_value": 160000},
                                                            {"item_no": "CO-1", "description": "Pp330 extra", "scheduled_value": 5000, "kind": "change_order"}]})
    d = cs.get(f"{API}/{A}").get_json()["data"]
    _no_paths(d, "detail")
    assert d["contract"] == {"value": 165000.0, "source": "schedule_of_values", "change_orders": 5000.0}
    p = d["project"]
    assert p["project_code"] == A and p["name"] == "Pp330 Facade" and p["superintendent"] == "Sam Super" and p["start_date"] == D(-40)
    assert d["client"]["name"] == "Pp330 Owners" and d["contacts"][0]["email"] == "pp330.dana@example.com"
    assert d["pms"][0]["name"].startswith("Smoke pm") and len(d["crew"]) == 2 and {c["employee_id"] for c in d["crew"]} == {"E-93301", "E-93302"}
    assert {c["worker_id"] for c in d["crew"]} == {"W-9331", "W-9332"} and all(c["trade"] == "mason" for c in d["crew"])
    assert d["building"] and d["building"]["address_line"].startswith("77 Pp330") and d["links"]["building"] == f"#buildings/{p['building_id']}"
    assert d["links"]["dashboard"] == f"/projects/{A}" and d["links"]["client"] == f"#clients/{ids['org']}"
    m = d["money"]["summary"]
    assert m["invoiced_total"] == 20000.0 and m["open_value"] == 20000.0 and m["overdue_count"] == 1
    assert [i["number"] for i in d["money"]["invoices"]] == [invoices.get(_db(), ids["inv_a"])["number"]], "only THIS job's invoices"
    assert d["aia"]["summary"]["contract_sum_to_date"] == 165000.0 and d["aia"]["applications"] == []
    pk = d["package"]
    assert pk["summary"]["required"] >= 1 and any(i["key"] == "coi" and i["status"] == "expiring" and i["days_left"] == 12 for i in pk["items"]), pk["items"]
    assert [x["title"] for x in d["documents_expiring"]] == ["Pp330 COI"] and d["documents_expiring"][0]["days_left"] == 12, "B's permit must not show"
    act = d["activity"]
    assert act["reports_30d"] == 2 and act["last_report_date"] == D(-2) and act["open_rfis"] == 1 and act["documents"] == 1 and act["photos"] == 0
    passed += 1; print("  detail ok")

    html = cs.get("/company").get_data(as_text=True)
    assert 'data-company-nav' in html and "label: 'Projects'" in (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")   # #339: shared sidebar
    assert 'id="view-projects"' in html and 'id="view-buildings"' not in html   # #339: no job-sites page
    assert "projects: 'Projects'" in html and "buildings: 'Job sites'" not in html
    assert 'href="#buildings" data-view="buildings"' not in html, "job sites are not a nav entry any more"
    # #343 — a job is ONE page: the in-portal summary page is gone, a row opens the
    # dashboard directly, and every old #projects/<code> link forwards there.
    assert 'id="project-detail"' not in html and "loadProjectDetail" not in html and "Open job dashboard" not in html
    assert "location.href = '/projects/' + encodeURIComponent(r.project_code)" in html
    assert "if (view === 'projects' && arg) { location.replace('/projects/' + arg); return; }" in html
    passed += 1; print("  served page ok")

    # #343 put the money summary on the job dashboard, company roles only; #358 replaced
    # that card with the Money card of the executive summary (inside SECTION:financial).
    # c_suite receives it; a pm assigned to the same job receives NONE of it (stripped
    # server-side, not hidden) and the company endpoint still 403s them.
    dash_cs = cs.get(f"/projects/{A}").get_data(as_text=True)
    assert 'id="jx-money"' in dash_cs
    assert 'id="pf"' not in dash_cs and "PROJ_loadFinancialSummary" not in dash_cs, "#358: the old card is gone"
    pm = _login("pm")
    r = pm.get(f"/projects/{A}")
    assert r.status_code == 200, r.status_code
    dash_pm = r.get_data(as_text=True)
    for needle in ('id="jx-money"', "<!-- SECTION:financial:start -->", "SECTION:company_portal", "/api/company/portal/projects/"):
        assert needle not in dash_pm, f"pm must not receive {needle}"
    assert pm.get(f"{API}/{A}").status_code == 403, "the endpoint behind the card stays company-only"
    passed += 1; print("  job dashboard card ok")

    print(f"PASS smoke_portal_projects_330 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
