#!/usr/bin/env python3
"""Documents drop zone — project_doc_intake (files awaiting tag approval) +
project_documents.type_key / ai_extract. Idempotent, both backends; the app ensures
it at boot too (doc_intake.ensure_schema from server.py).

Usage:  python apply_doc_intake.py    (honors SSC_DB_URL like every #2xx migration)
"""
from __future__ import annotations

import sys

import db_layer


def main() -> int:
    import doc_intake
    conn = db_layer.connect(pragma_fk=True)
    try:
        doc_intake.ensure_schema(conn)
    finally:
        conn.close()
    print("[doc-intake] project_doc_intake + project_documents.type_key/ai_extract ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
