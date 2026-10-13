#!/usr/bin/env python3
"""Change a sign-in email, confirmed from the new address (accepted 2026-10-12): the
BEHAVIORAL guard for email_change.py.

Proves, through a Flask test client against an ISOLATED database:
  * /confirm-email is reachable logged-out; the request routes are not
  * nothing is stored or sent when this server cannot deliver email (503)
  * bad, unchanged, taken addresses and a wrong current password are refused
  * a good request stores only a SHA-256 hash, emails the link to the NEW address only,
    and shows as pending; a newer request or a cancel kills the older link
  * only an admin may request a change for someone else
  * confirming (logged out) changes the email, clears the Google sign-in binding, drops
    Google Calendar / Gmail connections, notifies the OLD address, and works once only
  * the new email + the same password signs in; the old email no longer does
  * an expired link, and a link whose address got taken meanwhile, are refused
  * at most MAX_PER_HOUR requests per account per hour
  * logs never carry an email address or a token
  * the Account page and Team & access carry the controls

Database: honors SSC_DB_URL like every gate suite; otherwise a throwaway SQLite snapshot
from the versioned migrations — it NEVER opens the live superstars.db.
Run:  venv/bin/python tests/smoke_email_change.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")   # the fake mail dir is a test-only seam
import logging
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_email_change_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
for _k in ("APP_BASE_URL", "SSC_PUBLIC_BASE_URL", "SSC_RESET_MAIL_FAKE_DIR", "SENDGRID_API_KEY",
           "SSC_RESET_SMTP_HOST", "SSC_RESET_SMTP_USERNAME", "SSC_RESET_SMTP_PASSWORD", "SSC_RESET_EMAIL_FROM",
           "SSC_TRUSTED_PROXY", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_APPS_CLIENT_ID"):
    os.environ.pop(_k, None)

from flask import Flask  # noqa: E402

import account  # noqa: E402
import auth  # noqa: E402
import db_layer  # noqa: E402
import email_change  # noqa: E402
import gcal_sync  # noqa: E402
import gmail_suggest  # noqa: E402

TAG = "emchg"
MAIL = Path(_TMP) / "mail"
PW = "EmailChangeSmoke2026"
A, B, C, ADMIN = (f"{TAG}.a@example.com", f"{TAG}.b@example.com", f"{TAG}.c@example.com", f"{TAG}.admin@example.com")
NEW = f"{TAG}.new@superstarscontracting.com"


class LogTrap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _db():
    return db_layer.connect(pragma_fk=True)


def _q(sql, args=()):
    conn = _db()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _x(sql, args=()):
    conn = _db()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _seed():
    _cleanup()
    conn = _db()
    try:
        for email, role in ((A, "pm"), (B, "super"), (C, "pm"), (ADMIN, "admin")):
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Smoke {TAG} {role}"))
        conn.commit()
        gcal_sync.ensure(conn)
        gmail_suggest.ensure(conn)
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        for t in ("email_change_request", "google_calendar_event", "google_calendar_link", "gmail_suggestion", "gmail_seen", "gmail_link", "account_pw_fail"):
            for i in ids:
                try:
                    conn.execute(f"DELETE FROM {t} WHERE user_id = ?", (i,))
                except Exception:
                    conn.rollback()
        conn.commit()
        purge_users(conn, ids)
    finally:
        conn.close()


def _uid(email):
    return _q("SELECT id FROM users WHERE LOWER(email) = ?", (email.lower(),))[0]["id"]


def _mails():
    return sorted(MAIL.glob("email-change-*.eml")) if MAIL.exists() else []


def _token(path):
    m = re.search(r"/confirm-email\?token=(\S+)", path.read_text(encoding="utf-8"))
    assert m, "confirm link missing"
    return unquote(m.group(1))


def main() -> int:
    app = Flask("emchg", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    account.register(app)
    email_change.register(app)
    trap = LogTrap()
    lg = logging.getLogger("ssc.email_change")
    lg.addHandler(trap)
    lg.setLevel(logging.INFO)

    def login(email, ip):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": email, "password": PW}, environ_base={"REMOTE_ADDR": ip})
        assert r.status_code == 200, (email, r.status_code)
        return c

    _seed()
    passed = 0
    try:
        anon = app.test_client()
        # ---- the confirm page is public; the rest is not
        r = anon.get("/confirm-email?token=x")
        assert r.status_code == 200 and b"Confirm your new email" in r.data and r.headers.get("Referrer-Policy") == "no-referrer"
        assert "/confirm-email" in auth._PUBLIC_EXACT
        assert anon.post("/api/account/email-change", json={"new_email": NEW}).status_code in (401, 403)
        passed += 1; print("  public / gated ok")

        a = login(A, "10.98.0.1")
        uid_a = _uid(A)
        assert a.get("/api/account/email-change").get_json()["data"]["pending"] is None
        # ---- no delivery: 503 and nothing stored
        r = a.post("/api/account/email-change", json={"new_email": NEW, "current_password": PW})
        assert r.status_code == 503 and not _q("SELECT id FROM email_change_request WHERE user_id = ?", (uid_a,))
        os.environ["APP_BASE_URL"] = "https://app.example.test"
        os.environ["SSC_RESET_MAIL_FAKE_DIR"] = str(MAIL)
        # ---- refusals
        for body, why in (({"new_email": "not-an-email", "current_password": PW}, "valid"),
                          ({"new_email": A.upper(), "current_password": PW}, "already"),
                          ({"new_email": B, "current_password": PW}, "Another account"),
                          ({"new_email": NEW, "current_password": "wrong-password-1"}, "not right")):
            r = a.post("/api/account/email-change", json=body)
            assert r.status_code == 400 and why in r.get_json()["error"], (body, r.get_json())
        assert not _mails()
        passed += 1; print("  refusals ok")

        # ---- a good request: hash only, mail to the NEW address, pending
        r = a.post("/api/account/email-change", json={"new_email": NEW.upper(), "current_password": PW})
        assert r.status_code == 202, r.get_json()
        pend = r.get_json()["data"]["pending"]
        assert pend["new_email"] == NEW and pend["by_admin"] is False
        m1 = _mails()[-1]
        assert m1.read_text(encoding="utf-8").startswith(f"To: {NEW}\n")
        t1 = _token(m1)
        rows = _q("SELECT token_hash FROM email_change_request WHERE user_id = ?", (uid_a,))
        assert rows and all(t1 not in x["token_hash"] for x in rows) and rows[-1]["token_hash"] == email_change._hash(t1)
        # a newer request kills the older link
        assert a.post("/api/account/email-change", json={"new_email": NEW, "current_password": PW}).status_code == 202
        t2 = _token(_mails()[-1])
        assert anon.post("/api/auth/email-change/confirm", json={"token": t1}).status_code == 400
        # cancel kills the live one
        assert a.delete("/api/account/email-change").get_json()["data"]["pending"] is None
        assert anon.post("/api/auth/email-change/confirm", json={"token": t2}).status_code == 400
        assert _q("SELECT email FROM users WHERE id = ?", (uid_a,))[0]["email"] == A
        passed += 1; print("  request / replace / cancel ok")

        # ---- only an admin asks for someone else
        b = login(B, "10.98.0.2")
        assert b.post(f"/api/admin/users/{uid_a}/email-change", json={"new_email": NEW}).status_code == 403
        adm = login(ADMIN, "10.98.0.3")
        r = adm.post(f"/api/admin/users/{_uid(C)}/email-change", json={"new_email": f"{TAG}.c2@example.com"})
        assert r.status_code == 202 and r.get_json()["data"]["pending"]["by_admin"] is True
        assert "An administrator asked" in _mails()[-1].read_text(encoding="utf-8")
        passed += 1; print("  admin request ok")

        # ---- confirm (logged out): email changes, Google bindings drop, old address notified, once only
        _x("UPDATE users SET google_sub = ? WHERE id = ?", (f"{TAG}-sub", uid_a))
        _x("INSERT INTO google_calendar_link (user_id, google_email, refresh_token_enc, status, connected_at) VALUES (?, ?, 'x', 'active', '2026-10-12T09:00:00')", (uid_a, A))
        _x("INSERT INTO gmail_link (user_id, google_email, refresh_token_enc, status, connected_at) VALUES (?, ?, 'x', 'active', '2026-10-12T09:00:00')", (uid_a, A))
        assert a.post("/api/account/email-change", json={"new_email": NEW, "current_password": PW}).status_code == 202
        t3 = _token(_mails()[-1])
        n_before = len(_mails())
        r = anon.post("/api/auth/email-change/confirm", json={"token": t3})
        assert r.status_code == 200 and r.get_json()["data"]["email"] == NEW, r.get_json()
        u = _q("SELECT email, google_sub FROM users WHERE id = ?", (uid_a,))[0]
        assert u["email"] == NEW and u["google_sub"] is None
        assert not _q("SELECT id FROM google_calendar_link WHERE user_id = ?", (uid_a,))
        assert not _q("SELECT id FROM gmail_link WHERE user_id = ?", (uid_a,))
        notice = _mails()[n_before:]
        assert notice and notice[-1].read_text(encoding="utf-8").startswith(f"To: {A}\nSubject: {email_change.SUBJECT_NOTICE}")
        assert anon.post("/api/auth/email-change/confirm", json={"token": t3}).status_code == 400, "single use"
        assert a.get("/api/account/email-change").get_json()["data"]["pending"] is None
        # the new email signs in with the same password; the old one no longer does
        assert anon.post("/api/auth/login", json={"email": NEW, "password": PW}, environ_base={"REMOTE_ADDR": "10.98.0.4"}).status_code == 200
        assert anon.post("/api/auth/login", json={"email": A, "password": PW}, environ_base={"REMOTE_ADDR": "10.98.0.5"}).status_code == 401
        passed += 1; print("  confirm ok")

        # ---- expired, and taken in the meantime
        cc = login(C, "10.98.0.6")
        assert cc.post("/api/account/email-change", json={"new_email": f"{TAG}.c3@example.com", "current_password": PW}).status_code == 202
        t4 = _token(_mails()[-1])
        _x("UPDATE email_change_request SET expires_at = '2000-01-01T00:00:00' WHERE token_hash = ?", (email_change._hash(t4),))
        assert anon.post("/api/auth/email-change/confirm", json={"token": t4}).status_code == 400
        assert cc.post("/api/account/email-change", json={"new_email": f"{TAG}.c4@example.com", "current_password": PW}).status_code == 202
        t5 = _token(_mails()[-1])
        _x("UPDATE users SET email = ? WHERE id = ?", (f"{TAG}.c4@example.com", _uid(B)))
        assert anon.post("/api/auth/email-change/confirm", json={"token": t5}).status_code == 409
        passed += 1; print("  expired / taken ok")

        # ---- hourly cap
        _x("DELETE FROM email_change_request WHERE user_id = ?", (_uid(C),))
        codes = [cc.post("/api/account/email-change", json={"new_email": f"{TAG}.c{9 + i}@example.com", "current_password": PW}).status_code
                 for i in range(email_change.MAX_PER_HOUR + 1)]
        assert codes[:-1] == [202] * email_change.MAX_PER_HOUR and codes[-1] == 429, codes
        passed += 1; print("  hourly cap ok")

        # ---- logs carry ids only
        assert trap.lines and not any("@" in ln or t3 in ln for ln in trap.lines), trap.lines
        # ---- the pages carry the controls
        acc = (ROOT / "account.html").read_text(encoding="utf-8")
        assert 'id="em-open"' in acc and "/api/account/email-change" in acc and "An admin changes it in Team" not in acc
        adm_page = (ROOT / "admin_users.html").read_text(encoding="utf-8")
        assert 'data-email="' in adm_page and "/email-change'" in adm_page and 'id="ov-email"' in adm_page
        passed += 1; print("  logs / pages ok")
    finally:
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS smoke_email_change ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
