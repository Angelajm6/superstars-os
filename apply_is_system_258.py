#!/usr/bin/env python3
"""#258 — add users.is_system, flag existing test fixtures, purge stray @example.test.

`is_system=1` marks accounts created by the test gate (seeders). The admin console
hides them and the admin count includes only real (is_system=0) admins.

Migration choice: adding a column with a DEFAULT is fully FK-safe and preserves
every row + id (SQLite ALTER TABLE ADD COLUMN does not rewrite rows or touch FKs),
so no table rebuild is needed here (unlike #257's CHECK change). Idempotent: the
ADD COLUMN is skipped if it already exists.

Then: backfill is_system=1 on the KNOWN test-fixture email patterns (never the real
operator domain), and PURGE the accumulating stray smoke-auth fixture rows
(@example.test) + their sessions/audit. Verifies the real admin
(@superstarscontracting.com) stays is_system=0 and is the SOLE real admin.
Snapshot taken by the caller. No passwords/hashes read or printed.

Run:  python apply_is_system_258.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "superstars.db"

REAL_DOMAIN = "%@superstarscontracting.com"
EMPTY_PREPROD_FIXTURE_ENV = "SSC_ALLOW_EMPTY_PREPROD_FIXTURE"

# Known test-fixture email shapes (every gate seeder uses one of these). The real
# operator + any real c_suite/pm the operator onboards use other domains.
FIXTURE_PATTERNS = (
    "%@example.test",       # smoke_auth.py
    "%@example.invalid",    # smoke_auth_roles.py (smk257-*)
    "%@superstars.local",   # _smoke_auth (smoke@), smoke_dropplan_api / smoke_labor_rates (smk-*)
    "smk%",                 # any smk-prefixed local id
    "smoke-%",              # smoke-pm-*, smoke-auth-* belt-and-suspenders
)


def main() -> int:
    if not DB_PATH.exists():
        print(f"ERROR: superstars.db not found at {DB_PATH}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        # 1) add the column (idempotent)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "is_system" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0")
            print("[258] added users.is_system (default 0)")
        else:
            print("[258] users.is_system already present — skipping ADD COLUMN")

        # 2) backfill is_system=1 on the fixture patterns (NEVER the real operator domain)
        flagged = 0
        for pat in FIXTURE_PATTERNS:
            cur = conn.execute(
                "UPDATE users SET is_system=1 WHERE email LIKE ? AND email NOT LIKE ? AND is_system=0",
                (pat, REAL_DOMAIN))
            flagged += cur.rowcount
        conn.commit()
        print(f"[258] backfilled is_system=1 on {flagged} existing fixture row(s)")

        # 3) PURGE the accumulating stray smoke-auth fixtures (@example.test) + their refs
        stray_ids = [r[0] for r in conn.execute(
            "SELECT id FROM users WHERE email LIKE '%@example.test'").fetchall()]
        for uid in stray_ids:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM login_audit WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM role_change_audit WHERE user_id=? OR changed_by=?", (uid, uid))
        conn.execute("DELETE FROM users WHERE email LIKE '%@example.test'")
        conn.commit()
        print(f"[258] purged {len(stray_ids)} stray @example.test fixture row(s)")

        # 4) verify the real admin(s) are intact
        real_admins = conn.execute(
            "SELECT COUNT(1) FROM users WHERE role='admin' AND is_system=0").fetchone()[0]
        real_admin_is_company = conn.execute(
            "SELECT COUNT(1) FROM users WHERE role='admin' AND is_system=0 AND email LIKE ?",
            (REAL_DOMAIN,)).fetchone()[0]
        total = conn.execute("SELECT COUNT(1) FROM users").fetchone()[0]
        sys_n = conn.execute("SELECT COUNT(1) FROM users WHERE is_system=1").fetchone()[0]
        print(f"[258] users total={total} | is_system=1 fixtures={sys_n} | "
              f"real (is_system=0) admins={real_admins} | real admin on company domain={real_admin_is_company}")
        # A production migration must preserve at least one real admin, every one of
        # them on the company domain (the single-admin rule became never-zero-admins).
        # The isolated pre-production fixture builder intentionally starts with
        # no users at all and creates its review account only after the full
        # schema has landed.  That narrow, explicit build-only override keeps
        # the normal migration fail-closed everywhere else.
        empty_fixture = (os.environ.get(EMPTY_PREPROD_FIXTURE_ENV) == "1")
        ok = (real_admins >= 1 and real_admin_is_company == real_admins)
        if empty_fixture and total == 0:
            ok = True
            print("[258] empty pre-production fixture invariant: OK")
        print("[258] real admin invariant:", "OK" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
