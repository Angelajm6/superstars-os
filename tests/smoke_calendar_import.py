#!/usr/bin/env python3
"""Google Calendar -> the software, PRIVATE to each person (revamped 2026-10-16): the BEHAVIORAL guard
for calendar_import.py.

Proves against an ISOLATED database (no Google, no model call):
  * times: a timed event lands in New York time; an all-day event's exclusive end date is handled;
    an event set in another zone keeps that zone + its local start time; the same offset adds nothing
  * every event is copied (work and personal alike) into user_calendar_event for THAT user only, and
    never becomes a team item (crm_task); an edit updates the copy; an unchanged event is skipped; a
    cancelled or declined event deletes it; out-of-office / birthdays / working location are skipped
  * events_for(uid) returns only that user's rows; another user's copy of the same Google id is separate
  * pulling: the first read asks for the coming window, later reads ask only for changes; a 'gone'
    answer (410) starts over; a connection without the read permission is skipped
  * other calendars: the ticked ones (calendarList selected=true) are read too, keyed "<cal>:<id>"; the
    main calendar, unticked ones and the app's own Superstars calendar are not; an event there the person is
    invited to is skipped; each calendar keeps its own "changed since" mark; unticking removes its copies;
    a connection without the list permission reads the main calendar only (and the Inbox hint offers a reconnect)
  * deletes read back: a copy deleted in an admin's Superstars calendar deletes the item in the app (status
    'cancelled', like Delete); a PM's delete only drops their own Google copy, which sync_user does not put
    back; a cancelled copy of an item the app itself is taking off that person's calendar changes nothing
  * token expiry: a revoked sign-in marks the link needs_reconnect (and the loop stops reading it);
    a single 401 on the events call refreshes the token once and carries on
  * the one-time move: the old import's team copies (crm_task source 'gcal') become private rows of
    their person, leave crm_task and the Superstars-calendar bookkeeping, and that person re-reads
    the whole window; running it again does nothing
  * disconnecting deletes the private copies; nothing goes back to Google twice
  * logs carry ids / counts only; the Calendar card says the events are private

Database: honors SSC_DB_URL like every gate suite; otherwise a throwaway SQLite snapshot.
Run:  venv/bin/python tests/smoke_calendar_import.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import logging
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_calimp_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_GMAIL_POLL": "0",
                   "GOOGLE_OAUTH_CLIENT_ID": "test-client.apps.googleusercontent.com", "GOOGLE_OAUTH_CLIENT_SECRET": "test-secret",
                   "GOOGLE_OAUTH_REDIRECT_URI": "https://app.example.test/auth/google/callback",
                   "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(), "SSC_ALLOWED_GOOGLE_DOMAIN": "superstarscontracting.com"})
os.environ.pop("SSC_TIMEZONE", None)

import calendar_import as ci  # noqa: E402
import db_layer  # noqa: E402
import gcal_sync  # noqa: E402
import team_items  # noqa: E402

TAG = "calimp"
DAY = (date.today() + timedelta(days=5)).isoformat()


class LogTrap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _db():
    return db_layer.connect(pragma_fk=True)


def _q(sql, args=()):
    conn = _db()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ci.ensure(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        for t in ("user_calendar_event", "user_calendar_source", "gcal_import", "google_calendar_event", "google_calendar_link"):
            purge(conn, t, "user_id", uids)
        purge(conn, "meeting_invite", "owner_id", uids)
        conn.execute("DELETE FROM crm_task WHERE title LIKE 'Calimp%'")
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()


def _seed() -> dict:
    _cleanup()
    conn = _db()
    ids = {}
    try:
        for key, role in (("me", "admin"), ("other", "pm")):
            email = f"{TAG}.{key}@superstarscontracting.com"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, 'x', ?, ?, 1, 'active', 0, 0)", (email, role, f"Calimp {key}"))
            conn.commit()
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
            conn.execute("INSERT INTO google_calendar_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) "
                         "VALUES (?, ?, ?, 'active', ?, ?)", (ids[key], email, secret_box.encrypt_text("r"), "2026-10-15T08:00:00", gcal_sync.SCOPES))
        conn.commit()
    finally:
        conn.close()
    return ids


def ev(gid, title, *, start=None, end=None, allday=None, location="", updated="2026-10-15T10:00:00.000Z",
       status="confirmed", event_type="default", desc="", zone=None):
    e = {"id": gid, "summary": title, "location": location, "description": desc, "status": status, "updated": updated,
         "eventType": event_type, "attendees": []}
    if allday:
        e.update(start={"date": allday[0]}, end={"date": allday[1]})
    else:
        e.update(start={"dateTime": start or f"{DAY}T14:00:00Z"}, end={"dateTime": end or f"{DAY}T15:30:00Z"})
        if zone:
            e["start"]["timeZone"] = zone
    return e


def mine(uid):
    return _q("SELECT * FROM user_calendar_event WHERE user_id = ? ORDER BY google_event_id", (uid,))


def main() -> int:
    trap = LogTrap()
    lg = logging.getLogger("ssc.calendar_import")
    lg.addHandler(trap)
    lg.setLevel(logging.INFO)
    ids = _seed()
    me, other = ids["me"], ids["other"]
    passed = 0
    try:
        # ---- times + zones
        w = ci.when(ev("t", "x", start=f"{DAY}T14:00:00Z", end=f"{DAY}T15:30:00Z"))
        assert w["start_date"] == DAY and w["start_time"] in ("10:00", "09:00") and w["end_time"] in ("11:30", "10:30") and w["end_date"] is None
        assert w["source_tz"] is None
        w = ci.when(ev("t", "x", allday=(DAY, (date.fromisoformat(DAY) + timedelta(days=1)).isoformat())))
        assert w["start_date"] == DAY and w["start_time"] is None and w["end_date"] is None and w["source_tz"] is None
        three = (date.fromisoformat(DAY) + timedelta(days=3)).isoformat()
        assert ci.when(ev("t", "x", allday=(DAY, three)))["end_date"] == (date.fromisoformat(DAY) + timedelta(days=2)).isoformat()
        w = ci.when(ev("t", "x", start=f"{DAY}T12:00:00-05:00", end=f"{DAY}T13:00:00-05:00", zone="America/Chicago"))
        assert w["source_tz"] == "America/Chicago" and w["source_start_time"] == "12:00" and w["start_time"] == "13:00", w
        w = ci.when(ev("t", "x", start=f"{DAY}T12:00:00-04:00", zone="America/Detroit"))   # same wall clock as New York
        assert w["source_tz"] is None
        late = ci.when(ev("t", "x", start=f"{DAY}T23:30:00-08:00", end=f"{DAY}T23:45:00-08:00", zone="America/Los_Angeles"))
        assert late["start_date"] == (date.fromisoformat(DAY) + timedelta(days=1)).isoformat()   # next day in New York
        passed += 1; print("  times + zones ok")

        conn = _db()
        try:
            # ---- every event is copied privately; nothing becomes a team item
            assert ci.apply_event(conn, me, ev("e1", "Calimp walkthrough", location="455 Calimp St")) == "created"
            assert ci.apply_event(conn, me, ev("e2", "Calimp dentist")) == "created"
            assert ci.apply_event(conn, me, ev("e3", "Calimp chicago call", start=f"{DAY}T12:00:00-05:00", zone="America/Chicago")) == "created"
            assert ci.apply_event(conn, other, ev("e1", "Calimp other person's e1")) == "created"   # same Google id, other person
            rows = mine(me)
            assert [r["title"] for r in rows] == ["Calimp walkthrough", "Calimp dentist", "Calimp chicago call"]
            assert rows[0]["location"] == "455 Calimp St" and rows[2]["source_tz"] == "America/Chicago"
            assert [r["title"] for r in mine(other)] == ["Calimp other person's e1"]
            assert not _q("SELECT id FROM crm_task WHERE title LIKE 'Calimp%'")
            got = ci.events_for(conn, me, DAY, DAY)
            assert {g["title"] for g in got} == {"Calimp walkthrough", "Calimp dentist", "Calimp chicago call"} and all(g["private"] for g in got)
            assert [g["title"] for g in ci.events_for(conn, other, DAY, DAY)] == ["Calimp other person's e1"]
            assert ci.events_for(conn, None, DAY, DAY) == []
            passed += 1; print("  private copy per person ok (no team items)")

            # ---- edits, unchanged, cancels, declines, skips
            assert ci.apply_event(conn, me, ev("e1", "Calimp walkthrough moved", start=f"{DAY}T16:00:00Z", end=f"{DAY}T17:00:00Z",
                                               updated="2026-10-15T11:00:00.000Z")) == "updated"
            assert mine(me)[0]["title"] == "Calimp walkthrough moved" and mine(me)[0]["start_time"] in ("12:00", "11:00")
            assert ci.apply_event(conn, me, ev("e1", "Calimp walkthrough moved", updated="2026-10-15T11:00:00.000Z")) == "unchanged"
            assert ci.apply_event(conn, me, ev("e1", "x", status="cancelled", updated="2026-10-15T12:00:00.000Z")) == "deleted"
            declined = ev("e2", "Calimp dentist", updated="2026-10-15T12:00:00.000Z")
            declined["attendees"] = [{"email": f"{TAG}.me@superstarscontracting.com", "self": True, "responseStatus": "declined"}]
            assert ci.apply_event(conn, me, declined) == "deleted"
            assert [r["google_event_id"] for r in mine(me)] == ["e3"]
            assert [r["title"] for r in mine(other)] == ["Calimp other person's e1"]   # the other person's e1 is untouched
            assert ci.apply_event(conn, me, ev("e9", "Calimp vacation", event_type="outOfOffice")) == "skipped"
            assert ci.apply_event(conn, me, ev("e10", "Calimp birthday", event_type="birthday")) == "skipped"
            assert ci.apply_event(conn, me, ev("e11", "x", status="cancelled")) == "skipped"   # never seen
            passed += 1; print("  edits, unchanged, cancels, declines, skips ok")

            # ---- pulling through a fake Google
            seen = []

            def fake_get(conn_, link, url, params):
                if url == ci.LIST_URL:
                    return 200, {"items": []}
                seen.append(dict(params))
                if len(seen) == 3:
                    return 410, {}
                return 200, {"items": [ev("e20", "Calimp inspection", updated=f"2026-10-15T1{len(seen)}:00:00.000Z")]}
            real = ci._get
            ci._get = fake_get
            try:
                r1 = ci.import_user(conn, me)
                r2 = ci.import_user(conn, me)
                r3 = ci.import_user(conn, me)
            finally:
                ci._get = real
            assert r1 == {"created": 1} and r2 == {"updated": 1} and r3 == {"reset": True}, (r1, r2, r3)
            assert "timeMax" in seen[0] and "updatedMin" not in seen[0] and "updatedMin" in seen[1] and "timeMax" not in seen[1]
            back = (date.today() - date.fromisoformat(seen[0]["timeMin"][:10])).days
            assert ci.LOOKBACK_DAYS - 1 <= back <= ci.LOOKBACK_DAYS + 1, seen[0]["timeMin"]   # past events come in too
            assert _q("SELECT import_at FROM google_calendar_link WHERE user_id = ?", (me,))[0]["import_at"] is None   # starts over
            passed += 1; print("  pulling ok (window, changes, 410)")

            # ---- other calendars
            conn.execute("UPDATE google_calendar_link SET calendar_id = 'ssc-cal@group', import_at = NULL WHERE user_id = ?", (me,))
            conn.commit()
            reads, lists = [], [0]
            cal_list = [{"id": "me@x", "primary": True, "selected": True}, {"id": "family@group", "selected": True},
                        {"id": "ssc-cal@group", "selected": True}, {"id": "hidden@group", "selected": False},
                        {"id": "crew_1#x@group", "selected": True}]

            def fake_get2(conn_, link, url, params):
                if url == ci.LIST_URL:
                    lists[0] += 1
                    return 200, {"items": cal_list}
                cal = "primary" if url == ci.EVENTS_URL else url.split("/calendars/")[1].split("/")[0]
                if cal == "ssc-cal%40group":   # read for deletes only (read_back), never copied as a calendar
                    return 200, {"items": [ev("s1", "Calimp app copy")]}
                reads.append((cal, dict(params)))
                if cal == "primary":
                    return 200, {"items": [ev("p1", "Calimp main event")]}
                if cal == "family%40group":
                    invited = ev("f2", "Calimp invited", updated="2026-10-15T10:00:00.000Z")
                    invited["attendees"] = [{"email": f"{TAG}.ME@superstarscontracting.com", "responseStatus": "accepted"}]
                    return 200, {"items": [ev("p1", "Calimp soccer"), invited]}
                if cal == "crew_1%23x%40group":
                    return 404, {}
                raise AssertionError("read a calendar it should not: " + cal)
            real = ci._get
            ci._get = fake_get2
            try:
                r1 = ci.import_user(conn, me)
                rows = {r["google_event_id"]: r for r in mine(me)}
                assert rows["p1"]["title"] == "Calimp main event" and rows["p1"]["calendar_id"] is None, r1
                assert rows["family@group:p1"]["title"] == "Calimp soccer" and rows["family@group:p1"]["calendar_id"] == "family@group"
                assert "family@group:f2" not in rows   # invited: comes from the main calendar
                assert not [k for k in rows if "s1" in k]   # the app's own Superstars calendar is not copied
                assert {c for c, _ in reads} == {"primary", "family%40group", "crew_1%23x%40group"}   # not ssc / hidden / primary twice
                assert {g["title"] for g in ci.events_for(conn, me, DAY, DAY)} >= {"Calimp main event", "Calimp soccer"}
                marks = {r["calendar_id"]: r["import_at"] for r in _q("SELECT * FROM user_calendar_source WHERE user_id = ?", (me,))}
                assert marks.get("family@group") and "crew_1#x@group" not in marks, marks   # 404: tried again next round
                reads.clear()
                ci.import_user(conn, me)
                fam = [p for c, p in reads if c == "family%40group"]
                assert fam and "updatedMin" in fam[0] and "timeMax" not in fam[0]   # its own "changed since" mark
                # unticking a calendar removes its copies
                cal_list[1]["selected"] = False
                ci.import_user(conn, me)
                assert not [r for r in mine(me) if r["calendar_id"]] and [r for r in mine(me) if r["google_event_id"] == "p1"]
                assert not _q("SELECT id FROM user_calendar_source WHERE user_id = ? AND calendar_id = 'family@group'", (me,))
                # no list permission (connected before it): main calendar only, the list is never asked for
                cal_list[1]["selected"] = True
                ci.import_user(conn, me)
                assert [r for r in mine(me) if r["calendar_id"] == "family@group"]
                conn.execute("UPDATE google_calendar_link SET scopes = ? WHERE user_id = ?", (gcal_sync.SCOPES.replace(ci.LIST_SCOPE, ""), me))
                conn.commit()
                lists[0] = 0
                assert gcal_sync.status_for(conn, me)["lists_calendars"] is False
                ci.import_user(conn, me)
                assert lists[0] == 0 and not [r for r in mine(me) if r["calendar_id"]]
            finally:
                ci._get = real
            conn.execute("DELETE FROM user_calendar_event WHERE user_id = ? AND google_event_id = 'p1'", (me,))
            conn.execute("UPDATE google_calendar_link SET scopes = ? WHERE user_id = ?", (gcal_sync.SCOPES, me))
            conn.commit()
            assert ci.LIST_SCOPE in gcal_sync.SCOPES and gcal_sync.status_for(conn, me)["lists_calendars"] is True
            passed += 1; print("  other calendars ok (ticked only, keyed, invited skipped, own marks, untick, no permission)")

            # ---- deletes in the Superstars calendar come back to the app
            def item(title, who):
                conn.execute("INSERT INTO crm_task (entity_type, title, assignee_user_id, status, priority, created_by, created_at, kind, "
                             "start_date, due_date) VALUES ('none', ?, ?, 'open', 'normal', ?, ?, 'event', ?, ?)",
                             (title, who, me, "2026-10-19T09:00:00", DAY, DAY))
                conn.commit()
                return _q("SELECT id FROM crm_task WHERE title = ?", (title,))[0]["id"]
            a, b, d = item("Calimp A admin's", me), item("Calimp B pm's", other), item("Calimp D moved away", other)
            m = item("Calimp M meeting", me)
            conn.execute("UPDATE crm_task SET kind = 'meeting', invite_owner_id = ? WHERE id = ?", (me, m))
            conn.execute("INSERT INTO meeting_invite (item_id, owner_id, google_event_id, calendar_id) VALUES (?, ?, 'gM', 'ssc-cal@group')", (m, me))
            for uid, it, gid in ((me, a, "gA"), (other, b, "gB"), (me, d, "gD")):
                conn.execute("INSERT INTO google_calendar_event (user_id, item_id, google_event_id, body_hash, synced_at) VALUES (?, ?, ?, 'h', ?)",
                             (uid, it, gid, "2026-10-19T09:00:00"))
            conn.execute("UPDATE google_calendar_link SET calendar_id = 'ssc-other@group' WHERE user_id = ?", (other,))
            conn.commit()
            gone = {"ssc-cal%40group": ["gA", "gD", "gX", "gM"], "ssc-other%40group": ["gB"]}
            superstars_reads = []

            def fake_get3(conn_, link, url, params):
                if url == ci.LIST_URL or url == ci.EVENTS_URL:
                    return 200, {"items": []}
                cal = url.split("/calendars/")[1].split("/")[0]
                superstars_reads.append((cal, dict(params)))
                return 200, {"items": [dict(ev(g, "x"), status="cancelled") for g in gone.get(cal, [])]}
            real, real_sched = ci._get, gcal_sync.schedule
            ci._get, gcal_sync.schedule = fake_get3, (lambda *a: None)
            try:
                r_me = ci.import_user(conn, me)
                r_other = ci.import_user(conn, other)
                status = {r["id"]: r["status"] for r in _q("SELECT id, status FROM crm_task WHERE id IN (?, ?, ?)", (a, b, d))}
                assert r_me.get("removed") == 2 and status[a] == "cancelled", (r_me, status)   # admin: deleted in the app too
                assert _q("SELECT status FROM crm_task WHERE id = ?", (m,))[0]["status"] == "cancelled"   # the organizer deleted the invitation
                assert status[d] == "open"   # not on their calendar any more (assigned elsewhere): nothing to do
                assert r_other.get("dropped") == 1 and status[b] == "open", (r_other, status)   # PM: only their Google copy
                assert _q("SELECT dropped FROM google_calendar_event WHERE user_id = ? AND item_id = ?", (other, b))[0]["dropped"] == 1
                assert [p for c, p in superstars_reads if c == "ssc-other%40group" and "updatedMin" not in p]   # first read: the whole window
                superstars_reads.clear()
                assert not ci.import_user(conn, me).get("removed") and not ci.import_user(conn, other).get("dropped")   # once only
                assert all("updatedMin" in p for _, p in superstars_reads)   # later rounds ask only for changes
            finally:
                ci._get, gcal_sync.schedule = real, real_sched
            posted = []
            real_http, real_tok = gcal_sync._http, gcal_sync._access_token
            gcal_sync._access_token = lambda c, link: "tok"

            def http(method, url, **kw):
                if method == "POST":
                    posted.append((kw.get("json_body") or {}).get("summary") or "")
                    return 200, {"id": "new-%d" % len(posted)}
                return 200, {"items": []}
            gcal_sync._http = http
            try:
                gcal_sync.sync_user(conn, other)
            finally:
                gcal_sync._http, gcal_sync._access_token = real_http, real_tok
            assert not [p for p in posted if "Calimp B" in p], posted   # the PM's deleted copy is not put back
            assert [p for p in posted if "Calimp D" in p], posted        # other items still go to Google
            passed += 1; print("  deletes read back ok (admin deletes the item, PM drops own copy, guard, once only)")

            # ---- token expiry: one 401 retries; a revoked sign-in needs a reconnect
            calls = []
            real_http, real_tok = gcal_sync._http, gcal_sync._access_token
            gcal_sync._access_token = lambda c, link: "tok%d" % len(calls)

            def http_401_once(method, url, **kw):
                calls.append(kw.get("token"))
                return (401, {}) if len(calls) == 1 else (200, {"items": []})
            gcal_sync._http = http_401_once
            try:
                assert ci.import_user(conn, me) == {}
                assert len(calls) == 4   # 401 + retry on the main calendar, the (empty) calendar list, the Superstars calendar
                link = gcal_sync._link(conn, me)
                assert link["status"] == "active"

                def revoked(c, link):
                    raise gcal_sync.NeedsReconnect()
                gcal_sync._access_token = revoked
                assert ci.import_user(conn, me) == {"needs_reconnect": True}
                st = gcal_sync.status_for(conn, me)
                assert st["status"] == "needs_reconnect" and st["problem"] == "Google sign-in expired"
                assert ci.import_user(conn, me) == {"skipped": True}   # the loop leaves it alone until reconnect
                assert mine(me), "the last copy stays visible to its owner while the sign-in is expired"
            finally:
                gcal_sync._http, gcal_sync._access_token = real_http, real_tok
            conn.execute("UPDATE google_calendar_link SET status = 'active', last_error = NULL WHERE user_id = ?", (me,))
            conn.execute("UPDATE google_calendar_link SET scopes = 'openid email https://www.googleapis.com/auth/calendar.app.created' WHERE user_id = ?", (other,))
            conn.commit()
            assert ci.import_user(conn, other) == {"skipped": True}   # no read permission
            passed += 1; print("  token expiry ok (401 retry, needs_reconnect, no read permission)")

            # ---- the one-time move off the team calendar
            conn.execute("INSERT INTO crm_task (entity_type, title, detail, assignee_user_id, status, priority, created_by, created_at, kind, "
                         "start_date, due_date, start_time, address, source) VALUES ('none', 'Calimp old team copy', 'From Google Calendar.\n\nbring ladder', "
                         "?, 'open', 'normal', ?, ?, 'event', ?, ?, '09:00', '1 Old St', 'gcal')", (other, other, "2026-10-15T09:00:00", DAY, DAY))
            conn.execute("INSERT INTO crm_task (entity_type, title, assignee_user_id, status, priority, created_by, created_at, kind, "
                         "start_date, due_date, source) VALUES ('none', 'Calimp cancelled copy', ?, 'cancelled', 'normal', ?, ?, 'event', ?, ?, 'gcal')",
                         (other, other, "2026-10-15T09:00:00", DAY, DAY))
            conn.commit()
            old_id = _q("SELECT id FROM crm_task WHERE title = 'Calimp old team copy'")[0]["id"]
            conn.execute("INSERT INTO gcal_import (user_id, google_event_id, item_id, decision, checked_at) VALUES (?, 'g-old', ?, 'work', ?)",
                         (other, old_id, "2026-10-15T09:00:00"))
            conn.execute("INSERT INTO google_calendar_event (user_id, item_id, google_event_id, body_hash, synced_at) VALUES (?, ?, 'x', 'h', ?)",
                         (me, old_id, "2026-10-15T09:00:00"))
            conn.execute("UPDATE google_calendar_link SET import_at = '2026-10-15T09:00:00' WHERE user_id = ?", (other,))
            conn.commit()
            assert ci.move_team_copies(conn) == 2
            assert not _q("SELECT id FROM crm_task WHERE source = 'gcal'")
            assert not _q("SELECT id FROM google_calendar_event WHERE item_id = ?", (old_id,))
            moved = [r for r in mine(other) if r["google_event_id"] == "g-old"]
            assert len(moved) == 1 and moved[0]["title"] == "Calimp old team copy" and moved[0]["detail"] == "bring ladder" and moved[0]["location"] == "1 Old St"
            assert not any(r["title"] == "Calimp cancelled copy" for r in mine(other))
            assert not any(r["title"] == "Calimp old team copy" for r in mine(me))
            assert _q("SELECT import_at FROM google_calendar_link WHERE user_id = ?", (other,))[0]["import_at"] is None
            assert ci.move_team_copies(conn) == 0
            passed += 1; print("  one-time move to private ok")

            # ---- nothing goes back to Google twice; disconnect forgets the copies
            assert not gcal_sync._wanted(conn, me)   # private events are never pushed anywhere
            ci.forget_user(conn, other)
            assert mine(other) == [] and mine(me)
            passed += 1; print("  no echo; disconnect forgets ok")
        finally:
            conn.close()

        # ---- copy + logs
        js = (ROOT / "static/js/gcal_connect.js").read_text(encoding="utf-8")
        assert "s.reads_calendar" in js and "only you see them" in js and "team calendar" not in js
        isx = (ROOT / "static/js/inbox_schedule.js").read_text(encoding="utf-8")
        assert "!cs.other_calendars" in isx and "Reconnect to see your other calendars" in isx and "data-hint-off" in isx
        src = (ROOT / "gcal_sync.py").read_text(encoding="utf-8")
        assert "calendar_import.forget_user(conn" in src
        logs = "\n".join(trap.lines)
        assert "Calimp" not in logs and "Calimp St" not in logs and "ladder" not in logs
        passed += 1; print("  copy + logs ok")
    finally:
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"smoke_calendar_import: {passed}/10 groups passed")
    return 0 if passed == 10 else 1


if __name__ == "__main__":
    sys.exit(main())
