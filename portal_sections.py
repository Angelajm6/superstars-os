"""#284 — the /api/portal/<code>/* section payloads: the portal shell's data, and the
first PRODUCTION consumers of the client_payload() registry.

FOUR sections tonight: progress, photos, documents, daily. Each handler:

  * is INDEPENDENTLY CURATED. A mirror suffix is never a passthrough of the internal
    endpoint — the handler reads the DB itself and every emitted field passes through
    client_registry.client_payload() against a REGISTERED dataset. An unregistered
    field does not leave the building (fail-closed at the serialiser).
  * hard-codes audience="client". Even an admin preview receives the CLIENT projection —
    the internal passthrough branch is unreachable from these routes by construction,
    and preview parity is a corollary rather than a promise.
  * re-derives EVERYTHING per request via require_portal_section(): the effective
    client (self, or the #270 admin preview target), their ONE bound project, the #284
    role x section MATRIX row, and the #269 grant. The <code> in the URL must equal the
    bound project — a client cannot address another project's payload by URL.
  * serves photo/document BYTES through the EXISTING id-gated Classic routes
    (/api/portal/photos/<id>/..., /api/portal/documents/<id>/file) — per-item
    visibility re-derived from the row on every fetch (#264). No new byte surface.

THE DAILY BREAKDOWN (operator-approved allowlist, decided for #284):
  date + work/no-work; weather; active drop/elevation labels; STRUCTURED activity
  categories (stage-template step names + started/completed enums); that-day
  cell status changes in client vocabulary (status_tone client labels); that-day
  client-shared photos. NEVER: worker identities, hours, rates, headcounts, SOV
  quantities, internal notes, or ANY free-text column — no_work_reason,
  no_work_note, scope_of_work, description, drop_stage_status.note, cell reason
  are not in any SELECT below (provenance, not vocabulary).

PII discipline (CLAUDE.md): ids/labels/dates/counts only. Dates are LOCAL. All SQL is
parameterized through the caller's db_layer connection — identical on SQLite and PG.
"""
from __future__ import annotations

import functools
import logging
from pathlib import Path

from flask import g, jsonify, request, send_file

import client_grants
import client_registry as reg
import client_portal
import portal_matrix
import visibility
import dropplan_rollups as _rollups
from auth import _db


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ============= THE GATE — matrix + grant + project binding, per request =============

def require_portal_section(section: str):
    """403 unless: the request resolves to an effective CLIENT (self, or a validated
    admin/c_suite preview target — client_portal._resolve_portal_client), AND the URL's
    project_code equals that client's one bound project, AND `section` is inside the
    role's MATRIX row (#284), AND the client holds the #269 grant. Layered on top of the
    client/architect containment gates, which have already run for the session role."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(project_code, *args, **kwargs):
            conn = _db()
            try:
                target, is_preview, err = client_portal._resolve_portal_client(conn)
                if err:
                    return err
                role = target.get("role")          # 'client' by construction today
                if not portal_matrix.allowed(role, section):
                    logging.info(f"portal_sections: matrix deny role={role} "
                                 f"section={section} path={request.path}")
                    return jsonify({"error": "forbidden"}), 403
                code = client_portal.client_project_code(target["id"], conn)
                if not code or code != project_code:
                    logging.info(f"portal_sections: project-binding deny "
                                 f"target={target.get('id')} path={request.path}")
                    return jsonify({"error": "forbidden"}), 403
                if not client_grants.has_grant(conn, target["id"], code, section):
                    logging.info(f"portal_sections: grant deny section={section} "
                                 f"target={target.get('id')} preview={is_preview}")
                    return jsonify({"error": "forbidden"}), 403
            finally:
                conn.close()
            g.portal_client_id = target["id"]
            g.portal_is_preview = is_preview
            g.client_project_code = code
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ============= SECTION: progress =============

# WMO weather code -> the shared icon vocabulary (the internal widget's w-* symbol
# names). A structured map over an enum — no free text involved.
_WMO_ICON = {}
for _codes, _name in (((0, 1), "sun"), ((2,), "pcloud"), ((3, 45, 48), "cloud"),
                      (tuple(range(51, 68)) + tuple(range(80, 83)), "rain"),
                      (tuple(range(71, 78)) + (85, 86), "snow"),
                      (tuple(range(95, 100)), "storm")):
    for _c in _codes:
        _WMO_ICON[_c] = _name


def _drop_status(lifecycle, pct):
    """The internal Project Health three-bucket derivation, mirrored: structured
    inputs (lifecycle enum + derived pct) only."""
    if lifecycle == "not_started":
        return "not_started"
    if pct >= 100 or lifecycle in ("closed",):
        return "complete"
    return "active"


def _progress_boards(conn, code):
    """The client-safe Project Health boards: active-drop cards, drops-by-status
    counts, progress-by-elevation bars. Sources are drops (enum/label columns),
    drop_stage_status (via rollups) and stage_template_steps.name (structured
    template vocabulary). No note/description column is selected anywhere."""
    drops = conn.execute(
        "SELECT drop_id, sequence_no, elevation, lifecycle FROM drops "
        "WHERE project_code=? ORDER BY sequence_no", (code,)).fetchall()
    active, counts, by_elev = [], {"active": 0, "complete": 0, "not_started": 0}, {}
    for d in drops:
        p = _rollups.drop_progress(conn, d["drop_id"])
        pct = p.get("pct", 0.0) or 0.0
        status = _drop_status(d["lifecycle"], pct)
        counts[status] += 1
        if d["elevation"]:
            by_elev.setdefault(d["elevation"], []).append(pct)
        if status == "active":
            stage = _rollups.current_stage(conn, d["drop_id"])
            step = (f"Step {stage['step_no']} · {stage['name']}" if stage
                    else "All steps complete")
            active.append(reg.client_payload("health.active_drop", {
                "label": f"DP-{d['sequence_no']}",
                "elevation": d["elevation"],
                "pct": round(pct, 0),
                "step": step,
            }))
    status_counts = reg.client_payload("health.status_count", [
        {"status": "active", "label": "Active", "tone": "blue",
         "count": counts["active"]},
        {"status": "complete", "label": "Complete", "tone": "green",
         "count": counts["complete"]},
        {"status": "not_started", "label": "Not started", "tone": "neutral",
         "count": counts["not_started"]},
    ])
    elevation_progress = reg.client_payload("health.elevation_progress", [
        {"elevation": e, "pct": round(sum(v) / len(v), 0)}
        for e, v in sorted(by_elev.items())])
    return active, status_counts, elevation_progress


def _progress_weather():
    """Today's conditions + the 5-day strip, from the same Open-Meteo helper the
    internal widget uses (public data), every row through health.weather. On a
    provider failure the card is simply absent — never an error page."""
    try:
        from server import fetch_open_meteo_weather
        w = fetch_open_meteo_weather(40.8083, -73.9162)
    except Exception:
        return None, []
    now = reg.client_payload("health.weather", {
        "date": w.get("date"),
        "temp_f": w.get("temp_now"),
        "condition": w.get("condition_label"),
        "icon": _WMO_ICON.get(w.get("condition_code"), "cloud"),
        "precip_pct": w.get("precip_prob_today"),
        "wind_mph": w.get("wind_mph"),
    })
    days = reg.client_payload("health.weather", [{
        "date": f.get("date"),
        "temp_f": f.get("temp_max"),
        "condition": None,
        "icon": _WMO_ICON.get(f.get("code"), "cloud"),
        "precip_pct": f.get("precip_prob"),
        "wind_mph": None,
    } for f in (w.get("forecast") or [])])
    return now, days


@require_portal_section("progress")
def _sec_progress():
    """The client-safe Project Health mirror (#285): overall ring + generated summary
    (as before), plus active-drop cards, drops-by-status counts, progress-by-elevation
    bars, and the weather card. Every field through the registry; include_cost=False
    means no $ field exists to begin with."""
    code = g.client_project_code
    conn = _db()
    try:
        roll = _rollups.project_rollup(conn, code, include_cost=False)
        pct = roll.get("overall_progress_pct", 0.0) or 0.0
        label = client_portal._progress_label(pct)
        text = f"Your project is {label.lower()} — about {pct:.0f}% complete."
        last_activity = None
        photos_shared = None
        if client_grants.has_grant(conn, g.portal_client_id, code, "photos"):
            vis_ids = visibility.client_visible_photo_ids(conn, code)
            photos_shared = len(vis_ids)
            if vis_ids:
                row = conn.execute(
                    f"SELECT MAX(substr(taken_at,1,10)) FROM field_photos "
                    f"WHERE id IN ({','.join(['?'] * len(vis_ids))})", vis_ids).fetchone()
                last_activity = row[0] if row else None
                text += f" {photos_shared} photo{'s' if photos_shared != 1 else ''} shared with you."
        progress = reg.client_payload("health.progress",
                                      {"pct": round(pct, 0), "label": label})
        latest = conn.execute(
            "SELECT MAX(report_date) FROM report_index "
            "WHERE project_code=? AND status='issued'", (code,)).fetchone()
        summary = {"text": text, "last_activity": last_activity,
                   "latest_report": latest[0] if latest else None}
        if photos_shared is not None:
            summary["photos_shared"] = photos_shared
        summary = reg.client_payload("portal.progress_summary", summary)
        active, status_counts, elevation_progress = _progress_boards(conn, code)
    finally:
        conn.close()
    weather_now, weather_days = _progress_weather()   # network call AFTER the db closes
    return _no_store(jsonify({"data": {
        "progress": progress, "summary": summary,
        "active_drops": active, "status_counts": status_counts,
        "elevation_progress": elevation_progress,
        "weather": weather_now, "weather_days": weather_days,
    }}))


# ============= SECTION: photos =============

@require_portal_section("photos")
def _sec_photos():
    """Item-shared photos ONLY (visibility.py is the source of truth for WHICH; the
    registry curates the SHAPE). #285 — the internal Field Photos mirror: each photo
    carries its drop label + elevation (structured joins, generated labels), the
    caption column is NOT SELECTED (operator decision: drop · elevation · date only),
    and a stat strip rides alongside. Byte URLs stay on the Classic id-gated routes."""
    code = g.client_project_code
    conn = _db()
    try:
        ids = visibility.client_visible_photo_ids(conn, code)
        if not ids:
            stats = reg.client_payload("portal.photos_stats", {
                "shared_count": 0, "drops_covered": 0, "latest_date": None})
            return _no_store(jsonify({"data": {"photos": [], "stats": stats}}))
        rows = conn.execute(
            f"SELECT fp.id, fp.taken_at, d.sequence_no, d.elevation "
            f"FROM field_photos fp LEFT JOIN drops d ON d.drop_id = fp.drop_id "
            f"WHERE fp.id IN ({','.join(['?'] * len(ids))}) "
            f"ORDER BY fp.taken_at DESC, fp.id DESC", ids).fetchall()
        photos = reg.client_payload("portal.photo", [{
            "id": r["id"],
            "drop_label": (f"DP-{r['sequence_no']}" if r["sequence_no"] is not None
                           else "Unassigned"),
            "elevation": r["elevation"],
            "taken_at": (r["taken_at"] or "")[:10] or None,   # date only, LOCAL
            "thumb_url": f"/api/portal/photos/{r['id']}/thumb",
            "file_url": f"/api/portal/photos/{r['id']}/file",
        } for r in rows])
        drops_covered = len({p["drop_label"] for p in photos
                             if p.get("drop_label") and p["drop_label"] != "Unassigned"})
        latest = max((p["taken_at"] for p in photos if p.get("taken_at")), default=None)
        stats = reg.client_payload("portal.photos_stats", {
            "shared_count": len(photos), "drops_covered": drops_covered,
            "latest_date": latest})
        return _no_store(jsonify({"data": {"photos": photos, "stats": stats}}))
    finally:
        conn.close()


# ============= SECTION: documents =============

@require_portal_section("documents")
def _sec_documents():
    """Item-shared documents ONLY — same engine, same posture as photos."""
    code = g.client_project_code
    conn = _db()
    try:
        ids = visibility.client_visible_document_ids(conn, code)
        if not ids:
            return _no_store(jsonify({"data": {"documents": []}}))
        rows = conn.execute(
            f"SELECT id, title, category, doc_type, effective_date FROM project_documents "
            f"WHERE id IN ({','.join(['?'] * len(ids))}) ORDER BY uploaded_at DESC, id DESC",
            ids).fetchall()
        docs = reg.client_payload("portal.document", [{
            "id": r["id"], "title": r["title"], "category": r["category"],
            "doc_type": r["doc_type"], "effective_date": r["effective_date"],
            "file_url": f"/api/portal/documents/{r['id']}/file",
        } for r in rows])
        return _no_store(jsonify({"data": {"documents": docs}}))
    finally:
        conn.close()


# ============= SECTION: daily — the client DCR breakdown =============

_DAY_LIMIT = 30


def _daily_days(conn, code):
    """Issued report days, newest first. no_work_reason / no_work_note are NOT selected.
    #286 — dcr_sequence rides along: the display id is GENERATED from it, and it
    addresses the client-audience render route."""
    return conn.execute(
        "SELECT report_date AS date, MAX(COALESCE(no_work,0)) AS no_work, "
        "MAX(dcr_sequence) AS seq "
        "FROM report_index WHERE project_code=? AND status='issued' "
        "GROUP BY report_date ORDER BY report_date DESC LIMIT ?",
        (code, _DAY_LIMIT)).fetchall()


def _daily_weather(conn, code, dates):
    """{date: registered-weather} — temps + condition/wind strings the operator approved."""
    if not dates:
        return {}
    rows = conn.execute(
        f"SELECT date, am_temp_f, pm_temp_f, am_conditions, pm_conditions, wind "
        f"FROM weather_log WHERE project_code=? AND date IN ({','.join(['?'] * len(dates))})",
        [code, *dates]).fetchall()
    return {r["date"]: reg.client_payload("portal.daily_weather", {
        "am_temp_f": r["am_temp_f"], "pm_temp_f": r["pm_temp_f"],
        "am_conditions": r["am_conditions"], "pm_conditions": r["pm_conditions"],
        "wind": r["wind"],
    }) for r in rows}


def _daily_stage_activity(conn, code, dates):
    """{date: (drops, activities)} from drop_stage_status started_on/completed_on.
    STRUCTURED vocabulary only: the drop label (sequence + elevation) and the stage-
    template step NAME + a started/completed enum. ds.note is never selected."""
    if not dates:
        return {}
    ph = ",".join(["?"] * len(dates))
    rows = conn.execute(
        f"SELECT d.sequence_no, d.elevation, st.name AS category, "
        f"       ds.started_on, ds.completed_on "
        f"FROM drop_stage_status ds "
        f"JOIN drops d ON d.drop_id = ds.drop_id "
        f"JOIN stage_templates t ON t.project_code = d.project_code "
        f"JOIN stage_template_steps st ON st.template_id = t.template_id "
        f"                            AND st.step_no = ds.step_no "
        f"WHERE d.project_code=? AND (ds.started_on IN ({ph}) OR ds.completed_on IN ({ph}))",
        [code, *dates, *dates]).fetchall()
    out = {}
    for r in rows:
        # #285 — schedule-drop vocabulary is DP-{n} everywhere on the portal
        # (photos groups, progress cards, these chips). Drawing-cell status
        # changes keep their own surface's "Drop {idx}" naming.
        label = f"DP-{r['sequence_no']}"
        elev = r["elevation"]
        for col, verb in (("started_on", "started"), ("completed_on", "completed")):
            day = r[col]
            if day in set(dates):
                drops, acts = out.setdefault(day, ({}, []))
                drops.setdefault(label, {"label": label, "elevation": elev})
                acts.append({"category": r["category"], "status": verb})
    return out


def _daily_cell_changes(conn, code, dates):
    """{date: [registered change]} from the append-only elevation_cell_event log, in
    CLIENT vocabulary (status_tone client_label). reason / actor_uid never selected.

    #286 CHURN FILTER — the feed is NET day-level movement, not the raw event log:
    events collapse per (day, cell) to first-from -> last-to, so a no-op write
    (X -> X) and a same-day round trip (A -> B -> A) render NOTHING. Operator
    churn is not client news; the day either moved or it did not."""
    if not dates:
        return {}
    tones = {r["key"][len("elevation."):]: (r["client_label"] or r["label"])
             for r in conn.execute(
                 "SELECT key, label, client_label FROM status_tone "
                 "WHERE module='elevation' AND client_visible=1").fetchall()}
    ph = ",".join(["?"] * len(dates))
    rows = conn.execute(
        f"SELECT substr(ev.created_at,1,10) AS day, ev.cell_id, ed.idx AS drop_idx, "
        f"       c.level_name, ev.from_status, ev.to_status "
        f"FROM elevation_cell_event ev "
        f"JOIN elevation_cell c ON c.id = ev.cell_id "
        f"JOIN elevation_drop ed ON ed.id = c.drop_id "
        f"JOIN elevation e ON e.id = ed.elevation_id "
        f"WHERE e.project_code=? AND substr(ev.created_at,1,10) IN ({ph}) "
        f"ORDER BY ev.created_at, ev.id", [code, *dates]).fetchall()
    net = {}      # (day, cell_id) -> {first_from, last_to, drop_idx, level_name}
    order = []
    for r in rows:
        k = (r["day"], r["cell_id"])
        if k not in net:
            net[k] = {"first_from": r["from_status"], "drop_idx": r["drop_idx"],
                      "level_name": r["level_name"]}
            order.append(k)
        net[k]["last_to"] = r["to_status"]
    out = {}
    for k in order:
        day, _cell = k
        n = net[k]
        if n["first_from"] == n["last_to"]:
            continue          # no NET movement that day — churn, not news
        frm, to = tones.get(n["first_from"]), tones.get(n["last_to"])
        if to is None:
            continue          # a non-client-visible status never ships, even renamed
        out.setdefault(day, []).append(reg.client_payload("portal.daily_change", {
            "drop_label": f"Drop {n['drop_idx']}",
            "level": n["level_name"],
            "from_label": frm, "to_label": to,
        }))
    return out


def _daily_photos(conn, code, dates):
    """{date: [registered photo ref]} — that-day CLIENT-SHARED photos only. ids + byte
    URLs; caption deliberately absent here (free-text stays out of the daily payload)."""
    ids = visibility.client_visible_photo_ids(conn, code)
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT id, substr(taken_at,1,10) AS day FROM field_photos "
        f"WHERE id IN ({','.join(['?'] * len(ids))}) ORDER BY taken_at, id", ids).fetchall()
    want = set(dates)
    out = {}
    for r in rows:
        if r["day"] in want:
            out.setdefault(r["day"], []).append(reg.client_payload("portal.daily_photo", {
                "id": r["id"],
                "thumb_url": f"/api/portal/photos/{r['id']}/thumb",
                "file_url": f"/api/portal/photos/{r['id']}/file",
            }))
    return out


@require_portal_section("daily")
def _sec_daily():
    """The client daily timeline: one entry per ISSUED report day, each carrying ONLY
    the operator-approved breakdown. Every field goes through the registry; the day
    label is generated vocabulary, never a stored string."""
    code = g.client_project_code
    conn = _db()
    try:
        day_rows = _daily_days(conn, code)
        dates = [r["date"] for r in day_rows]
        weather = _daily_weather(conn, code, dates)
        stage = _daily_stage_activity(conn, code, dates)
        changes = _daily_cell_changes(conn, code, dates)
        photos = _daily_photos(conn, code, dates)
        days = []
        for r in day_rows:
            d = r["date"]
            no_work = bool(r["no_work"])
            seq = r["seq"]
            day = reg.client_payload("portal.daily_day", {
                "date": d, "no_work": no_work,
                "label": "No work performed" if no_work
                         else "Crew on site — work performed",
                # #286 — GENERATED display id (never the per-audience report_id column)
                "report_id": (f"DCR-{code}-{seq:03d}" if seq is not None else None),
                "seq": seq,
            })
            drops_map, acts = stage.get(d, ({}, []))
            day["weather"] = weather.get(d)
            day["active_drops"] = reg.client_payload(
                "portal.daily_drop", list(drops_map.values()))
            day["activities"] = reg.client_payload("portal.daily_activity", acts)
            day["status_changes"] = changes.get(d, [])
            day["photos"] = photos.get(d, [])
            days.append(day)
        return _no_store(jsonify({"data": {"days": days}}))
    finally:
        conn.close()


# ============= #286: the client-audience rendered DCR, by sequence =============

_SCRIPT_DIR = Path(__file__).resolve().parent
import ssc_paths  # #287
_DCR_RENDER_BASE = None   # #287 — replaced by _dcr_render_base(), per call


def _dcr_render_base():
    return ssc_paths.under_root("data_room", "reports", "dcr")


@require_portal_section("daily")
def _sec_daily_view(seq):
    """Serve the CLIENT-AUDIENCE rendered DCR for one issued sequence of the client's
    own project. Audience is PER-RENDER, not per-item: this route serves client.html
    and nothing else — the internal render and the PDF do not exist on this namespace,
    at any URL, for any parameter. Ownership + issuance re-derived per request; any
    other-project / unissued / unknown sequence is 404 (never reveals existence)."""
    code = g.client_project_code
    conn = _db()
    try:
        row = conn.execute(
            "SELECT 1 FROM report_index WHERE project_code=? AND report_type='DCR' "
            "AND dcr_sequence=? AND status='issued' LIMIT 1", (code, seq)).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    f = _dcr_render_base() / code / f"{seq:03d}" / "client.html"
    try:
        if not f.resolve().is_relative_to(_dcr_render_base().resolve()) or not f.exists():
            return jsonify({"error": "not found"}), 404
    except (OSError, ValueError):
        return jsonify({"error": "not found"}), 404
    resp = send_file(str(f), mimetype="text/html")
    return _no_store(resp)


# ============= #286: the look-ahead board (anatomy parity, read-only) =============

@require_portal_section("schedule")
def _sec_lookahead():
    """The client's Two-Week Look-Ahead: the SAME board the internal page renders,
    through the registry. STRICTLY READ-ONLY — this route never drafts, refreshes,
    or writes; an empty plan serves an empty window honestly.

    Curated per the #286 provenance decision: activity TITLES cross (planning labels
    designed for external consumption — flagged in HANDOFF); a delivery's name is
    replaced with the generic "Delivery"; crew / notes / source / constraint counts
    never enter the payload."""
    from datetime import date as _date
    code = g.client_project_code
    start = (request.args.get("start") or "").strip() or _date.today().isoformat()
    try:
        _date.fromisoformat(start)
    except ValueError:
        return jsonify({"error": "start must be YYYY-MM-DD"}), 400
    import lookahead as _lookahead
    conn = _db()
    try:
        raw = _lookahead.load_window(conn, code, start)
    finally:
        conn.close()

    def activity(a):
        name = "Delivery" if a.get("activity_type") == "delivery" else a.get("name")
        return reg.client_payload("la.activity", {
            "name": name, "activity_type": a.get("activity_type"), "grid": a.get("grid"),
        })

    groups = [{
        **reg.client_payload("la.group", gr),
        "activities": [activity(a) for a in gr.get("activities", [])],
    } for gr in raw.get("groups", [])]
    return _no_store(jsonify({"data": {
        "window_start": raw.get("window_start"), "window_end": raw.get("window_end"),
        "days": reg.client_payload("la.day", raw.get("days", [])),
        "kpis": reg.client_payload("la.kpis", raw.get("kpis", {})),
        "groups": groups,
        "general": [activity(a) for a in raw.get("general", [])],
    }}))


# ============= SECTION: proposals (#308) =============
# The #307 proposal rows, projected for the CLIENT. Scope = the organization that owns
# the client's ONE bound project (projects.client_org_id): every proposal that was SENT
# to that organization (never a draft, never a superseded version), each one labelled
# with its building because an organization can own several. The client can open the
# document, download the PDF, and — the C-suite's ask — accept with a typed signature or
# decline, from inside the portal. Accept/decline reuse proposals.accept/decline, so the
# trigger (#307: estimate approved, COI + deposit tasks, VP notification) is the same code
# path as the emailed link. An ADMIN PREVIEW is read-only: the write endpoints refuse it.

def _client_org_id(conn, code):
    r = conn.execute("SELECT client_org_id FROM projects WHERE project_code = ?", (code,)).fetchone()
    return (r["client_org_id"] if r else None) or None


def _client_status(r) -> tuple:
    """(client key, label). Internal lifecycle words never leave the building."""
    import proposals
    st = proposals._link_state(r)
    return {
        "open": ("open", "Awaiting your signature"),
        "signed": ("signed", "Accepted"),
        "declined": ("declined", "Declined"),
        "expired": ("expired", "Expired"),
    }.get(st, ("expired", "No longer available"))


def _proposal_row(r, code) -> dict:
    key, label = _client_status(r)
    base = f"/api/portal/{code}/proposals/{r['id']}"
    return reg.client_payload("portal.proposal", {
        "id": r["id"], "number": r["number"], "version": r["version"], "title": r.get("title"),
        "status": key, "status_label": label,
        "price": r.get("price"), "deposit_amount": r.get("deposit_amount"), "deposit_pct": r.get("deposit_pct"),
        "valid_until": r.get("valid_until"), "sent_at": r.get("sent_at"),
        "signed_at": r.get("signed_at"), "signed_name": r.get("signed_name"), "declined_at": r.get("declined_at"),
        "building": r.get("building_address_line") or r.get("building_address"),
        "view_url": base + "/view", "pdf_url": base + "/pdf", "can_accept": key == "open",
    })


def _own_proposal(conn, code, proposal_id):
    """The proposal ONLY if it belongs to the bound project's organization and was sent
    (draft / superseded rows do not exist for the client). None otherwise -> 404."""
    import proposals
    org = _client_org_id(conn, code)
    if not org:
        return None
    r = proposals.get(conn, proposal_id)
    if not r or r["client_org_id"] != org or r["status"] in ("draft", "superseded"):
        return None
    return r


@require_portal_section("proposals")
def _sec_proposals():
    import proposals
    project_code = g.client_project_code
    conn = _db()
    try:
        org = _client_org_id(conn, project_code)
        rows = []
        if org:
            for r in conn.execute(proposals._JOIN + "WHERE p.client_org_id = ? AND p.status IN ('sent','viewed','signed','declined') "
                                  "ORDER BY p.sent_at DESC, p.id DESC", (org,)).fetchall():
                rows.append(_proposal_row(dict(r), project_code))
        return _no_store(jsonify({"data": {"proposals": rows,
                                          "open_count": sum(1 for x in rows if x["status"] == "open")}}))
    finally:
        conn.close()


@require_portal_section("proposals")
def _sec_proposal_view(proposal_id):
    """The document itself (no acceptance UI — the portal card carries it). A real
    client's first open flips sent -> viewed exactly like the emailed link."""
    import proposals
    project_code = g.client_project_code
    conn = _db()
    try:
        r = _own_proposal(conn, project_code, proposal_id)
        if not r:
            return jsonify({"error": "not found"}), 404
        if not g.portal_is_preview and proposals._link_state(r) == "open":
            r = proposals.record_view(conn, r, ip=proposals._client_ip())
        from flask import make_response
        return _no_store(make_response(proposals.render_document(conn, r, mode="page"), 200))
    finally:
        conn.close()


@require_portal_section("proposals")
def _sec_proposal_pdf(proposal_id):
    import proposals
    project_code = g.client_project_code
    conn = _db()
    try:
        r = _own_proposal(conn, project_code, proposal_id)
        if not r:
            return jsonify({"error": "not found"}), 404
        p = proposals.pdf_file(conn, r["id"])
        if not p:
            return jsonify({"error": "the PDF is not available right now — open the proposal instead"}), 503
        from flask import make_response
        return _no_store(make_response(send_file(str(p), mimetype="application/pdf", as_attachment=True,
                                                 download_name=f"{r['number']}.pdf")))
    finally:
        conn.close()


@require_portal_section("proposals")
def _sec_proposal_accept(proposal_id):
    import proposals
    if g.portal_is_preview:
        return jsonify({"error": "read-only preview"}), 403
    body = request.get_json(silent=True) or {}
    if not body.get("agreed"):
        return jsonify({"error": "please confirm you accept the terms"}), 400
    project_code = g.client_project_code
    conn = _db()
    try:
        r = _own_proposal(conn, project_code, proposal_id)
        if not r:
            return jsonify({"error": "not found"}), 404
        try:
            r = proposals.accept(conn, r, name=body.get("name"), title=body.get("title"), ip=proposals._client_ip())
        except ValueError as e:
            return jsonify({"error": str(e)}), (409 if "no longer" in str(e) else 400)
        logging.info(f"portal_sections: proposal accepted id={r['id']} client={g.portal_client_id}")
        return _no_store(jsonify({"data": _proposal_row(r, project_code)}))
    finally:
        conn.close()


@require_portal_section("proposals")
def _sec_proposal_decline(proposal_id):
    import proposals
    if g.portal_is_preview:
        return jsonify({"error": "read-only preview"}), 403
    body = request.get_json(silent=True) or {}
    project_code = g.client_project_code
    conn = _db()
    try:
        r = _own_proposal(conn, project_code, proposal_id)
        if not r:
            return jsonify({"error": "not found"}), 404
        try:
            r = proposals.decline(conn, r, reason=body.get("reason"), ip=proposals._client_ip())
        except ValueError as e:
            return jsonify({"error": str(e)}), 409
        logging.info(f"portal_sections: proposal declined id={r['id']} client={g.portal_client_id}")
        return _no_store(jsonify({"data": _proposal_row(r, project_code)}))
    finally:
        conn.close()


def register(app) -> None:
    """Wire the four section payloads. Call AFTER client_portal.register (the client
    gate must already be routing /api/portal/* for the session role)."""
    app.add_url_rule("/api/portal/<project_code>/progress", "portal_sec_progress",
                     _sec_progress, methods=["GET"])
    app.add_url_rule("/api/portal/<project_code>/photos", "portal_sec_photos",
                     _sec_photos, methods=["GET"])
    app.add_url_rule("/api/portal/<project_code>/documents", "portal_sec_documents",
                     _sec_documents, methods=["GET"])
    app.add_url_rule("/api/portal/<project_code>/daily", "portal_sec_daily",
                     _sec_daily, methods=["GET"])
    app.add_url_rule("/api/portal/<project_code>/daily/<int:seq>/view",
                     "portal_sec_daily_view", _sec_daily_view, methods=["GET"])
    app.add_url_rule("/api/portal/<project_code>/lookahead", "portal_sec_lookahead",
                     _sec_lookahead, methods=["GET"])
    # #308 — proposals (read + the client's accept / decline)
    app.add_url_rule("/api/portal/<project_code>/proposals", "portal_sec_proposals",
                     _sec_proposals, methods=["GET"])
    app.add_url_rule("/api/portal/<project_code>/proposals/<int:proposal_id>/view",
                     "portal_sec_proposal_view", _sec_proposal_view, methods=["GET"])
    app.add_url_rule("/api/portal/<project_code>/proposals/<int:proposal_id>/pdf",
                     "portal_sec_proposal_pdf", _sec_proposal_pdf, methods=["GET"])
    app.add_url_rule("/api/portal/<project_code>/proposals/<int:proposal_id>/accept",
                     "portal_sec_proposal_accept", _sec_proposal_accept, methods=["POST"])
    app.add_url_rule("/api/portal/<project_code>/proposals/<int:proposal_id>/decline",
                     "portal_sec_proposal_decline", _sec_proposal_decline, methods=["POST"])
