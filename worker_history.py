"""#320 — Worker job history + payroll view.

ROADMAP Phase C: on each worker, the projects worked (from `sign_in_log` +
`project_assignments`), hours by project and by month, certifications, the
Certificate of Fitness credentials issued, and the rate history
(`worker_rates`, with `labor_rate_change` as the approval audit). One read model,
one endpoint: `GET /api/company/portal/workers/<employee_id>?months=12`.

Money here is an ESTIMATE of labor cost (worked hours × the rate effective on
each work date — the same rule as the Finance view's labor cost), never payroll.
Hours are "worked", never "paid" (CLAUDE.md terminology rule).

Comp data: this is a COMPANY-ONLY surface (Company Portal, admin / c_suite), per
the compensation governance rule. Nothing here is reachable from the project
dashboard, the worker app, or any client surface. No `*_path` columns are
selected — the card / photo / folder paths on `employees` and `cof_cards` never
enter a payload.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from flask import jsonify, request

import db_layer
from payroll_hours import compute_worked_hours

log = logging.getLogger("ssc.worker_history")

MONTHS_DEFAULT = 12
MONTHS_MAX = 36


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


def _err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def _wrap(data, **meta):
    from datetime import datetime
    m = {"generated_at": datetime.now().isoformat(timespec="seconds")}
    m.update(meta)
    return jsonify({"data": data, "meta": m})


def _days_until(iso, today: date) -> Optional[int]:
    if not iso:
        return None
    try:
        return (date.fromisoformat(str(iso)[:10]) - today).days
    except ValueError:
        return None


def _month_labels(today: date, months: int) -> list:
    """['YYYY-MM', ...] oldest → newest, `months` long, ending with today's month."""
    out = []
    y, m = today.year, today.month
    for _ in range(months):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def _try_rows(conn, sql: str, params=()) -> list:
    """A table that a fresh preprod DB may not have yet must not break the page."""
    try:
        return _rows(conn.execute(sql, params))
    except Exception as e:
        log.info("#320 optional source skipped: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return []


def worker_detail(conn, employee_id: str, *, months: int = MONTHS_DEFAULT, today: Optional[date] = None) -> Optional[dict]:
    today = today or date.today()
    t = today.isoformat()
    months = max(1, min(int(MONTHS_DEFAULT if months is None else months), MONTHS_MAX))
    labels = _month_labels(today, months)
    since = labels[0] + "-01"
    year_start = t[:4] + "-01-01"
    in30 = (today + timedelta(days=30)).isoformat()

    w = _one(conn.execute(
        "SELECT e.employee_id, e.worker_id, e.name, e.trade, e.archived_at, e.created_at, "
        "       s.current_rate, s.status AS labor_status, s.effective_date AS rate_effective_date "
        "FROM employees e LEFT JOIN labor_worker_state s ON s.employee_id = e.employee_id "
        "WHERE e.employee_id = ?", (employee_id,)))
    if not w:
        return None
    w["labor_status"] = w.get("labor_status") or "active"
    w["current_rate"] = _num(w.get("current_rate")) or None
    w["status"] = "archived" if w.get("archived_at") else w["labor_status"]

    # ---- every sign-in (window = the months shown; totals for YTD / all time come from the full log)
    from company_portal import _rate_on
    state_rates = {employee_id: {"current_rate": w["current_rate"]}}
    cache: dict = {}
    log_rows = _rows(conn.execute(
        "SELECT l.date, l.project_code, l.time_in, l.time_out, p.name AS project_name, p.status AS project_status "
        "FROM sign_in_log l LEFT JOIN projects p ON p.project_code = l.project_code "
        "WHERE l.employee_id = ? ORDER BY l.date, l.id", (employee_id,)))
    by_project: dict = {}
    by_month: dict = {ym: {"month": ym, "hours": 0.0, "days": 0, "est_cost": 0.0, "unrated_hours": 0.0} for ym in labels}
    matrix: dict = {}
    days_seen: dict = {}
    hours_all = hours_ytd = hours_window = cost_window = unrated_window = 0.0
    days_window: set = set()
    first_on_site = last_on_site = None
    recent = []
    for r in log_rows:
        h = compute_worked_hours(r.get("time_in"), r.get("time_out"))
        d = r["date"]
        first_on_site = first_on_site or d
        last_on_site = d
        hours_all += h
        if d >= year_start:
            hours_ytd += h
        rate = _rate_on(conn, employee_id, d, state_rates, cache)
        cost = round(h * rate, 2) if rate > 0 else 0.0
        code = r["project_code"]
        p = by_project.setdefault(code, {"project_code": code, "project_name": r.get("project_name"),
                                         "project_status": r.get("project_status"), "hours": 0.0, "days": 0,
                                         "first_date": d, "last_date": d, "est_cost": 0.0, "unrated_hours": 0.0})
        p["hours"] += h
        p["last_date"] = d
        p["est_cost"] += cost
        if rate <= 0:
            p["unrated_hours"] += h
        if (code, d) not in days_seen:
            days_seen[(code, d)] = True
            p["days"] += 1
        ym = d[:7]
        if d >= since:
            hours_window += h
            cost_window += cost
            days_window.add(d)
            if rate <= 0:
                unrated_window += h
            if ym in by_month:
                by_month[ym]["hours"] += h
                by_month[ym]["est_cost"] += cost
                if rate <= 0:
                    by_month[ym]["unrated_hours"] += h
                matrix.setdefault(code, {}).setdefault(ym, 0.0)
                matrix[code][ym] += h
        if d >= (today - timedelta(days=30)).isoformat():
            recent.append({"date": d, "project_code": code, "time_in": r.get("time_in"), "time_out": r.get("time_out"),
                           "hours": round(h, 2), "rate": rate or None, "est_cost": cost or None})
    # days per month (distinct dates)
    month_days: dict = {}
    for d in days_window:
        month_days[d[:7]] = month_days.get(d[:7], 0) + 1
    for ym, b in by_month.items():
        b["days"] = month_days.get(ym, 0)
        b["hours"] = round(b["hours"], 2)
        b["est_cost"] = round(b["est_cost"], 2)
        b["unrated_hours"] = round(b["unrated_hours"], 2)

    # ---- assignments (roles + dates), merged into the project rows
    assignments = _try_rows(conn,
        "SELECT a.project_code, a.role_on_project, a.start_date, a.end_date, a.status, p.name AS project_name, "
        "       p.status AS project_status FROM project_assignments a LEFT JOIN projects p ON p.project_code = a.project_code "
        "WHERE a.employee_id = ? ORDER BY COALESCE(a.start_date, ''), a.id", (employee_id,))
    for a in assignments:
        p = by_project.setdefault(a["project_code"], {"project_code": a["project_code"], "project_name": a.get("project_name"),
                                                      "project_status": a.get("project_status"), "hours": 0.0, "days": 0,
                                                      "first_date": None, "last_date": None, "est_cost": 0.0, "unrated_hours": 0.0})
        p["assignment_status"] = a.get("status")
        p["role_on_project"] = a.get("role_on_project")
        p["assigned_from"] = a.get("start_date")
        p["assigned_to"] = a.get("end_date")
    projects = sorted(by_project.values(), key=lambda p: (p.get("last_date") or "", p["project_code"]), reverse=True)
    for p in projects:
        p["hours"] = round(p["hours"], 2)
        p["est_cost"] = round(p["est_cost"], 2)
        p["unrated_hours"] = round(p["unrated_hours"], 2)
        p.setdefault("assignment_status", None)
        p.setdefault("role_on_project", None)
        p.setdefault("assigned_from", None)
        p.setdefault("assigned_to", None)
    matrix_rows = []
    for code, cells in matrix.items():
        row = [round(cells.get(ym, 0.0), 2) for ym in labels]
        matrix_rows.append({"project_code": code, "project_name": by_project[code].get("project_name"),
                            "cells": row, "total": round(sum(row), 2)})
    matrix_rows.sort(key=lambda r: -r["total"])

    # ---- certifications + credentials + rates
    certs = _rows(conn.execute(
        "SELECT c.id, c.cert_type_id, ct.name, c.date_obtained, c.expiration_date, c.status "
        "FROM certifications c LEFT JOIN cert_types ct ON ct.cert_type_id = c.cert_type_id "
        "WHERE c.employee_id = ? ORDER BY COALESCE(c.expiration_date, '9999'), ct.name", (employee_id,)))
    for c in certs:
        c["days_left"] = _days_until(c.get("expiration_date"), today)
        c["state"] = ("expired" if c["days_left"] is not None and c["days_left"] < 0
                      else "expiring" if c["days_left"] is not None and c["days_left"] <= 30 else "valid")
    credentials = _try_rows(conn,
        "SELECT card_id, issued_date, expires_date, status, issued_by, issuer_license FROM cof_cards "
        "WHERE employee_id = ? ORDER BY issued_date DESC", (employee_id,))
    for c in credentials:
        c["days_left"] = _days_until(c.get("expires_date"), today)
    rate_history = _try_rows(conn,
        "SELECT hourly_rate, effective_from, effective_to, notes FROM worker_rates WHERE employee_id = ? "
        "ORDER BY effective_from DESC, id DESC", (employee_id,))
    for r in rate_history:
        r["hourly_rate"] = _num(r.get("hourly_rate"))
        r["current"] = r.get("effective_to") in (None, "") and (r.get("effective_from") or "") <= t
    rate_changes = _try_rows(conn,
        "SELECT id, old_rate, new_rate, effective_date, status, is_initial, submitted_at, decided_at, note "
        "FROM labor_rate_change WHERE employee_id = ? OR worker_id = ? ORDER BY COALESCE(submitted_at, ''), id",
        (employee_id, w.get("worker_id")))
    corrections = _try_rows(conn,
        "SELECT COUNT(1) AS n FROM sign_in_edit WHERE employee_id = ?", (employee_id,))

    return {
        "worker": w,
        "window": {"months": months, "labels": labels, "since": since},
        "summary": {
            "hours_all_time": round(hours_all, 2), "hours_ytd": round(hours_ytd, 2),
            "hours_window": round(hours_window, 2), "days_window": len(days_window),
            "est_cost_window": round(cost_window, 2), "unrated_hours_window": round(unrated_window, 2),
            "projects_total": len(projects), "projects_active": sum(1 for p in projects if p.get("assignment_status") == "active"),
            "first_on_site": first_on_site, "last_on_site": last_on_site,
            "certs_total": len(certs), "certs_expired": sum(1 for c in certs if c["state"] == "expired"),
            "certs_expiring_30": sum(1 for c in certs if c["state"] == "expiring"),
            "credentials_issued": sum(1 for c in credentials if c.get("status") == "issued"),
            "pending_rate_changes": sum(1 for c in rate_changes if c.get("status") == "pending"),
            "hours_corrections": int((corrections[0]["n"] if corrections else 0) or 0),
        },
        "projects": projects,
        "by_month": [by_month[ym] for ym in labels],
        "matrix": {"months": labels, "rows": matrix_rows},
        "recent_days": list(reversed(recent)),
        "certifications": certs,
        "credentials": credentials,
        "rate_history": rate_history,
        "rate_changes": rate_changes,
    }


def register_routes(app, gate) -> None:
    @app.route("/api/company/portal/workers/<employee_id>", methods=["GET"])
    @gate
    def company_portal_worker_detail(employee_id):
        try:
            months = int(request.args.get("months") or MONTHS_DEFAULT)
        except ValueError:
            return _err("months must be an integer")
        conn = db_layer.connect()
        try:
            d = worker_detail(conn, employee_id, months=months)
        finally:
            conn.close()
        if d is None:
            return _err("worker not found", 404)
        return _wrap(d)
