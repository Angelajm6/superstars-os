"""The invoice editor (accepted prototype 2026-11-11: https://claude.ai/artifact/VZXCMLF9QNLbqiVpNmxkur).

Edit the whole invoice the way Proposal Studio edits a proposal: the company's standard invoice layout
(invoice_docs._standard_document, the one template every invoice uses) drawn in edit mode inside a no-scripts
frame, every word the client reads outlined to click and change, work lines added / removed, totals redrawn on
each save, plus "Ask Claude to change it" with an Undo. Saves go through invoices.update (the same rules as the
job's Edit form: draft or issued, the amount never below what has been paid, AIA / QuickBooks amounts locked).

  GET   /invoices/<id>/edit                       the page (invoice_editor.html)
  GET   /api/invoices/<id>/editor                 {invoice, rules, mode, reason, html, texts, back}
                                                  mode: edit | design (a Claude design is live: switch first) | aia | locked
  POST  /api/invoices/<id>/editor/revise          {instruction} -> Claude's change saved; {changed, before, note}

Admin / C-suite only (requires_section('company_portal')), like every invoice route.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from flask import jsonify, request, send_file

import db_layer
from auth import current_user, requires_section

log = logging.getLogger("ssc.invoice_editor")

SECTION = "company_portal"
PAGE = Path(__file__).resolve().parent / "invoice_editor.html"
SNAP_KEYS = ("lines", "retainage", "due_at", "kind", "bill_to_text", "project_text", "pay_text", "thanks_text", "doc_notes")
REVISE_PROMPT = """You change one invoice for Superstars Contracting Inc., a New York City facade restoration contractor.
You get the invoice as JSON and the person's request. Return ONLY a JSON object: {"invoice": {...}, "note": "..."}.
"invoice" holds the keys you changed, from: lines (the whole list, each {"description", "amount"}), retainage (dollars held),
due_at (YYYY-MM-DD), bill_to_text, project_text (first line = project name, then its address), pay_text, thanks_text,
doc_notes (a note the client reads). "note" is one plain sentence saying what you changed.
Rules: never invent a price. Use only amounts the person gives, amounts already on the invoice, or arithmetic on them
(a percentage of the contract or of a line). If the request needs a price you don't have, change nothing else and say so
in "note". Keep the company's plain, professional wording. Line descriptions are what was done, not marketing."""


def _conn():
    return db_layer.connect()


def _uid():
    u = current_user()
    return u["id"] if u else None


def mode_of(conn, inv: dict, rules: dict) -> tuple:
    """(mode, reason): what the editor can do with this invoice."""
    if rules.get("aia"):
        return "aia", "This invoice is a progress billing form (G702 / G703). Change it on the form under Invoices & Payments."
    if inv.get("source") == "quickbooks":
        return "locked", "This invoice lives in QuickBooks. Change it there and it updates here."
    if inv.get("status") == "paid":
        return "locked", "Paid in full, so this invoice is locked. You can still download it or email the receipt."
    if inv.get("status") == "void":
        return "locked", "This invoice was voided, so it can't be changed."
    if inv.get("design_html"):
        return "design", ("This invoice still uses a design made with Claude. Switch it to the company template to edit it "
                          "here. The design stays in the job's Documents.")
    return "edit", ""


def view(conn, invoice_id: int) -> Optional[dict]:
    import invoice_docs
    import invoices
    inv = invoices.get(conn, invoice_id)
    if not inv:
        return None
    rules = invoices.edit_rules(conn, inv)
    mode, reason = mode_of(conn, inv, rules)
    html = invoice_docs.render_document(conn, inv, edit=(mode == "edit"))
    back = f"/projects/{inv['project_code']}#payments" if inv.get("project_code") else "/company#finance"
    return {"invoice": invoice_docs.public(inv), "rules": rules, "mode": mode, "reason": reason, "html": html,
            "texts": invoice_docs.doc_texts(conn, inv), "back": back,
            "kinds": [{"code": k, "label": invoice_docs.KIND_LABEL.get(k, k)} for k in invoices.KINDS]}


def snapshot(conn, inv: dict) -> dict:
    """What is stored (blank wording = the standard wording), so Undo puts back exactly this."""
    return {"lines": inv.get("lines"), "retainage": inv.get("retainage") or 0, "due_at": inv.get("due_at"),
            "kind": inv.get("kind"), **{k: inv.get(k) or "" for k in ("bill_to_text", "project_text", "pay_text", "thanks_text", "doc_notes")}}


def _fake_change(payload: dict, instruction: str) -> dict:
    """Test double (no model): "add <what> at $N" adds a line; "N% retainage" holds N% of the invoice."""
    out = {}
    m = re.search(r"add (?:a line for |the )?(.+?) (?:at|for) \$([\d,]+(?:\.\d+)?)", instruction or "", re.I)
    if m:
        out["lines"] = payload["lines"] + [{"description": m.group(1).strip(), "amount": float(m.group(2).replace(",", ""))}]
    p = re.search(r"(\d+(?:\.\d+)?)\s*%\s*retainage", instruction or "", re.I)
    if p:
        total = sum(float(x["amount"]) for x in payload["lines"])
        out["retainage"] = round(total * float(p.group(1)) / 100, 2)
    return {"invoice": out, "note": "Done." if out else "I couldn't tell what to change. Say it another way."}


def revise(conn, invoice_id: int, instruction: str, uid) -> dict:
    import assistant
    import invoices
    import proposal_studio
    inv = invoices.get(conn, invoice_id)
    if not inv:
        raise LookupError("invoice not found")
    rules = invoices.edit_rules(conn, inv)
    mode, reason = mode_of(conn, inv, rules)
    if mode != "edit":
        raise ValueError(reason)
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("Say what to change.")
    before = snapshot(conn, inv)
    import invoice_docs
    payload = dict(before, **invoice_docs.doc_texts(conn, inv), number=inv["number"], status=inv["status"], client=inv.get("client_name"),
                   amount_paid=inv.get("amount_paid") or 0, amount_locked=rules.get("amount_locked"))
    contract = None
    try:
        contract = invoice_docs._contract(conn, inv)
    except Exception:
        conn.rollback()
    if contract:
        payload["contract"] = {"proposal": contract["label"], "total": contract["total"], "previously_invoiced": contract["prev"]}
    if assistant._fake():
        out = _fake_change(payload, instruction)
    else:
        proposal_studio._claude_ready(conn, uid)
        text, res = proposal_studio._call(REVISE_PROMPT, [{"type": "text", "text": f"<invoice>\n{json.dumps(payload, indent=1, default=str)}\n"
                                                                              f"</invoice>\n<request>\n{instruction[:2000]}\n</request>"}])
        proposal_studio._usage(conn, uid, res, "Invoice change")
        out = proposal_studio._json_from(text)
    change = {k: v for k, v in (out.get("invoice") or {}).items() if k in SNAP_KEYS}
    if change:
        invoices.update(conn, invoice_id, change, actor=uid)
    after = snapshot(conn, invoices.get(conn, invoice_id))
    changed = [k for k in SNAP_KEYS if json.dumps(before.get(k), sort_keys=True, default=str) != json.dumps(after.get(k), sort_keys=True, default=str)]
    return {"changed": changed, "before": before, "note": str(out.get("note") or "")[:500]}


def register(app) -> None:
    gate = requires_section(SECTION)

    def page(invoice_id):
        resp = send_file(str(PAGE))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    def api_view(invoice_id):
        conn = _conn()
        try:
            v = view(conn, invoice_id)
            if not v:
                return jsonify({"error": "invoice not found"}), 404
            resp = jsonify({"data": v})
            resp.headers["Cache-Control"] = "no-store"
            return resp
        finally:
            conn.close()

    def api_revise(invoice_id):
        body = request.get_json(silent=True) or {}
        conn = _conn()
        try:
            try:
                out = revise(conn, invoice_id, str(body.get("instruction") or ""), _uid())
            except LookupError as e:
                return jsonify({"error": str(e)}), 404
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            out["view"] = view(conn, invoice_id)
            return jsonify({"data": out})
        finally:
            conn.close()

    app.add_url_rule("/invoices/<int:invoice_id>/edit", "invoice_editor_page", gate(page), methods=["GET"])
    app.add_url_rule("/api/invoices/<int:invoice_id>/editor", "invoice_editor_view", gate(api_view), methods=["GET"])
    app.add_url_rule("/api/invoices/<int:invoice_id>/editor/revise", "invoice_editor_revise", gate(api_revise), methods=["POST"])
