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
WEATHER_MAX_DAYS = 14          # #333 — the strip is for the 3-day / week views only
DEFAULT_SITE = (40.8083, -73.9162)   # the company's home borough when no job has coordinates


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
        "WHERE p.start_date IS NOT NULL ORDER BY p.start_date, p.project_code"))

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
    odd = []          # rows whose dates cannot be read: reported as undated, never a 500
    for r in rows:
        start_d = str(r["start_date"] or "").strip()[:10]
        try:
            date.fromisoformat(start_d)
            end_d = _iso(r.get("target_completion")) if str(r.get("target_completion") or "").strip() else None
        except ValueError:
            odd.append({"project_code": r["project_code"], "name": r.get("name")})
            continue
        if start_d > e:
            continue
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
    undated = [r for r in _rows(conn.execute(
        "SELECT project_code, name, start_date FROM projects WHERE COALESCE(status,'active') = 'active' ORDER BY project_code"))
        if not str(r.get("start_date") or "").strip()]
    undated = [{"project_code": r["project_code"], "name": r.get("name")} for r in undated] + [
        o for o in odd if o["project_code"] not in {u["project_code"] for u in undated}]
    days: dict = {}

    def day(d: str) -> dict:
        return days.setdefault(d, {"reports": [], "on_site": {}, "headcount": 0, "milestones": []})
    try:
        for r in _rows(conn.execute(
                "SELECT report_date, project_code FROM report_index WHERE report_type = 'DCR' AND report_date >= ? AND report_date <= ? "
                "ORDER BY report_date", (s, e))):
            d = day(str(r["report_date"])[:10])
            if r["project_code"] not in d["reports"]:
                d["reports"].append(r["project_code"])
    except Exception as ex:
        log.info("#332 report dots skipped: %s", ex)
    # #333 — who was actually on site: distinct workers signed in per job per day
    try:
        for r in _rows(conn.execute(
                "SELECT date, project_code, COUNT(DISTINCT employee_id) AS n FROM sign_in_log "
                "WHERE date >= ? AND date <= ? GROUP BY date, project_code", (s, e))):
            d = day(str(r["date"])[:10])
            d["on_site"][r["project_code"]] = int(r["n"])
            d["headcount"] += int(r["n"])
    except Exception as ex:
        log.info("#333 headcount skipped: %s", ex)
    # #333 — milestones: documents that expire and client invoices that fall due
    try:
        for r in _rows(conn.execute(
                "SELECT d.id, d.project_code, d.category, d.requirement_key, d.title, d.expiry_date, p.name AS project_name "
                "FROM project_documents d LEFT JOIN projects p ON p.project_code = d.project_code "
                "WHERE COALESCE(d.superseded,0) = 0 AND d.expiry_date >= ? AND d.expiry_date <= ? ORDER BY d.expiry_date", (s, e))):
            when = str(r["expiry_date"])[:10]
            day(when)["milestones"].append({
                "kind": "document", "id": r["id"], "project_code": r["project_code"], "project_name": r.get("project_name"),
                "label": r.get("title") or (r.get("requirement_key") or r.get("category") or "document"),
                "category": r.get("category"), "date": when, "late": when < today})
    except Exception as ex:
        log.info("#333 document milestones skipped: %s", ex)
    try:
        for r in _rows(conn.execute(
                "SELECT i.id, i.number, i.project_code, i.due_at, i.amount, i.amount_paid, o.name AS client_name "
                "FROM invoice i LEFT JOIN crm_organization o ON o.id = i.client_org_id "
                "WHERE i.status = 'issued' AND i.due_at >= ? AND i.due_at <= ? ORDER BY i.due_at", (s, e))):
            when = str(r["due_at"])[:10]
            day(when)["milestones"].append({
                "kind": "invoice", "id": r["id"], "number": r["number"], "project_code": r.get("project_code"),
                "client_name": r.get("client_name"), "balance": round(float(r["amount"] or 0) - float(r["amount_paid"] or 0), 2),
                "label": f"{r['number']} due", "date": when, "late": when < today})
    except Exception as ex:
        log.info("#333 invoice milestones skipped: %s", ex)
    return {"range": {"from": s, "to": e, "today": today}, "jobs": jobs, "undated": undated, "days": days}


def _site(conn) -> tuple:
    """Coordinates for the weather strip: the one active job with a site, else the home borough."""
    try:
        rows = _rows(conn.execute(
            "SELECT site_lat, site_lng FROM projects WHERE COALESCE(status,'active') = 'active' AND site_lat IS NOT NULL "
            "AND site_lng IS NOT NULL ORDER BY start_date DESC LIMIT 1"))
        if rows:
            return float(rows[0]["site_lat"]), float(rows[0]["site_lng"])
    except Exception:
        pass
    return DEFAULT_SITE


def weather(conn, start: date, end: date, fetch=None) -> dict:
    """Per-day conditions for a short window (≤ 14 days), read through the same provider
    helper the DCR uses (cached 30 min per date; a provider failure just leaves a day out)."""
    if end < start:
        raise ValueError("the window ends before it starts")
    if (end - start).days + 1 > WEATHER_MAX_DAYS:
        raise ValueError(f"weather covers at most {WEATHER_MAX_DAYS} days")
    if fetch is None:
        import server
        fetch, labels = server.fetch_open_meteo_weather, server._WMO_LABELS
    else:
        labels = {}
        try:
            import server
            labels = server._WMO_LABELS
        except Exception:
            pass
    lat, lng = _site(conn)
    out = {}
    d = start
    while d <= end:
        try:
            w = fetch(lat, lng, d.isoformat()) or {}
            code = w.get("condition_code")
            out[d.isoformat()] = {"code": code, "label": labels.get(code, "—") if code is not None else "—",
                                  "temp_max": w.get("temp_max"), "temp_min": w.get("temp_min"),
                                  "precip_prob": w.get("precip_prob_today")}
        except Exception as ex:
            log.info("#333 weather skipped for %s: %s", d, ex)
        d += timedelta(days=1)
    return {"site": {"lat": lat, "lng": lng}, "days": out}


def _api_weather():
    conn = _conn()
    try:
        try:
            today = date.today()
            start = date.fromisoformat(_iso(request.args.get("from")) or today.isoformat())
            end = date.fromisoformat(_iso(request.args.get("to")) or (start + timedelta(days=6)).isoformat())
        except ValueError:
            return jsonify({"error": "dates must be YYYY-MM-DD"}), 400
        try:
            return jsonify({"data": weather(conn, start, end)})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    finally:
        conn.close()


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
        except Exception as e:   # say what broke instead of a bare 500 (#334: a missing column on preprod)
            log.exception("#332 calendar failed")
            return jsonify({"error": f"calendar failed: {type(e).__name__}: {str(e)[:160]}"}), 500
    finally:
        conn.close()


def register(app) -> None:
    import company_portal
    gate = requires_section(company_portal.SECTION)
    app.add_url_rule("/api/company/portal/calendar", "portal_calendar", gate(_api_calendar), methods=["GET"])
    app.add_url_rule("/api/company/portal/calendar/weather", "portal_calendar_weather", gate(_api_weather), methods=["GET"])   # #333
