"""Proposal Studio (accepted prototype 2026-10-20: https://claude.ai/artifact/A7RwoL4vXTVdY3otAZG32X).

Admin / C-suite ask Claude for a proposal in their own words; Claude writes the whole proposal from the company's
template (proposal_template.py), its house style (learned from the proposals it sent, editable) and its prices
(past proposals), saves it as a DRAFT, and the person edits anything in place before turning it into a PDF and
sending it from their own Gmail (a reply in the client's thread or a new email). Nothing is sent until they press
Send; Claude's changes are drafts with an Undo.

  GET   /proposal-studio                          the page (proposal_studio.html)
  POST  /api/proposal-studio/drafts               {what, client_org_id | client_name, building_address, mail_id?, file_ids?}
                                                  -> {job}; Claude drafts in the background
  GET   /api/proposal-studio/jobs/<job>           -> {state: working | done | failed, step, proposal_id, error}
  GET   /api/proposal-studio/proposals            recent proposals for the start page
  GET   /api/proposal-studio/<pid>                the proposal + its document in edit mode + people / threads to send to
  PATCH /api/proposal-studio/<pid>                {fields, lines?} -> saved; the document again
  POST  /api/proposal-studio/<pid>/revise         {instruction} -> Claude's change saved; {changed, before}
  POST  /api/proposal-studio/<pid>/new-version    a sent proposal becomes v2 to edit (the sent copy stays)
  POST  /api/proposal-studio/<pid>/send           {how: reply | new, mail_id, to, cc, subject, message} -> PDF + Gmail
  GET / PUT /api/proposal-studio/house-style      what Claude follows (editable); POST .../learn reads the sent proposals

Every model call counts toward the daily Claude limit (a hidden 'system' chat, never in History).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from flask import jsonify, request, send_file

import db_layer
from auth import current_user, requires_section

log = logging.getLogger(__name__)

SECTION = "company_portal"
PAGE = Path(__file__).resolve().parent / "proposal_studio.html"
STYLE_KEY = "proposal_house_style"
PAST_N = 20
FOLLOW_UP_DAYS = 3
FIELDS = ("title", "prepared_for", "project_text", "reference_text", "intro_text", "scope_summary", "schedule_text",
          "exclusions_text", "terms_text", "deposit_pct", "email_message", "submitted_by")
STYLE_ITEMS = (("opening", "Opening"), ("scope_style", "Scope style"), ("exclusions", "Standard exclusions"),
               ("terms", "Payment terms"), ("words", "Words you use"))
STEPS = ("Reading your template", "Reading your sent proposals for wording and prices", "Looking up the client",
         "Reading what you attached", "Writing the proposal")

_JOBS: dict = {}          # one Render instance (WEB_CONCURRENCY=1): in-memory is enough for a 30-second job
_LOCK = threading.Lock()


# ------------------------------------------------------------------ helpers
def _conn():
    return db_layer.connect()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _rows(conn, sql, params=()) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params=()) -> Optional[dict]:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def _me() -> dict:
    u = current_user() or {}
    return {"id": u.get("id"), "role": u.get("role"),
            "name": u.get("display_name") or u.get("full_name") or (u.get("email") or "").split("@")[0]}


def _clip(s, n) -> str:
    s = str(s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _strip_html(h: str) -> str:
    t = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", h or "")
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = re.sub(r"\{\{\s*\w+\s*\}\}", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _json_from(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("Claude didn't return a usable proposal. Try again, or say it another way.")
    try:
        d = json.loads(m.group(0))
    except ValueError:
        raise ValueError("Claude didn't return a usable proposal. Try again, or say it another way.")
    if not isinstance(d, dict):
        raise ValueError("Claude didn't return a usable proposal. Try again.")
    return d


def _num(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def clean_lines(raw) -> list:
    out = []
    for li in raw or []:
        if not isinstance(li, dict) or not str(li.get("description") or "").strip():
            continue
        qty, price = _num(li.get("qty")), _num(li.get("unit_price"))
        if (qty is not None and qty < 0) or (price is not None and price < 0):
            raise ValueError("quantities and prices can't be negative")
        out.append({"description": str(li["description"]).strip()[:300], "qty": qty if qty is not None else 1,
                    "unit": (str(li.get("unit") or "").strip()[:12] or None), "unit_price": price if price is not None else 0})
    return out[:60]


def _usage(conn, uid, res, title) -> None:
    if not res or not res.get("usage"):
        return
    try:
        import claude_agent
        now = _now()
        conn.execute("INSERT INTO claude_chat (user_id, surface, title, source, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                     (uid, "office", title[:80], "system", now, now))
        conn.commit()
        cid = _one(conn, "SELECT MAX(id) AS m FROM claude_chat WHERE user_id = ? AND source = 'system'", (uid,))["m"]
        claude_agent._add_msg(conn, cid, "assistant", "note", [{"type": "text", "text": f"[{title}]"}],
                              replay=False, usage=res.get("usage"), model_id=res.get("model"))
    except Exception as e:
        log.info("proposal studio usage not recorded: %s", type(e).__name__)


def _claude_ready(conn) -> None:
    import assistant
    import claude_agent
    why = assistant.unavailable_reason()
    if why:
        raise ValueError("Claude isn't switched on yet." if why == "no_api_key" else "Claude is switched off (daily limit set to zero).")
    if claude_agent._over_budget(conn):
        raise ValueError("Claude reached today's limit. Try again tomorrow, or edit by hand.")


def _call(system: str, content: list) -> tuple:
    import assistant
    res = assistant._call_model([{"type": "text", "text": system}], [{"role": "user", "content": content}])
    if res.get("refused"):
        raise ValueError("Claude couldn't write that one. Try describing the job another way.")
    return res["text"], res


# ------------------------------------------------------------------ house style
def house_style(conn) -> dict:
    try:
        r = _one(conn, "SELECT value FROM app_settings WHERE key = ?", (STYLE_KEY,))
        d = json.loads(r["value"]) if r and r.get("value") else {}
    except Exception:
        conn.rollback()
        d = {}
    items = {i.get("key"): i for i in d.get("items") or [] if isinstance(i, dict)}
    d["items"] = [dict({"key": k, "label": label, "text": "", "source": ""}, **items.get(k, {})) for k, label in STYLE_ITEMS]
    d.setdefault("learn", True)
    d.setdefault("show_sources", True)
    d.setdefault("state", "none")
    return d


def _put_style(conn, d: dict) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    conn.execute("DELETE FROM app_settings WHERE key = ?", (STYLE_KEY,))
    conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)", (STYLE_KEY, json.dumps(d), _now()))
    conn.commit()


def past_proposals(conn, n: int = PAST_N) -> list:
    rows = _rows(conn, "SELECT p.id, p.number, p.title, p.scope_summary, p.intro_text, p.exclusions_text, p.terms_text, "
                       "p.schedule_text, p.price, p.sent_at, p.status, o.name AS client_name FROM proposal p "
                       "JOIN crm_organization o ON o.id = p.client_org_id WHERE (p.status IN ('sent', 'viewed', 'signed') "
                       "OR (p.status IN ('superseded', 'declined') AND p.sent_at IS NOT NULL)) "
                       "ORDER BY COALESCE(p.sent_at, p.updated_at) DESC LIMIT ?", (n,))
    for r in rows:
        r["lines"] = _rows(conn, "SELECT description, qty, unit, unit_price FROM proposal_line WHERE proposal_id = ? "
                                 "ORDER BY sort, id", (r["id"],))
    return rows


def _past_text(rows: list, full: bool) -> str:
    out = []
    for r in rows:
        ls = "; ".join(f"{li['description']} — {'%g' % (li['qty'] or 0)} {li['unit'] or ''} @ ${li['unit_price'] or 0:,.2f}"
                       for li in r["lines"][:12])
        part = (f"## {r['number']} · {r['client_name']} · {(r.get('sent_at') or '')[:10]} · {r['status']} · total ${r.get('price') or 0:,.0f}\n"
                f"Title: {r.get('title') or ''}\nLines: {ls or '(none)'}\n")
        if full:
            part += (f"Opening: {_clip(r.get('intro_text'), 600)}\nScope: {_clip(r.get('scope_summary'), 1200)}\n"
                     f"Schedule: {_clip(r.get('schedule_text'), 300)}\nExclusions: {_clip(r.get('exclusions_text'), 600)}\n"
                     f"Terms: {_clip(r.get('terms_text'), 600)}\n")
        else:
            part += f"Scope: {_clip(r.get('scope_summary'), 500)}\n"
        out.append(part)
    return "\n".join(out)


LEARN_PROMPT = (
    "Below are a construction company's proposal template and the proposals it sent. Write down its house style so "
    "another writer sounds exactly like them. Reply with JSON only: {\"opening\": ..., \"scope_style\": ..., "
    "\"exclusions\": ..., \"terms\": ..., \"words\": ...}. opening = the opening paragraph they use (their words); "
    "scope_style = how they write scope (numbering, order, how quantities appear); exclusions = their standard "
    "exclusions (their words); terms = their payment terms (their words); words = terms of art they prefer and avoid. "
    "Each value is plain text, at most 600 characters. When something isn't in the material, write an empty string.")


def learn(conn, uid) -> dict:
    """Claude reads the template + the sent proposals and writes the house style (the person can correct it)."""
    import assistant
    import proposal_template
    rows = past_proposals(conn)
    tpl = proposal_template.active_html(conn)
    if not rows and not tpl:
        raise ValueError("There's nothing to learn from yet: upload your template or send a proposal first.")
    if assistant._fake():
        out, res = {"opening": "Thank you for the opportunity to submit our proposal.", "scope_style": "Numbered steps.",
                    "exclusions": rows[0]["exclusions_text"] if rows and rows[0].get("exclusions_text") else "",
                    "terms": rows[0]["terms_text"] if rows and rows[0].get("terms_text") else "", "words": "“Exterior”, not “facade”."}, None
    else:
        _claude_ready(conn)
        text, res = _call("You describe a company's writing style precisely, in its own words.",
                          [{"type": "text", "text": f"<template>\n{_clip(_strip_html(tpl or ''), 8000)}\n</template>\n"
                                                    f"<sent_proposals>\n{_past_text(rows, True)}\n</sent_proposals>\n\n{LEARN_PROMPT}"}])
        out = _json_from(text)
        _usage(conn, uid, res, "Proposal house style")
    d = house_style(conn)
    src = (f"from {len(rows)} proposal{'s' if len(rows) != 1 else ''}" if rows else "") + (" and your template" if tpl else "")
    for it in d["items"]:
        v = str(out.get(it["key"]) or "").strip()[:1200]
        if v and not it.get("edited"):
            it.update(text=v, source=src.strip())
    d.update(state="ready", learned_at=_now(), learned_from=len(rows))
    _put_style(conn, d)
    return d


# ------------------------------------------------------------------ drafting
DRAFT_PROMPT = (
    "Write a complete proposal for this job as the company would, following its house style and wording exactly, and "
    "pricing from its past proposals (same kind of work, same units; say where each price came from). Reply with JSON "
    "only, these keys:\n"
    "est_type (FR exterior restoration | IRA rope access inspection | IR interior repair | PG parking garage | OTH other), "
    "borough (MN, BX, BK, QN, SI, LI or NJ), title, prepared_for (contact and company), project_text (the address), "
    "reference_text (short, may be empty), intro_text (their opening paragraph addressed to the contact), scope_summary "
    "(their scope style; plain text with line breaks), lines (list of {description, qty, unit, unit_price, source}), "
    "schedule_text, exclusions_text, terms_text, deposit_pct (number), valid_days (number), email_subject, "
    "email_message (the short email that carries the PDF, signed with the person's first name), sources (3–6 short "
    "notes on what you used: template, which past proposals, prices, files).\n"
    "Plain text only, no markdown. Never invent a price with no basis: when there is none, use your best estimate and "
    "say so in that line's source.")


def _fake_draft(what: str, client: str, addr: str) -> dict:
    return {"est_type": "FR", "borough": "MN", "title": "Exterior repairs" + (f" · {addr.split(',')[0]}" if addr else ""),
            "prepared_for": client, "project_text": addr, "reference_text": "", "intro_text": f"Thank you for the opportunity. {what[:120]}",
            "scope_summary": "1. Sidewalk bridge and permits.\n2. Repoint the east elevation.\n3. Replace six steel lintels.",
            "lines": [{"description": "Sidewalk bridge, permits and mobilization", "qty": 1, "unit": "ls", "unit_price": 6400, "source": "last 3 jobs"},
                      {"description": "Repoint east elevation", "qty": 1850, "unit": "sf", "unit_price": 15, "source": "FR-MN-014"},
                      {"description": "Steel lintel replacement", "qty": 6, "unit": "ea", "unit_price": 2400, "source": "FR-MN-014"}],
            "schedule_text": "About six weeks from permit approval.", "exclusions_text": "", "terms_text": "", "deposit_pct": 30,
            "valid_days": 30, "email_subject": "Proposal", "email_message": "Hi,\n\nOur proposal is attached.\n\nThanks",
            "sources": ["Your template", "Prices from FR-MN-014"]}


def _context(conn, uid, req: dict) -> list:
    import proposal_template
    blocks = []
    tpl = proposal_template.active_html(conn)
    st = house_style(conn)
    style = "\n".join(f"{i['label']}: {i['text']}" for i in st["items"] if i.get("text"))
    past = past_proposals(conn) if st.get("learn", True) else []
    org = None
    if req.get("client_org_id"):
        org = _one(conn, "SELECT id, name FROM crm_organization WHERE id = ?", (req["client_org_id"],))
    client = (f"Existing client: {org['name']}" if org else f"New client: {req.get('client_name')}")
    if org:
        cs = _rows(conn, "SELECT full_name, title FROM crm_contact WHERE org_id = ? AND COALESCE(status,'active') <> 'inactive' LIMIT 8", (org["id"],))
        jobs = _rows(conn, "SELECT project_code, name, address FROM projects WHERE client_org_id = ? LIMIT 8", (org["id"],))
        client += ("\nContacts: " + "; ".join(f"{c['full_name']}{' (' + c['title'] + ')' if c.get('title') else ''}" for c in cs)) if cs else ""
        client += ("\nPast jobs: " + "; ".join(f"{j['project_code']} {j['name']} {j.get('address') or ''}" for j in jobs)) if jobs else ""
    txt = (f"<template_text>\n{_clip(_strip_html(tpl or ''), 6000) or '(no template uploaded; use a clean professional layout)'}\n</template_text>\n"
           f"<house_style>\n{style or '(not learned yet: follow the past proposals)'}\n</house_style>\n"
           f"<past_proposals>\n{_past_text(past, False) or '(none yet)'}\n</past_proposals>\n"
           f"<client>\n{client}\nBuilding: {req.get('building_address') or '(not given)'}\n</client>\n")
    if req.get("mail_id"):
        import inbox
        m = inbox.message(conn, int(req["mail_id"]), uid)
        if m:
            txt += f"<email from=\"{m['from']}\" subject=\"{m['subject']}\">\n{_clip(m['body'], 8000)}\n</email>\n"
    blocks.append({"type": "text", "text": txt})
    try:   # their own past proposals, as uploaded: the model copies them
        import proposal_template
        past_docs = proposal_template.past_proposal_blocks(conn)
    except Exception:
        past_docs = []
    if past_docs:
        blocks.append({"type": "text", "text": f"The company's own past proposals follow ({len(past_docs)}). Copy them exactly: the "
                                               "same section order and headings, the same opening and closing, the same way of "
                                               "writing scope, the same exclusions, terms and signature wording, and the same price "
                                               "formatting. Use their prices for the same kinds of work. Only the facts of this new "
                                               "job change."})
        blocks.extend(past_docs)
    if req.get("file_ids"):
        import claude_files
        for r in claude_files.owned(conn, uid, req["file_ids"]):
            blocks.extend(claude_files.blocks(conn, r))
    blocks.append({"type": "text", "text": f"<request from=\"{_me_name(conn, uid)}\">\n{req['what']}\n</request>\n\n{DRAFT_PROMPT}"})
    return blocks


def _me_name(conn, uid) -> str:
    u = _one(conn, "SELECT COALESCE(NULLIF(display_name, ''), NULLIF(full_name, ''), email) AS n FROM users WHERE id = ?", (uid,))
    return (u or {}).get("n") or ""


def _apply(conn, pid: int, d: dict, uid, *, first=False) -> dict:
    """Claude's (or the person's) values onto a DRAFT proposal."""
    import proposals
    fields = {}
    for k in FIELDS:
        if k in d and d[k] is not None:
            fields[k] = d[k] if k != "deposit_pct" else (_num(d[k]) if _num(d[k]) is not None else None)
    if "deposit_pct" in fields and fields["deposit_pct"] is None:
        fields.pop("deposit_pct")
    if d.get("valid_days"):
        try:
            fields["valid_until"] = (date.today() + timedelta(days=max(1, min(180, int(d["valid_days"]))))).isoformat()
        except (TypeError, ValueError):
            pass
    for k in ("extra_sections", "hidden_sections"):
        if k in d:
            fields[k] = d[k]
    lines = clean_lines(d["lines"]) if "lines" in d and d["lines"] is not None else None
    if lines is not None:
        fields["price"] = round(sum(li["qty"] * li["unit_price"] for li in lines), 2)
    r = proposals.update(conn, pid, fields, line_items=[dict(li, amount=round(li["qty"] * li["unit_price"], 2)) for li in lines]
                         if lines is not None else None, actor=uid)
    if first:
        conn.execute("UPDATE proposal SET studio = 1, sources = ?, source_mail_id = ? WHERE id = ?",
                     (json.dumps([str(s)[:200] for s in (d.get("sources") or [])][:8]), d.get("_mail_id"), pid))
        conn.commit()
    return r


def _create(conn, uid, req: dict, d: dict) -> int:
    import crm
    import estimates
    import proposals
    org_id = req.get("client_org_id")
    if not org_id:
        org_id = crm.create_org(conn, name=req["client_name"], relationship_type="client", created_by=uid)
    et = str(d.get("est_type") or "FR").upper()
    et = et if et in estimates.EST_TYPES else "FR"
    bo = str(d.get("borough") or "MN").upper()
    bo = bo if bo in estimates.BOROUGHS else "MN"
    est = estimates.create_estimate(conn, est_type=et, borough=bo, client_org_id=org_id,
                                    building_address=req.get("building_address") or d.get("project_text"),
                                    notes="Drafted in Proposal Studio: " + _clip(req["what"], 400), created_by=uid)
    p = proposals.create_from_estimate(conn, est["id"], created_by=uid)
    last = _one(conn, "SELECT submitted_by FROM proposal WHERE created_by = ? AND submitted_by IS NOT NULL AND id <> ? "
                      "ORDER BY id DESC LIMIT 1", (uid, p["id"]))   # the SUBMITTED BY they wrote last time
    if last and "submitted_by" not in d:
        d = dict(d, submitted_by=last["submitted_by"])
    _apply(conn, p["id"], dict(d, _mail_id=req.get("mail_id")), uid, first=True)
    proposals._event(conn, p["id"], "drafted", actor=uid, detail="drafted by Claude in Proposal Studio")
    conn.commit()
    return p["id"]


def _job(job: str, uid, req: dict) -> None:
    conn = _conn()

    def step(i):
        with _LOCK:
            _JOBS[job]["step"] = i

    try:
        import assistant
        step(0)
        if assistant._fake():
            d, res = _fake_draft(req["what"], req.get("client_name") or "", req.get("building_address") or ""), None
            step(4)
        else:
            content = _context(conn, uid, req)
            step(1); step(2)
            if req.get("file_ids"):
                step(3)
            step(4)
            for attempt in (1, 2):   # one more try when the reply isn't usable JSON or the call drops (2026-10-21)
                try:
                    text, res = _call("You write construction proposals for this company, in its own voice.", content)
                    _usage(conn, uid, res, "Proposal Studio draft")
                    d = _json_from(text)
                    break
                except Exception as x:
                    if attempt == 2 or "couldn't write" in str(x):
                        raise
                    log.info("proposal studio draft retry after %s", type(x).__name__)
        pid = _create(conn, uid, req, d)
        with _LOCK:
            _JOBS[job].update(state="done", proposal_id=pid)
        log.info("proposal studio draft %s ready", pid)
    except (ValueError, LookupError) as e:
        with _LOCK:
            _JOBS[job].update(state="failed", error=str(e))
    except Exception as e:
        log.warning("proposal studio draft failed: %s", type(e).__name__)
        try:
            conn.rollback()
        except Exception:
            pass
        with _LOCK:
            _JOBS[job].update(state="failed", error="Claude couldn't finish the draft. Try again in a minute.")
    finally:
        conn.close()


def start_draft(conn, uid, req: dict, *, background=True) -> str:
    what = str(req.get("what") or "").strip()
    if len(what) < 10:
        raise ValueError("Tell Claude a bit more about the job: who it's for and what the work is.")
    req["what"] = what[:6000]
    if req.get("client_org_id"):
        req["client_org_id"] = int(req["client_org_id"])
        if not _one(conn, "SELECT id FROM crm_organization WHERE id = ?", (req["client_org_id"],)):
            raise ValueError("That client no longer exists.")
    elif not str(req.get("client_name") or "").strip():
        raise ValueError("Pick the client, or type the name of a new one.")
    req["client_name"] = str(req.get("client_name") or "").strip()[:200]
    req["building_address"] = str(req.get("building_address") or "").strip()[:300]
    if req.get("file_ids"):
        import claude_files
        claude_files.owned(conn, uid, [int(x) for x in req["file_ids"]][:10])   # raises LookupError for any other id
        req["file_ids"] = [int(x) for x in req["file_ids"]][:10]
    if req.get("mail_id"):
        import inbox
        if not inbox.message(conn, int(req["mail_id"]), uid):
            raise ValueError("That email isn't in your inbox.")
    import assistant
    if not assistant._fake():
        _claude_ready(conn)
    job = uuid.uuid4().hex[:12]
    with _LOCK:
        _JOBS[job] = {"state": "working", "step": 0, "uid": uid, "proposal_id": None, "error": None, "at": _now()}
    if background:
        threading.Thread(target=_job, args=(job, uid, dict(req)), daemon=True).start()
    else:
        _job(job, uid, dict(req))
    return job


# ------------------------------------------------------------------ the editor
def view(conn, pid: int, uid) -> Optional[dict]:
    import proposal_template
    import proposals
    r = proposals.get(conn, pid)
    if not r:
        return None
    lis = proposals.lines(conn, pid)
    contacts = _rows(conn, "SELECT id, full_name, email, title FROM crm_contact WHERE org_id = ? AND email IS NOT NULL "
                           "AND email <> '' AND COALESCE(status,'active') <> 'inactive' ORDER BY full_name LIMIT 30", (r["client_org_id"],))
    threads = []
    try:
        import inbox
        ids = _rows(conn, "SELECT id FROM mail_message WHERE user_id = ? AND (client_org_id = ? OR id = ?) "
                          "ORDER BY received_at DESC LIMIT 25", (uid, r["client_org_id"], r.get("source_mail_id") or 0))
        seen = set()
        for x in ids:
            m = inbox.message(conn, x["id"], uid)
            if not m or m["thread_id"] in seen:
                continue
            seen.add(m["thread_id"])
            threads.append({"id": m["id"], "subject": m["subject"], "from": m["from"], "received_at": m["received_at"],
                            "direction": m["direction"]})
            if len(threads) >= 8:
                break
    except Exception as e:
        log.info("proposal studio threads unavailable: %s", type(e).__name__)
    st = house_style(conn)
    me = _one(conn, "SELECT google_email FROM gmail_link WHERE user_id = ?", (uid,)) or {}
    return {"proposal": {k: r.get(k) for k in ("id", "number", "version", "status", "title", "client_org_id", "client_name",
                                                "price", "valid_until", "recipient_email", "recipient_name", "sent_at",
                                                "estimate_code", "email_message", "deposit_pct", "source_mail_id", "updated_at")
                         },
            "sections": {"extra_sections": proposals._json_list(r.get("extra_sections")),
                         "hidden_sections": proposals._json_list(r.get("hidden_sections"))},
            "sources": proposals._json_list(r.get("sources")) if st.get("show_sources", True) else [],
            "lines": [{"description": li["description"], "qty": li["qty"], "unit": li["unit"], "unit_price": li["unit_price"]} for li in lis],
            "html": proposals.render_document(conn, r, mode="edit"),
            "contacts": contacts, "threads": threads, "template": proposal_template.public(conn),
            "gmail": me.get("google_email"), "editable": r["status"] == "draft"}


REVISE_PROMPT = (
    "You are editing a construction proposal before it is sent. Apply the person's request to the JSON below and reply "
    "with the complete updated JSON object only (same keys; lines are {description, qty, unit, unit_price}; "
    "submitted_by is the SUBMITTED BY block: who the proposal is from, one item per line). Keep "
    "everything they didn't ask to change exactly as it is, in the company's own wording. Plain text, no markdown.")
REVISE_KEYS = ("title", "submitted_by", "prepared_for", "project_text", "reference_text", "intro_text", "scope_summary", "schedule_text",
               "exclusions_text", "terms_text", "deposit_pct", "email_message", "extra_sections", "lines")


def snapshot(conn, pid) -> dict:
    import proposals
    r = proposals.get(conn, pid)
    d = {k: r.get(k) for k in REVISE_KEYS if k not in ("lines", "extra_sections")}
    d["submitted_by"] = r.get("submitted_by") or proposals.default_submitted_by(proposals._company_facts(conn))
    d["extra_sections"] = proposals._json_list(r.get("extra_sections"))
    d["hidden_sections"] = proposals._json_list(r.get("hidden_sections"))
    d["lines"] = [{"description": li["description"], "qty": li["qty"], "unit": li["unit"], "unit_price": li["unit_price"]}
                  for li in proposals.lines(conn, pid)]
    return d


def revise(conn, pid: int, instruction: str, uid) -> dict:
    import assistant
    before = snapshot(conn, pid)
    payload = {k: before.get(k) for k in REVISE_KEYS}
    if assistant._fake():
        out, res = dict(payload), None
        pct = re.search(r"(\d+)\s*%", instruction or "")
        if pct:
            sub = sum((li["qty"] or 0) * (li["unit_price"] or 0) for li in payload["lines"])
            out["lines"] = payload["lines"] + [{"description": f"Contingency ({pct.group(1)}%)", "qty": 1, "unit": "ls",
                                                "unit_price": round(sub * int(pct.group(1)) / 100, 2)}]
        elif "short" in (instruction or "").lower():
            out["scope_summary"] = (payload.get("scope_summary") or "").split("\n")[0]
    else:
        _claude_ready(conn)
        st = house_style(conn)
        style = "\n".join(f"{i['label']}: {i['text']}" for i in st["items"] if i.get("text"))
        text, res = _call(REVISE_PROMPT, [{"type": "text", "text": f"<house_style>\n{style}\n</house_style>\n<proposal>\n"
                                                                   f"{json.dumps(payload, indent=1)}\n</proposal>\n<request>\n{instruction[:2000]}\n</request>"}])
        _usage(conn, uid, res, "Proposal Studio change")
        out = _json_from(text)
    new = {k: out[k] for k in REVISE_KEYS if k in out}
    _apply(conn, pid, new, uid)
    after = snapshot(conn, pid)
    changed = [k for k in REVISE_KEYS if json.dumps(before.get(k), sort_keys=True, default=str) != json.dumps(after.get(k), sort_keys=True, default=str)]
    return {"changed": changed, "before": before}


# ------------------------------------------------------------------ send
def send(conn, pid: int, uid, body: dict) -> dict:
    import inbox
    import proposals
    import team_items
    from email.utils import getaddresses
    r = proposals.get(conn, pid)
    if not r:
        raise LookupError("proposal not found")
    if r["status"] != "draft":
        raise ValueError("This proposal was already sent. Make a new version to send it again.")
    how = body.get("how") or "new"
    message = str(body.get("message") or "").strip()
    if not message:
        raise ValueError("Write the message that goes with the PDF.")
    res = proposals.render_pdf(conn, pid, actor=uid)
    pdf = proposals.pdf_file(conn, pid) if res.get("ok") else None
    if not pdf:
        raise ValueError("The PDF couldn't be made, so nothing was sent. Try again in a minute.")
    fname = re.sub(r"[^A-Za-z0-9 .-]", "", f"{r['number']} {r.get('client_name') or ''}").strip()[:80] + ".pdf"
    att = [(fname, pdf.read_bytes(), "application/pdf")]
    if how == "reply":
        mid = int(body.get("mail_id") or 0)
        m = inbox.message(conn, mid, uid)
        if not m:
            raise ValueError("Pick the email to reply to.")
        out = inbox.send_reply(conn, uid, mid, message, subject=body.get("subject"), reply_all=bool(body.get("reply_all")),
                               attachments=att)
        to = out.get("to") or ""
        inbox.link_record(conn, mid, "proposal", pid, uid)
    else:
        to = str(body.get("to") or "").strip()
        out = inbox.compose(conn, uid, to, str(body.get("cc") or ""), str(body.get("subject") or f"Proposal {r['number']}"),
                            message, attachments=att)
    addrs = getaddresses([to])
    name, addr = (addrs[0] if addrs else ("", ""))
    now = _now()
    conn.execute("UPDATE proposal SET status = 'sent', sent_at = ?, sent_by = ?, recipient_email = COALESCE(NULLIF(?, ''), recipient_email), "
                 "recipient_name = COALESCE(NULLIF(?, ''), recipient_name), email_message = ?, updated_at = ? WHERE id = ? AND status = 'draft'",
                 (now, uid, addr, name, message, now, pid))
    conn.commit()
    proposals._event(conn, pid, "sent", actor=uid, detail="PDF sent from Gmail (" + ("reply in the thread" if how == "reply" else "new email") + ")")
    conn.commit()
    r = proposals.get(conn, pid)
    proposals._activity(conn, r, f"Proposal {r['number']} sent as a PDF from Gmail", actor=uid)
    task = None
    try:
        task = team_items.create(conn, {"kind": "task", "title": f"Follow up on proposal {r['number']} · {r.get('client_name') or ''}"[:160],
                                        "due_date": (date.today() + timedelta(days=FOLLOW_UP_DAYS)).isoformat(),
                                        "assignee_user_id": uid}, actor=uid)
    except Exception as e:
        log.info("proposal follow-up task not created: %s", type(e).__name__)
    return {"id": pid, "number": r["number"], "to": to, "follow_up_task": task,
            "follow_up_date": (date.today() + timedelta(days=FOLLOW_UP_DAYS)).isoformat()}


# ------------------------------------------------------------------ routes
def register(app) -> None:
    gate = requires_section(SECTION)

    def with_conn(fn):
        conn = _conn()
        try:
            return fn(conn)
        finally:
            conn.close()

    def err(msg, code=400):
        return jsonify({"error": msg}), code

    @app.route("/proposal-studio", methods=["GET"])
    @gate
    def proposal_studio_page():
        resp = send_file(str(PAGE))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    @app.route("/api/proposal-studio/drafts", methods=["POST"])
    @gate
    def proposal_studio_draft():
        me = _me()
        try:
            job = with_conn(lambda c: start_draft(c, me["id"], dict(request.get_json(silent=True) or {})))
        except LookupError:
            return err("One of the attached files is no longer available. Attach it again.")
        except ValueError as e:
            return err(str(e))
        return jsonify({"data": {"job": job, "steps": STEPS}}), 202

    @app.route("/api/proposal-studio/jobs/<job>", methods=["GET"])
    @gate
    def proposal_studio_job(job):
        me = _me()
        with _LOCK:
            j = dict(_JOBS.get(job) or {})
        if not j or j.get("uid") != me["id"]:
            return err("not found", 404)
        j.pop("uid", None)
        return jsonify({"data": j})

    @app.route("/api/proposal-studio/proposals", methods=["GET"])
    @gate
    def proposal_studio_list():
        rows = with_conn(lambda c: _rows(c, "SELECT p.id, p.number, p.title, p.status, p.price, p.sent_at, p.updated_at, "
                                            "o.name AS client_name FROM proposal p JOIN crm_organization o ON o.id = p.client_org_id "
                                            "WHERE p.status <> 'superseded' ORDER BY p.updated_at DESC LIMIT 12"))
        return jsonify({"data": rows})

    @app.route("/api/proposal-studio/<int:pid>", methods=["GET"])
    @gate
    def proposal_studio_get(pid):
        me = _me()
        v = with_conn(lambda c: view(c, pid, me["id"]))
        return jsonify({"data": v}) if v else err("not found", 404)

    @app.route("/api/proposal-studio/<int:pid>", methods=["PATCH"])
    @gate
    def proposal_studio_save(pid):
        me = _me()
        body = request.get_json(silent=True) or {}

        def run(c):
            d = dict(body.get("fields") or {})
            if "lines" in body:
                d["lines"] = body["lines"]
            _apply(c, pid, d, me["id"])
            return view(c, pid, me["id"])
        try:
            return jsonify({"data": with_conn(run)})
        except LookupError:
            return err("not found", 404)
        except ValueError as e:
            return err(str(e))

    @app.route("/api/proposal-studio/<int:pid>/revise", methods=["POST"])
    @gate
    def proposal_studio_revise(pid):
        me = _me()
        ins = str((request.get_json(silent=True) or {}).get("instruction") or "").strip()
        if not ins:
            return err("Say what to change.")

        def run(c):
            import proposals
            r = proposals.get(c, pid)
            if not r:
                raise LookupError()
            if r["status"] != "draft":
                raise ValueError("This proposal was already sent. Make a new version to change it.")
            out = revise(c, pid, ins, me["id"])
            return dict(view(c, pid, me["id"]), **out)
        try:
            return jsonify({"data": with_conn(run)})
        except LookupError:
            return err("not found", 404)
        except ValueError as e:
            return err(str(e))

    @app.route("/api/proposal-studio/<int:pid>/new-version", methods=["POST"])
    @gate
    def proposal_studio_version(pid):
        me = _me()
        try:
            import proposals
            r = with_conn(lambda c: proposals.new_version(c, pid, actor=me["id"]))
        except LookupError:
            return err("not found", 404)
        except ValueError as e:
            return err(str(e))
        return jsonify({"data": {"id": r["id"], "number": r["number"]}})

    @app.route("/api/proposal-studio/<int:pid>/send", methods=["POST"])
    @gate
    def proposal_studio_send(pid):
        me = _me()
        try:
            return jsonify({"data": with_conn(lambda c: send(c, pid, me["id"], request.get_json(silent=True) or {}))})
        except LookupError:
            return err("not found", 404)
        except ValueError as e:
            return err(str(e))

    @app.route("/api/proposal-studio/house-style", methods=["GET"])
    @gate
    def proposal_studio_style():
        return jsonify({"data": with_conn(house_style)})

    @app.route("/api/proposal-studio/house-style", methods=["PUT"])
    @gate
    def proposal_studio_style_put():
        body = request.get_json(silent=True) or {}

        def run(c):
            d = house_style(c)
            given = {i.get("key"): i for i in body.get("items") or [] if isinstance(i, dict)}
            for it in d["items"]:
                if it["key"] in given and "text" in given[it["key"]]:
                    t = str(given[it["key"]]["text"] or "").strip()[:1200]
                    if t != it.get("text"):
                        it.update(text=t, edited=True, source="edited by you")
            for k in ("learn", "show_sources"):
                if k in body:
                    d[k] = bool(body[k])
            _put_style(c, d)
            return d
        return jsonify({"data": with_conn(run)})

    @app.route("/api/proposal-studio/house-style/learn", methods=["POST"])
    @gate
    def proposal_studio_learn():
        me = _me()
        try:
            return jsonify({"data": with_conn(lambda c: learn(c, me["id"]))})
        except ValueError as e:
            return err(str(e))
