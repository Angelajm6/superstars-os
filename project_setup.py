"""Job setup — the Oct 7 C-suite notes, batch C: a permanent job number, one calendar color per
job, the access method of an exterior job, and what the job's workers see in the worker app.

  projects.job_number      INTEGER, unique, given once in creation order (#1 = the oldest job),
                           never reused or changed
  projects.color_index     0..43 into PALETTE, chosen so no two ACTIVE jobs share one (until
                           there are more active jobs than colors); admin can change it
  projects.access_methods  JSON list of ACCESS_METHODS keys; [] = none; NULL = never set.
                           Exterior Restoration (FR) jobs only
  projects.worker_access   JSON list of WORKER_FEATURES keys; NULL = the default (photos)

Existing jobs are numbered by creation date at boot (idempotent: only rows without a number),
and every active job without a color gets one (its old hashed color when still free).

  GET /api/admin/projects/setup-meta          palette, options, colors in use, the next number
  GET /api/admin/projects/<code>/setup        this job's number, color, access, worker access
  PUT /api/admin/projects/<code>/setup        {color_index?, access_methods?, worker_access?, job_type_other? (Other jobs)}
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from flask import jsonify, request

import db_layer
from auth import current_user, requires_company

log = logging.getLogger("ssc.project_setup")

# 44 soft colors. 0..31 are muted and read with white text (contrast >= 4.5); 0..15 are gentler
# shades of the old 16, so a saved job keeps its color family. 32..43 are light pastels that
# carry dark text instead: INK holds that same-family dark shade (text on bars, the job page).
# company_portal.html and projects.html carry the same list.
PALETTE = ("#4B5F94", "#3F7F76", "#9A6A30", "#7B68B5", "#8E4A6A", "#3F78A3", "#6B7D3E", "#A9524F",
           "#B35E44", "#3D7B5C", "#5670C0", "#94549A", "#7E5F45", "#6A6560", "#A85A77", "#3A7E90",
           "#5A7A5F", "#5A7287", "#8A6A86", "#85712A", "#55743F", "#2F6B5A", "#34557F", "#66466E",
           "#82424F", "#6E4C3C", "#48467E", "#6B6390", "#9A4E2E", "#4A525C", "#A0646A", "#2E6670",
           "#BCD9F2", "#F7E7A1", "#F9CFA5", "#C4E6D2", "#F4CBD6", "#D9D0F0",
           "#BFE5E6", "#F5C3B8", "#D5E2C0", "#E6CDE8", "#E8DCC6", "#C9D2F2")
INK = {32: "#1F4E79", 33: "#6B5408", 34: "#7A3F0E", 35: "#1F5B3C", 36: "#7A2E46", 37: "#44357A", 38: "#1D5A5C", 39: "#7D3226", 40: "#3F5520", 41: "#643068", 42: "#5E4A2C", 43: "#2E3F7A"}
OLD_PALETTE = 8
ACCESS_METHODS = (("suspended_scaffold", "Suspended scaffold"), ("supported_scaffold", "Supported scaffold"),
                  ("rope_access", "Rope access"), ("boom_lift", "Boom lift"))
ACCESS_LABELS = dict(ACCESS_METHODS)
ACCESS_TYPES = ("FR",)                       # Exterior Restoration: the job code starts FR-
# what the worker app can show for a job; sign in / out is always on and not listed
WORKER_FEATURES = (("photos", "Photos", "Take and upload job photos"),
                   ("drawings", "Drawings & drop plan", "View only"),
                   ("toolbox_talks", "Toolbox talks", "Read and sign"),
                   ("documents", "Job documents", "Permits, site safety plan, insurance: view only"))
WORKER_KEYS = tuple(k for k, _, _ in WORKER_FEATURES)
WORKER_DEFAULT = ("photos",)
PROJECT_TYPE_FOR = {"FR": "facade", "PG": "garage", "IR": "interiors", "IRA": "ira", "OTH": "generic"}
COLUMNS = (("job_number", "INTEGER"), ("color_index", "INTEGER"), ("access_methods", "TEXT"), ("worker_access", "TEXT"),
           ("job_type_other", "TEXT"))   # what an "Other" job is (e.g. Basement excavation)
OTHER_TYPES = ("OTH",)                     # job types described in words instead of picked from the list
OTHER_MAX = 80


def _has_column(conn, table, column) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                                 "AND table_name = ? AND column_name = ?", (table, column)).fetchone())
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone():
        return False
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def legacy_color(code: str) -> int:
    """The color a job had before colors were stored (portal_calendar's old hash)."""
    return sum(ord(c) for c in (code or "")) % OLD_PALETTE


def ensure(conn) -> None:
    """Idempotent: add the columns, the unique number index, then number and color what is missing."""
    if not _has_column(conn, "projects", "project_code"):
        return   # no projects table yet (a fixture that migrates later)
    for col, ddl in COLUMNS:
        if not _has_column(conn, "projects", col):
            conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
    conn.commit()
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_job_number ON projects(job_number)")
    conn.commit()
    backfill(conn)


def backfill(conn) -> dict:
    """Number every job that has no number, oldest first; color every active job that has none."""
    numbered = colored = 0
    todo = [r["project_code"] for r in conn.execute(
        "SELECT project_code FROM projects WHERE job_number IS NULL "
        "ORDER BY CASE WHEN created_at IS NULL THEN 0 ELSE 1 END, created_at, project_code").fetchall()]
    for code in todo:
        try:
            conn.execute("UPDATE projects SET job_number = (SELECT COALESCE(MAX(job_number), 0) + 1 FROM projects) "
                         "WHERE project_code = ? AND job_number IS NULL", (code,))
            conn.commit()
            numbered += 1
        except Exception:   # another worker numbered it at the same moment
            conn.rollback()
    for r in conn.execute("SELECT project_code FROM projects WHERE color_index IS NULL AND COALESCE(status, 'active') = 'active' "
                          "ORDER BY job_number").fetchall():
        c = pick_color(conn, prefer=legacy_color(r["project_code"]))
        conn.execute("UPDATE projects SET color_index = ? WHERE project_code = ? AND color_index IS NULL", (c, r["project_code"]))
        conn.commit()
        colored += 1
    if numbered or colored:
        log.info("job setup backfill: numbered=%s colored=%s", numbered, colored)
    return {"numbered": numbered, "colored": colored}


def colors_in_use(conn, exclude_code: Optional[str] = None) -> dict:
    """color index -> how many active jobs use it."""
    out: dict = {}
    for r in conn.execute("SELECT project_code, color_index FROM projects WHERE color_index IS NOT NULL "
                          "AND COALESCE(status, 'active') = 'active'").fetchall():
        if r["project_code"] != exclude_code:
            out[int(r["color_index"])] = out.get(int(r["color_index"]), 0) + 1
    return out


def pick_color(conn, prefer: Optional[int] = None, exclude_code: Optional[str] = None) -> int:
    """A color no active job uses (the preferred one when free), else the least used."""
    used = colors_in_use(conn, exclude_code)
    if prefer is not None and 0 <= prefer < len(PALETTE) and prefer not in used:
        return prefer
    return min(range(len(PALETTE)), key=lambda i: (used.get(i, 0), i))


def next_job_number(conn) -> int:
    r = conn.execute("SELECT COALESCE(MAX(job_number), 0) + 1 FROM projects").fetchone()
    return int(r[0] or 1)


def _list(raw, allowed) -> Optional[list]:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return [k for k in allowed if isinstance(v, list) and k in v]


def access_methods(row: dict) -> Optional[list]:
    return _list(row.get("access_methods"), ACCESS_LABELS)


def worker_access(row: dict) -> list:
    v = _list(row.get("worker_access"), WORKER_KEYS)
    return list(WORKER_DEFAULT) if v is None else v


def access_labels(row: dict) -> list:
    return [ACCESS_LABELS[k] for k in (access_methods(row) or [])]


def color_hex(row: dict) -> str:
    c = row.get("color_index")
    return PALETTE[int(c) % len(PALETTE)] if c is not None else PALETTE[legacy_color(row.get("project_code"))]


def color_ink(row: dict) -> str:
    """The job color to use for text and accents: the dark shade for a light pastel, else the color."""
    return INK.get(color_of(row), color_hex(row))


def color_of(row: dict) -> int:
    c = row.get("color_index")
    return int(c) % len(PALETTE) if c is not None else legacy_color(row.get("project_code"))


def _clean_list(v, allowed, label) -> list:
    if not isinstance(v, list):
        raise ValueError(f"{label} must be a list")
    bad = [x for x in v if x not in allowed]
    if bad:
        raise ValueError(f"unknown {label}: {', '.join(map(str, bad))[:80]}")
    return [k for k in allowed if k in v]


def validate(payload: dict, est_type: Optional[str]) -> dict:
    """Checked values to store; keys only for what the payload carries."""
    p = payload or {}
    out = {}
    if p.get("color_index") not in (None, ""):
        try:
            c = int(p.get("color_index"))
        except (TypeError, ValueError):
            raise ValueError("pick a color")
        if not 0 <= c < len(PALETTE):
            raise ValueError("pick a color")
        out["color_index"] = c
    if "access_methods" in p and p.get("access_methods") is not None:
        methods = _clean_list(p.get("access_methods"), ACCESS_LABELS, "access method")
        if methods and (est_type or "").upper() not in ACCESS_TYPES:
            raise ValueError("an access method is only set on an Exterior Restoration job")
        out["access_methods"] = json.dumps(methods)
    if "worker_access" in p and p.get("worker_access") is not None:
        out["worker_access"] = json.dumps(_clean_list(p.get("worker_access"), WORKER_KEYS, "worker app feature"))
    if (est_type or "").upper() in OTHER_TYPES and "job_type_other" in p:
        text = " ".join(str(p.get("job_type_other") or "").split())
        if not text:
            raise ValueError("describe the job type, for example Basement excavation")
        if len(text) > OTHER_MAX:
            raise ValueError(f"keep the job type under {OTHER_MAX} characters")
        out["job_type_other"] = text
    return out


def est_type_of(code: str) -> str:
    return (code or "").split("-")[0].upper()


def assign_new(conn, code: str, est_type: str, payload: Optional[dict] = None) -> dict:
    """A job just created through onboarding: its number, its color (the chosen one, else a free
    one), its type, access methods and worker access."""
    if not _has_column(conn, "projects", "job_number") or not _has_column(conn, "projects", "job_type_other"):
        ensure(conn)   # a database created after boot
    vals = validate(payload or {}, est_type)
    vals.setdefault("color_index", pick_color(conn, exclude_code=code))
    vals.setdefault("worker_access", json.dumps(list(WORKER_DEFAULT)))
    if est_type.upper() in ACCESS_TYPES:
        vals.setdefault("access_methods", json.dumps([]))
    pt = PROJECT_TYPE_FOR.get(est_type.upper())
    sets = ", ".join(f"{k} = ?" for k in vals)
    conn.execute(f"UPDATE projects SET {sets} WHERE project_code = ?", (*vals.values(), code))
    conn.commit()
    if pt:
        try:   # project_type has a CHECK constraint on older databases: never fail a create over it
            conn.execute("UPDATE projects SET project_type = ? WHERE project_code = ?", (pt, code))
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.info("project type not set on %s: %s", code, type(e).__name__)
    for _ in range(3):   # the number, race-safe against the unique index
        try:
            conn.execute("UPDATE projects SET job_number = (SELECT COALESCE(MAX(job_number), 0) + 1 FROM projects) "
                         "WHERE project_code = ? AND job_number IS NULL", (code,))
            conn.commit()
            break
        except Exception:
            conn.rollback()
    return setup(conn, code)


def setup(conn, code: str) -> Optional[dict]:
    try:
        r = conn.execute("SELECT project_code, name, status, job_number, color_index, access_methods, worker_access, job_type_other "
                         "FROM projects WHERE project_code = ?", (code,)).fetchone()
    except Exception:   # a database from before the job_type_other column
        conn.rollback()
        r = conn.execute("SELECT project_code, name, status, job_number, color_index, access_methods, worker_access "
                         "FROM projects WHERE project_code = ?", (code,)).fetchone()
    if not r:
        return None
    r = dict(r)
    et = est_type_of(code)
    import estimates
    other = (r.get("job_type_other") or "").strip() if et in OTHER_TYPES else ""
    return {"project_code": code, "job_number": r.get("job_number"), "color_index": color_of(r), "color": color_hex(r),
            "est_type": et, "job_type_other": other or None,
            "job_type_label": other or estimates.EST_TYPES.get(et, et), "other_applies": et in OTHER_TYPES,
            "access_applies": et in ACCESS_TYPES,
            "access_methods": access_methods(r), "access_labels": access_labels(r), "worker_access": worker_access(r)}


def update(conn, code: str, payload: dict, *, actor=None) -> dict:
    if not conn.execute("SELECT 1 FROM projects WHERE project_code = ?", (code,)).fetchone():
        raise LookupError("project not found")
    vals = validate(payload, est_type_of(code))
    if vals:
        sets = ", ".join(f"{k} = ?" for k in vals)
        conn.execute(f"UPDATE projects SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE project_code = ?", (*vals.values(), code))
        conn.commit()
        log.info("job setup updated %s fields=%s by=%s", code, ",".join(sorted(vals)), actor)
    return setup(conn, code)


def meta(conn, exclude_code: Optional[str] = None) -> dict:
    used = colors_in_use(conn, exclude_code)
    return {"palette": list(PALETTE), "used": {str(k): v for k, v in used.items()},
            "suggested_color": pick_color(conn, exclude_code=exclude_code), "next_job_number": next_job_number(conn),
            "access_methods": [{"key": k, "label": l} for k, l in ACCESS_METHODS], "access_types": list(ACCESS_TYPES),
            "other_types": list(OTHER_TYPES), "other_max": OTHER_MAX,
            "worker_features": [{"key": k, "label": l, "hint": h} for k, l, h in WORKER_FEATURES],
            "worker_default": list(WORKER_DEFAULT)}


# ---------------------------------------------------------------- routes
def _with_conn(fn):
    conn = db_layer.connect()
    try:
        return fn(conn)
    finally:
        conn.close()


@requires_company
def _api_meta():
    code = (request.args.get("project") or "").strip() or None
    return jsonify({"data": _with_conn(lambda c: meta(c, code))})


@requires_company
def _api_get(project_code):
    d = _with_conn(lambda c: setup(c, project_code))
    return jsonify({"data": d}) if d else (jsonify({"error": "project not found"}), 404)


@requires_company
def _api_put(project_code):
    body = request.get_json(silent=True) or {}
    actor = (current_user() or {}).get("id")
    try:
        return jsonify({"data": _with_conn(lambda c: update(c, project_code, body, actor=actor))})
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


def register(app) -> None:
    conn = db_layer.connect()
    try:
        ensure(conn)
    except Exception as e:   # never block boot on a fixture that migrates later
        log.warning("job setup ensure skipped: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    app.add_url_rule("/api/admin/projects/setup-meta", "project_setup_meta", _api_meta, methods=["GET"])
    app.add_url_rule("/api/admin/projects/<project_code>/setup", "project_setup_get", _api_get, methods=["GET"])
    app.add_url_rule("/api/admin/projects/<project_code>/setup", "project_setup_put", _api_put, methods=["PUT"])
