"""Google Calendar sync — each team member's tasks & events in their own Google Calendar.

One way, app -> Google. A person connects once ("Connect Google Calendar"); the app creates
a separate "Superstars" calendar in THEIR Google account and keeps in it every dated task and
event assigned to them (team_items / crm_task). Creating, editing, re-assigning, ticking done
or deleting an item in the app updates their Google calendar within seconds; "Sync now" and
connecting again catch up on anything missed. Disconnect deletes the Superstars calendar and
the stored sign-in.

Least privilege: the only Google scope is `calendar.app.created` (plus openid + email to
confirm WHO connected) — the app can create and manage the calendars it made, and cannot see
or change any other calendar or event of the person's.

  GET  /google-calendar/connect        start (signed-in team member) -> Google consent
  GET  /google-calendar/callback       Google sends the person back here
  GET  /api/google-calendar/status     available? connected? last sync, problems
  POST /api/google-calendar/sync       sync now
  POST /api/google-calendar/disconnect delete the Superstars calendar, revoke, forget

Rules
  * Switched off (status.available = false, connect 404) until BOTH the Google OAuth client
    (GOOGLE_APPS_CLIENT_ID / _SECRET, else the sign-in client GOOGLE_OAUTH_CLIENT_ID / _SECRET;
    and a redirect URI) AND the field-encryption key
    (secret_box) are configured. Nothing else changes when it is off.
  * The Google account must be the person's own company account: verified ID token, the
    same email as their app login, the company domain (hd claim).
  * The refresh token is stored encrypted (secret_box, fails closed); access tokens live
    in memory only. Never logged: tokens, emails, titles, details. Logs carry user ids and
    counts.
  * A revoked or expired sign-in marks the link `needs_reconnect`; the UI asks them to
    reconnect. Google errors never break the app write that triggered the sync.

Redirect URI: GOOGLE_CALENDAR_REDIRECT_URI, else the SSO redirect's origin +
/google-calendar/callback. It must be registered on the OAuth client in Google Cloud.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Optional

from flask import jsonify, redirect, request

import db_layer
import secret_box
from auth import current_user, requires_role

log = logging.getLogger("ssc.gcal")

TEAM_ROLES = ("admin", "c_suite", "pm", "super")
SCOPES = ("openid email https://www.googleapis.com/auth/calendar.app.created "
          "https://www.googleapis.com/auth/calendar.events.readonly "   # read: their own calendar, private to them (calendar_import.py)
          "https://www.googleapis.com/auth/calendar.calendarlist.readonly")   # which other calendars they show in Google (2026-10-19)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/calendar/v3"
TZ = "America/New_York"
CAL_NAME = "Superstars"
STATE_COOKIE = "gcal_state"
KEEP_DAYS = 30            # items that ended longer ago than this are left alone in Google
SYNC_INLINE = False       # tests flip this so a sync runs before the request returns


# ------------------------------------------------------------------ config
def _client_pair() -> tuple[str, str]:
    """The Google OAuth client Calendar and Gmail use. GOOGLE_APPS_CLIENT_ID/_SECRET (a client of
    its own, e.g. in a separate Cloud project) when BOTH are set; otherwise the sign-in client
    (GOOGLE_OAUTH_CLIENT_ID/_SECRET). Taken as a pair so an id is never sent with the other
    client's secret, and so setting up Calendar/Gmail never touches Google sign-in."""
    aid = (os.environ.get("GOOGLE_APPS_CLIENT_ID") or "").strip()
    asec = (os.environ.get("GOOGLE_APPS_CLIENT_SECRET") or "").strip()
    if aid and asec:
        return aid, asec
    return ((os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip(),
            (os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip())


def _client_id() -> str:
    return _client_pair()[0]


def _client_secret() -> str:
    return _client_pair()[1]


def _allowed_domain() -> str:
    return (os.environ.get("GOOGLE_OAUTH_ALLOWED_DOMAIN") or "superstarscontracting.com").strip().lower()


def redirect_uri() -> str:
    explicit = (os.environ.get("GOOGLE_CALENDAR_REDIRECT_URI") or "").strip()
    if explicit:
        return explicit
    sso = (os.environ.get("GOOGLE_OAUTH_REDIRECT_URI") or "").strip()
    if not sso:
        return ""
    u = urllib.parse.urlsplit(sso)
    return f"{u.scheme}://{u.netloc}/google-calendar/callback" if u.scheme and u.netloc else ""


def unavailable_reason() -> Optional[str]:
    if not (_client_id() and _client_secret() and redirect_uri()):
        return "google_not_configured"
    if not secret_box.is_configured():
        return "encryption_key_missing"
    return None


def _app_url() -> str:
    return (os.environ.get("APP_BASE_URL") or os.environ.get("SSC_PUBLIC_BASE_URL") or "").rstrip("/")


# ------------------------------------------------------------------ schema
def ensure(conn) -> None:
    pk = ("id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" if db_layer.is_postgres()
          else "id INTEGER PRIMARY KEY AUTOINCREMENT")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS google_calendar_link ("
        f"  {pk},"
        f"  user_id INTEGER NOT NULL,"
        f"  google_email TEXT,"
        f"  refresh_token_enc TEXT NOT NULL,"
        f"  calendar_id TEXT,"
        f"  status TEXT NOT NULL,"
        f"  connected_at TEXT NOT NULL,"
        f"  last_sync_at TEXT,"
        f"  last_error TEXT,"
        f"  synced_count INTEGER"
        f")")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_gcal_link_user ON google_calendar_link(user_id)")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS google_calendar_event ("
        f"  {pk},"
        f"  user_id INTEGER NOT NULL,"
        f"  item_id INTEGER NOT NULL,"
        f"  google_event_id TEXT NOT NULL,"
        f"  body_hash TEXT NOT NULL,"
        f"  synced_at TEXT NOT NULL"
        f")")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_gcal_event_user_item ON google_calendar_event(user_id, item_id)")
    # 1 = the person deleted this copy in Google and may not delete the item itself: it is not put back (calendar_import.read_back)
    if db_layer.is_postgres():
        conn.execute("ALTER TABLE google_calendar_event ADD COLUMN IF NOT EXISTS dropped INTEGER")
    elif not any(r[1] == "dropped" for r in conn.execute("PRAGMA table_info(google_calendar_event)").fetchall()):
        conn.execute("ALTER TABLE google_calendar_event ADD COLUMN dropped INTEGER")
    conn.commit()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")   # LOCAL


def _rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]


def _link(conn, uid) -> Optional[dict]:
    r = conn.execute("SELECT * FROM google_calendar_link WHERE user_id = ?", (uid,)).fetchone()
    return dict(r) if r else None


# ------------------------------------------------------------------ Google HTTP (tests swap _http)
def _http(method: str, url: str, *, token: Optional[str] = None, data=None, json_body=None) -> tuple:
    import requests
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    r = requests.request(method, url, headers=headers, data=data, json=json_body, timeout=12)
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {}
    return r.status_code, body


def _verify_id_token(id_token: str) -> Optional[dict]:
    """Signature / iss / aud / exp checked by google-auth against Google's keys."""
    try:
        from google.auth.transport import requests as g_requests
        from google.oauth2 import id_token as g_id_token
        return g_id_token.verify_oauth2_token(id_token, g_requests.Request(), _client_id())
    except Exception:
        return None


class NeedsReconnect(Exception):
    pass


_TOKENS: dict = {}   # user_id -> (access_token, expires_at); memory only


def _access_token(conn, link: dict) -> str:
    cached = _TOKENS.get(link["user_id"])
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    refresh = secret_box.decrypt_text(link["refresh_token_enc"])
    code, body = _http("POST", TOKEN_URL, data={"client_id": _client_id(), "client_secret": _client_secret(),
                                                "refresh_token": refresh, "grant_type": "refresh_token"})
    if code != 200 or not body.get("access_token"):
        if code in (400, 401) and body.get("error") in ("invalid_grant", "unauthorized_client", "invalid_client"):
            raise NeedsReconnect()
        raise RuntimeError(f"token refresh failed ({code})")
    _TOKENS[link["user_id"]] = (body["access_token"], time.time() + int(body.get("expires_in") or 3000))
    return body["access_token"]


# ------------------------------------------------------------------ what goes to Google
def _next_day(d: str) -> str:
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


def _plus(d: str, t: str, minutes: int) -> str:
    x = datetime.fromisoformat(f"{d}T{t}:00") + timedelta(minutes=minutes)
    return x.strftime("%Y-%m-%dT%H:%M:00")


def event_body(it: dict) -> Optional[dict]:
    """The Google event for one team item (or None when it has no day)."""
    kind, done = it.get("kind") or "task", (it.get("status") == "done")
    if kind in ("event", "meeting"):
        start = it.get("start_date") or it.get("due_date")
        if not start:
            return None
        end_d = it.get("end_date") or start
        if it.get("start_time"):
            st = it["start_time"]
            if it.get("end_time"):
                end = f"{end_d}T{it['end_time']}:00"
            else:
                end = _plus(end_d, st, 60)
            when = {"start": {"dateTime": f"{start}T{st}:00", "timeZone": TZ}, "end": {"dateTime": end, "timeZone": TZ}}
        else:
            when = {"start": {"date": start}, "end": {"date": _next_day(end_d)}}
        summary = it.get("title") or ("Meeting" if kind == "meeting" else "Event")
    else:
        due = it.get("due_date")
        if not due:
            return None
        if it.get("due_time"):
            when = {"start": {"dateTime": f"{due}T{it['due_time']}:00", "timeZone": TZ},
                    "end": {"dateTime": _plus(due, it["due_time"], 30), "timeZone": TZ}}
        else:
            when = {"start": {"date": due}, "end": {"date": _next_day(due)}}
        summary = ("✓ " if done else "☐ ") + (it.get("title") or "Task")
    lines = []
    if it.get("detail"):
        lines.append(it["detail"])
    if it.get("project_name"):
        lines.append("Job: " + it["project_name"])
    if it.get("created_by_name"):
        lines.append("From: " + it["created_by_name"])
    if done:
        lines.append("Done in Superstars.")
    if _app_url():
        lines.append("Open Superstars: " + _app_url() + "/projects")
    body = {"summary": summary, "description": "\n".join(lines), **when,
            "location": " · ".join(x for x in (it.get("location"), it.get("address")) if x) or None,
            "transparency": "transparent" if kind == "task" else "opaque",
            "extendedProperties": {"private": {"ssc_item": str(it["id"])}}}
    return {k: v for k, v in body.items() if v is not None}


def _hash(body: dict) -> str:
    return hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _wanted(conn, uid) -> dict:
    """item_id -> Google body, for this person's dated items (open or done) that end within
    the last KEEP_DAYS or later."""
    import team_items
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    out = {}
    try:   # legacy: events the old import copied from Google never go back to it (moved to private at boot)
        imported = {r[0] for r in conn.execute("SELECT id FROM crm_task WHERE source = 'gcal' AND assignee_user_id = ?", (uid,)).fetchall()}
    except Exception:
        conn.rollback()
        imported = set()
    for it in team_items.list_items(conn, status="all", mine_uid=uid, me=uid):
        if it["id"] in imported or it.get("invites"):   # a meeting with invitations: Google's invitation is everyone's copy (meeting_invites)
            continue
        if not it.get("calendar_from") or (it.get("calendar_to") or it["calendar_from"]) < cutoff:
            continue
        b = event_body({**it, "start_date": it.get("start_date") or it.get("calendar_from")})
        if b:
            out[it["id"]] = b
    return out


def _still_mine_but_old(conn, uid, item_id) -> bool:
    r = conn.execute("SELECT assignee_user_id, status, due_date, start_date, end_date FROM crm_task WHERE id = ?", (item_id,)).fetchone()
    if not r or (r["status"] or "open") == "cancelled":
        return False
    if r["assignee_user_id"] != uid and not conn.execute(
            "SELECT 1 FROM crm_task_attendee WHERE task_id = ? AND user_id = ?", (item_id, uid)).fetchone():
        return False
    last = r["end_date"] or r["start_date"] or r["due_date"]
    return bool(last) and last < (date.today() - timedelta(days=KEEP_DAYS)).isoformat()


# ------------------------------------------------------------------ sync
def _ensure_calendar(conn, link: dict, token: str) -> str:
    if link.get("calendar_id"):
        code, _ = _http("GET", f"{API}/calendars/{urllib.parse.quote(link['calendar_id'], safe='')}", token=token)
        if code == 200:
            return link["calendar_id"]
    code, body = _http("POST", f"{API}/calendars", token=token, json_body={"summary": CAL_NAME, "timeZone": TZ,
                                                                          "description": "Tasks and events assigned to you in Superstars."})
    if code not in (200, 201) or not body.get("id"):
        raise RuntimeError(f"could not create the calendar ({code})")
    conn.execute("UPDATE google_calendar_link SET calendar_id = ? WHERE user_id = ?", (body["id"], link["user_id"]))
    conn.execute("DELETE FROM google_calendar_event WHERE user_id = ?", (link["user_id"],))   # a new calendar starts empty
    conn.commit()
    return body["id"]


def sync_user(conn, uid) -> dict:
    """Bring this person's Superstars calendar in line with the app. Returns counts."""
    link = _link(conn, uid)
    if not link or link["status"] != "active" or unavailable_reason():
        return {"skipped": True}
    try:
        token = _access_token(conn, link)
        cal = urllib.parse.quote(_ensure_calendar(conn, link, token), safe="")
        wanted = _wanted(conn, uid)
        have = {r["item_id"]: r for r in _rows(conn.execute(
            "SELECT item_id, google_event_id, body_hash, dropped FROM google_calendar_event WHERE user_id = ?", (uid,)))}
        made = changed = removed = 0
        now = _now()
        for item_id, body in wanted.items():
            h, row = _hash(body), have.get(item_id)
            if row and (row["body_hash"] == h or row.get("dropped")):   # dropped: they deleted it in Google, it stays gone there
                continue
            if row:
                code, _ = _http("PUT", f"{API}/calendars/{cal}/events/{urllib.parse.quote(row['google_event_id'], safe='')}", token=token, json_body=body)
                if code in (200, 201):
                    conn.execute("UPDATE google_calendar_event SET body_hash = ?, synced_at = ? WHERE user_id = ? AND item_id = ?", (h, now, uid, item_id))
                    conn.commit()   # each Google change is recorded at once, so a later failure never duplicates it
                    changed += 1
                    continue
                if code not in (404, 410):
                    raise RuntimeError(f"update failed ({code})")
                conn.execute("DELETE FROM google_calendar_event WHERE user_id = ? AND item_id = ?", (uid, item_id))   # gone in Google: re-create
            code, ev = _http("POST", f"{API}/calendars/{cal}/events", token=token, json_body=body)
            if code not in (200, 201) or not ev.get("id"):
                raise RuntimeError(f"create failed ({code})")
            conn.execute("INSERT INTO google_calendar_event (user_id, item_id, google_event_id, body_hash, synced_at) VALUES (?, ?, ?, ?, ?)",
                         (uid, item_id, ev["id"], h, now))
            conn.commit()
            made += 1
        for item_id, row in have.items():
            if item_id in wanted or _still_mine_but_old(conn, uid, item_id):
                continue
            code, _ = _http("DELETE", f"{API}/calendars/{cal}/events/{urllib.parse.quote(row['google_event_id'], safe='')}", token=token)
            if code not in (200, 204, 404, 410):
                raise RuntimeError(f"delete failed ({code})")
            conn.execute("DELETE FROM google_calendar_event WHERE user_id = ? AND item_id = ?", (uid, item_id))
            conn.commit()
            removed += 1
        import meeting_invites
        meeting_invites.sync_owner(conn, uid, link, token, urllib.parse.unquote(cal))   # meetings this person sends invitations for
        total = conn.execute("SELECT COUNT(*) FROM google_calendar_event WHERE user_id = ?", (uid,)).fetchone()[0]
        conn.execute("UPDATE google_calendar_link SET last_sync_at = ?, last_error = NULL, synced_count = ? WHERE user_id = ?", (now, total, uid))
        conn.commit()
        log.info("gcal sync user=%s created=%s updated=%s removed=%s", uid, made, changed, removed)
        return {"created": made, "updated": changed, "removed": removed, "total": total}
    except NeedsReconnect:
        _TOKENS.pop(uid, None)
        conn.execute("UPDATE google_calendar_link SET status = 'needs_reconnect', last_error = ? WHERE user_id = ?", ("Google sign-in expired", uid))
        conn.commit()
        log.info("gcal sync user=%s needs reconnect", uid)
        return {"needs_reconnect": True}
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.execute("UPDATE google_calendar_link SET last_error = ? WHERE user_id = ?", ("Last sync did not finish; it will retry", uid))
        conn.commit()
        log.warning("gcal sync user=%s failed: %s", uid, type(e).__name__)
        return {"error": True}


_LOCK = threading.Lock()
_RUNNING: set = set()
_DIRTY: set = set()


def schedule(*user_ids) -> None:
    """Sync these people soon (after an app write). Never raises; off when unavailable."""
    if unavailable_reason():
        return
    for uid in {u for u in user_ids if u}:
        if SYNC_INLINE:
            conn = db_layer.connect()
            try:
                if _link(conn, uid):
                    sync_user(conn, uid)
            finally:
                conn.close()
            continue
        with _LOCK:
            if uid in _RUNNING:
                _DIRTY.add(uid)
                continue
            _RUNNING.add(uid)
        threading.Thread(target=_bg, args=(uid,), daemon=True).start()


def _bg(uid) -> None:
    try:
        while True:
            conn = db_layer.connect()
            try:
                if _link(conn, uid):
                    sync_user(conn, uid)
            except Exception as e:
                log.warning("gcal background sync user=%s: %s", uid, type(e).__name__)
            finally:
                conn.close()
            with _LOCK:
                if uid in _DIRTY:
                    _DIRTY.discard(uid)
                    continue
                _RUNNING.discard(uid)
                return
    except Exception:
        with _LOCK:
            _RUNNING.discard(uid)


# ------------------------------------------------------------------ routes
def _safe_next(v) -> str:
    v = str(v or "")
    return v if v.startswith("/") and not v.startswith("//") else "/projects"


def _back(next_url: str, outcome: str):
    sep = "&" if "?" in next_url else "?"
    base, _, frag = next_url.partition("#")
    resp = redirect(base + sep + "gcal=" + outcome + ("#" + frag if frag else ""))
    resp.set_cookie(STATE_COOKIE, "", max_age=0, httponly=True, samesite="Lax", path="/google-calendar/")
    return resp


@requires_role(*TEAM_ROLES)
def _connect():
    if unavailable_reason():
        return ("Not Found", 404)
    u = current_user() or {}
    nonce = secrets.token_urlsafe(24)
    state = nonce + "." + urllib.parse.quote(_safe_next(request.args.get("next")), safe="")
    params = {"client_id": _client_id(), "redirect_uri": redirect_uri(), "response_type": "code", "scope": SCOPES,
              "state": state, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "false",
              "hd": _allowed_domain(), "login_hint": u.get("email") or ""}
    resp = redirect(AUTH_URL + "?" + urllib.parse.urlencode(params))
    import auth
    resp.set_cookie(STATE_COOKIE, nonce, max_age=600, httponly=True, secure=auth._request_is_https(), samesite="Lax", path="/google-calendar/")
    return resp


@requires_role(*TEAM_ROLES)
def _callback():
    if unavailable_reason():
        return ("Not Found", 404)
    u = current_user() or {}
    state = request.args.get("state") or ""
    nonce, _, nxt = state.partition(".")
    next_url = _safe_next(urllib.parse.unquote(nxt))
    cookie = request.cookies.get(STATE_COOKIE) or ""
    if not nonce or not cookie or not secrets.compare_digest(nonce, cookie):
        return _back(next_url, "state")
    if request.args.get("error") or not request.args.get("code"):
        return _back(next_url, "denied")
    code, body = _http("POST", TOKEN_URL, data={"code": request.args["code"], "client_id": _client_id(), "client_secret": _client_secret(),
                                                "redirect_uri": redirect_uri(), "grant_type": "authorization_code"})
    if code != 200 or not body.get("refresh_token") or not body.get("id_token"):
        log.info("gcal connect user=%s: token exchange failed (%s)", u.get("id"), code)
        return _back(next_url, "failed")
    if "calendar.app.created" not in (body.get("scope") or ""):
        return _back(next_url, "scope")
    claims = _verify_id_token(body["id_token"])
    email = ((claims or {}).get("email") or "").strip().lower()
    if (not claims or claims.get("email_verified") is not True or (claims.get("hd") or "").lower() != _allowed_domain()
            or email != (u.get("email") or "").strip().lower()):
        log.info("gcal connect user=%s: google account rejected", u.get("id"))
        return _back(next_url, "account")
    conn = db_layer.connect()
    try:
        enc = secret_box.encrypt_text(body["refresh_token"])
        old = _link(conn, u["id"])
        if old:
            conn.execute("UPDATE google_calendar_link SET google_email = ?, refresh_token_enc = ?, status = 'active', "
                         "connected_at = ?, last_error = NULL WHERE user_id = ?", (email, enc, _now(), u["id"]))
        else:
            conn.execute("INSERT INTO google_calendar_link (user_id, google_email, refresh_token_enc, status, connected_at) "
                         "VALUES (?, ?, ?, 'active', ?)", (u["id"], email, enc, _now()))
        try:   # which permissions Google granted (reading the main calendar needs calendar.events.readonly)
            conn.execute("UPDATE google_calendar_link SET scopes = ?, import_at = NULL WHERE user_id = ?",
                         ((body.get("scope") or "")[:1000], u["id"]))
        except Exception:
            conn.rollback()
        conn.commit()
        if body.get("access_token"):
            _TOKENS[u["id"]] = (body["access_token"], time.time() + int(body.get("expires_in") or 3000))
        else:
            _TOKENS.pop(u["id"], None)
        result = sync_user(conn, u["id"])
    finally:
        conn.close()
    threading.Thread(target=_first_import, args=(u["id"],), daemon=True).start()
    log.info("gcal connected user=%s", u.get("id"))
    return _back(next_url, "connected" if not result.get("error") else "connected_retry")


def _first_import(uid) -> None:
    """Right after connecting: copy this person's own calendar in, privately (calendar_import)."""
    try:
        import calendar_import
        conn = db_layer.connect()
        try:
            calendar_import.import_user(conn, uid)
        finally:
            conn.close()
    except Exception as e:
        log.info("gcal first import user=%s skipped: %s", uid, type(e).__name__)


def status_for(conn, uid) -> dict:
    reason = unavailable_reason()
    link = _link(conn, uid) if not reason else None
    return {"available": not reason, "reason": reason, "connected": bool(link),
            "reads_calendar": bool(link) and "calendar.events.readonly" in (link.get("scopes") or ""),
            "lists_calendars": bool(link) and "calendar.calendarlist.readonly" in (link.get("scopes") or ""),
            "import_at": link.get("import_at") if link else None,
            "status": link["status"] if link else None, "google_email": link["google_email"] if link else None,
            "last_sync_at": link["last_sync_at"] if link else None, "synced_count": link["synced_count"] if link else 0,
            "problem": link["last_error"] if link else None}


@requires_role(*TEAM_ROLES)
def _status():
    u = current_user() or {}
    conn = db_layer.connect()
    try:
        return jsonify({"data": status_for(conn, u["id"])})
    finally:
        conn.close()


@requires_role(*TEAM_ROLES)
def _sync_now():
    if unavailable_reason():
        return jsonify({"error": "Google Calendar sync is not switched on yet"}), 409
    u = current_user() or {}
    conn = db_layer.connect()
    try:
        if not _link(conn, u["id"]):
            return jsonify({"error": "connect Google Calendar first"}), 409
        result = sync_user(conn, u["id"])
        return jsonify({"data": {**result, **status_for(conn, u["id"])}})
    finally:
        conn.close()


@requires_role(*TEAM_ROLES)
def _disconnect():
    u = current_user() or {}
    conn = db_layer.connect()
    try:
        link = _link(conn, u["id"])
        if not link:
            return jsonify({"data": {"connected": False}})
        if not unavailable_reason():
            try:   # best effort: remove the Superstars calendar, then revoke the sign-in
                token = _access_token(conn, link)
                if link.get("calendar_id"):
                    _http("DELETE", f"{API}/calendars/{urllib.parse.quote(link['calendar_id'], safe='')}", token=token)
                _http("POST", REVOKE_URL, data={"token": secret_box.decrypt_text(link["refresh_token_enc"])})
            except Exception as e:
                log.info("gcal disconnect user=%s: cleanup skipped (%s)", u["id"], type(e).__name__)
        conn.execute("DELETE FROM google_calendar_event WHERE user_id = ?", (u["id"],))
        conn.execute("DELETE FROM google_calendar_link WHERE user_id = ?", (u["id"],))
        try:   # the private copy of their own calendar goes with the connection (calendar_import.py)
            import calendar_import
            calendar_import.forget_user(conn, u["id"])
        except Exception as e:
            conn.rollback()
            log.info("gcal disconnect user=%s: private copy not removed (%s)", u["id"], type(e).__name__)
        conn.commit()
        _TOKENS.pop(u["id"], None)
        log.info("gcal disconnected user=%s", u["id"])
        return jsonify({"data": {"connected": False}})
    finally:
        conn.close()


def register(app) -> None:
    conn = db_layer.connect()
    try:
        ensure(conn)
        db_layer._PKMAP_CACHE.clear()   # tables created at boot after the pk-map cache filled (#290 lesson)
    except Exception as e:
        log.warning("google calendar schema ensure skipped: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    app.add_url_rule("/google-calendar/connect", "gcal_connect", _connect, methods=["GET"])
    app.add_url_rule("/google-calendar/callback", "gcal_callback", _callback, methods=["GET"])
    app.add_url_rule("/api/google-calendar/status", "gcal_status", _status, methods=["GET"])
    app.add_url_rule("/api/google-calendar/sync", "gcal_sync_now", _sync_now, methods=["POST"])
    app.add_url_rule("/api/google-calendar/disconnect", "gcal_disconnect", _disconnect, methods=["POST"])
