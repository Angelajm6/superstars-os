# Subscriptions Tracking — Operating Discipline

**Started:** May 15, 2026
**Reason:** "hard workers to hard thinkers" — we don't back-fill subscription data later. Every signup adds a row at signup time. Every renewal updates one.

---

## Where the data lives

**Now (pre-dashboard):** the "Subscriptions ledger" Google Doc in Google Drive (moved out of this repo on 2026-10-21, when the repo became public: it lists account-owner emails, MFA methods and admin links). Edit it by hand. To seed the database, export it as `subscriptions_ledger.csv` into this folder locally (it is git-ignored) and run `apply_subscriptions_schema.py`.

**Later (once dashboard is on workstation):** `subscriptions` table in `superstars.db`, populated by importing this CSV once. From then on, the dashboard's Subscriptions module is the source of truth and the CSV becomes a historical export.

**Schema:** `schema_subscriptions.sql` defines the future table. Includes two computed views (`subscriptions_monthly_burn` by category, `subscriptions_upcoming_renewals` for the next 30 days).

---

## The discipline

Every time the company signs up for *anything* — recurring SaaS, one-time hardware, professional services, anything that generates an invoice or a renewal cycle — **add a row to the ledger at the moment of signup.** Not "later when I remember." At the moment of signup.

This is one of those rules that's annoying for 30 seconds and saves you weeks of forensic accounting in year 3.

### What to capture per row

- `service_name` — the full product name as the vendor uses it
- `provider` — the company (Google, 1Password, Lenovo, etc.)
- `category` — one of: Productivity, Security, Connectivity, Hardware, AI, Communications, Cloud, Development, Industry, Other
- `owner_email` — almost always `admin@superstarscontracting.com`
- `billing_email` — almost always `subscriptions@superstarscontracting.com`
- `plan_tier` — "Business Standard", "Pro", etc.
- `seats` — how many users/units
- `unit_cost_usd` — per-seat or per-unit cost
- `billing_cycle` — monthly, annual, quarterly, one_time, custom
- `start_date` — YYYY-MM-DD
- `renewal_date` — YYYY-MM-DD (or N/A for one_time)
- `status` — active, trial, paused, canceled, pending_auth
- `mfa_method` — yubikey, totp, sms, none, n/a — what protects the login
- `admin_url` — link to where you go to manage the subscription
- `notes` — anything else worth knowing

---

## Status flow

- **pending_auth** — we want this, Arun hasn't approved yet
- **trial** — we're in a free trial, will decide before charge
- **active** — paying customer
- **paused** — temporarily suspended (rare)
- **canceled** — gone, keep the row for historical reference

Never delete a row. Mark canceled. The history is the value.

---

## Seeded entries (May 15, 2026)

The ledger starts with what we know:
- T-Mobile Business Internet — active
- ThinkStation P3 — one-time hardware
- ThinkVision monitors (x2) — one-time hardware
- CyberPower UPS — one-time hardware
- YubiKey 5C NFC pair (admin) — one-time hardware
- Google Workspace Business Standard — **pending_auth**
- 1Password Business — **pending_auth**

When Arun authorizes the two pending subscriptions tomorrow, you sign up, then immediately update those two rows with:
- `start_date` = today
- `renewal_date` = today + 1 month
- `status` = active
- `account_id` = the vendor's customer ID once you have it

---

## Coming next

Future signups to anticipate (don't sign up yet, just know they're coming):

- **GitHub** — likely free at first, Pro ($4/mo) eventually for private repos with more features
- **Backblaze** — ~$99/yr per computer for cloud backup
- **Mullvad VPN** — €5/mo (~$5)
- **SendGrid** — currently being used somehow; needs to be moved under `admin@` ownership
- **Anthropic API** — $200 starter credits, then usage-based
- **Cloudflare** — free tier likely sufficient for Tunnel + DNS
- **Domain registrar** — wherever `superstarscontracting.com` is currently held
- **Bluebeam Revu** — ~$240/yr/seat
- **Microsoft Office for Bluebeam workflow** — TBD (or use Google Workspace's Office compatibility)

Each one of these adds a row at signup. Each one. Always.

---

## Why this matters for the bigger story

The Subscriptions module that eventually goes into the dashboard does three things:

1. **Cost visibility** — at any moment, what is the company paying per month? The `subscriptions_monthly_burn` view answers this.
2. **Renewal awareness** — what's billing in the next 30 days? The `subscriptions_upcoming_renewals` view answers this.
3. **Audit surface** — who controls what, what auth method protects it, where do you log in to cancel/change. Critical when handing off operations, when offboarding a tool, or when auditing security posture.

When `subscriptions@superstarscontracting.com` (the Google Group) starts receiving real invoices, an Apps Script can eventually parse those emails and update the ledger automatically. That's a phase-2 build. For now, the discipline is manual entry. The point is to start the habit before the volume gets unmanageable.
