#!/usr/bin/env python3
"""Project-document AI auto-read (#234, Batch B).

Reads a single uploaded project document (PDF — possibly multi-page — or an
image) and SUGGESTS how it files into the compliance checklist: title, doc_type,
category (one of the 6), requirement_key (which checklist item it fulfills, or
"other"), effective_date, expiry_date — plus a confidence + warnings so the modal
can flag low-confidence fields. It does NOT save anything; the operator confirms
in the modal and the existing Batch-A upload persists.

Same shape as expense_scanner (#219): ALL pages go to the vision model in a
SINGLE API call; the call is isolated in `call_vision_model`; the
validate/coerce pipeline (`process_scan_result`) is a PURE function so it is
unit-testable without the API. Key from ENV only (ANTHROPIC_API_KEY) — never
logged, never returned, never committed. Missing key -> ScanUnavailable so the
route returns a clean 503 and the UI falls back to manual entry.
"""
import base64
import json
import os
import re
from pathlib import Path

import ai_models
import brand
import doc_types
import sheet_read

# Default vision model — env-overridable so the operator can pin a different
# model without a code change (same key as receipts/certs). Sonnet 5.5 since 2026-10-22 (was Sonnet 4.6:
# a third cheaper per token); Haiku 5.5 is the next step down once checked on real scans.
MODEL = os.environ.get("DOC_SCAN_MODEL", "claude-sonnet-5-5")

MAX_PAGES = 12                      # a permit/drawing set can run several pages
MAX_FILE_BYTES = 16 * 1024 * 1024   # 16 MB / file
CONFIDENCE_FLAG = 0.7               # < this -> flag "check this" / needs_review
# caps on what is kept from the model's read (stored with the document)
SUMMARY_MAX = 600
KEY_FACTS_MAX = 4
FIELDS_MAX = 80
FIELD_LABEL_MAX = 80
FIELD_VALUE_MAX = 400
RELATED_MAX = 5
RELATED_REASON_MAX = 200

# Image media types the vision API accepts directly. HEIC is NOT accepted by the
# image block, so it degrades: we still send it (labeled best-effort) but the
# model may return low confidence -> the UI flags "enter manually".
_IMG_MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".gif": "image/gif", ".webp": "image/webp",
              ".heic": "image/jpeg", ".heif": "image/jpeg"}


class ScanUnavailable(Exception):
    """No API key configured — caller should 503 + fall back to manual."""


class ScanError(Exception):
    """The vision call or parse failed — caller should 502, keep manual entry."""


_FIELD = {
    "type": "object",
    "properties": {"label": {"type": "string"}, "value": {"type": "string"}},
    "required": ["label", "value"],
    "additionalProperties": False,
}

_RELATED = {
    "type": "object",
    "properties": {"type_key": {"type": "string", "enum": list(doc_types.KEYS)},
                   "reason": {"type": "string"}},
    "required": ["type_key", "reason"],
    "additionalProperties": False,
}

SCAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": ["string", "null"],
                  "description": "A concise human title for the document"},
        "doc_type": {"type": ["string", "null"],
                     "description": "What kind of document this is, e.g. 'DOB Work Permit (PW2)'"},
        "category": {"type": ["string", "null"],
                     "description": "ONE of the category CODES provided"},
        "requirement_key": {"type": ["string", "null"],
                            "description": "The requirement_key it fulfills, or 'other' if none fits"},
        "effective_date": {"type": ["string", "null"],
                           "description": "ISO YYYY-MM-DD — issue/effective date, or null"},
        "expiry_date": {"type": ["string", "null"],
                        "description": "ISO YYYY-MM-DD — expiry/renewal date, or null if none"},
        "page_count": {"type": ["integer", "null"]},
        "confidence": {"type": "number",
                       "description": "0.0-1.0 overall confidence in the extracted fields"},
        "warnings": {"type": "array", "items": {"type": "string"}},
        # drop-zone intake: the document TYPE tag + everything read off the pages
        "type_key": {"type": "string", "enum": list(doc_types.KEYS),
                     "description": "ONE of the document type keys provided"},
        "summary": {"type": "string",
                    "description": "One or two plain sentences: what this document is and what it says"},
        "key_facts": {"type": "array", "items": _FIELD,
                      "description": "The 2-4 facts a project manager checks first (e.g. expiry, amount due, status)"},
        "fields": {"type": "array", "items": _FIELD,
                   "description": "EVERY piece of information on the document, one label/value per item"},
        # whose record this is, and what other documents it points to
        "party": {"type": "string", "enum": ["company", "subcontractor", "owner", "other", "unknown"],
                  "description": "Whose record this is: our company, a subcontractor, the owner/client, or other"},
        "party_name": {"type": ["string", "null"],
                       "description": "The subcontractor / owner / other company's name as printed, else null"},
        "related": {"type": "array", "items": _RELATED,
                    "description": "Other documents this one references or clearly calls for (max 5)"},
    },
    "required": ["title", "doc_type", "category", "requirement_key",
                 "effective_date", "expiry_date", "confidence",
                 "type_key", "summary", "key_facts", "fields", "party", "party_name", "related"],
    "additionalProperties": False,
}


def build_prompt(categories, requirements):
    """categories: list of (code, name). requirements: list of (category, key, label)."""
    cat_lines = "\n".join(f"  {code} — {name}" for code, name in categories)
    by_cat = {}
    for cat, key, label in requirements:
        by_cat.setdefault(cat, []).append(f"      {key} — {label}")
    req_lines = []
    for code, _name in categories:
        req_lines.append(f"  {code}:")
        req_lines.extend(by_cat.get(code, ["      (no specific items)"]))
    req_block = "\n".join(req_lines)
    type_block = "\n".join(f"  {t[0]} — {t[1]} (files to {t[2]})" for t in doc_types.TYPES)
    return f"""You are filing ONE document for an NYC facade-restoration general contractor (LL11 / FISP work). Read the document and classify it so it can be matched to the project's compliance checklist.

You may be given MULTIPLE pages (a multi-page PDF and/or several images). Read ALL of them as ONE document — do not treat each page as a separate document.

A spreadsheet (Excel or CSV) arrives as text: one "## Sheet: <name>" block per sheet, each row on its own line with cells separated by tabs. Read every sheet; totals, line items and dates in the cells are the document's fields.

Pick the CATEGORY — exactly one of these codes:
{cat_lines}

Pick the requirement_key — the single checklist item this document fulfills. Use the key EXACTLY as written. If the document doesn't match any item in the chosen category, use "other".
{req_block}

Also extract:
- title: a short, clean title for the document (e.g. the permit name + number).
- doc_type: what kind of document it is (e.g. "DOB Work Permit (PW2)", "Certificate of Insurance", "FISP Inspection Report").
- effective_date: the issue / effective / signed date as ISO YYYY-MM-DD (convert from any printed format; null if none is shown).
- expiry_date: the expiration / renewal / valid-until date as ISO YYYY-MM-DD (null if the document does not expire or none is shown).
- confidence: 0.0-1.0 — your overall confidence. LOWER it when the scan is blurry/partial, the category is ambiguous, or you are guessing a date. Never invent a date you cannot read — use null and lower confidence.
- warnings: short notes for the operator about anything uncertain (optional).
- type_key: the document TYPE — exactly one key from this list. Its folder must match the category you picked. If nothing fits, use the "..._other" key for that folder.
{type_block}
- summary: one or two plain sentences saying what the document is and what it says (e.g. who it is between, what it covers, its status).
- key_facts: the 2-4 facts a project manager would check first, as short label/value pairs (e.g. "Expires" / "03/14/2027", "Current payment due" / "$84,210.00", "Status" / "Approved").
- fields: EVERY piece of information printed or written on the document, as label/value pairs in reading order — names, addresses, numbers (permit, job, policy, BIN, license), dates, amounts, limits, coverages, line items, signatures present or missing, checkboxes ticked. Write dates as MM/DD/YYYY and amounts as printed. Copy values exactly; if something is unreadable, say "unreadable" rather than guessing.
- party: whose record this is. The general contractor filing it is {brand.COMPANY_NAME} ("company"). Use "subcontractor" for a subcontractor's own record (its COI, its subcontract, its lien waiver), "owner" for the building owner / client's own record, "other" for anyone else, "unknown" if you cannot tell.
- party_name: for subcontractor / owner / other, that company's name exactly as printed; otherwise null.
- related: up to 5 OTHER documents this one names, attaches, or clearly calls for (e.g. a contract requiring a bond or a certificate of insurance, a pay application calling for lien waivers, a permit referencing approved plans). Each is a type key from the list above plus a short reason quoting or paraphrasing the document. Only list what the document itself supports — never a generic checklist. Empty list if none.

Return ONLY the structured JSON. No prose."""


def spec_from_bytes(data_bytes, filename):
    """Build a content-block spec for one uploaded page (in-memory — no disk).
    A spreadsheet (.xlsx/.xlsm/.xls/.csv) goes as its text, one block per sheet
    (sheet_read); it is never sent as a picture. Raises ScanError if it won't open."""
    ext = Path(filename or "").suffix.lower()
    if ext in sheet_read.EXTS:
        try:
            body = sheet_read.text(data_bytes, ext)
        except Exception as e:
            raise ScanError(f"spreadsheet could not be opened: {type(e).__name__}")
        return {"text": body or "(the workbook is empty)", "title": Path(filename).name,
                "is_pdf": False, "is_text": True}
    is_pdf = ext == ".pdf"
    return {
        "data_b64": base64.standard_b64encode(data_bytes).decode("ascii"),
        "media_type": "application/pdf" if is_pdf else _IMG_MEDIA.get(ext, "image/jpeg"),
        "is_pdf": is_pdf,
    }


def _parse_json_defensive(text):
    """Structured output should already be clean JSON; still strip fences / prose
    and extract the first balanced object as a fallback."""
    if not text:
        raise ScanError("empty model response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise ScanError("model response was not valid JSON")


def call_vision_model(specs, categories, requirements):
    """ONE API call with ALL pages. Raises ScanUnavailable (no key) / ScanError."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ScanUnavailable("ANTHROPIC_API_KEY not configured")
    import anthropic
    content = []
    for spec in specs:
        if spec.get("is_text"):
            content.append({"type": "document", "title": spec["title"], "source": {
                "type": "text", "media_type": "text/plain", "data": spec["text"]}})
        elif spec["is_pdf"]:
            content.append({"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf", "data": spec["data_b64"]}})
        else:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": spec["media_type"], "data": spec["data_b64"]}})
    content.append({"type": "text", "text": build_prompt(categories, requirements)})
    client = anthropic.Anthropic()
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=8000, **ai_models.thinking_off(MODEL),
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": SCAN_SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:  # anthropic.APIError + transport errors
        raise ScanError(f"vision API call failed: {type(e).__name__}")
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    raw = _parse_json_defensive(text)
    raw["_meta"] = {"model": MODEL,
                    "input_tokens": getattr(resp.usage, "input_tokens", None),
                    "output_tokens": getattr(resp.usage, "output_tokens", None)}
    return raw


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def process_scan_result(raw, valid_categories, req_by_cat, default_category="PERMITS"):
    """PURE: validate + coerce the model's raw JSON into a safe SUGGESTION.

    valid_categories: set of category codes. req_by_cat: {category: set(keys)}.
    NEVER trusts blindly:
      - category not in the enum -> default_category + warn + flag.
      - requirement_key not valid FOR the chosen category -> "other" + warn + flag.
      - a date that isn't ISO YYYY-MM-DD -> null + warn + flag.
      - confidence < CONFIDENCE_FLAG -> needs_review.
    Returns the suggestion dict the modal pre-fills (no DB, no API, no *_path).
    """
    valid_categories = set(valid_categories)
    req_by_cat = req_by_cat or {}
    warnings = [str(w) for w in (raw.get("warnings") or []) if w]
    fields_to_check = []

    # category
    cat = (raw.get("category") or "").strip().upper()
    if cat not in valid_categories:
        if cat:
            warnings.append(f"Category '{cat}' wasn't recognized — defaulted to {default_category}; please check.")
        cat = default_category
        fields_to_check.append("category")

    # requirement_key — must be a real key FOR this category, else "other"
    rk = (raw.get("requirement_key") or "").strip()
    valid_keys = req_by_cat.get(cat, set())
    if rk.lower() in ("", "other", "none", "null"):
        rk = None
    elif rk not in valid_keys:
        warnings.append(f"'{rk}' isn't a {cat} checklist item — set to Other; pick the right item if needed.")
        rk = None
        fields_to_check.append("requirement_key")

    # dates — strict ISO or null
    def _date(field):
        v = raw.get(field)
        if v in (None, ""):
            return None
        if not _ISO_DATE.match(str(v)):
            warnings.append(f"Couldn't read the {field.replace('_', ' ')} ('{v}') — set it manually.")
            fields_to_check.append(field)
            return None
        return str(v)

    eff = _date("effective_date")
    exp = _date("expiry_date")
    # an expiry before the effective date is almost certainly a misread
    if eff and exp and exp < eff:
        warnings.append("The expiry date is before the effective date — check both.")
        fields_to_check.extend(["effective_date", "expiry_date"])

    # confidence
    try:
        conf = float(raw.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    needs_review = conf < CONFIDENCE_FLAG
    if needs_review:
        warnings.append("Low confidence — double-check the auto-filled fields before saving.")

    title = (raw.get("title") or "").strip() or None
    doc_type = (raw.get("doc_type") or "").strip() or None

    # type tag — a valid key whose folder agrees with the category; otherwise the
    # checklist item's type, else the folder's catch-all (and flag it)
    t = doc_types.get(raw.get("type_key"))
    if t and t["category"] != cat:
        warnings.append(f"The document type '{t['label']}' doesn't belong in {cat} — check the type.")
        t = None
        fields_to_check.append("type_key")
    if t is None:
        t = (doc_types.for_requirement(cat, rk)
             or doc_types.get(doc_types.FALLBACK_BY_CATEGORY.get(cat)))
    else:   # the tag decides which checklist item (if any) the document fulfils
        rk = t["requirement_key"] if t["requirement_key"] in valid_keys else None
    summary = str(raw.get("summary") or "").strip()[:SUMMARY_MAX] or None
    key_facts = _pairs(raw.get("key_facts"), KEY_FACTS_MAX)
    fields = _pairs(raw.get("fields"), FIELDS_MAX)
    party = str(raw.get("party") or "").strip().lower()
    party = party if party in ("company", "subcontractor", "owner", "other") else None
    party_name = (" ".join(str(raw.get("party_name") or "").split())[:120] or None) if party not in (None, "company") else None
    related, seen = [], set()
    for r in raw.get("related") or []:
        if not isinstance(r, dict):
            continue
        rt = doc_types.get(r.get("type_key"))
        reason = str(r.get("reason") or "").strip()[:RELATED_REASON_MAX]
        if rt and reason and rt["key"] not in seen and rt["key"] != (t["key"] if t else None):
            seen.add(rt["key"])
            related.append({"type_key": rt["key"], "reason": reason})
        if len(related) >= RELATED_MAX:
            break

    return {
        "title": title,
        "doc_type": doc_type,
        "category": cat,
        "requirement_key": rk,            # None == "Other / extra"
        "effective_date": eff,
        "expiry_date": exp,
        "confidence": round(conf, 2),
        "needs_review": bool(needs_review),
        "fields_to_check": sorted(set(fields_to_check)),
        "page_count": raw.get("page_count"),
        "warnings": warnings,
        "type_key": t["key"] if t else None,
        "summary": summary,
        "key_facts": key_facts,
        "fields": fields,
        "party": party,
        "party_name": party_name,
        "related": related,
    }


def _pairs(items, limit):
    """Model label/value pairs -> a clean, capped list of {label, value} strings."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        label = str(it.get("label") or "").strip()[:FIELD_LABEL_MAX]
        value = str(it.get("value") or "").strip()[:FIELD_VALUE_MAX]
        if label and value:
            out.append({"label": label, "value": value})
        if len(out) >= limit:
            break
    return out
