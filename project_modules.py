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

New jobs created with "+ New project" start with EVERY module on, exactly like existing jobs
(enabled_modules stays NULL until someone customizes it, so a module added later is on for them
too). admin / c_suite switch modules off from the project's Settings tab. Hiding a tab is a convenience, not access
control: the endpoints behind it keep their own role / assignment checks.
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
    {"key": "drops", "label": "Drop plan & reports", "hint": "Drop Plan, Drop Report, schedule health and the Operations panel",
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
NEW_JOB_DEFAULT = None               # a brand-new job is 'never customized' -> every module on


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


def register(app) -> None:
    conn = db_layer.connect()
    try:
        ensure(conn)
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
