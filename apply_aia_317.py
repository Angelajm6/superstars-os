#!/usr/bin/env python3
"""#317 — AIA progress billing tables: billing_sov, pay_application, pay_application_line.

Idempotent, both backends. The app also ensures the same schema at boot
(aia_billing.register), so an existing deploy heals itself; this script exists for the
#306 ledger and the fixture builder.

Usage:  python apply_aia_317.py          (honors SSC_DB_URL like every #2xx migration)
"""
from __future__ import annotations

import sys

import db_layer


def main() -> int:
    import aia_billing
    conn = db_layer.connect(pragma_fk=True)
    try:
        aia_billing.ensure_schema(conn)
    finally:
        conn.close()
    print("[#317] billing_sov / pay_application / pay_application_line ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
