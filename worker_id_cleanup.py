"""#346 — one-time worker ID cleanup: W-0030/31/32 become W-0027/28/29.

Angela added three empty test workers (W-0027..29) by mistake and asked for the three
real workers after them to move up, keeping all of their data. That overrides the
"worker IDs never change" rule for this one case only, so the map is PINNED here and the
action refuses to run a second time (its audit row is the marker).

Everything that proves who worked is keyed by employee_id (E-#####) and does not move.
What carries the W-#### and moves with the worker:
  * employees.worker_id, labor_worker_state.worker_id, labor_rate_change.worker_id,
    field_photos.worker_id (one transaction; no FKs point at worker_id)
  * the saved DCR files (internal.html / client.html print the labor roster by W-####).
    The sign-in <-> DCR reconcile reads those files and maps W-#### to LIVE workers, so a
    file left saying W-0030 would later be read as the NEXT hire (who gets W-0030).
    Stale PDFs are set aside and regenerated in small batches afterwards.
Issued card rows (CoF / company ID) are history and are NOT rewritten — the preview lists
who holds a card printed with the old number so it can be re-issued.

Guards: the targets must be gone from every W-keyed table, card numbers and saved DCRs
(delete the test workers first with Workforce > Delete permanently); an old ID must not
appear inside another code in a saved DCR (e.g. a card number) where the rewrite would be
ambiguous; the preview's plan_hash must still match under the lock; and the sign-in <->
DCR divergence report must be identical before and after (else nothing is changed).

PII (CLAUDE.md): responses and the audit row carry W-/E- IDs, counts and
<project>/<number> report folders — never names, phones or file-system paths.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from datetime import datetime

from flask import jsonify, request

import db_layer
import ssc_paths
from auth import _db, current_user, requires_company
from worker_lifecycle import has_column

log = logging.getLogger(__name__)

ACTION = "worker_renumber_346"
MAP = (("W-0030", "W-0027"), ("W-0031", "W-0028"), ("W-0032", "W-0029"))
W_TABLES = (("employees", "worker_id"), ("labor_worker_state", "worker_id"),
            ("labor_rate_change", "worker_id"), ("field_photos", "worker_id"))
LABELS = {"employees": "worker record", "labor_worker_state": "rate and status",
          "labor_rate_change": "rate history", "field_photos": "field photos"}
PDF_BATCH = 3
_FOLDER_RE = re.compile(r"^[A-Za-z0-9_-]+/\d{3,}$")


def _srcs():
    return [s for s, _ in MAP]


def _dsts():
    return [d for _, d in MAP]


def _patterns():
    ids = "|".join(re.escape(x) for x in _srcs() + _dsts())
    # anchored: a standalone W-#### (not the tail of SSC-COF-W-0030 or W-00301) — the only
    # form rewritten. loose: what the reconcile's own W-\d{4} reader would pick up; any
    # loose hit that is not anchored is ambiguous and blocks the cleanup.
    return re.compile(rf"(?<![\w-])({ids})(?!\d)"), re.compile(rf"({ids})")


def dcr_root():
    return ssc_paths.under_root("data_room", "reports", "dcr")


def _dcr_files():
    root = dcr_root()
    if not root.exists():
        return
    for proj in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        for seq in sorted(p for p in proj.iterdir() if p.is_dir() and p.name.isdigit()):
            for name in ("internal.html", "client.html"):
                f = seq / name
                if f.is_file():
                    yield f"{proj.name}/{seq.name}", name, f


def scan_files():
    anchored, loose = _patterns()
    out = []
    for folder, name, f in _dcr_files():
        text = f.read_text(encoding="utf-8", errors="replace")
        hits = anchored.findall(text)
        n_loose = len(loose.findall(text))
        if not n_loose:
            continue
        st = f.stat()
        out.append({
            "folder": folder, "file": name, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "src": {s: hits.count(s) for s in _srcs() if hits.count(s)},
            "dst": {d: hits.count(d) for d in _dsts() if hits.count(d)},
            "ambiguous": n_loose - len(hits),
            "has_pdf": (f.parent / "internal.pdf").is_file(),
        })
    return out


def _audit_row(conn):
    return conn.execute("SELECT id, after_json FROM audit_log WHERE action = ? ORDER BY id DESC",
                        (ACTION,)).fetchone()


def _count(conn, table, col, val):
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (val,)).fetchone()[0])


def plan(conn):
    problems = []
    done = _audit_row(conn) is not None
    if done:
        problems.append("This cleanup has already been applied.")

    sources = {}
    for src in _srcs():
        r = conn.execute("SELECT employee_id FROM employees WHERE worker_id = ?", (src,)).fetchone()
        sources[src] = r["employee_id"] if r else None
    if not done:
        for src, eid in sources.items():
            if not eid:
                problems.append(f"{src} is not on the roster, so there is nothing to renumber.")

    changes, target_hits = {}, {}
    for table, col in W_TABLES:
        if not has_column(conn, table, col):
            continue
        for src, dst in MAP:
            n = _count(conn, table, col, src)
            if n:
                changes.setdefault(table, {})[src] = n
            m = _count(conn, table, col, dst)
            if m:
                target_hits.setdefault(dst, []).append(f"{LABELS[table]} ({m})")
    for dst in _dsts():
        if has_column(conn, "cof_cards", "card_number_display"):
            m = int(conn.execute("SELECT COUNT(*) FROM cof_cards WHERE card_number_display = ? OR card_id LIKE ?",
                                 (f"SSC-COF-{dst}", f"SSC-COF-{dst}-%")).fetchone()[0])
            if m:
                target_hits.setdefault(dst, []).append(f"CoF cards ({m})")
        if has_column(conn, "company_id_cards", "card_number_display"):
            m = _count(conn, "company_id_cards", "card_number_display", f"SSC-CID-{dst}")
            if m:
                target_hits.setdefault(dst, []).append(f"company ID cards ({m})")
    if not done:
        for dst, where in target_hits.items():
            problems.append(f"{dst} still exists ({', '.join(where)}). Delete that test worker first: "
                            f"Workforce > {dst} > Delete permanently.")

    files = scan_files()
    if not done:
        dst_files = [f for f in files if f["dst"]]
        if dst_files:
            problems.append(f"{len(dst_files)} saved DCR file(s) mention {', '.join(sorted({d for f in dst_files for d in f['dst']}))} "
                            f"(e.g. {dst_files[0]['folder']}), so the new numbers would be ambiguous. Needs a manual look.")
        amb = [f for f in files if f["ambiguous"]]
        if amb:
            problems.append(f"{len(amb)} saved DCR file(s) contain an old worker ID inside another code "
                            f"(e.g. {amb[0]['folder']}). Needs a manual look.")

    cards = []
    lookup = dict(MAP)
    for src, eid in sources.items():
        if not eid:
            continue
        if has_column(conn, "cof_cards", "card_number_display"):
            for r in conn.execute("SELECT card_number_display, expires_date FROM cof_cards "
                                  "WHERE employee_id = ? AND status = 'issued'", (eid,)).fetchall():
                cards.append({"worker_id": lookup[src], "old_worker_id": src, "type": "CoF card",
                              "printed": r["card_number_display"], "expires": str(r["expires_date"] or "")[:10]})
        if has_column(conn, "company_id_cards", "card_number_display"):
            for r in conn.execute("SELECT card_number_display FROM company_id_cards "
                                  "WHERE employee_id = ? AND status = 'active'", (eid,)).fetchall():
                cards.append({"worker_id": lookup[src], "old_worker_id": src, "type": "Company ID card",
                              "printed": r["card_number_display"], "expires": ""})

    to_rewrite = [f for f in files if f["src"]]
    basis = {"sources": sources, "changes": changes, "target_hits": target_hits,
             "files": [[f["folder"], f["file"], f["size"], f["mtime_ns"], f["src"]] for f in files]}
    return {
        "map": [{"from": s, "to": d} for s, d in MAP],
        "done": done, "problems": problems, "can_apply": not problems,
        "sources": sources, "rows": changes,
        "dcr_files": len(to_rewrite),
        "dcr_reports": sorted({f["folder"] for f in to_rewrite}),
        "pdfs_to_regenerate": len({f["folder"] for f in to_rewrite if f["has_pdf"]}),
        "cards_to_reissue": cards,
        "plan_hash": hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()[:16],
    }


def _lock(conn):
    if db_layer.is_postgres():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (ACTION,))


def _divergences(conn):
    """Every issued-DCR project's sign-in <-> saved-DCR divergences, as a sorted list."""
    import signin_dcr_reconcile as rec
    out = []
    for r in conn.execute("SELECT DISTINCT project_code FROM report_index WHERE report_type = 'DCR' "
                          "AND status = 'issued' AND dcr_sequence IS NOT NULL").fetchall():
        for d in rec.compute_divergences(conn, r["project_code"]):
            out.append((r["project_code"], str(d["date"]), d["worker_id"], d["class"], d["report_id"]))
    return sorted(out)


def _pending_pdfs(conn):
    row = _audit_row(conn)
    if not row:
        return []
    try:
        folders = (json.loads(row["after_json"] or "{}") or {}).get("pdf_pending") or []
    except ValueError:
        return []
    root = dcr_root()
    return [f for f in folders if _FOLDER_RE.match(f)
            and (root / f / "internal.html").is_file() and not (root / f / "internal.pdf").is_file()]


# ---------------------------------------------------------------- routes
@requires_company   # #347: admin + c_suite
def _api_preview():
    conn = _db()
    try:
        p = plan(conn)
        p["pdfs_remaining"] = len(_pending_pdfs(conn)) if p["done"] else 0
        return jsonify({"data": p})
    finally:
        conn.close()


@requires_company   # #347: admin + c_suite
def _api_apply():
    body = request.get_json(silent=True) or {}
    conn = _db()
    try:
        p = plan(conn)
    finally:
        conn.close()
    if p["problems"]:
        return jsonify({"error": p["problems"][0], "data": p}), 409
    if body.get("plan_hash") != p["plan_hash"]:
        return jsonify({"error": "The data changed since the preview. Preview again.", "data": p}), 409

    anchored, _ = _patterns()
    lookup = dict(MAP)
    root = dcr_root()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = ssc_paths.under_root("data_room", "_maintenance_backup", f"{stamp}-worker-renumber-346")
    staged = []                                   # (target, tmp, backup copy)
    pdf_folders = set()
    for f in scan_files():
        if not f["src"]:
            continue
        target = root / f["folder"] / f["file"]
        keep = backup / f["folder"] / f["file"]
        keep.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, keep)
        pdf = target.parent / "internal.pdf"
        if pdf.is_file() and not (keep.parent / "internal.pdf").exists():
            shutil.copy2(pdf, keep.parent / "internal.pdf")
            pdf_folders.add(f["folder"])
        text = anchored.sub(lambda m: lookup.get(m.group(1), m.group(1)),
                            target.read_text(encoding="utf-8"))
        tmp = target.with_name(target.name + ".w346.tmp")
        tmp.write_text(text, encoding="utf-8")
        staged.append((target, tmp, keep))

    swapped, pdfs_removed = [], []

    def _restore():
        for target, tmp, keep in staged:
            try:
                if target in swapped:
                    shutil.copy2(keep, target)
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                log.exception("#346 file restore failed")
        for folder in pdfs_removed:
            try:
                shutil.copy2(backup / folder / "internal.pdf", root / folder / "internal.pdf")
            except Exception:
                log.exception("#346 PDF restore failed")

    conn = _db()
    try:
        _lock(conn)
        again = plan(conn)
        if again["problems"] or again["plan_hash"] != p["plan_hash"]:
            conn.rollback()
            _restore()
            return jsonify({"error": "The data changed while the files were being prepared. "
                                     "Nothing was changed. Preview again.", "data": again}), 409
        before = [(pc, d, lookup.get(w, w), c, rid) for pc, d, w, c, rid in _divergences(conn)]
        before.sort()
        before_rows = {src: dict(conn.execute(
            "SELECT employee_id, worker_id, trade FROM employees WHERE worker_id = ?", (src,)).fetchone())
            for src in _srcs()}
        for table, col in W_TABLES:
            if not has_column(conn, table, col):
                continue
            for src, dst in MAP:
                if table == "employees":
                    conn.execute("UPDATE employees SET worker_id = ?, updated_at = CURRENT_TIMESTAMP "
                                 "WHERE worker_id = ?", (dst, src))
                else:
                    conn.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (dst, src))
        for target, tmp, _keep in staged:
            os.replace(tmp, target)
            swapped.append(target)
        for folder in sorted(pdf_folders):
            pdf = root / folder / "internal.pdf"
            if pdf.is_file():
                pdf.unlink()                      # never keep a PDF that prints the old IDs
                pdfs_removed.append(folder)
        after = _divergences(conn)
        if after != before:
            conn.rollback()
            _restore()
            log.warning("#346 divergence check disagreed: %d before vs %d after", len(before), len(after))
            return jsonify({"error": "The sign-in / DCR cross-check came out different after the change, "
                                     "so nothing was changed. Needs a manual look.",
                            "data": {"before": len(before), "after": len(after)}}), 409
        user = current_user() or {}
        conn.execute(
            "INSERT INTO audit_log (action, actor_user_id, actor_role, target_type, target_id, "
            "before_json, after_json, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (ACTION, user.get("id"), user.get("role") or "system", "worker", ",".join(_srcs()),
             json.dumps({"map": MAP, "workers": before_rows, "rows": p["rows"]}, default=str),
             json.dumps({"dcr_files": [f"{t.parent.name}/{t.name}" for t, _, _ in staged],
                         "dcr_reports": p["dcr_reports"], "pdf_pending": pdfs_removed,
                         "backup": backup.name, "divergences": len(before)}),
             "One-time renumber " + ", ".join(f"{s} -> {d}" for s, d in MAP) +
             f"; original DCR files kept at data_room/_maintenance_backup/{backup.name}",
             datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        _restore()
        log.exception("#346 worker renumber failed: %s", e)
        return jsonify({"error": "Nothing was changed. The cleanup failed; the server log has the details."}), 500
    finally:
        conn.close()

    return jsonify({"data": {
        "ok": True, "map": p["map"], "rows": p["rows"], "dcr_files": len(staged),
        "pdfs_remaining": len(pdfs_removed), "cards_to_reissue": p["cards_to_reissue"],
        "divergences_unchanged": len(before),
    }})


@requires_company   # #347: admin + c_suite
def _api_pdfs():
    conn = _db()
    try:
        pending = _pending_pdfs(conn)
    finally:
        conn.close()
    root, made, failed = dcr_root(), 0, 0
    error = None
    for folder in pending[:PDF_BATCH]:
        try:
            from pdf_export import render_html_to_pdf
            status = render_html_to_pdf(root / folder / "internal.html", root / folder / "internal.pdf")
        except Exception as e:
            status = {"ok": False, "error": str(e)}
        if status.get("ok"):
            made += 1
        else:
            failed += 1
            error = "The PDF engine could not render a report. The HTML is correct; try again later."
            log.warning("#346 PDF regenerate failed for %s: %s", folder, status.get("error"))
            break
    return jsonify({"data": {"made": made, "failed": failed, "remaining": len(pending) - made, "error": error}})


def register(app) -> None:
    base = "/api/admin/maintenance/worker-renumber-346"
    app.add_url_rule(base, "worker_renumber_346_preview", _api_preview, methods=["GET"])
    app.add_url_rule(base, "worker_renumber_346_apply", _api_apply, methods=["POST"])
    app.add_url_rule(f"{base}/pdfs", "worker_renumber_346_pdfs", _api_pdfs, methods=["POST"])
