"""Team tasks & events — the to-do list on the dashboard, synced onto the team calendar.

Any kind of to-do: call someone, a job walkthrough, send an invoice, submit a proposal.
The creator picks TASK (something to get done, optionally by a deadline), EVENT
(something happening on a day, optionally at a time) or MEETING (an event with people in it,
2026-10-16; 2026-10-18: guests by email, Google Meet video and real Google Calendar invitations,
see meeting_invites.py). Each can be assigned to a person, or to nobody yet, and edited later. A meeting
also lists the other people in it (crm_task_attendee); it is "theirs" too (their Inbox, their
Superstars Google calendar).

Storage: the existing crm_task table (the CRM follow-ups already live there, so a client
follow-up is a team task too), extended additively (idempotent, catalog-checked, no CHECK
constraints):
  kind TEXT          'task' | 'event' | 'meeting'   (NULL = an older follow-up = task)
  project_code TEXT  optional link to a job
  due_time TEXT      task deadline time, HH:MM (only with a due_date)
  start_date, start_time, end_date, end_time   an event's when (end optional)
  location TEXT      an event's place name ("Client office")
  address TEXT       a street address (task or event) — shown with a Google Maps directions
                     link, so a phone opens turn-by-turn directions in one tap
  updated_at, updated_by
A task's calendar day is its due_date; an event or meeting spans start_date..end_date. An event also
keeps due_date = start_date so the older follow-up lists sort it correctly.

Who:
  * read (list, people)        admin, c_suite, pm, super — the whole team sees the list
  * create / edit / delete     admin, c_suite
  * mark done / reopen         admin, c_suite, or the person it is assigned to
Titles and details can name people, so they never go to the log (ids only).

  GET    /api/team/people                 names + roles of the people a task can go to
  GET    /api/team/items                  ?status=open|done|all  &mine=1  &from=&to=
  POST   /api/team/items
  PUT    /api/team/items/<id>
  POST   /api/team/items/<id>/done        {"done": true|false}
  DELETE /api/team/items/<id>             soft: status 'cancelled', kept for the record
  GET    /api/team/calendar?from=&to=     the TEAM calendar for every team role (#2b): every dated job
                                          (name, dates, colour, whether this person can open it, whether
                                          they are on it) + every task / event in the window. Nothing else:
                                          no clients, money, crew, on-site counts or reports
  GET    /api/team/address-suggest?q=     address suggestions while typing: the jobs' own addresses
                                          plus NYC GeoSearch (NYC Planning, free, no key); labels only
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Optional
from urllib.parse import quote

from flask import jsonify, request

import db_layer
from auth import current_user, requires_company, requires_role

log = logging.getLogger("ssc.team_items")

TEAM_ROLES = ("admin", "c_suite", "pm", "super")
EDIT_ROLES = ("admin", "c_suite")
KINDS = ("task", "event", "meeting")
DATED = ("event", "meeting")   # kinds with a start day (and optional times) instead of a deadline
MAX_ATTENDEES = 30
STATUSES = ("open", "done", "cancelled")
MAX_TITLE, MAX_DETAIL, MAX_LOCATION, MAX_ADDRESS = 200, 2000, 200, 300
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
COLUMNS = (("kind", "TEXT"), ("project_code", "TEXT"), ("due_time", "TEXT"), ("start_date", "TEXT"),
           ("start_time", "TEXT"), ("end_date", "TEXT"), ("end_time", "TEXT"), ("location", "TEXT"), ("address", "TEXT"),
           ("updated_at", "TEXT"), ("updated_by", "INTEGER"),
           # meetings (2026-10-18, meeting_invites.py): outside guests (JSON list of emails), Google Meet
           # wanted, whose Google Calendar sends the invitations, and whether this write emails the guests
           ("guest_emails", "TEXT"), ("video", "INTEGER"), ("invite_owner_id", "INTEGER"), ("invite_send", "TEXT"))
MAX_GUESTS = 50
_EMAIL = re.compile(r"^[^@\s<>(),;:\"]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


# ------------------------------------------------------------------ schema
def _has_table(conn, table) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name = ?",
                                 (table,)).fetchone())
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone())


def _columns(conn, table) -> set:
    if db_layer.is_postgres():
        return {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
                                           "AND table_name = ?", (table,)).fetchall()}
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure(conn) -> None:
    """Idempotent: add the task/event columns to crm_task when missing."""
    if not _has_table(conn, "crm_task"):
        return   # a fixture that creates the CRM tables later
    have = _columns(conn, "crm_task")
    for name, typ in COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE crm_task ADD COLUMN {name} {typ}")
    conn.execute("CREATE TABLE IF NOT EXISTS crm_task_attendee (task_id INTEGER NOT NULL, user_id INTEGER NOT NULL)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_crm_task_attendee ON crm_task_attendee(task_id, user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crm_task_attendee_user ON crm_task_attendee(user_id)")
    # the Google Calendar event that carries a meeting's invitations (meeting_invites.py)
    conn.execute("CREATE TABLE IF NOT EXISTS meeting_invite (item_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, "
                 "google_event_id TEXT, calendar_id TEXT, body_hash TEXT, meet_url TEXT, meet_state TEXT, guest_status TEXT, "
                 "last_error TEXT, synced_at TEXT, answers_at TEXT)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_meeting_invite_item ON meeting_invite(item_id)")
    conn.commit()


# ------------------------------------------------------------------ helpers
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")   # LOCAL, never UTC


def _rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]


def _date(v, label) -> Optional[str]:
    s = str(v or "").strip()[:10]
    if not s:
        return None
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise ValueError(f"the {label} is not a valid date")


def _time(v, label) -> Optional[str]:
    s = str(v or "").strip()[:5]
    if not s:
        return None
    if not _TIME.match(s):
        raise ValueError(f"the {label} is not a valid time")
    return s


def _text(v, label, limit, required=False) -> Optional[str]:
    s = str(v or "").strip()
    if required and not s:
        raise ValueError(f"the {label} can't be blank")
    if len(s) > limit:
        raise ValueError(f"the {label} is too long (max {limit} characters)")
    return s or None


def people(conn) -> list:
    """Active team members a task can be assigned to: names and roles only."""
    marks = ",".join("?" * len(TEAM_ROLES))
    rows = _rows(conn.execute(
        "SELECT id, COALESCE(NULLIF(display_name, ''), NULLIF(full_name, ''), email) AS name, role FROM users "
        f"WHERE role IN ({marks}) AND COALESCE(is_active, 1) = 1 AND COALESCE(status, 'active') = 'active' "
        "AND COALESCE(is_system, 0) = 0 ORDER BY name", TEAM_ROLES))
    names = [r["name"] for r in rows]
    out = []
    for r in rows:
        p = {"id": r["id"], "name": r["name"], "role": r["role"]}
        if names.count(r["name"]) > 1:   # two people with one name: show whose address it is
            p["hint"] = _email_of(conn, r["id"]).split("@")[0] + "@"
        out.append(p)
    return out


def _email_of(conn, uid) -> str:
    r = conn.execute("SELECT email FROM users WHERE id = ?", (uid,)).fetchone()
    return (r["email"] if r else "") or ""


def _assignable(conn, uid) -> bool:
    return any(p["id"] == uid for p in people(conn))


def _validate(conn, p: dict, cur: Optional[dict] = None) -> dict:
    """The full set of stored fields for a create (cur None) or an edit (cur = the row),
    checked together so a refused edit changes nothing."""
    cur = cur or {}
    get = lambda k: p[k] if k in p else cur.get(k)   # noqa: E731
    kind = str(get("kind") or "task").strip().lower()
    if kind not in KINDS:
        raise ValueError("pick task, event or meeting")
    out = {"kind": kind,
           "title": _text(get("title"), "title", MAX_TITLE, required=True),
           "detail": _text(get("detail"), "details", MAX_DETAIL)}
    uid = get("assignee_user_id")
    if uid in (None, "", 0, "0"):
        out["assignee_user_id"] = None
    else:
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            raise ValueError("pick someone from the team")
        if uid != cur.get("assignee_user_id") and not _assignable(conn, uid):
            raise ValueError("that person can't be assigned (not an active team member)")
        out["assignee_user_id"] = uid
    code = str(get("project_code") or "").strip() or None
    if code and code != cur.get("project_code") and not conn.execute(
            "SELECT 1 FROM projects WHERE project_code = ?", (code,)).fetchone():
        raise LookupError("project not found")
    out["project_code"] = code
    out["address"] = _text(get("address"), "address", MAX_ADDRESS)
    if kind == "task":
        due = _date(get("due_date"), "deadline")
        due_time = _time(get("due_time"), "deadline time")
        if due_time and not due:
            raise ValueError("a deadline time needs a deadline date")
        out.update(due_date=due, due_time=due_time, start_date=None, start_time=None, end_date=None,
                   end_time=None, location=None)
    else:
        # a task turned into an event keeps its deadline as the event day unless a date is given
        start = _date(p["start_date"] if "start_date" in p else (cur.get("start_date") or cur.get("due_date")), "date")
        if not start:
            raise ValueError("an event needs a date")
        st, en_d, et = _time(get("start_time"), "start time"), _date(get("end_date"), "end date"), _time(get("end_time"), "end time")
        if en_d and en_d < start:
            raise ValueError("the event ends before it starts")
        if en_d == start:
            en_d = None
        if et and not st:
            raise ValueError("an end time needs a start time")
        if et and st and not en_d and et <= st:
            raise ValueError("the end time is before the start time")
        out.update(start_date=start, start_time=st, end_date=en_d, end_time=et, due_date=start, due_time=None,
                   location=_text(get("location"), "location", MAX_LOCATION))
    out["attendees"] = _attendees(conn, p.get("attendee_user_ids") if "attendee_user_ids" in p else cur.get("attendee_ids"),
                                  kind, out["assignee_user_id"], set(cur.get("attendee_ids") or ()))
    if kind == "meeting":
        out["guest_emails"] = _guests(p["guest_emails"] if "guest_emails" in p else _json_list(cur.get("guest_emails")))
        out["video"] = 1 if (p["video"] if "video" in p else cur.get("video")) in (True, 1, "1", "true") else 0
    else:
        out["guest_emails"], out["video"] = [], 0
    out["invite_send"] = "none" if p.get("send_invites") in (False, 0, "0", "false") else "all"
    return out


def _json_list(v) -> list:
    import json
    try:
        x = json.loads(v) if isinstance(v, str) and v else (v or [])
    except ValueError:
        return []
    return x if isinstance(x, list) else []


def _guests(raw) -> list:
    """Outside guests of a meeting: email addresses, checked, lower-cased, no repeats."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("list the guests' email addresses")
    out = []
    for v in raw:
        e = str(v or "").strip().lower()
        if not e:
            continue
        if len(e) > 254 or not _EMAIL.match(e):
            raise ValueError("a guest's email address isn't valid")
        if e not in out:
            out.append(e)
    if len(out) > MAX_GUESTS:
        raise ValueError(f"a meeting can have up to {MAX_GUESTS} guests by email")
    return out


def _attendees(conn, raw, kind, assignee, already: set) -> list:
    """The other people in a meeting: active team members, without the assignee, no repeats.
    Only meetings have them; someone already listed may stay even if they left the team."""
    if kind != "meeting" or not raw:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("pick the people in the meeting from the team")
    out = []
    for v in raw:
        try:
            uid = int(v)
        except (TypeError, ValueError):
            raise ValueError("pick the people in the meeting from the team")
        if uid == assignee or uid in out:
            continue
        if uid not in already and not _assignable(conn, uid):
            raise ValueError("someone in the meeting isn't an active team member")
        out.append(uid)
    if len(out) > MAX_ATTENDEES:
        raise ValueError(f"a meeting can list up to {MAX_ATTENDEES} people")
    return out


def attendee_ids(conn, item_id) -> list:
    return [r[0] for r in conn.execute("SELECT user_id FROM crm_task_attendee WHERE task_id = ? ORDER BY user_id",
                                       (item_id,)).fetchall()]


def _set_attendees(conn, item_id, uids) -> None:
    conn.execute("DELETE FROM crm_task_attendee WHERE task_id = ?", (item_id,))
    for uid in uids:
        conn.execute("INSERT INTO crm_task_attendee (task_id, user_id) VALUES (?, ?)", (item_id, uid))


def directions_url(address) -> Optional[str]:
    """Google Maps directions to the address (the Maps app on a phone; no API key needed)."""
    a = str(address or "").strip()
    return "https://www.google.com/maps/dir/?api=1&destination=" + quote(a, safe="") if a else None


GEO_AUTOCOMPLETE = "https://geosearch.planninglabs.nyc/v2/autocomplete"
_UA = "SuperstarsOS/1.0 (+facade restoration dashboard)"
_SUGGEST_CACHE: dict = {}


def _geo_fetch(q: str) -> list:
    """NYC GeoSearch labels for a partial address. Tests swap this out (no network in CI)."""
    import requests
    r = requests.get(GEO_AUTOCOMPLETE, params={"text": q, "size": 6}, timeout=4,
                     headers={"User-Agent": _UA, "Accept": "application/json"})
    r.raise_for_status()
    return [((f.get("properties") or {}).get("label") or "") for f in (r.json().get("features") or [])]


def _tidy(label: str) -> str:
    """'890 EAST 135 STREET, Bronx, NY, USA' -> '890 East 135 Street, Bronx, NY'."""
    parts = [x.strip() for x in label.split(",") if x.strip()]
    if parts and parts[-1].upper() == "USA":
        parts = parts[:-1]
    if parts and parts[0].isupper():
        parts[0] = " ".join(w if any(ch.isdigit() for ch in w) else w.capitalize() for w in parts[0].split())
    return ", ".join(parts)


def suggest(conn, q: str, fetch=None) -> list:
    """Up to 8 address suggestions: the jobs' own addresses first, then NYC addresses.
    The outside lookup is best-effort — when it is slow or down, the jobs still come back."""
    q = re.sub(r"\s+", " ", (q or "").strip())[:120]
    if len(q) < 3:
        return []
    out, seen = [], set()

    def add(label, source, job=None):
        k = label.lower()
        if label and k not in seen:
            seen.add(k)
            out.append({"label": label, "source": source, "job": job})
    for r in _rows(conn.execute(
            "SELECT name, address, city_zip FROM projects WHERE LOWER(address) LIKE ? AND COALESCE(status, 'active') <> 'closed' "
            "ORDER BY name LIMIT 5", ("%" + q.lower() + "%",))):
        add(", ".join(x for x in (r.get("address"), r.get("city_zip")) if x), "job", r.get("name"))
    key = q.lower()
    labels = _SUGGEST_CACHE.get(key)
    if labels is None:
        try:
            labels = [_tidy(x) for x in (fetch or _geo_fetch)(q) if x]
            if len(_SUGGEST_CACHE) > 500:
                _SUGGEST_CACHE.clear()
            _SUGGEST_CACHE[key] = labels
        except Exception as e:   # never block typing on the outside lookup
            log.info("address suggest lookup skipped: %s", type(e).__name__)
            labels = []
    for lab in labels:
        add(lab, "nyc")
    return out[:8]


# ------------------------------------------------------------------ reads
def _shape(r: dict, today: str, me: Optional[int], can_edit: bool, people_in=None) -> dict:
    kind = r.get("kind") or "task"
    status = r.get("status") or "open"
    people_in = people_in or []
    if kind in DATED:
        first, last = r.get("start_date") or r.get("due_date"), r.get("end_date") or r.get("start_date") or r.get("due_date")
    else:
        first = last = r.get("due_date")
    return {
        "id": r["id"], "kind": kind, "title": r.get("title"), "detail": r.get("detail"), "status": status,
        "assignee_user_id": r.get("assignee_user_id"), "assignee_name": r.get("assignee_name"),
        "created_by_name": r.get("creator_name"), "project_code": r.get("project_code"), "project_name": r.get("project_name"),
        "entity_type": r.get("entity_type"), "entity_id": r.get("entity_id"), "org_name": r.get("org_name"),
        "due_date": r.get("due_date") if kind == "task" else None, "due_time": r.get("due_time"),
        "start_date": first if kind in DATED else None, "start_time": r.get("start_time"),
        "end_date": r.get("end_date"), "end_time": r.get("end_time"), "location": r.get("location"),
        "address": r.get("address"), "directions_url": directions_url(r.get("address")),
        "calendar_from": first, "calendar_to": last,
        "overdue": bool(kind == "task" and status == "open" and first and first < today),
        "today": bool(first and first <= today <= (last or first)),
        "created_at": r.get("created_at"), "completed_at": r.get("completed_at"),
        "attendee_ids": [p[0] for p in people_in], "attendee_names": [p[1] for p in people_in],
        "guest_emails": _json_list(r.get("guest_emails")) if kind == "meeting" else [],
        "video": bool(r.get("video")) and kind == "meeting", "meet_url": r.get("meet_url") if kind == "meeting" else None,
        "invites": bool(r.get("invite_owner_id")) and kind == "meeting", "guest_answers": _answers(r) if kind == "meeting" else {},
        "invite_problem": r.get("invite_error") if kind == "meeting" else None,
        "mine": bool(me and (r.get("assignee_user_id") == me or any(p[0] == me for p in people_in))),
        "can_edit": can_edit, "can_complete": bool(can_edit or (me and r.get("assignee_user_id") == me)),
        "source": r.get("source"),   # 'gcal' = came from the person's Google Calendar (calendar_import.py)
    }


def _answers(r) -> dict:
    """email -> Google's answer ('accepted' | 'declined' | 'tentative' | 'needsAction'), from meeting_invites."""
    import json
    try:
        x = json.loads(r.get("guest_status") or "{}")
    except ValueError:
        return {}
    return x if isinstance(x, dict) else {}


def list_items(conn, *, status="open", mine_uid=None, date_from=None, date_to=None, me=None, can_edit=False,
               project_code=None, kind=None) -> list:
    """mine_uid = items that person is on: assigned to them, or a meeting they are in."""
    where, params = [], []
    if kind in KINDS:
        where.append("COALESCE(t.kind, 'task') = ?")
        params.append(kind)
    if project_code:
        where.append("t.project_code = ?")
        params.append(project_code)
    if status in ("open", "done", "cancelled"):
        where.append("COALESCE(t.status, 'open') = ?")
        params.append(status)
    else:
        where.append("COALESCE(t.status, 'open') <> 'cancelled'")
    if mine_uid:
        where.append("(t.assignee_user_id = ? OR EXISTS (SELECT 1 FROM crm_task_attendee a WHERE a.task_id = t.id AND a.user_id = ?))")
        params += [mine_uid, mine_uid]
    if date_from and date_to:
        # on the calendar in [from, to]: a task by its deadline, an event by its span
        where.append("((COALESCE(t.kind, 'task') = 'task' AND t.due_date >= ? AND t.due_date <= ?) OR "
                     "(t.kind IN ('event', 'meeting') AND COALESCE(t.start_date, t.due_date) <= ? AND "
                     "COALESCE(t.end_date, t.start_date, t.due_date) >= ?))")
        params += [date_from, date_to, date_to, date_from]
    rows = _rows(conn.execute(
        "SELECT t.*, COALESCE(NULLIF(u.display_name, ''), NULLIF(u.full_name, ''), u.email) AS assignee_name, "
        "       COALESCE(NULLIF(c.display_name, ''), NULLIF(c.full_name, '')) AS creator_name, "
        "       p.name AS project_name, o.name AS org_name, mi.meet_url, mi.guest_status, mi.last_error AS invite_error "
        "FROM crm_task t LEFT JOIN users u ON u.id = t.assignee_user_id LEFT JOIN users c ON c.id = t.created_by "
        "LEFT JOIN meeting_invite mi ON mi.item_id = t.id "
        "LEFT JOIN projects p ON p.project_code = t.project_code "
        "LEFT JOIN crm_organization o ON t.entity_type = 'organization' AND o.id = t.entity_id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY (COALESCE(t.start_date, t.due_date) IS NULL), COALESCE(t.start_date, t.due_date), "
        "COALESCE(t.start_time, t.due_time, '99:99'), t.id DESC", params))
    today = date.today().isoformat()
    people_in = _people_in(conn, [r["id"] for r in rows if r.get("kind") == "meeting"])
    return [_shape(r, today, me, can_edit, people_in.get(r["id"])) for r in rows]


def _people_in(conn, ids) -> dict:
    """task id -> [(user id, name)] for these meetings."""
    out = {}
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        for r in conn.execute(
                "SELECT a.task_id, a.user_id, COALESCE(NULLIF(u.display_name, ''), NULLIF(u.full_name, ''), u.email) AS name "
                f"FROM crm_task_attendee a LEFT JOIN users u ON u.id = a.user_id WHERE a.task_id IN ({','.join('?' * len(chunk))}) "
                "ORDER BY name", chunk).fetchall():
            out.setdefault(r["task_id"], []).append((r["user_id"], r["name"]))
    return out


def get_item(conn, item_id) -> Optional[dict]:
    r = conn.execute("SELECT * FROM crm_task WHERE id = ?", (item_id,)).fetchone()
    if not r:
        return None
    return dict(r, attendee_ids=attendee_ids(conn, item_id) if r["kind"] == "meeting" else [])


def team_calendar(conn, start: date, end: date, *, role=None, uid=None) -> dict:
    """The team week / month for anyone on the team. Built on the company calendar's job
    window, then cut down to what the whole team may see."""
    import pm_scoping
    import portal_calendar
    w = portal_calendar.window(conn, start, end)
    mine_codes = pm_scoping.assigned_codes(uid, conn) if uid else set()
    jobs = []
    for j in w["jobs"]:
        jobs.append({
            "project_code": j["project_code"], "name": j["name"], "start_date": j["start_date"], "end_date": j["end_date"],
            "open_ended": j["open_ended"], "color": j["color"], "status": j["status"], "today_on": j["today_on"],
            "can_open": pm_scoping.pm_can_access_project(role, uid, j["project_code"], conn),
            "mine": j["project_code"] in mine_codes,
        })
    items = list_items(conn, status="all", date_from=w["range"]["from"], date_to=w["range"]["to"], me=uid,
                       can_edit=role in EDIT_ROLES)
    return {"range": w["range"], "jobs": jobs, "items": items}


# ------------------------------------------------------------------ writes
def _sync_google(*user_ids) -> None:
    """Push the change to the assignees' Google Calendars (gcal_sync). Best effort: a Google
    problem never fails the app write."""
    try:
        import gcal_sync
        gcal_sync.schedule(*user_ids)
    except Exception as e:
        log.info("google calendar sync not scheduled: %s", type(e).__name__)


def create(conn, p: dict, *, actor=None) -> int:
    v = _validate(conn, p)
    now = _now()
    conn.execute(
        "INSERT INTO crm_task (entity_type, entity_id, title, detail, due_date, assignee_user_id, status, priority, "
        "created_by, created_at, kind, project_code, due_time, start_date, start_time, end_date, end_time, location, "
        "address, updated_at, updated_by, guest_emails, video, invite_owner_id, invite_send) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("project" if v["project_code"] else "none", None, v["title"], v["detail"], v["due_date"], v["assignee_user_id"],
         "open", "normal", actor, now, v["kind"], v["project_code"], v["due_time"], v["start_date"], v["start_time"],
         v["end_date"], v["end_time"], v["location"], v["address"], now, actor, _dump(v["guest_emails"]), v["video"],
         _organizer(conn, v["kind"], actor), v["invite_send"]))
    conn.commit()
    new_id = conn.execute("SELECT MAX(id) AS m FROM crm_task").fetchone()["m"]
    if v["attendees"]:
        _set_attendees(conn, new_id, v["attendees"])
        conn.commit()
    log.info("team item created id=%s kind=%s by user=%s", new_id, v["kind"], actor)
    _sync_google(v["assignee_user_id"], actor if v["kind"] == "meeting" else None, *v["attendees"])
    return new_id


def _dump(v) -> Optional[str]:
    import json
    return json.dumps(v) if v else None


def _organizer(conn, kind, actor) -> Optional[int]:
    """Whose Google Calendar sends a meeting's invitations: the person saving it, when their Google
    Calendar is connected and working. None = no invitations (the meeting still shows for the team)."""
    if kind != "meeting" or not actor:
        return None
    try:
        import meeting_invites
        return actor if meeting_invites.can_organize(conn, actor) else None
    except Exception:
        return None


def update(conn, item_id, p: dict, *, actor=None) -> None:
    cur = get_item(conn, item_id)
    if not cur or (cur.get("status") or "open") == "cancelled":
        raise LookupError("not found")
    if not (cur.get("kind")):
        cur["kind"] = "task"
    v = _validate(conn, p, cur)
    cols = ["kind", "title", "detail", "assignee_user_id", "project_code", "due_date", "due_time", "start_date",
            "start_time", "end_date", "end_time", "location", "address", "video", "invite_send"]
    owner = cur.get("invite_owner_id") or _organizer(conn, v["kind"], actor)   # the first organizer keeps the Google event
    conn.execute(f"UPDATE crm_task SET {', '.join(c + ' = ?' for c in cols)}, guest_emails = ?, invite_owner_id = ?, "
                 "updated_at = ?, updated_by = ? WHERE id = ?",
                 (*[v[c] for c in cols], _dump(v["guest_emails"]), owner, _now(), actor, item_id))
    _set_attendees(conn, item_id, v["attendees"])
    conn.commit()
    log.info("team item updated id=%s by user=%s", item_id, actor)
    _sync_google(cur.get("assignee_user_id"), v["assignee_user_id"], owner, *cur["attendee_ids"], *v["attendees"])   # old and new people


def set_done(conn, item_id, done: bool, *, actor=None, actor_role=None) -> dict:
    cur = get_item(conn, item_id)
    if not cur or (cur.get("status") or "open") == "cancelled":
        raise LookupError("not found")
    if actor_role not in EDIT_ROLES and cur.get("assignee_user_id") != actor:
        raise PermissionError("only the person it is assigned to, or admin / C-suite, can do that")
    conn.execute("UPDATE crm_task SET status = ?, completed_at = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                 ("done" if done else "open", _now() if done else None, _now(), actor, item_id))
    conn.commit()
    log.info("team item %s id=%s by user=%s", "done" if done else "reopened", item_id, actor)
    _sync_google(cur.get("assignee_user_id"), *cur["attendee_ids"])
    return {"id": item_id, "status": "done" if done else "open"}


def cancel(conn, item_id, *, actor=None) -> None:
    cur = get_item(conn, item_id)
    if not cur or (cur.get("status") or "open") == "cancelled":
        raise LookupError("not found")
    conn.execute("UPDATE crm_task SET status = 'cancelled', updated_at = ?, updated_by = ? WHERE id = ?", (_now(), actor, item_id))
    conn.commit()
    log.info("team item deleted id=%s by user=%s", item_id, actor)
    _sync_google(cur.get("assignee_user_id"), cur.get("invite_owner_id"), *cur["attendee_ids"])


# ------------------------------------------------------------------ routes
def _me():
    u = current_user() or {}
    return u.get("id"), u.get("role")


def _with_conn(fn):
    conn = db_layer.connect()
    try:
        return fn(conn)
    finally:
        conn.close()


@requires_role(*TEAM_ROLES)
def _api_people():
    return jsonify({"data": _with_conn(people)})


@requires_role(*TEAM_ROLES)
def _api_calendar():
    uid, role = _me()
    try:
        d_from, d_to = _date(request.args.get("from"), "from date"), _date(request.args.get("to"), "to date")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not d_from or not d_to:
        return jsonify({"error": "give both from and to"}), 400
    try:
        data = _with_conn(lambda c: team_calendar(c, date.fromisoformat(d_from), date.fromisoformat(d_to), role=role, uid=uid))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"data": data})


@requires_role(*TEAM_ROLES)
def _api_suggest():
    return jsonify({"data": _with_conn(lambda c: suggest(c, request.args.get("q", "")))})


@requires_role(*TEAM_ROLES)
def _api_list():
    uid, role = _me()
    a = request.args
    try:
        d_from, d_to = _date(a.get("from"), "from date"), _date(a.get("to"), "to date")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if bool(d_from) != bool(d_to) or (d_from and d_to < d_from):
        return jsonify({"error": "give both from and to, in order"}), 400
    status = (a.get("status") or "open").lower()
    rows = _with_conn(lambda c: list_items(c, status=status, mine_uid=uid if a.get("mine") in ("1", "true") else None,
                                           date_from=d_from, date_to=d_to, me=uid, can_edit=role in EDIT_ROLES,
                                           project_code=(a.get("project") or "").strip() or None))
    return jsonify({"data": rows, "meta": {"count": len(rows), "can_edit": role in EDIT_ROLES}})


def _write(fn):
    try:
        return fn()
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@requires_company
def _api_create():
    uid, _ = _me()
    body = request.get_json(silent=True) or {}
    return _write(lambda: (jsonify({"data": {"id": _with_conn(lambda c: create(c, body, actor=uid))}}), 201))


@requires_company
def _api_update(item_id):
    uid, _ = _me()
    body = request.get_json(silent=True) or {}
    return _write(lambda: (_with_conn(lambda c: update(c, item_id, body, actor=uid)), jsonify({"data": {"id": item_id}}))[1])


@requires_role(*TEAM_ROLES)
def _api_done(item_id):
    uid, role = _me()
    body = request.get_json(silent=True) or {}
    done = body.get("done", True) not in (False, "false", 0, "0")
    return _write(lambda: jsonify({"data": _with_conn(lambda c: set_done(c, item_id, done, actor=uid, actor_role=role))}))


@requires_company
def _api_delete(item_id):
    uid, _ = _me()
    return _write(lambda: (_with_conn(lambda c: cancel(c, item_id, actor=uid)), jsonify({"data": {"id": item_id, "status": "cancelled"}}))[1])


def register(app) -> None:
    conn = db_layer.connect()
    try:
        ensure(conn)
    except Exception as e:   # a fresh preprod DB may migrate later; never block boot
        log.warning("team items schema ensure skipped: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    app.add_url_rule("/api/team/people", "team_people", _api_people, methods=["GET"])
    app.add_url_rule("/api/team/calendar", "team_calendar", _api_calendar, methods=["GET"])
    app.add_url_rule("/api/team/address-suggest", "team_address_suggest", _api_suggest, methods=["GET"])
    app.add_url_rule("/api/team/items", "team_items_list", _api_list, methods=["GET"])
    app.add_url_rule("/api/team/items", "team_items_create", _api_create, methods=["POST"])
    app.add_url_rule("/api/team/items/<int:item_id>", "team_items_update", _api_update, methods=["PUT"])
    app.add_url_rule("/api/team/items/<int:item_id>/done", "team_items_done", _api_done, methods=["POST"])
    app.add_url_rule("/api/team/items/<int:item_id>", "team_items_delete", _api_delete, methods=["DELETE"])
