#!/usr/bin/env python3
"""#276 — Estimating-division core schema: lead expansion + the estimator role +
the notification log. Per ESTIMATING_DIVISION_BLUEPRINT.md (Build B).

db_layer-aware, idempotent, dual-backend. Three parts:

  1. estimate LEAD EXPANSION (additive columns):
       division           facade|rope_access|interior|parking_garage — code enum,
                          BACKFILLED from est_type where obvious (IRA->rope_access,
                          FR->facade, IR->interior, PG->parking_garage), editable.
       ra_subtype         inspection|work — NULL unless division=rope_access
                          (IRA estimates backfill 'inspection').
       inquiry_kind       bid|po|undetermined (default undetermined — estimating
                          decides later when unclear).
       bid_due_date       LOCAL date, NULL — ALARMED on the surfaces (a missed bid
                          date is silent lost revenue).
       est_stage          received|walkthrough_scheduled|walkthrough_done|
                          proposal_draft|sent_to_vp — the ESTIMATING sub-machine,
                          active only while macro status='scoping' (the MACRO/MICRO
                          contract: #273's status lifecycle is untouched).
       est_stage_changed_at  LOCAL — aging derives from this, never stored flags;
                          the honest-backfill date lands here.
       walkthrough_date   LOCAL date, NULL — simple field (calendar wiring = Build C).
       assigned_estimator users.id, NULL (no FK — the #264/#266 user-id convention).
     Backfill: estimates already in status='scoping' get est_stage='received' with
     est_stage_changed_at seeded from status_changed_at (aging true from day one).

  2. USERS role catalog + 'estimator' (blueprint §5). SQLite cannot ALTER a CHECK —
     the documented #257 table-rebuild procedure runs (create new -> copy -> drop ->
     rename; row ids preserved; FK check scoped to users/sessions). Postgres drops
     and re-adds the named CHECK constraint. Idempotent both ways.

  3. notification_log — the stub/record pattern for internal notifications (SendGrid
     live-send only when SENDGRID_API_KEY is present; rows log every queued send so
     the gate never depends on a live provider). No client data beyond code+address.

NO production cutover. Honors SSC_DB_URL (unset -> live, operator runs ONCE at
deploy AFTER snapshot — migrations in numeric order: 273, 274, 276).

Run:  python apply_estimating_276.py
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import db_layer  # noqa: E402
from apply_crm_266 import _columns, _table_exists  # noqa: E402
from apply_estimates_273 import ensure_estimates_schema  # noqa: E402

ROLE_CATALOG_276 = ('admin', 'c_suite', 'pm', 'super', 'client', 'architect',
                    'vendor', 'estimator')
_ROLE_LIST_SQL = ", ".join(f"'{r}'" for r in ROLE_CATALOG_276)

# type -> division derivation (obvious mappings only; operator can edit per lead)
_DIVISION_OF_TYPE = {"IRA": "rope_access", "FR": "facade", "IR": "interior",
                     "PG": "parking_garage", "OTH": "other"}

_EST_COLS = (
    ("division", "TEXT"),
    ("ra_subtype", "TEXT"),
    ("inquiry_kind", "TEXT"),
    ("bid_due_date", "TEXT"),
    ("est_stage", "TEXT"),
    ("est_stage_changed_at", "TEXT"),
    ("walkthrough_date", "TEXT"),
    ("assigned_estimator", "INTEGER"),
)


def _ensure_estimate_columns(conn) -> dict:
    changed = {}
    have = _columns(conn, "estimate")
    for col, typ in _EST_COLS:
        if col not in have:
            conn.execute(f"ALTER TABLE estimate ADD COLUMN {col} {typ}")
            changed[col] = True
    conn.execute("CREATE INDEX IF NOT EXISTS idx_estimate_stage ON estimate(est_stage)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_estimate_division ON estimate(division)")
    conn.commit()
    return changed


def _backfill_estimates(conn) -> dict:
    """Derive division/subtype/kind for existing rows; seed est_stage for leads
    already in scoping so aging is TRUE from day one. Idempotent (only fills NULLs)."""
    n_div = n_stage = 0
    for t, d in _DIVISION_OF_TYPE.items():
        cur = conn.execute(
            "UPDATE estimate SET division=? WHERE est_type=? AND division IS NULL", (d, t))
        n_div += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.execute("UPDATE estimate SET ra_subtype='inspection' "
                 "WHERE est_type='IRA' AND division='rope_access' AND ra_subtype IS NULL")
    conn.execute("UPDATE estimate SET inquiry_kind='undetermined' WHERE inquiry_kind IS NULL")
    cur = conn.execute(
        "UPDATE estimate SET est_stage='received', est_stage_changed_at=status_changed_at "
        "WHERE status='scoping' AND est_stage IS NULL")
    n_stage = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.commit()
    return {"divisions_backfilled": n_div, "stages_seeded": n_stage}


# ============================ users CHECK + 'estimator' ============================

def _sqlite_users_has_estimator(conn) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    return "'estimator'" in ((row[0] if row else "") or "")


def _rebuild_users_sqlite(conn) -> tuple:
    """The documented #257 SQLite table-redefinition, with the 8-role CHECK. Copies
    every current column BY NAME (the table already has the #257 shape), preserves
    row ids, and FK-checks users/sessions before COMMIT."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    col_list = ", ".join(cols)
    ddl_cols = []
    for r in conn.execute("PRAGMA table_info(users)").fetchall():
        name, typ, notnull, dflt, pk = r[1], r[2], r[3], r[4], r[5]
        if pk:
            ddl_cols.append(f"{name} INTEGER PRIMARY KEY AUTOINCREMENT")
            continue
        d = f"{name} {typ or 'TEXT'}"
        if name == "role":
            d += f" NOT NULL CHECK (role IN ({_ROLE_LIST_SQL}))"
        else:
            if notnull:
                d += " NOT NULL"
            if dflt is not None:
                d += f" DEFAULT {dflt}"
        if name == "email":
            d += " UNIQUE"
        ddl_cols.append(d)
    ddl = "CREATE TABLE users_new (\n  " + ",\n  ".join(ddl_cols) + "\n)"

    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys=OFF;")
    # The live DB carries orphan sessions rows whose users were long deleted (the
    # same FK-OFF debris clean_user_orphans heals before every gate run). They are
    # dead cookies — the user row is gone, they can never authenticate — and they
    # would trip the post-rebuild FK check, so purge them first (logged).
    n_orph = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE user_id NOT IN (SELECT id FROM users)"
    ).fetchone()[0]
    if n_orph:
        conn.execute("DELETE FROM sessions WHERE user_id NOT IN (SELECT id FROM users)")
        conn.commit()
        print(f"[276] purged {n_orph} orphan session row(s) (deleted users' dead cookies)")
    before = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.execute("BEGIN;")
    try:
        conn.execute("DROP TABLE IF EXISTS users_new;")
        conn.execute(ddl)
        conn.execute(f"INSERT INTO users_new ({col_list}) SELECT {col_list} FROM users;")
        conn.execute("DROP TABLE users;")
        conn.execute("ALTER TABLE users_new RENAME TO users;")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);")
        after = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if after != before:
            raise RuntimeError(f"row count changed during rebuild: {before} -> {after}")
        viol = (conn.execute("PRAGMA foreign_key_check(users)").fetchall()
                + conn.execute("PRAGMA foreign_key_check(sessions)").fetchall())
        if viol:
            raise RuntimeError(f"foreign_key_check found {len(viol)} violation(s)")
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.isolation_level = ""
    return before, after


def _pg_users_has_estimator(conn) -> bool:
    row = conn.execute(
        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE t.relname='users' AND c.contype='c' "
        "AND pg_get_constraintdef(c.oid) LIKE ?", ("%role%",)).fetchone()
    return bool(row and "estimator" in row[0])


def _pg_swap_users_check(conn) -> bool:
    rows = conn.execute(
        "SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE t.relname='users' AND c.contype='c'").fetchall()
    target = None
    for name, definition in rows:
        if "role" in definition:
            target = name
            break
    if target:
        conn.execute(f'ALTER TABLE users DROP CONSTRAINT "{target}"')
    conn.execute(
        f"ALTER TABLE users ADD CONSTRAINT users_role_check_276 "
        f"CHECK (role IN ({_ROLE_LIST_SQL}))")
    conn.commit()
    return True


def ensure_estimator_role(conn) -> bool:
    """Add 'estimator' to the users.role CHECK on either backend. Idempotent.
    Returns True when a change was applied."""
    if db_layer.is_postgres():
        if _pg_users_has_estimator(conn):
            return False
        return _pg_swap_users_check(conn)
    if _sqlite_users_has_estimator(conn):
        return False
    _rebuild_users_sqlite(conn)
    return True


# ============================ notification log ============================

def _ensure_notification_log(conn) -> bool:
    created = not _table_exists(conn, "notification_log")
    pk = ("id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
          if db_layer.is_postgres() else "id INTEGER PRIMARY KEY AUTOINCREMENT")
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS notification_log (
              {pk},
              kind              TEXT    NOT NULL,
              recipient_user_id INTEGER,
              recipient_email   TEXT,
              subject           TEXT,
              deep_link         TEXT,
              estimate_code     TEXT,
              created_at        TEXT,
              sent              INTEGER DEFAULT 0,
              error             TEXT
            )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notif_kind ON notification_log(kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notif_recipient ON notification_log(recipient_user_id)")
    conn.commit()
    return created


def ensure_estimating_schema(conn) -> dict:
    """Everything #276 needs, idempotent + dual-backend. Caller owns the conn."""
    ensure_estimates_schema(conn)   # the #273 base first
    changed = {}
    changed.update(_ensure_estimate_columns(conn))
    changed.update(_backfill_estimates(conn))
    changed["estimator_role"] = ensure_estimator_role(conn)
    changed["notification_log"] = _ensure_notification_log(conn)
    return changed


def main() -> int:
    conn = db_layer.connect()
    backend = "postgres" if db_layer.is_postgres() else "sqlite"
    try:
        changed = ensure_estimating_schema(conn)
        have = _columns(conn, "estimate")
        ok = (all(c in have for c, _ in _EST_COLS)
              and _table_exists(conn, "notification_log"))
        if db_layer.is_postgres():
            ok = ok and _pg_users_has_estimator(conn)
        else:
            ok = ok and _sqlite_users_has_estimator(conn)
        print(f"[276] backend={backend}  changed={changed}")
        print(f"[276] verify: 8 estimate columns + estimator role + notification_log -> {'OK' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
