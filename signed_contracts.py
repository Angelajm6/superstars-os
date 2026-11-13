"""A signed proposal is the job's contract: it is filed in the job's Documents → Contracts (2026-11-13).

When a signed proposal starts a job (Projects → Ready to start, or an estimate converted), and when a proposal is
signed on an estimate that already is a job, its PDF is copied into the job's documents under CONTRACTS. It fills the
"Executed Owner Contract / AIA" checklist item unless something already does. The PDF is the proposal's own document:
the uploaded original for one from before the app, the app's PDF (with "Accepted by …") for one signed online.

Idempotent: a row remembers the proposal it came from (project_documents.source_proposal_id), so filing twice is a
no-op, and jobs started before this existed are backfilled the first time their documents are opened (ensure_filed).
Never raises into the caller: a job is never refused, and a document list never fails, over this.
"""
from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime

import ssc_paths

log = logging.getLogger(__name__)

REQUIREMENT = "owner_contract"   # "Executed Owner Contract / AIA" (apply_project_docs_schema)


def _columns(conn, table: str) -> set:
    from apply_crm_266 import _columns as cols
    return set(cols(conn, table))


_READY = False


def ensure_schema(conn) -> None:
    """Idempotent: project_documents.source_proposal_id (checked once per process)."""
    global _READY
    if _READY:
        return
    if "source_proposal_id" not in _columns(conn, "project_documents"):
        conn.execute("ALTER TABLE project_documents ADD COLUMN source_proposal_id INTEGER")
        conn.commit()
    _READY = True


def file_signed_proposal(conn, proposal_id: int, *, project_code: str | None = None, actor=None) -> int | None:
    """File one signed proposal into its job. Returns the new document id, or None (not signed, no job yet, already
    filed, or no PDF). Never raises."""
    try:
        import proposals
        ensure_schema(conn)
        r = proposals.get(conn, proposal_id)
        if not r or r.get("status") != "signed":
            return None
        if not project_code:
            row = conn.execute("SELECT converted_project_code AS c FROM estimate WHERE id = ?", (r["estimate_id"],)).fetchone()
            project_code = row and row["c"]
        if not project_code:
            return None
        if conn.execute("SELECT 1 FROM project_documents WHERE project_code = ? AND source_proposal_id = ?",
                        (project_code, r["id"])).fetchone():
            return None
        src = proposals.pdf_file(conn, r["id"])
        if src is None or not src.exists():
            log.info("signed proposal %s has no PDF yet; not filed to %s", r["id"], project_code)
            return None
        base = ssc_paths.under_root("data_room", "project_docs").resolve()
        pdir = ssc_paths.under_root("data_room", "project_docs") / project_code
        if not pdir.resolve().is_relative_to(base):
            return None
        pdir.mkdir(parents=True, exist_ok=True)
        dst = pdir / (uuid.uuid4().hex + ".pdf")
        shutil.copyfile(src, dst)
        label = proposals._label(r)
        taken = conn.execute("SELECT 1 FROM project_documents WHERE project_code = ? AND requirement_key = ? "
                             "AND COALESCE(superseded, 0) = 0", (project_code, REQUIREMENT)).fetchone()
        signed_on = str(r.get("signed_at") or "")[:10] or None
        how = "signed offline" if r.get("signed_title") == "signed offline" else "accepted online"
        title = f"Signed proposal {label}" + (f" — {r['title']}" if r.get("title") else "")
        notes = (f"Signed by {r.get('signed_name') or 'the client'}" + (f" on {signed_on}" if signed_on else "")
                 + f" ({how}). Filed automatically from Proposals.")
        cols = _columns(conn, "project_documents")
        fields = {"project_code": project_code, "category": "CONTRACTS", "requirement_key": None if taken else REQUIREMENT,
                  "title": title[:200], "doc_type": "PDF", "file_path": ssc_paths.store_rel(dst),
                  "file_name": f"Proposal {label} (signed).pdf", "file_size": dst.stat().st_size, "mime": "application/pdf",
                  "effective_date": signed_on, "notes": notes, "superseded": 0, "uploaded_by_uid": actor,
                  "uploaded_at": datetime.now().isoformat(), "source_proposal_id": r["id"]}
        if "type_key" in cols:
            fields["type_key"] = "proposal"
        try:
            cur = conn.execute(f"INSERT INTO project_documents ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                               tuple(fields.values()))
            new_id = cur.lastrowid
            if new_id is None:   # Postgres: no lastrowid
                new_id = conn.execute("SELECT id FROM project_documents WHERE project_code = ? AND source_proposal_id = ?",
                                      (project_code, r["id"])).fetchone()["id"]
            conn.commit()
        except Exception:
            dst.unlink(missing_ok=True)
            raise
        return new_id
    except Exception as e:
        log.warning("signed proposal %s not filed to %s: %s", proposal_id, project_code, type(e).__name__)
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def file_for_estimate(conn, estimate_id: int, project_code: str, *, actor=None) -> list:
    """Every signed proposal of the estimate a job was started from."""
    try:
        ids = [x["id"] for x in conn.execute("SELECT id FROM proposal WHERE estimate_id = ? AND status = 'signed' "
                                             "ORDER BY signed_at", (estimate_id,)).fetchall()]
    except Exception:
        return []
    return [d for d in (file_signed_proposal(conn, i, project_code=project_code, actor=actor) for i in ids) if d]


def ensure_filed(conn, project_code: str) -> list:
    """Self-heal for jobs started before this existed: file any signed proposal of the job's estimate not filed yet.
    A cheap query when there is nothing to do."""
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT p.id FROM proposal p JOIN estimate e ON e.id = p.estimate_id "
            "WHERE e.converted_project_code = ? AND p.status = 'signed' AND NOT EXISTS ("
            "SELECT 1 FROM project_documents d WHERE d.project_code = ? AND d.source_proposal_id = p.id)",
            (project_code, project_code)).fetchall()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []
    return [d for d in (file_signed_proposal(conn, x["id"], project_code=project_code) for x in rows) if d]
