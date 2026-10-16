#!/usr/bin/env python3
"""Daily Report step checklist (static/js/dcr_steps.js) — static contract, no server, no database.

The checklist re-presents the existing Daily Report entry form on PHONE widths only. This guard pins the
promises that keep it safe:
  * the project dashboard loads the script, and the form still has the 11 .dcr-block sections it maps onto
  * phone only: a (max-width: 860px) media query gates it; desktop keeps the full form
  * the form's own Issue / Cancel / row-Save handlers are reused, never re-implemented (no fetch in the script)
  * required steps are Work done today + Safety; a No-Work day always unlocks Issue
  * 'Save & continue' clicks each filled row's own Save and drops empty unsaved rows
  * hide rules that fight inline display styles use !important (controls bar, action row)
  * the MutationObserver never watches the whole entry view (render() writes inside it: that would loop)
  * the script parses (node --check when node exists)
Run:  venv/bin/python tests/smoke_dcr_steps.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILS = []


def ok(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def main() -> int:
    js = (ROOT / "static/js/dcr_steps.js").read_text(encoding="utf-8")
    dash = (ROOT / "dashboard-static.html").read_text(encoding="utf-8")

    ok("dashboard loads dcr_steps.js with a cache key", re.search(r'/files/static/js/dcr_steps\.js\?v=\d+" defer', dash) is not None)
    ok("the entry form still has 11 numbered sections to map onto",
       len(re.findall(r'<h3 class="dcr-sec[^"]*">\d+[a-z]?\. ', dash)) >= 11 and 'id="dcr-entry-view"' in dash)
    ok("the form's own Issue and number preview exist", 'id="dcr-issue"' in dash and 'id="dcr-number-preview"' in dash)

    ok("phone only: gated by (max-width: 860px)", "matchMedia('(max-width: 860px)')" in js)
    ok("no network calls: the form's own handlers do the saving and issuing", "fetch(" not in js and "XMLHttpRequest" not in js)
    ok("required steps are Work done today and Safety",
       len(re.findall(r"req: true", js)) == 2 and "k: 'work'" in js and "k: 'safety'" in js)
    ok("a No-Work day unlocks Issue", "function ready() { return noWork() ||" in js)
    ok("Save & continue clicks each filled row's own Save", "[data-row-save]" in js and "b.click()" in js)
    ok("empty unsaved rows are dropped so the page's unsaved prompt never fires", "r.remove()" in js and "dataset.saved" in js)
    ok("hide rules beat the inline display styles", js.count("!important") >= 5 and ".ds-controls{display:none!important}" in js)
    ok("observers watch only the form's containers, never the whole entry view",
       "observe(entry, { childList: true" not in js and "mo.observe(c, { childList: true })" in js)
    ok("falls back to the full form if the markup changes shape", "blocks.length < 11" in js)
    ok("sticky bar clears the job v2 bottom bar", "body.jv2 .ds-bar{bottom:56px}" in js)

    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "--check", str(ROOT / "static/js/dcr_steps.js")], capture_output=True, text=True)
        ok("dcr_steps.js parses (node --check)", r.returncode == 0)

    print("PASS: daily report steps" if not FAILS else f"FAIL: {len(FAILS)} check(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
