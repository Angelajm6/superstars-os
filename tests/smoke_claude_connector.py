#!/usr/bin/env python3
"""Your own Claude, connected (accepted prototype 2026-10-29, https://claude.ai/artifact/VdvZw7Dr3u4VB9H6kZHSJ3).

Proves against an ISOLATED database (no model call, no PDF engine):
  * off unless SSC_CLAUDE_CONNECTOR=1: every endpoint 404s
  * discovery: protected resource + authorization server metadata (PKCE S256, DCR, public client)
  * registration takes claude.ai's callback only; authorize never redirects to an unknown client or address
  * consent sits behind the app login; someone not ticked in Settings, or without two-step sign-in, is refused;
    the CSRF token and a fresh 6-digit code are required; 5 wrong codes end the request
  * tokens: PKCE checked, a code works once (reuse revokes what it gave), refresh tokens rotate and a reused old
    one cuts the connection; tokens are stored only as hashes
  * /mcp: 401 with resource_metadata without a token; initialize / tools/list / tools/call; no pay tools, no cost
    figures, no emails or phones; documents read as text (PDF), pictures (scanned PDF); save_document files a PDF
    and a new version supersedes, never deletes
  * unticking someone, or disconnecting, ends access on the next call; the optional IP check refuses others
  * Settings > Claude: status for the person, people + toggles for admins only; the page carries the card
  * admins only (2026-11-02): C-suite can't be ticked, and one ticked before is refused
  * writes (2026-11-02): a one-time upload link files the original file into a job folder or a bid (once, 15 minutes,
    owner's access re-checked, bad types refused without burning it); proposal_guide; write_proposal saves an
    ssc-layout draft with its checklist; edit_proposal + undo_proposal_change; send_proposal only to a contact at the
    proposal's client; mark sent / signed (follow-up tasks) / declined; the hourly write limit; "21st" finds "21"
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import base64
import hashlib
import io
import re
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_ccon_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(),
                   "APP_BASE_URL": "https://app.example.test"})
os.environ.pop("SSC_CONNECTOR_CIDRS", None)
os.environ.pop("SSC_CONNECTOR_IP_HEADER", None)

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import claude_connector as cc  # noqa: E402
import claude_docs  # noqa: E402
import crm  # noqa: E402
import estimates  # noqa: E402
import proposals  # noqa: E402
import db_layer  # noqa: E402
import ssc_paths  # noqa: E402
import totp  # noqa: E402

TAG = "ccon"
PW = "ClaudeConnectorSmokePassword2026"
JOB = "FR-MN-CCN1"
ORG = "Ccon Client"
CB = "https://claude.ai/api/mcp/auth_callback"
SECRET = totp.generate_secret()


def _db():
    return db_layer.connect(pragma_fk=True)


def _cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        cc.ensure(conn)
        claude_docs.ensure(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        for t in ("claude_connector_user", "claude_connector_code", "claude_connector_token", "claude_connector_log",
                  "claude_connector_request", "claude_connector_upload", "claude_connector_snapshot"):
            purge(conn, t, "user_id", uids)
        proposals.ensure_schema(conn)
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE ?", (ORG + "%",)).fetchall()]
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph(ests)})", ests).fetchall()]
        try:
            purge(conn, "invoice", "proposal_id", pids)
        except Exception:
            conn.rollback()
        for t in ("proposal_line", "proposal_event"):
            purge(conn, t, "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        for t in ("estimate_document", "estimate_ira"):
            purge(conn, t, "estimate_id", ests)
        purge(conn, "estimate", "id", ests)
        if orgs:
            for t in ("crm_activity", "crm_task"):
                conn.execute(f"DELETE FROM {t} WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM crm_organization WHERE name LIKE ?", (ORG + "%",))
        conn.execute("DELETE FROM claude_connector_client WHERE client_id NOT IN (SELECT client_id FROM claude_connector_token)")
        purge(conn, "claude_doc", "user_id", uids)
        purge(conn, "notification_log", "recipient_user_id", uids)
        conn.execute("DELETE FROM project_documents WHERE project_code = ?", (JOB,))
        conn.execute("DELETE FROM projects WHERE project_code = ?", (JOB,))
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()


def _pdf(text: str) -> bytes:
    """A one-page PDF holding `text` (no PDF library needed)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + "".join(f"{o:010d} 00000 n \n" for o in offs).encode()
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF".encode()


def _file_doc(conn, title, data, ext, category="PERMITS") -> int:
    d = ssc_paths.under_root("data_room", "project_docs") / JOB
    d.mkdir(parents=True, exist_ok=True)
    p = d / (hashlib.md5(title.encode()).hexdigest() + ext)
    p.write_bytes(data)
    conn.execute("INSERT INTO project_documents (project_code, category, title, doc_type, file_path, file_name, file_size, mime, "
                 "superseded, uploaded_at) VALUES (?,?,?,?,?,?,?,?,0,?)",
                 (JOB, category, title, "PDF", ssc_paths.store_rel(p), title + ext, len(data), "application/pdf", "2026-10-20T09:00:00"))
    conn.commit()
    return conn.execute("SELECT MAX(id) AS m FROM project_documents WHERE project_code = ?", (JOB,)).fetchone()["m"]


def _code():
    return totp._code_at(SECRET, int(time.time() // totp.STEP))


def _pkce():
    v = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip("=")
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")


ANTHROPIC = {"REMOTE_ADDR": "160.79.104.10"}


def main() -> int:
    _cleanup()
    conn = _db()
    try:
        for role, two_step in (("admin", True), ("c_suite", True), ("pm", False), ("nostep", False)):
            r = "admin" if role == "nostep" else role
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system, "
                         "totp_enabled, totp_secret) VALUES (?, ?, ?, ?, 1, 'active', 0, 0, ?, ?)",
                         (f"{TAG}.{role}@superstarscontracting.com", auth.hash_password(PW), r, f"Ccon {role}",
                          1 if two_step else 0, SECRET if two_step else None))
        conn.execute("INSERT INTO projects (project_code, name, status) VALUES (?, 'Ccon job', 'active')", (JOB,))
        conn.commit()
        ids = {r["email"].split(".")[1].split("@")[0]: r["id"]
               for r in conn.execute("SELECT id, email FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()}
        text_doc = _file_doc(conn, "BEST permit", _pdf("BEST permit B-0098712 two-point suspended scaffold, 6 drops"), ".pdf")
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (600, 800), "white").save(buf, "PDF")
        scan_doc = _file_doc(conn, "Drop layout scan", buf.getvalue(), ".pdf", "DRAWINGS")
    finally:
        conn.close()

    app = Flask("ccon", static_folder=str(ROOT / "static"), static_url_path="/files/static")
    auth.apply_auth_gate(app)
    ca.register(app)
    claude_docs.register(app)
    cc.register(app)

    def login(role):
        c = app.test_client()
        body = {"email": f"{TAG}.{role}@superstarscontracting.com", "password": PW}
        if role in ("admin", "c_suite"):
            body["totp"] = _code()
        r = c.post("/api/auth/login", json=body, environ_base={"REMOTE_ADDR": f"10.97.0.{len(role)}"})
        assert r.status_code == 200, (role, r.status_code)
        return c

    claude = app.test_client()   # claude.ai's servers (no app session)

    # ---- off by default
    os.environ.pop("SSC_CLAUDE_CONNECTOR", None)
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server"):
        assert claude.get(path).status_code == 404
    assert claude.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}).status_code == 404
    assert claude.post("/oauth/register", json={"redirect_uris": [CB]}).status_code == 404
    os.environ["SSC_CLAUDE_CONNECTOR"] = "1"
    passed = 1; print("  off by default ok")

    # ---- discovery
    prm = claude.get("/.well-known/oauth-protected-resource").get_json()
    assert prm["resource"] == "https://app.example.test/mcp" and prm["authorization_servers"] == ["https://app.example.test"]
    assert claude.get("/.well-known/oauth-protected-resource/mcp").get_json() == prm
    asm = claude.get("/.well-known/oauth-authorization-server").get_json()
    assert asm["code_challenge_methods_supported"] == ["S256"] and asm["token_endpoint_auth_methods_supported"] == ["none"]
    assert asm["registration_endpoint"].endswith("/oauth/register") and "client_id_metadata_document_supported" not in asm
    r = claude.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401 and 'resource_metadata="https://app.example.test/.well-known/oauth-protected-resource"' in r.headers["WWW-Authenticate"]
    passed += 1; print("  discovery + 401 ok")

    # ---- registration: claude.ai's callback only
    assert claude.post("/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]}).status_code == 400
    assert claude.post("/oauth/register", json={"redirect_uris": [CB, "http://localhost:3000/callback"]}).status_code == 400
    reg = claude.post("/oauth/register", json={"redirect_uris": [CB], "client_name": "Claude",
                                               "token_endpoint_auth_method": "client_secret_post"})
    assert reg.status_code == 201
    cid = reg.get_json()["client_id"]
    assert reg.get_json()["token_endpoint_auth_method"] == "none" and "client_secret" not in reg.get_json()

    def authorize(c, verifier_challenge, state="st-1", **over):
        q = {"response_type": "code", "client_id": cid, "redirect_uri": CB, "code_challenge": verifier_challenge[1],
             "code_challenge_method": "S256", "state": state, "scope": "superstars", "resource": "https://app.example.test/mcp"}
        q.update(over)
        return c.get("/oauth/authorize", query_string={k: v for k, v in q.items() if v is not None})

    pk = _pkce()
    bad = authorize(claude, pk, client_id="nope")
    assert bad.status_code == 400 and "Location" not in bad.headers, "unknown client: never redirect"
    bad = authorize(claude, pk, redirect_uri="https://evil.example/cb")
    assert bad.status_code == 400 and "Location" not in bad.headers
    r = authorize(claude, pk, code_challenge_method="plain")
    assert r.status_code == 302 and r.headers["Location"].startswith(CB) and "error=invalid_request" in r.headers["Location"]
    r = authorize(claude, pk, resource="https://other.example/mcp")
    assert "error=invalid_target" in r.headers["Location"]
    passed += 1; print("  registration + authorize checks ok")

    # ---- consent: behind the login, ticked people with two-step only
    def consent_url(c, pk_):
        r = authorize(c, pk_)
        assert r.status_code == 302 and r.headers["Location"].startswith("/oauth/consent/"), r.headers.get("Location")
        return r.headers["Location"]

    url = consent_url(claude, pk)
    anon = app.test_client().get(url)
    assert anon.status_code == 302 and anon.headers["Location"].startswith("/login?next=/oauth/consent/")
    a = login("admin")
    r = a.get(url)
    assert r.status_code == 403 and b"An admin turns it on" in r.data, "not ticked yet"
    # admin ticks themselves and the no-two-step admin; C-suite (admins only since 2026-11-02) and a PM can't be ticked
    for who in ("admin", "nostep"):
        assert a.post("/api/claude-connector/allow", json={"user_id": ids[who], "allowed": True}).status_code == 200
    for who in ("c_suite", "pm"):
        r = a.post("/api/claude-connector/allow", json={"user_id": ids[who], "allowed": True})
        assert r.status_code == 400 and "Only admins" in r.get_json()["error"], who
    conn = _db()
    try:   # a C-suite person ticked before the change is refused at consent
        conn.execute("INSERT INTO claude_connector_user (user_id, granted_by, granted_at) VALUES (?,?,?)",
                     (ids["c_suite"], ids["admin"], "2026-10-30T09:00:00"))
        conn.commit()
    finally:
        conn.close()
    r = login("c_suite").get(consent_url(claude, _pkce()))
    assert r.status_code == 403 and b"Only admins" in r.data
    assert login("c_suite").post("/api/claude-connector/allow", json={"user_id": ids["pm"], "allowed": True}).status_code == 403
    ns = app.test_client()
    assert ns.post("/api/auth/login", json={"email": f"{TAG}.nostep@superstarscontracting.com", "password": PW},
                   environ_base={"REMOTE_ADDR": "10.97.1.1"}).status_code == 200
    r = ns.get(consent_url(claude, _pkce()))
    assert r.status_code == 403 and b"two-step" in r.data
    r = a.get(url)
    assert r.status_code == 200 and b"Allow Claude" in r.data and b"6-digit" in r.data
    assert "form-action 'self' https://claude.ai" in r.headers["Content-Security-Policy"]
    csrf = re.search(rb'name="csrf" value="([^"]+)"', r.data).group(1).decode()
    assert a.post(url, data={"csrf": "wrong", "action": "allow", "code": _code()}).status_code == 400
    r = a.get(url)   # a fresh page (the bad CSRF post doesn't burn the request)
    csrf = re.search(rb'name="csrf" value="([^"]+)"', r.data).group(1).decode()
    r = a.post(url, data={"csrf": csrf, "action": "allow", "code": "000000" if _code() != "000000" else "111111"})
    assert r.status_code == 400 and b"match" in r.data
    csrf = re.search(rb'name="csrf" value="([^"]+)"', r.data).group(1).decode()
    r = a.post(url, data={"csrf": csrf, "action": "allow", "code": _code()})
    assert r.status_code == 302 and r.headers["Location"].startswith(CB + "?")
    q = parse_qs(urlsplit(r.headers["Location"]).query)
    assert q["state"] == ["st-1"] and q["iss"] == ["https://app.example.test"]
    code = q["code"][0]
    assert a.get(url).status_code == 400, "a request is used once"
    # five wrong codes end a request
    url2 = consent_url(claude, _pkce())
    page = a.get(url2)
    for _ in range(cc.MAX_TRIES):
        csrf2 = re.search(rb'name="csrf" value="([^"]+)"', page.data)
        page = a.post(url2, data={"csrf": csrf2.group(1).decode() if csrf2 else "", "action": "allow", "code": "12345x"})
    assert page.status_code in (400, 403) and a.get(url2).status_code == 400
    # cancel goes back with access_denied
    url3 = consent_url(claude, _pkce())
    csrf3 = re.search(rb'name="csrf" value="([^"]+)"', a.get(url3).data).group(1).decode()
    r = a.post(url3, data={"csrf": csrf3, "action": "deny"})
    assert "error=access_denied" in r.headers["Location"]
    passed += 1; print("  consent ok")

    # ---- tokens
    def exchange(code_, verifier, **over):
        f = {"grant_type": "authorization_code", "code": code_, "redirect_uri": CB, "client_id": cid,
             "code_verifier": verifier, "resource": "https://app.example.test/mcp"}
        f.update(over)
        return claude.post("/oauth/token", data=f, environ_base=ANTHROPIC)

    assert exchange(code, _pkce()[0]).get_json()["error"] == "invalid_grant", "wrong verifier"
    assert exchange(code, pk[0]).status_code == 400, "the code burned on the bad attempt"
    pk = _pkce()
    url = consent_url(claude, pk)
    csrf = re.search(rb'name="csrf" value="([^"]+)"', a.get(url).data).group(1).decode()
    code = parse_qs(urlsplit(a.post(url, data={"csrf": csrf, "action": "allow", "code": _code()}).headers["Location"]).query)["code"][0]
    t = exchange(code, pk[0])
    assert t.status_code == 200 and t.headers["Cache-Control"] == "no-store", t.get_json()
    tok = t.get_json()
    assert tok["token_type"] == "Bearer" and tok["expires_in"] == 3600 and tok["refresh_token"]
    conn = _db()
    try:
        raw = " ".join(str(dict(r)) for r in conn.execute("SELECT * FROM claude_connector_token").fetchall())
        assert tok["access_token"] not in raw and tok["refresh_token"] not in raw, "tokens stored as hashes only"
    finally:
        conn.close()
    assert exchange(code, pk[0]).get_json()["error"] == "invalid_grant"
    H = lambda at: {"Authorization": "Bearer " + at}  # noqa: E731
    rpc = lambda at, method, params=None, i=1: claude.post("/mcp", json={"jsonrpc": "2.0", "id": i, "method": method,  # noqa: E731
                                                                          "params": params or {}}, headers=H(at), environ_base=ANTHROPIC)
    assert rpc(tok["access_token"], "ping").status_code == 401, "a reused code revokes what it gave"
    passed += 1; print("  code exchange ok")

    def connect():
        p = _pkce()
        u = consent_url(claude, p)
        cs = re.search(rb'name="csrf" value="([^"]+)"', a.get(u).data).group(1).decode()
        c_ = parse_qs(urlsplit(a.post(u, data={"csrf": cs, "action": "allow", "code": _code()}).headers["Location"]).query)["code"][0]
        return exchange(c_, p[0]).get_json()

    tok = connect()
    r1 = claude.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid},
                     environ_base=ANTHROPIC).get_json()
    assert r1["access_token"] != tok["access_token"] and r1["refresh_token"] != tok["refresh_token"]
    assert rpc(r1["access_token"], "ping").get_json()["result"] == {}
    again = claude.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid},
                        environ_base=ANTHROPIC)
    assert again.get_json()["error"] == "invalid_grant" and rpc(r1["access_token"], "ping").status_code == 401, "reuse cuts it"
    passed += 1; print("  refresh rotation + reuse ok")

    # ---- MCP
    at = connect()["access_token"]
    init = rpc(at, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-ai"}}).get_json()
    assert init["result"]["protocolVersion"] == "2025-06-18" and init["result"]["capabilities"]["tools"]
    assert claude.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=H(at),
                       environ_base=ANTHROPIC).status_code == 202
    assert claude.get("/mcp", headers=H(at)).status_code == 405
    assert claude.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=dict(H(at), Origin="https://evil.example"),
                       environ_base=ANTHROPIC).status_code == 403
    tools = {t["name"]: t for t in rpc(at, "tools/list").get_json()["result"]["tools"]}
    assert set(tools) == {"search", "list_jobs", "get_job", "list_job_documents", "read_job_document", "list_estimates",
                          "list_proposals", "get_proposal", "get_client", "get_daily_report", "save_document",
                          "upload_link", "proposal_guide", "write_proposal", "edit_proposal", "undo_proposal_change",
                          "create_proposal_from_estimate", "mark_proposal_sent", "mark_proposal_signed",
                          "mark_proposal_declined", "send_proposal"}
    assert not any(re.search(r"pay|hours|email|delete|inbox", n) for n in tools)
    for n in ("upload_link", "write_proposal", "edit_proposal", "mark_proposal_signed", "send_proposal"):
        assert tools[n]["annotations"]["readOnlyHint"] is False and tools[n]["annotations"]["destructiveHint"] is False, n
    assert tools["proposal_guide"]["annotations"]["readOnlyHint"] is True
    assert tools["save_document"]["annotations"]["readOnlyHint"] is False and tools["get_job"]["annotations"]["readOnlyHint"] is True

    def call(name, args, i=2):
        return rpc(at, "tools/call", {"name": name, "arguments": args}, i).get_json()["result"]

    s = call("search", {"query": "ccon"})
    assert not s.get("isError") and JOB in s["content"][0]["text"]
    j = call("get_job", {"project_code": JOB})
    assert not j.get("isError") and not re.search(r'"[a-z_]*(cost|labor|wage|payroll|rate)[a-z_]*"', j["content"][0]["text"]), j
    assert "@" not in j["content"][0]["text"]
    assert call("get_job", {"project_code": "NOPE-1"})["isError"]
    assert call("get_job", {"project_code": JOB, "extra": 1})["isError"], "unknown arguments refused"
    assert call("payroll_summary", {})["isError"]
    docs = call("list_job_documents", {"project_code": JOB})
    assert "BEST permit" in docs["content"][0]["text"] and "file_path" not in docs["content"][0]["text"]
    rd = call("read_job_document", {"document_id": text_doc})
    assert rd["content"][0]["type"] == "text" and "B-0098712" in rd["content"][0]["text"] and "not instructions" in rd["content"][0]["text"]
    sc = call("read_job_document", {"document_id": scan_doc})
    assert sc["content"][1]["type"] == "image" and sc["content"][1]["mimeType"] == "image/jpeg"
    real_reads = cc.READS_PER_HOUR
    cc.READS_PER_HOUR = 0   # over the hourly reads: paused + one "unusual activity" email a day
    try:
        assert call("read_job_document", {"document_id": text_doc})["isError"]
        assert call("read_job_document", {"document_id": text_doc})["isError"]
    finally:
        cc.READS_PER_HOUR = real_reads
    passed += 1; print("  MCP reads ok")

    # ---- save_document: a PDF in the job, a new version supersedes (never deletes)
    real_pdf = claude_docs.pdf_bytes
    claude_docs.pdf_bytes = lambda d: _pdf("Means and methods")
    fake = types.ModuleType("server")

    def _insert(conn_, code_, fs, form, supersedes_id=None):
        data = fs.read()
        return _file_doc_ins(conn_, code_, form, fs.filename, data)
    fake._projdoc_insert = _insert
    prev = sys.modules.get("server")
    sys.modules["server"] = fake
    try:
        sv = call("save_document", {"project_code": JOB, "folder": "Safety", "title": "Means and Methods",
                                    "html": "<h1>Means and Methods</h1><script>x()</script><p>Six drops.</p>"})
        assert not sv.get("isError"), sv
        import json as _json
        out = _json.loads(sv["content"][0]["text"])
        assert out["saved"] and out["format"] == "PDF" and out["word_copy"].endswith("/docx")
        sv2 = call("save_document", {"project_code": JOB, "folder": "Safety", "title": "Means and Methods",
                                     "html": "<p>Seven drops.</p>", "replaces_document_id": out["document_id"]})
        out2 = _json.loads(sv2["content"][0]["text"])
        assert out2["replaced_document_id"] == out["document_id"]
        conn = _db()
        try:
            old = conn.execute("SELECT superseded, notes, category FROM project_documents WHERE id = ?", (out["document_id"],)).fetchone()
            assert old["superseded"] == 1 and old["notes"] == "Made with Claude" and old["category"] == "SAFETY"
            assert conn.execute("SELECT COUNT(*) AS n FROM project_documents WHERE project_code = ?", (JOB,)).fetchone()["n"] == 4
            assert "<script" not in conn.execute("SELECT html FROM claude_doc WHERE user_id = ? ORDER BY id LIMIT 1",
                                                 (ids["admin"],)).fetchone()["html"]
        finally:
            conn.close()
        assert call("save_document", {"project_code": JOB, "folder": "Safety", "title": "x", "html": "<p>y</p>",
                                      "replaces_document_id": out["document_id"]})["isError"], "only the current version"
        assert call("save_document", {"project_code": JOB, "folder": "Payroll", "title": "x", "html": "<p>y</p>"})["isError"]
    finally:
        claude_docs.pdf_bytes = real_pdf
        if prev is not None:
            sys.modules["server"] = prev
        else:
            sys.modules.pop("server", None)
    passed += 1; print("  save_document ok")

    # ---- writes (2026-11-02): uploads, proposals, statuses
    real = {"deliver": proposals._deliver, "pdf": proposals.render_pdf}
    sent_mail = []
    proposals._deliver = lambda to, subj, text, html: (sent_mail.append(to) or True, None)
    proposals.render_pdf = lambda conn_, pid_, actor=None: {"ok": False, "error": "no engine in the smoke"}
    fake = types.ModuleType("server")

    def _insert2(conn_, code_, fs, form, supersedes_id=None):
        return _file_doc_ins(conn_, code_, form, fs.filename, fs.read())
    fake._projdoc_insert = _insert2
    fake._PROJDOC_EXT_TYPE = {".pdf": ("PDF", "application/pdf"), ".jpg": ("JPG", "image/jpeg")}
    prev = sys.modules.get("server")
    sys.modules["server"] = fake
    import json as _json
    J = lambda res: _json.loads(res["content"][0]["text"])  # noqa: E731
    try:
        # the upload link: the original file, once, into one folder
        up = call("upload_link", {"target": "job", "project_code": JOB, "folder": "Inspections", "title": "Filing Report 1"})
        assert not up.get("isError"), up
        link = J(up)["upload_link"]
        assert link.startswith("https://app.example.test/u/sscup_") and "curl -F file=@" in J(up)["how"]
        path = urlsplit(link).path
        conn = _db()
        try:
            raw = " ".join(str(dict(r)) for r in conn.execute("SELECT * FROM claude_connector_upload").fetchall())
            assert path.rsplit("/", 1)[1] not in raw, "the link's token is stored only as a hash"
        finally:
            conn.close()
        anon = app.test_client()   # the person's phone: no app session
        pg = anon.get(path, headers={"Accept": "text/html"})
        assert pg.status_code == 200 and f"{JOB} · Inspections".encode() in pg.data and b'type="file"' in pg.data
        assert b"Ccon job" not in pg.data and b"BEST permit" not in pg.data, "the page shows only where files go"
        bad = anon.post(path, data={"file": (io.BytesIO(b"MZ"), "tool.exe")}, content_type="multipart/form-data")
        assert bad.status_code == 400 and "isn't a file type" in bad.get_json()["error"]
        ok = anon.post(path, data={"file": (io.BytesIO(_pdf("Falcon filing report")), "2600 East 21st Street - Filing Report 1.pdf")},
                       content_type="multipart/form-data")
        assert ok.status_code == 200 and ok.get_json()["filed"] == 1, ok.data
        assert anon.post(path, data={"file": (io.BytesIO(_pdf("again")), "again.pdf")},
                         content_type="multipart/form-data").status_code == 410, "used once"
        assert anon.get(path, headers={"Accept": "text/html"}).status_code == 410
        conn = _db()
        try:
            d = conn.execute("SELECT category, title, notes FROM project_documents WHERE project_code = ? ORDER BY id DESC LIMIT 1",
                             (JOB,)).fetchone()
            assert (d["category"], d["title"], d["notes"]) == ("INSPECTIONS", "Filing Report 1", "Added from Claude")
            # an old link and a link whose owner lost access look the same: ended
            late = J(call("upload_link", {"target": "job", "project_code": JOB, "folder": "Permits"}))["upload_link"]
            gone = J(call("upload_link", {"target": "job", "project_code": JOB, "folder": "Permits"}))["upload_link"]
            conn.execute("UPDATE claude_connector_upload SET expires_at = '2026-01-01T00:00:00' WHERE token_hash = ?",
                         (cc._hash(late.rsplit("/", 1)[1]),))
            conn.execute("UPDATE claude_connector_upload SET user_id = ? WHERE token_hash = ?",
                         (ids["pm"], cc._hash(gone.rsplit("/", 1)[1])))
            conn.commit()
        finally:
            conn.close()
        for u_ in (late, gone, "https://app.example.test/u/sscup_madeup"):
            assert anon.post(urlsplit(u_).path, data={"file": (io.BytesIO(_pdf("x")), "x.pdf")},
                             content_type="multipart/form-data").status_code == 410
        assert call("upload_link", {"target": "job", "project_code": JOB, "folder": "Bid files"})["isError"]
        assert call("upload_link", {"target": "job", "project_code": "NOPE-9", "folder": "Permits"})["isError"]

        # a bid's documents
        conn = _db()
        try:
            org = crm.create_org(conn, name=ORG + " Bids", relationship_type="client", created_by=ids["admin"])
            est = estimates.create_estimate(conn, est_type="FR", borough="BK", client_org_id=org,
                                            building_address="2600 East 21 Street", created_by=ids["admin"])
            conn.commit()
        finally:
            conn.close()
        bl = J(call("upload_link", {"target": "bid", "estimate_id": est["id"], "folder": "Drawings"}))["upload_link"]
        assert anon.post(urlsplit(bl).path, data={"file": [(io.BytesIO(_pdf("A-101")), "A-101.pdf"),
                                                           (io.BytesIO(_pdf("A-102")), "A-102.pdf")]},
                         content_type="multipart/form-data").get_json()["filed"] == 2
        conn = _db()
        try:
            assert conn.execute("SELECT COUNT(*) AS n FROM estimate_document WHERE estimate_id = ? AND category = 'drawing'",
                                (est["id"],)).fetchone()["n"] == 2
        finally:
            conn.close()

        # proposals: the guide, then claude.ai's Claude writes it and the app saves it in the company layout
        gd = call("proposal_guide", {"job_type": "Rope access inspection"})
        assert not gd.get("isError") and "rules" in J(gd) and "library" in J(gd)
        import proposal_engine as pe
        props = tools["write_proposal"]["inputSchema"]["properties"]
        draft = {k: v for k, v in pe.fake_draft({"client_name": ORG + " Riverside", "building_address": "410 Riverside Drive",
                                                 "what": "six drops, FISP report"}).items() if k in props}
        wp = call("write_proposal", dict(draft, client_name=ORG + " Riverside", building_address="410 Riverside Drive",
                                         summary="six drops, FISP report"))
        assert not wp.get("isError"), wp
        w = J(wp)
        pid = w["proposal_id"]
        assert w["status"] == "draft" and w["base_bid"] and "checklist" in w and "/proposal-studio#p/" in w["open_in_app"]
        assert call("write_proposal", {"building_address": "x", "job_type": "Roofing", "title": "t", "overview": "o",
                                       "lines": []})["isError"], "a client and lines are required"
        ed = call("edit_proposal", {"proposal_id": pid, "title": "410 Riverside Drive - Rope Access Inspections (8 drops)"})
        assert not ed.get("isError"), ed
        conn = _db()
        try:
            r = proposals.get(conn, pid)
            assert r["layout"] == "ssc" and r["title"].endswith("(8 drops)")
            contact = crm.create_contact(conn, full_name="Dana Ccon", org_id=r["client_org_id"], email="dana.ccon@example.test")
            stranger_org = crm.create_org(conn, name=ORG + " Other", relationship_type="client")
            stranger = crm.create_contact(conn, full_name="Not Theirs", org_id=stranger_org, email="nope.ccon@example.test")
            conn.commit()
        finally:
            conn.close()
        assert not call("undo_proposal_change", {"proposal_id": pid}).get("isError")
        conn = _db()
        try:
            assert not proposals.get(conn, pid)["title"].endswith("(8 drops)"), "undo puts the old version back"
        finally:
            conn.close()
        assert call("undo_proposal_change", {"proposal_id": pid})["isError"], "nothing left to undo"

        # email it: only to someone at the proposal's client
        assert call("send_proposal", {"proposal_id": pid, "contact_id": stranger})["isError"] and not sent_mail
        sp = call("send_proposal", {"proposal_id": pid, "contact_id": contact, "message": "Our proposal."})
        assert not sp.get("isError"), sp
        assert J(sp)["status"] == "sent" and sent_mail == ["dana.ccon@example.test"] and "@" not in sp["content"][0]["text"]
        assert call("edit_proposal", {"proposal_id": pid, "title": "x"})["isError"], "only drafts change"
        dc = call("mark_proposal_declined", {"proposal_id": pid, "reason": "went with a lower bid"})
        assert J(dc)["status"] == "declined"

        # from an estimate, then sent another way, then signed on paper
        fe = call("create_proposal_from_estimate", {"estimate_id": est["id"]})
        assert not fe.get("isError"), fe
        p2 = J(fe)["proposal_id"]
        assert call("mark_proposal_declined", {"proposal_id": p2})["isError"], "a draft was never sent"
        ms = call("mark_proposal_sent", {"proposal_id": p2, "to_name": "Front desk"})
        assert not ms.get("isError") and J(ms)["status"] == "sent", ms
        assert call("mark_proposal_signed", {"proposal_id": p2, "signer_name": "x"})["isError"], "a real signer name"
        sg = call("mark_proposal_signed", {"proposal_id": p2, "signer_name": "Lee Ccon"})
        assert not sg.get("isError") and J(sg)["status"] == "signed", sg
        conn = _db()
        try:
            ev = [r["event"] for r in conn.execute("SELECT event, actor_user_id FROM proposal_event WHERE proposal_id = ? AND "
                                                   "actor_user_id = ?", (p2, ids["admin"])).fetchall()]
            assert "signed" in ev and "sent" in ev, ev
            assert conn.execute("SELECT COUNT(*) AS n FROM crm_task WHERE entity_type = 'organization' AND entity_id = ?",
                                (org,)).fetchone()["n"] >= 1, "signing starts the follow-ups"
            assert conn.execute("SELECT actor_user_id FROM proposal_event WHERE proposal_id = ? AND event = 'declined'",
                                (pid,)).fetchone()["actor_user_id"] == ids["admin"]
            logged = [r["tool"] for r in conn.execute("SELECT tool FROM claude_connector_log WHERE user_id = ? AND event = 'saved'",
                                                       (ids["admin"],)).fetchall()]
            for t_ in ("upload_link", "upload", "write_proposal", "send_proposal", "mark_proposal_signed"):
                assert t_ in logged, t_
            cols = {r[1] for r in conn.execute("PRAGMA table_info(claude_connector_log)").fetchall()} if not db_layer.is_postgres() else None
            assert cols is None or cols == {"id", "user_id", "at", "event", "tool", "project_code", "n"}, "no content in the log"
        finally:
            conn.close()

        # too many changes in an hour: paused
        real_w = cc.WRITES_PER_HOUR
        cc.WRITES_PER_HOUR = 0
        try:
            assert "a lot of changes" in call("upload_link", {"target": "job", "project_code": JOB, "folder": "Permits"})["content"][0]["text"]
        finally:
            cc.WRITES_PER_HOUR = real_w

        # "21st" finds the job saved as "21"
        conn = _db()
        try:
            conn.execute("UPDATE projects SET address = '2600 East 21 Street' WHERE project_code = ?", (JOB,))
            conn.commit()
        finally:
            conn.close()
        s21 = J(call("search", {"query": "2600 East 21st Street"}))
        assert JOB in [j_["project_code"] for j_ in s21["jobs"]] and est["code"] in [e_["code"] for e_ in s21["estimates"]]
    finally:
        proposals._deliver, proposals.render_pdf = real["deliver"], real["pdf"]
        if prev is not None:
            sys.modules["server"] = prev
        else:
            sys.modules.pop("server", None)
    passed += 1; print("  writes ok")

    # ---- Settings > Claude
    st = a.get("/api/claude-connector").get_json()["data"]
    assert st["enabled"] and st["allowed"] and st["url"] == "https://app.example.test/mcp" and st["connections"]
    people = {p["id"] for p in st["people"]}
    assert people >= {ids["admin"], ids["nostep"]} and not people & {ids["pm"], ids["c_suite"]}, "admins only"
    assert any(x["what"] == "Saved a document" for x in st["activity"])
    for what in ("Filed documents", "Wrote a proposal", "Marked a proposal signed", "Emailed a proposal to the client"):
        assert any(x["what"] == what for x in st["activity"]), what
    assert login("c_suite").get("/api/claude-connector").status_code == 403, "C-suite no longer see the card"
    assert login("pm").get("/api/claude-connector").status_code == 403
    page = (ROOT / "company_portal.html").read_text(encoding="utf-8")
    assert 'id="cc-card"' in page and "/claude-connector" in page and "Connect your Claude" in page and "Only admins can connect" in page
    passed += 1; print("  settings ok")

    # ---- access ends: untick, disconnect, IP check
    assert rpc(at, "ping").status_code == 200
    a.post("/api/claude-connector/allow", json={"user_id": ids["admin"], "allowed": False})
    assert rpc(at, "ping").status_code == 401, "unticked: next call refused"
    a.post("/api/claude-connector/allow", json={"user_id": ids["admin"], "allowed": True})
    at = connect()["access_token"]
    assert a.post("/api/claude-connector/disconnect", json={}).status_code == 200
    assert rpc(at, "ping").status_code == 401
    at = connect()["access_token"]
    os.environ["SSC_CONNECTOR_CIDRS"] = "160.79.104.0/21"
    try:
        assert rpc(at, "ping").status_code == 200
        assert claude.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=H(at),
                           environ_base={"REMOTE_ADDR": "203.0.113.9"}).status_code == 403
        assert claude.post("/oauth/register", json={"redirect_uris": [CB]},
                           environ_base={"REMOTE_ADDR": "203.0.113.9"}).status_code == 403
    finally:
        os.environ.pop("SSC_CONNECTOR_CIDRS", None)
    conn = _db()
    try:
        conn.execute("UPDATE users SET status = 'inactive' WHERE id = ?", (ids["admin"],))
        conn.commit()
    finally:
        conn.close()
    assert rpc(at, "ping").status_code == 401, "a deactivated person is cut off"
    passed += 1; print("  access ends ok")

    # ---- alerts: every admin (and the person) is emailed; nothing secret in them; "unusual" once a day
    conn = _db()
    try:
        rows = [dict(r) for r in conn.execute("SELECT kind, recipient_user_id, subject, deep_link FROM notification_log "
                                              "WHERE recipient_user_id = ? AND kind LIKE 'claude_connector_%'", (ids["admin"],)).fetchall()]
        kinds = [r["kind"] for r in rows]
        for k in ("claude_connector_access", "claude_connector_refused", "claude_connector_codes",
                  "claude_connector_connected", "claude_connector_reuse", "claude_connector_busy"):
            assert k in kinds, (k, sorted(set(kinds)))
        assert kinds.count("claude_connector_busy") == 1, "unusual activity: once a day"
        assert any("Ccon nostep can now connect" in r["subject"] for r in rows), "the admin hears about others' access"
        cs_kinds = [r["kind"] for r in conn.execute("SELECT kind FROM notification_log WHERE recipient_user_id = ?",
                                                    (ids["c_suite"],)).fetchall()]
        assert "claude_connector_refused" in cs_kinds and "claude_connector_connected" not in cs_kinds, "the person, not everyone"
        assert ids["pm"] not in [r["recipient_user_id"] for r in conn.execute(
            "SELECT recipient_user_id FROM notification_log WHERE kind LIKE 'claude_connector_%'").fetchall()]
        blob = " ".join(f"{r['subject']} {r['deep_link']}" for r in rows)
        assert "sscmcp_" not in blob and SECRET not in blob and "/company#claude" in blob
    finally:
        conn.close()
    passed += 1; print("  alerts ok")

    print(f"smoke_claude_connector: {passed} groups passed")
    return 0


def _file_doc_ins(conn, code, form, filename, data) -> int:
    d = ssc_paths.under_root("data_room", "project_docs") / code
    d.mkdir(parents=True, exist_ok=True)
    p = d / (hashlib.md5(os.urandom(8)).hexdigest() + ".pdf")
    p.write_bytes(data)
    conn.execute("INSERT INTO project_documents (project_code, category, title, doc_type, file_path, file_name, file_size, mime, "
                 "notes, superseded, uploaded_at) VALUES (?,?,?,?,?,?,?,?,?,0,?)",
                 (code, form["category"], form["title"], "PDF", ssc_paths.store_rel(p), filename, len(data), "application/pdf",
                  form.get("notes"), "2026-10-29T10:00:00"))
    return conn.execute("SELECT MAX(id) AS m FROM project_documents WHERE project_code = ?", (code,)).fetchone()["m"]


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        os.environ.pop("SSC_CLAUDE_CONNECTOR", None)
        _cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(rc)
