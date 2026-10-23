#!/usr/bin/env python3
"""#307 — proposals: behavioral guard.

Flask test client on an ISOLATED database (honors SSC_DB_URL; builds a throwaway
SQLite snapshot from the versioned migrations when unset) with a scratch data root
and the fake mail seam. Proves:

  * ensure_schema is idempotent; 'proposal' is a valid estimate_document category
  * gating: pm / super / estimator / client -> 403 on /api/proposals; admin / c_suite -> 200
  * create from estimate: prefilled client / contact / building / price, number
    <code>-P1, status draft; a second open proposal for the same estimate -> 409
  * edit a draft (fields + schedule-of-values lines; price follows the lines;
    deposit amount derived); bad values -> 400
  * send: requires a price + email; writes the email to the fake dir with a
    /p/<token> link; status sent; a proposal PDF row lands in estimate_document
    when the chromium engine is present (and nothing breaks when it is not)
  * public page: bad token -> 404; good token -> 200, status viewed (once);
    logged-out AND logged-in-as-estimator both reach it (public + containment
    allowlists); the client-portal allowlist admits it too
  * accept: missing name / unchecked box -> 400; accept -> signed with name, IP,
    timestamp; THE TRIGGER: estimate -> approved at the proposal price, CRM tasks
    for the COI and the deposit invoice; a second accept -> 409; edit after -> 409
  * new version: sent/viewed -> superseded + fresh draft v2 with the same lines;
    the old link stops working; decline path on v2 (task opened); expired link
    -> the page says expired and accept -> 409
  * mark signed offline fires the same trigger; delete only a draft
  * no `*_path` / token keys in any JSON payload

Run:  venv/bin/python tests/smoke_proposals_307.py
"""
from __future__ import annotations

import json
import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")   # test-only seams are inert without this (runtime_seams.py)
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_prop_307_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
MAIL_DIR = Path(_TMP) / "mail"
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ["SSC_MAIL_FAKE_DIR"] = str(MAIL_DIR)
os.environ["APP_BASE_URL"] = "http://test.local"

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import buildings  # noqa: E402
import client_portal  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import proposals  # noqa: E402
import role_containment  # noqa: E402

TAG = "prop307"
PW = "ProposalsSmokePassword2026"
ROLES = ("c_suite", "admin", "pm", "super", "estimator", "client")
NOW = datetime.now().isoformat(timespec="seconds")
TODAY = date.today().isoformat()


def _db():
    return db_layer.connect(pragma_fk=True)


def _seed() -> dict:
    ids = {}
    conn = _db()
    try:
        for role in ROLES:
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                         "must_reset_password, is_system) VALUES (?, ?, ?, ?, 1, 'active', 0, 1)",
                         (email, auth.hash_password(PW), role, f"Smoke {role}"))
        conn.execute("DELETE FROM crm_organization WHERE name = ?", ("Prop307 Riverside Owners",))
        ids["org"] = crm.create_org(conn, name="Prop307 Riverside Owners", relationship_type="client")
        ids["contact"] = crm.create_contact(conn, full_name="Prop307 Dana Client", org_id=ids["org"],
                                            email="prop307.client@example.com", title="Board president")
        ids["admin"] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.admin@example.com",)).fetchone()["id"]
        est = estimates.create_estimate(conn, est_type="FR", borough="BX", client_org_id=ids["org"],
                                        contact_id=ids["contact"], building_address="41 Prop307 Terrace",
                                        notes="Repoint the north parapet, replace 14 lintels.",
                                        created_by=ids["admin"])
        ids["est"] = est["id"]; ids["code"] = est["code"]
        estimates.change_status(conn, est["id"], "scoping", actor_user_id=ids["admin"])
        estimates.change_status(conn, est["id"], "submitted", final_amount=48500, actor_user_id=ids["admin"])
        est2 = estimates.create_estimate(conn, est_type="FR", borough="BX", client_org_id=ids["org"],
                                         building_address="42 Prop307 Terrace", created_by=ids["admin"])
        ids["est2"] = est2["id"]; ids["code2"] = est2["code"]
        est3 = estimates.create_estimate(conn, est_type="FR", borough="BX", client_org_id=ids["org"],
                                         building_address="43 Prop307 Terrace", created_by=ids["admin"])
        ids["est3"] = est3["id"]; ids["code3"] = est3["code"]
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Prop307 %'").fetchall()]
        est_ids = [r["id"] for r in conn.execute("SELECT id FROM estimate WHERE building_address LIKE '% Prop307 %'").fetchall()]
        pids = [r["id"] for r in conn.execute(
            f"SELECT id FROM proposal WHERE estimate_id IN ({','.join('?' * len(est_ids)) or 'NULL'})", est_ids).fetchall()]
        try:
            inv_ids = [r["id"] for r in conn.execute(
                f"SELECT id FROM invoice WHERE proposal_id IN ({','.join('?' * len(pids)) or 'NULL'})", pids).fetchall()] if pids else []
        except Exception:   # invoice table absent on an older fixture
            conn.rollback(); inv_ids = []
        purge(conn, "invoice_payment", "invoice_id", inv_ids)   # #315: signatures draft deposit invoices
        purge(conn, "invoice", "id", inv_ids)
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate_document", "estimate_id", est_ids)
        purge(conn, "estimate", "id", est_ids)
        conn.execute("DELETE FROM building WHERE address_key LIKE '4_ prop307 %'")
        conn.execute("DELETE FROM crm_activity WHERE summary LIKE '%Prop307%' OR summary LIKE '%-P1%' OR summary LIKE '%-P2%'")
        if org_ids:
            ph = ",".join("?" * len(org_ids))
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph})", org_ids)
            conn.execute(f"DELETE FROM crm_task WHERE entity_type = 'organization' AND entity_id IN ({ph})", org_ids)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph})", org_ids)
        conn.execute("DELETE FROM notification_log WHERE subject LIKE '%Prop307%'")
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, ids)
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Prop307 %'")
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


FORBIDDEN_KEYS = ("token_hash", "token", "html_snapshot")


def _no_secrets(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert not k.endswith("_path"), k
            assert k not in FORBIDDEN_KEYS, k
            _no_secrets(v)
    elif isinstance(obj, list):
        for v in obj:
            _no_secrets(v)


def _login(app, role):
    c = app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code, r.get_data(as_text=True)[:200])
    return c


def _token_from_mail() -> str:
    eml = sorted(MAIL_DIR.glob("proposal-*.eml"))
    assert eml, "no proposal email written"
    body = eml[-1].read_text(encoding="utf-8")
    m = re.search(r"http://test\.local/p/([A-Za-z0-9_\-]+)", body)
    assert m, body[:300]
    return m.group(1)


def main() -> int:
    app = Flask("prop307", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    role_containment.register(app)
    buildings.register(app)
    estimates.register(app)
    proposals.register(app)
    conn = _db()
    try:
        proposals.ensure_schema(conn)   # idempotent
        proposals.ensure_schema(conn)
    finally:
        conn.close()
    assert "proposal" in estimates.DOC_CATEGORIES
    assert client_portal._client_always_allowed("/p/abc") and client_portal._client_always_allowed("/api/p/abc/accept")

    ids = _seed()
    passed = 0

    # ---- gates
    for role in ("pm", "super", "estimator", "client"):
        c = _login(app, role)
        assert c.get("/api/proposals").status_code == 403, role
    anon = app.test_client()
    assert anon.get("/api/proposals").status_code == 401
    admin = _login(app, "admin")
    cs = _login(app, "c_suite")
    assert cs.get("/api/proposals").status_code == 200
    passed += 1; print("  gates ok")

    # ---- create from estimate
    r = admin.post("/api/proposals", json={"estimate_id": ids["est"]})
    assert r.status_code == 201, r.get_data(as_text=True)
    p = r.get_json()["data"]; _no_secrets(p)
    assert p["number"] == f"{ids['code']}-P1" and p["status"] == "draft" and p["version"] == 1
    assert p["client_org_id"] == ids["org"] and p["contact_id"] == ids["contact"]
    assert p["recipient_email"] == "prop307.client@example.com" and p["recipient_name"] == "Prop307 Dana Client"
    assert p["price"] == 48500 and p["deposit_pct"] == 50 and p["deposit_amount"] == 24250   # the house deposit (2026-10-21)
    assert p["layout"] == "ssc" and p["label"].isdigit()
    assert p["building_id"] and "Prop307 Terrace" in (p["building_address"] or "")
    assert p["scope_summary"].startswith("Repoint") and p["editable"] is True
    assert admin.post("/api/proposals", json={"estimate_id": ids["est"]}).status_code == 409
    assert admin.post("/api/proposals", json={"estimate_id": 99999999}).status_code == 404
    assert admin.post("/api/proposals", json={}).status_code == 400
    pid = p["id"]
    passed += 1; print("  create ok")

    # ---- edit a draft
    r = admin.patch(f"/api/proposals/{pid}", json={
        "title": "North parapet restoration", "deposit_pct": 25,
        "schedule_text": "Mobilize within 2 weeks of the deposit; 6 weeks on site.",
        "valid_until": (date.today() + timedelta(days=45)).isoformat(),
        "lines": [{"description": "Repoint north parapet", "qty": 1, "unit": "ls", "unit_price": 30000},
                  {"description": "Replace steel lintels", "qty": 14, "unit": "ea", "unit_price": 1250},
                  {"description": "", "qty": 1, "unit_price": 999}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    p = r.get_json()["data"]; _no_secrets(p)
    assert p["price"] == 47500 and p["deposit_amount"] == 11875 and len(p["lines"]) == 2
    assert p["lines"][1]["amount"] == 17500 and p["title"] == "North parapet restoration"
    assert admin.patch(f"/api/proposals/{pid}", json={"deposit_pct": 140}).status_code == 400
    assert admin.patch(f"/api/proposals/{pid}", json={"valid_until": "yesterday"}).status_code == 400
    assert admin.patch(f"/api/proposals/{pid}", json={"price": -1}).status_code == 400
    prev = admin.get(f"/api/proposals/{pid}/preview")
    assert prev.status_code == 200 and b"North parapet restoration" in prev.data and b"$47,500.00" in prev.data
    passed += 1; print("  edit ok")

    # ---- send
    assert admin.post(f"/api/proposals/{pid}/send", json={"to_email": "nope"}).status_code == 400
    r = admin.post(f"/api/proposals/{pid}/send", json={"message": "Thanks for walking the roof with us."})
    assert r.status_code == 200, r.get_data(as_text=True)
    p = r.get_json()["data"]; _no_secrets(p)
    assert p["status"] == "sent" and p["sent_at"] and p["link_expires_at"].startswith(p["valid_until"])
    assert any(ev["event"] == "sent" for ev in p["events"])
    token = _token_from_mail()
    html_mail = sorted(MAIL_DIR.glob("proposal-*.html"))[-1].read_text(encoding="utf-8")
    assert "Thanks for walking the roof" in html_mail and f"/p/{token}" in html_mail and "$47,500.00" in html_mail
    conn = _db()
    try:
        docs = conn.execute("SELECT category, mime FROM estimate_document WHERE estimate_id = ?", (ids["est"],)).fetchall()
        pdf_ok = any(d["category"] == "proposal" and d["mime"] == "application/pdf" for d in docs)
        assert pdf_ok == p["has_pdf"]
        row = conn.execute("SELECT token_hash FROM proposal WHERE id = ?", (pid,)).fetchone()
        assert row["token_hash"] and row["token_hash"] != token and len(row["token_hash"]) == 64
    finally:
        conn.close()
    print(f"  send ok (pdf engine {'present' if pdf_ok else 'absent — page is the reference copy'})")
    passed += 1

    # ---- public page
    assert anon.get("/p/not-a-real-token-at-all-0000").status_code == 404
    assert anon.get("/p/x").status_code == 404
    r = anon.get(f"/p/{token}")
    assert r.status_code == 200 and b"Accept this proposal" in r.data and b"North parapet restoration" in r.data
    assert r.headers.get("Cache-Control") == "no-store"
    d = admin.get(f"/api/proposals/{pid}").get_json()["data"]
    assert d["status"] == "viewed" and d["viewed_at"] and d["view_count"] == 1
    anon.get(f"/p/{token}")
    d = admin.get(f"/api/proposals/{pid}").get_json()["data"]
    assert d["view_count"] == 2 and sum(1 for ev in d["events"] if ev["event"] == "viewed") == 1
    est_c = _login(app, "estimator")
    assert est_c.get(f"/p/{token}").status_code == 200        # containment allowlist
    pdf = anon.get(f"/p/{token}/pdf")
    assert pdf.status_code in (200, 503)
    if pdf.status_code == 200:
        assert pdf.mimetype == "application/pdf" and pdf.data[:4] == b"%PDF"
    assert admin.patch(f"/api/proposals/{pid}", json={"title": "x"}).status_code == 409   # not a draft
    passed += 1; print("  public page ok")

    # ---- accept
    assert anon.post(f"/api/p/{token}/accept", json={"name": "D", "agreed": True}).status_code == 400
    assert anon.post(f"/api/p/{token}/accept", json={"name": "Dana Client", "agreed": False}).status_code == 400
    assert anon.post("/api/p/bogus-token-000000000000/accept", json={"name": "Dana Client", "agreed": True}).status_code == 404
    r = anon.post(f"/api/p/{token}/accept", json={"name": "Dana Client", "title": "Board president", "agreed": True},
                  environ_base={"REMOTE_ADDR": "203.0.113.9"})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = admin.get(f"/api/proposals/{pid}").get_json()["data"]
    assert d["status"] == "signed" and d["signed_name"] == "Dana Client" and d["signed_at"] and d["signed_ip"] == "203.0.113.9"
    assert anon.post(f"/api/p/{token}/accept", json={"name": "Dana Client", "agreed": True}).status_code == 409
    r = anon.get(f"/p/{token}")
    assert r.status_code == 200 and b"this proposal is accepted" in r.data and b"Accept this proposal" not in r.data
    conn = _db()
    try:
        est = estimates.get_estimate(conn, ids["est"])
        assert est["status"] == "approved" and est["final_amount"] == 47500 and est["decided_date"] == TODAY
        tasks = [t["title"] for t in crm.list_tasks(conn, status="open") if t["entity_id"] == ids["org"]]
        assert any(t.startswith("Collect Certificate of Insurance") for t in tasks), tasks
        assert any(t.startswith("Issue deposit invoice $11,875.00") for t in tasks), tasks
        acts = [a["summary"] for a in crm.list_activity(conn, entity_type="organization", entity_id=ids["org"])]
        assert any("SIGNED" in a for a in acts)
    finally:
        conn.close()
    assert admin.post(f"/api/proposals/{pid}/new-version").status_code == 409   # signed is final
    assert admin.delete(f"/api/proposals/{pid}").status_code == 409
    passed += 1; print("  accept + trigger ok")

    # ---- second estimate: send, new version, old link dead, decline v2
    r = admin.post("/api/proposals", json={"estimate_id": ids["est2"]}); assert r.status_code == 201
    p2 = r.get_json()["data"]
    assert admin.post(f"/api/proposals/{p2['id']}/send", json={"to_email": "prop307.client@example.com"}).status_code == 400  # no price
    admin.patch(f"/api/proposals/{p2['id']}", json={"price": 9000, "lines": [{"description": "Lintel", "qty": 2, "unit": "EA", "unit_price": 1000}]})
    r = admin.post(f"/api/proposals/{p2['id']}/send", json={"to_email": "prop307.client@example.com", "to_name": "Dana"})
    assert r.status_code == 200, r.get_data(as_text=True)
    tok2 = _token_from_mail()
    assert anon.get(f"/p/{tok2}").status_code == 200
    r = admin.post(f"/api/proposals/{p2['id']}/new-version"); assert r.status_code == 201, r.get_data(as_text=True)
    p2b = r.get_json()["data"]
    assert p2b["version"] == 2 and p2b["number"] == f"{ids['code2']}-P2" and p2b["status"] == "draft"
    # 2026-10-22: the Superstars layout prints its lines' total, so that IS the price (a price apart from the lines was
    # the PDF-vs-proposal mismatch); the price given with the lines above is ignored
    assert [l["description"] for l in p2b["lines"]] == ["Lintel"] and p2b["price"] == 2000 and p2b["amounts"]["total"] == 2000
    old = admin.get(f"/api/proposals/{p2['id']}").get_json()["data"]
    assert old["status"] == "superseded" and old["superseded_by"] == p2b["id"]
    assert anon.get(f"/p/{tok2}").status_code == 404
    assert anon.post(f"/api/p/{tok2}/accept", json={"name": "Dana Client", "agreed": True}).status_code == 404
    r = admin.post(f"/api/proposals/{p2b['id']}/send", json={}); assert r.status_code == 200
    tok3 = _token_from_mail()
    r = anon.post(f"/api/p/{tok3}/decline", json={"reason": "Going with another bid this year."})
    assert r.status_code == 200
    d = admin.get(f"/api/proposals/{p2b['id']}").get_json()["data"]
    assert d["status"] == "declined" and d["decline_reason"].startswith("Going with")
    assert anon.get(f"/p/{tok3}").status_code == 200 and b"was declined" in anon.get(f"/p/{tok3}").data
    assert anon.post(f"/api/p/{tok3}/accept", json={"name": "Dana Client", "agreed": True}).status_code == 409
    conn = _db()
    try:
        tasks = [t["title"] for t in crm.list_tasks(conn, status="open") if t["entity_id"] == ids["org"]]
        assert any(t.startswith("Follow up on declined proposal") for t in tasks), tasks
    finally:
        conn.close()
    assert admin.post(f"/api/proposals/{p2b['id']}/new-version").status_code == 201   # declined can be revised
    passed += 1; print("  versions + decline ok")

    # ---- expired link
    r = admin.post("/api/proposals", json={"estimate_id": ids["est3"]}); p3 = r.get_json()["data"]
    admin.patch(f"/api/proposals/{p3['id']}", json={"price": 1500, "lines": [{"description": "Patch", "qty": 1, "unit": "LS", "unit_price": 1500}]})
    assert admin.post(f"/api/proposals/{p3['id']}/send", json={"to_email": "prop307.client@example.com"}).status_code == 200
    tok4 = _token_from_mail()
    conn = _db()
    try:
        conn.execute("UPDATE proposal SET token_expires_at = ? WHERE id = ?",
                     ((datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"), p3["id"]))
        conn.commit()
    finally:
        conn.close()
    r = anon.get(f"/p/{tok4}")
    assert r.status_code == 200 and b"has expired" in r.data
    assert anon.post(f"/api/p/{tok4}/accept", json={"name": "Dana Client", "agreed": True}).status_code == 409
    d = admin.get(f"/api/proposals/{p3['id']}").get_json()["data"]
    assert d["display_status"] == "expired" and d["status"] == "sent"
    # a resend refreshes the link
    assert admin.post(f"/api/proposals/{p3['id']}/send", json={}).status_code == 200
    tok5 = _token_from_mail()
    assert tok5 != tok4 and anon.get(f"/p/{tok5}").status_code == 200
    # mark signed offline fires the trigger too
    assert admin.post(f"/api/proposals/{p3['id']}/mark-signed", json={"name": ""}).status_code == 400
    r = admin.post(f"/api/proposals/{p3['id']}/mark-signed", json={"name": "Dana Client", "signed_on": TODAY})
    assert r.status_code == 200 and r.get_json()["data"]["status"] == "signed"
    conn = _db()
    try:
        assert estimates.get_estimate(conn, ids["est3"])["status"] == "approved"
    finally:
        conn.close()
    passed += 1; print("  expiry + resend + offline signature ok")

    # ---- list + delete a draft
    r = admin.get("/api/proposals?q=prop307"); assert r.status_code == 200
    j = r.get_json(); _no_secrets(j)
    assert j["meta"]["count"] >= 4 and j["meta"]["counts"]["signed"] >= 2
    assert admin.get(f"/api/proposals?estimate_id={ids['est2']}").get_json()["meta"]["count"] == 3
    draft = admin.get(f"/api/proposals?estimate_id={ids['est2']}&status=draft").get_json()["data"][0]
    assert admin.delete(f"/api/proposals/{draft['id']}").status_code == 200
    assert admin.get(f"/api/proposals/{draft['id']}").status_code == 404
    passed += 1; print("  list + delete ok")

    # ---- plain numbers (2026-10-21): unsent drafts made before them get one at boot; a sent proposal keeps its code
    conn = _db()
    try:
        sent = conn.execute("SELECT id FROM proposal WHERE estimate_id IN (?, ?, ?) AND status <> 'draft' ORDER BY id",
                            (ids["est"], ids["est2"], ids["est3"])).fetchone()["id"]
        e4 = estimates.create_estimate(conn, est_type="FR", borough="BX", client_org_id=ids["org"],
                                       building_address="44 Prop307 Terrace", created_by=ids["admin"])
        drafts = [proposals.create_from_estimate(conn, e4["id"], created_by=ids["admin"])["id"]]
        assert drafts, "a draft to number"
        conn.execute(f"UPDATE proposal SET display_no = NULL WHERE id IN ({','.join('?' * (len(drafts) + 1))})", drafts + [sent])
        conn.commit()
        proposals.ensure_schema(conn)
        proposals.ensure_schema(conn)   # idempotent
        got = {x["id"]: x["display_no"] for x in conn.execute("SELECT id, display_no FROM proposal").fetchall()}
        assert all(got[d] and got[d] >= 1042 for d in drafts) and got[sent] is None, (got, drafts, sent)
        assert len({got[d] for d in drafts}) == len(drafts), "each draft its own number"
        d0 = proposals.get(conn, drafts[0])
        assert proposals.public(d0)["label"] == str(got[drafts[0]])
        assert proposals.public(proposals.get(conn, sent))["label"].startswith("FR-") or "-P" in proposals.public(proposals.get(conn, sent))["label"]
    finally:
        conn.close()
    html = (ROOT / "company_portal.html").read_text(encoding="utf-8")
    for needle in ("pdf_view.js", "'Before the app'", "include: 'archive'", 'data-m="pdf">View the PDF', 'id="pr-pdf-pages"', "'Proposal No. ' + p.label"):
        assert needle in html, needle
    nav = (ROOT / "static/js/company_nav.js").read_text(encoding="utf-8")
    assert "Estimating queue" not in nav and "hrefFor: { estimator: '/estimating' }" in nav, "the sub-tab is gone; estimators keep their page"
    assert (ROOT / "static/vendor/pdfjs-4.0.379/pdf.min.js").exists() and (ROOT / "static/vendor/pdfjs-4.0.379/pdf.worker.min.js").exists()
    passed += 1; print("  numbers + PDF in the page ok")

    print(f"PASS smoke_proposals_307 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
