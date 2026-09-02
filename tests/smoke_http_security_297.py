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

assert "frame-ancestors 'none'" in headers
assert "object-src 'none'" in headers
assert "if _edge_is_https()" in headers, "HSTS must not be sent on local HTTP by default"
print("PASS: #297 HTTP security headers and non-wildcard browser API policy")
