#!/usr/bin/env python3
"""Expense receipt AI scan + classification (#219, Batch B).

Sends ALL pages of a receipt (multiple images AND/OR a multi-page PDF) to the
vision model in a SINGLE API call, gets back STRICT JSON (structured output),
then validates + classifies + applies alias memory — all with Decimal money
(no float drift). The model call is isolated in `call_vision_model`; the
validate/classify/alias pipeline (`process_scan_result`) is a PURE function so
it is unit-testable without the API.

Key from ENV only (ANTHROPIC_API_KEY) — never logged, never returned, never
committed. Missing key -> ScanUnavailable so the route returns a clean 503 and
the UI falls back to manual entry.
"""
import base64
import json
import os
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import ai_models

# Default vision model — good at messy / handwritten receipts. Overridable via
# env so the operator can pin a different model without a code change. Sonnet 5.5 since 2026-10-22.
MODEL = os.environ.get("EXPENSE_SCAN_MODEL", "claude-sonnet-5-5")

MAX_PAGES = 10
MAX_FILE_BYTES = 12 * 1024 * 1024  # 12 MB / file
CONFIDENCE_FLAG = 0.7              # < this -> flag the line "check this"
TOTAL_TOLERANCE = Decimal("0.05")  # $ tolerance for stated-vs-summed total


class ScanUnavailable(Exception):
    """No API key configured — caller should 503 + fall back to manual."""


class ScanError(Exception):
    """The vision call or parse failed — caller should 502 but keep the draft."""


SCAN_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor": {"type": ["string", "null"]},
        "vendor_contact": {"type": ["string", "null"]},
        "doc_type": {"type": ["string", "null"]},
        "doc_number": {"type": ["string", "null"]},
        "order_number": {"type": ["string", "null"]},
        "expense_date": {"type": ["string", "null"], "description": "ISO YYYY-MM-DD"},
        "stated_total": {"type": ["number", "null"], "description": "The total PRINTED on the receipt (all lines), or null"},
        "page_count": {"type": ["integer", "null"]},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": ["string", "null"]},
                    "description": {"type": "string"},
                    "qty": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "unit_price": {"type": ["number", "null"]},
                    "extended_price": {"type": ["number", "null"]},
                    "product_class": {"type": "string"},
                    "normalized_product": {"type": ["string", "null"]},
                    "is_refundable": {"type": "boolean"},
                    "confidence": {"type": "number"},
                },
                "required": ["description", "product_class", "is_refundable", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["vendor", "doc_type", "doc_number", "order_number", "expense_date",
                 "stated_total", "lines"],
    "additionalProperties": False,
}


def build_prompt(classes, units):
    cls = "\n".join("  " + c for c in classes)
    unit_list = ", ".join(units)
    return f"""You are reading a construction-materials receipt / invoice / pick ticket for an NYC facade-restoration contractor, and extracting EVERY line item as structured data for job costing.

You may be given MULTIPLE pages (several images and/or a multi-page PDF). Read ALL of them as ONE document:
- Aggregate every line item across ALL pages into a single `lines` array.
- A receipt may say e.g. "Page 2 of 3" — extract only the lines actually shown; do NOT invent lines from other pages, and do NOT add a page's printed subtotal as if it were a line item (never double-count totals).
- `stated_total` = the total amount PRINTED on the receipt for what is shown (the document's own total), or null if none is printed.

For each line item output:
- item_id: the vendor SKU / item code as printed (or null)
- description: the item description, verbatim and clean
- qty, unit_price, extended_price: numbers (no $ or commas). extended_price is the line total.
- unit: ONE of exactly these unit codes — {unit_list}. Pick the closest; if truly none fits use EA.
- product_class: ONE of exactly these class codes (what the thing physically IS):
{cls}
- normalized_product: a clean, groupable product name (e.g. "8x8x16 Std Hollow Block") so the same SKU from different receipts rolls up together.
- is_refundable: true ONLY for refundable deposits (pallet/keg/cylinder deposit). Those are product_class DEPOSIT_REFUNDABLE.
- confidence: 0.0-1.0, your confidence in THIS line's fields.

Classification rules:
- Masonry units (CMU/block/brick/bond beam/pavers/stone) -> MASONRY.
- A refundable pallet/deposit charge -> DEPOSIT_REFUNDABLE (is_refundable=true).
- A returned-material credit or any NEGATIVE line -> CREDIT_RETURN.
- Delivery / freight / fuel surcharge are their OWN class DELIVERY_FREIGHT — never folded into the material line.
- If unsure of the class, use OTHER and set confidence below 0.7.

HANDWRITTEN or hard-to-read receipts: read the handwriting best-effort, but LOWER the confidence on any field you are not sure about. Never guess a precise number you cannot read — estimate and lower confidence, or use null.

Header fields: vendor (company name), vendor_contact (address/phone if shown), doc_type (Invoice/Receipt/Pick Ticket/PO/...), doc_number, order_number, expense_date (ISO YYYY-MM-DD — convert from any printed format; null if unreadable), page_count.

Return ONLY the structured JSON. No prose."""


def _media_type_for(path):
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(Path(path).suffix.lower(), "image/jpeg")


def file_spec(path):
    """Build a content-block spec for one page file."""
    ext = Path(path).suffix.lower()
    return {"path": str(path), "is_pdf": ext == ".pdf",
            "media_type": "application/pdf" if ext == ".pdf" else _media_type_for(path)}


def _parse_json_defensive(text):
    """Structured output should already be clean JSON; still strip any fences /
    prose and extract the first balanced object as a fallback."""
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


def call_vision_model(file_specs, classes, units):
    """ONE API call with all pages. Raises ScanUnavailable (no key) / ScanError."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ScanUnavailable("ANTHROPIC_API_KEY not configured")
    import anthropic
    content = []
    for spec in file_specs:
        data_b64 = base64.standard_b64encode(Path(spec["path"]).read_bytes()).decode("ascii")
        if spec["is_pdf"]:
            content.append({"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf", "data": data_b64}})
        else:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": spec["media_type"], "data": data_b64}})
    content.append({"type": "text", "text": build_prompt(classes, units)})
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


def _dec(v):
    try:
        return Decimal(str(v if v is not None else 0))
    except Exception:
        return Decimal("0")


def _q2(d):
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def process_scan_result(raw, classes, units, alias_lookup=None,
                        refundable_classes=None, ooc_classes=None):
    """PURE: validate + classify + alias-override the model's raw JSON.

    classes/units: allowed enums (sets). alias_lookup: {item_key: {product_class,
    normalized_product}} for the receipt's vendor. Returns a dict with cleaned
    header, lines (each w/ confidence + low_confidence + alias_applied), the
    job-cost total (non-refundable, Decimal->float), and warnings. No API, no DB.
    """
    classes = set(classes)
    units = set(units)
    alias_lookup = alias_lookup or {}
    refundable_classes = set(refundable_classes or {"DEPOSIT_REFUNDABLE"})
    ooc_classes = set(ooc_classes or {"DEPOSIT_REFUNDABLE", "CREDIT_RETURN"})
    warnings = []

    header = {
        "vendor": raw.get("vendor"), "vendor_contact": raw.get("vendor_contact"),
        "doc_type": raw.get("doc_type"), "doc_number": raw.get("doc_number"),
        "order_number": raw.get("order_number"), "expense_date": raw.get("expense_date"),
        "stated_total": raw.get("stated_total"), "page_count": raw.get("page_count"),
    }
    # date sanity: must look like ISO YYYY-MM-DD else null + warn
    if header["expense_date"] and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(header["expense_date"])):
        warnings.append(f"Could not parse the date '{header['expense_date']}' — set it manually.")
        header["expense_date"] = None

    out_lines = []
    sum_all = Decimal("0")
    low_conf = 0
    for ln in (raw.get("lines") or []):
        pc = ln.get("product_class") or "OTHER"
        conf = ln.get("confidence")
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        if pc not in classes:
            warnings.append(f"Unknown class '{pc}' for \"{ln.get('description', '')[:40]}\" -> OTHER (check).")
            pc = "OTHER"
            conf = min(conf, 0.5)
        unit = ln.get("unit") or "EA"
        if unit not in units:
            warnings.append(f"Unknown unit '{unit}' for \"{ln.get('description', '')[:40]}\" -> EA (check).")
            unit = "EA"
            conf = min(conf, 0.6)
        qty = _dec(ln.get("qty"))
        unit_price = _dec(ln.get("unit_price"))
        ext_raw = ln.get("extended_price")
        ext = _q2(_dec(ext_raw)) if ext_raw not in (None, "") else _q2(qty * unit_price)
        refundable = bool(ln.get("is_refundable")) or pc in refundable_classes
        alias_applied = False
        # ALIAS OVERRIDE — exact (vendor,item_key) hit overrides the model @1.0
        key = (ln.get("item_id") or "").strip() or (ln.get("normalized_product") or ln.get("description") or "").strip()
        hit = alias_lookup.get(key.lower()) if key else None
        if hit:
            pc = hit.get("product_class") or pc
            refundable = refundable or pc in refundable_classes
            alias_applied = True
            conf = 1.0
        out_of_cost = refundable or pc in ooc_classes
        low = (conf < CONFIDENCE_FLAG) and not alias_applied
        if low:
            low_conf += 1
        out_lines.append({
            "item_id": (ln.get("item_id") or None),
            "description": ln.get("description") or "",
            "product_class": pc,
            "normalized_product": (hit.get("normalized_product") if hit else ln.get("normalized_product")) or None,
            "qty": float(qty), "unit": unit, "unit_price": float(unit_price),
            "extended_price": float(ext),
            "is_refundable": 1 if refundable else 0,
            "out_of_cost": 1 if out_of_cost else 0,
            "confidence": round(conf, 2),
            "low_confidence": bool(low),
            "alias_applied": alias_applied,
        })
        sum_all += ext

    # job-cost total (non-refundable / non-out-of-cost), Decimal — no drift
    total = sum((_dec(l["extended_price"]) for l in out_lines if not l["out_of_cost"]), Decimal("0"))
    # cross-check: do the extracted lines sum to the receipt's stated total?
    stated = header.get("stated_total")
    if stated is not None:
        if (_q2(sum_all) - _dec(stated)).copy_abs() > TOTAL_TOLERANCE:
            warnings.append(
                f"Lines sum to ${_q2(sum_all)} but the receipt states ${_q2(_dec(stated))} — "
                f"check for a missed or double-counted line.")
    if not out_lines:
        warnings.append("No line items were read — enter them manually.")
    return {
        "header": header,
        "lines": out_lines,
        "total": float(_q2(total)),
        "lines_sum_all": float(_q2(sum_all)),
        "low_confidence_count": low_conf,
        "warnings": warnings,
    }
