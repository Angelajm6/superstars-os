"""#296 — server-recorded worker app sessions.

The worker PWA is deliberately public at the transport layer, so an employee ID
is never an authorization credential.  This schema backs an opaque, short-lived
session minted only after a successful worker login.  The database stores a
SHA-256 digest, never the bearer token itself.
"""
from __future__ import annotations


def ensure_worker_session_security(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS worker_app_session (
            token_hash TEXT PRIMARY KEY,
            employee_id TEXT NOT NULL,
            project_code TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            started_sign_in_id INTEGER,
            ended_at TEXT,
            revoked_at TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_worker_app_session_employee "
        "ON worker_app_session(employee_id, expires_at)"
    )
