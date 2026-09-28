#!/usr/bin/env python3
"""#352 — Drop Plan: column repair (L x W x D) per drop. Behavioral guard.

Boots the real app on an ISOLATED database. Proves:
  * a column repair is logged with kind="column_repair": the project's COL-REPAIR SOV line
    is created on first use (once), the volume is computed like a patch
  * it needs all three dimensions (a bare quantity is refused)
  * patches and column repairs are totalled APART: drop list/detail volume_to_date stays
    patch-only, column_repair_to_date is the new figure; the project rollup lists both lines
  * the patch path is unchanged; the entry list carries sov_code so the UI can split them
  * the served dashboard carries the Concrete patch / Column repair switch and the report section
Run:  venv/bin/python tests/smoke_column_repair_352.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_colrep_352_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "colrep352"
PW = "ColumnRepair2026"
P = "CR352-P"
D1, D2 = "CR352-D1", "CR352-D2"


def _db():
    return db_layer.connect(pragma_fk=False)


def _purge(conn):
    purge(conn, "quantity_entries", "drop_id", [D1, D2])
    purge(conn, "drops", "drop_id", [D1, D2])
    purge(conn, "sov_line_items", "project_code", [P])
    purge(conn, "pm_project_assignment", "project_code", [P])
    purge(conn, "projects", "project_code", [P])


def _seed():
    conn = _db()
    try:
        _purge(conn)
        conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, 'Colrep352', '1 Col St', 'active')", (P,))
        for i, d in enumerate((D1, D2), start=1):
            conn.execute("INSERT INTO drops (drop_id, project_code, elevation, sequence_no) VALUES (?, ?, 'North', ?)", (d, P, i))
        conn.execute("INSERT INTO sov_line_items (project_code, sov_code, description, unit) VALUES (?, 'CONC-PATCH', 'Concrete patch', 'CF')", (P,))
        for role in ("pm", "admin"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Smoke {role} Colrep"))
        pm_id = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.pm@example.com",)).fetchone()["id"]
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, '2026-09-28')", (pm_id, P))
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        _purge(conn)
        conn.commit()
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
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


def _sov(conn=None):
    conn = _db()
    try:
        return {r["sov_code"]: r["sov_id"] for r in conn.execute(
            "SELECT sov_code, sov_id FROM sov_line_items WHERE project_code = ?", (P,)).fetchall()}
    finally:
        conn.close()


def main() -> int:
    _seed()
    passed = 0
    pm = _login("pm")
    q = lambda d: f"/api/dropplan/drops/{d}/quantity-entries"   # noqa: E731

    # ---- a column repair: needs L x W x D; the line is created on first use, once
    r = pm.post(q(D1), json={"kind": "column_repair", "quantity": 3})
    assert r.status_code == 400 and "length" in r.get_json()["error"], r.get_json()
    assert "COL-REPAIR" not in _sov(), "a refused entry must not create the line"
    r = pm.post(q(D1), json={"kind": "column_repair", "length": 12, "width": 12, "depth": 6,
                             "length_unit": "in", "width_unit": "in", "depth_unit": "in"})
    assert r.status_code == 201, r.get_json()
    e1 = r.get_json()["data"]
    assert abs(e1["volume_cf"] - 0.5) < 1e-9, e1        # 1 ft x 1 ft x 0.5 ft
    r = pm.post(q(D1), json={"kind": "column_repair", "length": 2, "width": 1, "depth": 1})
    assert r.status_code == 201
    sov = _sov()
    assert "COL-REPAIR" in sov and e1["sov_line_item"] == sov["COL-REPAIR"]
    conn = _db()
    try:
        n = conn.execute("SELECT COUNT(*) FROM sov_line_items WHERE project_code=? AND sov_code='COL-REPAIR'", (P,)).fetchone()[0]
        row = conn.execute("SELECT description, unit FROM sov_line_items WHERE project_code=? AND sov_code='COL-REPAIR'", (P,)).fetchone()
    finally:
        conn.close()
    assert n == 1 and row["description"] == "Column repair" and row["unit"] == "CF"
    passed += 1; print("  column repair logged ok")

    # ---- the patch path is unchanged
    r = pm.post(q(D1), json={"sov_line_item": sov["CONC-PATCH"], "length": 3, "width": 1, "depth": 1})
    assert r.status_code == 201, r.get_json()
    r = pm.post(q(D2), json={"sov_line_item": sov["CONC-PATCH"], "length": 1, "width": 1, "depth": 1})
    assert r.status_code == 201
    assert pm.post(q(D1), json={"length": 1, "width": 1, "depth": 1}).status_code == 400, "sov_line_item still required for a patch"
    passed += 1; print("  patch path unchanged ok")

    # ---- totals apart: list, detail, project rollup
    _r = pm.get(f"/api/dropplan/projects/{P}/drops"); assert _r.status_code == 200, (_r.status_code, _r.get_json())
    drops = {d["drop_id"]: d for d in _r.get_json()["data"]}
    assert abs(drops[D1]["volume_to_date"] - 3.0) < 1e-9, drops[D1]            # patches only
    assert abs(drops[D1]["column_repair_to_date"] - 2.5) < 1e-9, drops[D1]
    assert drops[D2]["column_repair_to_date"] == 0.0 and abs(drops[D2]["volume_to_date"] - 1.0) < 1e-9
    det = pm.get(f"/api/dropplan/drops/{D1}").get_json()["data"]
    assert abs(det["column_repair_to_date"] - 2.5) < 1e-9 and abs(det["volume_to_date"] - 3.0) < 1e-9
    roll = pm.get(f"/api/dropplan/projects/{P}/rollup").get_json()["data"]
    lines = {x["sov_code"]: x for x in roll["quantity_by_sov"]}
    assert set(lines) >= {"CONC-PATCH", "COL-REPAIR"}, roll.keys()
    entries = pm.get(q(D1)).get_json()["data"]
    assert sorted(e["sov_code"] for e in entries) == ["COL-REPAIR", "COL-REPAIR", "CONC-PATCH"], entries
    passed += 1; print("  totals kept apart ok")

    # ---- the served dashboard carries the switch and the report section
    html = _login("admin").get(f"/projects/{P}").get_data(as_text=True)
    for needle in ('data-dpkind="patch"', 'data-dpkind="column"', "kind='column_repair'", "Column repairs logged",
                   "column_repair_to_date", "COL-REPAIR"):
        assert needle in html, needle
    passed += 1; print("  served page ok")

    print(f"PASS smoke_column_repair_352 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_column_repair_352: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
