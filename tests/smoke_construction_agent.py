"""#198 — Construction Specialist Agent provenance smoke (synthetic-only).

Exercises the PII-safe provenance writer end-to-end against a THROWAWAY
SQLite database (the #198 schema in a temp dir), using ONLY synthetic
SMK--prefixed rows. Never the real superstars.db (CLAUDE.md). This is the
regression net for the agent's audit-trail surface.

Asserts:
  1. log_interaction writes EXACTLY ONE row per call (one-per-call /
     determinism — the core invariant the operator verifies).
  2. The PII scrub redacts phone / rate / PIN / SSN shapes but preserves
     §-code-sections and fractions.
  3. asked_at and created_at are LOCAL dates (today), never UTC.
  4. Stored rows are PII-safe: no phone/rate/PIN/SSN shapes survive.
  5. interaction_ids are unique across calls (no dupes / collisions).
  6. Synthetic rows carry the SMK- prefix and are fully cleaned up.
  7. db_guard: no default database; the real superstars.db (by name or by
     path), a missing file and a bare CLI call are all refused before any write.

PII discipline: prints booleans + counts only; never the stored text.

Run:
  python tests/smoke_construction_agent.py
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR / "agents" / "construction_specialist"))

import provenance as prov  # noqa: E402
import db_guard  # noqa: E402
SCHEMA = SCRIPT_DIR / "schema_construction_agent_provenance.sql"

SMK = "SMK-"
PASS, FAIL = 0, 0

# PII shapes that must NEVER appear in a stored row.
_PII_SHAPES = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                          # SSN
    re.compile(r"\b\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),        # phone
    re.compile(r"\$\s?\d", ),                                       # currency
    re.compile(r"\bPIN\s*[:#]?\s*\d", re.I),                       # PIN value
]


def check(label: str, ok: bool, note: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" — {note}" if note else ""))


def count_smk(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM construction_agent_provenance "
        "WHERE interaction_id LIKE 'SMK-%'").fetchone()[0]


def cleanup(conn):
    conn.execute(
        "DELETE FROM construction_agent_provenance "
        "WHERE interaction_id LIKE 'SMK-%'")
    conn.commit()


def refused(path) -> bool:
    try:
        db_guard.resolve(path)
        return False
    except db_guard.RefusedDB:
        return True


def guard_checks(tmp: Path):
    os.environ.pop(db_guard.ENV_VAR, None)
    check("guard: no database given -> refused", refused(None))
    # only the path string is checked against the real one; it is never opened
    check("guard: the repo's real superstars.db -> refused", refused(str(SCRIPT_DIR / "superstars.db")))
    decoy = tmp / "superstars.db"
    decoy.write_bytes(b"")
    check("guard: any file named superstars.db -> refused", refused(str(decoy)))
    check("guard: a missing file -> refused (never created)",
          refused(str(tmp / "nope.db")) and not (tmp / "nope.db").exists())
    try:
        prov.log_interaction(question_text="SMK refused", synthetic=True)
        check("log_interaction without a database raises", False)
    except db_guard.RefusedDB:
        check("log_interaction without a database raises", True)
    env = {k: v for k, v in os.environ.items() if k != db_guard.ENV_VAR}
    r = subprocess.run([sys.executable, str(SCRIPT_DIR / "agents" / "construction_specialist" / "provenance.py"),
                        "--question", "SMK cli", "--synthetic"], capture_output=True, text=True, env=env)
    check("CLI without --db exits 2 and writes nothing", r.returncode == 2 and "not logged" in r.stderr)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ssc-agent-smoke-"))
    guard_checks(tmp)
    db = tmp / "agent_test.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    os.environ[db_guard.ENV_VAR] = str(db)   # the helper reads it; nothing else is ever opened
    try:
        cleanup(conn)
        base = count_smk(conn)
        check("baseline synthetic rows == 0", base == 0, f"got {base}")

        # 1. scrub behavior (no DB write)
        scrubbed = prov.scrub_pii(
            "Per Chapter 33 §3314, a 1/2-inch crack; call 917-555-1234, "
            "$45.00/hr, PIN 1234, SSN 123-45-6789 for W-0007")
        check("scrub preserves §-section", "§3314" in scrubbed)
        check("scrub preserves fraction 1/2-inch", "1/2-inch" in scrubbed)
        check("scrub preserves W-#### token", "W-0007" in scrubbed)
        check("scrub redacts phone", "917-555-1234" not in scrubbed)
        check("scrub redacts rate", "45.00/hr" not in scrubbed and "$45" not in scrubbed)
        check("scrub redacts PIN value", "1234" not in scrubbed.replace("§3314", ""))
        check("scrub redacts SSN", "123-45-6789" not in scrubbed)

        # 2. one-row-per-call across 3 distinct calls
        ids = []
        for i in range(3):
            before = count_smk(conn)
            res = prov.log_interaction(
                question_text=f"SMK scenario {i}: FISP window for W-0007 drop?",
                sources_cited="ch33_index.md §3314; general-knowledge-unverified",
                answer_summary="SMK synthetic summary — no PII",
                synthetic=True)
            after = count_smk(conn)
            ids.append(res["interaction_id"])
            check(f"call {i} wrote exactly one row", after - before == 1,
                  f"delta={after-before}")
            check(f"call {i} id carries SMK- prefix",
                  res["interaction_id"].startswith(SMK))
            check(f"call {i} stamped CORPUS_VERSION",
                  bool(res["corpus_version"]) and res["corpus_version"] != "unknown")

        # 3. determinism: same question twice -> 2 distinct rows
        b = count_smk(conn)
        r1 = prov.log_interaction(question_text="SMK dup-check same question",
                                  synthetic=True)
        r2 = prov.log_interaction(question_text="SMK dup-check same question",
                                  synthetic=True)
        check("same question twice -> 2 new rows", count_smk(conn) - b == 2)
        check("two calls -> distinct interaction_ids",
              r1["interaction_id"] != r2["interaction_id"])

        # 4. unique ids overall
        all_ids = ids + [r1["interaction_id"], r2["interaction_id"]]
        check("all interaction_ids unique", len(set(all_ids)) == len(all_ids))

        # 5. inspect stored rows: local dates + PII-safe
        rows = conn.execute(
            "SELECT interaction_id, asked_at, question_text, sources_cited, "
            "answer_summary, created_at, operator_disposition "
            "FROM construction_agent_provenance "
            "WHERE interaction_id LIKE 'SMK-%'").fetchall()
        today = _dt.date.today().isoformat()
        local_ok = all(r[1].startswith(today) and r[5].startswith(today) for r in rows)
        check("asked_at + created_at are LOCAL today (not UTC)", local_ok)
        disp_ok = all(r[6] == "pending" for r in rows)
        check("operator_disposition defaults to 'pending'", disp_ok)

        pii_free = True
        for r in rows:
            blob = " ".join(str(x) for x in (r[2], r[3], r[4]))
            if any(p.search(blob) for p in _PII_SHAPES):
                pii_free = False
                break
        check("no PII shapes (phone/rate/PIN/SSN) in stored rows", pii_free)

        # 6. write a row WITH PII and confirm it is scrubbed before storage
        leaky = prov.log_interaction(
            question_text="SMK leak-test 917-555-0000 $99/hr PIN 4321",
            synthetic=True)
        stored = conn.execute(
            "SELECT question_text FROM construction_agent_provenance "
            "WHERE interaction_id=?", (leaky["interaction_id"],)).fetchone()[0]
        leak_clean = not any(p.search(stored) for p in _PII_SHAPES)
        check("PII passed to helper is scrubbed before storage", leak_clean)

    finally:
        cleanup(conn)
        final = count_smk(conn)
        conn.close()

    print()
    print(f"  cleanup: synthetic rows remaining = {final}")
    check("all synthetic rows cleaned up", final == 0, f"left {final}")
    print()
    print(f"=== Construction Agent provenance smoke: {PASS} passed, {FAIL} failed ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
