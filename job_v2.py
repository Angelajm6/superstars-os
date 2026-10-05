"""Job dashboard — v2 preview overlay (opt-in, additive).

The job dashboard (dashboard-static.html) is ~9,800 lines of working screens. Rather than fork a
second copy, the v2 preview re-presents it: when a request resolves to UI v2 (ui_version.current()
== 2 — `?ui=2` for one request, or the user's saved preference) the served page gets

  * <body class="jv2">                       scopes every v2 rule
  * /files/static/v2/job.css                 one font family + one type scale, top navigation,
                                             calmer cards, phone layout
  * /files/static/v2/job.js                  five top tabs over the existing nav links, an
                                             attention list + quick links on the Overview
  * /files/static/v2/switch-back.js          the "You're on the new interface · Switch back" bar

Anything else — v1 requests, the SSC_UI_FORCE_V1 kill switch, external roles — is returned
BYTE-FOR-BYTE unchanged. Nothing is renamed: every label comes from the existing nav links.
Hiding / re-grouping navigation is presentation only; the endpoints keep their own role checks.
"""
from __future__ import annotations

import re
from pathlib import Path

import ui_version

# roles served the internal job dashboard (external roles get the portal shell, never this page)
INTERNAL_ROLES = frozenset({"admin", "c_suite", "pm", "super"})

_STATIC = Path(__file__).resolve().parent / "static" / "v2"


def _asset_version() -> str:
    """Changes whenever job.css / job.js change, so a browser never serves a stale copy."""
    try:
        return str(int(max((_STATIC / n).stat().st_mtime for n in ("job.css", "job.js"))))
    except OSError:
        return "0"


def active(role) -> bool:
    """True when this request should get the v2 job dashboard."""
    return role in INTERNAL_ROLES and ui_version.current() == 2


def apply(html: str, role) -> str:
    if not active(role):
        return html
    out = re.sub(r"<body([^>]*)>", lambda m: '<body%s class="jv2">' % m.group(1), html, count=1)
    if out == html or "</head>" not in out or "</body>" not in out:
        return html   # unexpected shape: serve the classic page rather than a half-applied one
    v = _asset_version()
    out = out.replace("</head>", '<link rel="stylesheet" href="/files/static/v2/job.css?v=%s">\n</head>' % v, 1)
    out = out.replace("</body>", '<script src="/files/static/v2/job.js?v=%s" defer></script>\n'
                                 '<script src="/files/static/v2/switch-back.js" defer></script>\n</body>' % v, 1)
    return out
