#!/usr/bin/env python3
"""Create the EMPTY, synthetic pre-production schema in a fresh Postgres database.

This is deliberately not a production migration tool.  It builds an empty SQLite
snapshot from the versioned schema and migrations, then uses the project's
verified SQLite-to-Postgres migration.  No customer, worker, payroll, or project
data is read or copied.

Safety interlocks:
  * requires SSC_ENVIRONMENT=preprod;
  * requires SSC_PREPROD_FIXTURE_BOOTSTRAP_CONFIRM=CREATE_EMPTY_FIXTURE_SCHEMA;
  * requires an SSC_DB_URL Postgres URL;
  * refuses a target containing application tables other than the harmless
    boot-time support tables created by the web service.

Run this only as a Render one-off command after the preprod service is deployed:
  python preprod_fixture_bootstrap.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIRM_VALUE = "CREATE_EMPTY_FIXTURE_SCHEMA"
ALLOWED_PRE_BOOTSTRAP_TABLES = {
    # server.py's idempotent boot ensures can create these three tables before
    # the complete application schema has been migrated.  They are empty on a
    # fresh Render database and are safely replaced by the fixture migration.
    "worker_app_session", "drawing_set", "drawing_sheet",
    # password_reset.register() creates its token ledger during app import.
    # It contains no tokens or user data on a fresh database and is replaced by
    # the fixture migration, just like the other boot-time support tables.
    "password_reset_token",
}

# Older migrations predate db_layer and therefore require a local SQLite file
# named superstars.db.  These are schema/catalog migrations only; historical
# data-repair scripts and project-specific seeds are intentionally excluded.
LEGACY_MIGRATIONS = (
    "apply_dcr_aligned_schema.py", "apply_visitors_schema.py",
    "apply_cof_schema.py", "apply_cof_eligibility_fix.py",
    "apply_company_id_schema.py", "apply_cof_revision_schema.py",
    "apply_dcr_sequence_schema.py", "apply_worker_id_schema.py",
    "apply_project_type_schema.py", "apply_rfi_log_phase2.py",
    "apply_cert_library_replace.py", "apply_worker_archive_schema.py",
    "apply_cof_override_schema.py", "apply_dropplan_schema.py",
    "apply_spec_products_seed.py", "apply_no_work_day_schema.py",
    "apply_dcr_stale_schema.py", "apply_blank_forms_seed.py",
    "apply_auth_schema.py", "apply_signage_templates_seed.py",
    "apply_toolbox_talks_seed.py", "apply_worker_rates_schema.py",
    "apply_labor_rates_v2_schema.py", "apply_dropplan_patchmodel.py",
    "apply_dashboard_layouts_schema.py", "apply_expenses_schema.py",
    "apply_project_docs_schema.py", "apply_field_photos_schema.py",
    "apply_lookahead_schema.py", "apply_auth_roles_257.py",
    "apply_is_system_258.py",
    # #305 — the roster views (v_worker_roster / v_active_workers) were never in this
    # list, so every fixture DB (preprod included) booted without them: the Workforce
    # tab said "No workers yet" and /api/employees/<id> 500ed. server.py now also
    # ensures them at boot for databases that already exist.
    "apply_roster_views_schema.py",
    # #305 — worker-intake columns (certifications.card_number & co.): the certifications
    # endpoints 500ed on every fixture DB without them.
    "apply_worker_intake_schema.py",
    # #310 — project_assignments (the payroll worker pool) was never in this list, so a
    # fixture DB had no assignments table and /api/payroll/hours 500ed on it.
    "apply_assignments_schema.py",
)

DUAL_BACKEND_MIGRATIONS = (
    "apply_google_sso_261.py", "apply_pm_assignment_263.py",
    "apply_item_visibility_264.py", "apply_client_grants_269.py",
    "apply_crm_266.py", "apply_materials_272.py",
    "apply_material_consumption_272b.py", "apply_docs_photos_271.py",
    "apply_estimates_273.py", "apply_ira_274.py",
    "apply_estimating_276.py", "apply_walkthroughs_277.py",
    "apply_expenses_278.py", "apply_ui_version_279.py",
    "apply_elevation_280.py", "apply_public_hardening_289.py",
    "apply_drawing_author_293.py", "apply_photo_reassign_294.py",
    "apply_hours_edit_295.py", "apply_worker_session_security_296.py",
    "apply_buildings_304.py",
    "apply_proposals_307.py",
    "apply_invoices_315.py",
    "apply_company_vault_318.py",
    "apply_assignments_322.py",
    "apply_project_site_325.py",
    "apply_doc_packages_327.py",
)


def _require_safe_environment() -> str:
    if os.environ.get("SSC_ENVIRONMENT") != "preprod":
        raise SystemExit("Refusing: SSC_ENVIRONMENT must be exactly 'preprod'.")
    if os.environ.get("SSC_PREPROD_FIXTURE_BOOTSTRAP_CONFIRM") != CONFIRM_VALUE:
        raise SystemExit("Refusing: explicit empty-fixture confirmation is required.")
    url = (os.environ.get("SSC_DB_URL") or "").strip()
    if not url.startswith(("postgres://", "postgresql://")):
        raise SystemExit("Refusing: SSC_DB_URL must be a Postgres URL.")
    return url


def _assert_target_is_blank(url: str) -> None:
    import psycopg
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE'"
            )
            tables = {row[0] for row in cur.fetchall()}
    unexpected = tables - ALLOWED_PRE_BOOTSTRAP_TABLES
    if unexpected:
        raise SystemExit(
            "Refusing: target already has application tables; use a separate, fresh "
            "pre-production database."
        )


def _run(script_dir: Path, script: str, env: dict[str, str]) -> None:
    print(f"[fixture] {script}", flush=True)
    subprocess.run([sys.executable, script], cwd=script_dir, env=env, check=True)


def _build_empty_sqlite_snapshot(work: Path) -> Path:
    source = work / "source"
    shutil.copytree(
        ROOT, source,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "superstars.db*"),
    )
    snapshot = source / "superstars.db"
    with sqlite3.connect(snapshot) as conn:
        conn.executescript((source / "schema.sql").read_text(encoding="utf-8"))

    sqlite_env = os.environ.copy()
    sqlite_env.pop("SSC_DB_URL", None)
    # The source snapshot lives under `source/`.  Do not inherit Render's
    # persistent-data root while composing it: newer dual-backend migrations
    # use db_layer and would otherwise target /var/data/superstars.db instead
    # of this isolated temporary snapshot.
    sqlite_env.pop("SSC_DATA_ROOT", None)
    # apply_is_system_258 normally requires one real admin; the deliberately
    # empty preprod snapshot has none until preprod_seed_review_admin.py runs
    # after migration.  This scoped subprocess-only flag preserves its
    # fail-closed behavior for every normal migration invocation.
    sqlite_env["SSC_ALLOW_EMPTY_PREPROD_FIXTURE"] = "1"
    for script in LEGACY_MIGRATIONS + DUAL_BACKEND_MIGRATIONS:
        _run(source, script, sqlite_env)
    return snapshot


def _migrate_to_postgres(snapshot: Path, url: str) -> None:
    # --reset is safe only because _assert_target_is_blank ran first.  It removes
    # boot-time support tables so the migrated schema is internally consistent.
    subprocess.run(
        [sys.executable, str(ROOT / "migrate_sqlite_to_pg_259.py"), str(snapshot), url, "--reset"],
        cwd=ROOT, check=True,
    )


def _verify_target(url: str) -> None:
    import psycopg
    required = {"users", "projects", "employees", "worker_app_session", "crm_organization"}
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE'"
            )
            tables = {row[0] for row in cur.fetchall()}
    missing = required - tables
    if missing:
        raise SystemExit(f"Bootstrap verification failed; missing tables: {sorted(missing)}")
    print(f"[fixture] verified {len(tables)} Postgres tables; data rows are synthetic/empty.")


def main() -> int:
    url = _require_safe_environment()
    _assert_target_is_blank(url)
    with tempfile.TemporaryDirectory(prefix="ssc-preprod-fixture-") as temp:
        snapshot = _build_empty_sqlite_snapshot(Path(temp))
        _migrate_to_postgres(snapshot, url)
    _verify_target(url)
    print("[fixture] complete. Remove the confirmation environment variable now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
