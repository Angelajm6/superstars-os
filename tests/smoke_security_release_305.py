#!/usr/bin/env python3
"""#305 — the security release: behavioral guard.

Boots the REAL app (server.app) in-process on an ISOLATED database + a scratch
data root, signs in one user per role, and proves each fix by behavior:

  * PIN throttle counts pin_fail: 20 failures from one source -> 429 on the next
  * /api/cards/<id>/<type>/live: company roles only
  * /api/employees (+ detail, certifications, sign-ins): internal roles only;
    `pin` never in the payload; contact/PII fields only for company roles
  * site-closure checklist PATCH: column name allow-listed (SQL injection closed)
  * /api/rfis: project RFIs work again; elevation_id dispatches to drawing RFIs
  * estimator is contained to the estimating workspace; vendor is contained to
    nothing and is no longer assignable
  * worker create rejects a traversal employee_id
  * /project-files/<path>: pm/super must be assigned to the artifact's project
  * /preview/*: company roles only, and app pages are not previewable
  * /login?next= is percent-encoded
  * no duplicate (rule, method) registrations remain
  * dob_* upserts are Postgres-compatible (no INSERT OR REPLACE left)

Run:  venv/bin/python tests/smoke_security_release_305.py
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_sec_305_")


def _build_isolated_sqlite() -> str:
    import preprod_fixture_bootstrap as pfb
    src = Path(_TMP) / "source"
    shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", "superstars.db*", "venv", "node_modules", "worker_records", "data_room"))
    db = src / "superstars.db"
    with sqlite3.connect(db) as conn:
        conn.executescript((src / "schema.sql").read_text(encoding="utf-8"))
    env = os.environ.copy()
    env.pop("SSC_DB_URL", None); env.pop("SSC_DATA_ROOT", None)
    env["SSC_ALLOW_EMPTY_PREPROD_FIXTURE"] = "1"
    for script in pfb.LEGACY_MIGRATIONS + pfb.DUAL_BACKEND_MIGRATIONS:
        subprocess.run([sys.executable, script], cwd=src, env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"sqlite:{db}"


if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ["SSC_STRICT_ROUTES"] = "1"
for k in ("SSC_TRUSTED_PROXY", "GOOGLE_OAUTH_CLIENT_ID"):
    os.environ.pop(k, None)

import server  # noqa: E402  (boots the whole app; strict routes would SystemExit on a duplicate)
import auth  # noqa: E402
import auth_admin  # noqa: E402
import db_layer  # noqa: E402

TAG = "sec305"
PW = "SecuritySmokePassword2026"
ROLES = ("c_suite", "admin", "pm", "super", "estimator", "client", "vendor")
PROJ_A, PROJ_B = "S305-A", "S305-B"


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
        for code in (PROJ_A, PROJ_B):
            conn.execute("DELETE FROM projects WHERE project_code = ?", (code,))
            conn.execute("INSERT INTO projects (project_code, name, address, status, created_at, updated_at) "
                         "VALUES (?, ?, '1 Test St', 'active', ?, ?)",
                         (code, f"Smoke {code}", datetime.now().isoformat(), datetime.now().isoformat()))
        pm_id = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.pm@example.com",)).fetchone()["id"]
        conn.execute("DELETE FROM pm_project_assignment WHERE user_id = ?", (pm_id,))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_by, assigned_at) "
                     "VALUES (?, ?, ?, ?)", (pm_id, PROJ_A, pm_id, datetime.now().isoformat()))
        conn.execute("DELETE FROM employees WHERE employee_id = 'E-93050'")
        conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at) "
                     "VALUES ('E-93050', 'W-9350', 'Smoke Worker305', 'mason', ?, ?)",
                     (datetime.now().isoformat(), datetime.now().isoformat()))
        conn.execute("DELETE FROM labor_worker_state WHERE employee_id = 'E-93050'")
        conn.execute("INSERT INTO labor_worker_state (worker_id, employee_id, trade, current_rate, status, effective_date, created_at, updated_at) "
                     "VALUES ('W-9350', 'E-93050', 'mason', 30.0, 'active', ?, ?, ?)",
                     (datetime.now().date().isoformat(), datetime.now().isoformat(), datetime.now().isoformat()))
        conn.execute("DELETE FROM site_closure_log WHERE closure_id = 'CL-305'")
        conn.execute("INSERT INTO site_closure_log (closure_id, date, project_code, created_at, updated_at) "
                     "VALUES ('CL-305', ?, ?, ?, ?)",
                     (datetime.now().date().isoformat(), PROJ_A, datetime.now().isoformat(), datetime.now().isoformat()))
        conn.execute("DELETE FROM login_audit WHERE ip LIKE '10.35.%'")
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)   # throwaway DB; FK order does not matter here
    try:
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        for uid in ids:
            for t in ("sessions", "pm_project_assignment", "login_audit"):
                conn.execute(f"DELETE FROM {t} WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM users WHERE email LIKE ?", (f"{TAG}.%",))
        conn.execute("DELETE FROM rfi_log WHERE project_code IN (?, ?)", (PROJ_A, PROJ_B))
        conn.execute("DELETE FROM site_closure_log WHERE closure_id = 'CL-305'")
        conn.execute("DELETE FROM labor_worker_state WHERE employee_id = 'E-93050'")
        conn.execute("DELETE FROM employees WHERE employee_id = 'E-93050'")
        conn.execute("DELETE FROM projects WHERE project_code IN (?, ?)", (PROJ_A, PROJ_B))
        conn.execute("DELETE FROM login_audit WHERE ip LIKE '10.35.%'")
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _client(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW},
               environ_base={"REMOTE_ADDR": "10.35.0.1"})
    assert r.status_code == 200, (role, r.status_code, r.get_json())
    return c


def main() -> int:
    server.time.sleep = lambda *_a, **_k: None   # the throttle's backoff stall is not the point here
    _seed()
    try:
        # ---- no duplicate routes, and /api/rfis is owned once per method ----
        assert server._duplicate_routes() == [], server._duplicate_routes()
        rfi_rules = [r for r in server.app.url_map.iter_rules() if r.rule == "/api/rfis"]
        assert {m for r in rfi_rules for m in r.methods if m in ("GET", "POST")} == {"GET", "POST"}
        assert all(r.endpoint in ("create_rfi", "get_rfis") for r in rfi_rules), [r.endpoint for r in rfi_rules]

        # ---- PIN throttle counts pin_fail ----
        conn = _db()
        try:
            now = datetime.now().isoformat(timespec="seconds")
            for _ in range(auth.LOGIN_IP_MAX_FAILS):
                conn.execute("INSERT INTO login_audit (user_id, event, at, ip) VALUES (NULL, 'pin_fail', ?, ?)",
                             (now, "10.35.9.9"))
            conn.commit()
        finally:
            conn.close()
        assert auth._recent_ip_fails("10.35.9.9") >= auth.LOGIN_IP_MAX_FAILS
        anon = server.app.test_client()
        r = anon.post("/api/worker/login", json={"phone_or_pin": "1234"}, environ_base={"REMOTE_ADDR": "10.35.9.9"})
        assert r.status_code == 429, (r.status_code, r.get_json())
        r = anon.post("/api/worker/login", json={"phone_or_pin": "1234"}, environ_base={"REMOTE_ADDR": "10.35.9.8"})
        # (the fixture schema has no employees.pin column, so the lookup itself may 500 here;
        #  what matters is that a fresh source is NOT throttled)
        assert r.status_code != 429, "a fresh source is not locked out"

        cs, pm, sup, est, cli, ven = (_client(r) for r in ("c_suite", "pm", "super", "estimator", "client", "vendor"))

        # ---- credential card render: company roles only ----
        assert pm.get("/api/cards/E-93050/cof/live").status_code == 403
        assert sup.get("/api/cards/E-93050/cof/live").status_code == 403
        assert cs.get("/api/cards/E-93050/cof/live").status_code != 403   # (fixture schema has no pin column; only the gate is under test)

        # ---- employee PII ----
        for c, who in ((est, "estimator"), (cli, "client"), (ven, "vendor")):
            assert c.get("/api/employees").status_code in (403, 302), who
        r = pm.get("/api/employees"); assert r.status_code == 200
        rows = r.get_json()["data"]
        mine = [x for x in rows if x.get("employee_id") == "E-93050"]
        assert mine and "pin" not in mine[0] and "dob" not in mine[0] and "phone" not in mine[0]
        r = pm.get("/api/employees/E-93050"); assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:200])
        d = r.get_json()["data"]; assert "pin" not in d and "dob" not in d and "phone" not in d
        r = cs.get("/api/employees/E-93050"); assert r.status_code == 200
        d = r.get_json()["data"]; assert "pin" not in d, "pin never leaves the employee endpoints"
        for path in ("/api/certifications", "/api/sign-ins", "/api/employees/E-93050/certifications"):
            assert est.get(path).status_code in (403, 302), path
            _r = pm.get(path)
            assert _r.status_code in (200, 404), (path, _r.status_code, _r.get_data(as_text=True)[:160])   # 404 = fixture has no intake record

        # ---- site-closure checklist: allow-listed column, Y/N value ----
        inj = pm.patch("/api/site-closures/CL-305/checklist",
                       json={"item": "doc_daily_report='Y', project_code='PWNED' WHERE 1=1 --", "value": "Y"})
        assert inj.status_code == 400, inj.get_json()
        conn = _db()
        try:
            assert conn.execute("SELECT project_code FROM site_closure_log WHERE closure_id='CL-305'").fetchone()["project_code"] == PROJ_A
        finally:
            conn.close()
        assert pm.patch("/api/site-closures/CL-305/checklist", json={"item": "doc_daily_report", "value": "maybe"}).status_code == 400
        assert pm.patch("/api/site-closures/CL-305/checklist", json={"item": "doc_daily_report", "value": "Y"}).status_code == 200
        assert est.patch("/api/site-closures/CL-305/checklist", json={"item": "doc_daily_report", "value": "Y"}).status_code in (403, 302)

        # ---- RFIs: the project form works again; elevation payloads dispatch ----
        r = cs.post("/api/rfis", json={"project_code": PROJ_A, "subject_title": "Smoke 305 RFI", "submitted_by": "smoke"})
        assert r.status_code in (200, 201), (r.status_code, r.get_json())
        body = r.get_json(); rfi_no = (body.get("data") or body).get("rfi_number")
        assert rfi_no, body
        r = cs.get("/api/rfis"); assert r.status_code == 200
        assert any(x.get("rfi_number") == rfi_no for x in r.get_json()["data"])
        r = cs.post("/api/rfis", json={"elevation_id": 999999, "title": "x"})
        assert r.status_code in (403, 404), "elevation payload must reach the drawing-RFI handler"
        assert "project_code required" not in (r.get_data(as_text=True) or "")
        r = cs.get("/api/rfis?elevation_id=999999"); assert r.status_code in (403, 404)

        # ---- estimator / vendor containment ----
        assert est.get("/estimating").status_code == 200
        r = est.get("/"); assert r.status_code in (302, 303) and "/estimating" in r.headers.get("Location", "")
        assert est.get("/api/projects").status_code == 403
        assert est.get("/company").status_code in (302, 403)
        assert ven.get("/api/projects").status_code == 403
        assert ven.get("/projects").status_code == 403
        assert "vendor" not in auth_admin.ASSIGNABLE_ROLES

        # ---- worker create: traversal id ----
        r = cs.post("/api/workers/create", json={"name": "Smoke Trav", "employee_id": "../../etc"})
        assert r.status_code == 400, (r.status_code, r.get_json())
        assert not (DATA_ROOT / "etc").exists()

        # ---- project-files: pm scoped to assigned projects ----
        sub = server._ARTIFACT_SUBTREES[0]
        for code in (PROJ_A, PROJ_B):
            d = DATA_ROOT / "data_room" / sub / code
            d.mkdir(parents=True, exist_ok=True)
            (d / "smoke.html").write_text("<p>smoke</p>", encoding="utf-8")
        assert pm.get(f"/project-files/data_room/{sub}/{PROJ_A}/smoke.html").status_code == 200
        assert pm.get(f"/project-files/data_room/{sub}/{PROJ_B}/smoke.html").status_code == 404
        assert cs.get(f"/project-files/data_room/{sub}/{PROJ_B}/smoke.html").status_code == 200

        # ---- preview: company roles only; app pages never previewable ----
        assert pm.get("/preview/dcr/company-dashboard").status_code == 403
        assert cs.get("/preview/dcr/company-dashboard").status_code == 404
        assert cs.get("/preview/dcr/../server").status_code in (403, 404)

        # ---- login redirect encodes next ----
        r = server.app.test_client().get("/some page?x=1")
        assert r.status_code in (302, 303) and "next=/some%20page" in r.headers.get("Location", ""), r.headers.get("Location")

        # ---- Postgres-compatible upserts ----
        src = (ROOT / "nyc_compliance.py").read_text(encoding="utf-8")
        assert "INSERT OR REPLACE" not in src and src.count("ON CONFLICT(") == 4

        # ---- raw driver errors never reach the client (helper contract) ----
        with server.app.test_request_context("/api/x"):
            resp, code = server._server_error(RuntimeError("table users has no column secret"))
            assert code == 500 and "secret" not in resp.get_data(as_text=True)
    finally:
        _cleanup()
    print("PASS: #305 security release — PIN throttle, card/employee/closure gates, PII strip, "
          "SQL injection closed, RFI routes restored, estimator/vendor contained, traversal, "
          "project-file scoping, preview gate, next= encoding, upserts, error masking")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
