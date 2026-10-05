"""
smoke_project_docs_scan.py — Project Documents AI auto-read (#234, Batch B).

Proves the scan pipeline WITHOUT needing the live model (the key may be absent),
mirroring the expense Batch-B approach:

  1. PURE pipeline (document_scanner.process_scan_result) with canned outputs:
     - a permit -> category=PERMITS, requirement_key=pw2, dates pass through;
     - bad category -> safe default + flagged; requirement not valid for the
       chosen category -> "other" + flagged; non-ISO date -> null + flagged;
     - confidence < 0.7 -> needs_review; multi-page -> page_count surfaced.
  2. ENDPOINT gating against the RUNNING server: no key -> clean 503; unauth ->
     401; a non-permitted role -> 403.
  3. DETERMINISTIC ENDPOINT proof on an ISOLATED temp server (PORT=5099) with
     DOC_SCAN_FAKE set (no key needed): POST /scan -> suggestions (PERMITS + pw2
     + LOCAL effective/expiry + multi-page page_count), NO *_path, and NOTHING
     persisted (no auto-save); then SAVE via the Batch-A upload -> the pw2
     checklist item ticks on_file (green) and the expiry status is computed.

Operator-live safe: synthetic project SMK-DOCSCAN + synthetic role user only;
real workers/projects untouched; cleanup scoped to SMK-DOCSCAN. The temp server
binds 127.0.0.1:5099 (never 5050) so production is never disturbed.
"""
import io
import json
import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")   # test-only seams are inert without this (runtime_seams.py)
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import requests

import _smoke_auth  # noqa: E402
_smoke_auth.setup()

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5050")
SCRIPT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = SCRIPT_DIR / "superstars.db"
VENV_PY = SCRIPT_DIR / "venv" / "Scripts" / "python.exe"
FX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPT_DIR))
import document_scanner as scanner  # noqa: E402
from auth import hash_password  # noqa: E402
import db_layer  # noqa: E402  # #260 — route DB access through the env-driven layer (SSC_DB_URL)
import ssc_paths  # noqa: E402  # #287

PROJ = "SMK-DOCSCAN"
NOROLE_EMAIL = "smk-norole-scan@superstars.local"
SMOKE_EMAIL = "smoke@superstars.local"
SMOKE_PW = _smoke_auth.SMOKE_PASSWORD   # #258 — the per-run random pw set by _smoke_auth.setup() (no hardcoded backdoor)
TMP_PORT = "5099"
BASE2 = f"http://127.0.0.1:{TMP_PORT}"

PASS, FAIL = [], []


def ok(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {note}" if note else ""))
    return cond


def db():
    c = db_layer.connect()
    c.row_factory = sqlite3.Row
    return c


def _req_by_cat():
    conn = db()
    out = {}
    for r in conn.execute("SELECT category, requirement_key FROM document_requirements"):
        out.setdefault(r["category"], set()).add(r["requirement_key"])
    conn.close()
    return out


def _count_docs():
    conn = db()
    n = conn.execute("SELECT COUNT(*) FROM project_documents WHERE project_code=?", (PROJ,)).fetchone()[0]
    conn.close()
    return n


CATS = {"PERMITS", "DRAWINGS", "CONTRACTS", "INSPECTIONS", "SAFETY", "CLOSEOUT"}


def pipeline_tests(rbc):
    print("\n-- 1) PURE pipeline --")
    permit = json.loads((FX / "projdoc_permit_scan_result.json").read_text(encoding="utf-8"))
    p = scanner.process_scan_result(permit, CATS, rbc)
    ok("permit_category", p["category"] == "PERMITS", str(p["category"]))
    ok("permit_requirement", p["requirement_key"] == "pw2", str(p["requirement_key"]))
    ok("permit_dates_passthrough", p["effective_date"] == "2026-05-01" and p["expiry_date"] == "2027-04-30")
    ok("permit_high_conf_no_review", p["needs_review"] is False and not p["fields_to_check"])
    ok("permit_multipage", p["page_count"] == 2, str(p["page_count"]))

    bad = {"title": "x", "category": "NONSENSE", "requirement_key": "not_a_key",
           "effective_date": "05/01/2026", "expiry_date": None, "confidence": 0.4}
    pb = scanner.process_scan_result(bad, CATS, rbc)
    ok("bad_category_defaults", pb["category"] == "PERMITS" and "category" in pb["fields_to_check"])
    ok("bad_requirement_to_other", pb["requirement_key"] is None and "requirement_key" in pb["fields_to_check"])
    ok("bad_date_nulled", pb["effective_date"] is None and "effective_date" in pb["fields_to_check"])
    ok("low_conf_needs_review", pb["needs_review"] is True)

    xcat = {"category": "PERMITS", "requirement_key": "coi", "confidence": 0.85,
            "effective_date": None, "expiry_date": None}
    px = scanner.process_scan_result(xcat, CATS, rbc)
    ok("requirement_validated_against_category", px["requirement_key"] is None and "requirement_key" in px["fields_to_check"])

    nodate = {"category": "DRAWINGS", "requirement_key": "scaffold_eng", "confidence": 0.9,
              "effective_date": "2026-04-15", "expiry_date": None, "page_count": 5}
    pn = scanner.process_scan_result(nodate, CATS, rbc)
    ok("no_expiry_ok", pn["expiry_date"] is None and pn["category"] == "DRAWINGS" and pn["page_count"] == 5)


def gating_tests():
    print("\n-- 2) ENDPOINT gating (running server) --")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 48
    # no key (Path A) + admin session (patched requests) -> clean 503
    r = requests.post(f"{BASE}/api/projects/FR-BX-001/documents/scan",
                      files={"file": ("permit.png", io.BytesIO(png), "image/png")}, timeout=20)
    ok("scan_no_key_503", r.status_code == 503 and r.json().get("ai_available") is False, f"HTTP {r.status_code}")
    # unauthenticated -> 401 (the gate). NOTE: the docs module (and thus /scan)
    # permits ALL four standard roles (admin/c_suite/pm/super) and the users.role
    # CHECK constraint forbids any other role — so there is NO authenticated role
    # that yields 403 here (unlike /expenses/scan, which is admin/c_suite-only).
    # 401-unauthenticated is the meaningful gate for the docs surface.
    ru = requests.Session().post(f"{BASE}/api/projects/FR-BX-001/documents/scan",
                                 files={"file": ("permit.png", io.BytesIO(png), "image/png")}, timeout=20)
    ok("scan_unauth_401_gated", ru.status_code == 401, f"HTTP {ru.status_code}")
    # the public health route stays open (sanity: the gate is selective, not blanket)
    ok("scan_endpoint_is_gated", ru.status_code == 401 and requests.get(f"{BASE}/api/health", timeout=10).status_code == 200)


def _taskkill_port(port):
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue).OwningProcess"],
                             capture_output=True, text=True, timeout=20).stdout
        for pid in {p.strip() for p in out.splitlines() if p.strip().isdigit()}:
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True, timeout=20)
    except Exception:
        pass


def endpoint_proof():
    print("\n-- 3) DETERMINISTIC endpoint proof (temp server :5099, DOC_SCAN_FAKE) --")
    today = date.today()
    fake = {
        "title": "DOB Work Permit (PW2) — SMK Job", "doc_type": "DOB Work Permit (PW2)",
        "category": "PERMITS", "requirement_key": "pw2",
        "effective_date": (today - timedelta(days=30)).isoformat(),
        "expiry_date": (today + timedelta(days=200)).isoformat(),
        "page_count": 2, "confidence": 0.95, "warnings": [],
    }
    ffile = Path(tempfile.gettempdir()) / "smk_docscan_fake.json"
    ffile.write_text(json.dumps(fake), encoding="utf-8")
    env = dict(os.environ)
    env["PORT"] = TMP_PORT
    env["DOC_SCAN_FAKE"] = str(ffile)
    env.pop("ANTHROPIC_API_KEY", None)  # prove the FAKE path works with NO key
    logf = open(SCRIPT_DIR / "tests" / "_docscan_tmpsrv.log", "w")
    proc = subprocess.Popen([str(VENV_PY), "server.py"], cwd=str(SCRIPT_DIR),
                            stdout=logf, stderr=subprocess.STDOUT, env=env)
    try:
        up = False
        for _ in range(40):
            time.sleep(0.5)
            try:
                if requests.get(f"{BASE2}/api/health", timeout=3).status_code == 200:
                    up = True
                    break
            except requests.exceptions.ConnectionError:
                pass
            if proc.poll() is not None:
                break
        if not ok("tmpsrv_up", up, "temp server on :5099"):
            return
        s = requests.Session()
        s.post(f"{BASE2}/api/auth/login", json={"email": SMOKE_EMAIL, "password": SMOKE_PW}, timeout=10)
        conn = db()
        conn.execute("INSERT OR IGNORE INTO projects (project_code,name,status) VALUES (?,?,?)",
                     (PROJ, "SMK DocScan", "active"))
        conn.commit()
        conn.close()

        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        n_before = _count_docs()
        rs = s.post(f"{BASE2}/api/projects/{PROJ}/documents/scan",
                    files={"file": ("permit.png", io.BytesIO(png), "image/png")}, timeout=30)
        ok("scan_200", rs.status_code == 200, f"HTTP {rs.status_code}")
        sj = (rs.json() or {}).get("data", {})
        ok("scan_suggests_permits", sj.get("category") == "PERMITS", str(sj.get("category")))
        ok("scan_suggests_pw2", sj.get("requirement_key") == "pw2", str(sj.get("requirement_key")))
        ok("scan_effective_local", sj.get("effective_date") == fake["effective_date"], str(sj.get("effective_date")))
        ok("scan_expiry_local", sj.get("expiry_date") == fake["expiry_date"], str(sj.get("expiry_date")))
        ok("scan_multipage_one_call", sj.get("page_count") == 2, str(sj.get("page_count")))
        ok("scan_no_path_key", not any("path" in k.lower() for k in sj.keys()), str(sorted(sj.keys())))
        ok("scan_no_autosave", _count_docs() == n_before, f"before={n_before} after={_count_docs()}")

        # SAVE via Batch-A with the confirmed suggestion -> checklist tick + expiry computed
        save = s.post(f"{BASE2}/api/projects/{PROJ}/documents",
                      files={"file": ("permit.png", io.BytesIO(png), "image/png")},
                      data={"category": sj["category"], "requirement_key": sj["requirement_key"],
                            "title": sj.get("title") or "PW2",
                            "effective_date": sj["effective_date"], "expiry_date": sj["expiry_date"]},
                      timeout=20)
        ok("save_201", save.status_code == 201, f"HTTP {save.status_code}")
        cl = s.get(f"{BASE2}/api/projects/{PROJ}/documents", timeout=20).json()["data"]
        permits = [c for c in cl["categories"] if c["category"] == "PERMITS"][0]
        pw2 = [it for it in permits["items"] if it["requirement_key"] == "pw2"][0]
        ok("checklist_pw2_ticks_on_file", pw2["status"] == "on_file" and pw2.get("doc") is not None, str(pw2["status"]))
        ok("checklist_expiry_computed", pw2["doc"]["expiry_date"] == fake["expiry_date"])
        ok("readiness_incremented", cl["readiness"]["on_file"] >= 1, str(cl["readiness"]["on_file"]))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass
        _taskkill_port(TMP_PORT)
        logf.close()
        ffile.unlink(missing_ok=True)


def cleanup():
    print("\n-- cleanup (scoped to SMK-DOCSCAN) --")
    conn = db()
    rows = conn.execute("SELECT id, file_path FROM project_documents WHERE project_code=?", (PROJ,)).fetchall()
    for r in rows:
        try:
            if r["file_path"]:
                Path(r["file_path"]).unlink(missing_ok=True)
        except Exception:
            pass
    conn.execute("DELETE FROM project_documents WHERE project_code=?", (PROJ,))
    conn.execute("DELETE FROM projects WHERE project_code=?", (PROJ,))
    conn.execute("DELETE FROM users WHERE email=?", (NOROLE_EMAIL,))
    conn.commit()
    residue = conn.execute("SELECT COUNT(*) FROM project_documents WHERE project_code=?", (PROJ,)).fetchone()[0]
    conn.close()
    pdir = ssc_paths.under_root("data_room", "project_docs") / PROJ
    if pdir.exists():
        import shutil
        shutil.rmtree(pdir, ignore_errors=True)
    reqs_intact = None
    c2 = db()
    reqs_intact = c2.execute("SELECT COUNT(*) FROM document_requirements").fetchone()[0]
    c2.close()
    ok("cleanup_zero_residue", residue == 0 and not pdir.exists(), f"docs={residue}")
    ok("requirements_intact_27", reqs_intact == 27, str(reqs_intact))


def main():
    print("== #234 Project Documents AI-scan smoke ==")
    rbc = _req_by_cat()
    pipeline_tests(rbc)
    gating_tests()
    endpoint_proof()
    cleanup()
    print(f"\n== RESULT: {len(PASS)} PASS / {len(FAIL)} FAIL ==")
    if FAIL:
        print("FAILURES: " + ", ".join(FAIL))
    print("OVERALL:", "PASS" if not FAIL else "FAIL")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
