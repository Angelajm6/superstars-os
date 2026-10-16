"""smoke_auth_roles.py — multi-user accounts & roles authz guard (#257).

Drives the REAL HTTP flow against the running server with synthetic-only users
(emails smk257-*@example.invalid; never a real colleague). Asserts the
load-bearing authorization rules — and FAILS on the pre-#257 server so the gate
proves the fix:

  (a) pm CANNOT reach the individual-rates endpoints (403); c_suite CAN.
  (b) a role change takes effect SERVER-SIDE on the user's NEXT request (no
      re-login, no cached session role).
  (c) admin is never CREATED or reached through the generic role route (Make admin
      owns promotion — see smoke_make_admin.py), and an admin cannot downgrade/
      deactivate THEMSELVES (so the last admin can never be removed via self-action).
  (d) deactivation blocks login AND kills the in-flight session immediately.
  (e) must_reset_password forces the set-password screen before ANY data loads
      (a crafted API call from a must-reset session is 403, not served).
  (f) login_audit captures login_success / login_fail / password_set.

PII: synthetic emails + a throwaway temp/known password; NEVER logs a real
password/hash/temp-password (only booleans + status codes). Scoped cleanup
removes ONLY smk257-* rows + their sessions/audit. Real users untouched.

Run (server up):  python tests/smoke_auth_roles.py
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import sys
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
DB = SCRIPT_DIR / "superstars.db"
BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5050")
from auth import hash_password  # noqa: E402
import db_layer  # noqa: E402  # #259 — route direct DB access through the env-driven layer

PASS, FAIL = 0, 0
ADMIN_EMAIL = "smoke@superstars.local"          # the gate's standing test admin (fixture)
PM_EMAIL = "smk257-pm@example.invalid"
CS_EMAIL = "smk257-cs@example.invalid"
NEW_EMAIL = "smk257-new@example.invalid"        # created via the admin API (must_reset flow)
# #258 — RANDOM per run (no standing backdoor); held in-process for this run only,
# never logged. NEW_PW is the password set via the legit set-password API, kept
# strong (>=12, letter+digit) but non-constant.
ADMIN_PW = secrets.token_urlsafe(18)
SEED_PW = secrets.token_urlsafe(18)
NEW_PW = "Smk257" + secrets.token_urlsafe(8) + "9z"


def jbody(resp):
    """Parse JSON defensively — a pre-build 404/405 has no JSON body."""
    try:
        return resp.json() or {}
    except Exception:
        return {}


def check(label, ok, note=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" — {note}" if note else ""))
    return bool(ok)


def _conn():
    # #259 — honors SSC_DB_URL (SQLite default, or the Postgres test DB).
    return db_layer.connect()


def ensure_admin():
    """Seed/repair the standing smoke admin so we can act as admin (idempotent)."""
    c = _conn()
    try:
        row = c.execute("SELECT id FROM users WHERE LOWER(email)=?", (ADMIN_EMAIL,)).fetchone()
        if row:
            c.execute("UPDATE users SET password_hash=?, role='admin', is_active=1, status='active', "
                      "must_reset_password=0, is_system=1 WHERE id=?", (hash_password(ADMIN_PW), row[0]))
        else:
            c.execute("INSERT INTO users(email,password_hash,role,full_name,display_name,is_active,status,must_reset_password,is_system) "
                      "VALUES(?,?,'admin','Smoke Admin','Smoke Admin',1,'active',0,1)",
                      (ADMIN_EMAIL, hash_password(ADMIN_PW)))
        c.commit()
        return c.execute("SELECT id FROM users WHERE LOWER(email)=?", (ADMIN_EMAIL,)).fetchone()[0]
    finally:
        c.close()


def seed_user(email, role, must_reset=0, status="active"):
    c = _conn()
    try:
        c.execute("DELETE FROM users WHERE email=?", (email,))
        c.execute("INSERT INTO users(email,password_hash,role,full_name,display_name,is_active,status,must_reset_password,is_system) "
                  "VALUES(?,?,?,?,?,1,?,?,1)",
                  (email, hash_password(SEED_PW), role, "SMK 257 " + role, "SMK-257-" + role, status, must_reset))
        c.commit()
        return c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    finally:
        c.close()


def login(email, pw):
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": email, "password": pw}, timeout=15)
    return s, r


def cleanup():
    c = _conn()
    try:
        ids = [r[0] for r in c.execute("SELECT id FROM users WHERE email LIKE 'smk257-%'").fetchall()]
        for uid in ids:
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            c.execute("DELETE FROM login_audit WHERE user_id=?", (uid,))
            c.execute("DELETE FROM role_change_audit WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE email LIKE 'smk257-%'")
        c.commit()
        return len(ids)
    finally:
        c.close()


def main() -> int:
    admin_id = ensure_admin()
    cleanup()
    # SQL-seeded synthetic users so the rate-authz tests run on BOTH the pre- and
    # post-#257 server (the discriminators). API-driven tests follow.
    pm_id = seed_user(PM_EMAIL, "pm")
    cs_id = seed_user(CS_EMAIL, "c_suite")
    try:
        admin, ar = login(ADMIN_EMAIL, ADMIN_PW)
        check("admin can log in", ar.status_code == 200, f"{ar.status_code}")

        # ---------- (a) pm BLOCKED from individual rates; c_suite ALLOWED ----------
        print("\n== (a) rates authz: pm blocked, c_suite allowed ==")
        pm, _ = login(PM_EMAIL, SEED_PW)
        cs, _ = login(CS_EMAIL, SEED_PW)
        r_pm_pending = pm.get(f"{BASE}/api/labor-rates/pending", timeout=15)
        check("pm -> /api/labor-rates/pending is 403 (individual rates)", r_pm_pending.status_code == 403,
              f"got {r_pm_pending.status_code} (pre-#257 pm was allowed)")
        r_pm_roster = pm.get(f"{BASE}/api/labor-rates/roster", timeout=15)
        check("pm -> /api/labor-rates/roster is 403", r_pm_roster.status_code == 403, f"got {r_pm_roster.status_code}")
        r_cs_roster = cs.get(f"{BASE}/api/labor-rates/roster", timeout=15)
        check("c_suite -> /api/labor-rates/roster is 200 (rates visible)", r_cs_roster.status_code == 200,
              f"got {r_cs_roster.status_code}")
        r_pm_page = pm.get(f"{BASE}/admin/labor-rates", timeout=15)
        check("pm -> /admin/labor-rates page is 403 (not just hidden)", r_pm_page.status_code == 403,
              f"got {r_pm_page.status_code}")

        # ---------- (b) role change takes effect on the NEXT request ----------
        print("\n== (b) role change instant (server-side, next request) ==")
        up = admin.post(f"{BASE}/api/admin/users/{pm_id}/role", json={"new_role": "c_suite", "reason": "smk257 upgrade"}, timeout=15)
        check("admin upgrade pm->c_suite returns 200", up.status_code == 200, f"got {up.status_code}")
        # SAME pm session — no re-login — now resolves as c_suite and gains rate access:
        r_after_up = pm.get(f"{BASE}/api/labor-rates/roster", timeout=15)
        check("upgraded user gains rate access on next request (same session)", r_after_up.status_code == 200,
              f"got {r_after_up.status_code}")
        down = admin.post(f"{BASE}/api/admin/users/{pm_id}/role", json={"new_role": "pm", "reason": "smk257 downgrade"}, timeout=15)
        check("admin downgrade c_suite->pm returns 200", down.status_code == 200, f"got {down.status_code}")
        r_after_down = pm.get(f"{BASE}/api/labor-rates/roster", timeout=15)
        check("downgraded user loses rate access on next request", r_after_down.status_code == 403,
              f"got {r_after_down.status_code}")
        # audit rows present
        c = _conn()
        nrole = c.execute("SELECT COUNT(1) FROM role_change_audit WHERE user_id=?", (pm_id,)).fetchone()[0]
        c.close()
        check("role_change_audit recorded both changes (>=2)", nrole >= 2, f"{nrole}")

        # ---------- (c) admin guards ----------
        print("\n== (c) admin guards ==")
        c_admin = admin.post(f"{BASE}/api/admin/users",
                             json={"display_name": "SMK 257 Admin2", "email": "smk257-admin2@example.invalid", "role": "admin"}, timeout=15)
        check("create an admin directly REFUSED (403)", c_admin.status_code == 403, f"got {c_admin.status_code}")
        el = admin.post(f"{BASE}/api/admin/users/{pm_id}/role", json={"new_role": "admin"}, timeout=15)
        check("generic role route to admin REFUSED (403)", el.status_code == 403, f"got {el.status_code}")
        self_down = admin.post(f"{BASE}/api/admin/users/{admin_id}/role", json={"new_role": "pm"}, timeout=15)
        check("admin cannot downgrade THEMSELVES (403)", self_down.status_code == 403, f"got {self_down.status_code}")
        self_deact = admin.post(f"{BASE}/api/admin/users/{admin_id}/deactivate", json={}, timeout=15)
        check("admin cannot deactivate THEMSELVES (403)", self_deact.status_code == 403, f"got {self_deact.status_code}")
        # the acting admin's row is untouched by all of the above
        c = _conn()
        still_admin = c.execute("SELECT role, status FROM users WHERE id=?", (admin_id,)).fetchone()
        c.close()
        check("acting admin still admin+active after invariant attempts",
              bool(still_admin) and still_admin["role"] == "admin" and still_admin["status"] == "active",
              f"{(still_admin['role'], still_admin['status']) if still_admin else None}")

        # ---------- (d) deactivation blocks login + kills the session ----------
        print("\n== (d) deactivation kills session + blocks login ==")
        pm2, _ = login(PM_EMAIL, SEED_PW)
        check("pm session live before deactivation", pm2.get(f"{BASE}/api/auth/me", timeout=15).status_code == 200)
        da = admin.post(f"{BASE}/api/admin/users/{pm_id}/deactivate", json={}, timeout=15)
        check("admin deactivate pm returns 200", da.status_code == 200, f"got {da.status_code}")
        me_after = pm2.get(f"{BASE}/api/auth/me", timeout=15)
        check("in-flight pm session is killed on next request (401)", me_after.status_code == 401, f"got {me_after.status_code}")
        _, relog = login(PM_EMAIL, SEED_PW)
        check("deactivated pm cannot log in (401)", relog.status_code == 401, f"got {relog.status_code}")
        ra = admin.post(f"{BASE}/api/admin/users/{pm_id}/reactivate", json={}, timeout=15)
        check("admin reactivate pm returns 200", ra.status_code == 200, f"got {ra.status_code}")
        _, relog2 = login(PM_EMAIL, SEED_PW)
        check("reactivated pm can log in again (200)", relog2.status_code == 200, f"got {relog2.status_code}")

        # ---------- (e) must_reset forces reset before data ----------
        print("\n== (e) forced first-login reset before any data ==")
        cu = admin.post(f"{BASE}/api/admin/users",
                        json={"display_name": "SMK 257 New", "email": NEW_EMAIL, "role": "pm"}, timeout=15)
        check("admin create user returns 201 + temp password", cu.status_code == 201 and bool(jbody(cu).get("temp_password")),
              f"got {cu.status_code}")
        temp_pw = jbody(cu).get("temp_password") if cu.status_code == 201 else None
        check("created user flagged must_reset_password", bool((jbody(cu).get("data") or {}).get("must_reset_password")))
        if temp_pw:
            nu, nlog = login(NEW_EMAIL, temp_pw)
            check("new user can log in with temp password", nlog.status_code == 200, f"got {nlog.status_code}")
            check("login response signals must_reset_password", bool((jbody(nlog).get("user") or {}).get("must_reset_password")))
            # #263 — the created user is a pm scoped to ASSIGNED projects, so probe a
            # pm-accessible endpoint (/api/projects, their scoped list) — an unassigned
            # project endpoint would 403 on the assignment axis, not the must_reset gate.
            blocked = nu.get(f"{BASE}/api/projects", timeout=15)
            check("must_reset session BLOCKED from data (403 must_reset, not served)",
                  blocked.status_code == 403 and bool(jbody(blocked).get("must_reset")), f"got {blocked.status_code}")
            sp = nu.post(f"{BASE}/api/auth/set-password", json={"new_password": NEW_PW}, timeout=15)
            check("set-password succeeds (200)", sp.status_code == 200, f"got {sp.status_code}")
            after = nu.get(f"{BASE}/api/projects", timeout=15)
            check("data loads after password set (200)", after.status_code == 200, f"got {after.status_code}")
            # weak password rejected (strength rule)
            cu2 = admin.post(f"{BASE}/api/admin/users",
                             json={"display_name": "SMK 257 Weak", "email": "smk257-weak@example.invalid", "role": "pm"}, timeout=15)
            tw = jbody(cu2).get("temp_password") if cu2.status_code == 201 else None
            if tw:
                wsess, _ = login("smk257-weak@example.invalid", tw)
                wr = wsess.post(f"{BASE}/api/auth/set-password", json={"new_password": "short1"}, timeout=15)
                check("weak password rejected (400)", wr.status_code == 400, f"got {wr.status_code}")

        # ---------- (e2) #268: CLIENT onboardable via the Create-user UI -> welcome hard-stop ----------
        print("\n== (e2) #268 create a CLIENT via the admin UI -> forced reset -> welcome ==")
        # fail->pass: the Create-user dropdown must OFFER the client role (the UI surface)
        admin_page = (Path(__file__).resolve().parent.parent / "admin_users.html").read_text(encoding="utf-8", errors="replace")
        check("(e2) create-user dropdown offers the client role",
              'value="client"' in admin_page, "admin_users.html Create-user <select> must list client")
        CLIENT_EMAIL = "smk257-client@example.invalid"   # smk257-% => cleaned up by cleanup()
        cc = admin.post(f"{BASE}/api/admin/users",
                        json={"display_name": "SMK 268 Client", "email": CLIENT_EMAIL, "role": "client"}, timeout=15)
        check("(e2) admin creates a CLIENT (201 + temp password)",
              cc.status_code == 201 and bool(jbody(cc).get("temp_password")), f"got {cc.status_code}")
        cdata = jbody(cc).get("data") or {}
        check("(e2) created user role=client + must_reset",
              cdata.get("role") == "client" and bool(cdata.get("must_reset_password")),
              f"role={cdata.get('role')} must_reset={cdata.get('must_reset_password')}")
        cpw = jbody(cc).get("temp_password") if cc.status_code == 201 else None
        if cpw:
            csess, clog = login(CLIENT_EMAIL, cpw)
            check("(e2) client logs in with temp password (200)", clog.status_code == 200, f"got {clog.status_code}")
            check("(e2) login signals must_reset", bool((jbody(clog).get("user") or {}).get("must_reset_password")))
            sp = csess.post(f"{BASE}/api/auth/set-password", json={"new_password": NEW_PW}, timeout=15)
            check("(e2) client set-password succeeds (200)", sp.status_code == 200, f"got {sp.status_code}")
            # #267 — after reset the client is CONTAINED: /welcome serves; every other page -> /welcome; API -> 403
            w = csess.get(f"{BASE}/welcome", timeout=15)
            check("(e2) client /welcome serves (200)", w.status_code == 200, f"got {w.status_code}")
            portal = csess.get(f"{BASE}/portal", timeout=15, allow_redirects=False)
            check("(e2) client page -> redirect to /welcome (contained)",
                  portal.status_code in (301, 302, 303, 307, 308)
                  and portal.headers.get("Location", "").rstrip("/").endswith("/welcome"),
                  f"got {portal.status_code} {portal.headers.get('Location')}")
            api = csess.get(f"{BASE}/api/portal/project", timeout=15)
            check("(e2) client API -> 403 (contained)", api.status_code == 403, f"got {api.status_code}")

        # ---------- (f) login_audit captured ----------
        print("\n== (f) login_audit captured ==")
        # a deliberate bad login -> login_fail
        login(PM_EMAIL, "wrong-password-xyz")
        c = _conn()
        try:
            ev = {r[0] for r in c.execute(
                "SELECT DISTINCT event FROM login_audit WHERE user_id IN "
                "(SELECT id FROM users WHERE email LIKE 'smk257-%')").fetchall()}
        finally:
            c.close()
        check("login_audit has login_success", "login_success" in ev, f"{sorted(ev)}")
        check("login_audit has login_fail", "login_fail" in ev, f"{sorted(ev)}")
        check("login_audit has password_set", "password_set" in ev, f"{sorted(ev)}")

        # ---------- (g) #258: is_system fixtures hidden + invariant counts real admins ----------
        print("\n== (g) #258 is_system: console hides fixtures + invariant counts real admins ==")
        # (d) the smoke fixture carries is_system=1
        c = _conn()
        try:
            smoke_sys = c.execute("SELECT is_system FROM users WHERE LOWER(email)=?", (ADMIN_EMAIL,)).fetchone()
        finally:
            c.close()
        check("(d) smoke fixture carries is_system=1", bool(smoke_sys) and smoke_sys[0] == 1, f"{smoke_sys}")
        # seed a synthetic is_system=0 'real-looking' user to prove the filter keys on the
        # FLAG (not on email guessing): it MUST appear; the is_system=1 fixture must NOT.
        realish = "smk257-realish@example.invalid"
        c = _conn()
        try:
            c.execute("DELETE FROM users WHERE email=?", (realish,))
            c.execute("INSERT INTO users(email,password_hash,role,full_name,display_name,is_active,status,is_system) "
                      "VALUES(?,?, 'pm','SMK Realish','SMK Realish',1,'active',0)", (realish, hash_password(SEED_PW)))
            c.commit()
        finally:
            c.close()
        listing = jbody(admin.get(f"{BASE}/api/admin/users", timeout=15)).get("data") or []
        emails = {(u.get("email") or "").lower() for u in listing}
        check("(a) admin list HIDES the is_system fixture", ADMIN_EMAIL not in emails,
              f"fixture present={ADMIN_EMAIL in emails}")
        check("(a) admin list SHOWS an is_system=0 account (keys on flag, not email)", realish in emails)
        # (c) invariant counts ONLY real (is_system=0) admins
        import auth_admin  # noqa: E402
        c = _conn()
        try:
            real_admins = c.execute("SELECT COUNT(1) FROM users WHERE role='admin' AND is_system=0 "
                                    "AND status='active' AND is_active=1").fetchone()[0]
            all_admins = c.execute("SELECT COUNT(1) FROM users WHERE role='admin' "
                                   "AND status='active' AND is_active=1").fetchone()[0]
            counted = auth_admin._active_admin_count(c)
        finally:
            c.close()
        check("(c) _active_admin_count counts ONLY real admins (fixture excluded)",
              counted == real_admins and all_admins > real_admins,
              f"counted={counted} real={real_admins} all={all_admins}")

    finally:
        # scoped cleanup — ONLY smk257-* rows; real users (incl. the real admin) untouched
        n = cleanup()
        c = _conn()
        try:
            leftover = c.execute("SELECT COUNT(1) FROM users WHERE email LIKE 'smk257-%'").fetchone()[0]
            admins = c.execute("SELECT COUNT(1) FROM users WHERE role='admin'").fetchone()[0]
        finally:
            c.close()
        print(f"\n  cleanup: removed {n} synthetic user(s); smk257 leftover={leftover}; admin rows={admins}")
        check("cleanup: 0 synthetic leftover", leftover == 0)
        check("cleanup: admin rows intact (real + fixture)", admins >= 1, f"{admins}")

    print(f"\n=== auth-roles guard: {PASS} passed, {FAIL} failed ===")
    print("OVERALL:", "PASS" if FAIL == 0 else "FAIL")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
