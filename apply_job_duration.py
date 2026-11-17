#!/usr/bin/env python3
"""Job duration — projects.anticipated_completion / projects.actual_completion on BOTH backends.

  anticipated_completion  the end date expected when the job started; it does not move with the
                          schedule (target_completion does), so it is the baseline duration
  actual_completion       the day the work finished; stamped with today when the job is closed,
                          editable, cleared when the job is reopened

TEXT like start_date / target_completion (#334): the app writes ISO strings and compares them as
strings. Idempotent; ensured at boot by project_onboard.register.

Usage:  python apply_job_duration.py       (honors SSC_DB_URL like every dual-backend migration)
"""
from __future__ import annotations

import sys

import db_layer
from apply_project_dates_334 import _has_column

COLUMNS = (("anticipated_completion", "TEXT"), ("actual_completion", "TEXT"))


def ensure(conn) -> dict:
    added = []
    for col, ddl in COLUMNS:
        if not _has_column(conn, "projects", col):
            conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
            added.append(col)
    conn.commit()
    return {"added": added}


def main() -> int:
    conn = db_layer.connect(pragma_fk=True)
    try:
        r = ensure(conn)
    finally:
        conn.close()
    print(f"[job-duration] projects completion columns ready; added={r['added']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
