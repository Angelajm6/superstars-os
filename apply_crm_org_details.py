#!/usr/bin/env python3
"""Edit client gains the client's own details — crm_organization.email / phone / website /
address_line / city / state / zip. Idempotent, both backends; the app ensures it at boot too
(crm.ensure_org_details from server.py).

Usage:  python apply_crm_org_details.py    (honors SSC_DB_URL like every #2xx migration)
"""
from __future__ import annotations

import sys

import db_layer


def main() -> int:
    import crm
    conn = db_layer.connect(pragma_fk=True)
    try:
        crm.ensure_org_details(conn)
    finally:
        conn.close()
    print("[crm-org-details] crm_organization email/phone/website/address columns ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
