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
token_hash, token_expires_at, last_message. `pdf_path` / `token_hash` never
enter a JSON payload. AIA G702/G703 is #317 — an invoice of kind `progress` is
the row it will fill; nothing here blocks it.
"""
from __future__ import annotations

import hashlib
import html as _html
import logging
import os

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
            ("token_hash", "TEXT"), ("token_expires_at", "TEXT"), ("last_message", "TEXT"))
PRIVATE_KEYS = ("pdf_path", "token_hash")


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
    from proposals import _DOC_CSS
    e = _html.escape
    facts = _company_facts(conn)
    status = inv.get("status")
    kind = KIND_LABEL.get(inv.get("kind"), "Invoice")
    stamp = ""
    if status == "paid":
        stamp = f"<div class='sig keep'><div class='stamp'>Paid in full on {e(_fmt_date(inv.get('paid_at')))} — thank you.</div></div>"
    elif status == "void":
        stamp = "<div class='sig keep'><div class='stamp bad'>Void — this invoice was cancelled.</div></div>"
    elif status == "draft":
        stamp = "<div class='sig keep'><div class='stamp bad'>Draft — not yet issued.</div></div>"
    pay_rows = ""
    if inv.get("payments"):
        pay_rows = "".join(
            f"<tr><td>{e(_fmt_date(p.get('paid_on')))}</td><td>{e((p.get('method') or '').upper())}</td>"
            f"<td>{e(p.get('reference') or '')}</td><td class='num'>{_money(p.get('amount'))}</td></tr>" for p in inv["payments"])
        pay_rows = (f"<h2>Payments received</h2><table><thead><tr><th>Received</th><th>Method</th><th>Reference</th>"
                    f"<th class='num'>Amount</th></tr></thead><tbody>{pay_rows}</tbody></table>")
    retain = f"<tr><td>Retainage held</td><td class='num'>{_money(inv.get('retainage'))}</td></tr>" if inv.get("retainage") else ""
    balance_label = "Balance due" if status != "paid" else "Balance"
    instructions = facts.get("payment_instructions") or DEFAULT_PAY_INSTRUCTIONS
    letter = " · ".join(x for x in (facts["company_address"], facts["company_phone"], facts["company_email"]) if x)
    lic = f" · License {e(facts['company_license'])}" if facts["company_license"] else ""
    where = inv.get("building_address") or inv.get("project_name") or inv.get("project_code") or ""
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Invoice {e(inv['number'])} — {COMPANY_NAME}</title><style>{_DOC_CSS}{company_brand.BRAND_CSS}</style></head><body>
<div class="sheet"><div class="bar"></div><div class="pad">
  <div class="head">{company_brand.brand_block(conn, "Facade restoration · New York City")}
    <div class="meta"><strong>{e(kind.upper())}</strong>{e(inv['number'])}<br>Issued {e(_fmt_date(inv.get('issued_at')))}<br>Due {e(_fmt_date(inv.get('due_at')))}</div></div>
  <h1>{e(inv.get('description') or kind)}</h1>
  <div class="two" style="margin-top:14px">
    <div class="fact"><b>Bill to</b>{e(inv.get('client_name') or '')}{('<br>' + e(inv['sent_name'])) if inv.get('sent_name') else ''}{('<br>' + e(inv['sent_to'])) if inv.get('sent_to') else ''}</div>
    <div class="fact"><b>Project</b>{e(where or '—')}{('<br>' + e(inv['project_code'])) if inv.get('project_code') and where != inv.get('project_code') else ''}{('<br>Proposal ' + e(inv['proposal_number'])) if inv.get('proposal_number') else ''}</div>
  </div>
  <h2>Amount</h2>
  <table><tbody>
    <tr><td>{e(inv.get('description') or kind)}</td><td class='num'>{_money(inv.get('amount'))}</td></tr>
    {retain}
    <tr><td>Paid to date</td><td class='num'>{_money(inv.get('amount_paid'))}</td></tr>
    <tr class='total'><td>{balance_label}</td><td class='num'>{_money(inv.get('balance'))}</td></tr>
  </tbody></table>
  <div class="price keep"><div><b>{balance_label}</b><div class='d'>Due {e(_fmt_date(inv.get('due_at')))}</div></div><div class="v">{_money(inv.get('balance'))}</div></div>
  {pay_rows}
  <div class="keep"><h2>How to pay</h2><p>{e(instructions)}</p></div>
  {('<div class="keep"><h2>Notes</h2><p>' + e(inv['notes']) + '</p></div>') if inv.get('notes') and status != 'void' else ''}
  {stamp}
  <div class="foot">{COMPANY_NAME}{(' · ' + e(letter)) if letter else ''}{lic} · Invoice {e(inv['number'])}</div>
</div></div></body></html>"""


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
        res = pdf_export.render_html_to_pdf(html_path, pdf_path, timeout_sec=30)
        try:
            html_path.unlink(missing_ok=True)
        except Exception:
            pass
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "render failed"}
        conn.execute("UPDATE invoice SET pdf_path = ?, pdf_rendered_at = ? WHERE id = ?",
                     (ssc_paths.store_rel(pdf_path), _now(), invoice_id))
        conn.commit()
        return {"ok": True, "size": pdf_path.stat().st_size, "engine": res.get("engine")}
    except Exception as e:   # missing browser, timeout, disk — the link is the reference copy
        log.warning("#326 pdf not rendered for invoice %s: %s", invoice_id, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def pdf_file(conn, invoice_id: int) -> Optional[Path]:
    r = _one(conn.execute("SELECT pdf_path FROM invoice WHERE id = ?", (invoice_id,)))
    if not r or not r.get("pdf_path"):
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
    rows = [public(r) for r in invoices.list_invoices(conn, project_code=project_code, today=today)]
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
    app.add_url_rule("/api/invoices/<int:invoice_id>/recipient", "invoices_recipient", gate(_api_recipient), methods=["GET"])
    app.add_url_rule("/api/projects/<project_code>/payments", "project_payments", _api_project_payments, methods=["GET"])
    app.add_url_rule("/i/<token>", "invoice_public_page", _public_page, methods=["GET"])
    app.add_url_rule("/i/<token>/pdf", "invoice_public_pdf", _public_pdf, methods=["GET"])
