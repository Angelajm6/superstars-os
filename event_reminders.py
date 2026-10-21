"""Reminders before meetings, events, calls and walkthroughs — like Google Calendar's (2026-10-21).

Everyone on an item gets an email 1 day before and 1 hour before it starts. Items with no time
get one email, the day before at 9 AM (Google's all-day default). People who connected Google
Calendar also get Google's own pop-ups at the same moments (gcal_sync.event_body sets them).

What counts
  * crm_task (team_items): an open event or meeting with a start day; an open task with a due
    day AND a due time ("Call the architect, 2 PM"). A task with no time gets nothing.
    Who: the assignee + the meeting's attendees (crm_task_attendee).
  * walkthrough_visit (walkthroughs): a scheduled visit. Who: its attendee.
  Only active users. A multi-day event is reminded of its first day only.

When
  * timed:   '1d' at start - 24h (sent until start - 1h), '1h' at start - 60 min (sent until start)
  * untimed: '1d' at 09:00 the day before (sent until the day begins)
  A reminder whose moment fell before the item was created or last edited is skipped (no
  "tomorrow" email for a meeting booked three hours ahead). The windows mean a restart loses
  nothing, and nothing goes out once the item has started.

Once only: event_reminder_sent holds (source, item_id, user_id, slot, starts_at). The start is
in the key, so a moved item is reminded again for its new time.

Sending: the branded email, from "Superstars App", listing everyone on the item (team by name, outside
guests by email), through notifications._send_live (live only with SENDGRID_API_KEY);
every reminder is a notification_log row (kind 'event_reminder'). A loop thread runs a round
every minute (SSC_REMINDERS=0 turns it off). Logs carry ids and counts only, never titles or
emails. Times are the app's local clock (ssc_tz).
"""
from __future__ import annotations

import html
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

import db_layer

log = logging.getLogger("ssc.event_reminders")

ROUND_SECONDS = 60
ALL_DAY_HOUR = 9
LOOKAHEAD = timedelta(hours=25)
FROM_NAME = "Superstars App"
_LOOP = {"started": False}
_F = "Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


# ------------------------------------------------------------------ schema
def ensure(conn) -> None:
    pk = "id SERIAL PRIMARY KEY" if db_layer.is_postgres() else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(f"CREATE TABLE IF NOT EXISTS event_reminder_sent ({pk}, source TEXT NOT NULL, item_id INTEGER NOT NULL, "
                 "user_id INTEGER NOT NULL, slot TEXT NOT NULL, starts_at TEXT NOT NULL, sent_at TEXT)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_event_reminder_once "
                 "ON event_reminder_sent(source, item_id, user_id, slot, starts_at)")
    conn.commit()


# ------------------------------------------------------------------ helpers
def _rows(conn, sql, params=()) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _when(day, hhmm) -> Optional[datetime]:
    try:
        d = date.fromisoformat(str(day)[:10])
    except (TypeError, ValueError):
        return None
    if not hhmm:
        return datetime(d.year, d.month, d.day)
    try:
        h, m = str(hhmm).split(":")[:2]
        return datetime(d.year, d.month, d.day, int(h), int(m))
    except ValueError:
        return None


def _stamp(*values) -> Optional[datetime]:
    """The latest of created_at / updated_at (ISO text, 'T' or space)."""
    out = None
    for v in values:
        try:
            t = datetime.fromisoformat(str(v)[:19].replace(" ", "T")) if v else None
        except ValueError:
            t = None
        if t and (out is None or t > out):
            out = t
    return out


def _active(conn, uids) -> list:
    uids = sorted({u for u in uids if u})
    if not uids:
        return []
    marks = ",".join("?" * len(uids))
    return [r["id"] for r in _rows(conn, f"SELECT id FROM users WHERE id IN ({marks}) AND is_active = 1 "
                                         "AND COALESCE(status, 'active') = 'active'", tuple(uids))]


def _names(conn, uids) -> dict:
    uids = sorted(uids)
    if not uids:
        return {}
    marks = ",".join("?" * len(uids))
    return {r["id"]: r["nm"] for r in _rows(conn, f"SELECT id, COALESCE(display_name, full_name, email) AS nm FROM users "
                                                 f"WHERE id IN ({marks})", tuple(uids))}


def _slots(start: datetime, timed: bool, made: Optional[datetime], now: datetime) -> list:
    """The slots due right now for an item starting at `start`."""
    if timed:
        plan = (("1d", start - timedelta(hours=24), start - timedelta(hours=1)),
                ("1h", start - timedelta(hours=1), start))
    else:
        plan = (("1d", start - timedelta(days=1) + timedelta(hours=ALL_DAY_HOUR), start),)
    return [slot for slot, at, until in plan if at <= now < until and not (made and at < made)]


# ------------------------------------------------------------------ what is due
def _team_items(conn, today: str, horizon: str) -> list:
    import team_items
    rows = _rows(conn,
                 "SELECT t.id, t.title, t.kind, t.assignee_user_id, t.due_date, t.due_time, t.start_date, t.start_time, "
                 "       t.end_time, t.location, t.address, t.guest_emails, t.created_at, t.updated_at, p.name AS project_name, mi.meet_url "
                 "FROM crm_task t LEFT JOIN projects p ON p.project_code = t.project_code "
                 "LEFT JOIN meeting_invite mi ON mi.item_id = t.id "
                 "WHERE t.status = 'open' AND ("
                 "  (t.kind IN ('event', 'meeting') AND COALESCE(t.start_date, t.due_date) >= ? AND COALESCE(t.start_date, t.due_date) <= ?) OR "
                 "  (COALESCE(t.kind, 'task') = 'task' AND t.due_time IS NOT NULL AND t.due_time <> '' AND t.due_date >= ? AND t.due_date <= ?))",
                 (today, horizon, today, horizon))
    people = {}
    meeting_ids = [r["id"] for r in rows if r["kind"] == "meeting"]
    if meeting_ids:
        marks = ",".join("?" * len(meeting_ids))
        for a in _rows(conn, f"SELECT task_id, user_id FROM crm_task_attendee WHERE task_id IN ({marks})", tuple(meeting_ids)):
            people.setdefault(a["task_id"], set()).add(a["user_id"])
    out = []
    for r in rows:
        dated = r["kind"] in ("event", "meeting")
        day, hhmm = (r["start_date"] or r["due_date"], r["start_time"]) if dated else (r["due_date"], r["due_time"])
        out.append({"source": "team", "id": r["id"], "title": r["title"] or ("Meeting" if r["kind"] == "meeting" else "Event"),
                    "start": _when(day, hhmm), "timed": bool(hhmm), "end_time": r["end_time"] if dated else None,
                    "made": _stamp(r["created_at"], r["updated_at"]), "job": r["project_name"],
                    "place": " · ".join(x for x in (r["location"], r["address"]) if x) or None, "address": r["address"],
                    "meet_url": r["meet_url"], "path": "/projects",
                    "guests": team_items._json_list(r["guest_emails"]) if r["kind"] == "meeting" else [],
                    "people": {r["assignee_user_id"], *people.get(r["id"], ())}})
    return out


def _walkthroughs(conn, today: str, horizon: str) -> list:
    try:
        rows = _rows(conn,
                     "SELECT v.id, v.visit_date, v.visit_time, v.attendee_user_id, v.created_at, v.updated_at, "
                     "       e.code, e.building_address "
                     "FROM walkthrough_visit v JOIN estimate e ON e.id = v.estimate_id "
                     "WHERE v.status = 'scheduled' AND v.visit_date >= ? AND v.visit_date <= ?", (today, horizon))
    except Exception:   # a database without the estimating tables
        conn.rollback()
        return []
    return [{"source": "walkthrough", "id": r["id"],
             "title": "Walkthrough" + (" · " + r["building_address"] if r["building_address"] else ""),
             "start": _when(r["visit_date"], r["visit_time"]), "timed": bool(r["visit_time"]), "end_time": None,
             "made": _stamp(r["created_at"], r["updated_at"]), "job": r["code"], "place": r["building_address"],
             "address": r["building_address"], "meet_url": None, "guests": [], "path": "/estimating#" + (r["code"] or ""),
             "people": {r["attendee_user_id"]}} for r in rows]


def due_reminders(conn, now: datetime) -> list:
    """[(item, user_id, slot)] due at `now` and not sent yet."""
    today, horizon = now.date().isoformat(), (now + LOOKAHEAD).date().isoformat()
    out = []
    for it in _team_items(conn, today, horizon) + _walkthroughs(conn, today, horizon):
        if not it["start"] or it["start"] <= now:
            continue
        slots = _slots(it["start"], it["timed"], it["made"], now)
        if not slots:
            continue
        starts_at = it["start"].isoformat(timespec="minutes")
        it["people"] = set(_active(conn, it["people"]))
        it["names"] = _names(conn, it["people"])
        for uid in sorted(it["people"]):
            for slot in slots:
                if not conn.execute("SELECT 1 FROM event_reminder_sent WHERE source = ? AND item_id = ? AND user_id = ? "
                                    "AND slot = ? AND starts_at = ?", (it["source"], it["id"], uid, slot, starts_at)).fetchone():
                    out.append((it, uid, slot))
    return out


# ------------------------------------------------------------------ the email
def _clock(t: datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def _hhmm(v) -> Optional[str]:
    t = _when("2000-01-01", v) if v else None
    return _clock(t) if t else None


def _day_word(start: datetime, now: datetime) -> str:
    gap = (start.date() - now.date()).days
    return "Today" if gap == 0 else "Tomorrow" if gap == 1 else start.strftime("%A")


def _everyone(it: dict, me: Optional[int]) -> list:
    """Everyone on the item, for the email: the reader first ("You"), the team by name, then outside guests."""
    names = it.get("names") or {}
    team = sorted((names.get(u) or "Team member" for u in it["people"] if u != me), key=str.lower)
    return (["You"] if me in it["people"] else []) + team + list(it.get("guests") or [])


def compose(it: dict, slot: str, now: datetime, me: Optional[int] = None) -> dict:
    """{subject, text, html} for one reminder to user `me`."""
    import notifications
    import team_items
    start, title = it["start"], it["title"]
    lead = "In 1 hour" if slot == "1h" else _day_word(start, now) + (f" {_clock(start)}" if it["timed"] else "")
    head = "In 1 hour" if slot == "1h" else _day_word(start, now) + (f" at {_clock(start)}" if it["timed"] else "")
    when = f"{_day_word(start, now)} · {start.strftime('%A, %B')} {start.day}"
    if it["timed"]:
        end = _hhmm(it.get("end_time"))
        when += f" · {_clock(start)}" + (f" – {end}" if end else "")
    everyone = _everyone(it, me)
    link = notifications._base_url() + it["path"]
    directions = team_items.directions_url(it.get("address"))
    rows = [("When", when)]
    if it.get("job"):
        rows.append(("Job", it["job"]))
    if it.get("place"):
        rows.append(("Where", it["place"]))
    if it.get("meet_url"):
        rows.append(("Video", it["meet_url"]))
    if len(everyone) > 1:
        rows.append(("People", everyone))
    text = "\n".join([f"{head}: {title}", ""] + [f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in rows] +
                     ([f"Directions: {directions}"] if directions else []) +
                     ["", f"Open in Superstars: {link}", "", f"— {FROM_NAME} (reminders go out 1 day and 1 hour before)"])
    e = html.escape

    def cell(k, v_html):
        return (f'<tr><td style="padding:6px 0;width:92px;vertical-align:top;font-family:{_F};font-size:13px;color:#76777E;">{k}</td>'
                f'<td style="padding:6px 0;font-family:{_F};font-size:14px;line-height:1.45;color:#14161C;">{v_html}</td></tr>')
    cells = ""
    for k, v in rows:
        v_html = "<br>".join(e(x) for x in v) if isinstance(v, list) else e(v)
        if k == "Where" and directions:
            v_html += f' · <a href="{e(directions)}" style="color:#B11E2E;">Directions</a>'
        if k == "Video":
            v_html = f'<a href="{e(v)}" style="color:#B11E2E;">Join with Google Meet</a>'
        cells += cell(k, v_html)
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)}</title></head>
<body style="margin:0;padding:0;background:#FAF7F1;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">{e(head)} · {e(title)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#FAF7F1;"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:520px;background:#FFFFFF;border:1px solid #E8E4DD;border-radius:16px;overflow:hidden;">
<tr><td style="height:6px;background:#B11E2E;font-size:0;line-height:0;">&nbsp;</td></tr>
<tr><td style="padding:22px 24px 0 24px;font-family:{_F};"><div style="font-size:15px;font-weight:700;letter-spacing:0.6px;text-transform:uppercase;color:#14161C;">Superstars Contracting</div><div style="font-size:10px;font-weight:600;letter-spacing:1.2px;text-transform:uppercase;color:#76777E;margin-top:3px;">Reminder</div></td></tr>
<tr><td style="padding:20px 24px 0 24px;font-family:{_F};"><div style="font-size:13px;font-weight:600;color:#B11E2E;margin-bottom:4px;">{e(head)}</div><h1 style="margin:0;font-size:22px;line-height:1.25;font-weight:700;color:#14161C;">{e(title)}</h1></td></tr>
<tr><td style="padding:14px 24px 0 24px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{cells}</table></td></tr>
<tr><td style="padding:20px 24px 0 24px;"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr><td align="center" style="background:#24418C;border-radius:9px;"><a href="{e(link)}" style="display:inline-block;padding:13px 24px;font-family:{_F};font-size:14px;font-weight:700;letter-spacing:0.3px;color:#FFFFFF;text-decoration:none;border-radius:9px;">Open in Superstars</a></td></tr></table></td></tr>
<tr><td style="padding:22px 24px 0 24px;"><div style="height:1px;background:#E8E4DD;font-size:0;line-height:0;">&nbsp;</div></td></tr>
<tr><td style="padding:14px 24px 22px 24px;font-family:{_F};font-size:12px;line-height:1.5;color:#76777E;">You get this because you are on this item in Superstars. Reminders go out 1 day and 1 hour before.</td></tr>
</table></td></tr></table>
</body></html>"""
    return {"subject": f"{lead} · {title}", "text": text, "html": page, "link": link}


# ------------------------------------------------------------------ one round
def run_round(conn, now: Optional[datetime] = None) -> int:
    """Send every reminder due now. Returns how many went out (or were logged)."""
    import notifications
    now = now or datetime.now()
    due = due_reminders(conn, now)
    sent_n = 0
    for it, uid, slot in due:
        u = conn.execute("SELECT email FROM users WHERE id = ?", (uid,)).fetchone()
        email = u["email"] if u else None
        msg = compose(it, slot, now, uid)
        conn.execute("INSERT INTO event_reminder_sent (source, item_id, user_id, slot, starts_at, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (it["source"], it["id"], uid, slot, it["start"].isoformat(timespec="minutes"), now.isoformat(timespec="seconds")))
        conn.commit()   # recorded before the send: a crash mid-send never sends twice
        sent, error = 0, None
        if not email:
            error = "no recipient"
        elif notifications._live_key():
            sent, error = notifications._send_live(email, msg["subject"], msg["text"], msg["html"], from_name=FROM_NAME)
        conn.execute("INSERT INTO notification_log (kind, recipient_user_id, recipient_email, subject, deep_link, "
                     "created_at, sent, error) VALUES ('event_reminder', ?, ?, ?, ?, ?, ?, ?)",
                     (uid, email, msg["subject"], msg["link"], now.isoformat(timespec="seconds"), sent, error))
        conn.commit()
        sent_n += 1
        log.info("reminder: source=%s item=%s user=%s slot=%s sent=%s err=%s", it["source"], it["id"], uid, slot, sent, error or "-")
    return sent_n


def _refresh_google() -> None:
    """Once per boot: put the new pop-up reminders on everyone's existing Superstars copies
    (gcal_sync skips every copy whose body did not change)."""
    import gcal_sync
    if gcal_sync.unavailable_reason():
        return
    conn = db_layer.connect()
    try:
        uids = [r["user_id"] for r in _rows(conn, "SELECT user_id FROM google_calendar_link WHERE status = 'active'")]
    finally:
        conn.close()
    gcal_sync.schedule(*uids)
    log.info("reminders: google refresh scheduled for %s people", len(uids))


def _loop() -> None:
    time.sleep(30)
    try:
        _refresh_google()
    except Exception as e:
        log.warning("reminders google refresh failed: %s", type(e).__name__)
    while True:
        conn = db_layer.connect()
        try:
            run_round(conn)
        except Exception as e:
            log.warning("reminder round failed: %s", type(e).__name__)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
        time.sleep(ROUND_SECONDS)


def start() -> None:
    if _LOOP["started"] or os.environ.get("SSC_REMINDERS", "1") != "1":
        return
    _LOOP["started"] = True
    threading.Thread(target=_loop, name="event-reminders", daemon=True).start()


def register(app) -> None:
    conn = db_layer.connect()
    try:
        ensure(conn)
        db_layer._PKMAP_CACHE.clear()   # a table created at boot after the pk-map cache filled (#290 lesson)
    except Exception as e:
        log.warning("event reminders schema ensure skipped: %s", type(e).__name__)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    start()
