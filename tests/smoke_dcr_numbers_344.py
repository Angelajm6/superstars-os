#!/usr/bin/env python3
"""#344 — DCR numbers: the register, the number preview, the delete fix, and the
pinned FR-BX-001 109 → 097 correction. Behavioral guard.

Boots the real app (`import server`) on an ISOLATED database and a temp data root,
and builds the production scenario: real reports at 096 and 109, future-dated junk
at 097–100, numbers 101–108 unused. Proves:
  * number preview: a new date shows the next number; a dated day shows its own
  * list: the project-wide total rides in meta; a row with a PDF carries pdf_url
  * register (company only; pm 403): gaps, future-dated and out-of-order are flagged
  * delete: rows + BOTH HTML + the PDF leave the number's folder (folder removed),
    the files land in the non-served backup, an audit row records the rows
  * correction (admin only): refuses while 097 is taken; after the junk is deleted,
    refuses a stale plan_hash, then moves 109 → 097 (both rows' report_id +
    dcr_sequence, the folder, the number inside both HTML files; no PDF still
    saying 109), keeps the original in the backup, audits it, refuses a second run;
    the next new report is 098
Run:  venv/bin/python tests/smoke_dcr_numbers_344.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_dcrn_344_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import dcr_maintenance  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "dcrn344"
PW = "DcrNumbersSmoke2026"
P = dcr_maintenance.FIX_PROJECT          # the pinned project
REAL_96, REAL_109 = "2026-09-18", "2026-09-22"
JUNK = {97: "2099-01-01", 98: "2099-01-02", 99: "2099-01-03", 100: "2099-01-04"}
_created_project = False


def _db():
    return db_layer.connect(pragma_fk=False)


def _dir(seq):
    return DATA_ROOT / "data_room" / "reports" / "dcr" / P / f"{seq:03d}"


def _seed_dcr(conn, seq, day, pdf=True):
    for aud in ("internal", "client"):
        conn.execute(
            "INSERT INTO report_index (report_date, project_code, report_type, report_id, status, "
            "dcr_sequence, no_work, stale) VALUES (?, ?, 'DCR', ?, 'issued', ?, 0, 0)",
            (day, P, f"DCR-{P}-{seq:03d}-{aud}", seq))
    d = _dir(seq)
    d.mkdir(parents=True, exist_ok=True)
    for aud in ("internal", "client"):
        (d / f"{aud}.html").write_text(
            f"<html><div class='rid'>DCR-{P}-{seq:03d}</div><td>Report Number</td><td>DCR-{P}-{seq:03d}</td>"
            f"<p>not a match: DCR-{P}-{seq:03d}9</p></html>", encoding="utf-8")
    if pdf:
        (d / "internal.pdf").write_bytes(b"%PDF-stale-" + str(seq).encode())


def _seed():
    global _created_project
    conn = _db()
    try:
        if not conn.execute("SELECT 1 FROM projects WHERE project_code = ?", (P,)).fetchone():
            conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, 'Dcrn344', '1 Dcrn St', 'active')", (P,))
            _created_project = True
        conn.execute("DELETE FROM report_index WHERE project_code = ? AND report_type = 'DCR'", (P,))
        conn.execute("DELETE FROM audit_log WHERE action IN ('dcr_renumber_344', 'dcr_delete') AND target_id LIKE ?", (f"DCR-{P}-%",))
        ids = {}
        for role in ("admin", "c_suite", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Smoke {role} Dcrn"))
            ids[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        conn.execute("DELETE FROM pm_project_assignment WHERE user_id = ?", (ids["pm"],))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_at) VALUES (?, ?, '2026-09-01')", (ids["pm"], P))
        _seed_dcr(conn, 96, REAL_96)
        for seq, day in JUNK.items():
            _seed_dcr(conn, seq, day)
        _seed_dcr(conn, 109, REAL_109)
        conn.commit()
        return ids
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        conn.execute("DELETE FROM report_index WHERE project_code = ? AND report_type = 'DCR'", (P,))
        conn.execute("DELETE FROM audit_log WHERE action IN ('dcr_renumber_344', 'dcr_delete') AND target_id LIKE ?", (f"DCR-{P}-%",))
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        purge(conn, "pm_project_assignment", "user_id", uids)
        conn.commit()
        purge_users(conn, uids)
        if _created_project:
            conn.execute("DELETE FROM projects WHERE project_code = ?", (P,))
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
    admin, cs, pm = _login("admin"), _login("c_suite"), _login("pm")
    fix = "/api/admin/maintenance/dcr-renumber-344"

    # ---- number preview: before issuing, the operator sees the number
    d = pm.get(f"/api/projects/{P}/daily/2026-09-23/number").get_json()["data"]
    assert d["sequence"] == 110 and d["reissue"] is False and d["display_id"] == f"DCR-{P}-110", d
    d = pm.get(f"/api/projects/{P}/daily/{REAL_109}/number").get_json()["data"]
    assert d["sequence"] == 109 and d["reissue"] is True, d
    assert pm.get(f"/api/projects/{P}/daily/09-23-2026/number").status_code == 400
    passed += 1; print("  number preview ok")

    # ---- list: whole-project total, PDF link
    j = pm.get(f"/api/projects/{P}/reports?report_type=DCR&from_date=2026-09-20&to_date=2026-09-30").get_json()
    assert j["meta"]["count"] == 1 and j["meta"]["total_project"] == 6, j["meta"]
    row = j["data"][0]
    assert row["pdf_url"] == f"/project-files/data_room/reports/dcr/{P}/109/internal.pdf", row
    passed += 1; print("  list total + pdf ok")

    # ---- register: company only, flags the gaps / future / out-of-order
    assert pm.get(f"/api/projects/{P}/dcr-register").status_code == 403
    g = cs.get(f"/api/projects/{P}/dcr-register").get_json()["data"]
    a = g["anomalies"]
    assert g["total"] == 6 and g["max_sequence"] == 109 and g["next_sequence"] == 110, g
    assert a["future_dated"] == [97, 98, 99, 100], a
    assert a["missing"] == list(range(1, 96)) + list(range(101, 109)), a["missing_ranges"]
    assert a["missing_ranges"] == ["001–095", "101–108"], a["missing_ranges"]
    assert 109 in a["out_of_order"], a
    passed += 1; print("  register ok")

    # ---- correction: admin only; refuses while 097 is taken
    assert pm.get(fix).status_code == 403 and pm.post(fix, json={}).status_code == 403
    assert cs.get(fix).status_code == 200, "#347: c_suite can run the pinned fix"
    p = admin.get(fix).get_json()["data"]
    assert p["can_apply"] is False and p["target_taken"] is True and not p["done"], p
    assert any("already exists" in x for x in p["problems"]), p["problems"]
    assert [b["sequence"] for b in p["between"]] == [98, 99, 100], p["between"]
    r = admin.post(fix, json={"plan_hash": p["plan_hash"]})
    assert r.status_code == 409 and (_dir(109) / "internal.html").exists()
    passed += 1; print("  correction refuses while 097 is taken ok")

    # ---- delete the junk: rows, both HTML AND the PDF leave; folder gone; backup + audit
    for seq in JUNK:
        r = admin.delete(f"/api/projects/{P}/reports/by-sequence/{seq}")
        assert r.status_code == 200, (seq, r.status_code, r.get_json())
        body = r.get_json()
        assert sorted(body["files_deleted"]) == ["client.html", "internal.html", "internal.pdf"], body
        assert "backup" not in body, "no filesystem paths on the wire"
        assert not _dir(seq).exists(), f"{seq:03d}/ must be gone (the old delete left internal.pdf behind)"
    backups = list((DATA_ROOT / "data_room" / "_maintenance_backup").glob(f"*-dcr-delete-{P}-097"))
    assert len(backups) == 1 and (backups[0] / "internal.pdf").exists()
    conn = _db()
    try:
        n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'dcr_delete' AND target_id LIKE ?", (f"DCR-{P}-%",)).fetchone()[0]
        assert n == 4, n
        assert not conn.execute("SELECT 1 FROM report_index WHERE project_code = ? AND dcr_sequence BETWEEN 97 AND 100", (P,)).fetchone()
    finally:
        conn.close()
    assert admin.get(f"/project-files/data_room/_maintenance_backup/{backups[0].name}/internal.html").status_code == 404, \
        "maintenance backups must not be servable"
    passed += 1; print("  delete moves PDF + folder, audits ok")

    # ---- correction: stale hash refused; then applied; then refused again
    p = admin.get(fix).get_json()["data"]
    assert p["can_apply"] is True and p["target_taken"] is False and p["between"] == [], p
    assert p["id_hits"] == {"internal.html": 2, "client.html": 2}, p["id_hits"]
    r = admin.post(fix, json={"plan_hash": "0" * 16})
    assert r.status_code == 409 and (_dir(109) / "internal.html").exists() and not _dir(97).exists()
    r = admin.post(fix, json={"plan_hash": p["plan_hash"]})
    assert r.status_code == 200, r.get_json()
    res = r.get_json()["data"]
    assert res["to"] == f"DCR-{P}-097" and res["rows"] == 2, res
    assert res["number_replacements"] == {"internal.html": 2, "client.html": 2}, res
    assert not _dir(109).exists() and _dir(97).exists() and not _dir(97).with_name("097.tmp").exists()
    for aud in ("internal", "client"):
        html = (_dir(97) / f"{aud}.html").read_text(encoding="utf-8")
        assert f"DCR-{P}-097<" in html and f"DCR-{P}-109<" not in html, aud
        assert f"DCR-{P}-1099" in html, "the anchored pattern must not touch a longer number"
    pdf = _dir(97) / "internal.pdf"
    assert (not pdf.exists()) or (pdf.read_bytes() != b"%PDF-stale-109"), "no PDF may still say 109"
    kept = list((DATA_ROOT / "data_room" / "_maintenance_backup").glob(f"*-dcr-renumber-{P}-109-to-097"))
    assert len(kept) == 1 and f"DCR-{P}-109<" in (kept[0] / "internal.html").read_text(encoding="utf-8")
    conn = _db()
    try:
        rows = conn.execute("SELECT report_id, dcr_sequence, report_date FROM report_index "
                            "WHERE project_code = ? AND report_date = ? ORDER BY report_id", (P, REAL_109)).fetchall()
        assert [(r["report_id"], r["dcr_sequence"]) for r in rows] == [
            (f"DCR-{P}-097-client", 97), (f"DCR-{P}-097-internal", 97)], [dict(r) for r in rows]
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'dcr_renumber_344'").fetchone()[0] == 1
    finally:
        conn.close()
    p = admin.get(fix).get_json()["data"]
    assert p["done"] is True and p["can_apply"] is False, p
    assert admin.post(fix, json={"plan_hash": p["plan_hash"]}).status_code == 409
    passed += 1; print("  correction 109 -> 097 ok")

    # ---- after the fix, numbers follow on: the next new report is 098
    d = pm.get(f"/api/projects/{P}/daily/2026-09-23/number").get_json()["data"]
    assert d["sequence"] == 98 and d["reissue"] is False, d
    d = pm.get(f"/api/projects/{P}/daily/{REAL_109}/number").get_json()["data"]
    assert d["sequence"] == 97 and d["reissue"] is True, d
    passed += 1; print("  next number follows on ok")

    # ---- the served dashboard carries the new UI
    html = admin.get(f"/projects/{P}").get_data(as_text=True)
    for needle in ('id="dcr-number-preview"', 'id="dcr-numbering-banner"', 'id="dcr-all-numbers"', 'id="dcrg-overlay"',
                   "function refreshDcrNumber", "/dcr-register", "dcr-renumber-344", "json.meta.total_project",
                   "const issuedAtRaw = r.created_at || r.updated_at"):
        assert needle in html, needle
    passed += 1; print("  served page ok")

    print(f"PASS smoke_dcr_numbers_344 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    sys.exit(code)
