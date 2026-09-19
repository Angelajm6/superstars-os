#!/usr/bin/env python3
"""#325 — site coordinates on projects. Idempotent, both backends.

Adds `site_lat`, `site_lng`, `geofence_m` to `projects` so the worker app can
check a worker in to ANY job (until now the geofence and the project were hard-
wired to the first Bronx site). Seeds the legacy coordinates onto FR-BX-001 when
that project exists and has none, so production's existing control is unchanged
the day this lands.

Usage:  python apply_project_site_325.py       (honors SSC_DB_URL like every #2xx migration)
"""
from __future__ import annotations

import db_layer

LEGACY_CODE, LEGACY_LAT, LEGACY_LNG, LEGACY_RADIUS = "FR-BX-001", 40.8083, -73.9162, 200


def _has_column(conn, table: str, column: str) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute(
            "SELECT 1 AS x FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
            (table, column)).fetchone())
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def ensure(conn) -> dict:
    added = []
    for col, ddl in (("site_lat", "REAL"), ("site_lng", "REAL"), ("geofence_m", "INTEGER")):
        if not _has_column(conn, "projects", col):
            conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
            added.append(col)
    conn.commit()
    seeded = conn.execute(
        "UPDATE projects SET site_lat = ?, site_lng = ?, geofence_m = ? "
        "WHERE project_code = ? AND site_lat IS NULL AND site_lng IS NULL",
        (LEGACY_LAT, LEGACY_LNG, LEGACY_RADIUS, LEGACY_CODE)).rowcount or 0
    conn.commit()
    return {"added": added, "legacy_seeded": int(seeded)}


def main() -> int:
    conn = db_layer.connect(pragma_fk=True)
    try:
        r = ensure(conn)
    finally:
        conn.close()
    print(f"[#325] projects.site_lat/site_lng/geofence_m ready (added={r['added']}, legacy seeded={r['legacy_seeded']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
