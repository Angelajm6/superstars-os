"""Suggested documents — what else a job may need, read off what is already filed.

Runs over the job's CURRENT filed documents (approved tag, whose record it is, and
what Claude read off the pages) and proposes other documents. Three sources:

  rule     — a filed type that usually comes with another (a subcontract -> that
             sub's COI; a G702 pay app -> a lien waiver; a BEST permit -> rigging
             engineering)
  content  — words on the pages (Claude's summary / key facts / fields) that point
             at a document ("after-hours", "special inspection", "lien waiver")
  claude   — documents Claude saw the file name, attach, or call for (scan `related`)

A suggestion is NEVER called required unless the app can verify it: `on_checklist`
is true only when the suggested type fulfils an item on this job's required
checklist (document_requirements) that is still missing, and the document would be
our company's (a subcontractor's record never fills the job's own checklist). Everything else is
"may be needed". Already-filed documents (same type, same party) and suggestions
someone dismissed are left out.

The job decides what it needs (`plan`): its scope (exterior, rope access inspection or
repair, garage, interior, other), its access methods, how it is billed, whether the Drop
Plan has drops, and what is already filed (a PW2 brings in SST cards and the DOB sign-off;
a BEST permit the rigging engineering and drop layout). Each item is needed NOW, LATER (at
closeout) or MAYBE (with its condition). The items the job still misses come back as
suggestions (origin 'checklist'), so the Filed list only shows documents actually filed.

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
    ("aia_g702", "sov", "A G702/G703 payment application bills against the Schedule of Values."),
    ("aia_g702", "lien_waiver", "Owners usually ask for a lien waiver with each payment application."),
    ("sov", "aia_g702", "Billing against a Schedule of Values goes out as payment applications."),
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
    (r"\bwarrant(?:y|ies)\b", "warranty"),
    (r"\blien waivers?\b|\brelease of liens?\b", "lien_waiver"),
    (r"\badditional insured\b|\bcertificate of insurance\b", "coi"),
    (r"\bschedule of values\b", "sov"),
    (r"\bpay(?:ment)? app(?:lication)?s?\b|\bG702\b", "aia_g702"),
))

# Types that belong to one party: a sub's COI is not ours, and ours is not theirs.
PARTY_TYPES = {"coi", "lien_waiver", "warranty", "subcontract"}

# ---- what the job needs (2026-10-26): its scope, access methods, billing and what is filed ----
# ctx = {project_type: facade|garage|interiors|ira|generic, ra_scope: inspection|repair (rope access jobs),
#        access: [..] | None (never set), billing_type: aia_sov|lump_sum|…|None, has_drops: bool,
#        filed: {requirement keys of OUR current documents}, near_end: bool (closeout is due)}
# Each scope lists what it needs NOW (before / during the work), LATER (closeout) and MAYBE (depends on
# something the app can't know, shown with its condition). Reviewed against NYC BC Ch. 33 (2026-10-26):
# whether a job needs a PW2, TR1 or Site Safety Plan is still the filing architect's / expediter's call.
SCOPE_LABELS = {"facade": "Exterior Restoration", "ra_inspection": "Rope access · Inspection",
                "ra_repair": "Rope access · Repair work", "garage": "Parking Garage",
                "interiors": "Interior Repair", "generic": "Other"}
CORE_NOW = ("owner_contract", "coi", "toolbox_log")   # toolbox: daily pre-shift sign-in (BC 3301.12)
CORE_LATER = ("completion_letter",)
SCOPES = {
    "facade": {"now": ("pw2", "approved_arch", "shop_drawings", "sds"),
               "later": ("dob_signoff", "warranties", "lien_waivers"),
               "maybe": (("tr1", "If the permit lists special inspections"),
                         ("site_safety_plan", "Buildings 10+ stories or 125+ ft"),
                         ("fisp_report", "If this is a FISP / Local Law 11 job"),
                         ("probe_report", "If probes are in the scope"))},
    "ra_inspection": {"now": ("rope_access_plan", "drop_layout", "fisp_report"), "later": (),
                      "maybe": (("scaffold_eng", "Anchor certification, if the owner has none on file"),),
                      "note": {"fisp_report": "or a Probe / Condition Report"}},
    "ra_repair": {"now": ("rope_access_plan", "drop_layout", "shop_drawings", "sds"),
                  "later": ("warranties", "lien_waivers"),
                  "maybe": (("pw2", "If the repair is filed with DOB"),
                            ("scaffold_eng", "Anchor certification, if the owner has none on file"))},
    "garage": {"now": ("pw2", "approved_struct", "tr1", "shop_drawings", "sds"),
               "later": ("dob_signoff", "warranties", "lien_waivers"),
               "maybe": (("probe_report", "The Local Law 126 condition report"), ("scaffold_eng", "If shoring is needed"))},
    "interiors": {"now": ("sds",), "later": ("warranties", "lien_waivers"),
                  "maybe": (("pw2", "Unless it is an ordinary repair"), ("approved_arch", "If the job is filed with plans"))},
    "generic": {"now": ("pw2", "approved_struct", "tr1", "sds"), "later": ("dob_signoff", "lien_waivers"),
                "maybe": (("scaffold_eng", "Shoring / underpinning by a PE"),)},
}
# never suggested on that scope, even when a document mentions it (a filed one still shows in Filed)
NEVER = {
    "ra_inspection": {"pw2", "best_permit", "approved_arch", "approved_struct", "shop_drawings", "tr1", "dob_signoff",
                      "site_safety_plan", "sds", "warranties", "final_fisp"},
    "ra_repair": {"best_permit"},
    "garage": {"best_permit", "rope_access_plan", "drop_layout", "fisp_ack", "fisp_report", "final_fisp"},
    "interiors": {"sidewalk_shed", "best_permit", "rope_access_plan", "drop_layout", "scaffold_eng",
                  "fisp_ack", "fisp_report", "final_fisp"},
    "generic": {"best_permit", "rope_access_plan", "drop_layout", "fisp_ack", "fisp_report", "final_fisp"},
}
ACCESS_NEEDS = {"suspended_scaffold": ("best_permit", "scaffold_eng", "drop_layout"),
                "supported_scaffold": ("sidewalk_shed", "scaffold_eng"),
                "rope_access": ("rope_access_plan", "drop_layout"), "boom_lift": ()}
ACCESS_GROUP = {"suspended_scaffold": "Suspended scaffold", "supported_scaffold": "Supported scaffold",
                "rope_access": "Rope access", "boom_lift": "Boom lift"}
# a filed document (its checklist item) -> what it brings in
TRIGGERS = {
    "pw2": {"label": "PW2 work permit", "now": ("sst_records",), "later": ("dob_signoff",)},   # LL196 SST follows the permit
    "tr1": {"label": "TR1", "later": ("dob_signoff",)},
    "best_permit": {"label": "BEST permit", "now": ("scaffold_eng", "drop_layout")},
    "sidewalk_shed": {"label": "sidewalk-shed permit", "now": ("scaffold_eng",)},
    "site_safety_plan": {"label": "Site Safety Plan", "now": ("sst_records",)},
    "fisp_report": {"label": "FISP report", "now": ("fisp_ack",),
                    "maybe": (("final_fisp", "If the report rates the facade Unsafe"),
                              ("sidewalk_shed", "If rated Unsafe: right away"))},
    "pay_app": {"label": "payment application", "later": ("lien_waivers",)},
}
DROP_ITEMS = ("best_permit", "scaffold_eng", "drop_layout")
BILLING_ITEMS = ("sov", "pay_app")
# On the checklist but never required: suggested only when a document points at it.
NOT_REQUIRED = {"after_hours"}


def _access(ctx) -> list:
    return list(ctx.get("access") or [])


def _aia(ctx) -> bool:
    return ctx.get("billing_type") in (None, "", "aia_sov")


def scope_of(ctx) -> str:
    pt = (ctx or {}).get("project_type")
    if pt == "ira":
        return "ra_repair" if ctx.get("ra_scope") == "repair" else "ra_inspection"
    return pt if pt in SCOPES else "generic"


def plan(ctx) -> dict:
    """What this job needs: {"items": {requirement_key: {"phase": now|later, "group": why}},
    "maybe": {requirement_key: condition}, "note": {requirement_key: hint}, "scope": key}.
    A key appears once, under the first reason that brings it in."""
    sc_key = scope_of(ctx)
    sc, filed, items, maybe = SCOPES[sc_key], set(ctx.get("filed") or ()), {}, {}

    def need(keys, phase, group):
        for k in keys:
            if k not in items:
                items[k] = {"phase": phase, "group": group}
    need(CORE_NOW + (BILLING_ITEMS if _aia(ctx) else ()), "now", "Every job")
    need(sc["now"], "now", SCOPE_LABELS[sc_key])
    acc = _access(ctx)
    for a in acc:
        need(ACCESS_NEEDS.get(a, ()), "now", ACCESS_GROUP.get(a, a))
    if ctx.get("has_drops") and "suspended_scaffold" not in acc:
        need(DROP_ITEMS, "now", "Drops on the Drop Plan")
    later = list(CORE_LATER) + list(sc["later"]) + (["lien_waivers"] if _aia(ctx) else [])
    for rk in sorted(filed):
        t = TRIGGERS.get(rk)
        # a BEST permit on a job set up without suspended scaffold does not drag the rigging in (2026-10-24)
        if not t or (rk == "best_permit" and ctx.get("access") is not None and "suspended_scaffold" not in acc):
            continue
        need(t.get("now", ()), "now", f"Because the {t['label']} is filed")
        later += list(t.get("later", ()))
        for k, cond in t.get("maybe", ()):
            maybe.setdefault(k, cond)
    need(later, "later", "At closeout")
    for k, cond in sc["maybe"]:
        maybe.setdefault(k, cond)
    return {"scope": sc_key, "items": items, "note": dict(sc.get("note", {})),
            "maybe": {k: c for k, c in maybe.items() if k not in items}}


def phase(requirement_key, ctx):
    """now | later | maybe | None (not asked on this job)."""
    p = plan(ctx)
    if requirement_key in p["items"]:
        return p["items"][requirement_key]["phase"]
    return "maybe" if requirement_key in p["maybe"] else None


def why_not(requirement_key, ctx) -> str:
    if requirement_key in DROP_ITEMS:
        return "no suspended scaffold or drops"
    if requirement_key in BILLING_ITEMS:
        return "not billed AIA / schedule of values"
    return f"not needed on {SCOPE_LABELS[scope_of(ctx)]}"


def suggestable(requirement_key, ctx) -> bool:
    """Whether a document filling this checklist item fits the job at all (a filed document's
    rule or words may suggest it). No context or an item with no checklist key -> yes."""
    if ctx is None or not requirement_key:
        return True
    if phase(requirement_key, ctx):
        return True
    if requirement_key in NEVER.get(scope_of(ctx), ()):
        return False
    if requirement_key in BILLING_ITEMS:
        return _aia(ctx)
    if requirement_key in DROP_ITEMS or requirement_key == "rope_access_plan":
        return False   # access / drops decide these, never a mention
    return True


def applies(requirement_key, ctx) -> bool:
    """Whether this checklist item is REQUIRED on this job right now (counts toward readiness):
    what it needs now, plus closeout once the job is near its end."""
    if ctx is None:
        return True
    if requirement_key in NOT_REQUIRED:
        return False
    ph = phase(requirement_key, ctx)
    return ph == "now" or (ph == "later" and bool(ctx.get("near_end")))


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


def suggest(docs, missing_reqs, dismissed=(), ctx=None, missing_labels=None):
    """docs: current filed docs (dicts: id, title, type_key, category, requirement_key,
    party, party_name, ai). missing_reqs: {(category, requirement_key)} still missing
    on the checklist (only the items that apply to this job). dismissed: suggestion keys
    marked not needed. ctx: the job (see plan) — None skips the fit filter.
    missing_labels: {(category, requirement_key): checklist label}. Returns a list.
    Each suggestion carries `phase` (now | later | maybe) and `group` (why it is there:
    "Every job", the scope, an access method, "Because the PW2 is filed", "At closeout",
    "Might also apply"); `cond` is the one-line condition of a maybe. With a ctx, everything
    the job's plan needs and is not filed comes back (origin 'checklist')."""
    dismissed = set(dismissed or ())
    out = {}
    pl = plan(ctx) if ctx is not None else None

    def add(trigger, key, reason, origin, label=None, ph="maybe", group="Might also apply", cond=None):
        t = doc_types.get(key)
        if not t or not suggestable(t["requirement_key"], ctx):
            return
        party, name = _target_party(trigger, key)
        if _filed(docs, t, party, name):
            return
        k = key_for(key, party, name)
        if k in dismissed:
            return
        src = {"id": trigger.get("id"), "title": trigger.get("title")}
        if k in out:
            if src.get("id") and src not in out[k]["sources"]:
                out[k]["sources"].append(src)
            return
        on_list = (party == "company" and bool(t["requirement_key"])
                   and (t["category"], t["requirement_key"]) in missing_reqs)
        if pl is not None and party == "company" and t["requirement_key"] in pl["items"]:
            on_list = True   # on this job's list (now, or at closeout)
        out[k] = {
            "key": k, "type_key": key, "label": label or t["label"], "category": t["category"],
            "party": party, "party_name": name, "reason": reason, "origin": origin,
            # the checklist is the job's own: a sub's document never fills it, so never "required"
            "on_checklist": on_list, "phase": ph if party == "company" else "maybe",
            "group": group if party == "company" else "Might also apply", "cond": cond,
            "sources": [src] if src.get("id") else [], "_n": len(out),
        }

    labels = missing_labels or {}
    if pl is not None:   # the job's own list first, so its phase / group win over a document's mention
        for rk, it in pl["items"].items():
            t = next((x for x in doc_types.TYPES if x[3] == rk), None)
            if t:
                add({}, t[0], "On this job's list and not filed yet.", "checklist",
                    label=labels.get((t[2], rk)), ph=it["phase"], group=it["group"], cond=pl["note"].get(rk))
        for rk, cond in pl["maybe"].items():
            t = next((x for x in doc_types.TYPES if x[3] == rk), None)
            if t:
                add({}, t[0], cond, "scope", label=labels.get((t[2], rk)), cond=cond)

    for d in docs:
        tk = d.get("type_key")
        for trig, key, why in RULES:
            if tk == trig:
                add(d, key, why, "rule", cond=why)
        for r in ((d.get("ai") or {}).get("related") or []):
            why = str(r.get("reason") or "")[:200] or "Referenced in this document."
            add(d, r.get("type_key"), why, "claude", cond=why)
        text = _read_text(d)
        if text.strip():
            for rx, key in KEYWORDS:
                if key == tk:
                    continue
                m = rx.search(text)
                if m:
                    add(d, key, f"“{m.group(0)}” appears in {d.get('title') or 'a filed document'}.", "content",
                        cond=f"“{m.group(0)}” is in {d.get('title') or 'a filed document'}")

    # without a job context: what the checklist still misses (legacy callers)
    for cat, rk in sorted(missing_reqs, key=lambda x: (CATEGORY_ORDER.index(x[0]) if x[0] in CATEGORY_ORDER else 99, x[1])):
        t = doc_types.for_requirement(cat, rk)
        if t:
            add({}, t["key"], "On this job's checklist and not filed yet.", "checklist",
                label=labels.get((cat, rk)), ph="now", group="Every job")

    rank = {"now": 0, "later": 1, "maybe": 2}   # within a phase: the plan's order (its groups stay together)
    res = sorted(out.values(), key=lambda s: (rank.get(s["phase"], 2), not s["on_checklist"], s["_n"]))
    for s in res:
        del s["_n"]
    return res
