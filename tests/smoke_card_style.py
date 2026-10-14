#!/usr/bin/env python3
"""One card style + the weather window (accepted 2026-10-09). Static + pure-function guard,
no server, no database.

Proves:
  * typography.css carries the corner scale (cards 14, inside a card 10, tags 6, pills round)
    and the shared card / window / inner rules, including the Project Health cards
  * no screen sets a card-size corner other than 14 in its own CSS unless the shared rule
    covers that selector (a new card with 12 / 16 / 18 / 22 fails here)
  * weather_week.build_week turns a canned Open-Meteo payload into 7 days: weekdays from the
    local date strings, 6 readings through the day with the work day flagged, humidity
    averaged, sunrise/sunset as clock times, and the heads-up lines for gusts and rain
  * the job page opens the window from Field today (button role, keyboard, Esc) and the
    route is registered
Run:  venv/bin/python tests/smoke_card_style.py
"""
from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCREENS = ["dashboard-static.html", "company_portal.html", "company-dashboard.html", "projects.html", "account.html",
           "admin_projects.html", "login.html", "portal_shell.html", "worker-app.html", "static/css/widgets.css",
           "static/js/new_project_modal.js"]
# corners that are not cards: phone illustration in the new-project modal, the projects
# drawer (bottom sheet on phones), round avatars / pills
EXEMPT = {".np-phone", ".panel", ".ph-wrap .jx-hero::after",
          ".shutter-btn.recording"}   # the worker app record button turns into a rounded-square stop shape


def shared_selectors(css: str) -> set[str]:
    out = set()
    for block in re.findall(r"html body :is\(([^)]*(?:\([^)]*\)[^)]*)*)\)\s*\{\s*border-radius", css):
        out |= {" ".join(x.split()) for x in block.split(",") if x.strip()}
    return out


def main() -> int:
    bad = []
    t = (ROOT / "static" / "fonts" / "typography.css").read_text(encoding="utf-8")
    for need in ("--r-card: 14px", "--r-inner: 10px", "--r-tag: 6px", "--r-pill: 999px", "--card-shadow:",
                 "html body :is(.ph-wrap .jx-card, .ph-wrap .jx-kpi) { border: 1px solid var(--card-line); }"):
        if need not in t:
            bad.append(f"typography.css misses: {need}")
    covered = shared_selectors(t)
    for need in (".ph-wrap .jx-card", ".ph-wrap .jx-kpi", ".ph-wrap .jx-hero", ".cp-card", ".shc-card", ".pc-card",
                 ".jx-modal", ".wxw", ".wx-day", ".ph-wrap .jx-alert", ".btn-primary", ".ssc-field"):
        if need not in covered:
            bad.append(f"shared corner rule does not cover {need}")

    # every card-size corner (11–24px) in page CSS is either 14 or covered by the shared rule
    for f in SCREENS:
        s = (ROOT / f).read_text(encoding="utf-8")
        for m in re.finditer(r"([^{}]+)\{([^{}]*?)border-radius:\s*([0-9.]+)px", s):
            v = float(m.group(3))
            if not (11 <= v <= 24) or v == 14:
                continue
            sels = [" ".join(x.split()) for x in re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S).split(",")]
            for sel in sels:
                sel = sel.strip()
                if not sel.startswith((".", "html")) or sel in EXEMPT or "::" in sel:
                    continue
                if sel not in covered:
                    bad.append(f"{f}: {sel} uses {v:g}px (cards are 14, inside a card 10)")
    for f in ("projects.html", "login.html"):
        if "border-radius: var(--r-card, 14px)" not in (ROOT / f).read_text(encoding="utf-8"):
            bad.append(f"{f}: card does not use --r-card")

    # ---- weather_week.build_week on a canned payload
    import weather_week
    d0 = date(2026, 10, 9)   # a Friday
    days = [(d0 + timedelta(days=i)).isoformat() for i in range(7)]
    htime, temp, prob, wind, code, hum = [], [], [], [], [], []
    for i, ds in enumerate(days):
        for h in range(24):
            htime.append(f"{ds}T{h:02d}:00"); temp.append(50 + h / 2); wind.append(5 + i)
            prob.append(70 if (i == 1 and 7 <= h <= 15) else 10); code.append(61 if i == 1 else 2); hum.append(60 if h % 2 else 70)
    payload = {"daily": {"time": days, "weather_code": [2, 61, 0, 3, 2, 80, 0],
                         "temperature_2m_max": [64, 58, 62, 60, 63, 61, 66], "temperature_2m_min": [52, 49, 47, 50, 51, 53, 50],
                         "precipitation_probability_max": [10, 70, 0, 20, 10, 50, 0],
                         "precipitation_sum": [0, .42, 0, .01, 0, .15, 0],
                         "wind_speed_10m_max": [9, 14, 7, 10, 16, 11, 6], "wind_gusts_10m_max": [16, 31, 12, 18, 29, 20, 11],
                         "wind_direction_10m_dominant": [315, 90, 315, 180, 270, 225, 0],
                         "sunrise": [f"{ds}T07:06" for ds in days], "sunset": [f"{ds}T18:24" for ds in days]},
               "hourly": {"time": htime, "temperature_2m": temp, "precipitation_probability": prob,
                          "wind_speed_10m": wind, "weather_code": code, "relative_humidity_2m": hum}}
    w = weather_week.build_week(payload, {2: "Partly cloudy", 61: "Light rain"}, today=d0)
    D = w["days"]
    checks = [
        ("7 days", len(D) == 7),
        ("weekdays from local date strings", [x["weekday"] for x in D] == ["Fri", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu"]),
        ("today flagged once", [x["is_today"] for x in D].count(True) == 1 and D[0]["is_today"]),
        ("6 readings, 7 AM … 10 PM", [h["hour"] for h in D[0]["hours"]] == [7, 10, 13, 16, 19, 22]),
        ("work day flagged 7 AM – 3:30 PM", [h["work"] for h in D[0]["hours"]] == [True, True, True, False, False, False]),
        ("humidity averaged", D[0]["humidity"] == 65),
        ("sunrise/sunset as clock times", D[0]["sunrise"] == "7:06 AM" and D[0]["sunset"] == "6:24 PM"),
        ("wind compass", D[0]["wind_dir"] == "NW" and D[1]["wind_dir"] == "E" and D[6]["wind_dir"] == "N"),
        ("labels", D[0]["label"] == "Partly cloudy" and D[1]["label"] == "Light rain" and D[2]["label"] == "—"),
        ("calm dry day has no heads-up", D[0]["heads_up"] == []),
        ("gusts + work-day rain heads-up", any("Gusts up to 31 mph" in x for x in D[1]["heads_up"]) and any("70% chance of rain during the work day" in x for x in D[1]["heads_up"])),
        ("gusts below 25 mph stay quiet", D[3]["heads_up"] == []),
        ("empty payload is safe", weather_week.build_week({})["days"] == []),
    ]
    bad += [f"build_week: {name}" for name, ok in checks if not ok]

    # ---- the job page and the route
    page = (ROOT / "dashboard-static.html").read_text(encoding="utf-8")
    for need in ('id="jx-wx" role="button" tabindex="0" aria-haspopup="dialog"', 'class="wx-more">See the week →',
                 'id="wxw-bg"', 'role="dialog" aria-modal="true" aria-labelledby="wxw-t"', 'id="wxw-prev"', 'id="wxw-next"',
                 "fetch('/api/weather/week'", "e.key === 'Escape'", "e.key === 'ArrowLeft'", "e.key === 'ArrowRight'",
                 "Weather is unavailable right now. Try again in a few minutes."):
        if need not in page:
            bad.append(f"job page misses: {need}")
    if "@app.route('/api/weather/week', methods=['GET'])" not in (ROOT / "server.py").read_text(encoding="utf-8"):
        bad.append("route /api/weather/week missing")

    if bad:
        print("FAIL smoke_card_style:\n  " + "\n  ".join(bad))
        return 1
    print(f"PASS smoke_card_style ({len(SCREENS)} screens, {len(covered)} shared selectors, {len(checks)} weather checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
