"""Company templates — the company's blank forms, letters and plans, sorted by Claude (2026-10-26).

Angela's ask: a place on the company console to store templates of every kind. Claude reads each
upload and files it in a folder; when a job starts, the job's Documents tab offers the templates
that fit it (step 2), and Claude flags templates that look dated and proposes a redesign in one
house style (step 3). This module is step 1: the library.

  company_template   id, title, folder, type_key, offer_json, summary, fields_json,
                     design_verdict (ok | dated | NULL), design_notes (JSON list),
                     state (waiting | filed), read_status (ready | unread), read_error, model,
                     file_name, file_path, file_size, mime, content_sha256,
                     version, supersedes_id, superseded, created_by, created_at, updated_at,
                     filed_at, archived_at

* A drop saves the file at once and runs Claude's read in the same request (the Documents tab
  intake does the same); the row waits as `waiting` until someone approves the folder and name.
  No API key -> the file still waits, `unread`, and the person picks the folder.
* `offer_json` = which jobs the template is offered on: {job_types, access_methods,
  billing_types, regions}, each a list of the app's own codes; every list empty = every job.
* `fields_json` = the blanks Claude found, each tagged with the job fact that fills it (step 2
  fills them; nothing is filled here).
* Files live under <data root>/data_room/company_templates/<uuid>.<ext> and are read only through
  the gated route; `file_path` never enters a JSON payload (#229 pattern).

Access: the company portal gate (admin / c_suite), passed in by company_portal.register.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import jsonify, request, send_file

import billing_type
import db_layer
import doc_types
import estimates
import project_setup
import runtime_seams
import ssc_paths
from auth import current_user

log = logging.getLogger("ssc.company_templates")

# The six project folders first (a template in one of them files into the same folder on a job),
# then the company-only ones.
FOLDERS = (
    ("PERMITS", "Permits & Approvals"), ("DRAWINGS", "Drawings & Engineering"),
    ("CONTRACTS", "Contracts & Financial"), ("INSPECTIONS", "Inspections & Reports"),
    ("SAFETY", "Safety & Compliance"), ("CLOSEOUT", "Closeout"),
    ("LETTERS", "Letters & notices"), ("FIELD", "Field forms"), ("OTHER", "Other"),
)
FOLDER_NAMES = dict(FOLDERS)
PROJECT_FOLDERS = ("PERMITS", "DRAWINGS", "CONTRACTS", "INSPECTIONS", "SAFETY", "CLOSEOUT")

# The job facts a blank can be filled from (step 2). "other" = nothing the job knows.
FIELD_SOURCES = (
    ("job_address", "Job address"), ("job_number", "Job number"), ("job_name", "Job name"),
    ("owner", "Owner / client"), ("client_contact", "Client contact"), ("start_date", "Start date"),
    ("target_completion", "Target completion"), ("contract_value", "Contract value"),
    ("superintendent", "Superintendent"), ("project_manager", "Project manager"),
    ("company", "Our company details"), ("today", "Today's date"), ("other", "Not on file"),
)
FIELD_SOURCE_KEYS = tuple(k for k, _ in FIELD_SOURCES)
OFFER_KEYS = ("job_types", "access_methods", "billing_types", "regions")
DESIGN_VERDICTS = ("ok", "dated")
STATES = ("waiting", "filed")

TITLE_MAX = 160
SUMMARY_MAX = 600
FIELDS_MAX = 60
FIELD_LABEL_MAX = 80
NOTES_MAX = 4
NOTE_MAX = 200

_FILE_BASE = ssc_paths.under_root("data_room", "company_templates")
_EXT_MIME = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".heic": "image/heic", ".heif": "image/heif",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel", ".csv": "text/csv",
}
MAX_FILE_BYTES = 25 * 1024 * 1024
READ_MAX_BYTES = 16 * 1024 * 1024   # what Claude is sent (same cap as the Documents tab)


# ---------------------------------------------------------------- helpers
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _one(cur) -> Optional[dict]:
    r = cur.fetchone()
    return dict(r) if r else None


def _err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def _wrap(data, **meta):
    m = {"generated_at": _now()}
    if isinstance(data, list):
        m["count"] = len(data)
    m.update(meta)
    resp = jsonify({"data": data, "meta": m})
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _uid():
    u = current_user()
    return u["id"] if u else None


def _loads(s, default):
    try:
        v = json.loads(s) if s else default
    except (TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def offer_options() -> dict:
    """The codes a template can be offered on, with labels (for the picker and the prompt)."""
    return {
        "job_types": [{"code": k, "label": v} for k, v in estimates.EST_TYPES.items()],
        "access_methods": [{"code": k, "label": v} for k, v in project_setup.ACCESS_METHODS],
        "billing_types": [{"code": o["code"], "label": o["label"]} for o in billing_type.options()],
        "regions": [{"code": k, "label": v} for k, v in estimates.BOROUGH_LABELS.items()],
    }


def clean_offer(raw) -> dict:
    """Keep only known codes, in a stable order. Anything else -> empty lists (= every job)."""
    raw = raw if isinstance(raw, dict) else {}
    opts = offer_options()
    out = {}
    for key in OFFER_KEYS:
        valid = [o["code"] for o in opts[key]]
        got = raw.get(key) if isinstance(raw.get(key), list) else []
        got = {str(x).strip() for x in got}
        out[key] = [c for c in valid if c in got]
    return out


def offer_label(offer: dict) -> str:
    """'Every job' or e.g. 'Rope access · AIA / Schedule of values'."""
    opts = offer_options()
    parts = []
    for key in OFFER_KEYS:
        labels = {o["code"]: o["label"] for o in opts[key]}
        parts += [labels[c] for c in offer.get(key) or [] if c in labels]
    return " · ".join(parts) if parts else "Every job"


def _clean_folder(folder, type_key=None) -> str:
    f = str(folder or "").strip().upper()
    if f in FOLDER_NAMES:
        return f
    t = doc_types.get(type_key)
    return t["category"] if t else "OTHER"


def _clean_type(type_key, folder) -> Optional[str]:
    """A document type only when it belongs to the template's folder (it decides the job slot)."""
    t = doc_types.get(type_key)
    return t["key"] if t and t["category"] == folder else None


# ---------------------------------------------------------------- schema
def ensure_schema(conn) -> None:
    """Idempotent on both backends. Called from company_portal.ensure_schema (boot) and from
    apply_company_templates.py (ledger / fixture)."""
    pk = ("id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" if db_layer.is_postgres()
          else "id INTEGER PRIMARY KEY AUTOINCREMENT")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS company_template ("
        f"  {pk},"
        f"  title           TEXT NOT NULL,"
        f"  folder          TEXT NOT NULL DEFAULT 'OTHER',"
        f"  type_key        TEXT,"
        f"  offer_json      TEXT,"
        f"  summary         TEXT,"
        f"  fields_json     TEXT,"
        f"  design_verdict  TEXT,"
        f"  design_notes    TEXT,"
        f"  state           TEXT NOT NULL DEFAULT 'waiting',"
        f"  read_status     TEXT NOT NULL DEFAULT 'unread',"
        f"  read_error      TEXT,"
        f"  model           TEXT,"
        f"  file_name       TEXT NOT NULL,"
        f"  file_path       TEXT NOT NULL,"
        f"  file_size       INTEGER,"
        f"  mime            TEXT,"
        f"  content_sha256  TEXT,"
        f"  version         INTEGER NOT NULL DEFAULT 1,"
        f"  supersedes_id   INTEGER,"
        f"  superseded      INTEGER NOT NULL DEFAULT 0,"
        f"  created_by      INTEGER,"
        f"  created_at      TEXT NOT NULL,"
        f"  updated_at      TEXT NOT NULL,"
        f"  filed_at        TEXT,"
        f"  archived_at     TEXT"
        f")")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_template_live "
                 "ON company_template(archived_at, superseded, state, folder)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_template_sha ON company_template(content_sha256)")
    conn.commit()


# ---------------------------------------------------------------- Claude's read
READ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "folder": {"type": "string"},
        "type_key": {"type": "string"},
        "summary": {"type": "string"},
        "fields": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"label": {"type": "string"}, "source": {"type": "string"}},
            "required": ["label", "source"]}},
        "offer": {"type": "object", "additionalProperties": False,
                  "properties": {k: {"type": "array", "items": {"type": "string"}} for k in OFFER_KEYS},
                  "required": list(OFFER_KEYS)},
        "design": {"type": "object", "additionalProperties": False,
                   "properties": {"verdict": {"type": "string", "enum": list(DESIGN_VERDICTS)},
                                  "reasons": {"type": "array", "items": {"type": "string"}}},
                   "required": ["verdict", "reasons"]},
    },
    "required": ["title", "folder", "type_key", "summary", "fields", "offer", "design"],
}


def build_prompt() -> str:
    folders = "\n".join(f"  {k}: {v}" for k, v in FOLDERS)
    types = "\n".join(f"  {t['key']}: {t['label']} (folder {t['category']})" for t in doc_types.public_list())
    sources = "\n".join(f"  {k}: {v}" for k, v in FIELD_SOURCES)
    opts = offer_options()
    offer = "\n".join(f"  {key}: " + ", ".join(f"{o['code']} = {o['label']}" for o in opts[key]) for key in OFFER_KEYS)
    return f"""This file is a TEMPLATE from Superstars Contracting, a New York construction company
(exterior restoration, rope access, parking garages, interiors). It is a blank form, letter, plan
or checklist the company reuses on its jobs. Read it and describe it.

title: a short, plain name for the template (e.g. "Site Safety Plan, rope access"). No file extension.

folder: one of
{folders}

type_key: the project document type it produces when filled in for a job, or "none". Only a type
whose folder matches yours:
{types}

summary: one or two sentences: what it is and when the company uses it.

fields: every blank to fill in (a line, an empty box, a placeholder like [CLIENT]). label = the
printed label; source = the job fact that fills it, one of
{sources}

offer: which jobs it belongs on. Use the codes below; leave a list empty when the template does not
depend on it. All lists empty = every job.
{offer}

design: verdict "dated" when it looks plain or old (typewriter fonts, underlined headings, blank
underscores instead of labelled boxes, no logo or letterhead, cramped or uneven layout, scanned
photocopy), otherwise "ok". reasons: up to four short phrases about what looks dated (empty when ok).

Return ONLY the structured JSON."""


def _spec(data: bytes, filename: str) -> dict:
    """One content block for the file: PDF / picture as itself, Word and spreadsheets as text."""
    ext = Path(filename or "").suffix.lower()
    if ext == ".docx":
        import claude_files
        return {"type": "document", "title": Path(filename).name,
                "source": {"type": "text", "media_type": "text/plain",
                           "data": claude_files._docx_text(data) or "(the document is empty)"}}
    import sheet_read
    if ext in sheet_read.EXTS:
        return {"type": "document", "title": Path(filename).name,
                "source": {"type": "text", "media_type": "text/plain",
                           "data": sheet_read.text(data, ext) or "(the workbook is empty)"}}
    b64 = base64.standard_b64encode(data).decode("ascii")
    if ext == ".pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    media = {".png": "image/png"}.get(ext, "image/jpeg")   # HEIC goes best-effort, as on the Documents tab
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}}


def call_model(data: bytes, filename: str) -> dict:
    """ONE API call. Raises RuntimeError('unavailable') without a key, RuntimeError otherwise."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("unavailable")
    import anthropic

    import ai_models
    import document_scanner
    model = os.environ.get("TEMPLATE_READ_MODEL") or document_scanner.MODEL
    content = [_spec(data, filename), {"type": "text", "text": build_prompt()}]
    try:
        resp = anthropic.Anthropic().messages.create(
            model=model, max_tokens=8000, **ai_models.thinking_off(model),
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": READ_SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:  # anthropic.APIError + transport errors; never log the file
        raise RuntimeError(f"template read failed: {type(e).__name__}")
    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError("template read refused")
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    try:
        raw = json.loads(text)
    except ValueError:
        raise RuntimeError("template read was not JSON")
    raw["_model"] = model
    return raw


def process_read(raw) -> dict:
    """Pure: coerce the model's answer into the app's vocabularies (unit-testable without the API)."""
    raw = raw if isinstance(raw, dict) else {}
    folder = _clean_folder(raw.get("folder"), raw.get("type_key"))
    fields, seen = [], set()
    for f in raw.get("fields") or []:
        if not isinstance(f, dict):
            continue
        label = " ".join(str(f.get("label") or "").split())[:FIELD_LABEL_MAX]
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        src = str(f.get("source") or "").strip()
        fields.append({"label": label, "source": src if src in FIELD_SOURCE_KEYS else "other"})
        if len(fields) >= FIELDS_MAX:
            break
    design = raw.get("design") if isinstance(raw.get("design"), dict) else {}
    verdict = design.get("verdict") if design.get("verdict") in DESIGN_VERDICTS else None
    notes = [" ".join(str(n).split())[:NOTE_MAX] for n in (design.get("reasons") or []) if str(n).strip()]
    return {
        "title": " ".join(str(raw.get("title") or "").split())[:TITLE_MAX],
        "folder": folder,
        "type_key": _clean_type(raw.get("type_key"), folder),
        "summary": " ".join(str(raw.get("summary") or "").split())[:SUMMARY_MAX],
        "fields": fields,
        "offer": clean_offer(raw.get("offer")),
        "design_verdict": verdict,
        "design_notes": notes[:NOTES_MAX] if verdict == "dated" else [],
    }


def read_template(data: bytes, filename: str) -> tuple:
    """(read_status, suggestion | None, error | None). Planted JSON under the TEMPLATE_READ_FAKE seam."""
    fake = runtime_seams.seam("TEMPLATE_READ_FAKE")
    if not os.environ.get("ANTHROPIC_API_KEY") and not fake:
        return "unread", None, "AI reading is off. Pick the folder yourself."
    if len(data) > READ_MAX_BYTES:
        return "unread", None, "Too large for Claude to read (max 16 MB). Pick the folder yourself."
    try:
        if fake:
            with open(fake, encoding="utf-8") as fh:
                raw = json.load(fh)
        else:
            raw = call_model(data, filename)
    except Exception as e:
        log.error("template read failed: %s", type(e).__name__)
        return "unread", None, "Claude couldn't read this one. Pick the folder yourself."
    sug = process_read(raw)
    sug["model"] = raw.get("_model") or ("fake" if fake else None)
    return "ready", sug, None


# ---------------------------------------------------------------- rows
_PUBLIC_COLS = ("id, title, folder, type_key, offer_json, summary, fields_json, design_verdict, design_notes, "
                "state, read_status, read_error, model, file_name, file_size, mime, version, created_at, "
                "updated_at, filed_at")


def public(r: dict) -> dict:
    offer = clean_offer(_loads(r.get("offer_json"), {}))
    t = doc_types.get(r.get("type_key"))
    return {
        "id": r["id"], "title": r["title"], "folder": r["folder"], "folder_name": FOLDER_NAMES.get(r["folder"], r["folder"]),
        "type_key": r.get("type_key"), "type_label": t["label"] if t else None,
        "requirement_key": t["requirement_key"] if t else None,
        "offer": offer, "offer_label": offer_label(offer),
        "summary": r.get("summary") or "", "fields": _loads(r.get("fields_json"), []),
        "design_verdict": r.get("design_verdict"), "design_notes": _loads(r.get("design_notes"), []),
        "state": r["state"], "read_status": r["read_status"], "read_error": r.get("read_error"),
        "file_name": r["file_name"], "file_size": r.get("file_size"), "mime": r.get("mime"),
        "version": r.get("version") or 1, "created_at": r.get("created_at"), "updated_at": r.get("updated_at"),
        "filed_at": r.get("filed_at"), "file_url": f"/api/company/portal/templates/{r['id']}/file",
    }


def get(conn, template_id: int) -> Optional[dict]:
    r = _one(conn.execute(f"SELECT {_PUBLIC_COLS} FROM company_template WHERE id = ? AND archived_at IS NULL",
                          (template_id,)))
    return public(r) if r else None


def list_all(conn) -> list:
    rows = conn.execute(f"SELECT {_PUBLIC_COLS} FROM company_template WHERE archived_at IS NULL AND superseded = 0 "
                        f"ORDER BY state DESC, folder, LOWER(title), id").fetchall()
    return [public(dict(r)) for r in rows]


def library(conn) -> list:
    """Filed templates only (what step 2 offers to jobs)."""
    return [t for t in list_all(conn) if t["state"] == "filed"]


def _save_file(fs) -> tuple:
    ext = Path(fs.filename or "file").suffix.lower()
    if ext not in _EXT_MIME:
        raise ValueError(f"unsupported file type: {ext or '(none)'}. Use PDF, Word, Excel, CSV or a photo")
    base = _FILE_BASE.resolve()
    _FILE_BASE.mkdir(parents=True, exist_ok=True)
    fpath = _FILE_BASE / (uuid.uuid4().hex + ext)
    if not fpath.resolve().is_relative_to(base):
        raise ValueError("invalid path")
    fs.save(str(fpath))
    size = fpath.stat().st_size
    if size > MAX_FILE_BYTES:
        fpath.unlink(missing_ok=True)
        raise ValueError("file is larger than 25 MB")
    if size == 0:
        fpath.unlink(missing_ok=True)
        raise ValueError("file is empty")
    return fpath, ext, size


def _unlink(stored) -> None:
    try:
        p = ssc_paths.resolve_data_path(stored)
        if p.resolve().is_relative_to(_FILE_BASE.resolve()):
            p.unlink(missing_ok=True)
    except Exception as e:
        log.warning("template file not removed from disk: %s", type(e).__name__)


def add(conn_factory, fs, *, uploaded_by=None) -> tuple:
    """Save one upload, have Claude read it, return (template, duplicate_of | None).
    The DB connection is not held across the model call."""
    fpath, ext, size = _save_file(fs)
    data = fpath.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    file_name = Path(fs.filename or "file").name[:200]
    conn = conn_factory()
    try:
        dup = _one(conn.execute("SELECT id FROM company_template WHERE content_sha256 = ? AND archived_at IS NULL "
                                "AND superseded = 0 ORDER BY id LIMIT 1", (sha,)))
        if dup:
            fpath.unlink(missing_ok=True)
            return get(conn, dup["id"]), dup["id"]
        now = _now()
        try:
            cur = conn.execute(
                "INSERT INTO company_template (title, folder, offer_json, fields_json, design_notes, state, read_status, "
                "file_name, file_path, file_size, mime, content_sha256, created_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (Path(file_name).stem[:TITLE_MAX] or "Untitled template", "OTHER", json.dumps(clean_offer({})), "[]", "[]",
                 "waiting", "unread", file_name, ssc_paths.store_rel(fpath), size, _EXT_MIME[ext], sha, uploaded_by, now, now))
            new_id = cur.lastrowid
            conn.commit()
        except Exception:
            fpath.unlink(missing_ok=True)
            raise
    finally:
        conn.close()
    status, sug, read_err = read_template(data, file_name)
    conn = conn_factory()
    try:
        if sug:
            conn.execute(
                "UPDATE company_template SET title = COALESCE(NULLIF(?, ''), title), folder = ?, type_key = ?, offer_json = ?, "
                "summary = ?, fields_json = ?, design_verdict = ?, design_notes = ?, read_status = ?, read_error = NULL, "
                "model = ?, updated_at = ? WHERE id = ?",
                (sug["title"], sug["folder"], sug["type_key"], json.dumps(sug["offer"]), sug["summary"],
                 json.dumps(sug["fields"], ensure_ascii=False), sug["design_verdict"],
                 json.dumps(sug["design_notes"], ensure_ascii=False), status, sug.get("model"), _now(), new_id))
        else:
            conn.execute("UPDATE company_template SET read_status = ?, read_error = ?, updated_at = ? WHERE id = ?",
                         (status, read_err, _now(), new_id))
        conn.commit()
        return get(conn, new_id), None
    finally:
        conn.close()


def _apply_edits(row: dict, payload: dict) -> tuple:
    """(sets dict, error). Validates title / folder / type_key / offer from a payload."""
    sets = {}
    if "title" in payload:
        title = " ".join(str(payload.get("title") or "").split())[:TITLE_MAX]
        if not title:
            return None, "give the template a name"
        sets["title"] = title
    folder = row["folder"]
    if "folder" in payload:
        folder = str(payload.get("folder") or "").strip().upper()
        if folder not in FOLDER_NAMES:
            return None, "pick a folder"
        sets["folder"] = folder
    if "type_key" in payload or "folder" in payload:
        tk = payload.get("type_key", row.get("type_key"))
        sets["type_key"] = _clean_type(tk, folder)
        if tk and tk != "none" and "type_key" in payload and not sets["type_key"]:
            return None, "that document type belongs to a different folder"
    if "offer" in payload:
        sets["offer_json"] = json.dumps(clean_offer(payload.get("offer")))
    return sets, None


def update(conn, template_id: int, payload: dict, *, approve: bool = False) -> tuple:
    """(template | None, error | None). approve=True files a waiting template."""
    row = _one(conn.execute("SELECT * FROM company_template WHERE id = ? AND archived_at IS NULL", (template_id,)))
    if not row:
        return None, None
    if approve and row["state"] != "waiting":
        return None, "this template is already filed"
    sets, err = _apply_edits(row, payload or {})
    if err:
        return None, err
    if approve:
        sets.update(state="filed", filed_at=_now())
    if not sets:
        return get(conn, template_id), None
    sets["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(f"UPDATE company_template SET {cols} WHERE id = ?", (*sets.values(), template_id))
    conn.commit()
    return get(conn, template_id), None


def remove(conn, template_id: int) -> bool:
    """A waiting upload is deleted with its file; a filed template is archived (kept for History)."""
    row = _one(conn.execute("SELECT id, state, file_path FROM company_template WHERE id = ? AND archived_at IS NULL",
                            (template_id,)))
    if not row:
        return False
    if row["state"] == "waiting":
        conn.execute("DELETE FROM company_template WHERE id = ?", (template_id,))
        conn.commit()
        _unlink(row["file_path"])
    else:
        conn.execute("UPDATE company_template SET archived_at = ?, updated_at = ? WHERE id = ?", (_now(), _now(), template_id))
        conn.commit()
    return True


def resolve_file(conn, template_id: int) -> Optional[tuple]:
    """(path, mime, file_name) for a gated read, or None. Path-contained under the templates base."""
    r = _one(conn.execute("SELECT file_path, mime, file_name FROM company_template WHERE id = ?", (template_id,)))
    if not r or not r["file_path"]:
        return None
    p = ssc_paths.resolve_data_path(r["file_path"])
    if not (p.resolve().is_relative_to(_FILE_BASE.resolve()) and p.exists()):
        return None
    return p, r["mime"], r["file_name"]


def meta() -> dict:
    return {"folders": [{"code": k, "name": v, "project": k in PROJECT_FOLDERS} for k, v in FOLDERS],
            "types": doc_types.public_list(), "offer_options": offer_options(),
            "field_sources": [{"code": k, "label": v} for k, v in FIELD_SOURCES]}


# ---------------------------------------------------------------- routes
def register_routes(app, gate) -> None:
    def _with_conn(fn):
        conn = db_layer.connect()
        try:
            return fn(conn)
        finally:
            conn.close()

    @app.route("/api/company/portal/templates", methods=["GET"])
    @gate
    def templates_list():
        return _wrap(_with_conn(list_all), **meta())

    @app.route("/api/company/portal/templates", methods=["POST"])
    @gate
    def templates_upload():
        """Drop ONE file: save it now, have Claude read it, return the review card. The file is sent
        to the Anthropic API (same trust as the Documents tab). Nothing is filed until /approve."""
        fs = request.files.get("file")
        if not fs or not fs.filename:
            return _err("no file")
        try:
            tpl, dup = add(db_layer.connect, fs, uploaded_by=_uid())
        except ValueError as e:
            return _err(str(e))
        if dup:
            resp = _wrap(tpl, duplicate_of=dup)
            return resp, 200
        return _wrap(tpl), 201

    @app.route("/api/company/portal/templates/<int:template_id>/approve", methods=["POST"])
    @gate
    def templates_approve(template_id):
        tpl, err = _with_conn(lambda c: update(c, template_id, request.get_json(silent=True) or {}, approve=True))
        if err:
            return _err(err)
        if not tpl:
            return _err("template not found", 404)
        return _wrap(tpl)

    @app.route("/api/company/portal/templates/<int:template_id>", methods=["PATCH"])
    @gate
    def templates_update(template_id):
        tpl, err = _with_conn(lambda c: update(c, template_id, request.get_json(silent=True) or {}))
        if err:
            return _err(err)
        if not tpl:
            return _err("template not found", 404)
        return _wrap(tpl)

    @app.route("/api/company/portal/templates/<int:template_id>", methods=["DELETE"])
    @gate
    def templates_delete(template_id):
        if not _with_conn(lambda c: remove(c, template_id)):
            return _err("template not found", 404)
        return _wrap({"id": template_id, "deleted": True})

    @app.route("/api/company/portal/templates/<int:template_id>/file", methods=["GET"])
    @gate
    def templates_file(template_id):
        """The ONLY way a template file is read: inline, no-store, path-contained."""
        got = _with_conn(lambda c: resolve_file(c, template_id))
        if not got:
            return _err("file not found", 404)
        p, mime, name = got
        resp = send_file(str(p), mimetype=mime or "application/octet-stream", as_attachment=False,
                         download_name=name or f"template-{template_id}")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp
