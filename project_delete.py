"""Delete a project for good — mistake correction, same rule as deleting a client.

  DELETE /api/admin/projects/<code>   admin / c_suite

A job can go only while nothing real is filed under it: no daily report, sign-in, photo,
document, invoice, expense, RFI, drawing … Every table with a `project_code` column counts,
found at run time, so a table added later protects its rows without touching this file. A
job with records is refused (409) with what is still there, in words ("3 daily reports,
1 invoice"); the answer is Status → closed, which keeps the history.

What goes with the job (setup, not records): its crew and PM assignments, client-portal
grants, worker-app sessions, the IRA checklist row and the DOB data synced from NYC Open
Data. Its estimate: the one "+ New project" made on the fly is deleted; a real bid that was
converted goes back to approved, so it can be converted again. The building stays (other
jobs and the client may use it). The job number is not reused while a higher one exists.
"""
from __future__ import annotations

import logging
from typing import Optional

from flask import jsonify

import db_layer
from auth import current_user, requires_company

log = logging.getLogger("ssc.project_delete")

# set up with the job, deleted with it
SETUP_TABLES = ("project_assignments", "employee_assignments", "pm_project_assignment", "client_section_grant",
                "worker_app_session", "ira_job", "project_doc_suggest_dismissed",
                "dob_permits", "dob_violations", "dob_complaints")

# how a blocker reads; any other table reads as "<n> <table> records"
WORDS = {
    "report_index": "daily report", "sign_in_log": "sign-in", "sign_in_edit": "sign-in edit", "work_log": "work log entry",
    "weather_log": "weather entry", "deliveries": "delivery", "equipment_log": "equipment entry",
    "toolbox_talk_records": "toolbox talk", "safety_events": "safety event", "issues": "issue", "inspections": "inspection",
    "photos": "photo", "field_photos": "field photo", "rfi_log": "RFI", "rfi": "RFI", "visitors": "visitor",
    "meeting_schedule": "meeting", "meeting_records": "meeting record", "site_closure_log": "site closure",
    "project_documents": "document", "project_doc_intake": "document waiting for approval", "document_library": "document",
    "permits_library": "permit", "document_package": "document package", "drawing_set": "drawing set",
    "elevation": "elevation", "comment": "comment", "drops": "drop", "drop_plan": "drop plan",
    "invoice": "invoice", "pay_application": "pay application", "billing_sov": "schedule of values line",
    "sov_line_items": "schedule of values line", "payable": "bill", "expenses": "expense", "expense_entries": "expense",
    "project_expense": "expense", "material": "material", "material_txn": "material entry",
    "expected_delivery": "expected delivery", "lookahead_activity": "lookahead activity",
    "lookahead_schedule": "lookahead entry", "ira_visit": "IRA visit", "building_geometry": "3D model",
}
_PLURAL = {"delivery": "deliveries", "entry": "entries", "activity": "activities"}

_ONBOARD_NOTE = "Onboarded directly as an already-won job"   # project_onboard.NOTE
# anything here pointing at the job's estimate keeps the estimate (it goes back to approved)
_ESTIMATE_REFS = ("estimate_document", "proposal", "invoice", "walkthrough_visit", "walkthrough_report")


def _plural(word: str, n: int) -> str:
    if n == 1:
        return word
    if word.endswith("waiting for approval"):
        return word.replace("document", "documents", 1)
    last = word.split(" ")[-1]
    return word[: -len(last)] + _PLURAL.get(last, last + "s")


def _project_tables(conn) -> list:
    if db_layer.is_postgres():
        rows = conn.execute("SELECT c.table_name AS t FROM information_schema.columns c "
                            "JOIN information_schema.tables t ON t.table_name = c.table_name AND t.table_schema = c.table_schema "
                            "WHERE c.table_schema = 'public' AND c.column_name = 'project_code' "
                            "AND t.table_type = 'BASE TABLE'").fetchall()
        return sorted(r["t"] for r in rows)
    out = []
    for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        if any(c[1] == "project_code" for c in conn.execute(f"PRAGMA table_info('{r[0]}')").fetchall()):
            out.append(r[0])
    return sorted(out)


def blockers(conn, code: str) -> list:
    """What real records the job still has, as human words. Empty = deletable."""
    counts = {}
    for t in _project_tables(conn):
        if t == "projects" or t in SETUP_TABLES:
            continue
        n = int(conn.execute(f"SELECT COUNT(1) FROM {t} WHERE project_code = ?", (code,)).fetchone()[0] or 0)
        if n:
            word = WORDS.get(t) or t.replace("_", " ") + " record"
            counts[word] = counts.get(word, 0) + n
    return [f"{n} {_plural(w, n)}" for w, n in counts.items()]


def delete(conn, code: str, *, actor=None) -> Optional[dict]:
    """None = no such job; {"blockers": [...]} = refused, nothing changed; else {"deleted": code}."""
    p = conn.execute("SELECT project_code, name FROM projects WHERE project_code = ?", (code,)).fetchone()
    if not p:
        return None
    b = blockers(conn, code)
    if b:
        return {"blockers": b}
    tables = set(_project_tables(conn))
    for t in SETUP_TABLES:
        if t in tables:
            conn.execute(f"DELETE FROM {t} WHERE project_code = ?", (code,))
    from apply_crm_266 import _table_exists
    refs = [t for t in _ESTIMATE_REFS if _table_exists(conn, t)]
    for e in conn.execute("SELECT id, notes FROM estimate WHERE converted_project_code = ?", (code,)).fetchall():
        e = dict(e)
        made_here = (e.get("notes") or "").startswith(_ONBOARD_NOTE) and not any(conn.execute(
            f"SELECT 1 FROM {t} WHERE estimate_id = ?", (e["id"],)).fetchone() for t in refs)
        if made_here:
            conn.execute("DELETE FROM estimate_ira WHERE estimate_id = ?", (e["id"],))
            conn.execute("DELETE FROM estimate WHERE id = ?", (e["id"],))
        else:
            conn.execute("UPDATE estimate SET status = 'approved', converted_project_code = NULL, "
                         "updated_at = CURRENT_TIMESTAMP WHERE id = ?", (e["id"],))
    conn.execute("DELETE FROM projects WHERE project_code = ?", (code,))
    conn.commit()
    log.info("project deleted code=%s by user=%s", code, actor)
    return {"deleted": code}


# ---------------------------------------------------------------- routes
@requires_company
def _api_delete(project_code):
    conn = db_layer.connect()
    try:
        try:
            out = delete(conn, project_code, actor=(current_user() or {}).get("id"))
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        if out is None:
            return jsonify({"error": "project not found"}), 404
        if out.get("blockers"):
            return jsonify({"error": "This job has " + ", ".join(out["blockers"])
                            + ". Set its status to closed instead.", "blockers": out["blockers"]}), 409
        return jsonify({"data": out})
    finally:
        conn.close()


def register(app) -> None:
    app.add_url_rule("/api/admin/projects/<project_code>", "project_delete", _api_delete, methods=["DELETE"])
