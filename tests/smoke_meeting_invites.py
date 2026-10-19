#!/usr/bin/env python3
"""Meeting invitations + Google Meet through Google Calendar (meeting_invites.py). Guard.

Google is FAKED in memory: no network, no real tokens. Boots the real app on an ISOLATED database
and proves, through the HTTP routes:
  * a meeting saved by someone with a working Google Calendar becomes ONE Google event on THEIR
    Superstars calendar, with the assignee, the team people in it and the outside guests as guests
    (never the organizer), sendUpdates=all, the shared ssc_item mark, and no per-person copies
  * video: a Google Meet createRequest goes with conferenceDataVersion=1; the link comes back on the
    meeting; switching video off removes it; a calendar that refuses video still sends the
    invitation, without video, and the meeting says so
  * "Don't send" = sendUpdates=none; an edit PATCHes (Google emails the update); nothing changed =
    no Google call; delete, or turning it into an event, DELETEs it with sendUpdates=all
  * the guests' Yes / No / Maybe come back and show on the meeting
  * an organizer without Google Calendar: saved for the team, nobody emailed, no Google call
  * guests: bad addresses / too many -> 400; pm cannot create meetings (403); the Inbox and team
    list carry the Meet link and answers only to the team
  * each guest's own calendar import skips the invitation (no double in Mine)
  * logs carry ids and counts only: no title, no guest address
Run:  venv/bin/python tests/smoke_meeting_invites.py
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_meetinv_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")
os.environ["SSC_GMAIL_POLL"] = "0"

import secret_box  # noqa: E402

os.environ.update({"GOOGLE_OAUTH_CLIENT_ID": "test-client.apps.googleusercontent.com", "GOOGLE_OAUTH_CLIENT_SECRET": "test-secret",
                   "GOOGLE_OAUTH_REDIRECT_URI": "https://app.example.test/auth/google/callback",
                   "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key()})

import auth  # noqa: E402
import calendar_import as ci  # noqa: E402
import db_layer  # noqa: E402
import gcal_sync  # noqa: E402
import meeting_invites as mi  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "meetinv"
PW = "MeetInvSmoke2026"
T = date.today()
D = lambda n: (T + timedelta(days=n)).isoformat()   # noqa: E731
TITLE = "Meetinv board walk at the secret parapet"
OUTSIDE = "board.chair@riverside-owners.example"


class FakeGoogle:
    def __init__(self):
        self.calendars, self.calls, self.n, self.refuse_video = {}, [], 0, False
        self.answers = {}

    def http(self, method, url, *, token=None, data=None, json_body=None):
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((method, u.path, q, json_body))
        if url == gcal_sync.TOKEN_URL:
            return 200, {"access_token": "fake-access", "expires_in": 3600}
        parts = [urllib.parse.unquote(x) for x in u.path.split("/calendar/v3/")[1].split("/")]
        if parts == ["calendars"] and method == "POST":
            self.n += 1
            cid = f"cal{self.n}@group.calendar.google.com"
            self.calendars[cid] = {}
            return 200, {"id": cid}
        cid = parts[1]
        if cid not in self.calendars:
            return 404, {}
        evs = self.calendars[cid]
        if len(parts) == 2:
            return 200, {"id": cid}
        if len(parts) == 3 and method == "POST":
            body = dict(json_body)
            if "conferenceData" in body:
                if self.refuse_video:
                    return 400, {"error": {"message": "Invalid conference type value."}}
                assert q.get("conferenceDataVersion") == "1"
                body["conferenceData"] = {"createRequest": {"status": {"statusCode": "success"}},
                                          "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}]}
                body["hangoutLink"] = "https://meet.google.com/abc-defg-hij"
            self.n += 1
            eid = f"ev{self.n}"
            evs[eid] = body
            return 200, self._out(eid, body)
        eid = parts[3]
        if eid not in evs:
            return 404, {}
        if method == "PATCH":
            body = dict(evs[eid])
            for k, v in json_body.items():
                if k == "conferenceData" and v is None:
                    body.pop("conferenceData", None)
                    body.pop("hangoutLink", None)
                elif k == "conferenceData":
                    if self.refuse_video:
                        return 400, {}
                    body["conferenceData"] = {"entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/zzz-yyyy-xxx"}]}
                    body["hangoutLink"] = "https://meet.google.com/zzz-yyyy-xxx"
                else:
                    body[k] = v
            evs[eid] = body
            return 200, self._out(eid, body)
        if method == "GET":
            return 200, self._out(eid, evs[eid])
        if method == "DELETE":
            del evs[eid]
            return 204, {}
        return 400, {}

    def _out(self, eid, body):
        out = dict(body, id=eid)
        out["attendees"] = [dict(a, responseStatus=self.answers.get(a["email"], "needsAction")) for a in body.get("attendees") or []]
        return out

    def events(self):
        return {eid: ev for cal in self.calendars.values() for eid, ev in cal.items()}

    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PATCH", "PUT", "DELETE") and "/events" in c[1]]


class LogTrap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _db():
    return db_layer.connect(pragma_fk=False)


def _uids(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]


def _purge(conn):
    uids = _uids(conn)
    tasks = [r["id"] for r in conn.execute("SELECT id FROM crm_task WHERE title LIKE 'Meetinv%'").fetchall()]
    purge(conn, "meeting_invite", "item_id", tasks)
    purge(conn, "crm_task_attendee", "task_id", tasks)
    purge(conn, "crm_task", "id", tasks)
    for t in ("google_calendar_event", "google_calendar_link", "user_calendar_event"):
        purge(conn, t, "user_id", uids)
    conn.commit()
    purge_users(conn, uids)
    conn.commit()


def _seed() -> dict:
    conn = _db()
    try:
        ci.ensure(conn)
        _purge(conn)
        ids = {}
        for key, role in (("admin", "admin"), ("cs", "c_suite"), ("pm", "pm"), ("super", "super")):
            email = f"{TAG}.{key}@superstarscontracting.com"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, display_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, ?, 1, 'active', 0, 0)", (email, auth.hash_password(PW), role, f"Meetinv {key}", f"Meetinv {key}"))
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        for key in ("admin", "pm"):   # admin and pm have Google Calendar; c_suite does not
            conn.execute("INSERT INTO google_calendar_link (user_id, google_email, refresh_token_enc, status, connected_at, scopes) "
                         "VALUES (?, ?, ?, 'active', ?, ?)", (ids[key], f"{TAG}.{key}@superstarscontracting.com",
                                                              secret_box.encrypt_text("r"), "2026-10-18T08:00:00", gcal_sync.SCOPES))
        conn.commit()
        return ids
    finally:
        conn.close()


def _login(key):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@superstarscontracting.com", "password": PW})
    assert r.status_code == 200, (key, r.status_code)
    return c


def _item(c, item_id):
    r = c.get("/api/team/items?status=all")
    assert r.status_code == 200
    return next((i for i in r.get_json()["data"] if i["id"] == item_id), None)


def main() -> int:
    trap = LogTrap()
    for name in ("ssc.meeting_invites", "ssc.gcal", "ssc.team_items", "ssc.calendar_import"):
        logging.getLogger(name).addHandler(trap)
        logging.getLogger(name).setLevel(logging.DEBUG)
    ids = _seed()
    fake = FakeGoogle()
    real_http = gcal_sync._http
    gcal_sync._http = fake.http
    gcal_sync._TOKENS.clear()
    gcal_sync.SYNC_INLINE = True
    passed = 0
    try:
        admin, cs, pm = _login("admin"), _login("cs"), _login("pm")
        email = lambda k: f"{TAG}.{k}@superstarscontracting.com"   # noqa: E731

        # ---- create: one Google event with every guest, invitations sent, Meet made
        r = admin.post("/api/team/items", json={"kind": "meeting", "title": TITLE, "assignee_user_id": ids["pm"], "start_date": D(2),
                                                 "start_time": "10:00", "end_time": "11:00", "attendee_user_ids": [ids["super"], ids["admin"]],
                                                 "guest_emails": [OUTSIDE.upper(), OUTSIDE], "video": True})
        assert r.status_code == 201, r.get_data(as_text=True)[:300]
        mid = r.get_json()["data"]["id"]
        evs = {eid: ev for eid, ev in fake.events().items() if ev.get("extendedProperties", {}).get("shared", {}).get("ssc_item") == str(mid)}
        assert len(evs) == 1, evs
        ev = next(iter(evs.values()))
        assert sorted(a["email"] for a in ev["attendees"]) == sorted([email("pm"), email("super"), OUTSIDE])   # not the organizer
        post = next(c for c in fake.writes() if c[0] == "POST")
        assert post[2] == {"conferenceDataVersion": "1", "sendUpdates": "all"}
        assert post[3]["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"] == "hangoutsMeet"
        assert ev["summary"] == TITLE and ev["start"]["dateTime"].startswith(D(2) + "T10:00")
        # no per-person copies of this meeting anywhere (the invitation is everyone's copy)
        assert not [e for e in fake.events().values() if e.get("extendedProperties", {}).get("private", {}).get("ssc_item") == str(mid)
                    and "shared" not in e.get("extendedProperties", {})]
        it = _item(admin, mid)
        assert it["invites"] and it["video"] and it["meet_url"] == "https://meet.google.com/abc-defg-hij" and it["guest_emails"] == [OUTSIDE]
        cal = db_layer.connect()
        try:
            owner_cal = gcal_sync._link(cal, ids["admin"])["calendar_id"]
        finally:
            cal.close()
        assert owner_cal in fake.calendars and next(iter(evs)) in fake.calendars[owner_cal]
        passed += 1; print("  create ok (one event, guests, sendUpdates=all, Meet link back)")

        # ---- edits: nothing changed = no call; change = PATCH with the update email; Don't send = none
        n = len(fake.writes())
        assert admin.put(f"/api/team/items/{mid}", json={}).status_code == 200
        assert len(fake.writes()) == n
        assert admin.put(f"/api/team/items/{mid}", json={"start_time": "13:00", "end_time": "14:00"}).status_code == 200
        w = fake.writes()[-1]
        assert w[0] == "PATCH" and w[2]["sendUpdates"] == "all" and w[3]["start"]["dateTime"].startswith(D(2) + "T13:00")
        assert "conferenceData" not in w[3]   # the existing Meet link is kept
        assert admin.put(f"/api/team/items/{mid}", json={"detail": "bring the drawings", "send_invites": False}).status_code == 200
        assert fake.writes()[-1][2]["sendUpdates"] == "none"
        assert admin.put(f"/api/team/items/{mid}", json={"attendee_user_ids": [], "guest_emails": [], "send_invites": True}).status_code == 200
        assert [a["email"] for a in fake.writes()[-1][3]["attendees"]] == [email("pm")]
        passed += 1; print("  edits ok (no-op skipped, PATCH sends updates, Don't send = none, guests removed)")

        # ---- video off / on again, and a calendar that refuses video
        assert admin.put(f"/api/team/items/{mid}", json={"video": False}).status_code == 200
        assert fake.writes()[-1][3]["conferenceData"] is None and _item(admin, mid)["meet_url"] is None
        assert admin.put(f"/api/team/items/{mid}", json={"video": True}).status_code == 200
        assert _item(admin, mid)["meet_url"] == "https://meet.google.com/zzz-yyyy-xxx"
        fake.refuse_video = True
        r = admin.post("/api/team/items", json={"kind": "meeting", "title": "Meetinv no video calendar", "start_date": D(3),
                                                 "attendee_user_ids": [ids["super"]], "video": True})
        nv = r.get_json()["data"]["id"]
        it = _item(admin, nv)
        assert it["invites"] and it["meet_url"] is None and it["invite_problem"] == "Google did not add a video link to this meeting"
        assert [a["email"] for a in fake.events()[[k for k, e in fake.events().items() if e["summary"] == "Meetinv no video calendar"][0]]["attendees"]] == [email("super")]
        fake.refuse_video = False
        passed += 1; print("  video ok (off removes the link, on makes a new one, refused = invitation without video)")

        # ---- answers come back
        fake.answers = {email("pm"): "accepted"}
        conn = db_layer.connect()
        try:
            conn.execute("UPDATE meeting_invite SET answers_at = NULL")
            conn.commit()
            assert mi.refresh_answers(conn, ids["admin"]) >= 1
        finally:
            conn.close()
        assert _item(pm, mid)["guest_answers"] == {email("pm"): "accepted"}
        passed += 1; print("  answers ok")

        # ---- turned into an event / deleted -> Google cancels it for the guests
        assert admin.put(f"/api/team/items/{nv}", json={"kind": "event"}).status_code == 200
        w = fake.writes()[-1]
        assert w[0] == "DELETE" and w[2]["sendUpdates"] == "all"
        assert admin.delete(f"/api/team/items/{mid}").status_code == 200
        assert fake.writes()[-1][0] == "DELETE" and fake.writes()[-1][2]["sendUpdates"] == "all"
        assert not [e for e in fake.events().values() if e.get("extendedProperties", {}).get("shared")]
        passed += 1; print("  cancellations ok (to event, deleted)")

        # ---- no Google Calendar = saved for the team, nobody emailed
        n = len(fake.writes())
        r = cs.post("/api/team/items", json={"kind": "meeting", "title": "Meetinv by c-suite", "start_date": D(4),
                                              "attendee_user_ids": [ids["super"]], "guest_emails": [OUTSIDE], "video": True})
        assert r.status_code == 201
        it = _item(cs, r.get_json()["data"]["id"])
        assert not it["invites"] and it["meet_url"] is None
        assert not [c for c in fake.writes()[n:] if c[3] and c[3].get("attendees")]
        passed += 1; print("  no Google Calendar ok (team only, no email)")

        # ---- validation and roles
        for bad in (["not-an-email"], "everyone", [f"g{i}@x.example" for i in range(51)], ["a b@x.example"]):
            assert admin.post("/api/team/items", json={"kind": "meeting", "title": "Meetinv bad", "start_date": D(1),
                                                        "guest_emails": bad}).status_code == 400, bad
        assert pm.post("/api/team/items", json={"kind": "meeting", "title": "Meetinv pm", "start_date": D(1)}).status_code == 403
        r = admin.post("/api/team/items", json={"kind": "task", "title": "Meetinv task", "guest_emails": [OUTSIDE], "video": True})
        tid = r.get_json()["data"]["id"]
        it = _item(admin, tid)
        assert it["guest_emails"] == [] and not it["video"] and not it["invites"]   # only meetings have guests / video
        passed += 1; print("  validation + roles ok")

        # ---- a guest's own calendar import skips the invitation
        conn = db_layer.connect()
        try:
            got = ci.apply_event(conn, ids["super"], {"id": "inv1", "summary": TITLE, "status": "confirmed", "updated": "u",
                                                     "start": {"date": D(2)}, "end": {"date": D(3)},
                                                     "extendedProperties": {"shared": {"ssc_item": "123"}}})
            assert got == "skipped"
            assert not conn.execute("SELECT 1 FROM user_calendar_event WHERE user_id = ?", (ids["super"],)).fetchone()
        finally:
            conn.close()
        passed += 1; print("  no double in a guest's Mine ok")

        logs = "\n".join(trap.lines)
        assert "parapet" not in logs and OUTSIDE not in logs and "riverside" not in logs.lower()
        passed += 1; print("  logs ok")
    finally:
        gcal_sync._http = real_http
        gcal_sync.SYNC_INLINE = False
        conn = _db()
        try:
            _purge(conn)
        finally:
            conn.close()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"smoke_meeting_invites: {passed}/9 groups passed")
    return 0 if passed == 9 else 1


if __name__ == "__main__":
    sys.exit(main())
