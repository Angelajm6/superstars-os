"""#326 — Invoice documents: branded HTML → PDF, email to the client, a private link,
and the per-project Payments section.

Builds on #315 (`invoices.py` holds the rows and the lifecycle). This module adds
the client-facing half the C-suite asked for: "store AIA payments or standard
invoices and email them straight to the client".

  * `render_document(conn, inv)`  — the branded invoice (same sheet as proposals)
  * `render_pdf(conn, id)`        — HTML → PDF with the app's engine (best effort)
  * `send(conn, id, ...)`         — issues a private link (token, 180 days), emails
                                    the client, records sent_to / sent_at / count
  * public `/i/<token>` + `/i/<token>/pdf` — the client's copy, no login, no-store,
                                    first open recorded as viewed
  * `GET /api/projects/<code>/payments` — the project dashboard's Payments section
                                    (admin / c_suite): the job's invoices, totals,
                                    the client's contacts for the send default

Columns added to `invoice` (idempotent, both backends): sent_at, sent_to,
sent_name, sent_count, viewed_at, view_count, pdf_path, pdf_rendered_at,
token_hash, token_expires_at, last_message, design_doc_id, design_html, design_at, pdf_layout.

The company's invoice layout (2026-11-04, Angela's design): `render_document` draws every invoice that has no design of its
own. Previously Invoiced = earlier invoices on the same proposal, not void, so a later invoice never changes this page.
`LAYOUT` numbers the layout; a stored PDF from an older one is redrawn on its next open (`pdf_file`).

A design made with Claude (2026-11-02): saving a Claude document to the job as an invoice's document
(`claude_docs.save_to_job`) stores its HTML on the invoice (`set_design`); Preview, the PDF and the client's link then
show that design instead of the standard layout, until `DELETE /api/invoices/<id>/design` puts the standard one back. `pdf_path` / `token_hash` never
enter a JSON payload. AIA G702/G703 is #317 — an invoice of kind `progress` is
the row it will fill; nothing here blocks it.
"""
from __future__ import annotations

import hashlib
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
import invoices
import ssc_paths
from auth import current_user, requires_company, requires_section

log = logging.getLogger("ssc.invoice_docs")

COMPANY_NAME = "Superstars Contracting"
LINK_DAYS = 180
KIND_LABEL = {"deposit": "Deposit invoice", "progress": "Progress invoice", "final": "Final invoice",
              "change_order": "Change order invoice", "other": "Invoice"}
EMAIL_SUBJECT = "Invoice {number} from " + COMPANY_NAME
DEFAULT_PAY_INSTRUCTIONS = ("Please remit by check payable to Superstars Contracting Inc., or ask us for ACH details. "
                            "Reference the invoice number on your payment.")

_COLUMNS = (("sent_at", "TEXT"), ("sent_to", "TEXT"), ("sent_name", "TEXT"), ("sent_count", "INTEGER DEFAULT 0"),
            ("viewed_at", "TEXT"), ("view_count", "INTEGER DEFAULT 0"), ("pdf_path", "TEXT"), ("pdf_rendered_at", "TEXT"),
            ("token_hash", "TEXT"), ("token_expires_at", "TEXT"), ("last_message", "TEXT"),
            ("design_doc_id", "INTEGER"), ("design_html", "TEXT"), ("design_at", "TEXT"), ("pdf_layout", "INTEGER DEFAULT 0"))
LAYOUT = 2   # bump when the invoice layout changes: stored PDFs from an older one are redrawn
PRIVATE_KEYS = ("pdf_path", "token_hash", "design_html")


# ---------------------------------------------------------------- helpers
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> date:
    return date.today()


def _conn():
    return db_layer.connect()


def _one(cur) -> Optional[dict]:
    r = cur.fetchone()
    return dict(r) if r else None


def _rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]


def _money(v) -> str:
    try:
        return "${:,.2f}".format(float(v))
    except (TypeError, ValueError):
        return "$0.00"


def _fmt_date(iso) -> str:
    if not iso:
        return "—"
    try:
        d = date.fromisoformat(str(iso)[:10])
        return d.strftime("%B %-d, %Y")
    except ValueError:
        return str(iso)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _base_url() -> str:
    return (os.environ.get("APP_BASE_URL") or "http://127.0.0.1:5050").rstrip("/")


def _client_ip() -> Optional[str]:
    fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return fwd or request.remote_addr


def _has_column(conn, table: str, column: str) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute(
            "SELECT 1 AS x FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
            (table, column)).fetchone())
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def ensure_columns(conn) -> list:
    added = []
    for col, ddl in _COLUMNS:
        if not _has_column(conn, "invoice", col):
            conn.execute(f"ALTER TABLE invoice ADD COLUMN {col} {ddl}")
            added.append(col)
    conn.commit()
    return added


def public(inv: dict) -> dict:
    """Strip the server-only keys before a row leaves the API."""
    d = {k: v for k, v in inv.items() if k not in PRIVATE_KEYS}
    d["has_pdf"] = bool(inv.get("pdf_path"))
    d["has_design"] = bool(inv.get("design_html"))
    d["link_active"] = bool(inv.get("token_hash")) and (inv.get("token_expires_at") or "") >= _now()
    return d


def _company_facts(conn) -> dict:
    out = {}
    for key in ("company_address", "company_phone", "company_email", "company_license", "payment_instructions"):
        try:
            r = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            out[key] = (r["value"] or "").strip() if r else ""
        except Exception:
            out[key] = ""
    return out


def _by_token(conn, token: str) -> Optional[dict]:
    if not token or len(token) < 20 or len(token) > 128:
        return None
    r = _one(conn.execute("SELECT id FROM invoice WHERE token_hash = ?", (_hash(token),)))
    return invoices.get(conn, r["id"]) if r else None


def _link_state(inv: dict) -> str:
    if not inv or not inv.get("token_hash"):
        return "gone"
    if inv.get("status") == "void":
        return "void"
    if (inv.get("token_expires_at") or "") < _now():
        return "expired"
    return "open"


# ---------------------------------------------------------------- document
def render_document(conn, inv: dict) -> str:
    # #317 — an invoice born from an AIA application IS the G702 + G703
    try:
        import aia_billing
        app = aia_billing.for_invoice(conn, inv["id"])
    except Exception as ex:
        log.warning("#317 application lookup failed for invoice %s: %s", inv.get("id"), ex)
        app = None
    if app:
        return aia_billing.render_document(conn, app, inv)
    if inv.get("design_html"):
        return _design_document(conn, inv)
    return _standard_document(conn, inv)


_INV_CSS = """
*{box-sizing:border-box}body{margin:0;background:#FAF7F1;color:#14161C;font:13.5px/1.5 Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-variant-numeric:tabular-nums}
.inv{max-width:820px;margin:0 auto;background:#fff;padding:44px 48px 40px;overflow-wrap:anywhere}
.inv-lh{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;padding-bottom:10px;border-bottom:3px solid #B11E2E}
.inv-lh .brand-logo{display:block;max-height:58px;max-width:240px;object-fit:contain}
.inv-name{font-size:15px;font-weight:800;letter-spacing:.6px;text-transform:uppercase}
.inv-co{text-align:right;color:#5D6068;font-size:12.5px;min-width:0}.inv-co b{display:block;color:#14161C;font-size:13.5px;font-weight:600}
.inv-top{display:grid;grid-template-columns:1fr 1.45fr;gap:20px;align-items:end;margin-top:44px;padding-bottom:12px;border-bottom:1px solid #DCDDE0}
.inv-word{font-size:36px;font-weight:400;letter-spacing:.17em;line-height:1.1;margin:0}
.inv-word::after{content:"";display:block;width:58px;height:3px;background:#B11E2E;margin-top:14px}
.inv-flag{display:inline-block;margin-bottom:10px;padding:2px 9px;border:1.5px solid #B11E2E;border-radius:5px;color:#B11E2E;font-size:11px;font-weight:700;letter-spacing:.14em}
.inv-flag.void{background:#B11E2E;color:#fff}
.inv-meta{display:grid;grid-template-columns:auto 1fr;margin:0;min-width:0}.inv-meta>*{padding:4px 0;border-bottom:1px solid #DCDDE0}
.inv-meta dt{color:#8D9096;font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;padding-right:18px;text-align:right}
.inv-meta dd{margin:0;text-align:right;font-size:12.5px}.inv-meta dd.no{font-weight:700}.inv-meta dd.due{font-weight:700;color:#B11E2E}
.inv-parties{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid #DCDDE0}.inv-parties>div{padding:24px 0 12px;min-width:0}
.inv-parties>div+div{padding-left:22px;border-left:1px solid #DCDDE0}
.inv-label{font-size:10.5px;font-weight:700;letter-spacing:.2em;text-transform:uppercase;color:#B11E2E;margin:0 0 8px}
.inv-parties p.t{margin:0;color:#5D6068}.inv-parties p.s{margin:0 0 6px;color:#14161C;font-weight:600;font-size:14.5px}
.inv-work{margin-top:30px}.inv-work .hd{display:flex;justify-content:space-between;gap:16px;padding:0 10px 8px;border-bottom:2.5px solid #14161C;color:#8D9096;font-size:10.5px;letter-spacing:.18em;text-transform:uppercase}
.inv-work .ln{display:flex;justify-content:space-between;gap:28px;padding:16px 10px;border-bottom:1px solid #DCDDE0}
.inv-work .ln span:first-child{max-width:78%;min-width:0}.inv-work .ln span:last-child{white-space:nowrap}
.inv-tot{margin-top:18px}.inv-tot .r{display:flex;justify-content:space-between;gap:16px;padding:8px 10px;border-bottom:1px solid #DCDDE0;font-size:14.5px}
.inv-tot .r span:first-child{color:#5D6068;min-width:0}.inv-tot .r span:last-child{white-space:nowrap}
.inv-tot .r.bar{background:#B11E2E;border-bottom:0;-webkit-print-color-adjust:exact;print-color-adjust:exact}.inv-tot .r.bar span{color:#fff}
.inv-tot .r.bar.paid{background:#2E7D4F}
.inv-pay{margin-top:32px}.inv-pay .inv-label{padding:0 10px;margin-bottom:6px}
.inv-cols{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;border-top:2.5px solid #14161C;border-bottom:1px solid #DCDDE0}
.inv-cols>div{padding:14px 10px;min-width:0}.inv-cols>div+div{border-left:1px solid #DCDDE0}
.inv-cols small{display:block;color:#8D9096;font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;margin-bottom:5px}
.inv-cols b{display:block;font-weight:600;font-size:14px}.inv-cols em{display:block;font-style:normal;color:#8D9096;font-size:12px;margin-top:5px}
.inv-more{margin:12px 10px 0;color:#5D6068;font-size:12.5px;white-space:pre-line}
.inv-notes{margin-top:24px;break-inside:avoid}.inv-notes .inv-label{padding:0 10px}
.inv-thanks{margin-top:34px;padding-top:20px;border-top:1px solid #DCDDE0;text-align:center;break-inside:avoid}
.inv-thanks h2{margin:0;font-size:21px;font-weight:400;letter-spacing:.05em}
.inv-thanks h2::after{content:"";display:block;width:42px;height:3px;background:#B11E2E;margin:10px auto 12px}
.inv-thanks p{margin:0;color:#5D6068;font-size:13px}.inv-thanks p+p{color:#8D9096;font-size:12px;margin-top:6px}
@media (max-width:640px){
  .inv{padding:24px 18px 26px}.inv-lh{align-items:center}.inv-lh .brand-logo{max-height:44px;max-width:130px}.inv-co{font-size:11.5px}
  .inv-top{grid-template-columns:1fr;margin-top:26px;gap:22px}.inv-word{font-size:30px}
  .inv-parties{grid-template-columns:1fr}.inv-parties>div+div{padding-left:0;border-left:0;border-top:1px solid #DCDDE0}
  .inv-work .ln span:first-child{max-width:none}
  .inv-cols{grid-auto-flow:row}.inv-cols>div+div{border-left:0;border-top:1px solid #DCDDE0}
}
@page{size:letter;margin:.4in .5in}
@media print{body{background:#fff}.inv{max-width:none;padding:0}.inv-top{margin-top:24px}.inv-parties>div{padding-top:18px}.inv-work{margin-top:22px}.inv-tot{margin-top:12px}.inv-pay{margin-top:22px}.inv-cols>div{padding:10px}.inv-thanks{margin-top:20px;padding-top:14px}.inv-word::after,.inv-thanks h2::after,.inv-lh{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""


def _city_line(*parts) -> str:
    city, state, zip_code = (str(x or "").strip() for x in parts)
    return ", ".join(x for x in (city, " ".join(y for y in (state, zip_code) if y)) if x)


def _two_lines(address: str) -> list:
    """A one-line address on two lines: "356 Wythe Ave, Suite 300, Brooklyn, NY 11249" -> street / city, state zip."""
    bits = [x.strip() for x in (address or "").split(",") if x.strip()]
    return [", ".join(bits[:-2]), ", ".join(bits[-2:])] if len(bits) >= 3 else ([address] if address else [])


def _contract(conn, inv: dict) -> Optional[dict]:
    """The signed proposal behind this invoice: its label, total, title and place, and what was invoiced before this one."""
    if not inv.get("proposal_id"):
        return None
    try:
        import proposal_engine
        import proposals
        p = proposals.get(conn, inv["proposal_id"])
        if not p:
            return None
        total = float(proposals.amounts(conn, p).get("total") or 0)
        prev = _one(conn.execute("SELECT COALESCE(SUM(amount), 0) AS v FROM invoice WHERE proposal_id = ? AND id < ? AND status <> 'void'",
                                 (inv["proposal_id"], inv["id"])))["v"]
    except Exception as ex:   # a document never fails over its contract rows
        log.warning("contract rows skipped for invoice %s: %s", inv.get("id"), type(ex).__name__)
        conn.rollback()
        return None
    return {"label": proposal_engine.label(p), "total": total, "prev": round(float(prev or 0), 2), "title": p.get("title"),
            "signed_at": p.get("signed_at"), "street": p.get("building_address_line") or p.get("building_address"),
            "city": _city_line(p.get("building_city"), p.get("building_state"), p.get("building_zip")) if p.get("building_address_line") else "",
            "attn": p.get("recipient_name") or p.get("contact_name")}


def _bill_to(conn, inv: dict, contract: Optional[dict]) -> list:
    """Client name, Attn, address (street / city, state zip), each only when known."""
    out = [inv.get("client_name") or ""]
    attn = inv.get("sent_name") or (contract or {}).get("attn")
    org = None
    if inv.get("client_org_id"):
        try:
            org = crm.get_org(conn, inv["client_org_id"])
            attn = attn or default_recipient(conn, inv).get("name")
        except Exception as ex:
            log.info("client details skipped for invoice %s: %s", inv.get("id"), type(ex).__name__)
            conn.rollback()
    if attn:
        out.append(f"Attn: {attn}")
    if org:
        out += [x for x in (org.get("address_line"), _city_line(org.get("city"), org.get("state"), org.get("zip"))) if x]
    return out


def _project(conn, inv: dict, contract: Optional[dict]) -> tuple:
    """(title, address lines, what the thank-you calls the place)."""
    title = (contract or {}).get("title") or inv.get("project_name") or inv.get("project_code") or "—"
    street, city = (contract or {}).get("street"), (contract or {}).get("city")
    if not street:
        street = inv.get("building_address")
    if not street and inv.get("project_code"):
        try:
            r = _one(conn.execute("SELECT address FROM projects WHERE project_code = ?", (inv["project_code"],)))
            street = (r or {}).get("address")
        except Exception:
            conn.rollback()
    return title, [x for x in (street, city) if x], street or inv.get("project_name") or "this project"


def _due_text(inv: dict) -> str:
    """Deposits are due on receipt (the stored week only keeps "overdue" honest); a date on or before the issue date reads the same."""
    due, issued = inv.get("due_at"), inv.get("issued_at")
    if inv.get("kind") == "deposit" and (not due or (issued and due == (date.fromisoformat(str(issued)[:10]) + timedelta(days=invoices.DEPOSIT_NET_DAYS)).isoformat())):
        return "Due Upon Receipt"
    if not due:
        return "Set when issued"
    return "Due Upon Receipt" if issued and str(due)[:10] <= str(issued)[:10] else _fmt_date(due)


def _standard_document(conn, inv: dict) -> str:
    """The company's invoice: every invoice without a design of its own, in every situation (Angela's layout, 2026-11-04)."""
    e = _html.escape
    facts = _company_facts(conn)
    status = inv.get("status")
    number = e(inv["number"])
    contract = _contract(conn, inv)
    if contract and contract["total"] <= 0:
        contract = None
    bill = _bill_to(conn, inv, contract)
    title, where, place = _project(conn, inv, contract)
    uri = company_brand.logo_data_uri(conn)
    mark = (f'<img class="brand-logo" alt="{COMPANY_NAME}" src="{uri}">' if uri else f'<div class="inv-name">{COMPANY_NAME}</div>')
    lines = "".join("<br>" + e(x) for x in company_brand.letterhead_lines(facts))
    flag = {"draft": '<span class="inv-flag">DRAFT</span>', "void": '<span class="inv-flag void">VOID</span>'}.get(status, "")
    meta = f'<dt>Invoice no.</dt><dd class="no">{number}</dd><dt>Date</dt><dd>{e(_fmt_date(inv.get("issued_at")) if inv.get("issued_at") else "Not issued yet")}</dd>'
    if status not in ("paid", "void"):
        meta += f'<dt>Due</dt><dd class="due">{e(_due_text(inv))}</dd>'
    if contract:
        signed = f', signed {_fmt_date(contract["signed_at"])}' if contract.get("signed_at") else ""
        meta += f'<dt>Contract</dt><dd>Proposal No. {e(contract["label"])}{e(signed)}</dd>'
    rows = ""
    if contract:
        rows += (f'<div class="r"><span>Full Contract Balance (Proposal No. {e(contract["label"])})</span><span>{_money(contract["total"])}</span></div>'
                 f'<div class="r"><span>Previously Invoiced</span><span>{_money(contract["prev"])}</span></div>')
    if inv.get("retainage"):
        rows += f'<div class="r"><span>Retainage Held</span><span>{_money(inv["retainage"])}</span></div>'
    if status == "paid":
        rows += f'<div class="r bar paid"><span>Paid in full on {e(_fmt_date(inv.get("paid_at")))}</span><span>{_money(0)}</span></div>'
    elif status == "void":
        rows += f'<div class="r"><span>Amount Due</span><span>{_money(0)}</span></div>'
    else:
        if float(inv.get("amount_paid") or 0) > 0:
            rows += f'<div class="r"><span>Paid to Date</span><span>−{_money(inv["amount_paid"])}</span></div>'
        rows += f'<div class="r bar"><span>Amount Due</span><span>{_money(inv.get("balance"))}</span></div>'
    if contract and status != "void":
        rest = round(contract["total"] - contract["prev"] - float(inv.get("amount") or 0), 2)
        rows += f'<div class="r"><span>Remaining Balance After This Invoice</span><span>{"−" if rest < 0 else ""}{_money(abs(rest))}</span></div>'
    pay = ""
    if status not in ("paid", "void"):
        mail = _two_lines(facts["company_address"])
        mail_col = f'<div><small>Mail to</small><b>{"<br>".join(e(x) for x in mail)}</b></div>' if mail else ""
        more = f'<p class="inv-more">{e(facts["payment_instructions"])}</p>' if facts.get("payment_instructions") else ""
        pay = (f'<div class="inv-pay"><p class="inv-label">Payment</p><div class="inv-cols">'
               f'<div><small>Method</small><b>Check</b><em>Note invoice no. {number} on the memo line</em></div>'
               f'<div><small>Payable to</small><b>Superstars Contracting Inc.</b></div>{mail_col}</div>{more}</div>')
    notes = (f'<div class="inv-notes"><p class="inv-label">Notes</p><p class="inv-more" style="margin-top:0">{e(inv["notes"])}</p></div>'
             if inv.get("notes") and status != "void" else "")
    thanks = "" if status == "void" else (
        f'<div class="inv-thanks"><h2>Thank you.</h2><p>We appreciate your business and the opportunity to work on {e(place)}.</p>'
        + (f'<p>Questions? {e(facts["company_phone"])}</p>' if facts.get("company_phone") else "") + "</div>")
    desc = inv.get("description") or KIND_LABEL.get(inv.get("kind"), "Invoice")
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Invoice {number} — {COMPANY_NAME}</title><style>{_INV_CSS}</style></head><body>
<div class="inv">
  <div class="inv-lh">{mark}<div class="inv-co"><b>{COMPANY_NAME}</b>{lines[4:] if lines else ''}</div></div>
  <div class="inv-top"><div>{flag}<h1 class="inv-word">INVOICE</h1></div><dl class="inv-meta">{meta}</dl></div>
  <div class="inv-parties"><div><p class="inv-label">Bill to</p><p class="s">{e(bill[0])}</p><p class="t">{"<br>".join(e(x) for x in bill[1:])}</p></div>
    <div><p class="inv-label">Project</p><p class="s">{e(title)}</p><p class="t">{"<br>".join(e(x) for x in where)}</p></div></div>
  <div class="inv-work"><div class="hd"><span>Description of work billed</span><span>Amount</span></div>
    <div class="ln"><span>{e(desc)}</span><span>{_money(inv.get("amount"))}</span></div></div>
  <div class="inv-tot">{rows}</div>
  {pay}{notes}{thanks}
</div></body></html>"""


def _design_document(conn, inv: dict) -> str:
    """The invoice as designed with Claude: the company letterhead over the design, plus the status stamp."""
    import claude_docs
    e = _html.escape
    status = inv.get("status")
    stamp = {"paid": f"Paid in full on {_fmt_date(inv.get('paid_at'))} — thank you.",
             "void": "Void — this invoice was cancelled.", "draft": "Draft — not yet issued."}.get(status)
    stamp_html = (f"<p style='margin-top:18px;padding:8px 12px;border:1.5px solid {'#2E7D4F' if status == 'paid' else '#A8382B'};"
                  f"border-radius:8px;font-weight:600'>{e(stamp)}</p>") if stamp else ""
    return claude_docs.page({"title": f"Invoice {inv['number']} — {COMPANY_NAME}", "html": inv["design_html"] + stamp_html})


def amount_mismatch(inv: dict, html: str) -> Optional[str]:
    """The design's text should carry the invoice's amount; a design that doesn't is flagged, never blocked."""
    text = re.sub(r"\s+", " ", _html.unescape(re.sub(r"(?s)<[^>]+>", " ", html or "")))
    m = _money(inv.get("amount"))
    if m in text or m.replace(".00", "") in text:
        return None
    return f"The design doesn't show {m}, the amount on invoice {inv['number']}. Check the numbers before sending it."


def set_design(conn, invoice_id: int, doc_id: Optional[int], html: Optional[str], *, render: bool = True) -> dict:
    """Use a Claude document as this invoice's document (html=None puts the standard layout back). The stored PDF is
    redrawn so the PDF button and the client's link match; if the PDF engine is down it is drawn on the next open."""
    inv = invoices.get(conn, invoice_id)
    if not inv:
        raise LookupError("invoice not found")
    if html is not None:
        if inv.get("status") == "void":
            raise ValueError(f"invoice {inv['number']} is void")
        try:
            import aia_billing
            if aia_billing.for_invoice(conn, invoice_id):
                raise ValueError(f"invoice {inv['number']} is an AIA pay application; its G702 / G703 is the document")
        except ImportError:
            pass
    conn.execute("UPDATE invoice SET design_doc_id = ?, design_html = ?, design_at = ?, pdf_path = NULL, "
                 "pdf_rendered_at = NULL WHERE id = ?", (doc_id if html is not None else None, html,
                                                         _now() if html is not None else None, invoice_id))
    conn.commit()
    if render and (inv.get("pdf_path") or inv.get("token_hash")):
        render_pdf(conn, invoice_id)
    return {"id": invoice_id, "number": inv["number"],
            "warning": amount_mismatch(inv, html) if html is not None else None}


def _pdf_dir() -> Path:
    return ssc_paths.under_root("data_room", "invoices")


def render_pdf(conn, invoice_id: int, *, actor=None) -> dict:
    inv = invoices.get(conn, invoice_id)
    if not inv:
        return {"ok": False, "error": "invoice not found"}
    try:
        import pdf_export
        d = _pdf_dir()
        d.mkdir(parents=True, exist_ok=True)
        html_path = d / f"{inv['number']}.html"
        pdf_path = d / f"{inv['number']}.pdf"
        html_path.write_text(render_document(conn, inv), encoding="utf-8")
        res = pdf_export.render_html_to_pdf(html_path, pdf_path, timeout_sec=90)
        try:
            html_path.unlink(missing_ok=True)
        except Exception:
            pass
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "render failed"}
        conn.execute("UPDATE invoice SET pdf_path = ?, pdf_rendered_at = ?, pdf_layout = ? WHERE id = ?",
                     (ssc_paths.store_rel(pdf_path), _now(), LAYOUT, invoice_id))
        conn.commit()
        return {"ok": True, "size": pdf_path.stat().st_size, "engine": res.get("engine")}
    except Exception as e:   # missing browser, timeout, disk — the link is the reference copy
        log.warning("#326 pdf not rendered for invoice %s: %s", invoice_id, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def pdf_file(conn, invoice_id: int) -> Optional[Path]:
    r = _one(conn.execute("SELECT pdf_path, pdf_layout FROM invoice WHERE id = ?", (invoice_id,)))
    if not r or not r.get("pdf_path") or int(r.get("pdf_layout") or 0) < LAYOUT:   # older layout: the caller redraws it
        return None
    p = ssc_paths.resolve_data_path(r["pdf_path"])
    if not (p.resolve().is_relative_to(_pdf_dir().resolve()) and p.exists()):
        return None
    return p


# ---------------------------------------------------------------- email
_EMAIL_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{subject}</title></head>
<body style="margin:0;padding:0;background:#FAF7F1;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#FAF7F1;"><tr><td align="center" style="padding:32px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:#FFFFFF;border:1px solid #E8E4DD;border-radius:16px;overflow:hidden;">
<tr><td style="height:6px;background:#B11E2E;font-size:0;line-height:0;">&nbsp;</td></tr>
<tr><td style="padding:28px 36px 0 36px;font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<div style="font-size:15px;font-weight:700;letter-spacing:0.6px;text-transform:uppercase;color:#14161C;">Superstars Contracting</div>
<div style="font-size:10px;font-weight:600;letter-spacing:1.2px;text-transform:uppercase;color:#76777E;margin-top:3px;">{kind} {number}</div></td></tr>
<tr><td style="padding:26px 36px 0 36px;font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<h1 style="margin:0 0 10px 0;font-size:22px;line-height:1.25;font-weight:700;color:#14161C;">{title}</h1>
<p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:#384152;">{greeting}</p>
<p style="margin:0;font-size:15px;line-height:1.55;color:#384152;white-space:pre-wrap;">{message}</p>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-top:18px;font-family:inherit;font-size:14px;color:#384152;">
<tr><td style="padding:4px 18px 4px 0;color:#76777E;">Project</td><td>{project}</td></tr>
<tr><td style="padding:4px 18px 4px 0;color:#76777E;">Amount due</td><td><strong>{balance}</strong></td></tr>
<tr><td style="padding:4px 18px 4px 0;color:#76777E;">Due date</td><td>{due}</td></tr></table></td></tr>
<tr><td align="left" style="padding:24px 36px 0 36px;"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr><td align="center" style="background:#24418C;border-radius:9px;">
<a href="{url}" style="display:inline-block;padding:13px 26px;font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:14px;font-weight:700;letter-spacing:0.3px;color:#FFFFFF;text-decoration:none;border-radius:9px;">View the invoice</a></td></tr></table></td></tr>
<tr><td style="padding:18px 36px 30px 36px;font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<p style="margin:0;font-size:13px;line-height:1.5;color:#76777E;">The link opens the invoice with payment instructions and a PDF to download. It is personal to this invoice. If the button does not work, copy this address into your browser:<br><span style="word-break:break-all;color:#14161C;">{url}</span></p></td></tr>
</table></td></tr></table></body></html>"""


def render_email(inv: dict, url: str, message: str, to_name: Optional[str]) -> tuple:
    greeting = f"Hello {to_name.split(' ')[0]}," if to_name else "Hello,"
    where = inv.get("building_address") or inv.get("project_name") or inv.get("project_code") or "your project"
    kind = KIND_LABEL.get(inv.get("kind"), "Invoice")
    msg = (message or "").strip() or (
        f"Please find our {kind.lower()} for {where}. Open the link to view it, download the PDF, and see how to pay.")
    subject = EMAIL_SUBJECT.format(number=inv["number"])
    text = (f"{greeting}\n\n{msg}\n\n{kind} {inv['number']} — {inv.get('description') or ''}\n"
            f"Amount due: {_money(inv.get('balance'))}\nDue: {_fmt_date(inv.get('due_at'))}\n\n"
            f"View it here: {url}\n\n— {COMPANY_NAME}")
    html = _EMAIL_HTML.format(
        subject=_html.escape(subject), kind=_html.escape(kind), number=_html.escape(inv["number"]),
        title=_html.escape(inv.get("description") or kind), greeting=_html.escape(greeting),
        message=_html.escape(msg), project=_html.escape(where), balance=_html.escape(_money(inv.get("balance"))),
        due=_html.escape(_fmt_date(inv.get("due_at"))), url=_html.escape(url, quote=True))
    return subject, text, html


def _deliver(to_email: str, subject: str, text: str, html: str) -> tuple:
    fake_dir = (runtime_seams.seam("SSC_MAIL_FAKE_DIR") or runtime_seams.seam("SSC_RESET_MAIL_FAKE_DIR")).strip()
    if fake_dir:
        out = Path(fake_dir)
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        (out / f"invoice-{stamp}.eml").write_text(f"To: {to_email}\nSubject: {subject}\n\n{text}", encoding="utf-8")
        (out / f"invoice-{stamp}.html").write_text(html, encoding="utf-8")
        return True, None
    if (os.environ.get("SENDGRID_API_KEY") or "").strip():
        import notifications
        sent, err = notifications._send_live(to_email, subject, text, html, from_name=COMPANY_NAME, track_clicks=False)
        return bool(sent), err
    return False, "email is not configured (SENDGRID_API_KEY)"


def default_recipient(conn, inv: dict) -> dict:
    """The client's first contact with an email (a proposal's recipient wins)."""
    if inv.get("proposal_id"):
        p = _one(conn.execute("SELECT recipient_email, recipient_name FROM proposal WHERE id = ?", (inv["proposal_id"],)))
        if p and p.get("recipient_email"):
            return {"email": p["recipient_email"], "name": p.get("recipient_name")}
    c = _one(conn.execute("SELECT full_name, email FROM crm_contact WHERE org_id = ? AND email IS NOT NULL AND email <> '' "
                          "ORDER BY id LIMIT 1", (inv["client_org_id"],)))
    return {"email": c["email"], "name": c.get("full_name")} if c else {"email": None, "name": None}


def send(conn, invoice_id: int, *, to_email=None, to_name=None, message=None, actor=None) -> dict:
    inv = invoices.get(conn, invoice_id)
    if not inv:
        raise LookupError("invoice not found")
    if inv["status"] == "draft":
        raise ValueError("issue the invoice before sending it")
    if inv["status"] == "void":
        raise ValueError("a void invoice cannot be sent")
    fallback = default_recipient(conn, inv)
    to_email = (to_email or inv.get("sent_to") or fallback["email"] or "").strip().lower()
    if "@" not in to_email or "." not in to_email.split("@")[-1]:
        raise ValueError("a valid recipient email is required")
    to_name = (to_name or inv.get("sent_name") or fallback["name"] or "").strip()[:120] or None
    message = (message or "").strip()[:2000] or None
    now = _now()
    expires = (datetime.now() + timedelta(days=LINK_DAYS)).isoformat(timespec="seconds")
    # the token is never stored, only its digest: every send mints a fresh link and the
    # previous one stops working — the latest email is the client's source of truth
    token = secrets.token_urlsafe(32)
    conn.execute("UPDATE invoice SET token_hash = ?, token_expires_at = ? WHERE id = ?", (_hash(token), expires, invoice_id))
    conn.commit()
    pdf = render_pdf(conn, invoice_id, actor=actor)
    inv = invoices.get(conn, invoice_id)
    url = f"{_base_url()}/i/{token}"
    subject, text, html = render_email(inv, url, message, to_name)
    sent, err = _deliver(to_email, subject, text, html)
    if not sent:
        raise RuntimeError(f"email not sent: {err}")
    conn.execute("UPDATE invoice SET sent_at = ?, sent_to = ?, sent_name = ?, sent_count = COALESCE(sent_count, 0) + 1, "
                 "last_message = ?, updated_at = ? WHERE id = ?", (now, to_email, to_name, message, now, invoice_id))
    conn.commit()
    try:
        crm.add_activity(conn, entity_type="organization", entity_id=inv["client_org_id"], activity_type="system",
                         author_user_id=actor, function_tag="finance",
                         summary=f"Invoice {inv['number']} emailed to {to_name or to_email}"
                                 + ("" if pdf.get("ok") else " (PDF not rendered — the link is the copy)"))
    except Exception as e:
        log.info("#326 activity not logged: %s", e)
    return public(invoices.get(conn, invoice_id))


def record_view(conn, inv: dict) -> None:
    conn.execute("UPDATE invoice SET viewed_at = COALESCE(viewed_at, ?), view_count = COALESCE(view_count, 0) + 1 WHERE id = ?",
                 (_now(), inv["id"]))
    conn.commit()


# ---------------------------------------------------------------- project section
def project_payments(conn, project_code: str, *, today: Optional[date] = None) -> Optional[dict]:
    today = today or _today()
    p = _one(conn.execute("SELECT project_code, name, address, status, client_org_id FROM projects WHERE project_code = ?",
                          (project_code,)))
    if not p:
        return None
    rows = [dict(public(r), edit=invoices.edit_rules(conn, r))
            for r in invoices.list_invoices(conn, project_code=project_code, today=today)]
    open_rows = [r for r in rows if r["status"] == "issued"]
    summary = {
        "invoiced_total": round(sum(r["amount"] for r in rows if r["status"] in ("issued", "paid")), 2),
        "open_value": round(sum(r["balance"] for r in open_rows), 2), "open_count": len(open_rows),
        "overdue_value": round(sum(r["balance"] for r in open_rows if r["overdue"]), 2),
        "overdue_count": sum(1 for r in open_rows if r["overdue"]),
        "collected": round(sum(r["amount_paid"] for r in rows if r["status"] in ("issued", "paid")), 2),
        "draft_count": sum(1 for r in rows if r["status"] == "draft"),
    }
    client = crm.get_org(conn, p["client_org_id"]) if p.get("client_org_id") else None
    contacts = _rows(conn.execute("SELECT id, full_name, email FROM crm_contact WHERE org_id = ? AND email IS NOT NULL "
                                  "AND email <> '' ORDER BY full_name", (p["client_org_id"],))) if p.get("client_org_id") else []
    return {"project": p, "client": ({"id": client["id"], "name": client["name"]} if client else None),
            "contacts": contacts, "summary": summary, "invoices": rows,
            "kinds": [{"code": k, "label": v} for k, v in KIND_LABEL.items()]}


# ---------------------------------------------------------------- public page
def _gone(msg: str, code: int = 404):
    body = (f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{COMPANY_NAME}</title><style>body{{margin:0;background:#FAF7F1;font:15px/1.6 Inter,-apple-system,sans-serif;color:#14161C}}"
            f".c{{max-width:520px;margin:18vh auto;background:#fff;border:1px solid #E8E4DD;border-radius:16px;padding:28px 32px}}</style></head>"
            f"<body><div class='c'><b>{COMPANY_NAME}</b><p>{_html.escape(msg)}</p></div></body></html>")
    resp = make_response(body, code)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def _page(conn, inv: dict, token: str) -> str:
    from proposals import _PAGE_UI
    e = _html.escape
    doc = render_document(conn, inv)
    top = (f"<div class='top'><span class='note'>Invoice {e(inv['number'])} · {COMPANY_NAME}</span>"
           f"<a href='/i/{e(token, quote=True)}/pdf'>Download PDF</a></div>")
    if inv.get("status") == "paid":
        ui = "<div class='act'><h3>Paid — thank you.</h3><p>Keep this link to download the PDF at any time.</p></div>"
    else:
        ui = (f"<div class='act'><h3>Questions about this invoice?</h3><p>Reply to the email you received and we will get right back to you. "
              f"The amount due is {e(_money(inv.get('balance')))}, due {e(_fmt_date(inv.get('due_at')))}.</p></div>")
    return doc.replace("</head>", _PAGE_UI + "</head>").replace("<body>", "<body>" + top).replace("</body>", ui + "</body>")


def _public_page(token):
    conn = _conn()
    try:
        inv = _by_token(conn, token)
        state = _link_state(inv)
        if state == "gone":
            return _gone("This invoice link is not valid.")
        if state == "void":
            return _gone("This invoice was cancelled. If you were expecting one, reply to the email you received.", 410)
        if state == "expired":
            return _gone("This invoice link has expired. Reply to the email you received and we will send a fresh one.", 410)
        record_view(conn, inv)
        inv = invoices.get(conn, inv["id"])
        return _no_store(make_response(_page(conn, inv, token), 200))
    finally:
        conn.close()


def _public_pdf(token):
    conn = _conn()
    try:
        inv = _by_token(conn, token)
        if _link_state(inv) != "open":
            return _gone("This invoice link is not valid.")
        p = pdf_file(conn, inv["id"])
        if not p and render_pdf(conn, inv["id"]).get("ok"):   # a new design clears the stored PDF; draw it now
            p = pdf_file(conn, inv["id"])
        if not p:
            return _gone("The PDF is not available right now — the invoice page is the reference copy.", 503)
        return _no_store(make_response(send_file(str(p), mimetype="application/pdf", as_attachment=True,
                                                 download_name=f"{inv['number']}.pdf")))
    finally:
        conn.close()


# ---------------------------------------------------------------- routes
def _err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def _uid():
    u = current_user()
    return u["id"] if u else None


def _api_preview(invoice_id):
    conn = _conn()
    try:
        inv = invoices.get(conn, invoice_id)
        if not inv:
            return _err("invoice not found", 404)
        resp = make_response(render_document(conn, inv), 200)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    finally:
        conn.close()


def _api_pdf(invoice_id):
    conn = _conn()
    try:
        inv = invoices.get(conn, invoice_id)
        if not inv:
            return _err("invoice not found", 404)
        p = pdf_file(conn, invoice_id)
        if not p:
            res = render_pdf(conn, invoice_id, actor=_uid())
            p = pdf_file(conn, invoice_id) if res.get("ok") else None
        if not p:
            return _err("the PDF engine is not available — use Preview", 503)
        resp = make_response(send_file(str(p), mimetype="application/pdf", as_attachment=False,
                                       download_name=f"{inv['number']}.pdf"))
        resp.headers["Cache-Control"] = "no-store"
        return resp
    finally:
        conn.close()


def _api_send(invoice_id):
    body = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        try:
            return jsonify({"data": send(conn, invoice_id, to_email=body.get("to_email"), to_name=body.get("to_name"),
                                         message=body.get("message"), actor=_uid())}), 201
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 409 if "issue" in str(e) or "void" in str(e) else 400)
        except RuntimeError as e:
            return _err(str(e), 502)
    finally:
        conn.close()


def _api_recipient(invoice_id):
    conn = _conn()
    try:
        inv = invoices.get(conn, invoice_id)
        if not inv:
            return _err("invoice not found", 404)
        return jsonify({"data": default_recipient(conn, inv)})
    finally:
        conn.close()


def _api_design_reset(invoice_id):
    conn = _conn()
    try:
        return jsonify({"data": set_design(conn, invoice_id, None, None)})
    except LookupError as e:
        return _err(str(e), 404)
    finally:
        conn.close()


@requires_company
def _api_project_payments(project_code):
    conn = _conn()
    try:
        d = project_payments(conn, project_code)
        return jsonify({"data": d}) if d else _err("project not found", 404)
    finally:
        conn.close()


def register(app) -> None:
    conn = _conn()
    try:
        ensure_columns(conn)
    except Exception as e:   # a fresh preprod DB may migrate later; never block boot
        log.warning("#326 invoice document columns ensure skipped: %s", e)
    finally:
        conn.close()
    gate = requires_section(invoices.SECTION)
    app.add_url_rule("/api/invoices/<int:invoice_id>/preview", "invoices_preview", gate(_api_preview), methods=["GET"])
    app.add_url_rule("/api/invoices/<int:invoice_id>/pdf", "invoices_pdf", gate(_api_pdf), methods=["GET"])
    app.add_url_rule("/api/invoices/<int:invoice_id>/send", "invoices_send", gate(_api_send), methods=["POST"])
    app.add_url_rule("/api/invoices/<int:invoice_id>/design", "invoices_design_reset", gate(_api_design_reset),
                     methods=["DELETE"])
    app.add_url_rule("/api/invoices/<int:invoice_id>/recipient", "invoices_recipient", gate(_api_recipient), methods=["GET"])
    app.add_url_rule("/api/projects/<project_code>/payments", "project_payments", _api_project_payments, methods=["GET"])
    app.add_url_rule("/i/<token>", "invoice_public_page", _public_page, methods=["GET"])
    app.add_url_rule("/i/<token>/pdf", "invoice_public_pdf", _public_pdf, methods=["GET"])
