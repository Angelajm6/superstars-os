"""Only traffic that came through Cloudflare (2026-10-30).

The app is reachable two ways: https://app.superstarscontracting.com (Cloudflare in front) and the service's
own Render address, which skips Cloudflare. Through the back door, Cloudflare's protection is gone and the
client-IP header Cloudflare adds (CF-Connecting-IP) can be forged, which weakens the Claude connector's
"only Anthropic's servers" check.

The fix is a shared secret: a Cloudflare Transform Rule adds the request header X-SSC-Edge with a secret value
to every request it forwards, and this guard refuses (403) any request without that exact value. Only Cloudflare
knows it, so a request that skipped Cloudflare can't carry it.

  SSC_EDGE_SECRET   the value (1Password "Dashboard Secrets"; same value in the Cloudflare rule). Unset = off.

Exempt: /api/health, so Render's own health check (which doesn't pass through Cloudflare) keeps working; it
returns only status, backend flavor and a migration count.

Turn on in this order, or the whole app answers 403: (1) the Cloudflare rule, (2) SSC_EDGE_SECRET on Render.
To undo at once: delete SSC_EDGE_SECRET on Render.

Logs carry the method and sanitized path only, never header values.
"""
from __future__ import annotations

import hmac
import logging
import os

from flask import jsonify, request

import request_logging

HEADER = "X-SSC-Edge"
EXEMPT_EXACT = frozenset({"/api/health"})


def secret() -> str:
    return (os.environ.get("SSC_EDGE_SECRET") or "").strip()


def on() -> bool:
    return bool(secret())


def came_through_edge() -> bool:
    """True when the guard is off, or the request carries the edge secret."""
    s = secret()
    if not s:
        return True
    return hmac.compare_digest((request.headers.get(HEADER) or "").encode("utf-8"), s.encode("utf-8"))


def apply_edge_guard(app) -> None:
    @app.before_request
    def _edge_guard():
        if request.path in EXEMPT_EXACT or came_through_edge():
            return None
        logging.warning("edge guard: refused %s %s (did not come through Cloudflare)",
                        request.method, request_logging.sanitize_path(request.path))
        return jsonify({"error": "forbidden"}), 403

    logging.warning("edge guard %s", "on" if on() else "off (SSC_EDGE_SECRET unset)")
