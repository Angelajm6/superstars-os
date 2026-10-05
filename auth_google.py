"""Google OIDC SSO for the internal team (#261, hosting phase 2).

"Sign in with Google" for the company's Google Workspace, mapped onto the existing
#257 role/session/audit system. Email/password (auth.py) is UNCHANGED and stays the
path for external clients.

SECURITY MODEL (every rule enforced SERVER-SIDE here):
  * DOMAIN RESTRICTION — only a VERIFIED @<allowed-domain> Google identity may sign
    in. Enforced on the verified ID-token CLAIMS: email_verified == True AND the
    email's domain == allowed AND the `hd` CLAIM == allowed. The `hd` URL *param* is
    only a hint to Google's account chooser; it is NEVER trusted for the decision.
  * NO AUTO-PROVISIONING — a valid Google login whose email is not already an ACTIVE
    row in `users` (admin-created) is REJECTED. SSO authenticates identity;
    authorization/role still comes only from the admin-managed users table (#257).
    A disabled/pending account is rejected too — on BOTH login paths.
  * VETTED LIBRARY — the ID-token signature/iss/aud/exp is verified by google-auth
    (`verify_oauth2_token`) against Google's published keys. We do NOT hand-roll JWT.
  * CLIENT SECRET — from the GOOGLE_OAUTH_CLIENT_SECRET env var ONLY; never in code,
    never logged, never committed. CSRF `state` is required on the flow.

FEATURE FLAG / GRACEFUL DISABLE: with the GOOGLE_OAUTH_* env vars unset, SSO is
DISABLED — the button is hidden (sso/config reports enabled:false) and the
/auth/google/* routes return 404 — and the app runs exactly as before. So production
is never broken before the OAuth client is registered + the env vars are set.

PII discipline: never logs email, name, tokens, or the google_sub. Logs role +
redacted session id + generic outcome strings only. sso_error reasons in redirects
are generic codes (no PII).
"""
from __future__ import annotations

import base64
import json
import logging
import os

import runtime_seams
import secrets
import urllib.parse

import requests
from flask import jsonify, redirect, request

import auth  # reuse the #257 session / audit / cookie machinery (identical downstream)

# Google OIDC endpoints (public, well-known).
_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_VALID_ISS = ("accounts.google.com", "https://accounts.google.com")

_STATE_COOKIE = "g_oauth_state"
_STATE_TTL = 600  # seconds — the login->callback round trip is brief
_DEFAULT_DOMAIN = "superstarscontracting.com"


# ============= CONFIG (read at request time so per-process env works) =============

def _client_id() -> str:
    return (os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip()


def _client_secret() -> str:
    # Never logged, never returned to a client. Read here, used only for the
    # server-to-server token exchange with Google.
    return (os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()


def _redirect_uri() -> str:
    # MUST exactly match a redirect URI registered on the OAuth client in Google Cloud.
    return (os.environ.get("GOOGLE_OAUTH_REDIRECT_URI") or "").strip()


def _allowed_domain() -> str:
    return (os.environ.get("GOOGLE_OAUTH_ALLOWED_DOMAIN") or _DEFAULT_DOMAIN).strip().lower()


def sso_enabled() -> bool:
    """True iff the OAuth client is fully configured. A half-config does NOT
    half-enable SSO — all three of id/secret/redirect must be present."""
    return bool(_client_id() and _client_secret() and _redirect_uri())


# ============= ROLE HOME (mirrors login.html roleHome) =============

def _role_home(role: str) -> str:
    # #263 — pm lands on the assigned-projects-only view; non-company roles never land on
    # the company console (it 403s for them), so default also routes to /projects.
    # #267/#269 — a client goes to /welcome; the grant-aware client gate forwards clients
    # with >=1 granted section on to /portal (zero grants: the welcome hard-stop stays).
    return {"admin": "/admin/users", "c_suite": "/", "pm": "/projects",
            "client": "/welcome",
            "estimator": "/estimating"}.get(role, "/projects")   # #276


# ============= STATE COOKIE (CSRF) =============

def _set_state_cookie(resp, state: str) -> None:
    resp.set_cookie(_STATE_COOKIE, state, max_age=_STATE_TTL, httponly=True,
                    secure=auth._request_is_https(), samesite="Lax", path="/auth/google/")


def _clear_state_cookie(resp) -> None:
    resp.set_cookie(_STATE_COOKIE, "", max_age=0, httponly=True,
                    secure=auth._request_is_https(), samesite="Lax", path="/auth/google/")


# ============= ID-TOKEN VERIFICATION =============

def _verify_google_code(code: str) -> dict | None:
    """Exchange the auth `code` for tokens, then return the VERIFIED ID-token claims
    (dict) — or None on ANY failure. Signature/iss/aud/exp are verified by google-auth
    against Google's published keys (NOT hand-rolled).

    TEST SEAM — GOOGLE_OAUTH_FAKE_VERIFY=1 (test-only, NEVER set in production; mirrors
    the DOC_SCAN_FAKE / EXPENSE_SCAN_FAKE pattern) skips the network and decodes `code`
    as base64url(JSON claims), so the smoke can inject a fully-controlled VERIFIED claim
    without calling Google. The domain + no-auto-provision + status checks still run on
    that claim, so the seam alone cannot authorize a non-company / non-existent user."""
    if runtime_seams.seam("GOOGLE_OAUTH_FAKE_VERIFY") == "1":
        try:
            pad = "=" * (-len(code) % 4)
            return json.loads(base64.urlsafe_b64decode(code + pad).decode("utf-8"))
        except Exception:
            return None
    try:
        # 1) Exchange the single-use code (+ client secret) for tokens, over TLS.
        r = requests.post(_TOKEN_ENDPOINT, data={
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code",
        }, timeout=10)
        if r.status_code != 200:
            logging.info("auth(google): token exchange failed status=%s", r.status_code)
            return None
        id_tok = (r.json() or {}).get("id_token")
        if not id_tok:
            return None
        # 2) Verify signature (Google JWKS), iss, aud==client_id, exp — via google-auth.
        from google.auth.transport import requests as g_requests
        from google.oauth2 import id_token as g_id_token
        claims = g_id_token.verify_oauth2_token(id_tok, g_requests.Request(), _client_id())
        if claims.get("iss") not in _VALID_ISS:   # defense in depth
            return None
        return claims
    except Exception as ex:
        # Never log the token or its contents — only the error class.
        logging.info("auth(google): id-token verification failed: %s", type(ex).__name__)
        return None


# ============= USER LOOKUP / LINK =============

def _lookup_user_by_email(email: str) -> dict | None:
    conn = auth._db()
    try:
        row = conn.execute(
            "SELECT id, email, role, status, is_active, google_sub "
            "FROM users WHERE LOWER(email) = LOWER(?)",
            (email,),
        ).fetchone()
        if not row:
            return None
        return {"id": row["id"], "email": row["email"], "role": row["role"],
                "status": row["status"], "is_active": row["is_active"],
                "google_sub": row["google_sub"]}
    finally:
        conn.close()


def _finalize_link(user_id: int, sub: str, link_sub: bool) -> bool:
    """On first Google sign-in, store google_sub (harden the link) and clear
    must_reset_password (a verified Google identity replaces the temp-password
    rotation — 'must_reset doesn't apply to a Google sign-in'). Returns False if the
    google_sub UNIQUE constraint trips (sub already linked elsewhere) -> reject."""
    conn = auth._db()
    try:
        if link_sub:
            conn.execute(
                "UPDATE users SET google_sub = ?, must_reset_password = 0 WHERE id = ?",
                (sub, user_id))
        else:
            conn.execute(
                "UPDATE users SET must_reset_password = 0 WHERE id = ?", (user_id,))
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


# ============= REJECT HELPER =============

def _reject(reason: str, user_id, ip, audit: bool):
    """Redirect back to /login with a generic, PII-free reason code. When `audit`,
    record a login_fail (method=google) — only for an identity that resolved far
    enough to have (or provably lack) an account; never for mere CSRF/transport noise."""
    if audit:
        auth._login_audit(user_id, "login_fail", ip, method="google")
    resp = redirect(f"/login?sso_error={reason}")
    _clear_state_cookie(resp)
    return resp


# ============= ROUTES =============

def _google_login():
    """GET /auth/google/login — start the flow: set a CSRF state cookie, redirect to
    Google's consent screen. 404 when SSO is disabled."""
    if not sso_enabled():
        return ("Not Found", 404)
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "hd": _allowed_domain(),       # HINT to the account chooser; re-verified on the token
        "access_type": "online",
        "prompt": "select_account",
        "include_granted_scopes": "true",
    }
    resp = redirect(_AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params))
    _set_state_cookie(resp, state)
    return resp


def _google_callback():
    """GET /auth/google/callback — verify state, exchange + verify the ID token,
    enforce the domain rule, require an active admin-created account, link the
    google_sub, and create the #257 session. 404 when SSO is disabled."""
    if not sso_enabled():
        return ("Not Found", 404)
    ip = auth._client_ip()

    # User declined consent / Google returned an error.
    if request.args.get("error"):
        return _reject("denied", None, ip, audit=False)

    # CSRF: the returned state must equal the one we set in the cookie.
    state_param = request.args.get("state") or ""
    state_cookie = request.cookies.get(_STATE_COOKIE) or ""
    if not state_param or not state_cookie or not secrets.compare_digest(state_param, state_cookie):
        logging.info("auth(google): state mismatch")
        return _reject("state", None, ip, audit=False)

    code = request.args.get("code") or ""
    if not code:
        return _reject("verify", None, ip, audit=False)

    claims = _verify_google_code(code)
    if not claims:
        return _reject("verify", None, ip, audit=False)

    # DOMAIN RULE — on the VERIFIED claims only (never the hd URL param).
    email = (claims.get("email") or "").strip().lower()
    email_verified = claims.get("email_verified")
    hd = (claims.get("hd") or "").strip().lower()
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    allowed = _allowed_domain()
    if email_verified is not True or not domain or domain != allowed or hd != allowed:
        logging.info("auth(google): rejected non-company identity")
        return _reject("domain", None, ip, audit=False)

    sub = (claims.get("sub") or "").strip()
    if not sub:
        return _reject("verify", None, ip, audit=False)

    # NO AUTO-PROVISION: authorization comes only from the admin-managed users table.
    user = _lookup_user_by_email(email)
    if not user:
        logging.info("auth(google): no account for verified company identity")
        return _reject("notauthorized", None, ip, audit=True)
    if user["status"] != "active" or not user["is_active"]:
        logging.info("auth(google): account not active")
        return _reject("notauthorized", user["id"], ip, audit=True)
    # Once linked, the email's Google identity is fixed — a different sub is rejected.
    if user["google_sub"] and user["google_sub"] != sub:
        logging.info("auth(google): google_sub mismatch for linked account")
        return _reject("notauthorized", user["id"], ip, audit=True)

    # SUCCESS — link sub on first sign-in, clear must_reset, mint the #257 session.
    if not _finalize_link(user["id"], sub, link_sub=(not user["google_sub"])):
        return _reject("notauthorized", user["id"], ip, audit=True)
    sid = auth.create_session(user["id"], request.headers.get("User-Agent"))
    auth.touch_last_login(user["id"])
    auth._login_audit(user["id"], "login_success", ip, method="google")
    logging.info("auth(google): login ok role=%s sid=%s", user["role"], auth._redact_sid(sid))
    resp = redirect(_role_home(user["role"]))
    auth._set_session_cookie(resp, sid)
    _clear_state_cookie(resp)
    return resp


def _sso_config():
    """GET /api/auth/sso/config — public (login page reads it to show/hide the button).
    No secrets: only whether SSO is enabled + the login URL."""
    enabled = sso_enabled()
    return jsonify({"google": {
        "enabled": enabled,
        "login_url": "/auth/google/login" if enabled else None,
    }})


def register(app) -> None:
    """Wire the Google SSO routes onto `app` (called from server.py after auth_admin).
    The routes exist unconditionally; they 404 when SSO is disabled."""
    app.add_url_rule("/auth/google/login", "auth_google_login", _google_login, methods=["GET"])
    app.add_url_rule("/auth/google/callback", "auth_google_callback", _google_callback, methods=["GET"])
    app.add_url_rule("/api/auth/sso/config", "auth_sso_config", _sso_config, methods=["GET"])
