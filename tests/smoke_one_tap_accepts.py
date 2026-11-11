#!/usr/bin/env python3
"""One-tap accepts in the bell (activity_accepts.py, phase 3 of the accepted prototype).

Proves against an ISOLATED database (fictional people only):
  * Studio reuses a client of the same name and only makes a new one for a new name
  * a client that came with a proposal waits for every admin; a hand-added one and anything from before go-live don't
  * Keep / Edit / Merge: one answer clears it for all, a second gets 409, Keep's Undo brings it back
  * Merge moves every kind of record, deletes the emptied client, and refuses a client that isn't waiting
  * a signed proposal offers Create project / Not a project, both undoable; Undo refuses once the job has records
  * a contact suggestion is private to the mailbox owner and the page never gets the address until Add
  * admins only; wording names no teammate
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import base64
import json
import shutil
import sys
import tempfile
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_accepts_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(Path(_TMP) / "mail"), "SSC_GMAIL_POLL": "0",
                   "SSC_REMINDERS": "0", "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(), "SSC_ASSISTANT_FAKE": "1"})

from flask import Flask  # noqa: E402

import activity_accepts as aa  # noqa: E402
import activity_feed as af  # noqa: E402
import assistant  # noqa: E402
import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import crm  # noqa: E402
import db_layer  # noqa: E402
import estimates  # noqa: E402
import inbox  # noqa: E402
import invoices  # noqa: E402
import portal_projects  # noqa: E402
import project_delete  # noqa: E402
import proposal_engine  # noqa: E402
import proposals  # noqa: E402
import today_feed  # noqa: E402

TAG = "accpt"
PW = "OneTapAcceptsSmokePassword2026"
ME = f"{TAG}.me@superstarscontracting.com"
MATE = f"{TAG}.mate@superstarscontracting.com"
MATE_NAME = "Accpt Mate"
PREFIX = "Accpt Smoke"
NOW = time.time()


def _db():
    return db_layer.connect(pragma_fk=True)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def gmsg(gid, frm, subject, to=ME):
    hs = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
          {"name": "Message-ID", "value": f"<{gid}@accpt.example>"}]
    return {"id": gid, "threadId": "t-" + gid, "labelIds": ["INBOX", "UNREAD"], "internalDate": str(int((NOW + 2) * 1000)),
            "snippet": "Hello", "payload": {"mimeType": "text/plain", "headers": hs, "body": {"data": _b64("Hello")}}}


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        for m in (ca, inbox, today_feed, af):
            m.ensure(conn)
        proposals.ensure_schema(conn)
        invoices.ensure_schema(conn)
        conn.execute("DELETE FROM activity_decision WHERE kind <> 'start'")
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        mids = [r["id"] for r in conn.execute(f"SELECT id FROM mail_message WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "mail_link", "message_id", mids)
        purge(conn, "mail_message", "id", mids)
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE ?", (PREFIX + "%",)).fetchall()]
        purge(conn, "crm_contact", "org_id", orgs)
        invs = [r["id"] for r in conn.execute(f"SELECT id FROM invoice WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        purge(conn, "invoice_payment", "invoice_id", invs)
        purge(conn, "invoice", "id", invs)
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        codes = [r["project_code"] for r in conn.execute(f"SELECT project_code FROM projects WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph(ests)})", ests).fetchall()]
        purge(conn, "crm_task", "entity_id", orgs)
        purge(conn, "proposal_line", "proposal_id", pids)
        purge(conn, "proposal_event", "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "project_documents", "project_code", codes)
        for code in codes:
            project_delete.delete(conn, code)   # the real path: it clears what a new job sets up, which Postgres's foreign keys insist on
        purge(conn, "estimate_ira", "estimate_id", ests)
        purge(conn, "estimate", "id", ests)
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM building WHERE address_line LIKE '%Accpt Terrace%'")
        conn.execute("DELETE FROM crm_organization WHERE name LIKE ?", (PREFIX + "%",))
        conn.commit()
        purge_users(conn, uids)
    finally:
        conn.close()


def _users():
    conn = _db()
    try:
        ids = {}
        for key, role, name in (("me", "admin", "Accpt Me"), ("mate", "admin", MATE_NAME), ("pm", "pm", "Accpt Pm")):
            conn.execute("INSERT INTO users (email, password_hash, full_name, role, status, must_reset_password, created_at) "
                         "VALUES (?,?,?,?,'active',0,?)", (f"{TAG}.{key}@superstarscontracting.com", auth.hash_password(PW), name, role,
                                                           datetime.now().isoformat(timespec="seconds")))
            conn.commit()
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (f"{TAG}.{key}@superstarscontracting.com",)).fetchone()["id"]
        return ids
    finally:
        conn.close()


def _studio(conn, uid, name, addr, mail_id=None) -> int:
    """The real path: a proposal started in Proposal Studio for a client typed by name."""
    req = {"client_name": name, "building_address": addr, "what": "Rope access inspection.", "job_type": "Rope access inspection", "mail_id": mail_id}
    return proposal_engine.create(conn, uid, req, proposal_engine.fake_draft(req))


def _org_of(conn, pid) -> int:
    return proposals.get(conn, pid)["client_org_id"]


def main() -> int:
    app = Flask("accpt", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    assistant.register(app)
    ca.register(app)
    inbox.register(app)
    today_feed.register(app)
    af.register(app)
    _cleanup()
    ids = _users()

    def login(key):
        c = app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@superstarscontracting.com", "password": PW},
                   environ_base={"REMOTE_ADDR": f"10.98.0.{len(key)}"})
        assert r.status_code == 200, (key, r.status_code)
        return c

    me, mate, pm = login("me"), login("mate"), login("pm")

    def feed(client=None):
        r = (client or me).get("/api/activity")
        assert r.status_code == 200, r.status_code
        return r.get_json()["data"]

    def waits(client=None):
        return {w["id"]: w for w in feed(client)["waiting"]}

    def decide(item, action, client=None, **body):
        return (client or me).post("/api/activity/decide", json={"id": item, "action": action, **body})

    passed = 0
    conn = _db()
    try:
        # ---- access
        assert pm.get("/api/activity").status_code == 403 and decide("client:1", "keep", pm).status_code == 403
        assert pm.get("/api/activity/client-matches/1").status_code == 403
        assert app.test_client().post("/api/activity/decide", json={}).status_code == 401
        passed += 1; print("  access ok")

        # ---- nothing from before go-live; start the clock now
        old = _studio(conn, ids["mate"], f"{PREFIX} Old Client", "1 Accpt Terrace, New York, NY")
        conn.execute("UPDATE activity_decision SET decided_at = ? WHERE kind = 'start'", ((datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds"),))
        conn.commit()
        assert f"client:{_org_of(conn, old)}" not in waits(), "from before go-live"
        conn.execute("UPDATE activity_decision SET decided_at = ? WHERE kind = 'start'", ((datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"),))
        conn.execute("UPDATE crm_organization SET created_at = ? WHERE id = ?", ((datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds"), _org_of(conn, old)))
        conn.commit()
        passed += 1; print("  go-live line ok")

        # ---- Studio: same name, same client; a hand-added client doesn't ask
        target = crm.create_org(conn, name=f"{PREFIX} Halden Properties LLC", relationship_type="client", created_by=ids["mate"])
        p_same = _studio(conn, ids["mate"], f"  {PREFIX.lower()} halden properties llc ", "2 Accpt Terrace, New York, NY")
        assert _org_of(conn, p_same) == target, "any case, extra spaces: the same client"
        assert conn.execute("SELECT COUNT(*) AS n FROM crm_organization WHERE LOWER(name) = ?", (f"{PREFIX.lower()} halden properties llc",)).fetchone()["n"] == 1, "no second client"
        conn.execute("UPDATE crm_organization SET created_at = ? WHERE id = ?", ((datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds"), target))
        conn.commit()
        assert f"client:{target}" not in waits(), "added by hand"
        gone = crm.create_org(conn, name=f"{PREFIX} Closed Co", relationship_type="client", status="inactive")
        p_new = _studio(conn, ids["mate"], f"{PREFIX} Closed Co", "3 Accpt Terrace, New York, NY")
        assert _org_of(conn, p_new) != gone, "an inactive client isn't reused"
        passed += 1; print("  studio duplicates ok")

        # ---- a client from a proposal waits for every admin, including who made it; Keep clears it for all
        pk = _studio(conn, ids["mate"], f"{PREFIX} Halden Property Group", "4 Accpt Terrace, New York, NY")
        org = _org_of(conn, pk)
        item = f"client:{org}"
        w = waits()[item]
        assert w["title"] == f"New client added: {PREFIX} Halden Property Group" and w["sub"].startswith("Added when Proposal ")
        assert w["note"] == f"Looks like {PREFIX} Halden Properties LLC, already in your list." and w["pop"]
        assert [a["act"] for a in w["actions"]] == ["keep", "merge", "edit"]
        assert item in waits(mate), "the person who made it decides too"
        assert not [i for i in feed()["items"] if i["id"] == item], "not listed twice"
        blob = json.dumps(feed())
        assert MATE_NAME not in blob and f"{TAG}.mate" not in blob, "names no teammate"
        r = decide(item, "keep")
        assert r.status_code == 200 and r.get_json()["data"]["undo"] and "stays in Clients" in r.get_json()["data"]["message"]
        assert item not in waits() and item not in waits(mate), "one answer clears it for all"
        assert decide(item, "keep", mate).status_code == 409, "already answered"
        assert decide(item, "undo").status_code == 200 and item in waits(mate), "Undo brings it back"
        assert decide(item, "nonsense").status_code == 400
        passed += 1; print("  keep + undo ok")

        # ---- Edit
        assert decide(item, "edit", name="  ").status_code == 400 and item in waits()
        assert decide(item, "edit", name=f"{PREFIX} Halden Properties LLC").status_code == 400, "that name is taken: merge instead"
        assert decide(item, "edit", name="x", client_kind="spaceship").status_code == 400
        assert item in waits(), "a refused edit leaves it waiting"
        r = decide(item, "edit", name=f"{PREFIX} Halden Group NY", client_kind="owner")
        assert r.status_code == 200 and not r.get_json()["data"]["undo"]
        row = crm.get_org(conn, org)
        assert row["name"] == f"{PREFIX} Halden Group NY" and row["client_kind"] == "owner" and item not in waits()
        assert proposals.get(conn, pk)["client_org_id"] == org
        passed += 1; print("  edit ok")

        # ---- Merge moves everything and removes the emptied client
        pm1 = _studio(conn, ids["mate"], f"{PREFIX} Halden Prop Grp", "5 Accpt Terrace, New York, NY")
        dup = _org_of(conn, pm1)
        ditem = f"client:{dup}"
        est = proposals.get(conn, pm1)["estimate_id"]
        cid = crm.create_contact(conn, full_name="Dana Sample", org_id=dup, email="dana@accpt-halden.example", created_by=ids["mate"])
        crm.add_activity(conn, entity_type="organization", entity_id=dup, activity_type="system", summary="Accpt note", author_user_id=ids["mate"])
        inv = invoices.create(conn, {"client_org_id": dup, "kind": "other", "amount": 100, "description": "Accpt"}, created_by=ids["mate"])
        mid = inbox.ingest(conn, ids["me"], gmsg("g-merge", '"Dana Sample" <dana@accpt-halden.example>', "Hello"), ME)
        conn.execute("UPDATE mail_message SET client_org_id = ? WHERE id = ?", (dup, mid))
        conn.execute("INSERT INTO mail_link (message_id, entity_type, entity_id, linked_at) VALUES (?, 'client', ?, ?)", (mid, str(dup), datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        m = me.get(f"/api/activity/client-matches/{dup}").get_json()["data"]["clients"]
        assert m[0]["name"].startswith(f"{PREFIX} Halden") and m[0]["score"] >= m[-1]["score"] and "projects" in m[0]
        assert me.get("/api/activity/client-matches/999999").status_code == 404
        assert decide(ditem, "merge").status_code == 400 and decide(ditem, "merge", into_id=dup).status_code == 400
        assert decide(ditem, "merge", into_id=999999).status_code == 404
        assert decide(ditem, "merge", into_id=gone).status_code == 400, "not into an inactive client"
        assert crm.get_org(conn, dup), "refused merges change nothing"
        r = decide(ditem, "merge", into_id=target)
        assert r.status_code == 200, r.get_json()
        assert f"Merged into {PREFIX} Halden Properties LLC" in r.get_json()["data"]["message"] and not r.get_json()["data"]["undo"]
        assert not crm.get_org(conn, dup), "the emptied client is gone"
        assert proposals.get(conn, pm1)["client_org_id"] == target
        assert conn.execute("SELECT client_org_id FROM estimate WHERE id = ?", (est,)).fetchone()["client_org_id"] == target
        assert conn.execute("SELECT org_id FROM crm_contact WHERE id = ?", (cid,)).fetchone()["org_id"] == target
        assert invoices.get(conn, inv["id"])["client_org_id"] == target
        assert conn.execute("SELECT client_org_id FROM mail_message WHERE id = ?", (mid,)).fetchone()["client_org_id"] == target
        assert conn.execute("SELECT 1 FROM mail_link WHERE message_id = ? AND entity_type = 'client' AND entity_id = ?", (mid, str(target))).fetchone()
        assert conn.execute("SELECT 1 FROM crm_activity WHERE entity_type = 'organization' AND entity_id = ? AND summary = 'Accpt note'", (target,)).fetchone()
        assert decide(ditem, "merge", into_id=target).status_code == 409
        # a client nobody offered can't be merged by sending its id
        assert decide(f"client:{target}", "merge", into_id=org).status_code == 409 and crm.get_org(conn, target)
        passed += 1; print("  merge moved everything ok")

        # ---- a merge that leaves something behind keeps the client, inactive
        pm2 = _studio(conn, ids["mate"], f"{PREFIX} Leftover Co", "6 Accpt Terrace, New York, NY")
        left = _org_of(conn, pm2)
        real_moves = crm._ORG_MOVES
        crm._ORG_MOVES = tuple(x for x in real_moves if x[0] != "proposal")   # as if a new table were forgotten
        try:
            r = decide(f"client:{left}", "merge", into_id=target)
        finally:
            crm._ORG_MOVES = real_moves
        assert r.status_code == 200 and crm.get_org(conn, left)["status"] == "inactive", "nothing loses its link"
        passed += 1; print("  safe leftover ok")

        # ---- a signature offers the project
        ps = _studio(conn, ids["mate"], f"{PREFIX} Halden Group NY", "7 Accpt Terrace, New York, NY")
        assert _org_of(conn, ps) == org, "same client"
        proposals.mark_sent_outside(conn, ps, actor=ids["mate"])
        proposals.mark_signed_offline(conn, ps, name="Dana Sample", actor=ids["mate"])
        item = f"signed:{ps}"
        w = waits()[item]
        assert w["kind"] == "project" and "was signed by" in w["title"] and w["project"]["address"].startswith("7 Accpt Terrace")
        assert w["project"]["est_type"] and w["project"]["client"] == f"{PREFIX} Halden Group NY"
        assert item in waits(mate) and not [i for i in feed()["items"] if i["kind"] == "signed" and i.get("proposal_id") == ps], "not listed twice"
        assert [r["proposal_id"] for r in portal_projects.ready_to_start(conn) if r["proposal_id"] == ps], "same list as Ready to start"
        assert decide(item, "create", address="x").status_code == 400 and item in waits(), "a refused create leaves it waiting"
        assert decide(item, "create", est_type="FR", rope_access_scope="repair").status_code == 400 and item in waits(), "repair is a Rope Access choice"
        r = decide(item, "create", name="Accpt North Elevation", rope_access_scope="repair")
        assert r.status_code == 200, r.get_json()
        d = r.get_json()["data"]
        code = conn.execute("SELECT converted_project_code AS c FROM estimate WHERE id = ?", (proposals.get(conn, ps)["estimate_id"],)).fetchone()["c"]
        assert code and d["message"] == f"Project {code} created." and d["undo"] and d["href"] == f"/projects/{code}"
        row = conn.execute("SELECT name, client_org_id FROM projects WHERE project_code = ?", (code,)).fetchone()
        assert row["name"] == "Accpt North Elevation" and row["client_org_id"] == org
        assert conn.execute("SELECT rope_access_scope AS s FROM projects WHERE project_code = ?", (code,)).fetchone()["s"] == "repair"
        assert item not in waits(mate) and not [x for x in portal_projects.ready_to_start(conn) if x["proposal_id"] == ps]
        assert decide(item, "create").status_code == 409, "never two projects"
        deposit = conn.execute("SELECT id, project_code AS code, status FROM invoice WHERE proposal_id = ?", (ps,)).fetchone()
        assert deposit and deposit["code"] == code and deposit["status"] == "draft", "this job brought its deposit draft along"
        r = decide(item, "undo"); assert r.status_code == 200, ("Undo removes the new project", r.status_code, r.get_json())
        assert conn.execute("SELECT project_code AS c FROM invoice WHERE id = ?", (deposit["id"],)).fetchone()["c"] is None, "the draft stays, off the job"
        assert not conn.execute("SELECT 1 FROM projects WHERE project_code = ?", (code,)).fetchone() and item in waits(), "and it waits again"
        assert proposals.get(conn, ps)["status"] == "signed"
        assert decide(item, "create").status_code == 200
        code = conn.execute("SELECT converted_project_code AS c FROM estimate WHERE id = ?", (proposals.get(conn, ps)["estimate_id"],)).fetchone()["c"]
        conn.execute("INSERT INTO project_documents (project_code, category, title, doc_type, file_path, file_name, superseded, uploaded_by_uid, uploaded_at) "
                     "VALUES (?, 'PERMITS', 'Accpt', 'other', '/nope', 'a.pdf', 0, ?, ?)", (code, ids["me"], datetime.now().isoformat()))
        conn.commit()
        r = decide(item, "undo")
        assert r.status_code == 409 and "so it stays" in r.get_json()["error"], "a job with records stays"
        assert conn.execute("SELECT 1 FROM projects WHERE project_code = ?", (code,)).fetchone()
        assert conn.execute("SELECT project_code AS c FROM invoice WHERE id = ?", (deposit["id"],)).fetchone()["c"] == code, "a refused Undo changes nothing"
        passed += 1; print("  create project + undo ok")

        # ---- Not a project
        pn = _studio(conn, ids["mate"], f"{PREFIX} Halden Group NY", "8 Accpt Terrace, New York, NY")
        proposals.mark_sent_outside(conn, pn, actor=ids["mate"])
        proposals.mark_signed_offline(conn, pn, name="Dana Sample", actor=ids["mate"])
        item = f"signed:{pn}"
        assert decide(item, "no_project").status_code == 200 and item not in waits(mate)
        assert not [x for x in portal_projects.ready_to_start(conn) if x["proposal_id"] == pn]
        assert decide(item, "undo").status_code == 200 and item in waits()
        assert decide(item, "no_project").status_code == 200
        passed += 1; print("  not a project ok")

        # ---- a contact is private to the mailbox it was found in
        est_code = proposals.get(conn, ps)["estimate_code"]
        new_addr = "priya@accpt-north.example"
        mine = inbox.ingest(conn, ids["me"], gmsg("g-c1", f'"Priya Sample" <{new_addr}>', f"About {est_code}"), ME)
        row = conn.execute("SELECT client_org_id, contact_id FROM mail_message WHERE id = ?", (mine,)).fetchone()
        assert row["client_org_id"] == org and not row["contact_id"], "matched to the client by the job number"
        for gid, frm in (("g-c2", f'"No Reply" <noreply@accpt-north.example>'), ("g-c3", f"unnamed@accpt-north.example"),
                         ("g-c4", f'"A Teammate" <{MATE}>'), ("g-c5", '"Dana Sample" <dana@accpt-halden.example>')):
            inbox.ingest(conn, ids["me"], gmsg(gid, frm, f"About {est_code}"), ME)
        mw = [w for w in waits().values() if w["kind"] == "contact"]
        assert [w["title"] for w in mw] == ["Add Priya Sample as a contact?"], [w["title"] for w in mw]
        w = mw[0]
        assert w["sub"] == f"At {PREFIX} Halden Group NY. Found in an email in your Inbox, so only you see this."
        assert new_addr not in json.dumps(feed()), "the address stays on the server until Add"
        assert not [x for x in waits(mate).values() if x["kind"] == "contact"], "a teammate never sees it"
        assert decide(w["id"], "add", mate).status_code == 409, "and can't answer it"
        r = decide(w["id"], "add")
        assert r.status_code == 200 and r.get_json()["data"]["undo"]
        c = conn.execute("SELECT id, org_id, full_name FROM crm_contact WHERE LOWER(email) = ?", (new_addr,)).fetchone()
        assert c and c["org_id"] == org and c["full_name"] == "Priya Sample" and w["id"] not in waits()
        assert decide(w["id"], "undo", mate).status_code == 409, "only its owner can undo"
        assert decide(w["id"], "undo").status_code == 200
        assert not conn.execute("SELECT 1 FROM crm_contact WHERE LOWER(email) = ?", (new_addr,)).fetchone() and w["id"] in waits()
        assert decide(w["id"], "no_contact").status_code == 200 and w["id"] not in waits(), "No is remembered"
        # the email that started a proposal counts too, even before the client has any contacts
        src = inbox.ingest(conn, ids["me"], gmsg("g-src", '"Omar Sample" <omar@accpt-fresh.example>', "Please quote"), ME)
        pf = _studio(conn, ids["me"], f"{PREFIX} Fresh Co", "9 Accpt Terrace, New York, NY", mail_id=src)
        assert pf and [x for x in waits().values() if x["title"] == "Add Omar Sample as a contact?"]
        passed += 1; print("  contact private ok")

        # ---- the page
        js = (ROOT / "static/js/activity_bell.js").read_text(encoding="utf-8")
        for needle in ("'/api/activity/decide'", "Waiting on you", "esc(w.title)", "esc(w.note)", "This can’t be undone", "rememberClosed", "ssc:claude-changed"):
            assert needle in js, needle
        am = (ROOT / "static/js/auth_menu.js").read_text(encoding="utf-8")
        assert "activity_bell.js?v=3" in am and "activity_bell.css?v=3" in am
        for page in ROOT.glob("*.html"):
            t = page.read_text(encoding="utf-8")
            assert "auth_menu.js?v=1012" not in t, page.name
        assert project_delete and estimates and date and aa
        passed += 1; print("  pages ok")
    except BaseException:
        traceback.print_exc()   # printed first: a failed cleanup below would otherwise hide what really went wrong
        raise
    finally:
        conn.close()
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS: one-tap accepts ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
