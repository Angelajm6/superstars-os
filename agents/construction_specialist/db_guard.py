"""Which database the Construction Specialist's scripts may open (CLAUDE.md: never
open the real superstars.db; use the isolated DB).

There is NO default. The path comes from an explicit --db or SSC_AGENT_DB, must
already exist (a typo never creates a new file), and is refused when it is the
real database: the repo's superstars.db, any file named superstars.db, or the same
file reached through a link. Callers open the result with `connect()`, which uses
SQLite's mode=rw so nothing is ever created.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
REAL_DB_NAME = "superstars.db"
ENV_VAR = "SSC_AGENT_DB"


class RefusedDB(Exception):
    """The database was not given, does not exist, or is the real one."""


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def resolve(db_path: Optional[str] = None) -> Path:
    raw = db_path or os.environ.get(ENV_VAR) or ""
    if not str(raw).strip():
        raise RefusedDB(f"no isolated database given (pass --db or set {ENV_VAR})")
    p = Path(raw).expanduser().resolve()
    if p.name.lower() == REAL_DB_NAME or _same_file(p, REPO_ROOT / REAL_DB_NAME):
        raise RefusedDB("refusing the real superstars.db: use an isolated database")
    if not p.is_file():
        raise RefusedDB("the isolated database does not exist")
    return p


def connect(db_path: Optional[str] = None, **kw) -> sqlite3.Connection:
    p = resolve(db_path)
    return sqlite3.connect(f"{p.as_uri()}?mode=rw", uri=True, **kw)
