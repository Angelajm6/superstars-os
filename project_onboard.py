"""#324 — Onboard a job that is already won: "+ New project" on Projects & assignments.

Until now every project had to travel estimate → approved → convert. That is right
for a bid, wrong for a job won before the dashboard existed or agreed verbally. This
module creates the project in one step WITHOUT bypassing the pipeline's books:

  1. an estimate is created in the job's series (FR-BX-###, same numbering rule),
  2. it is stamped approved on the start date with the contract value and the note
     "onboarded directly" — Finance's won / pipeline numbers stay truthful,
  3. `estimates.convert_to_project` turns it into the active project (building
     auto-linked, IRA checklist opened for rope-access jobs),
  4. the project takes the operator's name and start date, and the chosen PM is
     assigned (`pm_project_assignment`).

Endpoint: POST /api/admin/projects/onboard (admin / c_suite via `requires_company`);
GET /api/admin/projects/onboard-meta feeds the modal's selects.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from flask import jsonify, request

import crm
import db_layer
import estimates
from auth import current_user, requires_company

log = logging.getLogger("ssc.project_onboard")

NOTE = "Onboarded directly as an already-won job (no bid in the pipeline)."


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _one(cur) -> Optional[dict]:
    r = cur.fetchone()
    return dict(r) if r else None


def _rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]


def _valid_date(v) -> Optional[str]:
    if v in (None, ""):
        return None
    s = str(v).strip()[:10]
    try:
        date.fromisoformat(s)
    except ValueError:
        raise ValueError("dates must be YYYY-MM-DD")
    return s


def meta(conn) -> dict:
    clients = [{"id": o["id"], "name": o["name"]} for o in crm.list_orgs(conn, relationship_type="client")
               if (o.get("status") or "active") == "active"]
    pms = _rows(conn.execute(
        "SELECT id, display_name, full_name, email FROM users WHERE role = 'pm' AND COALESCE(is_active, 1) = 1 "
        "AND COALESCE(status, 'active') = 'active' AND COALESCE(is_system, 0) = 0 "   # same filter as the assignments list
        "ORDER BY LOWER(COALESCE(display_name, full_name, email))"))
    return {
        "types": [{"code": k, "label": v} for k, v in estimates.EST_TYPES.items()],
        "boroughs": list(estimates.BOROUGHS),
        "clients": clients,
        "pms": [{"user_id": p["id"], "name": p.get("display_name") or p.get("full_name") or p.get("email")} for p in pms],
    }


def onboard(conn, payload: dict, *, actor=None) -> dict:
    p = payload or {}
    est_type = (p.get("est_type") or "").strip().upper()
    borough = (p.get("borough") or "").strip().upper()
    if est_type not in estimates.EST_TYPES:
        raise ValueError("pick the job type")
    if borough not in estimates.BOROUGHS:
        raise ValueError("pick the borough")
    address = (p.get("address") or "").strip()
    if len(address) < 4:
        raise ValueError("the building address is required")
    name = (p.get("name") or "").strip()[:200] or address
    contract = p.get("contract_value")
    if contract in (None, ""):
        contract = None
    else:
        try:
            contract = round(float(str(contract).replace(",", "").replace("$", "")), 2)
        except (TypeError, ValueError):
            raise ValueError("contract value must be a number")
        if contract < 0:
            raise ValueError("contract value cannot be negative")
    start = _valid_date(p.get("start_date")) or date.today().isoformat()
    notes = (p.get("notes") or "").strip()[:2000]

    # ---- client: existing org or a new one
    org_id = p.get("client_org_id")
    new_org = (p.get("new_org_name") or "").strip()
    if org_id in (None, "") and new_org:
        org_id = crm.create_org(conn, name=new_org, relationship_type="client", created_by=actor)
    try:
        org_id = int(org_id)
    except (TypeError, ValueError):
        raise ValueError("pick a client or type a new client name")
    if not crm.get_org(conn, org_id):
        raise LookupError("client not found")

    # ---- PM (optional)
    pm_user_id = p.get("pm_user_id")
    if pm_user_id in (None, ""):
        pm_user_id = None
    else:
        try:
            pm_user_id = int(pm_user_id)
        except (TypeError, ValueError):
            raise ValueError("pm_user_id must be an integer")
        pm = _one(conn.execute("SELECT id FROM users WHERE id = ? AND role = 'pm' AND COALESCE(is_active,1) = 1",
                               (pm_user_id,)))
        if not pm:
            raise LookupError("that project manager is not an active PM user")

    # ---- 1. the estimate in the series, 2. approved on the start date
    est = estimates.create_estimate(conn, est_type=est_type, borough=borough, client_org_id=org_id,
                                    building_address=address, notes=(NOTE + (" " + notes if notes else "")),
                                    created_by=actor)
    now = _now()
    conn.execute("UPDATE estimate SET status = 'approved', final_amount = ?, submitted_date = ?, decided_date = ?, "
                 "status_changed_at = ?, updated_at = ? WHERE id = ?",
                 (contract, start, start, f"{start}T00:00:00" if start != date.today().isoformat() else now, now, est["id"]))
    conn.commit()

    # ---- 3. convert (building auto-link, IRA checklist), 4. name / start / PM
    res = estimates.convert_to_project(conn, est["id"], actor_user_id=actor)
    code = res["project_code"]
    conn.execute("UPDATE projects SET name = ?, updated_at = ? WHERE project_code = ?", (name, now, code))
    try:
        conn.execute("UPDATE projects SET start_date = ? WHERE project_code = ?", (start, code))
    except Exception as e:   # projects.start_date exists only where the legacy ALTER ran
        log.info("#324 projects.start_date not set: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        conn.execute("UPDATE projects SET name = ?, updated_at = ? WHERE project_code = ?", (name, now, code))
    if pm_user_id:
        try:
            conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_by, assigned_at) "
                         "VALUES (?,?,?,?)", (pm_user_id, code, actor, now))
        except Exception as e:
            log.warning("#324 PM assignment not written: %s", e)
            try:
                conn.rollback()
            except Exception:
                pass
    conn.commit()
    try:
        crm.add_activity(conn, entity_type="organization", entity_id=org_id, activity_type="system",
                         author_user_id=actor, function_tag="sales",
                         summary=f"Project {code} onboarded directly — {name}"
                                 + (f" ({'${:,.0f}'.format(contract)})" if contract else ""))
    except Exception as e:
        log.info("#324 activity not logged: %s", e)
    project = _one(conn.execute("SELECT project_code, name, address, status, client_org_id, building_id "
                                "FROM projects WHERE project_code = ?", (code,)))
    return {"project_code": code, "project": project, "estimate_id": est["id"], "estimate_code": est["code"],
            "client_org_id": org_id, "pm_user_id": pm_user_id, "start_date": start, "contract_value": contract}


# ---------------------------------------------------------------- routes
@requires_company
def _api_meta():
    conn = db_layer.connect()
    try:
        return jsonify({"data": meta(conn)})
    finally:
        conn.close()


@requires_company
def _api_onboard():
    body = request.get_json(silent=True) or {}
    u = current_user() or {}
    conn = db_layer.connect()
    try:
        try:
            return jsonify({"data": onboard(conn, body, actor=u.get("id"))}), 201
        except LookupError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    finally:
        conn.close()


def register(app) -> None:
    app.add_url_rule("/api/admin/projects/onboard-meta", "projects_onboard_meta", _api_meta, methods=["GET"])
    app.add_url_rule("/api/admin/projects/onboard", "projects_onboard", _api_onboard, methods=["POST"])
