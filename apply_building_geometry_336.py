#!/usr/bin/env python3
"""#336 — building_geometry: the cached footprint / height / neighbours per job for the 3D
view. Idempotent, both backends; the app ensures it at boot too (building_3d.register).

Usage:  python apply_building_geometry_336.py    (honors SSC_DB_URL like every #2xx migration)
"""
from __future__ import annotations

import sys

import db_layer


def main() -> int:
    import building_3d
    conn = db_layer.connect(pragma_fk=True)
    try:
        building_3d.ensure_schema(conn)
    finally:
        conn.close()
    print("[#336] building_geometry ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
