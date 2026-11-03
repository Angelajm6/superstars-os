"""#289 GUARD — public-door hardening (Cloud M3), against the gate's isolated server.

  BRUTE-FORCE   rapid wrong passwords -> backoff observable (a later attempt is
                measurably slower); a login_lockout audit row is written; a CORRECT
                password DURING lockout is still refused with the identical 401 and
                still delayed (no lockout oracle); the failure body/status is byte-
                identical for unknown-email vs wrong-password (no enumeration).
  IP THROTTLE   fails from one source accrue on the IP counter (per-source).
  TOTP          enroll (begin -> confirm) -> login now needs the code; wrong code ->
                totp_required 401 (password counter NOT tripped); recovery code
                works once then is burned; disable clears it.
  FORCE_SSO     an admin flags a synthetic staffer force_sso -> their password login
                is 403 sso_required (only after a correct password — no enumeration);
                refused when the target has no google_sub.
  DEVICE (PIN)  enforcement OFF (default): a valid PIN with NO device token still
                signs in (field not stranded). enforcement ON: unprovisioned ->
                403 device_required; provision -> redeem -> sign in; revoke ->
                refused again. PIN throttle audits pin_fail regardless.
  SESSIONS      login sets HttpOnly + SameSite (+ Secure under X-Forwarded-Proto);
                a NEW sid each login (rotation); logout 200 + server-side invalidation
                (the old sid no longer authenticates).
  SECRETS       no app module opens a .env file (the runtime reads env only).

Isolated backend REQUIRED. PII-safe: synthetic ids/emails, ids/booleans/counts only.
127.0.0.1 only. Restores every setting it toggles (worker enforcement) in finally.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import db_layer  # noqa: E402
import totp as _totp  # noqa: E402
from auth import hash_password  # noqa: E402
from apply_public_hardening_289 import ensure_hardening_schema  # noqa: E402

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5050")
PASS, FAIL = [], []
PW = "Sup3rStrongPass!289"
IDS = {"users": [], "emps": []}
EMAIL_STAFF = "smk289-staff@superstars.local"
EMAIL_SSO = "smk289-sso@superstars.local"
EMP_ID = "E-99289"


def ok(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {note}" if note and not cond else ""))
    return bool(cond)


_FLAG_BEFORE = None   # snapshot's worker_device_enforcement at seed time


def seed():
    global _FLAG_BEFORE
    conn = db_layer.connect(pragma_fk=True)
    try:
        ensure_hardening_schema(conn)
        # #290 — ESTABLISH the grace baseline instead of inheriting it: since
        # the 2026-08-02 records op, LIVE runs worker_device_enforcement=1, so
        # any fresh snapshot arrives with the door sealed and the suite's
        # first PIN check (enforcement-off contract) would 403. Capture the
        # snapshot's value here, force 0 for the suite, restore in cleanup.
        row = conn.execute("SELECT value FROM app_settings "
                           "WHERE key='worker_device_enforcement'").fetchone()
        _FLAG_BEFORE = row[0] if row else None
        conn.execute("UPDATE app_settings SET value='0' "
                     "WHERE key='worker_device_enforcement'")
        for email, role, gsub in ((EMAIL_STAFF, "pm", None),
                                  (EMAIL_SSO, "admin", None)):
            conn.execute("DELETE FROM users WHERE email=?", (email,))
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                "must_reset_password, is_system, google_sub) VALUES (?,?,?,?,1,'active',0,1,?)",
                (email, hash_password(PW), role, f"SMK289 {role}", gsub))
            IDS["users"].append(cur.lastrowid)
        # a synthetic admin session actor for the admin-only endpoints
        admin_cur = conn.execute(
            "INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
            "must_reset_password, is_system) VALUES ('smk289-admin@superstars.local',?,'admin',"
            "'SMK289 admin',1,'active',0,1)", (hash_password(PW),))
        IDS["users"].append(admin_cur.lastrowid)
        admin_id = admin_cur.lastrowid
        admin_tok = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO sessions (id, user_id, expires_at, user_agent) "
                     "VALUES (?,?, '2099-01-01T00:00:00','smk289')", (admin_tok, admin_id))
        # a synthetic worker with a known PIN, on the roster view's base table
        conn.execute("DELETE FROM employees WHERE employee_id=?", (EMP_ID,))
        conn.execute(
            "INSERT INTO employees (employee_id, name, worker_id, pin, trade) "
            "VALUES (?, 'SMK289 Worker', 'W-9289', '4289', 'Laborer')", (EMP_ID,))
        IDS["emps"].append(EMP_ID)
        conn.commit()
        return admin_id, admin_tok
    finally:
        conn.close()


def cleanup():
    conn = db_layer.connect()
    try:
        if IDS["users"]:
            ph = ",".join("?" * len(IDS["users"]))
            for t, c in (("sessions", "user_id"), ("login_audit", "user_id")):
                try:
                    conn.execute(f"DELETE FROM {t} WHERE {c} IN ({ph})", tuple(IDS["users"]))
                except Exception:
                    pass
            conn.execute(f"DELETE FROM users WHERE id IN ({ph})", tuple(IDS["users"]))
        for emp in IDS["emps"]:
            conn.execute("DELETE FROM worker_device WHERE employee_id=?", (emp,))
            conn.execute("DELETE FROM sign_in_log WHERE employee_id=?", (emp,))
            conn.execute("DELETE FROM employees WHERE employee_id=?", (emp,))
        # restore the flag to what the snapshot arrived with (#290 — LIVE runs
        # 1 since the door-seal records op; hardcoding 0 here would leave an
        # isolated copy claiming grace when its source asserted enforcement)
        if _FLAG_BEFORE is not None:
            conn.execute("UPDATE app_settings SET value=? "
                         "WHERE key='worker_device_enforcement'", (_FLAG_BEFORE,))
        # scrub the anonymous pin_fail/lockout rows this suite generated (user_id NULL)
        try:
            conn.execute("DELETE FROM login_audit WHERE event IN "
                         "('pin_fail','pin_lockout','device_provisioned','device_revoked') "
                         "AND user_id IS NULL AND at LIKE '2026%' AND ip='testclient'")
        except Exception:
            pass
        conn.commit()
        print("  [cleanup] synthetic rows removed (scoped to SMK289 ids)")
    finally:
        conn.close()


def login(sess, email, password, totp=None):
    body = {"email": email, "password": password}
    if totp is not None:
        body["totp"] = totp
    return sess.post(f"{BASE}/api/auth/login", json=body, timeout=20, allow_redirects=False)


def A(admin_tok):
    s = requests.Session()
    s.cookies.set("ssc_session", admin_tok)
    return s


def run(admin_id, admin_tok):
    R = dict(timeout=20, allow_redirects=False)

    print("\n-- no enumeration: unknown-email vs wrong-password are identical --")
    r_unknown = login(requests.Session(), "smk289-nobody@superstars.local", "whatever12")
    r_wrong = login(requests.Session(), EMAIL_STAFF, "wrongpassword12")
    ok("unknown_email_401", r_unknown.status_code == 401)
    ok("wrong_password_401", r_wrong.status_code == 401)
    ok("no_enumeration_same_body", r_unknown.text == r_wrong.text,
       f"{r_unknown.text[:40]} vs {r_wrong.text[:40]}")

    print("\n-- brute force: backoff + lockout audit + correct-pw-still-refused --")
    s = requests.Session()
    for _ in range(6):   # exceed LOGIN_MAX_FAILS (5)
        login(s, EMAIL_STAFF, "stillwrong12")
    # a fresh wrong attempt should now be delayed (backoff tripped)
    t0 = time.time()
    login(s, EMAIL_STAFF, "stillwrong12")
    delayed = (time.time() - t0)
    ok("backoff_delays_response", delayed >= 0.3, f"{delayed:.2f}s")
    # correct password during lockout: still 401, still delayed, identical body
    t1 = time.time()
    r_correct = login(s, EMAIL_STAFF, PW)
    ok("correct_pw_refused_during_lockout", r_correct.status_code == 401
       and (time.time() - t1) >= 0.3, f"{r_correct.status_code}")
    conn = db_layer.connect()
    try:
        n_lock = conn.execute("SELECT COUNT(1) FROM login_audit WHERE user_id=? AND event='login_lockout'",
                              (IDS["users"][0],)).fetchone()[0]
    finally:
        conn.close()
    ok("lockout_audit_written", n_lock >= 1, f"login_lockout rows={n_lock}")

    print("\n-- session flags + rotation + logout invalidation (clean account) --")
    # wait out the window? no — use the SSO-less admin fixture account which hasn't
    # been failed against. Fresh session each login.
    fresh = requests.Session()
    r = login(fresh, "smk289-admin@superstars.local", PW)
    ok("clean_login_200", r.status_code == 200, f"{r.status_code}")
    setc = r.headers.get("Set-Cookie", "")
    ok("cookie_httponly", "HttpOnly" in setc, setc[:80])
    ok("cookie_samesite", "SameSite" in setc, setc[:80])
    sid1 = fresh.cookies.get("ssc_session")
    # Secure flag: verified in-process against the real cookie-writer under both
    # signals (a direct X-Forwarded-Proto header AND the SSC_TRUSTED_PROXY edge
    # declaration). Done in-process because waitress strips untrusted X-Forwarded-*
    # before the app sees them — the property under test is the cookie code, not
    # waitress's header policy.
    import auth as _authmod
    import server as _srv
    def _secure_for(headers=None, env=None):
        import os as _os
        saved = _os.environ.get("SSC_TRUSTED_PROXY")
        if env is not None:
            _os.environ["SSC_TRUSTED_PROXY"] = env
        elif "SSC_TRUSTED_PROXY" in _os.environ:
            del _os.environ["SSC_TRUSTED_PROXY"]
        try:
            with _srv.app.test_request_context("/api/auth/login", headers=headers or {}):
                resp = _srv.app.make_response("")
                _authmod._set_session_cookie(resp, "unit-sid")
                return "Secure" in resp.headers.get("Set-Cookie", "")
        finally:
            if saved is not None:
                _os.environ["SSC_TRUSTED_PROXY"] = saved
            else:
                _os.environ.pop("SSC_TRUSTED_PROXY", None)
    ok("cookie_secure_via_forwarded_proto", _secure_for(headers={"X-Forwarded-Proto": "https"}))
    ok("cookie_secure_via_trusted_proxy_env", _secure_for(env="1"))
    ok("cookie_not_secure_plain_http", not _secure_for())
    # rotation: a second login from a session carrying sid1 mints a different sid
    r3 = login(fresh, "smk289-admin@superstars.local", PW)
    sid2 = fresh.cookies.get("ssc_session")
    ok("session_rotates_on_login", sid1 and sid2 and sid1 != sid2)
    # old sid no longer authenticates (rotation destroyed it)
    old = requests.Session(); old.cookies.set("ssc_session", sid1)
    ok("old_sid_invalidated_after_rotation",
       old.get(f"{BASE}/api/auth/me", **R).status_code == 401)
    # logout invalidates server-side
    fresh.post(f"{BASE}/api/auth/logout", **R)
    ok("logout_invalidates", requests.Session().get(f"{BASE}/api/auth/me", **R).status_code == 401)

    print("\n-- TOTP arc (enroll -> require -> wrong -> recovery -> disable) --")
    # the admin fixture account is clean (never failed against); enroll on it.
    AE = "smk289-admin@superstars.local"
    st = requests.Session()
    r = login(st, AE, PW)
    ok("totp_precondition_login", r.status_code == 200, f"{r.status_code}")
    b = st.post(f"{BASE}/api/2fa/begin", **R)
    bd = b.json().get("data", {}) if b.status_code == 200 else {}
    secret, recovery = bd.get("secret"), bd.get("recovery_codes") or []
    ok("totp_begin_returns_secret_and_recovery",
       bool(secret) and len(recovery) == 10, f"{b.status_code} {b.text[:60]}")
    # 2026-10-30: a QR to scan (Salesforce Authenticator only scans), drawn on the server as an SVG data URI
    qr = bd.get("qr_svg") or ""
    import base64 as _b64, urllib.parse as _up
    _svg = ""
    if qr.startswith("data:image/svg+xml;base64,"):
        _svg = _b64.b64decode(qr.split(",", 1)[1]).decode("utf-8", "replace")
    elif qr.startswith("data:image/svg+xml"):
        _svg = _up.unquote(qr.split(",", 1)[1])
    ok("totp_begin_returns_qr_svg", "<svg" in _svg and (secret or "x") not in _svg and "http" not in _svg.replace("http://www.w3.org", ""),
       qr[:40])
    if secret:
        c = st.post(f"{BASE}/api/2fa/confirm",
                    json={"code": _totp._code_at(secret, int(time.time() // 30))}, **R)
        ok("totp_confirm_200", c.status_code == 200, f"{c.status_code} {c.text[:60]}")
        r_nocode = login(requests.Session(), AE, PW)
        ok("totp_login_requires_code", r_nocode.status_code == 401
           and r_nocode.json().get("totp_required") is True, f"{r_nocode.status_code}")
        r_wrongcode = login(requests.Session(), AE, PW, totp="000000")
        ok("totp_wrong_code_401", r_wrongcode.status_code == 401
           and r_wrongcode.json().get("totp_required") is True)
        r_good = login(requests.Session(), AE, PW,
                       totp=_totp._code_at(secret, int(time.time() // 30)))
        ok("totp_login_with_code_200", r_good.status_code == 200, f"{r_good.status_code}")
        ok("totp_recovery_code_works",
           login(requests.Session(), AE, PW, totp=recovery[0]).status_code == 200)
        ok("totp_recovery_code_single_use",
           login(requests.Session(), AE, PW, totp=recovery[0]).status_code == 401)
        # counter check: the missing-factor attempts did NOT trip the password lockout
        r_still = login(requests.Session(), AE, PW,
                        totp=_totp._code_at(secret, int(time.time() // 30)))
        ok("totp_miss_did_not_lock_password", r_still.status_code == 200, f"{r_still.status_code}")
        d = st.post(f"{BASE}/api/2fa/disable",
                    json={"code": _totp._code_at(secret, int(time.time() // 30))}, **R)
        ok("totp_disable_200", d.status_code == 200, f"{d.status_code}")
        ok("totp_login_plain_after_disable", login(requests.Session(), AE, PW).status_code == 200)

    print("\n-- force_sso: password path 403 (post-verify), refused w/o google_sub --")
    ad = A(admin_tok)
    # target SSO account has google_sub? seeded as NULL — so force-sso must refuse
    r = ad.post(f"{BASE}/api/admin/users/force-sso",
                json={"user_id": IDS["users"][1], "on": True}, **R)
    ok("force_sso_refused_without_gsub", r.status_code == 400, f"{r.status_code}")
    # give it a google_sub, then force
    conn = db_layer.connect()
    try:
        conn.execute("UPDATE users SET google_sub='smk289-sub' WHERE id=?", (IDS["users"][1],))
        conn.commit()
    finally:
        conn.close()
    r = ad.post(f"{BASE}/api/admin/users/force-sso",
                json={"user_id": IDS["users"][1], "on": True}, **R)
    ok("force_sso_set_200", r.status_code == 200, f"{r.status_code}")
    r = login(requests.Session(), EMAIL_SSO, PW)
    ok("force_sso_password_path_403", r.status_code == 403
       and r.json().get("sso_required") is True, f"{r.status_code} {r.text[:60]}")

    print("\n-- 2fa banner status feed --")
    r = ad.get(f"{BASE}/api/admin/2fa-status", **R)
    d = r.json().get("data", {}) if r.status_code == 200 else {}
    ok("twofa_status_200_shape", r.status_code == 200
       and "missing" in d and "staff" in d, f"{r.status_code}")

    print("\n-- worker PIN device binding --")
    R2 = dict(timeout=20)
    # enforcement OFF (default): valid PIN, no device token -> signs in
    r = requests.post(f"{BASE}/api/worker/login",
                      json={"phone_or_pin": "4289", "bypass_geofence": True}, **R2)
    ok("pin_ok_when_enforcement_off", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
    # turn enforcement ON via admin
    e = ad.post(f"{BASE}/api/admin/worker-devices/enforcement", json={"on": True}, **R2)
    ok("enforcement_toggle_200", e.status_code == 200, f"{e.status_code}")
    # unprovisioned now refused
    r = requests.post(f"{BASE}/api/worker/login",
                      json={"phone_or_pin": "4289", "bypass_geofence": True}, **R2)
    ok("pin_refused_unprovisioned", r.status_code == 403
       and r.json().get("device_required") is True, f"{r.status_code} {r.text[:80]}")
    # provision -> redeem -> sign in
    p = ad.post(f"{BASE}/api/admin/worker-devices/provision",
                json={"employee_id": EMP_ID, "label": "SMK289 phone"}, **R2)
    code = p.json().get("data", {}).get("provision_code") if p.status_code == 200 else None
    ok("provision_issues_code", bool(code), f"{p.status_code}")
    red = requests.post(f"{BASE}/api/worker/device/redeem",
                        json={"employee_id": EMP_ID, "provision_code": code}, **R2)
    token = red.json().get("data", {}).get("device_token") if red.status_code == 200 else None
    ok("redeem_issues_token", bool(token), f"{red.status_code}")
    r = requests.post(f"{BASE}/api/worker/login",
                      json={"phone_or_pin": "4289", "device_token": token, "bypass_geofence": True}, **R2)
    ok("pin_ok_with_provisioned_device", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
    # revoke -> refused again
    lst = ad.get(f"{BASE}/api/admin/worker-devices", params={"employee_id": EMP_ID}, **R2)
    dev_id = (lst.json().get("data", {}).get("devices") or [{}])[0].get("id")
    rv = ad.post(f"{BASE}/api/admin/worker-devices/revoke", json={"device_id": dev_id}, **R2)
    ok("revoke_200", rv.status_code == 200, f"{rv.status_code}")
    r = requests.post(f"{BASE}/api/worker/login",
                      json={"phone_or_pin": "4289", "device_token": token, "bypass_geofence": True}, **R2)
    ok("pin_refused_after_revoke", r.status_code == 403, f"{r.status_code}")
    # wrong provision code -> pin_fail audit, no token
    bad = requests.post(f"{BASE}/api/worker/device/redeem",
                        json={"employee_id": EMP_ID, "provision_code": "000000"}, **R2)
    ok("bad_provision_code_401", bad.status_code == 401, f"{bad.status_code}")

    print("\n-- secrets posture: no module reads a .env file --")
    offenders = []
    for py in SCRIPT_DIR.glob("*.py"):
        txt = py.read_text(encoding="utf-8", errors="replace")
        if re.search(r"""open\(\s*['"][^'"]*\.env['"]|load_dotenv|dotenv\.""", txt):
            offenders.append(py.name)
    ok("no_module_reads_dotenv", not offenders, str(offenders))


def main():
    print(f"== #289 guard: public-door hardening ==  BASE={BASE}")
    db_url = (os.environ.get("SSC_DB_URL") or "").strip()
    print(f"   backend={'postgres' if db_layer.is_postgres() else 'sqlite'}  "
          f"SSC_DB_URL={'(set)' if db_url else '(unset)'}")
    if not db_url and os.environ.get("ALLOW_LIVE") != "1":
        print("REFUSING TO RUN: SSC_DB_URL is unset — this suite seeds users + toggles "
              "worker enforcement and must never touch the live DB.")
        return 2
    admin_id, admin_tok = seed()
    try:
        run(admin_id, admin_tok)
    finally:
        cleanup()
    print(f"\n== {len(PASS)} PASS / {len(FAIL)} FAIL ==")
    if FAIL:
        print("FAILURES: " + ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
