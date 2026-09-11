#!/usr/bin/env python3
"""#304 — buildings as a first-class entity: schema + backfill. Idempotent, both backends.

Creates `building`, adds `projects.building_id` and `estimate.building_id`, then links
every project / estimate that has an address (creating the building rows on first
sight). Safe to re-run: a second run links nothing and creates nothing.

server.py also runs the same ensure + backfill at boot, so an existing deploy heals
itself; this script exists for the preprod fixture builder and manual runs.

Usage:  python apply_buildings_304.py          (honors SSC_DB_URL like every #2xx migration)
"""
from __future__ import annotations

import db_layer
import buildings


def main() -> int:
    conn = db_layer.connect(pragma_fk=True)
    try:
        buildings.ensure_schema(conn)
        stats = buildings.backfill(conn)
    finally:
        conn.close()
    print(f"[#304] building table ready; backfill: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
