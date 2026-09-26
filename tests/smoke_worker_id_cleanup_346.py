#!/usr/bin/env python3
"""#346 — one-time worker ID cleanup (W-0030..32 -> W-0027..29). Behavioral guard.

Boots the real app on an ISOLATED database and a temp data root. The pinned map is
swapped for synthetic IDs (W-9030..32 -> W-9027..29) and a private audit action, so the
suite never meets real workers and can run on the shared CI Postgres. Proves:
  * refuses while the targets still exist; the test workers are then removed with the
    #345 delete, and the preview goes green with the exact counts
  * refuses when a saved DCR mentions a target ID, or an old ID inside another code
  * apply moves every W-keyed column (employees, labor state, rate history, field photos)
    and leaves E-keyed history (sign-ins) and issued card rows untouched
  * the saved DCR files get the new numbers (standalone IDs only), originals are kept in
    the maintenance backup, the stale PDF is set aside and regenerated in batches
  * the sign-in <-> DCR divergence report is identical before and after
  * wrong plan_hash / second apply refused; pm 403 (admin + c_suite allowed, #347); audit carries no names
  * a new hire given a freed number can be issued a CoF without a card_id collision
Run:  venv/bin/python tests/smoke_worker_id_cleanup_346.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_wid_346_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)
import cof_issuer  # noqa: E402
import pdf_export  # noqa: E402
import signin_dcr_reconcile  # noqa: E402
import worker_id_cleanup as wic  # noqa: E402

wic.MAP = (("W-9030", "W-9027"), ("W-9031", "W-9028"), ("W-9032", "W-9029"))
wic.ACTION = "worker_renumber_346_smoke"

TAG = "wid346"
PW = "WorkerIdCleanup2026"
P = "WID346-P"
D1 = (date.today() - timedelta(days=3)).isoformat()
D2 = (date.today() - timedelta(days=2)).isoformat()
NAME = "Wid346 Synthetic"
TESTS = (("E-93627", "W-9027"), ("E-93628", "W-9028"), ("E-93629", "W-9029"))   # empty test workers
REAL = (("E-93630", "W-9030"), ("E-93631", "W-9031"), ("E-93632", "W-9032"))    # move up, keep data
OTHER = ("E-93626", "W-9026")                                                    # untouched
HIRE = ("E-93640", "W-9030")                                                     # a later hire
ALL_E = [e for e, _ in TESTS + REAL] + [OTHER[0], HIRE[0]]
ALL_W = sorted({w for _, w in TESTS + REAL} | {OTHER[1]})
API = "/api/admin/maintenance/worker-renumber-346"


def _db():
    return db_layer.connect(pragma_fk=False)


def _dcr(seq):
    return DATA_ROOT / "data_room" / "reports" / "dcr" / P / f"{seq:03d}"


def _roster(ids):
    return "".join(f"<tr><td class='wid'>{w}</td><td>{NAME}</td></tr>" for w in ids)


def _purge_all(conn):
    for t, c, vals in (("cof_cards", "employee_id", ALL_E), ("field_photos", "project_code", [P]),
                       ("sign_in_log", "employee_id", ALL_E), ("project_assignments", "employee_id", ALL_E),
                       ("labor_rate_change", "worker_id", ALL_W), ("labor_rate_change", "employee_id", ALL_E),
                       ("labor_worker_state", "worker_id", ALL_W), ("labor_worker_state", "employee_id", ALL_E),
                       ("report_index", "project_code", [P]), ("audit_log", "action", [wic.ACTION]),
                       ("audit_log", "target_id", ALL_E), ("employees", "employee_id", ALL_E),
                       ("projects", "project_code", [P])):
        purge(conn, t, c, vals)


def _seed():
    conn = _db()
    try:
        _purge_all(conn)
        conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, 'Wid346', '1 Wid St', 'active')", (P,))
        for eid, wid in TESTS + REAL + (OTHER,):
            conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at) "
                         "VALUES (?, ?, ?, 'laborer', ?, ?)", (eid, wid, NAME, D1, D1))
            conn.execute("INSERT INTO labor_worker_state (worker_id, employee_id, trade, current_rate, status, "
                         "effective_date, created_at, updated_at) VALUES (?, ?, 'laborer', 35.0, 'active', ?, ?, ?)",
                         (wid, eid, D1, D1, D1))
            conn.execute("INSERT INTO labor_rate_change (worker_id, employee_id, old_rate, new_rate, effective_date, "
                         "status, is_initial, submitted_at) VALUES (?, ?, NULL, 35.0, ?, 'approved', 1, ?)",
                         (wid, eid, D1, D1))
        for eid, _ in REAL + (OTHER,):
            conn.execute("INSERT INTO sign_in_log (date, employee_id, project_code, time_in, time_out, created_at, "
                         "updated_at) VALUES (?, ?, ?, '07:00', '15:30', ?, ?)", (D1, eid, P, D1, D1))
        conn.execute("INSERT INTO field_photos (project_code, worker_id, uploaded_at, file_path, thumb_path) "
                     "VALUES (?, 'W-9031', ?, 'x/full.jpg', 'x/thumb.jpg')", (P, D1))
        conn.execute("INSERT INTO cof_cards (card_id, employee_id, issued_date, expires_date, issued_by, status, "
                     "card_number_display) VALUES ('SSC-COF-W-9030-1', 'E-93630', ?, '2099-01-01', 'Smoke', 'issued', "
                     "'SSC-COF-W-9030')", (D1,))
        for seq, day in ((1, D1), (2, D2)):
            for aud in ("internal", "client"):
                conn.execute("INSERT INTO report_index (report_date, project_code, report_type, report_id, status, "
                             "dcr_sequence, no_work, stale) VALUES (?, ?, 'DCR', ?, 'issued', ?, 0, 0)",
                             (day, P, f"DCR-{P}-{seq:03d}-{aud}", seq))
        ids = {}
        for role in ("admin", "c_suite", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Smoke {role} Wid346"))
        conn.commit()
    finally:
        conn.close()
    # 001 (D1): everyone who signed in; 002 (D2): W-9031 on the DCR with no sign-in -> one
    # standing divergence that must survive the renumber unchanged (as W-9028)
    d = _dcr(1)
    d.mkdir(parents=True, exist_ok=True)
    (d / "internal.html").write_text(f"<table>{_roster(['W-9026', 'W-9030', 'W-9031', 'W-9032'])}</table>"
                                     "<p>W-9 then W-903 are not IDs</p>", encoding="utf-8")
    (d / "client.html").write_text(f"<table>{_roster(['W-9030'])}</table>", encoding="utf-8")
    (d / "internal.pdf").write_bytes(b"%PDF-stale")
    d = _dcr(2)
    d.mkdir(parents=True, exist_ok=True)
    (d / "internal.html").write_text(f"<table>{_roster(['W-9031'])}</table>", encoding="utf-8")


def _cleanup():
    conn = _db()
    try:
        _purge_all(conn)
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


def _q(sql, args=()):
    conn = _db()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _divs():
    conn = _db()
    try:
        return sorted((d["date"], d["worker_id"], d["class"]) for d in signin_dcr_reconcile.compute_divergences(conn, P))
    finally:
        conn.close()


def main() -> int:
    _seed()
    passed = 0
    admin, cs, pm = _login("admin"), _login("c_suite"), _login("pm")

    # ---- company roles only (#347: admin + c_suite); pm refused
    assert pm.get(API).status_code == 403
    assert pm.post(API, json={}).status_code == 403
    assert pm.post(f"{API}/pdfs").status_code == 403
    assert cs.get(API).status_code == 200
    passed += 1
    print("  company roles only ok")

    # ---- refuses while the test workers exist; the #345 delete clears them
    p = admin.get(API).get_json()["data"]
    assert not p["can_apply"] and any("W-9027 still exists" in x for x in p["problems"]), p["problems"]
    assert admin.post(API, json={"plan_hash": p["plan_hash"]}).status_code == 409
    for eid, _ in TESTS:
        r = admin.delete(f"/api/company/portal/workers/{eid}")
        assert r.status_code == 200, (eid, r.status_code, r.get_json())
    p = admin.get(API).get_json()["data"]
    assert p["can_apply"], p["problems"]
    assert p["rows"]["employees"] == {"W-9030": 1, "W-9031": 1, "W-9032": 1}, p["rows"]
    assert sum(p["rows"]["labor_worker_state"].values()) == 3 and sum(p["rows"]["labor_rate_change"].values()) == 3
    assert p["rows"]["field_photos"] == {"W-9031": 1}
    assert p["dcr_files"] == 3 and p["dcr_reports"] == [f"{P}/001", f"{P}/002"] and p["pdfs_to_regenerate"] == 1, p
    assert [(c["worker_id"], c["printed"]) for c in p["cards_to_reissue"]] == [("W-9027", "SSC-COF-W-9030")]
    assert NAME not in json.dumps(p) and str(DATA_ROOT) not in json.dumps(p), "preview leaks a name or a path"
    passed += 1
    print("  guards + preview ok")

    # ---- ambiguous files refuse
    extra = _dcr(3)
    extra.mkdir(parents=True, exist_ok=True)
    (extra / "internal.html").write_text("<p>card SSC-COF-W-9031 on file</p>", encoding="utf-8")
    p2 = admin.get(API).get_json()["data"]
    assert not p2["can_apply"] and any("inside another code" in x for x in p2["problems"]), p2["problems"]
    (extra / "internal.html").write_text("<p>ref W-90311</p>", encoding="utf-8")   # the reconcile reads W-9031
    p2 = admin.get(API).get_json()["data"]
    assert not p2["can_apply"] and any("inside another code" in x for x in p2["problems"]), p2["problems"]
    (extra / "internal.html").write_text(f"<table>{_roster(['W-9028'])}</table>", encoding="utf-8")
    p2 = admin.get(API).get_json()["data"]
    assert not p2["can_apply"] and any("mention W-9028" in x for x in p2["problems"]), p2["problems"]
    shutil.rmtree(extra)
    passed += 1
    print("  ambiguity guards ok")

    # ---- apply
    before_divs = _divs()
    assert before_divs == [(D2, "W-9031", "in_dcr_not_log")], before_divs
    signins_before = _q("SELECT employee_id, date FROM sign_in_log WHERE project_code = ? ORDER BY employee_id", (P,))
    r = admin.post(API, json={"plan_hash": "stale"})
    assert r.status_code == 409 and "changed" in r.get_json()["error"]

    # a cross-check that disagrees puts EVERYTHING back (rows, files, PDF, no audit)
    real_divs, calls = wic._divergences, []
    wic._divergences = lambda conn: (calls.append(1), [] if len(calls) == 1 else [("x", "d", "W-1", "c", "r")])[1]
    try:
        r = admin.post(API, json={"plan_hash": p["plan_hash"]})
    finally:
        wic._divergences = real_divs
    assert r.status_code == 409 and "cross-check" in r.get_json()["error"], r.get_json()
    assert _q("SELECT worker_id FROM employees WHERE employee_id = 'E-93630'") == [{"worker_id": "W-9030"}]
    assert _q("SELECT worker_id FROM field_photos WHERE project_code = ?", (P,)) == [{"worker_id": "W-9031"}]
    assert "W-9030" in (_dcr(1) / "internal.html").read_text(encoding="utf-8")
    assert (_dcr(1) / "internal.pdf").read_bytes() == b"%PDF-stale"
    assert not list(_dcr(1).glob("*.tmp")) and not _q("SELECT 1 FROM audit_log WHERE action = ?", (wic.ACTION,))
    assert admin.get(API).get_json()["data"]["plan_hash"] == p["plan_hash"], "a refused apply must leave no trace"
    shutil.rmtree(DATA_ROOT / "data_room" / "_maintenance_backup")
    r = admin.post(API, json={"plan_hash": p["plan_hash"]})
    assert r.status_code == 200, r.get_json()
    res = r.get_json()["data"]
    assert res["dcr_files"] == 3 and res["pdfs_remaining"] == 1 and res["divergences_unchanged"] == 1, res
    emp = {e["employee_id"]: e["worker_id"] for e in _q(
        "SELECT employee_id, worker_id FROM employees WHERE employee_id IN (?,?,?,?)", ("E-93630", "E-93631", "E-93632", "E-93626"))}
    assert emp == {"E-93630": "W-9027", "E-93631": "W-9028", "E-93632": "W-9029", "E-93626": "W-9026"}, emp
    lws = {x["employee_id"]: x["worker_id"] for x in _q(
        "SELECT employee_id, worker_id FROM labor_worker_state WHERE employee_id LIKE 'E-936%'")}
    assert lws == emp, lws
    lrc = {x["employee_id"]: x["worker_id"] for x in _q(
        "SELECT employee_id, worker_id FROM labor_rate_change WHERE employee_id LIKE 'E-936%'")}
    assert lrc == emp, lrc
    assert _q("SELECT worker_id FROM field_photos WHERE project_code = ?", (P,)) == [{"worker_id": "W-9028"}]
    assert _q("SELECT employee_id, date FROM sign_in_log WHERE project_code = ? ORDER BY employee_id", (P,)) == signins_before
    assert _q("SELECT card_id, card_number_display, status FROM cof_cards WHERE employee_id = 'E-93630'") == [
        {"card_id": "SSC-COF-W-9030-1", "card_number_display": "SSC-COF-W-9030", "status": "issued"}]
    passed += 1
    print("  rows moved, history untouched ok")

    # ---- files: new numbers, standalone IDs only, originals backed up, stale PDF set aside
    t1 = (_dcr(1) / "internal.html").read_text(encoding="utf-8")
    assert all(w in t1 for w in ("W-9026", "W-9027", "W-9028", "W-9029", "W-903 are")), t1
    for old in ("W-9030<", "W-9031<", "W-9032<"):
        assert old not in t1, old
    assert "W-9027" in (_dcr(1) / "client.html").read_text(encoding="utf-8")
    assert "W-9028" in (_dcr(2) / "internal.html").read_text(encoding="utf-8")
    assert not (_dcr(1) / "internal.pdf").exists(), "a PDF printing the old IDs must not stay"
    assert not list(_dcr(1).glob("*.tmp")), "staging files left behind"
    backups = list((DATA_ROOT / "data_room" / "_maintenance_backup").glob("*-worker-renumber-346"))
    assert len(backups) == 1
    kept = backups[0] / P / "001"
    assert "W-9030" in (kept / "internal.html").read_text(encoding="utf-8") and (kept / "internal.pdf").exists()
    assert _divs() == [(D2, "W-9028", "in_dcr_not_log")], _divs()
    passed += 1
    print("  files rewritten + divergence identical ok")

    # ---- audit, second apply refused, PDF batches
    a = _q("SELECT target_type, before_json, after_json, note FROM audit_log WHERE action = ?", (wic.ACTION,))
    assert len(a) == 1 and a[0]["target_type"] == "worker"
    blob = json.dumps(a)
    assert NAME not in blob and str(DATA_ROOT) not in blob, "audit leaks a name or a path"
    p3 = admin.get(API).get_json()["data"]
    assert p3["done"] and not p3["can_apply"] and p3["pdfs_remaining"] == 1
    assert admin.post(API, json={"plan_hash": p3["plan_hash"]}).status_code == 409

    real = pdf_export.render_html_to_pdf
    pdf_export.render_html_to_pdf = lambda html, pdf, **k: {"ok": False, "error": "engine missing"}
    try:
        r = admin.post(f"{API}/pdfs").get_json()["data"]
        assert r["failed"] == 1 and r["remaining"] == 1 and r["error"] and "engine missing" not in r["error"], r
        pdf_export.render_html_to_pdf = lambda html, pdf, **k: (Path(pdf).write_bytes(b"%PDF-new"), {"ok": True})[1]
        r = admin.post(f"{API}/pdfs").get_json()["data"]
        assert r == {"made": 1, "failed": 0, "remaining": 0, "error": None}, r
    finally:
        pdf_export.render_html_to_pdf = real
    assert (_dcr(1) / "internal.pdf").read_bytes() == b"%PDF-new"
    assert admin.get(API).get_json()["data"]["pdfs_remaining"] == 0
    passed += 1
    print("  audit + once-only + PDF batches ok")

    # ---- a later hire given the freed number: CoF card_id does not collide
    conn = _db()
    try:
        conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, created_at, updated_at) "
                     "VALUES (?, ?, ?, 'laborer', ?, ?)", (HIRE[0], HIRE[1], NAME, D1, D1))
        conn.commit()
        assert cof_issuer._next_cof_revision(conn, HIRE[0], "SSC-COF-W-9030") == 2
        assert cof_issuer._next_cof_revision(conn, "E-93630", "SSC-COF-W-9027") == 2
        assert cof_issuer._next_cof_revision(conn, HIRE[0]) == 1        # legacy call shape still works
    finally:
        conn.close()
    passed += 1
    print("  CoF revision ok")

    # ---- the page carries the admin box
    page = admin.get("/company").get_data(as_text=True)
    assert 'id="wf-fix346"' in page and "worker-renumber-346" in page
    passed += 1
    print("  served page ok")

    print(f"PASS smoke_worker_id_cleanup_346 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_worker_id_cleanup_346: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
