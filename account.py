"""Account — each signed-in staff member sees and edits their OWN account (Settings > Account).

Replaces the old "Preferences & security" panel on the console (/#settings) and its
"Interface" card. Every route acts on the caller's own row only; nothing here takes a
user id. Admins still change emails, roles and other people's access in Team & access.

  GET  /account                                  the page (every staff role)
  GET  /api/account                              name, email, role, dates, sign-in methods
  PUT  /api/account/profile                      {full_name, display_name}
  POST /api/account/password                     {current_password, new_password} — signs out
                                                 every other device
  GET  /api/account/sessions                     where this account is signed in
  POST /api/account/sessions/<key>/sign-out      sign one other device out
  POST /api/account/sessions/sign-out-others     sign every other device out

Two-step sign-in keeps its own routes (/api/2fa/*, auth_hardening.py). A session is named
by a short hash of its id: the id itself never leaves the server. Logs carry user ids and
counts only — never names, emails, passwords or session ids.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from flask import jsonify, request, send_file

import auth
from auth import COOKIE_NAME, current_user, requires_role

log = logging.getLogger("ssc.account")

ROLES = ("admin", "c_suite", "pm", "super", "estimator")
PAGE = Path(__file__).resolve().parent / "account.html"
MAX_NAME = 120
MAX_DISPLAY = 60
PW_FAIL_LIMIT = 5                       # wrong current passwords per window, then 429
PW_FAIL_WINDOW = timedelta(minutes=15)
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(v, limit: int) -> str:
    return _CTRL.sub("", str(v or "")).strip()[:limit + 1]


def _key(sid: str) -> str:
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]


def device_label(ua: str) -> str:
    """'Chrome on Mac' from a user agent — coarse, for people to recognise their devices."""
    ua = ua or ""
    if "Edg/" in ua:
        browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "Opera"
    elif "Firefox/" in ua or "FxiOS" in ua:
        browser = "Firefox"
    elif "Chrome/" in ua or "CriOS" in ua:
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = "Browser"
    if "iPhone" in ua:
        system = "iPhone"
    elif "iPad" in ua:
        system = "iPad"
    elif "Android" in ua:
        system = "Android"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        system = "Mac"
    elif "Windows" in ua:
        system = "Windows"
    elif "Linux" in ua:
        system = "Linux"
    else:
        return browser if browser != "Browser" else "Unknown device"
    return f"{browser} on {system}"


def _row(conn, uid: int) -> dict:
    cols = ("id, email, role, full_name, display_name, created_at, last_login_at, employee_id_link, "
            "password_hash, google_sub, totp_enabled, force_sso")
    try:
        r = conn.execute(f"SELECT {cols} FROM users WHERE id = ?", (uid,)).fetchone()
    except Exception:   # a database without the SSO / 2FA columns yet
        conn.rollback()
        r = conn.execute("SELECT id, email, role, full_name, display_name, created_at, last_login_at, "
                         "employee_id_link, password_hash FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(r) if r else {}


def _has_password(row: dict) -> bool:
    h = row.get("password_hash") or ""
    return h.startswith("$2") and not row.get("force_sso")


def _sid() -> str:
    return request.cookies.get(COOKIE_NAME) or ""


# ------------------------------------------------------------------ routes
@requires_role(*ROLES)
def _page():
    resp = send_file(str(PAGE))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@requires_role(*ROLES)
def _api_get():
    u = current_user()
    conn = auth._db()
    try:
        r = _row(conn, u["id"])
    finally:
        conn.close()
    return jsonify({"data": {
        "id": r["id"], "email": r["email"], "role": r["role"],
        "full_name": r.get("full_name") or "", "display_name": r.get("display_name") or r.get("full_name") or "",
        "created_at": r.get("created_at"), "last_login_at": r.get("last_login_at"),
        "worker_linked": bool(r.get("employee_id_link")),
        "password_sign_in": _has_password(r), "google_sign_in": bool(r.get("google_sub")),
        "sso_only": bool(r.get("force_sso")), "totp_enabled": bool(r.get("totp_enabled")),
    }})


@requires_role(*ROLES)
def _api_profile():
    u = current_user()
    body = request.get_json(silent=True) or {}
    full = _clean(body.get("full_name"), MAX_NAME)
    disp = _clean(body.get("display_name"), MAX_DISPLAY)
    if not full:
        return jsonify({"error": "Enter your full name."}), 400
    if len(full) > MAX_NAME:
        return jsonify({"error": f"Full name can be at most {MAX_NAME} characters."}), 400
    if len(disp) > MAX_DISPLAY:
        return jsonify({"error": f"Display name can be at most {MAX_DISPLAY} characters."}), 400
    conn = auth._db()
    try:
        conn.execute("UPDATE users SET full_name = ?, display_name = ? WHERE id = ?", (full, disp or full, u["id"]))
        conn.commit()
    finally:
        conn.close()
    log.info("account: profile updated uid=%s", u["id"])
    return jsonify({"data": {"full_name": full, "display_name": disp or full}})


def ensure(conn) -> None:
    """Wrong current-password attempts (the login_audit event list is a CHECK constraint;
    this stays out of it). Rows older than the window are pruned on each write."""
    conn.execute("CREATE TABLE IF NOT EXISTS account_pw_fail (user_id INTEGER NOT NULL, at TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_account_pw_fail ON account_pw_fail(user_id, at)")
    conn.commit()


def _since() -> str:
    return (datetime.now() - PW_FAIL_WINDOW).isoformat(timespec="seconds")


def _recent_pw_fails(uid: int) -> int:
    conn = auth._db()
    try:
        r = conn.execute("SELECT COUNT(1) FROM account_pw_fail WHERE user_id = ? AND at >= ?", (uid, _since())).fetchone()
        return r[0] if r else 0
    except Exception:
        return 0
    finally:
        conn.close()


def _note_pw_fail(uid: int) -> None:
    conn = auth._db()
    try:
        conn.execute("DELETE FROM account_pw_fail WHERE at < ?", (_since(),))
        conn.execute("INSERT INTO account_pw_fail (user_id, at) VALUES (?, ?)", (uid, auth._now_iso()))
        conn.commit()
    except Exception as e:
        log.warning("account: password fail not counted: %s", type(e).__name__)
    finally:
        conn.close()


@requires_role(*ROLES)
def _api_password():
    u = current_user()
    body = request.get_json(silent=True) or {}
    cur, new = body.get("current_password") or "", body.get("new_password") or ""
    conn = auth._db()
    try:
        r = _row(conn, u["id"])
    finally:
        conn.close()
    if not _has_password(r):
        return jsonify({"error": "Your account signs in with Google, so there is no password to change."}), 409
    if _recent_pw_fails(u["id"]) >= PW_FAIL_LIMIT:
        return jsonify({"error": "Too many wrong passwords. Try again in 15 minutes."}), 429
    if not auth.verify_password(cur, r["password_hash"]):
        _note_pw_fail(u["id"])
        return jsonify({"error": "Your current password is not right."}), 400
    err = auth.password_strength_error(new)
    if err:
        return jsonify({"error": err}), 400
    if new == cur:
        return jsonify({"error": "Pick a password different from the current one."}), 400
    sid = _sid()
    conn = auth._db()
    try:
        conn.execute("UPDATE users SET password_hash = ?, must_reset_password = 0 WHERE id = ?", (auth.hash_password(new), u["id"]))
        n = conn.execute("DELETE FROM sessions WHERE user_id = ? AND id <> ?", (u["id"], sid)).rowcount
        conn.commit()
    finally:
        conn.close()
    auth._login_audit(u["id"], "password_set", auth._client_ip())
    log.info("account: password changed uid=%s other_sessions_ended=%s", u["id"], n)   # never the password
    return jsonify({"data": {"ok": True, "signed_out": n or 0}})


@requires_role(*ROLES)
def _api_sessions():
    u = current_user()
    me = _sid()
    now = auth._now_iso()
    conn = auth._db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, created_at, last_used_at, expires_at, user_agent FROM sessions WHERE user_id = ? ORDER BY last_used_at DESC",
            (u["id"],)).fetchall()]
    finally:
        conn.close()
    out = [{"key": _key(r["id"]), "device": device_label(r.get("user_agent")), "current": r["id"] == me,
            "signed_in_at": str(r.get("created_at") or ""), "last_used_at": str(r.get("last_used_at") or "")}
           for r in rows if str(r.get("expires_at") or "") >= now]
    cur = [s for s in out if s["current"]]
    rest = sorted([s for s in out if not s["current"]], key=lambda s: s["last_used_at"], reverse=True)
    return jsonify({"data": cur + rest})


def _end(uid: int, keep_sid: str, only_key: str = "") -> int:
    conn = auth._db()
    try:
        ids = [r["id"] for r in conn.execute("SELECT id FROM sessions WHERE user_id = ?", (uid,)).fetchall()]
        gone = [s for s in ids if s != keep_sid and (not only_key or _key(s) == only_key)]
        for s in gone:
            conn.execute("DELETE FROM sessions WHERE id = ? AND user_id = ?", (s, uid))
        conn.commit()
        return len(gone)
    finally:
        conn.close()


@requires_role(*ROLES)
def _api_sign_out_one(key):
    u = current_user()
    me = _sid()
    if me and _key(me) == key:
        return jsonify({"error": "Use Sign out in the top bar to sign out of this device."}), 400
    n = _end(u["id"], me, key)
    if not n:
        return jsonify({"error": "That device is already signed out."}), 404
    log.info("account: signed out one device uid=%s", u["id"])
    return jsonify({"data": {"signed_out": n}})


@requires_role(*ROLES)
def _api_sign_out_others():
    u = current_user()
    n = _end(u["id"], _sid())
    log.info("account: signed out other devices uid=%s n=%s", u["id"], n)
    return jsonify({"data": {"signed_out": n}})


def register(app) -> None:
    conn = auth._db()
    try:
        ensure(conn)
    except Exception as e:
        log.warning("account schema ensure skipped: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    app.add_url_rule("/account", "account_page", _page, methods=["GET"])
    app.add_url_rule("/api/account", "account_get", _api_get, methods=["GET"])
    app.add_url_rule("/api/account/profile", "account_profile", _api_profile, methods=["PUT"])
    app.add_url_rule("/api/account/password", "account_password", _api_password, methods=["POST"])
    app.add_url_rule("/api/account/sessions", "account_sessions", _api_sessions, methods=["GET"])
    app.add_url_rule("/api/account/sessions/<key>/sign-out", "account_sign_out_one", _api_sign_out_one, methods=["POST"])
    app.add_url_rule("/api/account/sessions/sign-out-others", "account_sign_out_others", _api_sign_out_others, methods=["POST"])
