"""#261 — Google SSO smoke. The Google token verification is MOCKED via the
GOOGLE_OAUTH_FAKE_VERIFY seam (the callback decodes `code` as base64url(JSON claims))
— this NEVER calls real Google. Covers:

  (a) valid ACTIVE company user  -> session created, correct role home, login_audit
      (event=login_success, method=google) written, google_sub stored, must_reset cleared
  (b) wrong email domain / unverified email / missing hd claim -> REJECTED
  (c) email not in users table   -> REJECTED (no auto-provision; row NOT created)
  (d) disabled / pending user    -> REJECTED (status check on the SSO path too)
  (e) CSRF state mismatch / no state cookie -> REJECTED
  (f) SSO DISABLED (no creds)     -> config reports enabled:false + /auth/google/* 404

Plus: email/password login STILL works while SSO is enabled AND while disabled.

Self-contained: launches its OWN server(s) (server.py) with controlled GOOGLE_OAUTH_*
env, inheriting SSC_DB_URL so it runs against the ISOLATED test DB (never live). All
fixtures are synthetic is_system=1 rows (smk261-*), scoped-cleaned in finally:. PII
discipline: asserts booleans/counts only; never prints tokens, names, or hashes.

Run:  python tests/smoke_auth_sso.py     (SMOKE_SSO_PORT overrides the default 5153)
"""
from __future__ import annotations

import base64
import json
import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")   # test-only seams are inert without this (runtime_seams.py)
import secrets
import subprocess
import sys
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import db_layer  # noqa: E402
from auth import hash_password  # noqa: E402
from apply_google_sso_261 import ensure_google_sso_schema  # noqa: E402

VENV_PY = SCRIPT_DIR / "venv" / "Scripts" / "python.exe"
PORT = int(os.environ.get("SMOKE_SSO_PORT", "5153"))
BASE = f"http://127.0.0.1:{PORT}"
SRV_LOG = SCRIPT_DIR / "tests" / "_smoke_auth_sso_srv.log"
DOMAIN = "superstarscontracting.com"

# Dummy OAuth client config — presence ENABLES SSO; FAKE_VERIFY bypasses the network.
# Not real credentials; the secret never leaves this process / the subprocess env.
CLIENT_ID = "smk261.apps.googleusercontent.com"
CLIENT_SECRET = "smk261-secret-not-real-" + uuid.uuid4().hex[:8]
REDIRECT_URI = f"{BASE}/auth/google/callback"

# Synthetic fixtures. @superstarscontracting.com so the domain rule passes; roles are
# c_suite/pm (NEVER admin — single-admin invariant). is_system=1 hides them + exempts
# the invariant. PWUSER is an external client on the password path.
ACTIVE = f"smk261-sso-active@{DOMAIN}"
DISABLED = f"smk261-sso-disabled@{DOMAIN}"
PENDING = f"smk261-sso-pending@{DOMAIN}"
NOACCT = f"smk261-sso-noaccount@{DOMAIN}"     # NEVER inserted (no-auto-provision proof)
PWUSER = "smk261-sso-pwclient@example.invalid"
PWPASS = "SmkPw" + secrets.token_urlsafe(12) + "9"
SUB_ACTIVE = "smk261-sub-" + uuid.uuid4().hex
ALL_EMAILS = (ACTIVE, DISABLED, PENDING, NOACCT, PWUSER)

ENABLED_ENV = {
    "GOOGLE_OAUTH_CLIENT_ID": CLIENT_ID,
    "GOOGLE_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
    "GOOGLE_OAUTH_REDIRECT_URI": REDIRECT_URI,
    "GOOGLE_OAUTH_FAKE_VERIFY": "1",   # test seam — NEVER set in production
}
DISABLED_ENV = {
    "GOOGLE_OAUTH_CLIENT_ID": "",
    "GOOGLE_OAUTH_CLIENT_SECRET": "",
    "GOOGLE_OAUTH_REDIRECT_URI": "",
    "GOOGLE_OAUTH_FAKE_VERIFY": "",
}

PASS, FAIL = [], []


def ok(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {note}" if note and not cond else ""))
    return cond


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ---------- DB fixtures (synthetic, scoped) ----------

def _purge(conn, email):
    r = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if r:
        uid = r[0]
        conn.execute("DELETE FROM login_audit WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM role_change_audit WHERE user_id=? OR changed_by=?", (uid, uid))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))


def _insert(conn, email, role, status, is_active, must_reset, pw=None):
    conn.execute(
        "INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
        "must_reset_password, is_system, created_at) VALUES (?,?,?,?,?,?,?,1,?)",
        (email, hash_password(pw or secrets.token_urlsafe(16)), role, "SMK261 SSO Fixture",
         is_active, status, must_reset, _now()))


def seed():
    conn = db_layer.connect(pragma_fk=True)
    try:
        ensure_google_sso_schema(conn)  # idempotent; ensures google_sub + login_audit.method
        for em in ALL_EMAILS:
            _purge(conn, em)
        _insert(conn, ACTIVE, "pm", "active", 1, 1)        # must_reset=1 -> prove SSO clears it
        _insert(conn, DISABLED, "pm", "disabled", 0, 0)
        _insert(conn, PENDING, "c_suite", "pending", 1, 0)
        _insert(conn, PWUSER, "client", "active", 1, 0, pw=PWPASS)  # external client (password)
        conn.commit()                                       # NOACCT intentionally absent
    finally:
        conn.close()


def cleanup():
    conn = db_layer.connect(pragma_fk=True)
    try:
        for em in ALL_EMAILS:
            _purge(conn, em)
        conn.commit()
    finally:
        conn.close()


def user_row(email):
    conn = db_layer.connect()
    try:
        return conn.execute(
            "SELECT id, role, status, google_sub, must_reset_password FROM users WHERE email=?",
            (email,)).fetchone()
    finally:
        conn.close()


def audit_has(email, event, method):
    conn = db_layer.connect()
    try:
        u = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not u:
            return False
        n = conn.execute("SELECT COUNT(1) FROM login_audit WHERE user_id=? AND event=? AND method=?",
                         (u[0], event, method)).fetchone()[0]
        return n > 0
    finally:
        conn.close()


def session_count(email):
    conn = db_layer.connect()
    try:
        u = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not u:
            return 0
        return conn.execute("SELECT COUNT(1) FROM sessions WHERE user_id=?", (u[0],)).fetchone()[0]
    finally:
        conn.close()


# ---------- server lifecycle ----------

def kill_port(port):
    ps = (f"$c=Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue;"
          f"foreach($x in $c){{cmd /c \"taskkill /F /T /PID $($x.OwningProcess)\"}}")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=30)


def start_server(extra_env):
    env = {**os.environ, "PORT": str(PORT), **extra_env}
    logf = open(SRV_LOG, "w", encoding="utf-8")
    proc = subprocess.Popen([str(VENV_PY), "server.py"], cwd=str(SCRIPT_DIR),
                            stdout=logf, stderr=subprocess.STDOUT, env=env)
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                return proc
        except requests.exceptions.ConnectionError:
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"server exited rc={proc.returncode} — see {SRV_LOG.name}")
        time.sleep(0.5)
    raise RuntimeError("server did not come up in 40s")


def stop_server(proc):
    if proc:
        subprocess.run(["cmd", "/c", f"taskkill /F /T /PID {proc.pid}"], capture_output=True, timeout=30)


# ---------- SSO flow helpers (no redirect-follow; assert the 302 + Set-Cookie) ----------

def _fake_code(claims):
    return base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")


def sso_flow(claims, tamper=False, no_cookie=False):
    """Drive /auth/google/login -> /auth/google/callback. Returns the callback response
    (allow_redirects=False) so the caller asserts the 302 Location + Set-Cookie."""
    s = requests.Session()
    if no_cookie:
        # never establish a state cookie -> the callback must reject on the missing cookie
        r2 = s.get(f"{BASE}/auth/google/callback",
                   params={"state": secrets.token_urlsafe(16), "code": _fake_code(claims)},
                   allow_redirects=False, timeout=10)
        return r2, s
    r1 = s.get(f"{BASE}/auth/google/login", allow_redirects=False, timeout=10)
    loc = r1.headers.get("Location", "")
    state = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("state", [""])[0]
    param_state = ("tampered-" + state) if tamper else state
    r2 = s.get(f"{BASE}/auth/google/callback",
               params={"state": param_state, "code": _fake_code(claims)},
               allow_redirects=False, timeout=10)
    return r2, s


def _loc(r):
    return r.headers.get("Location", "")


def main():
    print(f"#261 smoke_auth_sso: target={BASE}  (verification MOCKED; isolated DB)")
    kill_port(PORT)
    seed()
    proc = None
    try:
        # ===== Phase 1: SSO ENABLED (dummy creds + fake verify) =====
        proc = start_server(ENABLED_ENV)

        cfg = requests.get(f"{BASE}/api/auth/sso/config", timeout=10).json()
        ok("config_enabled_when_creds_present",
           cfg.get("google", {}).get("enabled") is True
           and cfg["google"].get("login_url") == "/auth/google/login")

        # login route: state cookie + redirect to Google with the right params
        s0 = requests.Session()
        rl = s0.get(f"{BASE}/auth/google/login", allow_redirects=False, timeout=10)
        loc0 = _loc(rl)
        ok("login_redirects_to_google",
           rl.status_code == 302 and loc0.startswith("https://accounts.google.com/")
           and "state=" in loc0 and f"hd={DOMAIN}" in loc0 and "scope=openid" in loc0
           and "response_type=code" in loc0, f"loc={loc0[:80]}")
        ok("login_sets_state_cookie", bool(s0.cookies.get("g_oauth_state")))

        # (a) valid ACTIVE company user
        claims_a = {"email": ACTIVE, "email_verified": True, "hd": DOMAIN, "sub": SUB_ACTIVE, "name": "A"}
        r, _ = sso_flow(claims_a)
        ok("active_user_session_created",
           r.status_code == 302 and "/projects" in _loc(r) and bool(r.cookies.get("ssc_session")),
           f"status={r.status_code} loc={_loc(r)}")
        row = user_row(ACTIVE)
        # #263 — a pm's role home is the assigned-projects-only landing (/projects), not /dashboard
        ok("active_user_role_home_pm_projects", "/projects" in _loc(r))
        ok("active_user_google_sub_stored", bool(row) and row["google_sub"] == SUB_ACTIVE)
        ok("active_user_must_reset_cleared", bool(row) and row["must_reset_password"] in (0, False))
        ok("active_user_audit_login_success_method_google", audit_has(ACTIVE, "login_success", "google"))
        ok("active_user_session_row_exists", session_count(ACTIVE) >= 1)

        # re-sign-in with the SAME (now-linked) sub still works; a DIFFERENT sub is rejected
        r, _ = sso_flow(claims_a)
        ok("relink_same_sub_ok", r.status_code == 302 and "/projects" in _loc(r))
        r, _ = sso_flow({**claims_a, "sub": "different-" + SUB_ACTIVE})
        ok("linked_sub_mismatch_rejected", "sso_error=notauthorized" in _loc(r))

        # (b) domain rule
        r, _ = sso_flow({"email": "attacker@evil.com", "email_verified": True, "hd": "evil.com", "sub": "e1"})
        ok("wrong_domain_rejected", "sso_error=domain" in _loc(r) and not r.cookies.get("ssc_session"))
        r, _ = sso_flow({"email": ACTIVE, "email_verified": False, "hd": DOMAIN, "sub": SUB_ACTIVE})
        ok("email_unverified_rejected", "sso_error=domain" in _loc(r))
        r, _ = sso_flow({"email": ACTIVE, "email_verified": True, "sub": SUB_ACTIVE})  # no hd claim
        ok("missing_hd_claim_rejected", "sso_error=domain" in _loc(r))
        # domain spoof via a lookalike email but hd=allowed should still fail (email domain wins)
        r, _ = sso_flow({"email": "x@evil.com", "email_verified": True, "hd": DOMAIN, "sub": "e2"})
        ok("email_domain_must_match_too", "sso_error=domain" in _loc(r))

        # (c) no account -> no auto-provision
        r, _ = sso_flow({"email": NOACCT, "email_verified": True, "hd": DOMAIN, "sub": "noacct1"})
        ok("no_account_rejected_notauthorized", "sso_error=notauthorized" in _loc(r))
        ok("no_account_not_auto_provisioned", user_row(NOACCT) is None)

        # (d) disabled / pending
        r, _ = sso_flow({"email": DISABLED, "email_verified": True, "hd": DOMAIN, "sub": "dis1"})
        ok("disabled_user_rejected", "sso_error=notauthorized" in _loc(r) and session_count(DISABLED) == 0)
        r, _ = sso_flow({"email": PENDING, "email_verified": True, "hd": DOMAIN, "sub": "pen1"})
        ok("pending_user_rejected", "sso_error=notauthorized" in _loc(r) and session_count(PENDING) == 0)

        # (e) CSRF
        r, _ = sso_flow(claims_a, tamper=True)
        ok("csrf_state_mismatch_rejected", "sso_error=state" in _loc(r))
        r, _ = sso_flow(claims_a, no_cookie=True)
        ok("csrf_no_state_cookie_rejected", "sso_error=state" in _loc(r))

        # email/password STILL works while SSO is enabled
        sp = requests.Session()
        rp = sp.post(f"{BASE}/api/auth/login", json={"email": PWUSER, "password": PWPASS}, timeout=10)
        ok("password_login_still_works_sso_enabled",
           rp.status_code == 200 and bool(sp.cookies.get("ssc_session")))
        rbad = requests.post(f"{BASE}/api/auth/login", json={"email": PWUSER, "password": "wrong-pw"}, timeout=10)
        ok("password_login_wrong_pw_still_401", rbad.status_code == 401)

        stop_server(proc)
        proc = None

        # ===== Phase 2: SSO DISABLED (no creds) =====
        proc = start_server(DISABLED_ENV)
        cfg2 = requests.get(f"{BASE}/api/auth/sso/config", timeout=10).json()
        ok("config_disabled_when_no_creds",
           cfg2.get("google", {}).get("enabled") is False and cfg2["google"].get("login_url") is None)
        rl2 = requests.get(f"{BASE}/auth/google/login", allow_redirects=False, timeout=10)
        ok("login_route_404_when_disabled", rl2.status_code == 404)
        rc2 = requests.get(f"{BASE}/auth/google/callback?state=x&code=y", allow_redirects=False, timeout=10)
        ok("callback_route_404_when_disabled", rc2.status_code == 404)
        # password login STILL works with SSO disabled (the app runs exactly as today)
        sp2 = requests.Session()
        rp2 = sp2.post(f"{BASE}/api/auth/login", json={"email": PWUSER, "password": PWPASS}, timeout=10)
        ok("password_login_works_sso_disabled",
           rp2.status_code == 200 and bool(sp2.cookies.get("ssc_session")))
        stop_server(proc)
        proc = None

    finally:
        stop_server(proc)
        cleanup()
        kill_port(PORT)

    print(f"\n== smoke_auth_sso: {len(PASS)} PASS / {len(FAIL)} FAIL ==")
    if FAIL:
        print("FAILURES:", ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
