"""Edit a project's basics after it exists: name, address, client, dates, contract value.

Before this, those were fixed at onboarding — the only post-creation edits were the site /
dates form (Crew & site), billing type, tabs, status and PM assignments. The project CODE never
changes: it is the key on every report, document and DCR number.

  GET /api/projects/<code>/details         internal roles (the #263 scoping hook keeps a pm to
                                           their own jobs); `can_edit` says whether to offer Edit
  PUT /api/admin/projects/<code>/details   admin / c_suite — any subset of:
        name, address, city_zip, client_org_id | new_org_name, start_date, target_completion,
        contract_value

Rules
  * Everything is validated before anything is written (a refused edit changes nothing and never
    leaves a new client company behind).
  * A new address re-links the job (and its approved estimate) to its building
    (buildings.find_or_create — the old building row stays, other jobs may use it).
  * A new client also updates the legacy `projects.client` text the older reports read, moves the
    job's approved estimate, and moves the building unless another client still has a job there.
  * Contract value: an AIA job's contract IS its schedule of values (edit the SOV); otherwise it is
    the converted estimate's final amount, which is what this edits. A job with neither can't take
    one here.
  * Each saved edit lands on the client's timeline ("Project X updated: name, address") so the
    company can see who changed what; values never go to the log.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from flask import jsonify, request

import buildings
import crm
import db_layer
import portal_projects
import project_onboard
from auth import current_user, requires_company, requires_role

log = logging.getLogger("ssc.project_details")

INTERNAL = ("admin", "c_suite", "pm", "super")
LABELS = {"name": "name", "address": "address", "city_zip": "city / ZIP", "client_org_id": "client",
          "start_date": "start date", "target_completion": "target completion", "contract_value": "contract value"}


def _one(cur) -> Optional[dict]:
    r = cur.fetchone()
    return dict(r) if r else None


def _converted_estimate(conn, code: str) -> Optional[dict]:
    return _one(conn.execute("SELECT id, code, final_amount FROM estimate WHERE converted_project_code = ? "
                             "ORDER BY id DESC LIMIT 1", (code,)))


def details(conn, code: str) -> Optional[dict]:
    p = _one(conn.execute("SELECT project_code, name, address, city_zip, status, client_org_id, start_date, "
                          "target_completion, building_id FROM projects WHERE project_code = ?", (code,)))
    if not p:
        return None
    org = crm.get_org(conn, p["client_org_id"]) if p.get("client_org_id") else None
    contract = portal_projects._contract(conn, code)
    est = None if contract.get("source") == "schedule_of_values" else _converted_estimate(conn, code)
    if contract.get("source") == "schedule_of_values":
        note = "Set by the schedule of values — change it there."
    elif est:
        note = "The approved amount on estimate " + (est.get("code") or "") + "." if est.get("code") else "The approved amount on the job's estimate."
    else:
        note = "This job has no approved estimate to hold a contract value."
    return {
        "project_code": p["project_code"], "name": p.get("name"), "address": p.get("address"),
        "city_zip": p.get("city_zip"), "status": p.get("status") or "active",
        "client_org_id": p.get("client_org_id"), "client_name": (org or {}).get("name"),
        "start_date": p.get("start_date"), "target_completion": p.get("target_completion"),
        "contract_value": contract.get("value"), "contract_source": contract.get("source"),
        "contract_editable": bool(est), "contract_note": note,
    }


def _text(p: dict, key: str, label: str, max_len: int, required: bool) -> Optional[str]:
    v = str(p.get(key) or "").strip()
    if required and not v:
        raise ValueError(f"the {label} can't be blank")
    if len(v) > max_len:
        raise ValueError(f"the {label} is too long (max {max_len} characters)")
    return v or None


def update(conn, code: str, p: dict, *, actor=None) -> dict:
    cur = details(conn, code)
    if not cur:
        raise LookupError("project not found")
    want: dict = {}

    # ---- 1. validate everything (nothing is written until all of it passes)
    if "name" in p:
        want["name"] = _text(p, "name", "project name", 200, required=True)
    if "address" in p:
        want["address"] = _text(p, "address", "address", 300, required=True)
    if "city_zip" in p:
        want["city_zip"] = _text(p, "city_zip", "city / ZIP", 120, required=False)
    for k in ("start_date", "target_completion"):
        if k in p:
            want[k] = project_onboard._valid_date(p.get(k))
    start = want.get("start_date", cur.get("start_date"))
    target = want.get("target_completion", cur.get("target_completion"))
    if start and target and str(target)[:10] < str(start)[:10]:
        raise ValueError("target completion cannot be before the start date")

    new_org_name = (p.get("new_org_name") or "").strip()
    if p.get("client_org_id") not in (None, ""):
        try:
            org_id = int(p.get("client_org_id"))
        except (TypeError, ValueError):
            raise ValueError("pick a client")
        if not crm.get_org(conn, org_id):
            raise LookupError("client not found")
        want["client_org_id"] = org_id
    elif new_org_name:
        if len(new_org_name) > 200:
            raise ValueError("the client name is too long (max 200 characters)")
        want["client_org_id"] = None            # resolved in step 2, after every check passed

    if "contract_value" in p and p.get("contract_value") not in (None, ""):
        try:
            amount = round(float(str(p.get("contract_value")).replace(",", "").replace("$", "").strip()), 2)
        except ValueError:
            raise ValueError("the contract value must be a number")
        if amount < 0 or amount > 1e10:
            raise ValueError("the contract value is out of range")
        if not cur["contract_editable"]:
            raise ValueError(cur["contract_note"])
        want["contract_value"] = amount

    # ---- 2. write
    if "client_org_id" in want and want["client_org_id"] is None:
        want["client_org_id"] = project_onboard._existing_client_id(conn, new_org_name) or crm.create_org(
            conn, name=new_org_name, relationship_type="client", created_by=actor)
    changed = [k for k, v in want.items()
               if (str(v) if v is not None else None) != (str(cur.get(k)) if cur.get(k) is not None else None)]
    if not changed:
        return details(conn, code)

    now = datetime.now().isoformat(timespec="seconds")
    cols = [k for k in changed if k in ("name", "address", "city_zip", "client_org_id", "start_date", "target_completion")]
    if cols:
        conn.execute(f"UPDATE projects SET {', '.join(k + ' = ?' for k in cols)}, updated_at = ? WHERE project_code = ?",
                     (*[want[k] for k in cols], now, code))
    if "address" in changed or "city_zip" in changed:
        bid = buildings.find_or_create(conn, want.get("address", cur.get("address")),
                                       client_org_id=want.get("client_org_id", cur.get("client_org_id")),
                                       city_zip=want.get("city_zip", cur.get("city_zip")))
        if bid:
            conn.execute("UPDATE projects SET building_id = ? WHERE project_code = ?", (bid, code))
            # the job's approved estimate is at the same address — keep them on one building
            conn.execute("UPDATE estimate SET building_id = ? WHERE converted_project_code = ?", (bid, code))
    if "client_org_id" in changed:
        new_org = want["client_org_id"]
        org = crm.get_org(conn, new_org) or {}
        conn.execute("UPDATE projects SET client = ? WHERE project_code = ?", (org.get("name"), code))   # legacy text the older reports read
        # the job's approved estimate follows it (won value counts under the new client) …
        conn.execute("UPDATE estimate SET client_org_id = ? WHERE converted_project_code = ?", (new_org, code))
        # … and so does its building, unless another client still has a job there
        row = _one(conn.execute("SELECT building_id FROM projects WHERE project_code = ?", (code,)))
        bid = (row or {}).get("building_id")
        if bid and not _one(conn.execute("SELECT 1 AS x FROM projects WHERE building_id = ? AND project_code <> ? "
                                         "AND client_org_id IS NOT NULL AND client_org_id <> ?", (bid, code, new_org))):
            conn.execute("UPDATE building SET client_org_id = ?, updated_at = ? WHERE id = ?", (new_org, now, bid))
    if "contract_value" in changed:
        est = _converted_estimate(conn, code)
        conn.execute("UPDATE estimate SET final_amount = ?, updated_at = ? WHERE id = ?", (want["contract_value"], now, est["id"]))
    conn.commit()

    # ---- 3. the record: the client's timeline (old and new client when it moved)
    summary = f"Project {code} updated: " + ", ".join(LABELS[k] for k in changed)
    for org_id in {cur.get("client_org_id"), want.get("client_org_id")} - {None}:
        try:
            crm.add_activity(conn, entity_type="organization", entity_id=org_id, activity_type="system",
                             summary=summary, author_user_id=actor)
        except Exception as e:   # the edit is saved; the timeline line is a courtesy
            log.warning("project details activity not logged: %s", e)
            try:
                conn.rollback()
            except Exception:
                pass
    log.info("project details updated code=%s fields=%s by user=%s", code, ",".join(changed), actor)
    return details(conn, code)


# ---------------------------------------------------------------- routes
@requires_role(*INTERNAL)
def _api_get(project_code):
    conn = db_layer.connect()
    try:
        d = details(conn, project_code)
        if not d:
            return jsonify({"error": "project not found"}), 404
        d["can_edit"] = (current_user() or {}).get("role") in ("admin", "c_suite")
        return jsonify({"data": d})
    finally:
        conn.close()


@requires_company
def _api_put(project_code):
    body = request.get_json(silent=True) or {}
    conn = db_layer.connect()
    try:
        try:
            d = update(conn, project_code, body, actor=(current_user() or {}).get("id"))
        except LookupError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        d["can_edit"] = True
        return jsonify({"data": d})
    finally:
        conn.close()


def register(app) -> None:
    app.add_url_rule("/api/projects/<project_code>/details", "project_details_get", _api_get, methods=["GET"])
    app.add_url_rule("/api/admin/projects/<project_code>/details", "project_details_put", _api_put, methods=["PUT"])
