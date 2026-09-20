#!/usr/bin/env python3
"""#307 — proposals: schema. Idempotent, both backends.

Creates `proposal`, `proposal_line`, `proposal_event` (see proposals.py). server.py
runs the same ensure at boot, so an existing deploy heals itself; this script exists
for the migration ledger (#306), the preprod fixture builder and manual runs.

Usage:  python apply_proposals_307.py          (honors SSC_DB_URL like every #2xx migration)
"""
from __future__ import annotations

import db_layer
import proposals


def main() -> int:
    conn = db_layer.connect(pragma_fk=True)
    try:
        proposals.ensure_schema(conn)
    finally:
        conn.close()
    print("[#307] proposal, proposal_line, proposal_event ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
