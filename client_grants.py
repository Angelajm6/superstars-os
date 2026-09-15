"""#269 — SELECTIVE CLIENT UN-GATING: per-client, per-section, DEFAULT-OFF access grants.

The "gated day 1, unlock over time" mechanism (North Star §6/§8). This module owns the
SECTION-grant layer — the first of the TWO independent default-deny dials a client sits
behind (both server-enforced; hiding UI is never the control):

  1. SECTION grant (here, client_section_grant) — may the client see the section AT ALL.
     Presence of a row = unlocked; NO row = locked. A client with ZERO granted sections
     stays hard-contained on /welcome exactly as #267 built it.
  2. ITEM visibility (#264, visibility.py)      — within photos/documents, WHICH items.
     Still default-deny, shared one item at a time; red-flag still takes items offline.

So "photos granted" shows the photos SECTION rendering ONLY the individually
client-shared photos; an unshared photo stays invisible (and 404 by ID) even inside a
granted section.

Enforcement points:
  * client_portal._client_gate consumes granted_sections() to route: 0 grants -> the
    #267 welcome hard-stop (unchanged); >=1 grant -> the portal, which renders ONLY
    granted sections.
  * every /api/portal/<section> endpoint calls require_section() — grant + project
    scope re-derived per request; a non-granted section's endpoint returns 403 to the
    client no matter how it's addressed.
  * the grant/revoke/list endpoints below are admin/c_suite ONLY (@requires_company).

PII discipline (CLAUDE.md): payloads carry user_id / email / display_name / project_code
/ section names only — never worker names, rates, PINs, or *_path values. Dates are
LOCAL ISO (never UTC). All SQL is parameterized and routes through the caller's db_layer
connection — identical on SQLite (default/production) and Postgres.
"""
from __future__ import annotations

import logging

from flask import jsonify, request

import db_layer
from auth import _db, _now_iso, current_user, requires_company

# The grantable client sections, in portal display order. A future section is an
# additive change HERE (+ its portal endpoint/UI) — no schema change (no CHECK by
# design, see schema_client_grants_269.sql).
SECTIONS = ("progress", "photos", "documents", "proposals", "daily", "schedule",
            # #308 — proposals: the client reads, downloads and signs the proposals
            # sent to their organization (#307 rows). DEFAULT OFF like every grant.
            # #281 — the shared-shell sections. All DEFAULT OFF like every other grant:
            # adding a key here grants nobody anything, it only makes the toggle exist.
            "drawing", "weekly", "materials", "rfis")

# Labels travel WITH the catalog now. They used to live only in admin_projects.html, so a
# new key rendered as its raw slug until someone remembered to add it in a second place.
SECTION_LABELS = {
    "progress": "Progress", "photos": "Photos", "documents": "Documents",
    "proposals": "Proposals",
    "daily": "Daily Reports", "schedule": "Schedule",
    "drawing": "Drawing Markup", "weekly": "Weekly Summary",
    "materials": "Materials", "rfis": "RFIs",
}

# #283 — sections whose portal payload actually EXISTS today. A toggle for a section
# that opens to nothing is a broken promise (the deliberate-empty-state rule), so the
# admin UI hides catalog keys that are not yet served. SECTIONS is what MAY be granted;
# SERVED_SECTIONS is what a grant OPENS today. weekly/materials stay catalogued but
# hidden until their payloads ship — adding a payload later means adding its key HERE.
SERVED_SECTIONS = ("progress", "photos", "documents", "proposals", "daily", "schedule",
                   "drawing", "rfis")

# #270 — access presets: named bundles an admin applies in one click. IN CODE, no
# schema — applying one REPLACES the client's grant set with these sections (the
# per-section toggles remain the fine-grained control). Order = SECTIONS order.
# #281 — every preset is an EXPLICIT tuple, never a reference to SECTIONS.
#
# "full" used to BE the SECTIONS tuple, so adding a grantable key silently added it to
# the Full preset — every client on Full would have been granted new sections the moment
# the catalog grew, with nobody deciding that. A preset is an editorial choice about
# what a bundle means; the catalog is just what exists. _assert_preset_integrity()
# below enforces the distinction at import.
#
# #283 — drawing + rfis joined FULL (explicitly, by decision) and stay OUT of Standard.
# weekly / materials remain in NO preset: they are not even served yet.
PRESETS = {
    "minimal": ("progress",),
    "standard": ("progress", "photos", "daily"),
    "full": ("progress", "photos", "documents", "proposals", "daily", "schedule", "drawing", "rfis"),   # #308 — proposals joined FULL by decision
}
PRESET_LABELS = {"minimal": "Minimal", "standard": "Standard", "full": "Full view"}


def _assert_preset_integrity() -> None:
    """Import-time guard (the #281 lesson, made structural). VALUE-based on purpose:
    identity checks on literal tuples are defeated by CPython constant interning
    (two equal literals can be ONE object). The property that matters is the value
    contract — if a preset ever aliases the catalog again, the unserved keys it
    would inherit (weekly, materials) trip these assertions at import."""
    for name, bundle in PRESETS.items():
        unknown = [s for s in bundle if s not in SECTIONS]
        if unknown:
            raise RuntimeError(f"preset {name!r} bundles unknown section(s): {unknown}")
        unserved = [s for s in bundle if s not in SERVED_SECTIONS]
        if unserved:
            raise RuntimeError(
                f"preset {name!r} bundles unserved section(s) {unserved} — a preset "
                f"must never grant a section that opens to nothing")
    for s in ("drawing", "rfis", "weekly", "materials"):
        if s in PRESETS["standard"]:
            raise RuntimeError(f"'standard' must not bundle {s!r} — Full-only by decision")
    for s in ("drawing", "rfis"):
        if s not in PRESETS["full"]:
            raise RuntimeError(f"'full' must bundle {s!r} explicitly (#283 decision)")


_assert_preset_integrity()


_TABLE_READY = False   # cached once True — a created table never disappears in-process


def table_ready(conn) -> bool:
    """True when client_section_grant exists — a CATALOG probe (sqlite_master /
    information_schema), never a query against the table itself: on Postgres a failed
    statement aborts the surrounding transaction, so callers that touch grants inside a
    larger write (pm_scoping assignment hygiene) must probe this way, not try/except.
    Every read helper below self-guards with this so an UNMIGRATED database degrades to
    the safe posture — every section locked, the #267 welcome hard-stop — instead of
    500ing every client request."""
    global _TABLE_READY
    if _TABLE_READY:
        return True
    if db_layer.is_postgres():
        found = bool(conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='client_section_grant'").fetchone())
    else:
        found = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_section_grant'"
        ).fetchone())
    if found:
        _TABLE_READY = True
    return found


# ============= QUERIES (the single source every gate/endpoint reads) =============

def granted_sections(conn, user_id, project_code) -> set:
    """The sections `user_id` has been granted on `project_code`. Empty set = fully
    contained (#267). DEFAULT-OFF BY CONSTRUCTION: only explicit rows unlock."""
    if not project_code or not table_ready(conn):
        return set()
    rows = conn.execute(
        "SELECT section FROM client_section_grant WHERE user_id=? AND project_code=?",
        (user_id, project_code)).fetchall()
    return {r[0] for r in rows if r[0] in SECTIONS}


def has_grant(conn, user_id, project_code, section) -> bool:
    if not project_code or section not in SECTIONS or not table_ready(conn):
        return False
    return bool(conn.execute(
        "SELECT 1 FROM client_section_grant WHERE user_id=? AND project_code=? AND section=?",
        (user_id, project_code, section)).fetchone())


def grants_by_user(conn, user_ids) -> dict:
    """{user_id: sorted [sections]} for the admin screen — one batched query."""
    ids = [int(u) for u in user_ids]
    if not ids:
        return {}
    if not table_ready(conn):
        return {u: [] for u in ids}
    rows = conn.execute(
        f"SELECT user_id, section FROM client_section_grant "
        f"WHERE user_id IN ({','.join(['?'] * len(ids))})", ids).fetchall()
    out = {u: [] for u in ids}
    for r in rows:
        if r[1] in SECTIONS:
            out.setdefault(r[0], []).append(r[1])
    return {u: sorted(s) for u, s in out.items()}


# ============= ADMIN ENDPOINTS (admin/c_suite ONLY, server-enforced) =============

def _client_and_project(conn, user_id):
    """Validate the grant target: an active `client` user + their ONE assigned project.
    Returns (client_row, project_code, error_response)."""
    row = conn.execute(
        "SELECT id, role, status, is_active FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return None, None, (jsonify({"error": "user not found"}), 404)
    if row["role"] != "client":
        return None, None, (jsonify({"error": "section grants apply to client users only"}), 400)
    if row["status"] != "active" or not row["is_active"]:
        return None, None, (jsonify(
            {"error": "client account is not active — reactivate it before granting access"}), 400)
    pr = conn.execute(
        "SELECT project_code FROM pm_project_assignment WHERE user_id=? "
        "ORDER BY project_code LIMIT 1", (user_id,)).fetchone()
    if not pr:
        return None, None, (jsonify(
            {"error": "client has no assigned project — assign a project first"}), 400)
    return row, pr[0], None


@requires_company
def _api_list_grants():
    """GET /api/admin/client-grants[?user_id=N] — grants per client (all clients, or one).
    Shape: {data:{sections:[...], clients:[{user_id, project_code, sections:[...]}]}}"""
    want = request.args.get("user_id")
    conn = _db()
    try:
        if want is not None:
            try:
                ids = [int(want)]
            except ValueError:
                return jsonify({"error": "user_id must be an integer"}), 400
        else:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM users WHERE role='client' AND status='active' AND is_active=1"
            ).fetchall()]
        per_user = grants_by_user(conn, ids)
        clients = []
        for uid in ids:
            pr = conn.execute(
                "SELECT project_code FROM pm_project_assignment WHERE user_id=? "
                "ORDER BY project_code LIMIT 1", (uid,)).fetchone()
            clients.append({"user_id": uid,
                            "project_code": pr[0] if pr else None,
                            "sections": per_user.get(uid, [])})
        # `sections` is kept for anything already reading it. `grantable_sections`,
        # `section_labels` and `presets` are what the admin UI actually reads — it was
        # written to be data-driven but looked for `grantable_sections`, which nothing
        # emitted, so it silently fell back to a hard-coded list and a new key here would
        # never have reached the screen. Serving the catalog closes that.
        return jsonify({"data": {
            "sections": list(SECTIONS),
            "grantable_sections": list(SECTIONS),
            # #283 — the UI renders toggles only for grantable ∩ served; a catalogued
            # key with no payload yet stays invisible rather than opening to nothing.
            "served_sections": list(SERVED_SECTIONS),
            "section_labels": dict(SECTION_LABELS),
            "presets": {k: list(v) for k, v in PRESETS.items()},
            "preset_labels": dict(PRESET_LABELS),
            "clients": clients,
        }})
    finally:
        conn.close()


@requires_company
def _api_set_grant():
    """POST /api/admin/client-grants — {user_id, section, on:bool} grants (on) or revokes
    (off) ONE section for a client on their assigned project. Idempotent both ways.
    admin/c_suite ONLY. The project is re-derived server-side from the client's
    assignment — a caller can't grant a section on someone else's project."""
    actor = current_user() or {}
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    section = (data.get("section") or "").strip().lower()
    on = bool(data.get("on"))
    if user_id is None or not section:
        return jsonify({"error": "user_id and section are required"}), 400
    if section not in SECTIONS:
        return jsonify({"error": f"unknown section (valid: {', '.join(SECTIONS)})"}), 400
    conn = _db()
    try:
        if not table_ready(conn):
            return jsonify({"error": "grants schema not migrated — run apply_client_grants_269.py"}), 503
        _cl, code, err = _client_and_project(conn, user_id)
        if err:
            return err
        if on:
            conn.execute(
                "INSERT OR IGNORE INTO client_section_grant "
                "(user_id, project_code, section, granted_by, granted_at) VALUES (?,?,?,?,?)",
                (user_id, code, section, actor.get("id"), _now_iso()))
        else:
            conn.execute(
                "DELETE FROM client_section_grant WHERE user_id=? AND project_code=? AND section=?",
                (user_id, code, section))
        conn.commit()
        sections = sorted(granted_sections(conn, user_id, code))
        logging.info(
            f"client_grants: {'grant' if on else 'revoke'} section={section} "
            f"user_id={user_id} project={code} by={actor.get('id')} now={sections}")
        return jsonify({"data": {"user_id": user_id, "project_code": code,
                                 "section": section, "on": on, "sections": sections}})
    finally:
        conn.close()


@requires_company
def _api_apply_preset():
    """POST /api/admin/client-grants/preset — {user_id, preset} REPLACES the client's
    grant set with the preset's sections (one transaction: delete + insert, stamped
    granted_by/granted_at). admin/c_suite ONLY; same validation as _api_set_grant
    (active client + assigned project, project re-derived server-side). Idempotent —
    re-applying a preset yields the same set. NOT additive: applying `standard` over
    `full` removes documents/schedule (that's the point of a preset)."""
    actor = current_user() or {}
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    preset = (data.get("preset") or "").strip().lower()
    if user_id is None or not preset:
        return jsonify({"error": "user_id and preset are required"}), 400
    if preset not in PRESETS:
        return jsonify({"error": f"unknown preset (valid: {', '.join(PRESETS)})"}), 400
    conn = _db()
    try:
        if not table_ready(conn):
            return jsonify({"error": "grants schema not migrated — run apply_client_grants_269.py"}), 503
        _cl, code, err = _client_and_project(conn, user_id)
        if err:
            return err
        now = _now_iso()
        conn.execute("DELETE FROM client_section_grant WHERE user_id=? AND project_code=?",
                     (user_id, code))
        for s in PRESETS[preset]:
            conn.execute(
                "INSERT INTO client_section_grant "
                "(user_id, project_code, section, granted_by, granted_at) VALUES (?,?,?,?,?)",
                (user_id, code, s, actor.get("id"), now))
        conn.commit()
        sections = sorted(granted_sections(conn, user_id, code))
        logging.info(
            f"client_grants: preset={preset} applied user_id={user_id} project={code} "
            f"by={actor.get('id')} now={sections}")
        return jsonify({"data": {"user_id": user_id, "project_code": code,
                                 "preset": preset, "sections": sections}})
    finally:
        conn.close()


def register(app) -> None:
    """Wire the admin grant endpoints. The gate/endpoint enforcement lives in
    client_portal (which imports this module) — call AFTER client_portal.register."""
    app.add_url_rule("/api/admin/client-grants", "client_grants_list",
                     _api_list_grants, methods=["GET"])
    app.add_url_rule("/api/admin/client-grants", "client_grants_set",
                     _api_set_grant, methods=["POST"])
    app.add_url_rule("/api/admin/client-grants/preset", "client_grants_preset",
                     _api_apply_preset, methods=["POST"])   # #270 — one-click bundles
