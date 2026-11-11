#!/usr/bin/env python3
"""Claude's changes, kept (proposal_history.py; accepted prototype https://claude.ai/artifact/SJpPbUkDHenqQ9BfHECvnT),
and the optional Warranty row.

Proves against an ISOLATED database (fictional client, fake model):
  * each Claude change in the Studio is kept with a summary; the editor's view lists them, newest first, after a reload
  * Undo (by version) puts back the version before the change and is itself a step, so it can be undone (redo)
  * going back to an older change undoes every later one and says how many
  * a version of another proposal, an unknown one, or a sent proposal is refused
  * a change through Claude in claude.ai (connector) is listed too, and its undo is a step
  * the list keeps at most KEEP rows per proposal
  * Warranty: blank, its row is hidden in the editor with "+ Warranty", and left out of the PDF; filled, it shows
  * the page carries History, the top-bar Undo, the version Undo and the Warranty buttons
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_phist_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(Path(_TMP) / "mail"),
                   "SSC_GMAIL_POLL": "0", "SSC_REMINDERS": "0", "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(),
                   "SSC_ASSISTANT_FAKE": "1"})

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import claude_connector  # noqa: E402
import db_layer  # noqa: E402
import proposal_engine as pe  # noqa: E402
import proposal_history as ph  # noqa: E402
import proposal_layout  # noqa: E402
import proposal_studio  # noqa: E402
import proposals  # noqa: E402

TAG = "phist"
PW = "ProposalHistorySmokePassword2026"
CLIENT = "Phist Smoke Holdings"


def _db():
    return db_layer.connect(pragma_fk=True)


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        proposals.ensure_schema(conn)
        ph.ensure(conn)
        claude_connector.ensure(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph_ = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE ?", ("Phist Smoke%",)).fetchall()]
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph_(orgs)})", orgs).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph_(ests)})", ests).fetchall()]
        purge(conn, "proposal_version", "proposal_id", pids)
        purge(conn, "claude_connector_snapshot", "proposal_id", pids)
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate", "id", ests)
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph_(orgs)})", orgs)
        purge(conn, "crm_task", "entity_id", orgs)
        conn.execute("DELETE FROM building WHERE address_line LIKE '%Phist Terrace%'")
        conn.execute("DELETE FROM crm_organization WHERE name LIKE ?", ("Phist Smoke%",))
        conn.commit()
        purge_users(conn, uids)
    finally:
        conn.close()


def _setup():
    conn = _db()
    try:
        conn.execute("INSERT INTO users (email, password_hash, full_name, role, status, must_reset_password, created_at) "
                     "VALUES (?,?,?,?,'active',0,?)", (f"{TAG}.admin@superstarscontracting.com", auth.hash_password(PW),
                                                       "Phist Admin", "admin", datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.admin@superstarscontracting.com",)).fetchone()["id"]
        d = {"title": "Phist parapet repair", "overview": "Repair the parapets.", "submitted_to": [CLIENT],
             "project": ["31 Phist Terrace, New York, NY"], "notes": ["Phist note one."], "exclusions": [],
             "lines": [{"lead": "Parapet repair", "detail": "rebuild coping", "qty": 42, "unit": "LF", "unit_price": 300}],
             "warranty": "", "duration": "Six (6) weeks."}
        req = {"client_name": CLIENT, "building_address": "31 Phist Terrace, New York, NY", "heading": "ESTIMATE", "what": "smoke"}
        pid = pe.create(conn, uid, req, d)
        pid2 = pe.create(conn, uid, req, dict(d, title="Phist second"))
        return uid, pid, pid2
    finally:
        conn.close()


def main() -> int:
    app = Flask("phist", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    proposal_studio.register(app)
    _cleanup()
    uid, pid, pid2 = _setup()
    a = app.test_client()
    r = a.post("/api/auth/login", json={"email": f"{TAG}.admin@superstarscontracting.com", "password": PW},
               environ_base={"REMOTE_ADDR": "10.97.0.1"})
    assert r.status_code == 200, r.status_code

    def view(p=pid):
        r = a.get(f"/api/proposal-studio/{p}")
        assert r.status_code == 200, r.status_code
        return r.get_json()["data"]

    def revise(q):
        r = a.post(f"/api/proposal-studio/{pid}/revise", json={"instruction": q})
        assert r.status_code == 200, r.get_json()
        return r.get_json()["data"]

    def back(vid, p=pid):
        return a.post(f"/api/proposal-studio/{p}/undo", json={"version_id": vid})

    passed = 0
    conn = _db()
    try:
        # ---- each Claude change is kept, newest first, and survives a reload
        v = view()
        assert v["layout"] == "ssc" and v["versions"] == [], v.get("versions")
        r1 = revise("add a 10% contingency")
        assert r1["version_id"] and "notes" in r1["changed"], {k: r1.get(k) for k in ("version_id", "changed", "reply")}
        assert r1["versions"][0]["id"] == r1["version_id"] and r1["versions"][0]["summary"] == "Added a note advising a 10% contingency."
        assert r1["versions"][0]["source"] == "claude" and r1["versions"][0]["later"] == 0
        r2 = revise("3000 lump sum, as an alternate")
        assert r2["version_id"] and len(r2["content"]["alternates"]) == 1
        q = revise("what's the deposit?")
        assert q["version_id"] is None and len(q["versions"]) == 2, "a question keeps nothing"
        v = view()
        assert [x["id"] for x in v["versions"]] == [r2["version_id"], r1["version_id"]] and v["versions"][1]["later"] == 1
        passed += 1; print("  kept + listed after reload ok")

        # ---- undo the latest; undo the undo
        u = back(r2["version_id"]).get_json()["data"]
        assert u["content"]["alternates"] == [] and len(u["content"]["notes"]) == 2, u["content"]
        assert u["went_back"]["undid"] == 1 and u["versions"][0]["source"] == "back" and u["versions"][0]["summary"].startswith("Undid: ")
        redo = back(u["versions"][0]["id"]).get_json()["data"]
        assert len(redo["content"]["alternates"]) == 1, "the undo is itself undoable"
        passed += 1; print("  undo + redo ok")

        # ---- back to before the first change: every later one goes
        g = back(r1["version_id"]).get_json()["data"]
        assert g["content"]["notes"] == ["Phist note one."] and g["content"]["alternates"] == [], g["content"]
        assert g["went_back"]["undid"] == 4 and g["versions"][0]["summary"].startswith("Went back to before: ")
        assert len(g["content"]["lines"]) == 1
        passed += 1; print("  go back several ok")

        # ---- refusals
        assert back(r1["version_id"], pid2).status_code == 400, "another proposal's version"
        assert back(99999999).status_code == 400
        conn.execute("UPDATE proposal SET status = 'sent' WHERE id = ?", (pid2,))
        conn.commit()
        sid = ph.record(conn, pid2, uid, pe.snapshot(conn, pid2), "x", "claude")
        rr = back(sid, pid2)
        assert rr.status_code == 400 and "already sent" in rr.get_json()["error"]
        assert not view(pid2)["editable"], "the page shows History on drafts only (drawHistory)"
        conn.execute("UPDATE proposal SET status = 'draft' WHERE id = ?", (pid2,))
        conn.commit()
        try:
            ph.record(conn, pid2, uid, {}, "x", "nope")
            raise AssertionError("unknown source")
        except ValueError:
            pass
        passed += 1; print("  refusals ok")

        # ---- the claude.ai connector's changes are listed too
        user = {"id": uid, "role": "admin"}
        claude_connector._t_edit_proposal(conn, user, {"proposal_id": pid, "title": "Phist via claude.ai"})
        top = ph.listing(conn, pid)[0]
        assert top["source"] == "claude_ai" and "title" in top["summary"], top
        claude_connector._t_undo_proposal(conn, user, {"proposal_id": pid})
        top = ph.listing(conn, pid)[0]
        assert top["source"] == "back" and view()["proposal"]["title"] != "Phist via claude.ai"
        passed += 1; print("  connector ok")

        # ---- at most KEEP rows
        snap = pe.snapshot(conn, pid2)
        for i in range(ph.KEEP + 5):
            ph.record(conn, pid2, uid, snap, f"step {i}", "claude")
        n = conn.execute("SELECT COUNT(*) FROM proposal_version WHERE proposal_id = ?", (pid2,)).fetchone()[0]
        assert n == ph.KEEP, n
        assert ph.listing(conn, pid2)[0]["summary"] == f"step {ph.KEEP + 4}" and len(ph.listing(conn, pid2)) == ph.LIST_MAX
        assert ph.summary_of("Removed the shed line. The base bid is now $1.") == "Removed the shed line."
        assert ph.summary_of("", ["notes"], {"notes": "important notes"}) == "Changed the important notes"
        passed += 1; print("  keep limit + summaries ok")

        # ---- Warranty is optional
        v = view()
        assert 'tr data-row="warranty" hidden' in v["html"] and 'data-addrow="warranty"' in v["html"], "blank: hidden, with + Warranty"
        assert 'data-f="warranty"' in v["html"]
        doc = dict(pe.content(proposals.get(conn, pid)), number="1", date="2026-11-11", lines=v["content"]["lines"])
        assert "Warranty" not in proposal_layout.render(doc, mode="pdf"), "the PDF leaves a blank warranty out"
        v = a.patch(f"/api/proposal-studio/{pid}", json={"content": {"warranty": "One (1) year on workmanship."}}).get_json()["data"]
        assert 'tr data-row="warranty">' in v["html"] and 'data-addrow="warranty"' not in v["html"]
        doc["warranty"] = "One (1) year on workmanship."
        assert "One (1) year on workmanship." in proposal_layout.render(doc, mode="pdf")
        v = a.patch(f"/api/proposal-studio/{pid}", json={"content": {"warranty": ""}}).get_json()["data"]
        assert 'tr data-row="warranty" hidden' in v["html"], "Remove empties it"
        passed += 1; print("  warranty ok")

        # ---- the page
        page = (ROOT / "proposal_studio.html").read_text()
        for needle in ('id="chat-hist"', 'id="hist"', 'id="e-undo"', "version_id: id", "data-vback", "This also undoes",
                       "function optionalRows", "data-addrow", "ssc-rm", "artifact/SJpPbUkDHenqQ9BfHECvnT"):
            assert needle in page, needle
        passed += 1; print("  page ok")
    finally:
        conn.close()
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"proposal history: {passed}/8 groups passed")
    return 0 if passed == 8 else 1


if __name__ == "__main__":
    sys.exit(main())
