"""Project page — the redesigned tabs (default for staff since 2026-11-11).

The project page (dashboard-static.html) is ~11,000 lines of working screens. Rather than fork a
second copy, the redesign re-presents it. On a project's own page, for a staff role, the served page gets

  * <body class="jv2">                       scopes every rule
  * /files/static/v2/job.css                 app bar, project header, six colored sections, pills,
                                             the shared look every tab picks up, phone layout
  * /files/static/v2/job.js                  sections + pills over the existing nav links, Jump to
                                             (Ctrl/Cmd+K), + New, "Needs you" on the Overview

Accepted prototype: https://claude.ai/artifact/VGzVhPeig191nXeUsPybgZ

Scope: ONLY a project's own page (/projects/<code>, and /portal/<code> for staff roles). Never
the company console, the projects list, admin pages, login, the worker app or the legacy
/dashboard entry (no project), and never an external role.

The way back to the classic page (served BYTE-FOR-BYTE unchanged):
  * `?ui=1` on one request
  * the SSC_UI_FORCE_V1 kill switch (everyone, every request)
Hiding / regrouping navigation is presentation only; the endpoints keep their own role checks.
"""
from __future__ import annotations

import re
from pathlib import Path

import ui_version

# roles served the internal project page (external roles get the portal shell, never this page)
INTERNAL_ROLES = frozenset({"admin", "c_suite", "pm", "super"})

_STATIC = Path(__file__).resolve().parent / "static" / "v2"


def _asset_version() -> str:
    """Changes whenever job.css / job.js change, so a browser never serves a stale copy."""
    try:
        return str(int(max((_STATIC / n).stat().st_mtime for n in ("job.css", "job.js"))))
    except OSError:
        return "0"


def _classic_requested() -> bool:
    """`?ui=1` on this request asks for the classic page (never stored)."""
    try:
        from flask import has_request_context, request
        return has_request_context() and (request.args.get("ui") or "").strip() == "1"
    except Exception:   # noqa: BLE001 — outside a request: no override
        return False


def active(role, project_code) -> bool:
    """True when this request gets the redesigned project page: a specific project's own page,
    for a staff role, unless the kill switch is on or this request asked for ?ui=1."""
    if not project_code or role not in INTERNAL_ROLES:
        return False
    if ui_version.force_v1() or _classic_requested():
        return False
    return True


def apply(html: str, role, project_code=None) -> str:
    if not active(role, project_code):
        return html
    out = re.sub(r"<body([^>]*)>", lambda m: '<body%s class="jv2">' % m.group(1), html, count=1)
    if out == html or "</head>" not in out or "</body>" not in out:
        return html   # unexpected shape: serve the classic page rather than a half-applied one
    v = _asset_version()
    out = out.replace("</head>", '<link rel="stylesheet" href="/files/static/v2/job.css?v=%s">\n</head>' % v, 1)
    out = out.replace("</body>", '<script src="/files/static/v2/job.js?v=%s" defer></script>\n</body>' % v, 1)
    return out
