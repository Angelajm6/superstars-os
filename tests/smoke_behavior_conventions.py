"""smoke_behavior_conventions.py — behavioral guard (#253).

Stops the recurring FIELD-BUG classes at the root by FAILING the build if a key
modal/surface regresses one of them. These are the exact classes behind #253:

  (a) DATE-CHOSEN-PERSISTS: a user-chosen date field must be read from the picker
      (SSCDatePicker.getISO) on submit — never silently saving today() instead of
      the picked date.  [#253 labor-rate effective date; drop-stage start date;
      expense date]
  (b) SUBMIT-CREATES-RECORD: a primary "submit for approval" must fire on the
      WHOLE change, not a partial slice — the labor-rate submit must fire on a
      RATE *or* EFFECTIVE-DATE change, else a date-only edit is silently dropped
      while still toasting "Submitted".  [#253 submit-to-PM]
  (c) CANCEL-RESETS-FORM: a modal's Cancel/close must reset the form (incl. the
      SSCDatePicker dataset.iso, not just .value) so reopening is a clean slate.
      [#253 onboarding cancel]
  + RENDER: the drop-stage date chip must use a chip-SPECIFIC empty class, not the
      generic .dp-empty (whose 28px padding ballooned every undated chip and
      cascaded the stepper).  [#253 stage-date render]

The static surface checks parse the SERVED HTML/JS of each surface (same approach
as smoke_design_conventions.py) and assert the fixed pattern is present; a
self-test proves every matcher FAILS on the broken pattern and PASSES on the
fixed one.

PLUS one CROSS-VIEW BEHAVIORAL check (#254): an APPROVED labor-rate change —
including a DATE-ONLY backdate — must be reflected in the canonical worker_rates
the tracker/payroll grid resolves (get_rate_effective_on). It drives the real
write-flow on a SYNTHETIC worker (add rate -> submit backdate -> approve) and
asserts the tracker resolves the approved rate for the backdated week. On the
PRE-#254 server (the approve->worker_rates bridge silently skipped backdates)
this FAILS; on the fixed server it PASSES. Synthetic-only, scoped cleanup, real
data untouched; comp-data discipline — prints booleans only, never a rate value.
"""
import os
import re
import secrets
import sqlite3
import sys
from datetime import date, timedelta

import requests

import _smoke_auth  # noqa: E402
_smoke_auth.setup()

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5050")
DB_PATH = _smoke_auth.DB_PATH
import db_layer  # noqa: E402  # #260 — route DB access through the env-driven layer (SSC_DB_URL)
PASS, FAIL = [], []


def ok(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {note}" if note else ""))
    return bool(cond)


def fetch(path):
    r = requests.get(BASE + path, timeout=15)
    r.raise_for_status()
    return r.text


# ---- The recurring-class matchers. Each returns True when the FIXED pattern is
# ---- present. Kept as named functions so the self-test can prove each one
# ---- discriminates broken-vs-fixed on sample strings. ----------------------

def m_date_read(html, field_token):
    """(a) the submit reads the chosen date via SSCDatePicker.getISO(<field>),
    not a hardcoded today()."""
    return re.search(r"getISO\(" + re.escape(field_token) + r"\)", html) is not None


def m_submit_on_rate_or_date(html):
    """(b) the labor-rate edit submit fires on rate OR effective-date change."""
    return re.search(r"rateChanged\s*\|\|\s*dateChanged", html) is not None


def m_cancel_resets(html):
    """(c) the intake modal resets the form (clearing dataset.iso via setISO('')),
    and Cancel/close/open all call it (>=3 call sites)."""
    has_reset = re.search(r"function\s+resetIntakeForm\s*\(", html) is not None
    clears_iso = re.search(r"setISO\(\s*e\s*,\s*''\s*\)", html) is not None
    call_sites = len(re.findall(r"resetIntakeForm\(\)", html))
    return has_reset and clears_iso and call_sites >= 3


def m_stage_chip_specific_empty(html):
    """(RENDER) the drop-stage chip uses the chip-specific empty class, not the
    bare generic .dp-empty (the 28px-padding collision)."""
    uses_specific = "dp-datechip-empty" in html
    # the broken render added a bare ' dp-empty' to the datechip class string
    broken = re.search(r"dp-datechip'\s*\+\s*\(iso\?''\s*:\s*' dp-empty'\)", html) is not None
    return uses_specific and not broken


def m_stage_complete_control(html):
    """(#256) the Drop Plan stage row exposes a backdatable COMPLETE control and an
    UN-COMPLETE toggle, and the completion date rides a SSCDatePicker input. Pre-#256
    the row had only a start-date chip + N/A toggle, so a stage could never be marked
    complete — and since % counts only completed stages, the number could never move
    (and stayed wrong after a hard refresh). The affordance to set the COMPLETION
    signal must exist, be reversible, and use the date picker (backdatable)."""
    has_complete = "data-complete=" in html
    has_uncomplete = "data-uncomplete=" in html
    has_completion_datepicker = "dp-cdatechip" in html  # SSCDatePicker-wired completion date
    return has_complete and has_uncomplete and has_completion_datepicker


def m_dropreport_path_scoped(html):
    """(#257) cross-view / single-source: the Drop Report view must derive its
    project from /projects/<code> in the path — exactly like the Drop Plan board
    and the home progress widget — and pull the header CLIENT / PROJ_NAME from the
    project record (GET /api/projects/<code>). The bug it guards: the report
    HARDCODED PROJECT='FR-BX-001' + CLIENT='Compass Point, LLC' +
    PROJ_NAME='890 E 135th St', so on ANY other project's dashboard it loaded
    FR-BX-001's drops and printed FR-BX-001's client/address. Asserts the
    fix-positive shape AND the absence of the specific hardcoded declaration."""
    derives_from_path = "var _drpc=(location.pathname.match(" in html
    pulls_record = ("function loadMeta()" in html
                    and "/api/projects/'+encodeURIComponent(PROJECT)" in html)
    no_hardcoded_project = re.search(r"PROJECT='FR-BX-001'\s*,\s*CLIENT=", html) is None
    no_hardcoded_client = "CLIENT='Compass Point, LLC'" not in html
    no_hardcoded_name = "PROJ_NAME='890 E 135th St'" not in html
    return (derives_from_path and pulls_record and no_hardcoded_project
            and no_hardcoded_client and no_hardcoded_name)


def m_admin_console_home_nav(html):
    """(#258) the admin console (/admin/users) must offer a way OUT — so the operator
    is never stranded there (the #257 dead-end bug). #339 replaced the page's own
    'href="/dashboard"' + 'class="brand-home"' escape hatch with the shared
    company_nav.js sidebar (mounted via <nav data-company-nav> + SSC_NAV.mount()),
    whose Dashboard tab is the equivalent way out."""
    has_shared_nav = 'data-company-nav' in html
    mounts_it = 'SSC_NAV.mount()' in html
    return has_shared_nav and mounts_it


def m_app_admin_users_link(js):
    """(#258) the main app's shared header chip exposes an admin-ONLY entry point to
    /admin/users, so an admin can reach user management from the dashboard/console —
    gated to role=admin (the server route is the real authority; this is the UX link)."""
    links_to_console = "/admin/users" in js
    admin_only = "role === 'admin'" in js or "role==='admin'" in js
    return links_to_console and admin_only


# ---- (#254) cross-view BEHAVIORAL check: approved rate -> tracker resolution --

_PROP_EID, _PROP_WID = "E-99254", "W-9954"


def _prop_teardown():
    conn = db_layer.connect()
    try:
        for tbl in ("labor_rate_change", "labor_worker_state"):
            conn.execute(f"DELETE FROM {tbl} WHERE worker_id=?", (_PROP_WID,))
        conn.execute("DELETE FROM worker_rates WHERE employee_id=?", (_PROP_EID,))
        conn.execute("DELETE FROM project_assignments WHERE employee_id=?", (_PROP_EID,))
        conn.execute("DELETE FROM audit_log WHERE target_id IN (?,?)", (_PROP_EID, _PROP_WID))
        conn.execute("DELETE FROM employees WHERE employee_id=? OR worker_id=?", (_PROP_EID, _PROP_WID))
        conn.commit()
    finally:
        conn.close()


def behavioral_propagation_check():
    """Synthetic write-flow: add rate -> submit a DATE-ONLY backdate -> approve,
    then assert the canonical worker_rates the tracker resolves
    (get_rate_effective_on) reflects the approved rate for the backdated week.
    Returns (ok_bool, note). Fake $1.00 rate (comp discipline); booleans only."""
    from worker_rates import get_rate_effective_on
    _prop_teardown()
    # synthetic active worker + active project assignment (eligible to add a rate)
    conn = db_layer.connect()
    try:
        conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, intake_status) "
                     "VALUES (?,?,?,?, 'active')",
                     (_PROP_EID, _PROP_WID, "ZZ SMOKE Propagation", "Mechanic"))
        proj = conn.execute("SELECT project_code FROM projects WHERE status='active' OR status IS NULL LIMIT 1").fetchone()
        if proj:
            conn.execute("INSERT INTO project_assignments (employee_id, project_code, status) VALUES (?,?, 'active')",
                         (_PROP_EID, proj[0]))
        conn.commit()
    finally:
        conn.close()
    try:
        today = date.today()
        this_mon = today - timedelta(days=today.weekday())
        last_mon = this_mon - timedelta(days=7)
        # 1) initial rate effective THIS week's Monday
        r1 = requests.post(f"{BASE}/api/labor-rates/state",
                           json={"worker_id": _PROP_WID, "trade": "Mechanic", "rate": 1.00,
                                 "effective_date": this_mon.isoformat(), "status": "active"}, timeout=15)
        if r1.status_code not in (200, 201):
            return False, f"add-rate failed ({r1.status_code})"
        # 2) DATE-ONLY BACKDATE to LAST week's Monday (same rate) -> pending
        r2 = requests.post(f"{BASE}/api/labor-rates/changes",
                           json={"worker_id": _PROP_WID, "new_rate": 1.00,
                                 "effective_date": last_mon.isoformat()}, timeout=15)
        if r2.status_code not in (200, 201):
            return False, f"submit failed ({r2.status_code})"
        cid = (r2.json().get("data") or {}).get("change_id")
        if not cid:
            return False, "no change_id"
        # 3) approve -> the bridge must land the backdate in worker_rates
        r3 = requests.post(f"{BASE}/api/labor-rates/changes/{cid}/approve", json={}, timeout=15)
        if r3.status_code != 200:
            return False, f"approve failed ({r3.status_code})"
        # 4) cross-view: does the canonical source the tracker resolves now reflect
        #    the approved rate for the BACKDATED week? (boolean; never the value)
        rconn = db_layer.connect()
        rconn.row_factory = sqlite3.Row
        try:
            resolved = get_rate_effective_on(rconn, _PROP_EID, last_mon.isoformat())
        finally:
            rconn.close()
        if resolved is not None:
            return True, "tracker resolves the approved backdated rate (propagated)"
        return False, "tracker still 'Rate not set' for the backdated week (approve->worker_rates bridge skipped)"
    finally:
        _prop_teardown()


# ---- (#255) look-ahead persistence: drag / add / remove survive a reload ------

_LA_PROJ = "ZZ-SMOKE-LA"


def _la_teardown():
    conn = db_layer.connect()
    try:
        conn.execute("DELETE FROM lookahead_activity WHERE project_code=?", (_LA_PROJ,))
        # #260 — lookahead_activity.project_code REFERENCES projects(project_code).
        # Remove the activity rows (child) first, then the synthetic project, or
        # Postgres' enforced FK blocks the delete (SQLite ran with it off).
        conn.execute("DELETE FROM projects WHERE project_code=?", (_LA_PROJ,))
        conn.commit()
    finally:
        conn.close()


def _la_setup():
    # #260 — create the synthetic project so the lookahead INSERT satisfies the
    # project_code -> projects FK that Postgres enforces.
    conn = db_layer.connect()
    try:
        conn.execute("INSERT INTO projects(project_code, name) VALUES(?, 'ZZ Smoke Look-Ahead')",
                     (_LA_PROJ,))
        conn.commit()
    finally:
        conn.close()


def _la_all(win):
    return list(win.get("general", [])) + [a for g in win.get("groups", []) for a in g.get("activities", [])]


def behavioral_lookahead_check():
    """The Two-Week Look-Ahead is the editable master-schedule source of truth, so
    an ADDED activity must persist, a DRAGGED activity's LOCAL planned dates must
    survive a reload (and lock it source='manual'), and a REMOVED one must be
    gone. Drives add -> drag(PATCH) -> remove(DELETE) on a synthetic project and
    asserts each round-trips through a fresh GET. Returns (ok_bool, note)."""
    _la_teardown()
    _la_setup()
    base = f"{BASE}/api/projects/{_LA_PROJ}/lookahead"
    try:
        r = requests.post(base + "/activities", json={
            "name": "ZZ smoke activity", "activity_type": "work",
            "planned_start": "2026-06-15", "planned_finish": "2026-06-16"}, timeout=15)
        if r.status_code not in (200, 201):
            return False, f"add failed ({r.status_code})"
        aid = (r.json().get("data") or {}).get("id")
        if not aid:
            return False, "add returned no id"
        win = requests.get(base + "?start=2026-06-15", timeout=15).json()["data"]
        if not any(a["id"] == aid for a in _la_all(win)):
            return False, "added activity did not persist"
        # drag: PATCH new LOCAL planned dates -> must persist + lock manual
        p = requests.patch(f"{BASE}/api/lookahead/activities/{aid}",
                           json={"planned_start": "2026-06-18", "planned_finish": "2026-06-19"}, timeout=15)
        if p.status_code != 200:
            return False, f"drag PATCH failed ({p.status_code})"
        win2 = requests.get(base + "?start=2026-06-15", timeout=15).json()["data"]
        a = next((x for x in _la_all(win2) if x["id"] == aid), None)
        if not (a and a["planned_start"] == "2026-06-18" and a["planned_finish"] == "2026-06-19"
                and a["source"] == "manual"):
            return False, "dragged planned dates did not persist (or did not lock manual)"
        # remove: DELETE -> must be gone
        requests.delete(f"{BASE}/api/lookahead/activities/{aid}", timeout=15)
        win3 = requests.get(base + "?start=2026-06-15", timeout=15).json()["data"]
        if any(x["id"] == aid for x in _la_all(win3)):
            return False, "removed activity still present"
        return True, "add + drag (persisted LOCAL dates, locked manual) + remove all round-trip"
    finally:
        _la_teardown()


# ---- (#256) stage completion -> per-drop & overall % derived live, single-source --
# The recurring class here is the #255 family (divergent / stale % source). The
# fix: % is DERIVED on read from the canonical stage-status (drop_stage_status),
# and COMPLETING a stage is the only signal that moves it. This drives the real
# write-flow on a SYNTHETIC project+drop so the live FR-BX-001 data is untouched.

_SC_PROJ = "ZZ-SMOKE-256"
_SC_DROP = "ZZ-SMK-256-DP1"
_SC_STEPS = 5


def _sc_teardown():
    conn = db_layer.connect()
    try:
        conn.execute("PRAGMA busy_timeout=60000;")
        conn.execute("DELETE FROM audit_log WHERE action LIKE 'dropplan_%' AND target_id LIKE ?", (_SC_DROP + "#%",))
        conn.execute("DELETE FROM drop_stage_status WHERE drop_id=?", (_SC_DROP,))
        conn.execute("DELETE FROM drops WHERE drop_id=?", (_SC_DROP,))
        conn.execute("DELETE FROM stage_template_steps WHERE template_id IN "
                     "(SELECT template_id FROM stage_templates WHERE project_code=?)", (_SC_PROJ,))
        conn.execute("DELETE FROM stage_templates WHERE project_code=?", (_SC_PROJ,))
        conn.execute("DELETE FROM projects WHERE project_code=?", (_SC_PROJ,))
        conn.commit()
    finally:
        conn.close()


def _sc_setup():
    conn = db_layer.connect()
    try:
        conn.execute("PRAGMA busy_timeout=60000;")
        conn.execute("INSERT INTO projects(project_code,name) VALUES(?, 'ZZ Smoke 256 Progress')", (_SC_PROJ,))
        conn.execute("INSERT INTO stage_templates(project_code,name) VALUES(?, 'ZZ Smoke 256 template')", (_SC_PROJ,))
        tid = conn.execute("SELECT template_id FROM stage_templates WHERE project_code=?", (_SC_PROJ,)).fetchone()[0]
        for n in range(1, _SC_STEPS + 1):
            conn.execute("INSERT INTO stage_template_steps(template_id,step_no,name,default_working_days) "
                         "VALUES(?,?,?,2)", (tid, n, f"Stage {n}"))
        conn.execute("INSERT INTO drops(drop_id,project_code,elevation,sequence_no,lifecycle) "
                     "VALUES(?,?, 'North', 1, 'scaffold_active')", (_SC_DROP, _SC_PROJ))
        for n in range(1, _SC_STEPS + 1):
            conn.execute("INSERT INTO drop_stage_status(drop_id,step_no,status) VALUES(?,?, 'not_started')", (_SC_DROP, n))
        conn.commit()
    finally:
        conn.close()


def _sc_drop_pct():
    """The value the Drop-Plan card AND the Drop-Report read: drop_detail.progress.pct."""
    r = requests.get(f"{BASE}/api/dropplan/drops/{_SC_DROP}", timeout=15)
    return (r.json().get("data") or {}).get("progress", {}).get("pct")


def _sc_overall_pct():
    """The value the progress WIDGET / hero read: project_rollup.overall_progress_pct."""
    r = requests.get(f"{BASE}/api/dropplan/projects/{_SC_PROJ}/rollup", timeout=15)
    return (r.json().get("data") or {}).get("overall_progress_pct")


def behavioral_stage_completion_check():
    """#256 — completing a stage is the signal that moves %. On a SYNTHETIC
    project+drop (5 stages):
      * a BACKDATED START date -> stage in_progress, % unchanged (per the model);
      * mark COMPLETE with a PAST date -> per-drop % +1/total AND overall % move,
        and the recomputed values come back IN the PATCH response (no extra fetch);
      * completion date is stored as the chosen PAST date, never silently today;
      * (b) the value is identical across the drop-plan card, the drop-report, the
        widget endpoints AND the canonical helper (single-source — fails on divergence);
      * (c) it persists across a reload; un-complete reverts it (and clears completed_on).
    Returns (ok_bool, note). Scoped teardown; real data untouched; numbers only."""
    import dropplan_rollups as R
    _sc_teardown()
    _sc_setup()
    try:
        today = date.today()
        last_week = (today - timedelta(days=7)).isoformat()
        per = round(100.0 / _SC_STEPS, 1)  # 20.0

        if _sc_drop_pct() != 0.0 or _sc_overall_pct() != 0.0:
            return False, "baseline % not 0"

        # (1) BACKDATED START -> in_progress, % unchanged, response carries recomputed %
        r = requests.patch(f"{BASE}/api/dropplan/drops/{_SC_DROP}/stages/1",
                           json={"started_on": last_week}, timeout=15)
        if r.status_code != 200:
            return False, f"start PATCH failed ({r.status_code})"
        j = r.json().get("data") or {}
        if j.get("status") != "in_progress":
            return False, "start date did not set status in_progress (the recompute-signal gap)"
        if "drop_pct" not in j or "overall_pct" not in j:
            return False, "PATCH response missing recomputed drop_pct/overall_pct"
        if j.get("drop_pct") != 0.0 or j.get("overall_pct") != 0.0:
            return False, "start date moved % (should stay 0 until a stage is complete)"

        # (2) COMPLETE with a PAST date -> +1/total per-drop & overall; date is the PAST date
        r = requests.patch(f"{BASE}/api/dropplan/drops/{_SC_DROP}/stages/1",
                           json={"status": "complete", "completed_on": last_week}, timeout=15)
        if r.status_code != 200:
            return False, f"complete PATCH failed ({r.status_code})"
        j = r.json().get("data") or {}
        if j.get("status") != "complete":
            return False, "complete did not set status complete"
        if j.get("completed_on") == today.isoformat() or j.get("completed_on") != last_week:
            return False, f"completion date not the chosen PAST date (got {j.get('completed_on')!r})"
        if j.get("drop_pct") != per or j.get("overall_pct") != per:
            return False, f"% did not rise to {per} on complete (drop={j.get('drop_pct')} overall={j.get('overall_pct')})"

        # (3) SINGLE SOURCE — card, report, widget endpoints + helper all agree
        hconn = db_layer.connect()
        hconn.row_factory = sqlite3.Row
        try:
            helper_drop = R.drop_progress(hconn, _SC_DROP)["pct"]
            helper_overall = R.project_rollup(hconn, _SC_PROJ, include_cost=False)["overall_progress_pct"]
        finally:
            hconn.close()
        vals = [_sc_drop_pct(), _sc_overall_pct(), helper_drop, helper_overall, per]
        if len(set(vals)) != 1:
            return False, f"surfaces diverge (not single-source): {vals}"

        # (4) PERSISTS across a reload (re-read the canonical source + the chosen date)
        if _sc_drop_pct() != per or _sc_overall_pct() != per:
            return False, "completed % did not persist across reload"
        det = requests.get(f"{BASE}/api/dropplan/drops/{_SC_DROP}", timeout=15).json().get("data") or {}
        st1 = next((s for s in det.get("stages", []) if s["step_no"] == 1), {})
        if st1.get("completed_on") != last_week:
            return False, "completion date did not persist as the past date across reload"

        # (5) UN-COMPLETE -> reverts %, clears completed_on, persists
        r = requests.patch(f"{BASE}/api/dropplan/drops/{_SC_DROP}/stages/1",
                           json={"status": "in_progress"}, timeout=15)
        j = r.json().get("data") or {}
        if j.get("drop_pct") != 0.0 or j.get("overall_pct") != 0.0:
            return False, "un-complete did not revert %"
        if j.get("completed_on") is not None:
            return False, "un-complete did not clear completed_on"
        if _sc_drop_pct() != 0.0 or _sc_overall_pct() != 0.0:
            return False, "reverted % did not persist across reload"

        return True, (f"complete +{per} per-drop & overall (single-source across card/report/widget/helper), "
                      "PAST date stored, persists across reload, un-complete reverts")
    finally:
        _sc_teardown()


# ---- (#262) RBAC: Financial section gated to admin/c_suite, server-enforced --------
_RBAC_USERS = {
    "pm": "smk262-pm@superstars.local",
    "c_suite": "smk262-csuite@superstars.local",
}
# #258 — RANDOM per-run password; synthetic is_system=1 users, cleaned up in finally.
_RBAC_PW = secrets.token_urlsafe(18)


def _rbac_ensure_user(email, role):
    from auth import hash_password
    conn = db_layer.connect(pragma_fk=True)
    try:
        row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if row:
            conn.execute("UPDATE users SET password_hash=?, role=?, is_active=1, status='active', "
                         "must_reset_password=0, is_system=1 WHERE email=?",
                         (hash_password(_RBAC_PW), role, email))
        else:
            conn.execute("INSERT INTO users (email,password_hash,role,full_name,is_active,status,"
                         "must_reset_password,is_system,created_at) VALUES (?,?,?,?,1,'active',0,1,?)",
                         (email, hash_password(_RBAC_PW), role, f"SMK262 {role}", date.today().isoformat()))
        conn.commit()
    finally:
        conn.close()


def _rbac_login(email):
    s = requests.Session()   # fresh session (NOT the _smoke_auth-patched admin one)
    r = s.post(f"{BASE}/api/auth/login", json={"email": email, "password": _RBAC_PW}, timeout=10)
    return s if (r.status_code == 200 and s.cookies.get("ssc_session")) else None


def _rbac_assign_pm(email, project_code):
    """#263 — a pm is now scoped to ASSIGNED projects, so the RBAC check assigns the
    synthetic pm to FR-BX-001 before probing that project's (role-gated) surfaces;
    otherwise the project-assignment axis 403s before the Financial role gate is even
    reached. Ensures the pm_project_assignment schema (idempotent, both backends)."""
    from apply_pm_assignment_263 import ensure_pm_assignment_schema
    conn = db_layer.connect(pragma_fk=True)
    try:
        ensure_pm_assignment_schema(conn)
        uid = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
        conn.execute("DELETE FROM pm_project_assignment WHERE user_id=? AND project_code=?",
                     (uid, project_code))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_by, assigned_at) "
                     "VALUES (?, ?, ?, ?)", (uid, project_code, uid, date.today().isoformat()))
        conn.commit()
    finally:
        conn.close()


def _rbac_cleanup():
    conn = db_layer.connect(pragma_fk=True)
    try:
        for email in _RBAC_USERS.values():
            u = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if u:
                conn.execute("DELETE FROM pm_project_assignment WHERE user_id=? OR assigned_by=?", (u[0], u[0]))
                conn.execute("DELETE FROM login_audit WHERE user_id=?", (u[0],))
                conn.execute("DELETE FROM role_change_audit WHERE user_id=? OR changed_by=?", (u[0], u[0]))
                conn.execute("DELETE FROM sessions WHERE user_id=?", (u[0],))
                conn.execute("DELETE FROM users WHERE id=?", (u[0],))
        conn.commit()
    finally:
        conn.close()


def behavioral_rbac_financial_check():
    """#262 — Financial section (Schedule of Values + Labor Rate Tracker) gated to
    admin/c_suite, enforced in BOTH the server-rendered sidebar AND the endpoints.
    Drives real pm + c_suite sessions and asserts (fails on the pre-#262 server, where
    the Financial group is not stripped and there is no gated SoV endpoint):
      (a) the served dashboard for a PM has NO Financial group / SoV view, but DOES have
          Compliance; for c_suite both groups are present (the split);
      (b) a PM request to a Financial endpoint -> 403 while c_suite -> 200 (SoV data API
          + the labor-rates roster); and PM still reaches a non-Financial operational
          endpoint (not over-blocked).
    Synthetic is_system=1 users, scoped cleanup; never prints a rate value (comp/PII)."""
    for role, email in _RBAC_USERS.items():
        _rbac_ensure_user(email, role)
    try:
        pm = _rbac_login(_RBAC_USERS["pm"])
        cs = _rbac_login(_RBAC_USERS["c_suite"])
        if not (pm and cs):
            ok("rbac_role_logins", False, "could not log in synthetic pm/c_suite")
            return
        ok("rbac_role_logins", True)

        # #263 — assign the pm to FR-BX-001 so it can OPEN that project (the role axis,
        # not the assignment axis, is what this #262 check exercises). The project
        # dashboard for a pm is reached at /projects/<code> (a pm's /dashboard now
        # redirects to its projects list).
        _rbac_assign_pm(_RBAC_USERS["pm"], "FR-BX-001")
        pm_html = pm.get(f"{BASE}/projects/FR-BX-001", timeout=15).text
        cs_html = cs.get(f"{BASE}/projects/FR-BX-001", timeout=15).text

        # (a) sidebar — server-rendered per role
        # #340 — Phase 3 merged the old Financial + Expenses groups into one gated
        # "Money" group (still `data-group="financial"`, the SECTION:financial key is
        # unchanged) and folded Compliance & Permits into the ungated "Tracking" group.
        ok("rbac_pm_sidebar_no_financial",
           ('data-group="financial"' not in pm_html) and ('data-view="sov"' not in pm_html),
           "pm served dashboard must OMIT the Money group + SoV view (absent, not hidden)")
        ok("rbac_pm_sidebar_has_compliance",
           'data-view="compliance"' in pm_html, "pm keeps the Compliance & Permits view")
        ok("rbac_csuite_sidebar_has_financial",
           ('data-group="financial"' in cs_html) and ('data-view="sov"' in cs_html),
           "c_suite sees the Money group + SoV view")
        ok("rbac_sidebar_split_two_groups",
           all(t in cs_html for t in ('data-group="financial"', 'data-group="overview"',
                                      '>Finances<', 'data-view="compliance"')),
           "gated Finances group is distinct from the ungated Overview group that keeps Compliance & Permits")

        # (b) endpoints — pm 403, c_suite 200
        pm_sov = pm.get(f"{BASE}/api/projects/FR-BX-001/sov", timeout=15).status_code
        cs_sov = cs.get(f"{BASE}/api/projects/FR-BX-001/sov", timeout=15).status_code
        ok("rbac_sov_endpoint_pm403_csuite200", pm_sov == 403 and cs_sov == 200,
           f"pm={pm_sov} c_suite={cs_sov}")
        pm_lr = pm.get(f"{BASE}/api/labor-rates/roster", timeout=15).status_code
        cs_lr = cs.get(f"{BASE}/api/labor-rates/roster", timeout=15).status_code
        ok("rbac_laborrates_endpoint_pm403_csuite200", pm_lr == 403 and cs_lr == 200,
           f"pm={pm_lr} c_suite={cs_lr}")
        # pm NOT over-blocked: keeps a non-Financial operational endpoint (drop plan)
        pm_dp = pm.get(f"{BASE}/api/dropplan/projects/FR-BX-001/sov-lines", timeout=15).status_code
        ok("rbac_pm_keeps_operational", pm_dp == 200,
           f"pm dropplan sov-lines={pm_dp} (pm keeps everything except Financial)")
    finally:
        _rbac_cleanup()


def main():
    # ---- self-test: prove the matchers discriminate broken vs fixed ----------
    print("== self-test (prove each matcher fails on the broken pattern) ==")
    ok("selftest_date_read",
       m_date_read("x=SSCDatePicker.getISO($('lr-f-date'));", "$('lr-f-date')")
       and not m_date_read("x=todayISO();", "$('lr-f-date')"))
    ok("selftest_submit_on_rate_or_date",
       m_submit_on_rate_or_date("if(rateChanged || dateChanged){post();}")
       and not m_submit_on_rate_or_date("if(Math.abs(rate-cur)>=0.005){post();}"))
    ok("selftest_cancel_resets",
       m_cancel_resets("function resetIntakeForm(){ SSCDatePicker.setISO(e, ''); } "
                       "resetIntakeForm() resetIntakeForm() resetIntakeForm()")
       and not m_cancel_resets("modal.classList.remove('active');"))
    ok("selftest_stage_chip_empty",
       m_stage_chip_specific_empty("class=\"dp-datechip'+(iso?'':' dp-datechip-empty')+'\"")
       and not m_stage_chip_specific_empty("class=\"dp-datechip'+(iso?'':' dp-empty')+'\""))
    ok("selftest_stage_complete_control",
       m_stage_complete_control("<button data-complete='1'></button><button data-uncomplete='1'></button>"
                                "<input class='dp-cdatechip' data-completestep='1'>")
       and not m_stage_complete_control("<input class='dp-datechip' data-datestep='1'>"
                                        "<button data-na='1'>N/A</button>"))
    ok("selftest_dropreport_path_scoped",
       m_dropreport_path_scoped(
           "var _drpc=(location.pathname.match(/x/)||[])[1]; function loadMeta(){ "
           "jget('/api/projects/'+encodeURIComponent(PROJECT)); }")
       and not m_dropreport_path_scoped(
           "var PROJECT='FR-BX-001', CLIENT='Compass Point, LLC', PROJ_NAME='890 E 135th St';"))
    ok("selftest_admin_console_home_nav",
       m_admin_console_home_nav('<a class="brand-home" href="/"></a><a href="/dashboard">Dashboard</a>')
       and not m_admin_console_home_nav('<h1>User Management</h1>'))
    ok("selftest_app_admin_users_link",
       m_app_admin_users_link("if (user.role === 'admin') { href='/admin/users' }")
       and not m_app_admin_users_link("<button>Sign out</button>"))

    # ---- live surfaces -------------------------------------------------------
    print("\n== surfaces ==")
    surfaces = {}
    for key, path in (("labor", "/admin/labor-rates"),
                      ("console", "/"),
                      ("project", "/dashboard")):
        try:
            surfaces[key] = fetch(path)
        except Exception as e:
            surfaces[key] = ""
            ok(f"fetch_{key}", False, f"{path} -> {e}")

    labor = surfaces.get("labor", "")
    console = surfaces.get("console", "")
    project = surfaces.get("project", "")

    # (a) date-chosen-persists across the date-submit surfaces
    ok("labor_effdate_read_from_picker", m_date_read(labor, "$('lr-f-date')"),
       "labor-rate effective date read via getISO (not today)")
    ok("dropstage_date_read_from_picker", m_date_read(project, "dc"),
       "drop-stage start date read via getISO")
    ok("expense_date_read_from_picker", m_date_read(project, "$('exp-f-date')"),
       "expense date read via getISO")

    # (b) submit-creates-record: labor-rate fires on rate OR date change
    ok("labor_submit_on_rate_or_date", m_submit_on_rate_or_date(labor),
       "edit submit fires on rate OR effective-date change (not rate-only)")

    # (c) cancel-resets-form: onboarding intake
    ok("onboarding_cancel_resets_form", m_cancel_resets(console),
       "intake Cancel/close/open reset the form incl. SSCDatePicker dataset.iso")

    # (render) drop-stage chip uses the chip-specific empty class
    ok("dropstage_chip_specific_empty_class", m_stage_chip_specific_empty(project),
       "stage date chip uses dp-datechip-empty (not the generic .dp-empty)")

    # (#256) the Drop Plan stage row has a backdatable Complete control + un-complete
    ok("dropstage_complete_control_present", m_stage_complete_control(project),
       "stage row exposes data-complete + data-uncomplete + SSCDatePicker completion date")

    # (#257) the Drop Report follows /projects/<code> (not hardcoded to FR-BX-001)
    ok("dropreport_path_scoped_not_hardcoded", m_dropreport_path_scoped(project),
       "Drop Report derives project from the path + pulls client/name from the record "
       "(no FR-BX-001 / 'Compass Point, LLC' / '890 E 135th St' hardcode)")

    # (#258) admin-console navigation — no dead-end + an admin-only entry point
    try:
        admin_console = fetch("/admin/users")            # admin session via _smoke_auth
    except Exception as e:
        admin_console = ""
        ok("fetch_admin_console", False, f"/admin/users -> {e}")
    try:
        auth_menu_js = fetch("/files/static/js/auth_menu.js")
    except Exception as e:
        auth_menu_js = ""
        ok("fetch_auth_menu_js", False, f"auth_menu.js -> {e}")
    ok("admin_console_has_home_nav", m_admin_console_home_nav(admin_console),
       "/admin/users links back to the dashboard + company home (not a dead-end)")
    ok("app_exposes_admin_users_link", m_app_admin_users_link(auth_menu_js),
       "header chip exposes an admin-only Users link to /admin/users")

    # ---- (#254) cross-view propagation: approved rate reflected in the tracker -
    print("\n== cross-view behavioral (approved rate -> tracker resolution) ==")
    try:
        prop_ok, prop_note = behavioral_propagation_check()
    except Exception as e:
        prop_ok, prop_note = False, f"check errored: {type(e).__name__}: {e}"
    ok("approved_rate_propagates_to_tracker", prop_ok, prop_note)

    # ---- (#255) look-ahead: drag / add / remove persist across a reload -------
    print("\n== look-ahead persistence (drag / add / remove round-trip) ==")
    try:
        la_ok, la_note = behavioral_lookahead_check()
    except Exception as e:
        la_ok, la_note = False, f"check errored: {type(e).__name__}: {e}"
    ok("lookahead_drag_add_remove_persist", la_ok, la_note)

    # ---- (#256) stage completion -> per-drop & overall % derived live, single-source -
    print("\n== stage completion (per-drop & overall % derived live, single-source) ==")
    try:
        sc_ok, sc_note = behavioral_stage_completion_check()
    except Exception as e:
        sc_ok, sc_note = False, f"check errored: {type(e).__name__}: {e}"
    ok("stage_complete_moves_pct_single_source_persists", sc_ok, sc_note)

    # ---- (#262) RBAC: Financial section gated to admin/c_suite (sidebar + endpoints) -
    print("\n== RBAC: Financial gated to admin/c_suite (server-rendered sidebar + endpoints) ==")
    try:
        behavioral_rbac_financial_check()
    except Exception as e:
        ok("rbac_financial_gating", False, f"check errored: {type(e).__name__}: {e}")

    print(f"\n== RESULT: {len(PASS)} PASS / {len(FAIL)} FAIL ==")
    if FAIL:
        print("FAILURES: " + ", ".join(FAIL))
    print("OVERALL:", "PASS" if not FAIL else "FAIL")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
