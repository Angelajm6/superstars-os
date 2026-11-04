"""One-tap accepts: what waits in the bell for a decision (accepted prototype 2026-11-03:
https://claude.ai/artifact/UZ2YNWpwy6SmTYF2z9ReuR, phase 3).

  client   a client that came with a new proposal: Keep / Merge / Edit. One admin's answer clears it for all.
  project  a signed proposal not yet a project (the same list as Projects -> Ready to start): Create project / Not a project.
  contact  someone writing about a client who isn't a contact yet, found in YOUR mailbox: Add contact / Not now.
           Only the mailbox owner ever sees it, or anything read from that email.

What counts as waiting is worked out from the records each time. `activity_decision` only remembers answers the records
can't hold, and the `start` row keeps everything from before this went live out of the list. Nothing here acts on its
own: every change comes from a person pressing a button, and email is only read, never followed.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from email.utils import parseaddr
from typing import Optional

log = logging.getLogger("ssc.activity_accepts")

SAME_MOMENT = timedelta(minutes=5)     # a client added this close to its first proposal came from that proposal
POP_MAX_AGE = timedelta(hours=24)      # older ones wait in the bell without popping up
WINDOW_DAYS = 14
SIMILAR_AT = 0.8
MAIL_SCAN = 40
MAX_CONTACTS = 3
_SUFFIXES = {"llc", "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "lp", "llp", "the"}
_NOT_PEOPLE = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|notifications?|mailer-daemon|postmaster|billing|invoices?|accounts?|info|support|admin|office|sales|hello)\b", re.I)


class Stale(Exception):
    """Someone already answered this, or the record moved on."""


def _rows(conn, sql, params=()) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _stamp(at: datetime) -> str:
    return at.isoformat(timespec="seconds")


def _parse(at) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(at)).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def ensure(conn) -> None:
    """Idempotent. user_id 0 = an answer for everyone; a contact's answer belongs to the mailbox owner."""
    conn.execute("CREATE TABLE IF NOT EXISTS activity_decision (kind TEXT NOT NULL, ref TEXT NOT NULL, user_id INTEGER NOT NULL DEFAULT 0, "
                 "decision TEXT, decided_by INTEGER, decided_at TEXT, PRIMARY KEY (kind, ref, user_id))")
    conn.execute("INSERT INTO activity_decision (kind, ref, user_id, decision, decided_at) VALUES ('start', 'start', 0, 'start', ?) "
                 "ON CONFLICT (kind, ref, user_id) DO NOTHING", (_stamp(_now()),))
    conn.commit()


def _start(conn) -> str:
    r = conn.execute("SELECT decided_at FROM activity_decision WHERE kind = 'start'").fetchone()
    return r["decided_at"] if r else _stamp(_now())


def _decided(conn, kind: str, user_id: int = 0) -> dict:
    return {r["ref"]: r["decision"] for r in _rows(conn, "SELECT ref, decision FROM activity_decision WHERE kind = ? AND user_id = ?", (kind, user_id))}


def _decide(conn, kind: str, ref: str, decision: str, by: int, user_id: int = 0) -> None:
    cur = conn.execute("INSERT INTO activity_decision (kind, ref, user_id, decision, decided_by, decided_at) VALUES (?,?,?,?,?,?) "
                       "ON CONFLICT (kind, ref, user_id) DO NOTHING", (kind, ref, user_id, decision, by, _stamp(_now())))
    if not cur.rowcount:
        conn.rollback()
        raise Stale("Someone already answered this.")


def _norm(name) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower().replace("&", " and ")).split()
    return " ".join(w for w in words if w not in _SUFFIXES)


def _score(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _active_clients(conn, skip: int) -> list:
    return _rows(conn, "SELECT id, name FROM crm_organization WHERE id <> ? AND relationship_type IN ('client', 'lead') "
                       "AND COALESCE(status, 'active') = 'active'", (skip,))


def similar(conn, org_id: int, name: str) -> Optional[str]:
    """The closest other client's name, when it's close enough to ask."""
    want = _norm(name)
    best, best_at = None, SIMILAR_AT
    for o in _active_clients(conn, org_id):
        s = _score(want, _norm(o["name"]))
        if s >= best_at:
            best, best_at = o["name"], s
    return best


# ------------------------------------------------------------------ what's waiting
def _clients(conn, start: str) -> list:
    import proposal_engine
    done = _decided(conn, "client")
    out = []
    for o in _rows(conn, "SELECT id, name, created_at FROM crm_organization WHERE relationship_type IN ('client', 'lead') "
                         "AND COALESCE(status, 'active') = 'active' AND CAST(created_at AS TEXT) >= ? ORDER BY id DESC LIMIT 30", (start,)):
        if str(o["id"]) in done:
            continue
        first = _rows(conn, "SELECT number, display_no, version, created_at FROM proposal WHERE client_org_id = ? AND status <> 'deleted' "
                            "ORDER BY id LIMIT 1", (o["id"],))
        added, made = _parse(o["created_at"]), _parse(first[0]["created_at"]) if first else None
        if not added or not made or made - added > SAME_MOMENT:   # added by hand: the person who typed it already decided
            continue
        twin = similar(conn, o["id"], o["name"])
        out.append({"id": f"client:{o['id']}", "kind": "client", "icon": "person", "title": f"New client added: {o['name']}",
                    "sub": f"Added when Proposal {proposal_engine.label(first[0])} was started",
                    "note": f"Looks like {twin}, already in your list." if twin else None, "at": _stamp(added),
                    "href": f"/company#clients/{o['id']}", "name": o["name"],
                    "actions": [{"label": "Keep", "act": "keep", "primary": True}, {"label": "Merge…", "act": "merge"}, {"label": "Edit…", "act": "edit"}]})
    return out


def _projects(conn, start: str) -> list:
    import portal_projects
    out = []
    for r in portal_projects.ready_to_start(conn):
        ev = _rows(conn, "SELECT MAX(CAST(at AS TEXT)) AS at FROM proposal_event WHERE proposal_id = ? AND event = 'signed'", (r["proposal_id"],))
        at = ev[0]["at"] if ev else None
        if not at or at < start:   # from before this went live: it's on Ready to start
            continue
        out.append({"id": f"signed:{r['proposal_id']}", "kind": "project", "icon": "send", "tint": "ok",
                    "title": f"Proposal {r['label']} was signed by {r['client_name']}",
                    "sub": " · ".join(x for x in (r["address"], "Ready to start") if x), "note": None, "at": str(at)[:19],
                    "href": f"/company#proposals/{r['proposal_id']}", "proposal_id": r["proposal_id"],
                    "project": {"name": r["address"], "address": r["address"], "client": r["client_name"], "est_type": r["est_type"],
                                "borough": r["borough"], "contract_value": r.get("price")},
                    "actions": [{"label": "Create project", "act": "create", "primary": True}, {"label": "Not a project", "act": "no_project"}]})
    return out


def _contact_ref(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:24]   # an address never sits in the clear outside the mail store


def _contacts(conn, uid: int, start: str, now: datetime) -> list:
    """Only this person's own mail. Reads the sender's name and address, nothing else."""
    import inbox
    since = max(start, _stamp(now - timedelta(days=WINDOW_DAYS)))
    rows = _rows(conn, "SELECT m.id, m.from_enc, m.received_at, COALESCE(m.client_org_id, p.client_org_id) AS org_id "
                       "FROM mail_message m LEFT JOIN proposal p ON p.source_mail_id = m.id AND p.status <> 'deleted' "
                       "WHERE m.user_id = ? AND m.direction = 'in' AND m.trashed_at IS NULL AND m.contact_id IS NULL "
                       "AND COALESCE(m.client_org_id, p.client_org_id) IS NOT NULL AND CAST(m.received_at AS TEXT) >= ? "
                       "ORDER BY m.received_at DESC LIMIT ?", (uid, since, MAIL_SCAN))
    if not rows:
        return []
    me = _rows(conn, "SELECT email FROM users WHERE id = ?", (uid,))
    own_domain = (me[0]["email"].split("@")[-1].lower() if me and me[0].get("email") else "")
    known = {str(r["email"]).strip().lower() for r in _rows(conn, "SELECT email FROM crm_contact WHERE email IS NOT NULL AND email <> ''")}
    staff = {str(r["email"]).strip().lower() for r in _rows(conn, "SELECT email FROM users WHERE email IS NOT NULL")}
    done = _decided(conn, "contact", uid)
    out, seen = [], set()
    for r in rows:
        name, addr = parseaddr(inbox._dec(r["from_enc"]) or "")
        addr = addr.strip().lower()
        ref = _contact_ref(addr)
        if ("@" not in addr or ref in seen or ref in done or addr in known or addr in staff or addr.endswith("@" + own_domain)
                or _NOT_PEOPLE.match(addr)):
            continue
        name = " ".join(name.split()).strip("'\" ")
        if not name or "@" in name:
            continue   # no real name to save; a guess from the address would be wrong as often as right
        org = _rows(conn, "SELECT name FROM crm_organization WHERE id = ? AND COALESCE(status, 'active') = 'active'", (r["org_id"],))
        if not org:
            continue
        seen.add(ref)
        out.append({"id": f"contact:{ref}", "kind": "contact", "icon": "person", "title": f"Add {name} as a contact?",
                    "sub": f"At {org[0]['name']}. Found in an email in your Inbox, so only you see this.", "note": None,
                    "at": str(r["received_at"])[:19], "href": f"/company#clients/{r['org_id']}", "org_id": r["org_id"],
                    "person": {"name": name[:200], "email": addr},
                    "actions": [{"label": "Add contact", "act": "add", "primary": True}, {"label": "Not now", "act": "no_contact"}]})
        if len(out) >= MAX_CONTACTS:
            break
    return out


def waiting(conn, uid: int, now: Optional[datetime] = None) -> list:
    now = now or _now()
    start = _start(conn)
    out = []
    for source in (lambda: _clients(conn, start), lambda: _projects(conn, start), lambda: _contacts(conn, uid, start, now)):
        try:
            out += source()
        except Exception as e:   # one source failing must not take the bell down; no record contents in the log
            log.warning("activity accepts source skipped: %s", type(e).__name__)
            try:
                conn.rollback()
            except Exception:
                pass
    out.sort(key=lambda w: w["at"], reverse=True)
    for w in out:
        at = _parse(w["at"]) or now
        w["pop"] = now - at <= POP_MAX_AGE
    return out


def public(w: dict) -> dict:
    """What the page gets: a contact's address stays on the server until a person presses Add."""
    return {k: v for k, v in w.items() if k not in ("person", "org_id")}


def client_matches(conn, org_id: int) -> list:
    """Every other client for Merge, closest name first, with enough to tell them apart."""
    me = _rows(conn, "SELECT name FROM crm_organization WHERE id = ?", (org_id,))
    if not me:
        raise LookupError("client not found")
    want = _norm(me[0]["name"])
    rows = _active_clients(conn, org_id)
    jobs = {r["k"]: r["n"] for r in _rows(conn, "SELECT client_org_id AS k, COUNT(1) AS n FROM projects GROUP BY client_org_id")}
    props = {r["k"]: r["n"] for r in _rows(conn, "SELECT client_org_id AS k, COUNT(1) AS n FROM proposal WHERE status <> 'deleted' GROUP BY client_org_id")}
    out = [{"id": o["id"], "name": o["name"], "projects": jobs.get(o["id"], 0), "proposals": props.get(o["id"], 0),
            "score": round(_score(want, _norm(o["name"])), 3)} for o in rows]
    out.sort(key=lambda o: (-o["score"], str(o["name"]).lower()))
    return out


# ------------------------------------------------------------------ a person's answer
def _word(n: int, one: str) -> str:
    return f"{n} {one}{'' if n == 1 else 's'}"


def decide(conn, uid: int, item_id: str, action: str, body: dict) -> dict:
    """Re-reads what's waiting for THIS person, so a page can't act on something it wasn't offered or that already moved on."""
    body = body or {}
    if action == "undo":
        return _undo(conn, uid, item_id)
    w = next((x for x in waiting(conn, uid) if x["id"] == item_id), None)
    if not w:
        raise Stale("Someone already took care of this.")
    ref = item_id.split(":", 1)[1]
    if w["kind"] == "client":
        return _client(conn, uid, w, ref, action, body)
    if w["kind"] == "project":
        return _project(conn, uid, w, ref, action, body)
    return _contact(conn, uid, w, ref, action)


def _client(conn, uid, w, ref, action, body) -> dict:
    import crm
    org_id = int(ref)
    if action == "keep":
        _decide(conn, "client", ref, "keep", uid)
        conn.commit()
        return {"message": f"{w['name']} stays in Clients.", "undo": True}
    if action == "edit":
        name = " ".join(str(body.get("name") or "").split())[:200]
        if not name:
            raise ValueError("Enter the client's name.")
        kind = str(body.get("client_kind") or "").strip().lower()
        if kind and kind not in crm.CLIENT_KINDS:
            raise ValueError("Pick what kind of client this is.")
        twin = crm.find_org_by_name(conn, name)
        if twin and twin["id"] != org_id:
            raise ValueError(f"{twin['name']} is already in your list. Use Merge instead.")
        fields = {"name": name}
        if kind:
            fields["client_kind"] = kind
        crm.update_org(conn, org_id, actor_user_id=uid, **fields)
        _decide(conn, "client", ref, "edit", uid)
        conn.commit()
        return {"message": f"Saved {name} in Clients.", "undo": False}
    if action == "merge":
        try:
            into = int(body.get("into_id") or 0)
        except (TypeError, ValueError):
            into = 0
        if not into:
            raise ValueError("Pick the client to merge into.")
        res = crm.merge_org(conn, org_id, into, actor=uid)
        _decide(conn, "client", ref, "merge", uid)
        conn.commit()
        return {"message": f"Merged into {res['into_name']}. {_word(res['moved'], 'record')} moved with it.", "undo": False}
    raise ValueError("That isn't an answer for this.")


def _project(conn, uid, w, ref, action, body) -> dict:
    pid = int(ref)
    if action == "no_project":
        import portal_projects
        portal_projects.dismiss_ready(conn, pid, actor=uid)
        return {"message": "Marked as not a project.", "undo": True}
    if action == "create":
        import project_onboard
        p = w["project"]
        est_type = str(body.get("est_type") or p["est_type"] or "").upper()
        payload = {"from_proposal_id": pid, "name": body.get("name") or p["name"], "address": body.get("address") or p["address"],
                   "est_type": est_type, "borough": body.get("borough") or p["borough"], "contract_value": p.get("contract_value")}
        for extra in ("job_type_other", "rope_access_scope"):   # only asked for the job types that have them
            if body.get(extra):
                payload[extra] = body[extra]
        res = project_onboard.onboard(conn, payload, actor=uid)
        code = res.get("project_code") or (res.get("project") or {}).get("project_code")
        return {"message": f"Project {code} created.", "undo": True, "href": f"/projects/{code}", "open_label": "Open"}
    raise ValueError("That isn't an answer for this.")


def _contact(conn, uid, w, ref, action) -> dict:
    import crm
    if action == "no_contact":
        _decide(conn, "contact", ref, "no", uid, user_id=uid)
        conn.commit()
        return {"message": "It won't ask about them again.", "undo": False}
    if action == "add":
        p = w["person"]
        cid = crm.create_contact(conn, full_name=p["name"], org_id=w["org_id"], email=p["email"], created_by=uid)
        _decide(conn, "contact", ref, f"added:{cid}", uid, user_id=uid)
        conn.commit()
        return {"message": f"{p['name']} added as a contact.", "undo": True}
    raise ValueError("That isn't an answer for this.")


def _undo(conn, uid: int, item_id: str) -> dict:
    kind, _, ref = str(item_id).partition(":")
    if kind == "client":
        cur = conn.execute("DELETE FROM activity_decision WHERE kind = 'client' AND ref = ? AND user_id = 0 AND decision = 'keep'", (ref,))
        conn.commit()
        if not cur.rowcount:
            raise Stale("That can't be undone any more.")
        return {"message": "It's back in the bell.", "undo": False}
    if kind == "signed":
        return _undo_project(conn, uid, int(ref))
    if kind == "contact":
        rows = _rows(conn, "SELECT decision FROM activity_decision WHERE kind = 'contact' AND ref = ? AND user_id = ?", (ref, uid))
        if not rows or not str(rows[0]["decision"]).startswith("added:"):
            raise Stale("That can't be undone any more.")
        cid = int(rows[0]["decision"].split(":")[1])
        import crm
        if crm._has_table(conn, "project_contact") and _rows(conn, "SELECT 1 AS x FROM project_contact WHERE contact_id = ? LIMIT 1", (cid,)):
            raise Stale("That contact is on a job now. Remove it from the client's page.")
        conn.execute("UPDATE mail_message SET contact_id = NULL WHERE contact_id = ?", (cid,))
        conn.execute("DELETE FROM crm_contact WHERE id = ? AND created_by = ?", (cid, uid))
        conn.execute("DELETE FROM activity_decision WHERE kind = 'contact' AND ref = ? AND user_id = ?", (ref, uid))
        conn.commit()
        return {"message": "Contact removed.", "undo": False}
    raise ValueError("That can't be undone.")


def _undo_project(conn, uid: int, pid: int) -> dict:
    import project_delete
    r = _rows(conn, "SELECT p.project_dismissed_at, e.converted_project_code AS code FROM proposal p JOIN estimate e ON e.id = p.estimate_id "
                    "WHERE p.id = ? AND p.status = 'signed'", (pid,))
    if not r:
        raise LookupError("not found")
    if r[0]["code"]:
        # the deposit invoice the signature drafted came along with the job; a draft isn't work on it, so it stays with the proposal
        conn.execute("UPDATE invoice SET project_code = NULL WHERE project_code = ? AND proposal_id = ? AND status = 'draft'", (r[0]["code"], pid))
        res = project_delete.delete(conn, r[0]["code"], actor=uid)
        if res and res.get("blockers"):
            conn.rollback()   # the draft goes back on the job
            raise Stale(f"That project already has {', '.join(res['blockers'])}, so it stays. It can be closed from its page.")
        return {"message": "Project removed. It's back in the bell.", "undo": False}
    if r[0]["project_dismissed_at"]:
        conn.execute("UPDATE proposal SET project_dismissed_at = NULL, project_dismissed_by = NULL WHERE id = ?", (pid,))
        conn.commit()
        return {"message": "It's back in the bell.", "undo": False}
    raise Stale("That can't be undone any more.")
