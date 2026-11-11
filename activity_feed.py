"""The activity bell and top-right banners (accepted prototype 2026-11-03:
https://claude.ai/artifact/UZ2YNWpwy6SmTYF2z9ReuR, phase 2).

What the team did is worked out each time from the records that already say who did it and when, so no write path has
to remember to report anything:

  proposal_event        sent, signed, declined, started
  crm_organization      a client or lead was added
  projects              a project was created (who: the crm_activity row convert_to_project writes)
  project_documents     uploads, one line per job per day; never a banner
  invoice_payment       a payment was recorded; never a banner

Wording says what was done, never which teammate did it. A person's own actions are left out. Admins only for now.
What needs a decision comes from activity_accepts.py (phase 3).

"For you" (accepted prototype 2026-11-11: https://claude.ai/artifact/E3AZ6ytnir14NxaBCnQgK6): the bell's first tab is
this person's day, the same cards as Needs you on the dashboard (today_feed.needs), so the two always match. The badge
counts For you plus what waits for a decision. Once a day the first page shows a morning banner with the day's counts.
Nothing here reads anyone's email, and no AI runs. `activity_seen` keeps two marks per person: what they have read (the
badge) and what has already popped up as a banner.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

from flask import jsonify, request

import db_layer
from auth import current_user, requires_role

log = logging.getLogger("ssc.activity_feed")

WINDOW_DAYS = 14
MAX_ITEMS = 40
MAX_BANNERS = 3
BANNER_MAX_AGE = timedelta(hours=24)   # back after a long weekend: it's all in the bell, nothing pops up
SAME_MOMENT = timedelta(minutes=5)     # a client added this close to a proposal came from that proposal
QUICKBOOKS_CLIENT_NOTE = "Created from QuickBooks"   # qbo.py: a sync can add dozens at once


def _rows(conn, sql, params=()) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def ensure(conn) -> None:
    """Idempotent."""
    conn.execute("CREATE TABLE IF NOT EXISTS activity_seen (user_id INTEGER PRIMARY KEY, read_at TEXT, banner_at TEXT)")
    conn.commit()
    if not _has_column(conn, "activity_seen", "morning_on"):   # the day the morning banner last showed (local date)
        conn.execute("ALTER TABLE activity_seen ADD COLUMN morning_on TEXT")
        conn.commit()
    import activity_accepts
    activity_accepts.ensure(conn)


def _has_column(conn, table: str, column: str) -> bool:
    try:
        conn.execute(f"SELECT {column} FROM {table} WHERE 1 = 0")
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def _parse(at) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(at)).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _stamp(at: datetime) -> str:
    return at.isoformat(timespec="seconds")


def _time(at: datetime) -> str:
    return at.strftime("%I:%M %p").lstrip("0")


def _when(at: datetime, now: datetime) -> str:
    gap = now - at
    if gap < timedelta(minutes=1):
        return "just now"
    if gap < timedelta(hours=1):
        return f"{int(gap.total_seconds() // 60)} min ago"
    if at.date() == now.date():
        return _time(at)
    if at.date() == (now - timedelta(days=1)).date():
        return f"Yesterday, {_time(at)}"
    return f"{at.strftime('%b')} {at.day}, {_time(at)}"


def _money(v) -> str:
    return f"${float(v or 0):,.2f}".replace(".00", "")


def _item(key: str, kind: str, icon: str, title: str, sub: str, at, href: str, *, banner: bool = False, **extra) -> Optional[dict]:
    when = _parse(at)
    if not when:
        return None
    return {"id": key, "kind": kind, "icon": icon, "title": title, "sub": sub, "at": _stamp(when), "href": href,
            "banner": banner, **extra}


def _proposal_href(p: dict) -> str:
    return f"/proposal-studio#p/{p['proposal_id']}" if p.get("layout") == "ssc" else f"/company#proposals/{p['proposal_id']}"


def _proposal_items(conn, uid: int, since: str) -> list:
    import proposal_engine
    rows = _rows(conn, "SELECT e.id AS event_id, e.event, e.at, e.detail, p.id AS proposal_id, p.status, p.layout, p.number, "
                       "p.display_no, p.version, o.name AS client, b.address_line AS address, es.building_address AS est_address "
                       "FROM proposal_event e JOIN proposal p ON p.id = e.proposal_id "
                       "JOIN crm_organization o ON o.id = p.client_org_id "
                       "LEFT JOIN building b ON b.id = p.building_id "
                       "LEFT JOIN estimate es ON es.id = p.estimate_id "
                       "WHERE e.event IN ('created', 'sent', 'signed', 'declined') AND p.status <> 'deleted' "
                       "AND CAST(e.at AS TEXT) >= ? AND (e.actor_user_id IS NULL OR e.actor_user_id <> ?) "
                       "ORDER BY e.id DESC", (since, uid))
    out, seen = [], set()
    for r in rows:
        if (r["proposal_id"], r["event"]) in seen:   # sent twice: the latest is what matters
            continue
        seen.add((r["proposal_id"], r["event"]))
        label, client = proposal_engine.label(r), r["client"]
        place = r["address"] or r["est_address"] or ""
        href = _proposal_href(r)
        common = {"proposal_id": r["proposal_id"], "open_label": "Open proposal"}
        if r["event"] == "sent":
            old = "before the app" in str(r.get("detail") or "")   # typing in an old proposal is filing, not news
            it = _item(f"proposal:{r['event_id']}", "sent", "send", f"Proposal {label} was sent to {client}", place, r["at"],
                       href, banner=not old, **common)
        elif r["event"] == "signed":
            it = _item(f"proposal:{r['event_id']}", "signed", "send", f"Proposal {label} was signed by {client}", place, r["at"],
                       href, banner=True, **common)
        elif r["event"] == "declined":
            it = _item(f"proposal:{r['event_id']}", "declined", "doc", f"Proposal {label} was declined", client, r["at"],
                       href, banner=True, **common)
        else:
            if r["status"] != "draft":   # it moved on: "sent" says the rest
                continue
            title = f"Proposal {label} was started" if (r.get("version") or 1) > 1 else f"A proposal for {place or client} was started"
            it = _item(f"proposal:{r['event_id']}", "started", "doc", title, f"Proposal {label}", r["at"], href, **common)
        if it:
            out.append(it)
    return out


def _client_items(conn, uid: int, since: str) -> list:
    import proposal_engine
    out = []
    for o in _rows(conn, "SELECT id, name, relationship_type, created_at FROM crm_organization "
                         "WHERE relationship_type IN ('client', 'lead') AND CAST(created_at AS TEXT) >= ? "
                         "AND (created_by IS NULL OR created_by <> ?) AND COALESCE(notes, '') <> ? "
                         "ORDER BY id DESC LIMIT 60", (since, uid, QUICKBOOKS_CLIENT_NOTE)):
        added = _parse(o["created_at"])
        sub = "Added to your client list"
        first = _rows(conn, "SELECT number, display_no, version, created_at FROM proposal WHERE client_org_id = ? "
                            "AND status <> 'deleted' ORDER BY id LIMIT 1", (o["id"],))
        if first and added and (_parse(first[0]["created_at"]) or added + SAME_MOMENT * 2) - added <= SAME_MOMENT:
            sub = f"Added when Proposal {proposal_engine.label(first[0])} was started"
        word = "lead" if o["relationship_type"] == "lead" else "client"
        it = _item(f"client:{o['id']}", "client", "person", f"New {word} added: {o['name']}", sub, o["created_at"],
                   f"/company#clients/{o['id']}", banner=True, open_label=f"Open {word}")
        if it:
            out.append(it)
    return out


def _project_items(conn, uid: int, since: str) -> list:
    out = []
    for p in _rows(conn, "SELECT p.project_code, p.name, p.address, p.created_at FROM projects p "
                         "WHERE CAST(p.created_at AS TEXT) >= ? AND NOT EXISTS (SELECT 1 FROM crm_activity a "
                         "WHERE a.summary = 'Estimate ' || p.project_code || ' converted to project ' || p.project_code "
                         "AND a.author_user_id = ?) ORDER BY p.created_at DESC LIMIT 60", (since, uid)):
        code = p["project_code"]
        it = _item(f"project:{code}", "project", "build", f"Project {code} was created", p["address"] or p["name"] or "",
                   p["created_at"], f"/projects/{quote(str(code), safe='')}", banner=True, open_label="Open project")
        if it:
            out.append(it)
    return out


def _document_items(conn, uid: int, since: str) -> list:
    """One line per job per day, however many files: uploads don't interrupt anyone."""
    days: dict = {}
    for d in _rows(conn, "SELECT project_code, category, uploaded_at FROM project_documents WHERE CAST(uploaded_at AS TEXT) >= ? "
                         "AND (uploaded_by_uid IS NULL OR uploaded_by_uid <> ?) ORDER BY uploaded_at DESC LIMIT 500", (since, uid)):
        at = _parse(d["uploaded_at"])
        if not at or not d["project_code"]:
            continue
        g = days.setdefault((d["project_code"], at.date().isoformat()), {"n": 0, "cats": {}, "at": at})
        g["n"] += 1
        g["at"] = max(g["at"], at)
        cat = str(d["category"] or "Other").replace("_", " ").capitalize()
        g["cats"][cat] = g["cats"].get(cat, 0) + 1
    out = []
    for (code, day), g in days.items():
        title = f"1 document was uploaded to {code}" if g["n"] == 1 else f"{g['n']} documents were uploaded to {code}"
        sub = " · ".join(f"{cat} {n}" for cat, n in sorted(g["cats"].items(), key=lambda kv: -kv[1]))
        # the id carries the count, so a later upload the same day is new again
        out.append(_item(f"docs:{code}:{day}:{g['n']}", "docs", "doc", title, sub, g["at"], f"/projects/{quote(str(code), safe='')}",
                         open_label="Open project"))
    return out


def _payment_items(conn, uid: int, since: str) -> list:
    out = []
    for r in _rows(conn, "SELECT pay.id, pay.amount, pay.created_at, i.id AS invoice_id, i.number, i.amount AS total, i.status, "
                         "o.name AS client, (SELECT MAX(x.id) FROM invoice_payment x WHERE x.invoice_id = i.id) AS last_id "
                         "FROM invoice_payment pay JOIN invoice i ON i.id = pay.invoice_id "
                         "LEFT JOIN crm_organization o ON o.id = i.client_org_id "
                         "WHERE CAST(pay.created_at AS TEXT) >= ? AND CAST(pay.paid_on AS TEXT) >= ? "   # a QuickBooks catch-up isn't news
                         "AND (pay.created_by IS NULL OR pay.created_by <> ?) ORDER BY pay.id DESC LIMIT 60",
                     (since, since[:10], uid)):
        full = r["status"] == "paid" and r["id"] == r["last_id"]
        title = (f"Invoice {r['number']} was paid · {_money(r['total'])}" if full
                 else f"A {_money(r['amount'])} payment was recorded on invoice {r['number']}")
        it = _item(f"payment:{r['id']}", "paid", "money", title, r["client"] or "", r["created_at"],
                   f"/company#invoice/{r['invoice_id']}", open_label="Open invoice")
        if it:
            out.append(it)
    return out


def _brief_items(conn, uid: int, since: str) -> list:
    """The owner's morning brief is ready (until it is sent)."""
    import gmail_brief
    return gmail_brief.bell_items(conn, uid, since)


def _closed_cards(conn, uid: int) -> dict:
    """proposal id -> this person's Needs you card that closed because of it, while Reopen still works."""
    import claude_agent
    since = _stamp(datetime.now() - claude_agent.CARD_TTL)
    out = {}
    for a in _rows(conn, "SELECT id, result FROM claude_action WHERE user_id = ? AND status = 'declined' AND tool = 'draft_proposal' "
                         "AND created_at >= ? AND result LIKE '%\"handled\"%' ORDER BY id", (uid, since)):
        try:
            done = json.loads(a["result"] or "{}").get("handled") or {}
        except ValueError:
            continue
        if done.get("how") == "team" and done.get("proposal_id"):
            out[done["proposal_id"]] = a["id"]
    return out


# ------------------------------------------------------------------ For you: this person's day
MORNING_FROM_HOUR = 5          # the morning banner never shows for a page left open overnight before this
FOR_YOU_MAX = 30
_FOR_YOU_ICON = (("crew", "person"), ("money", "money"), ("bill", "money"), ("cash", "money"), ("doc", "doc"),
                 ("task", "check"), ("email", "mail"), ("mail", "mail"), ("field", "build"))


def _local_href(href: str) -> str:
    """Needs you links are written for the dashboard (#finance); the bell is on every page."""
    href = str(href or "")
    return "/company" + href if href.startswith("#") else (href or "/company#overview")


def _for_you_item(c: dict) -> dict:
    claude = bool(c.get("action_id"))
    group = "claude" if claude else ("today" if c.get("urgency") in ("now", "today") else "soon")
    cat = f"{c.get('cat') or ''} {c.get('icon') or ''}"
    icon = "spark" if claude else next((ic for k, ic in _FOR_YOU_ICON if k in cat), "doc")
    tint = "blue" if claude else ("bad" if c.get("urgency") == "now" else ("warn" if group == "today" else "plain"))
    why = str(c.get("why") or "")
    return {"id": "you:" + str(c.get("key")), "kind": "you", "group": group, "icon": icon, "tint": tint,
            "title": str(c.get("title") or ""), "sub": why[:160] + ("…" if len(why) > 160 else ""),
            "pill": c.get("pill"), "href": _local_href(c.get("href")), "urgency": c.get("urgency")}


def for_you(conn, uid: int, now: datetime, first: Optional[list] = None) -> list:
    """The same cards as Needs you (today_feed.needs): what wants this person today, Claude's drafts, what's coming up.
    Cards hidden on the dashboard (Later / Good) stay hidden here."""
    import today_feed
    today = now.date()
    pay = today_feed.payments(conn, today)["_rows"]
    crew = today_feed.crew(conn, today, now)
    cards = today_feed.needs(conn, uid, today, now, pay, crew)
    order = {"today": 0, "claude": 1, "soon": 2}
    items = [_for_you_item(c) for c in cards if c.get("key") and c.get("title")]
    items.sort(key=lambda i: order[i["group"]])   # stable: Needs you's own order inside each group
    return (list(first or []) + items)[:FOR_YOU_MAX]   # first: proposal reviews waiting on this person


def _first_name(conn, uid: int) -> str:
    r = conn.execute("SELECT full_name FROM users WHERE id = ?", (uid,)).fetchone()
    return (str((r["full_name"] if r else "") or "").split() or [""])[0]


def morning(conn, uid: int, now: datetime, you: list) -> Optional[dict]:
    """Once a day, on the first page after 5 AM: the day's counts. Nothing to say, nothing shows."""
    if now.hour < MORNING_FROM_HOUR or not you:
        return None
    r = conn.execute("SELECT morning_on FROM activity_seen WHERE user_id = ?", (uid,)).fetchone()
    if r and str(r["morning_on"] or "") == now.date().isoformat():
        return None
    count = lambda g: sum(1 for i in you if i["group"] == g)  # noqa: E731
    lines = []
    for n, one, many in ((count("review"), "proposal waiting on you", "proposals waiting on you"),
                         (count("today"), "thing due today or late", "things due today or late"),
                         (count("claude"), "suggestion from Claude to review", "suggestions from Claude to review"),
                         (count("soon"), "coming up this week", "coming up this week")):
        if n:
            lines.append({"n": n, "label": one if n == 1 else many})
    name = _first_name(conn, uid)
    return {"id": "morning:" + now.date().isoformat(), "day": now.date().isoformat(),
            "title": "Good morning" + (", " + name if name else ""), "lines": lines}


def _marks(conn, uid: int, now: datetime) -> tuple:
    """(read_at, banner_at). The first look starts from now, so turning this on doesn't set off two weeks of news."""
    r = conn.execute("SELECT read_at, banner_at FROM activity_seen WHERE user_id = ?", (uid,)).fetchone()
    if r:
        return _parse(r["read_at"]) or now, _parse(r["banner_at"]) or now
    conn.execute("INSERT INTO activity_seen (user_id, read_at, banner_at) VALUES (?,?,?) ON CONFLICT (user_id) DO NOTHING",
                 (uid, _stamp(now), _stamp(now)))
    conn.commit()
    return now, now


def build(conn, uid: int, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now().replace(microsecond=0)
    read_at, banner_at = _marks(conn, uid, now)
    since = _stamp(now - timedelta(days=WINDOW_DAYS))
    items = []
    for source in (_proposal_items, _client_items, _project_items, _document_items, _payment_items, _brief_items):
        try:
            items += [i for i in source(conn, uid, since) if i]
        except Exception as e:   # one source failing must not take the bell down; no record contents in the log
            log.warning("activity source %s skipped: %s", source.__name__, type(e).__name__)
            try:
                conn.rollback()   # Postgres refuses every later statement until the failed one is rolled back
            except Exception:
                pass
    reviews_you = []
    try:   # proposal reviews: a request for the reviewer, the answer for the person who asked (they name each other)
        import proposal_review
        rv_items, reviews_you = proposal_review.bell(conn, uid, since)
        items += rv_items
    except Exception as e:
        log.warning("activity reviews skipped: %s", type(e).__name__)
        try:
            conn.rollback()
        except Exception:
            pass
    import activity_accepts
    waiting = activity_accepts.waiting(conn, uid, now)
    asked_clients = {w["id"] for w in waiting if w["kind"] == "client"}
    asked_signed = {w["proposal_id"] for w in waiting if w["kind"] == "project"}
    items = [i for i in items if i["id"] not in asked_clients and not (i["kind"] == "signed" and i.get("proposal_id") in asked_signed)]
    items.sort(key=lambda i: i["at"], reverse=True)
    items = items[:MAX_ITEMS]
    latest = items[0]["at"] if items else None
    fresh = []
    for it in items:
        at = _parse(it["at"])
        it["unread"] = at > read_at
        it["when"] = _when(at, now)
        it["group"] = "today" if at.date() == now.date() else "earlier"
        if it.pop("banner") and at > banner_at and now - at <= BANNER_MAX_AGE:
            fresh.append(it)
    banners = fresh[:MAX_BANNERS]
    if any(b["kind"] == "sent" for b in banners):
        _offer_undo(conn, uid, banners)
    for w in waiting:
        w["when"] = _when(_parse(w["at"]) or now, now)
    try:
        you = for_you(conn, uid, now, reviews_you)
    except Exception as e:   # the day's list failing must not take the news down
        log.warning("activity for-you skipped: %s", type(e).__name__)
        try:
            conn.rollback()
        except Exception:
            pass
        you = []
    try:
        good_morning = morning(conn, uid, now, you)
    except Exception as e:
        log.warning("activity morning skipped: %s", type(e).__name__)
        good_morning = None
    return {"items": items, "unread": sum(1 for i in items if i["unread"]), "banners": banners, "latest": latest,
            "waiting": [activity_accepts.public(w) for w in waiting], "for_you": you, "morning": good_morning,
            "badge": len(you) + len(waiting)}


def _offer_undo(conn, uid: int, banners: list) -> None:
    """A send closes this person's card the next time their cards are read, so read them now: the banner can then say so."""
    import inbox
    try:
        inbox.today_cards(conn, uid)
        closed = _closed_cards(conn, uid)
    except Exception as e:
        log.warning("activity undo skipped: %s", type(e).__name__)
        try:
            conn.rollback()
        except Exception:
            pass
        return
    for b in banners:
        action_id = closed.get(b.get("proposal_id")) if b["kind"] == "sent" else None
        if action_id:
            b["undo_action_id"] = action_id
            b["note"] = "Your “Proposal requested” card is closed."


def mark(conn, uid: int, what: str, through: str, now: Optional[datetime] = None) -> None:
    """Moves a mark forward only, never past now."""
    now = now or datetime.now().replace(microsecond=0)
    if what == "morning":   # through = the day it showed (local date); only today counts
        if str(through or "") != now.date().isoformat():
            raise ValueError("give the day the morning banner showed")
        _marks(conn, uid, now)
        conn.execute("UPDATE activity_seen SET morning_on = ? WHERE user_id = ?", (now.date().isoformat(), uid))
        conn.commit()
        return
    at = _parse(through)
    if not at or what not in ("read", "banners"):
        raise ValueError("give what = read, banners or morning, and a time")
    at = min(at, now)
    read_at, banner_at = _marks(conn, uid, now)
    if what == "read" and at > read_at:
        conn.execute("UPDATE activity_seen SET read_at = ? WHERE user_id = ?", (_stamp(at), uid))
    elif what == "banners" and at > banner_at:
        conn.execute("UPDATE activity_seen SET banner_at = ? WHERE user_id = ?", (_stamp(at), uid))
    conn.commit()


# ------------------------------------------------------------------ routes
def _with_conn(fn):
    conn = db_layer.connect()
    try:
        return fn(conn)
    finally:
        conn.close()


def register(app) -> None:
    try:
        _with_conn(ensure)
    except Exception as e:
        log.warning("activity_feed schema ensure skipped: %s", e)
    gate = requires_role("admin")

    @app.route("/api/activity", methods=["GET"])
    @gate
    def activity_list():
        uid = (current_user() or {}).get("id")
        return jsonify({"data": _with_conn(lambda c: build(c, uid))})

    @app.route("/api/activity/seen", methods=["POST"])
    @gate
    def activity_seen():
        uid = (current_user() or {}).get("id")
        body = request.get_json(silent=True) or {}
        try:
            _with_conn(lambda c: mark(c, uid, body.get("what"), body.get("through")))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"data": {"ok": True}})

    @app.route("/api/activity/decide", methods=["POST"])
    @gate
    def activity_decide():
        import activity_accepts
        uid = (current_user() or {}).get("id")
        body = request.get_json(silent=True) or {}
        try:
            data = _with_conn(lambda c: activity_accepts.decide(c, uid, str(body.get("id") or ""), str(body.get("action") or ""), body))
        except activity_accepts.Stale as e:
            return jsonify({"error": str(e)}), 409
        except LookupError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"data": data})

    @app.route("/api/activity/client-matches/<int:org_id>", methods=["GET"])
    @gate
    def activity_client_matches(org_id):
        import activity_accepts
        import crm
        try:
            return jsonify({"data": {"clients": _with_conn(lambda c: activity_accepts.client_matches(c, org_id)),
                                     "kinds": [{"code": k, "label": v} for k, v in crm.CLIENT_KINDS.items()]}})
        except LookupError as e:
            return jsonify({"error": str(e)}), 404
