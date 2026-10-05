"""#293 — Authorable elevations: drawing set upload, sheet picker, AI-proposed
trace, manual authoring, and the draft/confirm state machine.

THE FLOW (operator intent): upload the engineer's full drawing set -> page
thumbnails render -> the text layer pre-fills each sheet's label/number and
flags the elevation pages (NO OCR — free accuracy on vector sets, graceful
blank pre-fill on scans) -> operator picks a sheet, indicates WHICH drawing on
the page is the subject (every elevation sheet carries an EXISTING and a
PROPOSED drawing — never assume one per page) -> the AI proposes the facade
grid (Tier B: machine proposes, HUMAN CONFIRMS — an elevation stays `draft`
until an internal user confirms it) -> manual authoring adjusts or replaces the
proposal entirely (the fallback that never fails) -> confirm generates one
elevation_cell per bay per floor and the elevation renders on /drawing-markup
exactly like the hand-traced 890 North.

SECURITY MODEL:
  * AUTHORING IS AN SSC ACT. Every endpoint in this module requires an
    INTERNAL role (elevation.PAINT_ROLES) *inside the handler* — never rely on
    path gates. The client gate opens /api/elevation* to drawing-granted
    clients and the architect allowlist opens the same prefixes, so this
    module's routes live on prefixes neither gate opens (/drawing-author,
    /api/drawing-sets, /api/author/*) AND carry their own role check. Defense
    in both layers.
  * PROJECT SCOPING reuses elevation._require_project (the #263 axis): a pm
    assigned to project A cannot touch project B's sets, sheets or drafts.
  * DRAFTS ARE INTERNAL-ONLY. elevation.py refuses by-id reads of a draft to
    external audiences and omits drafts from their picker; this module never
    serves anything to an external role at all.

STORAGE (#287): PDFs and page renders live under data_room/drawing_sets/
<project>/<set_id>/ — data_room is an existing ssc_paths anchor, so no
resolver change. DB rows store RELATIVE paths (store_rel). Serves resolve via
resolve_data_path + a containment check under the drawing_sets base, and are
IMMUTABLE-cached (#291 pattern): a set id's bytes never change — replacing a
drawing set is a NEW set id, never a rewrite.

AI (Tier B): the chosen page region goes to the Claude vision API and comes
back as a facade grid proposal (bays, floors, relative proportions,
irregularities, and per-drawing regions when the crop holds more than one
drawing). Key from env only; missing key -> clean 503 {ai_available: false}
(the cert-card pattern) and the manual path is fully unaffected.
SSC_TRACE_FAKE=<path-to-json> is the deterministic test seam (the *_SCAN_FAKE
convention): the planted JSON is processed through the SAME validation
pipeline instead of calling the API.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os

import runtime_seams
import re
import shutil
from datetime import datetime
from pathlib import Path

from flask import jsonify, request, send_file

import db_layer
import elevation
import ssc_paths
from auth import current_user

SCRIPT_DIR = Path(__file__).resolve().parent
AUTHOR_PAGE = SCRIPT_DIR / "templates" / "v2" / "drawing-author.html"

# ---- limits ----------------------------------------------------------------
MAX_PDF_BYTES = 80 * 1024 * 1024      # engineer sets run large; 80 MB is generous
MAX_PAGES = 40
THUMB_LONG_EDGE = 320.0               # px — the sheet picker grid
FULL_LONG_EDGE = 2000.0               # px — the authoring canvas underlay
AI_LONG_EDGE = 2000.0                 # px — the vision call (high-res tier)
MAX_BAYS = 80
MAX_FLOORS = 120                      # tall buildings are an explicit requirement
MIN_FRACTION = 0.005

# Nominal drawing scale for authored grids. PROPORTIONS ONLY — these feet are a
# canvas unit so the geometry renders through the same feet->px transform as the
# traced 890 North; they are never dimensions. dimension_basis says so.
NOMINAL_BAY_FT = 20.0
NOMINAL_FLOOR_FT = 11.0
PARAPET_FT = 2.5

AUTHORED_SCALE_NOTE = (
    "Authored grid — bay and floor proportions only, not dimensioned. "
    "Confirm against the architect's current set before using for takeoff."
)

# Vision model: house env-override pattern; strongest default for a counting task.
TRACE_MODEL = os.environ.get("SSC_TRACE_MODEL", "claude-opus-5")


def _db():
    return db_layer.connect()


def _now():
    return datetime.now().isoformat(timespec="seconds")   # LOCAL, never UTC


def _err(msg, code):
    return jsonify({"error": msg}), code


def _uid():
    return (current_user() or {}).get("id")


def _require_internal():
    """403 unless the caller is an INTERNAL role. Authoring is an SSC act —
    architect and client never author, whatever path gates they passed."""
    if not elevation.is_internal():
        return _err("forbidden", 403)
    return None


def _sets_base() -> Path:
    return ssc_paths.under_root("data_room", "drawing_sets")


# ============================================================================
# TEXT-LAYER PARSING — auto-name from the PDF, never OCR
# ============================================================================

_SHEET_NO_RE = re.compile(r"\b([A-Z]{1,2}-\d{3}\.\d{2})\b")
# The caption pattern that marks an elevation DRAWING on the page. Anchored on
# EXISTING/PROPOSED so "SEE SOUTH ELEVATION" notes and street names containing
# a direction word ("EAST 135 STREET") never false-positive.
_CAPTION_RE = re.compile(
    r"\b(EXISTING|PROPOSED)\s+(NORTH|SOUTH|EAST|WEST|NORTHEAST|NORTHWEST|"
    r"SOUTHEAST|SOUTHWEST)[\s-]+ELEVATION")
_TITLE_STOP_RE = re.compile(
    r"(?i)^(scale|date|drawn|checked|sheet|project|drawing\s*no|job|seal|"
    r"block|lot|address|revision|no\.|as\s+noted)")


def parse_sheet_text(text: str) -> dict:
    """PURE: one page's extracted text -> naming pre-fill. Empty text (a
    scanned set has no layer) degrades to blanks — never an error."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    m = _SHEET_NO_RE.search(text or "")
    sheet_number = m.group(1) if m else None

    captions = _CAPTION_RE.findall(text or "")
    faces = []
    for _kind, face in captions:
        f = face.title()
        if f not in faces:
            faces.append(f)
    has_existing = any(k == "EXISTING" for k, _ in captions)
    has_proposed = any(k == "PROPOSED" for k, _ in captions)

    # THE TITLE ANCHOR IS "SHEET NO.", NOT "Drawing Title:". In this title-block
    # layout the extractor emits the field LABELS as one run and the VALUES as
    # another, so the line after "Drawing Title:" is the next label — garbage.
    # The actual title lands between "SHEET NO." and the sheet-number line
    # (measured on the real 890 set: ... 'SHEET NO.', 'ELEVATIONS', 'A-001.00').
    # No anchor match -> title stays None: an honest blank pre-fill beats a
    # confident wrong one.
    title = None
    for i, ln in enumerate(lines):
        if not re.match(r"(?i)^SHEET\s*NO\.?", ln):
            continue
        parts = []
        for follow in lines[i + 1:i + 4]:
            if _SHEET_NO_RE.search(follow):
                break
            if len(follow) > 48 or ":" in follow or _TITLE_STOP_RE.match(follow):
                parts = []
                break
            parts.append(follow)
        if parts:
            title = " ".join(parts)
        break

    is_elevation = bool(captions) or bool(title and "ELEVATION" in title.upper())

    label = title
    if label and len(faces) == 1 and faces[0].upper() not in label.upper():
        label = f"{label} — {faces[0]}"
    if not label and faces:
        label = "Elevations — " + " / ".join(faces)

    return {
        "sheet_number": sheet_number,
        "label": label,
        "is_elevation": is_elevation,
        "faces": faces,
        "has_existing": has_existing,
        "has_proposed": has_proposed,
        "text_chars": len(text or ""),
    }


# ============================================================================
# UPLOAD — parse, file, render
# ============================================================================

def _render_page_png(pdf_path: Path, page_no: int, long_edge: float,
                     crop_frac=None) -> "tuple[bytes, int, int]":
    """Rasterize one page (1-based) to PNG bytes. crop_frac = (x0,y0,x1,y1)
    fractions in IMAGE coordinates (top-left origin)."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        page = doc[page_no - 1]
        w_pt, h_pt = page.get_size()
        if crop_frac:
            x0, y0, x1, y1 = crop_frac
            # pdfium crop = amounts cut from (left, bottom, right, top) in
            # PDF points; PDF origin is bottom-left, our fractions top-left.
            crop = (x0 * w_pt, (1.0 - y1) * h_pt, (1.0 - x1) * w_pt, y0 * h_pt)
            region_w, region_h = (x1 - x0) * w_pt, (y1 - y0) * h_pt
        else:
            crop = (0, 0, 0, 0)
            region_w, region_h = w_pt, h_pt
        scale = min(long_edge / max(region_w, region_h, 1.0), 6.0)
        bmp = page.render(scale=scale, crop=crop)
        img = bmp.to_pil()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), img.width, img.height
    finally:
        doc.close()


def _api_upload_set(project_code):
    """POST /api/projects/<code>/drawing-sets — multipart {file}. Parses the
    text layer, files one drawing_sheet per page, renders thumb + full PNGs.
    Disk rolls back on failure so DB and tree never diverge."""
    blocked = _require_internal()
    if blocked:
        return blocked
    conn = _db()
    set_dir = None
    try:
        scope = elevation._require_project(conn, project_code)
        if scope:
            return scope
        if "file" not in request.files:
            return _err("no file", 400)
        fs = request.files["file"]
        if Path(fs.filename or "").suffix.lower() != ".pdf":
            return _err("a drawing set must be a PDF", 400)
        data = fs.read()
        if len(data) > MAX_PDF_BYTES:
            return _err(f"file exceeds {MAX_PDF_BYTES // (1024 * 1024)} MB", 400)
        if not data.startswith(b"%PDF"):
            return _err("that file is not a PDF", 400)

        import pypdf
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            n_pages = len(reader.pages)
        except Exception:
            return _err("could not read that PDF", 400)
        if n_pages == 0:
            return _err("the PDF has no pages", 400)
        if n_pages > MAX_PAGES:
            return _err(f"the PDF has {n_pages} pages — the limit is {MAX_PAGES}", 400)

        cur = conn.execute(
            "INSERT INTO drawing_set (project_code, filename, page_count, "
            "uploaded_by_uid, uploaded_at) VALUES (?,?,?,?,?)",
            (project_code, Path(fs.filename or "set.pdf").name, n_pages,
             _uid(), _now()))
        set_id = cur.lastrowid

        base = _sets_base().resolve()
        set_dir = _sets_base() / project_code / str(set_id)
        if not set_dir.resolve().is_relative_to(base):
            conn.rollback()
            return _err("invalid project path", 400)
        set_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = set_dir / "original.pdf"
        pdf_path.write_bytes(data)
        conn.execute("UPDATE drawing_set SET pdf_path=? WHERE id=?",
                     (ssc_paths.store_rel(pdf_path), set_id))

        sheets = []
        for pno in range(1, n_pages + 1):
            page = reader.pages[pno - 1]
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            meta = parse_sheet_text(text)
            thumb_bytes, _, _ = _render_page_png(pdf_path, pno, THUMB_LONG_EDGE)
            full_bytes, _, _ = _render_page_png(pdf_path, pno, FULL_LONG_EDGE)
            tpath = set_dir / f"p{pno}_thumb.png"
            fpath = set_dir / f"p{pno}_full.png"
            tpath.write_bytes(thumb_bytes)
            fpath.write_bytes(full_bytes)
            mb = page.mediabox
            cur = conn.execute(
                "INSERT INTO drawing_sheet (set_id, page_no, label, sheet_number, "
                "is_elevation, thumb_path, image_path, width_pt, height_pt, text_meta) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (set_id, pno, meta["label"], meta["sheet_number"],
                 1 if meta["is_elevation"] else 0,
                 ssc_paths.store_rel(tpath), ssc_paths.store_rel(fpath),
                 float(mb.width), float(mb.height), json.dumps(meta)))
            sheets.append({"id": cur.lastrowid, "page_no": pno,
                           "label": meta["label"],
                           "sheet_number": meta["sheet_number"],
                           "is_elevation": meta["is_elevation"]})
        conn.commit()
        logging.info(f"drawing-set: uploaded set={set_id} project={project_code} "
                     f"pages={n_pages} uid={_uid()}")
        return jsonify({"data": {"id": set_id, "page_count": n_pages,
                                 "sheets": sheets}}), 201
    except Exception:
        conn.rollback()
        if set_dir is not None and set_dir.exists():
            shutil.rmtree(set_dir, ignore_errors=True)
        logging.exception("drawing-set upload failed")
        return _err("upload failed", 500)
    finally:
        conn.close()


# ============================================================================
# READ + SERVE
# ============================================================================

def _set_row(conn, set_id):
    return conn.execute(
        "SELECT id, project_code, filename, pdf_path, page_count, uploaded_at "
        "FROM drawing_set WHERE id=?", (set_id,)).fetchone()


def _api_list_sets(project_code):
    """GET /api/projects/<code>/drawing-sets — sets + their sheets."""
    blocked = _require_internal()
    if blocked:
        return blocked
    conn = _db()
    try:
        scope = elevation._require_project(conn, project_code)
        if scope:
            return scope
        sets = conn.execute(
            "SELECT id, filename, page_count, uploaded_at FROM drawing_set "
            "WHERE project_code=? ORDER BY id DESC", (project_code,)).fetchall()
        out = []
        for s in sets:
            rows = conn.execute(
                "SELECT id, page_no, label, sheet_number, is_elevation, text_meta "
                "FROM drawing_sheet WHERE set_id=? ORDER BY page_no", (s["id"],)).fetchall()
            sheets = []
            for r in rows:
                try:
                    meta = json.loads(r["text_meta"] or "{}")
                except ValueError:
                    meta = {}
                sheets.append({
                    "id": r["id"], "page_no": r["page_no"], "label": r["label"],
                    "sheet_number": r["sheet_number"],
                    "is_elevation": bool(r["is_elevation"]),
                    "faces": meta.get("faces") or [],
                    "has_existing": bool(meta.get("has_existing")),
                    "has_proposed": bool(meta.get("has_proposed")),
                })
            out.append({"id": s["id"], "filename": s["filename"],
                        "page_count": s["page_count"],
                        "uploaded_at": s["uploaded_at"], "sheets": sheets})
        return jsonify({"data": out})
    finally:
        conn.close()


def _serve_sheet_file(sheet_id, col):
    """Immutable serve of a sheet render. Bytes never change per id (#291/#292
    doctrine: replacement = new set = new ids), so the browser caches forever;
    `private` keeps it out of any shared edge cache."""
    blocked = _require_internal()
    if blocked:
        return blocked
    conn = _db()
    try:
        row = conn.execute(
            f"SELECT sh.{col} AS p, ds.project_code AS code FROM drawing_sheet sh "
            f"JOIN drawing_set ds ON ds.id = sh.set_id WHERE sh.id=?",
            (sheet_id,)).fetchone()
        if row is None or not row["p"]:
            return _err("not found", 404)
        scope = elevation._require_project(conn, row["code"])
        if scope:
            return scope
    finally:
        conn.close()
    p = ssc_paths.resolve_data_path(row["p"])
    try:
        if not p.resolve().is_relative_to(_sets_base().resolve()) or not p.exists():
            return _err("not found", 404)
    except (OSError, ValueError):
        return _err("not found", 404)
    resp = send_file(str(p), mimetype="image/png", conditional=True)
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


def _api_sheet_thumb(sheet_id):
    return _serve_sheet_file(sheet_id, "thumb_path")


def _api_sheet_image(sheet_id):
    return _serve_sheet_file(sheet_id, "image_path")


def _api_set_pdf(set_id):
    """GET /api/drawing-sets/<id>/pdf — the original, reference only. The HTML
    grid is the working surface; this is the source document."""
    blocked = _require_internal()
    if blocked:
        return blocked
    conn = _db()
    try:
        row = _set_row(conn, set_id)
        if row is None or not row["pdf_path"]:
            return _err("not found", 404)
        scope = elevation._require_project(conn, row["project_code"])
        if scope:
            return scope
    finally:
        conn.close()
    p = ssc_paths.resolve_data_path(row["pdf_path"])
    try:
        if not p.resolve().is_relative_to(_sets_base().resolve()) or not p.exists():
            return _err("not found", 404)
    except (OSError, ValueError):
        return _err("not found", 404)
    resp = send_file(str(p), mimetype="application/pdf", conditional=True,
                     download_name=(row["filename"] or "drawing-set.pdf"))
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


def _api_patch_sheet(sheet_id):
    """PATCH /api/drawing-sheets/<id> — {label?, sheet_number?, is_elevation?}.
    The operator's one-field confirm/override of the text-layer pre-fill."""
    blocked = _require_internal()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    conn = _db()
    try:
        row = conn.execute(
            "SELECT sh.id, ds.project_code AS code FROM drawing_sheet sh "
            "JOIN drawing_set ds ON ds.id = sh.set_id WHERE sh.id=?",
            (sheet_id,)).fetchone()
        if row is None:
            return _err("not found", 404)
        scope = elevation._require_project(conn, row["code"])
        if scope:
            return scope
        sets, params = [], []
        if "label" in data:
            sets.append("label=?")
            params.append((str(data.get("label") or "").strip() or None))
        if "sheet_number" in data:
            sets.append("sheet_number=?")
            params.append((str(data.get("sheet_number") or "").strip() or None))
        if "is_elevation" in data:
            sets.append("is_elevation=?")
            params.append(1 if data.get("is_elevation") else 0)
        if not sets:
            return _err("nothing to update", 400)
        params.append(sheet_id)
        conn.execute(f"UPDATE drawing_sheet SET {', '.join(sets)} WHERE id=?",
                     tuple(params))
        conn.commit()
        new = conn.execute(
            "SELECT id, page_no, label, sheet_number, is_elevation "
            "FROM drawing_sheet WHERE id=?", (sheet_id,)).fetchone()
        return jsonify({"data": {"id": new["id"], "page_no": new["page_no"],
                                 "label": new["label"],
                                 "sheet_number": new["sheet_number"],
                                 "is_elevation": bool(new["is_elevation"])}})
    finally:
        conn.close()


def _api_delete_set(set_id):
    """DELETE /api/drawing-sets/<id> — a bad upload is removable, but never
    out from under an elevation that references one of its sheets (409)."""
    blocked = _require_internal()
    if blocked:
        return blocked
    conn = _db()
    try:
        row = _set_row(conn, set_id)
        if row is None:
            return _err("not found", 404)
        scope = elevation._require_project(conn, row["project_code"])
        if scope:
            return scope
        ref = conn.execute(
            "SELECT COUNT(*) AS n FROM elevation e "
            "JOIN drawing_sheet sh ON sh.id = e.source_sheet_id "
            "WHERE sh.set_id=?", (set_id,)).fetchone()
        if ref["n"]:
            return _err("an elevation references this set — delete or re-source "
                        "those elevations first", 409)
        conn.execute("DELETE FROM drawing_sheet WHERE set_id=?", (set_id,))
        conn.execute("DELETE FROM drawing_set WHERE id=?", (set_id,))
        conn.commit()
    finally:
        conn.close()
    set_dir = _sets_base() / row["project_code"] / str(set_id)
    try:
        if set_dir.resolve().is_relative_to(_sets_base().resolve()) and set_dir.exists():
            shutil.rmtree(set_dir, ignore_errors=True)
    except (OSError, ValueError):
        pass
    return jsonify({"data": {"id": set_id, "deleted": True}})


# ============================================================================
# AI PROPOSAL — Tier B: machine proposes, human approves
# ============================================================================

TRACE_SCHEMA = {
    "type": "object",
    "properties": {
        "subject_found": {"type": "boolean",
                          "description": "True if the image contains a building facade elevation drawing"},
        "multiple_drawings": {"type": "boolean",
                              "description": "True if the image contains MORE THAN ONE distinct elevation drawing (e.g. an EXISTING and a PROPOSED view)"},
        "drawings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "label": {"type": ["string", "null"]},
                "kind": {"type": "string", "enum": ["existing", "proposed", "other"]},
                "x0": {"type": "number"}, "y0": {"type": "number"},
                "x1": {"type": "number"}, "y1": {"type": "number"},
            },
            "required": ["label", "kind", "x0", "y0", "x1", "y1"],
            "additionalProperties": False,
        }, "description": "Bounding box (fractions of THIS image, top-left origin) for each distinct elevation drawing seen"},
        "bays": {"type": "integer",
                 "description": "Vertical bay count of the SINGLE subject drawing (columns of windows/openings between structural grid lines)"},
        "floors": {"type": "integer",
                   "description": "Occupied floor count (stories), excluding parapet and roof bulkheads"},
        "col_fractions": {"type": "array", "items": {"type": "number"},
                          "description": "Relative widths of the bays, left to right; length == bays"},
        "row_fractions": {"type": "array", "items": {"type": "number"},
                          "description": "Relative heights of the floors, BOTTOM (ground) first; length == floors"},
        "ground_floor_taller": {"type": "boolean"},
        "irregularities": {"type": "array", "items": {"type": "string"},
                           "description": "Obvious irregularities: taller ground floor, setbacks, partial bays, towers, corner returns"},
        "confidence": {"type": "number"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["subject_found", "multiple_drawings", "drawings", "bays",
                 "floors", "col_fractions", "row_fractions",
                 "ground_floor_taller", "irregularities", "confidence"],
    "additionalProperties": False,
}

TRACE_PROMPT = """You are tracing a building facade for an NYC facade-restoration contractor. The image is a region of an architect's elevation sheet.

An "elevation drawing" here is the flat orthographic view of one building face. Sheets usually carry TWO of them — an EXISTING view and a PROPOSED view — plus notes, title block, and detail callouts.

First decide what this image contains:
- If it contains MORE THAN ONE distinct elevation drawing, set multiple_drawings=true and return a bounding box (x0,y0,x1,y1 as fractions of this image, origin top-left) for EACH, with kind existing/proposed/other. Then still analyze the single clearest EXISTING drawing for the grid fields.
- If it contains exactly one, set multiple_drawings=false and analyze it. Still return its bounding box in drawings.
- If there is no facade elevation drawing at all, set subject_found=false.

For the subject drawing, return the facade grid:
- bays: the count of vertical bays — columns of window openings between structural grid lines/piers, counted along the width. Corner returns drawn at the ends are part of the end bays, not extra bays.
- floors: occupied stories from grade to the roof line. EXCLUDE parapets and rooftop bulkheads.
- col_fractions: relative bay widths left-to-right (they need not be equal — end bays and entrance bays often differ). Length must equal bays.
- row_fractions: relative floor heights BOTTOM FIRST (ground floor first). Ground floors are often taller. Length must equal floors.
- irregularities: anything the grid cannot express — setbacks, towers, partial bays, a bay with a door instead of windows.
- confidence: 0.0-1.0. Lower it when the drawing is small, cluttered, or partly cropped. Count carefully before answering: miscounting bays or floors is the costly failure here.

Return ONLY the structured JSON."""


class TraceUnavailable(Exception):
    """No API key — route answers 503 {ai_available:false}; manual path stands."""


class TraceError(Exception):
    """Vision call/parse failed — 502; manual path stands."""


def call_trace_model(png_bytes: bytes) -> dict:
    """ONE vision call. SSC_TRACE_FAKE=<json path> short-circuits with planted
    raw JSON through the same validator (deterministic tests, no key, no net)."""
    fake = runtime_seams.seam("SSC_TRACE_FAKE").strip()
    if fake:
        try:
            # utf-8-sig: a PowerShell-authored fake carries a BOM; tolerate it.
            return json.loads(Path(fake).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            raise TraceError(f"SSC_TRACE_FAKE unreadable: {type(e).__name__}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise TraceUnavailable("ANTHROPIC_API_KEY not configured")
    import anthropic
    client = anthropic.Anthropic()
    try:
        resp = client.messages.create(
            model=TRACE_MODEL,
            max_tokens=10000,   # adaptive thinking shares this cap with the JSON
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": TRACE_SCHEMA}},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": base64.standard_b64encode(png_bytes).decode("ascii")}},
                {"type": "text", "text": TRACE_PROMPT},
            ]}],
        )
    except Exception as e:
        raise TraceError(f"vision API call failed: {type(e).__name__}")
    if getattr(resp, "stop_reason", None) == "refusal":
        raise TraceError("vision API declined the request")
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        raise TraceError("model response was not valid JSON")


def _norm_fractions(raw, count, warnings, what) -> list:
    """Clamp+normalize a fractions list to `count` positive entries summing 1.
    Mismatch degrades to uniform with a warning — never an error."""
    try:
        vals = [float(v) for v in (raw or [])]
    except (TypeError, ValueError):
        vals = []
    if len(vals) != count or any(v <= 0 for v in vals):
        if raw:
            warnings.append(f"{what} did not match the count — using equal {what}.")
        vals = [1.0] * count
    total = sum(vals)
    vals = [max(v / total, MIN_FRACTION) for v in vals]
    total = sum(vals)
    return [v / total for v in vals]


def process_trace_result(raw: dict) -> dict:
    """PURE: validate + coerce the model's raw JSON into a safe proposal.
    Never trusts blindly — counts clamp, fractions normalize, regions clamp to
    the unit square. This is what BOTH the real call and the fake seam feed."""
    warnings = [str(n) for n in (raw.get("notes") or []) if n]
    subject = bool(raw.get("subject_found"))
    try:
        bays = int(raw.get("bays") or 0)
    except (TypeError, ValueError):
        bays = 0
    try:
        floors = int(raw.get("floors") or 0)
    except (TypeError, ValueError):
        floors = 0
    if subject and not (1 <= bays <= MAX_BAYS):
        warnings.append(f"Bay count {bays} out of range — set it manually.")
        bays = 0
    if subject and not (1 <= floors <= MAX_FLOORS):
        warnings.append(f"Floor count {floors} out of range — set it manually.")
        floors = 0

    cols = _norm_fractions(raw.get("col_fractions"), bays, warnings, "bay widths") \
        if bays else []
    rows = _norm_fractions(raw.get("row_fractions"), floors, warnings, "floor heights") \
        if floors else []

    drawings = []
    for d in (raw.get("drawings") or [])[:6]:
        try:
            x0, y0 = max(0.0, min(1.0, float(d["x0"]))), max(0.0, min(1.0, float(d["y0"])))
            x1, y1 = max(0.0, min(1.0, float(d["x1"]))), max(0.0, min(1.0, float(d["y1"])))
        except (KeyError, TypeError, ValueError):
            continue
        if x1 - x0 < 0.02 or y1 - y0 < 0.02:
            continue
        kind = d.get("kind") if d.get("kind") in ("existing", "proposed", "other") else "other"
        drawings.append({"label": (str(d.get("label") or "").strip() or None),
                         "kind": kind,
                         "x0": round(x0, 4), "y0": round(y0, 4),
                         "x1": round(x1, 4), "y1": round(y1, 4)})

    try:
        conf = max(0.0, min(1.0, float(raw.get("confidence"))))
    except (TypeError, ValueError):
        conf = 0.0

    return {
        "subject_found": subject,
        "multiple_drawings": bool(raw.get("multiple_drawings")),
        "drawings": drawings,
        "bays": bays,
        "floors": floors,
        "col_fractions": [round(v, 5) for v in cols],
        "row_fractions": [round(v, 5) for v in rows],
        "ground_floor_taller": bool(raw.get("ground_floor_taller")),
        "irregularities": [str(s) for s in (raw.get("irregularities") or []) if s][:12],
        "confidence": round(conf, 2),
        "warnings": warnings,
    }


def _api_propose(set_id):
    """POST /api/drawing-sets/<id>/propose — {page_no, region?}. Renders the
    region at high res, sends it to the vision model, returns the proposal.
    NEVER writes anything: proposals land in the operator's editor, and only a
    human confirm turns a grid into cells."""
    blocked = _require_internal()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    conn = _db()
    try:
        row = _set_row(conn, set_id)
        if row is None or not row["pdf_path"]:
            return _err("not found", 404)
        scope = elevation._require_project(conn, row["project_code"])
        if scope:
            return scope
        try:
            page_no = int(data.get("page_no"))
        except (TypeError, ValueError):
            return _err("page_no is required", 400)
        if not (1 <= page_no <= row["page_count"]):
            return _err("no such page", 404)
    finally:
        conn.close()

    region = None
    r = data.get("region")
    if isinstance(r, dict):
        try:
            x0, y0 = max(0.0, min(1.0, float(r["x0"]))), max(0.0, min(1.0, float(r["y0"])))
            x1, y1 = max(0.0, min(1.0, float(r["x1"]))), max(0.0, min(1.0, float(r["y1"])))
        except (KeyError, TypeError, ValueError):
            return _err("region must be {x0,y0,x1,y1} fractions", 400)
        if x1 - x0 < 0.05 or y1 - y0 < 0.05:
            return _err("region is too small to analyze", 400)
        region = (x0, y0, x1, y1)

    pdf = ssc_paths.resolve_data_path(row["pdf_path"])
    try:
        if not pdf.resolve().is_relative_to(_sets_base().resolve()) or not pdf.exists():
            return _err("not found", 404)
    except (OSError, ValueError):
        return _err("not found", 404)

    try:
        png, _, _ = _render_page_png(pdf, page_no, AI_LONG_EDGE, crop_frac=region)
        raw = call_trace_model(png)
        proposal = process_trace_result(raw)
    except TraceUnavailable:
        return jsonify({"ai_available": False,
                        "error": "AI trace unavailable — author the grid manually"}), 503
    except TraceError as e:
        logging.warning(f"trace: {e}")
        return jsonify({"ai_available": True,
                        "error": "AI trace failed — try again or author manually"}), 502
    logging.info(f"trace: set={set_id} p{page_no} region={'yes' if region else 'full'} "
                 f"bays={proposal['bays']} floors={proposal['floors']} "
                 f"conf={proposal['confidence']}")
    return jsonify({"ai_available": True, "data": proposal})


# ============================================================================
# AUTHORED GEOMETRY — same shape the 890 North trace feeds the renderer
# ============================================================================

def level_ids(floors: int) -> list:
    """Zero-padded bottom-up level ids: L01..L80. Padding is what keeps string
    sorts (SQL ORDER BY level_id, the template's sort) numeric-correct — the
    CLAUDE.md zero-padded-id rule applied to floors."""
    width = max(2, len(str(floors)))
    return [f"L{n:0{width}d}" for n in range(1, floors + 1)]


def build_authored_geometry(cols_fr, rows_fr, source=None) -> dict:
    """PURE: fractions -> geometry_json in the EXACT shape the drawing-markup
    renderer consumes (facade/constants/grids/datums/levels/bounds/columns/
    openings/features/drops). Parity is this function: an authored elevation
    renders through the same code path as the traced 890 North because the
    payload is shaped identically — columns/openings empty, features {}.

    cols_fr: bay width fractions left->right.  rows_fr: floor height fractions
    BOTTOM FIRST. Both must be positive; they are re-normalized here."""
    bays, floors = len(cols_fr), len(rows_fr)
    if not (1 <= bays <= MAX_BAYS):
        raise ValueError(f"bays must be 1..{MAX_BAYS}")
    if not (1 <= floors <= MAX_FLOORS):
        raise ValueError(f"floors must be 1..{MAX_FLOORS}")
    if any(float(v) <= 0 for v in cols_fr) or any(float(v) <= 0 for v in rows_fr):
        raise ValueError("fractions must be positive")

    ct = sum(float(v) for v in cols_fr)
    rt = sum(float(v) for v in rows_fr)
    cols = [float(v) / ct for v in cols_fr]
    rows = [float(v) / rt for v in rows_fr]

    width_ft = NOMINAL_BAY_FT * bays
    wall_ft = NOMINAL_FLOOR_FT * floors
    parapet_top = round(wall_ft + PARAPET_FT, 2)

    bounds = [0.0]
    for c in cols:
        bounds.append(round(bounds[-1] + c * width_ft, 2))
    bounds[-1] = round(width_ft, 2)

    ids = level_ids(floors)
    tops = [0.0]
    for rfr in rows:
        tops.append(round(tops[-1] + rfr * wall_ft, 2))
    tops[-1] = round(wall_ft, 2)
    # levels TOP-DOWN as drawn (the 890 shape); ids stay bottom-up.
    levels = [{"id": ids[i], "name": f"Floor {i + 1}",
               "top": tops[i + 1], "bot": tops[i]}
              for i in range(floors)][::-1]

    datums = [{"id": ids[i], "name": f"FLOOR {i + 1}", "y": tops[i]}
              for i in range(floors)]
    datums.append({"id": "ROOF", "name": "ROOF", "y": tops[-1]})
    datums.reverse()

    grids = [{"id": str(i + 1), "x": bounds[i]} for i in range(len(bounds))]

    drops = []
    for i in range(bays):
        drops.append({"idx": i + 1, "grid_from": str(i + 1), "grid_to": str(i + 2),
                      "x0": bounds[i], "x1": bounds[i + 1],
                      "width_ft": round(bounds[i + 1] - bounds[i], 2),
                      "area_sf": None, "clear_bay": "", "note": ""})

    return {
        "authored": True,
        "provisional": False,
        "dimension_basis": "authored",
        "scale_note": AUTHORED_SCALE_NOTE,
        "source": source or {},
        "units": "ft",
        "facade": {"width_ft": round(width_ft, 2), "height_ft": parapet_top,
                   "draw_top_ft": parapet_top},
        "constants": {"parapet_top": parapet_top},
        "grids": grids,
        "datums": datums,
        "levels": levels,
        "bounds": bounds,
        "columns": [],
        "openings": [],
        "features": {},
        "author_spec": {"cols": [round(c, 5) for c in cols],
                        "rows": [round(r, 5) for r in rows]},
        "drops": drops,
    }


# ============================================================================
# ELEVATION AUTHORING — create draft, save geometry, confirm, delete
# ============================================================================

_FACE_CODES = {
    "north": "N", "south": "S", "east": "E", "west": "W",
    "northeast": "NE", "north-east": "NE", "ne": "NE",
    "northwest": "NW", "north-west": "NW", "nw": "NW",
    "southeast": "SE", "south-east": "SE", "se": "SE",
    "southwest": "SW", "south-west": "SW", "sw": "SW",
}


def face_code(label: str) -> str:
    """A short filing code from a free-text face label. 'North' -> N,
    'SE' -> SE, 'North — Building B' -> N, anything else -> its initials."""
    words = re.findall(r"[A-Za-z]+", (label or "").lower())
    for w in words:
        if w in _FACE_CODES:
            return _FACE_CODES[w]
    letters = "".join(w[0] for w in words[:4]).upper()
    return letters or "X"


def _elev_row(conn, elev_id):
    return conn.execute(
        "SELECT id, project_code, face, face_label, name, status, geometry_json, "
        "       source_sheet_id, region_json FROM elevation WHERE id=?",
        (elev_id,)).fetchone()


def _sheet_source(conn, sheet_id):
    """(project_code, source dict) for a sheet, or (None, None)."""
    row = conn.execute(
        "SELECT sh.id, sh.page_no, sh.label, sh.sheet_number, ds.id AS set_id, "
        "       ds.project_code AS code, ds.filename "
        "FROM drawing_sheet sh JOIN drawing_set ds ON ds.id = sh.set_id "
        "WHERE sh.id=?", (sheet_id,)).fetchone()
    if row is None:
        return None, None
    return row["code"], {
        "set_id": row["set_id"], "sheet_id": row["id"], "page_no": row["page_no"],
        "sheet": (row["sheet_number"] or row["label"] or f"p{row['page_no']}"),
        "sheet_label": row["label"], "set_filename": row["filename"],
    }


def _api_create_elevation():
    """POST /api/author/elevations — {project_code, face_label, name?,
    source_sheet_id?, region?}. Creates a DRAFT: invisible to external
    audiences until a human confirms."""
    blocked = _require_internal()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    code = (data.get("project_code") or "").strip()
    face_label = (data.get("face_label") or "").strip()
    if not code:
        return _err("project_code is required", 400)
    if not face_label or len(face_label) > 60:
        return _err("face_label is required (60 characters max)", 400)
    conn = _db()
    try:
        scope = elevation._require_project(conn, code)
        if scope:
            return scope
        sheet_id = data.get("source_sheet_id")
        region_json = None
        if sheet_id is not None:
            sheet_code, _src = _sheet_source(conn, sheet_id)
            if sheet_code is None:
                return _err("source sheet not found", 404)
            if sheet_code != code:
                return _err("that sheet belongs to a different project", 400)
        r = data.get("region")
        if isinstance(r, dict):
            try:
                region_json = json.dumps({
                    "x0": max(0.0, min(1.0, float(r["x0"]))),
                    "y0": max(0.0, min(1.0, float(r["y0"]))),
                    "x1": max(0.0, min(1.0, float(r["x1"]))),
                    "y1": max(0.0, min(1.0, float(r["y1"]))),
                })
            except (KeyError, TypeError, ValueError):
                return _err("region must be {x0,y0,x1,y1} fractions", 400)
        name = (data.get("name") or "").strip() or f"{face_label} Elevation"
        cur = conn.execute(
            "INSERT INTO elevation (project_code, face, face_label, name, status, "
            "source_sheet_id, region_json, created_at) VALUES (?,?,?,?, 'draft', ?,?,?)",
            (code, face_code(face_label), face_label, name, sheet_id, region_json, _now()))
        conn.commit()
        logging.info(f"author: draft elevation id={cur.lastrowid} project={code} "
                     f"face={face_label!r} uid={_uid()}")
        return jsonify({"data": {"id": cur.lastrowid, "project_code": code,
                                 "face_label": face_label, "name": name,
                                 "status": "draft"}}), 201
    finally:
        conn.close()


def _api_put_geometry(elev_id):
    """PUT /api/author/elevations/<id>/geometry — {cols[], rows[], face_label?,
    source_sheet_id?, region?}. DRAFTS ONLY: a confirmed elevation's grid is
    the surface the team files status against; re-shaping it under their marks
    is refused (409), not silently absorbed."""
    blocked = _require_internal()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    conn = _db()
    try:
        row = _elev_row(conn, elev_id)
        if row is None:
            return _err("not found", 404)
        scope = elevation._require_project(conn, row["project_code"])
        if scope:
            return scope
        if row["status"] != "draft":
            return _err("this elevation is confirmed — its grid is locked", 409)

        cols, rows = data.get("cols"), data.get("rows")
        if not isinstance(cols, list) or not isinstance(rows, list):
            return _err("cols and rows are required", 400)
        try:
            cols = [float(v) for v in cols]
            rows = [float(v) for v in rows]
        except (TypeError, ValueError):
            return _err("cols and rows must be numbers", 400)

        sheet_id = row["source_sheet_id"]
        if "source_sheet_id" in data:
            sheet_id = data.get("source_sheet_id")
        region_json = row["region_json"]
        if "region" in data:
            r = data.get("region")
            if isinstance(r, dict):
                try:
                    region_json = json.dumps({
                        "x0": max(0.0, min(1.0, float(r["x0"]))),
                        "y0": max(0.0, min(1.0, float(r["y0"]))),
                        "x1": max(0.0, min(1.0, float(r["x1"]))),
                        "y1": max(0.0, min(1.0, float(r["y1"]))),
                    })
                except (KeyError, TypeError, ValueError):
                    return _err("region must be {x0,y0,x1,y1} fractions", 400)
            else:
                region_json = None

        source = {}
        if sheet_id is not None:
            sheet_code, source = _sheet_source(conn, sheet_id)
            if sheet_code is None:
                return _err("source sheet not found", 404)
            if sheet_code != row["project_code"]:
                return _err("that sheet belongs to a different project", 400)
            if region_json:
                try:
                    source["region"] = json.loads(region_json)
                except ValueError:
                    pass

        face_label = row["face_label"]
        if "face_label" in data:
            face_label = (str(data.get("face_label") or "").strip() or face_label)

        try:
            geom = build_authored_geometry(cols, rows, source=source)
        except ValueError as ve:
            return _err(str(ve), 400)

        conn.execute(
            "UPDATE elevation SET geometry_json=?, face=?, face_label=?, name=?, "
            "source_sheet_id=?, region_json=?, source_sheet=? WHERE id=?",
            (json.dumps(geom), face_code(face_label), face_label,
             f"{face_label} Elevation", sheet_id, region_json,
             (source.get("sheet") if source else None), elev_id))
        conn.commit()
        return jsonify({"data": {"id": elev_id, "status": "draft",
                                 "bays": len(geom["author_spec"]["cols"]),
                                 "floors": len(geom["author_spec"]["rows"]),
                                 "geometry": geom}})
    finally:
        conn.close()


def _api_confirm(elev_id):
    """POST /api/author/elevations/<id>/confirm — the ONLY way a proposal
    becomes live. Generates elevation_drop rows (one per bay) + elevation_cell
    rows (one per bay per floor), flips status to confirmed. One-way."""
    blocked = _require_internal()
    if blocked:
        return blocked
    conn = _db()
    try:
        row = _elev_row(conn, elev_id)
        if row is None:
            return _err("not found", 404)
        scope = elevation._require_project(conn, row["project_code"])
        if scope:
            return scope
        if row["status"] != "draft":
            return _err("already confirmed", 409)
        try:
            geom = json.loads(row["geometry_json"] or "{}")
        except ValueError:
            geom = {}
        spec = geom.get("author_spec") or {}
        if not spec.get("cols") or not spec.get("rows"):
            return _err("set the grid before confirming", 400)

        floors = len(spec["rows"])
        ids = level_ids(floors)
        names = {ids[i]: f"Floor {i + 1}" for i in range(floors)}
        n_cells = 0
        for d in geom.get("drops") or []:
            cur = conn.execute(
                "INSERT INTO elevation_drop (elevation_id, idx, grid_from, grid_to, "
                "x0, x1, width_ft, area_sf, note) VALUES (?,?,?,?,?,?,?,?,?)",
                (elev_id, d["idx"], d["grid_from"], d["grid_to"], d["x0"], d["x1"],
                 d["width_ft"], d.get("area_sf"), (d.get("note") or None)))
            drop_id = cur.lastrowid
            for lid in ids:
                conn.execute(
                    "INSERT INTO elevation_cell (drop_id, level_id, level_name, "
                    "status_key, updated_at) VALUES (?,?,?,'not_started',?)",
                    (drop_id, lid, names[lid], _now()))
                n_cells += 1
        conn.execute("UPDATE elevation SET status='confirmed' WHERE id=?", (elev_id,))
        conn.commit()
        logging.info(f"author: confirmed elevation id={elev_id} "
                     f"drops={len(geom.get('drops') or [])} cells={n_cells} uid={_uid()}")
        return jsonify({"data": {"id": elev_id, "status": "confirmed",
                                 "drops": len(geom.get("drops") or []),
                                 "cells": n_cells}})
    finally:
        conn.close()


def _api_delete_elevation(elev_id):
    """DELETE /api/author/elevations/<id> — DRAFTS ONLY. A confirmed elevation
    carries (or will carry) the team's status marks; deleting it is not an
    authoring action and is refused (409)."""
    blocked = _require_internal()
    if blocked:
        return blocked
    conn = _db()
    try:
        row = _elev_row(conn, elev_id)
        if row is None:
            return _err("not found", 404)
        scope = elevation._require_project(conn, row["project_code"])
        if scope:
            return scope
        if row["status"] != "draft":
            return _err("a confirmed elevation cannot be deleted here", 409)
        # Defensive sweep: a draft has no drops/cells by construction, but a
        # stray row must not orphan.
        drops = conn.execute("SELECT id FROM elevation_drop WHERE elevation_id=?",
                             (elev_id,)).fetchall()
        for d in drops:
            conn.execute("DELETE FROM elevation_cell WHERE drop_id=?", (d["id"],))
        conn.execute("DELETE FROM elevation_drop WHERE elevation_id=?", (elev_id,))
        conn.execute("DELETE FROM elevation WHERE id=?", (elev_id,))
        conn.commit()
        return jsonify({"data": {"id": elev_id, "deleted": True}})
    finally:
        conn.close()


# ============================================================================
# PAGE + CONTEXT
# ============================================================================

def _author_page():
    """GET /drawing-author — internal only. NOT in the architect allowlist and
    NOT opened by any client grant: default-deny keeps it that way."""
    blocked = _require_internal()
    if blocked:
        return blocked
    if not AUTHOR_PAGE.exists():
        return _err("drawing author page not found", 404)
    resp = send_file(str(AUTHOR_PAGE))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


def _api_author_context():
    """GET /api/author/context — the projects this user may author in, plus
    each project's elevations (drafts included) for the resume/delete list."""
    blocked = _require_internal()
    if blocked:
        return blocked
    conn = _db()
    try:
        codes = elevation.accessible_codes(conn, current_user())
        if not codes:
            return jsonify({"data": {"projects": []}})
        marks = ",".join("?" for _ in codes)
        rows = conn.execute(
            f"SELECT project_code, name FROM projects WHERE project_code IN ({marks}) "
            f"ORDER BY project_code", tuple(codes)).fetchall()
        elevs = conn.execute(
            f"SELECT id, project_code, face, face_label, name, status, "
            f"       source_sheet_id, geometry_json FROM elevation "
            f"WHERE project_code IN ({marks}) ORDER BY id", tuple(codes)).fetchall()
        by_code = {}
        for e in elevs:
            traced = bool((e["geometry_json"] or "").strip() not in ("", "{}"))
            by_code.setdefault(e["project_code"], []).append({
                "id": e["id"], "face": e["face"],
                "face_label": e["face_label"] or e["face"],
                "name": e["name"], "status": e["status"] or "confirmed",
                "source_sheet_id": e["source_sheet_id"], "traced": traced,
            })
        return jsonify({"data": {"projects": [
            {"code": r["project_code"], "name": r["name"],
             "elevations": by_code.get(r["project_code"], [])}
            for r in rows]}})
    finally:
        conn.close()


def _api_get_draft(elev_id):
    """GET /api/author/elevations/<id> — the author-side read: spec + source
    refs for resuming a draft (or inspecting a confirmed grid, read-only)."""
    blocked = _require_internal()
    if blocked:
        return blocked
    conn = _db()
    try:
        row = _elev_row(conn, elev_id)
        if row is None:
            return _err("not found", 404)
        scope = elevation._require_project(conn, row["project_code"])
        if scope:
            return scope
        try:
            geom = json.loads(row["geometry_json"] or "{}")
        except ValueError:
            geom = {}
        region = None
        if row["region_json"]:
            try:
                region = json.loads(row["region_json"])
            except ValueError:
                region = None
        return jsonify({"data": {
            "id": row["id"], "project_code": row["project_code"],
            "face": row["face"], "face_label": row["face_label"],
            "name": row["name"], "status": row["status"] or "confirmed",
            "source_sheet_id": row["source_sheet_id"], "region": region,
            "author_spec": geom.get("author_spec"),
        }})
    finally:
        conn.close()


def register(app) -> None:
    """MUST follow apply_auth_gate + elevation.register (gates before routes)."""
    app.add_url_rule("/drawing-author", "drawing_author_page", _author_page,
                     methods=["GET"])
    app.add_url_rule("/api/author/context", "author_context", _api_author_context,
                     methods=["GET"])
    app.add_url_rule("/api/projects/<project_code>/drawing-sets", "drawing_sets_list",
                     _api_list_sets, methods=["GET"])
    app.add_url_rule("/api/projects/<project_code>/drawing-sets", "drawing_sets_upload",
                     _api_upload_set, methods=["POST"])
    app.add_url_rule("/api/drawing-sets/<int:set_id>", "drawing_set_delete",
                     _api_delete_set, methods=["DELETE"])
    app.add_url_rule("/api/drawing-sets/<int:set_id>/pdf", "drawing_set_pdf",
                     _api_set_pdf, methods=["GET"])
    app.add_url_rule("/api/drawing-sets/<int:set_id>/propose", "drawing_set_propose",
                     _api_propose, methods=["POST"])
    app.add_url_rule("/api/drawing-sheets/<int:sheet_id>", "drawing_sheet_patch",
                     _api_patch_sheet, methods=["PATCH"])
    app.add_url_rule("/api/drawing-sheets/<int:sheet_id>/thumb", "drawing_sheet_thumb",
                     _api_sheet_thumb, methods=["GET"])
    app.add_url_rule("/api/drawing-sheets/<int:sheet_id>/image", "drawing_sheet_image",
                     _api_sheet_image, methods=["GET"])
    app.add_url_rule("/api/author/elevations", "author_elevation_create",
                     _api_create_elevation, methods=["POST"])
    app.add_url_rule("/api/author/elevations/<int:elev_id>", "author_elevation_get",
                     _api_get_draft, methods=["GET"])
    app.add_url_rule("/api/author/elevations/<int:elev_id>", "author_elevation_delete",
                     _api_delete_elevation, methods=["DELETE"])
    app.add_url_rule("/api/author/elevations/<int:elev_id>/geometry",
                     "author_elevation_geometry", _api_put_geometry, methods=["PUT"])
    app.add_url_rule("/api/author/elevations/<int:elev_id>/confirm",
                     "author_elevation_confirm", _api_confirm, methods=["POST"])
