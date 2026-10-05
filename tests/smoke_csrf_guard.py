#!/usr/bin/env python3
"""Guard for csrf_guard.py (no DB, no server boot). Synthetic hosts/paths only."""
import io
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flask import Flask  # noqa: E402
import csrf_guard  # noqa: E402

FAILS = []


def ok(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


app = Flask("t_csrf")
csrf_guard.apply_csrf_guard(app)


@app.route("/<path:p>", methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"])
def _any(p):
    return "ok"


c = app.test_client()
HOST = "app.example.invalid"
ALLOWED = "https://allowed.example.invalid"


def call(method, headers=None, path="/api/thing"):
    h = {"Host": HOST}
    h.update(headers or {})
    return c.open(path, method=method, headers=h).status_code


def setmode(m, origins=None):
    for k in ("SSC_CSRF_MODE", "SSC_ALLOWED_ORIGINS"):
        os.environ.pop(k, None)
    if m is not None:
        os.environ["SSC_CSRF_MODE"] = m
    if origins:
        os.environ["SSC_ALLOWED_ORIGINS"] = origins


# --- enforce: decision matrix over all mutating methods ---
setmode("enforce", ALLOWED)
for m in ("POST", "PUT", "PATCH", "DELETE"):
    ok(f"{m} no headers allowed (non-browser)", call(m) == 200)
    ok(f"{m} Sec-Fetch-Site same-origin allowed", call(m, {"Sec-Fetch-Site": "same-origin"}) == 200)
    ok(f"{m} Sec-Fetch-Site none allowed", call(m, {"Sec-Fetch-Site": "none"}) == 200)
    ok(f"{m} Sec-Fetch-Site cross-site blocked", call(m, {"Sec-Fetch-Site": "cross-site"}) == 403)
    ok(f"{m} Sec-Fetch-Site same-site blocked", call(m, {"Sec-Fetch-Site": "same-site"}) == 403)
    ok(f"{m} Origin matching host allowed", call(m, {"Origin": f"https://{HOST}"}) == 200)
    ok(f"{m} Origin in allowed list allowed", call(m, {"Origin": ALLOWED}) == 200)
    ok(f"{m} Origin mismatch blocked", call(m, {"Origin": "https://evil.example.invalid"}) == 403)
    ok(f"{m} Origin null blocked", call(m, {"Origin": "null"}) == 403)
ok("Sec-Fetch-Site wins over a matching Origin", call("POST", {"Sec-Fetch-Site": "cross-site", "Origin": f"https://{HOST}"}) == 403)
ok("lookalike host is not a match", call("POST", {"Origin": f"https://{HOST}.evil.example.invalid"}) == 403)
ok("host match is case-insensitive", call("POST", {"Origin": f"https://{HOST.upper()}"}) == 200)
ok("Forwarded-Host is not trusted", call("POST", {"Origin": "https://evil.example.invalid", "X-Forwarded-Host": "evil.example.invalid"}) == 403)
for m in ("GET", "HEAD", "OPTIONS"):
    ok(f"{m} never blocked even cross-site", call(m, {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example.invalid"}) == 200)
ok("blocked response is JSON 403", c.post("/api/x", headers={"Host": HOST, "Sec-Fetch-Site": "cross-site"}).get_json() == {"error": "cross-site request blocked"})
for p in ("/login", "/api/auth/login", "/p/tok", "/api/p/tok/sign", "/api/worker-sign-in"):
    ok(f"public path {p} is guarded too", call("POST", {"Sec-Fetch-Site": "cross-site"}, path=p) == 403)

# --- report mode: allowed through, but logged; default mode is report ---
buf = io.StringIO(); h = logging.StreamHandler(buf); logging.getLogger().addHandler(h)
setmode("report")
ok("report mode lets a cross-site POST through", call("POST", {"Sec-Fetch-Site": "cross-site"}) == 200)
setmode(None)
ok("default mode is report", csrf_guard.mode() == "report")
ok("default mode lets a cross-site POST through", call("POST", {"Origin": "https://evil.example.invalid"}) == 200)
setmode("bogus")
ok("unknown mode behaves as report, not off", csrf_guard.mode() == "report" and call("POST", {"Sec-Fetch-Site": "cross-site"}) == 200)
logging.getLogger().removeHandler(h)
out = buf.getvalue()
ok("would-block is logged in report mode", "WOULD-BLOCK" in out and "reason=sec-fetch-site:cross-site" in out)
ok("origin-mismatch reason logged", "reason=origin-mismatch" in out)
ok("log never contains an Origin value", "evil.example.invalid" not in out)

# --- off mode ---
setmode("off")
ok("off mode does nothing", call("POST", {"Sec-Fetch-Site": "cross-site"}) == 200)

# --- hygiene / wiring ---
ok("nothing is exempt", csrf_guard.EXEMPT_PREFIXES == ())
server_src = (ROOT / "server.py").read_text(encoding="utf-8")
ok("server.py registers the guard", "csrf_guard.apply_csrf_guard(app)" in server_src)
render = (ROOT / "render.yaml").read_text(encoding="utf-8")
ok("render.yaml starts in report mode", "SSC_CSRF_MODE" in render and 'value: report' in render)
ok("render.yaml allows only the app origin", "https://app.superstarscontracting.com" in render)

setmode(None)
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s) failed")
    sys.exit(1)
print("PASS: csrf guard")
