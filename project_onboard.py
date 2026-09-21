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
        "regions": [{"code": c, "label": l} for c, l in estimates.BOROUGH_LABELS.items()],   # #331
        "clients": clients,
        "pms": [{"user_id": p["id"], "name": p.get("display_name") or p.get("full_name") or p.get("email")} for p in pms],
        "workers": active_workers(conn),   # #325 — the crew picker
        "geofence_default_m": GEOFENCE_DEFAULT,
    }


GEOFENCE_DEFAULT = 200


def active_workers(conn) -> list:
    """The canonical active roster (CLAUDE.md roster rule) for the crew pickers."""
    try:
        rows = _rows(conn.execute("SELECT employee_id, worker_id, name, trade FROM v_active_workers ORDER BY worker_id, name"))
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        rows = _rows(conn.execute("SELECT employee_id, worker_id, name, trade FROM employees WHERE archived_at IS NULL "
                                  "ORDER BY worker_id, name"))
    return [{"employee_id": r["employee_id"], "worker_id": r.get("worker_id"), "name": r.get("name"),
             "trade": r.get("trade")} for r in rows]


def _site_fields(p: dict, *, partial: bool = False) -> dict:
    """site_lat / site_lng (both or neither) + geofence_m. Returns only the keys present."""
    out = {}
    lat, lng = p.get("site_lat"), p.get("site_lng")
    if "site_lat" in p or "site_lng" in p or not partial:
        if lat in (None, "") and lng in (None, ""):
            if "site_lat" in p or "site_lng" in p:
                out["site_lat"] = None
                out["site_lng"] = None
        else:
            try:
                lat, lng = float(lat), float(lng)
            except (TypeError, ValueError):
                raise ValueError("site latitude and longitude must both be numbers")
            if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
                raise ValueError("site coordinates look wrong")
            out["site_lat"], out["site_lng"] = round(lat, 6), round(lng, 6)
    if "geofence_m" in p and p.get("geofence_m") not in (None, ""):
        try:
            m = int(float(p["geofence_m"]))
        except (TypeError, ValueError):
            raise ValueError("geofence radius must be a whole number of meters")
        if not (25 <= m <= 5000):
            raise ValueError("geofence radius must be between 25 and 5000 meters")
        out["geofence_m"] = m
    return out


def _crew_ids(conn, p: dict) -> Optional[list]:
    ids = p.get("crew")
    if ids is None:
        return None
    if not isinstance(ids, list):
        raise ValueError("crew must be a list of employee ids")
    ids = [str(i).strip() for i in ids if str(i).strip()]
    if not ids:
        return []
    active = {w["employee_id"] for w in active_workers(conn)}
    bad = [i for i in ids if i not in active]
    if bad:
        raise LookupError("crew includes a worker who is not on the active roster")
    return sorted(set(ids))


def _apply_crew(conn, code: str, ids: list, *, today: Optional[str] = None) -> dict:
    """Make the ACTIVE assignments of `code` equal to `ids`: new ones start today,
    dropped ones are off-rolled with an end date (never deleted — hours history)."""
    today = today or date.today().isoformat()
    current = {r["employee_id"] for r in _rows(conn.execute(
        "SELECT employee_id FROM project_assignments WHERE project_code = ? AND status = 'active'", (code,)))}
    added = [i for i in ids if i not in current]
    removed = [i for i in current if i not in ids]
    for eid in added:
        conn.execute("INSERT INTO project_assignments (project_code, employee_id, role_on_project, start_date, status) "
                     "VALUES (?,?,?,?,'active')", (code, eid, None, today))
    for eid in removed:
        conn.execute("UPDATE project_assignments SET status = 'off-rolled', end_date = ?, updated_at = CURRENT_TIMESTAMP "
                     "WHERE project_code = ? AND employee_id = ? AND status = 'active'", (today, code, eid))
    conn.commit()
    return {"added": added, "removed": removed}


def crew(conn, code: str) -> Optional[dict]:
    p = _one(conn.execute("SELECT project_code, name, status, site_lat, site_lng, geofence_m, start_date, target_completion "
                          "FROM projects WHERE project_code = ?", (code,)))
    if not p:
        return None
    assigned = {r["employee_id"] for r in _rows(conn.execute(
        "SELECT employee_id FROM project_assignments WHERE project_code = ? AND status = 'active'", (code,)))}
    workers = active_workers(conn)
    for w in workers:
        w["assigned"] = w["employee_id"] in assigned
    return {"project": p, "workers": workers, "assigned_count": len(assigned),
            "site": {"site_lat": p.get("site_lat"), "site_lng": p.get("site_lng"),
                     "geofence_m": p.get("geofence_m") or GEOFENCE_DEFAULT},
            "dates": {"start_date": p.get("start_date"), "target_completion": p.get("target_completion")}}   # #332


def set_crew(conn, code: str, payload: dict, *, actor=None) -> dict:
    p = payload or {}
    if not _one(conn.execute("SELECT project_code FROM projects WHERE project_code = ?", (code,))):
        raise LookupError("project not found")
    site = _site_fields(p, partial=True)
    ids = _crew_ids(conn, p)
    # #332 — the job's dates (what the calendar draws) are edited here too
    cur = _one(conn.execute("SELECT start_date, target_completion FROM projects WHERE project_code = ?", (code,))) or {}
    if "start_date" in p:
        site["start_date"] = _valid_date(p.get("start_date"))
    if "target_completion" in p:
        site["target_completion"] = _valid_date(p.get("target_completion"))
    new_start = site.get("start_date", cur.get("start_date"))
    new_end = site.get("target_completion", cur.get("target_completion"))
    if new_start and new_end and new_end < new_start:
        raise ValueError("target completion cannot be before the start date")
    if site:
        sets = ", ".join(f"{k} = ?" for k in site)
        conn.execute(f"UPDATE projects SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE project_code = ?", (*site.values(), code))
        conn.commit()
    change = _apply_crew(conn, code, ids) if ids is not None else {"added": [], "removed": []}
    out = crew(conn, code)
    out["change"] = change
    return out


def onboard(conn, payload: dict, *, actor=None) -> dict:
    p = payload or {}
    est_type = (p.get("est_type") or "").strip().upper()
    borough = (p.get("borough") or "").strip().upper()
    if est_type not in estimates.EST_TYPES:
        raise ValueError("pick the job type")
    if borough not in estimates.BOROUGHS:
        raise ValueError("pick the borough or region")
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
    target = _valid_date(p.get("target_completion"))          # #332 — the calendar bar's end
    if target and target < start:
        raise ValueError("target completion cannot be before the start date")
    notes = (p.get("notes") or "").strip()[:2000]
    site = _site_fields(p, partial=True)          # #325 — validated before anything is written
    crew_ids = _crew_ids(conn, p)

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
        conn.execute("UPDATE projects SET start_date = ?, target_completion = ? WHERE project_code = ?", (start, target, code))
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
    if site:                                        # #325 — the worker app's geofence for this job
        sets = ", ".join(f"{k} = ?" for k in site)
        conn.execute(f"UPDATE projects SET {sets} WHERE project_code = ?", (*site.values(), code))
        conn.commit()
    crew_change = _apply_crew(conn, code, crew_ids, today=start) if crew_ids else {"added": [], "removed": []}
    try:
        crm.add_activity(conn, entity_type="organization", entity_id=org_id, activity_type="system",
                         author_user_id=actor, function_tag="sales",
                         summary=f"Project {code} onboarded directly — {name}"
                                 + (f" ({'${:,.0f}'.format(contract)})" if contract else ""))
    except Exception as e:
        log.info("#324 activity not logged: %s", e)
    try:   # #331 — the auto-created job site carries the region label when it has none yet
        conn.execute("UPDATE building SET borough = ? WHERE id = (SELECT building_id FROM projects WHERE project_code = ?) "
                     "AND COALESCE(borough, '') = ''", (estimates.BOROUGH_LABELS.get(borough, borough), code))
        conn.commit()
    except Exception as e:
        log.info("#331 region label not stamped on the job site: %s", e)
    project = _one(conn.execute("SELECT project_code, name, address, status, client_org_id, building_id, "
                                "site_lat, site_lng, geofence_m FROM projects WHERE project_code = ?", (code,)))
    return {"project_code": code, "project": project, "estimate_id": est["id"], "estimate_code": est["code"],
            "client_org_id": org_id, "pm_user_id": pm_user_id, "start_date": start, "target_completion": target, "contract_value": contract,
            "crew": crew_change["added"]}


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


@requires_company
def _api_crew_get(project_code):
    conn = db_layer.connect()
    try:
        d = crew(conn, project_code)
        return (jsonify({"data": d}) if d else (jsonify({"error": "project not found"}), 404))
    finally:
        conn.close()


@requires_company
def _api_crew_set(project_code):
    body = request.get_json(silent=True) or {}
    u = current_user() or {}
    conn = db_layer.connect()
    try:
        try:
            return jsonify({"data": set_crew(conn, project_code, body, actor=u.get("id"))})
        except LookupError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    finally:
        conn.close()


def register(app) -> None:
    conn = db_layer.connect()
    try:
        import apply_project_site_325
        apply_project_site_325.ensure(conn)          # #325 — site columns heal at boot
        import apply_project_dates_334
        apply_project_dates_334.ensure(conn)         # #334 — start_date / target_completion heal at boot (preprod lacked them)
    except Exception as e:   # a fresh preprod DB may migrate later; never block boot
        log.warning("#325 project site columns ensure skipped: %s", e)
    finally:
        conn.close()
    app.add_url_rule("/api/admin/projects/onboard-meta", "projects_onboard_meta", _api_meta, methods=["GET"])
    app.add_url_rule("/api/admin/projects/onboard", "projects_onboard", _api_onboard, methods=["POST"])
    app.add_url_rule("/api/admin/projects/<project_code>/crew", "projects_crew_get", _api_crew_get, methods=["GET"])
    app.add_url_rule("/api/admin/projects/<project_code>/crew", "projects_crew_set", _api_crew_set, methods=["PUT"])
