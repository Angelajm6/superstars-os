"""The company's past proposals, uploaded (accepted prototype 2026-10-19; reworked 2026-10-21).

Since 2026-10-21 every upload is read once by Claude into the structure a new proposal has (proposal_engine.learn):
its wording becomes the library and its lines the price history. The layout itself is fixed in code
(proposal_layout.py); the template HTML below only still draws proposals made before that date.

Before 2026-10-21:
An admin / C-suite person uploads the proposal they already send (PDF works best; a Word file gives Claude
the words but not the layout). Claude reads it once and writes a self-contained HTML version of that
layout with {{placeholders}}; every proposal PDF is then drawn in it (proposals.render_document), including the
ones Claude drafts from an email. Removing the template goes back to the built-in layout.

  GET    /api/proposal-template           -> {active, name, uploaded_at, uploaded_by_name, state, error}
  POST   /api/proposal-template           multipart `file` (pdf / docx, 10 MB) -> starts the read (background)
  GET    /api/proposal-template/preview   -> a sample proposal drawn in the template (HTML, for a sandboxed frame)
  DELETE /api/proposal-template           -> back to the built-in layout

Stored in app_settings['proposal_template'] (JSON: name, rel path, html, state ready|reading|failed, error).
The generated HTML is sanitized: no scripts, no event handlers, no outside URLs (the PDF engine renders a
local file and must never fetch anything).
"""
from __future__ import annotations

import base64
import html as _html
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from flask import jsonify, request

import db_layer
import ssc_paths
from auth import SCRIPT_DIR, current_user, requires_section

log = logging.getLogger(__name__)

KEY = "proposal_template"
MAX_BYTES = 10 * 1024 * 1024
EXT = {".pdf": "application/pdf",
       ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
       # a picture of the proposal (screenshot / photo / scan) keeps the layout too (2026-10-20)
       ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".heic": "image/heic", ".webp": "image/webp"}
SAVE_AS_PDF = ("Claude can't read {ext} files. Open it and save it as a PDF (File › Export, or Print › Save as PDF), "
               "then upload the PDF. A screenshot of the proposal works too.")
SECTION = "company_portal"

TOKENS = ("company_name", "company_address", "company_phone", "company_email", "company_license", "logo",
          "number", "date", "valid_until", "title", "client_name", "contact_name", "recipient_email", "building",
          "scope", "line_items_rows", "total", "deposit", "schedule", "exclusions", "terms", "signature",
          "intro", "reference", "extra_sections", "submitted_by")
REQUIRED = ("scope", "line_items_rows", "total")

# ------------------------------------------------------------------ storage
def _conn():
    return db_layer.connect()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _get_raw(conn) -> dict:
    try:
        r = conn.execute("SELECT value FROM app_settings WHERE key = ?", (KEY,)).fetchone()
    except Exception:
        conn.rollback()
        return {}
    if not r or not r["value"]:
        return {}
    try:
        v = json.loads(r["value"])
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


STUCK_MINUTES = 10


def _store(conn) -> dict:
    """{templates: [...every upload...], active_id, next_id} (2026-10-20: every template is kept and listed).
    The one-template shape from before is read as a list of one. A read that never finished (the server restarted
    mid-read) shows as failed after STUCK_MINUTES, so it never says "reading" forever."""
    v = _get_raw(conn)
    if "templates" in v:
        v.setdefault("next_id", max([t.get("id", 0) for t in v["templates"]] + [0]) + 1)
        cut = (datetime.now() - timedelta(minutes=STUCK_MINUTES)).isoformat(timespec="seconds")
        for t in v["templates"]:
            if t.get("state") == "reading" and (t.get("uploaded_at") or "") < cut:
                t.update(state="failed", error="Claude didn't finish reading it. Remove it and upload it again.")
        return v
    if v.get("path"):
        t = dict(v, id=1)
        t.pop("previous", None)
        return {"templates": [t], "active_id": 1 if v.get("state") == "ready" else None, "next_id": 2}
    return {"templates": [], "active_id": None, "next_id": 1}


def _put(conn, value: Optional[dict]) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    conn.execute("DELETE FROM app_settings WHERE key = ?", (KEY,))
    if value is not None:
        conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)", (KEY, json.dumps(value), _now()))
    conn.commit()


_PREVIEW = threading.local()
_LOCK = threading.RLock()   # several uploads read at once: every read-change-write of the list is one at a time   # "See a sample" for one saved template draws it without making it the one in use


def _item(st: dict, tid) -> Optional[dict]:
    return next((t for t in st["templates"] if t.get("id") == tid), None)


def active_html(conn) -> Optional[str]:
    over = getattr(_PREVIEW, "html", None)
    if over:
        return over
    st = _store(conn)
    t = _item(st, st.get("active_id"))
    return t.get("html") if t and t.get("state") == "ready" and t.get("html") else None


def _names(conn, ids) -> dict:
    out = {}
    for uid in {i for i in ids if i}:
        try:
            u = conn.execute("SELECT COALESCE(NULLIF(display_name, ''), NULLIF(full_name, ''), email) AS n FROM users WHERE id = ?",
                             (uid,)).fetchone()
            out[uid] = u["n"] if u else None
        except Exception:
            conn.rollback()
    return out


def public(conn) -> dict:
    """The template in use (or the newest upload while Claude reads it) + every saved template."""
    st = _store(conn)
    act = _item(st, st.get("active_id"))
    newest = st["templates"][-1] if st["templates"] else None
    shown = newest if newest and newest.get("state") in ("reading", "failed") and newest is not act else act
    who = _names(conn, [t.get("uploaded_by") for t in st["templates"]])
    out = {"active": bool(act and act.get("state") == "ready"), "state": (shown or {}).get("state") or "none",
           "name": (shown or {}).get("name"), "uploaded_at": (shown or {}).get("uploaded_at"),
           "uploaded_by_name": who.get((shown or {}).get("uploaded_by")), "error": (shown or {}).get("error"),
           "active_id": st.get("active_id") if act else None,
           "templates": [{"id": t["id"], "name": t.get("name"), "ext": t.get("ext"), "state": t.get("state"), "error": t.get("error"),
                          "learned": _learned(t), "archive": _archive(conn, t),
                          "uploaded_at": t.get("uploaded_at"), "uploaded_by_name": who.get(t.get("uploaded_by")),
                          "in_use": bool(act and t["id"] == act["id"])} for t in reversed(st["templates"])]}
    return out


def _learned(t: dict) -> Optional[dict]:
    ex = t.get("example")
    if not isinstance(ex, dict):
        return None
    return {"job_type": ex.get("job_type"), "client": ex.get("client_name"), "lines": len(ex.get("lines") or []),
            "notes": len(ex.get("notes") or []), "exclusions": len(ex.get("exclusions") or []),
            "deliverables": len(ex.get("deliverables") or [])}


# ------------------------------------------------------------------ the archive (2026-10-21)
# Every upload is also a proposal from before the app: listed in Estimates & proposals with its own PDF, by the
# number, date, client, building and total Claude read (people can correct them).
ARCHIVE_FIELDS = ("number", "date", "client", "building", "total")


def _client_id(conn, name: str) -> Optional[int]:
    if not name:
        return None
    try:
        r = conn.execute("SELECT id FROM crm_organization WHERE LOWER(TRIM(name)) = ? ORDER BY id LIMIT 1",
                         (name.strip().lower(),)).fetchone()
    except Exception:
        conn.rollback()
        return None
    return r["id"] if r else None


def _archive(conn, t: dict) -> Optional[dict]:
    a = t.get("archive")
    if not isinstance(a, dict):
        return None
    return dict({k: a.get(k) for k in ARCHIVE_FIELDS}, edited=bool(a.get("edited")), client_org_id=_client_id(conn, a.get("client") or ""))


def archive_rows(conn, hidden: bool = False) -> list:
    """The archived proposals, shaped like proposals.public rows for the Estimates & proposals list. hidden=True: the
    ones an admin deleted from the list (the Deleted filter), which Claude still learns from."""
    out = []
    for t in _store(conn)["templates"]:
        a = _archive(conn, t) if t.get("state") == "ready" and not t.get("proposal_id") else None
        if not a or bool(t.get("hidden")) != hidden:   # marked as sent: it is a proposal in the list now (proposals.adopt_archived)
            continue
        out.append({"id": f"a{t['id']}", "archive_id": t["id"], "archived": True, "number": a["number"] or t.get("name"),
                    "label": a["number"] or t.get("name"), "client_name": a["client"], "client_org_id": a["client_org_id"],
                    "building_address": a["building"], "title": (t.get("example") or {}).get("title") or t.get("name"),
                    "price": a["total"] or None, "status": "archived", "display_status": "archived", "sent_at": a["date"] or None,
                    "updated_at": a["date"] or (t.get("uploaded_at") or "")[:10], "pdf_url": f"/api/proposal-template/{t['id']}/file",
                    "is_pdf": t.get("ext") == ".pdf", "hidden": bool(t.get("hidden"))})
    return out


def hide_archive(conn, tid: int, hidden: bool, *, actor=None) -> None:
    """Delete one from the Before the app list, or bring it back (2026-10-27). The upload and its file stay: Claude keeps
    learning wording and pricing from it, and an admin restores it from Deleted. Admins only (checked by the route)."""
    with _LOCK:
        st = _store(conn)
        t = _item(st, tid)
        if not t or t.get("proposal_id") or not isinstance(t.get("archive"), dict):
            raise LookupError("not found")
        if bool(t.get("hidden")) == hidden:
            raise ValueError("It is already deleted." if hidden else "It is already in the list.")
        if hidden:
            t.update(hidden=True, hidden_at=_now(), hidden_by=actor)
        else:
            for k in ("hidden", "hidden_at", "hidden_by"):
                t.pop(k, None)
        _put(conn, st)


def edit_archive(conn, tid: int, given: dict) -> dict:
    import proposal_engine
    with _LOCK:
        st = _store(conn)
        t = _item(st, tid)
        if not t:
            raise LookupError("not found")
        a = dict(t.get("archive") or {})
        for k in ARCHIVE_FIELDS:
            if k not in given:
                continue
            v = given[k]
            if k == "total":
                v = round(proposal_engine._num(v), 2)
            elif k == "date":
                v = proposal_engine.archive_facts({"date": _iso_date(v)})["date"]
                if str(given[k] or "").strip() and not v:
                    raise ValueError("Write the date as MM/DD/YYYY.")
            else:
                v = str(v or "").strip()[:300]
            a[k] = v
        a["edited"] = True
        t["archive"] = a
        _put(conn, st)
    return public(conn)


def _iso_date(v) -> str:
    """MM/DD/YYYY (how the app shows dates) or YYYY-MM-DD -> YYYY-MM-DD; anything else -> ''."""
    v = str(v or "").strip()
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", v)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return v if re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", v) else ""


def relearn(conn, uid) -> dict:
    """Read again, into the new structure, every upload that was only turned into a layout before 2026-10-21."""
    with _LOCK:
        st = _store(conn)
        todo = [t for t in st["templates"] if t.get("path") and (not isinstance(t.get("example"), dict)
                or not isinstance(t.get("archive"), dict) or t.get("state") != "ready")]   # incl. read before the archive
        for t in todo:
            t.update(state="reading", error=None, uploaded_at=_now())
        _put(conn, st)
    for t in todo:
        threading.Thread(target=_read_job, args=(t["id"], t["path"], t.get("ext") or "", t.get("name") or "", uid), daemon=True).start()
    return public(conn)


def use(conn, tid: int) -> dict:
    with _LOCK:
        return _use(conn, tid)


def _use(conn, tid: int) -> dict:
    st = _store(conn)
    t = _item(st, tid)
    if not t:
        raise LookupError("template not found")
    if t.get("state") != "ready":
        raise ValueError("Claude hasn't finished reading that one yet." if t.get("state") == "reading"
                         else "Claude couldn't read that one. Upload a cleaner PDF instead.")
    st["active_id"] = tid
    _put(conn, st)
    return public(conn)


def use_builtin(conn) -> dict:
    st = _store(conn)
    st["active_id"] = None
    _put(conn, st)
    return public(conn)


def remove(conn, tid: int) -> dict:
    with _LOCK:
        return _remove(conn, tid)


def _remove(conn, tid: int) -> dict:
    st = _store(conn)
    t = _item(st, tid)
    if not t:
        raise LookupError("template not found")
    st["templates"] = [x for x in st["templates"] if x.get("id") != tid]
    if st.get("active_id") == tid:   # the newest one left that Claude read gives the layout
        ready = [x for x in st["templates"] if x.get("state") == "ready"]
        st["active_id"] = ready[-1]["id"] if ready else None
    _put(conn, st)
    try:
        ssc_paths.resolve_data_path(t["path"]).unlink(missing_ok=True)
    except Exception as e:
        log.info("proposal template file not removed: %s", type(e).__name__)
    return public(conn)


def past_proposal_blocks(conn, limit: int = 4) -> list:
    """The newest past proposals as the model reads them (PDF / picture / Word text), so a new proposal copies
    their structure, wording, terms and pricing format exactly (Proposal Studio, 2026-10-20)."""
    out = []
    for t in list(reversed(_store(conn)["templates"]))[:limit]:
        try:
            raw = ssc_paths.resolve_data_path(t["path"]).read_bytes()
        except Exception:
            continue
        ext, name = t.get("ext") or "", t.get("name") or "proposal"
        try:
            if ext == ".pdf":
                out.append({"type": "document", "title": name, "source": {"type": "base64", "media_type": "application/pdf",
                                                                        "data": base64.b64encode(raw).decode("ascii")}})
            elif ext in (".png", ".jpg", ".jpeg", ".heic", ".webp"):
                import claude_files
                img, mime = claude_files._image_for_model(raw)
                out.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(img).decode("ascii")}})
            elif ext == ".docx":
                import claude_files
                out.append({"type": "document", "title": name, "source": {"type": "text", "media_type": "text/plain",
                                                                        "data": (claude_files._docx_text(raw) or "")[:60000] or "(empty)"}})
        except Exception as e:
            log.info("past proposal %s not readable: %s", t.get("id"), type(e).__name__)
    return out


def original(conn, tid: int):
    t = _item(_store(conn), tid)
    if not t or not t.get("path"):
        return None, None
    p = ssc_paths.resolve_data_path(t["path"])
    return (p if p.exists() else None), t


# ------------------------------------------------------------------ the HTML
_BAD_BLOCKS = re.compile(r"<\s*(script|iframe|object|embed|link|base|form|frame|frameset|applet)\b[^>]*>(.*?<\s*/\s*\1\s*>)?", re.I | re.S)
_ON_ATTR = re.compile(r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_META_REFRESH = re.compile(r"<\s*meta[^>]+http-equiv[^>]*>", re.I)
_EXT_URL = re.compile(r"(?i)(https?:|ftp:|//)[^\s\"')>]*")
_JS_URL = re.compile(r"(?i)javascript:")
_IMPORT = re.compile(r"(?i)@import[^;]*;")


def sanitize(html: str) -> str:
    h = (html or "").strip()
    m = re.search(r"```(?:html)?\s*(.*?)```", h, re.S | re.I)
    if m:
        h = m.group(1).strip()
    start = h.lower().find("<!doctype")
    if start < 0:
        start = h.lower().find("<html")
    if start > 0:
        h = h[start:]
    h = _BAD_BLOCKS.sub("", h)
    h = _META_REFRESH.sub("", h)
    h = _ON_ATTR.sub("", h)
    h = _IMPORT.sub("", h)
    h = _JS_URL.sub("", h)
    h = re.sub(r"(?is)<!--.*?-->", "", h)
    # outside addresses inside url(), src= and href= become nothing (the PDF engine never fetches)
    h = re.sub(r"(?i)url\(\s*['\"]?(https?:|//)[^)]*\)", "none", h)
    h = re.sub(r"(?i)(src|href)\s*=\s*(['\"])\s*(https?:|//|ftp:)[^'\"]*\2", r'\1=""', h)
    # the client's online page (proposals.public page) adds its bar and Accept / Decline at <head>/<body>
    h = re.sub(r"(?i)<body\b[^>]*>", "<body>", h)
    h = re.sub(r"(?i)</body\s*>", "</body>", h)
    h = re.sub(r"(?i)</head\s*>", "</head>", h)
    if "</head>" not in h:
        h = re.sub(r"(?i)(<html\b[^>]*>)", r"\1<head><meta charset='utf-8'></head>", h, count=1)
    return h


def check(html: str) -> Optional[str]:
    missing = [t for t in REQUIRED if "{{" + t + "}}" not in re.sub(r"\{\{\s*(\w+)\s*\}\}", r"{{\1}}", html)]
    if missing:
        return "Claude's version of the template is missing: " + ", ".join(missing) + ". Try a PDF of the template."
    if "<html" not in html.lower():
        return "Claude didn't return a full page. Try again, or upload a PDF of the template."
    return None


def logo_tag() -> str:
    """The company logo uploaded in Settings > Company brand; nothing until one is uploaded (2026-10-20)."""
    import company_brand
    uri = company_brand.logo_data_uri()
    return f'<img class="logo" alt="Superstars Contracting" src="{uri}">' if uri else ""


_COMPANY_RUN = re.compile(r"\{\{\s*company_name\s*\}\}(?:(?:\s|<[^>]{0,80}>|&nbsp;|&middot;|[|·•,\-–])*"
                          r"\{\{\s*company_(?:address|phone|email|license)\s*\}\})+")


def with_submitted_by(template: str) -> str:
    """Templates read before {{submitted_by}} existed show who it's from as {{company_name}} followed by the letterhead
    tokens; that run becomes {{submitted_by}} so each proposal can say who submits it (2026-10-21)."""
    if re.search(r"\{\{\s*submitted_by\s*\}\}", template):
        return template
    out, n = _COMPANY_RUN.subn("{{submitted_by}}", template, count=1)
    if n:
        return out
    m = re.search(r"(?i)submitted\s*by", template)   # a SUBMITTED BY box holding just {{company_name}}
    if m:
        t = re.compile(r"\{\{\s*company_name\s*\}\}").search(template, m.end(), m.end() + 600)
        if t:
            return template[:t.start()] + "{{submitted_by}}" + template[t.end():]
    return template


def fill(template: str, parts: dict) -> str:
    """parts: token -> HTML fragment (already escaped). Unknown tokens become empty."""
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: parts.get(m.group(1), ""), template)


def text(v) -> str:
    """Plain text -> safe HTML with line breaks kept."""
    return _html.escape(str(v or "")).replace("\n", "<br>")


# ------------------------------------------------------------------ reading the upload with Claude
def _block(raw: bytes, ext: str, name: str) -> dict:
    if ext == ".pdf":
        return {"type": "document", "title": name, "source": {"type": "base64", "media_type": "application/pdf",
                                                            "data": base64.b64encode(raw).decode("ascii")}}
    import claude_files
    if ext in (".png", ".jpg", ".jpeg", ".heic", ".webp"):
        img, mime = claude_files._image_for_model(raw)
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(img).decode("ascii")}}
    return {"type": "document", "title": name, "source": {"type": "text", "media_type": "text/plain",
                                                        "data": (claude_files._docx_text(raw) or "")[:60000] or "(empty)"}}


def _read_job(tid: int, rel: str, ext: str, name: str, uid) -> None:
    """Since 2026-10-21 an upload is read ONCE into the same structure a new proposal has (proposal_engine.learn):
    its wording, lines and prices become examples and library items. The layout itself is fixed in code."""
    conn = _conn()
    try:
        raw = ssc_paths.resolve_data_path(rel).read_bytes()
        import proposal_engine
        ex = proposal_engine.learn(_block(raw, ext, name), conn, uid)
        problem = None if (ex.get("lines") or ex.get("exclusions") or ex.get("notes")) else \
            "Claude couldn't find a proposal in that file. Upload the PDF of a proposal you sent."
        with _LOCK:
            st = _store(conn)
            t = _item(st, tid)
            if not t:   # removed while reading
                return
            if problem:
                t.update({"state": "failed", "error": problem})
            else:   # a person's corrections to the archive facts survive a re-read
                arch = t["archive"] if (t.get("archive") or {}).get("edited") else (ex.get("archive") or {})
                t.update({"state": "ready", "example": ex, "archive": arch, "error": None})
            _put(conn, st)
        log.info("proposal template %s read: %s", tid, "failed" if problem else "ready")
    except Exception as e:
        log.warning("proposal template read failed: %s", type(e).__name__)
        try:
            with _LOCK:
                st = _store(conn)
                t = _item(st, tid)
                if t:
                    t.update({"state": "failed", "error": "Claude couldn't read that file. Try a PDF of the proposal."})
                    _put(conn, st)
        except Exception:
            pass
    finally:
        conn.close()


def start_read(conn, filename: str, raw: bytes, uid, *, background=True) -> dict:
    ext = Path(filename or "").suffix.lower()
    if ext not in EXT:
        raise ValueError(SAVE_AS_PDF.format(ext=(ext.upper().lstrip(".") or "those")))
    if not raw:
        raise ValueError("That file is empty.")
    if len(raw) > MAX_BYTES:
        raise ValueError("The template must be 10 MB or smaller.")
    import assistant
    why = assistant.unavailable_reason()
    if why:
        raise ValueError("Claude isn't switched on yet, so it can't read the template." if why == "no_api_key"
                         else "Claude is switched off (daily limit set to zero).")
    d = ssc_paths.under_root("data_room", "proposal_template")
    d.mkdir(parents=True, exist_ok=True)
    p = d / (uuid.uuid4().hex + ext)
    p.write_bytes(raw)
    rel = ssc_paths.store_rel(p)
    name = Path(filename).name[:120]
    with _LOCK:
        st = _store(conn)
        tid = st["next_id"]
        st["next_id"] = tid + 1
        st["templates"].append({"id": tid, "name": name, "path": rel, "ext": ext, "state": "reading", "uploaded_at": _now(),
                                "uploaded_by": uid, "html": None, "error": None})
        _put(conn, st)
    if background:
        threading.Thread(target=_read_job, args=(tid, rel, ext, name, uid), daemon=True).start()
    else:
        _read_job(tid, rel, ext, name, uid)
    return public(conn)


def sample_html(conn, tid: Optional[int] = None) -> str:
    """A sample proposal in the template in use, or in saved template `tid`."""
    if tid is not None:
        t = _item(_store(conn), tid)
        if not t or t.get("state") != "ready":
            raise LookupError("template not ready")
        _PREVIEW.html = t["html"]
        try:
            return sample_html(conn)
        finally:
            _PREVIEW.html = None
    import proposals
    r = {"id": 0, "number": "SAMPLE-P1", "version": 1, "status": "draft", "title": "Exterior repairs · 410 E 58th St",
         "client_name": "Sample Client LLC", "recipient_name": "Dana Sample", "recipient_email": "client@example.com",
         "building_address": "410 E 58th St, New York, NY", "scope_summary": "Repoint the east elevation and replace six steel lintels.",
         "price": 48550, "deposit_pct": 30, "deposit_amount": 14565, "schedule_text": "About 6 weeks from the permit date.",
         "exclusions_text": proposals.DEFAULT_EXCLUSIONS, "terms_text": proposals.DEFAULT_TERMS,
         "valid_until": datetime.now().date().isoformat(), "updated_at": _now()}
    lis = [{"description": "Mobilization, sidewalk shed and DOB permits", "qty": 1, "unit": "ls", "unit_price": 6400, "amount": 6400},
           {"description": "Brick repointing, east elevation", "qty": 1850, "unit": "sf", "unit_price": 15, "amount": 27750},
           {"description": "Steel lintel replacement", "qty": 6, "unit": "ea", "unit_price": 2400, "amount": 14400}]
    return proposals.render_document(conn, r, mode="pdf", lis=lis)


# ------------------------------------------------------------------ routes
def _uid():
    return (current_user() or {}).get("id")


def register(app) -> None:
    gate = requires_section(SECTION)

    @app.route("/api/proposal-template", methods=["GET"])
    @gate
    def proposal_template_get():
        conn = _conn()
        try:
            return jsonify({"data": public(conn)})
        finally:
            conn.close()

    @app.route("/api/proposal-template", methods=["POST"])
    @gate
    def proposal_template_upload():
        files = request.files.getlist("file")
        if not files:
            return jsonify({"error": "Choose the proposals to upload."}), 400
        conn = _conn()
        try:   # as many past proposals as they like, in one go
            errors, out = [], None
            for f in files[:30]:
                try:
                    out = start_read(conn, f.filename or "", f.read(MAX_BYTES + 1), _uid())
                except ValueError as e:
                    errors.append(f"{Path(f.filename or 'file').name}: {e}")
            if out is None:
                return jsonify({"error": " ".join(errors) or "Nothing was uploaded."}), 400
            return jsonify({"data": dict(out, errors=errors)}), 202
        finally:
            conn.close()

    @app.route("/api/proposal-template/relearn", methods=["POST"])
    @gate
    def proposal_template_relearn():
        conn = _conn()
        try:
            return jsonify({"data": relearn(conn, _uid())})
        finally:
            conn.close()

    @app.route("/api/proposal-template/preview", methods=["GET"])
    @gate
    def proposal_template_preview():
        conn = _conn()
        try:
            resp = app.response_class(sample_html(conn), mimetype="text/html")
            resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox"
            return resp
        finally:
            conn.close()

    @app.route("/api/proposal-template", methods=["DELETE"])
    @gate
    def proposal_template_delete():
        """Use the built-in layout (the saved templates stay)."""
        conn = _conn()
        try:
            return jsonify({"data": use_builtin(conn)})
        finally:
            conn.close()

    @app.route("/api/proposal-template/<int:tid>/use", methods=["POST"])
    @gate
    def proposal_template_use(tid):
        conn = _conn()
        try:
            return jsonify({"data": use(conn, tid)})
        except LookupError:
            return jsonify({"error": "not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            conn.close()

    @app.route("/api/proposal-template/<int:tid>", methods=["PATCH"])
    @gate
    def proposal_template_edit(tid):
        """Correct an archived proposal's number, date (MM/DD/YYYY), client, building or total."""
        conn = _conn()
        try:
            return jsonify({"data": edit_archive(conn, tid, request.get_json(silent=True) or {})})
        except LookupError:
            return jsonify({"error": "not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            conn.close()

    @app.route("/api/proposal-template/<int:tid>", methods=["DELETE"])
    @gate
    def proposal_template_remove(tid):
        conn = _conn()
        try:
            return jsonify({"data": remove(conn, tid)})
        except LookupError:
            return jsonify({"error": "not found"}), 404
        finally:
            conn.close()

    @app.route("/api/proposal-template/<int:tid>/file", methods=["GET"])
    @gate
    def proposal_template_file(tid):
        from flask import send_file
        conn = _conn()
        try:
            p, t = original(conn, tid)
        finally:
            conn.close()
        if not p:
            return jsonify({"error": "not found"}), 404
        resp = send_file(str(p), mimetype=EXT.get(t.get("ext"), "application/octet-stream"), as_attachment=False,
                         download_name=t.get("name") or ("template" + (t.get("ext") or "")))
        resp.headers["Cache-Control"] = "no-store"   # (no sandbox CSP here: it would stop Chrome's PDF viewer)
        return resp

    @app.route("/api/proposal-template/<int:tid>/preview", methods=["GET"])
    @gate
    def proposal_template_preview_one(tid):
        conn = _conn()
        try:
            try:
                html = sample_html(conn, tid)
            except LookupError:
                return jsonify({"error": "not ready"}), 404
            resp = app.response_class(html, mimetype="text/html")
            resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox"
            return resp
        finally:
            conn.close()
