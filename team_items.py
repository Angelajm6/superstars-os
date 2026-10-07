"""Team tasks & events — the to-do list on the dashboard, synced onto the team calendar.

Any kind of to-do: call someone, a job walkthrough, send an invoice, submit a proposal.
The creator picks TASK (something to get done, optionally by a deadline) or EVENT
(something happening on a day, optionally at a time). Both can be assigned to a person,
or to nobody yet, and both can be edited later.

Storage: the existing crm_task table (the CRM follow-ups already live there, so a client
follow-up is a team task too), extended additively (idempotent, catalog-checked, no CHECK
constraints):
  kind TEXT          'task' | 'event'          (NULL = an older follow-up = task)
  project_code TEXT  optional link to a job
  due_time TEXT      task deadline time, HH:MM (only with a due_date)
  start_date, start_time, end_date, end_time   an event's when (end optional)
  location TEXT      an event's place name ("Client office")
  address TEXT       a street address (task or event) — shown with a Google Maps directions
                     link, so a phone opens turn-by-turn directions in one tap
  updated_at, updated_by
A task's calendar day is its due_date; an event spans start_date..end_date. An event also
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
KINDS = ("task", "event")
STATUSES = ("open", "done", "cancelled")
MAX_TITLE, MAX_DETAIL, MAX_LOCATION, MAX_ADDRESS = 200, 2000, 200, 300
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
COLUMNS = (("kind", "TEXT"), ("project_code", "TEXT"), ("due_time", "TEXT"), ("start_date", "TEXT"),
           ("start_time", "TEXT"), ("end_date", "TEXT"), ("end_time", "TEXT"), ("location", "TEXT"), ("address", "TEXT"),
           ("updated_at", "TEXT"), ("updated_by", "INTEGER"))


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
    return [{"id": r["id"], "name": r["name"], "role": r["role"]} for r in rows]


def _assignable(conn, uid) -> bool:
    return any(p["id"] == uid for p in people(conn))


def _validate(conn, p: dict, cur: Optional[dict] = None) -> dict:
    """The full set of stored fields for a create (cur None) or an edit (cur = the row),
    checked together so a refused edit changes nothing."""
    cur = cur or {}
    get = lambda k: p[k] if k in p else cur.get(k)   # noqa: E731
    kind = str(get("kind") or "task").strip().lower()
    if kind not in KINDS:
        raise ValueError("pick task or event")
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
    return out


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
def _shape(r: dict, today: str, me: Optional[int], can_edit: bool) -> dict:
    kind = r.get("kind") or "task"
    status = r.get("status") or "open"
    if kind == "event":
        first, last = r.get("start_date") or r.get("due_date"), r.get("end_date") or r.get("start_date") or r.get("due_date")
    else:
        first = last = r.get("due_date")
    return {
        "id": r["id"], "kind": kind, "title": r.get("title"), "detail": r.get("detail"), "status": status,
        "assignee_user_id": r.get("assignee_user_id"), "assignee_name": r.get("assignee_name"),
        "created_by_name": r.get("creator_name"), "project_code": r.get("project_code"), "project_name": r.get("project_name"),
        "entity_type": r.get("entity_type"), "entity_id": r.get("entity_id"), "org_name": r.get("org_name"),
        "due_date": r.get("due_date") if kind == "task" else None, "due_time": r.get("due_time"),
        "start_date": first if kind == "event" else None, "start_time": r.get("start_time"),
        "end_date": r.get("end_date"), "end_time": r.get("end_time"), "location": r.get("location"),
        "address": r.get("address"), "directions_url": directions_url(r.get("address")),
        "calendar_from": first, "calendar_to": last,
        "overdue": bool(kind == "task" and status == "open" and first and first < today),
        "today": bool(first and first <= today <= (last or first)),
        "created_at": r.get("created_at"), "completed_at": r.get("completed_at"),
        "mine": bool(me and r.get("assignee_user_id") == me),
        "can_edit": can_edit, "can_complete": bool(can_edit or (me and r.get("assignee_user_id") == me)),
    }


def list_items(conn, *, status="open", mine_uid=None, date_from=None, date_to=None, me=None, can_edit=False) -> list:
    where, params = [], []
    if status in ("open", "done", "cancelled"):
        where.append("COALESCE(t.status, 'open') = ?")
        params.append(status)
    else:
        where.append("COALESCE(t.status, 'open') <> 'cancelled'")
    if mine_uid:
        where.append("t.assignee_user_id = ?")
        params.append(mine_uid)
    if date_from and date_to:
        # on the calendar in [from, to]: a task by its deadline, an event by its span
        where.append("((COALESCE(t.kind, 'task') = 'task' AND t.due_date >= ? AND t.due_date <= ?) OR "
                     "(t.kind = 'event' AND COALESCE(t.start_date, t.due_date) <= ? AND "
                     "COALESCE(t.end_date, t.start_date, t.due_date) >= ?))")
        params += [date_from, date_to, date_to, date_from]
    rows = _rows(conn.execute(
        "SELECT t.*, COALESCE(NULLIF(u.display_name, ''), NULLIF(u.full_name, ''), u.email) AS assignee_name, "
        "       COALESCE(NULLIF(c.display_name, ''), NULLIF(c.full_name, '')) AS creator_name, "
        "       p.name AS project_name, o.name AS org_name "
        "FROM crm_task t LEFT JOIN users u ON u.id = t.assignee_user_id LEFT JOIN users c ON c.id = t.created_by "
        "LEFT JOIN projects p ON p.project_code = t.project_code "
        "LEFT JOIN crm_organization o ON t.entity_type = 'organization' AND o.id = t.entity_id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY (COALESCE(t.start_date, t.due_date) IS NULL), COALESCE(t.start_date, t.due_date), "
        "COALESCE(t.start_time, t.due_time, '99:99'), t.id DESC", params))
    today = date.today().isoformat()
    return [_shape(r, today, me, can_edit) for r in rows]


def get_item(conn, item_id) -> Optional[dict]:
    r = conn.execute("SELECT * FROM crm_task WHERE id = ?", (item_id,)).fetchone()
    return dict(r) if r else None


# ------------------------------------------------------------------ writes
def create(conn, p: dict, *, actor=None) -> int:
    v = _validate(conn, p)
    now = _now()
    conn.execute(
        "INSERT INTO crm_task (entity_type, entity_id, title, detail, due_date, assignee_user_id, status, priority, "
        "created_by, created_at, kind, project_code, due_time, start_date, start_time, end_date, end_time, location, "
        "address, updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("project" if v["project_code"] else "none", None, v["title"], v["detail"], v["due_date"], v["assignee_user_id"],
         "open", "normal", actor, now, v["kind"], v["project_code"], v["due_time"], v["start_date"], v["start_time"],
         v["end_date"], v["end_time"], v["location"], v["address"], now, actor))
    conn.commit()
    new_id = conn.execute("SELECT MAX(id) AS m FROM crm_task").fetchone()["m"]
    log.info("team item created id=%s kind=%s by user=%s", new_id, v["kind"], actor)
    return new_id


def update(conn, item_id, p: dict, *, actor=None) -> None:
    cur = get_item(conn, item_id)
    if not cur or (cur.get("status") or "open") == "cancelled":
        raise LookupError("not found")
    if not (cur.get("kind")):
        cur["kind"] = "task"
    v = _validate(conn, p, cur)
    cols = ["kind", "title", "detail", "assignee_user_id", "project_code", "due_date", "due_time", "start_date",
            "start_time", "end_date", "end_time", "location", "address"]
    conn.execute(f"UPDATE crm_task SET {', '.join(c + ' = ?' for c in cols)}, updated_at = ?, updated_by = ? WHERE id = ?",
                 (*[v[c] for c in cols], _now(), actor, item_id))
    conn.commit()
    log.info("team item updated id=%s by user=%s", item_id, actor)


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
    return {"id": item_id, "status": "done" if done else "open"}


def cancel(conn, item_id, *, actor=None) -> None:
    cur = get_item(conn, item_id)
    if not cur or (cur.get("status") or "open") == "cancelled":
        raise LookupError("not found")
    conn.execute("UPDATE crm_task SET status = 'cancelled', updated_at = ?, updated_by = ? WHERE id = ?", (_now(), actor, item_id))
    conn.commit()
    log.info("team item deleted id=%s by user=%s", item_id, actor)


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
                                           date_from=d_from, date_to=d_to, me=uid, can_edit=role in EDIT_ROLES))
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
    app.add_url_rule("/api/team/address-suggest", "team_address_suggest", _api_suggest, methods=["GET"])
    app.add_url_rule("/api/team/items", "team_items_list", _api_list, methods=["GET"])
    app.add_url_rule("/api/team/items", "team_items_create", _api_create, methods=["POST"])
    app.add_url_rule("/api/team/items/<int:item_id>", "team_items_update", _api_update, methods=["PUT"])
    app.add_url_rule("/api/team/items/<int:item_id>/done", "team_items_done", _api_done, methods=["POST"])
    app.add_url_rule("/api/team/items/<int:item_id>", "team_items_delete", _api_delete, methods=["DELETE"])
