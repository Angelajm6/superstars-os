#!/usr/bin/env python3
"""#358 — the job dashboard's executive summary. Behavioral guard.

Boots the real app on an ISOLATED database. Proves:
  * the summary rides in /api/projects/<code>/bootstrap (no extra page-load request) and
    at /summary; it is refused for anyone but internal roles and scoped per project
  * money is BUILT only for admin / c_suite — the key is absent for pm / super, and their
    page carries no Money card markup
  * header: days left, % of time used, billing chip, the live people
  * schedule counts drops; field_today flips when today's DCR exists; risk counts open +
    overdue RFIs, markup RFIs and ACTIVE DOB violations; money buckets receivables by age
    and counts change orders
  * the page: every data-goto points at a real view; the legacy 30 s poll is gone
Run:  venv/bin/python tests/smoke_job_dashboard_358.py
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_jx_358_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "jx358"
PW = "JobDashboard2026"
P = "JX358-P"
T = date.today()
D = lambda n: (T + timedelta(days=n)).isoformat()   # noqa: E731


def _db():
    return db_layer.connect(pragma_fk=False)


def _purge(conn):
    for t, c in (("field_photos", "project_code"), ("report_index", "project_code"), ("rfi_log", "project_code"), ("rfi", "project_code"),
                 ("dob_violations", "project_code"), ("invoice", "project_code"), ("billing_sov", "project_code"),
                 ("drops", "project_code"), ("pm_project_assignment", "project_code"), ("projects", "project_code")):
        purge(conn, t, c, [P])
    oids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE ?", (f"{TAG}%",)).fetchall()]
    purge(conn, "crm_organization", "id", oids)


def _seed():
    conn = _db()
    try:
        _purge(conn)
        org = crm.create_org(conn, name=f"{TAG} Owner", relationship_type="client")
        conn.execute("INSERT INTO projects (project_code, name, address, status, client_org_id, start_date, target_completion, billing_type) "
                     "VALUES (?, 'Jx358', '1 Jx St', 'active', ?, ?, ?, 'aia_sov')", (P, org, D(-50), D(50)))
        for i in (1, 2, 3):
            conn.execute("INSERT INTO drops (drop_id, project_code, elevation, sequence_no) VALUES (?, ?, 'North', ?)", (f"{P}-D{i}", P, i))
        conn.execute("INSERT INTO rfi_log (rfi_number, project_code, status, due_date) VALUES (?, ?, 'open', ?)", (f"{TAG}-1", P, D(-3)))
        conn.execute("INSERT INTO rfi_log (rfi_number, project_code, status, due_date) VALUES (?, ?, 'open', ?)", (f"{TAG}-2", P, D(10)))
        conn.execute("INSERT INTO rfi_log (rfi_number, project_code, status, due_date) VALUES (?, ?, 'closed', ?)", (f"{TAG}-3", P, D(-9)))
        # the compliance tables come from schema_compliance.sql (not in every fixture)
        conn.execute("CREATE TABLE IF NOT EXISTS dob_violations (violation_id TEXT PRIMARY KEY, project_code TEXT, status TEXT)")
        conn.execute("INSERT INTO dob_violations (violation_id, project_code, status) VALUES (?, ?, 'ACTIVE')", (f"{TAG}-V1", P))
        conn.execute("INSERT INTO dob_violations (violation_id, project_code, status) VALUES (?, ?, 'RESOLVED')", (f"{TAG}-V2", P))
        now = D(0) + "T08:00:00"
        conn.execute("INSERT INTO invoice (number, kind, status, client_org_id, project_code, amount, amount_paid, issued_at, due_at, created_at, updated_at) "
                     "VALUES (?, 'progress', 'issued', ?, ?, 1000, 0, ?, ?, ?, ?)", (f"{TAG}-I1", org, P, D(-75), D(-45), now, now))
        conn.execute("INSERT INTO invoice (number, kind, status, client_org_id, project_code, amount, amount_paid, issued_at, due_at, created_at, updated_at) "
                     "VALUES (?, 'progress', 'paid', ?, ?, 2000, 2000, ?, ?, ?, ?)", (f"{TAG}-I2", org, P, D(-90), D(-60), now, now))
        conn.execute("INSERT INTO billing_sov (project_code, item_no, description, scheduled_value, kind, sort, created_at, updated_at) "
                     "VALUES (?, '1', 'Base', 100000, 'base', 1, ?, ?)", (P, now, now))
        conn.execute("INSERT INTO billing_sov (project_code, item_no, description, scheduled_value, kind, sort, created_at, updated_at) "
                     "VALUES (?, 'CO-1', 'Extra', 5000, 'change_order', 2, ?, ?)", (P, now, now))
        ids = {}
        for role in ("admin", "c_suite", "pm", "super"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"{TAG} {role} Person"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, ?)", (ids["pm"], P, D(0)))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, ?)", (ids["super"], P, D(0)))
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        _purge(conn)
        conn.commit()
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code)
    return c


def main() -> int:
    _seed()
    passed = 0
    cs, pm, sup = _login("c_suite"), _login("pm"), _login("super")

    # ---- the summary rides in bootstrap; money for the C-suite only
    b = cs.get(f"/api/projects/{P}/bootstrap?date={D(0)}").get_json()["data"]
    s = b["summary"]
    assert "money" in s, s.keys()
    for who in (pm, sup):
        x = who.get(f"/api/projects/{P}/bootstrap?date={D(0)}").get_json()["data"]["summary"]
        assert "money" not in x, "money is never built for pm / super"
        assert "money" not in who.get(f"/api/projects/{P}/summary").get_json()["data"]
    assert cs.get("/api/projects/NOPE358/summary").status_code == 404
    passed += 1; print("  bootstrap + roles ok")

    # ---- header / schedule / field / risk / money
    h = s["header"]
    assert h["days_left"] == 50 and 49 <= h["time_elapsed_pct"] <= 51 and h["billing_short"] == "AIA", h
    assert h["client"] == f"{TAG} Owner" and h["project_manager"] == f"{TAG} pm Person", h
    assert s["schedule"]["total"] == 3 and s["schedule"]["not_started"] == 3, s["schedule"]
    assert s["field_today"]["dcr_today"] is False
    r = s["risk"]
    assert r["rfis_open"] == 2 and r["rfis_overdue"] == 1 and r["violations_active"] == 1, r
    m = s["money"]
    assert m["contract"] == 105000 and m["change_orders"] == 5000, m
    assert m["invoiced"] == 3000 and m["collected"] == 2000 and m["open_ar"] == 1000, m
    assert m["aging"]["d31_60"] == 1000 and m["overdue"] == 1000 and m["balance_to_finish"] == 102000, m
    conn = _db()
    try:
        conn.execute("INSERT INTO report_index (report_date, project_code, report_type, report_id, status, dcr_sequence, no_work, stale) "
                     "VALUES (?, ?, 'DCR', ?, 'issued', 7, 0, 0)", (D(0), P, f"DCR-{P}-007-internal"))
        conn.commit()
    finally:
        conn.close()
    # the Latest photos card links must resolve (they pointed at a route that never existed)
    base = Path(server._FP_BASE) / P
    base.mkdir(parents=True, exist_ok=True)
    thumb = base / "jx358-thumb.jpg"
    thumb.write_bytes(bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9"))
    conn = _db()
    try:
        conn.execute("INSERT INTO field_photos (project_code, taken_at, taken_at_estimated, uploaded_at, file_path, thumb_path, mime) "
                     "VALUES (?, ?, 0, ?, ?, ?, 'image/jpeg')", (P, D(0) + "T09:00:00", D(0) + "T09:00:00", str(thumb), str(thumb)))
        conn.commit()
    finally:
        conn.close()
    ph = cs.get(f"/api/projects/{P}/summary").get_json()["data"]["photos"]
    assert ph and ph[0]["thumb_url"].startswith("/api/field-photos/"), ph
    assert cs.get(ph[0]["thumb_url"]).status_code == 200, "the photo card's thumbnail loads"
    f = cs.get(f"/api/projects/{P}/summary").get_json()["data"]["field_today"]
    assert f["dcr_today"] is True and f["last_dcr"]["number"] == f"DCR-{P}-007", f
    passed += 1; print("  blocks ok")

    # ---- the page
    page = cs.get(f"/projects/{P}").get_data(as_text=True)
    views = set(re.findall(r'<section class="view(?: active)?" data-view="([^"]+)"', page))
    i = page.index("function kpi(k, v, sub, go, cls)")
    jx = page[i:page.index("</script>", i)]          # the summary script only (Payments has its own kpi())
    gotos = set(re.findall(r'data-goto="([a-z-]+)"', page)) | set(re.findall(r"kpi\('[^']*',.*?, '([a-z-]+)'\)", jx))
    gotos.discard("ok"); gotos.discard("bad"); gotos.discard("warn")
    assert gotos and gotos <= views, ("data-goto without a view", gotos - views)
    assert 'id="jx-money"' in page and 'id="jx-kpis"' in page
    assert "setInterval(refresh, 30000)" not in page, "the legacy 30 s poll is gone"
    pm_page = pm.get(f"/projects/{P}").get_data(as_text=True)
    assert 'id="jx-money"' not in pm_page and 'id="jx-items"' in pm_page, "pm: no Finances card"
    passed += 1; print("  page ok")

    print(f"PASS smoke_job_dashboard_358 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_job_dashboard_358: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
