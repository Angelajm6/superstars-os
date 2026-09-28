"""HTTP perimeter hardening for the authenticated operations portal.

Headers deliberately avoid a restrictive script/style CSP at this stage: the
legacy UI has many inline scripts.  The policies below still close framing,
plugin, base-tag, MIME-sniffing, referrer, and browser-feature attack surfaces
without silently breaking a C-suite pre-production review.
"""
from __future__ import annotations

import os
import re

from flask import request

# #351 — the ONLY pages the app shows inside its own frames (the project dashboard
# embeds them). They may be framed by the same origin; nothing may frame them from
# another site, and every other page stays unframeable. Without this the browser
# showed "refuses to connect" in both panels.
_SAME_ORIGIN_FRAMEABLE = (
    re.compile(r"^/drawing-markup/?$"),
    re.compile(r"^/api/projects/[^/]+/weekly/render$"),
)


def _frameable_by_self(path: str) -> bool:
    return any(rx.match(path or "") for rx in _SAME_ORIGIN_FRAMEABLE)


def _edge_is_https() -> bool:
    """Whether HSTS is safe for this request's public-facing connection."""
    if request.is_secure:
        return True
    trusted = (os.environ.get("SSC_TRUSTED_PROXY") or "").strip().lower()
    return bool(trusted) and trusted not in {"0", "false", "no"}


def apply_http_security_headers(app) -> None:
    """Add safe defaults to every response, including errors and file downloads."""
    @app.after_request
    def _http_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        self_frame = _frameable_by_self(request.path)
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN" if self_frame else "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(self), geolocation=(self), microphone=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "base-uri 'self'; frame-ancestors " + ("'self'" if self_frame else "'none'")
            + "; object-src 'none'",
        )
        if _edge_is_https():
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains",
            )
        return response
