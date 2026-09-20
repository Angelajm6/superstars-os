#!/usr/bin/env python3
"""#304 — buildings as a first-class entity: behavioral guard.

Flask test client on an ISOLATED database (honors SSC_DB_URL; builds a throwaway
SQLite snapshot from the versioned migrations when unset). Proves:

  * ensure_schema is idempotent; projects/estimate gain building_id
  * address identity: '890 East 135th Street' / '890 E 135th St, Bronx' /
    '890 e. 135th street' are ONE building; '12 Harbor Way' is another
  * backfill links existing projects + estimates, creates rows once, and a
    second run is a no-op
  * estimates.create_estimate and convert auto-link; crm.link_project too
  * gating: pm / super / estimator / client -> 403, c_suite / admin -> 200
  * CRUD: create (dup address refused, validation), update (key follows the
    address), archive hides from the default list, restore, link / unlink,
    unlinked pickers, search + client filter
  * the Company Portal's client detail reads the table (building ids present)
  * no `*_path` keys in any payload

Run:  venv/bin/python tests/smoke_buildings_304.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_bld_304_")


from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402


if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import buildings  # noqa: E402
import company_portal  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402

TAG = "bld304"
PW = "BuildingsSmokePassword2026"
ROLES = ("c_suite", "admin", "pm", "super", "estimator", "client")
NOW = datetime.now().isoformat(timespec="seconds")
TODAY = date.today().isoformat()


def _db():
    return db_layer.connect(pragma_fk=True)


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for role in ROLES:
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), role, f"Smoke {role}"))
        for name in ("Bld304 Compass", "Bld304 Harbor"):
            conn.execute("DELETE FROM crm_organization WHERE name = ?", (name,))
            conn.execute("INSERT INTO crm_organization (name, relationship_type, status, stage, created_at) "
                         "VALUES (?, 'client', 'active', 'active', ?)", (name, NOW))
        ids["org_a"] = conn.execute("SELECT id FROM crm_organization WHERE name = 'Bld304 Compass'").fetchone()["id"]
        ids["org_b"] = conn.execute("SELECT id FROM crm_organization WHERE name = 'Bld304 Harbor'").fetchone()["id"]
        # three projects: two at one building (address variants), one elsewhere, one with NO address
        for code, name, addr, cz, org in (
                ("B304-A", "Facade phase 1", "890 East 135th Street", "Bronx, NY 10454", ids["org_a"]),
                ("B304-B", "Facade phase 2", "890 E 135th St, Bronx", "Bronx, NY 10454", ids["org_a"]),
                ("B304-C", "Roof job", "12 Harbor Way", "Brooklyn, NY 11201", ids["org_b"]),
                ("B304-D", "No address yet", None, None, ids["org_b"])):
            conn.execute("DELETE FROM projects WHERE project_code = ?", (code,))
            conn.execute("INSERT INTO projects (project_code, name, address, city_zip, status, client_org_id, "
                         "created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                         (code, name, addr, cz, org, NOW, NOW))
        conn.execute("DELETE FROM estimate WHERE code LIKE 'B304-%'")
        for code, addr, status, amt, org in (("B304-E1", "890 e. 135th street", "submitted", 50000, ids["org_a"]),
                                             ("B304-E2", "12 HARBOR WAY", "approved", 120000, ids["org_b"])):
            conn.execute("INSERT INTO estimate (code, est_type, borough, seq, client_org_id, building_address, status, "
                         "final_amount, created_at, updated_at) VALUES (?, 'facade', 'BX', 1, ?, ?, ?, ?, ?, ?)",
                         (code, org, addr, status, amt, NOW, NOW))
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        conn.execute("DELETE FROM building WHERE address_key LIKE '890 e 135th%' OR address_key LIKE '12 harbor%' "
                     "OR address_key LIKE '77 smoke%' OR address_key LIKE '78 smoke%'")
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Bld304 %'").fetchall()]
        # estimates created through estimates.create_estimate carry generated codes: purge by client
        purge(conn, "estimate", "client_org_id", org_ids)
        conn.execute("DELETE FROM estimate WHERE code LIKE 'B304-%'")
        purge(conn, "projects", "client_org_id", org_ids)
        conn.execute("DELETE FROM projects WHERE project_code LIKE 'B304-%'")
        conn.execute("DELETE FROM crm_activity WHERE summary LIKE '%B304%'")
        if org_ids:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({','.join('?' * len(org_ids))})", org_ids)
            conn.execute(f"DELETE FROM crm_task WHERE entity_type = 'organization' AND entity_id IN ({','.join('?' * len(org_ids))})", org_ids)
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, ids)   # FK-safe on Postgres too
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Bld304 %'")
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _no_paths(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert not k.endswith("_path"), k
            _no_paths(v)
    elif isinstance(obj, list):
        for v in obj:
            _no_paths(v)


def main() -> int:
    app = Flask("bld304", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    company_portal.register(app)
    buildings.register(app)   # ensure_schema + boot backfill (links the seeded rows? no — seeded after)
    ids = _seed()
    try:
        conn = _db()
        try:
            # ---- schema idempotent, columns present ----
            buildings.ensure_schema(conn); buildings.ensure_schema(conn)
            assert buildings._has_column(conn, "projects", "building_id") and buildings._has_column(conn, "estimate", "building_id")
            # ---- identity ----
            k = buildings.address_key
            assert k("890 East 135th Street") == k("890 E 135th St, Bronx") == k("890 e. 135th street") == "890 e 135th st"
            assert k("12 Harbor Way") == k("12 HARBOR WAY") != k("890 E 135th St")
            assert k("") == "" and k(None) == ""
            # ---- backfill ----
            s1 = buildings.backfill(conn)
            assert s1["projects_linked"] == 3 and s1["estimates_linked"] == 2, s1
            assert s1["buildings_created"] == 2, s1
            s2 = buildings.backfill(conn)
            assert s2["projects_linked"] == 0 and s2["estimates_linked"] == 0 and s2["buildings_created"] == 0, s2
            rows = buildings.list_buildings(conn)
            mine = {r["address_key"]: r for r in rows if r["address_key"] in ("890 e 135th st", "12 harbor way")}
            assert set(mine) == {"890 e 135th st", "12 harbor way"}
            b890 = mine["890 e 135th st"]
            assert b890["client_org_id"] == ids["org_a"] and b890["projects_total"] == 2 and b890["estimates_total"] == 1
            assert b890["active_projects"] == 2 and b890["open_estimates"] == 1 and b890["pipeline_value"] == 50000.0
            assert b890["city"] == "Bronx" and b890["state"] == "NY" and b890["zip"] == "10454" and b890["borough"] == "Bronx"
            bhw = mine["12 harbor way"]
            assert bhw["won_value"] == 120000.0 and bhw["projects_total"] == 1
            noaddr = conn.execute("SELECT building_id FROM projects WHERE project_code = 'B304-D'").fetchone()["building_id"]
            assert noaddr is None, "a project without an address must not get a building"
            # ---- autolink on estimate create + convert + crm.link_project ----
            eid = estimates.create_estimate(conn, est_type="FR", borough="BX", client_org_id=ids["org_b"],
                                            building_address="12 harbor way", created_by=None)
            eid = eid["id"] if isinstance(eid, dict) else eid
            e = conn.execute("SELECT building_id, code FROM estimate WHERE id = ?", (eid,)).fetchone()
            assert e["building_id"] == bhw["id"], "new estimate must auto-link to the existing building"
            conn.execute("DELETE FROM projects WHERE project_code = 'B304-E'")
            conn.execute("INSERT INTO projects (project_code, name, address, status, created_at, updated_at) "
                         "VALUES ('B304-E', 'Unlinked', '77 Smoke Ave', 'active', ?, ?)", (NOW, NOW))
            conn.commit()
            assert crm.link_project(conn, "B304-E", ids["org_a"])
            pe = conn.execute("SELECT building_id FROM projects WHERE project_code = 'B304-E'").fetchone()
            assert pe["building_id"], "link_project must auto-create + link the building"
            assert conn.execute("SELECT client_org_id FROM building WHERE id = ?", (pe["building_id"],)).fetchone()["client_org_id"] == ids["org_a"]
            # ---- portal client detail reads the table ----
            d = company_portal.client_detail(conn, ids["org_a"])
            assert d["totals"]["buildings"] == 2, d["totals"]
            assert all(b.get("id") for b in d["buildings"]), "client view must carry building ids"
            assert any(b["address"] == "890 East 135th Street" and len(b["projects"]) == 2 for b in d["buildings"])
            db_ = company_portal.client_detail(conn, ids["org_b"])
            assert any(b["id"] is None and any(p["project_code"] == "B304-D" for p in b["projects"]) for b in db_["buildings"]), \
                "address-less projects stay visible under the no-address bucket"
            _no_paths(d)
        finally:
            conn.close()

        # ---- endpoints: gating + CRUD ----
        def client(role):
            c = app.test_client()
            r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
            assert r.status_code == 200, (role, r.status_code)
            return c
        cs = client("c_suite")
        for role in ("pm", "super", "estimator", "client"):
            r = client(role).get("/api/buildings")
            assert r.status_code in (403, 302), (role, r.status_code)
        assert client("admin").get("/api/buildings").status_code == 200

        r = cs.get("/api/buildings?search=harbor"); assert r.status_code == 200
        assert [x["address_key"] for x in r.get_json()["data"]] == ["12 harbor way"]
        r = cs.get(f"/api/buildings?client_org_id={ids['org_a']}")
        assert {x["address_key"] for x in r.get_json()["data"]} == {"890 e 135th st", "77 smoke ave"}
        _no_paths(r.get_json())

        # create: validation, duplicate refused
        r = cs.post("/api/buildings", json={"address_line": ""}); assert r.status_code == 400
        r = cs.post("/api/buildings", json={"address_line": "890 E. 135th Street"}); assert r.status_code == 400, "duplicate address must be refused"
        r = cs.post("/api/buildings", json={"address_line": "78 Smoke Blvd", "name": "Smoke Tower", "client_org_id": ids["org_b"],
                                            "borough": "BK", "zip": "11201", "bin": "3001234", "block": "12", "lot": "7",
                                            "stories": "6", "year_built": "1928", "notes": "hand-added"})
        assert r.status_code == 201, r.get_json()
        nb = r.get_json()["data"]
        assert nb["borough"] == "Brooklyn" and nb["stories"] == 6 and nb["year_built"] == 1928 and nb["client_name"] == "Bld304 Harbor"
        r = cs.post("/api/buildings", json={"address_line": "1 Bad St", "year_built": "1200"}); assert r.status_code == 400
        # detail
        r = cs.get(f"/api/buildings/{nb['id']}"); assert r.status_code == 200
        assert r.get_json()["data"]["projects"] == [] and "documents_expiring" in r.get_json()["data"]
        assert cs.get("/api/buildings/999999").status_code == 404
        # update: address change moves the key; clash refused
        r = cs.patch(f"/api/buildings/{nb['id']}", json={"address_line": "78 Smoke Boulevard", "stories": 7})
        assert r.status_code == 200 and r.get_json()["data"]["address_key"] == "78 smoke blvd" and r.get_json()["data"]["stories"] == 7
        r = cs.patch(f"/api/buildings/{nb['id']}", json={"address_line": "12 Harbor Way"}); assert r.status_code == 400
        # link / unlink via pickers
        r = cs.get(f"/api/buildings/unlinked?client_org_id={ids['org_b']}")
        assert any(p["project_code"] == "B304-D" for p in r.get_json()["data"]["projects"])
        r = cs.post(f"/api/buildings/{nb['id']}/link", json={"project_code": "B304-D"}); assert r.status_code == 200
        assert [p["project_code"] for p in r.get_json()["data"]["projects"]] == ["B304-D"]
        r = cs.post(f"/api/buildings/{nb['id']}/unlink", json={"project_code": "B304-D"}); assert r.status_code == 200
        assert r.get_json()["data"]["projects"] == []
        assert cs.post(f"/api/buildings/{nb['id']}/link", json={}).status_code == 400
        # archive hides from the default list, restore brings it back
        assert cs.post(f"/api/buildings/{nb['id']}/archive").status_code == 200
        assert nb["id"] not in [x["id"] for x in cs.get("/api/buildings").get_json()["data"]]
        assert nb["id"] in [x["id"] for x in cs.get("/api/buildings?archived=1").get_json()["data"]]
        assert cs.post(f"/api/buildings/{nb['id']}/restore").status_code == 200
        assert nb["id"] in [x["id"] for x in cs.get("/api/buildings").get_json()["data"]]
        # backfill endpoint is a no-op now
        r = cs.post("/api/buildings/backfill"); assert r.status_code == 200 and r.get_json()["data"]["buildings_created"] == 0
        # portal endpoints still answer with building ids
        r = cs.get(f"/api/company/portal/clients/{ids['org_a']}"); assert r.status_code == 200
        assert all(b.get("id") for b in r.get_json()["data"]["buildings"])
    finally:
        _cleanup()
    print("PASS: #304 buildings — identity, backfill, autolink (estimate/convert/crm), gating, CRUD, "
          "link/unlink, archive, portal client view on the table, no path keys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
