"""#263 — PM project-scoping guard. Server-enforced, dual-backend.

Proves (fails on a pre-#263 server, where the endpoints don't exist / nothing is scoped):
  (a) assignment + project-close endpoints are admin/c_suite-only — a pm gets 403;
  (b) a pm opens an ASSIGNED project but 403s on an UNASSIGNED one and on the company
      overview (page + /api/company/summary), incl. the dropplan project path;
  (c) a CLOSED project is excluded from a pm's projects list AND can no longer be opened
      by the assigned pm — while admin/c_suite still see it (records); reopen restores it;
  (d) a pm is 403 on Financial (SoV — even on an assigned project), the company Workforce
      roster, and Labor Rates; a company role (c_suite) is 200 on Workforce + Labor (not
      over-blocked).
Plus a literal fail->pass: BEFORE assignment a pm can't open project A (403); AFTER an
admin assigns it, the SAME request is 200 — assignment is the thing that flips access.

Isolation + hygiene (CLAUDE.md): runs against SMOKE_BASE (the gate's isolated server,
SSC_DB_URL → a snapshot copy or ssc_test — NEVER live). Self-ensures its own schema
(ensure_pm_assignment_schema). Synthetic is_system=1 users (smk263-*) + synthetic projects
(SMK263-*) only; scoped cleanup in finally (children first — FK-safe on Postgres). PII-safe:
asserts on status codes / counts / booleans / synthetic codes — never a worker name, rate,
PIN, or *_path value.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import db_layer  # noqa: E402
from auth import hash_password  # noqa: E402
from apply_pm_assignment_263 import ensure_pm_assignment_schema  # noqa: E402

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5050")

PW = secrets.token_urlsafe(18)        # random per run; held in-process, never logged
USERS = {                              # role -> synthetic email (is_system=1)
    "admin":   "smk263-admin@superstars.local",
    "c_suite": "smk263-csuite@superstars.local",
    "pm1":     "smk263-pm1@superstars.local",
    "pm2":     "smk263-pm2@superstars.local",
}
ROLE_OF = {"admin": "admin", "c_suite": "c_suite", "pm1": "pm", "pm2": "pm"}
PROJ_A = "SMK263-A"
PROJ_B = "SMK263-B"

_failures = []


def ok(name, cond, note=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   — {note}" if note and not cond else ""))
    if not cond:
        _failures.append(name)
    return cond


# ============= SETUP / TEARDOWN (direct DB, isolated backend) =============

def _seed():
    conn = db_layer.connect(pragma_fk=True)
    try:
        ensure_pm_assignment_schema(conn)   # self-prepare the #263 schema on this backend
        # synthetic users (is_system=1, active, no forced reset)
        for key, email in USERS.items():
            role = ROLE_OF[key]
            row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE users SET password_hash=?, role=?, is_active=1, status='active', "
                    "must_reset_password=0, is_system=1 WHERE email=?",
                    (hash_password(PW), role, email))
            else:
                conn.execute(
                    "INSERT INTO users (email,password_hash,role,full_name,is_active,status,"
                    "must_reset_password,is_system) VALUES (?,?,?,?,1,'active',0,1)",
                    (email, hash_password(PW), role, f"SMK263 {key}"))
        # synthetic projects A + B (active)
        for code in (PROJ_A, PROJ_B):
            row = conn.execute("SELECT project_code FROM projects WHERE project_code=?", (code,)).fetchone()
            if row:
                conn.execute("UPDATE projects SET status='active' WHERE project_code=?", (code,))
            else:
                conn.execute("INSERT INTO projects (project_code, name, status) VALUES (?,?,'active')",
                             (code, f"Smoke Project {code[-1]}"))
        # start clean: no pre-existing assignments for our pms
        for key in ("pm1", "pm2"):
            uid = conn.execute("SELECT id FROM users WHERE email=?", (USERS[key],)).fetchone()[0]
            conn.execute("DELETE FROM pm_project_assignment WHERE user_id=?", (uid,))
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = db_layer.connect(pragma_fk=True)
    try:
        for email in USERS.values():
            u = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if u:
                uid = u[0]
                conn.execute("DELETE FROM pm_project_assignment WHERE user_id=? OR assigned_by=?", (uid, uid))
                conn.execute("DELETE FROM login_audit WHERE user_id=?", (uid,))
                conn.execute("DELETE FROM role_change_audit WHERE user_id=? OR changed_by=?", (uid, uid))
                conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
                # EVERY remaining users(id) child must go before the user, else FK fails on the
                # delete. The #263 assign/close actions write an audit_log row (actor_user_id);
                # dashboard_layouts / worker_rates are scoped to the synthetic uid (no real-data
                # rows match), so this only removes what these fixtures created.
                conn.execute("DELETE FROM audit_log WHERE actor_user_id=?", (uid,))
                conn.execute("DELETE FROM dashboard_layouts WHERE user_id=?", (uid,))
                conn.execute("DELETE FROM worker_rates WHERE created_by=?", (uid,))
                conn.execute("DELETE FROM users WHERE id=?", (uid,))
        for code in (PROJ_A, PROJ_B):
            conn.execute("DELETE FROM pm_project_assignment WHERE project_code=?", (code,))
            conn.execute("DELETE FROM projects WHERE project_code=?", (code,))
        conn.commit()
    finally:
        conn.close()


def _login(key):
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": USERS[key], "password": PW}, timeout=10)
    return s if (r.status_code == 200 and s.cookies.get("ssc_session")) else None


def _uid(key):
    """The user id for a synthetic user, read straight from the isolated DB. (The admin
    GET list hides is_system=1 fixtures, so we can't look our test pms up via the API.)"""
    conn = db_layer.connect(pragma_fk=True)
    try:
        return conn.execute("SELECT id FROM users WHERE email=?", (USERS[key],)).fetchone()[0]
    finally:
        conn.close()


def _sc(sess, method, path, **kw):
    """Status code of a request (no body inspection — PII-safe)."""
    return sess.request(method, f"{BASE}{path}", timeout=15, **kw).status_code


def _project_codes(sess):
    """The project_codes a session sees from /api/projects (scoped server-side)."""
    r = sess.get(f"{BASE}/api/projects", timeout=15)
    if r.status_code != 200:
        return None
    data = r.json().get("data") or []
    return {p.get("project_code") for p in data}


# ============= CHECKS =============

def run():
    admin = _login("admin")
    csuite = _login("c_suite")
    pm1 = _login("pm1")
    pm2 = _login("pm2")
    if not ok("logins", all([admin, csuite, pm1, pm2]), "could not log in synthetic users"):
        return

    # ---- fail->pass: assignment is what flips project access -------------------
    ok("pre_assign_pm1_cannot_open_A", _sc(pm1, "GET", f"/projects/{PROJ_A}") == 403,
       "before assignment, pm1 must NOT open project A")

    # ---- (a) assignment endpoints are admin/c_suite-only ----------------------
    ok("pm_cannot_list_assignments", _sc(pm1, "GET", "/api/admin/pm-assignments") == 403)
    ok("pm_cannot_set_assignments",
       _sc(pm1, "PUT", "/api/admin/pm-assignments", json={"user_id": 1, "project_codes": [PROJ_A]}) == 403)
    ok("pm_cannot_close_project",
       _sc(pm1, "POST", f"/api/projects/{PROJ_A}/status", json={"status": "closed"}) == 403)
    ok("csuite_can_list_assignments", _sc(csuite, "GET", "/api/admin/pm-assignments") == 200)

    # admin assigns A (only) to pm1 (id read from the isolated DB — our pm is is_system=1)
    pm1_id = _uid("pm1")
    r = admin.put(f"{BASE}/api/admin/pm-assignments",
                  json={"user_id": pm1_id, "project_codes": [PROJ_A]}, timeout=15)
    ok("admin_assigns_A_to_pm1", r.status_code == 200 and r.json()["data"]["project_codes"] == [PROJ_A])

    # ---- fail->pass continued: AFTER assignment the same request is 200 --------
    ok("post_assign_pm1_can_open_A", _sc(pm1, "GET", f"/projects/{PROJ_A}") == 200,
       "after assignment, pm1 opens project A (same URL that was 403)")

    # ---- role-aware sidebar nav (server-rendered per role) ---------------------
    pm_html = pm1.get(f"{BASE}/projects/{PROJ_A}", timeout=15).text
    cs_html = csuite.get(f"{BASE}/projects/{PROJ_A}", timeout=15).text
    ok("pm_nav_back_to_projects",
       ('<a href="/projects"' in pm_html) and ("Company Console &rarr;</a>" not in pm_html
        and "Company Console →</a>" not in pm_html),
       "pm served dashboard keeps the projects back-link + strips the company deep link")
    ok("pm_nav_no_financial_group", 'data-group="financial"' not in pm_html,
       "pm dashboard omits the Financial group (#262 still holds)")
    ok("pm_nav_keeps_employees", 'data-view="employees"' in pm_html,
       "pm keeps the project-scoped Employees view")
    ok("csuite_nav_back_to_company",
       ('<a href="/projects"' not in cs_html) and 'data-group="financial"' in cs_html,
       "company role keeps the company back-link + Financial group")

    # ---- (b) pm opens assigned, 403 on unassigned + company overview ----------
    ok("pm1_assigned_api_200", _sc(pm1, "GET", f"/api/projects/{PROJ_A}/workers") == 200)
    ok("pm1_assigned_docs_200", _sc(pm1, "GET", f"/api/projects/{PROJ_A}/documents") == 200,
       "pm reaches the project Documents surface on an assigned project")
    ok("pm1_unassigned_page_403", _sc(pm1, "GET", f"/projects/{PROJ_B}") == 403)
    ok("pm1_unassigned_api_403", _sc(pm1, "GET", f"/api/projects/{PROJ_B}/workers") == 403)
    ok("pm1_unassigned_dropplan_403", _sc(pm1, "GET", f"/api/dropplan/projects/{PROJ_B}/rollup") == 403,
       "the /api/dropplan/projects/<code>/ path is scoped too")
    ok("pm1_company_overview_page_403", _sc(pm1, "GET", "/") == 403)
    ok("pm1_company_summary_403", _sc(pm1, "GET", "/api/company/summary") == 403)

    # ---- list scoping: pm sees only assigned ACTIVE; pm2 empty; c_suite all ----
    pm1_codes = _project_codes(pm1)
    ok("pm1_list_has_A_not_B", pm1_codes is not None and PROJ_A in pm1_codes and PROJ_B not in pm1_codes,
       f"pm1 list scoped to assigned active only")
    pm2_codes = _project_codes(pm2)
    ok("pm2_list_empty", pm2_codes == set(), "pm2 has no assignments -> empty projects view")
    cs_codes = _project_codes(csuite)
    ok("csuite_list_has_both", cs_codes is not None and {PROJ_A, PROJ_B} <= cs_codes,
       "company role sees all projects")

    # ---- (d) pm 403 on Financial + company Workforce + Labor Rates -------------
    ok("pm1_financial_sov_403", _sc(pm1, "GET", f"/api/projects/{PROJ_A}/sov") == 403,
       "Financial is gated even on an ASSIGNED project (role axis, #262)")
    ok("pm1_company_workforce_403", _sc(pm1, "GET", "/api/workers/intake-summary") == 403)
    ok("pm1_labor_rates_403", _sc(pm1, "GET", "/api/labor-rates/workers") == 403)
    # company role NOT over-blocked
    ok("csuite_workforce_200", _sc(csuite, "GET", "/api/workers/intake-summary") == 200)
    ok("csuite_labor_rates_200", _sc(csuite, "GET", "/api/labor-rates/workers") == 200)

    # ---- (c) project-close lifecycle ------------------------------------------
    ok("csuite_closes_A", _sc(csuite, "POST", f"/api/projects/{PROJ_A}/status", json={"status": "closed"}) == 200)
    pm1_codes_after = _project_codes(pm1)
    ok("closed_A_off_pm_list", pm1_codes_after == set() or PROJ_A not in pm1_codes_after,
       "closed project drops off the assigned pm's active list")
    ok("closed_A_pm_cannot_open", _sc(pm1, "GET", f"/projects/{PROJ_A}") == 403,
       "assigned pm can no longer OPEN a closed project")
    cs_codes_closed = _project_codes(csuite)
    ok("closed_A_still_for_csuite", cs_codes_closed is not None and PROJ_A in cs_codes_closed,
       "admin/c_suite still see the closed project (records)")
    # reopen (admin/c_suite only) restores pm access
    ok("csuite_reopens_A", _sc(csuite, "POST", f"/api/projects/{PROJ_A}/status", json={"status": "active"}) == 200)
    pm1_codes_reopened = _project_codes(pm1)
    ok("reopened_A_back_on_pm_list", pm1_codes_reopened is not None and PROJ_A in pm1_codes_reopened,
       "reopened project returns to the assigned pm's list")

    # ---- pm2 (no assignments): no project access, company overview 403 --------
    ok("pm2_cannot_open_A", _sc(pm2, "GET", f"/projects/{PROJ_A}") == 403)
    ok("pm2_company_overview_403", _sc(pm2, "GET", "/") == 403)


def main():
    print(f"== #263 PM project-scoping guard ==  BASE={BASE}")
    db_url = (os.environ.get("SSC_DB_URL") or "").strip()
    backend = "postgres" if db_layer.is_postgres() else "sqlite"
    print(f"   backend={backend}  SSC_DB_URL={'(set)' if db_url else '(unset=LIVE — refuse)'}")
    if not db_url and "localhost" not in BASE and "127.0.0.1" in BASE and os.environ.get("ALLOW_LIVE") != "1":
        # Defensive: the gate always sets SSC_DB_URL to an isolated backend. Refuse to
        # seed synthetic rows into the live DB if someone runs this bare.
        print("   REFUSING: SSC_DB_URL unset (would seed LIVE). Set an isolated backend.")
        return 2
    _seed()
    try:
        run()
    finally:
        _cleanup()
    n = len(_failures)
    print(f"\n== {'ALL PASS' if n == 0 else str(n) + ' FAILED: ' + ', '.join(_failures)} ==")
    return 0 if n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
