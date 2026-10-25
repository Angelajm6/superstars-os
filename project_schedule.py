"""The job dashboard's Schedule window: one payload for the whole view.

    job       start / target completion, days left, % of time used vs % of drops
              complete, and a pace verdict (from project_summary).
    lookahead the two-week board (lookahead.load_window), re-projected from THIS
              week's Drop Plan progress through the window being viewed, so paging
              ahead never shows stale or empty bars and past weeks aren't invented.
    items     the job's tasks, events and meetings in the window (team_items).
    site      the job's pin, for the weather flags on the day headers.

LOCAL dates only (CLAUDE.md): every date is a 'YYYY-MM-DD' string from local
date arithmetic, never UTC.
"""
from __future__ import annotations

from datetime import date, timedelta

import lookahead
import project_summary
import team_items

PACE_SLACK_PCT = 5   # drops may trail the calendar by this much and still be "on pace"


def this_monday(today: date | None = None) -> date:
    t = today or date.today()
    return t - timedelta(days=t.weekday())


def redraft(conn, code: str, window_start_iso: str, today: date | None = None) -> None:
    """Project the auto rows from this week's Monday through the end of the
    requested window (at least two working weeks). No commit."""
    anchor = this_monday(today)
    win_end = lookahead.working_days_window(date.fromisoformat(window_start_iso), 10)[-1]
    lookahead.draft_lookahead(conn, code, anchor.isoformat(), until_iso=max(win_end, anchor).isoformat())


def _job(conn, code: str, today: date) -> dict:
    h = project_summary._header(conn, code, today)
    s = project_summary._safe(conn, lambda: project_summary._schedule(conn, code), {}) or {}
    total, done = s.get("total") or 0, s.get("complete") or 0
    drops_pct = round(100 * done / total) if total else None
    time_pct = h.get("time_elapsed_pct")
    pace = None
    if time_pct is not None and drops_pct is not None:
        pace = "on_pace" if drops_pct + PACE_SLACK_PCT >= time_pct else "behind"
    return {"start_date": h.get("start_date"), "target_completion": h.get("target_completion"),
            "days_left": h.get("days_left"), "time_pct": time_pct,
            "drops_total": total, "drops_complete": done, "drops_pct": drops_pct, "pace": pace}


def _items(conn, code: str, d_from: str, d_to: str) -> list:
    rows = team_items.list_items(conn, status="any", date_from=d_from, date_to=d_to, project_code=code)
    return [{"id": r["id"], "kind": r["kind"], "title": r["title"], "status": r["status"],
             "from": r["calendar_from"], "to": r["calendar_to"] or r["calendar_from"],
             "time": r["start_time"] or r["due_time"], "assignee_name": r["assignee_name"]}
            for r in rows if r["status"] != "cancelled"]


def payload(conn, code: str, window_start_iso: str, today: date | None = None) -> dict:
    today = today or date.today()
    redraft(conn, code, window_start_iso, today)
    conn.commit()
    board = lookahead.load_window(conn, code, window_start_iso)
    site = conn.execute("SELECT site_lat, site_lng FROM projects WHERE project_code = ?", (code,)).fetchone()
    return {"job": _job(conn, code, today), "lookahead": board,
            "items": _items(conn, code, board["window_start"], board["window_end"]),
            "site": {"lat": site[0], "lng": site[1]} if site and site[0] is not None and site[1] is not None else None,
            "has_drops": bool(conn.execute("SELECT 1 FROM drops WHERE project_code = ? LIMIT 1", (code,)).fetchone())}
