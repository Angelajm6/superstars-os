"""Labels in Gmail (prototype accepted 2026-11-09: https://claude.ai/artifact/AD3UWCWVrSTcdKFN1grgyk).

The Inbox already sorts every work email (inbox.process: kind, summary, needs action). With this switched on, the same
sorting shows in the person's own Gmail, on the web and on the phone, as plain labels (no "SSC/" prefix, no labels per
job; the mailbox is the company's, so a label needs no company name):

  New bid · Proposal reply · Client question · Scheduling · Documents · Payment · Permit & notice · Vendor · Internal
  For <owner's first name>   needs a decision, approval, signature or payment OK from the owner the person picked
  Needs reply                someone at Superstars should answer; comes off once anyone at Superstars answers
                             (a reply from this mailbox or from a colleague in the conversation), or the email is Done
  FYI (cc)                   the person was only copied (or Bcc / a list) and nothing is asked

  * Received email only; personal mail, receipts and automatic notices get no label. Labels only tag: nothing is
    archived, deleted or sent. A Gmail label with the same name the person already has is used, not doubled.
  * Off until the person switches it on (Settings > Account), for their own mailbox only. Needs Gmail's modify
    permission (the Inbox's connection). Switching it on labels the email already sorted.
  * "Label my last 90 days": a one-time pass over older inbox + sent mail. Older email is sorted quickly (Claude's
    small model only, no cards, never "Needs reply" in the app) and gets its kind and FYI (cc); "For <owner>" and
    "Needs reply" are for email from now on. New mail always goes first.
  * "Remove all labels": every label this app made is deleted in Gmail; a label of the person's own that was used is
    only taken off the email this app put it on.
  * What was put on each email is kept (mail_message.gmail_labels), so a change takes off exactly what this app added.

In the app too (prototype accepted 2026-11-10: https://claude.ai/artifact/AkGYSYCVVwKZQPxHiP8Ywt): every admin and
C-suite person sees the kind, FYI and "For" labels on their own email in the Inbox tab, whether or not their Gmail gets
them (app_labels / thread_labels). The Inbox list has a "For <owner>" filter and a Label menu; an open email's label can
be fixed by hand (set_labels), and the sorting never changes a label someone fixed (mail_message.label_manual). A C-suite
person who picked nobody sees "Needs my OK": the email that needs the owner's OK is email that needs theirs.
"Sort my last 90 days" works with the Gmail switch off (the app's labels fill in; Gmail only when the switch is on).

Logs carry ids and counts only, never names, subjects or addresses.

  GET  /api/inbox/labels                  switch, owner + choices, the labels, the 90-day pass's progress
  PUT  /api/inbox/labels                  {enabled?, owner_user_id?}
  POST /api/inbox/labels/backfill         start "Sort my last 90 days"
  POST /api/inbox/labels/remove           take every label this app added off Gmail (and switch it off)
  POST /api/inbox/messages/<id>/label     {kind?, owner?}: fix a conversation's label by hand
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from email.utils import getaddresses, parseaddr
from typing import Optional

from flask import jsonify, request

import db_layer
from auth import current_user, requires_role

log = logging.getLogger("ssc.gmail_labels")

OFFICE_ROLES = ("admin", "c_suite")
BACKFILL_DAYS = 90
BACKFILL_MAX = 3000
BACKFILL_QUERY = (f"newer_than:{BACKFILL_DAYS}d (in:inbox OR in:sent) "
                  "-category:promotions -category:social -category:forums")
BACKFILL_PER_ROUND = 30

KIND_LABELS = {"bid_invitation": "New bid", "proposal_reply": "Proposal reply", "client_question": "Client question",
               "scheduling": "Scheduling", "documents": "Documents", "payment": "Payment",
               "permit_notice": "Permit & notice", "vendor": "Vendor", "internal": "Internal"}
ORDER = ("owner", "reply") + tuple(KIND_LABELS) + ("fyi",)
# Gmail only takes colours from its own palette; a label it refuses a colour for is made without one.
COLORS = {"owner": ("#4a86e8", "#ffffff"), "reply": ("#fb4c2f", "#ffffff"), "fyi": ("#efefef", "#666666")}
KIND_COLOR = ("#c9daf8", "#1c4587")

_RUNNING: dict = {}   # user id -> the 90-day pass thread
_LOCK = threading.Lock()
_MAKE_LOCK = threading.RLock()


# ------------------------------------------------------------------ schema
def _has_column(conn, table, column) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                                 "AND table_name = ? AND column_name = ?", (table, column)).fetchone())
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def ensure(conn) -> None:
    """Idempotent. mail_message gets what the labels are decided from (inbox.ensure creates the table first)."""
    pk = ("id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" if db_layer.is_postgres()
          else "id INTEGER PRIMARY KEY AUTOINCREMENT")
    conn.execute("CREATE TABLE IF NOT EXISTS mail_label_pref (user_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, "
                 "owner_user_id INTEGER, backfill_state TEXT, backfill_started_at TEXT, backfill_error TEXT, updated_at TEXT)")
    conn.execute(f"CREATE TABLE IF NOT EXISTS mail_label_map ({pk}, user_id INTEGER NOT NULL, label_key TEXT NOT NULL, "
                 f"gmail_label_id TEXT NOT NULL, name TEXT NOT NULL, created INTEGER NOT NULL DEFAULT 0, created_at TEXT)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mail_label_map ON mail_label_map(user_id, label_key)")
    # addressed: how this mailbox was on the email (from / to / cc / other); from_team: written by someone at Superstars;
    # business / triage_needs / for_owner: the sorting's answers as given (needs_action is the app's own list);
    # backfilled_at: came in through "Label my last 90 days"; gmail_labels: the label keys this app put on it in Gmail
    # label_manual: someone fixed this email's label by hand; the sorting leaves kind / for_owner alone from then on
    for col, typ in (("addressed", "TEXT"), ("from_team", "INTEGER"), ("business", "INTEGER"), ("triage_needs", "INTEGER"),
                     ("for_owner", "INTEGER"), ("backfilled_at", "TEXT"), ("gmail_labels", "TEXT"), ("label_manual", "INTEGER")):
        if not _has_column(conn, "mail_message", col):
            conn.execute(f"ALTER TABLE mail_message ADD COLUMN {col} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_ai_state ON mail_message(ai_state)")
    conn.commit()


def _conn():
    return db_layer.connect(pragma_fk=True)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")   # LOCAL


def _one(conn, sql, params=()) -> Optional[dict]:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def _rows(conn, sql, params=()) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ------------------------------------------------------------------ who the email was for
def addressed(own_email: str, frm: str, to: str, cc: str) -> str:
    """'from' (this mailbox wrote it), 'to', 'cc', or 'other' (Bcc, a group or list address)."""
    me = (own_email or "").strip().lower()
    if not me:
        return "to"
    if (parseaddr(frm or "")[1] or "").strip().lower() == me:
        return "from"
    if me in [a.lower() for _n, a in getaddresses([to or ""]) if a]:
        return "to"
    if me in [a.lower() for _n, a in getaddresses([cc or ""]) if a]:
        return "cc"
    return "other"


def from_team(own_email: str, frm: str) -> bool:
    """Written by someone at the company (the mailbox's own domain), this mailbox included."""
    dom = own_email.split("@")[-1].lower() if "@" in (own_email or "") else ""
    sender = (parseaddr(frm or "")[1] or "").strip().lower()
    return bool(dom) and sender.endswith("@" + dom)


# ------------------------------------------------------------------ settings
def pref(conn, uid: int) -> dict:
    return _one(conn, "SELECT * FROM mail_label_pref WHERE user_id = ?", (uid,)) or {
        "user_id": uid, "enabled": 0, "owner_user_id": None, "backfill_state": None, "backfill_started_at": None,
        "backfill_error": None}


def _save_pref(conn, uid: int, **vals) -> None:
    vals["updated_at"] = _now()
    if _one(conn, "SELECT user_id FROM mail_label_pref WHERE user_id = ?", (uid,)):
        conn.execute("UPDATE mail_label_pref SET " + ", ".join(f"{k} = ?" for k in vals) + " WHERE user_id = ?",
                     list(vals.values()) + [uid])
    else:
        cols = ["user_id"] + list(vals)
        conn.execute(f"INSERT INTO mail_label_pref ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [uid] + list(vals.values()))
    conn.commit()


def owner_person(conn, uid: int) -> Optional[dict]:
    p = pref(conn, uid)
    if not p.get("owner_user_id"):
        return None
    u = _one(conn, "SELECT id, full_name, display_name FROM users WHERE id = ? AND status = 'active'", (p["owner_user_id"],))
    if not u:
        return None
    name = (u.get("full_name") or u.get("display_name") or "").strip()
    return {"id": u["id"], "name": name, "first": name.split()[0] if name else ""}


def owner_label(conn, uid: int) -> str:
    o = owner_person(conn, uid)
    if o and o["first"]:
        return f"For {o['first']}"
    u = _one(conn, "SELECT role FROM users WHERE id = ?", (uid,)) or {}
    return "Needs my OK" if u.get("role") == "c_suite" else "For the owner"


def owner_choices(conn, uid: int) -> list:
    return [{"id": r["id"], "name": (r["full_name"] or r["display_name"] or "").strip()}
            for r in _rows(conn, "SELECT id, full_name, display_name FROM users WHERE role IN ('admin', 'c_suite') "
                                 "AND status = 'active' AND COALESCE(is_system, 0) = 0 AND id <> ? ORDER BY full_name", (uid,))
            if (r["full_name"] or r["display_name"] or "").strip()]


def names(conn, uid: int) -> dict:
    out = {"owner": owner_label(conn, uid), "reply": "Needs reply", "fyi": "FYI (cc)"}
    out.update(KIND_LABELS)
    return out


def can_label(conn, uid: int) -> bool:
    import inbox
    return bool(pref(conn, uid).get("enabled")) and inbox.can_delete(inbox._link(conn, uid))


# ------------------------------------------------------------------ which labels an email gets
def _team_answered(conn, m: dict) -> bool:
    """Someone at Superstars wrote in this conversation after this email (this mailbox or a colleague)."""
    if not m.get("thread_id"):
        return False
    return bool(_one(conn, "SELECT id FROM mail_message WHERE user_id = ? AND thread_id = ? AND id <> ? AND received_at >= ? "
                           "AND (direction = 'out' OR from_team = 1) LIMIT 1",
                     (m["user_id"], m["thread_id"], m["id"], m.get("received_at") or "")))


def app_labels(m: dict) -> dict:
    """The labels one email shows, in the app and in Gmail ("Needs reply" aside): {kind, owner, fyi}."""
    out = {"kind": None, "owner": False, "fyi": False}
    if m.get("direction") != "in" or m.get("trashed_at") or m.get("kind") is None or m.get("business") == 0:
        return out   # personal mail, receipts and automatic notices: no label
    if m["kind"] in KIND_LABELS:
        out["kind"] = m["kind"]
    out["owner"] = bool(m.get("for_owner")) and (not m.get("backfilled_at") or bool(m.get("label_manual")))
    asked = m.get("triage_needs") if m.get("triage_needs") is not None else m.get("needs_action")
    out["fyi"] = m.get("addressed") in ("cc", "other") and not asked and not m.get("for_owner")
    return out


def thread_labels(msgs: list) -> dict:
    """A conversation's labels (msgs newest first): the newest sorted kind, "For" when any email has it, FYI from the
    newest email received. 'mid' is the email a fix by hand changes."""
    ins = [x for x in msgs if x.get("direction") == "in" and not x.get("trashed_at")]
    out = {"kind": None, "owner": False, "fyi": False, "mid": ins[0]["id"] if ins else None}
    for x in ins:
        lab = app_labels(x)
        if lab["kind"] and not out["kind"]:
            out["kind"] = lab["kind"]
        out["owner"] = out["owner"] or lab["owner"]
    if ins:
        out["fyi"] = app_labels(ins[0])["fyi"] and not out["owner"]
    return out


def desired(conn, m: dict) -> set:
    """The label keys this email should carry in Gmail now."""
    if m.get("direction") != "in" or m.get("trashed_at") or m.get("kind") is None or m.get("business") == 0:
        return set()   # personal mail, receipts and automatic notices: no label
    lab = app_labels(m)
    want = {k for k in ("owner", "fyi") if lab[k]} | ({lab["kind"]} if lab["kind"] else set())
    if not m.get("backfilled_at") and m.get("needs_action") and m.get("status") != "done" and not _team_answered(conn, m):
        want.add("reply")
    return want


def set_labels(conn, uid: int, mid: int, kind: Optional[str] = None, owner: Optional[bool] = None) -> Optional[dict]:
    """Fix a conversation's label by hand. kind: one of KIND_LABELS, or 'other' (none of them); owner: "For" on / off.
    The sorting leaves what was fixed alone. Gmail follows when the person's labels are on."""
    m = _one(conn, "SELECT id, thread_id, gmail_id FROM mail_message WHERE id = ? AND user_id = ?", (mid, uid))
    if not m:
        return None
    msgs = (_rows(conn, "SELECT * FROM mail_message WHERE user_id = ? AND thread_id = ? ORDER BY received_at DESC, id DESC",
                  (uid, m["thread_id"])) if m["thread_id"] else _rows(conn, "SELECT * FROM mail_message WHERE id = ?", (mid,)))
    target = thread_labels(msgs)["mid"]
    if not target:
        return None
    t = next(x for x in msgs if x["id"] == target)
    if kind is not None:
        conn.execute("UPDATE mail_message SET kind = ?, business = 1, label_manual = 1 WHERE id = ?", (kind, target))
    if owner:
        conn.execute("UPDATE mail_message SET for_owner = 1, business = 1, kind = COALESCE(kind, 'other'), label_manual = 1 "
                     "WHERE id = ?", (target,))
    elif owner is not None:
        ids = [x["id"] for x in msgs if x.get("direction") == "in"]
        conn.execute(f"UPDATE mail_message SET for_owner = 0, label_manual = 1 WHERE id IN ({','.join('?' * len(ids))})", ids)
    conn.commit()
    relabel(conn, uid, [x["id"] for x in msgs])
    log.info("labels fixed by hand user=%s message=%s kind=%s owner=%s", uid, t["id"], kind is not None, owner)
    return thread_labels(_rows(conn, f"SELECT * FROM mail_message WHERE id IN ({','.join('?' * len(msgs))}) "
                                     "ORDER BY received_at DESC, id DESC", [x["id"] for x in msgs]))


def _have(m: dict) -> set:
    return {k for k in (m.get("gmail_labels") or "").split(",") if k}


# ------------------------------------------------------------------ Gmail labels
def _list_labels(conn, link) -> list:
    import inbox
    code, body = inbox._gmail(conn, link, "GET", "/labels")
    if code != 200:
        raise RuntimeError(f"labels list failed ({code})")
    return body.get("labels") or []


def _create(conn, link, key: str, name: str) -> tuple:
    """(gmail label id, made by this app). Uses the person's own label of that name when there is one."""
    import inbox
    for lab in _list_labels(conn, link):
        if (lab.get("name") or "").strip().lower() == name.lower() and lab.get("type") != "system":
            return lab["id"], False
    bg, fg = COLORS.get(key, KIND_COLOR)
    body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show",
            "color": {"backgroundColor": bg, "textColor": fg}}
    code, got = inbox._gmail(conn, link, "POST", "/labels", json_body=body)
    if code == 400:   # a colour Gmail does not take: the label without one
        body.pop("color")
        code, got = inbox._gmail(conn, link, "POST", "/labels", json_body=body)
    if code == 409:   # made a moment ago (another pass): use it
        for lab in _list_labels(conn, link):
            if (lab.get("name") or "").strip().lower() == name.lower():
                return lab["id"], False
    if code != 200 or not got.get("id"):
        raise RuntimeError(f"label create failed ({code})")
    return got["id"], True


def label_ids(conn, uid: int, link, keys) -> dict:
    """key -> Gmail label id, making the missing labels; a label renamed here (another owner) is renamed in Gmail.
    One at a time (the sync loop, the sorting loop and a page can all label at once): a label is never made twice."""
    with _MAKE_LOCK:
        return _label_ids(conn, uid, link, keys)


def _label_ids(conn, uid: int, link, keys) -> dict:
    import inbox
    want = names(conn, uid)
    have = {r["label_key"]: r for r in _rows(conn, "SELECT * FROM mail_label_map WHERE user_id = ?", (uid,))}
    out = {}
    for k in keys:
        r = have.get(k)
        if r and r["name"] != want[k]:
            if r["created"]:
                code, _b = inbox._gmail(conn, link, "PATCH", f"/labels/{inbox._q(r['gmail_label_id'])}", json_body={"name": want[k]})
                if code == 200:
                    conn.execute("UPDATE mail_label_map SET name = ? WHERE id = ?", (want[k], r["id"]))
                    conn.commit()
                    r["name"] = want[k]
                else:
                    r = None
            else:
                r = None
            if r is None:
                conn.execute("DELETE FROM mail_label_map WHERE user_id = ? AND label_key = ?", (uid, k))
                conn.commit()
        if not r:
            gid, made = _create(conn, link, k, want[k])
            conn.execute("INSERT INTO mail_label_map (user_id, label_key, gmail_label_id, name, created, created_at) "
                         "VALUES (?,?,?,?,?,?)", (uid, k, gid, want[k], 1 if made else 0, _now()))
            conn.commit()
            r = {"gmail_label_id": gid}
        out[k] = r["gmail_label_id"]
    return out


def _forget(conn, uid: int, link) -> None:
    """Labels Gmail no longer knows (deleted there) are forgotten, so they are made again; the others keep their record."""
    alive = {lab.get("id") for lab in _list_labels(conn, link)}
    for r in _rows(conn, "SELECT id, gmail_label_id FROM mail_label_map WHERE user_id = ?", (uid,)):
        if r["gmail_label_id"] not in alive:
            conn.execute("DELETE FROM mail_label_map WHERE id = ?", (r["id"],))
    conn.commit()


def relabel(conn, uid: int, mail_ids) -> int:
    """Bring these emails' labels in Gmail in line with how they are sorted now. Best effort, never raises.
    Emails that need the same change go in one Gmail call. Returns how many emails changed."""
    import inbox
    try:
        mail_ids = [i for i in mail_ids if i]
        if not mail_ids or not can_label(conn, uid):
            return 0
        link = inbox._link(conn, uid)
        groups: dict = {}
        for i in range(0, len(mail_ids), 500):
            chunk = mail_ids[i:i + 500]
            for m in _rows(conn, f"SELECT * FROM mail_message WHERE user_id = ? AND id IN ({','.join('?' * len(chunk))})",
                           [uid] + chunk):
                want, have = desired(conn, m), _have(m)
                if want != have:
                    groups.setdefault((tuple(sorted(want - have)), tuple(sorted(have - want))), []).append(
                        (m["id"], m["gmail_id"], ",".join(k for k in ORDER if k in want)))
        changed = 0
        for (add, rem), items in groups.items():
            ok = False
            for attempt in range(2):
                ids = label_ids(conn, uid, link, set(add) | set(rem))
                ok = inbox.gmail_modify(conn, uid, [g for _i, g, _w in items],
                                        add=[ids[k] for k in add], remove=[ids[k] for k in rem])
                if ok or attempt:
                    break
                _forget(conn, uid, link)   # a label deleted in Gmail: make it again, once
            if not ok:
                continue
            for mid, _g, w in items:
                conn.execute("UPDATE mail_message SET gmail_labels = ? WHERE id = ?", (w or None, mid))
            conn.commit()
            changed += len(items)
        if changed:
            log.info("gmail labels user=%s emails=%s calls=%s", uid, changed, len(groups))
        return changed
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.info("gmail labels user=%s skipped (%s)", uid, type(e).__name__)
        return 0


def relabel_thread(conn, uid: int, thread_id: Optional[str]) -> int:
    if not thread_id or not can_label(conn, uid):
        return 0
    return relabel(conn, uid, [r["id"] for r in _rows(conn, "SELECT id FROM mail_message WHERE user_id = ? AND thread_id = ?",
                                                     (uid, thread_id))])


def relabel_sorted(conn, uid: int) -> int:
    """Everything already sorted (switching on, or after a new owner was picked)."""
    since = (datetime.now() - timedelta(days=BACKFILL_DAYS)).isoformat(timespec="seconds")
    return relabel(conn, uid, [r["id"] for r in _rows(
        conn, "SELECT id FROM mail_message WHERE user_id = ? AND direction = 'in' AND kind IS NOT NULL AND received_at >= ? "
              "ORDER BY received_at DESC", (uid, since))])


def remove_all(conn, uid: int) -> dict:
    """Every label this app made is deleted in Gmail (which takes it off every email); a label of the person's own
    is taken off only the email this app put it on. Switches labels off."""
    import inbox
    _save_pref(conn, uid, enabled=0)
    link = inbox._link(conn, uid)
    done, failed = 0, 0
    for r in _rows(conn, "SELECT * FROM mail_label_map WHERE user_id = ?", (uid,)):
        ok = True
        if link and inbox.can_delete(link):
            if r["created"]:
                code, _b = inbox._gmail(conn, link, "DELETE", f"/labels/{inbox._q(r['gmail_label_id'])}")
                ok = code in (200, 204, 404)
            else:
                gids = [x["gmail_id"] for x in _rows(conn, "SELECT gmail_id, gmail_labels FROM mail_message WHERE user_id = ? "
                                                         "AND gmail_labels IS NOT NULL", (uid,))
                        if r["label_key"] in (x["gmail_labels"] or "").split(",")]
                for i in range(0, len(gids), 1000):
                    ok = inbox.gmail_modify(conn, uid, gids[i:i + 1000], remove=[r["gmail_label_id"]]) and ok
        if ok:
            conn.execute("DELETE FROM mail_label_map WHERE id = ?", (r["id"],))
            done += 1
        else:
            failed += 1
    if not failed:
        conn.execute("UPDATE mail_message SET gmail_labels = NULL WHERE user_id = ? AND gmail_labels IS NOT NULL", (uid,))
    conn.commit()
    log.info("gmail labels removed user=%s labels=%s failed=%s", uid, done, failed)
    return {"removed": done, "failed": failed}


# ------------------------------------------------------------------ "Label my last 90 days"
def backfill_counts(conn, uid: int) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM mail_message WHERE user_id = ? AND backfilled_at IS NOT NULL", (uid,)).fetchone()[0]
    left = conn.execute("SELECT COUNT(*) FROM mail_message WHERE user_id = ? AND ai_state = 'backfill'", (uid,)).fetchone()[0]
    fyi = sum(1 for m in _rows(conn, "SELECT direction, trashed_at, kind, business, for_owner, backfilled_at, label_manual, "
                                     "triage_needs, needs_action, addressed FROM mail_message WHERE user_id = ? "
                                     "AND backfilled_at IS NOT NULL AND addressed IN ('cc', 'other')", (uid,))
              if app_labels(m)["fyi"])
    return {"total": total, "sorted": total - left, "left": left, "fyi": fyi}


def backfill_state(conn, uid: int) -> str:
    """'' (never run) | listing (reading Gmail) | sorting | done | error."""
    st = pref(conn, uid).get("backfill_state") or ""
    if st == "sorting" and not conn.execute("SELECT COUNT(*) FROM mail_message WHERE user_id = ? AND ai_state = 'backfill'",
                                            (uid,)).fetchone()[0]:
        _save_pref(conn, uid, backfill_state="done")
        relabel(conn, uid, [r["id"] for r in _rows(conn, "SELECT id FROM mail_message WHERE user_id = ? AND backfilled_at IS NOT NULL",
                                                   (uid,))])
        return "done"
    return st


def _backfill_run(uid: int) -> None:
    import inbox
    conn = _conn()
    try:
        link = inbox._link(conn, uid)
        if not link:
            raise RuntimeError("not connected")
        own = link.get("google_email") or ""
        ids, page = [], None
        while len(ids) < BACKFILL_MAX:
            code, body = inbox._gmail(conn, link, "GET", "/messages?q=" + inbox._q(BACKFILL_QUERY) + "&maxResults=500"
                                      + (f"&pageToken={inbox._q(page)}" if page else ""))
            if code != 200:
                raise RuntimeError(f"list failed ({code})")
            ids += [m["id"] for m in body.get("messages") or [] if m.get("id")]
            page = body.get("nextPageToken")
            if not page:
                break
        ids = ids[:BACKFILL_MAX]
        now = _now()
        stored = 0
        for n, gid in enumerate(ids):
            if n % 50 == 0 and not inbox._link(conn, uid):   # Gmail disconnected meanwhile
                break
            row = _one(conn, "SELECT id, kind, direction, ai_state, addressed FROM mail_message WHERE user_id = ? AND gmail_id = ?",
                       (uid, gid))
            if row:
                if row["direction"] == "in" and row["kind"] is None and row["ai_state"] == "triaged":   # kept quietly (a thread)
                    conn.execute("UPDATE mail_message SET ai_state = 'backfill', backfilled_at = ? WHERE id = ?", (now, row["id"]))
                    conn.commit()
                if row["direction"] == "in" and not row["addressed"]:   # stored before labels: who it was for, from what is kept
                    m = inbox.message(conn, row["id"])
                    conn.execute("UPDATE mail_message SET addressed = ?, from_team = ? WHERE id = ?",
                                 (addressed(own, m["from"], m["to"], m["cc"]), 1 if from_team(own, m["from"]) else 0, row["id"]))
                    conn.commit()
                continue
            code, msg = inbox._gmail(conn, link, "GET", f"/messages/{inbox._q(gid)}?format=full")
            if code == 200 and inbox.ingest(conn, uid, inbox._hydrate(conn, link, msg), own, backfill=True):
                stored += 1
        _save_pref(conn, uid, backfill_state="sorting", backfill_error=None)
        log.info("gmail labels backfill listed user=%s found=%s new=%s", uid, len(ids), stored)
        inbox._LOOP["ai_wake"].set()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        _save_pref(conn, uid, backfill_state="error", backfill_error="Gmail did not answer. Try again in a few minutes.")
        log.warning("gmail labels backfill user=%s failed: %s", uid, type(e).__name__)
    finally:
        conn.close()
        with _LOCK:
            _RUNNING.pop(uid, None)


def start_backfill(conn, uid: int) -> bool:
    with _LOCK:
        if _RUNNING.get(uid):
            return False
        _save_pref(conn, uid, backfill_state="listing", backfill_started_at=_now(), backfill_error=None)
        t = threading.Thread(target=_backfill_run, args=(uid,), name=f"gmail-labels-{uid}", daemon=True)
        _RUNNING[uid] = t
    t.start()
    return True


def backfill_round(conn) -> int:
    """Sort a few older emails (inbox.claude_round, after the new mail). Returns how many."""
    import inbox
    n = 0
    for r in _rows(conn, "SELECT id FROM mail_message WHERE ai_state = 'backfill' AND trashed_at IS NULL "
                         f"ORDER BY received_at DESC LIMIT {BACKFILL_PER_ROUND}"):
        try:
            st = inbox.process(conn, r["id"])
        except Exception as e:
            conn.rollback()
            conn.execute("UPDATE mail_message SET ai_state = 'triaged' WHERE id = ?", (r["id"],))
            conn.commit()
            log.warning("gmail labels backfill sort %s failed: %s", r["id"], type(e).__name__)
            continue
        if st == "paused":   # the day's Claude budget: the rest waits
            break
        n += 1
    return n


# ------------------------------------------------------------------ routes
def _me() -> dict:
    u = current_user() or {}
    return {"id": u.get("id"), "role": u.get("role")}


def _view(conn, uid: int) -> dict:
    import inbox
    p, link = pref(conn, uid), inbox._link(conn, uid)
    o = owner_person(conn, uid)
    want = names(conn, uid)
    return {"enabled": bool(p.get("enabled")), "connected": bool(link), "can_label": inbox.can_delete(link),
            "owner": o, "owner_choices": owner_choices(conn, uid),
            "labels": [{"key": k, "name": want[k]} for k in ORDER],
            "backfill": dict(backfill_counts(conn, uid), state=backfill_state(conn, uid), days=BACKFILL_DAYS,
                             started_at=p.get("backfill_started_at"), problem=p.get("backfill_error"))}


@requires_role(*OFFICE_ROLES)
def _api_get():
    me = _me()
    conn = _conn()
    try:
        return jsonify({"data": _view(conn, me["id"])})
    finally:
        conn.close()


def _relabel_later(uid: int) -> None:
    def run():
        c = _conn()
        try:
            relabel_sorted(c, uid)
        finally:
            c.close()
    threading.Thread(target=run, name=f"gmail-relabel-{uid}", daemon=True).start()


@requires_role(*OFFICE_ROLES)
def _api_put():
    import inbox
    me = _me()
    d = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        vals = {}
        if "owner_user_id" in d:
            oid = d.get("owner_user_id")
            if oid in (None, "", 0):
                vals["owner_user_id"] = None
            else:
                try:
                    oid = int(oid)
                except (TypeError, ValueError):
                    return jsonify({"error": "Pick someone from the list."}), 400
                if oid not in [c["id"] for c in owner_choices(conn, me["id"])]:
                    return jsonify({"error": "Pick someone from the list."}), 400
                vals["owner_user_id"] = oid
        if "enabled" in d:
            on = d.get("enabled") not in (False, "false", 0, None)
            if on and not inbox.can_delete(inbox._link(conn, me["id"])):
                return jsonify({"error": "Connect Gmail again first, so the app may add labels."}), 409
            vals["enabled"] = 1 if on else 0
        if vals:
            _save_pref(conn, me["id"], **vals)
            if pref(conn, me["id"]).get("enabled"):
                _relabel_later(me["id"])   # label what is already sorted / rename "For ..." for the new owner
        return jsonify({"data": _view(conn, me["id"])})
    finally:
        conn.close()


@requires_role(*OFFICE_ROLES)
def _api_backfill():
    me = _me()
    conn = _conn()
    import inbox
    try:
        if not inbox._link(conn, me["id"]):   # the Gmail switch is not needed: the app's labels fill in either way
            return jsonify({"error": "Connect Gmail first."}), 409
        if backfill_state(conn, me["id"]) in ("listing", "sorting"):
            return jsonify({"data": _view(conn, me["id"])})
        start_backfill(conn, me["id"])
        return jsonify({"data": _view(conn, me["id"])}), 202
    finally:
        conn.close()


@requires_role(*OFFICE_ROLES)
def _api_remove():
    me = _me()
    conn = _conn()
    try:
        r = remove_all(conn, me["id"])
        v = _view(conn, me["id"])
        v["removed"] = r
        if r["failed"]:
            return jsonify({"error": "Gmail did not take every label off. Try again.", "data": v}), 502
        return jsonify({"data": v})
    finally:
        conn.close()


@requires_role(*OFFICE_ROLES)
def _api_label(mid):
    me = _me()
    d = request.get_json(silent=True) or {}
    kind = d.get("kind")
    if kind is not None and kind not in KIND_LABELS and kind != "other":
        return jsonify({"error": "Pick a label from the list."}), 400
    owner = None if d.get("owner") is None else d.get("owner") not in (False, "false", 0)
    if kind is None and owner is None:
        return jsonify({"error": "Pick a label from the list."}), 400
    conn = _conn()
    try:
        got = set_labels(conn, me["id"], mid, kind=kind, owner=owner)
        if got is None:
            return jsonify({"error": "not found"}), 404
        return jsonify({"data": got})
    finally:
        conn.close()


def register(app) -> None:
    conn = _conn()
    try:
        import inbox
        inbox.ensure(conn)   # mail_message first
        ensure(conn)
    except Exception as e:
        log.warning("gmail labels schema ensure skipped: %s", type(e).__name__)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    app.add_url_rule("/api/inbox/labels", "inbox_labels", _api_get, methods=["GET"])
    app.add_url_rule("/api/inbox/labels", "inbox_labels_put", _api_put, methods=["PUT"])
    app.add_url_rule("/api/inbox/labels/backfill", "inbox_labels_backfill", _api_backfill, methods=["POST"])
    app.add_url_rule("/api/inbox/labels/remove", "inbox_labels_remove", _api_remove, methods=["POST"])
    app.add_url_rule("/api/inbox/messages/<int:mid>/label", "inbox_message_label", _api_label, methods=["POST"])
