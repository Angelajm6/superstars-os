#!/usr/bin/env python3
"""Every script the app serves must parse (2026-11-12).

Why: PR #212 put a `//` comment in the middle of a line in dashboard-static.html. The rest of the line became a comment,
the browser stopped at the syntax error, and the job page's tabs did nothing in production. Every other test passed,
because none of them runs the page's JavaScript. This guard parses it all with Node (no browser needed):

  * every inline <script> (no src) in every page at the repo root and in templates/v2
  * each page again as each role is served it (access.render_sections + render_role_nav strip blocks per role,
    and a strip that cuts a script in half would break only that role's page)
  * every standalone file under static/js and static/v2, plus the worker app's service worker
  * a self-test: the exact line that broke the tabs must be caught

Node is on GitHub's ubuntu runners. Without Node this fails in CI and is skipped (with a warning) on a laptop.

Run:  venv/bin/python tests/smoke_inline_js.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import access  # noqa: E402

ROLES = ("admin", "c_suite", "pm", "super", "estimator", "client", "architect")
_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)
_TYPE = re.compile(r"""\btype\s*=\s*["']?([^"'\s>]+)""", re.I)
# One Node process parses everything: classic scripts as scripts, modules as modules.
_NODE = r"""
const fs = require('fs'), vm = require('vm');
const items = JSON.parse(fs.readFileSync(process.argv[2], 'utf8')), bad = [];
for (const it of items) {
  const src = fs.readFileSync(it.file, 'utf8');
  try {
    if (it.module) new vm.SourceTextModule(src, { identifier: it.label });
    else new vm.Script(src, { filename: it.label });
  } catch (e) {
    if (it.module && !vm.SourceTextModule) continue;
    const m = String(e.stack || e).split('\n');
    bad.push(it.label + ': ' + (e.message || e) + (m[0] && m[0].includes(':') ? '  @ ' + m[0] : ''));
  }
}
process.stdout.write(JSON.stringify(bad));
"""


def scripts(html: str) -> list:
    """(index, code, is_module) for each inline script the browser would run."""
    out = []
    for i, m in enumerate(_SCRIPT.finditer(html)):
        attrs, code = m.group(1), m.group(2)
        if re.search(r"\bsrc\s*=", attrs, re.I) or not code.strip():
            continue
        t = (_TYPE.search(attrs).group(1).lower() if _TYPE.search(attrs) else "text/javascript")
        if t in ("module",):
            out.append((i, code, True))
        elif t in ("text/javascript", "application/javascript", "javascript"):
            out.append((i, code, False))
        # application/json, text/template, importmap, … are data, not code
    return out


def variants(html: str) -> dict:
    """The page as it is on disk and as each role is served it (deduplicated)."""
    out = {"as written": html}
    seen = {html}
    for role in ROLES:
        try:
            v = access.render_role_nav(access.render_sections(html, role), role)
        except Exception:
            continue
        if v not in seen:
            seen.add(v)
            out[f"as served to {role}"] = v
    return out


def collect(tmp: Path) -> list:
    items = []

    def add(label: str, code: str, module: bool):
        f = tmp / f"s{len(items)}.js"
        f.write_text(code, encoding="utf-8")
        items.append({"file": str(f), "label": label, "module": module})

    pages = sorted(ROOT.glob("*.html")) + sorted((ROOT / "templates" / "v2").glob("*.html"))
    for page in pages:
        rel = page.relative_to(ROOT).as_posix()
        for how, html in variants(page.read_text(encoding="utf-8", errors="replace")).items():
            for i, code, module in scripts(html):
                add(f"{rel} ({how}) <script> #{i + 1}", code, module)
    for d in (ROOT / "static" / "js", ROOT / "static" / "v2"):
        for f in sorted(d.glob("*.js")):
            src = f.read_text(encoding="utf-8", errors="replace")   # import / export at the top level = loaded as type="module"
            add(f.relative_to(ROOT).as_posix(), src, bool(re.search(r"^\s*(?:import|export)\b", src, re.M)))
    sw = ROOT / "worker-app-sw.js"
    if sw.exists():
        add("worker-app-sw.js", sw.read_text(encoding="utf-8", errors="replace"), False)
    return items


def parse_all(node: str, items: list, tmp: Path) -> list:
    lst = tmp / "items.json"
    lst.write_text(json.dumps(items), encoding="utf-8")
    prog = tmp / "check.js"
    prog.write_text(_NODE, encoding="utf-8")
    r = subprocess.run([node, "--experimental-vm-modules", str(prog), str(lst)], capture_output=True, text=True, timeout=300)
    if r.returncode != 0 and not r.stdout.strip():
        raise RuntimeError("node failed: " + r.stderr[-800:])
    return json.loads(r.stdout or "[]")


def main() -> int:
    node = shutil.which("node")
    if not node:
        if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            print("FAIL: Node is not installed, so the app's JavaScript can't be checked")
            return 1
        print("SKIP: Node is not installed on this machine (CI runs this check)")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="ssc_inline_js_"))
    try:
        # self-test: the line from PR #212 (a `//` comment mid-line) must be caught, its fixed form must pass
        broken = ("<script>\ndocument.addEventListener('DOMContentLoaded', () => { if (location.hash === '#payments') "
                  "setTimeout(() => { const l = document.querySelector('.nav-link[data-view=payments]');   // note "
                  "if (l && !l.hidden) l.click(); }, 0); });\n</script>")
        fixed = broken.replace("   // note ", " ")
        probe = []
        for label, html in (("broken", broken), ("fixed", fixed)):
            for i, code, module in scripts(html):
                f = tmp / f"probe-{label}.js"
                f.write_text(code, encoding="utf-8")
                probe.append({"file": str(f), "label": label, "module": module})
        bad = parse_all(node, probe, tmp)
        assert len(bad) == 1 and bad[0].startswith("broken"), f"self-test failed: {bad}"
        # data blocks are skipped, modules are parsed as modules
        assert not scripts('<script type="application/json">{"a": </script>')
        assert scripts('<script type="module">import x from "./x.js";</script>')[0][2] is True

        items = collect(tmp)
        assert len(items) > 50, f"only {len(items)} scripts found: the page scan is broken"
        bad = parse_all(node, items, tmp)
        if bad:
            print("FAIL: these scripts don't parse, so the browser stops running them:")
            for b in bad[:40]:
                print("  - " + b)
            return 1
        pages = len({i["label"].split(" (")[0] for i in items if "<script>" in i["label"]})
        print(f"PASS: every script parses — {len(items)} scripts across {pages} pages (each role's version) and the static JS files")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
