"""#284 GUARD — THE PORTAL FLIP: matrix, nav-from-grants, registry-served payloads,
preview parity, and the landing switch. #285 extended it with the PARITY CONTRACT:
photos mirror (drop/elevation labels, caption BANNED with a populated sentinel,
stat strip), progress boards (active drops with template-step text, status counts,
elevation bars, registered weather), shared-sheet-on-both-surfaces assertions, and
a static free-text-column scan over every SQL string in portal_sections.py.

What it proves:
  MATRIX      fail-closed on every axis: an unseeded role resolves to an EMPTY
              section set (unit); a client's payload endpoint refuses a section
              without a grant even though the matrix allows it; a vendor/unknown
              external role has no portal surface at all.
  NAV         the served shell's DOM (scripts stripped — the #283 lesson) carries
              EXACTLY the effective sections as nav+views, zero internal nav
              items, for BOTH seeded grant shapes (progress+daily, and
              progress+photos+documents).
  PAYLOADS    right-subset by id for all four sections: photos/documents list
              ONLY item-shared ids of the client's own project; daily days carry
              only registered fields; progress carries only registered fields.
  DAILY       the operator-approved allowlist, asserted as recursive KEY-ABSENCE
              over the whole JSON (worker/hours/rate/headcount/quantity/note/
              reason/free-text keys never appear at any depth).
  PARITY      an admin preview of a client's shell is BYTE-IDENTICAL to the
              client's own page, and the section payloads are JSON-identical.
  LANDING     /welcome and Classic /portal forward a granted client to
              /portal/<code>; the #267 zero-grant hard-stop is byte-unchanged
              (welcome only, APIs 403); the architect keeps /drawing-markup;
              internal roles keep the internal shell on the same route.
  ISOLATION   cross-project by URL is 403 on every new endpoint; cross-project /
              unshared by-id photo and document fetches are 404; a client on the
              internal /api/* namespace is still 403.

Isolated backend REQUIRED (seeds users/projects/photos/documents/elevation rows).
PII-safe: synthetic identities, ids/keys/counts only. 127.0.0.1 only.
"""
from __future__ import annotations

import os
import re
import secrets
import sys
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import ssc_paths  # noqa: E402  # #287 — fixture paths honor SSC_DATA_ROOT

import db_layer  # noqa: E402
import portal_matrix  # noqa: E402
import client_registry as reg  # noqa: E402

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5050")
PC, PD = "SMK284-C", "SMK284-D"
PASS, FAIL = [], []
IDS = {"users": [], "photos": [], "docs": []}
FIX_SEQ = 99902   # the seeded report_index dcr_sequence for PC
DCR_FIX_DIR = ssc_paths.under_root("data_room", "reports", "dcr", PC, f"{FIX_SEQ:03d}")   # #287


def ok(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {note}" if note and not cond else ""))
    return bool(cond)


def markup(s):
    s = re.sub(r"<script\b.*?</script>", "", s, flags=re.S | re.I)
    return re.sub(r"<style\b.*?</style>", "", s, flags=re.S | re.I)


def seed():
    conn = db_layer.connect()
    try:
        for code in (PC, PD):
            conn.execute("DELETE FROM projects WHERE project_code=?", (code,))
            conn.execute("INSERT INTO projects (project_code, name, status) VALUES (?,?,'active')",
                         (code, f"Smoke 284 {code[-1]}"))
        users = {}
        for key, role in (("admin", "admin"), ("pm_on", "pm"), ("cli_min", "client"),
                          ("cli_full", "client"), ("cli_zero", "client"), ("cli_b", "client"),
                          ("arch", "architect"), ("vend", "vendor")):
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                "must_reset_password, is_system) VALUES (?,?,?,?,1,'active',0,1)",
                (f"smk284-{key}@superstars.local", "x!unusable", role, f"SMK284 {key}"))
            users[key] = cur.lastrowid
            IDS["users"].append(cur.lastrowid)
        for key, code in (("pm_on", PC), ("cli_min", PC), ("cli_full", PC), ("cli_zero", PC),
                          ("cli_b", PD), ("arch", PC), ("vend", PC)):
            conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_by, "
                         "assigned_at) VALUES (?,?,?, '2026-07-28T00:00:00')",
                         (users[key], code, users["admin"]))
        for key, code, secs in (("cli_min", PC, ("progress", "daily")),
                                ("cli_full", PC, ("progress", "photos", "documents")),
                                ("cli_b", PD, ("progress", "photos", "documents", "daily"))):
            for s in secs:
                conn.execute("INSERT INTO client_section_grant (user_id, project_code, section, "
                             "granted_by, granted_at) VALUES (?,?,?,?, '2026-07-28T00:00:00')",
                             (users[key], code, s, users["admin"]))

        # drops BEFORE photos: the shared photo references SMK284-DP3 (FK-safe on PG)
        conn.execute("INSERT INTO drops (drop_id, project_code, elevation, sequence_no, lifecycle) "
                     "VALUES ('SMK284-DP3', ?, 'North', 3, 'scaffold_active')", (PC,))

        def photo(code, cap, shared, drop_id=None):
            cur = conn.execute(
                "INSERT INTO field_photos (project_code, drop_id, caption, taken_at, uploaded_at, "
                "file_path, thumb_path, mime) VALUES (?,?,?, '2026-07-27 10:00:00', "
                "'2026-07-27 10:05:00', 'x/none.jpg', 'x/none_t.jpg', 'image/jpeg')",
                (code, drop_id, cap))
            pid = cur.lastrowid
            IDS["photos"].append(pid)
            if shared:
                conn.execute("INSERT INTO item_visibility (item_type, item_id, audience, "
                             "shared_by, shared_at) VALUES ('photo', ?, 'client', ?, "
                             "'2026-07-27T12:00:00')", (pid, users["admin"]))
            return pid

        def doc(code, title, shared):
            cur = conn.execute(
                "INSERT INTO project_documents (project_code, category, title, doc_type, "
                "file_path, effective_date, uploaded_at) VALUES (?, 'PERMITS', ?, 'PDF', "
                "'x/none.pdf', '2026-07-01', '2026-07-01T09:00:00')", (code, title))
            did = cur.lastrowid
            IDS["docs"].append(did)
            if shared:
                conn.execute("INSERT INTO item_visibility (item_type, item_id, audience, "
                             "shared_by, shared_at) VALUES ('document', ?, 'client', ?, "
                             "'2026-07-27T12:00:00')", (did, users["admin"]))
            return did

        fx = {
            # captions stay POPULATED — they are leak sentinels (#285: caption must
            # never reach the external photos payload again)
            "p_shared": photo(PC, "SENTINEL-284-CAPTION", True, drop_id="SMK284-DP3"),
            "p_unshared": photo(PC, "smk284 unshared", False),
            "p_other": photo(PD, "smk284 other-project shared", True),
            "d_shared": doc(PC, "SMK284 Shared Permit", True),
            "d_unshared": doc(PC, "SMK284 Unshared", False),
            "d_other": doc(PD, "SMK284 Other Project", True),
        }
        # daily fixtures on PC: an issued day with weather + stage + cell change.
        # Free-text columns are POPULATED with sentinels so a leak is detectable.
        conn.execute("INSERT INTO report_index (report_date, project_code, report_type, status, "
                     "report_id, dcr_sequence, no_work, no_work_reason, no_work_note) VALUES "
                     "('2026-07-27', ?, 'DCR', 'issued', 'SMK-284-RI', 99902, 0, "
                     "'SENTINEL-284-REASON', 'SENTINEL-284-NOTE')", (PC,))
        conn.execute("INSERT INTO weather_log (date, project_code, am_temp_f, pm_temp_f, "
                     "am_conditions, pm_conditions, wind) VALUES ('2026-07-27', ?, 71.0, 84.0, "
                     "'Sunny', 'Partly cloudy', '5-10 mph')", (PC,))
        conn.execute("INSERT INTO work_log (date, project_code, scope_of_work, description) "
                     "VALUES ('2026-07-27', ?, 'SENTINEL-284-SCOPE', 'SENTINEL-284-DESC')", (PC,))
        cur = conn.execute("INSERT INTO stage_templates (project_code, name) VALUES (?, 'SMK284 T')",
                           (PC,))
        tmpl = cur.lastrowid
        conn.execute("INSERT INTO stage_template_steps (template_id, step_no, name) "
                     "VALUES (?, 2, 'Grinding & Routing')", (tmpl,))
        conn.execute("INSERT INTO drop_stage_status (drop_id, step_no, status, started_on, note) "
                     "VALUES ('SMK284-DP3', 2, 'in_progress', '2026-07-27', 'SENTINEL-284-STAGE')")
        # #286 — look-ahead fixtures with SENTINELS: crew/notes must never cross;
        # a delivery's stored TITLE (vendor-bearing) must be replaced with "Delivery".
        conn.execute("INSERT INTO lookahead_activity (project_code, drop_id, name, activity_type, "
                     "planned_start, planned_finish, crew, source, notes) VALUES "
                     "(?, 'SMK284-DP3', 'SMK284 Facade Work', 'work', '2026-07-27', "
                     "'2026-08-08', 'SENTINEL-284-CREW', 'manual', 'SENTINEL-284-LANOTE')", (PC,))
        conn.execute("INSERT INTO lookahead_activity (project_code, drop_id, name, activity_type, "
                     "planned_start, planned_finish, source) VALUES "
                     "(?, 'SMK284-DP3', 'SENTINEL-284-VENDOR Masonry Delivery', 'delivery', "
                     "'2026-08-04', '2026-08-04', 'manual')", (PC,))
        cur = conn.execute("INSERT INTO elevation (project_code, face, name) VALUES (?, 'N', "
                           "'SMK284 North')", (PC,))
        elev = cur.lastrowid
        cur = conn.execute("INSERT INTO elevation_drop (elevation_id, idx) VALUES (?, 3)", (elev,))
        edrop = cur.lastrowid
        cur = conn.execute("INSERT INTO elevation_cell (drop_id, level_id, level_name, status_key, "
                           "internal_note) VALUES (?, 'L2', 'Level 2', 'in_progress', "
                           "'SENTINEL-284-INTNOTE')", (edrop,))
        cell1 = cur.lastrowid
        conn.execute("INSERT INTO elevation_cell_event (cell_id, from_status, to_status, reason, "
                     "actor_uid, created_at) VALUES (?, 'not_started', 'in_progress', "
                     "'SENTINEL-284-CELLREASON', ?, '2026-07-27T09:30:00')",
                     (cell1, users["admin"]))
        # #286 CHURN PLANTS — none of these may render:
        # (a) a same-day NO-OP write on cell1 (X -> X)
        conn.execute("INSERT INTO elevation_cell_event (cell_id, from_status, to_status, "
                     "actor_uid, created_at) VALUES (?, 'in_progress', 'in_progress', ?, "
                     "'2026-07-27T10:00:00')", (cell1, users["admin"]))
        # (b) a same-day ROUND TRIP on a second cell (A -> B -> A nets to nothing)
        cur = conn.execute("INSERT INTO elevation_cell (drop_id, level_id, level_name, "
                           "status_key) VALUES (?, 'L3', 'Level 3', 'not_started')", (edrop,))
        cell2 = cur.lastrowid
        conn.execute("INSERT INTO elevation_cell_event (cell_id, from_status, to_status, reason, "
                     "actor_uid, created_at) VALUES (?, 'not_started', 'on_hold', "
                     "'SENTINEL-284-CELLREASON', ?, '2026-07-27T11:00:00')", (cell2, users["admin"]))
        conn.execute("INSERT INTO elevation_cell_event (cell_id, from_status, to_status, "
                     "actor_uid, created_at) VALUES (?, 'on_hold', 'not_started', ?, "
                     "'2026-07-27T12:00:00')", (cell2, users["admin"]))
        # #286 — the client-audience render fixtures for the by-seq view route
        (DCR_FIX_DIR).mkdir(parents=True, exist_ok=True)
        (DCR_FIX_DIR / "client.html").write_text(
            "<html><body>SMK284 CLIENT RENDER MARKER</body></html>", encoding="utf-8")
        (DCR_FIX_DIR / "internal.html").write_text(
            "<html><body>SMK284 INTERNAL RENDER MARKER — SENTINEL-284-INTERNALDOC"
            "</body></html>", encoding="utf-8")
        sessions = {}
        for key, uid in users.items():
            tok = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions (id, user_id, expires_at, user_agent) "
                         "VALUES (?,?, '2099-01-01T00:00:00', 'smk284')", (tok, uid))
            sessions[key] = tok
        conn.commit()
        return users, sessions, fx
    finally:
        conn.close()


def cleanup():
    conn = db_layer.connect()
    try:
        uids = IDS["users"]
        if uids:
            ph = ",".join("?" * len(uids))
            for t, c in (("client_section_grant", "user_id"), ("pm_project_assignment", "user_id"),
                         ("sessions", "user_id"), ("login_audit", "user_id"),
                         ("audit_log", "actor_user_id"), ("dashboard_layouts", "user_id")):
                try:
                    conn.execute(f"DELETE FROM {t} WHERE {c} IN ({ph})", tuple(uids))
                except Exception as e:
                    print(f"    [cleanup] {t}: {e}")
            try:
                conn.execute(f"DELETE FROM audit_log WHERE target_type='user' AND target_id IN ({ph})",
                             tuple(str(u) for u in uids))
            except Exception as e:
                print(f"    [cleanup] audit_log targets: {e}")
            conn.execute(f"DELETE FROM users WHERE id IN ({ph})", tuple(uids))
        if IDS["photos"]:
            ph = ",".join("?" * len(IDS["photos"]))
            conn.execute(f"DELETE FROM item_visibility WHERE item_type='photo' AND item_id IN ({ph})",
                         tuple(IDS["photos"]))
            conn.execute(f"DELETE FROM field_photos WHERE id IN ({ph})", tuple(IDS["photos"]))
        if IDS["docs"]:
            ph = ",".join("?" * len(IDS["docs"]))
            conn.execute(f"DELETE FROM item_visibility WHERE item_type='document' AND item_id IN ({ph})",
                         tuple(IDS["docs"]))
            conn.execute(f"DELETE FROM project_documents WHERE id IN ({ph})", tuple(IDS["docs"]))
        conn.execute("DELETE FROM drop_stage_status WHERE drop_id='SMK284-DP3'")
        conn.execute("DELETE FROM stage_template_steps WHERE template_id IN "
                     "(SELECT template_id FROM stage_templates WHERE project_code IN (?,?))", (PC, PD))
        conn.execute("DELETE FROM elevation_cell_event WHERE cell_id IN (SELECT c.id FROM "
                     "elevation_cell c JOIN elevation_drop d ON d.id=c.drop_id JOIN elevation e "
                     "ON e.id=d.elevation_id WHERE e.project_code IN (?,?))", (PC, PD))
        conn.execute("DELETE FROM elevation_cell WHERE drop_id IN (SELECT d.id FROM elevation_drop d "
                     "JOIN elevation e ON e.id=d.elevation_id WHERE e.project_code IN (?,?))", (PC, PD))
        conn.execute("DELETE FROM elevation_drop WHERE elevation_id IN (SELECT id FROM elevation "
                     "WHERE project_code IN (?,?))", (PC, PD))
        for t in ("elevation", "report_index", "weather_log", "work_log", "drops",
                  "stage_templates", "lookahead_activity"):
            conn.execute(f"DELETE FROM {t} WHERE project_code IN (?,?)", (PC, PD))
        conn.execute("DELETE FROM projects WHERE project_code IN (?,?)", (PC, PD))
        conn.commit()
        import shutil
        shutil.rmtree(DCR_FIX_DIR.parent, ignore_errors=True)   # data_room/.../SMK284-C/
        print("  [cleanup] synthetic rows removed (scoped to SMK284 ids)")
    finally:
        conn.close()


def S(sessions, key):
    s = requests.Session()
    s.cookies.set("ssc_session", sessions[key])
    return s


# The daily allowlist, enforced as recursive KEY-ABSENCE. Includes every column name a
# leak would ride in on, not just the obvious vocabulary.
BANNED_DAILY_KEYS = {
    "worker", "workers", "worker_id", "employee_id", "name", "full_name", "hours",
    "worked_hours", "headcount", "rate", "rates", "cost", "pay", "qty", "quantity",
    "sov", "note", "notes", "internal_note", "reason", "no_work_reason", "no_work_note",
    "scope_of_work", "trades_working", "trade_area", "location_elevation", "description",
    "time_in", "time_out", "caption", "dcr_sequence", "actor_uid",
    "updated_by_uid", "uploaded_by_uid", "file_path", "thumb_path",
    # report_id LEFT the banned set in #286 (operator-approved: the daily table's
    # first column is the GENERATED display id; "seq" addresses the view route)
}


def banned_keys_found(obj, path=""):
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in BANNED_DAILY_KEYS:
                found.append(path + str(k))
            found += banned_keys_found(v, path + str(k) + ".")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += banned_keys_found(v, f"{path}{i}.")
    return found


def run():
    users, sessions, fx = seed()
    R = dict(allow_redirects=False, timeout=25)
    AD, PM = S(sessions, "admin"), S(sessions, "pm_on")
    CMIN, CFULL, CZERO = S(sessions, "cli_min"), S(sessions, "cli_full"), S(sessions, "cli_zero")
    CB, ARCH, VEND = S(sessions, "cli_b"), S(sessions, "arch"), S(sessions, "vend")

    print("\n-- matrix: fail-closed on every axis --")
    ok("matrix_unit_unknown_role_empty", portal_matrix.possible_sections("vendor") == frozenset()
       and portal_matrix.possible_sections("nope") == frozenset()
       and portal_matrix.possible_sections(None) == frozenset())
    ok("matrix_unit_client_row",
       portal_matrix.possible_sections("client") ==
       frozenset({"progress", "photos", "documents", "daily", "schedule", "drawing", "rfis",
                  "proposals"}))   # #308
    ok("matrix_unit_architect_row",
       portal_matrix.possible_sections("architect") == frozenset({"drawing", "rfis", "documents"}))
    r = VEND.get(f"{BASE}/portal/{PC}", **R)
    ok("vendor_has_no_portal_surface", r.status_code in (302, 403), f"{r.status_code}")
    r = CMIN.get(f"{BASE}/api/portal/{PC}/photos", **R)
    ok("granted_role_ungranted_section_403", r.status_code == 403, f"{r.status_code}")
    r = ARCH.get(f"{BASE}/api/portal/{PC}/documents", **R)
    ok("architect_matrix_without_grants_403", r.status_code == 403,
       "documents is in the architect ROW but no grant machinery exists — fail closed")

    print("\n-- landing: the flip, with the #267 hard-stop byte-unchanged --")
    r = CMIN.get(f"{BASE}/welcome", **R)
    ok("granted_welcome_forwards_to_shell", r.status_code == 302
       and r.headers.get("Location", "").endswith(f"/portal/{PC}"),
       f"{r.status_code} -> {r.headers.get('Location')}")
    r = CMIN.get(f"{BASE}/portal", **R)
    ok("classic_portal_forwards_to_shell", r.status_code == 302
       and r.headers.get("Location", "").endswith(f"/portal/{PC}"),
       f"{r.status_code} -> {r.headers.get('Location')}")
    r = CMIN.get(f"{BASE}/projects/{PC}", **R)
    ok("granted_client_contained_off_internal_pages", r.status_code == 302
       and r.headers.get("Location", "").endswith(f"/portal/{PC}"), f"{r.status_code}")
    r = CZERO.get(f"{BASE}/welcome", **R)
    ok("zero_grant_welcome_200", r.status_code == 200, f"{r.status_code}")
    r = CZERO.get(f"{BASE}/portal/{PC}", **R)
    ok("zero_grant_shell_redirects_welcome", r.status_code == 302
       and r.headers.get("Location", "").endswith("/welcome"), f"{r.status_code}")
    r = CZERO.get(f"{BASE}/api/portal/{PC}/progress", **R)
    ok("zero_grant_api_403", r.status_code == 403, f"{r.status_code}")
    r = CZERO.get(f"{BASE}/api/portal/context", **R)
    ok("zero_grant_classic_api_403", r.status_code == 403, f"{r.status_code}")
    r = ARCH.get(f"{BASE}/portal/{PC}", **R)
    ok("architect_landing_unchanged", r.status_code == 302
       and r.headers.get("Location", "").endswith("/drawing-markup"), f"{r.status_code}")
    r = PM.get(f"{BASE}/portal/{PC}", **R)
    ok("internal_role_keeps_internal_shell", r.status_code == 200
       and f"/api/projects/{PC}" in r.text and 'data-portal-nav=' not in markup(r.text),
       f"{r.status_code}")

    print("\n-- nav-from-grants: exact shapes, zero internal items --")
    shapes = (("cli_min", CMIN, ["progress", "daily"]),
              ("cli_full", CFULL, ["progress", "photos", "documents"]))
    pages = {}
    for label, sess, want in shapes:
        r = sess.get(f"{BASE}/portal/{PC}", **R)
        pages[label] = r.content if r.status_code == 200 else b""
        m = markup(r.text if r.status_code == 200 else "")
        navs = re.findall(r'data-portal-nav="(\w+)"', m)
        views = re.findall(r'data-portal-view="(\w+)"', m)
        ok(f"{label}_shell_200", r.status_code == 200, f"{r.status_code}")
        ok(f"{label}_nav_exact", navs == want, f"navs={navs} want={want}")
        ok(f"{label}_views_exact", views == want, f"views={views}")
        ok(f"{label}_zero_internal_nav", 'data-view="' not in m
           and not re.search(r"<!--\s*SECTION:", m) and "PORTAL_SECTION" not in m,
           "internal nav item / marker soup reached an external DOM")
        ok(f"{label}_no_drawing_or_rfis", "drawing" not in navs and "rfis" not in navs)

    print("\n-- payloads: right-subset by id, registered fields only --")
    r = CFULL.get(f"{BASE}/api/portal/{PC}/photos", **R)
    pdata = r.json().get("data", {}) if r.status_code == 200 else {}
    ids = [x["id"] for x in (pdata or {}).get("photos", [])]
    ok("photos_exactly_shared_own_project", ids == [fx["p_shared"]],
       f"{r.status_code} ids={ids} want=[{fx['p_shared']}]")
    if pdata.get("photos"):
        ok("photos_fields_registered_only",
           set(pdata["photos"][0]) <= set(reg.DATASETS["portal.photo"]))
    # ---- #285: the photos mirror — drop/elevation present, caption/labels GONE ----
    first_p = (pdata.get("photos") or [{}])[0]
    ok("photos_carry_drop_and_elevation",
       first_p.get("drop_label") == "DP-3" and first_p.get("elevation") == "North",
       str({k: first_p.get(k) for k in ("drop_label", "elevation")}))
    PHOTO_BANNED = {"caption", "stage", "label", "file_name", "notes", "description",
                    "worker_id", "uploaded_by_uid", "file_path", "thumb_path"}
    def photo_banned_found(obj, path=""):
        out = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).lower() in PHOTO_BANNED:
                    out.append(path + str(k))
                out += photo_banned_found(v, path + str(k) + ".")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                out += photo_banned_found(v, f"{path}{i}.")
        return out
    leaks = photo_banned_found(pdata)
    ok("photos_banned_keys_absent_recursively", not leaks, str(leaks))
    ok("photos_no_caption_sentinel_value", "SENTINEL-284-CAPTION" not in r.text,
       "a populated caption reached the external photos payload")
    stats = pdata.get("stats") or {}
    ok("photos_stats_shape_and_values",
       set(stats) <= set(reg.DATASETS["portal.photos_stats"])
       and stats.get("shared_count") == 1 and stats.get("drops_covered") == 1
       and stats.get("latest_date") == "2026-07-27", str(stats))

    print("\n-- #285 progress boards: registered fields, structured sources --")
    r = CMIN.get(f"{BASE}/api/portal/{PC}/progress", **R)
    gdata = r.json().get("data", {}) if r.status_code == 200 else {}
    act = gdata.get("active_drops") or []
    ok("progress_active_drops_present", len(act) == 1
       and act[0].get("label") == "DP-3" and act[0].get("elevation") == "North",
       str(act))
    ok("progress_active_step_is_template_text",
       act and act[0].get("step") == "Step 2 · Grinding & Routing", str(act))
    ok("progress_active_fields_registered",
       all(set(a) <= set(reg.DATASETS["health.active_drop"]) for a in act))
    scounts = gdata.get("status_counts") or []
    ok("progress_status_counts_shape",
       [ (s.get("status"), s.get("count")) for s in scounts ] ==
       [("active", 1), ("complete", 0), ("not_started", 0)]
       and all(set(s) <= set(reg.DATASETS["health.status_count"]) for s in scounts),
       str(scounts))
    ep = gdata.get("elevation_progress") or []
    ok("progress_elevation_bars_shape", len(ep) == 1
       and ep[0].get("elevation") == "North"
       and all(set(x) <= set(reg.DATASETS["health.elevation_progress"]) for x in ep),
       str(ep))
    wx = gdata.get("weather")
    ok("progress_weather_registered_or_absent",
       wx is None or set(wx) <= set(reg.DATASETS["health.weather"]),
       str(sorted(wx) if isinstance(wx, dict) else wx))
    ok("progress_weather_days_registered",
       all(set(x) <= set(reg.DATASETS["health.weather"])
           for x in (gdata.get("weather_days") or [])))

    print("\n-- #285 parity contract: shared sheet on both surfaces, SQL clean --")
    shell_html = CMIN.get(f"{BASE}/portal/{PC}", **R).text
    ok("portal_links_shared_sheet", "/files/static/css/widgets.css" in shell_html)
    internal_html = PM.get(f"{BASE}/projects/{PC}", **R).text
    ok("internal_links_shared_sheet", "/files/static/css/widgets.css" in internal_html)
    sheet = requests.get(f"{BASE}/files/static/css/widgets.css", timeout=15).text
    ok("shared_sheet_defines_parity_components",
       all(cls in sheet for cls in (".shc-photo", ".shc-tile", ".shc-drop",
                                    ".shc-donut-wrap", ".shc-dayrow", ".shc-wx-now")))
    ok("portal_dom_consumes_shared_components",
       'class="shc-' in shell_html or "shc-kpis" in shell_html)
    ok("internal_markup_untouched_by_shared_layer",
       'shc-' not in markup(internal_html),
       "internal DOM must not reference the shc- namespace (refactor is additive)")
    import ast as _ast
    src = (SCRIPT_DIR / "portal_sections.py").read_text(encoding="utf-8")
    # actual SQL only — a string (or f-string fragment) that BEGINS with a SQL
    # verb. Docstrings legitimately NAME the banned columns while documenting
    # their exclusion; they must not trip the scan.
    sql_strings = [n.value for n in _ast.walk(_ast.parse(src))
                   if isinstance(n, _ast.Constant) and isinstance(n.value, str)
                   and n.value.lstrip().upper().startswith(
                       ("SELECT", "INSERT", "UPDATE", "DELETE", "WITH"))]
    FREETEXT_COLS = re.compile(
        r"\b(caption|notes?|description|reason|scope_of_work|no_work_reason|"
        r"no_work_note|internal_note|trades_working|trade_area|location_elevation)\b")
    dirty = [s[:60] for s in sql_strings if FREETEXT_COLS.search(s)]
    ok("no_freetext_column_in_any_portal_select", not dirty, str(dirty))

    r = CFULL.get(f"{BASE}/api/portal/{PC}/documents", **R)
    dids = [x["id"] for x in (r.json().get("data", {}) or {}).get("documents", [])] if r.status_code == 200 else []
    ok("documents_exactly_shared_own_project", dids == [fx["d_shared"]],
       f"{r.status_code} ids={dids} want=[{fx['d_shared']}]")
    r = CFULL.get(f"{BASE}/api/portal/{PC}/progress", **R)
    d = r.json().get("data", {}) if r.status_code == 200 else {}
    ok("progress_fields_registered_only",
       set(d.get("progress", {})) <= set(reg.DATASETS["health.progress"])
       and set(d.get("summary", {})) <= set(reg.DATASETS["portal.progress_summary"]),
       str(d))

    print("\n-- daily: the allowlist as recursive key-absence --")
    r = CMIN.get(f"{BASE}/api/portal/{PC}/daily", **R)
    ok("daily_200", r.status_code == 200, f"{r.status_code}")
    days = (r.json().get("data", {}) or {}).get("days", []) if r.status_code == 200 else []
    ok("daily_one_day", len(days) == 1, str(len(days)))
    if days:
        day = days[0]
        ok("daily_registered_day_fields", set(day) <= (set(reg.DATASETS["portal.daily_day"])
           | {"weather", "active_drops", "activities", "status_changes", "photos"}),
           str(sorted(day)))
        ok("daily_weather_present", (day.get("weather") or {}).get("wind") == "5-10 mph")
        ok("daily_activity_structured", day.get("activities") ==
           [{"category": "Grinding & Routing", "status": "started"}], str(day.get("activities")))
        # #286 — this assertion now DOUBLES as the churn-filter proof: the fixtures
        # plant a same-day no-op (X->X) on cell1 and a same-day round trip (A->B->A)
        # on cell2. EXACTLY the one net change may render; any extra row = the
        # filter regressed.
        ok("daily_change_net_only_churn_filtered", day.get("status_changes") ==
           [{"drop_label": "Drop 3", "level": "Level 2", "from_label": "Not started",
             "to_label": "In progress"}], str(day.get("status_changes")))
        ok("daily_day_has_report_id",
           day.get("report_id") == f"DCR-{PC}-{FIX_SEQ:03d}" and day.get("seq") == FIX_SEQ,
           str({k: day.get(k) for k in ("report_id", "seq")}))
        ok("daily_photos_shared_only", [p.get("id") for p in day.get("photos", [])] ==
           [fx["p_shared"]], str(day.get("photos")))
    body_text = r.text if r.status_code == 200 else ""
    found = banned_keys_found((r.json() if r.status_code == 200 else {}))
    ok("daily_banned_keys_absent_recursively", not found, str(found))
    ok("daily_no_sentinel_values", "SENTINEL-284" not in body_text,
       "a populated free-text column reached the client payload")

    print("\n-- #286 page anatomy: the portal pages ARE the internal pages --")
    # cli_min holds progress+daily; grant schedule too so all three clones render
    conn2 = db_layer.connect()
    try:
        conn2.execute("INSERT INTO client_section_grant (user_id, project_code, section, "
                      "granted_by, granted_at) VALUES (?,?,?,?, '2026-07-29T00:00:00')",
                      (users["cli_min"], PC, "schedule", users["admin"]))
        conn2.commit()
    finally:
        conn2.close()
    shell = CMIN.get(f"{BASE}/portal/{PC}", **R).text
    sm = markup(shell)
    ok("anatomy_ph_header_kpis_grid",
       'class="shc-phead"' in sm and 'class="shc-kpirow"' in sm
       and 'id="pp-grid"' in sm and 'id="pp-reset-layout"' in sm
       and all(f'gs-id="{w}"' in sm for w in
               ("active-drops", "drops-status", "progress-elevation", "weather")))
    ok("anatomy_grid_engine_assets",
       "gridstack-all.js" in shell and "dash_layout.js" in shell)
    ok("anatomy_dcr_table_headers",
       re.search(r"<thead><tr><th>Report ID</th><th>Date</th><th>Status</th><th></th></tr></thead>",
                 sm) is not None)
    ok("anatomy_dcr_head_and_toolbar",
       "Daily Construction Reports" in sm and 'id="dr-search"' in sm
       and 'id="dr-filter-from"' in sm and 'id="dr-filter-apply"' in sm)
    ok("anatomy_dcr_readonly", "Issue New DCR" not in sm
       and "data-edit-seq" not in sm and "dcr-del" not in sm)
    ok("anatomy_la_board", "Two-Week Look-Ahead" in sm and 'id="pla-board"' in sm
       and 'id="pla-start"' in sm and 'id="pla-hero"' in sm)
    ok("anatomy_la_readonly", "la-add" not in sm and "Add activity" not in sm
       and "Refresh" not in sm and "data-rm" not in sm)

    print("\n-- #286 the client-audience DCR view route --")
    v = CMIN.get(f"{BASE}/api/portal/{PC}/daily/{FIX_SEQ}/view", **R)
    ok("view_200_client_file_bytes", v.status_code == 200
       and "SMK284 CLIENT RENDER MARKER" in v.text, f"{v.status_code}")
    ok("view_never_internal_file", "SENTINEL-284-INTERNALDOC" not in v.text,
       "the internal render reached a client")
    v = CMIN.get(f"{BASE}/api/portal/{PD}/daily/{FIX_SEQ}/view", **R)
    ok("view_cross_project_403", v.status_code == 403, f"{v.status_code}")
    v = CMIN.get(f"{BASE}/api/portal/{PC}/daily/99999/view", **R)
    ok("view_unknown_seq_404", v.status_code == 404, f"{v.status_code}")
    v = CMIN.get(f"{BASE}/project-files/data_room/reports/dcr/{PC}/{FIX_SEQ:03d}/internal.html", **R)
    ok("internal_files_namespace_closed", v.status_code in (302, 403), f"{v.status_code}")
    v = CZERO.get(f"{BASE}/api/portal/{PC}/daily/{FIX_SEQ}/view", **R)
    ok("view_zero_grant_403", v.status_code == 403, f"{v.status_code}")

    print("\n-- #286 look-ahead payload: curated, read-only --")
    la = CMIN.get(f"{BASE}/api/portal/{PC}/lookahead", **R)
    ld = la.json().get("data", {}) if la.status_code == 200 else {}
    ok("la_200_days_10", la.status_code == 200 and len(ld.get("days", [])) == 10,
       f"{la.status_code} days={len(ld.get('days', []))}")
    ok("la_groups_nonempty", len(ld.get("groups", [])) >= 1,
       "the fixture activities must produce a drop group — checks below are vacuous otherwise")
    flat = str(ld)
    ok("la_no_crew_notes_constraints",
       "'crew'" not in flat and "'notes'" not in flat and "constraint" not in flat
       and "'source'" not in flat, "an uncurated look-ahead field crossed")
    ok("la_sentinels_never_cross",
       "SENTINEL-284-CREW" not in flat and "SENTINEL-284-LANOTE" not in flat
       and "SENTINEL-284-VENDOR" not in flat,
       "a populated crew/note/vendor value reached the client board")
    for gr in ld.get("groups", []):
        ok("la_group_fields_registered",
           set(gr) <= (set(reg.DATASETS["la.group"]) | {"activities"}), str(sorted(gr)))
        break
    delivs = [a for g in ld.get("groups", []) for a in g.get("activities", [])
              if a.get("activity_type") == "delivery"]
    delivs += [a for a in ld.get("general", []) if a.get("activity_type") == "delivery"]
    ok("la_delivery_present_and_generic", len(delivs) >= 1
       and all(a.get("name") == "Delivery" for a in delivs),
       str([a.get("name") for a in delivs][:3]))
    ok("la_activity_fields_registered",
       all(set(a) <= set(reg.DATASETS["la.activity"])
           for g in ld.get("groups", []) for a in g.get("activities", [])))
    la2 = ARCH.get(f"{BASE}/api/portal/{PC}/lookahead", **R)
    ok("la_architect_403", la2.status_code == 403, f"{la2.status_code}")

    print("\n-- #286 layout persistence round-trip (client user) --")
    p = CMIN.put(f"{BASE}/api/dashboard/layout",
                 json={"page_key": "portal_progress",
                       "layout": [{"id": "weather", "x": 0, "y": 0, "w": 4, "h": 5}]},
                 timeout=15)
    ok("layout_put_200", p.status_code == 200, f"{p.status_code}")
    gj = CMIN.get(f"{BASE}/api/dashboard/layout?page_key=portal_progress", timeout=15).json()
    saved = (gj.get("data") or {}).get("layout") or []
    ok("layout_get_roundtrip", bool(saved) and saved[0].get("id") == "weather", str(saved))
    dl = CMIN.delete(f"{BASE}/api/dashboard/layout?page_key=portal_progress", timeout=15)
    ok("layout_delete_resets", dl.status_code == 200, f"{dl.status_code}")
    zg = CZERO.put(f"{BASE}/api/dashboard/layout",
                   json={"page_key": "portal_progress", "layout": []}, timeout=15)
    ok("layout_zero_grant_403", zg.status_code == 403, f"{zg.status_code}")

    print("\n-- preview parity: byte-for-byte page, JSON-identical payloads --")
    for label, sess, key in (("cli_min", CMIN, "cli_min"), ("cli_full", CFULL, "cli_full")):
        own = sess.get(f"{BASE}/portal/{PC}", **R).content   # fresh — grants changed above
        pv = AD.get(f"{BASE}/portal/{PC}", params={"preview_client": users[key]}, **R)
        ok(f"parity_page_bytes_{label}", pv.status_code == 200 and pv.content == own,
           f"{pv.status_code} own={len(own)}B pv={len(pv.content)}B")
    own = CMIN.get(f"{BASE}/api/portal/{PC}/daily", **R).json()
    pv = AD.get(f"{BASE}/api/portal/{PC}/daily",
                params={"preview_client": users["cli_min"]}, **R).json()
    ok("parity_daily_json", own == pv)
    own = CFULL.get(f"{BASE}/api/portal/{PC}/photos", **R).json()
    pv = AD.get(f"{BASE}/api/portal/{PC}/photos",
                params={"preview_client": users["cli_full"]}, **R).json()
    ok("parity_photos_json", own == pv)

    print("\n-- isolation: cross-project by URL and by id; internal namespace closed --")
    for sec in ("progress", "photos", "documents", "daily"):
        r = CMIN.get(f"{BASE}/api/portal/{PD}/{sec}", **R)
        ok(f"cross_project_url_403_{sec}", r.status_code == 403, f"{r.status_code}")
    r = CFULL.get(f"{BASE}/api/portal/photos/{fx['p_other']}/thumb", **R)
    ok("cross_project_photo_by_id_404", r.status_code == 404, f"{r.status_code}")
    r = CFULL.get(f"{BASE}/api/portal/photos/{fx['p_unshared']}/thumb", **R)
    ok("unshared_photo_by_id_404", r.status_code == 404, f"{r.status_code}")
    r = CFULL.get(f"{BASE}/api/portal/documents/{fx['d_other']}/file", **R)
    ok("cross_project_document_by_id_404", r.status_code == 404, f"{r.status_code}")
    r = CFULL.get(f"{BASE}/api/portal/documents/{fx['d_unshared']}/file", **R)
    ok("unshared_document_by_id_404", r.status_code == 404, f"{r.status_code}")
    cb_ids = [x["id"] for x in (CB.get(f"{BASE}/api/portal/{PD}/photos", **R)
                                .json().get("data", {}) or {}).get("photos", [])]
    ok("other_client_sees_own_project_only", cb_ids == [fx["p_other"]], str(cb_ids))
    for path in (f"/api/projects/{PC}/health", "/api/workers", "/api/field-photos",
                 f"/api/projects/{PC}/photos", "/api/expenses"):
        r = CMIN.get(f"{BASE}{path}", **R)
        ok(f"client_internal_api_403_{path.split('/')[2]}", r.status_code == 403,
           f"{path} -> {r.status_code}")


def main():
    print(f"== #284 guard: portal flip (matrix / nav / payloads / parity / landing) ==  BASE={BASE}")
    db_url = (os.environ.get("SSC_DB_URL") or "").strip()
    print(f"   backend={'postgres' if db_layer.is_postgres() else 'sqlite'}  "
          f"SSC_DB_URL={'(set)' if db_url else '(unset)'}")
    if not db_url and os.environ.get("ALLOW_LIVE") != "1":
        print("REFUSING TO RUN: SSC_DB_URL is unset — this suite seeds users/projects "
              "and must never touch the live DB.")
        return 2
    try:
        run()
    finally:
        cleanup()
    print(f"\n== {len(PASS)} PASS / {len(FAIL)} FAIL ==")
    if FAIL:
        print("FAILURES: " + ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
