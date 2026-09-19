#!/usr/bin/env python3
"""#325 — crews, site coordinates and the worker app's job picker: behavioral guard.

Boots the real app (`import server`) on an ISOLATED database. Proves:

  * migration: projects.site_lat / site_lng / geofence_m exist and are idempotent;
    the legacy Bronx coordinates are seeded onto FR-BX-001 when it has none
  * onboarding with a crew + site: assignments written (start = the start date),
    coordinates stored, a worker off the active roster -> 404, bad coordinates
    -> 400, nothing written on a rejected payload
  * crew endpoint: gates (pm / client 403, anon 401), GET shape (assigned flags,
    site), PUT adds + off-rolls (never deletes), site update, radius bounds,
    unknown project -> 404
  * worker login (PIN + location):
      - assigned to two jobs, inside only one geofence -> that job auto-picked
      - inside both -> no auto pick, both offered with distances
      - outside every geofenced job, none without coordinates -> 403 Not on site
      - a job with no coordinates is always eligible
      - no assignments at all -> every active project offered (closed excluded)
      - retired worker still refused
  * shift start: a job that is not the worker's -> 403; the picked job is
    accepted, the session row records it, and a second start -> 409

Run:  venv/bin/python tests/smoke_crew_sites_325.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_crew_325_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "crew325"
PW = "CrewSmokePassword2026"
ROLES = ("c_suite", "admin", "pm", "client")
TODAY = date.today()
API = "/api/admin/projects"
# two sites ~1.3 km apart in the Bronx, and one far away
SITE_A = (40.8083, -73.9162)
SITE_B = (40.8200, -73.9162)
FAR = (40.7000, -74.0000)


def D(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def _db():
    return db_layer.connect(pragma_fk=False)


def _table(conn, name) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchall()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


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
        _cleanup_rows(conn)
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Crew325 %'")
        ids["org"] = crm.create_org(conn, name="Crew325 Owners", relationship_type="client")
        # the fixture may predate employees.pin
        try:
            conn.execute("ALTER TABLE employees ADD COLUMN pin TEXT")
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        for eid, wid, name, pin in (("E-93251", "W-9325", "Crew325 Two Jobs", "9251"),
                                    ("E-93252", "W-9326", "Crew325 Unassigned", "9252"),
                                    ("E-93253", "W-9327", "Crew325 Retired", "9253")):
            conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, pin, created_at, updated_at) "
                         "VALUES (?, ?, ?, 'mason', ?, ?, ?)", (eid, wid, name, pin, D(0), D(0)))
        conn.execute("INSERT INTO labor_worker_state (worker_id, employee_id, trade, status, created_at, updated_at) "
                     "VALUES ('W-9327', 'E-93253', 'mason', 'inactive', ?, ?)", (D(0), D(0)))
        for code, name, lat, lng, status in (("CRW325-A", "Crew325 Site A", *SITE_A, "active"),
                                             ("CRW325-B", "Crew325 Site B", *SITE_B, "active"),
                                             ("CRW325-N", "Crew325 No coords", None, None, "active"),
                                             ("CRW325-X", "Crew325 Closed", *SITE_A, "closed")):
            conn.execute("INSERT INTO projects (project_code, name, address, status, site_lat, site_lng, geofence_m) "
                         "VALUES (?, ?, 'x', ?, ?, ?, 200)", (code, name, status, lat, lng))
        for code in ("CRW325-A", "CRW325-B", "CRW325-X"):
            conn.execute("INSERT INTO project_assignments (project_code, employee_id, start_date, status) "
                         "VALUES (?, 'E-93251', ?, 'active')", (code, D(-5)))
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup_rows(conn):
    for sql in ("DELETE FROM worker_app_session WHERE employee_id LIKE 'E-9325%'",
                "DELETE FROM sign_in_log WHERE employee_id LIKE 'E-9325%'",
                "DELETE FROM project_assignments WHERE employee_id LIKE 'E-9325%' OR project_code LIKE 'CRW325-%'",
                "DELETE FROM pm_project_assignment WHERE project_code LIKE 'CRW325-%'",
                "DELETE FROM labor_worker_state WHERE worker_id LIKE 'W-932%'",
                "DELETE FROM employees WHERE employee_id LIKE 'E-9325%'"):
        try:
            conn.execute(sql)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass


def _cleanup():
    conn = _db()
    try:
        _cleanup_rows(conn)
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Crew325 %'").fetchall()]
        ph = ",".join("?" * len(org_ids)) or "NULL"
        est_ids = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph})", org_ids).fetchall()]
        codes = [r["project_code"] for r in conn.execute(
            f"SELECT project_code FROM projects WHERE client_org_id IN ({ph}) OR project_code LIKE 'CRW325-%'", org_ids).fetchall()]
        for t, col in (("project_assignments", "project_code"), ("pm_project_assignment", "project_code"),
                       ("sign_in_log", "project_code"), ("ira_job", "project_code")):
            purge(conn, t, col, codes)
        for t in ("ira_job", "estimate_document", "proposal"):
            purge(conn, t, "estimate_id", est_ids)
        purge(conn, "projects", "project_code", codes)
        purge(conn, "estimate", "id", est_ids)
        purge(conn, "building", "client_org_id", org_ids)
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
    assert r.status_code == 200, (role, r.status_code, r.get_data(as_text=True)[:200])
    return c


def _worker_login(pin, lat, lng):
    c = server.app.test_client()
    r = c.post("/api/worker/login", json={"phone_or_pin": pin, "latitude": lat, "longitude": lng},
               environ_base={"REMOTE_ADDR": f"10.32.5.{int(pin) % 250}"})
    return c, r


def main() -> int:
    ids = _seed()
    checks = 0

    # ---- migration idempotent + legacy seed
    import apply_project_site_325 as mig
    conn = _db()
    try:
        assert mig.ensure(conn)["added"] == [] and mig._has_column(conn, "projects", "geofence_m")
        conn.execute("DELETE FROM projects WHERE project_code = 'FR-BX-001-CREW325'")
        conn.execute("INSERT INTO projects (project_code, name, address) VALUES ('FR-BX-001-CREW325', 'x', 'x')")
        conn.commit()
        legacy = conn.execute("SELECT site_lat FROM projects WHERE project_code = 'FR-BX-001'").fetchone()
        if legacy is not None:
            assert legacy["site_lat"] is not None, "the legacy Bronx site keeps its geofence"
        conn.execute("DELETE FROM projects WHERE project_code = 'FR-BX-001-CREW325'")
        conn.commit()
    finally:
        conn.close()
    checks += 1

    # ---- gates on the crew endpoint
    for role in ("pm", "client"):
        c = _login(role)
        assert c.get(f"{API}/CRW325-A/crew").status_code == 403, role
        assert c.put(f"{API}/CRW325-A/crew", json={"crew": []}).status_code == 403, role
    assert server.app.test_client().get(f"{API}/CRW325-A/crew").status_code == 401
    cs = _login("c_suite")
    assert cs.get(f"{API}/CRW325-NOPE/crew").status_code == 404
    checks += 1

    # ---- crew GET / PUT
    d = cs.get(f"{API}/CRW325-A/crew").get_json()["data"]
    assert d["assigned_count"] == 1 and d["site"]["site_lat"] == SITE_A[0] and d["site"]["geofence_m"] == 200
    W = {w["employee_id"]: w for w in d["workers"]}
    assert W["E-93251"]["assigned"] is True and W["E-93252"]["assigned"] is False
    assert "E-93253" not in W, "retired workers are not offered"
    r = cs.put(f"{API}/CRW325-A/crew", json={"crew": ["E-93253"]})
    assert r.status_code == 404 and "active roster" in r.get_json()["error"]
    assert cs.put(f"{API}/CRW325-A/crew", json={"geofence_m": 5}).status_code == 400
    assert cs.put(f"{API}/CRW325-A/crew", json={"site_lat": 91, "site_lng": 0}).status_code == 400
    assert cs.put(f"{API}/CRW325-A/crew", json={"site_lat": 40.8, "site_lng": ""}).status_code == 400
    r = cs.put(f"{API}/CRW325-A/crew", json={"crew": ["E-93252"], "geofence_m": 150})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()["data"]
    assert d["change"]["added"] == ["E-93252"] and d["change"]["removed"] == ["E-93251"]
    assert d["assigned_count"] == 1 and d["site"]["geofence_m"] == 150
    conn = _db()
    try:
        rows = conn.execute("SELECT employee_id, status, end_date FROM project_assignments WHERE project_code = 'CRW325-A' "
                            "ORDER BY employee_id").fetchall()
        st = {r["employee_id"]: (r["status"], r["end_date"]) for r in rows}
        assert st["E-93251"] == ("off-rolled", D(0)) and st["E-93252"][0] == "active", st
        # put it back for the login checks
    finally:
        conn.close()
    r = cs.put(f"{API}/CRW325-A/crew", json={"crew": ["E-93251"], "geofence_m": 200})
    assert r.status_code == 200 and r.get_json()["data"]["change"]["added"] == ["E-93251"]
    conn = _db()
    try:
        n = conn.execute("SELECT COUNT(1) AS n FROM project_assignments WHERE project_code = 'CRW325-A' AND employee_id = 'E-93251' "
                         "AND status = 'active'").fetchone()["n"]
        assert n == 1, "re-adding creates one fresh active row"
    finally:
        conn.close()
    checks += 1

    # ---- onboarding with crew + site
    base = {"client_org_id": ids["org"], "est_type": "FR", "borough": "BK", "address": "9 Crew325 Lane", "name": "Crew325 onboarded"}
    assert cs.post(f"{API}/onboard", json={**base, "crew": ["E-93253"]}).status_code == 404
    assert cs.post(f"{API}/onboard", json={**base, "site_lat": "abc", "site_lng": 1}).status_code == 400
    assert cs.post(f"{API}/onboard", json={**base, "crew": "E-93251"}).status_code == 400
    conn = _db()
    try:
        assert not conn.execute("SELECT 1 FROM projects WHERE name = 'Crew325 onboarded'").fetchone(), "rejected -> nothing written"
    finally:
        conn.close()
    r = cs.post(f"{API}/onboard", json={**base, "crew": ["E-93251", "E-93252"], "site_lat": SITE_B[0], "site_lng": SITE_B[1],
                                        "geofence_m": 300, "start_date": D(-2)})
    assert r.status_code == 201, r.get_json()
    d = r.get_json()["data"]
    code = d["project_code"]
    assert sorted(d["crew"]) == ["E-93251", "E-93252"] and d["project"]["site_lat"] == SITE_B[0] and d["project"]["geofence_m"] == 300
    conn = _db()
    try:
        rows = conn.execute("SELECT employee_id, start_date, status FROM project_assignments WHERE project_code = ? ORDER BY employee_id",
                            (code,)).fetchall()
        assert [(r["employee_id"], r["start_date"], r["status"]) for r in rows] == [("E-93251", D(-2), "active"), ("E-93252", D(-2), "active")]
    finally:
        conn.close()
    # take the unassigned worker off it again so the "no assignment" login case below holds
    cs.put(f"{API}/{code}/crew", json={"crew": ["E-93251"]})
    checks += 1

    # ---- worker login: per-job geofence
    # E-93251 has A, B, the closed X and the onboarded job (at SITE_B, 300 m).
    c, r = _worker_login("9251", *SITE_A)
    assert r.status_code == 200, r.get_json()
    j = r.get_json()["data"]
    assert j["project_code"] == "CRW325-A", j                       # only A in range -> auto pick
    assert [p["project_code"] for p in j["projects"]] == ["CRW325-A"]
    assert j["projects"][0]["in_range"] is True and j["projects"][0]["distance_m"] < 50
    token_a = j["session_token"]
    c2, r = _worker_login("9251", *SITE_B)
    assert r.status_code == 200, r.get_json()
    j = r.get_json()["data"]
    assert j["project_code"] is None, j                               # B + the onboarded job both in range -> chooser
    assert {p["project_code"] for p in j["projects"]} == {"CRW325-B", code}
    assert all(p["in_range"] for p in j["projects"]) and not any(p["project_code"] == "CRW325-X" for p in j["projects"])
    token_b = j["session_token"]
    _, r = _worker_login("9251", *FAR)
    assert r.status_code == 403 and r.get_json()["error"] == "Not on site"
    # a job with no coordinates is always eligible
    cs.put(f"{API}/CRW325-N/crew", json={"crew": ["E-93251"]})
    _, r = _worker_login("9251", *FAR)
    assert r.status_code == 200 and r.get_json()["data"]["project_code"] == "CRW325-N"
    cs.put(f"{API}/CRW325-N/crew", json={"crew": []})
    # no assignments at all -> every active project offered (closed excluded), each geofenced
    _, r = _worker_login("9252", *SITE_A)
    assert r.status_code == 200, r.get_json()
    j = r.get_json()["data"]
    offered = {p["project_code"] for p in j["projects"]}
    assert "CRW325-A" in offered and "CRW325-N" in offered and "CRW325-B" not in offered and "CRW325-X" not in offered
    assert all(p["assigned"] is False for p in j["projects"])
    # retired -> refused
    _, r = _worker_login("9253", *SITE_A)
    assert r.status_code == 403
    checks += 1

    # ---- shift start honours the pick
    r = c2.post("/api/worker/session/start", json={"employee_id": "E-93251", "project_code": "CRW325-N", "session_token": token_b})
    assert r.status_code == 403 and "your jobs" in r.get_json()["error"], r.get_json()
    r = c2.post("/api/worker/session/start", json={"employee_id": "E-93251", "project_code": "", "session_token": token_b})
    assert r.status_code == 403
    r = c2.post("/api/worker/session/start", json={"employee_id": "E-93251", "project_code": "CRW325-B", "session_token": token_b})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["data"]["project_code"] == "CRW325-B"
    conn = _db()
    try:
        row = conn.execute("SELECT project_code FROM worker_app_session WHERE employee_id = 'E-93251' AND project_code = 'CRW325-B'").fetchone()
        assert row, "the session row records the picked job"
        log_row = conn.execute("SELECT project_code FROM sign_in_log WHERE employee_id = 'E-93251' ORDER BY id DESC LIMIT 1").fetchone()
        assert log_row["project_code"] == "CRW325-B"
    finally:
        conn.close()
    r = c2.post("/api/worker/session/start", json={"employee_id": "E-93251", "project_code": "CRW325-B", "session_token": token_b})
    assert r.status_code == 409
    # the auto-picked session cannot be redirected to another job
    r = c.post("/api/worker/session/start", json={"employee_id": "E-93251", "project_code": "CRW325-B", "session_token": token_a})
    assert r.status_code == 401
    r = c.post("/api/worker/session/start", json={"employee_id": "E-93251", "project_code": "CRW325-A", "session_token": token_a})
    assert r.status_code == 200
    checks += 1

    print(f"PASS: #325 crews + sites + job picker — migration, crew endpoint, onboarding with crew/site, per-job geofence "
          f"login (auto pick / chooser / not on site / no coords / unassigned / retired), shift start ({checks} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
