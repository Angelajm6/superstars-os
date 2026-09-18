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


def _parse_hhmm(s):
    """Parse 'HH:MM' (or 'HH:MM:SS') to (hour, minute). Returns None on bad input."""
    if not s:
        return None
    try:
        parts = str(s).split(':')
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None


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
    """
    dates = week_dates(monday)
    date_strs = [d.isoformat() for d in dates]

    # #246 — pool from the CANONICAL roster view (v_worker_roster carries
    # labor_status with the #241 LEFT-JOIN semantics defined in ONE place);
    # this module applies its own inclusion rule (active OR has-hours) on top.
    workers = conn.execute(
        # #260 — sort key in the SELECT list: Postgres requires SELECT DISTINCT
        # ORDER BY expressions to be selected (SQLite did not). Dependent on
        # worker_id, so the distinct set is unchanged; callers ignore it.
        """SELECT DISTINCT e.employee_id, e.worker_id, e.name, e.trade,
                  e.labor_status, CAST(SUBSTR(e.worker_id, 3) AS INTEGER) AS sort_key
           FROM v_worker_roster e
           JOIN project_assignments pa ON pa.employee_id = e.employee_id
           WHERE pa.status = 'active'
           ORDER BY sort_key"""
    ).fetchall()

    # One query for the whole week — index on (date, project_code) covers this.
    placeholders = ",".join("?" * len(date_strs))
    rows = conn.execute(
        f"""SELECT id, employee_id, date, time_in, time_out, project_code
            FROM sign_in_log
            WHERE date IN ({placeholders})
            ORDER BY employee_id, date, id""",
        date_strs,
    ).fetchall()

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
        labor_active = w["labor_status"] != "inactive"
        days = []
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
                day_hours = 0.0
                for r in day_rows:
                    day_hours += compute_worked_hours(r["time_in"], r["time_out"])
                # Primary row is the one most recently created (last id); show
                # its times in the cell. Operator edits this primary row.
                primary = day_rows[-1]
                day = {
                    "date": dstr,
                    "sign_in_id": primary["id"],
                    "time_in": primary["time_in"],
                    "time_out": primary["time_out"],
                    "hours": round(day_hours, 2),
                    "project_code": primary["project_code"],
                    "has_entry": True,
                    "row_count": len(day_rows),  # >1 means multi-project day
                }
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
    }
