#!/usr/bin/env python3
"""Shared vision-extraction logic for cert cards.

Single source of truth for the prompt, schema, and Claude API call. Imported
by both the CLI POC (cert_extract_poc.py) and the Flask route
(/api/employees/<emp_id>/certifications/extract) so prompt or model changes
only need to happen in one place.

Requires ANTHROPIC_API_KEY in the environment — launch via
`op run --env-file=".env.template" -- ...` so the key is injected from
the 1Password vault, never read from disk.
"""

import base64
import json
import os
import sqlite3
import time
from pathlib import Path

import anthropic

import ai_models

# Sonnet 5.5 since 2026-10-22 (was Sonnet 4.6); env-overridable like the receipt / document scanners.
MODEL = os.environ.get("CERT_EXTRACT_MODEL", "claude-sonnet-5-5")

SCHEMA = {
    "type": "object",
    "properties": {
        "cert_type_id_guess": {
            "type": ["string", "null"],
            "description": "Best-match cert_type_id from the 47-row library, or null."
        },
        "cert_type_name_visible": {
            "type": ["string", "null"],
            "description": "Raw cert name/title as printed on the card."
        },
        "card_number": {
            "type": ["string", "null"],
            "description": "Card number / serial number printed on the card."
        },
        "date_obtained": {
            "type": ["string", "null"],
            "description": "Issue / training date in ISO YYYY-MM-DD, or null."
        },
        "expiration_date": {
            "type": ["string", "null"],
            "description": "Expiration date in ISO YYYY-MM-DD, or null."
        },
        "issuing_body": {
            "type": ["string", "null"],
            "description": "Issuing authority (NYC DOB, FDNY, OSHA, NYCCT, etc.)."
        },
        "holder_name": {
            "type": ["string", "null"],
            "description": "Worker name on the card (verification only)."
        },
        "extraction_notes": {
            "type": "string",
            "description": "Caveats, uncertainties, or notes about extraction quality."
        }
    },
    "required": [
        "cert_type_id_guess", "cert_type_name_visible", "card_number",
        "date_obtained", "expiration_date", "issuing_body",
        "holder_name", "extraction_notes"
    ],
    "additionalProperties": False
}


def _build_prompt(cert_types_library):
    """Embed the cert_types library inline so Claude has explicit candidates.

    cert_types_library: list of dicts with cert_type_id, name, is_cof_prerequisite."""
    library_lines = []
    for item in cert_types_library:
        flag = "  [CoF prereq]" if item.get("is_cof_prerequisite") else ""
        library_lines.append(f"  {item['cert_type_id']:<18}  {item['name']}{flag}")
    library = "\n".join(library_lines)

    return f"""You are extracting structured data from a photo of an NYC construction worker certification card.

Match the visible cert to ONE of these 47 known cert_type_id codes when possible:

{library}

Rules:
- Extract ONLY what you can clearly read from the image.
- Return null for any field you cannot confidently identify. Do NOT guess. Do NOT hallucinate.
- For cert_type_id_guess: pick the code from the list above whose name best matches the visible cert title. If nothing matches confidently, return null.
- For dates: use ISO YYYY-MM-DD. If the card shows a date in a different format, convert it. If you can't read the date confidently, return null.
- For card_number: copy the printed number exactly. Strip surrounding labels.
- For holder_name: include the worker's name as printed. Used for verification only — won't be stored long-term.
- If the image is blurry, glared, partial, or not a cert card at all, note that in extraction_notes and return nulls for the data fields."""


def _media_type_for(path):
    """Map common image extensions to MIME types Claude vision accepts."""
    ext = Path(path).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")


def load_cert_types_from_db(db_path=None):
    """Convenience: load the cert_types library as a list of dicts.

    #260 — routes through db_layer (honors SSC_DB_URL); production default = live
    SQLite. `db_path` is accepted for backward-compat but ignored when a backend is
    selected via SSC_DB_URL (cert_types is a static reference table, identical across
    backends), so a test run never reads the live DB out from under itself."""
    import db_layer
    conn = db_layer.connect()
    rows = conn.execute(
        "SELECT cert_type_id, name, is_cof_prerequisite FROM cert_types "
        "ORDER BY cert_type_id"
    ).fetchall()
    conn.close()
    return [
        {"cert_type_id": cid, "name": name, "is_cof_prerequisite": bool(prereq)}
        for cid, name, prereq in rows
    ]


def extract_cert_from_image(image_path, cert_types_library):
    """Run Claude Sonnet vision over one cert image and return the parsed JSON.

    Args:
        image_path: filesystem path to a JPEG/PNG/WEBP/etc. of a cert card.
        cert_types_library: list of dicts {cert_type_id, name, is_cof_prerequisite}.

    Returns:
        dict matching SCHEMA fields, plus a `_meta` key with
        {model, elapsed_seconds, input_tokens, output_tokens, stop_reason}.

    Raises:
        FileNotFoundError if image_path doesn't exist.
        RuntimeError if ANTHROPIC_API_KEY is unset, the API call fails, or
            the response cannot be parsed as JSON.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"image not found: {image_path}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Launch the process via "
            "`op run --env-file=\".env.template\" -- ...`"
        )

    image_b64 = base64.standard_b64encode(image_path.read_bytes()).decode("ascii")
    prompt = _build_prompt(cert_types_library)

    client = anthropic.Anthropic()
    t0 = time.monotonic()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            **ai_models.thinking_off(MODEL),
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": _media_type_for(image_path),
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
    except anthropic.APIError as e:
        raise RuntimeError(f"Anthropic API call failed: {e}") from e
    elapsed = time.monotonic() - t0

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        extracted = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"response was not valid JSON: {e}") from e

    extracted["_meta"] = {
        "model": MODEL,
        "elapsed_seconds": round(elapsed, 2),
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "stop_reason": response.stop_reason,
    }
    return extracted
