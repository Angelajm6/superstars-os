"""#289 (Cloud M3) — public-door hardening endpoints: staff TOTP + force_sso, the
staff-missing-2FA banner, and worker-app device provisioning/revocation.

Enrollment is SELF-SERVICE (a staffer enrols their OWN authenticator); force_sso
and worker-device provisioning are ADMIN actions. Everything is server-enforced
and audited. The TOTP secret and recovery plaintext are shown EXACTLY ONCE
(enrollment) and never re-emitted; the DB keeps only the secret (comp-data class)
and bcrypt-hashed recovery codes.
"""
from __future__ import annotations

import json
import logging
import secrets as _secrets

import bcrypt
from flask import g, jsonify, request

import db_layer
import totp as _totp
from auth import _db, _now_iso, _login_audit, _client_ip, current_user, requires_role

STAFF_ROLES = ("admin", "c_suite", "pm", "super", "estimator")


def _staff_only():
    u = current_user() or {}
    if u.get("role") not in STAFF_ROLES:
        return None, (jsonify({"error": "forbidden"}), 403)
    return u, None


# ============================ STAFF TOTP (self-service) ============================

def _qr_data_uri(text: str):
    """The setup QR as an SVG data: URI for an <img> (Salesforce Authenticator and others only scan), or None."""
    try:
        import segno
        return segno.make(text, error="m").svg_data_uri(scale=6, border=2, dark="#222633", light="#ffffff")
    except Exception:   # the key + link still work without the picture
        logging.warning("auth: totp qr unavailable")
        return None


def _api_totp_begin():
    """POST /api/auth/totp/begin — mint a fresh secret + recovery codes and return
    them ONCE (secret, otpauth URI, recovery list) for the caller to scan/save. The
    secret is stored but totp_enabled stays 0 until /confirm proves a working code —
    so an abandoned begin never locks the user into an unverified factor."""
    user, err = _staff_only()
    if err:
        return err
    secret = _totp.generate_secret()
    recovery = _totp.generate_recovery_codes()
    hashed = _totp.hash_recovery_codes(recovery)
    conn = _db()
    try:
        conn.execute("UPDATE users SET totp_secret=?, totp_enabled=0, totp_recovery=? WHERE id=?",
                     (secret, json.dumps(hashed), user["id"]))
        conn.commit()
    finally:
        conn.close()
    logging.info(f"auth: totp begin uid={user['id']}")   # never the secret
    uri = _totp.provisioning_uri(secret, user.get("email") or f"user-{user['id']}")
    return jsonify({"data": {
        "secret": secret,
        "otpauth_uri": uri,
        "qr_svg": _qr_data_uri(uri),   # drawn here (segno), so the secret never goes to an outside QR service
        "recovery_codes": recovery,   # shown ONCE — the caller must save them now
    }})


def _api_totp_confirm():
    """POST /api/auth/totp/confirm — {code}. Verify a live code against the pending
    secret and, on success, flip totp_enabled=1 (the factor is now live for login)."""
    user, err = _staff_only()
    if err:
        return err
    code = (request.get_json(silent=True) or {}).get("code", "")
    conn = _db()
    try:
        row = conn.execute("SELECT totp_secret FROM users WHERE id=?", (user["id"],)).fetchone()
        secret = row["totp_secret"] if row else None
        if not secret:
            return jsonify({"error": "start enrollment first"}), 400
        if not _totp.verify(secret, code):
            return jsonify({"error": "code did not match — check the time on your phone"}), 400
        conn.execute("UPDATE users SET totp_enabled=1 WHERE id=?", (user["id"],))
        conn.commit()
    finally:
        conn.close()
    logging.info(f"auth: totp enabled uid={user['id']}")
    return jsonify({"data": {"enabled": True}})


def _api_totp_disable():
    """POST /api/auth/totp/disable — {code}. A staffer removes their OWN TOTP, or an
    admin removes it for someone (user_id) who lost their device. Self-disable
    requires a live code (prevents a hijacked session from silently dropping 2FA);
    an admin acting for ANOTHER user does not (that IS the recovery path)."""
    actor = current_user() or {}
    if actor.get("role") not in STAFF_ROLES:
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    target_id = data.get("user_id")
    is_admin_for_other = (actor.get("role") in ("admin", "c_suite")
                          and target_id and int(target_id) != actor["id"])
    uid = int(target_id) if is_admin_for_other else actor["id"]
    conn = _db()
    try:
        if not is_admin_for_other:
            row = conn.execute("SELECT totp_secret, totp_enabled FROM users WHERE id=?",
                               (uid,)).fetchone()
            if row and row["totp_enabled"] and not _totp.verify(row["totp_secret"] or "",
                                                                data.get("code", "")):
                return jsonify({"error": "current code required to disable"}), 400
        conn.execute("UPDATE users SET totp_secret=NULL, totp_enabled=0, totp_recovery=NULL "
                     "WHERE id=?", (uid,))
        conn.commit()
    finally:
        conn.close()
    logging.info(f"auth: totp disabled uid={uid} by={actor['id']}")
    return jsonify({"data": {"enabled": False, "user_id": uid}})


# ============================ ADMIN: force SSO ============================

def _api_force_sso():
    """POST /api/admin/users/force-sso — {user_id, on}. admin/c_suite only. Flips a
    staffer's password path off (Google SSO becomes their only way in). Refuses to
    set it on an account with no google_sub yet (that would lock them out) and on a
    non-staff account."""
    actor = current_user() or {}
    if actor.get("role") not in ("admin", "c_suite"):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    uid = data.get("user_id")
    on = bool(data.get("on"))
    if uid is None:
        return jsonify({"error": "user_id required"}), 400
    conn = _db()
    try:
        row = conn.execute("SELECT role, google_sub FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            return jsonify({"error": "user not found"}), 404
        if row["role"] not in STAFF_ROLES:
            return jsonify({"error": "force-sso applies to staff accounts only"}), 400
        if on and not row["google_sub"]:
            return jsonify({"error": "link Google SSO first — this account has no google_sub "
                                     "and would be locked out"}), 400
        conn.execute("UPDATE users SET force_sso=? WHERE id=?", (1 if on else 0, uid))
        conn.commit()
    finally:
        conn.close()
    logging.info(f"auth: force_sso={on} uid={uid} by={actor['id']}")
    return jsonify({"data": {"user_id": uid, "force_sso": on}})


# ============================ ADMIN: the missing-2FA banner ============================

def staff_second_factor_status(conn) -> list:
    """Every real (non-system) STAFF account + whether it has a second factor.
    A factor = TOTP enabled OR google_sub linked OR force_sso (SSO-only implies
    Google's own 2FA). PII-safe: id / email / role / booleans only."""
    ph = ",".join("?" * len(STAFF_ROLES))
    rows = conn.execute(
        f"SELECT id, email, role, totp_enabled, force_sso, google_sub "
        f"FROM users WHERE role IN ({ph}) AND is_active=1 AND status='active' "
        f"AND COALESCE(is_system,0)=0 ORDER BY role, email", tuple(STAFF_ROLES)).fetchall()
    out = []
    for r in rows:
        has = bool(r["totp_enabled"]) or bool(r["google_sub"]) or bool(r["force_sso"])
        out.append({
            "user_id": r["id"], "email": r["email"], "role": r["role"],
            "has_totp": bool(r["totp_enabled"]),
            "has_sso": bool(r["google_sub"]),
            "force_sso": bool(r["force_sso"]),
            "has_second_factor": has,
        })
    return out


def _api_2fa_status():
    """GET /api/admin/2fa-status — admin/c_suite. Feeds the console banner listing
    staff still missing a second factor before flip day."""
    actor = current_user() or {}
    if actor.get("role") not in ("admin", "c_suite"):
        return jsonify({"error": "forbidden"}), 403
    conn = _db()
    try:
        rows = staff_second_factor_status(conn)
    finally:
        conn.close()
    missing = [r for r in rows if not r["has_second_factor"]]
    return jsonify({"data": {"staff": rows, "missing": missing,
                             "missing_count": len(missing), "total": len(rows)}})


# ============================ WORKER DEVICE PROVISIONING ============================

def worker_enforcement_on(conn) -> bool:
    row = conn.execute("SELECT value FROM app_settings WHERE key='worker_device_enforcement'").fetchone()
    return bool(row) and str(row[0]).strip() == "1"


def _api_device_provision():
    """POST /api/admin/worker-devices/provision — {employee_id, label?}. admin/super.
    Mints a ONE-TIME provision code (shown once) bound to a worker; the operator reads
    it into the worker's phone (or shows the QR), which redeems it for a device token.
    Returns the code + an enrollment URL for the QR."""
    actor = current_user() or {}
    if actor.get("role") not in ("admin", "super", "c_suite"):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    emp = (data.get("employee_id") or "").strip()
    label = (data.get("label") or "").strip()[:80] or None
    if not emp:
        return jsonify({"error": "employee_id required"}), 400
    conn = _db()
    try:
        w = conn.execute("SELECT employee_id FROM employees WHERE employee_id=?", (emp,)).fetchone()
        if not w:
            return jsonify({"error": "unknown employee_id"}), 404
        code = f"{_secrets.randbelow(1000000):06d}"   # 6-digit, easy to read aloud
        code_hash = bcrypt.hashpw(code.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
        conn.execute(
            "INSERT INTO worker_device (employee_id, provision_code_hash, label, status, "
            "issued_by, issued_at) VALUES (?,?,?, 'pending', ?, ?)",
            (emp, code_hash, label, actor["id"], _now_iso()))
        conn.commit()
        _login_audit(None, "device_provisioned", _client_ip())
    finally:
        conn.close()
    logging.info(f"auth: device provision issued emp={emp} by={actor['id']}")   # never the code
    return jsonify({"data": {
        "provision_code": code,   # ONCE — read into the worker's phone now
        "employee_id": emp,
        "enroll_url": f"/worker-app?provision={code}",
    }})


def _api_device_redeem():
    """POST /api/worker/device/redeem — {employee_id, provision_code}. PUBLIC (worker
    path): the phone exchanges a valid pending code for an opaque device token, stored
    only in its localStorage. The plaintext token is returned ONCE. Throttled by the
    per-source login limiter shape (a wrong code writes a pin_fail)."""
    data = request.get_json(silent=True) or {}
    emp = (data.get("employee_id") or "").strip()
    code = (data.get("provision_code") or "").strip()
    if not emp or not code:
        return jsonify({"error": "employee_id and provision_code required"}), 400
    ip = _client_ip()
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, provision_code_hash FROM worker_device "
            "WHERE employee_id=? AND status='pending'", (emp,)).fetchall()
        match = None
        for r in rows:
            if bcrypt.checkpw(code.encode("utf-8"), (r["provision_code_hash"] or "").encode("utf-8")):
                match = r
                break
        if not match:
            _login_audit(None, "pin_fail", ip)
            return jsonify({"error": "invalid or expired provisioning code"}), 401
        token = _secrets.token_urlsafe(24)
        token_hash = bcrypt.hashpw(token.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
        conn.execute(
            "UPDATE worker_device SET token_hash=?, provision_code_hash=NULL, status='active', "
            "redeemed_at=?, last_seen_at=? WHERE id=?",
            (token_hash, _now_iso(), _now_iso(), match["id"]))
        conn.commit()
    finally:
        conn.close()
    logging.info(f"auth: device redeemed emp={emp}")
    return jsonify({"data": {"device_token": token, "employee_id": emp}})


def device_valid_for(conn, employee_id, device_token) -> bool:
    """True iff `device_token` is an ACTIVE device for `employee_id`. Bumps
    last_seen_at on a hit. Used by the PIN login gate when enforcement is on."""
    if not employee_id or not device_token:
        return False
    rows = conn.execute(
        "SELECT id, token_hash FROM worker_device WHERE employee_id=? AND status='active' "
        "AND token_hash IS NOT NULL", (employee_id,)).fetchall()
    for r in rows:
        try:
            if bcrypt.checkpw(device_token.encode("utf-8"), r["token_hash"].encode("utf-8")):
                conn.execute("UPDATE worker_device SET last_seen_at=? WHERE id=?",
                             (_now_iso(), r["id"]))
                conn.commit()
                return True
        except Exception:
            continue
    return False


def _api_device_list():
    """GET /api/admin/worker-devices[?employee_id=] — admin/super. Console list for
    revocation. PII-safe: device rows carry employee_id + status + timestamps only
    (never the token)."""
    actor = current_user() or {}
    if actor.get("role") not in ("admin", "super", "c_suite"):
        return jsonify({"error": "forbidden"}), 403
    emp = (request.args.get("employee_id") or "").strip()
    conn = _db()
    try:
        if emp:
            rows = conn.execute(
                "SELECT id, employee_id, label, status, issued_at, redeemed_at, revoked_at, "
                "last_seen_at FROM worker_device WHERE employee_id=? ORDER BY id DESC",
                (emp,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, employee_id, label, status, issued_at, redeemed_at, revoked_at, "
                "last_seen_at FROM worker_device ORDER BY id DESC LIMIT 500").fetchall()
        enforcement = worker_enforcement_on(conn)
    finally:
        conn.close()
    return jsonify({"data": {"devices": [dict(r) for r in rows],
                             "enforcement_on": enforcement}})


def _api_device_revoke():
    """POST /api/admin/worker-devices/revoke — {device_id}. admin/super. Kills a
    device: the token stops working immediately (status='revoked')."""
    actor = current_user() or {}
    if actor.get("role") not in ("admin", "super", "c_suite"):
        return jsonify({"error": "forbidden"}), 403
    did = (request.get_json(silent=True) or {}).get("device_id")
    if did is None:
        return jsonify({"error": "device_id required"}), 400
    conn = _db()
    try:
        conn.execute("UPDATE worker_device SET status='revoked', token_hash=NULL, revoked_at=? "
                     "WHERE id=?", (_now_iso(), did))
        conn.commit()
        _login_audit(None, "device_revoked", _client_ip())
    finally:
        conn.close()
    logging.info(f"auth: device revoked id={did} by={actor['id']}")
    return jsonify({"data": {"device_id": did, "status": "revoked"}})


def _api_device_enforcement():
    """POST /api/admin/worker-devices/enforcement — {on}. admin/c_suite. Flips the
    global enforcement flag the operator turns ON after the crew's phones are
    provisioned (a hard pre-M5 gate). Default is OFF so no deploy strands the field."""
    actor = current_user() or {}
    if actor.get("role") not in ("admin", "c_suite"):
        return jsonify({"error": "forbidden"}), 403
    on = bool((request.get_json(silent=True) or {}).get("on"))
    conn = _db()
    try:
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('worker_device_enforcement', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP"
                     if not db_layer.is_postgres() else
                     "INSERT INTO app_settings (key, value) VALUES ('worker_device_enforcement', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=CURRENT_TIMESTAMP",
                     ("1" if on else "0",))
        conn.commit()
    finally:
        conn.close()
    logging.info(f"auth: worker enforcement={on} by={actor['id']}")
    return jsonify({"data": {"enforcement_on": on}})


def register(app) -> None:
    """Wire #289 endpoints. MUST follow apply_auth_gate. The redeem route lives on
    the public /api/worker/ prefix (a phone enrolls while logged out); everything
    else is behind the login gate + its own role check."""
    # NOT under /api/auth/ — that prefix is auth-EXEMPT (the login/logout/sso path),
    # and these need the caller's authenticated session. /api/2fa/* passes through
    # the normal login gate so current_user() resolves.
    app.add_url_rule("/api/2fa/begin", "totp_begin", _api_totp_begin, methods=["POST"])
    app.add_url_rule("/api/2fa/confirm", "totp_confirm", _api_totp_confirm, methods=["POST"])
    app.add_url_rule("/api/2fa/disable", "totp_disable", _api_totp_disable, methods=["POST"])
    app.add_url_rule("/api/admin/users/force-sso", "force_sso", _api_force_sso, methods=["POST"])
    app.add_url_rule("/api/admin/2fa-status", "twofa_status", _api_2fa_status, methods=["GET"])
    app.add_url_rule("/api/admin/worker-devices/provision", "device_provision",
                     _api_device_provision, methods=["POST"])
    app.add_url_rule("/api/worker/device/redeem", "device_redeem",
                     _api_device_redeem, methods=["POST"])
    app.add_url_rule("/api/admin/worker-devices", "device_list", _api_device_list, methods=["GET"])
    app.add_url_rule("/api/admin/worker-devices/revoke", "device_revoke",
                     _api_device_revoke, methods=["POST"])
    app.add_url_rule("/api/admin/worker-devices/enforcement", "device_enforcement",
                     _api_device_enforcement, methods=["POST"])
