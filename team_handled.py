"""Needs you clears itself when the team finishes the work (accepted prototype 2026-11-03:
https://claude.ai/artifact/UZ2YNWpwy6SmTYF2z9ReuR).

Every office person gets their own Claude card for an email in their own mailbox, so a proposal one person sent left
the card open for everyone else. Each time cards are read, this works out what the records say about the request:

  closed   a proposal from this conversation went out after the email -> the card closes and moves to Handled
  writing  a proposal from this conversation is still a draft -> "Being written", down from Now to Today
  ask      not linked, but same building, after the email (or a teammate replied) -> "Same request?", a person decides
  open     nothing yet. An older proposal at the same address never counts: it went out before the request.

The same email in two mailboxes has different Gmail thread ids and its Message-ID is stored encrypted, so
`mail_message.rfc_hash` (a fingerprint of the Message-ID) is what connects the copies. Other people's mail is only
compared, never shown: a card says what happened to the record and names no teammate.

Only records and a person's answer close a card; nothing an email says can. Reopen is remembered in `needs_answer`, so
the same proposal never closes that card again.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger("ssc.team_handled")

WENT_OUT = ("sent", "viewed", "signed", "declined", "superseded")
NO_PROPOSAL = 0   # needs_answer.proposal_id for "a teammate replied", which has no proposal behind it
SCAN_LIMIT = 500


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _rows(conn, sql, params=()) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params=()) -> Optional[dict]:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def ensure(conn) -> None:
    """Idempotent: a person's answers on a card (No, not the same / Reopen)."""
    conn.execute("CREATE TABLE IF NOT EXISTS needs_answer (user_id INTEGER NOT NULL, action_id INTEGER NOT NULL, "
                 "proposal_id INTEGER NOT NULL, reopened INTEGER NOT NULL DEFAULT 0, created_at TEXT)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_needs_answer ON needs_answer(user_id, action_id, proposal_id)")
    conn.commit()


def fingerprint(rfc_id: Optional[str]) -> Optional[str]:
    v = str(rfc_id or "").strip()
    return hashlib.sha256(v.encode("utf-8")).hexdigest() if v else None


def backfill(conn) -> int:
    """Emails stored before rfc_hash existed. Idempotent."""
    import inbox
    n = 0
    for r in _rows(conn, "SELECT id, rfc_id_enc FROM mail_message WHERE rfc_hash IS NULL AND rfc_id_enc IS NOT NULL"):
        try:
            h = fingerprint(inbox._dec(r["rfc_id_enc"]))
        except Exception as e:   # no field key here: the next boot tries again
            log.info("rfc_hash backfill stopped (%s)", type(e).__name__)
            break
        if h:
            conn.execute("UPDATE mail_message SET rfc_hash = ? WHERE id = ?", (h, r["id"]))
            n += 1
    conn.commit()
    return n


# ------------------------------------------------------------------ what the records say
def _conversation(conn, m: dict) -> tuple:
    """(ids of every email in this conversation in any mailbox, the copies of this email)."""
    row = _one(conn, "SELECT rfc_hash FROM mail_message WHERE id = ?", (m["id"],))
    h = (row or {}).get("rfc_hash")
    copies = _rows(conn, "SELECT id, user_id, thread_id FROM mail_message WHERE rfc_hash = ?", (h,)) if h else []
    if not copies:
        copies = [{"id": m["id"], "user_id": m["user_id"], "thread_id": m.get("thread_id")}]
    ids = set()
    for c in copies:
        ids.add(c["id"])
        if c["thread_id"]:
            ids.update(r["id"] for r in _rows(conn, "SELECT id FROM mail_message WHERE user_id = ? AND thread_id = ?",
                                              (c["user_id"], c["thread_id"])))
    return sorted(ids), copies


def _after(p: dict, received: str) -> bool:
    """The proposal belongs to work done after this email. Mark as sent stores noon of the day, so a proposal made
    after the email still counts when its sent time reads earlier that same day; an old proposal adopted today doesn't."""
    sent, made = p.get("sent_at") or "", p.get("created_at") or ""
    if sent and sent[:10] < received[:10]:
        return False
    return made >= received or sent >= received


def _linked(conn, ids: list) -> list:
    import proposals
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    return _rows(conn, proposals._JOIN + "WHERE p.status <> 'deleted' AND (p.source_mail_id IN (" + ph + ") OR CAST(p.id AS TEXT) IN ("
                 "SELECT entity_id FROM mail_link WHERE entity_type = 'proposal' AND message_id IN (" + ph + "))) "
                 "ORDER BY p.id DESC", list(ids) + list(ids))


def _address(p: dict) -> str:
    return p.get("building_address_line") or p.get("building_address") or ""


def _here(conn, facts: dict, where: str, params: tuple) -> list:
    """Proposals at the request's building; a request with no address falls back to the same client."""
    import buildings
    import proposals
    key = buildings.address_key(facts.get("building_address"))
    if not key and not facts.get("client_org_id"):
        return []
    rows = _rows(conn, proposals._JOIN + "WHERE p.status <> 'deleted' AND " + where + " ORDER BY p.id DESC LIMIT " + str(SCAN_LIMIT), params)
    if key:
        return [p for p in rows if buildings.address_key(_address(p)) == key]
    return [p for p in rows if p["client_org_id"] == int(facts["client_org_id"])]


def _teammate_replied(conn, m: dict, copies: list) -> bool:
    """A reply went out from a teammate's copy of this conversation, after this email."""
    for c in copies:
        if c["user_id"] != m["user_id"] and c["thread_id"] and _one(
                conn, "SELECT id FROM mail_message WHERE user_id = ? AND thread_id = ? AND direction = 'out' AND received_at >= ? LIMIT 1",
                (c["user_id"], c["thread_id"], m.get("received_at") or "")):
            return True
    return False


def _sent_note(p: dict) -> str:
    import proposals
    return f"Proposal {proposals._label(p)} was sent to {p.get('client_name') or 'the client'}"


def _answers(conn, uid: int, action_id: int) -> list:
    return _rows(conn, "SELECT proposal_id, reopened FROM needs_answer WHERE user_id = ? AND action_id = ?", (uid, action_id))


def check(conn, uid: int, a: dict, m: dict, inp: dict) -> dict:
    """{state: open | writing | ask | closed, ...} for one pending card. Reads only."""
    answers = _answers(conn, uid, a["id"])
    no = {x["proposal_id"] for x in answers}
    out = {"state": "open", "reopened": any(x["reopened"] for x in answers), "said_no": None, "older": None}
    if a["tool"] not in ("draft_proposal", "reply_email"):
        return out
    ids, copies = _conversation(conn, m)
    received = m.get("received_at") or ""
    if a["tool"] == "reply_email":
        if NO_PROPOSAL not in no and _teammate_replied(conn, m, copies):
            return {"state": "closed", "proposal": None, "note": "Answered in the conversation"}
        return out

    import claude_agent
    import proposals
    facts = claude_agent._draft_facts(inp)
    linked = [p for p in _linked(conn, ids) if p["id"] not in no and _after(p, received)]
    went = [p for p in linked if p["status"] in WENT_OUT]
    if went:
        return {"state": "closed", "proposal": went[0], "note": _sent_note(went[0])}
    drafts = [p for p in linked if p["status"] == "draft"]
    if drafts:
        return {"state": "writing", "proposal": drafts[0]}
    day = received[:10]
    near = [p for p in _here(conn, facts, "(p.created_at >= ? OR p.sent_at >= ?)", (day, day))
            if p["id"] not in no and _after(p, received)]
    if near:   # one that went out first: that is the likeliest answer
        near.sort(key=lambda p: (p["status"] not in WENT_OUT, -p["id"]))
        return {"state": "ask", "proposal": near[0], "what": facts["what"]}
    if NO_PROPOSAL not in no and _teammate_replied(conn, m, copies):
        return {"state": "ask", "proposal": None, "what": facts["what"]}
    said = [x["proposal_id"] for x in answers if x["proposal_id"] and not x["reopened"]]
    if said:
        p = proposals.get(conn, said[-1])
        out["said_no"] = proposals._label(p) if p else None
    older = _here(conn, facts, "p.sent_at IS NOT NULL AND p.sent_at < ?", (received,)) if facts.get("building_address") else []
    if older:
        out["older"] = proposals._label(older[0])
    return out


def safe_check(conn, uid: int, a: dict, m: dict, inp: dict) -> dict:
    """The dashboard never breaks on this: a failed check leaves the card as it was."""
    try:
        return check(conn, uid, a, m, inp)
    except Exception as e:
        log.info("needs-you check skipped for card %s (%s)", a.get("id"), type(e).__name__)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"state": "open", "reopened": False, "said_no": None, "older": None}


def close(conn, action_id: int, note: str, proposal_id: Optional[int], how: str) -> None:
    """Declined by nobody, like a reply draft that was withdrawn; `handled` puts it on the Handled list."""
    conn.execute("UPDATE claude_action SET status = 'declined', decided_at = ?, result = ? WHERE id = ? AND status = 'pending'",
                 (_now(), json.dumps({"summary": note, "undo": False,
                                      "handled": {"proposal_id": proposal_id or NO_PROPOSAL, "how": how}}), action_id))
    conn.commit()


def done_already(conn, uid: int, a: dict, mail_id) -> Optional[str]:
    """Approving a card from anywhere: the reason it must not go ahead (the card closes), or None."""
    import inbox
    m = inbox.message(conn, mail_id, uid) if mail_id else None
    if not m:
        return None
    try:
        inp = json.loads(a["input"] or "{}")
    except ValueError:
        inp = {}
    st = safe_check(conn, uid, a, m, inp)
    if st["state"] != "closed":
        return None
    close(conn, a["id"], st["note"], (st.get("proposal") or {}).get("id"), "team")
    return st["note"] + ", so this was closed. Nothing else was created."


# ------------------------------------------------------------------ what the card looks like
def dress(card: dict, st: dict, m: dict) -> None:
    """Changes one proposal or reply card for its state."""
    import proposals
    p = st.get("proposal")
    if st["state"] == "writing":
        card.update(urgency="today", pill="Being written", draft=None, href=f"/proposal-studio#p/{p['id']}",
                    why="A draft was started in Proposal Studio from this email. Nothing for you to do yet.",
                    actions=[{"type": "open", "label": "See the draft", "primary": True},
                             {"type": "link", "label": "Read the email", "href": f"#inbox/{m['id']}"}])
    elif st["state"] == "ask":
        left = {"label": "This email asks for", "text": str(st.get("what") or "")[:300]}
        if p:
            n = proposals._label(p)
            done = p["status"] in WENT_OUT
            card["why"] = f"Looks like Proposal {n} was {'already sent' if done else 'started'} for this. Is it the same request?"
            covers = (p.get("scope_summary") or p.get("title") or _address(p) or "")[:240]
            when = f"Sent {str(p.get('sent_at') or '')[:10]}, after this email." if done else "Still a draft, started after this email."
            card["compare"] = [left, {"label": f"Proposal {n} covers", "text": f"{covers} {when}".strip()}]
        else:
            card["why"] = "A reply went out in this conversation. Is this request taken care of?"
            card["compare"] = [left]
        card.update(urgency="today", pill="Same request?", draft=None,
                    actions=[{"type": "answer", "answer": "yes", "label": "Yes, it's done", "primary": True},
                             {"type": "answer", "answer": "no", "label": "No, still needed", "show": True}])
    else:
        if st.get("said_no"):
            card["checked"] = f"You said Proposal {st['said_no']} doesn't cover this."
        elif st.get("older"):
            card["checked"] = f"Proposal {st['older']} is at this address too, but it was sent before this email. This one still needs writing."
        if st.get("reopened"):
            card.update(pill="Reopened by you", urgency="now")


def handled(conn, uid: int) -> list:
    """What closed itself or a person confirmed, while the card would still have been open (CARD_TTL)."""
    import claude_agent
    import inbox
    from email.utils import parseaddr
    since = (datetime.now() - claude_agent.CARD_TTL).isoformat(timespec="seconds")
    out = []
    for a in _rows(conn, "SELECT a.id, a.tool, a.input, a.result, a.decided_at, c.mail_id FROM claude_action a "
                         "JOIN claude_chat c ON c.id = a.chat_id WHERE a.user_id = ? AND a.status = 'declined' AND c.source = 'mail' "
                         "AND a.created_at >= ? AND a.result LIKE '%\"handled\"%' ORDER BY a.decided_at DESC LIMIT 20", (uid, since)):
        m = inbox.message(conn, a["mail_id"], uid) if a.get("mail_id") else None
        if not m or m["trashed_at"]:
            continue
        who = parseaddr(m["from"])[0] or parseaddr(m["from"])[1] or "Someone"
        if a["tool"] == "draft_proposal":
            addr = claude_agent._draft_facts(json.loads(a["input"] or "{}"))["building_address"]
            title = f"{who} asked for a proposal" + (f": {addr}" if addr else "")
        else:
            title = f"{who} was waiting for a reply"
        out.append({"action_id": a["id"], "title": title, "note": json.loads(a["result"]).get("summary") or "",
                    "when": a["decided_at"]})
    return out


# ------------------------------------------------------------------ a person's answer
def answer(conn, uid: int, action_id: int, verb: str) -> None:
    """yes = it's done, close it; no = not the same, keep it open; reopen = bring a closed card back."""
    import claude_agent
    import inbox
    a = _one(conn, "SELECT a.*, c.mail_id FROM claude_action a JOIN claude_chat c ON c.id = a.chat_id "
                   "WHERE a.id = ? AND a.user_id = ? AND c.source = 'mail'", (action_id, uid))
    m = inbox.message(conn, a["mail_id"], uid) if a and a.get("mail_id") else None
    if not m:
        raise LookupError("not found")
    if datetime.fromisoformat(a["created_at"]) < datetime.now() - claude_agent.CARD_TTL:
        raise ValueError("This card's 48 hours are up. Ask Claude again so it uses today's information.")
    if verb == "reopen":
        res = json.loads(a["result"] or "{}")
        if a["status"] != "declined" or "handled" not in res:
            raise ValueError("Only a card under Handled can be reopened.")
        _remember(conn, uid, action_id, res["handled"].get("proposal_id") or NO_PROPOSAL, reopened=1)
        conn.execute("UPDATE claude_action SET status = 'pending', decided_by = NULL, decided_at = NULL, result = NULL WHERE id = ?", (action_id,))
        conn.commit()
        return
    if a["status"] != "pending":
        raise ValueError("This card was already decided.")
    st = safe_check(conn, uid, a, m, json.loads(a["input"] or "{}"))
    if st["state"] != "ask":
        raise ValueError("This card changed. Look at it again.")
    p = st.get("proposal")
    if verb == "yes":
        import proposals
        note = f"You confirmed Proposal {proposals._label(p)} covers it" if p else "You confirmed it's taken care of"
        close(conn, action_id, note, (p or {}).get("id"), "you")
    else:
        _remember(conn, uid, action_id, (p or {}).get("id") or NO_PROPOSAL)


def _remember(conn, uid: int, action_id: int, proposal_id: int, reopened: int = 0) -> None:
    conn.execute("DELETE FROM needs_answer WHERE user_id = ? AND action_id = ? AND proposal_id = ?", (uid, action_id, proposal_id))
    conn.execute("INSERT INTO needs_answer (user_id, action_id, proposal_id, reopened, created_at) VALUES (?,?,?,?,?)",
                 (uid, action_id, proposal_id, reopened, _now()))
    conn.commit()
