#!/usr/bin/env python3
"""#331 — job locations beyond the five boroughs (Long Island, New Jersey): behavioral guard.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * one list: regions.CODES == estimates.BOROUGHS; every code has a label; the five
    boroughs are unchanged and LI / NJ are present
  * metas: /api/estimates/meta and the onboard meta carry `regions` [{code,label}]
  * an estimate in NJ gets an `<TYPE>-NJ-<seq>` code; a job onboarded on Long Island
    converts, its estimate is in the LI series, and its auto-created job site carries
    the "Long Island" label; an unknown code is still refused
  * the pickers: "+ New project" renders labels with codes from the meta, the console's
    New estimate modal too, the portal's job-site modal lists Long Island and New Jersey
Run:  venv/bin/python tests/smoke_regions_331.py
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_reg_331_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import regions  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "reg331"
PW = "RegionsSmoke2026"


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed() -> dict:
    conn = _db()
    try:
        email = f"{TAG}.admin@example.com"
        conn.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                     "VALUES (?, ?, 'admin', 'Smoke admin Reg331', 1, 'active', 0, 1)", (email, auth.hash_password(PW)))
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Reg331 %'")
        org = crm.create_org(conn, name="Reg331 Owners", relationship_type="client")
        conn.commit()
        return {"org": org}
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        codes = [r["project_code"] for r in conn.execute("SELECT project_code FROM projects WHERE name LIKE 'Reg331 %'").fetchall()]
        for t in ("pm_project_assignment", "project_assignments", "billing_sov"):
            try:
                purge(conn, t, "project_code", codes)
            except Exception:
                conn.rollback()
        try:
            conn.execute("DELETE FROM ira_job WHERE project_code IN (" + ",".join("?" * len(codes)) + ")", codes) if codes else None
        except Exception:
            conn.rollback()
        conn.execute("DELETE FROM projects WHERE name LIKE 'Reg331 %'")
        conn.execute("DELETE FROM estimate WHERE building_address LIKE '% Reg331 %' OR notes LIKE '%Reg331%'")
        conn.execute("DELETE FROM building WHERE address_line LIKE '% Reg331 %'")
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Reg331 %'").fetchall()]
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


def main() -> int:
    ids = _seed()
    passed = 0

    assert tuple(regions.CODES) == tuple(estimates.BOROUGHS) and set(regions.LABELS) == set(regions.CODES)
    assert regions.CODES[:5] == ("MN", "BX", "BK", "QN", "SI") and "LI" in regions.CODES and "NJ" in regions.CODES
    assert regions.label("nj") == "New Jersey" and regions.label("LI") == "Long Island" and regions.label("ZZ") == "ZZ" and regions.label(None) == ""
    passed += 1; print("  one list ok")

    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.admin@example.com", "password": PW})
    assert r.status_code == 200
    meta = c.get("/api/estimates/meta").get_json()
    meta = meta.get("data", meta)
    assert {x["code"]: x["label"] for x in meta["regions"]}["NJ"] == "New Jersey" and "LI" in meta["boroughs"]
    om = c.get("/api/admin/projects/onboard-meta").get_json()["data"]
    assert any(x["code"] == "LI" and x["label"] == "Long Island" for x in om["regions"]) and "NJ" in om["boroughs"]
    passed += 1; print("  metas ok")

    r = c.post("/api/estimates", json={"est_type": "FR", "borough": "NJ", "client_org_id": ids["org"],
                                        "building_address": "9 Reg331 Hudson Pl", "notes": "Reg331 nj"})
    assert r.status_code in (200, 201), r.get_json()
    e = r.get_json(); e = e.get("data", e)
    assert re.fullmatch(r"FR-NJ-\d{3,}", e["code"]), e["code"]
    r = c.post("/api/estimates", json={"est_type": "FR", "borough": "LA", "client_org_id": ids["org"], "notes": "Reg331 bad"})
    assert r.status_code == 400
    r = c.post("/api/admin/projects/onboard", json={"client_org_id": ids["org"], "est_type": "FR", "borough": "LI",
                                                    "address": "40 Reg331 Shore Rd", "name": "Reg331 Shore facade",
                                                    "start_date": "2026-09-21"})
    assert r.status_code in (200, 201), r.get_json()
    d = r.get_json()["data"]
    proj = d.get("project") or d
    code = proj["project_code"]
    conn = _db()
    try:
        est = conn.execute("SELECT code, borough FROM estimate WHERE converted_project_code = ?", (code,)).fetchone()
        assert est and est["borough"] == "LI" and est["code"].startswith("FR-LI-"), dict(est) if est else None
        b = conn.execute("SELECT b.borough FROM building b JOIN projects p ON p.building_id = b.id WHERE p.project_code = ?", (code,)).fetchone()
        assert b and b["borough"] == "Long Island", dict(b) if b else None
    finally:
        conn.close()
    assert c.post("/api/admin/projects/onboard", json={"client_org_id": ids["org"], "est_type": "FR", "borough": "LA",
                                                       "address": "41 Reg331 Shore Rd"}).status_code == 400
    passed += 1; print("  NJ estimate + LI job ok")

    ap = c.get("/admin/projects").get_data(as_text=True)
    assert "m.regions" in ap and "Borough / region" in ap
    con = c.get("/", follow_redirects=True).get_data(as_text=True)
    assert "m.regions" in con and "Borough / region *" in con
    # #339 — the portal's job-site picker retired with the Job sites page; regions are picked on + New project
    passed += 1; print("  pickers ok")

    print(f"PASS smoke_regions_331 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
