"""Suggested documents — what else a job may need, read off what is already filed.

Runs over the job's CURRENT filed documents (approved tag, whose record it is, and
what Claude read off the pages) and proposes other documents. Three sources:

  rule     — a filed type that usually comes with another (a subcontract -> that
             sub's COI; a G702 pay app -> a lien waiver; a BEST permit -> rigging
             engineering)
  content  — words on the pages (Claude's summary / key facts / fields) that point
             at a document ("performance bond", "after-hours", "special inspection")
  claude   — documents Claude saw the file name, attach, or call for (scan `related`)

A suggestion is NEVER called required unless the app can verify it: `on_checklist`
is true only when the suggested type fulfils an item on this job's required
checklist (document_requirements) that is still missing, and the document would be
our company's (a subcontractor's record never fills the job's own checklist). Everything else is
"may be needed". Already-filed documents (same type, same party) and suggestions
someone dismissed are left out.

Pure — no DB, no API. server.py feeds it and serves the result with the documents.
"""
from __future__ import annotations

import re

import doc_intake
import doc_types

CATEGORY_ORDER = ("PERMITS", "DRAWINGS", "CONTRACTS", "INSPECTIONS", "SAFETY", "CLOSEOUT")

# (filed type, suggested type, why) — documents that usually travel together.
RULES = (
    ("subcontract", "coi", "A subcontractor working on the job should carry its own certificate of insurance."),
    ("subcontract", "lien_waiver", "Collect the subcontractor's lien waiver as it gets paid."),
    ("owner_contract", "sov", "Progress billing on the contract runs against a Schedule of Values."),
    ("owner_contract", "coi", "Owners usually ask for the contractor's certificate of insurance before work starts."),
    ("aia_g702", "sov", "A G702/G703 pay application bills against the Schedule of Values."),
    ("aia_g702", "lien_waiver", "Owners usually ask for a lien waiver with each pay application."),
    ("pw2", "approved_arch", "A DOB work permit is issued against approved plans."),
    ("best_permit", "scaffold_eng", "Suspended-scaffold work needs the scaffold & rigging engineering on site."),
    ("best_permit", "drop_layout", "A drop / rigging layout shows where each scaffold drop hangs."),
    ("fisp_report", "fisp_ack", "A filed FISP report is confirmed by the DOB acknowledgment."),
    ("rope_access_plan", "sst", "Workers on rope need their SST cards on file."),
    ("site_safety_plan", "toolbox_log", "Toolbox-talk sign-ins show the safety plan is being briefed."),
    ("product_data", "sds", "Products used on site need their Safety Data Sheets on file."),
    ("shop_drawings", "product_data", "Submittals are usually backed by the product data sheets."),
    ("violation", "dob_signoff", "Clearing a violation needs a DOB sign-off or certificate of correction."),
    ("completion_letter", "warranty", "Closeout usually includes the warranties."),
    ("completion_letter", "lien_waiver", "A final lien waiver usually goes with the closeout package."),
)

# (pattern on the read text, suggested type) — case-insensitive.
KEYWORDS = tuple((re.compile(p, re.I), k) for p, k in (
    (r"\bafter[- ]hours?\b|\bweekend work\b", "after_hours"),
    (r"\bsidewalk[- ]shed\b", "sidewalk_shed"),
    (r"\bsuspended scaffold|\bswing[- ]stage", "best_permit"),
    (r"\brope[- ]access\b", "rope_access_plan"),
    (r"\bspecial inspections?\b|\bTR-?1\b", "tr1"),
    (r"\b(?:performance|payment) bond\b", "bond"),
    (r"\bwarrant(?:y|ies)\b", "warranty"),
    (r"\blien waivers?\b|\brelease of liens?\b", "lien_waiver"),
    (r"\badditional insured\b|\bcertificate of insurance\b", "coi"),
    (r"\bschedule of values\b", "sov"),
))

# Types that belong to one party: a sub's COI is not ours, and ours is not theirs.
PARTY_TYPES = {"coi", "lien_waiver", "warranty", "subcontract", "bond"}


def _read_text(d) -> str:
    a = d.get("ai") or {}
    bits = [a.get("summary") or ""]
    for f in (a.get("key_facts") or []) + (a.get("fields") or []):
        bits.append(f"{f.get('label', '')}: {f.get('value', '')}")
    return "\n".join(bits)


def _target_party(trigger, suggested_key):
    """Who the suggested document should belong to."""
    if suggested_key in PARTY_TYPES and (trigger.get("party") == "subcontractor"
                                         or trigger.get("type_key") == "subcontract"):
        name = trigger.get("party_name") if trigger.get("party") == "subcontractor" else None
        return "subcontractor", name
    return "company", None


def _filed(docs, t, party, name) -> bool:
    for d in docs:
        same_type = d.get("type_key") == t["key"] or (
            not d.get("type_key") and t["requirement_key"]
            and d.get("category") == t["category"] and d.get("requirement_key") == t["requirement_key"])
        if not same_type:
            continue
        if t["key"] not in PARTY_TYPES:
            return True
        if party == "subcontractor" and not name:   # "a subcontractor's" — any sub's will do
            if d.get("party") == "subcontractor":
                return True
        elif doc_intake.same_party(d.get("party"), d.get("party_name"), party, name):
            return True
    return False


def key_for(type_key, party, name) -> str:
    return f"{type_key}|{party or 'company'}|{(name or '').strip().lower()}"


def suggest(docs, missing_reqs, dismissed=()):
    """docs: current filed docs (dicts: id, title, type_key, category, requirement_key,
    party, party_name, ai). missing_reqs: {(category, requirement_key)} still missing
    on the checklist. dismissed: suggestion keys marked not needed. Returns a list."""
    dismissed = set(dismissed or ())
    out = {}

    def add(trigger, key, reason, origin):
        t = doc_types.get(key)
        if not t:
            return
        party, name = _target_party(trigger, key)
        if _filed(docs, t, party, name):
            return
        k = key_for(key, party, name)
        if k in dismissed:
            return
        src = {"id": trigger.get("id"), "title": trigger.get("title")}
        if k in out:
            if src not in out[k]["sources"]:
                out[k]["sources"].append(src)
            return
        out[k] = {
            "key": k, "type_key": key, "label": t["label"], "category": t["category"],
            "party": party, "party_name": name, "reason": reason, "origin": origin,
            # the checklist is the job's own: a sub's document never fills it, so never "required"
            "on_checklist": (party == "company" and bool(t["requirement_key"])
                             and (t["category"], t["requirement_key"]) in missing_reqs),
            "sources": [src],
        }

    for d in docs:
        tk = d.get("type_key")
        for trig, key, why in RULES:
            if tk == trig:
                add(d, key, why, "rule")
        for r in ((d.get("ai") or {}).get("related") or []):
            add(d, r.get("type_key"), str(r.get("reason") or "")[:200] or "Referenced in this document.", "claude")
        text = _read_text(d)
        if text.strip():
            for rx, key in KEYWORDS:
                if key == tk:
                    continue
                m = rx.search(text)
                if m:
                    add(d, key, f"“{m.group(0)}” appears in {d.get('title') or 'a filed document'}.", "content")

    return sorted(out.values(), key=lambda s: (not s["on_checklist"], CATEGORY_ORDER.index(s["category"])
                                               if s["category"] in CATEGORY_ORDER else 99, s["label"]))
