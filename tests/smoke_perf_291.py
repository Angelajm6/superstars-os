"""#291 guard — the bootstrap aggregates are MIRRORS, never doors.

Proves, against the shared gate server:
  1. PARITY (admin): every key the console + project bootstraps return is
     deep-equal to the payload of the individual endpoint it mirrors, same
     request args. An aggregate that drifts from its endpoints goes red.
  2. ADMITS NOTHING NEW (pm): the project bootstrap's key set for a pm is a
     subset of the endpoints that pm can fetch individually (200s); the
     console bootstrap refuses a pm outright (company-only page, #263).
  3. CONTAINMENT (client): both aggregates 403 for a client session — the
     client gate fires before any aggregate code runs.
  4. THUMB CACHING (#291.3): /api/field-photos/<id>/thumb + /file carry
     exactly 'private, max-age=31536000, immutable' (private = Cloudflare's
     shared cache must never hold an auth-gated photo; browser-only), and
     the routes still 401 anonymously — headers never weakened auth.
  5. NO PATH KEYS: aggregate payloads contain no filesystem-path-shaped keys
     (CLAUDE.md files-cross-by-id rule), recursively.
  6. LAZY GRID (source-level): the field-photos + DCR photo tiles carry
     loading="lazy" in dashboard-static.html.

Isolated backend required (SSC_DB_URL); PII-safe output.
"""
from __future__ import annotations

import os
import re
import secrets
import sys
import uuid
from datetime import date
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import db_layer  # noqa: E402
import ssc_paths  # noqa: E402
from auth import hash_password  # noqa: E402

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5434")
PROJECT = "FR-BX-001"
PW = secrets.token_urlsafe(16)
EMAILS = {"pm": "smk291-pm@superstars.local", "client": "smk291-client@superstars.local"}

PASS, FAIL = [], []


def ok(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {note}" if note and not cond else ""))
    return bool(cond)


def seed():
    conn = db_layer.connect(pragma_fk=True)
    try:
        ids = {}
        for key, email in EMAILS.items():
            role = "pm" if key == "pm" else "client"
            row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if row:
                conn.execute("UPDATE users SET password_hash=?, role=?, is_active=1, "
                             "status='active', must_reset_password=0, is_system=1 WHERE id=?",
                             (hash_password(PW), role, row[0]))
                ids[key] = row[0]
            else:
                conn.execute("INSERT INTO users (email,password_hash,role,full_name,is_active,"
                             "status,must_reset_password,is_system) VALUES (?,?,?,?,1,'active',0,1)",
                             (email, hash_password(PW), role, f"SMK291 {key}"))
                ids[key] = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
            conn.execute("DELETE FROM pm_project_assignment WHERE user_id=?", (ids[key],))
            conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_by, "
                         "assigned_at) VALUES (?,?,?,?)", (ids[key], PROJECT, ids[key],
                                                           "2026-08-02T00:00:00"))
        # a synthetic photo row + real bytes under the ACTIVE data root
        pdir = ssc_paths.under_root("data_room", "field_photos", PROJECT, "smk291-" + uuid.uuid4().hex[:8])
        pdir.mkdir(parents=True, exist_ok=True)
        full, thumb = pdir / "full.jpg", pdir / "thumb.jpg"
        jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 400 + b"\xff\xd9"
        full.write_bytes(jpg)
        thumb.write_bytes(jpg)
        cur = conn.execute(
            "INSERT INTO field_photos (project_code, uploaded_at, file_path, thumb_path, mime) "
            "VALUES (?,?,?,?,?)",
            (PROJECT, "2026-08-02T00:00:00", ssc_paths.store_rel(full),
             ssc_paths.store_rel(thumb), "image/jpeg"))
        pid = cur.lastrowid or conn.execute(
            "SELECT MAX(id) FROM field_photos WHERE project_code=?", (PROJECT,)).fetchone()[0]
        conn.commit()
        ids["photo"] = pid
        ids["pdir"] = pdir
        return ids
    finally:
        conn.close()


def cleanup(ids):
    conn = db_layer.connect(pragma_fk=True)
    try:
        if ids.get("photo"):
            conn.execute("DELETE FROM field_photos WHERE id=?", (ids["photo"],))
        for key in EMAILS:
            uid = ids.get(key)
            if not uid:
                continue
            for sql in ("DELETE FROM client_section_grant WHERE user_id=?",
                        "DELETE FROM pm_project_assignment WHERE user_id=?",
                        "DELETE FROM sessions WHERE user_id=?",
                        "DELETE FROM login_audit WHERE user_id=?",
                        "DELETE FROM users WHERE id=?"):
                try:
                    conn.execute(sql, (uid,))
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
        conn.commit()
    finally:
        conn.close()
    try:
        import shutil
        shutil.rmtree(ids.get("pdir"), ignore_errors=True)
    except Exception:
        pass


def login(email, pw):
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": email, "password": pw}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"login {email.split('@')[0]} -> {r.status_code}")
    return s


PATH_KEY_RE = re.compile(r"(?i)(^|_)(file_?path|filepath|path|folder)$")


def path_keys(obj, hits, kp=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and PATH_KEY_RE.search(k):
                hits.append(f"{kp}.{k}")
            path_keys(v, hits, f"{kp}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            path_keys(v, hits, f"{kp}[{i}]")


def main() -> int:
    print("== #291 guard: bootstrap aggregates + thumb caching ==")
    import _smoke_auth
    _smoke_auth.setup()
    admin = requests  # patched session

    ids = seed()
    try:
        t = date.today().isoformat()
        CONSOLE_PARTS = {
            "summary": "/api/company/summary",
            "projects": "/api/projects",
            "compliance": "/api/compliance/summary",
            "activity": "/api/activity/recent?limit=12",
            "material_alerts": "/api/company/material-alerts",
            "rollup": f"/api/dropplan/projects/{PROJECT}/rollup",
            "drops": f"/api/dropplan/projects/{PROJECT}/drops",
            "on_site": f"/api/projects/{PROJECT}/on-site?date={t}",
        }
        PROJ_PARTS = {
            "rollup": f"/api/dropplan/projects/{PROJECT}/rollup",
            "drops": f"/api/dropplan/projects/{PROJECT}/drops",
            "on_site": f"/api/projects/{PROJECT}/on-site?date={t}",
            "workers": f"/api/projects/{PROJECT}/workers",
        }

        # ---- 1. PARITY (admin) ----
        for name, agg_url, parts in (
            ("console", f"/api/console/bootstrap?project={PROJECT}&date={t}&limit=12", CONSOLE_PARTS),
            ("project", f"/api/projects/{PROJECT}/bootstrap?date={t}", PROJ_PARTS),
        ):
            r = admin.get(f"{BASE}{agg_url}", timeout=30)
            ok(f"{name}_bootstrap_200", r.status_code == 200, f"{r.status_code}")
            agg = (r.json().get("data") or {}) if r.status_code == 200 else {}
            ok(f"{name}_bootstrap_has_all_parts", set(parts) <= set(agg.keys()),
               f"missing {set(parts) - set(agg.keys())}")
            for k, url in parts.items():
                ri = admin.get(f"{BASE}{url}", timeout=30)
                ind = (ri.json() or {}).get("data") if ri.status_code == 200 else None
                same = (k in agg) and (agg[k] == ind)
                ok(f"{name}_parity_{k}", same,
                   f"individual={ri.status_code}")
            hits = []
            path_keys(agg, hits)
            ok(f"{name}_no_path_keys", not hits, str(hits[:4]))

        # ---- 2. pm: subset discipline + console refusal ----
        pm = login(EMAILS["pm"], PW)
        r = pm.get(f"{BASE}/api/console/bootstrap", timeout=15)
        ok("console_bootstrap_pm_403", r.status_code == 403, f"{r.status_code}")
        r = pm.get(f"{BASE}/api/projects/{PROJECT}/bootstrap?date={t}", timeout=30)
        ok("project_bootstrap_pm_200", r.status_code == 200, f"{r.status_code}")
        pm_agg = (r.json().get("data") or {}) if r.status_code == 200 else {}
        allowed = set()
        for k, url in PROJ_PARTS.items():
            if pm.get(f"{BASE}{url}", timeout=30).status_code == 200:
                allowed.add(k)
        ok("project_bootstrap_pm_subset", set(pm_agg.keys()) <= allowed,
           f"agg={sorted(pm_agg)} allowed={sorted(allowed)}")

        # ---- 3. client containment ----
        cl = login(EMAILS["client"], PW)
        for url in (f"/api/console/bootstrap", f"/api/projects/{PROJECT}/bootstrap"):
            r = cl.get(f"{BASE}{url}", timeout=15)
            ok(f"client_403_{url.split('/')[2]}", r.status_code == 403, f"{r.status_code}")

        # ---- 4. thumb/file cache headers + auth intact ----
        WANT = "private, max-age=31536000, immutable"
        for col in ("thumb", "file"):
            r = admin.get(f"{BASE}/api/field-photos/{ids['photo']}/{col}", timeout=15)
            ok(f"photo_{col}_200", r.status_code == 200, f"{r.status_code}")
            ok(f"photo_{col}_cache_control", r.headers.get("Cache-Control") == WANT,
               f"got {r.headers.get('Cache-Control')!r}")
        anon = requests.Session()
        r = anon.get(f"{BASE}/api/field-photos/{ids['photo']}/thumb", timeout=15)
        ok("photo_thumb_anon_401", r.status_code == 401, f"{r.status_code}")

        # ---- 4b. static bundle: bounded freshness + validator (#291 close) ----
        r = admin.get(f"{BASE}/files/static/js/dash_layout.js", timeout=15)
        ok("static_js_cacheable", r.status_code == 200 and
           r.headers.get("Cache-Control") == "public, max-age=600",
           f"got {r.headers.get('Cache-Control')!r}")
        ok("static_js_has_validator", bool(r.headers.get("ETag") or r.headers.get("Last-Modified")))
        # pages stay no-store (the #205 Safari-PWA lesson is scoped, not lost)
        r = admin.get(f"{BASE}/", timeout=15)
        ok("page_html_still_no_store", "no-store" in (r.headers.get("Cache-Control") or ""))

        # ---- 4c. instant-paint cache: logout purge + uid guard (source) ----
        amjs = (SCRIPT_DIR / "static" / "js" / "auth_menu.js").read_text(encoding="utf-8",
                                                                         errors="replace")
        logout_block = amjs[amjs.index("au-logout"):amjs.index("window.location.href = '/login'")]
        ok("logout_purges_boot_cache", "SSC_BOOT.purge()" in logout_block)
        ok("user_mismatch_guard_wired", "guardUser" in amjs)
        shell = (SCRIPT_DIR / "portal_shell.html").read_text(encoding="utf-8", errors="replace")
        so = shell[shell.index("getElementById('signout')"):]
        ok("portal_logout_purges_cache", "_bootPurge(true)" in so.split("};")[0])
        ok("portal_preview_bypasses_cache", "if (PREVIEW_ID) return _netGetJSON(url);" in shell)
        for page, marker in (("company-dashboard.html", "SSC_BOOT.read('console')"),
                             ("dashboard-static.html", "SSC_BOOT.read('project.'+PROJECT)")):
            src_pg = (SCRIPT_DIR / page).read_text(encoding="utf-8", errors="replace")
            ok(f"instant_paint_wired_{page.split('-')[0]}", marker in src_pg
               and "SSC_BOOT.write(" in src_pg)

        # ---- 4d. #292 shared layer (source-level) ----
        amjs2 = (SCRIPT_DIR / "static" / "js" / "auth_menu.js").read_text(encoding="utf-8",
                                                                          errors="replace")
        boot_block = amjs2[amjs2.index("window.SSC_BOOT"):amjs2.index("window.SSC_PERF")]
        ok("292_staff_cache_is_localstorage", "localStorage.getItem" in boot_block
           and "BOOT_TTL_MS" in amjs2)
        ok("292_purge_covers_both_stores",
           "[localStorage, sessionStorage].forEach" in boot_block)
        srv = (SCRIPT_DIR / "server.py").read_text(encoding="utf-8", errors="replace")
        ok("292_aggregates_run_parallel", "_agg_parallel(" in srv
           and "copy_current_request_context" in srv)
        for page in ("company-dashboard.html", "dashboard-static.html"):
            src_pg2 = (SCRIPT_DIR / page).read_text(encoding="utf-8", errors="replace")
            tag = page.split('-')[0]
            ok(f"292_skeletons_wired_{tag}", "SSC_PERF.skeleton(" in src_pg2)
            ok(f"292_prefetch_wired_{tag}", "SSC_PERF.prefetchWire()" in src_pg2
               and "data-prefetch=" in src_pg2)
        # portal + worker shells must NEVER get the staff perf chrome: no
        # localStorage boot cache, no prefetch (shared/borrowed devices)
        for ext_page in ("portal_shell.html", "worker-app.html"):
            src_ext = (SCRIPT_DIR / ext_page).read_text(encoding="utf-8", errors="replace")
            ok(f"292_no_staff_chrome_{ext_page.split('.')[0].split('_')[0].split('-')[0]}",
               "auth_menu.js" not in src_ext and "prefetchWire" not in src_ext
               and "localStorage.setItem('ssc.boot" not in src_ext)

        # ---- 4e. #292 S2.0 — zero layout shift (source-level ordering) ----
        dl = (SCRIPT_DIR / "static" / "js" / "dash_layout.js").read_text(encoding="utf-8",
                                                                         errors="replace")
        ok("s20_shared_init_animate_off", "animate: false" in dl)
        sync_at = dl.find("FIRST FRAME")
        get_at = dl.find("api('GET'")
        ok("s20_shared_cache_apply_before_server_fetch", 0 <= sync_at < get_at)
        ok("s20_shared_reconcile_unanimated", "applySilent" in dl
           and "setAnimation(false)" in dl)
        ok("s20_shared_writethrough_on_save", "writeCachedLayout(PAGE_KEY, snap)" in dl)
        ok("s20_shared_portal_sessionstorage_fallback",
           "sessionStorage.setItem('ssc.boot.layout." in dl.replace('" + ', "'"))
        ph = (SCRIPT_DIR / "dashboard-static.html").read_text(encoding="utf-8",
                                                              errors="replace")
        ok("s20_ph_init_animate_off", "float:false,animate:false" in ph)
        ph_sync = ph.find("const cached=readCachedLayout()")
        ph_fetch = ph.find("loadSavedLayout(painted)")
        ok("s20_ph_cache_apply_before_server_fetch", 0 <= ph_sync < ph_fetch)
        ok("s20_ph_reconcile_unanimated", "grid.setAnimation(false)" in ph)
        # the first-frame evidence mark is part of the contract (preview
        # verification reads it: mark.startTime <= DOMContentLoaded)
        ok("s20_first_frame_mark_present",
           dl.count("ssc-layout-cache-applied") >= 1 and ph.count("ssc-layout-cache-applied") >= 1)
        # v=292 stamps: this deploy's pages atomically load this deploy's JS
        cc = (SCRIPT_DIR / "company-dashboard.html").read_text(encoding="utf-8", errors="replace")
        # version-AGNOSTIC: the contract is "every chrome <script> carries a
        # ?v= stamp, and all pages agree on it" — pinning a literal made a
        # routine stamp bump fail the gate for no safety gain (caught 2026-08-05).
        stamps = {}
        for fn in ("company-dashboard.html", "dashboard-static.html",
                   "projects.html", "portal_shell.html"):
            src_v = (SCRIPT_DIR / fn).read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"(auth_menu|dash_layout)\.js\?v=([A-Za-z0-9._-]+)", src_v):
                stamps.setdefault(m.group(1), set()).add(m.group(2))
        # scoped to real src= attributes — a prose mention of the filename in a
        # comment is not a script tag (caught itself 2026-08-05)
        unstamped = re.search(r"src=[\"'][^\"']*(auth_menu|dash_layout)\.js(?!\?v=)", cc)
        ok("s20_versioned_script_urls",
           stamps.get("auth_menu") and stamps.get("dash_layout") and not unstamped,
           f"stamps={ {k: sorted(v) for k, v in stamps.items()} }")
        ok("s20_stamps_agree_across_pages",
           all(len(v) == 1 for v in stamps.values()),
           f"disagreement: { {k: sorted(v) for k, v in stamps.items() if len(v) > 1} }")

        # ---- 4f. flag-#5 — redraw diff: identical data = zero render work ----
        ok("f5_render_diff_helper", "renderIfChanged" in amjs2 and "_rc" in amjs2)
        ok("f5_console_widgets_diffed", "renderIfChanged('cc.'+k" in cc)
        dsh = (SCRIPT_DIR / "dashboard-static.html").read_text(encoding="utf-8", errors="replace")
        ok("f5_dashboard_widgets_diffed", "renderIfChanged('ph.'+k" in dsh)
        ok("f5_portal_identical_skip", "_cachePainted" in shell
           and "painted === s" in shell)

        # ---- 5. lazy grid (source-level) ----
        src = (SCRIPT_DIR / "dashboard-static.html").read_text(encoding="utf-8", errors="replace")
        fp_lazy = src.count("<img loading=\"lazy\"") + src.count("'<img loading=\"lazy\"")
        ok("grid_imgs_lazy", "thumb_url" in src and
           re.search(r"""<img loading="lazy"[^>]*'\s*\+\s*esc\(p\.thumb_url\)""", src) is not None)
        ok("dcr_tile_lazy", 'loading="lazy" decoding="async" src=\'' in src.replace('" + ', "' + ")
           or "loading=\"lazy\" decoding=\"async\"" in src)

        print(f"\n== {len(PASS)} PASS / {len(FAIL)} FAIL ==")
        return 0 if not FAIL else 1
    finally:
        cleanup(ids)


if __name__ == "__main__":
    sys.exit(main())
