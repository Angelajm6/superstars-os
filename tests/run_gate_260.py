#!/usr/bin/env python3
r"""#260 — run the FULL gate against an ISOLATED database, never the live superstars.db.

Phase 1 (hosting migration) test-isolation: the gate must never write to production.
This runner stands up a *separate* waitress test server bound to 127.0.0.1:<GATE_PORT>
(default 5434, away from the live :5050) with SSC_DB_URL pointed at an isolated backend
— either a SQLite snapshot COPY (sqlite:///C:/.../copy.db) or the ssc_test Postgres
database — runs every gate suite against it, then tree-kills ONLY its own server PID
(never the production server). server:app and every routed smoke read SSC_DB_URL through
db_layer, so nothing touches the live SQLite file.

Usage (from the dashboard dir):
  # Postgres test db
  $env:SSC_DB_URL="postgresql://postgres@127.0.0.1:5433/ssc_test"; venv\Scripts\python.exe tests\run_gate_260.py
  # SQLite isolated copy
  $env:SSC_DB_URL="sqlite:///C:/Users/SSC-Admin/Superstars/snapshots/ssc_gate_sqlite.db"; venv\Scripts\python.exe tests\run_gate_260.py
  # one or more named suites only:
  ... tests\run_gate_260.py smoke_dropplan_api.py smoke_auth.py

Env knobs: SSC_DB_URL (required, isolated backend), GATE_PORT (default 5434),
SMOKE_STATIC_PORT (default 5152 — static-exposure launches its own server).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DASH = Path(__file__).resolve().parent.parent
VENV_PY = DASH / "venv" / "Scripts" / "python.exe"
PORT = int(os.environ.get("GATE_PORT", "5434"))
BASE = f"http://127.0.0.1:{PORT}"

# The standard gate, in run order. The meta-smoke runs CRUD + worker_lifecycle as its
# own subprocesses; we also run them standalone for clear per-suite results.
GATE = [
    "smoke_design_conventions.py",
    "smoke_auth.py",
    "smoke_auth_roles.py",
    "smoke_auth_sso.py",          # #261 — Google SSO (mocked verification, own server)
    "smoke_labor_rates.py",
    "smoke_behavior_conventions.py",
    "smoke_pm_scoping_263.py",    # #263 — PM project-scoping (assignment + close lifecycle)
    "smoke_client_portal_264.py", # #264 — client portal + default-deny visibility engine
    "smoke_client_welcome_267.py",# #267 — client welcome hard-stop containment (client → /welcome only)
    "smoke_client_grants_269.py",# #269 — selective client un-gating (per-section default-off grants)
    "smoke_preview_presets_270.py",# #270 — preview-as-client parity + presets + doc bulk share
    "smoke_docs_photos_271.py",   # #271 — doc versioning + view-not-print + EXIF capture order
    "smoke_materials_272.py",     # #272a — materials catalog/ledger/expected/count + role gates
    "smoke_materials_272b.py",    # #272b — derived consumption/burn/reorder math + hint + widget
    "smoke_crm_266.py",           # #266 — CRM/ops core (C-suite-gated; entities/activity/tasks/needs-attention)
    "smoke_estimates_273.py",     # #273 — estimates/bid tracking (numeric series, transitions, convert, gated docs)
    "smoke_ira_pipeline_274.py",  # #274 — IRA pipeline (convert-hook, CD-5, COI expiry, visits, payment, role gates)
    "smoke_estimating_276.py",    # #276 — estimating core (stage machine, macro/micro, SLA aging, estimator role, notifications)
    "smoke_walkthroughs_277.py",  # #277 — walkthrough visits (stage sync, derivation), calendar merge, GPS-stripped reports
    "smoke_expenses_widget_278.py",  # #278 — cost widget (rate-boundary penny math, unrated bucket) + expense ledger + role omission
    "smoke_ui_v2_phase0.py",      # #279 — UI v2 toggle: byte-identity, resolution order, kill switch
    "smoke_external_scrub_281.py",# #281 — external hard gate: no internal links/build stamps/baked project, expenses is financial
    "smoke_client_registry_281.py",# #281 — client field registry: default-deny allowlist + the guard on the registry itself
    "smoke_markup_280.py",        # #283 guard — elevation scoping/vocabulary/derivation, comments, RFIs, drawing+rfis grants
    "smoke_portal_shell_281.py",  # #283 guard — /portal/<code> landing, preview, served-shell scrub, catalog, fail-closed registry
    "smoke_portal_flip_284.py",   # #284 guard — matrix fail-closed, nav-from-grants, registry payloads, daily allowlist, parity, the flip
    "smoke_data_root_287.py",     # #287 guard — SSC_DATA_ROOT containment (own rooted server; watchdog on the repo tree)
    "smoke_pdf_chromium_288.py",  # #288 guard — chromium PDF engine (synthetic DCR/form/CR80; planted bad-binary fails clean)
    "smoke_public_hardening_289.py",  # #289 guard — login backoff/no-enum, TOTP, force_sso, worker device binding, session flags
    "smoke_tz_290.py",            # #290 guard — SSC_TZ enforcement: Eastern today on a UTC host; planted 23:30 boundary; Windows no-op
    "smoke_perf_291.py",          # #291 guard — bootstrap aggregates mirror-not-door (parity, subset, containment) + thumb cache headers + lazy grid
    "smoke_memo_292.py",          # #292 guard — ssc_memo: planted-write invalidation per type (stale serve = red), two-role boundary, single-flight, deep-copy serves
    "smoke_drawing_author_293.py",  # #293 guard — drawing sets/naming vs the real fixture, AI seam + no-key degrade (own servers), draft/confirm machine, 890 backfill, isolation
    "smoke_photo_reassign_294.py",  # #294 guard — reassign history (single/batch), same-project guard, DCR amend flag+audit, self-service perms, timing buckets, settable alert threshold
    "smoke_hours_edit_295.py",    # #295 guard — sign-in correction history, issued-day amend flag + audit + auto re-render (content-verified), memo invalidation on the edit doors, honest Cancel copy
    "smoke_worker_sessions_296.py",    # #296 static contract — worker PWA bearer sessions
    "smoke_http_security_297.py",      # #297 static contract — response security headers
    "smoke_login_experience_298.py",   # #298 static contract — password visibility + reset routing
    "smoke_client_building_hub_299.py",# #299 static contract — client buildings / job sites in CRM
    "smoke_password_reset_300.py",     # #300 static contract — reset module + pages
    "smoke_password_reset_flow_301.py",# #301 guard — reset BEHAVIOR: no-enum, hashed single-use tokens, session revoke, expiry, throttle
    "smoke_company_portal_302.py",     # #302 guard — company portal: company-axis gating, clients/buildings rollups, finance, compliance, company documents CRUD
    "smoke_migrations_306.py",         # #306 guard — migration ledger: baseline, pending, apply/record, drift, health count
    "smoke_one_home_339.py",         # #339 guard — ONE company sidebar on every page; console dashboard + CRM retired; Add client on the portal; Exterior wording
    "smoke_dcr_numbers_344.py",      # #344 — DCR register, number preview, delete moves PDF + folder, pinned 109->097 fix
    "smoke_dashboard_week_338.py",   # #338 guard — "This week" strip on the company Dashboard: served, week window, company-only
    "smoke_job_sites_337.py",        # #337 guard — job sites without a project offered with a one-click path; ledger records boot-ensured migrations
    "smoke_building3d_336.py",       # #336 guard — the building in 3D: geocode/footprint cache (stubbed), maths, model, gates incl. client audience, page + vendored three.js
    "smoke_project_dates_334.py",    # #334 guard — projects.start_date / target_completion ensured dual-backend; odd date values never 500 the calendar
    "smoke_calendar_plus_333.py",    # #333 guard — calendar extras: headcount, milestones, weather mapping (stubbed), reschedule, page hooks
    "smoke_calendar_332.py",         # #332 guard — jobs calendar: window rules, open-ended / closed / undated, report dots, dates via onboarding + Crew & site, pages
    "smoke_regions_331.py",          # #331 guard — regions beyond the boroughs: one list, metas, NJ estimate series, LI job + job-site label, pickers
    "smoke_portal_projects_330.py",  # #330 guard — portal Projects = jobs: list, a job's own page (money, package, docs, crew, activity), no cross-job leakage, gates
    "smoke_payables_319.py",         # #319 guard — accounts payable: bills lifecycle, receipt + renewal feeds, 30-day cash view, overview payload, Dashboard rename
    "smoke_aia_billing_317.py",      # #317 guard — AIA G702/G703: SOV editor rules, one draft, over-billing, lines 1–9, issue -> progress invoice, carry-forward, void, documents, gates
    "smoke_nav_clarity_328.py",      # #328 guard — navigation clarity: project identity block on every view, Weekly Payroll / Pay Rates naming, company portal Projects label
    "smoke_doc_package_327.py",      # #327 guard — permits & insurance package: readiness, send to client + private link, portal share, red-flag revocation, gates
    "smoke_project_payments_326.py", # #326 guard — invoice documents: preview/pdf, email to client + private link, view tracking, project Payments, gates
    "smoke_crew_sites_325.py",       # #325 guard — crews + site coordinates + worker-app job picker: per-job geofence, chooser, shift start validation
    "smoke_project_onboard_324.py",  # #324 guard — onboard an already-won job: gates, validation, estimate→approved→convert in one step, PM assignment, numbering
    "smoke_worker_history_320.py",   # #320 guard — worker job history: hours by project/month/matrix, rate-on-date cost, assignments, certs, credentials, rate audit, gates
    "smoke_company_vault_318.py",     # #318 guard — company vault: vehicles CRUD + driver roster, vehicle/tax categories, file upload/serve/delete (gated, path-contained), compliance rollup
    "smoke_invoices_315.py",          # #315 guard — invoices & receivables: numbering, draft→issued→paid, payments, void, overdue math, proposal-signed deposit draft, gates
    "smoke_payroll_week_310.py",       # #310 guard — Mon–Sun payroll week: 7 dates/totals, weekend hours counted + priced, CSV, divergence window, default week
    "smoke_csuite_reset_309.py",       # #309 guard — c_suite reset-only view of client accounts; admin unchanged; strong-password suggester wired on both set-password pages
    "smoke_client_proposals_308.py",   # #308 guard — client Proposals tab: grant + matrix + org scoping, view/PDF/accept/decline, preview read-only, registry-only fields
    "smoke_proposals_307.py",          # #307 guard — proposal from estimate, send + signed link, public view/accept/decline, signature trigger, versions, gates
    "smoke_buildings_304.py",          # #304 guard — building identity, backfill de-dup, autolink, CRUD + gating, portal client view on the table
    "smoke_security_release_305.py",   # #305 guard — PIN throttle, PII gates/strip, SQL-injection close, RFI routes, estimator/vendor containment, traversal, artifact scoping, preview gate, no duplicate routes
    "smoke_perf_budget_292.py",   # #292 THE BUDGET GATE — per-surface request-count + payload ceilings over the whole census; a page that goes chatty again fails here
    "smoke_dropplan_api.py",
    "smoke_worker_lifecycle.py",
    "smoke_dcr214_lifecycle.py",
    "smoke_crud_data_integrity.py",
    "smoke_static_exposure.py",
    "smoke_no_production_data_corruption.py",
]


def wait_health(deadline: float = 45.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    suites = [a for a in sys.argv[1:] if not a.startswith("--")] or GATE
    db_url = (os.environ.get("SSC_DB_URL") or "").strip()
    if not db_url:
        print("REFUSING TO RUN: SSC_DB_URL is unset — that would target the LIVE superstars.db.\n"
              "Set it to an isolated backend (sqlite:///<copy> or postgresql://...ssc_test).")
        return 2

    env = {**os.environ, "SMOKE_BASE": BASE}
    # static-exposure launches its own server; keep it off 5151 (operator preview) by default
    env.setdefault("SMOKE_STATIC_PORT", "5152")

    # #262/#266 — an isolated SQLite copy of live inherits FK-OFF ORPHAN child rows
    # (audit_log.actor_user_id, etc.) whose users.id target was long deleted. When a smoke
    # seeds a fresh user whose auto-increment id COLLIDES with an orphan's ref, the enforced
    # FK on the copy blocks that smoke's teardown DELETE (false FK failure). clean_user_orphans
    # NULLs/DELETEs those orphans; it self-skips Postgres (cleaned during migration) and refuses
    # to run against live. Self-healing here keeps the gate deterministic across DB snapshots.
    orphan_rc = subprocess.run(
        [str(VENV_PY), str(DASH / "clean_user_orphans.py")],
        cwd=str(DASH), capture_output=True, text=True, env=env)
    print((orphan_rc.stdout or "").strip() or "[orphans] (no output)")

    logf = open(DASH / "tests" / "_gate260_srv.log", "w", encoding="utf-8")
    srv = subprocess.Popen(
        [str(VENV_PY), "-m", "waitress", "--host=127.0.0.1", f"--port={PORT}",
         "--threads=8", "server:app"],
        cwd=str(DASH), stdout=logf, stderr=subprocess.STDOUT, env=env,
    )
    try:
        if not wait_health():
            print(f"test server did not come up on {BASE}; see tests/_gate260_srv.log")
            return 2
        print(f"isolated test server up on {BASE}")
        print(f"SSC_DB_URL = {db_url}\n")

        results = []
        for s in suites:
            t = time.time()
            try:
                p = subprocess.run(
                    [str(VENV_PY), str(DASH / "tests" / s)], cwd=str(DASH),
                    capture_output=True, text=True, env=env, timeout=1200,
                )
                rc, out, err = p.returncode, p.stdout, p.stderr
            except subprocess.TimeoutExpired as e:
                rc, out, err = 124, (e.stdout or ""), "TIMEOUT"
            ok = rc == 0
            dt = time.time() - t
            results.append((s, ok, rc, dt))
            print(f"{'PASS' if ok else 'FAIL'}  {s:<42} rc={rc}  {dt:5.1f}s")
            if not ok:
                for line in (out or "").splitlines()[-35:]:
                    print("    |", line)
                for line in (err or "").splitlines()[-12:]:
                    print("   E|", line)
                print()

        npass = sum(1 for _, ok, _, _ in results if ok)
        print("\n" + "=" * 60)
        for s, ok, rc, dt in results:
            print(f"  {'PASS' if ok else 'FAIL'}  {s:<42} {dt:5.1f}s")
        print(f"=== {npass}/{len(results)} suites green ===")
        return 0 if npass == len(results) else 1
    finally:
        # tree-kill ONLY our test server (never the production :5050 server)
        subprocess.run(["cmd", "/c", f"taskkill /F /T /PID {srv.pid}"], capture_output=True)
        logf.close()


if __name__ == "__main__":
    sys.exit(main())
