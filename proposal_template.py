"""The company's own proposal template (accepted prototype 2026-10-19).

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
from datetime import datetime
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
          "intro", "reference", "extra_sections")
REQUIRED = ("scope", "line_items_rows", "total")

PROMPT = (
    "This is the proposal template a construction company sends to its clients. Recreate its layout as ONE "
    "self-contained HTML document that prints to the same look on US Letter paper: the letterhead, colors, "
    "font sizes and weights (web-safe font stacks only), section order and headings, table style, footer, and any "
    "standard wording they always use. Replace every part that changes per proposal with these placeholders, "
    "written exactly like this:\n"
    "{{company_name}} {{company_address}} {{company_phone}} {{company_email}} {{company_license}} {{logo}} (an <img> of "
    "their logo, sized by your CSS via the class `logo`), {{number}} {{date}} {{valid_until}} {{title}} "
    "{{client_name}} {{contact_name}} {{recipient_email}} {{building}} (address), {{reference}} (a short reference, may be "
    "empty), {{intro}} (the opening letter paragraph, before the scope), {{scope}} (the scope text), "
    "{{line_items_rows}} (only <tr> rows with 4 cells: description, quantity, unit price, amount — put it inside "
    "your own <table> with the header row in their style), {{total}}, {{deposit}} (a sentence about the deposit, may be "
    "empty), {{schedule}}, {{exclusions}}, {{terms}}, {{extra_sections}} (any added sections, each with its own heading; "
    "place it after the terms), {{signature}} (the acceptance / signature block).\n"
    "Keep wording that is the same on every proposal (for example standard terms or a closing paragraph) as plain "
    "text; when the template has standard exclusions or terms, keep them as text instead of the placeholder.\n"
    "Rules: inline <style> only; no <script>, no links to fonts or images, no external URLs; use page-break-inside: "
    "avoid on blocks that should stay together; @page margin 0.5in. Reply with the HTML document only, nothing else.")


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


def _store(conn) -> dict:
    """{templates: [...every upload...], active_id, next_id} (2026-10-20: every template is kept and listed).
    The one-template shape from before is read as a list of one."""
    v = _get_raw(conn)
    if "templates" in v:
        v.setdefault("next_id", max([t.get("id", 0) for t in v["templates"]] + [0]) + 1)
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


_PREVIEW = threading.local()   # "See a sample" for one saved template draws it without making it the one in use


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
                          "uploaded_at": t.get("uploaded_at"), "uploaded_by_name": who.get(t.get("uploaded_by")),
                          "in_use": bool(act and t["id"] == act["id"])} for t in reversed(st["templates"])]}
    return out


def use(conn, tid: int) -> dict:
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
    st = _store(conn)
    t = _item(st, tid)
    if not t:
        raise LookupError("template not found")
    st["templates"] = [x for x in st["templates"] if x.get("id") != tid]
    if st.get("active_id") == tid:
        st["active_id"] = None
    _put(conn, st)
    try:
        ssc_paths.resolve_data_path(t["path"]).unlink(missing_ok=True)
    except Exception as e:
        log.info("proposal template file not removed: %s", type(e).__name__)
    return public(conn)


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
    p = Path(SCRIPT_DIR) / "static" / "brand" / "logo-light.svg"
    try:
        data = base64.b64encode(p.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f'<img class="logo" alt="Superstars Contracting" src="data:image/svg+xml;base64,{data}">'


def fill(template: str, parts: dict) -> str:
    """parts: token -> HTML fragment (already escaped). Unknown tokens become empty."""
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: parts.get(m.group(1), ""), template)


def text(v) -> str:
    """Plain text -> safe HTML with line breaks kept."""
    return _html.escape(str(v or "")).replace("\n", "<br>")


# ------------------------------------------------------------------ reading the upload with Claude
def _read_with_claude(raw: bytes, ext: str, name: str) -> tuple:
    import assistant
    if ext == ".pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.b64encode(raw).decode("ascii")}, "title": name}
    elif ext in (".png", ".jpg", ".jpeg", ".heic", ".webp"):
        import claude_files
        img, mime = claude_files._image_for_model(raw)
        block = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(img).decode("ascii")}}
    else:
        import claude_files
        block = {"type": "text", "text": "<template_text>\n" + (claude_files._docx_text(raw) or "")[:60000] + "\n</template_text>\n"
                 "(This came from a Word file, so only its words reached you: keep a clean letter layout with a "
                 "red #B11E2E accent for Superstars Contracting.)"}
    res = assistant._call_model([{"type": "text", "text": "You turn a company's document into an HTML template."}],
                                [{"role": "user", "content": [block, {"type": "text", "text": PROMPT}]}])
    return res["text"], res


def _fake_template() -> str:
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'><style>@page{margin:.5in}body{font-family:Georgia,serif;color:#222}"
            ".hd{border-bottom:3px solid #B11E2E;display:flex;justify-content:space-between}.logo{height:40px}"
            "table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #ddd;padding:4px}</style></head><body>"
            "<div class='hd'>{{logo}}<div>{{company_name}}<br>{{company_address}}</div></div><h1>PROPOSAL {{number}}</h1>"
            "<p>{{date}} · {{client_name}} · {{building}}</p><h2>{{title}}</h2><h3>Scope</h3><p>{{scope}}</p>"
            "<table><tr><th>Item</th><th>Qty</th><th>Rate</th><th>Amount</th></tr>{{line_items_rows}}</table>"
            "<p><b>Total {{total}}</b> {{deposit}}</p><h3>Exclusions</h3><p>{{exclusions}}</p><h3>Terms</h3><p>{{terms}}</p>"
            "{{signature}}</body></html>")


def _record_usage(conn, uid, res) -> None:
    """Counts toward the daily Claude cap like every other call (a hidden chat, never in History)."""
    try:
        import claude_agent
        now = _now()
        conn.execute("INSERT INTO claude_chat (user_id, surface, title, source, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                     (uid, "office", "Proposal template", "system", now, now))
        conn.commit()
        cid = conn.execute("SELECT MAX(id) AS m FROM claude_chat WHERE user_id = ? AND source = 'system'", (uid,)).fetchone()["m"]
        claude_agent._add_msg(conn, cid, "assistant", "note", [{"type": "text", "text": "[Proposal template read]"}],
                              replay=False, usage=res.get("usage"), model_id=res.get("model"))
    except Exception as e:
        log.info("proposal template usage not recorded: %s", type(e).__name__)


def _read_job(tid: int, rel: str, ext: str, name: str, uid) -> None:
    conn = _conn()
    try:
        raw = ssc_paths.resolve_data_path(rel).read_bytes()
        import assistant
        if assistant._fake():
            out, res = _fake_template(), None
        else:
            out, res = _read_with_claude(raw, ext, name)
        if res:
            _record_usage(conn, uid, res)
        html = sanitize(out)
        problem = check(html)
        st = _store(conn)
        t = _item(st, tid)
        if not t:   # removed while reading
            return
        t.update({"state": "failed", "error": problem} if problem else {"state": "ready", "html": html, "error": None})
        if not problem:
            st["active_id"] = tid   # a new upload becomes the one in use once it's read
        _put(conn, st)
        log.info("proposal template %s read: %s", tid, "failed" if problem else "ready")
    except Exception as e:
        log.warning("proposal template read failed: %s", type(e).__name__)
        try:
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
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "Choose the template file to upload."}), 400
        raw = f.read(MAX_BYTES + 1)
        conn = _conn()
        try:
            return jsonify({"data": start_read(conn, f.filename or "", raw, _uid())}), 202
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
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
