"""Drop Plan roll-up computation layer — Batch B (#200).

Pure functions over a sqlite3 connection (row_factory=Row). No stored
duplication: every total is computed from the ledgers (the single source
of truth), per DROP_PLAN_SYSTEM_DESIGN.md §3.7 / §5.

Money discipline (§6, #158 omit-not-zero):
  * drop_cost / project spend return the sentinel string "pending_rates"
    whenever a contributing SOV line has a NULL unit_rate — NEVER $0 for
    unpriced work. Real rates load later from the architect AIA/SOV.
  * The assembling helpers (drop_detail, project_rollup) take an
    `include_cost` flag. When False they OMIT cost/rate/expense KEYS
    ENTIRELY (not zeroed) so a pm/super response simply has no dollar
    fields — the omit is enforced here, not just at the endpoint.

PII-safe: no names; logged_by is surfaced verbatim as a W-####/id token.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Optional, Union

PENDING_RATES = "pending_rates"


def ceil_tenth(value) -> Optional[float]:
    """Round UP to the nearest tenth for DISPLAY (#202, operator 2026-05-29):
    volume is never under-counted for ordering/billing. Full precision is
    always stored; only the displayed value is ceil-rounded.

    Examples: 2.61 -> 2.7, 2.60 -> 2.6, 2.0 -> 2.0, 1.6667 -> 1.7.
    The round(v*10, 6) kills float artifacts (2.6*10 == 26.000000000000004
    would otherwise ceil to 2.7). Non-numeric / None -> None (never raises)."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return math.ceil(round(v * 10, 6)) / 10

# working-days variance tolerance (days) for the on_track band
_VARIANCE_TOL = 0.5


def _rows(conn: sqlite3.Connection, sql: str, args=()):
    return conn.execute(sql, args).fetchall()


def get_template_id(conn: sqlite3.Connection, project_code: str) -> Optional[int]:
    r = conn.execute(
        "SELECT template_id FROM stage_templates WHERE project_code=? ORDER BY template_id LIMIT 1",
        (project_code,)).fetchone()
    return r["template_id"] if r else None


# ---------------- progress ----------------

def drop_progress(conn: sqlite3.Connection, drop_id: str) -> dict:
    """% = complete steps / applicable (non-N/A) steps (§5)."""
    rows = _rows(conn, "SELECT status FROM drop_stage_status WHERE drop_id=?", (drop_id,))
    applicable = [r for r in rows if r["status"] != "n_a"]
    complete = [r for r in applicable if r["status"] == "complete"]
    pct = round(len(complete) / len(applicable) * 100, 1) if applicable else 0.0
    return {
        "complete_steps": len(complete),
        "applicable_steps": len(applicable),
        "total_steps": len(rows),
        "na_steps": len(rows) - len(applicable),
        "pct": pct,
    }


def current_stage(conn: sqlite3.Connection, drop_id: str) -> Optional[dict]:
    """The drop's current step: first in_progress, else first not_started
    (N/A and complete are skipped). None when every applicable step is done."""
    rows = _rows(
        conn,
        "SELECT ds.step_no, ds.status, st.name "
        "FROM drop_stage_status ds "
        "JOIN drops d ON d.drop_id=ds.drop_id "
        "JOIN stage_templates t ON t.project_code=d.project_code "
        "JOIN stage_template_steps st ON st.template_id=t.template_id AND st.step_no=ds.step_no "
        "WHERE ds.drop_id=? ORDER BY ds.step_no", (drop_id,))
    for want in ("in_progress", "not_started"):
        for r in rows:
            if r["status"] == want:
                return {"step_no": r["step_no"], "name": r["name"], "status": r["status"]}
    return None


# ---------------- quantities ----------------

def drop_quantity_totals(conn: sqlite3.Connection, drop_id: str) -> list:
    """SUM(quantity) and SUM(volume_cf) per SOV line for this drop.

    volume_total is the FULL-precision sum; volume_total_display is the
    ceil-to-tenth display value (#202). patch_count = entries with a
    computed volume (dimensioned patches)."""
    rows = _rows(
        conn,
        "SELECT s.sov_id, s.sov_code, s.unit, "
        "       SUM(q.quantity) AS qty_total, "
        "       SUM(q.volume_cf) AS volume_total, "
        "       COUNT(*) AS entry_count, "
        "       SUM(CASE WHEN q.volume_cf IS NOT NULL THEN 1 ELSE 0 END) AS patch_count "
        "FROM quantity_entries q JOIN sov_line_items s ON s.sov_id=q.sov_line_item "
        "WHERE q.drop_id=? GROUP BY s.sov_id ORDER BY s.sov_code", (drop_id,))
    out = []
    for r in rows:
        d = dict(r)
        vt = d.get("volume_total")
        d["volume_total"] = round(vt, 6) if vt is not None else None  # full precision retained
        d["volume_total_display"] = ceil_tenth(vt)                    # ceil-to-tenth (display)
        if d.get("qty_total") is not None:
            d["qty_total"] = round(d["qty_total"], 6)
        out.append(d)
    return out


# ---------------- working days / variance ----------------

def drop_working_days(conn: sqlite3.Connection, drop_id: str) -> dict:
    """Planned (template) vs actual (logged) working days + variance/status.

    planned_total = sum default_working_days over applicable (non-N/A) steps.
    Variance is computed over the steps that HAVE an actual logged, so we
    compare like-for-like: planned_to_date - actual_to_date (positive=ahead).
    """
    rows = _rows(
        conn,
        "SELECT ds.status, ds.working_days_actual, st.default_working_days "
        "FROM drop_stage_status ds "
        "JOIN drops d ON d.drop_id=ds.drop_id "
        "JOIN stage_templates t ON t.project_code=d.project_code "
        "JOIN stage_template_steps st ON st.template_id=t.template_id AND st.step_no=ds.step_no "
        "WHERE ds.drop_id=?", (drop_id,))
    applicable = [r for r in rows if r["status"] != "n_a"]
    planned_total = round(sum((r["default_working_days"] or 0) for r in applicable), 2)
    with_actual = [r for r in applicable if r["working_days_actual"] is not None]
    actual_to_date = round(sum(r["working_days_actual"] for r in with_actual), 2)
    planned_to_date = round(sum((r["default_working_days"] or 0) for r in with_actual), 2)
    if not with_actual:
        status = "no_actuals"
        variance = 0.0
    else:
        variance = round(planned_to_date - actual_to_date, 2)
        if variance > _VARIANCE_TOL:
            status = "ahead"
        elif variance < -_VARIANCE_TOL:
            status = "behind"
        else:
            status = "on_track"
    return {
        "planned_total_days": planned_total,
        "planned_to_date_days": planned_to_date,
        "actual_to_date_days": actual_to_date,
        "variance_days": variance,
        "status": status,
    }


# ---------------- cost (money — gate at caller) ----------------

def drop_cost(conn: sqlite3.Connection, drop_id: str) -> Union[float, str]:
    """Σ(quantity × unit_rate) per SOV line. Returns PENDING_RATES if ANY
    contributing line's unit_rate is NULL (never $0 for unpriced work).
    Returns 0.0 only when there are genuinely no quantity entries."""
    rows = _rows(
        conn,
        "SELECT s.unit_rate AS rate, SUM(q.quantity) AS qty "
        "FROM quantity_entries q JOIN sov_line_items s ON s.sov_id=q.sov_line_item "
        "WHERE q.drop_id=? GROUP BY s.sov_id", (drop_id,))
    if not rows:
        return 0.0
    if any(r["rate"] is None for r in rows):
        return PENDING_RATES
    return round(sum((r["qty"] or 0) * r["rate"] for r in rows), 2)


def drop_expense_total(conn: sqlite3.Connection, drop_id: str) -> float:
    # #260 — round in Python, not SQL: Postgres has no round(double precision, int)
    # (amount is REAL), only round(numeric, int). SUM then Python round() is dialect-neutral.
    r = conn.execute(
        "SELECT SUM(amount) FROM expense_entries WHERE drop_id=?", (drop_id,)).fetchone()
    return round(r[0], 2) if r and r[0] is not None else 0.0


# ---------------- assemblers (omit-not-zero) ----------------

# #352 — column repair: a second dimensioned repair (L x W x D -> CF) with its own SOV
# line, so it is quantified per drop WITHOUT being counted as a concrete patch.
COLUMN_REPAIR_CODE = "COL-REPAIR"
COLUMN_REPAIR_DESC = "Column repair"


def ensure_column_repair_sov(conn, project_code: str) -> int:
    """The project's column-repair SOV line (created on first use; caller commits)."""
    r = conn.execute("SELECT sov_id FROM sov_line_items WHERE project_code=? AND sov_code=?",
                     (project_code, COLUMN_REPAIR_CODE)).fetchone()
    if r:
        return r["sov_id"]
    conn.execute("INSERT INTO sov_line_items(project_code, sov_code, description, unit) VALUES (?,?,?,?)",
                 (project_code, COLUMN_REPAIR_CODE, COLUMN_REPAIR_DESC, "CF"))
    return conn.execute("SELECT sov_id FROM sov_line_items WHERE project_code=? AND sov_code=?",
                        (project_code, COLUMN_REPAIR_CODE)).fetchone()["sov_id"]


def drop_summary(conn: sqlite3.Connection, drop_id: str, include_cost: bool) -> Optional[dict]:
    """List-row summary for a drop. NEVER includes dollars (list view)."""
    d = conn.execute(
        "SELECT drop_id, project_code, elevation, sequence_no, window_count, lifecycle, "
        "structural_signoff_at, closed_at FROM drops WHERE drop_id=?", (drop_id,)).fetchone()
    if not d:
        return None
    out = dict(d)
    out["progress"] = drop_progress(conn, drop_id)
    out["current_stage"] = current_stage(conn, drop_id)
    out["working_days"] = drop_working_days(conn, drop_id)
    # #224 — patch volume to date so the By-Elevation cards + progress hero can show
    # CF without one detail call per drop. No schema change: summed straight from the
    # quantity_entries ledger (the single source of truth). full precision + ceil-tenth.
    # #352 — concrete patches and column repairs are totalled apart (volume_to_date keeps
    # meaning "patch CF"; column_repair_to_date is the new line).
    r = conn.execute(
        "SELECT SUM(CASE WHEN s.sov_code = ? THEN 0 ELSE q.volume_cf END), "
        "       SUM(CASE WHEN s.sov_code = ? THEN q.volume_cf ELSE 0 END) "
        "FROM quantity_entries q LEFT JOIN sov_line_items s ON s.sov_id = q.sov_line_item "
        "WHERE q.drop_id=? AND q.volume_cf IS NOT NULL",
        (COLUMN_REPAIR_CODE, COLUMN_REPAIR_CODE, drop_id)).fetchone()
    vt, cr = (r[0], r[1]) if r else (None, None)
    out["volume_to_date"] = round(vt, 6) if vt is not None else 0.0
    out["volume_to_date_display"] = ceil_tenth(vt) if vt is not None else 0.0
    out["column_repair_to_date"] = round(cr, 6) if cr else 0.0
    out["column_repair_to_date_display"] = ceil_tenth(cr) if cr else 0.0
    return out


def drop_detail(conn: sqlite3.Connection, drop_id: str, include_cost: bool) -> Optional[dict]:
    """Full drop detail. include_cost=False OMITS cost + expense keys entirely."""
    out = drop_summary(conn, drop_id, include_cost)
    if out is None:
        return None
    steps = _rows(
        conn,
        "SELECT ds.step_no, st.name, ds.status, ds.started_on, ds.completed_on, "
        "ds.working_days_actual, st.default_working_days, st.is_cure_gate, st.is_signoff_gate "
        "FROM drop_stage_status ds "
        "JOIN drops d ON d.drop_id=ds.drop_id "
        "JOIN stage_templates t ON t.project_code=d.project_code "
        "JOIN stage_template_steps st ON st.template_id=t.template_id AND st.step_no=ds.step_no "
        "WHERE ds.drop_id=? ORDER BY ds.step_no", (drop_id,))
    out["stages"] = [dict(s) for s in steps]
    out["quantity_totals"] = drop_quantity_totals(conn, drop_id)
    if include_cost:
        out["cost"] = drop_cost(conn, drop_id)        # float or "pending_rates"
        out["expense_total"] = drop_expense_total(conn, drop_id)
    # else: dollar keys intentionally ABSENT (omit-not-zero, §6 / #158)
    return out


def project_rollup(conn: sqlite3.Connection, project_code: str, include_cost: bool) -> dict:
    """Project-level roll-up. Quantities always; spend/expenses only when
    include_cost (keys omitted otherwise)."""
    drop_ids = [r["drop_id"] for r in _rows(
        conn, "SELECT drop_id FROM drops WHERE project_code=? ORDER BY sequence_no", (project_code,))]

    # aggregate progress = total complete / total applicable across drops
    comp = appl = 0
    for did in drop_ids:
        p = drop_progress(conn, did)
        comp += p["complete_steps"]
        appl += p["applicable_steps"]
    overall_pct = round(comp / appl * 100, 1) if appl else 0.0

    qty_by_sov = []
    for r in _rows(
        conn,
        "SELECT s.sov_code, s.unit, SUM(q.quantity) AS qty_total, "
        "SUM(q.volume_cf) AS volume_total "
        "FROM quantity_entries q "
        "JOIN sov_line_items s ON s.sov_id=q.sov_line_item "
        "JOIN drops d ON d.drop_id=q.drop_id "
        "WHERE d.project_code=? GROUP BY s.sov_id ORDER BY s.sov_code", (project_code,)):
        d = dict(r)
        vt = d.get("volume_total")
        d["volume_total"] = round(vt, 6) if vt is not None else None
        d["volume_total_display"] = ceil_tenth(vt)
        if d.get("qty_total") is not None:
            d["qty_total"] = round(d["qty_total"], 6)
        qty_by_sov.append(d)

    out = {
        "project_code": project_code,
        "drop_count": len(drop_ids),
        "overall_progress_pct": overall_pct,
        "quantity_by_sov": qty_by_sov,
    }
    if include_cost:
        # project spend = sum of drop costs; pending_rates if ANY drop is pending
        costs = [drop_cost(conn, did) for did in drop_ids]
        if any(c == PENDING_RATES for c in costs):
            out["total_spend"] = PENDING_RATES
        else:
            out["total_spend"] = round(sum(c for c in costs), 2)
        # #260 — round in Python (see drop_expense_total): no round(double, int) on Postgres.
        exp = conn.execute(
            "SELECT SUM(amount) FROM expense_entries WHERE project_code=?",
            (project_code,)).fetchone()[0]
        out["total_expenses"] = round(exp, 2) if exp is not None else 0.0
    return out


def progress_pair(conn: sqlite3.Connection, drop_id: str,
                  project_code: Optional[str] = None) -> dict:
    """The ONE derived value the stage-write endpoint returns so every surface
    (drop-plan card, drop-report, progress widget/hero) recomputes off the same
    canonical stage-status source — never a stored/cached % that can go stale (#256).

    Returns the per-drop % AND the project overall %, both freshly derived from
    drop_stage_status via drop_progress / project_rollup. The overall % is the
    weighted rollup (Σ complete / Σ applicable across drops), so it matches the
    per-drop math exactly. project_code is looked up from the drop when omitted."""
    prog = drop_progress(conn, drop_id)
    if project_code is None:
        row = conn.execute("SELECT project_code FROM drops WHERE drop_id=?", (drop_id,)).fetchone()
        project_code = row["project_code"] if row else None
    overall = (project_rollup(conn, project_code, include_cost=False)["overall_progress_pct"]
               if project_code else 0.0)
    return {
        "drop_pct": prog["pct"],
        "overall_pct": overall,
        "progress": prog,
        "project_code": project_code,
    }
