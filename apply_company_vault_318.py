#!/usr/bin/env python3
"""#318 — company vault: schema. Idempotent, both backends.

Creates `vehicle` and `company_document_file`, and adds `vehicle_id` / `doc_year`
to `company_document` (see company_vault.py). server.py runs the same ensure at
boot through company_portal.ensure_schema, so an existing deploy heals itself;
this script exists for the migration ledger (#306), the preprod fixture builder
and manual runs.

Usage:  python apply_company_vault_318.py       (honors SSC_DB_URL like every #2xx migration)
"""
from __future__ import annotations

import company_portal
import db_layer


def main() -> int:
    conn = db_layer.connect(pragma_fk=True)
    try:
        company_portal.ensure_schema(conn)   # company_document first, then the vault pieces
    finally:
        conn.close()
    print("[#318] vehicle, company_document_file, company_document.vehicle_id/doc_year ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
