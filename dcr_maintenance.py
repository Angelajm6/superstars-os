"""#344 — DCR numbering: the register, and one pinned correction.

A DCR's number is its ISSUE order (next = MAX+1 per project, see
server.next_dcr_sequence). The Daily Report list is windowed by date, so the
things that confuse numbering — gaps, reports issued out of date order,
future-dated rows — were invisible. The register shows every report by number
and flags them (company roles only).

The correction is ONE pinned action (admin + c_suite since #347): FR-BX-001's DCR 109 becomes 097.
It is not a general "renumber" tool on purpose — numbers are permanent identity.
It runs inside the app because the production database is only reachable from
there: preview first (shows exactly what is in 097–108 and whether 097 is free),
then apply with the preview's plan_hash. It refuses if the number is taken, if the
files aren't on this server's disk, if anything changed since the preview, or if it
already ran. Files are staged, the original folder is kept in a non-served backup,
and an audit row records every before/after.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from datetime import date, datetime

from flask import jsonify, request

import db_layer
import ssc_paths
from auth import _db, current_user, requires_company, requires_role

log = logging.getLogger(__name__)

FIX_ACTION = "dcr_renumber_344"
FIX_PROJECT = "FR-BX-001"
FIX_FROM = 109
FIX_TO = 97
_FILES = ("internal.html", "client.html", "internal.pdf")


# ---------------------------------------------------------------- shared helpers
def number_lock(conn, project_code):
    """Serialize DCR numbering per project until this transaction ends. Without it
    two issues in flight read the same MAX and both write NNN+1. Postgres
    (production) takes a transaction-scoped advisory lock; SQLite serializes
    writers itself. Taken by the issue route and by the correction below."""
    if db_layer.is_postgres():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"dcr-seq:{project_code}",))


def audit(conn, action, target_id, before=None, after=None, note=None):
    """One audit_log row for a DCR action. The caller commits."""
    user = current_user() or {}
    conn.execute(
        "INSERT INTO audit_log (action, actor_user_id, actor_role, target_type, "
        "target_id, before_json, after_json, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (action, user.get("id"), user.get("role") or "system", "dcr", target_id,
         json.dumps(before, default=str) if before is not None else None,
         json.dumps(after, default=str) if after is not None else None,
         note, datetime.now().isoformat(timespec="seconds")))


def seq_dir(project_code, seq):
    return ssc_paths.under_root("data_room", "reports", "dcr", project_code, f"{seq:03d}")


def _iso(v):
    return str(v)[:10] if v is not None else None


def _audience(report_id):
    rid = report_id or ""
    for a in ("internal", "client"):
        if rid.endswith(f"-{a}"):
            return a
    return None


# ---------------------------------------------------------------- the register
def _ranges(nums):
    """[97, 98, 99, 104] -> ['097–099', '104']"""
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start:03d}" if start == prev else f"{start:03d}–{prev:03d}")
            start = prev = n
    if start is not None:
        out.append(f"{start:03d}" if start == prev else f"{start:03d}–{prev:03d}")
    return out


def dcr_register(conn, project_code):
    rows = conn.execute(
        "SELECT report_id, report_date, dcr_sequence, status, created_at, updated_at, no_work "
        "FROM report_index WHERE project_code = ? AND report_type = 'DCR' "
        "AND dcr_sequence IS NOT NULL ORDER BY dcr_sequence, id", (project_code,)).fetchall()
    by_seq = {}
    for r in rows:
        d = dict(r)
        seq = int(d["dcr_sequence"])
        e = by_seq.setdefault(seq, {
            "sequence": seq, "display_id": f"DCR-{project_code}-{seq:03d}",
            "dates": set(), "audiences": [], "status": d.get("status"),
            "created_at": None, "updated_at": None, "no_work": 0})
        e["dates"].add(_iso(d.get("report_date")))
        aud = _audience(d.get("report_id"))
        if aud:
            e["audiences"].append(aud)
        if aud == "internal" or e["created_at"] is None:
            e["created_at"] = str(d.get("created_at") or "") or None
            e["updated_at"] = str(d.get("updated_at") or "") or None
        e["no_work"] = e["no_work"] or (d.get("no_work") or 0)

    today = date.today().isoformat()
    out, future, out_of_order, split_dates = [], [], [], []
    latest_so_far = None
    for seq in sorted(by_seq):
        e = by_seq[seq]
        dates = sorted(x for x in e.pop("dates") if x)
        e["report_date"] = dates[0] if dates else None
        if len(dates) > 1:
            split_dates.append(seq)
        rd = e["report_date"]
        e["future_dated"] = bool(rd and rd > today)
        e["out_of_order"] = bool(rd and latest_so_far and rd < latest_so_far)
        if e["future_dated"]:
            future.append(seq)
        if e["out_of_order"]:
            out_of_order.append(seq)
        if rd and (latest_so_far is None or rd > latest_so_far):
            latest_so_far = rd
        e["has_pdf"] = (seq_dir(project_code, seq) / "internal.pdf").exists()
        out.append(e)

    top = max(by_seq) if by_seq else 0
    missing = [n for n in range(1, top + 1) if n not in by_seq]
    return {
        "reports": out,
        "total": len(out),
        "max_sequence": top,
        "next_sequence": top + 1,
        "anomalies": {
            "missing": missing, "missing_ranges": _ranges(missing),
            "out_of_order": out_of_order, "future_dated": future,
            "split_dates": split_dates,
            "count": len(missing) + len(out_of_order) + len(future) + len(split_dates),
        },
    }


@requires_company
def _api_register(project_code):
    conn = _db()
    try:
        if not conn.execute("SELECT 1 FROM projects WHERE project_code = ?", (project_code,)).fetchone():
            return jsonify({"error": "Project not found"}), 404
        return jsonify({"data": dcr_register(conn, project_code)})
    finally:
        conn.close()


# ---------------------------------------------------------------- the pinned correction
def _file_facts(d):
    return {n: {"size": (d / n).stat().st_size, "mtime_ns": (d / n).stat().st_mtime_ns}
            for n in _FILES if (d / n).exists()}


def fix_plan(conn):
    """Everything the correction would do, and every reason it must not."""
    code, src, dst = FIX_PROJECT, FIX_FROM, FIX_TO
    old_id, new_id = f"DCR-{code}-{src:03d}", f"DCR-{code}-{dst:03d}"
    done = conn.execute(
        "SELECT created_at, actor_user_id FROM audit_log WHERE action = ? ORDER BY id LIMIT 1",
        (FIX_ACTION,)).fetchone()
    source = [dict(r) for r in conn.execute(
        "SELECT id, report_id, report_date, status, created_at FROM report_index "
        "WHERE project_code = ? AND report_type = 'DCR' AND dcr_sequence = ? ORDER BY id",
        (code, src)).fetchall()]
    target = [dict(r) for r in conn.execute(
        "SELECT report_id, report_date, created_at FROM report_index "
        "WHERE project_code = ? AND report_type = 'DCR' AND dcr_sequence = ? ORDER BY id",
        (code, dst)).fetchall()]
    between = [{"sequence": r["dcr_sequence"], "display_id": f"DCR-{code}-{int(r['dcr_sequence']):03d}",
                "report_date": _iso(r["report_date"]), "created_at": str(r["created_at"] or "")}
               for r in conn.execute(
                   "SELECT dcr_sequence, report_date, created_at FROM report_index "
                   "WHERE project_code = ? AND report_type = 'DCR' AND report_id LIKE ? "
                   "AND dcr_sequence > ? AND dcr_sequence < ? ORDER BY dcr_sequence",
                   (code, "%-internal", dst, src)).fetchall()]
    src_dir, dst_dir = seq_dir(code, src), seq_dir(code, dst)
    files = _file_facts(src_dir) if src_dir.exists() else {}
    pat = re.compile(re.escape(old_id) + r"(?!\d)")
    id_hits = {n: len(pat.findall((src_dir / n).read_text(encoding="utf-8", errors="replace")))
               for n in ("internal.html", "client.html") if n in files}

    problems = []
    if done:
        problems.append(f"Already done on {str(done['created_at'])[:16]} — this correction runs once.")
    else:
        if not source:
            problems.append(f"There is no {old_id} in this database.")
        if target:
            problems.append(f"{new_id} already exists (dated {_iso(target[0]['report_date'])}), "
                            f"so its number is taken. Nothing will change.")
        if dst_dir.exists():
            problems.append(f"A folder for {new_id} is already on this server's disk.")
        if source and "internal.html" not in files:
            problems.append(f"{old_id}'s report files are not on this server's disk, so the report "
                            f"can't be moved from here.")
        if source and files.get("internal.html") and not id_hits.get("internal.html"):
            problems.append(f"{old_id}'s report doesn't contain its own number — not touching it.")
    plan = {
        "project_code": code, "from": old_id, "to": new_id,
        "source_rows": [{"report_id": r["report_id"], "report_date": _iso(r["report_date"]),
                         "created_at": str(r["created_at"] or "")} for r in source],
        "target_taken": bool(target), "between": between,
        "files": files, "id_hits": id_hits,
    }
    plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True, default=str).encode()).hexdigest()[:16]
    for f in plan["files"].values():          # the UI needs sizes, not mtimes
        f.pop("mtime_ns", None)
    return {**plan, "done": bool(done), "problems": problems,
            "can_apply": not problems, "plan_hash": plan_hash}


@requires_company   # #347: admin + c_suite (pinned, guarded, runs once)
def _api_fix_preview():
    conn = _db()
    try:
        return jsonify({"data": fix_plan(conn)})
    finally:
        conn.close()


@requires_company   # #347: admin + c_suite (pinned, guarded, runs once)
def _api_fix_apply():
    body = request.get_json(silent=True) or {}
    code, src, dst = FIX_PROJECT, FIX_FROM, FIX_TO
    old_id, new_id = f"DCR-{code}-{src:03d}", f"DCR-{code}-{dst:03d}"

    # 1) quick check on its own connection, before the slow file work
    conn = _db()
    try:
        plan = fix_plan(conn)
    finally:
        conn.close()
    if plan["problems"]:
        return jsonify({"error": plan["problems"][0], "data": plan}), 409
    if body.get("plan_hash") != plan["plan_hash"]:
        return jsonify({"error": "The data changed since the preview — preview again.", "data": plan}), 409

    # 2) stage the renumbered copy (slow: PDF render) without holding any lock
    src_dir, dst_dir = seq_dir(code, src), seq_dir(code, dst)
    staging = dst_dir.with_name(f"{dst:03d}.tmp")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(src_dir, staging)
    pat = re.compile(re.escape(old_id) + r"(?!\d)")
    replaced = {}
    for name in ("internal.html", "client.html"):
        p = staging / name
        if p.exists():
            text, n = pat.subn(new_id, p.read_text(encoding="utf-8"))
            p.write_text(text, encoding="utf-8")
            replaced[name] = n
    pdf = staging / "internal.pdf"
    if pdf.exists():
        pdf.unlink()                          # never keep a PDF that still says 109
    try:
        from pdf_export import render_html_to_pdf
        pdf_status = render_html_to_pdf(staging / "internal.html", pdf)
    except Exception as e:                    # PDF is derivative; HTML is the record
        pdf_status = {"ok": False, "error": str(e)}
    if not pdf_status.get("ok") and pdf.exists():
        pdf.unlink()

    # 3) under the numbering lock: re-verify, rewrite the rows, swap the folders, commit
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = ssc_paths.under_root("data_room", "_maintenance_backup",
                                  f"{stamp}-dcr-renumber-{code}-{src:03d}-to-{dst:03d}")
    conn = _db()
    moved_src = placed_dst = False
    try:
        number_lock(conn, code)
        again = fix_plan(conn)
        if again["problems"] or again["plan_hash"] != plan["plan_hash"]:
            conn.rollback()
            shutil.rmtree(staging, ignore_errors=True)
            return jsonify({"error": "The data changed while the report was being prepared — "
                                     "nothing was changed. Preview again.", "data": again}), 409
        before = [dict(r) for r in conn.execute(
            "SELECT * FROM report_index WHERE project_code = ? AND report_type = 'DCR' "
            "AND dcr_sequence = ?", (code, src)).fetchall()]
        after = []
        for r in before:
            aud = _audience(r["report_id"])
            new_rid = f"DCR-{code}-{dst:03d}-{aud}" if aud else r["report_id"]
            conn.execute("UPDATE report_index SET dcr_sequence = ?, report_id = ? WHERE id = ?",
                         (dst, new_rid, r["id"]))
            after.append({"id": r["id"], "report_id": new_rid, "dcr_sequence": dst})
        audit(conn, FIX_ACTION, old_id, before=before,
              after={"rows": after, "replaced": replaced, "pdf_ok": bool(pdf_status.get("ok"))},
              note=f"{old_id} -> {new_id}; original folder kept at "
                   f"data_room/_maintenance_backup/{backup.name}")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_dir), str(backup))   # the untouched original, kept
        moved_src = True
        staging.rename(dst_dir)
        placed_dst = True
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        try:                                  # put the files back the way they were
            if placed_dst and dst_dir.exists():
                shutil.rmtree(dst_dir)
            if moved_src and backup.exists() and not src_dir.exists():
                shutil.move(str(backup), str(src_dir))
            shutil.rmtree(staging, ignore_errors=True)
        except Exception:
            log.exception("#344 correction: file rollback failed")
        log.exception("#344 correction failed: %s", e)
        return jsonify({"error": "Nothing was changed — the correction failed. "
                                 "The server log has the details."}), 500
    finally:
        conn.close()

    if not pdf_status.get("ok"):
        log.warning("#344 correction: PDF not regenerated: %s", pdf_status.get("error"))
    return jsonify({"data": {
        "ok": True, "from": old_id, "to": new_id, "rows": len(after),
        "number_replacements": replaced, "pdf_ok": bool(pdf_status.get("ok")),
    }})


def register(app) -> None:
    app.add_url_rule("/api/projects/<project_code>/dcr-register", "dcr_register",
                     _api_register, methods=["GET"])
    app.add_url_rule("/api/admin/maintenance/dcr-renumber-344", "dcr_fix_preview",
                     _api_fix_preview, methods=["GET"])
    app.add_url_rule("/api/admin/maintenance/dcr-renumber-344", "dcr_fix_apply",
                     _api_fix_apply, methods=["POST"])
