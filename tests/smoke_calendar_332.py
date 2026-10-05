#!/usr/bin/env python3
"""#332 — the jobs calendar: behavioral guard.

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * gates: anon -> 401; pm / client -> 403; bad dates / a window over 120 days -> 400
  * the window: a job inside it, an open-ended active job starting inside it, a job that
    ended before it (excluded), a closed job without an end date (ends when it was
    closed), an active job with no start date (listed as `undated`, never drawn);
    per-job duration, stable colour, `today_on`; daily reports per day
  * dates travel: onboarding stores `target_completion` (refused before the start);
    Crew & site returns and updates both dates (refuses end < start); the calendar
    shows the job the moment it is onboarded
  * served pages: the portal has the Calendar nav entry and view; "+ New project" and
    Crew & site carry the target-completion field
Run:  venv/bin/python tests/smoke_calendar_332.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_cal_332_")

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
import portal_calendar  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "cal332"
PW = "CalendarSmoke2026"
ROLES = ("c_suite", "admin", "pm", "client")
CODES = ("CAL332-A", "CAL332-B", "CAL332-C", "CAL332-D")
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
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Smoke {role} Cal332"))
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Cal332 %'")
        ids["org"] = crm.create_org(conn, name="Cal332 Owners", relationship_type="client")
        for c in CODES:
            conn.execute("DELETE FROM projects WHERE project_code = ?", (c,))
        conn.execute("DELETE FROM report_index WHERE project_code LIKE 'CAL332-%'")
        # A: inside the window, 11 days · B: active, open-ended, starts inside · C: closed long ago · D: active, undated
        conn.execute("INSERT INTO projects (project_code, name, address, status, client_org_id, start_date, target_completion, updated_at) "
                     "VALUES ('CAL332-A', 'Cal332 Facade', '1 Cal332 St', 'active', ?, ?, ?, ?)", (ids["org"], D(-5), D(5), D(0)))
        conn.execute("INSERT INTO projects (project_code, name, address, status, client_org_id, start_date, updated_at) "
                     "VALUES ('CAL332-B', 'Cal332 Roof', '2 Cal332 St', 'active', ?, ?, ?)", (ids["org"], D(2), D(0)))
        conn.execute("INSERT INTO projects (project_code, name, address, status, client_org_id, start_date, updated_at) "
                     "VALUES ('CAL332-C', 'Cal332 Old', '3 Cal332 St', 'closed', ?, ?, ?)", (ids["org"], D(-60), D(-40) + "T10:00:00"))
        conn.execute("INSERT INTO projects (project_code, name, address, status, client_org_id, updated_at) "
                     "VALUES ('CAL332-D', 'Cal332 Undated', '4 Cal332 St', 'active', ?, ?)", (ids["org"], D(0)))
        for n in (-1, 0):
            conn.execute("INSERT INTO report_index (report_date, project_code, report_type, status) VALUES (?, 'CAL332-A', 'DCR', 'final')", (D(n),))
        conn.commit()
    finally:
        conn.close()
    return ids


def _cleanup():
    conn = _db()
    try:
        codes = [r["project_code"] for r in conn.execute("SELECT project_code FROM projects WHERE project_code LIKE 'CAL332-%' OR name LIKE 'Cal332 %'").fetchall()]
        for t in ("pm_project_assignment", "project_assignments", "report_index", "billing_sov"):
            try:
                purge(conn, t, "project_code", codes)
            except Exception:
                conn.rollback()
        try:
            purge(conn, "ira_job", "project_code", codes)
        except Exception:
            conn.rollback()
        conn.execute("DELETE FROM projects WHERE project_code LIKE 'CAL332-%' OR name LIKE 'Cal332 %'")
        conn.execute("DELETE FROM estimate WHERE building_address LIKE '% Cal332 %' OR notes LIKE '%Cal332%'")
        conn.execute("DELETE FROM building WHERE address_line LIKE '% Cal332 %'")
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Cal332 %'").fetchall()]
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
    assert server.app.test_client().get(API).status_code == 401
    assert _login("pm").get(API).status_code == 403 and _login("client").get(API).status_code == 403
    assert cs.get(API + "?from=2026-13-01").status_code == 400
    assert cs.get(API + f"?from={D(0)}&to={D(200)}").status_code == 400
    assert cs.get(API + f"?from={D(5)}&to={D(0)}").status_code == 400
    passed += 1; print("  gates ok")

    d = cs.get(API + f"?from={D(-7)}&to={D(7)}").get_json()["data"]
    by = {j["project_code"]: j for j in d["jobs"] if j["project_code"].startswith("CAL332")}
    assert set(by) == {"CAL332-A", "CAL332-B"}, set(by)
    a, b = by["CAL332-A"], by["CAL332-B"]
    assert a["start_date"] == D(-5) and a["end_date"] == D(5) and a["days"] == 11 and not a["open_ended"] and a["today_on"]
    assert a["client_name"] == "Cal332 Owners" and a["color"] == portal_calendar.color_index("CAL332-A") and 0 <= a["color"] < 8
    assert b["open_ended"] and b["end_date"] is None and b["days"] is None and not b["today_on"]
    assert [u["project_code"] for u in d["undated"] if u["project_code"].startswith("CAL332")] == ["CAL332-D"]
    assert d["days"][D(0)]["reports"] == ["CAL332-A"] and d["days"][D(-1)]["reports"] == ["CAL332-A"]
    assert d["range"] == {"from": D(-7), "to": D(7), "today": D(0)}
    # the closed job shows in a window that covers it, ending the day it was closed
    d2 = cs.get(API + f"?from={D(-70)}&to={D(-30)}").get_json()["data"]
    c = next(j for j in d2["jobs"] if j["project_code"] == "CAL332-C")
    assert c["start_date"] == D(-60) and c["end_date"] == D(-40) and c["status"] == "closed" and not c["open_ended"]
    # the open-ended job overlaps any later window; a job that ended does not
    d3 = cs.get(API + f"?from={D(60)}&to={D(90)}").get_json()["data"]
    codes3 = {j["project_code"] for j in d3["jobs"]}
    assert "CAL332-B" in codes3 and "CAL332-A" not in codes3 and "CAL332-C" not in codes3
    passed += 1; print("  window ok")

    # ---- dates travel: onboarding + crew & site
    admin = _login("admin")
    base = {"client_org_id": ids["org"], "est_type": "FR", "borough": "BX", "address": "9 Cal332 Way", "name": "Cal332 Onboarded"}
    r = admin.post("/api/admin/projects/onboard", json={**base, "start_date": D(10), "target_completion": D(3)})
    assert r.status_code == 400 and "before the start" in r.get_json()["error"]
    r = admin.post("/api/admin/projects/onboard", json={**base, "start_date": D(10), "target_completion": D(40)})
    assert r.status_code in (200, 201), r.get_json()
    code = r.get_json()["data"]["project_code"]
    assert r.get_json()["data"]["target_completion"] == D(40)
    d4 = cs.get(API + f"?from={D(0)}&to={D(45)}").get_json()["data"]
    j = next(x for x in d4["jobs"] if x["project_code"] == code)
    assert j["start_date"] == D(10) and j["end_date"] == D(40) and j["days"] == 31, "onboarded job is on the calendar at once"
    crew = admin.get(f"/api/admin/projects/{code}/crew").get_json()["data"]
    assert crew["dates"] == {"start_date": D(10), "target_completion": D(40)}
    r = admin.put(f"/api/admin/projects/{code}/crew", json={"target_completion": D(5)})
    assert r.status_code == 400
    r = admin.put(f"/api/admin/projects/{code}/crew", json={"start_date": D(12), "target_completion": D(50)})
    assert r.status_code == 200 and r.get_json()["data"]["dates"] == {"start_date": D(12), "target_completion": D(50)}
    d5 = cs.get(API + f"?from={D(0)}&to={D(60)}").get_json()["data"]
    j = next(x for x in d5["jobs"] if x["project_code"] == code)
    assert j["start_date"] == D(12) and j["end_date"] == D(50)
    passed += 1; print("  dates travel ok")

    html = cs.get("/company").get_data(as_text=True)
    assert 'data-subtabs="projects"' in html and 'id="view-calendar"' in html and "loadCalendar" in html   # #339: Calendar is a Projects sub-tab
    assert "'Calendar', '/company#calendar'" in (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")
    assert 'data-cal-view="3day"' in html and 'data-cal-view="week"' in html and 'data-cal-view="month"' in html
    ap = admin.get("/admin/projects").get_data(as_text=True)
    mod = admin.get("/files/static/js/new_project_modal.js").get_data(as_text=True)   # the shared "+ New project" form
    assert 'id="np-end"' in mod and "target_completion" in mod and 'id="cs-start"' in ap and 'id="cs-end"' in ap and "target_completion" in ap
    passed += 1; print("  served pages ok")

    print(f"PASS smoke_calendar_332 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)
