"""Company brand: the logo and letterhead every document uses (accepted prototype 2026-10-20).

An admin uploads the company logo once in Settings > Company brand. Proposals (the built-in layout and the
company's own template, including every proposal Claude drafts), invoices and AIA pay applications print it in
their header; Claude is also shown it when it reads a proposal template, so the layout matches. With no upload,
documents keep the standard mark (static/brand/logo-light.svg) as before.

  GET    /api/company-brand              -> {logo: {name, mime, size, uploaded_at, uploaded_by_name, data_uri} | null,
                                             letterhead: {company_address, company_phone, company_email, company_license}}
  POST   /api/company-brand/logo         multipart `file` (png / jpg / svg / webp, 2 MB)
  DELETE /api/company-brand/logo         -> back to the standard mark
  PUT    /api/company-brand/letterhead   JSON with any of the letterhead keys (blank hides the line)

Admin only (the role, not C-suite). Stored in app_settings: 'company_logo' (JSON with the file as base64, so it
lives in the database with everything else) and the four letterhead keys proposals / invoices already read.
Documents embed the logo as a data: URI, so the PDF engine never fetches anything. SVGs with scripts, event
handlers or outside links are refused.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import jsonify, request

import db_layer
from auth import SCRIPT_DIR, current_user, requires_role

log = logging.getLogger(__name__)

KEY = "company_logo"
MAX_BYTES = 2 * 1024 * 1024
LETTERHEAD = ("company_address", "company_phone", "company_email", "company_license")
LETTERHEAD_MAX = 200
COMPANY_NAME = "Superstars Contracting"
STANDARD_LOGO = Path(SCRIPT_DIR) / "static" / "brand" / "logo-light.svg"
EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".webp": "image/webp"}
WRONG_TYPE = "{name} isn't a PNG, JPG, SVG or WebP image. Export the logo as one of those and try again."
_SVG_BAD = re.compile(r"<\s*script|<\s*foreignObject|\bon[a-z]+\s*=|javascript:|(?:href|src)\s*=\s*[\"']?\s*(?:https?:|//)"
                      r"|@import|url\(\s*[\"']?\s*(?:https?:|//)", re.I)


# ------------------------------------------------------------------ storage
def _conn():
    return db_layer.connect()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")


def _get(conn, key: str) -> Optional[str]:
    try:
        r = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    except Exception:
        conn.rollback()
        return None
    return r["value"] if r else None


def _put(conn, key: str, value: Optional[str]) -> None:
    _ensure(conn)
    conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    if value is not None:
        conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)", (key, value, _now()))


def logo(conn) -> Optional[dict]:
    """The uploaded logo ({name, mime, size, data (base64), uploaded_at, uploaded_by}) or None."""
    raw = _get(conn, KEY)
    if not raw:
        return None
    try:
        v = json.loads(raw)
    except ValueError:
        return None
    return v if isinstance(v, dict) and v.get("data") and v.get("mime") in EXT.values() else None


def logo_data_uri(conn=None) -> Optional[str]:
    """data: URI of the uploaded logo, or None when there is none. Never raises (documents still render)."""
    own = conn is None
    try:
        conn = conn or _conn()
        v = logo(conn)
        return f"data:{v['mime']};base64,{v['data']}" if v else None
    except Exception as x:
        log.info("company logo unavailable: %s", type(x).__name__)
        return None
    finally:
        if own and conn is not None:
            conn.close()


def document_logo_uri(conn=None) -> Optional[str]:
    """The logo a document prints: the uploaded one, else the standard mark file."""
    up = logo_data_uri(conn)
    if up:
        return up
    try:
        return "data:image/svg+xml;base64," + base64.b64encode(STANDARD_LOGO.read_bytes()).decode("ascii")
    except OSError:
        return None


def brand_block(conn, tagline: str) -> str:
    """The `.brand` header cell of the built-in proposal / invoice / AIA sheets: the uploaded logo when there is
    one, otherwise the company name as text (as before)."""
    import html as _html
    up = logo_data_uri(conn)
    if not up:
        return f'<div class="brand">{COMPANY_NAME}<small>{_html.escape(tagline)}</small></div>'
    return (f'<div class="brand"><img class="brand-logo" alt="{COMPANY_NAME}" src="{up}">'
            f'<small>{_html.escape(tagline)}</small></div>')


BRAND_CSS = ".brand-logo{display:block;max-height:52px;max-width:240px;object-fit:contain;margin-bottom:4px}"


def model_image_block(conn) -> Optional[dict]:
    """The uploaded logo as an image block Claude can look at (raster only; the API does not take SVG)."""
    v = logo(conn)
    if not v or v["mime"] == "image/svg+xml":
        return None
    try:
        import claude_files
        img, mime = claude_files._image_for_model(base64.b64decode(v["data"]))
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(img).decode("ascii")}}
    except Exception as x:
        log.info("company logo not shown to Claude: %s", type(x).__name__)
        return None


def letterhead(conn) -> dict:
    return {k: (_get(conn, k) or "").strip() for k in LETTERHEAD}


# ------------------------------------------------------------------ checks
def _sniff(raw: bytes) -> Optional[str]:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    head = raw[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") or head.startswith(b"<!--")) and b"<svg" in raw[:4096].lower():
        return "image/svg+xml"
    return None


def check(name: str, raw: bytes) -> str:
    """The file's media type, or ValueError with a message for the person uploading."""
    name = (name or "logo").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if not raw:
        raise ValueError("That file is empty. Choose the logo image again.")
    if len(raw) > MAX_BYTES:
        raise ValueError(f"{name} is {len(raw) / 1048576:.1f} MB. The limit is 2 MB; a smaller export of the same logo works.")
    ext = Path(name).suffix.lower()
    mime = _sniff(raw)
    if ext not in EXT or mime is None or EXT[ext] != mime and not (ext in (".jpg", ".jpeg") and mime == "image/jpeg"):
        raise ValueError(WRONG_TYPE.format(name=name))
    if mime == "image/svg+xml":
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(WRONG_TYPE.format(name=name))
        if _SVG_BAD.search(text):
            raise ValueError(f"{name} has scripts or outside links inside it. Export a plain SVG, or a PNG, and try again.")
    return mime


def save_logo(conn, name: str, raw: bytes, uid) -> dict:
    mime = check(name, raw)
    clean = (name or "logo").rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:120]
    _put(conn, KEY, json.dumps({"name": clean, "mime": mime, "size": len(raw), "data": base64.b64encode(raw).decode("ascii"),
                                "uploaded_at": _now(), "uploaded_by": uid}))
    conn.commit()
    return public(conn)


def remove_logo(conn) -> dict:
    _put(conn, KEY, None)
    conn.commit()
    return public(conn)


def save_letterhead(conn, body: dict) -> dict:
    for k in LETTERHEAD:
        if k in body:
            v = str(body.get(k) or "").strip()
            if len(v) > LETTERHEAD_MAX:
                raise ValueError(f"Keep each letterhead line under {LETTERHEAD_MAX} characters.")
            _put(conn, k, v or None)
    conn.commit()
    return public(conn)


def _user_name(conn, uid) -> str:
    if not uid:
        return ""
    try:
        r = conn.execute("SELECT display_name, email FROM users WHERE id = ?", (uid,)).fetchone()
    except Exception:
        conn.rollback()
        return ""
    return (r["display_name"] or (r["email"] or "").split("@")[0]) if r else ""


def public(conn) -> dict:
    v = logo(conn)
    out = None
    if v:
        out = {"name": v.get("name"), "mime": v["mime"], "size": v.get("size"), "uploaded_at": v.get("uploaded_at"),
               "uploaded_by_name": _user_name(conn, v.get("uploaded_by")), "data_uri": f"data:{v['mime']};base64,{v['data']}"}
    return {"logo": out, "letterhead": letterhead(conn)}


# ------------------------------------------------------------------ routes
def _uid():
    return (current_user() or {}).get("id")


def register(app) -> None:
    admin = requires_role("admin")

    @app.route("/api/company-brand", methods=["GET"])
    @admin
    def company_brand_get():
        conn = _conn()
        try:
            return jsonify({"data": public(conn)})
        finally:
            conn.close()

    @app.route("/api/company-brand/logo", methods=["POST"])
    @admin
    def company_brand_logo_upload():
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "Choose the logo image to upload."}), 400
        raw = f.read(MAX_BYTES + 1)
        conn = _conn()
        try:
            data = save_logo(conn, f.filename or "", raw, _uid())
            log.info("company logo uploaded mime=%s bytes=%d", data["logo"]["mime"], len(raw))
            return jsonify({"data": data})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            conn.close()

    @app.route("/api/company-brand/logo", methods=["DELETE"])
    @admin
    def company_brand_logo_remove():
        conn = _conn()
        try:
            log.info("company logo removed")
            return jsonify({"data": remove_logo(conn)})
        finally:
            conn.close()

    @app.route("/api/company-brand/letterhead", methods=["PUT"])
    @admin
    def company_brand_letterhead():
        body = request.get_json(silent=True) or {}
        conn = _conn()
        try:
            return jsonify({"data": save_letterhead(conn, body)})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            conn.close()
