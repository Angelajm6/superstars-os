"""Per-job dashboard modules — a job only shows the tabs it needs.

Most jobs need a handful of tabs; only a big job (e.g. the first facade job) needs drop plans,
RFIs, toolbox talks and the rest. Optional tabs are grouped into MODULES that are switched on
per project.

  projects.enabled_modules  TEXT   JSON list of enabled optional-module keys, e.g. ["drops","rfis"]
                                   NULL = "never customized" -> EVERY module is on (all existing
                                   jobs keep their full dashboard until someone changes them)

CORE tabs are never switchable: Project Health, Contacts, Project Documents, Field Photos,
Daily Report, Schedule, Payments, Expenses and Settings. The Schedule of Values tab follows the
job's billing type (billing_type.py), not a module.

Lean jobs (accepted 2026-10-13, options A + B): a job created with "+ New project" starts with the
tabs ticked in its "Tabs for this job" step, pre-ticked from its job type's STARTING SET
(TYPE_DEFAULTS, editable by the office on Settings > Usage & job tools, stored in
app_settings['job_type_tabs']). Jobs created before this keep enabled_modules NULL = every module
on. admin / c_suite add a tab from the sidebar's "+ Add a tab" search, remove one with its ×, or
use the project's Settings tab; removing a tab never deletes its data. Hiding a tab is a
convenience, not access control: the endpoints behind it keep their own role / assignment checks.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from flask import jsonify, request

import db_layer
from auth import current_user, requires_company

log = logging.getLogger("ssc.project_modules")

# key, label, hint, dashboard views it owns (data-view values of the nav links)
MODULES = (
    {"key": "crew", "label": "Crew & certifications", "hint": "Employees & Certs", "views": ("employees",)},
    {"key": "drawings", "label": "Drawing markup", "hint": "Mark up drawings and elevations", "views": ("drawings",)},
    {"key": "drops", "label": "Drop plan & reports", "hint": "Drop Plan, Drop Report and the Complete tile on Project Health",
     "views": ("drop-report", "drop-plans")},
    {"key": "weekly", "label": "Weekly summary", "hint": "Weekly Summary", "views": ("weekly-summary",)},
    {"key": "materials", "label": "Materials & product usage", "hint": "Materials & Deliveries, Product Usage",
     "views": ("materials", "product-usage")},
    {"key": "safety", "label": "Toolbox talks", "hint": "Toolbox Talks", "views": ("toolbox-talks",)},
    {"key": "rfis", "label": "RFIs", "hint": "RFI Log", "views": ("rfis",)},
    {"key": "compliance", "label": "Violations & permits", "hint": "Violations, Compliance & Permits",
     "views": ("violations", "compliance")},
    {"key": "templates", "label": "Templates", "hint": "Blank forms and templates", "views": ("templates",)},
)
KEYS = tuple(m["key"] for m in MODULES)
VIEWS_BY_KEY = {m["key"]: m["views"] for m in MODULES}
CORE_TABS = ("Project Health", "Contacts", "Schedule", "Project Documents", "Field Photos", "Daily Report",
             "Payments", "Expenses")   # always on (Settings too); Schedule of Values follows the billing type
# the starting set per job type (estimates.EST_TYPES); the office can change it (SETTING_KEY)
TYPE_DEFAULTS = {
    "FR": ["drops", "drawings", "crew", "materials", "safety", "compliance", "rfis"],
    "IRA": ["drawings", "crew", "safety", "compliance"],
    "IR": ["crew", "materials", "rfis"],
    "PG": ["crew", "materials", "compliance"],
    "OTH": [],
}
SETTING_KEY = "job_type_tabs"


def _has_column(conn, table, column) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                                 "AND table_name = ? AND column_name = ?", (table, column)).fetchone())
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone():
        return False
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def ensure(conn) -> None:
    """Idempotent: add projects.enabled_modules when missing (SQLite and Postgres)."""
    if not _has_column(conn, "projects", "project_code"):
        return   # no projects table yet (fixture that migrates later)
    if not _has_column(conn, "projects", "enabled_modules"):
        conn.execute("ALTER TABLE projects ADD COLUMN enabled_modules TEXT")
        conn.commit()


def _ensure_settings(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT, "
                 "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()


def type_defaults(conn) -> dict:
    """{est_type: [module keys]} — the office's saved starting sets over TYPE_DEFAULTS."""
    out = {t: [k for k in KEYS if k in v] for t, v in TYPE_DEFAULTS.items()}   # canonical order, like every stored list
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (SETTING_KEY,)).fetchone()
        saved = json.loads(row["value"]) if row and row["value"] else {}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        saved = {}
    for t, keys in (saved or {}).items():
        if t in out and isinstance(keys, list):
            out[t] = [k for k in KEYS if k in set(map(str, keys))]
    return out


def set_type_defaults(conn, mapping, *, actor=None) -> dict:
    if not isinstance(mapping, dict):
        raise ValueError("send a starting set per job type")
    clean = type_defaults(conn)
    for t, keys in mapping.items():
        if t not in TYPE_DEFAULTS:
            raise ValueError("unknown job type")
        clean[t] = validate(keys)
    _ensure_settings(conn)
    conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                 (SETTING_KEY, json.dumps(clean)))
    conn.commit()
    log.info("job type starting tabs updated by user=%s", actor)
    return clean


def starting_set(conn, est_type: str) -> list:
    return list(type_defaults(conn).get((est_type or "").upper(), []))


def catalog() -> list:
    return [{"key": m["key"], "label": m["label"], "hint": m["hint"], "views": list(m["views"])} for m in MODULES]


def _parse(raw) -> Optional[list]:
    """Stored value -> list of valid keys, or None meaning 'never customized' (all on)."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(v, list):
        return None
    return [k for k in KEYS if k in {str(x) for x in v}]


def validate(keys) -> list:
    if keys is None:
        raise ValueError("modules must be a list")
    if not isinstance(keys, (list, tuple)):
        raise ValueError("modules must be a list")
    bad = [k for k in keys if not isinstance(k, str) or k not in KEYS]
    if bad:
        raise ValueError("unknown module")
    return [k for k in KEYS if k in set(keys)]   # canonical order, de-duplicated


def enabled(conn, code: str) -> set:
    """Enabled optional-module keys for a project (everything on when never customized)."""
    try:
        row = conn.execute("SELECT enabled_modules FROM projects WHERE project_code = ?", (code,)).fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return set(KEYS)
    stored = _parse(row["enabled_modules"]) if row else None
    return set(KEYS) if stored is None else set(stored)


def disabled_views(conn, code: str) -> list:
    on = enabled(conn, code)
    return [v for m in MODULES if m["key"] not in on for v in m["views"]]


def disabled_keys(conn, code: str) -> list:
    on = enabled(conn, code)
    return [k for k in KEYS if k not in on]


def profile(conn, code: str) -> Optional[dict]:
    row = conn.execute("SELECT project_code FROM projects WHERE project_code = ?", (code,)).fetchone()
    if not row:
        return None
    on = enabled(conn, code)
    return {"project_code": code,
            "modules": [{"key": m["key"], "label": m["label"], "hint": m["hint"], "views": list(m["views"]),
                         "enabled": m["key"] in on} for m in MODULES]}


def set_modules(conn, code: str, keys, *, actor=None) -> dict:
    keys = validate(keys)
    ensure(conn)
    row = conn.execute("SELECT project_code FROM projects WHERE project_code = ?", (code,)).fetchone()
    if not row:
        raise LookupError("project not found")
    conn.execute("UPDATE projects SET enabled_modules = ?, updated_at = ? WHERE project_code = ?",
                 (json.dumps(keys), datetime.now().isoformat(timespec="seconds"), code))
    conn.commit()
    log.info("project modules updated code=%s enabled=%s by user=%s", code, ",".join(keys) or "-", actor)
    return profile(conn, code)


# ---------------------------------------------------------------- routes
def _api_get(project_code):
    # reached through /api/projects/<code>/… — the #263 scoping hook already refused an
    # unassigned pm; any signed-in internal role may read which tabs a job shows
    conn = db_layer.connect()
    try:
        p = profile(conn, project_code)
        return jsonify({"data": p}) if p else (jsonify({"error": "project not found"}), 404)
    finally:
        conn.close()


@requires_company
def _api_set(project_code):
    body = request.get_json(silent=True) or {}
    conn = db_layer.connect()
    try:
        try:
            return jsonify({"data": set_modules(conn, project_code, body.get("modules"),
                                                actor=(current_user() or {}).get("id"))})
        except LookupError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    finally:
        conn.close()


@requires_company
def _api_types_get():
    conn = db_layer.connect()
    try:
        return jsonify({"data": {"modules": catalog(), "core": list(CORE_TABS), "types": type_defaults(conn)}})
    finally:
        conn.close()


@requires_company
def _api_types_set():
    body = request.get_json(silent=True) or {}
    conn = db_layer.connect()
    try:
        try:
            return jsonify({"data": {"types": set_type_defaults(conn, body.get("types"), actor=(current_user() or {}).get("id"))}})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    finally:
        conn.close()


def register(app) -> None:
    conn = db_layer.connect()
    try:
        ensure(conn)
        _ensure_settings(conn)
    except Exception as e:   # never block boot on a fixture that migrates later
        log.warning("project modules ensure skipped: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    app.add_url_rule("/api/projects/<project_code>/modules", "project_modules_get", _api_get, methods=["GET"])
    app.add_url_rule("/api/admin/projects/<project_code>/modules", "project_modules_set", _api_set, methods=["PUT"])
    app.add_url_rule("/api/admin/job-type-tabs", "job_type_tabs_get", _api_types_get, methods=["GET"])
    app.add_url_rule("/api/admin/job-type-tabs", "job_type_tabs_set", _api_types_set, methods=["PUT"])
