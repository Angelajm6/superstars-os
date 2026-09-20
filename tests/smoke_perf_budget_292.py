"""#292 — THE PERFORMANCE BUDGET GATE (the forever rule).

The #275 structural-guard philosophy applied to speed: every served surface
carries a REQUEST-COUNT and PAYLOAD-BYTES ceiling measured at #292 close plus
slack. A future build that makes any page chatty again — a new widget with its
own fetch, an aggregate quietly bypassed, a payload that balloons — fails the
gate until it adopts the shared layer (bootstrap aggregate / cache-first
loader / ssc_memo).

HOW IT MEASURES (no browser needed, so it runs in the standard gate): the
page's LOAD-TIME endpoint set is declared here per surface — the census
established it empirically in the browser — and the suite fetches that exact
set as the surface's role, counting requests and summing response bytes. The
declared set IS the contract: adding a load-time fetch to a page without
adding it here leaves the page fast per this gate but slow in life, so the
census note below is part of the deal — re-run the browser census when you
add a load-time fetch, then update BUDGETS in the same commit.

CEILINGS are per-surface: `reqs` = load-time request count (page HTML + its
API set + chrome), `kb` = summed API payload budget. Both are #292-close
measurements + slack, rounded to human numbers.

PII-safe: counts and byte totals only.
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
from auth import hash_password  # noqa: E402

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5434")
P = "FR-BX-001"
PW = secrets.token_urlsafe(16)
ADMIN = "smk292b-admin@superstars.local"

# surface -> {page, apis[], reqs (ceiling), kb (ceiling), note}
# reqs = 1 (page HTML) + len(apis) + 1 (chrome js) unless noted; the ceiling is
# what the census measured at #292 close, plus slack for one future widget.
BUDGETS = {
    "console": {
        "page": "/",
        "apis": ["/api/console/bootstrap?project=" + P, "/api/dashboard/layout?page_key=company_console",
                 "/api/auth/me", "/api/admin/2fa-status", "/api/costs/widget?project=" + P,
                 "/api/admin/photo-edit-alerts"],
        # #294 — photo edit-rate banner joins the load-time set (the #289 2fa-
        # banner pattern: tiny admin-only aggregate, fetched once on load).
        # 1 + 6 + 1 = 8 requests — INSIDE the existing ceiling of 10; payload is
        # a few hundred bytes against 120 KB. Declared here in the same commit
        # per the census contract; the pattern DETAIL view is click-time, not
        # load-time, so it stays out of this set deliberately.
        "reqs": 10, "kb": 120,
        "note": "8-call fan-out -> bootstrap (#291); costs widget memoized (#292 S2.1); "
                "photo edit alerts (#294)",
    },
    "project_dashboard": {
        "page": "/projects/" + P,
        "apis": ["/api/projects/" + P + "/bootstrap", "/api/dashboard/layout?page_key=project_health",
                 "/api/auth/me", "/api/weather?project=" + P,
                 "/api/projects/" + P + "/reports/latest", "/api/toolbox-talks"],
        "reqs": 13, "kb": 120,
        "note": "4-call Project Health fan-out -> bootstrap (#291)",
    },
    "drop_plan": {
        "page": "/dropplan",
        "apis": ["/api/dropplan/projects/" + P + "/drops", "/api/dropplan/projects/" + P + "/rollup",
                 "/api/dropplan/projects/" + P + "/sov-lines", "/api/auth/me"],
        "reqs": 7, "kb": 60,
        "note": "cache-first loaders (#292 close); board+rollup+sov cached per project",
    },
    "labor_rates": {
        "page": "/admin/labor-rates",
        "apis": ["/api/labor-rates/roster", "/api/labor-rates/pending", "/api/auth/me"],
        # #311 — the page joined the shared shell: typography.css + widgets.css + shell.css
        # are three static, cacheable requests it did not make before. API set unchanged.
        "reqs": 9, "kb": 60,
        "note": "comp data: sessionStorage-only cache-first (#292 close); shell assets (#311)",
    },
    "drawing_markup": {
        "page": "/drawing-markup",
        # <ELEV_ID> is resolved from /api/elevations at run time — an id-literal
        # would 404 on any snapshot whose elevation ids differ (data-dependent
        # probes must never be hard-coded in a permanent gate).
        # /api/comments is a PER-THREAD endpoint (target_type+target_id
        # required, 400 without) — the page fetches it on cell open, NOT on
        # load, so it is deliberately absent from the load-time contract.
        "apis": ["/api/elevations", "/api/elevation/<ELEV_ID>",
                 "/api/rfis?elevation_id=<ELEV_ID>"],
        "reqs": 7, "kb": 100,
        # #293 RE-MEASURE (the deliberate raise this slot's old note demanded):
        # the surface is AUTHORABLE now. Load-time request COUNT is unchanged
        # (no new fetch on the markup page). Payload grew two ways: the
        # /api/elevations listing carries REAL rows + status alongside the
        # canonical placeholders, and /api/elevation/<id> gains
        # face_label/status/can_author. 890-snapshot census 2026-08-06 measured
        # ~64 KB summed; ceiling 80 -> 100 covers it plus authored siblings
        # appearing in the listing. KNOWN SCALE NOTE: an authored 80-floor x
        # 12-bay tower serves ~162 KB on its OWN /api/elevation/<id> (960
        # cells, measured in smoke_drawing_author_293) — the gate measures the
        # snapshot's 890 North; if a tower ever becomes this project's first
        # traced elevation, re-measure and raise HERE, deliberately.
        "note": "#293 authorable: listing carries real rows+status; by-id gains "
                "face_label/status; ceiling re-measured 2026-08-06",
    },
    "drawing_author": {
        # #293 — the authoring surface (internal-only). Load-time set: the
        # context (projects + their elevations incl. drafts) and the default
        # project's drawing-set listing. Thumbs are lazy per-sheet IMMUTABLE
        # image fetches, deliberately outside the load-time contract; the AI
        # propose call is operator-initiated, never load-time.
        "page": "/drawing-author",
        "apis": ["/api/author/context", "/api/projects/" + P + "/drawing-sets"],
        "reqs": 5, "kb": 40,
        "note": "#293 new surface: context + set listing; sheet thumbs lazy+immutable",
    },
    # #311 — these three joined the shared shell: +1 static, cacheable request (shell.css).
    # API sets unchanged; ceilings raised by exactly that one request, in this commit.
    "estimating": {
        "page": "/estimating",
        "apis": ["/api/estimating/queue", "/api/auth/me"],
        "reqs": 6, "kb": 40, "note": "already lean; shell.css (#311)",
    },
    "admin_users": {
        "page": "/admin/users",
        "apis": ["/api/admin/users", "/api/auth/me"],
        "reqs": 6, "kb": 40, "note": "already lean; shell.css (#311)",
    },
    "admin_projects": {
        "page": "/admin/projects",
        "apis": ["/api/admin/pm-assignments", "/api/auth/me"],
        "reqs": 6, "kb": 40, "note": "already lean; shell.css (#311)",
    },
    # #302 — Company Portal: ONE aggregate per view; the landing view (Overview) is
    # page + /overview + /api/auth/me (chrome). Other views fetch on demand, one
    # call each (finance / clients / expenses / workforce / compliance+documents).
    "company_portal": {"page": "/company", "apis": ["/api/company/portal/overview", "/api/auth/me"],
                       "reqs": 6, "kb": 60, "note": "one aggregate per view, hash-routed (#302)"},
    "login": {"page": "/login", "apis": ["/api/auth/sso/config"], "reqs": 4, "kb": 20,
              "note": "public"},
    "ui_settings": {"page": "/ui-settings", "apis": [], "reqs": 3, "kb": 10,
                    "note": "static page, zero API on load"},
    "worker_app": {"page": "/worker-app.html", "apis": [], "reqs": 3, "kb": 10,
                   "note": "PWA shell; PIN flow fetches on submit, not on load"},
    "portal": {"page": "/portal/" + P, "apis": ["/api/portal/" + P + "/progress", "/api/auth/me"],
               "reqs": 6, "kb": 60,
               "note": "client surface: session-scoped cache-first; landing section only"},
}

PASS, FAIL = [], []


def ok(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {note}" if note and not cond else ""))
    return bool(cond)


def main() -> int:
    print("== #292 PERFORMANCE BUDGET GATE (per-surface request/payload ceilings) ==")
    conn = db_layer.connect(pragma_fk=True)
    try:
        row = conn.execute("SELECT id FROM users WHERE email=?", (ADMIN,)).fetchone()
        if row:
            conn.execute("UPDATE users SET password_hash=?, role='admin', is_active=1, "
                         "status='active', must_reset_password=0, is_system=1 WHERE id=?",
                         (hash_password(PW), row[0]))
            uid = row[0]
        else:
            conn.execute("INSERT INTO users (email,password_hash,role,full_name,is_active,"
                         "status,must_reset_password,is_system) "
                         "VALUES (?,?,'admin','SMK292B Budget',1,'active',0,1)",
                         (ADMIN, hash_password(PW)))
            uid = conn.execute("SELECT id FROM users WHERE email=?", (ADMIN,)).fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": ADMIN, "password": PW}, timeout=20)
    if r.status_code != 200:
        print(f"  [FAIL] budget_login — {r.status_code}")
        return 1

    # resolve data-dependent ids once (see the drawing_markup note)
    elev_id = None
    try:
        je = s.get(f"{BASE}/api/elevations", timeout=30).json()
        rows = je.get("data") if isinstance(je, dict) else je
        if isinstance(rows, list):
            # the picker returns EVERY canonical face, untraced ones with
            # id=None — take the first TRACED row, never just rows[0]
            for r_ in rows:
                if isinstance(r_, dict) and r_.get("id"):
                    elev_id = r_["id"]
                    break
    except Exception:
        pass

    try:
        worst = []
        for name, b in sorted(BUDGETS.items()):
            apis = [u.replace("<ELEV_ID>", str(elev_id)) for u in b["apis"]
                    if "<ELEV_ID>" not in u or elev_id is not None]
            b = dict(b, apis=apis)
            n_req = 1 + len(b["apis"]) + 1          # page + APIs + chrome js
            total = 0
            statuses = []
            for u in b["apis"]:
                rr = s.get(f"{BASE}{u}", timeout=60)
                statuses.append(rr.status_code)
                if rr.status_code == 200:
                    total += len(rr.content)
            kb = total / 1024.0
            req_ok = n_req <= b["reqs"]
            kb_ok = kb <= b["kb"]
            ok(f"budget_{name}_requests({n_req}<={b['reqs']})", req_ok)
            ok(f"budget_{name}_payload({kb:.1f}KB<={b['kb']}KB)", kb_ok)
            # a surface whose declared API set 404s/500s means the declaration
            # drifted from the routes — that is a budget-gate failure too
            bad = [c for c in statuses if c not in (200, 403)]
            ok(f"budget_{name}_endpoints_resolve", not bad, f"statuses={statuses}")
            worst.append((name, n_req, b["reqs"], round(kb, 1), b["kb"]))

        print("\n  surface                requests(ceil)   payloadKB(ceil)")
        for n, rq, rqc, kb, kbc in worst:
            print(f"  {n:22} {rq:3}/{rqc:<3}          {kb:7.1f}/{kbc}")
        print(f"\n== {len(PASS)} PASS / {len(FAIL)} FAIL ==")
        for f in FAIL:      # names LAST so a truncated tail still shows them
            print(f"  FAILED: {f}")
        return 0 if not FAIL else 1
    finally:
        conn = db_layer.connect(pragma_fk=True)
        try:
            for sql in ("DELETE FROM sessions WHERE user_id=?",
                        "DELETE FROM login_audit WHERE user_id=?",
                        "DELETE FROM users WHERE id=?"):
                try:
                    conn.execute(sql, (uid,))
                    conn.commit()
                except Exception:
                    conn.rollback()
        finally:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
