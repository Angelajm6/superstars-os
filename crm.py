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

import re
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
# the client's own details on Edit client (2026-10-22): one email / phone / website + its address
ORG_DETAIL_FIELDS = ("email", "phone", "website", "address_line", "city", "state", "zip")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# a sensible default pipeline order for the "orgs grouped by stage" summary
STAGE_ORDER = ["prospect", "proposal", "onboarding", "active", "closed"]

# Clients revamp (2026-10-28): the client is whoever PAYS us. client_kind says what kind of payer it
# is; the billing fields are the defaults new invoices / pay apps / proposals start from.
CLIENT_KINDS = {"owner": "Building owner / LLC", "board": "Co-op or condo board", "gc": "General contractor",
                "cm": "Construction manager", "sub": "Another trade", "pm": "Property management co.",
                "home": "Homeowner", "agency": "Public agency or prime"}
CONTACT_ROLES = {"signs": "Signs contracts", "billing": "Billing / AP", "pm": "Project manager",
                 "site": "Site super", "board": "Board member", "agent": "Managing agent"}
# (column, type) — ints are 0/1 flags or whole days, reals are percents
CLIENT_PROFILE_COLS = (("client_kind", "TEXT"), ("payment_terms_days", "INTEGER"), ("deposit_pct", "REAL"),
                       ("retainage_pct", "REAL"), ("pay_apps", "INTEGER"), ("lien_waivers", "INTEGER"),
                       ("capital_improvement", "INTEGER"), ("certified_payroll", "INTEGER"),
                       ("billing_email", "TEXT"), ("coi_wording", "TEXT"), ("managing_agent_org_id", "INTEGER"))
CLIENT_PROFILE_FIELDS = tuple(c for c, _ in CLIENT_PROFILE_COLS)
_PROFILE_TYPES = dict(CLIENT_PROFILE_COLS)
# what a new client of each kind starts with (all editable): a GC gets pay apps, 10% retainage, lien waivers
BILLING_DEFAULTS = {
    "owner":  dict(payment_terms_days=30, deposit_pct=20, retainage_pct=0,  pay_apps=0, lien_waivers=0, capital_improvement=1, certified_payroll=0),
    "board":  dict(payment_terms_days=45, deposit_pct=20, retainage_pct=0,  pay_apps=0, lien_waivers=0, capital_improvement=1, certified_payroll=0),
    "gc":     dict(payment_terms_days=30, deposit_pct=0,  retainage_pct=10, pay_apps=1, lien_waivers=1, capital_improvement=1, certified_payroll=0),
    "cm":     dict(payment_terms_days=30, deposit_pct=0,  retainage_pct=10, pay_apps=1, lien_waivers=1, capital_improvement=1, certified_payroll=0),
    "sub":    dict(payment_terms_days=45, deposit_pct=0,  retainage_pct=10, pay_apps=1, lien_waivers=1, capital_improvement=1, certified_payroll=0),
    "pm":     dict(payment_terms_days=30, deposit_pct=20, retainage_pct=0,  pay_apps=0, lien_waivers=0, capital_improvement=1, certified_payroll=0),
    "home":   dict(payment_terms_days=0,  deposit_pct=25, retainage_pct=0,  pay_apps=0, lien_waivers=0, capital_improvement=0, certified_payroll=0),
    "agency": dict(payment_terms_days=30, deposit_pct=0,  retainage_pct=5,  pay_apps=1, lien_waivers=0, capital_improvement=0, certified_payroll=1),
}


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
    allowed = {"name", "relationship_type", "status", "stage", "notes", *ORG_DETAIL_FIELDS}
    sets, params = [], []
    old_stage = cur.get("stage")
    for k, v in fields.items():
        if k == "function_tags":
            sets.append("function_tags = ?")
            params.append(norm_tags(v))
        elif k in CLIENT_PROFILE_FIELDS:
            sets.append(f"{k} = ?")
            params.append(profile_value(conn, k, v, org_id=org_id))
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
            if k == "email" and str(v or "").strip() and not _EMAIL_RE.match(str(v).strip()):
                raise ValueError("That email doesn't look right.")
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
    labels = {"name": "name", "relationship_type": "type", "status": "status", "notes": "notes",
              "email": "email", "phone": "phone", "website": "website",
              "address_line": "address", "city": "address", "state": "address", "zip": "address"}
    moved = []
    for k in labels:
        if k in fields and labels[k] not in moved \
                and ((str(fields[k]).strip() or None) if fields[k] is not None else None) != (cur.get(k) or None):
            moved.append(labels[k])
    if "client_kind" in fields and (fields["client_kind"] or None) != (cur.get("client_kind") or None):
        moved.append("kind")
    if any(k in fields and k != "client_kind" for k in CLIENT_PROFILE_FIELDS):
        after = get_org(conn, org_id)
        if any(after.get(k) != cur.get(k) for k in CLIENT_PROFILE_FIELDS if k != "client_kind" and k in fields):
            moved.append("billing setup")
    if moved:
        add_activity(conn, entity_type="organization", entity_id=org_id, activity_type="system",
                     author_user_id=actor_user_id, summary="Client details updated: " + ", ".join(moved))
    return True


def profile_value(conn, k, v, *, org_id=None):
    """Validate + convert one CLIENT_PROFILE_FIELDS value for storage (None clears it)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    t = _PROFILE_TYPES[k]
    if k == "client_kind":
        v = str(v).strip().lower()
        if v not in CLIENT_KINDS:
            raise ValueError(f"invalid client kind: {v}")
        return v
    if k == "managing_agent_org_id":
        v = int(v)
        if v == org_id or not get_org(conn, v):
            raise ValueError("pick a managing agent from the list")
        return v
    if k == "billing_email":
        v = str(v).strip()
        if not _EMAIL_RE.match(v):
            raise ValueError("That billing email doesn't look right.")
        return v
    if t == "TEXT":
        return str(v).strip()
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{k.replace('_', ' ')} must be a number")
    if k in ("pay_apps", "lien_waivers", "capital_improvement", "certified_payroll"):
        return 1 if n else 0
    if k == "payment_terms_days" and not 0 <= n <= 180:
        raise ValueError("payment terms must be between 0 and 180 days")
    if k in ("deposit_pct", "retainage_pct") and not 0 <= n <= 100:
        raise ValueError(f"{k.split('_')[0]} must be between 0 and 100%")
    return int(n) if t == "INTEGER" else round(n, 2)


def ensure_client_profile(conn) -> bool:
    """Additive: crm_organization gains client_kind + the billing defaults, crm_contact gains roles +
    is_primary. Idempotent, both backends; existing rows keep NULL (the list asks for the kind)."""
    from apply_crm_266 import _columns
    changed = False
    have = _columns(conn, "crm_organization")
    for c, t in CLIENT_PROFILE_COLS:
        if c not in have:
            conn.execute(f"ALTER TABLE crm_organization ADD COLUMN {c} {t}")
            changed = True
    have = _columns(conn, "crm_contact")
    for c, t in (("roles", "TEXT"), ("is_primary", "INTEGER")):
        if c not in have:
            conn.execute(f"ALTER TABLE crm_contact ADD COLUMN {c} {t}")
            changed = True
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crmorg_agent ON crm_organization(managing_agent_org_id)")
    conn.commit()
    return changed


def find_org_by_name(conn, name) -> Optional[dict]:
    r = conn.execute("SELECT * FROM crm_organization WHERE LOWER(name) = ?", ((name or "").strip().lower(),)).fetchone()
    return dict(r) if r else None


def create_client(conn, *, name, client_kind, lead=False, profile=None, contact=None,
                  managing_agent_name=None, created_by=None) -> int:
    """Add client in one step: the payer with its kind + billing setup (the kind's BILLING_DEFAULTS,
    overridden by `profile`), its main contact with roles, and for a board its managing agent (found
    by name or added to the Directory). A name that's already there is refused, not duplicated."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Enter the client's name.")
    kind = (client_kind or "").strip().lower()
    if kind not in CLIENT_KINDS:
        raise ValueError("Pick what kind of client this is.")
    if find_org_by_name(conn, name):
        raise ValueError(f"{name} is already in Clients or the Directory.")
    vals = dict(BILLING_DEFAULTS[kind])
    vals.update({k: v for k, v in (profile or {}).items() if k in CLIENT_PROFILE_FIELDS and k != "client_kind"})
    vals["client_kind"] = kind
    clean = {k: profile_value(conn, k, v) for k, v in vals.items() if k != "managing_agent_org_id"}
    agent_id = None
    agent_name = (managing_agent_name or "").strip()
    if agent_name:
        if agent_name.lower() == name.lower():
            raise ValueError("The managing agent can't be the client itself.")
        agent = find_org_by_name(conn, agent_name)
        agent_id = agent["id"] if agent else create_org(conn, name=agent_name, relationship_type="property_manager",
                                                        created_by=created_by)
    contact = contact or {}
    if contact.get("email") and not _EMAIL_RE.match(str(contact["email"]).strip()):
        raise ValueError("The contact's email doesn't look right.")
    oid = create_org(conn, name=name, relationship_type="lead" if lead else "client", created_by=created_by)
    clean["managing_agent_org_id"] = agent_id
    cols = list(clean)
    conn.execute(f"UPDATE crm_organization SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                 [clean[c] for c in cols] + [oid])
    conn.commit()
    if (contact.get("full_name") or "").strip():
        create_contact(conn, full_name=contact["full_name"], org_id=oid, email=contact.get("email"),
                       phone=contact.get("phone"), title=contact.get("title"), created_by=created_by,
                       roles=contact.get("roles"), is_primary=True)
    add_activity(conn, entity_type="organization", entity_id=oid, activity_type="system", author_user_id=created_by,
                 summary=f"{'Lead' if lead else 'Client'} added · {CLIENT_KINDS[kind]}")
    return oid


def norm_roles(roles) -> Optional[str]:
    """A list or comma string of CONTACT_ROLES -> 'signs,billing' (unknown roles dropped)."""
    if not roles:
        return None
    if isinstance(roles, str):
        roles = roles.split(",")
    out = []
    for r in roles:
        r = str(r or "").strip().lower()
        if r in CONTACT_ROLES and r not in out:
            out.append(r)
    return ",".join(out) or None


def ensure_org_details(conn) -> bool:
    """Additive: crm_organization gains the ORG_DETAIL_FIELDS columns. Idempotent, both backends."""
    from apply_crm_266 import _columns
    have = _columns(conn, "crm_organization")
    missing = [c for c in ORG_DETAIL_FIELDS if c not in have]
    for c in missing:
        conn.execute(f"ALTER TABLE crm_organization ADD COLUMN {c} TEXT")
    conn.commit()
    return bool(missing)


# Real business records that keep a client from being deleted: (table, column, what to call it).
# A client with any of these is marked inactive instead; the delete is for a mistake or a test.
_ORG_KEEPERS = (
    ("projects", "client_org_id", "job"),
    ("estimate", "client_org_id", "estimate"),
    ("proposal", "client_org_id", "proposal"),
    ("invoice", "client_org_id", "invoice"),
    ("payable", "vendor_org_id", "bill"),
    ("document_package", "client_org_id", "document package"),
)


def _has_table(conn, table) -> bool:
    from apply_crm_266 import _table_exists
    return _table_exists(conn, table)


def _in(ids) -> str:
    return ",".join("?" * len(ids))


def org_delete_blockers(conn, org_id) -> list:
    """What still points at this client, as human words ("2 jobs", "1 proposal"). Empty = deletable."""
    out = []
    for table, col, word in _ORG_KEEPERS:
        if not _has_table(conn, table):
            continue
        n = conn.execute(f"SELECT COUNT(1) AS n FROM {table} WHERE {col} = ?", (org_id,)).fetchone()["n"]
        if n:
            out.append(f"{n} {word}{'' if n == 1 else 's'}")
    cids = [r["id"] for r in conn.execute("SELECT id FROM crm_contact WHERE org_id = ?", (org_id,)).fetchall()]
    if cids and _has_table(conn, "project_contact"):
        n = conn.execute(f"SELECT COUNT(1) AS n FROM project_contact WHERE contact_id IN ({_in(cids)})",
                         cids).fetchone()["n"]
        if n:
            out.append(f"{n} job contact{'' if n == 1 else 's'}")
    if _has_table(conn, "meeting_invite"):
        n = conn.execute(
            "SELECT COUNT(1) AS n FROM meeting_invite i JOIN crm_task t ON t.id = i.item_id "
            "WHERE (t.entity_type = 'organization' AND t.entity_id = ?)"
            + (f" OR (t.entity_type = 'contact' AND t.entity_id IN ({_in(cids)}))" if cids else ""),
            [org_id, *cids]).fetchone()["n"]
        if n:
            out.append(f"{n} meeting{'' if n == 1 else 's'} with invitations sent")
    return out


def delete_org(conn, org_id) -> Optional[dict]:
    """Permanently delete a client that nothing real points at: the org, its contacts, its notes /
    timeline, its follow-ups, its email links and its QuickBooks customer link. Buildings and emails
    that named it stay, unlinked. Returns None when the org doesn't exist, {"blockers": [...]} when
    it can't go, else {"deleted": True, "user_ids": [...]} (people whose calendars carried its items)."""
    if not get_org(conn, org_id):
        return None
    blockers = org_delete_blockers(conn, org_id)
    if blockers:
        return {"blockers": blockers}
    cids = [r["id"] for r in conn.execute("SELECT id FROM crm_contact WHERE org_id = ?", (org_id,)).fetchall()]
    ent = "(entity_type = 'organization' AND entity_id = ?)" + (
        f" OR (entity_type = 'contact' AND entity_id IN ({_in(cids)}))" if cids else "")
    ent_params = [org_id, *cids]
    tasks = _rows(conn.execute(f"SELECT id, assignee_user_id FROM crm_task WHERE {ent}", ent_params))
    tids = [t["id"] for t in tasks]
    users = {t["assignee_user_id"] for t in tasks if t.get("assignee_user_id")}
    if tids and _has_table(conn, "crm_task_attendee"):
        users |= {r["user_id"] for r in conn.execute(
            f"SELECT user_id FROM crm_task_attendee WHERE task_id IN ({_in(tids)})", tids).fetchall()}
        conn.execute(f"DELETE FROM crm_task_attendee WHERE task_id IN ({_in(tids)})", tids)
    if tids:
        conn.execute(f"DELETE FROM crm_task WHERE id IN ({_in(tids)})", tids)
    conn.execute(f"DELETE FROM crm_activity WHERE {ent}", ent_params)
    if _has_table(conn, "mail_link"):
        conn.execute("DELETE FROM mail_link WHERE entity_type = 'client' AND entity_id = ?", (str(org_id),))
    if _has_table(conn, "mail_message"):
        conn.execute("UPDATE mail_message SET client_org_id = NULL WHERE client_org_id = ?", (org_id,))
        if cids:
            conn.execute(f"UPDATE mail_message SET contact_id = NULL WHERE contact_id IN ({_in(cids)})", cids)
    if _has_table(conn, "building"):
        conn.execute("UPDATE building SET client_org_id = NULL WHERE client_org_id = ?", (org_id,))
    if _has_table(conn, "qbo_customer"):
        conn.execute("DELETE FROM qbo_customer WHERE org_id = ?", (org_id,))
    conn.execute("DELETE FROM crm_contact WHERE org_id = ?", (org_id,))
    conn.execute("DELETE FROM crm_organization WHERE id = ?", (org_id,))
    conn.commit()
    return {"deleted": True, "user_ids": sorted(users)}


# ============================ contacts ============================

def create_contact(conn, *, full_name, org_id=None, email=None, phone=None, title=None,
                   relationship_type=None, status="active", notes=None, created_by=None,
                   roles=None, is_primary=False) -> int:
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
    cid = _new_id(conn, "crm_contact")
    if roles:
        conn.execute("UPDATE crm_contact SET roles = ? WHERE id = ?", (norm_roles(roles), cid))
        conn.commit()
    if is_primary:
        set_primary_contact(conn, cid)
    return cid


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
        if k == "roles":
            sets.append("roles = ?")
            params.append(norm_roles(v))
            continue
        if k == "is_primary":
            if v:
                set_primary_contact(conn, contact_id)
            else:
                sets.append("is_primary = 0")
            continue
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


def set_primary_contact(conn, contact_id) -> None:
    """One main contact per client: this one on, the client's others off."""
    c = get_contact(conn, contact_id)
    if not c:
        return
    if c.get("org_id") is not None:
        conn.execute("UPDATE crm_contact SET is_primary = 0 WHERE org_id = ? AND id <> ?", (c["org_id"], contact_id))
    conn.execute("UPDATE crm_contact SET is_primary = 1 WHERE id = ?", (contact_id,))
    conn.commit()


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
