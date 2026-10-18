"""Meeting invitations through Google Calendar (prototype accepted 2026-10-18:
https://claude.ai/artifact/FegCD9XrcmH4L57qnAMGCF).

A meeting saved in Superstars becomes ONE real Google Calendar event, on the "Superstars" calendar of
the person who saved it (the organizer, crm_task.invite_owner_id), with every guest on it:

  * guests = the person it is assigned to + the team people in it (their login email) + the outside
    guests typed by email. Google emails each one the usual invitation (sendUpdates=all) and puts the
    meeting on their own calendar, with Yes / No / Maybe. "Don't send" on save = sendUpdates=none
  * video = Google Meet, made by Google (conferenceData.createRequest, conferenceDataVersion=1). The
    link is kept in meeting_invite.meet_url and shown in the app. If Google refuses video on that
    calendar, the meeting still goes out without it and the dialog says so
  * an edit in Superstars PATCHes the event (Google emails the update); a delete, or turning it into a
    task / event, DELETEs it with sendUpdates=all (Google emails the cancellation)
  * every round of the calendar loop reads the guests' answers back (meeting_invite.guest_status) and
    picks up a Meet link Google finished later
  * the per-person copies gcal_sync pushes for tasks and events are NOT made for these meetings (the
    invitation is everyone's copy), and calendar_import skips the invitation on each guest's own
    calendar (extendedProperties.shared.ssc_item), so nothing shows twice

Needs only the calendar.app.created permission everyone already granted (events on the app's own
calendar may have guests and a Meet link). Without a working Google Calendar the meeting is saved for
the team as before and nobody is emailed. Logs carry ids and counts only, never titles or addresses.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Optional

log = logging.getLogger("ssc.meeting_invites")

ANSWERS_EVERY = 300        # seconds between reading a meeting's answers back
ANSWERS_PER_ROUND = 25
KEEP_DAYS = 30


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _rows(conn, sql, params=()) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params=()) -> Optional[dict]:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def can_organize(conn, uid) -> bool:
    """This person's Google Calendar is connected and working, so it can send invitations."""
    import gcal_sync
    if not uid or gcal_sync.unavailable_reason():
        return False
    link = gcal_sync._link(conn, uid)
    return bool(link) and link.get("status") == "active"


# ------------------------------------------------------------------ the Google event
def guest_emails(conn, it: dict, organizer_emails: set) -> list:
    """Everyone invited: the assignee, the team people in it, the outside guests. Not the organizer."""
    ids = [i for i in [it.get("assignee_user_id"), *(it.get("attendee_ids") or [])] if i]
    out = []
    if ids:
        marks = ",".join("?" * len(ids))
        for r in _rows(conn, f"SELECT email FROM users WHERE id IN ({marks}) ORDER BY id", ids):
            e = (r["email"] or "").strip().lower()
            if e and e not in out:
                out.append(e)
    for e in it.get("guest_emails") or []:
        if e not in out:
            out.append(e)
    return [e for e in out if e not in organizer_emails]


def body_for(conn, it: dict, organizer_emails: set) -> Optional[dict]:
    import gcal_sync
    base = gcal_sync.event_body({**it, "start_date": it.get("start_date") or it.get("calendar_from")})
    if not base:
        return None
    base["attendees"] = [{"email": e} for e in guest_emails(conn, it, organizer_emails)]
    base["extendedProperties"] = {"private": {"ssc_item": str(it["id"])}, "shared": {"ssc_item": str(it["id"])}}
    base["guestsCanSeeOtherGuests"] = True
    base["guestsCanModify"] = False
    base.pop("transparency", None)
    return base


def _hash(body: dict, video: bool) -> str:
    return hashlib.sha1(json.dumps([body, bool(video)], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _meet(ev: dict) -> tuple:
    """(link, state) from a Google event: state 'success' | 'pending' | 'failure' | None."""
    cd = ev.get("conferenceData") or {}
    link = ev.get("hangoutLink") or next((p.get("uri") for p in cd.get("entryPoints") or [] if p.get("entryPointType") == "video"), None)
    state = ((cd.get("createRequest") or {}).get("status") or {}).get("statusCode")
    return link, (state or ("success" if link else None))


def _answers(ev: dict) -> dict:
    return {str(a.get("email") or "").lower(): a.get("responseStatus") or "needsAction"
            for a in ev.get("attendees") or [] if a.get("email") and not a.get("organizer") and not a.get("self")}


def _url(cal: str, event_id: Optional[str] = None, send: Optional[str] = None) -> str:
    import gcal_sync
    u = f"{gcal_sync.API}/calendars/{urllib.parse.quote(cal, safe='')}/events"
    if event_id:
        u += "/" + urllib.parse.quote(event_id, safe="")
    q = {"conferenceDataVersion": "1"}
    if send:
        q["sendUpdates"] = send
    return u + "?" + urllib.parse.urlencode(q)


def _save(conn, item_id, owner, **cols) -> None:
    have = _one(conn, "SELECT item_id FROM meeting_invite WHERE item_id = ?", (item_id,))
    if have:
        conn.execute(f"UPDATE meeting_invite SET {', '.join(k + ' = ?' for k in cols)}, owner_id = ? WHERE item_id = ?",
                     (*cols.values(), owner, item_id))
    else:
        keys = list(cols)
        conn.execute(f"INSERT INTO meeting_invite (item_id, owner_id, {', '.join(keys)}) VALUES (?, ?, {', '.join('?' * len(keys))})",
                     (item_id, owner, *cols.values()))
    conn.commit()


# ------------------------------------------------------------------ push (inside gcal_sync.sync_user)
def _owned(conn, uid) -> dict:
    """item id -> shaped item, for the live meetings this person organizes."""
    import team_items
    out = {}
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    ids = [r["id"] for r in _rows(conn, "SELECT id FROM crm_task WHERE invite_owner_id = ? AND kind = 'meeting' "
                                        "AND COALESCE(status, 'open') <> 'cancelled'", (uid,))]
    for it in team_items.list_items(conn, status="all", me=uid) if ids else []:
        if it["id"] in ids and (it.get("calendar_to") or it.get("calendar_from") or "") >= cutoff:
            it["guest_emails"] = it.get("guest_emails") or []
            it["video"] = bool(it.get("video"))
            out[it["id"]] = it
    return out


def sync_owner(conn, uid: int, link: dict, token: str, cal: str) -> dict:
    """Bring the invitations this person sends in line with the app. Called by gcal_sync.sync_user with
    a working token and the Superstars calendar id; Google errors other than a refused video raise."""
    import gcal_sync
    me = {(link.get("google_email") or "").lower()}
    r = _one(conn, "SELECT email FROM users WHERE id = ?", (uid,))
    me.add(((r or {}).get("email") or "").lower())
    me.discard("")
    owned = _owned(conn, uid)
    sends = {x["id"]: (x["invite_send"] or "all") for x in _rows(conn, "SELECT id, invite_send FROM crm_task WHERE invite_owner_id = ?", (uid,))}
    rows = {x["item_id"]: x for x in _rows(conn, "SELECT * FROM meeting_invite WHERE owner_id = ?", (uid,))}
    made = changed = removed = 0
    for item_id, it in owned.items():
        body = body_for(conn, it, me)
        row = rows.get(item_id) or {}
        if not body:
            continue
        send = "none" if sends.get(item_id) == "none" else "all"
        h = _hash(body, it["video"])
        want_meet = it["video"] and not row.get("meet_url") and row.get("meet_state") != "failure"
        drop_meet = (not it["video"]) and row.get("meet_url")
        if row.get("google_event_id") and row.get("body_hash") == h and not want_meet and not drop_meet:
            continue
        payload = dict(body)
        if want_meet:
            payload["conferenceData"] = {"createRequest": {"requestId": f"ssc-{item_id}-{int(time.time())}",
                                                           "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        elif drop_meet:
            payload["conferenceData"] = None
        if row.get("google_event_id"):
            code, ev = gcal_sync._http("PATCH", _url(cal, row["google_event_id"], send), token=token, json_body=payload)
            if code in (404, 410):
                row = {}
            elif code == 400 and want_meet:
                payload.pop("conferenceData", None)
                code, ev = gcal_sync._http("PATCH", _url(cal, row["google_event_id"], send), token=token, json_body=payload)
                ev = dict(ev or {}, _meet_refused=True)
            if row and code not in (200, 201):
                raise RuntimeError(f"meeting update failed ({code})")
            if row:
                changed += 1
        if not row.get("google_event_id"):
            code, ev = gcal_sync._http("POST", _url(cal, None, send), token=token, json_body=payload)
            if code == 400 and "conferenceData" in payload:
                payload.pop("conferenceData", None)
                code, ev = gcal_sync._http("POST", _url(cal, None, send), token=token, json_body=payload)
                ev = dict(ev or {}, _meet_refused=True)
            if code not in (200, 201) or not (ev or {}).get("id"):
                raise RuntimeError(f"meeting create failed ({code})")
            made += 1
        link_url, state = _meet(ev or {})
        if (ev or {}).get("_meet_refused"):
            link_url, state = None, "failure"
        if drop_meet:
            link_url, state = None, None
        _save(conn, item_id, uid, google_event_id=ev["id"], calendar_id=cal, body_hash=h,
              meet_url=link_url or (None if drop_meet else row.get("meet_url")),
              meet_state=state or (None if drop_meet else row.get("meet_state")),
              guest_status=json.dumps(_answers(ev or {})), synced_at=_now(), answers_at=_now(),
              last_error="Google did not add a video link to this meeting" if state == "failure" else None)
    for item_id, row in rows.items():   # deleted, turned into a task / event, or past the window
        if item_id in owned or not row.get("google_event_id"):
            continue
        cur = _one(conn, "SELECT kind, status, invite_owner_id, start_date, end_date FROM crm_task WHERE id = ?", (item_id,))
        last = (cur or {}).get("end_date") or (cur or {}).get("start_date") or ""
        if cur and cur.get("kind") == "meeting" and (cur.get("status") or "open") != "cancelled" and cur.get("invite_owner_id") == uid \
                and last and last < (date.today() - timedelta(days=KEEP_DAYS)).isoformat():
            continue   # an old meeting stays in Google as it was
        send = "all"   # a cancellation always reaches the guests
        code, _ = gcal_sync._http("DELETE", _url(row.get("calendar_id") or cal, row["google_event_id"], send), token=token)
        if code not in (200, 204, 404, 410):
            raise RuntimeError(f"meeting delete failed ({code})")
        conn.execute("DELETE FROM meeting_invite WHERE item_id = ?", (item_id,))
        conn.commit()
        removed += 1
    if made or changed or removed:
        log.info("meeting invites user=%s created=%s updated=%s removed=%s", uid, made, changed, removed)
    return {"created": made, "updated": changed, "removed": removed}


# ------------------------------------------------------------------ answers back (calendar loop)
def refresh_answers(conn, uid: int) -> int:
    """Read the guests' Yes / No / Maybe (and a Meet link Google finished later) for this organizer's
    upcoming meetings. Returns how many meetings were read."""
    import gcal_sync
    link = gcal_sync._link(conn, uid)
    if not link or link.get("status") != "active":
        return 0
    stale = (datetime.now() - timedelta(seconds=ANSWERS_EVERY)).isoformat(timespec="seconds")
    since = (date.today() - timedelta(days=1)).isoformat()
    rows = _rows(conn, "SELECT mi.* FROM meeting_invite mi JOIN crm_task t ON t.id = mi.item_id WHERE mi.owner_id = ? "
                       "AND mi.google_event_id IS NOT NULL AND COALESCE(t.end_date, t.start_date, t.due_date) >= ? "
                       "AND (mi.answers_at IS NULL OR mi.answers_at < ? OR mi.meet_state = 'pending') ORDER BY mi.answers_at",
                 (uid, since, stale))[:ANSWERS_PER_ROUND]
    if not rows:
        return 0
    try:
        token = gcal_sync._access_token(conn, link)
    except gcal_sync.NeedsReconnect:
        return 0
    n = 0
    for row in rows:
        code, ev = gcal_sync._http("GET", _url(row.get("calendar_id") or link.get("calendar_id") or "", row["google_event_id"]), token=token)
        if code != 200:
            conn.execute("UPDATE meeting_invite SET answers_at = ? WHERE item_id = ?", (_now(), row["item_id"]))
            conn.commit()
            continue
        meet, state = _meet(ev)
        conn.execute("UPDATE meeting_invite SET guest_status = ?, answers_at = ?, meet_url = COALESCE(?, meet_url), "
                     "meet_state = COALESCE(?, meet_state) WHERE item_id = ?",
                     (json.dumps(_answers(ev)), _now(), meet, state, row["item_id"]))
        conn.commit()
        n += 1
    return n


def refresh_all(conn) -> None:
    for r in _rows(conn, "SELECT DISTINCT owner_id FROM meeting_invite"):
        try:
            refresh_answers(conn, r["owner_id"])
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            log.info("meeting answers user=%s skipped (%s)", r["owner_id"], type(e).__name__)
