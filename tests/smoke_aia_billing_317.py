#!/usr/bin/env python3
"""#317 — AIA G702/G703 progress billing: behavioral guard.

Boots the real app (`import server`) on an ISOLATED database with the fake mail seam.
Proves:
  * schema idempotent; migration script runs
  * gates: pm / super / estimator / client -> 403 on billing, anon -> 401, unknown
    project -> 404
  * schedule of values: save (insert / update / delete), validation, import from the
    drop-plan lines, a billed line cannot be removed
  * applications: needs an SOV; one draft at a time; over-billing a line -> 400;
    G702 arithmetic (lines 1–9) on app #1; issue -> issued `progress` invoice for line 8
    with the period's retainage; a second application's column D and line 7 come from
    the first; delete only drafts; a voided invoice drops the application out of the
    history (app #3's previous = app #1 only)
  * documents: the invoice preview / public link for an application invoice IS the
    G702 + G703; the application preview marks a draft; no *_path / token_hash leaks
Run:  venv/bin/python tests/smoke_aia_billing_317.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_aia_317_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
MAIL_DIR = Path(_TMP) / "mail"
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ["SSC_MAIL_FAKE_DIR"] = str(MAIL_DIR)
os.environ["APP_BASE_URL"] = "http://test.local"
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import aia_billing  # noqa: E402
import auth  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "aia317"
PW = "AiaSmokePassword2026"
ROLES = ("c_suite", "admin", "pm", "super", "estimator", "client")
PROJ = "AIA317-A"
TODAY = date.today()


def D(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def _db():
    return db_layer.connect(pragma_fk=False)


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
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Aia317 %'")
        ids["org"] = crm.create_org(conn, name="Aia317 Owners", relationship_type="client")
        crm.create_contact(conn, full_name="Aia317 Dana Owner", org_id=ids["org"], email="aia317.dana@example.com")
        conn.execute("DELETE FROM projects WHERE project_code = ?", (PROJ,))
        conn.execute("INSERT INTO projects (project_code, name, address, status, client_org_id) VALUES (?, 'Aia317 Facade', "
                     "'9 Aia317 Plaza', 'active', ?)", (PROJ, ids["org"]))
        conn.execute("DELETE FROM sov_line_items WHERE project_code = ?", (PROJ,))
        conn.execute("INSERT INTO sov_line_items (project_code, sov_code, description, unit, unit_rate) VALUES (?, 'DP-1', "
                     "'Drop-plan brick', 'SF', 12.5)", (PROJ,))
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = _db()
    try:
        app_ids = [r["id"] for r in conn.execute("SELECT id FROM pay_application WHERE project_code = ?", (PROJ,)).fetchall()]
        purge(conn, "pay_application_line", "application_id", app_ids)
        purge(conn, "pay_application", "id", app_ids)
        purge(conn, "billing_sov", "project_code", [PROJ])
        conn.execute("DELETE FROM sov_line_items WHERE project_code = ?", (PROJ,))
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Aia317 %'").fetchall()]
        ph = ",".join("?" * len(org_ids)) or "NULL"
        inv_ids = [r["id"] for r in conn.execute(f"SELECT id FROM invoice WHERE client_org_id IN ({ph})", org_ids).fetchall()]
        purge(conn, "invoice_payment", "invoice_id", inv_ids)
        purge(conn, "invoice", "id", inv_ids)
        conn.execute("DELETE FROM projects WHERE project_code = ?", (PROJ,))
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


def _no_secrets(obj, where=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k != "token_hash" and not k.endswith("_path"), f"{k!r} leaked in {where}"
            _no_secrets(v, where)
    elif isinstance(obj, list):
        for v in obj:
            _no_secrets(v, where)


def _login(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code)
    return c


def _latest_mail() -> str:
    files = sorted(MAIL_DIR.glob("*"), key=lambda p: p.stat().st_mtime) if MAIL_DIR.exists() else []
    assert files, "no mail written"
    return files[-1].read_text(encoding="utf-8", errors="replace")


BILL = f"/api/projects/{PROJ}/billing"


def main() -> int:
    _seed()
    passed = 0

    # ---- schema + migration
    conn = _db()
    try:
        aia_billing.ensure_schema(conn); aia_billing.ensure_schema(conn)
    finally:
        conn.close()
    import apply_aia_317 as mig
    assert mig.main() == 0
    passed += 1; print("  schema ok")

    # ---- gates
    cs = _login("c_suite")
    assert server.app.test_client().get(BILL).status_code == 401
    for role in ("pm", "super", "estimator", "client"):
        assert _login(role).get(BILL).status_code == 403, role
    assert cs.get("/api/projects/NOPE-317/billing").status_code == 404
    passed += 1; print("  gates ok")

    # ---- schedule of values
    r = cs.post(f"{BILL}/applications", json={"period_to": D(0)})
    assert r.status_code == 400 and "schedule of values" in r.get_json()["error"]
    r = cs.put(f"{BILL}/sov", json={"rows": [{"item_no": "1", "description": "", "scheduled_value": 10}]})
    assert r.status_code == 400
    r = cs.put(f"{BILL}/sov", json={"rows": [
        {"item_no": "1", "description": "Mobilization", "scheduled_value": "10,000"},
        {"item_no": "2", "description": "Brick restoration", "scheduled_value": 60000},
        {"item_no": "3", "description": "Lintel replacement", "scheduled_value": 30000},
        {"item_no": "CO-1", "description": "Extra parapet", "scheduled_value": 5000, "kind": "change_order", "approved_on": D(-3)},
    ]})
    assert r.status_code == 200, r.get_json()
    sov = r.get_json()["data"]
    assert [s["item_no"] for s in sov] == ["1", "2", "3", "CO-1"] and sov[0]["scheduled_value"] == 10000.0
    r = cs.post(f"{BILL}/sov/import", json={})
    assert r.status_code == 200 and r.get_json()["meta"]["imported"] == 1
    sov = r.get_json()["data"]
    assert any(s["item_no"] == "DP-1" and s["scheduled_value"] == 0 for s in sov)
    # drop the imported row again (unbilled -> allowed), update line 3 by id
    keep = [dict(s) for s in sov if s["item_no"] != "DP-1"]
    keep[2]["scheduled_value"] = 30000
    r = cs.put(f"{BILL}/sov", json={"rows": keep})
    assert r.status_code == 200 and len(r.get_json()["data"]) == 4
    b = cs.get(BILL).get_json()["data"]
    _no_secrets(b, "billing")
    assert b["summary"]["original_contract_sum"] == 100000.0 and b["summary"]["net_change_orders"] == 5000.0
    assert b["summary"]["contract_sum_to_date"] == 105000.0 and b["next_number"] == 1 and b["client"]["name"] == "Aia317 Owners"
    passed += 1; print("  schedule of values ok")

    # ---- application #1
    r = cs.post(f"{BILL}/applications", json={"period_from": D(-30), "period_to": D(0), "retainage_pct": 10})
    assert r.status_code == 201, r.get_json()
    a1 = r.get_json()["data"]
    assert a1["number"] == 1 and a1["status"] == "draft" and len(a1["lines"]) == 4
    assert cs.post(f"{BILL}/applications", json={"period_to": D(0)}).status_code == 409, "one draft at a time"
    sid = {ln["item_no"]: ln["sov_id"] for ln in a1["lines"]}
    r = cs.put(f"/api/billing/applications/{a1['id']}", json={"lines": [{"sov_id": sid["1"], "this_period": 10001}]})
    assert r.status_code == 400 and "exceeds" in r.get_json()["error"]
    r = cs.put(f"/api/billing/applications/{a1['id']}", json={"lines": [
        {"sov_id": sid["1"], "this_period": 10000}, {"sov_id": sid["2"], "this_period": 20000, "stored": 5000},
        {"sov_id": sid["CO-1"], "this_period": 1000}]})
    assert r.status_code == 200, r.get_json()
    a1 = r.get_json()["data"]; g = a1["g702"]
    # 1=100000 2=5000 3=105000 4=10000+25000+1000=36000 5=10%*(31000)+10%*5000=3600 6=32400 7=0 8=32400 9=72600
    assert g["original_contract_sum"] == 100000.0 and g["net_change_orders"] == 5000.0 and g["contract_sum_to_date"] == 105000.0
    assert g["total_completed_stored"] == 36000.0 and g["retainage_work"] == 3100.0 and g["retainage_stored"] == 500.0
    assert g["total_retainage"] == 3600.0 and g["total_earned_less_retainage"] == 32400.0 and g["previous_certificates"] == 0.0
    assert g["current_payment_due"] == 32400.0 and g["balance_to_finish"] == 72600.0
    l2 = next(ln for ln in a1["lines"] if ln["item_no"] == "2")
    assert l2["total_completed"] == 25000.0 and l2["pct_complete"] == 41.7 and l2["balance_to_finish"] == 35000.0 and l2["retainage"] == 2500.0
    # a billed line cannot be dropped from the SOV
    r = cs.put(f"{BILL}/sov", json={"rows": [s for s in keep if s["item_no"] != "1"]})
    assert r.status_code == 400 and "cannot be removed" in r.get_json()["error"]
    html = cs.get(f"/api/billing/applications/{a1['id']}/preview").get_data(as_text=True)
    assert "G702" in html and "Continuation Sheet" in html and "Draft" in html and "$32,400.00" in html
    passed += 1; print("  application #1 arithmetic ok")

    # ---- issue #1 -> progress invoice
    r = cs.post(f"/api/billing/applications/{a1['id']}/issue", json={})
    assert r.status_code == 201, r.get_json()
    a1 = r.get_json()["data"]
    assert a1["status"] == "issued" and a1["invoice_id"] and a1["invoice_number"].startswith("INV-")
    inv = cs.get(f"/api/invoices/{a1['invoice_id']}").get_json()["data"]
    assert inv["kind"] == "progress" and inv["status"] == "issued" and inv["amount"] == 32400.0 and inv["retainage"] == 3600.0
    assert "Application for Payment #1" in inv["description"]
    assert cs.post(f"/api/billing/applications/{a1['id']}/issue", json={}).status_code == 409
    assert cs.put(f"/api/billing/applications/{a1['id']}", json={"notes": "x"}).status_code == 409
    assert cs.delete(f"/api/billing/applications/{a1['id']}").status_code == 409
    html = cs.get(f"/api/invoices/{a1['invoice_id']}/preview").get_data(as_text=True)
    assert "G702" in html and "G703" in html and "Draft" not in html, "the invoice document IS the application"
    r = cs.post(f"/api/invoices/{a1['invoice_id']}/send", json={"to_email": "aia317.dana@example.com", "to_name": "Dana"})
    assert r.status_code == 201, r.get_json()
    mail = _latest_mail()
    import re
    m = re.search(r"http://test\.local/i/([A-Za-z0-9_\-]+)", mail)
    assert m, "private link in the mail"
    pub = server.app.test_client().get(f"/i/{m.group(1)}")
    assert pub.status_code == 200 and "G703" in pub.get_data(as_text=True)
    b = cs.get(BILL).get_json()["data"]
    assert b["summary"]["certified_to_date"] == 32400.0 and b["summary"]["retainage_held"] == 3600.0 and b["summary"]["open_draft"] is None
    passed += 1; print("  issue -> invoice + document ok")

    # ---- application #2 carries #1 forward
    r = cs.post(f"{BILL}/applications", json={"period_from": D(1), "period_to": D(30)})
    assert r.status_code == 201
    a2 = r.get_json()["data"]
    assert a2["number"] == 2
    l1 = next(ln for ln in a2["lines"] if ln["item_no"] == "1")
    assert l1["previous"] == 10000.0 and l1["balance_to_finish"] == 0.0
    r = cs.put(f"/api/billing/applications/{a2['id']}", json={"lines": [{"sov_id": sid["1"], "this_period": 1}]})
    assert r.status_code == 400, "line 1 is fully billed"
    r = cs.put(f"/api/billing/applications/{a2['id']}", json={"lines": [{"sov_id": sid["2"], "this_period": 30000}]})
    a2 = r.get_json()["data"]; g = a2["g702"]
    # D+E = 10000+20000+30000+1000 = 61000 work; stored now 0 (this app's F) -> 4 = 61000; 5 = 6100; 6 = 54900; 7 = 32400; 8 = 22500
    assert g["total_completed_stored"] == 61000.0 and g["total_retainage"] == 6100.0 and g["previous_certificates"] == 32400.0
    assert g["current_payment_due"] == 22500.0 and g["balance_to_finish"] == 50100.0
    r = cs.post(f"/api/billing/applications/{a2['id']}/issue", json={"due_at": D(45)})
    assert r.status_code == 201
    a2 = r.get_json()["data"]
    inv2 = cs.get(f"/api/invoices/{a2['invoice_id']}").get_json()["data"]
    assert inv2["amount"] == 22500.0 and inv2["retainage"] == 2500.0 and inv2["due_at"] == D(45)
    passed += 1; print("  application #2 carry-forward ok")

    # ---- void #2's invoice -> #3 starts from #1 again
    r = cs.post(f"/api/invoices/{a2['invoice_id']}/void", json={"reason": "owner disputed"})
    assert r.status_code == 200, r.get_json()
    assert cs.get(f"/api/billing/applications/{a2['id']}").get_json()["data"]["status"] == "void"
    r = cs.post(f"{BILL}/applications", json={"period_to": D(60)})
    assert r.status_code == 201
    a3 = r.get_json()["data"]
    assert a3["number"] == 3 and a3["g702"]["previous_certificates"] == 32400.0
    l2 = next(ln for ln in a3["lines"] if ln["item_no"] == "2")
    assert l2["previous"] == 20000.0, "the voided application's work is not 'previous'"
    r = cs.delete(f"/api/billing/applications/{a3['id']}")
    assert r.status_code == 200
    assert cs.get(f"/api/billing/applications/{a3['id']}").status_code == 404
    b = cs.get(BILL).get_json()["data"]
    assert b["summary"]["applications"] == 2 and b["next_number"] == 3
    _no_secrets(b, "billing-final")
    passed += 1; print("  void + delete ok")

    # ---- the served page: the Payments loader must resolve the job from the real URL
    # (#326 read only ?project=, which /projects/<code> never carries -> "File not found")
    page = cs.get(f"/projects/{PROJ}").get_data(as_text=True)
    assert "window.SSC_PROJECT_CODE || decodeURIComponent((location.pathname.match(/\\/projects\\/([^\\/]+)/)" in page
    assert 'id="aia-card"' in page and 'id="aia-sov-table"' in page and "AIA_pay_load" in page
    assert 'window.SSC_PROJECT_CODE = "' + PROJ + '"' in page
    passed += 1; print("  served page ok")

    print(f"PASS smoke_aia_billing_317 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
