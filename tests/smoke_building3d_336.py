#!/usr/bin/env python3
"""#336 — the building in 3D, stage 1: behavioral guard.

Boots the real app (`import server`) on an ISOLATED database. The city providers are
STUBBED (no network in CI): a fake fetch answers GeoSearch and the footprints dataset.
Proves:
  * schema idempotent; migration script runs
  * resolve: an address geocodes to a BIN, the footprint and neighbours are fetched once
    and cached (a second call makes no request); refresh re-fetches; a provider failure
    is recorded and the model still answers with the fallback box; a linked building's
    BIN skips geocoding
  * maths: local metres around the centroid; a CCW square gives four walls with outward
    normals N/E/S/W and the right bearings; compass buckets; face codes from labels
  * model: heights in metres, stories estimated, neighbours in local coords, every
    traced confirmed elevation mapped to its wall (drafts hidden from external), the
    fallback sized from the traced facades
  * gates: anon 401; a client of another job 403; the client of THIS job 200 (external
    audience, no drafts); refresh POST internal only
  * the served markup page carries the 3D toggle, the stage, the hooks and the module;
    the vendored three.js is served publicly
Run:  venv/bin/python tests/smoke_building3d_336.py
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_b3d_336_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import building_3d as b3  # noqa: E402
import buildings  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "b3d336"
PW = "BuildingSmoke2026"
A, B = "B3D336-A", "B3D336-B"
LAT, LNG = 40.80065, -73.910026
BIN = "2004029"

# a ~20 m × 14 m rectangle around (LNG, LAT), lng/lat pairs, CCW
_K = math.cos(math.radians(LAT))
def _pt(dx_m, dy_m): return [LNG + dx_m / (111320.0 * _K), LAT + dy_m / 110540.0]
RING = [_pt(-10, -7), _pt(10, -7), _pt(10, 7), _pt(-10, 7), _pt(-10, -7)]
NB_RING = [_pt(30, -7), _pt(44, -7), _pt(44, 7), _pt(30, 7), _pt(30, -7)]
GEOM = {"facade": {"width_ft": 66.0, "height_ft": 82.0}, "levels": [{"id": "L1", "name": "Level 1", "bot": 0, "top": 41}, {"id": "L2", "name": "Level 2", "bot": 41, "top": 82}], "datums": []}


class Fake:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if self.fail:
            raise RuntimeError("provider down")
        if url == b3.GEOSEARCH:
            return {"features": [{"geometry": {"coordinates": [LNG, LAT]}, "properties": {"label": "890 EAST 135 STREET, Bronx, NY, USA",
                                                                                          "addendum": {"pad": {"bin": BIN}}}}]}
        w = params.get("$where", "")
        if w.startswith("bin="):
            return [{"bin": BIN, "height_roof": "82.67", "ground_elevation": "8", "construction_year": "1920",
                     "the_geom": {"type": "MultiPolygon", "coordinates": [[RING]]}}] if BIN in w else []
        if w.startswith("within_circle"):
            return [{"bin": BIN, "height_roof": "82.67", "the_geom": {"type": "MultiPolygon", "coordinates": [[RING]]}},
                    {"bin": "2000001", "height_roof": "30", "the_geom": {"type": "MultiPolygon", "coordinates": [[NB_RING]]}}]
        if w.startswith("intersects"):
            return [{"bin": BIN, "height_roof": "82.67", "ground_elevation": "8", "the_geom": {"type": "MultiPolygon", "coordinates": [[RING]]}}]
        return []


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for role in ("c_suite", "client", "client2", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), "client" if role.startswith("client") else role, f"Smoke {role} B3d336"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'B3d336 %'")
        ids["orgA"] = crm.create_org(conn, name="B3d336 Owners A", relationship_type="client")
        ids["orgB"] = crm.create_org(conn, name="B3d336 Owners B", relationship_type="client")
        for c in (A, B):
            conn.execute("DELETE FROM projects WHERE project_code = ?", (c,))
        conn.execute("INSERT INTO projects (project_code, name, address, city_zip, status, client_org_id) VALUES (?, 'B3d336 Facade', "
                     "'890 E 135th St', 'Bronx, NY 10454', 'active', ?)", (A, ids["orgA"]))
        conn.execute("INSERT INTO projects (project_code, name, address, city_zip, status, client_org_id) VALUES (?, 'B3d336 Other', "
                     "'1 B3d336 Other St', 'Bronx, NY 10454', 'active', ?)", (B, ids["orgB"]))
        conn.execute("DELETE FROM building_geometry WHERE project_code IN (?, ?)", (A, B))
        conn.execute("DELETE FROM elevation WHERE project_code IN (?, ?)", (A, B))
        cur = conn.execute("INSERT INTO elevation (project_code, face, name, status, geometry_json) VALUES (?, 'N', 'B3d336 North', 'confirmed', ?)", (A, json.dumps(GEOM)))
        ids["eN"] = cur.lastrowid
        cur = conn.execute("INSERT INTO elevation (project_code, face, face_label, name, status, geometry_json) VALUES (?, 'E', 'East (courtyard)', 'B3d336 East', 'confirmed', ?)", (A, json.dumps({"facade": {"width_ft": 46.0, "height_ft": 82.0}, "levels": GEOM["levels"]})))
        ids["eE"] = cur.lastrowid
        cur = conn.execute("INSERT INTO elevation (project_code, face, name, status, geometry_json) VALUES (?, 'S', 'B3d336 South draft', 'draft', ?)", (A, json.dumps(GEOM)))
        ids["eS"] = cur.lastrowid
        cur = conn.execute("INSERT INTO elevation_drop (elevation_id, idx, grid_from, grid_to, x0, x1, width_ft) VALUES (?, 1, 'A', 'B', 0, 33, 33)", (ids["eN"],))
        d = cur.lastrowid
        conn.execute("INSERT INTO elevation_cell (drop_id, level_id, level_name, status_key) VALUES (?, 'L1', 'Level 1', 'in_progress')", (d,))
        conn.commit()
        # a client is bound to its ONE job through pm_project_assignment (client_portal.client_project_code)
        conn.execute("DELETE FROM pm_project_assignment WHERE project_code IN (?, ?)", (A, B))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, '2026-01-01')", (ids["client"], A))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, '2026-01-01')", (ids["client2"], B))
        # the client portal admits the drawing surface only with the 'drawing' section grant (#283)
        conn.execute("DELETE FROM client_section_grant WHERE project_code IN (?, ?)", (A, B))
        for uid, code in ((ids["client"], A), (ids["client2"], B)):
            conn.execute("INSERT INTO client_section_grant (user_id, project_code, section, granted_by, granted_at) VALUES (?, ?, 'drawing', ?, '2026-01-01T00:00:00')", (uid, code, ids["c_suite"]))
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = _db()
    try:
        for t in ("building_geometry", "pm_project_assignment", "client_section_grant"):
            try:
                conn.execute(f"DELETE FROM {t} WHERE project_code IN (?, ?)", (A, B))
            except Exception:
                conn.rollback()
        eids = [r["id"] for r in conn.execute("SELECT id FROM elevation WHERE project_code IN (?, ?)", (A, B)).fetchall()]
        dids = [r["id"] for r in conn.execute("SELECT id FROM elevation_drop WHERE elevation_id IN (" + (",".join("?" * len(eids)) or "NULL") + ")", eids).fetchall()] if eids else []
        purge(conn, "elevation_cell", "drop_id", dids)
        purge(conn, "elevation_drop", "id", dids)
        purge(conn, "elevation", "id", eids)
        conn.execute("DELETE FROM projects WHERE project_code IN (?, ?)", (A, B))
        conn.execute("DELETE FROM building WHERE address_line LIKE '%B3d336%' OR address_line LIKE '890 E 135th St%' AND client_org_id IN "
                     "(SELECT id FROM crm_organization WHERE name LIKE 'B3d336 %')")
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'B3d336 %'").fetchall()]
        for t, col in (("crm_activity", "entity_id"), ("crm_task", "entity_id"), ("crm_contact", "org_id")):
            purge(conn, t, col, org_ids)
        purge(conn, "crm_organization", "id", org_ids)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code)
    return c


def main() -> int:
    ids = _seed()
    passed = 0

    conn = _db()
    try:
        b3.ensure_schema(conn); b3.ensure_schema(conn)
    finally:
        conn.close()
    import apply_building_geometry_336 as mig
    assert mig.main() == 0
    passed += 1; print("  schema ok")

    # ---- maths
    x, y = b3.local_xy(LNG + 0.001, LAT + 0.001, LNG, LAT)
    assert 80 < x < 90 and 105 < y < 115, (x, y)
    f = b3.faces([[-10, -7], [10, -7], [10, 7], [-10, 7]])
    assert set(f["walls"]) == {"N", "E", "S", "W"} and f["walls"]["N"]["normal"] == [0.0, 1.0] and f["walls"]["S"]["normal"] == [0.0, -1.0]
    assert f["walls"]["E"]["length_m"] == 14.0 and f["walls"]["N"]["length_m"] == 20.0 and f["walls"]["W"]["bearing"] == 270.0
    f2 = b3.faces([[-10, 7], [10, 7], [10, -7], [-10, -7]])   # clockwise input is fixed up
    assert f2["walls"]["N"]["normal"] == [0.0, 1.0]
    assert b3.compass(10) == "N" and b3.compass(100) == "E" and b3.compass(190) == "S" and b3.compass(300) == "W" and b3.compass(359) == "N"
    assert b3._face_code("N", None) == "N" and b3._face_code("East (courtyard)", None) == "E" and b3._face_code(None, "South Elevation") == "S" and b3._face_code("NE", None) == "N" and b3._face_code("Front", None) is None
    passed += 1; print("  maths ok")

    # ---- resolve + cache
    fake = Fake()
    conn = _db()
    try:
        row = b3.resolve(conn, A, fetch=fake)
        assert row["bin"] == BIN and row["source"] == "nyc_open_data" and row["error"] is None and row["geocode_label"].startswith("890 EAST")
        assert abs(row["lat"] - LAT) < 1e-4 and abs(row["lng"] - LNG) < 1e-4 and row["height_ft"] == 82.67
        assert [c[0] for c in fake.calls] == [b3.GEOSEARCH, b3.FOOTPRINTS, b3.FOOTPRINTS]   # geocode, footprint by bin, neighbours
        nb = json.loads(row["neighbours_json"]); assert len(nb) == 1 and nb[0]["bin"] == "2000001", "the building itself is not its own neighbour"
        n0 = len(fake.calls)
        b3.resolve(conn, A, fetch=fake)
        assert len(fake.calls) == n0, "cached: no request"
        b3.resolve(conn, A, fetch=fake, refresh=True)
        assert len(fake.calls) == n0 + 3, "refresh re-fetches"
        stale = b3.resolve(conn, A, fetch=fake, now=datetime.now() + timedelta(days=31))
        assert len(fake.calls) == n0 + 6 and stale["fetched_at"] > row["fetched_at"], "stale after 30 days"
        # a linked building with a BIN skips geocoding
        bid = buildings.find_or_create(conn, "890 E 135th St", client_org_id=ids["orgA"], city_zip="Bronx, NY 10454")
        conn.execute("UPDATE building SET bin = ? WHERE id = ?", (BIN, bid)); conn.execute("UPDATE projects SET building_id = ? WHERE project_code = ?", (bid, A)); conn.commit()
        fake2 = Fake()
        b3.resolve(conn, A, fetch=fake2, refresh=True)
        assert [c[0] for c in fake2.calls] == [b3.FOOTPRINTS, b3.FOOTPRINTS]
        # provider failure: recorded, no footprint, retried later not now
        bad = Fake(fail=True)
        r = b3.resolve(conn, B, fetch=bad)
        assert r["footprint_json"] is None and "provider down" in r["error"]
        n1 = len(bad.calls); b3.resolve(conn, B, fetch=bad); assert len(bad.calls) == n1, "an error is not retried within the hour"
    finally:
        conn.close()
    passed += 1; print("  resolve + cache ok")

    # ---- the model, internal
    cs = _login("c_suite")
    m = cs.get(f"/api/elevation/model3d/{A}").get_json()["data"]
    g = m["geometry"]
    assert g["available"] and g["bin"] == BIN and abs(g["height_m"] - 82.67 * 0.3048) < 0.01 and g["source"] == "nyc_open_data"
    ring = g["rings"][0]
    assert all(abs(p[0]) < 12 and abs(p[1]) < 9 for p in ring), ring
    assert set(g["walls"]) == {"N", "E", "S", "W"} and 19 < g["walls"]["N"]["length_m"] < 21 and 13 < g["walls"]["E"]["length_m"] < 15
    assert len(g["neighbours"]) == 1 and 25 < g["neighbours"][0]["rings"][0][0][0] < 35 and abs(g["neighbours"][0]["height_m"] - 30 * 0.3048) < 0.01
    assert m["mapping"] == {"N": ids["eN"], "E": ids["eE"]}, m["mapping"]     # the draft South is not draped
    assert {e["id"]: e["compass"] for e in m["elevations"]} == {ids["eN"]: "N", ids["eE"]: "E", ids["eS"]: "S"}
    assert m["project"]["stories"] == 8 and m["fallback"]["height_m"] == round(82.0 * 0.3048, 2) and m["fallback"]["width_m"] == round(66.0 * 0.3048, 2) and m["fallback"]["depth_m"] == round(46.0 * 0.3048, 2)
    assert m["can_refresh"] and m["audience"] == "internal"
    # project B: no footprint -> fallback box from defaults, page still works
    mb = cs.get(f"/api/elevation/model3d/{B}").get_json()["data"]
    assert not mb["geometry"]["available"] and mb["fallback"]["width_m"] > 0 and mb["mapping"] == {}
    assert cs.post(f"/api/elevation/model3d/{A}/refresh").status_code == 200
    passed += 1; print("  model ok")

    # ---- gates + external audience
    assert server.app.test_client().get(f"/api/elevation/model3d/{A}").status_code == 401
    assert _login("pm").get(f"/api/elevation/model3d/{A}").status_code == 403, "unassigned pm"
    assert _login("client2").get(f"/api/elevation/model3d/{A}").status_code == 403, "another job's client"
    cl = _login("client")
    r = cl.get(f"/api/elevation/model3d/{A}")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    mc = r.get_json()["data"]
    assert mc["audience"] == "external" and not mc["can_refresh"] and ids["eS"] not in {e["id"] for e in mc["elevations"]}
    assert cl.post(f"/api/elevation/model3d/{A}/refresh").status_code == 403
    passed += 1; print("  gates ok")

    # ---- the page + the vendored library
    html = cs.get("/drawing-markup").get_data(as_text=True)
    for needle in ('id="view3d"', 'id="stage3d"', "__markup3d", "/files/static/js/building3d.js", "/api/elevation/model3d/"):
        assert needle in html, needle
    assert server.app.test_client().get("/files/static/vendor/three.module.min.js").status_code == 200
    assert server.app.test_client().get("/files/static/js/building3d.js").status_code == 200
    js = (ROOT / "static" / "js" / "building3d.js").read_text(encoding="utf-8")
    assert not any(h in js.lower() for h in ("cdnjs.", "jsdelivr", "unpkg.", "https://")) and "/files/static/vendor/three.module.min.js" in js
    passed += 1; print("  page ok")

    print(f"PASS smoke_building3d_336 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
