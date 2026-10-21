# Superstars OS — Product Roadmap

> The single source of truth for WHAT we build next and WHY. The audit
> (2026-09-10) set the engineering order; the C-suite conversation
> (2026-09-11, Arun / Amit) set the business features. HANDOFF_NEXT.md records
> what has shipped; this file records what is planned. Issue numbers continue
> the repo's `#nnn` sequence.

## Status snapshot (2026-09-20)

| Shipped | Where |
|---|---|
| Password reset + branded email, security hardening, CRM buildings | Production (#298–#301) |
| Company Portal (`/company`) | Production (#302) |
| Shared sidebar shell; Office Console rebuilt on it | Production (#303) |
| Security release: every audit critical/high + mechanical mediums | Production (#305) |
| Buildings as a first-class entity (table, auto-link, portal Buildings view) | Production (#304) |
| Migration ledger + GitHub Actions gate on SQLite and Postgres | Production (#306) |
| Proposals from estimates: branded document, signed link, online acceptance → estimate approved + COI/deposit tasks | Production (#307) |
| Client portal Proposals tab: read, download, sign or decline inside the portal | Production (#308) |
| Mon–Sun payroll week on the Labor Rate Tracker | Production (#310) |
| Shared shell on the admin pages, motion layer, mobile menu, dark-blue accent, gradients + hovers | Production (#311–#314) |
| Invoices & receivables: Finance → Receivables, deposit invoice drafted on signature | Production (#315) |
| Company vault: vehicles with registration / insurance / inspection, tax returns by year, filings, records, file attachments | Production (#318) |
| Worker job history + payroll view: projects, hours by month, rate-on-date cost, certs, credentials, rate history | Production (#320) |
| Light sidebar; onboard an already-won job with "+ New project" on Projects & assignments | Production (#321, #324) |
| Crews + site coordinates per job; worker app job picker with per-job geofence | Production (#325) |
| Invoice documents: PDF, email to client with a private link, per-project Payments section | Production (#326) |
| Permits & insurance package per job with send-to-client link and portal share | Production (#327) |
| Navigation clarity: project identity block on every project view, Pay Rates vs Weekly Payroll, company portal Buildings → Projects | Preprod (#328) |
| Office Console condensed from 16 sidebar entries to 12 (Certifications, Compliance › Specs, queue + Interface folded in, Drop Plan per project) | Preprod (#329) |
| AIA progress billing: billing Schedule of Values per job, Applications for Payment with G702 + G703 documents, each issued as a progress invoice (email, link, PDF, payments) | Production (#317) |
| Accounts payable: bills we owe (by hand, from a scanned receipt, from a subscription renewal), payments, and "what needs to get paid" — a 30-day cash view of bills, renewals and client invoices due; console + portal "Overview" renamed "Dashboard" | Production (#319) |
| Company Portal → Projects lists jobs; a job's own page shows only that job (money, AIA, permits package, documents expiring, crew, activity, links); job sites keep their pages | Production (#330) |
| Job locations beyond the five boroughs: Long Island and New Jersey in every picker, estimate series `FR-NJ-###`, region label on the job site | Production (#331) |
| Jobs calendar in the Company Portal: 3-day / week / month, one bar per job from start to target completion, overlaps stacked, daily-report dots, click-through to the job page; target completion captured on "+ New project" and Crew & site | Production (#332) |
| Calendar extras: milestones on the grid (document expiries, invoice due dates), on-site headcount per day, weather strip on the short views, drag to move / stretch a job with undo, printable sheet | Production (#333) |
| The building in 3D, stage 1: real footprint + height from NYC Open Data (no key), neighbours for scale, every traced elevation draped on its wall, cells clickable into the same panel; fallback box outside the city; a 3D toggle on the drawing markup for staff and clients | Production (#336) |
| Job sites → projects in one click (#337); "This week" calendar strip on the Office Console Dashboard (#338) | Production |
| ONE home (#339): one seven-tab company sidebar on every company page (Dashboard · Projects · Clients · Proposals · Finance · Workforce · Company); console dashboard, CRM tab, job sites and quick actions retired; + Add client / contact / note / follow-up on Clients; "Exterior" wording | Production |
| Navigation revamp phases 2+3 (#340): Finance confirmed as one tab (already true from #339, no change needed); project dashboard sidebar cut from 9 groups/29 entries to 6 groups/21 entries — 8 dead "Coming with Phase 2B" placeholders removed outright, Financial+Expenses merged into one gated "Money" group, Compliance folded into Tracking, Employees & Certs folded into Field | Production |
| Settings pinned as its own sidebar entry (#341): moved out of the "Setup" group (now just Templates) into a standalone entry above the account footer on the project dashboard; same treatment on the company sidebar (portal, console, all four admin pages) — pulled out of the "Company" tab's sub-nav into an eighth, pinned destination rendered by `company_nav.js` | Production |
| Calendar fix (#342): an onboarded project with no start_date was silently getting today's date, so undated jobs drew bars on the portal calendar; `project_onboard.onboard()` now leaves `projects.start_date` NULL when left blank, so the calendar's existing NULL filter and `undated` bucket work as designed | Production |
| One click into a project (#343): a Projects-tab row opens the job dashboard directly (the in-portal summary page is gone; old `#projects/<code>` links forward); its contract / paperwork / crew cards now live on Project Health for admin/c_suite; the dashboard's brand lockup and account chrome match the company pages. Follow-up #343b: token sweep of the ~20 operational views | Production |
| Project dashboard token sweep, stage 1 (#343b): the content views move off the old palette onto the shared tokens — warm-gray text, warm-beige borders, old ink and the old bright blue (and its tints) become `--muted` / `--line` / `--ink` / `--ui`; untokenized CSS colors 798 → 281. Stage 2: the long tail of status colors | Production |
| DCR numbers (#344): number shown before issuing, true project totals, original issue time, PDF link, delete now removes the PDF + folder (with backup + audit), an "All numbers" register (gaps / out-of-order / future-dated, View + Delete), and a one-time admin fix for FR-BX-001's DCR 109 → 097 | Production |
| Workforce Active / Inactive (#345): Active (N) / Inactive (N) toggle; make a worker inactive with a reason, reactivate them later, and (admin) permanently delete a worker who never worked — blocked with "make inactive instead" when they have any history; headcount counts active workers only | Production |
| Worker ID cleanup (#346): one-time admin action — W-0030/31/32 become W-0027/28/29 with all their data (worker record, rates, rate history, photos, saved daily reports); guarded, backed up, cross-checked; CoF card numbers can't collide for a later hire | Production |
| C-suite can run the one-time fixes (#347): the DCR 109 → 097 fix, the worker ID cleanup and Delete permanently are open to admin and C-suite (still guarded; delete still blocked by history) | Production |
| DCR fix unblocked (#348): a leftover 097 folder from a report deleted before #344 no longer blocks the 109 → 097 fix — it is shown in the preview and moved into the backup | Production |
| Delete permanently removes ID cards too (#349): a worker with no work history can be deleted even if a company ID / CoF card was issued; the card records go with them (listed in the confirm, IDs kept in the audit) | Production |
| Daily Reports date filter button reads "Show" (#350): was "Apply", easy to confuse with the one-time fix's apply | Production |
| Project dashboard token sweep, stage 2 (#343b): the long tail of status and neutral colors onto shared tokens — one green, one deep red, one warning set (matching the company portal), one secondary gray, one subtle fill; untokenized CSS hex 293 → 37 (the rest are charts / intentional) | Production |
| Drawing Markup + Weekly Summary load again (#351): the panels said "refuses to connect" because the security headers forbade framing even by the app itself; now only those two pages may be framed, and only by the app | Production |
| Drop Plan column repair (#352): every drop logs column repairs as length × width × depth (in or ft) → CF, beside concrete patches; totalled apart per drop and per project, with its own Drop Report section | Production |
| The real building in 3D (#353): Drawing Markup's 3D view shows the actual building (Google's photorealistic 3D, free up to 1,000 sessions a month, capped) with the drop grid on its walls; massing view stays as the fallback. Switches on once the Google key + logo are in | Production |
| Photoreal grid on the real building (#354): the drop grid now sits on the building's real wall at street level (re-measured as detail loads), sized from the drawing, with a DROP n tag per column | Production |
| Company vehicles (#355): per car — E-ZPass tag, odometer log (miles this month / year / per month), service history with next service due, E-ZPass statement import (tolls matched to each car), car bills into Accounts Payable with the bill file | Production |
| Billing type per project (#356): AIA / Schedule of values · Lump sum · T&M · Unit price — asked at project creation, editable in Settings; the Schedule of Values and AIA pay applications only appear on AIA jobs; a Billing chip on the Projects list | Production |
| Project contacts (#357): a Contacts page per job — ownership group, property managers, consultants (by discipline), site supervisors (ours and the owner's/GC's) with call / email; the Daily Report and RFI headers now print the real superintendent, PM and client | Production |
| Job dashboard (#358): Project Health becomes an executive summary — KPI band, schedule health, money with receivables aging (C-suite only), field today, open items & risk, key contacts and latest photos, every card drilling into its tab; the old widget grid folds under Operations | Production |
| Collapsible sidebar (#359): the side menu folds to an icon rail on desktop (remembered, tooltips on every icon) on the project dashboard and every company page; project pages stop at 1440 px wide and centre on big monitors | Production |
| Client portal tab (#360): one place to see every client login, the job it's linked to, what each client can see and whether they've signed in — with add login, unlock sections, presets, preview and password reset; Company now holds company records only and Settings holds Team & access + preferences | Production |
| Density pass (#361): project dashboard text is never below 11px (12px for normal text), one light table-header style everywhere, a consistent title scale, four dialog sizes, one primary button colour and three layout breakpoints | Production |
| Per-tab clean-up (#362): every project tab has the same header (title, subtitle, actions), the Daily Report and AIA forms collapse properly on phones, every main button is the same blue, and a quarter of the hard-coded styles are gone | Production |
| Security hardening (2026-10-05): request logs keep no bodies or tokens, test-only switches can't run in production, a field-encryption helper is ready, and a cross-site request guard runs in report mode | Production |
| Simpler New project (2026-10-05): opens in place with the 5 required fields first and reuses an existing client instead of duplicating it | Production |
| Per-job tabs (2026-10-05): each job shows only the tabs it needs, switched in its Settings; new jobs start with every tab on (fixed 2026-10-06) | Production |
| Console navigation (2026-10-05): back to all projects from a job, Client portal under Clients, Dashboard numbers link to their view | Production |
| Job dashboard on phones + v2 preview (2026-10-05): compact phone layout; an opt-in v2 look (?ui=2) with top tabs and a Needs-your-attention overview | Production |
| Edit projects and clients: change a job's name, address, client, dates and contract value from its Settings tab; edit a client's name, type, status and notes, and edit or remove its contacts, from the client page — every change logged on the client's timeline | Production |
| Cross-site request guard switched on (2026-10-06): a forged form or script on another website can no longer save, delete or sign in on a user's behalf (403); `SSC_CSRF_MODE=report` is the rollback | Production |
| Worker app photos (2026-10-07): photos taken in the worker app now actually upload to the job's Field Photos (Unassigned), captioned with where and scope; before this fix they were silently discarded | Production |
| One title per page + pinnable sidebar (2026-10-07): the top bar names the section, the sub-tab row names the page; the sidebar pins open or folds to an icon rail that slides open on hover | Production |
| Clean dashboard + Tasks & events (2026-10-07): the dashboard keeps the week, Active projects, Workforce and Needs attention; admin and C-suite log tasks or events, assign them, link a job, and see them on the calendar | Production |
| Task & event addresses (2026-10-07): an address with suggestions (job addresses + NYC addresses) and a one-tap Google Maps "Directions" link | Production |
| QuickBooks phase 1 (#316, 2026-10-19): an admin connects QuickBooks once from Finance; clients are matched to QuickBooks customers; issuing an invoice creates it in QuickBooks, payments and voids follow, payments recorded in QuickBooks come back every 15 minutes, and an invoice can be emailed from QuickBooks with its Pay now button. Off until the Intuit keys are set in Render (sandbox first). Phase 2: bills / receipts to QuickBooks | Production (switched off until keys) |
| QuickBooks phase 1b (#316, 2026-10-20): admins bring in QuickBooks' open invoices (Receivables) and unpaid bills (Bills); they stay in step every 15 minutes; aging tiles and a next step per invoice; reminders from QuickBooks, follow-up tasks, payments recorded here go to QuickBooks; QuickBooks customers with no client here become clients flagged for an admin to approve or merge; all of it admin-only; disconnecting the test company removes everything that came from it | Production |
| Calendar reminders (2026-10-21): everyone on a meeting, event, timed task or walkthrough gets an email 1 day and 1 hour before (no time set: 9 AM the day before); people who connected Google Calendar also get Google's pop-ups at the same moments | Production |

Everything through #327 is in production as of 2026-09-20 (upstream PR #4, C-suite approved on preprod). #328 and #329 went to production the same day (upstream PR #5). #317 AIA billing and #319 accounts payable went to production the same afternoon (upstream PR #6). #330–#332 went to production 2026-09-21 (upstream PR #7). #333 and the #334/#335 fixes are in production. #336 (the building in 3D, stage 1) went to production the same day (upstream PR #10). #337 and #338 went to production 2026-09-22 (upstream PR #12). #339 (ONE home navigation phase 1) went to production the same day (upstream PR #12). #340 (navigation revamp phases 2+3) went to production 2026-09-24 (upstream PR #13). #341's project-dashboard pin went to production the same day (upstream PR #14); #341's company-sidebar pin + #342 (calendar fix) went to production 2026-09-25 (upstream PR #15). #343 (one click into a project) went to production the same day (upstream PR #16). #343b stage 1 (token sweep) went to production 2026-09-26 (upstream PR #17). #344 (DCR numbers), #345 (Workforce Active / Inactive) and #346 (one-time worker ID cleanup) went to production the same day (upstream PR #18); #347 (C-suite can run the one-time fixes) followed the same day (upstream PR #19); #348 (leftover 097 folder no longer blocks the DCR fix) the same day (upstream PR #20); #349 (an ID card alone no longer blocks Delete permanently) went to production 2026-09-28 (upstream PR #21); #350 (Daily Reports filter button reads "Show") the same day (upstream PR #22); #343b stage 2 (long-tail colors onto tokens) the same day (upstream PR #23); #351 (frames fix), #352 (column repair) and #353 (photoreal 3D, key set on production) went to production 2026-09-29 (upstream PR #24); #354 (photoreal grid fitted to the real building) the same day (upstream PR #25); #355 (company vehicles: mileage, service, E-ZPass tolls, car bills) the same day (upstream PR #26); Both one-time fixes were applied on production by Angela: DCR 109 → 097 (2026-09-26) and the worker ID cleanup W-0030/31/32 → W-0027/28/29 (2026-09-28); the roster now runs W-0001–W-0029. Preprod = main. Next: QuickBooks phase 1 (#316, low priority, next week); Phase D stage 2 (photoreal capture) when a job warrants it. Next: QuickBooks phase 1 (#316) once Amit creates the Intuit developer app; calendar milestones / headcount if Arun wants them. Next: QuickBooks phase 1 (#316) once Amit creates the Intuit developer app.

---

## What the C-suite asked for (2026-09-11 notes)

Verbatim intent, then how it maps onto what exists.

1. **Send proposals through the software; store them in the client's view
   (a "Proposals" tab).** Today a proposal is an `estimate` row with documents
   in `estimate_document` (`DOC_CATEGORIES` includes `contract`) and a status
   pipeline `intake → scoping → submitted → approved → lost → converted`. There
   is no generated proposal document, no send step, and clients cannot see
   proposals in the portal.
2. **Once a proposal is generated and sent, a deposit invoice** — "now
   everything is through QuickBooks". `ira_job` already carries
   `deposit_received / deposit_date / balance_status / qb_invoice_ref` for
   rope-access jobs only; nothing issues an invoice.
3. **Some clients have multiple buildings. Clients → Buildings → see
   everything related to the building. View / portal.** The Company Portal
   derives buildings from address strings (#302). Needs a real `building` table
   (#304) and a building page in both the company and client views.
4. **Only when the proposal is SIGNED do you need the insurance.** Signature is
   the trigger for the Certificate of Insurance requirement (`project_documents`
   category `CONTRACTS` / key `coi`). Today nothing records a signature.
5. **AIA documents — the official documents contractors use to bill big
   jobs; Schedules of Values.** AIA G702 (Application for Payment) + G703
   (Continuation Sheet). `sov_line_items` exists but only as the drop-plan
   quantity model (`sov_code, description, unit, unit_rate`); no billing
   period, no % complete, no retainage, no G702/G703 output.
6. **Company portal with everything that has to do with the company:**
   licenses · workers (jobs they've done, payroll…) · documents (car insurance
   for the 6 vehicles, filing receipts, …) · accounts receivable and accounts
   payable · tax returns · a view of what needs to get paid, any due invoice.
   #302 covers licenses/insurance/bonds (`company_document`), workforce and
   rates, and receivables from `ira_job`. Missing: vehicles, tax returns,
   payables, a real invoice model, worker job history on the worker.
7. **(Verbal) 3D drawing markup:** a 3D model of each building, click into its
   parts, rendering close to the real building. Today `/drawing-markup` is a 2D
   elevation grid (`elevation`, `elevation_cell`, `elevation_drop`) traced from
   drawing sets with AI assistance (#293).

---

## The plan, in order

Each item ships behind the preprod review gate with a behavioral test, one PR
per issue into `preprod`, then a release PR into the client repo once approved.

### Phase A — foundation (September)

- **#304 Buildings as a first-class entity — SHIPPED to preprod 2026-09-11.** `building(id, client_org_id,
  name, address_line, borough, city, state, zip, bin, block, lot, lat, lng,
  stories, year_built, notes)` + `projects.building_id` + `estimate.building_id`,
  backfilled from the #302 address matcher and reviewable in the CRM. Building
  page: every project, estimate/proposal, document, photo set, RFI and visit
  for that address. Unlocks items 3, 7 and the client portal's building view.
- **#306 Migration ledger + CI on Postgres + strict routes in CI — SHIPPED 2026-09-11.** The
  three structural fixes from the audit. The gate already runs strict; CI makes
  it run on every PR against a Postgres service.
- **Shared shell, next four pages — SHIPPED to preprod 2026-09-18 (#311).**
  `admin_users`, `admin_projects`, `estimating`, `admin_labor_rates` are on the
  sidebar shell; the project dashboard and client portal share its sidebar
  language; one motion layer (view enter, hover/press, skeletons, reduced-motion)
  runs across every surface.

### Phase B — proposals to cash (October)

- **#307 Proposals — SHIPPED to preprod 2026-09-15.** A proposal is generated FROM an estimate: branded HTML →
  PDF (the app's chromium engine), versioned in `estimate_document`
  (`category='proposal'`), with scope, price, exclusions, schedule, terms, and
  a schedule of values seed. States: `draft → sent → viewed → signed → declined
  → superseded`. Send = SendGrid email with a signed, expiring link.
  Shipped with the public accept/decline page and the typed-name signature
  (originally scoped under #308), so `signed_at` already approves the estimate
  and opens the COI + deposit-invoice tasks. #308 is now only the client
  portal's Proposals tab on top of the same rows.
- **#308 Client "Proposals" tab + e-signature — SHIPPED to preprod 2026-09-15.** The client portal gains a
  Proposals section (grant-gated like every other section): open the PDF,
  accept with a typed-name signature + IP + timestamp (audit row, PDF stamped
  with the signature page). `proposal.signed_at` is THE trigger for: the COI
  requirement on the project (item 4), the project's creation/conversion, and
  the deposit invoice (next item). A DocuSign/Dropbox Sign adapter is a later
  swap-in; typed acceptance is legally sufficient for this use and ships
  without a new subscription.
- **#316 QuickBooks integration, phase 1 — deposit invoice.** Intuit OAuth2
  (sandbox first), customer sync from `crm_organization`, and on
  `proposal.signed` create the deposit invoice in QuickBooks Online from the
  proposal's deposit terms; store `qb_invoice_id`, poll status (sent / viewed /
  paid), reflect in the portal's receivables. Later phases: expenses → QBO
  bills, progress invoices from G702, payments back.
- **#315 Invoices & receivables model — SHIPPED to preprod 2026-09-19.** `invoice`
  (number INV-YYYY-####, kind deposit|progress|final|change_order|other, status
  draft→issued→paid / void, client, project, building, estimate, proposal,
  amount, retainage, amount_paid, issued/due/paid dates, qb_invoice_id) +
  `invoice_payment`. "What needs to get paid / any due invoice" is a real query
  on the Company Portal (Finance → Receivables: open, overdue, due in 30 days,
  awaiting deposit, drafts, collected YTD; Overview surfaces overdue + drafts).
  A signed proposal with deposit terms drafts its deposit invoice automatically.
  Numbering note: roadmap items below were renumbered on 2026-09-19 because
  #309–#314 were consumed by the September UI / ops tickets (see HANDOFF).
### Phase C — the company's back office (October–November)

- **#317 AIA progress billing (G702/G703) — SHIPPED to preprod 2026-09-20** (as `billing_sov` + `pay_application`, leaving the drop-plan `sov_line_items` alone). Original plan: upgrade `sov_line_items` into a
  billing schedule of values per project (`scheduled_value, previous_billed,
  this_period, stored_materials, pct_complete, retainage`), billing periods,
  and a G702 + G703 render (HTML → PDF, matching the AIA layout, with the
  company's own numbering). Each application becomes a `progress` invoice
  (#315) and, with #316 phase 2, a QuickBooks invoice.
- **#318 Company vault — SHIPPED to preprod 2026-09-19.** Extend `company_document` with: vehicles (6 cars:
  registration, insurance, inspection, plate, assigned driver), tax returns by
  year, filing receipts, and a generic "company record" with expiry — all in
  the Compliance view's expiry buckets. File attachments on company documents
  (today it is metadata only).
- **#319 Accounts payable — SHIPPED to preprod 2026-09-20.** `payable(vendor, doc, amount, due_at, paid_at,
  source: receipt|bill|subscription)` fed by the receipts ledger, the
  subscriptions register, and manual bills; "what needs to get paid" = due
  payables + due receivables on one Portal screen with a 30-day cash view.
- **#320 Worker job history + payroll view — SHIPPED to preprod 2026-09-19.** On each worker: projects
  worked (from `sign_in_log` + assignments), hours by project and month,
  certifications, credentials issued, rate history (`worker_rates`). Company-only
  surface per the comp-data rule.
- **#321 Company overhead + subscriptions screen** in the Portal (the
  `subscriptions` ledger already exists in the schema; the CSV is the interim).

### Phase D — the building in 3D (November onward, staged)

The ask is "a 3D model of each building you can click into, looking almost
identical to the real one". That is two different things, and the honest path
is to ship them in order:

- **Stage 1 — 3D massing — SHIPPED to preprod 2026-09-21 as #336** (from the REAL footprint and height, not just the grid). Original plan: extrude each
  elevation grid (`elevation` × `elevation_cell` rows/columns, stories, drops)
  into a simple 3D box model of the building in the browser (three.js). Every
  face is the existing elevation; every cell is clickable and opens the same
  data the 2D markup shows (drop status, photos, RFIs, comments). No capture
  cost, works for every building that has a drawing set. Ships as a view
  toggle on `/drawing-markup`, not a replacement.
- **#317 Stage 2 — photoreal skin.** "Almost identical to the real one" needs
  a capture, not a drawing: a drone/phone photogrammetry pass (or a Matterport
  / LiDAR scan) per building produces a textured mesh. We import the mesh
  (glTF), align it to the Stage-1 massing, and project the elevation grid onto
  it so clicks still resolve to cells. Per-building cost is the capture (a
  drone operator or a phone scan the foreman does) plus storage/CDN for meshes
  of 50–300 MB each. Pilot on 890 E 135th St first.
- **Stage 3 — condition overlay.** Field photos and inspection findings pinned
  to the mesh; drop progress colouring the facade over time. This is where the
  3D view starts paying for itself in client conversations.

Not on the roadmap: authoring BIM-grade models from drawings. The ROI is not
there for facade restoration, and Stage 2 gets the visual result for a fraction
of the effort.

---

## Ordering rationale

Buildings (#304) come first because proposals, invoices, the client portal and
the 3D model all hang off a building record. Proposals → signature → deposit
invoice is one continuous flow and is built as one (#307, #308, #315, #316), because each
piece is only valuable with the next. AIA billing (#317) reuses the invoice
model and the QuickBooks connection. The 3D work is staged so the C-suite sees
a clickable building within a sprint (Stage 1) while the photoreal capture is
arranged.
