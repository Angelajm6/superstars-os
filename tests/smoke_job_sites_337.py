#!/usr/bin/env python3
"""#337 — job sites become projects in one click; the ledger records boot-ensured migrations.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * the Projects list carries `meta.sites_without_project` (job sites with no project);
    creating a project linked to the site removes it; archived sites are not offered
  * the served portal has the notice container, the "Create project here" button logic
    and the prefill link builder; the admin page opens "+ New project" from
    ?new=1&address=… (handler present, borough matched by label or code)
  * migrations: a pending script whose boot-ensured objects exist is recorded as applied
    with note "boot-ensured"; one whose objects are missing stays pending; the object
    probe handles both "table" and "table.column"
Run:  venv/bin/python tests/smoke_job_sites_337.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_js_337_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import buildings  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import migrations  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "js337"
PW = "JobSitesSmoke2026"


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        email = f"{TAG}.c_suite@example.com"
        conn.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                     "VALUES (?, ?, 'c_suite', 'Smoke c_suite Js337', 1, 'active', 0, 1)", (email, auth.hash_password(PW)))
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Js337 %'")
        ids["org"] = crm.create_org(conn, name="Js337 Owners", relationship_type="client")
        conn.execute("DELETE FROM building WHERE address_line LIKE '% Js337 %'")
        conn.execute("DELETE FROM projects WHERE project_code LIKE 'JS337-%'")
        ids["site"] = buildings.find_or_create(conn, "12 Js337 Way", client_org_id=ids["org"], city_zip="Bronx, NY 10454", borough="Bronx")
        ids["archived"] = buildings.find_or_create(conn, "13 Js337 Way", client_org_id=ids["org"], city_zip="Bronx, NY 10454")
        conn.execute("UPDATE building SET archived_at = '2026-01-01T00:00:00' WHERE id = ?", (ids["archived"],))
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = _db()
    try:
        conn.execute("DELETE FROM projects WHERE project_code LIKE 'JS337-%'")
        conn.execute("DELETE FROM building WHERE address_line LIKE '% Js337 %'")
        conn.execute("DELETE FROM schema_migrations WHERE name = 'js337_fake.py'")
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Js337 %'").fetchall()]
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

    c = server.app.test_client()
    assert c.post("/api/auth/login", json={"email": f"{TAG}.c_suite@example.com", "password": PW}).status_code == 200
    r = c.get("/api/company/portal/projects?status=all").get_json()
    sites = {s["id"]: s for s in r["meta"]["sites_without_project"]}
    assert ids["site"] in sites and ids["archived"] not in sites, sites.keys()
    assert sites[ids["site"]]["address_line"] == "12 Js337 Way" and sites[ids["site"]]["client_name"] == "Js337 Owners" and sites[ids["site"]]["borough"] == "Bronx"
    conn = _db()
    try:
        conn.execute("INSERT INTO projects (project_code, name, address, status, client_org_id, building_id) VALUES ('JS337-A', 'Js337 job', '12 Js337 Way', 'active', ?, ?)", (ids["org"], ids["site"]))
        conn.commit()
    finally:
        conn.close()
    r = c.get("/api/company/portal/projects?status=all").get_json()
    assert ids["site"] not in {s["id"] for s in r["meta"]["sites_without_project"]}, "a site with a project is no longer offered"
    passed += 1; print("  sites without project ok")

    html = c.get("/company").get_data(as_text=True)
    for needle in ('id="pj-notice"', "Create project here", "function newProjectHref", "sites_without_project", "← All job sites"):
        assert needle in html, needle
    ap = c.get("/admin/projects").get_data(as_text=True)
    for needle in ("openNewProjectWith", "q.get('new') === '1'", "np-borough"):
        assert needle in ap, needle
    passed += 1; print("  pages ok")

    # ---- ledger reconciliation
    conn = _db()
    try:
        assert migrations._object_exists(conn, "projects") and migrations._object_exists(conn, "projects.target_completion")
        assert not migrations._object_exists(conn, "no_such_table_js337") and not migrations._object_exists(conn, "projects.no_such_col")
        migrations.ensure_ledger(conn)
        conn.execute("DELETE FROM schema_migrations WHERE name IN ('apply_payables_319.py', 'apply_building_geometry_336.py')")
        conn.commit()
        st = migrations.status(conn)
        assert {"apply_payables_319.py", "apply_building_geometry_336.py"} <= set(st["pending"])
        migrations.BOOT_ENSURED["js337_fake.py"] = ("no_such_table_js337",)
        try:
            done = migrations.reconcile_boot_ensured(conn, st["pending"] + ["js337_fake.py"])
        finally:
            migrations.BOOT_ENSURED.pop("js337_fake.py", None)
        assert set(done) == {"apply_payables_319.py", "apply_building_geometry_336.py"}, done
        rows = {r["name"]: r["note"] for r in conn.execute("SELECT name, note FROM schema_migrations WHERE name IN ('apply_payables_319.py', 'apply_building_geometry_336.py')").fetchall()}
        assert rows == {"apply_payables_319.py": "boot-ensured", "apply_building_geometry_336.py": "boot-ensured"}
        assert "js337_fake.py" not in {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        assert not [n for n in migrations.status(conn)["pending"] if n in rows]
        out = migrations.boot(auto=False)
        assert out["error"] is None and out["pending"] == []
    finally:
        conn.close()
    passed += 1; print("  ledger reconcile ok")

    print(f"PASS smoke_job_sites_337 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
