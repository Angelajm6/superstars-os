#!/usr/bin/env python3
"""Projects table: the PM column lists PMs only and can be edited; the address lookup.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * the PM column (company portal Projects list) shows active users whose role is pm —
    not the job's clients / architects / supers (they share pm_project_assignment as
    their access scope), not deactivated PMs, not someone who stopped being a PM
  * GET /api/company/portal/projects/<code>/pms: assigned PMs, outdated rows (inactive
    user, or a role that no longer uses project scope), the PMs that can be added, and
    the other people with access as counts only
  * the served pages: no Open invoices column; the PM editor; the project form's address autofill + City / ZIP
  * PUT …/pms {user_ids}: adds and removes PMs and clears outdated rows not listed, on THIS job only;
    a client's / architect's / super's access is never touched; only active PMs can be
    assigned; gates (anon 401, pm / client 403, unknown job 404)
  * address lookup: GeoSearch features parse into street, borough code, city / ZIP,
    coordinates and BIN; a failing lookup or a short query gives []; the route is gated
Run:  venv/bin/python tests/smoke_project_pms.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_pms_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import buildings  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "pms401"
PW = "ProjectPmsSmoke2026"
A, B = "PMS401-A", "PMS401-B"
# key -> (role, active)
PEOPLE = {"c_suite": ("c_suite", 1), "pm1": ("pm", 1), "pm2": ("pm", 1), "pm_gone": ("pm", 0),
          "client": ("client", 1), "architect": ("architect", 1), "super": ("super", 1), "former": ("admin", 1)}


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for key, (role, active) in PEOPLE.items():
            email = f"{TAG}.{key}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, display_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)",
                         (email, auth.hash_password(PW), role, f"Smoke {key}", f"Pms401 {key}", active,
                          "active" if active else "disabled"))
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        for code in (A, B):
            conn.execute("DELETE FROM pm_project_assignment WHERE project_code = ?", (code,))
            conn.execute("DELETE FROM projects WHERE project_code = ?", (code,))
            conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, ?, '1 Pms401 St', 'active')",
                         (code, f"Pms401 {code}"))
        # job A: one real PM, a deactivated PM, a former PM (now admin), and three people with access
        for key in ("pm1", "pm_gone", "former", "client", "architect", "super"):
            conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, '2026-09-01')",
                         (ids[key], A))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, '2026-09-01')",
                     (ids["pm1"], B))
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = _db()
    try:
        purge(conn, "pm_project_assignment", "project_code", [A, B])
        purge(conn, "projects", "project_code", [A, B])
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(key):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@example.com", "password": PW})
    assert r.status_code == 200, (key, r.status_code, r.get_data(as_text=True)[:200])
    return c


def _rows_for(conn, code) -> set:
    return {r["user_id"] for r in conn.execute("SELECT user_id FROM pm_project_assignment WHERE project_code = ?",
                                               (code,)).fetchall()}


FEATURE = {"geometry": {"type": "Point", "coordinates": [-73.9123, 40.8091]},
           "properties": {"label": "890 EAST 135 STREET, Bronx, NY, USA", "housenumber": "890",
                          "street": "EAST 135 STREET", "borough": "Bronx", "postalcode": "10454",
                          "addendum": {"pad": {"bin": "2000001", "bbl": "2025000001"}}}}


def main() -> int:
    checks = 0
    ids = _seed()
    cs, pm, client = _login("c_suite"), _login("pm1"), _login("client")
    anon = server.app.test_client()

    # ---- the PM column lists PMs only
    rows = {r["project_code"]: r for r in cs.get("/api/company/portal/projects?status=all").get_json()["data"]}
    assert [p["name"] for p in rows[A]["pms"]] == ["Pms401 pm1"], rows[A]["pms"]
    assert rows[A]["pms"][0]["user_id"] == ids["pm1"]
    checks += 1

    # ---- what the editor sees
    url = f"/api/company/portal/projects/{A}/pms"
    assert anon.get(url).status_code == 401
    assert pm.get(url).status_code == 403 and client.get(url).status_code == 403
    assert cs.get("/api/company/portal/projects/NOPE-404/pms").status_code == 404
    d = cs.get(url).get_json()["data"]
    assigned = {u["user_id"]: u for u in d["assigned"]}
    assert set(assigned) == {ids["pm1"], ids["pm_gone"], ids["former"]}, assigned
    assert not assigned[ids["pm1"]]["outdated"] and assigned[ids["pm_gone"]]["outdated"] and assigned[ids["former"]]["outdated"]
    avail = {u["user_id"] for u in d["available"]}
    assert {ids["pm1"], ids["pm2"]} <= avail and ids["pm_gone"] not in avail and ids["client"] not in avail
    assert d["others"] == {"client": 1, "architect": 1, "super": 1}, d["others"]   # counts only, no names
    assert "email" not in str(d["others"])
    checks += 1

    # ---- set the job's PMs
    assert pm.put(url, json={"user_ids": [ids["pm2"]]}).status_code == 403
    assert cs.put(url, json={"user_ids": "pm2"}).status_code == 400
    assert cs.put(url, json={"user_ids": ["x"]}).status_code == 400
    assert cs.put(url, json={"user_ids": [ids["client"]]}).status_code == 400      # only PMs here
    assert cs.put(url, json={"user_ids": [ids["pm_gone"], 999999]}).status_code == 400   # nobody new but an active PM
    assert cs.put("/api/company/portal/projects/NOPE-404/pms", json={"user_ids": []}).status_code == 404
    conn = _db()
    try:
        assert _rows_for(conn, A) == {ids[k] for k in ("pm1", "pm_gone", "former", "client", "architect", "super")}
    finally:
        conn.close()   # nothing changed by the refused saves
    r = cs.put(url, json={"user_ids": [ids["pm1"], ids["pm_gone"]]})   # an outdated name listed = kept; the other removed
    assert r.status_code == 200, r.get_json()
    assert {u["user_id"] for u in r.get_json()["data"]["assigned"]} == {ids["pm1"], ids["pm_gone"]}
    assert r.get_json()["data"]["removed"] == [ids["former"]] and r.get_json()["data"]["added"] == []
    r = cs.put(url, json={"user_ids": [ids["pm2"]]})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()["data"]
    assert [u["user_id"] for u in d["assigned"]] == [ids["pm2"]]
    assert d["added"] == [ids["pm2"]] and sorted(d["removed"]) == sorted([ids["pm1"], ids["pm_gone"]])
    conn = _db()
    try:
        assert _rows_for(conn, A) == {ids["pm2"], ids["client"], ids["architect"], ids["super"]}   # access untouched
        assert _rows_for(conn, B) == {ids["pm1"]}                                                  # other jobs untouched
    finally:
        conn.close()
    r = cs.put(url, json={"user_ids": [ids["pm2"], ids["pm1"], ids["pm1"]]})   # add back; duplicates collapse
    assert r.status_code == 200 and {u["user_id"] for u in r.get_json()["data"]["assigned"]} == {ids["pm1"], ids["pm2"]}
    assert cs.put(url, json={"user_ids": []}).status_code == 200                   # no PM at all is allowed
    rows = {r["project_code"]: r for r in cs.get("/api/company/portal/projects?status=all").get_json()["data"]}
    assert rows[A]["pms"] == [] and [p["user_id"] for p in rows[B]["pms"]] == [ids["pm1"]]
    conn = _db()
    try:
        assert _rows_for(conn, A) == {ids["client"], ids["architect"], ids["super"]}
    finally:
        conn.close()
    checks += 1

    # ---- address lookup (GeoSearch stubbed: no network in CI)
    buildings._ADDR_CACHE.clear()
    got = buildings.address_suggest("890 east 135", fetch=lambda q: [FEATURE, {"properties": {}}, FEATURE])
    assert len(got) == 1, got   # empty features dropped, duplicates collapsed
    g = got[0]
    assert g["address_line"] == "890 East 135 Street" and g["borough_code"] == "BX" and g["city"] == "Bronx"
    assert g["zip"] == "10454" and g["city_zip"] == "Bronx, NY 10454" and g["bin"] == "2000001"
    assert (g["lat"], g["lng"]) == (40.8091, -73.9123) and g["label"] == "890 East 135 Street, Bronx, NY 10454"
    buildings._ADDR_CACHE.clear()

    def boom(q):
        raise TimeoutError("slow")
    assert buildings.address_suggest("890 east 135", fetch=boom) == []
    assert buildings.address_suggest("89", fetch=lambda q: [FEATURE]) == []
    real = buildings._geo_features
    buildings._geo_features = lambda q: [FEATURE]
    try:
        buildings._ADDR_CACHE.clear()
        assert anon.get("/api/address/suggest?q=890+east").status_code == 401
        assert client.get("/api/address/suggest?q=890+east").status_code == 403
        r = cs.get("/api/address/suggest?q=890+east")
        assert r.status_code == 200 and r.get_json()["data"][0]["borough_code"] == "BX"
        assert pm.get("/api/address/suggest?q=890+east").status_code == 200   # PMs fill addresses too
    finally:
        buildings._geo_features = real
        buildings._ADDR_CACHE.clear()
    checks += 1

    # ---- the served pages: no Open invoices column, the PM editor, the address autofill
    html = cs.get("/company").get_data(as_text=True)
    assert "label: 'Open invoices'" not in html, "the Projects table no longer shows open invoices"
    for needle in ('id="pj-pm"', 'id="pj-pmscrim"', "data-pj-pm=", "'/pms'", "user_ids: [...PM.pick, ...keep]",
                   "Edit PMs", "Their access is managed under Clients"):
        assert needle in html, needle
    js = cs.get("/files/static/js/new_project_modal.js").get_data(as_text=True)
    for needle in ("/api/address/suggest?q=", 'id="np-cityzip"', "city_zip: $('np-cityzip').value.trim() || null",
                   "city_zip: p.city_zip || ''", 'role="combobox"', "Filled from the address: ", "PM column on the Projects list"):
        assert needle in js, needle
    checks += 1

    print(f"PASS smoke_project_pms ({checks} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
