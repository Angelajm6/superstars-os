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
  * 2026-11-19 (calendar, easier to scan): each job says whether weather can stop it
    (`weather_risk`: wind for rope access / suspended scaffold / boom lift, rain for exterior
    work), the weather is ONE New York City forecast with wind (`weather()`), and the
    calendar read carries the open tasks that are already overdue (`overdue`).

Company-portal section (admin / c_suite). Read-only. No PII: names of jobs, clients and
PMs only.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from flask import jsonify, request

import db_layer
from access import PM_ROLES_SQL
from auth import current_user, requires_section

log = logging.getLogger("ssc.portal_calendar")

MAX_DAYS = 120
PALETTE = 8                  # the old hash colors; saved colors run 0..43 (project_setup.PALETTE)
WEATHER_MAX_DAYS = 45          # a month view (6 weeks); days outside the forecast just come back empty
WEATHER_PAST_DAYS = 31         # the forecast service keeps recent days; older ones are left out
WEATHER_AHEAD_DAYS = 15        # ... and forecasts 16 days, today included
NYC = (40.8083, -73.9162)      # the weather on the calendar is New York City's (the company's home borough)
DEFAULT_SITE = NYC
WIND_METHODS = ("rope_access", "suspended_scaffold", "boom_lift")   # work that stops in high wind
OUTDOOR_TYPES = ("FR", "IRA")  # exterior restoration and rope-access jobs stop in the rain (by the job code's type)
WIND_MPH, GUST_MPH, RAIN_PCT = 20, 30, 60   # the thresholds the calendar warns at



def _qb_hide() -> str:
    """#316 1b: invoices that came from QuickBooks are for admins only."""
    try:
        import qbo
        return qbo.hide_sql("i")
    except Exception:
        return ""

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


def job_type(code: str) -> str:
    """The type part of a job code: FR-BX-001 -> FR, IRA-MN-002 -> IRA."""
    return (str(code or "").split("-")[0] or "").upper()


def weather_risk(code: str, methods: list) -> dict:
    """What weather can stop this job: wind (rope access, suspended scaffold, boom lift, or a rope-access
    job) and rain (exterior work). Interiors and garages are never warned."""
    t = job_type(code)
    wind = bool(set(methods or []) & set(WIND_METHODS)) or t == "IRA"
    labels = []
    try:
        import project_setup
        labels = [project_setup.ACCESS_LABELS[m] for m in (methods or []) if m in project_setup.ACCESS_LABELS]
    except Exception:
        pass
    return {"wind": wind, "rain": t in OUTDOOR_TYPES, "access": labels}


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
    colors = {}   # Oct 7 notes: each job's own saved color (project_setup); the old hash until it has one
    try:
        if codes:
            colors = {r["project_code"]: int(r["color_index"]) for r in conn.execute(
                f"SELECT project_code, color_index FROM projects WHERE project_code IN ({marks}) AND color_index IS NOT NULL", codes).fetchall()}
    except Exception:
        conn.rollback()
    access: dict = {}   # access methods (project_setup), for the weather warnings
    try:
        if codes:
            import project_setup
            access = {r["project_code"]: (project_setup.access_methods(r) or []) for r in _rows(conn.execute(
                f"SELECT project_code, access_methods FROM projects WHERE project_code IN ({marks})", codes))}
    except Exception:
        conn.rollback()
    pms: dict = {}
    if codes:
        try:
            for r in _rows(conn.execute(
                    f"SELECT a.project_code, COALESCE(u.display_name, u.full_name, u.email) AS name FROM pm_project_assignment a "
                    f"JOIN users u ON u.id = a.user_id WHERE a.project_code IN ({marks}) AND u.role IN {PM_ROLES_SQL} "
                    f"AND COALESCE(u.is_active, 1) = 1 AND COALESCE(u.status, 'active') = 'active' "
                    f"ORDER BY LOWER(COALESCE(u.display_name, u.full_name, u.email))", codes)):
                pms.setdefault(r["project_code"], []).append(r["name"])
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
            "color": colors.get(r["project_code"], color_index(r["project_code"])), "today_on": start_d <= today and (open_ended or end_d >= today),
            "weather_risk": weather_risk(r["project_code"], access.get(r["project_code"]) or []),
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
                "WHERE i.status = 'issued' AND i.due_at >= ? AND i.due_at <= ?" + _qb_hide() + " ORDER BY i.due_at", (s, e))):
            when = str(r["due_at"])[:10]
            day(when)["milestones"].append({
                "kind": "invoice", "id": r["id"], "number": r["number"], "project_code": r.get("project_code"),
                "client_name": r.get("client_name"), "balance": round(float(r["amount"] or 0) - float(r["amount_paid"] or 0), 2),
                "label": f"{r['number']} due", "date": when, "late": when < today})
    except Exception as ex:
        log.info("#333 invoice milestones skipped: %s", ex)
    return {"range": {"from": s, "to": e, "today": today}, "jobs": jobs, "undated": undated, "days": days}


_WX_CACHE: dict = {}
_WX_TTL = 30 * 60


def _get_json(url: str) -> dict:
    """The forecast service (Open-Meteo, no key). One place, so tests swap it out: no network in CI."""
    import json
    import urllib.request
    with urllib.request.urlopen(url, timeout=6) as resp:
        return json.loads(resp.read().decode("utf-8"))


def weather(conn, start: date, end: date, get=None, today: Optional[date] = None) -> dict:
    """New York City's weather per day for [start, end], in ONE call: high / low, the condition, the chance
    of rain and the day's strongest wind and gusts (mph). Only the days the service covers come back
    (the last month and the next 16 days); a failure returns no days instead of breaking the calendar.
    `conn` is unused (kept for callers); the coordinates are always NYC's, never a job's address."""
    if end < start:
        raise ValueError("the window ends before it starts")
    if (end - start).days + 1 > WEATHER_MAX_DAYS:
        raise ValueError(f"weather covers at most {WEATHER_MAX_DAYS} days")
    today = today or date.today()
    lo, hi = max(start, today - timedelta(days=WEATHER_PAST_DAYS)), min(end, today + timedelta(days=WEATHER_AHEAD_DAYS))
    lat, lng = NYC
    out: dict = {}
    if lo <= hi:
        key = (lo.isoformat(), hi.isoformat())
        hit = _WX_CACHE.get(key)
        import time
        if get is None and hit and time.time() - hit[0] < _WX_TTL:
            out = hit[1]
        else:
            import urllib.parse
            url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
                "latitude": lat, "longitude": lng, "start_date": lo.isoformat(), "end_date": hi.isoformat(),
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                         "wind_speed_10m_max,wind_gusts_10m_max",
                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "America/New_York"})
            try:
                labels = {}
                try:
                    import server
                    labels = server._WMO_LABELS
                except Exception:
                    pass
                daily = ((get or _get_json)(url) or {}).get("daily") or {}

                def col(name, i):
                    v = daily.get(name) or []
                    return v[i] if i < len(v) else None
                for i, d in enumerate(daily.get("time") or []):
                    code = col("weather_code", i)
                    out[str(d)[:10]] = {
                        "code": code, "label": labels.get(code, "—") if code is not None else "—",
                        "temp_max": col("temperature_2m_max", i), "temp_min": col("temperature_2m_min", i),
                        "precip_prob": col("precipitation_probability_max", i),
                        "wind_max": col("wind_speed_10m_max", i), "gust_max": col("wind_gusts_10m_max", i)}
                if get is None:
                    _WX_CACHE[key] = (time.time(), out)
            except Exception as ex:
                log.info("calendar weather unavailable: %s", type(ex).__name__)
                out = {}
    return {"site": {"lat": lat, "lng": lng, "name": "New York City"}, "days": out,
            "thresholds": {"wind_mph": WIND_MPH, "gust_mph": GUST_MPH, "rain_pct": RAIN_PCT}}


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
            data = window(conn, start, end)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:   # say what broke instead of a bare 500 (#334: a missing column on preprod)
            log.exception("#332 calendar failed")
            return jsonify({"error": f"calendar failed: {type(e).__name__}: {str(e)[:160]}"}), 500
        # team tasks (by deadline) and events (by their days), open and done, on the same calendar
        try:
            import team_items
            u = current_user() or {}
            data["items"] = team_items.list_items(conn, status="all", date_from=data["range"]["from"], date_to=data["range"]["to"],
                                                  me=u.get("id"), can_edit=u.get("role") in team_items.EDIT_ROLES)
        except Exception as ex:
            log.warning("calendar items skipped: %s", type(ex).__name__)
            data["items"] = []
        # open tasks already past their deadline (any date before today), so today can say so
        data["overdue"] = []
        try:
            import team_items
            u = current_user() or {}
            y = (date.today() - timedelta(days=1)).isoformat()
            data["overdue"] = [it for it in team_items.list_items(conn, status="open", kind="task", date_from="1900-01-01", date_to=y,
                                                                    me=u.get("id"), can_edit=u.get("role") in team_items.EDIT_ROLES)
                               if it.get("overdue")][:50]
        except Exception as ex:
            log.warning("calendar overdue skipped: %s", type(ex).__name__)
            try:
                conn.rollback()
            except Exception:
                pass
        # the signed-in person's OWN Google events (private: calendar_import.events_for is the only reader,
        # and it gets this user's id only); drawn as "only you" chips, never anyone else's
        data["mine"], data["mine_connected"] = [], False
        try:
            import calendar_import
            import gcal_sync
            u = current_user() or {}
            if u.get("id"):
                data["mine"] = calendar_import.events_for(conn, u["id"], data["range"]["from"], data["range"]["to"])
                data["mine_connected"] = bool(data["mine"]) or (not gcal_sync.unavailable_reason()
                                                                and calendar_import.can_read(gcal_sync._link(conn, u["id"])))
        except Exception as ex:
            log.warning("calendar own events skipped: %s", type(ex).__name__)
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"data": data})
    finally:
        conn.close()


def register(app) -> None:
    import company_portal
    gate = requires_section(company_portal.SECTION)
    app.add_url_rule("/api/company/portal/calendar", "portal_calendar", gate(_api_calendar), methods=["GET"])
    app.add_url_rule("/api/company/portal/calendar/weather", "portal_calendar_weather", gate(_api_weather), methods=["GET"])   # #333
