"""One proposal engine for every door (accepted prototype 2026-10-21).

Proposal Studio, the Claude window and "proposal from an email" all write proposals here, so they all come out the
same. The rule: anything that is the same on every Superstars proposal is code or a setting, never Claude.

  house     the fixed wording (app_settings 'proposal_house'): validity, SUBMITTED BY, withdraw note, mobilization,
            hours, deposit %, net days, insurance, acceptance, footer, the next proposal number. Edited once, by people.
  examples  every uploaded past proposal, read ONCE by Claude into the same structure a new proposal has
            (proposal_template.py stores it on the upload), plus the proposals sent from the app.
  library   the notes, exclusions and deliverables those examples use, by job type; people can hide or add items.
  content   one proposal's own text (proposal.content JSON) + its price lines (proposal_line, with a bold `lead`).

Claude writes only `content` + lines, as structured output (assistant._call_model(schema=...)), never dates,
numbers, totals, the deposit or house wording. proposal_layout draws the document; proposal_checks decides whether
it can be sent.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

import proposal_checks
import proposal_layout as L

log = logging.getLogger(__name__)

HOUSE_KEY = "proposal_house"
LIBRARY_KEY = "proposal_library"
JOB_TYPES = ("Rope access inspection", "Facade repairs", "Terracotta repairs", "Masonry probes", "Coating / waterproofing",
             "Roofing", "Parking garage", "Interior", "Other")
EST_TYPE = {"Rope access inspection": "IRA", "Masonry probes": "IRA", "Terracotta repairs": "IRA", "Facade repairs": "FR",
            "Coating / waterproofing": "FR", "Roofing": "FR", "Parking garage": "PG", "Interior": "IR", "Other": "OTH"}
BOROUGHS = ("MN", "BX", "BK", "QN", "SI", "LI", "NJ")
LIST_KEYS = ("submitted_to", "project", "reference", "notes", "deliverables", "additional_insureds", "exclusions")
TEXT_KEYS = ("heading", "title", "overview", "mobilization", "duration", "hours", "warranty", "accepted_by", "job_type",
             "email_subject")
UNIT_ALIASES = {"DAYS": "DAY", "D": "DAY", "EACH": "EA", "LUMP SUM": "LS", "L.S.": "LS", "SQFT": "SF", "SQ FT": "SF",
                "LIN FT": "LF", "LFT": "LF", "HRS": "HR", "HOUR": "HR", "HOURS": "HR", "WEEK": "WK", "WEEKS": "WK"}

_S = {"type": "string"}
_SL = {"type": "array", "items": _S}
LINE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["lead", "detail", "qty", "unit", "unit_price", "source"],
               "properties": {"lead": _S, "detail": _S, "qty": {"type": "number"}, "unit": {"type": "string", "enum": list(L.UNITS)},
                              "unit_price": {"type": "number"}, "source": _S}}
CONTENT_PROPS = {
    "heading": {"type": "string", "enum": list(L.HEADINGS)}, "job_type": {"type": "string", "enum": list(JOB_TYPES)},
    "title": _S, "submitted_to": _SL, "project": _SL, "reference": _SL, "overview": _S,
    "lines": {"type": "array", "items": LINE_SCHEMA}, "notes": _SL, "deliverables": _SL, "mobilization": _S,
    "duration": _S, "hours": _S, "warranty": _S, "additional_insureds": _SL, "exclusions": _SL, "accepted_by": _S,
}
LEARN_SCHEMA = {"type": "object", "additionalProperties": False, "required": list(CONTENT_PROPS) + ["client_name"],
                "properties": dict(CONTENT_PROPS, client_name=_S)}
DRAFT_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": list(CONTENT_PROPS) + ["borough", "email_subject", "email_message", "sources"],
                "properties": dict(CONTENT_PROPS, borough={"type": "string", "enum": list(BOROUGHS)}, email_subject=_S,
                                   email_message=_S, sources=_SL)}
REVISE_SCHEMA = {"type": "object", "additionalProperties": False, "required": list(CONTENT_PROPS) + ["email_message"],
                 "properties": dict(CONTENT_PROPS, email_message=_S)}

RULES = (
    "Rules for every proposal:\n"
    "- The app adds, by itself, everything that is the same on every proposal: the proposal number and date, "
    "\"Proposal Valid\", the SUBMITTED BY box, the withdraw note (always the first Important Note), mobilization and hours "
    "defaults, the deposit and payment line, the insurance sentence, the acceptance block, the first exclusion (\"Any work "
    "not specifically listed above.\") and the footer. Never write any of those, and never write totals or the deposit.\n"
    "- overview starts with \"Superstars Contracting Inc. is pleased to submit this proposal\" and is one paragraph.\n"
    "- submitted_to: one line each — the company (or person) first, then department / role lines, then the address; add "
    "\"c/o Name, Title\" when there is a contact. project: the address, then one short line describing the building or "
    "the scope limits. reference: what this answers (report, drawings, request), 1–2 lines.\n"
    "- lines: lead is the bold item name, at most four words, the way the company writes it (\"Mobilization\", \"Rope "
    "access drops\", \"Flagman\", \"Inspection report\", \"CMU crack repair\"); everything else goes in detail, in lower "
    "case, one short phrase (\"rigging and close-up physical inspection\"). Never put quantities, rates or \"daily rate\" "
    "in the text: they are columns. Units only from the list. Mobilization first, reports / tests last.\n"
    "- pricing: follow the pricing structure of the closest past proposal of the same job type, item by item: the same "
    "kinds of lines, the same units (per drop stays EA per drop, per day stays DAY, lump sums stay LS) and its rates. "
    "Use the price history only for items that proposal doesn't have. Rates the request gives always win. Say in each "
    "line's source which proposal the rate came from.\n"
    "- notes: job notes only. When some items are billed on actual quantities, say \"Items 2 and 3 are unit-priced and "
    "will be billed on actual …\" with the real item numbers, and any \"Additional X will be priced at $Y per X\" uses "
    "that line's exact rate.\n"
    "- Numbers in words and digits, the company's way: \"six (6) drops\", \"eight (8) working days\".\n"
    "- notes, exclusions, deliverables: copy library items word for word, character for character. Pick the ones that "
    "fit this job; write a new one only when nothing in the library covers it. When a library item needs this job's "
    "facts (a count, item numbers, a name, a rate), change only those words and keep every other word.\n"
    "- mobilization / hours: \"\" to use the company's standard sentence; write one only when this job differs. warranty: "
    "\"\" when there is none. additional_insureds: \"the Owner\" plus the client / engineer / managing agent named in the "
    "job. accepted_by: the client's company name as on SUBMITTED TO.\n"
    "- Plain text everywhere, no markdown. Never invent facts that aren't in the request, the files or the examples.")


# ------------------------------------------------------------------ settings
def _get(conn, key) -> dict:
    try:
        r = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        v = json.loads(r["value"]) if r and r["value"] else {}
        return v if isinstance(v, dict) else {}
    except Exception:
        conn.rollback()
        return {}


def _put(conn, key, value: dict) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                 (key, json.dumps(value), datetime.now().isoformat(timespec="seconds")))
    conn.commit()


HOUSE_EXTRA = {"next_number": 1042, "valid_days": 30}
HOUSE_EDITABLE = tuple(L.HOUSE_DEFAULTS) + tuple(HOUSE_EXTRA)


def house(conn) -> dict:
    d = dict(L.HOUSE_DEFAULTS, **HOUSE_EXTRA)
    d.update({k: v for k, v in _get(conn, HOUSE_KEY).items() if k in HOUSE_EDITABLE and v not in (None, "", [])})
    return d


def put_house(conn, given: dict) -> dict:
    cur = _get(conn, HOUSE_KEY)
    for k, v in (given or {}).items():
        if k not in HOUSE_EDITABLE:
            continue
        if k == "submitted_by":
            v = [str(x).strip()[:160] for x in (v if isinstance(v, list) else str(v or "").split("\n")) if str(x).strip()][:8]
        elif k in ("deposit_pct", "net_days", "next_number", "valid_days"):
            try:
                v = float(v) if k == "deposit_pct" else int(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k.replace('_', ' ')} must be a number")
            if v < 0 or (k == "deposit_pct" and v > 100):
                raise ValueError(f"{k.replace('_', ' ')} is out of range")
        else:
            v = str(v or "").strip()[:600]
        cur[k] = v
    _put(conn, HOUSE_KEY, cur)
    return house(conn)


# ------------------------------------------------------------------ examples + library
def examples(conn) -> list:
    """Past proposals in the content structure: the uploads Claude read, then proposals sent from the app (newest first)."""
    import proposal_template
    out = []
    for t in reversed(proposal_template._store(conn)["templates"]):
        if isinstance(t.get("example"), dict):
            out.append(dict(t["example"], _from=t.get("name") or "past proposal"))
    try:
        rows = conn.execute("SELECT id, content FROM proposal WHERE layout = 'ssc' AND sent_at IS NOT NULL "
                            "AND status <> 'superseded' ORDER BY sent_at DESC LIMIT 12").fetchall()
    except Exception:
        conn.rollback()
        rows = []
    import proposals
    for r in rows:
        c = _json(r["content"])
        if c:
            c["lines"] = [dict(li, **split_line(li)) for li in proposals.lines(conn, r["id"])]
            out.append(dict(c, _from=f"proposal sent from the app #{r['id']}"))
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9$]+", " ", str(s or "").lower()).strip()


def library(conn) -> dict:
    """{notes|exclusions|deliverables: [{text, job_types, uses, added}]} from the examples, minus hidden, plus added."""
    h = house(conn)
    lib = _get(conn, LIBRARY_KEY)
    hidden = {_norm(x) for x in lib.get("hidden") or []}
    skip = {_norm(h["withdraw_note"]), _norm(h["first_exclusion"])}
    out = {"notes": {}, "exclusions": {}, "deliverables": {}}
    for ex in examples(conn):
        for kind in out:
            for x in ex.get(kind) or []:
                k = _norm(x)
                if not k or k in hidden or k in skip:
                    continue
                it = out[kind].setdefault(k, {"text": x.strip(), "job_types": [], "uses": 0, "added": False})
                it["uses"] += 1
                if ex.get("job_type") and ex["job_type"] not in it["job_types"]:
                    it["job_types"].append(ex["job_type"])
    for a in lib.get("added") or []:
        kind, k = a.get("kind"), _norm(a.get("text"))
        if kind in out and k and k not in hidden and k not in out[kind]:
            out[kind][k] = {"text": a["text"].strip(), "job_types": [a["job_type"]] if a.get("job_type") else [], "uses": 0, "added": True}
    return {kind: sorted(v.values(), key=lambda i: -i["uses"]) for kind, v in out.items()}


def library_change(conn, *, hide: Optional[str] = None, unhide: Optional[str] = None, add: Optional[dict] = None) -> dict:
    lib = _get(conn, LIBRARY_KEY)
    lib.setdefault("hidden", [])
    lib.setdefault("added", [])
    if hide:
        lib["hidden"].append(hide)
        lib["added"] = [a for a in lib["added"] if _norm(a.get("text")) != _norm(hide)]
    if unhide:
        lib["hidden"] = [x for x in lib["hidden"] if _norm(x) != _norm(unhide)]
    if add and add.get("kind") in ("notes", "exclusions", "deliverables") and str(add.get("text") or "").strip():
        lib["added"].append({"kind": add["kind"], "text": str(add["text"]).strip()[:600], "job_type": add.get("job_type")})
        lib["hidden"] = [x for x in lib["hidden"] if _norm(x) != _norm(add["text"])]
    _put(conn, LIBRARY_KEY, lib)
    return library(conn)


def _known(conn) -> set:
    lib = library(conn)
    return {_norm(i["text"]) for kind in lib.values() for i in kind}


# ------------------------------------------------------------------ cleaning model / person output
def _json(v) -> dict:
    if isinstance(v, dict):
        return v
    try:
        d = json.loads(v or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _num(v) -> float:
    try:
        return max(0.0, float(str(v).replace("$", "").replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0.0


def unit(u) -> str:
    u = str(u or "").strip().upper().rstrip(".")
    return UNIT_ALIASES.get(u, u)


def clean_lines(raw) -> list:
    out = []
    for li in raw or []:
        if not isinstance(li, dict):
            continue
        lead, detail = str(li.get("lead") or "").strip()[:120], str(li.get("detail") or "").strip()[:300]
        if not lead and not detail and li.get("description"):
            lead, detail = "", str(li["description"]).strip()[:300]
        if not lead and not detail:
            continue
        out.append({"lead": lead, "detail": detail, "qty": _num(li.get("qty")), "unit": unit(li.get("unit")),
                    "unit_price": _num(li.get("unit_price")), "source": str(li.get("source") or "")[:200]})
    return out[:60]


def clean_content(d: dict, h: dict) -> dict:
    out = {}
    for k in TEXT_KEYS:
        if k in d:
            out[k] = str(d[k] or "").strip()[:4000]
    for k in LIST_KEYS:
        if k in d:
            items = d[k] if isinstance(d[k], list) else str(d[k] or "").split("\n")
            out[k] = [re.sub(r"^\s*[•\-\*]\s*", "", str(x)).strip()[:600] for x in items if str(x or "").strip()][:40]
    if "heading" in out:
        out["heading"] = out["heading"].upper() if out["heading"].upper() in L.HEADINGS else "ESTIMATE"
    if "job_type" in out and out["job_type"] not in JOB_TYPES:
        out["job_type"] = "Other"
    if "notes" in out:
        out["notes"] = [x for x in out["notes"] if _norm(x) != _norm(h["withdraw_note"])]
    if "exclusions" in out:
        out["exclusions"] = [x for x in out["exclusions"] if _norm(x) != _norm(h["first_exclusion"])]
    for k in ("mobilization", "hours"):
        if k in out and _norm(out[k]) == _norm(h[k]):
            out[k] = ""
    return out


# ------------------------------------------------------------------ lines <-> proposal_line
def split_line(li: dict) -> dict:
    """proposal_line keeps the full text in `description` (every other screen reads it) and the bold part in `lead`."""
    lead = (li.get("lead") or "").strip()
    desc = (li.get("description") or "").strip()
    if lead and desc.lower().startswith(lead.lower()):
        return {"lead": lead, "detail": desc[len(lead):].lstrip(" ,").strip()}
    return {"lead": lead, "detail": desc if not lead else desc}


def join_line(li: dict) -> str:
    lead, detail = (li.get("lead") or "").strip(), (li.get("detail") or "").strip()
    if not lead:
        return detail
    if not detail:
        return lead
    return lead + (" " if L._joins_plain(detail) else ", ") + detail


# ------------------------------------------------------------------ the proposal as a document
def label(r: dict) -> str:
    """The number people see: 1042, and 1042-2 for a second version."""
    n = r.get("display_no")
    if not n:
        return r.get("number") or ""
    return f"{n}" + (f"-{r['version']}" if (r.get("version") or 1) > 1 else "")


def next_number(conn) -> int:
    try:
        m = conn.execute("SELECT MAX(display_no) AS m FROM proposal").fetchone()["m"]
    except Exception:
        conn.rollback()
        m = None
    return max(int(house(conn)["next_number"]), int(m or 0) + 1)


def content(r: dict) -> dict:
    return _json(r.get("content"))


def doc(conn, r: dict, lis: Optional[list] = None) -> dict:
    import proposals
    c = content(r)
    rows = proposals.lines(conn, r["id"]) if lis is None else lis
    d = {k: c.get(k) for k in TEXT_KEYS + LIST_KEYS if k in c}
    d.update(number=label(r), date=(r.get("sent_at") or date.today().isoformat())[:10],
             lines=[dict(li, **split_line(li)) if "detail" not in li else li for li in rows],
             new_clauses=new_clauses(conn, c))
    d["title"] = r.get("title") or c.get("title") or ""
    return d


def new_clauses(conn, c: dict) -> list:
    """Notes / exclusions / deliverables that aren't in the library yet and nobody has OK'd."""
    known, acked = _known(conn), {_norm(x) for x in c.get("acked") or []}
    out = []
    for kind in ("notes", "exclusions", "deliverables"):
        for x in c.get(kind) or []:
            k = _norm(x)
            if k and k not in known and k not in acked:
                out.append(x)
    return out


def house_for(conn, r: dict) -> dict:
    """The house wording, with this proposal's own deposit % (set from the house when it was made)."""
    h = house(conn)
    if r.get("deposit_pct") is not None:
        h["deposit_pct"] = float(r["deposit_pct"])
    return h


def render(conn, r: dict, mode: str = "pdf", lis: Optional[list] = None) -> str:
    import company_brand
    h = house_for(conn, r)
    d = doc(conn, r, lis)
    signed = None
    if r.get("status") == "signed":
        signed = {"name": r.get("signed_name"), "title": r.get("signed_title"), "at": r.get("signed_at")}
    return L.render(d, h, mode=mode, logo_uri=company_brand.logo_data_uri(conn), signed=signed,
                    flagged=d["new_clauses"] if mode == "edit" else None)


def checks(conn, r: dict) -> list:
    return proposal_checks.check(doc(conn, r), house_for(conn, r))


def blocked(conn, r: dict) -> Optional[str]:
    """None when it can be sent, else the first red item."""
    if r.get("layout") != "ssc":
        return None
    bad = [x["text"] for x in checks(conn, r) if x["level"] == "block"]
    return ("Fix the checklist first: " + bad[0]) if bad else None


# ------------------------------------------------------------------ saving
def save(conn, pid: int, patch: dict, lines: Optional[list] = None, *, actor=None) -> dict:
    """A person's (or Claude's) change to a draft: content keys and/or the whole line list."""
    import proposals
    r = proposals.get(conn, pid)
    if not r:
        raise LookupError("proposal not found")
    if r["status"] != "draft":
        raise ValueError("This proposal was already sent. Make a new version to change it.")
    h = house(conn)
    c = content(r)
    c.update(clean_content(patch or {}, h))
    if "acked" in (patch or {}):
        c["acked"] = [str(x)[:600] for x in patch["acked"]][:60]
    if "email_message" in (patch or {}):
        c["email_message"] = str(patch["email_message"] or "")[:4000]
    fields = {"title": c.get("title") or r.get("title"), "scope_summary": c.get("overview") or "",
              "exclusions_text": "\n".join(c.get("exclusions") or []), "schedule_text": c.get("duration") or ""}
    if "email_message" in c:
        fields["email_message"] = c["email_message"]
    li = None
    if lines is not None:
        li = [{"description": join_line(x), "lead": x["lead"], "qty": x["qty"], "unit": x["unit"] or None,
               "unit_price": x["unit_price"], "amount": round(x["qty"] * x["unit_price"], 2)} for x in clean_lines(lines)]
        fields["price"] = round(sum(x["amount"] for x in li), 2)
    proposals.update(conn, pid, fields, line_items=li, actor=actor)
    conn.execute("UPDATE proposal SET content = ? WHERE id = ?", (json.dumps(c), pid))
    conn.commit()
    return proposals.get(conn, pid)


def seed(conn, r: dict, *, org: Optional[dict] = None, contact: Optional[dict] = None) -> dict:
    """The starting content of a proposal made from an estimate (no Claude): the parties and the address."""
    org = org or {}
    to = [org.get("name") or r.get("client_name") or ""]
    if contact and contact.get("full_name"):
        to.append(f"c/o {contact['full_name']}" + (f", {contact['title']}" if contact.get("title") else ""))
    addr = r.get("building_address") or ""
    if addr:   # their proposals put the building's address under the client unless the client's office is given
        to.append(addr)
    return {"heading": "ESTIMATE", "title": r.get("title") or "", "submitted_to": [x for x in to if x],
            "project": [addr] if addr else [], "reference": [], "overview": "", "notes": [], "deliverables": [],
            "additional_insureds": ["the Owner"] + ([org["name"]] if org.get("name") else []), "exclusions": [],
            "accepted_by": org.get("name") or ""}


# ------------------------------------------------------------------ Claude
def _closest(exs: list, job_type: Optional[str], n: int = 3) -> list:
    same = [e for e in exs if e.get("job_type") == job_type]
    rest = [e for e in exs if e.get("job_type") != job_type]
    return (same + rest)[:n]


def _strip(e: dict) -> dict:
    return {k: v for k, v in e.items() if not k.startswith("_") and k not in ("acked",)}


def _library_text(lib: dict, job_type: Optional[str]) -> str:
    out = []
    for kind in ("notes", "exclusions", "deliverables"):
        items = sorted(lib[kind], key=lambda i: (job_type not in i["job_types"], -i["uses"]))[:40]
        out.append(f"{kind}:\n" + "\n".join(f"- {i['text']}" + (f"  [{', '.join(i['job_types'])}]" if i["job_types"] else "")
                                             for i in items))
    return "\n".join(out)


def _prices(exs: list) -> str:
    rows = []
    for e in exs[:15]:
        for li in e.get("lines") or []:
            rows.append(f"{e.get('job_type') or ''} · {li.get('lead') or ''} {li.get('detail') or ''} — "
                        f"{'%g' % _num(li.get('qty'))} {li.get('unit') or ''} @ ${_num(li.get('unit_price')):,.2f} ({e.get('_from')})")
    return "\n".join(rows[:120])


def _house_text(h: dict) -> str:
    return "\n".join(f"{k}: {h[k] if not isinstance(h[k], list) else ' / '.join(h[k])}"
                     for k in ("valid_text", "submitted_by", "withdraw_note", "mobilization", "hours", "deposit_pct",
                               "net_days", "insurance", "first_exclusion", "acceptance"))


def _model(system: str, blocks: list, schema: dict, conn, uid, title: str) -> dict:
    import assistant
    import proposal_studio
    last = None
    for attempt in (1, 2):   # one more try when the reply is cut off or the call drops
        try:
            res = assistant._call_model([{"type": "text", "text": system}], [{"role": "user", "content": blocks}], schema=schema)
        except Exception as x:
            last = x
            log.info("proposal engine call failed (%s), attempt %s", type(x).__name__, attempt)
            continue
        proposal_studio._usage(conn, uid, res, title)
        if res.get("refused"):
            raise ValueError("Claude couldn't write that one. Try describing the job another way.")
        if isinstance(res.get("data"), dict):
            return res["data"]
        last = ValueError("no structured reply")
    log.warning("proposal engine gave up: %s", type(last).__name__ if last else "unknown")
    raise ValueError("Claude couldn't finish it. Try again in a minute.")


def context_blocks(conn, uid, req: dict) -> list:
    """What Claude reads to write a new proposal."""
    import proposal_studio
    h = house(conn)
    exs = examples(conn)
    jt = req.get("job_type")
    close = _closest(exs, jt)
    org = None
    if req.get("client_org_id"):
        org = proposal_studio._one(conn, "SELECT id, name FROM crm_organization WHERE id = ?", (req["client_org_id"],))
    client = f"Existing client: {org['name']}" if org else f"New client: {req.get('client_name')}"
    if org:
        cs = proposal_studio._rows(conn, "SELECT full_name, title, email FROM crm_contact WHERE org_id = ? "
                                         "AND COALESCE(status,'active') <> 'inactive' LIMIT 8", (org["id"],))
        client += ("\nContacts: " + "; ".join(f"{c['full_name']}{' (' + c['title'] + ')' if c.get('title') else ''}" for c in cs)) if cs else ""
    txt = (f"<fixed_wording_the_app_adds>\n{_house_text(h)}\n</fixed_wording_the_app_adds>\n"
           f"<library>\n{_library_text(library(conn), jt)}\n</library>\n"
           f"<closest_past_proposals>\n{json.dumps([_strip(e) for e in close], indent=1)[:60000] or '(none yet)'}\n</closest_past_proposals>\n"
           f"<price_history>\n{_prices(exs) or '(none yet)'}\n</price_history>\n"
           f"<client>\n{client}\nBuilding: {req.get('building_address') or '(not given)'}\nJob type: {jt or '(decide)'}\n"
           f"Heading: {req.get('heading') or 'ESTIMATE'}\n</client>\n")
    if req.get("mail_id"):
        import inbox
        m = inbox.message(conn, int(req["mail_id"]), uid)
        if m:
            txt += f"<email from=\"{m['from']}\" subject=\"{m['subject']}\">\n{proposal_studio._clip(m['body'], 8000)}\n</email>\n"
    blocks = [{"type": "text", "text": txt}]
    if req.get("file_ids"):
        import claude_files
        for r in claude_files.owned(conn, uid, req["file_ids"]):
            blocks.extend(claude_files.blocks(conn, r))
    blocks.append({"type": "text", "text": f"<request>\n{req['what']}\n</request>\n\nWrite this proposal. The first of the closest past proposals "
                                           f"(same job type) sets the line structure and pricing. Copy the closest past "
                                           f"proposals' structure, order and wording; only the facts of this job change.\n\n{RULES}"})
    return blocks


SYSTEM = ("You write construction proposals for Superstars Contracting Inc. (NYC facade restoration and rope access), "
          "in its own voice, as structured data the app draws in the company's fixed layout.")


def fake_draft(req: dict) -> dict:
    """SSC_ASSISTANT_FAKE: a complete, checklist-clean draft shaped like the company's real proposals."""
    who = req.get("client_name") or "the client"
    addr = req.get("building_address") or "21 West Street, New York, NY 10006"
    return {"heading": req.get("heading") or "ESTIMATE", "job_type": req.get("job_type") or "Rope access inspection",
            "borough": "MN", "title": f"{addr.split(',')[0]} - Rope Access Inspections",
            "submitted_to": [who, addr], "project": [addr, "Close-up inspection at the marked drop locations"],
            "reference": ["Marked-up elevations, approximate drop locations"],
            "overview": "Superstars Contracting Inc. is pleased to submit this proposal to provide Industrial Rope Access (IRA) "
                        f"close-up inspections at {addr.split(',')[0]}. " + req["what"][:200],
            "lines": [{"lead": "Mobilization", "detail": "site safety plan, rope access plan and permits", "qty": 1, "unit": "LS", "unit_price": 3500, "source": "1041"},
                      {"lead": "Rope access drops", "detail": "rigging and close-up physical inspection", "qty": 6, "unit": "EA", "unit_price": 4500, "source": "1041"},
                      {"lead": "Flagman", "detail": "and ground control, sidewalk barricades", "qty": 8, "unit": "DAY", "unit_price": 650, "source": "1041"},
                      {"lead": "Inspection report", "detail": "deficiencies marked on PDF, full photo log", "qty": 1, "unit": "LS", "unit_price": 2500, "source": "1041"}],
            "notes": ["Scope is limited to six (6) drops at the approximate locations marked on the elevations. Items 2 and 3 are "
                      "unit-priced and will be billed on actual drops and days performed. Additional drops will be priced at $4,500.00 per drop.",
                      "Rope access is weather dependent and will not be performed in rain, high wind, or other unsafe conditions."],
            "deliverables": ["Close-up inspection of all areas indicated.", "All photos taken during the inspections."],
            "mobilization": "", "duration": "Approximately eight (8) working days for six (6) drops, weather permitting.",
            "hours": "", "warranty": "", "additional_insureds": ["the Owner", who],
            "exclusions": ["Facade repairs of any kind.", "Sidewalk shed, scaffolding, or netting."], "accepted_by": who,
            "email_subject": f"Proposal · {addr.split(',')[0]}", "email_message": "Hi,\n\nOur proposal is attached.\n\nThanks",
            "sources": ["Your past proposal 1041"]}


def write(conn, uid, req: dict) -> dict:
    """Claude writes the content of a new proposal (not saved yet)."""
    import assistant
    if assistant._fake():
        return fake_draft(req)
    return _model(SYSTEM, context_blocks(conn, uid, req), DRAFT_SCHEMA, conn, uid, "Proposal Studio draft")


def create(conn, uid, req: dict, d: dict) -> int:
    """A client (new or existing) + estimate + proposal in the locked layout, filled with Claude's content."""
    import crm
    import estimates
    import proposals
    org_id = req.get("client_org_id")
    if not org_id:
        org_id = crm.create_org(conn, name=req["client_name"], relationship_type="client", created_by=uid)
    et = EST_TYPE.get(d.get("job_type") or req.get("job_type"), "OTH")
    et = et if et in estimates.EST_TYPES else "FR"
    bo = str(d.get("borough") or "MN").upper()
    bo = bo if bo in estimates.BOROUGHS else "MN"
    est = estimates.create_estimate(conn, est_type=et, borough=bo, client_org_id=org_id,
                                    building_address=req.get("building_address") or (d.get("project") or [""])[0],
                                    notes="Drafted in Proposal Studio: " + str(req.get("what") or "")[:400], created_by=uid)
    p = proposals.create_from_estimate(conn, est["id"], created_by=uid)
    patch = {k: d[k] for k in TEXT_KEYS + LIST_KEYS if k in d}
    if req.get("heading"):
        patch["heading"] = req["heading"]
    patch["email_message"] = d.get("email_message") or ""
    save(conn, p["id"], patch, d.get("lines") or [], actor=uid)
    conn.execute("UPDATE proposal SET studio = 1, sources = ?, source_mail_id = ? WHERE id = ?",
                 (json.dumps([str(s)[:200] for s in (d.get("sources") or [])][:8]), req.get("mail_id"), p["id"]))
    proposals._event(conn, p["id"], "drafted", actor=uid, detail="drafted by Claude in Proposal Studio")
    conn.commit()
    return p["id"]


def snapshot(conn, pid: int) -> dict:
    import proposals
    r = proposals.get(conn, pid)
    c = content(r)
    out = {k: c.get(k, [] if k in LIST_KEYS else "") for k in CONTENT_PROPS if k != "lines"}
    out["title"] = r.get("title") or c.get("title") or ""
    out["email_message"] = c.get("email_message") or r.get("email_message") or ""
    out["lines"] = [{k: v for k, v in dict(li, **split_line(li)).items() if k in ("lead", "detail", "qty", "unit", "unit_price")}
                    for li in proposals.lines(conn, pid)]
    out["acked"] = c.get("acked") or []
    return out


REVISE_SYSTEM = ("You edit a Superstars Contracting proposal before it is sent. Apply the person's request to the proposal "
                 "below and return the complete proposal (every key). Keep everything they didn't ask to change exactly as it "
                 "is, word for word. When a change affects other parts (a quantity mentioned in the notes or the duration, "
                 "item numbers in a unit-price note), update those too so the proposal stays consistent.")


def revise(conn, pid: int, instruction: str, uid) -> dict:
    import assistant
    before = snapshot(conn, pid)
    payload = {k: before[k] for k in list(CONTENT_PROPS) + ["email_message"] if k in before}
    if assistant._fake():
        out = json.loads(json.dumps(payload))
        m = re.search(r"(\d+)\s*%", instruction or "")
        if m:
            out["notes"] = out["notes"] + [f"Due to concealed or unforeseen conditions, the Owner is advised to carry a "
                                            f"{m.group(1)}% contingency. Any additional work will be priced by change order."]
    else:
        import proposal_studio
        proposal_studio._claude_ready(conn)
        txt = (f"<rules>\n{RULES}\n</rules>\n<proposal>\n{json.dumps(payload, indent=1)}\n</proposal>\n"
               f"<request>\n{instruction[:2000]}\n</request>")
        out = _model(REVISE_SYSTEM, [{"type": "text", "text": txt}], REVISE_SCHEMA, conn, uid, "Proposal Studio change")
    patch = {k: out[k] for k in out if k != "lines"}
    save(conn, pid, patch, out.get("lines") if "lines" in out else None, actor=uid)
    after = snapshot(conn, pid)
    changed = [k for k in list(CONTENT_PROPS) + ["email_message"]
               if json.dumps(before.get(k), sort_keys=True, default=str) != json.dumps(after.get(k), sort_keys=True, default=str)]
    return {"changed": changed, "before": before}


def undo(conn, pid: int, before: dict, uid) -> dict:
    patch = {k: v for k, v in (before or {}).items() if k != "lines"}
    return save(conn, pid, patch, before.get("lines"), actor=uid)


# ------------------------------------------------------------------ learning a past proposal
LEARN_PROMPT = ("This is one of the company's own past proposals. Record it exactly as written, in the structure given: "
                "every line of SUBMITTED TO, PROJECT and REFERENCE; the overview paragraph; each Base Bid line split into the "
                "bold lead and the rest (detail), with quantity, unit and rate; every Important Note except the first "
                "\"reserves the right to withdraw\" one; deliverables; the Schedule & Terms rows (mobilization, duration, hours, "
                "warranty — \"\" when a row isn't there); the names on the COI (additional_insureds); every exclusion except "
                "\"Any work not specifically listed above.\"; and who it's ACCEPTED BY. Copy the wording word for word. "
                "source on each line: \"\". client_name: the client company.")


def learn(raw_block: dict, conn=None, uid=None) -> dict:
    """Claude reads one past proposal (a PDF / image / text block) into the content structure."""
    import assistant
    if assistant._fake():
        return {"heading": "ESTIMATE", "job_type": "Rope access inspection", "title": "Past proposal", "client_name": "Past client",
                "submitted_to": ["Past client"], "project": [], "reference": [], "overview": "",
                "lines": [{"lead": "Rope access drops", "detail": "rigging and close-up physical inspection", "qty": 6,
                           "unit": "EA", "unit_price": 4500, "source": ""}],
                "notes": ["Rope access is weather dependent and will not be performed in rain, high wind, or other unsafe conditions."],
                "deliverables": ["All photos taken during the inspections."], "mobilization": "", "duration": "", "hours": "",
                "warranty": "", "additional_insureds": ["the Owner"], "exclusions": ["Facade repairs of any kind."],
                "accepted_by": "Past client"}
    d = _model("You read a construction company's proposal into structured data, word for word.",
               [raw_block, {"type": "text", "text": LEARN_PROMPT}], LEARN_SCHEMA, conn, uid, "Proposal read")
    h = dict(L.HOUSE_DEFAULTS)
    out = clean_content(d, h)
    out["lines"] = clean_lines(d.get("lines"))
    out["client_name"] = str(d.get("client_name") or "")[:200]
    return out
