#!/usr/bin/env python3
"""Projects batch C (Oct 7 C-suite notes): job numbers, a color per job, access method,
workforce access, the Create / Edit project window. Guard.

Boots the real app on an ISOLATED database. Proves:
  * every job gets a permanent number in creation order (#1 = oldest), unique, continuing from
    the highest; active jobs get distinct colors (the old hashed color kept when free); a
    closed job is not colored
  * onboarding gives the next number, the chosen (or a free) color, the job type, the access
    method (Exterior Restoration only) and the worker access; a bad value is refused BEFORE
    anything is created
  * the setup API is admin / C-suite only, validates, and edits color / access / worker access
  * the projects list, the calendar and the job header carry the number, the color and the
    access chips
  * the worker app gets each job's features; with photos switched off the upload is refused
  * the pages: the wide sectioned window with Edit mode, the # column, a 16-color palette that
    matches project_setup.PALETTE everywhere, the photo button that hides
Run:  venv/bin/python tests/smoke_projects_batch_c.py
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_batchc_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

from PIL import Image  # noqa: E402

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)
import project_setup  # noqa: E402

TAG = "batchc"
PW = "ProjectsBatchC2026"
OLD = ("BATCHC-OLD1", "BATCHC-OLD2", "BATCHC-CLOSED")
EID, WID, PIN = "E-97201", "W-9720", "9720"
SITE = (40.8083, -73.9162)
T = date.today()
D = lambda n: (T + timedelta(days=n)).isoformat()   # noqa: E731


def _db():
    return db_layer.connect(pragma_fk=False)


def _codes(conn):
    return [r["project_code"] for r in conn.execute(
        "SELECT project_code FROM projects WHERE project_code LIKE 'BATCHC-%' OR name LIKE ?", (f"{TAG}%",)).fetchall()]


def _wipe(conn):
    codes = _codes(conn)
    for sql, args in (("DELETE FROM field_photos WHERE employee_id = ?", (EID,)),
                      ("DELETE FROM worker_app_session WHERE employee_id = ?", (EID,)),
                      ("DELETE FROM sign_in_log WHERE employee_id = ?", (EID,)),
                      ("DELETE FROM project_assignments WHERE employee_id = ?", (EID,)),
                      ("DELETE FROM employees WHERE employee_id = ?", (EID,))):
        try:
            conn.execute(sql, args)
            conn.commit()
        except Exception:
            conn.rollback()
    if codes:
        marks = ",".join("?" * len(codes))
        for t in ("pm_project_assignment", "project_assignments", "estimate"):
            try:
                col = "converted_project_code" if t == "estimate" else "project_code"
                conn.execute(f"DELETE FROM {t} WHERE {col} IN ({marks})", codes)
                conn.commit()
            except Exception:
                conn.rollback()
        purge(conn, "projects", "project_code", codes)
    for sql in ("DELETE FROM crm_organization WHERE name LIKE ?",):
        try:
            conn.execute(sql, (f"{TAG}%",))
            conn.commit()
        except Exception:
            conn.rollback()
    conn.commit()


def _seed() -> dict:
    conn = _db()
    try:
        _wipe(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        purge_users(conn, uids)
        ids = {}
        for role in ("admin", "c_suite", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"{TAG} {role}"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.commit()
        return ids
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        _wipe(conn)
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


def _row(code):
    conn = _db()
    try:
        return dict(conn.execute("SELECT * FROM projects WHERE project_code = ?", (code,)).fetchone())
    finally:
        conn.close()


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (320, 240), (150, 90, 60)).save(buf, "JPEG", quality=80)
    return buf.getvalue()


def main() -> int:
    _seed()
    passed = 0
    cs, pm = _login("c_suite"), _login("pm")

    # ---- numbering + colors of existing jobs
    conn = _db()
    try:
        top = conn.execute("SELECT COALESCE(MAX(job_number), 0) FROM projects").fetchone()[0]
        for code, created, status in ((OLD[1], "2024-03-01 09:00:00", "active"), (OLD[0], "2024-01-01 09:00:00", "active"),
                                      (OLD[2], "2024-02-01 09:00:00", "closed")):
            conn.execute("INSERT INTO projects (project_code, name, address, status, created_at) VALUES (?, ?, 'x', ?, ?)",
                         (code, f"{TAG} {code}", status, created))
        conn.commit()
        project_setup.backfill(conn)
        project_setup.backfill(conn)   # idempotent
        n = {c: conn.execute("SELECT job_number, color_index FROM projects WHERE project_code = ?", (c,)).fetchone() for c in OLD}
        assert [n[OLD[0]][0], n[OLD[2]][0], n[OLD[1]][0]] == [top + 1, top + 2, top + 3], "numbered oldest first, after the highest"
        assert n[OLD[2]][1] is None, "a closed job is not colored"
        used = project_setup.colors_in_use(conn)
        assert all(v == 1 for v in used.values()) or len(used) >= len(project_setup.PALETTE), ("active jobs share a color", used)
        dup = False
        try:
            conn.execute("UPDATE projects SET job_number = ? WHERE project_code = ?", (n[OLD[0]][0], OLD[1]))
            conn.commit()
        except Exception:
            conn.rollback()
            dup = True
        assert dup, "job numbers are unique"
    finally:
        conn.close()
    passed += 1; print("  numbering + colors ok")

    # ---- onboarding: next number, chosen color, type, access, worker access; bad values refused first
    meta = cs.get("/api/admin/projects/setup-meta").get_json()["data"]
    assert len(meta["palette"]) == 16 and meta["next_job_number"] == top + 4
    assert pm.get("/api/admin/projects/setup-meta").status_code == 403
    base = {"new_org_name": f"{TAG} Owner LLC", "est_type": "FR", "borough": "BX", "address": "1 Batch C Ave", "billing_type": "lump_sum",
            "name": f"{TAG} Exterior job"}
    conn = _db()
    try:
        before = conn.execute("SELECT COUNT(1) FROM projects").fetchone()[0]
    finally:
        conn.close()
    for bad in ({"color_index": 99}, {"access_methods": ["ladder"]}, {"worker_access": ["payroll"]},
                {"est_type": "IR", "access_methods": ["rope_access"]},
                {"est_type": "OTH"}, {"est_type": "OTH", "job_type_other": "   "}, {"est_type": "OTH", "job_type_other": "x" * 81}):
        r = cs.post("/api/admin/projects/onboard", json={**base, **bad})
        assert r.status_code == 400, (bad, r.status_code)
    conn = _db()
    try:
        assert conn.execute("SELECT COUNT(1) FROM projects").fetchone()[0] == before, "a refused create leaves nothing behind"
    finally:
        conn.close()
    r = cs.post("/api/admin/projects/onboard", json={**base, "color_index": 13, "access_methods": ["rope_access", "suspended_scaffold"],
                                                     "worker_access": ["photos", "drawings"]})
    assert r.status_code == 201, r.get_data(as_text=True)[:300]
    d = r.get_json()["data"]
    fr = d["project_code"]
    assert d["job_number"] == top + 4 and d["setup"]["color_index"] == 13
    row = _row(fr)
    assert json.loads(row["access_methods"]) == ["suspended_scaffold", "rope_access"] and json.loads(row["worker_access"]) == ["photos", "drawings"]
    if "project_type" in row:
        assert row["project_type"] == "facade", row["project_type"]
    r = cs.post("/api/admin/projects/onboard", json={**base, "est_type": "IR", "name": f"{TAG} Interior job", "address": "2 Batch C Ave"})
    assert r.status_code == 201
    ir = r.get_json()["data"]["project_code"]
    irow = _row(ir)
    assert irow["job_number"] == top + 5 and irow["access_methods"] is None and json.loads(irow["worker_access"]) == ["photos"]
    assert irow["color_index"] != 13, "the next job gets a color nobody active uses"
    # "Other": anything outside the four types, described in words; the description stands in for the type name
    om = cs.get("/api/admin/projects/onboard-meta").get_json()
    om = om.get("data", om)
    assert any(t["code"] == "OTH" and t["label"] == "Other" for t in om["types"]), om["types"]
    assert meta["other_types"] == ["OTH"] and meta["other_max"] == 80
    r = cs.post("/api/admin/projects/onboard", json={**base, "est_type": "OTH", "job_type_other": "  Basement   excavation ",
                                                     "name": f"{TAG} Other job", "address": "3 Batch C Ave"})
    assert r.status_code == 201, r.get_data(as_text=True)[:300]
    oth = r.get_json()["data"]["project_code"]
    orow = _row(oth)
    assert oth.startswith("OTH-BX-") and orow["job_type_other"] == "Basement excavation" and orow["access_methods"] is None
    if "project_type" in orow:
        assert orow["project_type"] == "generic", orow["project_type"]
    su = cs.get(f"/api/admin/projects/{oth}/setup").get_json()["data"]
    assert su["job_type_other"] == "Basement excavation" and su["job_type_label"] == "Basement excavation" and su["other_applies"] is True
    assert cs.get(f"/api/admin/projects/{fr}/setup").get_json()["data"]["job_type_label"] == "Exterior Restoration"
    passed += 1; print("  onboarding ok")

    # ---- the setup API
    assert pm.get(f"/api/admin/projects/{fr}/setup").status_code == 403
    assert cs.put(f"/api/admin/projects/{ir}/setup", json={"access_methods": ["boom_lift"]}).status_code == 400, "not an exterior job"
    assert cs.put(f"/api/admin/projects/{fr}/setup", json={"color_index": -1}).status_code == 400
    s = cs.put(f"/api/admin/projects/{fr}/setup", json={"color_index": 2, "access_methods": [], "worker_access": []}).get_json()["data"]
    assert s["color_index"] == 2 and s["access_methods"] == [] and s["worker_access"] == [] and s["job_number"] == top + 4
    s = cs.put(f"/api/admin/projects/{fr}/setup", json={"access_methods": ["boom_lift"], "worker_access": ["photos"]}).get_json()["data"]
    assert s["access_labels"] == ["Boom lift"] and s["color_index"] == 2
    mine = cs.get(f"/api/admin/projects/setup-meta?project={fr}").get_json()["data"]["used"].get("2", 0)
    all_ = cs.get("/api/admin/projects/setup-meta").get_json()["data"]["used"].get("2", 0)
    assert all_ == mine + 1, "the job's own color is not counted against it"
    s = cs.put(f"/api/admin/projects/{oth}/setup", json={"job_type_other": "Underpinning"}).get_json()["data"]
    assert s["job_type_label"] == "Underpinning"
    assert cs.put(f"/api/admin/projects/{oth}/setup", json={"job_type_other": ""}).status_code == 400, "an Other job keeps a description"
    s = cs.put(f"/api/admin/projects/{fr}/setup", json={"job_type_other": "ignored"}).get_json()["data"]
    assert s["job_type_other"] is None and s["job_type_label"] == "Exterior Restoration", "only Other jobs carry a description"
    passed += 1; print("  setup api ok")

    # ---- list, calendar, header
    rows = {r["project_code"]: r for r in cs.get("/api/company/portal/projects?status=all").get_json()["data"]}
    assert rows[fr]["job_number"] == top + 4 and rows[fr]["color"] == project_setup.PALETTE[2] and rows[fr]["access_labels"] == ["Boom lift"]
    assert rows[OLD[0]]["job_number"] == top + 1
    cs.put(f"/api/admin/projects/{fr}/crew", json={"start_date": D(-1), "target_completion": D(20)})
    cal = cs.get(f"/api/company/portal/calendar?from={D(0)}&to={D(6)}").get_json()["data"]
    job = next(j for j in cal["jobs"] if j["project_code"] == fr)
    assert job["color"] == 2, job
    h = cs.get(f"/api/projects/{fr}/summary").get_json()["data"]["header"]
    assert h["job_number"] == top + 4 and h["access_labels"] == ["Boom lift"]
    passed += 1; print("  list + calendar + header ok")

    # ---- the worker app: features per job; photos off -> upload refused
    conn = _db()
    try:
        try:
            conn.execute("ALTER TABLE employees ADD COLUMN pin TEXT")
            conn.commit()
        except Exception:
            conn.rollback()
        conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, pin, created_at, updated_at) VALUES (?, ?, 'Batchc Mason', 'mason', ?, ?, ?)",
                     (EID, WID, PIN, D(0), D(0)))
        conn.execute("UPDATE projects SET site_lat = ?, site_lng = ?, geofence_m = 200 WHERE project_code = ?", (*SITE, fr))
        conn.execute("INSERT INTO project_assignments (project_code, employee_id, start_date, status) VALUES (?, ?, ?, 'active')", (fr, EID, D(0)))
        conn.commit()
    finally:
        conn.close()
    w = server.app.test_client()
    r = w.post("/api/worker/login", json={"phone_or_pin": PIN, "latitude": SITE[0], "longitude": SITE[1]})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    lg = r.get_json()["data"]
    token = lg["session_token"]
    pj = next(p for p in lg.get("projects", []) if p["project_code"] == fr)
    assert pj["features"] == ["photos"], pj
    assert w.post("/api/worker/session/start", json={"employee_id": EID, "project_code": fr, "session_token": token}).status_code == 200
    form = lambda: {"session_token": token, "employee_id": EID, "location": "S2", "scope": "pointing", "photo": (io.BytesIO(_jpeg()), "p.jpg")}   # noqa: E731
    assert w.post("/api/worker/photos", data=form(), content_type="multipart/form-data").status_code == 201, "photos on: uploads"
    cs.put(f"/api/admin/projects/{fr}/setup", json={"worker_access": []})
    r = w.post("/api/worker/photos", data=form(), content_type="multipart/form-data")
    assert r.status_code == 403 and "switched off" in r.get_json()["error"], r.get_data(as_text=True)[:200]
    passed += 1; print("  worker app ok")

    # ---- the pages
    js = (ROOT / "static" / "js" / "new_project_modal.js").read_text(encoding="utf-8")
    assert 'id="np-other"' in js and "function otherApplies()" in js and "job_type_other: otherApplies()" in js
    assert "max-width:1060px" in js and "edit: edit" in js and "/setup" in js and "/drawing-sets" in js and "np-s-work" in js and "np-s-access" in js
    portal = cs.get("/company").get_data(as_text=True)
    assert "{ key: 'job_number', label: '#'" in portal and "data-pj-edit" in portal and "new_project_modal.js?v=1014" in portal
    assert "defaultSort: { key: 'job_number', dir: 'asc' }" in portal, "#1 at the top, the newest job at the bottom"
    pal = list(project_setup.PALETTE)
    jc = re.search(r"const JOB_COLORS = \[([^\]]+)\]", portal).group(1)
    assert re.findall(r"'(#[0-9A-F]{6})'", jc) == pal
    assert all(f".c{i}{{background:{c}}}" in portal and f".cw{i}{{background:{c}}}" in portal for i, c in enumerate(pal))
    home = (ROOT / "projects.html").read_text(encoding="utf-8")
    assert re.findall(r"'(#[0-9A-F]{6})'", re.search(r"var COLORS = \[([^\]]+)\]", home).group(1)) == pal
    dash = cs.get(f"/projects/{fr}").get_data(as_text=True)
    assert 'id="ps-proj-edit"' in dash and "window.SSCNewProject.edit(decodeURIComponent(" in dash
    wa = (ROOT / "worker-app.html").read_text(encoding="utf-8")
    assert "sessionData.photosOn" in wa and ".photo-btn[hidden]" in wa
    passed += 1; print("  pages ok")

    print(f"PASS smoke_projects_batch_c ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_projects_batch_c: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
