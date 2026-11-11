#!/usr/bin/env python3
"""Review a proposal before it goes (proposal_review.py; accepted prototype https://claude.ai/artifact/E3AZ6ytnir14NxaBCnQgK6).

Proves against an ISOLATED database (fictional people only):
  * who can be asked: other active admins only (not yourself, not a PM); the last one asked comes first
  * a request: the reviewer gets a sticky banner and a For you item, the asker doesn't; the review email goes from the
    asker's Gmail (test mail folder) to the reviewer, never to a client
  * only the reviewer answers (403 for anyone else); Ask for changes needs a note; the asker hears the answer (banner + For you)
  * asking again replaces the old review; an approval holds for its total: a price change reopens it for the reviewer
  * remind (asker only) pops it again; cancel (asker or reviewer only)
  * sending closes the review; someone else sending is offered the asker as Cc
  * the Studio view carries the review; the pages carry the strip, the pop-up and the sticky banner
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_prvw_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

MAIL = Path(_TMP) / "mail"
os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(MAIL), "SSC_GMAIL_POLL": "0",
                   "SSC_REMINDERS": "0", "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(), "SSC_ASSISTANT_FAKE": "1"})

from flask import Flask  # noqa: E402

import activity_feed as af  # noqa: E402
import assistant  # noqa: E402
import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import inbox  # noqa: E402
import proposal_review as pr  # noqa: E402
import proposal_studio  # noqa: E402
import proposals  # noqa: E402
import today_feed  # noqa: E402

TAG = "prvw"
PW = "ProposalReviewSmokePassword2026"
CLIENT = "Prvw Smoke Holdings"
ADDR = "31 Prvw Terrace, New York, NY"


def _db():
    return db_layer.connect(pragma_fk=True)


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        for m in (ca, inbox, today_feed, af, pr):
            m.ensure(conn)
        proposals.ensure_schema(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE ?", ("Prvw Smoke%",)).fetchall()]
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph(ests)})", ests).fetchall()]
        purge(conn, "proposal_review", "proposal_id", pids)
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate", "id", ests)
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
        purge(conn, "crm_task", "entity_id", orgs)
        conn.execute("DELETE FROM building WHERE address_line LIKE '%Prvw Terrace%'")
        conn.execute("DELETE FROM crm_organization WHERE name LIKE ?", ("Prvw Smoke%",))
        purge(conn, "activity_seen", "user_id", uids)
        conn.commit()
        purge_users(conn, uids)
    finally:
        conn.close()


def _users():
    conn = _db()
    try:
        ids = {}
        for key, role, name in (("asker", "admin", "Prvw Asker"), ("boss", "admin", "Prvw Boss"), ("other", "admin", "Prvw Other"),
                                ("pm", "pm", "Prvw Pm")):
            conn.execute("INSERT INTO users (email, password_hash, full_name, role, status, must_reset_password, created_at) "
                         "VALUES (?,?,?,?,'active',0,?)", (f"{TAG}.{key}@superstarscontracting.com", auth.hash_password(PW), name, role,
                                                           datetime.now().isoformat(timespec="seconds")))
            conn.commit()
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.{key}@superstarscontracting.com",)).fetchone()["id"]
        ids["org"] = crm.create_org(conn, name=CLIENT, relationship_type="client", created_by=ids["asker"])
        est = estimates.create_estimate(conn, est_type="FR", borough="MN", client_org_id=ids["org"], building_address=ADDR, created_by=ids["asker"])
        ids["pid"] = proposals.create_from_estimate(conn, est["id"], created_by=ids["asker"])["id"]
        conn.execute("UPDATE proposal SET price = 48600 WHERE id = ?", (ids["pid"],))
        conn.execute("INSERT INTO proposal_line (proposal_id, sort, description, qty, unit, unit_price, amount) VALUES (?,?,?,?,?,?,?)",
                     (ids["pid"], 1, "Parapet rebuild", 1, "LS", 48600, 48600))
        conn.commit()
        return ids
    finally:
        conn.close()


def main() -> int:
    app = Flask("prvw", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    ca.register(app)
    inbox.register(app)
    today_feed.register(app)
    af.register(app)
    proposal_studio.register(app)
    _cleanup()
    ids = _users()
    pid = ids["pid"]

    def login(key):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@superstarscontracting.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.96.0.{len(key)}"})
        assert r.status_code == 200, (key, r.status_code)
        return c

    asker, boss, other = login("asker"), login("boss"), login("other")

    def studio(c):
        r = c.get(f"/api/proposal-studio/{pid}")
        assert r.status_code == 200, r.status_code
        return r.get_json()["data"]

    def post(c, path, body=None):
        return c.post(f"/api/proposal-studio/{pid}/review{path}", json=body or {})

    def feed(c):
        return c.get("/api/activity").get_json()["data"]

    def pops(d, needle):
        return [b for b in d["banners"] if needle in b["title"]]

    passed = 0
    conn = _db()
    try:
        for c in (asker, boss, other):
            feed(c)   # first looks start the marks; nothing from before pops
        time.sleep(1.1)

        # ---- who can be asked
        v = studio(asker)
        assert v["review"] is None and {p["id"] for p in v["reviewers"]} >= {ids["boss"], ids["other"]}, v["reviewers"]
        assert ids["asker"] not in {p["id"] for p in v["reviewers"]} and ids["pm"] not in {p["id"] for p in v["reviewers"]}
        assert post(asker, "", {"reviewer_id": ids["pm"]}).status_code == 400
        assert post(asker, "", {"reviewer_id": ids["asker"]}).status_code == 400
        passed += 1; print("  reviewers ok")

        # ---- the request: in the app for the reviewer, an email from the asker's Gmail
        r = post(asker, "", {"reviewer_id": ids["boss"], "note": "Check the parapet allowance", "email": True})
        assert r.status_code == 200, r.get_json()
        v = r.get_json()["data"]
        assert v["review"]["status"] == "pending" and v["review"]["reviewer"]["id"] == ids["boss"] and v["review"]["i_asked"]
        assert v["review_email"]["sent"], v["review_email"]
        eml = [f.read_text(errors="ignore") for f in MAIL.glob("*.eml")]
        assert any(f"{TAG}.boss@" in e and "Review: Proposal" in e and f"/proposal-studio#p/{pid}" in e for e in eml), "review email to the reviewer"
        assert len(eml) == 1 and f"To: {TAG}.boss@" in eml[0], "one email, to the reviewer only"
        assert studio(asker)["reviewers"][0]["id"] == ids["boss"], "the one you asked last comes first"
        d = feed(boss)
        b = pops(d, "Prvw asked you to review Proposal")
        assert b and b[0].get("sticky") and b[0]["href"] == f"/proposal-studio#p/{pid}", d["banners"]
        assert d["for_you"][0]["group"] == "review" and d["badge"] >= 1, d["for_you"][:2]
        assert not pops(feed(asker), "asked you to review") and not pops(feed(other), "asked you to review"), "only the reviewer"
        assert not [i for i in feed(other)["for_you"] if i["group"] == "review"]
        passed += 1; print("  request + email + reviewer's bell ok")

        # ---- only the reviewer answers; changes need a note; the asker hears
        assert post(other, "/answer", {"answer": "approve"}).status_code == 403
        assert post(asker, "/answer", {"answer": "approve"}).status_code == 403
        assert post(boss, "/answer", {"answer": "changes"}).status_code == 400
        time.sleep(1.1)
        r = post(boss, "/answer", {"answer": "changes", "note": "Drop the shed to 6 weeks"})
        assert r.status_code == 200 and r.get_json()["data"]["review"]["status"] == "changes"
        d = feed(asker)
        assert pops(d, "Prvw asked for changes on Proposal") and [i for i in d["for_you"] if i["group"] == "review" and "changes" in i["title"]]
        assert not [i for i in feed(boss)["for_you"] if i["group"] == "review"], "nothing waits on the reviewer now"
        passed += 1; print("  answer: changes ok")

        # ---- ask again: the old one is replaced; approve; the asker hears
        time.sleep(1.1)
        assert post(asker, "", {"reviewer_id": ids["boss"], "note": "Done, 6 weeks", "email": False}).status_code == 200
        n = conn.execute("SELECT status FROM proposal_review WHERE proposal_id = ? ORDER BY id", (pid,)).fetchall()
        assert [x["status"] for x in n] == ["replaced", "pending"], [x["status"] for x in n]
        time.sleep(1.1)
        v = post(boss, "/answer", {"answer": "approve"}).get_json()["data"]
        assert v["review"]["status"] == "approved" and abs(v["review"]["total"] - 48600) < 0.01, v["review"]
        d = feed(asker)
        assert pops(d, "Prvw approved Proposal") and [i for i in d["for_you"] if "approved and ready to send" in i["title"]]
        passed += 1; print("  ask again + approve ok")

        # ---- a price change after the approval sends it back to the reviewer
        time.sleep(1.1)
        conn.execute("UPDATE proposal SET price = 51000 WHERE id = ?", (pid,))
        conn.execute("UPDATE proposal_line SET unit_price = 51000, amount = 51000 WHERE proposal_id = ?", (pid,))
        conn.commit()
        v = studio(asker)
        assert v["review"]["status"] == "pending" and v["review"]["reopened"], v["review"]
        assert conn.execute("SELECT COUNT(1) AS n FROM proposal_event WHERE proposal_id = ? AND event = 'review_reopened'", (pid,)).fetchone()["n"] == 1
        studio(asker)
        assert conn.execute("SELECT COUNT(1) AS n FROM proposal_event WHERE proposal_id = ? AND event = 'review_reopened'", (pid,)).fetchone()["n"] == 1, "once"
        d = feed(boss)
        assert pops(d, "changed after you approved it"), d["banners"]
        assert [i for i in d["for_you"] if i["group"] == "review"]
        passed += 1; print("  reopened on change ok")

        # ---- remind (asker only) pops it again; cancel (asker or reviewer only)
        assert post(boss, "/remind").status_code == 403
        time.sleep(1.1)
        assert post(asker, "/remind").status_code == 200
        assert pops(feed(boss), "changed after you approved it"), "a reminder pops again"
        assert post(other, "/cancel").status_code == 403
        assert post(boss, "/cancel").status_code == 200 and studio(asker)["review"] is None
        assert not [i for i in feed(boss)["for_you"] if i["group"] == "review"]
        passed += 1; print("  remind + cancel ok")

        # ---- sending: someone else gets the asker as Cc; the review closes
        assert post(asker, "", {"reviewer_id": ids["boss"], "email": False}).status_code == 200
        assert pr.cc_for(conn, pid, ids["boss"]) == f"{TAG}.asker@superstarscontracting.com"
        assert pr.cc_for(conn, pid, ids["asker"]) == "", "your own: no Cc"
        assert boss.get(f"/api/proposal-studio/{pid}/send-options").get_json()["data"]["review_cc"] == f"{TAG}.asker@superstarscontracting.com"
        row = pr.close_on_send(conn, pid, ids["boss"])
        assert row and conn.execute("SELECT status FROM proposal_review WHERE id = ?", (row["id"],)).fetchone()["status"] == "sent"
        assert studio(asker)["review"] is None
        conn.execute("UPDATE proposal SET status = 'sent' WHERE id = ?", (pid,))
        conn.commit()
        assert post(asker, "", {"reviewer_id": ids["boss"]}).status_code == 400, "only a draft"
        assert studio(asker)["reviewers"] == []
        passed += 1; print("  send closes + Cc ok")

        # ---- the pages
        html = (ROOT / "proposal_studio.html").read_text(encoding="utf-8")
        for needle in ('id="e-rvw"', 'id="rr-m"', "data-rvw=\"approve\"", "'/review' + path", "Send for review", "s-revwarn", "review_cc",
                       "esc(rv.reviewer.name)"):
            assert needle in html, needle
        js = (ROOT / "static/js/activity_bell.js").read_text(encoding="utf-8")
        assert "it.sticky" in js and "group === 'review'" in js
        src = (ROOT / "proposal_studio.py").read_text(encoding="utf-8")
        assert "proposal_review.close_on_send(conn, pid, uid)" in src
        passed += 1; print("  pages ok")
    finally:
        conn.close()
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: proposal review ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
