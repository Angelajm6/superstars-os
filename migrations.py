#!/usr/bin/env python3
"""#306 — the migration ledger.

Until now nothing recorded which `apply_*` scripts had run against a given database:
the operator kept a checklist, fixture databases silently missed scripts (#305 found
two), and every module that needed a table on Render grew its own boot-time
"ensure". This module gives the app ONE ordered list and ONE table:

    schema_migrations(name PRIMARY KEY, checksum, applied_at, note)

* The ordered list is `preprod_fixture_bootstrap.DUAL_BACKEND_MIGRATIONS` — the
  scripts that honor SSC_DB_URL and therefore run on SQLite AND Postgres. (The
  LEGACY list is SQLite-only and only ever runs inside the fixture builder.)
  A new migration = a new dual-backend script appended to that tuple. Nothing
  else to remember.
* `pending(conn)` = names in the list with no ledger row. `apply(names)` runs
  each script as a subprocess with the current SSC_DB_URL (exactly how the fixture
  builder and the operator run them) and records name + sha256 + timestamp.
* Existing databases predate the ledger. The first time the ledger table is
  created on a database that already carries the application schema, every
  migration in the list is recorded as `baseline` — those databases were
  migrated by hand and by the fixture builder, and the modules that landed after
  #290 ensure their own tables at boot. From then on, only NEW entries are pending.
* Boot (server.py): ensure the ledger, baseline if needed, log what is pending.
  With SSC_AUTO_MIGRATE=1 (preprod) pending migrations are applied at boot;
  without it (production, until the C-suite gate says otherwise) they are only
  reported — in the log and in /api/health as `migrations_pending`.

CLI:
    python migrations.py status          # applied / pending for SSC_DB_URL
    python migrations.py apply           # run + record every pending migration
    python migrations.py apply --dry-run # list what would run
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import db_layer

ROOT = Path(__file__).resolve().parent
log = logging.getLogger(__name__)


def migration_names() -> tuple:
    import preprod_fixture_bootstrap as pfb
    return tuple(pfb.DUAL_BACKEND_MIGRATIONS)


# ---- the sentinel that says "this database already carries the app schema" ----
_BASELINE_SENTINELS = ("crm_organization", "worker_app_session", "estimate")


def _table_exists(conn, name: str) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute(
            "SELECT 1 AS x FROM information_schema.tables WHERE table_name = ?", (name,)).fetchone())
    return bool(conn.execute(
        "SELECT 1 AS x FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)).fetchone())


def checksum(name: str) -> str:
    p = ROOT / name
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def ensure_ledger(conn) -> bool:
    """Create the table. Returns True when it was created by THIS call."""
    existed = _table_exists(conn, "schema_migrations")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  name       TEXT PRIMARY KEY,"
        "  checksum   TEXT,"
        "  applied_at TEXT NOT NULL,"
        "  note       TEXT"
        ")")
    conn.commit()
    return not existed


def applied(conn) -> dict:
    return {r["name"]: dict(r) for r in conn.execute(
        "SELECT name, checksum, applied_at, note FROM schema_migrations").fetchall()}


def pending(conn) -> list:
    done = applied(conn)
    return [n for n in migration_names() if n not in done]


def record(conn, name: str, note: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO schema_migrations (name, checksum, applied_at, note) VALUES (?, ?, ?, ?)",
        (name, checksum(name), datetime.now().isoformat(timespec="seconds"), note))
    conn.commit()


def baseline_if_needed(conn, created_now: bool) -> int:
    """On a database that already carries the app schema (sentinel tables exist),
    record every known migration as `baseline` the first time the ledger appears.
    Returns how many rows were written."""
    if not created_now:
        return 0
    if not all(_table_exists(conn, t) for t in _BASELINE_SENTINELS):
        return 0
    n = 0
    for name in migration_names():
        record(conn, name, note="baseline")
        n += 1
    return n


def apply(names: Iterable[str], *, dry_run: bool = False, env: Optional[dict] = None) -> list:
    """Run each script (subprocess, cwd=repo, current SSC_DB_URL) and record it.
    Stops at the first failure and raises. Returns the names applied."""
    names = list(names)
    if dry_run:
        return names
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    done = []
    for name in names:
        script = ROOT / name
        if not script.exists():
            raise FileNotFoundError(f"migration script missing: {name}")
        res = subprocess.run([sys.executable, str(script)], cwd=ROOT, env=run_env,
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"migration {name} failed (exit {res.returncode}):\n"
                               f"{(res.stderr or res.stdout)[-2000:]}")
        conn = db_layer.connect(pragma_fk=True)
        try:
            ensure_ledger(conn)
            if name not in applied(conn):
                record(conn, name)
        finally:
            conn.close()
        done.append(name)
        log.info("migration applied: %s", name)
    return done


def status(conn) -> dict:
    created = ensure_ledger(conn)
    baselined = baseline_if_needed(conn, created)
    done = applied(conn)
    pend = [n for n in migration_names() if n not in done]
    drift = [n for n, r in done.items() if r.get("checksum") and checksum(n) and r["checksum"] != checksum(n)]
    return {"ledger_created": created, "baselined": baselined, "applied": len(done),
            "pending": pend, "checksum_drift": drift}


def pending_count() -> int:
    """For /api/health. -1 when the ledger cannot be read (never raises)."""
    try:
        conn = db_layer.connect(pragma_fk=True)
        try:
            if not _table_exists(conn, "schema_migrations"):
                return len(migration_names())
            return len(pending(conn))
        finally:
            conn.close()
    except Exception:
        return -1


# #337 — migrations whose MODULE ensures the same schema at boot. When the objects
# exist, the ledger records the script as applied (note "boot-ensured") instead of
# reporting it pending forever on a database that never ran the script by hand.
# "table" or "table.column".
BOOT_ENSURED = {
    "apply_aia_317.py": ("pay_application", "pay_application_line", "billing_sov"),
    "apply_payables_319.py": ("payable",),
    "apply_project_dates_334.py": ("projects.start_date", "projects.target_completion"),
    "apply_building_geometry_336.py": ("building_geometry",),
    "apply_invoices_315.py": ("invoice", "invoice_payment"),
    "apply_company_vault_318.py": ("vehicle", "company_document_file"),
    "apply_assignments_322.py": ("project_assignments",),
    "apply_project_site_325.py": ("projects.site_lat", "projects.geofence_m"),
    "apply_doc_packages_327.py": ("document_package", "document_package_item"),
    "apply_buildings_304.py": ("building",),
    "apply_proposals_307.py": ("proposal",),
}


def _object_exists(conn, spec: str) -> bool:
    if "." in spec:
        table, column = spec.split(".", 1)
        if db_layer.is_postgres():
            return bool(conn.execute("SELECT 1 AS x FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
                                     (table, column)).fetchone())
        return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())
    return _table_exists(conn, spec)


def reconcile_boot_ensured(conn, pending_names: list) -> list:
    """Record every pending migration whose boot-ensured objects all exist. Returns the names."""
    done = []
    for name in pending_names:
        specs = BOOT_ENSURED.get(name)
        if not specs:
            continue
        try:
            if all(_object_exists(conn, s) for s in specs):
                record(conn, name, note="boot-ensured")
                done.append(name)
        except Exception as e:
            log.info("#337 reconcile skipped %s: %s", name, e)
    return done


def boot(auto: bool) -> dict:
    """Called by server.py at import. Never raises."""
    out = {"pending": [], "applied_now": [], "error": None, "reconciled": []}
    try:
        conn = db_layer.connect(pragma_fk=True)
        try:
            st = status(conn)
            if st["pending"]:
                out["reconciled"] = reconcile_boot_ensured(conn, st["pending"])
                if out["reconciled"]:
                    log.info("#337 ledger: recorded %d boot-ensured migration(s): %s", len(out["reconciled"]), out["reconciled"])
                    st = status(conn)
        finally:
            conn.close()
        if st["baselined"]:
            log.info("#306 migration ledger created; %d migration(s) recorded as baseline", st["baselined"])
        if st["checksum_drift"]:
            log.warning("#306 migration scripts changed after they were applied: %s", st["checksum_drift"])
        out["pending"] = st["pending"]
        if st["pending"] and auto:
            out["applied_now"] = apply(st["pending"])
            log.info("#306 auto-applied %d migration(s): %s", len(out["applied_now"]), out["applied_now"])
            out["pending"] = []
        elif st["pending"]:
            log.warning("#306 %d pending migration(s) — set SSC_AUTO_MIGRATE=1 or run `python migrations.py apply`: %s",
                        len(st["pending"]), st["pending"])
    except Exception as e:  # the ledger must never stop the app from serving
        out["error"] = f"{type(e).__name__}: {e}"
        log.warning("#306 migration ledger unavailable: %s", out["error"])
    return out


def _cli(argv: list) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    conn = db_layer.connect(pragma_fk=True)
    try:
        st = status(conn)
    finally:
        conn.close()
    backend = "postgres" if db_layer.is_postgres() else "sqlite"
    if cmd == "status":
        print(f"[#306] backend={backend} applied={st['applied']} pending={len(st['pending'])}"
              + (f" baselined={st['baselined']}" if st["baselined"] else ""))
        for n in st["pending"]:
            print(f"  pending  {n}")
        for n in st["checksum_drift"]:
            print(f"  DRIFT    {n} (script changed after it was applied)")
        return 0
    if cmd == "apply":
        dry = "--dry-run" in argv
        names = apply(st["pending"], dry_run=dry)
        for n in names:
            print(f"  {'would apply' if dry else 'applied'}  {n}")
        if not names:
            print("[#306] nothing pending")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(_cli(sys.argv))
