#!/usr/bin/env python3
"""#318 — company vault: behavioral guard.

Flask test client on an ISOLATED database (honors SSC_DB_URL; builds a throwaway
SQLite snapshot from the versioned migrations when unset) with a scratch data
root for the uploads. Proves:

  * ensure_schema is idempotent (vehicle, company_document_file, the two columns)
  * gating: pm / client -> 403 on vehicles + files; anon -> 401; admin / c_suite -> 200
  * vehicles: validation (label, year, status, vin, driver must be an active
    worker), plate normalised, driver rendered "W-#### — Name", edit, retire
    (single-shot), archived=1 lists it, retired vehicles are not flagged
  * vehicle paper: vault documents with vehicle_id per category; `documents` /
    `missing` / `next_expiry_days` on the vehicle; newest expiry wins; unknown
    vehicle -> 400
  * tax categories carry doc_year (validated); group chips: meta.groups,
    meta.category_groups, ?group= and ?vehicle_id= filters; `group` on rows
  * files: unsupported extension -> 400, empty file -> 400, PDF -> 201 with a
    gated file_url and NO file_path anywhere; the file is served inline with
    no-store; listing shows file_count / files; delete removes the row AND the
    disk file; upload on an archived document -> 404; bogus ids -> 404
  * compliance rollup: a vehicle document expiring soon appears as kind
    'vehicle' with the vehicle label as subject; the old company_document kind
    still works for non-vehicle paper
  * the #302 payloads keep their shape (no `*_path` keys)

Run:  venv/bin/python tests/smoke_company_vault_318.py
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_vault_318_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import company_portal  # noqa: E402
import company_vault  # noqa: E402
import db_layer  # noqa: E402

TAG = "vault318"
PW = "VaultSmokePassword2026"
ROLES = ("c_suite", "admin", "pm", "client")
TODAY = date.today()
NOW = datetime.now().isoformat(timespec="seconds")
API = "/api/company/portal"


def D(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def _db():
    return db_layer.connect(pragma_fk=True)


def _seed():
    conn = _db()
    try:
        for role in ROLES:
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), role, f"Smoke {role}"))
        conn.execute("DELETE FROM employees WHERE employee_id IN ('E-93181','E-93182')")
        conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at) "
                     "VALUES ('E-93181', 'W-9318', 'Vault Driver', 'mason', ?, ?)", (NOW, NOW))
        conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at, archived_at) "
                     "VALUES ('E-93182', 'W-9319', 'Vault Former', 'mason', ?, ?, ?)", (NOW, NOW, NOW))
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        doc_ids = [r["id"] for r in conn.execute("SELECT id FROM company_document WHERE title LIKE 'Vault318 %'").fetchall()]
        purge(conn, "company_document_file", "document_id", doc_ids)
        purge(conn, "company_document", "id", doc_ids)
        conn.execute("DELETE FROM vehicle WHERE label LIKE 'Vault318 %'")
        conn.execute("DELETE FROM employees WHERE employee_id IN ('E-93181','E-93182')")
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _no_paths(obj, where=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert not (k.endswith("_path") or k == "folder"), f"path key {k!r} leaked in {where}"
            _no_paths(v, where)
    elif isinstance(obj, list):
        for v in obj:
            _no_paths(v, where)


def _login(app, role):
    c = app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code, r.get_data(as_text=True)[:200])
    return c


def _j(resp, where=""):
    j = resp.get_json()
    _no_paths(j, where)
    return j


def main() -> int:
    app = Flask("vault318", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    company_portal.register(app)
    conn = _db()
    try:
        company_vault.ensure_schema(conn)   # idempotent
        company_vault.ensure_schema(conn)
        assert company_vault._has_column(conn, "company_document", "vehicle_id")
        assert company_vault._has_column(conn, "company_document", "doc_year")
    finally:
        conn.close()
    _seed()
    checks = 0

    # ---- gates
    for role in ("pm", "client"):
        c = _login(app, role)
        assert c.get(f"{API}/vehicles").status_code == 403, role
        assert c.post(f"{API}/vehicles", json={"label": "x"}).status_code == 403
        assert c.get(f"{API}/company-documents/files/1").status_code == 403
    assert app.test_client().get(f"{API}/vehicles").status_code == 401
    cs = _login(app, "c_suite")
    admin = _login(app, "admin")
    assert admin.get(f"{API}/vehicles").status_code == 200
    checks += 1

    # ---- vehicles: validation + create
    assert cs.post(f"{API}/vehicles", json={"label": "  "}).status_code == 400
    assert cs.post(f"{API}/vehicles", json={"label": "Vault318 X", "year": "abc"}).status_code == 400
    assert cs.post(f"{API}/vehicles", json={"label": "Vault318 X", "status": "sold"}).status_code == 400
    assert cs.post(f"{API}/vehicles", json={"label": "Vault318 X", "vin": "123"}).status_code == 400
    r = cs.post(f"{API}/vehicles", json={"label": "Vault318 X", "driver_employee_id": "E-93182"})
    assert r.status_code == 400 and "active worker" in r.get_json()["error"], "archived worker cannot drive"
    r = cs.post(f"{API}/vehicles", json={"label": "Vault318 Van 2", "year": 2021, "make": "Ford", "model": "Transit 250",
                                         "plate": "abc 1234", "vin": "1ftbr1c85mka12345", "color": "White",
                                         "driver_employee_id": "E-93181"})
    assert r.status_code == 201, r.get_json()
    v = _j(r, "vehicle create")["data"]
    assert v["plate"] == "ABC1234" and v["vin"] == "1FTBR1C85MKA12345" and v["status"] == "active"
    assert v["driver"] == "W-9318 — Vault Driver", v["driver"]
    r = cs.post(f"{API}/vehicles", json={"label": "Vault318 Pickup", "status": "retired"})
    v2 = _j(r)["data"]
    lst = _j(cs.get(f"{API}/vehicles"), "vehicles list")
    mine = {x["id"]: x for x in lst["data"] if x["label"].startswith("Vault318 ")}
    assert set(mine) == {v["id"], v2["id"]}
    assert sorted(mine[v["id"]]["missing"]) == ["Vehicle Inspection", "Vehicle Insurance", "Vehicle Registration"]
    assert mine[v2["id"]]["missing"] == [], "retired vehicles are not flagged"
    assert any(d["employee_id"] == "E-93181" and d["label"] == "W-9318 — Vault Driver" for d in lst["meta"]["drivers"])
    assert not any(d["employee_id"] == "E-93182" for d in lst["meta"]["drivers"]), "archived worker not offered as driver"
    # edit
    r = cs.patch(f"{API}/vehicles/{v['id']}", json={"color": "Blue", "driver_employee_id": None})
    assert r.status_code == 200 and _j(r)["data"]["color"] == "Blue" and _j(r)["data"]["driver"] is None
    assert cs.patch(f"{API}/vehicles/{v['id']}", json={}).status_code == 400
    assert cs.patch(f"{API}/vehicles/999999", json={"color": "x"}).status_code == 404
    checks += 1

    # ---- vehicle paper on the vault
    assert cs.post(f"{API}/company-documents", json={"category": "vehicle_registration", "title": "Vault318 reg",
                                                     "vehicle_id": 999999}).status_code == 400
    assert cs.post(f"{API}/company-documents", json={"category": "vehicle_registration", "title": "Vault318 reg",
                                                     "vehicle_id": "abc"}).status_code == 400
    r = cs.post(f"{API}/company-documents", json={"category": "vehicle_registration", "title": "Vault318 registration 2025",
                                                  "vehicle_id": v["id"], "doc_number": "REG-1", "expiry_date": D(-20)})
    assert r.status_code == 201, r.get_json()
    reg_old = r.get_json()["data"]["id"]
    r = cs.post(f"{API}/company-documents", json={"category": "vehicle_registration", "title": "Vault318 registration 2026",
                                                  "vehicle_id": v["id"], "doc_number": "REG-2", "expiry_date": D(200)})
    reg_new = r.get_json()["data"]["id"]
    r = cs.post(f"{API}/company-documents", json={"category": "vehicle_insurance", "title": "Vault318 auto policy",
                                                  "vehicle_id": v["id"], "issuer": "Carrier", "expiry_date": D(12), "amount": 1000000})
    ins = r.get_json()["data"]["id"]
    mine = {x["id"]: x for x in _j(cs.get(f"{API}/vehicles"))["data"]}[v["id"]]
    assert mine["missing"] == ["Vehicle Inspection"], mine["missing"]
    assert mine["documents"]["vehicle_registration"]["id"] == reg_new, "newest expiry wins"
    assert mine["documents"]["vehicle_registration"]["days_left"] == 200
    assert mine["documents"]["vehicle_insurance"]["days_left"] == 12 and mine["next_expiry_days"] == 12
    assert mine["doc_count"] == 2
    docs = _j(cs.get(f"{API}/company-documents"), "documents")
    assert docs["meta"]["groups"]["vehicles"] == "Vehicles" and docs["meta"]["category_groups"]["vehicle_insurance"] == "vehicles"
    assert docs["meta"]["category_groups"]["tax_return_federal"] == "tax" and docs["meta"]["category_groups"]["insurance_gl"] == "insurance"
    row = [x for x in docs["data"] if x["id"] == ins][0]
    assert row["vehicle_label"] == "Vault318 Van 2" and row["group"] == "vehicles" and row["file_count"] == 0 and row["files"] == []
    ids_v = {x["id"] for x in _j(cs.get(f"{API}/company-documents?vehicle_id={v['id']}"))["data"]}
    assert ids_v == {reg_old, reg_new, ins}
    ids_g = {x["id"] for x in _j(cs.get(f"{API}/company-documents?group=vehicles"))["data"]}
    assert ids_g >= ids_v and all(x["group"] == "vehicles" for x in _j(cs.get(f"{API}/company-documents?group=vehicles"))["data"])
    checks += 1

    # ---- tax paper with a year
    assert cs.post(f"{API}/company-documents", json={"category": "tax_return_federal", "title": "Vault318 1120S",
                                                     "doc_year": 1234}).status_code == 400
    assert cs.post(f"{API}/company-documents", json={"category": "tax_return_federal", "title": "Vault318 1120S",
                                                     "doc_year": "twenty"}).status_code == 400
    r = cs.post(f"{API}/company-documents", json={"category": "tax_return_federal", "title": "Vault318 1120S 2025",
                                                  "doc_year": 2025, "issuer": "IRS"})
    assert r.status_code == 201
    tax = r.get_json()["data"]["id"]
    row = [x for x in _j(cs.get(f"{API}/company-documents?group=tax"))["data"] if x["id"] == tax][0]
    assert row["doc_year"] == 2025 and row["group"] == "tax" and row["vehicle_id"] is None
    r = cs.post(f"{API}/company-documents", json={"category": "company_record", "title": "Vault318 EIN letter"})
    rec = r.get_json()["data"]["id"]
    assert [x for x in _j(cs.get(f"{API}/company-documents?group=records"))["data"] if x["id"] == rec]
    checks += 1

    # ---- files
    def up(doc_id, name, payload=b"%PDF-1.4 vault318\n"):
        return cs.post(f"{API}/company-documents/{doc_id}/files", data={"file": (io.BytesIO(payload), name)},
                       content_type="multipart/form-data")
    assert cs.post(f"{API}/company-documents/{tax}/files", data={}, content_type="multipart/form-data").status_code == 400
    r = up(tax, "return.exe")
    assert r.status_code == 400 and "unsupported" in r.get_json()["error"]
    r = up(tax, "empty.pdf", b"")
    assert r.status_code == 400 and "empty" in r.get_json()["error"]
    assert up(999999, "x.pdf").status_code == 404
    r = up(tax, "1120S-2025.pdf")
    assert r.status_code == 201, r.get_json()
    f1 = _j(r, "file upload")["data"]
    assert f1["file_name"] == "1120S-2025.pdf" and f1["mime"] == "application/pdf" and f1["file_size"] > 0
    assert f1["file_url"] == f"{API}/company-documents/files/{f1['id']}"
    r = up(tax, "receipt.PNG", b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    assert r.status_code == 201 and r.get_json()["data"]["mime"] == "image/png"
    f2 = r.get_json()["data"]
    stored = list((DATA_ROOT / "data_room" / "company_vault" / str(tax)).iterdir())
    assert len(stored) == 2 and all(len(p.stem) == 32 for p in stored), "uuid names, original filename never in the path"
    docs = _j(cs.get(f"{API}/company-documents"), "documents with files")
    row = [x for x in docs["data"] if x["id"] == tax][0]
    assert row["file_count"] == 2 and {f["id"] for f in row["files"]} == {f1["id"], f2["id"]}
    # served inline, no-store, gated
    r = cs.get(f1["file_url"])
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data.startswith(b"%PDF-1.4 vault318")
    assert "no-store" in r.headers.get("Cache-Control", "") and "attachment" not in (r.headers.get("Content-Disposition") or "")
    assert _login(app, "pm").get(f1["file_url"]).status_code == 403
    assert app.test_client().get(f1["file_url"]).status_code == 401
    assert cs.get(f"{API}/company-documents/files/999999").status_code == 404
    # delete removes the row and the disk file
    assert cs.delete(f"{API}/company-documents/files/{f2['id']}").status_code == 200
    assert cs.delete(f"{API}/company-documents/files/{f2['id']}").status_code == 404
    assert len(list((DATA_ROOT / "data_room" / "company_vault" / str(tax)).iterdir())) == 1
    row = [x for x in _j(cs.get(f"{API}/company-documents"))["data"] if x["id"] == tax][0]
    assert row["file_count"] == 1
    # archived document refuses new files
    assert cs.delete(f"{API}/company-documents/{rec}").status_code == 200
    assert up(rec, "late.pdf").status_code == 404
    checks += 1

    # ---- compliance rollup
    co = _j(cs.get(f"{API}/compliance?days=30"), "compliance")["data"]
    by = {(i["kind"], i["id"]): i for i in co["items"]}
    assert by[("vehicle", ins)]["subject"] == "Vault318 Van 2" and by[("vehicle", ins)]["state"] == "due_30"
    assert by[("vehicle", ins)]["subject_ref"] == v["id"] and by[("vehicle", ins)]["group"] == "Vehicle Insurance"
    assert by[("vehicle", reg_old)]["state"] == "expired"
    assert co["by_kind"].get("vehicle", 0) >= 2
    r = cs.post(f"{API}/company-documents", json={"category": "insurance_gl", "title": "Vault318 GL", "expiry_date": D(5)})
    gl = r.get_json()["data"]["id"]
    co = _j(cs.get(f"{API}/compliance?days=30"))["data"]
    gl_item = [i for i in co["items"] if i["kind"] == "company_document" and i["id"] == gl][0]
    assert gl_item["subject"] == "Company" and gl_item["state"] == "due_30"
    ov = _j(cs.get(f"{API}/overview"), "overview")["data"]
    assert ov["expiring"]["next_30"] >= 2
    checks += 1

    # ---- retire: single-shot, hidden by default, listed with archived=1, paper stays
    assert cs.delete(f"{API}/vehicles/{v['id']}").status_code == 200
    assert cs.delete(f"{API}/vehicles/{v['id']}").status_code == 404
    assert not any(x["id"] == v["id"] for x in _j(cs.get(f"{API}/vehicles"))["data"])
    gone = [x for x in _j(cs.get(f"{API}/vehicles?archived=1"))["data"] if x["id"] == v["id"]][0]
    assert gone["status"] == "retired" and gone["missing"] == [] and gone["doc_count"] == 2
    assert cs.patch(f"{API}/vehicles/{v['id']}", json={"color": "Red"}).status_code == 404, "retired vehicles are read-only"
    assert [x for x in _j(cs.get(f"{API}/company-documents"))["data"] if x["id"] == ins][0]["vehicle_label"] == "Vault318 Van 2"
    checks += 1

    print(f"PASS: #318 company vault — gates, vehicles CRUD + driver roster, vehicle paper + missing flags, tax years, "
          f"group filters, file upload/serve/delete (gated, path-contained), compliance rollup, retire ({checks} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
