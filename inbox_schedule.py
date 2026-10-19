"""Inbox · Mine / All (prototype accepted 2026-10-16: https://claude.ai/artifact/9rvSjzcZiLxcjdBYRTNeHV).

  Mine  everyone with a team login (admin, c_suite, pm, super): their OWN Google Calendar events
        (calendar_import.user_calendar_event, private) + the tasks / events / meetings they are on
        (assigned to them, or a meeting they are in) + their open overdue tasks.
  All   admin / c_suite only: every task / event / meeting assigned to anyone, with the assignee,
        filtered by assignee, type and dates. NEVER anyone's private calendar events.

Access is decided HERE, on the server: `scope=all` from any other role is a 403, and the private
events are read only through calendar_import.events_for(signed-in user's id). No request value can
name another person's calendar (the assignee filter only narrows team items).

  GET /api/inbox/schedule?scope=mine|all&from=YYYY-MM-DD&to=YYYY-MM-DD&type=task|event|meeting&assignee=<id>
  GET /inbox                     the Inbox page for PMs and supers (Mine); admins go to /company#inbox/mine
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from flask import jsonify, redirect, request, send_file

import db_layer
from auth import current_user, requires_role

log = logging.getLogger("ssc.inbox_schedule")

TEAM_ROLES = ("admin", "c_suite", "pm", "super")
ALL_ROLES = ("admin", "c_suite")
MAX_DAYS = 92
OVERDUE_DAYS = 60
PAGE = Path(__file__).resolve().parent / "inbox_page.html"


def _day(v, fallback: date) -> date:
    s = str(v or "").strip()[:10]
    if not s:
        return fallback
    return date.fromisoformat(s)   # ValueError -> 400


def _window(args) -> tuple:
    today = date.today()
    d_from = _day(args.get("from"), today)
    d_to = _day(args.get("to"), d_from + timedelta(days=13))
    if d_to < d_from:
        raise ValueError("the end date is before the start date")
    if (d_to - d_from).days > MAX_DAYS:
        raise ValueError(f"pick at most {MAX_DAYS} days")
    return d_from.isoformat(), d_to.isoformat()


def _calendar_status(conn, uid) -> dict:
    import gcal_sync
    st = gcal_sync.status_for(conn, uid)
    return {"available": st["available"], "connected": st["connected"], "reads": st["reads_calendar"],
            "other_calendars": st["lists_calendars"],
            "needs_reconnect": st["status"] == "needs_reconnect", "synced_at": st.get("import_at"),
            "problem": st.get("problem")}


def mine(conn, uid, d_from: str, d_to: str, kind=None, can_edit=False) -> dict:
    """The signed-in person's own view. `uid` is always current_user()['id']."""
    import calendar_import
    import team_items
    items = team_items.list_items(conn, status="all", mine_uid=uid, date_from=d_from, date_to=d_to, me=uid,
                                  can_edit=can_edit, kind=kind if kind in team_items.KINDS else None)
    items = [i for i in items if not (i["status"] == "done" and (i["calendar_to"] or i["calendar_from"] or "") < date.today().isoformat())]
    seen = {i["id"] for i in items}
    if kind in (None, "", "task"):
        cutoff = (date.fromisoformat(d_from) - timedelta(days=OVERDUE_DAYS)).isoformat()
        for i in team_items.list_items(conn, status="open", mine_uid=uid, date_from=cutoff,
                                       date_to=(date.fromisoformat(d_from) - timedelta(days=1)).isoformat(), me=uid,
                                       can_edit=can_edit, kind="task"):
            if i["id"] not in seen:
                items.insert(0, i)
    events = calendar_import.events_for(conn, uid, d_from, d_to) if kind in (None, "", "calendar") else []
    if kind == "calendar":
        items = []
    return {"range": {"from": d_from, "to": d_to}, "items": items, "calendar": events,
            "calendar_status": _calendar_status(conn, uid)}


def everyone(conn, uid, d_from: str, d_to: str, kind=None, assignee=None) -> dict:
    """Admin / C-suite: the team's assigned work. Team items only — never user_calendar_event."""
    import team_items
    items = team_items.list_items(conn, status="all", date_from=d_from, date_to=d_to, me=uid, can_edit=True,
                                  mine_uid=assignee, kind=kind if kind in team_items.KINDS else None)
    items = [i for i in items if i["assignee_user_id"] or i["attendee_ids"] or i.get("guest_emails")]   # assigned to someone
    return {"range": {"from": d_from, "to": d_to}, "items": items, "people": team_items.people(conn)}


@requires_role(*TEAM_ROLES)
def _api_schedule():
    u = current_user() or {}
    uid, role = u.get("id"), u.get("role")
    scope = (request.args.get("scope") or "mine").strip().lower()
    if scope not in ("mine", "all"):
        return jsonify({"error": "scope is mine or all"}), 400
    if scope == "all" and role not in ALL_ROLES:
        log.info("inbox schedule: scope=all refused uid=%s role=%s", uid, role)
        return jsonify({"error": "forbidden"}), 403
    try:
        d_from, d_to = _window(request.args)
    except ValueError as e:
        return jsonify({"error": str(e) if "pick" in str(e) or "before" in str(e) else "not a valid date"}), 400
    kind = (request.args.get("type") or "").strip().lower() or None
    if kind not in (None, "task", "event", "meeting") and not (scope == "mine" and kind == "calendar"):
        return jsonify({"error": "type is task, event or meeting"}), 400
    assignee = None
    if scope == "all" and (request.args.get("assignee") or "").strip():
        try:
            assignee = int(request.args["assignee"])
        except ValueError:
            return jsonify({"error": "pick someone from the team"}), 400
    conn = db_layer.connect()
    try:
        data = mine(conn, uid, d_from, d_to, kind, role in ALL_ROLES) if scope == "mine" else everyone(conn, uid, d_from, d_to, kind, assignee)
    finally:
        conn.close()
    return jsonify({"data": data})


@requires_role(*TEAM_ROLES)
def _page():
    if (current_user() or {}).get("role") in ALL_ROLES:
        return redirect("/company#inbox/mine")
    resp = send_file(str(PAGE), mimetype="text/html", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def register(app) -> None:
    app.add_url_rule("/api/inbox/schedule", "inbox_schedule", _api_schedule, methods=["GET"])
    app.add_url_rule("/inbox", "inbox_page", _page, methods=["GET"])
