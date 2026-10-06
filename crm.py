"""#266 — CRM/ops core logic: the four primitives (organizations, contacts, activity
log, follow-up tasks) + the project->client link + the "Needs Attention" feed.

This module holds the DB logic so the endpoints (server.py) AND the guard smoke both
call the same functions. Every function takes an open `conn` (db_layer.connect()) — the
caller owns it — so it works on SQLite (production) and Postgres (gate) unchanged;
placeholders are '?' (db_layer translates to %s on PG). Dates are LOCAL (CLAUDE.md dates
rule): the app/module writes `datetime.now()` / `date.today()` — never UTC — and stores
user-picked dates verbatim.

Access is enforced at the endpoint layer (requires_section('crm') = admin/c_suite, one
source of truth in access.py); this module is access-agnostic pure logic.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

# ---- controlled vocabularies (light validation; stage is intentionally free/enum) ----
RELATIONSHIP_TYPES = {"client", "vendor", "lead", "architect", "engineer", "sub", "partner", "other",
                      "owner", "property_manager", "consultant", "expeditor"}   # #357 — the people around a job
ORG_STATUSES = {"active", "inactive"}
ACTIVITY_TYPES = {"note", "call", "email", "meeting", "stage_change", "system"}
ACTIVITY_ENTITIES = {"organization", "contact", "project"}
TASK_ENTITIES = {"organization", "contact", "project", "none"}
TASK_STATUSES = {"open", "done"}
TASK_PRIORITIES = {"low", "normal", "high"}
FUNCTION_TAGS = {"finance", "sales", "compliance", "ops", "exec"}
# a sensible default pipeline order for the "orgs grouped by stage" summary
STAGE_ORDER = ["prospect", "proposal", "onboarding", "active", "closed"]


# ============================ small helpers ============================

def _now() -> str:
    """LOCAL timestamp (naive, Eastern on this box) — never UTC (CLAUDE.md dates rule)."""
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def norm_tags(tags) -> str:
    """Normalize function tags -> a comma-joined, de-duped, validated string.
    Accepts a list or a comma string; drops anything not in FUNCTION_TAGS."""
    if not tags:
        return ""
    if isinstance(tags, str):
        tags = tags.split(",")
    out = []
    for t in tags:
        t = (t or "").strip().lower()
        if t in FUNCTION_TAGS and t not in out:
            out.append(t)
    return ",".join(out)


def _one_tag(tag) -> Optional[str]:
    tag = (tag or "").strip().lower()
    return tag if tag in FUNCTION_TAGS else None


def _new_id(conn, table: str) -> int:
    """Portable last-insert id: re-read MAX(id). Fine here — single-writer console flows,
    and it avoids lastrowid/RETURNING dialect differences across the two backends."""
    return conn.execute(f"SELECT MAX(id) AS m FROM {table}").fetchone()["m"]


def _rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]


# ============================ organizations ============================

def create_org(conn, *, name, relationship_type=None, status="active", stage=None,
               function_tags=None, notes=None, created_by=None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("organization name is required")
    rt = (relationship_type or "").strip().lower() or None
    if rt and rt not in RELATIONSHIP_TYPES:
        raise ValueError(f"invalid relationship_type: {rt}")
    st = (status or "active").strip().lower()
    if st not in ORG_STATUSES:
        raise ValueError(f"invalid status: {st}")
    conn.execute(
        "INSERT INTO crm_organization (name, relationship_type, status, stage, "
        "function_tags, notes, created_at, created_by) VALUES (?,?,?,?,?,?,?,?)",
        (name, rt, st, (stage or "").strip() or None, norm_tags(function_tags),
         (notes or "").strip() or None, _now(), created_by))
    conn.commit()
    return _new_id(conn, "crm_organization")


def list_orgs(conn, *, search=None, relationship_type=None, stage=None, function_tag=None) -> list:
    """Org list for the CRM section: each row carries its last_activity timestamp.
    Filterable by free-text name/notes, relationship_type, stage, and function_tag."""
    where, params = [], []
    if search:
        where.append("(LOWER(o.name) LIKE ? OR LOWER(COALESCE(o.notes,'')) LIKE ?)")
        s = f"%{search.strip().lower()}%"
        params += [s, s]
    if relationship_type:
        where.append("o.relationship_type = ?")
        params.append(relationship_type.strip().lower())
    if stage:
        where.append("o.stage = ?")
        params.append(stage.strip())
    tag = _one_tag(function_tag)
    if tag:
        where.append("(',' || COALESCE(o.function_tags,'') || ',') LIKE ?")
        params.append(f"%,{tag},%")
    sql = (
        "SELECT o.*, "
        "  (SELECT MAX(a.occurred_at) FROM crm_activity a "
        "     WHERE a.entity_type='organization' AND a.entity_id=o.id) AS last_activity "
        "FROM crm_organization o")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.name COLLATE NOCASE" if not _is_pg(conn) else " ORDER BY LOWER(o.name)"
    return _rows(conn.execute(sql, params))


def get_org(conn, org_id) -> Optional[dict]:
    r = conn.execute("SELECT * FROM crm_organization WHERE id=?", (org_id,)).fetchone()
    return dict(r) if r else None


def update_org(conn, org_id, *, actor_user_id=None, **fields) -> bool:
    """Update allowed org fields. If `stage` changes, auto-log a stage_change activity on
    the org timeline (the 'stage_change' primitive) so the onboarding pipeline is auditable."""
    cur = get_org(conn, org_id)
    if not cur:
        return False
    allowed = {"name", "relationship_type", "status", "stage", "notes"}
    sets, params = [], []
    old_stage = cur.get("stage")
    for k, v in fields.items():
        if k == "function_tags":
            sets.append("function_tags = ?")
            params.append(norm_tags(v))
        elif k in allowed:
            if k == "name" and not str(v or "").strip():
                raise ValueError("the client name can't be blank")
            if k == "relationship_type" and v:
                v = str(v).strip().lower()
                if v not in RELATIONSHIP_TYPES:
                    raise ValueError(f"invalid relationship_type: {v}")
            if k == "status" and v:
                v = str(v).strip().lower()
                if v not in ORG_STATUSES:
                    raise ValueError(f"invalid status: {v}")
            sets.append(f"{k} = ?")
            params.append((str(v).strip() or None) if v is not None else None)
    if not sets:
        return True
    params.append(org_id)
    conn.execute(f"UPDATE crm_organization SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    new_stage = fields.get("stage")
    if "stage" in fields and (new_stage or None) != (old_stage or None):
        add_activity(conn, entity_type="organization", entity_id=org_id,
                     activity_type="stage_change", author_user_id=actor_user_id,
                     summary=f"Stage: {old_stage or '—'} → {new_stage or '—'}")
    labels = {"name": "name", "relationship_type": "type", "status": "status", "notes": "notes"}
    moved = [labels[k] for k in labels if k in fields
             and ((str(fields[k]).strip() or None) if fields[k] is not None else None) != (cur.get(k) or None)]
    if moved:
        add_activity(conn, entity_type="organization", entity_id=org_id, activity_type="system",
                     author_user_id=actor_user_id, summary="Client details updated: " + ", ".join(moved))
    return True


# ============================ contacts ============================

def create_contact(conn, *, full_name, org_id=None, email=None, phone=None, title=None,
                   relationship_type=None, status="active", notes=None, created_by=None) -> int:
    full_name = (full_name or "").strip()
    if not full_name:
        raise ValueError("contact full_name is required")
    if org_id is not None and not get_org(conn, org_id):
        raise ValueError("org_id does not exist")
    rt = (relationship_type or "").strip().lower() or None
    if rt and rt not in RELATIONSHIP_TYPES:
        raise ValueError(f"invalid relationship_type: {rt}")
    conn.execute(
        "INSERT INTO crm_contact (org_id, full_name, email, phone, title, relationship_type, "
        "status, notes, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (org_id, full_name, (email or "").strip() or None, (phone or "").strip() or None,
         (title or "").strip() or None, rt, (status or "active").strip().lower(),
         (notes or "").strip() or None, _now(), created_by))
    conn.commit()
    return _new_id(conn, "crm_contact")


def list_contacts(conn, *, org_id=None) -> list:
    if org_id is not None:
        return _rows(conn.execute(
            "SELECT * FROM crm_contact WHERE org_id=? ORDER BY full_name", (org_id,)))
    return _rows(conn.execute("SELECT * FROM crm_contact ORDER BY full_name"))


def get_contact(conn, contact_id) -> Optional[dict]:
    r = conn.execute("SELECT * FROM crm_contact WHERE id=?", (contact_id,)).fetchone()
    return dict(r) if r else None


def update_contact(conn, contact_id, **fields) -> bool:
    if not get_contact(conn, contact_id):
        return False
    allowed = {"org_id", "full_name", "email", "phone", "title", "relationship_type", "status", "notes"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "full_name" and not str(v or "").strip():
            raise ValueError("the contact name can't be blank")
        if k == "status" and v and str(v).strip().lower() not in ORG_STATUSES:
            raise ValueError(f"invalid status: {v}")
        if k == "relationship_type" and v:
            v = str(v).strip().lower()
            if v not in RELATIONSHIP_TYPES:
                raise ValueError(f"invalid relationship_type: {v}")
        sets.append(f"{k} = ?")
        params.append(v if k == "org_id" else ((str(v).strip() or None) if v is not None else None))
    if not sets:
        return True
    params.append(contact_id)
    conn.execute(f"UPDATE crm_contact SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    return True


# ============================ activity (the timeline) ============================

def add_activity(conn, *, entity_type, entity_id, activity_type, summary=None, body=None,
                 author_user_id=None, occurred_at=None, function_tag=None) -> int:
    """#273 — optional function_tag (same vocab as crm_task): the estimates/IRA pipeline
    stamps its auto-logged rows 'sales' so future role-slicing can filter the timeline.
    Requires the #273 migration (ensure_crm_schema adds the column additively)."""
    if entity_type not in ACTIVITY_ENTITIES:
        raise ValueError(f"invalid entity_type: {entity_type}")
    if activity_type not in ACTIVITY_TYPES:
        raise ValueError(f"invalid activity_type: {activity_type}")
    occurred = (occurred_at or "").strip() or _now()   # user-picked LOCAL, else now
    conn.execute(
        "INSERT INTO crm_activity (entity_type, entity_id, activity_type, summary, body, "
        "author_user_id, occurred_at, created_at, function_tag) VALUES (?,?,?,?,?,?,?,?,?)",
        (entity_type, entity_id, activity_type, (summary or "").strip() or None,
         (body or "").strip() or None, author_user_id, occurred, _now(), _one_tag(function_tag)))
    conn.commit()
    return _new_id(conn, "crm_activity")


def list_activity(conn, *, entity_type, entity_id, limit=200) -> list:
    """An entity's timeline, newest first (by occurred_at, then id)."""
    return _rows(conn.execute(
        "SELECT * FROM crm_activity WHERE entity_type=? AND entity_id=? "
        "ORDER BY occurred_at DESC, id DESC LIMIT ?",
        (entity_type, entity_id, limit)))


def recent_activity(conn, *, limit=15) -> list:
    """Company-wide recent-activity feed for the console widget."""
    return _rows(conn.execute(
        "SELECT * FROM crm_activity ORDER BY occurred_at DESC, id DESC LIMIT ?", (limit,)))


# ============================ tasks (follow-ups) + needs-attention ============================

def create_task(conn, *, title, entity_type="none", entity_id=None, detail=None, due_date=None,
                assignee_user_id=None, priority="normal", function_tag=None, created_by=None) -> int:
    title = (title or "").strip()
    if not title:
        raise ValueError("task title is required")
    et = (entity_type or "none").strip().lower()
    if et not in TASK_ENTITIES:
        raise ValueError(f"invalid entity_type: {et}")
    if et == "none":
        entity_id = None
    pr = (priority or "normal").strip().lower()
    if pr not in TASK_PRIORITIES:
        raise ValueError(f"invalid priority: {pr}")
    conn.execute(
        "INSERT INTO crm_task (entity_type, entity_id, title, detail, due_date, "
        "assignee_user_id, status, priority, function_tag, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (et, entity_id, title, (detail or "").strip() or None,
         (due_date or "").strip() or None, assignee_user_id, "open", pr,
         _one_tag(function_tag), created_by, _now()))
    conn.commit()
    return _new_id(conn, "crm_task")


def complete_task(conn, task_id) -> bool:
    r = conn.execute("SELECT id FROM crm_task WHERE id=?", (task_id,)).fetchone()
    if not r:
        return False
    conn.execute("UPDATE crm_task SET status='done', completed_at=? WHERE id=?",
                 (_now(), task_id))
    conn.commit()
    return True


def list_tasks(conn, *, status=None, assignee_user_id=None, function_tag=None,
               entity_type=None, entity_id=None) -> list:
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status.strip().lower())
    if assignee_user_id is not None:
        where.append("assignee_user_id = ?")
        params.append(assignee_user_id)
    tag = _one_tag(function_tag)
    if tag:
        where.append("function_tag = ?")
        params.append(tag)
    if entity_type:
        where.append("entity_type = ?")
        params.append(entity_type.strip().lower())
    if entity_id is not None:
        where.append("entity_id = ?")
        params.append(entity_id)
    sql = "SELECT * FROM crm_task"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY (due_date IS NULL), due_date, id DESC"
    return _rows(conn.execute(sql, params))


def needs_attention(conn, *, assignee_user_id=None, function_tag=None, today=None) -> list:
    """The killer view: OPEN tasks, overdue first, then by due date. Each row carries an
    `overdue` flag + `days_until` (negative = overdue). Filter by assignee (me) + tag."""
    today = today or _today()
    where = ["status = 'open'"]
    params = []
    if assignee_user_id is not None:
        where.append("assignee_user_id = ?")
        params.append(assignee_user_id)
    tag = _one_tag(function_tag)
    if tag:
        where.append("function_tag = ?")
        params.append(tag)
    rows = _rows(conn.execute(
        "SELECT * FROM crm_task WHERE " + " AND ".join(where), params))
    for r in rows:
        due = r.get("due_date")
        r["overdue"] = bool(due and due < today)
        r["due_today"] = bool(due and due == today)
        r["days_until"] = _days_between(today, due) if due else None
    # overdue first, then no-due-date last, then soonest due; high priority breaks ties
    prio = {"high": 0, "normal": 1, "low": 2}

    def _key(r):
        due = r.get("due_date") or "9999-12-31"
        return (0 if r["overdue"] else 1, due, prio.get(r.get("priority"), 1))
    rows.sort(key=_key)
    return rows


def _days_between(a_iso: str, b_iso: str) -> Optional[int]:
    try:
        return (date.fromisoformat(b_iso) - date.fromisoformat(a_iso)).days
    except Exception:
        return None


# ============================ project <-> client-org link ============================

def link_project(conn, project_code, org_id) -> bool:
    if org_id is not None and not get_org(conn, org_id):
        raise ValueError("org_id does not exist")
    r = conn.execute("SELECT project_code FROM projects WHERE project_code=?",
                     (project_code,)).fetchone()
    if not r:
        return False
    conn.execute("UPDATE projects SET client_org_id=? WHERE project_code=?",
                 (org_id, project_code))
    try:
        import buildings as _buildings   # #304
        _buildings.autolink_project(conn, project_code)
    except Exception:
        pass
    conn.commit()
    return True


def unlink_project(conn, project_code) -> bool:
    return link_project(conn, project_code, None)


def list_org_projects(conn, org_id) -> list:
    """Buildings / job sites linked to this client organization.

    ``projects`` is the existing operational source of truth for a job site.  Returning
    the address here lets the CRM present an unambiguous client -> building -> job
    hierarchy without creating a second, competing building registry.
    """
    return _rows(conn.execute(
        "SELECT project_code, name, address, city_zip, status, client_org_id FROM projects "
        "WHERE client_org_id=? ORDER BY COALESCE(address, name), project_code", (org_id,)))


# ============================ console surfacing ============================

def pipeline_by_stage(conn) -> list:
    """Orgs grouped by stage for the pipeline summary widget. Known stages in a sensible
    order first, then any free-form stages, then unstaged."""
    rows = _rows(conn.execute(
        "SELECT COALESCE(stage,'') AS stage, COUNT(*) AS n FROM crm_organization "
        "WHERE COALESCE(status,'active') <> 'inactive' GROUP BY COALESCE(stage,'')"))
    counts = {r["stage"]: r["n"] for r in rows}
    out = []
    for s in STAGE_ORDER:
        if s in counts:
            out.append({"stage": s, "count": counts.pop(s)})
    for s in sorted(k for k in counts if k):     # free-form stages
        out.append({"stage": s, "count": counts.pop(s)})
    if counts.get(""):
        out.append({"stage": "(unstaged)", "count": counts[""]})
    return out


def _is_pg(conn) -> bool:
    """COLLATE NOCASE is a SQLite-ism; use LOWER() ordering on Postgres."""
    try:
        import db_layer
        return db_layer.is_postgres()
    except Exception:
        return False
