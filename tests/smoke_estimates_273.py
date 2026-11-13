#!/usr/bin/env python3
"""#273 — Estimates/bid tracking guard smoke (dual-backend).

Proves the load-bearing behaviours, fail->pass where practical:
  (a) NUMERIC allocation — the 013-collision probe: with seq 9 AND 13 planted in the
      series (plus a pre-existing PROJECT in the same series), the next API-created
      code is -014, never -010 (the E-00013 lexicographic lesson, day one).
  (b) TRANSITION VALIDATION — illegal jumps 400 (intake->approved, ->converted via
      status, converted->anything); approve without final_amount+qb_estimate_ref 400;
      the legal chain 200s; user-picked LOCAL dates stored VERBATIM.
  (c) CONVERT — creates EXACTLY ONE projects row (status active, client_org_id
      backlinked), stamps converted_project_code + status; double-click returns
      already=true and creates nothing; convert on non-approved 400.
  (d) DOCS GATED BY-ID — upload 200 (c_suite), serve inline; pm AND super AND client
      403 on the file route; unknown id 404; delete-with-docs 409 (nothing
      hard-deletes once documents exist).
  (e) IRA EXTENSION — row exists for IRA from create; fields persist via PUT;
      PUT on a non-IRA type 400.
  (f) ROLE GATES — pm/super 403 on EVERY estimates endpoint; the console page itself
      403s them (#263) so the section is fully absent from their reachable nav;
      client containment untouched (403, never data).
  (g) FORBIDDEN KEYS — no *_path/file_path key in ANY payload seen; the converted
      project's row on an operational surface carries NO amount keys.

Isolation (CLAUDE.md): runs against SMOKE_BASE (the gate's isolated server) and
REFUSES to run without SSC_DB_URL (would otherwise seed the live DB). Synthetic-only
data (SMK273-* / smk273-* / the IR-SI + FR-SI + IRA-SI series), is_system=1, random
per-run password, FK-safe scoped teardown (children before parents, disk docs too).
PII-safe output: counts/booleans/status codes only.
"""
from __future__ import annotations

import os
import shutil
import sys
import secrets
from datetime import date
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import ssc_paths  # noqa: E402  # #287 — fixture paths honor SSC_DATA_ROOT

import db_layer            # noqa: E402
from auth import hash_password  # noqa: E402
from apply_estimates_273 import ensure_estimates_schema  # noqa: E402
from apply_crm_266 import _table_exists  # noqa: E402
import crm                 # noqa: E402

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5050")
PW = secrets.token_urlsafe(18)
USERS = {
    "csuite": "smk273-csuite@superstars.local",
    "pm":     "smk273-pm@superstars.local",
    "super":  "smk273-super@superstars.local",
    "client": "smk273-client@superstars.local",
}
ROLE_OF = {"csuite": "c_suite", "pm": "pm", "super": "super", "client": "client"}
ORG_NAME = "SMK273 Client Org"
SERIES = (("IRA", "SI"), ("FR", "SI"), ("IR", "SI"))   # synthetic series, live has none
SUB_DATE = "2026-03-05"    # user-picked LOCAL dates (verbatim persistence)
DEC_DATE = "2026-03-20"
_DOC_DIR = ssc_paths.under_root("data_room", "estimate_docs")   # #287

PASS, FAIL = [], []
SEEN_PAYLOADS = []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


# ---------------- setup / teardown ----------------

def _seed():
    conn = db_layer.connect(pragma_fk=True)
    try:
        ensure_estimates_schema(conn)
        for key, email in USERS.items():
            role = ROLE_OF[key]
            row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE users SET password_hash=?, role=?, is_active=1, status='active', "
                    "must_reset_password=0, is_system=1 WHERE email=?", (hash_password(PW), role, email))
            else:
                conn.execute(
                    "INSERT INTO users (email,password_hash,role,full_name,is_active,status,"
                    "must_reset_password,is_system) VALUES (?,?,?,?,1,'active',0,1)",
                    (email, hash_password(PW), role, f"SMK273 {key}"))
        conn.commit()
        if not conn.execute("SELECT 1 FROM crm_organization WHERE name=?", (ORG_NAME,)).fetchone():
            crm.create_org(conn, name=ORG_NAME, relationship_type="client")
        return conn.execute("SELECT id FROM crm_organization WHERE name=?", (ORG_NAME,)).fetchone()[0]
    finally:
        conn.close()


def _cleanup():
    conn = db_layer.connect(pragma_fk=True)
    try:
        codes = []
        # #274 made converting an IRA estimate create ira_job/ira_visit children — a
        # deliberate guard update (#263-class): clear them first when the tables exist.
        has_ira = _table_exists(conn, "ira_job")
        for t, b in SERIES:
            for r in conn.execute("SELECT id, code FROM estimate WHERE est_type=? AND borough=?",
                                  (t, b)).fetchall():
                codes.append(r[1])
                if has_ira:
                    conn.execute("DELETE FROM ira_visit WHERE project_code=?", (r[1],))
                    conn.execute("DELETE FROM ira_job WHERE project_code=?", (r[1],))
                if _table_exists(conn, "assistant_thread"):   # the delete-guard check below plants one
                    conn.execute("DELETE FROM assistant_thread WHERE estimate_id=?", (r[0],))
                if _table_exists(conn, "proposal"):   # ...and draft / sent proposals
                    for (pid,) in conn.execute("SELECT id FROM proposal WHERE estimate_id=?", (r[0],)).fetchall():
                        conn.execute("DELETE FROM proposal_line WHERE proposal_id=?", (pid,))
                        conn.execute("DELETE FROM proposal_event WHERE proposal_id=?", (pid,))
                    conn.execute("DELETE FROM proposal WHERE estimate_id=?", (r[0],))
                conn.execute("DELETE FROM estimate_document WHERE estimate_id=?", (r[0],))
                conn.execute("DELETE FROM estimate_ira WHERE estimate_id=?", (r[0],))
                conn.execute("DELETE FROM estimate WHERE id=?", (r[0],))
            for r in conn.execute("SELECT project_code FROM projects WHERE project_code LIKE ?",
                                  (f"{t}-{b}-%",)).fetchall():
                conn.execute("DELETE FROM projects WHERE project_code=?", (r[0],))
        org = conn.execute("SELECT id FROM crm_organization WHERE name=?", (ORG_NAME,)).fetchone()
        if org:
            conn.execute("DELETE FROM crm_activity WHERE entity_type='organization' AND entity_id=?", (org[0],))
            conn.execute("DELETE FROM crm_contact WHERE org_id=?", (org[0],))
            conn.execute("DELETE FROM crm_organization WHERE id=?", (org[0],))
        for email in USERS.values():
            u = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if u:
                conn.execute("DELETE FROM login_audit WHERE user_id=?", (u[0],))
                conn.execute("DELETE FROM sessions WHERE user_id=?", (u[0],))
                conn.execute("DELETE FROM users WHERE id=?", (u[0],))
        conn.commit()
        for c in codes:   # on-disk smoke docs
            shutil.rmtree(_DOC_DIR / c, ignore_errors=True)
    finally:
        conn.close()


def _login(key):
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": USERS[key], "password": PW}, timeout=10)
    return s if (r.status_code == 200 and s.cookies.get("ssc_session")) else None


def _sc(sess, method, path, **kw):
    r = sess.request(method, f"{BASE}{path}", timeout=15, **kw)
    body = None
    try:
        body = r.json()
        SEEN_PAYLOADS.append(body)
    except Exception:
        pass
    return r.status_code, body


def _forbidden_keys(obj, found):
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if kl.endswith("_path") or kl in ("filepath", "folder_slug"):
                found.append(k)
            _forbidden_keys(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _forbidden_keys(v, found)


# ---------------- checks ----------------

def main():
    if not (os.environ.get("SSC_DB_URL") or "").strip():
        print("REFUSING TO RUN: SSC_DB_URL is unset — this smoke seeds users/orgs and "
              "must never touch the live superstars.db.")
        return 2
    print(f"#273 estimates guard — BASE={BASE}  backend={'postgres' if db_layer.is_postgres() else 'sqlite'}")
    _cleanup()
    org_id = _seed()
    try:
        cs, pm, su, cl = _login("csuite"), _login("pm"), _login("super"), _login("client")
        ok("logins", all([cs, pm, su]), "csuite/pm/super authenticate")
        if not cs:
            print("cannot proceed without c_suite session"); return 1

        # ---------- (f) ROLE GATES first (fail->pass by role on the same calls) ----------
        GATED = [
            ("GET", "/api/estimates"), ("GET", "/api/estimates/meta"),
            ("POST", "/api/estimates"), ("GET", "/api/estimates/1"),
            ("POST", "/api/estimates/1/status"), ("POST", "/api/estimates/1/convert"),
            ("PUT", "/api/estimates/1/ira"), ("GET", "/api/estimates/1/documents"),
            ("GET", "/api/estimates/documents/1/file"),
        ]
        for role, sess in (("pm", pm), ("super", su)):
            for m, p in GATED:
                st, _ = _sc(sess, m, p, json={} if m in ("POST", "PUT") else None)
                ok(f"{role}_403 {m} {p}", st == 403)
            st = sess.get(f"{BASE}/", timeout=10, allow_redirects=False).status_code
            ok(f"{role}_console_403 (section absent from nav)", st == 403)
        if cl:
            st, _ = _sc(cl, "GET", "/api/estimates")
            ok("client_containment_403", st == 403)
        ok("csuite_meta_200", _sc(cs, "GET", "/api/estimates/meta")[0] == 200)

        # ---------- (a) numeric allocation + the 013 probe ----------
        conn = db_layer.connect()
        try:
            pre = conn.execute("SELECT COUNT(*) FROM estimate WHERE est_type='IR' AND borough='SI'").fetchone()[0]
            ok("ir_si_series_clean", pre == 0)
            # plant seq 9 + 13 directly (zero-padded codes), plus a PROJECT in-series
            now = "2026-07-04T08:00:00"
            for sq in (9, 13):
                conn.execute(
                    "INSERT INTO estimate (code, est_type, borough, seq, client_org_id, status, "
                    "created_at, updated_at, status_changed_at) VALUES (?,?,?,?,?, 'intake', ?,?,?)",
                    (f"IR-SI-{sq:03d}", "IR", "SI", sq, org_id, now, now, now))
            conn.execute("INSERT INTO projects (project_code, name, status) VALUES ('FR-SI-013','smk273 planted','active')")
            conn.commit()
        finally:
            conn.close()
        st, body = _sc(cs, "POST", "/api/estimates",
                       json={"est_type": "IR", "borough": "SI", "client_org_id": org_id})
        ok("alloc_after_9_and_13_is_014", st == 200 and (body or {}).get("data", {}).get("code") == "IR-SI-014",
           f"got {(body or {}).get('data', {}).get('code')}")
        st, body = _sc(cs, "POST", "/api/estimates",
                       json={"est_type": "FR", "borough": "SI", "client_org_id": org_id,
                             "building_address": "SMK273 88 Facade Ave"})
        fr_id = (body or {}).get("data", {}).get("id")
        ok("alloc_seeded_from_projects_series", st == 200 and (body or {}).get("data", {}).get("code") == "FR-SI-014",
           f"got {(body or {}).get('data', {}).get('code')}")
        st, body = _sc(cs, "POST", "/api/estimates",
                       json={"est_type": "IRA", "borough": "SI", "client_org_id": org_id,
                             "building_address": "SMK273 2500 Tower Rd"})
        ira_id = (body or {}).get("data", {}).get("id")
        ira_code = (body or {}).get("data", {}).get("code")
        ok("ira_created_001", st == 200 and ira_code == "IRA-SI-001")
        st, _ = _sc(cs, "POST", "/api/estimates",
                    json={"est_type": "XX", "borough": "SI", "client_org_id": org_id})
        ok("unknown_type_400", st == 400)

        # ---------- (e) IRA extension lifecycle ----------
        st, body = _sc(cs, "GET", f"/api/estimates/{ira_id}")
        d = (body or {}).get("data", {})
        ok("ira_row_exists_from_create", st == 200 and d.get("ira") is not None)
        st, body = _sc(cs, "PUT", f"/api/estimates/{ira_id}/ira",
                       json={"dob_registered_email": "smk273@superstars.local",
                             "engineer_name": "SMK273 PE", "scope_mode": "drops",
                             "scope_value": 24, "internal_drop_calc": 26})
        ok("ira_put_200", st == 200)
        st, body = _sc(cs, "GET", f"/api/estimates/{ira_id}")
        ira = ((body or {}).get("data", {}) or {}).get("ira") or {}
        ok("ira_fields_persist", ira.get("scope_mode") == "drops" and ira.get("scope_value") == 24
           and ira.get("internal_drop_calc") == 26)
        st, _ = _sc(cs, "PUT", f"/api/estimates/{fr_id}/ira", json={"engineer_name": "x"})
        ok("ira_put_on_fr_400", st == 400)

        # ---------- (b) transition validation ----------
        st, _ = _sc(cs, "POST", f"/api/estimates/{ira_id}/status", json={"status": "approved"})
        ok("intake_to_approved_400", st == 400)
        st, _ = _sc(cs, "POST", f"/api/estimates/{ira_id}/status", json={"status": "converted"})
        ok("status_jump_to_converted_400", st == 400)
        st, _ = _sc(cs, "POST", f"/api/estimates/{ira_id}/convert")
        ok("convert_from_intake_400", st == 400)
        st, _ = _sc(cs, "POST", f"/api/estimates/{ira_id}/status", json={"status": "scoping"})
        ok("intake_to_scoping_200", st == 200)
        st, body = _sc(cs, "POST", f"/api/estimates/{ira_id}/status",
                       json={"status": "submitted", "date": SUB_DATE})
        ok("scoping_to_submitted_200", st == 200)
        ok("submitted_date_verbatim", ((body or {}).get("data", {}) or {}).get("submitted_date") == SUB_DATE)
        st, _ = _sc(cs, "POST", f"/api/estimates/{ira_id}/status", json={"status": "approved"})
        ok("approve_without_amount_400", st == 400)
        st, body = _sc(cs, "POST", f"/api/estimates/{ira_id}/status",
                       json={"status": "approved", "final_amount": 18500,
                             "qb_estimate_ref": "QB-273", "date": DEC_DATE})
        ok("approve_with_amount_ref_200", st == 200)
        d = (body or {}).get("data", {}) or {}
        ok("decided_date_verbatim", d.get("decided_date") == DEC_DATE)
        ok("final_amount_persists", d.get("final_amount") == 18500)

        # ---------- (c) convert: one project, backlink, idempotent ----------
        st, body = _sc(cs, "POST", f"/api/estimates/{ira_id}/convert")
        d = (body or {}).get("data", {}) or {}
        ok("convert_200_not_already", st == 200 and d.get("already") is False
           and d.get("project_code") == ira_code)
        st, body = _sc(cs, "POST", f"/api/estimates/{ira_id}/convert")
        d2 = (body or {}).get("data", {}) or {}
        ok("convert_doubleclick_already", st == 200 and d2.get("already") is True)
        conn = db_layer.connect()
        try:
            n = conn.execute("SELECT COUNT(*) FROM projects WHERE project_code=?", (ira_code,)).fetchone()[0]
            row = conn.execute("SELECT status, client_org_id, name FROM projects WHERE project_code=?",
                               (ira_code,)).fetchone()
            est = conn.execute("SELECT status, converted_project_code, status_changed_at FROM estimate WHERE id=?",
                               (ira_id,)).fetchone()
            act = conn.execute(
                "SELECT COUNT(*) FROM crm_activity WHERE entity_type='organization' AND entity_id=? "
                "AND function_tag='sales'", (org_id,)).fetchone()[0]
        finally:
            conn.close()
        ok("exactly_one_project_row", n == 1)
        ok("project_active_and_backlinked", row and row[0] == "active" and row[1] == org_id)
        ok("estimate_stamped_converted", est and est[0] == "converted" and est[1] == ira_code)
        ok("status_changed_at_local_today", bool(est and str(est[2]).startswith(date.today().isoformat())))
        ok("activity_sales_tagged_rows", act >= 5)   # opens + transitions + convert
        st, _ = _sc(cs, "POST", f"/api/estimates/{ira_id}/status", json={"status": "lost"})
        ok("converted_is_terminal_400", st == 400)

        # ---------- (d) documents: upload, gated serve, no-hard-delete ----------
        pdf = b"%PDF-1.4\n1 0 obj<</T(smk273)>>endobj\ntrailer<<>>\n%%EOF\n"
        st, body = _sc(cs, "POST", f"/api/estimates/{fr_id}/documents",
                       files={"file": ("smk273-bid.pdf", pdf, "application/pdf")},
                       data={"category": "bid_file", "title": "SMK273 bid"})
        doc_id = ((body or {}).get("data", {}) or {}).get("id")
        ok("doc_upload_200", st == 200 and bool(doc_id))
        st, body = _sc(cs, "GET", f"/api/estimates/{fr_id}/documents")
        docs = (body or {}).get("data", []) or []
        ok("doc_listed", any(x.get("id") == doc_id for x in docs))
        ok("doc_payload_has_gated_url_no_path",
           docs and docs[0].get("file_url", "").startswith("/api/estimates/documents/")
           and "file_path" not in docs[0])
        r = cs.get(f"{BASE}/api/estimates/documents/{doc_id}/file", timeout=10)
        ok("doc_serves_200_inline", r.status_code == 200
           and "inline" in (r.headers.get("Content-Disposition") or "").lower()
           and r.content.startswith(b"%PDF"))
        for role, sess in (("pm", pm), ("super", su), ("client", cl)):
            if sess:
                stx = sess.get(f"{BASE}/api/estimates/documents/{doc_id}/file", timeout=10).status_code
                ok(f"doc_file_{role}_403", stx == 403)
        st, _ = _sc(cs, "GET", "/api/estimates/documents/999999/file")
        ok("doc_unknown_404", st == 404)
        st, _ = _sc(cs, "DELETE", f"/api/estimates/{fr_id}")
        ok("delete_with_docs_409", st == 409)
        st, _ = _sc(cs, "DELETE", f"/api/estimates/{ira_id}")
        ok("delete_converted_409", st == 409)

        # ---------- (d2) Estimates & proposals ⋯ menu (2026-11-13): the list and the page say
        # up front why Delete can't run; a clean one deletes; anything pointing at it blocks ----------
        st, body = _sc(cs, "GET", "/api/estimates")
        rows = {r["id"]: r for r in ((body or {}).get("data") or {}).get("estimates", [])}
        ok("list_blocker_documents", "document" in str((rows.get(fr_id) or {}).get("delete_blocker") or ""))
        ok("list_blocker_converted", "project" in str((rows.get(ira_id) or {}).get("delete_blocker") or ""))
        st, body = _sc(cs, "GET", f"/api/estimates/{fr_id}")
        ok("detail_blocker_documents", "document" in str(((body or {}).get("data") or {}).get("delete_blocker") or ""))
        st, body = _sc(cs, "POST", "/api/estimates", json={"est_type": "IR", "borough": "SI", "client_org_id": org_id})
        clean_id = ((body or {}).get("data") or {}).get("id")
        st, body = _sc(cs, "GET", "/api/estimates")
        row = next((r for r in ((body or {}).get("data") or {}).get("estimates", []) if r["id"] == clean_id), {})
        ok("list_clean_no_blocker", bool(clean_id) and "delete_blocker" in row and row["delete_blocker"] is None)
        def _pdf_doc(est):   # a proposal PDF the app drew (same row shape as proposals.render_pdf)
            c = db_layer.connect()
            try:
                c.execute("INSERT INTO estimate_document (estimate_id, category, title, doc_type, file_path, file_name, "
                          "file_size, mime, uploaded_by, uploaded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (est, "proposal", "Proposal smk273", "PDF", "proposals/smk273.pdf", "smk273.pdf", 10,
                           "application/pdf", None, now))
                did = c.execute("SELECT MAX(id) FROM estimate_document").fetchone()[0]
                c.commit()
                return did
            finally:
                c.close()

        def _q(sql, args):
            c = db_layer.connect()
            try:
                return c.execute(sql, args).fetchone()[0]
            finally:
                c.close()

        # the 24 John Street case: a Studio draft was deleted and its PDF stayed on the estimate,
        # another draft is still there with its PDF, and Claude drafted it -> all of that goes with it
        _pdf_doc(clean_id)   # orphan: its draft is long gone
        st, body = _sc(cs, "POST", "/api/proposals", json={"estimate_id": clean_id})
        draft_id = ((body or {}).get("data") or {}).get("id")
        ok("draft_created", st in (200, 201) and bool(draft_id), f"status {st}")
        d_doc = _pdf_doc(clean_id)
        c = db_layer.connect()
        try:
            c.execute("UPDATE proposal SET pdf_doc_id=? WHERE id=?", (d_doc, draft_id))
            if _table_exists(c, "assistant_thread"):
                c.execute("INSERT INTO assistant_thread (estimate_id, title, source, created_at, updated_at) "
                          "VALUES (?, 'smk273', 'office', ?, ?)", (clean_id, now, now))
            c.commit()
        finally:
            c.close()
        st, body = _sc(cs, "GET", f"/api/estimates/{clean_id}")
        d = (body or {}).get("data") or {}
        ok("leftovers_do_not_block", d.get("delete_blocker") is None, str(d.get("delete_blocker")))
        ok("delete_note_lists_leftovers", "1 unsent draft proposal" in str(d.get("delete_note"))
           and "2 proposal PDFs" in str(d.get("delete_note")), str(d.get("delete_note")))
        # deleting a draft now takes its PDF with it (it used to stay as a "document")
        st, body = _sc(cs, "POST", "/api/proposals", json={"estimate_id": fr_id})
        d2 = ((body or {}).get("data") or {}).get("id")
        d2_doc = _pdf_doc(fr_id)
        c = db_layer.connect()
        try:
            c.execute("UPDATE proposal SET pdf_doc_id=? WHERE id=?", (d2_doc, d2)); c.commit()
        finally:
            c.close()
        st, _ = _sc(cs, "DELETE", f"/api/proposals/{d2}")
        ok("draft_delete_takes_its_pdf", st == 200 and _q("SELECT COUNT(*) FROM estimate_document WHERE id=?", (d2_doc,)) == 0)
        ok("draft_delete_keeps_uploads", _q("SELECT COUNT(*) FROM estimate_document WHERE estimate_id=? AND category<>'proposal'", (fr_id,)) >= 1)
        # a SENT proposal is real: it blocks
        c = db_layer.connect()
        try:
            c.execute("UPDATE proposal SET status='sent' WHERE id=?", (draft_id,)); c.commit()
        finally:
            c.close()
        st, body = _sc(cs, "DELETE", f"/api/estimates/{clean_id}")
        ok("sent_proposal_blocks_409", st == 409 and "sent or signed proposal" in str((body or {}).get("error")))
        c = db_layer.connect()
        try:
            c.execute("UPDATE proposal SET status='draft' WHERE id=?", (draft_id,)); c.commit()
        finally:
            c.close()
        st, _ = _sc(cs, "DELETE", f"/api/estimates/{clean_id}")
        ok("delete_with_leftovers_200", st == 200)
        ok("deleted_is_gone_404", _sc(cs, "GET", f"/api/estimates/{clean_id}")[0] == 404)
        ok("leftovers_removed", _q("SELECT COUNT(*) FROM proposal WHERE estimate_id=?", (clean_id,)) == 0
           and _q("SELECT COUNT(*) FROM estimate_document WHERE estimate_id=?", (clean_id,)) == 0
           and _q("SELECT COUNT(*) FROM assistant_thread WHERE estimate_id=?", (clean_id,)) == 0)

        # ---------- (g) forbidden keys across everything seen ----------
        found = []
        _forbidden_keys(SEEN_PAYLOADS, found)
        ok("no_path_keys_anywhere", not found, f"saw: {sorted(set(found))[:5]}")
        st, body = _sc(su, "GET", "/api/projects")
        keys = set()
        for p in ((body or {}).get("data") or []):
            if isinstance(p, dict) and p.get("project_code") == ira_code:
                keys = set(p.keys())
        ok("converted_project_visible_operationally", st == 200 and bool(keys))
        ok("no_amount_keys_on_project_surface",
           not (keys & {"final_amount", "qb_estimate_ref", "amount"}))

        print(f"\n== RESULT: {len(PASS)} PASS / {len(FAIL)} FAIL ==")
        if FAIL:
            print("FAILURES: " + ", ".join(FAIL))
        print("OVERALL:", "PASS" if not FAIL else "FAIL")
        return 0 if not FAIL else 1
    finally:
        _cleanup()


if __name__ == "__main__":
    sys.exit(main())
