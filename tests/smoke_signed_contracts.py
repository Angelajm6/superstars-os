#!/usr/bin/env python3
"""A signed proposal is filed in its job's Documents → Contracts (2026-11-13, signed_contracts.py).

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * Ready to start (onboard from_proposal_id): the signed proposal lands in the new job's CONTRACTS, fills
    "Executed Owner Contract / AIA" (owner_contract), keeps its source_proposal_id, its file is a copy under the job's
    documents and the gated file route serves it; the job's documents list shows the item on file
  * idempotent: opening the documents again, or ensure_filed, adds nothing
  * backfill: a job started before this existed (its row missing) gets it the first time its documents are opened
  * signed on a job that already exists (estimate converted first, then the proposal marked signed): filed at signing
  * a second signed proposal on a job whose contract item is filled comes in as an extra Contracts document
Run:  venv/bin/python tests/smoke_signed_contracts.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_sigc_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ["SSC_MAIL_FAKE_DIR"] = str(Path(_TMP) / "mail")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import proposals  # noqa: E402
import signed_contracts  # noqa: E402
import ssc_paths  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "sigc"
PW = "SignedContractsSmoke2026"
PDF = b"%PDF-1.4\n1 0 obj<</T(sigc)>>endobj\ntrailer<<>>\n%%EOF\n"


def _db():
    return db_layer.connect(pragma_fk=False)


def _with_pdf(conn, pid):
    """The proposal's document is an uploaded original (like a proposal from before the app): no rendering needed."""
    d = ssc_paths.under_root("data_room", "proposals")
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"sigc-{pid}.pdf"
    f.write_bytes(PDF)
    conn.execute("UPDATE proposal SET pdf_path = ?, pdf_hash = ? WHERE id = ?", (ssc_paths.store_rel(f), proposals.ORIGINAL_PDF, pid))
    conn.commit()


def _proposal(conn, org, address, price, admin):
    est = estimates.create_estimate(conn, est_type="FR", borough="BX", client_org_id=org, building_address=address,
                                    notes="Sigc scope", created_by=admin)
    estimates.change_status(conn, est["id"], "scoping", actor_user_id=admin)
    estimates.change_status(conn, est["id"], "submitted", final_amount=price, actor_user_id=admin)
    p = proposals.create_from_estimate(conn, est["id"], created_by=admin)
    _with_pdf(conn, p["id"])
    return est, p["id"]


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        email = f"{TAG}.c_suite@example.com"
        conn.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                     "VALUES (?, ?, 'c_suite', 'Smoke Sigc', 1, 'active', 0, 1)", (email, auth.hash_password(PW)))
        ids["admin"] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Sigc %'")
        ids["org"] = crm.create_org(conn, name="Sigc Edge Engineering", relationship_type="client")
        conn.commit()
        ids["estA"], ids["A"] = _proposal(conn, ids["org"], "1 Sigc John St", 6500, ids["admin"])
        ids["estB"], ids["B"] = _proposal(conn, ids["org"], "2 Sigc John St", 9000, ids["admin"])
        proposals.mark_signed_offline(conn, ids["A"], name="Pat Signer", actor=ids["admin"])
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = _db()
    try:
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Sigc %'").fetchall()]
        ph = ",".join("?" * len(org_ids)) or "NULL"
        est = conn.execute(f"SELECT id, code, converted_project_code FROM estimate WHERE client_org_id IN ({ph})", org_ids).fetchall()
        est_ids = [r["id"] for r in est]
        codes = [r["code"] for r in est] + [r["converted_project_code"] for r in est if r["converted_project_code"]]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE client_org_id IN ({ph})", org_ids).fetchall()]
        purge(conn, "invoice", "client_org_id", org_ids)
        purge(conn, "project_documents", "project_code", codes)
        for t in ("proposal_line", "proposal_event", "proposal_revision"):
            purge(conn, t, "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        for t in ("pm_project_assignment", "project_assignments", "project_modules", "ira_job"):
            try:
                purge(conn, t, "project_code", codes)
            except Exception:
                conn.rollback()
        purge(conn, "estimate_document", "estimate_id", est_ids)
        purge(conn, "projects", "project_code", codes)
        purge(conn, "estimate", "id", est_ids)
        conn.execute("DELETE FROM building WHERE address_line LIKE '% Sigc %'")
        for t, col in (("crm_activity", "entity_id"), ("crm_task", "entity_id"), ("crm_contact", "org_id")):
            purge(conn, t, col, org_ids)
        purge(conn, "crm_organization", "id", org_ids)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login():
    c = server.app.test_client()
    assert c.post("/api/auth/login", json={"email": f"{TAG}.c_suite@example.com", "password": PW}).status_code == 200
    return c


def _contracts(code):
    conn = _db()
    try:
        return [dict(r) for r in conn.execute("SELECT id, requirement_key, source_proposal_id, file_path, title, category "
                                              "FROM project_documents WHERE project_code = ? AND category = 'CONTRACTS' ORDER BY id",
                                              (code,)).fetchall()]
    finally:
        conn.close()


def main() -> int:
    ids = _seed()
    passed = 0
    cs = _login()

    # ---- Ready to start: the signed proposal becomes the job's contract
    r = cs.post("/api/admin/projects/onboard", json={"from_proposal_id": ids["A"], "address": "1 Sigc John St", "name": "1 Sigc John St"})
    assert r.status_code in (200, 201), r.get_data(as_text=True)[:300]
    code = r.get_json()["data"]["project_code"]
    rows = _contracts(code)
    assert len(rows) == 1 and rows[0]["source_proposal_id"] == ids["A"], rows
    assert rows[0]["requirement_key"] == "owner_contract", rows
    assert rows[0]["title"].startswith("Signed proposal"), rows
    p = ssc_paths.resolve_data_path(rows[0]["file_path"])
    assert p.exists() and p.resolve().is_relative_to(ssc_paths.under_root("data_room", "project_docs").resolve()), p
    assert p.read_bytes() == PDF
    passed += 4
    f = cs.get(f"/api/documents/{rows[0]['id']}/file")
    assert f.status_code == 200 and f.data.startswith(b"%PDF"), f.status_code
    passed += 1
    d = cs.get(f"/api/projects/{code}/documents")
    assert d.status_code == 200
    assert "owner_contract" in d.get_data(as_text=True) and "Signed proposal" in d.get_data(as_text=True)
    passed += 1

    # ---- idempotent
    cs.get(f"/api/projects/{code}/documents")
    conn = _db()
    try:
        assert signed_contracts.ensure_filed(conn, code) == []
        assert signed_contracts.file_signed_proposal(conn, ids["A"]) is None
    finally:
        conn.close()
    assert len(_contracts(code)) == 1
    passed += 1

    # ---- backfill: a job started before this existed gets its contract when its documents are opened
    conn = _db()
    try:
        conn.execute("DELETE FROM project_documents WHERE project_code = ?", (code,))
        conn.commit()
    finally:
        conn.close()
    assert cs.get(f"/api/projects/{code}/documents").status_code == 200
    rows = _contracts(code)
    assert len(rows) == 1 and rows[0]["source_proposal_id"] == ids["A"] and rows[0]["requirement_key"] == "owner_contract", rows
    passed += 1

    # ---- signed on a job that already exists: the estimate became a job first, then the proposal was signed
    conn = _db()
    try:
        estimates.change_status(conn, ids["estB"]["id"], "approved", final_amount=9000, qb_estimate_ref="Q-SIGC", actor_user_id=ids["admin"])
        res = estimates.convert_to_project(conn, ids["estB"]["id"], actor_user_id=ids["admin"])
        codeB = res["project_code"]
        assert _contracts(codeB) == [], "nothing signed yet, nothing filed"
        proposals.mark_signed_offline(conn, ids["B"], name="Pat Signer", actor=ids["admin"])
    finally:
        conn.close()
    rows = _contracts(codeB)
    assert len(rows) == 1 and rows[0]["source_proposal_id"] == ids["B"] and rows[0]["requirement_key"] == "owner_contract", rows
    passed += 1

    # ---- a second signed proposal on a job whose contract item is filled: an extra Contracts document
    conn = _db()
    try:
        conn.execute("UPDATE project_documents SET source_proposal_id = NULL WHERE project_code = ?", (codeB,))   # an uploaded contract
        conn.commit()
        assert signed_contracts.file_signed_proposal(conn, ids["B"]) is not None
    finally:
        conn.close()
    rows = _contracts(codeB)
    assert len(rows) == 2 and rows[1]["requirement_key"] is None and rows[1]["source_proposal_id"] == ids["B"], rows
    passed += 1

    print(f"PASS smoke_signed_contracts: {passed} checks")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        _cleanup()
    sys.exit(rc)
