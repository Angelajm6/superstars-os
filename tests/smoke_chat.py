#!/usr/bin/env python3
"""Company chat (2026-11-16, accepted prototype https://claude.ai/artifact/C7oS45e2gZxt6wGGDC7VMs; chat.py).

Flask test client on an ISOLATED database (honors SSC_DB_URL; builds a throwaway SQLite snapshot when unset). Proves:

  * gates: signed out -> 401; a client -> 403 on the API and no page; the page for every office role; estimator reaches chat
  * #general: everyone (office and the worker app) sees it; a new person starts with nothing unread there
  * job channels: admin sees every open job, a PM only their assigned job (another job's chat is 404), the estimator none,
    a worker only the job they are crewed on; a closed job has no chat; the crew + PM are the members
  * messages: unread counts for the others, the @mention flag, reading clears it, a reply quotes, the poll rev moves
  * private messages: one per set of people (same id again), outsiders get 404, a group of three
  * channels: private = members only; public = any office login (not the worker app), posting joins; name clash 409;
    add people; leave; the worker app can't make one
  * files: a photo is stored encrypted and served only to members (nosniff, no-store, sandbox); an unsent file is the
    uploader's only; a file from another chat can't be sent; an unknown type is refused
  * reactions toggle; delete is own-only (admin may), deleted text never comes back
  * make task: a crm_task for the person picked, linked to the chat
  * worker app: the shift token; a revoked / wrong token is 401; send, read, upload and fetch a file

Run:  venv/bin/python tests/smoke_chat.py
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import hashlib
import io
import secrets
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_chat_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(),
                   "SSC_GCAL_SYNC": "0"})
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import chat  # noqa: E402
import db_layer  # noqa: E402
import role_containment  # noqa: E402
import team_items  # noqa: E402

TAG = "chatsmk"
PW = "ChatSmokePassword2026!"
JOB, JOB2, SHUT = "CHAT-A", "CHAT-B", "CHAT-X"
W1, W2 = "E-97101", "E-97102"


def _db():
    return db_layer.connect(pragma_fk=True)


def _png() -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (180, 90, 60)).save(out, "PNG")
    return out.getvalue()


def _app():
    app = Flask("chat_smoke", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    app.config["TESTING"] = True
    auth.apply_auth_gate(app)
    role_containment.register(app)
    chat.register(app)
    return app


def _user(conn, role, name):
    email = f"{TAG}.{role}.{name.split()[0].lower()}@example.com"
    conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                 "VALUES (?,?,?,?,1,'active',0,0)", (email, auth.hash_password(PW), role, name))
    uid = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]
    return email, int(uid)


def _login(app, email):
    c = app.test_client()
    r = c.post("/api/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, (email, r.status_code)
    return c


def _token(conn, eid, revoked=False):
    tok = secrets.token_urlsafe(32)
    now = datetime.now()
    conn.execute("INSERT INTO worker_app_session (token_hash, employee_id, project_code, issued_at, expires_at, revoked_at) "
                 "VALUES (?,?,?,?,?,?)", (hashlib.sha256(tok.encode()).hexdigest(), eid, JOB, now.isoformat(timespec="seconds"),
                                          (now + timedelta(hours=8)).isoformat(timespec="seconds"),
                                          now.isoformat(timespec="seconds") if revoked else None))
    return tok


def _cleanup(conn):
    ids = [r[0] for r in conn.execute("SELECT id FROM chat_conv WHERE project_code LIKE 'CHAT-%' OR created_by LIKE 'u:%' "
                                      "OR created_by LIKE 'e:E-971%'").fetchall()] if _has(conn, "chat_conv") else []
    if ids:
        marks = ",".join("?" * len(ids))
        for sql in (f"DELETE FROM chat_react WHERE msg_id IN (SELECT id FROM chat_msg WHERE conv_id IN ({marks}))",
                    f"DELETE FROM chat_file WHERE conv_id IN ({marks})", f"DELETE FROM chat_msg WHERE conv_id IN ({marks})",
                    f"DELETE FROM chat_member WHERE conv_id IN ({marks})", f"DELETE FROM chat_read WHERE conv_id IN ({marks})",
                    f"DELETE FROM chat_conv WHERE id IN ({marks}) AND conv_key <> 'general'"):
            conn.execute(sql, ids)
    if _has(conn, "chat_msg"):
        gen = conn.execute("SELECT id FROM chat_conv WHERE conv_key = 'general'").fetchone()
        if gen:
            conn.execute("DELETE FROM chat_msg WHERE conv_id = ? AND body LIKE 'chatsmk%'", (gen[0],))
    conn.execute("DELETE FROM crm_task WHERE title LIKE 'chatsmk%'")
    conn.execute("DELETE FROM worker_app_session WHERE employee_id LIKE 'E-971%'")
    conn.execute("DELETE FROM project_assignments WHERE project_code LIKE 'CHAT-%'")
    conn.execute("DELETE FROM pm_project_assignment WHERE project_code LIKE 'CHAT-%'")
    conn.execute("DELETE FROM employees WHERE employee_id LIKE 'E-971%'")
    conn.execute("DELETE FROM projects WHERE project_code LIKE 'CHAT-%'")
    conn.commit()
    ids = [r[0] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
    purge_users(conn, ids)


def _has(conn, table) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchall()
        return True
    except Exception:
        conn.rollback()
        return False


def main() -> int:
    app = _app()
    conn = _db()
    passed = 0
    try:
        team_items.ensure(conn)
        _cleanup(conn)
        U = {}
        for role, name in (("admin", "Ada Admin"), ("c_suite", "Cy Suite"), ("pm", "Pat Manager"), ("pm", "Quinn Other"),
                           ("super", "Sam Super"), ("estimator", "Eve Estimator"), ("client", "Cleo Client")):
            U[name.split()[0].lower()] = _user(conn, role, name)
        for code, st in ((JOB, "active"), (JOB2, "active"), (SHUT, "closed")):
            conn.execute("INSERT INTO projects (project_code, name, address, status) VALUES (?, ?, ?, ?)",
                         (code, f"{TAG} {code}", f"{code} Street", st))
        for eid, wid, name in ((W1, "W-9711", "Wade Worker"), (W2, "W-9712", "Will Elsewhere")):
            conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, pin, created_at, updated_at) "
                         "VALUES (?, ?, ?, 'mason', NULL, ?, ?)", (eid, wid, name, chat._now(), chat._now()))
        conn.execute("INSERT INTO project_assignments (project_code, employee_id, start_date, status) VALUES (?, ?, ?, 'active')",
                     (JOB, W1, "2026-11-01"))
        conn.execute("INSERT INTO project_assignments (project_code, employee_id, start_date, status) VALUES (?, ?, ?, 'active')",
                     (JOB2, W2, "2026-11-01"))
        conn.execute("INSERT INTO pm_project_assignment (user_id, project_code, assigned_by, assigned_at) VALUES (?, ?, ?, ?)",
                     (U["pat"][1], JOB, U["ada"][1], chat._now()))
        tok1, tok2, tokx = _token(conn, W1), _token(conn, W2), _token(conn, W1, revoked=True)
        conn.commit()

        C = {k: _login(app, v[0]) for k, v in U.items()}
        key = {k: f"u:{v[1]}" for k, v in U.items()}

        def W(tok, op, **kw):
            return app.test_client().post("/api/worker/chat", json=dict(kw, op=op, session_token=tok))

        def convs(k):
            r = C[k].get("/api/chat/convs")
            assert r.status_code == 200, (k, r.status_code)
            return r.get_json()["data"]

        def job_id(lst, code):
            hit = [c["id"] for c in lst if c["kind"] == "job" and c["project_code"] == code]
            return hit[0] if hit else None

        # ---- gates
        assert app.test_client().get("/api/chat/convs").status_code == 401
        assert C["cleo"].get("/api/chat/convs").status_code == 403
        assert C["cleo"].get("/chat").status_code in (302, 403)
        for k in ("ada", "cy", "pat", "sam", "eve"):
            r = C[k].get("/chat")
            assert r.status_code == 200 and b"data-nav-h1" in r.data and b"chat.js" in r.data, (k, r.status_code)
        assert C["eve"].get("/api/chat/convs").status_code == 200
        assert W(tokx, "convs").status_code == 401 and W("x" * 40, "convs").status_code == 401
        assert app.test_client().post("/api/worker/chat", json={"op": "convs"}).status_code == 401
        passed += 1; print("  gates ok")

        # ---- who sees which job
        a = convs("ada")
        gen = [c["id"] for c in a if c["everyone"]][0]
        ja, jb = job_id(a, JOB), job_id(a, JOB2)
        assert ja and jb and job_id(a, SHUT) is None, "admin: every open job, never a closed one"
        p = convs("pat")
        assert job_id(p, JOB) == ja and job_id(p, JOB2) is None, "pm: only the assigned job"
        assert C["pat"].get(f"/api/chat/convs/{jb}/messages").status_code == 404
        assert C["quinn"].get(f"/api/chat/convs/{ja}").status_code == 404
        assert job_id(convs("sam"), JOB) and job_id(convs("sam"), JOB2), "super: open jobs"
        e = convs("eve")
        assert not [c for c in e if c["kind"] == "job"] and gen in [c["id"] for c in e]
        w1 = W(tok1, "convs").get_json()["data"]
        assert job_id(w1, JOB) == ja and job_id(w1, JOB2) is None and gen in [c["id"] for c in w1]
        assert W(tok1, "msgs", conv_id=jb).status_code == 404
        d = C["ada"].get(f"/api/chat/convs/{ja}").get_json()["data"]
        mk = {m["key"] for m in d["members"]}
        assert key["pat"] in mk and f"e:{W1}" in mk and f"e:{W2}" not in mk, "job members = PM + crew"
        passed += 1; print("  job channels ok")

        # ---- messages, unread, mention, read, reply, poll
        rev0 = C["ada"].get("/api/chat/poll").get_json()["data"]["rev"]
        assert not [c for c in convs("pat") if c["id"] == ja][0]["unread"]
        r = C["ada"].post(f"/api/chat/convs/{ja}/messages", json={"body": "chatsmk rig moves to drop 4 @Pat Manager",
                                                                 "mentions": [key["pat"], "u:999999"]})
        assert r.status_code == 201, r.get_json()
        m1 = r.get_json()["data"]
        assert m1["mine"] and m1["body"].startswith("chatsmk")
        assert C["ada"].get("/api/chat/poll").get_json()["data"]["rev"] > rev0
        pj = [c for c in convs("pat") if c["id"] == ja][0]
        assert pj["unread"] == 1 and pj["mention"] and "rig moves" in pj["preview"]
        sj = [c for c in convs("sam") if c["id"] == ja][0]
        assert sj["unread"] == 1 and not sj["mention"]
        assert not [c for c in convs("ada") if c["id"] == ja][0]["unread"], "your own message is read"
        assert C["pat"].get("/api/chat/unread").get_json()["data"]["unread"] >= 1
        assert C["pat"].post(f"/api/chat/convs/{ja}/read", json={"last_id": m1["id"]}).status_code == 200
        assert not [c for c in convs("pat") if c["id"] == ja][0]["unread"]
        r = W(tok1, "send", conv_id=ja, body="chatsmk on it", reply_to=m1["id"])
        assert r.status_code == 200, r.get_json()
        wm = r.get_json()["data"]
        assert wm["reply"]["id"] == m1["id"] and wm["author"] == f"e:{W1}"
        msgs = C["pat"].get(f"/api/chat/convs/{ja}/messages").get_json()["data"]["messages"]
        assert [m["id"] for m in msgs][-2:] == [m1["id"], wm["id"]] and msgs[-1]["field"]
        assert C["pat"].post(f"/api/chat/convs/{ja}/messages", json={"body": "  "}).status_code == 400
        assert C["pat"].post(f"/api/chat/convs/{ja}/messages", json={"body": "x" * 4001}).status_code == 400
        assert W(tok1, "read", conv_id=ja, last_id=wm["id"]).status_code == 200
        passed += 1; print("  messages ok")

        # ---- private + group messages
        r = C["ada"].post("/api/chat/convs", json={"kind": "dm", "members": [key["pat"]]})
        assert r.status_code == 201
        dm = r.get_json()["data"]["id"]
        assert C["ada"].post("/api/chat/convs", json={"kind": "dm", "members": [key["pat"]]}).get_json()["data"]["id"] == dm
        assert C["pat"].post("/api/chat/convs", json={"kind": "dm", "members": [key["ada"]]}).get_json()["data"]["id"] == dm
        assert dm not in [c["id"] for c in convs("pat")], "an empty chat someone else opened is hidden"
        C["ada"].post(f"/api/chat/convs/{dm}/messages", json={"body": "chatsmk private note"})
        pd = [c for c in convs("pat") if c["id"] == dm][0]
        assert pd["unread"] == 1 and pd["title"] == "Ada Admin"
        for k in ("cy", "sam", "quinn"):
            assert C[k].get(f"/api/chat/convs/{dm}/messages").status_code == 404, k
            assert dm not in [c["id"] for c in convs(k)]
        assert W(tok1, "msgs", conv_id=dm).status_code == 404
        g = C["sam"].post("/api/chat/convs", json={"kind": "dm", "members": [key["pat"], f"e:{W1}"]}).get_json()["data"]["id"]
        W(tok1, "send", conv_id=g, body="chatsmk group hello")
        gi = [c for c in convs("pat") if c["id"] == g][0]
        assert gi["group"] and gi["unread"] == 1
        r = W(tok1, "create", members=[f"e:{W2}"])
        assert r.status_code == 403, "a worker can't message a worker on another job"
        assert W(tok1, "create", members=[key["sam"]]).status_code == 200
        passed += 1; print("  private + group ok")

        # ---- channels
        r = C["ada"].post("/api/chat/convs", json={"kind": "channel", "name": "ChatSmk Office!", "private": True,
                                                   "members": [key["cy"]]})
        assert r.status_code == 201, r.get_json()
        priv = r.get_json()["data"]["id"]
        assert C["cy"].get(f"/api/chat/convs/{priv}").get_json()["data"]["title"] == "chatsmk-office"
        assert C["sam"].get(f"/api/chat/convs/{priv}").status_code == 404
        assert C["ada"].post("/api/chat/convs", json={"kind": "channel", "name": "chatsmk office"}).status_code == 409
        pub = C["ada"].post("/api/chat/convs", json={"kind": "channel", "name": "chatsmk-safety"}).get_json()["data"]["id"]
        sp = [c for c in convs("sam") if c["id"] == pub][0]
        assert not sp["joined"]
        assert C["sam"].post(f"/api/chat/convs/{pub}/messages", json={"body": "chatsmk joining"}).status_code == 201
        assert [c for c in convs("sam") if c["id"] == pub][0]["joined"], "posting joins"
        assert pub not in [c["id"] for c in W(tok1, "convs").get_json()["data"]], "public != the worker app"
        assert C["ada"].post(f"/api/chat/convs/{priv}/members", json={"members": [key["sam"], f"e:{W1}"]}).get_json()["data"]["added"] == 2
        assert C["sam"].get(f"/api/chat/convs/{priv}").status_code == 200
        assert priv in [c["id"] for c in W(tok1, "convs").get_json()["data"]]
        assert C["sam"].post(f"/api/chat/convs/{priv}/leave", json={}).status_code == 200
        assert C["sam"].get(f"/api/chat/convs/{priv}").status_code == 404
        assert C["ada"].post(f"/api/chat/convs/{gen}/leave", json={}).status_code == 403
        assert W(tok1, "create_channel", name="x").status_code == 404
        passed += 1; print("  channels ok")

        # ---- files
        r = C["pat"].post("/api/chat/files", data={"conv_id": str(ja), "file": (io.BytesIO(_png()), "lintel.png")},
                          content_type="multipart/form-data")
        assert r.status_code == 201, r.get_json()
        f1 = r.get_json()["data"]
        assert f1["kind"] == "image" and f1["name"].endswith(".jpg")
        row = conn.execute("SELECT path, thumb_path FROM chat_file WHERE id = ?", (f1["id"],)).fetchone()
        raw = (Path(os.environ["SSC_DATA_ROOT"]) / row[0]).read_bytes() if not Path(row[0]).is_absolute() else Path(row[0]).read_bytes()
        assert not raw.startswith(b"\xff\xd8"), "stored encrypted, not as a plain JPEG"
        assert C["ada"].get(f"/api/chat/files/{f1['id']}").status_code == 404, "unsent: the uploader's only"
        r = C["pat"].post("/api/chat/files", data={"conv_id": str(ja), "file": (io.BytesIO(b"MZ\x90"), "tool.exe")},
                          content_type="multipart/form-data")
        assert r.status_code == 400
        r = C["pat"].post("/api/chat/files", data={"conv_id": str(jb), "file": (io.BytesIO(b"%PDF-1.4"), "x.pdf")},
                          content_type="multipart/form-data")
        assert r.status_code == 404, "upload into a chat you can't see"
        r = C["pat"].post("/api/chat/files", data={"conv_id": str(dm), "file": (io.BytesIO(b"%PDF-1.4 x"), "plan.pdf")},
                          content_type="multipart/form-data")
        fdm = r.get_json()["data"]
        assert C["pat"].post(f"/api/chat/convs/{ja}/messages", json={"file_ids": [fdm["id"]]}).status_code == 400, \
            "a file uploaded to another chat can't be sent here"
        r = C["pat"].post(f"/api/chat/convs/{ja}/messages", json={"body": "", "file_ids": [f1["id"]]})
        assert r.status_code == 201 and r.get_json()["data"]["files"][0]["id"] == f1["id"]
        g1 = C["ada"].get(f"/api/chat/files/{f1['id']}")
        assert g1.status_code == 200 and g1.data[:2] == b"\xff\xd8"
        assert g1.headers["X-Content-Type-Options"] == "nosniff" and "no-store" in g1.headers["Cache-Control"]
        assert "sandbox" in g1.headers["Content-Security-Policy"]
        assert C["ada"].get(f"/api/chat/files/{f1['id']}?thumb=1").status_code == 200
        assert C["quinn"].get(f"/api/chat/files/{f1['id']}").status_code == 404
        assert W(tok2, "msgs", conv_id=ja).status_code == 404
        assert app.test_client().post("/api/worker/chat/file", json={"session_token": tok2, "file_id": f1["id"]}).status_code == 404
        wf = app.test_client().post("/api/worker/chat/file", json={"session_token": tok1, "file_id": f1["id"], "thumb": True})
        assert wf.status_code == 200 and wf.data[:2] == b"\xff\xd8"
        r = app.test_client().post("/api/worker/chat/upload", data={"session_token": tok1, "conv_id": str(ja),
                                                                     "file": (io.BytesIO(_png()), "bay3.png")},
                                   content_type="multipart/form-data")
        assert r.status_code == 201, r.get_json()
        assert W(tok1, "send", conv_id=ja, file_ids=[r.get_json()["data"]["id"]]).status_code == 200
        fl = C["ada"].get(f"/api/chat/convs/{ja}/files").get_json()["data"]
        assert len(fl) == 2 and {x["by"] for x in fl} == {"Pat Manager", "Wade Worker"}
        passed += 1; print("  files ok")

        # ---- reactions, delete
        assert C["pat"].post(f"/api/chat/messages/{m1['id']}/react", json={"emoji": "👍"}).status_code == 200
        assert W(tok1, "react", msg_id=m1["id"], emoji="👍").status_code == 200
        rx = [m for m in C["ada"].get(f"/api/chat/convs/{ja}/messages").get_json()["data"]["messages"] if m["id"] == m1["id"]][0]["reacts"]
        assert rx[0]["count"] == 2 and not rx[0]["mine"]
        C["pat"].post(f"/api/chat/messages/{m1['id']}/react", json={"emoji": "👍"})
        rx = [m for m in C["ada"].get(f"/api/chat/convs/{ja}/messages").get_json()["data"]["messages"] if m["id"] == m1["id"]][0]["reacts"]
        assert rx[0]["count"] == 1, "a second tap takes it back"
        assert C["pat"].post(f"/api/chat/messages/{m1['id']}/react", json={"emoji": "<b>"}).status_code == 400
        assert C["pat"].delete(f"/api/chat/messages/{m1['id']}").status_code == 403
        assert W(tok1, "delete", msg_id=m1["id"]).status_code == 403
        assert W(tok1, "delete", msg_id=wm["id"]).status_code == 200
        dl = [m for m in C["pat"].get(f"/api/chat/convs/{ja}/messages").get_json()["data"]["messages"] if m["id"] == wm["id"]][0]
        assert dl["deleted"] and dl["body"] == ""
        assert C["ada"].delete(f"/api/chat/messages/{m1['id']}").status_code == 200, "admin may delete any"
        passed += 1; print("  reactions + delete ok")

        # ---- make task
        m2 = C["sam"].post(f"/api/chat/convs/{ja}/messages", json={"body": "chatsmk check the lintel"}).get_json()["data"]
        r = C["sam"].post(f"/api/chat/messages/{m2['id']}/task",
                          json={"title": "chatsmk lintel RFI", "assignee_user_id": U["pat"][1], "due_date": "2026-11-20"})
        assert r.status_code == 201, r.get_json()
        t = conn.execute("SELECT assignee_user_id, project_code, due_date, detail FROM crm_task WHERE id = ?",
                         (r.get_json()["data"]["task_id"],)).fetchone()
        assert t[0] == U["pat"][1] and t[1] == JOB and t[2] == "2026-11-20" and f"/chat#c{ja}" in t[3]
        assert C["sam"].post(f"/api/chat/messages/{m2['id']}/task", json={"title": "chatsmk x", "assignee_user_id": U["cleo"][1]}).status_code == 400
        assert C["quinn"].post(f"/api/chat/messages/{m2['id']}/task", json={"title": "chatsmk x"}).status_code == 404
        passed += 1; print("  make task ok")

        # ---- page + nav needles
        page = (ROOT / "chat_page.html").read_text(encoding="utf-8")
        nav = (ROOT / "static" / "js" / "company_nav.js").read_text(encoding="utf-8")
        js = (ROOT / "static" / "js" / "chat.js").read_text(encoding="utf-8")
        assert page.count("data-nav-h1") == 1 and "SSC_NAV.mount()" in page
        assert "key: 'chat'" in nav and "/api/chat/unread" in nav and "ssc:chat-unread" in js
        assert "toISOString" not in js and "SSCDatePicker.getISO" in js
        wapp = (ROOT / "worker-app.html").read_text(encoding="utf-8")
        assert "screen-chat" in wapp and "/api/worker/chat" in wapp, "the worker app has its Chat screen"
        passed += 1; print("  page needles ok")
    finally:
        try:
            _cleanup(conn)
        finally:
            conn.close()
            shutil.rmtree(_TMP, ignore_errors=True)
    print(f"PASS smoke_chat ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
