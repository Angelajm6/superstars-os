"""Shared helpers for the behavioral suites (#301+): an isolated database and
FK-safe cleanup that behaves the same on SQLite and Postgres.

    from _isolated_db import build_isolated_sqlite, purge_users, purge

* build_isolated_sqlite() — schema.sql + the fixture migration lists into a temp
  dir; returns a `sqlite:` URL. Suites call it only when SSC_DB_URL is unset (the
  CI Postgres leg and the #260 gate set it).
* purge_users(conn, ids) — delete users and EVERY row that references them.
  Postgres enforces the FKs the SQLite fixture only declares, so cleanup must
  delete dependents first; each dependent table is optional (try/except).
* purge(conn, table, column, values) — tolerant delete.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (table, column) pairs that point at users.id — order does not matter, every
# delete is wrapped. Add here when a new table references users.
USER_DEPENDENTS = (
    ("sessions", "user_id"), ("login_audit", "user_id"), ("password_reset_token", "user_id"),
    ("audit_log", "actor_user_id"), ("role_change_audit", "actor_user_id"), ("role_change_audit", "target_user_id"),
    ("pm_project_assignment", "user_id"), ("pm_project_assignment", "assigned_by"),
    ("client_section_grant", "user_id"), ("client_section_grant", "granted_by"),
    ("crm_activity", "author_user_id"), ("crm_task_attendee", "user_id"), ("crm_task", "assignee_user_id"), ("crm_task", "created_by"),
    ("user_calendar_event", "user_id"),
    ("crm_organization", "created_by"), ("crm_contact", "created_by"),
    ("estimate", "created_by"), ("estimate_document", "uploaded_by"),
    ("project_documents", "uploaded_by_uid"), ("project_expense", "entered_by_uid"), ("project_expense", "voided_by_uid"),
    ("expenses", "created_by_uid"), ("expenses", "reviewed_by_uid"),
    ("labor_rate_change", "submitted_by_uid"), ("labor_rate_change", "decided_by_uid"),
    ("field_photo_reassign", "actor_uid"), ("sign_in_edit", "edited_by_uid"),
    ("notification_log", "user_id"), ("dashboard_layouts", "user_id"), ("worker_device", "provisioned_by_uid"),
    ("company_document", "created_by"), ("company_document", "updated_by"),
    ("rfi", "raised_by_uid"), ("comment", "author_uid"),
)


def build_isolated_sqlite(tmp: Path | None = None) -> str:
    import preprod_fixture_bootstrap as pfb
    tmp = tmp or Path(tempfile.mkdtemp(prefix="ssc_isolated_"))
    src = tmp / "source"
    shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", "superstars.db*", "venv", "node_modules",
        "worker_records", "data_room", ".github"))
    db = src / "superstars.db"
    with sqlite3.connect(db) as conn:
        conn.executescript((src / "schema.sql").read_text(encoding="utf-8"))
    env = os.environ.copy()
    env.pop("SSC_DB_URL", None)
    env.pop("SSC_DATA_ROOT", None)
    env["SSC_ALLOW_EMPTY_PREPROD_FIXTURE"] = "1"
    for script in pfb.LEGACY_MIGRATIONS + pfb.DUAL_BACKEND_MIGRATIONS:
        subprocess.run([sys.executable, script], cwd=src, env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"sqlite:{db}"


def purge(conn, table: str, column: str, values) -> None:
    values = list(values)
    if not values:
        return
    marks = ",".join("?" * len(values))
    try:
        conn.execute(f"DELETE FROM {table} WHERE {column} IN ({marks})", values)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def purge_users(conn, user_ids) -> None:
    ids = [int(i) for i in user_ids]
    if not ids:
        return
    for table, column in USER_DEPENDENTS:
        purge(conn, table, column, ids)
    purge(conn, "users", "id", ids)
