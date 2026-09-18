#!/usr/bin/env python3
"""#309 — C-suite resets a client's password; strong-password suggester. Behavioral guard.

Minimal Flask app (auth gate + auth_admin) on an ISOLATED database. Proves:

  * role matrix: /admin/users + list + reset-password accept admin AND c_suite;
    create / role / deactivate / reactivate stay admin-only (c_suite -> 403);
    pm / estimator / client -> 403 everywhere; anonymous -> 401
  * c_suite list = client accounts only (`limited: true`); admin list = everyone
  * c_suite reset: client -> 200 with a one-time password + must_reset flag;
    pm / c_suite targets -> 403; admin target -> 403 for everyone; unknown -> 404
  * admin reset of a pm still works (unchanged)
  * the temp password is never in the log line
  * the two set-password pages load the suggester and wire the button; the static
    file exists, and — when node is available — every generated value passes the
    server's own password_strength_error and is unique across a batch

Run:  venv/bin/python tests/smoke_csuite_reset_309.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_csr_309_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import auth_admin  # noqa: E402
import db_layer  # noqa: E402

TAG = "csr309"
PW = "CsuiteResetSmoke2026"
ROLES = ("admin", "c_suite", "pm", "estimator", "client")


def _db():
    return db_layer.connect(pragma_fk=True)


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for role in ROLES + ("client2",):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            # is_system=0 on purpose: the list endpoint hides system rows (#258)
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, display_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, ?, 1, 'active', 0, 0)",
                         (email, auth.hash_password(PW), "client" if role.startswith("client") else role,
                          f"Smoke {role}", f"Smoke {role}"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.execute("DELETE FROM login_audit WHERE ip LIKE '10.39.%'")
        conn.commit()
        purge_users(conn, ids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(app, role):
    c = app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW},
               environ_base={"REMOTE_ADDR": "10.39.0.1"})
    assert r.status_code == 200, (role, r.status_code, r.get_data(as_text=True)[:200])
    return c


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(); self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def main() -> int:
    app = Flask("csr309", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    auth_admin.register(app)
    ids = _seed()
    cap = _Capture(); logging.getLogger().addHandler(cap); logging.getLogger().setLevel(logging.INFO)
    passed = 0

    # ---- role matrix
    anon = app.test_client()
    assert anon.get("/api/admin/users").status_code == 401
    for role in ("pm", "estimator", "client"):
        c = _login(app, role)
        assert c.get("/admin/users").status_code == 403, role
        assert c.get("/api/admin/users").status_code == 403, role
        assert c.post(f"/api/admin/users/{ids['client']}/reset-password").status_code == 403, role
    cs = _login(app, "c_suite")
    assert cs.get("/admin/users").status_code == 200
    assert cs.post("/api/admin/users", json={"display_name": "x", "email": "x@example.com", "role": "client"}).status_code == 403
    assert cs.post(f"/api/admin/users/{ids['pm']}/role", json={"new_role": "estimator", "reason": "x"}).status_code == 403
    assert cs.post(f"/api/admin/users/{ids['pm']}/deactivate").status_code == 403
    assert cs.post(f"/api/admin/users/{ids['pm']}/reactivate").status_code == 403
    passed += 1; print("  role matrix ok")

    # ---- list filtering
    j = cs.get("/api/admin/users").get_json()
    assert j["limited"] is True and j["data"] and all(u["role"] == "client" for u in j["data"])
    assert {u["id"] for u in j["data"]} >= {ids["client"], ids["client2"]}
    admin = _login(app, "admin")
    j = admin.get("/api/admin/users").get_json()
    assert j["limited"] is False and {u["role"] for u in j["data"]} >= {"admin", "c_suite", "pm", "client"}
    passed += 1; print("  list filtering ok")

    # ---- resets
    assert cs.post(f"/api/admin/users/{ids['pm']}/reset-password").status_code == 403
    assert cs.post(f"/api/admin/users/{ids['c_suite']}/reset-password").status_code == 403
    assert cs.post(f"/api/admin/users/{ids['admin']}/reset-password").status_code == 403
    assert admin.post(f"/api/admin/users/{ids['admin']}/reset-password").status_code == 403
    assert cs.post("/api/admin/users/99999999/reset-password").status_code == 404
    r = cs.post(f"/api/admin/users/{ids['client']}/reset-password")
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    temp = j["temp_password"]
    assert len(temp) >= 12 and j["data"]["must_reset_password"] is True and j["data"]["role"] == "client"
    assert auth.password_strength_error(temp) is None
    assert not any(temp in ln for ln in cap.lines), "temp password leaked into a log line"
    assert any("actor_role=c_suite" in ln for ln in cap.lines)
    # the client's old password is dead; the temp one works and lands on the forced reset
    bad = app.test_client().post("/api/auth/login", json={"email": f"{TAG}.client@example.com", "password": PW},
                                 environ_base={"REMOTE_ADDR": "10.39.0.2"})
    assert bad.status_code == 401
    good = app.test_client().post("/api/auth/login", json={"email": f"{TAG}.client@example.com", "password": temp},
                                  environ_base={"REMOTE_ADDR": "10.39.0.3"})
    assert good.status_code == 200 and (good.get_json().get("must_reset_password") or good.get_json().get("user", {}).get("must_reset_password"))
    # admin still resets a pm (unchanged behaviour)
    r = admin.post(f"/api/admin/users/{ids['pm']}/reset-password")
    assert r.status_code == 200 and r.get_json()["data"]["role"] == "pm"
    passed += 1; print("  resets ok")

    # ---- the suggester: pages + static file + generator contract
    js = ROOT / "static" / "js" / "pw_suggest.js"
    assert js.exists() and "SSCPasswordSuggest" in js.read_text(encoding="utf-8")
    for page in ("reset_password.html", "set_password.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert 'id="pw-suggest"' in html and "pw_suggest.js" in html and "SSCPasswordSuggest.wire(" in html, page
    assert app.test_client().get("/files/static/js/pw_suggest.js").status_code == 200   # public asset
    node = shutil.which("node")
    if node:
        script = ("const c=require('crypto');global.window={crypto:{getRandomValues:(a)=>c.randomFillSync(a)}};"
                  "global.Event=class{};require(process.argv[1]);"
                  "const out=[];for(let i=0;i<200;i++)out.push(window.SSCPasswordSuggest.generate());"
                  "console.log(JSON.stringify(out));")
        res = subprocess.run([node, "-e", script, str(js)], capture_output=True, text=True, timeout=60)
        assert res.returncode == 0, res.stderr[-500:]
        vals = json.loads(res.stdout.strip())
        assert len(vals) == 200 and len(set(vals)) > 190
        for v in vals:
            assert auth.password_strength_error(v) is None, v
            assert re.fullmatch(r"[A-Z][a-z]+-[A-Z][a-z]+-[A-Z][a-z]+-\d{2}", v), v
        print("  suggester ok (node contract checked)")
    else:
        print("  suggester ok (node not available — generator contract skipped)")
    passed += 1

    print(f"PASS smoke_csuite_reset_309 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
