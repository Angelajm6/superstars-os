"""#276 — Estimating-division core (blueprint Build B): the estimator's workspace
queue, the estimating sub-stage machine, the VP's table, SLA aging + attention, and
internal notifications. Bound visual reference: MOCKUP_estimating_workspace_v2.html.

MACRO/MICRO CONTRACT: #273's `status` stays the macro lifecycle, untouched
(intake|scoping|submitted|approved|lost|converted). `est_stage` is the ESTIMATING
sub-machine — received -> walkthrough_scheduled -> walkthrough_done ->
proposal_draft -> sent_to_vp — active ONLY while status='scoping'. sent_to_vp + VP
approve drives the macro exactly as #273 built it: the approve action walks the two
LEGAL macro transitions (scoping->submitted, submitted->approved) through
estimates.change_status, so #273's validation, stamps and activity rows all run.

ACCESS (blueprint §5): the QUEUE surfaces (/estimating + /api/estimating/queue +
stage clicks) allow estimator/admin/c_suite via access.SECTION_ACCESS['estimating'];
the VP table + nudge + assign + approve stay 'estimates' (admin/c_suite ONLY). The
estimator sees the leads they work — including their amounts (they ENTER the
proposal amount) — but NO CRM breadth, NO company endpoints, NO rollup financials
(the workspace hero carries counts, never $; the $ band lives on the VP table).

AGING (SLA): derived from est_stage_changed_at at READ time — never stored flags.
Thresholds are code constants below, documented for a later admin UI.

Dates LOCAL. All SQL parameterized via db_layer — identical on SQLite and Postgres.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Response, jsonify, request

import access
import crm
import estimates
import notifications
from auth import _db, current_user, requires_section

SCRIPT_DIR = Path(__file__).resolve().parent
ESTIMATING_PAGE = SCRIPT_DIR / "estimating.html"

# ===================== SLA CONSTANTS (single documented block) =====================
# The attention thresholds. An estimating stage older than STAGE_SLA_DAYS shows the
# red attention glow and enters the VP's "stalled" list; a proposal sitting on the
# VP's table longer than VP_SLA_DAYS glows there too. Derived from
# est_stage_changed_at at read time (never stored). A later admin Settings UI edits
# THESE two numbers — keep them here, nowhere else.
STAGE_SLA_DAYS = 7
VP_SLA_DAYS = 3

# ===================== the estimating sub-stage machine =====================
EST_STAGES = ("received", "walkthrough_scheduled", "walkthrough_done",
              "proposal_draft", "sent_to_vp")
_STAGE_IDX = {s: i for i, s in enumerate(EST_STAGES)}
# one step forward, one step back (VP kick-back = sent_to_vp -> proposal_draft)
_STAGE_MOVES = {
    "received": {"walkthrough_scheduled"},
    "walkthrough_scheduled": {"walkthrough_done", "received"},
    "walkthrough_done": {"proposal_draft", "walkthrough_scheduled"},
    "proposal_draft": {"sent_to_vp", "walkthrough_done"},
    "sent_to_vp": {"proposal_draft"},
}

DIVISIONS = ("facade", "rope_access", "interior", "parking_garage", "other")
DIVISION_LABEL = {"facade": "Facade", "rope_access": "Rope Access",
                  "interior": "Interior", "parking_garage": "Parking Garage", "other": "Other"}
INQUIRY_KINDS = ("bid", "po", "undetermined")
RA_SUBTYPES = ("inspection", "work")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _uid():
    return (current_user() or {}).get("id")


def _days_since(iso_ts, today=None) -> int:
    today = today or date.today()
    try:
        return max(0, (today - date.fromisoformat((iso_ts or "")[:10])).days)
    except Exception:
        return 0


# ===================== aging + the 5-segment pipeline strip =====================

def lead_age_days(row, today=None) -> int:
    """Age in the CURRENT position: est_stage_changed_at while the sub-machine is
    active (status=scoping), else status_changed_at (intake/submitted/...)."""
    if row.get("status") == "scoping" and row.get("est_stage_changed_at"):
        return _days_since(row["est_stage_changed_at"], today)
    return _days_since(row.get("status_changed_at") or row.get("created_at"), today)


def lead_overdue(row, today=None) -> bool:
    """The attention rule: the current position has aged past its SLA."""
    age = lead_age_days(row, today)
    if row.get("status") == "scoping" and row.get("est_stage") == "sent_to_vp":
        return age > VP_SLA_DAYS
    if row.get("status") in ("intake", "scoping"):
        return age > STAGE_SLA_DAYS
    return False


def pipe_state(row, today=None) -> list:
    """The per-lead PIPELINE STRIP (mockup): 5 segments — Intake, Walkthrough,
    Proposal, VP, Submitted — each 'done'|'now'|'late'|'todo'. Green behind you,
    blue where you are, coral when the current stage is overdue, oat ahead.
    ONE mapping, served to both pages and asserted by the guard."""
    status = row.get("status")
    stage = row.get("est_stage")
    late = lead_overdue(row, today)
    cur = "late" if late else "now"
    if status == "intake":
        return [cur, "todo", "todo", "todo", "todo"]
    if status == "scoping":
        if stage in ("received", "walkthrough_scheduled", None):
            return ["done", cur, "todo", "todo", "todo"]
        if stage in ("walkthrough_done", "proposal_draft"):
            return ["done", "done", cur, "todo", "todo"]
        return ["done", "done", "done", cur, "todo"]          # sent_to_vp
    if status == "submitted":
        return ["done", "done", "done", "done", "now"]
    if status in ("approved", "converted"):
        return ["done", "done", "done", "done", "done"]
    return ["todo", "todo", "todo", "todo", "todo"]           # lost / anything else


def stage_action(row) -> dict:
    """The card's primary action, server-decided so the UI can't drift from the
    machine: {action, label} (action names map to endpoints/modals client-side)."""
    status, stage = row.get("status"), row.get("est_stage")
    if status == "intake":
        return {"action": "start", "label": "Start estimating"}
    if status != "scoping":
        return {}
    return {
        "received": {"action": "schedule_walkthrough", "label": "Schedule walkthrough"},
        "walkthrough_scheduled": {"action": "walkthrough_done", "label": "Mark walkthrough done"},
        "walkthrough_done": {"action": "proposal_draft", "label": "Start proposal"},
        "proposal_draft": {"action": "send_to_vp", "label": "Send to VP"},
        "sent_to_vp": {"action": "", "label": "With VP"},
    }.get(stage or "received", {})


# ===================== the stage machine (shared with the smoke) =====================

def _apply_stage(conn, est, new_stage, *, actor_user_id=None, extra_sets=None,
                 extra_params=None) -> dict:
    """The MACHINE PRIMITIVE: one adjacency-validated est_stage move + stamps +
    activity + notifications. #277 splits this out of advance_stage so the
    walkthrough-visit layer drives the SAME machine (schedule -> scheduled,
    done -> done, cancel -> revert) with no parallel state. Raises ValueError."""
    old = est.get("est_stage") or "received"
    new_stage = (new_stage or "").strip().lower()
    if est["status"] != "scoping":
        raise ValueError("the estimating stage machine is active only while status='scoping'")
    if new_stage not in EST_STAGES:
        raise ValueError(f"unknown est_stage: {new_stage}")
    if new_stage not in _STAGE_MOVES.get(old, set()):
        raise ValueError(f"illegal stage move: {old} -> {new_stage}")
    sets = ["est_stage=?", "est_stage_changed_at=?", "updated_at=?"] + list(extra_sets or [])
    params = [new_stage, _now(), _now()] + list(extra_params or [])
    params.append(est["id"])
    conn.execute(f"UPDATE estimate SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    crm.add_activity(conn, entity_type="organization", entity_id=est["client_org_id"],
                     activity_type="system", author_user_id=actor_user_id, function_tag="sales",
                     summary=f"Estimate {est['code']}: estimating {old} → {new_stage}")
    est2 = estimates.get_estimate(conn, est["id"])
    assignee = est2.get("assigned_estimator")
    if assignee and assignee != actor_user_id:
        notifications.notify_user(
            conn, kind="stage", user_id=assignee,
            subject=f"[SSC] {est2['code']} moved to {new_stage.replace('_', ' ')}",
            estimate_code=est2["code"],
            extra_line=f"{est2.get('building_address') or ''}".strip())
    if new_stage == "sent_to_vp":
        notifications.notify_vps(
            conn, kind="vp_review",
            subject=f"[SSC] {est2['code']} awaits your approval",
            estimate_code=est2["code"], path="/",
            extra_line=f"{est2.get('building_address') or ''}".strip())
    return est2


def advance_stage(conn, est_id, new_stage, *, walkthrough_date=None, final_amount=None,
                  qb_estimate_ref=None, attendee_user_id=None, actor_user_id=None) -> dict:
    """Apply one estimating sub-stage move. Server-validated: adjacency only;
    active only while status='scoping'. Amounts may ride along to proposal_draft /
    sent_to_vp (the estimator enters the proposal amount — blueprint §5).

    #277: `walkthrough_scheduled` DELEGATES to the visit layer — it creates a real
    walkthrough_visit (date required as before; attendee defaults to the actor —
    someone from the company must attend) and the visit layer advances this machine
    + derives estimate.walkthrough_date. `walkthrough_done` also marks the earliest
    scheduled visit done, so visit state and stage never diverge."""
    est = estimates.get_estimate(conn, est_id)
    if not est:
        raise LookupError("estimate not found")
    new_stage = (new_stage or "").strip().lower()
    if new_stage == "walkthrough_scheduled":
        import walkthroughs   # lazy — walkthroughs imports this module at top level
        wd = (walkthrough_date or "").strip()
        try:
            datetime.strptime(wd, "%Y-%m-%d")
        except (ValueError, TypeError):
            raise ValueError("walkthrough_scheduled requires walkthrough_date (YYYY-MM-DD)")
        walkthroughs.create_visit(conn, est_id, visit_date=wd,
                                  attendee_user_id=attendee_user_id or actor_user_id,
                                  actor_user_id=actor_user_id)
        return estimates.get_estimate(conn, est_id)

    extra_sets, extra_params = [], []
    if new_stage in ("proposal_draft", "sent_to_vp"):
        if final_amount is not None and str(final_amount).strip() != "":
            amt = float(final_amount)
            if amt < 0:
                raise ValueError("final_amount must be >= 0")
            extra_sets.append("final_amount=?"); extra_params.append(amt)
        if qb_estimate_ref is not None and str(qb_estimate_ref).strip() != "":
            extra_sets.append("qb_estimate_ref=?"); extra_params.append(str(qb_estimate_ref).strip())
    est2 = _apply_stage(conn, est, new_stage, actor_user_id=actor_user_id,
                        extra_sets=extra_sets, extra_params=extra_params)
    if new_stage == "walkthrough_done":
        import walkthroughs   # lazy
        walkthroughs.mark_next_scheduled_done(conn, est_id, actor_user_id=actor_user_id,
                                              sync_stage=False)
    return est2


def start_estimating(conn, est_id, *, actor_user_id=None) -> dict:
    """intake -> scoping through the #273 macro machine (unchanged); the sub-machine
    initializes at 'received' (estimates.change_status seeds it — one init site)."""
    return estimates.change_status(conn, est_id, "scoping", actor_user_id=actor_user_id)


def vp_approve(conn, est_id, *, final_amount, qb_estimate_ref, on_date=None,
               actor_user_id=None) -> dict:
    """The VP-table approve: requires est_stage='sent_to_vp', then walks the TWO
    legal #273 macro transitions (scoping->submitted, submitted->approved) through
    estimates.change_status — validation, date stamps and activity rows exactly as
    #273 built them. Approve is the submit click (blueprint §2 step 7)."""
    est = estimates.get_estimate(conn, est_id)
    if not est:
        raise LookupError("estimate not found")
    if est["status"] != "scoping" or (est.get("est_stage") or "") != "sent_to_vp":
        raise ValueError("only a proposal on the VP's table (sent_to_vp) can be approved here")
    estimates.change_status(conn, est_id, "submitted", on_date=on_date,
                            actor_user_id=actor_user_id)
    est2 = estimates.change_status(conn, est_id, "approved", on_date=on_date,
                                   final_amount=final_amount,
                                   qb_estimate_ref=qb_estimate_ref,
                                   actor_user_id=actor_user_id)
    if est.get("assigned_estimator"):
        notifications.notify_user(
            conn, kind="approved", user_id=est["assigned_estimator"],
            subject=f"[SSC] {est['code']} approved by the VP",
            estimate_code=est["code"])
    return est2


# ===================== payloads =====================

_LEAD_COLS = """e.id, e.code, e.est_type, e.borough, e.status, e.status_changed_at,
e.created_at, e.building_address, e.division, e.ra_subtype, e.inquiry_kind,
e.bid_due_date, e.est_stage, e.est_stage_changed_at, e.walkthrough_date,
e.assigned_estimator, e.final_amount, e.qb_estimate_ref, e.converted_project_code,
o.name AS org_name"""


def lead_public(row, today=None) -> dict:
    r = dict(row)
    ndocs = r.pop("_ndocs", None)
    nreports = r.pop("_nreports", None)
    age = lead_age_days(r, today)
    due = r.get("bid_due_date")
    due_days = None
    if due:
        try:
            due_days = (date.fromisoformat(due) - (today or date.today())).days
        except Exception:
            due_days = None
    return {
        "id": r["id"], "code": r["code"], "est_type": r["est_type"],
        "borough": r["borough"], "status": r["status"],
        "org_name": r.get("org_name"), "building_address": r.get("building_address"),
        "division": r.get("division"), "division_label": DIVISION_LABEL.get(r.get("division"), r.get("division")),
        "ra_subtype": r.get("ra_subtype"), "inquiry_kind": r.get("inquiry_kind"),
        "bid_due_date": due, "bid_due_days": due_days,
        "est_stage": r.get("est_stage"), "est_stage_changed_at": r.get("est_stage_changed_at"),
        "walkthrough_date": r.get("walkthrough_date"),
        "assigned_estimator": r.get("assigned_estimator"),
        "final_amount": r.get("final_amount"), "qb_estimate_ref": r.get("qb_estimate_ref"),
        "age_days": age, "overdue": lead_overdue(r, today),
        "pipe": pipe_state(r, today), "action": stage_action(r),
        "docs_count": ndocs, "report_count": nreports,
    }


def queue_payload(conn, today=None) -> dict:
    """The /estimating workspace: hero rollups + the actionable queue (intake +
    scoping). Hero counts include submitted (green: in follow-up) per the mockup —
    counts ONLY, never $ (rollup financials stay on the VP band, c_suite)."""
    today = today or date.today()
    rows = [dict(r) for r in conn.execute(
        f"SELECT {_LEAD_COLS} FROM estimate e JOIN crm_organization o ON o.id=e.client_org_id "
        "WHERE e.status IN ('intake','scoping','submitted') "
        "ORDER BY e.est_stage_changed_at, e.status_changed_at").fetchall()]
    ndocs = {r["estimate_id"]: r["n"] for r in conn.execute(
        "SELECT estimate_id, COUNT(*) AS n FROM estimate_document GROUP BY estimate_id").fetchall()}
    for r in rows:
        r["_ndocs"] = ndocs.get(r["id"], 0)
    # #277 — walkthrough report counts + the estimator's upcoming-walkthroughs list
    # (table-existence guarded so a #276-only DB still serves the queue).
    from apply_crm_266 import _table_exists
    has_wt = _table_exists(conn, "walkthrough_report")
    nreps = {}
    wt_upcoming = []
    if has_wt:
        nreps = {r["estimate_id"]: r["n"] for r in conn.execute(
            "SELECT estimate_id, COUNT(*) AS n FROM walkthrough_report GROUP BY estimate_id").fetchall()}
        import walkthroughs
        wt_upcoming = walkthroughs.upcoming_walkthroughs(conn)
    for r in rows:
        r["_nreports"] = nreps.get(r["id"], 0)

    intake = [r for r in rows if r["status"] == "intake"]
    scoping = [r for r in rows if r["status"] == "scoping"]
    submitted = [r for r in rows if r["status"] == "submitted"]
    stalled = [r for r in scoping if lead_overdue(r, today)]
    div_counts = {}
    for r in rows:
        d = r.get("division") or "other"
        div_counts[d] = div_counts.get(d, 0) + 1
    due_week = [r for r in rows if r["status"] in ("intake", "scoping") and r.get("bid_due_date")
                and 0 <= ((date.fromisoformat(r["bid_due_date"]) - today).days) <= 7]
    # queue order: attention first, then oldest-in-stage first
    cards = sorted((intake + scoping),
                   key=lambda r: (not lead_overdue(r, today), -lead_age_days(r, today)))
    return {
        "hero": {
            "open_total": len(rows),
            "submitted": len(submitted),
            "in_estimating": len(scoping) - len(stalled),
            "stalled": len(stalled),
            "intake": len(intake),
            "division_counts": div_counts,
            "bids_due_week": len(due_week),
        },
        "leads": [lead_public(r, today) for r in cards],
        "walkthroughs_upcoming": wt_upcoming,   # #277 — the estimator's schedule
        "sla": {"stage_days": STAGE_SLA_DAYS, "vp_days": VP_SLA_DAYS},
    }


def vp_table_payload(conn, today=None) -> dict:
    """The VP's table (admin/c_suite): awaiting-approval rows (sent_to_vp, days on
    table, red past VP_SLA_DAYS), stalled-in-estimating rows (past STAGE_SLA_DAYS),
    and the stat band (counts + open pipeline $ + bids due this week)."""
    today = today or date.today()
    rows = [dict(r) for r in conn.execute(
        f"SELECT {_LEAD_COLS} FROM estimate e JOIN crm_organization o ON o.id=e.client_org_id "
        "WHERE e.status IN ('intake','scoping','submitted')").fetchall()]
    # #277 — the VP reviews the walkthrough report beside the amount
    from apply_crm_266 import _table_exists
    if _table_exists(conn, "walkthrough_report"):
        nreps = {r["estimate_id"]: r["n"] for r in conn.execute(
            "SELECT estimate_id, COUNT(*) AS n FROM walkthrough_report GROUP BY estimate_id").fetchall()}
        for r in rows:
            r["_nreports"] = nreps.get(r["id"], 0)
    awaiting = [r for r in rows if r["status"] == "scoping" and r.get("est_stage") == "sent_to_vp"]
    stalled = [r for r in rows if r["status"] == "scoping" and r.get("est_stage") != "sent_to_vp"
               and lead_age_days(r, today) > STAGE_SLA_DAYS]
    awaiting.sort(key=lambda r: -lead_age_days(r, today))
    stalled.sort(key=lambda r: -lead_age_days(r, today))
    pipeline_amt = sum(r["final_amount"] or 0 for r in rows
                      if r["status"] in ("scoping", "submitted"))
    due_week = [r for r in rows if r["status"] in ("intake", "scoping") and r.get("bid_due_date")
                and 0 <= ((date.fromisoformat(r["bid_due_date"]) - today).days) <= 7]
    return {
        "band": {
            "awaiting": len(awaiting),
            "awaiting_oldest_days": max((lead_age_days(r, today) for r in awaiting), default=0),
            "stalled": len(stalled),
            "pipeline_amount": pipeline_amt,
            "bids_due_week": len(due_week),
        },
        "awaiting": [lead_public(r, today) for r in awaiting],
        "stalled": [lead_public(r, today) for r in stalled],
        "sla": {"stage_days": STAGE_SLA_DAYS, "vp_days": VP_SLA_DAYS},
    }


# ===================== endpoints =====================

_FORBIDDEN_HTML = (
    "<!doctype html><meta charset=utf-8><title>Not authorized</title>"
    "<div style=\"font-family:Inter,system-ui,sans-serif;max-width:520px;margin:18vh auto;"
    "text-align:center;color:#222633\">"
    "<div style=\"font-size:15px;font-weight:700;color:#B11E2E;letter-spacing:.5px\">NOT AUTHORIZED</div>"
    "<p style=\"color:#76777E;font-size:14px;line-height:1.6;margin:14px 0 22px\">"
    "The estimating workspace is limited to the estimating team and C-suite.</p></div>"
)


def _serve_estimating_page():
    """GET /estimating — the workspace page (estimator/admin/c_suite; blueprint §5).
    The login gate has already authenticated; clients were routed by their gate."""
    role = (current_user() or {}).get("role")
    if not access.can_access("estimating", role):
        return Response(_FORBIDDEN_HTML, status=403, mimetype="text/html")
    import ui_version                                        # #279
    html = ui_version.resolve_page(ESTIMATING_PAGE).read_text(encoding="utf-8")
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@requires_section('estimating')
def _api_queue():
    conn = _db()
    try:
        return jsonify({"data": queue_payload(conn)})
    finally:
        conn.close()


@requires_section('estimating')
def _api_start(est_id):
    conn = _db()
    try:
        est = start_estimating(conn, est_id, actor_user_id=_uid())
        return jsonify({"data": estimates.est_public(est)})
    except LookupError:
        return jsonify({"error": "not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()


@requires_section('estimating')
def _api_stage(est_id):
    d = request.get_json(silent=True) or {}
    conn = _db()
    try:
        est = advance_stage(conn, est_id, d.get("stage"),
                            walkthrough_date=d.get("walkthrough_date"),
                            final_amount=d.get("final_amount"),
                            qb_estimate_ref=d.get("qb_estimate_ref"),
                            actor_user_id=_uid())
        return jsonify({"data": lead_public({**est, "org_name": None})})
    except LookupError:
        return jsonify({"error": "not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()


@requires_section('estimates')
def _api_vp_table():
    conn = _db()
    try:
        return jsonify({"data": vp_table_payload(conn)})
    finally:
        conn.close()


@requires_section('estimates')
def _api_vp_approve(est_id):
    d = request.get_json(silent=True) or {}
    conn = _db()
    try:
        est = vp_approve(conn, est_id, final_amount=d.get("final_amount"),
                         qb_estimate_ref=d.get("qb_estimate_ref"),
                         on_date=d.get("date"), actor_user_id=_uid())
        return jsonify({"data": estimates.est_public(est)})
    except LookupError:
        return jsonify({"error": "not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()


@requires_section('estimates')
def _api_nudge(est_id):
    """POST — the stalled-list nudge: a CRM activity row on the client org + a
    notification to the assigned estimator. No stage change — a nudge is a poke."""
    conn = _db()
    try:
        est = estimates.get_estimate(conn, est_id)
        if not est:
            return jsonify({"error": "not found"}), 404
        crm.add_activity(conn, entity_type="organization", entity_id=est["client_org_id"],
                         activity_type="system", author_user_id=_uid(), function_tag="sales",
                         summary=f"Estimate {est['code']}: nudged — stalled in estimating")
        nid = None
        if est.get("assigned_estimator"):
            nid = notifications.notify_user(
                conn, kind="nudge", user_id=est["assigned_estimator"],
                subject=f"[SSC] Nudge: {est['code']} is stalled — please advance it",
                estimate_code=est["code"],
                extra_line=f"{est.get('building_address') or ''}".strip())
        return jsonify({"data": {"nudged": True, "notification_id": nid}})
    finally:
        conn.close()


@requires_section('estimates')
def _api_assign(est_id):
    """PUT — assign/unassign the estimator (admin/c_suite). Assignment notifies."""
    d = request.get_json(silent=True) or {}
    conn = _db()
    try:
        est = estimates.get_estimate(conn, est_id)
        if not est:
            return jsonify({"error": "not found"}), 404
        uid = d.get("user_id")
        if uid is not None:
            u = conn.execute("SELECT id, role FROM users WHERE id=? AND is_active=1",
                             (uid,)).fetchone()
            if not u:
                return jsonify({"error": "user not found"}), 400
            if u["role"] not in ("estimator", "admin", "c_suite"):
                return jsonify({"error": "assignee must be an estimator (or admin/c_suite)"}), 400
        conn.execute("UPDATE estimate SET assigned_estimator=?, updated_at=? WHERE id=?",
                     (uid, _now(), est_id))
        conn.commit()
        if uid and uid != est.get("assigned_estimator"):
            notifications.notify_user(
                conn, kind="assignment", user_id=uid,
                subject=f"[SSC] {est['code']} assigned to you",
                estimate_code=est["code"],
                extra_line=f"{est.get('building_address') or ''}".strip())
        return jsonify({"data": {"assigned_estimator": uid}})
    finally:
        conn.close()


@requires_section('estimates')
def _api_estimators():
    """GET — assignable estimating seats for the console (estimator role users)."""
    conn = _db()
    try:
        rows = [{"id": r["id"], "name": r["nm"], "role": r["role"]} for r in conn.execute(
            "SELECT id, COALESCE(display_name, full_name, email) AS nm, role FROM users "
            "WHERE role IN ('estimator','c_suite','admin') AND status='active' AND is_active=1 "
            "AND COALESCE(is_system,0)=0 ORDER BY (role<>'estimator'), LOWER(nm)").fetchall()]
        return jsonify({"data": rows})
    finally:
        conn.close()


def register(app) -> None:
    """Wire the estimating surfaces. Queue = 'estimating' section (estimator +
    admin/c_suite); VP table/actions = 'estimates' (admin/c_suite). Call after the
    auth + containment gates."""
    app.add_url_rule("/estimating", "estimating_page", _serve_estimating_page, methods=["GET"])
    app.add_url_rule("/api/estimating/queue", "estimating_queue", _api_queue, methods=["GET"])
    app.add_url_rule("/api/estimating/<int:est_id>/start", "estimating_start", _api_start, methods=["POST"])
    app.add_url_rule("/api/estimating/<int:est_id>/stage", "estimating_stage", _api_stage, methods=["POST"])
    app.add_url_rule("/api/estimating/vp-table", "estimating_vp_table", _api_vp_table, methods=["GET"])
    app.add_url_rule("/api/estimating/<int:est_id>/vp-approve", "estimating_vp_approve", _api_vp_approve, methods=["POST"])
    app.add_url_rule("/api/estimating/<int:est_id>/nudge", "estimating_nudge", _api_nudge, methods=["POST"])
    app.add_url_rule("/api/estimating/<int:est_id>/assign", "estimating_assign", _api_assign, methods=["PUT"])
    app.add_url_rule("/api/estimating/estimators", "estimating_estimators", _api_estimators, methods=["GET"])
