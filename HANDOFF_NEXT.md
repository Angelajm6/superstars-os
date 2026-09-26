# Handoff to the next Claude Code session

> **Read this + `CLAUDE.md` + `ROADMAP.md` at the start of every new session.**
> `ROADMAP.md` is what is PLANNED (C-suite asks + engineering order); this file is what has SHIPPED. Together
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

### #344 — DCR numbers: register, number preview, delete fix, one-time 109 → 097 (Bookore, 2026-09-26)

* **Ask (Angela):** "Issue New DCR" produced `DCR-FR-BX-001-109` where she expected 097, plus
  any Daily Report improvements. Decision with her: numbers stay in ISSUE order (next = MAX+1,
  deliberate since #282), the UI gets clearer, and 109 → 097 is fixed once if 097 is free.
* **Why 109:** MAX+1 — a report numbered 108 already existed. The list is windowed to the
  last 30 days, so whatever occupies 097–108 (backdated days? future-dated test reports?)
  was invisible. I can't query production (IP allowlist), so the fix runs inside the app.
* **`dcr_maintenance.py` (new):** `number_lock()` (Postgres advisory xact lock on
  `dcr-seq:<project>`, now taken by the issue route too — two issues in flight could read
  the same MAX), `audit()`, the **register** `GET /api/projects/<code>/dcr-register`
  (company roles) — every report by number with missing ranges, out-of-date-order, and
  future-dated flags — and the **pinned correction** `GET|POST
  /api/admin/maintenance/dcr-renumber-344` (admin only, FR-BX-001 109→097 hardcoded,
  refuses once its `audit_log` row exists). Preview returns what's in 097–108, whether 097
  is free, whether the files are on this server's disk, and a `plan_hash`. Apply:
  stage a copy in `097.tmp/` with the number rewritten (anchored `DCR-FR-BX-001-109(?!\d)`)
  in both HTML files + PDF regenerated (a PDF still saying 109 is never kept) → under the
  lock re-verify the hash → UPDATE both rows' report_id + dcr_sequence + audit → move `109/`
  to `data_room/_maintenance_backup/…` (not servable: outside `_ARTIFACT_SUBTREES`) → rename
  staging into place → commit; any failure puts the files back. Remove the pinned
  endpoints once it has run in production.
* **Delete fix (server.py):** both DCR delete routes now MOVE the files (HTML + the PDF the
  old code left behind) to the maintenance backup, remove the number's folder, and write
  a `dcr_delete` audit row with the deleted rows. Response shape unchanged; no paths on the
  wire.
* **Daily Report UI:** number preview above Issue ("Will be issued as …-098" / "This date
  already has …-097"), and the number in the confirm prompt; "Total reports" + header =
  whole project (`meta.total_project` on the list API, via `response_wrapper(**extra_meta)`);
  "Issued At" = `created_at` with "edited <date>"; PDF link per row (`pdf_url`); company
  roles get a numbering banner + "All numbers" panel (View / Delete per row — reaches
  reports dated outside the list's window) and, for admin on FR-BX-001, the one-time fix box.
* **Tests:** `tests/smoke_dcr_numbers_344.py` (both gates) builds the production scenario
  (096 + 109 real, 097–100 dated 2099, 101–108 unused) and drives preview → refusal →
  junk delete → stale-hash refusal → apply → second-run refusal → next number 098.
* **Operator runbook (production):** Render dashboard → take a DB backup → FR-BX-001 →
  Daily Report → All numbers → check what's in 097–108; delete test/duplicate reports only
  → the one-time box turns "Ready" → apply. If 097 is a real report it refuses; talk first.

### #343b (stage 1) — the project dashboard's content views onto the shared tokens (Bookore, 2026-09-25)

* **What:** 549 CSS color literals in `dashboard-static.html` rewritten to the widgets.css
  `:root` tokens. Untokenized CSS colors (excl. white) went 798 → 281. Two groups:
  (A) values that already EQUAL a token (`#B11E2E`→`--brand-red`, `#8C92A0`→`--muted`,
  `#ECEEF3`→`--line`, the status tokens, …) — zero visual change; (B) the four colors that
  WERE the old look — `#76777E` warm-gray text → `--muted`, `#E8E4DD` warm-beige borders →
  `--line`, `#14161C` → `--ink`, `#1E73E8` bright blue → `--ui` — plus the old blue's
  translucent tints `rgba(30,115,232,a)` → `rgba(36,65,140,a)` so solid/tint pairs stay
  coherent (navy text never sits on a bright-blue tint).
* **Rules the transform followed (reuse for stage 2):** CSS contexts ONLY — `<style>`
  blocks with `/* comments */` carved out (ticket refs like `#262` are valid 3-digit hex),
  and `style="…"` attribute values including ones built inside JS strings (escaped quotes
  handled). Never JS color literals (Chart.js / canvas / `EC_PALETTE`) or SVG presentation
  attributes — `var()` doesn't work there. A hex that is already `var(--x, #hex)`'s
  fallback is skipped (no nested `var()`). Every inserted `var()` carries an explicit
  fallback so print/popup windows without widgets.css still render. Verified: all 17
  inline scripts pass `node --check`; no JS reads computed CSS values back.
* **Deliberately unchanged:** `.btn-primary` buttons (Scan Receipt, the Lumen modal
  saves) stay azure `--accent` — the design guard requires it and it's the shared
  system's split (azure = widget/modal primary, navy `--ui` = nav/links/hero actions),
  product-wide, not a sweep decision.
* **Stage 2 (open):** the 281 long-tail literals are status/semantic colors needing a
  per-use call — old greens `#2F7C57`/`#0F6E56`, ambers `#B68838`/`#BA7517`, reds
  `#A8382B`/`#A23B30`/`#7A2E1B`/`#C8102E` (older brand red), warm grays `#F1EEE8`/`#54505A`/
  `#8F8675`, light fills `#F5F6F8`/`#F3F3F5`/`#F4F6F9`, plus old-palette colors that live
  in JS/SVG (Chart.js colors, the Drop Report sync icon stroke).

### #343 — One click into a project, one visual language (Bookore, 2026-09-25)

* **Ask (Angela):** from the Projects tab you were "within a project already" but still had
  to open the project dashboard as a separate entity, and that dashboard looked outdated
  next to the new UI. Option 2 of three scoped (full rebuild and iframe-embed deferred).
* **The hop is gone:** a Projects-tab row now opens `/projects/<code>` directly. The
  portal's in-between job summary page (`loadProjectDetail`, `#project-detail`, its
  "Open job dashboard →" button) is deleted; the portal router forwards any
  `#projects/<code>` link (Overview list, calendar bars/milestones, old bookmarks) to
  `/projects/<code>` with `location.replace`.
* **Nothing lost:** that page's contract / invoices / AIA apps / permits package /
  documents expiring / job facts / crew / client contacts is now a "Contract, paperwork
  & crew" block on Project Health, right under the ops KPIs (static, not a GridStack
  widget — the saved-layout keys are untouched). Same endpoint,
  `GET /api/company/portal/projects/<code>`, unchanged. Loader
  `window.PROJ_loadFinancialSummary` runs on load and on returning to Project Health.
* **Gating:** the block (markup, its `<style>`, its script, even its explanatory comment)
  sits inside `<!-- SECTION:company_portal:start/end -->` — deliberately the SAME key the
  endpoint is gated on (`requires_section("company_portal")`, admin/c_suite), not
  `financial` as first planned, so the HTML gate and the API gate can never drift apart.
  pm/super receive none of it; the view switcher only names the loader behind a
  `typeof` guard (same pattern as `SOV_load`).
* **Chrome:** brand lockup now matches `shell.css .ssc-side .brand` exactly (34px mark,
  uppercase 13px Archivo wordmark, uppercase sub) — correction to the plan: the
  wordmark was ALREADY Archivo; the visible mismatch was size, case and the 24px mark.
  Account avatar `#1E73E8` → `var(--ui)` (was the only old-blue left beside the
  already-`--ui` active nav row). Account menu, live chip, eyebrow labels and the
  pinned-Settings divider moved onto `--side-*` / `--muted` / `--status-complete`.
* **Tests:** `smoke_portal_projects_330.py` — the served-portal asserts flip (no
  summary page, row → `/projects/`, router forward) and a new group proves c_suite gets
  the card + loader while a pm assigned to the same job gets none of it and still 403s
  the endpoint. `run_gate_ci.py` 35/35. Live walkthrough: Projects row → dashboard in
  one click; card renders a real onboarded job; computed avatar colour == active-nav
  colour (`rgb(36,65,140)`); `#projects/<code>` forwards.
* **Not in this phase — #343b:** the ~20 operational content views (Daily Report, RFI
  Log, Drop Report, …) still carry most of the file's hardcoded hex (10 `<style>` blocks;
  Materials & Deliveries and parts of Payments/SOV already use the shared tokens). A
  view-by-view token sweep is the follow-up; this phase fixed the chrome and the hop.

### #340 — Navigation revamp phases 2 + 3: Finance consolidation + project sidebar cut (Bookore, 2026-09-22)

* **Ask (Angela):** "let's ... finish up the phase 2 and 3 of the navigation" — Finance as
  one tab (phase 2) and the project dashboard sidebar cut to ~15 entries (phase 3), per the
  plan approved alongside #339.
* **Phase 2 finding — already shipped by #339, no code change:** the company Finance tab
  (`static/js/company_nav.js`) already covers bills/payables, Expenses, Weekly payroll and
  Pay rates as ONE tab with four sub-destinations (`sub: [['What needs to get paid', ...],
  ['Expenses', ...], ['Weekly payroll', ...], ['Pay rates', ...]]`); `company_portal.html`'s
  `view-expenses` already titles itself "Finance · Expenses" (`TITLES.expenses`), not a
  standalone concept. Verified via code read, no gap found — phase 2 was complete before
  this session started.
* **Phase 3 — `dashboard-static.html` project sidebar, 9 groups / 29 entries → 6 groups / 21
  entries:** removed the 8 dead "Coming with Phase 2B" placeholder stubs outright (their
  `<section class="view">` content panes too, not just the nav links): Subcontractors &
  COIs, Site Closure, Meeting Minutes, Equipment Log, Inspection Log, Punch List, Warranty &
  Closeout, Client Portal. Regrouped the 21 remaining REAL views into 6 groups: **Overview**
  (Project Health), **Field** (Employees & Certs [moved from the old People group],
  Drawing Markup, Project Documents, Field Photos, Drop Report/Plan, Schedule), **Daily**
  (Daily Report, Weekly Summary, Materials & Deliveries, Toolbox Talks), **Tracking** (RFI
  Log, Violations, Compliance & Permits [moved from the old standalone Compliance group]),
  **Money** (Schedule of Values, Payments, Expenses, Product Usage — merged the old separate
  Financial + Expenses nav-groups into one; both were already gated by the same
  `SECTION:financial` marker, so the merge is one gate instead of two; dropped the redundant
  client-side `exp-nav-group`/`revealForAdmin` toggle since the group now relies on the same
  server-side strip the SOV/Payments items already used), **Setup** (Templates, Settings).
  `SECTION:internal_ops` / `SECTION:financial` marker pairing was re-verified with the exact
  regex from `access.py` (`_SECTION_RE`) after every edit — a first pass accidentally
  orphaned the Employees & Certs content pane's closing marker, caught and fixed before
  shipping.
* **Not done (deliberately out of scope):** did NOT build the "Documents folder-based page"
  / "Money group Billing+Costs sub-tab" ideas from the original audit — those are net-new
  feature surfaces, not navigation reorganization, and would need their own scoping pass.
  The floor for further entry-count reduction is ~20 real, actively used destinations; going
  lower means either building those net-new surfaces or deciding to merge/hide real
  capability, both of which need explicit sign-off first.
  **Correction (2026-09-24):** the "Client access page" idea below was a stale assumption —
  it already exists and has since build #270 (2026-07-02), well before this session's work.
  `/admin/projects` ("Assignments & onboarding" under the Projects tab) has a full "Client
  Access" section: per-client project assignment, per-section grant toggles (DEFAULT OFF),
  one-click presets (Minimal/Standard/Full), and a "👁 Preview" button that opens the portal
  exactly as that client sees it. Backed by `client_grants.py` + `/api/admin/pm-assignments`
  (which already merges in `grantable_sections`/`presets`/each client's `sections`). No gap
  here — do not rebuild this.
* **Tests:** `tests/smoke_behavior_conventions.py` (`rbac_pm_sidebar_has_compliance` now
  checks `data-view="compliance"` instead of the retired `data-group="compliance"`;
  `rbac_sidebar_split_two_groups` checks the merged `data-group="financial"` +
  `>Money<` label against the ungated `data-group="tracking"` group; also fixed
  `m_admin_console_home_nav`, a STALE #339 regression this session's gate run caught for the
  first time — it checked for the pre-#339 `href="/dashboard"` + `class="brand-home"`
  escape hatch on `/admin/users`, which #339's sidebar swap had already removed in favor of
  the shared `company_nav.js` nav; nothing was actually broken for the user, just the test).
  `tests/smoke_portal_shell_281.py` dropped its now-nonexistent subs/equipment/site-closure
  per-role-strip checks (those views no longer exist for ANY role, not just external ones).
  `tests/run_gate_ci.py`: 35/35 pass. `tests/run_gate_260.py` (the RBAC/behavioral operator
  gate) could not be run end-to-end in this environment — it needs a seeded `FR-BX-001`-style
  project + pm assignment that only exists on the real workstation DB; verified the sidebar
  markup directly instead (regex-simulated the `access.py` SECTION strip, then a live
  logged-in-admin browser walkthrough of every Money/Tracking/Field destination — all
  loaded real data with no console errors beyond a pre-existing unrelated `/api/weather` 400).
* **Not in this phase (next):** the "Documents folder-based page" and Money's Billing/Costs
  sub-tab split remain open ideas from the original audit if the entry count needs to drop
  further — each is a scoping conversation, not a mechanical nav change. ("Client access
  panel" is NOT on this list — see the correction above, it already ships on
  `/admin/projects`.)

### #339 — ONE home: the company sidebar, navigation revamp phase 1 (Bookore, 2026-09-22)

* **Ask (Angela, after the audit + Arun's 2026-09-22 meeting):** too many tabs, two homes
  (Office Console vs Company Portal) with every concept in both, job sites confusing, CRM
  unnecessary, quick actions unnecessary. Decisions: calendar lives inside Projects;
  Workforce stays top-level; "exterior", never "facades" (Arun).
* **What:** `static/js/company_nav.js` is the ONE definition of the company-level sidebar —
  seven tabs (Dashboard · Projects · Clients · Proposals · Finance · Workforce · Company),
  each with its sub-tab row (`<div class="ssc-subtabs" data-subtabs="<tab>">`), rendered on
  the portal (`/company`), the console (`/`) and the four admin pages from
  `<nav data-company-nav>` + `SSC_NAV.mount()`. Active tab / sub-tab / crumb / h1 follow
  `pathname + hash`; roles via the existing `sscNavForRole` hook + `ssc:user-loaded`.
  Include it SYNC in `<head>`: it also owns the redirects that retire the old homes
  (`/`, `/#projects` → `/company#overview`; `/#crm` → `/company#clients`; portal
  `#buildings` → `#projects`, `#compliance` → `#company`), and the console's hash router
  defers to `SSC_NAV.redirectIfRetired()` before its own "no such panel → home" fallback.
* **Retired:** the console's Dashboard panel (KPIs, widget grid, Quick actions) and CRM
  panel + module (`window.CRM`); the portal's Job sites view, modal and loaders; every
  per-page copy of the sidebar and `sscNavForRole`. The console's remaining panels
  (Onboarding & directory, Sign-ins, Certifications, Pipeline, Weekly payroll, Compliance
  documents, Specs, Settings) are sub-tabs of their tabs.
* **Portal:** "This week" (#338) moved to the company Dashboard (`loadWeekStrip`, same
  API); Clients gets **+ Add client** (organization → optional main contact) and the client
  page gets **+ Contact / + Note / + Follow-up** and a "done" on each follow-up, all on the
  existing `/api/crm/*` endpoints (`requires_section('crm')`, unchanged); the client page
  lists projects instead of job sites; the #337 notice keeps its "Create project" buttons
  (no job-site page to link to). Company view = the old Compliance & Vault.
* **Words:** `estimates.EST_TYPES["FR"]` = "Exterior Restoration"; `project_type_config`
  label "Exterior Restoration / Recladding" (id stays `facade`); console `DIV_LABEL`.
  The project dashboard's back link → `/company#overview`; its cert links → `/#workforce`,
  `/#cert-library`.
* **Tests:** `tests/smoke_one_home_339.py` (gate + CI). #329's guard retired with the
  console it guarded (file removed from both gates); #328 / #330 / #337 / #338 / #319 /
  #302 assertions follow the shared sidebar. Static assets cache for 600 s: bump the
  `?v=` on `company_nav.js` whenever it changes (it is `339b` now for that reason).
* **Not in phase 1 (next):** Finance as one tab with real sub-pages (#340), the project
  sidebar cut to 15 (#341), integrations (Claude proposal agent, QuickBooks).
* **Known pre-existing:** the console's `/api/workers/intake-summary` answers 400 on an
  empty roster (console error on the Onboarding page; not new).

### #338 — "This week" on the Office Console Dashboard (Bookore, 2026-09-21)

* **Ask (Angela):** a calendar snippet on the main dashboard as an overall view.
* **What:** a one-row Monday–Sunday strip between the KPI tiles and Active Projects on
  `company-dashboard.html`, reading the same `/api/company/portal/calendar` window the
  portal calendar uses (admin / c_suite only — the console's audience). Job bars in the
  calendar's colours (lane-packed, 4 lanes then "+N more" → the calendar), today
  highlighted, per-day "N on site" and up to three milestone chips, a footer with the
  count and the undated-jobs pointer to Crew & site. Header link opens
  `/company#calendar/<monday>`; a bar opens `/projects/<code>` (the console's convention).
  No view switcher, drag, weather or print here — those stay on the calendar. EN / ES.
* **Test** `tests/smoke_dashboard_week_338.py` (gate + CI).

### #337 — Job sites → projects in one click; the ledger and boot-ensured schema (Bookore, 2026-09-21)

* **Incident:** "Arun added projects on production yesterday and they're gone." Production
  request log (Render API): Sunday 14:27–14:33 UTC, five `POST /api/buildings` 201 —
  job sites through the portal's "Add job site" — and NO onboarding request anywhere.
  #330 then turned the Projects tab into a list of real jobs, so the sites left that
  tab. Nothing was deleted; they sit under Projects → "Job sites".
* **Fix:** `GET /api/company/portal/projects` meta gains `sites_without_project`
  (unarchived buildings with no project). The Projects tab shows a notice listing them
  with **Create project**; a job site's page shows **Create project here** when it has
  no project. Both link to `/admin/projects?new=1&address=…&borough=…&client_org_id=…&name=…`,
  which opens "+ New project" prefilled (`openNewProjectWith`; borough matched by label
  or code; the query string is cleared). Onboarding auto-links the new project to the
  existing job site by address (`buildings.find_or_create`), so nothing typed is lost.
* **Ledger:** production's `/api/health` said 4 migrations pending although the app had
  created their tables at boot. `migrations.BOOT_ENSURED` maps each such script to the
  objects its module ensures ("table" or "table.column"); `boot()` records pending
  scripts whose objects all exist (note `boot-ensured`). Add an entry when a new module
  ensures its own schema. Production reads 0 pending after this deploy.
* **Test** `tests/smoke_job_sites_337.py` (gate + CI).
* **Lesson:** a rename that changes what a list contains needs a bridge for the rows
  people already entered. Reading production's request log through the Render API
  (`/v1/logs` with `path` / `method` / `statusCode` labels; `RENDER_API_KEY` from the
  shell env, never printed) answered "what did they actually do" in minutes.

### Release to production — 2026-09-21, #336 (Bookore)

* Upstream PR `superstars-contracting/dashboard#10`: the building in 3D, stage 1.
  Approved by Angela. Both gates green (exit code checked). Merged `061c27f`; Render build
  stamp 14:01:27 UTC; health 200, `/files/static/vendor/three.module.min.js` 200 and the
  model route 401 anonymous on `app.superstarscontracting.com`. `preprod` = `main`.
* Earlier the same day: #333 + #334 (PR #8), #335 (PR #9), #330–#332 (PR #7).

### #336 — The building in 3D, stage 1 (Bookore, 2026-09-21)

* **Ask (Angela, 2026-09-21):** the client-portal drawing markup shows the building as
  flat blocks; clients want a real, navigable 3D view they can click into. Option A was
  chosen (no vendor, no key, nothing for Amit to set up).
* **Where the shape comes from** (`building_3d.py`): NYC Open Data *Building Footprints*
  (`5zhs-2jue`, fields `bin, height_roof, ground_elevation, construction_year, the_geom`)
  and NYC Planning *GeoSearch* (address → point + BIN). Lookup order: linked
  `building.bin` → geocode the address → the job's site point (`intersects`). Neighbours
  = footprints within 90 m (≤ 60). Cached per job in `building_geometry`
  (`apply_building_geometry_336.py`, ledger + boot ensure; 30-day TTL; a failure is
  stored and retried after 6 h; `?refresh=1` / `POST …/refresh` for internal roles).
  Both providers are free and anonymous; the fetcher is injectable, the test stubs it.
* **The model** `GET /api/elevation/model3d/<code>` — registered UNDER `/api/elevation/`
  on purpose: that prefix is what the client (`client_portal`, needs the `drawing`
  grant) and architect (`elevation._ARCH_ALLOW_PREFIXES`) allowlists admit; a route under
  `/api/projects/` 403s for them before it runs. Payload: footprint rings and neighbours
  in LOCAL METRES around the centroid (x east, y north), height in metres, `walls`
  (longest edge per compass direction with outward normal + bearing), the job's
  elevations with a compass code (`_face_code`: 'N', 'North', 'East (courtyard)' …),
  `mapping` compass → the first confirmed traced elevation (drafts hidden from external),
  a `fallback` box sized from the traced facades (used outside the city / on failure).
* **The viewer** `static/js/building3d.js` (ES module) over vendored
  `static/vendor/three.module.min.js` (three 0.169.0, MIT, from the npm build;
  sha256 f7cee3c7…72ee). Extrudes the footprint and neighbours, drapes each mapped
  elevation's drop × level grid on its wall (traced facade width/height scaled to the
  real wall), colours cells by status tone (page CSS vars), orbit / zoom / pinch, N-E-S-W
  fly-to, face labels, hover tooltip, click → `onPick(elevId, dropId, cellId)`.
* **The page** (`templates/v2/drawing-markup.html`): a "3D" toggle in the toolbar shows
  `#stage3d` in place of the sheet; the module is imported on first use; picking a cell
  drives the SAME state (`S.drop`, `load(elevId)`, `renderPanel`) so the right-hand panel,
  paint bar and picker follow. `window.__markup3d` exposes open/close/state.
* **Test** `tests/smoke_building3d_336.py` (gate + CI).
* **Verified live:** 890 E 135th St resolves to BIN 2004029 (82.67 ft) with 21
  neighbours from the demo database; the North elevation drapes on the north wall.
* **Next stages (roadmap Phase D):** photoreal skin from a capture (Stage 2) — the
  Google 3D Tiles toggle was declined to avoid a vendor account; condition overlay
  (Stage 3).

### #334 — projects.start_date / target_completion on Postgres; calendar never 500s (Bookore, 2026-09-21)

* **Symptom (Angela, preprod screenshot):** Calendar → "Could not load the calendar ·
  Internal server error". The portal's Projects tab (#330) read the same columns.
* **Cause:** the two columns come from `schema_project_assignments.sql`, applied only by
  the sqlite-only legacy `apply_assignments_schema.py`, which joined the fixture list in
  #310 (2026-09-18). Preprod's Postgres was built 2026-09-04, so it never had them; every
  CI fixture (built after) did, so both gates were green. #322 fixed the
  `project_assignments` TABLE from the same script but not these two ALTERs. Production
  (built from the real database, where the legacy script had run) has them.
* **Fix:** `apply_project_dates_334.py` (`ensure` adds both as TEXT, dual-backend; in
  `DUAL_BACKEND_MIGRATIONS`; called at boot from `project_onboard.register` next to the
  #325 ensure). `portal_calendar.window` no longer compares the columns with `''` in SQL,
  treats unparsable dates as undated instead of raising, and the API returns
  `calendar failed: <type>: <msg>` (logged) instead of a bare 500.
* **Rule (third time):** a column or table the app reads must be ensured dual-backend at
  boot. Grep LEGACY_MIGRATIONS for anything else the app depends on before trusting a
  green Postgres gate — the gate's fixture is newer than preprod and production.
* **Test** `tests/smoke_project_dates_334.py` (gate + CI).

### #333 — Calendar extras: milestones, headcount, weather, drag, print (Bookore, 2026-09-21)

* **Ask (Angela, 2026-09-21):** "implement your calendar ideas".
* **Data (`portal_calendar.window`)**, per day: `on_site` {code: distinct workers from
  `sign_in_log`} + `headcount`; `milestones` = non-superseded `project_documents` with an
  `expiry_date` in the window (`late` when past) + `issued` invoices with `due_at` in the
  window (balance, client). Drafts and superseded documents never appear.
* **Weather** `GET /api/company/portal/calendar/weather?from&to` (≤ 14 days): one call
  per day through `server.fetch_open_meteo_weather` (30-min cache; a failing day is
  left out), labelled with `server._WMO_LABELS`; coordinates = the latest active job
  with a site, else the home borough. `portal_calendar.weather(conn, s, e, fetch=)`
  takes the provider as a parameter so the test stubs it — no network in CI.
* **UI:** amber `◆` chips for expiring documents, blue `$` for invoices due, red when
  late (click → the job); "N on site" badge in the day header with a per-job tooltip;
  the weather strip under the day headers on 3-day / week only; drag a bar to move the
  whole job, drag its right grip to change the end → `PUT /api/admin/projects/<code>/crew`
  with an Undo link in the toast (a click after a drag is swallowed); "Print" builds a
  jobs table + a by-day table and prints the view (`@media print` hides the shell).
* **Test** `tests/smoke_calendar_plus_333.py` (gate + CI).
* **Left out on purpose:** weather on the month view (too noisy); drag on touch (works
  with a mouse / trackpad; phones open the job instead).

### Release to production — 2026-09-21 (Bookore)

* Upstream PR `superstars-contracting/dashboard#7`: #330 portal project pages (+ the
  Postgres test fix), #331 Long Island / New Jersey, #332 jobs calendar. Approved by
  Angela. Both gates green before the merge (checked by exit code this time). Merged
  `488f320`; Render build stamp 08:24:54 UTC; health 200 on the Render hostname AND on
  `app.superstarscontracting.com` (the Cloudflare edge was fine this morning).
  `preprod` fast-forwarded to `main`.
* **After-deploy chore for Arun:** existing jobs have no target completion; set them
  under Projects & assignments → Crew & site so the calendar bars get an end.

### #332 — The jobs calendar (Bookore, 2026-09-21)

* **Ask (Angela for Arun, 2026-09-21):** a calendar showing which jobs run on which
  day, overlaps, and each job's duration; 3-day / week / month views; every job
  clickable through to its page; a newly created job with dates appears by itself;
  clean and easy to scan.
* **Data** `portal_calendar.py`: `GET /api/company/portal/calendar?from&to` (company
  section; window ≤ 120 days). One bar per job from `projects.start_date` to
  `target_completion`; an active job without an end is `open_ended` (drawn to the edge
  of the window with a fade); a closed job without an end ends the day it was closed
  (`updated_at`); active jobs without a start come back as `undated` (the page says so
  and links to Crew & site). `days[date].reports` = project codes with a DCR that day.
  Colour = stable hash of the code. No PII.
* **Dates travel:** "+ New project" gained *Target completion*; `onboard()` stores it
  (refuses end < start). Crew & site (`GET/PUT /api/admin/projects/<code>/crew`) returns
  and edits `dates` (start + target completion, same rule) — that is where existing
  jobs get their end date.
* **UI (`company_portal.html` → Calendar, nav entry under Company):** segmented 3 days
  / Week / Month (remembered per browser), ‹ Today ›, range title, Monday-first grid,
  weekends tinted, today highlighted, bars packed into lanes per row (month caps at 3
  lanes + "+N more" → week view on that day), continuation edges when a bar crosses a
  row, closed jobs dimmed, hover card (client, address, dates, duration, PM, crew),
  daily-report dots per day in the job's colour, legend chips; click anywhere on a job
  → `#projects/<code>`. `#calendar/YYYY-MM-DD` deep-links to a date. Windows are cached
  per range while the view is open.
* **Test** `tests/smoke_calendar_332.py` (gate + CI).
* **Not in this ticket (ideas offered):** drag to move / stretch a bar; milestones on
  the grid (COI / permit expiries, invoice due dates); on-site headcount from the
  worker app per day; weather strip on the week view; a printable "this week" sheet.

### #331 — Job locations beyond the five boroughs (Bookore, 2026-09-21)

* **Ask (Angela, 2026-09-21):** pick Long Island or New Jersey (not only the five
  boroughs) when entering a project's location.
* **One list:** `regions.py` (`LABELS` code → name, `CODES`, `label()`, `options()`).
  `estimates.BOROUGHS` now IS `regions.CODES` (+ `BOROUGH_LABELS`), so the estimate
  code series simply gains `<TYPE>-LI-###` and `<TYPE>-NJ-###`; `buildings._BOROUGH_CODES`
  reads the same map. Adding a region = one line in `regions.py`, no schema change
  (`estimate.borough` is free text, `building.borough` stores the label).
* **Metas** `/api/estimates/meta` and `/api/admin/projects/onboard-meta` carry
  `regions: [{code, label}]` next to the old `boroughs` codes.
* **Pickers:** "+ New project" and the console's "New estimate" render
  "Long Island (LI)" style options from the meta; the portal's job-site modal lists Long
  Island and New Jersey. Onboarding stamps the region label on the auto-created job site
  when it has none.
* **Test** `tests/smoke_regions_331.py` (gate + CI).
* **Not in this ticket:** Westchester / Connecticut (one line each when asked); the
  city / state / ZIP split already accepts NJ addresses (`_split_city_zip`).

### #330 — Company Portal → Projects = jobs; a job's own page (Bookore, 2026-09-20)

* **Ask (Angela, 2026-09-20, screenshot):** inside a project in the portal, none of the
  containers were about that project (open estimates, won value, other projects at the
  address). Cause: the tab was the #304 job-site (building) roll-up, renamed "Projects"
  in #328. A new job's page must show only that job.
* **Module** `portal_projects.py` (read-only, company-portal section):
  `GET /api/company/portal/projects?status=active|closed|all&search=` — one row per job:
  client, PMs (`pm_project_assignment`), crew count (active `project_assignments`),
  contract (`billing_sov` sum when it exists, else the converted estimate's
  `final_amount`), invoiced / open / overdue / collected (`invoice`), last DCR
  (`report_index`), open RFIs (`rfi_log`). `GET …/projects/<code>` — the page: identity,
  client + contacts, PMs, crew (name / trade via `v_active_workers`, no paths), job site
  row, contract, money (`invoice_docs.project_payments`), AIA summary + applications
  (`aia_billing.billing`), permits & insurance readiness (`doc_packages.package`, with
  days_left computed here), documents with expiry dates, activity (DCRs in 30 days, last
  DCR, open RFIs, photos, documents), links (dashboard `/projects/<code>`, crew & site,
  `#buildings/<id>`, `#clients/<id>`). Every optional piece is read through `_safe` so a
  missing table blanks one card, never the page.
* **UI (`company_portal.html`):** nav "Projects" → `#projects` (list: job, address,
  client, PM, crew, contract, open invoices, last report, status; Active / Closed / All;
  "Job sites" and "+ New project" buttons). `#projects/<code>` = the job page: toolbar
  (status, type, Crew & site, Open job dashboard →), KPIs (contract, invoiced,
  collected, open, balance-to-finish or crew), Money / Permits & insurance / Documents
  expiring / Crew cards, Job facts, Activity, Client contacts. `#buildings` (job sites)
  keeps its pages, titled "Job sites", lit under Projects in the nav.
* **Test** `tests/smoke_portal_projects_330.py` (gate + CI): two jobs at one address,
  the page for one never shows the other's invoices or documents. `smoke_nav_clarity_328`
  updated for the new hash.
* **Not in this ticket:** editing a project from the portal (Crew & site stays on the
  console); daily-report list on the page (the job dashboard has it).
* **Postgres lesson (the PR's postgres gate failed and was merged anyway because
  `gh pr checks --watch | tail` hid the exit code):** the fixture-built
  `project_assignments` carries a FOREIGN KEY to `employees` on Postgres (the legacy
  `apply_assignments_schema.py`), SQLite does not enforce it without `pragma_fk`. A
  test that assigns crew must seed `employees` rows (see #325). Fixed the same night in
  the follow-up PR. Rule: never pipe `gh pr checks --watch` through `tail`; read `$?`.

### Release to production — 2026-09-20, third of the day (Bookore)

* Upstream PR `superstars-contracting/dashboard#6`: #317 AIA billing + the Payments
  loader fix, #319 accounts payable + "Overview" → "Dashboard". Arun approved on
  preprod. Merged `cd0f9cf`; Render build stamp 15:36:44 UTC; `/api/health` → 0
  pending migrations on the Render hostname; the four new tables were created at boot.
  `preprod` fast-forwarded to `main`.
* **The custom domain was down again at release time** — `app`, `www` and the apex all
  time out on ports 80 AND 443 at Cloudflare's edge (188.114.96/97.x) while
  `https://ssc-dashboard-go4h.onrender.com` answers in 0.2 s and other
  Cloudflare-fronted sites load fine from the same network. Same zone-wide pattern as
  2026-09-19 (which cleared on its own). It is the company's Cloudflare zone (Amit),
  not the app. Workaround: the Render hostname. Durable fix: the `app` record as a
  DNS-only (grey cloud) CNAME to `ssc-dashboard-go4h.onrender.com`, or pause Cloudflare
  on the zone.

### #319 — Accounts payable + the 30-day cash view; "Overview" → "Dashboard" (Bookore, 2026-09-20)

* **Ask (C-suite 2026-09-11 item 6: "accounts receivable and accounts payable … a view
  of what needs to get paid, any due invoice"; Angela 2026-09-20: rename Overview).**
* **Module** `payables.py`; table `payable` in `apply_payables_319.py` (dual-backend
  list; boot heals): vendor, source (`bill` | `receipt` | `subscription`) + `source_ref`,
  doc number, description, project, category, amount, paid_amount, issued / due / paid,
  method, reference, status (`open` → `paid` when the balance hits zero, or `void`).
  Nothing is created automatically — a receipt paid at the counter is not a payable —
  the operator picks from candidates: scanned `expenses` of the last 90 days not yet
  logged, and active `subscriptions` renewing inside 45 days not yet logged for that
  renewal date (`source_ref = "<id>:<renewal_date>"`).
* **Cash view** (`payables.summary`): open / overdue / due-7 / due-30 bills, unlogged
  renewals inside 30 days as projections, receivables due inside 30 days from the
  #315 invoices (`invoices.receivables_summary` + open invoices with due_at ≤ +30),
  `cash_30 = {in, out, net}`, and one `upcoming` list (payable | renewal | receivable)
  in date order. The Overview payload carries it (`overview.payables`, lazy import so
  a missing table never breaks the portal); "Money at a glance" shows "Bills to pay"
  and "Cash, next 30 days".
* **Routes** (`requires_section('company_portal')`): `GET /api/company/portal/payables
  ?status=open|overdue|paid|void|all&search=`, `POST …/payables`, `PATCH …/<id>` (open
  only), `POST …/<id>/pay` (partial ok; overpay → 400), `POST …/<id>/void` (no payments
  only), `POST …/from-receipt/<expense_id>`, `POST …/from-subscription/<sub_id>`.
* **UI (`company_portal.html` → Finance):** "What needs to get paid" card at the top:
  six KPIs (bills open, overdue, due in 7 days, out / in / net next 30 days), the bills
  table with Pay… / Void, the "Coming up" table, "+ New bill", "From a receipt…", "Log a
  renewal…" pickers and the Record-a-payment modal.
* **Rename:** Office Console first tab + `ssc-top-h1` "Overview" → "Dashboard" (ES
  "Panel"); the four admin sidebars; Company Portal nav / title / `TITLES`. Hashes
  (`#projects`, `#overview`) unchanged.
* **Test** `tests/smoke_payables_319.py` (gate + CI). The `subscriptions` table is not
  in the fixture lists: the test creates a minimal dual-backend copy when missing.
* **Not in this ticket:** vendor statements / aging report; attaching the vendor's PDF
  to a bill; paying from the app; QuickBooks bill sync (#316 phase 2).

### #317 — AIA progress billing, G702 + G703 (Bookore, 2026-09-20)

* **Ask (C-suite 2026-09-11 item 5, priority list item 3 "AIA payments"):** the
  official application-for-payment documents, on top of the invoice model.
* **Module** `aia_billing.py`; tables in `apply_aia_317.py` (dual-backend list; boot
  heals): `billing_sov` (the BILLING schedule of values per job: item no, description,
  scheduled value, kind base | change_order, approved_on), `pay_application` (number per
  job, period, application date, retainage % work / stored, `invoice_id`),
  `pay_application_line` (this_period + stored per SOV item). The drop-plan
  `sov_line_items` (unit rates) is untouched; "Import drop-plan lines" copies its
  descriptions in at $0.
* **Arithmetic** (`compute`): G703 columns C–I per line; G702 lines 1–9. "From previous
  application" (D) and line 7 are recomputed from the earlier issued / paid applications
  every time — a voided application drops out. Retainage on the invoice = total
  retainage now − previous certificate's retainage.
* **Rules:** one draft per job; D+E+F ≤ C per line (400 "exceeds its scheduled value");
  an SOV line on any application cannot be removed (set it to 0); an application with
  an invoice is read-only; delete = draft only; void = through the invoice
  (`POST /api/invoices/<id>/void`) and the application reads `void`.
* **Issue** = `invoices.create(kind='progress', amount=line 8, retainage=period
  retainage, source_doc='AIA G702/G703 · Application #n')` + `invoices.issue` on the
  application date. **The invoice document IS the G702 + G703**:
  `invoice_docs.render_document` delegates to `aia_billing.render_document` when the
  invoice belongs to an application, so preview / PDF / email / `/i/<token>` / view
  tracking / Payments / Receivables are all the #326 paths. `/api/billing/applications/<id>/preview`
  renders any status (draft watermark).
* **Routes** (`requires_company` = admin / c_suite): `GET /api/projects/<code>/billing`,
  `PUT …/billing/sov` (replace-by-id), `POST …/billing/sov/import`,
  `POST …/billing/applications`, `GET | PUT | DELETE /api/billing/applications/<id>`,
  `POST …/<id>/issue`, `GET …/<id>/preview`.
* **UI (`dashboard-static.html`, Financial group — server-stripped for pm/super):**
  Schedule of Values view = the billing SOV editor (lines, change orders, import, save;
  contract sums) with the drop-plan unit rates below it. Payments view = "AIA progress
  billing" card (contract to date, completed & stored, retainage held, balance to
  finish; applications table; G702/G703 + PDF links) and the application modal: period,
  dates, retainage %, the G703 grid with editable E / F and live G / % / H / I, live
  G702 lines 1–9, Save draft / Save & issue invoice / Delete draft.
* **Test** `tests/smoke_aia_billing_317.py` (gate + CI): fixed-number arithmetic for two
  applications, carry-forward, void, over-billing, documents, gates.
* **Not in this ticket:** the client-portal view of applications (they get the email +
  link); lien waivers; a G702 "amount certified" that differs from the amount applied
  (the owner marks that on paper today); QuickBooks sync (#316 phase 2).

### #329 — Office Console condensed (Bookore, 2026-09-20)

* **Ask (Angela, 2026-09-20, "go with the list"):** fewer sidebar entries on the
  Office Console. 16 → 12.
* **Grouped nav tabs.** A `.nav-tab` may carry `data-panels="a,b"`: `activatePanel`
  lights the tab for any of its panels and titles the page with the tab's label
  (`_tabOwns`). Grouped panels show `.ssc-subtabs` (plain hash anchors, so hash
  routing does the switch; `.ssc-subtab.active` follows the panel).
  * **Certifications** = `cert-library` (+ `#cert-health`, which was a Phase-B
    placeholder panel: markup removed, hash aliased to the library).
  * **Compliance** owns `compliance` + `specs` (Specs Library is its second tab;
    `SPECS_loadCatalog` still fires on `#specs`).
* **Moved, not removed.** "Estimating queue →" sits in the Estimates page header;
  "Open interface preferences →" is an Interface section on Settings; Drop Plan is
  reached from each project (sidebar + Overview cards). All URLs unchanged.
* **Admin pages' own sidebars** (`admin_projects`, `admin_users`, `estimating`,
  `admin_labor_rates`) match: Certifications; no Cert Health / Specs Library /
  Drop Plan; the "Estimating queue" entry is `data-roles="estimator"` only (it is
  the estimator's one link and must stay for that role).
* **Test** `tests/smoke_console_condense_329.py` (gate + CI).
* **Left alone on purpose:** the overlap between the console tabs and the Company
  Portal (Finance / Expenses / Workforce & Pay / Compliance). Which surface owns
  each topic is a C-suite decision, noted in ROADMAP.

### #328 — Navigation clarity (Bookore, 2026-09-20)

* **Ask (Angela, 2026-09-20):** too many tabs; the company portal's "Buildings"
  should read "Projects"; "Labor Rates" (console) vs "Labor Rate Tracker" (project
  sidebar) was two names for two different things; inside a project you could not
  tell which job you were in once you left Project Health.
* **Project identity block** (`dashboard-static.html`, `.proj-line` under the brand):
  code chip (brand red, it is an ID), name, address, client — on EVERY view. Filled
  server-side by `access.render_project_identity`, which gained a
  `data-project-address` fill (`server._project_identity` reads `projects.address`
  defensively; the address is dropped when it equals the name). The browser tab is
  named `CODE · Name — Superstars Contracting`. On phones (#312 compact bar) the
  line stays visible as one row (`widgets.css`, `?v=328`).
* **Pay Rates vs Weekly Payroll.** `/admin/labor-rates` is now "Pay Rates" (set and
  audit each worker's hourly rate) on the console nav and on every admin page's own
  sidebar. The sheet formerly called "Labor Rate Tracker" (hours × rate per week,
  the check-cutting view) is "Weekly Payroll" (ES "Nómina Semanal") with its own
  visible console tab under Workforce next to Hours Log — admin / c_suite only,
  revealed together with Pay Rates. Its link is GONE from the project sidebar: it is
  company-wide, not per project. `#labor-rate` hash and every API path unchanged.
* **Company portal:** the "Buildings" tab / title / links read "Projects"; the
  address entity itself is worded "job site" (columns, buttons, empty states). Hash
  routes (`#buildings`, `#buildings/<id>`), `/api/company/portal/buildings*` and the
  `building` table are unchanged — labels only.
* **Test** `tests/smoke_nav_clarity_328.py` (gate + CI): boots the real app; served
  project page carries the filled identity block for c_suite and pm and no LRT link;
  console has the Weekly Payroll tab + Pay Rates and no old labels; portal nav says
  Projects; Pay Rates page heading; renderer unit checks.
* **Not in this ticket:** condensing the Office Console tabs (proposal sent to
  Angela for the C-suite to pick).

### Release to production — 2026-09-20 (Bookore)

* **What:** the whole preprod stack #302–#327 (26 fork PRs #15–#40) promoted to
  production after the C-suite's yes. Upstream PR
  `superstars-contracting/dashboard#4`, branch `release/prod-2026-09-20` =
  `preprod` + a merge of `main` (the #310 / #310b hotfixes were on both under
  different SHAs; the three conflicts were resolved with the preprod side and the
  result is content-identical to preprod). Gate passed on SQLite and Postgres in
  the upstream repo (first run of `gate.yml` there). Merged `d6d40a8`; Render
  deploy `dep-danqruqjnfac7393dkr0` live 09:58 UTC; `/api/health` →
  `migrations_pending: 0`; boot logs clean; new public/gated routes answer
  (`/d/`, `/i/` → 404 on bad tokens, package / invoices / portal APIs → 401 anon).
* **Schema on production:** no manual migration was run and none was needed —
  every migration new to production (#304, #307, #315, #318, #325, #327) is a
  wrapper around a module ensure that also runs at boot; #322
  `project_assignments` already existed there. The #306 ledger was created and
  baselined on that first boot, so its `baseline` rows for those seven are
  truthful only because of the boot ensures. **Rule going forward:** a migration
  whose module does NOT ensure at boot must be applied to production by hand
  (`python migrations.py apply` from the allow-listed SSC workstation) BEFORE the
  release boots, or the baseline will silently mark it applied.
* `preprod` was fast-forwarded to `main` (`d6d40a8`) so the two are identical;
  branch new work from `preprod` as usual.
* `app.superstarscontracting.com` answered 200 over HTTPS again at release time
  (the 2026-09-19 Cloudflare edge failure had cleared).

### #327 — Permits & insurance package, send to client (Bookore, 2026-09-19)

* **Ask (C-suite via Angela, 2026-09-19, priority item 2):** a section within each
  job for the permits and insurance documents (from the broker) that secure the
  job, delivered to the client.
* **What existed:** Project Documents (#229 / #271) — categories, a required-docs
  checklist per category, uploads, versions, per-document client sharing
  (#270 visibility engine). What was missing: the job-securing subset as one
  readiness view, and a way to hand it to the client in one go.
* **Module** `doc_packages.py`. `PACKAGE_KEYS` = COI + bond (CONTRACTS) and every
  PERMITS requirement; un-keyed PERMITS documents ride along as extras.
  `GET /api/projects/<code>/documents/package` (admin / c_suite / pm / super):
  items with missing / on_file / expiring / expired, extras, summary, the
  client + contacts, `last_sent` (sent_to, viewed_at, view_count, link_active)
  and `sends`. `POST …/package/send` (admin / c_suite / pm): recipient (body,
  else the client's first contact), message, `document_ids` (default = every
  deliverable document; a red-flagged or out-of-package id → 400),
  `share_in_portal` (default true → `visibility.share(...'client')` so the
  portal's Documents section shows them). Each send is its own
  `document_package` row + items with a 90-day link `/d/<token>`; older links
  keep working until they expire. Email lists the documents; CRM activity row.
* **Public** `/d/<token>` and `/d/<token>/file/<id>`: no login, no-store,
  noindex, first open recorded; a file is served only if it is in the package,
  not superseded and not red-flagged — a flag pulls a document from every sent
  package at once. `/d/` added to the public, containment and client allowlists.
  Tables in `apply_doc_packages_327.py` (dual-backend list; boot heals).
* **Project dashboard → Project Documents:** a "Permits & insurance — what
  secures this job" card under the readiness hero (per-item pills, extras, last
  sent / opened, "Send to client…" modal with the client's contacts as
  suggestions, checkboxes per document (expired ones unticked), "also show in
  the client's portal").
* **Test** `tests/smoke_doc_package_327.py` (gate + CI) boots the real app and
  uploads through the real endpoint.
* **Not in this ticket:** editing the package definition per job (it is the
  taxonomy's PERMITS + COI + bond); reminders when the COI nears expiry (the
  Compliance view already lists it); requesting the COI from the broker by email.

### #326 — Invoice documents, email to client, project Payments (Bookore, 2026-09-19)

* **Ask (C-suite via Angela, 2026-09-19, priority item 3, without QuickBooks):** a
  payment section per job holding invoices, emailed straight to the client.
* **Module** `invoice_docs.py` on top of #315. Columns added to `invoice`
  (idempotent via `invoices.ensure_schema`): sent_at, sent_to, sent_name,
  sent_count, viewed_at, view_count, pdf_path, pdf_rendered_at, token_hash,
  token_expires_at, last_message. `pdf_path` / `token_hash` never leave the
  server — `invoices._wrap` scrubs every payload through `invoice_docs.public`
  (flags `has_pdf`, `link_active` instead).
* **Document:** `render_document` — the proposal sheet (same `_DOC_CSS`), kind
  heading, bill-to, project, amount / retainage / paid / balance due, payments
  received, "How to pay" (app_settings `payment_instructions`, else the default
  check / ACH line), a paid / void / draft stamp. `render_pdf` uses the app's
  engine (best effort; absent on macOS, present on Render); the gated
  `/api/invoices/<id>/pdf` renders on demand and 503s without an engine;
  `/api/invoices/<id>/preview` is the HTML.
* **Send** (`POST /api/invoices/<id>/send`, admin / c_suite): issued or paid
  only (draft → 409 "issue it first", void → 409). Recipient = body, else the
  last send, else the proposal's recipient, else the client's first contact
  with an email (`GET …/recipient`). EVERY send mints a fresh 180-day link
  `/i/<token>` and the previous one dies; email (SendGrid live, fake dir in
  tests) carries the link, amount due and due date; a CRM activity row.
* **Public** `/i/<token>` (+ `/pdf`): no login, no-store, noindex; first open
  recorded (`viewed_at`, `view_count`); void → 410, expired → 410. Prefix `/i/`
  added to `auth._PUBLIC_PREFIXES`, `role_containment._COMMON_PREFIXES` and
  `client_portal._CLIENT_ALLOW_PREFIXES` (a logged-in client / estimator reaches
  it too).
* **Project dashboard → Financial → Payments** (`GET /api/projects/<code>/payments`,
  `requires_company`; the whole Financial group is server-stripped for
  pm / super): KPIs (invoiced, open, overdue, collected, drafts), the job's
  invoices with Preview / PDF / Issue / Email client / Resend / Payment /
  Email receipt, New invoice (prefilled with the job + client), send modal with
  the client's contacts as suggestions, payment modal.
* **Company Portal:** the invoice detail modal gains Preview / PDF / Email to
  client / Resend / Email receipt and shows sent / opened; the receivables table
  shows sent / opened under the status.
* **Test** `tests/smoke_project_payments_326.py` (gate + CI).
* **Not in this ticket:** a client-portal Payments tab (the link is the client's
  copy for now); QuickBooks (#316 — blocked on the QBO admin app);
  AIA G702 / G703 (#317 — fills a `progress` invoice); online card / ACH payment.

### #325 — Crews, site coordinates, and the worker app's job picker (Bookore, 2026-09-19)

* **Ask (C-suite via Angela, 2026-09-19, priority item 1):** once a job is added, the
  daily report and the labor portal must work for it immediately.
* **What was wrong:** the worker app was single-site by construction — the login
  geofenced every worker against the first Bronx job's hard-coded coordinates and
  returned `FR-BX-001` as the project; shift start trusted whatever project code
  the client sent. Nothing about a new job could reach the field.
* **Schema:** `apply_project_site_325.py` adds `projects.site_lat / site_lng /
  geofence_m` (dual-backend, in `DUAL_BACKEND_MIGRATIONS`; boot heals through
  `project_onboard.register`) and seeds the legacy Bronx coordinates onto
  FR-BX-001 when it has none — production's existing control is unchanged the
  day this lands.
* **Worker login (`/api/worker/login`):** `_worker_projects()` = the worker's
  ACTIVE assignments on active projects (a worker with no assignment at all is
  offered every active project — the office tightens this by assigning crews),
  each geofenced against ITS OWN coordinates (default radius 200 m; a project
  with no coordinates on file is eligible as is). No candidates → 403 "No job
  assigned — see the office"; none in range → 403 "Not on site". Exactly one
  eligible → `project_code` auto-picked; several → `project_code: null` and
  `projects: [...]` (code, name, distance, in_range) for the chooser. The
  session row stores `''` until the worker picks.
* **Shift start (`/api/worker/session/start`):** `project_code` is required and
  must be one of the worker's jobs (403 otherwise); the pick is written to the
  session row; an auto-picked session cannot be redirected to another job.
* **Worker app:** a "Choose your job" screen (EN/ES) between PIN and the shift
  when more than one job is in range; the working screen shows the job name.
* **Office:** the New project modal gains Site location (lat / lng / radius,
  "Use my location" reads the phone's GPS — the PM taps it standing on site) and
  a Crew checkbox list (active roster, W-#### — Name). Every project row on
  Projects & assignments gains "Crew & site" (`GET/PUT
  /api/admin/projects/<code>/crew`, admin / c_suite): PUT makes the active
  assignments equal to the list — new ones start today, dropped ones are
  off-rolled with an end date, never deleted (hours history).
* **Test** `tests/smoke_crew_sites_325.py` (gate + CI) boots the real app.
* **Preprod note:** its workers had no assignments until #322 created the
  table today, so on preprod every worker is currently offered every active
  project; assign crews there before demoing the picker.
* **Not in this ticket:** address → coordinates lookup (no geocoding key on
  file; "Use my location" covers the field case); a crew editor on the
  Workforce tab (the per-worker project checklist at onboarding still exists).

### #324 — "+ New project": onboard an already-won job (Bookore, 2026-09-19)

* **Ask (Angela, 2026-09-19):** "figure out how to add / onboard more jobs." Until
  now every project had to travel estimate → approved → convert — right for a
  bid, four screens and a fake estimate for a job won before the dashboard
  existed or agreed verbally.
* **What:** `project_onboard.py` — `POST /api/admin/projects/onboard` (+
  `GET …/onboard-meta` for the selects), admin / c_suite via
  `requires_company`. One call: (1) `estimates.create_estimate` in the job's
  series so numbering stays consistent, (2) stamped approved on the start date
  with the contract value and the note "Onboarded directly as an already-won
  job" so Finance's won / pipeline figures stay truthful, (3)
  `estimates.convert_to_project` (building auto-link, IRA checklist row), (4)
  the operator's project name + start date and the chosen PM written to
  `pm_project_assignment`; a CRM activity row on the client. Client is an
  existing active org or a new name (creates the org as a client). Validation
  rejects before anything is written.
* **Page:** Projects & assignments gains "+ New project" (head row) and a modal
  (client / new client, job type, borough, address, name, contract value, start
  date, PM, notes); success toasts the code and reloads the lists. The head
  copy now says new bids start in Estimates and won jobs onboard here.
* **Test** `tests/smoke_project_onboard_324.py` (gate + CI): gates, meta
  filters (active clients, real active PMs), validation with nothing left
  behind, the happy path end to end, PM visibility via `pm_scoping`, numbering
  in both directions, new client, IRA row, page markup.
* **Not in this ticket:** editing a project's name / dates after creation (the
  console's project settings are unchanged); bulk import from a spreadsheet —
  worth adding if the C-suite has more than a handful to onboard at once.

### #323 — Project-health buttons washed out on hover (Bookore, 2026-09-19)

* **Reported by Angela on preprod:** hovering "Drop Plan" under Project Health
  faded the button to a pale box with unreadable text.
* **Why:** `[data-link]:hover { background: rgba(177,30,46,.04) }` (a generic
  row-hover rule in `dashboard-static.html`) has the same specificity as
  `.ph-actions .ph-btn` and comes later, so it replaced the button's solid blue
  with a near-transparent wash under white text.
* **Fix:** `.ph-actions .ph-btn[data-link]:hover` → `--ui-deep` with white text;
  the ghost variant fills `--ui-soft` with blue text. The hero's
  `.hero-btn-primary:hover` still carried a #313-era red; it is `--ui-deep` now.

### #322 — project_assignments missing on preprod Postgres (Bookore, 2026-09-19)

* **Found in the Render logs** after the #321 deploy: `GET /api/payroll/hours:
  relation "project_assignments" does not exist`, repeatedly between 09:12 and
  09:23 ET on 2026-09-19 — the console loads that endpoint on every visit, so
  the Labor Rate Tracker showed an error to whoever was on preprod.
* **Why:** `apply_assignments_schema.py` opens superstars.db with sqlite3
  directly (LEGACY list) and so never ran on the Postgres database; #310 only
  added it to the SQLite fixture. Everything else the payroll pool needs
  (`v_worker_roster`, `worker_rates`, `labor_worker_state`) is present.
* **Fix:** `apply_assignments_322.py` — dual-backend, idempotent, no FKs —
  appended to `DUAL_BACKEND_MIGRATIONS`; `SSC_AUTO_MIGRATE=1` applies it at
  the next preprod boot and the #306 ledger records it. A no-op on SQLite.
* **Lesson:** a table that lives only in a LEGACY (sqlite3-direct) migration is
  invisible to Postgres. When a feature starts depending on one, add a dual-
  backend twin the same day.

### #321 — Light sidebar (Bookore, 2026-09-19)

* **Decision (Angela, 2026-09-19):** the ink-black sidebar was the heaviest element
  on every screen; of three tones shown (light grey, soft blue, brand paper) she
  chose **light grey**. Tokens in `widgets.css` `:root`: `--side-bg:#F3F4F6`,
  `--side-edge:#E3E5EA`, `--side-ink:#2B2F38`, `--side-muted:#8A8F99`,
  `--side-hover:rgba(20,22,28,.05)`.
* **One language, four families:** `shell.css` (`.ssc-side` — console, Company
  Portal, admin Users / Projects / Labor Rates, Estimating), the project
  dashboard's and the client portal's `.sidebar` re-skin blocks. Brand name in
  ink, section labels muted, links in `--side-ink`, hover = faint ink wash +
  2px nudge, **active = `--ui-soft` fill, `--ui` text and a 3px `--ui` left
  edge** (the indicator was white on ink). A 1px `--side-edge` border replaces
  the dark block's hard edge; at phone width it moves to the bottom of the
  brand bar. The `.ssc-menu-btn` is now white with an ink label and turns
  `--ui-soft` on hover. The client portal's Sign out button is white with the
  blue text. Cache versions bumped to `?v=321`.
* **Unchanged:** the red star, every top bar / page head, the LRT hero and
  everything inside the content column; the login page.

### #320 — Worker job history + payroll view (Bookore, 2026-09-19)

* **What:** ROADMAP Phase C — one read model per worker in `worker_history.py`,
  served at `GET /api/company/portal/workers/<employee_id>?months=12` (1..36,
  clamped) behind the company-portal gate (admin / c_suite). Company-only per the
  comp-data rule: never linked from the project dashboard, worker app or any
  client surface.
* **Payload:** `worker` (id, W-####, name, trade, status active | inactive |
  archived, current rate + its effective date), `summary` (hours in the window /
  YTD / all time, days on site, est. labor cost + unrated hours in the window,
  projects total / active, first + last on site, certs total / expired /
  expiring, credentials issued, pending rate changes, hours corrections),
  `projects` (per project: hours, distinct days, first / last date, est. cost,
  unrated hours, plus the assignment's role / status / dates merged in — an
  assignment with no sign-ins still shows), `by_month`, `matrix` (project ×
  month hours), `recent_days` (last 30 days of sign-ins with the rate applied),
  `certifications` (with expired / expiring / valid state), `credentials`
  (Certificate of Fitness cards — no path columns), `rate_history`
  (`worker_rates`, `current` flag) and `rate_changes` (the approval audit).
* **Money rule:** worked hours × the rate effective ON THE WORK DATE via
  `company_portal._rate_on` — the same rule as the Finance view's labor cost: a
  raise splits the cost at its boundary; a worker with a rate on file but a
  sign-in older than every effective-dated row prices at the current rate (the
  fallback, deliberately shared); a worker with no rate at all reports unrated
  hours and no cost. It is an estimate of labor cost, never payroll; labels say
  "worked" and "est. cost".
* **Portal (Workforce & Pay):** every worker row links to `#workforce/<id>`;
  the detail page has six KPIs, a months selector (6 / 12 / 24 / 36), Projects
  worked, Hours by month (bars + table), Hours by project and month (matrix),
  Last 30 days, Rate history + Rate changes, Certifications, Credentials issued.
* **Test** `tests/smoke_worker_history_320.py` (gate + CI). Optional sources
  (`worker_rates`, `project_assignments`, `cof_cards`, `labor_rate_change`,
  `sign_in_edit`) degrade to empty when a fixture lacks the table.
* **Not in this ticket:** CSV export of the per-worker history; a payroll-period
  view (the Labor Rate Tracker on the console remains the check-cutting sheet);
  edits — everything here is read-only.

### #318 — Company vault (Bookore, 2026-09-19)

* **What:** ROADMAP Phase C "Company vault" — Amit's "everything that has to do
  with the company" (2026-09-11 notes). `company_vault.py` adds `vehicle` and
  `company_document_file`, and `vehicle_id` / `doc_year` on `company_document`
  (idempotent ADD COLUMN on both backends). Migration
  `apply_company_vault_318.py` (appended to `DUAL_BACKEND_MIGRATIONS`); boot
  heals through `company_portal.ensure_schema`, which now calls the vault's.
* **Categories:** `COMPANY_DOC_CATEGORIES` grew vehicle_registration /
  vehicle_insurance / vehicle_inspection / vehicle_title / vehicle_other,
  tax_return_federal / tax_return_state / tax_filing_receipt / payroll_filing,
  and company_record. `doc_group()` maps every category onto one of four
  shelves (insurance, vehicles, tax, records) — the portal's chips; the
  documents list carries `group`, `vehicle_label`, `doc_year`, `files`,
  `file_count`, and accepts `?group=` / `?vehicle_id=`.
* **Vehicles:** label, year/make/model/color, plate (normalised), VIN, status
  active|retired, driver = `employees.employee_id` (active roster only, offered
  as "W-#### — Name" in `meta.drivers`). The list rolls up the newest
  registration / insurance / inspection per vehicle with days left, `missing`
  for an active vehicle with no such document, and `next_expiry_days`. Retire
  is single-shot and makes the row read-only; its paper stays in the vault.
* **Files:** `POST …/company-documents/<id>/files` (multipart `file`; PDF /
  JPG / PNG / HEIC / XLSX / DOCX, 25 MB, empty rejected, archived document →
  404), stored as `data_room/company_vault/<doc id>/<uuid>.<ext>` (original
  name never in the path), read ONLY through the gated
  `…/company-documents/files/<id>` (inline, no-store, path-contained), delete
  removes row + disk file. `file_path` never leaves the server.
* **Compliance:** a vehicle document expiring shows as kind `vehicle` with the
  vehicle label as subject (`subject_ref` = vehicle id); other company paper
  keeps kind `company_document`. Overview's expiring counts include both.
* **Portal (Compliance & Vault):** KPI row gains Vault documents (+ files
  attached) and Vehicles (+ documents missing); a Vehicles card (plate, driver,
  status, the three document columns with pills, "+ Paper" pre-selects the
  vehicle in the document modal, Edit, Retire); the vault card gets shelf chips
  and a Files column. The document modal shows Vehicle for vehicle categories,
  Tax / filing year for tax ones, and, when editing, the attached files with
  Upload / Remove.
* **Test** `tests/smoke_company_vault_318.py` (gate + CI). `#302`'s suite is
  unchanged and still passes on the extended schema.
* **Not in this ticket:** no reminder emails for vehicle renewals (the
  compliance digest is a separate item); mileage / maintenance logs; a per-file
  expiry (expiry lives on the document, not the file).

### #315 — Invoices & receivables (Bookore, 2026-09-19)

* **What:** ROADMAP Phase B "invoices & receivables model" (the roadmap's old #310;
  renumbered — #309–#314 were consumed by the September UI / ops tickets).
  Money-in only: `invoice` + `invoice_payment` in `invoices.py`, migration
  `apply_invoices_315.py` (appended to `DUAL_BACKEND_MIGRATIONS`; boot ensures
  the schema too). Numbers are `INV-YYYY-####` with a numeric max (CLAUDE.md ID
  rule). Lifecycle draft → issued → paid, or draft / issued → void; `overdue`
  and `days_overdue` are derived at read time, never stored.
* **Rules that matter:** payments land only on an issued invoice and never
  exceed the open balance; the invoice flips to paid when they cover it; a paid
  deposit flags the IRA job's `deposit_received` when the project is known;
  void refuses anything with payments; delete is draft-only; issued invoices
  accept only due date / notes / description edits (amount is frozen). Default
  terms on issue: net 30, deposits net 7.
* **The trigger:** `proposals._on_signed` now calls
  `invoices.create_from_proposal` (idempotent per proposal) so the "Issue deposit
  invoice" CRM task names a real draft. Signature → estimate approved → COI task
  → deposit invoice draft, all in one commit path; failures there are logged and
  never block the signature.
* **API** (`requires_section('company_portal')`, admin / c_suite): `GET/POST
  /api/invoices` (list carries `meta.summary`), `GET/PATCH/DELETE
  /api/invoices/<id>`, `POST …/issue`, `POST …/payments`, `POST …/void`.
  Filters: status (open|draft|issued|paid|void), kind, client_org_id, project,
  proposal_id, overdue=1, search.
* **Company Portal:** Finance gains "Receivables — invoices" above the IRA card
  (six KPIs, search / status / kind filters, New invoice, row → detail modal
  with Issue / Record payment / Void / Delete draft). Overview's money list
  shows Open invoices + Collected YTD; needs-attention lists overdue, drafts
  waiting to be issued and due-within-30. `#finance/overdue` and
  `#finance/draft` preselect the filter. A signed proposal's detail links its
  deposit invoice. `overview()` / `finance()` payloads gained `invoices` /
  `invoice_summary`; the IRA `receivables` keys are unchanged (#302 test pins
  them). Both helpers degrade to empty when the tables are missing.
* **Test** `tests/smoke_invoices_315.py` (gate + CI): gates, validation,
  numbering, issue terms, edit rules, payments, overdue math + summary buckets,
  void / delete, the proposal trigger, the IRA flag, portal payloads, no
  `*_path` keys.
* **Not in this ticket:** the client portal does not show invoices yet (needs a
  grant section like proposals); no PDF / email for invoices (QuickBooks sends
  them in #316); no payables (#319).

### #314 — Gradients and clean hovers on every surface (Bookore, 2026-09-18)

* **Ask (Angela, 2026-09-18):** the production Labor Rate Tracker hero (3px accent
  bar + a soft blue radial wash fading to white) is the look to carry everywhere,
  plus the three hover behaviours from the CSS options reviewed the same day.
* **Shared classes in `widgets.css`:** `.ssc-hero` (wash + bar), `.ssc-tile`
  (calm-blue inward vignette, same recipe as `.ph-tile` / `.shc-tile`), and the
  hover set — **lift** (`.hv-lift`: -2px translate + layered shadow) for tiles and
  cards, **edge** (`.hv-edge`: 3px `--ui` indicator growing to 60% + `--ui-soft`
  tint) for link rows, **tint** (`.hv-fill`: `--ui-soft` cell background, no
  movement) for dense table rows. Selector lists opt existing markup in without
  touching HTML: `.cp-kpi`, `.prp-card`, `.pm-card`, `.shc-tile`, `.kpis .tile`,
  `.project-card` lift; `.cp-list li:has(a)` and `.doc-row` edge; `#lrt-grid`,
  `#hrs-table`, `.shc-dcr-table`, `.cp-table` and the admin `.wrap table` rows
  tint. `@media (hover:none)` removes lift/edge on touch; reduced-motion already
  zeroes the motion tokens.
* **Hero bands applied:** Company Portal first card per view (`.cp-view` rules) +
  KPI vignette; client portal `.v-head`; the admin `.head-row` on Users / Projects
  (`shell.css`); Labor Rates `#lr-admin-only` frame + its KPI tiles; Estimating
  `.hero`; project dashboard `.hero` (wash over the cream band, gradient top
  border). LRT itself is unchanged. Cache versions bumped to `?v=314`.
* **Verified:** browser on all seven surfaces; CI gate 16/16; perf budget 41/42
  (the one failure is the known local `/api/weather` 400 — macOS SSL).

### #313 — Interactive accent moves from brand red to a dark blue (Bookore, 2026-09-18)

* **Decision (Angela, 2026-09-18):** navigation and interactive accents are a dark
  blue; brand red stays for identity (the star, worker IDs and codes, top bars on
  documents and emails) and for error / danger states. New tokens in `widgets.css`
  `:root`: `--ui:#24418C`, `--ui-deep:#1B3272`, `--ui-soft:#E8EDF8`, `--ui-edge`.
  The auth pages (login / forgot / reset / set-password) carry their own `:root` and
  received the two tokens too.
* **Switched:** shell nav active pill + top-bar user-chip links (`shell.css`); the
  client portal's nav active/hover, sign-out, Open buttons and the proposal Accept
  button; the project dashboard's nav active, avatar, header CTAs, sign-out; the
  console's legacy tab underline, `.btn`, language toggle, project-card hover, Hours
  Log week buttons and cell-editor buttons, onboarding camera / save buttons, spec
  add button; the auth pages' primary buttons, links, focus rings and the
  password suggester; the public proposal page's Accept button and the two email
  CTA buttons (reset + proposal). Cache versions bumped to `?v=313`.
* **Deliberately unchanged:** `.cp-id`, `.cc-wid`, `.ph-wid`, estimate codes and
  CRM names (identifier red per the design rule), status reds (`--bad`, behind,
  no-work banners), the Hours Log red table header + grand-total cell, and the red
  top bars on the proposal document / emails.

### #312 — Collapsible menu at phone width (Bookore, 2026-09-18)

* Below 860 px every sidebar is a compact brand bar with a **Menu** button; the nav
  and footer stay hidden until tapped, rise in, and close on a nav tap, Escape, or
  when the window widens. `static/js/shell.js` (one small deferred script, loaded by
  all eight sidebar surfaces) creates the button and toggles `.nav-open`; it handles
  both families — the shell (`aside.ssc-side` + `.brand`) and the project dashboard /
  client portal (`.sidebar` + `.sidebar-header`) — without touching their markup.
* Styles: `.ssc-menu-btn` + the `.sidebar` phone rules in `widgets.css`; the shell's
  phone block in `shell.css` replaced the old wrapping strip. The project dashboard's
  own sidebar breakpoint moved from 768 to 860 px to match.
* Budgets: +1 static request per surface, all inside the existing ceilings (no
  changes to `smoke_perf_budget_292`).

### #311 — UI coherence: one shell, one motion language (Bookore, 2026-09-18)

* **Every operator page is now on the shared shell.** `admin_users.html`,
  `admin_projects.html`, `estimating.html` and `admin_labor_rates.html` dropped their
  one-off header rows for the same ink sidebar + top bar the Office Console and the
  Company Portal use (`shell.css`). The sidebar markup is inline per page (no extra
  request beyond `shell.css`); links carry `data-roles`, and a tiny inline
  `sscNavForRole(role)` hides what the signed-in role cannot open (the server gates
  remain the control — this is navigation hygiene, not access control). Labor Rates
  had no shared tokens at all; it now loads `typography.css` + `widgets.css` too, its
  local palette still applying inside the content column. Stale build stamps hidden.
* **Project dashboard + client portal** keep their markup and JS; CSS-only re-skins
  give them the same sidebar language (ink background, brand-red active pill with the
  white indicator, quiet hover). The blue stays the widget accent inside content.
* **Motion layer** (`widgets.css`, so every page has it): `ssc-rise` view/panel enter
  (220 ms), hover lift + press sink on `.btn-primary/.btn-ghost`, card hover shadow,
  table-row hover, focus rings, `.ssc-skel` shimmer, `.ssc-pill` variants, and a
  `prefers-reduced-motion` switch that zeroes every duration. Console panels,
  Company Portal views, client portal views and project-dashboard views all rise in.
* **Shell additions** (`shell.css`): nav active indicator + hover nudge, `.ssc-user`
  top-bar chip (name · role pill · sign out), `.ssc-page-head`, and the `.wrap`
  rhythm so a legacy content column matches the console.
* **Budgets**: `smoke_perf_budget_292` ceilings raised by exactly the new static
  requests (users/projects/estimating 5→6; labor rates 6→9 + 60 KB). API sets are
  unchanged. Design guard: logo images on the four pages declare their box.

### #310 — The payroll week is Monday through Sunday (Bookore, 2026-09-18)

* **Why:** crews now work weekends; the Labor Rate Tracker / Hours Log only showed
  Mon–Fri, so Saturday and Sunday hours had nowhere to be logged or paid.
* **`payroll_hours.py`:** `WEEK_DAYS = 7`; `week_dates()` returns Mon..Sun;
  `last_completed_week()` resolves to the prior Mon..Sun from ANY day of the
  current week (Sunday included — the week is still in progress); `totals_by_day`
  sized from the dates. `build_week_grid` is otherwise unchanged: every consumer
  (grid, CSV, PDF timesheet, comp overlay) iterates `dates`, so weekend hours are
  counted, totalled and priced at the per-day rate like any other day.
* **`server.py`:** the sign-in/DCR divergence + reconcile windows for a `week_start`
  now run Monday..Sunday (were Mon..Fri). Docstrings updated.
* **`company-dashboard.html`:** day labels are seven (EN + ES), both week-range
  labels end on Sunday, the LRT colgroup is sized from the dates, Sat/Sun header
  cells are tinted (`.lrt-weekend`; Hours Log shows a small WEEKEND caption) and
  edit exactly like weekdays. **"Full week" still fills Mon–Fri only** — weekend
  hours are entered on purpose, cell by cell or with that day's "All full day".
* **Untouched by design:** the weekly summary report keeps its own Mon..Sun date
  list; the look-ahead's working-day math (Mon–Fri) is a schedule concern, not
  payroll.
* **Tests:** `tests/smoke_payroll_week_310.py` (both gates; boots `server.app`) —
  helpers, 7-day grid with weekend hours counted + priced, default week, CSV
  columns, divergence window, non-Monday 400. `tests/smoke_weekly_hours.py`
  updated to the seven-day contract (operator gate).

### #309 — C-suite can reset a client's password; strong-password suggester (Bookore, 2026-09-18)

* **Why:** a customer was locked out and the only account able to issue a one-time
  password was the single production admin login. `auth_admin.CSUITE_RESET_ROLES =
  ('client',)`: `/admin/users`, the list and `…/reset-password` now accept `c_suite`;
  the list is filtered to client accounts for them and the reset handler refuses any
  other target role (403). Creating accounts, changing roles and deactivating remain
  admin-only; the admin row is never reset from the page by anyone. The page renders a
  "Client accounts" reset-only view for c_suite (no Create, no role/deactivate buttons).
* **Strong-password suggester:** `static/js/pw_suggest.js` (`SSCPasswordSuggest.generate
  / wire`) — three random dictionary words + two digits (`Harbor-Copper-Stone-47`,
  always ≥ 12 chars with a letter and a digit, from `crypto.getRandomValues`). Wired on
  `reset_password.html` and `set_password.html`: the button fills both fields, reveals
  them through the existing eye toggles, re-runs the checklist and shows a "save it"
  hint. No server involvement.
* **Test** `tests/smoke_csuite_reset_309.py` (both gates): role matrix on every users
  endpoint, c_suite list filtering, reset allowed/denied by target role, page wiring +
  generator contract (node when available).

### Ops — warnings mirrored to the host log stream (Bookore, 2026-09-18)

`server.py` logging: the file under the data root (`/var/data/server.log` on Render)
remains the full INFO record; WARNING and above are now ALSO written to stderr, which
Render collects into the service's Logs tab. Motivation: a customer's password-reset
email never arrived and the only record ("password reset email not sent via
sendgrid …" or "delivery failed") lived on the disk, unreadable without a shell.
From this build, Render's log search for `password reset` answers it. No new data
leaves the building: the warnings never carry an email address (PII rule).

### #308 — The client's Proposals tab (Bookore, 2026-09-15)

The C-suite's "store proposals in the client's view" — the #307 rows, projected for
the client, inside the grant-gated portal shell.

* **Catalog:** `proposals` joined `client_grants.SECTIONS` / `SERVED_SECTIONS` /
  `SECTION_LABELS`, the client row of `portal_matrix.ROLE_SECTION_MATRIX`, and the
  FULL preset (by decision; Standard/Minimal unchanged). Default OFF like every
  grant — the admin toggle appears on its own (the grants UI is data-driven).
* **Registry:** `client_registry.DATASETS["portal.proposal"]` — id, number,
  version, title, status (CLIENT key `open | signed | declined | expired`, never
  the lifecycle word), status_label, price, deposit_amount, deposit_pct,
  valid_until, sent_at, signed_at, signed_name, declined_at, building, view_url,
  pdf_url, can_accept. Absent by provenance: decline_reason, signed_ip, recipient
  email, estimate code/status, any internal note.
* **Payloads** (`portal_sections.py`, all `require_portal_section("proposals")`):
  `GET /api/portal/<code>/proposals` (the organization that owns the bound
  project — `projects.client_org_id`; sent/viewed/signed/declined only, never a
  draft or a superseded version; each labelled with its building), `…/<id>/view`
  (the document; a real client's first open flips sent → viewed, a preview never
  does), `…/<id>/pdf`, and — the portal's first write endpoints, by the C-suite's
  ask — `POST …/<id>/accept {name, title, agreed}` and `POST …/<id>/decline
  {reason}`. Both reuse `proposals.accept / decline`, so the #307 trigger (estimate
  approved, COI + deposit tasks, VP notification, activity) is the same code path
  as the emailed link. An admin preview gets 403 on both writes. By-id access to a
  foreign organization's proposal, a draft, or a superseded version is 404.
* **Shell** (`portal_shell.html`): `PORTAL_SECTION:proposals` nav + view; cards
  with status pill, price, deposit, Open / Download PDF, and — when open — the
  typed-name signature form with the consent box (hidden in preview with a note).
  Accepting re-fetches the list (and purges the #291 boot cache for it).
* **Test** `tests/smoke_client_proposals_308.py` (both gates; boots `server.app`):
  catalog + registry, shell nav from grants, per-request grant toggle, org
  scoping + project binding, roles, preview read-only, registry-only payload, view
  → viewed, accept → trigger, decline. `smoke_portal_flip_284`'s matrix pin now
  includes `proposals`.

Next: #309 (QuickBooks deposit invoice on `proposal.signed`) and #310 (invoice
model), so the "Accepted" card can show the deposit invoice status.

### #307 — Proposals: generated from an estimate, sent with a signed link, accepted online (Bookore, 2026-09-15)

The first piece of the C-suite's "proposals → signature → deposit invoice" flow
(ROADMAP items 1, 2, 4). A proposal is now a record, not an attachment.

* **`proposals.py`** — tables `proposal` (one per estimate *version*; number
  `<estimate code>-P<version>`), `proposal_line` (schedule-of-values seed) and
  `proposal_event` (audit). Status `draft → sent → viewed → signed | declined`;
  a new version marks the old one `superseded` and kills its link. Only a draft
  is editable. Price follows the line items when no explicit price is given;
  deposit amount is derived from `deposit_pct` (default 30 %).
* **The document** — `render_document()` is self-contained branded HTML (letterhead
  from `app_settings` keys `company_address / company_phone / company_email /
  company_license` when present). `render_pdf()` runs it through
  `pdf_export.render_html_to_pdf` (chromium on Render, `SSC_PDF_ENGINE`), stores
  the file under `data_room/proposals/` and a row in `estimate_document`
  (new category `proposal`). Best effort: when the engine is missing the
  proposal still sends and the public page is the reference copy; the PDF route
  answers 503 with a plain message.
* **Send** — `send()` needs a price and a valid recipient; issues a 32-byte
  random token (sha256 stored, never the token), expiring at `valid_until`
  23:59:59; emails the branded message via `notifications._send_live`
  (`track_clicks=False` so the link is not rewritten). Test seam
  `SSC_MAIL_FAKE_DIR` (falls back to `SSC_RESET_MAIL_FAKE_DIR`). A failed
  delivery rolls the row back to draft and records `send_failed`. Resend
  issues a fresh link (also the fix for an expired one).
* **Public page** `/p/<token>` (+ `/p/<token>/pdf`, `POST /api/p/<token>/accept
  | /decline`) — public by token (`auth._PUBLIC_PREFIXES`, and admitted by the
  client-portal, estimator/vendor and architect containment gates so a
  signed-in user can open the link too). First open flips `sent → viewed`;
  accept requires a typed name + the consent box and records name, title, IP,
  timestamp; `no-store` + `noindex` on every response. Bad/expired/superseded
  links get a branded 404/410-style page, never a stack trace.
* **THE TRIGGER (`_on_signed`)** — the estimate moves to `approved` with the
  proposal price as `final_amount` (the manual approve modal still requires a
  QuickBooks ref; a signed proposal is the stronger evidence, so it is applied
  directly); CRM tasks open for the Certificate of Insurance (7 days, high)
  and the deposit invoice (2 days, high — #309 automates it); c_suite users get
  a `proposal_signed` notification; every step writes a CRM activity row.
  `mark_signed_offline()` (paper / email signature) fires the same trigger.
  Decline opens a follow-up task and leaves the estimate untouched.
* **Company Portal → Proposals**: list (KPIs, search, status filter), "New
  proposal" from any open estimate, detail with editor (fields + line items),
  Preview / PDF / Send… / Resend… / New version / Mark signed offline / Delete
  draft, who-and-where facts and the event timeline. Building detail's estimate
  rows gained a "Proposal" action. Date inputs go through the app's
  `SSCDatePicker` (`dateGet` / `dateSet`) — the picker rewrites
  `type=date` inputs to MM-DD-YYYY text, so read `dataset.iso`, never `.value`.
* **Migration** `apply_proposals_307.py` appended to `DUAL_BACKEND_MIGRATIONS`
  (the #306 ledger picks it up; boot ensures the schema regardless).
* **Test** `tests/smoke_proposals_307.py` (gate + CI): gates, prefill, edit
  rules, send + link extraction from the fake mailbox, public view/accept/
  decline, the trigger, versions, expiry + resend, offline signature, delete,
  no-secrets payloads. Tolerates a missing PDF engine.

Internal endpoints are `requires_section('estimates')` (admin / c_suite).
Next: #308 (client portal Proposals tab reading the same rows) and #309
(QuickBooks deposit invoice from `proposal.signed`).

### #306 — Migration ledger + CI gate on Postgres (Bookore, 2026-09-11)

Two of the audit's three structural fixes (the third, route default-deny, is now
partly covered by the strict duplicate-route boot check from #305).

* **`migrations.py` — the ledger.** Table `schema_migrations(name, checksum,
  applied_at, note)`. The ordered list of migrations IS
  `preprod_fixture_bootstrap.DUAL_BACKEND_MIGRATIONS` (one list, two readers).
  `status / pending / apply / baseline_if_needed / boot / pending_count`.
  Existing databases are **baselined** the first time the ledger appears (every
  listed migration recorded with note=baseline) — they were migrated by hand or
  by the fixture builder. From then on only NEW entries are pending.
  `python migrations.py status|apply [--dry-run]`.
* **Boot**: server.py ensures the ledger, baselines, and logs what is pending.
  `SSC_AUTO_MIGRATE=1` (set on preprod) applies pending migrations at boot;
  production only reports (log + `/api/health.migrations_pending`, 0 = healthy,
  -1 = ledger unreadable). **Adding a migration = append a dual-backend
  `apply_*.py` to DUAL_BACKEND_MIGRATIONS.** Nothing else.
* **`tests/run_gate_ci.py` — the PR gate**, cross-platform: compile every file,
  strict boot (duplicate routes refuse to start) + ledger clean, the #297–#300
  static contracts, the #301/#302/#304/#305/#306 behavioral suites, and the
  design-conventions guard against a live server. `--backend sqlite` (each
  suite builds its own fixture) or `--backend postgres` (SSC_DB_URL at a
  fixture-built Postgres). ~45 s on SQLite.
* **`.github/workflows/gate.yml`**: on every PR / push to `preprod` and `main`,
  a matrix of sqlite + postgres:16 (service container). The Postgres leg builds
  the fixture through `preprod_fixture_bootstrap.py` — the same path as the
  preprod bring-up — so the CI database is shaped exactly like preprod.
* The #260 gate (`run_gate_260.py`) remains the operator's full census on the
  Windows workstation; CI runs the subset every PR must pass.

Next: require the `gate` checks in branch protection on the client repo's `main`
(and on the fork's `preprod`) once a few runs are green.

### #304 — Buildings as a first-class entity (Bookore, 2026-09-11)

`buildings.py` + `apply_buildings_304.py`. A client owns buildings; projects and
estimates happen at a building.

* **Table** `building(id, client_org_id, name, address_line, address_key, borough,
  city, state, zip, bin, block, lot, lat, lng, stories, year_built, notes, …,
  archived_at)`; `projects.building_id` + `estimate.building_id`. Identity =
  `address_key` (unique): '890 East 135th Street' == '890 E 135th St, Bronx'.
  The matcher moved from company_portal to `buildings.address_key` (one owner).
* **Auto-link**: `estimates.create_estimate`, estimate → project convert, and
  `crm.link_project` call `buildings.autolink_*`; `backfill()` links everything
  older, creating rows on first sight, and runs at boot (idempotent) so preprod /
  prod heal on deploy. Projects with no address stay visible in the portal under
  a "No address on file" bucket.
* **Endpoints** (`requires_section('crm')` = admin/c_suite): list (search /
  client / archived), create (duplicate address refused; borough codes
  normalized; validation), get (projects, estimates, expiring docs, RFI + photo
  counts), patch (key follows the address; clash refused), archive/restore,
  link/unlink project or estimate, `unlinked` pickers, `backfill`.
* **Portal**: sidebar "Clients" + "Buildings"; Buildings list with search /
  client filter / archived toggle / re-run matcher / add; building page with
  KPIs, projects, estimates, expiring documents, facts, edit modal, archive,
  link/unlink pickers. Client page's building cards link to the building page.
* Guard: `tests/smoke_buildings_304.py`. Fixture: `apply_buildings_304.py` in
  DUAL_BACKEND_MIGRATIONS.
* Not yet: the client-portal building view (grant-gated; lands with #308),
  lat/lng geocoding, DOB BIN lookup.

### #305 — Security release (Bookore, 2026-09-11)

Every critical and high finding of the 2026-09-10 audit, plus the mechanical
mediums, in one branch with a commit per fix. Behavioral guard:
`tests/smoke_security_release_305.py` (boots `server.app` in-process on an
isolated DB + scratch data root, one user per role).

* **Worker PIN throttle was inert** — the PIN path audits `pin_fail`, the
  per-source counter only counted `login_fail`. `auth._recent_ip_fails` now counts
  both; 20 failures from one source -> 429 on the public `/api/worker/login`.
* **Credential card render** `/api/cards/<id>/<type>/live` (live PIN) ->
  `@requires_company`.
* **Worker PII** — `/api/employees` (+detail, `/api/certifications`,
  `/api/sign-ins`, per-worker certs, face-photo GET) -> `requires_section('internal_ops')`,
  and `server.strip_worker_pii()` drops `pin` for everyone and dob/phone/email/
  emergency-contact/address/license fields for non-company roles.
* **SQL injection** in the site-closure checklist PATCH: column name allow-listed
  (`SITE_CLOSURE_CHECKLIST_KEYS`), value Y/N; all four closure routes gated.
* **Route collisions** — `/api/rfis` GET/POST were registered by elevation.py
  BEFORE server.py's decorators ran, so the project RFI form + list were dead.
  server.py owns both now and dispatches to `elevation._api_rfi_*` when an
  `elevation_id` is present. A second collision surfaced the same way: two
  GET `/api/projects` handlers (the older one shadowed the console's, dropping
  `assigned_workers`); the shadowed one is gone. `server._duplicate_routes()`
  reports duplicates at boot; `SSC_STRICT_ROUTES=1` refuses to start (the gate
  runs strict).
* **Containment for estimator + vendor** (`role_containment.py`): estimator lives
  in `/estimating` + its API; vendor gets nothing and is no longer in
  `auth_admin.ASSIGNABLE_ROLES`.
* **Worker create**: caller-supplied `employee_id` must match `E-\d{5}`; the
  folder is containment-checked; `INSERT OR IGNORE` replaced by INSERT + retry on
  id collision (a concurrent onboard no longer returns 200 with no row).
* **/project-files** is project-scoped for pm/super via `pm_can_access_project`.
* **Preview blueprint** gated to admin/c_suite; root serve allow-listed to report
  prefixes (the company console was previewable unstripped).
* **Stored XSS**: `escapeHtml()` on issue descriptions (project dashboard) and
  project name/address (console cards).
* Mediums: `INSERT OR REPLACE` -> `ON CONFLICT DO UPDATE` in nyc_compliance
  (Postgres-safe); UTC "today" in cert-expiry math -> local; projdoc + receipt
  byte routes use `ssc_paths.resolve_data_path`; worker-file containment via
  `is_relative_to`; geofence longitude scaled by cos(lat); `/login?next=` quoted;
  83 handlers stop echoing driver text (`_server_error`, logged with a ref);
  reset-token burn gated on rowcount; `toISOString().slice(0,10)` -> `todayLocal()`
  on the facade dashboards; audit/counter failures now log a warning.
* **Fixture drift found by the guard**: `apply_roster_views_schema.py` and
  `apply_worker_intake_schema.py` were never in the preprod fixture lists, so
  fixture DBs (preprod!) had no `v_worker_roster` / `v_active_workers` views and
  no `certifications.card_number` — Workforce said "No workers yet" and several
  routes 500ed. Both are in `LEGACY_MIGRATIONS` now, and server.py ensures the
  two views at boot on both backends.

Not done (tracked): CSP `script-src` (needs the inline-script sweep), worker-app
hard-coded project + geofence-from-projects, `(project_code, rfi_number)` unique
index, `GET /api/projects/<code>` explicit field list.

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
