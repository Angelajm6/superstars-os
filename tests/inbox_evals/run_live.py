#!/usr/bin/env python3
"""Score the REAL model on the Inbox test set (piece 5, 2026-11-18). Run by hand; it costs money (~30 Claude passes).

    ANTHROPIC_API_KEY=... venv/bin/python tests/inbox_evals/run_live.py --yes [--case signed-proposal-not-marked-sent] [--max-usd 5]

The key comes from 1Password into the environment for this one command; it is never written anywhere. Everything runs on
a fresh ISOLATED database and a temp data root with the fictional cases only: never the real database, never real mail.
The sorting step is planted (business email that needs something) so the score is about what Claude SUGGESTS.

For each case: the tools Claude proposed, what it must have proposed (missing), what it must not (wrong), and what the
server caught (refused). A case passes when nothing is missing or wrong. Prints a scorecard and the total cost; writes
the details as JSON to a temp file (the path is printed). Use it after changing Claude's instructions or tools, and
compare against the last run.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--yes", action="store_true", help="I know this calls the real model and costs money")
    ap.add_argument("--case", action="append", help="only these case ids (repeatable)")
    ap.add_argument("--max-usd", type=float, default=5.0, help="stop when the run has cost this much (default 5)")
    a = ap.parse_args()
    if not a.yes:
        print("This calls the real model (about 30 passes, a few dollars). Re-run with --yes.")
        return 2
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        print("ANTHROPIC_API_KEY is not set (take it from 1Password for this command only).")
        return 2
    tmp = tempfile.mkdtemp(prefix="ssc_ievl_live_")
    os.environ["SSC_ALLOW_TEST_SEAMS"] = "1"   # only the sorting step is planted; Claude itself is real
    os.environ.pop("SSC_ASSISTANT_FAKE", None)
    from _isolated_db import build_isolated_sqlite
    os.environ["SSC_DB_URL"] = build_isolated_sqlite()   # always a fresh isolated copy: never the real database
    import secret_box
    os.environ.update({"SSC_DATA_ROOT": str(Path(tmp) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(Path(tmp) / "mail"), "SSC_GMAIL_POLL": "0",
                       "SSC_REMINDERS": "0", "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(),
                       "SSC_INBOX_TRIAGE_FAKE": json.dumps({"kind": "client_question", "business": True, "needs_action": True, "summary": "eval"})})
    import db_layer
    from inbox_evals import engine
    import inbox
    from smoke_inbox_evals import users, cleanup
    cleanup()
    conn = db_layer.connect(pragma_fk=True)
    rows, spent = [], 0.0
    try:
        ids = users(conn)
        for n, case in enumerate(engine.cases(), start=1):
            if a.case and case["id"] not in a.case:
                continue
            if spent >= a.max_usd:
                print(f"stopped: ${spent:.2f} spent (--max-usd {a.max_usd})")
                break
            refs = engine.build(conn, case, n, ids)
            inbox.process(conn, refs["mail_id"], force=True)
            out = engine.outcome(conn, refs["mail_id"])
            r = engine.score_live(case, out)
            spent += r["cost_micro"] / 1_000_000
            rows.append(r)
            print(f"  {'pass' if r['ok'] else 'FAIL'}  {case['id']:<40} proposed: {', '.join(r['proposed']) or '(nothing)'}"
                  + (f"  missing: {', '.join(r['missing'])}" if r["missing"] else "")
                  + (f"  wrong: {', '.join(r['wrong'])}" if r["wrong"] else "")
                  + (f"  caught by the server: {', '.join(r['caught'])}" if r["caught"] else ""))
    finally:
        conn.close()
        cleanup()
        shutil.rmtree(tmp, ignore_errors=True)
    ok = sum(1 for r in rows if r["ok"])
    out_path = Path(tempfile.gettempdir()) / f"inbox_evals_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    out_path.write_text(json.dumps({"when": datetime.now().isoformat(timespec="seconds"), "score": f"{ok}/{len(rows)}",
                                    "cost_usd": round(spent, 2), "cases": rows}, indent=2), encoding="utf-8")
    print(f"\nScore: {ok}/{len(rows)} cases right · cost ${spent:.2f} · details: {out_path}")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
