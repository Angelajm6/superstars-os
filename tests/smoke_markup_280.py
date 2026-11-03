"""#283 GUARD for the #280 drawing-markup module (shipped without a suite).

What it proves, per the fold-in audit's gap inventory:
  SCOPING     every elevation/comment/RFI endpoint is project-scoped through the
              TARGET row (cross-project by-id -> 403; unknown id -> 404), for
              client AND architect.
  GRANTS      (#283 wiring) client access is per-section, re-derived per request:
              'drawing' gates the page + elevation APIs + drop/photo comment
              threads; 'rfis' gates RFI reads and RFI comment threads. A client
              holding neither — or zero grants (#267) — gets nothing.
  VOCABULARY  /api/elevation/<id> serves the INTERNAL key set to internal roles
              and the client_key set to external ones; internal_note is absent
              from every external payload BY KEY (asserted recursively, with a
              planted-leak self-test proving the detector detects).
  WRITES      cell/drop status POSTs: internal-only (architect and client 403),
              transition validation, reason REQUIRED for on_hold/rework (400
              without), per-cell events on bulk drop paint.
  DERIVATION  a drop's work status is DERIVED from its cells (never stored) —
              five planted cell mixes must each produce the documented rollup.
  HISTORY     cell history serves reasons to everyone but names SSC staff to
              external viewers only as W-#### / 'SSC' (never kind='person').
  COMMENTS    client read-only (can_post:false, POST 403); architect posts 201;
              rate limit 429 on the 11th in-window comment; soft delete removes
              the body from every subsequent payload but keeps the row.
  RFIs        numeric allocation (planted unpadded 'RFI-9' -> next is RFI-010,
              the CLAUDE.md zero-padded-id rule); architect raises but cannot
              close (only internal); attach-later drop validation; client POST
              403.

Isolated backend REQUIRED: this suite seeds synthetic users/projects and will
refuse to run when SSC_DB_URL is unset (that would seed the live DB).
PII-safe: synthetic identities only; assertions print ids/keys/counts, never
names. 127.0.0.1 only.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import db_layer  # noqa: E402

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5050")
PA, PB = "SMK280-A", "SMK280-B"
PASS, FAIL = [], []
IDS = {"users": [], "projects": [PA, PB]}


def ok(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {note}" if note and not cond else ""))
    return bool(cond)


def forbidden_keys(payload, keys=("internal_note",)):
    """Recursively collect forbidden KEY names present anywhere in a JSON payload.
    Key-based on purpose: values can coincide, keys cannot."""
    hits = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in keys:
                    hits.append(f"{path}.{k}")
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(payload, "$")
    return hits


def seed():
    conn = db_layer.connect()
    try:
        for code in (PA, PB):
            conn.execute("DELETE FROM projects WHERE project_code=?", (code,))
            conn.execute("INSERT INTO projects (project_code, name, status) VALUES (?,?, 'active')",
                         (code, f"Smoke 280 {code[-1]}"))
        users = {}
        for key, role in (("csuite", "c_suite"), ("arch", "architect"),
                          ("cl_d", "client"),    # drawing + rfis + progress
                          ("cl_w", "client"),    # drawing only (+ progress)
                          ("cl_r", "client"),    # rfis only (+ progress)
                          ("cl_z", "client"),    # progress only — no markup access
                          ("cl_0", "client")):   # ZERO grants — #267 hard-stop
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                "must_reset_password, is_system) VALUES (?,?,?,?,1,'active',0,1)",
                (f"smk280-{key}@superstars.local", "x!unusable", role, f"SMK280 {key}"))
            users[key] = cur.lastrowid
            IDS["users"].append(cur.lastrowid)
        # bindings: clients + architect live on project A (client binding = assignment row)
        for key in ("arch", "cl_d", "cl_w", "cl_r", "cl_z", "cl_0"):
            conn.execute(
                "INSERT INTO pm_project_assignment (user_id, project_code, assigned_by, assigned_at) "
                "VALUES (?,?,?, '2026-07-28T00:00:00')", (users[key], PA, users["csuite"]))
        grants = {"cl_d": ("progress", "drawing", "rfis"),
                  "cl_w": ("progress", "drawing"),
                  "cl_r": ("progress", "rfis"),
                  "cl_z": ("progress",)}
        for key, secs in grants.items():
            for s in secs:
                conn.execute(
                    "INSERT INTO client_section_grant (user_id, project_code, section, "
                    "granted_by, granted_at) VALUES (?,?,?,?, '2026-07-28T00:00:00')",
                    (users[key], PA, s, users["csuite"]))
        # elevations: one per project; drops + cells with level_id TEXT
        elevs, drops, cells = {}, {}, {}
        for code, ek in ((PA, "eA"), (PB, "eB")):
            cur = conn.execute(
                "INSERT INTO elevation (project_code, face, name) VALUES (?, 'N', ?)",
                (code, f"North smoke {ek}"))
            elevs[ek] = cur.lastrowid
        for ek, dk, idx in (("eA", "dA1", 1), ("eA", "dA2", 2), ("eB", "dB1", 1)):
            cur = conn.execute(
                "INSERT INTO elevation_drop (elevation_id, idx, grid_from, grid_to) "
                "VALUES (?,?,?,?)", (elevs[ek], idx, f"G{idx}", f"G{idx + 1}"))
            drops[dk] = cur.lastrowid
        for dk, n in (("dA1", 3), ("dA2", 2), ("dB1", 1)):
            cells[dk] = []
            for i in range(1, n + 1):
                cur = conn.execute(
                    "INSERT INTO elevation_cell (drop_id, level_id, level_name, status_key) "
                    "VALUES (?,?,?, 'not_started')", (drops[dk], f"L{i}", f"Level {i}"))
                cells[dk].append(cur.lastrowid)
        sessions = {}
        for key, uid in users.items():
            tok = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions (id, user_id, expires_at, user_agent) "
                         "VALUES (?,?, '2099-01-01T00:00:00', 'smk280')", (tok, uid))
            sessions[key] = tok
        conn.commit()
        return users, sessions, elevs, drops, cells
    finally:
        conn.close()


def cleanup():
    conn = db_layer.connect()
    try:
        uids = IDS["users"]
        ph = ",".join("?" * len(uids)) if uids else "NULL"
        for sql, args in [
            (f"DELETE FROM comment WHERE project_code IN (?,?)", (PA, PB)),
            (f"DELETE FROM rfi WHERE project_code IN (?,?)", (PA, PB)),
            ("DELETE FROM elevation_cell_event WHERE cell_id IN (SELECT c.id FROM elevation_cell c "
             "JOIN elevation_drop d ON d.id=c.drop_id JOIN elevation e ON e.id=d.elevation_id "
             "WHERE e.project_code IN (?,?))", (PA, PB)),
            ("DELETE FROM elevation_cell WHERE drop_id IN (SELECT d.id FROM elevation_drop d "
             "JOIN elevation e ON e.id=d.elevation_id WHERE e.project_code IN (?,?))", (PA, PB)),
            ("DELETE FROM elevation_drop WHERE elevation_id IN (SELECT id FROM elevation "
             "WHERE project_code IN (?,?))", (PA, PB)),
            ("DELETE FROM elevation WHERE project_code IN (?,?)", (PA, PB)),
        ]:
            try:
                conn.execute(sql, args)
            except Exception as e:
                print(f"    [cleanup] {e}")
        if uids:
            for t, c in (("client_section_grant", "user_id"), ("pm_project_assignment", "user_id"),
                         ("sessions", "user_id"), ("login_audit", "user_id"),
                         ("audit_log", "actor_user_id"), ("role_change_audit", "user_id")):
                try:
                    conn.execute(f"DELETE FROM {t} WHERE {c} IN ({ph})", tuple(uids))
                except Exception as e:
                    print(f"    [cleanup] {t}: {e}")
            conn.execute(f"DELETE FROM users WHERE id IN ({ph})", tuple(uids))
        conn.execute("DELETE FROM projects WHERE project_code IN (?,?)", (PA, PB))
        conn.commit()
        print("  [cleanup] synthetic rows removed (scoped to SMK280 ids)")
    finally:
        conn.close()


def S(sessions, key):
    s = requests.Session()
    s.cookies.set("ssc_session", sessions[key])
    return s


def run():
    users, sessions, elevs, drops, cells = seed()
    R = dict(allow_redirects=False, timeout=20)
    CS, ARCH = S(sessions, "csuite"), S(sessions, "arch")
    D, W, RC, Z, Z0 = (S(sessions, k) for k in ("cl_d", "cl_w", "cl_r", "cl_z", "cl_0"))
    eA, eB = elevs["eA"], elevs["eB"]
    dA1, dA2, dB1 = drops["dA1"], drops["dA2"], drops["dB1"]

    print("\n-- detector self-test (planted leak MUST be caught) --")
    planted = {"data": {"cells": [{"id": 1, "internal_note": "leak"}]}}
    ok("selftest_forbidden_key_detector", forbidden_keys(planted) ==
       ["$.data.cells[0].internal_note"])
    ok("selftest_clean_payload_passes", forbidden_keys({"data": {"cells": [{"id": 1}]}}) == [])

    print("\n-- client-gate widening: grants gate the markup family per request --")
    r = Z0.get(f"{BASE}/drawing-markup", **R)
    ok("zero_grant_page_to_welcome", r.status_code == 302 and "welcome" in r.headers.get("Location", ""),
       f"{r.status_code} -> {r.headers.get('Location')}")
    for name, sess in (("zero_grant", Z0), ("progress_only", Z)):
        bad = []
        for path in (f"/api/elevations", f"/api/elevation/{eA}",
                     f"/api/comments?target_type=drop&target_id={dA1}",
                     f"/api/rfis?elevation_id={eA}"):
            rr = sess.get(BASE + path, **R)
            if rr.status_code != 403:
                bad.append(f"{path}:{rr.status_code}")
        ok(f"{name}_all_markup_apis_403", not bad, "; ".join(bad))
    r = Z.get(f"{BASE}/drawing-markup", **R)
    ok("progress_only_page_to_portal", r.status_code == 302 and r.headers.get("Location", "").endswith("/portal"),
       f"{r.status_code} -> {r.headers.get('Location')}")
    r = RC.get(f"{BASE}/api/rfis", params={"elevation_id": eA}, **R)
    ok("rfis_only_can_read_rfis", r.status_code == 200, f"{r.status_code}")
    r = RC.get(f"{BASE}/api/elevation/{eA}", **R)
    ok("rfis_only_no_drawing", r.status_code == 403, f"{r.status_code}")
    r = W.get(f"{BASE}/api/rfis", params={"elevation_id": eA}, **R)
    ok("drawing_only_no_rfis", r.status_code == 403, f"{r.status_code}")
    r = D.get(f"{BASE}/drawing-markup", **R)
    ok("drawing_grant_page_200", r.status_code == 200, f"{r.status_code}")
    r = D.get(f"{BASE}/api/elevation/{eA}", **R)
    ok("drawing_grant_elevation_200", r.status_code == 200, f"{r.status_code}")

    print("\n-- scoping: target-derived project, cross-project 403, unknown 404 --")
    r = D.get(f"{BASE}/api/elevation/{eB}", **R)
    ok("client_cross_project_elevation_403", r.status_code == 403, f"{r.status_code}")
    r = ARCH.get(f"{BASE}/api/elevation/{eB}", **R)
    ok("arch_cross_project_elevation_403", r.status_code == 403, f"{r.status_code}")
    r = ARCH.get(f"{BASE}/api/elevation/999999", **R)
    ok("unknown_elevation_404", r.status_code == 404, f"{r.status_code}")
    r = D.get(f"{BASE}/api/comments", params={"target_type": "drop", "target_id": dB1}, **R)
    ok("client_cross_project_comments_403", r.status_code == 403, f"{r.status_code}")
    r = ARCH.post(f"{BASE}/api/comments",
                  json={"target_type": "drop", "target_id": dB1, "body": "smk280 x"}, **R)
    ok("arch_cross_project_comment_post_403", r.status_code == 403, f"{r.status_code}")
    r = ARCH.post(f"{BASE}/api/rfis", json={"elevation_id": eB, "title": "smk280 x"}, **R)
    ok("arch_cross_project_rfi_403", r.status_code == 403, f"{r.status_code}")
    r = ARCH.get(f"{BASE}/api/elevations", **R)
    listed = [e.get("id") for e in (r.json().get("data") or [])] if r.status_code == 200 else None
    ok("arch_elevation_list_scoped_to_A", r.status_code == 200 and listed is not None
       and eA in listed and eB not in listed, f"{r.status_code} ids={listed}")

    print("\n-- per-job Drawings tab: ?project= lists that job only, never widens --")
    def _listed(sess, code):
        r = sess.get(f"{BASE}/api/elevations", params={"project": code}, **R)
        rows = (r.json().get("data") or []) if r.status_code == 200 else None
        return r.status_code, rows
    st, rows = _listed(CS, PB)
    ok("project_param_lists_only_that_job", st == 200 and rows is not None and rows
       and all(e.get("project_code") == PB for e in rows) and eB in [e.get("id") for e in rows],
       f"{st} codes={sorted({e.get('project_code') for e in rows or []})}")
    st, rows = _listed(CS, "SMK280-NOPE")
    ok("project_param_job_without_drawings_empty", st == 200 and rows == [], f"{st} n={len(rows or [])}")
    st, rows = _listed(ARCH, PB)
    ok("project_param_never_widens_scope", st == 200 and rows == [], f"{st} n={len(rows or [])}")
    dash = (SCRIPT_DIR / "dashboard-static.html").read_text(encoding="utf-8")
    ok("drawings_tab_frame_carries_job", 'src="/drawing-markup?project=__PROJECT_CODE__"' in dash)
    mk = (SCRIPT_DIR / "templates" / "v2" / "drawing-markup.html").read_text(encoding="utf-8")
    ok("markup_page_scopes_list_and_memory", "fetch('/api/elevations' + QS" in mk
       and "LS_KEY = 'ssc.drawingMarkup.elevationId' + (PROJECT" in mk)

    print("\n-- writes: internal-only paint, transition validation, reason law --")
    r = ARCH.post(f"{BASE}/api/elevation/cell",
                  json={"cell_id": cells["dA1"][0], "status_key": "in_progress"}, **R)
    ok("arch_cannot_paint_cell", r.status_code == 403, f"{r.status_code}")
    r = D.post(f"{BASE}/api/elevation/cell",
               json={"cell_id": cells["dA1"][0], "status_key": "in_progress"}, **R)
    ok("client_cannot_paint_cell", r.status_code == 403, f"{r.status_code}")
    r = CS.post(f"{BASE}/api/elevation/cell",
                json={"cell_id": cells["dA1"][0], "status_key": "bogus"}, **R)
    ok("bogus_status_400", r.status_code == 400, f"{r.status_code}")
    r = CS.post(f"{BASE}/api/elevation/cell",
                json={"cell_id": cells["dA1"][0], "status_key": "on_hold"}, **R)
    ok("on_hold_without_reason_400", r.status_code == 400, f"{r.status_code}")
    r = CS.post(f"{BASE}/api/elevation/cell",
                json={"cell_id": cells["dA1"][0], "status_key": "rework"}, **R)
    ok("rework_without_reason_400", r.status_code == 400, f"{r.status_code}")
    r = CS.post(f"{BASE}/api/elevation/cell",
                json={"cell_id": cells["dA1"][0], "status_key": "on_hold",
                      "reason": "smk280 hold reason", "internal_note": "smk280 INTERNAL ONLY"}, **R)
    ok("on_hold_with_reason_200", r.status_code == 200, f"{r.status_code}")
    r = CS.post(f"{BASE}/api/elevation/cell",
                json={"cell_id": cells["dA1"][0], "status_key": "in_progress",
                      "reason": "x" * 501}, **R)
    ok("overlong_reason_400", r.status_code == 400, f"{r.status_code}")
    conn = db_layer.connect()
    try:
        before = conn.execute("SELECT COUNT(*) FROM elevation_cell_event "
                              "WHERE cell_id IN (?,?)", tuple(cells["dA2"])).fetchone()[0]
    finally:
        conn.close()
    r = CS.post(f"{BASE}/api/elevation/drop",
                json={"drop_id": dA2, "status_key": "rework", "reason": "smk280 drop rework"}, **R)
    ok("drop_paint_200_counts_cells", r.status_code == 200
       and r.json()["data"]["cells_updated"] == len(cells["dA2"]), f"{r.status_code}")
    conn = db_layer.connect()
    try:
        after = conn.execute("SELECT COUNT(*) FROM elevation_cell_event "
                             "WHERE cell_id IN (?,?)", tuple(cells["dA2"])).fetchone()[0]
    finally:
        conn.close()
    ok("drop_paint_event_per_cell", after - before == len(cells["dA2"]), f"{before}->{after}")

    print("\n-- vocabulary split + internal_note absence (key-level, recursive) --")
    r = CS.get(f"{BASE}/api/elevation/{eA}", **R)
    j = r.json().get("data", {})
    ok("internal_audience_flag", j.get("audience") == "internal")
    ok("internal_vocab_is_internal_keys",
       set(j.get("statuses", {}).keys()) == {"not_started", "in_progress", "on_hold", "rework", "complete"},
       str(sorted(j.get("statuses", {}).keys())))
    ok("internal_sees_internal_note_key",
       any("internal_note" in c for c in j.get("cells", [])))
    ok("internal_can_paint", j.get("can_paint") is True and j.get("can_collaborate") is True)
    for who, sess in (("client", D), ("architect", ARCH)):
        r = sess.get(f"{BASE}/api/elevation/{eA}", **R)
        j = r.json()
        d = j.get("data", {})
        leaks = forbidden_keys(j)
        ok(f"{who}_external_audience", d.get("audience") == "external")
        ok(f"{who}_no_internal_note_key_anywhere", not leaks, "; ".join(leaks))
        ok(f"{who}_cannot_paint", d.get("can_paint") is False)
        vocab_rr = [v.get("reason_required") for v in d.get("statuses", {}).values()]
        ok(f"{who}_vocab_never_requires_reason", vocab_rr and not any(vocab_rr))
    r = ARCH.get(f"{BASE}/api/elevation/{eA}", **R)
    ok("arch_can_collaborate", r.json()["data"].get("can_collaborate") is True)
    r = D.get(f"{BASE}/api/elevation/{eA}", **R)
    ok("client_cannot_collaborate", r.json()["data"].get("can_collaborate") is False)
    ok("external_sees_reason", any(c.get("reason") for c in r.json()["data"].get("cells", [])),
       "the on_hold reason is external by construction and must be present")

    print("\n-- derivation: five planted cell mixes -> documented rollup --")
    mixes = [
        (("complete", "complete", "complete"), "complete"),
        (("complete", "in_progress", "not_started"), "in_progress"),
        (("complete", "on_hold", "in_progress"), "on_hold"),
        (("on_hold", "rework", "complete"), "rework"),
        (("not_started", "not_started", "not_started"), "not_started"),
    ]
    for mix, want in mixes:
        for cid, st in zip(cells["dA1"], mix):
            payload = {"cell_id": cid, "status_key": st}
            if st in ("on_hold", "rework"):
                payload["reason"] = "smk280 derivation mix"
            rr = CS.post(f"{BASE}/api/elevation/cell", json=payload, **R)
            assert rr.status_code == 200, f"seed paint failed: {rr.status_code}"
        r = CS.get(f"{BASE}/api/elevation/{eA}", **R)
        got = {d["id"]: d.get("derived_status") for d in r.json()["data"]["drops"]}.get(dA1)
        ok(f"derived_{'+'.join(mix)}_is_{want}", got == want, f"got {got}")

    print("\n-- history: reasons for all, staff never named to externals --")
    r = CS.get(f"{BASE}/api/elevation/cell/{cells['dA1'][0]}/history", **R)
    hist_i = r.json().get("data", [])
    ok("history_internal_200_nonempty", r.status_code == 200 and len(hist_i) >= 2, f"{len(hist_i)}")
    ok("history_internal_actor_person", all(h["actor"].get("kind") == "person" for h in hist_i))
    r = D.get(f"{BASE}/api/elevation/cell/{cells['dA1'][0]}/history", **R)
    hist_e = r.json().get("data", [])
    ok("history_external_200", r.status_code == 200 and len(hist_e) == len(hist_i))
    ok("history_external_staff_never_person",
       all(h["actor"].get("kind") in ("worker_id", "org") for h in hist_e),
       str(sorted({h['actor'].get('kind') for h in hist_e})))
    ok("history_external_no_internal_note", not forbidden_keys(hist_e))
    r = ARCH.get(f"{BASE}/api/elevation/cell/{cells['dB1'][0]}/history", **R)
    ok("history_cross_project_403", r.status_code == 403, f"{r.status_code}")

    print("\n-- comments: read-only client, architect writes, rate limit, soft delete --")
    r = D.get(f"{BASE}/api/comments", params={"target_type": "drop", "target_id": dA1}, **R)
    ok("client_thread_200_cannot_post", r.status_code == 200 and r.json().get("can_post") is False,
       f"{r.status_code} can_post={r.json().get('can_post') if r.status_code == 200 else '?'}")
    r = D.post(f"{BASE}/api/comments",
               json={"target_type": "drop", "target_id": dA1, "body": "smk280 client"}, **R)
    ok("client_comment_post_403", r.status_code == 403, f"{r.status_code}")
    xss = "<script>alert('smk280')</script>"
    r = ARCH.post(f"{BASE}/api/comments",
                  json={"target_type": "drop", "target_id": dA1, "body": xss}, **R)
    arch_comment = r.json().get("data", {}).get("id") if r.status_code == 201 else None
    ok("arch_comment_201", r.status_code == 201, f"{r.status_code}")
    r = D.get(f"{BASE}/api/comments", params={"target_type": "drop", "target_id": dA1}, **R)
    bodies = [c.get("body") for c in r.json().get("data", [])]
    ok("comment_body_raw_never_html_interpreted", xss in bodies,
       "the API must return the body verbatim (escaping is the renderer's job)")
    authors = {c["author"].get("kind") for c in r.json().get("data", [])}
    ok("external_viewer_sees_arch_as_person", "person" in authors, str(authors))
    r = CS.post(f"{BASE}/api/comments",
                json={"target_type": "drop", "target_id": dA1, "body": "smk280 internal"}, **R)
    cs_comment = r.json().get("data", {}).get("id") if r.status_code == 201 else None
    ok("internal_comment_201", r.status_code == 201, f"{r.status_code}")
    r = D.get(f"{BASE}/api/comments", params={"target_type": "drop", "target_id": dA1}, **R)
    staff_kinds = {c["author"].get("kind") for c in r.json().get("data", [])
                   if c.get("id") == cs_comment}
    ok("external_viewer_staff_comment_not_named", staff_kinds <= {"worker_id", "org"}, str(staff_kinds))

    # rate limit: architect has 1 comment so far; 9 more fills the 10-window
    for i in range(9):
        rr = ARCH.post(f"{BASE}/api/comments",
                       json={"target_type": "drop", "target_id": dA2, "body": f"smk280 rl {i}"}, **R)
        assert rr.status_code == 201, f"rate-limit filler {i}: {rr.status_code}"
    r = ARCH.post(f"{BASE}/api/comments",
                  json={"target_type": "drop", "target_id": dA2, "body": "smk280 rl over"}, **R)
    ok("comment_rate_limit_429_on_11th", r.status_code == 429, f"{r.status_code}")

    r = D.delete(f"{BASE}/api/comments/{arch_comment}", **R)
    ok("client_cannot_delete", r.status_code == 403, f"{r.status_code}")
    r = ARCH.delete(f"{BASE}/api/comments/{cs_comment}", **R)
    ok("arch_cannot_delete_others", r.status_code == 403, f"{r.status_code}")
    r = ARCH.delete(f"{BASE}/api/comments/{arch_comment}", **R)
    ok("author_soft_delete_200", r.status_code == 200, f"{r.status_code}")
    r = CS.get(f"{BASE}/api/comments", params={"target_type": "drop", "target_id": dA1}, **R)
    ids_after = [c.get("id") for c in r.json().get("data", [])]
    bodies_after = " ".join(c.get("body") or "" for c in r.json().get("data", []))
    ok("soft_deleted_absent_from_payload", arch_comment not in ids_after and xss not in bodies_after)
    conn = db_layer.connect()
    try:
        rrow = conn.execute("SELECT deleted_at FROM comment WHERE id=?", (arch_comment,)).fetchone()
    finally:
        conn.close()
    ok("soft_deleted_row_retained", rrow is not None and rrow[0] is not None,
       "soft delete must stamp deleted_at, never remove the row")

    print("\n-- RFI comment threads ride the rfis grant --")
    conn = db_layer.connect()
    try:
        cur = conn.execute(
            "INSERT INTO rfi (project_code, elevation_id, number, title, raised_by_uid, "
            "raised_at, status) VALUES (?,?, 'RFI-9', 'smk280 planted unpadded', ?, "
            "'2026-07-28T00:00:00', 'open')", (PA, eA, users["csuite"]))
        planted_rfi = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    r = D.get(f"{BASE}/api/comments", params={"target_type": "rfi", "target_id": planted_rfi}, **R)
    ok("rfi_thread_with_rfis_grant_200", r.status_code == 200, f"{r.status_code}")
    r = W.get(f"{BASE}/api/comments", params={"target_type": "rfi", "target_id": planted_rfi}, **R)
    ok("rfi_thread_without_rfis_grant_403", r.status_code == 403, f"{r.status_code}")
    r = W.get(f"{BASE}/api/comments", params={"target_type": "drop", "target_id": dA1}, **R)
    ok("drop_thread_with_drawing_grant_200", r.status_code == 200, f"{r.status_code}")

    print("\n-- RFIs: numeric allocation, role split, attach-later validation --")
    r = ARCH.post(f"{BASE}/api/rfis", json={"elevation_id": eA, "title": "smk280 arch rfi"}, **R)
    new_rfi = r.json().get("data", {}) if r.status_code == 201 else {}
    ok("arch_rfi_201", r.status_code == 201, f"{r.status_code}")
    ok("rfi_numeric_allocation_after_unpadded", new_rfi.get("number") == "RFI-010",
       f"planted RFI-9 -> next must be RFI-010, got {new_rfi.get('number')}")
    r = D.post(f"{BASE}/api/rfis", json={"elevation_id": eA, "title": "smk280 client rfi"}, **R)
    ok("client_rfi_post_403", r.status_code == 403, f"{r.status_code}")
    r = D.get(f"{BASE}/api/rfis", params={"elevation_id": eA}, **R)
    jj = r.json()
    ok("client_rfi_list_200_flags_off", r.status_code == 200 and jj.get("can_raise") is False
       and jj.get("can_close") is False, f"{r.status_code}")
    ok("client_rfi_list_no_internal_note", not forbidden_keys(jj))
    rid = new_rfi.get("id")
    r = ARCH.patch(f"{BASE}/api/rfis/{rid}", json={"status": "closed"}, **R)
    ok("arch_cannot_close", r.status_code == 403, f"{r.status_code}")
    r = ARCH.patch(f"{BASE}/api/rfis/{rid}", json={"drop_id": dB1}, **R)
    ok("attach_foreign_drop_400", r.status_code == 400, f"{r.status_code}")
    r = ARCH.patch(f"{BASE}/api/rfis/{rid}", json={"drop_id": dA1}, **R)
    ok("arch_attach_own_drop_200", r.status_code == 200 and r.json()["data"].get("drop_id") == dA1,
       f"{r.status_code}")
    r = CS.patch(f"{BASE}/api/rfis/{rid}", json={"status": "closed"}, **R)
    ok("internal_close_200_stamps", r.status_code == 200 and r.json()["data"].get("closed_at"),
       f"{r.status_code}")
    r = D.patch(f"{BASE}/api/rfis/{rid}", json={"drop_id": None}, **R)
    ok("client_rfi_patch_403", r.status_code == 403, f"{r.status_code}")


def main():
    print(f"== #283 guard: drawing markup / comments / RFIs (#280) ==  BASE={BASE}")
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
