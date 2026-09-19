"""#264 CLIENT portal + the client DEFAULT-DENY gate — #267 welcome hard-stop,
#269 selective per-section un-gating.

The most security-sensitive surface in the app. Posture (TWO default-deny layers, both
server-enforced — hiding UI is never the control):

  * SECTION GRANTS (#269, client_grants.py). Every portal section (progress / photos /
    documents / daily / schedule) starts OFF. A client with ZERO granted sections is
    HARD-CONTAINED on /welcome exactly as #267 built it: every other page redirects to
    /welcome, every API returns 403. Granting sections (admin/c_suite only) unlocks the
    portal ONE SECTION AT A TIME: the portal page renders ONLY granted sections, and every
    /api/portal/<section> endpoint re-checks its grant per request — a non-granted
    section's endpoint is 403 no matter how it's addressed.
  * PER-ITEM VISIBILITY (#264, visibility.py). Within photos AND documents (#269 extends
    the engine to documents), only individually client-shared, non-red-flagged items are
    served. A granted section shows ONLY its shared items.
  * PROJECT-SCOPED. A client is assigned to exactly ONE project (pm_project_assignment,
    generalized from #263). They see only that project, and only while it is active.
  * PER-RESOURCE ISOLATION (closes the #263 by-ID gap). Every by-ID fetch re-derives
    ownership from the row: a photo/document serves ONLY if it BELONGS to the client's
    project AND is shared to the `client` audience AND is not red-flagged (visibility.py).
    Guessing an id from another project or an unshared item yields 404 — never the bytes.
  * CURATED-ONLY PAYLOAD. The portal serializes id / caption / title / date / gated URLs
    only — never cost, labor, worker PII, file paths, uploader, or any internal field
    (CLAUDE.md).
  * READ-ONLY. The portal exposes no write endpoint.

Production stays on SQLite; all SQL routes through the caller's db_layer connection so it
runs identically on Postgres. Dates are LOCAL.
"""
from __future__ import annotations

import functools
import logging
from pathlib import Path

from flask import Response, g, jsonify, redirect, request, send_file

from auth import _db, _now_iso, current_user
import client_grants
import visibility
import dropplan_rollups as _rollups

SCRIPT_DIR = Path(__file__).resolve().parent
import ssc_paths  # #287
PORTAL_PAGE = SCRIPT_DIR / "client_portal.html"
WELCOME_PAGE = SCRIPT_DIR / "welcome.html"   # #267 — the client hard-stop
_FP_BASE = ssc_paths.under_root("data_room", "field_photos")   # #287
_DOC_BASE = ssc_paths.under_root("data_room", "project_docs")   # #287

# ============= CLIENT DEFAULT-DENY GATE — #267 contain / #269 grant-aware =============
# The ONLY paths a client may ALWAYS reach: the forced-reset page, the auth API, the two
# public no-data pings, and the vendored static assets the welcome/portal shells load.
# Everything else is grant-routed:
#   0 granted sections  -> #267 behavior byte-for-byte: /welcome only; every other PAGE
#                          redirects to /welcome, every API -> 403.
#   >=1 granted section -> the portal is the client's home: /welcome forwards to /portal,
#                          /portal + /api/portal/* become reachable (each API still
#                          enforces ITS OWN section grant — see require_section), and
#                          every OTHER page/API stays contained (redirect /portal | 403).
_CLIENT_ALLOW_PREFIXES = ("/api/auth/", "/files/static/", "/p/", "/api/p/", "/i/")   # #307 proposal + #326 invoice links
_CLIENT_ALLOW_EXACT = {"/set-password", "/api/health", "/api/today"}


def _client_always_allowed(path: str) -> bool:
    if path in _CLIENT_ALLOW_EXACT:
        return True
    return any(path.startswith(p) for p in _CLIENT_ALLOW_PREFIXES)


def _grant_ctx(user):
    """(project_code, granted_sections) for the current client — re-derived per request
    from the DB (a revoke takes effect on the very next request; no session state)."""
    conn = _db()
    try:
        code = client_project_code(user["id"], conn)
        return code, (client_grants.granted_sections(conn, user["id"], code) if code else set())
    finally:
        conn.close()


def _client_gate():
    """Registered AFTER the auth gate (g.auth_user set). Grant-aware containment (#269):
    a `client` with zero granted sections is hard-contained on /welcome (#267 unchanged);
    with >=1 grant their world is the portal — and ONLY the portal. Server-side, so no
    internal/admin/by-ID surface is reachable even by direct URL either way."""
    user = current_user()
    if not user or user.get("role") != "client":
        return None  # non-clients handled by their own gates
    path = request.path
    if _client_always_allowed(path):
        return None
    _code, granted = _grant_ctx(user)
    g.client_granted_sections = granted   # portal endpoints reuse (same request)
    if not granted:
        # ---- #267 hard-stop: zero grants -> welcome is the ONLY page ----
        if path == "/welcome":
            return None
        logging.info(f"client_portal: contain block (no grants) path={path}")
        if path.startswith("/api/"):
            return jsonify({"error": "forbidden"}), 403
        return redirect("/welcome")
    # ---- >=1 grant: the portal is home ----
    # #284 FLIP — the client's home is now the NEW SHELL at /portal/<code>. /welcome
    # forwards there, and the Classic PAGE (/portal exact) redirects there too; the
    # Classic ENGINE (its /api/portal/* endpoints, the by-id byte routes, the admin
    # preview of Classic) stays fully intact — the rollback is reverting this commit.
    if path == "/welcome" or path == "/portal":
        return redirect(f"/portal/{_code}")
    if path.startswith("/portal/") or path.startswith("/api/portal/"):
        return None                     # per-endpoint section grants enforced below
    # #286 — the portal widget grid saves per-user layouts through the SAME generic
    # endpoint the internal dashboards use. Safe to open to a granted client: the
    # endpoint is user-scoped (a session only ever reads/writes its own row),
    # page_key-allowlisted, and the layout body is structurally sanitized to
    # {id,x,y,w,h} — no project data flows either way. Zero-grant clients never
    # reach here (the hard-stop above already returned).
    if path == "/api/dashboard/layout":
        return None
    # #280/#283 — the drawing markup surface, now grant-wired (the #281 open decision,
    # decided): 'drawing' gates the page, the elevation APIs, and their comment threads;
    # 'rfis' gates RFI reads. Same #269 posture as every section — re-derived per request,
    # a revoke takes effect on the next request. Kept in the >=1-GRANT branch: a
    # zero-grant client is still hard-stopped on /welcome (#267 intact). Project scope is
    # enforced separately by elevation._require_project. The architect is NOT grant-gated
    # here (their own gate + project assignment scope them); the client stays READ-ONLY on
    # comments/RFIs at the endpoint (absent from elevation.COLLAB_WRITE_ROLES).
    # Comments ride the section that hosts them: drop/photo threads live on the drawing
    # (this gate); an RFI thread additionally requires the rfis grant, enforced in
    # elevation._api_comments_list where the target type is known.
    if path == "/drawing-markup" or path.startswith("/drawing-markup/") \
            or path.startswith("/api/elevation/") or path == "/api/elevations" \
            or path.startswith("/api/comments"):
        if "drawing" in granted:
            return None
        logging.info(f"client_portal: drawing grant missing path={path}")
        if path.startswith("/api/"):
            return jsonify({"error": "forbidden"}), 403
        return redirect("/portal")
    if path.startswith("/api/rfis"):
        if "rfis" in granted:
            return None
        logging.info(f"client_portal: rfis grant missing path={path}")
        return jsonify({"error": "forbidden"}), 403
    logging.info(f"client_portal: contain block (granted) path={path}")
    if path.startswith("/api/"):
        return jsonify({"error": "forbidden"}), 403
    return redirect(f"/portal/{_code}")   # #284 — containment lands on the new shell


# ============= #270 — EFFECTIVE CLIENT (self, or read-only admin preview) =============

def _resolve_portal_client(conn):
    """The client whose portal this request serves. Returns (target, is_preview, err):
      * role `client`      -> SELF. Any ?preview_client param is IGNORED — a client can
                              never preview anyone (nor widen their own view with it).
      * admin / c_suite    -> ONLY with a valid ?preview_client=<id> naming an ACTIVE
                              client: that client, read-only preview. Every grant check,
                              project scope, and per-item visibility downstream evaluates
                              against the TARGET — the preview can never show more (or
                              less) than the client's own login would. No param -> 403.
      * anyone else        -> 403 (pm/super/etc. can never reach the portal).
    Stateless by construction: no session swap, no impersonation cookie — the param is
    re-validated on EVERY request."""
    user = current_user()
    if not user:
        return None, False, (jsonify({"error": "forbidden"}), 403)
    role = user.get("role")
    if role == "client":
        return user, False, None
    if role in ("admin", "c_suite"):
        raw = request.args.get("preview_client")
        if not raw:
            return None, False, (jsonify({"error": "forbidden"}), 403)
        try:
            target_id = int(raw)
        except (TypeError, ValueError):
            return None, False, (jsonify({"error": "forbidden"}), 403)
        row = conn.execute(
            "SELECT id, role, status, is_active, display_name, full_name, email "
            "FROM users WHERE id=?", (target_id,)).fetchone()
        if (not row or row["role"] != "client" or row["status"] != "active"
                or not row["is_active"]):
            return None, False, (jsonify({"error": "forbidden"}), 403)
        target = {"id": row["id"], "role": "client",
                  "display_name": row["display_name"] or row["full_name"] or row["email"]}
        return target, True, None
    return None, False, (jsonify({"error": "forbidden"}), 403)


# ============= #269 — PER-SECTION ENFORCEMENT DECORATOR (preview-aware #270) =============

def require_section(section: str):
    """Endpoint gate for ONE portal section. Resolves the EFFECTIVE client (self, or the
    admin-preview target), then re-derives their project + the section grant from the DB
    per request and 403s when the section isn't granted — server-enforced, direct URL
    included, preview identical to the real client by construction. On success stashes
    g.portal_client_id / g.client_project_code / g.portal_is_preview for the handler."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            conn = _db()
            try:
                target, is_preview, err = _resolve_portal_client(conn)
                if err:
                    return err
                code = client_project_code(target["id"], conn)
                if not code or not client_grants.has_grant(conn, target["id"], code, section):
                    logging.info(
                        f"client_portal: section denied section={section} "
                        f"target={target.get('id')} preview={is_preview} path={request.path}")
                    return jsonify({"error": "forbidden"}), 403
            finally:
                conn.close()
            g.portal_client_id = target["id"]
            g.portal_is_preview = is_preview
            g.client_project_code = code
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ============= CLIENT PROJECT SCOPE =============

def client_project_code(user_id, conn):
    """The ONE active project a client is assigned to (pm_project_assignment, generalized).
    None if unassigned or the project is closed (closed drops off the client view, #263)."""
    row = conn.execute(
        "SELECT a.project_code FROM pm_project_assignment a "
        "JOIN projects p ON p.project_code = a.project_code "
        "WHERE a.user_id = ? AND (p.status = 'active' OR p.status IS NULL) "
        "ORDER BY a.project_code LIMIT 1",
        (user_id,)).fetchone()
    return row[0] if row else None


# ============= CURATED SERIALIZERS (no PII / paths / internal fields) =============

def _portal_photo(r) -> dict:
    """The ONLY photo fields a client receives. No worker_id / stage / file_name / size /
    mime / uploader / project_code / *_path — just what's needed to show the image."""
    return {
        "id": r["id"],
        "caption": r["caption"],
        "taken_at": (r["taken_at"] or "")[:10] or None,   # date only (LOCAL), no time
        "thumb_url": f"/api/portal/photos/{r['id']}/thumb",
        "file_url": f"/api/portal/photos/{r['id']}/file",
    }


def _progress_label(pct: float) -> str:
    if pct <= 0:
        return "Getting started"
    if pct >= 100:
        return "Complete"
    if pct < 35:
        return "Underway"
    if pct < 80:
        return "In progress"
    return "Nearing completion"


# ============= WELCOME / PENDING HARD-STOP (#267) =============

def _welcome_page():
    """The client welcome/pending page — the contained dead-end. Served to any authenticated
    user (the auth gate enforces login; the client gate routes clients here). No-store."""
    if not WELCOME_PAGE.exists():
        return ("welcome page missing", 500)
    import ui_version                                    # #279
    resp = send_file(str(ui_version.resolve_page(WELCOME_PAGE)))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ============= PORTAL ENDPOINTS (client-only, read-only, section-gated #269) =============

def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def _portal_page():
    """GET /portal — the portal shell. Serves the EFFECTIVE client's portal: a real
    client (zero grants -> /welcome), or a read-only admin/c_suite preview via
    ?preview_client=<id> (zero-grant target -> back to /admin/projects, where the
    containment is stated). Every preview page open writes an audit_log row (#270)."""
    conn = _db()
    try:
        target, is_preview, err = _resolve_portal_client(conn)
        if err:
            return err
        code = client_project_code(target["id"], conn)
        granted = client_grants.granted_sections(conn, target["id"], code) if code else set()
        if not granted:
            return redirect("/admin/projects" if is_preview else "/welcome")
        if is_preview:
            actor = current_user() or {}
            conn.execute(
                "INSERT INTO audit_log (action, actor_user_id, actor_role, target_type, "
                "target_id, note, created_at) VALUES (?,?,?,?,?,?,?)",
                ("client_portal_preview", actor.get("id"), actor.get("role"), "user",
                 str(target["id"]), "read-only client-portal preview opened", _now_iso()))
            conn.commit()
    finally:
        conn.close()
    if not PORTAL_PAGE.exists():
        return ("client portal page missing", 500)
    import ui_version                                    # #279
    return _no_store(send_file(str(ui_version.resolve_page(PORTAL_PAGE))))


def _portal_context():
    """GET /api/portal/context — the portal SHELL payload: curated project header + the
    effective client's granted sections (the ONLY sections the page renders). Requires
    >=1 grant. Carries NO section data itself — each section loads through its own
    grant-checked endpoint. In preview mode a `preview` block rides OUTSIDE `data`, so
    `data` stays byte-identical to the real client's own response (parity-guarded)."""
    conn = _db()
    try:
        target, is_preview, err = _resolve_portal_client(conn)
        if err:
            return err
        code = client_project_code(target["id"], conn)
        granted = client_grants.granted_sections(conn, target["id"], code) if code else set()
        if not code or not granted:
            return jsonify({"error": "forbidden"}), 403
        p = conn.execute(
            "SELECT project_code, name, address, city_zip, status FROM projects WHERE project_code = ?",
            (code,)).fetchone()
        if not p:
            return jsonify({"error": "forbidden"}), 403
        payload = {"data": {
            "project": {
                "code": p["project_code"], "name": p["name"] or p["project_code"],
                "address": ", ".join(x for x in (p["address"], p["city_zip"]) if x) or None,
                "status": (p["status"] or "active"),
            },
            # portal display order, granted only
            "sections": [s for s in client_grants.SECTIONS if s in granted],
        }}
        if is_preview:
            # OUTSIDE data (parity): who the preview shows, for the amber banner only.
            payload["preview"] = {"on": True, "client_name": target.get("display_name")}
        return _no_store(jsonify(payload))
    finally:
        conn.close()


@require_section("progress")
def _portal_project():
    """Curated high-level progress + a curated summary (the 'progress' SECTION). NO money,
    NO drop internals — overall % only (project_rollup with include_cost=False). Photo
    counts are mentioned ONLY when the photos section is also granted."""
    code = g.client_project_code
    conn = _db()
    try:
        roll = _rollups.project_rollup(conn, code, include_cost=False)  # include_cost=False => NO $ fields
        pct = roll.get("overall_progress_pct", 0.0) or 0.0
        label = _progress_label(pct)
        summary = f"Your project is {label.lower()} — about {pct:.0f}% complete."
        last_activity = None
        photos_shared = None
        if client_grants.has_grant(conn, g.portal_client_id, code, "photos"):
            vis_ids = visibility.client_visible_photo_ids(conn, code)
            photos_shared = len(vis_ids)
            if vis_ids:
                row = conn.execute(
                    f"SELECT MAX(substr(taken_at,1,10)) FROM field_photos "
                    f"WHERE id IN ({','.join(['?']*len(vis_ids))})", vis_ids).fetchone()
                last_activity = row[0] if row else None
                summary += f" {photos_shared} photo{'s' if photos_shared != 1 else ''} shared with you."
        payload = {"progress": {"pct": round(pct, 0), "label": label},
                   "summary": {"text": summary, "last_activity": last_activity}}
        if photos_shared is not None:
            payload["summary"]["photos_shared"] = photos_shared
        # financials intentionally omitted (omitted, not zeroed — CLAUDE.md comp governance)
        return _no_store(jsonify({"data": payload}))
    finally:
        conn.close()


@require_section("photos")
def _portal_photos():
    """The client gallery — ONLY photos shared to the client audience for the client's
    project, never red-flagged. Default-deny: an unshared photo is simply absent."""
    code = g.client_project_code
    conn = _db()
    try:
        ids = visibility.client_visible_photo_ids(conn, code)
        if not ids:
            return _no_store(jsonify({"data": {"photos": []}}))
        rows = conn.execute(
            f"SELECT id, caption, taken_at FROM field_photos WHERE id IN ({','.join(['?']*len(ids))}) "
            f"ORDER BY taken_at DESC, id DESC", ids).fetchall()
        return _no_store(jsonify({"data": {"photos": [_portal_photo(r) for r in rows]}}))
    finally:
        conn.close()


def _portal_serve(photo_id, col):
    """Serve photo bytes to a client BY ID — only after re-deriving ownership + visibility
    from the row (per-resource isolation). Any other-project / unshared / flagged / unknown
    id -> 404 (never reveals existence). `col` is 'file_path' or 'thumb_path'."""
    code = g.client_project_code
    conn = _db()
    try:
        if not visibility.photo_visible_to_client(conn, photo_id, code):
            return jsonify({"error": "not found"}), 404
        r = conn.execute(
            f"SELECT {col} AS p, mime FROM field_photos WHERE id = ?", (photo_id,)).fetchone()
    finally:
        conn.close()
    if not r or not r["p"]:
        return jsonify({"error": "not found"}), 404
    p = ssc_paths.resolve_data_path(r["p"])   # #287
    try:
        if not p.resolve().is_relative_to(_FP_BASE.resolve()) or not p.exists():
            return jsonify({"error": "not found"}), 404
    except (OSError, ValueError):
        return jsonify({"error": "not found"}), 404
    return send_file(str(p), mimetype=(r["mime"] or "image/jpeg"), conditional=True)


@require_section("photos")
def _portal_photo_thumb(photo_id):
    return _portal_serve(photo_id, "thumb_path")


@require_section("photos")
def _portal_photo_file(photo_id):
    return _portal_serve(photo_id, "file_path")


# ---- #269 — documents section (per-item visibility, same engine as photos) ----

def _portal_document(r) -> dict:
    """The ONLY document fields a client receives. No file_path / file_name / file_size /
    mime / uploader / notes / requirement_key — just what's needed to list + open it."""
    return {
        "id": r["id"],
        "title": r["title"],
        "category": r["category"],
        "doc_type": r["doc_type"],
        "effective_date": r["effective_date"],
        "file_url": f"/api/portal/documents/{r['id']}/file",
    }


@require_section("documents")
def _portal_documents():
    """The client document list — ONLY documents shared to the client audience for the
    client's project (item_type='document'), never red-flagged, never superseded."""
    code = g.client_project_code
    conn = _db()
    try:
        ids = visibility.client_visible_document_ids(conn, code)
        if not ids:
            return _no_store(jsonify({"data": {"documents": []}}))
        rows = conn.execute(
            f"SELECT id, title, category, doc_type, effective_date FROM project_documents "
            f"WHERE id IN ({','.join(['?']*len(ids))}) ORDER BY uploaded_at DESC, id DESC",
            ids).fetchall()
        return _no_store(jsonify({"data": {"documents": [_portal_document(r) for r in rows]}}))
    finally:
        conn.close()


@require_section("documents")
def _portal_document_file(doc_id):
    """Serve document bytes BY ID — per-resource isolation identical to photos: belongs to
    the client's project AND client-shared AND not red-flagged/superseded, else 404."""
    code = g.client_project_code
    conn = _db()
    try:
        if not visibility.document_visible_to_client(conn, doc_id, code):
            return jsonify({"error": "not found"}), 404
        r = conn.execute(
            "SELECT file_path, mime FROM project_documents WHERE id = ?", (doc_id,)).fetchone()
    finally:
        conn.close()
    if not r or not r["file_path"]:
        return jsonify({"error": "not found"}), 404
    p = ssc_paths.resolve_data_path(r["file_path"])   # #287
    try:
        if not p.resolve().is_relative_to(_DOC_BASE.resolve()) or not p.exists():
            return jsonify({"error": "not found"}), 404
    except (OSError, ValueError):
        return jsonify({"error": "not found"}), 404
    resp = send_file(str(p), mimetype=(r["mime"] or "application/octet-stream"), conditional=True)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---- #269 — daily section (curated daily-report timeline; no internals) ----

@require_section("daily")
def _portal_daily():
    """Curated daily-report timeline for the client's project: date + work/no-work label
    per issued report day. NO labor, NO worker identities, NO report internals — the full
    client-version DCR render stays internal until a deliberate future share build."""
    code = g.client_project_code
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT report_date, MAX(COALESCE(no_work,0)) AS no_work "
            "FROM report_index WHERE project_code=? AND status='issued' "
            "GROUP BY report_date ORDER BY report_date DESC LIMIT 60",
            (code,)).fetchall()
        days = [{"date": r["report_date"],
                 "no_work": bool(r["no_work"]),
                 "label": ("No work performed" if r["no_work"] else "Crew on site — work performed")}
                for r in rows]
        return _no_store(jsonify({"data": {"days": days}}))
    finally:
        conn.close()


# ---- #269 — schedule section (curated look-ahead; no crew/notes/internals) ----

@require_section("schedule")
def _portal_schedule():
    """Curated look-ahead for the client's project: activity name/type + planned window,
    recent past through the near future. NO crew, NO notes, NO drop internals."""
    from datetime import date, timedelta
    code = g.client_project_code
    today = date.today()                       # LOCAL (CLAUDE.md dates rule)
    lo = (today - timedelta(days=7)).isoformat()
    hi = (today + timedelta(days=28)).isoformat()
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT name, activity_type, planned_start, planned_finish FROM lookahead_activity "
            "WHERE project_code=? AND planned_finish >= ? AND planned_start <= ? "
            "ORDER BY planned_start, planned_finish, id LIMIT 120",
            (code, lo, hi)).fetchall()
        acts = [{"name": r["name"], "type": r["activity_type"],
                 "start": r["planned_start"], "finish": r["planned_finish"]} for r in rows]
        return _no_store(jsonify({"data": {"activities": acts, "window": {"start": lo, "end": hi}}}))
    finally:
        conn.close()


def register(app) -> None:
    """Wire the grant-aware client gate + the read-only, section-gated portal page/API.
    Call AFTER apply_auth_gate (and the other gates) so g.auth_user is set; the gate then
    enforces grant-aware default-deny for the client role, and every portal endpoint
    resolves the EFFECTIVE client (self, or admin/c_suite read-only preview #270) +
    section grant + project scope + per-resource visibility, all per request."""
    app.before_request(_client_gate)
    app.add_url_rule("/welcome", "client_welcome_page", _welcome_page, methods=["GET"])  # #267 hard-stop
    app.add_url_rule("/portal", "client_portal_page", _portal_page, methods=["GET"])
    app.add_url_rule("/api/portal/context", "client_portal_context", _portal_context, methods=["GET"])
    app.add_url_rule("/api/portal/project", "client_portal_project", _portal_project, methods=["GET"])
    app.add_url_rule("/api/portal/photos", "client_portal_photos", _portal_photos, methods=["GET"])
    app.add_url_rule("/api/portal/photos/<int:photo_id>/thumb", "client_portal_thumb",
                     _portal_photo_thumb, methods=["GET"])
    app.add_url_rule("/api/portal/photos/<int:photo_id>/file", "client_portal_file",
                     _portal_photo_file, methods=["GET"])
    app.add_url_rule("/api/portal/documents", "client_portal_documents", _portal_documents, methods=["GET"])
    app.add_url_rule("/api/portal/documents/<int:doc_id>/file", "client_portal_document_file",
                     _portal_document_file, methods=["GET"])
    app.add_url_rule("/api/portal/daily", "client_portal_daily", _portal_daily, methods=["GET"])
    app.add_url_rule("/api/portal/schedule", "client_portal_schedule", _portal_schedule, methods=["GET"])
