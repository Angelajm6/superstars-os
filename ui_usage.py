"""Usage & job tools — step 1 of the job page redesign ("measure first").

The server logs cannot tell which tabs people open (the daily report loads on every visit and
the job dashboard refreshes every minute), so the pages send one tiny beacon when a tab / view
is shown. Only the surface, the view name, the role and the job are kept, counted per day:
never who, never content. Plus a per-job "data footprint": which optional tools actually hold
data on each job. Both feed the redesign's job types (step 3) with real numbers.

  POST /api/usage/view                  {surface: job|company|home, view, project_code?} — any
                                        signed-in team member; unknown names are ignored (204)
  GET  /api/admin/usage?days=14         company roles — opens per view, by role, jobs, last day
  GET  /api/admin/projects/footprint    company roles — per active job: rows per tool + tabs on

Table ui_view_usage(day, surface, view, role, project_code, opens), one row per combination
per day (idempotent ensure, no CHECK constraints).
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Optional

from flask import jsonify, request

import db_layer
from auth import current_user, requires_company

log = logging.getLogger("ssc.usage")

ROLES = ("admin", "c_suite", "pm", "super")
SURFACES = ("job", "company", "home")
# the job dashboard's views (data-view values) and the company portal's routes
JOB_VIEWS = ("dashboard", "contacts", "employees", "drawings", "documents", "field-photos", "drop-report", "drop-plans",
             "schedule", "daily-report", "weekly-summary", "materials", "toolbox-talks", "rfis", "violations", "compliance",
             "sov", "payments", "expenses", "product-usage", "templates", "settings")
COMPANY_VIEWS = ("overview", "clients", "portal", "calendar", "projects", "proposals", "finance", "expenses", "workforce",
                 "company", "usage")
HOME_VIEWS = ("home", "home-week", "home-month")
ALLOWED = {"job": JOB_VIEWS, "company": COMPANY_VIEWS, "home": HOME_VIEWS}
CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")

# tool -> (label, count query on one job). Every table outside the base schema may be
# missing on a given database, so each count is guarded.
TOOLS = (
    ("crew", "Crew", "SELECT COUNT(*) FROM project_assignments WHERE project_code = ? AND COALESCE(status, 'active') = 'active'"),
    ("drawings", "Drawings", "SELECT (SELECT COUNT(*) FROM elevation WHERE project_code = ?) + (SELECT COUNT(*) FROM drawing_set WHERE project_code = ?)"),
    ("drops", "Drops", "SELECT COUNT(*) FROM drops WHERE project_code = ?"),
    ("materials", "Materials", "SELECT (SELECT COUNT(*) FROM material WHERE project_code = ?) + (SELECT COUNT(*) FROM expected_delivery WHERE project_code = ?)"),
    ("safety", "Toolbox talks", "SELECT COUNT(*) FROM toolbox_talk_records WHERE project_code = ?"),
    ("rfis", "RFIs", "SELECT COUNT(*) FROM rfi_log WHERE project_code = ?"),
    ("compliance", "Permits & violations", "SELECT (SELECT COUNT(*) FROM dob_permits WHERE project_code = ?) + (SELECT COUNT(*) FROM dob_violations WHERE project_code = ?) + (SELECT COUNT(*) FROM dob_complaints WHERE project_code = ?)"),
    ("daily", "Daily reports", "SELECT COUNT(*) FROM report_index WHERE project_code = ? AND report_type = 'DCR'"),
    ("documents", "Documents", "SELECT COUNT(*) FROM project_documents WHERE project_code = ? AND COALESCE(superseded, 0) = 0"),
    ("photos", "Photos", "SELECT COUNT(*) FROM field_photos WHERE project_code = ?"),
    ("payments", "Invoices", "SELECT COUNT(*) FROM invoice WHERE project_code = ? AND COALESCE(status, '') <> 'void'"),
    ("expenses", "Expenses", "SELECT COUNT(*) FROM expenses WHERE project_code = ?"),
    ("sov", "Schedule of values", "SELECT COUNT(*) FROM billing_sov WHERE project_code = ?"),
)
OPTIONAL = ("crew", "drawings", "drops", "materials", "safety", "rfis", "compliance")   # project_modules switches (weekly/templates have no per-job rows)


def ensure(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS ui_view_usage (day TEXT NOT NULL, surface TEXT NOT NULL, view TEXT NOT NULL, "
                 "role TEXT NOT NULL, project_code TEXT NOT NULL, opens INTEGER NOT NULL)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ui_view_usage ON ui_view_usage(day, surface, view, role, project_code)")
    conn.commit()


def record(conn, surface: str, view: str, role: str, project_code: str = "") -> bool:
    if surface not in ALLOWED or view not in ALLOWED[surface] or role not in ROLES:
        return False
    code = project_code if (surface == "job" and project_code and CODE.match(project_code)) else ""
    day = date.today().isoformat()
    key = (day, surface, view, role, code)
    cur = conn.execute("UPDATE ui_view_usage SET opens = opens + 1 WHERE day = ? AND surface = ? AND view = ? AND role = ? AND project_code = ?", key)
    if not getattr(cur, "rowcount", 0):
        try:
            conn.execute("INSERT INTO ui_view_usage (day, surface, view, role, project_code, opens) VALUES (?, ?, ?, ?, ?, 1)", key)
        except Exception:   # two beacons raced on a new row: count it on the row the other one made
            conn.rollback()
            conn.execute("UPDATE ui_view_usage SET opens = opens + 1 WHERE day = ? AND surface = ? AND view = ? AND role = ? AND project_code = ?", key)
    conn.commit()
    return True


def report(conn, days: int) -> dict:
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = [dict(r) for r in conn.execute(
        "SELECT surface, view, role, project_code, day, opens FROM ui_view_usage WHERE day >= ?", (since,)).fetchall()]
    out = {}
    for s, views in ALLOWED.items():
        for v in views:
            out[(s, v)] = {"surface": s, "view": v, "opens": 0, "by_role": {r: 0 for r in ROLES}, "jobs": set(), "days": set(), "last_day": None}
    for r in rows:
        k = (r["surface"], r["view"])
        if k not in out:
            continue
        o = out[k]
        o["opens"] += r["opens"]
        o["by_role"][r["role"]] = o["by_role"].get(r["role"], 0) + r["opens"]
        if r["project_code"]:
            o["jobs"].add(r["project_code"])
        o["days"].add(r["day"])
        o["last_day"] = max(o["last_day"] or "", r["day"])
    views = []
    for o in out.values():
        views.append({**o, "jobs": len(o["jobs"]), "days": len(o["days"])})
    order = {"job": 0, "company": 1, "home": 2}   # the job page first: it is what the redesign is about
    views.sort(key=lambda o: (order[o["surface"]], -o["opens"], o["view"]))
    first = conn.execute("SELECT MIN(day) FROM ui_view_usage").fetchone()[0]
    return {"since": since, "days": days, "counting_since": first, "views": views}


def _count(conn, sql: str, code: str) -> Optional[int]:
    try:
        return int(conn.execute(sql, (code,) * sql.count("?")).fetchone()[0] or 0)
    except Exception:
        try:
            conn.rollback()   # Postgres needs this after a failed statement
        except Exception:
            pass
        return None   # the table does not exist on this database


def footprint(conn) -> dict:
    import project_modules
    jobs = [dict(r) for r in conn.execute(
        "SELECT project_code, name, enabled_modules FROM projects WHERE COALESCE(status, 'active') = 'active' ORDER BY name, project_code").fetchall()]
    out = []
    for j in jobs:
        counts = {key: _count(conn, sql, j["project_code"]) for key, _, sql in TOOLS}
        enabled = project_modules._parse(j.get("enabled_modules"))
        out.append({"project_code": j["project_code"], "name": j.get("name") or j["project_code"], "counts": counts,
                    "modules_on": list(project_modules.KEYS) if enabled is None else enabled,
                    "customized": enabled is not None})
    return {"tools": [{"key": k, "label": label, "optional": k in OPTIONAL} for k, label, _ in TOOLS],
            "module_labels": {m["key"]: m["label"] for m in project_modules.MODULES}, "jobs": out}


# ------------------------------------------------------------------ routes
def _api_view():
    u = current_user() or {}
    body = request.get_json(silent=True, force=True) or {}
    if u.get("role") not in ROLES:
        return ("", 204)
    conn = db_layer.connect()
    try:
        record(conn, str(body.get("surface") or ""), str(body.get("view") or ""), u["role"], str(body.get("project_code") or ""))
    except Exception as e:
        log.info("usage beacon not counted: %s", type(e).__name__)
    finally:
        conn.close()
    return ("", 204)


@requires_company
def _api_report():
    try:
        days = max(1, min(int(request.args.get("days") or 14), 180))
    except ValueError:
        days = 14
    conn = db_layer.connect()
    try:
        return jsonify({"data": report(conn, days)})
    finally:
        conn.close()


@requires_company
def _api_footprint():
    conn = db_layer.connect()
    try:
        return jsonify({"data": footprint(conn)})
    finally:
        conn.close()


def register(app) -> None:
    conn = db_layer.connect()
    try:
        ensure(conn)
    except Exception as e:
        log.warning("usage schema ensure skipped: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    app.add_url_rule("/api/usage/view", "usage_view", _api_view, methods=["POST"])
    app.add_url_rule("/api/admin/usage", "usage_report", _api_report, methods=["GET"])
    app.add_url_rule("/api/admin/projects/footprint", "projects_footprint", _api_footprint, methods=["GET"])
