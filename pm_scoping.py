"""#263 — PM project-scoping: project-membership assignment + access enforcement.

Two INDEPENDENT axes (see access.py / CLAUDE.md):
  * ROLE       -> which SECTIONS a user can reach (access.SECTION_ACCESS, #262) and
                 company-vs-project (access.can_access_company, #263).
  * ASSIGNMENT -> which PROJECTS a `pm` may open (the pm_project_assignment table).

This module owns the ASSIGNMENT axis end to end:
  * pm_can_access_project(role, user_id, code) — the ONE predicate, server-enforced.
  * a before_request hook that rejects any request whose path carries a project_code
    (`/projects/<code>` or `…/projects/<code>/…`, incl. /api/dropplan/projects/<code>/)
    the current user can't access — one choke point covers every project-scoped route.
  * admin/c_suite-only endpoints to assign/unassign a pm's projects + close/reopen a
    project (the lifecycle). A pm (or anyone non-company) CANNOT assign — 403.
  * project-list scoping: a pm sees only assigned ACTIVE projects (closed excluded).

SECURITY: enforced SERVER-SIDE. Hiding nav is not access control — a crafted request
from a pm for an unassigned (or closed) project, or for the company console, is rejected
here with 403, never served the data.

PII discipline (CLAUDE.md): assignment payloads carry user_id / project_code / role /
display_name / email only — never worker names, rates, PINs, or *_path values. Dates are
LOCAL ISO strings (never UTC). All SQL is parameterized and routed through db_layer so it
runs identically on SQLite (default/production) and Postgres (the dual-backend gate).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from flask import Response, jsonify, request, send_file

import access
import client_grants
from auth import _db, _now_iso, current_user, requires_company

SCRIPT_DIR = Path(__file__).resolve().parent
ADMIN_PROJECTS_PAGE = SCRIPT_DIR / "admin_projects.html"

# A path carries a project_code when it contains a `/projects/<code>` segment. This
# matches BOTH `/projects/<code>` (the dashboard page) and every project-scoped API —
# `/api/projects/<code>/…` AND `/api/dropplan/projects/<code>/…`. The bare landing
# `/projects` and the list `/api/projects` have no trailing segment, so they don't match.
_PROJECT_CODE_RE = re.compile(r"/projects/([^/?#]+)")

_FORBIDDEN_PROJECT_HTML = (
    "<!doctype html><meta charset=utf-8><title>Not authorized</title>"
    "<div style=\"font-family:Inter,system-ui,sans-serif;max-width:520px;margin:18vh auto;"
    "text-align:center;color:#222633\">"
    "<div style=\"font-size:15px;font-weight:700;color:#B11E2E;letter-spacing:.5px\">NOT AUTHORIZED</div>"
    "<p style=\"color:#76777E;font-size:14px;line-height:1.6;margin:14px 0 22px\">"
    "You aren't assigned to this project. If you think this is a mistake, ask an admin to "
    "assign it to you.</p>"
    "<a href=\"/projects\" style=\"display:inline-block;background:#B11E2E;color:#fff;"
    "text-decoration:none;padding:10px 20px;border-radius:6px;font-size:13px;font-weight:600\">"
    "&larr; Back to Projects</a></div>"
)


# ============= DATA HELPERS =============

def project_code_from_path(path: str):
    """The project_code embedded in `path`, or None. request.path is already URL-decoded
    by Werkzeug, so the raw segment is the literal project_code."""
    if not path:
        return None
    m = _PROJECT_CODE_RE.search(path)
    return m.group(1) if m else None


def assigned_codes(user_id, conn) -> set:
    rows = conn.execute(
        "SELECT project_code FROM pm_project_assignment WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {r[0] for r in rows}


def _project_status(code, conn):
    row = conn.execute(
        "SELECT status FROM projects WHERE project_code = ?", (code,)
    ).fetchone()
    if row is None:
        return None
    return (row[0] or "active")


def pm_can_access_project(role, user_id, code, conn=None) -> bool:
    """The single project-access predicate (server-enforced):
      * admin/c_suite  -> any project, active OR closed (closed stays visible for records).
      * pm             -> only an ASSIGNED project that is currently ACTIVE.
      * other roles    -> any ACTIVE project (not assignment-scoped; no closed records).
    `code` that names no project -> False for non-company roles (nothing to open)."""
    if access.can_access_company(role):
        return True
    own = conn or _db()
    try:
        status = _project_status(code, own)
        if status is None:
            return False
        if role == "pm":
            return status == "active" and code in assigned_codes(user_id, own)
        if role in ("client", "architect", "vendor"):
            # #264 — external roles NEVER reach internal project endpoints; they use their
            # own curated surface (the client portal). Defense-in-depth behind the client
            # default-deny gate. Their per-resource access is checked there, not here.
            return False
        if role == "estimator":
            # #276 — the estimator works the estimating QUEUE (/estimating), never
            # project dashboards: leads live pre-project; converted work is ops' world.
            return False
        return status == "active"   # super (internal field tier): active projects
    finally:
        if own is not conn:
            own.close()


def filter_visible_projects(rows, role, user_id, conn):
    """Scope a list of project rows for the projects view / /api/projects:
      * admin/c_suite -> all rows (incl. closed, for records).
      * pm            -> assigned AND active only.
      * other roles   -> active only."""
    if access.can_access_company(role):
        return rows
    if role == "estimator":
        return []   # #276 — no project list; the estimator's world is /estimating
    assigned = assigned_codes(user_id, conn) if role == "pm" else None
    out = []
    for r in rows:
        status = (r["status"] if "status" in r.keys() else None) or "active"
        if status != "active":
            continue
        if role == "pm" and r["project_code"] not in assigned:
            continue
        out.append(r)
    return out


# ============= BEFORE-REQUEST SCOPING HOOK =============

def _scoping_gate():
    """Reject any project-scoped request the current user can't access. Registered AFTER
    auth.apply_auth_gate, so g.auth_user is already set for authenticated requests (and
    None for public/unauthenticated ones, which this hook lets through untouched)."""
    user = current_user()
    if not user:
        return None
    # #267 — a `client` is governed ENTIRELY by the client containment gate (client_portal):
    # every page -> /welcome, every API -> 403. Defer here so a project PAGE bounces to
    # /welcome instead of showing this gate's 403 (pm_can_access_project still denies clients
    # everywhere else, defense-in-depth intact).
    if user.get("role") == "client":
        return None
    code = project_code_from_path(request.path)
    if code is None:
        return None
    if pm_can_access_project(user.get("role"), user.get("id"), code):
        return None
    logging.info(
        f"pm_scoping: project access denied role={user.get('role')} "
        f"user_id={user.get('id')} path={request.path}")
    if request.path.startswith("/api/"):
        return jsonify({"error": "forbidden"}), 403
    return Response(_FORBIDDEN_PROJECT_HTML, status=403, mimetype="text/html")


# ============= ASSIGNMENT + LIFECYCLE ENDPOINTS (admin/c_suite ONLY) =============

def _project_exists(conn, code) -> bool:
    return conn.execute(
        "SELECT 1 FROM projects WHERE project_code = ?", (code,)).fetchone() is not None


@requires_company
def _list_assignments():
    """GET /api/admin/pm-assignments — the assignment matrix for the admin screen:
    every project (with status) + every real, active pm and their assigned codes."""
    import project_setup   # the job's calendar color + number, same as the Projects tab
    conn = _db()
    try:
        # job_number / color_index / address are added by later migrations; select only what exists
        extra = [c for c in ("job_number", "color_index", "address")
                 if project_setup._has_column(conn, "projects", c)]
        projects = []
        for r in conn.execute(
                "SELECT project_code, name, status" + "".join(", " + c for c in extra) + " FROM projects "
                "ORDER BY (status='closed'), name").fetchall():
            row = {"project_code": r["project_code"], **{c: r[c] for c in extra}}
            projects.append({
                "project_code": r["project_code"], "name": r["name"],
                "status": (r["status"] or "active"),
                "job_number": row.get("job_number"),
                "address": row.get("address") or "",
                "color": project_setup.color_hex(row),
            })
        pms = []
        for u in conn.execute(
            "SELECT id, email, display_name, full_name FROM users "
            "WHERE role='pm' AND status='active' AND is_active=1 AND COALESCE(is_system,0)=0 "
            "ORDER BY LOWER(COALESCE(display_name, full_name, email))"
        ).fetchall():
            pms.append({
                "user_id": u["id"],
                "email": u["email"],
                "display_name": u["display_name"] or u["full_name"] or u["email"],
                "project_codes": sorted(assigned_codes(u["id"], conn)),
            })
        # #264 — external clients (read-only portal), each scoped to ONE project.
        # #269 — each carries its granted sections (default OFF; empty = welcome-contained).
        client_rows = conn.execute(
            "SELECT id, email, display_name, full_name FROM users "
            "WHERE role='client' AND status='active' AND is_active=1 AND COALESCE(is_system,0)=0 "
            "ORDER BY LOWER(COALESCE(display_name, full_name, email))"
        ).fetchall()
        per_user = client_grants.grants_by_user(conn, [u["id"] for u in client_rows])
        clients = []
        for u in client_rows:
            codes = sorted(assigned_codes(u["id"], conn))
            clients.append({
                "user_id": u["id"],
                "email": u["email"],
                "display_name": u["display_name"] or u["full_name"] or u["email"],
                "project_code": codes[0] if codes else None,
                "sections": per_user.get(u["id"], []),
            })
        return jsonify({"data": {"projects": projects, "pms": pms, "clients": clients,
                                 "grantable_sections": list(client_grants.SECTIONS),
                                 # #270 — named grant bundles (apply = replace-set)
                                 "presets": {k: list(v) for k, v in client_grants.PRESETS.items()},
                                 "preset_labels": dict(client_grants.PRESET_LABELS)}})
    finally:
        conn.close()


@requires_company
def _set_assignments():
    """PUT /api/admin/pm-assignments — {user_id, project_codes:[...]} replaces a pm's
    assignment set (assign + unassign in one save). admin/c_suite ONLY (server-enforced).
    Validates the target is an active pm and every code names a real project."""
    actor = current_user()
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    codes = data.get("project_codes")
    if user_id is None or not isinstance(codes, list):
        return jsonify({"error": "user_id and project_codes[] are required"}), 400
    # de-dupe, drop blanks, keep as strings
    codes = sorted({str(c).strip() for c in codes if str(c).strip()})
    conn = _db()
    try:
        target = conn.execute(
            "SELECT id, role, status, is_active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not target:
            return jsonify({"error": "user not found"}), 404
        if target["role"] not in ("pm", "client"):
            return jsonify({"error": "project assignments apply to pm or client users only"}), 400
        # #264 — a client is scoped to a SINGLE project (the portal shows exactly one).
        if target["role"] == "client" and len(codes) > 1:
            return jsonify({"error": "a client is scoped to a single project"}), 400
        for c in codes:
            if not _project_exists(conn, c):
                return jsonify({"error": f"unknown project: {c}"}), 400
        # Replace-set: clear then re-insert the chosen codes (transactional).
        conn.execute("DELETE FROM pm_project_assignment WHERE user_id = ?", (user_id,))
        now = _now_iso()
        for c in codes:
            conn.execute(
                "INSERT INTO pm_project_assignment (user_id, project_code, assigned_by, assigned_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, c, (actor or {}).get("id"), now))
        # #269 — default-deny hygiene: a client unassigned from a project loses its section
        # grants NOW (no dormant rows that silently resurrect on a future re-assignment).
        # Gated on a CATALOG probe, not try/except: on Postgres a failed statement aborts
        # the whole transaction, so swallowing it would silently roll back the assignment
        # INSERTs above while still returning 200.
        if target["role"] == "client" and client_grants.table_ready(conn):
            if codes:
                conn.execute(
                    f"DELETE FROM client_section_grant WHERE user_id = ? AND project_code NOT IN "
                    f"({','.join(['?'] * len(codes))})", (user_id, *codes))
            else:
                conn.execute("DELETE FROM client_section_grant WHERE user_id = ?", (user_id,))
        conn.commit()
        logging.info(
            f"pm_scoping: set assignments user_id={user_id} n={len(codes)} "
            f"by={(actor or {}).get('id')}")
        return jsonify({"data": {"user_id": user_id, "project_codes": codes}})
    finally:
        conn.close()


@requires_company
def _set_project_status(project_code):
    """POST /api/projects/<code>/status — {status:'active'|'closed'} marks a project
    closed or reopens it. admin/c_suite ONLY (server-enforced; the before_request hook
    also runs first — company roles always pass it). A closed project drops off assigned
    PMs' active-projects view and they can no longer open it; admin/c_suite still see it."""
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip().lower()
    if status not in ("active", "closed"):
        return jsonify({"error": "status must be 'active' or 'closed'"}), 400
    conn = _db()
    try:
        if not _project_exists(conn, project_code):
            return jsonify({"error": "project not found"}), 404
        conn.execute(
            "UPDATE projects SET status = ? WHERE project_code = ?", (status, project_code))
        conn.commit()
    finally:
        conn.close()
    logging.info(
        f"pm_scoping: project {project_code} status -> {status} "
        f"by={(current_user() or {}).get('id')}")
    return jsonify({"data": {"project_code": project_code, "status": status}})


@requires_company
def _admin_projects_page():
    """GET /admin/projects — the admin/c_suite assignment + project-close screen."""
    if not ADMIN_PROJECTS_PAGE.exists():
        return jsonify({"error": "admin projects page not found"}), 404
    import ui_version                                            # #279
    resp = send_file(str(ui_version.resolve_page(ADMIN_PROJECTS_PAGE)))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def register(app) -> None:
    """Wire the project-scoping before_request hook + the assignment/lifecycle endpoints
    + the admin assignment page. Call AFTER auth.apply_auth_gate(app) so the login gate
    (which sets g.auth_user) runs first; this hook then enforces the project-ASSIGNMENT
    axis on top. Every assignment/lifecycle endpoint is admin/c_suite-only (server-side)."""
    app.before_request(_scoping_gate)
    app.add_url_rule("/admin/projects", "admin_projects_page",
                     _admin_projects_page, methods=["GET"])
    app.add_url_rule("/api/admin/pm-assignments", "pm_assignments_list",
                     _list_assignments, methods=["GET"])
    app.add_url_rule("/api/admin/pm-assignments", "pm_assignments_set",
                     _set_assignments, methods=["PUT"])
    app.add_url_rule("/api/projects/<project_code>/status", "project_set_status",
                     _set_project_status, methods=["POST"])
