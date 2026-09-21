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
| Calendar extras: milestones on the grid (document expiries, invoice due dates), on-site headcount per day, weather strip on the short views, drag to move / stretch a job with undo, printable sheet | Preprod (#333) |

Everything through #327 is in production as of 2026-09-20 (upstream PR #4, C-suite approved on preprod). #328 and #329 went to production the same day (upstream PR #5). #317 AIA billing and #319 accounts payable went to production the same afternoon (upstream PR #6). #330–#332 went to production 2026-09-21 (upstream PR #7). #333 (calendar extras) is on preprod awaiting review. Next: QuickBooks phase 1 (#316) once Amit creates the Intuit developer app; calendar milestones / headcount if Arun wants them. Next: QuickBooks phase 1 (#316) once Amit creates the Intuit developer app.

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

- **#322 Stage 1 — 3D massing from what we already have.** Extrude each
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
