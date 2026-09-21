"""#330 — the Company Portal's Projects tab lists JOBS, and a job's page shows only that job.

Until now the tab (born as "Buildings", #304) listed job sites — addresses that
aggregate every project and estimate at that address. Renaming it "Projects" (#328) made
the mismatch obvious: click a project and you got the address roll-up (open estimates,
won value, other projects). This module gives the portal a project-centric read:

  * `list_projects` — one row per job: identity, client, PM, crew size, contract, what is
    invoiced / open / overdue, last daily report, open RFIs.
  * `project_detail` — everything the office needs about ONE job, read from the modules
    that own each piece: invoices (#315) + payments summary (#326), AIA billing summary
    (#317), permits & insurance package readiness (#327), documents with expiry dates,
    crew from the assignments (#325), activity (daily reports, RFIs, photos), plus the
    links out (project dashboard, job site, client, CRM).

Job sites keep their own pages (`#buildings/<id>`, one address ↔ many jobs); the
Projects view links to them, they are no longer the front door.

Read-only; gated by the company-portal section (admin / c_suite). Never returns a
`*_path` or a worker's PII beyond name / trade (company-only surface, same as Workforce).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from flask import jsonify, request

import crm
import db_layer
from auth import requires_section

log = logging.getLogger("ssc.portal_projects")


def _conn():
    return db_layer.connect(pragma_fk=True)


def _rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]


def _one(cur) -> Optional[dict]:
    r = cur.fetchone()
    return dict(r) if r else None


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _today() -> date:
    return date.today()


def _safe(fn, default):
    """Every optional module is read defensively: one missing table must not blank the page."""
    try:
        return fn()
    except Exception as e:
        log.info("#330 optional piece unavailable: %s", e)
        return default


_PROJECT_COLS = ("p.project_code, p.name, p.address, p.city_zip, p.project_type, p.status, p.start_date, "
                 "p.target_completion, p.superintendent, p.client_org_id, p.building_id")


def _pms(conn, codes: list) -> dict:
    if not codes:
        return {}
    marks = ",".join("?" * len(codes))
    out: dict = {}
    for r in _rows(conn.execute(
            f"SELECT a.project_code, u.full_name, u.email FROM pm_project_assignment a JOIN users u ON u.id = a.user_id "
            f"WHERE a.project_code IN ({marks}) AND COALESCE(u.is_active,1) = 1 ORDER BY u.full_name", codes)):
        out.setdefault(r["project_code"], []).append({"name": r["full_name"], "email": r["email"]})
    return out


def _contract(conn, code: str) -> dict:
    """Contract value: the AIA schedule of values when one exists, else the converted estimate."""
    sov = _safe(lambda: _one(conn.execute(
        "SELECT SUM(CASE WHEN kind = 'base' THEN scheduled_value ELSE 0 END) AS base, "
        "       SUM(CASE WHEN kind <> 'base' THEN scheduled_value ELSE 0 END) AS co, COUNT(1) AS n "
        "FROM billing_sov WHERE project_code = ?", (code,))), None)
    if sov and int(sov.get("n") or 0):
        return {"value": round(_num(sov["base"]) + _num(sov["co"]), 2), "source": "schedule_of_values",
                "change_orders": round(_num(sov["co"]), 2)}
    est = _safe(lambda: _one(conn.execute(
        "SELECT code, final_amount FROM estimate WHERE converted_project_code = ? ORDER BY id DESC LIMIT 1", (code,))), None)
    if est and est.get("final_amount") is not None:
        return {"value": round(_num(est["final_amount"]), 2), "source": "estimate", "estimate_code": est.get("code")}
    return {"value": None, "source": None}


def list_projects(conn, *, status: str = "active", search: Optional[str] = None, today: Optional[date] = None) -> list:
    today = today or _today()
    where, params = ["1=1"], []
    if status == "active":
        where.append("COALESCE(p.status,'active') = 'active'")
    elif status and status != "all":
        where.append("COALESCE(p.status,'active') = ?"); params.append(status)
    if search:
        s = f"%{search.strip().lower()}%"
        where.append("(LOWER(p.project_code) LIKE ? OR LOWER(COALESCE(p.name,'')) LIKE ? OR LOWER(COALESCE(p.address,'')) LIKE ? "
                     "OR LOWER(COALESCE(o.name,'')) LIKE ?)")
        params += [s, s, s, s]
    rows = _rows(conn.execute(
        f"SELECT {_PROJECT_COLS}, o.name AS client_name FROM projects p "
        f"LEFT JOIN crm_organization o ON o.id = p.client_org_id "
        f"WHERE {' AND '.join(where)} ORDER BY CASE WHEN COALESCE(p.status,'active') = 'active' THEN 0 ELSE 1 END, p.project_code",
        params))
    codes = [r["project_code"] for r in rows]
    pms = _pms(conn, codes)
    marks = ",".join("?" * len(codes)) if codes else "NULL"
    crew = _safe(lambda: {r["project_code"]: int(r["n"]) for r in _rows(conn.execute(
        f"SELECT project_code, COUNT(1) AS n FROM project_assignments WHERE project_code IN ({marks}) "
        f"AND COALESCE(status,'active') = 'active' GROUP BY project_code", codes))} if codes else {}, {})
    t = today.isoformat()
    inv = _safe(lambda: {r["project_code"]: r for r in _rows(conn.execute(
        f"SELECT project_code, "
        f"  SUM(CASE WHEN status IN ('issued','paid') THEN amount ELSE 0 END) AS invoiced, "
        f"  SUM(CASE WHEN status = 'issued' THEN amount - amount_paid ELSE 0 END) AS open_value, "
        f"  SUM(CASE WHEN status = 'issued' AND due_at IS NOT NULL AND due_at < ? THEN amount - amount_paid ELSE 0 END) AS overdue_value, "
        f"  SUM(CASE WHEN status IN ('issued','paid') THEN amount_paid ELSE 0 END) AS collected "
        f"FROM invoice WHERE project_code IN ({marks}) GROUP BY project_code", [t] + codes))} if codes else {}, {})
    last_rep = _safe(lambda: {r["project_code"]: r["d"] for r in _rows(conn.execute(
        f"SELECT project_code, MAX(report_date) AS d FROM report_index WHERE project_code IN ({marks}) "
        f"AND report_type = 'DCR' GROUP BY project_code", codes))} if codes else {}, {})
    rfis = _safe(lambda: {r["project_code"]: int(r["n"]) for r in _rows(conn.execute(
        f"SELECT project_code, COUNT(1) AS n FROM rfi_log WHERE project_code IN ({marks}) "
        f"AND LOWER(COALESCE(status,'open')) NOT IN ('closed','answered','responded') GROUP BY project_code", codes))} if codes else {}, {})
    out = []
    for r in rows:
        c = r["project_code"]
        i = inv.get(c) or {}
        out.append({**r, "status": r.get("status") or "active", "pms": pms.get(c, []), "crew_count": crew.get(c, 0),
                    "contract": _contract(conn, c),
                    "invoiced": round(_num(i.get("invoiced")), 2), "open_value": round(_num(i.get("open_value")), 2),
                    "overdue_value": round(_num(i.get("overdue_value")), 2), "collected": round(_num(i.get("collected")), 2),
                    "last_report_date": last_rep.get(c), "open_rfis": rfis.get(c, 0)})
    return out


def project_detail(conn, code: str, *, today: Optional[date] = None) -> Optional[dict]:
    today = today or _today()
    p = _one(conn.execute(f"SELECT {_PROJECT_COLS}, o.name AS client_name FROM projects p "
                          f"LEFT JOIN crm_organization o ON o.id = p.client_org_id WHERE p.project_code = ?", (code,)))
    if not p:
        return None
    p["status"] = p.get("status") or "active"
    client = crm.get_org(conn, p["client_org_id"]) if p.get("client_org_id") else None
    contacts = _rows(conn.execute("SELECT id, full_name, email, phone FROM crm_contact WHERE org_id = ? ORDER BY full_name",
                                  (p["client_org_id"],))) if p.get("client_org_id") else []
    building = _safe(lambda: _one(conn.execute(
        "SELECT id, name, address_line, borough, city, zip, bin, block, lot, stories, year_built FROM building WHERE id = ?",
        (p["building_id"],))) if p.get("building_id") else None, None)
    crew = _safe(lambda: _rows(conn.execute(
        "SELECT a.employee_id, a.role_on_project, a.start_date, w.worker_id, w.name, w.trade FROM project_assignments a "
        "LEFT JOIN v_active_workers w ON w.employee_id = a.employee_id "
        "WHERE a.project_code = ? AND COALESCE(a.status,'active') = 'active' ORDER BY w.name, a.employee_id", (code,))), [])

    def money():
        import invoice_docs
        pay = invoice_docs.project_payments(conn, code, today=today) or {}
        return {"summary": pay.get("summary") or {}, "invoices": pay.get("invoices") or []}
    money_d = _safe(money, {"summary": {}, "invoices": []})

    def aia():
        import aia_billing
        b = aia_billing.billing(conn, code) or {}
        apps = b.get("applications") or []
        return {"summary": b.get("summary") or {}, "applications": [
            {k: a.get(k) for k in ("id", "number", "status", "invoice_number", "period_from", "period_to", "application_date")}
            | {"current_payment_due": (a.get("g702") or {}).get("current_payment_due"),
               "pct_complete": (a.get("g702") or {}).get("pct_complete")} for a in apps]}
    aia_d = _safe(aia, {"summary": {}, "applications": []})

    def package():
        import doc_packages
        pk = doc_packages.package(conn, code) or {}
        items = []
        for i in pk.get("items") or []:
            d = i.get("doc") or {}
            days = None
            if d.get("expiry_date"):
                try:
                    days = (date.fromisoformat(str(d["expiry_date"])[:10]) - today).days
                except ValueError:
                    days = None
            items.append({"key": i.get("requirement_key"), "category": i.get("category"), "label": i.get("label"),
                          "status": i.get("status"), "expiry_date": d.get("expiry_date"), "days_left": days,
                          "title": d.get("title")})
        return {"summary": pk.get("summary") or {}, "items": items, "last_sent": pk.get("last_sent"), "sends": pk.get("sends", 0)}
    package_d = _safe(package, {"summary": {}, "items": [], "last_sent": None, "sends": 0})

    docs = []
    for d in _safe(lambda: _rows(conn.execute(
            "SELECT id, category, requirement_key, title, expiry_date, version FROM project_documents "
            "WHERE project_code = ? AND COALESCE(superseded,0) = 0 AND expiry_date IS NOT NULL ORDER BY expiry_date", (code,))), []):
        try:
            d["days_left"] = (date.fromisoformat(str(d["expiry_date"])[:10]) - today).days
        except Exception:
            d["days_left"] = None
        docs.append(d)

    since = (today - timedelta(days=30)).isoformat()
    activity = {
        "reports_30d": _safe(lambda: int(_one(conn.execute(
            "SELECT COUNT(1) AS n FROM report_index WHERE project_code = ? AND report_type = 'DCR' AND report_date >= ?",
            (code, since)))["n"]), 0),
        "last_report_date": _safe(lambda: _one(conn.execute(
            "SELECT MAX(report_date) AS d FROM report_index WHERE project_code = ? AND report_type = 'DCR'", (code,)))["d"], None),
        "open_rfis": _safe(lambda: int(_one(conn.execute(
            "SELECT COUNT(1) AS n FROM rfi_log WHERE project_code = ? AND LOWER(COALESCE(status,'open')) NOT IN ('closed','answered','responded')",
            (code,)))["n"]), 0),
        "photos": _safe(lambda: int(_one(conn.execute("SELECT COUNT(1) AS n FROM field_photos WHERE project_code = ?", (code,)))["n"]), 0),
        "documents": _safe(lambda: int(_one(conn.execute(
            "SELECT COUNT(1) AS n FROM project_documents WHERE project_code = ? AND COALESCE(superseded,0) = 0", (code,)))["n"]), 0),
    }
    return {
        "project": p, "client": ({"id": client["id"], "name": client["name"]} if client else None), "contacts": contacts,
        "pms": _pms(conn, [code]).get(code, []), "crew": crew, "building": building, "contract": _contract(conn, code),
        "money": money_d, "aia": aia_d, "package": package_d, "documents_expiring": docs, "activity": activity,
        "links": {"dashboard": f"/projects/{code}", "crew_and_site": "/admin/projects",
                  "building": f"#buildings/{p['building_id']}" if p.get("building_id") else None,
                  "client": f"#clients/{p['client_org_id']}" if p.get("client_org_id") else None},
    }


# ---------------------------------------------------------------- routes
def _api_list():
    conn = _conn()
    try:
        rows = list_projects(conn, status=(request.args.get("status") or "active").strip().lower(),
                             search=(request.args.get("search") or "").strip() or None)
        return jsonify({"data": rows, "meta": {"count": len(rows)}})
    finally:
        conn.close()


def _api_detail(project_code):
    conn = _conn()
    try:
        d = project_detail(conn, project_code)
        return jsonify({"data": d}) if d else (jsonify({"error": "project not found"}), 404)
    finally:
        conn.close()


def register(app) -> None:
    import company_portal
    gate = requires_section(company_portal.SECTION)
    app.add_url_rule("/api/company/portal/projects", "portal_projects_list", gate(_api_list), methods=["GET"])
    app.add_url_rule("/api/company/portal/projects/<project_code>", "portal_projects_detail", gate(_api_detail), methods=["GET"])
