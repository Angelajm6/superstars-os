#!/usr/bin/env python3
"""#319 — accounts payable table `payable`. Idempotent, both backends. The app ensures the
same schema at boot (payables.register); this script exists for the #306 ledger and the
fixture builder.

Usage:  python apply_payables_319.py       (honors SSC_DB_URL like every #2xx migration)
"""
from __future__ import annotations

import sys

import db_layer


def main() -> int:
    import payables
    conn = db_layer.connect(pragma_fk=True)
    try:
        payables.ensure_schema(conn)
    finally:
        conn.close()
    print("[#319] payable table ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
