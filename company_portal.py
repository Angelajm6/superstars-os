"""#302 — Company Portal: the C-suite's single view of the whole company.

One page (``/company``) + JSON endpoints that ROLL UP data the app already
holds into the shape an owner needs: clients and the buildings each one has
work on, the money (pipeline, won, spend, receivables, labor cost), the
workforce with pay rates, and everything that expires (worker certs, permits,
project documents, company licenses and insurance).

Access: ``requires_section('company_portal')`` = admin / c_suite only (the
COMPANY axis, see access.py). Pay rates and amounts are comp-class data and
are allowed here precisely because this surface is company-only and never
field-reachable (CLAUDE.md comp-data rule).

Data sources (read-only rollups, no new copies of anything):
  * clients .......... crm_organization (relationship_type='client') + crm_contact
  * buildings ........ the `building` table (#304), one row per normalized address,
                       linked to a client (there is no buildings table yet — #303
                       will add one and this module will prefer it when present)
  * pipeline / won ... estimate (status, final_amount, submitted/decided dates)
  * spend ............ expenses (scanned receipts, #272) + project_expense (#278
                       ledger) — reported SEPARATELY, they can overlap
  * receivables ...... ira_job (deposit_received, balance_status, qb_invoice_ref)
  * labor cost ....... sign_in_log hours (payroll_hours.compute_worked_hours,
                       WORKED hours) x labor_worker_state.current_rate
  * expiring ......... certifications, permits_library, project_documents,
                       estimate_document, company_document (NEW, this module)

Portability: every query runs unchanged on SQLite and Postgres (db_layer
translates '?'); dates are ISO strings compared as strings; month buckets use
substr(date, 1, 7); no dialect-only functions.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

from flask import jsonify, request, send_file

import db_layer
from auth import SCRIPT_DIR, current_user, requires_section
import invoices
from payroll_hours import compute_worked_hours

log = logging.getLogger(__name__)

PAGE = SCRIPT_DIR / "company_portal.html"
SECTION = "company_portal"

PIPELINE_STATUSES = ("intake", "scoping", "submitted")
WON_STATUSES = ("approved", "converted")

# Company-level documents (licenses, insurance, bonds, registrations). Distinct
# from project_documents (per-project COI/permits) and certifications (per-worker).
COMPANY_DOC_CATEGORIES = {
    "insurance_gl": "General Liability",
    "insurance_wc": "Workers' Compensation",
    "insurance_umbrella": "Umbrella / Excess",
    "insurance_auto": "Commercial Auto",
    "insurance_other": "Other Insurance",
    "license_gc": "General Contractor License",
    "license_hic": "Home Improvement License",
    "license_rigging": "Rigging License",
    "license_safety": "Safety Registration (SST / SSM)",
    "license_other": "Other License",
    "bond": "Bond",
    "registration": "Registration / Filing",
    "other": "Other",
    # #318 — the company vault: vehicles, tax, filings, generic records
    "vehicle_registration": "Vehicle Registration",
    "vehicle_insurance": "Vehicle Insurance",
    "vehicle_inspection": "Vehicle Inspection",
    "vehicle_title": "Vehicle Title",
    "vehicle_other": "Vehicle — Other",
    "tax_return_federal": "Tax Return — Federal",
    "tax_return_state": "Tax Return — State / City",
    "tax_filing_receipt": "Tax Filing Receipt",
    "payroll_filing": "Payroll Filing (941 / NYS-45)",
    "company_record": "Company Record",
}
# #318 — which vault shelf a category sits on (the portal's group chips)
COMPANY_DOC_GROUPS = {
    "insurance": "Insurance & licenses", "vehicles": "Vehicles", "tax": "Tax & filings", "records": "Records",
}
def doc_group(category: str) -> str:
    if category.startswith("vehicle_"):
        return "vehicles"
    if category.startswith(("tax_", "payroll_")):
        return "tax"
    if category in ("company_record", "other"):
        return "records"
    return "insurance"
VEHICLE_DOC_CATEGORIES = ("vehicle_registration", "vehicle_insurance", "vehicle_inspection")   # the three every vehicle needs
COMPANY_DOC_EDITABLE = ("category", "title", "holder", "doc_number", "issuer",
                       "effective_date", "expiry_date", "amount", "notes", "vehicle_id", "doc_year")


# ============================ helpers ============================

def _conn():
    return db_layer.connect()


def _rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]


def _one(cur) -> Optional[dict]:
    r = cur.fetchone()
    return dict(r) if r else None


def _scalar(conn, sql: str, params=()) -> float:
    r = conn.execute(sql, params).fetchone()
    if not r:
        return 0
    v = r[0] if not hasattr(r, "keys") else r[list(r.keys())[0]]
    return v or 0


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> date:
    return date.today()


def _wrap(data, **meta):
    m = {"generated_at": _now_iso()}
    if isinstance(data, list):
        m["count"] = len(data)
    m.update(meta)
    return jsonify({"data": data, "meta": m})


def _err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _days_until(iso: Optional[str], today: Optional[date] = None) -> Optional[int]:
    if not iso:
        return None
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return None
    return (d - (today or _today())).days


from buildings import address_key  # #304 — one matcher, owned by the buildings module


def _valid_date(v) -> bool:
    if v in (None, ""):
        return True
    try:
        date.fromisoformat(str(v))
        return True
    except ValueError:
        return False


# ============================ schema ============================

def ensure_schema(conn) -> None:
    """Idempotent. company_document = company-level licenses / insurance / bonds."""
    if db_layer.is_postgres():
        pk = "id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
    else:
        pk = "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS company_document ("
        f"  {pk},"
        f"  category       TEXT NOT NULL,"
        f"  title          TEXT NOT NULL,"
        f"  holder         TEXT,"
        f"  doc_number     TEXT,"
        f"  issuer         TEXT,"
        f"  effective_date TEXT,"
        f"  expiry_date    TEXT,"
        f"  amount         REAL,"
        f"  notes          TEXT,"
        f"  created_by     INTEGER,"
        f"  created_at     TEXT NOT NULL,"
        f"  updated_at     TEXT NOT NULL,"
        f"  archived_at    TEXT"
        f")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_document_expiry "
                 "ON company_document(archived_at, expiry_date)")
    conn.commit()
    # #318 — the vault: vehicles, per-document files, and the two link columns
    import company_vault
    company_vault.ensure_schema(conn)


# ============================ buildings (#304: the table) ============================

def buildings_by_client(conn) -> dict:
    """{org_id: [building, ...]} from the `building` table (#304). Each building
    carries its projects and estimates. Projects / estimates that have a client
    but no building yet (no address) are grouped under one "No address on file"
    bucket so nothing disappears from the client view."""
    import buildings as _b
    out: dict = {}
    rows = _b.list_buildings(conn, include_archived=False)
    ids = [r["id"] for r in rows]
    pm, em = _b._projects_for(conn, ids), _b._estimates_for(conn, ids)
    for r in rows:
        if not r.get("client_org_id"):
            continue
        b = {"id": r["id"], "key": r["address_key"], "address": r["address_line"], "name": r.get("name"),
             "borough": r.get("borough"), "bin": r.get("bin"), "stories": r.get("stories"),
             "projects": [{"project_code": p["project_code"], "name": p["name"],
                           "status": p.get("status") or "active", "project_type": p.get("project_type")}
                          for p in pm.get(r["id"], [])],
             "estimates": [{"id": e["id"], "code": e["code"], "status": e["status"], "est_stage": e.get("est_stage"),
                            "final_amount": _num(e.get("final_amount")), "submitted_date": e.get("submitted_date"),
                            "decided_date": e.get("decided_date"),
                            "converted_project_code": e.get("converted_project_code")}
                           for e in em.get(r["id"], [])]}
        out.setdefault(r["client_org_id"], []).append(b)

    def loose(org_id):
        lst = out.setdefault(org_id, [])
        for b in lst:
            if b.get("id") is None:
                return b
        b = {"id": None, "key": "", "address": "No address on file", "name": None, "borough": None,
             "projects": [], "estimates": []}
        lst.append(b)
        return b

    for p in _rows(conn.execute(
            "SELECT project_code, name, status, project_type, client_org_id FROM projects "
            "WHERE client_org_id IS NOT NULL AND building_id IS NULL")):
        loose(p["client_org_id"])["projects"].append({
            "project_code": p["project_code"], "name": p["name"],
            "status": p.get("status") or "active", "project_type": p.get("project_type")})
    for e in _rows(conn.execute(
            "SELECT id, code, status, est_stage, final_amount, submitted_date, decided_date, "
            "converted_project_code, client_org_id FROM estimate "
            "WHERE client_org_id IS NOT NULL AND building_id IS NULL")):
        loose(e["client_org_id"])["estimates"].append({
            "id": e["id"], "code": e["code"], "status": e["status"], "est_stage": e.get("est_stage"),
            "final_amount": _num(e.get("final_amount")), "submitted_date": e.get("submitted_date"),
            "decided_date": e.get("decided_date"), "converted_project_code": e.get("converted_project_code")})
    for lst in out.values():
        for b in lst:
            b["active_projects"] = sum(1 for p in b["projects"] if p["status"] == "active")
            b["open_estimates"] = sum(1 for e in b["estimates"] if e["status"] in PIPELINE_STATUSES)
            b["won_value"] = round(sum(e["final_amount"] for e in b["estimates"]
                                       if e["status"] in WON_STATUSES), 2)
    return out


# ============================ overview ============================

def overview(conn, today: Optional[date] = None) -> dict:
    today = today or _today()
    t = today.isoformat()
    ym = t[:7]
    year = t[:4]
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    in30 = (today + timedelta(days=30)).isoformat()

    active_projects = _scalar(conn, "SELECT COUNT(1) FROM projects WHERE COALESCE(status,'active') = 'active'")
    clients = _scalar(conn, "SELECT COUNT(1) FROM crm_organization "
                            "WHERE relationship_type = 'client' AND COALESCE(status,'active') = 'active'")
    bmap = buildings_by_client(conn)
    buildings = sum(len(v) for v in bmap.values())

    pipe = _one(conn.execute(
        "SELECT COUNT(1) AS n, COALESCE(SUM(final_amount),0) AS v FROM estimate "
        "WHERE status IN ('intake','scoping','submitted')")) or {}
    won = _one(conn.execute(
        "SELECT COUNT(1) AS n, COALESCE(SUM(final_amount),0) AS v FROM estimate "
        "WHERE status IN ('approved','converted') AND COALESCE(decided_date, updated_at) >= ?",
        (year + "-01-01",))) or {}
    lost = _scalar(conn, "SELECT COUNT(1) FROM estimate WHERE status = 'lost' "
                         "AND COALESCE(decided_date, updated_at) >= ?", (year + "-01-01",))

    receipts_mtd = _scalar(conn, "SELECT COALESCE(SUM(total),0) FROM expenses "
                                 "WHERE expense_date LIKE ? AND status <> 'draft'", (ym + "%",))
    receipts_ytd = _scalar(conn, "SELECT COALESCE(SUM(total),0) FROM expenses "
                                 "WHERE expense_date LIKE ? AND status <> 'draft'", (year + "%",))
    ledger_mtd = _scalar(conn, "SELECT COALESCE(SUM(amount),0) FROM project_expense "
                               "WHERE expense_date LIKE ? AND voided_at IS NULL", (ym + "%",))
    ledger_ytd = _scalar(conn, "SELECT COALESCE(SUM(amount),0) FROM project_expense "
                               "WHERE expense_date LIKE ? AND voided_at IS NULL", (year + "%",))
    needs_review = _scalar(conn, "SELECT COUNT(1) FROM expenses WHERE status = 'needs_review'")

    headcount = _scalar(conn, "SELECT COUNT(1) FROM employees WHERE archived_at IS NULL")
    on_site = _scalar(conn, "SELECT COUNT(DISTINCT employee_id) FROM sign_in_log WHERE date = ?", (t,))
    hours_week = 0.0
    for r in _rows(conn.execute("SELECT time_in, time_out FROM sign_in_log WHERE date >= ? AND date <= ?",
                                (week_start, t))):
        hours_week += compute_worked_hours(r.get("time_in"), r.get("time_out"))

    receivables = _one(conn.execute(
        "SELECT COUNT(1) AS n, COALESCE(SUM(e.final_amount),0) AS v, "
        "       SUM(CASE WHEN COALESCE(j.deposit_received,0) = 0 THEN 1 ELSE 0 END) AS no_deposit "
        "FROM ira_job j LEFT JOIN estimate e ON e.id = j.estimate_id "
        "WHERE COALESCE(j.balance_status,'open') = 'open'")) or {}

    exp = compliance(conn, today=today, horizon_days=30)
    expiring = {"expired": exp["summary"]["expired"], "next_30": exp["summary"]["due_30"]}

    return {
        "as_of": t,
        "projects": {"active": int(active_projects)},
        "clients": {"active": int(clients), "buildings": int(buildings)},
        "pipeline": {"open_count": int(pipe.get("n") or 0), "open_value": round(_num(pipe.get("v")), 2),
                     "won_count_ytd": int(won.get("n") or 0), "won_value_ytd": round(_num(won.get("v")), 2),
                     "lost_count_ytd": int(lost)},
        "spend": {"receipts_mtd": round(_num(receipts_mtd), 2), "receipts_ytd": round(_num(receipts_ytd), 2),
                  "ledger_mtd": round(_num(ledger_mtd), 2), "ledger_ytd": round(_num(ledger_ytd), 2),
                  "receipts_needing_review": int(needs_review)},
        "workforce": {"headcount": int(headcount), "on_site_today": int(on_site),
                      "worked_hours_this_week": round(hours_week, 2)},
        "receivables": {"open_jobs": int(receivables.get("n") or 0),
                        "open_value": round(_num(receivables.get("v")), 2),
                        "awaiting_deposit": int(receivables.get("no_deposit") or 0)},
        "invoices": _safe_invoice_summary(conn, today),   # #315
        "expiring": expiring,
    }


def _safe_invoice_summary(conn, today) -> dict:
    """#315 — a fresh preprod DB may not have the invoice tables yet; never break the overview."""
    try:
        return invoices.receivables_summary(conn, today=today)
    except Exception as e:
        log.info("#315 invoice summary unavailable: %s", e)
        return {"available": False, "open_count": 0, "open_value": 0, "overdue_count": 0, "overdue_value": 0,
                "due_soon_count": 0, "due_soon_value": 0, "horizon_days": 30, "awaiting_deposit_count": 0,
                "awaiting_deposit_value": 0, "draft_count": 0, "draft_value": 0,
                "collected_ytd_count": 0, "collected_ytd_value": 0}


# ============================ clients ============================

def list_clients(conn, *, search: Optional[str] = None, relationship_type: str = "client",
                 include_inactive: bool = False) -> list:
    sql = ("SELECT o.id, o.name, o.relationship_type, o.status, o.stage, o.function_tags, o.created_at "
           "FROM crm_organization o WHERE 1=1")
    params: list = []
    if relationship_type and relationship_type != "all":
        sql += " AND o.relationship_type = ?"
        params.append(relationship_type)
    if not include_inactive:
        sql += " AND COALESCE(o.status,'active') = 'active'"
    if search:
        sql += " AND LOWER(o.name) LIKE ?"
        params.append("%" + search.lower() + "%")
    sql += " ORDER BY LOWER(o.name)"
    orgs = _rows(conn.execute(sql, params))
    if not orgs:
        return []

    contacts = {r["org_id"]: r["n"] for r in _rows(conn.execute(
        "SELECT org_id, COUNT(1) AS n FROM crm_contact WHERE org_id IS NOT NULL GROUP BY org_id"))}
    projects = {r["client_org_id"]: r for r in _rows(conn.execute(
        "SELECT client_org_id, COUNT(1) AS n, "
        "       SUM(CASE WHEN COALESCE(status,'active') = 'active' THEN 1 ELSE 0 END) AS active "
        "FROM projects WHERE client_org_id IS NOT NULL GROUP BY client_org_id"))}
    estimates = {r["client_org_id"]: r for r in _rows(conn.execute(
        "SELECT client_org_id, "
        "       SUM(CASE WHEN status IN ('intake','scoping','submitted') THEN 1 ELSE 0 END) AS open_n, "
        "       COALESCE(SUM(CASE WHEN status IN ('intake','scoping','submitted') THEN final_amount ELSE 0 END),0) AS open_v, "
        "       SUM(CASE WHEN status IN ('approved','converted') THEN 1 ELSE 0 END) AS won_n, "
        "       COALESCE(SUM(CASE WHEN status IN ('approved','converted') THEN final_amount ELSE 0 END),0) AS won_v "
        "FROM estimate WHERE client_org_id IS NOT NULL GROUP BY client_org_id"))}
    last_activity = {r["entity_id"]: r["last_at"] for r in _rows(conn.execute(
        "SELECT entity_id, MAX(created_at) AS last_at FROM crm_activity "
        "WHERE entity_type = 'organization' GROUP BY entity_id"))}
    open_tasks = {r["entity_id"]: r["n"] for r in _rows(conn.execute(
        "SELECT entity_id, COUNT(1) AS n FROM crm_task "
        "WHERE entity_type = 'organization' AND status = 'open' GROUP BY entity_id"))}
    bmap = buildings_by_client(conn)

    out = []
    for o in orgs:
        oid = o["id"]
        p = projects.get(oid) or {}
        e = estimates.get(oid) or {}
        blds = bmap.get(oid) or []
        out.append({
            "id": oid, "name": o["name"], "relationship_type": o["relationship_type"],
            "status": o.get("status") or "active", "stage": o.get("stage"),
            "function_tags": o.get("function_tags"),
            "contacts": int(contacts.get(oid) or 0),
            "buildings": len(blds),
            "projects_total": int(p.get("n") or 0), "projects_active": int(p.get("active") or 0),
            "estimates_open": int(e.get("open_n") or 0), "pipeline_value": round(_num(e.get("open_v")), 2),
            "estimates_won": int(e.get("won_n") or 0), "won_value": round(_num(e.get("won_v")), 2),
            "open_tasks": int(open_tasks.get(oid) or 0),
            "last_activity_at": last_activity.get(oid),
        })
    return out


def client_detail(conn, org_id: int, today: Optional[date] = None) -> Optional[dict]:
    org = _one(conn.execute(
        "SELECT id, name, relationship_type, status, stage, function_tags, notes, created_at "
        "FROM crm_organization WHERE id = ?", (org_id,)))
    if not org:
        return None
    today = today or _today()
    contacts = _rows(conn.execute(
        "SELECT id, full_name, email, phone, title, relationship_type, status "
        "FROM crm_contact WHERE org_id = ? ORDER BY LOWER(full_name)", (org_id,)))
    buildings = (buildings_by_client(conn).get(org_id) or [])
    project_codes = [p["project_code"] for b in buildings for p in b["projects"]]
    docs = []
    if project_codes:
        marks = ",".join("?" * len(project_codes))
        for d in _rows(conn.execute(
                f"SELECT id, project_code, category, requirement_key, title, expiry_date, version "
                f"FROM project_documents WHERE project_code IN ({marks}) "
                f"AND COALESCE(superseded,0) = 0 AND expiry_date IS NOT NULL "
                f"ORDER BY expiry_date", project_codes)):
            d["days_left"] = _days_until(d["expiry_date"], today)
            docs.append(d)
    activity = _rows(conn.execute(
        "SELECT id, activity_type, summary, created_at FROM crm_activity "
        "WHERE entity_type = 'organization' AND entity_id = ? ORDER BY created_at DESC LIMIT 10",
        (org_id,)))
    tasks = _rows(conn.execute(
        "SELECT id, title, due_date, priority, status FROM crm_task "
        "WHERE entity_type = 'organization' AND entity_id = ? AND status = 'open' "
        "ORDER BY COALESCE(due_date,'9999') LIMIT 20", (org_id,)))
    totals = {
        "buildings": len(buildings),
        "projects": sum(len(b["projects"]) for b in buildings),
        "projects_active": sum(b["active_projects"] for b in buildings),
        "estimates_open": sum(b["open_estimates"] for b in buildings),
        "won_value": round(sum(b["won_value"] for b in buildings), 2),
        "pipeline_value": round(sum(e["final_amount"] for b in buildings for e in b["estimates"]
                                    if e["status"] in PIPELINE_STATUSES), 2),
    }
    return {"organization": org, "contacts": contacts, "buildings": buildings,
            "documents_expiring": docs, "activity": activity, "open_tasks": tasks, "totals": totals}


# ============================ finance ============================

def _month_list(months: int, today: date) -> list:
    y, m = today.year, today.month
    out = []
    for _ in range(months):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def finance(conn, *, months: int = 12, today: Optional[date] = None) -> dict:
    today = today or _today()
    months = max(1, min(int(months or 12), 36))
    labels = _month_list(months, today)
    since = labels[0] + "-01"

    def series(sql, params):
        d = {r["ym"]: _num(r["v"]) for r in _rows(conn.execute(sql, params))}
        return [round(d.get(k, 0.0), 2) for k in labels]

    receipts = series("SELECT substr(expense_date,1,7) AS ym, SUM(total) AS v FROM expenses "
                      "WHERE expense_date >= ? AND status <> 'draft' GROUP BY substr(expense_date,1,7)", (since,))
    ledger = series("SELECT substr(expense_date,1,7) AS ym, SUM(amount) AS v FROM project_expense "
                    "WHERE expense_date >= ? AND voided_at IS NULL GROUP BY substr(expense_date,1,7)", (since,))
    submitted = series("SELECT substr(submitted_date,1,7) AS ym, SUM(final_amount) AS v FROM estimate "
                       "WHERE submitted_date >= ? GROUP BY substr(submitted_date,1,7)", (since,))
    won = series("SELECT substr(decided_date,1,7) AS ym, SUM(final_amount) AS v FROM estimate "
                 "WHERE decided_date >= ? AND status IN ('approved','converted') "
                 "GROUP BY substr(decided_date,1,7)", (since,))

    by_category = _rows(conn.execute(
        "SELECT category, source, SUM(v) AS total, SUM(n) AS n FROM ("
        "  SELECT COALESCE(category,'uncategorized') AS category, 'receipts' AS source, total AS v, 1 AS n "
        "  FROM expenses WHERE expense_date >= ? AND status <> 'draft' "
        "  UNION ALL "
        "  SELECT COALESCE(category,'uncategorized'), 'ledger', amount, 1 "
        "  FROM project_expense WHERE expense_date >= ? AND voided_at IS NULL"
        ") x GROUP BY category, source ORDER BY total DESC", (since, since)))
    by_project = _rows(conn.execute(
        "SELECT x.project_code, p.name, SUM(x.v) AS total FROM ("
        "  SELECT project_code, total AS v FROM expenses WHERE expense_date >= ? AND status <> 'draft' "
        "  UNION ALL "
        "  SELECT project_code, amount FROM project_expense WHERE expense_date >= ? AND voided_at IS NULL"
        ") x LEFT JOIN projects p ON p.project_code = x.project_code "
        "GROUP BY x.project_code, p.name ORDER BY total DESC", (since, since)))
    pipeline = _rows(conn.execute(
        "SELECT status, COUNT(1) AS n, COALESCE(SUM(final_amount),0) AS value FROM estimate "
        "GROUP BY status"))
    order = {s: i for i, s in enumerate(("intake", "scoping", "submitted", "approved", "converted", "lost"))}
    pipeline.sort(key=lambda r: order.get(r["status"], 99))
    receivables = _rows(conn.execute(
        "SELECT j.project_code, p.name AS project_name, o.name AS client_name, e.code AS estimate_code, "
        "       e.final_amount, j.deposit_received, j.deposit_date, j.balance_status, j.qb_invoice_ref, "
        "       j.cd5_status, j.report_sent_date "
        "FROM ira_job j LEFT JOIN estimate e ON e.id = j.estimate_id "
        "LEFT JOIN projects p ON p.project_code = j.project_code "
        "LEFT JOIN crm_organization o ON o.id = e.client_org_id "
        "ORDER BY CASE WHEN COALESCE(j.balance_status,'open') = 'open' THEN 0 ELSE 1 END, j.updated_at DESC"))
    qb = _one(conn.execute(
        "SELECT SUM(CASE WHEN COALESCE(synced_to_qb,0) <> 0 THEN 1 ELSE 0 END) AS synced, COUNT(1) AS total "
        "FROM expenses WHERE status <> 'draft'")) or {}

    labor = labor_cost(conn, days=30, today=today)

    return {
        "months": labels,
        "series": {"receipts": receipts, "ledger": ledger, "submitted": submitted, "won": won},
        "spend_by_category": by_category,
        "spend_by_project": by_project,
        "pipeline_by_status": pipeline,
        "receivables": receivables,
        "invoices": _safe_invoice_list(conn, today),          # #315
        "invoice_summary": _safe_invoice_summary(conn, today),
        "quickbooks": {"receipts_synced": int(qb.get("synced") or 0), "receipts_total": int(qb.get("total") or 0)},
        "labor_last_30_days": labor,
        "subscriptions": subscriptions(conn, today=today),
    }


def _safe_invoice_list(conn, today) -> list:
    try:
        return invoices.list_invoices(conn, limit=300, today=today)
    except Exception as e:
        log.info("#315 invoice list unavailable: %s", e)
        return []


def subscriptions(conn, today: Optional[date] = None) -> dict:
    """Recurring company costs (the `subscriptions` ledger, schema_subscriptions.sql).
    The table exists on the live DB but is not part of the migration lists, so a
    fresh preprod database may not have it: report `available: False` instead of
    failing the whole finance view. Burn math is done here (portable) rather than
    through the SQLite-only julianday() views."""
    today = today or _today()
    try:
        rows = _rows(conn.execute(
            "SELECT id, service_name, provider, category, plan_tier, seats, unit_cost_usd, "
            "       billing_cycle, renewal_date, status, owner_email, payment_method "
            "FROM subscriptions ORDER BY category, service_name"))
    except Exception:
        try:
            conn.rollback()   # a failed SELECT aborts a Postgres transaction
        except Exception:
            pass
        return {"available": False, "monthly_burn": 0.0, "by_category": [], "upcoming_renewals": [],
                "active_count": 0, "items": []}
    by_cat: dict = {}
    total = 0.0
    upcoming = []
    for r in rows:
        seats = _num(r.get("seats")) or 1
        unit = _num(r.get("unit_cost_usd"))
        cycle = (r.get("billing_cycle") or "").lower()
        monthly = unit * seats if cycle == "monthly" else (unit * seats / 12.0 if cycle == "annual"
                  else (unit * seats / 3.0 if cycle == "quarterly" else 0.0))
        r["monthly_usd"] = round(monthly, 2)
        if (r.get("status") or "active") == "active":
            total += monthly
            c = by_cat.setdefault(r.get("category") or "Other", {"category": r.get("category") or "Other",
                                                                  "active_count": 0, "monthly_usd": 0.0})
            c["active_count"] += 1
            c["monthly_usd"] += monthly
            d = _days_until(r.get("renewal_date"), today) if r.get("renewal_date") not in (None, "", "PENDING") else None
            r["days_until_renewal"] = d
            if d is not None and 0 <= d <= 30:
                upcoming.append(r)
    cats = sorted(by_cat.values(), key=lambda x: -x["monthly_usd"])
    for c in cats:
        c["monthly_usd"] = round(c["monthly_usd"], 2)
    upcoming.sort(key=lambda x: x.get("renewal_date") or "")
    return {"available": True, "monthly_burn": round(total, 2), "by_category": cats,
            "upcoming_renewals": upcoming,
            "active_count": sum(1 for r in rows if (r.get("status") or "active") == "active"),
            "items": rows}


def _rate_on(conn, employee_id: str, work_date: str, state_rates: dict, cache: dict) -> float:
    """The rate effective ON THE WORK DATE (worker_rates, the canonical
    effective-dated table — same rule as project_costs: a mid-history raise
    splits spend at the boundary). Falls back to labor_worker_state.current_rate
    for workers whose approved rate was never bridged into worker_rates."""
    key = (employee_id, work_date)
    if key in cache:
        return cache[key]
    rate = 0.0
    try:
        from worker_rates import get_rate_effective_on
        row = get_rate_effective_on(conn, employee_id, work_date)
        rate = _num(row.get("hourly_rate")) if row else 0.0
    except Exception:
        rate = 0.0
    if rate <= 0:
        rate = _num((state_rates.get(employee_id) or {}).get("current_rate"))
    cache[key] = rate
    return rate


def labor_cost(conn, *, days: int = 30, today: Optional[date] = None) -> dict:
    """WORKED hours x the rate effective on each work date — an estimate of
    labor cost, not payroll. Hours without any rate on file are reported as an
    explicit UNRATED bucket, never as $0. Comp-class data: company-only surface."""
    today = today or _today()
    since = (today - timedelta(days=days - 1)).isoformat()
    rates = {r["employee_id"]: r for r in _rows(conn.execute(
        "SELECT employee_id, trade, current_rate FROM labor_worker_state"))}
    by_trade: dict = {}
    total_hours = 0.0
    total_cost = 0.0
    unrated_hours = 0.0
    rate_cache: dict = {}
    for r in _rows(conn.execute(
            "SELECT s.employee_id, s.date, s.time_in, s.time_out, e.trade FROM sign_in_log s "
            "LEFT JOIN employees e ON e.employee_id = s.employee_id "
            "WHERE s.date >= ? AND s.date <= ?", (since, today.isoformat()))):
        h = compute_worked_hours(r.get("time_in"), r.get("time_out"))
        if h <= 0:
            continue
        st = rates.get(r["employee_id"]) or {}
        rate = _rate_on(conn, r["employee_id"], r.get("date") or today.isoformat(), rates, rate_cache)
        trade = st.get("trade") or r.get("trade") or "unassigned"
        b = by_trade.setdefault(trade, {"trade": trade, "hours": 0.0, "cost": 0.0})
        b["hours"] += h
        total_hours += h
        if rate > 0:
            b["cost"] += h * rate
            total_cost += h * rate
        else:
            unrated_hours += h
    trades = sorted(by_trade.values(), key=lambda x: -x["hours"])
    for t in trades:
        t["hours"] = round(t["hours"], 2)
        t["cost"] = round(t["cost"], 2)
    return {"since": since, "days": days, "worked_hours": round(total_hours, 2),
            "estimated_cost": round(total_cost, 2), "unrated_hours": round(unrated_hours, 2),
            "by_trade": trades}


# ============================ expenses ============================

def list_expenses(conn, *, project_code: Optional[str] = None, category: Optional[str] = None,
                  month: Optional[str] = None, source: Optional[str] = None,
                  status: Optional[str] = None, limit: int = 200) -> dict:
    limit = max(1, min(int(limit or 200), 1000))
    items = []
    if source in (None, "", "all", "receipts"):
        sql = ("SELECT e.id, 'receipts' AS source, e.project_code, p.name AS project_name, e.expense_date, "
               "       e.vendor, e.category, e.cost_code, e.payment_method, e.total AS amount, e.status, "
               "       e.doc_type, e.doc_number, e.synced_to_qb, e.notes "
               "FROM expenses e LEFT JOIN projects p ON p.project_code = e.project_code WHERE 1=1")
        params: list = []
        if project_code:
            sql += " AND e.project_code = ?"; params.append(project_code)
        if category:
            sql += " AND e.category = ?"; params.append(category)
        if month:
            sql += " AND e.expense_date LIKE ?"; params.append(month + "%")
        if status:
            sql += " AND e.status = ?"; params.append(status)
        sql += " ORDER BY e.expense_date DESC, e.id DESC LIMIT ?"; params.append(limit)
        items += _rows(conn.execute(sql, params))
    if source in (None, "", "all", "ledger"):
        sql = ("SELECT x.id, 'ledger' AS source, x.project_code, p.name AS project_name, x.expense_date, "
               "       x.vendor, x.category, NULL AS cost_code, NULL AS payment_method, x.amount, "
               "       'booked' AS status, NULL AS doc_type, NULL AS doc_number, 0 AS synced_to_qb, x.note AS notes "
               "FROM project_expense x LEFT JOIN projects p ON p.project_code = x.project_code "
               "WHERE x.voided_at IS NULL")
        params = []
        if project_code:
            sql += " AND x.project_code = ?"; params.append(project_code)
        if category:
            sql += " AND x.category = ?"; params.append(category)
        if month:
            sql += " AND x.expense_date LIKE ?"; params.append(month + "%")
        if status and status != "booked":
            sql += " AND 1=0"
        sql += " ORDER BY x.expense_date DESC, x.id DESC LIMIT ?"; params.append(limit)
        items += _rows(conn.execute(sql, params))
    items.sort(key=lambda r: (r.get("expense_date") or "", r.get("id") or 0), reverse=True)
    items = items[:limit]
    for it in items:
        it["amount"] = round(_num(it.get("amount")), 2)
    total = round(sum(i["amount"] for i in items), 2)
    categories = sorted({r["category"] for r in _rows(conn.execute(
        "SELECT DISTINCT category FROM expenses WHERE category IS NOT NULL "
        "UNION SELECT DISTINCT category FROM project_expense WHERE category IS NOT NULL"))
        if r.get("category")})
    projects = _rows(conn.execute(
        "SELECT project_code, name FROM projects ORDER BY COALESCE(status,'active') = 'active' DESC, name"))
    return {"items": items, "total": total, "filters": {"categories": categories, "projects": projects}}


# ============================ workforce ============================

def workforce(conn, today: Optional[date] = None) -> dict:
    today = today or _today()
    t = today.isoformat()
    since7 = (today - timedelta(days=6)).isoformat()
    since30 = (today - timedelta(days=29)).isoformat()
    in30 = (today + timedelta(days=30)).isoformat()

    workers = _rows(conn.execute(
        "SELECT e.employee_id, e.worker_id, e.name, e.trade, "
        "       s.current_rate, s.status AS rate_status, s.effective_date "
        "FROM employees e LEFT JOIN labor_worker_state s ON s.employee_id = e.employee_id "
        "WHERE e.archived_at IS NULL ORDER BY e.worker_id, e.name"))
    hours7: dict = {}
    hours30: dict = {}
    last_seen: dict = {}
    for r in _rows(conn.execute(
            "SELECT employee_id, date, time_in, time_out FROM sign_in_log WHERE date >= ? AND date <= ?",
            (since30, t))):
        h = compute_worked_hours(r.get("time_in"), r.get("time_out"))
        hours30[r["employee_id"]] = hours30.get(r["employee_id"], 0.0) + h
        if r["date"] >= since7:
            hours7[r["employee_id"]] = hours7.get(r["employee_id"], 0.0) + h
        if r["date"] > last_seen.get(r["employee_id"], ""):
            last_seen[r["employee_id"]] = r["date"]
    certs = {r["employee_id"]: r for r in _rows(conn.execute(
        "SELECT employee_id, COUNT(1) AS total, "
        "       SUM(CASE WHEN expiration_date IS NOT NULL AND expiration_date < ? THEN 1 ELSE 0 END) AS expired, "
        "       SUM(CASE WHEN expiration_date >= ? AND expiration_date <= ? THEN 1 ELSE 0 END) AS expiring "
        "FROM certifications GROUP BY employee_id", (t, t, in30)))}
    pending = {r["employee_id"]: r["n"] for r in _rows(conn.execute(
        "SELECT employee_id, COUNT(1) AS n FROM labor_rate_change WHERE status = 'pending' GROUP BY employee_id"))}

    by_trade: dict = {}
    for w in workers:
        c = certs.get(w["employee_id"]) or {}
        w["hours_7d"] = round(hours7.get(w["employee_id"], 0.0), 2)
        w["hours_30d"] = round(hours30.get(w["employee_id"], 0.0), 2)
        w["last_on_site"] = last_seen.get(w["employee_id"])
        w["certs_total"] = int(c.get("total") or 0)
        w["certs_expired"] = int(c.get("expired") or 0)
        w["certs_expiring_30"] = int(c.get("expiring") or 0)
        w["pending_rate_changes"] = int(pending.get(w["employee_id"]) or 0)
        w["current_rate"] = _num(w.get("current_rate")) or None
        tr = w.get("trade") or "unassigned"
        b = by_trade.setdefault(tr, {"trade": tr, "workers": 0, "rated": 0, "rate_sum": 0.0})
        b["workers"] += 1
        if w["current_rate"]:
            b["rated"] += 1
            b["rate_sum"] += w["current_rate"]
    trades = []
    for b in by_trade.values():
        trades.append({"trade": b["trade"], "workers": b["workers"],
                       "avg_rate": round(b["rate_sum"] / b["rated"], 2) if b["rated"] else None})
    trades.sort(key=lambda x: -x["workers"])
    return {
        "workers": workers,
        "by_trade": trades,
        "summary": {
            "headcount": len(workers),
            "rated": sum(1 for w in workers if w["current_rate"]),
            "on_site_today": sum(1 for w in workers if w["last_on_site"] == t),
            "certs_expired": sum(w["certs_expired"] for w in workers),
            "certs_expiring_30": sum(w["certs_expiring_30"] for w in workers),
            "pending_rate_changes": sum(w["pending_rate_changes"] for w in workers),
        },
    }


# ============================ compliance ============================

def compliance(conn, *, today: Optional[date] = None, horizon_days: int = 90) -> dict:
    today = today or _today()
    t = today.isoformat()
    horizon = (today + timedelta(days=int(horizon_days or 90))).isoformat()
    items = []

    for r in _rows(conn.execute(
            "SELECT c.id, c.expiration_date, c.status, ct.name AS cert_name, ct.category, "
            "       e.employee_id, e.worker_id, e.name AS worker_name "
            "FROM certifications c JOIN cert_types ct ON ct.cert_type_id = c.cert_type_id "
            "JOIN employees e ON e.employee_id = c.employee_id "
            "WHERE e.archived_at IS NULL AND c.expiration_date IS NOT NULL AND c.expiration_date <= ?",
            (horizon,))):
        items.append({"kind": "worker_cert", "id": r["id"], "title": r["cert_name"],
                      "group": r.get("category"), "subject": f"{r.get('worker_id') or ''} — {r.get('worker_name') or ''}".strip(" —"),
                      "subject_ref": r["employee_id"], "expires_on": r["expiration_date"]})
    for r in _rows(conn.execute(
            "SELECT pl.permit_id, pl.permit_type, pl.permit_number, pl.issuing_agency, pl.expiration_date, "
            "       pl.status, pl.project_code, p.name AS project_name "
            "FROM permits_library pl LEFT JOIN projects p ON p.project_code = pl.project_code "
            "WHERE pl.expiration_date IS NOT NULL AND pl.expiration_date <= ?", (horizon,))):
        items.append({"kind": "permit", "id": r["permit_id"], "title": r["permit_type"] or "Permit",
                      "group": r.get("issuing_agency"), "subject": r.get("project_name") or r.get("project_code"),
                      "subject_ref": r.get("project_code"), "number": r.get("permit_number"),
                      "expires_on": r["expiration_date"]})
    for r in _rows(conn.execute(
            "SELECT d.id, d.project_code, d.category, d.requirement_key, d.title, d.expiry_date, p.name AS project_name "
            "FROM project_documents d LEFT JOIN projects p ON p.project_code = d.project_code "
            "WHERE COALESCE(d.superseded,0) = 0 AND d.expiry_date IS NOT NULL AND d.expiry_date <= ?",
            (horizon,))):
        items.append({"kind": "project_document", "id": r["id"], "title": r["title"],
                      "group": r.get("category"), "subject": r.get("project_name") or r.get("project_code"),
                      "subject_ref": r.get("project_code"), "expires_on": r["expiry_date"]})
    for r in _rows(conn.execute(
            "SELECT d.id, d.estimate_id, d.category, d.title, d.expiry_date, e.code "
            "FROM estimate_document d LEFT JOIN estimate e ON e.id = d.estimate_id "
            "WHERE d.expiry_date IS NOT NULL AND d.expiry_date <= ?", (horizon,))):
        items.append({"kind": "estimate_document", "id": r["id"], "title": r["title"],
                      "group": r.get("category"), "subject": r.get("code"),
                      "subject_ref": r.get("estimate_id"), "expires_on": r["expiry_date"]})
    for r in _rows(conn.execute(
            "SELECT d.id, d.category, d.title, d.holder, d.doc_number, d.issuer, d.expiry_date, d.vehicle_id, "
            "       v.label AS vehicle_label "
            "FROM company_document d LEFT JOIN vehicle v ON v.id = d.vehicle_id "
            "WHERE d.archived_at IS NULL AND d.expiry_date IS NOT NULL AND d.expiry_date <= ?", (horizon,))):
        vehicle = r.get("vehicle_label")
        items.append({"kind": "vehicle" if r.get("vehicle_id") else "company_document", "id": r["id"], "title": r["title"],
                      "group": COMPANY_DOC_CATEGORIES.get(r["category"], r["category"]),
                      "subject": vehicle or r.get("holder") or r.get("issuer") or "Company",
                      "subject_ref": r.get("vehicle_id"),
                      "number": r.get("doc_number"), "expires_on": r["expiry_date"]})

    for it in items:
        d = _days_until(it["expires_on"], today)
        it["days_left"] = d
        it["state"] = "expired" if (d is not None and d < 0) else ("due_30" if d is not None and d <= 30
                                                                   else ("due_60" if d is not None and d <= 60 else "due_90"))
    items.sort(key=lambda x: (x["expires_on"] or "", x["kind"]))
    summary = {"expired": sum(1 for i in items if i["state"] == "expired"),
               "due_30": sum(1 for i in items if i["state"] == "due_30"),
               "due_60": sum(1 for i in items if i["state"] == "due_60"),
               "due_90": sum(1 for i in items if i["state"] == "due_90")}
    by_kind: dict = {}
    for i in items:
        by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
    return {"as_of": t, "horizon_days": int(horizon_days or 90), "summary": summary,
            "by_kind": by_kind, "items": items}


# ============================ company documents ============================

def list_company_documents(conn, *, include_archived: bool = False, today: Optional[date] = None) -> list:
    today = today or _today()
    sql = ("SELECT d.id, d.category, d.title, d.holder, d.doc_number, d.issuer, d.effective_date, d.expiry_date, "
           "       d.amount, d.notes, d.created_at, d.updated_at, d.archived_at, d.vehicle_id, d.doc_year, "
           "       v.label AS vehicle_label "
           "FROM company_document d LEFT JOIN vehicle v ON v.id = d.vehicle_id")
    if not include_archived:
        sql += " WHERE d.archived_at IS NULL"
    sql += " ORDER BY COALESCE(d.expiry_date,'9999'), d.category, d.title"
    out = _rows(conn.execute(sql))
    import company_vault
    files = company_vault.files_by_document(conn, [d["id"] for d in out])
    for d in out:
        d["category_label"] = COMPANY_DOC_CATEGORIES.get(d["category"], d["category"])
        d["group"] = doc_group(d["category"])
        d["days_left"] = _days_until(d.get("expiry_date"), today)
        d["amount"] = _num(d.get("amount")) or None
        d["files"] = files.get(d["id"], [])
        d["file_count"] = len(d["files"])
    return out


def _validate_company_doc(payload: dict, *, partial: bool) -> tuple:
    d = {k: payload.get(k) for k in COMPANY_DOC_EDITABLE if k in payload}
    if not partial:
        if not (d.get("category") in COMPANY_DOC_CATEGORIES):
            return None, "category must be one of: " + ", ".join(sorted(COMPANY_DOC_CATEGORIES))
        if not str(d.get("title") or "").strip():
            return None, "title is required"
    if "category" in d and d["category"] not in COMPANY_DOC_CATEGORIES:
        return None, "unknown category"
    if "title" in d and not str(d.get("title") or "").strip():
        return None, "title cannot be blank"
    for k in ("effective_date", "expiry_date"):
        if k in d and not _valid_date(d[k]):
            return None, f"{k} must be YYYY-MM-DD"
    if "amount" in d and d["amount"] not in (None, ""):
        try:
            d["amount"] = float(d["amount"])
        except (TypeError, ValueError):
            return None, "amount must be a number"
    for k in ("title", "holder", "doc_number", "issuer", "notes"):
        if k in d and d[k] is not None:
            d[k] = str(d[k]).strip()[:500] or None
    if "title" in d and not d["title"]:
        return None, "title cannot be blank"
    for k in ("vehicle_id", "doc_year"):   # #318
        if k in d:
            if d[k] in (None, ""):
                d[k] = None
            else:
                try:
                    d[k] = int(d[k])
                except (TypeError, ValueError):
                    return None, f"{k} must be a whole number"
    if d.get("doc_year") is not None and not (1990 <= d["doc_year"] <= 2100):
        return None, "doc_year must be a four-digit year"
    return d, None


def create_company_document(conn, payload: dict, *, actor_id=None) -> tuple:
    d, err = _validate_company_doc(payload, partial=False)
    if err:
        return None, err
    import company_vault
    if d.get("vehicle_id") and not company_vault.get_vehicle(conn, d["vehicle_id"]):
        return None, "vehicle not found"
    now = _now_iso()
    conn.execute(
        "INSERT INTO company_document (category, title, holder, doc_number, issuer, effective_date, "
        "  expiry_date, amount, notes, vehicle_id, doc_year, created_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (d["category"], d["title"], d.get("holder"), d.get("doc_number"), d.get("issuer"),
         d.get("effective_date") or None, d.get("expiry_date") or None, d.get("amount"),
         d.get("notes"), d.get("vehicle_id"), d.get("doc_year"), actor_id, now, now))
    conn.commit()
    new_id = conn.execute("SELECT MAX(id) AS m FROM company_document").fetchone()["m"]
    return new_id, None


def update_company_document(conn, doc_id: int, payload: dict) -> tuple:
    d, err = _validate_company_doc(payload, partial=True)
    if err:
        return False, err
    if not d:
        return False, "nothing to update"
    if d.get("vehicle_id"):
        import company_vault
        if not company_vault.get_vehicle(conn, d["vehicle_id"]):
            return False, "vehicle not found"
    sets = ", ".join(f"{k} = ?" for k in d)
    params = [d[k] if d[k] != "" else None for k in d] + [_now_iso(), doc_id]
    cur = conn.execute(f"UPDATE company_document SET {sets}, updated_at = ? WHERE id = ? AND archived_at IS NULL",
                       params)
    conn.commit()
    return (cur.rowcount or 0) > 0, None


def archive_company_document(conn, doc_id: int) -> bool:
    cur = conn.execute("UPDATE company_document SET archived_at = ?, updated_at = ? "
                       "WHERE id = ? AND archived_at IS NULL", (_now_iso(), _now_iso(), doc_id))
    conn.commit()
    return (cur.rowcount or 0) > 0


# ============================ endpoints ============================

def _serve_page():
    resp = send_file(str(PAGE), conditional=False, etag=False, max_age=0)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


def _with_conn(fn):
    conn = _conn()
    try:
        return fn(conn)
    finally:
        conn.close()


def register(app) -> None:
    conn = _conn()
    try:
        ensure_schema(conn)
        # #290 lesson: a table CREATED at boot after the per-process pk-map cache
        # was populated needs the cache dropped so lastrowid works on Postgres.
        db_layer._PKMAP_CACHE.clear()
    except Exception as e:  # a fresh preprod DB may migrate later; never block boot
        log.warning("#302 company_document schema ensure skipped: %s", e)
    finally:
        conn.close()

    gate = requires_section(SECTION)

    @app.route("/company", methods=["GET"])
    @gate
    def company_portal_page():
        return _serve_page()

    @app.route("/api/company/portal/overview", methods=["GET"])
    @gate
    def company_portal_overview():
        return _wrap(_with_conn(overview))

    @app.route("/api/company/portal/clients", methods=["GET"])
    @gate
    def company_portal_clients():
        rt = (request.args.get("type") or "client").strip().lower()
        rows = _with_conn(lambda c: list_clients(
            c, search=(request.args.get("search") or "").strip() or None,
            relationship_type=rt, include_inactive=request.args.get("inactive") == "1"))
        return _wrap(rows)

    @app.route("/api/company/portal/clients/<int:org_id>", methods=["GET"])
    @gate
    def company_portal_client_detail(org_id):
        d = _with_conn(lambda c: client_detail(c, org_id))
        if d is None:
            return _err("organization not found", 404)
        return _wrap(d)

    @app.route("/api/company/portal/finance", methods=["GET"])
    @gate
    def company_portal_finance():
        try:
            months = int(request.args.get("months") or 12)
        except ValueError:
            return _err("months must be an integer")
        return _wrap(_with_conn(lambda c: finance(c, months=months)))

    @app.route("/api/company/portal/expenses", methods=["GET"])
    @gate
    def company_portal_expenses():
        a = request.args
        month = (a.get("month") or "").strip()
        if month and not re.fullmatch(r"\d{4}-\d{2}", month):
            return _err("month must be YYYY-MM")
        try:
            limit = int(a.get("limit") or 200)
        except ValueError:
            return _err("limit must be an integer")
        return _wrap(_with_conn(lambda c: list_expenses(
            c, project_code=(a.get("project") or "").strip() or None,
            category=(a.get("category") or "").strip() or None, month=month or None,
            source=(a.get("source") or "all").strip().lower(), status=(a.get("status") or "").strip() or None,
            limit=limit)))

    @app.route("/api/company/portal/workforce", methods=["GET"])
    @gate
    def company_portal_workforce():
        return _wrap(_with_conn(workforce))

    @app.route("/api/company/portal/compliance", methods=["GET"])
    @gate
    def company_portal_compliance():
        try:
            horizon = int(request.args.get("days") or 90)
        except ValueError:
            return _err("days must be an integer")
        return _wrap(_with_conn(lambda c: compliance(c, horizon_days=max(1, min(horizon, 365)))))

    @app.route("/api/company/portal/company-documents", methods=["GET"])
    @gate
    def company_documents_list():
        rows = _with_conn(lambda c: list_company_documents(c, include_archived=request.args.get("archived") == "1"))
        g = (request.args.get("group") or "").strip().lower()
        if g in COMPANY_DOC_GROUPS:
            rows = [r for r in rows if r["group"] == g]
        v = (request.args.get("vehicle_id") or "").strip()
        if v.isdigit():
            rows = [r for r in rows if r.get("vehicle_id") == int(v)]
        return _wrap(rows, categories=COMPANY_DOC_CATEGORIES, groups=COMPANY_DOC_GROUPS,
                     category_groups={k: doc_group(k) for k in COMPANY_DOC_CATEGORIES})

    @app.route("/api/company/portal/company-documents", methods=["POST"])
    @gate
    def company_documents_create():
        payload = request.get_json(silent=True) or {}
        actor = (current_user() or {}).get("id")
        new_id, err = _with_conn(lambda c: create_company_document(c, payload, actor_id=actor))
        if err:
            return _err(err)
        return _wrap({"id": new_id}), 201

    @app.route("/api/company/portal/company-documents/<int:doc_id>", methods=["PATCH"])
    @gate
    def company_documents_update(doc_id):
        payload = request.get_json(silent=True) or {}
        ok, err = _with_conn(lambda c: update_company_document(c, doc_id, payload))
        if err:
            return _err(err)
        if not ok:
            return _err("document not found", 404)
        return _wrap({"id": doc_id, "updated": True})

    @app.route("/api/company/portal/company-documents/<int:doc_id>", methods=["DELETE"])
    @gate
    def company_documents_archive(doc_id):
        ok = _with_conn(lambda c: archive_company_document(c, doc_id))
        if not ok:
            return _err("document not found", 404)
        return _wrap({"id": doc_id, "archived": True})

    import company_vault
    company_vault.register_routes(app, gate)   # #318 — vehicles + document files
    import worker_history
    worker_history.register_routes(app, gate)   # #320 — worker job history + payroll view
