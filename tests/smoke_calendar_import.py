#!/usr/bin/env python3
"""Google Calendar -> the software (accepted 2026-10-15): the BEHAVIORAL guard for calendar_import.py.

Proves against an ISOLATED database (no Google, no model call):
  * times: a timed event lands in New York time; an all-day event's exclusive end date is handled
  * work vs personal: a client attendee, a job address or an estimate address makes it work with
    no model call; otherwise the (planted) sort decides; personal events never become items and
    their text is never stored; without Claude only rule matches come in; over the daily limit
    an unmatched event waits ('later') and is read again next time
  * a work event becomes a team event assigned to the person, marked 'from Google', linked to the
    job it matched; an edit in Google updates it; a cancelled or declined event cancels it; an
    unchanged event is skipped; out-of-office / birthdays are skipped
  * pulling: the first read asks for the coming window, later reads ask only for changes; a
    'gone' answer (410) starts over; a connection without the read permission is skipped
  * imported events never go back to the person's Superstars calendar (no duplicates in Google)
  * the connection asks for calendar.events.readonly and reports whether it reads; pages show
    "from Google" and the reconnect prompt; logs carry ids / counts only

Database: honors SSC_DB_URL like every gate suite; otherwise a throwaway SQLite snapshot.
Run:  venv/bin/python tests/smoke_calendar_import.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import json
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
for _k in ("ANTHROPIC_API_KEY", "SSC_ASSISTANT_FAKE", "SSC_CALENDAR_SORT_FAKE", "SSC_ASSISTANT_DAILY_CAP_USD", "SSC_TIMEZONE"):
    os.environ.pop(_k, None)

import assistant  # noqa: E402,F401
import calendar_import as ci  # noqa: E402
import claude_agent as ca  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import gcal_sync  # noqa: E402
import team_items  # noqa: E402

TAG = "calimp"
ORG = "Calimp Smoke Owners"
CLIENT = "board.calimp@calimp-owners.example"
JOB = "CALIMP-1"
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
        ca.ensure(conn)
        ci.ensure(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        for t in ("gcal_import", "google_calendar_event", "google_calendar_link"):
            purge(conn, t, "user_id", uids)
        chats = [r["id"] for r in conn.execute(f"SELECT id FROM claude_chat WHERE user_id IN ({','.join('?' * len(uids)) or 'NULL'})", uids).fetchall()]
        purge(conn, "claude_chat_message", "chat_id", chats)
        purge(conn, "claude_chat", "id", chats)
        conn.execute("DELETE FROM crm_task WHERE title LIKE 'Calimp%'")
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name = ?", (ORG,)).fetchall()]
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({','.join('?' * len(orgs)) or 'NULL'})", orgs).fetchall()]
        purge(conn, "estimate", "id", ests)
        conn.execute("DELETE FROM building WHERE address_key LIKE '%calimp%'")
        conn.execute("DELETE FROM projects WHERE project_code = ?", (JOB,))
        if orgs:
            ph = ",".join("?" * len(orgs))
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph})", orgs)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph})", orgs)
        conn.execute("DELETE FROM crm_organization WHERE name = ?", (ORG,))
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
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                     "VALUES (?, 'x', 'admin', 'Calimp Admin', 1, 'active', 0, 0)", (f"{TAG}.admin@superstarscontracting.com",))
        conn.commit()
        ids["me"] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.admin@superstarscontracting.com",)).fetchone()["id"]
        ids["org"] = crm.create_org(conn, name=ORG, relationship_type="client")
        crm.create_contact(conn, full_name="Calimp Board", org_id=ids["org"], email=CLIENT)
        estimates.create_estimate(conn, est_type="FR", borough="MN", client_org_id=ids["org"], building_address="77 Calimpton Ave",
                                  created_by=ids["me"])
        conn.execute("INSERT INTO projects (project_code, name, address) VALUES (?, ?, ?)", (JOB, "Calimp job", "455 Calimp Street, New York"))
        conn.execute("INSERT INTO google_calendar_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) "
                     "VALUES (?, ?, ?, 'active', ?, ?)", (ids["me"], f"{TAG}.admin@superstarscontracting.com", secret_box.encrypt_text("r"),
                                                          "2026-10-15T08:00:00", gcal_sync.SCOPES))
        conn.commit()
    finally:
        conn.close()
    return ids


def ev(gid, title, *, start=None, end=None, allday=None, location="", attendees=(), updated="2026-10-15T10:00:00.000Z",
       status="confirmed", event_type="default", desc=""):
    e = {"id": gid, "summary": title, "location": location, "description": desc, "status": status, "updated": updated,
         "eventType": event_type, "attendees": [{"email": a, "responseStatus": "accepted"} for a in attendees]}
    if allday:
        e.update(start={"date": allday[0]}, end={"date": allday[1]})
    else:
        e.update(start={"dateTime": start or f"{DAY}T14:00:00Z"}, end={"dateTime": end or f"{DAY}T15:30:00Z"})
    return e


def main() -> int:
    trap = LogTrap()
    lg = logging.getLogger("ssc.calendar_import")
    lg.addHandler(trap)
    lg.setLevel(logging.INFO)
    ids = _seed()
    me, dom = ids["me"], "superstarscontracting.com"
    passed = 0
    try:
        # ---- times
        w = ci.when(ev("t", "x", start=f"{DAY}T14:00:00Z", end=f"{DAY}T15:30:00Z"))
        assert w["start_date"] == DAY and w["start_time"] in ("10:00", "09:00") and w["end_time"] in ("11:30", "10:30") and w["end_date"] is None
        w = ci.when(ev("t", "x", allday=(DAY, (date.fromisoformat(DAY) + timedelta(days=1)).isoformat())))
        assert w == {"start_date": DAY, "start_time": None, "end_date": None, "end_time": None}
        three = (date.fromisoformat(DAY) + timedelta(days=3)).isoformat()
        assert ci.when(ev("t", "x", allday=(DAY, three)))["end_date"] == (date.fromisoformat(DAY) + timedelta(days=2)).isoformat()
        passed += 1; print("  times ok")

        conn = _db()
        try:
            # ---- rules: work without any model call (no key, no fake)
            assert ci.apply_event(conn, me, ev("e1", "Calimp walkthrough with the board", attendees=(CLIENT, f"{TAG}.admin@superstarscontracting.com")), dom) == "created"
            assert ci.apply_event(conn, me, ev("e2", "Calimp site check", location="455 Calimp St, New York, NY"), dom) == "created"
            assert ci.apply_event(conn, me, ev("e3", "Calimp measure for estimate", location="77 Calimpton Avenue"), dom) == "created"
            assert ci.apply_event(conn, me, ev("e4", "Calimp dentist"), dom) == "personal"            # no Claude: unmatched stays out
            items = _q("SELECT * FROM crm_task WHERE title LIKE 'Calimp%' ORDER BY id")
            assert [i["title"] for i in items] == ["Calimp walkthrough with the board", "Calimp site check", "Calimp measure for estimate"]
            assert all(i["source"] == "gcal" and i["assignee_user_id"] == me and i["kind"] == "event" and i["start_date"] == DAY for i in items)
            assert items[1]["project_code"] == JOB and items[1]["address"] == "455 Calimp St, New York, NY"
            assert items[0]["detail"].startswith("From Google Calendar.")
            assert _q("SELECT decision, item_id FROM gcal_import WHERE google_event_id = 'e4'") == [{"decision": "personal", "item_id": None}]
            passed += 1; print("  rules ok (client attendee, job address, estimate address; personal stays out)")

            # ---- the planted sort decides the rest; the limit makes it wait
            os.environ["SSC_CALENDAR_SORT_FAKE"] = json.dumps({"work": True})
            os.environ["SSC_ASSISTANT_FAKE"] = "1"
            assert ci.apply_event(conn, me, ev("e5", "Calimp team meeting about bids"), dom) == "created"
            os.environ["SSC_CALENDAR_SORT_FAKE"] = json.dumps({"work": False})
            assert ci.apply_event(conn, me, ev("e6", "Calimp gym"), dom) == "personal"
            assert ci.apply_event(conn, me, ev("e6", "Calimp gym"), dom) == "unchanged"
            os.environ["SSC_ASSISTANT_DAILY_CAP_USD"] = "0.0000001"
            assert ci.apply_event(conn, me, ev("e7", "Calimp something"), dom) == "later"
            assert ci.apply_event(conn, me, ev("e8", "Calimp client call", attendees=(CLIENT,)), dom) == "created"   # rules still work
            os.environ.pop("SSC_ASSISTANT_DAILY_CAP_USD")
            os.environ["SSC_CALENDAR_SORT_FAKE"] = json.dumps({"work": True})
            assert ci.apply_event(conn, me, ev("e7", "Calimp something"), dom) == "created"   # read again next time
            assert _q("SELECT COUNT(*) AS n FROM claude_chat_message m JOIN claude_chat c ON c.id = m.chat_id "
                      "WHERE c.user_id = ? AND c.source = 'calendar' AND m.kind = 'triage'", (me,))[0]["n"] == 3
            passed += 1; print("  sorting + daily limit ok")

            # ---- edits, cancels, declines, skips
            e1_item = _q("SELECT item_id FROM gcal_import WHERE google_event_id = 'e1'")[0]["item_id"]
            assert ci.apply_event(conn, me, ev("e1", "Calimp walkthrough moved", start=f"{DAY}T16:00:00Z", end=f"{DAY}T17:00:00Z",
                                               attendees=(CLIENT,), updated="2026-10-15T11:00:00.000Z"), dom) == "updated"
            it = team_items.get_item(conn, e1_item)
            assert it["title"] == "Calimp walkthrough moved" and it["start_time"] in ("12:00", "11:00")
            assert ci.apply_event(conn, me, ev("e1", "Calimp walkthrough moved", status="cancelled", updated="2026-10-15T12:00:00.000Z"), dom) == "cancelled"
            assert team_items.get_item(conn, e1_item)["status"] == "cancelled"
            declined = ev("e2", "Calimp site check", location="455 Calimp St", updated="2026-10-15T12:00:00.000Z")
            declined["attendees"] = [{"email": f"{TAG}.admin@superstarscontracting.com", "self": True, "responseStatus": "declined"}]
            assert ci.apply_event(conn, me, declined, dom) == "cancelled"
            assert ci.apply_event(conn, me, ev("e9", "Calimp vacation", event_type="outOfOffice"), dom) == "skipped"
            assert ci.apply_event(conn, me, ev("e10", "Calimp birthday", event_type="birthday"), dom) == "skipped"
            passed += 1; print("  edits, cancels, declines, skips ok")

            # ---- pulling through a fake Google
            seen = []

            def fake_get(conn_, link, params):
                seen.append(dict(params))
                if len(seen) == 3:
                    return 410, {}
                return 200, {"items": [ev("e20", "Calimp inspection", attendees=(CLIENT,), updated=f"2026-10-15T1{len(seen)}:00:00.000Z")]}
            real = ci._get
            ci._get = fake_get
            try:
                r1 = ci.import_user(conn, me)
                r2 = ci.import_user(conn, me)
                r3 = ci.import_user(conn, me)
            finally:
                ci._get = real
            assert r1 == {"created": 1} and r2 == {"updated": 1} and r3 == {"reset": True}
            assert "timeMax" in seen[0] and "updatedMin" not in seen[0] and "updatedMin" in seen[1] and "timeMax" not in seen[1]
            assert _q("SELECT import_at FROM google_calendar_link WHERE user_id = ?", (me,))[0]["import_at"] is None   # starts over
            conn.execute("UPDATE google_calendar_link SET scopes = 'openid email https://www.googleapis.com/auth/calendar.app.created' WHERE user_id = ?", (me,))
            conn.commit()
            assert ci.import_user(conn, me) == {"skipped": True}
            passed += 1; print("  pulling ok (window, changes, 410, needs the read permission)")

            # ---- nothing goes back to Google twice
            conn.execute("INSERT INTO crm_task (entity_type, title, assignee_user_id, status, priority, created_by, created_at, kind, "
                         "start_date, due_date) VALUES ('none', 'Calimp app-made event', ?, 'open', 'normal', ?, ?, 'event', ?, ?)",
                         (me, me, "2026-10-15T09:00:00", DAY, DAY))
            conn.commit()
            wanted = gcal_sync._wanted(conn, me)
            gcal_ids = {r["id"] for r in _q("SELECT id FROM crm_task WHERE source = 'gcal' AND assignee_user_id = ?", (me,))}
            app_id = _q("SELECT id FROM crm_task WHERE title = 'Calimp app-made event'")[0]["id"]
            assert app_id in wanted and not (gcal_ids & set(wanted))
            passed += 1; print("  no echo back to Google ok")

            # ---- connection, status, pages, logs
            assert ci.READ_SCOPE in gcal_sync.SCOPES and "calendar.app.created" in gcal_sync.SCOPES
            st = gcal_sync.status_for(conn, me)
            assert st["connected"] and st["reads_calendar"] is False
        finally:
            conn.close()
        js = (ROOT / "static/js/gcal_connect.js").read_text(encoding="utf-8")
        assert "s.reads_calendar" in js and "Reconnect once so work events" in js
        assert (ROOT / "company_portal.html").read_text(encoding="utf-8").count("from Google") >= 2
        logs = "\n".join(trap.lines)
        assert "Calimp" not in logs and CLIENT not in logs and "Calimp St" not in logs
        passed += 1; print("  connection, pages, logs ok")
    finally:
        for k in ("SSC_CALENDAR_SORT_FAKE", "SSC_ASSISTANT_FAKE", "SSC_ASSISTANT_DAILY_CAP_USD"):
            os.environ.pop(k, None)
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"smoke_calendar_import: {passed}/7 groups passed")
    return 0 if passed == 7 else 1


if __name__ == "__main__":
    sys.exit(main())
