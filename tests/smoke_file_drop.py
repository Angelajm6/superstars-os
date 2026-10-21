#!/usr/bin/env python3
"""Every page with a file input also takes a dragged file (static/js/file_drop.js, 2026-10-20).

Some browsers never open the file window (a managed-Chrome policy, an extension); dragging still works.
Static guard: every repo page that has <input type="file"> (or builds one in its script) loads file_drop.js,
and the helper keeps its promises (drop goes to the input's own change handler, a drop another spot took is
left alone, a stray drop never opens the file in the tab, the tip when no window opens).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILS = []


def ok(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


pages = [p for p in list(ROOT.glob("*.html")) + list((ROOT / "templates" / "v2").glob("*.html"))
         if re.search(r"type=[\"']file[\"']", p.read_text(encoding="utf-8"))]
skip = {"worker-app.html"}   # phones: no drag and drop
missing = [p.name for p in pages if p.name not in skip and "/files/static/js/file_drop.js" not in p.read_text(encoding="utf-8")]
ok("pages with file inputs load file_drop.js", not missing)
if missing:
    print("    missing:", ", ".join(missing))
js = (ROOT / "static/js/file_drop.js").read_text(encoding="utf-8")
ok("drop feeds the input's own change handler", "t.input.files = dt.files" in js and "new Event('change', { bubbles: true })" in js)
ok("a drop another spot handled is left alone", "if (e.defaultPrevented) return;   // the spot handled it itself" in js)
ok("a stray drop never opens the file in the tab", "e.preventDefault();   // never let the browser open the file" in js)
ok("camera-only inputs are not drop targets", "!inp.hasAttribute('capture')" in js)
ok("a tip says to drag when no file window opens", "No file window?" in js)
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s) failed")
    sys.exit(1)
print("PASS: file drop")
