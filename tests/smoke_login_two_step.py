#!/usr/bin/env python3
"""The login page asks for the 6-digit code (2026-10-29).

With two-step sign-in on, a correct password gets 401 {totp_required}; the page showed "Email or password is
incorrect." and never asked for the code, so anyone who turned two-step sign-in on was locked out of the
password route. Proves against an ISOLATED database:
  * the API: correct password -> 401 totp_required; + a wrong code -> 401 totp_required; + the right code -> 200;
    a wrong password still gets the uniform 401 (no totp_required, nothing revealed)
  * the page: a hidden code box, shown on totp_required; the code is sent as `totp`; the Google-only 403 is
    explained; the plain wrong-password message stays
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import db_layer  # noqa: E402
import totp  # noqa: E402

EMAIL = "l2s.admin@superstarscontracting.com"
PW = "LoginTwoStepSmokePassword2026"
SECRET = totp.generate_secret()


def _cleanup():
    conn = db_layer.connect()
    try:
        purge_users(conn, [r["id"] for r in conn.execute("SELECT id FROM users WHERE email = ?", (EMAIL,)).fetchall()])
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    _cleanup()
    conn = db_layer.connect()
    try:
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system, "
                     "totp_enabled, totp_secret) VALUES (?, ?, 'admin', 'L2s admin', 1, 'active', 0, 0, 1, ?)",
                     (EMAIL, auth.hash_password(PW), SECRET))
        conn.commit()
    finally:
        conn.close()
    app = Flask("l2s")
    auth.apply_auth_gate(app)
    c = app.test_client()
    env = {"REMOTE_ADDR": "10.96.0.1"}
    r = c.post("/api/auth/login", json={"email": EMAIL, "password": PW}, environ_base=env)
    assert r.status_code == 401 and r.get_json().get("totp_required"), r.get_json()
    r = c.post("/api/auth/login", json={"email": EMAIL, "password": PW, "totp": "12345x"}, environ_base=env)
    assert r.status_code == 401 and r.get_json().get("totp_required")
    r = c.post("/api/auth/login", json={"email": EMAIL, "password": "wrong-" + PW}, environ_base=env)
    assert r.status_code == 401 and not r.get_json().get("totp_required"), "a wrong password reveals nothing"
    code = totp._code_at(SECRET, int(time.time() // totp.STEP))
    r = c.post("/api/auth/login", json={"email": EMAIL, "password": PW, "totp": code}, environ_base=env)
    assert r.status_code == 200, r.get_json()
    print("  api ok")

    page = (ROOT / "login.html").read_text(encoding="utf-8")
    assert 'id="code-wrap" hidden' in page and 'id="code"' in page and 'autocomplete="one-time-code"' in page
    assert "j.totp_required" in page and "totp: codeEl.value.trim()" in page
    assert "j.sso_required" in page and "Email or password is incorrect." in page
    print("  page ok")
    print("smoke_login_two_step: 2 groups passed")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        _cleanup()
    sys.exit(rc)
