#!/usr/bin/env python3
"""Job duration v2 — projects.original_completion on BOTH backends (after apply_job_duration.py).

What the screens call them (v2, 2026-11-18: one end date to edit, not two):
  target_completion       "Anticipated completion": the end date the job is expected to finish.
                          The calendar, days left, pace and the past-its-end warnings read it.
                          (Column from #334; the name stays because everything reads it.)
  original_completion     the FIRST anticipated date, kept by the app, never edited: what the
                          duration is measured against once the date moves
                          (project_details.sync_original keeps it; cleared with the date)
  actual_completion       the day the work finished; stamped on close, cleared on reopen
  anticipated_completion  RETIRED (v1, 2026-11-17, an editable baseline beside the target).
                          Its values move to original_completion (and to target_completion
                          where a job had no target); then it is emptied. Unused by the app.

TEXT like start_date / target_completion (#334). Idempotent and safe on every boot: each
backfill only fills an empty value from another one, and the retired column is empty after the
first run. Ensured at boot by project_onboard.register.

Usage:  python apply_completion_original.py       (honors SSC_DB_URL like every dual-backend migration)
"""
from __future__ import annotations

import sys

import db_layer
from apply_project_dates_334 import _has_column

import apply_job_duration  # noqa: E402  (anticipated / actual completion must exist first)

COLUMNS = (("original_completion", "TEXT"),)

_SET = "{c} IS NOT NULL AND {c} <> ''"
_EMPTY = "({c} IS NULL OR {c} = '')"


def ensure(conn) -> dict:
    apply_job_duration.ensure(conn)
    added = []
    for col, ddl in COLUMNS:
        if not _has_column(conn, "projects", col):
            conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
            added.append(col)
    a, t, o = "anticipated_completion", "target_completion", "original_completion"
    # v1 data: the baseline becomes the original; a job with only the baseline gets it as its date
    conn.execute(f"UPDATE projects SET {o} = {a} WHERE {_EMPTY.format(c=o)} AND {_SET.format(c=a)}")
    conn.execute(f"UPDATE projects SET {t} = {a} WHERE {_EMPTY.format(c=t)} AND {_SET.format(c=a)}")
    conn.execute(f"UPDATE projects SET {a} = NULL WHERE {a} IS NOT NULL")
    # every dated job has an original (the rule project_details.sync_original keeps from now on)
    conn.execute(f"UPDATE projects SET {o} = {t} WHERE {_EMPTY.format(c=o)} AND {_SET.format(c=t)}")
    conn.commit()
    return {"added": added}


def main() -> int:
    conn = db_layer.connect(pragma_fk=True)
    try:
        r = ensure(conn)
    finally:
        conn.close()
    print(f"[job-duration v2] projects.original_completion ready; added={r['added']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
