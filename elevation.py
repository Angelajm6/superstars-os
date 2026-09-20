"""#280 — North Elevation drop plan: endpoints, audiences, and the external payload.

SECURITY MODEL — this ships to outside parties, so read this before editing anything.

TWO AUDIENCES, NOT THREE. Per the operator's correction to the earlier mockup:
  * INTERNAL  admin / c_suite / pm / super / estimator
      Everything, plus paint mode (the only roles that may change a status).
  * EXTERNAL  architect / client — ONE view, IDENTICAL for both.
      Same drawing, same DROP n labels, same grids, same dimension strings, same
      statuses INCLUDING rework (a field condition, not an internal metric), and the
      same `reason` text. They differ only in WRITE rights, never in what they see:
      architect may comment / raise RFIs / request meetings; client is read-only.

WHAT NEVER LEAVES THE BUILDING (asserted in tests/smoke_elevation_280.py):
  * elevation_cell.internal_note — never selected into an external payload, at all.
  * SLA / performance metrics, estimate stages, cost, rates, margin — none of these
    exist in this module's queries, by construction.

THE TWO-FIELD REASON SPLIT IS THIS BUILD'S SECURITY MECHANISM. The phase-2 client
registry + client_payload() serialiser do not exist yet, so "external" and "internal"
are separate COLUMNS rather than a filtering rule applied to one column:
    reason         EXTERNAL by construction — shown to architect and client
    internal_note  INTERNAL by construction — excluded by not being selected
The operating rule for whoever is typing: the reason is what you would say to the
architect's face. Anything else goes in the internal note.

DEBT, RECORDED DELIBERATELY (#280 -> phase 2): every external endpoint here builds its
payload from a HAND-CURATED NARROW SELECT naming each column. No SELECT *, no model
dump. When phase 2 lands the client-field registry and client_payload(), these
endpoints get routed through it and the hand-curation is deleted. Until then the narrow
SELECT is the guarantee, and the smoke asserts it holds.

PROJECT SCOPING: an architect is scoped to ASSIGNED PROJECTS ONLY, reusing the existing
#263 pm_project_assignment grant — never a parallel mechanism, and never "all projects".
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from flask import Response, g, jsonify, request, send_file

import access
import db_layer
import pm_scoping
from auth import current_user

SCRIPT_DIR = Path(__file__).resolve().parent
# THE DRAWING MARKUP PAGE. Named and routed separately from the existing /dropplan
# (#201/#256), which is the per-drop LIFECYCLE schedule and stays exactly as it is.
# This page is the interactive elevation with per-drop, per-floor work status.
MARKUP_PAGE = SCRIPT_DIR / "templates" / "v2" / "drawing-markup.html"

# ---- audiences -------------------------------------------------------------
INTERNAL_ROLES = frozenset({"admin", "c_suite", "pm", "super", "estimator"})
EXTERNAL_ROLES = frozenset({"architect", "client"})
# Roles that may CHANGE a drop status. Deliberately narrower than "internal can see":
# painting the elevation is an SSC act.
PAINT_ROLES = INTERNAL_ROLES
# Roles that may write collaboration objects (comments, RFIs, meeting requests).
# The client is READ-ONLY by design and is absent here.
COLLAB_WRITE_ROLES = INTERNAL_ROLES | {"architect"}

# The elevation picker's canonical entries, in header order.
#
# FULL-SET SCHEMA CHOICE: 'ALL' is carried as a fifth `elevation.face` VALUE, not as a
# new `kind` column. elevation.face has NO CHECK constraint (the allowed values live in a
# comment), so a fifth value costs zero migration on either backend, where a new column
# would cost one on both. If the full set later needs its own attributes it can graduate
# to a column; nothing here forecloses that.
#
# The full-set VIEW IS NOT DESIGNED — rendering four elevations together is a different
# problem (shared scale? stacked? wrapped? one continuous developed elevation?) and has
# not been specified. The entry exists in the picker and reports itself untraced; picking
# it does not attempt a render.
FACES = (("N", "North"), ("S", "South"), ("E", "East"), ("W", "West"), ("ALL", "Full set"))

STATUS_KEYS = ("not_started", "in_progress", "on_hold", "rework", "complete")
REASON_REQUIRED = frozenset({"on_hold", "rework"})
MAX_REASON = 500
MAX_COMMENT = 2000


def _db():
    return db_layer.connect()


def _now():
    return datetime.now().isoformat(timespec="seconds")   # LOCAL, never UTC


def _err(msg, code):
    return jsonify({"error": msg}), code


# ============================================================================
# ACCESS — one place. Every endpoint below calls _require_project().
# ============================================================================

def _role():
    return (current_user() or {}).get("role")


def _uid():
    return (current_user() or {}).get("id")


def is_internal(role=None) -> bool:
    return (role or _role()) in INTERNAL_ROLES


def audience(role=None) -> str:
    """'internal' or 'external'. Anything unrecognised is EXTERNAL — default-deny:
    a role added later is treated as an outsider until someone says otherwise."""
    return "internal" if is_internal(role) else "external"


def accessible_codes(conn, user) -> set:
    """The project codes this user may open. Default-deny: an unknown role gets none.

    admin / c_suite      every project (the company axis, #263)
    pm/super/estimator   assigned projects (#263 pm_project_assignment)
    architect            ASSIGNED PROJECTS ONLY — never all. An architect on
                         890 E 135th must not see any other job.
    client               the single project their portal is bound to (#264/#269)
    """
    role = (user or {}).get("role")
    uid = (user or {}).get("id")
    if role in access.COMPANY_ROLES:
        return {r[0] for r in conn.execute("SELECT project_code FROM projects").fetchall()}
    if role in ("pm", "super", "estimator", "architect"):
        return pm_scoping.assigned_codes(uid, conn)
    if role == "client":
        import client_portal
        code = client_portal.client_project_code(uid, conn)
        return {code} if code else set()
    return set()


def _require_project(conn, code):
    """None when allowed, else a 403 response. Server-side, every time, no exceptions."""
    user = current_user()
    if not user or not code:
        return _err("forbidden", 403)
    if code not in accessible_codes(conn, user):
        logging.info(f"elevation: scope block role={user.get('role')} "
                     f"uid={user.get('id')} code={code}")
        return _err("forbidden", 403)
    return None


# ============================================================================
# READ — GET /api/elevation/<id>
# ============================================================================

def _drop_rows(conn, elev_id):
    """NARROW SELECT — named columns only. internal_note is absent from the external
    branch by CONSTRUCTION: it is not in the column list."""
    return conn.execute(
        "SELECT id, idx, grid_from, grid_to, x0, x1, width_ft, area_sf, note "
        "FROM elevation_drop WHERE elevation_id=? ORDER BY idx", (elev_id,)).fetchall()


def _cells_external(conn, elev_id):
    """External cell payload. internal_note IS NOT SELECTED — not fetched, not dropped
    later. `reason` IS included: it is external by construction."""
    return conn.execute(
        "SELECT c.id, c.drop_id, c.level_id, c.level_name, c.status_key, c.reason, "
        "       c.updated_at "
        "FROM elevation_cell c JOIN elevation_drop d ON d.id = c.drop_id "
        "WHERE d.elevation_id = ? ORDER BY d.idx, c.level_id", (elev_id,)).fetchall()


def _cells_internal(conn, elev_id):
    return conn.execute(
        "SELECT c.id, c.drop_id, c.level_id, c.level_name, c.status_key, c.reason, "
        "       c.internal_note, c.updated_by_uid, c.updated_at "
        "FROM elevation_cell c JOIN elevation_drop d ON d.id = c.drop_id "
        "WHERE d.elevation_id = ? ORDER BY d.idx, c.level_id", (elev_id,)).fetchall()


def status_vocabulary(conn, aud="internal"):
    """The five tones, from status_tone — never hard-coded in a template or a payload
    (non-negotiable #7: status colour comes from the status table only).

    Keyed by the token that audience's payload actually carries: the internal key for
    internal viewers, `client_key` for external ones. The renderer then looks up whatever
    token it was given without knowing which audience it is serving."""
    rows = conn.execute(
        "SELECT key, label, tone, severity_rank, client_key, client_label, client_visible, "
        "       sort_order FROM status_tone WHERE module='elevation' ORDER BY sort_order"
    ).fetchall()
    out = {}
    for r in rows:
        short = r["key"].split(".", 1)[1]
        if aud == "internal":
            out[short] = {"key": short, "label": r["label"], "tone": r["tone"],
                          "severity": r["severity_rank"],
                          "reason_required": short in REASON_REQUIRED}
        elif r["client_visible"]:
            ck = r["client_key"] or short
            out[ck] = {"key": ck, "label": r["client_label"] or r["label"],
                       "tone": r["tone"], "severity": r["severity_rank"],
                       "reason_required": False}
    return out


def _status_meta(conn):
    """short key -> {client_key, severity}. One query, used by the derivation below."""
    rows = conn.execute(
        "SELECT key, client_key, severity_rank FROM status_tone WHERE module='elevation'"
    ).fetchall()
    return {r["key"].split(".", 1)[1]: {"client_key": r["client_key"], "sev": r["severity_rank"]}
            for r in rows}


def derive_drop_status(cell_statuses, meta) -> str:
    """A drop's work status, DERIVED from its five cells. NEVER STORED, NEVER ENTERED.

    The two models sit at different grains and must not be allowed to drift:
      `drops` (#201/#256)  per-drop LIFECYCLE — scaffold_active means "is the rig up",
                           not "is the work done". Untouched by this build.
      elevation_cell       per-drop-per-FLOOR work status. The old model cannot express
                           this grain.
    So any drop-level WORK status shown anywhere is computed here from the cells. There
    is no column to enter it into, which prevents divergence by construction rather than
    by remembering.

    Alert statuses win by severity_rank (from status_tone — the ordering is data, not a
    switch here). Progress statuses fall through to explicit rules, because "half the
    floors are complete" is a progress question that severity cannot answer:
      any rework   -> rework      (highest severity; a failure anywhere fails the drop)
      any on_hold  -> on_hold
      all complete -> complete
      any movement -> in_progress (some complete, or some in progress)
      otherwise    -> not_started
    """
    if not cell_statuses:
        return "not_started"
    present = set(cell_statuses)
    alerts = [s for s in present if meta.get(s, {}).get("sev", 0) >= 60]
    if alerts:
        return max(alerts, key=lambda s: meta.get(s, {}).get("sev", 0))
    if present == {"complete"}:
        return "complete"
    if present & {"in_progress", "complete"}:
        return "in_progress"
    return "not_started"


def _api_get_elevation(elev_id):
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, project_code, face, face_label, name, status, source_sheet, "
            "       sheet_date, dob_job, scale_note, geometry_json "
            "FROM elevation WHERE id=?", (elev_id,)).fetchone()
        if row is None:
            return _err("not found", 404)
        blocked = _require_project(conn, row["project_code"])
        if blocked:
            return blocked

        aud = audience()
        # #293 — DRAFTS ARE INTERNAL-ONLY. An unconfirmed grid is a proposal in
        # authoring, not a record; to an external audience it does not exist
        # (404, never 403 — a 403 would confirm the id is real).
        status = row["status"] or "confirmed"
        if status != "confirmed" and aud != "internal":
            return _err("not found", 404)
        meta = _status_meta(conn)
        drops = [{"id": d["id"], "idx": d["idx"], "grid_from": d["grid_from"],
                  "grid_to": d["grid_to"], "x0": d["x0"], "x1": d["x1"],
                  "width_ft": d["width_ft"], "area_sf": d["area_sf"], "note": d["note"]}
                 for d in _drop_rows(conn, elev_id)]

        # `status` carries the token for THIS audience: the internal key internally,
        # client_key externally. The internal key never ships to an external payload,
        # even where the two spell the same word today.
        def _tok(sk):
            return sk if aud == "internal" else (meta.get(sk, {}).get("client_key") or sk)

        if aud == "internal":
            cells = [{"id": c["id"], "drop_id": c["drop_id"], "level_id": c["level_id"],
                      "level_name": c["level_name"], "status": c["status_key"],
                      "reason": c["reason"], "internal_note": c["internal_note"],
                      "updated_at": c["updated_at"]}
                     for c in _cells_internal(conn, elev_id)]
        else:
            cells = [{"id": c["id"], "drop_id": c["drop_id"], "level_id": c["level_id"],
                      "level_name": c["level_name"], "status": _tok(c["status_key"]),
                      "reason": c["reason"], "updated_at": c["updated_at"]}
                     for c in _cells_external(conn, elev_id)]

        # Drop roll-up: DERIVED from the cells, never stored. Computed off the raw
        # internal keys, then translated for the audience.
        raw = {}
        for c in _cells_external(conn, elev_id):
            raw.setdefault(c["drop_id"], []).append(c["status_key"])
        for d in drops:
            d["derived_status"] = _tok(derive_drop_status(raw.get(d["id"], []), meta))

        try:
            geom = json.loads(row["geometry_json"] or "{}")
        except ValueError:
            geom = {}

        # AS-OF DATE. The page is a progress view now, and a progress view with no
        # "updated" stamp cannot be trusted by whoever is reading it — an architect has
        # no way to tell yesterday's picture from last month's. Derived from the most
        # recent cell edit; None when nothing has ever been marked, which the page
        # renders as "not started yet" rather than inventing a date.
        upd = conn.execute(
            "SELECT MAX(c.updated_at) AS m FROM elevation_cell c "
            "JOIN elevation_drop d ON d.id = c.drop_id WHERE d.elevation_id = ?",
            (elev_id,)).fetchone()
        last_marked = conn.execute(
            "SELECT MAX(ev.created_at) AS m FROM elevation_cell_event ev "
            "JOIN elevation_cell c ON c.id = ev.cell_id "
            "JOIN elevation_drop d ON d.id = c.drop_id WHERE d.elevation_id = ?",
            (elev_id,)).fetchone()
        pname = conn.execute("SELECT name FROM projects WHERE project_code=?",
                             (row["project_code"],)).fetchone()

        return jsonify({"data": {
            "id": row["id"],
            "project_code": row["project_code"],
            "project_name": (pname["name"] if pname else None),
            "updated_at": (upd["m"] if upd else None),
            "last_marked_at": (last_marked["m"] if last_marked else None),
            "face": row["face"],
            "face_label": row["face_label"] or row["face"],
            "status": status,
            "name": row["name"],
            "source_sheet": row["source_sheet"],
            "sheet_date": row["sheet_date"],
            "dob_job": row["dob_job"],
            "scale_note": row["scale_note"],
            "geometry": geom,
            "drops": drops,
            "cells": cells,
            "statuses": status_vocabulary(conn, aud),
            "audience": aud,
            "can_paint": _role() in PAINT_ROLES,
            "can_collaborate": _role() in COLLAB_WRITE_ROLES,
            "can_author": is_internal(),
        }})
    finally:
        conn.close()


# ============================================================================
# WRITE — every change appends a cell_event. History is never overwritten.
# ============================================================================

def _validate_status(status_key, reason):
    if status_key not in STATUS_KEYS:
        return f"status must be one of: {', '.join(STATUS_KEYS)}"
    if status_key in REASON_REQUIRED and not (reason or "").strip():
        return f"a reason is required for '{status_key}'"
    if reason and len(reason) > MAX_REASON:
        return f"reason must be {MAX_REASON} characters or fewer"
    return None


def _cell_project(conn, cell_id):
    row = conn.execute(
        "SELECT e.project_code AS code, c.status_key AS status FROM elevation_cell c "
        "JOIN elevation_drop d ON d.id = c.drop_id "
        "JOIN elevation e ON e.id = d.elevation_id WHERE c.id=?", (cell_id,)).fetchone()
    return row


def _apply_cell(conn, cell_id, from_status, status_key, reason, internal_note, uid):
    """Update one cell + append its event. Caller owns the transaction."""
    if internal_note is None:
        conn.execute(
            "UPDATE elevation_cell SET status_key=?, reason=?, updated_by_uid=?, updated_at=? "
            "WHERE id=?", (status_key, (reason or None), uid, _now(), cell_id))
    else:
        conn.execute(
            "UPDATE elevation_cell SET status_key=?, reason=?, internal_note=?, "
            "updated_by_uid=?, updated_at=? WHERE id=?",
            (status_key, (reason or None), (internal_note or None), uid, _now(), cell_id))
    conn.execute(
        "INSERT INTO elevation_cell_event (cell_id, from_status, to_status, reason, "
        "actor_uid, created_at) VALUES (?,?,?,?,?,?)",
        (cell_id, from_status, status_key, (reason or None), uid, _now()))


def _api_post_cell():
    """POST /api/elevation/cell — {cell_id, status_key, reason?, internal_note?}"""
    if _role() not in PAINT_ROLES:
        return _err("forbidden", 403)
    data = request.get_json(silent=True) or {}
    cell_id = data.get("cell_id")
    status_key = (data.get("status_key") or "").strip()
    reason = (data.get("reason") or "").strip() or None
    internal_note = data.get("internal_note")
    if internal_note is not None:
        internal_note = (str(internal_note).strip() or None)
    if not cell_id:
        return _err("cell_id is required", 400)
    bad = _validate_status(status_key, reason)
    if bad:
        return _err(bad, 400)

    conn = _db()
    try:
        row = _cell_project(conn, cell_id)
        if row is None:
            return _err("not found", 404)
        blocked = _require_project(conn, row["code"])
        if blocked:
            return blocked
        _apply_cell(conn, cell_id, row["status"], status_key, reason, internal_note, _uid())
        conn.commit()
        return jsonify({"data": {"cell_id": cell_id, "status_key": status_key,
                                 "reason": reason}})
    finally:
        conn.close()


def _api_post_drop():
    """POST /api/elevation/drop — {drop_id, status_key, reason?, internal_note?}
    Paints every level of one drop. Still one cell_event PER CELL — a bulk action must
    not collapse into a single audit row, or the trail stops matching the cells."""
    if _role() not in PAINT_ROLES:
        return _err("forbidden", 403)
    data = request.get_json(silent=True) or {}
    drop_id = data.get("drop_id")
    status_key = (data.get("status_key") or "").strip()
    reason = (data.get("reason") or "").strip() or None
    internal_note = data.get("internal_note")
    if internal_note is not None:
        internal_note = (str(internal_note).strip() or None)
    if not drop_id:
        return _err("drop_id is required", 400)
    bad = _validate_status(status_key, reason)
    if bad:
        return _err(bad, 400)

    conn = _db()
    try:
        row = conn.execute(
            "SELECT e.project_code AS code FROM elevation_drop d "
            "JOIN elevation e ON e.id = d.elevation_id WHERE d.id=?", (drop_id,)).fetchone()
        if row is None:
            return _err("not found", 404)
        blocked = _require_project(conn, row["code"])
        if blocked:
            return blocked
        cells = conn.execute(
            "SELECT id, status_key FROM elevation_cell WHERE drop_id=? ORDER BY level_id",
            (drop_id,)).fetchall()
        for c in cells:
            _apply_cell(conn, c["id"], c["status_key"], status_key, reason,
                        internal_note, _uid())
        conn.commit()
        return jsonify({"data": {"drop_id": drop_id, "status_key": status_key,
                                 "cells_updated": len(cells)}})
    finally:
        conn.close()


def _api_cell_history(cell_id):
    """GET /api/elevation/cell/<id>/history — the append-only trail for one cell.
    Actor is rendered through the same W-#### / real-name rule as comments."""
    conn = _db()
    try:
        row = _cell_project(conn, cell_id)
        if row is None:
            return _err("not found", 404)
        blocked = _require_project(conn, row["code"])
        if blocked:
            return blocked
        rows = conn.execute(
            "SELECT from_status, to_status, reason, actor_uid, created_at "
            "FROM elevation_cell_event WHERE cell_id=? ORDER BY id", (cell_id,)).fetchall()
        internal = is_internal()
        return jsonify({"data": [
            {"from_status": r["from_status"], "to_status": r["to_status"],
             "reason": r["reason"], "at": r["created_at"],
             "actor": display_actor(conn, r["actor_uid"], internal)}
            for r in rows]})
    finally:
        conn.close()


# ============================================================================
# ACTOR DISPLAY — SSC staff are W-#### to outsiders; outsiders are named
# ============================================================================

_ACTOR_CACHE_KEY = "_elev_actor_cache"


def display_actor(conn, uid, internal_viewer: bool) -> dict:
    """How a person is named to THIS viewer.

    Internal viewer  -> real display name for everyone.
    External viewer  -> SSC STAFF render as their W-#### worker id (never a name);
                        outside parties (architect / client / vendor) show real names,
                        because an architect signing a comment anonymously is useless
                        to the owner.

    An SSC user with no linked worker record has no W-####; they render as "SSC" — a
    role-level attribution, never a name, and never a fabricated id.
    """
    if not uid:
        return {"label": "—", "kind": "unknown"}
    cache = g.setdefault(_ACTOR_CACHE_KEY, {}) if g else {}
    ck = (uid, internal_viewer)
    if ck in cache:
        return cache[ck]
    row = conn.execute(
        "SELECT u.display_name, u.full_name, u.role, u.employee_id_link "
        "FROM users u WHERE u.id=?", (uid,)).fetchone()
    if row is None:
        out = {"label": "—", "kind": "unknown"}
    else:
        name = row["display_name"] or row["full_name"] or "—"
        role = row["role"]
        if internal_viewer or role in EXTERNAL_ROLES or role == "vendor":
            out = {"label": name, "kind": "person", "role": role}
        else:
            wid = None
            if row["employee_id_link"]:
                w = conn.execute(
                    "SELECT worker_id FROM employees WHERE employee_id=?",
                    (row["employee_id_link"],)).fetchone()
                wid = w["worker_id"] if w else None
            out = ({"label": wid, "kind": "worker_id"} if wid
                   else {"label": "SSC", "kind": "org"})
    cache[ck] = out
    return out


# ============================================================================
# PAGE
# ============================================================================

def _markup_page(elev_id=None):
    """GET /drawing-markup  ·  GET /drawing-markup/<elev_id> — the standalone v2 page.

    Served DIRECTLY from templates/v2/, not through ui_version.resolve_page(): this page
    has no v1 original to fall back to, so twin-resolution has nothing to resolve. It is
    the first surface built to the v2 system, shipped standalone — the #279 toggle is
    untouched by it, and its byte-identity gate is unaffected because this route did not
    exist when that gate was measured."""
    role = _role()
    if role not in (INTERNAL_ROLES | EXTERNAL_ROLES):
        return _err("forbidden", 403)
    if not MARKUP_PAGE.exists():
        return _err("drawing markup page not found", 404)
    resp = send_file(str(MARKUP_PAGE))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def _api_my_elevations():
    """GET /api/elevations — the elevation picker, and the check that an architect sees
    ONLY assigned projects.

    #293 — elevations are AUTHORABLE: a project holds many real rows (free-text
    faces included: SE, NW, ...), each draft or confirmed. The listing serves:
      * every REAL row the audience may know about — internal sees drafts
        (status carried so the picker can grey them as "in authoring"),
        external sees CONFIRMED rows only (a draft does not exist to them);
      * canonical N/S/E/W/ALL placeholders (id=None, traced=False) for faces
        with no visible row — absent would read as "this building has no south
        side". A face whose only row is a draft still placeholders for an
        external viewer, by the same rule.
    "traced" still means there is geometry to draw, not merely that a row exists."""
    conn = _db()
    try:
        user = current_user()
        codes = accessible_codes(conn, user)
        if not codes:
            return jsonify({"data": []})
        marks = ",".join("?" for _ in codes)
        rows = conn.execute(
            f"SELECT e.id, e.project_code, e.face, e.face_label, e.name, e.status, "
            f"       e.geometry_json, p.name AS project_name "
            f"FROM elevation e LEFT JOIN projects p ON p.project_code = e.project_code "
            f"WHERE e.project_code IN ({marks}) ORDER BY e.id", tuple(codes)).fetchall()
        if not rows:
            return jsonify({"data": []})

        internal = is_internal()
        by_project, pnames = {}, {}
        for r in rows:
            pnames[r["project_code"]] = r["project_name"]
            if (r["status"] or "confirmed") != "confirmed" and not internal:
                continue
            by_project.setdefault(r["project_code"], []).append(r)
        canon = {f for f, _ in FACES}

        def entry(r):
            traced = bool((r["geometry_json"] or "").strip() not in ("", "{}"))
            label = r["face_label"] or dict(FACES).get(r["face"], r["face"])
            return {
                "id": r["id"],
                "project_code": r["project_code"],
                "project_name": pnames.get(r["project_code"]),
                "face": r["face"],
                "label": label,
                "name": r["name"] or label,
                "status": r["status"] or "confirmed",
                "traced": traced,
            }

        out = []
        for code in sorted(pnames):
            visible = by_project.get(code, [])
            by_face = {}
            for r in visible:
                by_face.setdefault(r["face"], []).append(r)
            # canonical faces first, in header order: real rows, else a placeholder
            for face, label in FACES:
                if by_face.get(face):
                    out.extend(entry(r) for r in by_face[face])
                else:
                    out.append({
                        "id": None, "project_code": code,
                        "project_name": pnames.get(code), "face": face,
                        "label": label, "name": label, "status": "confirmed",
                        "traced": False,
                    })
            # then the authored non-canonical faces (SE, NW, free text), by id
            for r in visible:
                if r["face"] not in canon:
                    out.append(entry(r))
        return jsonify({"data": out})
    finally:
        conn.close()


# ============================================================================
# ARCHITECT CONTAINMENT — the gate that makes this safe to hand outside
# ============================================================================
#
# `architect` was already in the users role CHECK, defined-not-onboarded, so nothing had
# ever contained it. pm_scoping.pm_can_access_project already returns False for architect
# on any path CARRYING a project code — but that only covers project-scoped paths. Every
# internal endpoint WITHOUT a project code in its path (and without its own role gate)
# was reachable. A `client` is contained by client_portal._client_gate; there was no
# equivalent for an architect, and one role short of containment is the whole hole.
#
# ALLOWLIST, not denylist. A route added tomorrow is closed to architects until somebody
# deliberately opens it — the same default-deny posture as #264/#267/#269.

_ARCH_ALLOW_EXACT = {
    "/drawing-markup",
    "/api/elevations",
    "/set-password",
    "/api/health",
    "/api/today",
    "/api/ui/version",        # #279 — every role may choose its own interface
}
_ARCH_ALLOW_PREFIXES = (
    "/portal/",               # #281 — the shared shell, served from the portal namespace
    "/api/portal/",           #        and its curated payloads
    "/drawing-markup/",
    "/api/elevation/",
    "/api/comments",          # step 4
    "/api/rfi",               # step 5
    "/api/meeting-request",   # step 6 (route exists only once step 6 ships)
    "/api/auth/",
    "/files/static/",
    "/p/", "/api/p/",         # #307 — a proposal link works for any signed-in role
)


def _architect_allowed(path: str) -> bool:
    return path in _ARCH_ALLOW_EXACT or any(path.startswith(p) for p in _ARCH_ALLOW_PREFIXES)


def _architect_gate():
    """Contain `architect` to the drawing-markup surface. Registered AFTER the auth gate
    (g.auth_user set) and after the client gate. Server-side; hiding nav is not access
    control. Per-PROJECT scoping is enforced separately in _require_project — this gate
    decides which ROUTES exist for an architect at all, not which projects."""
    user = current_user()
    if not user or user.get("role") != "architect":
        return None
    path = request.path
    if _architect_allowed(path):
        return None
    logging.info(f"elevation: architect contain block path={path} uid={user.get('id')}")
    if path.startswith("/api/"):
        return _err("forbidden", 403)
    from flask import redirect
    return redirect("/drawing-markup")


# ============================================================================
# STEP 4 — COMMENTS
# ============================================================================
#
# Architect and internal roles post; the CLIENT IS READ-ONLY but SEES everything. That
# is a relationship assumption, not a technical one — the architect works for the owner,
# so their comments are not confidential from the client. Confirmed with the operator.
#
# PLAIN TEXT ONLY. No HTML, no markdown, no auto-linking. Bodies are stored raw and
# escaped at every render, and the API never emits HTML. This surface is reachable by
# parties outside the company, so a stored-XSS here would execute in an SSC operator's
# session — the cheapest correct answer is that markup is never interpreted at all.
#
# SOFT DELETE ONLY: deleted_at is stamped, the row stays. A comment thread an architect
# can silently rewrite is not a record of anything.

COMMENT_TARGETS = ("drop", "photo", "rfi")
_RATE_WINDOW_SECONDS = 60
_RATE_MAX_COMMENTS = 10


def _target_project(conn, target_type, target_id):
    """The project a comment target belongs to, or None if the target does not exist.
    Resolving through the target — never trusting a project_code from the client — is
    what stops a caller attaching a comment to another job's drop.

    comment.target_id is TEXT (it has to span several target tables) but every id it
    points at is INTEGER. SQLite would coerce silently; POSTGRES WOULD RAISE. Coerce
    here so the comparison is int-to-int on both backends, and treat a non-numeric id
    as simply not found rather than as an error."""
    try:
        target_id = int(str(target_id).strip())
    except (TypeError, ValueError):
        return None
    if target_type == "drop":
        row = conn.execute(
            "SELECT e.project_code AS code FROM elevation_drop d "
            "JOIN elevation e ON e.id = d.elevation_id WHERE d.id = ?", (target_id,)).fetchone()
    elif target_type == "rfi":
        row = conn.execute("SELECT project_code AS code FROM rfi WHERE id = ?",
                           (target_id,)).fetchone()
    elif target_type == "photo":
        row = conn.execute("SELECT project_code AS code FROM field_photos WHERE id = ?",
                           (target_id,)).fetchone()
    else:
        return None
    return row["code"] if row else None


def _rate_limited(conn, uid) -> bool:
    """A crude per-user window. Enough to stop a runaway client or a stuck retry loop
    from filling the table; not a security boundary (the auth gate is)."""
    since = (datetime.now().timestamp() - _RATE_WINDOW_SECONDS)
    rows = conn.execute(
        "SELECT created_at FROM comment WHERE author_uid = ? ORDER BY id DESC LIMIT ?",
        (uid, _RATE_MAX_COMMENTS)).fetchall()
    if len(rows) < _RATE_MAX_COMMENTS:
        return False
    try:
        oldest = datetime.fromisoformat(rows[-1]["created_at"]).timestamp()
    except (TypeError, ValueError):
        return False
    return oldest > since


def _comment_row(conn, r, internal_viewer):
    return {
        "id": r["id"],
        "target_type": r["target_type"],
        "target_id": r["target_id"],
        "body": r["body"],                 # RAW text; the client escapes on render
        "at": r["created_at"],
        "author": display_actor(conn, r["author_uid"], internal_viewer),
        "mine": r["author_uid"] == _uid(),
    }


def _api_comments_list():
    """GET /api/comments?target_type=&target_id= — the thread, oldest first.
    Soft-deleted rows are omitted entirely rather than shown as tombstones."""
    ttype = (request.args.get("target_type") or "").strip()
    tid = (request.args.get("target_id") or "").strip()
    if ttype not in COMMENT_TARGETS or not tid:
        return _err("target_type and target_id are required", 400)
    conn = _db()
    try:
        code = _target_project(conn, ttype, tid)
        if code is None:
            return _err("not found", 404)
        blocked = _require_project(conn, code)
        if blocked:
            return blocked
        # #283 — comments ride their host surface. The route itself sits behind the
        # client's 'drawing' grant (client_portal gate); an RFI thread belongs to the
        # RFIs section, so a client additionally needs 'rfis' to read one.
        if _role() == "client" and ttype == "rfi":
            import client_grants
            if not client_grants.has_grant(conn, _uid(), code, "rfis"):
                return _err("forbidden", 403)
        rows = conn.execute(
            "SELECT id, target_type, target_id, body, author_uid, created_at "
            "FROM comment WHERE project_code=? AND target_type=? AND target_id=? "
            "AND deleted_at IS NULL ORDER BY id", (code, ttype, str(tid))).fetchall()
        internal = is_internal()
        return jsonify({"data": [_comment_row(conn, r, internal) for r in rows],
                        "can_post": _role() in COLLAB_WRITE_ROLES})
    finally:
        conn.close()


def _api_comments_post():
    """POST /api/comments — {target_type, target_id, body}. Client is READ-ONLY here."""
    if _role() not in COLLAB_WRITE_ROLES:
        return _err("forbidden", 403)
    data = request.get_json(silent=True) or {}
    ttype = (data.get("target_type") or "").strip()
    tid = str(data.get("target_id") or "").strip()
    body = (data.get("body") or "").strip()
    if ttype not in COMMENT_TARGETS or not tid:
        return _err(f"target_type must be one of {', '.join(COMMENT_TARGETS)}", 400)
    if not body:
        return _err("a comment body is required", 400)
    if len(body) > MAX_COMMENT:
        return _err(f"a comment must be {MAX_COMMENT} characters or fewer", 400)
    conn = _db()
    try:
        code = _target_project(conn, ttype, tid)
        if code is None:
            return _err("not found", 404)
        blocked = _require_project(conn, code)
        if blocked:
            return blocked
        if _rate_limited(conn, _uid()):
            return _err("too many comments just now — wait a moment and try again", 429)
        cur = conn.execute(
            "INSERT INTO comment (project_code, target_type, target_id, body, author_uid, "
            "created_at) VALUES (?,?,?,?,?,?)", (code, ttype, tid, body, _uid(), _now()))
        conn.commit()
        row = conn.execute(
            "SELECT id, target_type, target_id, body, author_uid, created_at "
            "FROM comment WHERE id=?", (cur.lastrowid,)).fetchone()
        logging.info(f"comment: uid={_uid()} {ttype}/{tid} on {code}")   # never the body
        return jsonify({"data": _comment_row(conn, row, is_internal())}), 201
    finally:
        conn.close()


def _api_comment_delete(comment_id):
    """DELETE /api/comments/<id> — SOFT delete. Author may remove their own; an admin may
    remove any. Nothing is ever hard-deleted."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, project_code, author_uid, deleted_at FROM comment WHERE id=?",
            (comment_id,)).fetchone()
        if row is None:
            return _err("not found", 404)
        blocked = _require_project(conn, row["project_code"])
        if blocked:
            return blocked
        if not (row["author_uid"] == _uid() or _role() == "admin"):
            return _err("forbidden", 403)
        if row["deleted_at"] is None:
            conn.execute("UPDATE comment SET deleted_at=? WHERE id=?", (_now(), comment_id))
            conn.commit()
        return jsonify({"data": {"id": comment_id, "deleted": True}})
    finally:
        conn.close()


# ============================================================================
# STEP 5 — RFIs
# ============================================================================
#
# An architect may RAISE an RFI; internal roles raise and respond (respond = close, plus
# the comment thread from step 4). drop_id and level_id are NULLABLE AND EDITABLE AFTER
# CREATION — an RFI is often raised before anyone knows which drop it belongs to, and
# forcing that decision at creation time is how RFIs end up filed against the wrong bay.

RFI_STATUSES = ("open", "closed")
MAX_RFI_TITLE = 200
MAX_RFI_BODY = 4000


def _next_rfi_number(conn, code) -> str:
    """RFI-001, RFI-002, … per project. Numeric max, never lexicographic — 'RFI-010'
    sorts before 'RFI-9' as text (the CLAUDE.md zero-padded-id rule)."""
    rows = conn.execute("SELECT number FROM rfi WHERE project_code=?", (code,)).fetchall()
    top = 0
    for r in rows:
        m = re.match(r"^RFI-(\d+)$", (r["number"] or "").strip())
        if m:
            top = max(top, int(m.group(1)))
    return f"RFI-{top + 1:03d}"


def _rfi_row(conn, r, internal_viewer):
    return {
        "id": r["id"],
        "number": r["number"],
        "title": r["title"],
        "body": r["body"],
        "status": r["status"],
        "drop_id": r["drop_id"],
        "level_id": r["level_id"],
        "raised_at": r["raised_at"],
        "closed_at": r["closed_at"],
        "raised_by": display_actor(conn, r["raised_by_uid"], internal_viewer),
    }


def _api_rfi_list():
    """GET /api/rfis?elevation_id= — the RFI list beside the drawing. Those carrying a
    drop_id render as a pin on that drop, for every role."""
    elev_id = request.args.get("elevation_id")
    conn = _db()
    try:
        if elev_id:
            row = conn.execute("SELECT project_code FROM elevation WHERE id=?",
                               (elev_id,)).fetchone()
            if row is None:
                return _err("not found", 404)
            code = row["project_code"]
        else:
            return _err("elevation_id is required", 400)
        blocked = _require_project(conn, code)
        if blocked:
            return blocked
        rows = conn.execute(
            "SELECT id, number, title, body, status, drop_id, level_id, raised_by_uid, "
            "       raised_at, closed_at FROM rfi WHERE project_code=? "
            "ORDER BY (status='closed'), id DESC", (code,)).fetchall()
        internal = is_internal()
        return jsonify({"data": [_rfi_row(conn, r, internal) for r in rows],
                        "can_raise": _role() in COLLAB_WRITE_ROLES,
                        "can_close": is_internal()})
    finally:
        conn.close()


def _api_rfi_create():
    """POST /api/rfis — {elevation_id, title, body?, drop_id?, level_id?}."""
    if _role() not in COLLAB_WRITE_ROLES:
        return _err("forbidden", 403)
    data = request.get_json(silent=True) or {}
    elev_id = data.get("elevation_id")
    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip() or None
    drop_id = data.get("drop_id")
    level_id = (data.get("level_id") or "").strip() or None
    if not elev_id:
        return _err("elevation_id is required", 400)
    if not title:
        return _err("a title is required", 400)
    if len(title) > MAX_RFI_TITLE:
        return _err(f"title must be {MAX_RFI_TITLE} characters or fewer", 400)
    if body and len(body) > MAX_RFI_BODY:
        return _err(f"body must be {MAX_RFI_BODY} characters or fewer", 400)
    conn = _db()
    try:
        row = conn.execute("SELECT id, project_code FROM elevation WHERE id=?",
                           (elev_id,)).fetchone()
        if row is None:
            return _err("not found", 404)
        code = row["project_code"]
        blocked = _require_project(conn, code)
        if blocked:
            return blocked
        if drop_id and not _drop_in_elevation(conn, drop_id, elev_id):
            return _err("that drop is not on this elevation", 400)
        number = _next_rfi_number(conn, code)
        cur = conn.execute(
            "INSERT INTO rfi (project_code, elevation_id, drop_id, level_id, number, title, "
            "body, raised_by_uid, raised_at, status) VALUES (?,?,?,?,?,?,?,?,?,'open')",
            (code, elev_id, drop_id or None, level_id, number, title, body, _uid(), _now()))
        conn.commit()
        new = conn.execute(
            "SELECT id, number, title, body, status, drop_id, level_id, raised_by_uid, "
            "raised_at, closed_at FROM rfi WHERE id=?", (cur.lastrowid,)).fetchone()
        logging.info(f"rfi: {number} raised uid={_uid()} on {code}")
        return jsonify({"data": _rfi_row(conn, new, is_internal())}), 201
    finally:
        conn.close()


def _drop_in_elevation(conn, drop_id, elev_id) -> bool:
    return bool(conn.execute("SELECT 1 FROM elevation_drop WHERE id=? AND elevation_id=?",
                             (drop_id, elev_id)).fetchone())


def _api_rfi_update(rfi_id):
    """PATCH /api/rfis/<id> — {drop_id?, level_id?, status?}.

    ATTACHING LATER IS THE POINT: drop_id/level_id are editable after creation, so an RFI
    raised before anyone knew the bay can be filed to it once they do. Only an INTERNAL
    role may close one — an architect raising and then closing their own question would
    make the log meaningless."""
    data = request.get_json(silent=True) or {}
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, project_code, elevation_id, status FROM rfi WHERE id=?",
            (rfi_id,)).fetchone()
        if row is None:
            return _err("not found", 404)
        blocked = _require_project(conn, row["project_code"])
        if blocked:
            return blocked

        sets, params = [], []
        if "drop_id" in data:
            d = data.get("drop_id")
            if d and not _drop_in_elevation(conn, d, row["elevation_id"]):
                return _err("that drop is not on this elevation", 400)
            sets.append("drop_id=?")
            params.append(d or None)
        if "level_id" in data:
            sets.append("level_id=?")
            params.append((data.get("level_id") or "").strip() or None)
        if "status" in data:
            st = (data.get("status") or "").strip()
            if st not in RFI_STATUSES:
                return _err(f"status must be one of {', '.join(RFI_STATUSES)}", 400)
            if not is_internal():
                return _err("only an internal role may open or close an RFI", 403)
            sets.append("status=?")
            params.append(st)
            sets.append("closed_at=?")
            params.append(_now() if st == "closed" else None)
        if not sets:
            return _err("nothing to update", 400)
        if _role() not in COLLAB_WRITE_ROLES:
            return _err("forbidden", 403)

        params.append(rfi_id)
        conn.execute(f"UPDATE rfi SET {', '.join(sets)} WHERE id=?", tuple(params))
        conn.commit()
        new = conn.execute(
            "SELECT id, number, title, body, status, drop_id, level_id, raised_by_uid, "
            "raised_at, closed_at FROM rfi WHERE id=?", (rfi_id,)).fetchone()
        return jsonify({"data": _rfi_row(conn, new, is_internal())})
    finally:
        conn.close()


def register(app) -> None:
    """MUST follow apply_auth_gate (current_user populated) and the #263 scoping hook."""
    app.before_request(_architect_gate)
    app.add_url_rule("/drawing-markup", "drawing_markup_page", _markup_page, methods=["GET"])
    app.add_url_rule("/drawing-markup/<int:elev_id>", "drawing_markup_page_id",
                     _markup_page, methods=["GET"])
    app.add_url_rule("/api/elevations", "elevation_list", _api_my_elevations, methods=["GET"])
    app.add_url_rule("/api/elevation/<int:elev_id>", "elevation_get",
                     _api_get_elevation, methods=["GET"])
    app.add_url_rule("/api/elevation/cell", "elevation_cell_post",
                     _api_post_cell, methods=["POST"])
    app.add_url_rule("/api/elevation/drop", "elevation_drop_post",
                     _api_post_drop, methods=["POST"])
    app.add_url_rule("/api/elevation/cell/<int:cell_id>/history", "elevation_cell_history",
                     _api_cell_history, methods=["GET"])
    # step 4 — comments
    app.add_url_rule("/api/comments", "comments_list", _api_comments_list, methods=["GET"])
    app.add_url_rule("/api/comments", "comments_post", _api_comments_post, methods=["POST"])
    app.add_url_rule("/api/comments/<int:comment_id>", "comment_delete",
                     _api_comment_delete, methods=["DELETE"])
    # step 5 — RFIs
    # #305 — /api/rfis GET+POST are owned by server.py (project RFIs); it dispatches
    # to _api_rfi_list / _api_rfi_create when an elevation_id is present. Registering
    # them here too made the project RFI form dead (first-registered rule wins).
    app.add_url_rule("/api/rfis/<int:rfi_id>", "rfi_update", _api_rfi_update,
                     methods=["PATCH"])
