#!/usr/bin/env python3
"""Settings > Account — each person sees and edits their OWN account (account.py). Guard.

Boots the real app on an ISOLATED database. Proves:
  * /account and /api/account: every staff role (estimator included), signed-out 401, a client
    never gets in; the payload carries no hash, secret or session id
  * profile: full name required, length caps, control characters dropped, an empty display
    name falls back to the full name, the header (/api/auth/me) follows
  * password: wrong current password refused (and counted: the 6th try in 15 min is 429), weak
    or unchanged new password refused, a Google-only account has none to change; a change
    keeps this session, signs every other device out, and the old password stops working
  * devices: listed by a short key (never the session id), this device marked; one other device
    or all others can be signed out, never this one; nobody can touch another person's sessions
  * the console's old Settings panel (Interface card, two-step block) is gone and /#settings
    forwards to /account; Settings sub-tabs are Account / Team & access / Usage & job tools / Claude approvals / Company brand (admin);
    the header name links to /account; logs carry ids only
Run:  venv/bin/python tests/smoke_account_settings.py
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_account_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)
import account  # noqa: E402

TAG = "acctset"
PW = "AccountSmoke2026a"
NEW_PW = "AccountSmoke2026b"
LOG = io.StringIO()


def _db():
    return db_layer.connect(pragma_fk=False)


def _uids(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]


def _seed() -> dict:
    conn = _db()
    try:
        uids = _uids(conn)
        if uids:
            conn.execute(f"DELETE FROM account_pw_fail WHERE user_id IN ({','.join('?' * len(uids))})", uids)
            conn.commit()
        purge_users(conn, uids)
        ids = {}
        for key, role in (("cs", "c_suite"), ("pm", "pm"), ("super", "super"), ("est", "estimator"), ("client", "client"), ("sso", "pm")):
            email = f"{TAG}.{key}@example.com"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Account {key}"))
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.execute("UPDATE users SET force_sso = 1 WHERE id = ?", (ids["sso"],))
        conn.commit()
        return ids
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        uids = _uids(conn)
        if uids:
            conn.execute(f"DELETE FROM account_pw_fail WHERE user_id IN ({','.join('?' * len(uids))})", uids)
            conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(key, pw=PW, ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/129.0 Safari/537.36"):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@example.com", "password": pw}, headers={"User-Agent": ua})
    return c, r.status_code


def main() -> int:
    ids = _seed()
    handler = logging.StreamHandler(LOG)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    passed = 0

    # ---- who gets in
    anon = server.app.test_client()
    assert anon.get("/api/account").status_code == 401
    cs, _ = _login("cs")
    pm, _ = _login("pm")
    est, s = _login("est"); assert s == 200
    cl, _ = _login("client")
    for c in (cs, pm, est):
        assert c.get("/account").status_code == 200, "every staff role opens their account"
        r = c.get("/api/account"); assert r.status_code == 200
    assert cl.get("/api/account").status_code in (302, 403) and cl.get("/account").status_code in (302, 403)
    d = pm.get("/api/account").get_json()["data"]
    assert d["email"] == f"{TAG}.pm@example.com" and d["role"] == "pm" and d["password_sign_in"] is True and d["totp_enabled"] is False
    raw = pm.get("/api/account").get_data(as_text=True)
    assert "password_hash" not in raw and "$2b$" not in raw and "totp_secret" not in raw
    page = pm.get("/account").get_data(as_text=True)
    assert 'id="pf"' in page and 'id="dv-list"' in page and "/api/2fa/begin" in page and 'id="tf-qr"' in page and "qr_svg" in page and "/settings/interface" not in page
    passed += 1; print("  access ok")

    # ---- profile
    assert pm.put("/api/account/profile", json={"full_name": "   ", "display_name": "x"}).status_code == 400
    assert pm.put("/api/account/profile", json={"full_name": "x" * 121}).status_code == 400
    assert pm.put("/api/account/profile", json={"full_name": "Pat", "display_name": "y" * 61}).status_code == 400
    r = pm.put("/api/account/profile", json={"full_name": " Pat\x07 Rivera ", "display_name": ""})
    assert r.status_code == 200 and r.get_json()["data"] == {"full_name": "Pat Rivera", "display_name": "Pat Rivera"}
    r = pm.put("/api/account/profile", json={"full_name": "Pat Rivera", "display_name": "Pat"})
    assert r.get_json()["data"]["display_name"] == "Pat"
    me = pm.get("/api/auth/me").get_json()["user"]
    assert me["full_name"] == "Pat Rivera" and me["display_name"] == "Pat", "the header follows"
    assert cs.get("/api/auth/me").get_json()["user"]["full_name"] == "Account cs", "only my own row changed"
    passed += 1; print("  profile ok")

    # ---- devices (before the password change signs the others out)
    pm_phone, _ = _login("pm", ua="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1")
    pm_pc, _ = _login("pm", ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/129.0 Safari/537.36 Edg/129.0")
    r = pm.get("/api/account/sessions")
    rows = r.get_json()["data"]
    assert len(rows) == 3 and rows[0]["current"] is True and sum(1 for s in rows if s["current"]) == 1
    assert {s["device"] for s in rows} == {"Chrome on Mac", "Safari on iPhone", "Edge on Windows"}
    assert all(re.fullmatch(r"[0-9a-f]{16}", s["key"]) for s in rows)
    conn = _db()
    try:
        sids = [x["id"] for x in conn.execute("SELECT id FROM sessions WHERE user_id = ?", (ids["pm"],)).fetchall()]
    finally:
        conn.close()
    assert not any(sid in r.get_data(as_text=True) for sid in sids), "a session id never leaves the server"
    mine = rows[0]["key"]
    phone = next(s["key"] for s in rows if s["device"] == "Safari on iPhone")
    assert pm.post(f"/api/account/sessions/{mine}/sign-out").status_code == 400, "not this device"
    assert cs.post(f"/api/account/sessions/{phone}/sign-out").status_code == 404, "never someone else's"
    assert pm_phone.get("/api/auth/me").status_code == 200
    assert pm.post(f"/api/account/sessions/{phone}/sign-out").status_code == 200
    assert pm_phone.get("/api/auth/me").status_code == 401 and pm.get("/api/auth/me").status_code == 200
    assert pm.post("/api/account/sessions/sign-out-others").get_json()["data"]["signed_out"] == 1
    assert pm_pc.get("/api/auth/me").status_code == 401 and pm.get("/api/auth/me").status_code == 200
    assert len(pm.get("/api/account/sessions").get_json()["data"]) == 1
    passed += 1; print("  devices ok")

    # ---- password
    other, _ = _login("pm")
    assert pm.post("/api/account/password", json={"current_password": "wrong-one-1234", "new_password": NEW_PW}).status_code == 400
    assert pm.post("/api/account/password", json={"current_password": PW, "new_password": "short1"}).status_code == 400
    assert pm.post("/api/account/password", json={"current_password": PW, "new_password": "onlylettersherex"}).status_code == 400
    assert pm.post("/api/account/password", json={"current_password": PW, "new_password": PW}).status_code == 400
    sso, s = _login("sso")
    if s == 200:   # force_sso may refuse the password login outright, which is fine too
        assert sso.post("/api/account/password", json={"current_password": PW, "new_password": NEW_PW}).status_code == 409
        assert sso.get("/api/account").get_json()["data"]["password_sign_in"] is False
    r = pm.post("/api/account/password", json={"current_password": PW, "new_password": NEW_PW})
    assert r.status_code == 200 and r.get_json()["data"]["signed_out"] == 1, r.get_data(as_text=True)
    assert pm.get("/api/auth/me").status_code == 200, "this session stays"
    assert other.get("/api/auth/me").status_code == 401, "every other device is signed out"
    assert _login("pm")[1] == 401 and _login("pm", NEW_PW)[1] == 200
    # wrong current passwords are counted: the 6th within 15 minutes is refused outright
    sup, _ = _login("super")
    codes = [sup.post("/api/account/password", json={"current_password": f"nope-{i}-xxxxxxx", "new_password": NEW_PW}).status_code for i in range(6)]
    assert codes == [400] * 5 + [429], codes
    assert sup.post("/api/account/password", json={"current_password": PW, "new_password": NEW_PW}).status_code == 429, "even the right one, until the window passes"
    passed += 1; print("  password ok")

    # ---- the pages around it
    nav = (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")
    assert "sub: [['Account', '/account'], ['Team & access', '/admin/users'], ['Usage & job tools', '/company#usage'], ['Claude approvals', '/company#claude'], ['Company brand', '/company#brand', ['admin']]]" in nav
    assert "'settings': '/account'" in nav and "Preferences & security', '/#settings'" not in nav
    assert "{ key: 'home', label: 'Home', href: '/projects', roles: ['pm', 'super']" in nav
    console = (ROOT / "company-dashboard.html").read_text(encoding="utf-8")
    assert 'data-panel="settings"' not in console and "interface-section" not in console and "twofa-enroll-btn" not in console
    menu = (ROOT / "static" / "js" / "auth_menu.js").read_text(encoding="utf-8")
    assert "<a class=\"au-name\" href=\"/account\"" in menu and "user.display_name || user.full_name" in menu
    for f in ("admin_users.html", "admin_projects.html", "estimating.html"):
        assert '<a href="/account" title="Your account"' in (ROOT / f).read_text(encoding="utf-8"), f
    assert account.device_label("") == "Unknown device" and account.device_label("Mozilla/5.0 (Linux; Android 14) Chrome/129.0 Mobile Safari/537.36") == "Chrome on Android"
    passed += 1; print("  pages ok")

    logs = LOG.getvalue()
    assert f"{TAG}." not in logs and PW not in logs and NEW_PW not in logs and not any(sid in logs for sid in sids), "logs carry ids only"
    passed += 1; print("  logs ok")

    logging.getLogger().removeHandler(handler)
    print(f"PASS smoke_account_settings ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_account_settings: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
