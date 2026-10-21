"""#358 — the job dashboard's executive summary, in ONE payload.

The C-suite found Project Health cramped and buried: two KPI rows, a fixed money block,
then a drag-grid, with nothing drilling through and none of the questions they ask —
are we behind, what does it cost against the contract, did today's report go out, what
is open with the DOB / the architect, who do I call. This module answers those in one
payload, served as a `summary` part of /api/projects/<code>/bootstrap (no extra page-load
request) and on its own at GET /api/projects/<code>/summary.

Blocks: header · schedule · money (ONLY for admin / c_suite — omitted from the payload for
anyone else, not just hidden) · field_today · risk · open_items · contacts · photos.
Oct 7 C-suite notes: the page shows On site today / % complete / Open items on top; open
items are the job's own tasks and events (team_items, also on the team calendar); key
contacts come in four groups (Our team, Owner, Engineer / Architect, General contractor). Every block is
built independently and safely: one failing query (an optional table missing on a fresh
fixture) blanks that block, never the page.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from flask import jsonify

import db_layer
from auth import current_user

log = logging.getLogger("ssc.project_summary")
FINANCIAL_ROLES = {"admin", "c_suite"}


def _rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]


def _one(cur) -> Optional[dict]:
    r = cur.fetchone()
    return dict(r) if r else None


def _safe(conn, fn, default=None):
    try:
        return fn()
    except Exception as e:
        log.info("#358 summary block skipped: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return default


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- blocks
def _header(conn, code, today, role=None):
    import billing_type
    import project_contacts
    p = _one(conn.execute("SELECT * FROM projects WHERE project_code = ?", (code,))) or {}
    people = project_contacts.project_people(conn, code)
    start, end = p.get("start_date"), p.get("target_completion")
    days_left = elapsed = None
    if end:
        try:
            days_left = (date.fromisoformat(str(end)[:10]) - today).days
        except ValueError:
            pass
    if start and end:
        try:
            s, e = date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])
            if e > s:
                elapsed = max(0.0, min(100.0, round((today - s).days / (e - s).days * 100, 1)))
        except ValueError:
            pass
    bt = p.get("billing_type")
    import project_setup
    return {"code": code, "name": p.get("name"), "job_number": p.get("job_number"), "access_labels": project_setup.access_labels(p),
            "color": project_setup.color_hex({**p, "project_code": code}), "color_ink": project_setup.color_ink({**p, "project_code": code}), "can_edit": role in FINANCIAL_ROLES, "address": p.get("address"), "status": p.get("status") or "active",
            "billing_type": bt, "billing_label": billing_type.label(bt), "billing_short": billing_type.SHORT.get(bt or "", "Not set"),
            "start_date": start, "target_completion": end, "days_left": days_left, "time_elapsed_pct": elapsed,
            "client": people.get("owner_client"), "project_manager": people.get("project_manager"),
            "superintendent": people.get("superintendent")}


def _schedule(conn, code):
    import dropplan_rollups as r
    ids = [x["drop_id"] for x in _rows(conn.execute(
        "SELECT drop_id FROM drops WHERE project_code = ? ORDER BY sequence_no", (code,)))]
    counts = {"total": len(ids), "complete": 0, "active": 0, "behind": 0, "not_started": 0}
    behind = []
    for did in ids:
        d = r.drop_summary(conn, did, include_cost=False) or {}
        pct = (d.get("progress") or {}).get("pct") or 0
        lc = d.get("lifecycle") or ""
        wd = d.get("working_days") or {}
        if pct >= 100:
            st = "complete"
        elif not (pct > 0 or (lc and lc != "not_started")):
            st = "not_started"
        elif wd.get("status") == "behind":
            st = "behind"
        else:
            st = "active"
        counts[st] += 1
        if st == "behind":
            behind.append({"drop_id": did, "label": f"Drop {d.get('sequence_no')}" + (f" · {d['elevation']}" if d.get("elevation") else ""),
                           "pct": pct, "days_behind": abs(_num(wd.get("variance_days") or wd.get("variance")))})
    roll = _safe(conn, lambda: r.project_rollup(conn, code, include_cost=False), {}) or {}
    behind.sort(key=lambda x: -x["days_behind"])
    return {**counts, "overall_pct": roll.get("overall_progress_pct") or 0.0, "worst_behind": behind[:3]}


def _money(conn, code, today):
    import portal_projects
    import project_costs
    contract = portal_projects._contract(conn, code) or {}
    co = _safe(conn, lambda: _num(conn.execute(
        "SELECT SUM(scheduled_value) FROM billing_sov WHERE project_code = ? AND kind = 'change_order'", (code,)).fetchone()[0]), 0.0)
    inv = _safe(conn, lambda: _rows(conn.execute(
        "SELECT amount, amount_paid, due_at, status FROM invoice WHERE project_code = ? AND status IN ('issued','paid')",
        (code,))), []) or []
    invoiced = sum(_num(i["amount"]) for i in inv)
    collected = sum(_num(i["amount_paid"]) for i in inv)
    aging = {"current": 0.0, "d1_30": 0.0, "d31_60": 0.0, "d61_90": 0.0, "d90p": 0.0}
    for i in inv:
        if i["status"] != "issued":
            continue
        owed = _num(i["amount"]) - _num(i["amount_paid"])
        if owed <= 0:
            continue
        due = None
        try:
            due = date.fromisoformat(str(i["due_at"])[:10]) if i.get("due_at") else None
        except ValueError:
            pass
        late = (today - due).days if due else 0
        key = "current" if late <= 0 else "d1_30" if late <= 30 else "d31_60" if late <= 60 else "d61_90" if late <= 90 else "d90p"
        aging[key] += owed
    open_ar = sum(aging.values())
    costs = _safe(conn, lambda: project_costs.widget_payload(conn, code, today), None)
    cost_total = _num((costs or {}).get("total"))
    cv = contract.get("value")
    return {"contract": cv, "contract_source": contract.get("source"), "change_orders": round(co, 2),
            "invoiced": round(invoiced, 2), "collected": round(collected, 2), "open_ar": round(open_ar, 2),
            "aging": {k: round(v, 2) for k, v in aging.items()},
            "overdue": round(open_ar - aging["current"], 2),
            "balance_to_finish": round(cv - invoiced, 2) if cv else None,
            "costs_to_date": round(cost_total, 2) if costs is not None else None,
            "cost_pct_of_contract": round(cost_total / cv * 100, 1) if (cv and costs is not None) else None,
            "billed_pct": round(invoiced / cv * 100, 1) if cv else None}


def _field_today(conn, code, today):
    t = today.isoformat()
    on_site = _safe(conn, lambda: int(conn.execute(
        "SELECT COUNT(DISTINCT employee_id) FROM sign_in_log WHERE project_code = ? AND date = ?", (code, t)).fetchone()[0]), 0)
    dcr_today = _safe(conn, lambda: bool(conn.execute(
        "SELECT 1 FROM report_index WHERE project_code = ? AND report_type = 'DCR' AND report_date = ? LIMIT 1",
        (code, t)).fetchone()), False)
    last = _safe(conn, lambda: _one(conn.execute(
        "SELECT report_date, dcr_sequence FROM report_index WHERE project_code = ? AND report_type = 'DCR' "
        "AND dcr_sequence IS NOT NULL ORDER BY report_date DESC, dcr_sequence DESC LIMIT 1", (code,))), None)
    assigned = _safe(conn, lambda: int(conn.execute(
        "SELECT COUNT(1) FROM project_assignments WHERE project_code = ? AND COALESCE(status, 'active') = 'active'",
        (code,)).fetchone()[0]), 0)
    return {"on_site": on_site, "assigned": assigned, "dcr_today": dcr_today,
            "last_dcr": ({"date": str(last["report_date"])[:10], "number": f"DCR-{code}-{int(last['dcr_sequence']):03d}"}
                         if last else None)}


def _risk(conn, code, today):
    t = today.isoformat()
    closed = "('closed','answered','responded')"
    rfis_open = _safe(conn, lambda: int(conn.execute(
        f"SELECT COUNT(1) FROM rfi_log WHERE project_code = ? AND LOWER(COALESCE(status,'open')) NOT IN {closed}",
        (code,)).fetchone()[0]), 0)
    rfis_overdue = _safe(conn, lambda: int(conn.execute(
        f"SELECT COUNT(1) FROM rfi_log WHERE project_code = ? AND LOWER(COALESCE(status,'open')) NOT IN {closed} "
        f"AND due_date IS NOT NULL AND due_date < ?", (code, t)).fetchone()[0]), 0)
    markup_rfis = _safe(conn, lambda: int(conn.execute(
        "SELECT COUNT(1) FROM rfi WHERE project_code = ? AND LOWER(COALESCE(status,'open')) = 'open'", (code,)).fetchone()[0]), 0)
    violations = _safe(conn, lambda: int(conn.execute(
        "SELECT COUNT(1) FROM dob_violations WHERE project_code = ? AND UPPER(COALESCE(status,'')) = 'ACTIVE'",
        (code,)).fetchone()[0]), 0)
    complaints = _safe(conn, lambda: int(conn.execute(
        "SELECT COUNT(1) FROM dob_complaints WHERE project_code = ? AND UPPER(COALESCE(status,'')) = 'ACTIVE'",
        (code,)).fetchone()[0]), 0)
    certs = _safe(conn, lambda: _one(conn.execute(
        "SELECT SUM(CASE WHEN c.expiration_date < ? THEN 1 ELSE 0 END) AS expired, "
        "       SUM(CASE WHEN c.expiration_date >= ? AND c.expiration_date <= ? THEN 1 ELSE 0 END) AS soon "
        "FROM certifications c JOIN project_assignments a ON a.employee_id = c.employee_id "
        "WHERE a.project_code = ? AND COALESCE(a.status,'active') = 'active' "
        "AND LOWER(COALESCE(c.status,'')) NOT IN ('revoked','void')",
        (t, t, (today + timedelta(days=30)).isoformat(), code))), {}) or {}
    return {"rfis_open": rfis_open, "rfis_overdue": rfis_overdue, "markup_rfis_open": markup_rfis,
            "violations_active": violations, "complaints_active": complaints,
            "certs_expired": int(certs.get("expired") or 0), "certs_expiring_30": int(certs.get("soon") or 0)}


# Key contacts: four groups (Oct 7 C-suite notes). Property managers sit with the Owner,
# every consultant discipline (architect, engineers, expeditor) under Engineer / Architect,
# and the owner's / GC's site people under General contractor.
CONTACT_GROUPS = (("owner", "Owner", ("owner", "property_manager")),
                  ("design", "Engineer / Architect", ("consultant",)),
                  ("gc", "General contractor", ("site_supervisor",)))
CONTACTS_PER_GROUP = 3


def _contacts(conn, code):
    import project_contacts
    d = project_contacts.list_for_project(conn, code, with_pii=True) or {}
    groups, team = d.get("groups") or {}, d.get("team") or {}
    ours = [{"name": p["name"], "role": "Project manager", "org": None, "phone": None, "email": p.get("email")}
            for p in team.get("pms", [])]
    sups = sorted(team.get("supervisors", []), key=lambda x: not x.get("is_primary"))
    ours += [{"name": x["name"], "role": "Superintendent" if x.get("is_primary") else (x.get("title") or "Site supervisor"),
              "org": None, "phone": None, "email": None} for x in sups]
    out = [{"key": "team", "label": "Our team", "people": ours[:CONTACTS_PER_GROUP]}]
    for key, label, roles in CONTACT_GROUPS:
        rows = [x for r in roles for x in groups.get(r, [])]
        rows.sort(key=lambda x: (not x.get("is_primary"), x.get("role") != roles[0]))   # main contact first, then the owner before the PM
        out.append({"key": key, "label": label, "people": [
            {"name": x["name"], "role": x.get("discipline_label") or x.get("title") or project_contacts.ROLES.get(x["role"]),
             "org": x.get("org"), "phone": x.get("phone"), "email": x.get("email"), "primary": bool(x.get("is_primary"))}
            for x in rows[:CONTACTS_PER_GROUP]], "more": max(0, len(rows) - CONTACTS_PER_GROUP)})
    return out


def _open_items(conn, code, today, uid, role):
    """The job's open tasks, plus events that have not ended yet: overdue first, then by date,
    undated last. Anyone who can open the job sees them (the team calendar is shared);
    admin / C-suite add and edit, the assignee ticks their own done."""
    import team_items
    t = today.isoformat()
    rows = team_items.list_items(conn, status="open", project_code=code, me=uid, can_edit=role in FINANCIAL_ROLES)
    rows = [r for r in rows if r["kind"] == "task" or (r.get("calendar_to") or r.get("calendar_from") or t) >= t]
    rows.sort(key=lambda r: (not r["overdue"], r.get("calendar_from") is None, r.get("calendar_from") or "",
                             r.get("start_time") or r.get("due_time") or "99:99"))
    week = (today + timedelta(days=7)).isoformat()
    return {"count": len(rows), "overdue": sum(1 for r in rows if r["overdue"]),
            "events_this_week": sum(1 for r in rows if r["kind"] == "event" and (r.get("calendar_from") or "") <= week),
            "can_add": role in FINANCIAL_ROLES, "items": rows[:25]}


def _photos(conn, code):
    return [{"id": r["id"], "thumb_url": f"/api/field-photos/{r['id']}/thumb", "taken_at": str(r.get("taken_at") or r.get("uploaded_at") or "")[:10],
             "caption": r.get("caption"), "video": str(r.get("mime") or "").startswith("video/")} for r in _rows(conn.execute(
        "SELECT id, taken_at, uploaded_at, caption, mime FROM field_photos WHERE project_code = ? "
        "ORDER BY COALESCE(taken_at, uploaded_at) DESC, id DESC LIMIT 6", (code,)))]


def summary(conn, code: str, role: Optional[str], today: Optional[date] = None, uid: Optional[int] = None) -> Optional[dict]:
    today = today or date.today()
    if not conn.execute("SELECT 1 FROM projects WHERE project_code = ?", (code,)).fetchone():
        return None
    out = {
        "header": _safe(conn, lambda: _header(conn, code, today, role), {"code": code}),
        "schedule": _safe(conn, lambda: _schedule(conn, code), None),
        "field_today": _safe(conn, lambda: _field_today(conn, code, today), None),
        "risk": _safe(conn, lambda: _risk(conn, code, today), None),
        "open_items": _safe(conn, lambda: _open_items(conn, code, today, uid, role), None),
        "contacts": _safe(conn, lambda: _contacts(conn, code), []),
        "photos": _safe(conn, lambda: _photos(conn, code), []),
    }
    if role in FINANCIAL_ROLES:   # never built — not merely hidden — for anyone else
        out["money"] = _safe(conn, lambda: _money(conn, code, today), None)
    return out


# ---------------------------------------------------------------- route
def _api_summary(project_code):
    # reached through /api/projects/<code>/… — the #263 scoping hook has already refused
    # an unassigned pm; external roles never reach the internal namespace
    me = current_user() or {}
    role = me.get("role")
    if role not in ("admin", "c_suite", "pm", "super"):
        return jsonify({"error": "forbidden"}), 403
    conn = db_layer.connect()
    try:
        d = summary(conn, project_code, role, uid=me.get("id"))
        return jsonify({"data": d}) if d else (jsonify({"error": "project not found"}), 404)
    finally:
        conn.close()


def register(app) -> None:
    app.add_url_rule("/api/projects/<project_code>/summary", "api_project_summary", _api_summary, methods=["GET"])
