"""#229 — apply schema_project_docs.sql (project_documents + document_requirements)
and seed the global required-docs checklist from PROJECT_DOCUMENTS_TAXONOMY.md.

Idempotent (safe to re-run): CREATE TABLE IF NOT EXISTS for the tables +
INSERT OR IGNORE for the seed (the UNIQUE(category, requirement_key) makes the
seed a no-op on re-run). Mirrors the split_statements pattern in
apply_riggers_schema.py per the CLAUDE.md migration rule.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DB = SCRIPT_DIR / "superstars.db"
SQL = SCRIPT_DIR / "schema_project_docs.sql"

# (category, requirement_key, label, sort_order) — the per-category required-doc
# checklist, authored from the taxonomy (NYC façade-restoration GC: LL11/FISP).
REQUIREMENTS = [
    ("PERMITS", "pw2", "DOB Work Permit (PW2)", 1),
    ("PERMITS", "sidewalk_shed", "Sidewalk-Shed / Supported-Scaffold Permit", 2),
    ("PERMITS", "best_permit", "Suspended-Scaffold (BEST) Permit", 3),
    ("PERMITS", "fisp_ack", "FISP Filing Acknowledgment", 4),
    ("PERMITS", "after_hours", "After-Hours / Street-Occupancy Variance", 5),
    ("DRAWINGS", "approved_arch", "DOB-Approved Architectural Set", 1),
    ("DRAWINGS", "approved_struct", "DOB-Approved Structural Set", 2),
    ("DRAWINGS", "scaffold_eng", "Scaffold & Rigging Engineering (PE-stamped)", 3),
    ("DRAWINGS", "drop_layout", "Drop / Rigging Layout", 4),
    ("DRAWINGS", "shop_drawings", "Shop Drawings & Submittals", 5),
    ("CONTRACTS", "owner_contract", "Executed Owner Contract / AIA", 1),
    ("CONTRACTS", "sov", "Schedule of Values (SOV)", 2),
    ("CONTRACTS", "coi", "Certificate of Insurance (COI)", 3),
    ("CONTRACTS", "pay_app", "Payment Application", 4),
    ("INSPECTIONS", "fisp_report", "FISP / LL11 Inspection Report (QEWI)", 1),
    ("INSPECTIONS", "probe_report", "Probe / Condition Report", 2),
    ("INSPECTIONS", "tr1", "TR1 Special-Inspection Reports", 3),
    ("INSPECTIONS", "dob_signoff", "DOB Sign-Offs", 4),
    ("SAFETY", "site_safety_plan", "Site Safety Plan", 1),
    ("SAFETY", "sst_records", "SST Cards / Records", 2),
    ("SAFETY", "rope_access_plan", "Rope Access Plan (SPRAT/IRATA)", 3),
    ("SAFETY", "sds", "SDS Binder", 4),
    ("SAFETY", "toolbox_log", "Toolbox-Talk Sign-In Log", 5),
    ("CLOSEOUT", "completion_letter", "Letter of Completion / Sign-Off", 1),
    ("CLOSEOUT", "warranties", "Warranties", 2),
    ("CLOSEOUT", "final_fisp", "Final FISP Filing (Safe / SWARMP)", 3),
    ("CLOSEOUT", "lien_waivers", "Lien Waivers", 4),
]


def split_statements(sql_text: str):
    out, cur = [], []
    for line in sql_text.splitlines():
        s = line.strip()
        if s.startswith("--") or not s:
            continue
        cur.append(line)
        if s.endswith(";"):
            out.append("\n".join(cur))
            cur = []
    if cur:
        out.append("\n".join(cur))
    return out


def main() -> int:
    if not DB.exists():
        print(f"ABORT: {DB} not found")
        return 1
    conn = sqlite3.connect(str(DB))
    conn.execute("PRAGMA busy_timeout=60000;")
    applied = skipped = 0
    for stmt in split_statements(SQL.read_text(encoding="utf-8")):
        try:
            conn.execute(stmt)
            applied += 1
        except sqlite3.OperationalError as e:
            if "already exists" in str(e) or "duplicate column" in str(e):
                skipped += 1
            else:
                conn.close()
                raise
    seeded = 0
    for cat, key, label, order in REQUIREMENTS:
        cur = conn.execute(
            "INSERT OR IGNORE INTO document_requirements (category, requirement_key, label, sort_order) "
            "VALUES (?,?,?,?)", (cat, key, label, order))
        seeded += cur.rowcount
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM document_requirements").fetchone()[0]
    by_cat = conn.execute(
        "SELECT category, COUNT(*) FROM document_requirements GROUP BY category ORDER BY category").fetchall()
    conn.close()
    print(f"schema: {applied} applied, {skipped} skipped | requirements: {seeded} newly seeded, {total} total")
    print("per category:", dict(by_cat))
    return 0


if __name__ == "__main__":
    sys.exit(main())
