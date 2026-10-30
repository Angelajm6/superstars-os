# #290 (Cloud M4) — SSC_TZ enforcement MUST be the first app code that runs:
# every import below may derive dates at module level, and all of them must
# already see the enforced zone (UTC cloud host -> Eastern). No-op when unset.
import ssc_tz
ssc_tz.enforce()

import flask
from flask import Flask, jsonify, request, send_from_directory, send_file, Response, redirect
import sqlite3
from pathlib import Path
from datetime import datetime, date, timedelta
import logging
import math
import json
import os

import runtime_seams
import re
import time
import uuid
import hashlib
import secrets

# #273 DEV-ONLY isolation marker — must sit ABOVE the db_layer import and every
# #289-pattern boot ensure below, because those connect at IMPORT time and read
# SSC_DB_URL then. When `python server.py` (this __main__ dev path — production
# waitress imports server:app and never triggers it) starts with no SSC_DB_URL,
# a gitignored `.dev_db_url` file redirects the dev server to an ISOLATED DB
# copy so browser preview sessions can never touch the live superstars.db
# (CLAUDE.md isolation rule). Loud banner when active; delete the file to point
# a manual dev run back at live.
if __name__ == '__main__':
    _dev_marker = Path(__file__).resolve().parent / '.dev_db_url'
    if not os.environ.get('SSC_DB_URL') and _dev_marker.exists():
        _dev_url = _dev_marker.read_text(encoding='utf-8').strip()
        if _dev_url:
            os.environ['SSC_DB_URL'] = _dev_url
            print("\n  *** DEV DB OVERRIDE (.dev_db_url) — ISOLATED COPY, NOT LIVE ***", flush=True)

# #259 — env-driven DB layer (SSC_DB_URL): SQLite default (unchanged) or Postgres.
import db_layer

# Vision-based cert extraction (requires ANTHROPIC_API_KEY in env — launch
# the server via `op run --env-file=".env.template" -- python server.py`).
from cert_extractor import extract_cert_from_image, load_cert_types_from_db

SCRIPT_DIR = Path(__file__).resolve().parent
import ssc_paths  # #287 — THE data-path resolver (SSC_DATA_ROOT)
DB_PATH = ssc_paths.sqlite_db_path()   # #287 — data-root aware (unset = same file)
COMPANY_DASHBOARD_PATH = SCRIPT_DIR / "company-dashboard.html"
DASHBOARD_PATH = SCRIPT_DIR / "dashboard-static.html"
PORTAL_SHELL_PATH = SCRIPT_DIR / "portal_shell.html"   # #284 — the external shell
PM_PROJECTS_PATH = SCRIPT_DIR / "projects.html"  # #263 — pm projects-only landing

# #248 — PUBLIC static serving is the vendored-asset subtree ONLY. The
# pre-#248 mount was the ENTIRE project dir at /files, which made the DB,
# source, docs, and daily DB snapshots downloadable with no login. Keeping
# the /files/static/... URL shape means zero changes for every page that
# references shell assets (css/js/fonts/vendor).
app = Flask(__name__, static_folder=str(SCRIPT_DIR / 'static'),
            static_url_path='/files/static')

# #297 — The portal, worker PWA, and client portal are all same-origin.  Do not
# advertise the authenticated API to arbitrary websites through a wildcard CORS
# policy; integrations call server-to-server and do not require browser CORS.
import http_security
http_security.apply_http_security_headers(app)

# Browser-preview blueprint: /preview/* URLs for HTML-first design iteration.
# WeasyPrint stays in render_pdf.py for production export only.
from preview_routes import preview_bp
app.register_blueprint(preview_bp)

# Dashboard auth foundation (#48): wires /login, /api/auth/* and a
# before_request gate that redirects unauthenticated requests to /login
# (HTML) / returns 401 (JSON). The gate exempts /api/worker/*, /worker-app
# (+ its PWA manifest/service-worker shell files), /api/health, /api/today,
# and /files/static/* (vendored assets ONLY — #248) — worker-app PIN sign-in
# is untouched. /preview/* lost its exemption in #248: it serves rendered
# project documents (internal DCRs etc.), so it requires a session like any
# other operator surface. Per-route role gating uses @requires_role from auth.py.
from auth import (apply_auth_gate, requires_role, requires_section,  # noqa: F401
                  requires_company, current_user)
import access  # #262 — central section→roles map (RBAC single source of truth)
import crm  # #266 — CRM/ops core logic (shared by the endpoints + the guard smoke)
apply_auth_gate(app)
import password_reset  # noqa: E402
password_reset.register(app)

# #302 — Company Portal (C-suite whole-company view). MUST follow apply_auth_gate:
# every route is behind requires_section('company_portal') = admin/c_suite.
import company_portal  # noqa: E402
company_portal.register(app)

# #304 — buildings as a first-class entity (table + backfill at boot; CRM-axis endpoints).
import buildings  # noqa: E402
buildings.register(app)

# #307 — proposals generated from estimates, sent with a signed link, accepted online.
# Internal endpoints are requires_section('estimates'); /p/<token> + /api/p/<token>/* are
# public by token (auth._PUBLIC_PREFIXES).
import proposals  # noqa: E402
proposals.register(app)
import invoices  # noqa: E402
invoices.register(app)
import invoice_docs  # noqa: E402
invoice_docs.register(app)   # #326 — invoice documents, email, public link, project Payments
import qbo  # noqa: E402
qbo.register(app)            # #316 — QuickBooks Online: invoices out, payments back (off until configured)
import doc_packages  # noqa: E402
doc_packages.register(app)   # #327 — permits & insurance package, send to client
import aia_billing
aia_billing.register(app)    # #317 — AIA G702/G703 progress billing on the invoice model
import payables
payables.register(app)       # #319 — accounts payable + the 30-day cash view
import fleet
fleet.register(app)          # #355 — vehicles: mileage, service, E-ZPass tolls, car bills
import portal_projects
portal_projects.register(app)   # #330 — Company Portal Projects tab = jobs; a job's own page
import portal_calendar
portal_calendar.register(app)   # #332 — the jobs calendar (3-day / week / month)
import building_3d
building_3d.register(app)       # #336 — the building in 3D (footprint + height from NYC Open Data, grids on the walls)
import dcr_maintenance
dcr_maintenance.register(app)   # #344 — DCR register + the pinned 109→097 correction
import worker_lifecycle
worker_lifecycle.register(app)   # #345 — Workforce: archive / reactivate / delete permanently
import hours_overtime
hours_overtime.register(app)   # hours over 8: approve / reject + the week picker summary
import worker_id_cleanup
worker_id_cleanup.register(app)  # #346 — one-time W-0030..32 -> W-0027..29 renumber (admin)

# #279 (UI v2 phase 0) — the interface toggle. MUST follow apply_auth_gate: the
# stored per-user preference is read off current_user(). Sets g.ui_version on page
# routes; every UI route serves its page through ui_version.serve_ui, which returns
# the v2 twin when one exists and the untouched v1 file when it does not.
import ui_version  # noqa: E402
ui_version.apply(app)
ui_version.register(app)

# Admin account management — multi-user accounts & roles, Phase 1 (#257). Every
# endpoint is admin-only, gated server-side (the before_request gate runs first).
import auth_admin  # noqa: E402
auth_admin.register(app)

import auth_google  # noqa: E402  # #261 — Google OIDC SSO (feature-flagged; 404 when unconfigured)
auth_google.register(app)

# #289 (Cloud M3) — public-door hardening: staff TOTP + force_sso + the missing-2FA
# banner, and worker-app device provisioning/revocation. MUST follow apply_auth_gate.
# The schema is ensured at boot (idempotent) so a fresh deploy needs no manual step.
import apply_public_hardening_289 as _hardening_schema
try:
    _hc = db_layer.connect(pragma_fk=True)
    try:
        _hardening_schema.ensure_hardening_schema(_hc)
        _hc.commit()
    finally:
        _hc.close()
    # #290 hotfix — the ensure above may have CREATED tables on a fresh DB
    # after the first connect populated the per-process pk-map cache; drop the
    # cache so those tables get transparent lastrowid too.
    db_layer._PKMAP_CACHE.clear()
except Exception as _e:
    logging.warning(f"#289 hardening schema ensure skipped: {_e}")
# #305 — the canonical roster views (CLAUDE.md roster rule) were missing on every
# fixture-built database (preprod): the migration that creates them was never in the
# fixture lists. Ensure them at boot, idempotently, on both backends.
def _ensure_roster_views(conn):
    pg = db_layer.is_postgres()
    head = "CREATE OR REPLACE VIEW" if pg else "CREATE VIEW IF NOT EXISTS"
    conn.execute(f"{head} v_worker_roster AS "
                 "SELECT e.*, COALESCE(ls.status, 'active') AS labor_status "
                 "FROM employees e LEFT JOIN labor_worker_state ls ON ls.worker_id = e.worker_id")
    conn.execute(f"{head} v_active_workers AS "
                 "SELECT * FROM v_worker_roster WHERE archived_at IS NULL AND labor_status = 'active'")
try:
    _rv = db_layer.connect(pragma_fk=True)
    try:
        _ensure_roster_views(_rv)
        _rv.commit()
    finally:
        _rv.close()
except Exception as _e:
    logging.warning(f"#305 roster views ensure skipped: {_e}")

# #306 — migration ledger: ensure the table, baseline an existing database, report what
# is pending (and apply it when SSC_AUTO_MIGRATE=1 — preprod). Never blocks boot.
import migrations  # noqa: E402
_MIGRATION_BOOT = migrations.boot(auto=os.environ.get('SSC_AUTO_MIGRATE') == '1')

import auth_hardening
auth_hardening.register(app)

# #305 — containment for the roles that had none (estimator, vendor). After apply_auth_gate.
import role_containment  # noqa: E402
role_containment.register(app)

# #296 — worker PWA bearer sessions.  The PWA routes are public so the session
# record, rather than a client supplied employee_id, authorizes clock actions.
import apply_worker_session_security_296 as _worker_session_schema
try:
    _wsc = db_layer.connect(pragma_fk=True)
    try:
        _worker_session_schema.ensure_worker_session_security(_wsc)
        _wsc.commit()
    finally:
        _wsc.close()
    db_layer._PKMAP_CACHE.clear()
except Exception as _e:
    logging.warning(f"#296 worker session schema ensure skipped: {type(_e).__name__}")

# #263 — PM project-scoping: registers the project-ASSIGNMENT before_request hook
# (rejects any /projects/<code> the user can't access) + the admin/c_suite-only
# assignment + project-close endpoints. MUST follow apply_auth_gate so g.auth_user
# is set before the scoping hook runs.
import pm_scoping  # noqa: E402
pm_scoping.register(app)
import project_onboard  # noqa: E402
project_onboard.register(app)   # #324 — onboard an already-won job
import billing_type  # noqa: E402
billing_type.register(app)      # #356 — AIA / lump sum / T&M / unit price per job
import project_modules  # noqa: E402
import job_v2  # noqa: E402
project_modules.register(app)    # per-job dashboard modules (which optional tabs a job shows)
import project_contacts  # noqa: E402
project_contacts.register(app)  # #357 — owners, property managers, consultants, site supervisors
import project_summary  # noqa: E402
project_summary.register(app)   # #358 — the job dashboard summary (bootstrap part + /summary)
import project_details  # noqa: E402
project_details.register(app)   # edit a job's name, address, client, dates, contract value
import project_delete  # noqa: E402
project_delete.register(app)    # delete a job that has nothing filed under it yet
import team_items  # noqa: E402
team_items.register(app)   # team tasks & events: the dashboard to-do list, synced onto the calendar
import gcal_sync  # noqa: E402
gcal_sync.register(app)   # each person's tasks & events in their own Google Calendar (off until configured)
import gmail_suggest  # noqa: E402
gmail_suggest.register(app)   # to-dos spotted in each person's Gmail, one tap from their to-do (off until configured)
import ui_usage  # noqa: E402
ui_usage.register(app)   # job page redesign step 1: which views get opened (counts only) + each job's tool footprint
import account  # noqa: E402
account.register(app)    # Settings > Account: each person's own name, password, devices
import email_change  # noqa: E402
import field_videos  # noqa: E402
try:   # worker videos (field_videos.py): field_photos.duration_s, added once at boot
    _fv_conn = db_layer.connect()
    try:
        field_videos.ensure(_fv_conn)
    finally:
        _fv_conn.close()
except Exception as _fv_err:   # never block boot (a fixture that migrates later)
    logging.warning("field videos ensure skipped: %s", type(_fv_err).__name__)
email_change.register(app)   # change a sign-in email, confirmed from the new address
import assistant  # noqa: E402
assistant.register(app)   # Claude on estimates, bids and proposals: saved conversations, daily cap (off until the key is set)
import claude_agent  # noqa: E402
claude_agent.register(app)   # Claude across the platform: lookups run, every change waits for a person's approval
import claude_docs  # noqa: E402
claude_docs.register(app)   # documents Claude makes: open beside the chat, edit, PDF / Word, save to a job
import claude_connector  # noqa: E402
claude_connector.register(app)   # your own claude.ai, connected (MCP + OAuth; off unless SSC_CLAUDE_CONNECTOR=1)
import inbox  # noqa: E402
inbox.register(app)   # admin / C-suite email live in the software: matched, sorted, Claude's cards (approval for every change and send)
import calendar_import  # noqa: E402
calendar_import.register(app)   # each person's main Google Calendar, copied privately for them (only they see it)
import event_reminders  # noqa: E402
event_reminders.register(app)   # emails 1 day + 1 hour before every meeting, event, timed task and walkthrough
import inbox_schedule  # noqa: E402
inbox_schedule.register(app)   # Inbox Mine (everyone) / All (admin + C-suite, server-checked): calendar + assigned work
import today_feed  # noqa: E402
today_feed.register(app)   # the Dashboard's Today: four cards, Your day, Needs you (live, each card opens its page)
import proposal_template  # noqa: E402
proposal_template.register(app)   # the company's own proposal layout, read once by Claude; every proposal PDF uses it
import company_brand  # noqa: E402
company_brand.register(app)   # Settings > Company brand (admin): the logo + letterhead on every document
import proposal_studio  # noqa: E402
proposal_studio.register(app)   # Proposal Studio: ask Claude for a proposal, edit anything, send the PDF from Gmail
import project_setup  # noqa: E402
project_setup.register(app)   # Oct 7 notes batch C: job numbers, a color per job, access method, worker access

# #264 — the per-item VISIBILITY ENGINE (default-deny) + the read-only CLIENT portal.
# client_portal registers a default-deny before_request gate (a `client` reaches ONLY the
# portal + auth; every internal endpoint / by-ID resource -> 403) and the curated portal
# API. MUST follow apply_auth_gate (g.auth_user set) and the #263 hook.
import visibility  # noqa: E402,F401  # visibility engine (share/redflag, per-resource checks)
import client_portal  # noqa: E402
client_portal.register(app)

# #269 — SELECTIVE CLIENT UN-GATING: per-client, per-section, DEFAULT-OFF access grants
# (client_section_grant). The client gate above consumes the grants to route (0 grants ->
# the #267 /welcome hard-stop; >=1 -> the portal, granted sections only); this registers
# the admin/c_suite-only grant/revoke/list endpoints.
import client_grants  # noqa: E402
client_grants.register(app)

# #284 — the ROLE x SECTION matrix (portal_matrix) + the /api/portal/<code>/* section
# payloads (portal_sections): the portal shell's data, every field through the
# client_payload() registry, matrix + grant + project-binding re-derived per request.
# MUST follow client_portal.register (the client gate routes /api/portal/* first).
import portal_matrix  # noqa: E402
import portal_sections  # noqa: E402
portal_sections.register(app)

# #360 — the Client portal tab: one read of who has portal access and how it is set up
# (login state, linked project, unlocked sections). Writes stay on their own endpoints.
import portal_access  # noqa: E402
portal_access.register(app)

# #272a — Materials & Deliveries: per-project catalog + txn ledger + expected
# deliveries + weekly count. Operational section (all dashboard roles; pm via the
# central scoping hook on /api/projects/<code>/ paths + per-resource checks on by-id
# routes). Units reuse the expense taxonomy enum (server.EXPENSE_UNITS, read lazily).
import materials  # noqa: E402
materials.register(app)

# #273 — Estimate/bid tracking on the company console: the general estimate log
# ({TYPE}-{BORO}-{NNN} series), attachments (gated by-id serving), server-validated
# status pipeline + one-click convert-to-project, CRM-linked activity ('sales').
# Every endpoint @requires_section('estimates') = admin/c_suite (access.py, one source).
import estimates  # noqa: E402
estimates.register(app)

# #274 — IRA inspection pipeline + calendar, same 'estimates' section/gating:
# checklist rail (CD-5 tracking, COI w/ #271-style expiry pill, contract, Fieldwire
# link, report + payment strip), multi-visit calendar, waiting-on digest.
import ira  # noqa: E402
ira.register(app)

# #276 — the estimating-division core: /estimating workspace (estimator/admin/
# c_suite via SECTION_ACCESS['estimating']), the estimating sub-stage machine, the
# VP's table + SLA aging ('estimates' section), internal notifications (stub/record;
# live SendGrid only when SENDGRID_API_KEY is present).
import estimating  # noqa: E402
estimating.register(app)

# #277 — walkthrough scheduling (stage-synced, attendee-required) + iPad
# walkthrough reports (GPS-stripped photos on the ESTIMATE) + the merged company
# schedule (inspections + walkthroughs on one console calendar).
import walkthroughs  # noqa: E402
walkthroughs.register(app)

# #280 — the drawing markup page (north elevation, per-drop per-floor work status) +
# the architect containment gate. Separate from /dropplan (#201/#256), which is the
# per-drop LIFECYCLE schedule and is untouched. MUST follow apply_auth_gate, the #263
# scoping hook and the client gate: the architect gate is a before_request that assumes
# g.auth_user is already set.
import elevation  # noqa: E402
elevation.register(app)

# #293 — AUTHORABLE elevations: drawing-set upload + sheet picker + AI-proposed
# trace + manual authoring + the draft/confirm state machine. Schema ensured at
# boot (idempotent, dual-backend) per the #289 pattern — a fresh deploy needs no
# manual migration step, and the 890 North row backfills to face_label='North' /
# status='confirmed' on first boot. Routes carry their OWN internal-role checks:
# the client gate and architect allowlist never open /drawing-author or
# /api/drawing-sets, and must not.
import apply_drawing_author_293 as _author_schema  # noqa: E402
try:
    _ac = db_layer.connect(pragma_fk=True)
    try:
        _author_schema.ensure_all(_ac)
        _ac.commit()
    finally:
        _ac.close()
    db_layer._PKMAP_CACHE.clear()   # same #290 fresh-DB pk-map note as #289 above
except Exception as _e:
    logging.warning(f"#293 drawing-author schema ensure skipped: {_e}")
import drawing_sets  # noqa: E402
drawing_sets.register(app)

# #294 — field photo reassignment history + edit tracking. AMEND-NEVER-ERASE on
# the one assignment door (/api/field-photos/assign hooks record_reassignments
# in-transaction); admin/c_suite pattern view + threshold alerts read the trail.
# Schema ensured at boot (#289 pattern) — the threshold seed + report_index
# photo_amended columns land on first boot with no manual step.
import apply_photo_reassign_294 as _reassign_schema  # noqa: E402
try:
    _rc = db_layer.connect(pragma_fk=True)
    try:
        _reassign_schema.ensure_reassign_schema(_rc)
        _rc.commit()
    finally:
        _rc.close()
    db_layer._PKMAP_CACHE.clear()   # #290 fresh-DB pk-map note, as above
except Exception as _e:
    logging.warning(f"#294 photo-reassign schema ensure skipped: {_e}")
import photo_reassign  # noqa: E402
photo_reassign.register(app)

# #295 — sign-in hours edit history + the DCR hours-amended flag. AMEND-NEVER-
# ERASE on the EXISTING sign-in doors (PUT/PATCH/DELETE on /api/sign-ins/<id>);
# corrections write sign_in_edit rows in-transaction, an issued day gets
# hours_amended(+at) + an audit row + an AUTO re-render of both audiences.
import apply_hours_edit_295 as _hours_edit_schema  # noqa: E402
try:
    _hc = db_layer.connect(pragma_fk=True)
    try:
        _hours_edit_schema.ensure_hours_edit_schema(_hc)
        _hc.commit()
    finally:
        _hc.close()
    db_layer._PKMAP_CACHE.clear()   # #290 fresh-DB pk-map note, as above
except Exception as _e:
    logging.warning(f"#295 hours-edit schema ensure skipped: {_e}")

# Hours over 8 in a day: reason + admin/PM approval (payroll_hours.DAY_LIMIT).
import apply_hours_overtime as _hours_overtime_schema  # noqa: E402
try:
    _hc = db_layer.connect(pragma_fk=True)
    try:
        _hours_overtime_schema.ensure_hours_overtime_schema(_hc)
        _hc.commit()
    finally:
        _hc.close()
    db_layer._PKMAP_CACHE.clear()
except Exception as _e:
    logging.warning(f"hours-overtime schema ensure skipped: {_e}")

# #278 — Project Cost "Spent to Date" (C-Suite) + the project expense ledger.
# Comp data: every endpoint admin/c_suite; cost keys OMITTED for every other role
# (403, never zeroed payloads); company console only — no field-reachable surface.
import project_costs  # noqa: E402
project_costs.register(app)

# Security: cap upload size. Raised to 256 MB (#235) so a field-photo BATCH POST
# (many images, several 8-12 MB) isn't rejected at the WSGI layer; the Field
# Photos UI also uploads in chunks. A request over the cap returns a clean 413
# JSON (see the 413 handler in the Field Photos section), never a 500.
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024 * 1024

# Whitelist of allowed file types for worker document uploads
ALLOWED_DOC_MIME_TYPES = {
    'image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp',
    'image/heic', 'image/heif',           # iPhone defaults
    'application/pdf',
}
ALLOWED_DOC_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif', '.pdf'}

# ============= #248 — FILE-SERVING ALLOWLISTS (by construction) =============
# Generated-artifact subtrees reachable through the gated /project-files/
# route. Everything else under data_room/ is deliberately ABSENT: db_backups
# and server_logs are never servable; receipts, project_docs, and
# field_photos already have their own gated id-based routes with their own
# role rules (listing them here would loosen those).
# #287 — evaluated per call (env decides the base): see _artifact_roots().
_ARTIFACT_SUBTREES = ("reports", "photos", "forms", "toolbox_talks",
                      "signage", "credentials")


def _artifact_roots():
    return tuple(ssc_paths.under_root("data_room", t) for t in _ARTIFACT_SUBTREES)

# Extensions an artifact may carry. Code / config / db / log / csv are absent
# BY DESIGN — a future artifact type gets its extension added here
# consciously, never inherited by accident.
_ARTIFACT_EXTENSIONS = {
    '.html', '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp',
    '.heic', '.heif', '.svg',
}

# Legacy root output dirs reachable through the authed root catch-all
# (pre-data_room era generators still write here). Top-level dirs NOT listed
# (tests, venv, worker_records, intake, data_room, static, ...) are
# unreachable through that route by construction.
_ROOT_SERVE_DIRS = {
    'meetings', 'drop_plans', 'site_closures', 'toolbox_talks',
    'meeting_workflow_run', 'rfi_workflow_run',
}


def _split_safe_rel(rel):
    """Normalize a URL-supplied relative path into segments, or None if it
    smells like traversal: absolute paths, drive letters/ADS colons,
    backslashes are normalized first, then any '..' or dot-leading segment
    is rejected outright."""
    if not rel:
        return None
    raw = str(rel).replace('\\', '/')
    if raw.startswith('/') or ':' in raw:
        return None
    parts = [p for p in raw.split('/') if p not in ('', '.')]
    if not parts or any(p == '..' or p.startswith('.') for p in parts):
        return None
    return parts


def _safe_artifact_path(rel):
    """Resolve rel (project-relative) and return it ONLY if it is a real file
    inside one of _ARTIFACT_ROOTS with an allowlisted extension. Traversal is
    rejected before joining; containment is re-asserted on the resolved path
    (belt and suspenders)."""
    parts = _split_safe_rel(rel)
    if parts is None:
        return None
    candidate = ssc_paths.data_root().joinpath(*parts)   # #287
    if candidate.suffix.lower() not in _ARTIFACT_EXTENSIONS:
        return None
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not any(resolved.is_relative_to(root.resolve()) for root in _artifact_roots()):
        return None
    if not resolved.is_file():
        return None
    return resolved


def _file_url_for(rel_path):
    """URL scheme for a project-relative file (#248): vendored static/ assets
    stay on the public /files/static mount; everything else is a generated
    artifact served through the gated /project-files/ route."""
    rel = str(rel_path).replace('\\', '/').lstrip('/')
    return ('/files/' + rel) if rel.startswith('static/') else ('/project-files/' + rel)


# ============= DASHBOARD ROUTES =============

@app.route('/')
def index():
    """Company Overview Console — admin/c_suite ONLY (#263). A `pm` is scoped to assigned
    projects and lands on /projects instead; hitting the company console directly returns
    403 (server-enforced — hiding nav is not access control). The before_request login gate
    has already ensured the request is authenticated."""
    role = (current_user() or {}).get("role")
    if not access.can_access_company(role):
        return Response(_COMPANY_FORBIDDEN_HTML, status=403, mimetype="text/html")
    page = COMPANY_DASHBOARD_PATH if COMPANY_DASHBOARD_PATH.exists() else DASHBOARD_PATH
    return ui_version.serve_ui(page, _serve_html_no_store)   # #279


# #263 — a pm/super (non-company role) that reaches the company console gets this 403,
# with a one-click path back to where they belong (their projects), never a dead end.
_COMPANY_FORBIDDEN_HTML = (
    "<!doctype html><meta charset=utf-8><title>Not authorized</title>"
    "<div style=\"font-family:Inter,system-ui,sans-serif;max-width:520px;margin:18vh auto;"
    "text-align:center;color:#222633\">"
    "<div style=\"font-size:15px;font-weight:700;color:#B11E2E;letter-spacing:.5px\">NOT AUTHORIZED</div>"
    "<p style=\"color:#76777E;font-size:14px;line-height:1.6;margin:14px 0 22px\">"
    "The company console is limited to admin and C-suite. Your projects are over here.</p>"
    "<a href=\"/projects\" style=\"display:inline-block;background:#B11E2E;color:#fff;"
    "text-decoration:none;padding:10px 20px;border-radius:6px;font-size:13px;font-weight:600\">"
    "&rarr; Go to Projects</a></div>"
)


@app.route('/projects')
def pm_projects_landing():
    """#263 — projects-only landing. A `pm` lands here (assigned ACTIVE projects only,
    closed excluded; scoping enforced in /api/projects). admin/c_suite normally land on
    the company console but may view this too. The list itself is server-scoped, so a pm
    never sees a project they aren't assigned."""
    if PM_PROJECTS_PATH.exists():
        return ui_version.serve_ui(PM_PROJECTS_PATH, _serve_html_no_store)   # #279
    return ("projects page missing", 500)


def _serve_html_no_store(path):
    """Serve an HTML page NO-STORE with NO ETag/conditional handling (#205 cache
    lesson, generalized in #210 for the company console). `/` and /projects/<code>
    do not end in .html, so the global no-cache after_request hook does not also
    strip ETag — disable conditional/etag here so a stale validator can never 304
    old markup/JS back. The visible build-version stamp tells the operator which
    build is live."""
    resp = send_file(str(path), conditional=False, etag=False, max_age=0)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


def _project_identity(code):
    """{project_code, name, client_name} for the shell header, or None. Read-only, and
    NOT access control — the #263 before_request hook has already rejected a project this
    user may not open by the time we get here."""
    if not code:
        return None
    conn = db()
    try:
        row = conn.execute(
            "SELECT project_code, name FROM projects WHERE project_code = ?", (code,)).fetchone()
        if row is None:
            return None
        out = {"project_code": row["project_code"], "name": row["name"], "client_name": None,
               "address": None}
        # #328 — the sidebar identity block shows the site address when the column exists
        try:
            arow = conn.execute("SELECT address FROM projects WHERE project_code = ?", (code,)).fetchone()
            if arow and arow["address"]:
                out["address"] = arow["address"]
        except Exception:
            pass
        # client org is optional (#266 added projects.client_org_id); absent is fine.
        try:
            crow = conn.execute(
                "SELECT o.name AS client_name FROM projects p "
                "JOIN crm_organization o ON o.id = p.client_org_id "
                "WHERE p.project_code = ?", (code,)).fetchone()
            if crow:
                out["client_name"] = crow["client_name"]
        except Exception:
            pass          # pre-#266 database — header simply omits the client line
        return out
    finally:
        conn.close()


def _serve_dashboard_no_store(project_code=None, role_override=None):
    """Project Health surface (per-project dashboard), served no-store. The sidebar is
    RENDERED PER ROLE here:
      * #262 — gated SECTION blocks (Financial) the role can't access are STRIPPED, so a
        disallowed role never receives that markup (the Financial endpoints 403 too).
      * #263 — the "← Back to …" link + in-view "Company Console →" deep links are chosen
        by role: company roles get the company back-link, a pm gets "← Back to Projects"
        and every company deep link is stripped (a pm never sees a path to the console,
        which would 403 anyway).
    Hiding a menu item is not access control — the project-ASSIGNMENT before_request hook
    rejects an unassigned /projects/<code> regardless; this just makes the menu match."""
    # role_override is the PREVIEW-AS-CLIENT path (#281): the shell renders as the target
    # client would see it, so what Amit verifies is what the client gets.
    role = role_override or (current_user() or {}).get("role")
    # #279 — v2 twin when one exists, the untouched v1 file otherwise. The role-based
    # SECTION stripping below runs on WHICHEVER file was chosen: server-side access
    # enforcement is not a v1 behaviour a v2 page gets to opt out of.
    html = ui_version.resolve_page(DASHBOARD_PATH).read_text(encoding="utf-8")
    html = access.render_sections(html, role)   # #262 — strip gated SECTION blocks
    html = access.render_role_nav(html, role)   # #263 — role-correct sidebar nav
    # #281 — project identity was hard-coded into three headers, binding one file to one
    # job and one client. Filled here instead, from the code in the URL.
    html = access.render_project_identity(html, _project_identity(project_code))
    # #281 — and WHERE this shell fetches from: the internal project namespace for
    # internal roles, the curated portal namespace for external ones.
    html = access.render_api_base(html, role, project_code)
    html = _render_billing_type(html, project_code)   # #356
    html = _render_modules(html, project_code)         # per-job optional tabs
    html = job_v2.apply(html, role, project_code)      # opt-in v2 preview, ONLY on a project's own dashboard; v1 is byte-identical
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def _render_modules(html, project_code):
    """Hide the nav links of optional modules this job has switched off, and put the off-list on
    <body data-mod-off> so CSS / the dashboard script can hide the cards that belong to them.
    A convenience, not access control (the endpoints behind keep their own checks)."""
    if not project_code:
        return html
    conn = db()
    try:
        views = project_modules.disabled_views(conn, project_code)
        keys = project_modules.disabled_keys(conn, project_code)
    finally:
        conn.close()
    if not keys:
        return html
    for v in views:
        html = re.sub(r'(<a class="nav-link[^"]*"[^>]*?\bdata-view="%s")' % re.escape(v), r'\1 hidden', html, count=1)
    return re.sub(r'<body([^>]*)>', lambda m: '<body%s data-mod-off="%s">' % (m.group(1), " ".join(keys)), html, count=1)


def _render_billing_type(html, project_code):
    """#356 — the job's billing type rides on <body data-billing-type> so the SOV / AIA
    pieces can react without a request, and the Schedule of Values nav link (already
    stripped for non-financial roles) is hidden on a job billed another way."""
    if not project_code:
        return html
    conn = db()
    try:
        bt = billing_type.get(conn, project_code) or ""
    finally:
        conn.close()
    html = html.replace("<body>", f'<body data-billing-type="{bt}">', 1)
    if bt and bt != "aia_sov":
        html = html.replace('<a class="nav-link" data-view="sov">', '<a class="nav-link" data-view="sov" hidden>', 1)
    return html


@app.route('/projects/<project_code>')
def project_dashboard(project_code):
    """Project-specific dashboard. Project context passed via URL → JS reads it from
    location.pathname. Access is enforced by the #263 before_request scoping hook: a pm
    opening an unassigned or closed project gets 403 here, server-side."""
    return _serve_dashboard_no_store(project_code)


def _serve_portal_shell(project_code, effective_sections, fallback_client=None):
    """#284 — render the EXTERNAL portal shell for one effective section set.

    portal_shell.html is allowlist-first: it contains ONLY portal components (no
    internal nav/views/scripts exist in the file to strip). Rendering is a pure
    function of (project_code, effective_sections, fallback_client) — a real client
    session and an admin preview of that client produce byte-identical pages BY
    CONSTRUCTION (the fallback derives from the TARGET, same either way), which is
    the #284 preview-parity acceptance criterion.

    #285 — "Prepared for" is never blank: the project's client ORG name when the
    #266 link exists, else the client user's display name (the fallback)."""
    identity = _project_identity(project_code) or {"project_code": project_code}
    if not identity.get("client_name") and fallback_client:
        identity = {**identity, "client_name": fallback_client}
    html = ui_version.resolve_page(PORTAL_SHELL_PATH).read_text(encoding="utf-8")
    html = portal_matrix.render_portal_sections(html, effective_sections)
    html = access.render_project_identity(html, identity)
    html = access.render_api_base(html, "client", project_code)   # /api/portal/<code>
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route('/portal/<project_code>')
def portal_project_shell(project_code):
    """#281/#284 — the project shell, served per audience.

    INTERNAL roles get dashboard-static.html on the internal namespace (unchanged).
    A CLIENT (or an admin previewing one, #270) gets portal_shell.html: nav and view
    panes rendered ONLY for their EFFECTIVE sections (#284 matrix ∩ #269 grants),
    fetching from the curated /api/portal/<code>/* namespace. An architect keeps
    /drawing-markup as their home; any other external role has no portal surface.

    WHY A SEPARATE ROUTE rather than letting external roles onto /projects/<code>:
    #264's boundary is a single routing-layer rule — pm_scoping.pm_can_access_project
    returns False for every external role on any path carrying a project code, which
    covers /projects/<code> AND every /api/projects/<code>/… in one place. Opening it
    would have made ~26 internal endpoint families reachable for external sessions with
    only per-endpoint gates behind them. This route spends no security decision to save
    work: the boundary is untouched and an external session never forms an internal URL.

    Access here is the external user's own binding — the same pm_project_assignment /
    client grant machinery the portal already uses — checked server-side, every request."""
    actor = current_user() or {}
    role = actor.get("role")
    effective = actor

    # THE #281 TEMPORARY LOCK IS REMOVED HERE (#284 flip commit): external roles now
    # ENTER this route and are served per the audience branches below — nav is grant-
    # driven, the payloads are registry-served, and the scrub/parity guards are green.
    # Reverting THIS COMMIT alone restores the pre-flip world (Classic landing).

    # PREVIEW-AS-CLIENT (#270 pattern). Without this the shell would render with the
    # ADMIN's role and show Amit everything — a preview that disagrees with what the
    # client actually gets is worse than no preview at all, which is the whole reason
    # this is an explicit switch rather than a silent repoint.
    raw_preview = request.args.get("preview_client")
    if raw_preview and role in ("admin", "c_suite"):
        conn = db()
        try:
            try:
                target_id = int(raw_preview)
            except (TypeError, ValueError):
                return jsonify({"error": "forbidden"}), 403
            trow = conn.execute(
                "SELECT id, role, status, is_active, display_name, full_name, email "
                "FROM users WHERE id=?",
                (target_id,)).fetchone()
            if (not trow or trow["role"] not in access.EXTERNAL_API_ROLES
                    or trow["status"] != "active" or not trow["is_active"]):
                return jsonify({"error": "forbidden"}), 403
            effective = {"id": trow["id"], "role": trow["role"],
                         "display_name": trow["display_name"],
                         "full_name": trow["full_name"], "email": trow["email"]}
            conn.execute(
                "INSERT INTO audit_log (action, actor_user_id, actor_role, target_type, "
                "target_id, note, created_at) VALUES (?,?,?,?,?,?,?)",
                ("portal_shell_preview", actor.get("id"), role, "user", str(target_id),
                 f"read-only new-shell preview of {project_code}",
                 datetime.now().isoformat(timespec="seconds")))
            conn.commit()
        finally:
            conn.close()
        role = effective["role"]

    conn = db()
    try:
        allowed = elevation.accessible_codes(conn, effective)
    finally:
        conn.close()
    if project_code not in allowed:
        logging.info(f"portal_shell: scope block role={role} code={project_code}")
        # A bare 403 — deliberately NOT the company-console message, which offers a link
        # to /projects that an external role cannot use and would not understand.
        return Response(
            "<!doctype html><meta charset=utf-8><title>Not authorized</title>"
            "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
            "max-width:420px;margin:18vh auto;text-align:center;color:#23211d\">"
            "<div style=\"font-size:15px;font-weight:700;color:#B11E2E;letter-spacing:.5px\">"
            "NOT AUTHORIZED</div><p style=\"color:#8a8378;font-size:14px;line-height:1.6\">"
            "This project is not available on your account.</p></div>",
            status=403, mimetype="text/html")

    # ---- #284: the EXTERNAL experience — the portal shell, per effective sections ----
    if role == "client":
        conn = db()
        try:
            eff = portal_matrix.effective_sections(
                conn, effective["id"], "client", project_code)
        finally:
            conn.close()
        if not eff:
            # zero effective sections: a real client belongs on the #267 welcome
            # hard-stop (their gate already routes them there — this is the in-route
            # backstop); a previewing admin goes back where containment is stated.
            is_preview = effective is not actor
            return redirect("/admin/projects" if is_preview else "/welcome")
        # #285 — "Prepared for" fallback: the TARGET's display name (same value for
        # a real client session and an admin preview of that client — parity holds).
        fallback = (effective.get("display_name") or effective.get("full_name")
                    or effective.get("email"))
        return _serve_portal_shell(project_code, eff, fallback_client=fallback)
    if role == "architect":
        # #284 seeds the architect's MATRIX row, but architects have no grant
        # machinery yet — their effective set is empty by construction, and their
        # home remains the drawing surface (landing deliberately unchanged tonight).
        return redirect("/drawing-markup")
    if role in access.EXTERNAL_API_ROLES:
        # vendor / any future external role: no matrix row -> no portal surface.
        return jsonify({"error": "forbidden"}), 403

    # INTERNAL roles: unchanged — the internal shell on the internal namespace.
    return _serve_dashboard_no_store(project_code, role_override=role)


@app.route('/dashboard')
def legacy_dashboard():
    """Legacy entry. Company roles land on the default project dashboard (unchanged); a
    pm has no 'default project', so route them to their projects list (#263)."""
    role = (current_user() or {}).get("role")
    if not access.can_access_company(role):
        return redirect('/projects')
    return _serve_dashboard_no_store()


# ============= DASHBOARD WIDGET LAYOUTS (#209) — generic, per-user =============
# Drag/resize positions for a user's widgets on a given page. GENERIC: page_key
# lets Project Health, the company console, and future surfaces share one table
# + one JS module. PII discipline: layout_json is sanitized to a list of
# {id,x,y,w,h} — widget ids + grid positions ONLY, never names/rates/PINs.
_LAYOUT_PAGE_KEYS = {'project_health', 'company_console',
                     'portal_progress'}   # #286 — the client portal's widget grid
# #278 ROOT FIX — the per-page widget-id ALLOWLIST is gone. It was frozen at the
# original widgets, so the sanitizer SILENTLY STRIPPED every newer widget
# (material-alerts #272b, project-costs #278) from saved layouts: drags "saved"
# but the new widgets' positions were discarded server-side (operator-reported
# live). Enumerated lists rot — the #275 lesson, server-side edition. The
# security property (layout_json can never carry injected data) is preserved
# STRUCTURALLY instead: ids must match a strict charset/length pattern; whether
# an id corresponds to a real widget is the client's concern (dash_layout drops
# ids with no matching grid item on load).
_LAYOUT_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,39}$')


def _sanitize_layout(page_key, raw):
    """Coerce arbitrary input into a safe layout: a list of {id,x,y,w,h} with
    integer positions and pattern-safe widget ids (see _LAYOUT_ID_RE — no
    enumerated per-widget allowlist, it silently ate new widgets). Returns None
    if not a list. layout_json still can never carry injected data: ids are
    charset/length-bound, positions are clamped ints."""
    if not isinstance(raw, list):
        return None
    allowed = None   # pattern-validated below; no enumeration

    def _int(v, lo, hi, dflt):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return dflt
        return max(lo, min(hi, n))

    out = []
    seen = set()
    for it in raw:
        if not isinstance(it, dict):
            continue
        wid = it.get('id')
        if not isinstance(wid, str) or wid in seen:
            continue
        if not _LAYOUT_ID_RE.match(wid):
            continue
        if allowed is not None and wid not in allowed:
            continue
        seen.add(wid)
        out.append({
            'id': wid,
            'x': _int(it.get('x'), 0, 11, 0),
            'y': _int(it.get('y'), 0, 999, 0),
            'w': _int(it.get('w'), 1, 12, 4),
            'h': _int(it.get('h'), 1, 50, 2),
        })
    return out


@app.route('/api/dashboard/layout', methods=['GET'])
def api_dashboard_layout_get():
    """Return THIS user's saved layout for page_key, or data:null (use default)."""
    user = current_user()
    if not user:
        return jsonify({"error": "auth required"}), 401
    page_key = request.args.get('page_key', '')
    if page_key not in _LAYOUT_PAGE_KEYS:
        return jsonify({"error": "unknown page_key"}), 400
    conn = db()
    try:
        row = conn.execute(
            "SELECT layout_json, updated_at FROM dashboard_layouts "
            "WHERE user_id=? AND page_key=?", (user['id'], page_key)).fetchone()
    finally:
        conn.close()
    if not row:
        return response_wrapper(None)
    try:
        layout = json.loads(row['layout_json'])
    except (ValueError, TypeError):
        layout = None
    return response_wrapper({"page_key": page_key, "layout": layout,
                             "updated_at": row['updated_at']})


@app.route('/api/dashboard/layout', methods=['PUT', 'POST'])
def api_dashboard_layout_save():
    """Upsert THIS user's layout for page_key (one row per user+page)."""
    user = current_user()
    if not user:
        return jsonify({"error": "auth required"}), 401
    body = request.get_json(silent=True) or {}
    page_key = body.get('page_key', '')
    if page_key not in _LAYOUT_PAGE_KEYS:
        return jsonify({"error": "unknown page_key"}), 400
    layout = _sanitize_layout(page_key, body.get('layout'))
    if layout is None:
        return jsonify({"error": "layout must be a list of {id,x,y,w,h}"}), 400
    conn = db()
    try:
        conn.execute(
            "INSERT INTO dashboard_layouts (user_id, page_key, layout_json, updated_at) "
            "VALUES (?,?,?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(user_id, page_key) DO UPDATE SET "
            "layout_json=excluded.layout_json, updated_at=CURRENT_TIMESTAMP",
            (user['id'], page_key, json.dumps(layout)))
        conn.commit()
    finally:
        conn.close()
    return response_wrapper({"page_key": page_key, "saved": len(layout)})


@app.route('/api/dashboard/layout', methods=['DELETE'])
def api_dashboard_layout_reset():
    """Delete THIS user's saved layout for page_key — page reverts to default."""
    user = current_user()
    if not user:
        return jsonify({"error": "auth required"}), 401
    page_key = request.args.get('page_key', '')
    if page_key not in _LAYOUT_PAGE_KEYS:
        return jsonify({"error": "unknown page_key"}), 400
    conn = db()
    try:
        cur = conn.execute(
            "DELETE FROM dashboard_layouts WHERE user_id=? AND page_key=?",
            (user['id'], page_key))
        conn.commit()
        deleted = cur.rowcount
    finally:
        conn.close()
    return response_wrapper({"page_key": page_key, "reset": True, "deleted": deleted})


# Comp-sensitive actions are kept OFF the general activity feed (the audit rows
# still exist for the dedicated comp surfaces). PII/comp discipline.
_ACTIVITY_DENY_ACTIONS = {'rate_change', 'hours_correction'}


@app.route('/api/activity/recent', methods=['GET'])
def api_activity_recent():
    """Recent audit-log events for the company console activity feed. PII-SAFE:
    returns action + target_type + target_id (codes/ids — W-####/E-#####/project/
    drop codes, NEVER names) + actor_role + timestamp ONLY. before_json/after_json
    (which can hold row snapshots / comp values) and actor_user_id are NEVER
    serialized. Comp-sensitive actions are filtered out. Requires a logged-in user."""
    if not current_user():
        return jsonify({"error": "auth required"}), 401
    try:
        limit = max(1, min(50, int(request.args.get('limit', 12))))
    except (TypeError, ValueError):
        limit = 12
    conn = db()
    try:
        rows = conn.execute(
            "SELECT action, target_type, target_id, actor_role, created_at "
            "FROM audit_log ORDER BY id DESC LIMIT 200").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        if r['action'] in _ACTIVITY_DENY_ACTIONS:
            continue
        out.append({'action': r['action'], 'target_type': r['target_type'],
                    'target_id': r['target_id'], 'actor_role': r['actor_role'],
                    'created_at': r['created_at']})
        if len(out) >= limit:
            break
    return response_wrapper(out)


# Logging setup
logging.basicConfig(
    filename=str(ssc_paths.server_log_path()),
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# #309-ops — mirror WARNING and above to stderr as well. The file under the data root
# stays the full INFO record; the stream is what a cloud host (Render) collects, so a
# "password reset email not sent via sendgrid" line is visible from the dashboard
# without a shell into the disk. Guarded so a re-import (tests) adds it once.
if not any(getattr(h, "_ssc_stream_mirror", False) for h in logging.getLogger().handlers):
    _stream = logging.StreamHandler()   # defaults to stderr
    _stream.setLevel(logging.WARNING)
    _stream.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s'))
    _stream._ssc_stream_mirror = True
    logging.getLogger().addHandler(_stream)

class _ReqConn:
    """#291 — request-scoped connection PROXY. db() hands every caller in one
    request the SAME underlying connection; a handler's .close() must not kill
    it for the next caller (handlers close 'their' connection everywhere), so
    close() ROLLS BACK instead — byte-equivalent to what a real close did to
    uncommitted work (sqlite discards on close) while keeping the connection
    alive. Everything else delegates to the real connection; the real close
    happens in _close_req_conn at request teardown."""

    __slots__ = ("_c",)

    def __init__(self, c):
        object.__setattr__(self, "_c", c)

    def close(self):
        try:
            self._c.rollback()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._c, name)

    def __setattr__(self, name, value):
        setattr(self._c, name, value)

    def __enter__(self):
        return self._c.__enter__()

    def __exit__(self, *a):
        return self._c.__exit__(*a)


def db():
    # #259 — routed through the env-driven layer (SSC_DB_URL). SQLite is the DEFAULT
    # and returns the SAME native sqlite3 connection as before (60s timeout, Row,
    # WAL + busy_timeout); a postgres:// URL returns the psycopg-backed wrapper.
    # #291 — ONE connection per request, cached on g: the app opened a fresh
    # connection per db() call (several per request; the console bootstrap paid
    # EIGHT network handshakes on cloud Postgres). Outside a request context
    # (CLI scripts importing server helpers) behavior is unchanged per-call.
    if not flask.has_request_context():
        return db_layer.connect()
    c = getattr(flask.g, "_ssc_req_conn", None)
    if c is None:
        c = db_layer.connect()
        flask.g._ssc_req_conn = c
    return _ReqConn(c)


@app.teardown_request
def _close_req_conn(exc):
    """#291 — the request-scoped connection's REAL close. Exception -> roll
    back first so a half-done write never lingers into the pool of one."""
    c = getattr(flask.g, "_ssc_req_conn", None)
    if c is not None:
        flask.g._ssc_req_conn = None
        try:
            if exc is not None:
                c.rollback()
            c.close()
        except Exception:
            pass

def rows_to_dicts(rows):
    return [dict(row) for row in rows]


# #247 — CLAUDE.md §2: no filesystem path EVER crosses the wire. Files are
# served via gated, id-based routes only. This is the single scrubber every
# response that spreads a DB row (SELECT *, dict(row), rows_to_dicts) runs
# through, so a path column can't reach JSON even if a future SELECT adds one.
# The gate's response-path guard (tests/_smoke_auth.py) enforces the same
# invariant from the outside. Server-side path handling (building paths from
# ids, file ops) is untouched — this only strips the OUTBOUND JSON.
# 'folder_slug' is included because it embeds the worker's name (PII), same
# class as the paths even though it isn't literally a path.
_RESPONSE_PATH_KEYS = frozenset({
    'folder_path', 'face_image_path', 'photo_path', 'file_path', 'filepath',
    'scan_path', 'html_path', 'pdf_path', 'edge_path', 'signature_path',
    'datasheet_pdf_path', 'folder_slug',
})


def scrub_paths(obj):
    """Recursively drop path-bearing keys from dicts/lists IN PLACE; returns
    obj for convenience. Use at every response site that emits a raw DB row."""
    if isinstance(obj, dict):
        for k in [k for k in obj if k in _RESPONSE_PATH_KEYS]:
            del obj[k]
        for v in obj.values():
            scrub_paths(v)
    elif isinstance(obj, list):
        for v in obj:
            scrub_paths(v)
    return obj

# #305 — worker PII never leaves the employee endpoints for non-company roles.
# `pin` never leaves them at all (credential cards have their own gated route).
_WORKER_PII_ALWAYS = frozenset({'pin'})
_WORKER_PII_COMPANY_ONLY = frozenset({
    'dob', 'phone', 'email', 'emergency_contact_name', 'emergency_contact_phone',
    'emergency_contact_relation', 'home_address', 'address', 'license_number',
    'id_number', 'ssn',
})


def strip_worker_pii(obj, role=None):
    """Recursively drop PII keys from a payload. Company roles (admin/c_suite) keep
    contact fields (the workforce profile shows them); everyone else gets the
    roster shape only. Returns the same structure."""
    if role is None:
        role = (current_user() or {}).get('role')
    deny = set(_WORKER_PII_ALWAYS)
    if role not in access.COMPANY_ROLES:
        deny |= _WORKER_PII_COMPANY_ONLY
    def _walk(x):
        if isinstance(x, dict):
            return {k: _walk(v) for k, v in x.items() if k not in deny}
        if isinstance(x, list):
            return [_walk(v) for v in x]
        return x
    return _walk(obj)


def _server_error(e):
    """#305 — never echo driver/exception text to the client (table + column names,
    conflicting values). Logged with a short id the operator can grep for."""
    import uuid
    ref = uuid.uuid4().hex[:8]
    logging.error(f"[{ref}] {request.method} {request.path}: {type(e).__name__}: {e}")
    return jsonify({"error": "Something went wrong on the server.", "ref": ref}), 500


def response_wrapper(data, count=None, **extra_meta):
    """Wrap response with metadata.

    #251 — defense-in-depth for the CLAUDE.md PII rule: every wrapped payload is
    run through scrub_paths(), so a path-bearing key (folder_path,
    face_image_path, scan_path, ...) can NEVER reach JSON — even if a response
    site spreads a raw DB row (SELECT *) and forgets the manual scrub. This makes
    "no filesystem path on the wire" a STRUCTURAL invariant instead of per-site
    discipline (the #247 model relied on ~16 hand-placed scrub_paths calls being
    complete and every future endpoint remembering). Files cross the wire via
    gated id-based routes only; the per-site scrub_paths calls stay as
    belt-and-suspenders (idempotent). count is unaffected — scrub drops dict keys,
    never list elements.
    """
    scrub_paths(data)
    return jsonify({
        "data": data,
        "meta": {
            "count": count if count is not None else len(data) if isinstance(data, list) else 1,
            "generated_at": datetime.now().isoformat(),
            **extra_meta,
        }
    })

def _fmt_mdy(value) -> str:
    """'2026-05-21' (or 'YYYY-MM-DDTHH:MM:SS', 'YYYY-MM-DD HH:MM:SS') -> '05-21-2026'.

    Single Python-side date-display helper, mirroring SSCDatePicker.fmtMDY
    on the client + _fmt_mdy in render_dcr_html. Date-only output — the
    "Issued at" / cert / RFI surfaces drop the time portion per the
    display rule. Returns '' for empty input so template fields render
    blank rather than 'None'.
    """
    if not value:
        return ''
    s = str(value)[:10]
    try:
        d = datetime.strptime(s, '%Y-%m-%d').date()
        return d.strftime('%m-%d-%Y')
    except (ValueError, TypeError):
        return str(value)

# Per CLAUDE.md PII rule: request logging records metadata only (method, sanitized
# path, status, body size) — never request content. See request_logging.py.
import request_logging
request_logging.apply_request_logging(app)

# Cross-site write protection (Origin / Sec-Fetch-Site). Mode via SSC_CSRF_MODE: enforce (default) | report | off.
import csrf_guard
csrf_guard.apply_csrf_guard(app)

# Test-only seams are inert unless a test run opts in (runtime_seams.py); name any ignored ones.
runtime_seams.warn_if_ignored()

# ============= DASHBOARD META =============

@app.route('/api/health', methods=['GET'])
def health():
    # #294 S2 — the punchlist "health db label" fix: the payload said
    # "superstars.db" even on Postgres. Backend FLAVOR only — never the URL.
    return response_wrapper({
        "status": "ok",
        "db": "postgres" if db_layer.is_postgres() else "sqlite",
        "migrations_pending": migrations.pending_count(),   # #306 — 0 is healthy; -1 = ledger unreadable
        "timestamp": datetime.now().isoformat()
    })

@app.route('/api/today', methods=['GET'])
def today():
    return response_wrapper({
        "date": date.today().isoformat()
    })

# ============= WRITE HELPERS =============

def get_next_rfi_number(conn, project_code):
    """Auto-generate next project-prefixed sequential RFI number per Phase-2
    handoff: <project_code>-RFI-NNNN, zero-padded to 4 digits.
    Example: FR-BX-001-RFI-0001.

    Allocates the SMALLEST positive integer not currently in use for this
    project (gap-fill — matches the DCR sequence convention). Robust to
    legacy short-form 'RFI-XXX' rows: the regex scans every numeric suffix
    on the project's rows and picks max+1 in single-pass, ignoring rows
    that don't parse.
    """
    import re
    rows = conn.execute(
        "SELECT rfi_number FROM rfi_log WHERE project_code = ?",
        (project_code,),
    ).fetchall()
    used = set()
    for r in rows:
        wid = r['rfi_number'] or ''
        m = re.search(r'(?:^|-)(\d{1,6})$', wid)
        if m:
            try:
                used.add(int(m.group(1)))
            except ValueError:
                pass
    n = 1
    while n in used:
        n += 1
    return f"{project_code}-RFI-{n:04d}"

def validate_employee_exists(conn, employee_id):
    """Validate employee exists"""
    row = conn.execute("SELECT 1 FROM employees WHERE employee_id = ?", (employee_id,)).fetchone()
    return row is not None

def validate_project_exists(conn, project_code):
    """Validate project exists"""
    row = conn.execute("SELECT 1 FROM projects WHERE project_code = ?", (project_code,)).fetchone()
    return row is not None

# ============= SIGN-IN LOG ENDPOINTS =============

def _hours_check(conn, employee_id, date_str, time_in, time_out, exclude_id=None, reason=None):
    """One place at a time + the 8-hour day (payroll_hours). Returns
    (error_response, None) or (None, the day's total after this write).
    409 code=overlap: the times overlap another entry that day (any job).
    422 code=needs_reason: the day passes 8 hours and no reason was given."""
    import payroll_hours as ph
    others = ph.day_entries(conn, employee_id, date_str, exclude_id)
    hit = ph.find_overlap(others, time_in, time_out)
    if hit:
        return (jsonify({"error": ph.overlap_message(hit), "code": "overlap",
                         "project_code": hit["project_code"]}), 409), None
    total = ph.day_total(others, (time_in, time_out))
    if (ph.rule_applies(date_str) and not (reason or "").strip()
            and ph.needs_reason(total, ph.get_overtime(conn, employee_id, date_str))):
        return (jsonify({"error": f"That makes {total:g} hours for the day. Hours over 8 need a "
                                  "reason and an admin or PM's approval.",
                         "code": "needs_reason", "needs_reason": True, "day_total": total}), 422), None
    return None, total


def _hours_sync(conn, employee_id, date_str, reason=None, actor_role=None):
    """After a sign-in write: bring the day's over-8 request in step."""
    import payroll_hours as ph
    if not ph.rule_applies(date_str):
        return
    try:
        from auth import current_user
        user = current_user() or {}
    except Exception:
        user = {}
    total = ph.day_total(ph.day_entries(conn, employee_id, date_str))
    ph.sync_overtime(conn, employee_id, date_str, total, reason,
                     actor_uid=user.get("id"), actor_role=actor_role or user.get("role"))


@app.route('/api/sign-ins', methods=['POST'])
def create_sign_in():
    """Create a sign-in row. Two supported callers:
    - worker-app live flow (via /api/worker-sign-in, not this route): time_in
      only, time_out set later by sign-out
    - DCR form's backdated manual labor entry: passes BOTH time_in + time_out
      with an arbitrary date (the operator entering a past day's roster)

    Validates: employee + project exist, time_out >= time_in when both present,
    and rejects duplicates for the same (employee_id, date, project_code) with
    409 — silent duplicates double-count hours in the DCR labor aggregator, so
    the caller must explicitly delete the existing row before re-adding.
    """
    try:
        data = request.get_json() or {}
        employee_id = data.get('employee_id')
        project_code = data.get('project_code')
        date_str = data.get('date', date.today().isoformat())
        time_in = data.get('time_in')
        time_out = data.get('time_out')  # optional — backdated entry supplies both

        if not employee_id or not project_code or not date_str or not time_in:
            return jsonify({"error": "employee_id, project_code, date, time_in are required"}), 400
        # HH:MM string compare is correct for same-day shifts (the DCR labor
        # path doesn't model overnight). Reject inverted ranges explicitly so
        # they don't silently render as negative hours later.
        if time_out and time_out < time_in:
            return jsonify({"error": f"time_out ({time_out}) is before time_in ({time_in})"}), 400

        conn = db()
        if not validate_employee_exists(conn, employee_id):
            conn.close()
            return jsonify({"error": "Employee not found"}), 400
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 400

        existing = conn.execute(
            "SELECT id, time_in, time_out FROM sign_in_log "
            "WHERE employee_id = ? AND date = ? AND project_code = ?",
            (employee_id, date_str, project_code)
        ).fetchone()
        if existing:
            conn.close()
            return jsonify({
                "error": "Sign-in already exists for this worker on this date",
                "existing_id": existing["id"],
                "existing_time_in": existing["time_in"],
                "existing_time_out": existing["time_out"],
            }), 409

        ot_reason = (data.get('overtime_reason') or '').strip()
        err, _total = _hours_check(conn, employee_id, date_str, time_in, time_out, reason=ot_reason)
        if err:
            conn.close()
            return err
        now = datetime.now().isoformat()
        cur = conn.execute(
            "INSERT INTO sign_in_log (date, employee_id, project_code, time_in, time_out, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date_str, employee_id, project_code, time_in, time_out, now, now)
        )
        new_id = cur.lastrowid   # before _hours_sync, which may insert its own row
        _hours_sync(conn, employee_id, date_str, ot_reason)
        # Stale flag: if a DCR was already issued for this (project, date),
        # the rendered artifact no longer matches live labor. Mark it.
        _mark_dcr_stale(conn, project_code, date_str)
        conn.commit()
        row = conn.execute("SELECT * FROM sign_in_log WHERE id = ?", (new_id,)).fetchone()
        conn.close()
        return response_wrapper(dict(row)), 201
    except Exception as e:
        logging.error(f"POST /api/sign-ins: {str(e)}")
        return _server_error(e)

@app.route('/api/sign-ins/<int:sign_in_id>', methods=['PATCH'])
def update_sign_in(sign_in_id):
    """Update sign-out time. #295 — a correction through this door writes a
    sign_in_edit history row; an issued day gets the hours_amended flag,
    audit row, and an auto re-render of the issued artifact(s)."""
    try:
        data = request.get_json()
        time_out = data.get('time_out')
        reason = (data.get('reason') or '').strip()[:200] or None

        conn = db()
        before = conn.execute(
            "SELECT * FROM sign_in_log WHERE id = ?", (sign_in_id,)
        ).fetchone()
        if not before:
            conn.close()
            return jsonify({"error": "sign_in_log row not found"}), 404
        ot_reason = (data.get('overtime_reason') or '').strip()
        err, _total = _hours_check(conn, before["employee_id"], before["date"], before["time_in"],
                                   time_out, exclude_id=sign_in_id, reason=ot_reason)
        if err:
            conn.close()
            return err
        conn.execute(
            "UPDATE sign_in_log SET time_out = ?, updated_at = ? WHERE id = ?",
            (time_out, datetime.now().isoformat(), sign_in_id)
        )
        _hours_sync(conn, before["employee_id"], before["date"], ot_reason)
        edit_id = _record_sign_in_edit(conn, before, 'edit', before["time_in"], time_out, reason)
        _mark_dcr_stale(conn, before["project_code"], before["date"])
        amended = _amend_issued_dcr_hours(
            conn, before["project_code"], before["date"], edit_id,
            "sign-out corrected after issue")
        conn.commit()

        row = conn.execute("SELECT * FROM sign_in_log WHERE id = ?", (sign_in_id,)).fetchone()
        conn.close()
        out = dict(row)
        if amended:
            rr = _rerender_amended_dcr(before["project_code"], before["date"],
                                       amended["sequence"], amended["audiences"])
            out["dcr_amended"] = {"display_id": amended["display_id"],
                                  "rerendered": rr.get("ok", False),
                                  "rerender_error": rr.get("error")}
        return response_wrapper(out), 200
    except Exception as e:
        return _server_error(e)


@app.route('/api/sign-ins/<int:sign_in_id>', methods=['PUT'])
def replace_sign_in(sign_in_id):
    """Full replace of a sign_in_log row's billable times.

    Used by the Weekly Hours Log when the operator edits a cell that already
    has an entry. Distinct from PATCH (which only updates time_out) because
    the payroll grid lets the operator change BOTH time_in and time_out at
    once (e.g., late arrival + early departure on the same shift).

    Body: {time_in: "HH:MM", time_out: "HH:MM"}. Both required.
    Validates time_out >= time_in via the same HH:MM string compare used by
    POST. Returns the updated row, or 404 if the id doesn't exist."""
    try:
        data = request.get_json() or {}
        time_in = data.get('time_in')
        time_out = data.get('time_out')
        if not time_in or not time_out:
            return jsonify({"error": "time_in and time_out are required"}), 400
        if time_out < time_in:
            return jsonify({"error": f"time_out ({time_out}) is before time_in ({time_in})"}), 400

        reason = (data.get('reason') or '').strip()[:200] or None
        conn = db()
        before = conn.execute(
            "SELECT * FROM sign_in_log WHERE id = ?", (sign_in_id,)
        ).fetchone()
        if not before:
            conn.close()
            return jsonify({"error": "sign_in_log row not found"}), 404
        ot_reason = (data.get('overtime_reason') or '').strip()
        err, _total = _hours_check(conn, before["employee_id"], before["date"], time_in, time_out,
                                   exclude_id=sign_in_id, reason=ot_reason)
        if err:
            conn.close()
            return err
        conn.execute(
            "UPDATE sign_in_log SET time_in = ?, time_out = ?, updated_at = ? WHERE id = ?",
            (time_in, time_out, datetime.now().isoformat(), sign_in_id)
        )
        _hours_sync(conn, before["employee_id"], before["date"], ot_reason)
        # #295 — the correction trail + issued-day amendment, in-transaction.
        edit_id = _record_sign_in_edit(conn, before, 'edit', time_in, time_out, reason)
        _mark_dcr_stale(conn, before["project_code"], before["date"])
        amended = _amend_issued_dcr_hours(
            conn, before["project_code"], before["date"], edit_id,
            "hours corrected after issue")
        conn.commit()
        row = conn.execute("SELECT * FROM sign_in_log WHERE id = ?", (sign_in_id,)).fetchone()
        conn.close()
        out = dict(row)
        if amended:
            # Post-commit by design: the DB correction is the truth; a render
            # failure degrades to the stale-flag manual path, never a rollback.
            rr = _rerender_amended_dcr(before["project_code"], before["date"],
                                       amended["sequence"], amended["audiences"])
            out["dcr_amended"] = {"display_id": amended["display_id"],
                                  "rerendered": rr.get("ok", False),
                                  "rerender_error": rr.get("error")}
        return response_wrapper(out), 200
    except Exception as e:
        logging.error(f"PUT /api/sign-ins/{sign_in_id}: {str(e)}")
        return _server_error(e)


@app.route('/api/sign-ins/<int:sign_in_id>', methods=['DELETE'])
def delete_sign_in(sign_in_id):
    """Delete a sign_in_log row. Used by the Weekly Hours Log when the
    operator clears a cell (a worker who was incorrectly marked as
    present, or a row entered against the wrong worker/date).

    Returns 404 if the id doesn't exist so the caller can distinguish
    'already gone' from 'never existed' if needed."""
    try:
        # #295 — reason rides the query string (DELETE bodies are unreliable
        # across proxies); cancel-rollback deletes label themselves with it.
        reason = (request.args.get('reason') or '').strip()[:200] or None
        conn = db()
        # Read the FULL row BEFORE deleting — the history row preserves the
        # removed times (amend-never-erase: the delete erases the row, the
        # trail keeps what it said).
        before = conn.execute(
            "SELECT * FROM sign_in_log WHERE id = ?", (sign_in_id,)
        ).fetchone()
        if not before:
            conn.close()
            return jsonify({"error": "sign_in_log row not found"}), 404
        conn.execute("DELETE FROM sign_in_log WHERE id = ?", (sign_in_id,))
        _hours_sync(conn, before["employee_id"], before["date"])
        edit_id = _record_sign_in_edit(conn, before, 'remove', None, None, reason)
        _mark_dcr_stale(conn, before["project_code"], before["date"])
        amended = _amend_issued_dcr_hours(
            conn, before["project_code"], before["date"], edit_id,
            "sign-in removed after issue")
        conn.commit()
        conn.close()
        out = {"deleted": True, "id": sign_in_id}
        if amended:
            rr = _rerender_amended_dcr(before["project_code"], before["date"],
                                       amended["sequence"], amended["audiences"])
            out["dcr_amended"] = {"display_id": amended["display_id"],
                                  "rerendered": rr.get("ok", False),
                                  "rerender_error": rr.get("error")}
        return jsonify(out), 200
    except Exception as e:
        logging.error(f"DELETE /api/sign-ins/{sign_in_id}: {str(e)}")
        return _server_error(e)


# ============= PAYROLL: WEEKLY HOURS LOG =============

@app.route('/api/payroll/hours', methods=['GET'])
def api_payroll_hours():
    """Weekly Hours grid for payroll. Source of truth = sign_in_log.

    Query params:
      week_start (optional, ISO 'YYYY-MM-DD'): the Monday of the week. If
        omitted, defaults to the last completed Mon-Sun week relative to
        today (payroll runs one week in arrears). #310 — seven days.

    Returns the payroll_hours.build_week_grid dict: dates, workers (with
    per-day cells + weekly totals), totals_by_day, grand_total. Worker pool
    = employees with at least one active project_assignment.

    Role-gated comp data (#158): if the requester's role is
    'admin' / 'c_suite', each worker carries `hourly_rate` and
    `amount_owed` for THIS week. For all other roles those keys are
    OMITTED ENTIRELY (not zeroed, not "—") so a sniffed payload
    reveals nothing about pay (per CLAUDE.md comp-data rule + #146
    handoff).

    #197 — Rate lookup is PER-DAY-WORKED, not as-of-week-start. Each
    sign_in_log row gets joined to the rate effective on its own date.
    `amount_owed` = SUM(day_hours * rate_effective_on(day_date)) across
    the week. `rate_not_set` is True only when the worker has no
    active rate on ANY day they actually worked.

    Why: the prior as-of-week-start lookup mis-rendered "Rate not set"
    for any worker whose `effective_from` fell mid-week — including
    every new hire and every operator-side rate change that wasn't
    aligned to a Monday. Mario W-0007's effective_from=2026-05-13
    against week_start=2026-05-11 was the operator-reported instance.
    The structural fix is per-day, not per-week.
    """
    from payroll_hours import last_completed_week, build_week_grid
    from worker_rates import get_rate_effective_on, role_can_see_rates
    from auth import current_user
    week_start = (request.args.get('week_start') or '').strip()
    try:
        if week_start:
            monday = datetime.strptime(week_start, '%Y-%m-%d').date()
            if monday.weekday() != 0:
                return jsonify({"error": "week_start must be a Monday"}), 400
        else:
            monday, _ = last_completed_week()
        conn = db()
        try:
            grid = build_week_grid(conn, monday)
            # Comp-data overlay — admin/c_suite only.
            user = current_user() or {}
            if role_can_see_rates(user.get('role')):
                grand_amount = 0.0
                for w in grid.get('workers', []):
                    # Per-day rate lookup — accumulate amount_owed across
                    # each day's hours-worked × that-day's effective rate.
                    # Display rate (the column the operator reads) is the
                    # rate effective on the FIRST day the worker worked
                    # this week; this handles new hires whose effective_from
                    # falls mid-week and rate changes mid-week alike.
                    amount_owed = 0.0
                    display_rate = None
                    display_eff_from = None
                    any_worked_day_has_rate = False
                    worker_actually_worked = False
                    for day in w.get('days', []):
                        day_hours = float(day.get('hours') or 0)
                        if day_hours <= 0:
                            continue
                        worker_actually_worked = True
                        r = get_rate_effective_on(conn, w['employee_id'], day['date'])
                        if r:
                            day_rate = float(r['hourly_rate'])
                            amount_owed += day_hours * day_rate
                            any_worked_day_has_rate = True
                            if display_rate is None:
                                display_rate = day_rate
                                display_eff_from = r['effective_from']
                    if not worker_actually_worked:
                        # Worker on roster but no hours this week — use
                        # current rate (or first historically-set) for
                        # display so the operator can still see what
                        # they'd be paid if they did sign in. Sentinel
                        # 'rate_not_set' only when literally no rate row.
                        r = get_rate_effective_on(conn, w['employee_id'],
                                                  grid['week_end'])
                        if r:
                            w['hourly_rate'] = round(float(r['hourly_rate']), 2)
                            w['rate_effective_from'] = r['effective_from']
                            w['amount_owed'] = 0.0
                        else:
                            w['rate_not_set'] = True
                    elif any_worked_day_has_rate:
                        w['hourly_rate'] = round(display_rate, 2)
                        w['rate_effective_from'] = display_eff_from
                        w['amount_owed'] = round(amount_owed, 2)
                        grand_amount += w['amount_owed']
                    else:
                        # Worker has hours but no rate active on ANY of
                        # the days worked — render "Rate not set."
                        w['rate_not_set'] = True
                grid['grand_amount_owed'] = round(grand_amount, 2)
                grid['rates_visible'] = True
            else:
                grid['rates_visible'] = False
        finally:
            conn.close()
        return response_wrapper(grid), 200
    except Exception as e:
        # Never echo any rate values — only the operation that failed.
        logging.error(f"GET /api/payroll/hours: {str(e)}")
        return _server_error(e)


@app.route('/api/payroll/hours.csv', methods=['GET'])
def api_payroll_hours_csv():
    """CSV export of the weekly hours grid. One row per worker; columns:
    employee_id, name, trade, <Mon date> … <Sun date> (#310 — seven days),
    weekly_total. A 'DAILY TOTAL' summary row follows the data rows.

    Hours are hours WORKED (lunch deducted) so the CSV matches what the
    operator hands to payroll. Pay-or-not is a downstream decision.
    """
    import csv
    import io
    from payroll_hours import last_completed_week, build_week_grid
    week_start = (request.args.get('week_start') or '').strip()
    try:
        if week_start:
            monday = datetime.strptime(week_start, '%Y-%m-%d').date()
            if monday.weekday() != 0:
                return jsonify({"error": "week_start must be a Monday"}), 400
        else:
            monday, _ = last_completed_week()
        conn = db()
        grid = build_week_grid(conn, monday)
        conn.close()

        buf = io.StringIO()
        w = csv.writer(buf)
        # Worker ID (W-####) first — the human-facing identifier that
        # payroll uses to match workers across systems. employee_id stays
        # so the internal key is in the export for traceback.
        w.writerow(["worker_id", "employee_id", "name", "trade",
                    *grid["dates"], "weekly_total", "overtime_pending"])
        # Day cells and totals are PAID hours; extra hours still waiting for
        # approval sit in their own column so nobody pays them by accident.
        for emp in grid["workers"]:
            w.writerow([
                emp.get("worker_id") or "",
                emp["employee_id"], emp["name"], emp["trade"] or "",
                *[(d["hours"] if d["has_entry"] else "") for d in emp["days"]],
                emp["weekly_total"], emp.get("overtime_pending_hours") or "",
            ])
        w.writerow(["", "", "DAILY TOTAL", "",
                    *grid["totals_by_day"], grid["grand_total"],
                    grid.get("overtime_pending_hours") or ""])

        csv_bytes = buf.getvalue().encode("utf-8")
        filename = f"weekly-hours-{grid['week_start']}-to-{grid['week_end']}.csv"
        from flask import Response
        return Response(
            csv_bytes, status=200, mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        logging.error(f"GET /api/payroll/hours.csv: {str(e)}")
        return _server_error(e)


@app.route('/api/payroll/hours.pdf', methods=['GET'])
def api_payroll_hours_pdf():
    """PDF export of the weekly hours grid. Renders an HTML timesheet via
    render_timesheet_html and pipes through pdf_export (headless Edge),
    same pipeline as the DCR PDFs. Returns the PDF as an attachment.

    PDF render failures return 502 with the error so the UI can surface
    "PDF render failed — try CSV instead" rather than a blank download.
    """
    from payroll_hours import last_completed_week, build_week_grid
    from render_timesheet_html import render_timesheet_html
    from pdf_export import render_html_to_pdf, PDFExportError
    week_start = (request.args.get('week_start') or '').strip()
    try:
        if week_start:
            monday = datetime.strptime(week_start, '%Y-%m-%d').date()
            if monday.weekday() != 0:
                return jsonify({"error": "week_start must be a Monday"}), 400
        else:
            monday, _ = last_completed_week()
        conn = db()
        grid = build_week_grid(conn, monday)
        conn.close()

        html_str = render_timesheet_html(grid)

        # Use a stable temp filename pair under data_room so the file is
        # locatable for post-mortem if rendering fails. Overwritten each call.
        out_dir = ssc_paths.under_root("data_room", "reports", "weekly_hours")   # #287
        out_dir.mkdir(parents=True, exist_ok=True)
        html_tmp = out_dir / f"week-{grid['week_start']}.html"
        pdf_tmp = out_dir / f"week-{grid['week_start']}.pdf"
        # Atomic write
        html_swap = out_dir / f"week-{grid['week_start']}.html.tmp"
        html_swap.write_text(html_str, encoding='utf-8')
        html_swap.replace(html_tmp)

        try:
            result = render_html_to_pdf(html_tmp, pdf_tmp)
        except PDFExportError as e:
            return jsonify({"error": f"PDF setup error: {e}"}), 502
        if not result.get("ok"):
            return jsonify({"error": result.get("error", "PDF render failed")}), 502

        filename = f"weekly-hours-{grid['week_start']}-to-{grid['week_end']}.pdf"
        return send_file(
            str(pdf_tmp), mimetype="application/pdf",
            as_attachment=True, download_name=filename,
        )
    except Exception as e:
        logging.error(f"GET /api/payroll/hours.pdf: {str(e)}")
        return _server_error(e)


# ============= RFI ENDPOINTS =============

# ---------------------------------------------------------------------------
# RFI Log — Phase 2 endpoints (handoff HANDOFF_REPORTS_PHASE2_RFI_LOG.md)
# ---------------------------------------------------------------------------
# Field set follows construction_builds_spec.json rfi_log.fields. Status
# values pulled from project_type_config.RFI_STATUS_SUBSET (the spec's
# RFI-specific enum: Open / Answered / Closed / Overdue / Void).
# ---------------------------------------------------------------------------

# Spec-defined writable fields. The "_mirror" map writes the new spec
# names into the legacy columns (description / response / due_date /
# response_date / discipline) too, so a downstream consumer reading
# either set sees the same payload while Phase 2 is rolling out.
_RFI_WRITABLE_FIELDS = (
    'subject_title', 'submitted_by', 'sent_to',
    'date_submitted', 'date_response_required', 'date_response_received',
    'status', 'question_description', 'response_answer',
    'scope_category', 'location_unit', 'location_id',
    'drawing_spec_reference', 'schedule_impact_flag', 'cost_impact_flag',
    'impact_magnitude_note', 'related_documents',
)
_RFI_LEGACY_MIRROR = {
    'question_description': 'description',
    'response_answer':       'response',
    'date_response_required': 'due_date',
    'date_response_received': 'response_date',
    'scope_category':        'discipline',
}


def _coerce_bool(v):
    if isinstance(v, bool):
        return 1 if v else 0
    if v in (1, '1', 'true', 'True', True):
        return 1
    return 0


def _derive_rfi_status(row, today_iso):
    """Auto-Overdue derivation per spec: status flips to 'Overdue' when
    current_date > date_response_required AND date_response_received is
    empty. Persisted status wins for non-Open cases — only Open + past-due
    promotes to Overdue.
    """
    stored = (row.get('status') or 'Open')
    if stored == 'Open':
        drr = row.get('date_response_required')
        drcv = row.get('date_response_received')
        if drr and not drcv and drr < today_iso:
            return 'Overdue'
    return stored


def _rfi_turnaround_days(row):
    """date_response_received - date_submitted in days, or None."""
    sub = row.get('date_submitted')
    rcv = row.get('date_response_received')
    if not sub or not rcv:
        return None
    try:
        d1 = datetime.strptime(sub, '%Y-%m-%d').date()
        d2 = datetime.strptime(rcv, '%Y-%m-%d').date()
        return (d2 - d1).days
    except (ValueError, TypeError):
        return None


def _rfi_row_to_dict(row, today_iso):
    """Shape an rfi_log row for the API: includes status_derived + turnaround_days.
    Maps legacy columns into the spec names for callers that only know the
    new names (e.g. when an old RFI was written via the legacy POST)."""
    d = dict(row)
    d.setdefault('question_description', d.get('description'))
    d.setdefault('response_answer',      d.get('response'))
    d.setdefault('date_response_required', d.get('due_date'))
    d.setdefault('date_response_received', d.get('response_date'))
    d.setdefault('scope_category',       d.get('discipline'))
    d['status_derived'] = _derive_rfi_status(d, today_iso)
    d['turnaround_days'] = _rfi_turnaround_days(d)
    # Booleans normalize to JSON true/false for the UI
    d['schedule_impact_flag'] = bool(d.get('schedule_impact_flag'))
    d['cost_impact_flag']     = bool(d.get('cost_impact_flag'))
    return d


@app.route('/api/rfis', methods=['POST'])
def create_rfi():
    """Create an RFI with the full spec field set.

    Body keys (all optional except project_code; sensible defaults applied):
      project_code (required)
      subject_title, submitted_by, sent_to
      date_submitted (defaults to LOCAL today),
      date_response_required, date_response_received
      status (defaults to 'Open'; restricted to RFI_STATUS_SUBSET)
      question_description, response_answer
      scope_category, location_unit, location_id, drawing_spec_reference
      schedule_impact_flag, cost_impact_flag, impact_magnitude_note,
      related_documents
    Server auto-assigns project-prefixed sequential rfi_number.
    """
    from project_type_config import RFI_STATUS_SUBSET
    data = request.get_json(silent=True) or {}
    if data.get('elevation_id'):
        return elevation._api_rfi_create()   # #305 — drawing-markup RFIs (was a route collision)
    try:
        project_code = data.get('project_code')
        if not project_code:
            return jsonify({"error": "project_code required"}), 400
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 404
        status = data.get('status') or 'Open'
        if status not in RFI_STATUS_SUBSET:
            conn.close()
            return jsonify({
                "error": f"status must be one of {RFI_STATUS_SUBSET}"
            }), 400
        rfi_number = get_next_rfi_number(conn, project_code)
        # LOCAL today per the dates rule (#74). Python's date.today() is local.
        today_iso = date.today().isoformat()
        date_submitted = data.get('date_submitted') or today_iso

        # Build the insert payload — write each field by its spec name AND
        # mirror into the legacy column so a Phase-1 reader sees the same.
        cols = {
            'rfi_number':              rfi_number,
            'project_code':            project_code,
            'subject_title':           data.get('subject_title'),
            'submitted_by':            data.get('submitted_by'),
            'sent_to':                 data.get('sent_to'),
            'date_submitted':          date_submitted,
            'date_response_required':  data.get('date_response_required'),
            'date_response_received':  data.get('date_response_received'),
            'status':                  status,
            'question_description':    data.get('question_description'),
            'response_answer':         data.get('response_answer'),
            'scope_category':          data.get('scope_category'),
            'location_unit':           data.get('location_unit'),
            'location_id':             data.get('location_id'),
            'drawing_spec_reference':  data.get('drawing_spec_reference'),
            'schedule_impact_flag':    _coerce_bool(data.get('schedule_impact_flag')),
            'cost_impact_flag':        _coerce_bool(data.get('cost_impact_flag')),
            'impact_magnitude_note':   data.get('impact_magnitude_note'),
            'related_documents':       data.get('related_documents'),
            # Legacy mirrors — keep Phase-1 readers happy
            'description':             data.get('question_description'),
            'response':                data.get('response_answer'),
            'due_date':                data.get('date_response_required'),
            'response_date':           data.get('date_response_received'),
            'discipline':              data.get('scope_category'),
            'created_at':              datetime.now().isoformat(),
            'updated_at':              datetime.now().isoformat(),
        }
        cols_sql = ', '.join(cols.keys())
        placeholders = ', '.join(['?'] * len(cols))
        conn.execute(
            f"INSERT INTO rfi_log ({cols_sql}) VALUES ({placeholders})",
            list(cols.values()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM rfi_log WHERE rfi_number = ?", (rfi_number,)).fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Insert failed"}), 500
        return response_wrapper(_rfi_row_to_dict(row, today_iso)), 201
    except Exception as e:
        logging.error(f"POST /api/rfis: {str(e)}")
        return _server_error(e)


@app.route('/api/rfis/<rfi_id>', methods=['PATCH'])
def update_rfi(rfi_id):
    """Patch an RFI by rfi_number. Accepts any subset of _RFI_WRITABLE_FIELDS.
    Mirrors spec names into legacy columns so concurrent readers stay
    consistent during the Phase-2 rollout window."""
    from project_type_config import RFI_STATUS_SUBSET
    try:
        data = request.get_json(silent=True) or {}
        # Whitelist + boolean coercion
        updates, params = [], []
        for key, value in data.items():
            if key not in _RFI_WRITABLE_FIELDS:
                continue
            if key == 'status' and value not in RFI_STATUS_SUBSET:
                return jsonify({
                    "error": f"status must be one of {RFI_STATUS_SUBSET}"
                }), 400
            if key in ('schedule_impact_flag', 'cost_impact_flag'):
                value = _coerce_bool(value)
            updates.append(f"{key} = ?")
            params.append(value)
            # Mirror to legacy column
            if key in _RFI_LEGACY_MIRROR:
                updates.append(f"{_RFI_LEGACY_MIRROR[key]} = ?")
                params.append(value)
        if not updates:
            return jsonify({"error": "no writable fields in payload"}), 400
        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(rfi_id)
        conn = db()
        try:
            row_before = conn.execute(
                "SELECT 1 FROM rfi_log WHERE rfi_number = ?", (rfi_id,)
            ).fetchone()
            if not row_before:
                return jsonify({"error": "RFI not found"}), 404
            conn.execute(
                f"UPDATE rfi_log SET {', '.join(updates)} WHERE rfi_number = ?",
                params,
            )
            conn.commit()
            row = conn.execute("SELECT * FROM rfi_log WHERE rfi_number = ?", (rfi_id,)).fetchone()
        finally:
            conn.close()
        return response_wrapper(_rfi_row_to_dict(row, date.today().isoformat()))
    except Exception as e:
        logging.error(f"PATCH /api/rfis/{rfi_id}: {str(e)}")
        return _server_error(e)


@app.route('/api/rfis/<rfi_id>/render', methods=['GET'])
def render_rfi(rfi_id):
    """Render the formal RFI doc (Phase 2E) in the DCR design language.

    Returns the standalone HTML for the operator to send to the architect/
    EOR/owner's rep. Same print contract as the DCR — @page Letter @
    0.4in margins, sections never split, headings never strand. Browser
    Ctrl+P -> Save as PDF gives a clean, paginated multi-page document.
    """
    from render_rfi_html import render_rfi_html
    try:
        conn = db()
        try:
            row = conn.execute(
                "SELECT * FROM rfi_log WHERE rfi_number = ?", (rfi_id,)
            ).fetchone()
            if not row:
                conn.close()
                return jsonify({"error": "RFI not found"}), 404
            rfi = _rfi_row_to_dict(row, date.today().isoformat())
            project_row = conn.execute(
                "SELECT * FROM projects WHERE project_code = ?",
                (rfi.get('project_code'),),
            ).fetchone()
            project = dict(project_row) if project_row else {}
            if project:   # #357 — the header prints the live people, not the dead columns
                import project_contacts
                people = project_contacts.project_people(conn, project["project_code"])
                project["client"] = people["owner_client"] or project.get("client")
                project["superintendent"] = people["superintendent"] or project.get("superintendent")
        finally:
            conn.close()
        html = render_rfi_html(rfi, project)
        from flask import Response
        return Response(html, mimetype='text/html; charset=utf-8')
    except Exception as e:
        logging.error(f"GET /api/rfis/{rfi_id}/render: {str(e)}")
        return _server_error(e)


@app.route('/api/rfis/<rfi_id>', methods=['DELETE'])
def delete_rfi(rfi_id):
    """Hard-delete an RFI by rfi_number. Used by the register's Delete
    affordance and by the smoke test."""
    try:
        conn = db()
        try:
            row = conn.execute("SELECT 1 FROM rfi_log WHERE rfi_number = ?", (rfi_id,)).fetchone()
            if not row:
                return jsonify({"error": "RFI not found"}), 404
            conn.execute("DELETE FROM rfi_log WHERE rfi_number = ?", (rfi_id,))
            conn.commit()
        finally:
            conn.close()
        return response_wrapper({"rfi_number": rfi_id, "deleted": True})
    except Exception as e:
        logging.error(f"DELETE /api/rfis/{rfi_id}: {str(e)}")
        return _server_error(e)

# ============= SITE CLOSURE ENDPOINTS =============

SITE_CLOSURE_CHECKLIST_KEYS = (
    'personnel_all_signed_out', 'personnel_visitors_escorted', 'personnel_no_remaining',
    'equipment_tools_secured', 'equipment_scaffold_locked', 'equipment_compressors_secured',
    'equipment_mast_climbers_locked', 'fire_watch_completed', 'fire_heat_sources_cool',
    'fire_extinguishers_ready', 'dust_collection_sealed', 'dust_storage_sealed',
    'dust_tarps_secure', 'water_penetrations_covered', 'water_window_protection',
    'security_access_locked', 'security_fence_secured', 'security_sidewalk_clear',
    'building_doors_locked', 'building_roof_locked', 'climate_hvac_undisturbed',
    'climate_storage_sealed', 'doc_daily_report', 'doc_photos_taken',
)

@app.route('/api/site-closures', methods=['POST'])
@requires_section('internal_ops')   # #305
def create_site_closure():
    """Create new site closure record"""
    try:
        data = request.get_json()
        project_code = data.get('project_code')
        
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 400
        
        closure_id = f"SC-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        # Build checklist values
        checklist = data.get('checklist', {})
        cols = ['closure_id', 'date', 'project_code', 'foreman_id', 'time_of_close', 'weather_at_close',
                'equipment_left_overnight', 'created_at', 'updated_at']
        vals = [closure_id, data.get('date'), project_code, data.get('foreman_id'),
                data.get('time_of_close'), data.get('weather_at_close'),
                data.get('equipment_left_overnight'), datetime.now().isoformat(), datetime.now().isoformat()]
        
        # Add checklist items
        checklist_keys = [
            'personnel_all_signed_out', 'personnel_visitors_escorted', 'personnel_no_remaining',
            'equipment_tools_secured', 'equipment_scaffold_locked', 'equipment_compressors_secured',
            'equipment_mast_climbers_locked', 'fire_watch_completed', 'fire_heat_sources_cool',
            'fire_extinguishers_ready', 'dust_collection_sealed', 'dust_storage_sealed',
            'dust_tarps_secure', 'water_penetrations_covered', 'water_window_protection',
            'security_access_locked', 'security_fence_secured', 'security_sidewalk_clear',
            'building_doors_locked', 'building_roof_locked', 'climate_hvac_undisturbed',
            'climate_storage_sealed', 'doc_daily_report', 'doc_photos_taken'
        ]
        
        for key in checklist_keys:
            cols.append(key)
            vals.append(checklist.get(key, 'N'))
        
        placeholders = ', '.join(['?' for _ in vals])
        conn.execute(
            f"INSERT INTO site_closure_log ({', '.join(cols)}) VALUES ({placeholders})",
            vals
        )
        conn.commit()
        
        row = conn.execute("SELECT * FROM site_closure_log WHERE closure_id = ?", (closure_id,)).fetchone()
        conn.close()
        
        return response_wrapper(dict(row) if row else {"closure_id": closure_id}), 201
    except Exception as e:
        logging.error(f"POST /api/site-closures: {str(e)}")
        return _server_error(e)

@app.route('/api/site-closures/<closure_id>/checklist', methods=['PATCH'])
@requires_section('internal_ops')   # #305
def update_closure_checklist(closure_id):
    """Update single checklist item"""
    try:
        data = request.get_json(silent=True) or {}
        item = data.get('item')
        value = data.get('value')
        # #305 — the column name came straight from the request body into the SQL
        # (injection). Allowlist it; the value is a Y/N flag.
        if item not in SITE_CLOSURE_CHECKLIST_KEYS:
            return jsonify({"error": "unknown checklist item"}), 400
        if value not in ('Y', 'N'):
            return jsonify({"error": "value must be 'Y' or 'N'"}), 400
        
        conn = db()
        conn.execute(
            f"UPDATE site_closure_log SET {item} = ?, updated_at = ? WHERE closure_id = ?",
            (value, datetime.now().isoformat(), closure_id)
        )
        conn.commit()
        conn.close()
        
        return response_wrapper({"item": item, "value": value}), 200
    except Exception as e:
        return _server_error(e)

# ============= DCR SITE ACTIVITY ENDPOINTS =============

def _parse_dcr_date(date_str):
    """Validate YYYY-MM-DD or return today's date if not supplied. Raises ValueError on bad format."""
    if not date_str:
        return date.today().isoformat()
    datetime.strptime(date_str, '%Y-%m-%d')
    return date_str


@app.route('/api/work-log', methods=['POST'])
def create_work_log():
    """Log work performed for a project on a given date."""
    try:
        data = request.get_json() or {}
        project_code = data.get('project_code')
        if not project_code:
            return jsonify({"error": "project_code required"}), 400
        try:
            date_str = _parse_dcr_date(data.get('date'))
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 400
        conn.execute(
            "INSERT INTO work_log (date, project_code, trade_area, location_elevation, "
            "description, scope_of_work, trades_working) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date_str, project_code, data.get('trade_area'), data.get('location_elevation'),
             data.get('description'), data.get('scope_of_work'), data.get('trades_working'))
        )
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
        row = conn.execute("SELECT * FROM work_log WHERE id = ?", (new_id,)).fetchone()
        conn.close()
        return response_wrapper(dict(row)), 201
    except Exception as e:
        logging.error(f"POST /api/work-log: {str(e)}")
        return _server_error(e)


@app.route('/api/deliveries', methods=['POST'])
def create_delivery():
    """Log a material delivery."""
    try:
        data = request.get_json() or {}
        project_code = data.get('project_code')
        if not project_code:
            return jsonify({"error": "project_code required"}), 400
        try:
            date_str = _parse_dcr_date(data.get('date'))
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 400
        conn.execute(
            "INSERT INTO deliveries (date, project_code, time, material, qty, unit, "
            "supplier, notes, description, delivered_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date_str, project_code, data.get('time'), data.get('material'),
             data.get('qty'), data.get('unit'), data.get('supplier'), data.get('notes'),
             data.get('description'), data.get('delivered_by'))
        )
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
        row = conn.execute("SELECT * FROM deliveries WHERE id = ?", (new_id,)).fetchone()
        conn.close()
        return response_wrapper(dict(row)), 201
    except Exception as e:
        logging.error(f"POST /api/deliveries: {str(e)}")
        return _server_error(e)


@app.route('/api/equipment-log', methods=['POST'])
def create_equipment_log():
    """Log equipment on site for a given day. Renderer uses `equipment` as display
    name; accepted as alias for the schema's equipment_type column."""
    try:
        data = request.get_json() or {}
        project_code = data.get('project_code')
        if not project_code:
            return jsonify({"error": "project_code required"}), 400
        try:
            date_str = _parse_dcr_date(data.get('date'))
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        equipment_type = data.get('equipment_type') or data.get('equipment')
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 400
        conn.execute(
            "INSERT INTO equipment_log (date, project_code, equipment_type, equipment_id, "
            "owner, hours_used, issues, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date_str, project_code, equipment_type, data.get('equipment_id'),
             data.get('owner'), data.get('hours_used'), data.get('issues'),
             data.get('status'), data.get('notes'))
        )
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
        row = conn.execute("SELECT * FROM equipment_log WHERE id = ?", (new_id,)).fetchone()
        conn.close()
        return response_wrapper(dict(row)), 201
    except Exception as e:
        logging.error(f"POST /api/equipment-log: {str(e)}")
        return _server_error(e)


@app.route('/api/weather-log', methods=['POST'])
def create_weather_log():
    """Log site weather for a given date. Operator-entered values override
    the live Open-Meteo fallback at DCR aggregation time."""
    try:
        data = request.get_json() or {}
        project_code = data.get('project_code')
        if not project_code:
            return jsonify({"error": "project_code required"}), 400
        try:
            date_str = _parse_dcr_date(data.get('date'))
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 400
        conn.execute(
            "INSERT INTO weather_log (date, project_code, am_temp_f, pm_temp_f, "
            "am_conditions, pm_conditions, wind, conditions) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (date_str, project_code, data.get('am_temp_f'), data.get('pm_temp_f'),
             data.get('am_conditions'), data.get('pm_conditions'),
             data.get('wind'), data.get('conditions'))
        )
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
        row = conn.execute("SELECT * FROM weather_log WHERE id = ?", (new_id,)).fetchone()
        conn.close()
        return response_wrapper(dict(row)), 201
    except Exception as e:
        logging.error(f"POST /api/weather-log: {str(e)}")
        return _server_error(e)


# ============= DCR SAFETY + COMPLIANCE ENDPOINTS =============

@app.route('/api/toolbox-talks/records', methods=['POST'])
def create_toolbox_talk_record():
    """Log a toolbox-talk occurrence. `topic` is resolved from toolbox_talk_library
    via talk_id at render time, so a `topic` field in the body is accepted but not
    persisted. `conducted_by` is accepted as a synonym for `facilitator`."""
    try:
        data = request.get_json() or {}
        project_code = data.get('project_code')
        if not project_code:
            return jsonify({"error": "project_code required"}), 400
        try:
            date_str = _parse_dcr_date(data.get('date'))
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        facilitator = data.get('facilitator') or data.get('conducted_by')
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 400
        conn.execute(
            "INSERT INTO toolbox_talk_records (date, project_code, talk_id, "
            "facilitator, attendees, duration_minutes) VALUES (?, ?, ?, ?, ?, ?)",
            (date_str, project_code, data.get('talk_id'), facilitator,
             data.get('attendees'), data.get('duration_minutes'))
        )
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
        row = conn.execute("SELECT * FROM toolbox_talk_records WHERE id = ?", (new_id,)).fetchone()
        conn.close()
        return response_wrapper(dict(row)), 201
    except Exception as e:
        logging.error(f"POST /api/toolbox-talks/records: {str(e)}")
        return _server_error(e)


@app.route('/api/safety-events', methods=['POST'])
def create_safety_event():
    """Log a safety event (incident, near-miss, observation)."""
    try:
        data = request.get_json() or {}
        project_code = data.get('project_code')
        if not project_code:
            return jsonify({"error": "project_code required"}), 400
        try:
            date_str = _parse_dcr_date(data.get('date'))
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 400
        conn.execute(
            "INSERT INTO safety_events (date, project_code, event_type, severity, "
            "time, person, description, action, reported_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date_str, project_code, data.get('event_type'), data.get('severity'),
             data.get('time'), data.get('person'), data.get('description'),
             data.get('action'), data.get('reported_by'))
        )
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
        row = conn.execute("SELECT * FROM safety_events WHERE id = ?", (new_id,)).fetchone()
        conn.close()
        return response_wrapper(dict(row)), 201
    except Exception as e:
        logging.error(f"POST /api/safety-events: {str(e)}")
        return _server_error(e)


@app.route('/api/issues', methods=['POST'])
def create_issue():
    """Log a project issue / delay."""
    try:
        data = request.get_json() or {}
        project_code = data.get('project_code')
        if not project_code:
            return jsonify({"error": "project_code required"}), 400
        try:
            date_str = _parse_dcr_date(data.get('date'))
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        due_date = data.get('due_date')
        if due_date:
            try:
                datetime.strptime(due_date, '%Y-%m-%d')
            except ValueError:
                return jsonify({"error": "due_date must be YYYY-MM-DD"}), 400
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 400
        conn.execute(
            "INSERT INTO issues (date, project_code, category, description, "
            "time_lost_hrs, action, owner, status, due_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date_str, project_code, data.get('category'), data.get('description'),
             data.get('time_lost_hrs'), data.get('action'), data.get('owner'),
             data.get('status'), due_date)
        )
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
        row = conn.execute("SELECT * FROM issues WHERE id = ?", (new_id,)).fetchone()
        conn.close()
        return response_wrapper(dict(row)), 201
    except Exception as e:
        logging.error(f"POST /api/issues: {str(e)}")
        return _server_error(e)


@app.route('/api/inspections', methods=['POST'])
def create_inspection():
    """Log an inspection (DOB, 3rd party, internal QC)."""
    try:
        data = request.get_json() or {}
        project_code = data.get('project_code')
        if not project_code:
            return jsonify({"error": "project_code required"}), 400
        try:
            date_str = _parse_dcr_date(data.get('date'))
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 400
        conn.execute(
            "INSERT INTO inspections (date, project_code, type, inspector_name, "
            "agency, area, result, notes, scope) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date_str, project_code, data.get('type'), data.get('inspector_name'),
             data.get('agency'), data.get('area'), data.get('result'),
             data.get('notes'), data.get('scope'))
        )
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
        row = conn.execute("SELECT * FROM inspections WHERE id = ?", (new_id,)).fetchone()
        conn.close()
        return response_wrapper(dict(row)), 201
    except Exception as e:
        logging.error(f"POST /api/inspections: {str(e)}")
        return _server_error(e)


# ============= DCR VISITORS =============

@app.route('/api/visitors', methods=['POST'])
def create_visitor():
    """Log a site visitor. Powers the DCR visitors section."""
    try:
        data = request.get_json() or {}
        project_code = data.get('project_code')
        if not project_code:
            return jsonify({"error": "project_code required"}), 400
        try:
            date_str = _parse_dcr_date(data.get('date'))
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 400
        conn.execute(
            "INSERT INTO visitors (date, project_code, name, company, role, "
            "time_in, time_out, purpose, accompanied_by, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date_str, project_code, data.get('name'), data.get('company'),
             data.get('role'), data.get('time_in'), data.get('time_out'),
             data.get('purpose'), data.get('accompanied_by'), data.get('notes'))
        )
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
        row = conn.execute("SELECT * FROM visitors WHERE id = ?", (new_id,)).fetchone()
        conn.close()
        return response_wrapper(dict(row)), 201
    except Exception as e:
        logging.error(f"POST /api/visitors: {str(e)}")
        return _server_error(e)


# ============= DELETE endpoints for DCR entry rows + reports =============

def _delete_entry_row(table, row_id):
    """Generic single-row delete by integer id. Parameterized SQL. Returns
    Flask response tuple. 404 if no row matched, 200 otherwise."""
    conn = db()
    try:
        existing = conn.execute(f"SELECT id FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if not existing:
            return jsonify({"error": "Not found"}), 404
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        conn.commit()
        return jsonify({"deleted": True, "id": row_id, "table": table}), 200
    except Exception as e:
        logging.error(f"DELETE {table}/{row_id}: {str(e)}")
        return _server_error(e)
    finally:
        conn.close()


@app.route('/api/work-log/<int:row_id>', methods=['DELETE'])
def delete_work_log(row_id):
    return _delete_entry_row('work_log', row_id)


@app.route('/api/deliveries/<int:row_id>', methods=['DELETE'])
def delete_delivery(row_id):
    return _delete_entry_row('deliveries', row_id)


@app.route('/api/equipment-log/<int:row_id>', methods=['DELETE'])
def delete_equipment_log(row_id):
    return _delete_entry_row('equipment_log', row_id)


@app.route('/api/weather-log/<int:row_id>', methods=['DELETE'])
def delete_weather_log(row_id):
    return _delete_entry_row('weather_log', row_id)


@app.route('/api/toolbox-talks/records/<int:row_id>', methods=['DELETE'])
def delete_toolbox_talk_record(row_id):
    return _delete_entry_row('toolbox_talk_records', row_id)


@app.route('/api/safety-events/<int:row_id>', methods=['DELETE'])
def delete_safety_event(row_id):
    return _delete_entry_row('safety_events', row_id)


@app.route('/api/issues/<int:row_id>', methods=['DELETE'])
def delete_issue(row_id):
    return _delete_entry_row('issues', row_id)


@app.route('/api/inspections/<int:row_id>', methods=['DELETE'])
def delete_inspection(row_id):
    return _delete_entry_row('inspections', row_id)


@app.route('/api/visitors/<int:row_id>', methods=['DELETE'])
def delete_visitor(row_id):
    return _delete_entry_row('visitors', row_id)


@app.route('/api/photos/<int:row_id>', methods=['DELETE'])
def delete_photo(row_id):
    """Delete photo row + file on disk. DB delete succeeds even if file
    delete fails (orphan file is better than orphan DB row)."""
    conn = db()
    try:
        row = conn.execute("SELECT file_path FROM photos WHERE id = ?", (row_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        file_path = row['file_path']
        conn.execute("DELETE FROM photos WHERE id = ?", (row_id,))
        conn.commit()
    except Exception as e:
        logging.error(f"DELETE /api/photos/{row_id}: {str(e)}")
        return _server_error(e)
    finally:
        conn.close()
    file_deleted = False
    file_error = None
    if file_path:
        try:
            p = ssc_paths.resolve_data_path(file_path)   # #287
            if p.exists():
                p.unlink()
                file_deleted = True
        except Exception as e:
            file_error = str(e)
            logging.warning(f"Photo file delete failed for id={row_id}: {file_error}")
    return jsonify({
        "deleted": True, "id": row_id, "file_deleted": file_deleted,
        "file_error": file_error,
    }), 200


# ============= PATCH endpoints for DCR entry rows =============

def _patch_dcr_row(table, row_id, allowed_fields, aliases=None):
    """Generic PATCH for a DCR sub-section row.

    Updates only the columns named in allowed_fields. Aliases is an optional
    {alias: real_column} map for POST-compat field aliases (e.g. 'equipment'
    → 'equipment_type' on equipment_log). 404 if no row matched; 400 if no
    updatable fields were supplied. `date` and `project_code` are NOT
    mutable here — re-creating a row is the operator path for moving it
    across days or projects (the semantics of every DCR sub-section are
    'this happened on this date for this project')."""
    data = request.get_json(silent=True) or {}
    updates = {}
    for k in allowed_fields:
        if k in data:
            updates[k] = data[k]
    if aliases:
        for alias, real in aliases.items():
            if alias in data and real not in updates:
                updates[real] = data[alias]
    if not updates:
        return jsonify({"error": "no updatable fields in payload"}), 400
    conn = db()
    try:
        existing = conn.execute(
            f"SELECT id FROM {table} WHERE id = ?", (row_id,)
        ).fetchone()
        if not existing:
            return jsonify({"error": "not found"}), 404
        set_clauses = [f"{k} = ?" for k in updates] + ["updated_at = CURRENT_TIMESTAMP"]
        params = list(updates.values()) + [row_id]
        conn.execute(
            f"UPDATE {table} SET {', '.join(set_clauses)} WHERE id = ?",
            params
        )
        conn.commit()
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        # #247 — generic SELECT * (e.g. photos.file_path); scrub before the wire.
        return response_wrapper(scrub_paths(dict(row))), 200
    except Exception as e:
        logging.error(f"PATCH /api/{table}/{row_id}: {e}")
        return _server_error(e)
    finally:
        conn.close()


@app.route('/api/work-log/<int:row_id>', methods=['PATCH'])
def patch_work_log(row_id):
    return _patch_dcr_row('work_log', row_id, {
        'trade_area', 'location_elevation', 'description',
        'scope_of_work', 'trades_working',
    })


@app.route('/api/deliveries/<int:row_id>', methods=['PATCH'])
def patch_delivery(row_id):
    return _patch_dcr_row('deliveries', row_id, {
        'time', 'material', 'qty', 'unit', 'supplier', 'notes',
        'description', 'delivered_by',
    })


@app.route('/api/equipment-log/<int:row_id>', methods=['PATCH'])
def patch_equipment_log(row_id):
    return _patch_dcr_row('equipment_log', row_id, {
        'equipment_type', 'equipment_id', 'owner', 'hours_used',
        'issues', 'status', 'notes',
    }, aliases={'equipment': 'equipment_type'})


@app.route('/api/weather-log/<int:row_id>', methods=['PATCH'])
def patch_weather_log(row_id):
    return _patch_dcr_row('weather_log', row_id, {
        'am_temp_f', 'pm_temp_f', 'am_conditions', 'pm_conditions',
        'wind', 'conditions',
    })


@app.route('/api/toolbox-talks/records/<int:row_id>', methods=['PATCH'])
def patch_toolbox_talk_record(row_id):
    return _patch_dcr_row('toolbox_talk_records', row_id, {
        'talk_id', 'facilitator', 'attendees', 'duration_minutes',
    }, aliases={'conducted_by': 'facilitator'})


@app.route('/api/safety-events/<int:row_id>', methods=['PATCH'])
def patch_safety_event(row_id):
    return _patch_dcr_row('safety_events', row_id, {
        'event_type', 'severity', 'time', 'person', 'description',
        'action', 'reported_by',
    })


@app.route('/api/issues/<int:row_id>', methods=['PATCH'])
def patch_issue(row_id):
    # Mirror POST due_date validation so PATCH can't sneak in a malformed value.
    data = request.get_json(silent=True) or {}
    if 'due_date' in data and data['due_date']:
        try:
            datetime.strptime(data['due_date'], '%Y-%m-%d')
        except ValueError:
            return jsonify({"error": "due_date must be YYYY-MM-DD"}), 400
    return _patch_dcr_row('issues', row_id, {
        'category', 'description', 'time_lost_hrs', 'action',
        'owner', 'status', 'due_date',
    })


@app.route('/api/inspections/<int:row_id>', methods=['PATCH'])
def patch_inspection(row_id):
    return _patch_dcr_row('inspections', row_id, {
        'type', 'inspector_name', 'agency', 'area', 'result',
        'notes', 'scope',
    })


@app.route('/api/visitors/<int:row_id>', methods=['PATCH'])
def patch_visitor(row_id):
    return _patch_dcr_row('visitors', row_id, {
        'name', 'company', 'role', 'time_in', 'time_out',
        'purpose', 'accompanied_by', 'notes',
    })


@app.route('/api/photos/<int:row_id>', methods=['PATCH'])
def patch_photo(row_id):
    # Photo file metadata only — file_path / filename / url / date /
    # project_code are NOT mutable here. Re-uploading is the path for
    # changing the file itself.
    return _patch_dcr_row('photos', row_id, {
        'location', 'description', 'uploaded_by',
    })


def _dcr_backup_name(project_code, seq, what):
    return f"{datetime.now():%Y%m%d-%H%M%S}-{what}-{project_code}-{seq:03d}"


def _retire_dcr_files(project_code, seq, audiences, backup_name):
    """#344 — MOVE a DCR's files (HTML per audience + the internal PDF) into
    data_room/_maintenance_backup/<backup_name>/ instead of unlinking them, then
    drop the number's folder once it is empty. The old delete removed only the
    HTML, leaving internal.pdf behind — the folder never went away and a later
    report on that number would sit next to a stale PDF. _maintenance_backup is
    outside _ARTIFACT_SUBTREES, so nothing in it is servable. Best-effort per file;
    returns (moved_names, errors, backup_rel)."""
    seq_dir = ssc_paths.under_root("data_room", "reports", "dcr", project_code, f"{seq:03d}")
    backup = ssc_paths.under_root("data_room", "_maintenance_backup", backup_name)
    names = [f"{a}.html" for a in audiences] + (["internal.pdf"] if 'internal' in audiences else [])
    moved, errors = [], {}
    for name in names:
        src = seq_dir / name
        try:
            if src.exists():
                backup.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(backup / name))
                moved.append(name)
        except Exception as e:
            errors[name] = str(e)
            logging.warning(f"DCR file retire failed for {project_code} seq={seq} {name}: {e}")
    try:
        if seq_dir.exists() and not any(seq_dir.iterdir()):
            seq_dir.rmdir()
    except Exception:
        pass
    backup_rel = backup.relative_to(ssc_paths.data_root()).as_posix() if moved else None
    return moved, errors, backup_rel


@app.route('/api/projects/<project_code>/reports/by-sequence/<int:seq>', methods=['DELETE'])
def delete_report_by_sequence(project_code, seq):
    """Atomic DCR delete: removes BOTH internal and client report_index rows for
    (project_code, dcr_sequence=seq) plus the report's files. Frontend Delete button
    uses this so the operator's 'Delete' action removes the entire report (the
    legacy per-report_id route is retained below for low-level use). 404 if no
    rows match. DB deletes are transactional; file moves are best-effort.
    #344 — files (both HTML + the PDF) move to a non-served backup folder, the
    number's folder is removed, and an audit row records what was deleted."""
    display_id = f"DCR-{project_code}-{seq:03d}"
    backup_name = _dcr_backup_name(project_code, seq, "dcr-delete")
    conn = db()
    try:
        rows = conn.execute(
            "SELECT * FROM report_index "
            "WHERE project_code = ? AND dcr_sequence = ? AND report_type = 'DCR'",
            (project_code, seq)
        ).fetchall()
        if not rows:
            return jsonify({"error": "Not found"}), 404
        before = [dict(r) for r in rows]
        deleted_report_ids = [r['report_id'] for r in before]
        conn.execute(
            "DELETE FROM report_index "
            "WHERE project_code = ? AND dcr_sequence = ? AND report_type = 'DCR'",
            (project_code, seq)
        )
        dcr_maintenance.audit(conn, 'dcr_delete', display_id, before=before,
                   note=f"files moved to data_room/_maintenance_backup/{backup_name}")
        conn.commit()
    except Exception as e:
        logging.error(f"DELETE /api/projects/{project_code}/reports/by-sequence/{seq}: {str(e)}")
        return _server_error(e)
    finally:
        conn.close()

    moved, file_errors, _ = _retire_dcr_files(
        project_code, seq, ('internal', 'client'), backup_name)
    return jsonify({
        "deleted": True,
        "project_code": project_code,
        "sequence": seq,
        "report_ids_deleted": deleted_report_ids,
        "files_deleted": moved,
        "file_errors": file_errors or None,
    }), 200


@app.route('/api/projects/<project_code>/reports/<report_id>', methods=['DELETE'])
def delete_report(project_code, report_id):
    """Delete a single report_index row + its rendered HTML file (sequence-based
    path). One row per audience: deleting the internal variant leaves the client
    variant alone, and vice versa. DB delete succeeds even if file delete fails."""
    audience = None
    for suf in ('internal', 'client'):
        if report_id.endswith(f'-{suf}'):
            audience = suf
            break
    conn = db()
    try:
        row = conn.execute(
            "SELECT * FROM report_index WHERE project_code = ? AND report_id = ?",
            (project_code, report_id)
        ).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        before = dict(row)
        seq = before['dcr_sequence']
        backup_name = _dcr_backup_name(project_code, seq or 0, f"dcr-delete-{audience or 'row'}")
        conn.execute(
            "DELETE FROM report_index WHERE project_code = ? AND report_id = ?",
            (project_code, report_id)
        )
        # #344 — audit + move (not unlink) the audience's files, incl. the PDF
        dcr_maintenance.audit(conn, 'dcr_delete', _display_id(report_id), before=before,
                   note=f"{audience or 'row'} only; files moved to data_room/_maintenance_backup/{backup_name}")
        conn.commit()
    except Exception as e:
        logging.error(f"DELETE /api/projects/{project_code}/reports/{report_id}: {str(e)}")
        return _server_error(e)
    finally:
        conn.close()
    file_deleted = False
    file_error = None
    if seq is not None and audience:
        moved, errors, _ = _retire_dcr_files(project_code, seq, (audience,), backup_name)
        file_deleted = f"{audience}.html" in moved
        file_error = errors.get(f"{audience}.html")
    return jsonify({
        "deleted": True, "report_id": report_id, "file_deleted": file_deleted,
        "file_error": file_error,
    }), 200


# ============= DCR AGGREGATOR =============

@app.route('/api/projects/<project_code>/daily/<report_date>', methods=['GET'])
def get_dcr_daily(project_code, report_date):
    """Return the full DCR JSON for (project_code, report_date) at the
    requested audience. ?audience=internal (default) or ?audience=client."""
    audience = request.args.get('audience', 'internal')
    if audience not in ('internal', 'client'):
        return jsonify({"error": "audience must be 'internal' or 'client'"}), 400
    try:
        datetime.strptime(report_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    conn = db()
    if not validate_project_exists(conn, project_code):
        conn.close()
        return jsonify({"error": "Project not found"}), 400
    conn.close()
    try:
        from dcr_aggregator import aggregate_dcr  # lazy to avoid circular import
        dcr = aggregate_dcr(project_code, report_date, audience)
        # Surface No-Work Day + stale flags so the entry view can show
        # the banner state on load. Both are report-level attributes
        # not part of the aggregated DCR section data.
        conn = db()
        nw_row = conn.execute(
            "SELECT no_work, no_work_reason, no_work_note FROM report_index "
            "WHERE project_code = ? AND report_date = ? AND report_type='DCR' "
            "AND no_work = 1 LIMIT 1",
            (project_code, report_date),
        ).fetchone()
        stale_row = conn.execute(
            "SELECT MAX(stale) AS stale, MAX(stale_marked_at) AS stale_marked_at "
            "FROM report_index "
            "WHERE project_code = ? AND report_date = ? AND report_type='DCR' "
            "AND status = 'issued'",
            (project_code, report_date),
        ).fetchone()
        conn.close()
        if nw_row:
            dcr['no_work'] = 1
            dcr['no_work_reason'] = nw_row['no_work_reason']
            dcr['no_work_note'] = nw_row['no_work_note']
        else:
            dcr['no_work'] = 0
        if stale_row and stale_row['stale']:
            dcr['stale'] = 1
            dcr['stale_marked_at'] = stale_row['stale_marked_at']
        else:
            dcr['stale'] = 0
        # #295 — the entry view warns BEFORE an hours edit on an issued day
        # (the correction is allowed; the warning says it will be recorded
        # and the report re-rendered). stale_row's WHERE already scopes to
        # status='issued'; surface existence separately from staleness.
        conn = db()
        issued_row = conn.execute(
            "SELECT 1 FROM report_index WHERE project_code = ? AND report_date = ? "
            "AND UPPER(report_type)='DCR' AND status = 'issued' LIMIT 1",
            (project_code, report_date)).fetchone()
        conn.close()
        dcr['issued'] = 1 if issued_row else 0
        return response_wrapper(dcr)
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logging.error(f"GET /api/projects/{project_code}/daily/{report_date}: {str(e)}")
        return _server_error(e)


def _mark_dcr_stale(conn, project_code, report_date):
    """Mark any issued DCR for (project_code, report_date) as stale.

    Called from every sign_in_log mutation path. The rendered HTML/PDF
    on disk was frozen at issue time; once labor changes, the artifact
    no longer matches live data — flag it so the UI prompts re-issue.

    No-op when the project_code/date pair has no issued DCR (operator
    edited labor on a day that was never issued — nothing to mark).
    Re-running on an already-stale row is also a no-op (the WHERE
    clause matches but the UPDATE is idempotent).

    #292 — this is ALSO the labor-cost invalidation choke-point: every
    sign_in_log mutation path already routes here by doctrine, so the
    memoized labor engine (ssc_memo 'labor_cost') is bumped exactly where
    labor truth changes. A future sign-in write that skips this helper
    breaks the stale-flag AND the memo guard — two red gates, not one.
    """
    if not project_code or not report_date:
        return
    import ssc_memo
    ssc_memo.bump('labor_cost', project_code)
    conn.execute(
        "UPDATE report_index SET stale = 1, stale_marked_at = CURRENT_TIMESTAMP "
        "WHERE project_code = ? AND report_date = ? "
        "AND report_type = 'DCR' AND status = 'issued' "
        "AND (stale IS NULL OR stale = 0)",
        (project_code, report_date),
    )


def _record_sign_in_edit(conn, before, action, to_in, to_out, reason):
    """#295 — the labor paper trail (AMEND-NEVER-ERASE, the #294 posture).
    One history row per operator correction through the sign-in doors:
    action 'edit' (from -> to times) or 'remove' (to_* NULL). `before` is the
    sign_in_log row BEFORE the mutation. Returns the history row id so the
    caller can mark dcr_amended once the issued-day check runs. Worker-app
    self sign-in/out never routes here — the original record is not an edit."""
    try:
        from auth import current_user
        user = current_user() or {}
    except Exception:
        user = {}
    cur = conn.execute(
        "INSERT INTO sign_in_edit (sign_in_id, employee_id, project_code, date, "
        "action, from_time_in, from_time_out, to_time_in, to_time_out, "
        "actor_uid, actor_role, reason, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (before["id"], before["employee_id"], before["project_code"], before["date"],
         action, before["time_in"], before["time_out"], to_in, to_out,
         user.get("id"), user.get("role"), (reason or None),
         datetime.now().isoformat()))
    return cur.lastrowid


def _amend_issued_dcr_hours(conn, project_code, report_date, edit_id, note):
    """#295 — when the corrected day has an ISSUED DCR: set the honest marker
    (report_index.hours_amended + at), write the audit row, mark the history
    row dcr_amended, and return {sequence, audiences, display_id} so the
    caller can re-render. Returns None when the day has no issued DCR.
    Callable transaction-side with no Flask context (tests, CLI)."""
    issued = conn.execute(
        "SELECT id, report_id, dcr_sequence FROM report_index "
        "WHERE project_code = ? AND report_date = ? "
        "AND UPPER(report_type) = 'DCR' AND status = 'issued'",
        (project_code, report_date)).fetchall()
    if not issued:
        return None
    now = datetime.now().isoformat()
    # The note names the project + day — an audit row must be attributable
    # without joining anything (and synthetic-project rows self-identify).
    note = f"{project_code} {report_date}: {note}"
    hist = conn.execute("SELECT actor_uid, actor_role FROM sign_in_edit WHERE id = ?",
                        (edit_id,)).fetchone()
    audiences = []
    seq = None
    for d in issued:
        if d["dcr_sequence"] is not None:
            seq = d["dcr_sequence"]
        rid = str(d["report_id"] or "")
        if rid.endswith("-internal"):
            audiences.append("internal")
        elif rid.endswith("-client"):
            audiences.append("client")
        conn.execute(
            "UPDATE report_index SET hours_amended=1, "
            "hours_amended_at=COALESCE(hours_amended_at, ?) WHERE id=?",
            (now, d["id"]))
        conn.execute(
            "INSERT INTO audit_log (action, actor_user_id, actor_role, target_type, "
            "target_id, before_json, after_json, note, created_at) "
            "VALUES ('hours_edit_after_issue', ?,?,?,?,?,?,?,?)",
            ((hist["actor_uid"] if hist else None), (hist["actor_role"] if hist else None),
             "report_index", str(d["id"]),
             json.dumps({"sign_in_edit_id": edit_id}), None, note, now))
    conn.execute("UPDATE sign_in_edit SET dcr_amended=1 WHERE id=?", (edit_id,))
    display_id = f"DCR-{project_code}-{seq:03d}" if seq is not None else None
    return {"sequence": seq, "audiences": sorted(set(audiences)) or ["internal"],
            "display_id": display_id}


def _rerender_amended_dcr(project_code, report_date, seq, audiences):
    """#295 — regenerate the issued artifact(s) so the served report matches
    the corrected labor. Runs AFTER the edit transaction committed: the DB
    correction is the truth and must never be rolled back because a render
    failed — on failure the day simply stays stale-flagged (the pre-#295
    manual re-issue path). Returns {'ok': bool, 'audiences': [...], 'error'}."""
    if seq is None:
        return {"ok": False, "error": "no sequence on the issued row"}
    conn = db()
    written = []
    try:
        for aud in audiences:
            res = _issue_one_dcr(conn, project_code, report_date, aud, seq)
            written.append(res["html_path"])
        conn.commit()
        return {"ok": True, "audiences": audiences}
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        for f in written:
            try:
                Path(f).unlink()
            except OSError:
                pass
        logging.warning(f"#295 re-render failed for {project_code} {report_date}: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def lookup_existing_dcr_sequence(conn, project_code, report_date):
    """If a DCR already exists for (project_code, report_date) at any audience,
    return its dcr_sequence. Otherwise return None. Used to preserve the seq
    number on re-issue (audiences for the same date share one seq)."""
    row = conn.execute(
        "SELECT dcr_sequence FROM report_index "
        "WHERE project_code = ? AND report_date = ? AND report_type = 'DCR' "
        "AND dcr_sequence IS NOT NULL LIMIT 1",
        (project_code, report_date)
    ).fetchone()
    return row['dcr_sequence'] if row else None


def next_dcr_sequence(conn, project_code):
    """Next DCR sequence = MAX(existing) + 1, numerically. Always.

    A DCR's number is IMMUTABLE once issued (repair pass, decided policy):
    issuing a new report NEVER renumbers an existing one, regardless of date
    order — the number is the report's identity (report_id, artifact dir,
    anything already printed), and identity does not move. A backdated entry
    therefore takes the next number even though its date sorts earlier;
    chronological ordering is a display concern (sort by report_date). A
    deleted report's number stays retired — no gap-fill reuse (same posture
    as the Worker-ID rule)."""
    row = conn.execute(
        "SELECT COALESCE(MAX(dcr_sequence), 0) FROM report_index "
        "WHERE project_code = ? AND report_type = 'DCR' AND dcr_sequence IS NOT NULL",
        (project_code,)
    ).fetchone()
    return int(row[0]) + 1


import re as _re  # local-alias to avoid touching the top-of-file imports


def _issue_one_dcr(conn, project_code, report_date, audience, seq):
    """Aggregate + render + write + upsert for one audience. Returns
    {report_id, audience, html_url, html_path, sequence}. Caller owns the
    connection + commit/close so 'both' can run as a single transaction; the
    caller also owns the html_path so it can roll back orphaned HTML files
    if the transaction fails after the file write. The seq is computed once
    by the caller and shared across audiences.

    HTML is written ATOMICALLY (.tmp then rename) so a concurrent fetch via
    /project-files/ during a re-issue can never catch a half-written file."""
    from dcr_aggregator import aggregate_dcr
    from render_dcr_html import DCRHTMLRenderer
    dcr = aggregate_dcr(project_code, report_date, audience)
    report_id = f"DCR-{project_code}-{seq:03d}-{audience}"
    display_id = f"DCR-{project_code}-{seq:03d}"
    dcr['report_id'] = report_id
    dcr['display_id'] = display_id
    dcr['dcr_sequence'] = seq
    html = DCRHTMLRenderer(dcr).render()
    out_dir = ssc_paths.under_root("data_room", "reports", "dcr", project_code, f"{seq:03d}")   # #287
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{audience}.html"
    tmp_file = out_dir / f"{audience}.html.tmp"
    tmp_file.write_text(html, encoding='utf-8')
    tmp_file.replace(out_file)  # atomic on POSIX and Windows
    rel = out_file.relative_to(ssc_paths.data_root()).as_posix()   # #287 — URL shape unchanged
    html_url = f"/project-files/{rel}"

    # PDF render: internal audience only (the "full record" copy that gets
    # archived). Client audience stays HTML-only — it's the consumer-facing
    # progress report, not the official record. PDF failure does NOT fail
    # the issuance — HTML is the source of truth; the PDF can be regenerated
    # from it via pdf_export.py. Warnings surface in the response so the UI
    # can show "DCR issued, PDF render failed" instead of pretending it's OK.
    pdf_path = None
    pdf_url = None
    pdf_status = None
    drive_status = None  # Drive archive runs only if PDF render succeeded
    if audience == 'internal':
        from pdf_export import render_html_to_pdf, PDFExportError
        pdf_target = out_dir / f"{audience}.pdf"
        try:
            pdf_status = render_html_to_pdf(out_file, pdf_target)
        except PDFExportError as e:
            pdf_status = {"ok": False, "error": str(e)}
            logging.warning(f"DCR {report_id}: Edge not installed, PDF skipped: {e}")
        if pdf_status.get('ok'):
            pdf_path = pdf_target
            pdf_url = f"/project-files/{pdf_target.relative_to(ssc_paths.data_root()).as_posix()}"   # #287
            # Drive archive — copy the local PDF into the project's synced
            # folder. Failure here is a WARN, never an error: local PDF is
            # always retained; Drive may simply not be configured/running.
            from drive_archive import archive_dcr_pdf
            drive_status = archive_dcr_pdf(
                pdf_target, project_code, report_date, seq, audience='internal'
            )
            if not drive_status.get('ok'):
                logging.warning(
                    f"DCR {report_id}: Drive archive {drive_status.get('status','unavailable')} "
                    f"— {drive_status.get('reason')}"
                )
        else:
            logging.warning(
                f"DCR {report_id}: PDF render failed — {pdf_status.get('error')}"
            )

    # Promote any pre-issuance No-Work placeholder for this date.
    # If the operator flagged the day BEFORE issuing (via /no-work POST),
    # a status='no_work_pending' row exists without dcr_sequence. Capture
    # its flag/reason/note so the new issued row carries them; the
    # placeholder gets deleted in the same pass to keep report_index clean.
    nw_row = conn.execute(
        "SELECT id, no_work, no_work_reason, no_work_note FROM report_index "
        "WHERE project_code = ? AND report_type='DCR' AND report_date = ? "
        "AND status = 'no_work_pending' AND no_work = 1 LIMIT 1",
        (project_code, report_date)
    ).fetchone()
    nw_flag = 0
    nw_reason = None
    nw_note = None
    if nw_row:
        nw_flag = 1
        nw_reason = nw_row['no_work_reason']
        nw_note = nw_row['no_work_note']
        # Drop the placeholder — the real issued row supersedes it.
        conn.execute("DELETE FROM report_index WHERE id = ?", (nw_row['id'],))
    else:
        # Already-flagged via /no-work on a previously-issued row? Read
        # the existing issued row's flag so a re-issue doesn't drop it.
        prev = conn.execute(
            "SELECT no_work, no_work_reason, no_work_note FROM report_index "
            "WHERE project_code = ? AND report_type='DCR' AND report_date = ? "
            "AND no_work = 1 LIMIT 1",
            (project_code, report_date)
        ).fetchone()
        if prev:
            nw_flag = 1
            nw_reason = prev['no_work_reason']
            nw_note = prev['no_work_note']

    existing = conn.execute(
        "SELECT id FROM report_index WHERE project_code = ? AND report_type = ? AND report_id = ?",
        (project_code, 'DCR', report_id)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE report_index SET status = ?, report_date = ?, dcr_sequence = ?, "
            "       no_work = ?, no_work_reason = ?, no_work_note = ?, "
            "       updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            ('issued', report_date, seq, nw_flag, nw_reason, nw_note, existing['id'])
        )
    else:
        conn.execute(
            "INSERT INTO report_index "
            "  (report_date, project_code, report_type, report_id, status, "
            "   dcr_sequence, no_work, no_work_reason, no_work_note, stale) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (report_date, project_code, 'DCR', report_id, 'issued', seq,
             nw_flag, nw_reason, nw_note)
        )
    # Clear stale across ALL audience rows for this (project, date) —
    # re-issuance regenerates the artifact from current data, so both
    # internal and client are now in sync with live regardless of which
    # audience this call rendered. Operator's mental model is "the DCR
    # for date X is fresh," not "the internal copy is fresh."
    conn.execute(
        "UPDATE report_index SET stale = 0, stale_marked_at = NULL "
        "WHERE project_code = ? AND report_date = ? AND report_type = 'DCR'",
        (project_code, report_date),
    )
    return {"report_id": report_id, "audience": audience, "html_url": html_url,
            "html_path": out_file, "display_id": display_id, "sequence": seq,
            "pdf_path": pdf_path, "pdf_url": pdf_url, "pdf_status": pdf_status,
            "drive_status": drive_status,
            "no_work": nw_flag, "no_work_reason": nw_reason, "no_work_note": nw_note}


NO_WORK_REASONS = {"Rain", "Snow", "Holiday", "Other"}


@app.route('/api/projects/<project_code>/daily/<report_date>/no-work', methods=['POST', 'DELETE'])
def api_dcr_no_work(project_code, report_date):
    """Mark / unmark a DCR date as a No-Work Day (Rain / Snow / Holiday / Other).

    POST body (optional):
      {reason: 'Rain'|'Snow'|'Holiday'|'Other', note: str}
      Defaults reason to 'Rain' when omitted (the most common case).

    The flag persists on every report_index row matching (project_code,
    report_date) — both audiences share the designation. If no row
    exists yet (no DCR issued), an INSERT placeholder is created so the
    no-work state is captured before issuance; issue_dcr later promotes
    that placeholder into a full issued row.

    DELETE clears the no-work designation (sets no_work=0, reason/note=NULL).

    Returns the updated state.
    """
    try:
        datetime.strptime(report_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    try:
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 404

        if request.method == 'DELETE':
            conn.execute(
                "UPDATE report_index SET no_work=0, no_work_reason=NULL, no_work_note=NULL, "
                "       updated_at=CURRENT_TIMESTAMP "
                "WHERE project_code = ? AND report_date = ? AND report_type='DCR'",
                (project_code, report_date),
            )
            conn.commit()
            row = conn.execute(
                "SELECT no_work, no_work_reason, no_work_note FROM report_index "
                "WHERE project_code = ? AND report_date = ? AND report_type='DCR' LIMIT 1",
                (project_code, report_date),
            ).fetchone()
            conn.close()
            return response_wrapper({
                "project_code": project_code,
                "report_date": report_date,
                "no_work": int(row["no_work"]) if row else 0,
                "no_work_reason": row["no_work_reason"] if row else None,
                "no_work_note": row["no_work_note"] if row else None,
            })

        data = request.get_json() or {}
        reason = (data.get('reason') or 'Rain').strip()
        if reason not in NO_WORK_REASONS:
            return jsonify({
                "error": "reason must be one of: " + ", ".join(sorted(NO_WORK_REASONS))
            }), 400
        note = (data.get('note') or '').strip() or None

        # Look up matching report_index rows for the date. If none exist,
        # the operator is flagging the day BEFORE issuance — store a
        # placeholder so the state is captured. issue_dcr later sees
        # this and propagates the flag through to both audiences.
        rows = conn.execute(
            "SELECT id, dcr_sequence FROM report_index "
            "WHERE project_code = ? AND report_date = ? AND report_type='DCR'",
            (project_code, report_date),
        ).fetchall()
        if rows:
            conn.execute(
                "UPDATE report_index SET no_work=1, no_work_reason=?, no_work_note=?, "
                "       updated_at=CURRENT_TIMESTAMP "
                "WHERE project_code = ? AND report_date = ? AND report_type='DCR'",
                (reason, note, project_code, report_date),
            )
        else:
            # Pre-issuance placeholder. status='no_work_pending' so it
            # doesn't confuse the archive (which filters status='issued').
            conn.execute(
                "INSERT INTO report_index "
                "  (report_date, project_code, report_type, status, "
                "   no_work, no_work_reason, no_work_note) "
                "VALUES (?, ?, 'DCR', 'no_work_pending', 1, ?, ?)",
                (report_date, project_code, reason, note),
            )
        conn.commit()
        out = conn.execute(
            "SELECT no_work, no_work_reason, no_work_note FROM report_index "
            "WHERE project_code = ? AND report_date = ? AND report_type='DCR' LIMIT 1",
            (project_code, report_date),
        ).fetchone()
        conn.close()
        return response_wrapper({
            "project_code": project_code,
            "report_date": report_date,
            "no_work": int(out["no_work"]),
            "no_work_reason": out["no_work_reason"],
            "no_work_note": out["no_work_note"],
        })
    except Exception as e:
        logging.error(f"{request.method} /api/projects/{project_code}/daily/{report_date}/no-work: {str(e)}")
        return _server_error(e)


@app.route('/api/projects/<project_code>/daily/<report_date>/number', methods=['GET'])
def dcr_number_preview(project_code, report_date):
    """#344 — the number a DCR for this date would carry, shown on the entry form
    BEFORE the operator issues it (a number is permanent once issued). Re-issuing
    a date keeps its number; a new date takes the next one, whatever its date."""
    try:
        datetime.strptime(report_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    conn = db()
    try:
        if not validate_project_exists(conn, project_code):
            return jsonify({"error": "Project not found"}), 404
        existing = lookup_existing_dcr_sequence(conn, project_code, report_date)
        nxt = next_dcr_sequence(conn, project_code)
    finally:
        conn.close()
    seq = existing if existing is not None else nxt
    return response_wrapper({"sequence": seq, "display_id": f"DCR-{project_code}-{seq:03d}",
                             "reissue": existing is not None, "next_sequence": nxt})


@app.route('/api/projects/<project_code>/daily/<report_date>/issue', methods=['POST'])
def issue_dcr(project_code, report_date):
    """Issue a DCR for (project_code, report_date) at the given audience.

    Body: {audience: 'internal'|'client'|'both' (default 'internal'),
           override_active: bool (default false)}

    audience='internal' or 'client' returns the legacy single-row shape
    ({report_id, display_id, audience, html_url, sequence, generated_at}).
    audience='both' runs both audiences inside a single transaction and
    returns {internal_url, client_url, internal_report_id, client_report_id,
    display_id, sequence, audience:'both', generated_at}.

    Sequence semantics: per-project counter, tracks ISSUANCE order.
    Both audiences for the same date share one sequence. Re-issuing a DCR
    for the same (project, date) preserves the existing sequence — pass
    override_active=true to opt into the re-issue (otherwise returns 409
    with the existing sequence). The DATE is preserved in the rendered
    HTML body, not in the report_id."""
    data = request.get_json() or {}
    audience = data.get('audience', 'internal')
    override_active = bool(data.get('override_active', False))
    # #194 — roster-completeness modal protocol. The UI surfaces a
    # modal when this endpoint returns 409 with `missing_regulars`
    # set; the modal collects an `action` and re-POSTs with
    # `roster_completeness` set so this endpoint can apply it BEFORE
    # the issue proceeds. `roster_skip=true` is the explicit "I've
    # already resolved offline" escape (reserved for ad-hoc tooling;
    # NOT exposed in the UI).
    roster_completeness = data.get('roster_completeness') or {}
    roster_skip = bool(data.get('roster_skip', False))
    if audience not in ('internal', 'client', 'both'):
        return jsonify({"error": "audience must be 'internal', 'client', or 'both'"}), 400
    try:
        datetime.strptime(report_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    conn = db()
    if not validate_project_exists(conn, project_code):
        conn.close()
        return jsonify({"error": "Project not found"}), 400
    # Track HTML files written so we can roll them back if the transaction
    # fails. Otherwise a half-issued DCR leaves orphan HTML on disk that the
    # gated /project-files/ route would happily serve.
    written_files = []
    try:
        # ---- #194 roster completeness gate ----
        # Pull recent-regulars from sign_in_log; if any are missing
        # from today's roster AND the operator hasn't already resolved
        # the modal, return 409 with the structured payload the UI
        # uses to surface it.
        import signin_dcr_reconcile as sdr
        if not roster_skip:
            missing = sdr.detect_missing_regulars(
                conn, project_code, report_date,
                threshold=sdr.ROSTER_THRESHOLD_DEFAULT,
                window=sdr.ROSTER_WINDOW_DEFAULT,
            )
            ack_action = roster_completeness.get('action')
            ack_missing = list(roster_completeness.get('acknowledge_missing') or [])
            if missing:
                missing_set = {m['worker_id'] for m in missing}
                if ack_action == 'cancel':
                    conn.close()
                    return jsonify({
                        "error": "DCR issuance cancelled by operator (#194 roster check)",
                        "roster_completeness_status": "cancelled_by_operator",
                    }), 409
                # If the operator hasn't acknowledged the full set, OR
                # action is missing, return the gate response.
                if (ack_action not in ('mark_absent', 'add_default_8h')
                        or set(ack_missing) != missing_set):
                    conn.close()
                    return jsonify({
                        "error": (
                            "Roster completeness check (#194): "
                            f"{len(missing)} regularly-present worker(s) "
                            f"not on today's roster. Operator must "
                            f"acknowledge before DCR can issue."
                        ),
                        "roster_completeness_required": True,
                        "missing_regulars": missing,
                        "project_code": project_code,
                        "report_date": report_date,
                        "modal_actions": [
                            "mark_absent",
                            "add_default_8h",
                            "cancel",
                        ],
                    }), 409
                # Operator has resolved the modal — apply the decision
                # before issuing. Inserts happen inside the same conn,
                # rolled back if anything below this point fails.
                user = current_user() or {}
                sdr.apply_roster_completeness_decision(
                    conn, project_code, report_date,
                    acknowledge_missing=ack_missing,
                    action=ack_action,
                    actor_user_id=user.get('id'),
                    actor_role=user.get('role') or 'system',
                )
        # ---- end #194 gate ----
        dcr_maintenance.number_lock(conn, project_code)   # #344
        existing_seq = lookup_existing_dcr_sequence(conn, project_code, report_date)
        if existing_seq is not None and not override_active:
            conn.close()
            display_id = f"DCR-{project_code}-{existing_seq:03d}"
            return jsonify({
                "error": f"DCR already exists for {project_code} on {report_date}",
                "existing_sequence": existing_seq,
                "existing_display_id": display_id,
                "hint": "Pass override_active=true to re-issue (sequence stays the same)",
            }), 409
        if existing_seq is not None:
            # Re-issue path — sequence is preserved.
            seq = existing_seq
        else:
            # NEW DCR: strictly max+1. Numbers are immutable identity — a
            # backdated date still appends numerically; existing reports are
            # never renumbered by an issue (see next_dcr_sequence).
            seq = next_dcr_sequence(conn, project_code)
        display_id = f"DCR-{project_code}-{seq:03d}"
        generated_at = datetime.utcnow().isoformat() + 'Z'
        if audience == 'both':
            internal = _issue_one_dcr(conn, project_code, report_date, 'internal', seq)
            written_files.append(internal["html_path"])
            client = _issue_one_dcr(conn, project_code, report_date, 'client', seq)
            written_files.append(client["html_path"])
            conn.commit()
            conn.close()
            # #247 — scrub: pdf_status carries pdf_path/edge_path (the UI only
            # reads ok/error/size). *_url stay (gated/served routes).
            return response_wrapper(scrub_paths({
                "audience": "both",
                "generated_at": generated_at,
                "internal_url": internal["html_url"],
                "client_url": client["html_url"],
                "internal_report_id": internal["report_id"],
                "client_report_id": client["report_id"],
                "display_id": display_id,
                "sequence": seq,
                # PDF render runs on internal only; surface its status so the
                # UI can show a "PDF render failed" warning even when HTML
                # issuance succeeded.
                "pdf_url": internal.get("pdf_url"),
                "pdf_status": internal.get("pdf_status"),
                "drive_status": internal.get("drive_status"),
            })), 201
        else:
            result = _issue_one_dcr(conn, project_code, report_date, audience, seq)
            written_files.append(result["html_path"])
            conn.commit()
            conn.close()
            return response_wrapper(scrub_paths({
                "report_id": result["report_id"],
                "audience": result["audience"],
                "html_url": result["html_url"],
                "display_id": display_id,
                "sequence": seq,
                "generated_at": generated_at,
                "pdf_url": result.get("pdf_url"),
                "pdf_status": result.get("pdf_status"),
                "drive_status": result.get("drive_status"),
            })), 201
    except (KeyError, ValueError) as e:
        try: conn.rollback()
        except Exception: pass
        conn.close()
        for fp in written_files:
            try: fp.unlink(missing_ok=True)
            except Exception: pass
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        conn.close()
        # Orphan-HTML cleanup: any file we wrote before the DB transaction
        # rolled back becomes a ghost report otherwise.
        for fp in written_files:
            try: fp.unlink(missing_ok=True)
            except Exception: pass
        logging.error(
            f"POST /api/projects/{project_code}/daily/{report_date}/issue "
            f"(audience={audience}, override={override_active}): {str(e)}"
        )
        return _server_error(e)


# ============= DCR REPORT ARCHIVE =============

def _display_id(report_id):
    """Strip the trailing -internal / -client suffix from a report_id for
    operator-facing display. Returns the input unchanged if no suffix
    matches. report_id stays in the response as the canonical DB key;
    display_id rides alongside for UI rendering."""
    if not report_id:
        return report_id
    for suffix in ('-internal', '-client'):
        if report_id.endswith(suffix):
            return report_id[:-len(suffix)]
    return report_id


@app.route('/api/projects/<project_code>/reports', methods=['GET'])
def list_reports(project_code):
    """List report_index rows for a project with optional filters.

    Query params:
      report_type (optional) — exact match, e.g. 'DCR'
      audience (optional)    — 'internal' or 'client'; applied via the
                               report_id suffix since report_index has
                               no audience column
      from_date (optional)   — inclusive YYYY-MM-DD
      to_date (optional)     — inclusive YYYY-MM-DD

    Returns {data: [{<row>, audience, html_url}, ...], meta: {count}}
    ordered by report_date DESC, id DESC. html_url is synthesized from
    the disk layout for issued DCR rows; NULL for rows where we can't
    derive a path."""
    # Default to internal-only so the operator-facing archive list shows
    # one row per (project, date). API consumers can pass audience=all to
    # get both audiences (back-compat for callers that relied on the
    # pre-default "no filter" behavior).
    audience = request.args.get('audience', 'internal')
    if audience not in ('internal', 'client', 'all'):
        return jsonify({"error": "audience must be 'internal', 'client', or 'all'"}), 400
    for k in ('from_date', 'to_date'):
        v = request.args.get(k)
        if v:
            try:
                datetime.strptime(v, '%Y-%m-%d')
            except ValueError:
                return jsonify({"error": f"{k} must be YYYY-MM-DD"}), 400
    conn = db()
    if not validate_project_exists(conn, project_code):
        conn.close()
        return jsonify({"error": "Project not found"}), 404
    where = ["project_code = ?"]
    params = [project_code]
    rt = request.args.get('report_type')
    if rt:
        where.append("report_type = ?")
        params.append(rt)
    if audience != 'all':
        where.append("report_id LIKE ?")
        params.append(f"%-{audience}")
    fd = request.args.get('from_date')
    if fd:
        where.append("report_date >= ?")
        params.append(fd)
    td = request.args.get('to_date')
    if td:
        where.append("report_date <= ?")
        params.append(td)
    sql = f"SELECT * FROM report_index WHERE {' AND '.join(where)} ORDER BY report_date DESC, id DESC"
    rows = conn.execute(sql, params).fetchall()
    # #344 — the whole project's DCR count, not just the date window the list shows
    total_project = None
    if rt == 'DCR':
        total_project = conn.execute(
            "SELECT COUNT(*) FROM report_index WHERE project_code = ? AND report_type = 'DCR' "
            "AND report_id LIKE ?", (project_code, '%-internal')).fetchone()[0]
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        rid = d.get('report_id') or ''
        if rid.endswith('-internal'):
            d['audience'] = 'internal'
        elif rid.endswith('-client'):
            d['audience'] = 'client'
        else:
            d['audience'] = None
        d['pdf_url'] = None
        if d['audience'] and d.get('dcr_sequence') is not None and d.get('report_type') == 'DCR':
            d['html_url'] = f"/project-files/data_room/reports/dcr/{project_code}/{d['dcr_sequence']:03d}/{d['audience']}.html"
            if d['audience'] == 'internal' and ssc_paths.under_root(
                    "data_room", "reports", "dcr", project_code, f"{d['dcr_sequence']:03d}", "internal.pdf").exists():
                d['pdf_url'] = d['html_url'][:-len('.html')] + '.pdf'   # #344
        else:
            d['html_url'] = None
        d['display_id'] = _display_id(rid)
        out.append(d)
    return response_wrapper(out, count=len(out), total_project=total_project)


@app.route('/api/projects/<project_code>/reports/latest', methods=['GET'])
def latest_report(project_code):
    """Return the most recent report_index row matching report_type +
    audience (plus optional on_date filter). 200 + {data: null} when
    no row found — per the design call, callers prefer a consistent
    envelope shape over a 404 branch."""
    report_type = request.args.get('report_type', 'DCR')
    audience = request.args.get('audience', 'internal')
    if audience not in ('internal', 'client'):
        return jsonify({"error": "audience must be 'internal' or 'client'"}), 400
    on_date = request.args.get('on_date')
    if on_date:
        try:
            datetime.strptime(on_date, '%Y-%m-%d')
        except ValueError:
            return jsonify({"error": "on_date must be YYYY-MM-DD"}), 400
    conn = db()
    if not validate_project_exists(conn, project_code):
        conn.close()
        return jsonify({"error": "Project not found"}), 404
    where = ["project_code = ?", "report_type = ?", "report_id LIKE ?"]
    params = [project_code, report_type, f"%-{audience}"]
    if on_date:
        where.append("report_date = ?")
        params.append(on_date)
    sql = f"SELECT * FROM report_index WHERE {' AND '.join(where)} ORDER BY report_date DESC, id DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    conn.close()
    if not row:
        return response_wrapper(None)
    d = dict(row)
    d['audience'] = audience
    if d.get('dcr_sequence') is not None and d.get('report_type') == 'DCR':
        d['html_url'] = f"/project-files/data_room/reports/dcr/{project_code}/{d['dcr_sequence']:03d}/{audience}.html"
    else:
        d['html_url'] = None
    d['display_id'] = _display_id(d.get('report_id'))
    try:
        rd = datetime.strptime(d['report_date'], '%Y-%m-%d').date()
        d['days_ago'] = (date.today() - rd).days
    except (ValueError, TypeError):
        d['days_ago'] = None
    return response_wrapper(d)


# ============= DROP PLAN ENDPOINTS =============

@app.route('/api/drops/<drop_id>/status', methods=['PATCH'])
def update_drop_status(drop_id):
    """Update drop plan status"""
    try:
        data = request.get_json()
        status = data.get('status')
        
        conn = db()
        conn.execute(
            "UPDATE drop_plan SET status = ?, updated_at = ? WHERE drop_id = ?",
            (status, datetime.now().isoformat(), drop_id)
        )
        conn.commit()
        
        row = conn.execute("SELECT * FROM drop_plan WHERE drop_id = ?", (drop_id,)).fetchone()
        conn.close()
        
        return response_wrapper(dict(row) if row else {}), 200
    except Exception as e:
        return _server_error(e)

# ============= ACTION ITEM ENDPOINTS =============

@app.route('/api/action-items/<int:action_id>/status', methods=['PATCH'])
def update_action_item_status(action_id):
    """Update action item status"""
    try:
        data = request.get_json()
        status = data.get('status')
        completion_date = data.get('completion_date')
        
        conn = db()
        conn.execute(
            "UPDATE meeting_action_items SET status = ?, completion_date = ?, updated_at = ? WHERE id = ?",
            (status, completion_date, datetime.now().isoformat(), action_id)
        )
        conn.commit()
        
        row = conn.execute("SELECT * FROM meeting_action_items WHERE id = ?", (action_id,)).fetchone()
        conn.close()
        
        return response_wrapper(dict(row) if row else {}), 200
    except Exception as e:
        return _server_error(e)

# ============= EMPLOYEE ENDPOINTS =============

@app.route('/api/employees', methods=['POST'])
def create_employee():
    """Create new employee. Allocates a fresh Worker ID (W-####) for the
    human-facing display label — bulk-import in import_workers.py uses the
    same allocator (worker_id.next_worker_id_sequence) so single-onboards
    and CSV imports never collide. employee_id is still the internal PK."""
    try:
        from worker_id import assign_worker_id
        data = request.get_json()
        employee_id = data.get('employee_id') or str(uuid.uuid4())[:8]
        name = data.get('name')
        trade = data.get('trade')

        conn = db()
        worker_id = assign_worker_id(conn)
        conn.execute(
            "INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (employee_id, worker_id, name, trade,
             datetime.now().isoformat(), datetime.now().isoformat())
        )
        conn.commit()

        row = conn.execute("SELECT * FROM employees WHERE employee_id = ?", (employee_id,)).fetchone()
        conn.close()

        return response_wrapper(dict(row) if row else {"employee_id": employee_id, "worker_id": worker_id}), 201
    except Exception as e:
        logging.error(f"POST /api/employees: {str(e)}")
        return _server_error(e)

@app.route('/api/employees/<emp_id>', methods=['PATCH'])
def update_employee(emp_id):
    """Edit an existing employee. Allowed fields: name, trade, dob, phone,
    email, emergency_contact_*, language, hire_date.

    Side-effects run inside a single DB transaction:
    - phone change re-derives PIN (last-4 of digits-only) and checks collision
      against every other row; conflict aborts with 409, no DB write.
    - name change renames worker_records/{emp_id}_{slug}/ on disk after the
      DB UPDATE but before commit; rename failure rolls the UPDATE back.

    Immutable post-import: employee_id, pin (mutated only via phone),
    folder_path (mutated only via name), intake_status, face_image_path,
    photo_path, created_at, updated_at."""
    import re
    EDITABLE = {'name', 'trade', 'dob', 'phone', 'email',
                'emergency_contact_name', 'emergency_contact_phone',
                'emergency_contact_relation', 'language', 'hire_date'}

    try:
        data = request.get_json(silent=True) or {}

        # ----- Pre-validation (no DB writes) -----
        if 'name' in data and not (data.get('name') or '').strip():
            return jsonify({"error": "name cannot be empty"}), 400
        if 'phone' in data and not (data.get('phone') or '').strip():
            return jsonify({"error": "phone cannot be empty"}), 400
        if 'language' in data and data['language'] and data['language'] not in ('EN', 'ES'):
            return jsonify({"error": "language must be 'EN' or 'ES'"}), 400
        for df in ('dob', 'hire_date'):
            if df in data and data[df]:
                try:
                    datetime.strptime(data[df], "%Y-%m-%d")
                except ValueError:
                    return jsonify({"error": f"{df} must be ISO YYYY-MM-DD"}), 400
        if 'phone' in data and data['phone']:
            if len(re.sub(r'\D', '', data['phone'])) < 4:
                return jsonify({"error": "phone must contain at least 4 digits"}), 400

        # ----- Build updates dict from editable fields only -----
        updates = {}
        for k in EDITABLE:
            if k in data:
                v = data[k]
                if isinstance(v, str):
                    v = v.strip()
                    updates[k] = v if v else None
                else:
                    updates[k] = v

        if not updates:
            return jsonify({"error": "No editable fields in payload"}), 400

        conn = db()
        current = conn.execute(
            "SELECT employee_id, folder_path FROM employees WHERE employee_id = ?",
            (emp_id,)
        ).fetchone()
        if not current:
            conn.close()
            return jsonify({"error": "employee not found"}), 404

        # ----- Phone change → re-derive PIN + collision check -----
        pin_changed = False
        new_pin = None
        if 'phone' in updates and updates['phone']:
            phone_digits = re.sub(r'\D', '', updates['phone'])
            new_pin = phone_digits[-4:]
            collision = conn.execute(
                "SELECT employee_id FROM employees WHERE pin = ? AND employee_id != ?",
                (new_pin, emp_id)
            ).fetchone()
            if collision:
                conn.close()
                return jsonify({
                    "error": f"PIN collision with {collision['employee_id']} — choose a phone with different last-4"
                }), 409
            updates['phone'] = phone_digits
            updates['pin'] = new_pin
            pin_changed = True

        # ----- Name change → prepare folder rename (done after UPDATE so the
        #      DB rollback path is meaningful) -----
        rename_from = None
        rename_to = None
        if 'name' in updates and updates['name']:
            new_slug = slugify_name(updates['name'])
            new_folder = WORKER_RECORDS_DIR / f"{emp_id}_{new_slug}"
            current_folder_path = current['folder_path']
            if current_folder_path:
                # #294 S4 — stored rows resolve through THE resolver (pre-#287
                # rows are Windows-absolute); new rows are stored PORTABLE.
                current_folder = ssc_paths.resolve_data_path(current_folder_path)
                if current_folder.resolve() != new_folder.resolve():
                    if current_folder.exists():
                        rename_from = current_folder
                        rename_to = new_folder
                    updates['folder_path'] = ssc_paths.store_rel(new_folder)
            else:
                updates['folder_path'] = ssc_paths.store_rel(new_folder)
            # #294 S4 — face_image_path embeds the folder slug: when the folder
            # actually moves, re-point the row at the renamed location (same
            # filename, portable form) or the headshot goes dark after a rename.
            if rename_from is not None and current['face_image_path']:
                face_name = str(current['face_image_path']).replace('\\', '/').rsplit('/', 1)[-1]
                if face_name:
                    updates['face_image_path'] = ssc_paths.store_rel(new_folder / face_name)

        # ----- UPDATE row -----
        set_clauses = [f"{k} = ?" for k in updates] + ["updated_at = CURRENT_TIMESTAMP"]
        params = list(updates.values()) + [emp_id]
        conn.execute(
            f"UPDATE employees SET {', '.join(set_clauses)} WHERE employee_id = ?",
            params
        )

        # ----- Folder side-effects (transaction still open) -----
        if rename_from is not None:
            try:
                rename_from.rename(rename_to)
            except OSError as e:
                conn.rollback()
                conn.close()
                return jsonify({"error": f"folder rename failed: {e}"}), 500
        elif 'folder_path' in updates:
            try:
                # #294 S4 — the stored value is PORTABLE (relative); mkdir the
                # resolved on-disk location, never the raw row value.
                ssc_paths.resolve_data_path(updates['folder_path']).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                conn.rollback()
                conn.close()
                return jsonify({"error": f"folder mkdir failed: {e}"}), 500

        conn.commit()

        row = conn.execute(
            "SELECT * FROM employees WHERE employee_id = ?",
            (emp_id,)
        ).fetchone()
        conn.close()

        # CLAUDE.md PII discipline: pin is not volunteered in normal responses.
        # Only surfaced when this call caused the change so the operator can
        # share the new PIN with the worker once.
        # #247 — SELECT * carries face_image_path/folder_path/photo_path; scrub.
        result = scrub_paths(dict(row)) if row else {}
        result.pop('pin', None)
        if pin_changed:
            result['pin_changed'] = True
            result['new_pin'] = new_pin

        return response_wrapper(result), 200
    except Exception as e:
        app.logger.error(f"PATCH /api/employees/{emp_id} failed: {e}")
        return _server_error(e)


@app.route('/api/employees/<emp_id>/intake/complete', methods=['POST'])
def complete_intake(emp_id):
    """Guarded forward transition: intake_status 'pending' → 'done'.

    intake_status is excluded from PATCH /api/employees' EDITABLE set so the
    field can't be flipped by a generic worker edit. This is the ONLY endpoint
    that moves a worker into intake-complete state. Forward-only — there is
    no reverse route. Idempotent: an already-done worker returns 200 with
    {already_done: true} so the operator's 'Complete Intake' button is safe
    to re-click without producing a 409.

    Intake completion is operator-driven (the operator decides when the work
    is done); the server doesn't gate on which artifacts are on file."""
    conn = None
    try:
        conn = db()
        emp = conn.execute(
            "SELECT employee_id, intake_status FROM employees WHERE employee_id = ?",
            (emp_id,)
        ).fetchone()
        if not emp:
            return jsonify({"error": "employee not found"}), 404
        if emp["intake_status"] == "done":
            return response_wrapper({
                "employee_id": emp_id,
                "intake_status": "done",
                "already_done": True,
            }), 200
        conn.execute(
            "UPDATE employees SET intake_status = 'done', updated_at = CURRENT_TIMESTAMP "
            "WHERE employee_id = ?",
            (emp_id,)
        )
        conn.commit()
        return response_wrapper({
            "employee_id": emp_id,
            "intake_status": "done",
            "already_done": False,
        }), 200
    except Exception as e:
        logging.error(f"POST /api/employees/{emp_id}/intake/complete: {e}")
        return _server_error(e)
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


@app.route('/api/employees/<emp_id>', methods=['DELETE'])
@requires_company   # #345 — was reachable by any logged-in role
def delete_employee(emp_id):
    """Delete a worker. Auto-dispatches between hard-delete and soft-archive
    based on whether the worker has any operational history — the SAME check as
    the Workforce tab's "Delete permanently" (#345, worker_lifecycle.BLOCKING):

      - NO history -> the worker and every setup row that points at them
        (assignments, rate rows, labor state, devices, sessions) plus their
        records folder are removed, dependents first.
      - HAS history -> soft-archive: employees.archived_at = NOW + reason. The
        history stays so DCRs and the Hours Log stay accurate; the worker can be
        reactivated from the Workforce tab's Inactive list.

    Body: optional {reason}. 404 if the worker doesn't exist; 409 if already
    archived. Returns {deleted: 'hard'|'archived', history_counts}."""
    import worker_lifecycle
    data = request.get_json(silent=True) or {}
    reason = (data.get('reason') or '').strip() or None
    conn = db()
    try:
        emp = conn.execute("SELECT employee_id, archived_at FROM employees WHERE employee_id = ?",
                           (emp_id,)).fetchone()
        if not emp:
            return jsonify({"error": "employee not found"}), 404
        if emp["archived_at"]:
            return jsonify({"error": "employee already archived", "archived_at": emp["archived_at"]}), 409
        plan = worker_lifecycle.removal_plan(conn, emp_id)
        counts = {**plan["blocking"], "total": sum(plan["blocking"].values())}
        if plan["can_delete"]:
            removed, err = worker_lifecycle.delete_worker(conn, emp_id)
            if err:
                return err
            conn.commit()
            conn.close(); conn = None
            worker_lifecycle.remove_folder(emp_id)
            worker_lifecycle.remove_card_files(removed.get("_card_files"))   # #349
            return response_wrapper({"employee_id": emp_id, "deleted": "hard", "history_counts": counts}), 200
        conn.execute("UPDATE employees SET archived_at = CURRENT_TIMESTAMP, archived_reason = ?, "
                     "updated_at = CURRENT_TIMESTAMP WHERE employee_id = ?", (reason, emp_id))
        worker_lifecycle._audit(conn, "worker_archive", emp_id, before={"state": "active"},
                                after={"state": "archived"}, note=reason)
        conn.commit()
        row = conn.execute("SELECT archived_at, archived_reason FROM employees WHERE employee_id = ?",
                           (emp_id,)).fetchone()
        return response_wrapper({"employee_id": emp_id, "deleted": "archived",
                                 "archived_at": row["archived_at"], "archived_reason": row["archived_reason"],
                                 "history_counts": counts}), 200
    except Exception as e:
        logging.error(f"DELETE /api/employees/{emp_id}: {e}")
        return _server_error(e)
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


# ============= CERTIFICATION ENDPOINTS =============

@app.route('/api/certifications', methods=['POST'])
def create_certification():
    """Create new certification"""
    try:
        data = request.get_json()
        employee_id = data.get('employee_id')
        cert_type_id = data.get('cert_type_id')
        
        conn = db()
        if not validate_employee_exists(conn, employee_id):
            conn.close()
            return jsonify({"error": "Employee not found"}), 400
        
        conn.execute(
            "INSERT INTO certifications (employee_id, cert_type_id, expiration_date, file_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (employee_id, cert_type_id, data.get('expiration_date'), data.get('file_path'),
             datetime.now().isoformat(), datetime.now().isoformat())
        )
        conn.commit()
        
        new_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()['id']
        row = conn.execute("SELECT * FROM certifications WHERE id = ?", (new_id,)).fetchone()
        conn.close()
        # #247 — certifications SELECT * carries scan_path/file_path; scrub.
        return response_wrapper(scrub_paths(dict(row)) if row else {}), 201
    except Exception as e:
        return _server_error(e)

# ============= EXISTING READ ENDPOINTS (abbreviated) =============

# #305 — the older GET /api/projects handler that lived here shadowed api_projects_list
# (the Company Console one, with assigned_workers). One route, one handler.

@app.route('/api/projects/<code>', methods=['GET'])
def get_project(code):
    conn = db()
    row = conn.execute("SELECT * FROM projects WHERE project_code = ?", (code,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Project not found"}), 404
    conn.close()
    return response_wrapper(dict(row))

@app.route('/api/employees', methods=['GET'])
@requires_section('internal_ops')   # #305 — worker PII is internal-tier
def get_employees():
    """List employees. Archived workers (archived_at IS NOT NULL) are
    filtered out by default. Pass ?include_archived=true to include them
    (e.g., for restore flows or a 'recently archived' view)."""
    include_archived = (request.args.get('include_archived', '').lower()
                        in ('1', 'true', 'yes'))
    conn = db()
    if include_archived:
        rows = conn.execute("SELECT * FROM employees").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM employees WHERE archived_at IS NULL"
        ).fetchall()
    conn.close()
    # #247 — SELECT * carries face_image_path/folder_path/photo_path; scrub.
    return response_wrapper(strip_worker_pii(scrub_paths(rows_to_dicts(rows))), len(rows))

@app.route('/api/employees/<emp_id>', methods=['GET'])
@requires_section('internal_ops')   # #305
def get_employee(emp_id):
    conn = db()
    # #246 — canonical roster view: labor_status rides along so the profile
    # modal (a MASTER surface) can badge deactivated workers.
    emp_row = conn.execute("SELECT * FROM v_worker_roster WHERE employee_id = ?", (emp_id,)).fetchone()
    if not emp_row:
        conn.close()
        return jsonify({"error": "Employee not found"}), 404
    
    employee = dict(emp_row)
    cert_rows = conn.execute(
        "SELECT c.*, ct.name as cert_name FROM certifications c LEFT JOIN cert_types ct ON c.cert_type_id = ct.cert_type_id WHERE c.employee_id = ?",
        (emp_id,)
    ).fetchall()
    employee['certifications'] = rows_to_dicts(cert_rows)
    conn.close()
    # #247 — employee SELECT * + cert rows (scan_path) — scrub the whole tree.
    return response_wrapper(strip_worker_pii(scrub_paths(employee)))

@app.route('/api/certifications', methods=['GET'])
@requires_section('internal_ops')   # #305
def get_certifications():
    conn = db()
    rows = conn.execute(
        "SELECT c.*, ct.name as cert_name FROM certifications c LEFT JOIN cert_types ct ON c.cert_type_id = ct.cert_type_id ORDER BY c.expiration_date"
    ).fetchall()
    conn.close()
    certs = rows_to_dicts(rows)
    today = date.today().isoformat()
    for cert in certs:
        if cert.get('expiration_date'):
            cert['computed_status'] = 'Expired' if cert['expiration_date'] < today else 'Expiring Soon' if cert['expiration_date'] <= today else 'Active'
    return response_wrapper(certs, len(certs))

@app.route('/api/sign-ins', methods=['GET'])
@requires_section('internal_ops')   # #305
def get_sign_ins():
    conn = db()
    rows = conn.execute(
        "SELECT s.*, e.name FROM sign_in_log s LEFT JOIN employees e ON s.employee_id = e.employee_id ORDER BY s.date DESC"
    ).fetchall()
    conn.close()
    return response_wrapper(rows_to_dicts(rows), len(rows))

@app.route('/api/rfis', methods=['GET'])
def get_rfis():
    """Cross-project RFI list. Each row carries status_derived (Auto-Overdue)
    + turnaround_days. Use /api/projects/<code>/rfis for the
    register-shaped per-project payload + the default sort."""
    if request.args.get('elevation_id'):
        return elevation._api_rfi_list()   # #305 — drawing-markup RFIs
    conn = db()
    rows = conn.execute("SELECT * FROM rfi_log ORDER BY date_submitted DESC").fetchall()
    conn.close()
    today_iso = date.today().isoformat()
    out = [_rfi_row_to_dict(r, today_iso) for r in rows]
    return response_wrapper(out, len(out))


@app.route('/api/rfis/<rfi_id>', methods=['GET'])
def get_rfi(rfi_id):
    conn = db()
    row = conn.execute("SELECT * FROM rfi_log WHERE rfi_number = ?", (rfi_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "RFI not found"}), 404
    return response_wrapper(_rfi_row_to_dict(row, date.today().isoformat()))


@app.route('/api/projects/<project_code>/rfis', methods=['GET'])
def api_project_rfis(project_code):
    """RFI Log register payload for a project.

    Query params:
      status                   filter (multi-value supported via ?status=Open&status=Overdue)
      due_before / due_after   bound date_response_required (YYYY-MM-DD)
      schedule_impact_only=1   only schedule-impacting rows
      include_legacy_unprefixed=1
                               include legacy rfi_numbers like 'RFI-001'
                               (without the project_code prefix). Default off.

    Default sort (per spec): status_derived='Open' or 'Overdue' first
    (Overdue ranks higher than Open), then date_response_required ASC,
    nulls last.
    """
    try:
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 404
        # Pull every row for the project — register-grade volume, server-side
        # filter+sort below. (For very large RFI counts, pagination would
        # move into SQL; volumes today don't warrant it.)
        rows = conn.execute(
            "SELECT * FROM rfi_log WHERE project_code = ?",
            (project_code,),
        ).fetchall()
        conn.close()
        today_iso = date.today().isoformat()
        out = [_rfi_row_to_dict(r, today_iso) for r in rows]

        # Filters
        status_filter = [s for s in request.args.getlist('status') if s]
        if status_filter:
            sset = set(status_filter)
            out = [r for r in out if r.get('status_derived') in sset]
        due_before = request.args.get('due_before')
        due_after  = request.args.get('due_after')
        if due_before:
            out = [r for r in out
                   if (r.get('date_response_required') or '9999-12-31') <= due_before]
        if due_after:
            out = [r for r in out
                   if (r.get('date_response_required') or '0000-01-01') >= due_after]
        if request.args.get('schedule_impact_only') in ('1', 'true', 'yes'):
            out = [r for r in out if r.get('schedule_impact_flag')]

        # Default sort: Overdue=0, Open=1, Answered/Closed/Void=2, then
        # date_response_required ASC nulls last.
        def _sort_key(r):
            s = r.get('status_derived') or 'Open'
            rank = 2
            if s == 'Overdue':
                rank = 0
            elif s == 'Open':
                rank = 1
            return (rank, r.get('date_response_required') or '9999-12-31',
                    r.get('rfi_number') or '')
        out.sort(key=_sort_key)
        return response_wrapper(out, len(out))
    except Exception as e:
        logging.error(f"GET /api/projects/{project_code}/rfis: {str(e)}")
        return _server_error(e)


@app.route('/api/projects/<project_code>/rfi-constraints', methods=['GET'])
def api_project_rfi_constraints(project_code):
    """CONSTRAINT FEED for Phase 3 Two-Week Look-Ahead.

    Per construction_builds_spec.json linkage_rules.rfi_to_lookahead:
    'An open RFI with schedule_impact_flag=true and a location_reference
    becomes a CONSTRAINT on that location in the Two-Week Look-Ahead.'

    Returns RFIs where:
      status_derived IN ('Open', 'Overdue')
      AND schedule_impact_flag = 1
    Each row carries rfi_number, subject_title, sent_to,
    date_response_required (the 'needed by' date for the constraint),
    location_unit, location_id, status_derived, and turnaround context.

    Phase-3 Look-Ahead consumes this to render gating constraints on
    each location's row.
    """
    try:
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 404
        rows = conn.execute(
            "SELECT * FROM rfi_log WHERE project_code = ? "
            "AND schedule_impact_flag = 1",
            (project_code,),
        ).fetchall()
        conn.close()
        today_iso = date.today().isoformat()
        out = []
        for r in rows:
            d = _rfi_row_to_dict(r, today_iso)
            if d.get('status_derived') not in ('Open', 'Overdue'):
                continue
            # Slim payload — only what the Look-Ahead needs
            out.append({
                'rfi_number':              d.get('rfi_number'),
                'subject_title':           d.get('subject_title'),
                'sent_to':                 d.get('sent_to'),
                'date_submitted':          d.get('date_submitted'),
                'date_response_required':  d.get('date_response_required'),
                'status_derived':          d.get('status_derived'),
                'location_unit':           d.get('location_unit'),
                'location_id':             d.get('location_id'),
                'scope_category':          d.get('scope_category'),
                'schedule_impact_flag':    d.get('schedule_impact_flag'),
                'cost_impact_flag':        d.get('cost_impact_flag'),
                'impact_magnitude_note':   d.get('impact_magnitude_note'),
            })
        # Sort by date_response_required ASC (nearest constraint first)
        out.sort(key=lambda x: x.get('date_response_required') or '9999-12-31')
        return response_wrapper(out, len(out))
    except Exception as e:
        logging.error(f"GET /api/projects/{project_code}/rfi-constraints: {str(e)}")
        return _server_error(e)

@app.route('/api/drops', methods=['GET'])
def get_drops():
    conn = db()
    rows = conn.execute("SELECT * FROM drop_plan ORDER BY planned_start_date").fetchall()
    conn.close()
    return response_wrapper(rows_to_dicts(rows), len(rows))

@app.route('/api/drops/<drop_id>', methods=['GET'])
def get_drop(drop_id):
    conn = db()
    row = conn.execute("SELECT * FROM drop_plan WHERE drop_id = ?", (drop_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Drop not found"}), 404
    return response_wrapper(dict(row))

@app.route('/api/site-closures', methods=['GET'])
@requires_section('internal_ops')   # #305
def get_site_closures():
    conn = db()
    rows = conn.execute("SELECT * FROM site_closure_log ORDER BY date DESC").fetchall()
    conn.close()
    return response_wrapper(rows_to_dicts(rows), len(rows))

@app.route('/api/site-closures/<closure_id>', methods=['GET'])
@requires_section('internal_ops')   # #305
def get_site_closure(closure_id):
    conn = db()
    row = conn.execute("SELECT * FROM site_closure_log WHERE closure_id = ?", (closure_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Closure not found"}), 404
    return response_wrapper(dict(row))

@app.route('/api/permits', methods=['GET'])
def get_permits():
    conn = db()
    rows = conn.execute("SELECT * FROM permits_library ORDER BY expiration_date").fetchall()
    conn.close()
    return response_wrapper(rows_to_dicts(rows), len(rows))

@app.route('/api/documents', methods=['GET'])
def get_documents():
    conn = db()
    rows = conn.execute("SELECT * FROM document_library ORDER BY uploaded_at DESC").fetchall()
    conn.close()
    return response_wrapper(rows_to_dicts(rows), len(rows))

@app.route('/api/toolbox-talks', methods=['GET'])
def get_toolbox_talks():
    conn = db()
    rows = conn.execute("SELECT * FROM toolbox_talk_library ORDER BY title").fetchall()
    conn.close()
    return response_wrapper(rows_to_dicts(rows), len(rows))

@app.route('/api/meetings', methods=['GET'])
def get_meetings():
    conn = db()
    rows = conn.execute("SELECT * FROM meeting_records ORDER BY date DESC").fetchall()
    conn.close()
    return response_wrapper(rows_to_dicts(rows), len(rows))

@app.route('/api/meetings/<meeting_id>', methods=['GET'])
def get_meeting(meeting_id):
    conn = db()
    row = conn.execute("SELECT * FROM meeting_records WHERE meeting_id = ?", (meeting_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Meeting not found"}), 404
    meeting = dict(row)
    action_rows = conn.execute("SELECT * FROM meeting_action_items WHERE meeting_id = ?", (meeting_id,)).fetchall()
    meeting['action_items'] = rows_to_dicts(action_rows)
    conn.close()
    return response_wrapper(meeting)

@app.route('/api/action-items', methods=['GET'])
def get_action_items():
    conn = db()
    rows = conn.execute("SELECT * FROM meeting_action_items ORDER BY due_date").fetchall()
    conn.close()
    return response_wrapper(rows_to_dicts(rows), len(rows))

@app.route('/api/drops/<drop_id>/sign-off', methods=['POST'])
def sign_off_drop(drop_id):
    """Record drop plan sign-off"""
    try:
        data = request.get_json()
        role = data.get('role')  # 'Foreman', 'Super', 'QEI'
        signed_by_employee_id = data.get('signed_by_employee_id')
        sign_off_date = data.get('date', date.today().isoformat())
        
        conn = db()
        
        # Update drop_plan sign_off_status
        conn.execute(
            "UPDATE drop_plan SET sign_off_status = 'Complete', updated_at = ? WHERE drop_id = ?",
            (datetime.now().isoformat(), drop_id)
        )
        
        # Insert sign-off record (if table exists) or just log it
        try:
            conn.execute(
                "INSERT INTO drop_plan_sign_offs (drop_id, role, signed_by_employee_id, date, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (drop_id, role, signed_by_employee_id, sign_off_date, datetime.now().isoformat())
            )
        except:
            pass  # Table may not exist yet
        
        conn.commit()
        row = conn.execute("SELECT * FROM drop_plan WHERE drop_id = ?", (drop_id,)).fetchone()
        conn.close()
        
        return response_wrapper(dict(row)), 200
    except Exception as e:
        return _server_error(e)


# ============= WORKER APP ENDPOINTS =============

_WORKER_SESSION_TTL = timedelta(hours=16)


def _worker_token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _worker_session(conn, token, *, employee_id=None, project_code=None):
    """Return the one active PWA session represented by an opaque bearer token.

    An employee ID in a request body is display data only; it must match this
    server-side record and can never select somebody else's time record.
    """
    if not isinstance(token, str) or len(token) < 32:
        return None
    row = conn.execute(
        "SELECT token_hash, employee_id, project_code, expires_at, started_sign_in_id, ended_at "
        "FROM worker_app_session WHERE token_hash=? AND revoked_at IS NULL",
        (_worker_token_hash(token),),
    ).fetchone()
    if not row or str(row["expires_at"]) <= datetime.now().isoformat(timespec="seconds"):
        return None
    if employee_id and row["employee_id"] != employee_id:
        return None
    if project_code and row["project_code"] and row["project_code"] != project_code:   # #325: NULL = not picked yet
        return None
    return row

def _haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000  # Earth radius meters
    d_lat = (lat1 - lat2) * math.pi / 180
    d_lng = (lng1 - lng2) * math.pi / 180 * math.cos(math.radians(lat2))   # #305 — E-W was overstated ~32%
    a = (d_lat / 2) ** 2 + (d_lng / 2) ** 2
    return R * 2 * (a ** 0.5)


def _worker_features(conn, project_code):
    """What the worker app shows on this job (project_setup.worker_access; photos by default)."""
    import project_setup
    try:
        r = conn.execute("SELECT worker_access FROM projects WHERE project_code = ?", (project_code,)).fetchone()
        return project_setup.worker_access(dict(r) if r else {})
    except Exception:
        conn.rollback()
        return list(project_setup.WORKER_DEFAULT)


def _worker_projects(conn, employee_id, latitude=None, longitude=None):
    """#325 — the jobs a worker may check in to, each geofenced against ITS OWN site.

    Active assignments on active projects; a worker with no assignment at all is
    offered every active project (the office tightens this by assigning crews).
    With a location: a project with coordinates is `eligible` only inside its
    radius (default 200 m); a project with no coordinates on file is eligible as
    is. Without a location (shift start, after the login already checked it)
    every candidate is eligible."""
    rows = conn.execute(
        "SELECT p.project_code, p.name, p.site_lat, p.site_lng, p.geofence_m FROM projects p "
        "JOIN project_assignments a ON a.project_code = p.project_code "
        "WHERE a.employee_id = ? AND a.status = 'active' AND COALESCE(p.status, 'active') <> 'closed' "
        "ORDER BY p.name, p.project_code", (employee_id,)).fetchall()
    assigned = bool(rows)
    if not rows:
        rows = conn.execute(
            "SELECT project_code, name, site_lat, site_lng, geofence_m FROM projects "
            "WHERE COALESCE(status, 'active') <> 'closed' ORDER BY name, project_code").fetchall()
    out = []
    for r in rows:
        has = r["site_lat"] is not None and r["site_lng"] is not None
        dist = in_range = None
        if has and latitude is not None and longitude is not None:
            dist = _haversine_m(float(latitude), float(longitude), float(r["site_lat"]), float(r["site_lng"]))
            in_range = dist <= float(r["geofence_m"] or 200)
        out.append({"project_code": r["project_code"], "name": r["name"] or r["project_code"],
                    "features": _worker_features(conn, r["project_code"]),
                    "has_geofence": has, "distance_m": (round(dist) if dist is not None else None),
                    "in_range": in_range, "assigned": assigned,
                    "eligible": (not has) or (in_range if in_range is not None else True)})
    return out


@app.route('/api/worker/login', methods=['POST'])
def worker_login():
    """Worker sign-in: PIN + geofence validation.

    DB-backed lookup replaces the hardcoded sample-data dict that previously
    gated auth — that dict was migration scaffolding from before real workers
    were imported. PINs now resolve against employees.pin, derived at import
    time from the worker's phone last-4 (see import_workers.py)."""
    try:
        data = request.get_json(silent=True) or {}
        pin = (data.get('phone_or_pin') or '').strip()
        device_token = (data.get('device_token') or '').strip()
        latitude = float(data.get('latitude', 0))
        longitude = float(data.get('longitude', 0))

        if len(pin) != 4 or not pin.isdigit():
            return jsonify({"error": "Invalid PIN"}), 401

        # #289 — per-source PIN THROTTLE (always on, independent of device
        # enforcement): a burst of wrong PINs from one origin gets stalled +
        # eventually refused, so 4 digits can't be brute-forced from the open
        # internet. Uses the login_audit 'pin_fail' rows the same window covers.
        import auth as _auth
        _pin_ip = _auth._client_ip()
        _pin_fails = _auth._recent_ip_fails(_pin_ip)   # shares the login fail window
        if _pin_fails >= _auth.LOGIN_IP_MAX_FAILS:
            _auth._login_audit(None, "pin_lockout", _pin_ip)
            time.sleep(_auth._login_backoff_delay(_pin_fails))
            return jsonify({"error": "Too many attempts — wait a few minutes"}), 429
        _pin_delay = _auth._login_backoff_delay(_pin_fails)

        conn = db()
        # #246 — resolve against the canonical roster so labor status gates
        # sign-in: a deactivated (retired) worker's PIN no longer signs in —
        # new hours for a retired worker were the last propagation hole.
        # Reactivation on Labor Rates instantly restores sign-in.
        rows = conn.execute(
            "SELECT employee_id, name, labor_status FROM v_worker_roster "
            "WHERE pin = ? AND pin IS NOT NULL AND pin != '' "
            "AND archived_at IS NULL",
            (pin,)
        ).fetchall()

        if len(rows) == 0:
            conn.close()
            if _pin_delay:
                time.sleep(_pin_delay)
            _auth._login_audit(None, "pin_fail", _pin_ip)
            return jsonify({"error": "Invalid PIN"}), 401
        if all(r["labor_status"] == 'inactive' for r in rows):
            conn.close()
            return jsonify({"error": "Worker is inactive — see the office"}), 403
        if len(rows) > 1:
            # import_workers.py blocks PIN collisions pre-flight, so this branch
            # is defensive — never auth on ambiguity, log for investigation.
            app.logger.error(
                f"PIN collision in employees: {len(rows)} matches for given PIN"
            )
            conn.close()
            return jsonify({"error": "Authentication failed"}), 500
        emp = dict(rows[0])

        # #289 — DEVICE BINDING. When enforcement is on, a valid PIN is not enough:
        # the phone must present a device_token issued by the operator's one-time
        # provisioning (worker_device). Enforcement DEFAULTS OFF (app_settings) so a
        # deploy never strands the field crew — the operator flips it on after the
        # crew's phones are provisioned, a hard pre-M5 gate. Throttle above is
        # unconditional; THIS gate is the flag.
        import auth_hardening as _wd
        if _wd.worker_enforcement_on(conn):
            if not _wd.device_valid_for(conn, emp['employee_id'], device_token):
                conn.close()
                if _pin_delay:
                    time.sleep(_pin_delay)
                _auth._login_audit(None, "pin_fail", _pin_ip)
                return jsonify({"error": "This device isn't set up yet — ask the office to add it",
                                "device_required": True}), 403

        # #325 — geofence PER JOB: the worker's projects (assignments, or every active
        # project when unassigned), each checked against its own coordinates. No
        # client-controlled bypass: a project's coordinates live on the projects row.
        candidates = _worker_projects(conn, emp['employee_id'], latitude, longitude)
        eligible = [p for p in candidates if p["eligible"]]
        if not candidates:
            conn.close()
            return jsonify({"error": "No job assigned — see the office"}), 403
        if not eligible:
            conn.close()
            return jsonify({"error": "Not on site"}), 403
        auto_code = eligible[0]["project_code"] if len(eligible) == 1 else None

        cert_rows = conn.execute(
            "SELECT c.*, ct.name as cert_name FROM certifications c "
            "LEFT JOIN cert_types ct ON c.cert_type_id = ct.cert_type_id "
            "WHERE c.employee_id = ? ORDER BY c.expiration_date",
            (emp['employee_id'],)
        ).fetchall()
        certs = rows_to_dicts(cert_rows)
        today = date.today().isoformat()

        # Test-day workers have no certs imported yet — once Phase D lands, the
        # existing expiration gate enforces real eligibility. A separate task
        # tracks refactoring the gate to CoF-prereq-only semantics.
        if certs:
            expired = [c for c in certs if c.get('expiration_date', '') < today]
            if expired:
                conn.close()
                return jsonify({
                    "error": "Certification expired",
                    "cert": expired[0].get('cert_name')
                }), 403
        # A bearer is only ever returned once and only its digest lives in the
        # database.  Clock-in/out validate it before touching a labor record.
        session_token = secrets.token_urlsafe(32)
        now = datetime.now().isoformat(timespec="seconds")
        expires = (datetime.now() + _WORKER_SESSION_TTL).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO worker_app_session "
            "(token_hash, employee_id, project_code, issued_at, expires_at) VALUES (?,?,?,?,?)",
            (_worker_token_hash(session_token), emp['employee_id'], auto_code or '', now, expires),   # '' = not picked yet (#325)
        )
        conn.commit()
        conn.close()

        return response_wrapper({
            "employee_id": emp['employee_id'],
            "name": emp['name'],
            "session_token": session_token,
            "project_code": auto_code,          # None -> the app shows the job chooser
            "projects": eligible,               # #325 — what the chooser offers
            "certifications": certs
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/worker/session/start', methods=['POST'])
def worker_session_start():
    """Record shift start in sign_in_log.

    Mirrors the column set used by POST /api/sign-ins so the schema-mismatch
    INSERT (latitude/longitude/start_time columns that don't exist) no longer
    silently breaks the worker-app sign-in flow."""
    try:
        data = request.get_json(silent=True) or {}
        employee_id = data.get('employee_id')
        project_code = (data.get('project_code') or '').strip()
        session_token = data.get('session_token') or ''
        if not employee_id:
            return jsonify({"error": "employee_id required"}), 400
        now_iso = datetime.now().isoformat()
        today = date.today().isoformat()

        conn = db()
        session = _worker_session(conn, session_token, employee_id=employee_id,
                                  project_code=project_code or None)
        if not session:
            conn.close()
            return jsonify({"error": "valid worker session required"}), 401
        if session["started_sign_in_id"] is not None or session["ended_at"] is not None:
            conn.close()
            return jsonify({"error": "worker session already used"}), 409
        # #325 — the shift lands on one of THIS worker's jobs, never an arbitrary code
        allowed = {p["project_code"] for p in _worker_projects(conn, employee_id)}
        if not project_code or project_code not in allowed:
            conn.close()
            return jsonify({"error": "pick one of your jobs"}), 403
        # One place at a time: a finished entry today (another job, or typed in
        # by the office) that covers now blocks a second clock-in. An entry
        # left open (a missed sign-out) does not, or the worker is locked out.
        import payroll_hours as _ph
        _closed = [e for e in _ph.day_entries(conn, employee_id, today) if e["time_out"]]
        _hit = _ph.find_overlap(_closed, now_iso, None)
        if _hit:
            conn.close()
            return jsonify({"error": _ph.overlap_message(_hit), "code": "overlap"}), 409
        if not session["project_code"]:
            conn.execute("UPDATE worker_app_session SET project_code=? WHERE token_hash=?",
                         (project_code, session["token_hash"]))
        cursor = conn.execute(
            "INSERT INTO sign_in_log (date, employee_id, project_code, time_in, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (today, employee_id, project_code, now_iso, now_iso, now_iso)
        )
        sign_in_log_id = cursor.lastrowid
        conn.execute(
            "UPDATE worker_app_session SET started_sign_in_id=? WHERE token_hash=?",
            (sign_in_log_id, session["token_hash"]),
        )
        # Stale flag: if a DCR was already issued for this (project, date)
        # — usually only matters when the operator backdates a DCR before
        # workers finish signing out — flag it. Worker-app sign-ins on
        # not-yet-issued days are no-op (no DCR to mark).
        _mark_dcr_stale(conn, project_code, today)
        conn.commit()
        conn.close()

        return response_wrapper({
            "sign_in_log_id": sign_in_log_id,
            "employee_id": employee_id,
            "project_code": project_code,
            "time_in": now_iso
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/worker/session/end', methods=['POST'])
def worker_session_end():
    """Record shift end by closing the most recent open sign_in_log row for
    this employee today.

    The schema has no session_id column, so we identify the row via
    (employee_id, today's date, time_out IS NULL) and close the most recent."""
    try:
        data = request.get_json(silent=True) or {}
        employee_id = data.get('employee_id')
        session_token = data.get('session_token') or ''
        if not employee_id:
            return jsonify({"error": "employee_id required"}), 400
        now_iso = datetime.now().isoformat()
        # SQLite DATE('now') is UTC; worker_session_start stores Python's local
        # date.today(). Mismatch means a session opened locally before midnight
        # UTC can't be found by DATE('now') — pass the local date explicitly.
        today = date.today().isoformat()

        conn = db()
        session = _worker_session(conn, session_token, employee_id=employee_id)
        if not session or session["started_sign_in_id"] is None or session["ended_at"] is not None:
            conn.close()
            return jsonify({"error": "active worker session required"}), 401
        row = conn.execute(
            "SELECT id, project_code, date FROM sign_in_log "
            "WHERE id = ? AND employee_id = ? AND date = ? AND time_out IS NULL",
            (session["started_sign_in_id"], employee_id, today)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "No open session found for today"}), 404

        sign_in_log_id = row['id']
        # Past 8 hours for the day: the worker gives a reason before clocking
        # out (the app shows the reason screen on 422 needs_reason).
        import payroll_hours as _ph
        ot_reason = (data.get('reason') or '').strip()
        if _ph.rule_applies(today):
            full = conn.execute("SELECT time_in FROM sign_in_log WHERE id = ?", (sign_in_log_id,)).fetchone()
            total = _ph.day_total(_ph.day_entries(conn, employee_id, today, exclude_id=sign_in_log_id),
                                  (full["time_in"], now_iso))
            if not ot_reason and _ph.needs_reason(total, _ph.get_overtime(conn, employee_id, today)):
                conn.close()
                return jsonify({"error": "Over 8 hours today. Tell the office why.",
                                "code": "needs_reason", "needs_reason": True, "day_total": total}), 422
        conn.execute(
            "UPDATE sign_in_log SET time_out = ?, updated_at = ? WHERE id = ?",
            (now_iso, now_iso, sign_in_log_id)
        )
        _hours_sync(conn, employee_id, today, ot_reason, actor_role='worker')
        conn.execute(
            "UPDATE worker_app_session SET ended_at=?, revoked_at=? WHERE token_hash=?",
            (now_iso, now_iso, session["token_hash"]),
        )
        _mark_dcr_stale(conn, row['project_code'], row['date'])
        conn.commit()
        conn.close()

        return response_wrapper({
            "sign_in_log_id": sign_in_log_id,
            "employee_id": employee_id,
            "time_out": now_iso
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/worker/session/<session_id>', methods=['GET'])
def get_worker_session(session_id):
    """Legacy route intentionally retired.

    The old implementation queried a nonexistent column and, more importantly,
    would have disclosed a labor row through a public worker route.  The PWA
    does not use a read-session endpoint; clock actions use the bearer token.
    """
    return jsonify({"error": "worker session lookup is not available"}), 410


# Worker-app photo upload. Before this, the app's Send button only bumped a local counter
# and every photo was thrown away while the screen said "Photos upload directly".
_WORKER_PHOTO_LOCATIONS = {"S1": "Scaffold 1", "S2": "Scaffold 2", "S3": "Scaffold 3", "S4": "Scaffold 4",
                           "S5": "Scaffold 5", "ROOF": "Roof", "INT": "Interior", "OUT": "Outside"}
_WORKER_PHOTO_SCOPES = {"brick": "Brick", "pointing": "Pointing", "demo": "Demo", "sealant": "Sealant",
                        "cleanup": "Cleanup", "other": "Other"}
_WORKER_PHOTO_MAX_BYTES = 15 * 1024 * 1024   # one phone-camera JPEG is ~1-5 MB
_WORKER_PHOTO_DAILY_CAP = 300                # per worker per job per day — bounds a stuck retry loop


@app.route('/api/worker/photos', methods=['POST'])
def worker_photo_upload():
    """One photo from a signed-in worker (multipart: photo, session_token, employee_id,
    location, scope). Authenticated by the worker-app bearer session, which must be on an
    open shift; the photo always lands on THAT shift's job (never a client-chosen code),
    in the job's Field Photos tab (Unassigned tray, caption "Scaffold 2 · Pointing") so the
    office can sort it into a drop. Same image pipeline as the dashboard upload (downscale,
    thumbnail, orientation baked in, GPS stripped). Logs carry the job and photo id only."""
    import field_photos as fp
    token = request.form.get('session_token') or ''
    employee_id = (request.form.get('employee_id') or '').strip()
    location = (request.form.get('location') or '').strip()
    scope = (request.form.get('scope') or '').strip()
    photo = request.files.get('photo')
    if not employee_id:
        return jsonify({"error": "employee_id required"}), 400
    conn = db()
    try:
        session = _worker_session(conn, token, employee_id=employee_id)
        if not session or session["started_sign_in_id"] is None or session["ended_at"] is not None \
                or not session["project_code"]:
            return jsonify({"error": "sign in to a job first", "session": False}), 401
        if "photos" not in _worker_features(conn, session["project_code"]):
            return jsonify({"error": "photos are switched off for this job"}), 403
        if location not in _WORKER_PHOTO_LOCATIONS or scope not in _WORKER_PHOTO_SCOPES:
            return jsonify({"error": "pick where and the scope"}), 400
        if not photo or not photo.filename:
            return jsonify({"error": "no photo in request"}), 400
        data = photo.read(_WORKER_PHOTO_MAX_BYTES + 1)
        if len(data) > _WORKER_PHOTO_MAX_BYTES:
            return jsonify({"error": "photo too large"}), 413
        project_code = session["project_code"]
        today = date.today().isoformat()
        sent_today = conn.execute(
            "SELECT COUNT(*) FROM field_photos WHERE project_code = ? AND worker_id = ? AND substr(uploaded_at, 1, 10) = ?",
            (project_code, employee_id, today)).fetchone()[0]
        if sent_today >= _WORKER_PHOTO_DAILY_CAP:
            return jsonify({"error": "daily photo limit reached — see the office"}), 429
        try:
            res = fp.process_image(data, photo.filename, fallback_dt_iso=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except fp.SkipImage:
            return jsonify({"error": "could not read that photo — take it again"}), 400
        file_path, thumb_path = _fp_write(project_code, res)
        caption = f"{_WORKER_PHOTO_LOCATIONS[location]} · {_WORKER_PHOTO_SCOPES[scope]}"
        cur = conn.execute(
            "INSERT INTO field_photos (project_code, drop_id, worker_id, stage, caption, taken_at, taken_at_estimated, "
            "uploaded_at, uploaded_by_uid, file_path, thumb_path, file_name, file_size, mime, width, height, "
            "orientation_applied) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_code, None, employee_id, None, caption, res["taken_at"], 1 if res["taken_at_estimated"] else 0,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), None, file_path, thumb_path, res["file_name"],
             len(res["display_bytes"]), res["mime"], res["width"], res["height"], 1 if res["orientation_applied"] else 0))
        conn.commit()
        logging.info("worker photo stored project=%s id=%s", project_code, cur.lastrowid)
        return response_wrapper({"id": cur.lastrowid, "project_code": project_code, "caption": caption,
                                 "sent_today": sent_today + 1}), 201
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logging.error("POST /api/worker/photos: %s", type(e).__name__)
        return jsonify({"error": "upload failed — try again"}), 500
    finally:
        conn.close()

@app.route('/api/worker/videos', methods=['POST'])
def worker_video_upload():
    """One short video from a signed-in worker (accepted 2026-10-14; field_videos.py). Multipart:
    video, poster (a JPEG still the app grabbed when recording started), duration (seconds),
    session_token, employee_id, location, scope. Same gates as a photo (open shift, the shift's
    job, the job's "photos" switch, where + scope), plus the video limits: 60 s, size, 10 a day,
    and enough free disk. Stored as-is next to the job's photos (no re-encoding); the poster
    becomes the grid thumbnail. Logs carry the job and id only."""
    import field_photos as fp
    import field_videos as fv
    token = request.form.get('session_token') or ''
    employee_id = (request.form.get('employee_id') or '').strip()
    location = (request.form.get('location') or '').strip()
    scope = (request.form.get('scope') or '').strip()
    video, poster = request.files.get('video'), request.files.get('poster')
    if not employee_id:
        return jsonify({"error": "employee_id required"}), 400
    conn = db()
    try:
        session = _worker_session(conn, token, employee_id=employee_id)
        if not session or session["started_sign_in_id"] is None or session["ended_at"] is not None \
                or not session["project_code"]:
            return jsonify({"error": "sign in to a job first", "session": False}), 401
        if "photos" not in _worker_features(conn, session["project_code"]):
            return jsonify({"error": "photos and videos are switched off for this job"}), 403
        if location not in _WORKER_PHOTO_LOCATIONS or scope not in _WORKER_PHOTO_SCOPES:
            return jsonify({"error": "pick where and the scope"}), 400
        if not video or not poster:
            return jsonify({"error": "no video in request"}), 400
        try:
            duration = fv.clean_duration(request.form.get('duration'))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        data = video.read(fv.MAX_BYTES + 1)
        if len(data) > fv.MAX_BYTES:
            return jsonify({"error": "video too large — keep it under a minute"}), 413
        mime = fv.kind_of(data)
        if not mime:
            return jsonify({"error": "could not read that video — record it again"}), 400
        project_code = session["project_code"]
        today = date.today().isoformat()
        sent_today = conn.execute(
            "SELECT COUNT(*) FROM field_photos WHERE project_code = ? AND worker_id = ? AND substr(uploaded_at, 1, 10) = ? "
            "AND mime LIKE 'video/%'", (project_code, employee_id, today)).fetchone()[0]
        if sent_today >= fv.DAILY_CAP:
            return jsonify({"error": f"{fv.DAILY_CAP} videos a day is the limit — take photos or see the office"}), 429
        if not fv.has_room(len(data)):
            logging.warning("worker video refused: storage nearly full project=%s", project_code)
            return jsonify({"error": "storage is nearly full — tell the office (photos still work)"}), 507
        try:
            still = fp.process_image(poster.read(fp.MAX_FILE_BYTES + 1), "poster.jpg",
                                     fallback_dt_iso=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except fp.SkipImage:
            return jsonify({"error": "could not read that video — record it again"}), 400
        fv.ensure(conn)
        file_path, thumb_path = _fp_write_video(project_code, data, fv.MIMES[mime], still["thumb_bytes"])
        caption = f"{_WORKER_PHOTO_LOCATIONS[location]} · {_WORKER_PHOTO_SCOPES[scope]}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT INTO field_photos (project_code, drop_id, worker_id, stage, caption, taken_at, taken_at_estimated, "
            "uploaded_at, uploaded_by_uid, file_path, thumb_path, file_name, file_size, mime, width, height, "
            "orientation_applied, duration_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_code, None, employee_id, None, caption, now, 0, now, None, file_path, thumb_path,
             "video" + fv.MIMES[mime], len(data), mime, still["width"], still["height"], 0, duration))
        conn.commit()
        logging.info("worker video stored project=%s id=%s seconds=%s", project_code, cur.lastrowid, duration)
        return response_wrapper({"id": cur.lastrowid, "project_code": project_code, "caption": caption,
                                 "videos_today": sent_today + 1}), 201
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logging.error("POST /api/worker/videos: %s", type(e).__name__)
        return jsonify({"error": "upload failed — try again"}), 500
    finally:
        conn.close()


@app.route('/api/photos/upload', methods=['POST'])
def upload_photo():
    """Upload a site photo to disk + record metadata in the photos table.

    Disk layout: data_room/photos/<project_code>/<date>/<location>/<ts>_<uuid8>.<ext>
    On DB INSERT failure, the on-disk file is rolled back (deleted) so disk
    state and DB state never diverge. Replaces the prior contract (session_id,
    zone, scope, latitude, longitude) which had no live callers."""
    try:
        if 'photo' not in request.files:
            return jsonify({"error": "No photo file"}), 400
        project_code = request.form.get('project_code')
        if not project_code:
            return jsonify({"error": "project_code required"}), 400
        try:
            date_str = _parse_dcr_date(request.form.get('date'))
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400

        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 400
        conn.close()

        location = (request.form.get('location') or 'Unknown').strip() or 'Unknown'
        description = request.form.get('description')
        uploaded_by = request.form.get('uploaded_by')

        photo_file = request.files['photo']
        orig_name = photo_file.filename or ""
        ext = Path(orig_name).suffix.lower()
        if ext not in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif'}:
            ext = ".jpg"

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        photo_uuid = uuid.uuid4().hex[:8]
        filename = f"{timestamp}_{photo_uuid}{ext}"

        photos_base = (ssc_paths.under_root("data_room", "photos")).resolve()
        photo_dir = ssc_paths.under_root("data_room", "photos") / project_code / date_str / location
        if not photo_dir.resolve().is_relative_to(photos_base):
            return jsonify({"error": "Invalid location path"}), 400
        photo_dir.mkdir(parents=True, exist_ok=True)

        file_path = photo_dir / filename
        photo_file.save(str(file_path))

        # #295 S2 — rel against the ACTIVE DATA ROOT (what /project-files
        # resolves from), never the code dir: on the cloud the file lands
        # under /var/data while SCRIPT_DIR is /app — relative_to raised and
        # every DCR photo upload 500'd. The two only coincide on an unrooted
        # workstation. Stored row is PORTABLE (store_rel) per the #287 write
        # doctrine.
        rel = file_path.resolve().relative_to(ssc_paths.data_root().resolve()).as_posix()
        url = f"/project-files/{rel}"

        conn = db()
        try:
            conn.execute(
                "INSERT INTO photos (date, project_code, file_path, filename, url, "
                "location, description, uploaded_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (date_str, project_code, ssc_paths.store_rel(file_path), filename, url,
                 location, description, uploaded_by)
            )
            conn.commit()
            new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
            row = conn.execute("SELECT * FROM photos WHERE id = ?", (new_id,)).fetchone()
            conn.close()
            # #247 — photos.file_path stripped; the gated `url` field stays.
            return response_wrapper(scrub_paths(dict(row))), 201
        except Exception as db_err:
            conn.close()
            try:
                file_path.unlink(missing_ok=True)
            except Exception:
                pass
            logging.error(f"POST /api/photos/upload DB insert failed, rolled back file {file_path}: {db_err}")
            return jsonify({"error": f"DB insert failed: {db_err}"}), 500
    except Exception as e:
        logging.error(f"POST /api/photos/upload: {str(e)}")
        return _server_error(e)

@app.route('/api/photos', methods=['GET'])
def get_photos():
    """List photos for (project, date). Filters on the photos.date column
    (the operator-meaningful 'photo taken on day X' value) AND project_code,
    so cross-project photos can't leak into a single-project view.

    Query params:
      date         — ISO YYYY-MM-DD; defaults to today (local)
      project_code — defaults to FR-BX-001. `project` is accepted as an
                     alias for backwards compatibility with existing callers.

    Prior bug: this endpoint filtered on DATE(created_at) (the row's INSERT
    timestamp, which is in server TZ and unrelated to the photo's day) and
    silently dropped the project_code filter altogether — both photos from
    other projects AND photos for the requested project but uploaded on a
    different day were mis-selected."""
    try:
        date_filter = request.args.get('date', date.today().isoformat())
        project_code = (request.args.get('project_code')
                        or request.args.get('project')
                        or 'FR-BX-001')
        conn = db()
        rows = conn.execute(
            "SELECT * FROM photos WHERE date = ? AND project_code = ? "
            "ORDER BY created_at DESC",
            (date_filter, project_code)
        ).fetchall()
        conn.close()
        # #247 — photos.file_path stripped; the gated `url` field stays.
        return response_wrapper(scrub_paths(rows_to_dicts(rows)) if rows else [])
    except Exception as e:
        return jsonify({"error": str(e)}), 400
# ============= PROJECTS (Company Console) =============

@app.route('/api/projects', methods=['GET'])
def api_projects_list():
    """All projects + counts for the Company Console grid. #263 — role-scoped: a pm sees
    only assigned ACTIVE projects (closed excluded), admin/c_suite see all."""
    try:
        conn = db()
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY status, name"
        ).fetchall()
        user = current_user() or {}
        rows = pm_scoping.filter_visible_projects(rows, user.get("role"), user.get("id"), conn)
        out = []
        for r in rows:
            project_code = r["project_code"]
            assigned_count = conn.execute(
                "SELECT COUNT(*) FROM project_assignments WHERE project_code = ? AND status = 'active'",
                (project_code,)
            ).fetchone()[0]
            out.append({**dict(r), "assigned_workers": assigned_count})
        conn.close()
        return response_wrapper(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/projects/<project_code>/workers', methods=['GET'])
def api_project_workers(project_code):
    """Workers ASSIGNED to a specific project (the filtered roster for the
    project dashboard). Archived workers are filtered out — pass
    ?include_archived=true to include them.

    PII posture (#233): returns ONLY the fields the consumers actually use —
    the DCR labor add-dropdown (employee_id + worker_id + name) and the
    Project Health KPI tiles (certs_expiring_30d + certs_expired). It does
    NOT spread the employees row, so no face_image_path / folder_path /
    phone / dob / pin or any other *_path or column ever reaches the JSON.
    Cert windows use the LOCAL date (date.today(), never UTC — no
    off-by-one, per CLAUDE.md dates rule). Gated; served no-store because
    the payload carries worker names (same posture as /crew-compliance)."""
    try:
        include_archived = (request.args.get('include_archived', '').lower()
                            in ('1', 'true', 'yes'))
        conn = db()
        # #246 — the active-roster rule lives in the CANONICAL VIEW
        # v_active_workers (#241 semantics: labor_worker_state.status,
        # missing-row-means-active, archived excluded). Deactivate ->
        # instantly gone from this selector; reactivate -> instantly back;
        # historical rows (sign_in_log etc.) untouched either way.
        # include_archived=true is the audit escape hatch: raw employees,
        # no filters.
        src = 'employees' if include_archived else 'v_active_workers'
        rows = conn.execute(
            f"""SELECT e.employee_id, e.worker_id, e.name
               FROM {src} e
               JOIN project_assignments pa ON pa.employee_id = e.employee_id
               WHERE pa.project_code = ? AND pa.status = 'active'
               ORDER BY CAST(SUBSTR(e.worker_id, 3) AS INTEGER)""",
            (project_code,)
        ).fetchall()

        today = date.today()
        today_iso = today.isoformat()
        d30_iso = (today + timedelta(days=30)).isoformat()
        out = []
        for e in rows:
            certs = conn.execute(
                "SELECT expiration_date FROM certifications WHERE employee_id = ?",
                (e["employee_id"],)
            ).fetchall()
            expiring_30 = sum(1 for c in certs if c["expiration_date"] and today_iso <= c["expiration_date"] <= d30_iso)
            expired = sum(1 for c in certs if c["expiration_date"] and c["expiration_date"] < today_iso)
            out.append({"employee_id": e["employee_id"], "worker_id": e["worker_id"],
                        "name": e["name"], "certs_expiring_30d": expiring_30,
                        "certs_expired": expired})
        conn.close()
        resp = response_wrapper(out)
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/projects/<project_code>/crew-compliance', methods=['GET'])
def api_project_crew_compliance(project_code):
    """#223 — crew credential readiness for the project's on-site roster; powers
    the Employees & Certifications card view. Reuses employees + certifications
    (no cert-data changes); compliance is COMPUTED from cert expiry using the
    LOCAL date. PII posture: returns the worker's name/trade/worker_id (same
    audience as the existing per-project roster) plus a has_photo BOOLEAN —
    NEVER any *_path. Headshots come ONLY from the gated face-photo route.
    no-store (the payload carries names)."""
    conn = db()
    try:
        include_archived = (request.args.get('include_archived', '').lower()
                            in ('1', 'true', 'yes'))
        # #246 — OPERATIONAL surface: the crew = the canonical active roster
        # (v_active_workers: #241 rule, one place). Deactivating a worker on
        # Labor Rates removes their card/row/counts here instantly; the
        # readiness hero math downstream is computed over this filtered set.
        # include_archived=true stays the raw audit hatch.
        src = 'employees' if include_archived else 'v_active_workers'
        rows = conn.execute(
            f"""SELECT e.employee_id, e.worker_id, e.name, e.trade, e.face_image_path, e.intake_status,
                       pa.role_on_project
                FROM {src} e
                JOIN project_assignments pa ON pa.employee_id = e.employee_id
                WHERE pa.project_code = ? AND pa.status = 'active'
                ORDER BY e.name""",
            (project_code,)).fetchall()
        today = date.today()
        today_iso = today.isoformat()
        d30_iso = (today + timedelta(days=30)).isoformat()
        base = ssc_paths.under_root("worker_records").resolve()   # #287
        renderable = ('.jpg', '.jpeg', '.png')   # browser-renderable headshots only
        workers = []
        agg = {"total": 0, "ready": 0, "expiring": 0, "expired": 0}
        for e in rows:
            certs = conn.execute(
                """SELECT ct.name AS cert_name, c.date_obtained, c.expiration_date, c.status
                   FROM certifications c LEFT JOIN cert_types ct ON ct.cert_type_id = c.cert_type_id
                   WHERE c.employee_id = ?
                   ORDER BY CASE WHEN c.expiration_date IS NULL THEN 1 ELSE 0 END,
                            c.expiration_date ASC, c.id DESC""",
                (e["employee_id"],)).fetchall()
            clist = []
            n_expiring = n_expired = 0
            for c in certs:
                exp = c["expiration_date"]
                if not exp and not c["date_obtained"]:
                    sd = "unknown"
                elif not exp:
                    sd = "valid"
                elif exp < today_iso:
                    sd = "expired"; n_expired += 1
                elif exp <= d30_iso:
                    sd = "expiring"; n_expiring += 1
                else:
                    sd = "valid"
                days = None
                if exp:
                    try:
                        days = (date.fromisoformat(exp) - today).days
                    except (ValueError, TypeError):
                        days = None
                clist.append({"cert_name": c["cert_name"] or "Certification",
                              "date_obtained": c["date_obtained"], "expiration_date": exp,
                              "status_derived": sd, "days_to_expiry": days})
            compliance = "expired" if n_expired else ("expiring" if n_expiring else "ready")
            has_photo = False
            fp = e["face_image_path"]
            if fp:
                try:
                    # #294 S3 — through THE resolver (the _fp_serve #290 lesson, again):
                    # pre-#287 rows are Windows-absolute; raw Path() can never sit
                    # under a Linux root, so has_photo read false on the cloud host.
                    p = ssc_paths.resolve_data_path(fp)
                    has_photo = (p.suffix.lower() in renderable
                                 and p.resolve().is_relative_to(base) and p.exists())
                except (OSError, ValueError):
                    has_photo = False
            agg["total"] += 1
            agg[compliance] += 1
            workers.append({
                "employee_id": e["employee_id"], "worker_id": e["worker_id"],
                "name": e["name"], "trade": e["trade"], "role_on_project": e["role_on_project"],
                "has_photo": bool(has_photo),
                "intake_status": e["intake_status"], "cert_total": len(clist),
                "cert_expiring": n_expiring, "cert_expired": n_expired,
                "compliance": compliance, "certs": clist,
            })
        total = agg["total"]
        pct_ready = round(100 * agg["ready"] / total) if total else 0
        resp = response_wrapper({"workers": workers, "hero": {**agg, "pct_ready": pct_ready}})
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp
    finally:
        conn.close()


# ===========================================================================
# Project Documents — Batch A (#229): per-project compliance doc checklist +
# single/bulk upload + gated file serving. file_path is on-disk ONLY (NEVER in
# any JSON); readiness/status is COMPUTED from expiry (LOCAL date). NO AI yet.
# ===========================================================================
_PROJDOC_ROLES = ('admin', 'c_suite', 'pm', 'super')   # site-management; Contracts may tighten to admin/c_suite later
_PROJDOC_CATEGORIES = ('PERMITS', 'DRAWINGS', 'CONTRACTS', 'INSPECTIONS', 'SAFETY', 'CLOSEOUT')
_PROJDOC_CATNAMES = {
    'PERMITS': 'Permits & Approvals', 'DRAWINGS': 'Drawings & Engineering',
    'CONTRACTS': 'Contracts & Financial', 'INSPECTIONS': 'Inspections & Reports',
    'SAFETY': 'Safety & Compliance', 'CLOSEOUT': 'Closeout',
}
_PROJDOC_EXT_TYPE = {
    '.pdf': ('PDF', 'application/pdf'),
    '.jpg': ('JPG', 'image/jpeg'), '.jpeg': ('JPG', 'image/jpeg'),
    '.png': ('PNG', 'image/png'),
    '.heic': ('HEIC', 'image/heic'), '.heif': ('HEIC', 'image/heif'),
    '.xlsx': ('XLSX', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    '.xlsm': ('XLSM', 'application/vnd.ms-excel.sheet.macroEnabled.12'),
    '.xls': ('XLS', 'application/vnd.ms-excel'),
    '.csv': ('CSV', 'text/csv'),
}


def _projdoc_suggest_category(filename):
    """Filename heuristic for the bulk tray's category auto-suggest (overridable)."""
    f = (filename or '').lower()
    if any(k in f for k in ('coi', 'insurance', 'contract', 'aia', 'sov', 'pay app', 'payapp', 'pay_app', 'g702')):
        return 'CONTRACTS'
    if any(k in f for k in ('rope', 'sds', 'safety', 'sst', 'toolbox')):
        return 'SAFETY'
    if any(k in f for k in ('arch', 'drawing', 'draw', 'rev', 'struct', 'shop')):
        return 'DRAWINGS'
    if any(k in f for k in ('fisp', 'inspect', 'probe', 'tr1', 'qewi', 'signoff', 'sign-off')):
        return 'INSPECTIONS'
    if any(k in f for k in ('completion', 'warrant', 'lien', 'closeout')):
        return 'CLOSEOUT'
    if any(k in f for k in ('permit', 'pw2', 'best', 'variance', 'shed')):
        return 'PERMITS'
    return 'PERMITS'


def _projdoc_status(d, today_iso, d30_iso):
    if d.get('superseded'):
        return 'superseded'
    exp = d.get('expiry_date')
    if exp:
        if exp < today_iso:
            return 'expired'
        if exp <= d30_iso:
            return 'expiring'
    return 'on_file'


def _projdoc_public(d, today_iso, d30_iso):
    """PII/path-safe shape — NO file_path. file_url is the GATED route, not a path."""
    out = {
        'id': d['id'], 'project_code': d['project_code'], 'category': d['category'],
        'requirement_key': d['requirement_key'], 'title': d['title'], 'doc_type': d['doc_type'],
        'file_name': d['file_name'], 'file_size': d['file_size'], 'mime': d['mime'],
        'effective_date': d['effective_date'], 'expiry_date': d['expiry_date'],
        'version': d['version'], 'notes': d['notes'], 'superseded': d['superseded'],
        'uploaded_at': d['uploaded_at'], 'status': _projdoc_status(d, today_iso, d30_iso),
        'file_url': f"/api/documents/{d['id']}/file",
        # drop zone — the approved type tag + what Claude read off the pages
        'type_key': d.get('type_key'), 'type_label': doc_intake.type_label(d.get('type_key')),
        'ai': doc_intake.doc_ai_public(d),
        # whose record it is (NULL = our company); several parties can share a checklist item
        'party': d.get('party'), 'party_name': d.get('party_name'),
    }
    # #271 — version history (computed by the list endpoint for current docs only)
    if '_history' in d:
        out['history'] = d['_history']
        out['history_count'] = len(d['_history'])
    return out


def _projdoc_save_file(project_code, fs):
    """Save an upload under data_room/project_docs/<project>/<uuid>.<ext>.
    Returns (path, doc_type, mime, file_size); raises ValueError on a bad type/path.
    The stored name is a uuid — the original filename never touches the path."""
    ext = Path(fs.filename or 'document').suffix.lower()
    if ext not in _PROJDOC_EXT_TYPE:
        raise ValueError(f"unsupported file type: {ext or '(none)'}")
    base = (ssc_paths.under_root('data_room', 'project_docs')).resolve()
    pdir = ssc_paths.under_root('data_room', 'project_docs') / project_code
    if not pdir.resolve().is_relative_to(base):
        raise ValueError("invalid project path")
    pdir.mkdir(parents=True, exist_ok=True)
    fpath = pdir / (uuid.uuid4().hex + ext)
    fs.save(str(fpath))
    doc_type, mime = _PROJDOC_EXT_TYPE[ext]
    return fpath, doc_type, mime, fpath.stat().st_size


def _projdoc_insert(conn, project_code, fs, form, supersedes_id=None):
    """Single-row insert from a FileStorage + form-like dict. Rolls the on-disk file
    back on DB failure so disk/DB never diverge. Returns the new id.
    #271: supersedes_id links this row as the NEW VERSION of an existing document
    (the caller validates + marks the old row superseded in the same transaction)."""
    category = (form.get('category') or '').strip().upper()
    if category not in _PROJDOC_CATEGORIES:
        raise ValueError("invalid category")
    fpath, doc_type, mime, size = _projdoc_save_file(project_code, fs)
    try:
        rk = (form.get('requirement_key') or '').strip() or None
        file_name = Path(fs.filename or 'document').name
        title = (form.get('title') or '').strip() or file_name
        eff = (form.get('effective_date') or '').strip() or None
        exp = (form.get('expiry_date') or '').strip() or None
        for dval in (eff, exp):
            if dval:
                datetime.strptime(dval, '%Y-%m-%d')   # LOCAL YYYY-MM-DD validation
        version = (form.get('version') or '').strip() or None
        notes = (form.get('notes') or '').strip() or None
        party, party_name = doc_intake.clean_party(form.get('party'), form.get('party_name'))
        uid = (current_user() or {}).get('id')
        cur = conn.execute(
            "INSERT INTO project_documents (project_code, category, requirement_key, title, doc_type, "
            "file_path, file_name, file_size, mime, effective_date, expiry_date, version, notes, "
            "superseded, uploaded_by_uid, uploaded_at, supersedes_id, party, party_name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
            (project_code, category, rk, title, doc_type, str(fpath), file_name, size, mime,
             eff, exp, version, notes, uid, datetime.now().isoformat(), supersedes_id, party, party_name))
        return cur.lastrowid
    except Exception:
        try:
            fpath.unlink(missing_ok=True)
        except Exception:
            pass
        raise


_PROJDOC_OTHER_PARTIES = ('subcontractor', 'other')


def _projdoc_other_company(d):
    """A subcontractor's or another company's record: filed under Additional documents,
    never counted toward the job's own checklist."""
    return (d.get('party') or 'company') in _PROJDOC_OTHER_PARTIES


def _projdoc_job_ctx(conn, project_code):
    """What decides which documents the job needs (doc_suggest.plan): its type (and, for rope
    access, inspection or repair), access methods (None = never set), billing type, whether the
    Drop Plan has drops and whether closeout is near (target completion within 30 days, or closed).
    The caller adds `filed` (what is on file)."""
    import project_setup
    ctx = {'project_type': None, 'ra_scope': None, 'access': None, 'access_applies': False, 'billing_type': None,
           'has_drops': False, 'near_end': False}
    try:
        su = project_setup.setup(conn, project_code) or {}
        et = su.get('est_type') or project_setup.est_type_of(project_code)
        ctx['ra_scope'] = su.get('rope_access_scope')
        ctx['access'] = su.get('access_methods')
        ctx['access_applies'] = bool(su.get('access_applies'))
        ctx['project_type'] = project_setup.PROJECT_TYPE_FOR.get(et)
    except Exception:
        conn.rollback()
    try:   # a type set on the project wins over the job-code prefix ('generic' is the column default)
        row = conn.execute("SELECT project_type FROM projects WHERE project_code=?", (project_code,)).fetchone()
        if row and row['project_type'] and (row['project_type'] != 'generic' or not ctx['project_type']):
            ctx['project_type'] = row['project_type']
    except Exception:   # a database without the #project-type column
        conn.rollback()
    try:
        ctx['billing_type'] = billing_type.get(conn, project_code)
    except Exception:
        conn.rollback()
    try:
        ctx['has_drops'] = conn.execute("SELECT 1 FROM drops WHERE project_code=? LIMIT 1",
                                        (project_code,)).fetchone() is not None
    except Exception:   # no Drop Plan tables on this database
        conn.rollback()
    try:
        row = conn.execute("SELECT status, target_completion FROM projects WHERE project_code=?", (project_code,)).fetchone()
        if row:
            end = str(row['target_completion'] or '')[:10]
            ctx['near_end'] = (row['status'] or 'active') != 'active' or bool(
                end and end <= (date.today() + timedelta(days=30)).isoformat())
    except Exception:   # a database without target_completion
        conn.rollback()
    return ctx


@app.route('/api/projects/<project_code>/documents', methods=['GET'])
@requires_role(*_PROJDOC_ROLES)
def api_project_documents(project_code):
    """#229 — the compliance checklist payload: per category, each REQUIRED item with
    its computed status (on_file/missing/expiring/expired) + extra docs + the readiness
    rollup (on_file/missing/attention + % of required on file). NO *_path. no-store.
    An item can hold several current documents (ours + each subcontractor's COI, say):
    `docs` lists them all; `doc` is the one that decides the item's status (the best
    standing, newest first). `suggestions` = doc_suggest over what is filed.
    Only what the job needs now counts (doc_suggest.applies: its scope, access methods, billing,
    drops, what is filed; closeout once the end is near); a missing item is returned as a suggestion, never as an empty
    row. Another company's documents (party subcontractor / other) are not ours: they never
    fill the job's checklist and come back grouped in `additional`."""
    conn = db()
    try:
        ctx = _projdoc_job_ctx(conn, project_code)
        today = date.today()
        today_iso = today.isoformat()
        d30_iso = (today + timedelta(days=30)).isoformat()
        reqs = [dict(r) for r in conn.execute(
            "SELECT category, requirement_key, label, sort_order FROM document_requirements "
            "ORDER BY category, sort_order").fetchall()]
        docs = [dict(d) for d in conn.execute(
            "SELECT * FROM project_documents WHERE project_code=? ORDER BY uploaded_at DESC, id DESC",
            (project_code,)).fetchall()]
        if ctx.get('access') is None and any(d.get('type_key') == 'best_permit' and not d['superseded']
                                             for d in docs):
            ctx['has_drops'] = True   # access never set, but a BEST permit is filed: suspended scaffold
        by_req = {}   # every non-superseded doc of OURS fulfilling each requirement, newest first
        for d in docs:
            rk = d['requirement_key']
            if rk and not d['superseded'] and not _projdoc_other_company(d):
                by_req.setdefault((d['category'], rk), []).append(d)
        ctx['filed'] = {rk for (_c, rk) in by_req}   # what is on file brings more in (a PW2 -> SST cards, …)
        _rank = {'on_file': 0, 'expiring': 1, 'expired': 2}
        # #271 — version chains: history = walk supersedes_id from each CURRENT doc.
        # A superseded row that is REFERENCED by a newer version lives in History (not
        # extras); legacy superseded rows with no successor stay extras as before.
        by_id = {d['id']: d for d in docs}
        in_history = set()

        def _doc_history(d):
            chain, seen = [], set()
            cur_id = d.get('supersedes_id')
            while cur_id and cur_id in by_id and cur_id not in seen:
                seen.add(cur_id)
                prev = by_id[cur_id]
                chain.append({'id': prev['id'], 'effective_date': prev['effective_date'],
                              'expiry_date': prev['expiry_date'], 'uploaded_at': prev['uploaded_at'],
                              'doc_type': prev['doc_type'], 'mime': prev['mime'],
                              'file_name': prev['file_name'],
                              'file_url': f"/api/documents/{prev['id']}/file"})
                in_history.add(cur_id)
                cur_id = prev.get('supersedes_id')
            return chain   # newest previous first; v-numbers rendered client-side

        for d in docs:
            if not d['superseded']:
                d['_history'] = _doc_history(d)
        cat_reqs = {}
        for r in reqs:
            cat_reqs.setdefault(r['category'], []).append(r)
        agg = {'required_total': 0, 'on_file': 0, 'missing': 0, 'attention': 0}
        missing_labels = {}
        not_asked = []
        out_cats = []
        for code in _PROJDOC_CATEGORIES:
            items = []
            for r in cat_reqs.get(code, []):
                held = [_projdoc_public(d, today_iso, d30_iso)
                        for d in by_req.get((code, r['requirement_key']), [])]
                required = doc_suggest.applies(r['requirement_key'], ctx)
                missing_labels[(code, r['requirement_key'])] = r['label']
                if not required and not held:   # not needed now and nothing filed: not shown, not counted
                    if doc_suggest.phase(r['requirement_key'], ctx) is None and r['requirement_key'] not in doc_suggest.NOT_REQUIRED:
                        not_asked.append({'requirement_key': r['requirement_key'], 'label': r['label'],
                                          'why': doc_suggest.why_not(r['requirement_key'], ctx)})
                    continue
                if required:
                    agg['required_total'] += 1
                if held:
                    best = min(held, key=lambda x: _rank.get(x['status'], 9))   # stable: newest wins ties
                    st = best['status']
                    if required:
                        agg['attention' if st in ('expiring', 'expired') else 'on_file'] += 1
                    items.append({'requirement_key': r['requirement_key'], 'label': r['label'],
                                  'status': st, 'doc': best, 'docs': held, 'required': required})
                else:
                    agg['missing'] += 1
                    items.append({'requirement_key': r['requirement_key'], 'label': r['label'],
                                  'status': 'missing', 'doc': None, 'docs': [], 'required': True})
            req_keys = {r['requirement_key'] for r in cat_reqs.get(code, [])}
            extras = [_projdoc_public(d, today_iso, d30_iso) for d in docs
                      if d['category'] == code and d['id'] not in in_history and not _projdoc_other_company(d)
                      and (not d['requirement_key']
                           or d['requirement_key'] not in req_keys or d['superseded'])]
            filed = sum(1 for it in items if it['status'] != 'missing')
            out_cats.append({'category': code, 'name': _PROJDOC_CATNAMES[code], 'items': items,
                             'extras': extras, 'filed': filed, 'total': len(items)})
        rt = agg['required_total']
        pct = round(100 * agg['on_file'] / rt) if rt else 0
        # #270 — annotate every doc payload with its client-visibility state (one batched
        # query pair via visibility.document_states; default internal-only/not-flagged).
        states = visibility.document_states(conn, project_code)
        additional = {}   # other companies' documents, one group per (party, company name)
        for d in docs:
            if _projdoc_other_company(d) and d['id'] not in in_history and not d['superseded']:
                g = additional.setdefault((d['party'], (d.get('party_name') or '').strip().lower()),
                                          {'party': d['party'], 'party_name': d.get('party_name') or None, 'docs': []})
                g['docs'].append(_projdoc_public(d, today_iso, d30_iso))
        additional = sorted(additional.values(), key=lambda g: ((g['party_name'] or '~').lower(), g['party']))
        for d in [x for c in out_cats for it in c['items'] for x in it['docs']] + \
                 [x for c in out_cats for x in c['extras']] + [x for g in additional for x in g['docs']]:
            st = states.get(d['id'], {})   # `doc` is one of `docs`
            d['shared_client'] = bool(st.get('shared_client'))
            d['flagged'] = bool(st.get('flagged'))
        missing_reqs = {(c['category'], it['requirement_key']) for c in out_cats
                        for it in c['items'] if it['status'] == 'missing'}
        current = [{**d, 'ai': doc_intake.doc_ai_public(d)} for d in docs if not d['superseded']]
        dismissed = {r['suggestion_key'] for r in conn.execute(
            "SELECT suggestion_key FROM project_doc_suggest_dismissed WHERE project_code=?",
            (project_code,)).fetchall()}
        suggestions = doc_suggest.suggest(current, missing_reqs, dismissed, ctx=ctx, missing_labels=missing_labels)
        resp = response_wrapper({'categories': out_cats, 'readiness': {**agg, 'pct': pct},
                                 'suggestions': suggestions, 'additional': additional,
                                 'job': {'project_type': ctx.get('project_type'),
                                         'scope': doc_suggest.scope_of(ctx),
                                         'scope_label': doc_suggest.SCOPE_LABELS[doc_suggest.scope_of(ctx)],
                                         'near_end': bool(ctx.get('near_end')),
                                         'access_applies': bool(ctx.get('access_applies')),
                                         'access_set': ctx.get('access') is not None,
                                         'billing_type': ctx.get('billing_type'),
                                         'has_drops': bool(ctx.get('has_drops')), 'not_asked': not_asked}})
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp
    finally:
        conn.close()


@app.route('/api/projects/<project_code>/documents', methods=['POST'])
@requires_role(*_PROJDOC_ROLES)
def api_project_documents_upload(project_code):
    """#229 — upload ONE document (multipart: file + category, requirement_key, title,
    effective_date, expiry_date, version, notes). LOCAL dates.
    #271 — optional supersedes_id makes this an UPDATE (new version): the old row is
    marked superseded (NEVER deleted — file + row kept for History) and its external
    share rows are removed with the engine's audit trail (a client never sees a stale
    permit). The NEW version starts INTERNAL-ONLY by design — re-sharing is deliberate
    (operator-approved security default)."""
    conn = db()
    try:
        if not validate_project_exists(conn, project_code):
            return jsonify({"error": "project not found"}), 404
        if 'file' not in request.files:
            return jsonify({"error": "no file"}), 400
        supersedes_id = (request.form.get('supersedes_id') or '').strip() or None
        old = None
        if supersedes_id is not None:
            try:
                supersedes_id = int(supersedes_id)
            except ValueError:
                return jsonify({"error": "supersedes_id must be an integer"}), 400
            old = conn.execute(
                "SELECT id, project_code, superseded FROM project_documents WHERE id=?",
                (supersedes_id,)).fetchone()
            if not old or old['project_code'] != project_code:
                return jsonify({"error": "document to update not found in this project"}), 404
            if old['superseded']:
                return jsonify({"error": "that version is already superseded — update the current version"}), 409
        try:
            new_id = _projdoc_insert(conn, project_code, request.files['file'], request.form,
                                     supersedes_id=supersedes_id)
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        if old is not None:
            uid = (current_user() or {}).get('id')
            conn.execute("UPDATE project_documents SET superseded=1 WHERE id=?", (supersedes_id,))
            # instant external revoke of the stale version, audited per audience
            for aud in visibility.audiences_for(conn, 'document', supersedes_id):
                visibility.unshare(conn, 'document', supersedes_id, aud, uid)
        conn.commit()
        today = date.today()
        row = dict(conn.execute("SELECT * FROM project_documents WHERE id=?", (new_id,)).fetchone())
        return response_wrapper(_projdoc_public(row, today.isoformat(), (today + timedelta(days=30)).isoformat())), 201
    except Exception as e:
        logging.error(f"POST /api/projects/{project_code}/documents: {e}")
        return _server_error(e)
    finally:
        conn.close()


@app.route('/api/projects/<project_code>/documents/bulk', methods=['POST'])
@requires_role(*_PROJDOC_ROLES)
def api_project_documents_bulk(project_code):
    """#229 — upload MANY in one request. Client sends file_<i> + category_<i> (chosen
    category per file, seeded by the filename heuristic + operator override) + expiry_<i>."""
    conn = db()
    try:
        if not validate_project_exists(conn, project_code):
            return jsonify({"error": "project not found"}), 404
        pairs = []
        i = 0
        while f'file_{i}' in request.files:
            pairs.append((i, request.files[f'file_{i}']))
            i += 1
        if not pairs:   # fallback: any file values, category from heuristic
            pairs = list(enumerate(request.files.values()))
        if not pairs:
            return jsonify({"error": "no files"}), 400
        saved, errors = [], []
        for idx, fs in pairs:
            cat = (request.form.get(f'category_{idx}') or request.form.get('category') or '').strip().upper()
            if cat not in _PROJDOC_CATEGORIES:
                cat = _projdoc_suggest_category(fs.filename)
            form = {'category': cat, 'title': fs.filename,
                    'expiry_date': request.form.get(f'expiry_{idx}', '')}
            try:
                saved.append(_projdoc_insert(conn, project_code, fs, form))
            except ValueError as ve:
                errors.append({'file': Path(fs.filename or '?').name, 'error': str(ve)})
        conn.commit()
        return response_wrapper({'saved': len(saved), 'ids': saved, 'errors': errors}), 201
    except Exception as e:
        logging.error(f"POST /api/projects/{project_code}/documents/bulk: {e}")
        return _server_error(e)
    finally:
        conn.close()


@app.route('/api/documents/suggest-category', methods=['GET'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_suggest():
    """Filename -> suggested category (one home for the bulk-tray heuristic). ?filename=..."""
    return response_wrapper({'category': _projdoc_suggest_category(request.args.get('filename', ''))})


@app.route('/api/projects/<project_code>/documents/scan', methods=['POST'])
@requires_role(*_PROJDOC_ROLES)
def api_project_documents_scan(project_code):
    """#234 Batch B — AI auto-read ONE document on upload. Sends ALL pages (a
    multi-page PDF and/or images) to the vision model in a SINGLE call with the
    docs taxonomy (6 categories + the required-doc checklist) embedded, and
    returns SUGGESTIONS for the modal to confirm: title, doc_type, category,
    requirement_key, effective_date, expiry_date (LOCAL), confidence + warnings +
    which fields to double-check. Does NOT save — the operator confirms and the
    Batch-A /documents upload persists. Key from ENV only; missing key -> clean
    503 -> manual entry. The file IS sent to the Anthropic API (same trust as
    receipts/certs). No *_path; gated like the docs module; served no-store."""
    import document_scanner as scanner
    files = (request.files.getlist('file') or request.files.getlist('files')
             or request.files.getlist('pages'))
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify({"error": "no file"}), 400
    if len(files) > scanner.MAX_PAGES:
        return jsonify({"error": f"Too many pages — max {scanner.MAX_PAGES}"}), 400
    for f in files:
        if Path(f.filename or '').suffix.lower() not in _PROJDOC_EXT_TYPE:
            return jsonify({"error": "unsupported file type — use PDF, JPG, PNG, HEIC, Excel or CSV"}), 400
    # DOC_SCAN_FAKE (test-only, NEVER set in prod) bypasses the live model so the
    # pipeline can be proven deterministically when the API key is absent.
    fake_path = runtime_seams.seam('DOC_SCAN_FAKE')
    if not os.environ.get('ANTHROPIC_API_KEY') and not fake_path:
        return jsonify({"error": "AI auto-read unavailable — ANTHROPIC_API_KEY not configured. "
                                 "Enter the document details manually.", "ai_available": False}), 503
    conn = db()
    try:
        if not validate_project_exists(conn, project_code):
            return jsonify({"error": "project not found"}), 404
        req_rows = conn.execute(
            "SELECT category, requirement_key, label FROM document_requirements "
            "ORDER BY category, sort_order").fetchall()
    finally:
        conn.close()
    categories = [(c, _PROJDOC_CATNAMES.get(c, c)) for c in _PROJDOC_CATEGORIES]
    requirements = [(r['category'], r['requirement_key'], r['label']) for r in req_rows]
    req_by_cat = {}
    for cat, key, _label in requirements:
        req_by_cat.setdefault(cat, set()).add(key)
    # Read ALL pages into memory and send them in ONE call — no temp files, no
    # *_path. A multi-page PDF is a single document block (all pages, one read).
    specs = []
    for f in files:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(0)
        if size > scanner.MAX_FILE_BYTES:
            return jsonify({"error": "a page is too large (max 16 MB)"}), 400
        try:
            specs.append(scanner.spec_from_bytes(f.read(), f.filename))
        except scanner.ScanError:
            return jsonify({"error": "That spreadsheet couldn't be opened. Check the file and try again."}), 400
    try:
        if fake_path:
            with open(fake_path, encoding='utf-8') as fh:
                raw = json.load(fh)
        else:
            raw = scanner.call_vision_model(specs, categories, requirements)
    except scanner.ScanUnavailable:
        return jsonify({"error": "AI auto-read unavailable — enter manually.", "ai_available": False}), 503
    except scanner.ScanError:
        return jsonify({"error": "Couldn't read the document automatically — enter the details manually.",
                        "scan_failed": True}), 502
    except Exception as e:
        logging.error(f"POST documents/scan: {type(e).__name__}: {e}")
        return jsonify({"error": "Couldn't read the document — enter the details manually.",
                        "scan_failed": True}), 502
    suggestion = scanner.process_scan_result(raw, set(_PROJDOC_CATEGORIES), req_by_cat)
    if not suggestion.get('page_count'):
        suggestion['page_count'] = len(specs)
    suggestion['model'] = (raw.get('_meta') or {}).get('model') or scanner.MODEL
    resp = response_wrapper(suggestion)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


# ---------- Documents drop zone: intake -> Claude reads -> approve the tag -> filed ----------
import doc_intake  # noqa: E402
import doc_types  # noqa: E402
import doc_suggest  # noqa: E402

try:
    _di_conn = db()
    try:
        doc_intake.ensure_schema(_di_conn)
        db_layer._PKMAP_CACHE.clear()
    finally:
        _di_conn.close()
except Exception as _di_e:   # never blocks boot; apply_doc_intake.py is the migration
    logging.warning(f"doc-intake schema ensure skipped: {_di_e}")


def _projdoc_scope_err(conn, project_code):
    """404 for an unknown project, 403 when a PM/super isn't on it, else None."""
    if not validate_project_exists(conn, project_code):
        return jsonify({"error": "project not found"}), 404
    user = current_user() or {}
    if not pm_scoping.pm_can_access_project(user.get("role"), user.get("id"), project_code, conn):
        return jsonify({"error": "forbidden"}), 403
    return None


def _intake_row(conn, intake_id):
    """(row, None) for an intake item the current user may act on, else (None, error)."""
    row = conn.execute("SELECT * FROM project_doc_intake WHERE id=?", (intake_id,)).fetchone()
    if not row:
        return None, (jsonify({"error": "not found"}), 404)
    err = _projdoc_scope_err(conn, row['project_code'])
    if err:
        return None, err
    return row, None


def _projdoc_unlink(path_text):
    """Delete a stored file, confined to data_room/project_docs/."""
    try:
        p = ssc_paths.resolve_data_path(path_text)
        base = (ssc_paths.under_root('data_room', 'project_docs')).resolve()
        if p.resolve().is_relative_to(base):
            p.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def _intake_read(conn_factory, project_code, data_bytes, filename):
    """Run Claude's read on one stored file. Returns (status, suggestion|None, error|None).
    The DB connection is NOT held across the model call."""
    import document_scanner as scanner
    fake_path = runtime_seams.seam('DOC_SCAN_FAKE')
    if not os.environ.get('ANTHROPIC_API_KEY') and not fake_path:
        return 'unread', None, "AI reading is off — pick the type yourself."
    if len(data_bytes) > scanner.MAX_FILE_BYTES:
        return 'unread', None, "Too large for Claude to read (max 16 MB) — pick the type yourself."
    conn = conn_factory()
    try:
        req_rows = conn.execute(
            "SELECT category, requirement_key, label FROM document_requirements "
            "ORDER BY category, sort_order").fetchall()
    finally:
        conn.close()
    categories = [(c, _PROJDOC_CATNAMES.get(c, c)) for c in _PROJDOC_CATEGORIES]
    requirements = [(r['category'], r['requirement_key'], r['label']) for r in req_rows]
    req_by_cat = {}
    for cat, key, _label in requirements:
        req_by_cat.setdefault(cat, set()).add(key)
    try:
        if fake_path:
            with open(fake_path, encoding='utf-8') as fh:
                raw = json.load(fh)
        else:
            raw = scanner.call_vision_model([scanner.spec_from_bytes(data_bytes, filename)],
                                            categories, requirements)
    except scanner.ScanUnavailable:
        return 'unread', None, "AI reading is off — pick the type yourself."
    except Exception as e:   # ScanError + anything unexpected; never log document content
        logging.error(f"doc intake read failed: {type(e).__name__}")
        return 'unread', None, "Claude couldn't read this one — pick the type yourself."
    sug = scanner.process_scan_result(raw, set(_PROJDOC_CATEGORIES), req_by_cat)
    sug['model'] = (raw.get('_meta') or {}).get('model') or scanner.MODEL
    return 'ready', sug, None


@app.route('/api/documents/types', methods=['GET'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_types():
    """The document types the drop zone tags with (key, label, category, requirement_key)."""
    return response_wrapper({'types': doc_types.public_list(),
                             'categories': [{'code': c, 'name': _PROJDOC_CATNAMES[c]} for c in _PROJDOC_CATEGORIES]})


@app.route('/api/projects/<project_code>/documents/intake', methods=['GET'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_intake_list(project_code):
    """Files dropped on this project still waiting for their tag to be approved."""
    conn = db()
    try:
        err = _projdoc_scope_err(conn, project_code)
        if err:
            return err
        rows = conn.execute("SELECT * FROM project_doc_intake WHERE project_code=? ORDER BY id",
                            (project_code,)).fetchall()
        resp = response_wrapper({'items': [doc_intake.intake_public(r) for r in rows]})
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    finally:
        conn.close()


@app.route('/api/projects/<project_code>/documents/intake', methods=['POST'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_intake_add(project_code):
    """Drop ONE file: save it now, have Claude read it, return the review card.
    The file is sent to the Anthropic API (same trust as /documents/scan). Nothing is
    filed until /approve. Without an API key the file still waits, as 'unread'."""
    fs = request.files.get('file')
    if not fs or not fs.filename:
        return jsonify({"error": "no file"}), 400
    conn = db()
    try:
        err = _projdoc_scope_err(conn, project_code)
        if err:
            return err
        try:
            fpath, ftype, mime, size = _projdoc_save_file(project_code, fs)
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        file_name = Path(fs.filename).name
        uid = (current_user() or {}).get('id')
        sha = hashlib.sha256(fpath.read_bytes()).hexdigest()
        try:
            cur = conn.execute(
                "INSERT INTO project_doc_intake (project_code, file_path, file_name, file_size, mime, "
                "doc_type, status, uploaded_by_uid, created_at, content_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (project_code, str(fpath), file_name, size, mime, ftype, 'unread', uid,
                 datetime.now().isoformat(timespec='seconds'), sha))
            intake_id = cur.lastrowid
            conn.commit()
        except Exception:
            _projdoc_unlink(str(fpath))
            raise
        # The same file already waiting (a double drop, a second tab): keep the first, drop
        # this copy. Checked after the insert so two drops racing each other still end as one.
        first = conn.execute("SELECT * FROM project_doc_intake WHERE project_code=? AND content_sha256=? "
                             "ORDER BY id LIMIT 1", (project_code, sha)).fetchone()
        if first and first['id'] != intake_id:
            conn.execute("DELETE FROM project_doc_intake WHERE id=?", (intake_id,))
            conn.commit()
            _projdoc_unlink(str(fpath))
            out = doc_intake.intake_public(first)
            out['duplicate_of'] = first['id']
            resp = response_wrapper(out)
            resp.headers['Cache-Control'] = 'no-store'
            return resp, 200
    finally:
        conn.close()
    status, sug, read_err = _intake_read(db, project_code, fpath.read_bytes(), file_name)
    conn = db()
    try:
        conn.execute("UPDATE project_doc_intake SET status=?, suggestion_json=?, error=? WHERE id=?",
                     (status, json.dumps(sug, ensure_ascii=False) if sug else None, read_err, intake_id))
        conn.commit()
        row = conn.execute("SELECT * FROM project_doc_intake WHERE id=?", (intake_id,)).fetchone()
        resp = response_wrapper(doc_intake.intake_public(row))
        resp.headers['Cache-Control'] = 'no-store'
        return resp, 201
    finally:
        conn.close()


@app.route('/api/documents/intake/<int:intake_id>/file', methods=['GET'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_intake_file(intake_id):
    """View a waiting file (inline, gated, confined to data_room/project_docs/)."""
    from flask import send_file
    conn = db()
    try:
        row, err = _intake_row(conn, intake_id)
        if err:
            return err
        p = ssc_paths.resolve_data_path(row['file_path'])
        base = (ssc_paths.under_root('data_room', 'project_docs')).resolve()
        if not (p.resolve().is_relative_to(base) and p.exists()):
            return jsonify({"error": "file missing"}), 404
        resp = send_file(str(p), mimetype=row['mime'] or 'application/octet-stream',
                         as_attachment=False, download_name=(row['file_name'] or f"document-{intake_id}"))
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp
    finally:
        conn.close()


@app.route('/api/documents/intake/<int:intake_id>/approve', methods=['POST'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_intake_approve(intake_id):
    """File a waiting document under the approved tag. Body {type_key, title?,
    effective_date?, expiry_date?, supersedes_id?, party?, party_name?}. party = whose
    record it is (company / subcontractor / owner / other; Claude's read when omitted). The tag decides the folder and the
    checklist item. supersedes_id = 'newer version of' (same rules as Update: the old
    version moves to History and loses its client shares; the new one starts internal)."""
    body = request.get_json(silent=True) or {}
    t = doc_types.get(body.get('type_key'))
    if not t:
        return jsonify({"error": "pick a document type"}), 400
    conn = db()
    try:
        row, err = _intake_row(conn, intake_id)
        if err:
            return err
        code = row['project_code']
        try:
            sug = json.loads(row['suggestion_json']) if row['suggestion_json'] else None
        except ValueError:
            sug = None
        sug = sug or {}
        valid_reqs = {r['requirement_key'] for r in conn.execute(
            "SELECT requirement_key FROM document_requirements WHERE category=?", (t['category'],)).fetchall()}
        rk = t['requirement_key'] if t['requirement_key'] in valid_reqs else None
        title = (str(body.get('title') or '').strip() or sug.get('title') or row['file_name'] or 'Document')[:300]
        dates = {}
        for k in ('effective_date', 'expiry_date'):
            v = body[k] if k in body else sug.get(k)
            v = (str(v).strip() if v else '') or None
            if v:
                try:
                    datetime.strptime(v, '%Y-%m-%d')   # LOCAL YYYY-MM-DD
                except ValueError:
                    return jsonify({"error": f"{k} must be YYYY-MM-DD"}), 400
            dates[k] = v
        supersedes_id = body.get('supersedes_id')
        if supersedes_id not in (None, ''):
            try:
                supersedes_id = int(supersedes_id)
            except (TypeError, ValueError):
                return jsonify({"error": "supersedes_id must be an integer"}), 400
            old = conn.execute("SELECT project_code, superseded FROM project_documents WHERE id=?",
                               (supersedes_id,)).fetchone()
            if not old or old['project_code'] != code:
                return jsonify({"error": "document to update not found in this project"}), 404
            if old['superseded']:
                return jsonify({"error": "that version is already superseded"}), 409
        else:
            supersedes_id = None
        set_by = 'ai' if sug.get('type_key') == t['key'] else 'user'
        if 'party' in body:
            party, party_name = doc_intake.clean_party(body.get('party'), body.get('party_name'))
        else:
            party, party_name = doc_intake.clean_party(sug.get('party'), sug.get('party_name'))
        uid = (current_user() or {}).get('id')
        cur = conn.execute(
            "INSERT INTO project_documents (project_code, category, requirement_key, title, doc_type, "
            "file_path, file_name, file_size, mime, effective_date, expiry_date, version, notes, "
            "superseded, uploaded_by_uid, uploaded_at, supersedes_id, type_key, ai_extract, party, party_name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)",
            (code, t['category'], rk, title, row['doc_type'], row['file_path'], row['file_name'],
             row['file_size'], row['mime'], dates['effective_date'], dates['expiry_date'], None, None,
             uid, datetime.now().isoformat(), supersedes_id, t['key'],
             doc_intake.extract_for_storage(sug if sug else None, set_by), party, party_name))
        new_id = cur.lastrowid
        if supersedes_id is not None:
            conn.execute("UPDATE project_documents SET superseded=1 WHERE id=?", (supersedes_id,))
            for aud in visibility.audiences_for(conn, 'document', supersedes_id):
                visibility.unshare(conn, 'document', supersedes_id, aud, uid)
        conn.execute("DELETE FROM project_doc_intake WHERE id=?", (intake_id,))
        conn.commit()
        today = date.today()
        fresh = dict(conn.execute("SELECT * FROM project_documents WHERE id=?", (new_id,)).fetchone())
        return response_wrapper(_projdoc_public(fresh, today.isoformat(),
                                                (today + timedelta(days=30)).isoformat())), 201
    except Exception as e:
        logging.error(f"POST documents/intake/{intake_id}/approve: {type(e).__name__}")
        return _server_error(e)
    finally:
        conn.close()


@app.route('/api/projects/<project_code>/documents/suggestions/dismiss', methods=['POST'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_suggestion_dismiss(project_code):
    """Mark a suggested document 'not needed' for this job. Body {key}."""
    key = str((request.get_json(silent=True) or {}).get('key') or '').strip()
    if not key or len(key) > 300 or not doc_types.get(key.split('|', 1)[0]):
        return jsonify({"error": "unknown suggestion"}), 400
    conn = db()
    try:
        err = _projdoc_scope_err(conn, project_code)
        if err:
            return err
        if not conn.execute("SELECT 1 FROM project_doc_suggest_dismissed WHERE project_code=? AND suggestion_key=?",
                            (project_code, key)).fetchone():
            conn.execute("INSERT INTO project_doc_suggest_dismissed (project_code, suggestion_key, dismissed_by_uid, "
                         "dismissed_at) VALUES (?,?,?,?)",
                         (project_code, key, (current_user() or {}).get('id'),
                          datetime.now().isoformat(timespec='seconds')))
            conn.commit()
        return response_wrapper({'dismissed': key})
    finally:
        conn.close()


@app.route('/api/documents/intake/<int:intake_id>', methods=['DELETE'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_intake_delete(intake_id):
    """Remove a waiting file (row + file). Filed documents are untouched."""
    conn = db()
    try:
        row, err = _intake_row(conn, intake_id)
        if err:
            return err
        conn.execute("DELETE FROM project_doc_intake WHERE id=?", (intake_id,))
        conn.commit()
        _projdoc_unlink(row['file_path'])
        return response_wrapper({'deleted': intake_id})
    finally:
        conn.close()


@app.route('/api/documents/<int:doc_id>/file', methods=['GET'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_file(doc_id):
    """Serve a document's bytes through THIS auth-gated route ONLY. Never exposes the
    path; confined to data_room/project_docs/. no-store (docs may be sensitive).
    #271 — VIEW, NOT PRINT: Content-Disposition is EXPLICITLY inline (with the original
    filename for a deliberate save-as), so a title click renders the PDF in the tab;
    printing is only ever the operator's manual choice. No print-intent exists on this
    path — guarded against regression by smoke_docs_photos_271."""
    from flask import send_file
    conn = db()
    try:
        row = conn.execute("SELECT file_path, mime, file_name FROM project_documents WHERE id=?",
                           (doc_id,)).fetchone()
        if not row or not row['file_path']:
            return jsonify({"error": "not found"}), 404
        p = ssc_paths.resolve_data_path(row['file_path'])   # #305 — legacy absolute rows resolve on the cloud
        base = (ssc_paths.under_root('data_room', 'project_docs')).resolve()
        if not (p.resolve().is_relative_to(base) and p.exists()):
            return jsonify({"error": "file missing"}), 404
        resp = send_file(str(p), mimetype=row['mime'] or 'application/octet-stream',
                         as_attachment=False, download_name=(row['file_name'] or f"document-{doc_id}"))
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp
    finally:
        conn.close()


_SHEET_PREVIEW_MAX_BYTES = 32 * 1024 * 1024


def _projdoc_sheet_resp(path_text, file_name):
    """The cells of a stored spreadsheet for the in-app preview (sheet_read.grid). Confined to
    data_room/project_docs/; values as Excel last saved them (no formulas run, no macros)."""
    import sheet_read
    ext = Path(file_name or '').suffix.lower()
    if ext not in sheet_read.EXTS:
        return jsonify({"error": "not a spreadsheet"}), 400
    p = ssc_paths.resolve_data_path(path_text)
    base = (ssc_paths.under_root('data_room', 'project_docs')).resolve()
    if not (p.resolve().is_relative_to(base) and p.exists()):
        return jsonify({"error": "file missing"}), 404
    if p.stat().st_size > _SHEET_PREVIEW_MAX_BYTES:
        return jsonify({"error": "This spreadsheet is too large to show here. Use Download to open it."}), 413
    try:
        out = sheet_read.grid(p.read_bytes(), ext)
    except Exception as e:   # never log cell content
        logging.info(f"sheet preview failed: {type(e).__name__}")
        return jsonify({"error": "This spreadsheet couldn't be opened here. Use Download to open it."}), 422
    out['preview_rows'] = sheet_read.PREVIEW_ROWS
    resp = response_wrapper(out)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/documents/<int:doc_id>/sheet', methods=['GET'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_sheet(doc_id):
    """Preview a filed spreadsheet (any version) as cells, on the job's own scope."""
    conn = db()
    try:
        row = conn.execute("SELECT project_code, file_path, file_name FROM project_documents WHERE id=?",
                           (doc_id,)).fetchone()
        if not row or not row['file_path']:
            return jsonify({"error": "not found"}), 404
        err = _projdoc_scope_err(conn, row['project_code'])
        if err:
            return err
    finally:
        conn.close()
    return _projdoc_sheet_resp(row['file_path'], row['file_name'])


@app.route('/api/documents/intake/<int:intake_id>/sheet', methods=['GET'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_intake_sheet(intake_id):
    """Preview a spreadsheet still waiting in the drop-zone tray."""
    conn = db()
    try:
        row, err = _intake_row(conn, intake_id)
        if err:
            return err
    finally:
        conn.close()
    return _projdoc_sheet_resp(row['file_path'], row['file_name'])


@app.route('/api/documents/<int:doc_id>/replace-file', methods=['POST'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_replace_file(doc_id):
    """#271 — REPLACE: the 'wrong file was uploaded' fix ONLY. Swaps the FILE on this
    SAME version row (path/name/size/mime/doc_type updated, old file deleted from disk),
    creates NO history entry, changes NO version/share/flag state. The everyday renewal
    action is Update (a new version via the upload endpoint's supersedes_id)."""
    conn = db()
    try:
        code, err = _doc_actor_can_admin(conn, doc_id)
        if err:
            return err
        if 'file' not in request.files:
            return jsonify({"error": "no file"}), 400
        row = conn.execute("SELECT file_path FROM project_documents WHERE id=?", (doc_id,)).fetchone()
        old_path = Path(row['file_path']) if row and row['file_path'] else None
        try:
            fpath, doc_type, mime, size = _projdoc_save_file(code, request.files['file'])
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        file_name = Path(request.files['file'].filename or 'document').name
        try:
            conn.execute(
                "UPDATE project_documents SET file_path=?, file_name=?, file_size=?, mime=?, doc_type=? "
                "WHERE id=?",
                (str(fpath), file_name, size, mime, doc_type, doc_id))
            conn.commit()
        except Exception:
            try:
                fpath.unlink(missing_ok=True)   # roll the new file back; row unchanged
            except Exception:
                pass
            raise
        # the wrong file is gone for good (confined delete; the ROW/version is untouched)
        base = (ssc_paths.under_root('data_room', 'project_docs')).resolve()
        if old_path is not None:
            try:
                if old_path.resolve().is_relative_to(base):
                    old_path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        today = date.today()
        fresh = dict(conn.execute("SELECT * FROM project_documents WHERE id=?", (doc_id,)).fetchone())
        resp = response_wrapper(_projdoc_public(fresh, today.isoformat(),
                                                (today + timedelta(days=30)).isoformat()))
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    finally:
        conn.close()


@app.route('/api/documents/<int:doc_id>', methods=['PATCH'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_patch(doc_id):
    """#229 — edit metadata / mark superseded. Whitelisted fields only; never the path.
    title = rename (the name shown on the row; the stored file name is unchanged)."""
    conn = db()
    try:
        _code, err = _doc_actor_can_admin(conn, doc_id)
        if err:
            return err
        body = request.get_json(silent=True) or {}
        fields = {}
        for k in ('category', 'requirement_key', 'title', 'effective_date', 'expiry_date', 'version', 'notes', 'superseded'):
            if k in body:
                fields[k] = body[k]
        if 'title' in fields:
            fields['title'] = " ".join(str(fields['title'] or '').split())[:300]
            if not fields['title']:
                return jsonify({"error": "the name can't be empty"}), 400
        if 'party' in body:
            fields['party'], fields['party_name'] = doc_intake.clean_party(body.get('party'), body.get('party_name'))
        if 'category' in fields and fields['category'] not in _PROJDOC_CATEGORIES:
            return jsonify({"error": "invalid category"}), 400
        for dk in ('effective_date', 'expiry_date'):
            if fields.get(dk):
                try:
                    datetime.strptime(fields[dk], '%Y-%m-%d')
                except ValueError:
                    return jsonify({"error": f"{dk} must be YYYY-MM-DD"}), 400
        if 'superseded' in fields:
            fields['superseded'] = 1 if fields['superseded'] else 0
        if not fields:
            return jsonify({"error": "no fields"}), 400
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE project_documents SET {sets} WHERE id=?", (*fields.values(), doc_id))
        conn.commit()
        today = date.today()
        row = dict(conn.execute("SELECT * FROM project_documents WHERE id=?", (doc_id,)).fetchone())
        return response_wrapper(_projdoc_public(row, today.isoformat(), (today + timedelta(days=30)).isoformat()))
    finally:
        conn.close()


@app.route('/api/documents/<int:doc_id>', methods=['DELETE'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_delete(doc_id):
    """#229 — remove the row + its on-disk file (confined to data_room/project_docs/).
    Deleting the current version brings the previous one back as current (it stays
    internal; its shares were pulled when it was superseded). A newer version that
    pointed at the deleted row now points at the one before it, so History stays whole."""
    conn = db()
    try:
        _code, err = _doc_actor_can_admin(conn, doc_id)
        if err:
            return err
        row = conn.execute("SELECT file_path, superseded, supersedes_id FROM project_documents WHERE id=?",
                           (doc_id,)).fetchone()
        prev_id = row['supersedes_id']
        restored = None
        if not row['superseded'] and prev_id and conn.execute(
                "SELECT 1 FROM project_documents WHERE id=? AND superseded=1", (prev_id,)).fetchone():
            conn.execute("UPDATE project_documents SET superseded=0 WHERE id=?", (prev_id,))
            restored = prev_id
        conn.execute("UPDATE project_documents SET supersedes_id=? WHERE supersedes_id=?", (prev_id, doc_id))
        conn.execute("DELETE FROM item_visibility WHERE item_type='document' AND item_id=?", (doc_id,))
        conn.execute("DELETE FROM item_redflag WHERE item_type='document' AND item_id=?", (doc_id,))
        conn.execute("DELETE FROM project_documents WHERE id=?", (doc_id,))
        conn.commit()
        if row['file_path']:
            _projdoc_unlink(row['file_path'])
        return response_wrapper({"deleted": doc_id, "restored": restored})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reports Phase 1 — shared spine endpoints (read-only)
# ---------------------------------------------------------------------------
# Both endpoints below serve `project_type_config.py` — the single source
# that mirrors construction_builds_spec.json. Every future Weekly Summary /
# Two-Week Look-Ahead / RFI Log build (Phases 2-4) reads project_type-
# specific lists from THIS endpoint, never from a local copy. Read
# REPORTS_PHASE1_SHARED_SPINE.md for the consumer guide.
# ---------------------------------------------------------------------------

@app.route('/api/project-types', methods=['GET'])
def api_project_types():
    """All project_type definitions + shared field schema, in one shot.

    Phase-2/3/4 consumers that need to render a project-type picker (or
    iterate every type's option lists for option pre-population) call this.
    Single-project consumers should prefer /api/projects/<code>/project-config
    which resolves the project_code -> exactly one type's payload.
    """
    from project_type_config import (
        PROJECT_TYPES, STATUS_ENUM, RFI_STATUS_SUBSET,
        SHARED_FIELD_DEFS, LOCATION_REFERENCE_SCHEMA, SPINE_VERSION,
    )
    return response_wrapper({
        "spine_version": SPINE_VERSION,
        "project_types": PROJECT_TYPES,
        "status_enum": STATUS_ENUM,
        "rfi_status_subset": RFI_STATUS_SUBSET,
        "shared_fields": SHARED_FIELD_DEFS,
        "location_reference": LOCATION_REFERENCE_SCHEMA,
    })


@app.route('/api/projects/<project_code>/project-config', methods=['GET'])
def api_project_config(project_code):
    """Resolved project-type config for a specific project.

    Looks up `project_code` -> projects.project_type, then returns that
    type's `location_unit_options`, `typical_scopes`, `typical_inspections`,
    `typical_long_lead`, plus the shared field defs + location_reference
    shape. This is the endpoint Phase-2 RFI Log / Look-Ahead UIs hit when
    rendering option dropdowns + flag controls for a single project.
    """
    from project_type_config import get_project_config_for
    conn = db()
    try:
        cfg = get_project_config_for(conn, project_code)
    finally:
        conn.close()
    if cfg is None:
        return jsonify({"error": "Project not found"}), 404
    return response_wrapper(cfg)


@app.route('/api/projects/<project_code>/weekly/render', methods=['GET'])
def api_project_weekly_render(project_code):
    """LIVE-render the Weekly Summary Report for `project_code` (Phase 4).

    Per construction_builds_spec.json weekly_summary_report, this is an
    AGGREGATION of the week's daily reports — no independent data entry.
    Pulls live RFI counts (linkage_rules.rfi_to_reports) and references
    the Look-Ahead.

    Query params:
      week_ending  (optional) ISO YYYY-MM-DD; defaults to the most
                   recent completed Friday (mirrors the Hours Log's
                   last_completed_week semantics).
      audience     'internal' (default) | 'owner' — toggles section set.

    Returns HTML (Content-Type text/html). Matches the Phase-3 Look-Ahead
    + Phase-2 RFI live-render shape.
    """
    from render_weekly_summary_v2 import render_weekly_summary_html
    week_ending = (request.args.get('week_ending') or '').strip() or None
    audience = (request.args.get('audience') or 'internal').strip().lower()
    if audience not in ('internal', 'owner'):
        return jsonify({"error": "audience must be 'internal' or 'owner'"}), 400
    if week_ending:
        try:
            datetime.strptime(week_ending, '%Y-%m-%d')
        except ValueError:
            return jsonify({"error": "week_ending must be YYYY-MM-DD"}), 400
    try:
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 404
        html = render_weekly_summary_html(
            conn, project_code,
            week_ending_iso=week_ending, audience=audience,
        )
        conn.close()
        return Response(html, mimetype='text/html')
    except Exception as e:
        logging.error(f"GET /api/projects/{project_code}/weekly/render: {str(e)}")
        return _server_error(e)


@app.route('/api/projects/<project_code>/lookahead/render', methods=['GET'])
def api_project_lookahead_render(project_code):
    """LIVE-render the Two-Week Look-Ahead for `project_code` (Phase 3).

    Query params:
      start         (optional) ISO YYYY-MM-DD; default = LOCAL today
      working_days  (optional) int; default = 10 (the 2-week window)

    Returns HTML (Content-Type text/html). Mirrors the live-render pattern
    /api/rfis/<rfi_id>/render uses for Phase 2 — read DB now, render now,
    no on-disk staging files. Per linkage_rules.rfi_to_lookahead, the
    Constraints section reads open / overdue schedule-impact RFIs (the
    same set /api/projects/<code>/rfi-constraints returns) and joins
    them to drops by location_id.
    """
    from render_lookahead_v2 import render_lookahead_html
    start = (request.args.get('start') or '').strip() or None
    try:
        working_days = int(request.args.get('working_days') or 10)
    except ValueError:
        return jsonify({"error": "working_days must be an integer"}), 400
    if working_days < 1 or working_days > 20:
        return jsonify({"error": "working_days must be 1..20"}), 400
    if start:
        try:
            datetime.strptime(start, '%Y-%m-%d')
        except ValueError:
            return jsonify({"error": "start must be YYYY-MM-DD"}), 400
    try:
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 404
        html = render_lookahead_html(conn, project_code, start_iso=start,
                                     working_days=working_days)
        conn.close()
        return Response(html, mimetype='text/html')
    except Exception as e:
        logging.error(f"GET /api/projects/{project_code}/lookahead/render: {str(e)}")
        return _server_error(e)


@app.route('/api/blank-forms', methods=['GET'])
def api_blank_forms():
    """Catalog of reusable blank (template) forms.

    Returns every row in blank_forms with a file_url synthesized for the
    gated /project-files/data_room/forms/<filename> route so the operator's
    UI can link straight to the file without a second round-trip.

    Optional category filter.
    """
    cat = (request.args.get('category') or '').strip()
    where = []
    params = []
    if cat:
        where.append("category = ?")
        params.append(cat)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        conn = db()
        rows = conn.execute(
            f"SELECT id, title, filename, category, description, mime_type, "
            f"       created_at FROM blank_forms{where_sql} "
            f"ORDER BY category, title",
            params,
        ).fetchall()
        conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d['file_url'] = '/project-files/data_room/forms/' + d['filename']
            out.append(d)
        return response_wrapper(out, count=len(out))
    except Exception as e:
        logging.error(f"GET /api/blank-forms: {str(e)}")
        return _server_error(e)


# ====================================================================
# Labor Rates admin (#158) — admin/c_suite only
# ====================================================================
# These endpoints carry COMPENSATION data. The blanket auth gate
# ensures a session exists; @requires_role('admin','c_suite') ensures
# the caller is authorized to see/edit rates. Anyone else gets 403.
#
# PII rule: rate values must never appear in server.log. The logging
# in this section uses counts / booleans / employee_id only — never
# the rate dollar amount.
# ====================================================================

# ====================================================================
# Batch credential print (#174 / #178) — admin/c_suite only
# ====================================================================
# Generates a single PDF bundle containing every active worker's
# credential card (CoF for W-0001..W-0012, Company ID for W-0013/14)
# laid out 4-up on Letter LANDSCAPE with 2.00" lamination spacing
# between adjacent cards. Back-pages tile a single shared back design
# 4x (no mirroring), so any duplex mode prints correctly. Operator
# prints duplex, cuts at the visible 2" gaps, laminates.

@app.route('/api/credentials/batch-print', methods=['POST', 'GET'])
@requires_role('admin', 'c_suite')
def api_credentials_batch_print():
    """Generate (and serve) the credentials batch-print PDF.

    Hash-cached as of #189. The bundle generator computes a fingerprint
    of the input set (workers, photos, rigger info, template mtimes,
    BUNDLE_FORMAT_VERSION); if a cached PDF for that fingerprint exists,
    it's served instantly. Otherwise the bundle is regenerated, saved
    with the new fingerprint in its on-disk name, and served. The
    download `filename` in the response stays operator-friendly
    (SuperstarsContracting-AllIDs-<YYYY-MM-DD>.pdf); the on-disk URL
    embeds the fingerprint so cache lookups are O(1).

    Cache decision (#190 fix — previously POST forced regen, which
    meant every button click was a fresh 5s render and the cache
    never benefited the operator). New rule:

      POST and GET both serve from cache when the fingerprint matches.
      Only `?force=1` triggers a forced regen (reserved for a future
      "regenerate now" admin override; not exposed in the UI today).

    The button keeps POST as its method — that's CSRF protection via
    the existing session machinery, NOT a hint to bypass the cache.
    """
    import generate_credentials_batch as gcb
    from datetime import date as _date
    try:
        today = _date.today().isoformat()
        # Force regen only on explicit ?force=1. Both POST and GET
        # otherwise serve from cache when the fingerprint matches.
        force = (request.args.get('force') == '1')
        result = gcb.main(
            base_url="http://127.0.0.1:5050",
            force_regenerate=force,
        )
        if not isinstance(result, dict) or result.get("status") not in (
            "cache_hit", "cache_miss"
        ):
            return jsonify({"error": "batch-print render failed",
                            "status": (result or {}).get("status")
                                      if isinstance(result, dict) else str(result)}), 500
        output = result["output_path"]
        # #295 S2 — data-root anchor (containment-anchor family): the batch PDF
        # lives under the renders root, which is /var/data on the cloud.
        rel = output.resolve().relative_to(ssc_paths.data_root().resolve()).as_posix()
        friendly_filename = f"SuperstarsContracting-AllIDs-{today}.pdf"
        logging.info(
            f"credentials batch-print served: status={result['status']} "
            f"fingerprint={result.get('fingerprint')} "
            f"path={rel} ({output.stat().st_size} bytes)"
        )
        return response_wrapper({
            "url": "/project-files/" + rel,
            # Operator-facing download name — the on-disk path includes
            # the fingerprint, but the browser saves it under the
            # friendly dated name (via the JS anchor's download attr).
            "filename": friendly_filename,
            "size": output.stat().st_size,
            "generated_at": today,
            "cache_status": result["status"],
            "fingerprint": result.get("fingerprint"),
            "timing_ms": int(result.get("stage_t", {}).get("total", 0) * 1000),
        })
    except Exception as e:
        logging.error(f"{request.method} /api/credentials/batch-print: {str(e)}")
        return _server_error(e)


@app.route('/api/labor-rates/workers', methods=['GET'])
@requires_section('financial')
def api_labor_rates_workers():
    """Per-worker overview: every active worker with their current rate
    (or null if no rate set yet) + a count of historical rate rows.

    Used by the Labor Rates admin page to render one row per worker.
    """
    from worker_rates import get_current_rate
    try:
        conn = db()
        try:
            rows = conn.execute(
                # #260 — sort key in the SELECT list: Postgres requires SELECT
                # DISTINCT ORDER BY expressions to be selected (SQLite did not).
                """SELECT DISTINCT e.employee_id, e.worker_id, e.name, e.trade,
                          CAST(SUBSTR(e.employee_id, 3) AS INTEGER) AS sort_key
                   FROM employees e
                   JOIN project_assignments pa ON pa.employee_id = e.employee_id
                   WHERE pa.status = 'active'
                   ORDER BY sort_key"""
            ).fetchall()
            out = []
            for w in rows:
                eid = w['employee_id']
                cur_rate = get_current_rate(conn, eid)
                hist_count = conn.execute(
                    "SELECT COUNT(*) FROM worker_rates WHERE employee_id = ?", (eid,)
                ).fetchone()[0]
                d = {
                    'employee_id': eid,
                    'worker_id': w['worker_id'],
                    'name': w['name'],
                    'trade': w['trade'],
                    'history_count': hist_count,
                }
                if cur_rate:
                    d['current_rate'] = round(float(cur_rate['hourly_rate']), 2)
                    d['current_effective_from'] = cur_rate['effective_from']
                    d['current_notes'] = cur_rate['notes']
                # else: rate_not_set is implied by absence of current_rate
                out.append(d)
        finally:
            conn.close()
        return response_wrapper(out, count=len(out))
    except Exception as e:
        # Operation only — never echo rate values.
        logging.error(f"GET /api/labor-rates/workers: {str(e)}")
        return _server_error(e)


@app.route('/api/labor-rates/workers/<employee_id>/history', methods=['GET'])
@requires_section('financial')
def api_labor_rates_history(employee_id):
    """Full rate history for one worker, newest first."""
    from worker_rates import get_rate_history
    try:
        conn = db()
        try:
            rows = get_rate_history(conn, employee_id)
            # Each row's hourly_rate is rounded for display consistency.
            for r in rows:
                if 'hourly_rate' in r and r['hourly_rate'] is not None:
                    r['hourly_rate'] = round(float(r['hourly_rate']), 2)
        finally:
            conn.close()
        return response_wrapper(rows, count=len(rows))
    except Exception as e:
        logging.error(f"GET /api/labor-rates/workers/{employee_id}/history: {str(e)}")
        return _server_error(e)


@app.route('/api/labor-rates/workers/<employee_id>', methods=['POST'])
@requires_section('financial')
def api_labor_rates_set(employee_id):
    """Create a new rate row, atomically end-dating the prior active one.

    Body JSON: {hourly_rate: float, effective_from: 'YYYY-MM-DD',
                notes?: str}

    Writes an audit_log entry. Never logs the rate value to server.log.
    """
    from worker_rates import set_rate, RateError
    try:
        body = request.get_json(silent=True) or {}
        user = current_user() or {}
        actor_id = user.get('id')
        actor_role = user.get('role')
        conn = db()
        try:
            new_row = set_rate(
                conn,
                employee_id=employee_id,
                hourly_rate=body.get('hourly_rate'),
                effective_from=(body.get('effective_from') or '').strip(),
                notes=body.get('notes'),
                actor_user_id=actor_id,
                actor_role=actor_role,
            )
        finally:
            conn.close()
        # Log the operation outcome with counts/IDs only — no rate value.
        logging.info(
            f"labor-rates: rate_change actor_id={actor_id} role={actor_role} "
            f"target={employee_id} ok=1"
        )
        # The response carries the new row including the rate — that's
        # fine, it's an authenticated admin/c_suite response over TLS.
        new_row['hourly_rate'] = round(float(new_row['hourly_rate']), 2)
        return response_wrapper(new_row), 201
    except RateError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logging.error(f"POST /api/labor-rates/workers/{employee_id}: {str(e)}")
        return _server_error(e)


# ====================================================================
# Sign-in ↔ DCR divergence invariant (#191) — defensive infrastructure
# ====================================================================
# See signin_dcr_reconcile.py for the architectural rationale. Short
# version: sign_in_log IS the source of truth; the issued DCR is a
# frozen rendering. _mark_dcr_stale flows DCR → log direction only,
# which means current code paths can't introduce divergence — but
# this endpoint surfaces any that DO appear (e.g., via manual DB
# manipulation or a future regression).

@app.route('/api/projects/<project_code>/signin-dcr-divergence', methods=['GET'])
@requires_role('admin', 'c_suite')
def api_signin_dcr_divergence(project_code):
    """Return the sign_in_log ↔ DCR divergence summary + per-(date,
    W-####) list for `project_code` over the date range. Read-only.

    Query params:
      week_start (optional, ISO Monday) — narrows to that Monday-Sunday (#310)
      start_date / end_date (optional, ISO) — explicit range
    If no params, the endpoint scans every issued DCR for the project.

    Response shape:
      {data: {
        project_code, start_date, end_date,
        summary: {total, in_dcr_not_log, in_log_not_dcr},
        divergences: [{date, worker_id, class, report_id}, ...]
      }}
    """
    import signin_dcr_reconcile as sdr
    try:
        start = (request.args.get('start_date') or '').strip() or None
        end = (request.args.get('end_date') or '').strip() or None
        week_start = (request.args.get('week_start') or '').strip()
        if week_start:
            monday = datetime.strptime(week_start, '%Y-%m-%d').date()
            if monday.weekday() != 0:
                return jsonify({"error": "week_start must be a Monday"}), 400
            start = monday.isoformat()
            end = (monday + timedelta(days=6)).isoformat()   # #310 — the payroll week is Mon-Sun
        conn = db()
        try:
            divergences = sdr.compute_divergences(
                conn, project_code, start_date=start, end_date=end
            )
            summary = sdr.divergence_summary(divergences)
        finally:
            conn.close()
        return response_wrapper({
            "project_code": project_code,
            "start_date": start,
            "end_date": end,
            "summary": summary,
            "divergences": divergences,
        })
    except Exception as e:
        logging.error(
            f"GET /api/projects/{project_code}/signin-dcr-divergence: {str(e)}"
        )
        return _server_error(e)


@app.route('/api/projects/<project_code>/signin-dcr-divergence/reconcile',
           methods=['POST'])
@requires_role('admin', 'c_suite')
def api_signin_dcr_reconcile(project_code):
    """Reconcile every `in_dcr_not_log` divergence for `project_code`
    over the given week (or full range). Inserts the missing
    sign_in_log rows from the DCR artifact, audit-logs each restore
    with PII-safe payloads. `in_log_not_dcr` divergences are returned
    in the response but NOT auto-deleted (operator decision).

    Body / query: same shape as the GET endpoint (week_start OR
    start_date+end_date). Returns the post-reconcile summary so the
    UI can re-render the banner without a second round-trip.
    """
    import signin_dcr_reconcile as sdr
    try:
        start = (request.args.get('start_date') or '').strip() or None
        end = (request.args.get('end_date') or '').strip() or None
        week_start = (request.args.get('week_start') or '').strip()
        if week_start:
            monday = datetime.strptime(week_start, '%Y-%m-%d').date()
            if monday.weekday() != 0:
                return jsonify({"error": "week_start must be a Monday"}), 400
            start = monday.isoformat()
            end = (monday + timedelta(days=6)).isoformat()   # #310 — the payroll week is Mon-Sun
        user = current_user() or {}
        conn = db()
        try:
            divergences = sdr.compute_divergences(
                conn, project_code, start_date=start, end_date=end
            )
            # Single transaction — the helper stages INSERTs on the
            # conn; commit here when the helper returns cleanly.
            reconcile_summary = sdr.reconcile_in_dcr_not_log(
                conn, project_code, divergences,
                actor_user_id=user.get('id'),
                actor_role=user.get('role') or 'system',
            )
            if reconcile_summary.get("errors"):
                conn.rollback()
            else:
                conn.commit()
            # Re-compute divergences post-reconcile so the UI gets
            # fresh state.
            post = sdr.compute_divergences(
                conn, project_code, start_date=start, end_date=end
            )
        finally:
            conn.close()
        logging.info(
            f"signin-dcr-reconcile: project={project_code} "
            f"week_start={week_start} "
            f"reconciled={reconcile_summary.get('reconciled')} "
            f"actor_role={user.get('role')}"
        )
        return response_wrapper({
            "project_code": project_code,
            "start_date": start,
            "end_date": end,
            "reconcile_summary": reconcile_summary,
            "post_divergences_summary": sdr.divergence_summary(post),
            "post_divergences": post,
        })
    except Exception as e:
        logging.error(
            f"POST /api/projects/{project_code}/signin-dcr-divergence/reconcile: "
            f"{str(e)}"
        )
        return _server_error(e)


# ============= LABOR RATES v2 (#220) — approval-gated rate management =============
# Layered on top of worker_rates (canonical, untouched). Keyed by worker_id so
# real + synthetic workers share one model. COMP DATA: full roster/history/submit
# = admin/c_suite; the pending-approval queue + approve/reject = admin/c_suite/pm
# (PMs get ONLY the queue, never the roster). Money via _exp_money (Decimal, 2dp);
# dates LOCAL; history is role-stamped (no names — PII-safe).
LABOR_TRADES = ('Mechanic', 'Laborer', 'Rope Access', 'Superintendent')
# #262 — sourced from the central section map (access.SECTION_ACCESS['financial']) so the
# Financial role set lives in ONE place: change it in access.py and BOTH the sidebar and
# these endpoints follow. Still admin/c_suite (#257: pm BLOCKED from individual rates).
_LR_FULL_ROLES = tuple(sorted(access.roles_for('financial')))
# #257 — individual worker rates/comp are admin/c_suite ONLY. pm was removed from
# rate approval here (catalog: c_suite/admin approve; pm must NOT see individual
# rates). Server-side enforced on every rates endpoint; the page is gated too.
_LR_APPROVE_ROLES = tuple(sorted(access.roles_for('financial')))

# #241 — canonical Worker ID shape (CLAUDE.md terminology rule: literal 'W-' +
# zero-padded 4-digit sequence). EVERY endpoint that accepts a worker id from a
# request normalizes through _lr_worker_id() — the lowercase free-typed
# 'w-0016' that broke the payroll rate join must be impossible to store again.
_WORKER_ID_RE = _re.compile(r'^W-\d{4}$')


def _lr_worker_id(conn, raw, require_employee=False):
    """Normalize + validate a request-supplied worker id.

    Returns (worker_id, employee_id_or_None). Raises ValueError with an
    operator-facing message when the shape is wrong or (when
    require_employee=True) the worker does not exist in employees.
    Normalization = strip + UPPERCASE, so 'w-0016' becomes 'W-0016' before
    any lookup or write. Write-side normalization is the chosen invariant
    (joins stay case-sensitive; the data can simply never go in wrong)."""
    wid = (raw or '').strip().upper()
    if not _WORKER_ID_RE.match(wid):
        raise ValueError("worker_id must match W-#### (e.g. W-0016)")
    emp = conn.execute(
        "SELECT employee_id FROM employees WHERE worker_id = ?", (wid,)).fetchone()
    if require_employee and not emp:
        raise ValueError(f"unknown worker_id {wid} — not in the workforce roster")
    return wid, (emp['employee_id'] if emp else None)


def _lr_actor():
    u = current_user() or {}
    return u.get('id'), u.get('role')


def _lr_state_public(row, pending=None):
    d = {
        'worker_id': row['worker_id'],
        'trade': row['trade'],
        'current_rate': _exp_money(row['current_rate']) if row['current_rate'] is not None else None,
        'status': row['status'],
        'effective_date': row['effective_date'],
        'updated_at': row['updated_at'],
        'has_pending': bool(pending),
    }
    if pending:
        d['pending_new_rate'] = _exp_money(pending['new_rate'])
        d['pending_change_id'] = pending['id']
        d['pending_type'] = (pending['change_type'] or 'rate')  # 'rate' | 'deactivate'
    return d


@app.route('/api/labor-rates/roster', methods=['GET'])
@requires_role(*_LR_FULL_ROLES)
def api_lr_roster():
    """Full roster split Active / Inactive + KPIs. Admin/c_suite only. No names."""
    conn = db()
    try:
        states = conn.execute(
            "SELECT * FROM labor_worker_state "
            "ORDER BY CAST(SUBSTR(worker_id,3) AS INTEGER), worker_id").fetchall()
        pend = {}
        for p in conn.execute(
                "SELECT worker_id, id, new_rate, change_type FROM labor_rate_change "
                "WHERE status='pending' ORDER BY id"):
            pend[p['worker_id']] = p
        active, inactive = [], []
        for s in states:
            p = pend.get(s['worker_id'])
            (active if s['status'] == 'active' else inactive).append(_lr_state_public(s, p))
        return jsonify({
            "data": {"active": active, "inactive": inactive},
            "meta": {"generated_at": datetime.now().isoformat()},
            "kpis": {"total": len(states), "active": len(active),
                     "inactive": len(inactive), "pending": len(pend)},
        })
    finally:
        conn.close()


@app.route('/api/labor-rates/history/<worker_id>', methods=['GET'])
@requires_role(*_LR_FULL_ROLES)
def api_lr_history(worker_id):
    """Per-worker rate-change timeline (role-stamped, no names)."""
    conn = db()
    try:
        rows = conn.execute(
            "SELECT id, old_rate, new_rate, effective_date, status, is_initial, change_type, "
            "submitted_by_role, submitted_at, decided_by_role, decided_at, note "
            "FROM labor_rate_change WHERE worker_id=? "
            "ORDER BY COALESCE(effective_date, substr(submitted_at,1,10)) DESC, id DESC",
            (worker_id,)).fetchall()
        out = [{
            'id': r['id'],
            'change_type': (r['change_type'] or 'rate'),
            'old_rate': _exp_money(r['old_rate']) if r['old_rate'] is not None else None,
            'new_rate': _exp_money(r['new_rate']),
            'effective_date': r['effective_date'],
            'status': r['status'], 'is_initial': bool(r['is_initial']),
            'submitted_by_role': r['submitted_by_role'], 'submitted_at': r['submitted_at'],
            'decided_by_role': r['decided_by_role'], 'decided_at': r['decided_at'],
            'note': r['note'],
        } for r in rows]
        return response_wrapper(out, count=len(out))
    finally:
        conn.close()


@app.route('/api/labor-rates/changes', methods=['POST'])
@requires_role(*_LR_FULL_ROLES)
def api_lr_submit_change():
    """Submit a rate change -> pending PM approval. current_rate UNCHANGED."""
    conn = db()
    try:
        body = request.get_json(silent=True) or {}
        try:
            wid, _ = _lr_worker_id(conn, body.get('worker_id'))
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        st = conn.execute("SELECT * FROM labor_worker_state WHERE worker_id=?", (wid,)).fetchone()
        if not st:
            return jsonify({"error": "unknown worker"}), 404
        try:
            new_rate = float(body.get('new_rate'))
        except (TypeError, ValueError):
            return jsonify({"error": "new_rate must be a number"}), 400
        if new_rate <= 0:
            return jsonify({"error": "new_rate must be positive"}), 400
        eff = (body.get('effective_date') or '').strip()
        try:
            datetime.strptime(eff, '%Y-%m-%d')
        except ValueError:
            return jsonify({"error": "effective_date must be YYYY-MM-DD"}), 400
        uid, role = _lr_actor()
        now = datetime.now().isoformat()
        # one pending per worker: supersede any existing pending
        conn.execute(
            "UPDATE labor_rate_change SET status='rejected', decided_by_uid=?, "
            "decided_by_role=?, decided_at=?, note='superseded by a newer submission' "
            "WHERE worker_id=? AND status='pending'", (uid, role, now, wid))
        cur = conn.execute(
            "INSERT INTO labor_rate_change (worker_id, employee_id, old_rate, new_rate, "
            "effective_date, status, is_initial, submitted_by_uid, submitted_by_role, "
            "submitted_at, note) VALUES (?,?,?,?,?,'pending',0,?,?,?,?)",
            (wid, st['employee_id'], st['current_rate'], _exp_money(new_rate), eff,
             uid, role, now, body.get('note')))
        conn.commit()
        return response_wrapper({"change_id": cur.lastrowid, "worker_id": wid, "status": "pending"}), 201
    finally:
        conn.close()


@app.route('/api/labor-rates/changes/<int:cid>/approve', methods=['POST'])
@requires_role(*_LR_APPROVE_ROLES)
def api_lr_approve(cid):
    """PM (or admin) approves -> new_rate becomes current_rate + history entry."""
    conn = db()
    try:
        ch = conn.execute("SELECT * FROM labor_rate_change WHERE id=?", (cid,)).fetchone()
        if not ch:
            return jsonify({"error": "change not found"}), 404
        if ch['status'] != 'pending':
            return jsonify({"error": "change is not pending"}), 409
        uid, role = _lr_actor()
        now = datetime.now().isoformat()
        ctype = (ch['change_type'] or 'rate')
        conn.execute("UPDATE labor_rate_change SET status='approved', decided_by_uid=?, "
                     "decided_by_role=?, decided_at=? WHERE id=?", (uid, role, now, cid))
        if ctype == 'deactivate':
            # #221 — approved deactivation moves the worker to Inactive (no rate change)
            conn.execute("UPDATE labor_worker_state SET status='inactive', updated_at=? WHERE worker_id=?",
                         (now, ch['worker_id']))
            conn.commit()
        else:
            conn.execute("UPDATE labor_worker_state SET current_rate=?, effective_date=?, "
                         "updated_at=? WHERE worker_id=?",
                         (_exp_money(ch['new_rate']), ch['effective_date'], now, ch['worker_id']))
            # #254 — RELIABLY bridge the approved rate into the canonical
            # worker_rates so the tracker / payroll grid resolves it, for ALL
            # change types (rate, DATE-ONLY, BACKDATE). The old path called
            # set_rate (which REJECTS a backdate) inside a try/except that
            # swallowed the failure, so an approved backdate never reached
            # worker_rates and the tracker rendered 'Rate not set' (the bug).
            # bridge_approved_rate never rejects a backdate. Atomic with the
            # approval: one commit, so a bridge failure rolls back the whole
            # approval rather than leaving labor_worker_state and worker_rates
            # out of sync.
            if ch['employee_id']:
                from worker_rates import bridge_approved_rate
                bridge_approved_rate(conn, employee_id=ch['employee_id'],
                                     hourly_rate=float(ch['new_rate']),
                                     effective_from=ch['effective_date'],
                                     notes='PM-approved rate change',
                                     actor_user_id=uid, actor_role=role)
            conn.commit()
        st = conn.execute("SELECT * FROM labor_worker_state WHERE worker_id=?", (ch['worker_id'],)).fetchone()
        return response_wrapper(_lr_state_public(st, None))
    finally:
        conn.close()


@app.route('/api/labor-rates/changes/<int:cid>/reject', methods=['POST'])
@requires_role(*_LR_APPROVE_ROLES)
def api_lr_reject(cid):
    """PM (or admin) rejects -> current_rate UNCHANGED, row flips rejected."""
    conn = db()
    try:
        ch = conn.execute("SELECT status FROM labor_rate_change WHERE id=?", (cid,)).fetchone()
        if not ch:
            return jsonify({"error": "change not found"}), 404
        if ch['status'] != 'pending':
            return jsonify({"error": "change is not pending"}), 409
        uid, role = _lr_actor()
        body = request.get_json(silent=True) or {}
        conn.execute("UPDATE labor_rate_change SET status='rejected', decided_by_uid=?, "
                     "decided_by_role=?, decided_at=?, note=? WHERE id=?",
                     (uid, role, datetime.now().isoformat(), body.get('note'), cid))
        conn.commit()
        return response_wrapper({"change_id": cid, "status": "rejected"})
    finally:
        conn.close()


@app.route('/api/labor-rates/deactivate', methods=['POST'])
@requires_role(*_LR_FULL_ROLES)
def api_lr_request_deactivate():
    """#221 — submit a DEACTIVATION request: routes through the SAME PM-approval
    queue as rate changes. The worker STAYS active (with a pending badge) until a
    PM approves; on approve they move to Inactive. Admin submits; PM decides."""
    conn = db()
    try:
        body = request.get_json(silent=True) or {}
        try:
            wid, _ = _lr_worker_id(conn, body.get('worker_id'))
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        st = conn.execute("SELECT * FROM labor_worker_state WHERE worker_id=?", (wid,)).fetchone()
        if not st:
            return jsonify({"error": "unknown worker"}), 404
        if st['status'] != 'active':
            return jsonify({"error": "worker is not active"}), 409
        uid, role = _lr_actor()
        now = datetime.now().isoformat()
        # one pending per worker: supersede any existing pending (rate or deactivate)
        conn.execute(
            "UPDATE labor_rate_change SET status='rejected', decided_by_uid=?, "
            "decided_by_role=?, decided_at=?, note='superseded by a newer submission' "
            "WHERE worker_id=? AND status='pending'", (uid, role, now, wid))
        # new_rate is NOT NULL; for a deactivate it carries the current rate as a
        # sentinel (the UI shows 'Deactivate', not a rate). effective_date NULL.
        cur = conn.execute(
            "INSERT INTO labor_rate_change (worker_id, employee_id, old_rate, new_rate, "
            "effective_date, status, change_type, is_initial, submitted_by_uid, "
            "submitted_by_role, submitted_at, note) "
            "VALUES (?,?,?,?,?,'pending','deactivate',0,?,?,?,?)",
            (wid, st['employee_id'], st['current_rate'], st['current_rate'], None,
             uid, role, now, body.get('note')))
        conn.commit()
        return response_wrapper({"change_id": cur.lastrowid, "worker_id": wid,
                                 "change_type": "deactivate", "status": "pending"}), 201
    finally:
        conn.close()


@app.route('/api/labor-rates/pending', methods=['GET'])
@requires_role(*_LR_APPROVE_ROLES)
def api_lr_pending():
    """PM-scoped queue: ONLY the pending rate-change items (no full roster)."""
    conn = db()
    try:
        rows = conn.execute(
            "SELECT c.id, c.worker_id, c.old_rate, c.new_rate, c.effective_date, "
            "c.submitted_by_role, c.submitted_at, c.change_type, s.trade "
            "FROM labor_rate_change c LEFT JOIN labor_worker_state s ON s.worker_id=c.worker_id "
            "WHERE c.status='pending' ORDER BY c.submitted_at, c.id").fetchall()
        out = [{
            'id': r['id'], 'worker_id': r['worker_id'], 'trade': r['trade'],
            'change_type': (r['change_type'] or 'rate'),
            'old_rate': _exp_money(r['old_rate']) if r['old_rate'] is not None else None,
            'new_rate': _exp_money(r['new_rate']), 'effective_date': r['effective_date'],
            'submitted_by_role': r['submitted_by_role'], 'submitted_at': r['submitted_at'],
        } for r in rows]
        return response_wrapper(out, count=len(out))
    finally:
        conn.close()


def _lr_card_authorized(conn, worker_id, role):
    """Who may see a worker's name/photo: admin/c_suite -> anyone; pm -> ONLY
    workers that currently have a PENDING item in their queue (so a PM cannot
    enumerate every worker's identity); everyone else -> no. (#221)"""
    if role in ('admin', 'c_suite'):
        return True
    if role == 'pm':
        return conn.execute(
            "SELECT 1 FROM labor_rate_change WHERE worker_id=? AND status='pending' LIMIT 1",
            (worker_id,)).fetchone() is not None
    return False


@app.route('/api/labor-rates/worker-card/<worker_id>', methods=['GET'])
@requires_role(*_LR_APPROVE_ROLES)
def api_lr_worker_card(worker_id):
    """Identity card for the hover popup: {display_name, trade, has_photo}.
    NEVER returns any *_path. Gated: admin/c_suite any worker; pm only the workers
    in its pending queue; super/other 403 (the decorator)."""
    conn = db()
    try:
        _, role = _lr_actor()
        if not _lr_card_authorized(conn, worker_id, role):
            return jsonify({"error": "forbidden"}), 403
        emp = conn.execute("SELECT name, trade, face_image_path FROM employees WHERE worker_id=?",
                           (worker_id,)).fetchone()
        st = conn.execute("SELECT trade FROM labor_worker_state WHERE worker_id=?", (worker_id,)).fetchone()
        display_name = (emp['name'] if emp and emp['name'] else None)
        trade = (emp['trade'] if emp and emp['trade'] else (st['trade'] if st else None))
        has_photo = False
        if emp and emp['face_image_path']:
            try:
                # #294 S3 — resolver first (pre-#287 rows are Windows-absolute)
                p = ssc_paths.resolve_data_path(emp['face_image_path'])
                base = ssc_paths.under_root("worker_records").resolve()   # #287
                has_photo = p.resolve().is_relative_to(base) and p.exists()
            except Exception:
                has_photo = False
        return response_wrapper({"worker_id": worker_id, "display_name": display_name,
                                 "trade": trade, "has_photo": bool(has_photo)})
    finally:
        conn.close()


@app.route('/api/labor-rates/worker-photo/<worker_id>', methods=['GET'])
@requires_role(*_LR_APPROVE_ROLES)
def api_lr_worker_photo(worker_id):
    """Serve a worker's headshot through THIS auth-gated route only (same gating
    as the card; never exposes the path). Path is confined to worker_records/."""
    from flask import send_file
    conn = db()
    try:
        _, role = _lr_actor()
        if not _lr_card_authorized(conn, worker_id, role):
            return jsonify({"error": "forbidden"}), 403
        emp = conn.execute("SELECT face_image_path FROM employees WHERE worker_id=?", (worker_id,)).fetchone()
        if not emp or not emp['face_image_path']:
            return jsonify({"error": "no photo"}), 404
        # #294 S3 — resolver first (pre-#287 rows are Windows-absolute)
        p = ssc_paths.resolve_data_path(emp['face_image_path'])
        base = ssc_paths.under_root("worker_records").resolve()   # #287
        if not (p.resolve().is_relative_to(base) and p.exists()):
            return jsonify({"error": "photo missing"}), 404
        mt = 'image/png' if p.suffix.lower() == '.png' else 'image/jpeg'
        resp = send_file(str(p), mimetype=mt)
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp
    finally:
        conn.close()


@app.route('/api/labor-rates/state/<worker_id>/status', methods=['POST'])
@requires_role(*_LR_FULL_ROLES)
def api_lr_set_status(worker_id):
    """#221 — ONLY Reactivate (->active) is an instant admin action here.
    DEACTIVATION is PM-gated: it must go through /deactivate (pending approval),
    so this endpoint refuses a direct ->inactive (no admin bypass of the gate)."""
    conn = db()
    try:
        try:
            worker_id, _ = _lr_worker_id(conn, worker_id)
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        if not conn.execute("SELECT 1 FROM labor_worker_state WHERE worker_id=?", (worker_id,)).fetchone():
            return jsonify({"error": "unknown worker"}), 404
        status = (request.get_json(silent=True) or {}).get('status')
        if status != 'active':
            return jsonify({"error": "Only reactivation is instant. Deactivation requires PM "
                                     "approval — submit a deactivation request."}), 400
        conn.execute("UPDATE labor_worker_state SET status='active', updated_at=? WHERE worker_id=?",
                     (datetime.now().isoformat(), worker_id))
        conn.commit()
        return response_wrapper({"worker_id": worker_id, "status": "active"})
    finally:
        conn.close()


@app.route('/api/labor-rates/state/<worker_id>/trade', methods=['POST'])
@requires_role(*_LR_FULL_ROLES)
def api_lr_set_trade(worker_id):
    """Trade assignment. Immediate admin action — not PM-gated."""
    conn = db()
    try:
        try:
            worker_id, _ = _lr_worker_id(conn, worker_id)
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        if not conn.execute("SELECT 1 FROM labor_worker_state WHERE worker_id=?", (worker_id,)).fetchone():
            return jsonify({"error": "unknown worker"}), 404
        trade = (request.get_json(silent=True) or {}).get('trade')
        if trade not in LABOR_TRADES:
            return jsonify({"error": "invalid trade"}), 400
        conn.execute("UPDATE labor_worker_state SET trade=?, updated_at=? WHERE worker_id=?",
                     (trade, datetime.now().isoformat(), worker_id))
        conn.commit()
        return response_wrapper({"worker_id": worker_id, "trade": trade})
    finally:
        conn.close()


@app.route('/api/labor-rates/state', methods=['POST'])
@requires_role(*_LR_FULL_ROLES)
def api_lr_add_worker():
    """Add a NEW worker's initial rate. Immediate admin action (approved initial
    history) — not PM-gated; only subsequent CHANGES need PM sign-off.

    #241 — worker_id is normalized + validated and MUST exist in employees.
    The free-text id that let 'w-0016' in (employee_id silently NULL, so the
    worker_rates bridge never ran and payroll rendered 'Rate not set') is
    closed at this gate; the UI now submits from a selector, and this check
    is the belt-and-suspenders for any non-UI caller."""
    conn = db()
    try:
        body = request.get_json(silent=True) or {}
        try:
            wid, eid = _lr_worker_id(conn, body.get('worker_id'), require_employee=True)
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        if conn.execute("SELECT 1 FROM labor_worker_state WHERE worker_id=?", (wid,)).fetchone():
            return jsonify({"error": "worker already has a rate"}), 409
        trade = body.get('trade')
        if trade not in LABOR_TRADES:
            return jsonify({"error": "invalid trade"}), 400
        try:
            rate = float(body.get('rate'))
        except (TypeError, ValueError):
            return jsonify({"error": "rate must be a number"}), 400
        if rate <= 0:
            return jsonify({"error": "rate must be positive"}), 400
        eff = (body.get('effective_date') or '').strip()
        try:
            datetime.strptime(eff, '%Y-%m-%d')
        except ValueError:
            return jsonify({"error": "effective_date must be YYYY-MM-DD"}), 400
        status = body.get('status') if body.get('status') in ('active', 'inactive') else 'active'
        uid, role = _lr_actor()
        now = datetime.now().isoformat()
        conn.execute("INSERT INTO labor_worker_state (worker_id, employee_id, trade, current_rate, "
                     "status, effective_date, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     (wid, eid, trade, _exp_money(rate), status, eff, now, now))
        conn.execute("INSERT INTO labor_rate_change (worker_id, employee_id, old_rate, new_rate, "
                     "effective_date, status, is_initial, submitted_by_uid, submitted_by_role, "
                     "submitted_at, decided_by_uid, decided_by_role, decided_at, note) "
                     "VALUES (?,?,?,?,?,'approved',1,?,?,?,?,?,?,?)",
                     (wid, eid, None, _exp_money(rate), eff, uid, role, now, uid, role, now,
                      'initial rate (admin)'))
        conn.commit()
        if eid:
            try:
                from worker_rates import set_rate
                set_rate(conn, employee_id=eid, hourly_rate=float(rate), effective_from=eff,
                         notes='initial rate', actor_user_id=uid, actor_role=role)
            except Exception:
                pass
        return response_wrapper({"worker_id": wid, "trade": trade, "status": status}), 201
    finally:
        conn.close()


@app.route('/api/labor-rates/eligible-workers', methods=['GET'])
@requires_role(*_LR_FULL_ROLES)
def api_lr_eligible_workers():
    """#241 — workers selectable in the 'Add worker rate' form: on the active
    project roster (active assignment, not archived) and NOT already on the
    labor-rates file. Replaces the free-typed Worker ID input — the enabling
    defect behind the 'w-0016' malformed id. Numeric W-#### order (one
    ordering convention for every worker selector). Carries names for the
    'W-#### — name' labels (admin/c_suite-gated page) — served no-store."""
    conn = db()
    try:
        rows = conn.execute(
            # #260 — the numeric sort key is in the SELECT list because Postgres
            # requires every SELECT DISTINCT ORDER BY expression to appear there
            # (SQLite did not). It's functionally dependent on worker_id, so it
            # does not change the distinct set; the response ignores it.
            """SELECT DISTINCT e.worker_id, e.name,
                      CAST(SUBSTR(e.worker_id, 3) AS INTEGER) AS sort_key
               FROM employees e
               JOIN project_assignments pa ON pa.employee_id = e.employee_id
               LEFT JOIN labor_worker_state ls ON ls.worker_id = e.worker_id
               WHERE pa.status = 'active' AND e.archived_at IS NULL
                 AND ls.worker_id IS NULL
               ORDER BY sort_key""").fetchall()
        out = [{"worker_id": r["worker_id"], "name": r["name"]} for r in rows]
        resp = response_wrapper(out, count=len(out))
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp
    finally:
        conn.close()


@app.route('/admin/labor-rates', methods=['GET'])
@requires_role('admin', 'c_suite')
def admin_labor_rates_page():
    """Serve the Labor Rates admin page (HTML). #257 — admin/c_suite ONLY. pm was
    removed (pm must NOT see individual worker rates/comp); every rates API is
    also admin/c_suite-gated server-side. super/external get 403 here."""
    page = SCRIPT_DIR / 'admin_labor_rates.html'
    if not page.exists():
        return jsonify({"error": "admin page not found"}), 404
    resp = send_file(str(ui_version.resolve_page(page)))   # #279
    # comp-data page — never cache (no-store) anywhere
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/api/toolbox-talks/library', methods=['GET'])
def api_toolbox_talks_library():
    """Catalog of Ch 33 toolbox talks (EN + ES PDFs per talk).

    Mirrors /api/blank-forms + /api/signage-templates shape. Returns
    every row in `toolbox_talks` with synthesized gated URLs for both
    languages under /project-files/data_room/toolbox_talks/.

    Optional category filter (Site / Fall / Scaffold / Demo / General).

    Note: distinct from the pre-existing /api/toolbox-talks (which
    reads from toolbox_talk_library — an older empty-stub table).
    """
    cat = (request.args.get('category') or '').strip()
    where = []
    params = []
    if cat:
        where.append("category = ?")
        params.append(cat)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        conn = db()
        rows = conn.execute(
            f"SELECT id, topic_number, category, title_en, title_es, "
            f"       ch33_ref, filename_en, filename_es, est_minutes, "
            f"       description, created_at "
            f"FROM toolbox_talks{where_sql} "
            f"ORDER BY topic_number",
            params,
        ).fetchall()
        conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d['file_url_en'] = '/project-files/data_room/toolbox_talks/' + d['filename_en']
            d['file_url_es'] = '/project-files/data_room/toolbox_talks/' + d['filename_es']
            out.append(d)
        return response_wrapper(out, count=len(out))
    except Exception as e:
        logging.error(f"GET /api/toolbox-talks/library: {str(e)}")
        return _server_error(e)


@app.route('/api/signage-templates', methods=['GET'])
def api_signage_templates():
    """Catalog of standard construction-site signs (signage_templates).

    Mirrors /api/blank-forms shape. Returns every row with a file_url
    synthesized for the gated /project-files/data_room/signage/<filename>
    route so the operator's UI can link straight to the rendered PDF
    without a second round-trip.

    Optional category filter (Safety / PPE / DOB / Site).
    """
    cat = (request.args.get('category') or '').strip()
    where = []
    params = []
    if cat:
        where.append("category = ?")
        params.append(cat)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        conn = db()
        rows = conn.execute(
            f"SELECT id, code, title, filename, category, orientation, "
            f"       description, mime_type, created_at "
            f"FROM signage_templates{where_sql} "
            f"ORDER BY category, code, title",
            params,
        ).fetchall()
        conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d['file_url'] = '/project-files/data_room/signage/' + d['filename']
            out.append(d)
        return response_wrapper(out, count=len(out))
    except Exception as e:
        logging.error(f"GET /api/signage-templates: {str(e)}")
        return _server_error(e)


@app.route('/api/spec-products', methods=['GET'])
@requires_company  # #263 — company Specs Library tab (admin/c_suite only; pm 403)
def api_spec_products():
    """Browse / search the manufacturer-agnostic specifications catalog.

    Query params (all optional):
      manufacturer  default 'Sika'; pass empty string '' for all manufacturers
      category      exact category match (case-sensitive)
      product_line  exact product_line match
      tag           filter by tags column (e.g. '890-core')
      search        substring match against product_name OR product_code

    Returns rows ordered by manufacturer, category, product_line, product_name.
    """
    mfr = request.args.get('manufacturer')
    cat = (request.args.get('category') or '').strip()
    line = (request.args.get('product_line') or '').strip()
    tag = (request.args.get('tag') or '').strip()
    search = (request.args.get('search') or '').strip()

    where = []
    params = []
    if mfr is not None and mfr != '':
        where.append("manufacturer = ?")
        params.append(mfr)
    elif mfr is None:
        # Default the bare endpoint to Sika so the first-page render is
        # focused; callers can pass manufacturer='' to see everything.
        where.append("manufacturer = ?")
        params.append('Sika')
    if cat:
        where.append("category = ?")
        params.append(cat)
    if line:
        where.append("product_line = ?")
        params.append(line)
    if tag:
        where.append("tags = ?")
        params.append(tag)
    if search:
        where.append("(product_name LIKE ? OR product_code LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        conn = db()
        rows = conn.execute(
            # #247 — datasheet_pdf_path dropped (path-named, NULL across all
            # 107 rows, zero consumers); spec_url is the public TDS link.
            f"SELECT id, manufacturer, category, product_line, product_name, "
            f"       product_code, description, spec_url, "
            f"       tags, created_at "
            f"FROM spec_products{where_sql} "
            f"ORDER BY manufacturer, category, product_line, product_name",
            params,
        ).fetchall()
        # Surface categories + counts as a sidebar helper (cheap when
        # the result set is small; saves the UI a second round-trip).
        cats = conn.execute(
            "SELECT manufacturer, category, COUNT(*) AS n FROM spec_products "
            "GROUP BY manufacturer, category ORDER BY manufacturer, category"
        ).fetchall()
        conn.close()
        return response_wrapper({
            "items": rows_to_dicts(rows),
            "categories": rows_to_dicts(cats),
            "count": len(rows),
        })
    except Exception as e:
        logging.error(f"GET /api/spec-products: {str(e)}")
        return _server_error(e)


@app.route('/api/projects/<project_code>/document-specs', methods=['GET'])
def api_project_document_specs_list(project_code):
    """List the specs currently attached to a project's Project Documents."""
    try:
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 404
        rows = conn.execute(
            "SELECT pds.id AS link_id, pds.added_at, pds.added_by, pds.notes, "
            "       sp.id AS spec_product_id, sp.manufacturer, sp.category, "
            "       sp.product_line, sp.product_name, sp.product_code, "
            "       sp.description, sp.spec_url, sp.tags "  # #247 — no datasheet_pdf_path
            "FROM project_document_specs pds "
            "JOIN spec_products sp ON sp.id = pds.spec_product_id "
            "WHERE pds.project_code = ? "
            "ORDER BY sp.category, sp.product_line, sp.product_name",
            (project_code,),
        ).fetchall()
        conn.close()
        return response_wrapper(rows_to_dicts(rows))
    except Exception as e:
        logging.error(f"GET /api/projects/{project_code}/document-specs: {str(e)}")
        return _server_error(e)


@app.route('/api/projects/<project_code>/document-specs', methods=['POST'])
def api_project_document_specs_attach(project_code):
    """Attach a spec_product to a project's Project Documents.

    Body: {spec_product_id: int, added_by: str (optional), notes: str (optional)}
    Returns 201 on insert, 200 with the existing row on re-attach (idempotent
    via the UNIQUE(project_code, spec_product_id) constraint).
    """
    data = request.get_json() or {}
    try:
        spec_id = int(data.get('spec_product_id') or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "spec_product_id must be an integer"}), 400
    if spec_id <= 0:
        return jsonify({"error": "spec_product_id is required"}), 400
    added_by = (data.get('added_by') or '').strip() or None
    notes = (data.get('notes') or '').strip() or None
    try:
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 404
        spec = conn.execute(
            "SELECT id FROM spec_products WHERE id = ?", (spec_id,)
        ).fetchone()
        if not spec:
            conn.close()
            return jsonify({"error": "spec_product_id not found"}), 404
        cur = conn.execute(
            "INSERT OR IGNORE INTO project_document_specs "
            "  (project_code, spec_product_id, added_by, notes) "
            "VALUES (?, ?, ?, ?)",
            (project_code, spec_id, added_by, notes),
        )
        already = (cur.rowcount == 0)
        conn.commit()
        row = conn.execute(
            "SELECT id, project_code, spec_product_id, added_at, added_by, notes "
            "FROM project_document_specs "
            "WHERE project_code = ? AND spec_product_id = ?",
            (project_code, spec_id),
        ).fetchone()
        conn.close()
        return response_wrapper(dict(row) if row else {}), (200 if already else 201)
    except Exception as e:
        logging.error(f"POST /api/projects/{project_code}/document-specs: {str(e)}")
        return _server_error(e)


@app.route('/api/projects/<project_code>/document-specs/<int:spec_product_id>', methods=['DELETE'])
def api_project_document_specs_detach(project_code, spec_product_id):
    """Detach a spec_product from a project's Project Documents. 200 on
    delete, 404 if the attachment doesn't exist."""
    try:
        conn = db()
        cur = conn.execute(
            "DELETE FROM project_document_specs "
            "WHERE project_code = ? AND spec_product_id = ?",
            (project_code, spec_product_id),
        )
        conn.commit()
        affected = cur.rowcount
        conn.close()
        if affected == 0:
            return jsonify({"error": "attachment not found"}), 404
        return response_wrapper({"detached": affected})
    except Exception as e:
        logging.error(f"DELETE /api/projects/{project_code}/document-specs/{spec_product_id}: {str(e)}")
        return _server_error(e)


@app.route('/api/projects/<project_code>/locations', methods=['GET'])
def api_project_locations(project_code):
    """Canonical location_reference catalog for a project.

    Returns the project's drops from `drop_plan` shaped for the spine
    (location_unit / location_id) so RFI / DCR / Look-Ahead pickers can
    offer real drops instead of free-text. Grouped by elevation in the
    order the operator works the building (the drop_plan_890.md layout
    for FR-BX-001: North → West → South → East).

    Response:
      {data: {
        project_code, project_type, location_unit,  # 'Drop' for facade
        groups: [
          {elevation: 'North (East 135th St)',
           drops: [{location_id:'DP-001', label:'DP-001 · North 1', step_count:6, status:'pending'}, ...]
          }, ...
        ],
        flat: [{location_unit:'Drop', location_id:'DP-001', elevation:'...', label:'...', status:'pending'}, ...],
        count
      }}

    The flat array is what writes need (location_unit + location_id
    pair); the groups array is for the dropdown render. Both come from
    the same `drop_plan` query so they can't drift.
    """
    try:
        conn = db()
        if not validate_project_exists(conn, project_code):
            conn.close()
            return jsonify({"error": "Project not found"}), 404
        # drop_plan is the source — sort by the numeric tail of drop_id
        # (DP-002 before DP-010) so the UI reads building-walk order.
        rows = conn.execute(
            "SELECT drop_id, elevation, status, notes "
            "FROM drop_plan WHERE project_code = ? "
            "ORDER BY CAST(SUBSTR(drop_id, 4) AS INTEGER)",
            (project_code,)
        ).fetchall()
        # Step counts per drop for the UI badge ("DP-001 · 6 steps").
        steps_by_drop = {
            r["drop_id"]: r["n"] for r in conn.execute(
                "SELECT drop_id, COUNT(*) AS n FROM drop_activities "
                "WHERE drop_id IN (SELECT drop_id FROM drop_plan WHERE project_code = ?) "
                "GROUP BY drop_id",
                (project_code,)
            ).fetchall()
        }
        # project_type — usually 'facade' for FR-BX-001 → location_unit='Drop'
        # is the most natural pick; the spine's location_unit_options
        # remain available via /api/projects/<code>/project-config.
        ptype = conn.execute(
            "SELECT project_type FROM projects WHERE project_code = ?",
            (project_code,)
        ).fetchone()
        conn.close()
        groups_map = {}
        flat = []
        for r in rows:
            elev = r["elevation"] or "Unassigned"
            n_tail = r["drop_id"].split("-")[-1].lstrip("0") or "0"
            label = f'{r["drop_id"]} · {elev.split(" ")[0]} {n_tail}'
            entry = {
                "location_unit": "Drop",
                "location_id": r["drop_id"],
                "elevation": elev,
                "label": label,
                "status": r["status"] or "pending",
                "step_count": steps_by_drop.get(r["drop_id"], 0),
            }
            flat.append(entry)
            groups_map.setdefault(elev, []).append({
                "location_id": entry["location_id"],
                "label": entry["label"],
                "step_count": entry["step_count"],
                "status": entry["status"],
            })
        # Preserve elevation order = first appearance in drop_id order.
        elev_order = []
        for e in flat:
            if e["elevation"] not in elev_order:
                elev_order.append(e["elevation"])
        groups = [{"elevation": e, "drops": groups_map[e]} for e in elev_order]
        return response_wrapper({
            "project_code": project_code,
            "project_type": ptype["project_type"] if ptype else None,
            "location_unit": "Drop",
            "groups": groups,
            "flat": flat,
            "count": len(flat),
        })
    except Exception as e:
        logging.error(f"GET /api/projects/{project_code}/locations: {str(e)}")
        return _server_error(e)


@app.route('/api/projects/<project_code>/on-site', methods=['GET'])
def api_project_on_site(project_code):
    """SHARED 'who is on site now' source (#115/#116).

    Returns the current on-site roster for `project_code` on `date` (defaults
    to LOCAL today per the dates rule — never UTC). The project-dashboard
    Workers-Today / Today-on-Site widgets and the Submit-RFI on-site header
    both consume this — single source of truth, sorted by Worker ID
    ascending (matches the DCR labor section after #107).

    Query params:
      date=YYYY-MM-DD   default = local today

    Response shape:
      {data: {
        project_code, date,
        headcount,            # total workers signed in today
        total_hours,          # sum of hours from rows with time_in+time_out
        still_on_site,        # rows with time_in but no time_out
        foreman,              # the foreman's name + worker_id (or null)
        workers: [{
          employee_id, worker_id, name, trade,
          time_in, time_out, hours, still_in
        }, ...]
      }}
    """
    from payroll_hours import compute_worked_hours
    try:
        # LOCAL date — server-side is Eastern, matches the operator's clock
        # and the existing sign-in storage convention (#77 dates rule).
        date_str = request.args.get('date') or date.today().isoformat()
        try:
            datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        conn = db()
        try:
            if not validate_project_exists(conn, project_code):
                return jsonify({"error": "Project not found"}), 404
            rows = conn.execute(
                """SELECT s.employee_id, s.time_in, s.time_out,
                          e.name AS emp_name, e.trade AS emp_trade,
                          e.worker_id AS emp_worker_id
                   FROM sign_in_log s
                   LEFT JOIN employees e ON s.employee_id = e.employee_id
                   WHERE s.project_code = ? AND s.date = ?
                   ORDER BY s.time_in, s.id""",
                (project_code, date_str)
            ).fetchall()
            # day_location: first non-empty location_elevation OR trade_area
            # from any work_log row for this project+date. Surfaced on the
            # Daily Sign-In table's Area column for every row — the team is
            # at this location today. Empty when no work_log exists yet.
            wl_row = conn.execute(
                """SELECT location_elevation, trade_area
                     FROM work_log
                    WHERE project_code = ? AND date = ?
                    ORDER BY id ASC""",
                (project_code, date_str)
            ).fetchone()
            day_location = None
            if wl_row:
                loc = (wl_row['location_elevation'] or '').strip()
                area = (wl_row['trade_area'] or '').strip()
                day_location = loc or area or None
        finally:
            conn.close()
        workers = []
        total_hours = 0.0
        still_on_site = 0
        foreman = None
        for r in rows:
            t_in = r['time_in']
            t_out = r['time_out']
            hours = compute_worked_hours(t_in, t_out) if t_in and t_out else None
            if hours is not None:
                total_hours += hours
            if t_in and not t_out:
                still_on_site += 1
            trade = r['emp_trade'] or ''
            entry = {
                'employee_id': r['employee_id'],
                'worker_id': r['emp_worker_id'],
                'name': r['emp_name'],
                'trade': trade,
                'time_in': t_in,
                'time_out': t_out,
                'hours': hours,
                'still_in': bool(t_in and not t_out),
            }
            workers.append(entry)
            # First foreman in the cohort (by time_in order) gets the slot.
            if foreman is None and isinstance(trade, str) and 'foreman' in trade.lower():
                foreman = {
                    'employee_id': r['employee_id'],
                    'worker_id': r['emp_worker_id'],
                    'name': r['emp_name'],
                }
        # Sort by Worker ID ascending (numeric trailing digits per CLAUDE.md
        # schema rule). Rows without worker_id sort to the end. Mirrors the
        # DCR labor section sort from #107 — every roster surface reads
        # in the same order.
        def _wid_key(row):
            wid = row.get('worker_id')
            if not wid:
                return (1, 0)
            digits = ''.join(ch for ch in str(wid) if ch.isdigit())
            try:
                return (0, int(digits)) if digits else (1, 0)
            except ValueError:
                return (1, 0)
        workers.sort(key=_wid_key)
        return response_wrapper({
            'project_code': project_code,
            'date': date_str,
            'headcount': len(workers),
            'total_hours': round(total_hours, 2),
            'still_on_site': still_on_site,
            'foreman': foreman,
            'workers': workers,
            'day_location': day_location,
        })
    except Exception as e:
        logging.error(f"GET /api/projects/{project_code}/on-site: {str(e)}")
        return _server_error(e)


@app.route('/api/projects/<project_code>/assign', methods=['POST'])
def api_project_assign(project_code):
    """Assign an existing worker to a project. Body: {employee_id, role_on_project}."""
    try:
        data = request.get_json(silent=True) or {}
        employee_id = data.get("employee_id")
        if not employee_id:
            return jsonify({"error": "employee_id required"}), 400
        conn = db()
        # Skip if already assigned and active
        existing = conn.execute(
            "SELECT id FROM project_assignments WHERE project_code = ? AND employee_id = ? AND status = 'active'",
            (project_code, employee_id)
        ).fetchone()
        if existing:
            conn.close()
            return response_wrapper({"assignment_id": existing["id"], "already_assigned": True})
        cur = conn.execute(
            """INSERT INTO project_assignments
               (project_code, employee_id, role_on_project, start_date, status)
               VALUES (?, ?, ?, DATE('now'), 'active')""",
            (project_code, employee_id, data.get("role_on_project"))
        )
        aid = cur.lastrowid
        conn.commit()
        conn.close()
        return response_wrapper({"assignment_id": aid})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/company/summary', methods=['GET'])
@requires_company  # #263 — company overview metrics (admin/c_suite only; pm 403)
def api_company_summary():
    """Roll-up metrics for the Company Overview top-of-page banner."""
    try:
        conn = db()
        # #246 — "workers in the system" = the master (non-archived) roster;
        # archived workers no longer inflate the KPI. Labor-inactive workers
        # still count here (they are in the system — the console Workforce
        # page shows them with an Inactive badge).
        total_workers = conn.execute(
            "SELECT COUNT(*) FROM employees WHERE archived_at IS NULL").fetchone()[0]
        active_projects = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE status = 'active' OR status IS NULL"
        ).fetchone()[0]
        all_projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]

        today = date.today().isoformat()   # #305 — LOCAL, never UTC (dates rule)
        d30 = (date.today() + timedelta(days=30)).isoformat()
        certs = conn.execute(
            "SELECT expiration_date, status FROM certifications"
        ).fetchall()
        valid = expiring_30 = expired = 0
        for c in certs:
            if c["status"] and c["status"].lower() in ("revoked", "expired", "void"):
                continue
            exp = c["expiration_date"]
            if not exp:
                valid += 1
            elif exp < today:
                expired += 1
            elif exp <= d30:
                expiring_30 += 1
                valid += 1   # still valid but flagged
            else:
                valid += 1
        conn.close()
        return response_wrapper({
            "total_workers": total_workers,
            "active_projects": active_projects,
            "all_projects": all_projects,
            "certs": {"valid": valid, "expiring_30d": expiring_30, "expired": expired,
                      "total": valid + expired}
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ================= #266 — CRM / OPS CORE (C-suite function) =================
# The four primitives — organizations, contacts, activity log, follow-up tasks — plus the
# project->client link and the "Needs Attention" feed. EVERY route below is
# @requires_section('crm'); access.py SECTION_ACCESS['crm'] = {admin, c_suite}, so pm /
# super / any external role get 403 SERVER-SIDE (hiding the nav is not access control).
# Logic lives in crm.py (shared with tests/smoke_crm_266.py). LOCAL dates only (CLAUDE.md
# dates rule). Activity/task entities are organization|contact this build; 'project' stays
# in the vocabulary (scale-ready) — the project<->client relationship is client_org_id.

try:   # the client's email / phone / website / address columns; apply_crm_org_details.py is the migration
    _cod_conn = db()
    try:
        crm.ensure_org_details(_cod_conn)
    finally:
        _cod_conn.close()
except Exception as _cod_e:   # never blocks boot
    logging.warning(f"crm org details ensure skipped: {_cod_e}")


def _crm_uid():
    return (current_user() or {}).get("id")


def _crm_pick(d, keys):
    return {k: d[k] for k in keys if k in d}


# ---- organizations ----
@app.route('/api/crm/organizations', methods=['GET'])
@requires_section('crm')
def api_crm_orgs_list():
    try:
        conn = db()
        rows = crm.list_orgs(
            conn, search=request.args.get('search'),
            relationship_type=request.args.get('relationship_type'),
            stage=request.args.get('stage'), function_tag=request.args.get('function_tag'))
        conn.close()
        return response_wrapper(rows, count=len(rows))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/crm/organizations', methods=['POST'])
@requires_section('crm')
def api_crm_org_create():
    try:
        d = request.get_json(silent=True) or {}
        conn = db()
        oid = crm.create_org(
            conn, name=d.get('name'), relationship_type=d.get('relationship_type'),
            status=d.get('status') or 'active', stage=d.get('stage'),
            function_tags=d.get('function_tags'), notes=d.get('notes'), created_by=_crm_uid())
        org = crm.get_org(conn, oid)
        conn.close()
        return response_wrapper({"id": oid, "organization": org})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/crm/organizations/<int:org_id>', methods=['GET'])
@requires_section('crm')
def api_crm_org_get(org_id):
    """Entity detail: the org + its contacts + activity timeline + open tasks + linked projects."""
    try:
        conn = db()
        org = crm.get_org(conn, org_id)
        if not org:
            conn.close()
            return jsonify({"error": "not found"}), 404
        payload = {
            "organization": org,
            "contacts": crm.list_contacts(conn, org_id=org_id),
            "timeline": crm.list_activity(conn, entity_type='organization', entity_id=org_id),
            "open_tasks": crm.list_tasks(conn, status='open', entity_type='organization', entity_id=org_id),
            "linked_projects": crm.list_org_projects(conn, org_id),
        }
        conn.close()
        return response_wrapper(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/crm/organizations/<int:org_id>', methods=['PUT'])
@requires_section('crm')
def api_crm_org_update(org_id):
    try:
        d = request.get_json(silent=True) or {}
        conn = db()
        ok = crm.update_org(conn, org_id, actor_user_id=_crm_uid(),
                            **_crm_pick(d, ('name', 'relationship_type', 'status', 'stage', 'notes', 'function_tags',
                                            *crm.ORG_DETAIL_FIELDS)))
        if not ok:
            conn.close()
            return jsonify({"error": "not found"}), 404
        org = crm.get_org(conn, org_id)
        conn.close()
        return response_wrapper({"organization": org})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/crm/organizations/<int:org_id>', methods=['DELETE'])
@requires_section('crm')
def api_crm_org_delete(org_id):
    """Permanently delete a client nothing real points at (no jobs, estimates, proposals, invoices,
    bills, document packages). Anything else is refused with what still points at it."""
    try:
        conn = db()
        out = crm.delete_org(conn, org_id)
        conn.close()
        if out is None:
            return jsonify({"error": "not found"}), 404
        if out.get("blockers"):
            return jsonify({"error": "This client has " + ", ".join(out["blockers"])
                            + " — mark it inactive instead.", "blockers": out["blockers"]}), 409
        if out["user_ids"]:
            team_items._sync_google(*out["user_ids"])
        logging.info("crm org deleted id=%s by user=%s", org_id, _crm_uid())
        return response_wrapper({"deleted": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ---- contacts ----
@app.route('/api/crm/contacts', methods=['GET'])
@requires_section('crm')
def api_crm_contacts_list():
    try:
        conn = db()
        rows = crm.list_contacts(conn, org_id=request.args.get('org_id', type=int))
        conn.close()
        return response_wrapper(rows, count=len(rows))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/crm/contacts', methods=['POST'])
@requires_section('crm')
def api_crm_contact_create():
    try:
        d = request.get_json(silent=True) or {}
        conn = db()
        cid = crm.create_contact(
            conn, full_name=d.get('full_name'), org_id=d.get('org_id'), email=d.get('email'),
            phone=d.get('phone'), title=d.get('title'), relationship_type=d.get('relationship_type'),
            status=d.get('status') or 'active', notes=d.get('notes'), created_by=_crm_uid())
        contact = crm.get_contact(conn, cid)
        conn.close()
        return response_wrapper({"id": cid, "contact": contact})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/crm/contacts/<int:contact_id>', methods=['GET'])
@requires_section('crm')
def api_crm_contact_get(contact_id):
    try:
        conn = db()
        contact = crm.get_contact(conn, contact_id)
        if not contact:
            conn.close()
            return jsonify({"error": "not found"}), 404
        payload = {
            "contact": contact,
            "organization": crm.get_org(conn, contact['org_id']) if contact.get('org_id') else None,
            "timeline": crm.list_activity(conn, entity_type='contact', entity_id=contact_id),
            "open_tasks": crm.list_tasks(conn, status='open', entity_type='contact', entity_id=contact_id),
        }
        conn.close()
        return response_wrapper(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/crm/contacts/<int:contact_id>', methods=['PUT'])
@requires_section('crm')
def api_crm_contact_update(contact_id):
    try:
        d = request.get_json(silent=True) or {}
        conn = db()
        ok = crm.update_contact(conn, contact_id,
                                **_crm_pick(d, ('org_id', 'full_name', 'email', 'phone', 'title',
                                                'relationship_type', 'status', 'notes')))
        if not ok:
            conn.close()
            return jsonify({"error": "not found"}), 404
        contact = crm.get_contact(conn, contact_id)
        conn.close()
        return response_wrapper({"contact": contact})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ---- activity (the timeline) ----
@app.route('/api/crm/activity', methods=['POST'])
@requires_section('crm')
def api_crm_activity_add():
    try:
        d = request.get_json(silent=True) or {}
        conn = db()
        aid = crm.add_activity(
            conn, entity_type=d.get('entity_type'), entity_id=d.get('entity_id'),
            activity_type=d.get('activity_type') or 'note', summary=d.get('summary'),
            body=d.get('body'), author_user_id=_crm_uid(), occurred_at=d.get('occurred_at'))
        conn.close()
        return response_wrapper({"id": aid})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/crm/activity', methods=['GET'])
@requires_section('crm')
def api_crm_activity_list():
    try:
        et = request.args.get('entity_type')
        eid = request.args.get('entity_id', type=int)
        if not et or eid is None:
            return jsonify({"error": "entity_type and entity_id required"}), 400
        conn = db()
        rows = crm.list_activity(conn, entity_type=et, entity_id=eid)
        conn.close()
        return response_wrapper(rows, count=len(rows))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ---- tasks + needs-attention ----
@app.route('/api/crm/tasks', methods=['POST'])
@requires_section('crm')
def api_crm_task_create():
    try:
        d = request.get_json(silent=True) or {}
        conn = db()
        tid = crm.create_task(
            conn, title=d.get('title'), entity_type=d.get('entity_type') or 'none',
            entity_id=d.get('entity_id'), detail=d.get('detail'), due_date=d.get('due_date'),
            assignee_user_id=d.get('assignee_user_id'), priority=d.get('priority') or 'normal',
            function_tag=d.get('function_tag'), created_by=_crm_uid())
        conn.close()
        return response_wrapper({"id": tid})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/crm/tasks', methods=['GET'])
@requires_section('crm')
def api_crm_tasks_list():
    try:
        conn = db()
        rows = crm.list_tasks(
            conn, status=request.args.get('status'),
            assignee_user_id=request.args.get('assignee_user_id', type=int),
            function_tag=request.args.get('function_tag'))
        conn.close()
        return response_wrapper(rows, count=len(rows))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/crm/tasks/<int:task_id>/complete', methods=['POST'])
@requires_section('crm')
def api_crm_task_complete(task_id):
    try:
        conn = db()
        ok = crm.complete_task(conn, task_id)
        conn.close()
        if not ok:
            return jsonify({"error": "not found"}), 404
        return response_wrapper({"id": task_id, "status": "done"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/crm/needs-attention', methods=['GET'])
@requires_section('crm')
def api_crm_needs_attention():
    """Open + overdue tasks, prioritized. ?mine=1 scopes to the caller; ?function_tag= filters."""
    try:
        mine = request.args.get('mine') in ('1', 'true', 'yes')
        conn = db()
        rows = crm.needs_attention(
            conn, assignee_user_id=_crm_uid() if mine else None,
            function_tag=request.args.get('function_tag'))
        conn.close()
        return response_wrapper(rows, count=len(rows))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ---- project <-> client-org link ----
@app.route('/api/crm/projects/<project_code>/link', methods=['POST'])
@requires_section('crm')
def api_crm_link_project(project_code):
    """Body {org_id:<id>} to link, {org_id:null} to unlink."""
    try:
        d = request.get_json(silent=True) or {}
        org_id = d.get('org_id')
        conn = db()
        ok = crm.link_project(conn, project_code, org_id)
        conn.close()
        if not ok:
            return jsonify({"error": "project not found"}), 404
        return response_wrapper({"project_code": project_code, "client_org_id": org_id})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ---- console surfacing + assignable users ----
@app.route('/api/crm/console', methods=['GET'])
@requires_section('crm')
def api_crm_console():
    """Light C-suite surfacing: needs-attention count + top items, pipeline by stage, recent feed."""
    try:
        conn = db()
        na = crm.needs_attention(conn)
        payload = {
            "needs_attention_count": len(na),
            "overdue_count": sum(1 for t in na if t.get('overdue')),
            "needs_attention_top": na[:6],
            "pipeline": crm.pipeline_by_stage(conn),
            "recent_activity": crm.recent_activity(conn, limit=12),
        }
        conn.close()
        return response_wrapper(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/crm/assignable-users', methods=['GET'])
@requires_section('crm')
def api_crm_assignable_users():
    """Active staff for the task-assignee dropdown (internal names only — no worker PII)."""
    try:
        conn = db()
        rows = conn.execute(
            "SELECT id, COALESCE(display_name, full_name, email) AS name, role FROM users "
            "WHERE is_active = 1 AND COALESCE(status,'active') = 'active' "
            "ORDER BY COALESCE(display_name, full_name, email)").fetchall()
        conn.close()
        return response_wrapper([dict(r) for r in rows], count=len(rows))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ============= WORKER INTAKE =============

import re
import shutil

WORKER_RECORDS_DIR = ssc_paths.under_root("worker_records")   # #287
WORKER_RECORDS_DIR.mkdir(parents=True, exist_ok=True)


def _worker_folder_resolve(stored, employee_id, name):
    """#294 S4 — the ONE way worker-record WRITE flows turn employees.folder_path
    into an on-disk folder. Stored rows go through THE resolver (pre-#287 rows
    are Windows-absolute and can never sit under a Linux root raw); an empty row
    mints the standard layout under the active root. Returns
    (folder: Path, store_value: str | None) — store_value is the PORTABLE
    relative row to write back, set ONLY when the row was empty (existing rows
    stay untouched per the #287 zero-mutation posture)."""
    if stored:
        return ssc_paths.resolve_data_path(stored), None
    folder = WORKER_RECORDS_DIR / f"{employee_id}_{slugify_name(name or employee_id)}"
    return folder, ssc_paths.store_rel(folder)


def slugify_name(name):
    """Convert worker name to safe folder name. 'José Vargas' -> 'Jose_Vargas'."""
    if not name:
        return "unknown"
    # Normalize accented chars to ASCII
    import unicodedata
    normalized = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    # Replace whitespace runs with underscore, strip non-safe chars
    cleaned = re.sub(r'[^A-Za-z0-9 _-]', '', normalized)
    return re.sub(r'\s+', '_', cleaned).strip('_') or "unknown"


def next_employee_id():
    """Generate next E-XXXXX employee ID. Uses NUMERIC max (not lexicographic)
    so mixed-width existing IDs (E-001, E-012, E-00013) all sort correctly."""
    conn = db()
    # Cast the numeric suffix to INTEGER for proper max calculation
    row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(employee_id, 3) AS INTEGER)) AS max_n "
        "FROM employees WHERE employee_id LIKE 'E-%'"
    ).fetchone()
    conn.close()
    max_n = (row["max_n"] if row and row["max_n"] is not None else 0)
    return f"E-{max_n+1:05d}"


@app.route('/api/workers/create', methods=['POST'])
@requires_company
def api_worker_create():
    """Create a new worker record + worker folder. Returns the new employee_id,
    auto-assigned worker_id (W-####), and folder path.

    WF-1 fix: every new onboard now allocates the next sequential W-####
    via worker_id.assign_worker_id (same allocator used by /api/employees
    POST + bulk CSV import — single source of truth). Before this fix the
    INSERT here omitted the worker_id column entirely, leaving every
    post-baseline worker with NULL and silently breaking the DCR roster /
    workforce list / credentials downstream."""
    from worker_id import assign_worker_id
    try:
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 400

        supplied_id = (data.get("employee_id") or "").strip()
        if supplied_id and not re.fullmatch(r"E-\d{5}", supplied_id):   # #305 — no traversal via the id
            return jsonify({"error": "employee_id must look like E-00042"}), 400
        employee_id = supplied_id or next_employee_id()
        folder_slug = slugify_name(name)
        def _worker_folder(eid):
            f = WORKER_RECORDS_DIR / f"{eid}_{folder_slug}"
            if not f.resolve().is_relative_to(WORKER_RECORDS_DIR.resolve()):   # #305 — containment
                raise ValueError("worker folder escapes the records root")
            return f
        folder = _worker_folder(employee_id)
        (folder / "id").mkdir(parents=True, exist_ok=True)
        (folder / "certs").mkdir(parents=True, exist_ok=True)

        conn = db()
        # Allocate the next W-#### inside the transaction so concurrent
        # onboards can't race (UNIQUE index on worker_id catches stragglers).
        worker_id = assign_worker_id(conn)

        # WF-3: derive PIN from last-4 of phone server-side — never accept a
        # hand-entered PIN from the onboard form (the input was removed).
        # PATCH /api/employees on phone change does the same derivation +
        # collision check; mirror that contract here so single-onboard
        # / edit / bulk-import all produce the same PIN for the same phone.
        # PII rule: PIN is derived, not stored from user input — and never
        # echoed back to logs (CLAUDE.md PII rule line 38).
        phone_raw = data.get("phone") or ""
        phone_digits = re.sub(r'\D', '', phone_raw)
        derived_pin = phone_digits[-4:] if len(phone_digits) >= 4 else None
        if derived_pin:
            # Collision check — phone last-4 must be unique across employees.
            # Active operating cohort is ~10 workers so collisions are rare,
            # but the worker app's PIN lookup keys on this so a collision
            # would silently send the operator to the wrong worker.
            collision = conn.execute(
                "SELECT employee_id FROM employees WHERE pin = ? AND employee_id != ?",
                (derived_pin, employee_id)
            ).fetchone()
            if collision:
                conn.close()
                return jsonify({
                    "error": (f"PIN collision (phone last-4 = {derived_pin}) "
                              f"with an existing worker — please use a phone "
                              f"with different last-4 digits")
                }), 409
        # If caller explicitly passed a 'pin' (legacy CSV import path), let it
        # win as a safety valve; otherwise the derived value goes in.
        if not data.get("pin"):
            data = dict(data)
            data["pin"] = derived_pin
        # Mirror the digits-only normalization the PATCH path uses, so the
        # stored phone format is consistent regardless of how it was entered.
        if phone_digits:
            data["phone"] = phone_digits
        # #305 — no more INSERT OR IGNORE (a concurrent onboard used to return 200 with
        # a folder on disk and NO row). Plain INSERT; on an id collision an auto-allocated
        # id is re-allocated and retried, a caller-supplied id falls through to UPDATE.
        def _insert_employee(eid, fold):
            conn.execute(
                """INSERT INTO employees
                   (employee_id, worker_id, name, trade, dob, phone, email,
                    emergency_contact_name, emergency_contact_phone, emergency_contact_relation,
                    language, hire_date, pin, folder_path, intake_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    eid, worker_id, name, data.get("trade"),
                    data.get("dob"), data.get("phone"), data.get("email"),
                    data.get("emergency_contact_name"), data.get("emergency_contact_phone"),
                    data.get("emergency_contact_relation"),
                    data.get("language"), data.get("hire_date"), data.get("pin"),
                    str(fold), "pending"
                )
            )
        def _is_collision(exc):
            msg = str(exc).lower()
            return "unique" in msg or "duplicate key" in msg or "constraint" in msg
        inserted = False
        for _attempt in range(6):
            try:
                _insert_employee(employee_id, folder)
                inserted = True
                break
            except Exception as _e:
                if not _is_collision(_e):
                    raise
                try:
                    conn.rollback()
                except Exception:
                    pass
                if supplied_id:
                    break   # caller-supplied id that already exists -> UPDATE below
                for sub in ("id", "certs"):
                    try:
                        (folder / sub).rmdir()
                    except OSError:
                        pass
                try:
                    folder.rmdir()
                except OSError:
                    pass
                employee_id = next_employee_id()
                folder = _worker_folder(employee_id)
                (folder / "id").mkdir(parents=True, exist_ok=True)
                (folder / "certs").mkdir(parents=True, exist_ok=True)
        if not inserted and not supplied_id:
            conn.close()
            return jsonify({"error": "could not allocate an employee id, try again"}), 503
        if not inserted and supplied_id:
            conn.execute(
                """UPDATE employees SET name=COALESCE(?, name), trade=COALESCE(?, trade),
                      dob=COALESCE(?, dob), phone=COALESCE(?, phone), email=COALESCE(?, email),
                      emergency_contact_name=COALESCE(?, emergency_contact_name),
                      emergency_contact_phone=COALESCE(?, emergency_contact_phone),
                      emergency_contact_relation=COALESCE(?, emergency_contact_relation),
                      language=COALESCE(?, language), hire_date=COALESCE(?, hire_date),
                      pin=COALESCE(?, pin), folder_path=?, updated_at=CURRENT_TIMESTAMP
                   WHERE employee_id=?""",
                (
                    name, data.get("trade"), data.get("dob"), data.get("phone"),
                    data.get("email"), data.get("emergency_contact_name"),
                    data.get("emergency_contact_phone"), data.get("emergency_contact_relation"),
                    data.get("language"), data.get("hire_date"), data.get("pin"),
                    str(folder), employee_id
                )
            )
        # Auto-assign to projects specified, OR default to all active projects
        project_codes = data.get("project_codes") or []
        if not project_codes:
            # Default: assign to all currently-active projects (typically just one today)
            actives = conn.execute(
                "SELECT project_code FROM projects WHERE status = 'active' OR status IS NULL"
            ).fetchall()
            project_codes = [r["project_code"] for r in actives]

        for pcode in project_codes:
            existing = conn.execute(
                "SELECT id FROM project_assignments WHERE project_code = ? AND employee_id = ? AND status = 'active'",
                (pcode, employee_id)
            ).fetchone()
            if not existing:
                conn.execute(
                    """INSERT INTO project_assignments
                       (project_code, employee_id, role_on_project, start_date, status)
                       VALUES (?, ?, ?, DATE('now'), 'active')""",
                    (pcode, employee_id, data.get("trade"))
                )

        conn.commit()
        conn.close()
        # #247 — no folder_path / folder_slug (name-bearing) in the response.
        # The worker folder is a server-side concept; the UI shows worker_id.
        return response_wrapper({
            "employee_id": employee_id,
            "worker_id": worker_id,
            "name": name,
            "assigned_to": project_codes,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/workers/<employee_id>', methods=['GET'])
@requires_company
def api_worker_get(employee_id):
    """Return full worker record + all documents + all certs.

    #246 — sourced from the canonical roster view so labor_status rides
    along: the profile modal (a MASTER surface) badges deactivated workers."""
    try:
        conn = db()
        emp = conn.execute(
            "SELECT * FROM v_worker_roster WHERE employee_id = ?", (employee_id,)
        ).fetchone()
        if not emp:
            conn.close()
            return jsonify({"error": "not found"}), 404

        docs = conn.execute(
            "SELECT * FROM worker_documents WHERE employee_id = ? ORDER BY uploaded_at DESC",
            (employee_id,)
        ).fetchall()
        certs = conn.execute(
            """SELECT c.*, ct.name AS cert_name, ct.description AS cert_description,
                      ct.is_cof_prerequisite
               FROM certifications c
               JOIN cert_types ct ON ct.cert_type_id = c.cert_type_id
               WHERE c.employee_id = ?
               ORDER BY c.expiration_date ASC""",
            (employee_id,)
        ).fetchall()
        conn.close()

        # #247 — emit GATED file_url (the /worker-files/ route, auth-gated) in
        # place of the raw file_path the document thumbnail used to read. Same
        # /worker-files/<rel> pattern the cert/face responses already use.
        doc_dicts = []
        for d in docs:
            dd = dict(d)
            fp = dd.get('file_path')
            if fp:
                try:
                    # #294 S4 — resolver first: pre-#287 rows are Windows-absolute
                    # (relative_to raised on Linux -> file_url None -> dead thumbs).
                    rel = ssc_paths.resolve_data_path(fp).resolve().relative_to(WORKER_RECORDS_DIR.resolve())
                    dd['file_url'] = "/worker-files/" + str(rel).replace("\\", "/")
                except (ValueError, OSError):
                    dd['file_url'] = None
            doc_dicts.append(dd)

        # employee/docs/certs all SELECT * (face_image_path, folder_path,
        # photo_path, file_path, scan_path). Scrub the whole tree; consumers use
        # the gated routes (face-photo, file_url) + original_filename, not paths.
        return response_wrapper(scrub_paths({
            "employee": dict(emp),
            "documents": doc_dicts,
            "certifications": [dict(c) for c in certs],
        }))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/workers/<employee_id>/upload', methods=['POST'])
@requires_company
def api_worker_upload(employee_id):
    """Upload a scan to a worker's folder.
    Multipart form fields: file=<binary>, doc_type=<string>, doc_label=<string>, related_cert_id=<int|null>.

    Security controls applied:
      1. MIME type must be in ALLOWED_DOC_MIME_TYPES whitelist
      2. File extension must be in ALLOWED_DOC_EXTENSIONS whitelist
      3. File size capped at 20MB (config'd above)
      4. Server-generated UUID filename; original_filename stored separately
      5. doc_type validated against fixed enum (no folder path injection)
      6. Path traversal proof: final path must resolve inside WORKER_RECORDS_DIR
    """
    try:
        if 'file' not in request.files:
            return jsonify({"error": "no file in request"}), 400
        f = request.files['file']
        if not f.filename:
            return jsonify({"error": "empty filename"}), 400

        # ----- SECURITY: MIME + extension whitelist -----
        if f.mimetype not in ALLOWED_DOC_MIME_TYPES:
            return jsonify({"error": f"file type {f.mimetype} not allowed. Allowed: images + PDF"}), 400
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_DOC_EXTENSIONS:
            return jsonify({"error": f"file extension {ext} not allowed"}), 400

        # ----- SECURITY: doc_type enum validation -----
        ALLOWED_DOC_TYPES = {
            'id_front', 'id_back', 'passport', 'idnyc',
            'sst_front', 'sst_back',
            'cert_front', 'cert_back', 'cert_combined',
            'other'
        }
        doc_type = request.form.get("doc_type", "other")
        if doc_type not in ALLOWED_DOC_TYPES:
            return jsonify({"error": f"invalid doc_type {doc_type}"}), 400

        conn = db()
        emp = conn.execute(
            "SELECT employee_id, name, folder_path FROM employees WHERE employee_id = ?",
            (employee_id,)
        ).fetchone()
        if not emp:
            conn.close()
            return jsonify({"error": "employee not found"}), 404

        # #294 S4 — stored rows resolve through THE resolver; an empty row mints
        # the standard layout and stores it PORTABLE (relative).
        folder, _store = _worker_folder_resolve(emp["folder_path"], employee_id, emp["name"])
        if _store:
            conn.execute(
                "UPDATE employees SET folder_path = ? WHERE employee_id = ?",
                (_store, employee_id)
            )

        # ----- SECURITY: path traversal proof -----
        folder = folder.resolve()
        if not folder.is_relative_to(WORKER_RECORDS_DIR.resolve()):
            conn.close()
            return jsonify({"error": "invalid folder path"}), 400

        doc_label = request.form.get("doc_label", "")[:200]      # cap to prevent excessive sizes
        related_cert_id = request.form.get("related_cert_id")
        if related_cert_id:
            try:
                related_cert_id = int(related_cert_id)
            except ValueError:
                related_cert_id = None
        else:
            related_cert_id = None

        # Choose subfolder (no user input affects the path beyond enum)
        if doc_type in ("id_front", "id_back", "passport", "idnyc"):
            subfolder = folder / "id"
        else:
            subfolder = folder / "certs"
        subfolder.mkdir(parents=True, exist_ok=True)

        # ----- SECURITY: UUID filename (no user input in path) -----
        from datetime import datetime as dt
        stamp = dt.utcnow().strftime("%Y%m%d-%H%M%S")
        target_filename = f"{doc_type}_{stamp}_{uuid.uuid4().hex[:8]}{ext}"
        target_path = (subfolder / target_filename).resolve()

        # Final path-traversal check after resolve()
        if not target_path.is_relative_to(WORKER_RECORDS_DIR.resolve()):
            conn.close()
            return jsonify({"error": "path traversal blocked"}), 400

        f.save(str(target_path))
        size = target_path.stat().st_size

        # Record in DB. Original filename is what the user uploaded; for audit only — never re-used as a path.
        # #294 S4 — the stored row is PORTABLE (store_rel), never a host-absolute path.
        cur = conn.execute(
            """INSERT INTO worker_documents
               (employee_id, doc_type, doc_label, file_path, original_filename,
                mime_type, file_size_bytes, related_cert_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (employee_id, doc_type, doc_label, ssc_paths.store_rel(target_path), f.filename[:255],
             f.mimetype, size, related_cert_id)
        )
        doc_id = cur.lastrowid
        conn.commit()
        conn.close()

        # #247 — gated file_url, never the raw path.
        try:
            _rel = target_path.resolve().relative_to(WORKER_RECORDS_DIR.resolve())
            _file_url = "/worker-files/" + str(_rel).replace("\\", "/")
        except (ValueError, OSError):
            _file_url = None
        return response_wrapper({
            "doc_id": doc_id,
            "file_url": _file_url,
            "size_bytes": size,
            "doc_type": doc_type,
            "doc_label": doc_label,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/workers/<employee_id>/documents/<int:doc_id>', methods=['DELETE'])
@requires_company
def api_worker_delete_document(employee_id, doc_id):
    """Delete a worker document. Removes the file from disk AND the DB row.
    Path is validated to ensure deletion can't escape the worker_records dir."""
    try:
        conn = db()
        row = conn.execute(
            "SELECT file_path FROM worker_documents WHERE id = ? AND employee_id = ?",
            (doc_id, employee_id)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "not found"}), 404

        # Security: only delete files inside worker_records dir
        try:
            # #294 S4 — resolver first (stored rows may be pre-#287 absolute or portable)
            target = ssc_paths.resolve_data_path(row["file_path"]).resolve()
            if not target.is_relative_to(WORKER_RECORDS_DIR.resolve()):
                conn.close()
                return jsonify({"error": "invalid file path"}), 400
            if target.exists():
                target.unlink()
        except Exception as e:
            logging.warning(f"Could not delete file: {e}")

        conn.execute("DELETE FROM worker_documents WHERE id = ?", (doc_id,))
        conn.commit()
        conn.close()
        return response_wrapper({"deleted": doc_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/workers/<employee_id>/certs/<int:cert_id>', methods=['DELETE'])
@requires_company
def api_worker_delete_cert(employee_id, cert_id):
    """Delete a single certification entry from a worker."""
    try:
        conn = db()
        conn.execute(
            "DELETE FROM certifications WHERE id = ? AND employee_id = ?",
            (cert_id, employee_id)
        )
        conn.commit()
        conn.close()
        return response_wrapper({"deleted": cert_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/workers/<employee_id>/certs/<int:cert_id>', methods=['PATCH'])
@requires_company
def api_worker_patch_cert(employee_id, cert_id):
    """Edit fields on an existing certification row.

    Editable: cert_type_id, date_obtained, expiration_date, status,
    card_number, issuing_body, notes, scan_path. Mirrors the validation
    in POST/import (dates ISO YYYY-MM-DD, expiration_date >= date_obtained
    when both supplied). 404 if the cert id doesn't belong to the named
    employee — prevents cross-worker edits via guessed ids."""
    EDITABLE = {'cert_type_id', 'date_obtained', 'expiration_date', 'status',
                'card_number', 'issuing_body', 'notes', 'scan_path'}
    data = request.get_json(silent=True) or {}
    updates = {}
    for k in EDITABLE:
        if k in data:
            v = data[k]
            if isinstance(v, str):
                v = v.strip() or None
            updates[k] = v
    if not updates:
        return jsonify({"error": "no editable fields in payload"}), 400
    for df in ('date_obtained', 'expiration_date'):
        if df in updates and updates[df]:
            try:
                datetime.strptime(updates[df], '%Y-%m-%d')
            except ValueError:
                return jsonify({"error": f"{df} must be ISO YYYY-MM-DD"}), 400

    conn = db()
    try:
        existing = conn.execute(
            "SELECT id, date_obtained, expiration_date FROM certifications "
            "WHERE id = ? AND employee_id = ?",
            (cert_id, employee_id)
        ).fetchone()
        if not existing:
            return jsonify({"error": "certification not found"}), 404
        # Cross-field validation against the post-merge state, not just the
        # incoming payload — operator can update one half of the date pair.
        merged_do = updates.get('date_obtained', existing['date_obtained'])
        merged_exp = updates.get('expiration_date', existing['expiration_date'])
        if merged_do and merged_exp and merged_exp < merged_do:
            return jsonify({
                "error": f"expiration_date ({merged_exp}) is before date_obtained ({merged_do})"
            }), 400
        set_clauses = [f"{k} = ?" for k in updates] + ["updated_at = CURRENT_TIMESTAMP"]
        params = list(updates.values()) + [cert_id]
        conn.execute(
            f"UPDATE certifications SET {', '.join(set_clauses)} WHERE id = ?",
            params
        )
        conn.commit()
        row = conn.execute("SELECT * FROM certifications WHERE id = ?", (cert_id,)).fetchone()
        # #247 — certifications SELECT * carries scan_path/file_path; scrub.
        return response_wrapper(scrub_paths(dict(row))), 200
    except Exception as e:
        logging.error(f"PATCH /api/workers/{employee_id}/certs/{cert_id}: {e}")
        return _server_error(e)
    finally:
        conn.close()


@app.route('/api/workers/<employee_id>/certs', methods=['POST'])
@requires_company
def api_worker_add_cert(employee_id):
    """Add a certification to a worker.
    Body: {cert_type_id, date_obtained, expiration_date, card_number, issuing_body, notes, status}
    Returns the new cert row including its id."""
    try:
        data = request.get_json(silent=True) or {}
        cert_type_id = data.get("cert_type_id")
        if not cert_type_id:
            return jsonify({"error": "cert_type_id is required"}), 400

        conn = db()
        # Make sure cert type exists; if not, create it on the fly (free-text new cert)
        ct = conn.execute(
            "SELECT cert_type_id FROM cert_types WHERE cert_type_id = ?",
            (cert_type_id,)
        ).fetchone()
        if not ct:
            new_name = data.get("cert_type_name", cert_type_id)
            conn.execute(
                "INSERT INTO cert_types (cert_type_id, name, description, validity_months) VALUES (?, ?, ?, ?)",
                (cert_type_id, new_name, data.get("cert_type_description"), data.get("validity_months"))
            )

        cur = conn.execute(
            """INSERT INTO certifications
               (employee_id, cert_type_id, date_obtained, expiration_date, status,
                card_number, issuing_body, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                employee_id, cert_type_id,
                data.get("date_obtained"), data.get("expiration_date"),
                data.get("status", "active"), data.get("card_number"),
                data.get("issuing_body"), data.get("notes")
            )
        )
        cert_id = cur.lastrowid
        conn.commit()
        conn.close()

        return response_wrapper({"cert_id": cert_id, "cert_type_id": cert_type_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/employees/<employee_id>/certifications/extract', methods=['POST'])
@requires_company
def api_certification_extract(employee_id):
    """Upload a cert card photo, save to the worker folder, run vision-based
    extraction, return structured cert data for operator review.

    Does NOT insert into certifications — the operator reviews the extracted
    fields and then POSTs to /api/employees/<id>/certifications to save.

    Multipart form fields: file=<image>.

    Returns {extracted, image_path, image_url}. image_url is what the
    operator's browser fetches to render the saved photo via /worker-files/.
    """
    import re as _re
    try:
        if 'file' not in request.files:
            return jsonify({"error": "no file in request"}), 400
        f = request.files['file']
        if not f.filename:
            return jsonify({"error": "empty filename"}), 400

        # Same whitelist used by api_worker_upload — images + PDF.
        if f.mimetype not in ALLOWED_DOC_MIME_TYPES:
            return jsonify({"error": f"file type {f.mimetype} not allowed"}), 400
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_DOC_EXTENSIONS:
            return jsonify({"error": f"file extension {ext} not allowed"}), 400

        conn = db()
        emp = conn.execute(
            "SELECT employee_id, name, folder_path FROM employees WHERE employee_id = ?",
            (employee_id,)
        ).fetchone()
        if not emp:
            conn.close()
            return jsonify({"error": "employee not found"}), 404

        # Back-fill folder_path if the row predates the standardized layout.
        # #294 S4 — stored rows resolve through THE resolver; minted rows are PORTABLE.
        folder, _store = _worker_folder_resolve(emp["folder_path"], employee_id, emp["name"])
        if _store:
            conn.execute(
                "UPDATE employees SET folder_path = ? WHERE employee_id = ?",
                (_store, employee_id)
            )
            conn.commit()

        folder = folder.resolve()
        if not folder.is_relative_to(WORKER_RECORDS_DIR.resolve()):
            conn.close()
            return jsonify({"error": "invalid folder path"}), 400

        certs_dir = folder / "certs"
        certs_dir.mkdir(parents=True, exist_ok=True)

        # cert_N.<ext>. Pick N = max(existing) + 1 — never overwrite.
        existing_nums = []
        for p in certs_dir.iterdir():
            m = _re.match(r"^cert_(\d+)", p.name)
            if m:
                existing_nums.append(int(m.group(1)))
        next_n = (max(existing_nums) + 1) if existing_nums else 1
        target_path = (certs_dir / f"cert_{next_n}{ext}").resolve()
        if not target_path.is_relative_to(WORKER_RECORDS_DIR.resolve()):
            conn.close()
            return jsonify({"error": "path traversal blocked"}), 400

        f.save(str(target_path))
        conn.close()

        # AI extraction is OPTIONAL — the server boots without
        # ANTHROPIC_API_KEY. When it's not configured (continuous-run Path A),
        # the cert-card photo is still saved to the worker folder and the
        # operator can fall back to manual cert entry. Return 503 with a clean
        # "feature disabled" signal so the UI can show "AI unavailable — use
        # manual entry" rather than treating it as an error.
        relative = target_path.relative_to(WORKER_RECORDS_DIR.resolve())
        image_url = "/worker-files/" + str(relative).replace("\\", "/")
        if not os.environ.get('ANTHROPIC_API_KEY'):
            return jsonify({
                "error": "AI extraction disabled — ANTHROPIC_API_KEY not configured",
                "ai_available": False,
                "image_url": image_url,  # #247 — gated route, no image_path
                "note": "Image saved. Use manual entry to record cert data.",
            }), 503

        # Run extraction. If the API call fails, leave the image on disk so the
        # operator can retry or do manual entry — return 500 with the path.
        try:
            cert_types = load_cert_types_from_db(DB_PATH)
            extracted = extract_cert_from_image(target_path, cert_types)
        except (RuntimeError, FileNotFoundError) as e:
            app.logger.error(f"cert extraction failed for {target_path.name}: {e}")
            return jsonify({
                "error": f"extraction failed: {e}",
                "image_url": image_url,  # #247 — gated route, no image_path
                "note": "image saved on disk; operator can retry or use manual entry",
            }), 500

        return response_wrapper({
            "extracted": extracted,
            "image_url": image_url,  # #247 — gated route, no image_path
        })
    except Exception as e:
        app.logger.error(f"POST /api/employees/{employee_id}/certifications/extract failed: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/employees/<employee_id>/certifications', methods=['POST'])
def api_certification_save(employee_id):
    """Save a confirmed certification row after operator review. Mirrors the
    validation rules in import_certifications.py and dedups on the same
    4-tuple (employee_id, cert_type_id, card_number, date_obtained).

    JSON body: cert_type_id (required), card_number, date_obtained,
    expiration_date, issuing_body, notes, scan_path.

    On dedup hit: 200 with {already_exists: True, row}. Otherwise 201 with
    {already_exists: False, cert_id, row}."""
    try:
        data = request.get_json(silent=True) or {}

        cert_type_id = (data.get("cert_type_id") or "").strip()
        card_number = (data.get("card_number") or "").strip() or None
        date_obtained = (data.get("date_obtained") or "").strip() or None
        expiration_date = (data.get("expiration_date") or "").strip() or None
        issuing_body = (data.get("issuing_body") or "").strip() or None
        notes = (data.get("notes") or "").strip() or None
        # #247 — the client round-trips the GATED image_url (/worker-files/<rel>),
        # never a filesystem path. Resolve it back to a path server-side and
        # confine it to WORKER_RECORDS_DIR. (Legacy scan_path body still accepted
        # but only if it resolves inside the worker-records root.)
        scan_path = None
        _img_url = (data.get("image_url") or "").strip()
        if _img_url.startswith("/worker-files/"):
            _cand = (WORKER_RECORDS_DIR / _img_url[len("/worker-files/"):]).resolve()
            if str(_cand).startswith(str(WORKER_RECORDS_DIR.resolve())) and _cand.exists():
                scan_path = str(_cand)
        if scan_path is None:
            _legacy = (data.get("scan_path") or "").strip()
            if _legacy:
                _cand = Path(_legacy).resolve()
                if str(_cand).startswith(str(WORKER_RECORDS_DIR.resolve())):
                    scan_path = str(_cand)

        if not cert_type_id:
            return jsonify({"error": "cert_type_id is required"}), 400
        for df, dval in (("date_obtained", date_obtained), ("expiration_date", expiration_date)):
            if dval:
                try:
                    datetime.strptime(dval, "%Y-%m-%d")
                except ValueError:
                    return jsonify({"error": f"{df} must be ISO YYYY-MM-DD"}), 400
        if date_obtained and expiration_date and expiration_date < date_obtained:
            return jsonify({
                "error": f"expiration_date ({expiration_date}) is before date_obtained ({date_obtained})"
            }), 400

        conn = db()

        emp = conn.execute(
            "SELECT employee_id FROM employees WHERE employee_id = ?",
            (employee_id,)
        ).fetchone()
        if not emp:
            conn.close()
            return jsonify({"error": "employee not found"}), 404

        ct = conn.execute(
            "SELECT cert_type_id FROM cert_types WHERE cert_type_id = ?",
            (cert_type_id,)
        ).fetchone()
        if not ct:
            conn.close()
            return jsonify({
                "error": f"cert_type_id={cert_type_id!r} not in cert_types library"
            }), 400

        # 4-tuple dedup. IFNULL(...) so NULL == NULL behaves intuitively.
        existing = conn.execute(
            "SELECT id FROM certifications "
            "WHERE employee_id = ? AND cert_type_id = ? "
            "AND COALESCE(card_number, '') = COALESCE(?, '') "
            "AND COALESCE(date_obtained, '') = COALESCE(?, '')",
            (employee_id, cert_type_id, card_number, date_obtained)
        ).fetchone()
        if existing:
            row = conn.execute(
                "SELECT * FROM certifications WHERE id = ?", (existing["id"],)
            ).fetchone()
            conn.close()
            return response_wrapper({
                "cert_id": existing["id"],
                "already_exists": True,
                "row": dict(row),
            }), 200

        cur = conn.execute(
            """INSERT INTO certifications
               (employee_id, cert_type_id, card_number, date_obtained,
                expiration_date, issuing_body, notes, status, scan_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (employee_id, cert_type_id, card_number, date_obtained,
             expiration_date, issuing_body, notes, scan_path)
        )
        new_id = cur.lastrowid
        conn.commit()
        row = conn.execute(
            "SELECT * FROM certifications WHERE id = ?", (new_id,)
        ).fetchone()
        conn.close()

        return response_wrapper(scrub_paths({
            "cert_id": new_id,
            "already_exists": False,
            "row": dict(row),
        })), 201
    except Exception as e:
        app.logger.error(f"POST /api/employees/{employee_id}/certifications failed: {e}")
        return _server_error(e)


@app.route('/api/employees/<employee_id>/certifications', methods=['GET'])
@requires_section('internal_ops')   # #305
def api_certifications_list(employee_id):
    """List one worker's certifications, joined with cert_types for the
    human-readable cert name and CoF-prereq flag. Adds a derived status field
    so the frontend can color-code at render time without re-computing.

    status_derived values:
      - 'valid'    — no expiration, or expires more than 30 days out
      - 'expiring' — expires within 30 days
      - 'expired'  — expiration date is in the past
      - 'unknown'  — no expiration_date AND no date_obtained
    """
    try:
        conn = db()
        emp = conn.execute(
            "SELECT employee_id FROM employees WHERE employee_id = ?",
            (employee_id,)
        ).fetchone()
        if not emp:
            conn.close()
            return jsonify({"error": "employee not found"}), 404

        rows = conn.execute(
            """SELECT c.id AS cert_id, c.cert_type_id, ct.name AS cert_name,
                      ct.is_cof_prerequisite, c.card_number, c.date_obtained,
                      c.expiration_date, c.issuing_body, c.scan_path, c.status,
                      c.notes, c.created_at, c.updated_at
               FROM certifications c
               LEFT JOIN cert_types ct ON ct.cert_type_id = c.cert_type_id
               WHERE c.employee_id = ?
               ORDER BY
                 CASE WHEN c.expiration_date IS NULL THEN 1 ELSE 0 END,
                 c.expiration_date ASC,
                 c.id DESC""",
            (employee_id,)
        ).fetchall()
        conn.close()

        today = date.today()
        d30 = today + timedelta(days=30)
        today_iso = today.isoformat()
        d30_iso = d30.isoformat()

        out = []
        for r in rows:
            row = dict(r)
            exp = row.get("expiration_date")
            if not exp and not row.get("date_obtained"):
                row["status_derived"] = "unknown"
            elif not exp:
                row["status_derived"] = "valid"
            elif exp < today_iso:
                row["status_derived"] = "expired"
            elif exp <= d30_iso:
                row["status_derived"] = "expiring"
            else:
                row["status_derived"] = "valid"
            # #247 — emit the GATED scan_url only; scan_path is scrubbed below.
            sp = row.get("scan_path")
            if sp:
                try:
                    rel = Path(sp).resolve().relative_to(WORKER_RECORDS_DIR.resolve())
                    row["scan_url"] = "/worker-files/" + str(rel).replace("\\", "/")
                except (ValueError, OSError):
                    row["scan_url"] = None
            else:
                row["scan_url"] = None
            out.append(row)

        return response_wrapper(scrub_paths(out), count=len(out))
    except Exception as e:
        app.logger.error(f"GET /api/employees/{employee_id}/certifications failed: {e}")
        return _server_error(e)


@app.route('/api/employees/<employee_id>/face-photo', methods=['POST'])
def api_employee_face_photo(employee_id):
    """Upload a face photo for the worker. Overwrites any existing face image
    (the worker has one current face — re-taking replaces). Updates
    employees.face_image_path."""
    try:
        if 'file' not in request.files:
            return jsonify({"error": "no file in request"}), 400
        f = request.files['file']
        if not f.filename:
            return jsonify({"error": "empty filename"}), 400

        # Face photos: images only — explicitly reject PDF even though it's
        # in the wider doc-upload whitelist.
        FACE_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif'}
        if f.mimetype not in ALLOWED_DOC_MIME_TYPES or f.mimetype == 'application/pdf':
            return jsonify({"error": f"face photo must be an image (got {f.mimetype})"}), 400
        ext = Path(f.filename).suffix.lower()
        if ext not in FACE_IMAGE_EXTS:
            return jsonify({"error": f"face photo extension {ext} not allowed"}), 400

        conn = db()
        emp = conn.execute(
            "SELECT employee_id, name, folder_path FROM employees WHERE employee_id = ?",
            (employee_id,)
        ).fetchone()
        if not emp:
            conn.close()
            return jsonify({"error": "employee not found"}), 404

        # #294 S4 — stored rows resolve through THE resolver; minted rows are PORTABLE.
        folder, _store = _worker_folder_resolve(emp["folder_path"], employee_id, emp["name"])
        if _store:
            conn.execute(
                "UPDATE employees SET folder_path = ? WHERE employee_id = ?",
                (_store, employee_id)
            )
            conn.commit()

        folder = folder.resolve()
        if not folder.is_relative_to(WORKER_RECORDS_DIR.resolve()):
            conn.close()
            return jsonify({"error": "invalid folder path"}), 400

        folder.mkdir(parents=True, exist_ok=True)

        # Remove any prior face.* so we don't leave a stale file when the
        # extension changes between uploads (face.jpg → face.heic etc.).
        for old in folder.glob("face.*"):
            try:
                old.unlink()
            except OSError as e:
                app.logger.warning(f"could not remove old face file {old}: {e}")

        target_path = (folder / f"face{ext}").resolve()
        if not target_path.is_relative_to(WORKER_RECORDS_DIR.resolve()):
            conn.close()
            return jsonify({"error": "path traversal blocked"}), 400

        f.save(str(target_path))

        # iPhones default to HEIC; browsers can't render HEIC in <img>. Convert
        # to a sibling face.jpg so the profile thumbnail is always renderable.
        # The HEIC original is preserved as source-of-truth; face_image_path
        # points at the JPEG so the UI just works without conditional logic.
        displayable_path = target_path
        heic_conversion = None
        if ext in {'.heic', '.heif'}:
            try:
                from PIL import Image
                import pillow_heif
                pillow_heif.register_heif_opener()
                with Image.open(str(target_path)) as im:
                    if im.mode != 'RGB':
                        im = im.convert('RGB')
                    jpeg_path = (folder / "face.jpg").resolve()
                    im.save(str(jpeg_path), "JPEG", quality=85, optimize=True)
                displayable_path = jpeg_path
                heic_conversion = {"ok": True, "original": str(target_path),
                                   "jpeg": str(jpeg_path)}
            except Exception as e:
                app.logger.warning(
                    f"HEIC->JPEG conversion failed for {employee_id} "
                    f"({target_path.name}): {e}"
                )
                heic_conversion = {"ok": False, "error": str(e)}
                # Fall through: face_image_path still points at the HEIC. The
                # UI will fail to render it but the record + file are intact.

        # #294 S4 — the stored row is PORTABLE (store_rel), never host-absolute.
        conn.execute(
            "UPDATE employees SET face_image_path = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE employee_id = ?",
            (ssc_paths.store_rel(displayable_path), employee_id)
        )
        conn.commit()
        conn.close()

        relative = displayable_path.relative_to(WORKER_RECORDS_DIR.resolve())
        image_url = "/worker-files/" + str(relative).replace("\\", "/")

        # #247 — image_url is the GATED route; no face_image_path on the wire.
        # heic_conversion reduced to a boolean (its original/jpeg keys held
        # filesystem paths) — the UI only needs "did the conversion succeed".
        heic_ok = bool(heic_conversion and heic_conversion.get("ok")) if heic_conversion else None
        return response_wrapper({
            "image_url": image_url,
            "heic_converted": heic_ok,
        })
    except Exception as e:
        app.logger.error(f"POST /api/employees/{employee_id}/face-photo failed: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/employees/<employee_id>/face-photo', methods=['DELETE'])
def api_employee_face_photo_delete(employee_id):
    """Remove a worker's face photo. Clears employees.face_image_path and
    unlinks face.* files from disk. After deletion the worker can't be
    issued a new credential — POST /credential/issue hard-gates on
    face_image_path — until a new face photo is uploaded.

    404 if the worker doesn't exist. Idempotent — a worker who already has
    no face photo returns 200 with {already_absent: true}. DB is the source
    of truth; file unlink errors are logged but don't fail the response."""
    conn = None
    try:
        conn = db()
        emp = conn.execute(
            "SELECT employee_id, face_image_path, folder_path "
            "FROM employees WHERE employee_id = ?",
            (employee_id,)
        ).fetchone()
        if not emp:
            return jsonify({"error": "employee not found"}), 404
        if not emp["face_image_path"]:
            return response_wrapper({
                "employee_id": employee_id,
                "has_photo": False,
                "files_unlinked": 0,
                "already_absent": True,
            }), 200
        conn.execute(
            "UPDATE employees SET face_image_path = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE employee_id = ?",
            (employee_id,)
        )
        conn.commit()

        files_unlinked = 0
        folder = emp["folder_path"]
        if folder:
            try:
                # #294 S4 — resolver first (pre-#287 rows are Windows-absolute)
                fp = ssc_paths.resolve_data_path(folder).resolve()
                if fp.is_relative_to(WORKER_RECORDS_DIR.resolve()):
                    for old in fp.glob("face.*"):
                        try:
                            old.unlink()
                            files_unlinked += 1
                        except OSError as e:
                            app.logger.warning(
                                f"face-photo unlink failed for {employee_id} {old.name}: {e}"
                            )
            except Exception as e:
                app.logger.warning(
                    f"face-photo dir cleanup failed for {employee_id}: {e}"
                )

        return response_wrapper({
            "employee_id": employee_id,
            "has_photo": False,
            "files_unlinked": files_unlinked,
            "already_absent": False,
        }), 200
    except Exception as e:
        logging.error(f"DELETE /api/employees/{employee_id}/face-photo: {e}")
        return _server_error(e)
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


@app.route('/api/employees/<employee_id>/face-photo', methods=['GET'])
@requires_section('internal_ops')   # #305
def api_employee_face_photo_get(employee_id):
    """#223 — serve a worker's headshot for the Employees & Certifications card
    view through THIS auth-gated route only (behind the global auth gate, same
    audience as the per-project roster page). NEVER exposes the path; confined to
    worker_records/; no-store. 404 when there's no browser-renderable photo so the
    card falls back to a non-semantic initials avatar."""
    from flask import send_file
    conn = db()
    try:
        emp = conn.execute("SELECT face_image_path FROM employees WHERE employee_id = ?",
                           (employee_id,)).fetchone()
        if not emp or not emp["face_image_path"]:
            return jsonify({"error": "no photo"}), 404
        # #294 S3 — resolver first (the _fp_serve #290 lesson): pre-#287 rows are
        # Windows-absolute and can never resolve raw on the Linux root — this
        # route 404'd every pre-#287 headshot on the cloud host.
        p = ssc_paths.resolve_data_path(emp["face_image_path"])
        ext = p.suffix.lower()
        if ext not in ('.jpg', '.jpeg', '.png'):
            return jsonify({"error": "unsupported"}), 404
        base = WORKER_RECORDS_DIR.resolve()
        if not (p.resolve().is_relative_to(base) and p.exists()):
            return jsonify({"error": "photo missing"}), 404
        mt = 'image/png' if ext == '.png' else 'image/jpeg'
        resp = send_file(str(p), mimetype=mt)
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp
    finally:
        conn.close()


@app.route('/api/cert-types', methods=['GET'])
@requires_company  # #263 — company Cert Library tab (admin/c_suite only; pm 403)
def api_cert_types():
    """List all cert types for autocomplete + the Cert Library UI.

    Returns category + reference_url so the company-console grouped
    library view can render section headers per category and a
    DOB-PDF link per row. Non-DOB entries (CPR, OSHA-30) have a
    NULL reference_url and render without a link.

    Order: category, then name — natural reading order for the
    grouped table; consumers that need a different sort can re-sort
    client-side.
    """
    try:
        conn = db()
        rows = conn.execute(
            "SELECT cert_type_id, name, description, validity_months, "
            "       is_cof_prerequisite, category, reference_url "
            "FROM cert_types ORDER BY category, name"
        ).fetchall()
        conn.close()
        return response_wrapper(rows_to_dicts(rows))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/workers/intake-summary', methods=['GET'])
@requires_company  # #263 — the company-wide Workforce roster (admin/c_suite only; pm 403)
def api_workers_intake_summary():
    """List of all workers with intake status + cert health + doc count
    + credential eligibility + current_credential state. The two new
    fields let the workforce list render the Action button per row
    without follow-up round-trips.

    Archived workers are filtered out by default. Pass ?include_archived=true
    to include them."""
    try:
        include_archived = (request.args.get('include_archived', '').lower()
                            in ('1', 'true', 'yes'))
        # Lazy import — cof_issuer needs the DB open and we want server.py
        # startup to stay light.
        from cof_issuer import has_valid_prerequisite
        conn = db()
        # #246 — MASTER surface: every (non-archived) worker stays visible;
        # labor_status rides along so the Workforce roster renders an
        # Inactive badge instead of silently hiding retired workers.
        if include_archived:
            emps = conn.execute(
                "SELECT employee_id, worker_id, name, trade, phone, language, intake_status, folder_path, archived_at, labor_status "
                "FROM v_worker_roster ORDER BY worker_id"
            ).fetchall()
        else:
            emps = conn.execute(
                "SELECT employee_id, worker_id, name, trade, phone, language, intake_status, folder_path, labor_status "
                "FROM v_worker_roster WHERE archived_at IS NULL ORDER BY worker_id"
            ).fetchall()

        today = date.today().isoformat()   # #305 — LOCAL
        out = []
        for e in emps:
            emp_id = e["employee_id"]
            doc_count = conn.execute(
                "SELECT COUNT(*) FROM worker_documents WHERE employee_id = ?", (emp_id,)
            ).fetchone()[0]
            certs = conn.execute(
                """SELECT cert_type_id, expiration_date, status FROM certifications
                   WHERE employee_id = ?""", (emp_id,)
            ).fetchall()
            cert_count = len(certs)
            expiring_30 = sum(
                1 for c in certs if c["expiration_date"] and c["expiration_date"] <= str(
                    (date.today() + timedelta(days=30))
                ) and c["expiration_date"] >= today
            )
            expired = sum(
                1 for c in certs if c["expiration_date"] and c["expiration_date"] < today
            )
            # Eligibility: SCAFFOLD-16 or RIGGER-32 currently valid → 'cof'; else 'company_id'.
            eligible, _ = has_valid_prerequisite(emp_id)
            eligibility = 'cof' if eligible else 'company_id'
            # Current credential — CoF (status='issued') takes precedence; fall back to Company ID (status='active').
            # html_url points at the LIVE-render route (#90) — photo/PIN/name
            # come from the worker's current record at view time so later
            # edits (face-photo upload after issuance, phone-driven PIN
            # change, name correction) reflect without re-issue. The
            # frozen html_export_path stays on the row as the archival
            # 'as issued' artifact.
            current_credential = None
            cof_row = conn.execute(
                """SELECT card_id, card_number_display, issued_date, expires_date, status FROM cof_cards
                   WHERE employee_id = ? AND status = 'issued'
                   ORDER BY issued_date DESC LIMIT 1""",
                (emp_id,)
            ).fetchone()
            if cof_row:
                current_credential = {
                    "type": "cof",
                    "card_number_display": cof_row["card_number_display"] or cof_row["card_id"],
                    "issued_date": cof_row["issued_date"],
                    "expires_date": cof_row["expires_date"],
                    "status": cof_row["status"],
                    "html_url": f"/api/cards/{emp_id}/cof/live",
                }
            else:
                cid_row = conn.execute(
                    """SELECT card_id, card_number_display, issued_date, status FROM company_id_cards
                       WHERE employee_id = ? AND status = 'active'
                       ORDER BY issued_date DESC, created_at DESC LIMIT 1""",
                    (emp_id,)
                ).fetchone()
                if cid_row:
                    current_credential = {
                        "type": "company_id",
                        "card_number_display": cid_row["card_number_display"],
                        "issued_date": cid_row["issued_date"],
                        "status": cid_row["status"],
                        "html_url": f"/api/cards/{emp_id}/company_id/live",
                    }
            out.append({
                **dict(e),
                "doc_count": doc_count,
                "cert_count": cert_count,
                "certs_expiring_30d": expiring_30,
                "certs_expired": expired,
                "eligibility": eligibility,
                "current_credential": current_credential,
            })
        conn.close()
        # #247 — dict(e) spreads folder_path (name-bearing); no consumer reads
        # it on the Workforce roster. Scrub before it crosses the wire.
        return response_wrapper(scrub_paths(out))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ============= UNIFIED CREDENTIAL ENDPOINTS =============

@app.route('/api/employees/<emp_id>/credential', methods=['GET'])
def get_employee_credential(emp_id):
    """Return the worker's current credential state + eligibility +
    photo-readiness. Used by the workforce list (per-row context) and
    any per-worker detail view."""
    conn = None
    try:
        from cof_issuer import has_valid_prerequisite
        conn = db()
        emp = conn.execute(
            "SELECT employee_id, face_image_path FROM employees WHERE employee_id = ?",
            (emp_id,)
        ).fetchone()
        if not emp:
            return jsonify({"error": "Employee not found"}), 404
        eligible, _ = has_valid_prerequisite(emp_id)
        eligibility = 'cof' if eligible else 'company_id'
        face_present = bool(emp["face_image_path"])

        current_type = card_number_display_v = issued_date_v = expires_v = status_v = html_export_path = None
        cof_row = conn.execute(
            """SELECT card_id, card_number_display, issued_date, expires_date, status, html_export_path
               FROM cof_cards WHERE employee_id = ? AND status = 'issued'
               ORDER BY issued_date DESC LIMIT 1""",
            (emp_id,)
        ).fetchone()
        if cof_row:
            current_type = 'cof'
            card_number_display_v = cof_row["card_number_display"] or cof_row["card_id"]
            issued_date_v = cof_row["issued_date"]
            expires_v = cof_row["expires_date"]
            status_v = cof_row["status"]
            html_export_path = cof_row["html_export_path"]
        else:
            cid_row = conn.execute(
                """SELECT card_id, card_number_display, issued_date, status, html_export_path
                   FROM company_id_cards WHERE employee_id = ? AND status = 'active'
                   ORDER BY issued_date DESC, created_at DESC LIMIT 1""",
                (emp_id,)
            ).fetchone()
            if cid_row:
                current_type = 'company_id'
                card_number_display_v = cid_row["card_number_display"]
                issued_date_v = cid_row["issued_date"]
                status_v = cid_row["status"]
                html_export_path = cid_row["html_export_path"]

        # html_url points at the LIVE-render route (#90) when a card is
        # current — photo/PIN/name reflect the worker's current row at
        # view time, not the issuance-time snapshot. NULL when no current
        # credential exists for the worker.
        html_url = f"/api/cards/{emp_id}/{current_type}/live" if current_type else None

        payload = {
            "type": current_type,
            "card_number_display": card_number_display_v,
            "issued_date": issued_date_v,
            "status": status_v,
            "html_url": html_url,
            "eligibility": eligibility,
            "face_image_path_present": face_present,
        }
        if current_type == 'cof':
            payload["expires_date"] = expires_v
        return response_wrapper(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/api/cards/<emp_id>/<cred_type>/live', methods=['GET'])
@requires_company   # #305 — renders the live PIN; company roles only (was ungated)
def serve_card_live(emp_id, cred_type):
    """Render the worker's card with LIVE data at request time.

    Operator decision (#90, 'always show current'): the card's
    NAME / PIN / PHOTO come from the worker's CURRENT employees row at
    every view/print — not from the issuance-time snapshot. This auto-
    repairs cards issued before the worker had a photo on file (e.g. an
    empty-photo card from before face-photo upload landed) without
    requiring a re-issue.

    Identity fields (card_number_display, issued_date, expires_date,
    issued_by, rigger_*, signature_path) stay FROZEN — they're properties
    of the physical credential and must not drift once printed.

    URL: /api/cards/<emp_id>/cof/live or /api/cards/<emp_id>/company_id/live.
    Returns rendered HTML (text/html). 404 if the worker doesn't exist or
    has no active card of the requested type.
    """
    import jinja2
    if cred_type not in ('cof', 'company_id'):
        return jsonify({"error": "type must be cof or company_id"}), 400
    conn = None
    try:
        conn = db()
        emp = conn.execute(
            "SELECT employee_id, name, trade, pin, face_image_path "
            "FROM employees WHERE employee_id = ?",
            (emp_id,)
        ).fetchone()
        if not emp:
            return jsonify({"error": "Worker not found"}), 404
        emp = dict(emp)

        # Render-time PIN self-heal (#188 Option A).
        # PINs may be missing if a worker was created via a code path
        # that bypassed WF-3's last-4-phone derivation — historically:
        # rows pre-dating WF-3 / #126, but also any future alternate
        # worker-create path that forgets to derive. Rather than
        # printing '----' on the card or failing the render, generate
        # + persist + audit-log a PIN inline, BEFORE templating. The
        # canonical helper (worker_pin.assign_pin_for_worker) handles
        # phone-last-4 derivation, #133 collision fallback, and the
        # PII-safe audit_log payload.
        from worker_pin import assign_pin_for_worker, is_valid_pin
        if not is_valid_pin(emp.get('pin')):
            user = current_user() or {}
            new_pin = assign_pin_for_worker(
                conn,
                emp_id,
                actor_user_id=user.get('id'),
                actor_role=user.get('role') or 'system',
                source='pin_render_heal',
            )
            if new_pin:
                conn.commit()
                emp['pin'] = new_pin
                # PII rule: don't log the PIN value, only the fact.
                logging.info(
                    f"cards/{cred_type}/live: pin_render_heal applied "
                    f"employee_id={emp_id} actor_role={user.get('role')}"
                )

        if cred_type == 'cof':
            card_row = conn.execute(
                "SELECT card_id, card_number_display, issued_date, expires_date, "
                "issued_by, rigger_name_snapshot, rigger_license_snapshot, signature_path "
                "FROM cof_cards WHERE employee_id = ? AND status = 'issued' "
                "ORDER BY issued_date DESC LIMIT 1",
                (emp_id,)
            ).fetchone()
            template_name = 'cof_card_print.html'
        else:
            # company_id_cards has no signature_path / expires_date columns
            # (those are CoF-only — Company ID has no attestation signature
            # and no expiry).
            card_row = conn.execute(
                "SELECT card_id, card_number_display, issued_date, issued_by "
                "FROM company_id_cards WHERE employee_id = ? AND status = 'active' "
                "ORDER BY issued_date DESC, created_at DESC LIMIT 1",
                (emp_id,)
            ).fetchone()
            template_name = 'company_id_card_print.html'
        if not card_row:
            return jsonify({"error": f"No active {cred_type} card for this worker"}), 404
        card = dict(card_row)

        # LIVE photo — read employees.face_image_path each request. Build a
        # /worker-files/ URL if the file exists; empty string otherwise so
        # the template's {% if %} falls back to its 'PHOTO' placeholder.
        #
        # Cache-bust with the file's mtime (#172): if the operator deletes
        # + re-uploads a photo, the file path is identical (face.jpg per
        # worker) so the browser cache would otherwise serve the prior
        # bytes (or, worse, the prior 401/302 redirect HTML cached during
        # a brief unauthed moment). Suffixing ?v=<mtime> forces a fresh
        # request whenever the underlying file changes.
        photo_url = ''
        face_path = emp.get('face_image_path')
        if face_path:
            # #294 S3 — unconditional resolver: the old `if not is_absolute()` gate
            # skipped it for pre-#287 Windows-absolute rows, defeating the
            # re-anchoring the resolver exists to do on a rooted host.
            fp = ssc_paths.resolve_data_path(face_path)
            if fp.exists():
                try:
                    rel = fp.resolve().relative_to(WORKER_RECORDS_DIR.resolve())
                    photo_url = '/worker-files/' + str(rel).replace('\\', '/')
                    try:
                        photo_url += f'?v={int(fp.stat().st_mtime)}'
                    except OSError:
                        pass
                except ValueError:
                    photo_url = ''

        # Signature stays FROZEN — it's the issuer's at issuance time, not
        # the worker's. Same path logic as the static issuer code; the URL
        # scheme is gated for artifacts (#248) via _file_url_for.
        signature_url = ''
        sig_path = card.get('signature_path')
        if sig_path:
            sig_full = ssc_paths.resolve_data_path(sig_path.lstrip('/'))   # #287
            if sig_full.exists():
                signature_url = _file_url_for(sig_path)

        cnd = card.get('card_number_display') or card.get('card_id') or ''
        # ISSUED_DATE / EXPIRES_DATE rendered as MM-DD-YYYY (the display rule);
        # the full datetime stays in the cof_cards row for audit.
        ctx = {
            'NAME': emp.get('name') or '',
            'EMPLOYEE_ID': emp_id,
            'CARD_NUMBER_DISPLAY': cnd,
            'ISSUED_DATE': _fmt_mdy(card.get('issued_date')),
            'ISSUED_BY': card.get('issued_by') or '',
            'EXPIRES_DATE': _fmt_mdy(card.get('expires_date')),
            'TRADE': emp.get('trade') or '',
            'PIN': emp.get('pin') or '----',
            'PHOTO_URL_OR_BLANK': photo_url,
            'RIGGER_NAME': card.get('rigger_name_snapshot') or '',
            'RIGGER_LICENSE': card.get('rigger_license_snapshot') or '',
            'SIGNATURE_URL': signature_url,
        }
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(SCRIPT_DIR)),
            autoescape=True,
        )
        try:
            html = env.get_template(template_name).render(**ctx)
        except Exception as ex:
            logging.error(f"GET /api/cards/{emp_id}/{cred_type}/live render: {ex}")
            return jsonify({"error": f"Template render failed: {ex}"}), 500
        return Response(html, mimetype='text/html; charset=utf-8')
    except Exception as e:
        logging.error(f"GET /api/cards/{emp_id}/{cred_type}/live: {e}")
        return _server_error(e)
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


@app.route('/api/employees/<emp_id>/credential/issue', methods=['POST'])
def issue_employee_credential(emp_id):
    """Unified credential issuance — dispatches CoF vs Company ID based on
    eligibility. Hard-gates on face_image_path. 409 if there's already an
    active credential of the chosen type unless override_active=true.
    Renders the HTML template inline (Jinja2), saves to disk, updates
    html_export_path on the new row, returns the credential info with
    html_url. Operator opens the URL and uses browser print-to-PDF when
    a paper card is needed (per CLAUDE.md HTML-first rule)."""
    import jinja2
    conn = None
    try:
        data = request.get_json() or {}
        issued_by = data.get('issued_by')
        if not issued_by:
            return jsonify({"error": "issued_by required"}), 400
        override_active = bool(data.get('override_active', False))
        rigger_id = data.get('rigger_id')

        conn = db()
        emp = conn.execute(
            "SELECT employee_id, name, trade, pin, face_image_path FROM employees WHERE employee_id = ?",
            (emp_id,)
        ).fetchone()
        if not emp:
            return jsonify({"error": "Employee not found"}), 404
        if not emp["face_image_path"]:
            return jsonify({"error": "Worker has no face photo on file. Take an ID photo before issuing a credential."}), 400

        from cof_issuer import has_valid_prerequisite, issue_cof, get_default_rigger_for_project
        from company_id_issuer import issue_company_id

        eligible, _ = has_valid_prerequisite(emp_id)
        cred_type = 'cof' if eligible else 'company_id'

        # Cross-type mutual exclusivity: a worker holds exactly one
        # credential at a time. Check for an active credential of EITHER
        # type. If any active credential exists (same or other type) and
        # override_active is false, return 409 with the current credential.
        existing_cof = conn.execute(
            "SELECT card_id, card_number_display, issued_date, expires_date, status FROM cof_cards "
            "WHERE employee_id = ? AND status = 'issued' LIMIT 1",
            (emp_id,)
        ).fetchone()
        existing_cid = conn.execute(
            "SELECT card_id, card_number_display, issued_date, status FROM company_id_cards "
            "WHERE employee_id = ? AND status = 'active' LIMIT 1",
            (emp_id,)
        ).fetchone()
        existing_any = existing_cof or existing_cid

        if existing_any and not override_active:
            if existing_cof:
                current_cred = {
                    "type": "cof",
                    "card_number_display": existing_cof["card_number_display"] or existing_cof["card_id"],
                    "issued_date": existing_cof["issued_date"],
                    "expires_date": existing_cof["expires_date"],
                    "status": existing_cof["status"],
                }
            else:
                current_cred = {
                    "type": "company_id",
                    "card_number_display": existing_cid["card_number_display"],
                    "issued_date": existing_cid["issued_date"],
                    "status": existing_cid["status"],
                }
            return jsonify({
                "error": f"Worker already has an active {current_cred['type']}",
                "current_credential": current_cred,
                "hint": "Pass override_active=true to supersede"
            }), 409

        # override_active=true → supersede CROSS-type rows before issuance.
        # Same-type supersede is handled inside the issuer modules
        # (cof_issuer.issue_cof / company_id_issuer.issue_company_id both
        # mark prior 'issued'/'active' rows as 'replaced' internally).
        if override_active:
            if cred_type == 'cof' and existing_cid:
                conn.execute(
                    "UPDATE company_id_cards SET status='replaced', updated_at=CURRENT_TIMESTAMP "
                    "WHERE employee_id=? AND status='active'",
                    (emp_id,)
                )
            elif cred_type == 'company_id' and existing_cof:
                conn.execute(
                    "UPDATE cof_cards SET status='replaced', updated_at=CURRENT_TIMESTAMP "
                    "WHERE employee_id=? AND status='issued'",
                    (emp_id,)
                )
            conn.commit()
        # Close the first conn before calling into the issuer modules —
        # they open their own conns and SQLite WAL prefers one writer at
        # a time. Reset conn=None so the outer finally doesn't double-close.
        conn.close()
        conn = None

        # Issue the card row
        try:
            if cred_type == 'cof':
                rigger_id_param = rigger_id
                if not rigger_id_param:
                    rigger = get_default_rigger_for_project('FR-BX-001')
                    rigger_id_param = rigger['id'] if rigger else None
                card = issue_cof(emp_id, rigger_id=rigger_id_param, project_code='FR-BX-001')
            else:
                card = issue_company_id(emp_id, issued_by)
        except Exception as ex:
            logging.error(f"POST .../credential/issue ({emp_id}) issuer-step: {ex}")
            return jsonify({"error": f"Issuance failed: {ex}"}), 500

        # Render HTML inline via Jinja2. WeasyPrint subprocess removed —
        # GTK runtime isn't installed on this workstation. Per CLAUDE.md
        # HTML-first rule the operator opens the html_url in a browser
        # and uses Ctrl+P / print-to-PDF when a paper card is needed.
        # pdf_export_path stays NULL on the row for future re-introduction
        # of an automated PDF renderer (WeasyPrint w/ GTK, or Playwright).
        if cred_type == 'cof':
            template_name = 'cof_card_print.html'
            subdir = 'cof'
            cnd = card.get('card_number_display') or card['card_id']
            expires_str = card.get('expires_date') or ''
        else:
            template_name = 'company_id_card_print.html'
            subdir = 'company_id'
            cnd = card['card_number_display']
            expires_str = ''
        # PHOTO_URL_OR_BLANK + SIGNATURE_URL: gated "/project-files/<rel>"
        # (#248) if the file exists, else empty string (the template's
        # {% if %} fallback handles the no-photo / no-signature case).
        photo_url = ''
        photo_snapshot = card.get('photo_snapshot_path')
        if photo_snapshot:
            photo_full = ssc_paths.resolve_data_path(photo_snapshot.lstrip('/'))   # #287
            if photo_full.exists():
                photo_url = _file_url_for(photo_snapshot)
        signature_url = ''
        sig_path = card.get('signature_path')
        if sig_path:
            sig_full = ssc_paths.resolve_data_path(sig_path.lstrip('/'))   # #287
            if sig_full.exists():
                signature_url = _file_url_for(sig_path)
        ctx = {
            'NAME': emp['name'] or '',
            'EMPLOYEE_ID': emp_id,
            'CARD_NUMBER_DISPLAY': cnd or '',
            # MM-DD-YYYY display per the date-format rule; storage unchanged.
            'ISSUED_DATE': _fmt_mdy(card.get('issued_date')),
            'ISSUED_BY': card.get('issued_by') or issued_by,
            'EXPIRES_DATE': _fmt_mdy(expires_str),
            'TRADE': emp['trade'] or '',
            'PIN': emp['pin'] or '----',
            'PHOTO_URL_OR_BLANK': photo_url,
            'RIGGER_NAME': card.get('rigger_name_snapshot') or '',
            'RIGGER_LICENSE': card.get('rigger_license_snapshot') or '',
            'SIGNATURE_URL': signature_url,
        }
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(SCRIPT_DIR)),
            autoescape=True,
        )
        try:
            tpl = env.get_template(template_name)
            html_out = tpl.render(**ctx)
        except Exception as ex:
            logging.error(f"POST .../credential/issue ({emp_id}) jinja render failed: {ex}")
            return jsonify({"error": f"Template render failed: {ex}"}), 500
        html_dir = ssc_paths.under_root("data_room", "credentials", subdir)   # #287
        html_dir.mkdir(parents=True, exist_ok=True)
        html_file = html_dir / f"{emp_id}.html"
        html_file.write_text(html_out, encoding='utf-8')
        # #295 S2 — data-root anchor (containment-anchor family): store_rel is
        # exactly the /project-files rel form AND the portable stored row.
        html_rel = ssc_paths.store_rel(html_file)
        conn = db()
        if cred_type == 'cof':
            conn.execute(
                "UPDATE cof_cards SET html_export_path = ?, updated_at = CURRENT_TIMESTAMP WHERE card_id = ?",
                (html_rel, card['card_id'])
            )
        else:
            conn.execute(
                "UPDATE company_id_cards SET html_export_path = ?, updated_at = CURRENT_TIMESTAMP WHERE card_id = ?",
                (html_rel, card['card_id'])
            )
        conn.commit()
        conn.close()
        conn = None
        html_url = '/project-files/' + html_rel

        response_body = {
            "type": cred_type,
            "card_number_display": cnd,
            "card_id": card['card_id'],
            "issued_date": card['issued_date'],
            "status": card['status'],
            "html_url": html_url,
        }
        if cred_type == 'cof':
            response_body["expires_date"] = card.get('expires_date')
        return response_wrapper(response_body), 201
    except Exception as e:
        logging.error(f"POST .../credential/issue ({emp_id}): {e}")
        return _server_error(e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _current_credential_row(conn, emp_id):
    """Locate the worker's current credential — same dispatch as GET
    /credential. Returns (table_name, dict-row) or (None, None). CoF
    (status='issued') takes precedence over Company ID (status='active')."""
    cof = conn.execute(
        "SELECT * FROM cof_cards WHERE employee_id = ? AND status = 'issued' "
        "ORDER BY issued_date DESC LIMIT 1",
        (emp_id,)
    ).fetchone()
    if cof:
        return ('cof_cards', dict(cof))
    cid = conn.execute(
        "SELECT * FROM company_id_cards WHERE employee_id = ? AND status = 'active' "
        "ORDER BY issued_date DESC, created_at DESC LIMIT 1",
        (emp_id,)
    ).fetchone()
    if cid:
        return ('company_id_cards', dict(cid))
    return (None, None)


@app.route('/api/employees/<emp_id>/credential', methods=['PATCH'])
def patch_employee_credential(emp_id):
    """Edit the worker's CURRENT credential (same dispatch as GET).

    Editable: notes, status (for both card types) + expires_date (CoF only).
    Immutable post-issue: card_id, card_number_display, issued_date,
    photo_snapshot_path, html_export_path, basis_certs_json, rigger_* —
    those are properties of the physical card and must not drift after
    print/handover.

    404 if there's no current credential to edit; 400 if expires_date is
    supplied for a Company ID (no such field on that card type)."""
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        conn = db()
        table, card = _current_credential_row(conn, emp_id)
        if not card:
            return jsonify({"error": "no current credential to edit"}), 404
        EDITABLE = {'notes', 'status'}
        if table == 'cof_cards':
            EDITABLE = EDITABLE | {'expires_date'}
        elif 'expires_date' in data:
            return jsonify({"error": "company_id cards have no expires_date"}), 400
        updates = {}
        for k in EDITABLE:
            if k in data:
                updates[k] = data[k]
        if not updates:
            return jsonify({"error": "no editable fields in payload"}), 400
        if 'expires_date' in updates and updates['expires_date']:
            try:
                datetime.strptime(updates['expires_date'], '%Y-%m-%d')
            except ValueError:
                return jsonify({"error": "expires_date must be YYYY-MM-DD"}), 400
        set_clauses = [f"{k} = ?" for k in updates] + ["updated_at = CURRENT_TIMESTAMP"]
        params = list(updates.values()) + [card['card_id']]
        conn.execute(
            f"UPDATE {table} SET {', '.join(set_clauses)} WHERE card_id = ?",
            params
        )
        conn.commit()
        row = conn.execute(
            f"SELECT * FROM {table} WHERE card_id = ?", (card['card_id'],)
        ).fetchone()
        payload = {
            "type": 'cof' if table == 'cof_cards' else 'company_id',
            "card_id": row['card_id'],
            "card_number_display": row['card_number_display'],
            "status": row['status'],
            "notes": row['notes'],
        }
        if table == 'cof_cards':
            payload["expires_date"] = row['expires_date']
        return response_wrapper(payload), 200
    except Exception as e:
        logging.error(f"PATCH /api/employees/{emp_id}/credential: {e}")
        return _server_error(e)
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


@app.route('/api/employees/<emp_id>/credential', methods=['DELETE'])
def delete_employee_credential(emp_id):
    """Soft-revoke: status='revoked' on the worker's current credential.
    The DB row is preserved (audit trail) and so are the rendered HTML/PDF
    files on disk. After revocation, GET /credential reports no current
    credential — the lookup only finds CoFs with status='issued' or
    Company IDs with status='active' — so the worker becomes credential-
    less and is eligible for re-issuance. 404 if no current credential."""
    conn = None
    try:
        conn = db()
        table, card = _current_credential_row(conn, emp_id)
        if not card:
            return jsonify({"error": "no current credential to revoke"}), 404
        conn.execute(
            f"UPDATE {table} SET status = 'revoked', updated_at = CURRENT_TIMESTAMP "
            f"WHERE card_id = ?",
            (card['card_id'],)
        )
        conn.commit()
        return jsonify({
            "revoked": True,
            "type": 'cof' if table == 'cof_cards' else 'company_id',
            "card_id": card['card_id'],
        }), 200
    except Exception as e:
        logging.error(f"DELETE /api/employees/{emp_id}/credential: {e}")
        return _server_error(e)
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


@app.route('/worker-files/<path:filepath>', methods=['GET'])
@requires_company
def serve_worker_file(filepath):
    """Serve scanned documents back to the dashboard (image preview etc.)."""
    try:
        # Only allow paths inside worker_records dir for safety
        target = (WORKER_RECORDS_DIR / filepath).resolve()
        if not target.resolve().is_relative_to(WORKER_RECORDS_DIR.resolve()):   # #305 — prefix match was not containment
            return jsonify({"error": "invalid path"}), 403
        if not target.exists() or not target.is_file():
            return jsonify({"error": "not found"}), 404
        return send_file(str(target))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ============= WEATHER (Open-Meteo, no API key) =============

_WMO_LABELS = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Rain showers", 81: "Rain showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Severe thunderstorm",
}


# #217 — weather cache. Weather is the one inherently-online widget; cache
# each (lat,lng,date) fetch for ~30 min so repeated widget loads + every
# concurrent operator don't hammer Open-Meteo, and so a provider outage can
# fall back to the last-good value (marked stale) instead of breaking the page.
_WEATHER_CACHE = {}
_WEATHER_TTL_SECONDS = 1800  # 30 min
_WEEKDAY_ABBR = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']


def fetch_open_meteo_weather(lat, lng, date_str=None):
    """Fetch weather for a date (YYYY-MM-DD).

    For TODAY: returns current conditions (temp, feels-like, wind, condition,
    today's high/low + rain chance) AND a 5-day daily forecast — each day with
    weekday, weather_code, high/low, and precipitation probability (#217).
    For dates >5 days back: the archive endpoint. For 1-5 days back / future:
    the forecast endpoint with a date filter. Same dict shape regardless of
    source so callers (incl. the DCR aggregator) don't care which was hit.

    Cached per (lat,lng,date) for ~30 min; on a provider failure, degrades to
    the last-good cached value (marked stale) so the widget never breaks the
    page offline. Raises only if there is nothing cached to fall back to."""
    import urllib.request, urllib.parse

    target_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today()
    today = date.today()
    days_back = (today - target_date).days
    is_archive = days_back > 5
    is_today = target_date == today

    cache_key = (round(float(lat), 4), round(float(lng), 4), target_date.isoformat())
    now = datetime.utcnow()
    cached = _WEATHER_CACHE.get(cache_key)
    if cached and (now - cached["ts"]).total_seconds() < _WEATHER_TTL_SECONDS:
        return cached["data"]

    endpoint = "https://archive-api.open-meteo.com/v1/archive" if is_archive \
        else "https://api.open-meteo.com/v1/forecast"

    # precipitation_probability_max is a FORECAST-only daily variable — the
    # archive endpoint rejects it (400), so only request it off-archive.
    daily_vars = "temperature_2m_max,temperature_2m_min,weather_code"
    if not is_archive:
        daily_vars += ",precipitation_probability_max"

    params = {
        "latitude": lat, "longitude": lng,
        "start_date": target_date.isoformat(),
        "end_date": target_date.isoformat(),
        "daily": daily_vars,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": "America/New_York",
    }
    if is_today:
        # Current block (incl. feels-like) + a 5-day daily window for the row.
        params["current"] = "temperature_2m,apparent_temperature,wind_speed_10m,wind_direction_10m,weather_code"
        params["end_date"] = (today + timedelta(days=4)).isoformat()
    else:
        # Past/future single day: pick a representative hourly reading.
        params["hourly"] = "temperature_2m,wind_speed_10m,wind_direction_10m,weather_code"

    url = endpoint + "?" + urllib.parse.urlencode(params)
    # 5s ceiling — fail fast when Open-Meteo is unreachable (the DCR aggregator
    # stays inside the smoke's 15s ceiling). On failure, degrade to last-good.
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        if cached:
            stale = dict(cached["data"])
            stale["stale"] = True
            return stale
        raise

    cur = data.get("current", {}) or {}
    daily = data.get("daily", {}) or {}
    hourly = data.get("hourly", {}) or {}

    if is_today and cur:
        temp_now = cur.get("temperature_2m")
        wind_mph = cur.get("wind_speed_10m")
        wind_dir_deg = cur.get("wind_direction_10m")
        condition_code = cur.get("weather_code")
        feels_like = cur.get("apparent_temperature")
    else:
        # Past/future day: pick noon (or midpoint) as the representative reading.
        temps = hourly.get("temperature_2m", []) or []
        winds = hourly.get("wind_speed_10m", []) or []
        wdirs = hourly.get("wind_direction_10m", []) or []
        codes = hourly.get("weather_code", []) or []
        idx = 12 if len(temps) > 12 else (len(temps) // 2 if temps else 0)
        temp_now = temps[idx] if temps else None
        wind_mph = winds[idx] if winds else None
        wind_dir_deg = wdirs[idx] if wdirs else None
        condition_code = codes[idx] if codes else None
        feels_like = None

    dmax = daily.get("temperature_2m_max") or []
    dmin = daily.get("temperature_2m_min") or []
    dcode = daily.get("weather_code") or []
    dprob = daily.get("precipitation_probability_max") or []
    ddate = daily.get("time") or []

    out = {
        "temp_now": temp_now,
        "feels_like": feels_like,
        "wind_mph": wind_mph,
        "wind_dir_deg": wind_dir_deg,
        "condition_code": condition_code,
        "temp_max": dmax[0] if dmax else None,
        "temp_min": dmin[0] if dmin else None,
        "precip_prob_today": dprob[0] if dprob else None,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "date": target_date.isoformat(),
        "source": "open-meteo-archive" if is_archive else "open-meteo-forecast",
    }

    # 5-day daily forecast (today only). Weekday is derived from the
    # America/New_York date STRING (no UTC timestamp) so MON..SUN are correct
    # locally per the dates rule.
    forecast = []
    if is_today:
        for i in range(min(5, len(ddate))):
            try:
                di = datetime.strptime(ddate[i], '%Y-%m-%d').date()
                wd = _WEEKDAY_ABBR[di.weekday()]
            except Exception:
                wd = ''
            forecast.append({
                "date": ddate[i],
                "weekday": wd,
                "code": dcode[i] if i < len(dcode) else None,
                "temp_max": dmax[i] if i < len(dmax) else None,
                "temp_min": dmin[i] if i < len(dmin) else None,
                "precip_prob": dprob[i] if i < len(dprob) else None,
            })
    out["forecast"] = forecast

    deg = out.get("wind_dir_deg")
    if deg is not None:
        compass = ["N","NE","E","SE","S","SW","W","NW","N"]
        out["wind_dir"] = compass[int((deg + 22.5) // 45)]
    out["condition_label"] = _WMO_LABELS.get(out.get("condition_code"), "—")

    _WEATHER_CACHE[cache_key] = {"data": out, "ts": now}
    return out


@app.route('/api/weather', methods=['GET'])
def api_weather():
    """Live weather for a project's coords + a given date. Default lat/lng
    is the 890 E 135th Street site (Bronx). ?date=YYYY-MM-DD selects the day —
    today by default; past dates >5 days back pull from Open-Meteo's archive
    endpoint. Same response shape regardless of which endpoint was hit."""
    try:
        lat = float(request.args.get("lat", 40.8083))
        lng = float(request.args.get("lng", -73.9162))
        date_str = request.args.get("date")
        if date_str:
            try:
                datetime.strptime(date_str, '%Y-%m-%d')
            except ValueError:
                return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        out = fetch_open_meteo_weather(lat, lng, date_str)
        return response_wrapper(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/weather/week', methods=['GET'])
def api_weather_week():
    """The 7-day weather window behind Field today (weather_week.py). Same default site
    coords as /api/weather; 503 when the forecast is unreachable and nothing is cached."""
    import weather_week
    try:
        lat = float(request.args.get("lat", 40.8083))
        lng = float(request.args.get("lng", -73.9162))
    except ValueError:
        return jsonify({"error": "lat and lng must be numbers"}), 400
    try:
        return response_wrapper(weather_week.fetch_week(lat, lng, _WMO_LABELS))
    except Exception:
        return jsonify({"error": "Weather is unavailable right now. Try again in a few minutes."}), 503


# ============= NYC COMPLIANCE WATCH =============

@app.route('/api/compliance/summary', methods=['GET'])
def api_compliance_summary():
    """Returns red/yellow/green status per project across permits, violations, complaints."""
    from datetime import datetime, timedelta
    try:
        conn = db()
        projects = conn.execute(
            "SELECT project_code, name, bin, address FROM projects"
        ).fetchall()

        today = datetime.utcnow().date()
        d30 = today + timedelta(days=30)
        d90_ago = today - timedelta(days=90)
        d30_ago = today - timedelta(days=30)

        out = []
        for p in projects:
            code = p["project_code"]
            # Permits status
            permits_open = conn.execute(
                "SELECT COUNT(*) FROM dob_permits WHERE project_code = ? AND filing_status NOT IN ('Issued','REVOKED','SUPERCEDED','EXPIRED') OR (expiration_date IS NOT NULL AND expiration_date >= ?)",
                (code, str(today))
            ).fetchone()[0]
            permits_expiring_30 = conn.execute(
                "SELECT COUNT(*) FROM dob_permits WHERE project_code = ? AND expiration_date IS NOT NULL AND expiration_date BETWEEN ? AND ?",
                (code, str(today), str(d30))
            ).fetchone()[0]
            # Violations status
            v_active = conn.execute(
                "SELECT COUNT(*) FROM dob_violations WHERE project_code = ? AND (status IS NULL OR status NOT IN ('RESOLVED','DISPOSED','DISMISSED','CLOSED'))",
                (code,)
            ).fetchone()[0]
            v_recent = conn.execute(
                "SELECT COUNT(*) FROM dob_violations WHERE project_code = ? AND issue_date >= ?",
                (code, str(d90_ago))
            ).fetchone()[0]
            # Complaints status
            c_active = conn.execute(
                "SELECT COUNT(*) FROM dob_complaints WHERE project_code = ? AND (status IS NULL OR status NOT IN ('CLOSED','RESOLVED'))",
                (code,)
            ).fetchone()[0]
            c_recent = conn.execute(
                "SELECT COUNT(*) FROM dob_complaints WHERE project_code = ? AND date_entered >= ?",
                (code, str(d30_ago))
            ).fetchone()[0]

            # Overall light: red if any active violation OR expired permit; yellow if expiring 30d or recent complaint; green otherwise
            light = "green"
            if v_active > 0:
                light = "red"
            elif permits_expiring_30 > 0 or c_recent > 0:
                light = "yellow"

            # Last refresh
            last_run = conn.execute(
                "SELECT MAX(run_at) AS last FROM dob_pulse_runs WHERE project_code = ?", (code,)
            ).fetchone()
            last_refresh = last_run["last"] if last_run and last_run["last"] else None

            out.append({
                "project_code": code,
                "name": p["name"],
                "bin": p["bin"],
                "address": p["address"],
                "light": light,
                "permits": {"open": permits_open, "expiring_30d": permits_expiring_30},
                "violations": {"active": v_active, "issued_90d": v_recent},
                "complaints": {"active": c_active, "filed_30d": c_recent},
                "last_refresh": last_refresh,
            })
        conn.close()
        return response_wrapper(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/compliance/permits', methods=['GET'])
def api_compliance_permits():
    project_code = request.args.get("project_code")
    if not project_code:
        return jsonify({"error": "project_code required"}), 400
    try:
        conn = db()
        rows = conn.execute(
            """SELECT permit_id, work_permit, job_filing_number, permit_type, work_type,
                      filing_status, issuance_date, expiration_date, permittee_business_name
               FROM dob_permits WHERE project_code = ?
               ORDER BY filing_date DESC NULLS LAST, expiration_date DESC""",
            (project_code,)
        ).fetchall()
        conn.close()
        return response_wrapper(rows_to_dicts(rows) if rows else [])
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/compliance/violations', methods=['GET'])
def api_compliance_violations():
    project_code = request.args.get("project_code")
    if not project_code:
        return jsonify({"error": "project_code required"}), 400
    try:
        conn = db()
        rows = conn.execute(
            """SELECT violation_id, source, violation_number, violation_type, violation_category,
                      issue_date, hearing_date, status, description, penalty_imposed, penalty_paid
               FROM dob_violations WHERE project_code = ?
               ORDER BY issue_date DESC""",
            (project_code,)
        ).fetchall()
        conn.close()
        return response_wrapper(rows_to_dicts(rows) if rows else [])
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/compliance/complaints', methods=['GET'])
def api_compliance_complaints():
    project_code = request.args.get("project_code")
    if not project_code:
        return jsonify({"error": "project_code required"}), 400
    try:
        conn = db()
        rows = conn.execute(
            """SELECT complaint_id, complaint_number, complaint_category, status,
                      date_entered, disposition_date, disposition_code, inspection_date
               FROM dob_complaints WHERE project_code = ?
               ORDER BY date_entered DESC""",
            (project_code,)
        ).fetchall()
        conn.close()
        return response_wrapper(rows_to_dicts(rows) if rows else [])
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/compliance/refresh', methods=['POST'])
def api_compliance_refresh():
    """Trigger a live pull from NYC OpenData. Body: {project_code: 'FR-BX-001'} or {} for all."""
    try:
        import nyc_compliance
        body = request.get_json(silent=True) or {}
        project_code = body.get("project_code")
        if project_code:
            result = nyc_compliance.refresh_project(project_code)
            return response_wrapper({"project": project_code, "result": result})
        else:
            results = nyc_compliance.refresh_all()
            return response_wrapper({"all": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/compliance/pulse', methods=['GET'])
def api_compliance_pulse():
    """Last N pulse runs for monitoring."""
    try:
        limit = int(request.args.get("limit", 30))
        conn = db()
        rows = conn.execute(
            """SELECT run_at, project_code, dataset, bin_queried, records_returned,
                      status_code, duration_ms, error_message
               FROM dob_pulse_runs ORDER BY run_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        conn.close()
        return response_wrapper(rows_to_dicts(rows) if rows else [])
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ============= DROP PLAN (Batch B #200) =============
# Roll-up reads + append-only writes for the drop-plan system. Security
# per DROP_PLAN_SYSTEM_DESIGN.md §6 / banked #158: cost/rate/expense
# fields are OMITTED ENTIRELY (never zeroed) for non admin/c_suite. The
# four operational roles may read stages/quantities/progress and log
# quantities + stage status; external/client roles get 403 via
# requires_role. Money writes (expenses) are admin/c_suite only.
import dropplan_rollups as _rollups
import lookahead as _lookahead  # #255 — Two-Week Look-Ahead (auto-draft + editable)
import project_schedule as _project_schedule  # the Schedule window's one payload

_DROPPLAN_ROLES = ('admin', 'c_suite', 'pm', 'super')
_DROPPLAN_COST_ROLES = ('admin', 'c_suite')


def _dropplan_can_see_cost():
    return (current_user() or {}).get('role') in _DROPPLAN_COST_ROLES


def _dropplan_actor():
    """PII-safe logged_by fallback: the acting user's numeric id, never a name."""
    uid = (current_user() or {}).get('id')
    return f"uid:{uid}" if uid is not None else None


@app.route('/api/dropplan/projects/<project_code>/drops', methods=['GET'])
@requires_role(*_DROPPLAN_ROLES)
def api_dropplan_drops(project_code):
    """Project drops list: elevation, sequence, lifecycle, current stage,
    progress %, planned-vs-actual working days + variance. No dollars."""
    try:
        conn = db()
        try:
            ids = [r['drop_id'] for r in conn.execute(
                "SELECT drop_id FROM drops WHERE project_code=? ORDER BY sequence_no",
                (project_code,)).fetchall()]
            out = [_rollups.drop_summary(conn, did, include_cost=False) for did in ids]
        finally:
            conn.close()
        return response_wrapper(out, count=len(out))
    except Exception as e:
        logging.error(f"GET /api/projects/{project_code}/drops: {str(e)}")
        return _server_error(e)


@app.route('/api/dropplan/drops/<drop_id>', methods=['GET'])
@requires_role(*_DROPPLAN_ROLES)
def api_dropplan_drop_detail(drop_id):
    """Drop detail: stage status per step (incl. N/A), quantity totals per
    SOV line. cost + expense_total ONLY for admin/c_suite (keys omitted
    otherwise); cost is 'pending_rates' when contributing rates are NULL."""
    try:
        conn = db()
        try:
            detail = _rollups.drop_detail(conn, drop_id, include_cost=_dropplan_can_see_cost())
        finally:
            conn.close()
        if detail is None:
            return jsonify({"error": "drop not found"}), 404
        return response_wrapper(detail)
    except Exception as e:
        logging.error(f"GET /api/drops/{drop_id}: {str(e)}")
        return _server_error(e)


@app.route('/api/dropplan/projects/<project_code>/rollup', methods=['GET'])
@requires_role(*_DROPPLAN_ROLES)
def api_dropplan_rollup(project_code):
    """Project roll-up: overall progress + qty by SOV (always); total spend
    + expenses ONLY for admin/c_suite (omitted otherwise)."""
    try:
        conn = db()
        try:
            roll = _rollups.project_rollup(conn, project_code, include_cost=_dropplan_can_see_cost())
        finally:
            conn.close()
        return response_wrapper(roll)
    except Exception as e:
        logging.error(f"GET /api/projects/{project_code}/dropplan-rollup: {str(e)}")
        return _server_error(e)


# ============= TWO-WEEK LOOK-AHEAD (#255) — auto-draft + editable =============
# Gated as the project Drop-Plan views are. AUTO-DRAFTS planned activity bars
# from the Drop Plan; the super adjusts by dragging. Every load re-projects ONLY the
# untouched source='auto' rows (project_schedule.redraft) — manual/custom rows survive.

def _lookahead_default_start():
    """This week's Monday, LOCAL — the natural window start."""
    t = date.today()
    return (t - timedelta(days=t.weekday())).isoformat()


@app.route('/api/projects/<project_code>/schedule', methods=['GET'])
@requires_role(*_DROPPLAN_ROLES)
def api_project_schedule(project_code):
    """The Schedule window in one call: job dates + pace, the two-week board, the
    job's tasks / events / meetings in the window, and the site pin for weather."""
    start = (request.args.get('start') or '').strip() or _lookahead_default_start()
    try:
        date.fromisoformat(start)
    except ValueError:
        return jsonify({"error": "start must be YYYY-MM-DD"}), 400
    conn = db()
    try:
        if not validate_project_exists(conn, project_code):
            return jsonify({"error": "project not found"}), 404
        data = _project_schedule.payload(conn, project_code, start)
    finally:
        conn.close()
    resp = response_wrapper(data)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


@app.route('/api/projects/<project_code>/lookahead', methods=['GET'])
@requires_role(*_DROPPLAN_ROLES)
def api_lookahead_get(project_code):
    """Window load. Auto rows are re-projected from this week's Drop Plan progress
    through the window on every load (manual/custom rows kept). LOCAL dates; no-store."""
    start = (request.args.get('start') or '').strip() or _lookahead_default_start()
    try:
        date.fromisoformat(start)
    except ValueError:
        return jsonify({"error": "start must be YYYY-MM-DD"}), 400
    conn = db()
    try:
        _project_schedule.redraft(conn, project_code, start)
        conn.commit()
        data = _lookahead.load_window(conn, project_code, start)
    finally:
        conn.close()
    resp = response_wrapper(data)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


@app.route('/api/projects/<project_code>/lookahead/activities', methods=['POST'])
@requires_role(*_DROPPLAN_ROLES)
def api_lookahead_add(project_code):
    """Add a manual activity — a drop activity OR a custom no-drop/project-wide
    row. Body: drop_id (nullable), name, activity_type, planned_start,
    planned_finish, crew, notes. LOCAL dates."""
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    atype = (body.get('activity_type') or 'work').strip()
    if atype not in ('stage', 'work', 'delivery', 'inspection'):
        return jsonify({"error": "bad activity_type"}), 400
    ps = (body.get('planned_start') or '').strip()
    pf = (body.get('planned_finish') or ps).strip()
    try:
        date.fromisoformat(ps); date.fromisoformat(pf)
    except ValueError:
        return jsonify({"error": "planned_start/finish must be YYYY-MM-DD"}), 400
    if pf < ps:
        pf = ps
    if atype in ('delivery', 'inspection'):
        pf = ps  # milestones are single-day
    drop_id = (body.get('drop_id') or None) or None
    conn = db()
    try:
        nxt = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM lookahead_activity "
                          "WHERE project_code=?", (project_code,)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO lookahead_activity (project_code, drop_id, name, activity_type, "
            "planned_start, planned_finish, crew, source, notes, sort_order) "
            "VALUES (?,?,?,?,?,?,?, 'manual', ?, ?)",
            (project_code, drop_id, name, atype, ps, pf, body.get('crew'), body.get('notes'), nxt))
        conn.commit()
        return response_wrapper({"id": cur.lastrowid}), 201
    finally:
        conn.close()


@app.route('/api/lookahead/activities/<int:aid>', methods=['PATCH'])
@requires_role(*_DROPPLAN_ROLES)
def api_lookahead_patch(aid):
    """Edit/drag an activity. Dragging (planned_start/finish) or editing
    (name/crew/type) flips the row to source='manual' so a re-draft never
    clobbers it. LOCAL dates."""
    body = request.get_json(silent=True) or {}
    conn = db()
    try:
        row = conn.execute("SELECT * FROM lookahead_activity WHERE id=?", (aid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        row = dict(row)
        err = _projdoc_scope_err(conn, row['project_code'])   # the id carries no project in the path
        if err:
            return err
        sets, args = [], []
        if 'planned_start' in body or 'planned_finish' in body:
            ps = (body.get('planned_start') or row['planned_start'])
            pf = (body.get('planned_finish') or row['planned_finish'])
            try:
                date.fromisoformat(ps); date.fromisoformat(pf)
            except ValueError:
                return jsonify({"error": "dates must be YYYY-MM-DD"}), 400
            if pf < ps:
                pf = ps
            if row['activity_type'] in ('delivery', 'inspection'):
                pf = ps
            sets += ["planned_start=?", "planned_finish=?"]; args += [ps, pf]
        if 'name' in body:
            sets.append("name=?"); args.append((body.get('name') or '').strip())
        for k in ('crew', 'notes'):
            if k in body:
                sets.append(f"{k}=?"); args.append(body.get(k) or None)
        if 'activity_type' in body and body['activity_type'] in ('stage', 'work', 'delivery', 'inspection'):
            sets.append("activity_type=?"); args.append(body['activity_type'])
        if not sets:
            return jsonify({"error": "nothing to update"}), 400
        sets += ["source='manual'", "updated_at=CURRENT_TIMESTAMP"]  # any edit locks it manual
        conn.execute(f"UPDATE lookahead_activity SET {', '.join(sets)} WHERE id=?", (*args, aid))
        conn.commit()
        out = dict(conn.execute("SELECT * FROM lookahead_activity WHERE id=?", (aid,)).fetchone())
        return response_wrapper(out)
    finally:
        conn.close()


@app.route('/api/lookahead/activities/<int:aid>', methods=['DELETE'])
@requires_role(*_DROPPLAN_ROLES)
def api_lookahead_delete(aid):
    """Remove an activity row (only on a job the caller can open)."""
    conn = db()
    try:
        row = conn.execute("SELECT project_code FROM lookahead_activity WHERE id=?", (aid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        err = _projdoc_scope_err(conn, row[0])
        if err:
            return err
        conn.execute("DELETE FROM lookahead_activity WHERE id=?", (aid,))
        conn.commit()
        return response_wrapper({"deleted": aid})
    finally:
        conn.close()


def _parse_dim(body, key):
    """Parse one patch dimension. Returns (value_or_None, error_or_None).
    Blank/missing -> None (not provided). Non-numeric or <= 0 -> error."""
    raw = body.get(key)
    if raw is None or (isinstance(raw, str) and raw.strip() == ''):
        return None, None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None, f"{key} must be a number"
    if v <= 0:
        return None, f"{key} must be greater than 0"
    return v, None


@app.route('/api/dropplan/drops/<drop_id>/quantity-entries', methods=['POST'])
@requires_role(*_DROPPLAN_ROLES)
def api_dropplan_post_quantity(drop_id):
    """APPEND one quantity entry (#202 dimensioned patch model). Never
    overwrites a stored total. An entry is EITHER a dimensioned concrete
    patch (length+width+depth, each with its own ft/in unit; volume_cf is a
    GENERATED column normalizing each dim to feet) OR a simple quantity/unit.
    logged_on defaults to LOCAL today. Full precision stored; ceil-to-tenth
    is display-only (dropplan_rollups.ceil_tenth)."""
    try:
        body = request.get_json(silent=True) or {}
        sov_line = body.get('sov_line_item')
        column_repair = sov_line is None and body.get('kind') == 'column_repair'   # #352
        if sov_line is None and not column_repair:
            return jsonify({"error": "sov_line_item required"}), 400

        # Dimensions (concrete patch). All three required together if any present.
        length, e1 = _parse_dim(body, 'length')
        width, e2 = _parse_dim(body, 'width')
        depth, e3 = _parse_dim(body, 'depth')
        for e in (e1, e2, e3):
            if e:
                return jsonify({"error": e}), 400
        dims_present = [d is not None for d in (length, width, depth)]
        is_patch = any(dims_present)
        if is_patch and not all(dims_present):
            return jsonify({"error": "a concrete patch needs all of length, width, depth"}), 400
        if column_repair and not is_patch:
            return jsonify({"error": "a column repair needs length, width and depth"}), 400

        def _unit(key):
            u = (body.get(key) or 'ft')
            return u if u in ('ft', 'in') else 'ft'
        length_unit, width_unit, depth_unit = _unit('length_unit'), _unit('width_unit'), _unit('depth_unit')

        # Simple quantity (non-patch lines).
        quantity = body.get('quantity')
        if quantity is not None and (not isinstance(quantity, str) or quantity.strip() != ''):
            try:
                quantity = float(quantity) if quantity != '' else None
            except (TypeError, ValueError):
                return jsonify({"error": "quantity must be a number"}), 400
        else:
            quantity = None

        if not is_patch and quantity is None:
            return jsonify({"error": "provide a quantity, or length+width+depth for a concrete patch"}), 400

        logged_on = (body.get('logged_on') or '').strip() or date.today().isoformat()
        logged_by = body.get('logged_by') or _dropplan_actor()
        conn = db()
        try:
            drop_row = conn.execute("SELECT project_code FROM drops WHERE drop_id=?", (drop_id,)).fetchone()
            if drop_row is None:
                return jsonify({"error": "drop not found"}), 404
            if column_repair:
                import dropplan_rollups as _dpr
                sov_line = _dpr.ensure_column_repair_sov(conn, drop_row["project_code"])
            if conn.execute("SELECT 1 FROM sov_line_items WHERE sov_id=?", (sov_line,)).fetchone() is None:
                return jsonify({"error": "sov_line_item not found"}), 404
            cur = conn.execute(
                "INSERT INTO quantity_entries(drop_id, sov_line_item, step_no, quantity, unit, "
                "length, width, depth, length_unit, width_unit, depth_unit, logged_on, logged_by, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (drop_id, sov_line, body.get('step_no'), quantity, body.get('unit'),
                 length, width, depth, length_unit, width_unit, depth_unit,
                 logged_on, logged_by, body.get('note')))
            _dropplan_audit(conn, 'dropplan_quantity_add', 'quantity_entry', cur.lastrowid,
                            after={'drop_id': drop_id, 'sov_line_item': sov_line,
                                   'length': length, 'width': width, 'depth': depth,
                                   'quantity': quantity}, note='add')
            conn.commit()
            row = dict(conn.execute(
                "SELECT entry_id, drop_id, sov_line_item, step_no, quantity, unit, length, width, depth, "
                "length_unit, width_unit, depth_unit, volume_cf, logged_on, logged_by, note "
                "FROM quantity_entries WHERE entry_id=?", (cur.lastrowid,)).fetchone())
        finally:
            conn.close()
        # Display helper: ceil-to-tenth (full precision retained in volume_cf).
        try:
            from dropplan_rollups import ceil_tenth
            row['volume_cf_display'] = ceil_tenth(row.get('volume_cf'))
        except Exception:
            pass
        return response_wrapper(row), 201
    except Exception as e:
        logging.error(f"POST /api/dropplan/drops/{drop_id}/quantity-entries: {str(e)}")
        return _server_error(e)


def _dropplan_audit(conn, action, target_type, target_id, before=None, after=None, note=None):
    """Append an audit_log row for a drop-plan mutation (#203). PII-safe:
    actor is the numeric user id + role; no names. before/after are small
    JSON snapshots of the changed row."""
    u = current_user() or {}
    conn.execute(
        "INSERT INTO audit_log (action, actor_user_id, actor_role, target_type, target_id, "
        "before_json, after_json, note, created_at) VALUES (?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)",
        (action, u.get('id'), u.get('role'), target_type, str(target_id),
         json.dumps(before, default=str) if before is not None else None,
         json.dumps(after, default=str) if after is not None else None, note))


@app.route('/api/dropplan/drops/<drop_id>/quantity-entries', methods=['GET'])
@requires_role(*_DROPPLAN_ROLES)
def api_dropplan_list_quantity(drop_id):
    """List the individual quantity entries (patches) for a drop — the patch
    table the UI renders (each row editable/removable). volume_cf_display is
    ceil-to-tenth; full precision is in volume_cf."""
    try:
        conn = db()
        try:
            rows = conn.execute(
                "SELECT q.entry_id, q.sov_line_item, s.sov_code, q.step_no, q.quantity, q.unit, "
                "q.length, q.width, q.depth, q.length_unit, q.width_unit, q.depth_unit, q.volume_cf, "
                "q.logged_on, q.logged_by, q.note "
                "FROM quantity_entries q JOIN sov_line_items s ON s.sov_id=q.sov_line_item "
                "WHERE q.drop_id=? ORDER BY q.entry_id", (drop_id,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d['volume_cf_display'] = _rollups.ceil_tenth(d.get('volume_cf'))
                out.append(d)
        finally:
            conn.close()
        return response_wrapper(out, count=len(out))
    except Exception as e:
        logging.error(f"GET /api/dropplan/drops/{drop_id}/quantity-entries: {str(e)}")
        return _server_error(e)


@app.route('/api/dropplan/drops/<drop_id>', methods=['PATCH'])
@requires_role(*_DROPPLAN_ROLES)
def api_dropplan_patch_drop(drop_id):
    """Update a drop's lifecycle (the 'Activate' action #204) and/or notes.
    Activating sets lifecycle='scaffold_active' so the board shows its ring.
    Audit-logged."""
    try:
        body = request.get_json(silent=True) or {}
        fields = {}
        if 'lifecycle' in body:
            if body['lifecycle'] not in ('not_started', 'scaffold_active', 'awaiting_paint', 'closed'):
                return jsonify({"error": "bad lifecycle"}), 400
            fields['lifecycle'] = body['lifecycle']
        if 'notes' in body:
            fields['notes'] = body['notes']
        if not fields:
            return jsonify({"error": "no fields to update"}), 400
        conn = db()
        try:
            old = conn.execute("SELECT lifecycle, notes FROM drops WHERE drop_id=?", (drop_id,)).fetchone()
            if old is None:
                return jsonify({"error": "drop not found"}), 404
            old = dict(old)
            sets = ", ".join(f"{k}=?" for k in fields)  # keys from the fixed whitelist above
            conn.execute(f"UPDATE drops SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE drop_id=?",
                         (*fields.values(), drop_id))
            _dropplan_audit(conn, 'dropplan_drop_update', 'drop', drop_id, before=old, after=fields, note='drop update')
            conn.commit()
            row = dict(conn.execute("SELECT drop_id, project_code, elevation, sequence_no, lifecycle, "
                                    "window_count, structural_signoff_at, closed_at FROM drops WHERE drop_id=?",
                                    (drop_id,)).fetchone())
        finally:
            conn.close()
        return response_wrapper(row)
    except Exception as e:
        logging.error(f"PATCH /api/dropplan/drops/{drop_id}: {str(e)}")
        return _server_error(e)


@app.route('/api/dropplan/quantity-entries/<int:entry_id>', methods=['PATCH'])
@requires_role(*_DROPPLAN_ROLES)
def api_dropplan_patch_quantity(entry_id):
    """Edit a discrete patch's dimensions (length/width/depth + units) and/or
    note. Editing a discrete row is allowed — append-only protects aggregate
    totals, not individual line items. Audit-logged."""
    try:
        body = request.get_json(silent=True) or {}
        length, e1 = _parse_dim(body, 'length')
        width, e2 = _parse_dim(body, 'width')
        depth, e3 = _parse_dim(body, 'depth')
        for e in (e1, e2, e3):
            if e:
                return jsonify({"error": e}), 400
        dims = [length, width, depth]
        if any(d is not None for d in dims) and not all(d is not None for d in dims):
            return jsonify({"error": "a concrete patch needs all of length, width, depth"}), 400

        def _unit(key, cur):
            u = body.get(key)
            if u is None:
                return cur
            return u if u in ('ft', 'in') else 'ft'

        conn = db()
        try:
            old = conn.execute(
                "SELECT drop_id, length, width, depth, length_unit, width_unit, depth_unit, note "
                "FROM quantity_entries WHERE entry_id=?", (entry_id,)).fetchone()
            if old is None:
                return jsonify({"error": "quantity entry not found"}), 404
            old = dict(old)
            new_len = length if length is not None else old['length']
            new_wid = width if width is not None else old['width']
            new_dep = depth if depth is not None else old['depth']
            conn.execute(
                "UPDATE quantity_entries SET length=?, width=?, depth=?, length_unit=?, width_unit=?, "
                "depth_unit=?, note=COALESCE(?, note) WHERE entry_id=?",
                (new_len, new_wid, new_dep,
                 _unit('length_unit', old['length_unit']), _unit('width_unit', old['width_unit']),
                 _unit('depth_unit', old['depth_unit']), body.get('note'), entry_id))
            new = dict(conn.execute(
                "SELECT entry_id, drop_id, sov_line_item, length, width, depth, length_unit, width_unit, "
                "depth_unit, volume_cf, logged_on, logged_by, note FROM quantity_entries WHERE entry_id=?",
                (entry_id,)).fetchone())
            _dropplan_audit(conn, 'dropplan_quantity_edit', 'quantity_entry', entry_id,
                            before=old, after={'length': new_len, 'width': new_wid, 'depth': new_dep}, note='edit')
            conn.commit()
        finally:
            conn.close()
        new['volume_cf_display'] = _rollups.ceil_tenth(new.get('volume_cf'))
        return response_wrapper(new)
    except Exception as e:
        logging.error(f"PATCH /api/dropplan/quantity-entries/{entry_id}: {str(e)}")
        return _server_error(e)


@app.route('/api/dropplan/quantity-entries/<int:entry_id>', methods=['DELETE'])
@requires_role(*_DROPPLAN_ROLES)
def api_dropplan_delete_quantity(entry_id):
    """Remove a discrete patch row. Allowed (append-only protects aggregate
    totals, not individual line items). Audit-logged."""
    try:
        conn = db()
        try:
            old = conn.execute(
                "SELECT drop_id, sov_line_item, length, width, depth, volume_cf "
                "FROM quantity_entries WHERE entry_id=?", (entry_id,)).fetchone()
            if old is None:
                return jsonify({"error": "quantity entry not found"}), 404
            old = dict(old)
            conn.execute("DELETE FROM quantity_entries WHERE entry_id=?", (entry_id,))
            _dropplan_audit(conn, 'dropplan_quantity_delete', 'quantity_entry', entry_id,
                            before=old, note='deleted')
            conn.commit()
        finally:
            conn.close()
        return response_wrapper({"deleted": entry_id, "drop_id": old['drop_id']})
    except Exception as e:
        logging.error(f"DELETE /api/dropplan/quantity-entries/{entry_id}: {str(e)}")
        return _server_error(e)


@app.route('/api/dropplan/projects/<project_code>/expense-entries', methods=['POST'])
@requires_role('admin', 'c_suite')
def api_dropplan_post_expense(project_code):
    """APPEND one expense entry. admin/c_suite ONLY. The amount (comp data)
    is NEVER written to the server log."""
    try:
        body = request.get_json(silent=True) or {}
        category = body.get('category')
        if category not in ('material', 'labor', 'equipment', 'other'):
            return jsonify({"error": "category must be material/labor/equipment/other"}), 400
        logged_on = (body.get('logged_on') or '').strip() or date.today().isoformat()
        conn = db()
        try:
            cur = conn.execute(
                "INSERT INTO expense_entries(project_code, drop_id, category, amount, vendor, "
                "logged_on, logged_by, source, note) VALUES (?,?,?,?,?,?,?,?,?)",
                (project_code, body.get('drop_id'), category, body.get('amount'), body.get('vendor'),
                 logged_on, body.get('logged_by') or _dropplan_actor(),
                 body.get('source') or 'manual', body.get('note')))
            conn.commit()
            eid = cur.lastrowid
        finally:
            conn.close()
        logging.info(f"dropplan: expense_entry project={project_code} category={category} id={eid}")
        return response_wrapper({"entry_id": eid, "logged_on": logged_on}), 201
    except Exception as e:
        logging.error(f"POST /api/projects/{project_code}/expense-entries: {str(e)}")
        return _server_error(e)


@app.route('/api/dropplan/drops/<drop_id>/stages/<int:step_no>', methods=['PATCH'])
@requires_role(*_DROPPLAN_ROLES)
def api_dropplan_patch_stage(drop_id, step_no):
    """Set a drop's stage state — and return the RECOMPUTED per-drop % and overall %.

    The % is DERIVED on read from drop_stage_status (the single source every
    surface uses); completing a stage is the only signal that moves it. So this
    endpoint keeps status and the dates COHERENT server-side so the caller can't
    leave a date without the matching status (the #256 field bug, where a start
    date never flipped the stage to in_progress and there was no way to complete):

      (1) set a START date  -> status in_progress (from not_started)
      (2) mark COMPLETE w/ a LOCAL completion date -> status complete, % +1/total
      (3) un-complete        -> status in_progress, completion date cleared, % reverts

    Dates are LOCAL 'YYYY-MM-DD' (never UTC). Each response carries drop_pct +
    overall_pct (the one derived helper) so all three surfaces render the same value."""
    try:
        body = request.get_json(silent=True) or {}
        has_status = 'status' in body
        new_status = body.get('status')
        if has_status and new_status not in ('not_started', 'in_progress', 'complete', 'n_a'):
            return jsonify({"error": "bad status"}), 400

        def _vdate(key):
            """('provided', val|None) when key present (val validated, '' -> None);
            ('absent', None) otherwise. Raises ValueError on a non-LOCAL-date value."""
            if key not in body:
                return ('absent', None)
            v = body[key]
            if v is None or str(v).strip() == '':
                return ('provided', None)
            s = str(v).strip()
            datetime.strptime(s, '%Y-%m-%d')   # LOCAL YYYY-MM-DD; raises on bad input
            return ('provided', s)

        try:
            st_state, st_val = _vdate('started_on')
            cp_state, cp_val = _vdate('completed_on')
        except ValueError:
            return jsonify({"error": "started_on/completed_on must be LOCAL YYYY-MM-DD"}), 400

        if not (has_status or st_state == 'provided' or cp_state == 'provided'
                or 'working_days_actual' in body or 'note' in body):
            return jsonify({"error": "no fields to update"}), 400

        conn = db()
        try:
            old = conn.execute("SELECT status, started_on, completed_on, working_days_actual "
                               "FROM drop_stage_status WHERE drop_id=? AND step_no=?",
                               (drop_id, step_no)).fetchone()
            if old is None:
                return jsonify({"error": "stage row not found"}), 404

            # ---- derive a COHERENT (status, dates) triple --------------------
            final_started = st_val if st_state == 'provided' else old['started_on']
            final_completed = cp_val if cp_state == 'provided' else old['completed_on']
            if has_status:
                final_status = new_status
            elif cp_state == 'provided' and cp_val:
                final_status = 'complete'                       # a completion date completes it
            elif st_state == 'provided' and st_val and old['status'] == 'not_started':
                final_status = 'in_progress'                   # a start date starts it
            else:
                final_status = old['status']

            if final_status == 'complete':
                if not final_completed:                        # UI always sends the chosen/backdated date
                    final_completed = date.today().isoformat()  # LOCAL fallback only
            elif final_status == 'in_progress':
                final_completed = None                         # un-complete clears the completion date
            elif final_status == 'not_started':
                final_started = None
                final_completed = None                         # full reset (e.g. un-N/A)
            # 'n_a': leave the resolved dates as-is

            fields = {'status': final_status,
                      'started_on': final_started,
                      'completed_on': final_completed}
            if 'working_days_actual' in body:
                fields['working_days_actual'] = body['working_days_actual']
            if 'note' in body:
                fields['note'] = body['note']

            # Column names come from the fixed whitelist above, not user input.
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE drop_stage_status SET {sets} WHERE drop_id=? AND step_no=?",
                         (*fields.values(), drop_id, step_no))
            _dropplan_audit(conn, 'dropplan_stage_update', 'drop_stage_status',
                            f"{drop_id}#{step_no}", before=dict(old), after=fields, note='stage update')
            conn.commit()
            row = dict(conn.execute(
                "SELECT drop_id, step_no, status, started_on, completed_on, working_days_actual, note "
                "FROM drop_stage_status WHERE drop_id=? AND step_no=?", (drop_id, step_no)).fetchone())
            # the ONE derived value — per-drop % AND project overall %, recomputed live
            pair = _rollups.progress_pair(conn, drop_id)
            row["drop_pct"] = pair["drop_pct"]
            row["overall_pct"] = pair["overall_pct"]
            row["progress"] = pair["progress"]
        finally:
            conn.close()
        return response_wrapper(row)
    except Exception as e:
        logging.error(f"PATCH /api/drops/{drop_id}/stages/{step_no}: {str(e)}")
        return _server_error(e)


@app.route('/api/dropplan/drops/<drop_id>/activate', methods=['POST'])
@requires_role(*_DROPPLAN_ROLES)
def api_dropplan_activate(drop_id):
    """#225 — Activate a (not-started) drop: lifecycle -> scaffold_active and stage 1
    (the scaffold / rope set-up) becomes in_progress with the provided start date, so
    the stepper unlocks for dating. start_date is the LOCAL day the scaffold/rope line
    went up (YYYY-MM-DD; optional — re-activating without it just updates the status)."""
    try:
        body = request.get_json(silent=True) or {}
        start = (body.get('start_date') or '').strip()
        if start:
            try:
                datetime.strptime(start, '%Y-%m-%d')
            except ValueError:
                return jsonify({"error": "start_date must be YYYY-MM-DD"}), 400
        conn = db()
        try:
            d = conn.execute("SELECT drop_id, lifecycle FROM drops WHERE drop_id=?", (drop_id,)).fetchone()
            if d is None:
                return jsonify({"error": "drop not found"}), 404
            conn.execute("UPDATE drops SET lifecycle='scaffold_active', updated_at=CURRENT_TIMESTAMP "
                         "WHERE drop_id=?", (drop_id,))
            if start:
                conn.execute("UPDATE drop_stage_status SET status='in_progress', started_on=? "
                             "WHERE drop_id=? AND step_no=1", (start, drop_id))
            else:
                conn.execute("UPDATE drop_stage_status SET status='in_progress' "
                             "WHERE drop_id=? AND step_no=1", (drop_id,))
            _dropplan_audit(conn, 'dropplan_activate', 'drops', drop_id,
                            before={'lifecycle': d['lifecycle']},
                            after={'lifecycle': 'scaffold_active', 'stage1_start': start or None},
                            note='activate drop')
            conn.commit()
        finally:
            conn.close()
        return response_wrapper({"drop_id": drop_id, "lifecycle": "scaffold_active",
                                 "stage1_started_on": start or None})
    except Exception as e:
        logging.error(f"POST /api/dropplan/drops/{drop_id}/activate: {str(e)}")
        return _server_error(e)


@app.route('/api/dropplan/paint-phases/<int:phase_id>', methods=['PATCH'])
@requires_role(*_DROPPLAN_ROLES)
def api_dropplan_patch_paint(phase_id):
    """Update an elevation paint phase status / dates."""
    try:
        body = request.get_json(silent=True) or {}
        fields = {}
        if 'status' in body:
            if body['status'] not in ('not_ready', 'ready', 'in_progress', 'complete'):
                return jsonify({"error": "bad status"}), 400
            fields['status'] = body['status']
        for k in ('started_on', 'completed_on'):
            if k in body:
                fields[k] = body[k]
        if not fields:
            return jsonify({"error": "no fields to update"}), 400
        conn = db()
        try:
            if conn.execute("SELECT 1 FROM paint_phases WHERE phase_id=?", (phase_id,)).fetchone() is None:
                return jsonify({"error": "paint phase not found"}), 404
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE paint_phases SET {sets} WHERE phase_id=?", (*fields.values(), phase_id))
            conn.commit()
            row = dict(conn.execute(
                "SELECT phase_id, project_code, elevation, status, started_on, completed_on "
                "FROM paint_phases WHERE phase_id=?", (phase_id,)).fetchone())
        finally:
            conn.close()
        return response_wrapper(row)
    except Exception as e:
        logging.error(f"PATCH /api/paint-phases/{phase_id}: {str(e)}")
        return _server_error(e)


@app.route('/api/dropplan/projects/<project_code>/sov-lines', methods=['GET'])
@requires_role(*_DROPPLAN_ROLES)
def api_dropplan_sov_lines(project_code):
    """SOV line items for the project's quantity-entry dropdown. Returns
    code/description/unit only — NO unit_rate (no dollars in the picker)."""
    try:
        conn = db()
        try:
            rows = conn.execute(
                "SELECT sov_id, sov_code, description, unit FROM sov_line_items "
                "WHERE project_code=? ORDER BY sov_code", (project_code,)).fetchall()
            out = rows_to_dicts(rows)
        finally:
            conn.close()
        return response_wrapper(out, count=len(out))
    except Exception as e:
        logging.error(f"GET /api/dropplan/projects/{project_code}/sov-lines: {str(e)}")
        return _server_error(e)


@app.route('/api/projects/<project_code>/sov', methods=['GET'])
@requires_section('financial')
def api_project_sov(project_code):
    """#262 — Schedule of Values (FINANCIAL section). Server-enforced to admin/c_suite
    via the central access.SECTION_ACCESS map (pm/super -> 403, even on a direct call).
    Returns the project's SOV line items WITH unit_rate (the billing breakdown — that's
    why it's gated; the drop-plan picker /sov-lines omits dollars and stays pm-visible)."""
    conn = db()
    try:
        rows = conn.execute(
            "SELECT sov_code, description, unit, unit_rate FROM sov_line_items "
            "WHERE project_code=? ORDER BY sov_code", (project_code,)).fetchall()
        out = [{"sov_code": r["sov_code"], "description": r["description"],
                "unit": r["unit"], "unit_rate": r["unit_rate"]} for r in rows]
        return response_wrapper(out, count=len(out))
    finally:
        conn.close()


@app.route('/dropplan', methods=['GET'])
@requires_role(*_DROPPLAN_ROLES)
def dropplan_page():
    """Retired standalone page (batch 2C). Drop Plan lives inside each project now, so an old
    bookmark goes to that project (/dropplan?project=CODE) or, with no code, to the project list.
    The /api/dropplan/* endpoints are unchanged: the in-project tab uses them."""
    code = (request.args.get('project') or '').strip()
    if re.fullmatch(r'[A-Za-z0-9._-]{1,40}', code):
        return redirect(f'/projects/{code}')
    return redirect('/company#projects')


# ===========================================================================
# Field Photos — Phase 1 (#235): per-project work-in-progress photo gallery +
# Unassigned sort/assign tray + bulk upload. file_path/thumb_path are on-disk
# ONLY (NEVER in JSON); the gated /api/field-photos/<id>/(thumb|file) routes serve the
# bytes. GPS/EXIF is stripped on processing; dates are LOCAL. All four project
# roles can view + upload.
# ===========================================================================
# #294 — the self-service field-photo editor set lives in photo_reassign (one
# source; the reassignment endpoints gate on the SAME roles, so they can't drift).
_FP_ROLES = photo_reassign.FP_EDIT_ROLES
_FP_BASE = ssc_paths.under_root('data_room', 'field_photos')   # #287
_FP_EXT = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp'}
_FP_MAX_FILES = 60            # per single POST (the UI chunks bigger batches)
_FP_CLUSTER_GAP_MIN = 90      # a > 90-min gap (or a date change) starts a new time cluster


@app.errorhandler(413)
def _fp_request_too_large(e):
    return jsonify({"error": "That upload is too large for one request — the app uploads photos in "
                             "smaller groups; try again or select fewer at once.", "too_large": True}), 413


def _fp_drop_labels(conn, project_code):
    out = {}
    for r in conn.execute("SELECT drop_id, sequence_no, elevation FROM drops WHERE project_code=?", (project_code,)):
        out[r["drop_id"]] = f"DP-{r['sequence_no']} · {r['elevation'] or '—'}"
    return out


def _fp_drops_list(conn, project_code):
    rows = conn.execute("SELECT drop_id, sequence_no, elevation FROM drops WHERE project_code=? ORDER BY sequence_no",
                        (project_code,)).fetchall()
    return [{"drop_id": r["drop_id"], "label": f"DP-{r['sequence_no']} · {r['elevation'] or '—'}",
             "sequence_no": r["sequence_no"], "elevation": r["elevation"]} for r in rows]


def _fieldphoto_public(r, label_map):
    """PII/path-safe shape — NO file_path / thumb_path. URLs are the gated routes."""
    return {
        "id": r["id"], "project_code": r["project_code"],
        "drop_id": r["drop_id"], "drop_label": label_map.get(r["drop_id"]) if r["drop_id"] else None,
        "worker_id": r["worker_id"], "stage": r["stage"], "caption": r["caption"],
        "taken_at": r["taken_at"], "taken_at_estimated": bool(r["taken_at_estimated"]),
        "uploaded_at": r["uploaded_at"], "file_name": r["file_name"], "file_size": r["file_size"],
        "mime": r["mime"], "width": r["width"], "height": r["height"],
        "orientation_applied": bool(r["orientation_applied"]),
        "kind": "video" if str(r["mime"] or "").startswith("video/") else "photo",   # worker videos (field_videos.py)
        "duration_s": _row_get(r, "duration_s"),
        "thumb_url": f"/api/field-photos/{r['id']}/thumb", "file_url": f"/api/field-photos/{r['id']}/file",
    }


def _row_get(r, key):
    """A column that older databases may not have yet (e.g. field_photos.duration_s)."""
    try:
        return r[key]
    except (KeyError, IndexError):
        return None


def _fp_write(project_code, res):
    """Write the display + thumb bytes under data_room/field_photos/<project>/<uuid>/.
    Returns (file_path, thumb_path). Raises on a bad path."""
    base = _FP_BASE.resolve()
    pdir = _FP_BASE / project_code / uuid.uuid4().hex
    if not pdir.resolve().is_relative_to(base):
        raise ValueError("invalid project path")
    pdir.mkdir(parents=True, exist_ok=True)
    fpath = pdir / ("full" + res["ext"])
    tpath = pdir / ("thumb" + res["ext"])
    fpath.write_bytes(res["display_bytes"])
    tpath.write_bytes(res["thumb_bytes"])
    # #287 — store PORTABLE relative paths; reads go through resolve_data_path
    return ssc_paths.store_rel(fpath), ssc_paths.store_rel(tpath)


def _fp_write_video(project_code, data, ext, thumb_bytes):
    """A worker video and its JPEG still under data_room/field_photos/<project>/<uuid>/ (the same
    place as photos). Returns (file_path, thumb_path) as portable relative paths."""
    base = _FP_BASE.resolve()
    pdir = _FP_BASE / project_code / uuid.uuid4().hex
    if not pdir.resolve().is_relative_to(base):
        raise ValueError("invalid project path")
    pdir.mkdir(parents=True, exist_ok=True)
    fpath, tpath = pdir / ("video" + ext), pdir / "thumb.jpg"
    fpath.write_bytes(data)
    tpath.write_bytes(thumb_bytes)
    return ssc_paths.store_rel(fpath), ssc_paths.store_rel(tpath)


def _fp_parse_batch_date(s):
    """MM/DD/YYYY or YYYY-MM-DD -> 'YYYY-MM-DD 12:00:00' (LOCAL noon) for the
    no-EXIF fallback, or None."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d 12:00:00")
        except ValueError:
            continue
    return None


def _fp_dt(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(str(s)[:19], fmt)
        except (ValueError, TypeError):
            continue
    return None


def _fp_cluster_label(first_dt, last_dt):
    if not first_dt:
        return "Undated"
    day = f"{first_dt:%a} {first_dt:%b} {first_dt.day}"
    def t(d):
        h = d.hour % 12 or 12
        return f"{h}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"
    if last_dt and (last_dt - first_dt).total_seconds() > 60:
        return f"{day} · {t(first_dt)}–{t(last_dt)}"
    return f"{day} · {t(first_dt)}"


def _fp_time_clusters(rows, label_map, tcol):
    """Group consecutive photos (already ordered ASC by tcol) into clusters,
    starting a new cluster on a > _FP_CLUSTER_GAP_MIN gap or a date change."""
    clusters, cur = [], []

    def flush():
        if not cur:
            return
        dts = [_fp_dt(p[tcol]) for p in cur]
        dts = [d for d in dts if d]
        clusters.append({
            "label": _fp_cluster_label(dts[0] if dts else None, dts[-1] if dts else None),
            "count": len(cur), "likely_one_drop": len(cur) > 1,
            "photos": [_fieldphoto_public(p, label_map) for p in cur],
        })

    prev = None
    for r in rows:
        dt = _fp_dt(r[tcol])
        if cur and (dt is None or prev is None or
                    (dt - prev).total_seconds() > _FP_CLUSTER_GAP_MIN * 60 or dt.date() != prev.date()):
            flush()
            cur = []
        cur.append(r)
        prev = dt
    flush()
    return clusters


def _fp_stats(conn, project_code):
    g = lambda q, p=(): conn.execute(q, (project_code,) + p).fetchone()[0]
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    latest = g("SELECT MAX(taken_at) FROM field_photos WHERE project_code=?")
    return {
        "total": g("SELECT COUNT(*) FROM field_photos WHERE project_code=?"),
        "this_week": g("SELECT COUNT(*) FROM field_photos WHERE project_code=? AND substr(taken_at,1,10) >= ?", (week_ago,)),
        "drops_covered": g("SELECT COUNT(DISTINCT drop_id) FROM field_photos WHERE project_code=? AND drop_id IS NOT NULL"),
        "total_drops": g("SELECT COUNT(*) FROM drops WHERE project_code=?"),
        "unassigned": g("SELECT COUNT(*) FROM field_photos WHERE project_code=? AND drop_id IS NULL"),
        "latest": (str(latest)[:10] if latest else None),
    }


def _fp_no_store(resp):
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


@app.route('/api/projects/<project_code>/photos/upload', methods=['POST'])
@requires_role(*_FP_ROLES)
def api_field_photos_upload(project_code):
    """Upload 1..N images (multipart). Optional batch tags drop_id/date/worker/
    stage. Each image is processed independently (downscale + thumb + EXIF date +
    orientation baked in + GPS stripped); a file that can't be decoded is SKIPPED
    with a reason and the rest still succeed. Lands Unassigned unless a drop was
    given. Returns a per-file result list. No *_path."""
    import field_photos as fp
    files = (request.files.getlist('photos') or request.files.getlist('files')
             or request.files.getlist('photo'))
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify({"error": "no files in request"}), 400
    if len(files) > _FP_MAX_FILES:
        return jsonify({"error": f"Too many photos in one request — send up to {_FP_MAX_FILES} at a time "
                                 f"(the app uploads in groups).", "max_files": _FP_MAX_FILES}), 400
    drop_id = (request.form.get('drop_id') or '').strip() or None
    worker_id = (request.form.get('worker_id') or request.form.get('worker') or '').strip() or None
    stage = (request.form.get('stage') or '').strip() or None
    # #238 — the batch "Description" maps to the caption column (one per batch);
    # worker_id/stage are retained in the schema but no longer sent by the UI.
    caption = (request.form.get('caption') or request.form.get('description') or '').strip() or None
    fallback_dt = _fp_parse_batch_date(request.form.get('date')) or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = db()
    try:
        if not validate_project_exists(conn, project_code):
            return jsonify({"error": "project not found"}), 404
        if drop_id and not conn.execute("SELECT 1 FROM drops WHERE drop_id=? AND project_code=?",
                                        (drop_id, project_code)).fetchone():
            return jsonify({"error": "drop not found for this project"}), 400
        uid = (current_user() or {}).get('id')
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stored, skipped = [], []
        for fs in files:
            name = Path(fs.filename or 'photo').name
            try:
                data = fs.read()
                res = fp.process_image(data, fs.filename, fallback_dt_iso=fallback_dt)
            except fp.SkipImage as se:
                skipped.append({"file": name, "reason": str(se)})
                continue
            except Exception as ex:
                logging.warning(f"field photo process failed ({name}): {type(ex).__name__}: {ex}")
                skipped.append({"file": name, "reason": f"could not process ({type(ex).__name__})"})
                continue
            try:
                file_path, thumb_path = _fp_write(project_code, res)
            except Exception as ex:
                logging.error(f"field photo store failed ({name}): {ex}")
                skipped.append({"file": name, "reason": "could not store the image"})
                continue
            cur = conn.execute(
                "INSERT INTO field_photos (project_code, drop_id, worker_id, stage, caption, taken_at, taken_at_estimated, "
                "uploaded_at, uploaded_by_uid, file_path, thumb_path, file_name, file_size, mime, width, height, "
                "orientation_applied) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_code, drop_id, worker_id, stage, caption, res["taken_at"], 1 if res["taken_at_estimated"] else 0,
                 now, uid, file_path, thumb_path, res["file_name"], len(res["display_bytes"]), res["mime"],
                 res["width"], res["height"], 1 if res["orientation_applied"] else 0))
            stored.append({"id": cur.lastrowid, "file": res["file_name"], "taken_at": res["taken_at"],
                           "estimated": res["taken_at_estimated"], "orientation_applied": res["orientation_applied"]})
        conn.commit()
        return _fp_no_store(response_wrapper({
            "stored": stored, "skipped": skipped,
            "stored_count": len(stored), "skipped_count": len(skipped),
            "drop_id": drop_id, "landed": "drop" if drop_id else "unassigned",
        })), 201
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logging.error(f"POST photos/upload: {type(e).__name__}: {e}")
        return jsonify({"error": "Upload failed — try again or with fewer photos."}), 500
    finally:
        conn.close()


@app.route('/api/projects/<project_code>/photos', methods=['GET'])
@requires_role(*_FP_ROLES)
def api_field_photos_list(project_code):
    """Gallery — PAGINATED. ?group=drop|time|all, ?drop_id, ?worker_id, ?date
    (YYYY-MM-DD), ?limit (<=200), ?offset. Returns the page + total/has_more +
    stats tiles + this project's drops (for the filter/assign list). No *_path."""
    group = (request.args.get('group') or 'drop').lower()
    drop_f = (request.args.get('drop_id') or '').strip() or None
    worker_f = (request.args.get('worker_id') or '').strip() or None
    date_f = (request.args.get('date') or '').strip() or None
    try:
        limit = max(1, min(int(request.args.get('limit', 60)), 200))
        offset = max(0, int(request.args.get('offset', 0)))
    except (ValueError, TypeError):
        limit, offset = 60, 0
    conn = db()
    try:
        if not validate_project_exists(conn, project_code):
            return jsonify({"error": "project not found"}), 404
        where, params = ["fp.project_code = ?"], [project_code]
        if drop_f == '__unassigned__':
            where.append("fp.drop_id IS NULL")
        elif drop_f:
            where.append("fp.drop_id = ?")
            params.append(drop_f)
        if worker_f:
            where.append("fp.worker_id = ?")
            params.append(worker_f)
        if date_f:
            where.append("substr(fp.taken_at,1,10) = ?")
            params.append(date_f)
        wsql = " AND ".join(where)
        if group == 'drop':
            order = "ORDER BY (d.sequence_no IS NULL), d.sequence_no, fp.taken_at DESC, fp.id DESC"
        else:
            order = "ORDER BY fp.taken_at DESC, fp.id DESC"
        total = conn.execute(f"SELECT COUNT(*) FROM field_photos fp WHERE {wsql}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT fp.*, d.sequence_no FROM field_photos fp LEFT JOIN drops d ON d.drop_id = fp.drop_id "
            f"WHERE {wsql} {order} LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        label_map = _fp_drop_labels(conn, project_code)
        # #264 — annotate each photo with its client-visibility state (one batched pair of
        # queries) so the Field Photos UI can render the Share-with-client + Red-flag toggles.
        vis = visibility.photo_states(conn, project_code)
        # #294 — one batched query for the page's amendment markers (the
        # visibility.photo_states shape): corrections only, so tray
        # assignments never read as mistakes.
        reassigns = photo_reassign.reassign_states(conn, [r["id"] for r in rows])
        photos = []
        for r in rows:
            d = _fieldphoto_public(r, label_map)
            st = vis.get(r["id"], {"shared_client": False, "flagged": False})
            d["shared_client"] = st["shared_client"]
            d["flagged"] = st["flagged"]
            d["reassign_count"] = reassigns.get(r["id"], 0)
            d["reassigned"] = r["id"] in reassigns
            photos.append(d)
        out = {
            "photos": photos,
            "total": total, "limit": limit, "offset": offset,
            "has_more": (offset + len(rows)) < total, "group": group,
            "stats": _fp_stats(conn, project_code), "drops": _fp_drops_list(conn, project_code),
        }
        return _fp_no_store(response_wrapper(out))
    finally:
        conn.close()


@app.route('/api/projects/<project_code>/photos/unassigned', methods=['GET'])
@requires_role(*_FP_ROLES)
def api_field_photos_unassigned(project_code):
    """The sort tray — Unassigned photos grouped by taken-at (or upload) time
    clusters. ?by=time|upload. No *_path."""
    tcol = "uploaded_at" if (request.args.get('by') or 'time').lower() == 'upload' else "taken_at"
    conn = db()
    try:
        if not validate_project_exists(conn, project_code):
            return jsonify({"error": "project not found"}), 404
        rows = conn.execute(
            f"SELECT * FROM field_photos WHERE project_code=? AND drop_id IS NULL "
            f"ORDER BY {tcol} ASC, id ASC LIMIT 2000", (project_code,)).fetchall()
        label_map = _fp_drop_labels(conn, project_code)
        out = {"clusters": _fp_time_clusters(rows, label_map, tcol), "count": len(rows),
               "by": tcol, "drops": _fp_drops_list(conn, project_code)}
        return _fp_no_store(response_wrapper(out))
    finally:
        conn.close()


@app.route('/api/field-photos/assign', methods=['POST'])
@requires_role(*_FP_ROLES)
def api_field_photos_assign():
    """BULK assign — { photo_ids[], drop_id (null=back to Unassigned), stage?,
    worker? }. ONE UPDATE for the whole selection. The photos must all be in one
    project and the drop must belong to it."""
    body = request.get_json(silent=True) or {}
    ids = []
    for i in (body.get('photo_ids') or []):
        try:
            ids.append(int(i))
        except (ValueError, TypeError):
            pass
    if not ids:
        return jsonify({"error": "no photo_ids"}), 400
    if len(ids) > 5000:
        return jsonify({"error": "too many photos in one assign"}), 400
    drop_id = (body.get('drop_id') or '').strip() or None
    stage = body.get('stage')
    stage = (str(stage).strip() or None) if stage is not None else None
    worker = body.get('worker_id') if 'worker_id' in body else body.get('worker')
    worker = (str(worker).strip() or None) if worker is not None else None
    qm = ",".join("?" * len(ids))
    conn = db()
    try:
        projs = conn.execute(f"SELECT DISTINCT project_code FROM field_photos WHERE id IN ({qm})", ids).fetchall()
        if not projs:
            return jsonify({"error": "no such photos"}), 404
        if len(projs) > 1:
            return jsonify({"error": "selection spans multiple projects"}), 400
        pcode = projs[0]["project_code"]
        if drop_id and not conn.execute("SELECT 1 FROM drops WHERE drop_id=? AND project_code=?",
                                        (drop_id, pcode)).fetchone():
            return jsonify({"error": "drop not found for this project"}), 400
        # #294 — AMEND, NEVER ERASE. Before the move, the history rows ride the
        # SAME transaction: from->to per photo, timing-since-upload, whole-batch
        # detection, and the issued-DCR photo_amended flag + audit where a
        # correction touches an already-issued day. The tray flow (NULL->drop)
        # is logged too but never counts as a "correction" in the analytics.
        pre_rows = conn.execute(
            f"SELECT id, project_code, drop_id, uploaded_at, uploaded_by_uid, taken_at "
            f"FROM field_photos WHERE id IN ({qm})", ids).fetchall()
        reassign_summary = photo_reassign.record_reassignments(
            conn, (current_user() or {}).get('id'), pre_rows, drop_id,
            reason=(str(body.get('reason') or '').strip() or None))
        sets, sp = ["drop_id = ?"], [drop_id]
        if stage is not None:
            sets.append("stage = ?")
            sp.append(stage)
        if worker is not None:
            sets.append("worker_id = ?")
            sp.append(worker)
        n = conn.execute(f"UPDATE field_photos SET {', '.join(sets)} WHERE id IN ({qm})", sp + ids).rowcount
        conn.commit()
        return _fp_no_store(response_wrapper({
            "assigned": n, "drop_id": drop_id, "project_code": pcode,
            "changed": reassign_summary["changed"],
            "corrections": reassign_summary["corrections"],
            "whole_batch": reassign_summary["whole_batch_batches"] > 0,
            "dcr_amended": reassign_summary["dcr_amended"],
        }))
    finally:
        conn.close()


def _fp_serve(photo_id, col):
    conn = db()
    try:
        r = conn.execute(f"SELECT {col} AS p, mime FROM field_photos WHERE id=?", (photo_id,)).fetchone()
    finally:
        conn.close()
    if not r or not r["p"]:
        return jsonify({"error": "not found"}), 404
    # #290 — stored rows go through THE resolver (this route predated #287 and
    # was missed): relative rows anchor under the active root, and pre-#287
    # absolute WINDOWS rows re-anchor on a Linux host. The containment check
    # below is unchanged and still decides what may serve.
    p = ssc_paths.resolve_data_path(r["p"])
    try:
        if not p.resolve().is_relative_to(_FP_BASE.resolve()) or not p.exists():
            return jsonify({"error": "not found"}), 404
    except (OSError, ValueError):
        return jsonify({"error": "not found"}), 404
    mime = r["mime"] or "image/jpeg"
    if col == "thumb_path" and mime.startswith("video/"):
        mime = "image/jpeg"   # a video's thumbnail is the JPEG still the worker app grabbed
    resp = send_file(str(p), mimetype=mime, conditional=True)
    # #291 — a photo id's bytes never change (replacement = new id), so the
    # BROWSER may cache forever. `private` is load-bearing: these are
    # auth-gated field photos and Cloudflare's shared edge cache must never
    # hold them (CF does not cache `private` responses; guard-asserted, and
    # cf-cache-status stays DYNAMIC in production).
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


@app.route('/api/field-photos/<int:photo_id>/thumb', methods=['GET'])
@requires_role(*_FP_ROLES)
def api_field_photo_thumb(photo_id):
    return _fp_serve(photo_id, "thumb_path")


@app.route('/api/field-photos/<int:photo_id>/file', methods=['GET'])
@requires_role(*_FP_ROLES)
def api_field_photo_file(photo_id):
    return _fp_serve(photo_id, "file_path")


@app.route('/api/field-photos/<int:photo_id>', methods=['PATCH'])
@requires_role(*_FP_ROLES)
def api_field_photo_patch(photo_id):
    body = request.get_json(silent=True) or {}
    conn = db()
    try:
        row = conn.execute("SELECT project_code FROM field_photos WHERE id=?", (photo_id,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        sets, params = [], []
        for k in ('worker_id', 'stage', 'caption', 'taken_at'):
            if k in body:
                v = body[k]
                sets.append(f"{k} = ?")
                params.append((str(v).strip() or None) if v is not None else None)
        if 'drop_id' in body:
            d = (body.get('drop_id') or '').strip() or None
            if d and not conn.execute("SELECT 1 FROM drops WHERE drop_id=? AND project_code=?",
                                      (d, row["project_code"])).fetchone():
                return jsonify({"error": "drop not found for this project"}), 400
            sets.append("drop_id = ?")
            params.append(d)
        if 'taken_at_estimated' in body:
            sets.append("taken_at_estimated = ?")
            params.append(1 if body.get('taken_at_estimated') else 0)
        if not sets:
            return jsonify({"error": "no fields to update"}), 400
        params.append(photo_id)
        conn.execute(f"UPDATE field_photos SET {', '.join(sets)} WHERE id=?", params)
        conn.commit()
        full = dict(conn.execute("SELECT * FROM field_photos WHERE id=?", (photo_id,)).fetchone())
        return _fp_no_store(response_wrapper(_fieldphoto_public(full, _fp_drop_labels(conn, full["project_code"]))))
    finally:
        conn.close()


@app.route('/api/field-photos/<int:photo_id>', methods=['DELETE'])
@requires_role(*_FP_ROLES)
def api_field_photo_delete(photo_id):
    conn = db()
    try:
        r = conn.execute("SELECT file_path, thumb_path FROM field_photos WHERE id=?", (photo_id,)).fetchone()
        if not r:
            return jsonify({"error": "not found"}), 404
        paths = (r["file_path"], r["thumb_path"])
        conn.execute("DELETE FROM field_photos WHERE id=?", (photo_id,))
        conn.commit()
    finally:
        conn.close()
    base = _FP_BASE.resolve()
    parent = None
    for pth in paths:
        try:
            p = Path(pth)
            if p.resolve().is_relative_to(base):
                parent = p.parent
                p.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        if parent and parent.resolve().is_relative_to(base) and not any(parent.iterdir()):
            parent.rmdir()
    except Exception:
        pass
    return response_wrapper({"deleted": photo_id})


# ============= #264 — PHOTO VISIBILITY: share-with-client + red-flag (internal) =============
# Internal control surface for the visibility engine. Gated to _FP_ROLES (admin/c_suite/pm/
# super) AND per-resource: the actor must be able to access THIS photo's project (resolved
# from the row — a pm can only share photos on a project they're assigned to). The CLIENT
# never reaches these (not in _FP_ROLES + the client default-deny gate). Both audited.

def _fp_actor_can_admin_photo(conn, photo_id):
    """Resolve the photo's project + confirm the current user may act on it. Returns the
    project_code on success, or (None, error_response) on failure — by-ID per-resource check."""
    row = conn.execute("SELECT project_code FROM field_photos WHERE id=?", (photo_id,)).fetchone()
    if not row:
        return None, (jsonify({"error": "not found"}), 404)
    user = current_user() or {}
    if not pm_scoping.pm_can_access_project(user.get("role"), user.get("id"), row["project_code"], conn):
        return None, (jsonify({"error": "forbidden"}), 403)
    return row["project_code"], None


@app.route('/api/field-photos/<int:photo_id>/share', methods=['POST'])
@requires_role(*_FP_ROLES)
def api_field_photo_share(photo_id):
    """Toggle an external audience on a photo. Body {audience:'client', on:bool}. v1 = client."""
    body = request.get_json(silent=True) or {}
    audience = (body.get('audience') or 'client').strip()
    on = bool(body.get('on'))
    if audience != 'client':
        return jsonify({"error": "unsupported audience (v1: client only)"}), 400
    conn = db()
    try:
        _code, err = _fp_actor_can_admin_photo(conn, photo_id)
        if err:
            return err
        row = conn.execute("SELECT mime FROM field_photos WHERE id = ?", (photo_id,)).fetchone()
        if on and row and str(row["mime"] or "").startswith("video/"):
            return jsonify({"error": "videos can't be shared with clients yet"}), 409
        uid = (current_user() or {}).get("id")
        res = (visibility.share(conn, 'photo', photo_id, 'client', uid) if on
               else visibility.unshare(conn, 'photo', photo_id, 'client', uid))
        if not res.get("ok"):
            return jsonify({"error": res.get("reason", "cannot share")}), 409
        conn.commit()
        return _fp_no_store(response_wrapper(visibility.state(conn, 'photo', photo_id)))
    finally:
        conn.close()


@app.route('/api/field-photos/<int:photo_id>/redflag', methods=['POST'])
@requires_role(*_FP_ROLES)
def api_field_photo_redflag(photo_id):
    """Red-flag / take offline (on:true) — instantly revoke ALL external visibility + block
    re-share; or clear it (on:false). Body {on:bool}."""
    on = bool((request.get_json(silent=True) or {}).get('on'))
    conn = db()
    try:
        _code, err = _fp_actor_can_admin_photo(conn, photo_id)
        if err:
            return err
        uid = (current_user() or {}).get("id")
        (visibility.redflag if on else visibility.unflag)(conn, 'photo', photo_id, uid)
        conn.commit()
        return _fp_no_store(response_wrapper(visibility.state(conn, 'photo', photo_id)))
    finally:
        conn.close()


# ============= #269 — DOCUMENT VISIBILITY: share-with-client + red-flag (internal) =====
# Documents join the #264 engine (item_type='document') exactly like photos: default-deny,
# per-audience share, sticky red-flag, audited. Gated to _PROJDOC_ROLES AND per-resource
# (the actor must be able to access THIS document's project, re-derived from the row).
# The CLIENT never reaches these (not in _PROJDOC_ROLES + the client containment gate).

def _doc_actor_can_admin(conn, doc_id):
    """Resolve the document's project + confirm the current user may act on it. Returns
    (project_code, None) on success or (None, error_response) — by-ID per-resource check."""
    row = conn.execute("SELECT project_code FROM project_documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return None, (jsonify({"error": "not found"}), 404)
    user = current_user() or {}
    if not pm_scoping.pm_can_access_project(user.get("role"), user.get("id"), row["project_code"], conn):
        return None, (jsonify({"error": "forbidden"}), 403)
    return row["project_code"], None


@app.route('/api/documents/<int:doc_id>/share', methods=['POST'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_share(doc_id):
    """Toggle an external audience on a document. Body {audience:'client', on:bool}. v1 = client."""
    body = request.get_json(silent=True) or {}
    audience = (body.get('audience') or 'client').strip()
    on = bool(body.get('on'))
    if audience != 'client':
        return jsonify({"error": "unsupported audience (v1: client only)"}), 400
    conn = db()
    try:
        _code, err = _doc_actor_can_admin(conn, doc_id)
        if err:
            return err
        uid = (current_user() or {}).get("id")
        res = (visibility.share(conn, 'document', doc_id, 'client', uid) if on
               else visibility.unshare(conn, 'document', doc_id, 'client', uid))
        if not res.get("ok"):
            return jsonify({"error": res.get("reason", "cannot share")}), 409
        conn.commit()
        resp = response_wrapper(visibility.state(conn, 'document', doc_id))
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    finally:
        conn.close()


@app.route('/api/documents/<int:doc_id>/redflag', methods=['POST'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_redflag(doc_id):
    """Red-flag / take a document offline (on:true) — instantly revoke ALL external
    visibility + block re-share; or clear it (on:false). Body {on:bool}."""
    on = bool((request.get_json(silent=True) or {}).get('on'))
    conn = db()
    try:
        _code, err = _doc_actor_can_admin(conn, doc_id)
        if err:
            return err
        uid = (current_user() or {}).get("id")
        (visibility.redflag if on else visibility.unflag)(conn, 'document', doc_id, uid)
        conn.commit()
        resp = response_wrapper(visibility.state(conn, 'document', doc_id))
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    finally:
        conn.close()


@app.route('/api/documents/share-bulk', methods=['POST'])
@requires_role(*_PROJDOC_ROLES)
def api_projdoc_share_bulk():
    """#270 — bulk share/unshare documents with the client audience in ONE atomic call.
    Body {ids:[int], audience:'client', on:bool}. SKIP-NONE semantics: every id must
    exist (else 404) and the actor must pass the per-resource pm_can_access_project
    check for EVERY id's project (else 403) — any failure rejects the WHOLE call before
    a single write; there is no partial silent success. Sharing (on) with any red-flagged
    id in the selection -> 409 whole-call (the engine refuses shares on flagged items —
    clear the flag deliberately first). Each share/unshare is audited individually by the
    #264 engine. This is a ONE-TIME action — no standing auto-share rules exist; new
    uploads stay internal-only by default (deliberate)."""
    body = request.get_json(silent=True) or {}
    audience = (body.get('audience') or 'client').strip()
    on = bool(body.get('on'))
    raw_ids = body.get('ids')
    if audience != 'client':
        return jsonify({"error": "unsupported audience (v1: client only)"}), 400
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"error": "ids[] is required"}), 400
    try:
        ids = sorted({int(i) for i in raw_ids})
    except (TypeError, ValueError):
        return jsonify({"error": "ids[] must be integers"}), 400
    if len(ids) > 200:
        return jsonify({"error": "too many ids (max 200 per call)"}), 400
    qmarks = ','.join(['?'] * len(ids))
    conn = db()
    try:
        rows = conn.execute(
            f"SELECT id, project_code FROM project_documents WHERE id IN ({qmarks})",
            ids).fetchall()
        if len(rows) != len(ids):
            return jsonify({"error": "one or more documents not found"}), 404
        user = current_user() or {}
        for code in sorted({r['project_code'] for r in rows}):
            if not pm_scoping.pm_can_access_project(user.get('role'), user.get('id'), code, conn):
                logging.info(f"share-bulk: per-resource deny actor={user.get('id')} project={code}")
                return jsonify({"error": "forbidden"}), 403
        if on:
            flagged = [r[0] for r in conn.execute(
                f"SELECT item_id FROM item_redflag WHERE item_type='document' "
                f"AND item_id IN ({qmarks})", ids).fetchall()]
            if flagged:
                return jsonify({"error": f"{len(flagged)} selected document(s) are red-flagged — "
                                         f"clear the flag before sharing"}), 409
        uid = user.get('id')
        for doc_id in ids:
            res = (visibility.share(conn, 'document', doc_id, 'client', uid) if on
                   else visibility.unshare(conn, 'document', doc_id, 'client', uid))
            if not res.get('ok'):
                conn.rollback()   # no partial writes — the whole call fails together
                return jsonify({"error": res.get('reason', 'cannot share')}), 409
        conn.commit()
        states = {str(doc_id): visibility.state(conn, 'document', doc_id) for doc_id in ids}
        resp = response_wrapper({'on': on, 'count': len(ids), 'states': states})
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    finally:
        conn.close()


# ============= STATIC / FILE SERVING (#248 — allowlist by construction) =============
#
# Three tiers, least privilege first:
#   1. /files/static/*  — PUBLIC. Vendored shell assets ONLY. Served by
#      Flask's built-in static route (static_folder is the static/ subtree —
#      see app = Flask(...) at the top), so nothing outside static/ is
#      reachable here BY CONSTRUCTION. Same URLs + same caching machinery as
#      the pre-#248 whole-project mount, so offline-safe / cache-bust
#      behavior is unchanged for the shell.
#   2. /project-files/<rel> — GATED. Generated artifacts under the
#      _ARTIFACT_ROOTS allowlist (defined at the top of this file).
#   3. /<path:filename> — GATED root catch-all for legacy root outputs,
#      restricted to _ROOT_SERVE_DIRS + artifact extensions.
# NEVER SERVABLE by any route, any auth state: *.db / -wal / -shm, DB
# snapshots, *.py, *.md, .env*, dotfiles, tests/ — by construction here,
# asserted by tests/smoke_static_exposure.py in the gate.


@app.route('/project-files/<path:relpath>', methods=['GET'])
@requires_role('admin', 'c_suite', 'pm', 'super')
def serve_project_file(relpath):
    """Gated artifact serving (#248). Generated artifacts carry project data
    (DCR renders, site photos, credential card exports) — never public. The
    before_request gate authenticates the session (this prefix is NOT
    exempt and gets 401 JSON when anonymous, never a /login redirect — #172:
    a redirect cached against an <img> URL poisons the browser cache). The
    role list is every dashboard role, spelled out so a future restricted
    role does not inherit artifact access silently."""
    target = _safe_artifact_path(relpath)
    if target is None:
        return jsonify({"error": "not found"}), 404
    # #305 — project-scoped: a pm/super may only fetch artifacts of projects they are
    # assigned to. The project code is the first path segment that names a project.
    u = current_user() or {}
    if u.get("role") not in access.COMPANY_ROLES:
        import pm_scoping
        conn = db()
        try:
            codes = {r["project_code"] for r in conn.execute("SELECT project_code FROM projects").fetchall()}
            hit = next((seg for seg in relpath.replace("\\", "/").split("/") if seg in codes), None)
            if hit is None or not pm_scoping.pm_can_access_project(u.get("role"), u.get("id"), hit, conn):
                return jsonify({"error": "not found"}), 404
        finally:
            conn.close()
    return send_file(str(target))


@app.route('/worker-app-manifest.json')
def worker_app_manifest():
    """Worker-app PWA manifest — public shell file (code/config, no data).
    Public-exact in auth.py (#248); needs an explicit route because the
    hardened catch-all below denies .json by design."""
    resp = send_file(str(SCRIPT_DIR / 'worker-app-manifest.json'), max_age=0)
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    return resp


@app.route('/favicon.ico')
def favicon():
    """Browser-tab icon fallback — the star mark as PNG. Browsers request
    /favicon.ico on any page without a <link rel="icon"> (generated reports,
    Python-rendered pages), so this keeps the grey globe off every tab.
    Public-exact in auth.py: brand asset, no data."""
    resp = send_file(str(SCRIPT_DIR / 'static' / 'brand' / 'favicon-32.png'), mimetype='image/png')
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/worker-app-sw.js')
def worker_app_sw():
    """Worker-app service worker — public shell file. Explicit route for the
    same reason as the manifest; the after_request hook already no-caches
    *-sw.js so updates reach devices."""
    return send_file(str(SCRIPT_DIR / 'worker-app-sw.js'))


@app.route('/<path:filename>')
def static_files(filename):
    """Authed catch-all for legacy root outputs by name (e.g.
    /DCR-FR-BX-001-2026-05-05-internal.html, /drop_plans/DP-001.html,
    /rfi_submission_form.html). #248: allowlist by construction — root-level
    files plus _ROOT_SERVE_DIRS, artifact extensions only, traversal-proof.
    Sensitive trees (data_room/, worker_records/, tests/, ...) are not
    reachable here: data_room artifacts go through /project-files/, worker
    files through /worker-files/."""
    parts = _split_safe_rel(filename)
    if parts is None:
        return jsonify({"error": "File not found", "path": filename}), 404
    if len(parts) > 1 and parts[0] not in _ROOT_SERVE_DIRS:
        return jsonify({"error": "File not found", "path": filename}), 404
    # #287 — legacy outputs are DATA: prefer the data root; fall back to the
    # repo dir for pre-data_room files that were never migrated. Unset root =
    # both bases are SCRIPT_DIR (identical to the old single check).
    resolved = None
    for base in (ssc_paths.data_root(), SCRIPT_DIR):
        candidate = base.joinpath(*parts)
        if candidate.suffix.lower() not in _ARTIFACT_EXTENSIONS:
            return jsonify({"error": "File not found", "path": filename}), 404
        try:
            r = candidate.resolve()
            if r.is_relative_to(base.resolve()) and r.is_file():
                resolved = r
                break
        except OSError:
            pass
    if resolved is None:
        return jsonify({"error": "File not found", "path": filename}), 404
    if not resolved.is_file():
        return jsonify({"error": "File not found", "path": filename}), 404
    return send_file(str(resolved))


@app.after_request
def add_no_cache_headers(response):
    """Force browsers (especially iPhone Safari) to always pull fresh HTML/JS.
    Without this, Safari aggressively caches PWA assets and edits never reach the device.
    """
    path = request.path or ''
    # #291 — the vendored static bundle (/files/static/*) gets BOUNDED freshness
    # instead of no-store: 10 min browser+edge (Cloudflare caches it; repeats are
    # edge HITs) with Flask's ETag/conditional revalidation after. Deploys are
    # fully fresh within the window. The Safari-PWA no-store lesson (#205) stays
    # scoped to PAGES and the worker-app shell below — those still never cache.
    if path.startswith('/files/static/'):
        response.headers['Cache-Control'] = 'public, max-age=600'
        response.headers.pop('Pragma', None)
        response.headers.pop('Expires', None)
        return response
    if path.endswith('.html') or path.endswith('.js') or path.endswith('-sw.js') or path == '/' or path.startswith('/worker-app'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

# ============= EXPENSE / SPEND MODULE (#218, Batch A) =============
# Per-project expense capture: header + line items + receipt image + the
# product-usage rollup that feeds estimating. COST DATA -> gated to the
# established money roles (admin/c_suite); non-cost roles get 403 and the
# nav/views are OMITTED client-side. Money is computed with Decimal (no float
# drift); dates are LOCAL YYYY-MM-DD; receipt_image_path is NEVER serialized.
from decimal import Decimal, ROUND_HALF_UP

_EXPENSE_COST_ROLES = ('admin', 'c_suite')

EXPENSE_PRODUCT_CLASSES = [
    'MASONRY', 'CEMENT_MORTAR', 'CONCRETE_REPAIR', 'SEALANTS_CAULK', 'WATERPROOFING',
    'GFRC_PRECAST', 'STUCCO_EIFS', 'COATINGS_PAINT', 'ROOFING', 'ELECTRICAL',
    'EQUIP_RENTAL', 'EQUIP_PURCHASE', 'SCAFFOLD_ACCESS', 'TOOLS_CONSUMABLES',
    'FASTENERS_HARDWARE', 'PPE_SAFETY', 'DUMPSTER_DISPOSAL', 'FUEL_VEHICLE',
    'DELIVERY_FREIGHT', 'PERMITS_FEES', 'SUBCONTRACTOR', 'DEPOSIT_REFUNDABLE',
    'CREDIT_RETURN', 'OTHER',
]
EXPENSE_CLASS_LABELS = {
    'MASONRY': 'Masonry', 'CEMENT_MORTAR': 'Cement / Mortar',
    'CONCRETE_REPAIR': 'Concrete Repair', 'SEALANTS_CAULK': 'Sealants / Caulk',
    'WATERPROOFING': 'Waterproofing', 'GFRC_PRECAST': 'GFRC / Precast',
    'STUCCO_EIFS': 'Stucco / EIFS', 'COATINGS_PAINT': 'Coatings / Paint',
    'ROOFING': 'Roofing', 'ELECTRICAL': 'Electrical', 'EQUIP_RENTAL': 'Equip Rental',
    'EQUIP_PURCHASE': 'Equip Purchase', 'SCAFFOLD_ACCESS': 'Scaffold / Access',
    'TOOLS_CONSUMABLES': 'Tools / Consumables', 'FASTENERS_HARDWARE': 'Fasteners / Hardware',
    'PPE_SAFETY': 'PPE / Safety', 'DUMPSTER_DISPOSAL': 'Dumpster / Disposal',
    'FUEL_VEHICLE': 'Fuel / Vehicle', 'DELIVERY_FREIGHT': 'Delivery / Freight',
    'PERMITS_FEES': 'Permits / Fees', 'SUBCONTRACTOR': 'Subcontractor',
    'DEPOSIT_REFUNDABLE': 'Deposit (refundable)', 'CREDIT_RETURN': 'Credit / Return',
    'OTHER': 'Other',
}
EXPENSE_UNITS = [
    'PC', 'EA', 'bag', 'cube', 'pallet', 'tube', 'sausage', 'case', 'box', 'bucket',
    'pail', 'gallon', 'roll', 'board', 'SF', 'LF', 'lb', 'ton', 'day', 'week', 'month',
    'pull', 'LS',
]
EXPENSE_STATUSES = ('draft', 'needs_review', 'reviewed')
_EXP_OUT_OF_COST_CLASSES = {'DEPOSIT_REFUNDABLE', 'CREDIT_RETURN'}
_EXP_REFUNDABLE_CLASSES = {'DEPOSIT_REFUNDABLE'}
_RECEIPT_EXT = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.pdf'}
_RECEIPT_MIME = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.heic': 'image/heic', '.heif': 'image/heif', '.pdf': 'application/pdf',
}
_RECEIPTS_BASE = ssc_paths.under_root("data_room", "receipts")   # #287


def _exp_dec(v):
    try:
        return Decimal(str(v if v is not None else 0))
    except Exception:
        return Decimal('0')


def _exp_q2(d):
    return d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _exp_money(v):
    """Float rounded to 2dp for JSON (computed via Decimal — no float drift)."""
    return float(_exp_q2(_exp_dec(v)))


def _exp_actor_uid():
    return (current_user() or {}).get('id')


def _exp_normalize_line(ln, idx):
    """Validate class/unit, derive refundable + out_of_cost from flags + class,
    compute extended_price via Decimal (trust the receipt's line total if given,
    else qty*unit_price)."""
    pc = ln.get('product_class') or 'OTHER'
    if pc not in EXPENSE_PRODUCT_CLASSES:
        pc = 'OTHER'
    qty = _exp_dec(ln.get('qty'))
    unit_price = _exp_dec(ln.get('unit_price'))
    ext_raw = ln.get('extended_price')
    ext = _exp_q2(_exp_dec(ext_raw)) if ext_raw not in (None, '') else _exp_q2(qty * unit_price)
    refundable = 1 if (ln.get('is_refundable') or pc in _EXP_REFUNDABLE_CLASSES) else 0
    out_of_cost = 1 if (refundable or ln.get('out_of_cost') or pc in _EXP_OUT_OF_COST_CLASSES) else 0
    try:
        sort_order = int(ln.get('sort_order'))
    except (TypeError, ValueError):
        sort_order = idx
    return {
        'item_id': (ln.get('item_id') or None),
        'description': (ln.get('description') or ''),
        'product_class': pc,
        'normalized_product': (ln.get('normalized_product') or None),
        'qty': float(qty),
        'unit': (ln.get('unit') or 'EA'),
        'unit_price': float(unit_price),
        'extended_price': float(ext),
        'is_refundable': refundable,
        'out_of_cost': out_of_cost,
        'sort_order': sort_order,
    }


def _exp_compute_total(lines):
    tot = Decimal('0')
    for ln in lines:
        if not ln.get('out_of_cost'):
            tot += _exp_dec(ln.get('extended_price'))
    return float(_exp_q2(tot))


def _exp_public(row):
    """Safe expense dict for JSON: drops receipt_image_path (PII/path rule),
    exposes has_receipt boolean + money rounded via Decimal."""
    d = dict(row)
    rip = d.pop('receipt_image_path', None)
    d['has_receipt'] = bool(rip)
    if 'total' in d:
        d['total'] = _exp_money(d.get('total'))
    return d


def _exp_line_public(row):
    d = dict(row)
    d['qty'] = float(d.get('qty') or 0)
    d['unit_price'] = float(d.get('unit_price') or 0)
    d['extended_price'] = _exp_money(d.get('extended_price'))
    d['is_refundable'] = bool(d.get('is_refundable'))
    d['out_of_cost'] = bool(d.get('out_of_cost'))
    return d


def _exp_store_receipt(project_code, file_storage):
    """Store under data_room/receipts/<project>/<uuid>.<ext> (non-guessable).
    Returns (abs_path_str, None) or (None, error)."""
    ext = Path(file_storage.filename or '').suffix.lower()
    if ext not in _RECEIPT_EXT:
        return None, "Unsupported file type — use JPG, PNG, HEIC, or PDF"
    base = _RECEIPTS_BASE.resolve()
    rdir = _RECEIPTS_BASE / project_code
    if not rdir.resolve().is_relative_to(base):
        return None, "Invalid path"
    rdir.mkdir(parents=True, exist_ok=True)
    path = rdir / f"{uuid.uuid4().hex}{ext}"
    file_storage.save(str(path))
    return str(path), None


def _exp_unlink_receipt(rip):
    """Delete a receipt IFF it's inside the receipts base (never escapes). For a
    multi-page scan (scan_<uuid>/ folder) remove the whole folder (all pages)."""
    if not rip:
        return
    try:
        p = Path(rip)
        base = _RECEIPTS_BASE.resolve()
        if not p.resolve().is_relative_to(base):
            return
        if p.parent.name.startswith('scan_') and p.parent.resolve().is_relative_to(base):
            shutil.rmtree(str(p.parent), ignore_errors=True)
        elif p.exists():
            p.unlink()
    except Exception as fe:
        logging.warning(f"receipt unlink failed: {fe}")


def _exp_read_payload():
    """Return (header_dict, lines_list_or_None, receipt_filestorage_or_None) for
    both multipart (capture w/ image) and JSON (manual) requests."""
    HEADER_FIELDS = ('vendor', 'doc_type', 'doc_number', 'order_number',
                     'expense_date', 'category', 'cost_code', 'payment_method',
                     'status', 'notes')
    ctype = request.content_type or ''
    if ctype.startswith('multipart/form-data'):
        form = request.form
        header = {k: form.get(k) for k in HEADER_FIELDS}
        lines = None
        if form.get('lines'):
            try:
                parsed = json.loads(form.get('lines'))
                lines = parsed if isinstance(parsed, list) else None
            except Exception:
                lines = None
        return header, lines, request.files.get('receipt')
    body = request.get_json(silent=True) or {}
    header = {k: body.get(k) for k in HEADER_FIELDS}
    lines = body.get('lines') if isinstance(body.get('lines'), list) else None
    return header, lines, None


def _exp_insert_lines(conn, expense_id, norm_lines):
    for ln in norm_lines:
        conn.execute(
            "INSERT INTO expense_line_items (expense_id, item_id, description, "
            "product_class, normalized_product, qty, unit, unit_price, extended_price, "
            "is_refundable, out_of_cost, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (expense_id, ln['item_id'], ln['description'], ln['product_class'],
             ln['normalized_product'], ln['qty'], ln['unit'], ln['unit_price'],
             ln['extended_price'], ln['is_refundable'], ln['out_of_cost'], ln['sort_order']))


def _exp_detail_dict(conn, expense_id):
    row = conn.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
    if not row:
        return None
    out = _exp_public(row)
    lrows = conn.execute(
        "SELECT * FROM expense_line_items WHERE expense_id=? ORDER BY sort_order, id",
        (expense_id,)).fetchall()
    out['line_items'] = [_exp_line_public(r) for r in lrows]
    return out


@app.route('/api/expenses/taxonomy', methods=['GET'])
@requires_role(*_EXPENSE_COST_ROLES)
def api_expense_taxonomy():
    """The shared product-class + unit enums (single source for the picker)."""
    return response_wrapper({
        'classes': [{'code': c, 'label': EXPENSE_CLASS_LABELS.get(c, c)} for c in EXPENSE_PRODUCT_CLASSES],
        'units': EXPENSE_UNITS,
        'statuses': list(EXPENSE_STATUSES),
        'out_of_cost_classes': sorted(_EXP_OUT_OF_COST_CLASSES),
        'refundable_classes': sorted(_EXP_REFUNDABLE_CLASSES),
    })


@app.route('/api/projects/<project_code>/expenses', methods=['GET'])
@requires_role(*_EXPENSE_COST_ROLES)
def api_expenses_list(project_code):
    """List + filters (q, vendor, category, product_class, cost_code, status,
    from/to) + KPIs (total spend [excl out_of_cost — already baked into
    expense.total], this month, receipts on file, needs-review). No *_path."""
    conn = db()
    try:
        if not validate_project_exists(conn, project_code):
            return jsonify({"error": "Project not found"}), 404
        where = ["e.project_code = ?"]
        params = [project_code]
        for col, arg in (('vendor', 'vendor'), ('category', 'category'),
                         ('cost_code', 'cost_code'), ('status', 'status')):
            v = request.args.get(arg)
            if v:
                where.append(f"e.{col} = ?")
                params.append(v)
        fd = request.args.get('from')
        if fd:
            where.append("e.expense_date >= ?")
            params.append(fd)
        td = request.args.get('to')
        if td:
            where.append("e.expense_date <= ?")
            params.append(td)
        pclass = request.args.get('product_class')
        if pclass:
            where.append("EXISTS (SELECT 1 FROM expense_line_items li WHERE li.expense_id=e.id AND li.product_class=?)")
            params.append(pclass)
        q = (request.args.get('q') or '').strip()
        if q:
            like = f"%{q}%"
            where.append("(e.vendor LIKE ? OR e.doc_number LIKE ? OR e.order_number LIKE ? OR "
                         "e.notes LIKE ? OR e.cost_code LIKE ? OR EXISTS (SELECT 1 FROM "
                         "expense_line_items lq WHERE lq.expense_id=e.id AND "
                         "(lq.description LIKE ? OR lq.item_id LIKE ?)))")
            params += [like] * 7
        rows = conn.execute(
            "SELECT e.* FROM expenses e WHERE " + " AND ".join(where) +
            " ORDER BY e.expense_date DESC, e.id DESC", params).fetchall()
        # line counts in one pass (avoid N+1 at scale)
        ids = [r['id'] for r in rows]
        lc_map = {}
        if ids:
            qmarks = ",".join("?" * len(ids))
            for lr in conn.execute(
                f"SELECT expense_id, COUNT(*) c, COUNT(DISTINCT product_class) dc "
                f"FROM expense_line_items WHERE expense_id IN ({qmarks}) GROUP BY expense_id", ids):
                lc_map[lr['expense_id']] = (lr['c'], lr['dc'])
        out = []
        total_spend = Decimal('0')
        month_spend = Decimal('0')
        receipts_on_file = 0
        needs_review = 0
        ym = date.today().strftime('%Y-%m')
        for r in rows:
            d = _exp_public(r)
            c, dc = lc_map.get(r['id'], (0, 0))
            d['line_count'] = c
            d['product_class_count'] = dc
            out.append(d)
            total_spend += _exp_dec(r['total'])
            if (r['expense_date'] or '').startswith(ym):
                month_spend += _exp_dec(r['total'])
            if r['receipt_image_path']:
                receipts_on_file += 1
            if r['status'] == 'needs_review':
                needs_review += 1
        return jsonify({
            "data": out,
            "meta": {"count": len(out), "generated_at": datetime.now().isoformat()},
            "kpis": {
                "total_spend": float(_exp_q2(total_spend)),
                "this_month": float(_exp_q2(month_spend)),
                "receipts_on_file": receipts_on_file,
                "needs_review": needs_review,
            },
        })
    finally:
        conn.close()


@app.route('/api/projects/<project_code>/expenses', methods=['POST'])
@requires_role(*_EXPENSE_COST_ROLES)
def api_expenses_create(project_code):
    """Create header + lines (+ optional receipt image multipart). Admin manual
    entry may pass status=reviewed; default needs_review."""
    conn = db()
    try:
        if not validate_project_exists(conn, project_code):
            return jsonify({"error": "Project not found"}), 404
        header, lines, receipt = _exp_read_payload()
        exp_date = (header.get('expense_date') or '').strip() or date.today().isoformat()
        try:
            datetime.strptime(exp_date, '%Y-%m-%d')
        except ValueError:
            return jsonify({"error": "expense_date must be YYYY-MM-DD"}), 400
        status = header.get('status') or 'needs_review'
        if status not in EXPENSE_STATUSES:
            status = 'needs_review'
        norm = [_exp_normalize_line(ln, i) for i, ln in enumerate(lines or [])]
        total = _exp_compute_total(norm)
        uid = _exp_actor_uid()
        now = datetime.now().isoformat()
        reviewed_by = uid if status == 'reviewed' else None
        reviewed_at = now if status == 'reviewed' else None
        cur = conn.execute(
            "INSERT INTO expenses (project_code, vendor, doc_type, doc_number, order_number, "
            "expense_date, category, cost_code, payment_method, total, status, notes, created_at, "
            "created_by_uid, reviewed_by_uid, reviewed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_code, header.get('vendor'), header.get('doc_type'), header.get('doc_number'),
             header.get('order_number'), exp_date, header.get('category'), header.get('cost_code'),
             header.get('payment_method'), total, status, header.get('notes'), now, uid,
             reviewed_by, reviewed_at))
        expense_id = cur.lastrowid
        _exp_insert_lines(conn, expense_id, norm)
        # #219 — learn (vendor,item)->class from every confirmed save (manual or
        # reviewed scan) so future scans of the same SKU auto-classify.
        _exp_learn_aliases(conn, header.get('vendor'), norm)
        if receipt and receipt.filename:
            rip, err = _exp_store_receipt(project_code, receipt)
            if err:
                conn.rollback()
                return jsonify({"error": err}), 400
            conn.execute("UPDATE expenses SET receipt_image_path=? WHERE id=?", (rip, expense_id))
        conn.commit()
        return response_wrapper(_exp_detail_dict(conn, expense_id)), 201
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logging.error(f"POST expenses: {e}")
        return _server_error(e)
    finally:
        conn.close()


@app.route('/api/expenses/<int:expense_id>', methods=['GET'])
@requires_role(*_EXPENSE_COST_ROLES)
def api_expense_detail(expense_id):
    conn = db()
    try:
        out = _exp_detail_dict(conn, expense_id)
        if out is None:
            return jsonify({"error": "Expense not found"}), 404
        return response_wrapper(out)
    finally:
        conn.close()


@app.route('/api/expenses/<int:expense_id>', methods=['PATCH'])
@requires_role(*_EXPENSE_COST_ROLES)
def api_expense_patch(expense_id):
    """Edit header / replace lines (recompute total) / set status. Setting
    status=reviewed stamps reviewed_by_uid + reviewed_at (PM approve)."""
    conn = db()
    try:
        row = conn.execute("SELECT id FROM expenses WHERE id=?", (expense_id,)).fetchone()
        if not row:
            return jsonify({"error": "Expense not found"}), 404
        body = request.get_json(silent=True) or {}
        sets, params = [], []
        for f in ('vendor', 'doc_type', 'doc_number', 'order_number', 'category',
                  'cost_code', 'payment_method', 'notes'):
            if f in body:
                sets.append(f"{f}=?")
                params.append(body.get(f))
        if 'expense_date' in body:
            ed = (body.get('expense_date') or '').strip()
            if ed:
                try:
                    datetime.strptime(ed, '%Y-%m-%d')
                except ValueError:
                    return jsonify({"error": "expense_date must be YYYY-MM-DD"}), 400
            sets.append("expense_date=?")
            params.append(ed or None)
        if 'status' in body:
            st = body.get('status')
            if st not in EXPENSE_STATUSES:
                return jsonify({"error": "bad status"}), 400
            sets.append("status=?")
            params.append(st)
            if st == 'reviewed':
                sets += ["reviewed_by_uid=?", "reviewed_at=?"]
                params += [_exp_actor_uid(), datetime.now().isoformat()]
            else:
                sets += ["reviewed_by_uid=?", "reviewed_at=?"]
                params += [None, None]
        norm_for_learn = None
        if isinstance(body.get('lines'), list):
            norm_for_learn = [_exp_normalize_line(ln, i) for i, ln in enumerate(body['lines'])]
            conn.execute("DELETE FROM expense_line_items WHERE expense_id=?", (expense_id,))
            _exp_insert_lines(conn, expense_id, norm_for_learn)
            sets.append("total=?")
            params.append(_exp_compute_total(norm_for_learn))
        if sets:
            params.append(expense_id)
            conn.execute(f"UPDATE expenses SET {', '.join(sets)} WHERE id=?", params)
        # #219 — alias learning on edit/approve (the user just confirmed these lines)
        if norm_for_learn is not None:
            vrow = conn.execute("SELECT vendor FROM expenses WHERE id=?", (expense_id,)).fetchone()
            _exp_learn_aliases(conn, vrow['vendor'] if vrow else None, norm_for_learn)
        conn.commit()
        return response_wrapper(_exp_detail_dict(conn, expense_id))
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logging.error(f"PATCH expense {expense_id}: {e}")
        return _server_error(e)
    finally:
        conn.close()


@app.route('/api/expenses/<int:expense_id>', methods=['DELETE'])
@requires_role(*_EXPENSE_COST_ROLES)
def api_expense_delete(expense_id):
    """Cascade-delete lines + THIS expense's receipt file only. Others untouched."""
    conn = db()
    try:
        row = conn.execute("SELECT receipt_image_path FROM expenses WHERE id=?", (expense_id,)).fetchone()
        if not row:
            return jsonify({"error": "Expense not found"}), 404
        rip = row['receipt_image_path']
        conn.execute("DELETE FROM expense_line_items WHERE expense_id=?", (expense_id,))
        conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
        conn.commit()
        _exp_unlink_receipt(rip)
        return response_wrapper({"deleted": expense_id})
    finally:
        conn.close()


@app.route('/api/expenses/<int:expense_id>/receipt', methods=['POST'])
@requires_role(*_EXPENSE_COST_ROLES)
def api_expense_receipt_upload(expense_id):
    conn = db()
    try:
        row = conn.execute("SELECT project_code, receipt_image_path FROM expenses WHERE id=?", (expense_id,)).fetchone()
        if not row:
            return jsonify({"error": "Expense not found"}), 404
        f = request.files.get('receipt') or request.files.get('file')
        if not f or not f.filename:
            return jsonify({"error": "No receipt file"}), 400
        rip, err = _exp_store_receipt(row['project_code'], f)
        if err:
            return jsonify({"error": err}), 400
        old = row['receipt_image_path']
        conn.execute("UPDATE expenses SET receipt_image_path=? WHERE id=?", (rip, expense_id))
        conn.commit()
        if old and old != rip:
            _exp_unlink_receipt(old)
        return response_wrapper({
            "has_receipt": True,
            "content_type": _RECEIPT_MIME.get(Path(rip).suffix.lower(), 'application/octet-stream'),
        }), 201
    finally:
        conn.close()


@app.route('/api/expenses/<int:expense_id>/receipt', methods=['GET'])
@requires_role(*_EXPENSE_COST_ROLES)
def api_expense_receipt_get(expense_id):
    """Serve the receipt image ONLY through this auth-gated route (never /files)."""
    from flask import send_file
    conn = db()
    try:
        row = conn.execute("SELECT receipt_image_path FROM expenses WHERE id=?", (expense_id,)).fetchone()
        if not row:
            return jsonify({"error": "Expense not found"}), 404
        rip = row['receipt_image_path']
        if not rip:
            return jsonify({"error": "No receipt on file"}), 404
        p = ssc_paths.resolve_data_path(rip)   # #305 — same resolver as every other byte route
        # #219 — ?page=N serves page N of a multi-page scan (page 1 = the stored path)
        page = request.args.get('page', type=int)
        if page and page > 1 and p.parent.name.startswith('scan_'):
            cand = sorted(p.parent.glob(f"page_{page:03d}.*"))
            if cand:
                p = cand[0]
        if not (p.resolve().is_relative_to(_RECEIPTS_BASE.resolve()) and p.exists()):
            return jsonify({"error": "Receipt file missing"}), 404
        return send_file(str(p), mimetype=_RECEIPT_MIME.get(p.suffix.lower(), 'application/octet-stream'))
    finally:
        conn.close()


@app.route('/api/projects/<project_code>/expenses/product-usage', methods=['GET'])
@requires_role(*_EXPENSE_COST_ROLES)
def api_expense_product_usage(project_code):
    """Rollup grouped by product_class -> normalized_product (fallback to the
    description) -> unit: sum qty, distinct receipts, sum extended_price (excl
    out_of_cost). Refundable/out-of-cost amounts kept separate (shown with ↩)."""
    conn = db()
    try:
        if not validate_project_exists(conn, project_code):
            return jsonify({"error": "Project not found"}), 404
        rows = conn.execute(
            "SELECT li.product_class, "
            "COALESCE(NULLIF(TRIM(li.normalized_product), ''), li.description) AS product, "
            "li.unit, li.qty, li.extended_price, li.is_refundable, li.out_of_cost, li.expense_id "
            "FROM expense_line_items li JOIN expenses e ON e.id=li.expense_id "
            "WHERE e.project_code=?", (project_code,)).fetchall()
        groups = {}
        for r in rows:
            key = (r['product_class'], (r['product'] or '—'), (r['unit'] or 'EA'))
            g = groups.setdefault(key, {'qty': Decimal('0'), 'receipts': set(),
                                        'cost': Decimal('0'), 'refundable': Decimal('0'),
                                        'is_refundable': False, 'out_of_cost': False})
            g['qty'] += _exp_dec(r['qty'])
            g['receipts'].add(r['expense_id'])
            if r['out_of_cost']:
                g['refundable'] += _exp_dec(r['extended_price'])
                g['out_of_cost'] = True
                if r['is_refundable']:
                    g['is_refundable'] = True
            else:
                g['cost'] += _exp_dec(r['extended_price'])
        out = []
        for (cls, prod, unit), g in groups.items():
            out.append({
                'product_class': cls,
                'product_class_label': EXPENSE_CLASS_LABELS.get(cls, cls),
                'product': prod, 'unit': unit,
                'qty': float(g['qty']),
                'receipts': len(g['receipts']),
                'total_spend': float(_exp_q2(g['cost'])),
                'refundable_total': float(_exp_q2(g['refundable'])),
                'is_refundable': g['is_refundable'],
                'out_of_cost': g['out_of_cost'],
            })
        out.sort(key=lambda x: (x['product_class'], x['product']))
        cost_total = sum((_exp_dec(x['total_spend']) for x in out), Decimal('0'))
        refund_total = sum((_exp_dec(x['refundable_total']) for x in out), Decimal('0'))
        return jsonify({
            "data": out,
            "meta": {"count": len(out), "generated_at": datetime.now().isoformat()},
            "totals": {"cost_total": float(_exp_q2(cost_total)),
                       "refundable_total": float(_exp_q2(refund_total))},
        })
    finally:
        conn.close()


# ---- #219 Batch B — AI receipt scan + alias memory ----

def _exp_store_receipt_pages(project_code, files):
    """Store ALL pages under data_room/receipts/<project>/scan_<uuid>/page_NNN.<ext>.
    Returns (first_page_path, page_count, error)."""
    base = _RECEIPTS_BASE.resolve()
    rdir = _RECEIPTS_BASE / project_code / f"scan_{uuid.uuid4().hex}"
    if not rdir.resolve().is_relative_to(base):
        return None, 0, "Invalid path"
    rdir.mkdir(parents=True, exist_ok=True)
    first, n = None, 0
    for i, f in enumerate(files, 1):
        ext = Path(f.filename or '').suffix.lower()
        if ext not in _RECEIPT_EXT:
            return None, 0, f"Unsupported file type {ext} — use JPG, PNG, HEIC, or PDF"
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(0)
        if size > 12 * 1024 * 1024:
            return None, 0, "A page is too large (max 12 MB/page)"
        p = rdir / f"page_{i:03d}{ext}"
        f.save(str(p))
        if first is None:
            first = str(p)
        n += 1
    return first, n, None


def _exp_page_paths(first_path, page_count):
    """Ordered page paths for a (possibly multi-page) receipt.
    #294 S3 — resolve the stored path first: a pre-#287 Windows-absolute row has
    no parent to glob on a rooted Linux host (today's one caller passes a
    fresh-stored path, where resolving is a no-op — this is for stored-row reuse)."""
    if not first_path:
        return []
    resolved = ssc_paths.resolve_data_path(first_path)
    parent = resolved.parent
    if parent.name.startswith('scan_'):
        pages = sorted(parent.glob('page_*'))
        return [str(x) for x in pages] or [str(resolved)]
    return [str(resolved)]


def _exp_alias_lookup(conn, vendor):
    """{item_key_lower: {product_class, normalized_product}} for one vendor."""
    if not vendor:
        return {}
    out = {}
    for r in conn.execute(
        "SELECT item_key, product_class, normalized_product FROM expense_class_alias WHERE vendor=?",
        (vendor,)):
        if r['item_key']:
            out[r['item_key'].strip().lower()] = {
                "product_class": r['product_class'], "normalized_product": r['normalized_product']}
    return out


def _exp_learn_aliases(conn, vendor, norm_lines):
    """Upsert (vendor, item_key) -> class + normalized for confirmed lines so
    future scans of the same SKU auto-classify. item_key = item_id else
    normalized description. Skips OTHER / empty keys. PII-safe (no amounts)."""
    if not vendor:
        return
    now = datetime.now().isoformat()
    for ln in norm_lines:
        pc = ln.get('product_class')
        if not pc or pc == 'OTHER':
            continue
        key = (ln.get('item_id') or '').strip() or (ln.get('normalized_product') or ln.get('description') or '').strip()
        if not key:
            continue
        key_l = key.lower()
        existing = conn.execute(
            "SELECT id FROM expense_class_alias WHERE vendor=? AND item_key=?", (vendor, key_l)).fetchone()
        if existing:
            conn.execute("UPDATE expense_class_alias SET product_class=?, normalized_product=?, updated_at=? WHERE id=?",
                         (pc, ln.get('normalized_product'), now, existing['id']))
        else:
            conn.execute("INSERT INTO expense_class_alias (vendor, item_key, product_class, normalized_product, updated_at) VALUES (?,?,?,?,?)",
                         (vendor, key_l, pc, ln.get('normalized_product'), now))


@app.route('/api/expenses/scan', methods=['POST'])
@requires_role(*_EXPENSE_COST_ROLES)
def api_expense_scan():
    """Accept 1..N pages (images and/or a multi-page PDF) in one request, store
    them all on a new DRAFT expense, send them to the vision model in a SINGLE
    call, validate + classify + alias-override, land status=needs_review, and
    return the draft for review. Key is read from ENV only; missing key -> clean
    503 so the UI falls back to manual entry. Never leaks any *_path."""
    import expense_scanner as scanner
    files = (request.files.getlist('files') or request.files.getlist('pages')
             or request.files.getlist('receipt'))
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify({"error": "No files in request"}), 400
    if len(files) > scanner.MAX_PAGES:
        return jsonify({"error": f"Too many pages — max {scanner.MAX_PAGES}"}), 400
    for f in files:
        if Path(f.filename or '').suffix.lower() not in _RECEIPT_EXT:
            return jsonify({"error": "Unsupported file type — use JPG, PNG, HEIC, or PDF"}), 400
    project_code = request.form.get('project_code') or 'FR-BX-001'
    # EXPENSE_SCAN_FAKE (test-only, NEVER set in prod) bypasses the live model.
    fake_path = runtime_seams.seam('EXPENSE_SCAN_FAKE')
    if not os.environ.get('ANTHROPIC_API_KEY') and not fake_path:
        return jsonify({"error": "AI scan disabled — ANTHROPIC_API_KEY not configured. Enter the expense manually.",
                        "ai_available": False}), 503
    conn = db()
    try:
        if not validate_project_exists(conn, project_code):
            return jsonify({"error": "Project not found"}), 404
        # 1) DRAFT + store every page (originals always kept, tied to the expense)
        now = datetime.now().isoformat()
        uid = _exp_actor_uid()
        cur = conn.execute(
            "INSERT INTO expenses (project_code, status, total, created_at, created_by_uid) VALUES (?,?,?,?,?)",
            (project_code, 'draft', 0, now, uid))
        expense_id = cur.lastrowid
        first_path, page_count, err = _exp_store_receipt_pages(project_code, files)
        if err:
            conn.rollback()
            return jsonify({"error": err}), 400
        conn.execute("UPDATE expenses SET receipt_image_path=?, receipt_page_count=? WHERE id=?",
                     (first_path, page_count, expense_id))
        conn.commit()
        # 2) ONE vision call across all pages (or test-fake)
        specs = [scanner.file_spec(p) for p in _exp_page_paths(first_path, page_count)]
        try:
            if fake_path:
                with open(fake_path, encoding='utf-8') as fh:
                    raw = json.load(fh)
            else:
                raw = scanner.call_vision_model(specs, EXPENSE_PRODUCT_CLASSES, EXPENSE_UNITS)
        except scanner.ScanUnavailable:
            return jsonify({"error": "AI scan disabled — enter manually.", "ai_available": False,
                            "draft_id": expense_id}), 503
        except scanner.ScanError:
            return jsonify({"error": "Couldn't read the receipt automatically — enter it manually or try again.",
                            "scan_failed": True, "draft_id": expense_id}), 502
        # 3) validate + classify + alias override (using THIS vendor's alias memory)
        alias_map = _exp_alias_lookup(conn, raw.get('vendor'))
        processed = scanner.process_scan_result(
            raw, EXPENSE_PRODUCT_CLASSES, EXPENSE_UNITS, alias_map,
            _EXP_REFUNDABLE_CLASSES, _EXP_OUT_OF_COST_CLASSES)
        h = processed['header']
        norm = [_exp_normalize_line(l, i) for i, l in enumerate(processed['lines'])]
        total = _exp_compute_total(norm)
        exp_date = h.get('expense_date') or date.today().isoformat()
        try:
            datetime.strptime(exp_date, '%Y-%m-%d')
        except ValueError:
            exp_date = date.today().isoformat()
        notes = ('vendor_contact: ' + str(h.get('vendor_contact'))) if h.get('vendor_contact') else None
        conn.execute(
            "UPDATE expenses SET vendor=?, doc_type=?, doc_number=?, order_number=?, expense_date=?, "
            "status='needs_review', total=?, notes=? WHERE id=?",
            (h.get('vendor'), h.get('doc_type'), h.get('doc_number'), h.get('order_number'),
             exp_date, total, notes, expense_id))
        conn.execute("DELETE FROM expense_line_items WHERE expense_id=?", (expense_id,))
        _exp_insert_lines(conn, expense_id, norm)
        conn.commit()
        # 4) return draft + transient scan artifacts (confidence/warnings) — no *_path
        out = _exp_detail_dict(conn, expense_id)
        for i, li in enumerate(out.get('line_items', [])):
            if i < len(processed['lines']):
                li['confidence'] = processed['lines'][i]['confidence']
                li['low_confidence'] = processed['lines'][i]['low_confidence']
                li['alias_applied'] = processed['lines'][i]['alias_applied']
        out['scan'] = {
            "warnings": processed['warnings'],
            "low_confidence_count": processed['low_confidence_count'],
            "stated_total": h.get('stated_total'),
            "lines_sum_all": processed['lines_sum_all'],
            "page_count": page_count,
            "model": (raw.get('_meta') or {}).get('model') or scanner.MODEL,
        }
        return response_wrapper(out), 201
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logging.error(f"POST expenses/scan: {type(e).__name__}: {e}")
        return jsonify({"error": "Scan failed — enter manually or try again."}), 500
    finally:
        conn.close()


# ============= ERROR HANDLERS =============

# ============= #291 — PAGE BOOTSTRAP AGGREGATES (performance) =============
# One round trip instead of the page's parallel API fan-out. DISCIPLINE: an
# aggregate is a MIRROR, never a door — each part is produced by calling the
# EXACT view function Flask has registered for that endpoint (decorators and
# all), inside this same request context (auth gate, pm scoping and client/
# architect containment already ran). A part whose own gates refuse this
# caller is simply OMITTED from the payload; the page falls back to the
# individual endpoint, which refuses identically. Guard:
# tests/smoke_perf_291.py asserts key-for-key payload parity per role.

from werkzeug.exceptions import HTTPException as _AggHTTPException


def _agg_part(endpoint_name, *args, **kwargs):
    """Invoke the registered view function for `endpoint_name` and unwrap its
    JSON data payload. None (omitted) when the view's own gates refuse the
    caller or the view errors — the aggregate never converts a part failure
    into a page failure."""
    view = app.view_functions.get(endpoint_name)
    if view is None:
        return None
    try:
        resp = view(*args, **kwargs)
        body = resp[0] if isinstance(resp, tuple) else resp
        status = resp[1] if isinstance(resp, tuple) and len(resp) > 1 else getattr(body, "status_code", 200)
        if status != 200:
            return None
        j = body.get_json(silent=True)
        if j is None:
            return None
        return j.get("data", j)
    except _AggHTTPException:
        return None
    except Exception as e:
        logging.warning(f"#291 aggregate part {endpoint_name} failed: {e}")
        return None


def _agg_parallel(specs, workers=4):
    """#292 — run aggregate parts CONCURRENTLY. specs: {key: (endpoint, args)}.

    Each part executes on a worker thread inside a COPY of the current request
    context (built in the PARENT thread, where the context is active). The
    copy pushes a fresh app context, so the parent's authenticated user is
    seeded into the thread's g (current_user reads g.auth_user, which the
    before_request gate set only on the parent). db() inside the thread sees
    the fresh g too — each part gets ITS OWN connection, and the context pop
    fires teardown_request in-thread, closing it. The parts remain the exact
    registered gated view functions: mirror-not-door is unchanged, and the
    #37 parity guard proves it stays that way."""
    from concurrent.futures import ThreadPoolExecutor
    parent_user = current_user()

    def make(ep, args):
        @flask.copy_current_request_context
        def _inner():
            flask.g.auth_user = parent_user
            return _agg_part(ep, *args)
        return _inner

    jobs = {k: make(ep, args) for k, (ep, args) in specs.items()}
    out = {}
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(jobs)))) as ex:
        futs = {k: ex.submit(fn) for k, fn in jobs.items()}
        for k, f in futs.items():
            try:
                out[k] = f.result()
            except Exception as e:
                logging.warning(f"#292 parallel aggregate part {k} failed: {e}")
                out[k] = None
    return out


@app.route('/api/console/bootstrap', methods=['GET'])
@requires_company   # the console page itself is admin/c_suite-only (#263)
def api_console_bootstrap():
    """#291/#292 — the company console's load() fan-out in ONE response,
    parts executed in parallel. Query args ride through to the parts that
    read them (?project=, ?date=, ?limit=). A missing key means "fetch it
    individually" (and that fetch will gate identically)."""
    project = (request.args.get('project') or '').strip()
    if not project:
        conn = db()
        row = conn.execute("SELECT project_code FROM projects WHERE status='active' "
                           "ORDER BY project_code LIMIT 1").fetchone()
        conn.close()
        project = row["project_code"] if row else ""
    specs = {
        "summary": ("api_company_summary", ()),
        "projects": ("get_projects", ()),
        "compliance": ("api_compliance_summary", ()),
        "activity": ("api_activity_recent", ()),
        "material_alerts": ("materials_company_alerts", ()),
    }
    if project:
        specs["rollup"] = ("api_dropplan_rollup", (project,))
        specs["drops"] = ("api_dropplan_drops", (project,))
        specs["on_site"] = ("api_project_on_site", (project,))
    parts = _agg_parallel(specs)
    return response_wrapper({k: v for k, v in parts.items() if v is not None})


@app.route('/api/projects/<project_code>/bootstrap', methods=['GET'])
def api_project_bootstrap(project_code):
    """#291/#292 — the project dashboard's Project Health load() in ONE
    response, parts in parallel. Reached through the same /api/projects/<code>/
    path the #263 scoping hook guards; every part re-runs its own gates."""
    parts = _agg_parallel({
        "rollup": ("api_dropplan_rollup", (project_code,)),
        "drops": ("api_dropplan_drops", (project_code,)),
        "on_site": ("api_project_on_site", (project_code,)),
        "workers": ("api_project_workers", (project_code,)),
        "summary": ("api_project_summary", (project_code,)),   # #358 — the job dashboard's executive summary
    })
    return response_wrapper({k: v for k, v in parts.items() if v is not None})


@app.errorhandler(404)
def not_found(error):
    logging.error(f"404: {request.path}")
    return jsonify({"error": "Endpoint not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    logging.error(f"500: {str(error)}")
    return jsonify({"error": "Internal server error"}), 500

import friendly_errors  # people get a page, programs keep the JSON (403 / 404 on browser navigations)
friendly_errors.install(app)

# #305 — a second registration of the same (rule, method) silently shadows the first
# (the /api/rfis collision). Report duplicates at boot; strict mode refuses to start.
def _duplicate_routes():
    seen, dups = {}, []
    for r in app.url_map.iter_rules():
        for m in (r.methods or set()) - {"HEAD", "OPTIONS"}:
            key = (r.rule, m)
            if key in seen and seen[key] != r.endpoint:
                dups.append((r.rule, m, seen[key], r.endpoint))
            seen.setdefault(key, r.endpoint)
    return dups

_dups = _duplicate_routes()
if _dups:
    for rule, m, a, b in _dups:
        logging.error(f"duplicate route {m} {rule}: {a} shadows {b}")
    if os.environ.get("SSC_STRICT_ROUTES") == "1":
        raise SystemExit("duplicate routes registered — see log")


if __name__ == '__main__':
    import sys
    # The #273 dev-isolation marker (.dev_db_url) is applied at the TOP of this
    # file, before the db_layer import — the import-time boot ensures must see
    # the override, so it cannot live down here.
    print("\n" + "=" * 60, flush=True)
    print("  Server starting...", flush=True)
    print("  Try in browser: http://127.0.0.1:5050", flush=True)
    print("  Or:             http://localhost:5050", flush=True)
    print("=" * 60 + "\n", flush=True)
    sys.stdout.flush()
    # Loopback only per CLAUDE.md loopback policy: the workstation lives on a
    # shared coworking-space network — the dashboard must not be reachable from LAN.
    # PORT is env-overridable (default 5050) ONLY for the dev-server path — lets a
    # smoke spin up an isolated instance on another port; production (waitress via
    # run_server.ps1) is unaffected and still binds 5050.
    _port = int(os.environ.get('PORT', '5050'))
    app.run(host='127.0.0.1', port=_port, debug=False, use_reloader=False, threaded=True)
