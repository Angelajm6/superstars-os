#!/usr/bin/env python3
"""Make admin — never-zero-admins replaces the single-admin rule. Behavioral guard.

Minimal Flask app (auth gate + auth_admin) on an ISOLATED database. Proves:

  * POST /api/admin/users/<id>/make-admin is admin-only: c_suite / pm / anonymous refused
  * confirm=true is required; only an ACTIVE C-suite account that has set its own
    password can be promoted (pm, client, disabled, must-reset -> 403); not yourself
  * a promotion writes role_change_audit (c_suite -> admin, with the reason) and the
    new admin can use admin-only endpoints on their very next request
  * admin is still never a create role, and the generic role route never targets admin
  * two admins: either may move the OTHER off admin or deactivate them; nobody may act
    on themselves; the last admin can never be downgraded or deactivated
  * the page carries the Make admin control and its confirmation dialog

Run:  venv/bin/python tests/smoke_make_admin.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_mkadmin_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import auth_admin  # noqa: E402
import db_layer  # noqa: E402

TAG = "mkadm"
PW = "MakeAdminSmoke2026"
# key -> (role, status, must_reset)
SEED = {
    "admin": ("admin", "active", 0),
    "owner": ("c_suite", "active", 0),
    "cs_new": ("c_suite", "active", 1),
    "cs_off": ("c_suite", "disabled", 0),
    "pm": ("pm", "active", 0),
    "client": ("client", "active", 0),
}


def _db():
    return db_layer.connect(pragma_fk=True)


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for key, (role, status, must_reset) in SEED.items():
            email = f"{TAG}.{key}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, display_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                         (email, auth.hash_password(PW), role, f"Smoke {key}", f"Smoke {key}",
                          1 if status == "active" else 0, status, must_reset))
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.execute("DELETE FROM login_audit WHERE ip LIKE '10.41.%'")
        conn.commit()
        purge_users(conn, ids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(app, key):
    c = app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@example.com", "password": PW},
               environ_base={"REMOTE_ADDR": "10.41.0.1"})
    assert r.status_code == 200, (key, r.status_code, r.get_data(as_text=True)[:200])
    return c


def _role(uid):
    conn = _db()
    try:
        return conn.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()["role"]
    finally:
        conn.close()


def _mk(client, uid, **body):
    body.setdefault("confirm", True)
    return client.post(f"/api/admin/users/{uid}/make-admin", json=body)


def main() -> int:
    app = Flask("mkadmin", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    auth_admin.register(app)
    ids = _seed()
    passed = 0

    # ---- who may call it
    assert app.test_client().post(f"/api/admin/users/{ids['owner']}/make-admin", json={"confirm": True}).status_code == 401
    owner = _login(app, "owner")
    assert _mk(owner, ids["owner"]).status_code == 403          # c_suite cannot self-promote
    assert _mk(_login(app, "pm"), ids["owner"]).status_code == 403
    assert _role(ids["owner"]) == "c_suite"
    passed += 1; print("  admin-only ok")

    # ---- who may be promoted
    admin = _login(app, "admin")
    assert _mk(admin, ids["owner"], confirm=False).status_code == 400
    assert admin.post(f"/api/admin/users/{ids['owner']}/make-admin", json={}).status_code == 400
    assert _mk(admin, ids["admin"]).status_code == 403          # yourself
    for key in ("pm", "client", "cs_off", "cs_new"):
        r = _mk(admin, ids[key])
        assert r.status_code == 403, (key, r.status_code)
        assert _role(ids[key]) == SEED[key][0], key
    assert "set their own password" in _mk(admin, ids["cs_new"]).get_json()["error"]
    assert _mk(admin, 99999999).status_code == 404
    passed += 1; print("  eligibility ok")

    # ---- admin still never created, never reached through the generic role route
    r = admin.post("/api/admin/users", json={"display_name": "x", "email": f"{TAG}.x@example.com", "role": "admin"})
    assert r.status_code == 403
    assert admin.post(f"/api/admin/users/{ids['owner']}/role", json={"new_role": "admin"}).status_code == 403
    assert _role(ids["owner"]) == "c_suite"
    passed += 1; print("  no back doors ok")

    # ---- the promotion
    r = _mk(admin, ids["owner"], reason="owner")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["data"]["role"] == "admin" and _role(ids["owner"]) == "admin"
    assert _mk(admin, ids["owner"]).get_json().get("unchanged") is True
    conn = _db()
    try:
        a = conn.execute("SELECT old_role, new_role, changed_by, reason FROM role_change_audit "
                         "WHERE user_id = ? ORDER BY id DESC", (ids["owner"],)).fetchone()
    finally:
        conn.close()
    assert (a["old_role"], a["new_role"], a["changed_by"], a["reason"]) == ("c_suite", "admin", ids["admin"], "owner")
    # the SAME session the owner already had is an admin on its next request
    j = owner.get("/api/admin/users").get_json()
    assert j["limited"] is False
    assert sum(1 for u in j["data"] if u["role"] == "admin") >= 2
    passed += 1; print("  promotion + audit ok")

    # ---- two admins: act on each other, never on yourself, never below one
    assert owner.post(f"/api/admin/users/{ids['owner']}/role", json={"new_role": "c_suite"}).status_code == 403
    assert owner.post(f"/api/admin/users/{ids['owner']}/deactivate").status_code == 403
    r = owner.post(f"/api/admin/users/{ids['admin']}/role", json={"new_role": "c_suite"})
    assert r.status_code == 200 and _role(ids["admin"]) == "c_suite"
    # the original admin is now C-suite: admin-only routes refuse them
    assert _mk(admin, ids["owner"]).status_code == 403
    # promote back; an admin may deactivate / reactivate ANOTHER admin
    assert _mk(owner, ids["admin"]).status_code == 200
    admin = _login(app, "admin")
    assert admin.post(f"/api/admin/users/{ids['owner']}/deactivate").status_code == 200
    assert owner.get("/api/admin/users").status_code == 401        # session killed
    assert admin.post(f"/api/admin/users/{ids['owner']}/reactivate").status_code == 200
    # a disabled admin can be moved off admin (it takes no active admin away)
    assert admin.post(f"/api/admin/users/{ids['owner']}/deactivate").status_code == 200
    r = admin.post(f"/api/admin/users/{ids['owner']}/role", json={"new_role": "c_suite"})
    assert r.status_code == 200 and _role(ids["owner"]) == "c_suite"
    passed += 1; print("  two-admin rules ok")

    # ---- the page
    html = (ROOT / "admin_users.html").read_text(encoding="utf-8")
    for needle in ('id="ov-mkadmin"', 'data-mkadmin="', "/make-admin", "confirm:true", 'id="mk-blocked"'):
        assert needle in html, needle
    passed += 1; print("  page ok")

    print(f"PASS smoke_make_admin ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
