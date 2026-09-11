"""Dashboard auth foundation (#48) — hashing, sessions, gating.

Scope: dashboard users only (operator + future C-suite/PM/super). Worker-app
PIN sign-in is unaffected; this module does not touch employees.pin or
/api/worker/* routes.

Public surface used by server.py:
  - apply_auth_gate(app)         — registers before_request hook + login routes
  - requires_role(*roles)        — per-route decorator for role gating
  - current_user()               — accessor for the request-scoped user dict

Session model: server-side. Cookie holds an opaque random id; the row in
`sessions` is the truth. Logout / password change / admin revoke = DELETE
the row. 12-hour sliding window.

PII discipline (per CLAUDE.md):
  - Never log password_hash, plaintext passwords, or full session ids.
  - Session ids in logs are redacted to last-6 prefix.
  - Login failure responses do not distinguish "no such email" from
    "wrong password" — both return the same 401.
"""
from __future__ import annotations

import functools
import logging
from urllib.parse import quote
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import bcrypt
from flask import g, jsonify, make_response, redirect, request, send_file

import db_layer  # #259 — env-driven SQLite/Postgres layer (SQLite default)
import access  # #262 — central role→section access map (the RBAC single source of truth)

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "superstars.db"
LOGIN_PAGE_PATH = SCRIPT_DIR / "login.html"

COOKIE_NAME = "ssc_session"
SESSION_TTL = timedelta(hours=12)  # sliding — refreshed on each authed request
# #289 — absolute ceiling: a session is dead 7 days after CREATION no matter how
# much it slid. Bounds a stolen cookie's usefulness even under continuous use.
SESSION_ABSOLUTE_TTL = timedelta(days=7)

MIN_PASSWORD_LEN = 12
LOGIN_MAX_FAILS = 5                          # per-account, within the window
LOGIN_FAIL_WINDOW = timedelta(minutes=15)

# #289 — public-door login hardening (Cloud M3). The app is going to the open
# internet at M5; the login endpoint gets brute-force resistance NOW.
#   * Per-account AND per-source(IP) fail counting in the window.
#   * EXPONENTIAL BACKOFF: once fails cross the soft threshold, every response
#     (right OR wrong password, existing OR not) is delayed by a growing amount,
#     capped — so there is no timing oracle and no lockout oracle. During a hard
#     lockout even a correct password is refused with the identical 401.
#   * All of it audited (login_lockout), never revealing which of email/password
#     was wrong.
LOGIN_IP_MAX_FAILS = 20                       # per-source, within the window
LOGIN_BACKOFF_AFTER = 3                       # start delaying after this many fails
LOGIN_BACKOFF_BASE = 0.4                      # seconds; delay = base * 2**(fails-after)
LOGIN_BACKOFF_CAP = 6.0                       # seconds, hard ceiling on the delay
SET_PASSWORD_PAGE_PATH = SCRIPT_DIR / "set_password.html"
# A must_reset_password user may reach ONLY these paths (plus the public /api/auth/*)
# until they set a real password — NO data loads behind the forced-reset screen.
_RESET_ALLOWED_EXACT = {"/set-password", "/login"}

# Paths that bypass auth entirely. Order: prefix match (startswith) for
# wildcard buckets; exact match for single paths.
#
# #248 — the /files/ exemption narrowed to /files/static/ (vendored shell
# assets only; the old whole-project mount let anyone download the DB and
# source). /preview/ LOST its exemption: it serves rendered project
# documents (internal DCRs etc.), so it now requires a session like every
# other operator surface. Generated artifacts are served by the gated
# /project-files/ route, which is intentionally NOT exempt.
_PUBLIC_PREFIXES = (
    "/api/auth/",         # login, logout, me, sso/config — see below for /me caveat
    "/api/worker/",       # worker-app PIN flow — unchanged
    "/files/static/",     # vendored shell assets ONLY (css/js/fonts/vendor)
    "/auth/google/",      # #261 — Google OIDC login/callback must be reachable while
                          # logged OUT (that IS the login). When SSO is disabled (no
                          # GOOGLE_OAUTH_* env) the handlers themselves return 404.
)
_PUBLIC_EXACT = {
    "/login",
    "/forgot-password",
    "/reset-password",
    "/api/health",
    "/api/today",         # date-only, no PII, used by client shells on load
    "/worker-app",
    "/worker-app.html",
    "/api/worker-sign-in",
    "/worker-app-manifest.json",  # PWA shell (#248) — code/config, no data
    "/worker-app-sw.js",          # PWA shell (#248)
}


# ============= PASSWORD HASHING =============

def hash_password(plaintext: str) -> str:
    """bcrypt hash with per-call random salt. Returns the 60-char $2b$ string."""
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("password must be a non-empty string")
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plaintext: str, hashed: str) -> bool:
    """Constant-time compare against a stored bcrypt hash. False on any error."""
    if not plaintext or not hashed:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ============= DB HELPER (local — avoids import cycle with server.py) =============

def _db():
    # #259 — env-driven (SSC_DB_URL). SQLite default returns the SAME native sqlite3
    # connection as before (Row, WAL, busy_timeout, foreign_keys=ON); a postgres URL
    # returns the psycopg-backed wrapper (Postgres enforces FKs natively).
    return db_layer.connect(pragma_fk=True)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _redact_sid(sid: Optional[str]) -> str:
    """Session id appearing in logs/errors — last 6 chars only, no leak of the full token."""
    if not sid:
        return "—"
    return f"…{sid[-6:]}"


# ============= AUDIT + SECURITY HELPERS (#257) =============

def _client_ip() -> Optional[str]:
    """The client IP for audit + rate limiting.

    #289 — X-Forwarded-For is only trusted when SSC_TRUSTED_PROXY is set (the
    funnel today; Cloudflare/Render at M4). A forwarded header is client-supplied
    and trivially spoofable, so trusting it blind would let an attacker rotate a
    fake XFF to dodge per-IP throttling AND poison the audit trail. When the env
    is unset we use remote_addr — the real socket peer — full stop.

    SSC_TRUSTED_PROXY may name how many right-most proxy hops to peel: '1' (or
    'true'/'yes') takes the LAST XFF entry appended by our own proxy; an integer
    N takes the Nth-from-the-right. The left-most entries are attacker-controlled
    and never used."""
    trusted = (os.environ.get("SSC_TRUSTED_PROXY") or "").strip().lower()
    if trusted and trusted not in ("0", "false", "no"):
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                try:
                    hops = int(trusted)
                except ValueError:
                    hops = 1
                idx = max(1, hops)
                # idx-th from the right (1 = last hop = the one our proxy added)
                pick = parts[-idx] if idx <= len(parts) else parts[0]
                return pick[:64] or None
    return (request.remote_addr or "")[:64] or None


def _login_audit(user_id: Optional[int], event: str, ip: Optional[str],
                 method: Optional[str] = None) -> None:
    """Append a login_audit row (LOCAL timestamp). NEVER records the password, the
    hash, or which non-existent email was tried (user_id is NULL when unknown).
    `method` distinguishes the login path — 'google' for SSO (#261), NULL for the
    email/password path. Auth must never fail because the audit write failed (any
    backend error — incl. a not-yet-migrated method column — is swallowed)."""
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO login_audit (user_id, event, at, ip, method) VALUES (?, ?, ?, ?, ?)",
            (user_id, event, _now_iso(), ip, method),
        )
        conn.commit()
    except Exception as e:
        # do not block auth (SQLite or PG) — but never silently: a dropped audit row
        # also disables every counter that reads this table (#305)
        logging.warning(f"login_audit write failed for event={event}: {type(e).__name__}")
    finally:
        conn.close()


def _recent_login_fails(user_id: int) -> int:
    """login_fail count for this account within the lockout window."""
    since = (datetime.now() - LOGIN_FAIL_WINDOW).isoformat(timespec="seconds")
    conn = _db()
    try:
        row = conn.execute(
            "SELECT COUNT(1) FROM login_audit WHERE user_id = ? AND event = 'login_fail' AND at >= ?",
            (user_id, since),
        ).fetchone()
        return row[0] if row else 0
    except Exception as e:   # not-yet-migrated table, or any backend error — fail open (no lockout)
        logging.warning(f"_recent_login_fails unavailable: {type(e).__name__}")
        return 0
    finally:
        conn.close()


def _recent_ip_fails(ip: Optional[str]) -> int:
    """login_fail count from this SOURCE IP within the window (#289 per-source
    throttle — catches password spraying across many accounts from one origin)."""
    if not ip:
        return 0
    since = (datetime.now() - LOGIN_FAIL_WINDOW).isoformat(timespec="seconds")
    conn = _db()
    try:
        row = conn.execute(
            "SELECT COUNT(1) FROM login_audit WHERE ip = ? "
            "AND event IN ('login_fail', 'pin_fail') AND at >= ?",   # #305 — PIN failures count too
            (ip, since)).fetchone()
        return row[0] if row else 0
    except Exception as e:
        logging.warning(f"_recent_ip_fails unavailable: {type(e).__name__}")
        return 0
    finally:
        conn.close()


def _login_backoff_delay(fails: int) -> float:
    """Seconds to stall a login response given the current fail count. Zero below
    the soft threshold, then exponential, capped. Applied UNIFORMLY (right or wrong
    password) once tripped, so response timing never distinguishes the two —
    closing the timing oracle that a bare per-account counter would open."""
    if fails < LOGIN_BACKOFF_AFTER:
        return 0.0
    return min(LOGIN_BACKOFF_CAP, LOGIN_BACKOFF_BASE * (2 ** (fails - LOGIN_BACKOFF_AFTER)))


def password_strength_error(pw: str) -> Optional[str]:
    """Return an error string if the password is too weak, else None. Reasonable
    rule: >= MIN_PASSWORD_LEN chars with at least one letter and one digit."""
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if not any(c.isalpha() for c in pw):
        return "Password must include at least one letter."
    if not any(c.isdigit() for c in pw):
        return "Password must include at least one number."
    return None


# ============= USERS =============

def get_user_by_email(email: str) -> Optional[dict]:
    if not email:
        return None
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, email, password_hash, role, full_name, display_name, employee_id_link, "
            "       is_active, status, must_reset_password, created_at, last_login_at, "
            "       totp_secret, totp_enabled, totp_recovery, force_sso, google_sub "
            "FROM users WHERE LOWER(email) = LOWER(?)",
            (email.strip(),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_id(user_id: int) -> Optional[dict]:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, email, role, full_name, employee_id_link, is_active, "
            "       created_at, last_login_at "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def touch_last_login(user_id: int) -> None:
    conn = _db()
    try:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_now_iso(), user_id))
        conn.commit()
    finally:
        conn.close()


# ============= SESSIONS =============

def create_session(user_id: int, user_agent: Optional[str]) -> str:
    """Insert a sessions row, return the opaque id (caller sets it as cookie)."""
    sid = secrets.token_urlsafe(32)
    expires = (datetime.now() + SESSION_TTL).isoformat(timespec="seconds")
    # Trim UA to a reasonable length — not used for auth, just forensic breadcrumb.
    ua = (user_agent or "")[:255]
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO sessions (id, user_id, expires_at, user_agent) VALUES (?, ?, ?, ?)",
            (sid, user_id, expires, ua),
        )
        conn.commit()
    finally:
        conn.close()
    return sid


def lookup_session(sid: str) -> Optional[dict]:
    """Resolve a session id to the active user row. Refreshes sliding TTL on hit.

    Returns None for missing, expired, or inactive-user sessions. Expired
    rows are deleted opportunistically (no separate sweeper needed for the
    single-operator workload).
    """
    if not sid:
        return None
    conn = _db()
    try:
        row = conn.execute(
            "SELECT s.id, s.user_id, s.expires_at, s.created_at, "
            "       u.email, u.role, u.full_name, u.display_name, u.employee_id_link, "
            "       u.is_active, u.status, u.must_reset_password "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.id = ?",
            (sid,),
        ).fetchone()
        if not row:
            return None
        # Expiry check — string compare on ISO-8601 with seconds precision is total-order safe.
        if row["expires_at"] < _now_iso():
            conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            conn.commit()
            return None
        # #289 — ABSOLUTE ceiling: dead SESSION_ABSOLUTE_TTL after creation, no
        # matter how much the sliding TTL was refreshed. created_at may be a DB
        # DEFAULT (CURRENT_TIMESTAMP, 'YYYY-MM-DD HH:MM:SS' UTC-ish) or a value we
        # wrote; compare on the date-time prefix, tolerant of the 'T' separator.
        created = str(row["created_at"] or "").replace("T", " ")[:19]
        if created:
            cutoff = (datetime.now() - SESSION_ABSOLUTE_TTL).isoformat(
                sep=" ", timespec="seconds")
            if created < cutoff:
                conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                conn.commit()
                logging.info(f"auth: session past absolute cap sid={_redact_sid(sid)}")
                return None
        # Status is re-read from the users row on EVERY request (the session stores
        # only user_id) — so a deactivation / role change takes effect on the user's
        # very next request. A non-active user's session is destroyed so re-auth is forced.
        if row["status"] != "active" or not row["is_active"]:
            conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            conn.commit()
            return None
        # Sliding refresh: push expires_at forward, update last_used_at.
        new_expires = (datetime.now() + SESSION_TTL).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE sessions SET expires_at = ?, last_used_at = ? WHERE id = ?",
            (new_expires, _now_iso(), sid),
        )
        conn.commit()
        return {
            "id": row["user_id"],
            "email": row["email"],
            "role": row["role"],
            "full_name": row["full_name"],
            "display_name": row["display_name"] or row["full_name"],
            "employee_id_link": row["employee_id_link"],
            "status": row["status"],
            "must_reset_password": bool(row["must_reset_password"]),
        }
    finally:
        conn.close()


def destroy_session(sid: str) -> None:
    if not sid:
        return
    conn = _db()
    try:
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        conn.commit()
    finally:
        conn.close()


def destroy_all_user_sessions(user_id: int) -> None:
    """Invalidate every session for a user (e.g. on password change). Reserved for future use."""
    conn = _db()
    try:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


# ============= COOKIE HELPERS =============

def _request_is_https() -> bool:
    """True if the CLIENT-FACING connection is HTTPS (drives the Secure cookie flag).

    Three signals, any one counts:
      * request.is_secure — a direct TLS connection.
      * X-Forwarded-Proto=https — a proxy that forwards the header (dev/direct).
      * SSC_TRUSTED_PROXY set — the operator asserts a TLS-terminating trusted edge
        sits in front (Tailscale serve today; Cloudflare/Render at M4). #289 —
        waitress STRIPS untrusted X-Forwarded-* by default, so the header alone is
        unreliable behind our own proxy; the trusted-proxy declaration is the
        authoritative "every real request arrived over HTTPS at the edge" signal
        (the app binds loopback-only, so the proxy is the only ingress)."""
    if request.is_secure:
        return True
    if request.headers.get("X-Forwarded-Proto", "").lower() == "https":
        return True
    tp = (os.environ.get("SSC_TRUSTED_PROXY") or "").strip().lower()
    return bool(tp) and tp not in ("0", "false", "no")


def _set_session_cookie(response, sid: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        sid,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=_request_is_https(),
        samesite="Lax",
        path="/",
    )


def _clear_session_cookie(response) -> None:
    response.set_cookie(
        COOKIE_NAME,
        "",
        max_age=0,
        httponly=True,
        secure=_request_is_https(),
        samesite="Lax",
        path="/",
    )


# ============= CURRENT USER =============

def current_user() -> Optional[dict]:
    """Request-scoped user dict, populated by the before_request gate. None if unauthed."""
    return getattr(g, "auth_user", None)


# ============= GATE =============

def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    for prefix in _PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _wants_json(path: str) -> bool:
    """JSON-style endpoints get a 401; HTML routes get a redirect to /login.

    Non-HTML resource fetches (worker-photo image, file downloads under
    /worker-files/) ALSO get 401 instead of a redirect. The 302 -> /login
    HTML response is fatal when a <img src=/worker-files/...> hits it
    while the cookie isn't set yet — the browser caches the redirected
    HTML against the image URL, then renders that URL as a broken image
    on every subsequent page load even after a fresh login. See #172
    (Robert A. photo loop bug). 401 keeps the browser cache clean.
    """
    if path.startswith("/api/"):
        return True
    if path.startswith("/worker-files/"):
        return True
    if path.startswith("/project-files/"):  # gated artifacts (#248) — same #172 rule
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def _before_request_gate():
    # OPTIONS preflight — let CORS handle it without auth.
    if request.method == "OPTIONS":
        return None
    path = request.path
    if _is_public_path(path):
        return None
    sid = request.cookies.get(COOKIE_NAME)
    user = lookup_session(sid) if sid else None
    if not user:
        if _wants_json(path):
            return jsonify({"error": "auth required"}), 401
        # HTML route — redirect to /login with ?next= so we can bounce back post-login.
        # next is intentionally restricted to same-origin paths only (validated on login).
        return redirect("/login?next=" + quote(path, safe="/"))   # #305 — no query splitting
    g.auth_user = user
    # Forced first-login reset (#257): a must_reset_password user can reach ONLY the
    # set-password screen + the public /api/auth/* endpoints. NO data loads behind it
    # — server-side, so it holds even if the client UI is bypassed.
    if user.get("must_reset_password") and path not in _RESET_ALLOWED_EXACT:
        if _wants_json(path):
            return jsonify({"error": "password reset required", "must_reset": True}), 403
        return redirect("/set-password")
    return None


def _login_page():
    if LOGIN_PAGE_PATH.exists():
        # #279 — the toggle resolves to v1 here in practice (nobody is signed in on the
        # login page, so there is no stored preference to read), but ?ui=2 still lets the
        # operator preview a v2 login twin once one exists.
        import ui_version
        return send_file(str(ui_version.resolve_page(LOGIN_PAGE_PATH)))
    return ("login page missing", 500)


_UNIFORM_401 = ({"error": "invalid credentials"}, 401)


def _fail_login(user, ip, delay):
    """The ONE failure exit: audit (only for a resolved account), stall by `delay`
    (uniform whether the password was right, wrong, or the email unknown), return
    the identical 401. No branch here reveals which of email/password was wrong,
    nor whether the account exists — the response body, status, and timing are the
    same for every failure mode."""
    if user:
        _login_audit(user["id"], "login_fail", ip)
    if delay > 0:
        time.sleep(delay)
    logging.info("auth: login failed")  # PII rule: never the email
    return jsonify(_UNIFORM_401[0]), _UNIFORM_401[1]


def _api_login():
    """POST /api/auth/login → {email, password, totp?} → session cookie.

    #289 order of operations, chosen so nothing distinguishes accounts to an
    UNAUTHENTICATED caller:
      1. rate context (per-account + per-source fails) -> backoff delay
      2. HARD LOCKOUT (account or IP over the ceiling): audit login_lockout,
         stall, uniform 401 — a correct password is refused identically.
      3. verify password; wrong -> uniform 401 via _fail_login (stalled).
      4. [password now known correct] force_sso -> 403 use-SSO; TOTP enrolled ->
         require the code (a distinct `totp_required` step, but only reachable
         AFTER a correct password, so it enumerates nothing to an attacker).
      5. success: rotate the session, audit, set cookie."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    totp_code = (data.get("totp") or "").strip()
    if not email or not password:
        return jsonify({"error": "email and password required"}), 400
    ip = _client_ip()
    user = get_user_by_email(email)

    acct_fails = _recent_login_fails(user["id"]) if user else 0
    ip_fails = _recent_ip_fails(ip)
    delay = max(_login_backoff_delay(acct_fails), _login_backoff_delay(ip_fails))

    # HARD LOCKOUT — refuse even a correct password with the uniform 401.
    if (user and acct_fails >= LOGIN_MAX_FAILS) or ip_fails >= LOGIN_IP_MAX_FAILS:
        if user:
            _login_audit(user["id"], "login_lockout", ip)
        if delay > 0:
            time.sleep(delay)
        logging.info("auth: login refused (locked out)")
        return jsonify(_UNIFORM_401[0]), _UNIFORM_401[1]

    active = bool(user) and user.get("status") == "active" and bool(user.get("is_active"))
    if not user or not active or not verify_password(password, user["password_hash"]):
        return _fail_login(user, ip, delay)

    # ---- password is correct from here; the branches below are safe to distinguish ----
    if user.get("force_sso"):
        logging.info("auth: password path refused (force_sso)")
        return jsonify({"error": "This account must sign in with Google.",
                        "sso_required": True}), 403

    if user.get("totp_enabled"):
        import json
        ok_totp = False
        recovery_used = False
        if totp_code:
            import totp as _totp
            if _totp.verify(user.get("totp_secret") or "", totp_code):
                ok_totp = True
            else:
                try:
                    codes = json.loads(user.get("totp_recovery") or "[]")
                except Exception:
                    codes = []
                burned, remaining = _totp.consume_recovery(totp_code, codes)
                if burned:
                    ok_totp = True
                    recovery_used = True
                    _conn = _db()
                    try:
                        _conn.execute("UPDATE users SET totp_recovery=? WHERE id=?",
                                      (json.dumps(remaining), user["id"]))
                        _conn.commit()
                    finally:
                        _conn.close()
        if not ok_totp:
            # correct password, missing/incorrect second factor: NOT a login_fail
            # against the password counter (that would let a factor-2 miss trip the
            # password lockout); its own audit event, and a distinct 401 that the
            # UI turns into the code prompt.
            _login_audit(user["id"], "totp_fail", ip)
            return jsonify({"error": "second factor required", "totp_required": True}), 401
        if recovery_used:
            _login_audit(user["id"], "totp_recovery_used", ip)

    # rotate: destroy any session the client presented before minting a fresh one.
    old_sid = request.cookies.get(COOKIE_NAME)
    if old_sid:
        destroy_session(old_sid)
    sid = create_session(user["id"], request.headers.get("User-Agent"))
    touch_last_login(user["id"])
    _login_audit(user["id"], "login_success", ip)
    logging.info(f"auth: login ok role={user['role']} sid={_redact_sid(sid)}")
    response = make_response(jsonify({
        "user": {
            "id": user["id"],
            "email": user["email"],
            "full_name": user["full_name"],
            "display_name": user.get("display_name") or user["full_name"],
            "role": user["role"],
            "must_reset_password": bool(user.get("must_reset_password")),
        }
    }))
    _set_session_cookie(response, sid)
    return response


def _api_logout():
    sid = request.cookies.get(COOKIE_NAME)
    user = lookup_session(sid) if sid else None
    if user:
        _login_audit(user["id"], "logout", _client_ip())
    destroy_session(sid)
    logging.info(f"auth: logout sid={_redact_sid(sid)}")
    response = make_response(jsonify({"ok": True}))
    _clear_session_cookie(response)
    return response


def _api_me():
    """GET /api/auth/me — returns the current user or 401. Used by the client shell to
    render the header name/role + decide which controls to show."""
    sid = request.cookies.get(COOKIE_NAME)
    user = lookup_session(sid) if sid else None
    if not user:
        return jsonify({"user": None}), 401
    return jsonify({"user": {
        "id": user["id"],
        "email": user["email"],
        "full_name": user["full_name"],
        "display_name": user.get("display_name") or user["full_name"],
        "role": user["role"],
        "status": user.get("status"),
        "must_reset_password": bool(user.get("must_reset_password")),
        # #262 — gated-section visibility for this role (same source the
        # server-rendered sidebar uses); the client may reflect it.
        "sections": access.section_visibility(user["role"]),
    }})


def _set_password_page():
    """The forced first-login 'set your password' screen. Reached only by an
    authenticated user (the before_request gate redirects must_reset users here)."""
    if SET_PASSWORD_PAGE_PATH.exists():
        import ui_version                                              # #279
        resp = send_file(str(ui_version.resolve_page(SET_PASSWORD_PAGE_PATH)))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp
    return ("set-password page missing", 500)


def _api_set_password():
    """POST /api/auth/set-password — {new_password}. The user must be authenticated
    (logged in with their temp password). Validates strength, updates the bcrypt
    hash, clears must_reset_password, audits password_set. Session stays valid so
    the client can route straight to the role home. NEVER logs the password."""
    sid = request.cookies.get(COOKIE_NAME)
    user = lookup_session(sid) if sid else None
    if not user:
        return jsonify({"error": "auth required"}), 401
    data = request.get_json(silent=True) or {}
    new_pw = data.get("new_password") or ""
    err = password_strength_error(new_pw)
    if err:
        return jsonify({"error": err}), 400
    conn = _db()
    try:
        conn.execute(
            "UPDATE users SET password_hash = ?, must_reset_password = 0 WHERE id = ?",
            (hash_password(new_pw), user["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    _login_audit(user["id"], "password_set", _client_ip())
    logging.info(f"auth: password_set user_id={user['id']}")  # PII rule: never the password
    return jsonify({"ok": True, "role": user["role"]})


def apply_auth_gate(app) -> None:
    """Wire the before_request gate + the /login page + the /api/auth/* routes onto `app`."""
    app.before_request(_before_request_gate)
    app.add_url_rule("/login", "auth_login_page", _login_page, methods=["GET"])
    app.add_url_rule("/set-password", "auth_set_password_page", _set_password_page, methods=["GET"])
    app.add_url_rule("/api/auth/login", "auth_api_login", _api_login, methods=["POST"])
    app.add_url_rule("/api/auth/logout", "auth_api_logout", _api_logout, methods=["POST"])
    app.add_url_rule("/api/auth/me", "auth_api_me", _api_me, methods=["GET"])
    app.add_url_rule("/api/auth/set-password", "auth_api_set_password", _api_set_password, methods=["POST"])


# ============= ROLE DECORATOR =============

def requires_role(*allowed_roles: str):
    """Per-route role gate. Use on top of the blanket before_request login gate
    for routes that need more than just "logged in" — Labor Rates + LRT rate
    columns will use @requires_role('admin', 'c_suite') when #158 lands.

    Returns 403 JSON on wrong role; the blanket gate has already enforced
    that the request is authenticated before this decorator runs.
    """
    allowed: set = set(allowed_roles)

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                # before_request should have caught this — defense in depth.
                return jsonify({"error": "auth required"}), 401
            if user.get("role") not in allowed:
                logging.info(
                    f"auth: role denied role={user.get('role')} "
                    f"path={request.path} required={sorted(allowed)}"
                )
                return jsonify({"error": "forbidden"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def requires_section(section: str):
    """Per-route gate by SECTION — reads the SAME `access.SECTION_ACCESS` map the
    server-rendered sidebar uses, so a section's access lives in ONE place (change it
    in access.py and both the menu and the endpoints follow). Returns 403 JSON if the
    authenticated user's role can't access `section`; the blanket before_request gate
    has already enforced that the request is authenticated."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                # before_request should have caught this — defense in depth.
                return jsonify({"error": "auth required"}), 401
            if not access.can_access(section, user.get("role")):
                logging.info(
                    f"auth: section denied section={section} role={user.get('role')} "
                    f"path={request.path}"
                )
                return jsonify({"error": "forbidden"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def requires_company(fn):
    """Per-route gate for company-level surfaces (#263) — the company overview console
    and its company-wide tabs/endpoints (Workforce, Cert Health/Library, Specs, Settings,
    …) are admin/c_suite ONLY. Reads `access.can_access_company` so the company-vs-project
    role line lives in ONE place. A `pm` (scoped to assigned projects) gets 403, even on a
    direct URL/API call — hiding the nav is not access control. The blanket before_request
    login gate runs first; this adds the role check."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "auth required"}), 401
        if not access.can_access_company(user.get("role")):
            logging.info(
                f"auth: company denied role={user.get('role')} path={request.path}")
            return jsonify({"error": "forbidden"}), 403
        return fn(*args, **kwargs)
    return wrapper
