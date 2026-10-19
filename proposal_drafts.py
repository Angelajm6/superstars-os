"""Proposals Claude drafts from an email (accepted prototype 2026-10-19).

The Inbox pass proposes a `draft_proposal` card (claude_agent.py) when an email asks for a price. Nothing exists
in the platform until a person approves it. The dashboard's "Review PDF & send" panel reads and edits that card
here, before approval:

  GET  /api/proposal-drafts/<action_id>            -> the card's values, the email it answers, the template in use
  POST /api/proposal-drafts/<action_id>/preview    {values} -> the PDF-to-be as HTML (drawn in the company template)
  POST /api/proposal-drafts/<action_id>/save       {values} -> keeps the edits on the card
  POST /api/proposal-drafts/<action_id>/revise     {values, instruction} -> Claude changes the draft (still a draft)
  POST /api/proposal-drafts/<action_id>/send       {values} -> approve: creates client (if new) + estimate + proposal,
                                                    renders the PDF and sends it as a reply in the email's Gmail thread

`send` goes through claude_agent's own approval path, so it is logged in Settings > Claude approvals like any card.
Only the person whose email it is can open or send it (the card's user_id).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

from flask import jsonify, request

import db_layer
from auth import current_user, requires_section

log = logging.getLogger(__name__)

SECTION = "company_portal"
VALID_DAYS_DEFAULT = 30
FIELDS = ("client_org_id", "client_name", "est_type", "borough", "building_address", "title", "scope_summary",
          "lines", "schedule_text", "exclusions_text", "valid_days", "deposit_pct", "message")


# ------------------------------------------------------------------ the values
def clean(inp: dict) -> dict:
    """Normalize a draft's values (from Claude or the panel). Raises ValueError with a plain message."""
    import estimates
    out = {k: inp.get(k) for k in FIELDS if k in inp}
    out["mail_id"] = int(inp.get("mail_id") or 0)
    if inp.get("client_org_id") not in (None, "", 0, "0"):
        out["client_org_id"] = int(inp["client_org_id"])
    else:
        out["client_org_id"] = None
        if not str(inp.get("client_name") or "").strip():
            raise ValueError("give the client: client_org_id of an existing client, or client_name for a new one")
    out["est_type"] = str(inp.get("est_type") or "FR").strip().upper()
    if out["est_type"] not in estimates.EST_TYPES:
        raise ValueError("est_type must be one of " + ", ".join(estimates.EST_TYPES))
    out["borough"] = str(inp.get("borough") or "MN").strip().upper()
    if out["borough"] not in estimates.BOROUGHS:
        raise ValueError("borough must be one of " + ", ".join(estimates.BOROUGHS))
    lines = []
    for li in inp.get("lines") or []:
        if not isinstance(li, dict) or not str(li.get("description") or "").strip():
            continue
        try:
            qty = float(str(li.get("qty") if li.get("qty") not in (None, "") else 1).replace(",", ""))
            price = float(str(li.get("unit_price") or 0).replace("$", "").replace(",", ""))
        except ValueError:
            raise ValueError("each line needs a number for quantity and unit price")
        if qty < 0 or price < 0:
            raise ValueError("quantities and prices can't be negative")
        lines.append({"description": str(li["description"]).strip()[:300], "qty": qty,
                      "unit": (str(li.get("unit") or "").strip()[:12] or None), "unit_price": price})
    if not lines:
        raise ValueError("a proposal needs at least one priced line")
    out["lines"] = lines[:40]
    out["title"] = str(inp.get("title") or "").strip()[:160] or "Proposal"
    out["scope_summary"] = str(inp.get("scope_summary") or "").strip()[:6000]
    out["message"] = str(inp.get("message") or "").strip()[:6000]
    if not out["message"]:
        raise ValueError("write the email that goes with the PDF (message)")
    try:
        out["valid_days"] = max(1, min(180, int(inp.get("valid_days") or VALID_DAYS_DEFAULT)))
    except (TypeError, ValueError):
        out["valid_days"] = VALID_DAYS_DEFAULT
    if inp.get("deposit_pct") not in (None, ""):
        out["deposit_pct"] = max(0.0, min(100.0, float(inp["deposit_pct"])))
    for k in ("client_name", "building_address", "schedule_text", "exclusions_text"):
        out[k] = (str(inp.get(k) or "").strip() or None)
    return out


def total(v: dict) -> float:
    return round(sum(li["qty"] * li["unit_price"] for li in v.get("lines") or []), 2)


def _client_name(conn, v: dict) -> str:
    if v.get("client_org_id"):
        r = conn.execute("SELECT name FROM crm_organization WHERE id = ?", (v["client_org_id"],)).fetchone()
        if r:
            return r["name"]
    return v.get("client_name") or ""


def preview_html(conn, v: dict, recipient: tuple = ("", "")) -> str:
    import proposals
    price = total(v)
    pct = v.get("deposit_pct") if v.get("deposit_pct") is not None else proposals.DEPOSIT_PCT_DEFAULT
    r = {"id": 0, "number": "DRAFT", "version": 1, "status": "draft", "title": v["title"],
         "client_name": _client_name(conn, v), "recipient_name": recipient[0], "recipient_email": recipient[1],
         "building_address": v.get("building_address"), "scope_summary": v.get("scope_summary"), "price": price,
         "deposit_pct": pct, "deposit_amount": round(price * pct / 100, 2) if pct else None,
         "schedule_text": v.get("schedule_text"), "exclusions_text": v.get("exclusions_text") or proposals.DEFAULT_EXCLUSIONS,
         "terms_text": proposals.DEFAULT_TERMS, "updated_at": datetime.now().isoformat(timespec="seconds"),
         "valid_until": (date.today() + timedelta(days=v.get("valid_days") or VALID_DAYS_DEFAULT)).isoformat()}
    lis = [dict(li, amount=round(li["qty"] * li["unit_price"], 2)) for li in v["lines"]]
    return proposals.render_document(conn, r, mode="pdf", lis=lis)


# ------------------------------------------------------------------ approve = create + PDF + reply in the thread
def create_and_send(conn, uid: int, v: dict) -> dict:
    """The card's execute step. Order matters: nothing is emailed unless the PDF exists."""
    import crm
    import estimates
    import inbox
    import proposals
    from email.utils import parseaddr
    m = inbox.message(conn, v["mail_id"], uid)
    if not m:
        raise LookupError("that email is not in your inbox")
    org_id = v.get("client_org_id")
    if org_id and not crm.get_org(conn, org_id):
        raise ValueError("that client no longer exists")
    if not org_id:
        org_id = crm.create_org(conn, name=v["client_name"], relationship_type="client", created_by=uid)
    est_id = m.get("estimate_id")
    est = estimates.get_estimate(conn, est_id) if est_id else None
    if not est or est.get("client_org_id") != org_id or est.get("status") in ("lost", "converted"):
        est = estimates.create_estimate(conn, est_type=v["est_type"], borough=v["borough"], client_org_id=org_id,
                                        building_address=v.get("building_address"),
                                        notes=f"From the email “{(m['subject'] or '')[:120]}”", created_by=uid)
    est_id = est["id"]
    open_p = conn.execute("SELECT id FROM proposal WHERE estimate_id = ? AND status = 'draft' ORDER BY version DESC",
                          (est_id,)).fetchone()
    p = proposals.get(conn, open_p["id"]) if open_p else proposals.create_from_estimate(conn, est_id, created_by=uid)
    name, addr = parseaddr(m["from"])
    fields = {"title": v["title"], "scope_summary": v.get("scope_summary") or "", "schedule_text": v.get("schedule_text") or "",
              "valid_until": (date.today() + timedelta(days=v["valid_days"])).isoformat(),
              "recipient_email": addr or "", "recipient_name": name or "", "price": total(v)}
    if v.get("exclusions_text"):
        fields["exclusions_text"] = v["exclusions_text"]
    if v.get("deposit_pct") is not None:
        fields["deposit_pct"] = v["deposit_pct"]
    p = proposals.update(conn, p["id"], fields,
                         line_items=[dict(li, amount=round(li["qty"] * li["unit_price"], 2)) for li in v["lines"]], actor=uid)
    res = proposals.render_pdf(conn, p["id"], actor=uid)
    pdf = proposals.pdf_file(conn, p["id"]) if res.get("ok") else None
    if not pdf:
        raise ValueError("The PDF couldn't be made, so nothing was sent. The proposal is saved as a draft "
                         f"({p['number']}); try again from Proposals.")
    inbox.send_reply(conn, uid, m["id"], v["message"],
                     attachments=[(f"{p['number']} {re.sub(r'[^A-Za-z0-9 .-]', '', _client_name(conn, v))[:40]}.pdf".replace("  ", " "),
                                   pdf.read_bytes(), "application/pdf")])
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("UPDATE proposal SET status = 'sent', sent_at = ?, sent_by = ?, updated_at = ? WHERE id = ? AND status = 'draft'",
                 (now, uid, now, p["id"]))
    conn.commit()
    proposals._event(conn, p["id"], "sent", actor=uid, detail="PDF sent as a reply in the client's Gmail thread")
    conn.commit()
    p = proposals.get(conn, p["id"])
    proposals._activity(conn, p, f"Proposal {p['number']} sent as a PDF reply from Gmail", actor=uid)
    inbox.link_record(conn, m["id"], "proposal", p["id"], uid)
    return {"id": p["id"], "summary": f"{p['number']} sent as a PDF in the email thread", "link": f"/company#proposals/{p['id']}",
            "undo": False}


# ------------------------------------------------------------------ the review panel
def _conn():
    return db_layer.connect()


def _me():
    u = current_user() or {}
    return u


def _action(conn, aid, uid) -> Optional[dict]:
    r = conn.execute("SELECT * FROM claude_action WHERE id = ? AND tool = 'draft_proposal'", (aid,)).fetchone()
    r = dict(r) if r else None
    return r if r and r["user_id"] == uid else None


def _view(conn, a, uid) -> dict:
    import inbox
    import proposal_template
    v = json.loads(a["input"] or "{}")
    m = inbox.message(conn, int(v.get("mail_id") or 0), uid) or {}
    from email.utils import parseaddr
    name, addr = parseaddr(m.get("from") or "")
    return {"action_id": a["id"], "status": a["status"], "values": v, "client_name": _client_name(conn, v),
            "new_client": not v.get("client_org_id"),
            "email": {"id": m.get("id"), "from_name": name, "from_email": addr, "subject": m.get("subject"),
                      "received_at": m.get("received_at"), "text": (m.get("body") or "")[:5000]},
            "reply_subject": "Re: " + re.sub(r"^\s*(re|fw|fwd)\s*:\s*", "", m.get("subject") or "", flags=re.I),
            "template": proposal_template.public(conn), "total": total(clean(v)) if v.get("lines") else 0}


def _save(conn, aid, v: dict) -> None:
    conn.execute("UPDATE claude_action SET input = ? WHERE id = ? AND status = 'pending'", (json.dumps(v), aid))
    conn.commit()


REVISE_PROMPT = (
    "You are editing a construction proposal draft before it is sent. Apply the person's request to the JSON below and "
    "reply with the complete updated JSON object only (same keys; `lines` is a list of {description, qty, unit, "
    "unit_price}). Keep everything they didn't ask to change exactly as it is. If the request changes prices, also keep "
    "`message` consistent with them.")


def revise(conn, a, v: dict, instruction: str, uid) -> dict:
    import assistant
    import claude_agent
    why = assistant.unavailable_reason()
    if why:
        raise ValueError("Claude isn't available right now. Edit the details by hand instead.")
    if claude_agent._over_budget(conn):
        raise ValueError("Claude reached today's limit. Edit the details by hand, or try again tomorrow.")
    payload = {k: v.get(k) for k in FIELDS if k in v}
    fake = assistant._fake()
    if fake:
        out = dict(payload)
        pct = re.search(r"(\d+)\s*%", instruction or "")
        if pct:
            sub = total(v)
            out["lines"] = list(v["lines"]) + [{"description": f"Contingency ({pct.group(1)}%)", "qty": 1, "unit": "ls",
                                                "unit_price": round(sub * int(pct.group(1)) / 100, 2)}]
        res = None
    else:
        res = assistant._call_model(
            [{"type": "text", "text": REVISE_PROMPT}],
            [{"role": "user", "content": [{"type": "text", "text": "<draft>\n" + json.dumps(payload, indent=1) + "\n</draft>\n"
                                           "<request>\n" + instruction[:2000] + "\n</request>"}]}])
        txt = res["text"]
        mm = re.search(r"\{.*\}", txt, re.S)
        if not mm:
            raise ValueError("Claude didn't return a usable draft. Try saying it another way.")
        try:
            out = json.loads(mm.group(0))
        except ValueError:
            raise ValueError("Claude didn't return a usable draft. Try saying it another way.")
    nv = clean(dict(v, **{k: out[k] for k in FIELDS if k in out}, mail_id=v["mail_id"]))
    if res and a.get("chat_id"):
        try:
            claude_agent._add_msg(conn, a["chat_id"], "assistant", "note",
                                  [{"type": "text", "text": f"[Proposal draft changed on request: {instruction[:200]}]"}],
                                  replay=False, usage=res.get("usage"), model_id=res.get("model"))
        except Exception as e:
            log.info("revise usage not recorded: %s", type(e).__name__)
    return nv


def register(app) -> None:
    gate = requires_section(SECTION)

    def load(aid):
        me = _me()
        conn = _conn()
        a = _action(conn, aid, me.get("id"))
        return me, conn, a

    @app.route("/api/proposal-drafts/<int:aid>", methods=["GET"])
    @gate
    def proposal_draft_get(aid):
        me, conn, a = load(aid)
        try:
            if not a:
                return jsonify({"error": "not found"}), 404
            return jsonify({"data": _view(conn, a, me["id"])})
        finally:
            conn.close()

    @app.route("/api/proposal-drafts/<int:aid>/preview", methods=["POST"])
    @gate
    def proposal_draft_preview(aid):
        me, conn, a = load(aid)
        try:
            if not a:
                return jsonify({"error": "not found"}), 404
            v = json.loads(a["input"] or "{}")
            body = (request.get_json(silent=True) or {}).get("values") or v
            try:
                c = clean(dict(body, mail_id=v.get("mail_id")))
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            import inbox
            from email.utils import parseaddr
            m = inbox.message(conn, c["mail_id"], me["id"]) or {}
            return jsonify({"data": {"html": preview_html(conn, c, parseaddr(m.get("from") or "")), "total": total(c)}})
        finally:
            conn.close()

    @app.route("/api/proposal-drafts/<int:aid>/save", methods=["POST"])
    @gate
    def proposal_draft_save(aid):
        me, conn, a = load(aid)
        try:
            if not a:
                return jsonify({"error": "not found"}), 404
            if a["status"] != "pending":
                return jsonify({"error": "This proposal was already decided."}), 409
            v = json.loads(a["input"] or "{}")
            try:
                c = clean(dict((request.get_json(silent=True) or {}).get("values") or {}, mail_id=v.get("mail_id")))
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            _save(conn, aid, c)
            return jsonify({"data": _view(conn, _action(conn, aid, me["id"]), me["id"])})
        finally:
            conn.close()

    @app.route("/api/proposal-drafts/<int:aid>/revise", methods=["POST"])
    @gate
    def proposal_draft_revise(aid):
        me, conn, a = load(aid)
        try:
            if not a:
                return jsonify({"error": "not found"}), 404
            if a["status"] != "pending":
                return jsonify({"error": "This proposal was already decided."}), 409
            d = request.get_json(silent=True) or {}
            ins = str(d.get("instruction") or "").strip()
            if not ins:
                return jsonify({"error": "Say what to change."}), 400
            v = json.loads(a["input"] or "{}")
            try:
                base = clean(dict(d.get("values") or v, mail_id=v.get("mail_id")))
                nv = revise(conn, a, base, ins, me["id"])
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            _save(conn, aid, nv)
            return jsonify({"data": _view(conn, _action(conn, aid, me["id"]), me["id"])})
        finally:
            conn.close()

    @app.route("/api/proposal-drafts/<int:aid>/send", methods=["POST"])
    @gate
    def proposal_draft_send(aid):
        me, conn, a = load(aid)
        try:
            if not a:
                return jsonify({"error": "not found"}), 404
            if a["status"] != "pending":
                return jsonify({"error": "This proposal was already decided."}), 409
            v = json.loads(a["input"] or "{}")
            body = (request.get_json(silent=True) or {}).get("values")
            if body:
                try:
                    _save(conn, aid, clean(dict(body, mail_id=v.get("mail_id"))))
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400
        finally:
            conn.close()
        import claude_agent
        return claude_agent._decide(aid, "approve")   # the one approval path: run, execute, log
