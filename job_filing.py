"""Claude files the documents that went back and forth by email into the job's Documents (2026-11-18).

Accepted prototype: https://claude.ai/artifact/VrgB7MGzsHfG9VyUZZB4nG. One card kind, `file_to_job`, in two places:
  * on an email (sent or received) in a conversation about a job that exists: Claude proposes it from the Inbox
  * when a job is created (`convert_to_project`, so also a signed proposal starting one): `offer_on_new_job` gathers the
    estimate's own files and every attachment in the person's threads linked to the estimate / its proposals or about
    the same address, and makes the card itself (no Claude call). The job's Documents tab shows it.

Each file: tick box, title, folder (the job's six). A file already on the job (same source, or same name + size, or the
signed proposal filed by signed_contracts) shows as "Already filed" and is skipped; at approval, identical bytes are
skipped too. A newer revision of a document on the job (Rev C after Rev B) is filed as its new version (the old one
superseded). Only the person's own mailbox is ever read (one Gmail per account). Undo removes what the card filed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import ssc_paths

log = logging.getLogger(__name__)

FOLDERS = {"PERMITS": "Permits", "DRAWINGS": "Drawings & Specs", "CONTRACTS": "Contracts & Insurance",
           "INSPECTIONS": "Inspections & Reports", "SAFETY": "Safety & Compliance", "CLOSEOUT": "Closeout"}
EXT_TYPE = {".pdf": ("PDF", "application/pdf"), ".jpg": ("JPG", "image/jpeg"), ".jpeg": ("JPG", "image/jpeg"),
            ".png": ("PNG", "image/png"), ".heic": ("HEIC", "image/heic"), ".heif": ("HEIC", "image/heif"),
            ".xlsx": ("XLSX", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            ".xlsm": ("XLSM", "application/vnd.ms-excel.sheet.macroEnabled.12"), ".xls": ("XLS", "application/vnd.ms-excel"),
            ".csv": ("CSV", "text/csv")}   # = server._PROJDOC_EXT_TYPE (what the Documents tab accepts)
TINY_IMAGE = 30 * 1024   # a picture this small is a logo or a signature, never a document
MAX_FILES = 40
_SCHEMA = {"ok": False}


def _rows(conn, sql, params=()) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params=()) -> Optional[dict]:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_schema(conn) -> None:
    """Idempotent: project_documents.source_key ('mail:<gmail id>:<file name>' / 'estdoc:<id>') so filing twice is a no-op."""
    if _SCHEMA["ok"]:
        return
    import signed_contracts
    signed_contracts.ensure_schema(conn)
    if "source_key" not in signed_contracts._columns(conn, "project_documents"):
        conn.execute("ALTER TABLE project_documents ADD COLUMN source_key TEXT")
        conn.commit()
    # a file whose bytes were already on the job when someone approved it: never offered again for that job
    conn.execute("CREATE TABLE IF NOT EXISTS job_file_seen (project_code TEXT NOT NULL, source_key TEXT NOT NULL, "
                 "doc_id INTEGER, seen_at TEXT NOT NULL)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_job_file_seen ON job_file_seen(project_code, source_key)")
    conn.commit()
    _SCHEMA["ok"] = True


def suggest_folder(name: str) -> str:
    """The Documents tab's own filename rule (server._projdoc_suggest_category)."""
    f = (name or "").lower()
    for folder, keys in (("CONTRACTS", ("coi", "insurance", "contract", "aia", "sov", "pay app", "payapp", "pay_app", "g702", "proposal")),
                         ("SAFETY", ("rope", "sds", "safety", "sst", "toolbox")),
                         ("DRAWINGS", ("arch", "drawing", "draw", "rev", "struct", "shop", "spec", "detail", "product data")),
                         ("INSPECTIONS", ("fisp", "inspect", "probe", "tr1", "qewi", "signoff", "sign-off", "report", "survey")),
                         ("CLOSEOUT", ("completion", "warrant", "lien", "closeout")),
                         ("PERMITS", ("permit", "pw2", "best", "variance", "shed"))):
        if any(k in f for k in keys):
            return folder
    return "DRAWINGS" if f.endswith((".jpg", ".jpeg", ".png", ".heic", ".heif")) else "CONTRACTS" if f.endswith((".xlsx", ".xls", ".csv", ".xlsm")) else "PERMITS"


def _stem(name: str) -> str:
    return re.sub(r"[_\s]+", " ", Path(name or "").stem).strip()


def _base(name: str) -> str:
    """'41HT Shop Drawings Rev C.pdf' and '41HT shop drawings rev B' -> '41ht shop drawings' (a revision's family)."""
    s = _stem(name).lower()
    s = re.sub(r"\b(rev(ision)?|ver(sion)?|v)\.?\s*[a-z0-9]{1,3}\b", " ", s)
    s = re.sub(r"\b(draft|final|signed|copy|updated)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _job_docs(conn, code: str) -> list:
    return _rows(conn, "SELECT id, category, title, file_name, file_size, file_path, source_key, source_proposal_id "
                       "FROM project_documents WHERE project_code = ? AND COALESCE(superseded, 0) = 0", (code,))


def _proposal_numbers(conn, ids) -> dict:
    import proposal_engine
    out = {}
    for i in {i for i in ids if i}:
        p = _one(conn, "SELECT id, number, display_no, version FROM proposal WHERE id = ?", (i,))
        if p:
            out[i] = proposal_engine.label(p)
    return out


def _sources(conn, uid: int, inp: dict, code: str) -> list:
    """[{src, group, name, size, title, folder}] in order; refuses files that are not this person's or this job's."""
    import inbox
    out = []
    for i, f in enumerate((inp.get("files") or [])[:MAX_FILES]):
        if not isinstance(f, dict):
            continue
        folder = str(f.get("folder") or "").upper()
        if f.get("estimate_doc_id"):
            d = _one(conn, "SELECT d.*, e.code, e.converted_project_code FROM estimate_document d JOIN estimate e ON e.id = d.estimate_id "
                           "WHERE d.id = ?", (int(f["estimate_doc_id"]),))
            if not d or d["converted_project_code"] != code:
                raise ValueError(f"estimate document {f['estimate_doc_id']} is not from {code}'s estimate")
            name = d["file_name"] or d["title"] or "document"
            out.append({"src": f"estdoc:{d['id']}", "group": ("est", d["estimate_id"], d["code"]), "name": name,
                        "size": d.get("file_size") or 0, "title": str(f.get("title") or d.get("title") or _stem(name))[:120],
                        "folder": folder if folder in FOLDERS else suggest_folder(name), "estdoc": d, "i": i})
            continue
        m = inbox.message(conn, int(f.get("mail_id") or 0), uid)
        if not m:
            raise LookupError("that email is not in this person's inbox")
        att = next((a for a in m["attachments"] if a["name"] == f.get("name")), None)
        if not att:
            raise ValueError(f"'{f.get('name')}' is not attached to that email")
        out.append({"src": f"mail:{m['gmail_id']}:{att['name']}", "group": ("mail", m["id"], m), "name": att["name"],
                    "size": att.get("size") or 0, "title": str(f.get("title") or _stem(att["name"]))[:120],
                    "folder": folder if folder in FOLDERS else suggest_folder(att["name"]), "att": att, "mail": m, "i": i})
    return out


def plan(conn, uid: int, inp: dict) -> dict:
    """{job, groups: [{title, meta, dir, rows: [{key, name, size, title, folder, note, dupe, replaces}]}], count}."""
    ensure_schema(conn)
    code = str(inp.get("project_code") or "").strip()
    job = _one(conn, "SELECT project_code, name, address FROM projects WHERE project_code = ?", (code,))
    if not job:
        raise LookupError("no job with that code; use list_jobs to find it")
    docs = _job_docs(conn, code)
    keys = {d["source_key"] for d in docs if d.get("source_key")}
    keys |= {r["source_key"] for r in _rows(conn, "SELECT source_key FROM job_file_seen WHERE project_code = ?", (code,))}
    named = {(str(d["file_name"] or "").lower(), int(d["file_size"] or 0)) for d in docs}
    signed = _proposal_numbers(conn, [d.get("source_proposal_id") for d in docs])
    groups, order = {}, []
    for s in _sources(conn, uid, inp, code):
        g = s["group"]
        gk = f"{g[0]}{g[1]}"
        if gk not in groups:
            if g[0] == "est":
                groups[gk] = {"title": f"Estimate {g[2]} · its own files", "meta": "uploaded during estimating", "dir": "", "rows": []}
            else:
                m = g[2]
                out = m["direction"] == "out"
                day = (m.get("received_at") or "")[:10]
                when = f"{day[5:7]}/{day[8:10]}" if len(day) == 10 else ""
                groups[gk] = {"title": m.get("subject") or "(no subject)", "meta": f"{'Sent' if out else 'Received'} {when}".strip(),
                              "dir": "Sent" if out else "Received", "rows": []}
            order.append(gk)
        row = {"key": f"f{s['i']}", "name": s["name"], "size": s["size"], "title": s["title"], "folder": s["folder"],
               "note": "", "dupe": "", "replaces": None, "src": s["src"]}
        low = s["name"].lower()
        if s["src"] in keys or (low, int(s["size"] or 0)) in named:
            row["dupe"] = "Already filed (same file)"
        elif any(n and re.search(rf"(?<!\d){re.escape(str(n).split('-')[0])}(?!\d)", low) and "proposal" in low for n in signed.values()):
            row["dupe"] = "Already filed: the signed proposal (Contracts)"
        elif not low.endswith(tuple(EXT_TYPE)):
            continue   # a file type the Documents tab can't hold (a .zip, a .dwg): not offered
        elif low.endswith((".jpg", ".jpeg", ".png", ".heic", ".heif")) and 0 < int(s["size"] or 0) < TINY_IMAGE:
            continue
        else:
            fam = _base(s["name"])
            old = next((d for d in docs if fam and len(fam) >= 6 and _base(d["file_name"] or d["title"] or "") == fam), None)
            if old:
                row["replaces"] = old["id"]
                row["note"] = f"New version of “{old['title'] or old['file_name']}” (it moves to history)"
        groups[gk]["rows"].append(row)
    out = [groups[k] for k in order if groups[k]["rows"]]
    out.sort(key=lambda g: 0 if not g["dir"] else 1)   # the estimate's own files first
    return {"job": job, "groups": out, "count": sum(1 for g in out for r in g["rows"] if not r["dupe"])}


def check(ctx, inp) -> None:
    p = plan(ctx.conn, ctx.uid, inp)
    if not p["count"]:
        raise ValueError(f"every one of those files is already on {p['job']['project_code']}; nothing to file")


def moot(conn, a: dict, inp: dict) -> Optional[str]:
    try:
        p = plan(conn, a.get("user_id"), inp)
    except LookupError as e:
        return "That job was deleted" if "job" in str(e) else None
    except ValueError:
        return None
    return None if p["count"] else f"Every file is already on {p['job']['project_code']}"


def _size(n) -> str:
    n = int(n or 0)
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, round(n / 1024))} KB" if n else ""


def card(ctx, inp) -> dict:
    try:
        p = plan(ctx.conn, ctx.uid, inp)
    except (LookupError, ValueError):
        return {"title": "File to the job", "fields": []}
    j = p["job"]
    threads = sum(1 for g in p["groups"] if g["dir"])
    groups = [{"title": g["title"], "meta": g["meta"], "dir": g["dir"],
               "rows": [{"key": r["key"], "kind": "file", "name": r["name"], "size": _size(r["size"]), "title": r["title"],
                         "folder": r["folder"], "note": r["note"], "dupe": r["dupe"]} for r in g["rows"]]} for g in p["groups"]]
    fields = []
    for g in p["groups"]:
        for r in g["rows"]:
            if not r["dupe"]:
                fields += [{"key": r["key"] + ".title", "label": "Title", "type": "change", "value": r["title"]},
                           {"key": r["key"] + ".folder", "label": "Folder", "type": "change", "value": r["folder"]}]
    return {"title": "File to the job", "client": f"{j['project_code']} · {j['name'] or j['address'] or ''}".strip(" ·"),
            "subtitle": f"from its estimate and {threads} email thread{'s' if threads != 1 else ''}" if inp.get("new_job") else "",
            "groups": groups, "count": p["count"], "approve_word": "File", "unit": "document",
            "folder_options": [{"value": k, "label": v} for k, v in FOLDERS.items()], "fields": fields}


def _bytes(conn, uid: int, s: dict) -> bytes:
    if s.get("estdoc"):
        base = ssc_paths.under_root("data_room").resolve()
        path = Path(s["estdoc"]["file_path"] or "").resolve()
        if not path.is_file() or not path.is_relative_to(base):
            raise ValueError(f"{s['name']} is missing from the estimate's files")
        return path.read_bytes()
    import inbox
    return inbox.attachment_bytes(conn, uid, s["mail"], s["att"])


def _hash_file(path: str) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def execute(ctx, inp) -> dict:
    ensure_schema(ctx.conn)
    p = plan(ctx.conn, ctx.uid, inp)
    code = p["job"]["project_code"]
    srcs = {f"f{s['i']}": s for s in _sources(ctx.conn, ctx.uid, inp, code)}
    have = None   # hashes of what is on the job, read once, only if something is being filed
    base = ssc_paths.under_root("data_room", "project_docs").resolve()
    pdir = ssc_paths.under_root("data_room", "project_docs") / code
    if not pdir.resolve().is_relative_to(base):
        raise ValueError("invalid job folder")
    filed, replaced, lines, skipped, mails = [], [], [], 0, set()
    for g in p["groups"]:
        for r in g["rows"]:
            if r["dupe"]:
                continue
            fk, tk = r["key"] + ".folder", r["key"] + ".title"
            folder = inp.get(fk, r["folder"]) if fk in inp else r["folder"]
            if not folder:
                continue   # unticked on the card
            folder = str(folder).upper()
            if folder not in FOLDERS:
                raise ValueError(f"pick one of the job's folders for {r['name']}")
            title = (str(inp.get(tk) or "").strip() if tk in inp else r["title"]) or _stem(r["name"])
            s = srcs[r["key"]]
            data = _bytes(ctx.conn, ctx.uid, s)
            digest = hashlib.sha256(data).hexdigest()
            if have is None:
                have = {_hash_file(d["file_path"]): d["id"] for d in _job_docs(ctx.conn, code)}
            if digest in have:
                skipped += 1
                if not _one(ctx.conn, "SELECT 1 FROM job_file_seen WHERE project_code = ? AND source_key = ?", (code, r["src"])):
                    ctx.conn.execute("INSERT INTO job_file_seen (project_code, source_key, doc_id, seen_at) VALUES (?,?,?,?)",
                                     (code, r["src"], have[digest], _now()))
                    ctx.conn.commit()
                continue
            ext = Path(r["name"]).suffix.lower()
            doc_type, mime = EXT_TYPE[ext]
            pdir.mkdir(parents=True, exist_ok=True)
            fpath = pdir / (uuid.uuid4().hex + ext)
            fpath.write_bytes(data)
            try:
                ctx.conn.execute(
                    "INSERT INTO project_documents (project_code, category, title, doc_type, file_path, file_name, file_size, "
                    "mime, superseded, uploaded_by_uid, uploaded_at, supersedes_id, source_key, notes) VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
                    (code, folder, title[:200], doc_type, str(fpath), Path(r["name"]).name, len(data), mime, ctx.uid, _now(),
                     r["replaces"], r["src"], "Filed by Claude from " + ("the estimate" if s.get("estdoc") else
                                                                         f"an email: {(s['mail'].get('subject') or '')[:120]}")))
                did = _one(ctx.conn, "SELECT MAX(id) AS m FROM project_documents WHERE project_code = ? AND source_key = ?",
                           (code, r["src"]))["m"]
                if r["replaces"]:
                    ctx.conn.execute("UPDATE project_documents SET superseded = 1 WHERE id = ? AND project_code = ?", (r["replaces"], code))
                    replaced.append(r["replaces"])
                ctx.conn.commit()
            except Exception:
                ctx.conn.rollback()
                fpath.unlink(missing_ok=True)
                raise
            have[digest] = did
            filed.append(did)
            lines.append(f"{title} → {FOLDERS[folder]}")
            if s.get("mail"):
                mails.add(s["mail"]["id"])
    if not filed and not skipped:
        raise ValueError("every file was unticked; nothing to file (Decline instead)")
    import inbox
    for mid in mails:
        try:
            inbox.link_record(ctx.conn, mid, "project", code, ctx.uid)
        except Exception:
            ctx.conn.rollback()
    n = len(filed)
    summary = f"{n} document{'s' if n != 1 else ''} filed to {code}" + (f" · {skipped} already there" if skipped else "")
    log.info("job filing: %s document(s) filed to %s, %s skipped", n, code, skipped)
    return {"id": code, "summary": summary, "lines": lines, "link": f"/projects/{code}#documents", "undo": bool(filed),
            "filed": filed, "replaced": replaced}


def undo(ctx, action, result) -> None:
    code = result["id"]
    for did in result.get("filed") or []:
        d = _one(ctx.conn, "SELECT file_path FROM project_documents WHERE id = ? AND project_code = ?", (did, code))
        if not d:
            continue
        ctx.conn.execute("DELETE FROM project_documents WHERE id = ? AND project_code = ?", (did, code))
        try:
            Path(d["file_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    for did in result.get("replaced") or []:
        ctx.conn.execute("UPDATE project_documents SET superseded = 0 WHERE id = ? AND project_code = ?", (did, code))
    ctx.conn.commit()


def today_text(conn, uid: int, inp: dict) -> str:
    try:
        p = plan(conn, uid, inp)
    except (LookupError, ValueError):
        return ""
    return "\n".join(f"{r['title']} → {FOLDERS[r['folder']]}" for g in p["groups"] for r in g["rows"] if not r["dupe"])


# ------------------------------------------------------------------ when a job is created
def _address_key(addr: str) -> Optional[str]:
    """'41 Harborview Terrace, Brooklyn' -> '41 harborview' (house number + first street word), or None."""
    m = re.match(r"\s*(\d+[a-z]?(?:-\d+)?)\s+(?:(?:east|west|north|south|e|w|n|s)\.?\s+)?([a-z0-9]+)", (addr or "").lower())
    return f"{m.group(1)} {m.group(2)}" if m else None


def gather(conn, uid: int, est_id: int, code: str) -> list:
    """The files a new job could take: its estimate's own, then attachments in this person's threads linked to the
    estimate or its proposals, or naming the same address. Sent and received (Angela 2026-11-18)."""
    import inbox
    est = _one(conn, "SELECT id, code, client_org_id, building_address FROM estimate WHERE id = ?", (est_id,))
    if not est:
        return []
    files = [{"estimate_doc_id": d["id"]} for d in _rows(conn, "SELECT id, category FROM estimate_document WHERE estimate_id = ? "
                                                                "ORDER BY id", (est_id,)) if d["category"] != "proposal"]
    pids = [r["id"] for r in _rows(conn, "SELECT id FROM proposal WHERE estimate_id = ?", (est_id,))]
    mids = set()
    if uid:
        ph = ",".join("?" * len(pids)) or "NULL"
        for r in _rows(conn, f"SELECT id FROM mail_message WHERE user_id = ? AND trashed_at IS NULL AND (estimate_id = ? "
                             f"OR proposal_id IN ({ph}))", [uid, est_id] + pids):
            mids.add(r["id"])
        links = [("estimate", str(est_id))] + [("proposal", str(p)) for p in pids]
        for et, eid in links:
            for r in _rows(conn, "SELECT l.message_id FROM mail_link l JOIN mail_message m ON m.id = l.message_id "
                                 "WHERE m.user_id = ? AND l.entity_type = ? AND l.entity_id = ?", (uid, et, eid)):
                mids.add(r["message_id"])
        key = _address_key(est.get("building_address"))
        if key and est.get("client_org_id"):
            for r in inbox.client_mail(conn, est["client_org_id"], uid):
                m = inbox.message(conn, r["id"], uid) or {}   # subject and text are stored encrypted
                text = re.sub(r"\s+", " ", f"{m.get('subject') or ''} {m.get('body') or ''}".lower())
                if key in text:
                    mids.add(r["id"])
    for mid in sorted(mids):
        m = inbox.message(conn, mid, uid)
        if not m or m.get("trashed_at"):
            continue
        for a in m["attachments"]:
            files.append({"mail_id": mid, "name": a["name"]})
    return files[:MAX_FILES]


def offer_on_new_job(conn, est_id: int, code: str, actor: Optional[int]) -> Optional[int]:
    """Make the "Claude found N documents for this job" card for the person who created the job. Never raises: a job is
    never refused over this. Returns the card id, or None when there is nothing to file."""
    if not actor:
        return None
    try:
        import claude_agent
        claude_agent.ensure(conn)
        u = _one(conn, "SELECT id, role FROM users WHERE id = ?", (actor,))
        if not u or u["role"] not in claude_agent.OFFICE_ROLES:
            return None
        if _one(conn, "SELECT id FROM claude_action WHERE user_id = ? AND tool = 'file_to_job' AND status = 'pending' "
                      "AND input LIKE ?", (actor, f'%"project_code": "{code}"%')):
            return None
        inp = {"project_code": code, "files": gather(conn, actor, est_id, code), "new_job": True}
        p = plan(conn, actor, inp)
        if not p["count"]:
            return None
        threads = sum(1 for g in p["groups"] if g["dir"])
        now = _now()
        conn.execute("INSERT INTO claude_chat (user_id, surface, title, source, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                     (actor, "office", f"Documents for {code}", "job", now, now))
        conn.commit()
        chat = _one(conn, "SELECT MAX(id) AS m FROM claude_chat WHERE user_id = ? AND source = 'job'", (actor,))["m"]
        text = (f"I found {p['count']} document{'s' if p['count'] != 1 else ''} for {code} in its estimate"
                + (f" and {threads} email thread{'s' if threads != 1 else ''}" if threads else "") + ". Review and file them in one step.")
        mid = claude_agent._add_msg(conn, chat, "assistant", "text", [{"type": "text", "text": text}])
        conn.execute("INSERT INTO claude_action (chat_id, message_id, tool_use_id, user_id, tool, input, summary, status, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (chat, mid, f"job-{code}-{uuid.uuid4().hex[:8]}", actor, "file_to_job", json.dumps(inp),
                                                  f"File {p['count']} documents to {code}", "pending", now))
        conn.commit()
        aid = _one(conn, "SELECT MAX(id) AS m FROM claude_action WHERE chat_id = ?", (chat,))["m"]
        log.info("job filing: offered %s document(s) for new job %s (card %s)", p["count"], code, aid)
        return aid
    except Exception as e:
        log.warning("job filing offer skipped for %s (%s)", code, type(e).__name__)
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def pending_for_job(conn, uid: int, code: str) -> Optional[dict]:
    """The person's waiting card for this job (the Documents tab's banner): {chat_id, action_id, count}."""
    import claude_agent
    claude_agent.sweep_moot(conn, user_id=uid)
    since = (datetime.now() - claude_agent.CARD_TTL).isoformat(timespec="seconds")
    for a in _rows(conn, "SELECT id, chat_id, input FROM claude_action WHERE user_id = ? AND tool = 'file_to_job' "
                         "AND status = 'pending' AND created_at >= ? ORDER BY id DESC", (uid, since)):
        try:
            inp = json.loads(a["input"] or "{}")
        except ValueError:
            continue
        if inp.get("project_code") == code:
            try:
                n = plan(conn, uid, inp)["count"]
            except (LookupError, ValueError):
                continue
            return {"chat_id": a["chat_id"], "action_id": a["id"], "count": n}
    return None
