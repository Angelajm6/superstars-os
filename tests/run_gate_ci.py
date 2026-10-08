#!/usr/bin/env python3
"""#306 — the CI gate. Cross-platform (macOS / Linux / Windows), one command:

    python tests/run_gate_ci.py --backend sqlite      # each suite builds its own isolated SQLite
    python tests/run_gate_ci.py --backend postgres    # SSC_DB_URL must point at a fixture-built Postgres

What it runs, in order — any failure fails the gate:
  1. byte-compile every *.py in the repo
  2. STRICT BOOT: `import server` with SSC_STRICT_ROUTES=1 (duplicate routes refuse to start)
     + the migration ledger reports nothing pending
  3. static contracts:  smoke_http_security_297, smoke_login_experience_298,
                        smoke_client_building_hub_299, smoke_password_reset_300
  4. behavioral suites: smoke_password_reset_flow_301, smoke_company_portal_302,
                        smoke_buildings_304, smoke_security_release_305, smoke_migrations_306
  5. design-conventions guard against a live server on 127.0.0.1:<GATE_PORT>

The #260 gate (`run_gate_260.py`) stays the operator's full census on the Windows
workstation; this is the subset every pull request must pass on GitHub.
"""
from __future__ import annotations

import argparse
import os
import py_compile
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
STATIC = ["smoke_http_security_297.py", "smoke_login_experience_298.py",
          "smoke_client_building_hub_299.py", "smoke_password_reset_300.py",
          "smoke_request_logging.py", "smoke_secret_box.py", "smoke_runtime_seams.py", "smoke_csrf_guard.py", "smoke_company_dashboard_links.py", "smoke_login_next_query.py", "smoke_edit_projects_clients.py", "smoke_worker_photos.py", "smoke_shell_titles_pin.py", "smoke_team_items.py", "smoke_team_home_2b.py", "smoke_gcal_sync.py", "smoke_gmail_suggest.py", "smoke_usage_footprint.py"]
BEHAVIORAL = ["smoke_password_reset_flow_301.py", "smoke_company_portal_302.py",
              "smoke_buildings_304.py", "smoke_security_release_305.py", "smoke_migrations_306.py",
              "smoke_proposals_307.py", "smoke_client_proposals_308.py", "smoke_csuite_reset_309.py",
              "smoke_payroll_week_310.py", "smoke_invoices_315.py", "smoke_company_vault_318.py", "smoke_worker_history_320.py", "smoke_project_onboard_324.py", "smoke_crew_sites_325.py", "smoke_project_payments_326.py", "smoke_doc_package_327.py", "smoke_nav_clarity_328.py", "smoke_aia_billing_317.py", "smoke_payables_319.py", "smoke_portal_projects_330.py", "smoke_regions_331.py", "smoke_calendar_332.py", "smoke_calendar_plus_333.py", "smoke_project_dates_334.py", "smoke_building3d_336.py", "smoke_job_sites_337.py", "smoke_dashboard_week_338.py", "smoke_one_home_339.py", "smoke_dcr_numbers_344.py", "smoke_workforce_archive_345.py", "smoke_worker_id_cleanup_346.py", "smoke_column_repair_352.py", "smoke_photoreal_3d_353.py", "smoke_fleet_355.py", "smoke_billing_type_356.py", "smoke_project_contacts_357.py", "smoke_job_dashboard_358.py", "smoke_layout_rail_359.py", "smoke_client_portal_tab_360.py", "smoke_project_modules.py", "smoke_projects_flow.py", "smoke_job_v2_preview.py"]
PORT = int(os.environ.get("GATE_PORT", "5434"))

results: list = []


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""), flush=True)


def build_sqlite_fixture(tmp: Path) -> str:
    """Same recipe as the behavioral suites: schema.sql + the fixture migration lists."""
    import preprod_fixture_bootstrap as pfb
    src = tmp / "source"
    shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", "superstars.db*", "venv", "node_modules", "worker_records", "data_room", ".github"))
    db = src / "superstars.db"
    with sqlite3.connect(db) as conn:
        conn.executescript((src / "schema.sql").read_text(encoding="utf-8"))
    env = dict(os.environ); env.pop("SSC_DB_URL", None); env.pop("SSC_DATA_ROOT", None)
    env["SSC_ALLOW_EMPTY_PREPROD_FIXTURE"] = "1"
    for script in pfb.LEGACY_MIGRATIONS + pfb.DUAL_BACKEND_MIGRATIONS:
        subprocess.run([PY, script], cwd=src, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"sqlite:{db}"


def run(cmd: list, env: dict, timeout: int = 900) -> tuple:
    res = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    tail = (res.stdout + "\n" + res.stderr).strip().splitlines()
    return res.returncode, "\n".join(tail[-12:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["sqlite", "postgres"], default="sqlite")
    ap.add_argument("--skip-design", action="store_true")
    args = ap.parse_args()
    sys.path.insert(0, str(ROOT))

    tmp = Path(tempfile.mkdtemp(prefix="ssc_gate_ci_"))
    data_root = tmp / "dataroot"; data_root.mkdir()
    base_env = dict(os.environ)
    base_env["SSC_DATA_ROOT"] = str(data_root)
    base_env["PYTHONDONTWRITEBYTECODE"] = "1"
    base_env.pop("SSC_TRUSTED_PROXY", None)
    if args.backend == "postgres":
        url = (os.environ.get("SSC_DB_URL") or "").strip()
        if not url.startswith(("postgres://", "postgresql://")):
            print("--backend postgres needs SSC_DB_URL pointing at a fixture-built Postgres"); return 2
        boot_url = url
    else:
        base_env.pop("SSC_DB_URL", None)          # suites build their own isolated SQLite
        print("building the strict-boot SQLite fixture…", flush=True)
        boot_url = build_sqlite_fixture(tmp)
    print(f"gate backend: {args.backend}", flush=True)

    # 1. compile
    bad = []
    for p in ROOT.glob("*.py"):
        try:
            py_compile.compile(str(p), doraise=True)
        except py_compile.PyCompileError as e:
            bad.append(f"{p.name}: {e.msg.splitlines()[0]}")
    for p in (ROOT / "tests").glob("*.py"):
        try:
            py_compile.compile(str(p), doraise=True)
        except py_compile.PyCompileError as e:
            bad.append(f"tests/{p.name}: {e.msg.splitlines()[0]}")
    step("compile", not bad, "; ".join(bad[:3]))

    # 2. strict boot + ledger clean
    env = dict(base_env); env["SSC_DB_URL"] = boot_url; env["SSC_STRICT_ROUTES"] = "1"; env["SSC_AUTO_MIGRATE"] = "1"
    code, out = run([PY, "-c", "import server, migrations, db_layer\n"
                                "c = db_layer.connect(); st = migrations.status(c); c.close()\n"
                                "print('dups', len(server._duplicate_routes()), 'pending', st['pending'])\n"
                                "raise SystemExit(0 if not st['pending'] else 3)"], env, timeout=300)
    step("strict boot + migration ledger clean", code == 0, out)

    # 3 + 4. suites
    for name in STATIC + BEHAVIORAL:
        env = dict(base_env)
        code, out = run([PY, f"tests/{name}"], env)
        step(name, code == 0, out)

    # 5. design guard against a live server
    if not args.skip_design:
        env = dict(base_env); env["SSC_DB_URL"] = boot_url; env["PORT"] = str(PORT); env["APP_BASE_URL"] = f"http://127.0.0.1:{PORT}"
        srv = subprocess.Popen([PY, "server.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        try:
            up = False
            for _ in range(60):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2) as r:
                        if r.status == 200:
                            up = True; break
                except Exception:
                    time.sleep(1)
            if not up:
                step("design guard", False, "server never answered /api/health")
            else:
                genv = dict(env); genv["SMOKE_BASE"] = f"http://127.0.0.1:{PORT}"
                code, out = run([PY, "tests/smoke_design_conventions.py"], genv, timeout=600)
                step("smoke_design_conventions.py", code == 0, out)
        finally:
            srv.terminate()
            try:
                srv.wait(timeout=10)
            except subprocess.TimeoutExpired:
                srv.kill()

    shutil.rmtree(tmp, ignore_errors=True)
    failed = [r for r in results if not r[1]]
    print(f"\n== GATE ({args.backend}): {len(results) - len(failed)} PASS / {len(failed)} FAIL ==")
    for name, _, detail in failed:
        print(f"FAILED {name}\n{detail}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
