"""#273 — Estimate/bid tracking: every inquiry the company bids, any type, from
intake to approved/lost, with document attachments, CRM-linked, and one-click
conversion of an approved estimate into Active Projects.

PLACEMENT (operator-mandated): a company-console section ('estimates' in
access.SECTION_ACCESS), admin/c_suite ONLY — CRM-class gating. Amounts and payment
status never touch the field-reachable project dashboard; every endpoint here is
@requires_section('estimates') (server-enforced — hiding nav is never the control).

PRINCIPLES
  * CODE SERIES {TYPE}-{BORO}-{NNN} (e.g. IRA-BX-001). est_type catalog IN CODE
    (EST_TYPES — extensible); borough enum MN|BX|BK|QN|SI. seq allocated NUMERICALLY
    per (type, borough) — CAST/int math per the CLAUDE.md zero-padded-ID rule (the
    E-00013 lesson). The series is seeded from BOTH existing estimates AND existing
    projects.project_code rows in the same series (FR-BX-001 predates this module),
    so a converted code can never collide with a pre-existing project.
  * STATUS PIPELINE intake|scoping|submitted|approved|lost|converted with SERVER-
    VALIDATED transitions; 'approved' requires final_amount + qb_estimate_ref
    (QuickBooks remains the pricing tool — we record the reference + final number
    only); 'converted' is reachable ONLY via the convert endpoint, double-click-safe.
  * DOCUMENTS: file on disk under data_room/estimate_docs/<code>/<uuid>.<ext>;
    file_path NEVER in JSON — served ONLY by the gated by-id route (#229/#247
    pattern). Nothing hard-deletes once documents exist.
  * CRM-LINKED, never duplicated: client_org_id -> crm_organization (#266). Every
    status change + conversion writes crm_activity (function_tag 'sales') on the
    linked org — the CRM timeline, and the future company agent's filing target.
  * IRA extension row (estimate_ira) exists only for est_type='IRA' — the additive
    pattern for future type extensions.

Dates LOCAL (YYYY-MM-DD / naive local timestamps — CLAUDE.md dates rule). All SQL
parameterized through the caller's db_layer connection — identical on SQLite
(default/production) and Postgres (the dual-backend gate).
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from pathlib import Path

from flask import jsonify, request, send_file

import crm
from auth import _db, current_user, requires_section

SCRIPT_DIR = Path(__file__).resolve().parent
import ssc_paths  # #287
_DOC_BASE = ssc_paths.under_root("data_room", "estimate_docs")   # #287

# ---- catalogs (in code, extensible — the operator adds a type here, no schema change) ----
EST_TYPES = {
    "IRA": "Industrial Rope Access Inspection",
    "FR":  "Facade Restoration",
    "IR":  "Interior Repair",
    "PG":  "Parking Garage",   # #276 — the fourth division's code series (blueprint §1)
}
BOROUGHS = ("MN", "BX", "BK", "QN", "SI")
# #276 — division derivation (obvious mappings; editable per lead)
DIVISION_OF_TYPE = {"IRA": "rope_access", "FR": "facade", "IR": "interior",
                    "PG": "parking_garage"}
STATUSES = ("intake", "scoping", "submitted", "approved", "lost", "converted")

# Server-validated pipeline: one step forward, one step back, lost from any live
# stage, reopen from lost. 'converted' is TERMINAL and reachable ONLY via /convert.
_TRANSITIONS = {
    "intake":    {"scoping", "lost"},
    "scoping":   {"submitted", "lost", "intake"},
    "submitted": {"approved", "lost", "scoping"},
    "approved":  {"submitted", "lost"},
    "lost":      {"intake", "scoping"},
    "converted": set(),
}

# #273 attachment categories; #274 extends with the IRA artifact slots (cd5, coi).
DOC_CATEGORIES = ("bid_file", "drawing", "addendum", "roof_plan", "contract",
                  "cd5", "coi", "proposal", "other")   # #307 — generated proposal PDFs

_DOC_EXT_TYPE = {
    '.pdf': ('PDF', 'application/pdf'),
    '.jpg': ('JPG', 'image/jpeg'), '.jpeg': ('JPG', 'image/jpeg'),
    '.png': ('PNG', 'image/png'),
    '.heic': ('HEIC', 'image/heic'), '.heif': ('HEIC', 'image/heif'),
    '.xlsx': ('XLSX', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
}

_CODE_RE_TMPL = r"^%s-%s-(\d+)$"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _uid():
    return (current_user() or {}).get("id")


def _valid_date(s):
    """LOCAL YYYY-MM-DD or None; raises ValueError on garbage."""
    s = (s or "").strip()
    if not s:
        return None
    datetime.strptime(s, "%Y-%m-%d")
    return s


def _has_276(conn) -> bool:
    """True when the #276 lead-expansion columns exist. Keeps this module working
    against a #273-only schema (standalone smoke DBs) — same adaptive pattern as
    add_document's expiry_date."""
    from apply_crm_266 import _columns
    return "division" in _columns(conn, "estimate")


_DIVISIONS = ("facade", "rope_access", "interior", "parking_garage")
_INQUIRY_KINDS = ("bid", "po", "undetermined")
_RA_SUBTYPES = ("inspection", "work")


def _validate_lead_fields(*, division=None, ra_subtype=None, inquiry_kind=None,
                          bid_due_date=None):
    """#276 lead-field vocabulary checks (ValueError -> 400 at the endpoints)."""
    if division and division not in _DIVISIONS:
        raise ValueError(f"unknown division: {division}")
    st = (ra_subtype or "").strip().lower()
    if st and st not in _RA_SUBTYPES:
        raise ValueError(f"unknown ra_subtype: {st}")
    if st and division != "rope_access":
        raise ValueError("ra_subtype applies only to the rope_access division")
    k = (inquiry_kind or "").strip().lower()
    if k and k not in _INQUIRY_KINDS:
        raise ValueError(f"unknown inquiry_kind: {k}")
    _valid_date(bid_due_date)   # raises on garbage


# ============================ allocation (the E-00013 rule) ============================

def next_seq(conn, est_type: str, borough: str) -> int:
    """Next NUMERIC sequence for the (type, borough) series. MAX is taken over BOTH
    estimate.seq (CAST — never lexicographic) AND any pre-existing projects whose
    project_code already follows the series (FR-BX-001 predates this module); parsing
    of project codes happens in Python so a stray non-numeric tail can never crash the
    Postgres CAST. Deleting the highest reverts the number (MAX+1 — same behavior as
    worker ids)."""
    row = conn.execute(
        "SELECT MAX(CAST(seq AS INTEGER)) AS m FROM estimate WHERE est_type=? AND borough=?",
        (est_type, borough)).fetchone()
    best = row["m"] or 0
    pat = re.compile(_CODE_RE_TMPL % (re.escape(est_type), re.escape(borough)))
    for r in conn.execute("SELECT project_code FROM projects WHERE project_code LIKE ?",
                          (f"{est_type}-{borough}-%",)).fetchall():
        m = pat.match(r["project_code"] or "")
        if m:
            best = max(best, int(m.group(1)))
    return best + 1


def make_code(est_type: str, borough: str, seq: int) -> str:
    return f"{est_type}-{borough}-{seq:03d}"


# ============================ core logic (shared with the smoke) ============================

def create_estimate(conn, *, est_type, borough, client_org_id, contact_id=None,
                    building_address=None, notes=None, created_by=None,
                    division=None, ra_subtype=None, inquiry_kind=None,
                    bid_due_date=None, in_stage_since=None) -> dict:
    est_type = (est_type or "").strip().upper()
    borough = (borough or "").strip().upper()
    if est_type not in EST_TYPES:
        raise ValueError(f"unknown est_type: {est_type or '(none)'}")
    if borough not in BOROUGHS:
        raise ValueError(f"unknown borough: {borough or '(none)'}")
    org = crm.get_org(conn, client_org_id)
    if not org:
        raise ValueError("client_org_id does not exist")
    if contact_id is not None:
        c = crm.get_contact(conn, contact_id)
        if not c:
            raise ValueError("contact_id does not exist")
        if c.get("org_id") and c["org_id"] != client_org_id:
            raise ValueError("contact belongs to a different organization")
    seq = next_seq(conn, est_type, borough)
    code = make_code(est_type, borough, seq)
    now = _now()
    cols = ["code", "est_type", "borough", "seq", "client_org_id", "contact_id",
            "building_address", "status", "notes", "created_by", "created_at",
            "updated_at", "status_changed_at"]
    vals = [code, est_type, borough, seq, client_org_id, contact_id,
            (building_address or "").strip() or None, "intake",
            (notes or "").strip() or None, created_by, now, now, now]
    if _has_276(conn):
        # #276 lead expansion — division derived-defaulted from the type, editable;
        # kind defaults undetermined; the BACKFILL date makes aging TRUE from day one.
        division = (division or "").strip().lower() or DIVISION_OF_TYPE.get(est_type)
        _validate_lead_fields(division=division, ra_subtype=ra_subtype,
                              inquiry_kind=inquiry_kind, bid_due_date=bid_due_date)
        since = _valid_date(in_stage_since)
        if since:
            vals[cols.index("status_changed_at")] = since
        cols += ["division", "ra_subtype", "inquiry_kind", "bid_due_date"]
        vals += [division,
                 ((ra_subtype or "").strip().lower() or
                  ("inspection" if (division == "rope_access" and est_type == "IRA") else None))
                 if division == "rope_access" else None,
                 (inquiry_kind or "").strip().lower() or "undetermined",
                 _valid_date(bid_due_date)]
    conn.execute(
        f"INSERT INTO estimate ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        vals)
    conn.commit()
    new_id = conn.execute("SELECT id FROM estimate WHERE code=?", (code,)).fetchone()["id"]
    try:
        import buildings as _buildings   # #304 — every estimate with an address belongs to a building
        _buildings.autolink_estimate(conn, new_id)
    except Exception:
        pass
    if est_type == "IRA":   # the IRA-only extension row exists from day one
        if not conn.execute("SELECT 1 FROM estimate_ira WHERE estimate_id=?", (new_id,)).fetchone():
            conn.execute("INSERT INTO estimate_ira (estimate_id) VALUES (?)", (new_id,))
            conn.commit()
    crm.add_activity(conn, entity_type="organization", entity_id=client_org_id,
                     activity_type="system", author_user_id=created_by, function_tag="sales",
                     summary=f"Estimate {code} opened (intake) — {EST_TYPES[est_type]}")
    return get_estimate(conn, code=code)


def get_estimate(conn, est_id=None, code=None):
    if code is not None:
        r = conn.execute("SELECT * FROM estimate WHERE code=?", (code,)).fetchone()
    else:
        r = conn.execute("SELECT * FROM estimate WHERE id=?", (est_id,)).fetchone()
    return dict(r) if r else None


def allowed_transitions(status: str):
    return sorted(_TRANSITIONS.get(status, set()))


def change_status(conn, est_id, new_status, *, on_date=None, final_amount=None,
                  qb_estimate_ref=None, actor_user_id=None) -> dict:
    """Apply a server-validated status transition. Entering 'approved' REQUIRES
    final_amount + qb_estimate_ref (either already on the row or provided here).
    submitted/decided dates stamp LOCAL (user-picked date honored). Raises ValueError
    on an illegal jump (the endpoint maps it to 400)."""
    est = get_estimate(conn, est_id)
    if not est:
        raise LookupError("estimate not found")
    new_status = (new_status or "").strip().lower()
    old = est["status"]
    if new_status not in STATUSES:
        raise ValueError(f"unknown status: {new_status}")
    if new_status == "converted" or old == "converted":
        raise ValueError("conversion happens via the convert endpoint, never a status jump")
    if new_status not in _TRANSITIONS.get(old, set()):
        raise ValueError(f"illegal transition: {old} -> {new_status}")
    on_date = _valid_date(on_date) or _today()

    sets = ["status=?", "status_changed_at=?", "updated_at=?"]
    params = [new_status, _now(), _now()]
    # values may arrive with the transition (the approve modal carries them)
    if final_amount is not None and str(final_amount).strip() != "":
        amt = float(final_amount)
        if amt < 0:
            raise ValueError("final_amount must be >= 0")
        sets.append("final_amount=?"); params.append(amt)
        est["final_amount"] = amt
    if qb_estimate_ref is not None and str(qb_estimate_ref).strip() != "":
        sets.append("qb_estimate_ref=?"); params.append(str(qb_estimate_ref).strip())
        est["qb_estimate_ref"] = str(qb_estimate_ref).strip()

    if new_status == "approved":
        if est.get("final_amount") in (None, "") or not (est.get("qb_estimate_ref") or "").strip():
            raise ValueError("approval requires final_amount and qb_estimate_ref")
    if new_status == "submitted":
        sets.append("submitted_date=?"); params.append(on_date)
    if new_status in ("approved", "lost"):
        sets.append("decided_date=?"); params.append(on_date)
    if old == "lost" and new_status in ("intake", "scoping"):
        sets.append("decided_date=?"); params.append(None)   # reopened — decision cleared

    params.append(est_id)
    conn.execute(f"UPDATE estimate SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    if new_status == "scoping" and _has_276(conn):
        # #276 MACRO/MICRO contract — entering estimating initializes the sub-machine
        # at 'received' (once; a submitted->scoping pull-back keeps its prior stage).
        conn.execute(
            "UPDATE estimate SET est_stage=COALESCE(est_stage,'received'), "
            "est_stage_changed_at=COALESCE(est_stage_changed_at, ?) WHERE id=?",
            (_now(), est_id))
        conn.commit()
    crm.add_activity(conn, entity_type="organization", entity_id=est["client_org_id"],
                     activity_type="system", author_user_id=actor_user_id, function_tag="sales",
                     summary=f"Estimate {est['code']}: {old} → {new_status}")
    return get_estimate(conn, est_id)


def convert_to_project(conn, est_id, *, actor_user_id=None) -> dict:
    """One-click conversion of an APPROVED estimate into Active Projects:
    project_code = estimate code, name = building address, status active. Stamps
    converted_project_code + status 'converted' and writes the activity row.
    DOUBLE-CLICK-SAFE: a second call returns the already-converted result instead of
    creating anything. The converted project is the lifecycle anchor ONLY (#263
    close/reopen applies); its project dashboard gains no new sections."""
    est = get_estimate(conn, est_id)
    if not est:
        raise LookupError("estimate not found")
    code = est["code"]
    if est["status"] == "converted" and est.get("converted_project_code"):
        return {"already": True, "project_code": est["converted_project_code"],
                "estimate": get_estimate(conn, est_id)}
    if est["status"] != "approved":
        raise ValueError("only an approved estimate can convert")
    existing = conn.execute("SELECT project_code FROM projects WHERE project_code=?",
                            (code,)).fetchone()
    if existing:
        # A pre-existing project with this exact code that we did NOT create — the
        # series seeding makes this near-impossible; refuse rather than adopt.
        raise ValueError(f"project {code} already exists")
    now = _now()
    conn.execute(
        "INSERT INTO projects (project_code, name, address, status, client_org_id, "
        "created_at, updated_at) VALUES (?,?,?, 'active', ?,?,?)",
        (code, est.get("building_address") or code, est.get("building_address"),
         est["client_org_id"], now, now))
    conn.execute(
        "UPDATE estimate SET status='converted', converted_project_code=?, "
        "status_changed_at=?, updated_at=? WHERE id=?", (code, now, now, est_id))
    try:
        import buildings as _buildings   # #304 — the new project inherits the estimate's building
        _buildings.autolink_project(conn, code)
    except Exception:
        pass
    conn.commit()
    crm.add_activity(conn, entity_type="organization", entity_id=est["client_org_id"],
                     activity_type="system", author_user_id=actor_user_id, function_tag="sales",
                     summary=f"Estimate {code} converted to project {code}")
    if est["est_type"] == "IRA":
        # #274 — a converted IRA estimate is an inspection JOB: create its checklist row
        # immediately (the ira list also self-heals, so pre-#274 conversions backfill).
        try:
            import ira
            ira.ensure_job(conn, code, est_id)
        except ImportError:
            pass   # #273-only deployment — the #274 migration backfills later
    return {"already": False, "project_code": code, "estimate": get_estimate(conn, est_id)}


# ============================ payload shaping (NO paths, ever) ============================

def _age_days(row) -> int:
    anchor = (row.get("status_changed_at") or row.get("created_at") or "")[:10]
    try:
        return max(0, (date.today() - date.fromisoformat(anchor)).days)
    except Exception:
        return 0


def est_public(row, org_name=None, contact_name=None) -> dict:
    d = dict(row)
    return {
        "id": row["id"], "code": row["code"], "est_type": row["est_type"],
        "type_label": EST_TYPES.get(row["est_type"], row["est_type"]),
        "borough": row["borough"], "seq": row["seq"], "status": row["status"],
        "client_org_id": row["client_org_id"], "org_name": org_name,
        "contact_id": row["contact_id"], "contact_name": contact_name,
        "building_address": row["building_address"],
        "qb_estimate_ref": row["qb_estimate_ref"], "final_amount": row["final_amount"],
        "submitted_date": row["submitted_date"], "decided_date": row["decided_date"],
        "converted_project_code": row["converted_project_code"], "notes": row["notes"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "status_changed_at": row["status_changed_at"], "age_days": _age_days(dict(row)),
        # #276 lead expansion (None on a pre-276 schema)
        "division": d.get("division"), "ra_subtype": d.get("ra_subtype"),
        "inquiry_kind": d.get("inquiry_kind"), "bid_due_date": d.get("bid_due_date"),
        "est_stage": d.get("est_stage"), "est_stage_changed_at": d.get("est_stage_changed_at"),
        "walkthrough_date": d.get("walkthrough_date"),
        "assigned_estimator": d.get("assigned_estimator"),
    }


def doc_public(d) -> dict:
    """PII/path-safe shape — NO file_path. file_url is the GATED route, not a path.
    expiry_date rides along when present (#274 — the COI's #271-style pill source)."""
    return {
        "id": d["id"], "estimate_id": d["estimate_id"], "category": d["category"],
        "title": d["title"], "doc_type": d["doc_type"], "file_name": d["file_name"],
        "file_size": d["file_size"], "mime": d["mime"], "uploaded_at": d["uploaded_at"],
        "expiry_date": d.get("expiry_date"),
        "file_url": f"/api/estimates/documents/{d['id']}/file",
    }


# ============================ documents (disk + gated serve) ============================

def _doc_save_file(code, fs):
    """Save an upload under data_room/estimate_docs/<code>/<uuid>.<ext>. The stored
    name is a uuid — the original filename never touches the path (#229 pattern)."""
    ext = Path(fs.filename or "document").suffix.lower()
    if ext not in _DOC_EXT_TYPE:
        raise ValueError(f"unsupported file type: {ext or '(none)'}")
    base = _DOC_BASE.resolve()
    ddir = _DOC_BASE / code
    if not ddir.resolve().is_relative_to(base):
        raise ValueError("invalid path")
    ddir.mkdir(parents=True, exist_ok=True)
    fpath = ddir / (uuid.uuid4().hex + ext)
    fs.save(str(fpath))
    doc_type, mime = _DOC_EXT_TYPE[ext]
    return fpath, doc_type, mime, fpath.stat().st_size


def add_document(conn, est_id, fs, *, category, title=None, uploaded_by=None,
                 expiry_date=None) -> int:
    est = get_estimate(conn, est_id)
    if not est:
        raise LookupError("estimate not found")
    category = (category or "").strip().lower()
    if category not in DOC_CATEGORIES:
        raise ValueError(f"invalid category: {category or '(none)'}")
    fpath, doc_type, mime, size = _doc_save_file(est["code"], fs)
    try:
        file_name = Path(fs.filename or "document").name
        cols = ["estimate_id", "category", "title", "doc_type", "file_path", "file_name",
                "file_size", "mime", "uploaded_by", "uploaded_at"]
        vals = [est_id, category, (title or "").strip() or file_name, doc_type,
                ssc_paths.store_rel(fpath), file_name, size, mime, uploaded_by, _now()]   # #287
        # #274 — expiry_date column exists once apply_ira_274 ran; include it only when
        # provided so a #273-only schema (older gate DB) keeps working unchanged.
        if expiry_date:
            cols.append("expiry_date")
            vals.append(expiry_date)
        conn.execute(
            f"INSERT INTO estimate_document ({', '.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})", vals)
        conn.commit()
        r = conn.execute("SELECT MAX(id) AS m FROM estimate_document").fetchone()
        return r["m"]
    except Exception:
        try:
            fpath.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def list_documents(conn, est_id) -> list:
    return [doc_public(dict(d)) for d in conn.execute(
        "SELECT * FROM estimate_document WHERE estimate_id=? ORDER BY uploaded_at DESC, id DESC",
        (est_id,)).fetchall()]


# ============================ endpoints ============================

@requires_section('estimates')
def _api_meta():
    """GET /api/estimates/meta — the in-code catalogs for the board's selects."""
    return jsonify({"data": {
        "types": [{"code": k, "label": v} for k, v in EST_TYPES.items()],
        "boroughs": list(BOROUGHS),
        "statuses": list(STATUSES),
        "doc_categories": list(DOC_CATEGORIES),
    }})


@requires_section('estimates')
def _api_list():
    """GET /api/estimates[?status=...&est_type=...&borough=...&search=...] — the board
    payload: every estimate with org name + age-in-stage; the UI groups by status."""
    conn = _db()
    try:
        where, params = [], []
        for key, col in (("status", "e.status"), ("est_type", "e.est_type"),
                         ("borough", "e.borough")):
            v = (request.args.get(key) or "").strip()
            if v:
                where.append(f"{col}=?")
                params.append(v.upper() if key != "status" else v.lower())
        s = (request.args.get("search") or "").strip().lower()
        if s:
            where.append("(LOWER(e.code) LIKE ? OR LOWER(COALESCE(e.building_address,'')) LIKE ? "
                         "OR LOWER(COALESCE(o.name,'')) LIKE ?)")
            params += [f"%{s}%"] * 3
        sql = ("SELECT e.*, o.name AS org_name, c.full_name AS contact_name "
               "FROM estimate e JOIN crm_organization o ON o.id=e.client_org_id "
               "LEFT JOIN crm_contact c ON c.id=e.contact_id")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.est_type, e.borough, CAST(e.seq AS INTEGER)"
        rows = conn.execute(sql, params).fetchall()
        out = [est_public(dict(r), r["org_name"], r["contact_name"]) for r in rows]
        if _has_276(conn):
            # #276 — the board cards carry the same pipeline strip + SLA aging as the
            # workspace: ONE mapping source (estimating.pipe_state), imported lazily
            # (estimating imports this module at top level).
            import estimating as _estimating
            for r, raw in zip(out, rows):
                d = dict(raw)
                r["pipe"] = _estimating.pipe_state(d)
                r["age_days"] = _estimating.lead_age_days(d)
                r["overdue"] = _estimating.lead_overdue(d)
        counts = {}
        for r in out:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return jsonify({"data": {"estimates": out, "counts": counts}})
    finally:
        conn.close()


@requires_section('estimates')
def _api_create():
    d = request.get_json(silent=True) or {}
    conn = _db()
    try:
        est = create_estimate(
            conn, est_type=d.get("est_type"), borough=d.get("borough"),
            client_org_id=d.get("client_org_id"), contact_id=d.get("contact_id"),
            building_address=d.get("building_address"), notes=d.get("notes"),
            created_by=_uid(),
            # #276 lead expansion + the honest-backfill date (create/edit only —
            # this endpoint is 'estimates' = admin/c_suite, so backfill is theirs)
            division=d.get("division"), ra_subtype=d.get("ra_subtype"),
            inquiry_kind=d.get("inquiry_kind"), bid_due_date=d.get("bid_due_date"),
            in_stage_since=d.get("in_stage_since"))
        return jsonify({"data": est_public(est)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()


@requires_section('estimating')
def _api_detail(est_id):
    # #276 — the estimator works this lead too, so the section widened from
    # 'estimates' to 'estimating' (admin/c_suite unchanged — they're in both).
    conn = _db()
    try:
        est = get_estimate(conn, est_id)
        if not est:
            return jsonify({"error": "not found"}), 404
        org = crm.get_org(conn, est["client_org_id"])
        contact = crm.get_contact(conn, est["contact_id"]) if est["contact_id"] else None
        ira = None
        if est["est_type"] == "IRA":
            r = conn.execute("SELECT * FROM estimate_ira WHERE estimate_id=?", (est_id,)).fetchone()
            ira = dict(r) if r else None
        # the CRM strip: the linked client's recent timeline — COMPANY ROLES ONLY.
        # The estimator sees the lead, never the org's cross-estimate CRM breadth
        # (blueprint §5: no CRM tab, no company timelines).
        import access as _access
        acts = []
        if _access.can_access("estimates", (current_user() or {}).get("role")):
            acts = crm.list_activity(conn, entity_type="organization",
                                     entity_id=est["client_org_id"], limit=10)
            acts = [{"id": a["id"], "activity_type": a["activity_type"], "summary": a["summary"],
                     "occurred_at": a["occurred_at"], "function_tag": a.get("function_tag")}
                    for a in acts]
        return jsonify({"data": {
            "estimate": est_public(est, (org or {}).get("name"),
                                   (contact or {}).get("full_name")),
            "ira": ira,
            "documents": list_documents(conn, est_id),
            "client_activity": acts,
            "allowed_transitions": allowed_transitions(est["status"]),
            "convertible": est["status"] == "approved",
        }})
    finally:
        conn.close()


@requires_section('estimates')
def _api_update(est_id):
    """PUT — edit the working fields. Amount/ref may be set here too (they gate the
    approve transition). A converted estimate is read-only."""
    d = request.get_json(silent=True) or {}
    conn = _db()
    try:
        est = get_estimate(conn, est_id)
        if not est:
            return jsonify({"error": "not found"}), 404
        if est["status"] == "converted":
            return jsonify({"error": "a converted estimate is read-only"}), 409
        sets, params = [], []
        if "client_org_id" in d and d["client_org_id"] != est["client_org_id"]:
            if not crm.get_org(conn, d["client_org_id"]):
                return jsonify({"error": "client_org_id does not exist"}), 400
            sets.append("client_org_id=?"); params.append(d["client_org_id"])
            est["client_org_id"] = d["client_org_id"]
        if "contact_id" in d:
            cid = d["contact_id"]
            if cid is not None:
                c = crm.get_contact(conn, cid)
                if not c:
                    return jsonify({"error": "contact_id does not exist"}), 400
                if c.get("org_id") and c["org_id"] != est["client_org_id"]:
                    return jsonify({"error": "contact belongs to a different organization"}), 400
            sets.append("contact_id=?"); params.append(cid)
        for k in ("building_address", "qb_estimate_ref", "notes"):
            if k in d:
                sets.append(f"{k}=?")
                params.append((str(d[k]).strip() or None) if d[k] is not None else None)
        if _has_276(conn):
            try:
                div = (d.get("division") or "").strip().lower() or est.get("division")
                _validate_lead_fields(
                    division=d.get("division") and div,
                    ra_subtype=d.get("ra_subtype"),
                    inquiry_kind=d.get("inquiry_kind"),
                    bid_due_date=d.get("bid_due_date"))
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            if "division" in d and d["division"]:
                sets.append("division=?"); params.append(div)
                if div != "rope_access":
                    sets.append("ra_subtype=?"); params.append(None)
            if "ra_subtype" in d:
                sets.append("ra_subtype=?")
                params.append((d.get("ra_subtype") or "").strip().lower() or None)
            if "inquiry_kind" in d and d["inquiry_kind"]:
                sets.append("inquiry_kind=?")
                params.append(str(d["inquiry_kind"]).strip().lower())
            if "bid_due_date" in d:
                sets.append("bid_due_date=?")
                params.append((str(d["bid_due_date"]).strip() or None) if d["bid_due_date"] else None)
            since = (d.get("in_stage_since") or "").strip()
            if since:
                # #276 HONEST BACKFILL — the paper backlog enters with TRUE aging:
                # both the macro anchor and (when the sub-machine is live) the stage
                # anchor move to the stated LOCAL date. admin/c_suite only (this
                # endpoint's section), never reachable from the estimator queue.
                try:
                    datetime.strptime(since, "%Y-%m-%d")
                except ValueError:
                    return jsonify({"error": "in_stage_since must be YYYY-MM-DD"}), 400
                sets.append("status_changed_at=?"); params.append(since)
                if est.get("est_stage"):
                    sets.append("est_stage_changed_at=?"); params.append(since)
        if "final_amount" in d:
            v = d["final_amount"]
            if v in (None, ""):
                sets.append("final_amount=?"); params.append(None)
            else:
                try:
                    amt = float(v)
                except (TypeError, ValueError):
                    return jsonify({"error": "final_amount must be a number"}), 400
                if amt < 0:
                    return jsonify({"error": "final_amount must be >= 0"}), 400
                sets.append("final_amount=?"); params.append(amt)
        if sets:
            sets.append("updated_at=?"); params.append(_now())
            params.append(est_id)
            conn.execute(f"UPDATE estimate SET {', '.join(sets)} WHERE id=?", params)
            conn.commit()
        return jsonify({"data": est_public(get_estimate(conn, est_id))})
    finally:
        conn.close()


@requires_section('estimates')
def _api_delete(est_id):
    """DELETE — mistake correction ONLY. Refused once documents exist (nothing
    hard-deletes then) or once converted. MAX+1 allocation means deleting the highest
    seq frees its number — same accepted behavior as worker ids."""
    conn = _db()
    try:
        est = get_estimate(conn, est_id)
        if not est:
            return jsonify({"error": "not found"}), 404
        ndocs = conn.execute("SELECT COUNT(*) AS n FROM estimate_document WHERE estimate_id=?",
                             (est_id,)).fetchone()["n"]
        if ndocs:
            return jsonify({"error": "estimate has documents — nothing hard-deletes once documents exist"}), 409
        if est["status"] == "converted":
            return jsonify({"error": "a converted estimate cannot be deleted"}), 409
        conn.execute("DELETE FROM estimate_ira WHERE estimate_id=?", (est_id,))
        conn.execute("DELETE FROM estimate WHERE id=?", (est_id,))
        conn.commit()
        return jsonify({"data": {"deleted": est_id}})
    finally:
        conn.close()


@requires_section('estimates')
def _api_status(est_id):
    d = request.get_json(silent=True) or {}
    conn = _db()
    try:
        est = change_status(conn, est_id, d.get("status"), on_date=d.get("date"),
                            final_amount=d.get("final_amount"),
                            qb_estimate_ref=d.get("qb_estimate_ref"),
                            actor_user_id=_uid())
        return jsonify({"data": est_public(est)})
    except LookupError:
        return jsonify({"error": "not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()


@requires_section('estimates')
def _api_convert(est_id):
    conn = _db()
    try:
        res = convert_to_project(conn, est_id, actor_user_id=_uid())
        return jsonify({"data": {"already": res["already"], "project_code": res["project_code"],
                                 "estimate": est_public(res["estimate"])}})
    except LookupError:
        return jsonify({"error": "not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()


@requires_section('estimating')
def _api_ira_put(est_id):
    """PUT /api/estimates/<id>/ira — the IRA-only panel (400 for other types).
    #276: 'estimating' section — the estimator fills scope numbers too."""
    d = request.get_json(silent=True) or {}
    conn = _db()
    try:
        est = get_estimate(conn, est_id)
        if not est:
            return jsonify({"error": "not found"}), 404
        if est["est_type"] != "IRA":
            return jsonify({"error": "not an IRA estimate"}), 400
        mode = (d.get("scope_mode") or "").strip().lower() or None
        if mode not in (None, "drops", "days"):
            return jsonify({"error": "scope_mode must be drops or days"}), 400

        def _num(v):
            if v in (None, ""):
                return None
            return float(v)
        try:
            scope_value = _num(d.get("scope_value"))
            drop_calc = _num(d.get("internal_drop_calc"))
        except (TypeError, ValueError):
            return jsonify({"error": "scope_value / internal_drop_calc must be numbers"}), 400
        # upsert without ON CONFLICT (dialect-neutral; row normally exists from create)
        have = conn.execute("SELECT estimate_id FROM estimate_ira WHERE estimate_id=?",
                            (est_id,)).fetchone()
        if have:
            conn.execute(
                "UPDATE estimate_ira SET dob_registered_email=?, engineer_name=?, "
                "scope_mode=?, scope_value=?, internal_drop_calc=? WHERE estimate_id=?",
                ((d.get("dob_registered_email") or "").strip() or None,
                 (d.get("engineer_name") or "").strip() or None,
                 mode, scope_value, drop_calc, est_id))
        else:
            conn.execute(
                "INSERT INTO estimate_ira (estimate_id, dob_registered_email, engineer_name, "
                "scope_mode, scope_value, internal_drop_calc) VALUES (?,?,?,?,?,?)",
                (est_id, (d.get("dob_registered_email") or "").strip() or None,
                 (d.get("engineer_name") or "").strip() or None, mode, scope_value, drop_calc))
        conn.execute("UPDATE estimate SET updated_at=? WHERE id=?", (_now(), est_id))
        conn.commit()
        r = conn.execute("SELECT * FROM estimate_ira WHERE estimate_id=?", (est_id,)).fetchone()
        return jsonify({"data": dict(r)})
    finally:
        conn.close()


@requires_section('estimating')
def _api_docs_list(est_id):
    # #276: 'estimating' — the estimator reads the lead's drawings/bid forms.
    conn = _db()
    try:
        if not get_estimate(conn, est_id):
            return jsonify({"error": "not found"}), 404
        return jsonify({"data": list_documents(conn, est_id)})
    finally:
        conn.close()


@requires_section('estimating')
def _api_docs_upload(est_id):
    # #276: 'estimating' — the estimator attaches walkthrough-adjacent paper too.
    if 'file' not in request.files:
        return jsonify({"error": "no file"}), 400
    conn = _db()
    try:
        doc_id = add_document(conn, est_id, request.files['file'],
                              category=request.form.get("category"),
                              title=request.form.get("title"), uploaded_by=_uid())
        d = conn.execute("SELECT * FROM estimate_document WHERE id=?", (doc_id,)).fetchone()
        return jsonify({"data": doc_public(dict(d))})
    except LookupError:
        return jsonify({"error": "not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()


@requires_section('estimating')
def _api_doc_file(doc_id):
    """GET /api/estimates/documents/<id>/file — the ONLY way estimate paper is read.
    Inline disposition (view, not print — #271 rule), no-store, path-contained.
    #276: 'estimating' — the estimator opens the lead's documents."""
    conn = _db()
    try:
        row = conn.execute("SELECT file_path, mime, file_name FROM estimate_document WHERE id=?",
                           (doc_id,)).fetchone()
        if not row or not row["file_path"]:
            return jsonify({"error": "not found"}), 404
        p = ssc_paths.resolve_data_path(row["file_path"])   # #287 — rows may be relative now
        base = _DOC_BASE.resolve()
        if not (p.resolve().is_relative_to(base) and p.exists()):
            return jsonify({"error": "file missing"}), 404
        resp = send_file(str(p), mimetype=row["mime"] or "application/octet-stream",
                         as_attachment=False, download_name=(row["file_name"] or f"document-{doc_id}"))
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp
    finally:
        conn.close()


def register(app) -> None:
    """Wire the estimates endpoints. Every route is @requires_section('estimates')
    (admin/c_suite — access.SECTION_ACCESS, ONE source of truth). Call after the auth
    + containment gates."""
    app.add_url_rule("/api/estimates/meta", "estimates_meta", _api_meta, methods=["GET"])
    app.add_url_rule("/api/estimates", "estimates_list", _api_list, methods=["GET"])
    app.add_url_rule("/api/estimates", "estimates_create", _api_create, methods=["POST"])
    app.add_url_rule("/api/estimates/<int:est_id>", "estimates_detail", _api_detail, methods=["GET"])
    app.add_url_rule("/api/estimates/<int:est_id>", "estimates_update", _api_update, methods=["PUT"])
    app.add_url_rule("/api/estimates/<int:est_id>", "estimates_delete", _api_delete, methods=["DELETE"])
    app.add_url_rule("/api/estimates/<int:est_id>/status", "estimates_status", _api_status, methods=["POST"])
    app.add_url_rule("/api/estimates/<int:est_id>/convert", "estimates_convert", _api_convert, methods=["POST"])
    app.add_url_rule("/api/estimates/<int:est_id>/ira", "estimates_ira_put", _api_ira_put, methods=["PUT"])
    app.add_url_rule("/api/estimates/<int:est_id>/documents", "estimates_docs_list", _api_docs_list, methods=["GET"])
    app.add_url_rule("/api/estimates/<int:est_id>/documents", "estimates_docs_upload", _api_docs_upload, methods=["POST"])
    app.add_url_rule("/api/estimates/documents/<int:doc_id>/file", "estimates_doc_file", _api_doc_file, methods=["GET"])
