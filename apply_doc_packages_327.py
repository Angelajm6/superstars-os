#!/usr/bin/env python3
"""#327 — permits & insurance packages: schema. Idempotent, both backends.

Creates `document_package` and `document_package_item` (see doc_packages.py).
server.py runs the same ensure at boot; this script exists for the migration
ledger (#306), the preprod fixture builder and manual runs.

Usage:  python apply_doc_packages_327.py       (honors SSC_DB_URL like every #2xx migration)
"""
from __future__ import annotations

import db_layer
import doc_packages


def main() -> int:
    conn = db_layer.connect(pragma_fk=True)
    try:
        doc_packages.ensure_schema(conn)
    finally:
        conn.close()
    print("[#327] document_package, document_package_item ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
