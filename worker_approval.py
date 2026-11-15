"""New-worker approval (2026-11-15).

A worker onboarded in the app starts as WAITING FOR APPROVAL and cannot sign in to the
worker app until an admin, C-suite, PM or super approves them.

  employees.approval_status   'pending' | 'approved' | NULL
                              NULL = approved. Every worker who existed before this
                              feature reads as approved, so nobody on the crew is locked
                              out by the deploy; only the create routes write 'pending'.
  employees.approved_by_uid   the users.id who approved (NULL for legacy rows)
  employees.approved_at       local ISO timestamp

Who may approve:
  admin / c_suite  -> any worker
  pm / super       -> a worker with an active assignment on a job they can open
                      (pm_scoping.pm_can_access_project: a PM's own active jobs, a
                      super's active jobs). One predicate, the same one every job
                      page uses.

  POST /api/workers/<employee_id>/approve   -> {employee_id, worker_id, approval_status}

Pay rates are untouched: rate changes stay with the Pay Rates approval queue.
"""
from __future__ import annotations

import logging
from datetime import datetime

from flask import jsonify

import db_layer

APPROVER_ROLES = ("admin", "c_suite", "pm", "super")
PENDING = "pending"
APPROVED = "approved"


def _has_column(conn, table, column) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                                 "AND table_name = ? AND column_name = ?", (table, column)).fetchone())
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone():
        return False
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def ensure(conn) -> None:
    """Idempotent: the three approval columns on employees. No backfill: NULL = approved."""
    if not _has_column(conn, "employees", "employee_id"):
        return   # no employees table yet (a fixture that migrates later)
    for col, typ in (("approval_status", "TEXT"), ("approved_by_uid", "INTEGER"), ("approved_at", "TEXT")):
        if not _has_column(conn, "employees", col):
            conn.execute(f"ALTER TABLE employees ADD COLUMN {col} {typ}")
    conn.commit()


def is_pending(conn, employee_id) -> bool:
    row = conn.execute("SELECT approval_status FROM employees WHERE employee_id = ?", (employee_id,)).fetchone()
    return bool(row) and row["approval_status"] == PENDING


def pending_ids(conn) -> set:
    """Every worker still waiting (a handful at most; read straight from employees,
    because the roster views were created with e.* before these columns existed)."""
    return {r["employee_id"] for r in conn.execute(
        "SELECT employee_id FROM employees WHERE approval_status = ?", (PENDING,)).fetchall()}


def can_approve(conn, user, employee_id) -> bool:
    role = (user or {}).get("role")
    if role in ("admin", "c_suite"):
        return True
    if role not in ("pm", "super"):
        return False
    import pm_scoping
    codes = [r["project_code"] for r in conn.execute(
        "SELECT DISTINCT project_code FROM project_assignments WHERE employee_id = ? AND status = 'active'",
        (employee_id,)).fetchall()]
    return any(pm_scoping.pm_can_access_project(role, user.get("id"), c, conn) for c in codes)


def _api_approve(employee_id):
    from auth import current_user
    user = current_user() or {}
    conn = db_layer.connect()
    try:
        w = conn.execute("SELECT employee_id, worker_id, approval_status, archived_at FROM employees "
                         "WHERE employee_id = ?", (employee_id,)).fetchone()
        if not w or w["archived_at"]:
            return jsonify({"error": "worker not found"}), 404
        if not can_approve(conn, user, employee_id):
            return jsonify({"error": "You can approve workers on your own jobs only"}), 403
        if w["approval_status"] != PENDING:
            return jsonify({"data": {"employee_id": employee_id, "worker_id": w["worker_id"],
                                     "approval_status": APPROVED, "already_approved": True}})
        conn.execute("UPDATE employees SET approval_status = ?, approved_by_uid = ?, approved_at = ?, "
                     "updated_at = CURRENT_TIMESTAMP WHERE employee_id = ? AND approval_status = ?",
                     (APPROVED, user.get("id"), datetime.now().isoformat(), employee_id, PENDING))
        conn.commit()
        logging.info(f"worker_approval: approved emp={employee_id} by uid={user.get('id')} role={user.get('role')}")
        return jsonify({"data": {"employee_id": employee_id, "worker_id": w["worker_id"],
                                 "approval_status": APPROVED, "already_approved": False}})
    finally:
        conn.close()


def register(app) -> None:
    from auth import requires_role
    app.add_url_rule("/api/workers/<employee_id>/approve", "worker_approve",
                     requires_role(*APPROVER_ROLES)(_api_approve), methods=["POST"])
    try:
        c = db_layer.connect()
        try:
            ensure(c)
        finally:
            c.close()
    except Exception as e:   # never block boot (a fixture that migrates later)
        logging.warning("worker approval ensure skipped: %s", type(e).__name__)
