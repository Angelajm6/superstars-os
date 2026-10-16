#!/usr/bin/env python3
"""One type system for every screen (accepted 2026-10-09, "Inter only"). Static guard.

Proves:
  * no screen names Archivo any more (printed cards and Python-rendered documents keep their own)
  * no screen uses an extra-bold weight (800 / 900): titles are medium, nothing heavy
  * on-screen numbers and IDs are not in a monospace font (one family; CLAUDE.md: internal IDs
    never monospace) — the raw-JSON debug box of the RFI form is the one exception
  * typography.css carries the system (Inter, the title / card / label / number tokens, the
    company dashboard link in capitals) and every page loads it with the same version tag
Run:  venv/bin/python tests/smoke_type_system.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCREENS = ["dashboard-static.html", "company_portal.html", "company-dashboard.html", "projects.html", "account.html",
           "admin_users.html", "admin_projects.html", "admin_labor_rates.html", "estimating.html", "login.html",
           "set_password.html", "forgot_password.html", "reset_password.html", "client_portal.html", "portal_shell.html",
           "welcome.html", "worker-app.html", "rfi_submission_form.html", "static/css/widgets.css",
           "static/css/shell.css", "static/js/new_project_modal.js", "templates/v2/drawing-markup.html",
           "templates/v2/drawing-author.html", "static/v2/v2.css"]


def main() -> int:
    bad = []
    for f in SCREENS:
        s = (ROOT / f).read_text(encoding="utf-8")
        if "Archivo" in s:
            bad.append(f"{f}: still names Archivo")
        if re.search(r"font-weight:\s*(800|900)\b", s):
            bad.append(f"{f}: extra-bold weight")
        mono = re.findall(r"font-family:\s*[^;\"}]*monospace", s)
        if f == "rfi_submission_form.html":
            mono = mono[1:] if len(mono) == 1 else mono   # the raw-JSON debug box
        if mono:
            bad.append(f"{f}: monospace on screen ({len(mono)})")
    t = (ROOT / "static" / "fonts" / "typography.css").read_text(encoding="utf-8")
    for need in ("--font: 'Inter'", "--t-title: 22px", "--t-title-w: 500", "--t-card: 16px", "--t-label: 11px",
                 "html body .tab-head h2", "html body .ssc-top h1", "html body .bl-company { font-size: 11.5px; font-weight: 400; letter-spacing: .1em; text-transform: uppercase;",
                 # sidebar + page titles in spaced capitals (accepted 2026-10-10, option B); the greeting keeps normal case
                 "font-size: var(--t-title); font-weight: var(--t-title-w); letter-spacing: .04em; text-transform: uppercase;",
                 "html body :is(.nav-link, .ssc-side .ssc-nav a, .ssc-side .ssc-nav .nav-tab, .ssc-side .ssc-side-foot a) {\n  font-size: 12px; font-weight: 500; letter-spacing: .06em; text-transform: uppercase;",
                 "html body .nav-group-label { font-size: 10.5px; font-weight: 500; letter-spacing: .12em; }",
                 "html body .hello h1 { font-size: 26px;"):
        if need not in t:
            bad.append(f"typography.css misses: {need}")
    tags = set()
    for f in [p for p in ROOT.glob("*.html")] + list((ROOT / "templates" / "v2").glob("*.html")):
        tags |= set(re.findall(r"fonts/typography\.css(\?v=\d+)?\"", f.read_text(encoding="utf-8", errors="replace")))
    if len(tags) != 1 or "" in tags:
        bad.append(f"typography.css is loaded with mixed version tags: {sorted(tags)}")
    if bad:
        print("FAIL smoke_type_system:\n  " + "\n  ".join(bad))
        return 1
    print(f"PASS smoke_type_system ({len(SCREENS)} screens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
