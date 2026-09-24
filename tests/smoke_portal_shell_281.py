"""#283 GUARD for the #281 portal foundation (shipped without a suite).

What it proves, per the fold-in audit's gap inventory:
  LANDING     (#284 — the flip replaced the #281 LOCK) /portal/<code> is now the
              CLIENT's surface: a client on their own project gets the portal
              shell (200), another project is 403 (scope), an architect keeps
              /drawing-markup, and the shell a client receives carries ONLY
              portal nav (grant-driven), never internal nav items.
  SCOPE       an unassigned pm gets 403; an assigned pm gets 200 and the page is
              wired to the INTERNAL namespace (/api/projects/<code>), never the
              curated portal one.
  PREVIEW     ?preview_client is admin/c_suite-ONLY and target-validated on BOTH
              paths (Classic + new shell): a client passing it sees SELF, a pm
              gets 403, a non-external / inactive / non-existent target is
              refused, and each admin preview writes its audit row.
  SCRUB       the served shell carries no internal-document links, no build
              changelog prose, and no workforce/comp markup for an external
              role — asserted on the DOM the role actually receives.
  CATALOG     /api/admin/client-grants serves the section catalog with
              served_sections ⊆ grantable, weekly/materials catalogued but NOT
              served, presets are copies (never the catalog object), Standard
              excludes drawing/rfis, Full includes them.
  REGISTRY    client_payload() is fail-closed: an unregistered dataset RAISES
              rather than passing data through, an unregistered FIELD is dropped
              (planted-leak self-test), and the registry guard rejects an
              internal-by-nature field registered client-safe.

Isolated backend REQUIRED (seeds users/projects). PII-safe: synthetic identities,
ids/keys/counts only. 127.0.0.1 only.
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

import client_grants  # noqa: E402
import client_registry as reg  # noqa: E402
import db_layer  # noqa: E402

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5050")
PA, PB = "SMK281-A", "SMK281-B"
PASS, FAIL = [], []
IDS = {"users": []}


def ok(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {note}" if note and not cond else ""))
    return bool(cond)


def seed():
    conn = db_layer.connect()
    try:
        for code in (PA, PB):
            conn.execute("DELETE FROM projects WHERE project_code=?", (code,))
            conn.execute("INSERT INTO projects (project_code, name, status) VALUES (?,?,'active')",
                         (code, f"Smoke 281 {code[-1]}"))
        users = {}
        for key, role, active in (("admin", "admin", 1), ("pm_on", "pm", 1), ("pm_off", "pm", 1),
                                  ("client_a", "client", 1), ("client_b", "client", 1),
                                  ("arch", "architect", 1), ("client_x", "client", 0)):
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, role, full_name, is_active, status, "
                "must_reset_password, is_system) VALUES (?,?,?,?,?,?,0,1)",
                (f"smk281-{key}@superstars.local", "x!unusable", role, f"SMK281 {key}",
                 active, "active" if active else "inactive"))
            users[key] = cur.lastrowid
            IDS["users"].append(cur.lastrowid)
        for key, code in (("pm_on", PA), ("client_a", PA), ("client_b", PB), ("arch", PA)):
            conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_by, "
                         "assigned_at) VALUES (?,?,?, '2026-07-28T00:00:00')",
                         (users[key], code, users["admin"]))
        for key, code in (("client_a", PA), ("client_b", PB)):
            for s in ("progress", "documents"):
                conn.execute("INSERT INTO client_section_grant (user_id, project_code, section, "
                             "granted_by, granted_at) VALUES (?,?,?,?, '2026-07-28T00:00:00')",
                             (users[key], code, s, users["admin"]))
        sessions = {}
        for key, uid in users.items():
            tok = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions (id, user_id, expires_at, user_agent) "
                         "VALUES (?,?, '2099-01-01T00:00:00', 'smk281')", (tok, uid))
            sessions[key] = tok
        conn.commit()
        return users, sessions
    finally:
        conn.close()


def cleanup():
    conn = db_layer.connect()
    try:
        uids = IDS["users"]
        ph = ",".join("?" * len(uids)) if uids else "NULL"
        if uids:
            for t, c in (("client_section_grant", "user_id"), ("pm_project_assignment", "user_id"),
                         ("sessions", "user_id"), ("login_audit", "user_id"),
                         ("audit_log", "actor_user_id"), ("role_change_audit", "user_id"),
                         ("dashboard_layouts", "user_id")):
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
        conn.execute("DELETE FROM projects WHERE project_code IN (?,?)", (PA, PB))
        conn.commit()
        print("  [cleanup] synthetic rows removed (scoped to SMK281 ids)")
    finally:
        conn.close()


def S(sessions, key):
    s = requests.Session()
    s.cookies.set("ssc_session", sessions[key])
    return s


def audit_count(action, target_id=None):
    conn = db_layer.connect()
    try:
        if target_id is None:
            row = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action=?", (action,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action=? AND target_id=?",
                               (action, str(target_id))).fetchone()
        return row[0]
    finally:
        conn.close()


def run():
    users, sessions = seed()
    R = dict(allow_redirects=False, timeout=25)
    AD = S(sessions, "admin")
    PM_ON, PM_OFF = S(sessions, "pm_on"), S(sessions, "pm_off")
    CA, CB, ARCH = S(sessions, "client_a"), S(sessions, "client_b"), S(sessions, "arch")

    print("\n-- #284 landing (the flip): the shell is the client's surface, scoped --")
    r = CA.get(f"{BASE}/portal/{PA}", **R)
    body = r.text if r.status_code == 200 else ""
    ca_markup = re.sub(r"<script\b.*?</script>", "", body, flags=re.S | re.I)
    ok("client_own_project_gets_portal_shell", r.status_code == 200
       and 'data-portal-nav="progress"' in ca_markup
       and 'data-portal-nav="documents"' in ca_markup,
       f"{r.status_code}")
    ok("client_shell_nav_is_grant_driven",
       'data-portal-nav="photos"' not in ca_markup
       and 'data-portal-nav="daily"' not in ca_markup,
       "client_a holds progress+documents ONLY")
    ok("client_shell_has_no_internal_nav", 'data-view="' not in ca_markup,
       "an internal nav item reached an external DOM")
    ok("client_shell_uses_portal_namespace",
       f"/api/portal/{PA}" in body and f"/api/projects/{PA}" not in body)
    r = CA.get(f"{BASE}/portal/{PB}", **R)
    ok("client_other_project_403", r.status_code == 403, f"{r.status_code}")
    r = ARCH.get(f"{BASE}/portal/{PA}", **R)
    ok("architect_kept_on_drawing_surface", r.status_code == 302
       and r.headers.get("Location", "").endswith("/drawing-markup"),
       f"{r.status_code} -> {r.headers.get('Location')}")

    print("\n-- internal scope on the same route --")
    r = PM_OFF.get(f"{BASE}/portal/{PA}", **R)
    ok("unassigned_pm_403", r.status_code == 403, f"{r.status_code}")
    r = PM_ON.get(f"{BASE}/portal/{PA}", **R)
    body = r.text if r.status_code == 200 else ""
    ok("assigned_pm_200", r.status_code == 200, f"{r.status_code}")
    ok("assigned_pm_gets_internal_namespace",
       f"/api/projects/{PA}" in body and f"/api/portal/{PA}" not in body,
       "the shell must fetch from the internal namespace for an internal role")
    ok("shell_identity_filled_from_url", PA in body and "890 E 135th" not in body)

    print("\n-- preview: admin/c_suite only, target-validated, audited, on BOTH paths --")
    before = audit_count("portal_shell_preview", users["client_a"])
    r = AD.get(f"{BASE}/portal/{PA}", params={"preview_client": users["client_a"]}, **R)
    ok("admin_preview_new_shell_200", r.status_code == 200, f"{r.status_code}")
    ok("admin_preview_uses_portal_namespace",
       f"/api/portal/{PA}" in r.text and f"/api/projects/{PA}" not in r.text,
       "previewing AS a client must repoint the shell at the curated namespace")
    ok("admin_preview_writes_audit_row",
       audit_count("portal_shell_preview", users["client_a"]) == before + 1)
    for label, val in (("nonexistent", 99999999), ("inactive_client", users["client_x"]),
                       ("non_external_role", users["pm_on"]), ("garbage", "not-an-int")):
        rr = AD.get(f"{BASE}/portal/{PA}", params={"preview_client": val}, **R)
        ok(f"admin_preview_rejects_{label}", rr.status_code == 403, f"{rr.status_code}")
    r = PM_ON.get(f"{BASE}/portal/{PA}", params={"preview_client": users["client_a"]}, **R)
    ok("pm_preview_new_shell_ignored", r.status_code == 200
       and f"/api/projects/{PA}" in r.text,
       "a pm passing preview_client must still get their OWN internal view, never the client's")
    r = PM_ON.get(f"{BASE}/portal", params={"preview_client": users["client_a"]}, **R)
    ok("pm_preview_classic_403", r.status_code == 403, f"{r.status_code}")
    r = PM_ON.get(f"{BASE}/api/portal/documents", params={"preview_client": users["client_a"]}, **R)
    ok("pm_preview_classic_api_403", r.status_code == 403, f"{r.status_code}")
    r = CA.get(f"{BASE}/api/portal/context", params={"preview_client": users["client_b"]}, **R)
    flat = str(r.json()) if r.status_code == 200 else ""
    ok("client_preview_param_ignored_self_only",
       r.status_code == 200 and PA in flat and PB not in flat,
       "a client may never widen their view with the preview param")
    r = CA.get(f"{BASE}/portal/{PA}", params={"preview_client": users["client_b"]}, **R)
    ok("client_preview_param_ignored_on_shell", r.status_code == 200
       and f"/api/portal/{PA}" in r.text,
       "a client passing preview_client must get SELF, never another client's shell")

    print("\n-- served-shell scrub for the role that actually receives it --")
    preview = AD.get(f"{BASE}/portal/{PA}", params={"preview_client": users["client_a"]}, **R).text
    # Assert on MARKUP, not on the whole response: behaviour scripts legitimately
    # mention view names they defensively look for (`querySelector('[data-view=
    # "employees"]')` is guarded precisely BECAUSE the strip removes that element).
    # A view name inside a script is not a section; the DOM is what a role receives.
    p_markup = re.sub(r"<script\b.*?</script>", "", preview, flags=re.S | re.I)
    p_markup = re.sub(r"<style\b.*?</style>", "", p_markup, flags=re.S | re.I)
    internal_links = re.findall(r'"[^"]*-internal\.[a-z]+"', preview)
    ok("preview_no_internal_document_links", not internal_links, str(internal_links[:3]))
    ok("preview_no_build_changelog", not re.search(r"build #\d+\s*·", preview))
    # #340 — subs/equipment/site-closure were "Coming with Phase 2B" placeholder
    # views; Phase 3 removed them from the template entirely (not just stripped
    # per-role), so they no longer belong in this per-role strip check.
    for marker, label in (('data-view="expenses"', "expenses_view"),
                          ('data-view="product-usage"', "product_usage"),
                          ('id="kpi-workers"', "workforce_kpi"),
                          ('data-view="employees"', "employees_view"),
                          ('data-view="toolbox-talks"', "toolbox_view"),
                          ('data-view="sov"', "sov_view")):
        ok(f"preview_strips_{label}", marker not in p_markup, f"{marker} present in external DOM")
    # ...and the internal-only SECTION blocks are gone as BLOCKS, so nothing inside
    # them (rows, tables, KPI tiles) can survive by having been missed individually.
    for sec in ("internal_ops", "financial", "workforce_kpi"):
        ok(f"preview_no_{sec}_block", f"SECTION:{sec}:start" not in preview,
           "the marker itself must be consumed by the strip")
    admin_page = AD.get(f"{BASE}/projects/{PA}", **R).text
    ok("admin_not_over_stripped", 'data-view="expenses"' in admin_page,
       "the financial role must still receive its own sections")

    print("\n-- grant catalog API shape --")
    r = AD.get(f"{BASE}/api/admin/client-grants", **R)
    d = r.json().get("data", {}) if r.status_code == 200 else {}
    grantable, served = d.get("grantable_sections") or [], d.get("served_sections") or []
    ok("catalog_200", r.status_code == 200, f"{r.status_code}")
    ok("catalog_serves_grantable_and_served", bool(grantable) and bool(served))
    ok("served_is_subset_of_grantable", set(served) <= set(grantable),
       f"served={served} grantable={grantable}")
    ok("drawing_and_rfis_are_served", {"drawing", "rfis"} <= set(served), str(served))
    ok("weekly_materials_catalogued_not_served",
       {"weekly", "materials"} <= set(grantable) and not ({"weekly", "materials"} & set(served)),
       "a section with no payload must never render a toggle")
    labels = d.get("section_labels") or {}
    ok("every_grantable_key_has_a_label", all(k in labels for k in grantable),
       str([k for k in grantable if k not in labels]))
    presets = d.get("presets") or {}
    ok("standard_excludes_drawing_and_rfis",
       not ({"drawing", "rfis"} & set(presets.get("standard", []))), str(presets.get("standard")))
    ok("full_includes_drawing_and_rfis",
       {"drawing", "rfis"} <= set(presets.get("full", [])), str(presets.get("full")))
    ok("no_preset_bundles_an_unserved_section",
       all(set(v) <= set(served) for v in presets.values()),
       str({k: sorted(set(v) - set(served)) for k, v in presets.items() if set(v) - set(served)}))
    ok("presets_are_copies_not_the_catalog",
       all(client_grants.PRESETS[k] is not client_grants.SECTIONS for k in client_grants.PRESETS)
       and set(client_grants.PRESETS["full"]) != set(client_grants.SECTIONS),
       "a preset that IS the catalog silently grants every future section")

    print("\n-- client_payload(): fail-closed by provenance --")
    dirty = {"pct": 42, "label": "ok", "internal_note": "never", "margin_pct": 31,
             "updated_by_uid": 7, "hold_reason": "budget"}
    out = reg.client_payload("health.progress", dirty)
    ok("registered_fields_only", set(out) == {"pct", "label"}, str(sorted(out)))
    ok("unregistered_fields_dropped",
       not ({"internal_note", "margin_pct", "updated_by_uid", "hold_reason"} & set(out)))
    try:
        reg.client_payload("health.does_not_exist", dirty)
        ok("unknown_dataset_raises", False, "an unknown dataset must RAISE, never pass through")
    except reg.RegistryError:
        ok("unknown_dataset_raises", True)
    ok("internal_audience_passes_through",
       reg.client_payload("health.progress", dirty, audience="internal") is dirty)
    rows = reg.client_payload("health.active_drop",
                              [{"drop_id": 1, "label": "D1", "internal_note": "x"}])
    ok("list_shape_preserved_and_filtered",
       isinstance(rows, list) and set(rows[0]) == {"drop_id", "label"}, str(rows))
    saved = dict(reg.DATASETS)
    try:
        reg.DATASETS["health.planted"] = frozenset({"pct", "internal_note"})
        try:
            reg.assert_registry_clean()
            ok("registry_guard_rejects_internal_field", False,
               "an internal-by-nature field registered client-safe must fail the guard")
        except reg.RegistryError:
            ok("registry_guard_rejects_internal_field", True)
    finally:
        reg.DATASETS.clear()
        reg.DATASETS.update(saved)
    reg.assert_registry_clean()
    ok("registry_clean_after_selftest", True)


def main():
    print(f"== #283 guard: portal shell / preview / catalog / registry (#281) ==  BASE={BASE}")
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
