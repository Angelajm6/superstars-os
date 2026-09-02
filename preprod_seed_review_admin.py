#!/usr/bin/env python3
"""Create one synthetic, password-based admin for the pre-production review.

The credentials are supplied only through Render environment variables and are
never printed.  The account is marked is_system=1 so it cannot be mistaken for
a real employee or production administrator.
"""
from __future__ import annotations

import os
import re
import sys

CONFIRM_VALUE = "CREATE_SYNTHETIC_REVIEW_ADMIN"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _value(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise SystemExit(f"Refusing: {name} is required.")
    return value


def main() -> int:
    if os.environ.get("SSC_ENVIRONMENT") != "preprod":
        raise SystemExit("Refusing: SSC_ENVIRONMENT must be exactly 'preprod'.")
    if os.environ.get("SSC_PREPROD_REVIEW_ADMIN_CONFIRM") != CONFIRM_VALUE:
        raise SystemExit("Refusing: explicit synthetic-review-admin confirmation is required.")
    # Deferred imports keep the safety interlocks testable in a lightweight local
    # checkout; the Render image includes these runtime dependencies.
    import db_layer
    from auth import hash_password
    if not db_layer.is_postgres():
        raise SystemExit("Refusing: the review account may only be created in Postgres preprod.")

    email = _value("SSC_PREPROD_REVIEW_ADMIN_EMAIL").lower()
    name = _value("SSC_PREPROD_REVIEW_ADMIN_NAME")
    password = _value("SSC_PREPROD_REVIEW_ADMIN_PASSWORD")
    if not EMAIL_RE.match(email):
        raise SystemExit("Refusing: review admin email is not valid.")
    if len(password) < 16:
        raise SystemExit("Refusing: review admin password must be at least 16 characters.")

    conn = db_layer.connect()
    try:
        existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            raise SystemExit("Refusing: a user with that email already exists.")
        conn.execute(
            "INSERT INTO users (email, password_hash, role, full_name, is_active, is_system) "
            "VALUES (?, ?, 'admin', ?, 1, 1)",
            (email, hash_password(password), name),
        )
        conn.commit()
    finally:
        conn.close()
    print("[review-admin] synthetic pre-production admin created; credentials were not logged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
