"""#356 — how a job is billed: AIA / Schedule of values, lump sum, T&M or unit price.

Some jobs bill against an approved schedule of values (AIA G702/G703 pay applications,
#317); others are lump sum, time & materials or unit price and never will. Until now
nothing on a project said which — "has an SOV" was only inferred from billing_sov rows.

  projects.billing_type  TEXT  aia_sov | lump_sum | time_materials | unit_price | NULL
                               (NULL = "Not set": legacy jobs until someone picks)

Asked when a project is created (onboarding), changeable in the project's Settings by
admin / c_suite. Effects: the Schedule of Values tab and the AIA progress-billing card
only appear for AIA jobs, the Projects list shows a Billing chip, and the AIA write
routes refuse on a job billed another way (409). A job with pay applications can't be
switched away from AIA. Backfill (idempotent, NULLs only): jobs that already have
billing_sov rows become aia_sov; a value someone picked is never overwritten.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from flask import jsonify, request

import db_layer
from auth import current_user, requires_company

log = logging.getLogger("ssc.billing_type")

TYPES = {"aia_sov": "AIA / Schedule of values", "lump_sum": "Lump sum",
         "time_materials": "Time & materials", "unit_price": "Unit price"}
SHORT = {"aia_sov": "AIA", "lump_sum": "Lump sum", "time_materials": "T&M", "unit_price": "Unit price"}


def options() -> list:
    return [{"code": k, "label": v, "short": SHORT[k]} for k, v in TYPES.items()]


def _has_column(conn, table, column) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                                 "AND table_name = ? AND column_name = ?", (table, column)).fetchone())
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone():
        return False
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _has_table(conn, table) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name = ?",
                                 (table,)).fetchone())
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone())


def ensure(conn) -> None:
    if not _has_table(conn, "projects"):
        return
    if not _has_column(conn, "projects", "billing_type"):
        conn.execute("ALTER TABLE projects ADD COLUMN billing_type TEXT")
    if _has_table(conn, "billing_sov"):
        conn.execute("UPDATE projects SET billing_type = 'aia_sov' WHERE billing_type IS NULL AND project_code IN "
                     "(SELECT DISTINCT project_code FROM billing_sov)")
    conn.commit()


def validate(v) -> str:
    s = (str(v or "")).strip()
    if s not in TYPES:
        raise ValueError("pick how this job is billed")
    return s


def get(conn, code: str) -> Optional[str]:
    try:
        r = conn.execute("SELECT billing_type FROM projects WHERE project_code = ?", (code,)).fetchone()
        return (r["billing_type"] if r else None) or None
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def label(v: Optional[str]) -> str:
    return TYPES.get(v or "", "Not set")


def aia_blocked(conn, code: str) -> Optional[str]:
    """A message when this job is billed some other way (AIA writes must refuse), else None.
    Not set = allowed (legacy jobs keep working until someone picks)."""
    bt = get(conn, code)
    if bt and bt != "aia_sov":
        return f"this job is billed {TYPES[bt].lower()} — switch it to AIA / Schedule of values in Settings first"
    return None


def set_type(conn, code: str, value, *, actor=None) -> dict:
    v = validate(value)
    row = conn.execute("SELECT project_code, billing_type FROM projects WHERE project_code = ?", (code,)).fetchone()
    if not row:
        raise LookupError("project not found")
    if v != "aia_sov" and _has_table(conn, "pay_application"):
        n = conn.execute("SELECT COUNT(*) FROM pay_application WHERE project_code = ?", (code,)).fetchone()[0]
        if n:
            raise PermissionError(f"this job has {n} AIA pay application(s) — it has to stay AIA / Schedule of values")
    conn.execute("UPDATE projects SET billing_type = ?, updated_at = ? WHERE project_code = ?",
                 (v, datetime.now().isoformat(timespec="seconds"), code))
    conn.commit()
    return profile(conn, code)


def profile(conn, code: str) -> Optional[dict]:
    row = conn.execute("SELECT project_code, name, status FROM projects WHERE project_code = ?", (code,)).fetchone()
    if not row:
        return None
    bt = get(conn, code)
    return {"project_code": row["project_code"], "name": row["name"], "status": row["status"] or "active",
            "billing_type": bt, "billing_label": label(bt), "billing_short": SHORT.get(bt or "", "Not set"),
            "billing_options": options()}


# ---------------------------------------------------------------- routes
def _api_profile(project_code):
    # reached through /api/projects/<code>/… — the #263 scoping hook has already
    # refused an unassigned pm; any signed-in internal role may read how a job is billed
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
            return jsonify({"data": set_type(conn, project_code, body.get("billing_type"),
                                             actor=(current_user() or {}).get("id"))})
        except LookupError as e:
            return jsonify({"error": str(e)}), 404
        except PermissionError as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    finally:
        conn.close()


def register(app) -> None:
    conn = db_layer.connect()
    try:
        ensure(conn)
    except Exception as e:   # never block boot on a fixture that migrates later
        log.warning("#356 billing_type ensure skipped: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    app.add_url_rule("/api/projects/<project_code>/profile", "project_profile", _api_profile, methods=["GET"])
    app.add_url_rule("/api/admin/projects/<project_code>/billing-type", "project_billing_type_set", _api_set,
                     methods=["PUT"])
