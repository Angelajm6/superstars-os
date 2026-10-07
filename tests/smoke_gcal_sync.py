#!/usr/bin/env python3
"""Google Calendar sync (gcal_sync.py) — each person's tasks & events in their own Google
Calendar. Guard. Google is FAKED in memory here: no network, no real tokens.

Boots the real app on an ISOLATED database. Proves:
  * switched off unless the OAuth client AND the encryption key are configured (status says
    why; connect 404s)
  * connect: team roles only (a client 403, signed-out -> login); Google is asked ONLY for
    openid + email + calendar.app.created, offline, with a CSRF state cookie; a bad state, a
    refusal, a different / non-company Google account, or a missing calendar grant are each
    refused and store nothing
  * a good connect stores the refresh token ENCRYPTED (never the plaintext), creates the
    "Superstars" calendar and pushes that person's dated items only (not undated ones, not
    other people's): timed task = 30 min, all-day task, timed event, multi-day all-day event,
    the address as the location, done tasks shown with ✓
  * every app change follows within the request: edit -> updated; nothing changed -> no Google
    write; reassigned away -> removed from the old person, added to the new one; done -> ✓;
    deleted -> removed; an event deleted by hand in Google comes back on the next change
  * an expired Google sign-in marks the link needs_reconnect (and stops), shown by status
  * disconnect deletes the Superstars calendar, revokes the sign-in and forgets everything
  * logs carry ids and counts only: no email, token or title
  * both pages carry the connect row
Run:  venv/bin/python tests/smoke_gcal_sync.py
"""
from __future__ import annotations

import io
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
_TMP = tempfile.mkdtemp(prefix="ssc_gcal_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import secret_box  # noqa: E402

CONFIG = {"GOOGLE_OAUTH_CLIENT_ID": "test-client.apps.googleusercontent.com", "GOOGLE_OAUTH_CLIENT_SECRET": "test-secret",
          "GOOGLE_OAUTH_REDIRECT_URI": "https://app.example.test/auth/google/callback",
          "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key()}
SAVED = {k: os.environ.get(k) for k in CONFIG}

import auth  # noqa: E402
import db_layer  # noqa: E402
import gcal_sync  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "gcalsync"
PW = "GcalSyncSmoke2026"
DOMAIN = "superstarscontracting.com"
T = date.today()
D = lambda n: (T + timedelta(days=n)).isoformat()   # noqa: E731
REFRESH = "1//fake-refresh-token-gcalsync"
SECRET_TITLE = "Gcalsync call Zebulon about the hoist"


class FakeGoogle:
    def __init__(self):
        self.calendars, self.calls, self.refresh_ok, self.revoked = {}, [], True, []
        self.next_exchange = None
        self.n = 0

    def http(self, method, url, *, token=None, data=None, json_body=None):
        self.calls.append((method, url))
        if url == gcal_sync.TOKEN_URL:
            if data.get("grant_type") == "authorization_code":
                return self.next_exchange
            if not self.refresh_ok:
                return 400, {"error": "invalid_grant"}
            return 200, {"access_token": "fake-access", "expires_in": 3600}
        if url.startswith(gcal_sync.REVOKE_URL):
            self.revoked.append(data.get("token"))
            return 200, {}
        assert token == "fake-access", "Google calls carry the access token"
        path = url[len(gcal_sync.API):]
        parts = [urllib.parse.unquote(x) for x in path.strip("/").split("/")]
        if parts == ["calendars"] and method == "POST":
            self.n += 1
            cid = f"cal{self.n}@group.calendar.google.com"
            self.calendars[cid] = {"meta": json_body, "events": {}}
            return 200, {"id": cid}
        cid = parts[1]
        if cid not in self.calendars:
            return 404, {}
        cal = self.calendars[cid]
        if len(parts) == 2:
            if method == "GET":
                return 200, {"id": cid}
            if method == "DELETE":
                del self.calendars[cid]
                return 204, {}
        if len(parts) == 3 and method == "POST":
            self.n += 1
            eid = f"ev{self.n}"
            cal["events"][eid] = json_body
            return 200, {"id": eid}
        eid = parts[3]
        if eid not in cal["events"]:
            return 404, {}
        if method == "PUT":
            cal["events"][eid] = json_body
            return 200, {"id": eid}
        if method == "DELETE":
            del cal["events"][eid]
            return 204, {}
        return 400, {}

    def events(self):
        out = {}
        for c in self.calendars.values():
            for eid, b in c["events"].items():
                out[int(b["extendedProperties"]["private"]["ssc_item"])] = b
        return out

    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PUT", "DELETE") and c[1].startswith(gcal_sync.API)]


G = FakeGoogle()
CLAIMS = {}


def _db():
    return db_layer.connect(pragma_fk=False)


def _uids(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]


def _seed() -> dict:
    conn = _db()
    try:
        uids = _uids(conn)
        for t, col in (("google_calendar_event", "user_id"), ("google_calendar_link", "user_id"), ("crm_task", "created_by")):
            purge(conn, t, col, uids)
        conn.commit()
        purge_users(conn, uids)
        ids = {}
        for key, role in (("cs", "c_suite"), ("pm", "pm"), ("super", "super"), ("client", "client")):
            email = f"{TAG}.{key}@{DOMAIN}"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, display_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, ?, 1, 'active', 0, 0)", (email, auth.hash_password(PW), role, f"Gcal {key}", f"Gcal {key}"))
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.commit()
        return ids
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        uids = _uids(conn)
        for t, col in (("google_calendar_event", "user_id"), ("google_calendar_link", "user_id"), ("crm_task", "created_by")):
            purge(conn, t, col, uids)
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    for k, v in SAVED.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(key):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@{DOMAIN}", "password": PW})
    assert r.status_code == 200, (key, r.status_code)
    return c


def _connect(c, email, *, exchange=None, state_ok=True):
    r = c.get("/google-calendar/connect?next=/projects")
    assert r.status_code == 302, r.status_code
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["Location"]).query))
    state = q["state"] if state_ok else "wrong." + q["state"].split(".", 1)[1]
    CLAIMS.clear()
    CLAIMS.update({"email": email, "email_verified": True, "hd": email.rsplit("@", 1)[-1]})
    G.next_exchange = exchange or (200, {"refresh_token": REFRESH, "access_token": "fake-access", "expires_in": 3600, "id_token": "x",
                                         "scope": "openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/calendar.app.created"})
    r = c.get("/google-calendar/callback?" + urllib.parse.urlencode({"state": state, "code": "auth-code"}))
    assert r.status_code == 302, r.status_code
    return r.headers["Location"], q


def _link(uid):
    conn = _db()
    try:
        r = conn.execute("SELECT * FROM google_calendar_link WHERE user_id = ?", (uid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def main() -> int:
    ids = _seed()
    passed = 0
    gcal_sync._http = G.http
    gcal_sync._verify_id_token = lambda tok: dict(CLAIMS)
    gcal_sync.SYNC_INLINE = True
    cs, pm, sup, cl = (_login(k) for k in ("cs", "pm", "super", "client"))

    # ---- off until configured
    for k in CONFIG:
        os.environ.pop(k, None)
    st = pm.get("/api/google-calendar/status").get_json()["data"]
    assert st["available"] is False and st["reason"] == "google_not_configured", st
    assert pm.get("/google-calendar/connect").status_code == 404
    os.environ.update({k: v for k, v in CONFIG.items() if k != "SSC_FIELD_ENCRYPTION_KEY"})
    assert pm.get("/api/google-calendar/status").get_json()["data"]["reason"] == "encryption_key_missing"
    os.environ.update(CONFIG)
    assert pm.get("/api/google-calendar/status").get_json()["data"]["available"] is True
    assert gcal_sync.redirect_uri() == "https://app.example.test/google-calendar/callback"
    passed += 1; print("  switch ok")

    # ---- items, before connecting
    def item(body):
        r = cs.post("/api/team/items", json=body)
        assert r.status_code == 201, r.get_json()
        return r.get_json()["data"]["id"]
    t_timed = item({"title": SECRET_TITLE, "assignee_user_id": ids["pm"], "due_date": D(1), "due_time": "14:00", "address": "890 East 135 Street, Bronx, NY"})
    t_allday = item({"title": "Gcalsync send invoice", "assignee_user_id": ids["pm"], "due_date": D(2)})
    t_nodate = item({"title": "Gcalsync someday", "assignee_user_id": ids["pm"]})
    e_timed = item({"kind": "event", "title": "Gcalsync walkthrough", "assignee_user_id": ids["pm"], "start_date": D(3), "start_time": "09:00", "end_time": "10:30", "location": "Lobby"})
    e_multi = item({"kind": "event", "title": "Gcalsync trade show", "assignee_user_id": ids["pm"], "start_date": D(5), "end_date": D(6)})
    other = item({"title": "Gcalsync not yours", "assignee_user_id": ids["super"], "due_date": D(1)})
    assert G.writes() == [], "nobody is connected yet: Google is never called"

    # ---- connect: gates + refusals
    anon = server.app.test_client().get("/google-calendar/connect")
    assert anon.status_code == 302 and "/login" in anon.headers["Location"]
    r = cl.get("/google-calendar/connect")   # the client gate turns a client away before the route (403 or /welcome)
    assert r.status_code == 403 or (r.status_code == 302 and "accounts.google.com" not in r.headers["Location"]), r.status_code
    loc, q = _connect(pm, f"{TAG}.pm@{DOMAIN}", state_ok=False)
    assert loc.endswith("/projects?gcal=state") and _link(ids["pm"]) is None
    assert q["scope"] == "openid email https://www.googleapis.com/auth/calendar.app.created", q["scope"]
    assert q["access_type"] == "offline" and q["login_hint"] == f"{TAG}.pm@{DOMAIN}" and q["redirect_uri"] == gcal_sync.redirect_uri()
    loc, _ = _connect(pm, f"someone.else@{DOMAIN}")
    assert loc.endswith("gcal=account") and _link(ids["pm"]) is None, "another person's Google account"
    loc, _ = _connect(pm, f"{TAG}.pm@gmail.com")
    assert loc.endswith("gcal=account") and _link(ids["pm"]) is None, "a personal account"
    loc, _ = _connect(pm, f"{TAG}.pm@{DOMAIN}", exchange=(200, {"refresh_token": REFRESH, "id_token": "x", "scope": "openid email"}))
    assert loc.endswith("gcal=scope") and _link(ids["pm"]) is None, "calendar access not granted"
    loc, _ = _connect(pm, f"{TAG}.pm@{DOMAIN}", exchange=(400, {"error": "invalid_grant"}))
    assert loc.endswith("gcal=failed") and _link(ids["pm"]) is None
    passed += 1; print("  refusals ok")

    # ---- a good connect
    buf = io.StringIO(); h = logging.StreamHandler(buf); root = logging.getLogger(); lvl = root.level
    root.setLevel(logging.INFO); root.addHandler(h)
    try:
        loc, _ = _connect(pm, f"{TAG}.pm@{DOMAIN}")
        assert loc.endswith("/projects?gcal=connected"), loc
        link = _link(ids["pm"])
        assert link["status"] == "active" and link["refresh_token_enc"].startswith("v1:") and REFRESH not in link["refresh_token_enc"]
        assert secret_box.decrypt_text(link["refresh_token_enc"]) == REFRESH
        assert len(G.calendars) == 1 and list(G.calendars.values())[0]["meta"]["summary"] == "Superstars"
        ev = G.events()
        assert set(ev) == {t_timed, t_allday, e_timed, e_multi}, ev.keys()
        assert ev[t_timed]["start"] == {"dateTime": f"{D(1)}T14:00:00", "timeZone": "America/New_York"}
        assert ev[t_timed]["end"]["dateTime"] == f"{D(1)}T14:30:00" and ev[t_timed]["summary"] == "☐ " + SECRET_TITLE
        assert ev[t_timed]["location"] == "890 East 135 Street, Bronx, NY" and ev[t_timed]["transparency"] == "transparent"
        assert ev[t_allday]["start"] == {"date": D(2)} and ev[t_allday]["end"] == {"date": D(3)}
        assert ev[e_timed]["start"]["dateTime"] == f"{D(3)}T09:00:00" and ev[e_timed]["end"]["dateTime"] == f"{D(3)}T10:30:00"
        assert ev[e_timed]["summary"] == "Gcalsync walkthrough" and ev[e_timed]["location"] == "Lobby"
        assert ev[e_multi]["start"] == {"date": D(5)} and ev[e_multi]["end"] == {"date": D(7)}
        st = pm.get("/api/google-calendar/status").get_json()["data"]
        assert st["connected"] and st["synced_count"] == 4 and st["google_email"] == f"{TAG}.pm@{DOMAIN}" and st["last_sync_at"]
        passed += 1; print("  connect ok")

        # ---- changes follow
        before = len(G.writes())
        assert pm.post("/api/google-calendar/sync").get_json()["data"]["created"] == 0
        assert len(G.writes()) == before, "nothing changed: no Google write"
        assert cs.put(f"/api/team/items/{t_allday}", json={"title": "Gcalsync send the invoice"}).status_code == 200
        assert G.events()[t_allday]["summary"] == "☐ Gcalsync send the invoice"
        assert pm.post(f"/api/team/items/{t_allday}/done", json={"done": True}).status_code == 200
        assert G.events()[t_allday]["summary"].startswith("✓ ")
        assert cs.put(f"/api/team/items/{e_timed}", json={"assignee_user_id": ids["super"]}).status_code == 200
        assert e_timed not in G.events(), "reassigned away: gone from the pm's calendar (the super is not connected)"
        assert cs.delete(f"/api/team/items/{e_multi}").status_code == 200
        assert e_multi not in G.events()
        cal = list(G.calendars.values())[0]
        gone = [eid for eid, b in cal["events"].items() if b["extendedProperties"]["private"]["ssc_item"] == str(t_timed)][0]
        del cal["events"][gone]   # someone deleted it by hand in Google
        assert cs.put(f"/api/team/items/{t_timed}", json={"due_time": "15:00"}).status_code == 200
        assert G.events()[t_timed]["start"]["dateTime"] == f"{D(1)}T15:00:00", "re-created"
        assert cs.put(f"/api/team/items/{t_nodate}", json={"due_date": D(4)}).status_code == 200 and t_nodate in G.events()
        assert other not in G.events()
        passed += 1; print("  changes ok")

        # ---- the super connects too: reassignment lands with them
        loc, _ = _connect(sup, f"{TAG}.super@{DOMAIN}")
        assert loc.endswith("gcal=connected") and len(G.calendars) == 2
        sup_cal = [c for c in G.calendars.values() if any(b["summary"] == "Gcalsync walkthrough" for b in c["events"].values())]
        assert sup_cal, "the walkthrough is now in the super's own calendar"

        # ---- an expired Google sign-in
        G.refresh_ok = False
        gcal_sync._TOKENS.clear()
        assert cs.put(f"/api/team/items/{t_nodate}", json={"title": "Gcalsync later"}).status_code == 200, "the app write never fails"
        assert _link(ids["pm"])["status"] == "needs_reconnect"
        st = pm.get("/api/google-calendar/status").get_json()["data"]
        assert st["status"] == "needs_reconnect" and st["problem"]
        G.refresh_ok = True
        loc, _ = _connect(pm, f"{TAG}.pm@{DOMAIN}")
        assert loc.endswith("gcal=connected") and _link(ids["pm"])["status"] == "active"
        assert G.events()[t_nodate]["summary"] == "☐ Gcalsync later", "reconnecting catches up"
        passed += 1; print("  reconnect ok")

        # ---- disconnect
        pm_cal = _link(ids["pm"])["calendar_id"]
        assert pm.post("/api/google-calendar/disconnect").status_code == 200
        assert pm_cal not in G.calendars and REFRESH in G.revoked and _link(ids["pm"]) is None
        conn = _db()
        try:
            assert conn.execute("SELECT COUNT(*) FROM google_calendar_event WHERE user_id = ?", (ids["pm"],)).fetchone()[0] == 0
        finally:
            conn.close()
        assert pm.get("/api/google-calendar/status").get_json()["data"]["connected"] is False
        assert pm.post("/api/google-calendar/sync").status_code == 409
        passed += 1; print("  disconnect ok")
    finally:
        root.removeHandler(h); root.setLevel(lvl)
    out = buf.getvalue()
    for secret in (REFRESH, "fake-access", f"{TAG}.pm@", "Zebulon", "Gcalsync send"):
        assert secret not in out, ("log leak", secret)
    assert "gcal sync user=" in out and "gcal connected user=" in out
    passed += 1; print("  logs ok")

    # ---- pages
    for page, path in (("projects.html", "/projects"), ("company_portal.html", "/company")):
        src = (ROOT / page).read_text(encoding="utf-8")
        assert "<div data-gcal hidden></div>" in src and 'src="/files/static/js/gcal_connect.js?v=' in src, page
    js = (ROOT / "static" / "js" / "gcal_connect.js").read_text(encoding="utf-8")
    for needle in ("/api/google-calendar/status", "/api/google-calendar/sync", "/api/google-calendar/disconnect",
                   "/google-calendar/connect?next=", "Connect Google Calendar", "Reconnect"):
        assert needle in js, needle
    passed += 1; print("  pages ok")

    print(f"PASS smoke_gcal_sync ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_gcal_sync: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
