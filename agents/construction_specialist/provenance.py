#!/usr/bin/env python3
"""PII-safe provenance writer for the Construction Specialist Agent (#198).

The Task-tool sub-agent calls this once per substantive interaction to
record an audit row in `construction_agent_provenance`. Operator decision
Q3 (spec §10): a DB table from day one — "no agent decision is
unauditable."

PII discipline (CLAUDE.md PII rule) — this writer NEVER stores worker
names, PIN values, or rate/pay values. Two layers:

  1. By contract: the agent passes W-#### references only, never names,
     never PIN/rate values. (Enforced by the agent definition.)
  2. By defense: `scrub_pii()` redacts phone numbers, $/rate amounts,
     SSNs, and PIN-labeled digits before anything is written — a
     belt-and-suspenders backstop for an accidental leak. It is
     deliberately narrow so it does NOT touch §-code-sections
     (e.g. §3314), fractions (1/2-inch), or plain integers.

Dates are LOCAL (CLAUDE.md dates rule — never UTC): asked_at and
created_at are written as local "YYYY-MM-DD HH:MM:SS" strings.

Test rows MUST be written with synthetic=True (or an SMK-/SYN- prefixed
interaction_id) so the anti-corruption meta-smoke treats them as
expected residue, and they MUST be cleaned up by the test.

Importable:  from provenance import log_interaction
CLI:
  python agents/construction_specialist/provenance.py --db <isolated.db> \
      --question "..." --sources "..." --summary "..." \
      --disposition pending
Prints the interaction_id + row id only — never echoes the content back.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import db_guard  # noqa: E402 — no default database: an isolated one only (CLAUDE.md)
CORPUS_VERSION_FILE = HERE / "corpus" / "CORPUS_VERSION"

SYNTHETIC_PREFIX = "SMK-"

# --- Defensive PII scrub patterns (narrow on purpose) -------------------
# Order matters: SSN before phone (SSN is a sub-shape of a phone run).
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
# Currency / rate: requires a $ OR an explicit per-time unit, so plain
# numbers and §-sections survive.
_CURRENCY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?/\s?(?:hr|hour|day|wk|week|yr|year))?", re.I)
_RATE_UNIT_RE = re.compile(r"\b\d+(?:\.\d{1,2})?\s?/\s?(?:hr|hour|day|wk|week|yr|year)\b", re.I)
# PIN explicitly labeled.
_PIN_RE = re.compile(r"\bPIN\s*[:#]?\s*\d{3,6}\b", re.I)


def scrub_pii(text: Optional[str]) -> Optional[str]:
    """Redact the highest-risk PII shapes. Returns None unchanged."""
    if not text:
        return text
    text = _SSN_RE.sub("[ssn redacted]", text)
    text = _PHONE_RE.sub("[phone redacted]", text)
    text = _CURRENCY_RE.sub("[rate redacted]", text)
    text = _RATE_UNIT_RE.sub("[rate redacted]", text)
    text = _PIN_RE.sub("PIN [redacted]", text)
    return text


def _local_now_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_corpus_version() -> str:
    try:
        return CORPUS_VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _new_interaction_id(synthetic: bool) -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    rand = uuid.uuid4().hex[:6]
    base = f"CAP-{stamp}-{rand}"
    return (SYNTHETIC_PREFIX + base) if synthetic else base


def log_interaction(
    question_text: str,
    sources_cited: str = "",
    answer_summary: str = "",
    operator_disposition: str = "pending",
    corpus_version: Optional[str] = None,
    interaction_id: Optional[str] = None,
    asked_at: Optional[str] = None,
    synthetic: bool = False,
    db_path: Optional[Path] = None,
) -> dict:
    """Write exactly one PII-safe provenance row. Returns a PII-safe
    dict: {interaction_id, row_id, corpus_version}. Never returns the
    stored text."""
    if not question_text or not question_text.strip():
        raise ValueError("question_text is required")

    db_path = db_guard.resolve(db_path)   # RefusedDB before anything is written
    corpus_version = corpus_version or _read_corpus_version()
    asked_at = asked_at or _local_now_str()
    created_at = _local_now_str()
    if interaction_id is None:
        interaction_id = _new_interaction_id(synthetic)
    elif synthetic and not interaction_id.startswith(SYNTHETIC_PREFIX):
        interaction_id = SYNTHETIC_PREFIX + interaction_id

    q = scrub_pii(question_text)
    s = scrub_pii(sources_cited)
    a = scrub_pii(answer_summary)

    conn = db_guard.connect(str(db_path), timeout=60.0)
    try:
        conn.execute("PRAGMA busy_timeout=60000;")
        cur = conn.execute(
            "INSERT INTO construction_agent_provenance "
            "(interaction_id, asked_at, question_text, corpus_version, "
            " sources_cited, answer_summary, operator_disposition, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (interaction_id, asked_at, q, corpus_version, s, a,
             operator_disposition, created_at),
        )
        conn.commit()
        row_id = cur.lastrowid
    finally:
        conn.close()

    return {
        "interaction_id": interaction_id,
        "row_id": row_id,
        "corpus_version": corpus_version,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Write one PII-safe Construction Specialist Agent "
                    "provenance row (W-#### only; no names/PINs/rates).")
    ap.add_argument("--question", required=True,
                    help="The question asked (PII-safe; W-#### only).")
    ap.add_argument("--sources", default="",
                    help="Sources cited, or 'general-knowledge-unverified'.")
    ap.add_argument("--summary", default="",
                    help="Short PII-safe summary of the answer.")
    ap.add_argument("--disposition", default="pending",
                    help="Operator disposition (default 'pending').")
    ap.add_argument("--corpus-version", default=None,
                    help="Override CORPUS_VERSION (default: read from file).")
    ap.add_argument("--interaction-id", default=None,
                    help="Override interaction_id (default: auto CAP-...).")
    ap.add_argument("--synthetic", action="store_true",
                    help="Mark as a test row (SMK- prefix). Tests only.")
    ap.add_argument("--db", default=None,
                    help=f"Isolated database (or set {db_guard.ENV_VAR}). Required; "
                         "the real superstars.db is refused.")
    args = ap.parse_args()

    try:
        db_guard.resolve(args.db)
    except db_guard.RefusedDB as e:
        print(f"provenance not logged: {e}", file=sys.stderr)
        return 2
    res = log_interaction(
        question_text=args.question,
        sources_cited=args.sources,
        answer_summary=args.summary,
        operator_disposition=args.disposition,
        corpus_version=args.corpus_version,
        interaction_id=args.interaction_id,
        synthetic=args.synthetic,
        db_path=args.db,
    )
    # PII-safe stdout: ids + version only, never the stored content.
    print(f"provenance row written: interaction_id={res['interaction_id']} "
          f"row_id={res['row_id']} corpus_version={res['corpus_version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
