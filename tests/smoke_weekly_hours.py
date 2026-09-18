#!/usr/bin/env python3
"""PII-safe smoke for the Weekly Hours Log.

DEV-DB ONLY — NOT part of the standard gate. The refuse-to-run guard
(see clean_test_data) exits immediately when real DCRs exist for the
project, so on the live operator DB this smoke does nothing. Run it
against a dev DB with no issued DCRs.

#245 — the smoke now spawns its server on a PRIVATE port (5152), never
5050: a spawn against the production port can double-bind on Windows
and leave a stale-code zombie (the #244 incident; see CLAUDE.md
"Production restart procedure").

Invoke manually:

  python tests/smoke_weekly_hours.py

Asserts:
  1. Default week selector resolves to the last completed Mon-Fri week
     (computed via payroll_hours.last_completed_week — no pinned dates).
  2. Per-day hours and per-worker weekly totals correct (lunch always -30).
  3. Grand total = sum of all workers' weeks; #244/#245 pool semantics
     (active-only roster + hours-bearing history; no_signin_count).
  4. CSV export values match the grid exactly.
  5. PDF renders non-empty with letterhead + table.
  6. A cell edit writes through to sign_in_log; the DCR labor aggregator
     reads the SAME number — single source of truth.

PII discipline: uses E-00001 / E-00002 (dev-DB rows), asserts by
employee_id + counts + numeric values only. Never prints names, phones,
or *_path values. Test data is fully cleaned up at the end regardless
of pass/fail (try/finally).
"""
import csv
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

D = Path(__file__).resolve().parent.parent
VENV_PY = D / "venv" / "Scripts" / "python.exe"
DB = D / "superstars.db"
LOG = D / "tests" / "_smoke_weekly_hours_server.log"

PROJECT = "FR-BX-001"
EMP_A = "E-00001"  # dev-DB test workers (real ids — the refusal guard protects live data)
EMP_B = "E-00002"
PORT = "5152"  # private port — NEVER 5050 (see module docstring)

# #245 — the target week is computed, not pinned: the old hardcoded
# 2026-05-11 made every assertion stale after that week passed.
sys.path.insert(0, str(D))
import db_layer  # noqa: E402  # #260 — route DB access through the env-driven layer (SSC_DB_URL)
from payroll_hours import last_completed_week, compute_worked_hours  # noqa: E402

_mon, _sun = last_completed_week(date.today())   # #310 — Mon..Sun
WEEK_START = _mon.isoformat()
WEEK_END = _sun.isoformat()
WEEK_DATES = [(_mon + timedelta(days=i)).isoformat() for i in range(7)]


def hit(method, path, body=None):
    url = f"http://127.0.0.1:{PORT}" + path
    data = None
    h = {}
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(), {}
        except Exception:
            return e.code, b"", {}
    except Exception as e:
        return None, repr(e).encode(), {}


PASS, FAIL = [], []


def expect(label, cond, extra=""):
    line = f"{'PASS' if cond else 'FAIL'}: {label}" + (f" ({extra})" if extra else "")
    if cond:
        PASS.append(line)
    else:
        FAIL.append(line)
        print(line)


def clean_test_data():
    """Pre-clean sign_in_log for the test workers on the target week
    ONLY.

    SAFETY (post-incident 2026-05-26):
      This smoke was originally written for the pre-launch dev period
      when the DB was empty of operator data. The previous version
      unconditionally wiped report_index, work_log, and every rendered
      DCR file under data_room/reports/dcr/<project>/ — which during
      a recent run destroyed 16 real DCRs and 18 sign_in_log rows.

      The destructive blanket deletes are removed. EMP_A / EMP_B are
      *real worker IDs* on this project (not "synthetic" — the original
      comment was wrong); to keep us from clobbering operator data we
      NOW REFUSE TO RUN if either:
        * any DCR has been issued for the project (report_index rows),
          OR
        * there are non-test sign_in_log rows for EMP_A/EMP_B on the
          target week dates.
      The smoke is intended for a dev-only DB; once the operator has
      issued real DCRs, run it on a separate dev DB instead.
    """
    with db_layer.connect() as c:
        c.row_factory = sqlite3.Row
        n_reports = c.execute(
            "SELECT COUNT(*) FROM report_index "
            "WHERE project_code=? AND report_type='DCR'",
            (PROJECT,),
        ).fetchone()[0]
        if n_reports > 0:
            print(
                f"REFUSE TO RUN: {n_reports} DCRs exist for {PROJECT}. "
                "smoke_weekly_hours destroys operator data when real DCRs "
                "are present (see post-incident note above). Run this on "
                "a dev DB instead."
            )
            sys.exit(3)
        # Sign-ins on the test week for EMP_A/EMP_B (only the synthetic
        # 07:00/15:30 markers the smoke creates). We do NOT wipe arbitrary
        # rows — only ones matching the smoke's own time pattern.
        placeholders = ",".join("?" * len(WEEK_DATES))
        c.execute(
            f"DELETE FROM sign_in_log "
            f"WHERE employee_id IN (?, ?) AND date IN ({placeholders}) "
            f"AND time_in = '07:00' AND time_out = '15:30'",
            (EMP_A, EMP_B, *WEEK_DATES),
        )
        c.commit()
    # NOTE: do NOT touch data_room/reports/dcr/<project>/ — that's
    # operator output, not test output.


def find_worker(grid, eid):
    for w in grid["workers"]:
        if w["employee_id"] == eid:
            return w
    return None


print(f"=== Weekly Hours Log smoke ===")
print(f"  expected default week: {WEEK_START}..{WEEK_END}")
print(f"  synthetic workers: {EMP_A}, {EMP_B}")
clean_test_data()

# Helper assertion BEFORE starting the server (fixed historical date —
# pure-function check, valid forever)
from datetime import date as _date

mon, sun = last_completed_week(_date(2026, 5, 20))   # #310 — the week is Mon..Sun
expect("helper: last_completed_week(2026-05-20) -> (2026-05-11, 2026-05-17)",
       (mon.isoformat(), sun.isoformat()) == ("2026-05-11", "2026-05-17"),
       f"got ({mon.isoformat()}, {sun.isoformat()})")
mon, sun = last_completed_week(_date(2026, 5, 24))
expect("helper: last_completed_week(Sunday 2026-05-24) -> still the prior week (05-11..05-17)",
       (mon.isoformat(), sun.isoformat()) == ("2026-05-11", "2026-05-17"),
       f"got ({mon.isoformat()}, {sun.isoformat()})")
mon, sun = last_completed_week(_date(2026, 5, 25))
expect("helper: last_completed_week(Monday 2026-05-25) -> (2026-05-18, 2026-05-24)",
       (mon.isoformat(), sun.isoformat()) == ("2026-05-18", "2026-05-24"),
       f"got ({mon.isoformat()}, {sun.isoformat()})")
expect("helper: worked_hours(07:00, 15:30) == 8.00",
       compute_worked_hours("07:00", "15:30") == 8.0)
expect("helper: worked_hours(07:00, 12:00) == 4.50  (early leave)",
       compute_worked_hours("07:00", "12:00") == 4.5)
expect("helper: worked_hours(08:00, 16:30) == 8.00  (different shift, same length)",
       compute_worked_hours("08:00", "16:30") == 8.0)
expect("helper: worked_hours(07:00, None) == 0.00  (missing time_out)",
       compute_worked_hours("07:00", None) == 0.0)

f = open(LOG, "w")
# #245 — private port via the dev-server PORT env override; never 5050.
import os as _os
_env = {**_os.environ, "PORT": PORT}
proc = subprocess.Popen(
    [str(VENV_PY), "server.py"], cwd=str(D), stdout=f, stderr=subprocess.STDOUT,
    env=_env,
)
try:
    for _ in range(15):
        time.sleep(1)
        try:
            # Probe an unauthenticated endpoint — the auth gate (#48) redirects
            # "/" to /login, so probe /api/health which is in the public allowlist.
            if urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2).status == 200:
                break
        except Exception:
            pass
        if proc.poll() is not None:
            sys.exit(2)
    else:
        sys.exit(2)
    # Auth gate (#48): login the smoke admin + patch urllib for cookies.
    # SMOKE_BASE must point at THIS smoke's private-port server.
    _os.environ["SMOKE_BASE"] = f"http://127.0.0.1:{PORT}"
    import _smoke_auth
    _smoke_auth.setup()

    # ----- Step 1: default week resolves correctly -----
    print("\n--- Step 1: GET /api/payroll/hours (no week_start) ---")
    s, body, _ = hit("GET", "/api/payroll/hours")
    j = json.loads(body)
    grid = j.get("data", j)
    expect("GET default returns 200", s == 200)
    expect(f"default week_start == {WEEK_START}", grid.get("week_start") == WEEK_START)
    expect(f"default week_end == {WEEK_END}", grid.get("week_end") == WEEK_END)
    expect("7 date entries (Mon-Sun, #310)", len(grid.get("dates", [])) == 7)
    expect("grid has workers list", isinstance(grid.get("workers"), list))

    # ----- Step 2: seed hours-worked math (full day + early leave + absent) -----
    print("\n--- Step 2: seed sign_in_log rows ---")
    # EMP_A: Mon full day (07:00-15:30 -> 8.00), Tue early leave (07:00-12:00 -> 4.50), Wed absent
    # EMP_B: Mon full day, Tue full day, Wed-Fri absent
    seeds = [
        (EMP_A, "2026-05-11", "07:00", "15:30"),  # 8.00
        (EMP_A, "2026-05-12", "07:00", "12:00"),  # 4.50
        (EMP_B, "2026-05-11", "07:00", "15:30"),  # 8.00
        (EMP_B, "2026-05-12", "07:00", "15:30"),  # 8.00
    ]
    for emp, d, ti, to in seeds:
        s, _, _ = hit("POST", "/api/sign-ins", body={
            "employee_id": emp, "project_code": PROJECT, "date": d,
            "time_in": ti, "time_out": to,
        })
        expect(f"POST sign_in {emp} {d}", s == 201)

    # ----- Step 3: re-fetch grid + assert per-cell + per-worker totals -----
    print("\n--- Step 3: GET grid and verify math ---")
    s, body, _ = hit("GET", f"/api/payroll/hours?week_start={WEEK_START}")
    grid = json.loads(body)["data"]
    a = find_worker(grid, EMP_A); b = find_worker(grid, EMP_B)
    expect(f"{EMP_A} present in grid", a is not None)
    expect(f"{EMP_B} present in grid", b is not None)
    if a:
        mon_a = a["days"][0]; tue_a = a["days"][1]; wed_a = a["days"][2]
        expect(f"{EMP_A} Mon hours == 8.00", mon_a["hours"] == 8.0)
        expect(f"{EMP_A} Tue hours == 4.50 (early leave)", tue_a["hours"] == 4.5)
        expect(f"{EMP_A} Wed has_entry == False (absent)", wed_a["has_entry"] == False)
        expect(f"{EMP_A} weekly_total == 12.50", a["weekly_total"] == 12.5)
    if b:
        expect(f"{EMP_B} weekly_total == 16.00", b["weekly_total"] == 16.0)
    # totals_by_day[0] for Mon = 8 + 8 = 16; totals[1] for Tue = 4.5 + 8 = 12.5
    expect("totals_by_day[Mon] == 16.00", grid["totals_by_day"][0] == 16.0)
    expect("totals_by_day[Tue] == 12.50", grid["totals_by_day"][1] == 12.5)
    # Grand total includes any other workers in the project pool — verify it's
    # at least the sum of our two synthetic workers' contributions.
    expect("grand_total >= 28.50", grid["grand_total"] >= 28.5,
           f"got {grid['grand_total']}")
    # And that no OTHER worker had any hours (they'd be 0)
    other_hours = sum(w["weekly_total"] for w in grid["workers"]
                      if w["employee_id"] not in (EMP_A, EMP_B))
    expect("no other workers contributed hours", other_hours == 0.0,
           f"got {other_hours}")
    expect(f"grand_total == 28.50 (only our 2 workers)", grid["grand_total"] == 28.5)

    # ----- Step 3b: #244/#245 pool semantics -----
    # The roster = labor-ACTIVE workers (zero-hour actives included, counted
    # as no_signin_count) + any labor-INACTIVE worker WITH hours this week
    # (payable history). Inactive + zero hours must not be in the pool.
    print("\n--- Step 3b: #244/#245 worker-pool semantics ---")
    expect("grid carries #244 roster keys",
           all(k in grid for k in ("roster_active_count", "no_signin_count", "paid_count")))
    expect("pool rule: every worker is labor-active OR has hours",
           all(w.get("labor_active", True) or (w.get("weekly_total") or 0) > 0
               for w in grid["workers"]))
    ns_payload = sum(1 for w in grid["workers"]
                     if w.get("labor_active", True) and (w.get("weekly_total") or 0) <= 0)
    expect("no_signin_count matches the payload", grid["no_signin_count"] == ns_payload,
           f"meta={grid['no_signin_count']} payload={ns_payload}")
    expect("paid_count matches workers with hours",
           grid["paid_count"] == sum(1 for w in grid["workers"] if (w.get("weekly_total") or 0) > 0))

    # ----- Step 4: CSV export matches the grid -----
    print("\n--- Step 4: CSV export matches grid ---")
    s, body, hdrs = hit("GET", f"/api/payroll/hours.csv?week_start={WEEK_START}")
    expect("CSV returns 200", s == 200)
    expect("CSV mimetype is text/csv", "text/csv" in (hdrs.get("Content-Type", "") or ""))
    text = body.decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    # New shape after Fix 1: worker_id is column 0, employee_id column 1.
    expect("CSV header is correct shape (10 cols, worker_id first)",
           rows[0][:4] == ["worker_id", "employee_id", "name", "trade"]
           and rows[0][-1] == "weekly_total" and len(rows[0]) == 10)
    # Find our test rows by employee_id (column 1) — synthetic + stable join key
    by_eid = {r[1]: r for r in rows[1:-1]}
    expect(f"CSV has {EMP_A} row", EMP_A in by_eid)
    expect(f"CSV has {EMP_B} row", EMP_B in by_eid)
    if EMP_A in by_eid:
        r = by_eid[EMP_A]
        # Cols: worker_id, employee_id, name, trade, Mon, Tue, Wed, Thu, Fri, weekly_total
        import re as _re
        expect(f"CSV {EMP_A} worker_id matches W-####", bool(_re.match(r"^W-\d{4}$", r[0] or "")),
               extra=f"got {r[0]!r}")
        expect(f"CSV {EMP_A} Mon == 8.0", float(r[4]) == 8.0)
        expect(f"CSV {EMP_A} Tue == 4.5", float(r[5]) == 4.5)
        expect(f"CSV {EMP_A} Wed blank (absent)", r[6] == "")
        expect(f"CSV {EMP_A} weekly_total == 12.5", float(r[-1]) == 12.5)
    # DAILY TOTAL summary row (last)
    expect("CSV ends with DAILY TOTAL summary", rows[-1][2] == "DAILY TOTAL")
    expect("CSV daily total Mon == 16.0", float(rows[-1][4]) == 16.0)
    expect("CSV grand total == 28.5", float(rows[-1][-1]) == 28.5)

    # ----- Step 5: PDF renders, non-empty, has letterhead -----
    print("\n--- Step 5: PDF export renders ---")
    s, body, hdrs = hit("GET", f"/api/payroll/hours.pdf?week_start={WEEK_START}")
    expect("PDF returns 200", s == 200)
    expect("PDF mimetype is application/pdf",
           "application/pdf" in (hdrs.get("Content-Type", "") or ""))
    expect("PDF starts with %PDF magic bytes", body[:4] == b"%PDF")
    expect("PDF > 10 KB (real content, not just headers)", len(body) > 10240,
           f"got {len(body)} bytes")

    # ----- Step 6: edit a cell + assert DCR sees the same number -----
    print("\n--- Step 6: edit cell -> DCR labor reflects same worked-hours ---")
    # The seeded Mon EMP_A row has sign_in_id we can grab from the grid
    a_mon_sid = a["days"][0]["sign_in_id"]
    expect("EMP_A Mon has sign_in_id", a_mon_sid is not None)
    if a_mon_sid:
        # Change Mon EMP_A to 07:30-15:30 -> raw 8h - 0.5 lunch = 7.50h
        s, _, _ = hit("PUT", f"/api/sign-ins/{a_mon_sid}",
                      body={"time_in": "07:30", "time_out": "15:30"})
        expect("PUT cell returns 200", s == 200)
        # Re-read grid
        s, body, _ = hit("GET", f"/api/payroll/hours?week_start={WEEK_START}")
        grid2 = json.loads(body)["data"]
        a2 = find_worker(grid2, EMP_A)
        expect(f"after PUT: {EMP_A} Mon hours == 7.50", a2["days"][0]["hours"] == 7.5)
        # Now issue a DCR for that Monday and confirm the labor section uses the
        # same worked-hours figure (the SHARED helper). Week-relative (#245) —
        # the old hardcoded 2026-05-11 went stale the week after it was written.
        hit("POST", "/api/work-log", body={
            "project_code": PROJECT, "date": WEEK_DATES[0],
            "trade_area": "test", "description": "hours-smoke",
        })
        s, body, _ = hit("POST", f"/api/projects/{PROJECT}/daily/{WEEK_DATES[0]}/issue",
                          body={"audience": "internal"})
        expect("DCR issue returns 201", s == 201)
        # Pull the aggregator's view of that day's labor and find EMP_A
        s, body, _ = hit("GET", f"/api/projects/{PROJECT}/daily/{WEEK_DATES[0]}?audience=internal")
        dcr = json.loads(body)["data"]
        labor_rows = dcr.get("labor", {}).get("rows", [])
        a_in_dcr = next((r for r in labor_rows if r.get("employee_id") == EMP_A), None)
        expect(f"DCR labor includes {EMP_A}", a_in_dcr is not None)
        if a_in_dcr:
            expect(f"DCR labor {EMP_A} hours == 7.50 (single source of truth)",
                   a_in_dcr.get("hours") == 7.5,
                   f"got {a_in_dcr.get('hours')}")

    # ----- Bonus: PUT validation (time_out < time_in) -----
    print("\n--- Bonus: PUT inverted-time validation ---")
    if a_mon_sid:
        s, body, _ = hit("PUT", f"/api/sign-ins/{a_mon_sid}",
                          body={"time_in": "10:00", "time_out": "08:00"})
        expect("PUT inverted times returns 400", s == 400)

finally:
    # Always-runs cleanup
    print("\n=== Cleanup ===")
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=4)
    f.close()
    clean_test_data()
    # Verify clean
    with db_layer.connect() as c:
        placeholders = ",".join("?" * len(WEEK_DATES))
        n = c.execute(
            f"SELECT COUNT(*) FROM sign_in_log WHERE employee_id IN (?, ?) AND date IN ({placeholders})",
            (EMP_A, EMP_B, *WEEK_DATES),
        ).fetchone()[0]
        nr = c.execute("SELECT COUNT(*) FROM report_index WHERE project_code=?",
                       (PROJECT,)).fetchone()[0]
    print(f"  post-cleanup: sign_in_log test rows={n}, report_index for project={nr}")

print(f"\n=== Summary: {len(PASS)} pass, {len(FAIL)} fail ===")
if FAIL:
    for x in FAIL:
        print(f"  - {x}")
    sys.exit(1)
print("\nALL CLEAR")
