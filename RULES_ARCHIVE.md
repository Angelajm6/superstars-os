# RULES_ARCHIVE — full legacy project rules

Moved out of CLAUDE.md on 2026-10-16 to keep CLAUDE.md under 200 words. Read the relevant section here before touching PII, auth, deploy, or compensation code.

---

# CLAUDE.md — Project Rules for Superstars Ops Platform

This file is read at the start of every Claude Code session. Keep it short.
Pin only rules that have already cost us hours.

---

## Convention: reference rules by what they say, not by number

Never reference hard rules by number in code comments. Rules can be inserted,
moved, or removed — numbered cross-references become wrong silently. Reference
rules by what they say (e.g., `per CLAUDE.md loopback policy`,
`per CLAUDE.md PII rule`) so the cross-reference survives reordering.

---

## 1. Design rule: HTML-first, PDF-last

**Always design in HTML. Iterate in the browser. Convert to PDF only at the moment of delivery.**

- The browser's print preview (Ctrl+P → Save as PDF) is a real PDF renderer.
  Use it for ad-hoc previews and one-offs.
- `pdf_export.py` (headless Microsoft Edge) is the production export tool.
  WeasyPrint was dropped — GTK won't install on this workstation; `render_pdf.py`
  survives only as the legacy WeasyPrint CLI for old generators (RFI etc., with a
  graceful fallback when the lib is absent). Neither is the design surface —
  invoke them only when generating final deliverables or batch output.
- The `/preview/*` routes (see `preview_routes.py`) serve every template
  directly in the browser. Use them. (Since #248 they sit behind the
  dashboard login like every operator surface — sign in once.)
- **Why this rule exists:** the CoF card iteration cost ~15 cycles fighting
  WeasyPrint-specific quirks (flex/grid failures, SVG text rendering, inline-block
  hacks). Every one of those would have been a 30-second browser refresh if we'd
  iterated in HTML instead of regenerating PDF each time.

## 2. Data rule: real PII never enters Claude chats

Worker phone numbers, addresses, SSNs, ID photos, license numbers — none of
these are pasted into chats. When values must be discussed, redact: `W1`,
`XXX-XXX-1234`, `[name redacted]`.

All real data lives in `superstars.db` on the encrypted workstation drive.
API keys live in `.env`, gitignored, never committed.

### What counts as PII-bearing by default

These surfaces embed names or other PII in their values/paths. Treat them
as PII-bearing without inspection — assume sensitive unless proven otherwise:

- Anything under `worker_records/`. Folder names follow
  `E-XXXXX_<First>_<Last>`, so even a directory listing leaks names.
- Any `*_path` column on the `employees` table — `face_image_path`,
  `photo_path`, `folder_path`. They embed the slugified worker name.
- `workers_import_template.csv`, `certifications_import_template.csv`, and
  any future PII intake CSV.
- Server logs that include those paths (e.g. face-photo upload logs).
- Any `name`, `phone`, `email`, `dob`, `emergency_contact_*`, `pin`,
  `license_number` value pulled from `employees`, `project_riggers`, or
  certifications.

### Inspection patterns: WRONG vs RIGHT

These are the failure modes that have actually leaked names into chat —
each got fixed retroactively but the fix is too late, the leak is in
scrollback. Don't rely on remembering. Use the right pattern from the start.

**WRONG — leaks PII to chat output:**

```bash
# Directory listings under worker_records/ — every folder name has the worker's name
ls worker_records/
ls -la worker_records/E-00001_*

# Raw file inspection on any PII-bearing file
head workers_import_template.csv
cat certifications_import_template.csv
tail server.log                            # server logs include face-photo paths
type workers_import_template.csv           # PowerShell equivalent
Get-Content workers_import_template.csv

# Printing path values from the DB or API responses
print(d.get("face_image_path"))            # contains worker name slug
print(employee_row["folder_path"])         # same
print(response_json["data"]["image_url"])  # same — /worker-files/E-00001_<Name>/...

# Echoing CSV rows or DB rows raw
print(csv_row)                             # rows have name, phone, dob, etc.
print(conn.execute("SELECT * FROM employees").fetchone())
```

**RIGHT — counts, shape, booleans, redacted values:**

```bash
# Counts and shape only
wc -l workers_import_template.csv
awk -F, 'END{print NR}' workers_import_template.csv
awk -F, 'NR==1{print NF}' workers_import_template.csv   # column count

# Python: len, booleans, derived flags — never raw values
print(len(rows))
print(bool(face_image_path), face_image_path.endswith(".jpg") if face_image_path else False)
print({"has_phone": bool(row["phone"]), "has_dob": bool(row["dob"])})

# Worker-record file counts without naming the workers
python -c "
from pathlib import Path
total = sum(1 for p in Path('worker_records').iterdir() if p.is_dir())
print(f'{total} worker folders')
# count face.* across all worker folders without printing any name
n = sum(1 for d in Path('worker_records').iterdir() if d.is_dir() for _ in d.glob('face.*'))
print(f'{n} face files')
"

# If a value must be discussed, redact: first initial + last 4 (phone)
def redact_name(s): return f"{s[0]}." if s else "—"
def redact_phone(s): return f"XXX-XXX-{s[-4:]}" if s and len(s) >= 4 else "—"
```

**Test-assertion pattern:**

```python
# Assert by booleans, counts, sizes — never print the PII-bearing value itself
expect("face.jpg sibling exists", file_count("E-00001", "face.jpg") == 1)
expect("face_image_path ends in .jpg", fp and fp.lower().endswith(".jpg"))
# NOT: expect("face_image_path matches", fp == "C:\\...\\Robert_Arriciaga\\face.jpg")
```

### Default posture

When uncertain whether a value is PII-bearing, treat it as PII-bearing.
A false positive ("I redacted something that turned out to be safe") costs
nothing; a false negative is permanent — it lives in scrollback forever.

## 3. Schema rule: IDs sort numerically, not lexicographically

Employee IDs are `E-XXXXX`. When computing the next ID or sorting workers:

```sql
SELECT MAX(CAST(SUBSTR(employee_id, 3) AS INTEGER)) FROM workers
```

NOT:

```sql
SELECT MAX(employee_id) FROM workers  -- WRONG: "E-012" > "E-00013" textually
```

This rule caused the E-00013 collision bug. Apply the same pattern to any
zero-padded ID format (employee, card, RFI, etc.).

**Roster rule (#246):** worker lists MUST consume the canonical roster
source — `v_active_workers` (operational surfaces) or `v_worker_roster`
(master surfaces, with the Inactive badge) — never join
`labor_worker_state` directly in a surface. One rule, one place; every
new surface inherits deactivation propagation for free.

## 4. JS rule: avoid possessive apostrophes in i18n string literals

`'modal-step3': "the worker\\'s folder is created"` will crash JS parsing in
some contexts and silently break an entire dashboard. Rephrase to drop the
possessive: `"the worker folder is created"`. This cost us a half-day of
"why is nothing loading?" debugging.

## 5. Migration rule: schema scripts are idempotent

Use the `split_statements` pattern (see `apply_riggers_schema.py`) so re-running
a migration is safe. Duplicate column errors are caught and counted as skipped.
Never write a migration that fails on second run.

## 6. Dates rule: LOCAL date, never `toISOString()`

`new Date().toISOString().slice(0,10)` returns the **UTC** date. On this
workstation (Eastern), UTC rolls over to "tomorrow" any time after ~7 PM
local. Operator-entered rows under that "tomorrow" date silently mismatch
the "today" they think they're entering — the bug behind #74 (backdated
DCRs empty, real entries landed at UTC-tomorrow). Use the local-date
helper in `dashboard-static.html`:

```js
function todayLocal(offsetDays) {
  const d = new Date();
  if (offsetDays) d.setDate(d.getDate() + offsetDays);
  return d.getFullYear() + '-'
       + String(d.getMonth()+1).padStart(2,'0') + '-'
       + String(d.getDate()).padStart(2,'0');
}
```

NEVER use `toISOString().slice(0,10)` for "today" / "today ± N days" /
date-input defaults / date-comparison anchors. Python's `date.today()`
is already local (safe). #77 swept the remaining UTC sites; future code
should not reintroduce the pattern.

## 7. Operational discipline rule: snapshot, kill orphans, loopback-only

**Snapshot before any destructive DB op.** Targeted DELETE, schema
migration, mass UPDATE, anything that mutates more than one operator-
expected row:

```bash
cp superstars.db "../snapshots/superstars-pre-<op-name>-$(date +%Y%m%d-%H%M%S).db"
```

Snapshots live in `C:\Users\SSC-Admin\Superstars\snapshots` — OUTSIDE
the project root, never under any served or servable tree. (#248: the
old `data_room/db_backups` target sat inside the public /files mount,
so every daily snapshot was downloadable without auth.) The daily
"Superstars DB Snapshot" scheduled task (`snapshot_db.ps1`) writes to
the same directory.

The snapshot is the evidence + the recovery point. Already saved us
once after a mid-cleanup DB went to 0 bytes; we want it again the next
time something weird happens.

**Kill orphan servers on 5050 before starting a new one** —
`Stop-Process` and `Popen.terminate()` don't reliably kill Flask on
Windows; they leave orphans behind that intercept smoke tests with
stale code (caught me 5+ times this batch). Use the tree-kill:

```powershell
$c = Get-NetTCPConnection -LocalPort 5050 -State Listen -ErrorAction SilentlyContinue
foreach ($conn in $c) { cmd /c "taskkill /F /T /PID $($conn.OwningProcess)" }
```

Smoke-test scripts in `tests/` use this pattern in their `finally`
block. Ad-hoc `subprocess.Popen` diagnostics should too.

**Production restart procedure (#244 zombie incident).** The listener
tree-kill above is NOT enough for a production-code deploy: a
`Start-ScheduledTask` that fires while the port is still held leaves a
**zombie waitress pair that never exits**, and Windows lets it
double-bind 5050 later — it then serves STALE code while the page
looks current (the build stamp is read from disk; imported Python
modules are not). Deploy restarts go like this, every time:

```powershell
Stop-ScheduledTask -TaskName "SSC Dashboard Server"
# kill EVERY dashboard python process — not just current listeners
Get-Process python -ErrorAction SilentlyContinue |
  ForEach-Object { cmd /c "taskkill /F /T /PID $($_.Id)" }
# confirm the port is FULLY free before starting once
Get-NetTCPConnection -LocalPort 5050 -State Listen -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "SSC Dashboard Server"
```

Then verify by **API behavior** — hit an endpoint whose response the
new build changes — never by the build stamp alone.

**Flask binds to `127.0.0.1:5050` ONLY. NEVER `0.0.0.0`.** Tailscale
serve proxies external traffic to the loopback. Binding to `0.0.0.0`
would expose the dashboard to the LAN — and PII with it.

## 8. Terminology rule: Worker ID, hours worked, SSC brand

- **Worker ID = `W-####`** (literal `W-` + zero-padded 4-digit sequence).
  Stable: once assigned, never changes. New onboards get `max + 1`,
  never reuse a deleted number. Helpers live in `worker_id.py`.
  Distinct from the internal `employee_id` (E-#####) primary key —
  worker_id is for humans, employee_id is the FK target. Display as
  "W-#### — Name" on every surface that identifies a worker.
- **Hours = "worked", never "paid".** The 8.00 figure (07:00–15:30
  minus 30-min lunch) is labor performed. Whether those hours get
  paid is a separate manual downstream decision. Helper is
  `payroll_hours.compute_worked_hours`; UI labels say "Worked Hours"
  / "Hours Worked" / "hours worked" — never "Paid Hours".
- **Brand: Superstars Contracting Inc. — "AMG" must never appear.**
  AMG is a sister-org alias that has surfaced in mockups before; the
  dashboard is SSC-branded across every operator surface, every
  rendered report header, and every export filename.
- **ID / code columns in tables and lists use the row's sans (Inter),
  never `font-family:monospace`.** Worker IDs (W-####), cert codes,
  and similar internal identifiers belong with the row — switching
  to a different typeface reads as a styling bug, not as emphasis.
  The row's `font-weight:700` + `color:#B11E2E` is enough to make
  the identifier prominent without changing the typeface. This
  applies to every list/table surface — workforce, DCR labor roster
  (entry view + rendered DCR), cert library, hours log. Document /
  regulatory numbers issued by an external authority (NYC DOB
  permit numbers, FDNY violations) may stay monospace where digit
  alignment helps reading; the rule targets *internal* identifiers.

## 9. Secrets rule: vault all authenticating API keys, never plaintext on disk

All API keys that authenticate as the company, are billed against the company's
accounts, or grant access to non-public data live in 1Password Business under
the "Dashboard Secrets" vault. Never store such keys in plaintext anywhere on
disk — not in `.env`, not in source code, not in config, not in scripts. Code
accesses them via 1Password CLI references (`op://Vault/Item/field`) injected
at runtime by `op run`, or via explicit `op read` within an authenticated
1Password session. No agent, script, or process gets unfiltered access — every
fetch goes through the Windows Hello / master password / YubiKey-gated
1Password session and is audit-logged. This rule does NOT apply to: (a) no-key
public APIs like Open-Meteo, (b) intentionally-public client-side keys like
Stripe publishable keys or domain-restricted Maps keys. For operational
hygiene, every external service the dashboard depends on still gets a
1Password vault item even with no credential — the vault doubles as the
service inventory.

## Deployment context (current — keep this current as the deploy evolves)

The Flask server runs continuously under the Windows scheduled task
**`SSC Dashboard Server`** (registered via `Register-ScheduledTask`,
launches `run_server.ps1` at SSC-Admin logon, hidden, restarts 3×@1min
on failure, no time cap). The launcher runs the app under **waitress**
(production WSGI on Windows) bound to `127.0.0.1:5050`. Falls back to
the Flask dev server if waitress isn't installed; same bind.

Path A — `ANTHROPIC_API_KEY` is **optional**. Server boots without it.
Cert-card AI extraction returns clean `503 {ai_available: false}` so
the UI can show "AI scan unavailable — use manual entry" instead of
treating it as an error. Path B (op-run injection of the vaulted key)
is on the roadmap but not yet wired into the scheduled task.

External access is via **Tailscale serve** (NOT funnel — private, not
public) at `https://ssc-bkbase.tail55067c.ts.net/`. Field devices
(operator phone, tablets) join the tailnet via auth key — no admin
login on the device. The Tailscale daemon proxies TLS-terminated
traffic to the local `127.0.0.1:5050`; the server itself never sees
external connections directly.

Operator commands:

```powershell
Start-ScheduledTask -TaskName "SSC Dashboard Server"   # start now
Stop-ScheduledTask  -TaskName "SSC Dashboard Server"   # stop
Get-ScheduledTask   -TaskName "SSC Dashboard Server"   # status
# Per-day log:
#   data_room/server_logs/server-YYYY-MM-DD.log
```

## Dashboard auth foundation (#48)

The dashboard (company console + project dashboard) sits behind a real
login. Worker-app PIN sign-in is unaffected — workers continue using the
worker app's PIN flow and do NOT get rows in `users`.

The PIN flow (PIN = phone last-4, public-exempt in `auth.py`) is a KNOWN
weak-auth gap, acceptable only until multi-user login (Pillar A) ships.
Pillar A's field/crew role is its explicit successor: when that lands, the
PIN flow gets retired or re-gated — it is not silently carried forward.

**Schema:** `users` (id, email, password_hash, role, full_name,
employee_id_link, is_active, created_at, last_login_at). Roles:
`admin`, `c_suite`, `pm`, `super`. `sessions` table holds server-side
sessions; cookie is opaque, 12-hour sliding. Schema lives in
`schema_auth.sql`; apply via `python apply_auth_schema.py` (idempotent).

**Gating:** `auth.apply_auth_gate(app)` registers a `before_request` hook
that redirects unauthenticated HTML requests to `/login?next=...` and
returns 401 JSON for unauthenticated `/api/*` requests. Public exemptions
(#248): `/login`, `/api/auth/*`, `/api/health`, `/api/today`,
`/api/worker/*`, `/worker-app` (+ its PWA manifest/service-worker shell
files), and `/files/static/*` — vendored assets ONLY. The whole-project
`/files/*` mount and the `/preview/*` exemption were REMOVED in #248
(they exposed the DB, source, snapshots, and rendered project documents
to anonymous fetches). Generated artifacts are served by the
session-gated `/project-files/` route; never re-widen `/files`. For
per-route role gating use `@requires_role('admin', 'c_suite')` (e.g.,
once Labor Rates #158 lands).

**Bootstrap (FIRST RUN ON A FRESH DEPLOY — REQUIRED):**

```powershell
# 1. Apply the auth schema (idempotent; safe to re-run)
python apply_auth_schema.py

# 2. Seed the first admin user (interactive — never echoes the password)
python bootstrap_admin.py

# 3. Start / restart the scheduled task so the server picks up the new code
Stop-ScheduledTask  -TaskName "SSC Dashboard Server"
Start-ScheduledTask -TaskName "SSC Dashboard Server"

# 4. Sign in at /login (or https://ssc-bkbase.tail55067c.ts.net/login)
```

If the server starts before bootstrap runs, the dashboard is locked
(redirect loop to /login with no valid creds) — run `bootstrap_admin.py`
and refresh.

**PII discipline for auth:** never log passwords, password_hashes, or
full session ids. The login API does not distinguish "no such email"
from "wrong password" — both return the same 401. `_smoke_auth.py`
provides a known smoke admin for the test suite; production users are
created via `bootstrap_admin.py` (first admin) or, later, an in-app
admin surface.

**HTTPS / cookie flag:** session cookies set `Secure` when either
`request.is_secure` (direct HTTPS) or `X-Forwarded-Proto: https`
(Tailscale-terminated HTTPS). Localhost HTTP works without `Secure`;
both paths verified in `tests/smoke_auth.py`.

## Compensation / payroll data governance

Pay rates, gross/net pay, deductions, tax withholdings, and any
employer-cost-of-labor figures are **company-confidential** and have
distinct handling rules above and beyond the PII rule:

- **DB at rest:** the encrypted workstation drive is the only place
  these values live. SQLCipher wrapper is a tracked roadmap item (#71
  / #72) — until it lands, the BitLocker volume is the only encryption
  layer.
- **Surface restriction:** comp data appears **only on the company
  console** (`company-dashboard.html`). NEVER on the per-project
  dashboard (`dashboard-static.html`, which is field-reachable via
  Tailscale), NEVER in the worker app, NEVER in any rendered DCR or
  exported PDF.
- **Field-reachable surfaces must be comp-free.** Anyone walking
  on-site can be standing next to the operator looking at a tablet —
  pay numbers must not be visible there.
- **The Hours Log on the company console shows hours, not dollars.**
  Pay-rate × hours math, if added, lives on a separate company-console
  surface (the Labor Sheet, task #71) gated behind whatever auth model
  ends up on top of company-only views.

When in doubt: does this number tell someone what someone earns? If
yes, it's comp data — keep it off field-reachable surfaces.

## Dependency security tooling (safety net, not a substitute for judgment)

Two automated dependency-security layers are active on this repo:
- **GitHub Dependabot** — alerts on known CVEs in dependencies and auto-opens patch PRs.
- **Socket Security** (GitHub app, scoped to this repo only) — supply-chain attack detection: malware, typosquatting, hidden/obfuscated code, suspicious privileged-API usage (filesystem/network/child_process/eval), reviewed inline on pull requests.

These are a SAFETY NET. Their presence must NOT reduce the system's own vigilance or confidence in preventing malware, spyware, data exfiltration, or anything that could harm the system or leak PII. When adding or updating ANY dependency, Claude/agents still:
- Vet the package first — legitimate, actively maintained, exact correct name. No blind `pip install` / `npm install` of unfamiliar packages.
- Never introduce malware, spyware, obfuscated code, or anything that exfiltrates data, credentials, or worker PII.
- Uphold all existing security rules — secrets vaulted in 1Password (never plaintext on disk), PII never in chats or logs, parameterized SQL, 127.0.0.1 loopback bind only.

The tools catch what slips past discipline; they do not license carelessness. Socket and Dependabot run quietly in the background so the operator can focus on organizing the business and the work — not on policing threats. First-principles security judgment remains the system's responsibility; the tooling is backup, not the primary defense.

---

## File layout

| Path | Purpose |
|------|---------|
| `server.py` | Flask server, all API routes, port 5050 |
| `preview_routes.py` | Browser preview blueprint (registered in server.py) |
| `superstars.db` | SQLite DB. NEVER commit. NEVER paste contents in chats. |
| `.env` | Local runtime env. NEVER commit. Authenticating API keys live in the 1Password vault (per the secrets rule) — `.env.template` documents shape only. |
| `.gitignore` | Excludes secrets, DB, uploads, caches. |
| `schema*.sql` | Schema definitions. Commit these. |
| `apply_*_schema.py` | Idempotent migration runners. |
| `cof_*.html`, `cof_*.py` | Certificate of Fitness card system. |
| `render_*_html.py` | Per-document-type HTML renderers. |
| `generate_*.py` | Per-document-type generators (read DB, render, write). |
| `pdf_export.py` | Headless-Edge PDF export — the production PDF path. |
| `render_pdf.py` | Legacy WeasyPrint CLI (old generators only; not the production path). |
| `worker-app.html` | Mobile PWA for worker check-in. PIN = last 4 of phone. |
| `company-dashboard.html` | Cross-project console with EN/ES toggle. |
| `dashboard-static.html` | Per-project dashboard. |
| `login.html` | Dashboard sign-in page (#48). Brand-styled, vanilla JS. |
| `auth.py` | Dashboard auth: hashing, sessions, `before_request` gate, `requires_role`. |
| `schema_auth.sql` / `apply_auth_schema.py` | users + sessions tables, idempotent migration. |
| `bootstrap_admin.py` | One-shot interactive seeder for the FIRST admin user. |
| `static/js/auth_menu.js` | Header user menu (name · role · Sign out) on both dashboards. |
| `tests/_smoke_auth.py` | Smoke-suite helper: seeds smoke admin, patches `requests`/`urllib` so existing tests authenticate. |
| `tests/smoke_auth.py` | Auth foundation end-to-end smoke: login, gate, logout, exemptions. |
| `tests/smoke_static_exposure.py` | #248 exposure gate: public serving = vendored `/files/static/*` only; DB / source / snapshots / `.env` unservable by ANY route incl. traversal; artifacts require a session. Part of the standard gate. |

## Architecture

- Flask binds to `127.0.0.1:5050` (localhost only — never expose to LAN).
- SQLite with WAL mode for concurrent reads + writes.
- Vanilla HTML/JS frontend, no build step.
- Headless Edge (`pdf_export.py`) is the production PDF tool (WeasyPrint dropped —
  GTK won't install here; `render_pdf.py` is legacy-only). SQLite is the only data store.
- Bilingual (EN/ES) via `data-i18n` attributes + `I18N` dict in JS,
  persisted in `localStorage.dashboard_lang`.

## Communication style

Direct. No long preambles. Brief acknowledge → execute → report.
Ask one focused clarifying question only when genuinely ambiguous;
otherwise make the call and explain in one line.
