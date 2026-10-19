#!/usr/bin/env python3
"""Company Dashboard — every tile / row links to the view behind it (static guard, no DB).

The Dashboard tab (company_portal.html, loadOverview) is the Today view (accepted prototype 2026-10-19):
four cards (Active jobs, On site now, Weather, Payments due), the week, "Needs you" (today_feed.py) and
"Your day". Each card / row must be a real link to a real view:
  * the four tiles carry an href, and they are the only four
  * "Money at a glance" is gone (markup, loader, link table)
  * every destination is an existing view: `#x[/y]` -> a key of LOADERS, `/#x` -> a location the
    shared company nav knows (so the link never lands on a dead page) — the tiles AND every href the
    server puts on a "Needs you" card
  * a card opens its page; its buttons are buttons, never an <a> nested inside an <a>
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
end = html.index("// ---------- TASKS & EVENTS", start)
fn = html[start:end]

# ---- the four tiles
kpis = re.findall(r"tile\('([^']+)',.*?, '(#[a-z]+|/#[a-z]+)'\)", fn, re.S)
labels = [k for k, _ in kpis]
ok("four tiles are links", len(kpis) == 4)
ok("tiles are Active jobs, On site now, Weather, Payments due", labels == ["Active jobs", "On site now", "Weather", "Payments due"])

# ---- no money block on the dashboard
for gone in ("ov-money", "Money at a glance", "OV_MONEY_LINKS", "ov-asof"):
    ok(f"dashboard has no {gone}", gone not in html)
ok("the Tasks & events card replaces it", 'id="tk-card"' in html and "<h2>Tasks &amp; events</h2>" in html and "loadTasks();" in fn)
feed = (ROOT / "today_feed.py").read_text(encoding="utf-8") + (ROOT / "inbox.py").read_text(encoding="utf-8")
links = {i: h for i, h in enumerate(re.findall(r'href="(#[a-z][^"{]*|/#[a-z][^"{]*)"', feed) + re.findall(r"href=f?\"(#[a-z]+)", feed))}
ok("Needs you cards carry their page", len(links) >= 6 and "href=\"#finance\"" in feed and "#inbox/" in feed)

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
ok("link targets were collected", len(targets) >= 3)

# ---- markup: tiles are anchors, rows are one link, no nested anchors
ok("KPI helper renders an <a> when given a link", "cp-kpi cp-kpi-link" in html and 'aria-label="${esc(k)} — open"' in html)
ok("a card is one clickable article with buttons, no nested links", '<article class="td-card' in fn and "data-td-act" in fn and "function tdGo(c)" in fn)
ok("links are keyboard-focusable and visibly focused", "a.cp-kpi-link:focus-visible" in html)

if FAILS:
    print(f"FAIL: {len(FAILS)} check(s) failed")
    sys.exit(1)
print("PASS: company dashboard links")
