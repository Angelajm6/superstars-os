#!/usr/bin/env python3
"""Sign in: "Remember this device for 30 days" skips the authenticator code — admins only. Guard.

Boots the real app on an ISOLATED database. Proves:
  * the code step tells an admin it can remember the device, and nobody else
  * admin + code + remember -> a 30-day HttpOnly cookie scoped to the login call; next sign-in
    from that browser needs the password but not the code (audited as remembered_device)
  * the password is still checked: a wrong one is the same uniform 401
  * no remember tick -> no cookie, the code is asked again
  * a non-admin asking to be remembered gets no cookie; an admin cookie stops working if the
    account is no longer an admin, in another account, in another browser, once expired, or
    after the password or authenticator changes
  * only the hash of the cookie is stored; the page has the checkbox; logs carry no token
Run:  venv/bin/python tests/smoke_remember_device.py
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_remember_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)
import totp as _totp  # noqa: E402

TAG = "remdev"
PW = "RememberSmoke2026a"
LOG = io.StringIO()


def _db():
    return db_layer.connect(pragma_fk=False)


def _uids(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]


def _purge(conn):
    uids = _uids(conn)
    if uids:
        conn.execute(f"DELETE FROM trusted_device WHERE user_id IN ({','.join('?' * len(uids))})", uids)
        conn.commit()
    purge_users(conn, uids)
    conn.commit()


def _seed() -> dict:
    conn = _db()
    try:
        _purge(conn)
        out = {}
        for key, role in (("admin", "admin"), ("admin2", "admin"), ("cs", "c_suite")):
            email = f"{TAG}.{key}@example.com"
            secret = _totp.generate_secret()
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, "
                         "is_system, totp_secret, totp_enabled) VALUES (?, ?, ?, ?, 1, 'active', 0, 1, ?, 1)",
                         (email, auth.hash_password(PW), role, f"Remember {key}", secret))
            out[key] = {"id": conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"], "secret": secret}
        conn.commit()
        return out
    finally:
        conn.close()


def _code(secret):
    return _totp._code_at(secret, int(time.time() // 30))


def _post(c, key, pw=PW, **extra):
    body = {"email": f"{TAG}.{key}@example.com", "password": pw, **extra}
    return c.post("/api/auth/login", json=body)


def _cookie(r):
    return next((h for h in r.headers.getlist("Set-Cookie") if h.startswith(auth.TRUSTED_COOKIE + "=")), "")


def main() -> int:
    u = _seed()
    handler = logging.StreamHandler(LOG)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    passed = 0

    # ---- the code step offers it to admins only
    c = server.app.test_client()
    r = _post(c, "admin")
    assert r.status_code == 401 and r.get_json() == {"error": "second factor required", "totp_required": True, "can_remember": True}
    r = _post(server.app.test_client(), "cs")
    assert r.status_code == 401 and r.get_json()["can_remember"] is False
    passed += 1; print("  offer ok")

    # ---- admin ticks it: a 30-day cookie, only for the login call; only its hash is stored
    r = _post(c, "admin", totp=_code(u["admin"]["secret"]), remember=True)
    assert r.status_code == 200
    ck = _cookie(r)
    assert ck and "HttpOnly" in ck and "SameSite=Lax" in ck and "Path=/api/auth/login" in ck and "Max-Age=2592000" in ck, ck
    token = ck.split(";")[0].split("=", 1)[1]
    conn = _db()
    try:
        rows = conn.execute("SELECT token_hash, expires_at FROM trusted_device WHERE user_id = ?", (u["admin"]["id"],)).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1 and rows[0]["token_hash"] == auth._hash_token(token) and token not in rows[0]["token_hash"]
    assert rows[0]["expires_at"][:10] == time.strftime("%Y-%m-%d", time.localtime(time.time() + 30 * 86400))
    passed += 1; print("  remember ok")

    # ---- next time: password only (still checked), audited as remembered
    c.post("/api/auth/logout")
    assert _post(c, "admin", pw="WrongPassword2026x").status_code == 401
    r = _post(c, "admin")
    assert r.status_code == 200 and r.get_json()["user"]["role"] == "admin", r.get_json()
    assert not _cookie(r), "an already-remembered sign-in does not mint another cookie"
    conn = _db()
    try:
        m = conn.execute("SELECT method FROM login_audit WHERE user_id = ? AND event = 'login_success' ORDER BY id DESC",
                         (u["admin"]["id"],)).fetchone()["method"]
    finally:
        conn.close()
    assert m == "remembered_device"
    passed += 1; print("  skip code ok")

    # ---- not ticked / not admin / wrong browser / wrong account
    d = server.app.test_client()
    r = _post(d, "admin2", totp=_code(u["admin2"]["secret"]))
    assert r.status_code == 200 and not _cookie(r), "no tick, no cookie"
    d.post("/api/auth/logout")
    assert _post(d, "admin2").status_code == 401, "asked again"
    e = server.app.test_client()
    r = _post(e, "cs", totp=_code(u["cs"]["secret"]), remember=True)
    assert r.status_code == 200 and not _cookie(r), "c_suite is not offered it"
    assert _post(server.app.test_client(), "admin").status_code == 401, "another browser still needs the code"
    f = server.app.test_client()
    f.set_cookie(auth.TRUSTED_COOKIE, token, path="/api/auth/login")
    assert _post(f, "admin2").status_code == 401, "one admin's device cookie is not another's"
    assert _post(f, "admin").status_code == 200
    passed += 1; print("  scope ok")

    # ---- forgotten when: role changes, expiry, password change, authenticator change
    def _fresh():
        g = server.app.test_client()
        g.set_cookie(auth.TRUSTED_COOKIE, token, path="/api/auth/login")
        return g
    conn = _db()
    try:
        conn.execute("UPDATE users SET role = 'c_suite' WHERE id = ?", (u["admin"]["id"],)); conn.commit()
        assert _post(_fresh(), "admin").status_code == 401, "no longer an admin"
        conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (u["admin"]["id"],)); conn.commit()
        assert _post(_fresh(), "admin").status_code == 200
        conn.execute("UPDATE trusted_device SET expires_at = '2000-01-01T00:00:00' WHERE user_id = ?", (u["admin"]["id"],)); conn.commit()
        assert _post(_fresh(), "admin").status_code == 401, "expired"
        conn.execute("UPDATE trusted_device SET expires_at = '2099-01-01T00:00:00' WHERE user_id = ?", (u["admin"]["id"],)); conn.commit()
        assert _post(_fresh(), "admin").status_code == 200
        conn.execute("UPDATE users SET totp_secret = ? WHERE id = ?", (_totp.generate_secret(), u["admin"]["id"])); conn.commit()
        assert _post(_fresh(), "admin").status_code == 401, "authenticator re-enrolled"
        conn.execute("UPDATE users SET totp_secret = ?, password_hash = ? WHERE id = ?",
                     (u["admin"]["secret"], auth.hash_password(PW), u["admin"]["id"])); conn.commit()
        assert _post(_fresh(), "admin").status_code == 401, "password changed (new hash)"
    finally:
        conn.close()
    passed += 1; print("  forget ok")

    # ---- the page and the logs
    page = (ROOT / "login.html").read_text(encoding="utf-8")
    assert 'id="remember"' in page and "Remember this device for 30 days" in page and "j.can_remember" in page and "body.remember = true" in page
    logs = LOG.getvalue()
    assert token not in logs and PW not in logs and f"{TAG}." not in logs, "logs carry no token, password or email"
    passed += 1; print("  page + logs ok")

    logging.getLogger().removeHandler(handler)
    print(f"PASS smoke_remember_device ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_remember_device: {e}")
        rc = 1
    finally:
        conn = _db()
        try:
            _purge(conn)
        finally:
            conn.close()
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(rc)
