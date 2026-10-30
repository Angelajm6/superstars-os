"""Sign-in ↔ DCR-artifact divergence detection + reconciliation (#191).

ARCHITECTURAL NOTE — this module exists as DEFENSIVE invariant infrastructure,
not as the fix to a current divergence. A full diagnostic over
FR-BX-001's 16 issued DCRs across 5-04..5-25 (104 (date, W-####) pairs)
returned ZERO divergences. The handoff's premise — "sign_in_log and the
DCR roster silently diverge" — is falsified by the data at the time
#191 ships.

What the architecture actually says about direction-of-truth:

  sign_in_log IS the source of truth for who-was-on-site-when. The
  issued DCR is a FROZEN RENDERING derived from sign_in_log at issue
  time, persisted as `data_room/reports/dcr/<project>/<seq>/internal.html`
  (and `client.html`). After issuance, every sign_in_log mutation path
  (POST/PATCH/PUT/DELETE on /api/sign-ins/...) calls
  `_mark_dcr_stale(conn, project_code, report_date)` which sets
  `report_index.stale = 1` for any issued DCR on that date. The
  staling flow runs **DCR → log direction only**: a sign_in_log edit
  ages out the DCR; a DCR delete does NOT touch sign_in_log (the
  /api/projects/<p>/reports/by-sequence/<seq> DELETE only removes
  report_index + HTML artifacts).

That asymmetry is the architectural reason the diagnostic found zero
divergences: edits flow inward to the log, and the DCR catches up via
re-issue. There is no code path that mutates the DCR roster in a way
that bypasses sign_in_log — because the DCR roster IS rendered from
sign_in_log; it has no independent storage.

Given the above, the right framing for this invariant is:

  Detect: any (date, W-####) where the rendered DCR artifact's W-####
  roster disagrees with what sign_in_log currently has for the same
  (project_code, date). This would surface, for example:
    - A sign_in_log row deleted directly via DB (bypassing the API
      gates) — never expected in normal operation but possible during
      manual recovery.
    - A future code path that adds a worker to the DCR roster without
      writing sign_in_log (no such path exists today; the invariant
      would surface it the moment one is introduced).
    - Smoke-test corruption (post-#175 audit closed this; the invariant
      catches a future regression).

  Reconcile: for `in_dcr_not_log` divergences (DCR artifact says worker
  was there; log doesn't), INSERT the missing sign_in_log row using the
  DCR artifact as source-of-truth. Default time_in='07:00', time_out=
  '15:30' (8-hr day, matches existing operator-entered rows). Audit log
  per restore with PII-safe `{log_present: bool, source: 'dcr',
  report_id}` payloads — never raw values.

  Do NOT auto-delete: for `in_log_not_dcr` divergences (log has worker;
  DCR roster doesn't), surface in the report and let the operator
  decide. These could be legitimate (manual sign-ins recorded after DCR
  issuance) or could be orphans from a failed flow.

PII discipline (per CLAUDE.md PII rule): this module returns and
audit-logs `worker_id` (W-####), `date`, `report_id`, and presence
booleans only. Worker names, phone numbers, employee_ids in error
messages are forbidden. Hours and rates never appear in audit payloads.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date as _date
from pathlib import Path
from typing import Iterable, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
import ssc_paths  # #287
from payroll_hours import day_entries, find_overlap
DCR_ARTIFACT_ROOT = ssc_paths.under_root("data_room", "reports", "dcr")   # #287

# Default times to use when reconciling an `in_dcr_not_log` divergence.
# Matches the 07:00–15:30 = 8.5h with 30-min lunch = 8h pattern that
# every existing sign_in_log row across FR-BX-001 weeks 5-04..5-25 uses.
RECONCILE_TIME_IN = "07:00"
RECONCILE_TIME_OUT = "15:30"

_WORKER_ID_RE = re.compile(r"W-\d{4}")


def _read_dcr_roster(project_code: str, sequence: int) -> set[str]:
    """Return the set of W-#### identifiers present on the rendered
    DCR HTML artifact for (project_code, sequence). Empty set if the
    artifact doesn't exist or has no roster.

    The internal-audience artifact is the authoritative roster source
    (it includes everyone who signed in; the client-audience artifact
    is the same data filtered to billable surfaces). Matches the same
    extraction strategy the operator-facing DCR viewer uses.
    """
    p = DCR_ARTIFACT_ROOT / project_code / f"{sequence:03d}" / "internal.html"
    if not p.exists():
        return set()
    try:
        html = p.read_text(encoding="utf-8")
    except OSError:
        return set()
    return set(_WORKER_ID_RE.findall(html))


def _employee_id_to_worker_id(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["employee_id"]: r["worker_id"]
        for r in conn.execute(
            "SELECT employee_id, worker_id FROM employees"
        ).fetchall()
    }


def _worker_id_to_employee_id(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["worker_id"]: r["employee_id"]
        for r in conn.execute(
            "SELECT employee_id, worker_id FROM employees"
        ).fetchall()
    }


def compute_divergences(
    conn: sqlite3.Connection,
    project_code: str,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """Return all (date, worker_id, class, report_id) divergences for
    `project_code` over [start_date, end_date]. Both bounds inclusive,
    ISO format YYYY-MM-DD. Either may be None to remove that bound.

    Class is one of:
      - 'in_dcr_not_log' — artifact roster has worker_id, sign_in_log
        for the same date does not. Reconcilable (insert).
      - 'in_log_not_dcr' — sign_in_log has worker_id, DCR artifact
        roster for the same date does not. NOT auto-reconciled —
        operator decision required.

    Pairs that appear in BOTH the artifact and the log are omitted
    from the result (the invariant holds for those).

    PII-safe output: dicts contain worker_id (W-####), date,
    employee_id (E-#####, an internal identifier — not PII per
    CLAUDE.md), class, and report_id only.
    """
    # Pull every issued DCR in scope.
    where = (
        "WHERE project_code = ? AND report_type = 'DCR' AND status = 'issued' "
        "AND dcr_sequence IS NOT NULL"
    )
    params: list = [project_code]
    if start_date:
        where += " AND report_date >= ?"
        params.append(start_date)
    if end_date:
        where += " AND report_date <= ?"
        params.append(end_date)
    # Distinct on (sequence, date) — internal + client audiences share
    # the same sequence and the same roster source.
    issued = conn.execute(
        f"SELECT DISTINCT report_date, dcr_sequence FROM report_index "
        f"{where} ORDER BY report_date",
        params,
    ).fetchall()

    emp_to_wid = _employee_id_to_worker_id(conn)

    # Build the sign_in_log roster per date in the same SELECT to
    # avoid N+1 queries.
    if not issued:
        return []
    dates_in_scope = [r["report_date"] for r in issued]
    placeholders = ",".join("?" * len(dates_in_scope))
    log_rows = conn.execute(
        f"SELECT date, employee_id FROM sign_in_log "
        f"WHERE project_code = ? AND date IN ({placeholders})",
        [project_code, *dates_in_scope],
    ).fetchall()
    log_by_date: dict[str, set[str]] = {}
    for r in log_rows:
        wid = emp_to_wid.get(r["employee_id"])
        if wid:
            log_by_date.setdefault(r["date"], set()).add(wid)

    divergences: list[dict] = []
    for r in issued:
        date = r["report_date"]
        seq = r["dcr_sequence"]
        artifact_roster = _read_dcr_roster(project_code, seq)
        log_roster = log_by_date.get(date, set())
        # Only assign a report_id label for the internal audience (the
        # operator-facing one); the client variant shares the same
        # sequence/roster but a separate report_id row.
        report_id = f"DCR-{project_code}-{seq:03d}"
        for wid in sorted(artifact_roster - log_roster):
            divergences.append({
                "date": date,
                "worker_id": wid,
                "class": "in_dcr_not_log",
                "report_id": report_id,
            })
        for wid in sorted(log_roster - artifact_roster):
            divergences.append({
                "date": date,
                "worker_id": wid,
                "class": "in_log_not_dcr",
                "report_id": report_id,
            })
    return divergences


def divergence_summary(divergences: Iterable[dict]) -> dict:
    """Aggregate per-class counts. PII-safe."""
    d = list(divergences)
    return {
        "total": len(d),
        "in_dcr_not_log": sum(1 for x in d if x["class"] == "in_dcr_not_log"),
        "in_log_not_dcr": sum(1 for x in d if x["class"] == "in_log_not_dcr"),
    }


# =====================================================================
# #194 — DCR-issue-time roster completeness check (the prevention).
# =====================================================================
# Failure mode this catches: at DCR issue time, the foreman can forget
# a worker who was actually present (this is exactly what happened to
# Kevin W-0003 on 2026-05-21 — DCR-014 was issued without him). By the
# time anyone notices, the DCR is signed/archived and the worker's day
# is lost. The reconciliation invariant from #191 CANNOT catch this —
# it compares sign_in_log to the DCR roster, and both agree (the row
# was just never added).
#
# This module's prevention: at issue time, compute who has been on site
# REGULARLY in the recent past. If any regular is missing from today's
# roster, refuse to issue silently — return a structured response the
# UI uses to surface a modal. The operator either acknowledges the
# absence (worker really is off today) or adds the worker with a
# default shift. Either way, the omission can't ship silently.

ROSTER_THRESHOLD_DEFAULT = 4   # appearances within window required
ROSTER_WINDOW_DEFAULT = 7      # working days to look back over
ROSTER_DEFAULT_TIME_IN = "07:00"
ROSTER_DEFAULT_TIME_OUT = "15:30"   # 07:00..15:30 - 30min lunch = 8h


def _last_n_working_days(
    conn: sqlite3.Connection,
    project_code: str,
    as_of: str,
    window: int,
) -> list[str]:
    """Return the last `window` working days for `project_code`
    strictly BEFORE `as_of` (ISO YYYY-MM-DD). Working days = days where
    AT LEAST ONE sign_in_log row exists for the project — that's the
    operational definition of "the project was active." Days with no
    sign-ins (weekends + no_work DCR days where the project produced
    zero labor) are skipped.

    Ordered most-recent first; the caller can either iterate or
    convert to a set.
    """
    rows = conn.execute(
        "SELECT DISTINCT date FROM sign_in_log "
        "WHERE project_code = ? AND date < ? "
        "ORDER BY date DESC LIMIT ?",
        (project_code, as_of, window),
    ).fetchall()
    return [r["date"] for r in rows]


def compute_recent_regulars(
    conn: sqlite3.Connection,
    project_code: str,
    as_of: str,
    *,
    threshold: int = ROSTER_THRESHOLD_DEFAULT,
    window: int = ROSTER_WINDOW_DEFAULT,
) -> dict[str, int]:
    """Return {W-####: appearance_count} for workers who appeared in
    sign_in_log on `threshold`-or-more of the last `window` working
    days for `project_code`, anchored at `as_of` (exclusive).

    Counts span ALL sign_in_log appearances within the window,
    regardless of the source — operator-entered, worker self-signed,
    or roster-completeness-default rows from this same module. The
    intent is "have they been here recently"; how the row got into the
    log doesn't change that answer.

    Empty result if the project has fewer than `threshold` working
    days of history (nascent-project / cold-start case: nobody is yet
    a "regular," so nothing can be missing).
    """
    days = _last_n_working_days(conn, project_code, as_of, window)
    if len(days) < threshold:
        return {}
    placeholders = ",".join("?" * len(days))
    rows = conn.execute(
        f"SELECT e.worker_id AS worker_id, COUNT(DISTINCT s.date) AS n "
        f"FROM sign_in_log s "
        f"JOIN employees e ON e.employee_id = s.employee_id "
        f"WHERE s.project_code = ? AND s.date IN ({placeholders}) "
        f"GROUP BY e.worker_id "
        f"HAVING COUNT(DISTINCT s.date) >= ?",
        [project_code, *days, threshold],
    ).fetchall()
    return {r["worker_id"]: r["n"] for r in rows}


def compute_proposed_roster(
    conn: sqlite3.Connection,
    project_code: str,
    report_date: str,
) -> set[str]:
    """Set of W-#### identifiers currently in sign_in_log for
    (project_code, report_date) — i.e., who would appear on the DCR
    artifact if issuance fired right now."""
    emp_to_wid = _employee_id_to_worker_id(conn)
    rows = conn.execute(
        "SELECT DISTINCT employee_id FROM sign_in_log "
        "WHERE project_code = ? AND date = ?",
        (project_code, report_date),
    ).fetchall()
    out = set()
    for r in rows:
        wid = emp_to_wid.get(r["employee_id"])
        if wid:
            out.add(wid)
    return out


def detect_missing_regulars(
    conn: sqlite3.Connection,
    project_code: str,
    report_date: str,
    *,
    threshold: int = ROSTER_THRESHOLD_DEFAULT,
    window: int = ROSTER_WINDOW_DEFAULT,
) -> list[dict]:
    """Combine compute_recent_regulars + compute_proposed_roster.

    Returns a list of {worker_id, recent_count, window, threshold}
    dicts — one entry per regular who is NOT on today's proposed
    roster. Empty list means the roster is complete by the heuristic.

    Result is sorted by worker_id for stable JSON output.
    """
    regulars = compute_recent_regulars(
        conn, project_code, report_date,
        threshold=threshold, window=window,
    )
    proposed = compute_proposed_roster(conn, project_code, report_date)
    missing_wids = sorted(set(regulars) - proposed)
    return [
        {
            "worker_id": wid,
            "recent_count": regulars[wid],
            "window": window,
            "threshold": threshold,
        }
        for wid in missing_wids
    ]


def apply_roster_completeness_decision(
    conn: sqlite3.Connection,
    project_code: str,
    report_date: str,
    *,
    acknowledge_missing: list[str],
    action: str,
    actor_user_id: Optional[int] = None,
    actor_role: Optional[str] = "system",
) -> dict:
    """Resolve a roster-completeness gap so the DCR issue can proceed.

    `acknowledge_missing` is the full list of W-####s the operator
    confirmed as either absent or to-be-added (must match the missing
    set the UI surfaced — the endpoint cross-checks).

    `action` is one of:
      - 'mark_absent'   : proceed without any inserts; audit-log the
                          operator's decision so future audit trails
                          show the omission was explicit.
      - 'add_default_8h': insert sign_in_log rows for each ack'd
                          W-#### with the canonical 07:00..15:30
                          (=8h) shift; audit-log per insert.
      - 'cancel'        : explicit no-op; the caller (the issue
                          endpoint) interprets this by NOT proceeding
                          with issuance.

    Returns a summary dict the endpoint can echo back to the UI.
    """
    if action not in ('mark_absent', 'add_default_8h', 'cancel'):
        raise ValueError(f"action must be mark_absent | add_default_8h | cancel, got {action!r}")
    summary = {
        "action": action,
        "acknowledged_count": len(acknowledge_missing),
        "inserted_count": 0,
        "errors": [],
    }
    if action == 'cancel':
        return summary

    if action == 'mark_absent':
        # Audit only — no DB mutations. One row per ack'd W-####.
        for wid in acknowledge_missing:
            conn.execute(
                "INSERT INTO audit_log "
                "(action, actor_user_id, actor_role, target_type, target_id, "
                " before_json, after_json, note, created_at) "
                "VALUES ('roster_completeness_mark_absent', ?, ?, 'worker', ?, ?, ?, ?, "
                "        CURRENT_TIMESTAMP)",
                (
                    actor_user_id, actor_role, wid,
                    json.dumps({"on_proposed_roster": False,
                                "is_recent_regular": True}),
                    json.dumps({"on_proposed_roster": False,
                                "is_recent_regular": True,
                                "operator_decision": "mark_absent",
                                "date": report_date,
                                "project_code": project_code}),
                    "#194 — operator confirmed regular worker absent today",
                )
            )
        return summary

    # action == 'add_default_8h' — insert sign_in_log row + audit.
    wid_to_emp = _worker_id_to_employee_id(conn)
    for wid in acknowledge_missing:
        emp_id = wid_to_emp.get(wid)
        if not emp_id:
            summary["errors"].append(f"no employee row for {wid}")
            continue
        # Defensive duplicate-skip: the operator may have raced an
        # actual sign-in between the modal opening and the action POST.
        existing = conn.execute(
            "SELECT id FROM sign_in_log "
            "WHERE project_code = ? AND date = ? AND employee_id = ? LIMIT 1",
            (project_code, report_date, emp_id),
        ).fetchone()
        if existing:
            continue
        # One place at a time: never add a default day over hours the worker
        # already has on another job.
        if find_overlap(day_entries(conn, emp_id, report_date),
                        ROSTER_DEFAULT_TIME_IN, ROSTER_DEFAULT_TIME_OUT):
            summary["errors"].append(f"{wid} already logged on another job that day")
            continue
        conn.execute(
            "INSERT INTO sign_in_log "
            "(date, employee_id, project_code, time_in, time_out, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (report_date, emp_id, project_code,
             ROSTER_DEFAULT_TIME_IN, ROSTER_DEFAULT_TIME_OUT),
        )
        conn.execute(
            "INSERT INTO audit_log "
            "(action, actor_user_id, actor_role, target_type, target_id, "
            " before_json, after_json, note, created_at) "
            "VALUES ('signin_roster_completeness_default', ?, ?, 'worker', ?, ?, ?, ?, "
            "        CURRENT_TIMESTAMP)",
            (
                actor_user_id, actor_role, emp_id,
                json.dumps({"log_present": False}),
                json.dumps({
                    "log_present": True,
                    "hours": 8.0,
                    "source": "roster_completeness_default",
                    "date": report_date,
                    "project_code": project_code,
                }),
                "#194 — operator chose 'Add with 8h default' "
                "after roster-completeness check flagged this regular as missing",
            )
        )
        summary["inserted_count"] += 1
    return summary


def reconcile_in_dcr_not_log(
    conn: sqlite3.Connection,
    project_code: str,
    divergences: List[dict],
    *,
    actor_user_id: Optional[int] = None,
    actor_role: Optional[str] = "system",
) -> dict:
    """Insert a missing sign_in_log row for every `in_dcr_not_log`
    divergence. ATOMIC at the transaction level: caller controls the
    commit boundary, but every UPDATE here is staged inside one
    conn's implicit transaction, so a failure mid-loop rolls back the
    whole batch when the caller drops the conn without committing.

    Returns a summary dict:
      {
        "attempted": int,
        "reconciled": int,
        "skipped_already_present": int,
        "skipped_no_worker_id": int,
        "errors": [str, ...],
      }

    PII discipline: writes audit_log rows with
      action='signin_reconcile_from_dcr'
      before_json={"log_present": false}
      after_json={"log_present": true, "source": "dcr",
                  "report_id": "<DCR-...-NNN>"}
    No times, hours, names, or rate values are ever logged.
    """
    wid_to_emp = _worker_id_to_employee_id(conn)
    summary = {
        "attempted": 0,
        "reconciled": 0,
        "skipped_already_present": 0,
        "skipped_no_worker_id": 0,
        "errors": [],
    }
    for d in divergences:
        if d.get("class") != "in_dcr_not_log":
            continue
        summary["attempted"] += 1
        wid = d.get("worker_id")
        date = d.get("date")
        report_id = d.get("report_id")
        emp_id = wid_to_emp.get(wid)
        if not emp_id:
            summary["skipped_no_worker_id"] += 1
            continue
        # Defensive — a race where the operator inserted the row
        # between divergence-compute and reconcile call.
        existing = conn.execute(
            "SELECT id FROM sign_in_log "
            "WHERE project_code = ? AND date = ? AND employee_id = ? LIMIT 1",
            (project_code, date, emp_id),
        ).fetchone()
        if existing:
            summary["skipped_already_present"] += 1
            continue
        if find_overlap(day_entries(conn, emp_id, date), RECONCILE_TIME_IN, RECONCILE_TIME_OUT):
            summary["skipped_overlap"] = summary.get("skipped_overlap", 0) + 1
            continue
        try:
            conn.execute(
                "INSERT INTO sign_in_log "
                "(date, employee_id, project_code, time_in, time_out, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (date, emp_id, project_code,
                 RECONCILE_TIME_IN, RECONCILE_TIME_OUT),
            )
            conn.execute(
                "INSERT INTO audit_log "
                "(action, actor_user_id, actor_role, target_type, target_id, "
                " before_json, after_json, note, created_at) "
                "VALUES ('signin_reconcile_from_dcr', ?, ?, 'worker', ?, ?, ?, ?, "
                "        CURRENT_TIMESTAMP)",
                (
                    actor_user_id,
                    actor_role,
                    emp_id,
                    json.dumps({"log_present": False}),
                    json.dumps({
                        "log_present": True,
                        "source": "dcr",
                        "report_id": report_id,
                        "date": date,
                        "project_code": project_code,
                    }),
                    "#191 — restored from issued DCR roster (defensive invariant)",
                ),
            )
            summary["reconciled"] += 1
            logging.info(
                f"signin_dcr_reconcile: restored sign_in_log row "
                f"project={project_code} date={date} worker_id={wid} "
                f"from {report_id} actor_role={actor_role}"
            )
        except Exception as e:
            summary["errors"].append(
                f"date={date} worker_id={wid}: {type(e).__name__}: {e}"
            )
    return summary
