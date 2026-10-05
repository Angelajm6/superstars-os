#!/usr/bin/env python3
"""Company Dashboard — every tile / row links to the view behind it (static guard, no DB).

The Dashboard tab (company_portal.html, loadOverview) renders six KPI tiles, the "Money at a
glance" rows and the "Needs attention" rows. Each must be a real link to a real view:
  * all six KPI tiles carry an href
  * every Money row label has a destination in OV_MONEY_LINKS (a new row cannot ship link-less)
  * every destination is an existing view: `#x[/y]` -> a key of LOADERS, `/#x` -> a location the
    shared company nav knows (so the link never lands on a dead page)
  * Needs-attention rows are one link per row (no <a> nested inside an <a>)
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
html = (ROOT / "company_portal.html").read_text(encoding="utf-8")
nav = (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")
FAILS = []


def ok(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


start = html.index("async function loadOverview()")
end = html.index("// ---------- CLIENTS ----------", start)
fn = html[start:end]

# ---- the six KPI tiles
kpis = re.findall(r"kpi\('([^']+)',.*?, '(#[a-z]+|/#[a-z]+)'\)", fn)
labels = [k for k, _ in kpis]
ok("six KPI tiles are links", len(kpis) == 6)
ok("KPI tiles are the expected six", labels == ["Active projects", "Open pipeline", "Won this year", "Spend this month", "Workforce", "Expiring soon"])

# ---- money rows all have a destination
money_block = fn[fn.index("$('ov-money').innerHTML = ["): fn.index("].map(([k, v]) => { const h = OV_MONEY_LINKS[k]")]
row_labels = re.findall(r"^\s*\['([^']+)',", money_block, re.M)
links_src = html[html.index("const OV_MONEY_LINKS = {"): html.index("async function loadOverview()")]
links = dict(re.findall(r"'([^']+)':\s*'([^']+)'", links_src))
ok("money rows were found", len(row_labels) >= 10)
ok("every Money row has a destination", all(l in links for l in row_labels))
ok("no stale destination for a removed row", all(k in row_labels for k in links))

# ---- every destination is a real view
loaders = set(re.findall(r"(\w+):\s*load\w+", html[html.index("const LOADERS = {"): html.index("}", html.index("const LOADERS = {"))]))
nav_pairs = set(re.findall(r"\['(/[a-z]*)', '([a-z]*)'\]", nav))
targets = [h for _, h in kpis] + list(links.values()) + re.findall(r'href="(#[^"]+|/#[^"]+)"', fn)
bad = []
for t in targets:
    if t.startswith("/#"):
        if ("/", t[2:].split("/")[0]) not in nav_pairs:
            bad.append(t)
    else:
        if t[1:].split("/")[0] not in loaders:
            bad.append(t)
ok("every destination is an existing view", not bad)
if bad:
    print("    unknown destinations:", sorted(set(bad)))
ok("link targets were collected", len(targets) >= 20)

# ---- markup: tiles are anchors, rows are one link, no nested anchors
ok("KPI helper renders an <a> when given a link", "cp-kpi cp-kpi-link" in html and 'aria-label="${esc(k)} — open"' in html)
ok("attention rows are a single link per row", '<li class="has-link"><a href="${m[1]}"><span>${esc(k)}</span><span class="cp-link">${m[2]}</span></a></li>' in fn)
ok("money rows are a single link per row", '<li class="has-link"><a href="${h}">' in fn)
ok("an inner link in a money row is flattened (no <a> inside <a>)", "replace(/<a [^>]*>([^<]*)<\\/a>/g, '<span class=\"cp-link\">$1</span>')" in fn)
ok("links are keyboard-focusable and visibly focused", "a.cp-kpi-link:focus-visible" in html)

if FAILS:
    print(f"FAIL: {len(FAILS)} check(s) failed")
    sys.exit(1)
print("PASS: company dashboard links")
