"""#360 — the Client portal tab: who has access to the client portal and how it is set up.

Before this, the answer was spread over three screens (Team & access for the login,
Projects → Assignments & onboarding for the project + section toggles, the project
dashboard for per-item sharing). This module is ONE read that joins them for the
company console; every WRITE the tab makes goes through the endpoints that already own
it, with their own gates unchanged:

  * link / change the client's project  PUT  /api/admin/pm-assignments      (admin, c_suite)
  * unlock / lock a section             POST /api/admin/client-grants        (admin, c_suite)
  * apply a preset                      POST /api/admin/client-grants/preset (admin, c_suite)
  * reset a client's password           POST /api/admin/users/<id>/reset-password (admin, c_suite for clients)
  * create a client login               POST /api/admin/users                (admin only)

GET /api/company/portal-access (@requires_company). Payload = display name, email, role,
account state, last sign-in, the linked project + its client company, and the section
names. Internal roles only; nothing here is logged (emails never enter a log line).
"""
from __future__ import annotations

from flask import jsonify

import client_grants
import portal_matrix
from auth import _db, current_user, requires_company

EXTERNAL_ROLES = ("client", "architect", "vendor")
ROLE_LABELS = {"client": "Client", "architect": "Architect / Engineer", "vendor": "Vendor"}


def _setup_state(u, project_code, sections) -> tuple[str, str]:
    """(key, label) — the ONE thing that is next for this login, in the order it has to
    happen. A client is live only when all four are true."""
    if u["status"] != "active" or not u["is_active"]:
        return "inactive", "Deactivated"
    if not project_code:
        return "no_project", "Link a project"
    if u["role"] == "client" and not sections:
        return "locked", "Nothing unlocked yet"
    if u["must_reset_password"] or not u["last_login_at"]:
        return "invited", "Hasn't signed in yet"
    return "live", "Live"


def _preset_of(sections) -> str | None:
    have = set(sections)
    for name, bundle in client_grants.PRESETS.items():
        if have == set(bundle):
            return name
    return None


def build(conn) -> dict:
    marks = ",".join("?" * len(EXTERNAL_ROLES))
    users = conn.execute(
        "SELECT id, email, role, full_name, display_name, status, is_active, "
        "       must_reset_password, created_at, last_login_at "
        f"FROM users WHERE role IN ({marks}) ORDER BY COALESCE(display_name, full_name, email)",
        EXTERNAL_ROLES).fetchall()
    ids = [u["id"] for u in users]
    links: dict[int, str] = {}
    if ids:
        for r in conn.execute(
                "SELECT user_id, MIN(project_code) AS project_code FROM pm_project_assignment "
                f"WHERE user_id IN ({','.join('?' * len(ids))}) GROUP BY user_id", ids).fetchall():
            links[r["user_id"]] = r["project_code"]
    projects = {}
    for p in conn.execute(
            "SELECT p.project_code, p.name, p.status, o.name AS org_name "
            "FROM projects p LEFT JOIN crm_organization o ON o.id = p.client_org_id").fetchall():
        projects[p["project_code"]] = p
    grants = client_grants.grants_by_user(conn, [u["id"] for u in users if u["role"] == "client"])
    order = {s: i for i, s in enumerate(client_grants.SECTIONS)}

    clients, others = [], []
    for u in users:
        code = links.get(u["id"])
        p = projects.get(code) if code else None
        if u["role"] == "client":
            secs = sorted(grants.get(u["id"], []), key=lambda s: order.get(s, 99))
        else:
            # no grant machinery for other external roles — show what their role opens
            secs = [s for s in client_grants.SECTIONS if s in portal_matrix.possible_sections(u["role"])]
        state, state_label = _setup_state(u, code, secs)
        row = {
            "user_id": u["id"], "name": u["display_name"] or u["full_name"] or u["email"],
            "email": u["email"], "role": u["role"], "role_label": ROLE_LABELS.get(u["role"], u["role"]),
            "active": bool(u["status"] == "active" and u["is_active"]),
            "created_at": u["created_at"], "last_login_at": u["last_login_at"],
            "project_code": code, "project_name": p["name"] if p else None,
            "org_name": p["org_name"] if p else None,
            "sections": secs, "preset": _preset_of(secs) if u["role"] == "client" else None,
            "state": state, "state_label": state_label,
        }
        (clients if u["role"] == "client" else others).append(row)

    active = [c for c in clients if c["active"]]
    return {
        "clients": clients,
        "others": others,
        "counts": {
            "clients": len(active),
            "live": sum(1 for c in active if c["state"] == "live"),
            "invited": sum(1 for c in active if c["state"] == "invited"),
            "needs_setup": sum(1 for c in active if c["state"] in ("no_project", "locked")),
            "projects": len({c["project_code"] for c in active if c["project_code"] and c["sections"]}),
        },
        # the toggles the tab renders = grantable ∩ served (same rule as #283)
        "sections": [s for s in client_grants.SECTIONS if s in client_grants.SERVED_SECTIONS],
        "section_labels": dict(client_grants.SECTION_LABELS),
        "presets": {k: list(v) for k, v in client_grants.PRESETS.items()},
        "preset_labels": dict(client_grants.PRESET_LABELS),
        "projects": [{"project_code": c, "name": p["name"], "org_name": p["org_name"]}
                     for c, p in sorted(projects.items())
                     if (p["status"] or "active").lower() != "closed"],
        "can_create": (current_user() or {}).get("role") == "admin",
    }


@requires_company
def _api_portal_access():
    conn = _db()
    try:
        return jsonify({"data": build(conn)})
    finally:
        conn.close()


def register(app) -> None:
    app.add_url_rule("/api/company/portal-access", "company_portal_access",
                     _api_portal_access, methods=["GET"])
