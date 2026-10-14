"""#264 — the per-item VISIBILITY ENGINE (North Star §6). Default-deny, audited.

The single source of truth for "can an external audience see this item." Built once, it
serves client now and design_team + vendor later, identically. Item-type-agnostic:
`item_type='photo'` (v1, -> field_photos.id); 'document' plugs in next with no changes.

Core rules (default-deny BY CONSTRUCTION):
  * An item is shared to an audience IFF a row exists in item_visibility AND the item is
    not red-flagged. NO row => not shared. There is no "share=false" state to forget.
  * RED-FLAG is a sticky "take offline" lever: while flagged, the item is suppressed from
    EVERY audience and new shares are refused (the legal/sensitivity panic button). It is
    reversible (unflag), but unflagging does NOT silently re-share — the operator re-shares
    deliberately.
  * Every share / unshare / redflag / unflag is written to visibility_audit (who, what, when).

All SQL is parameterized and routed through the caller's db_layer connection, so it runs
identically on SQLite (default/production) and Postgres. Dates are LOCAL ISO strings
(CLAUDE.md). This module performs NO authorization — callers gate by role + per-resource
project membership before invoking it.
"""
from __future__ import annotations

from auth import _now_iso

# Audiences (v1). 'design' and 'vendor:<id>' join later with no schema change.
CLIENT = "client"


# ============= STATE QUERIES =============

def is_flagged(conn, item_type, item_id) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM item_redflag WHERE item_type=? AND item_id=?",
        (item_type, item_id)).fetchone())


def is_shared(conn, item_type, item_id, audience) -> bool:
    """True iff the item is shared to `audience` AND not red-flagged. The not-flagged
    guard is belt-and-suspenders: redflag() already removes the share rows, but checking
    here means a red-flagged item can NEVER read as visible even if a row lingered."""
    if is_flagged(conn, item_type, item_id):
        return False
    return bool(conn.execute(
        "SELECT 1 FROM item_visibility WHERE item_type=? AND item_id=? AND audience=?",
        (item_type, item_id, audience)).fetchone())


def audiences_for(conn, item_type, item_id) -> set:
    """The audiences this item is currently shared to (empty if flagged or unshared) —
    for rendering the internal share-state UI."""
    if is_flagged(conn, item_type, item_id):
        return set()
    return {r[0] for r in conn.execute(
        "SELECT audience FROM item_visibility WHERE item_type=? AND item_id=?",
        (item_type, item_id)).fetchall()}


def state(conn, item_type, item_id) -> dict:
    """Compact visibility state for one item (for the internal Field Photos UI)."""
    flagged = is_flagged(conn, item_type, item_id)
    auds = set() if flagged else audiences_for(conn, item_type, item_id)
    return {"shared_client": (CLIENT in auds), "flagged": flagged,
            "audiences": sorted(auds)}


# ============= MUTATIONS (each audited) =============

def _audit(conn, item_type, item_id, audience, action, actor_id):
    conn.execute(
        "INSERT INTO visibility_audit (item_type, item_id, audience, action, actor_id, at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (item_type, item_id, audience, action, actor_id, _now_iso()))


def share(conn, item_type, item_id, audience, actor_id) -> dict:
    """Share an item to `audience`. Refused while the item is red-flagged (clear it first).
    Idempotent. Returns {ok, reason?}. Caller commits."""
    if is_flagged(conn, item_type, item_id):
        return {"ok": False, "reason": "item is red-flagged (offline); clear the flag first"}
    conn.execute(
        "INSERT OR IGNORE INTO item_visibility (item_type, item_id, audience, shared_by, shared_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (item_type, item_id, audience, actor_id, _now_iso()))
    _audit(conn, item_type, item_id, audience, "share", actor_id)
    return {"ok": True}


def unshare(conn, item_type, item_id, audience, actor_id) -> dict:
    """Remove an item from `audience`. Idempotent. Caller commits."""
    conn.execute(
        "DELETE FROM item_visibility WHERE item_type=? AND item_id=? AND audience=?",
        (item_type, item_id, audience))
    _audit(conn, item_type, item_id, audience, "unshare", actor_id)
    return {"ok": True}


def redflag(conn, item_type, item_id, actor_id) -> dict:
    """TAKE OFFLINE — instantly revoke the item from EVERY external audience and mark it
    flagged so it can't be re-shared until cleared. Idempotent. Caller commits."""
    conn.execute(
        "INSERT OR IGNORE INTO item_redflag (item_type, item_id, flagged_by, flagged_at) "
        "VALUES (?, ?, ?, ?)",
        (item_type, item_id, actor_id, _now_iso()))
    # instant revoke from all audiences
    conn.execute("DELETE FROM item_visibility WHERE item_type=? AND item_id=?",
                 (item_type, item_id))
    _audit(conn, item_type, item_id, None, "redflag", actor_id)
    return {"ok": True}


def unflag(conn, item_type, item_id, actor_id) -> dict:
    """Clear a red-flag (reversible). Does NOT re-share — the operator re-shares deliberately.
    Caller commits."""
    conn.execute("DELETE FROM item_redflag WHERE item_type=? AND item_id=?",
                 (item_type, item_id))
    _audit(conn, item_type, item_id, None, "unflag", actor_id)
    return {"ok": True}


# ============= PHOTO CONVENIENCE (v1) — joins to field_photos =============

def client_visible_photo_ids(conn, project_code) -> list:
    """The field_photo ids in `project_code` that are shared to the client audience AND
    not red-flagged — the ONLY photos a client may see for that project (default-deny)."""
    rows = conn.execute(
        "SELECT fp.id FROM field_photos fp "
        "JOIN item_visibility v ON v.item_type='photo' AND v.item_id=fp.id AND v.audience=? "
        "LEFT JOIN item_redflag rf ON rf.item_type='photo' AND rf.item_id=fp.id "
        "WHERE fp.project_code=? AND rf.id IS NULL AND COALESCE(fp.mime, '') NOT LIKE 'video/%' "
        "ORDER BY fp.taken_at DESC, fp.id DESC",
        (CLIENT, project_code)).fetchall()
    return [r[0] for r in rows]


def photo_visible_to_client(conn, photo_id, project_code) -> bool:
    """Per-resource isolation gate for a client by-ID photo fetch: True ONLY if the photo
    BELONGS to `project_code` AND is shared to the client audience AND is not red-flagged.
    project_code-in-path is never trusted alone — this re-derives ownership from the row.
    Returns False for any other-project / unshared / flagged / nonexistent id."""
    return bool(conn.execute(
        "SELECT 1 FROM field_photos fp "
        "JOIN item_visibility v ON v.item_type='photo' AND v.item_id=fp.id AND v.audience=? "
        "LEFT JOIN item_redflag rf ON rf.item_type='photo' AND rf.item_id=fp.id "
        "WHERE fp.id=? AND fp.project_code=? AND rf.id IS NULL AND COALESCE(fp.mime, '') NOT LIKE 'video/%'",
        (CLIENT, photo_id, project_code)).fetchone())


# ============= DOCUMENT CONVENIENCE (#269) — joins to project_documents =============
# Documents plug into the SAME engine (item_type='document') exactly as #264 promised:
# default-deny, red-flag, audit — all inherited. Superseded versions are excluded from
# the client view even if a share row lingers (the operator shares the CURRENT doc).

def client_visible_document_ids(conn, project_code) -> list:
    """The project_documents ids in `project_code` shared to the client audience, not
    red-flagged, and not superseded — the ONLY documents a client may see (default-deny)."""
    rows = conn.execute(
        "SELECT pd.id FROM project_documents pd "
        "JOIN item_visibility v ON v.item_type='document' AND v.item_id=pd.id AND v.audience=? "
        "LEFT JOIN item_redflag rf ON rf.item_type='document' AND rf.item_id=pd.id "
        "WHERE pd.project_code=? AND rf.id IS NULL AND COALESCE(pd.superseded,0)=0 "
        "ORDER BY pd.uploaded_at DESC, pd.id DESC",
        (CLIENT, project_code)).fetchall()
    return [r[0] for r in rows]


def document_visible_to_client(conn, doc_id, project_code) -> bool:
    """Per-resource isolation gate for a client by-ID document fetch: True ONLY if the
    document BELONGS to `project_code` AND is shared to the client audience AND is not
    red-flagged AND is not superseded. Same posture as photo_visible_to_client — False
    for any other-project / unshared / flagged / superseded / nonexistent id."""
    return bool(conn.execute(
        "SELECT 1 FROM project_documents pd "
        "JOIN item_visibility v ON v.item_type='document' AND v.item_id=pd.id AND v.audience=? "
        "LEFT JOIN item_redflag rf ON rf.item_type='document' AND rf.item_id=pd.id "
        "WHERE pd.id=? AND pd.project_code=? AND rf.id IS NULL AND COALESCE(pd.superseded,0)=0",
        (CLIENT, doc_id, project_code)).fetchone())


def document_states(conn, project_code) -> dict:
    """{doc_id: {shared_client, flagged}} for every document in a project — batched, so
    the internal Documents list can annotate share state without N lookups."""
    shared = {r[0] for r in conn.execute(
        "SELECT v.item_id FROM item_visibility v JOIN project_documents pd ON pd.id=v.item_id "
        "WHERE v.item_type='document' AND v.audience=? AND pd.project_code=?",
        (CLIENT, project_code)).fetchall()}
    flagged = {r[0] for r in conn.execute(
        "SELECT rf.item_id FROM item_redflag rf JOIN project_documents pd ON pd.id=rf.item_id "
        "WHERE rf.item_type='document' AND pd.project_code=?",
        (project_code,)).fetchall()}
    ids = shared | flagged
    return {did: {"shared_client": (did in shared and did not in flagged),
                  "flagged": (did in flagged)} for did in ids}


def photo_states(conn, project_code) -> dict:
    """{photo_id: {shared_client, flagged}} for every photo in a project — one batched pair
    of queries so the internal Field Photos list can annotate each card without N lookups."""
    shared = {r[0] for r in conn.execute(
        "SELECT v.item_id FROM item_visibility v JOIN field_photos fp ON fp.id=v.item_id "
        "WHERE v.item_type='photo' AND v.audience=? AND fp.project_code=?",
        (CLIENT, project_code)).fetchall()}
    flagged = {r[0] for r in conn.execute(
        "SELECT rf.item_id FROM item_redflag rf JOIN field_photos fp ON fp.id=rf.item_id "
        "WHERE rf.item_type='photo' AND fp.project_code=?",
        (project_code,)).fetchall()}
    ids = shared | flagged
    return {pid: {"shared_client": (pid in shared and pid not in flagged),
                  "flagged": (pid in flagged)} for pid in ids}
