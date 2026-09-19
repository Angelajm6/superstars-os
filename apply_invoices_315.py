#!/usr/bin/env python3
"""#315 — invoices & receivables: schema. Idempotent, both backends.

Creates `invoice` and `invoice_payment` (see invoices.py). server.py runs the same
ensure at boot, so an existing deploy heals itself; this script exists for the
migration ledger (#306), the preprod fixture builder and manual runs.

Usage:  python apply_invoices_315.py          (honors SSC_DB_URL like every #2xx migration)
"""
from __future__ import annotations

import db_layer
import invoices


def main() -> int:
    conn = db_layer.connect(pragma_fk=True)
    try:
        invoices.ensure_schema(conn)
    finally:
        conn.close()
    print("[#315] invoice, invoice_payment ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
