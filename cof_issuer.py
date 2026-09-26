#!/usr/bin/env python3
"""
Certificate of Fitness — issuance backend.

Business rules (locked with the user 2026-05-06):
  1) Hard prerequisite: employee MUST have a valid 16-hr Suspended Scaffold
     User certification (any cert_type with is_cof_prerequisite=1).
     If not, no CoF can be issued. Period.
  2) Expiration date = the EARLIEST expiration_date across ALL of the
     employee's tracked certifications (cert_types treated as a flat list).
     If OSHA-30 expires Aug 15 and 16-hr expires Dec 1, CoF expires Aug 15.
  3) Issued date = the date the issuer (CEO) hits "Sign & Issue" — captured
     server-side, not client-side.
  4) Signature = the file path stored in app_settings.issuer_signature_path,
     applied to all cards in the batch.
  5) Card numbers = SSC-COF-{employee_id} (e.g., SSC-COF-E-00013).
     Card number is unique per worker — re-issuing reuses the same number
     and supersedes the prior card.

Usage:
  python cof_issuer.py status                    # eligibility report for all employees
  python cof_issuer.py issue <emp_id> [<emp_id>]  # batch issue cards
  python cof_issuer.py preview <emp_id>           # dry run — show what would be issued
"""

import os
import sys
import json
import shutil
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
import ssc_paths  # #287
DB_PATH = ssc_paths.sqlite_db_path()   # #287
PHOTOS_DIR = ssc_paths.under_root("employee_photos")   # #287
EXPORTS_DIR = ssc_paths.under_root("cof_exports")   # #287
SIGNATURES_DIR = ssc_paths.under_root("issuer_signatures")   # #287
CREDENTIALS_DIR = ssc_paths.under_root("data_room", "credentials", "cof")   # #287

# Renewal warning threshold
RENEWAL_WARN_DAYS = 30

# Default validity (days) for a CoF card issued under the admin override
# path — i.e. the operator flipped employees.cof_override=1 because real
# prerequisite certs haven't been entered yet. 1 year is a deliberate
# short anchor: the override is a bootstrap, not a permanent issuance
# path; the card forces a refresh once real certs land or 12 months
# pass, whichever comes first. When real prereq certs ARE on file, the
# normal expiry path (earliest cert expiry, see calculate_cof_expiry)
# wins — the override default is only consulted when there are no certs
# to anchor expiry to.
COF_OVERRIDE_VALIDITY_DAYS = 365


# =====================================================================
# DB helpers
# =====================================================================

def db_conn():
    # #260 — route through the env-driven layer (SSC_DB_URL). Production default is
    # the live SQLite file (unchanged); a test backend (isolated copy / Postgres) is
    # used only when SSC_DB_URL is set, so credential issuance honors test isolation.
    import db_layer
    return db_layer.connect()


def get_setting(key, default=None):
    conn = db_conn()
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = db_conn()
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
        (key, value)
    )
    conn.commit()
    conn.close()


# =====================================================================
# Eligibility
# =====================================================================

def employee_certs(employee_id):
    """All certifications for an employee, joined with cert_types."""
    conn = db_conn()
    rows = conn.execute(
        """SELECT c.id, c.cert_type_id, ct.name AS cert_name,
                  ct.validity_months, ct.is_cof_prerequisite,
                  c.date_obtained, c.expiration_date, c.status
           FROM certifications c
           JOIN cert_types ct ON ct.cert_type_id = c.cert_type_id
           WHERE c.employee_id = ?""",
        (employee_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def is_cert_currently_valid(cert):
    """A cert is valid if status is active-ish AND expiration is in the future (or null)."""
    today = date.today().isoformat()
    if cert.get("status") and str(cert["status"]).lower() in ("revoked", "expired", "void"):
        return False
    exp = cert.get("expiration_date")
    if exp is None or exp == "":
        return True   # no expiry = perpetual
    return exp >= today


def employee_has_cof_override(employee_id):
    """True iff employees.cof_override = 1 for this worker. The override is
    an admin-set escape hatch (added 2026-05) for the bootstrap roster
    whose real prerequisite certs haven't been entered yet. New onboards
    default to 0; the override is reversible (UPDATE ... SET cof_override
    = 0) and visible in the DB so it can be audited."""
    conn = db_conn()
    row = conn.execute(
        "SELECT cof_override FROM employees WHERE employee_id = ?",
        (employee_id,)
    ).fetchone()
    conn.close()
    return bool(row and row["cof_override"])


def has_valid_prerequisite(employee_id):
    """Returns (bool, msg) — True if employee holds a currently-valid CoF
    prerequisite cert OR has the admin cof_override flag set.

    The override path returns a distinctive reason ('admin override') so
    downstream surfaces can render it differently from real-cert eligibility
    if they want; today both paths just light up the same "Issue CoF" action.
    """
    certs = employee_certs(employee_id)
    prereq_certs = [c for c in certs if c.get("is_cof_prerequisite")]
    valid_prereqs = [c for c in prereq_certs if is_cert_currently_valid(c)]
    if valid_prereqs:
        return True, "OK"
    # No valid prereq cert. Check the admin override before declining —
    # the OR path keeps eligibility true even after real certs are added
    # (the cert path takes precedence; expiry comes from cert dates).
    if employee_has_cof_override(employee_id):
        return True, "admin override"
    if not prereq_certs:
        return False, "No 16-hr Suspended Scaffold cert on file."
    # Had a prereq cert at some point but it expired.
    exp_dates = [c["expiration_date"] for c in prereq_certs if c.get("expiration_date")]
    last_exp = max(exp_dates) if exp_dates else "(unknown)"
    return False, f"16-hr Suspended Scaffold cert expired (last expiry: {last_exp})."


def calculate_cof_expiry(employee_id, today_override=None):
    """Earliest expiration across all currently-valid tracked certs. When
    no tracked cert has an expiration date but cof_override=1 (admin
    bootstrap), fall back to today + COF_OVERRIDE_VALIDITY_DAYS so a card
    can still be issued — short anchor on purpose, forces a refresh once
    real certs land. Returns None only when the worker is genuinely
    ineligible (no override, no certs with expiry).

    today_override (optional ISO date string or date) lets callers
    deterministically anchor the override-default expiry — useful for
    tests / batch backfills that want the issued and expires dates to
    line up with a specific issue date.
    """
    certs = employee_certs(employee_id)
    valid_with_expiry = [
        c for c in certs
        if is_cert_currently_valid(c) and c.get("expiration_date")
    ]
    if valid_with_expiry:
        return min(c["expiration_date"] for c in valid_with_expiry)
    if employee_has_cof_override(employee_id):
        if isinstance(today_override, str):
            anchor = date.fromisoformat(today_override)
        else:
            anchor = today_override or date.today()
        return (anchor + timedelta(days=COF_OVERRIDE_VALIDITY_DAYS)).isoformat()
    return None


def cof_status_for_employee(employee_id):
    """Returns a structured status dict the dashboard uses."""
    eligible, reason = has_valid_prerequisite(employee_id)
    expiry = calculate_cof_expiry(employee_id) if eligible else None

    # Look up most recent active card
    conn = db_conn()
    card = conn.execute(
        """SELECT card_id, issued_date, expires_date, status
           FROM cof_cards WHERE employee_id = ? AND status = 'issued'
           ORDER BY issued_date DESC LIMIT 1""",
        (employee_id,)
    ).fetchone()
    conn.close()

    today = date.today()
    if card:
        card = dict(card)
        try:
            card_exp = date.fromisoformat(card["expires_date"])
        except (TypeError, ValueError):
            card_exp = None
        if not eligible:
            outcome = "ineligible"
        elif card_exp and card_exp < today:
            outcome = "needs_new"      # current card already expired
        elif expiry and card["expires_date"] != expiry:
            outcome = "needs_reissue"  # underlying certs changed → reissue
        elif card_exp and (card_exp - today).days <= RENEWAL_WARN_DAYS:
            outcome = "expiring_soon"
        else:
            outcome = "active"
    else:
        outcome = "needs_new" if eligible else "ineligible"

    return {
        "employee_id": employee_id,
        "eligible": eligible,
        "ineligibility_reason": None if eligible else reason,
        "calculated_cof_expires": expiry,
        "current_card": card,
        "outcome": outcome,
    }


def all_employees_status():
    """Returns status for every employee, sorted by outcome priority."""
    conn = db_conn()
    rows = conn.execute(
        "SELECT employee_id, name, trade, photo_path FROM employees ORDER BY name"
    ).fetchall()
    conn.close()

    out = []
    for r in rows:
        s = cof_status_for_employee(r["employee_id"])
        s["name"] = r["name"]
        s["trade"] = r["trade"]
        s["photo_path"] = r["photo_path"]
        s["certs"] = employee_certs(r["employee_id"])
        out.append(s)

    # Order: needs_new > needs_reissue > expiring_soon > active > ineligible
    priority = {"needs_new": 0, "needs_reissue": 1, "expiring_soon": 2, "active": 3, "ineligible": 4}
    out.sort(key=lambda s: (priority.get(s["outcome"], 99), s["name"] or ""))
    return out


# =====================================================================
# Card numbering
# =====================================================================

def card_number_for_employee(employee_id):
    """Stable card_number_display for an employee — same across reissues.
    Format: SSC-COF-{worker_id}, e.g. SSC-COF-W-0013. Resolves worker_id
    from the DB; falls back to SSC-COF-{employee_id} (legacy E- format)
    with a logged warning if the worker has no worker_id on file.

    Card numbers are user-facing — printed on the physical card and shown
    in the UI. We use the human-facing W-#### identifier so cards line up
    with the rest of the dashboard's worker references (per CLAUDE.md
    terminology rule). The internal DB primary key card_id keeps the
    employee_id + revision suffix for FK stability."""
    if not employee_id:
        raise RuntimeError("employee_id required to generate card number")
    conn = db_conn()
    try:
        row = conn.execute(
            "SELECT worker_id FROM employees WHERE employee_id = ?",
            (employee_id,)
        ).fetchone()
    finally:
        conn.close()
    if row and row["worker_id"]:
        return f"SSC-COF-{row['worker_id']}"
    print(
        f"[cof_issuer] WARN: employee {employee_id} has no worker_id on file; "
        f"falling back to legacy E- card number format",
        file=sys.stderr,
    )
    return f"SSC-COF-{employee_id}"


def _next_cof_revision(conn, employee_id, card_number_display=None):
    """Next revision number for this employee's CoF cards. Count-based
    (matches company_id_issuer._next_revision pattern) — every issuance
    creates one row, including the row that gets marked 'replaced' by a
    later reissue. Returns 1 if no prior cards.

    #346: card_id is `<card number>-<rev>` and the card number carries the
    W-####, so after the one-time worker renumber a new hire given a freed
    number (e.g. W-0030) would recount from 1 and collide with the card the
    previous holder of that number was issued. The revision is therefore also
    kept above every existing card_id with the same card number."""
    n = conn.execute(
        "SELECT COUNT(*) FROM cof_cards WHERE employee_id = ?",
        (employee_id,)
    ).fetchone()[0]
    rev = n + 1
    if card_number_display:
        prefix = f"{card_number_display}-"
        for r in conn.execute(
            "SELECT card_id FROM cof_cards WHERE card_id LIKE ?", (prefix + "%",)
        ).fetchall():
            tail = r[0][len(prefix):]
            if tail.isdigit():
                rev = max(rev, int(tail) + 1)
    return rev


# =====================================================================
# Issuance
# =====================================================================

def list_project_riggers(project_code):
    """Return all active riggers assigned to a project."""
    conn = db_conn()
    rows = conn.execute(
        """SELECT id, rigger_name, license_number, rigger_type, is_default, signature_path
           FROM project_riggers
           WHERE project_code = ? AND is_active = 1
           ORDER BY is_default DESC, rigger_name""",
        (project_code,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_rigger(rigger_id):
    conn = db_conn()
    r = conn.execute(
        "SELECT id, project_code, rigger_name, license_number, rigger_type, signature_path FROM project_riggers WHERE id = ?",
        (rigger_id,)
    ).fetchone()
    conn.close()
    return dict(r) if r else None


def get_default_rigger_for_project(project_code, rigger_type='Special Rigger'):
    """Returns the default rigger for a project (the one used for quick-issue).

    Filters by `rigger_type` so a CoF — which under NYC DOB §3314.4 must be
    signed by a Special Rigger — won't silently pick up a Master Rigger /
    Sign Hanger row if the operator later adds one and marks it default.
    Pass `rigger_type=None` to disable the filter (any active rigger,
    default first).
    """
    conn = db_conn()
    if rigger_type is None:
        r = conn.execute(
            """SELECT id, rigger_name, license_number, signature_path
               FROM project_riggers
               WHERE project_code = ? AND is_active = 1
               ORDER BY is_default DESC, id ASC LIMIT 1""",
            (project_code,)
        ).fetchone()
    else:
        r = conn.execute(
            """SELECT id, rigger_name, license_number, signature_path
               FROM project_riggers
               WHERE project_code = ? AND is_active = 1 AND rigger_type = ?
               ORDER BY is_default DESC, id ASC LIMIT 1""",
            (project_code, rigger_type)
        ).fetchone()
    conn.close()
    return dict(r) if r else None


def issue_cof(employee_id, rigger_id=None, project_code=None, today_override=None):
    """
    Atomically issue a CoF for one employee, signed by a specific rigger.

    If rigger_id is not provided, falls back to the project's default rigger.
    If project_code isn't provided, uses the worker's first active project assignment.

    Raises RuntimeError if the employee fails the prerequisite check.
    Returns the new card row as a dict.
    """
    from worker_id import worker_id_for_display
    eligible, reason = has_valid_prerequisite(employee_id)
    if not eligible:
        wid = worker_id_for_display(employee_id) or "(unknown worker)"
        raise RuntimeError(f"Cannot issue CoF for {wid}: {reason}")

    issued_date = (today_override or date.today()).isoformat() if not isinstance(today_override, str) else today_override

    # Anchor the override-default expiry to the issued date (not date.today())
    # so a backdated test / batch issuance produces consistent dates.
    expiry = calculate_cof_expiry(employee_id, today_override=issued_date)
    if not expiry:
        wid = worker_id_for_display(employee_id) or "(unknown worker)"
        raise RuntimeError(
            f"Cannot calculate expiry for {wid}: no certifications with expiration dates."
        )

    # ----- Resolve rigger -----
    # If caller didn't specify, use the project's default rigger.
    if not project_code:
        # Find any active project the employee is on
        conn_p = db_conn()
        row = conn_p.execute(
            "SELECT project_code FROM project_assignments WHERE employee_id = ? AND status = 'active' LIMIT 1",
            (employee_id,)
        ).fetchone()
        conn_p.close()
        project_code = row["project_code"] if row else None

    rigger = None
    if rigger_id:
        rigger = get_rigger(rigger_id)
    elif project_code:
        rigger = get_default_rigger_for_project(project_code)

    if rigger:
        issued_by = rigger["rigger_name"]
        issuer_license = rigger["license_number"]
        signature_path = rigger.get("signature_path") or ""
        rigger_id_for_card = rigger["id"]
    else:
        # Legacy fallback for any caller path that doesn't have a rigger setup yet
        issued_by = get_setting("issuer_name", "ARUN MAL")
        issuer_license = get_setting("issuer_license", "7652")
        signature_path = get_setting("issuer_signature_path", "") or ""
        rigger_id_for_card = None

    card_number_display = card_number_for_employee(employee_id)
    certs = employee_certs(employee_id)
    basis = {
        "calculated_from": [
            {"cert_type_id": c["cert_type_id"], "cert_name": c["cert_name"],
             "expires": c["expiration_date"]}
            for c in certs if is_cert_currently_valid(c) and c.get("expiration_date")
        ]
    }

    # Snapshot the worker's face photo at issuance time. Read face_image_path
    # (the authoritative current photo, set by POST /api/employees/<id>/face-photo)
    # — NOT photo_path, which is the legacy intake column and is NULL for
    # everyone post-Worker-app rollout. Then copy the file into
    # data_room/credentials/cof/<emp_id>_v<rev>.<ext> so the issued card has
    # a frozen-in-time photo (a future face-photo edit shouldn't retroactively
    # change the photo on an already-printed card).
    conn = db_conn()
    try:
        emp = conn.execute(
            "SELECT face_image_path FROM employees WHERE employee_id = ?",
            (employee_id,)
        ).fetchone()
        face_src_path = emp["face_image_path"] if emp else None

        # Compute revision + unique card_id BEFORE marking the prior card
        # replaced — the count-based revision then sees the prior row as
        # part of the existing history.
        revision = _next_cof_revision(conn, employee_id, card_number_display)
        card_id = f"{card_number_display}-{revision}"

        # Snapshot the photo to credentials dir (best-effort; if it fails the
        # card still issues with photo_snapshot_path=NULL and the template
        # falls back to its 'PHOTO' placeholder).
        photo_snapshot = None
        if face_src_path:
            # #295 S2 — THE resolver for the stored row (pre-#287 rows are
            # Windows-absolute; the old is_absolute gate skipped exactly the
            # rows that need re-anchoring), and the snapshot rel anchors to the
            # DATA ROOT — on the cloud SCRIPT_DIR is /app and relative_to
            # raised, so cards silently issued with no photo.
            src = ssc_paths.resolve_data_path(face_src_path)
            if src.exists():
                CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
                ext = src.suffix.lower() or ".jpg"
                dest = CREDENTIALS_DIR / f"{employee_id}_v{revision}{ext}"
                try:
                    shutil.copy2(str(src), str(dest))
                    photo_snapshot = ssc_paths.store_rel(dest)
                except Exception as e:
                    print(f"[cof_issuer] photo snapshot failed: {e}", file=sys.stderr)

        # Mark any existing 'issued' CoF as 'replaced' (same-type supersede).
        conn.execute(
            "UPDATE cof_cards SET status='replaced', updated_at=CURRENT_TIMESTAMP "
            "WHERE employee_id = ? AND status = 'issued'",
            (employee_id,)
        )
        # CoF supersedes Company ID (#98): if the worker has an active
        # Company ID (e.g., issued earlier when they weren't CoF-eligible),
        # mark it 'replaced' too. The CoF is the higher credential and the
        # 'one active credential per worker' rule must hold across types.
        # The reverse direction is NOT applied — issuing a Company ID does
        # NOT touch an existing CoF (the operator must explicitly revoke
        # the CoF first if they really want to downgrade).
        conn.execute(
            "UPDATE company_id_cards SET status='replaced', updated_at=CURRENT_TIMESTAMP "
            "WHERE employee_id = ? AND status = 'active'",
            (employee_id,)
        )

        conn.execute(
            """INSERT INTO cof_cards
               (card_id, employee_id, issued_date, expires_date, issued_by, issuer_license,
                signature_path, photo_snapshot_path, status, basis_certs_json,
                rigger_id, rigger_name_snapshot, rigger_license_snapshot,
                card_number_display)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'issued', ?, ?, ?, ?, ?)""",
            (card_id, employee_id, issued_date, expiry, issued_by, issuer_license,
             signature_path, photo_snapshot, json.dumps(basis),
             rigger_id_for_card, issued_by, issuer_license,
             card_number_display)
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM cof_cards WHERE card_id = ?", (card_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def issue_batch(employee_ids, rigger_id=None, project_code=None):
    """Issue CoFs for multiple employees in one transaction-ish operation.
    All cards in a batch are signed by the same rigger (rigger_id).
    Returns dict mapping employee_id → result (card dict OR error string)."""
    results = {}
    for eid in employee_ids:
        try:
            results[eid] = issue_cof(eid, rigger_id=rigger_id, project_code=project_code)
        except Exception as e:
            results[eid] = {"error": str(e)}
    return results


# =====================================================================
# CLI
# =====================================================================

def cli_status():
    rows = all_employees_status()
    print(f"\n{'Status':<16}{'Employee':<22}{'Trade':<14}{'Calc Exp':<12}  Reason / Detail")
    print("-" * 95)
    for s in rows:
        outcome = s["outcome"]
        name = (s.get("name") or "—")[:20]
        trade = (s.get("trade") or "—")[:12]
        calc = s.get("calculated_cof_expires") or "—"
        detail = s.get("ineligibility_reason") or ""
        if s.get("current_card"):
            detail = f"current: {s['current_card']['card_id']} (exp {s['current_card']['expires_date']})"
        print(f"{outcome:<16}{name:<22}{trade:<14}{calc:<12}  {detail}")


def cli_preview(employee_id):
    s = cof_status_for_employee(employee_id)
    print(json.dumps(s, indent=2, default=str))


def cli_issue(employee_ids):
    results = issue_batch(employee_ids)
    print("\nIssuance results:")
    for eid, r in results.items():
        if isinstance(r, dict) and "error" in r:
            print(f"  {eid}: FAILED — {r['error']}")
        else:
            print(f"  {eid}: ISSUED card {r['card_id']} (exp {r['expires_date']})")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "status":
        cli_status()
    elif cmd == "preview" and len(sys.argv) >= 3:
        cli_preview(sys.argv[2])
    elif cmd == "issue" and len(sys.argv) >= 3:
        cli_issue(sys.argv[2:])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
