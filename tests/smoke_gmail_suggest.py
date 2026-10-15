#!/usr/bin/env python3
"""Gmail suggestions (gmail_suggest.py) — to-dos spotted in each person's email. Guard.
Google AND the AI are faked here: no network, no real tokens, no model calls.

Boots the real app on an ISOLATED database. Proves:
  * off unless the OAuth client + encryption key are configured; connect asks ONLY for
    openid, email and gmail.readonly (read-only), offline, with a CSRF state; another person's
    or a personal Google account, or no Gmail grant, is refused and stores nothing
  * a check reads only the Primary-inbox query, skips no-reply senders, bulk mail
    (List-Unsubscribe / Precedence) and the person's own mail WITHOUT spending an AI call, and
    turns what the AI calls a to-do into a suggestion (non-to-dos are only remembered by id)
  * the AI's answer is re-validated (bad date / time dropped, an event without a date becomes
    a task, long titles cut); the daily cap, a missing key and an AI error all fall back to the
    keyword rules (which read "tomorrow", weekdays, "3pm", street addresses)
  * the same email is never suggested twice; sender, subject, excerpt and proposal are stored
    ENCRYPTED, the full body never
  * suggestions are private: another person cannot list, add or dismiss them
  * "Add to my to-do" (as a PM) creates a task / event ASSIGNED TO THEM with a link back to the
    email, once; edits before adding are honoured; dismiss + undo work
  * an expired Google sign-in -> needs_reconnect; disconnect revokes and forgets everything
  * logs carry ids and counts only; both pages carry the card
Run:  venv/bin/python tests/smoke_gmail_suggest.py
"""
from __future__ import annotations

import base64
import io
import logging
import os
import shutil
import sys
import tempfile
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_gmail_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")
os.environ["SSC_GMAIL_POLL"] = "0"   # no background poller in a test

import secret_box  # noqa: E402

CONFIG = {"GOOGLE_OAUTH_CLIENT_ID": "test-client.apps.googleusercontent.com", "GOOGLE_OAUTH_CLIENT_SECRET": "test-secret",
          "GOOGLE_OAUTH_REDIRECT_URI": "https://app.example.test/auth/google/callback",
          "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(), "ANTHROPIC_API_KEY": "test-not-a-real-key", "GMAIL_AI_DAILY_CAP": "50"}
SAVED = {k: os.environ.get(k) for k in CONFIG}

import auth  # noqa: E402
import db_layer  # noqa: E402
import gcal_sync  # noqa: E402
import gmail_suggest  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "gmailsug"
PW = "GmailSuggestSmoke2026"
DOMAIN = "superstarscontracting.com"
T = date.today()
REFRESH = "1//fake-gmail-refresh"
SECRET_SUBJECT = "Pay app 4 for Quillfeather Towers"


def b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def msg(mid, sender, subject, body, *, extra_headers=None, labels=("INBOX", "CATEGORY_PERSONAL")):
    hs = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}] + [{"name": k, "value": v} for k, v in (extra_headers or {}).items()]
    return {"id": mid, "threadId": "t" + mid, "internalDate": str(int(datetime.now().timestamp() * 1000)), "labelIds": list(labels),
            "snippet": body[:80], "payload": {"mimeType": "multipart/alternative", "headers": hs,
                                               "parts": [{"mimeType": "text/plain", "body": {"data": b64(body + "\n\nOn Mon someone wrote:\n> old quoted text")}}]}}


class FakeGoogle:
    def __init__(self):
        self.mail, self.queries, self.revoked, self.refresh_ok, self.next_exchange = {}, [], [], True, None

    def http(self, method, url, *, token=None, data=None, json_body=None):
        if url == gcal_sync.TOKEN_URL:
            if data.get("grant_type") == "authorization_code":
                return self.next_exchange
            return (200, {"access_token": "fake-access", "expires_in": 3600}) if self.refresh_ok else (400, {"error": "invalid_grant"})
        if url.startswith(gcal_sync.REVOKE_URL):
            self.revoked.append(data.get("token"))
            return 200, {}
        assert token == "fake-access"
        if url.startswith(gmail_suggest.GMAIL + "/messages?"):
            self.queries.append(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)))
            return 200, {"messages": [{"id": k} for k in self.mail]}
        mid = urllib.parse.unquote(url.split("/messages/")[1].split("?")[0])
        return (200, self.mail[mid]) if mid in self.mail else (404, {})


G = FakeGoogle()
CLAIMS = {}
AI_CALLS = []
AI_ANSWERS = {}


def fake_ai(subject, sender, received, text, today):
    AI_CALLS.append(subject)
    if subject in AI_ANSWERS:
        a = AI_ANSWERS[subject]
        if isinstance(a, Exception):
            raise a
        return a
    return {"is_todo": False, "kind": "task", "title": "", "date": "", "time": "", "address": "", "why": ""}


def _db():
    return db_layer.connect(pragma_fk=False)


def _uids(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]


def _wipe(conn, uids):
    for t, col in (("gmail_suggestion", "user_id"), ("gmail_seen", "user_id"), ("gmail_link", "user_id"),
                   ("google_calendar_event", "user_id"), ("google_calendar_link", "user_id"), ("crm_task", "created_by")):
        purge(conn, t, col, uids)
    conn.execute("DELETE FROM gmail_ai_usage")
    conn.commit()


def _seed() -> dict:
    conn = _db()
    try:
        uids = _uids(conn)
        _wipe(conn, uids)
        purge_users(conn, uids)
        ids = {}
        for key, role in (("cs", "c_suite"), ("pm", "pm"), ("pm2", "pm"), ("client", "client")):
            email = f"{TAG}.{key}@{DOMAIN}"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, display_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, ?, 1, 'active', 0, 0)", (email, auth.hash_password(PW), role, f"Gmail {key}", f"Gmail {key}"))
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.commit()
        return ids
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        uids = _uids(conn)
        _wipe(conn, uids)
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


def _connect(c, email, *, exchange=None):
    r = c.get("/gmail/connect?next=/projects")
    assert r.status_code == 302, r.status_code
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["Location"]).query))
    CLAIMS.clear(); CLAIMS.update({"email": email, "email_verified": True, "hd": email.rsplit("@", 1)[-1]})
    G.next_exchange = exchange or (200, {"refresh_token": REFRESH, "access_token": "fake-access", "expires_in": 3600, "id_token": "x",
                                         "scope": "openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/gmail.readonly"})
    r = c.get("/gmail/callback?" + urllib.parse.urlencode({"state": q["state"], "code": "auth-code"}))
    assert r.status_code == 302
    return r.headers["Location"], q


def _count(sql, params=()):
    conn = _db()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def main() -> int:
    ids = _seed()
    passed = 0
    gcal_sync._http = G.http
    gcal_sync._verify_id_token = lambda tok: dict(CLAIMS)
    gmail_suggest._ai = fake_ai
    cs, pm, pm2, cl = (_login(k) for k in ("cs", "pm", "pm2", "client"))

    # ---- rules on their own
    wed = T + timedelta(days=(2 - T.weekday()) % 7 or 7)
    r = gmail_suggest._clean(gmail_suggest._rules("Re: site visit", "Can we meet tomorrow at 3pm at 252 Harbor View Ave, Bronx, NY 10454?", T))
    assert r == {"kind": "event", "title": "site visit", "date": (T + timedelta(days=1)).isoformat(), "time": "15:00",
                 "address": "252 Harbor View Ave, Bronx, NY 10454", "why": "Mentions something to do."}, r
    r = gmail_suggest._clean(gmail_suggest._rules("Invoice", "Please send the invoice by Wednesday.", T))
    assert r["kind"] == "task" and r["date"] == wed.isoformat() and r["time"] == ""
    assert gmail_suggest._clean(gmail_suggest._rules("Lunch photos", "Great seeing everyone yesterday!", T)) is None
    bad = gmail_suggest._clean({"is_todo": True, "kind": "event", "title": "x" * 300, "date": "next week", "time": "25:99", "address": "", "why": ""})
    assert bad["kind"] == "task" and bad["date"] == "" and bad["time"] == "" and len(bad["title"]) == 120, "the model is never trusted blindly"
    passed += 1; print("  rules ok")

    # ---- off until configured, then the connect gates
    for k in CONFIG:
        os.environ.pop(k, None)
    assert pm.get("/api/gmail/status").get_json()["data"]["reason"] == "google_not_configured"
    assert pm.get("/gmail/connect").status_code == 404 and pm.get("/api/gmail/suggestions").get_json()["data"] == []
    os.environ.update(CONFIG)
    assert pm.get("/api/gmail/status").get_json()["data"]["available"] is True and gmail_suggest.redirect_uri() == "https://app.example.test/gmail/callback"
    loc, q = _connect(pm, f"someone.else@{DOMAIN}")
    assert loc.endswith("gmail=account") and _count("SELECT COUNT(*) FROM gmail_link WHERE user_id = ?", (ids["pm"],)) == 0
    assert q["scope"] == "openid email https://www.googleapis.com/auth/gmail.readonly" and q["access_type"] == "offline"
    loc, _ = _connect(pm, f"{TAG}.pm@{DOMAIN}", exchange=(200, {"refresh_token": REFRESH, "id_token": "x", "scope": "openid email"}))
    assert loc.endswith("gmail=scope") and _count("SELECT COUNT(*) FROM gmail_link WHERE user_id = ?", (ids["pm"],)) == 0
    r = cl.get("/gmail/connect")
    assert r.status_code == 403 or (r.status_code == 302 and "accounts.google.com" not in r.headers["Location"])
    passed += 1; print("  gates ok")

    # ---- the mailbox
    me = f"{TAG}.pm@{DOMAIN}"
    G.mail = {
        "m1": msg("m1", "Owner Rep <owner@example.com>", SECRET_SUBJECT, "Hi, can you send pay app 4 with the updated SOV by Friday? Thanks."),
        "m2": msg("m2", "Builders Weekly <news@example.com>", "This week in construction", "Read more", extra_headers={"List-Unsubscribe": "<mailto:x@example.com>"}),
        "m3": msg("m3", "No Reply <no-reply@example.com>", "Your receipt", "Thanks for your order"),
        "m4": msg("m4", f"Gmail pm <{me}>", "note to self", "remember the ladder"),
        "m5": msg("m5", "Friend <friend@example.com>", "Photos from Saturday", "Here are the photos!"),
        "m6": msg("m6", "Property Manager <pmgr@example.com>", "Roof access Thursday", "Meet Thursday 10am at 252 Harbor View Ave."),
    }
    AI_ANSWERS.clear()
    AI_ANSWERS[SECRET_SUBJECT] = {"is_todo": True, "kind": "task", "title": "Send pay application #4 with the updated SOV",
                                  "date": (T + timedelta(days=2)).isoformat(), "time": "", "address": "", "why": "Asks for the pay app by Friday."}
    AI_ANSWERS["Roof access Thursday"] = {"is_todo": True, "kind": "event", "title": "Roof access walkthrough", "date": (T + timedelta(days=1)).isoformat(),
                                          "time": "10:00", "address": "252 Harbor View Ave, Bronx, NY 10454", "why": "Sets a meeting."}
    buf = io.StringIO(); h = logging.StreamHandler(buf); root = logging.getLogger(); lvl = root.level
    root.setLevel(logging.INFO); root.addHandler(h)
    try:
        loc, _ = _connect(pm, me)
        assert loc.endswith("/projects?gmail=connected"), loc
        assert G.queries[-1]["q"] == "in:inbox category:primary -from:me newer_than:2d"
        assert sorted(AI_CALLS) == sorted([SECRET_SUBJECT, "Photos from Saturday", "Roof access Thursday"]), ("no AI spent on skipped mail", AI_CALLS)
        sug = pm.get("/api/gmail/suggestions").get_json()["data"]
        by = {s["subject"]: s for s in sug}
        assert set(by) == {SECRET_SUBJECT, "Roof access Thursday"}, set(by)
        a = by[SECRET_SUBJECT]
        assert a["kind"] == "task" and a["title"] == "Send pay application #4 with the updated SOV" and a["from_name"] == "Owner Rep" and a["source"] == "ai"
        assert a["gmail_url"] == "https://mail.google.com/mail/?authuser=" + urllib.parse.quote(me) + "#all/tm1"
        ev = by["Roof access Thursday"]
        assert ev["kind"] == "event" and ev["time"] == "10:00" and ev["address"].startswith("252 Harbor View")
        assert _count("SELECT COUNT(*) FROM gmail_seen WHERE user_id = ?", (ids["pm"],)) == 6, "every email is remembered by id"
        # encrypted at rest; never the full body
        conn = _db()
        try:
            for r in conn.execute("SELECT * FROM gmail_suggestion WHERE user_id = ?", (ids["pm"],)).fetchall():
                flat = " ".join(str(v) for v in dict(r).values())
                assert "Pay app 4" not in flat and "Owner Rep" not in flat and "Harbor View" not in flat, "stored encrypted"
            link = dict(conn.execute("SELECT * FROM gmail_link WHERE user_id = ?", (ids["pm"],)).fetchone())
            assert link["refresh_token_enc"].startswith("v1:") and REFRESH not in link["refresh_token_enc"]
        finally:
            conn.close()
        before = len(AI_CALLS)
        assert pm.post("/api/gmail/check").get_json()["data"]["new"] == 0 and len(AI_CALLS) == before, "never read twice"
        passed += 1; print("  check ok")

        # ---- fallbacks: cap, no key, AI error
        G.mail["m7"] = msg("m7", "GC Super <gc@example.com>", "Inspection tomorrow", "DOB inspection tomorrow at 9am, please be on site.")
        AI_ANSWERS["Inspection tomorrow"] = RuntimeError("model down")
        assert pm.post("/api/gmail/check").get_json()["data"]["new"] == 1
        s7 = [s for s in pm.get("/api/gmail/suggestions").get_json()["data"] if s["subject"] == "Inspection tomorrow"][0]
        assert s7["source"] == "rules" and s7["kind"] == "event" and s7["time"] == "09:00"
        os.environ["GMAIL_AI_DAILY_CAP"] = str(_count("SELECT used FROM gmail_ai_usage"))
        G.mail["m8"] = msg("m8", "Architect <arch@example.com>", "Please confirm the mockup", "Could you confirm the mockup panel by Monday?")
        n = len(AI_CALLS)
        assert pm.post("/api/gmail/check").get_json()["data"]["new"] == 1 and len(AI_CALLS) == n, "cap reached: rules, no AI call"
        os.environ["GMAIL_AI_DAILY_CAP"] = CONFIG["GMAIL_AI_DAILY_CAP"]
        os.environ.pop("ANTHROPIC_API_KEY")
        G.mail["m9"] = msg("m9", "Owner <o@example.com>", "Need the COI", "We need the updated COI asap.")
        assert pm.post("/api/gmail/check").get_json()["data"]["new"] == 1 and len(AI_CALLS) == n, "no key: rules"
        os.environ["ANTHROPIC_API_KEY"] = CONFIG["ANTHROPIC_API_KEY"]
        passed += 1; print("  fallbacks ok")

        # ---- private + accept / edit / dismiss / undo
        assert pm2.get("/api/gmail/suggestions").get_json()["data"] == []
        assert pm2.post(f"/api/gmail/suggestions/{a['id']}/accept", json={}).status_code == 404
        assert pm2.post(f"/api/gmail/suggestions/{a['id']}/dismiss").status_code == 404
        r = pm.post(f"/api/gmail/suggestions/{a['id']}/accept", json={})
        assert r.status_code == 200, r.get_json()
        item = r.get_json()["data"]["item_id"]
        mine = {i["id"]: i for i in pm.get("/api/team/items?status=open&mine=1").get_json()["data"]}
        assert item in mine and mine[item]["title"] == "Send pay application #4 with the updated SOV" and mine[item]["due_date"] == (T + timedelta(days=2)).isoformat()
        assert "Open in Gmail: https://mail.google.com/mail/?authuser=" in mine[item]["detail"] and mine[item]["assignee_user_id"] == ids["pm"]
        assert pm.post(f"/api/gmail/suggestions/{a['id']}/accept", json={}).status_code == 400, "once"
        r = pm.post(f"/api/gmail/suggestions/{ev['id']}/accept", json={"kind": "event", "title": "Walk the roof with the PM", "date": (T + timedelta(days=3)).isoformat(),
                                                                  "time": "11:30", "address": "252 Harbor View Ave, Bronx, NY 10454"})
        assert r.status_code == 200, r.get_json()
        e_item = {i["id"]: i for i in pm.get("/api/team/items?status=open&mine=1").get_json()["data"]}[r.get_json()["data"]["item_id"]]
        assert e_item["kind"] == "event" and e_item["start_time"] == "11:30" and e_item["title"] == "Walk the roof with the PM" and e_item["directions_url"]
        assert pm.post(f"/api/gmail/suggestions/{s7['id']}/accept", json={"kind": "event", "date": "not a date"}).status_code == 400
        assert pm.post(f"/api/gmail/suggestions/{s7['id']}/dismiss").status_code == 200
        st = {s["id"]: s["status"] for s in pm.get("/api/gmail/suggestions").get_json()["data"]}
        assert st[s7["id"]] == "dismissed" and st[a["id"]] == "added"
        assert pm.post(f"/api/gmail/suggestions/{s7['id']}/undo").status_code == 200
        assert pm.get("/api/gmail/status").get_json()["data"]["new_count"] == 3
        assert pm.post("/api/team/items", json={"title": "Gmail nope"}).status_code == 403, "a pm still cannot create tasks directly"
        passed += 1; print("  accept ok")

        # ---- expired sign-in, then disconnect
        G.refresh_ok = False
        gmail_suggest._TOKENS.clear()
        assert pm.post("/api/gmail/check").get_json()["data"]["status"] == "needs_reconnect"
        G.refresh_ok = True
        assert pm.post("/api/gmail/disconnect").status_code == 200 and REFRESH in G.revoked
        for t in ("gmail_link", "gmail_seen", "gmail_suggestion"):
            assert _count(f"SELECT COUNT(*) FROM {t} WHERE user_id = ?", (ids["pm"],)) == 0, t
        assert item in {i["id"] for i in pm.get("/api/team/items?status=open&mine=1").get_json()["data"]}, "added tasks stay"
        assert pm.post("/api/gmail/check").status_code == 409
        passed += 1; print("  disconnect ok")
    finally:
        root.removeHandler(h); root.setLevel(lvl)
    out = buf.getvalue()
    for secret in (REFRESH, "fake-access", "Quillfeather", "owner@example.com", me, "Harbor View", "pay app 4"):
        assert secret not in out, ("log leak", secret)
    assert "gmail check user=" in out and "gmail suggestion added user=" in out
    passed += 1; print("  logs ok")

    for page in ("projects.html", "company_portal.html"):
        src = (ROOT / page).read_text(encoding="utf-8")
        assert "data-gmail-card hidden" in src and 'src="/files/static/js/gmail_suggest.js?v=' in src and "ssc:items-changed" in src, page
    js = (ROOT / "static" / "js" / "gmail_suggest.js").read_text(encoding="utf-8")
    for needle in ("/api/gmail/status", "/api/gmail/suggestions", "/accept", "/dismiss", "/undo", "/api/gmail/disconnect",
                   "/gmail/connect?next=", "Add to my to-do", "Open in Gmail", "SSCDatePicker.getISO"):
        assert needle in js, needle
    assert "Check now" not in js and "data-check" not in js and "/company#inbox" in js   # checks itself; admin / C-suite use the Inbox
    passed += 1; print("  pages ok")

    print(f"PASS smoke_gmail_suggest ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_gmail_suggest: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
