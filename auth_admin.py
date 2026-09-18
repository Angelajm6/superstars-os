"""Admin account management — multi-user accounts & roles, Phase 1 (#257).

ADMIN-ONLY surface, gated SERVER-SIDE on every endpoint (@requires_role('admin')).
Hiding a UI control is NOT access control — a crafted request from a lower role
is rejected here with 403, never served the data.

CORE SECURITY PRINCIPLE: a user can NEVER set or change their OWN role; role is
assigned by an admin only. Enforced below (a) by gating to admin, and (b) by
refusing self-role-change.

SINGLE-ADMIN INVARIANT (operator: "only one admin, which I have"):
  * REFUSE to create a second admin (admin is not a creatable role).
  * REFUSE to elevate anyone to admin.
  * REFUSE to deactivate or downgrade the SOLE/last active admin, and refuse an
    admin acting on their OWN account (can't self-downgrade or self-deactivate).

Role catalog is the 7-role set in the users CHECK. ONBOARDABLE this phase = the
internal tier minus admin = c_suite + pm. super + the external three (client,
architect, vendor) are DEFINED-not-onboarded (no request-access intake here).

PII discipline: temp passwords are returned to the admin ONCE in the create/reset
response and NEVER logged. password_hash is never emitted. Audit writes go to
role_change_audit + login_audit (LOCAL timestamps, never UTC).
"""
from __future__ import annotations

import logging
import re
import secrets
import sqlite3
import string
from pathlib import Path

from flask import jsonify, request, send_file

from auth import (_client_ip, _db, _login_audit, _now_iso, current_user,
                  destroy_all_user_sessions, hash_password, password_strength_error,
                  requires_role)

SCRIPT_DIR = Path(__file__).resolve().parent
ADMIN_USERS_PAGE = SCRIPT_DIR / "admin_users.html"

ROLE_CATALOG = ('admin', 'c_suite', 'pm', 'super', 'client', 'architect', 'vendor',
                'estimator')   # #276 — the estimating seat (blueprint §5)
# Onboardable via the UI this phase: the internal tier minus admin (single-admin
# invariant) + the external `client` (#264 — read-only portal; email/password, forced
# first-login reset) + `estimator` (#276 — lands on /estimating, works the queue).
# super + architect/vendor stay defined-not-onboarded for now.
# #280 — `architect` becomes onboardable: an outside architect/engineer gets a login,
# is contained to the drawing-markup surface (elevation._architect_gate) and scoped to
# ASSIGNED PROJECTS ONLY via the existing #263 pm_project_assignment grant. Creating the
# account grants nothing on its own — with no assignment they see no project at all.
ONBOARDABLE_ROLES = ('c_suite', 'pm', 'client', 'estimator', 'architect')
# Assignable as a role-change target: anything in the catalog EXCEPT admin
# (no elevation to admin, ever).
# #305 — 'vendor' has no containment gate anywhere yet; it is not assignable until it does.
ASSIGNABLE_ROLES = tuple(r for r in ROLE_CATALOG if r not in ('admin', 'vendor'))
# #309 — a C-suite account may issue a one-time password for THESE roles only (a locked-out
# client must not need the single admin login). Creating accounts, changing roles and
# deactivating stay admin-only; the admin keeps resetting any non-admin account.
CSUITE_RESET_ROLES = ('client',)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TEMP_ALPHABET = string.ascii_letters + string.digits  # no symbols — conveyable by hand


def _gen_temp_password(n: int = 16) -> str:
    """A strong one-time temp password (letters+digits; always has >=1 of each so it
    also satisfies the strength rule). Shown to the admin once, never stored/logged."""
    while True:
        pw = "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(n))
        if any(c.isalpha() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def _active_admin_count(conn) -> int:
    return conn.execute(
        # #258 — count only REAL admins (is_system=0). Test fixtures (is_system=1)
        # are exempt from the single-admin invariant.
        "SELECT COUNT(1) FROM users WHERE role='admin' AND status='active' "
        "AND is_active=1 AND COALESCE(is_system,0)=0"
    ).fetchone()[0]


def _get_user(conn, user_id: int):
    return conn.execute(
        "SELECT id, email, role, full_name, display_name, status, is_active, is_system, "
        "       must_reset_password, last_login_at, created_at, created_by, deactivated_at "
        "FROM users WHERE id = ?", (user_id,)).fetchone()


def _public_user(row) -> dict:
    """Admin-facing user view. NEVER includes password_hash."""
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"] or row["full_name"],
        "role": row["role"],
        "status": row["status"],
        "must_reset_password": bool(row["must_reset_password"]),
        "last_login_at": row["last_login_at"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
    }


def _role_change_audit(conn, user_id, old_role, new_role, changed_by, reason):
    conn.execute(
        "INSERT INTO role_change_audit (user_id, old_role, new_role, changed_by, at, reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, old_role, new_role, changed_by, _now_iso(), reason))


# ============= ENDPOINTS =============

def _list_users():
    conn = _db()
    try:
        # #258 — show ONLY real accounts. Test fixtures (is_system=1) are hidden so
        # the operator never sees seeded admins. The filter keys on the FLAG, not on
        # email guessing: a rogue admin created some other way has is_system=0 and
        # WOULD still appear here.
        # #309 — a C-suite viewer sees ONLY the accounts they may act on (clients).
        limited = (current_user() or {}).get("role") != "admin"
        where = "COALESCE(is_system,0)=0"
        params: tuple = ()
        if limited:
            where += " AND role IN (%s)" % ",".join("?" * len(CSUITE_RESET_ROLES))
            params = tuple(CSUITE_RESET_ROLES)
        rows = conn.execute(
            "SELECT id, email, role, full_name, display_name, status, is_active, "
            "       must_reset_password, last_login_at, created_at, created_by, deactivated_at "
            f"FROM users WHERE {where} ORDER BY "
            "CASE role WHEN 'admin' THEN 0 WHEN 'c_suite' THEN 1 WHEN 'pm' THEN 2 ELSE 3 END, "
            "LOWER(email)", params).fetchall()
        return jsonify({"data": [_public_user(r) for r in rows], "limited": limited})
    finally:
        conn.close()


def _create_user():
    actor = current_user()
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    display_name = (data.get("display_name") or "").strip()
    role = (data.get("role") or "").strip()
    if not email or not _EMAIL_RE.match(email):
        return jsonify({"error": "a valid email is required"}), 400
    if not display_name:
        return jsonify({"error": "display name is required"}), 400
    # SINGLE-ADMIN INVARIANT + onboarding policy (server-side, not just the dropdown).
    if role == "admin":
        return jsonify({"error": "cannot create an admin — the single-admin invariant forbids a second admin"}), 403
    if role not in ONBOARDABLE_ROLES:
        return jsonify({"error": f"role '{role}' is not onboardable in this phase (allowed: {', '.join(ONBOARDABLE_ROLES)})"}), 400
    temp_pw = _gen_temp_password()
    conn = _db()
    try:
        try:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, role, full_name, display_name, "
                "                   is_active, status, must_reset_password, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, 'active', 1, ?, ?)",
                (email, hash_password(temp_pw), role, display_name, display_name,
                 (actor or {}).get("email"), _now_iso()))
            conn.commit()
        except sqlite3.IntegrityError:
            return jsonify({"error": "a user with that email already exists"}), 409
        row = _get_user(conn, cur.lastrowid)
    finally:
        conn.close()
    logging.info(f"admin: created user id={row['id']} role={role} by={(actor or {}).get('id')}")  # never the temp pw
    # temp_password returned ONCE for the admin to convey; never logged, never stored in plaintext.
    return jsonify({"data": _public_user(row), "temp_password": temp_pw}), 201


def _change_role(user_id: int):
    actor = current_user()
    data = request.get_json(silent=True) or {}
    new_role = (data.get("new_role") or "").strip()
    reason = (data.get("reason") or "").strip() or None
    if actor and actor.get("id") == user_id:
        return jsonify({"error": "you cannot change your own role"}), 403
    if new_role == "admin":
        return jsonify({"error": "cannot elevate to admin — the single-admin invariant forbids it"}), 403
    if new_role not in ASSIGNABLE_ROLES:
        return jsonify({"error": f"invalid role (assignable: {', '.join(ASSIGNABLE_ROLES)})"}), 400
    conn = _db()
    try:
        target = _get_user(conn, user_id)
        if not target:
            return jsonify({"error": "user not found"}), 404
        old_role = target["role"]
        if old_role == new_role:
            return jsonify({"data": _public_user(target), "unchanged": True})
        # Protect the last admin: downgrading an admin can never leave zero admins.
        if old_role == "admin" and _active_admin_count(conn) <= 1:
            return jsonify({"error": "cannot downgrade the sole admin"}), 403
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
        _role_change_audit(conn, user_id, old_role, new_role, (actor or {}).get("id"), reason)
        conn.commit()
        row = _get_user(conn, user_id)
    finally:
        conn.close()
    logging.info(f"admin: role change user_id={user_id} {old_role}->{new_role} by={(actor or {}).get('id')}")
    # Takes effect on the target's NEXT request — role is re-read from users on every
    # request (the session stores only user_id; no cached role to trust).
    return jsonify({"data": _public_user(row)})


def _deactivate_user(user_id: int):
    actor = current_user()
    if actor and actor.get("id") == user_id:
        return jsonify({"error": "you cannot deactivate your own account"}), 403
    conn = _db()
    try:
        target = _get_user(conn, user_id)
        if not target:
            return jsonify({"error": "user not found"}), 404
        if target["role"] == "admin" and _active_admin_count(conn) <= 1:
            return jsonify({"error": "cannot deactivate the sole admin"}), 403
        conn.execute(
            "UPDATE users SET status='disabled', is_active=0, deactivated_at=? WHERE id=?",
            (_now_iso(), user_id))
        conn.commit()
    finally:
        conn.close()
    # Kill the user's sessions NOW — deactivation blocks login AND drops the in-flight
    # session (the gate also re-checks status on every request as a second layer).
    destroy_all_user_sessions(user_id)
    logging.info(f"admin: deactivated user_id={user_id} by={(actor or {}).get('id')}")
    conn = _db()
    try:
        return jsonify({"data": _public_user(_get_user(conn, user_id))})
    finally:
        conn.close()


def _reactivate_user(user_id: int):
    actor = current_user()
    conn = _db()
    try:
        target = _get_user(conn, user_id)
        if not target:
            return jsonify({"error": "user not found"}), 404
        conn.execute(
            "UPDATE users SET status='active', is_active=1, deactivated_at=NULL WHERE id=?",
            (user_id,))
        conn.commit()
        row = _get_user(conn, user_id)
    finally:
        conn.close()
    logging.info(f"admin: reactivated user_id={user_id} by={(actor or {}).get('id')}")
    return jsonify({"data": _public_user(row)})


def _reset_password(user_id: int):
    actor = current_user()
    temp_pw = _gen_temp_password()
    conn = _db()
    try:
        target = _get_user(conn, user_id)
        if not target:
            return jsonify({"error": "user not found"}), 404
        # #309 — the admin row is never reset from the page (single-admin invariant,
        # managed out-of-band); a non-admin actor may reset CSUITE_RESET_ROLES only.
        trole = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()["role"]
        if trole == "admin":
            return jsonify({"error": "the admin account is managed out-of-band"}), 403
        if (actor or {}).get("role") != "admin" and trole not in CSUITE_RESET_ROLES:
            logging.info(f"admin: reset denied actor_role={(actor or {}).get('role')} "
                         f"target_role={trole} user_id={user_id}")
            return jsonify({"error": "a C-suite account can reset client passwords only"}), 403
        conn.execute(
            "UPDATE users SET password_hash=?, must_reset_password=1 WHERE id=?",
            (hash_password(temp_pw), user_id))
        conn.commit()
        row = _get_user(conn, user_id)
    finally:
        conn.close()
    destroy_all_user_sessions(user_id)          # force re-login with the temp password
    _login_audit(user_id, "password_reset", _client_ip())
    logging.info(f"admin: reset password user_id={user_id} by={(actor or {}).get('id')} "
                 f"actor_role={(actor or {}).get('role')}")  # never the temp pw
    return jsonify({"data": _public_user(row), "temp_password": temp_pw})


def _admin_users_page():
    if not ADMIN_USERS_PAGE.exists():
        return jsonify({"error": "admin page not found"}), 404
    import ui_version                                        # #279
    resp = send_file(str(ui_version.resolve_page(ADMIN_USERS_PAGE)))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def register(app) -> None:
    """Wire the admin user-management page + APIs. EVERY endpoint is admin-only,
    server-side — the before_request login gate runs first, then @requires_role('admin')."""
    # #309 — the page, the list and reset-password open to c_suite (limited to client
    # accounts, enforced inside each handler); everything else stays admin-only.
    app.add_url_rule("/admin/users", "admin_users_page",
                     requires_role('admin', 'c_suite')(_admin_users_page), methods=["GET"])
    app.add_url_rule("/api/admin/users", "admin_users_list",
                     requires_role('admin', 'c_suite')(_list_users), methods=["GET"])
    app.add_url_rule("/api/admin/users", "admin_users_create",
                     requires_role('admin')(_create_user), methods=["POST"])
    app.add_url_rule("/api/admin/users/<int:user_id>/role", "admin_users_role",
                     requires_role('admin')(_change_role), methods=["POST"])
    app.add_url_rule("/api/admin/users/<int:user_id>/deactivate", "admin_users_deactivate",
                     requires_role('admin')(_deactivate_user), methods=["POST"])
    app.add_url_rule("/api/admin/users/<int:user_id>/reactivate", "admin_users_reactivate",
                     requires_role('admin')(_reactivate_user), methods=["POST"])
    app.add_url_rule("/api/admin/users/<int:user_id>/reset-password", "admin_users_reset_pw",
                     requires_role('admin', 'c_suite')(_reset_password), methods=["POST"])
