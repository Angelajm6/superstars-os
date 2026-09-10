# Handoff to the next Claude Code session

> **Read this + `CLAUDE.md` at the start of every new session.** Together
> they orient you in ~2 minutes. This doc is the operational snapshot;
> CLAUDE.md is the pinned rules.

---

## Where the system is right now

A continuous-run, single-project ops platform for **Superstars
Contracting Inc.** (NYC facade restoration). Single active project:
**FR-BX-001 / 890 E 135th Street, Bronx** (client: Compass Point, LLC). 8-worker
real roster. Server runs unattended under a Windows scheduled task and is
exposed to the operator's phone/tablet via Tailscale (private serve, not
public funnel).

Real external users are on the live system: two **clients** and one
**architect**. Everything they can see is default-deny and re-derived
per request — see "Access model" below before touching any portal,
grant, or elevation code.

### #303 — The shared shell: Office Console on the sidebar layout (Bookore, 2026-09-10)

`static/css/shell.css` is now THE application chrome: dark left sidebar (brand
lockup, grouped nav, foot links) + white sticky top bar (page title, chrome
right). The Company Portal (#302) and the Office Console (`/`) both use it; every
operator page migrates onto it over time (the audit's "shared shell" plan).

Console changes, chrome only — no panel logic touched:
* The horizontal tab strip became the sidebar. In-page panels keep their
  `.nav-tab` + `data-panel` contract (now real `<a href="#panel">` links, so
  hash routing and keyboard navigation work); the tabs are grouped Company /
  Workforce / Operations / Admin and the pages that were URL-only (`/estimating`,
  `/admin/projects`, `/admin/labor-rates`, `/dropplan`, `/settings/interface`,
  `/company`) got entry points. `#hours` (Hours Log) got a tab for the first time.
* First tab renamed Projects → **Overview** (EN/ES dict updated); the KPI tiles
  moved INSIDE the Overview panel instead of sitting above every tab; the
  always-visible "Company Overview" heading and the build-stamp footer line are
  gone from user-facing copy (the stamp element remains, hidden, for tooling).
* Shell rules are `.ssc-side .ssc-nav …` (0,3,0) on purpose so a page's legacy
  `.nav-tab` rules never win. Console-only overrides sit at the END of its inline
  style block.

Guards unchanged and green: design conventions 139/139 (console buttons still
resolve to the accent), #299 static contract, #302 portal behavioral. Perf budget
unaffected (one extra static stylesheet, cached). Next surfaces to migrate:
`admin_users.html`, `admin_projects.html`, `estimating.html`, `admin_labor_rates.html`.
Buildings-as-a-table moves to **#304**.

### #302 — Company Portal (Bookore, 2026-09-10)

`/company` — the C-suite's whole-company view, a new standalone page
(`company_portal.html`) + module (`company_portal.py`) gated by the new
`company_portal` section (admin/c_suite; everyone else 403 on the page and every
endpoint). Six views, one aggregate request each, hash-routed: Overview · Clients
& Buildings · Finance · Expenses · Workforce & Pay · Compliance.

* **Buildings are DERIVED** (no buildings table yet — #303): the distinct
  normalized addresses across `projects` + `estimate` linked to a client
  (`company_portal.address_key`: '890 East 135th Street' == '890 E 135th St, Bronx').
  Each building groups its projects and estimates.
* **Finance** rolls up: estimate pipeline/won/lost by month and status, spend from
  BOTH ledgers (`expenses` receipts + `project_expense`, reported separately —
  they can overlap), by category/project, receivables from `ira_job`, labor cost =
  worked hours × the rate effective on the work date (`worker_rates`, fallback
  `labor_worker_state`) with an explicit UNRATED bucket, QuickBooks sync counts,
  and the `subscriptions` ledger when the table exists (`available:false` when not).
* **Compliance** sweeps everything with an expiry: worker certs, `permits_library`,
  `project_documents`, `estimate_document`, and the NEW `company_document` table
  (company licenses / insurance / bonds — created at boot, idempotent) into
  expired / ≤30 / 31–60 / 61–90 buckets. CRUD for company documents lives on the
  page (add / edit / archive; archive is soft).
* Comp-class data (rates, labor cost) appears here ON PURPOSE: this is a
  company-only surface (CLAUDE.md comp-data rule), never field-reachable.
* Perf budget declared (`company_portal`: reqs 6 / kb 60); design guard registry
  updated; no build stamps or issue numbers in visible copy.

Guard: `tests/smoke_company_portal_302.py` (behavioral, isolated DB: gating for
six roles, address de-dup, rollup math, both ledgers + filters, expiry buckets,
company-document CRUD, no `*_path` keys). Next: #303 first-class `building`
table + `projects.building_id` / `estimate.building_id`; company-level expense
ledger; Subscriptions CRUD.

### #301 — self-service password reset + password visibility (Bookore, 2026-09-08)

The sign-in page's "Forgot password?" now works end to end and the eye toggle
swaps its icon (eye / eye-off) on `/login`, `/set-password` and `/reset-password`.
`password_reset.py` was rewritten from the #300 sketch:

* **Delivery actually happens.** #300 only knew an SMTP transport behind five
  `SSC_RESET_SMTP_*` vars nobody had set, so every "sent" answer was a lie.
  Order now: `SSC_RESET_MAIL_FAKE_DIR` (test seam) → SendGrid via the existing
  `notifications._send_live` (`SENDGRID_API_KEY`) → SMTP fallback. Link origin =
  `APP_BASE_URL` (already in the Render env; `SSC_PUBLIC_BASE_URL` is an alias),
  https-only except loopback.
* **Honest UI.** `GET /api/auth/password-reset/config` → `{enabled}`; the
  request page shows "not enabled on this environment, contact your admin"
  instead of a fake "check your email".
* **Audit that lands.** `login_audit.event` has a CHECK constraint; the #300
  events (`password_reset_requested/_completed`) were silently dropped by
  `auth._login_audit`. Completion now writes the allowed `password_reset` event;
  request attempts go to the new `password_reset_request` table (ip + at, no
  email), which also backs a per-IP throttle (5 / 15 min → 429).
* Unchanged guarantees: identical generic 202 for unknown / inactive /
  `force_sso` / delivery-off; SHA-256 of the token only; one live token per
  user; 30-min expiry; single use; account must still be `active` at confirm;
  every session revoked on success. `/login?reset=1` shows the "password
  updated" banner.

Guard: `tests/smoke_password_reset_flow_301.py` (behavioral, Flask test client,
isolated DB — builds its own SQLite snapshot when `SSC_DB_URL` is unset) — plus
the #296–#300 static contracts, all now in the #260 gate list.

### Cloud M1 (#287) — the storage abstraction

Every DATA path resolves through **`ssc_paths.py`** driven by **`SSC_DATA_ROOT`**:
unset (production today) = exactly the old repo-dir layout, byte for byte; set =
all data categories under the root (`<root>/data_room/...`, `<root>/worker_records`,
`<root>/superstars.db`). Categories: media (field_photos, project_docs, photos,
receipts, walkthroughs, estimate_docs, material_slips, worker_records,
employee_photos, issuer_signatures), renders (reports, credentials, forms,
toolbox_talks, signage, cof_exports + the legacy root output dirs), logs, db.
Static assets and code stay repo-relative. STORED rows: every pre-#287 `*_path`
is an absolute Windows path — reads go through `ssc_paths.resolve_data_path()`
(as-is while it exists; re-anchored under the active root by its data anchor
once the tree moves); NEW media writes store RELATIVE paths (`store_rel`).
`migrate_data_root_287.py` copies + sha256-verifies the tree per category
(dry-run default; idempotent re-run = verify). Guard:
`tests/smoke_data_root_287.py` (own rooted server; upload/render/serve under a
scratch root; repo-tree mtime watchdog proves zero out-of-root writes). The
gate runs green in all four backend×root configs. NOT yet cloud: M2 = PDF on
Linux, M3 = public-door hardening, M4 = bring-up (see the operator's
CLOUD_MIGRATION_BLUEPRINT.md).

### #294 S1 — field photo reassignment + edit tracking (punchlist anchor item)

Origin: a super uploaded a batch labeled Drop 7 that belonged to Drop 8.
AMEND-NEVER-ERASE (the DCR-immutability posture applied to photos), riding
the ONE existing assignment door:

- **/api/field-photos/assign** (unchanged URL) now records a
  `field_photo_reassign` history row per changed photo IN-TRANSACTION
  (from -> to, actor, optional `reason`, batch key, SECONDS-SINCE-UPLOAD),
  detects WHOLE-BATCH corrections (the moved set covers an entire
  (uploader, uploaded_at) upload group), and — when a correction touches a
  day with an ISSUED DCR — sets `report_index.photo_amended(+at)`, writes an
  `audit_log` row (`fp_reassign_after_issue`), and returns the amendment in
  the response. Tray assignment (NULL -> drop) is logged for the trail but
  NEVER counts as a correction: the sort-tray workflow is intended use, not
  a mistake signal.
- **UI (dashboard-static Field Photos)**: gallery cards are now selectable
  (hover check; sort-tray behavior unchanged) — the floating bar switches to
  "Move to" + an optional reason in gallery mode, so the whole-mislabeled-
  upload fix is a ~10-second self-service act for _FP_ROLES
  (admin/c_suite/pm/super — operator-approved lean; oversight = visibility,
  not gatekeeping). Corrected photos carry a ↻ marker; the lightbox shows
  the full trail (from -> to, who, when, reason, whole-upload + after-issue
  chips) via /api/field-photos/<id>/history. The DCR archive shows an
  "↻ photos amended" pill on flagged rows.
- **Edit tracking (edit-rate = feedback about the SOFTWARE)**:
  /api/admin/photo-edit-patterns (admin/c_suite) — per-user corrections with
  TIMING BUCKETS (<=15 min = the upload UI lets the wrong drop through;
  <=48 h = workflow lag; longer = field labeling habits) + whole-batch +
  after-issue counts + concentration share (>=80% one person = training).
  /api/admin/photo-edit-alerts feeds a console banner (the #289 2fa-banner
  pattern; DECLARED in the console budget same-commit, 8 <= 10 reqs): fires
  past a SETTABLE threshold (app_settings `fp_reassign_alert_threshold`,
  default 5/user/rolling-7d, POST .../threshold to change) AND on any
  whole-batch correction; every alert carries the dominant bucket READING,
  never a bare count. uid / W-#### in alert text — names never.
- **CASE TRAP fixed en route**: report_index rows carry `report_type='DCR'`
  (uppercase) on live; the amend query matches UPPER(report_type) and the
  guard seeds mirror the real casing. Alert/pattern assertions in the guard
  are SCOPED TO THE SUITE'S OWN UIDS — the feed is global, and a gate
  snapshot taken after real corrections exist must not fail the suite.
- Guard: `tests/smoke_photo_reassign_294.py` (44 checks) in the gate.
  Punchlist items NOT in this session: "Users"->"Directory" rename, color
  corrections (operator to enumerate), portal preview double-fetch.

### #294 S2 — selection UX (the batch case was too fiddly) + health db label

Operator report: select circles missed clicks (opened the lightbox instead);
whole-batch moves took N precise clicks; circles invisible on tablets.

- **ROOT CAUSE of "missed" clicks — found in browser hit-testing, not the
  delegation:** the #264 `.fp-vis` share/flag overlay covers the WHOLE card on
  hover (`inset:0`, z-index:3, pointer-events:auto) while the check sat at
  z-index:2 — a real mouse could NEVER hit the circle (elementFromPoint A/B
  proved it: old stacking resolves the circle's own pixels to the overlay ->
  lightbox). Harness clicks dispatch directly on the node (no hit-testing),
  which is why #294 S1's browser pass missed it. Fix: gallery check z-index:4
  (+24px visual, ::after corner hit zone ~44x38). Vis buttons sit center-card
  — reachable as before; a corner sliver may overlap the zone only on the
  narrowest cards (selection wins there — visible + reversible).
- **Per-group "Select all"** button in every gallery group header (drop /
  date / all groupings — so it follows the active filter for free). Selecting
  with pages unloaded LOADS THE REST FIRST (bounded ~40 pages; console-warns
  if capped — no silent partial "all"); count on the button only when the
  gallery is fully loaded (a partial count would lie). Toggles to "Unselect
  all". Verified in preview at real scale: 20-of-99 loaded -> click -> 99
  selected -> Move wrote 99 history rows (whole_batch + dcr_amended + reason
  all correct) -> moved back, net-zero on the dev snapshot.
- **Touch:** `@media (hover: none)` shows the circles at rest (.92) on
  tablets/iPads; select/anysel still go full. Tap-to-open untouched.
- **Health db label (S1 punchlist item):** /api/health `db` now reports the
  backend FLAVOR ("postgres"/"sqlite" via db_layer), never a filename — and
  doubles as the public deploy fingerprint (frontend-only deploys are
  otherwise unverifiable without auth; see OPTIONS-Allow note in memory).
- Dev-preview footnote — FIXED 2026-08-10: the `.dev_db_url` override is now
  applied at the TOP of server.py (before the db_layer import), so the
  import-time boot-ensures run against the ISOLATED copy, never the default
  live db (previously they ran against live on every dev boot while the
  isolated copy stayed unmigrated -> photos 500). preview_dev_isolated.ps1
  now defers to the marker (its last-resort pin moved 273 -> 278), and
  snapshots/ssc_dev_278.db was refreshed from live. Gate 41/41 both backends.

### #295 — daily hours correction: sign-in edit flow (amend-never-erase)

Origin: Aug-12 hours entered wrong on production; Cancel didn't "clear"
them. DIAGNOSIS: Cancel was working as designed — it discards unsaved
drafts and rolls back SAME-SESSION manual adds; rows saved in EARLIER
sessions are records, and no edit path existed for them. Built the edit
flow on the EXISTING sign-in doors (PUT/PATCH/DELETE /api/sign-ins/<id> —
the Weekly Hours Log has used them for months, silently):

- **History** (`sign_in_edit`, boot-ensured via apply_hours_edit_295):
  every operator correction writes action/from→to/actor/reason
  in-transaction; DELETE preserves the erased times. Worker-app self
  sign-in/out deliberately does NOT write history (original record, not
  a correction). Weekly Hours Log edits now leave a trail for free.
- **Issued-day arc**: correction ALLOWED; report_index.hours_amended(+at)
  set, audit_log 'hours_edit_after_issue' row, history row flagged, and
  BOTH audiences AUTO RE-RENDER via _issue_one_dcr with the preserved
  sequence (#282 immutability). Re-render runs POST-COMMIT by design: the
  DB correction is truth; a render failure degrades to the stale-flag
  manual re-issue path (response carries rerendered:false). Stale ends
  CLEARED on success (artifact matches live). Archive shows a
  "⏲ hours amended" pill (same family as #294's photo pill; list_reports
  SELECT * carries the new columns automatically).
- **Roster edit UI** (entry view): every saved row gets ✎ (inline
  time-in/out + optional reason + row Save/Cancel) and 🗑 (confirm +
  optional reason). Summary labels rows "saved records"; on an issued day
  the summary + button titles warn the correction is logged and the
  report re-renders. The daily GET now returns `issued` for this.
- **Cancel semantics**: unchanged mechanics (discard drafts + roll back
  session adds — those deletes now self-label 'entry cancelled before
  issue' in the trail) + the confirm copy states saved sign-ins are kept
  and corrected via ✎/🗑.
- **Derived data**: nothing stored to recompute — worked hours/weekly log
  compute on read; the #292 memo choke-point (_mark_dcr_stale →
  ssc_memo.bump) fires on all three doors (guard-asserted end-to-end:
  cost-widget total MOVES on edit and on remove).
- Guard: tests/smoke_hours_edit_295.py (40 checks) in the gate — history
  arcs, cancel-label, anon 401s, issued arc re-render VERIFIED BY CONTENT
  (internal artifact carries the corrected time, drops the old one, keeps
  the control row; client artifact is curated — bytes-changed check),
  archive flag, memo movement, structural UI markers.
- NOTE: client-audience DCRs carry no per-worker times (portal parity) —
  content checks against the client artifact must assert bytes-changed,
  not time strings.
- **Gate fallout fixed en route**: the #196 DcrStalingLifecycle section in
  smoke_crud_data_integrity asserted stale=1 after a PUT — pre-#295
  semantics (the correction door now SELF-HEALS: re-render clears stale).
  Rewritten: step 3a asserts the amend arc (rerendered:true, stale stays
  0, hours_amended=1); step 3b keeps #196 coverage via the ADDITION door
  (POST still stales; re-issue clears). Its teardown + the smoke's main
  sweep now clean the correction residue (sign_in_edit + amend-audit
  rows — numeric report_index targets defeat the corruption suite's
  prefix filter). CAUGHT BY the no-production-corruption tripwire, NOT
  the suite itself: smoke_crud_data_integrity's main() always exits 0
  (sections can fail silently) — chip filed to fix its rc semantics.

### #295 S2 — the containment-anchor family (rel/URL math vs the data root)

Production: DCR photo upload 500'd ("/var/data/... is not in the subpath
of '/app'"). The write landed correctly under the root; the rel/URL math
anchored to SCRIPT_DIR — the two coincide ONLY on an unrooted
workstation, so this whole class worked locally and broke on cloud. The
path-family trilogy: S3 = reads through the resolver, S4 = writes
store_rel, S2 = rel/containment ANCHORS against data_root().

Swept every relative_to / is_relative_to / startswith in the runtime:
- FIXED: /api/photos/upload (reported break; row now store_rel);
  credentials batch-print (Print All IDs was broken on cloud,
  undiagnosed); credential issue html_export_path/html_url; cof_issuer +
  company_id_issuer photo snapshots (BOTH silently issued cards with no
  photo on cloud — best-effort except swallowed the raise; both also had
  the is_absolute-gate-defeats-the-resolver bug from S3).
- CORRECT, left alone: /project-files + the catch-all (data_root;
  dual-base by design), materials slips, receipts, portal renders,
  preview routes (SCRIPT_DIR is RIGHT there — code templates).
- Guard: check_dcr_photo_upload_rooted in smoke_data_root_287 (upload
  lands under scratch root + portable row + serves back + out-of-root
  location refused). Caveat documented: a shallow '../..' location
  misfiles WITHIN the photos base and is permitted today (pre-existing;
  authed operators only).

### #294 S3 — worker headshots blank on cloud: the raw-path family swept

Operator report: workforce/project-dashboard headshots render on the
workstation, NOT on production; field photos fine. Root cause = the #290
`_fp_serve` defect, still present in the WORKER-FACE family: all 14
employees.face_image_path rows are pre-#287 WINDOWS-ABSOLUTE; raw
`Path(stored)` can never sit under the Linux root, so `has_photo` computed
false (the `<img>` never rendered — not even a 404) and both photo serve
routes 404'd. FILES WERE PRESENT on the cloud disk all along (workstation
worker_records: 19 files / 15 face.*, ZERO modified after the 2026-08-02
migration; tree was in the sha256-verified M4 tar) — route bug, not
missing media.

- Fixed through `ssc_paths.resolve_data_path` (containment checks
  unchanged): crew-compliance has_photo, labor-rates worker-card
  has_photo, labor-rates worker-photo serve, employees face-photo serve.
- Normalized two adjacent sites: the worker-app photo_url builder's
  `if not is_absolute()` gate (it SKIPPED the resolver for exactly the
  rows that need re-anchoring; accidentally worked on Linux because a
  Windows string is not POSIX-absolute) and `_exp_page_paths` (receipt
  page glob now resolves first; today's one caller passes fresh paths —
  future-proofing only).
- Guard: `check_worker_face_cross_flavor` in tests/smoke_data_root_287.py
  — seeds an employee whose face_image_path is Windows-absolute with the
  file under the scratch root, asserts has_photo on both APIs + 200 PNG
  bytes from both serve routes (all four FAIL on the pre-fix code).
- ~~STILL OPEN (chip filed): the worker-record WRITE flows~~ **CLOSED in
  S4 (2026-08-11), next section.**

### #294 S4 — worker-record WRITE flows: resolver on read, store_rel on write

The S3 counterpart: uploads (cert scan, documents, face photo), the
name-change folder rename, and deletes still built raw Path() from stored
folder_path/file_path — 400 "invalid folder path" fail-closed on cloud
for all pre-#287 workers.

- `_worker_folder_resolve()` (server.py, next to WORKER_RECORDS_DIR) is
  THE way write flows turn employees.folder_path into an on-disk folder:
  stored rows go through resolve_data_path; an empty row mints the
  standard layout and stores it PORTABLE (store_rel). Existing absolute
  rows are left untouched (#287 zero-mutation) — the resolver handles
  them forever.
- NEW rows now stored PORTABLE everywhere: minted folder_path,
  worker_documents.file_path, employees.face_image_path (upload +
  rename). We stopped minting Windows-absolute rows entirely.
- Containment checks modernized `startswith` -> `is_relative_to` (the
  prefix-collision footgun: /root/worker_records2 passed startswith).
- Latent bug fixed en route: the name-change folder rename never
  re-pointed face_image_path (embeds the slug) — headshot went dark
  after any rename, on ANY host. Now re-pointed (portable) in the same
  transaction, only when the folder actually moves.
- Doc-list file_url builder (S3's one missed sibling): resolver first —
  pre-#287 doc rows raised ValueError -> file_url None -> dead thumbnails
  on cloud.
- Guard (#287 suite, same seeded pre-#287 worker, now with
  Windows-absolute folder_path too): face/doc/cert uploads land under
  the scratch root, rows stored PORTABLE (worker_records/... prefix,
  no drive letter), serve-back works from the new rows, face DELETE
  unlinks via the resolver. Suite standalone 32/32. The suite's server
  env strips ANTHROPIC_API_KEY so the cert probe hits the deterministic
  keyless branch.

### #293 S1 — AUTHORABLE elevations (drawing set -> sheet picker -> AI trace -> confirm)

The #280 markup surface is authorable for ANY project and ANY elevation.
Operator flow: upload the engineer's multi-page PDF on **/drawing-author**
(internal-only) -> page thumbnails render; each sheet's label / sheet number /
is_elevation flag pre-fills from the PDF **text layer** (never OCR — the
title-block parser anchors on `SHEET NO.`, and face captions on
`(EXISTING|PROPOSED) <FACE> ELEVATION`; a scanned set degrades to blank
pre-fill) -> pick the sheet, drag a region or click an AI-suggested one
(every elevation sheet carries an EXISTING and a PROPOSED drawing — the
operator picks the subject, never the machine) -> **Propose with AI** (Tier
B: claude-opus-5 via `SSC_TRACE_MODEL`, structured-output grid: bays, floors,
proportions, irregularities; keyless server -> clean 503 and the manual tools
are the whole story) -> adjust with the always-available manual tools
(numeric bays x floors, divider drag, split/delete bay, add/remove floor) ->
Save draft -> **Confirm** generates one `elevation_cell` per bay per floor
and the elevation goes live on /drawing-markup.

Pieces: `apply_drawing_author_293.py` (drawing_set + drawing_sheet +
elevation gains face_label/source_sheet_id/region_json/**status**; ensured at
BOOT per the #289 pattern — production self-migrates; 890 North backfills to
face_label='North', status='confirmed', geometry untouched: 12 drops/60 cells
verified through the live API). `drawing_sets.py` (upload/parse/render/serve
+ authoring endpoints on `/api/drawing-sets` + `/api/author/*` — prefixes NO
external gate opens, AND every handler re-checks internal role; sheet
renders + the set PDF serve IMMUTABLE, #291 header). `elevation.py` listing
serves REAL rows (free-text faces, drafts internal-only) + the canonical
placeholders; by-id 404s drafts to external audiences. Markup page: elevation
selector with draft/untraced greying, zoom for tall buildings (80 floors =
960 cells verified, level ids ZERO-PADDED L01..L80 so string sorts hold),
authored geometry renders through the SAME renderer (features {} tolerated,
nominal feet labels suppressed via `geometry.authored`).

**STATE MACHINE:** draft -> confirmed, one-way. Drafts: editable, deletable,
invisible outside (by-id 404, absent from external pickers). Confirmed: grid
LOCKED (geometry PUT / re-confirm / delete -> 409) — the team's marks sit on
those cells. `elevation.status` DEFAULTS to 'confirmed' so every pre-#293 row
and every out-of-flow INSERT keeps pre-#293 visibility; only the authoring
flow writes 'draft'.

Fixture: `tests/fixtures/890E135_prefiled_plans_2022.pdf` (12.8 MB,
gitignored; committed README has provenance + the STO caveat: GEOMETRY
REFERENCE ONLY, its insulation scope was removed from the real job). Guard:
`tests/smoke_drawing_author_293.py` — 89 checks, own two servers (fake-seam
`SSC_TRACE_FAKE` + keyless degrade), wired into the gate. Budget re-measured
DELIBERATELY: drawing_markup kb 80 -> 100 (census note in BUDGETS), new
drawing_author surface 5 reqs / 40 KB; an authored 80-floor tower serves
~162 KB on its own by-id — recorded in the BUDGETS note against the day a
tower is a project's first elevation.

NOT in this session (session 2): drop assignment onto authored grids,
cell-level activation, change-order items.

### Cloud M4 (#290) — bring-up artifacts (blueprint + tz + battery)

The repo now carries the full Render bring-up kit — see
**CLOUD_M4_RUNBOOK_290.md** for the operator flow, rehearsal procedure and the
M5 final-sync runbook. Pieces:

- **render.yaml + Dockerfile + .dockerignore** — one web service
  (`ssc-dashboard`, docker runtime, starter, region virginia — MUST match
  ssc-dashboard-db), disk `/var/data` (5 GB), health `/api/health`, env
  topology values literal + every secret `sync: false` (names only; values
  1Password → Render). Image: python:3.12-slim + chromium (+ fonts-inter/
  liberation/dejavu, tzdata, rsync). Waitress binds 0.0.0.0:$PORT INSIDE the
  container — a documented, deliberate exception to the loopback rule (no LAN
  in a container; Render's proxy is the only ingress). The workstation deploy
  keeps 127.0.0.1:5050.
- **SSC_TZ enforcement (`ssc_tz.py`)** — `ssc_tz.enforce()` is the FIRST call
  in server.py: SSC_TZ set + POSIX → export TZ + tzset() before any date use
  (UTC host thinks in Eastern); unset → no-op; Windows → hard no-op that never
  exports TZ (MSVC would misparse IANA names in children). Unresolvable zone
  RAISES at boot (#288 fail-fast doctrine). db_layer opens PG sessions with
  `TimeZone=<SSC_TZ>` (libpq options, connection-scoped). Guard:
  `tests/smoke_tz_290.py` (gate #36) — planted 23:30-Eastern boundary instants
  (EST + EDT) where UTC date ≠ Eastern date, POSIX UTC-host simulation with a
  can-fail control, Windows no-op contract, boot-order source check, live
  /api/today probe.
- **Linux sweep** — generate_credentials_batch now goes through db_layer
  (was 3× raw sqlite3.connect: broke the bundle on PG AND read LIVE under the
  gate) and its dead EDGE_PATHS list is gone. Known-fine Windows-isms left in
  place: pdf_export's exists()-guarded Edge/Chrome fallback paths, workstation
  tooling (.ps1, tests' taskkill), 22 CRLF shebangs (nothing execs them
  directly), apply_refresh_cofs_171's hardcoded dir (one-time, historical).
- **Remote verification** — `tests/acceptance_battery_290.py` (PRE = health/
  login/auth-gate/static/worker-app/timezone; FULL adds synthetic is_system
  login, chromium PDF w/ page count, portal containment probes, photo-serves,
  DCR client render; refuses full phase on a non-Postgres SSC_DB_URL) and
  `tests/verify_media_remote_290.py` (per-tree counts/bytes + sampled sha256
  over Render SSH, PII-safe output).

### Cloud M3 (#289) — public-door hardening (DEPLOYED behavior)

The login endpoint, staff auth, and worker PIN flow are hardened for the M5
public exposure. **Enforcement switches ship OFF (grace) so the deploy strands
nobody; the operator flips them after enrolling people** — see the OPERATOR
2FA / DEVICE TO-DO below.

- **Login** (`auth.py`): per-account + per-source(IP) fail counting →
  exponential backoff, uniform on right/wrong password (no timing oracle);
  hard lockout refuses even a correct password with the identical 401
  (audited `login_lockout`); unknown-email ≡ wrong-password response (no
  enumeration). `_client_ip` trusts `X-Forwarded-For` ONLY when
  `SSC_TRUSTED_PROXY` is set. Sessions: rotate on login, absolute 7-day
  ceiling atop the 12h slide, logout server-invalidates. `_request_is_https`
  honors `SSC_TRUSTED_PROXY` (waitress strips untrusted XFP) so the Secure
  cookie flag is correct behind the M4 TLS edge.
- **Staff 2FA** (`totp.py` stdlib RFC-6238 + `auth_hardening.py`):
  self-service TOTP at `/api/2fa/begin|confirm|disable` (Settings panel UI),
  otpauth URI + 10 bcrypt-hashed single-use recovery codes; login two-step
  (password → `totp_required` 401 → code), enumeration-safe. `force_sso`
  admin flag disables the password path (403 `sso_required`), refused unless
  the account has `google_sub`. Console banner (`/api/admin/2fa-status`)
  lists staff still missing a factor.
- **Worker PIN** (`server.py` + `auth_hardening.py`): per-source PIN throttle
  ALWAYS ON; device binding behind `app_settings.worker_device_enforcement`
  (default `0` = grace). When `1`, a valid PIN also needs a `device_token`
  from one-time provisioning (admin issues a 6-digit code / `?provision=`
  URL → the phone redeems it for a localStorage token). Revocable per device.
- Schema: `apply_public_hardening_289.py` (idempotent, dual-backend, **ensured
  at server boot** — no manual migration step on deploy). Secrets: runtime
  reads env only (guard-asserted); inventory in `SECRETS_INVENTORY_289.md`
  (the M4 Render env group).

**OPERATOR 2FA / DEVICE TO-DO — before the M5 public DNS flip:**
1. Each staff account (admin/c_suite/pm/super/estimator) gets a second
   factor. Fastest: **link Google SSO** (already domain-restricted). Or
   enroll **TOTP** in Settings → Two-Factor. Do the **admin account(s) first**,
   then c_suite, then pm/super/estimator. The console banner lists who's still
   missing one; it clears as you go.
2. Optionally set `force_sso` on staff who should be SSO-only (must have SSO
   linked first).
3. Provision the **8 field phones** (worker-devices/provision → open the
   `?provision=` URL on each phone, enter the Worker ID). Then flip
   `worker_device_enforcement` to `1` (worker-devices/enforcement). This is a
   **hard pre-M5 gate** — do NOT flip the public DNS until it's on.
4. At M4, set `SSC_TRUSTED_PROXY` in the Render env group so per-IP throttling
   and Secure cookies read the real client through Cloudflare/Render.

### Cloud M2 (#288) — PDF engine abstraction

`pdf_export.py` is engine-selectable via **`SSC_PDF_ENGINE`** ('edge' |
'chromium'; unset -> edge, today's Windows path byte-for-byte). Chromium
binary from `SSC_CHROMIUM_PATH` (must exist when set — a wrong path raises
a clean PDFExportError, never a fallback), else PATH lookup (chromium /
chromium-browser / google-chrome / chrome), else the Windows Chrome dirs.
ALL engine-specific flags live in `engine_flags()` — edge keeps
`--headless=old` (its print-flush quirk); chromium uses `--headless
--no-sandbox` (modern Chrome dropped old-headless; containerized M4 hosts
need no-sandbox, and our renders are our own self-contained file:// HTML).
The result dict keeps its historical shape — the browser path stays under
"edge_path" (the #247 response scrub list keys on it) with an "engine" key
alongside. generate_credentials_batch consumes the SAME discovery + flags
(its two embedded Edge command blocks are gone). Engine parity proven on
the workstation across 7 doc types (DCR internal+client, weekly-hours,
blank form, toolbox talk, signage, CoF credential card): identical page
counts, size ratios 1.00-1.17; the credential BUNDLE shares the pipeline.
Guard: `tests/smoke_pdf_chromium_288.py` (gate #34). M4 sets
SSC_PDF_ENGINE=chromium on the cloud host.

### Architecture in one paragraph

Flask (Python 3.12) + waitress on `127.0.0.1:5050`, vanilla HTML/JS
frontend (no build step). Data access goes through **`db_layer`**, driven
by `SSC_DB_URL`: SQLite (`superstars.db`, WAL) is the default and what
production runs; Postgres is supported and the gate runs on both. PDF
render is headless Microsoft Edge (`pdf_export.py`) — WeasyPrint/GTK won't
install on Windows Home and is no longer used. Surfaces:

- **Company console** (`company-dashboard.html`) — workforce list, cert
  health, Weekly Hours Log. Hosts comp-side data; **field-restricted**.
- **Project dashboard** (`dashboard-static.html`) — per-project DCR
  entry + archive, sign-ins, photos. Field-reachable via Tailscale.
  Rendered **per role**: gated SECTION blocks are stripped server-side.
- **Worker app** (`worker-app.html`) — mobile PWA, PIN sign-in (PIN =
  last 4 of phone). Field-reachable.
- **Client portal** (`client_portal.html`, "Classic") — what real clients
  see today. Per-item visibility engine (`visibility.py`): a document or
  photo is invisible unless explicitly shared to the client audience, and
  ownership is re-derived from the row on every by-id fetch.
- **Drawing markup** (`templates/v2/drawing-markup.html`) — the #280
  elevation surface, shared by internal roles, architect and client.

DCR renderer (`render_dcr_html.py`) emits 11-section SSC-branded HTML
with inline-SVG logo, print-CSS that mirrors screen layout (0.55in
insets, two-column paired sections, `break-inside:avoid` on `.sec`),
and a `beforeprint` JS that fits-to-one-page when a report is
≤1.25× a page (Chromium `zoom` — paint-only `transform` was wrong).

### Access model (read before touching portal / grants / elevation)

Three independent axes, each enforced server-side on every request. Hiding
a nav item is never access control.

1. **ROLE → sections** (`access.py SECTION_ACCESS`). One source feeds both
   sidebar stripping and endpoint gates. Eight roles: `admin`, `c_suite`,
   `pm`, `super`, `estimator`, `client`, `architect`, `vendor`.
2. **ASSIGNMENT → projects** (`pm_project_assignment`). pm/super/estimator/
   architect see assigned projects only; a client sees the single project
   their portal is bound to.
3. **GRANT → portal sections** (`client_section_grant`, #269). Per-client,
   per-section, **default OFF**. Zero grants = the #267 hard-stop: the
   client is contained on `/welcome` and every API 403s.

**Grant wiring as of #283.** `client_grants.SECTIONS` is what MAY be
granted; `SERVED_SECTIONS` is what a grant actually OPENS today. The admin
UI renders toggles for the intersection, so a section whose portal payload
does not exist yet (`weekly`, `materials`) is catalogued but shows no
toggle — never a grant that opens to nothing. `drawing` gates the drawing
markup page, `/api/elevations`, `/api/elevation/*` and drop/photo comment
threads; `rfis` gates `/api/rfis` and RFI comment threads. Presets are
explicit copies, never the catalog object (an import-time guard enforces
it): Standard excludes drawing/rfis, Full includes them.

The architect is contained by an **allowlist** (`elevation._architect_gate`)
— a route added tomorrow is closed to them until someone opens it — plus
normal project scoping. Architects are not grant-gated on their own
drawing surface.

**#286 — THE PARITY CONTRACT (doctrine; supersedes the #285 wording).**
Parity is PAGE ANATOMY, not component styling: for every portal section the
page SKELETON — header block, KPI tile row, widget grid, tables, boards —
is IDENTICAL to its internal counterpart; only the data feeding it differs
(the `client_payload()` registry, as always). The shared layer is
`/files/static/css/widgets.css` (`shc-` families, extracted verbatim from
the internal fp-/ph-/dcr-/wx-/la- rules) PLUS the actual layout engine:
`/files/static/js/dash_layout.js` + vendored GridStack are consumed by
BOTH surfaces — the portal Progress grid is the same drag/persist/reset
machinery as the internal dashboards (page_key `portal_progress`,
`/api/dashboard/layout` opened to granted clients: user-scoped,
key-allowlisted, structurally sanitized). Plumbing separation unchanged:
allowlist shell, registry-only data, never serve internal pages to
external roles. Client-safe DELTAS are subtractions, never substitutions
(a removed workforce tile is not replaced with worker data; removed
actions leave read-only anatomy).

**PROVENANCE EXCEPTION (#286, planner-approved):** look-ahead activity
TITLES cross to the client — they are planning labels designed for
external consumption (the internal Print exists to share that board).
Delivery titles are REPLACED with the generic "Delivery" before
projection; crew, notes, source and constraint counts never cross. This
is the ONLY free-text field with an external pass; anything else still
requires its own explicit decision here.

**#286 anatomy notes.** Daily = the internal DCR archive (KPI tiles,
search + date range, black-header table); the view icon serves the
CLIENT-AUDIENCE render by sequence (`/api/portal/<code>/daily/<seq>/view`
— serves `client.html` and nothing else; audience is per-render). The
status-churn feed collapses to NET day-level changes (X→X and same-day
round trips render nothing) and lives as a secondary expand block inside
the table row. Schedule = the internal Two-Week Look-Ahead board
read-only via `load_window` (NEVER the drafting GET — a client request
must never write), minus the constraints column. Admin client-access page
has ONE Preview button; Classic remains server-side purely as rollback.

**#284 — THE FLIP IS LIVE.** `/portal/<code>` is the CLIENT's home:
`portal_shell.html` (allowlist-first — the file contains ONLY portal
components), nav + view panes rendered from the client's EFFECTIVE set =
`portal_matrix.ROLE_SECTION_MATRIX[role]` ∩ #269 grants, fetching
`/api/portal/<code>/{progress,photos,documents,daily}` (portal_sections.py —
every field through the `client_payload()` registry, audience hard-coded
"client", URL code must equal the client's bound project). `/welcome` and
Classic `/portal` 302 granted clients to `/portal/<code>`; the #267
zero-grant hard-stop is unchanged; the architect keeps `/drawing-markup`;
the Classic ENGINE (its /api/portal/* payloads + id-gated byte routes +
admin Classic preview) is intact. Admin preview of the new shell is
byte-identical to the client's own page by construction.
**ROLLBACK = `git revert 2c4fb4a` (the flip commit) + #244 restart — one
commit, nothing else.** Guarded by `tests/smoke_portal_flip_284.py` (56
checks, planted-failure-proven) + the updated landing contracts in
smoke_portal_shell_281 / smoke_client_grants_269.

### The performance budget (#292 — permanent, gate #39)

`tests/smoke_perf_budget_292.py` holds a per-surface REQUEST-COUNT and
PAYLOAD-BYTES ceiling for every served surface (census numbers + slack). A
build that makes any page chatty again fails the gate until it adopts the
shared layer: bootstrap aggregate (`_agg_parallel`), cache-first paint
(`SSC_PERF.cacheFirstJSON` / `SSC_BOOT`), or `ssc_memo`. The declared
load-time endpoint set per surface is part of the contract — add a load-time
fetch, re-run the browser census, and update BUDGETS in the SAME commit.
Raising a ceiling is legitimate when the surface genuinely grows (e.g. #293
makes drawing markup authorable) — it must be deliberate and measured, never
a silent edit.

### The memo doctrine (#292 — permanent)

Expensive reads are memoized via ssc_memo with write-invalidation. TTL
freshness-guessing is banned. Every memoized payload ships with a
planted-write invalidation guard: each invalidating write type must provably
change the served payload, and a stale serve is a red gate.

Mechanics: `ssc_memo.memoize(scope, fn)` caches the RAW role-NEUTRAL
aggregation only — gating/curation/shaping stay per-request ABOVE the cache
(two-role probe in the guard). Time-dependent computes put the date IN the
scope key (a new day is a new key by construction). Domain writes bump at
their choke-points: sign-in writes ride `_mark_dcr_stale` (every labor
mutation already routes there); rate changes ride worker_rates.set_rate +
bridge_approved_rate (domain-wide — rates cross projects); expense
create/void bump per project. Serves are deep copies; single-flight per
scope. Guard: tests/smoke_memo_292.py (gate #39).

### Two invariants that are load-bearing

**DCR numbers are immutable.** A DCR's number is its identity (report_id,
render dir, anything printed). `next_dcr_sequence` allocates strictly
`MAX(seq)+1`; issuing a report NEVER renumbers an existing one regardless
of date order; a deleted number stays retired (no gap-fill reuse). Date
ordering is a *display* concern — sort by `report_date`. The old
date-positional allocator + `shift_dcrs_for_backdate` renumbered two real
issued reports during normal operator use and were removed in the #282
repair pass; `renumber_dcrs_by_date.py` was deleted in #283 for the same
reason. Do not reintroduce either.

**Status colour comes from the status table, never from a template.**
`status_tone` holds the five-tone ladder used by the elevation module,
keyed `elevation.<key>`, ordered by `severity_rank`:

| key | label | tone | severity | client-visible |
|---|---|---|---|---|
| `not_started` | Not started | neutral | 0 | yes |
| `complete` | Complete | green | 10 | yes |
| `in_progress` | In progress | blue | 20 | yes |
| `on_hold` | On hold | gold | 60 | yes |
| `rework` | Rework | coral | 80 | yes |

`on_hold` and `rework` are alert statuses (severity ≥ 60) and **require a
reason** (400 without one). A drop's work status is **derived** from its
cells and never stored: any rework → rework; any on_hold → on_hold; all
complete → complete; any movement → in_progress; else not_started.
`reason` is external by construction; `internal_note` is a separate column
that never enters an external payload. Phase 2 seeds more modules into
this table — it must not recreate it.

### DB baseline

103 tables (SQLite live; Postgres `ssc_test` mirrors it for the gate).
The old 42-table/77-row "clean slate" figure below is historical.

```
(historical — 2026-05-20 clean slate)
42 tables, 77 rows total

reference (preserved):
  employees:            8       (full real roster, W-0001..W-0008 backfilled)
  projects:             1       (FR-BX-001 only)
  project_assignments:  8       (all active)
  project_riggers:      2       (TBD placeholders — replace before live CoF use)
  cert_types:          47       (catalog)
  app_settings:         3
  subscriptions:        8

transactional (all zero — clean slate):
  sign_in_log: 0   work_log: 0   deliveries: 0   equipment_log: 0
  safety_events: 0   toolbox_talk_records: 0   issues: 0   inspections: 0
  visitors: 0   photos: 0   weather_log: 0   report_index: 0

issued credentials (zero):
  cof_cards: 0   company_id_cards: 0   certifications: 0

flagged-but-untouched (RFI, meetings, lookahead, drop_plan, NYC DOB
caches, identifications, worker_documents, employee_assignments, ...):
  all at 0
```

Latest pre-wrap snapshot: `data_room/db_backups/superstars-post-clean-slate-2-20260520-232822.db`.

---

## How to run / test

### Start the server (continuous-run)

```powershell
# Start the scheduled task that runs waitress on 127.0.0.1:5050.
# Hidden, no PowerShell window required.
Start-ScheduledTask -TaskName "SSC Dashboard Server"

# Verify (expect HTTP 200, 121K-ish page):
Invoke-WebRequest http://127.0.0.1:5050/ -UseBasicParsing
```

### Stop / restart for maintenance

```powershell
Stop-ScheduledTask -TaskName "SSC Dashboard Server"
# Then for a code change: Start-ScheduledTask again. (No code-reload
# in the production task; restart applies new code.)
```

### Inspect the launcher logs

```
data_room/server_logs/server-YYYY-MM-DD.log
```

Boots, waitress-vs-dev decision, exits, restart-count, and Python
tracebacks all land here.

### Kill orphan on 5050 (before any smoke or new server)

```powershell
$c = Get-NetTCPConnection -LocalPort 5050 -State Listen -ErrorAction SilentlyContinue
foreach ($conn in $c) { cmd /c "taskkill /F /T /PID $($conn.OwningProcess)" }
```

`Popen.terminate()` and `Stop-Process` don't reliably kill Flask on
Windows. Use the tree-kill. (See CLAUDE.md operational-discipline rule.)

### Run THE GATE (the thing to run before any deploy)

Never against live — the runner refuses without `SSC_DB_URL`:

```powershell
# SQLite, isolated copy of live
python -c "import sqlite3; s=sqlite3.connect('file:superstars.db?mode=ro',uri=True); d=sqlite3.connect('../snapshots/ssc_gate.db'); s.backup(d)"
$env:SSC_DB_URL="sqlite:///C:/Users/SSC-Admin/Superstars/snapshots/ssc_gate.db"
venv\Scripts\python.exe tests\run_gate_260.py
```

```powershell
# Postgres (portable dev instance; pg_ctl gets its OWN command — never
# chained with a python DB step, that wedges it)
.pgdev\pgsql\bin\pg_ctl.exe -D .pgdev\data -o "-p 5433" -l .pgdev\pg_start.log -w start
```

```powershell
venv\Scripts\python.exe migrate_sqlite_to_pg_259.py ..\snapshots\ssc_gate.db postgresql://postgres@127.0.0.1:5433/ssc_test --reset
$env:SSC_DB_URL="postgresql://postgres@127.0.0.1:5433/ssc_test"
venv\Scripts\python.exe tests\run_gate_260.py
```

31 suites, both backends, ~2 min (SQLite) / ~5 min (PG). A single suite:
pass its filename as an argument.

### Run a smoke test

Repository has two volume smokes in `tests/`:

```bash
python tests/smoke_dcr_volume.py             # 200 DCRs + 30 gap-fill cycles, ~7 min
python tests/smoke_dcr_backdated_30day.py    # 30 backdated days with manual labor, ~40 s
python tests/smoke_weekly_hours.py           # Weekly Hours Log + DCR consistency, ~30 s
```

Each self-manages the server lifecycle (starts a separate Popen,
`taskkill`s it on finally). PII-safe — synthetic IDs + counts only.
Validated-ceiling table in `TESTING_LIMITS.md`.

### Snapshot before any destructive op

```bash
# #248: snapshots live OUTSIDE the project root (never under a servable tree)
cp superstars.db "../snapshots/superstars-pre-<op>-$(date +%Y%m%d-%H%M%S).db"
```

---

## Builds #279–#283 (the current arc)

- **#279 — UI v2 toggle, phase 0.** Per-user Classic/New switch with silent
  fallback. There is **no Jinja in this app**: a v2 page is a twin file at
  `templates/v2/<name>`, resolved by `ui_version.resolve_page`, and the
  role-based SECTION stripping runs on whichever file is chosen.
- **#280 — Drawing markup / North elevation.** Traced North elevation, 12
  drops × 5 levels = 60 cells, per-cell work status with append-only
  events, plus comments (step 4) and RFIs (step 5). Introduced the
  `architect` role and its containment gate.
- **#281 — Portal foundation.** One shell served from two namespaces
  (`access.render_api_base`), the `client_payload()` field registry
  (provenance, not vocabulary — a field absent from the registry is never
  emitted), the Classic/New preview switch, four new grantable sections.
- **#282 — Verification + repair.** Audited what #280/#281 shipped
  straight to production. Found the containment lock existing only in a
  working tree (now committed), proved the Classic documents/photos chain
  untouched, and root-caused + fixed the DCR resequencer that had
  renumbered two real reports. Live numbering restored and re-rendered.
- **#283 — Fold-in (this build).** Grant wiring (above), two new guard
  suites, design-guard registration for the v2 tree, DCR artifact cleanup,
  `renumber_dcrs_by_date.py` retired, merge to main.

### Guard coverage added in #283

- `tests/smoke_markup_280.py` — elevation scoping, internal-vs-external
  vocabulary split, `internal_note` absence asserted **by key, recursively**,
  status-write validation and the reason law, drop-status derivation
  (five planted cell mixes), cell history audience filtering, comments
  (client read-only, rate limit, soft delete), RFIs (numeric allocation,
  role split, attach-later), and the grant wiring end to end.
- `tests/smoke_portal_shell_281.py` — the containment lock, pm scope,
  preview validation + audit on both paths, served-shell scrub asserted
  on the **DOM** (scripts stripped first — a view name inside a guarded
  `querySelector` is not a section), catalog shape, fail-closed registry.

Both are wired into `tests/run_gate_260.py` and both were proven by
planted regression: a payload leak and a fail-open registry each turn the
relevant suite red, and the code restores clean.

## Known gaps / operator decisions pending

- **RESOLVED (#284, operator-approved):** 2026-07-16 → **DCR-062** and
  2026-07-17 → **DCR-063** re-issued 2026-07-28 from surviving DB data
  through the standard pipeline (next-free numbers per immutability; 054/
  055 stay retired). Each report carries an Administrative issues-row note
  ("Re-issued 2026-07-28 from database records; original render lost in
  the 2026-07-23 renumbering incident."), rendered in BOTH audiences.
  **DCR-041** artifacts re-rendered at its existing number, content-
  verified. Audit: `dcr_reissue_284` / `dcr_rerender_284`. The quarantined
  originals at `snapshots/dcr_quarantine_283/054|055` are retained
  untouched. Tree verified: seqs 1–63 contiguous-by-allocation (54/55
  retired), zero unindexed dirs, zero missing renders.
- `report_index` id 11754 (2026-07-07) is a dead `no_work_pending`
  placeholder (no_work=0, seq NULL) beside that date's issued seq-47 rows —
  harmless promotion residue, excluded from contiguity checks. Delete or
  leave at leisure.
- The live architect user has **no `pm_project_assignment` row** — they
  cannot open any elevation until assigned to FR-BX-001.
- `.orphan_dst_058_*` — a PDF-only remnant whose identity could not be
  established (Edge PDFs use subset font encodings, so the text is not
  extractable). Quarantined, not destroyed.

## Pending work (by priority — pick from the top)

### The portal-flip track (#284 SHIPPED the core; remainder below)

- DONE 2026-07-28: matrix + nav-from-grants + progress/photos/documents/
  daily payloads through the registry + THE FLIP (clients land on
  `/portal/<code>`). The new shell's schedule view re-consumes Classic's
  curated `/api/portal/schedule`; drawing renders as a nav link when
  granted.
- REMAINING: weekly / materials portal payloads (catalogued, unserved);
  the fancy sections (weather, drops-by-status, progress-by-elevation)
  through the registry; architect grant machinery if the architect ever
  gets shell sections. (S/E/W elevation tracing is UNBLOCKED by #293 —
  the operator authors them from the uploaded set on /drawing-author;
  no more hand-seeding scripts.)

### Field-blocking before tomorrow's site test

- **#73 — Site Super onboarding flow.** The dashboard's "Onboard" button
  is wired (company-dashboard), but no end-to-end "I'm the site super,
  add me as a worker" walkthrough exists. Needs: a brief operator
  script + maybe a guided form, and a check that the new worker shows
  up in the labor manual-add dropdown and in the DCR labor section the
  same day.
- **#62 — Google Drive auto-archive (drive_targets.json `root` is
  empty).** Code is built (`drive_archive.py`, hooked into
  `_issue_one_dcr`). What's missing is the actual filesystem path to
  the Drive-synced "890 E 135th Street" folder. Install Drive for
  Desktop, sign in, find 890's folder in File Explorer, paste the
  path into `drive_targets.json` (the `_note` in that file is the
  cheat sheet). After that, every finalized DCR PDF auto-copies to
  `<root>/Daily Reports/`.

### Roadmap (not field-blocking, ordered)

- **#71 — Labor Sheet + pay rates** (company-console only). Per-worker
  hourly rate × weekly hours = gross labor cost; a comp-side view
  alongside the existing Weekly Hours Log. **Strict surface
  restriction**: never on the project dashboard, never field-reachable.
  See CLAUDE.md "Compensation / payroll data governance" section.
- **#72 — Comp-data governance / SQLCipher.** Encrypt the SQLite DB at
  rest (SQLCipher wrapper). BitLocker is the only encryption layer
  right now; a stolen workstation with BitLocker disabled exposes the
  DB. **Decision 2026-06-10 (#239):** the old pull-forward trigger
  ("before real pay-rate data lands") fired when Labor Rates (#220–#222)
  shipped — consciously NOT pulling SQLCipher forward. BitLocker covers
  at-rest on the workstation, and SQLCipher does not address the live
  attack surfaces (a running server reads a decrypted DB either way).
  Re-bundled into the **Backblaze backup task** — the point where DB
  copies first leave the BitLocker boundary; encrypted backups are the
  minimum bar there. New trigger: implement with (or before) Backblaze,
  not tied to Labor Rates.
- **#53 — Tailscale ACL.** Today the tailnet is open within the org;
  any device on the tailnet can hit the dashboard. Need ACL: which
  tailscale users / device-tags can reach `127.0.0.1:5050` via serve.
  Keep field devices to the project-dashboard surface only; keep
  company-console behind a stricter ACL.
- **Path B — `op run` integration with the scheduled task.** Currently
  the task launches without `op run`, so `ANTHROPIC_API_KEY` is unset
  → AI cert extraction returns 503. Path B injects the vaulted key at
  task start. Needs the 1Password CLI to be runnable from the scheduled-
  task context (S4U or stored credentials) — the design is in the
  `SECRETS_CHECKLIST.md` "vault pattern" section.
- **#64 — Live time model.** Today, `sign_in_log.time_in` /
  `time_out` are the BILLABLE in/out times the operator enters or the
  worker app records on sign-in. The roadmap adds a SEPARATE
  attendance-timestamp column so live-PIN attendance can layer on top
  without redefining what payroll reads. `payroll_hours.py` docstring
  flags this as a forward note; the schema migration + worker-app
  flow are TBD.

---

## Recent commits orientation — HISTORICAL (pre-#248 rebuild era)

The list below predates the auth/portal/estimating arc. For current
orientation use `git log --oneline -25` and the build summaries above.

### Older list (kept for the DCR-renderer context)

```
51ff3dc feat(deploy): continuous-run scheduled task — Path A, no `op run` (task #54)
717aeb0 feat(dcr): auto-fit-to-one-page when a report just barely overflows (≤1.25×)
c01cf01 fix(dcr): print insets match screen — PDF reads framed, not edge-to-edge
d6a65ea fix(dcr): preserve two-column layout in print/PDF (paired sections)
21efe9f fix(dcr): header title no longer clips at the right edge (browser + PDF)
0bddc00 feat(dcr): redesign render_dcr_html — black/red/white SSC brand + print pagination
9fc60c2 fix(dcr): use local-timezone today as date-picker default (#74)
8bbaf8a fix(dashboard): sweep remaining UTC toISOString() date sites (task #77)
626454f fix(workers): Onboard/Edit forms — relation reveal + live phone mask
0fe86ca feat(workers): emergency-contact relation dropdown (#13)
e5c397d refactor(payroll): "worked hours" terminology — rename helper + relabel
1ec775f feat(workers): "Add Worker"→"Onboard" + "Hired"→"Onboarded"
24aa693 feat(workers): display phone numbers as XXX-XXX-XXXX (Fix 2 / task #16)
d305a2b feat(workers): Worker ID (W-####) — schema, allocator, backfill, surfaces
c42856e test(payroll): PII-safe smoke for the Weekly Hours Log
a3dd977 feat(payroll): Weekly Hours Log frontend
f878fad feat(payroll): Weekly Hours Log backend
```

Earlier history (DCR + CoF + Company ID + 200-DCR smoke + Worker Intake
flows) extends back to the rebuild commit `683d574` (post-incident DB
restore from CSV).

---

## What to do on session start

1. Read CLAUDE.md (rules) + this file (state).
2. `git status` — should be clean, on `main`. **If anything is
   uncommitted, find out why before touching it**: #282 found production
   running a security lock that existed only in the working tree.
3. `git log --oneline -15` — orient on the last batch.
4. `Get-ScheduledTask "SSC Dashboard Server"` — confirm State: Running.
5. `Invoke-WebRequest http://127.0.0.1:5050/api/health` — should be 200.
6. If port 5050 is in use by an unexpected python.exe, tree-kill it
   (CLAUDE.md operational-discipline rule).
7. Wait for the user's first task — don't speculate.

### Non-negotiables when working in this codebase

- **Never run a suite or a migration against live.** Isolated copy via
  `SSC_DB_URL`, always. The gate runner refuses to start without it.
- **Snapshot before any live write** (`..\snapshots`, outside the served
  tree). A DB snapshot does NOT cover rendered artifacts — deleting a
  render dir is irreversible, so verify identity first or quarantine.
- **A running server has already imported the code.** On-disk edits do
  not reach it; restart per the #244 zombie-safe procedure and verify by
  API behaviour, never by the build stamp.
- New code goes through `db_layer` (never `sqlite3.connect` directly) and
  must pass on **both** backends.

---

*Last updated: 2026-08-06 — #293 S1 SHIPPED (b8f61c0): authorable elevations
(drawing sets + sheet picker + text-layer naming + AI-proposed trace + manual
authoring + draft/confirm). Gate is 40 suites, 40/40 green on BOTH backends.
Deployed via push; Render health-gates the swap (a #293 boot failure keeps the
old build serving — glance at the Render dashboard "Live" event for b8f61c0).
SESSION 2 NEXT: drop assignment onto authored grids, cell-level activation,
change-order items. The live architect still has no pm_project_assignment row.
Prior entry follows.*

*2026-08-03 — #291 SHIPPED (edc0a55): bootstrap aggregates
(mirror-not-door, guard #37), request-scoped db connection, instant-paint
sessionStorage cache (logout-purged, uid-guarded, preview-bypassed),
immutable private thumbs, edge-cached statics, lazy tiles. Measured: console
interactive halved (3.3s -> 1.59s p50 warm; target 1.0-1.5s NEAR-MISSED —
named offender: bootstrap's 8 parts run serially, next lever is
intra-aggregate parallelism); dashboard 2.5 -> 1.6s; repeat photo grids are
zero-request. Production = app.superstarscontracting.com (CF proxied),
workstation = parachute + dev. Gate is 37 suites both backends. PART B
hardening (Render PG credential rotation + external-access IP allowlist) is
operator-driven — walkthrough in the #291 close report; rotation invalidates
the terminal env + 1Password external-URL item until re-pasted. Standing
opens: Wix re-point, DKIM start-auth, staff 2FA + phone provisioning before
unsealing the worker door, Wed-crash diagnosis, first-invoice true-up.*