#!/usr/bin/env python3
"""Reminders 1 day + 1 hour before meetings, events, timed tasks and walkthroughs (event_reminders). Guard.

Boots the real app on an ISOLATED database and drives run_round with a fixed clock. Proves:
  * a timed meeting: '1d' at start - 24h and '1h' at start - 60 min, each once, to the assignee
    AND every attendee; nothing before its moment, nothing once it has started
  * a second round at the same moment sends nothing (once only)
  * an event with no time: one reminder, 9 AM the day before; no '1h'
  * a task with a time is reminded; a task without a time never is
  * done / cancelled items and inactive people get nothing
  * booked late: a meeting created 3 hours ahead gets no '1d', only the '1h'
  * moved: a new start time is reminded again
  * a scheduled walkthrough visit reminds its attendee; a cancelled one does not
  * the email: subject reads "Tomorrow 2:00 PM · <title>" / "In 1 hour · <title>", the HTML carries
    the title escaped, the job, the directions link and the Open button; a notification_log row per send
  * titles never reach the log
  * Google: event_body carries the same pop-ups (1440 + 60 timed, 900 all-day, none for an untimed
    task or a done one); meeting invitations inherit them
Run:  venv/bin/python tests/smoke_event_reminders.py
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_reminders_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ["SSC_REMINDERS"] = "0"   # no background loop: the test drives every round
os.environ["SSC_GMAIL_POLL"] = "0"
os.environ.setdefault("SSC_STRICT_ROUTES", "0")
os.environ.pop("SENDGRID_API_KEY", None)

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402,F401  (boots the whole app)
import event_reminders as er  # noqa: E402
import gcal_sync  # noqa: E402

TAG = "remindsmoke"
P = "REMIND-P"
EST = "REMIND-EST-1"
SECRET = "Walk Zebediah through the remindsmoke scaffold"   # must never reach the log
DAY = "2031-03-11"      # far from today, so nothing else in the database shares the window
PREV = "2031-03-10"
OLD = "2031-01-01T08:00:00"
FAILS = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _db():
    return db_layer.connect(pragma_fk=False)


def _uids(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]


def _wipe(conn):
    uids = _uids(conn)
    tids = [r["id"] for r in conn.execute("SELECT id FROM crm_task WHERE created_by IN (%s)" % ",".join("?" * len(uids)),
                                          uids).fetchall()] if uids else []
    purge(conn, "crm_task_attendee", "task_id", tids)
    purge(conn, "event_reminder_sent", "user_id", uids)
    purge(conn, "notification_log", "recipient_user_id", uids)
    purge(conn, "crm_task", "created_by", uids)
    eids = [r["id"] for r in conn.execute("SELECT id FROM estimate WHERE code = ?", (EST,)).fetchall()]
    purge(conn, "walkthrough_visit", "estimate_id", eids)
    purge(conn, "estimate", "code", [EST])
    purge(conn, "crm_organization", "name", [f"{TAG} org"])
    purge(conn, "projects", "project_code", [P])
    conn.commit()
    purge_users(conn, uids)
    conn.commit()


def _seed() -> dict:
    conn = _db()
    try:
        _wipe(conn)
        conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, 'Remind Job', '1 Remind St', 'active')", (P,))
        ids = {}
        for key, active in (("admin", 1), ("pm", 1), ("super", 1), ("gone", 0)):
            email = f"{TAG}.{key}@example.com"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, display_name, is_active, status, must_reset_password) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                         (email, auth.hash_password("RemindSmoke2026"), "admin" if key == "admin" else "pm",
                          f"Remind {key}", f"Remind {key}", active, "active" if active else "disabled"))
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.commit()
        return ids
    finally:
        conn.close()


def _task(conn, ids, *, title, kind, day, at=None, assignee=None, status="open", created=OLD, attendees=(), address=None):
    timed_cols = ("start_date", "start_time") if kind in ("event", "meeting") else ("due_date", "due_time")
    conn.execute("INSERT INTO crm_task (title, kind, status, assignee_user_id, created_by, created_at, project_code, address, "
                 f"due_date, {timed_cols[0]}, {timed_cols[1]}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                 if kind in ("event", "meeting") else
                 "INSERT INTO crm_task (title, kind, status, assignee_user_id, created_by, created_at, project_code, address, "
                 "due_date, due_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (title, kind, status, assignee, ids["admin"], created, P, address, day, day, at) if kind in ("event", "meeting")
                 else (title, kind, status, assignee, ids["admin"], created, P, address, day, at))
    tid = conn.execute("SELECT MAX(id) AS m FROM crm_task").fetchone()["m"]
    for u in attendees:
        conn.execute("INSERT INTO crm_task_attendee (task_id, user_id) VALUES (?, ?)", (tid, u))
    conn.commit()
    return tid


def _sent(conn, tid, source="team") -> set:
    return {(r["user_id"], r["slot"]) for r in conn.execute(
        "SELECT user_id, slot FROM event_reminder_sent WHERE source = ? AND item_id = ?", (source, tid)).fetchall()}


def _count(conn, tid, slot) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM event_reminder_sent WHERE source = 'team' AND item_id = ? AND slot = ?",
                        (tid, slot)).fetchone()["n"]


def at(s: str) -> datetime:
    return datetime.fromisoformat(s)


def main() -> int:
    ids = _seed()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    logging.getLogger().addHandler(handler)
    logging.getLogger("ssc.event_reminders").setLevel(logging.INFO)
    conn = _db()
    try:
        meet = _task(conn, ids, title=SECRET + " <b>", kind="meeting", day=DAY, at="14:00", assignee=ids["pm"],
                     attendees=(ids["super"], ids["gone"]), address="890 E 135th St, Bronx")
        allday = _task(conn, ids, title="Inspection day", kind="event", day=DAY, assignee=ids["pm"])
        call = _task(conn, ids, title="Call the architect", kind="task", day=DAY, at="10:30", assignee=ids["super"])
        notime = _task(conn, ids, title="Send the invoice", kind="task", day=DAY, assignee=ids["super"])
        done = _task(conn, ids, title="Already done", kind="event", day=DAY, at="14:00", assignee=ids["pm"], status="done")
        gone = _task(conn, ids, title="Cancelled", kind="meeting", day=DAY, at="14:00", assignee=ids["pm"], status="cancelled")
        late = _task(conn, ids, title="Booked late", kind="meeting", day=DAY, at="16:00", assignee=ids["pm"],
                     created=f"{DAY}T13:00:00")
        conn.execute("INSERT INTO crm_organization (name) VALUES (?)", (f"{TAG} org",))
        org_id = conn.execute("SELECT MAX(id) AS m FROM crm_organization").fetchone()["m"]
        conn.execute("INSERT INTO estimate (code, est_type, borough, seq, client_org_id, building_address, status, created_at) "
                     "VALUES (?, 'FR', 'BX', 1, ?, '12 Walk Ave, Bronx', 'scoping', ?)", (EST, org_id, OLD))
        est_id = conn.execute("SELECT id FROM estimate WHERE code = ?", (EST,)).fetchone()["id"]
        for status in ("scheduled", "cancelled"):
            conn.execute("INSERT INTO walkthrough_visit (estimate_id, visit_date, visit_time, attendee_user_id, status, created_at) "
                         "VALUES (?, ?, '09:00', ?, ?, ?)", (est_id, DAY, ids["pm"], status, OLD))
        visits = [r["id"] for r in conn.execute("SELECT id FROM walkthrough_visit WHERE estimate_id = ? ORDER BY id", (est_id,))]
        conn.commit()

        # before anything is due
        er.run_round(conn, at(f"{PREV}T08:00"))
        check(not _sent(conn, meet) and not _sent(conn, allday), "nothing is sent before a reminder's moment")

        # 9 AM the day before: the all-day event
        er.run_round(conn, at(f"{PREV}T09:00"))
        check(_sent(conn, allday) == {(ids["pm"], "1d")}, "an event with no time: one reminder at 9 AM the day before")
        check(_sent(conn, visits[0], "walkthrough") == {(ids["pm"], "1d")}, "a scheduled walkthrough reminds its attendee a day ahead")
        check(not _sent(conn, visits[1], "walkthrough"), "a cancelled walkthrough gets nothing")
        er.run_round(conn, at(f"{PREV}T10:30"))
        check(_sent(conn, call) == {(ids["super"], "1d")}, "a task with a time gets its day-before reminder")

        # 2 PM the day before: the meeting, to the assignee + the active attendee
        n = er.run_round(conn, at(f"{PREV}T14:00"))
        check(_sent(conn, meet) == {(ids["pm"], "1d"), (ids["super"], "1d")}, "a meeting reminds the assignee and every attendee 1 day before")
        check(not any(u == ids["gone"] for u, _ in _sent(conn, meet)), "an inactive person gets nothing")
        check(n == 2, "the 2 PM round sends exactly the two meeting reminders")
        check(er.run_round(conn, at(f"{PREV}T14:01")) == 0, "a second round sends nothing again")
        check(not _sent(conn, done) and not _sent(conn, gone), "done and cancelled items get nothing")

        row = conn.execute("SELECT subject, deep_link, sent, error FROM notification_log WHERE kind = 'event_reminder' "
                           "AND recipient_user_id = ? ORDER BY id DESC", (ids["pm"],)).fetchone()
        check(row and row["subject"] == f"Tomorrow 2:00 PM · {SECRET} <b>", "the subject reads 'Tomorrow 2:00 PM · <title>'")
        check(row and row["sent"] == 0 and row["error"] is None, "no mail key: logged, not sent, no error")

        it = next(i for i in er._team_items(conn, DAY, DAY) if i["id"] == meet)
        it["people"] = {ids["pm"], ids["super"]}   # as due_reminders leaves it: active people only
        it["names"] = er._names(conn, it["people"])
        it["guests"] = ["guest.one@example.org"]
        msg = er.compose(it, "1h", at(f"{DAY}T13:00"), ids["pm"])
        check(msg["subject"] == f"In 1 hour · {SECRET} <b>", "the 1-hour subject reads 'In 1 hour · <title>'")
        check("&lt;b&gt;" in msg["html"] and "<b>" not in msg["html"].split("<h1", 1)[1].split("</h1>", 1)[0],
              "the title is escaped in the email")
        check("Remind Job" in msg["html"] and "google.com/maps/dir" in msg["html"] and "Open in Superstars" in msg["html"],
              "the email carries the job, directions and the Open button")
        check("People: You, Remind super, guest.one@example.org" in msg["text"]
              and "You<br>Remind super<br>guest.one@example.org" in msg["html"],
              "the email lists everyone on it: you first, the team by name, then outside guests")
        check(er.FROM_NAME == "Superstars App", "the sender reads 'Superstars App'")

        # booked three hours ahead: no '1d'; the '1h' comes
        er.run_round(conn, at(f"{DAY}T14:30"))
        check(not _sent(conn, late), "a meeting booked 3 hours ahead gets no day-before reminder")
        er.run_round(conn, at(f"{DAY}T15:00"))
        check(_sent(conn, late) == {(ids["pm"], "1h")}, "...and gets the 1-hour reminder")

        # 1 hour before the meeting; then after it has started
        er.run_round(conn, at(f"{DAY}T13:00"))
        check((ids["pm"], "1h") in _sent(conn, meet) and (ids["super"], "1h") in _sent(conn, meet), "1 hour before: everyone on it")
        check(not any(s == "1h" for _, s in _sent(conn, allday)), "an event with no time has no 1-hour reminder")
        check(not _sent(conn, notime), "a task with no time never gets a reminder")

        # moved to the next day: reminded again for the new time
        conn.execute("UPDATE crm_task SET start_date = '2031-03-12', due_date = '2031-03-12' WHERE id = ?", (meet,))
        conn.commit()
        er.run_round(conn, at("2031-03-11T14:00"))
        check(_count(conn, meet, "1d") == 4, "a moved meeting is reminded again for its new time")
        er.run_round(conn, at("2031-03-12T14:05"))
        check(_count(conn, meet, "1h") == 2, "nothing goes out once the meeting has started")

        logs = buf.getvalue()
        check("reminder: source=team" in logs and SECRET not in logs and "@example.com" not in logs,
              "the log has ids only: no titles, no emails")

        # Google pop-ups
        b = gcal_sync.event_body({"id": 1, "kind": "meeting", "start_date": DAY, "start_time": "14:00"})
        check(b["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 1440},
                                                                    {"method": "popup", "minutes": 60}]},
              "Google: a timed item pops up 1 day and 1 hour before")
        b = gcal_sync.event_body({"id": 1, "kind": "event", "start_date": DAY})
        check(b["reminders"]["overrides"] == [{"method": "popup", "minutes": 900}], "Google: an all-day event pops up 9 AM the day before")
        b = gcal_sync.event_body({"id": 1, "kind": "task", "due_date": DAY})
        check(b["reminders"]["overrides"] == [], "Google: a task with no time has no pop-up")
        b = gcal_sync.event_body({"id": 1, "kind": "task", "due_date": DAY, "due_time": "10:30", "status": "done"})
        check(b["reminders"]["overrides"] == [], "Google: a done item has no pop-up")
    finally:
        logging.getLogger().removeHandler(handler)
        _wipe(conn)
        conn.close()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{'FAILED' if FAILS else 'OK'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
