"""The Today dashboard (accepted prototype 2026-10-19: https://claude.ai/artifact/N8QnPA4usnfXhWWWdqKPYK).

One read for the top of /company#overview, admin / c_suite only (the company_portal section):

  GET  /api/company/portal/today            -> {cards, day, needs}
  GET  /api/company/portal/today/weather    -> today's weather on the active job sites (slow: its own call)
  POST /api/company/portal/today/hide       -> {key, for: 'later' | 'good'} hides one "Needs you" card for this person

  cards  the four tiles: active jobs, on site now, weather (filled by the weather call), payments due
  day    "Your day": today in time order — this person's tasks / events / meetings, their private Google
         Calendar events (calendar_import.events_for, own uid only) and the bills due today
  needs  "Needs you": everything that wants a person today, each with where it lives (`href`) so a click opens
         that page. Claude's suggestions from the Inbox come in here too (inbox cards, incl. drafted proposals).

Nothing here changes a record: a card's buttons either open a page, prefill the Claude window (which proposes;
a person approves), or call routes that already exist. The page polls this every 60 s, so a new email that
Claude turned into a card shows up "Just now" without a reload.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from flask import jsonify, request

import db_layer
from auth import current_user, requires_section

log = logging.getLogger(__name__)

SECTION = "company_portal"
DOC_HORIZON_DAYS = 14          # documents and certificates show up two weeks ahead
BILL_SOON_DAYS = 7             # "Payments due" counts today + the next 7 days
LATER_HOURS = 4                # "Later" hides a card for 4 hours
CREW_CHECK_HOUR = 8            # missing sign-ins are flagged from 8 AM on weekdays
NYC = (40.7128, -74.0060)      # weather fallback when no active job has a site pin

TYPE_LABELS = {"FR": "Exterior Restoration", "IRA": "Rope Access", "IR": "Interior Repair",
               "PG": "Parking Garage", "OTH": "Other"}


# ------------------------------------------------------------------ helpers
def _rows(conn, sql, params=()) -> list:
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception as e:   # a fresh fixture may not have every table; the dashboard never breaks on that
        log.info("today_feed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return []


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt_time(t: Optional[str]) -> str:
    if not t:
        return ""
    try:
        h, m = int(t[:2]), int(t[3:5])
    except ValueError:
        return t
    return f"{h % 12 or 12}{':%02d' % m if m else ''} {'AM' if h < 12 else 'PM'}"


def _money(v) -> str:
    return "${:,.0f}".format(_num(v)) if abs(_num(v) - round(_num(v))) < 0.005 else "${:,.2f}".format(_num(v))


def _days_word(n: int) -> str:
    if n < 0:
        return f"{-n} day{'s' if n != -1 else ''} late"
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    return f"in {n} days"


def ensure(conn) -> None:
    """Idempotent: the per-person "Later" / "Dismiss" marks."""
    conn.execute("CREATE TABLE IF NOT EXISTS today_hidden (user_id INTEGER NOT NULL, item_key TEXT NOT NULL, "
                 "until_at TEXT, created_at TEXT)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_today_hidden ON today_hidden(user_id, item_key)")
    conn.commit()


def _hidden(conn, uid, now: datetime) -> set:
    if not uid:
        return set()
    n = now.isoformat(timespec="seconds")
    return {r["item_key"] for r in _rows(conn, "SELECT item_key, until_at FROM today_hidden WHERE user_id = ?", (uid,))
            if not r.get("until_at") or r["until_at"] > n}


def hide(conn, uid, key: str, mode: str, now: Optional[datetime] = None) -> None:
    now = now or datetime.now()
    until = (now + timedelta(hours=LATER_HOURS)).isoformat(timespec="seconds") if mode == "later" else None
    conn.execute("DELETE FROM today_hidden WHERE user_id = ? AND item_key = ?", (uid, key))
    conn.execute("INSERT INTO today_hidden (user_id, item_key, until_at, created_at) VALUES (?, ?, ?, ?)",
                 (uid, key, until, now.isoformat(timespec="seconds")))
    conn.commit()


def unhide(conn, uid, key: str) -> None:
    conn.execute("DELETE FROM today_hidden WHERE user_id = ? AND item_key = ?", (uid, key))
    conn.commit()


# ------------------------------------------------------------------ the four cards
def _active_jobs(conn) -> list:
    return _rows(conn, "SELECT project_code, name, site_lat, site_lng FROM projects "
                       "WHERE COALESCE(status, 'active') = 'active' ORDER BY project_code") or \
        _rows(conn, "SELECT project_code, name FROM projects WHERE COALESCE(status, 'active') = 'active' ORDER BY project_code")


def _type_of(code: str) -> str:
    head = (code or "").split("-")[0].upper()
    return head if head in TYPE_LABELS else "OTH"


def _previous_workday(d: date) -> date:
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return p


def crew(conn, today: date, now: datetime) -> dict:
    """Who is on site now (signed in today, not signed out) and who from the last workday's crew hasn't come in."""
    t = today.isoformat()
    rows = _rows(conn, "SELECT s.employee_id, s.project_code, s.time_in, s.time_out, p.name AS project_name "
                       "FROM sign_in_log s LEFT JOIN projects p ON p.project_code = s.project_code WHERE s.date = ?", (t,))
    signed_in = {r["employee_id"] for r in rows}
    now_on = {r["employee_id"] for r in rows if not (r.get("time_out") or "").strip()}
    by_site: dict = {}
    for r in rows:
        s = by_site.setdefault(r["project_code"], {"project_code": r["project_code"], "name": r.get("project_name") or r["project_code"],
                                                   "on_site": 0, "missing": []})
        if r["employee_id"] in now_on:
            s["on_site"] += 1
    missing_total = 0
    if today.weekday() < 5 and now.hour >= CREW_CHECK_HOUR:
        prev = _previous_workday(today).isoformat()
        for r in _rows(conn, "SELECT DISTINCT s.employee_id, s.project_code, e.name AS worker_name, p.name AS project_name "
                             "FROM sign_in_log s JOIN employees e ON e.employee_id = s.employee_id "
                             "LEFT JOIN projects p ON p.project_code = s.project_code "
                             "WHERE s.date = ? AND e.archived_at IS NULL AND COALESCE(p.status, 'active') = 'active'", (prev,)):
            if r["employee_id"] in signed_in:
                continue
            s = by_site.setdefault(r["project_code"], {"project_code": r["project_code"], "name": r.get("project_name") or r["project_code"],
                                                       "on_site": 0, "missing": []})
            if not any(m["employee_id"] == r["employee_id"] for m in s["missing"]):
                s["missing"].append({"employee_id": r["employee_id"], "name": r.get("worker_name") or r["employee_id"]})
                missing_total += 1
    sites = sorted(by_site.values(), key=lambda s: (-s["on_site"], s["project_code"]))
    return {"on_site_now": len(now_on), "signed_in_today": len(signed_in), "signed_out": len(signed_in - now_on),
            "missing": missing_total, "sites": sites}


def payments(conn, today: date) -> dict:
    """Bills to pay (payables): what is due today or late, and what is due in the next 7 days."""
    try:
        import payables
        rows = payables.list_payables(conn, status="open", limit=2000, today=today)
    except Exception as e:
        log.info("today_feed payables unavailable: %s", e)
        rows = []
    t, w = today.isoformat(), (today + timedelta(days=BILL_SOON_DAYS)).isoformat()
    now_rows = [r for r in rows if r.get("due_at") and r["due_at"] <= t]
    soon_rows = [r for r in rows if r.get("due_at") and t < r["due_at"] <= w]
    return {"due_now_value": round(sum(_num(r.get("balance")) for r in now_rows), 2), "due_now_count": len(now_rows),
            "overdue_count": sum(1 for r in now_rows if r.get("overdue")),
            "soon_value": round(sum(_num(r.get("balance")) for r in soon_rows), 2), "soon_count": len(soon_rows),
            "_rows": now_rows + soon_rows}


def cards(conn, today: date, now: datetime) -> dict:
    jobs = _active_jobs(conn)
    by_type: dict = {}
    for j in jobs:
        k = _type_of(j["project_code"])
        by_type[k] = by_type.get(k, 0) + 1
    c = crew(conn, today, now)
    p = payments(conn, today)
    return {
        "jobs": {"active": len(jobs), "by_type": [{"code": k, "label": TYPE_LABELS[k], "n": n} for k, n in sorted(by_type.items())]},
        "crew": {k: v for k, v in c.items()},
        "payments": {k: v for k, v in p.items() if not k.startswith("_")},
    }


# ------------------------------------------------------------------ weather (its own call: one forecast per site)
def weather(conn, today: date, fetch=None) -> dict:
    """Today on each active site with a pin (max 6), plus one headline for the card. `fetch(lat, lng)` is
    weather_week.fetch_week by default; the smoke passes a canned one (no network in tests)."""
    if fetch is None:
        import sys
        import weather_week
        # the WMO labels live next to the job-page weather route; never `import server` here (run as a
        # script locally it is __main__, and importing it again would build a second app)
        srv = sys.modules.get("server") or sys.modules.get("__main__")
        labels = getattr(srv, "_WMO_LABELS", None)
        fetch = lambda lat, lng: weather_week.fetch_week(lat, lng, labels)   # noqa: E731
    t = today.isoformat()
    sites, seen = [], set()
    for j in _active_jobs(conn):
        lat, lng = j.get("site_lat"), j.get("site_lng")
        if lat is None or lng is None:
            continue
        key = (round(float(lat), 2), round(float(lng), 2))
        if key in seen:
            continue
        seen.add(key)
        sites.append({"project_code": j["project_code"], "name": j.get("name") or j["project_code"], "lat": float(lat), "lng": float(lng)})
        if len(sites) >= 6:
            break
    pinned = bool(sites)
    if not sites:
        sites = [{"project_code": None, "name": "New York City", "lat": NYC[0], "lng": NYC[1]}]
    out = []
    for s in sites:
        try:
            wk = fetch(s["lat"], s["lng"])
        except Exception as e:
            log.info("today_feed weather %s: %s", s["project_code"], e)
            continue
        day = next((d for d in (wk.get("days") or []) if d.get("date") == t), None)
        if not day:
            continue
        rain_from = next((h["hour"] for h in day.get("hours") or [] if (h.get("precip_prob") or 0) >= 50), None)
        out.append({"project_code": s["project_code"], "name": s["name"], "label": day.get("label"),
                    "temp_max": day.get("temp_max"), "temp_min": day.get("temp_min"), "gust_mph": day.get("gust_mph"),
                    "precip_prob": day.get("precip_prob"), "rain_from": rain_from, "heads_up": day.get("heads_up") or [],
                    "hours": day.get("hours") or [], "stale": bool(wk.get("stale")),
                    "wind_mph": day.get("wind_mph"), "wind_dir": day.get("wind_dir"), "precip_in": day.get("precip_in"),
                    "humidity": day.get("humidity"), "sunrise": day.get("sunrise"), "sunset": day.get("sunset"),
                    # the next 7 days for the Weather window (a click on the Weather card)
                    "week": [{k: d.get(k) for k in ("date", "weekday", "is_today", "label", "temp_max", "temp_min",
                                                    "precip_prob", "gust_mph", "heads_up")}
                             for d in (wk.get("days") or []) if (d.get("date") or "") >= t][:7]})
    if not out:
        return {"available": False, "sites": [], "pinned": pinned}
    now_h = datetime.now().hour
    first = out[0]
    cur = None
    for h in first["hours"]:
        if h.get("hour") is not None and h["hour"] <= max(now_h, first["hours"][0]["hour"]):
            cur = h
    head = []
    if first.get("rain_from") is not None:
        head.append(f"Rain from {_fmt_time('%02d:00' % first['rain_from'])}")
    if first.get("gust_mph"):
        head.append(f"gusts {round(first['gust_mph'])} mph")
    return {"available": True, "pinned": pinned, "temp_now": (cur or {}).get("temp"), "label": first.get("label"),
            "headline": " · ".join(head) or f"High {round(first['temp_max'])}°" if first.get("temp_max") is not None else "",
            "warn": any(s["heads_up"] for s in out), "sites": out}


# ------------------------------------------------------------------ Your day
def your_day(conn, uid, today: date, pay_rows: list) -> list:
    t = today.isoformat()
    out = []
    try:
        import team_items
        items = team_items.list_items(conn, status="open", mine_uid=uid, date_from=t, date_to=t, me=uid, can_edit=True)
    except Exception as e:
        log.info("today_feed items: %s", e)
        items = []
    for it in items:
        timed = it.get("start_time") or it.get("due_time")
        kind = it["kind"]
        where = it.get("location") or it.get("address") or it.get("project_name") or it.get("org_name") or ""
        out.append({"key": f"item:{it['id']}", "time": timed, "all_day": not timed, "kind": kind,
                    "title": ("Due: " if kind == "task" else "") + (it.get("title") or ""),
                    "sub": where, "address": it.get("address"), "directions_url": it.get("directions_url"),
                    "meet_url": it.get("meet_url"), "end_time": it.get("end_time"), "item_id": it["id"],
                    "href": "#calendar/" + t})
    try:
        import calendar_import
        for ev in calendar_import.events_for(conn, uid, t, t):
            out.append({"key": f"gcal:{ev['id']}", "time": ev.get("start_time") if ev.get("start_date") == t else None,
                        "all_day": not ev.get("start_time"), "kind": "calendar", "title": ev.get("title") or "(no title)",
                        "sub": ev.get("location") or "", "end_time": ev.get("end_time"), "html_link": ev.get("html_link"),
                        "href": ev.get("html_link") or "#calendar/" + t, "private": True})
    except Exception as e:
        log.info("today_feed calendar: %s", e)
    for r in pay_rows:
        if r.get("due_at") == t:
            out.append({"key": f"bill:{r['id']}", "time": None, "all_day": True, "kind": "bill",
                        "title": f"Bill due · {r.get('vendor') or 'vendor'}", "sub": _money(r.get("balance")), "href": "#finance"})
    out.sort(key=lambda x: (0 if x["all_day"] else 1, x["time"] or ""))
    return out


# ------------------------------------------------------------------ Needs you
def _card(key, cat, urgency, title, why, *, href, source, pill=None, icon=None, actions=None, draft=None, when=None, **extra):
    d = {"key": key, "cat": cat, "urgency": urgency, "title": title, "why": why, "href": href, "source": source,
         "pill": pill, "icon": icon or cat.split(" ")[0], "actions": actions or [], "draft": draft, "when": when}
    d.update(extra)
    return d


def _renew_hint(it: dict) -> str:
    k = it["kind"]
    if k == "worker_cert":
        return "Book the renewal class or test, then upload the new card on the worker's page."
    if k == "permit":
        return f"Renew it with {it.get('group') or 'the issuing agency'}, then upload the new permit on the job."
    if k == "vehicle":
        return "Renew it, then upload the new document under Company › Documents & vehicles."
    if k in ("project_document", "estimate_document"):
        return "Ask the issuer for the renewed copy, then upload it on the job."
    return "Ask the issuer (broker, agency or vendor) for the renewed copy, then upload it under Company › Documents."


def _doc_href(it: dict) -> str:
    k, ref = it["kind"], it.get("subject_ref")
    if k == "worker_cert" and ref:
        return "#workforce/" + str(ref)
    if k in ("permit", "project_document") and ref:
        return "/projects/" + str(ref)
    if k == "estimate_document":
        return "#proposals"
    return "#company"


PROPOSAL_EXPIRY_DAYS = 7       # a sent proposal shows up a week before its valid-until date
PROPOSAL_LAPSED_DAYS = 14      # ... and for two weeks after it lapsed without an answer
REVISION_NEWS_DAYS = 7         # the person who asked for a revision sees the answer for a week (or until dismissed)


def _us(iso) -> str:
    """MM/DD/YYYY for people (DESIGN_SYSTEM: display MM/DD/YYYY, store ISO)."""
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso or "")
    return f"{d.month:02d}/{d.day:02d}/{d.year}"


def _proposal_href(r: dict) -> str:
    return f"/proposal-studio#p/{r['id']}" if r.get("layout") == "ssc" else f"#proposals/{r['id']}"


def proposal_cards(conn, uid, today: date) -> list:
    """Proposals that need a person. Nothing here sends anything: "Draft a follow-up" opens the Claude window with the
    request typed in, and whatever Claude writes is a draft the person approves (claude_agent cards)."""
    import proposals
    me = (_rows(conn, "SELECT role FROM users WHERE id = ?", (uid,)) or [{}])[0]
    admin = me.get("role") == "admin"
    out = []
    lo, hi = (today - timedelta(days=PROPOSAL_LAPSED_DAYS)).isoformat(), (today + timedelta(days=PROPOSAL_EXPIRY_DAYS)).isoformat()
    # 8. sent, no answer yet, valid-until close (or just passed): the person who sent it, and the admins, follow up
    for r in _rows(conn, "SELECT p.id, p.number, p.display_no, p.version, p.layout, p.status, p.title, p.price, p.valid_until, "
                         "p.sent_at, p.sent_by, p.recipient_name, o.name AS client_name FROM proposal p "
                         "JOIN crm_organization o ON o.id = p.client_org_id WHERE p.status IN ('sent','viewed') "
                         "AND p.valid_until IS NOT NULL AND p.valid_until >= ? AND p.valid_until <= ?", (lo, hi)):
        if not (admin or r.get("sent_by") in (None, uid)):
            continue
        if _rows(conn, "SELECT id FROM proposal WHERE revision_of = ? AND status = 'draft'", (r["id"],)):
            continue   # a new version is already being made: the follow-up is that version
        n = proposals.expires_in_days(r, today)
        no = proposals._label(r)
        client = r.get("client_name") or "the client"
        seen = "opened it, no answer yet" if r["status"] == "viewed" else "hasn't opened it yet"
        when = "expired " + _days_word(n).replace(" late", " ago") if n < 0 else "expires " + _days_word(n)
        out.append(_card(f"proposal-expiry:{r['id']}:{r['valid_until']}", "money email",
                         "now" if n <= 1 else ("today" if n <= 3 else "week"),
                         f"Proposal {no} {when} · {_money(r.get('price'))}",
                         f"{client} {seen}. Sent {_us(r.get('sent_at'))}, valid until {_us(r['valid_until'])}. "
                         + ("Follow up, or make a new version with fresh dates." if n < 0 else "Follow up before it lapses."),
                         href=_proposal_href(r), source="Proposals", pill=("Expired " if n < 0 else "Valid until ") + _us(r["valid_until"]),
                         icon="cash", proposal_id=r["id"],
                         actions=[{"type": "open", "label": "Open proposal", "primary": True},
                                  {"type": "claude", "label": "Draft a follow-up",
                                   "prompt": f"Draft a short, friendly follow-up email to {client} about proposal {no} "
                                             f"({r.get('title') or 'our proposal'}, {_money(r.get('price'))}), sent "
                                             f"{_us(r.get('sent_at'))} and valid until {_us(r['valid_until'])}. Reply in their "
                                             f"latest email thread if there is one. Show me the draft; don't send it."}]))
    # 9. revisions waiting for an admin (only admins can decide them)
    if admin:
        for v in proposals.pending_revisions(conn):
            r = proposals.get(conn, v["proposal_id"]) or {}
            what = "Price review" if v["price_review"] else "Revision"
            out.append(_card(f"proposal-revision:{v['id']}", "money", "today",
                             f"{what} requested on proposal {v['label']} · {v.get('client_name') or ''}".rstrip(" ·"),
                             f"{v.get('requested_by_name') or 'Someone'}: {v['reason'][:220]}"
                             + (f" Proposed price {_money(v['proposed_price'])}." if v.get("proposed_price") else ""),
                             href=_proposal_href(dict(r, id=v["proposal_id"])), source="Proposals › Revisions",
                             pill="Waiting for an admin", icon="task", proposal_id=v["proposal_id"],
                             actions=[{"type": "open", "label": "Review it", "primary": True}], when=v["requested_at"]))
    # 10. the answer to a revision this person asked for
    since = (today - timedelta(days=REVISION_NEWS_DAYS)).isoformat()
    for x in _rows(conn, "SELECT id FROM proposal_revision WHERE requested_by = ? AND status IN ('approved','rejected') "
                         "AND decided_by <> requested_by AND decided_at >= ?", (uid, since)):
        v = proposals.revision_public(proposals._revision_row(conn, x["id"]))
        ok = v["status"] == "approved"
        target = proposals.get(conn, v["new_proposal_id"] or v["proposal_id"]) or {}
        out.append(_card(f"proposal-revision-answer:{v['id']}", "money", "today",
                         f"Revision of proposal {v['label']} {'approved' if ok else 'rejected'}",
                         (f"{v.get('decided_by_name') or 'An admin'}: {v['decision_note']}" if v.get("decision_note") else
                          f"Decided by {v.get('decided_by_name') or 'an admin'}.")
                         + (" The new version is ready to edit and send." if ok else " The sent proposal stays as it was."),
                         href=_proposal_href(dict(target, id=v["new_proposal_id"] or v["proposal_id"])),
                         source="Proposals › Revisions", pill="Approved" if ok else "Rejected", icon="task",
                         proposal_id=v["new_proposal_id"] or v["proposal_id"],
                         actions=[{"type": "open", "label": "Open the new version" if ok else "Open proposal", "primary": True}],
                         when=v["decided_at"]))
    return out


def needs(conn, uid, today: date, now: datetime, pay_rows: list, crew_info: dict) -> list:
    t = today.isoformat()
    out = []

    # 1. Claude's suggestions from the Inbox (emails turned into replies, proposals, tasks…)
    try:
        import inbox
        for c in inbox.today_cards(conn, uid):
            out.append(c)
    except Exception as e:
        log.info("today_feed inbox cards: %s", e)

    # 2. tasks that are late or due today (mine)
    try:
        import team_items
        for it in team_items.list_items(conn, status="open", mine_uid=uid, me=uid, can_edit=True, kind="task"):
            due = it.get("due_date")
            if not due or due > t:
                continue
            late = due < t
            out.append(_card(f"task:{it['id']}", "task", "now" if late else "today", it.get("title") or "Task",
                             (it.get("detail") or "") [:240] or ("Was due " + due if late else "Due today" + (f" at {_fmt_time(it.get('due_time'))}" if it.get("due_time") else "")),
                             href="#calendar/" + due, source="Tasks & events",
                             pill=("Late since " + due) if late else ("Due " + (_fmt_time(it.get("due_time")) or "today")),
                             icon="task", item_id=it["id"],
                             actions=[{"type": "done", "label": "Mark done", "primary": True}, {"type": "open", "label": "Open"}]))
    except Exception as e:
        log.info("today_feed tasks: %s", e)

    # 3. bills due today / late, and due in the next 7 days
    for r in pay_rows:
        due = r.get("due_at")
        n = (date.fromisoformat(due) - today).days
        who = r.get("vendor") or "Vendor"
        what = r.get("description") or r.get("doc_number") or ""
        out.append(_card(f"bill:{r['id']}", "money", "now" if n < 0 else ("today" if n == 0 else "week"),
                         f"{who} bill due {_days_word(n) if n >= 0 else '(' + _days_word(n) + ')'} · {_money(r.get('balance'))}",
                         what or "Open bill in Finance.", href="#finance", source="Finance › What needs to get paid",
                         pill=("Late" if n < 0 else "Due " + _days_word(n)), icon="bill",
                         actions=[{"type": "open", "label": "Pay or mark paid", "primary": True}]))

    # 3b. #316 1b: clients created from QuickBooks, waiting for an admin to review (admins only)
    try:
        import qbo
        if qbo.show_imported() and qbo.is_connected(conn):
            link = qbo.connection(conn)
            n = qbo.review_count(conn, link["realm_id"])
            if n:
                out.append(_card("qbo:review", "money", "today", f"{n} new client{'s' if n != 1 else ''} from QuickBooks to review",
                                 "Created automatically when their invoices came in from QuickBooks. Approve each one, or merge it into a client you already have.",
                                 href="#finance/qb-review", source="Finance › QuickBooks", pill="To review", icon="doc",
                                 actions=[{"type": "open", "label": "Review", "primary": True}]))
    except Exception as e:
        log.info("today_feed quickbooks review: %s", e)

    # 4. invoices overdue (money owed to us)
    try:
        import invoices
        for r in invoices.list_invoices(conn, status="open", limit=500, today=today):
            if not r.get("overdue"):
                continue
            late = (today - date.fromisoformat(r["due_at"])).days if r.get("due_at") else 0
            client = r.get("client_name") or "the client"
            num = r.get("number") or f"#{r['id']}"
            out.append(_card(f"invoice:{r['id']}", "money email", "week",
                             f"Invoice {num} is {late} day{'s' if late != 1 else ''} overdue · {_money(r.get('balance'))}",
                             f"{client}" + (f" · {r.get('project_name')}" if r.get("project_name") else ""),
                             href="#finance/overdue", source="Finance › Invoices", pill=f"{late} days late", icon="cash",
                             actions=[{"type": "claude", "label": "Draft a follow-up", "primary": True,
                                       "prompt": f"Draft a short, polite payment follow-up email to {client} about invoice {num} "
                                                 f"({_money(r.get('balance'))}, due {r.get('due_at')}). Reply in their latest email thread if there is one."},
                                      {"type": "open", "label": "Open invoice"}]))
    except Exception as e:
        log.info("today_feed invoices: %s", e)

    # 5. documents, permits and certificates: one card each when they expire within two weeks; the ones
    #    already expired are one card per kind (a long-expired list must not bury today's work)
    try:
        import company_portal
        items = company_portal.compliance(conn, today=today, horizon_days=DOC_HORIZON_DAYS)["items"]
        expired: dict = {}
        for it in items:
            if it.get("days_left") is not None and it["days_left"] < 0:
                expired.setdefault(it["kind"], []).append(it)
        for kind, its in expired.items():
            if len(its) == 1:
                continue   # a single one keeps its own card below
            names = ", ".join(sorted({(i.get("subject") or i["title"]) for i in its})[:3])
            label = {"worker_cert": "worker certifications", "permit": "permits", "vehicle": "vehicle documents",
                     "project_document": "job documents", "estimate_document": "bid documents"}.get(kind, "company documents")
            out.append(_card(f"docs-expired:{kind}:{len(its)}", "doc" + (" crew" if kind == "worker_cert" else ""), "now",
                             f"{len(its)} {label} have expired", f"Including {names}. " + _renew_hint(its[0]),
                             href={"worker_cert": "/#cert-health", "permit": "/#compliance", "project_document": "/#compliance",
                                   "estimate_document": "#proposals"}.get(kind, "#company"),
                             source={"worker_cert": "Workforce › Certifications"}.get(kind, "Company › Documents"),
                             pill=f"Oldest {min(i['expires_on'] for i in its)}", icon="doc",
                             actions=[{"type": "open", "label": "Review them", "primary": True}]))
        for it in items:
            n = it.get("days_left")
            if n is None or (n < 0 and len(expired.get(it["kind"], [])) > 1):
                continue
            subj = it.get("subject") or ""
            title = f"{it['title']}" + (f" · {subj}" if subj and it["kind"] != "company_document" else "")
            out.append(_card(f"doc:{it['kind']}:{it['id']}", "doc" + (" crew" if it["kind"] == "worker_cert" else ""),
                             "now" if n < 0 else ("today" if n <= 3 else "week"),   # expired = overdue: do it first
                             (title + (" expired" if n < 0 else f" expires {_days_word(n)}")),
                             _renew_hint(it), href=_doc_href(it),
                             source={"worker_cert": "Workforce › Certifications", "permit": "Job › Permits",
                                     "project_document": "Job › Documents", "estimate_document": "Proposals",
                                     "vehicle": "Company › Vehicles"}.get(it["kind"], "Company › Documents"),
                             pill=("Expired " + it["expires_on"]) if n < 0 else (f"{n} day{'s' if n != 1 else ''} left" if n else "Expires today"),
                             icon="doc",
                             actions=[{"type": "claude", "label": "Help me renew it", "primary": True,
                                       "prompt": f"{title} expires on {it['expires_on']}. Tell me how to renew it and draft the email "
                                                 f"to whoever issues it (broker, agency or vendor) if one is needed."},
                                      {"type": "open", "label": "Open"}]))
    except Exception as e:
        log.info("today_feed documents: %s", e)

    # 6. crew from the last workday who haven't signed in today
    for s in crew_info.get("sites") or []:
        miss = s.get("missing") or []
        if not miss:
            continue
        names = ", ".join(m["name"] for m in miss[:4]) + (f" and {len(miss) - 4} more" if len(miss) > 4 else "")
        out.append(_card(f"crew:{s['project_code']}:{t}", "crew field", "today",
                         f"{len(miss)} worker{'s' if len(miss) != 1 else ''} not signed in at {s['project_code']}",
                         f"{names} worked there on the last workday and haven't signed in today.",
                         href="/projects/" + s["project_code"], source="Worker app sign-ins",
                         pill="Since " + _fmt_time("%02d:00" % CREW_CHECK_HOUR), icon="crew",
                         actions=[{"type": "open", "label": "Open the job", "primary": True}]))

    # 8-10. proposals: about to expire, revisions waiting for an admin, revisions just decided (2026-10-22)
    try:
        out += proposal_cards(conn, uid, today)
    except Exception as e:
        log.info("today_feed proposals: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass

    # 7. the old "Needs attention" lines, now cards
    rec = _rows(conn, "SELECT COUNT(1) AS n FROM expenses WHERE status = 'needs_review'")
    if rec and int(rec[0].get("n") or 0):
        n = int(rec[0]["n"])
        out.append(_card("receipts:review", "money", "week", f"{n} receipt{'s' if n != 1 else ''} need review",
                         "Scanned receipts waiting for a job and a category.", href="#expenses", source="Finance › Expenses",
                         icon="bill", actions=[{"type": "open", "label": "Review", "primary": True}]))
    try:
        import invoices
        s = invoices.receivables_summary(conn, today=today)
        if s.get("draft_count"):
            n = int(s["draft_count"])
            out.append(_card("invoices:draft", "money", "week", f"{n} draft invoice{'s' if n != 1 else ''} waiting to be issued · {_money(s.get('draft_value'))}",
                             "Issue them so the clock starts on payment.", href="#finance/draft", source="Finance › Invoices",
                             icon="cash", actions=[{"type": "open", "label": "Issue", "primary": True}]))
    except Exception as e:
        log.info("today_feed draft invoices: %s", e)
    try:
        import field_videos
        st = field_videos.storage()
        if st.get("warn"):
            out.append(_card("storage:warn", "field", "week", f"File storage {st['used_pct']}% full",
                             "Photos and videos from the field use the most space.", href="#usage", source="Settings",
                             icon="doc", actions=[]))
    except Exception:
        pass

    hidden = _hidden(conn, uid, now)
    order = {"now": 0, "today": 1, "week": 2}
    out = [c for c in out if c["key"] not in hidden]
    # Claude's suggestions lead each group (they are the newest work), then by date
    out.sort(key=lambda c: (order.get(c["urgency"], 3), 0 if c.get("action_id") else 1, c.get("when") or ""))
    return out


def build(conn, uid, today: Optional[date] = None, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now()
    today = today or now.date()
    c = cards(conn, today, now)
    p = payments(conn, today)
    return {"as_of": today.isoformat(), "now": now.isoformat(timespec="minutes"), "cards": c,
            "day": your_day(conn, uid, today, p["_rows"]),
            "needs": needs(conn, uid, today, now, p["_rows"], c["crew"])}


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
        log.warning("today_feed schema ensure skipped: %s", e)
    gate = requires_section(SECTION)

    @app.route("/api/company/portal/today", methods=["GET"])
    @gate
    def company_today():
        uid = (current_user() or {}).get("id")
        return jsonify({"data": _with_conn(lambda c: build(c, uid))})

    @app.route("/api/company/portal/today/weather", methods=["GET"])
    @gate
    def company_today_weather():
        return jsonify({"data": _with_conn(lambda c: weather(c, date.today()))})

    @app.route("/api/company/portal/today/hide", methods=["POST"])
    @gate
    def company_today_hide():
        uid = (current_user() or {}).get("id")
        body = request.get_json(silent=True) or {}
        key = str(body.get("key") or "").strip()[:200]
        mode = body.get("for") or "good"
        if not key or mode not in ("later", "good", "undo"):
            return jsonify({"error": "give a card key and for = later, good or undo"}), 400
        if mode == "undo":
            _with_conn(lambda c: unhide(c, uid, key))
        else:
            _with_conn(lambda c: hide(c, uid, key, mode))
        return jsonify({"data": {"key": key, "for": mode}})
