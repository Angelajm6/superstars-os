#!/usr/bin/env python3
"""Materials field view (static/js/mt_field.js) — static contract, no server, no database.

The field view re-presents the existing Materials & Deliveries screen on PHONE widths only. This guard pins:
  * the project dashboard loads the script, and the screen still has every hook it drives
    (header buttons, catalog, expected panel, count panel, log modal + its fields, seg types)
  * phone only: every layout / step rule sits inside @media (max-width: 860px); desktop is unchanged
  * no network calls: the screen's own buttons, modals and save handlers do all the work
  * the log modal has three steps and refuses to move on without a material / a quantity
  * "On site now" is read from the catalog table (days left only when the table has it)
  * it backs off if the markup changes shape
  * the script parses (node --check when node exists)
Run:  venv/bin/python tests/smoke_mt_field.py
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
    js = (ROOT / "static/js/mt_field.js").read_text(encoding="utf-8")
    dash = (ROOT / "dashboard-static.html").read_text(encoding="utf-8")

    ok("dashboard loads mt_field.js with a cache key", re.search(r'/files/static/js/mt_field\.js\?v=\d+" defer', dash) is not None)
    hooks = ["mt-btn-log", "mt-btn-count", "mt-btn-add", "mt-btn-transfer", "mt-btn-expected", "mt-catalog", "mt-expected-panel",
             "mt-count-panel", "mt-modal-log", "mt-log-mat", "mt-log-qty", "mt-log-slip", "mt-log-save", "mt-log-err", "mt-log-seg", "mt-activity"]
    ok("the Materials screen still has every hook the field view drives", all(f'id="{h}"' in dash for h in hooks))
    ok("the log modal still offers delivery and self-pickup", 'data-t="delivery"' in dash and 'data-t="pickup"' in dash)
    ok("expected rows still carry the one-step 'mark received' link", "data-exrecv" in dash)

    css = js[js.index("var CSS"):js.index("var STEP_NAMES")]
    ok("layout and step rules are phone only (inside max-width: 860px)",
       css.count("@media (max-width: 860px)") == 2 and "#mt-modal-log:not(.mtf-s1)" in css.split("'}' +")[0])
    ok("no network calls: the screen's own handlers save", "fetch(" not in js and "XMLHttpRequest" not in js)
    ok("three steps: what came, how much, packing slip", "['What came', 'How much', 'Packing slip']" in js)
    ok("step 1 needs a material, step 2 a quantity", "Pick what came." in js and "Enter how much came." in js)
    ok("On site now is read from the catalog table (days left only when present)",
       ".mt-table tbody tr" in js and "left && left !== '—'" in js)
    ok("backs off when the markup changes shape", "markup changed: leave the screen alone" in js)

    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "--check", str(ROOT / "static/js/mt_field.js")], capture_output=True, text=True)
        ok("mt_field.js parses (node --check)", r.returncode == 0)

    print("PASS: materials field view" if not FAILS else f"FAIL: {len(FAILS)} check(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
