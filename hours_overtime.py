"""Hours over 8 in a day: the approve / reject door, and the week picker's summary.

The rule itself lives in payroll_hours (DAY_LIMIT, payable_hours, sync_overtime);
the sign-in doors in server.py ask for the reason. This module is what an admin,
C-suite or PM uses to decide, plus a per-week summary so Weekly Payroll's week
picker can show which weeks have hours and which still need approval.

PII (CLAUDE.md): responses and audit rows carry IDs, dates and hours — never
names or reasons in the server log.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from flask import jsonify, request

import payroll_hours as ph
from auth import _db, current_user, requires_role

log = logging.getLogger(__name__)

DECIDE_ROLES = ("admin", "c_suite", "pm")
_MAX_RANGE_DAYS = 120


def _valid_date(v):
    try:
        return datetime.strptime(str(v or "").strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def decide(conn, employee_id, date_str, decision, reason=None, note=None, actor=None):
    """Approve or reject the day's hours over the limit. Approve pays every hour
    logged that day (approved_hours = the day's total now); reject pays the
    limit. A reason must exist (the one asked with, or one given here)."""
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    if not ph.rule_applies(date_str):
        raise ValueError("days before the 8-hour rule started are paid as logged")
    total = ph.day_total(ph.day_entries(conn, employee_id, date_str))
    if total <= ph.DAY_LIMIT:
        raise LookupError("this day is 8 hours or less, nothing to approve")
    ot = ph.get_overtime(conn, employee_id, date_str)
    reason = (reason or "").strip()[:500] or (ot or {}).get("reason")
    if not reason:
        raise ValueError("add a reason for the extra hours")
    actor = actor or {}
    now = datetime.now().isoformat(timespec="seconds")
    status = "approved" if decision == "approve" else "rejected"
    approved = total if status == "approved" else None
    note = (note or "").strip()[:500] or None
    if ot:
        conn.execute(
            "UPDATE hours_overtime SET logged_hours = ?, approved_hours = ?, reason = ?, status = ?, "
            "decided_by_uid = ?, decided_by_role = ?, decided_at = ?, decision_note = ? WHERE id = ?",
            (total, approved, reason, status, actor.get("id"), actor.get("role"), now, note, ot["id"]))
    else:
        conn.execute(
            "INSERT INTO hours_overtime (employee_id, date, logged_hours, approved_hours, reason, status, "
            "requested_by_uid, requested_by_role, requested_at, decided_by_uid, decided_by_role, "
            "decided_at, decision_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (employee_id, date_str, total, approved, reason, status, actor.get("id"), actor.get("role"),
             now, actor.get("id"), actor.get("role"), now, note))
    conn.execute(
        "INSERT INTO audit_log (action, actor_user_id, actor_role, target_type, target_id, "
        "before_json, after_json, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"hours_overtime_{status}", actor.get("id"), actor.get("role") or "system", "worker", employee_id,
         json.dumps({"status": (ot or {}).get("status")}),
         json.dumps({"date": date_str, "hours": total, "status": status}), None, now))
    row = ph.get_overtime(conn, employee_id, date_str)
    return {"employee_id": employee_id, "date": date_str, "status": status,
            "hours_logged": total, "hours": ph.payable_hours(total, row)}


def week_summary(conn, start, end):
    """{week_start: {hours, pending}} for every Mon–Sun week touching
    [start, end]: paid hours, and days over the limit still waiting."""
    rows = conn.execute(
        "SELECT employee_id, date, time_in, time_out FROM sign_in_log WHERE date >= ? AND date <= ?",
        (start, end)).fetchall()
    per_day = {}
    for r in rows:
        k = (r["employee_id"], r["date"])
        per_day[k] = per_day.get(k, 0.0) + ph.compute_worked_hours(r["time_in"], r["time_out"])
    ots = {(r["employee_id"], r["date"]): dict(r) for r in conn.execute(
        "SELECT * FROM hours_overtime WHERE date >= ? AND date <= ?", (start, end)).fetchall()}
    weeks = {}
    for (eid, d), logged in per_day.items():
        logged = round(logged, 2)
        dd = date.fromisoformat(d)
        monday = (dd - timedelta(days=dd.weekday())).isoformat()
        w = weeks.setdefault(monday, {"hours": 0.0, "pending": 0})
        if ph.rule_applies(d):
            ot = ots.get((eid, d))
            w["hours"] += ph.payable_hours(logged, ot)
            if logged > ph.DAY_LIMIT and ph.overtime_view(logged, ot)["status"] in ("pending", "missing"):
                w["pending"] += 1
        else:
            w["hours"] += logged
    for w in weeks.values():
        w["hours"] = round(w["hours"], 2)
    return weeks


# ---------------------------------------------------------------- routes
@requires_role(*DECIDE_ROLES)
def _api_decide():
    body = request.get_json(silent=True) or {}
    employee_id = (body.get("employee_id") or "").strip()
    date_str = _valid_date(body.get("date"))
    if not employee_id or not date_str:
        return jsonify({"error": "employee_id and date (YYYY-MM-DD) are required"}), 400
    conn = _db()
    try:
        if not conn.execute("SELECT 1 FROM employees WHERE employee_id = ?", (employee_id,)).fetchone():
            return jsonify({"error": "worker not found"}), 404
        try:
            out = decide(conn, employee_id, date_str, body.get("decision"), body.get("reason"),
                         body.get("note"), actor=current_user())
        except LookupError as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        conn.commit()
        log.info(f"hours-overtime: {out['status']} target={employee_id} date={date_str}")
        return jsonify({"data": out})
    finally:
        conn.close()


@requires_role(*DECIDE_ROLES)
def _api_weeks():
    start = _valid_date(request.args.get("from"))
    end = _valid_date(request.args.get("to"))
    if not start or not end or end < start:
        return jsonify({"error": "from and to (YYYY-MM-DD) are required"}), 400
    if (date.fromisoformat(end) - date.fromisoformat(start)).days > _MAX_RANGE_DAYS:
        return jsonify({"error": f"range is limited to {_MAX_RANGE_DAYS} days"}), 400
    conn = _db()
    try:
        return jsonify({"data": week_summary(conn, start, end)})
    finally:
        conn.close()


def register(app) -> None:
    app.add_url_rule("/api/payroll/overtime/decide", "payroll_overtime_decide", _api_decide, methods=["POST"])
    app.add_url_rule("/api/payroll/weeks", "payroll_weeks", _api_weeks, methods=["GET"])
