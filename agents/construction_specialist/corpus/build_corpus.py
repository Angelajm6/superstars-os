#!/usr/bin/env python3
"""Assemble the Construction Specialist Agent MVP corpus pack (#198).

Phase A / v0 — scaffolding. Builds the corpus context pack from ONLY
what already lives in the app repo:

  * Chapter 33 index  <- toolbox_talks_data.py (the 19 NYC DOB Ch 33
                          toolbox talks, each tagged with its §-ref)
  * Sika spec index   <- spec_products table in superstars.db
                          (107 Sika products, manufacturer-published TDS
                          links)

It then writes a machine-readable corpus_manifest.json, computes a
CORPUS_VERSION stamp (date + sha256 of the manifest), and refreshes the
human-readable CORPUS_MANIFEST.md header with that stamp.

Deferred public-source items (Local Law 11/77/126, SPRAT/IRATA, deep
product write-ups) are listed as clearly-marked placeholders only — they
are a SEPARATE later batch and are NOT curated here.

Re-runnable: re-stamps deterministically when a source changes. The DB
(superstars.db) is gitignored; the extracted index files committed under
this folder are the version-controlled corpus of record.

PII-safe: toolbox + spec data carry no worker PII. The script prints
counts only.

Run:
  python agents/construction_specialist/corpus/build_corpus.py --db <isolated.db>
  (an isolated database with the spec_products seed; the real superstars.db is refused)
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent                 # .../corpus
AGENT_DIR = HERE.parent                                  # .../construction_specialist
REPO_ROOT = AGENT_DIR.parent.parent                      # dashboard/
sys.path.insert(0, str(HERE.parent))
import db_guard  # noqa: E402 — no default database: an isolated one only (CLAUDE.md)
DB_PATH = None   # set in main() from --db / SSC_AGENT_DB through db_guard
TALKS_PY = REPO_ROOT / "toolbox_talks_data.py"

CH33_DIR = HERE / "chapter33"
SIKA_DIR = HERE / "sika"


def _load_talks():
    spec = importlib.util.spec_from_file_location("toolbox_talks_data", TALKS_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.TALKS


def build_ch33_index() -> dict:
    """Chapter 33 orientation index, English-only, derived from the
    structured toolbox-talk content. The talks are SSC's plain-language
    distillation of Ch 33 — orientation, not the controlling code text.
    """
    talks = _load_talks()
    lines = [
        "# NYC Building Code Chapter 33 — Safeguards During Construction",
        "## Agent orientation index (C1)",
        "",
        "> **Orientation, not controlling text.** This index is SSC's",
        "> plain-language distillation of Chapter 33, extracted from the",
        "> 19 toolbox talks in `toolbox_talks_data.py`. The controlling",
        "> legal text is the official NYC Building Code Chapter 33 (137 pp,",
        "> 21 sections, amended by Local Law 77 of 2023). When citing a",
        "> requirement, name the §-section and flag that the exact text",
        "> must be confirmed against the official code. The source PDF is",
        "> NOT yet in the repo (DOB BC-33, `data_room/dob_codes/` shows",
        "> 'Pending Download') — see the deferred manifest.",
        "",
        f"_Derived from `toolbox_talks_data.py` — {len(talks)} talks._",
        "",
        "| # | Category | §-ref | Topic | Key rules (with §) |",
        "|---|---|---|---|---|",
    ]
    catalog = []
    for t in talks:
        rules = t.get("rules_en", [])
        rule_summ = "; ".join(
            f"{txt} ({ref})" for (txt, ref) in rules
        ).replace("|", "/")
        title = t["title_en"].replace("|", "/")
        lines.append(
            f"| {t['topic_number']} | {t['category']} | {t['ch33_ref']} | "
            f"{title} | {rule_summ} |"
        )
        catalog.append({
            "topic_number": t["topic_number"],
            "slug": t["slug"],
            "category": t["category"],
            "ch33_ref": t["ch33_ref"],
            "title_en": t["title_en"],
            "why_en": t.get("why_en", ""),
            "rules_en": [{"text": txt, "ref": ref} for (txt, ref) in rules],
        })

    lines += [
        "",
        "## Per-topic detail",
        "",
    ]
    for c in catalog:
        lines.append(f"### Topic {c['topic_number']} — {c['title_en']}  (`{c['ch33_ref']}`, {c['category']})")
        if c["why_en"]:
            lines.append("")
            lines.append(c["why_en"])
        if c["rules_en"]:
            lines.append("")
            lines.append("Rules:")
            for r in c["rules_en"]:
                lines.append(f"- {r['text']}  — `{r['ref']}`")
        lines.append("")

    CH33_DIR.mkdir(parents=True, exist_ok=True)
    (CH33_DIR / "ch33_index.md").write_text("\n".join(lines), encoding="utf-8")
    (CH33_DIR / "ch33_index.json").write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"items": len(catalog), "source": "toolbox_talks_data.py"}


def build_sika_index() -> dict:
    """Sika spec index (C2), derived from the spec_products table.
    Product data, not PII. Each row keeps its manufacturer-published TDS
    URL so the agent points the operator at the controlling data sheet.
    """
    conn = db_guard.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT manufacturer, category, product_line, product_name, "
        "product_code, description, spec_url, tags "
        "FROM spec_products ORDER BY category, product_name"
    ).fetchall()
    conn.close()

    by_cat: dict[str, list] = {}
    catalog = []
    for r in rows:
        cat = r["category"] or "(uncategorized)"
        by_cat.setdefault(cat, []).append(r)
        catalog.append({
            "manufacturer": r["manufacturer"],
            "category": cat,
            "product_line": r["product_line"],
            "product_name": r["product_name"],
            "product_code": r["product_code"],
            "spec_url": r["spec_url"],
        })

    lines = [
        "# Sika product specifications — agent index (C2)",
        "",
        "> Substrate/product matching reference. The controlling document",
        "> for any product is always the manufacturer-published Technical",
        "> Data Sheet (TDS) at the `spec_url`. When the agent recommends a",
        "> product it names the product + cites the TDS, and flags that",
        "> surface prep, mix ratios, and cure times must be confirmed",
        "> against the current TDS (manufacturers revise data sheets).",
        "",
        f"_Extracted from `spec_products` (superstars.db) — {len(rows)} products "
        f"across {len(by_cat)} categories. The DB is gitignored; this file is the "
        f"version-controlled corpus of record._",
        "",
        "## MVP deep-focus (operator decision Q7 — the frequent three)",
        "- **Concrete repair** — `products/concrete_repair.md` (C10).",
        "- **Sealants / joint design** — `products/sealants_joint_design.md` (C13); complements this Sika index.",
        "- **Brick repointing** — `products/brick_masonry_repointing.md` (C12).",
        "",
        "_The frequent-three product write-ups are committed under `corpus/products/`",
        "(#198 Batch 2). Sto/EIFS (C11) and roofing (C14) remain v1, not this batch._",
        "",
        "## Products by category",
        "",
    ]
    for cat in sorted(by_cat):
        items = by_cat[cat]
        lines.append(f"### {cat}  ({len(items)})")
        lines.append("")
        lines.append("| Product | Code | Line | TDS |")
        lines.append("|---|---|---|---|")
        for r in items:
            name = (r["product_name"] or "").replace("|", "/")
            code = (r["product_code"] or "").replace("|", "/")
            line = (r["product_line"] or "").replace("|", "/")
            url = r["spec_url"] or ""
            tds = f"[TDS]({url})" if url else "—"
            lines.append(f"| {name} | {code} | {line} | {tds} |")
        lines.append("")

    SIKA_DIR.mkdir(parents=True, exist_ok=True)
    (SIKA_DIR / "sika_spec_index.md").write_text("\n".join(lines), encoding="utf-8")
    (SIKA_DIR / "sika_spec_index.json").write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "items": len(rows),
        "categories": len(by_cat),
        "source": "spec_products (superstars.db)",
    }


def write_manifest_and_stamp(ch33: dict, sika: dict) -> str:
    today = _dt.date.today().isoformat()  # local date (CLAUDE.md dates rule)

    manifest = {
        "corpus_name": "construction_specialist",
        "phase": "A / v0.2 (MVP scaffolding + frequent-three product systems, #198 Batch 2)",
        "built_date_local": today,
        "included": [
            {
                "id": "C1",
                "title": "NYC Building Code Chapter 33 — Safeguards During Construction",
                "source": "toolbox_talks_data.py (19 toolbox talks, §-tagged)",
                "files": ["chapter33/ch33_index.md", "chapter33/ch33_index.json"],
                "items": ch33["items"],
                "note": "Orientation distillation; source PDF (DOB BC-33) NOT in repo "
                        "(data_room/dob_codes shows 'Pending Download'). Controlling "
                        "text is the official code.",
            },
            {
                "id": "C2",
                "title": "Sika product specifications",
                "source": "spec_products table (superstars.db)",
                "files": ["sika/sika_spec_index.md", "sika/sika_spec_index.json"],
                "items": sika["items"],
                "categories": sika["categories"],
                "note": "Controlling document per product is the manufacturer TDS at spec_url.",
            },
            {
                "id": "C10",
                "title": "Concrete repair systems",
                "source": "validated public/manufacturer technical literature (curated 2026-05-28)",
                "files": ["products/concrete_repair.md"],
                "note": "Frequent-three (decision Q7). Orientation; controlling authority = "
                        "manufacturer TDS + EOR. Carries the incipient-anode + post-tension cautions.",
            },
            {
                "id": "C13",
                "title": "Sealants & joint design",
                "source": "validated public/manufacturer technical literature (curated 2026-05-28)",
                "files": ["products/sealants_joint_design.md"],
                "note": "Frequent-three (decision Q7). Orientation; controlling authority = "
                        "manufacturer TDS + design professional. Complements the Sika index (C2).",
            },
            {
                "id": "C12",
                "title": "Brick masonry & repointing",
                "source": "validated public technical literature incl. NPS Brief 2 (curated 2026-05-28)",
                "files": ["products/brick_masonry_repointing.md"],
                "note": "Frequent-three (decision Q7). Orientation; controlling authority = "
                        "design professional / mortar-analysis lab. Softer-than-brick principle.",
            },
        ],
        "deferred_public_sources": [
            {"id": "C3", "title": "SPRAT reference summary", "source": "sprat.org (public)"},
            {"id": "C4", "title": "IRATA reference summary", "source": "irata.org (public)"},
            {"id": "C5", "title": "Local Law 11 / FISP reference", "source": "NYC.gov / DOB, 1 RCNY §103-04"},
            {"id": "C6", "title": "Local Law 77 of 2023 summary", "source": "NYC.gov / DOB"},
            {"id": "C7", "title": "Local Law 126 / Parking Structure Inspection", "source": "NYC.gov / DOB, 1 RCNY §103-13"},
            {"id": "C11", "title": "Sto & EIFS systems — deep write-up", "source": "Sto + public technical literature"},
            {"id": "C14", "title": "Roofing membranes & assemblies — deep write-up", "source": "manufacturer public literature"},
            {"id": "C15-C20", "title": "Software / engineering / architectural / NYC-process references", "source": "public docs"},
        ],
        "deferred_note": "Deferred items are a SEPARATE later batch — NOT curated in #198. "
                         "Each, when added, records retrieval date + source URL so staleness "
                         "is auditable (spec §4 governance).",
    }

    manifest_path = HERE / "corpus_manifest.json"
    manifest_json = json.dumps(manifest, indent=2, ensure_ascii=False)
    manifest_path.write_text(manifest_json, encoding="utf-8")

    digest = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()[:12]
    corpus_version = f"v0.2-{today}-{digest}"
    (HERE / "CORPUS_VERSION").write_text(corpus_version + "\n", encoding="utf-8")
    return corpus_version


def main() -> int:
    global DB_PATH
    import argparse
    ap = argparse.ArgumentParser(description="Rebuild the Construction Specialist corpus indexes.")
    ap.add_argument("--db", default=None, help=f"Isolated database (or set {db_guard.ENV_VAR}).")
    try:
        DB_PATH = db_guard.resolve(ap.parse_args().db)
    except db_guard.RefusedDB as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if not TALKS_PY.exists():
        print(f"ERROR: {TALKS_PY} not found", file=sys.stderr)
        return 1
    ch33 = build_ch33_index()
    sika = build_sika_index()
    version = write_manifest_and_stamp(ch33, sika)
    print(f"[corpus] Chapter 33 index: {ch33['items']} topics")
    print(f"[corpus] Sika spec index: {sika['items']} products, "
          f"{sika['categories']} categories")
    print(f"[corpus] CORPUS_VERSION = {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
