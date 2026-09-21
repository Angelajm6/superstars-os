"""#332 — the jobs calendar: what is happening on which day, for the Company Portal.

One read: every job whose [start_date, end] overlaps the requested window, as bars.
  * `start_date` is set on onboarding ("+ New project"); `target_completion` is the end.
    An active job without an end is open-ended (drawn to the edge of the window); a
    closed job without an end ends the day it was last updated (when it was closed).
  * Jobs without a start date cannot be drawn; they come back in `undated` so the page
    can say so and point at Crew & site, where dates are set.
  * `days` carries the daily reports filed per date (project codes), so a day shows
    which jobs actually reported.
  * A job's colour is a stable function of its code, so it looks the same in every view.

Company-portal section (admin / c_suite). Read-only. No PII: names of jobs, clients and
PMs only.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from flask import jsonify, request

import db_layer
from auth import requires_section

log = logging.getLogger("ssc.portal_calendar")

MAX_DAYS = 120
PALETTE = 8


def _conn():
    return db_layer.connect(pragma_fk=True)


def _rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]


def _one(cur) -> Optional[dict]:
    r = cur.fetchone()
    return dict(r) if r else None


def _iso(v) -> Optional[str]:
    if v in (None, ""):
        return None
    s = str(v).strip()[:10]
    date.fromisoformat(s)
    return s


def color_index(code: str) -> int:
    return sum(ord(c) for c in (code or "")) % PALETTE


def window(conn, start: date, end: date) -> dict:
    """Jobs overlapping [start, end] inclusive, plus the daily reports filed in it."""
    if end < start:
        raise ValueError("the window ends before it starts")
    if (end - start).days + 1 > MAX_DAYS:
        raise ValueError(f"a window is at most {MAX_DAYS} days")
    s, e = start.isoformat(), end.isoformat()
    today = date.today().isoformat()
    rows = _rows(conn.execute(
        "SELECT p.project_code, p.name, p.address, p.city_zip, p.project_type, COALESCE(p.status,'active') AS status, "
        "       p.start_date, p.target_completion, p.updated_at, o.name AS client_name "
        "FROM projects p LEFT JOIN crm_organization o ON o.id = p.client_org_id "
        "WHERE p.start_date IS NOT NULL AND p.start_date <> '' AND p.start_date <= ? "
        "ORDER BY p.start_date, p.project_code", (e,)))
    codes = [r["project_code"] for r in rows]
    marks = ",".join("?" * len(codes)) if codes else "NULL"
    pms: dict = {}
    if codes:
        try:
            for r in _rows(conn.execute(
                    f"SELECT a.project_code, u.full_name FROM pm_project_assignment a JOIN users u ON u.id = a.user_id "
                    f"WHERE a.project_code IN ({marks}) ORDER BY u.full_name", codes)):
                pms.setdefault(r["project_code"], []).append(r["full_name"])
        except Exception as ex:
            log.info("#332 pm lookup skipped: %s", ex)
    crew: dict = {}
    if codes:
        try:
            crew = {r["project_code"]: int(r["n"]) for r in _rows(conn.execute(
                f"SELECT project_code, COUNT(1) AS n FROM project_assignments WHERE project_code IN ({marks}) "
                f"AND COALESCE(status,'active') = 'active' GROUP BY project_code", codes))}
        except Exception as ex:
            log.info("#332 crew lookup skipped: %s", ex)
    jobs = []
    for r in rows:
        start_d = str(r["start_date"])[:10]
        end_d = _iso(r.get("target_completion")) if r.get("target_completion") else None
        open_ended = False
        if not end_d:
            if r["status"] == "active":
                open_ended = True
            else:
                end_d = (str(r.get("updated_at") or "")[:10] or start_d)
                if end_d < start_d:
                    end_d = start_d
        # overlap test: an open-ended job overlaps anything after its start
        if not open_ended and end_d < s:
            continue
        jobs.append({
            "project_code": r["project_code"], "name": r.get("name") or r["project_code"], "address": r.get("address"),
            "client_name": r.get("client_name"), "status": r["status"], "project_type": r.get("project_type"),
            "start_date": start_d, "end_date": end_d, "open_ended": open_ended,
            "days": ((date.fromisoformat(end_d) - date.fromisoformat(start_d)).days + 1) if end_d else None,
            "pm": ", ".join(pms.get(r["project_code"], [])) or None, "crew_count": crew.get(r["project_code"], 0),
            "color": color_index(r["project_code"]), "today_on": start_d <= today and (open_ended or end_d >= today),
        })
    undated = _rows(conn.execute(
        "SELECT project_code, name FROM projects WHERE (start_date IS NULL OR start_date = '') "
        "AND COALESCE(status,'active') = 'active' ORDER BY project_code"))
    days: dict = {}
    try:
        for r in _rows(conn.execute(
                "SELECT report_date, project_code FROM report_index WHERE report_type = 'DCR' AND report_date >= ? AND report_date <= ? "
                "ORDER BY report_date", (s, e))):
            d = str(r["report_date"])[:10]
            days.setdefault(d, {"reports": []})
            if r["project_code"] not in days[d]["reports"]:
                days[d]["reports"].append(r["project_code"])
    except Exception as ex:
        log.info("#332 report dots skipped: %s", ex)
    return {"range": {"from": s, "to": e, "today": today}, "jobs": jobs, "undated": undated, "days": days}


def _api_calendar():
    conn = _conn()
    try:
        try:
            today = date.today()
            start = date.fromisoformat(_iso(request.args.get("from")) or (today - timedelta(days=today.weekday())).isoformat())
            end = date.fromisoformat(_iso(request.args.get("to")) or (start + timedelta(days=41)).isoformat())
        except ValueError:
            return jsonify({"error": "dates must be YYYY-MM-DD"}), 400
        try:
            return jsonify({"data": window(conn, start, end)})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    finally:
        conn.close()


def register(app) -> None:
    import company_portal
    gate = requires_section(company_portal.SECTION)
    app.add_url_rule("/api/company/portal/calendar", "portal_calendar", gate(_api_calendar), methods=["GET"])
