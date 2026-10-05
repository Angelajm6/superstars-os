"""Cross-site request forgery guard for state-changing requests.

The session cookie is SameSite=Lax, which is the only CSRF defence otherwise. This adds a
second layer that does not depend on cookie semantics: every POST/PUT/PATCH/DELETE is checked
with the browser-supplied fetch-metadata headers (no tokens, no client changes).

Decision, in order (non-state-changing methods are never checked):
  1. Sec-Fetch-Site present (all current browsers):  same-origin | none  -> allow
                                                      same-site | cross-site -> violation
  2. else Origin present:  its host must be this request's Host or in SSC_ALLOWED_ORIGINS,
                           otherwise (including "null") -> violation
  3. neither header (curl, server-to-server, the smoke suites): allow. A cross-site attack
     needs a browser, and a browser sends at least one of the headers on a cross-site write.

Mode, env SSC_CSRF_MODE:
  report  (default)  log the would-be block (method, sanitized path, reason code only) and ALLOW
  enforce            respond 403
  off                do nothing
An unrecognised value behaves as report (never silently off).

SSC_ALLOWED_ORIGINS: comma-separated origins (scheme://host[:port]) allowed besides the
request's own host, e.g. https://app.superstarscontracting.com. Forwarded-Host headers are
deliberately NOT trusted.

EXEMPT_PREFIXES is empty: nothing is exempt. A future inbound webhook must be added here by
name AND verify its own signature/token.

Logs carry method, sanitized path and a reason code — never the Origin value, headers or body.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

from flask import jsonify, request

import request_logging

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
EXEMPT_PREFIXES: tuple = ()
MODES = ("off", "report", "enforce")


def mode() -> str:
    m = (os.environ.get("SSC_CSRF_MODE") or "report").strip().lower()
    return m if m in MODES else "report"


def _host(value: str) -> str:
    """Lower-cased host[:port] of an origin string, or '' if it is not an http(s) origin."""
    try:
        u = urlsplit((value or "").strip())
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or not u.netloc:
        return ""
    return u.netloc.lower()


def allowed_hosts() -> set:
    raw = os.environ.get("SSC_ALLOWED_ORIGINS") or ""
    return {h for h in (_host(o) for o in raw.split(",")) if h}


def violation_reason() -> str | None:
    """None if the request passes; otherwise a short reason code."""
    if request.method not in MUTATING_METHODS:
        return None
    path = request.path or ""
    if any(path.startswith(p) for p in EXEMPT_PREFIXES):
        return None

    sfs = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if sfs:
        if sfs in ("same-origin", "none"):
            return None
        return f"sec-fetch-site:{sfs}"

    origin = request.headers.get("Origin")
    if origin is not None:
        h = _host(origin)
        if not h:
            return "origin-invalid"
        if h == (request.host or "").lower() or h in allowed_hosts():
            return None
        return "origin-mismatch"

    return None


def apply_csrf_guard(app) -> None:
    @app.before_request
    def _csrf_guard():
        m = mode()
        if m == "off":
            return None
        reason = violation_reason()
        if reason is None:
            return None
        logging.warning("csrf %s: %s %s reason=%s",
                        "BLOCKED" if m == "enforce" else "WOULD-BLOCK (report mode)",
                        request.method, request_logging.sanitize_path(request.path), reason)
        if m == "enforce":
            return jsonify({"error": "cross-site request blocked"}), 403
        return None
