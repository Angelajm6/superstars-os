"""Weekly hours-worked computation, source-of-truth = sign_in_log.

Single helper for hours WORKED (labor actually performed) used by both
the DCR labor section and the weekly Hours Log so the two views can
never drift apart. Lunch is ALWAYS subtracted — every day gets a
30-minute deduction regardless of shift length (per
HANDOFF_WEEKLY_HOURS_LOG).

Naming note (intentional): we say "worked," not "paid." Whether
those hours ultimately get paid is a downstream manual decision
(an early-leave for "personal" may be unpaid; "sent home — weather"
may be paid). The number is the same; the framing avoids implying
the system makes pay-or-not calls.

Forward note (task #64, NOT this commit): time_in / time_out are the
BILLABLE in/out times. When the live PIN model lands, attendance
timestamps will be added on a separate column so payroll keeps
reading from these billable fields without redefinition.
"""
from datetime import date, datetime, timedelta


DEFAULT_LUNCH_MINUTES = 30
STANDARD_TIME_IN = "07:00"
STANDARD_TIME_OUT = "15:30"


WEEK_DAYS = 7   # #310 — the payroll week is Monday through Sunday (crews work weekends)


def last_completed_week(today=None):
    """Return (monday, sunday) of the most recent Mon-Sun week that is
    fully past relative to `today`. Any day of the current week — Sunday
    included, the week is still in progress — resolves to the previous
    week. Payroll runs one week in arrears, so this is what the weekly
    view defaults to.

    #310 — the week used to be Mon-Fri; the company now works weekends, so
    Saturday and Sunday hours are payroll hours like any other day.

    Examples (today -> result):
      Wed May 20, 2026 -> (May 11, May 17)  # this week not yet done
      Sun May 24, 2026 -> (May 11, May 17)  # Sunday is still this week
      Mon May 25, 2026 -> (May 18, May 24)
    """
    today = today or date.today()
    # weekday(): Mon=0 ... Sun=6 — this week's Monday, then step back one week.
    this_mon = today - timedelta(days=today.weekday())
    last_mon = this_mon - timedelta(days=7)
    return last_mon, last_mon + timedelta(days=WEEK_DAYS - 1)


def week_dates(monday):
    """Return the 7 dates Mon..Sun starting from `monday` (#310)."""
    return [monday + timedelta(days=i) for i in range(WEEK_DAYS)]


def to_hhmm(s):
    """'HH:MM' from 'HH:MM', 'HH:MM:SS' or a worker-app ISO timestamp
    ('2026-10-30T07:02:11'). The worker app stores the full timestamp; before
    this the parser choked on the date part and those shifts counted 0 hours."""
    if not s:
        return None
    s = str(s)
    if 'T' in s:
        s = s.split('T', 1)[1]
    parts = s.split(':')
    try:
        h, m = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


def _parse_hhmm(s):
    """Parse 'HH:MM' (or 'HH:MM:SS', or an ISO timestamp) to (hour, minute).
    Returns None on bad input."""
    v = to_hhmm(s)
    if v is None:
        return None
    return int(v[:2]), int(v[3:])


def compute_worked_hours(time_in, time_out, lunch_minutes=DEFAULT_LUNCH_MINUTES):
    """Return hours WORKED as a float rounded to 2 decimals.

    Formula:  max(0, (time_out - time_in) - lunch_minutes), in hours.

    "Worked" not "paid": this is labor actually performed. Whether the
    hours get paid is a separate, manual downstream decision (early-leave
    for "personal" might be unpaid; "sent home — weather" might be paid).
    The number is identical to the prior compute_paid_hours; only the
    framing changed so the system isn't implying pay-or-not policy.

    Reused by the weekly Hours grid AND the DCR aggregator so the two
    surfaces never disagree. Returns 0.0 if either time is missing or
    malformed — the caller decides whether to render that as blank or
    explicit zero.

    Overnight shifts (out < in) are NOT supported — return 0.0 for those.
    Facade work doesn't span midnight; if someone genuinely needs to
    log overnight, payroll catches it manually rather than letting the
    formula guess.
    """
    pin = _parse_hhmm(time_in)
    pout = _parse_hhmm(time_out)
    if pin is None or pout is None:
        return 0.0
    h_in, m_in = pin
    h_out, m_out = pout
    raw_minutes = (h_out * 60 + m_out) - (h_in * 60 + m_in)
    if raw_minutes <= 0:
        return 0.0
    paid_minutes = max(0, raw_minutes - lunch_minutes)
    return round(paid_minutes / 60.0, 2)


# ---------------------------------------------------------------------------
# One place at a time + the 8-hour day (Angela, 2026-10-30).
# A worker's entries on one date may span several jobs, but their times must
# not overlap. The day's hours across every job are capped at DAY_LIMIT for
# pay until an admin or PM approves the extra (hours_overtime).
# ---------------------------------------------------------------------------
DAY_LIMIT = 8.0
_DAY_END = 24 * 60
# The rule starts with the payroll week of Mon 2026-10-26 (not yet paid when it
# shipped). Earlier weeks were paid as logged and must read the same forever.
RULE_START = "2026-10-26"


def rule_applies(date_str):
    return str(date_str) >= RULE_START


def _minutes(s):
    p = _parse_hhmm(s)
    return None if p is None else p[0] * 60 + p[1]


def day_entries(conn, employee_id, date_str, exclude_id=None):
    """Every sign-in row for this worker on this date, any job."""
    rows = conn.execute(
        "SELECT id, project_code, time_in, time_out FROM sign_in_log "
        "WHERE employee_id = ? AND date = ? ORDER BY id", (employee_id, date_str)).fetchall()
    return [dict(r) for r in rows if exclude_id is None or r["id"] != exclude_id]


def find_overlap(entries, time_in, time_out):
    """The first entry whose times overlap [time_in, time_out), or None. An
    entry still open (no time out) runs to the end of the day."""
    a0 = _minutes(time_in)
    if a0 is None:
        return None
    a1 = _minutes(time_out)
    a1 = _DAY_END if a1 is None else a1
    for e in entries:
        b0 = _minutes(e["time_in"])
        if b0 is None:
            continue
        b1 = _minutes(e["time_out"])
        b1 = _DAY_END if b1 is None else b1
        if a0 < b1 and b0 < a1:
            return e
    return None


def overlap_message(e):
    def fmt(v):
        hm = to_hhmm(v)
        if not hm:
            return "now"
        h, m = int(hm[:2]), hm[3:]
        return f"{(h % 12) or 12}:{m} {'AM' if h < 12 else 'PM'}"
    return (f"Already logged on {e['project_code']} from {fmt(e['time_in'])} to "
            f"{fmt(e['time_out']) if e['time_out'] else 'now (still signed in)'} that day. "
            "Change the times so they don't overlap.")


def day_total(entries, extra=None):
    """Worked hours for the day across jobs; `extra` = (time_in, time_out) of
    the entry being saved."""
    total = sum(compute_worked_hours(e["time_in"], e["time_out"]) for e in entries)
    if extra:
        total += compute_worked_hours(*extra)
    return round(total, 2)


def get_overtime(conn, employee_id, date_str):
    r = conn.execute("SELECT * FROM hours_overtime WHERE employee_id = ? AND date = ?",
                     (employee_id, date_str)).fetchone()
    return dict(r) if r else None


def payable_hours(total, ot):
    """Hours payroll pays for a day: everything up to DAY_LIMIT, more only
    once approved (and never more than was approved)."""
    if total <= DAY_LIMIT:
        return round(total, 2)
    if ot and ot.get("status") == "approved" and ot.get("approved_hours") is not None:
        return round(min(total, max(DAY_LIMIT, float(ot["approved_hours"]))), 2)
    return DAY_LIMIT


def overtime_view(logged, ot):
    """The day's over-the-limit state for the UI. 'missing' = over the limit
    with no request, or more hours than were approved: someone must give a
    reason (or approve with one)."""
    status = ot["status"] if ot else "missing"
    if status == "approved" and ot.get("approved_hours") is not None and logged > float(ot["approved_hours"]):
        status = "missing"
    return {
        "id": ot["id"] if ot else None,
        "status": status,
        "extra": round(logged - DAY_LIMIT, 2),
        "reason": ot["reason"] if ot else None,
        "approved_hours": ot.get("approved_hours") if ot else None,
    }


def needs_reason(total, ot):
    """True when saving a day of `total` hours needs a fresh reason: over the
    limit and not already covered by an approval or a pending request."""
    if total <= DAY_LIMIT:
        return False
    if not ot:
        return True
    if ot["status"] == "pending":
        return False
    if ot["status"] == "approved" and ot.get("approved_hours") is not None:
        return total > float(ot["approved_hours"])
    return True   # rejected -> a new request needs its own reason


def sync_overtime(conn, employee_id, date_str, total, reason=None,
                  actor_uid=None, actor_role=None, now=None):
    """Keep hours_overtime in step with the day's hours after a write.
    <= limit: a pending or rejected request is dropped. Over the limit with a
    reason: (re)opened as pending. Over the limit already covered: the pending
    row's logged hours are refreshed. Returns the row (or None)."""
    from datetime import datetime as _dt
    now = now or _dt.now().isoformat(timespec="seconds")
    ot = get_overtime(conn, employee_id, date_str)
    if total <= DAY_LIMIT:
        if ot and ot["status"] in ("pending", "rejected"):
            conn.execute("DELETE FROM hours_overtime WHERE id = ?", (ot["id"],))
        return None
    reason = (reason or "").strip()[:500] or None
    if reason:
        if ot:
            conn.execute(
                "UPDATE hours_overtime SET logged_hours = ?, reason = ?, status = 'pending', "
                "approved_hours = NULL, requested_by_uid = ?, requested_by_role = ?, requested_at = ?, "
                "decided_by_uid = NULL, decided_by_role = NULL, decided_at = NULL, decision_note = NULL "
                "WHERE id = ?", (total, reason, actor_uid, actor_role, now, ot["id"]))
        else:
            conn.execute(
                "INSERT INTO hours_overtime (employee_id, date, logged_hours, reason, status, "
                "requested_by_uid, requested_by_role, requested_at) VALUES (?,?,?,?,'pending',?,?,?)",
                (employee_id, date_str, total, reason, actor_uid, actor_role, now))
    elif ot and ot["status"] == "pending":
        conn.execute("UPDATE hours_overtime SET logged_hours = ? WHERE id = ?", (total, ot["id"]))
    return get_overtime(conn, employee_id, date_str)


def build_week_grid(conn, monday):
    """Build the weekly hours grid for the given Monday.

    Returns dict:
      {
        "week_start": "YYYY-MM-DD" (monday),
        "week_end":   "YYYY-MM-DD" (sunday — #310),
        "dates":      ["YYYY-MM-DD"] * 7 (Mon..Sun),
        "workers": [
          {"employee_id": "E-00001", "name": "...", "trade": "...",
           "days": [{"date": "...", "sign_in_id": int|None,
                     "time_in": "HH:MM"|None, "time_out": "HH:MM"|None,
                     "hours": float, "project_code": "FR-BX-001"|None}, ...],
           "weekly_total": float}, ...
        ],
        "totals_by_day": [float, float, float, float, float],
        "grand_total": float,
      }

    Worker pool (#244): employees with at least one ACTIVE project_assignment,
    further scoped by labor_worker_state.status — the single source of truth
    for the active roster since #241 (LEFT-JOIN semantics: no state row =
    active). A worker on the sheet is either
      - labor-ACTIVE (including zero-hour workers — those are the "no
        sign-ins" payroll exception, counted in no_signin_count), or
      - labor-INACTIVE but with hours in THIS week (check-cutting history
        must not rewrite when a worker is later retired).
    Inactive workers with zero hours in the week are not part of the
    week's roster at all — retired, not "no sign-ins."

    KNOWN LIMITATION (reported in #244, deliberately not silently chosen):
    the model stores only CURRENT status. Approved 'deactivate' rows are
    timestamped in labor_rate_change, but reactivation is an instant
    status flip with no history row — so "was this worker active during
    week X" is NOT answerable for arbitrary past weeks. Consequence: a
    worker who was active-with-zero-hours in a past week and was deactivated
    afterwards will not appear on that past week's sheet. Workers with
    hours always appear, so payable history is never rewritten.

    Sums hours across all projects for a given (worker, date) — payroll
    doesn't care which project a worker was on, just the total.

    2026-10-30 (Angela):
      * the pool is everyone with a sign-in this week (any status, on a crew
        or not: taking a deactivated worker off a crew used to erase them from
        every past week) plus labor-active, non-archived workers on a crew;
      * `trade` is the Pay Rates trade (labor_worker_state), falling back to
        the employee record — edits on Pay Rates show here at once;
      * a day's `hours` is what payroll PAYS (payable_hours: 8 until extra is
        approved); `hours_logged` is what was worked; `overtime` carries the
        request when the day is over the limit.
    """
    dates = week_dates(monday)
    date_strs = [d.isoformat() for d in dates]

    placeholders = ",".join("?" * len(date_strs))
    # #246 — pool from the CANONICAL roster view (labor_status, #241 semantics).
    # #260 — the sort key is selected (Postgres).
    workers = conn.execute(
        f"""SELECT e.employee_id, e.worker_id, e.name,
                  COALESCE(ls.trade, e.trade) AS trade,
                  e.labor_status, e.archived_at,
                  CAST(SUBSTR(e.worker_id, 3) AS INTEGER) AS sort_key
           FROM v_worker_roster e
           LEFT JOIN labor_worker_state ls ON ls.worker_id = e.worker_id
           WHERE e.employee_id IN (SELECT employee_id FROM project_assignments WHERE status = 'active')
              OR e.employee_id IN (SELECT employee_id FROM sign_in_log WHERE date IN ({placeholders}))
           ORDER BY sort_key, e.employee_id""",
        date_strs,
    ).fetchall()

    # One query for the whole week — index on (date, project_code) covers this.
    rows = conn.execute(
        f"""SELECT id, employee_id, date, time_in, time_out, project_code
            FROM sign_in_log
            WHERE date IN ({placeholders})
            ORDER BY employee_id, date, id""",
        date_strs,
    ).fetchall()

    overtime = {}
    for r in conn.execute(
            f"SELECT * FROM hours_overtime WHERE date IN ({placeholders})", date_strs).fetchall():
        overtime[(r["employee_id"], r["date"])] = dict(r)

    # Bucket by (employee_id, date) — multiple project rows on same day get summed.
    by_emp_day = {}
    for r in rows:
        key = (r["employee_id"], r["date"])
        by_emp_day.setdefault(key, []).append(r)

    grid_workers = []
    totals_by_day = [0.0] * len(date_strs)
    grand_total = 0.0
    roster_active_count = 0
    no_signin_count = 0

    for w in workers:
        eid = w["employee_id"]
        labor_active = w["labor_status"] != "inactive" and not w["archived_at"]
        days = []
        pending_extra = 0.0
        weekly_total = 0.0
        day_hours_by_index = []
        for i, dstr in enumerate(date_strs):
            day_rows = by_emp_day.get((eid, dstr), [])
            if not day_rows:
                day = {"date": dstr, "sign_in_id": None, "time_in": None,
                       "time_out": None, "hours": 0.0, "project_code": None,
                       "has_entry": False}
                day_hours_by_index.append(0.0)
            else:
                # Sum hours across all projects for this (worker, date)
                logged = round(sum(compute_worked_hours(r["time_in"], r["time_out"])
                                   for r in day_rows), 2)
                ot = overtime.get((eid, dstr))
                capped = rule_applies(dstr)
                day_hours = payable_hours(logged, ot) if capped else logged
                # Primary row is the one most recently created (last id); show
                # its times in the cell. Operator edits this primary row.
                primary = day_rows[-1]
                day = {
                    "date": dstr,
                    "sign_in_id": primary["id"],
                    "time_in": to_hhmm(primary["time_in"]),
                    "time_out": to_hhmm(primary["time_out"]),
                    "hours": day_hours,
                    "hours_logged": logged,
                    "project_code": primary["project_code"],
                    "has_entry": True,
                    "row_count": len(day_rows),  # >1 means multi-project day
                }
                if capped and logged > DAY_LIMIT:
                    day["overtime"] = overtime_view(logged, ot)
                    if day["overtime"]["status"] in ("pending", "missing"):
                        pending_extra += round(logged - day_hours, 2)
                weekly_total += day_hours
                day_hours_by_index.append(day_hours)
            days.append(day)
        # #244 inclusion rule — see docstring. Inactive + no hours this
        # week -> not on this week's sheet (retired, not "no sign-ins").
        if not labor_active and weekly_total <= 0:
            continue
        if labor_active:
            roster_active_count += 1
            if weekly_total <= 0:
                no_signin_count += 1
        for i, dh in enumerate(day_hours_by_index):
            totals_by_day[i] += dh
        grid_workers.append({
            "employee_id": eid,
            "worker_id": w["worker_id"],
            "name": w["name"],
            "trade": w["trade"],
            "labor_active": labor_active,
            "days": days,
            "weekly_total": round(weekly_total, 2),
            "overtime_pending_hours": round(pending_extra, 2),
        })
        grand_total += weekly_total

    # No-Work Day designation per date — surfaced so consumers (Labor
    # Rate Tracker, Weekly Summary) can render "NO WORK — REASON" in
    # that day's column instead of a blank zero. Pulled from
    # report_index.no_work; date keys match `dates` exactly.
    no_work_by_date: dict[str, dict] = {}
    placeholders2 = ",".join("?" * len(date_strs))
    for r in conn.execute(
        f"SELECT DISTINCT report_date, no_work_reason, no_work_note "
        f"FROM report_index "
        f"WHERE report_type='DCR' AND no_work=1 AND report_date IN ({placeholders2})",
        date_strs,
    ).fetchall():
        no_work_by_date[r["report_date"]] = {
            "reason": r["no_work_reason"],
            "note": r["no_work_note"],
        }

    return {
        "week_start": date_strs[0],
        "week_end": date_strs[-1],
        "dates": date_strs,
        "workers": grid_workers,
        "totals_by_day": [round(t, 2) for t in totals_by_day],
        "grand_total": round(grand_total, 2),
        "no_work_by_date": no_work_by_date,
        # #244 — corrected roster math (see docstring): the denominator is
        # labor-ACTIVE workers; no_signin_count = active with zero hours.
        "roster_active_count": roster_active_count,
        "no_signin_count": no_signin_count,
        "paid_count": sum(1 for gw in grid_workers if gw["weekly_total"] > 0),
        "overtime_pending_hours": round(sum(gw["overtime_pending_hours"] for gw in grid_workers), 2),
        "overtime_pending_count": sum(1 for gw in grid_workers for d in gw["days"]
                                      if (d.get("overtime") or {}).get("status") in ("pending", "missing")),
    }
