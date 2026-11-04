#!/usr/bin/env python3
"""Clients revamp (2026-10-28): crm_organization gains client_kind + the billing defaults
(payment terms, deposit, retainage, pay apps, lien waivers, capital improvement, certified payroll,
billing email, COI wording, managing agent); crm_contact gains roles + is_primary. Idempotent, both
backends; the app ensures it at boot too (crm.ensure_client_profile from server.py).

Usage:  python apply_client_profile.py    (honors SSC_DB_URL like every #2xx migration)
"""
from __future__ import annotations

import sys

import db_layer


def main() -> int:
    import crm
    conn = db_layer.connect(pragma_fk=True)
    try:
        crm.ensure_client_profile(conn)
    finally:
        conn.close()
    print("[client-profile] crm_organization kind + billing columns and crm_contact roles ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
