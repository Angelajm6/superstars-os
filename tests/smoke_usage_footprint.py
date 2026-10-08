#!/usr/bin/env python3
"""Job page redesign step 1 — "measure first": which views get opened, and which tools each
job uses (ui_usage.py). Guard.

Boots the real app on an ISOLATED database. Proves:
  * POST /api/usage/view: signed-out 401; a team member's view is counted per day under
    (surface, view, role, job) — never who; repeats add to the same row; an unknown surface or
    view, a non-team role (client) and a job code on a non-job surface are ignored or dropped
  * GET /api/admin/usage: company roles only; every known view listed (0 when never opened),
    the job page first, opens by role, jobs, days used, last day; the days window applies
  * GET /api/admin/projects/footprint: company roles only; per active job, rows per tool
    (tables missing on this database come back as null, never an error), the tabs switched on
  * the three pages send the beacon (with an early-call stub), the job page counts the view it
    opens on and every tab switch; Settings gains "Usage & job tools" (/company#usage)
Run:  venv/bin/python tests/smoke_usage_footprint.py
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
_TMP = tempfile.mkdtemp(prefix="ssc_usage_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)
import ui_usage  # noqa: E402

TAG = "usagefp"
PW = "UsageFootprintSmoke2026"
A, B = "USGFP-A", "USGFP-B"
TODAY = date.today().isoformat()


def _db():
    return db_layer.connect(pragma_fk=False)


def _uids(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]


def _wipe(conn):
    conn.execute("DELETE FROM ui_view_usage")
    for t in ("report_index", "project_assignments", "rfi_log", "projects"):
        try:
            purge(conn, t, "project_code", [A, B])
        except Exception:
            conn.rollback()
    conn.commit()


def _seed() -> dict:
    conn = _db()
    try:
        uids = _uids(conn)
        _wipe(conn)
        purge_users(conn, uids)
        conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, 'Usage Alpha', '1 Use St', 'active')", (A,))
        conn.execute("INSERT INTO projects (project_code, name, address, status, enabled_modules) VALUES (?, 'Usage Bravo', '2 Use St', 'active', ?)",
                     (B, '["crew"]'))
        conn.execute("INSERT INTO report_index (project_code, report_type, report_date, status) VALUES (?, 'DCR', ?, 'final')", (A, TODAY))
        conn.execute("INSERT INTO report_index (project_code, report_type, report_date, status) VALUES (?, 'DCR', ?, 'draft')", (A, TODAY))
        ids = {}
        for key, role in (("cs", "c_suite"), ("pm", "pm"), ("super", "super"), ("client", "client")):
            email = f"{TAG}.{key}@example.com"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Usage {key}"))
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.commit()
        return ids
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        uids = _uids(conn)
        _wipe(conn)
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(key):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@example.com", "password": PW})
    assert r.status_code == 200, (key, r.status_code)
    return c


def _rows():
    conn = _db()
    try:
        return sorted((r["surface"], r["view"], r["role"], r["project_code"], r["opens"]) for r in conn.execute("SELECT * FROM ui_view_usage").fetchall())
    finally:
        conn.close()


def main() -> int:
    _seed()
    passed = 0
    cs, pm, sup, cl = (_login(k) for k in ("cs", "pm", "super", "client"))

    # ---- the beacon
    assert server.app.test_client().post("/api/usage/view", json={"surface": "job", "view": "rfis"}).status_code == 401
    for c, body in ((pm, {"surface": "job", "view": "daily-report", "project_code": A}),
                    (pm, {"surface": "job", "view": "daily-report", "project_code": A}),
                    (sup, {"surface": "job", "view": "daily-report", "project_code": A}),
                    (pm, {"surface": "home", "view": "home", "project_code": A}),
                    (cs, {"surface": "company", "view": "overview"}),
                    (pm, {"surface": "job", "view": "not-a-tab", "project_code": A}),
                    (pm, {"surface": "elsewhere", "view": "home"}),
                    (pm, {"surface": "job", "view": "rfis", "project_code": "bad code!"}),
                    (cl, {"surface": "job", "view": "rfis", "project_code": A})):
        r = c.post("/api/usage/view", json=body)
        assert r.status_code in (204, 403) or (r.status_code == 302), (body, r.status_code)
    assert _rows() == sorted([("job", "daily-report", "pm", A, 2), ("job", "daily-report", "super", A, 1), ("home", "home", "pm", "", 1),
                              ("company", "overview", "c_suite", "", 1), ("job", "rfis", "pm", "", 1)]), _rows()
    conn = _db()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ui_view_usage)").fetchall()} if not db_layer.is_postgres() else set()
        assert not cols or cols == {"day", "surface", "view", "role", "project_code", "opens"}, "no person, no content"
    finally:
        conn.close()
    passed += 1; print("  beacon ok")

    # ---- the report
    assert pm.get("/api/admin/usage").status_code == 403 and sup.get("/api/admin/projects/footprint").status_code == 403
    d = cs.get("/api/admin/usage?days=14").get_json()["data"]
    views = {(v["surface"], v["view"]): v for v in d["views"]}
    assert len(views) == sum(len(v) for v in ui_usage.ALLOWED.values()), "every known view is listed"
    dr = views[("job", "daily-report")]
    assert dr["opens"] == 3 and dr["by_role"]["pm"] == 2 and dr["by_role"]["super"] == 1 and dr["jobs"] == 1 and dr["days"] == 1 and dr["last_day"] == TODAY
    assert views[("job", "templates")]["opens"] == 0 and views[("job", "templates")]["last_day"] is None
    assert d["views"][0]["surface"] == "job" and d["views"][0]["view"] == "daily-report", "the job page first, most opened first"
    assert d["counting_since"] == TODAY
    conn = _db()
    try:
        conn.execute("UPDATE ui_view_usage SET day = ? WHERE view = 'overview'", ((date.today() - timedelta(days=40)).isoformat(),))
        conn.commit()
    finally:
        conn.close()
    assert {(v["surface"], v["view"]): v for v in cs.get("/api/admin/usage?days=14").get_json()["data"]["views"]}[("company", "overview")]["opens"] == 0
    assert {(v["surface"], v["view"]): v for v in cs.get("/api/admin/usage?days=90").get_json()["data"]["views"]}[("company", "overview")]["opens"] == 1
    passed += 1; print("  report ok")

    # ---- the footprint
    f = cs.get("/api/admin/projects/footprint").get_json()["data"]
    jobs = {j["project_code"]: j for j in f["jobs"]}
    assert {A, B} <= set(jobs)
    assert jobs[A]["counts"]["daily"] == 2 and jobs[A]["counts"]["rfis"] == 0 and jobs[A]["customized"] is False
    assert set(jobs[A]["modules_on"]) == set(__import__("project_modules").KEYS), "never customized = every tab on"
    assert jobs[B]["customized"] is True and jobs[B]["modules_on"] == ["crew"]
    keys = {t["key"] for t in f["tools"]}
    assert {"crew", "drops", "rfis", "compliance", "daily", "photos", "payments"} <= keys
    assert all(v is None or isinstance(v, int) for v in jobs[A]["counts"].values()), "a missing table is null, never an error"
    passed += 1; print("  footprint ok")

    # ---- the pages
    stub = "<script>window.SSC_track=window.SSC_track||function(s,v){(window.SSC_trackQ=window.SSC_trackQ||[]).push([s,v]);};</script>"
    for page in ("dashboard-static.html", "company_portal.html", "projects.html"):
        src = (ROOT / page).read_text(encoding="utf-8")
        assert stub in src and 'src="/files/static/js/usage_beacon.js?v=' in src, page
    dash = (ROOT / "dashboard-static.html").read_text(encoding="utf-8")
    assert "SSC_track('job', targetView);" in dash and "document.querySelector('.nav-link.active[data-view]')" in dash
    portal = (ROOT / "company_portal.html").read_text(encoding="utf-8")
    assert "SSC_track('company', v);" in portal and 'id="view-usage"' in portal and "usage: loadUsage" in portal
    assert "SSC_track('home', 'home');" in (ROOT / "projects.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "js" / "usage_beacon.js").read_text(encoding="utf-8")
    assert "navigator.sendBeacon('/api/usage/view'" in js and "early.forEach" in js
    nav = (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")
    assert "['Usage & job tools', '/company#usage']" in nav and "['/company', 'usage']" in nav
    page = cs.get("/company").get_data(as_text=True)
    assert 'id="view-usage"' in page
    passed += 1; print("  pages ok")

    print(f"PASS smoke_usage_footprint ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_usage_footprint: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
