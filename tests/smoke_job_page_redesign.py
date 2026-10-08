#!/usr/bin/env python3
"""Job page redesign, batches A + B (Oct 7 C-suite notes). Guard.

Boots the real app on an ISOLATED database. Proves:
  * open items = the job's own open tasks and events (team_items with this job): overdue
    first, finished events dropped, other jobs' items never shown; counts for the KPI;
    admin / C-suite may add, the assignee (or admin / C-suite) may tick done; a task added
    with the job lands in /api/team/items?project= and on the team calendar
  * field today carries workers on site of assigned and no safety events
  * key contacts come in four groups: Our team, Owner (with the property manager), Engineer /
    Architect, General contractor; the main contact first
  * the page: three top numbers (on site · % complete · open items), no Schedule health card,
    Finances (not Money) for the C-suite only, an Open items card with an add form
  * the sidebar has three groups (Overview / On Site / Finances); pm / super never get
    Finances; every tab is still there
  * Employees is the crew list with licenses (role on the job from the assignment)
  * the drawings open in 3D (massing, so the photoreal daily cap is not spent) with no
    Author button
Run:  venv/bin/python tests/smoke_job_page_redesign.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_jobpage_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)
import project_contacts  # noqa: E402

TAG = "jobpg"
PW = "JobPageRedesign2026"
P, P2 = "JOBPG-A", "JOBPG-B"
T = date.today()
D = lambda n: (T + timedelta(days=n)).isoformat()   # noqa: E731
EMP = (("E-93581", "W-9358", "Jobpg Mason"), ("E-93582", "W-9359", "Jobpg Rigger"))


def _db():
    return db_layer.connect(pragma_fk=False)


def _purge(conn):
    for sql, args in (("DELETE FROM crm_task WHERE project_code IN (?, ?) OR title LIKE ?", (P, P2, f"{TAG}%")),
                      ("DELETE FROM project_contact WHERE project_code IN (?, ?)", (P, P2)),
                      ("DELETE FROM certifications WHERE employee_id IN (?, ?)", tuple(e[0] for e in EMP)),
                      ("DELETE FROM project_assignments WHERE project_code IN (?, ?)", (P, P2)),
                      ("DELETE FROM employees WHERE employee_id IN (?, ?)", tuple(e[0] for e in EMP)),
                      ("DELETE FROM crm_contact WHERE full_name LIKE ?", (f"{TAG}%",)),
                      ("DELETE FROM crm_organization WHERE name LIKE ?", (f"{TAG}%",))):
        try:
            conn.execute(sql, args)
            conn.commit()
        except Exception:
            conn.rollback()
    for t in ("drops", "pm_project_assignment", "projects"):
        purge(conn, t, "project_code", [P, P2])
    conn.commit()


def _seed() -> dict:
    conn = _db()
    try:
        _purge(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        purge_users(conn, uids)
        for code in (P, P2):
            conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, ?, '1 Job St', 'active')", (code, f"Jobpg {code}"))
        for i in (1, 2):
            conn.execute("INSERT INTO drops (drop_id, project_code, elevation, sequence_no) VALUES (?, ?, 'North', ?)", (f"{P}-D{i}", P, i))
        for eid, wid, name in EMP:
            conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at) VALUES (?, ?, ?, 'mason', ?, ?)",
                         (eid, wid, name, D(0), D(0)))
        conn.execute("INSERT INTO project_assignments (project_code, employee_id, role_on_project, start_date, status) VALUES (?, ?, 'Foreman', ?, 'active')",
                     (P, EMP[0][0], D(-5)))
        conn.execute("INSERT INTO project_assignments (project_code, employee_id, start_date, status) VALUES (?, ?, ?, 'active')", (P, EMP[1][0], D(-5)))
        ids = {}
        for role in ("admin", "c_suite", "pm", "super"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 0)", (email, auth.hash_password(PW), role, f"{TAG} {role} Person"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, ?)", (ids["pm"], P, D(0)))
        conn.commit()
        # contacts: an owner + their property manager, two consultants, a GC site person
        add = lambda p: project_contacts.add(conn, P, p, actor=ids["admin"])   # noqa: E731
        add({"role": "property_manager", "new_contact": {"full_name": f"{TAG} Pm Riley"}, "new_org": f"{TAG} Mgmt"})
        add({"role": "owner", "new_contact": {"full_name": f"{TAG} Owner Dana"}, "new_org": f"{TAG} Holdings"})
        add({"role": "consultant", "discipline": "structural_engineer", "new_contact": {"full_name": f"{TAG} Eng Lee"}, "new_org": f"{TAG} Eng"})
        add({"role": "consultant", "discipline": "architect", "new_contact": {"full_name": f"{TAG} Arch Morgan"}, "new_org": f"{TAG} Arch"})
        add({"role": "site_supervisor", "new_contact": {"full_name": f"{TAG} Gc Chris"}, "new_org": f"{TAG} Builders"})
        conn.commit()
        return ids
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        _purge(conn)
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


def _summary(c, code=P):
    r = c.get(f"/api/projects/{code}/summary")
    assert r.status_code == 200, r.status_code
    return r.get_json()["data"]


def main() -> int:
    ids = _seed()
    passed = 0
    adm, cs, pm, sup = (_login(r) for r in ("admin", "c_suite", "pm", "super"))

    # ---- open items: the job's tasks and events
    mk = lambda body: cs.post("/api/team/items", json=body)   # noqa: E731
    assert mk({"kind": "task", "title": f"{TAG} overdue call", "project_code": P, "due_date": D(-2), "assignee_user_id": ids["pm"]}).status_code == 201
    assert mk({"kind": "task", "title": f"{TAG} later task", "project_code": P, "due_date": D(5)}).status_code == 201
    assert mk({"kind": "task", "title": f"{TAG} undated task", "project_code": P}).status_code == 201
    assert mk({"kind": "event", "title": f"{TAG} job walk", "project_code": P, "start_date": D(2)}).status_code == 201
    assert mk({"kind": "event", "title": f"{TAG} past walk", "project_code": P, "start_date": D(-3)}).status_code == 201
    assert mk({"kind": "task", "title": f"{TAG} other job", "project_code": P2, "due_date": D(1)}).status_code == 201
    assert pm.post("/api/team/items", json={"kind": "task", "title": f"{TAG} pm try", "project_code": P}).status_code == 403
    o = _summary(cs)["open_items"]
    titles = [x["title"] for x in o["items"]]
    assert titles == [f"{TAG} overdue call", f"{TAG} job walk", f"{TAG} later task", f"{TAG} undated task"], titles
    assert o["count"] == 4 and o["overdue"] == 1 and o["events_this_week"] == 1 and o["can_add"] is True, o
    po = _summary(pm)["open_items"]
    assert po["can_add"] is False and po["count"] == 4
    mine = next(x for x in po["items"] if x["title"] == f"{TAG} overdue call")
    other = next(x for x in po["items"] if x["title"] == f"{TAG} later task")
    assert mine["can_complete"] is True and other["can_complete"] is False
    assert pm.post(f"/api/team/items/{other['id']}/done", json={"done": True}).status_code == 403
    assert pm.post(f"/api/team/items/{mine['id']}/done", json={"done": True}).status_code == 200
    o = _summary(cs)["open_items"]
    assert o["count"] == 3 and o["overdue"] == 0, o
    lst = cs.get(f"/api/team/items?project={P}").get_json()["data"]
    assert {x["title"] for x in lst} >= {f"{TAG} later task", f"{TAG} job walk"} and all(x["project_code"] == P for x in lst)
    cal = cs.get(f"/api/team/calendar?from={D(0)}&to={D(7)}").get_json()["data"]
    assert any(x.get("title") == f"{TAG} job walk" for x in cal.get("items", [])), "the job's event is on the team calendar"
    passed += 1; print("  open items ok")

    # ---- field today + contacts
    s = _summary(cs)
    f = s["field_today"]
    assert "safety_7d" not in f and f["assigned"] == 2 and f["on_site"] == 0, f
    groups = s["contacts"]
    assert [g["label"] for g in groups] == ["Our team", "Owner", "Engineer / Architect", "General contractor"], groups
    g = {x["label"]: x for x in groups}
    assert [p["name"] for p in g["Our team"]["people"]] == [f"{TAG} pm Person"]
    owner = [p["name"] for p in g["Owner"]["people"]]
    assert owner == [f"{TAG} Owner Dana", f"{TAG} Pm Riley"], owner   # each is the main contact of its role; the owner first
    assert {p["name"] for p in g["Engineer / Architect"]["people"]} == {f"{TAG} Eng Lee", f"{TAG} Arch Morgan"}
    assert {p["role"] for p in g["Engineer / Architect"]["people"]} == {"Structural engineer", "Architect"}
    assert [p["name"] for p in g["General contractor"]["people"]] == [f"{TAG} Gc Chris"]
    assert project_contacts.ROLES == {"owner": "Owner", "property_manager": "Property manager",
                                      "consultant": "Engineer / Architect", "site_supervisor": "General contractor"}
    passed += 1; print("  field + contacts ok")

    # ---- the page
    page = cs.get(f"/projects/{P}").get_data(as_text=True)
    pm_page = pm.get(f"/projects/{P}").get_data(as_text=True)
    i = page.index("// ---- KPI band (Oct 7 C-suite notes)")
    band = page[i:page.index("$j('jx-kpis').innerHTML", i)]
    assert re.findall(r"kpi\('([^']+)'", band) == ["On site today", "Complete"] and ">Open items<" in band, band
    for gone in ("Drops behind", "Today\\'s report", "Balance to finish", "Open RFIs", 'id="jx-schedule"', "Safety events", 'id="jx-risk"'):
        assert gone not in page[page.index('data-view="dashboard"'):page.index('id="jx-ops-toggle"')], gone
    assert 'id="jx-items"' in page and 'id="jx-form"' in page and "/api/team/items" in page
    assert '<h2>Finances <a data-goto="payments">' in page and 'id="jx-money"' not in pm_page
    passed += 1; print("  dashboard page ok")

    # ---- sidebar: three groups, Finances for the C-suite only, every tab kept
    nav = page[page.index('<nav class="nav">'):page.index("</nav>", page.index('<nav class="nav">'))]
    assert re.findall(r'<span class="nav-group-name">([^<]+)</span>', nav) == ["Overview", "On Site", "Finances"]
    want = {"dashboard", "contacts", "schedule", "documents", "rfis", "violations", "compliance", "templates", "employees", "drawings",
            "field-photos", "drop-report", "drop-plans", "daily-report", "weekly-summary", "materials", "toolbox-talks",
            "sov", "payments", "expenses", "product-usage"}
    assert set(re.findall(r'data-view="([^"]+)"', nav)) == want
    pm_nav = pm_page[pm_page.index('<nav class="nav">'):pm_page.index("</nav>", pm_page.index('<nav class="nav">'))]
    assert re.findall(r'<span class="nav-group-name">([^<]+)</span>', pm_nav) == ["Overview", "On Site"] and 'data-view="sov"' not in pm_nav
    passed += 1; print("  sidebar ok")

    # ---- Employees: the crew list with licenses
    crew = cs.get(f"/api/projects/{P}/crew-compliance").get_json()["data"]["workers"]
    by = {w["worker_id"]: w for w in crew}
    assert by["W-9358"]["role_on_project"] == "Foreman" and by["W-9359"]["role_on_project"] is None
    emp = page[page.index('<section class="view" data-view="employees">'):]
    emp = emp[:emp.index("</section>")]
    assert "<th>Role on this job</th><th>Licenses</th>" in emp and "ec-hero" not in emp and "Employees &amp; Certifications" not in emp
    assert ">Employees</a>" in nav
    passed += 1; print("  employees ok")

    # ---- drawings: 3D first, no Author button
    mk_html = (ROOT / "templates" / "v2" / "drawing-markup.html").read_text(encoding="utf-8")
    assert 'id="authorlink"' not in mk_html and ">Author</a>" not in mk_html
    assert "function autoOpen3d()" in mk_html and "if (V3.mode == null) V3.mode = 'massing';" in mk_html and "autoOpen3d();" in mk_html
    passed += 1; print("  drawings ok")

    print(f"PASS smoke_job_page_redesign ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_job_page_redesign: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
