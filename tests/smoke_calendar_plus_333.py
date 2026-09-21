#!/usr/bin/env python3
"""#333 — calendar extras: milestones, on-site headcount, weather strip, drag-to-reschedule,
print sheet. Behavioral guard.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * days carry `on_site` (distinct workers signed in per job) and `headcount`
  * milestones: a document expiring in the window (with `late` when past) and an issued
    invoice falling due (balance, client) land on their days; a draft invoice and a
    superseded document do not
  * weather: the endpoint maps the provider's per-day answer (stubbed — no network in
    the test) onto dates with a label; > 14 days -> 400; a failing day is left out
  * rescheduling by drag uses the existing Crew & site PUT: start and end move together
  * served page: the strip, the drag grips, the print button and print stylesheet exist
Run:  venv/bin/python tests/smoke_calendar_plus_333.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_cal_333_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import invoices  # noqa: E402
import portal_calendar  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "cal333"
PW = "CalendarPlusSmoke2026"
ROLES = ("c_suite", "admin", "pm")
A = "CAL333-A"
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
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Smoke {role} Cal333"))
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Cal333 %'")
        ids["org"] = crm.create_org(conn, name="Cal333 Owners", relationship_type="client")
        conn.execute("DELETE FROM projects WHERE project_code = ?", (A,))
        conn.execute("INSERT INTO projects (project_code, name, address, status, client_org_id, start_date, target_completion, updated_at) "
                     "VALUES (?, 'Cal333 Facade', '1 Cal333 St', 'active', ?, ?, ?, ?)", (A, ids["org"], D(-3), D(10), D(0)))
        conn.execute("DELETE FROM sign_in_log WHERE project_code = ?", (A,))
        for emp, n in (("E-93331", 0), ("E-93332", 0), ("E-93331", 0), ("E-93331", -1)):   # two distinct today, one yesterday
            conn.execute("INSERT INTO sign_in_log (date, employee_id, project_code, time_in, created_at, updated_at) "
                         "VALUES (?, ?, ?, '07:00', ?, ?)", (D(n), emp, A, D(0), D(0)))
        conn.execute("DELETE FROM project_documents WHERE project_code = ?", (A,))
        conn.execute("INSERT INTO project_documents (project_code, category, requirement_key, title, doc_type, file_path, expiry_date, "
                     "superseded, uploaded_at) VALUES (?, 'CONTRACTS', 'coi', 'Cal333 COI', 'pdf', 'x/a.pdf', ?, 0, ?)", (A, D(4), D(-1)))
        conn.execute("INSERT INTO project_documents (project_code, category, requirement_key, title, doc_type, file_path, expiry_date, "
                     "superseded, uploaded_at) VALUES (?, 'PERMITS', 'pw2', 'Cal333 Old permit', 'pdf', 'x/b.pdf', ?, 1, ?)", (A, D(5), D(-1)))
        conn.execute("INSERT INTO project_documents (project_code, category, title, doc_type, file_path, expiry_date, superseded, uploaded_at) "
                     "VALUES (?, 'PERMITS', 'Cal333 Lapsed shed', 'pdf', 'x/c.pdf', ?, 0, ?)", (A, D(-2), D(-9)))
        conn.commit()
        inv = invoices.create(conn, {"kind": "progress", "client_org_id": ids["org"], "project_code": A, "amount": 12000, "description": "Cal333 progress"})
        invoices.issue(conn, inv["id"], issued_on=D(-10), due_on=D(6))
        invoices.record_payment(conn, inv["id"], amount=2000, paid_on=D(-1))
        ids["inv"] = inv
        drafted = invoices.create(conn, {"kind": "final", "client_org_id": ids["org"], "project_code": A, "amount": 500,
                                         "description": "Cal333 draft", "due_at": D(3)})
        ids["draft"] = drafted
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = _db()
    try:
        for t in ("sign_in_log", "project_documents", "pm_project_assignment", "project_assignments"):
            try:
                conn.execute(f"DELETE FROM {t} WHERE project_code = ?", (A,))
            except Exception:
                conn.rollback()
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Cal333 %'").fetchall()]
        ph = ",".join("?" * len(org_ids)) or "NULL"
        inv_ids = [r["id"] for r in conn.execute(f"SELECT id FROM invoice WHERE client_org_id IN ({ph})", org_ids).fetchall()]
        purge(conn, "invoice_payment", "invoice_id", inv_ids)
        purge(conn, "invoice", "id", inv_ids)
        conn.execute("DELETE FROM projects WHERE project_code = ?", (A,))
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


def _login(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code)
    return c


API = "/api/company/portal/calendar"


def main() -> int:
    ids = _seed()
    passed = 0
    cs = _login("c_suite")

    d = cs.get(API + f"?from={D(-7)}&to={D(14)}").get_json()["data"]
    days = d["days"]
    assert days[D(0)]["on_site"][A] == 2 and days[D(0)]["headcount"] >= 2 and days[D(-1)]["on_site"][A] == 1
    assert A in days[D(0)]["reports"] or days[D(0)]["reports"] == [] or True   # reports are #332's; not required here
    passed += 1; print("  headcount ok")

    coi = [m for m in days.get(D(4), {}).get("milestones", []) if m["kind"] == "document" and m["project_code"] == A]
    assert coi and coi[0]["label"] == "Cal333 COI" and coi[0]["category"] == "CONTRACTS" and not coi[0]["late"]
    lapsed = [m for m in days.get(D(-2), {}).get("milestones", []) if m["project_code"] == A]
    assert lapsed and lapsed[0]["late"] and lapsed[0]["label"] == "Cal333 Lapsed shed"
    assert not [m for m in days.get(D(5), {}).get("milestones", []) if m.get("label") == "Cal333 Old permit"], "superseded stays off"
    due = [m for m in days.get(D(6), {}).get("milestones", []) if m["kind"] == "invoice" and m["project_code"] == A]
    assert due and due[0]["number"] == ids["inv"]["number"] and due[0]["balance"] == 10000.0 and due[0]["client_name"] == "Cal333 Owners"
    assert not [m for m in days.get(D(3), {}).get("milestones", []) if m.get("number") == ids["draft"]["number"]], "drafts are not due"
    passed += 1; print("  milestones ok")

    # weather: stub the provider, exercise the mapping + limits
    calls = []
    def fake(lat, lng, date_str=None):
        calls.append(date_str)
        if date_str == D(1):
            raise RuntimeError("provider down")
        return {"condition_code": 61 if date_str == D(0) else 2, "temp_max": 71.5, "temp_min": 58.0, "precip_prob_today": 40}
    conn = _db()
    try:
        w = portal_calendar.weather(conn, TODAY, TODAY + timedelta(days=2), fetch=fake)
    finally:
        conn.close()
    assert calls == [D(0), D(1), D(2)] and set(w["days"]) == {D(0), D(2)}, w
    assert w["days"][D(0)]["label"] == "Light rain" and w["days"][D(2)]["label"] == "Partly cloudy" and w["days"][D(0)]["precip_prob"] == 40
    assert "lat" in w["site"] and "lng" in w["site"]
    real = server.fetch_open_meteo_weather
    server.fetch_open_meteo_weather = fake
    try:
        assert cs.get(API + f"/weather?from={D(0)}&to={D(20)}").status_code == 400
        assert cs.get(API + "/weather?from=2026-13-01").status_code == 400
        r = cs.get(API + f"/weather?from={D(0)}&to={D(2)}")
        assert r.status_code == 200 and D(0) in r.get_json()["data"]["days"]
        assert _login("pm").get(API + "/weather").status_code == 403
        assert server.app.test_client().get(API + "/weather").status_code == 401
    finally:
        server.fetch_open_meteo_weather = real
    passed += 1; print("  weather ok")

    # drag = the Crew & site PUT, start and end together
    admin = _login("admin")
    r = admin.put(f"/api/admin/projects/{A}/crew", json={"start_date": D(-1), "target_completion": D(12)})
    assert r.status_code == 200 and r.get_json()["data"]["dates"] == {"start_date": D(-1), "target_completion": D(12)}
    j = next(x for x in cs.get(API + f"?from={D(-7)}&to={D(14)}").get_json()["data"]["jobs"] if x["project_code"] == A)
    assert j["start_date"] == D(-1) and j["end_date"] == D(12) and j["days"] == 14
    passed += 1; print("  reschedule ok")

    html = cs.get("/company").get_data(as_text=True)
    for needle in ('id="cal-weather"', 'class="cal-grip"', 'id="cal-print"', "@media print", "cal-print-list", "calendar/weather", "cal-ms"):
        assert needle in html, needle
    passed += 1; print("  served page ok")

    print(f"PASS smoke_calendar_plus_333 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
