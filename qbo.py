"""#316 — QuickBooks Online, phase 1: invoices go out, payments come back.

ONE company-level connection. An admin connects the company's QuickBooks once (Intuit
OAuth 2.0); everyone with Finance (admin / C-suite) then sees the result:

  * clients here are linked to QuickBooks customers (matched by name or billing email, or
    created there);
  * issuing an invoice here creates the same invoice in QuickBooks (one line, the invoice
    amount, the description; DocNumber = our INV-YYYY-####); voiding here voids it there;
  * a payment recorded here is sent to QuickBooks as a Payment against that invoice;
  * payments recorded in QuickBooks (checks, QuickBooks Payments card / ACH) come back here
    every QBO_POLL_MINUTES (15) through Intuit's change feed (CDC) and close the invoice;
  * "Send from QuickBooks" emails the invoice through QuickBooks (its Pay now button).

Not in phase 1: bills / receipts / vehicle costs to QuickBooks (phase 2); invoices typed
straight into QuickBooks are not copied in.

  GET  /quickbooks/connect                    admin -> Intuit consent
  GET  /quickbooks/callback                   Intuit sends the admin back here
  GET  /api/quickbooks/status                 Finance: connected? stats, invoices that need you
  GET  /api/quickbooks/clients                Finance: clients here + QuickBooks customers + suggestions
  POST /api/quickbooks/clients                Finance: save the matches (+ send the open invoices)
  POST /api/quickbooks/sync                   Finance: check now (payments back, retries)
  POST /api/quickbooks/invoices/<id>/push     Finance: send one issued invoice to QuickBooks
  POST /api/quickbooks/invoices/<id>/send     Finance: email it from QuickBooks
  POST /api/quickbooks/disconnect             admin: revoke and forget the connection

Rules
  * Switched off (status.available = false, connect 404) until QBO_CLIENT_ID / _SECRET, a
    redirect URI (QBO_REDIRECT_URI, else APP_BASE_URL + /quickbooks/callback) and the
    field-encryption key (secret_box) are set. QBO_ENVIRONMENT = sandbox (default) | production.
  * The refresh token is stored encrypted (secret_box, fails closed). Intuit rotates it on
    every refresh, so the new one is saved each time. Access tokens live in memory only.
  * Cost cap: Intuit's free Builder tier meters READS (500,000 a month). Every read is
    counted per day and stops at QBO_DAILY_READ_CAP (15,000) — well under the monthly
    allowance. Writes (create invoice / customer / payment) are not metered by Intuit.
  * QuickBooks errors never break the app write that triggered them: the invoice is marked
    qb_status = 'error' with Intuit's message, shown under "needs you", and retried on the
    next check.
  * Never logged: tokens, client names, emails, amounts. Logs carry ids and counts.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from flask import jsonify, redirect, request

import db_layer
import secret_box
from auth import current_user, requires_role, requires_section

log = logging.getLogger("ssc.qbo")

SECTION = "company_portal"
AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
API_BASE = {"sandbox": "https://sandbox-quickbooks.api.intuit.com",
            "production": "https://quickbooks.api.intuit.com"}
APP_BASE = {"sandbox": "https://app.sandbox.qbo.intuit.com",
            "production": "https://app.qbo.intuit.com"}
SCOPE = "com.intuit.quickbooks.accounting"
MINOR = "75"
STATE_COOKIE = "qbo_state"
SYNC_INLINE = False          # tests flip this so pushes run before the request returns
ITEM_NAME = "Construction services"


# ------------------------------------------------------------------ config
def _client_id() -> str:
    return (os.environ.get("QBO_CLIENT_ID") or "").strip()


def _client_secret() -> str:
    return (os.environ.get("QBO_CLIENT_SECRET") or "").strip()


def environment() -> str:
    e = (os.environ.get("QBO_ENVIRONMENT") or "sandbox").strip().lower()
    return e if e in API_BASE else "sandbox"


def redirect_uri() -> str:
    explicit = (os.environ.get("QBO_REDIRECT_URI") or "").strip()
    if explicit:
        return explicit
    base = (os.environ.get("APP_BASE_URL") or "").strip().rstrip("/")
    return base + "/quickbooks/callback" if base.startswith("http") else ""


def read_cap() -> int:
    try:
        return max(100, int(os.environ.get("QBO_DAILY_READ_CAP") or 15000))
    except ValueError:
        return 15000


def poll_seconds() -> int:
    try:
        return max(5, int(os.environ.get("QBO_POLL_MINUTES") or 15)) * 60
    except ValueError:
        return 900


def unavailable_reason() -> Optional[str]:
    if not (_client_id() and _client_secret() and redirect_uri()):
        return "quickbooks_not_configured"
    if not secret_box.is_configured():
        return "encryption_key_missing"
    return None


# ------------------------------------------------------------------ schema
def _has_column(conn, table: str, column: str) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute("SELECT 1 AS x FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
                                 (table, column)).fetchone())
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


_INVOICE_COLUMNS = (("qb_error", "TEXT"), ("qb_doc_number", "TEXT"), ("qb_realm_id", "TEXT"), ("qb_synced_at", "TEXT"))
_PAYMENT_COLUMNS = (("qb_payment_id", "TEXT"), ("source", "TEXT"))


def ensure(conn) -> None:
    pk = ("id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" if db_layer.is_postgres()
          else "id INTEGER PRIMARY KEY AUTOINCREMENT")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS qbo_connection ("
        f"  {pk},"
        f"  realm_id TEXT NOT NULL,"
        f"  company_name TEXT,"
        f"  environment TEXT NOT NULL,"
        f"  refresh_token_enc TEXT NOT NULL,"
        f"  status TEXT NOT NULL,"
        f"  connected_by INTEGER,"
        f"  connected_at TEXT NOT NULL,"
        f"  matched_at TEXT,"
        f"  poll_cursor TEXT,"
        f"  last_poll_at TEXT,"
        f"  last_error TEXT,"
        f"  item_id TEXT,"
        f"  reads_day TEXT,"
        f"  reads_count INTEGER"
        f")")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS qbo_customer ("
        f"  {pk},"
        f"  realm_id TEXT NOT NULL,"
        f"  org_id INTEGER NOT NULL,"
        f"  qb_customer_id TEXT NOT NULL,"
        f"  qb_name TEXT,"
        f"  how TEXT,"
        f"  linked_at TEXT NOT NULL"
        f")")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_qbo_customer_org ON qbo_customer(realm_id, org_id)")
    for table, cols in (("invoice", _INVOICE_COLUMNS), ("invoice_payment", _PAYMENT_COLUMNS)):
        for col, ddl in cols:
            if not _has_column(conn, table, col):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invoice_qb ON invoice(qb_invoice_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invoice_payment_qb ON invoice_payment(qb_payment_id)")
    conn.commit()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")   # LOCAL


def _rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]


def _one(cur) -> Optional[dict]:
    r = cur.fetchone()
    return dict(r) if r else None


def connection(conn) -> Optional[dict]:
    return _one(conn.execute("SELECT * FROM qbo_connection ORDER BY id DESC LIMIT 1"))


def is_connected(conn) -> bool:
    if unavailable_reason():
        return False
    try:
        link = connection(conn)
    except Exception:
        conn.rollback()
        return False
    return bool(link and link["status"] == "active")


# ------------------------------------------------------------------ Intuit HTTP (tests swap _http)
def _http(method: str, url: str, *, token: Optional[str] = None, basic: bool = False, data=None, json_body=None) -> tuple:
    import requests
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    elif basic:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{_client_id()}:{_client_secret()}".encode()).decode()
    r = requests.request(method, url, headers=headers, data=data, json=json_body, timeout=20)
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {}
    return r.status_code, body


class NeedsReconnect(Exception):
    pass


class QBError(Exception):
    """Intuit refused a request. str() is Intuit's message, safe to show to the office."""


class CapReached(Exception):
    pass


_TOKENS: dict = {}    # realm_id -> (access_token, expires_at); memory only
_LOCK = threading.RLock()   # one QuickBooks conversation at a time: no double invoices (single waitress process)


def _access_token(conn, link: dict) -> str:
    cached = _TOKENS.get(link["realm_id"])
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    refresh = secret_box.decrypt_text(link["refresh_token_enc"])
    code, body = _http("POST", TOKEN_URL, basic=True, data={"grant_type": "refresh_token", "refresh_token": refresh})
    if code != 200 or not body.get("access_token"):
        if code in (400, 401) and body.get("error") in ("invalid_grant", "invalid_client", "unauthorized_client"):
            raise NeedsReconnect()
        raise RuntimeError(f"token refresh failed ({code})")
    if body.get("refresh_token") and body["refresh_token"] != refresh:   # Intuit rotates it: keep the newest
        link["refresh_token_enc"] = secret_box.encrypt_text(body["refresh_token"])
        conn.execute("UPDATE qbo_connection SET refresh_token_enc = ? WHERE id = ?", (link["refresh_token_enc"], link["id"]))
        conn.commit()
    _TOKENS[link["realm_id"]] = (body["access_token"], time.time() + int(body.get("expires_in") or 3600))
    return body["access_token"]


def _fault(body) -> str:
    try:
        err = (body.get("Fault") or body.get("fault") or {}).get("Error") or (body.get("Fault") or {}).get("error") or []
        e = err[0]
        msg = (e.get("Message") or "").strip()
        det = (e.get("Detail") or "").strip()
        out = msg if not det or det == msg else f"{msg}: {det}"
        return out[:300] or "QuickBooks refused the request"
    except Exception:
        return "QuickBooks refused the request"


def _count_read(conn, link: dict) -> None:
    today = date.today().isoformat()
    n = int(link.get("reads_count") or 0) if link.get("reads_day") == today else 0
    if n >= read_cap():
        raise CapReached()
    conn.execute("UPDATE qbo_connection SET reads_day = ?, reads_count = ? WHERE id = ?", (today, n + 1, link["id"]))
    conn.commit()
    link["reads_day"], link["reads_count"] = today, n + 1


def _api(conn, link: dict, method: str, path: str, *, params: Optional[dict] = None, json_body=None) -> dict:
    """One call to the Accounting API. GETs count against the daily read cap."""
    if method == "GET":
        _count_read(conn, link)
    q = {"minorversion": MINOR, **(params or {})}
    url = f"{API_BASE[link['environment']]}/v3/company/{link['realm_id']}/{path}?{urllib.parse.urlencode(q)}"
    for attempt in (1, 2):
        code, body = _http(method, url, token=_access_token(conn, link), json_body=json_body)
        if code == 401 and attempt == 1:
            _TOKENS.pop(link["realm_id"], None)   # expired early: refresh once and retry
            continue
        if 200 <= code < 300:
            return body or {}
        if code == 401:
            raise NeedsReconnect()
        raise QBError(_fault(body))
    raise QBError("QuickBooks refused the request")


def _query(conn, link: dict, sql: str) -> dict:
    return (_api(conn, link, "GET", "query", params={"query": sql}).get("QueryResponse") or {})


def _q(s: str) -> str:
    """A literal inside a QuickBooks query: escape backslashes and single quotes."""
    return str(s).replace("\\", "\\\\").replace("'", "\\'")


# ------------------------------------------------------------------ customers
_DROP = {"llc", "inc", "corp", "corporation", "co", "company", "the", "ltd", "lp", "llp", "pc", "of"}
_SAME = {"management": "mgmt", "mgt": "mgmt", "cooperative": "coop", "street": "st", "avenue": "ave",
         "condominium": "condo", "condominiums": "condo", "associates": "assoc", "and": "&", "apartments": "apts"}


def _norm(name: str) -> str:
    s = (name or "").lower().replace("co-op", "coop").replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", s)
    return " ".join(_SAME.get(w, w) for w in words if w not in _DROP)


def _similar(a: str, b: str) -> float:
    x, y = set(_norm(a).split()), set(_norm(b).split())
    return len(x & y) / len(x | y) if x and y else 0.0


def _org_emails(conn, org_id: int) -> list:
    return [r["email"].strip().lower() for r in conn.execute(
        "SELECT email FROM crm_contact WHERE org_id = ? AND email IS NOT NULL AND email <> '' ORDER BY id", (org_id,)).fetchall()]


def suggest(org: dict, emails: list, customers: list) -> Optional[dict]:
    """The QuickBooks customer this client most likely is, with how we know."""
    n = _norm(org["name"])
    for c in customers:
        if n and _norm(c["name"]) == n:
            return {"id": c["id"], "name": c["name"], "how": "Same name"}
    for c in customers:
        if c.get("email") and c["email"] in emails:
            return {"id": c["id"], "name": c["name"], "how": "Same billing email"}
    best = max(customers, key=lambda c: _similar(org["name"], c["name"]), default=None)
    if best and _similar(org["name"], best["name"]) >= 0.6:
        return {"id": best["id"], "name": best["name"], "how": "Similar name"}
    return None


def list_customers(conn, link: dict) -> list:
    out, start = [], 1
    for _ in range(5):   # up to 5,000 customers
        res = _query(conn, link, f"select * from Customer where Active = true startposition {start} maxresults 1000")
        page = res.get("Customer") or []
        out += [{"id": str(c["Id"]), "name": c.get("DisplayName") or c.get("CompanyName") or "",
                 "email": ((c.get("PrimaryEmailAddr") or {}).get("Address") or "").strip().lower() or None} for c in page]
        if len(page) < 1000:
            break
        start += 1000
    return sorted(out, key=lambda c: c["name"].lower())


def _clients(conn, realm: str) -> list:
    """Clients we invoice (any invoice) plus any already linked."""
    return _rows(conn.execute(
        "SELECT o.id, o.name, q.qb_customer_id, q.qb_name, q.how FROM crm_organization o "
        "LEFT JOIN qbo_customer q ON q.org_id = o.id AND q.realm_id = ? "
        "WHERE o.id IN (SELECT client_org_id FROM invoice) OR q.id IS NOT NULL ORDER BY o.name", (realm,)))


def _customer_link(conn, realm: str, org_id: int) -> Optional[dict]:
    return _one(conn.execute("SELECT * FROM qbo_customer WHERE realm_id = ? AND org_id = ?", (realm, org_id)))


def _save_link(conn, realm: str, org_id: int, qb_id: str, qb_name: str, how: str) -> None:
    conn.execute("DELETE FROM qbo_customer WHERE realm_id = ? AND org_id = ?", (realm, org_id))
    conn.execute("INSERT INTO qbo_customer (realm_id, org_id, qb_customer_id, qb_name, how, linked_at) VALUES (?,?,?,?,?,?)",
                 (realm, org_id, str(qb_id), (qb_name or "")[:200], how, _now()))
    conn.commit()


def _create_customer(conn, link: dict, org: dict) -> dict:
    name = (org["name"] or f"Client {org['id']}").strip()[:100]
    body = {"DisplayName": name, "CompanyName": name}
    emails = _org_emails(conn, org["id"])
    if emails:
        body["PrimaryEmailAddr"] = {"Address": emails[0]}
    try:
        c = _api(conn, link, "POST", "customer", json_body=body).get("Customer") or {}
    except QBError as e:
        if "Duplicate Name" not in str(e):
            raise
        found = (_query(conn, link, f"select * from Customer where DisplayName = '{_q(name)}'").get("Customer") or [])
        if not found:
            raise
        c = found[0]
    return {"id": str(c["Id"]), "name": c.get("DisplayName") or name}


def ensure_customer(conn, link: dict, org_id: int) -> str:
    """The QuickBooks customer id for this client: the saved link, else the same name there, else created."""
    have = _customer_link(conn, link["realm_id"], org_id)
    if have:
        return have["qb_customer_id"]
    org = _one(conn.execute("SELECT id, name FROM crm_organization WHERE id = ?", (org_id,)))
    if not org:
        raise QBError("the client on this invoice no longer exists")
    found = (_query(conn, link, f"select * from Customer where DisplayName = '{_q((org['name'] or '').strip()[:100])}'")
             .get("Customer") or [])
    if found:
        c, how = {"id": str(found[0]["Id"]), "name": found[0].get("DisplayName")}, "auto"
    else:
        c, how = _create_customer(conn, link, org), "created"
    _save_link(conn, link["realm_id"], org_id, c["id"], c["name"], how)
    return c["id"]


# ------------------------------------------------------------------ invoices
def _item_id(conn, link: dict) -> str:
    """The service item every invoice line uses: QuickBooks' own "Services", else one we create."""
    if link.get("item_id"):
        return link["item_id"]
    items = _query(conn, link, "select * from Item where Name = 'Services'").get("Item") or []
    if not items:
        items = _query(conn, link, f"select * from Item where Name = '{ITEM_NAME}'").get("Item") or []
    if items:
        iid = str(items[0]["Id"])
    else:
        acct = _query(conn, link, "select * from Account where AccountType = 'Income' maxresults 1").get("Account") or []
        if not acct:
            raise QBError("QuickBooks has no income account to put the invoice line on")
        it = _api(conn, link, "POST", "item", json_body={"Name": ITEM_NAME, "Type": "Service",
                                                        "IncomeAccountRef": {"value": str(acct[0]["Id"])}}).get("Item") or {}
        iid = str(it["Id"])
    conn.execute("UPDATE qbo_connection SET item_id = ? WHERE id = ?", (iid, link["id"]))
    conn.commit()
    link["item_id"] = iid
    return iid


def _invoice_body(inv: dict, customer_id: str, item_id: str, email: Optional[str]) -> dict:
    amt = round(float(inv["amount"]), 2)
    desc = (inv.get("description") or f"{(inv.get('kind') or 'other').replace('_', ' ').title()} invoice").strip()
    if inv.get("project_name"):
        desc = f"{desc}\nJob: {inv['project_name']}"
    body = {"CustomerRef": {"value": customer_id}, "DocNumber": inv["number"][:21],
            "TxnDate": inv.get("issued_at") or date.today().isoformat(),
            "Line": [{"DetailType": "SalesItemLineDetail", "Amount": amt, "Description": desc[:4000],
                      "SalesItemLineDetail": {"ItemRef": {"value": item_id}, "Qty": 1, "UnitPrice": amt}}],
            "PrivateNote": f"From Superstars OS, invoice {inv['number']}"}
    if inv.get("due_at"):
        body["DueDate"] = inv["due_at"]
    if email:
        body["BillEmail"] = {"Address": email}
    if float(inv.get("retainage") or 0) > 0:
        body["CustomerMemo"] = {"value": f"Retainage held: ${float(inv['retainage']):,.2f}"}
    return body


def _mark(conn, invoice_id: int, **cols) -> None:
    cols["qb_synced_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE invoice SET {sets} WHERE id = ?", (*cols.values(), invoice_id))
    conn.commit()


def push_invoice(conn, invoice_id: int) -> dict:
    """Make sure this issued invoice (and its payments) is in QuickBooks. Never raises."""
    import invoice_docs
    import invoices
    with _LOCK:
        link = connection(conn)
        if not link or link["status"] != "active" or unavailable_reason():
            return {"skipped": True}
        inv = invoices.get(conn, invoice_id)
        if not inv or inv["status"] == "draft":
            return {"skipped": True}
        try:
            if inv.get("qb_invoice_id") and inv.get("qb_realm_id") not in (None, link["realm_id"]):
                return {"skipped": True}   # it lives in another QuickBooks company
            if inv["status"] == "void":
                return _void_in_qb(conn, link, inv)
            if not inv.get("qb_invoice_id"):
                found = (_query(conn, link, f"select * from Invoice where DocNumber = '{_q(inv['number'][:21])}'")
                         .get("Invoice") or [])   # a retry after a lost answer: never make it twice
                if found:
                    qbi = found[0]
                else:
                    cust = ensure_customer(conn, link, inv["client_org_id"])
                    email = invoice_docs.default_recipient(conn, inv).get("email")
                    qbi = _api(conn, link, "POST", "invoice",
                               json_body=_invoice_body(inv, cust, _item_id(conn, link), email)).get("Invoice") or {}
                _mark(conn, invoice_id, qb_invoice_id=str(qbi["Id"]), qb_doc_number=qbi.get("DocNumber") or None,
                      qb_realm_id=link["realm_id"], qb_status="paid" if inv["status"] == "paid" else "created", qb_error=None)
                log.info("qbo invoice %s created in quickbooks", invoice_id)
                inv = invoices.get(conn, invoice_id)
            pushed = _push_payments(conn, link, inv)
            want = "paid" if inv["status"] == "paid" else (inv.get("qb_status") if inv.get("qb_status") == "emailed" else "created")
            if inv.get("qb_error") or inv.get("qb_status") != want:
                _mark(conn, invoice_id, qb_status=want, qb_error=None)
            return {"ok": True, "qb_invoice_id": inv["qb_invoice_id"], "payments": pushed}
        except NeedsReconnect:
            _needs_reconnect(conn, link)
            return {"needs_reconnect": True}
        except CapReached:
            _mark(conn, invoice_id, qb_status="pending")
            return {"capped": True}
        except QBError as e:
            _mark(conn, invoice_id, qb_status="error", qb_error=str(e))
            log.info("qbo invoice %s refused by quickbooks", invoice_id)
            return {"error": str(e)}
        except Exception as e:
            conn.rollback()
            _mark(conn, invoice_id, qb_status="error", qb_error="QuickBooks did not answer; it will retry")
            log.warning("qbo invoice %s push failed: %s", invoice_id, type(e).__name__)
            return {"error": "retry"}


def _push_payments(conn, link: dict, inv: dict) -> int:
    rows = _rows(conn.execute("SELECT * FROM invoice_payment WHERE invoice_id = ? AND qb_payment_id IS NULL "
                              "AND COALESCE(source, 'app') <> 'quickbooks' ORDER BY id", (inv["id"],)))
    if not rows:
        return 0
    cust = ensure_customer(conn, link, inv["client_org_id"])
    for p in rows:
        ref = (p.get("reference") or p.get("method") or "")[:21] or None
        body = {"CustomerRef": {"value": cust}, "TotalAmt": round(float(p["amount"]), 2), "TxnDate": p["paid_on"],
                "PrivateNote": f"Recorded in Superstars OS ({p.get('method') or 'payment'}) on {inv['number']}",
                "Line": [{"Amount": round(float(p["amount"]), 2),
                          "LinkedTxn": [{"TxnId": inv["qb_invoice_id"], "TxnType": "Invoice"}]}]}
        if ref:
            body["PaymentRefNum"] = ref
        qp = _api(conn, link, "POST", "payment", json_body=body).get("Payment") or {}
        conn.execute("UPDATE invoice_payment SET qb_payment_id = ?, source = COALESCE(source, 'app') WHERE id = ?",
                     (f"{qp['Id']}:{inv['qb_invoice_id']}", p["id"]))
        conn.commit()
    log.info("qbo invoice %s: %s payment(s) sent to quickbooks", inv["id"], len(rows))
    return len(rows)


def _void_in_qb(conn, link: dict, inv: dict) -> dict:
    if not inv.get("qb_invoice_id"):
        if inv.get("qb_status"):   # voided before it ever reached QuickBooks: nothing to mirror
            _mark(conn, inv["id"], qb_status=None, qb_error=None)
        return {"skipped": True}
    if inv.get("qb_status") == "voided":
        return {"skipped": True}
    cur = _api(conn, link, "GET", f"invoice/{inv['qb_invoice_id']}").get("Invoice") or {}
    _api(conn, link, "POST", "invoice", params={"operation": "void"},
         json_body={"Id": inv["qb_invoice_id"], "SyncToken": cur.get("SyncToken", "0")})
    _mark(conn, inv["id"], qb_status="voided", qb_error=None)
    log.info("qbo invoice %s voided in quickbooks", inv["id"])
    return {"ok": True, "voided": True}


def send_from_qb(conn, invoice_id: int, to_email: Optional[str] = None) -> dict:
    """Email the invoice through QuickBooks (its Pay now button when QuickBooks Payments is on)."""
    import invoice_docs
    import invoices
    inv = invoices.get(conn, invoice_id)
    if not inv:
        raise LookupError("invoice not found")
    if inv["status"] not in ("issued",):
        raise ValueError("only an open (issued) invoice can be emailed")
    if not inv.get("qb_invoice_id"):
        res = push_invoice(conn, invoice_id)
        inv = invoices.get(conn, invoice_id)
        if not inv.get("qb_invoice_id"):
            raise ValueError(res.get("error") or "the invoice is not in QuickBooks yet")
    to = (to_email or inv.get("sent_to") or invoice_docs.default_recipient(conn, inv).get("email") or "").strip().lower()
    if "@" not in to or "." not in to.split("@")[-1]:
        raise ValueError("a valid recipient email is required")
    with _LOCK:
        link = connection(conn)
        try:
            _api(conn, link, "POST", f"invoice/{inv['qb_invoice_id']}/send", params={"sendTo": to})
        except NeedsReconnect:
            _needs_reconnect(conn, link)
            raise ValueError("QuickBooks needs to be reconnected")
        except QBError as e:
            raise ValueError(str(e))
        except Exception as e:
            conn.rollback()
            log.warning("qbo send %s failed: %s", invoice_id, type(e).__name__)
            raise ValueError("QuickBooks did not answer; try again")
    now = _now()
    conn.execute("UPDATE invoice SET qb_status = 'emailed', sent_at = ?, sent_to = ?, sent_count = COALESCE(sent_count, 0) + 1, "
                 "updated_at = ? WHERE id = ?", (now, to, now, invoice_id))
    conn.commit()
    try:
        import crm
        crm.add_activity(conn, entity_type="organization", entity_id=inv["client_org_id"], activity_type="system",
                         author_user_id=_uid(), function_tag="finance",
                         summary=f"Invoice {inv['number']} emailed from QuickBooks")
    except Exception as e:
        log.info("qbo activity not logged: %s", type(e).__name__)
    log.info("qbo invoice %s emailed from quickbooks", invoice_id)
    return invoice_docs.public(invoices.get(conn, invoice_id))


# ------------------------------------------------------------------ payments back + retries
def _cdc_since(link: dict) -> str:
    floor = datetime.now(timezone.utc) - timedelta(days=29)   # Intuit looks back 30 days at most
    cur = link.get("poll_cursor")
    try:
        t = datetime.fromisoformat(cur) if cur else None
        if t and t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except ValueError:
        t = None
    if not t:
        t = datetime.fromisoformat(link["connected_at"]).astimezone(timezone.utc) - timedelta(days=1)
    return max(t, floor).isoformat(timespec="seconds")


def pull_payments(conn, link: dict) -> int:
    """Payments recorded in QuickBooks since the last check -> payments here."""
    import invoices
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = _api(conn, link, "GET", "cdc", params={"entities": "Payment", "changedSince": _cdc_since(link)})
    made = 0
    for block in (body.get("CDCResponse") or []):
        for qr in (block.get("QueryResponse") or []):
            for p in (qr.get("Payment") or []):
                if p.get("status") == "Deleted":
                    log.info("qbo payment deleted in quickbooks (not undone here)")
                    continue
                for line in (p.get("Line") or []):
                    for lt in (line.get("LinkedTxn") or []):
                        if lt.get("TxnType") != "Invoice":
                            continue
                        key = f"{p['Id']}:{lt['TxnId']}"
                        if conn.execute("SELECT 1 FROM invoice_payment WHERE qb_payment_id = ?", (key,)).fetchone():
                            continue
                        inv = _one(conn.execute("SELECT id, status, amount, amount_paid FROM invoice WHERE qb_invoice_id = ? "
                                                "AND qb_realm_id = ?", (str(lt["TxnId"]), link["realm_id"])))
                        if not inv or inv["status"] != "issued":
                            continue
                        amt = min(round(float(line.get("Amount") or 0), 2),
                                  round(float(inv["amount"]) - float(inv["amount_paid"] or 0), 2))
                        if amt <= 0:
                            continue
                        ref = p.get("PaymentRefNum") or p["Id"]
                        invoices.record_payment(conn, inv["id"], amount=amt, paid_on=(p.get("TxnDate") or "")[:10] or None,
                                                method="quickbooks", reference=f"QuickBooks payment {ref}"[:200],
                                                qb_payment_id=key)
                        if invoices.get(conn, inv["id"])["status"] == "paid":
                            _mark(conn, inv["id"], qb_status="paid")
                        made += 1
    conn.execute("UPDATE qbo_connection SET poll_cursor = ? WHERE id = ?", (body.get("time") or started, link["id"]))
    conn.commit()
    link["poll_cursor"] = body.get("time") or started
    if made:
        log.info("qbo %s payment(s) brought back from quickbooks", made)
    return made


def check(conn) -> dict:
    """The 15-minute round (and "Check now"): payments back, then retry what is waiting."""
    with _LOCK:
        link = connection(conn)
        if not link or link["status"] != "active" or unavailable_reason():
            return {"skipped": True}
        out = {"payments": 0, "sent": 0, "errors": 0}
        try:
            out["payments"] = pull_payments(conn, link)
            waiting = [r["id"] for r in conn.execute(
                "SELECT id FROM invoice WHERE status IN ('issued', 'paid', 'void') AND qb_status IN ('pending', 'error') "
                "ORDER BY id LIMIT 50").fetchall()]
            waiting += [r["id"] for r in conn.execute(   # payments recorded here that did not reach QuickBooks yet
                "SELECT DISTINCT i.id FROM invoice i JOIN invoice_payment p ON p.invoice_id = i.id "
                "WHERE i.qb_invoice_id IS NOT NULL AND i.qb_realm_id = ? AND p.qb_payment_id IS NULL "
                "AND COALESCE(p.source, 'app') <> 'quickbooks' LIMIT 50", (link["realm_id"],)).fetchall()]
            for iid in dict.fromkeys(waiting):
                res = push_invoice(conn, iid)
                if res.get("needs_reconnect") or res.get("capped"):
                    break
                out["errors" if res.get("error") else "sent"] += 1
            conn.execute("UPDATE qbo_connection SET last_poll_at = ?, last_error = NULL WHERE id = ?", (_now(), link["id"]))
            conn.commit()
        except NeedsReconnect:
            _needs_reconnect(conn, link)
            out["needs_reconnect"] = True
        except CapReached:
            conn.execute("UPDATE qbo_connection SET last_error = ? WHERE id = ?",
                         ("Daily QuickBooks limit reached; checking resumes tomorrow", link["id"]))
            conn.commit()
            out["capped"] = True
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.execute("UPDATE qbo_connection SET last_error = ? WHERE id = ?", ("The last check did not finish; it will retry", link["id"]))
            conn.commit()
            log.warning("qbo check failed: %s", type(e).__name__)
            out["error"] = True
        return out


def _needs_reconnect(conn, link: dict) -> None:
    _TOKENS.pop(link["realm_id"], None)
    conn.execute("UPDATE qbo_connection SET status = 'needs_reconnect', last_error = ? WHERE id = ?",
                 ("The QuickBooks sign-in expired; an admin needs to reconnect", link["id"]))
    conn.commit()
    log.info("qbo needs reconnect")


# ------------------------------------------------------------------ hooks (invoices.py calls these)
def after(event: str, invoice_id: int) -> None:
    """issued / paid_here / voided: queue the QuickBooks side. Never raises; off when not connected."""
    conn = db_layer.connect()
    try:
        if not is_connected(conn):
            return
        row = _one(conn.execute("SELECT qb_invoice_id, qb_status FROM invoice WHERE id = ?", (invoice_id,)))
        if not row:
            return
        if event == "issued" and not row["qb_invoice_id"]:
            conn.execute("UPDATE invoice SET qb_status = 'pending', qb_error = NULL WHERE id = ?", (invoice_id,))
            conn.commit()
        elif event in ("paid_here", "voided") and not row["qb_invoice_id"]:
            return   # never sent there: nothing to mirror
    finally:
        conn.close()
    schedule(invoice_id)


def schedule(invoice_id: int) -> None:
    if SYNC_INLINE:
        conn = db_layer.connect()
        try:
            push_invoice(conn, invoice_id)
        finally:
            conn.close()
        return
    threading.Thread(target=_bg_push, args=(invoice_id,), daemon=True).start()


def _bg_push(invoice_id: int) -> None:
    conn = db_layer.connect()
    try:
        push_invoice(conn, invoice_id)
    except Exception as e:
        log.warning("qbo background push %s: %s", invoice_id, type(e).__name__)
    finally:
        conn.close()


_LOOP = {"started": False, "wake": threading.Event()}


def _loop() -> None:
    time.sleep(30)
    while True:
        conn = db_layer.connect()
        try:
            if is_connected(conn):
                check(conn)
        except Exception as e:
            log.warning("qbo round failed: %s", type(e).__name__)
        finally:
            conn.close()
        _LOOP["wake"].wait(poll_seconds())
        _LOOP["wake"].clear()


def start() -> None:
    if _LOOP["started"] or os.environ.get("SSC_QBO_POLL", "1") != "1":
        return
    _LOOP["started"] = True
    threading.Thread(target=_loop, name="qbo-poll", daemon=True).start()


# ------------------------------------------------------------------ status
def status(conn, user: Optional[dict] = None) -> dict:
    reason = unavailable_reason()
    out = {"available": not reason, "reason": reason, "environment": environment(), "connected": False,
           "can_connect": bool(user and user.get("role") == "admin")}
    link = connection(conn)
    if not link:
        return out
    realm = link["realm_id"]
    who = _one(conn.execute("SELECT COALESCE(display_name, full_name) AS n FROM users WHERE id = ?", (link["connected_by"],))) or {}
    clients = _clients(conn, realm)
    month = date.today().isoformat()[:7] + "-01"
    sendable = conn.execute("SELECT COUNT(*) FROM invoice WHERE status = 'issued' OR (status = 'paid' AND qb_realm_id = ?)",
                            (realm,)).fetchone()[0]
    in_qb = conn.execute("SELECT COUNT(*) FROM invoice WHERE qb_invoice_id IS NOT NULL AND qb_realm_id = ? AND status <> 'void'",
                         (realm,)).fetchone()[0]
    back = conn.execute("SELECT COUNT(*) FROM invoice_payment WHERE source = 'quickbooks' AND paid_on >= ?", (month,)).fetchone()[0]
    needs = _rows(conn.execute("SELECT id, number, qb_error FROM invoice WHERE qb_status = 'error' ORDER BY id"))
    waiting = conn.execute("SELECT COUNT(*) FROM invoice WHERE qb_status = 'pending'").fetchone()[0]
    not_sent = conn.execute("SELECT COUNT(*) FROM invoice WHERE status = 'issued' AND qb_invoice_id IS NULL "
                            "AND COALESCE(qb_status, '') NOT IN ('pending', 'error')").fetchone()[0]
    out.update({
        "connected": link["status"] == "active", "status": link["status"], "company_name": link["company_name"],
        "environment": link["environment"], "connected_by": who.get("n"), "connected_at": link["connected_at"],
        "matched": bool(link.get("matched_at")), "last_check_at": link["last_poll_at"], "problem": link["last_error"],
        "every_minutes": poll_seconds() // 60, "open_url": APP_BASE[link["environment"]] + "/app/invoice?txnId=",
        "stats": {"clients_linked": sum(1 for c in clients if c["qb_customer_id"]), "clients_total": len(clients),
                  "invoices_in_qb": in_qb, "invoices_total": sendable, "payments_back_month": back,
                  "reads_today": int(link.get("reads_count") or 0) if link.get("reads_day") == date.today().isoformat() else 0,
                  "read_cap": read_cap(), "waiting": waiting, "not_sent": not_sent},
        "needs_you": [{"id": r["id"], "number": r["number"], "error": r["qb_error"] or "QuickBooks refused it"} for r in needs],
    })
    return out


# ------------------------------------------------------------------ routes
def _uid():
    u = current_user()
    return u["id"] if u else None


def _err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def _back(outcome: str):
    resp = redirect("/company?qb=" + outcome + "#finance")
    resp.set_cookie(STATE_COOKIE, "", max_age=0, httponly=True, samesite="Lax", path="/quickbooks/")
    return resp


@requires_role("admin")
def _connect():
    if unavailable_reason():
        return ("Not Found", 404)
    nonce = secrets.token_urlsafe(24)
    params = {"client_id": _client_id(), "redirect_uri": redirect_uri(), "response_type": "code",
              "scope": SCOPE, "state": nonce}
    resp = redirect(AUTH_URL + "?" + urllib.parse.urlencode(params))
    import auth
    resp.set_cookie(STATE_COOKIE, nonce, max_age=600, httponly=True, secure=auth._request_is_https(), samesite="Lax", path="/quickbooks/")
    return resp


@requires_role("admin")
def _callback():
    if unavailable_reason():
        return ("Not Found", 404)
    state = request.args.get("state") or ""
    cookie = request.cookies.get(STATE_COOKIE) or ""
    if not state or not cookie or not secrets.compare_digest(state, cookie):
        return _back("state")
    realm = (request.args.get("realmId") or "").strip()
    if request.args.get("error") or not request.args.get("code") or not re.fullmatch(r"\d{1,30}", realm):
        return _back("denied")
    code, body = _http("POST", TOKEN_URL, basic=True, data={"grant_type": "authorization_code", "code": request.args["code"],
                                                            "redirect_uri": redirect_uri()})
    if code != 200 or not body.get("refresh_token") or not body.get("access_token"):
        log.info("qbo connect user=%s: token exchange failed (%s)", _uid(), code)
        return _back("failed")
    conn = db_layer.connect()
    try:
        with _LOCK:
            now = _now()
            conn.execute("DELETE FROM qbo_connection")
            conn.execute("INSERT INTO qbo_connection (realm_id, environment, refresh_token_enc, status, connected_by, connected_at) "
                         "VALUES (?, ?, ?, 'active', ?, ?)",
                         (realm, environment(), secret_box.encrypt_text(body["refresh_token"]), _uid(), now))
            conn.commit()
            _TOKENS[realm] = (body["access_token"], time.time() + int(body.get("expires_in") or 3600))
            link = connection(conn)
            try:
                info = _api(conn, link, "GET", f"companyinfo/{realm}").get("CompanyInfo") or {}
                conn.execute("UPDATE qbo_connection SET company_name = ? WHERE id = ?",
                             ((info.get("CompanyName") or "")[:200] or None, link["id"]))
                if conn.execute("SELECT 1 FROM qbo_customer WHERE realm_id = ? LIMIT 1", (realm,)).fetchone():
                    conn.execute("UPDATE qbo_connection SET matched_at = ? WHERE id = ?", (now, link["id"]))   # reconnect: matches kept
                conn.commit()
            except Exception as e:
                conn.rollback()
                log.info("qbo company name skipped: %s", type(e).__name__)
    finally:
        conn.close()
    log.info("qbo connected user=%s", _uid())
    return _back("connected")


def _api_status():
    conn = db_layer.connect()
    try:
        return jsonify({"data": status(conn, current_user())})
    finally:
        conn.close()


def _need_link(conn):
    if unavailable_reason():
        return None, _err("QuickBooks is not switched on yet", 409)
    link = connection(conn)
    if not link:
        return None, _err("connect QuickBooks first", 409)
    if link["status"] != "active":
        return None, _err("QuickBooks needs to be reconnected by an admin", 409)
    return link, None


def _api_clients():
    conn = db_layer.connect()
    try:
        link, bad = _need_link(conn)
        if bad:
            return bad
        try:
            with _LOCK:
                customers = list_customers(conn, link)
        except NeedsReconnect:
            _needs_reconnect(conn, link)
            return _err("QuickBooks needs to be reconnected by an admin", 409)
        except CapReached:
            return _err("today's QuickBooks limit is reached; try again tomorrow", 429)
        except QBError as e:
            return _err(str(e), 502)
        except Exception as e:
            conn.rollback()
            log.warning("qbo customers failed: %s", type(e).__name__)
            return _err("QuickBooks did not answer; try again", 502)
        out = []
        for c in _clients(conn, link["realm_id"]):
            emails = _org_emails(conn, c["id"])
            linked = {"id": c["qb_customer_id"], "name": c["qb_name"], "how": "Linked"} if c["qb_customer_id"] else None
            out.append({"org_id": c["id"], "name": c["name"], "email": emails[0] if emails else None,
                        "linked": linked, "suggestion": linked or suggest(c, emails, customers)})
        open_unsent = conn.execute("SELECT COUNT(*) FROM invoice WHERE status = 'issued' AND qb_invoice_id IS NULL "
                                   "AND COALESCE(qb_status, '') NOT IN ('pending', 'error')").fetchone()[0]
        return jsonify({"data": {"clients": out, "customers": [{"id": c["id"], "name": c["name"]} for c in customers],
                                 "first_time": not link.get("matched_at"), "open_unsent": open_unsent}})
    finally:
        conn.close()


def _api_save_clients():
    body = request.get_json(silent=True) or {}
    matches = body.get("matches")
    if not isinstance(matches, list):
        return _err("matches must be a list")
    conn = db_layer.connect()
    try:
        link, bad = _need_link(conn)
        if bad:
            return bad
        made = linked = 0
        try:
            with _LOCK:
                for m in matches[:2000]:
                    try:
                        org_id = int(m.get("org_id"))
                    except (TypeError, ValueError):
                        return _err("org_id must be an integer")
                    org = _one(conn.execute("SELECT id, name FROM crm_organization WHERE id = ?", (org_id,)))
                    if not org:
                        continue
                    qid = str(m.get("qb_customer_id") or "").strip()
                    if qid:
                        if not re.fullmatch(r"\d{1,20}", qid):
                            return _err("qb_customer_id must be a QuickBooks id")
                        have = _customer_link(conn, link["realm_id"], org_id)
                        if not have or have["qb_customer_id"] != qid:
                            _save_link(conn, link["realm_id"], org_id, qid, str(m.get("qb_name") or "")[:200], "matched")
                        linked += 1
                    elif not _customer_link(conn, link["realm_id"], org_id):
                        c = _create_customer(conn, link, org)
                        _save_link(conn, link["realm_id"], org_id, c["id"], c["name"], "created")
                        made += 1
        except NeedsReconnect:
            _needs_reconnect(conn, link)
            return _err("QuickBooks needs to be reconnected by an admin", 409)
        except CapReached:
            return _err("today's QuickBooks limit is reached; try again tomorrow", 429)
        except QBError as e:
            return _err(str(e), 502)
        except Exception as e:
            conn.rollback()
            log.warning("qbo save clients failed: %s", type(e).__name__)
            return _err("QuickBooks did not answer; try again", 502)
        queued = 0
        if body.get("send_open"):
            ids = [r["id"] for r in conn.execute("SELECT id FROM invoice WHERE status = 'issued' AND qb_invoice_id IS NULL "
                                                 "AND COALESCE(qb_status, '') NOT IN ('pending', 'error')").fetchall()]
            for iid in ids:
                conn.execute("UPDATE invoice SET qb_status = 'pending', qb_error = NULL WHERE id = ?", (iid,))
            conn.commit()
            queued = len(ids)
        conn.execute("UPDATE qbo_connection SET matched_at = ? WHERE id = ?", (_now(), link["id"]))
        conn.commit()
        log.info("qbo clients saved: %s linked, %s created, %s invoice(s) queued", linked, made, queued)
        if queued:
            if SYNC_INLINE:
                check(conn)
            elif _LOOP["started"]:
                _LOOP["wake"].set()
            else:
                threading.Thread(target=_bg_check, daemon=True).start()
        return jsonify({"data": {"linked": linked, "created": made, "queued": queued, **status(conn, current_user())}})
    finally:
        conn.close()


def _bg_check() -> None:
    conn = db_layer.connect()
    try:
        check(conn)
    finally:
        conn.close()


def _api_sync():
    conn = db_layer.connect()
    try:
        link, bad = _need_link(conn)
        if bad:
            return bad
        res = check(conn)
        return jsonify({"data": {**res, **status(conn, current_user())}})
    finally:
        conn.close()


def _api_push(invoice_id):
    import invoices
    conn = db_layer.connect()
    try:
        link, bad = _need_link(conn)
        if bad:
            return bad
        inv = invoices.get(conn, invoice_id)
        if not inv:
            return _err("invoice not found", 404)
        if inv["status"] == "draft":
            return _err("issue the invoice first; it goes to QuickBooks when issued", 409)
        res = push_invoice(conn, invoice_id)
        if res.get("needs_reconnect"):
            return _err("QuickBooks needs to be reconnected by an admin", 409)
        if res.get("capped"):
            return _err("today's QuickBooks limit is reached; it will go tomorrow", 429)
        if res.get("error"):
            return _err(res["error"] if res["error"] != "retry" else "QuickBooks did not answer; it will retry", 502)
        import invoice_docs
        return jsonify({"data": invoice_docs.public(invoices.get(conn, invoice_id))})
    finally:
        conn.close()


def _api_send(invoice_id):
    body = request.get_json(silent=True) or {}
    conn = db_layer.connect()
    try:
        link, bad = _need_link(conn)
        if bad:
            return bad
        try:
            return jsonify({"data": send_from_qb(conn, invoice_id, body.get("to_email"))}), 201
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 409)
    finally:
        conn.close()


@requires_role("admin")
def _disconnect():
    conn = db_layer.connect()
    try:
        link = connection(conn)
        if link and not unavailable_reason():
            try:   # best effort: tell Intuit to forget the sign-in
                _http("POST", REVOKE_URL, basic=True, json_body={"token": secret_box.decrypt_text(link["refresh_token_enc"])})
            except Exception as e:
                log.info("qbo disconnect: revoke skipped (%s)", type(e).__name__)
        if link:
            _TOKENS.pop(link["realm_id"], None)
        conn.execute("DELETE FROM qbo_connection")   # client matches and invoice ids are kept for a reconnect
        conn.execute("UPDATE invoice SET qb_status = NULL WHERE qb_status = 'pending'")
        conn.commit()
        log.info("qbo disconnected user=%s", _uid())
        return jsonify({"data": {"connected": False}})
    finally:
        conn.close()


def register(app) -> None:
    conn = db_layer.connect()
    try:
        ensure(conn)
        db_layer._PKMAP_CACHE.clear()   # tables created at boot after the pk-map cache filled (#290 lesson)
    except Exception as e:
        log.warning("quickbooks schema ensure skipped: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    gate = requires_section(SECTION)
    app.add_url_rule("/quickbooks/connect", "qbo_connect", _connect, methods=["GET"])
    app.add_url_rule("/quickbooks/callback", "qbo_callback", _callback, methods=["GET"])
    app.add_url_rule("/api/quickbooks/status", "qbo_status", gate(_api_status), methods=["GET"])
    app.add_url_rule("/api/quickbooks/clients", "qbo_clients", gate(_api_clients), methods=["GET"])
    app.add_url_rule("/api/quickbooks/clients", "qbo_clients_save", gate(_api_save_clients), methods=["POST"])
    app.add_url_rule("/api/quickbooks/sync", "qbo_sync", gate(_api_sync), methods=["POST"])
    app.add_url_rule("/api/quickbooks/invoices/<int:invoice_id>/push", "qbo_push", gate(_api_push), methods=["POST"])
    app.add_url_rule("/api/quickbooks/invoices/<int:invoice_id>/send", "qbo_send", gate(_api_send), methods=["POST"])
    app.add_url_rule("/api/quickbooks/disconnect", "qbo_disconnect", _disconnect, methods=["POST"])
    if not unavailable_reason():
        start()
