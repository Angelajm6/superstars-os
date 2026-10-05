#!/usr/bin/env python3
"""A logged-out visit to /projects/X?ui=2 must come back to /projects/X?ui=2 after sign-in.

The auth gate builds /login?next=…; it used to keep only the path, dropping ?ui=2. Now it keeps an
ALLOW-LIST of harmless interface switches (ui=1|2) and nothing else — arbitrary query data (tokens,
ids, search text) must never be copied into the login URL. No database, no server boot.
"""
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flask import Flask  # noqa: E402

import auth  # noqa: E402

FAILS = []


def ok(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


app = Flask("t_next")
auth.apply_auth_gate(app)


@app.route("/projects/<code>")
@app.route("/company")
def _page(code=None):
    return "ok"


c = app.test_client()


def nxt(url):
    r = c.get(url)
    loc = r.headers.get("Location", "")
    return r.status_code, loc, (unquote(loc.split("next=", 1)[1]) if "next=" in loc else None)


st, loc, n = nxt("/projects/FR-BX-001?ui=2")
ok("?ui=2 survives the login redirect", st == 302 and n == "/projects/FR-BX-001?ui=2")
ok("it is percent-encoded (no query splitting)", "%3F" in loc and "&" not in loc.split("next=", 1)[1])
ok("?ui=1 survives too", nxt("/projects/FR-BX-001?ui=1")[2] == "/projects/FR-BX-001?ui=1")
ok("a page with no query is unchanged", nxt("/projects/FR-BX-001")[2] == "/projects/FR-BX-001")
ok("/company?ui=2 is handled the same way", nxt("/company?ui=2")[2] == "/company?ui=2")

# ---- the allow-list: nothing else is copied into the login URL
_, loc, n = nxt("/projects/FR-BX-001?token=SYNTHETIC-SECRET&ui=2&q=search+text")
ok("other query data is dropped, ui is kept", n == "/projects/FR-BX-001?ui=2")
ok("a secret in the query never reaches the login URL", "SYNTHETIC-SECRET" not in loc and "search" not in loc)
ok("a non-allow-listed ui value is dropped", nxt("/projects/FR-BX-001?ui=3")[2] == "/projects/FR-BX-001"
   and nxt("/projects/FR-BX-001?ui=evil%0d%0aSet-Cookie:x")[2] == "/projects/FR-BX-001")
ok("an unrelated query is dropped", nxt("/projects/FR-BX-001?x=1")[2] == "/projects/FR-BX-001")
ok("repeated ui keeps only a valid first value", nxt("/projects/FR-BX-001?ui=2&ui=1")[2] == "/projects/FR-BX-001?ui=2")

# ---- API calls are never redirected (they get 401 JSON)
r = c.get("/api/projects/FR-BX-001/modules?ui=2")
ok("API paths still return 401 JSON, not a redirect", r.status_code == 401 and r.is_json)

# ---- the login page honors it: same-origin only, query preserved
login = (ROOT / "login.html").read_text(encoding="utf-8")
ok("login.html still rejects absolute and scheme-relative next", "raw.startsWith('/') || raw.startsWith('//')" in login.replace("!raw.startsWith('/') ||", "raw.startsWith('/') ||"))
ok("login.html sends the user to next, query included", "window.location.href = (next && next !== '/') ? next : roleHome(u.role);" in login)

if FAILS:
    print(f"FAIL: {len(FAILS)} check(s) failed")
    sys.exit(1)
print("PASS: login keeps ?ui= across the sign-in redirect")
