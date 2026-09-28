#!/usr/bin/env python3
"""Static guard for #297 HTTP perimeter hardening (no database or secrets needed)."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
server = (ROOT / "server.py").read_text(encoding="utf-8")
headers = (ROOT / "http_security.py").read_text(encoding="utf-8")

assert "from flask_cors import CORS" not in server, "wildcard CORS import remains"
assert 'origins": "*"' not in server, "wildcard API CORS remains"
assert "http_security.apply_http_security_headers(app)" in server

for name in (
    "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy",
    "Permissions-Policy", "Content-Security-Policy", "Strict-Transport-Security",
):
    assert name in headers, f"missing {name}"

assert "frame-ancestors " in headers and "'none'" in headers   # default stays 'none' (behaviour below)

# #351 — behaviour: only the two pages the dashboard embeds may be framed, and only by
# the app itself; everything else (and any look-alike path) stays DENY / 'none'.
import sys
sys.path.insert(0, str(ROOT))
from flask import Flask  # noqa: E402
import http_security  # noqa: E402

_app = Flask("t351")
http_security.apply_http_security_headers(_app)


@_app.route("/<path:p>")
def _any(p):
    return "ok"


_c = _app.test_client()
for path in ("/drawing-markup", "/api/projects/FR-BX-001/weekly/render"):
    r = _c.get(path)
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN", path
    assert "frame-ancestors 'self'" in r.headers["Content-Security-Policy"], path
for path in ("/company", "/projects/FR-BX-001", "/api/projects/FR-BX-001/weekly/render/x",
             "/drawing-markup-evil", "/login"):
    r = _c.get(path)
    assert r.headers["X-Frame-Options"] == "DENY", path
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"], path
assert "object-src 'none'" in headers
assert "if _edge_is_https()" in headers, "HSTS must not be sent on local HTTP by default"
print("PASS: #297 HTTP security headers and non-wildcard browser API policy (+ #351 same-origin frames)")
