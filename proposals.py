"""#307 — Proposals: generated from an estimate, sent with a secure link, accepted online.

A proposal is the document the client says yes to. Until now that was an email with
a PDF attached and an `estimate` row someone moved to "approved" by hand. This module
makes it a record:

    proposal(id, estimate_id, version, number, status, ...terms..., token_hash, ...)
    proposal_line(proposal_id, sort, description, qty, unit, unit_price, amount)
    proposal_event(proposal_id, event, at, actor_user_id, ip, detail)

Flow
----
    draft ──send──▶ sent ──(client opens)──▶ viewed ──accept──▶ signed
      │                │                       │
      │                └──────────decline──────┴──▶ declined
      └── new version ▶ superseded (the old one; the new one starts as draft)

* `create_from_estimate` prefills client / contact / building / price from the
  estimate (#304 building link, #273 estimate). Only a `draft` is editable; a sent
  proposal gets a NEW VERSION (v2, v3…) and the old one is `superseded`.
* `send` renders the document, tries a PDF (the app's chromium engine, best effort),
  stores it as `estimate_document` category 'proposal', emails a branded message with
  a signed, expiring link `${APP_BASE_URL}/p/<token>`. The token is random (32 bytes),
  only its sha256 is stored, and it expires at `valid_until` (default 30 days).
* The public page `/p/<token>` is reachable logged-out (auth._PUBLIC_PREFIXES). It
  shows the document, marks the proposal `viewed`, and lets the client accept with
  a typed-name signature (+ IP + timestamp) or decline with a reason.
* `signed_at` is THE trigger (ROADMAP item 4): the estimate moves to `approved` with
  the proposal price as `final_amount`, and CRM follow-up tasks are opened for the
  Certificate of Insurance and the deposit invoice (#309 wires QuickBooks to the
  latter). Every transition writes a `proposal_event` and a CRM activity row.
* Payload rule: no `*_path`, no token hash, no HTML snapshot in JSON — ever.
* Access: internal endpoints are `requires_section('estimates')` (admin / c_suite);
  public endpoints authenticate by the token alone and only expose the client-facing
  shape. Dates LOCAL. SQL portable (db_layer '?' placeholders).
"""
from __future__ import annotations

import hashlib
import json
import html as _html
import logging
import os
import re

import runtime_seams
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from flask import Response, jsonify, make_response, request, send_file

import company_brand
import crm
import db_layer
import estimates
import ssc_paths
from auth import current_user, requires_role, requires_section

log = logging.getLogger(__name__)

SECTION = "estimates"
STATUSES = ("draft", "sent", "viewed", "signed", "declined", "superseded", "deleted")   # deleted: hidden, restorable (2026-10-23)
OPEN_STATUSES = ("sent", "viewed")            # a client can still act
REVISABLE = ("sent", "viewed", "declined")    # a sent proposal that can get a new version (signed is final)
REVISION_STATUSES = ("pending", "approved", "rejected", "withdrawn")
EDITABLE_FIELDS = ("title", "scope_summary", "price", "deposit_pct", "schedule_text",
                   "exclusions_text", "terms_text", "valid_until", "recipient_email",
                   "recipient_name", "contact_id",
                   # Proposal Studio (2026-10-20)
                   "intro_text", "reference_text", "prepared_for", "project_text", "extra_sections",
                   "hidden_sections", "email_message",
                   "submitted_by")   # 2026-10-21: the SUBMITTED BY block, per proposal
LINK_DAYS_DEFAULT = 30
DEPOSIT_PCT_DEFAULT = 30.0
COMPANY_NAME = "Superstars Contracting"
DEFAULT_TERMS = (
    "This proposal is valid until the date shown above. Work begins once the signed "
    "proposal and the deposit are received and the Certificate of Insurance is on file. "
    "The balance is invoiced on progress and due within 15 days of each invoice. Work "
    "beyond the scope described here is quoted separately as a change order before it "
    "starts. Access, permits filed by the client, and unforeseen conditions discovered "
    "during the work are handled by written agreement between both parties."
)
DEFAULT_EXCLUSIONS = (
    "Engineering and filing fees unless listed above. Repairs to conditions concealed "
    "behind finishes. Overtime or off-hours work unless requested. Sales tax where "
    "applicable."
)


# ============================ helpers ============================

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> date:
    return date.today()


def _conn():
    return db_layer.connect(pragma_fk=True)


def _rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]


def _one(cur) -> Optional[dict]:
    r = cur.fetchone()
    return dict(r) if r else None


def _wrap(data, **meta):
    m = {"generated_at": _now()}
    m.update(meta)
    if isinstance(data, list) and "count" not in m:
        m["count"] = len(data)
    return jsonify({"data": data, "meta": m})


def _err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def _uid():
    return (current_user() or {}).get("id")


def _client_ip() -> Optional[str]:
    try:
        from auth import _client_ip as _ip
        return _ip()
    except Exception:
        return request.remote_addr


def _valid_date(s, field: str = "date") -> Optional[str]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{field} must be a date (YYYY-MM-DD)")
    return s


def _money(v) -> str:
    try:
        return "${:,.2f}".format(float(v or 0))
    except (TypeError, ValueError):
        return "—"


def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        d = date.fromisoformat(str(iso)[:10])
        return d.strftime("%B %-d, %Y")
    except Exception:
        return str(iso)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _base_url() -> str:
    return (os.environ.get("APP_BASE_URL") or "http://127.0.0.1:5050").rstrip("/")


def _has_column(conn, table: str, column: str) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute(
            "SELECT 1 AS x FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
            (table, column)).fetchone())
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


# ============================ schema ============================

def ensure_schema(conn) -> None:
    """Idempotent on both backends. No FKs by design (boot-order on a fresh preprod DB;
    same rule as #304)."""
    pk = ("id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" if db_layer.is_postgres()
          else "id INTEGER PRIMARY KEY AUTOINCREMENT")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS proposal ("
        f"  {pk},"
        f"  estimate_id      INTEGER NOT NULL,"
        f"  version          INTEGER NOT NULL DEFAULT 1,"
        f"  number           TEXT NOT NULL,"
        f"  status           TEXT NOT NULL DEFAULT 'draft',"
        f"  client_org_id    INTEGER NOT NULL,"
        f"  contact_id       INTEGER,"
        f"  building_id      INTEGER,"
        f"  title            TEXT,"
        f"  scope_summary    TEXT,"
        f"  price            REAL,"
        f"  deposit_pct      REAL,"
        f"  deposit_amount   REAL,"
        f"  schedule_text    TEXT,"
        f"  exclusions_text  TEXT,"
        f"  terms_text       TEXT,"
        f"  valid_until      TEXT,"
        f"  recipient_email  TEXT,"
        f"  recipient_name   TEXT,"
        f"  sent_at          TEXT,"
        f"  sent_by          INTEGER,"
        f"  viewed_at        TEXT,"
        f"  view_count       INTEGER NOT NULL DEFAULT 0,"
        f"  signed_at        TEXT,"
        f"  signed_name      TEXT,"
        f"  signed_title     TEXT,"
        f"  signed_ip        TEXT,"
        f"  declined_at      TEXT,"
        f"  decline_reason   TEXT,"
        f"  superseded_by    INTEGER,"
        f"  token_hash       TEXT,"
        f"  token_expires_at TEXT,"
        f"  pdf_path         TEXT,"
        f"  pdf_doc_id       INTEGER,"
        f"  pdf_rendered_at  TEXT,"
        f"  created_by       INTEGER,"
        f"  created_at       TEXT NOT NULL,"
        f"  updated_at       TEXT NOT NULL"
        f")")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_proposal_est_ver ON proposal(estimate_id, version)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_proposal_number ON proposal(number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proposal_token ON proposal(token_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proposal_client ON proposal(client_org_id)")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS proposal_line ("
        f"  {pk},"
        f"  proposal_id  INTEGER NOT NULL,"
        f"  sort         INTEGER NOT NULL DEFAULT 0,"
        f"  description  TEXT NOT NULL,"
        f"  qty          REAL,"
        f"  unit         TEXT,"
        f"  unit_price   REAL,"
        f"  amount       REAL"
        f")")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proposal_line_p ON proposal_line(proposal_id, sort)")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS proposal_event ("
        f"  {pk},"
        f"  proposal_id    INTEGER NOT NULL,"
        f"  event          TEXT NOT NULL,"
        f"  at             TEXT NOT NULL,"
        f"  actor_user_id  INTEGER,"
        f"  ip             TEXT,"
        f"  detail         TEXT"
        f")")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proposal_event_p ON proposal_event(proposal_id, at)")
    # 2026-10-22: a revision of a sent proposal is asked for, then an admin approves or rejects it
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS proposal_revision ("
        f"  {pk},"
        f"  proposal_id      INTEGER NOT NULL,"
        f"  requested_by     INTEGER,"
        f"  requested_at     TEXT NOT NULL,"
        f"  reason           TEXT NOT NULL,"
        f"  price_review     INTEGER NOT NULL DEFAULT 0,"
        f"  proposed_price   REAL,"
        f"  status           TEXT NOT NULL DEFAULT 'pending',"
        f"  decided_by       INTEGER,"
        f"  decided_at       TEXT,"
        f"  decision_note    TEXT,"
        f"  new_proposal_id  INTEGER"
        f")")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proposal_revision_p ON proposal_revision(proposal_id, status)")
    for col, typ in STUDIO_COLUMNS:   # Proposal Studio (2026-10-20): every part of the document is editable
        if not _has_column(conn, "proposal", col):
            conn.execute(f"ALTER TABLE proposal ADD COLUMN {col} {typ}")
    for col, typ in (("deleted_from", "TEXT"), ("deleted_at", "TEXT"), ("deleted_by", "INTEGER"),   # 2026-10-23
                     ("project_dismissed_at", "TEXT"), ("project_dismissed_by", "INTEGER")):   # "Not a project" on Ready to start
        if not _has_column(conn, "proposal", col):
            conn.execute(f"ALTER TABLE proposal ADD COLUMN {col} {typ}")
    if not _has_column(conn, "proposal_line", "lead"):   # the bold part of a line (2026-10-21)
        conn.execute("ALTER TABLE proposal_line ADD COLUMN lead TEXT")
    conn.commit()
    _number_drafts(conn)


def _number_drafts(conn) -> None:
    """Unsent drafts made before plain numbers get one (1042, 1043…), oldest first; anything already sent keeps the
    code the client saw (2026-10-21). Idempotent: only drafts with no number."""
    import proposal_engine
    rows = _rows(conn.execute("SELECT id FROM proposal WHERE status = 'draft' AND display_no IS NULL ORDER BY created_at, id"))
    for r in rows:
        conn.execute("UPDATE proposal SET display_no = ? WHERE id = ? AND display_no IS NULL",
                     (proposal_engine.next_number(conn), r["id"]))
        conn.commit()


# ============================ reads ============================

_JOIN = ("SELECT p.*, o.name AS client_name, e.code AS estimate_code, e.status AS estimate_status, "
         "e.est_type AS estimate_type, e.building_address, e.converted_project_code, "
         "b.name AS building_name, b.address_line AS building_address_line, "
         "b.city AS building_city, b.state AS building_state, b.zip AS building_zip, "
         "c.full_name AS contact_name, c.email AS contact_email, c.title AS contact_title "
         "FROM proposal p "
         "JOIN crm_organization o ON o.id = p.client_org_id "
         "JOIN estimate e ON e.id = p.estimate_id "
         "LEFT JOIN building b ON b.id = p.building_id "
         "LEFT JOIN crm_contact c ON c.id = p.contact_id ")


def get(conn, proposal_id: int) -> Optional[dict]:
    return _one(conn.execute(_JOIN + "WHERE p.id = ?", (proposal_id,)))


def _by_token(conn, token: str) -> Optional[dict]:
    if not token or len(token) < 20 or len(token) > 128:
        return None
    return _one(conn.execute(_JOIN + "WHERE p.token_hash = ? AND p.status <> 'deleted'", (_hash(token),)))


def lines(conn, proposal_id: int) -> list:
    return _rows(conn.execute("SELECT id, sort, description, lead, qty, unit, unit_price, amount "
                              "FROM proposal_line WHERE proposal_id = ? ORDER BY sort, id", (proposal_id,)))


def events(conn, proposal_id: int) -> list:
    return _rows(conn.execute("SELECT id, event, at, actor_user_id, ip, detail FROM proposal_event "
                              "WHERE proposal_id = ? ORDER BY at, id", (proposal_id,)))


def list_proposals(conn, *, status=None, client_org_id=None, estimate_id=None, building_id=None,
                   q=None) -> list:
    where, params = ["1=1"], []
    if status:
        where.append("p.status = ?"); params.append(status)
    else:   # a deleted proposal shows only under its own filter (2026-10-23)
        where.append("p.status <> 'deleted'")
    if client_org_id:
        where.append("p.client_org_id = ?"); params.append(int(client_org_id))
    if estimate_id:
        where.append("p.estimate_id = ?"); params.append(int(estimate_id))
    if building_id:
        where.append("p.building_id = ?"); params.append(int(building_id))
    if q:
        like = f"%{q.strip().lower()}%"
        num = re.fullmatch(r"\s*(?:proposal\s*)?(?:no\.?\s*)?#?\s*(\d+)(?:-\d+)?\s*", q, re.I)   # "Proposal No. 1045", "1045-2"
        where.append("(LOWER(p.number) LIKE ? OR LOWER(COALESCE(p.title,'')) LIKE ? OR LOWER(o.name) LIKE ? "
                     "OR LOWER(COALESCE(e.building_address,'')) LIKE ? OR LOWER(COALESCE(b.address_line,'')) LIKE ?"
                     + (" OR CAST(p.display_no AS TEXT) = ?" if num else "") + ")")   # 2026-10-22: "1045", the number people see
        params += [like] * 5 + ([num.group(1)] if num else [])
    return [public(r) for r in _rows(conn.execute(
        _JOIN + "WHERE " + " AND ".join(where) + " ORDER BY p.updated_at DESC, p.id DESC", params))]


def _internal_status(r: dict) -> str:
    """The lifecycle status plus 'expired' for an open link past its date (derived; the
    stored status stays what it is so a resend can revive it)."""
    st = r.get("status")
    if st in OPEN_STATUSES and r.get("token_expires_at") and r["token_expires_at"] < _now():
        return "expired"
    return st


def _label(r: dict) -> str:
    import proposal_engine
    return proposal_engine.label(r)


def public(r: dict) -> dict:
    """Internal JSON shape. NO paths, NO token hash, NO html."""
    return {
        "id": r["id"], "estimate_id": r["estimate_id"], "estimate_code": r.get("estimate_code"),
        "estimate_status": r.get("estimate_status"), "estimate_type": r.get("estimate_type"),
        "converted_project_code": r.get("converted_project_code"),
        "version": r["version"], "number": r["number"], "status": r["status"],
        "label": _label(r), "layout": r.get("layout"),
        "display_status": _internal_status(r),
        "client_org_id": r["client_org_id"], "client_name": r.get("client_name"),
        "contact_id": r.get("contact_id"), "contact_name": r.get("contact_name"),
        "contact_email": r.get("contact_email"),
        "building_id": r.get("building_id"), "building_name": r.get("building_name"),
        "building_address": r.get("building_address_line") or r.get("building_address"),
        "title": r.get("title"), "scope_summary": r.get("scope_summary"),
        "price": r.get("price"), "deposit_pct": r.get("deposit_pct"), "deposit_amount": r.get("deposit_amount"),
        "schedule_text": r.get("schedule_text"), "exclusions_text": r.get("exclusions_text"),
        "terms_text": r.get("terms_text"), "valid_until": r.get("valid_until"),
        "recipient_email": r.get("recipient_email"), "recipient_name": r.get("recipient_name"),
        "sent_at": r.get("sent_at"), "viewed_at": r.get("viewed_at"), "view_count": r.get("view_count") or 0,
        "signed_at": r.get("signed_at"), "signed_name": r.get("signed_name"), "signed_title": r.get("signed_title"),
        "declined_at": r.get("declined_at"), "decline_reason": r.get("decline_reason"),
        "superseded_by": r.get("superseded_by"),
        "link_expires_at": r.get("token_expires_at"),
        "has_pdf": bool(r.get("pdf_path")), "pdf_rendered_at": r.get("pdf_rendered_at"),
        "pdf_doc_id": r.get("pdf_doc_id"),
        "editable": r["status"] == "draft",
        "deleted_from": r.get("deleted_from"), "deleted_at": r.get("deleted_at"),
        "expires_in_days": expires_in_days(r), "revision_of": r.get("revision_of"),
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def expires_in_days(r: dict, today: Optional[date] = None) -> Optional[int]:
    """Days until a sent proposal's valid-until date (0 = today, negative = lapsed); None once the client has answered
    or it was never sent. LOCAL dates."""
    if r.get("status") not in OPEN_STATUSES or not r.get("valid_until"):
        return None
    try:
        return (date.fromisoformat(str(r["valid_until"])[:10]) - (today or _today())).days
    except ValueError:
        return None


# ============================ writes ============================

def _event(conn, proposal_id: int, event: str, *, actor=None, ip=None, detail=None) -> None:
    conn.execute("INSERT INTO proposal_event (proposal_id, event, at, actor_user_id, ip, detail) "
                 "VALUES (?,?,?,?,?,?)", (proposal_id, event, _now(), actor, ip, detail))


def _activity(conn, r: dict, summary: str, actor=None) -> None:
    try:
        crm.add_activity(conn, entity_type="organization", entity_id=r["client_org_id"],
                         activity_type="system", author_user_id=actor, function_tag="sales",
                         summary=summary)
    except Exception as e:   # the timeline is a courtesy, never a blocker
        log.warning("#307 activity not logged: %s", e)


def _deposit(price, pct) -> Optional[float]:
    try:
        if price is None or pct is None:
            return None
        return round(float(price) * float(pct) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def create_from_estimate(conn, estimate_id: int, *, created_by=None) -> dict:
    est = estimates.get_estimate(conn, estimate_id)
    if not est:
        raise LookupError("estimate not found")
    if est["status"] in ("lost",):
        raise ValueError("a lost estimate cannot be proposed; reopen it first")
    open_existing = _one(conn.execute(
        "SELECT id, number, status FROM proposal WHERE estimate_id = ? AND status IN ('draft','sent','viewed') "
        "ORDER BY version DESC", (estimate_id,)))
    if open_existing:
        raise ValueError(f"proposal {open_existing['number']} is already {open_existing['status']} "
                         f"for this estimate — edit it or create a new version from it")
    last = _one(conn.execute("SELECT MAX(version) AS v FROM proposal WHERE estimate_id = ?", (estimate_id,)))
    version = int((last or {}).get("v") or 0) + 1
    number = f"{est['code']}-P{version}"
    contact = crm.get_contact(conn, est["contact_id"]) if est.get("contact_id") else None
    org = crm.get_org(conn, est["client_org_id"]) or {}
    price = est.get("final_amount")
    kind = estimates.EST_TYPES.get(est["est_type"]) or str(est["est_type"] or "Work").replace("_", " ").title()
    title = f"{kind} — {est.get('building_address') or org.get('name') or est['code']}"
    now = _now()
    valid_until = (_today() + timedelta(days=LINK_DAYS_DEFAULT)).isoformat()
    conn.execute(
        "INSERT INTO proposal (estimate_id, version, number, status, client_org_id, contact_id, building_id, "
        "title, scope_summary, price, deposit_pct, deposit_amount, schedule_text, exclusions_text, terms_text, "
        "valid_until, recipient_email, recipient_name, created_by, created_at, updated_at) "
        "VALUES (?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (estimate_id, version, number, est["client_org_id"], est.get("contact_id"), est.get("building_id"),
         title, (est.get("notes") or "").strip() or None, price, DEPOSIT_PCT_DEFAULT,
         _deposit(price, DEPOSIT_PCT_DEFAULT), None, DEFAULT_EXCLUSIONS, DEFAULT_TERMS, valid_until,
         (contact or {}).get("email"), (contact or {}).get("full_name"), created_by, now, now))
    conn.commit()
    pid = _one(conn.execute("SELECT id FROM proposal WHERE number = ?", (number,)))["id"]
    # every new proposal is drawn in the locked Superstars layout (2026-10-21)
    import proposal_engine
    h = proposal_engine.house(conn)
    seeded = proposal_engine.seed(conn, dict(get(conn, pid)), org=org, contact=contact)
    conn.execute("UPDATE proposal SET layout = 'ssc', display_no = ?, content = ?, deposit_pct = ?, deposit_amount = ?, "
                 "valid_until = ? WHERE id = ?",
                 (proposal_engine.next_number(conn), json.dumps(seeded), h["deposit_pct"], _deposit(price, h["deposit_pct"]),
                  (_today() + timedelta(days=int(h["valid_days"]))).isoformat(), pid))
    if price and float(price) > 0:   # the document prints its lines' total: the estimate's price is the first line
        conn.execute("INSERT INTO proposal_line (proposal_id, sort, description, lead, qty, unit, unit_price, amount) "
                     "VALUES (?,?,?,?,?,?,?,?)", (pid, 0, "Base bid", "Base bid", 1, "LS", float(price), round(float(price), 2)))
    sync_price(conn, pid, layout="ssc")
    _event(conn, pid, "created", actor=created_by, detail=f"from estimate {est['code']}")
    conn.commit()
    r = get(conn, pid)
    _activity(conn, r, f"Proposal {number} drafted from estimate {est['code']}", actor=created_by)
    return r


def update(conn, proposal_id: int, fields: dict, line_items: Optional[list] = None, *, actor=None) -> dict:
    r = get(conn, proposal_id)
    if not r:
        raise LookupError("proposal not found")
    if r["status"] != "draft":
        raise ValueError("only a draft can be edited — create a new version instead")
    sets, vals = [], []
    for k in EDITABLE_FIELDS:
        if k not in fields:
            continue
        v = fields[k]
        if k in ("price", "deposit_pct"):
            if v in (None, ""):
                v = None
            else:
                v = float(v)
                if v < 0:
                    raise ValueError(f"{k} must be >= 0")
                if k == "deposit_pct" and v > 100:
                    raise ValueError("deposit_pct must be <= 100")
        elif k == "valid_until":
            v = _valid_date(v, "valid_until")
        elif k in ("extra_sections", "hidden_sections"):
            v = json.dumps(_json_list(v) if not isinstance(v, str) else _json_list(v))
        elif k == "contact_id":
            if v in (None, ""):
                v = None
            else:
                c = crm.get_contact(conn, int(v))
                if not c:
                    raise ValueError("contact_id does not exist")
                if c.get("org_id") and c["org_id"] != r["client_org_id"]:
                    raise ValueError("contact belongs to a different organization")
                v = int(v)
        else:
            v = (str(v).strip() if v is not None else "") or None
        sets.append(f"{k} = ?"); vals.append(v)
    if line_items is not None:
        conn.execute("DELETE FROM proposal_line WHERE proposal_id = ?", (proposal_id,))
        total = 0.0
        for i, li in enumerate(line_items):
            desc = (str(li.get("description") or "")).strip()
            if not desc:
                continue
            qty = float(li["qty"]) if li.get("qty") not in (None, "") else None
            unit_price = float(li["unit_price"]) if li.get("unit_price") not in (None, "") else None
            amount = (float(li["amount"]) if li.get("amount") not in (None, "")
                      else (round(qty * unit_price, 2) if qty is not None and unit_price is not None else None))
            total += amount or 0
            conn.execute("INSERT INTO proposal_line (proposal_id, sort, description, lead, qty, unit, unit_price, amount) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (proposal_id, i, desc, (str(li.get("lead") or "").strip() or None), qty,
                          (str(li.get("unit") or "").strip() or None), unit_price, amount))
        if "price" not in fields and line_items and total > 0:
            sets.append("price = ?"); vals.append(round(total, 2))
    sets.append("updated_at = ?"); vals.append(_now())
    vals.append(proposal_id)
    conn.execute(f"UPDATE proposal SET {', '.join(sets)} WHERE id = ?", vals)
    sync_price(conn, proposal_id, layout=r.get("layout"))
    conn.commit()
    return get(conn, proposal_id)


def line_total(conn, proposal_id: int) -> Optional[float]:
    """The base bid total exactly as the document draws it (each line rounded to the cent, then summed); None with no lines."""
    import proposal_layout as L
    lis = lines(conn, proposal_id)
    return L.total({"lines": lis}) if lis else None


def sync_price(conn, proposal_id: int, layout: Optional[str] = None) -> None:
    """ONE amount everywhere (2026-10-22): a proposal in the Superstars layout prints the total of its lines, so its stored
    price (the list, the email, the deposit invoice) is always that total and can't be set apart from them. The deposit
    amount stays derived from the price. Caller commits."""
    if layout is None:
        layout = (_one(conn.execute("SELECT layout FROM proposal WHERE id = ?", (proposal_id,))) or {}).get("layout")
    if layout == "ssc":
        conn.execute("UPDATE proposal SET price = ? WHERE id = ?", (line_total(conn, proposal_id), proposal_id))
    cur = _one(conn.execute("SELECT price, deposit_pct FROM proposal WHERE id = ?", (proposal_id,)))
    conn.execute("UPDATE proposal SET deposit_amount = ? WHERE id = ?",
                 (_deposit(cur["price"], cur["deposit_pct"]), proposal_id))


def logged_price_problem(conn, r: dict, price) -> Optional[str]:
    """A price read from outside the app (a proposal emailed from Gmail) can become the proposal's price only when the
    document agrees with it or has no breakdown of its own; else this says what to fix."""
    if r.get("layout") != "ssc" or price in (None, ""):
        return None
    price = round(float(str(price).replace("$", "").replace(",", "")), 2)
    lis, total = lines(conn, r["id"]), line_total(conn, r["id"])
    if total is not None and abs(total - price) < 0.005:
        return None
    if len(lis) > 1 or (lis and (lis[0].get("lead") or lis[0].get("description")) != "Base bid"):
        return (f"The proposal's price lines add up to {_money(total)}, not {_money(price)}. Change the lines in Proposal Studio "
                f"so the record matches what was sent, then log it.")
    return None


def log_price(conn, proposal_id: int, price, *, actor=None) -> dict:
    """Record the price a proposal was sent at outside the app. In the Superstars layout the price is the document's
    total, so a proposal with no breakdown (none, or the estimate's single "Base bid" line) gets that one line at this
    price; one whose lines disagree is refused (logged_price_problem)."""
    r = get(conn, proposal_id)
    if not r:
        raise LookupError("proposal not found")
    why = logged_price_problem(conn, r, price)
    if why:
        raise ValueError(why)
    price = round(float(str(price).replace("$", "").replace(",", "")), 2)
    if r.get("layout") != "ssc":
        return update(conn, proposal_id, {"price": price}, actor=actor)
    total = line_total(conn, proposal_id)
    if total is not None and abs(total - price) < 0.005:
        return r
    return update(conn, proposal_id, {}, [{"description": "Base bid", "lead": "Base bid", "qty": 1, "unit": "LS",
                                           "unit_price": price, "amount": price}], actor=actor)


def amounts(conn, r: dict) -> dict:
    """{total, deposit, deposit_pct}: the figures the document, its PDF, the email and every list show."""
    if r.get("layout") == "ssc":
        import proposal_engine
        total = line_total(conn, r["id"]) or 0.0
        pct = float(proposal_engine.house_for(conn, r)["deposit_pct"])
        return {"total": total, "deposit_pct": pct, "deposit": round(total * pct / 100, 2)}
    return {"total": round(float(r.get("price") or 0), 2), "deposit_pct": r.get("deposit_pct"),
            "deposit": r.get("deposit_amount")}


def new_version(conn, proposal_id: int, *, actor=None, supersede: bool = True) -> dict:
    """Clone into a fresh draft (vN+1) and mark the source superseded. Allowed from
    sent / viewed / declined (a signed proposal is final; a draft is simply edited).
    supersede=False (an approved revision, 2026-10-22): the sent copy stays live for the client until the new version
    is sent (supersede_predecessor); the draft remembers it in revision_of."""
    src = get(conn, proposal_id)
    if not src:
        raise LookupError("proposal not found")
    if src["status"] not in REVISABLE:
        raise ValueError(f"cannot version a {src['status']} proposal")
    open_draft = _one(conn.execute("SELECT id FROM proposal WHERE revision_of = ? AND status = 'draft'", (proposal_id,)))
    if open_draft:
        raise ValueError("a new version of this proposal is already being edited")
    last = _one(conn.execute("SELECT MAX(version) AS v FROM proposal WHERE estimate_id = ?", (src["estimate_id"],)))
    version = int(last["v"]) + 1
    number = f"{src['estimate_code']}-P{version}"
    now = _now()
    conn.execute(
        "INSERT INTO proposal (estimate_id, version, number, status, client_org_id, contact_id, building_id, "
        "title, scope_summary, price, deposit_pct, deposit_amount, schedule_text, exclusions_text, terms_text, "
        "valid_until, recipient_email, recipient_name, created_by, created_at, updated_at) "
        "VALUES (?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (src["estimate_id"], version, number, src["client_org_id"], src.get("contact_id"), src.get("building_id"),
         src.get("title"), src.get("scope_summary"), src.get("price"), src.get("deposit_pct"),
         src.get("deposit_amount"), src.get("schedule_text"), src.get("exclusions_text"), src.get("terms_text"),
         (_today() + timedelta(days=LINK_DAYS_DEFAULT)).isoformat(), src.get("recipient_email"),
         src.get("recipient_name"), actor, now, now))
    pid = _one(conn.execute("SELECT id FROM proposal WHERE number = ?", (number,)))["id"]
    for li in lines(conn, proposal_id):
        conn.execute("INSERT INTO proposal_line (proposal_id, sort, description, lead, qty, unit, unit_price, amount) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (pid, li["sort"], li["description"], li.get("lead"), li["qty"], li["unit"], li["unit_price"], li["amount"]))
    copied = [c for c, _ in STUDIO_COLUMNS if c not in ("pdf_hash", "revision_of")]
    conn.execute("UPDATE proposal SET " + ", ".join(f"{c} = ?" for c in copied) + ", revision_of = ? WHERE id = ?",
                 [src.get(c) for c in copied] + [proposal_id, pid])
    sync_price(conn, pid)
    if supersede:
        conn.execute("UPDATE proposal SET status = 'superseded', superseded_by = ?, token_hash = NULL, "
                     "updated_at = ? WHERE id = ?", (pid, now, proposal_id))
        _event(conn, proposal_id, "superseded", actor=actor, detail=f"by {number}")
    else:
        _event(conn, proposal_id, "revision_started", actor=actor, detail=f"{number} drafted; this copy stays with the client until it is sent")
    _event(conn, pid, "created", actor=actor, detail=f"version {version} of {src['number']}")
    conn.commit()
    r = get(conn, pid)
    _activity(conn, r, f"Proposal {number} drafted (replaces {src['number']})", actor=actor)
    return r


def predecessor_problem(conn, r: dict) -> Optional[str]:
    """Why a revision draft can't go out: the client answered the version it replaces in the meantime."""
    if not r.get("revision_of"):
        return None
    prev = _one(conn.execute("SELECT status FROM proposal WHERE id = ?", (r["revision_of"],)))
    if prev and prev["status"] == "signed":
        return "The client signed the version this one revises, so this revision can't be sent. Delete the draft or talk to an admin."
    return None


def supersede_predecessor(conn, r: dict, *, actor=None) -> None:
    """A revision was just sent: the version it replaces is superseded now (its record, PDF and history stay)."""
    if not r.get("revision_of"):
        return
    cur = conn.execute("UPDATE proposal SET status = 'superseded', superseded_by = ?, token_hash = NULL, updated_at = ? "
                       "WHERE id = ? AND status IN ('sent','viewed','declined')", (r["id"], _now(), r["revision_of"]))
    if (getattr(cur, "rowcount", 0) or 0) > 0:
        _event(conn, r["revision_of"], "superseded", actor=actor, detail=f"by {_label(r)}")
    conn.commit()


def delete_draft(conn, proposal_id: int) -> None:
    r = get(conn, proposal_id)
    if not r:
        raise LookupError("proposal not found")
    if r["status"] != "draft":
        raise ValueError("only a draft can be deleted")
    conn.execute("DELETE FROM proposal_line WHERE proposal_id = ?", (proposal_id,))
    conn.execute("DELETE FROM proposal_event WHERE proposal_id = ?", (proposal_id,))
    conn.execute("DELETE FROM proposal WHERE id = ?", (proposal_id,))
    conn.commit()


# ============================ the document ============================

_DOC_CSS = """
  *{box-sizing:border-box} body{margin:0;background:#FAF7F1;color:#14161C;font:14px/1.55 Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
  .sheet{max-width:820px;margin:0 auto;background:#fff;border:1px solid #E8E4DD;border-radius:16px;overflow:hidden}
  .bar{height:8px;background:#B11E2E}
  .pad{padding:32px 44px}
  .head{display:flex;justify-content:space-between;gap:24px;align-items:flex-start;border-bottom:1px solid #E8E4DD;padding-bottom:22px}
  .brand{font-size:15px;font-weight:800;letter-spacing:.6px;text-transform:uppercase}
  .brand small{display:block;font-size:10px;font-weight:600;letter-spacing:1.2px;color:#76777E;margin-top:3px}
  .meta{text-align:right;font-size:12px;color:#76777E}
  .meta strong{display:block;font-size:22px;letter-spacing:1px;color:#14161C;font-weight:800}
  h1{font-size:22px;line-height:1.25;margin:26px 0 6px}
  h2{font-size:11px;letter-spacing:1.2px;text-transform:uppercase;color:#76777E;margin:26px 0 8px}
  .two{display:grid;grid-template-columns:1fr 1fr;gap:24px}
  .fact{font-size:13px}.fact b{display:block;font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#76777E;margin-bottom:2px}
  p{margin:0 0 10px;white-space:pre-wrap}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#76777E;text-align:left;padding:8px 6px;border-bottom:1px solid #E8E4DD}
  td{padding:9px 6px;border-bottom:1px solid #F0EDE6;vertical-align:top}
  .num{text-align:right;white-space:nowrap}
  .total td{font-weight:800;font-size:15px;border-bottom:0;border-top:2px solid #14161C}
  .price{display:flex;justify-content:space-between;align-items:baseline;background:#FAF7F1;border-radius:12px;padding:16px 20px;margin-top:18px}
  .price .v{font-size:26px;font-weight:800}
  .price .d{font-size:12px;color:#76777E}
  .sig{margin-top:30px;border:1px solid #E8E4DD;border-radius:12px;padding:18px 20px;page-break-inside:avoid}
  .sig .line{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-top:22px}
  .sig .line div{border-top:1px solid #14161C;padding-top:6px;font-size:11px;color:#76777E}
  .stamp{background:#e7f6f0;color:#0c7a5f;border-radius:10px;padding:12px 16px;font-weight:700}
  .stamp.bad{background:#fceeec;color:#A8382B}
  .foot{margin-top:28px;font-size:11px;color:#76777E;border-top:1px solid #E8E4DD;padding-top:14px}
  .keep{page-break-inside:avoid}
  @media print{body{background:#fff}.sheet{border:0;border-radius:0;max-width:none}.pad{padding:18px 8px}}
"""


_SIG_CSS = (".sig{margin-top:30px;border:1px solid #E8E4DD;border-radius:12px;padding:18px 20px;page-break-inside:avoid}"
            ".sig .line{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-top:22px}"
            ".sig .line div{border-top:1px solid #14161C;padding-top:6px;font-size:11px;color:#76777E}"
            ".stamp{background:#e7f6f0;color:#0c7a5f;border-radius:10px;padding:12px 16px;font-weight:700}"
            ".stamp.bad{background:#fceeec;color:#A8382B}")


def _company_facts(conn) -> dict:
    """Optional letterhead details from app_settings (company_address, company_phone,
    company_email, company_license). Blank when unset — the document still renders."""
    out = {}
    for key in ("company_address", "company_phone", "company_email", "company_license"):
        try:
            r = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            out[key] = (r["value"] or "").strip() if r else ""
        except Exception:
            out[key] = ""
    return out


STUDIO_COLUMNS = (("intro_text", "TEXT"), ("reference_text", "TEXT"), ("prepared_for", "TEXT"), ("project_text", "TEXT"),
                  ("extra_sections", "TEXT"), ("hidden_sections", "TEXT"), ("email_message", "TEXT"),
                  ("sources", "TEXT"), ("source_mail_id", "INTEGER"), ("studio", "INTEGER"), ("submitted_by", "TEXT"),
                  # 2026-10-21: the locked Superstars layout (proposal_engine / proposal_layout)
                  ("content", "TEXT"), ("layout", "TEXT"), ("display_no", "INTEGER"),
                  # 2026-10-22: the PDF on disk is the rendered document with this fingerprint; the version a revision replaces
                  ("pdf_hash", "TEXT"), ("revision_of", "INTEGER"))
HIDEABLE = ("intro", "scope", "schedule", "exclusions", "terms")


def _json_list(v) -> list:
    if isinstance(v, list):
        return v
    try:
        x = json.loads(v or "[]")
    except (TypeError, ValueError):
        return []
    return x if isinstance(x, list) else []


_EDIT_CSS = ("[data-f],[data-lf]{display:inline-block;min-width:2ch;border-radius:4px;outline:none;cursor:text;"
             "transition:background .3s}[data-f]:hover,[data-lf]:hover{box-shadow:0 0 0 1px rgba(67,100,220,.45)}"
             "[data-f]:focus,[data-lf]:focus{background:rgba(67,100,220,.07);box-shadow:0 0 0 1px rgba(67,100,220,.6)}"
             "div[data-f]{display:block}[data-f]:empty::before,[data-lf]:empty::before{content:'…';color:#aab}"
             ".ssc-flash{background:#fff3c4!important}")


def default_submitted_by(facts: dict) -> str:
    """The SUBMITTED BY block before anyone edits it: the company and its letterhead details (Settings > Company brand)."""
    return "\n".join(x for x in (COMPANY_NAME, facts.get("company_address"),
                                  " | ".join(y for y in (facts.get("company_phone"), facts.get("company_email")) if y)) if x)


def render_document(conn, r: dict, *, mode: str = "pdf", lis: Optional[list] = None, known: Optional[set] = None) -> str:
    """Self-contained HTML of the proposal. mode: 'pdf' (signature lines, or the signed stamp) |
    'page' (the public page injects its accept/decline UI below) | 'edit' (Proposal Studio: the same
    document with every text marked data-f / data-lf so the person edits it in place). `lis` draws lines
    that are not saved yet. When the company uploaded its own proposal template (proposal_template.py),
    the proposal is drawn in that layout. Proposals made since 2026-10-21 (layout 'ssc') are drawn in the locked
    Superstars layout by proposal_engine."""
    if r.get("layout") == "ssc":
        import proposal_engine
        return proposal_engine.render(conn, r, mode, lis, known)
    e = _html.escape
    lis = lines(conn, r["id"]) if lis is None else lis
    facts = _company_facts(conn)
    edit = mode == "edit"
    hidden = set(_json_list(r.get("hidden_sections")))
    extra = [s for s in _json_list(r.get("extra_sections")) if isinstance(s, dict)]

    def txt(v) -> str:
        return e(str(v or "")).replace("\n", "<br>")

    def ed(key, html, block=False) -> str:
        if not edit:
            return html
        tag = "div" if block else "span"
        return f'<{tag} data-f="{key}" contenteditable="true">{html}</{tag}>'

    addr = r.get("building_address_line") or r.get("building_address") or ""
    city = " ".join(x for x in (r.get("building_city"), r.get("building_state"), r.get("building_zip")) if x)
    contact = r.get("recipient_name") or r.get("contact_name") or ""
    prepared = r.get("prepared_for") or ", ".join(x for x in (contact, r.get("client_name")) if x)
    project = r.get("project_text") or ", ".join(x for x in (r.get("building_name"), addr, city) if x)
    submitted = r.get("submitted_by") or default_submitted_by(facts)

    def qty_of(li):
        return "" if li.get("qty") is None else ("%g" % float(li["qty"]))

    def li_cells(i, li, numcls):
        up = _money(li["unit_price"]) if li.get("unit_price") is not None else ""
        amt = _money(li["amount"]) if li.get("amount") is not None else ""
        if edit:
            return (f"<td><span data-lf='description' contenteditable='true'>{e(li.get('description') or '')}</span></td>"
                    f"<td{numcls}><span data-lf='qty' contenteditable='true'>{qty_of(li)}</span> "
                    f"<span data-lf='unit' contenteditable='true'>{e(li.get('unit') or '')}</span></td>"
                    f"<td{numcls}><span data-lf='unit_price' contenteditable='true'>{up}</span></td><td{numcls}>{amt}</td>")
        return (f"<td>{e(li.get('description') or '')}</td><td{numcls}>{qty_of(li)}{(' ' + e(li['unit'])) if li.get('unit') else ''}</td>"
                f"<td{numcls}>{up}</td><td{numcls}>{amt}</td>")

    intro = "" if "intro" in hidden or not (r.get("intro_text") or edit) else ed("intro_text", txt(r.get("intro_text")), True)
    scope = "" if "scope" in hidden else ed("scope_summary", txt(r.get("scope_summary")), True)
    sched = "" if "schedule" in hidden else ed("schedule_text", txt(r.get("schedule_text")), True)
    excl = "" if "exclusions" in hidden else ed("exclusions_text", txt(r.get("exclusions_text")), True)
    terms = "" if "terms" in hidden else ed("terms_text", txt(r.get("terms_text")), True)
    extras = [(ed(f"x{i}h", txt(s.get("heading"))), ed(f"x{i}b", txt(s.get("body")), True)) for i, s in enumerate(extra)]

    if r.get("status") == "signed":
        sig = (f"<div class='sig keep'><div class='stamp'>Accepted by {e(r.get('signed_name') or '')}"
               f"{(' · ' + e(r['signed_title'])) if r.get('signed_title') else ''} on {e(_fmt_date(r.get('signed_at')))}</div>"
               f"<p style='margin:10px 0 0;font-size:12px;color:#76777E'>Electronic acceptance recorded at "
               f"{e(r.get('signed_at') or '')} from {e(r.get('signed_ip') or 'an unrecorded address')}. "
               f"Proposal {e(_label(r))}.</p></div>")
    elif r.get("status") == "declined":
        why = f"<p style='margin:10px 0 0'>{e(r['decline_reason'])}</p>" if r.get("decline_reason") else ""
        sig = (f"<div class='sig keep'><div class='stamp bad'>Declined on {e(_fmt_date(r.get('declined_at')))}</div>"
               f"{why}</div>")
    elif mode in ("pdf", "edit"):
        sig = ("<div class='sig keep'><b>Acceptance</b><p style='margin:6px 0 0;font-size:13px'>By signing below, "
               f"{e(r.get('client_name') or 'the client')} accepts this proposal and authorizes {COMPANY_NAME} "
               "to proceed under the terms above.</p><div class='line'><div>Name and signature</div><div>Date</div></div></div>")
    else:
        sig = ""
    letter = " · ".join(x for x in (facts["company_address"], facts["company_phone"], facts["company_email"]) if x)
    lic = f" · License {e(facts['company_license'])}" if facts["company_license"] else ""
    try:
        import proposal_template
        tpl = proposal_template.active_html(conn)
    except Exception as x:
        log.info("proposal template unavailable: %s", type(x).__name__)
        tpl = None
    if tpl:
        sv = "".join(f"<tr data-li='{i}'>" + li_cells(i, li, "") + "</tr>" for i, li in enumerate(lis))
        dep = (f"Deposit to start: {_money(r['deposit_amount'])} ({'%g' % float(r.get('deposit_pct') or 0)}%), "
               "balance on progress invoices.") if r.get("deposit_amount") else ""
        extra_html = "".join(f"<h3 style='margin:14px 0 4px'>{h}</h3><div>{b}</div>" for h, b in extras)
        has = lambda tok: re.search(r"\{\{\s*" + tok + r"\s*\}\}", tpl) is not None   # noqa: E731
        scope_part = scope if has("intro") or not intro else f"<div style='margin-bottom:10px'>{intro}</div>{scope}"
        terms_part = terms if has("extra_sections") or not extra_html else terms + extra_html
        tpl = proposal_template.with_submitted_by(tpl)
        out = proposal_template.fill(tpl, {
            "submitted_by": ed("submitted_by", txt(submitted), True),
            "company_name": e(COMPANY_NAME), "company_address": e(facts["company_address"]), "company_phone": e(facts["company_phone"]),
            "company_email": e(facts["company_email"]), "company_license": e(facts["company_license"]),
            "logo": proposal_template.logo_tag(), "number": e(_label(r)),
            "date": e(_fmt_date(r.get("sent_at") or r.get("updated_at"))), "valid_until": e(_fmt_date(r.get("valid_until"))),
            "title": ed("title", e(r.get("title") or "Proposal")), "client_name": ed("prepared_for", e(prepared)),
            "contact_name": "", "recipient_email": e(r.get("recipient_email") or ""), "building": ed("project_text", e(project)),
            "reference": ed("reference_text", e(r.get("reference_text") or "")), "intro": intro, "extra_sections": extra_html,
            "scope": scope_part, "line_items_rows": sv, "total": e(_money(r.get("price"))), "deposit": e(dep),
            "schedule": sched, "exclusions": excl, "terms": terms_part,
            "signature": f"<style>{_SIG_CSS}</style>{sig}" if sig else ""})
        return out.replace("</head>", f"<style>{_EDIT_CSS}</style></head>", 1) if edit else out
    rows = ""
    if lis:
        rows = "".join(f"<tr data-li='{i}'>" + li_cells(i, li, " class='num'") + "</tr>" for i, li in enumerate(lis))
        rows = (f"<h2>Schedule of values</h2><table><thead><tr><th>Item</th><th class='num'>Qty</th>"
                f"<th class='num'>Unit price</th><th class='num'>Amount</th></tr></thead><tbody>{rows}"
                f"<tr class='total'><td colspan='3'>Total</td><td class='num'>{_money(r.get('price'))}</td></tr>"
                f"</tbody></table>")
    deposit = ""
    if r.get("deposit_amount"):
        deposit = (f"<div class='d'>Deposit to start: <b>{_money(r['deposit_amount'])}</b> "
                   f"({'%g' % float(r.get('deposit_pct') or 0)}%) · balance on progress invoices</div>")
    ref = r.get("reference_text")
    block = lambda head, body: f"<div class='keep'><h2>{head}</h2><p>{body}</p></div>" if body else ""   # noqa: E731
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Proposal {e(_label(r))} — {COMPANY_NAME}</title><style>{_DOC_CSS}{company_brand.BRAND_CSS}{_EDIT_CSS if edit else ''}</style></head><body>
<div class="sheet"><div class="bar"></div><div class="pad">
  <div class="head">{company_brand.brand_block(conn, "Exterior restoration · New York City")}
    <div class="meta"><strong>PROPOSAL</strong>{e(_label(r))}<br>Issued {e(_fmt_date(r.get('sent_at') or r.get('updated_at')))}<br>Valid until {e(_fmt_date(r.get('valid_until')))}</div></div>
  <h1>{ed("title", e(r.get('title') or 'Proposal'))}</h1>
  <div class="two" style="margin-top:14px">
    <div class="fact"><b>Prepared for</b>{ed("prepared_for", e(prepared))}{('<br>' + e(r['recipient_email'])) if r.get('recipient_email') and not edit else ''}</div>
    <div class="fact"><b>Submitted by</b>{ed("submitted_by", txt(submitted), True)}</div>
    <div class="fact"><b>Building</b>{ed("project_text", e(project or '—'))}{('<br><b style="margin-top:6px">Reference</b>' + ed("reference_text", e(ref or ''))) if (ref or edit) else ''}</div>
  </div>
  {('<div style="margin-top:18px">' + intro + '</div>') if intro else ''}
  {('<h2>Scope of work</h2><p>' + scope + '</p>') if scope else ''}
  {rows}
  <div class="price keep"><div><b>Proposal price</b>{deposit}</div><div class="v">{_money(r.get('price'))}</div></div>
  {block('Schedule', sched)}
  {block('Exclusions', excl)}
  {block('Terms', terms)}
  {''.join(block(h, b) for h, b in extras)}
  {sig}
  <div class="foot">{COMPANY_NAME}{(' · ' + e(letter)) if letter else ''}{lic} · Proposal {e(_label(r))} v{r['version']}</div>
</div></div></body></html>"""


def _pdf_dir() -> Path:
    return ssc_paths.under_root("data_room", "proposals")


def _fingerprint(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def render_pdf(conn, proposal_id: int, *, actor=None, html: Optional[str] = None) -> dict:
    """HTML → PDF with the app's engine; stores the file under data_room/proposals and a
    row in estimate_document (category 'proposal'). Returns {"ok", "error"?}. Never raises.
    The PDF is the very document the editor and the client page draw (render_document mode 'pdf'); its fingerprint
    is kept so pdf_file knows when the document changed (2026-10-22)."""
    r = get(conn, proposal_id)
    if not r:
        return {"ok": False, "error": "proposal not found"}
    try:
        import pdf_export
        d = _pdf_dir()
        d.mkdir(parents=True, exist_ok=True)
        html_path = d / f"{r['number']}.html"
        pdf_path = d / f"{r['number']}.pdf"
        html = render_document(conn, r, mode="pdf") if html is None else html
        html_path.write_text(html, encoding="utf-8")
        res = pdf_export.render_html_to_pdf(html_path, pdf_path, timeout_sec=30)
        try:
            html_path.unlink(missing_ok=True)
        except Exception:
            pass
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "render failed"}
        rel = ssc_paths.store_rel(pdf_path)
        now = _now()
        doc_id = r.get("pdf_doc_id")
        size = pdf_path.stat().st_size
        if doc_id and _one(conn.execute("SELECT id FROM estimate_document WHERE id = ?", (doc_id,))):
            conn.execute("UPDATE estimate_document SET file_path = ?, file_size = ?, uploaded_at = ?, title = ? WHERE id = ?",
                         (rel, size, now, f"Proposal {r['number']}", doc_id))
        else:
            conn.execute(
                "INSERT INTO estimate_document (estimate_id, category, title, doc_type, file_path, file_name, "
                "file_size, mime, uploaded_by, uploaded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["estimate_id"], "proposal", f"Proposal {r['number']}", "PDF", rel, f"{r['number']}.pdf",
                 size, "application/pdf", actor, now))
            doc_id = _one(conn.execute("SELECT MAX(id) AS m FROM estimate_document"))["m"]
        conn.execute("UPDATE proposal SET pdf_path = ?, pdf_doc_id = ?, pdf_rendered_at = ?, pdf_hash = ? WHERE id = ?",
                     (rel, doc_id, now, _fingerprint(html), proposal_id))
        conn.commit()
        return {"ok": True, "size": size, "engine": res.get("engine")}
    except Exception as e:   # missing browser, timeout, disk — the proposal still sends
        log.warning("#307 pdf not rendered for proposal %s: %s", proposal_id, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def pdf_file(conn, proposal_id: int) -> Optional[Path]:
    """The PDF of the proposal as it is NOW. It used to be re-made only when updated_at was later than the last render,
    to the second, so an edit in the same second as a render (or the Download click racing the autosave) served the
    old amount. Now the document is drawn and compared with the fingerprint of the PDF on disk."""
    r = get(conn, proposal_id)
    if not r:
        return None
    html = render_document(conn, r, mode="pdf")
    p = ssc_paths.resolve_data_path(r["pdf_path"]) if r.get("pdf_path") else None
    if p is None or not p.exists() or r.get("pdf_hash") != _fingerprint(html):
        if not render_pdf(conn, proposal_id, html=html).get("ok"):
            return None
        p = ssc_paths.resolve_data_path(get(conn, proposal_id)["pdf_path"])
    return p if p.exists() else None


# ============================ send ============================

EMAIL_SUBJECT = "Proposal {number} from " + COMPANY_NAME
EMAIL_FROM_NAME = COMPANY_NAME

_EMAIL_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{subject}</title></head>
<body style="margin:0;padding:0;background:#FAF7F1;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#FAF7F1;"><tr><td align="center" style="padding:32px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:#FFFFFF;border:1px solid #E8E4DD;border-radius:16px;overflow:hidden;">
<tr><td style="height:6px;background:#B11E2E;font-size:0;line-height:0;">&nbsp;</td></tr>
<tr><td style="padding:28px 36px 0 36px;font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<div style="font-size:15px;font-weight:700;letter-spacing:0.6px;text-transform:uppercase;color:#14161C;">Superstars Contracting</div>
<div style="font-size:10px;font-weight:600;letter-spacing:1.2px;text-transform:uppercase;color:#76777E;margin-top:3px;">Proposal {number}</div></td></tr>
<tr><td style="padding:26px 36px 0 36px;font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<h1 style="margin:0 0 10px 0;font-size:22px;line-height:1.25;font-weight:700;color:#14161C;">{title}</h1>
<p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:#384152;">{greeting}</p>
<p style="margin:0;font-size:15px;line-height:1.55;color:#384152;white-space:pre-wrap;">{message}</p>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-top:18px;font-family:inherit;font-size:14px;color:#384152;">
<tr><td style="padding:4px 18px 4px 0;color:#76777E;">Building</td><td>{building}</td></tr>
<tr><td style="padding:4px 18px 4px 0;color:#76777E;">Proposal price</td><td><strong>{price}</strong></td></tr>
<tr><td style="padding:4px 18px 4px 0;color:#76777E;">Valid until</td><td>{valid_until}</td></tr></table></td></tr>
<tr><td align="left" style="padding:24px 36px 0 36px;"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr><td align="center" style="background:#24418C;border-radius:9px;">
<a href="{url}" style="display:inline-block;padding:13px 26px;font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:14px;font-weight:700;letter-spacing:0.3px;color:#FFFFFF;text-decoration:none;border-radius:9px;">Review and accept the proposal</a></td></tr></table></td></tr>
<tr><td style="padding:18px 36px 30px 36px;font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<p style="margin:0;font-size:13px;line-height:1.5;color:#76777E;">The link opens the proposal, lets you download the PDF, and records your acceptance with a typed signature. It is personal to this proposal and stops working after {valid_until}. If the button does not work, copy this address into your browser:<br><span style="word-break:break-all;color:#14161C;">{url}</span></p></td></tr>
</table></td></tr></table></body></html>"""


def render_email(r: dict, url: str, message: str) -> tuple:
    greeting = f"Hello {r['recipient_name'].split(' ')[0]}," if r.get("recipient_name") else "Hello,"
    msg = (message or "").strip() or (
        f"Please find our proposal for {r.get('building_address_line') or r.get('building_address') or 'your building'} "
        f"below. Open the link to review the full scope, download the PDF, and accept online when you are ready.")
    subject = EMAIL_SUBJECT.format(number=_label(r))
    text = (f"{greeting}\n\n{msg}\n\nProposal {_label(r)} — {r.get('title') or ''}\n"
            f"Price: {_money(r.get('price'))}\nValid until: {_fmt_date(r.get('valid_until'))}\n\n"
            f"Review and accept it here: {url}\n\n— {COMPANY_NAME}")
    html = _EMAIL_HTML.format(
        subject=_html.escape(subject), number=_html.escape(_label(r)),
        title=_html.escape(r.get("title") or "Your proposal"), greeting=_html.escape(greeting),
        message=_html.escape(msg),
        building=_html.escape(r.get("building_address_line") or r.get("building_address") or "—"),
        price=_html.escape(_money(r.get("price"))), valid_until=_html.escape(_fmt_date(r.get("valid_until"))),
        url=_html.escape(url, quote=True))
    return subject, text, html


def _deliver(to_email: str, subject: str, text: str, html: str) -> tuple:
    """(sent: bool, error). Fake dir (tests) → SendGrid → not configured."""
    fake_dir = (runtime_seams.seam("SSC_MAIL_FAKE_DIR") or runtime_seams.seam("SSC_RESET_MAIL_FAKE_DIR")).strip()
    if fake_dir:
        out = Path(fake_dir)
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        (out / f"proposal-{stamp}.eml").write_text(f"To: {to_email}\nSubject: {subject}\n\n{text}", encoding="utf-8")
        (out / f"proposal-{stamp}.html").write_text(html, encoding="utf-8")
        return True, None
    if (os.environ.get("SENDGRID_API_KEY") or "").strip():
        import notifications
        sent, err = notifications._send_live(to_email, subject, text, html,
                                             from_name=EMAIL_FROM_NAME, track_clicks=False)
        return bool(sent), err
    return False, "email is not configured (SENDGRID_API_KEY)"


def send(conn, proposal_id: int, *, to_email=None, to_name=None, message=None, actor=None) -> dict:
    r = get(conn, proposal_id)
    if not r:
        raise LookupError("proposal not found")
    if r["status"] not in ("draft", "sent", "viewed"):
        raise ValueError(f"a {r['status']} proposal cannot be sent")
    to_email = (to_email or r.get("recipient_email") or r.get("contact_email") or "").strip().lower()
    if "@" not in to_email or "." not in to_email.split("@")[-1]:
        raise ValueError("a valid recipient email is required")
    to_name = (to_name or r.get("recipient_name") or r.get("contact_name") or "").strip() or None
    sync_price(conn, proposal_id, layout=r.get("layout"))   # rows written before 2026-10-22 may have drifted from their lines
    conn.commit()
    r = get(conn, proposal_id)
    if r.get("price") in (None, "") or float(r["price"]) <= 0:
        raise ValueError("set the proposal price before sending")
    import proposal_engine
    why = (proposal_engine.blocked(conn, r) or predecessor_problem(conn, r)) if r["status"] == "draft" else None
    if why:
        raise ValueError(why)
    valid_until = r.get("valid_until") or (_today() + timedelta(days=LINK_DAYS_DEFAULT)).isoformat()
    if valid_until < _today().isoformat():
        raise ValueError("valid_until is in the past — extend it before sending")
    token = secrets.token_urlsafe(32)
    expires = f"{valid_until}T23:59:59"
    now = _now()
    conn.execute(
        "UPDATE proposal SET recipient_email = ?, recipient_name = ?, valid_until = ?, token_hash = ?, "
        "token_expires_at = ?, sent_at = ?, sent_by = ?, status = CASE WHEN status = 'draft' THEN 'sent' ELSE status END, "
        "updated_at = ? WHERE id = ?",
        (to_email, to_name, valid_until, _hash(token), expires, now, actor, now, proposal_id))
    conn.commit()
    # the fixed wording as sent, so the sent copy never changes (a resend keeps what it first went out with)
    proposal_engine.freeze_house(conn, proposal_id, keep=r["status"] != "draft")
    pdf = render_pdf(conn, proposal_id, actor=actor)     # best effort; the page has the PDF link
    r = get(conn, proposal_id)
    url = f"{_base_url()}/p/{token}"
    subject, text, html = render_email(r, url, message)
    sent, err = _deliver(to_email, subject, text, html)
    if not sent:
        # roll the send back so the row does not claim a delivery that never happened
        conn.execute("UPDATE proposal SET token_hash = NULL, token_expires_at = NULL, sent_at = NULL, "
                     "status = CASE WHEN status = 'sent' AND viewed_at IS NULL THEN 'draft' ELSE status END "
                     "WHERE id = ?", (proposal_id,))
        _event(conn, proposal_id, "send_failed", actor=actor, detail=err)
        conn.commit()
        raise RuntimeError(f"email not sent: {err}")
    _event(conn, proposal_id, "sent", actor=actor,
           detail=f"to {to_email}" + ("" if pdf.get("ok") else " — PDF not rendered (engine unavailable); the page is the reference copy"))
    if not pdf.get("ok"):
        log.warning("#307 proposal %s sent without a PDF: %s", r["number"], pdf.get("error"))
    conn.commit()
    r = get(conn, proposal_id)
    supersede_predecessor(conn, r, actor=actor)
    _activity(conn, r, f"Proposal {_label(r)} sent to {to_name or to_email}", actor=actor)
    return r


# ============================ the client's actions ============================

def _link_state(r: dict) -> str:
    """'open' | 'expired' | 'signed' | 'declined' | 'superseded' | 'gone'."""
    if not r or not r.get("token_hash"):
        return "gone"
    if r["status"] in ("signed", "declined", "superseded"):
        return r["status"]
    if (r.get("token_expires_at") or "") < _now():
        return "expired"
    return "open"


def record_view(conn, r: dict, ip=None) -> dict:
    now = _now()
    first = r["status"] == "sent"
    conn.execute("UPDATE proposal SET status = CASE WHEN status = 'sent' THEN 'viewed' ELSE status END, "
                 "viewed_at = COALESCE(viewed_at, ?), view_count = view_count + 1 WHERE id = ?", (now, r["id"]))
    if first:
        _event(conn, r["id"], "viewed", ip=ip)
    conn.commit()
    r = get(conn, r["id"])
    if first:
        _activity(conn, r, f"Proposal {_label(r)} opened by the client")
    return r


def accept(conn, r: dict, *, name: str, title: Optional[str] = None, ip=None) -> dict:
    name = (name or "").strip()
    if len(name) < 2:
        raise ValueError("please type your full name to accept")
    if len(name) > 120:
        raise ValueError("name is too long")
    if _link_state(r) != "open":
        raise ValueError("this proposal can no longer be accepted")
    now = _now()
    conn.execute("UPDATE proposal SET status = 'signed', signed_at = ?, signed_name = ?, signed_title = ?, "
                 "signed_ip = ?, updated_at = ? WHERE id = ? AND status IN ('sent','viewed')",
                 (now, name, (title or "").strip()[:120] or None, ip, now, r["id"]))
    _event(conn, r["id"], "signed", ip=ip, detail=name)
    conn.commit()
    r = get(conn, r["id"])
    _on_signed(conn, r)
    return r


def _on_signed(conn, r: dict) -> None:
    """THE trigger (ROADMAP item 4). Estimate → approved at the proposal price; CRM tasks
    for the Certificate of Insurance and the deposit invoice (#309 automates the latter)."""
    est = estimates.get_estimate(conn, r["estimate_id"])
    today = _today().isoformat()
    if est and est["status"] in ("intake", "scoping", "submitted"):
        conn.execute("UPDATE estimate SET status = 'approved', final_amount = COALESCE(?, final_amount), "
                     "submitted_date = COALESCE(submitted_date, ?), decided_date = ?, status_changed_at = ?, "
                     "updated_at = ? WHERE id = ?",
                     (r.get("price"), today, today, _now(), _now(), r["estimate_id"]))
        conn.commit()
        _activity(conn, r, f"Estimate {est['code']}: {est['status']} → approved (proposal {_label(r)} signed)")
    due_coi = (_today() + timedelta(days=7)).isoformat()
    label = r.get("building_address_line") or r.get("building_address") or r.get("client_name")
    try:
        crm.create_task(conn, title=f"Collect Certificate of Insurance — {label}",
                        entity_type="organization", entity_id=r["client_org_id"],
                        detail=f"Proposal {_label(r)} was signed by {r.get('signed_name')} on {r.get('signed_at')}. "
                               f"The COI is required before work starts.",
                        due_date=due_coi, priority="high", function_tag="sales")
        if r.get("deposit_amount"):
            inv = None
            try:
                import invoices
                inv = invoices.create_from_proposal(conn, r)   # #315: the deposit invoice is drafted here
            except Exception as e:
                log.warning("#315 deposit invoice not drafted for %s: %s", r.get("number"), e)
            where = (f"Draft invoice {inv['number']} is waiting under Company Portal → Finance → Receivables; issue it there."
                     if inv else "Create the deposit invoice under Company Portal → Finance → Receivables.")
            crm.create_task(conn, title=f"Issue deposit invoice {_money(r['deposit_amount'])} — {label}",
                            entity_type="organization", entity_id=r["client_org_id"],
                            detail=f"Deposit of {'%g' % float(r.get('deposit_pct') or 0)}% on proposal {_label(r)} "
                                   f"({_money(r.get('price'))}). {where}",
                            due_date=(_today() + timedelta(days=2)).isoformat(), priority="high", function_tag="sales")
    except Exception as e:
        log.warning("#307 follow-up tasks not created: %s", e)
    _activity(conn, r, f"Proposal {_label(r)} SIGNED by {r.get('signed_name')} — {_money(r.get('price'))}")
    try:
        import notifications
        notifications.notify_vps(conn, kind="proposal_signed",
                                 subject=f"Proposal {_label(r)} signed — {label} ({_money(r.get('price'))})",
                                 estimate_code=r.get("estimate_code"), path="/company#proposals/" + str(r["id"]))
    except Exception as e:
        log.info("#307 VP notification skipped: %s", e)


def decline(conn, r: dict, *, reason: Optional[str] = None, ip=None) -> dict:
    if _link_state(r) != "open":
        raise ValueError("this proposal can no longer be declined")
    now = _now()
    reason = (reason or "").strip()[:1000] or None
    conn.execute("UPDATE proposal SET status = 'declined', declined_at = ?, decline_reason = ?, updated_at = ? "
                 "WHERE id = ? AND status IN ('sent','viewed')", (now, reason, now, r["id"]))
    _event(conn, r["id"], "declined", ip=ip, detail=reason)
    conn.commit()
    r = get(conn, r["id"])
    _activity(conn, r, f"Proposal {_label(r)} declined" + (f": {reason}" if reason else ""))
    try:
        crm.create_task(conn, title=f"Follow up on declined proposal {_label(r)}",
                        entity_type="organization", entity_id=r["client_org_id"],
                        detail=reason or "No reason given.", due_date=(_today() + timedelta(days=3)).isoformat(),
                        priority="normal", function_tag="sales")
    except Exception as e:
        log.warning("#307 follow-up task not created: %s", e)
    return r


def mark_signed_offline(conn, proposal_id: int, *, name: str, signed_on=None, actor=None) -> dict:
    """The client signed on paper / by email: record it and fire the same trigger."""
    r = get(conn, proposal_id)
    if not r:
        raise LookupError("proposal not found")
    if r["status"] not in ("draft", "sent", "viewed"):
        raise ValueError(f"a {r['status']} proposal cannot be marked signed")
    name = (name or "").strip()
    if len(name) < 2:
        raise ValueError("the signer's name is required")
    on = _valid_date(signed_on, "signed_on") or _today().isoformat()
    now = _now()
    signed_at = now if on == _today().isoformat() else f"{on}T12:00:00"
    conn.execute("UPDATE proposal SET status = 'signed', signed_at = ?, signed_name = ?, signed_title = 'signed offline', "
                 "updated_at = ? WHERE id = ?", (signed_at, name, now, proposal_id))
    _event(conn, proposal_id, "signed", actor=actor, detail=f"{name} (recorded offline)")
    conn.commit()
    r = get(conn, proposal_id)
    _on_signed(conn, r)
    return r


def mark_sent_outside(conn, proposal_id: int, *, actor=None, sent_on=None, to_email=None, to_name=None,
                      how: Optional[str] = None) -> dict:
    """The proposal went out another way (an email from someone's own mailbox, by hand): record it as sent (2026-10-22).
    No link is made for the client (there is no online accept); sent_at is the day given (noon) or now. Draft only; the
    price stays the total of its lines; a revision supersedes the version it replaces, as a send does."""
    r = get(conn, proposal_id)
    if not r:
        raise LookupError("proposal not found")
    if r["status"] != "draft":
        raise ValueError(f"This proposal is already {r['status']}.")
    why = predecessor_problem(conn, r)
    if why:
        raise ValueError(why)
    to_email = (to_email or "").strip().lower() or None
    if to_email and ("@" not in to_email or "." not in to_email.split("@")[-1]):
        raise ValueError("check the email address")
    on = _valid_date(sent_on, "sent_on") or _today().isoformat()
    if on > _today().isoformat():
        raise ValueError("the date it was sent can't be in the future")
    now = _now()
    sent_at = now if on == _today().isoformat() else f"{on}T12:00:00"
    sync_price(conn, proposal_id, layout=r.get("layout"))
    import proposal_engine
    proposal_engine.freeze_house(conn, proposal_id)
    conn.execute("UPDATE proposal SET status = 'sent', sent_at = ?, sent_by = ?, recipient_email = COALESCE(?, recipient_email), "
                 "recipient_name = COALESCE(?, recipient_name), updated_at = ? WHERE id = ? AND status = 'draft'",
                 (sent_at, actor, to_email, (to_name or "").strip()[:120] or None, now, proposal_id))
    _event(conn, proposal_id, "sent", actor=actor,
           detail="marked as sent: it went out outside the app" + (f" ({str(how).strip()[:120]})" if how and str(how).strip() else ""))
    conn.commit()
    r = get(conn, proposal_id)
    render_pdf(conn, proposal_id, actor=actor)   # best effort: the app's copy of what was sent
    supersede_predecessor(conn, r, actor=actor)
    _activity(conn, r, f"Proposal {_label(r)} marked as sent (outside the app)", actor=actor)
    return get(conn, proposal_id)


# ============================ keeping only the latest (2026-10-23) ============================
# The client asked for a change and a new version went out: the old one can be deleted so only the final proposal is
# listed. A sent proposal is never erased: it is HIDDEN (status 'deleted', the status it had kept in deleted_from), so
# every list, count, the client portal and the client's link leave it out by status, and an admin can restore it.
# Only admins delete a sent proposal (checked here); a signed one is the contract and is never deleted.

def hide(conn, proposal_id: int, *, actor=None, actor_role: Optional[str] = None) -> dict:
    if actor_role != "admin":
        raise PermissionError("Only an admin can delete a proposal that was sent.")
    r = get(conn, proposal_id)
    if not r:
        raise LookupError("proposal not found")
    if r["status"] == "signed":
        raise ValueError("A signed proposal is the contract, so it can't be deleted.")
    if r["status"] in ("draft", "deleted"):
        raise ValueError("Only a sent proposal is deleted this way." if r["status"] == "draft" else "It is already deleted.")
    now = _now()
    conn.execute("UPDATE proposal SET deleted_from = status, status = 'deleted', deleted_at = ?, deleted_by = ?, updated_at = ? "
                 "WHERE id = ? AND status NOT IN ('signed','draft','deleted')", (now, actor, now, proposal_id))
    conn.execute("UPDATE proposal_revision SET status = 'withdrawn', decided_by = ?, decided_at = ? "
                 "WHERE proposal_id = ? AND status = 'pending'", (actor, now, proposal_id))
    _event(conn, proposal_id, "deleted", actor=actor, detail=f"hidden (was {r['status']}); an admin can restore it")
    conn.commit()
    _activity(conn, r, f"Proposal {_label(r)} deleted (hidden; it can be restored)", actor=actor)
    return get(conn, proposal_id)


def restore(conn, proposal_id: int, *, actor=None, actor_role: Optional[str] = None) -> dict:
    if actor_role != "admin":
        raise PermissionError("Only an admin can restore a deleted proposal.")
    r = get(conn, proposal_id)
    if not r:
        raise LookupError("proposal not found")
    if r["status"] != "deleted":
        raise ValueError("That proposal isn't deleted.")
    back = r.get("deleted_from") or "sent"
    conn.execute("UPDATE proposal SET status = ?, deleted_from = NULL, deleted_at = NULL, deleted_by = NULL, updated_at = ? "
                 "WHERE id = ?", (back, _now(), proposal_id))
    _event(conn, proposal_id, "restored", actor=actor, detail=f"back as {back}")
    conn.commit()
    return get(conn, proposal_id)


RECORD_FIELDS = ("number", "title", "recipient", "valid_until")


def edit_record(conn, proposal_id: int, field: str, value, *, actor=None) -> dict:
    """One field edited in place (the pencils on Estimates & proposals, 2026-10-23).
      number       the number people see (1046). Every version of the job carries it (1046, 1046-2…), so it changes on
                   all of them; refused when another job already has it. Drafts and sent proposals alike.
      title        drafts only (it is printed on the document).
      recipient    "Name <email>" — who it goes / went to (the record; nothing is sent).
      valid_until  a draft's date, or a sent one's (extends the client's link; not in the past)."""
    r = get(conn, proposal_id)
    if not r:
        raise LookupError("proposal not found")
    if field not in RECORD_FIELDS:
        raise ValueError("That can't be edited here.")
    if r["status"] == "deleted":
        raise ValueError("Restore the proposal first.")
    v = str(value if value is not None else "").strip()
    now = _now()
    if field == "number":
        m = re.fullmatch(r"#?\s*(\d{1,7})(?:-\d+)?", v)
        if not m or int(m.group(1)) < 1:
            raise ValueError("Use digits, like 1046.")
        n = int(m.group(1))
        other = _one(conn.execute("SELECT p.id, p.display_no, p.version, p.number, o.name AS client_name FROM proposal p "
                                  "JOIN crm_organization o ON o.id = p.client_org_id WHERE p.display_no = ? AND p.estimate_id <> ? "
                                  "ORDER BY p.id LIMIT 1", (n, r["estimate_id"])))
        if other:
            raise ValueError(f"Proposal {n} already exists ({other['client_name']}). Pick another number.")
        old = _label(r)
        ids = [x["id"] for x in _rows(conn.execute("SELECT id FROM proposal WHERE estimate_id = ?", (r["estimate_id"],)))]
        for pid in ids:
            conn.execute("UPDATE proposal SET display_no = ?, updated_at = ? WHERE id = ?", (n, now, pid))
        conn.commit()
        _event(conn, proposal_id, "number_changed", actor=actor, detail=f"{old} → {_label(get(conn, proposal_id))}")
        conn.commit()
        return get(conn, proposal_id)
    if field == "title":
        if r["status"] != "draft":
            raise ValueError("The title is printed on the sent proposal. Make a new version to change it.")
        if not v:
            raise ValueError("The title can't be empty.")
        if r.get("layout") == "ssc":
            import proposal_engine
            return proposal_engine.save(conn, proposal_id, {"title": v[:300]}, None, actor=actor)
        return update(conn, proposal_id, {"title": v[:300]}, actor=actor)
    if field == "recipient":
        from email.utils import parseaddr
        name, addr = parseaddr(v)
        addr = (addr or "").strip().lower()
        if "@" not in addr or "." not in addr.split("@")[-1]:
            raise ValueError("Write it as Name <email@company.com>.")
        if r["status"] == "signed":
            raise ValueError("A signed proposal's record stays as it was.")
        conn.execute("UPDATE proposal SET recipient_email = ?, recipient_name = ?, updated_at = ? WHERE id = ?",
                     (addr, (name or "").strip()[:120] or None, now, proposal_id))
        _event(conn, proposal_id, "recipient_changed", actor=actor, detail=f"to {addr}")
        conn.commit()
        return get(conn, proposal_id)
    # valid_until
    d = _valid_date(v, "valid until")
    if not d:
        raise ValueError("Pick a date.")
    if r["status"] == "draft":
        return update(conn, proposal_id, {"valid_until": d}, actor=actor)
    if r["status"] not in OPEN_STATUSES:
        raise ValueError(f"A {r['status']} proposal's date stays as it was.")
    if d < _today().isoformat():
        raise ValueError("Pick today or a later date.")
    conn.execute("UPDATE proposal SET valid_until = ?, token_expires_at = CASE WHEN token_hash IS NULL THEN token_expires_at "
                 "ELSE ? END, updated_at = ? WHERE id = ?", (d, f"{d}T23:59:59", now, proposal_id))
    _event(conn, proposal_id, "valid_until_changed", actor=actor, detail=f"valid until {d}")
    conn.commit()
    return get(conn, proposal_id)


# ============================ revisions (2026-10-22) ============================
# Anyone who works on proposals can ask for a revision of a SENT proposal (a scope change, or a price review a week
# after sending). Only an ADMIN approves or rejects it — checked here, whatever the screen shows. Approval drafts the
# next version (vN+1) to edit; the sent copy is never touched and stays the client's live proposal until the new
# version is sent (supersede_predecessor). Every step is a proposal_event, so the timeline tells the whole story.

def _revision_row(conn, rid: int) -> Optional[dict]:
    return _one(conn.execute(
        "SELECT v.*, ru.full_name AS requested_by_name, du.full_name AS decided_by_name, p.number AS proposal_number, "
        "p.display_no, p.version, p.client_org_id, o.name AS client_name FROM proposal_revision v "
        "JOIN proposal p ON p.id = v.proposal_id JOIN crm_organization o ON o.id = p.client_org_id "
        "LEFT JOIN users ru ON ru.id = v.requested_by LEFT JOIN users du ON du.id = v.decided_by WHERE v.id = ?", (rid,)))


def revision_public(v: dict) -> dict:
    return {"id": v["id"], "proposal_id": v["proposal_id"], "label": _label({"display_no": v.get("display_no"),
                                                                           "version": v.get("version"), "number": v.get("proposal_number")}),
            "client_name": v.get("client_name"), "status": v["status"], "reason": v["reason"],
            "price_review": bool(v.get("price_review")), "proposed_price": v.get("proposed_price"),
            "requested_by": v.get("requested_by"), "requested_by_name": v.get("requested_by_name"), "requested_at": v["requested_at"],
            "decided_by_name": v.get("decided_by_name"), "decided_at": v.get("decided_at"), "decision_note": v.get("decision_note"),
            "new_proposal_id": v.get("new_proposal_id")}


def revisions(conn, proposal_id: int) -> list:
    """The requests about this proposal: made on it, or the one that created it (newest first)."""
    ids = [r["id"] for r in _rows(conn.execute(
        "SELECT id FROM proposal_revision WHERE proposal_id = ? OR new_proposal_id = ? ORDER BY requested_at DESC, id DESC",
        (proposal_id, proposal_id)))]
    return [revision_public(_revision_row(conn, i)) for i in ids]


def pending_revisions(conn) -> list:
    ids = [r["id"] for r in _rows(conn.execute(
        "SELECT id FROM proposal_revision WHERE status = 'pending' ORDER BY requested_at, id"))]
    return [revision_public(_revision_row(conn, i)) for i in ids]


def _notify(conn, user_ids, subject: str, path: str) -> None:
    """Internal email to staff (notification_log row first; live only with SendGrid). Never blocks the change."""
    try:
        import notifications
        for uid in {u for u in user_ids if u}:
            notifications.notify_user(conn, kind="proposal_revision", user_id=uid, subject=subject, path=path)
    except Exception as e:
        log.info("proposal revision notification skipped: %s", type(e).__name__)


def request_revision(conn, proposal_id: int, *, actor, reason: str, price_review: bool = False,
                     proposed_price=None) -> dict:
    r = get(conn, proposal_id)
    if not r:
        raise LookupError("proposal not found")
    if r["status"] not in REVISABLE:
        raise ValueError("Only a sent proposal can be revised." if r["status"] == "draft" else
                         f"A {r['status']} proposal can't be revised.")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValueError("Say what should change, so the admin can decide.")
    price = None
    if proposed_price not in (None, ""):
        try:
            price = round(float(str(proposed_price).replace("$", "").replace(",", "")), 2)
        except ValueError:
            raise ValueError("The proposed price must be a number.")
        if price <= 0:
            raise ValueError("The proposed price must be more than zero.")
    if _one(conn.execute("SELECT id FROM proposal_revision WHERE proposal_id = ? AND status = 'pending'", (proposal_id,))):
        raise ValueError("A revision of this proposal is already waiting for an admin.")
    if _one(conn.execute("SELECT id FROM proposal WHERE revision_of = ? AND status = 'draft'", (proposal_id,))):
        raise ValueError("A new version of this proposal is already being edited.")
    now = _now()
    conn.execute("INSERT INTO proposal_revision (proposal_id, requested_by, requested_at, reason, price_review, proposed_price, status) "
                 "VALUES (?,?,?,?,?,?, 'pending')", (proposal_id, actor, now, reason[:2000], 1 if (price_review or price) else 0, price))
    rid = _one(conn.execute("SELECT MAX(id) AS m FROM proposal_revision WHERE proposal_id = ?", (proposal_id,)))["m"]
    _event(conn, proposal_id, "revision_requested", actor=actor,
           detail=("Price review" if (price_review or price) else "Revision") + (f" (proposed {_money(price)})" if price else "") + f": {reason[:300]}")
    conn.commit()
    _activity(conn, r, f"Revision of proposal {_label(r)} requested", actor=actor)
    admins = [x["id"] for x in _rows(conn.execute(
        "SELECT id FROM users WHERE role = 'admin' AND status = 'active' AND is_active = 1"))]
    _notify(conn, [a for a in admins if a != actor], f"Proposal {_label(r)}: a revision is waiting for your approval",
            f"/proposal-studio#p/{proposal_id}")
    return revision_public(_revision_row(conn, rid))


def decide_revision(conn, rid: int, *, actor, actor_role: str, approve: bool, note: Optional[str] = None) -> dict:
    """Admin only. Approve = the next version is drafted to edit; reject = nothing changes. The request's status, the
    deciding admin, the time and the note are kept, and the person who asked is told."""
    if actor_role != "admin":
        raise PermissionError("Only an admin can approve or reject a revision.")
    v = _revision_row(conn, rid)
    if not v:
        raise LookupError("revision not found")
    if v["status"] != "pending":
        raise ValueError(f"This revision was already {v['status']}.")
    note = (note or "").strip()[:1000] or None
    if approve:
        src = get(conn, v["proposal_id"])
        if src["status"] not in REVISABLE:
            raise ValueError(f"The proposal is {src['status']} now, so it can't be revised. Reject the request instead.")
    # claim the request first: a second click (or a second admin) gets "already decided", never a second version
    cur = conn.execute("UPDATE proposal_revision SET status = ?, decided_by = ?, decided_at = ?, decision_note = ? "
                       "WHERE id = ? AND status = 'pending'", ("approved" if approve else "rejected", actor, _now(), note, rid))
    if not (getattr(cur, "rowcount", 0) or 0):
        conn.rollback()
        raise ValueError("Someone decided this revision a moment ago. Reload.")
    conn.commit()
    new_id = None
    if approve:
        try:
            new_id = new_version(conn, v["proposal_id"], actor=actor, supersede=False)["id"]
        except Exception:
            conn.rollback()
            conn.execute("UPDATE proposal_revision SET status = 'pending', decided_by = NULL, decided_at = NULL, "
                         "decision_note = NULL WHERE id = ?", (rid,))
            conn.commit()
            raise
        conn.execute("UPDATE proposal_revision SET new_proposal_id = ? WHERE id = ?", (new_id, rid))
        _event(conn, new_id, "revision_approved", actor=actor, detail=note or v["reason"][:300])
    _event(conn, v["proposal_id"], "revision_approved" if approve else "revision_rejected", actor=actor,
           detail=("new version drafted" if approve else "nothing changed") + (f": {note}" if note else ""))
    conn.commit()
    out = revision_public(_revision_row(conn, rid))
    _notify(conn, [v["requested_by"]] if v["requested_by"] != actor else [],
            f"Proposal {out['label']}: your revision was {'approved' if approve else 'rejected'}",
            f"/proposal-studio#p/{new_id or v['proposal_id']}")
    return out


def withdraw_revision(conn, rid: int, *, actor, actor_role: str) -> dict:
    v = _revision_row(conn, rid)
    if not v:
        raise LookupError("revision not found")
    if v["requested_by"] != actor and actor_role != "admin":
        raise PermissionError("Only the person who asked (or an admin) can withdraw it.")
    if v["status"] != "pending":
        raise ValueError(f"This revision was already {v['status']}.")
    conn.execute("UPDATE proposal_revision SET status = 'withdrawn', decided_by = ?, decided_at = ? WHERE id = ?", (actor, _now(), rid))
    _event(conn, v["proposal_id"], "revision_withdrawn", actor=actor)
    conn.commit()
    return revision_public(_revision_row(conn, rid))


def admin_new_version(conn, proposal_id: int, *, actor, actor_role: str, supersede: bool = True) -> dict:
    """An admin skips the request: the change is recorded as a revision the admin approved, then versioned."""
    if actor_role != "admin":
        raise PermissionError("Only an admin can make a new version directly. Ask for a revision instead.")
    r = get(conn, proposal_id)
    if not r:
        raise LookupError("proposal not found")
    new = new_version(conn, proposal_id, actor=actor, supersede=supersede)
    now = _now()
    conn.execute("INSERT INTO proposal_revision (proposal_id, requested_by, requested_at, reason, status, decided_by, decided_at, "
                 "decision_note, new_proposal_id) VALUES (?,?,?,?, 'approved', ?,?,?,?)",
                 (proposal_id, actor, now, "New version made by an admin", actor, now, None, new["id"]))
    conn.commit()
    return new


# ============================ public page ============================

_PAGE_UI = """
<style>
  .act{max-width:820px;margin:18px auto 40px;background:#fff;border:1px solid #E8E4DD;border-radius:16px;padding:24px 28px}
  .act h3{margin:0 0 6px;font-size:17px}.act p{color:#384152}
  .act label{display:block;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:#76777E;margin:12px 0 5px}
  .act input,.act textarea{width:100%;font:inherit;font-size:14px;padding:10px 12px;border:1px solid #E8E4DD;border-radius:9px;background:#fff}
  .act .row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .act label.chk{display:flex;gap:10px;align-items:flex-start;font-size:13px;font-weight:500;letter-spacing:0;text-transform:none;color:#384152;margin:14px 0 0}
  .act .chk input{width:auto;margin-top:3px}
  .btns{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;align-items:center}
  .btn{font:inherit;font-size:14px;font-weight:700;padding:12px 22px;border-radius:9px;border:1px solid #24418C;background:#24418C;color:#fff;cursor:pointer}
  .btn.ghost{background:#fff;color:#14161C;border-color:#E8E4DD}
  .btn:disabled{opacity:.6;cursor:default}
  .err{display:none;color:#A8382B;background:#fceeec;border-radius:8px;padding:8px 10px;font-size:13px;margin-top:12px}
  .err.show{display:block}
  .top{max-width:820px;margin:18px auto 0;display:flex;justify-content:space-between;align-items:center;gap:12px;padding:0 4px;flex-wrap:wrap}
  .top a{color:#14161C;font-weight:700;font-size:13px;text-decoration:none;border:1px solid #E8E4DD;background:#fff;border-radius:9px;padding:9px 14px}
  .note{font-size:13px;color:#76777E}
  @media (max-width:600px){.act .row{grid-template-columns:1fr}.pad{padding:22px 20px}}
</style>"""


def _page(conn, r: dict, token: str) -> str:
    e = _html.escape
    state = _link_state(r)
    doc = render_document(conn, r, mode="page")
    top = (f"<div class='top'><span class='note'>Proposal {e(_label(r))} · {COMPANY_NAME}</span>"
           f"<a href='/p/{e(token, quote=True)}/pdf'>Download PDF</a></div>")
    if state == "open":
        ui = f"""
<div class="act" id="act">
  <h3>Accept this proposal</h3>
  <p>Type your name to accept. Your name, the date and time, and your network address are recorded with the proposal as your electronic signature.</p>
  <div class="row"><div><label for="nm">Full name</label><input id="nm" autocomplete="name" placeholder="Your full name"></div>
  <div><label for="tt">Title (optional)</label><input id="tt" placeholder="e.g. Property manager"></div></div>
  <label class="chk"><input type="checkbox" id="ag"> I have read the scope, price, exclusions and terms above and accept proposal {e(_label(r))} on behalf of {e(r.get('client_name') or 'the client')}.</label>
  <div class="err" id="er"></div>
  <div class="btns"><button class="btn" id="ok" type="button">Accept proposal</button><button class="btn ghost" id="no" type="button">Decline</button></div>
  <div id="dec" hidden><label for="rs">Let us know why (optional)</label><textarea id="rs" rows="3"></textarea>
  <div class="btns"><button class="btn ghost" id="no2" type="button">Confirm decline</button></div></div>
</div>
<script>
(function(){{
  var T={repr_js(token)};
  function j(u,b){{return fetch(u,{{method:'POST',headers:{{'Content-Type':'application/json','Accept':'application/json'}},body:JSON.stringify(b)}}).then(function(r){{return r.json().then(function(x){{if(!r.ok)throw new Error(x.error||'Request failed');return x;}})}})}}
  function err(m){{var b=document.getElementById('er');b.textContent=m;b.className='err'+(m?' show':'');}}
  document.getElementById('ok').onclick=function(){{
    var nm=document.getElementById('nm').value.trim(),tt=document.getElementById('tt').value.trim();
    if(nm.length<2)return err('Please type your full name.');
    if(!document.getElementById('ag').checked)return err('Please tick the box to confirm you accept the terms.');
    err('');this.disabled=true;
    j('/api/p/'+T+'/accept',{{name:nm,title:tt,agreed:true}}).then(function(){{location.reload();}}).catch(function(x){{err(x.message);document.getElementById('ok').disabled=false;}});
  }};
  document.getElementById('no').onclick=function(){{document.getElementById('dec').hidden=false;this.hidden=true;}};
  document.getElementById('no2').onclick=function(){{
    if(!confirm('Decline this proposal?'))return;this.disabled=true;
    j('/api/p/'+T+'/decline',{{reason:document.getElementById('rs').value}}).then(function(){{location.reload();}}).catch(function(x){{err(x.message);document.getElementById('no2').disabled=false;}});
  }};
}})();
</script>"""
    elif state == "signed":
        ui = (f"<div class='act'><h3>Thank you — this proposal is accepted.</h3><p>Accepted by {e(r.get('signed_name') or '')} on "
              f"{e(_fmt_date(r.get('signed_at')))}. We will be in touch about the deposit invoice and the Certificate of Insurance. "
              f"Keep this link to download the PDF at any time.</p></div>")
    elif state == "declined":
        ui = "<div class='act'><h3>This proposal was declined.</h3><p>If that was a mistake, or you would like a revised proposal, reply to the email you received and we will send a new version.</p></div>"
    elif state == "expired":
        ui = (f"<div class='act'><h3>This proposal has expired.</h3><p>It was valid until {e(_fmt_date(r.get('valid_until')))}. "
              f"Reply to the email you received and we will send a refreshed version.</p></div>")
    else:
        ui = "<div class='act'><h3>A newer version of this proposal has been sent.</h3><p>Please use the link in the most recent email.</p></div>"
    return doc.replace("</head>", _PAGE_UI + "</head>").replace("<body>", "<body>" + top).replace("</body>", ui + "</body>")


def repr_js(s: str) -> str:
    import json
    return json.dumps(s)


def _gone(msg: str, code: int = 404):
    e = _html.escape
    body = (f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{COMPANY_NAME}</title><style>{_DOC_CSS}</style></head><body><div class='sheet' style='margin-top:40px'>"
            f"<div class='bar'></div><div class='pad'><div class='brand'>{COMPANY_NAME}</div><h1>{e(msg)}</h1>"
            f"<p style='color:#76777E'>If you were expecting a proposal, reply to the email you received and we will send a fresh link.</p>"
            f"</div></div></body></html>")
    resp = make_response(body, code)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def _public_page(token):
    conn = _conn()
    try:
        r = _by_token(conn, token)
        if not r:
            return _gone("This proposal link is not valid.")
        if _link_state(r) == "open":
            r = record_view(conn, r, ip=_client_ip())
        return _no_store(make_response(_page(conn, r, token), 200))
    finally:
        conn.close()


def _public_pdf(token):
    conn = _conn()
    try:
        r = _by_token(conn, token)
        if not r:
            return _gone("This proposal link is not valid.")
        p = pdf_file(conn, r["id"])
        if not p:
            return _gone("The PDF is not available right now — the proposal above is the reference copy.", 503)
        resp = make_response(send_file(str(p), mimetype="application/pdf", as_attachment=True,
                                       download_name=f"{_label(r)}.pdf"))
        return _no_store(resp)
    finally:
        conn.close()


def _public_accept(token):
    body = request.get_json(silent=True) or {}
    if not body.get("agreed"):
        return _err("please confirm you accept the terms")
    conn = _conn()
    try:
        r = _by_token(conn, token)
        if not r:
            return _err("this proposal link is not valid", 404)
        try:
            r = accept(conn, r, name=body.get("name"), title=body.get("title"), ip=_client_ip())
        except ValueError as e:
            return _err(str(e), 409 if "no longer" in str(e) else 400)
        return _no_store(jsonify({"ok": True, "status": r["status"], "signed_at": r["signed_at"]}))
    finally:
        conn.close()


def _public_decline(token):
    body = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        r = _by_token(conn, token)
        if not r:
            return _err("this proposal link is not valid", 404)
        try:
            r = decline(conn, r, reason=body.get("reason"), ip=_client_ip())
        except ValueError as e:
            return _err(str(e), 409)
        return _no_store(jsonify({"ok": True, "status": r["status"]}))
    finally:
        conn.close()


# ============================ internal endpoints ============================

def _detail(conn, r: dict) -> dict:
    d = public(r)
    d["lines"] = lines(conn, r["id"])
    d["events"] = events(conn, r["id"])
    d["signed_ip"] = r.get("signed_ip")
    d["amounts"] = amounts(conn, r)
    d["revisions"] = revisions(conn, r["id"])
    return d


def _role() -> Optional[str]:
    return (current_user() or {}).get("role")


def _api_list():
    conn = _conn()
    try:
        rows = list_proposals(conn, status=(request.args.get("status") or "").strip() or None,
                              client_org_id=request.args.get("client_org_id") or None,
                              estimate_id=request.args.get("estimate_id") or None,
                              building_id=request.args.get("building_id") or None,
                              q=request.args.get("q") or None)
        if (request.args.get("status") or "").strip() == "deleted" and _role() != "admin":
            return _err("Only an admin can see deleted proposals.", 403)
        counts = {s: 0 for s in STATUSES}
        for x in _rows(conn.execute("SELECT status, COUNT(*) AS n FROM proposal GROUP BY status")):
            counts[x["status"]] = x["n"]
        if _role() != "admin":
            counts.pop("deleted", None)
        st = (request.args.get("status") or "").strip()
        if request.args.get("include") == "archive" or st == "archived":   # proposals from before the app (2026-10-21)
            import proposal_template
            arch = proposal_template.archive_rows(conn)
            q = (request.args.get("q") or "").strip().lower()
            if q:
                arch = [a for a in arch if q in " ".join(str(a.get(k) or "") for k in ("label", "client_name", "building_address", "title")).lower()]
            if request.args.get("client_org_id"):
                arch = [a for a in arch if str(a["client_org_id"]) == str(request.args["client_org_id"])]
            counts["archived"] = len(proposal_template.archive_rows(conn))
            rows = arch if st == "archived" else (rows + arch if not st else rows)
        signed_ytd = _one(conn.execute("SELECT COALESCE(SUM(price),0) AS v, COUNT(*) AS n FROM proposal "
                                       "WHERE status = 'signed' AND signed_at >= ?", (f"{_today().year}-01-01",)))
        awaiting = _one(conn.execute("SELECT COALESCE(SUM(price),0) AS v, COUNT(*) AS n FROM proposal "
                                     "WHERE status IN ('sent','viewed')"))
        return _wrap(rows, counts=counts, signed_ytd=signed_ytd, awaiting=awaiting, statuses=list(STATUSES))
    finally:
        conn.close()


def _api_create():
    body = request.get_json(silent=True) or {}
    try:
        est_id = int(body.get("estimate_id"))
    except (TypeError, ValueError):
        return _err("estimate_id is required")
    conn = _conn()
    try:
        try:
            r = create_from_estimate(conn, est_id, created_by=_uid())
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 409)
        return _wrap(_detail(conn, r)), 201
    finally:
        conn.close()


def _api_get(proposal_id):
    conn = _conn()
    try:
        r = get(conn, proposal_id)
        if not r:
            return _err("proposal not found", 404)
        return _wrap(_detail(conn, r))
    finally:
        conn.close()


def _api_update(proposal_id):
    body = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        try:
            r = update(conn, proposal_id, body, body.get("lines"), actor=_uid())
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 409 if "draft" in str(e) else 400)
        return _wrap(_detail(conn, r))
    finally:
        conn.close()


def _api_delete(proposal_id):
    """A draft is removed. A sent one (admin only, 2026-10-23) is hidden: gone from every list, count, the client
    portal and the client's link; restorable from the admin's Deleted filter."""
    conn = _conn()
    try:
        try:
            r = get(conn, proposal_id)
            if r and r["status"] != "draft":
                hide(conn, proposal_id, actor=_uid(), actor_role=_role())
                return _wrap({"deleted": proposal_id, "hidden": True})
            delete_draft(conn, proposal_id)
        except PermissionError as e:
            return _err(str(e), 403)
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 409)
        return _wrap({"deleted": proposal_id})
    finally:
        conn.close()


def _api_restore(proposal_id):
    conn = _conn()
    try:
        try:
            r = restore(conn, proposal_id, actor=_uid(), actor_role=_role())
        except PermissionError as e:
            return _err(str(e), 403)
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 409)
        return _wrap(_detail(conn, r))
    finally:
        conn.close()


def _api_record(proposal_id):
    """PATCH {field: number | title | recipient | valid_until, value}: one field edited in place from the list."""
    body = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        try:
            r = edit_record(conn, proposal_id, str(body.get("field") or ""), body.get("value"), actor=_uid())
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 409 if "already" in str(e) else 400)
        return _wrap(_detail(conn, r))
    finally:
        conn.close()


def _api_send(proposal_id):
    body = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        try:
            r = send(conn, proposal_id, to_email=body.get("to_email"), to_name=body.get("to_name"),
                     message=body.get("message"), actor=_uid())
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 409 if "cannot be sent" in str(e) else 400)
        except RuntimeError as e:
            return _err(str(e), 502)
        return _wrap(_detail(conn, r))
    finally:
        conn.close()


def _api_new_version(proposal_id):
    """Admin only since 2026-10-22: everyone else asks for a revision, which an admin approves."""
    conn = _conn()
    try:
        try:
            r = admin_new_version(conn, proposal_id, actor=_uid(), actor_role=_role())
        except PermissionError as e:
            return _err(str(e), 403)
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 409)
        return _wrap(_detail(conn, r)), 201
    finally:
        conn.close()


def _api_revisions(proposal_id):
    conn = _conn()
    try:
        if not get(conn, proposal_id):
            return _err("proposal not found", 404)
        return _wrap(revisions(conn, proposal_id))
    finally:
        conn.close()


def _api_revisions_pending():
    conn = _conn()
    try:
        return _wrap(pending_revisions(conn))
    finally:
        conn.close()


def _api_revision_request(proposal_id):
    body = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        try:
            v = request_revision(conn, proposal_id, actor=_uid(), reason=body.get("reason"),
                                 price_review=bool(body.get("price_review")), proposed_price=body.get("proposed_price"))
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 409 if "already" in str(e) else 400)
        return _wrap(v), 201
    finally:
        conn.close()


def _api_revision_decide(rid, approve: bool):
    body = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        try:
            v = decide_revision(conn, rid, actor=_uid(), actor_role=_role(), approve=approve, note=body.get("note"))
        except PermissionError as e:
            return _err(str(e), 403)
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 409)
        return _wrap(v)
    finally:
        conn.close()


def _api_revision_withdraw(rid):
    conn = _conn()
    try:
        try:
            v = withdraw_revision(conn, rid, actor=_uid(), actor_role=_role())
        except PermissionError as e:
            return _err(str(e), 403)
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 409)
        return _wrap(v)
    finally:
        conn.close()


def _api_mark_signed(proposal_id):
    body = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        try:
            r = mark_signed_offline(conn, proposal_id, name=body.get("name"), signed_on=body.get("signed_on"), actor=_uid())
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 409 if "cannot" in str(e) else 400)
        return _wrap(_detail(conn, r))
    finally:
        conn.close()


def _api_mark_sent(proposal_id):
    body = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        try:
            r = mark_sent_outside(conn, proposal_id, actor=_uid(), sent_on=body.get("sent_on"), to_email=body.get("to_email"),
                                  to_name=body.get("to_name"), how=body.get("how"))
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 409 if "already" in str(e) or "signed" in str(e) else 400)
        return _wrap(_detail(conn, r))
    finally:
        conn.close()


def _api_preview(proposal_id):
    conn = _conn()
    try:
        r = get(conn, proposal_id)
        if not r:
            return _err("proposal not found", 404)
        return _no_store(make_response(render_document(conn, r, mode="pdf"), 200))
    finally:
        conn.close()


def _api_pdf(proposal_id):
    conn = _conn()
    try:
        r = get(conn, proposal_id)
        if not r:
            return _err("proposal not found", 404)
        p = pdf_file(conn, r["id"])
        if not p:
            return _err("PDF engine unavailable on this server — use Preview and print to PDF", 503)
        return _no_store(make_response(send_file(str(p), mimetype="application/pdf", as_attachment=True,
                                                 download_name=f"{_label(r)}.pdf")))
    finally:
        conn.close()


def register(app) -> None:
    conn = _conn()
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    gate = requires_section(SECTION)
    app.add_url_rule("/api/proposals", "proposals_list", gate(_api_list), methods=["GET"])
    app.add_url_rule("/api/proposals", "proposals_create", gate(_api_create), methods=["POST"])
    app.add_url_rule("/api/proposals/<int:proposal_id>", "proposals_get", gate(_api_get), methods=["GET"])
    app.add_url_rule("/api/proposals/<int:proposal_id>", "proposals_update", gate(_api_update), methods=["PATCH"])
    app.add_url_rule("/api/proposals/<int:proposal_id>", "proposals_delete", gate(_api_delete), methods=["DELETE"])
    app.add_url_rule("/api/proposals/<int:proposal_id>/send", "proposals_send", gate(_api_send), methods=["POST"])
    app.add_url_rule("/api/proposals/<int:proposal_id>/new-version", "proposals_new_version", gate(_api_new_version), methods=["POST"])
    app.add_url_rule("/api/proposals/<int:proposal_id>/restore", "proposals_restore", gate(_api_restore), methods=["POST"])
    app.add_url_rule("/api/proposals/<int:proposal_id>/record", "proposals_record", gate(_api_record), methods=["PATCH"])
    app.add_url_rule("/api/proposals/<int:proposal_id>/mark-sent", "proposals_mark_sent", gate(_api_mark_sent), methods=["POST"])
    app.add_url_rule("/api/proposals/<int:proposal_id>/mark-signed", "proposals_mark_signed", gate(_api_mark_signed), methods=["POST"])
    app.add_url_rule("/api/proposals/<int:proposal_id>/preview", "proposals_preview", gate(_api_preview), methods=["GET"])
    app.add_url_rule("/api/proposals/<int:proposal_id>/pdf", "proposals_pdf", gate(_api_pdf), methods=["GET"])
    # revisions (2026-10-22): anyone on proposals asks; approve / reject check the admin role inside decide_revision too
    admin = requires_role("admin")
    app.add_url_rule("/api/proposals/<int:proposal_id>/revisions", "proposals_revisions", gate(_api_revisions), methods=["GET"])
    app.add_url_rule("/api/proposals/<int:proposal_id>/revisions", "proposals_revision_request", gate(_api_revision_request), methods=["POST"])
    app.add_url_rule("/api/proposals/revisions", "proposals_revisions_pending", gate(_api_revisions_pending), methods=["GET"])
    app.add_url_rule("/api/proposals/revisions/<int:rid>/approve", "proposals_revision_approve",
                     gate(admin(lambda rid: _api_revision_decide(rid, True))), methods=["POST"])
    app.add_url_rule("/api/proposals/revisions/<int:rid>/reject", "proposals_revision_reject",
                     gate(admin(lambda rid: _api_revision_decide(rid, False))), methods=["POST"])
    app.add_url_rule("/api/proposals/revisions/<int:rid>/withdraw", "proposals_revision_withdraw", gate(_api_revision_withdraw), methods=["POST"])
    # public — token is the credential (auth._PUBLIC_PREFIXES: /p/ and /api/p/)
    app.add_url_rule("/p/<token>", "proposal_public_page", _public_page, methods=["GET"])
    app.add_url_rule("/p/<token>/pdf", "proposal_public_pdf", _public_pdf, methods=["GET"])
    app.add_url_rule("/api/p/<token>/accept", "proposal_public_accept", _public_accept, methods=["POST"])
    app.add_url_rule("/api/p/<token>/decline", "proposal_public_decline", _public_decline, methods=["POST"])
