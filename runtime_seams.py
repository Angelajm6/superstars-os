"""Single gate for test-only seams — they are inert unless a test run explicitly opts in.

The app has deterministic test seams that replace real behaviour with planted data:

  GOOGLE_OAUTH_FAKE_VERIFY   skip Google token verification (decode `code` as claims)
  DOC_SCAN_FAKE              replace the document AI scan with planted JSON
  EXPENSE_SCAN_FAKE          replace the expense AI scan with planted JSON
  SSC_TRACE_FAKE             replace the drawing-trace AI call with planted JSON
  SSC_MAIL_FAKE_DIR          write outbound mail to a folder instead of sending
  SSC_RESET_MAIL_FAKE_DIR    same, for password-reset mail (puts reset links on disk)
  SSC_ASSISTANT_FAKE         replace the Claude reply on estimates/proposals with planted text

Previously each was honored whenever its env var was set, guarded only by a comment saying
"never set in production". Per CLAUDE.md secrets/security rules that is not enough: one
stray env var would silently bypass SSO verification or divert mail.

Now every read goes through `seam(name)`, which returns the value ONLY when:
  1. SSC_ALLOW_TEST_SEAMS=1 is set (the test runners and seam-using smoke suites set it), AND
  2. the process does not look like production (RENDER=true, or SSC_ENVIRONMENT=production|prod).
Rule 2 is a hard stop: the opt-in cannot re-enable seams on Render.

Otherwise it returns "" — the seam is inert and the real code path runs. A seam variable
that is set but ignored is reported once at boot, by NAME only (never the value).
"""
from __future__ import annotations

import logging
import os

ALLOW_ENV = "SSC_ALLOW_TEST_SEAMS"

SEAM_VARS = (
    "GOOGLE_OAUTH_FAKE_VERIFY",
    "DOC_SCAN_FAKE",
    "EXPENSE_SCAN_FAKE",
    "SSC_TRACE_FAKE",
    "SSC_MAIL_FAKE_DIR",
    "SSC_RESET_MAIL_FAKE_DIR",
    "SSC_ASSISTANT_FAKE",
)


def looks_like_production() -> bool:
    if (os.environ.get("RENDER") or "").strip().lower() in {"1", "true", "yes"}:
        return True
    return (os.environ.get("SSC_ENVIRONMENT") or "").strip().lower() in {"production", "prod"}


def allowed() -> bool:
    return os.environ.get(ALLOW_ENV) == "1" and not looks_like_production()


def seam(name: str) -> str:
    """The seam's value if seams are allowed, else '' (inert). Unknown names are a bug."""
    if name not in SEAM_VARS:
        raise KeyError(f"unregistered test seam: {name}")
    if not allowed():
        return ""
    return os.environ.get(name) or ""


def warn_if_ignored() -> list:
    """Log (names only) any seam variable that is set but being ignored. Returns the names."""
    if allowed():
        return []
    ignored = [n for n in SEAM_VARS if (os.environ.get(n) or "").strip()]
    if ignored:
        logging.warning("test-only seam variable(s) set but IGNORED outside an opted-in test run: %s",
                        ", ".join(ignored))
    return ignored
