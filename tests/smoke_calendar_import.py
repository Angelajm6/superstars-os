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
        for t in ("user_calendar_event", "gcal_import", "google_calendar_event", "google_calendar_link"):
            purge(conn, t, "user_id", uids)
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

            def fake_get(conn_, link, params):
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
                assert len(calls) == 2
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
        src = (ROOT / "gcal_sync.py").read_text(encoding="utf-8")
        assert "calendar_import.forget_user(conn" in src
        logs = "\n".join(trap.lines)
        assert "Calimp" not in logs and "Calimp St" not in logs and "ladder" not in logs
        passed += 1; print("  copy + logs ok")
    finally:
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"smoke_calendar_import: {passed}/8 groups passed")
    return 0 if passed == 8 else 1


if __name__ == "__main__":
    sys.exit(main())
