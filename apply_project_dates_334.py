#!/usr/bin/env python3
"""#334 — projects.start_date / projects.target_completion on BOTH backends.

The columns were born in `schema_project_assignments.sql` (a sqlite-only legacy
migration that joined the fixture list in #310, on 2026-09-18). A Postgres database
built before that day — preprod, 2026-09-04 — never got them, so the calendar (#332)
and the portal's Projects tab (#330) failed with "column p.start_date does not exist"
while every CI fixture (built after) had them. Same lesson as #322: anything the app
reads must be ensured dual-backend, at boot.

TEXT on purpose: the app writes ISO strings and compares them as strings.

Usage:  python apply_project_dates_334.py       (honors SSC_DB_URL like every #2xx migration)
"""
from __future__ import annotations

import sys

import db_layer

COLUMNS = (("start_date", "TEXT"), ("target_completion", "TEXT"))


def _has_column(conn, table: str, column: str) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute(
            "SELECT 1 AS x FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
            (table, column)).fetchone())
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


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
    print(f"[#334] projects date columns ready; added={r['added']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
