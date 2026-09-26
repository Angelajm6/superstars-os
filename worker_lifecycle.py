"""#345 — a worker's lifecycle: Active ⇄ Inactive, and permanent removal.

Inactive = archived (`employees.archived_at`, done here, reason recorded) OR labor-
inactive (`labor_worker_state.status`, which stays PM-approved via /labor-rates —
archiving deliberately does NOT touch it). Reactivate clears both.

Permanent removal is for a worker who never actually worked here — a test record, a
duplicate, an onboarding that never started. Anything that proves they worked or hold
credentials BLOCKS it (archive instead); setup rows with no history value on their own
are removed WITH the worker, dependents first because Postgres enforces the foreign
keys SQLite only declares. Every table is probed in the catalog first: optional
tables differ between databases, and on Postgres a failed statement would poison the
transaction.

PII (CLAUDE.md): responses and audit rows carry IDs, trade and counts — never names,
phones, or folder names/paths.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime

from flask import jsonify, request

import db_layer
import ssc_paths
from auth import _db, current_user, requires_company, requires_role

log = logging.getLogger(__name__)

# (table, column, keyed_by, label) — keyed_by "E" = employee_id, "W" = worker_id
BLOCKING = (
    ("sign_in_log", "employee_id", "E", "sign-ins"),
    ("sign_in_edit", "employee_id", "E", "hour corrections"),
    ("certifications", "employee_id", "E", "certifications"),
    ("identifications", "employee_id", "E", "IDs"),
    ("employee_documents", "employee_id", "E", "documents"),
    ("worker_documents", "employee_id", "E", "intake documents"),
    ("cof_cards", "employee_id", "E", "CoF cards"),
    ("company_id_cards", "employee_id", "E", "company ID cards"),
    ("field_photos", "worker_id", "W", "field photos"),
    ("drop_plan_sign_offs", "signed_by_employee_id", "E", "drop-plan sign-offs"),
    ("vehicle", "driver_employee_id", "E", "vehicle assignments"),
    ("users", "employee_id_link", "E", "dashboard login"),
)
PURGED = (
    ("project_assignments", "employee_id", "E"),
    ("employee_assignments", "employee_id", "E"),
    ("worker_rates", "employee_id", "E"),
    ("labor_rate_change", "employee_id", "E"),
    ("labor_rate_change", "worker_id", "W"),
    ("labor_worker_state", "employee_id", "E"),
    ("labor_worker_state", "worker_id", "W"),
    ("worker_device", "employee_id", "E"),
    ("worker_app_session", "employee_id", "E"),
)


def has_column(conn, table, column) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
            "AND table_name = ? AND column_name = ?", (table, column)).fetchone())
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone():
        return False
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _worker(conn, employee_id):
    return conn.execute(
        "SELECT r.employee_id, r.worker_id, r.trade, r.archived_at, r.archived_reason, r.labor_status "
        "FROM v_worker_roster r WHERE r.employee_id = ?", (employee_id,)).fetchone()


def _key(w, keyed_by):
    return w["employee_id"] if keyed_by == "E" else w["worker_id"]


def _folder(employee_id):
    """The worker's records folder, found by its E-##### prefix directly under the
    worker_records root (the stored folder_path is never trusted for a delete)."""
    root = ssc_paths.under_root("worker_records")
    if not root.exists():
        return None
    hits = [p for p in root.iterdir() if p.is_dir() and p.name.startswith(f"{employee_id}_")]
    return hits[0] if len(hits) == 1 else None


def removal_plan(conn, employee_id):
    w = _worker(conn, employee_id)
    if not w:
        return None
    blocking, purge = {}, {}
    for table, col, by, label in BLOCKING:
        key = _key(w, by)
        if key and has_column(conn, table, col):
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (key,)).fetchone()[0]
            if n:
                blocking[label] = int(n)
    for table, col, by in PURGED:
        key = _key(w, by)
        if key and has_column(conn, table, col):
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (key,)).fetchone()[0]
            if n:
                purge[table] = purge.get(table, 0) + int(n)
    folder = _folder(employee_id)
    return {
        "employee_id": w["employee_id"], "worker_id": w["worker_id"],
        "blocking": blocking, "can_delete": not blocking,
        "removes": purge,
        "folder_files": sum(1 for p in folder.rglob("*") if p.is_file()) if folder else 0,
    }


def _audit(conn, action, employee_id, before=None, after=None, note=None):
    import json
    user = current_user() or {}
    conn.execute(
        "INSERT INTO audit_log (action, actor_user_id, actor_role, target_type, target_id, "
        "before_json, after_json, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (action, user.get("id"), user.get("role") or "system", "worker", employee_id,
         json.dumps(before, default=str) if before is not None else None,
         json.dumps(after, default=str) if after is not None else None,
         note, datetime.now().isoformat(timespec="seconds")))


def _state(w):
    if w["archived_at"]:
        return "archived"
    return "inactive" if (w["labor_status"] or "active") == "inactive" else "active"


# ---------------------------------------------------------------- routes
@requires_company
def _api_archive(employee_id):
    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip()[:500] or None
    conn = _db()
    try:
        w = _worker(conn, employee_id)
        if not w:
            return jsonify({"error": "worker not found"}), 404
        if w["archived_at"]:
            return jsonify({"error": "already inactive (archived)"}), 409
        conn.execute("UPDATE employees SET archived_at = CURRENT_TIMESTAMP, archived_reason = ?, "
                     "updated_at = CURRENT_TIMESTAMP WHERE employee_id = ?", (reason, employee_id))
        _audit(conn, "worker_archive", employee_id, before={"state": _state(w)},
               after={"state": "archived"}, note=reason)
        conn.commit()
        return jsonify({"data": {"employee_id": employee_id, "worker_id": w["worker_id"], "state": "archived"}})
    finally:
        conn.close()


@requires_company
def _api_reactivate(employee_id):
    conn = _db()
    try:
        w = _worker(conn, employee_id)
        if not w:
            return jsonify({"error": "worker not found"}), 404
        before = _state(w)
        if before == "active":
            return jsonify({"error": "already active"}), 409
        conn.execute("UPDATE employees SET archived_at = NULL, archived_reason = NULL, "
                     "updated_at = CURRENT_TIMESTAMP WHERE employee_id = ?", (employee_id,))
        if w["worker_id"] and (w["labor_status"] or "active") == "inactive":
            # same instant reactivation as /api/labor-rates/state/<W>/status
            conn.execute("UPDATE labor_worker_state SET status = 'active', updated_at = ? WHERE worker_id = ?",
                         (datetime.now().isoformat(), w["worker_id"]))
        _audit(conn, "worker_reactivate", employee_id, before={"state": before}, after={"state": "active"})
        conn.commit()
        return jsonify({"data": {"employee_id": employee_id, "worker_id": w["worker_id"], "state": "active"}})
    finally:
        conn.close()


@requires_company
def _api_removal_preview(employee_id):
    conn = _db()
    try:
        plan = removal_plan(conn, employee_id)
        if plan is None:
            return jsonify({"error": "worker not found"}), 404
        return jsonify({"data": plan})
    finally:
        conn.close()


def delete_worker(conn, employee_id):
    """Remove a history-free worker and every setup row that points at them. Returns
    (plan, error_response). The caller commits; the folder is removed after commit."""
    plan = removal_plan(conn, employee_id)
    if plan is None:
        return None, (jsonify({"error": "worker not found"}), 404)
    if not plan["can_delete"]:
        return plan, (jsonify({"error": "This worker has history, so they can't be deleted — "
                                        "make them inactive instead.", "data": plan}), 409)
    w = _worker(conn, employee_id)
    for table, col, by in PURGED:
        key = _key(w, by)
        if key and has_column(conn, table, col):
            conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (key,))
    conn.execute("DELETE FROM employees WHERE employee_id = ?", (employee_id,))
    # the audit keeps IDs + counts only — no name/contact PII survives a permanent delete
    _audit(conn, "worker_delete", employee_id,
           before={"employee_id": w["employee_id"], "worker_id": w["worker_id"], "trade": w["trade"],
                   "state": _state(w)},
           after={"removed_rows": plan["removes"], "folder_files": plan["folder_files"]})
    return plan, None


def remove_folder(employee_id):
    folder = _folder(employee_id)
    if folder:
        try:
            shutil.rmtree(folder)
        except Exception:
            log.exception("#345 worker folder removal failed for %s", employee_id)


@requires_company   # #347: admin + c_suite; still blocked by any history
def _api_delete(employee_id):
    conn = _db()
    try:
        plan, err = delete_worker(conn, employee_id)
        if err:
            return err
        conn.commit()
    finally:
        conn.close()
    remove_folder(employee_id)
    return jsonify({"data": {"employee_id": employee_id, "worker_id": plan["worker_id"], "deleted": True}})


def register(app) -> None:
    base = "/api/company/portal/workers/<employee_id>"
    app.add_url_rule(f"{base}/archive", "worker_archive", _api_archive, methods=["POST"])
    app.add_url_rule(f"{base}/reactivate", "worker_reactivate", _api_reactivate, methods=["POST"])
    app.add_url_rule(f"{base}/removal", "worker_removal_preview", _api_removal_preview, methods=["GET"])
    app.add_url_rule(base, "worker_delete", _api_delete, methods=["DELETE"])
