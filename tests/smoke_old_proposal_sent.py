#!/usr/bin/env python3
"""Proposals → Before the app → Mark as sent (2026-10-27).

Boots the real app (`import server`) on an ISOLATED database. Proves:
  * an upload from before the app is listed (status=archived) until it is marked as sent, then leaves that list
  * POST /api/proposals/archived/<id>/mark-sent refuses a bad number, a future day, no total, no type / location, an
    approval dated before the send, and a number another proposal already has (409); an unknown upload -> 404
  * marked as sent: a sent proposal with the number people see, sent on the day given (noon), at its total; a client
    name the app doesn't know is added to Clients; the estimate is submitted at that total
  * the proposal's PDF is the uploaded PDF, byte for byte (never redrawn); `original_pdf` is true; a second try -> 409
  * with approved_by it is signed offline and Projects → Ready to start offers it
  * Delete in the ⋯ menu (2026-10-27): only an admin (C-suite -> 403); the upload leaves Before the app, is listed under
    Deleted, is still one of Claude's examples, and Restore brings it back; a second delete / restore -> 409
  * Mark signed from the list (2026-10-27): a draft (its sent date becomes the signing day) and a declined latest version
    can be signed; a future day or a day before it was sent -> 400; a declined version with a newer one -> 409
  * Proposal Studio → Archive: the uploads carry proposal_id once marked; the ⋯ menu links to the portal's form
    (#proposals/before/<id>/sent|signed), which the portal opens
  * the served page has the modal, the menu items and the calls, and no button under each row
Run:  venv/bin/python tests/smoke_old_proposal_sent.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_oldsent_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ["SSC_MAIL_FAKE_DIR"] = str(Path(_TMP) / "mail")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import crm  # noqa: E402
import estimates  # noqa: E402
import db_layer  # noqa: E402
import proposal_template  # noqa: E402
import proposals  # noqa: E402
import ssc_paths  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "olds"
PW = "OldProposalSentSmoke2026"
PDF = b"%PDF-1.4\n% Olds original proposal\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
KNOWN, UNKNOWN = "Olds Gardens Co-op", "Olds Ashford Management"


def _db():
    return db_layer.connect(pragma_fk=False)


def _seed() -> dict:
    conn = _db()
    try:
        for role in ("c_suite", "admin"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, 'Smoke Olds', 1, 'active', 0, 1)", (email, auth.hash_password(PW), role))
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Olds %'")
        crm.create_org(conn, name=KNOWN, relationship_type="client")
        conn.commit()
        d = ssc_paths.under_root("proposal_templates")
        d.mkdir(parents=True, exist_ok=True)
        (d / "olds-a.pdf").write_bytes(PDF)
        (d / "olds-b.pdf").write_bytes(PDF + b"% b\n")
        (d / "olds-c.pdf").write_bytes(PDF + b"% c\n")
        before = proposal_template._get_raw(conn)
        st = proposal_template._store(conn)
        base = st["next_id"]
        for i, (f, no, client, addr, total, day) in enumerate((
                ("olds-a.pdf", "0961", UNKNOWN, "155 E 31st St, New York", 78400, "2026-03-03"),
                ("olds-b.pdf", "0987", KNOWN, "3530 Olds Hudson Pkwy, Bronx", 212750, "2026-05-12"),
                ("olds-c.pdf", "0950", KNOWN, "9 Olds Duplicate St, Bronx", 5000, "2026-02-02"))):
            st["templates"].append({"id": base + i, "name": f, "path": ssc_paths.store_rel(d / f), "ext": ".pdf", "state": "ready",
                                    "uploaded_at": "2026-10-21T09:00:00", "example": {"title": f"Olds repairs {i}"},
                                    "archive": {"number": no, "date": day, "client": client, "building": addr, "total": total}})
        st["next_id"] = base + 3
        proposal_template._put(conn, st)
    finally:
        conn.close()
    return {"a": base, "b": base + 1, "c": base + 2, "before": before}


def _cleanup(ids):
    conn = _db()
    try:
        proposal_template._put(conn, ids["before"] or None)
        org_ids = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Olds %'").fetchall()]
        ph = ",".join("?" * len(org_ids)) or "NULL"
        est_ids = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph})", org_ids).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE client_org_id IN ({ph})", org_ids).fetchall()]
        inv = [r["id"] for r in conn.execute(f"SELECT id FROM invoice WHERE client_org_id IN ({ph})", org_ids).fetchall()]
        purge(conn, "invoice", "id", inv)
        for t in ("proposal_line", "proposal_event", "proposal_revision"):
            purge(conn, t, "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate_document", "estimate_id", est_ids)
        purge(conn, "estimate", "id", est_ids)
        conn.execute("DELETE FROM building WHERE address_line LIKE '%Olds%' OR address_line LIKE '155 E 31st%'")
        for t, col in (("crm_activity", "entity_id"), ("crm_task", "entity_id"), ("crm_contact", "org_id")):
            purge(conn, t, col, org_ids)
        purge(conn, "crm_organization", "id", org_ids)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def main() -> int:
    ids = _seed()
    passed = 0
    c, adm = server.app.test_client(), server.app.test_client()
    assert c.post("/api/auth/login", json={"email": f"{TAG}.c_suite@example.com", "password": PW}).status_code == 200
    assert adm.post("/api/auth/login", json={"email": f"{TAG}.admin@example.com", "password": PW}).status_code == 200
    arch = lambda: {r["archive_id"] for r in c.get("/api/proposals?status=archived").get_json()["data"]}   # noqa: E731
    post = lambda tid, **kw: c.post(f"/api/proposals/archived/{tid}/mark-sent", json=kw)   # noqa: E731
    good = dict(number="0961", sent_on="2026-03-03", client=UNKNOWN, building="155 E 31st St, New York", total="78,400.00",
                est_type="FR", borough="MN")
    try:
        assert {ids["a"], ids["b"]} <= arch(), "both uploads are listed under Before the app"
        passed += 1

        for bad, want in ((dict(number="abc"), 400), (dict(sent_on="2099-01-01"), 400), (dict(total="0"), 400),
                          (dict(est_type="ZZ"), 400), (dict(borough=""), 400),
                          (dict(approved_by="Pat Signer", approved_on="2026-03-01"), 400)):
            r = post(ids["a"], **dict(good, **bad))
            assert r.status_code == want, (bad, r.status_code, r.get_json())
        assert post(999999, **good).status_code == 404
        passed += 1

        r = post(ids["a"], **good)
        assert r.status_code == 201, r.get_json()
        p = r.get_json()["data"]
        assert p["status"] == "sent" and p["label"].startswith("961") and p["sent_at"] == "2026-03-03T12:00:00", p
        assert abs(float(p["price"]) - 78400) < 0.01 and p["original_pdf"] is True and p["title"] == "Olds repairs 0", p
        assert p["estimate_status"] == "submitted" and p["estimate_code"].startswith("FR-MN-"), p
        conn = _db()
        try:
            assert conn.execute("SELECT COUNT(*) AS n FROM crm_organization WHERE name = ?", (UNKNOWN,)).fetchone()["n"] == 1
        finally:
            conn.close()
        assert ids["a"] not in arch() and ids["b"] in arch(), "the marked one leaves Before the app"
        passed += 1

        pdf = c.get(f"/api/proposals/{p['id']}/pdf")
        assert pdf.status_code == 200 and pdf.data == PDF, "the original PDF is served as it was uploaded"
        assert post(ids["a"], **good).status_code == 409, "a second try is refused"
        passed += 1

        r = post(ids["b"], number="961", sent_on="2026-05-12", client=KNOWN, total="212750", est_type="FR", borough="BX")
        assert r.status_code == 409 and "already exists" in r.get_json()["error"], r.get_json()
        r = post(ids["b"], number="0987", sent_on="2026-05-12", client=KNOWN.upper(), building="3530 Olds Hudson Pkwy, Bronx",
                 total="212750", est_type="FR", borough="BX", approved_by="Pat Signer", approved_on="2026-10-20")
        assert r.status_code == 201, r.get_json()
        q = r.get_json()["data"]
        assert q["status"] == "signed" and q["signed_name"] == "Pat Signer" and q["signed_at"].startswith("2026-10-20"), q
        conn = _db()
        try:
            assert conn.execute("SELECT COUNT(*) AS n FROM crm_organization WHERE name LIKE 'Olds Gardens%'").fetchone()["n"] == 1, \
                "a known client (any case) is reused"
        finally:
            conn.close()
        ready = {x["proposal_id"] for x in c.get("/api/company/portal/projects?status=all").get_json()["meta"]["ready_to_start"]}
        assert q["id"] in ready and p["id"] not in ready, "only the approved one is ready to start"
        passed += 1

        cid = ids["c"]
        assert c.delete(f"/api/proposals/archived/{cid}").status_code == 403, "only an admin deletes"
        assert adm.delete(f"/api/proposals/archived/{cid}").status_code == 200
        assert cid not in arch() and adm.delete(f"/api/proposals/archived/{cid}").status_code == 409
        gone = [r for r in adm.get("/api/proposals?status=deleted").get_json()["data"] if r.get("archive_id") == cid]
        assert len(gone) == 1 and gone[0]["hidden"] is True and gone[0]["archived"] is True, gone
        import proposal_engine
        conn = _db()
        try:
            assert any(e.get("_from") == "olds-c.pdf" for e in proposal_engine.examples(conn)), "Claude still learns from it"
        finally:
            conn.close()
        assert c.post(f"/api/proposals/archived/{cid}/restore", json={}).status_code == 403
        assert adm.post(f"/api/proposals/archived/{cid}/restore", json={}).status_code == 200 and cid in arch()
        assert adm.post(f"/api/proposals/archived/{cid}/restore", json={}).status_code == 409
        assert adm.delete(f"/api/proposals/archived/{ids['a']}").status_code == 404, "one marked as sent is a proposal now"
        passed += 1

        conn = _db()
        try:
            org = conn.execute("SELECT id FROM crm_organization WHERE name = ?", (KNOWN,)).fetchone()["id"]
            uid = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.admin@example.com",)).fetchone()["id"]
            made = []
            for addr in ("1 Olds Sign St", "2 Olds Sign St", "3 Olds Sign St"):
                est = estimates.create_estimate(conn, est_type="FR", borough="BX", client_org_id=org, building_address=addr, created_by=uid)
                estimates.change_status(conn, est["id"], "scoping", actor_user_id=uid)
                estimates.change_status(conn, est["id"], "submitted", final_amount=10000, actor_user_id=uid)
                made.append(proposals.create_from_estimate(conn, est["id"], created_by=uid))
            draft, dec, old = made
            conn.execute("UPDATE proposal SET status = 'declined', sent_at = '2026-10-01T12:00:00' WHERE id IN (?, ?)", (dec["id"], old["id"]))
            conn.commit()
            v2 = proposals.create_from_estimate(conn, old["estimate_id"], created_by=uid)   # old now has a newer version
        finally:
            conn.close()
        sign = lambda pid, **kw: c.post(f"/api/proposals/{pid}/mark-signed", json=dict({"name": "Pat Signer"}, **kw))   # noqa: E731
        assert sign(draft["id"], signed_on="2099-01-01").status_code == 400
        assert sign(dec["id"], signed_on="2026-09-30").status_code == 400, "signed before it was sent"
        assert sign(old["id"]).status_code == 409, "a declined version with a newer one"
        r = sign(draft["id"], signed_on="2026-10-21")
        assert r.status_code == 200 and r.get_json()["data"]["status"] == "signed", r.get_json()
        assert r.get_json()["data"]["sent_at"] == "2026-10-21T12:00:00", r.get_json()["data"]["sent_at"]
        r = sign(dec["id"])
        assert r.status_code == 200 and r.get_json()["data"]["status"] == "signed", r.get_json()
        assert v2["status"] == "draft"
        passed += 1

        tpl = {t["id"]: t for t in c.get("/api/proposal-template").get_json()["data"]["templates"]}
        assert tpl[ids["a"]]["proposal_id"] == p["id"] and not tpl[ids["c"]]["proposal_id"], "Studio knows which are in the list"
        studio = c.get("/proposal-studio").get_data(as_text=True)
        for needle in ("/company#proposals/before/", 'data-act="sent"', 'data-act="signed"', "Open in Proposals"):
            assert needle in studio, needle

        page = c.get("/company").get_data(as_text=True)
        for needle in ('id="pr-arch-form"', "/proposals/archived/", 'id="pr-arch-ok"', "original_pdf", 'data-m="archdel"',
                       'data-m="archrestore"', "prxArchConfirm", 'data-m="marksigned"', 'data-m="archsigned"', "prxSignable", "prArchFromLink"):
            assert needle in page, needle
        assert "data-prx-arch" not in page, "no Mark as sent button under each row"
        passed += 1
    finally:
        _cleanup(ids)
    print(f"smoke_old_proposal_sent: {passed}/8 checks passed")
    return 0 if passed == 8 else 1


if __name__ == "__main__":
    sys.exit(main())
