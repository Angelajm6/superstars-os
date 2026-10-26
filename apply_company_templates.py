#!/usr/bin/env python3
"""Company templates (2026-10-26) — company_template, the template library on the company console.
Idempotent, both backends; the app ensures it at boot too (company_templates.ensure_schema from
company_portal.ensure_schema).

Usage:  python apply_company_templates.py    (honors SSC_DB_URL like every #2xx migration)
"""
from __future__ import annotations

import sys

import db_layer


def main() -> int:
    import company_templates
    conn = db_layer.connect(pragma_fk=True)
    try:
        company_templates.ensure_schema(conn)
    finally:
        conn.close()
    print("[company-templates] company_template ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
