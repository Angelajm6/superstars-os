"""#293 GUARD — authorable elevations: drawing sets, sheet naming, AI-proposed
trace, manual authoring, the draft/confirm state machine, and isolation.

What it proves:
  UPLOAD      multi-page PDF -> drawing_set + one drawing_sheet per page, thumb
              + full renders on disk, served IMMUTABLE (#291 header) with
              containment; non-PDF and garbage rejected; a RASTER pdf (the
              scanned-set stand-in) uploads with graceful BLANK pre-fill.
  NAMING      against the REAL 890 E 135 fixture (7-page vector set): sheet
              numbers A-000.00..A-005.00, elevation flags on p3..p7 and NOT on
              p1/p2, face detection (North/West/South/East), the
              existing+proposed flag on every elevation sheet. Text layer only
              — no OCR anywhere. (Fixture absent -> loud SKIP, lifecycle still
              proven on the raster set.)
  AI          the propose endpoint through the SSC_TRACE_FAKE seam (planted
              JSON through the SAME validator; zero network), region
              validation, and PROPOSE-NEVER-WRITES. On a keyless, fakeless
              server: clean 503 {ai_available:false} AND the full manual
              create->geometry->confirm flow still works end to end.
  STATE       draft is INTERNAL-ONLY (arch/client by-id -> 404, absent from
              their picker); confirm generates bays x floors cells exactly and
              locks the grid (geometry PUT / re-confirm / delete -> 409);
              drafts delete cleanly; painting an authored cell works with the
              same status vocabulary as 890 North (session-2 readiness).
  PARITY      a confirmed authored elevation serves the SAME payload anatomy
              as the traced 890 North (geometry.facade/levels/bounds, drops,
              cells, statuses, derived_status), features {} tolerated;
              80-floor x 12-bay confirm -> 960 cells with sort-safe zero-padded
              level ids and a recorded payload size (tall-building check).
  890         the live backfill through the FULL API path: FR-BX-001 North is
              status=confirmed / face_label=North and still serves 12 drops x
              60 cells (count + rendered spot-check).
  ISOLATION   pm assigned to A only: every set/sheet/author endpoint on B ->
              403. Architect: /drawing-author redirects away, every author API
              403. Client (drawing grant): no author surface at all. No
              session: login redirect / 401.

Self-contained: launches its OWN servers (inherits SSC_DB_URL -> isolated test
DB, never live): SRV1 with SSC_TRACE_FAKE planted + no ANTHROPIC_API_KEY, then
SRV2 with neither (the degrade server). Synthetic fixtures are is_system rows
scoped-cleaned in finally. PII-safe: ids, counts and booleans only.

Run:  python tests/smoke_drawing_author_293.py    (SMOKE_293_PORT overrides 5154)
"""
from __future__ import annotations

import io
import json
import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")   # test-only seams are inert without this (runtime_seams.py)
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import db_layer  # noqa: E402

VENV_PY = SCRIPT_DIR / "venv" / "Scripts" / "python.exe"
PORT = int(os.environ.get("SMOKE_293_PORT", "5154"))
BASE = f"http://127.0.0.1:{PORT}"
SRV_LOG = SCRIPT_DIR / "tests" / "_smoke_293_srv.log"
FAKE_JSON = SCRIPT_DIR / "tests" / "_smk293_trace_fake.json"

PA, PB = "SMK293-A", "SMK293-B"
PASS, FAIL, SKIP = [], [], []
IDS = {"users": []}

FIXTURE = Path(os.environ.get("SMK293_FIXTURE") or (SCRIPT_DIR / "tests" / "fixtures" /
                                                    "890E135_prefiled_plans_2022.pdf"))
if not FIXTURE.exists():
    _alt = Path(r"C:\Users\SSC-Admin\Documents\Claude\Projects\Superstars Dashboard"
                r"\fixtures\890E135_prefiled_plans_2022.pdf")
    if _alt.exists():
        FIXTURE = _alt

# The planted proposal SRV1's fake seam serves — deliberately imperfect
# fractions (they must NORMALIZE through the validator, not pass through).
FAKE_TRACE = {
    "subject_found": True, "multiple_drawings": False,
    "drawings": [{"label": "EXISTING NORTH", "kind": "existing",
                  "x0": 0.06, "y0": 0.52, "x1": 0.55, "y1": 0.95}],
    "bays": 12, "floors": 5,
    "col_fractions": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0.8],
    "row_fractions": [1.35, 1, 1, 1, 1],
    "ground_floor_taller": True,
    "irregularities": ["stair tower at east end", "overhead door bay 11"],
    "confidence": 0.87, "notes": [],
}


def ok(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {note}" if note and not cond else ""))
    return bool(cond)


def skip(name, why):
    SKIP.append(name)
    print(f"  [SKIP] {name} — {why}")


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ---------- fixtures ----------

def make_raster_pdf() -> bytes:
    """A 2-page RASTER pdf via Pillow — a valid PDF with NO text layer, i.e.
    exactly what a scanned set looks like to the parser."""
    from PIL import Image, ImageDraw
    pages = []
    for i in range(2):
        img = Image.new("RGB", (900, 600), "white")
        d = ImageDraw.Draw(img)
        d.rectangle([80, 80, 820, 520], outline="black", width=3)
        d.rectangle([120, 300, 780, 520], outline="black", width=2)
        pages.append(img)
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:])
    return buf.getvalue()


def seed():
    conn = db_layer.connect()
    try:
        for code in (PA, PB):
            conn.execute("DELETE FROM projects WHERE project_code=?", (code,))
            conn.execute("INSERT INTO projects (project_code, name, status) "
                         "VALUES (?,?, 'active')", (code, f"Smoke 293 {code[-1]}"))
        users = {}
        for key, role in (("csuite", "c_suite"), ("pma", "pm"),
                          ("arch", "architect"), ("cl", "client")):
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, role, full_name, is_active, "
                "status, must_reset_password, is_system) VALUES (?,?,?,?,1,'active',0,1)",
                (f"smk293-{key}@superstars.local", "x!unusable", role, f"SMK293 {key}"))
            users[key] = cur.lastrowid
            IDS["users"].append(cur.lastrowid)
        # pm + architect + client all bound to A ONLY
        for key in ("pma", "arch", "cl"):
            conn.execute(
                "INSERT INTO pm_project_assignment (user_id, project_code, assigned_by, "
                "assigned_at) VALUES (?,?,?,?)", (users[key], PA, users["csuite"], _now()))
        conn.execute(
            "INSERT INTO client_section_grant (user_id, project_code, section, granted_by, "
            "granted_at) VALUES (?,?, 'drawing', ?, ?)", (users["cl"], PA, users["csuite"], _now()))
        sessions = {}
        for key, uid in users.items():
            tok = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions (id, user_id, expires_at, user_agent) "
                         "VALUES (?,?, '2099-01-01T00:00:00', 'smk293')", (tok, uid))
            sessions[key] = tok
        conn.commit()
        return users, sessions
    finally:
        conn.close()


def cleanup():
    conn = db_layer.connect()
    try:
        for sql, args in [
            ("DELETE FROM elevation_cell_event WHERE cell_id IN (SELECT c.id FROM "
             "elevation_cell c JOIN elevation_drop d ON d.id=c.drop_id "
             "JOIN elevation e ON e.id=d.elevation_id WHERE e.project_code IN (?,?))", (PA, PB)),
            ("DELETE FROM elevation_cell WHERE drop_id IN (SELECT d.id FROM elevation_drop d "
             "JOIN elevation e ON e.id=d.elevation_id WHERE e.project_code IN (?,?))", (PA, PB)),
            ("DELETE FROM elevation_drop WHERE elevation_id IN (SELECT id FROM elevation "
             "WHERE project_code IN (?,?))", (PA, PB)),
            ("DELETE FROM elevation WHERE project_code IN (?,?)", (PA, PB)),
            ("DELETE FROM drawing_sheet WHERE set_id IN (SELECT id FROM drawing_set "
             "WHERE project_code IN (?,?))", (PA, PB)),
            ("DELETE FROM drawing_set WHERE project_code IN (?,?)", (PA, PB)),
        ]:
            try:
                conn.execute(sql, args)
            except Exception as e:
                print(f"    [cleanup] {e}")
        uids = IDS["users"]
        if uids:
            ph = ",".join("?" * len(uids))
            for t, c in (("client_section_grant", "user_id"),
                         ("pm_project_assignment", "user_id"),
                         ("sessions", "user_id"), ("login_audit", "user_id"),
                         ("audit_log", "actor_user_id"), ("role_change_audit", "user_id")):
                try:
                    conn.execute(f"DELETE FROM {t} WHERE {c} IN ({ph})", tuple(uids))
                except Exception as e:
                    print(f"    [cleanup] {t}: {e}")
            conn.execute(f"DELETE FROM users WHERE id IN ({ph})", tuple(uids))
        conn.execute("DELETE FROM projects WHERE project_code IN (?,?)", (PA, PB))
        conn.commit()
        print("  [cleanup] synthetic rows removed (scoped to SMK293 ids)")
    finally:
        conn.close()
    # on-disk set dirs for the synthetic projects
    import ssc_paths
    import shutil
    for code in (PA, PB):
        d = ssc_paths.under_root("data_room", "drawing_sets") / code
        try:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


# ---------- server lifecycle ----------

def kill_port(port):
    ps = (f"$c=Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction "
          f"SilentlyContinue; foreach($x in $c){{cmd /c \"taskkill /F /T /PID "
          f"$($x.OwningProcess)\"}}")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, timeout=30)


def start_server(extra_env, drop=("ANTHROPIC_API_KEY", "SSC_TRACE_FAKE")):
    env = {**os.environ, "PORT": str(PORT)}
    for k in drop:
        env.pop(k, None)
    env.update(extra_env)
    logf = open(SRV_LOG, "a", encoding="utf-8")
    proc = subprocess.Popen([str(VENV_PY), "server.py"], cwd=str(SCRIPT_DIR),
                            stdout=logf, stderr=subprocess.STDOUT, env=env)
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                return proc
        except requests.exceptions.ConnectionError:
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"server exited rc={proc.returncode} — see {SRV_LOG.name}")
        time.sleep(0.5)
    raise RuntimeError("server did not come up in 45s")


def stop_server(proc):
    if proc:
        subprocess.run(["cmd", "/c", f"taskkill /F /T /PID {proc.pid}"],
                       capture_output=True, timeout=30)


def sess(sessions, key):
    s = requests.Session()
    s.cookies.set("ssc_session", sessions[key])
    return s


# ---------- the checks ----------

def upload_pdf(s, code, data, name="set.pdf"):
    return s.post(f"{BASE}/api/projects/{code}/drawing-sets",
                  files={"file": (name, data, "application/pdf")}, timeout=180)


def run_main(users, sessions):
    """Everything on SRV1 (fake-trace seam active, no API key)."""
    CS = sess(sessions, "csuite")
    PM = sess(sessions, "pma")
    AR = sess(sessions, "arch")
    CL = sess(sessions, "cl")
    R = dict(timeout=30, allow_redirects=False)

    # ================= upload: the REAL fixture (vector text layer) =========
    print("\n-- upload + text-layer naming (real 7-page fixture) --")
    fixture_set = None
    if FIXTURE.exists():
        r = upload_pdf(CS, PA, FIXTURE.read_bytes(), "890E135_prefiled_plans_2022.pdf")
        ok("fixture_upload_201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        if r.status_code == 201:
            fixture_set = r.json()["data"]
            ok("fixture_page_count_7", fixture_set["page_count"] == 7,
               str(fixture_set["page_count"]))
            ok("fixture_7_sheets", len(fixture_set["sheets"]) == 7)
    else:
        skip("fixture_upload_201", f"fixture not found at {FIXTURE}")

    sheets = {}
    if fixture_set:
        r = CS.get(f"{BASE}/api/projects/{PA}/drawing-sets", **R)
        listing = r.json()["data"]
        st = next(x for x in listing if x["id"] == fixture_set["id"])
        sheets = {sh["page_no"]: sh for sh in st["sheets"]}
        want_numbers = {2: "A-000.00", 3: "A-001.00", 4: "A-002.00",
                        5: "A-003.00", 6: "A-004.00", 7: "A-005.00"}
        got = {p: sheets[p]["sheet_number"] for p in want_numbers}
        ok("naming_sheet_numbers", got == want_numbers, str(got))
        elev_flags = {p: sheets[p]["is_elevation"] for p in range(1, 8)}
        ok("naming_elevation_flags", elev_flags ==
           {1: False, 2: False, 3: True, 4: True, 5: True, 6: True, 7: True},
           str(elev_flags))
        want_faces = {3: ["North"], 4: ["West"], 5: ["South"], 6: ["East"]}
        got_faces = {p: sheets[p]["faces"] for p in want_faces}
        ok("naming_faces", got_faces == want_faces, str(got_faces))
        ok("naming_existing_and_proposed",
           all(sheets[p]["has_existing"] and sheets[p]["has_proposed"]
               for p in (3, 4, 5, 6)),
           str({p: (sheets[p]["has_existing"], sheets[p]["has_proposed"])
                for p in (3, 4, 5, 6)}))
        ok("naming_p3_label", bool(sheets[3]["label"]) and
           "ELEVATION" in sheets[3]["label"].upper() and
           "NORTH" in sheets[3]["label"].upper(), str(sheets[3]["label"]))
        ok("naming_p7_label", bool(sheets[7]["label"]) and
           "ELEVATION" in sheets[7]["label"].upper(), str(sheets[7]["label"]))

        # serves: thumb/full/pdf, immutable header, PNG magic
        sh3 = sheets[3]
        rt = CS.get(f"{BASE}/api/drawing-sheets/{sh3['id']}/thumb", **R)
        rf = CS.get(f"{BASE}/api/drawing-sheets/{sh3['id']}/image", **R)
        rp = CS.get(f"{BASE}/api/drawing-sets/{fixture_set['id']}/pdf", **R)
        ok("serve_thumb_png", rt.status_code == 200 and rt.content[:8].startswith(b"\x89PNG"))
        ok("serve_full_png", rf.status_code == 200 and rf.content[:8].startswith(b"\x89PNG"))
        ok("serve_pdf", rp.status_code == 200 and rp.content[:4] == b"%PDF")
        WANT_CC = "private, max-age=31536000, immutable"
        ok("serve_immutable_headers",
           rt.headers.get("Cache-Control") == WANT_CC and
           rf.headers.get("Cache-Control") == WANT_CC and
           rp.headers.get("Cache-Control") == WANT_CC,
           str([rt.headers.get("Cache-Control")]))

        # operator override round-trip
        r = CS.patch(f"{BASE}/api/drawing-sheets/{sheets[1]['id']}",
                     json={"label": "Site plan", "is_elevation": False}, **R)
        ok("sheet_patch_roundtrip", r.status_code == 200 and
           r.json()["data"]["label"] == "Site plan" and
           r.json()["data"]["is_elevation"] is False)

    # ================= upload: raster pdf (scanned-set degradation) =========
    print("\n-- upload: raster pdf (the scanned-set path) + rejects --")
    r = upload_pdf(CS, PA, make_raster_pdf(), "scanned.pdf")
    ok("raster_upload_201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    raster_set = r.json()["data"] if r.status_code == 201 else None
    if raster_set:
        ok("raster_blank_prefill",
           all(sh["label"] is None and sh["sheet_number"] is None and
               not sh["is_elevation"] for sh in raster_set["sheets"]),
           str(raster_set["sheets"]))
    r = upload_pdf(CS, PA, b"not a pdf at all", "junk.pdf")
    ok("garbage_pdf_400", r.status_code == 400, str(r.status_code))
    r = CS.post(f"{BASE}/api/projects/{PA}/drawing-sets",
                files={"file": ("x.txt", b"text", "text/plain")}, timeout=30)
    ok("non_pdf_400", r.status_code == 400, str(r.status_code))

    # ================= AI propose: planted fake through the validator =======
    print("\n-- AI propose (SSC_TRACE_FAKE seam; zero network) --")
    prop_set = fixture_set or raster_set
    prop_page = 3 if fixture_set else 1
    conn = db_layer.connect()
    try:
        n_before = conn.execute("SELECT COUNT(*) AS n FROM elevation").fetchone()["n"]
    finally:
        conn.close()
    r = CS.post(f"{BASE}/api/drawing-sets/{prop_set['id']}/propose",
                json={"page_no": prop_page}, **R)
    ok("propose_200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        p = r.json()["data"]
        ok("propose_counts", p["bays"] == 12 and p["floors"] == 5)
        ok("propose_fractions_normalized",
           abs(sum(p["col_fractions"]) - 1.0) < 0.001 and
           abs(sum(p["row_fractions"]) - 1.0) < 0.001 and
           len(p["col_fractions"]) == 12)
        ok("propose_ground_taller_carried", p["ground_floor_taller"] is True)
        ok("propose_irregularities", len(p["irregularities"]) == 2)
        ok("propose_region_carried", len(p["drawings"]) == 1 and
           p["drawings"][0]["kind"] == "existing")
    r = CS.post(f"{BASE}/api/drawing-sets/{prop_set['id']}/propose",
                json={"page_no": prop_page,
                      "region": {"x0": 0.06, "y0": 0.52, "x1": 0.55, "y1": 0.95}}, **R)
    ok("propose_with_region_200", r.status_code == 200, str(r.status_code))
    r = CS.post(f"{BASE}/api/drawing-sets/{prop_set['id']}/propose",
                json={"page_no": 999}, **R)
    ok("propose_bad_page_404", r.status_code == 404, str(r.status_code))
    r = CS.post(f"{BASE}/api/drawing-sets/{prop_set['id']}/propose",
                json={"page_no": prop_page, "region": {"x0": 0.5, "y0": 0.5,
                                                       "x1": 0.51, "y1": 0.51}}, **R)
    ok("propose_tiny_region_400", r.status_code == 400, str(r.status_code))
    conn = db_layer.connect()
    try:
        n_after = conn.execute("SELECT COUNT(*) AS n FROM elevation").fetchone()["n"]
    finally:
        conn.close()
    ok("propose_never_writes", n_after == n_before, f"{n_before}->{n_after}")

    # ================= authoring state machine ==============================
    print("\n-- authoring: draft -> geometry -> confirm; drafts internal-only --")
    src_sheet = sheets[3]["id"] if sheets else prop_set["sheets"][0]["id"]
    r = CS.post(f"{BASE}/api/author/elevations",
                json={"project_code": PA, "face_label": "Southeast",
                      "source_sheet_id": src_sheet,
                      "region": {"x0": 0.06, "y0": 0.52, "x1": 0.55, "y1": 0.95}}, **R)
    ok("draft_create_201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    draft = r.json()["data"]["id"]
    ok("draft_status_draft", r.json()["data"]["status"] == "draft")

    # invisibility while draft
    ra = AR.get(f"{BASE}/api/elevation/{draft}", **R)
    rc = CL.get(f"{BASE}/api/elevation/{draft}", **R)
    ok("draft_arch_by_id_404", ra.status_code == 404, str(ra.status_code))
    ok("draft_client_by_id_404", rc.status_code == 404, str(rc.status_code))
    la = AR.get(f"{BASE}/api/elevations", **R).json().get("data") or []
    ok("draft_absent_from_arch_picker", all(e.get("id") != draft for e in la))
    ri = CS.get(f"{BASE}/api/elevation/{draft}", **R)
    ok("draft_internal_by_id_200", ri.status_code == 200 and
       ri.json()["data"]["status"] == "draft", str(ri.status_code))

    # geometry: 5 bays x 4 floors, taller ground
    r = CS.put(f"{BASE}/api/author/elevations/{draft}/geometry",
               json={"cols": [1, 1, 1.4, 1, 1], "rows": [1.5, 1, 1, 1]}, **R)
    ok("geometry_put_200", r.status_code == 200 and r.json()["data"]["bays"] == 5
       and r.json()["data"]["floors"] == 4, f"{r.status_code} {r.text[:200]}")
    r = CS.put(f"{BASE}/api/author/elevations/{draft}/geometry",
               json={"cols": [], "rows": [1]}, **R)
    ok("geometry_rejects_empty", r.status_code == 400, str(r.status_code))
    r = CS.put(f"{BASE}/api/author/elevations/{draft}/geometry",
               json={"cols": [1, -2], "rows": [1]}, **R)
    ok("geometry_rejects_negative", r.status_code == 400, str(r.status_code))

    # confirm -> 5x4=20 cells
    r = CS.post(f"{BASE}/api/author/elevations/{draft}/confirm", **R)
    ok("confirm_200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    ok("confirm_counts", r.status_code == 200 and r.json()["data"]["drops"] == 5
       and r.json()["data"]["cells"] == 20, r.text[:120])

    # parity: the confirmed elevation through the SAME endpoint 890 uses
    r = CS.get(f"{BASE}/api/elevation/{draft}", **R)
    d = r.json().get("data") or {}
    ok("authored_get_200", r.status_code == 200)
    ok("authored_payload_anatomy",
       {"geometry", "drops", "cells", "statuses", "audience", "face_label",
        "status"} <= set(d.keys()), str(sorted(d.keys()))[:200])
    ok("authored_status_confirmed", d.get("status") == "confirmed")
    ok("authored_geometry_shape",
       bool(d.get("geometry", {}).get("facade")) and
       d["geometry"].get("features") == {} and
       d["geometry"].get("authored") is True and
       len(d["geometry"].get("levels") or []) == 4 and
       len(d["geometry"].get("bounds") or []) == 6)
    ok("authored_drops_cells", len(d.get("drops") or []) == 5 and
       len(d.get("cells") or []) == 20)
    ok("authored_status_vocab", set(d.get("statuses") or {}) ==
       {"not_started", "in_progress", "on_hold", "rework", "complete"})
    ok("authored_derived_not_started",
       all(x.get("derived_status") == "not_started" for x in d.get("drops") or []))
    lv_ids = sorted({c["level_id"] for c in d.get("cells") or []})
    ok("authored_level_ids_padded", lv_ids == ["L01", "L02", "L03", "L04"], str(lv_ids))

    # external view of the CONFIRMED elevation (identical-view doctrine holds)
    ra = AR.get(f"{BASE}/api/elevation/{draft}", **R)
    ok("confirmed_arch_by_id_200", ra.status_code == 200, str(ra.status_code))
    if ra.status_code == 200:
        ok("confirmed_arch_no_internal_note",
           "internal_note" not in json.dumps(ra.json()))
    la = AR.get(f"{BASE}/api/elevations", **R).json().get("data") or []
    ok("confirmed_in_arch_picker", any(e.get("id") == draft for e in la))

    # grid locks on confirm
    r = CS.put(f"{BASE}/api/author/elevations/{draft}/geometry",
               json={"cols": [1, 1], "rows": [1, 1]}, **R)
    ok("confirmed_geometry_409", r.status_code == 409, str(r.status_code))
    r = CS.post(f"{BASE}/api/author/elevations/{draft}/confirm", **R)
    ok("confirm_twice_409", r.status_code == 409, str(r.status_code))
    r = CS.delete(f"{BASE}/api/author/elevations/{draft}", **R)
    ok("confirmed_delete_409", r.status_code == 409, str(r.status_code))

    # paint an authored cell — the session-2 surface speaks the same language
    cell0 = (d.get("cells") or [{}])[0].get("id")
    r = CS.post(f"{BASE}/api/elevation/cell",
                json={"cell_id": cell0, "status_key": "in_progress"}, **R)
    ok("authored_cell_paint_200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    r = CS.get(f"{BASE}/api/elevation/{draft}", **R)
    dd = r.json()["data"]
    painted = next((x for x in dd["drops"]
                    if any(c["drop_id"] == x["id"] and c["status"] == "in_progress"
                           for c in dd["cells"])), None)
    ok("authored_derivation_live",
       painted is not None and painted["derived_status"] == "in_progress")

    # draft delete path
    r = CS.post(f"{BASE}/api/author/elevations",
                json={"project_code": PA, "face_label": "Northwest"}, **R)
    d2 = r.json()["data"]["id"]
    r = CS.delete(f"{BASE}/api/author/elevations/{d2}", **R)
    ok("draft_delete_200", r.status_code == 200, str(r.status_code))
    ctx = CS.get(f"{BASE}/api/author/context", **R).json()["data"]
    pa = next((p for p in ctx["projects"] if p["code"] == PA), {"elevations": []})
    ok("draft_gone_from_context", all(e["id"] != d2 for e in pa["elevations"]))

    # ================= tall building: 12 bays x 80 floors ===================
    print("\n-- tall building: 12 x 80 -> 960 cells --")
    r = CS.post(f"{BASE}/api/author/elevations",
                json={"project_code": PA, "face_label": "North Tower"}, **R)
    tall = r.json()["data"]["id"]
    r = CS.put(f"{BASE}/api/author/elevations/{tall}/geometry",
               json={"cols": [1] * 12, "rows": [1.4] + [1] * 79}, **R)
    ok("tall_geometry_200", r.status_code == 200 and r.json()["data"]["floors"] == 80)
    r = CS.post(f"{BASE}/api/author/elevations/{tall}/confirm", **R)
    ok("tall_confirm_960_cells", r.status_code == 200 and
       r.json()["data"]["cells"] == 960, r.text[:120])
    t0 = time.time()
    r = CS.get(f"{BASE}/api/elevation/{tall}", **R)
    dt = time.time() - t0
    td = r.json().get("data") or {}
    ok("tall_get_200", r.status_code == 200)
    ok("tall_cells_960", len(td.get("cells") or []) == 960)
    tl = sorted({c["level_id"] for c in td.get("cells") or []})
    ok("tall_level_ids_sort_safe", tl[0] == "L01" and tl[-1] == "L80" and len(tl) == 80,
       f"{tl[0]}..{tl[-1]} n={len(tl)}")
    kb = len(r.content) / 1024.0
    print(f"    tall payload: {kb:.0f} KB in {dt:.2f}s (recorded for the budget note)")
    ok("tall_payload_sane", kb < 600, f"{kb:.0f} KB")

    # ================= /api/elevations listing shape ========================
    print("\n-- listing: real rows + canonical placeholders --")
    li = CS.get(f"{BASE}/api/elevations", **R).json()["data"]
    mine = [e for e in li if e["project_code"] == PA]
    ok("listing_has_authored_rows",
       any(e.get("id") == draft and e.get("label") == "Southeast" for e in mine) and
       any(e.get("id") == tall for e in mine))
    # placeholders exist for exactly the canonical faces with NO real row —
    # "North Tower" legitimately claims the N slot (face_code -> N), so N must
    # NOT placeholder here while S/E/W/ALL must.
    real_faces = {e["face"] for e in mine if e["id"] is not None}
    canon_faces = {e["face"] for e in mine if e["id"] is None}
    want_ph = {"N", "S", "E", "W", "ALL"} - real_faces
    ok("listing_placeholders_present", canon_faces == want_ph,
       f"got={sorted(canon_faces)} want={sorted(want_ph)}")
    ok("listing_rows_carry_status",
       all("status" in e for e in mine))

    # ================= 890 canary through the full API ======================
    print("\n-- 890 North backfill through the live API path --")
    e890 = next((e for e in li if e["project_code"] == "FR-BX-001" and
                 e["face"] == "N" and e.get("id")), None)
    if e890 is None:
        skip("bf890_api", "no FR-BX-001 North on this snapshot")
    else:
        ok("bf890_label_status", e890.get("label") == "North" and
           e890.get("status") == "confirmed", str(e890)[:160])
        r = CS.get(f"{BASE}/api/elevation/{e890['id']}", **R)
        d8 = r.json().get("data") or {}
        ok("bf890_serves_200", r.status_code == 200)
        ok("bf890_12_drops_60_cells", len(d8.get("drops") or []) == 12 and
           len(d8.get("cells") or []) == 60,
           f"drops={len(d8.get('drops') or [])} cells={len(d8.get('cells') or [])}")
        ok("bf890_geometry_untouched",
           abs((d8.get("geometry", {}).get("facade", {}).get("width_ft") or 0) - 265.0) < 0.01)
        ok("bf890_not_authored_flagged", not d8.get("geometry", {}).get("authored"))

    # ================= isolation ============================================
    print("\n-- isolation: pm scoped to A; architect + client contained --")
    r = CS.post(f"{BASE}/api/projects/{PB}/drawing-sets",
                files={"file": ("b.pdf", make_raster_pdf(), "application/pdf")}, timeout=60)
    bset = r.json()["data"] if r.status_code == 201 else None
    ok("seed_b_set", bset is not None, f"{r.status_code}")
    r = CS.post(f"{BASE}/api/author/elevations",
                json={"project_code": PB, "face_label": "North"}, **R)
    bdraft = r.json()["data"]["id"] if r.status_code == 201 else None
    ok("seed_b_draft", bdraft is not None)

    bad = []
    checks = [
        ("upload_B", lambda: PM.post(f"{BASE}/api/projects/{PB}/drawing-sets",
                                     files={"file": ("x.pdf", make_raster_pdf(),
                                                     "application/pdf")}, timeout=60)),
        ("list_B", lambda: PM.get(f"{BASE}/api/projects/{PB}/drawing-sets", **R)),
        ("pdf_B", lambda: PM.get(f"{BASE}/api/drawing-sets/{bset['id']}/pdf", **R)),
        ("propose_B", lambda: PM.post(f"{BASE}/api/drawing-sets/{bset['id']}/propose",
                                      json={"page_no": 1}, **R)),
        ("thumb_B", lambda: PM.get(
            f"{BASE}/api/drawing-sheets/{bset['sheets'][0]['id']}/thumb", **R)),
        ("patch_B", lambda: PM.patch(
            f"{BASE}/api/drawing-sheets/{bset['sheets'][0]['id']}",
            json={"label": "x"}, **R)),
        ("del_set_B", lambda: PM.delete(f"{BASE}/api/drawing-sets/{bset['id']}", **R)),
        ("create_B", lambda: PM.post(f"{BASE}/api/author/elevations",
                                     json={"project_code": PB, "face_label": "N"}, **R)),
        ("get_draft_B", lambda: PM.get(f"{BASE}/api/author/elevations/{bdraft}", **R)),
        ("geom_B", lambda: PM.put(f"{BASE}/api/author/elevations/{bdraft}/geometry",
                                  json={"cols": [1], "rows": [1]}, **R)),
        ("confirm_B", lambda: PM.post(f"{BASE}/api/author/elevations/{bdraft}/confirm", **R)),
        ("delete_B", lambda: PM.delete(f"{BASE}/api/author/elevations/{bdraft}", **R)),
    ]
    for name, fn in checks:
        rr = fn()
        if rr.status_code != 403:
            bad.append(f"{name}:{rr.status_code}")
    ok("pm_cross_project_all_403", not bad, "; ".join(bad))
    r = PM.get(f"{BASE}/api/projects/{PA}/drawing-sets", **R)
    ok("pm_own_project_200", r.status_code == 200, str(r.status_code))

    # architect containment: page redirects away, APIs closed
    r = AR.get(f"{BASE}/drawing-author", **R)
    ok("arch_author_page_redirected", r.status_code in (301, 302, 303) and
       "/drawing-author" not in (r.headers.get("Location") or ""),
       f"{r.status_code} -> {r.headers.get('Location')}")
    bad = []
    for name, url, method, kwargs in [
        ("ctx", "/api/author/context", "get", {}),
        ("sets", f"/api/projects/{PA}/drawing-sets", "get", {}),
        ("create", "/api/author/elevations", "post",
         {"json": {"project_code": PA, "face_label": "N"}}),
        ("thumb", f"/api/drawing-sheets/{src_sheet}/thumb", "get", {}),
    ]:
        rr = getattr(AR, method)(f"{BASE}{url}", **{**R, **kwargs})
        if rr.status_code != 403:
            bad.append(f"{name}:{rr.status_code}")
    ok("arch_author_apis_403", not bad, "; ".join(bad))

    # client (drawing grant): the author surface simply does not exist for them
    r = CL.get(f"{BASE}/drawing-author", **R)
    ok("client_author_page_blocked", r.status_code != 200, str(r.status_code))
    bad = []
    for name, url in [("ctx", "/api/author/context"),
                      ("sets", f"/api/projects/{PA}/drawing-sets"),
                      ("thumb", f"/api/drawing-sheets/{src_sheet}/thumb")]:
        rr = CL.get(f"{BASE}{url}", **R)
        if rr.status_code != 403:
            bad.append(f"{name}:{rr.status_code}")
    ok("client_author_apis_403", not bad, "; ".join(bad))

    # no session at all
    r = requests.get(f"{BASE}/drawing-author", timeout=20, allow_redirects=False)
    ok("anon_author_page_login_redirect", r.status_code in (301, 302, 303) and
       "login" in (r.headers.get("Location") or ""), f"{r.status_code}")
    r = requests.get(f"{BASE}/api/author/context", timeout=20)
    ok("anon_author_api_401", r.status_code == 401, str(r.status_code))

    # set delete: refused while referenced, allowed when clear
    if fixture_set:
        r = CS.delete(f"{BASE}/api/drawing-sets/{fixture_set['id']}", **R)
        ok("set_delete_refused_while_referenced", r.status_code == 409, str(r.status_code))
    r = CS.delete(f"{BASE}/api/author/elevations/{bdraft}", **R)
    r = CS.delete(f"{BASE}/api/drawing-sets/{bset['id']}", **R)
    ok("set_delete_when_clear_200", r.status_code == 200, str(r.status_code))


def run_degrade(users, sessions):
    """SRV2: no ANTHROPIC_API_KEY, no SSC_TRACE_FAKE — the propose endpoint
    degrades to a clean 503 and the MANUAL path still works end to end."""
    CS = sess(sessions, "csuite")
    R = dict(timeout=30, allow_redirects=False)
    print("\n-- degrade server: no key, no fake --")
    r = upload_pdf(CS, PA, make_raster_pdf(), "degrade.pdf")
    ok("degrade_upload_201", r.status_code == 201, f"{r.status_code}")
    dset = r.json()["data"]
    r = CS.post(f"{BASE}/api/drawing-sets/{dset['id']}/propose",
                json={"page_no": 1}, **R)
    ok("degrade_propose_503", r.status_code == 503, str(r.status_code))
    ok("degrade_ai_available_false", r.status_code == 503 and
       r.json().get("ai_available") is False, r.text[:120])
    # the manual path is fully unaffected — the whole flow, no AI anywhere
    r = CS.post(f"{BASE}/api/author/elevations",
                json={"project_code": PA, "face_label": "West",
                      "source_sheet_id": dset["sheets"][0]["id"]}, **R)
    did = r.json()["data"]["id"]
    r = CS.put(f"{BASE}/api/author/elevations/{did}/geometry",
               json={"cols": [1, 1, 1], "rows": [1, 1]}, **R)
    ok("degrade_manual_geometry_200", r.status_code == 200, str(r.status_code))
    r = CS.post(f"{BASE}/api/author/elevations/{did}/confirm", **R)
    ok("degrade_manual_confirm_6_cells", r.status_code == 200 and
       r.json()["data"]["cells"] == 6, r.text[:120])
    r = CS.delete(f"{BASE}/api/drawing-sets/{dset['id']}", **R)
    ok("degrade_set_cleanup", r.status_code in (200, 409), str(r.status_code))


def main() -> int:
    if not (os.environ.get("SSC_DB_URL") or "").strip():
        print("REFUSING TO RUN: SSC_DB_URL is unset (this suite seeds users/projects).")
        return 2
    print(f"#293 smoke_drawing_author: target={BASE}  fixture={'YES' if FIXTURE.exists() else 'NO'}")
    SRV_LOG.write_text("", encoding="utf-8")
    kill_port(PORT)
    users, sessions = seed()
    FAKE_JSON.write_text(json.dumps(FAKE_TRACE), encoding="utf-8")
    proc = None
    try:
        proc = start_server({"SSC_TRACE_FAKE": str(FAKE_JSON)})
        run_main(users, sessions)
        stop_server(proc)
        proc = None
        kill_port(PORT)
        proc = start_server({})     # no key, no fake
        run_degrade(users, sessions)
        return 0 if not FAIL else 1
    finally:
        stop_server(proc)
        kill_port(PORT)
        try:
            FAKE_JSON.unlink()
        except OSError:
            pass
        cleanup()
        print(f"\n== {len(PASS)} PASS / {len(FAIL)} FAIL / {len(SKIP)} SKIP ==")
        for f in FAIL:
            print(f"  FAILED: {f}")


if __name__ == "__main__":
    sys.exit(main())
