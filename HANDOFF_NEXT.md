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

### Schedule window rebuilt (Bookore, 2026-10-24)

* Accepted prototype: https://claude.ai/artifact/HSKLWGfpYbD5YES2Jm9ook (row 2). One call: `GET /api/projects/<code>/schedule?start=`
  (`project_schedule.py`) = job dates + days left + time used vs drops complete + pace, the two-week board (`lookahead.load_window`),
  the job's tasks / events / meetings (`team_items`), site pin (weather flags from `/api/weather/week`), `has_drops`.
* Desktop / tablet: timeline strip, ‹ two weeks ›, board grouped by drop with a **Blocks** pill (open schedule-impact RFIs), a
  Job-wide group; tap a bar = edit sheet (page root, SSCDatePicker dates, Remove with a second tap); drag = move / right edge resize
  with Undo. ≤860px: a day-by-day agenda. Jobs without drops: an empty state + Add activity / Set up Drop Plan.
* **Drafting:** auto rows are re-projected on every load from THIS week's Monday through the viewed window, and RECONCILED (a still-
  projected stage keeps its row id; a hand-edited stage is never drafted twice and later stages follow its finish). Past windows get
  no invented bars. The old `?refresh=1` and Start field are gone.
* **Security fix:** PATCH / DELETE `/api/lookahead/activities/<id>` now check the activity's job (`_projdoc_scope_err`); before, any
  PM / super could edit or delete any job's activity by id.
* Guard: `tests/smoke_project_schedule.py` (new, in the gate).
### Projects table, PMs, addresses, Documents (Bookore, 2026-10-24)

* Accepted prototype: https://claude.ai/artifact/HSKLWGfpYbD5YES2Jm9ook. **Open invoices** column removed from the Projects table
  (the API keeps `open_value` / `overdue_value`: Claude and the "$ overdue" badge use them; no invoice data touched).
* **PM column = PMs only.** `pm_project_assignment` also holds clients' / architects' / supers' / estimators' project ACCESS; the
  column listed all of them (and inactive or ex-PMs), which is why #1's PM list looked wrong. `portal_projects._pms` and
  `portal_calendar` now list active `role='pm'` users. Editor: pencil in the PM cell (bottom sheet on a phone) →
  `GET/PUT /api/company/portal/projects/<code>/pms` (admin / C-suite). PUT sets the job's PMs; "outdated" rows (inactive account,
  or role no longer scoped) stay unless removed; an active client / architect / super / estimator row is never touched (counts
  only in the editor). **Project 1 cleanup is a click for Angela**: open #1's PM cell, Remove the outdated names, Save.
* **Address autofill** in the New / Edit project form: `GET /api/address/suggest?q=` (`buildings.address_suggest`, NYC Planning
  GeoSearch — the free, keyless service the app already used for Team addresses / 3D / compliance; no new dependency). A pick fills
  borough (new jobs), a new **City / ZIP** field and the site pin when blank. Onboard stores `projects.city_zip` (+ the building's
  city / state / ZIP). Outside NYC: no suggestions, typed as before.
* **Documents:** the CONTRACTS checklist item `bond` became `pay_app` "Payment Application" (idempotent, `doc_intake._bond_to_pay_app`,
  runs at boot); type `aia_g702` = "Payment Application (AIA G702/G703)" fills it; `bond` left the picker (legacy label kept, filed
  bonds untouched). **Filed** shows only filed documents (no missing rows; tiles show "N filed"); what is missing is under
  **Still needed for this job** (`suggestions`, origin `checklist`). Required items fit the job (`doc_suggest.applies` / `SCOPE`:
  type, access methods, billing, drops) — no drop / rigging layout without suspended scaffold or drops; `job.not_asked` explains
  what was left out. A second COI / pay app **files alongside** by default ("This replaces …" is an explicit tick).
  **Additional documents**: `party` subcontractor / other → `additional` groups (one card per company), never filling our checklist.
* Guards: `tests/smoke_project_pms.py` (new, in the gate), `smoke_doc_dropzone.py`, `smoke_doc_package_327.py` (bond left the package).

### Projects tab: active jobs first, "Ready to start" for signed proposals (Bookore, 2026-10-23)

* Accepted prototype: https://claude.ai/artifact/Sz6pFAL3nSVCav97gmPsDE. The #337 "addresses added without a project" banner is
  gone (every proposal adds an address, so it grew with every bid). The list is now the first thing on the tab: a one-line
  summary, Active / Closed / All with counts, client-side search, and a "Needs you" column (worst first: overdue invoice,
  past end date, no daily report for 2+ workdays while crew is on, no PM, no crew, billing not set; else "On track").
  At ≤860px the table becomes cards.
* **Ready to start** (Projects only, not the Dashboard): `meta.ready_to_start` on `GET /api/company/portal/projects` =
  signed proposals whose estimate is not converted, not marked "Not a project", and with no job made at that address on or
  after the signing day. `meta.waiting_signature` = proposals sent/viewed. Panel (sheet on phones): Create project /
  Not a project (`POST /api/company/portal/projects/ready/<proposal_id>/dismiss` → `proposal.project_dismissed_at/_by`,
  ensured by `proposals.ensure_schema`).
* Create project opens the shared form (`new_project_modal.js?v=1016`) filled from the proposal; `onboard()` takes
  `from_proposal_id`: the job reuses the proposal's estimate (its code, type, region and client; no second estimate) and the
  deposit invoice moves onto the job. Guard: `tests/smoke_projects_ready.py` (in the gate).

### Inbox synced both ways with Gmail; one Claude draft per conversation (Bookore, 2026-10-23)

* A reply sent from Gmail itself (or from the Inbox) closes the conversation: `inbox.close_thread` (called from `ingest` for every
  outbound email) marks the earlier emails done and `withdraw_drafts` declines Claude's pending `reply_email` cards (decided_by NULL,
  reason in `result`). An email already answered when Claude reaches it is sorted only; a sent email never gets a reply draft.
  The newest draft replaces older ones in the thread (`_one_draft`); `suggest_prompt` lists what is already waiting; `today_cards`
  shows one card per (thread, tool).
* Gmail -> here: history also maps UNREAD and INBOX label changes (`apply_labels`); thread re-reads reconcile read / archived
  (only with gmail.modify). Arrival keeps Gmail's state: `status='new'` only with UNREAD; `mail_message.archived_at` (new column,
  idempotent in `ensure`) = out of Gmail's Inbox. List filter `inbox` (default; `all` is an alias) hides archived conversations.
* Here -> Gmail (`gmail_modify`, `/messages/batchModify`, best effort, needs `MODIFY_SCOPE`): opening = read; **Done = Archive**
  (whole thread; `done:false` = Move to Inbox); `POST /api/inbox/messages/<id>/unread`.
* Live: no Refresh button. The page calls `GET /api/inbox/changes?sync=1` every 10 s and on tab focus (fingerprint + `kick`, a
  background `sync_user` at most every 15 s per person); default `SSC_INBOX_SYNC_SECONDS` 30. Claude's passes run on their own
  thread (`claude_round` / `_ai_loop`) so a slow pass never holds up sync. Google push (`GMAIL_PUBSUB_TOPIC`) still unverified on Render.
* UI: tabs Email · My calendar · Team (same hashes), filters Inbox / Needs reply / Filed, live line "Up to date · synced …",
  conversation bar Done / Delete / Mark unread / Open in Gmail. Prototype: `../docs/review-2026-10-23-inbox/`.
* Guard: `tests/smoke_inbox_sync.py` (in the gate).

### Documents tab: preview any document in the app (Bookore, 2026-10-23)

* Filed rows have a **Preview** button (the title and History's "View" do the same); review cards preview from the
  file icon or the file name. It opens the shared side panel `static/js/pdf_view.js` (now v=2): PDFs drawn page by
  page with the vendored pdf.js (same on phone / tablet / desktop), pictures shown inline, anything else (XLSX, HEIC
  in Chrome) gets a message + Download. Header: name · type · file name · size, "Open in new tab", Download, ✕ / Esc.
* No framing change: the file routes stay `X-Frame-Options: DENY`; pdf.js fetches the bytes, nothing is iframed.
* History entries now carry `mime` + `file_name` so a previous version previews like the current one.
* Guard: `tests/smoke_doc_dropzone.py`.

### Documents tab: no double uploads; rename + delete (Bookore, 2026-10-23)

* **Double upload fixed.** A drop on the Documents tab was taken twice: the tab's own `window` drop listener AND
  `static/js/file_drop.js` (document, bubble — fires first) which fed the same files into `#pd-cv-input`. The tab now
  listens on `document` in the capture phase and `preventDefault()`s, so file_drop.js sees it handled. Server half:
  `project_doc_intake.content_sha256`; the same file already waiting returns `200 {…, duplicate_of}` instead of a
  second card (checked after insert, so racing drops end as one).
* **Rename.** Each review card shows an editable name (Claude's title, else the file name); it is sent as `title` on
  approve. Filed rows show the document's own title (no longer the checklist label) with a pencil: rename in place,
  Enter saves, Esc cancels → `PATCH /api/documents/<id> {title}` (blank → 400).
* **Delete.** Every filed row has a trash button with an inline confirm. `DELETE /api/documents/<id>` brings the
  previous version back as current (still internal) and relinks History; clears shares/flags. PATCH and DELETE now
  check the job scope (`_doc_actor_can_admin`) — before, any docs role could edit/delete any job's document.
* Guard: `tests/smoke_doc_dropzone.py` (dedupe, rename, delete + restore, scope, capture-phase drop).

### Claude can change a job: details, status, access methods (Bookore, 2026-10-22)

* Angela asked Claude to "delete access method suspended scaffold" on FR-BX-001 and it said it couldn't. New write tool
  `update_job` (admin / C-suite, any page): name, address, city / ZIP, client, start date, target completion, contract
  value, status (active / closed) and access methods (`add_access_methods` / `remove_access_methods`). It is an approval
  card like every other change: the card shows each new value with "Now: …" (access methods as "old → new"), editable
  fields are what get saved, and **Undo** puts the old values back (`result.before`). It writes through
  `project_details.update` / `project_setup.update` (the Edit project form's own validation) and the same status write
  as `/api/projects/<code>/status`. `job_summary` now returns `job_setup` (status, job type, access methods) so Claude
  sees what is set.
* Adding a tool changes the `tools` prefix of every live chat, and current models bind replayed thinking to that prefix
  (a 400 on accounts the check enforces). The Claude window now sends `thinking.block_binding.prefix_mismatch_behavior
  = "drop_block"` (beta `thinking-binding-controls-2026-08-01`, `ai_models.agent_kwargs`) so stale blocks are dropped
  and old chats keep working; if the API ever refuses that control, `call_model` retries once without it (log line
  "claude thinking-binding control refused"). New tools can now be added without breaking open conversations.

### Estimates & proposals revamp: latest only, delete / restore, pencils (Bookore, 2026-10-23)

* Accepted prototype: https://claude.ai/artifact/8a8VUxBgrGSyRWXDMgzXvm. `company_portal.html` Proposals: a Proposals / Estimates /
  Bids switch; status chips with counts; the number carries the status in color (amber draft, blue sent/opened, green signed,
  grey declined/replaced/deleted); "Latest of each job" by default (older versions fold under the newest); a row opens a panel on
  the right (`#proposals/<id>`; the old full page is `#proposals/<id>/page`). "+ New proposal" is gone: proposals start with
  "✦ New proposal with Claude" / Proposal Studio (Estimates and Bids keep their + New).
* **Delete** = `DELETE /api/proposals/<id>`: a draft is removed; a sent one is admin-only and HIDDEN (`status='deleted'`,
  `deleted_from` / `deleted_at` / `deleted_by`), so every status filter (lists, counts, client portal, the client's link, Today
  cards, Claude's tools, Studio's examples) leaves it out; pending revisions on it are withdrawn. Signed can't be deleted.
  `POST /api/proposals/<id>/restore` (admin) puts it back; `?status=deleted` lists them (admin).
* **Pencils** = `PATCH /api/proposals/<id>/record {field, value}`: number (every version of the job changes together; refused when
  another job has it; editing it higher moves the next automatic number after it), title (drafts), recipient, valid-until
  (drafts; sent/opened ones extend the client's link). Estimates / Bids list: address, amount, bid due, status (the allowed next
  statuses, `est_public.next_statuses`).
* Guard: `tests/smoke_proposal_revisions.py` group "keep only the latest + edit in place".

### Proposals: one amount everywhere, admin-approved revisions, expiry follow-ups, reply in a thread (Bookore, 2026-10-22)

* **One amount.** A Superstars-layout proposal's stored `price` is always its lines' total as the document prints it
  (`proposals.sync_price`, called by every write and both send paths); a price sent apart from the lines is ignored.
  `proposals.amounts(conn, r)` = {total, deposit, deposit_pct} is what the editor, portal, email, send review and PDF
  show. A new proposal from an estimate gets the estimate's price as a "Base bid" line. **PDF:** `pdf_file` re-renders
  when the drawn document's sha256 differs from `proposal.pdf_hash` (it used to compare second-resolution timestamps, so
  an edit in the same second, or Download racing the autosave, served the old amount); Studio's Download flushes the
  autosave first. A sent proposal keeps the fixed wording it went out with (`content.house`, `proposal_engine.freeze_house`).
* **Revisions** (`proposal_revision`, `proposal.revision_of`): anyone on proposals asks (`POST /api/proposals/<id>/revisions`
  {reason, price_review, proposed_price}); **only admins** approve/reject (`/api/proposals/revisions/<rid>/approve|reject`,
  `requires_role('admin')` + `decide_revision` checks the role again). Approval drafts vN+1; the sent copy stays live for
  the client, untouched, until the revision is sent (`supersede_predecessor`). A revision can't be sent if the client signed
  the version it replaces. New version directly = admin only (recorded as an approved revision). Withdraw = the asker or an admin.
* **Today › Needs you** (`today_feed.proposal_cards`): sent proposals 7 days before / 14 days after valid-until (the sender +
  admins; Open proposal / Draft a follow-up = Claude window prefilled, nothing sent); pending revisions (admins); the
  decision (the asker, 7 days). Studio shows an expiry banner with the same follow-up button.
* **Send** (Studio): the pop-up loads the client's threads lazily (`/send-options`, `?q=` searches the inbox), previews a
  reply's recipients/thread (`/send-preview`), then a Review step; `/send` refuses without `confirm: true` and when the
  total differs from `expected_total`. Reply = `inbox.send_reply` with the reviewed To/Cc, In-Reply-To, linked to the proposal.
  The portal's link-email Send modal points drafts to Studio's reply flow.
* **Studio editor:** Exclude / Keep beside each note, exclusion and deliverable (and in the checklist); excluded paragraphs
  are listed with Put back and Claude is told not to re-add them. Claude chat is first in a sticky right rail; on tablet it
  slides in from the right, on a phone it is a bottom sheet (toolbar "✦ Claude"); the toolbar sticks under the header.
* **Claude speed:** one shared Anthropic client (`assistant.client()`), the chat poll's card purge throttled to once a
  minute, a turn reads each attached file once, the drawer polls at 0.7 s then 1.5 s and reads /status once per answer,
  Studio's view no longer decrypts the inbox on every autosave, the clause library is read once per view, and the draft
  progress shows the server's real step (it was a 5-second timer).
* **Mark as sent** (a draft that went out another way): `POST /api/proposals/<id>/mark-sent` {sent_on, to_email, to_name, how} (`proposals.mark_sent_outside`: no client link, sent_at = that day at noon, supersedes a revised version). Buttons: Proposal Studio (Send pop-up and the Not-ready banner) and the portal's proposal page. Claude's `log_proposal_sent` no longer needs a Gmail email, and the proposals search finds the number people see ("1045", "Proposal No. 1045").
* Guard: `tests/smoke_proposal_revisions.py` (in the gate). `smoke_proposals_307` now expects the line total as the price.
  Not exercised locally: a real Gmail send and a real Chromium PDF (the suite fakes both).

### Claude follows you: one conversation across pages, tabs and devices (Bookore, 2026-10-22)

* Accepted prototype: https://claude.ai/artifact/7Z4s6fDgoiog8CHZb44tKE. Close Claude on the company dashboard, open it
  on a job dashboard: same conversation ("Picked up from … · same conversation" under the title).
* Drawer (`static/js/claude_drawer.js`, v=14): the open chat id is in **localStorage** `ssc.claude.chat` (every tab);
  open/closed stays per tab (sessionStorage). Tabs tell each other on a send / switch / new chat / finished turn
  (BroadcastChannel `ssc-claude`, receivers reload quietly). No stored id (new device) → `GET /api/claude/chats/latest`.
* **Office-only rule** (Angela, 2026-10-22): a chat follows everywhere unless it has shown pay or email details.
  `claude_chat.office_only` = 'pay' | 'email', set when `payroll_summary` / `search_email` / `email_detail` return data
  (backfilled at boot from stored tool calls; Inbox mail chats count as 'email'). On a job dashboard such a chat is not
  listed and GET/POST return 403 `{locked}`; the drawer shows "Start a new chat here / Open the company dashboard".
  Tools + system prompt stay those of the surface the chat STARTED on (Opus 5.5 binds replayed thinking to an unchanged
  prefix: never change a live chat's tools); `claude_chat.page_surface` (the page NOW) makes office-only tools refuse
  on a job dashboard.
* What Claude sees, in the message head: `[Page: …]` ("Claude sees" line, × hides it → `[Page: not shared]`),
  `[Quoted from the page: …]` (select text → "Ask Claude about this"; emails/phones scrubbed), `[Points at: …]`
  (@-mentions from `GET /api/claude/mentions` = the `find` tool's search; hints only, Claude looks them up with its
  tools). The chat view returns `page` + `quote` per user message.
* Not yet: workers as @-mentions (needs a new lookup tool; adding tools changes every live chat's prefix, so batch
  it with other tool changes).

### Claude budget + cost: monthly budget, per-person guard, cheaper calls (Bookore, 2026-10-22)

* The $5/day company-wide cap is gone (it stopped Proposal Studio testing on 2026-10-21). `assistant.limit_reason(conn,
  uid)` is checked before every model call (Proposals panel, Claude window, Proposal Studio, Inbox): a **monthly** company
  budget `SSC_ASSISTANT_MONTHLY_BUDGET_USD` (default $100), a **per-person daily** guard `SSC_ASSISTANT_USER_DAILY_USD`
  (default $10; spend billed by `assistant_message.billed_user_id` / `claude_chat.user_id`), and the old
  `SSC_ASSISTANT_DAILY_CAP_USD` only if set (unset = no company daily cap). Budget 0 = Claude off. No dollar figure on any page.
* `assistant.check_budget_alerts` emails every active admin once per month at 50 / 80 / 100% (`notification_log` kinds
  `claude_budget_50|80|100`); runs after each recorded cost, never raises.
* Caching (Proposals panel threads): system + ticked documents cached 1 h; the record (estimate / proposal) rides on the
  newest message, the previous reply is the 5-min read point; no top-level auto cache. Before, the record sat in the system
  prompt ahead of the documents, so each proposal edit re-wrote every document at the cache-write price. Proposal Studio /
  engine one-shot calls: no top-level auto cache (pure surcharge on unique content); the engine's company material
  (house, library, closest past proposals, prices) has its own 5-min breakpoint.
* Models: the Claude window runs `SSC_CLAUDE_MODEL` (default = `SSC_ASSISTANT_MODEL`, Opus 5.5) at `SSC_CLAUDE_EFFORT`
  (default **low**, was medium). Document / expense / cert scanners Sonnet 4.6 → **Sonnet 5.5** (`CERT_EXTRACT_MODEL` is new);
  Gmail suggestions Haiku 4.5 → **Haiku 5.5** (thinking off, effort low); drawing trace Opus 5 → **Opus 5.5**.
  `ai_models.py` holds the per-model request options (refusal fallback only where offered; the right "thinking off").
* Not done yet (verify first, costs real calls): Haiku 5.5 for the scanners / Claude window; Claude window context
  clearing (must stay append-only: Opus 5.5 checks replayed thinking blocks against an unchanged history).

### Documents tab: several per item, whose record, suggested documents (Bookore, 2026-10-22)

* **Several documents per checklist item.** Each item in `GET /api/projects/<code>/documents` now carries `docs` (every
  current doc on it, newest first); `doc` stays as the one that sets the item's status (best standing). Before this, a
  second COI (a sub's) vanished from the payload. Folder rows list each one.
* **Whose record it is**: `project_documents.party` (`company` | `subcontractor` | `owner` | `other`, NULL = company) +
  `party_name`. Claude's read returns `party` / `party_name`; the review card has a "Belongs to" picker; approve, upload
  and PATCH accept it. "Newer version of" now only matches the same item **and** party, so a sub's COI files alongside ours.
* **Suggested for this job** (`doc_suggest.py`, pure): rules (subcontract → that sub's COI + lien waiver, G702 → SOV +
  lien waiver, BEST permit → rigging engineering, …), words in what Claude read ("performance bond", "after-hours"), and
  Claude's new `related` scan field. Returned as `suggestions` on the documents payload. "Required · missing" only when it
  fills a missing checklist item **and** the document would be ours; everything else is "May be needed". Upload from a
  suggestion pre-sets the type + party; "Not needed" → `POST …/documents/suggestions/dismiss {key}` (`project_doc_suggest_dismissed`).
* **Removed from the tab**: the Specifications table and the permits & insurance package card + send dialog. Their APIs
  stay (`/document-specs`, `/documents/package`); the Company Portal job page still uses the package.
* Guard: `tests/smoke_doc_dropzone.py` (12 groups). The live model read with the new `party` / `related` fields was NOT run
  locally (no key).


* The job's Documents tab is now: a big drop area (the whole tab takes drops; Choose files; Scan with camera on
  phones) → one review card per file → **Approve** → filed. Below it, six folder tiles (the existing six categories)
  replace the 28-row checklist; a tile opens its rows (same #270/#271 row: share, red-flag, Update/Replace, History)
  plus the required items still missing. The permits & insurance package card moved under the folders.
* **Intake** (`doc_intake.py`, routes in `server.py` next to the docs routes): `POST /api/projects/<code>/documents/intake`
  saves the file at once (same `data_room/project_docs/<code>/` home), runs Claude's read, and keeps it in
  `project_doc_intake` until approved, so a refresh never loses it. `GET …/intake` lists what's waiting;
  `POST /api/documents/intake/<id>/approve {type_key, supersedes_id?}` files it (the tag decides folder + checklist
  item; supersedes_id = "newer version of", same rules as Update); `DELETE /api/documents/intake/<id>` removes it.
  No API key → the file still waits as `unread` and the user picks the type.
* **Types** (`doc_types.py`, `GET /api/documents/types`): about 40 tags (PW2, CD-5, COI, AIA A101, G702/G703, SOV, change
  order, TR1, SDS, lien waiver, …), each mapped to one category and, when it fulfils one, a checklist item.
* **Claude's read** (`document_scanner.py`): the schema now also returns `type_key`, `summary`, `key_facts` and
  `fields` (every label/value on the pages, capped at 80). Stored on the filed doc as `project_documents.ai_extract`
  (+ `type_key`); shown as "Everything Claude found" on the card and "What Claude read" on the filed row.
* Migration `apply_doc_intake.py` (dual-backend list); the app also ensures it at boot. Guard: `tests/smoke_doc_dropzone.py`
  (in the gate). The live model read with the new schema was NOT exercised locally (no key); the seam-driven test covers
  the pipeline. Check one real drop on production.
### Calendar reminders: 1 day + 1 hour before (Bookore, 2026-10-21)

* `event_reminders.py`: a loop thread (every 60s; `SSC_REMINDERS=0` turns it off) emails everyone on an item 1 day and
  1 hour before it starts. Items: open events / meetings, open tasks **with a time** (`crm_task`), scheduled walkthrough
  visits (`walkthrough_visit`). Who: assignee + meeting attendees; walkthrough attendee; active users only. No time set:
  one email at 9 AM the day before. A reminder whose moment fell before the item was created / last edited is skipped.
* Once only: `event_reminder_sent` (unique source, item, user, slot, start); a moved item is reminded again. Every send is
  a `notification_log` row (`kind='event_reminder'`); live only with `SENDGRID_API_KEY`. Logs: ids only.
* Google: `gcal_sync.event_body` sets `reminders` (popup 1440 + 60 timed, 900 all-day, none for untimed tasks / done);
  meeting invitations inherit it. On boot the loop schedules a sync for every connected person so existing copies update.
* Guard: `tests/smoke_event_reminders.py` (in `run_gate_ci.py`). Prototype: `../docs/review-2026-10-21-reminders/`.

### Make admin: more than one admin (Bookore, 2026-10-16)

* The single-admin rule is gone; the rule is now **never zero admins**. Arun (owner) needs admin alongside Amit.
* `POST /api/admin/users/<id>/make-admin` (admin-only, body `{confirm: true, reason?}`) promotes an **active C-suite
  account that has set its own password** (`must_reset_password=0`); anything else is 403. Audited in `role_change_audit`.
  Admin is still never a create role, and the generic role route still refuses `new_role=admin`.
* Admins can move another admin off admin or deactivate them; nobody acts on themselves; the last ACTIVE admin can't be
  removed (a disabled admin can be moved off the role). Admin rows still have no Reset password (Forgot password instead).
* UI: `admin_users.html` — "Make admin" button on active C-suite rows + confirmation dialog (`#ov-mkadmin`).
* Guard: `tests/smoke_make_admin.py` (in `run_gate_ci.py`). `apply_is_system_258.py` now checks ≥1 real admin, all on the company domain.

### Daily Report as a step-by-step checklist, phone only (Bookore, 2026-10-16)

* `static/js/dcr_steps.js` (loaded by `dashboard-static.html`, `?v=2`) re-presents the existing Daily Report entry form
  (`#dcr-entry-view`, 11 `.dcr-block` sections) at **≤860px only**; desktop keeps the full form. It does not re-implement
  anything: the form's own row Save, Issue and Cancel handlers do all the work.
* Checklist home → one step at a time → sticky "Save & continue" → Review (reveals the form's own number preview + Issue button).
  Auto steps (Project info, Weather, Crew) start done; **required = Work done today + Safety** (visit + continue; content is not
  validated, so a quiet day can continue empty); optional = Deliveries & equipment, Issues & delays, Inspections & visitors, Photos.
  A No-Work day always unlocks Issue. "Save & continue" clicks each filled row's own Save first and drops empty unsaved rows.
* No server-side draft exists: rows are saved individually by the page (as before); nothing unsaved survives a reload.
* The Ask Claude button is hidden while a step is open on a phone; with the v2 job page the sticky bar sits above its bottom bar.
* Guard: `tests/smoke_dcr_steps.py` (in `run_gate_ci.py`). Next: the same step pattern for Payments / Materials / Expenses.

### Client portal: type scale + Drawing Markup stays in the shell (Bookore, 2026-10-16)

* `portal_shell.html`: one override block (before `</head>`) moves the portal onto the dashboard's type scale — nothing under
  13px except caps labels (12px) and the dense look-ahead grid (11px), weights 400/500/600 (was 700 / 9–11px), page title 22px/500
  caps, card title 16px/500, KPI label 13px caps, KPI value 28px/500. `html body` prefixes match typography.css specificity.
  Family was already Inter everywhere; the mismatch was sizes and weights.
* Drawing Markup is a portal view now: `data-portal-view="drawing"` holds an iframe of `/drawing-markup` (same pattern as the project
  dashboard), so the sidebar + top bar stay. `.content.is-bleed` makes it full-width. `/drawing-markup` is still a working direct URL.

### Job page redesign step 2 — six tabs on the v2 overlay (Bookore, 2026-10-16)

* Branch `worktree-six-tab-job-overlay`. Mockup accepted (`../docs/review-2026-10-16-six-tabs/`); from 2026-10-16 Angela
  reviews only the prototype, then builds ship straight to production (no post-build screenshot review). Opt-in only (`?ui=2` / saved preference); v1 is byte-for-byte unchanged.
* `static/v2/job.js`: six tabs (Overview · Today · Work · Documents · Money · Team); Today = Daily Report, Field Photos,
  Toolbox Talks. Superintendents land on Today once (never over a `#hash` deep link). **+ New** menu (Daily report, Photo,
  Delivery, RFI, Expense, Blank form or sign = Templates). Phone: bottom tab bar (four tabs + More past five; pm/super have
  five, admin/c_suite six), top tab row hidden ≤860px; the Ask Claude button and "Switch back" pill sit above the bar.
* Merges are pill-level only: Drops / Drop report, Permits & violations, Costs / Costs by product keep their own screens
  (Violations and Templates lose their pill; Violations was an empty placeholder). Real content merges are step 6.
* Money is still removed from the page for pm/super (they get five tabs). `tests/smoke_job_v2_preview.py` updated (six tabs etc.).
* Next: step 4 pilot (turn `?ui=2` on for Angela, one PM, one super). Step 3 (job types from the footprint data) waits for usage numbers, about 2026-10-29.

### How changes ship now (Bookore, 2026-10-07)

* **No preprod step.** Build on a branch, verify on a local isolated DB, send Angela screenshots
  (and a clickable mockup first for visual work). On her "accepted": push the branch to
  `upstream`, PR straight into `superstars-contracting/dashboard` main, merge when both gate
  checks pass, verify production, then fast-forward fork `main` to `upstream/main`.
* The preprod Render service (`ssc-dashboard-preprod`) is being suspended by Angela; the
  `preprod` branch no longer exists anywhere.
* Shipped today on this flow: one title per page + pinnable sidebar (#96), clean dashboard +
  team tasks & events (`team_items.py`, #97), task/event addresses with Google Maps directions
  (`/api/team/address-suggest` uses NYC GeoSearch; free, no key; best-effort).

### Worker app photos upload (Bookore, 2026-10-07)

* Angelajm6/superstars-os#94, released as superstars-contracting/dashboard#38 (live 13:29 UTC).
  * **Bug:** the worker app's Send only bumped a local counter, so every photo was discarded.
    Photos taken before the fix can't be recovered.
  * **`POST /api/worker/photos`** (under the public `/api/worker/` prefix):
    * Authenticated by the worker-app bearer session on an OPEN shift. The photo lands on that
      shift's job; a form `project_code` is ignored.
    * Stored in `field_photos` with `drop_id` NULL (Unassigned) and `worker_id` = employee_id.
      The caption comes from the location and scope buttons.
    * Limits: 15 MB per photo, 300 per worker per job per day.
  * **`worker-app.html`:** a photo counts as sent only when the response carries a stored id.
    The service worker answers `/api/*` offline with 200 `{error:'offline'}`. On failure the
    app keeps the photo and shows "Try again".
  * **Service worker:** `CACHE_VERSION` bumped to `v8-2026-10-07a`. Bump it again on every
    `worker-app.html` change.
  * **Guard:** `tests/smoke_worker_photos.py`.
  * **Still to check:** one real phone photo. Browser tests used a generated image because
    the test browser has no camera.

### CSRF guard enforcing (Bookore, 2026-10-06)

* Angelajm6/superstars-os#92, released as superstars-contracting/dashboard#37 (deploy 20:42 UTC). Production boot log reads
  `csrf guard mode=enforce`; a cross-site POST to `/api/auth/login` returns 403, a same-origin
  one goes through. The production deploy ran as a **blueprint sync**, so `render.yaml` env
  values do reach the production service (the first instance of that deploy briefly booted with
  the old `report` value before the synced env var took over). Details under the 2026-10-05 batch
  (`csrf_guard.py`).

### Edit projects and clients (Bookore, 2026-10-06)

* **Projects — `project_details.py`:** `GET /api/projects/<code>/details` (internal roles; the
  #263 hook keeps a pm to their jobs; `can_edit` = admin / c_suite) and
  `PUT /api/admin/projects/<code>/details` (admin / c_suite) for name, address, city / ZIP, client
  (`client_org_id` or `new_org_name`, which reuses a same-named active client), start / target
  dates and contract value. The project CODE never changes.
  - Validates everything before writing; a refused edit leaves nothing behind (no orphan client).
  - New address → the job and its approved estimate re-link to the building (`find_or_create`).
  - New client → legacy `projects.client` text, the approved estimate and the building move
    (the building stays if another client still has a job there).
  - Contract value = the converted estimate's `final_amount`; an AIA job's contract is its
    schedule of values, so editing it here is refused with that message.
  - Each save lands on the client timeline(s): "Project X updated: name, address…"; values never
    logged.
* **UI — job Settings tab → Project details:** read-only list for everyone (contract value only for
  company roles), **Edit details** for admin / C-suite; saving refreshes the name / address /
  client across the page without a reload.
* **Clients — company console client page:** **Edit client** (name, relationship, active /
  inactive, notes) and an **Edit** link per contact (name, title, phone, email, **Remove contact** =
  `status='inactive'`, hidden from the client page and contact counts). Uses the existing
  `PUT /api/crm/organizations/<id>` and `/api/crm/contacts/<id>` (section `crm` = admin / c_suite),
  now refusing a blank name / invalid contact status; client edits log "Client details updated: …".
* **Test:** `tests/smoke_edit_projects_clients.py` (both gates).

### 2026-10-05 batch — security hardening, New project flow, per-job tabs, job dashboard v2 preview (written up 2026-10-06)

Shipped to production 2026-10-05 (release PRs superstars-contracting/dashboard #31–#35) from
another workstation; this entry records it after the fact.

* **Security**
  - `request_logging.py` — metadata-only request log (method, sanitized path, status, body
    size; no bodies or query strings; `/p` `/i` `/d` token links and `/worker-files` masked).
  - `secret_box.py` — Fernet field encryption helper; key `SSC_FIELD_ENCRYPTION_KEY` (op://
    reference in `.env.template`, comma list for rotation, fails closed). **Not wired to any
    table yet**; existing plaintext values have no `v1:` prefix, so a future migration must
    re-wrap them. `op run --env-file=.env.template` fails until the 1Password item exists.
  - `runtime_seams.py` — the six test seams (fake Google verify, doc / expense scan, trace,
    mail and reset-mail dirs) only work with `SSC_ALLOW_TEST_SEAMS=1` AND off Render
    (`RENDER=true` or `SSC_ENVIRONMENT=production`) — this includes **preprod**, so a fake mail
    dir set there no longer diverts mail.
  - `csrf_guard.py` — Origin / Sec-Fetch-Site check on every write. **Enforce by default since
    2026-10-06** (cross-site writes get 403; code default and `render.yaml` both say `enforce`,
    an unknown value also enforces) with
    `SSC_ALLOWED_ORIGINS=https://app.superstarscontracting.com`. Switched after production logs
    since the 2026-10-05 release showed no would-be block from a real user (the only one was a
    curl probe). Boot logs `csrf guard mode=...` — check it after a deploy. Escape hatch if a
    legitimate flow is ever refused: set `SSC_CSRF_MODE=report` on the Render service.
* **New project flow** — "+ New project" opens the shared `static/js/new_project_modal.js`
  in place (console and Assignments page); five required fields first, the rest under "More
  details"; typing an existing active client's name reuses it.
* **Per-job tabs** — `project_modules.py`, `projects.enabled_modules` (NULL = every tab on);
  9 optional modules switched in the job's Settings by admin / c_suite. (New jobs started with
  none on until the 2026-10-06 fix above.)
* **Console** — job dashboard "← All projects" + "Company dashboard" for company roles; Client
  portal moved under Clients (`/company#portal` still works); console Dashboard tiles and rows
  link to the view behind each number.
* **Job dashboard** — compact phone layout (≤860px); **v2 preview** (`job_v2.py`,
  `static/v2/job.css|js`): `?ui=2` or a saved v2 interface preference gives top tabs
  (Overview, Work, Documents, Money, Team) over the same screens, only on a project's own page
  for internal roles; the login redirect keeps `?ui=1|2`.
* **Tests** — smoke_request_logging, smoke_secret_box, smoke_runtime_seams, smoke_csrf_guard,
  smoke_project_modules, smoke_projects_flow (was `…_361`, renamed 2026-10-06 — #361 is the
  density pass), smoke_job_v2_preview, smoke_login_next_query, smoke_company_dashboard_links;
  all in both gates since 2026-10-06.
* **Workflow change** — the fork's `preprod` branch was deleted; the preprod Render service
  deploys from fork `main`. Review PRs target fork `main`; production release = fork `main` →
  upstream `release/prod-<date>` → PR to `superstars-contracting/dashboard` main. The stale
  `develop` branch (2026-08-27, nothing unique) was deleted 2026-10-06.

### Flow fixes after the 2026-10-05 work (Bookore, 2026-10-06)

* **New jobs start with every tab on.** `project_modules.NEW_JOB_DEFAULT = None`: "+ New project"
  leaves `projects.enabled_modules` NULL (never customized), exactly like existing jobs, so a
  module added later is on for them too. Before, new jobs stored `[]` and hid Employees, Drawing
  Markup, Drop Plan/Report, RFIs, Materials, Toolbox Talks, Compliance… from PMs and supers.
  Jobs created with "+ New project" between the 2026-10-05 release and this fix still have `[]`
  stored — switch their modules on from the job's Settings tab (or reset the column to NULL).
* **Onboarding refuses before it creates.** Billing type, modules, PM and an existing client id
  are validated before a new client company can be created (a refused onboarding used to leave an
  orphan `crm_organization` row).
* **Drill-throughs to switched-off tabs** on the job dashboard: header links hide, KPI tiles and
  rows become plain text (`pruneGotos` after `JX_render`, `window.SSC_viewOn`); Contacts → Crew
  → Open only shows when Employees & Certs is on.
* **`/admin/projects?new=1…`** ("Create project" links opened in a new tab, bookmarks) waits for
  DOMContentLoaded — `new_project_modal.js` is deferred, so the form never opened before.
* **Job dashboard "Latest photos"** (#358) linked to `/api/photos/<id>/thumb`, which never
  existed; it is `/api/field-photos/<id>/thumb` now (broken images in production since #358).
* **Tests:** `smoke_project_modules` (new job = every tab on and NULL stored; three refused
  onboardings leave no client; ?new=1 + pruning markup) and `smoke_job_dashboard_358` (a seeded
  photo's card thumbnail loads, 200).

### #349 — Delete permanently: an ID card alone no longer blocks (Bookore, 2026-09-27)

* Test worker W-0027 couldn't be deleted: it had one company ID card (issued during testing).
  Angela chose "remove cards too": a card on its own is not work history (a card can be
  printed at onboarding), so for a worker with no sign-ins / certs / documents / photos /
  sign-offs / vehicle / login, Delete permanently now also removes their `cof_cards` /
  `company_id_cards` rows and the card files they reference (only inside
  `data_room/credentials`). The preview returns `cards` counts, the confirm lists them, the
  `worker_delete` audit keeps the card IDs. Anyone with real history is still refused.
* Note: project crew links were never the blocker — `project_assignments` are purged with the
  worker; crew is edited under Projects admin → Crew & site (uncheck a worker).

### #348 — DCR 109 → 097 fix: a leftover 097 folder no longer blocks it (Bookore, 2026-09-26)

* On production the fix refused with "A folder for DCR-FR-BX-001-097 is already on this
  server's disk" although no report 097 exists — a report 097 was deleted before #344, and
  the old delete left `internal.pdf`, so the folder survived.
* A target folder with NO report row is now a "leftover": the preview lists its known files
  (other file names are counted, never echoed) and the apply moves it to
  `data_room/_maintenance_backup/<stamp>-dcr-renumber-…-leftover-097/` (never deleted; put
  back if the apply fails). A folder WITH a report row still blocks (the number is taken).

### #347 — C-suite can run the one-time fixes (Bookore, 2026-09-26)

* Angela's production login is c_suite, so the admin-only fix boxes never appeared. On her
  explicit instruction ("allow C-suite to run the fixes") these are now admin + c_suite
  (`requires_company`): the #344 DCR 109 → 097 fix, the #346 worker ID cleanup (preview,
  apply, PDF batches) and #345 Delete permanently (still refused for any worker with
  history). pm/super stay 403. All three remain pinned/guarded; the fixes still run once.

### #346 — one-time worker ID cleanup: W-0030/31/32 → W-0027/28/29 (Bookore, 2026-09-26)

* **Why:** three empty test workers (W-0027..29) were added by mistake; Angela asked for
  them to be removed and the three real workers after them to move up with all their data.
  This overrides the "worker IDs never change" rule for this one case only — the map is
  PINNED in `worker_id_cleanup.py` and the action runs once (its audit row is the marker).
* **What moves:** `employees.worker_id`, `labor_worker_state.worker_id`,
  `labor_rate_change.worker_id`, `field_photos.worker_id` (one transaction; nothing has an FK
  on worker_id), plus every saved DCR file (internal/client.html) that prints the old IDs —
  standalone IDs only. Originals go to `data_room/_maintenance_backup/<stamp>-worker-renumber-346/`;
  stale internal.pdf files are set aside and regenerated in batches of 3
  (`POST …/worker-renumber-346/pdfs`, the UI loops). E-keyed history (sign-ins, certs,
  documents) does not move. Issued card rows are NOT rewritten — the preview lists who
  holds a card printed with the old number (re-issue it).
* **Guards:** targets absent from every W-keyed table, card numbers and saved DCRs; no old
  ID inside another code in a saved DCR (the reconcile reads `W-\d{4}` loosely); plan_hash
  re-checked under an advisory lock; the sign-in ↔ DCR divergence report must be identical
  before and after, otherwise rows AND files are put back.
* **CoF fix:** `_next_cof_revision` also stays above every existing card_id with the same
  card number, so a new hire given a freed number (W-0030) can't collide with the old card.
* **UI:** admin-only "One-time worker ID cleanup" box at the top of Workforce; hides once
  applied and all PDFs are regenerated.
* **Runbook (production):** Render DB backup → Workforce → delete W-0027, W-0028, W-0029
  (Delete permanently, #345) → the box turns green → Apply → let the PDFs finish → re-issue
  any listed cards.
* **Tests:** `tests/smoke_worker_id_cleanup_346.py` (both gates; synthetic W-9xxx map).

### #345 — Workforce: Active / Inactive, make inactive, reactivate, delete (Bookore, 2026-09-26)

* **What:** the Workforce list gets an **Active (N) / Inactive (N)** toggle. Inactive =
  archived (`employees.archived_at` + `archived_reason`) OR labor-inactive
  (`labor_worker_state`, still PM-approved via /labor-rates — archiving deliberately does NOT
  touch it). The Inactive list shows status, since, reason, last on site and a Reactivate
  button. A worker page gets **Make inactive** (asks for a reason), **Reactivate** (clears
  both archive and labor-inactive), and — admin only — **Delete permanently**.
* **Delete rules (`worker_lifecycle.py`):** anything proving they worked or hold credentials
  BLOCKS it with a 409 "make them inactive instead" (sign-ins, hour edits, certs, IDs,
  documents, CoF / company-ID cards, field photos, drop-plan sign-offs, vehicle driver, a
  dashboard login). Setup rows (assignments, rates, labor state + rate changes keyed by
  W-####, devices, sessions) are purged with the worker, dependents first (Postgres
  enforces the FKs), then the worker_records folder after commit. Every table is probed in
  the catalog first. Audit rows (`worker_archive` / `worker_reactivate` / `worker_delete`)
  carry IDs, trade and counts only — no names.
* **Also:** headcount + the Overview tile count active workers only (`v_active_workers`,
  roster rule); the old `DELETE /api/employees/<E>` is now company roles only and uses the
  same rules.
* **Routes:** `POST /api/company/portal/workers/<E>/archive|reactivate` (admin/c_suite),
  `GET …/removal` (preview), `DELETE /api/company/portal/workers/<E>` (admin).
* **Tests:** `tests/smoke_workforce_archive_345.py` (both gates).
* **Next (#346):** Angela deletes the empty test workers W-0027..29 with this Delete, then
  the one-shot W-0030..32 → W-0027..29 renumber runs.
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

### #351 — Drawing Markup + Weekly Summary "refuse to connect" fixed (Bookore, 2026-09-28)

* **Cause:** #297's perimeter headers sent `X-Frame-Options: DENY` + `frame-ancestors 'none'`
  on EVERY response, and the project dashboard embeds two pages in iframes
  (`/drawing-markup`, `/api/projects/<code>/weekly/render`) — so the browser blocked both
  ("refuses to connect") since #297.
* **Fix:** `http_security._SAME_ORIGIN_FRAMEABLE` — only those two paths get
  `SAMEORIGIN` / `frame-ancestors 'self'`; every other page (and look-alike paths) keeps
  DENY / 'none'. Another site still cannot frame anything. Add a path there if the
  dashboard ever embeds a new page.
* **Test:** `tests/smoke_http_security_297.py` now checks the behaviour on a bare Flask app.
### #352 — Drop Plan: column repair per drop (L × W × D) (Bookore, 2026-09-28)

* **What:** each drop's repair panel has a **Concrete patch / Column repair** switch with the
  same L × W × D inputs (in/ft per dimension, live CF). A column repair is a
  `quantity_entries` row on the project's `COL-REPAIR` SOV line ("Column repair", CF),
  created on first use by `dropplan_rollups.ensure_column_repair_sov` when the POST carries
  `kind: "column_repair"` (no sov_line_item). It needs all three dimensions.
* **Totals kept apart:** `drop_summary.volume_to_date` stays patch-only (the "patch CF"
  hero); new `column_repair_to_date` feeds a "column repair CF" hero stat (shown once any is
  logged) and the panel's second running total. The project rollup lists both SOV lines.
  The Drop Report gets a "Column repairs logged" section + a summary figure.
* **Unchanged on purpose:** Product Usage (materials) consumes ALL logged volume — column
  repairs use the same repair mortar. The legacy `/dropplan` page never lists column repairs
  as patches and never picks COL-REPAIR as its patch line. The new SOV line shows in the
  financial Schedule of Values with no unit rate until one is set.
* **Test:** `tests/smoke_column_repair_352.py` (both gates).
### #362 — Per-tab clean-up: one tab header, responsive grids, inline styles (Bookore, 2026-10-02)

* **One tab header** (`.tab-head` + `.tab-sub` + `.tab-actions`, `.tab-lbl`, `.tab-sel`):
  Drop Report, Weekly Summary, Toolbox Talks, RFI Log, Violations, Schedule of Values,
  NYC Compliance, Templates and Project Settings now share it (the other tabs already had
  a module head; Drawing Markup stays full-bleed). A shared empty state `.tab-empty` exists
  for the per-tab rework (#363–#367).
* **Responsive fixes:** three inline fixed grids beat their own media queries — the DCR
  project-info card (4 cols), the DCR weather block (3) and the AIA application row (5).
  They are classes now and collapse at 1024 / 640.
* **One primary colour, finished:** the inline red primaries (Compliance refresh, New RFI,
  Weekly Summary Open, DCR Issue / tile Save) are `.btn-primary`; secondary actions are
  `.btn-ghost`. Templates' category chips use the shared chip style (the script toggles
  `.active` only), and the Toolbox chip handler is scoped to its own filter row so the two
  no longer switch each other.
* **Inline styles 531 → 393:** repeated ones became classes (`.dcr-sec`, `.dcr-add`,
  `.dcr-in`, `.dcr-block`, `th.th-snug`, `td.td-snug`, `td.td-empty`, `.t-sub`, `.cw-*`).
* **Guard:** `smoke_design_conventions` `tabs_*` checks — an inline-style ceiling (393;
  lower it as tabs are cleaned, never raise it), a header on every tab, no fixed inline
  grids, no inline red primary buttons.

### #361 — Density pass, global rules (project dashboard) (Bookore, 2026-09-29)

* **Type floor:** no screen text under 11px; under 12px only for uppercase / letter-spaced
  labels and the dense grids (`.dp-*` drop board, `.ph-*` health widgets, `.wx-*`,
  `.cal-*`, `.la-*` look-ahead, photo-thumbnail overlays). 187 sizes moved (8–11.5px →
  11/12px). `@media print` / `@page` are exempt (printed reports keep their sizes).
* **Table headers:** ONE rule — light fill (`--fill`) + `--ink-2` 11px/600 caps. The dark
  default is gone (it showed grey-on-dark wherever a module set only the colour); the 10
  module header rules and 34 inline `<th style>` keep only padding / alignment.
* **Titles:** view 20px, dialog 18px, card 15px (was 16–28px).
* **Dialogs:** four sizes, `--modal-sm/md/lg/xl` = 480/640/880/1080px (were ten widths).
* **Primary buttons:** DCR, pick-list, payments and hero primaries moved from navy
  `--ui` to the one primary colour `--accent` (the #236 rule).
* **Breakpoints:** 1024 / 860 / 640 only (920→1024; 900, 768, 760→860; 680, 600→640).
* **Guard:** `smoke_design_conventions` gains `density_*` checks (type floor with a
  self-test, light header + no dark overrides, title scale, dialog sizes, one primary,
  breakpoints). Verified: every tab at desktop and 375px — no sideways scroll, no clipped
  text, nothing under 11px.

### #360 — Client portal tab; Company vs Settings untangled (Bookore, 2026-09-29)

* **Why:** Settings (`/#settings`) drew the Company sub-tabs and Team & access sat under
  Company, so the two read as the same place; client-portal access was spread over Team &
  access (login), Projects → Assignments (project + section toggles) and the project
  dashboard (per-item sharing).
* **Sidebar (`company_nav.js`, v=360):** new **Client portal** tab (`/company#portal`).
  Company = Documents & vehicles · Compliance documents · Specs. Settings (pinned) owns
  **Team & access** (`/admin/users`) + **Preferences & security** (`/#settings`, the old
  "coming next" placeholder replaced by link cards); it lights up on `/admin/users`.
* **`portal_access.py`:** `GET /api/company/portal-access` (@requires_company) — one read
  joining each external login's state (inactive → link a project → nothing unlocked →
  invited → live), linked project + client company, sections, matching preset, counts;
  architects listed separately with what their role opens; `can_create` = admin. Nothing
  logged. Every write reuses its owning endpoint and gate (pm-assignments, client-grants,
  preset, reset-password, create user — admin only, unchanged).
* **Tab UI (`company_portal.html`):** KPI band, a 4-step "how it works" strip, the logins
  table (Preview · Manage), a Manage dialog (project, section switches, preset, preview,
  password reset with the one-time password shown once) and "+ Add client login" (admin).
  The old client cards on Projects → Assignments are removed; that section links here.
* **Test:** `tests/smoke_client_portal_tab_360.py` (both gates); `smoke_one_home_339`
  updated for eight tabs and the Settings sub-tabs.

### #359 — Collapsible sidebar rail + layout base (Bookore, 2026-09-29)

* **Rail:** `static/js/shell.js` adds a « button to the sidebar's brand row on every
  company page (`aside.ssc-side`) and on the project dashboard (`<aside class="sidebar"
  data-rail>`; the client portal has none). It toggles `html.side-rail` — a 68 px icon
  rail; labels stay in the markup (font-size 0) and become each link's tooltip +
  aria-label (a link's own tooltip is kept). Remembered per browser (`ssc.side.rail`);
  each internal page sets the class in `<head>` before the first paint. Desktop only:
  every rule sits inside `@media (min-width: 861px)` in `widgets.css`, so the phone
  Menu drawer is unchanged.
* **Layout:** project views stop at 1440 px and centre (Drawing Markup stays full-bleed),
  side gutters shrink with the window, the Live line is slimmer and views start closer
  to it; the dead top-bar CSS is gone.
* **Cache keys:** `widgets.css?v=359` and `shell.js?v=359` on all eight shells (one key
  each, asserted).
* **Test:** `tests/smoke_layout_rail_359.py` (both gates).

### #358 — Job dashboard: executive summary (Bookore, 2026-09-29)

* **`project_summary.py`:** one call builds the job's summary — header (status, billing
  type, start → target, days left / over, % of time used, client, PM, superintendent),
  schedule (drops complete / active / behind / not started from the Drop Plan rollups,
  worst three), field today (on site, DCR issued today + last DCR, safety last 7 days),
  risk (open + overdue RFIs, drawing-markup questions, ACTIVE DOB violations / complaints,
  crew certs expiring ≤ 30 days), contacts (main per role), latest 6 photos. **money**
  (contract incl. change orders, invoiced, collected, open AR aged 0 / 1–30 / 31–60 /
  61–90 / 90+, balance to finish, costs to date) is BUILT only for admin / c_suite — the
  key is absent for pm / super. Each block is safe-wrapped: a missing table reads as zero.
* **Where it rides:** a `summary` part of `/api/projects/<code>/bootstrap` (no extra
  page-load request — perf budget unchanged) and `GET /api/projects/<code>/summary`
  (internal roles, project scoping applies).
* **Page:** Project Health is now header + chips, a KPI band (every tile drills through),
  and cards: Schedule health, Money (inside `SECTION:financial`), Field today, Open items
  & risk, Key contacts, Latest photos — each with a "View →" link. The old widget grid
  sits under a collapsible **Operations** bar (closed for admin / c_suite, open for
  pm / super; remembered per browser). The old fixed money block and the dead 30 s
  `refresh()` poll are gone (the RFI table now loads when its tab opens).
* **Test:** `tests/smoke_job_dashboard_358.py` (both gates).

### #357 — Project contacts: ownership group, property managers, consultants, site supervisors (Bookore, 2026-09-29)

* **`project_contacts.py`:** `project_contact` links a job to a CRM person (`contact_id`) or,
  for OUR site supervisors, an employee (`employee_id`), with role owner | property_manager
  | consultant (discipline architect | structural_engineer | mep_engineer | expeditor |
  other) | site_supervisor; one main contact per (role, side); soft remove. Adding a new
  person can create the company too (relationship type from the role).
  `crm.RELATIONSHIP_TYPES` + owner, property_manager, consultant, expeditor.
* **Access:** new section `project_contacts` = admin / c_suite / pm / super (read, with
  phone + email so the field can call); writes `requires_company`; `/contacts/lookup`
  names only; employees' phones never shown; contact PII never logged (test captures logs).
* **`project_people(conn, code)`** is the single source for document headers:
  superintendent = our main site supervisor (else the legacy column), project manager =
  first assigned PM, owner / client = the client company. Wired into `dcr_aggregator`
  (header + sign-off), the RFI render route, and `portal_projects.project_detail`. Old
  issued DCR files are untouched; regenerating one prints the current people.
* **UI:** project sidebar Overview → **Contacts**: Our team (PM, our supervisors, crew) +
  Ownership group, Property management, Consultants (by discipline), Owner / GC site
  supervisors; Call / Email buttons, ★ main contact, Remove; "+ Add contact" modal (pick
  from the CRM, new person + company, or one of our workers).
* **Not done:** `rfi_workflow.py` still routes RFIs through its hard-coded demo map — a
  follow-up could route to the job's consultants.
* **Test:** `tests/smoke_project_contacts_357.py` (both gates).

### #356 — Billing type per project (Bookore, 2026-09-29)

* **Why:** C-suite — "some projects do or don't have schedules of values". Nothing recorded how
  a job is billed; "has an SOV" was only inferred from `billing_sov` rows.
* **`billing_type.py`:** `projects.billing_type` = `aia_sov | lump_sum | time_materials |
  unit_price | NULL` ("Not set"), catalog-checked ALTER, idempotent backfill (NULL + has
  billing_sov rows -> aia_sov; a picked value is never overwritten). `GET
  /api/projects/<code>/profile` (status + billing), `PUT /api/admin/projects/<code>/billing-type`
  (`requires_company`; 409 when AIA pay applications exist and the new type isn't AIA).
* **Effects:** onboarding asks "How is it billed?" (required in the modal; the API accepts it
  optional so estimate conversion still works); the page renders `<body data-billing-type>`
  (server-side, `_render_billing_type`) — `.aia-only` pieces (SOV editor, AIA card) hide on
  other types via CSS, `.bt-note` explains, a warn banner shows when not set; the Schedule of
  Values nav link is rendered `hidden` on non-AIA jobs; Settings shows the real status and an
  editable billing row (admin / c_suite; read-only for others); AIA write routes (save SOV,
  import, new application) 409 on non-AIA jobs, not-set still allowed; Projects list gets a
  Billing chip. Drop-plan unit rates stay visible (they are field tracking, not billing).
* **Test:** `tests/smoke_billing_type_356.py` (both gates).

### #355 — Company vehicles: mileage, service, E-ZPass tolls, car bills (Bookore, 2026-09-29)

* **Ask:** per company car — E-ZPass assignment, mileage, service dates, which tolls it pays,
  car bills; "know how many miles they've driven, when they were last serviced".
* **Angela's choices:** tolls by **statement import** (E-ZPass has no API), mileage as an
  **odometer log**, car bills **into Accounts Payable**.
* **Module `fleet.py`** (registered after payables; company gate = admin / c_suite):
  `vehicle` + `ezpass_tag/ezpass_account/service_every_miles/service_every_months`;
  `vehicle_odometer` (manual | service | bill), `vehicle_service`, `vehicle_toll`
  (`dedupe_key` UNIQUE), `payable.vehicle_id`, `payable_file` (under
  `data_room/payables/<id>/`, served only by `/api/company/portal/payables/files/<id>`).
* **Numbers:** miles in a period = last reading ≤ end − last reading before start (else the
  first inside); avg/month over the span the readings cover (≤ 12 mo); next service =
  last service + every N months / N miles (default 6 mo / 5,000 mi), "soon" within 30 days
  or 500 mi, "overdue" past either; a reading lower than an earlier one is refused.
* **E-ZPass import** (`POST /api/company/portal/vehicles/tolls/import`, CSV or XLSX): finds
  the header row below account-info lines, fuzzy column match (date/time/tag/plate/agency/
  entry/exit/amount), skips payment rows (no tag/plate), matches by tag (digits, leading
  zeros ignored) else plate; unknown tags are REPORTED, not stored; re-upload is a no-op.
  The real E-ZPass NY export layout hasn't been seen yet — if a statement doesn't parse,
  the error names the missing column; adjust `parse_statement` `_pick` keywords.
* **Car bill** = `payables.create(category='vehicle')` + `vehicle_id` + optional file +
  optional odometer + "already paid" (`payables.pay`). It shows in Finance → Accounts payable.
* **UI:** Company → Vehicles table gains Mileage / Service / Tolls (month) / Open bills;
  "Open" = fleet panel (KPIs + Mileage (12-month bars) / Service / Tolls / Bills /
  E-ZPass & intervals tabs); "Import E-ZPass statement" on the card.
* **Test:** `tests/smoke_fleet_355.py` (both gates).

### #354 — Photoreal: the drop grid sits on the real building (Bookore, 2026-09-29)

* **Seen on production:** the building rendered, but the grid floated above it. Causes:
  (1) street level came from the FIRST downward ray, taken while only Google's coarse root
  tiles were loaded, and was then locked; (2) the grid was stretched to NYC's recorded height
  instead of the traced drawing; (3) the elevation label didn't follow the grid's offset.
* **Fix (`building3d.js` `fitToTiles`):** re-fits every ~1.5 s and on each tile load —
  street level = the MEDIAN of downward rays 5 m outside each wall (one probe on a roof / in
  the rail cut can't win); each wall's grid is its own group, slid along the normal onto the
  real facade found by a sideways ray (±12 m window). Over photoreal the drawing's height is
  used (sy = 1). Labels follow; each drop column gets a "DROP n" tag.
* **Test seam:** `opts.photoreal.__testTiles` = a stand-in mesh (no network); `debugState()`
  exposes `fit`, `world`, `faceGroups`. Verified: ground -20 -> -26 guess corrected, facade
  snapped exactly 3 m out, 12 drop tags.

### #353 — Drawing Markup: the real building in 3D (Google Photorealistic 3D Tiles) (Bookore, 2026-09-28)

* **What:** the 3D view gets a **Photoreal / Massing** toggle. Photoreal streams Google's
  textured 3D mesh of the real building (no capture, no manual work), anchored at the
  model's reference lat/lng (the origin of every local coordinate), and the drop x level
  grid floats see-through on the real walls, still clickable. Default = photoreal when it
  is set up; massing otherwise, and on any key / quota / coverage error (with a note).
* **Renderer:** NASA-AMMOS `3d-tiles-renderer` 0.5.3 (Apache-2.0) vendored in
  `static/vendor/3d-tiles-renderer/` + three 0.169.0 add-ons (GLTFLoader, DRACOLoader +
  decoder, BufferGeometryUtils, Pass) in `static/vendor/three-addons/`. Our vendored
  three.module.min.js is byte-identical to the official 0.169.0 build. The page's import
  map maps bare `three` / `three/addons/` to those files (same URL building3d.js imports ->
  one three.js instance). The vector-tile plugins' `@mapbox/vector-tile` / `pbf` /
  `pmtiles` are lazy `import()`s the photoreal path never calls — not vendored.
* **Frames:** the library's OBJECT_FRAME is +Z north / +X west; ReorientationPlugin
  `azimuth: Math.PI` turns it into ours (+X east, -Z north). Heights are ellipsoidal: the
  grid starts at `ground_ellipsoid_m` (NYC ground_ft + GEOID -32.7 m) and is re-seated by a
  downward ray onto the loaded tiles just outside a wall.
* **Switch-on rule (`building_3d.photoreal`):** needs `SSC_GOOGLE_TILES_KEY` (browser key,
  referrer + Map Tiles API restricted) AND today's app-side cap not used up AND the official Google Maps logo
  at `static/vendor/google/google_maps_logo.svg` (policy: logo + data credits on screen) AND
  a located building. Reasons surface as no_key / no_logo / no_location.
* **Set up (2026-09-29):** key in Render on preprod (production: add the same
  `SSC_GOOGLE_TILES_KEY` on the ssc-dashboard service); logo = the "Gray" SVG from Google's
  `Google_Maps_Attribution_Assets.zip` (developers.google.com), vendored unmodified at
  `static/vendor/google/google_maps_logo.svg`, shown 18 px tall in the credits bar.
* **Cost + cap:** Map Tiles API "Photorealistic 3D Tiles" — 1,000 root-tileset requests /
  month free (each = one 3D session of up to 3 h), then $6 per 1,000. Google's "3D Tiles
  root requests per day" quota is NOT adjustable, so the cap is app-side: the key is NOT in
  the model payload; `POST /api/elevation/model3d/<code>/photoreal-session` counts a session
  in `photoreal_usage(day, sessions)` and returns the key, 429 past
  `SSC_GOOGLE_TILES_DAILY_CAP` (default 30/day ≈ 900/month). The key is referrer-restricted
  to our three hosts, so the app is its only consumer. Backup: a $1 billing budget alert.
* **Test:** `tests/smoke_photoreal_3d_353.py` (both gates). Verified locally with a dummy
  key: the module chain loads via the import map, the renderer requests Google's root
  tileset with the key, Google rejects it, and the view falls back to massing with a note.

### #343b (stage 2) — the long tail of status and neutral colors (Bookore, 2026-09-28)

* **What:** 287 more CSS colors in `dashboard-static.html` onto shared tokens (untokenized
  hex in CSS contexts 293 → 37; plus translucent tints following their solid). Same rules
  and transform as stage 1 (CSS contexts only, comments carved out, never JS/SVG, no nested
  var(), explicit fallbacks).
* **Families:** greens `#0F6E56/#2F7C57/#15806A/#178A6C` → `--status-complete-deep`;
  text reds `#A8382B/#A23B30/#A23C3C/#7A2E1B` → NEW `--status-behind-deep` (#A8382B, the
  portal's bad-pill ink); `#C8102E` (older brand red) → `--brand-red`; ambers → NEW
  `--status-warn` / `--status-warn-soft` / `--status-warn-edge` (the portal's warn pill
  #9A5B00 / #FFF4E5); secondary grays (`#54505A`, `#5B6270`, `#444`, `#666`, …) → NEW `--ink-2`;
  pale fills → NEW `--fill`, pale hairlines → `--line` (property-aware: a border becomes
  `--line`, a background `--fill`), near-white → `--canvas`; oat/warm grays → `--oat-ink` /
  `--status-oat` / `--cream`. Tints: rgba(15,110,86) → rgba(12,122,95), rgba(186,117,23) →
  rgba(154,91,0), rgba(200,16,46) → rgba(177,30,46), rgba(107,114,128) → rgba(140,146,160).
* **New tokens** live in `static/css/widgets.css :root`; dashboard-static loads it as
  `?v=3432`.
* **Left on purpose (37):** chart palettes and '?'-context colors, purple accents, the dark
  panel colors, faint "disabled" grays (#AEB4C2, #C2C4CA, …), light text on dark
  (#EAECEF), and a few one-off edge colors on soft fills.
* **Verified:** 17 inline scripts pass `node --check`; the 21 views open with no JS errors;
  the new tokens resolve in the browser.

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
* **Stage 2 (done, see above):** the 281 long-tail literals are status/semantic colors needing a
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
