#!/usr/bin/env python3
"""#301 — self-service password reset: the BEHAVIORAL guard.

`smoke_password_reset_300.py` is a static contract (strings in files). This one
exercises the real endpoints through a Flask test client against an ISOLATED
database and proves the security properties password_reset.py promises:

  * /forgot-password and /reset-password are reachable logged-out
  * /api/auth/password-reset/config is honest about deliverability
  * the request endpoint answers identically for unknown, inactive, SSO-only,
    and delivery-disabled cases (no user enumeration) and mints no token for them
  * only a SHA-256 hash of the token is stored; the raw token is only in the email
  * weak passwords are rejected and leave the token usable
  * a good confirm changes the password, revokes every session, marks the token
    used; the same token cannot be replayed; an expired token is refused
  * the new password signs in through /api/auth/login
  * per-IP throttle returns 429 after MAX_REQUESTS_PER_IP within the window

Database: honors SSC_DB_URL like every gate suite (point it at the isolated gate
backend). When SSC_DB_URL is unset it builds a throwaway SQLite snapshot from the
versioned migrations in a temp dir — it NEVER opens the live superstars.db.

Run:  venv/bin/python tests/smoke_password_reset_flow_301.py
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="ssc_reset_301_")


def _build_isolated_sqlite() -> str:
    """Empty schema from schema.sql + the same migration lists the preprod
    fixture bootstrap uses. Returns a sqlite: URL for db_layer."""
    import preprod_fixture_bootstrap as pfb
    src = Path(_TMP) / "source"
    shutil.copytree(
        ROOT, src,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "superstars.db*",
                                      "venv", "node_modules", "worker_records", "data_room"),
    )
    db = src / "superstars.db"
    with sqlite3.connect(db) as conn:
        conn.executescript((src / "schema.sql").read_text(encoding="utf-8"))
    env = os.environ.copy()
    env.pop("SSC_DB_URL", None)
    env.pop("SSC_DATA_ROOT", None)
    env["SSC_ALLOW_EMPTY_PREPROD_FIXTURE"] = "1"
    for script in pfb.LEGACY_MIGRATIONS + pfb.DUAL_BACKEND_MIGRATIONS:
        subprocess.run([sys.executable, script], cwd=src, env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"sqlite:{db}"


if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()

# Delivery env is controlled per-phase below; start from a clean slate.
for _k in ("APP_BASE_URL", "SSC_PUBLIC_BASE_URL", "SSC_RESET_MAIL_FAKE_DIR", "SENDGRID_API_KEY",
           "SSC_RESET_SMTP_HOST", "SSC_RESET_SMTP_USERNAME", "SSC_RESET_SMTP_PASSWORD",
           "SSC_RESET_EMAIL_FROM", "SSC_TRUSTED_PROXY"):
    os.environ.pop(_k, None)

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import db_layer  # noqa: E402
import password_reset  # noqa: E402

MAIL_DIR = Path(_TMP) / "mail"
TAG = "reset301"
USERS = {
    "active":   (f"{TAG}.active@example.com",   "active",   1, 0),
    "inactive": (f"{TAG}.inactive@example.com", "inactive", 0, 0),
    "sso":      (f"{TAG}.sso@example.com",      "active",   1, 1),
}
OLD_PW = "OldPassword2026x"
NEW_PW = "BrandNewPassword2026"


def _db():
    return db_layer.connect(pragma_fk=True)


def _seed():
    conn = _db()
    try:
        for email, status, is_active, force_sso in USERS.values():
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute(
                "INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                "                   must_reset_password, is_system, force_sso) "
                "VALUES (?, ?, 'pm', ?, ?, ?, 0, 1, ?)",
                (email, auth.hash_password(OLD_PW), f"Smoke {TAG}", is_active, status, force_sso),
            )
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%@example.com",)).fetchall()]
        for uid in ids:
            conn.execute("DELETE FROM password_reset_token WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM login_audit WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM password_reset_request WHERE request_ip LIKE '10.99.%'")
        conn.execute("DELETE FROM users WHERE email LIKE ?", (f"{TAG}.%@example.com",))
        conn.commit()
    finally:
        conn.close()


def _uid(email: str) -> int:
    return auth.get_user_by_email(email)["id"]


def _tokens(uid: int) -> list:
    conn = _db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM password_reset_token WHERE user_id = ?", (uid,)).fetchall()]
    finally:
        conn.close()


def _mail_files() -> list:
    return sorted(MAIL_DIR.glob("*.eml")) if MAIL_DIR.exists() else []


def _token_from_mail(path: Path) -> str:
    m = re.search(r"/reset-password\?token=([^\s]+)", path.read_text(encoding="utf-8"))
    assert m, "reset link missing from email"
    return unquote(m.group(1))


def _enable_fake_delivery():
    os.environ["APP_BASE_URL"] = "https://preprod.example.com/"
    os.environ["SSC_RESET_MAIL_FAKE_DIR"] = str(MAIL_DIR)


def _disable_delivery():
    os.environ.pop("APP_BASE_URL", None)
    os.environ.pop("SSC_RESET_MAIL_FAKE_DIR", None)


def main() -> int:
    app = Flask("reset301", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    password_reset.register(app)
    client = app.test_client()

    def post(url, payload, ip):
        return client.post(url, json=payload, environ_base={"REMOTE_ADDR": ip})

    _seed()
    try:
        # ---- pages are public (no redirect to /login) ----
        for path in ("/forgot-password", "/reset-password"):
            r = client.get(path)
            assert r.status_code == 200, (path, r.status_code)
            assert b"Superstars Contracting" in r.data

        # ---- config is honest ----
        _disable_delivery()
        assert client.get("/api/auth/password-reset/config").get_json() == {"enabled": False}
        os.environ["APP_BASE_URL"] = "http://not-loopback.example.com"
        os.environ["SSC_RESET_MAIL_FAKE_DIR"] = str(MAIL_DIR)
        assert client.get("/api/auth/password-reset/config").get_json() == {"enabled": False}, \
            "plain http on a non-loopback host must not count as deliverable"
        _enable_fake_delivery()
        assert client.get("/api/auth/password-reset/config").get_json() == {"enabled": True}

        active_email = USERS["active"][0]
        uid = _uid(active_email)

        # ---- identical generic answer, no token minted ----
        _disable_delivery()
        r = post("/api/auth/password-reset/request", {"email": active_email}, "10.99.0.1")
        assert r.status_code == 202 and r.get_json()["message"] == password_reset.GENERIC_MESSAGE
        assert _tokens(uid) == [], "no token may be minted while delivery is unconfigured"

        _enable_fake_delivery()
        generic = None
        for email, ip in ((f"{TAG}.nobody@example.com", "10.99.0.2"),
                          (USERS["inactive"][0], "10.99.0.3"),
                          (USERS["sso"][0], "10.99.0.4"),
                          ("", "10.99.0.5")):
            r = post("/api/auth/password-reset/request", {"email": email}, ip)
            assert r.status_code == 202, (email, r.status_code)
            body = r.get_json()
            generic = generic or body
            assert body == generic, "response must not vary by account state"
        assert _tokens(_uid(USERS["inactive"][0])) == []
        assert _tokens(_uid(USERS["sso"][0])) == []
        assert _mail_files() == [], "no email may be sent for ineligible accounts"

        # ---- the real thing ----
        sid = auth.create_session(uid, "smoke-301")
        assert auth.lookup_session(sid)
        r = post("/api/auth/password-reset/request", {"email": active_email}, "10.99.0.6")
        assert r.status_code == 202 and r.get_json() == generic
        rows = _tokens(uid)
        assert len(rows) == 1 and rows[0]["used_at"] is None
        mails = _mail_files()
        assert len(mails) == 1, mails
        token = _token_from_mail(mails[0])
        assert len(token) >= 40
        assert rows[0]["token_hash"] != token and token not in mails[0].name
        assert rows[0]["token_hash"] == password_reset._hash(token)
        # the HTML alternative carries the same link (escaped) and the brand button
        html_files = sorted(MAIL_DIR.glob("*.html"))
        assert len(html_files) == 1, html_files
        html_doc = html_files[0].read_text(encoding="utf-8")
        assert "Choose a new password" in html_doc and token in html_doc and "<script" not in html_doc

        # a second request replaces the first token (one live token per user)
        r = post("/api/auth/password-reset/request", {"email": active_email}, "10.99.0.7")
        assert r.status_code == 202
        rows = _tokens(uid)
        assert len(rows) == 1 and rows[0]["token_hash"] != password_reset._hash(token)
        r = post("/api/auth/password-reset/confirm", {"token": token, "new_password": NEW_PW}, "10.99.0.7")
        assert r.status_code == 400 and r.get_json()["code"] == "invalid_token", "replaced token must die"
        token = _token_from_mail(_mail_files()[-1])

        # ---- weak password: rejected, token untouched ----
        r = post("/api/auth/password-reset/confirm", {"token": token, "new_password": "short1"}, "10.99.0.8")
        assert r.status_code == 400 and r.get_json()["code"] == "weak_password"
        r = post("/api/auth/password-reset/confirm", {"token": token, "new_password": "x" * 20}, "10.99.0.8")
        assert r.status_code == 400 and r.get_json()["code"] == "weak_password"
        assert _tokens(uid)[0]["used_at"] is None

        # ---- garbage / missing token ----
        for bad in ("", "nope", token[:-1]):
            r = post("/api/auth/password-reset/confirm", {"token": bad, "new_password": NEW_PW}, "10.99.0.8")
            assert r.status_code == 400 and r.get_json()["code"] == "invalid_token", bad

        # ---- good confirm ----
        r = post("/api/auth/password-reset/confirm", {"token": token, "new_password": NEW_PW}, "10.99.0.9")
        assert r.status_code == 200 and r.get_json() == {"ok": True}, r.get_json()
        user = auth.get_user_by_email(active_email)
        assert auth.verify_password(NEW_PW, user["password_hash"])
        assert not auth.verify_password(OLD_PW, user["password_hash"])
        assert auth.lookup_session(sid) is None, "every prior session must be revoked"
        assert _tokens(uid)[0]["used_at"] is not None
        conn = _db()
        try:
            audited = conn.execute(
                "SELECT COUNT(1) AS n FROM login_audit WHERE user_id = ? AND event = 'password_reset'",
                (uid,)).fetchone()["n"]
        finally:
            conn.close()
        assert audited == 1, "the completed reset must actually land in login_audit (CHECK-allowed event)"

        # ---- replay is refused ----
        r = post("/api/auth/password-reset/confirm", {"token": token, "new_password": NEW_PW + "1"}, "10.99.0.9")
        assert r.status_code == 400 and r.get_json()["code"] == "invalid_token"
        assert auth.verify_password(NEW_PW, auth.get_user_by_email(active_email)["password_hash"])

        # ---- expired token is refused ----
        r = post("/api/auth/password-reset/request", {"email": active_email}, "10.99.0.10")
        assert r.status_code == 202
        token = _token_from_mail(_mail_files()[-1])
        conn = _db()
        try:
            past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
            conn.execute("UPDATE password_reset_token SET expires_at = ? WHERE user_id = ?", (past, uid))
            conn.commit()
        finally:
            conn.close()
        r = post("/api/auth/password-reset/confirm", {"token": token, "new_password": NEW_PW + "2"}, "10.99.0.10")
        assert r.status_code == 400 and r.get_json()["code"] == "invalid_token"

        # ---- the new password signs in ----
        r = post("/api/auth/login", {"email": active_email, "password": NEW_PW}, "10.99.0.11")
        assert r.status_code == 200, (r.status_code, r.get_json())
        r = post("/api/auth/login", {"email": active_email, "password": OLD_PW}, "10.99.0.11")
        assert r.status_code == 401

        # ---- per-IP throttle ----
        statuses = []
        for _ in range(password_reset.MAX_REQUESTS_PER_IP + 2):
            statuses.append(post("/api/auth/password-reset/request",
                                 {"email": f"{TAG}.nobody@example.com"}, "10.99.0.99").status_code)
        assert statuses[:password_reset.MAX_REQUESTS_PER_IP] == [202] * password_reset.MAX_REQUESTS_PER_IP, statuses
        assert statuses[password_reset.MAX_REQUESTS_PER_IP:] == [429, 429], statuses
        # a different source is unaffected
        assert post("/api/auth/password-reset/request",
                    {"email": f"{TAG}.nobody@example.com"}, "10.99.0.100").status_code == 202
    finally:
        _cleanup()
        _disable_delivery()
        shutil.rmtree(_TMP, ignore_errors=True)

    print("PASS: #301 password reset flow — public pages, honest config, no enumeration, "
          "hashed single-use tokens, session revocation, expiry, login, throttle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
