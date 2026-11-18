"""The Inbox accuracy test set's engine (piece 5, 2026-11-18). Shared by the gate suite (smoke_inbox_evals.py, Claude's
answers planted, free) and the live scorer (run_live.py, the real model, costs money, run by hand).

A case (cases.json) is one made-up email in a made-up world:
  world       client + contacts, proposals (with status and dates), estimates, a job with documents, another client
  email       direction, sender, Cc, subject, text, attachments (+ optional prior email in the thread, a teammate's email)
  prompt_has  what Claude must be told (where things stand); prompt_lacks: what it must never see
  calls       planted tool calls (gate): each expects "card" or "refused: <words of the server's answer>"
  then/after  a change made afterwards (the proposal is signed, the phone fixed by hand, the file filed) and the waiting
              card's fate ("closed: <words>")
  live        for the real model: tools it must propose, tools it must not

Placeholders in strings: {org} {other_org} {mail_id} {mate_mail_id} {J} {P1.id} {P1.label} {E1.id} {C1.id} ...
Everything is fictional; the database is the isolated one the caller built.
"""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOW = time.time()
TAG = "ievl"


def cases() -> list:
    return json.loads((HERE / "cases.json").read_text(encoding="utf-8"))["cases"]


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii")


def _gmsg(gid: str, thread: str, e: dict, me_addr: str, minutes_ago: int) -> dict:
    out = e.get("dir") == "out"
    frm = me_addr if out else e.get("from", "Robin Evalsmoke <robin@ievl-client.example>")
    to = e.get("to") or ("Robin Evalsmoke <robin@ievl-client.example>" if out else me_addr)
    hs = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": e.get("subject", "Eval")},
          {"name": "Message-ID", "value": f"<{gid}@ievl.example>"}]
    if e.get("cc"):
        hs.append({"name": "Cc", "value": e["cc"]})
    parts = [{"mimeType": "text/plain", "body": {"data": _b64(e.get("body", ""))}}]
    parts += [{"mimeType": "application/octet-stream", "filename": a["name"],
               "headers": [{"name": "Content-Disposition", "value": f"attachment; filename={a['name']}"}],
               "body": {"attachmentId": f"{gid}-a{i}", "size": a.get("size", 120000)}} for i, a in enumerate(e.get("attachments") or [])]
    return {"id": gid, "threadId": thread, "labelIds": ["SENT"] if out else ["INBOX", "UNREAD"],
            "internalDate": str(int((NOW - minutes_ago * 60) * 1000)), "snippet": e.get("body", "")[:80],
            "payload": {"mimeType": "multipart/mixed", "headers": hs, "parts": parts}}


def fill(v, refs: dict):
    """Placeholders -> values; a string that is exactly one placeholder of a number becomes that number."""
    if isinstance(v, dict):
        return {k: fill(x, refs) for k, x in v.items()}
    if isinstance(v, list):
        return [fill(x, refs) for x in v]
    if isinstance(v, str):
        whole = re.fullmatch(r"\{([\w.]+)\}", v)
        if whole and isinstance(refs.get(whole.group(1)), int):
            return refs[whole.group(1)]
        return re.sub(r"\{([\w.]+)\}", lambda m: str(refs.get(m.group(1), m.group(0))), v)
    return v


def build(conn, case: dict, n: int, ids: dict) -> dict:
    """Makes the case's world; returns the placeholder values."""
    import crm
    import estimates
    import inbox
    import proposals
    import ssc_paths
    w = case.get("world") or {}
    uid, mate = ids["me"], ids["mate"]
    refs = {}
    org = crm.create_org(conn, name=f"Ievl {case['id']} Owners", relationship_type="client", created_by=uid)
    refs["org"] = org
    for c in w.get("contacts") or []:
        refs[f"{c['ref']}.id"] = crm.create_contact(conn, org_id=org, full_name=c["name"], email=c.get("email"), phone=c.get("phone"),
                                                    title=c.get("title"), created_by=uid)
    street = f"{40 + n} Ievl Terrace, Brooklyn, NY"
    refs["address"] = street

    def estimate(o, status="intake", addr=street):
        e = estimates.create_estimate(conn, est_type="FR", borough="BK", client_org_id=o, building_address=addr, created_by=uid)
        if status != "intake":
            conn.execute("UPDATE estimate SET status = ? WHERE id = ?", (status, e["id"]))
            conn.commit()
        return e

    for e in w.get("estimates") or []:
        est = estimate(org, e.get("status", "intake"))
        refs[f"{e['ref']}.id"], refs[f"{e['ref']}.code"] = est["id"], est["code"]
    for p in w.get("proposals") or []:
        est = estimate(org)
        pid = proposals.create_from_estimate(conn, est["id"], created_by=uid)["id"]
        conn.execute("UPDATE proposal SET status = ?, price = ?, title = ?, sent_at = ?, signed_at = ?, declined_at = ? WHERE id = ?",
                     (p.get("status", "draft"), p.get("price"), p.get("title") or f"{street} - parapet repairs",
                      p.get("sent"), p.get("signed"), p.get("declined"), pid))
        conn.commit()
        refs[f"{p['ref']}.id"], refs[f"{p['ref']}.label"] = pid, proposals._label(proposals.get(conn, pid))
        refs[f"{p['ref']}.estimate"] = est["id"]
    if w.get("other_client"):
        other = crm.create_org(conn, name=f"Ievl {case['id']} Elsewhere", relationship_type="client", created_by=uid)
        refs["other_org"] = other
        for c in w["other_client"].get("contacts") or []:
            refs[f"{c['ref']}.id"] = crm.create_contact(conn, org_id=other, full_name=c["name"], created_by=uid)
        for p in w["other_client"].get("proposals") or []:
            est = estimate(other, addr=f"9{n} Elsewhere Ave, Queens, NY")
            pid = proposals.create_from_estimate(conn, est["id"], created_by=uid)["id"]
            conn.execute("UPDATE proposal SET status = ?, title = ? WHERE id = ?", (p.get("status", "sent"), p.get("title"), pid))
            conn.commit()
            refs[f"{p['ref']}.id"] = pid
    if "job" in w:   # "job": {} is a job with no documents yet
        j = w["job"] or {}
        code = f"FR-EV-{n:03d}"
        conn.execute("INSERT INTO projects (project_code, name, address, client_org_id, status) VALUES (?,?,?,?,'active')",
                     (code, street.split(",")[0], street, org))
        if j.get("from"):
            conn.execute("UPDATE estimate SET converted_project_code = ?, status = 'converted' WHERE id = ?",
                         (code, refs[f"{j['from']}.estimate"]))
        jdir = ssc_paths.under_root("data_room", "project_docs", code)
        jdir.mkdir(parents=True, exist_ok=True)
        for k, d in enumerate(j.get("docs") or []):
            path = jdir / f"ev{k}.pdf"
            path.write_bytes(("bytes of " + d.get("bytes_of", d["name"])).encode("utf-8"))
            conn.execute("INSERT INTO project_documents (project_code, category, title, doc_type, file_path, file_name, file_size, "
                         "mime, superseded, uploaded_at) VALUES (?,?,?,'PDF',?,?,?,'application/pdf',0,?)",
                         (code, d.get("folder", "DRAWINGS"), d.get("title") or d["name"], str(path), d["name"], d.get("size", 120000),
                          datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        refs["J"] = code
    me_addr, mate_addr = ids["me_email"], ids["mate_email"]
    thread = f"t-{TAG}-{n}"
    if case.get("prior"):   # an earlier email in the same conversation, with what was approved on it
        pm = inbox.ingest(conn, uid, _gmsg(f"g-{TAG}-{n}-p", thread, case["prior"], me_addr, 600), me_addr)
        chat = inbox._mail_chat(conn, inbox.message(conn, pm))
        for a in case["prior"].get("approved") or []:
            conn.execute("INSERT INTO claude_action (chat_id, message_id, tool_use_id, user_id, tool, input, summary, status, created_at, "
                         "decided_at) VALUES (?, 0, ?, ?, ?, '{}', ?, 'approved', ?, ?)",
                         (chat, f"tu-{TAG}-{n}-{a['tool']}", uid, a["tool"], a["summary"], datetime.now().isoformat(timespec="seconds"),
                          datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    mid = inbox.ingest(conn, uid, _gmsg(f"g-{TAG}-{n}", thread, fill(case["email"], refs), me_addr, 30), me_addr)
    refs["mail_id"] = mid
    link = case.get("link") or {}
    sets = {"client_org_id": org if link.get("client") else None, "project_code": refs.get("J") if link.get("job") else None,
            "proposal_id": refs.get(f"{link['proposal']}.id") if link.get("proposal") else None}
    for col, val in sets.items():
        if val:
            conn.execute(f"UPDATE mail_message SET {col} = ? WHERE id = ?", (val, mid))
    conn.commit()
    if case.get("mate_email"):
        refs["mate_mail_id"] = inbox.ingest(conn, mate, _gmsg(f"g-{TAG}-{n}-m", f"{thread}-m", case["mate_email"], mate_addr, 40), mate_addr)
    return refs


def outcome(conn, mid: int) -> dict:
    """What happened on the email's Claude pass: the prompt, every proposed tool, refused or made into a card."""
    chat = conn.execute("SELECT chat_id FROM mail_message WHERE id = ?", (mid,)).fetchone()["chat_id"]
    msgs = [dict(r) for r in conn.execute("SELECT role, kind, content FROM claude_chat_message WHERE chat_id = ? ORDER BY id", (chat,)).fetchall()]
    prompt = next(("".join(b.get("text", "") for b in json.loads(m["content"]) if b.get("type") == "text")
                   for m in msgs if m["role"] == "user" and m["kind"] == "text"), "")
    uses, results = {}, {}
    for m in msgs:
        for b in json.loads(m["content"]):
            if b.get("type") == "tool_use":
                uses[b["id"]] = (b["name"], b.get("input") or {})
            elif b.get("type") == "tool_result":
                results[b["tool_use_id"]] = b
    proposed = []
    for tid, (name, inp) in uses.items():
        r = results.get(tid) or {}
        text = r.get("content") if isinstance(r.get("content"), str) else json.dumps(r.get("content"))
        proposed.append({"tool": name, "input": inp, "refused": bool(r.get("is_error")), "answer": text or ""})
    cards = [dict(r) for r in conn.execute("SELECT id, tool, input, status, user_id, chat_id FROM claude_action WHERE chat_id = ? "
                                           "AND tool_use_id LIKE 'toolu_%' ORDER BY id", (chat,)).fetchall()]
    usage = conn.execute("SELECT COALESCE(SUM(cost_micro), 0) AS c FROM claude_chat_message WHERE chat_id = ?", (chat,)).fetchone()["c"]
    return {"chat": chat, "prompt": prompt, "proposed": proposed, "cards": cards, "cost_micro": usage or 0}


def then(conn, case: dict, refs: dict) -> None:
    """The change made after the card was shown (by hand, or by someone else)."""
    import crm
    for step in case.get("then") or []:
        if "sign" in step:
            conn.execute("UPDATE proposal SET status = 'signed', signed_at = ?, sent_at = COALESCE(sent_at, ?) WHERE id = ?",
                         (datetime.now().isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds"), refs[f"{step['sign']}.id"]))
        if "contact" in step:
            for ref, vals in step["contact"].items():
                crm.update_contact(conn, refs[f"{ref}.id"], **vals)
        if "job_doc_from_mail" in step:   # someone filed the attachment by hand (its source remembered)
            m = conn.execute("SELECT gmail_id FROM mail_message WHERE id = ?", (refs["mail_id"],)).fetchone()
            for name in step["job_doc_from_mail"]:
                conn.execute("INSERT INTO project_documents (project_code, category, title, doc_type, file_path, file_name, file_size, mime, "
                             "superseded, uploaded_at, source_key) VALUES (?, 'DRAWINGS', ?, 'PDF', '/nowhere', ?, 1, 'application/pdf', 0, ?, ?)",
                             (refs["J"], name, name, datetime.now().isoformat(timespec="seconds"), f"mail:{m['gmail_id']}:{name}"))
    conn.commit()


def score_live(case: dict, out: dict) -> dict:
    """The real model: did it propose what it must, and nothing it must not? A refused proposal still counts as proposed
    (the server caught it, but Claude got it wrong)."""
    live = case.get("live") or {}
    names = [p["tool"] for p in out["proposed"] if p["tool"] in WRITE_TOOLS]
    missing = [t for t in live.get("must") or [] if t not in names]
    wrong = [t for t in live.get("must_not") or [] if t in names]
    caught = [p["tool"] for p in out["proposed"] if p["refused"] and p["tool"] in WRITE_TOOLS]
    return {"id": case["id"], "ok": not missing and not wrong, "proposed": names, "missing": missing, "wrong": wrong,
            "caught": caught, "cost_micro": out["cost_micro"]}


WRITE_TOOLS = {"draft_proposal", "reply_email", "create_estimate", "create_event", "create_task", "save_email_attachments",
               "log_proposal_sent", "add_client_note", "update_client_record", "file_to_job", "send_proposal", "update_proposal",
               "create_proposal", "change_estimate_status", "email_client", "update_task_or_event", "mark_done", "update_job"}
