"""Request logging — metadata only, never request content.

Per CLAUDE.md PII rule (and the pasted AI-layer rule: never log email bodies,
attachments or personal data), the access log records IDs and metadata:
method, a sanitized path, status, and body SIZE. It never reads or records the
request body, so a future inbox-derived field (subject, sender, body) cannot
reach server.log by passing through an API.

This is an ALLOWLIST by construction: nothing from the body is logged, so there
is no denylist of "sensitive keys" to keep up to date.

Paths are sanitized because two route families carry secrets or names in the URL:
  * token-as-credential client links  (/p/<token>, /api/p/<token>, /i/<token>, /d/<token>)
  * worker file mounts                (/worker-files/E-#####_<First>_<Last>/...)
Query strings are never logged (request.path excludes them).
"""
from __future__ import annotations

import logging
import re

from flask import request

_TOKEN_PATH = re.compile(r"^(/(?:api/)?(?:p|i|d))/[^/?#]+")
_WORKER_FILES = re.compile(r"^(/worker-files)/.*")


def sanitize_path(path: str) -> str:
    """Mask credential-bearing and name-bearing URL segments."""
    path = path or ""
    path = _TOKEN_PATH.sub(r"\1/<redacted>", path, count=1)
    path = _WORKER_FILES.sub(r"\1/<redacted>", path, count=1)
    return path


def apply_request_logging(app) -> None:
    """Register the request/response log hooks (metadata only)."""
    @app.before_request
    def _log_request():
        # content_length comes from the header; the body is never read here.
        logging.info("%s %s | body_bytes=%s",
                     request.method, sanitize_path(request.path), request.content_length or 0)

    @app.after_request
    def _log_response(response):
        logging.info("Response: %s %s", response.status_code, sanitize_path(request.path))
        return response
