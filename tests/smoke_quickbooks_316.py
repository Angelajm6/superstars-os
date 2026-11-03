#!/usr/bin/env python3
"""#316 QuickBooks Online, phase 1 (qbo.py) — invoices out, payments back. Guard.
QuickBooks is FAKED in memory here: no network, no real tokens.

Boots the real app on an ISOLATED database. Proves:
  * switched off unless the Intuit app keys, a redirect URI AND the encryption key are set
    (status says why; connect 404s)
  * ADMINS ONLY: every QuickBooks route (status, clients, sync, push, send, connect, disconnect)
    is 403 for C-suite and PM; invoices issued by C-suite still sync in the background
  * connect: Intuit is asked for the accounting scope only,
    with a CSRF state cookie; a bad state, a refusal or a bad company id store nothing; a good
    connect stores the refresh token ENCRYPTED and reads the company name
  * client matching: same name, same billing email, nothing -> "create"; saving links them,
    creates the new one in QuickBooks, and (when asked) sends the invoices issued before
  * issuing an invoice creates it in QuickBooks (DocNumber, customer, amount, due date, bill
    email); a payment recorded here becomes a QuickBooks payment; voiding here voids it there
  * payments recorded in QuickBooks come back once (never twice; never our own echo) and close
    the invoice
  * a QuickBooks refusal marks the invoice "needs you" with Intuit's message and never breaks
    the app write; Try again / the next check sends it; a retry never makes a second invoice
  * Send from QuickBooks emails through QuickBooks
  * Intuit's rotating refresh token is saved (encrypted) each time; an expired sign-in marks
    the connection needs_reconnect and app writes still succeed
  * the daily read cap stops reads and says so
  * disconnect: revokes; client matches are kept
  * logs carry ids and counts only: no token, client name or email
  * 1b: bringing in QuickBooks' open invoices + unpaid bills: admin-only (C-suite / PM 403 and
    never see them in lists, totals, Today or the calendar); part-paid balances; a customer with
    no client here becomes a client flagged for review (approve / merge moves its invoices);
    follow-ups; imported invoices / bills cannot be edited, voided or (bills) paid here; a payment
    recorded here goes to QuickBooks; QuickBooks changes (paid, new, deleted, bill paid) follow
  * disconnecting the TEST company removes everything that came from it, undoes its payments on
    real invoices and frees the app's invoices for the next company
  * the Finance page carries the card
Run:  venv/bin/python tests/smoke_quickbooks_316.py
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import sys
import tempfile
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_qbo_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ["SSC_MAIL_FAKE_DIR"] = str(Path(_TMP) / "mail")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")
os.environ["SSC_QBO_POLL"] = "0"   # no background loop in a test
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")

import secret_box  # noqa: E402

CONFIG = {"QBO_CLIENT_ID": "test-intuit-client", "QBO_CLIENT_SECRET": "test-intuit-secret",
          "APP_BASE_URL": "https://app.example.test", "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key()}
SAVED = {k: os.environ.get(k) for k in list(CONFIG) + ["QBO_ENVIRONMENT", "QBO_DAILY_READ_CAP"]}

import auth  # noqa: E402
import db_layer  # noqa: E402
import qbo  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "qbotest"
PW = "QboSmoke316Pass"
DOMAIN = "superstarscontracting.com"
REALM = "9130355555555555"
REFRESH1, REFRESH2 = "AB11-fake-refresh-one-qbotest", "AB11-fake-refresh-two-qbotest"
SECRET_NAME = "Qbtest Zebulon Riverside Towers Condominium"
SECRET_EMAIL = "ap@zebulon-hudson.example"


class FakeQB:
    def __init__(self):
        self.customers = {"101": {"Id": "101", "DisplayName": SECRET_NAME},
                          "102": {"Id": "102", "DisplayName": "HP Mgmt Co", "PrimaryEmailAddr": {"Address": SECRET_EMAIL}},
                          "103": {"Id": "103", "DisplayName": "Kingsbridge Realty"}}
        self.items = {"1": {"Id": "1", "Name": "Services"}}
        self.invoices, self.payments, self.calls, self.sent, self.revoked = {}, {}, [], [], []
        self.bills, self.cdc_inv, self.cdc_bill, self.cdc_billpay = {}, [], [], []
        self.n = 500
        self.refresh_ok, self.rotate, self.refuse_invoice_for = True, False, set()
        self.next_exchange = None
        self.cdc = []          # Payment objects the "QuickBooks user" recorded

    def _id(self):
        self.n += 1
        return str(self.n)

    def http(self, method, url, *, token=None, basic=False, data=None, json_body=None):
        self.calls.append((method, url))
        if url == qbo.TOKEN_URL:
            assert basic, "token calls use the client's Basic auth"
            if data["grant_type"] == "authorization_code":
                return self.next_exchange
            if not self.refresh_ok:
                return 400, {"error": "invalid_grant"}
            out = {"access_token": "fake-access", "expires_in": 3600, "refresh_token": REFRESH2 if self.rotate else data["refresh_token"]}
            return 200, out
        if url == qbo.REVOKE_URL:
            self.revoked.append(json_body["token"])
            return 200, {}
        assert token == "fake-access", "API calls carry the access token"
        u = urllib.parse.urlsplit(url)
        assert u.netloc == "sandbox-quickbooks.api.intuit.com", u.netloc
        q = dict(urllib.parse.parse_qsl(u.query))
        assert q.get("minorversion") == "75"
        path = u.path.split(f"/v3/company/{REALM}/", 1)[1]
        if method == "GET" and path == f"companyinfo/{REALM}":
            return 200, {"CompanyInfo": {"CompanyName": "Superstars Contracting Corp (sandbox)"}}
        if method == "GET" and path == "query":
            return 200, {"QueryResponse": self.query(q["query"])}
        if method == "GET" and path == "cdc":
            assert q["entities"] in ("Payment", "Payment,Invoice,Bill,BillPayment") and q["changedSince"]
            qr = [{"Payment": list(self.cdc)}]
            if "Invoice" in q["entities"]:
                qr += [{"Invoice": list(self.cdc_inv)}, {"Bill": list(self.cdc_bill)}, {"BillPayment": list(self.cdc_billpay)}]
            return 200, {"CDCResponse": [{"QueryResponse": qr}],
                         "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if method == "POST" and path == "customer":
            if any(c["DisplayName"] == json_body["DisplayName"] for c in self.customers.values()):
                return 400, {"Fault": {"Error": [{"Message": "Duplicate Name Exists Error", "code": "6240"}]}}
            cid = self._id()
            self.customers[cid] = {"Id": cid, **json_body}
            return 200, {"Customer": self.customers[cid]}
        if method == "POST" and path == "invoice" and q.get("operation") == "void":
            inv = self.invoices[json_body["Id"]]
            assert json_body["SyncToken"] == inv["SyncToken"]
            inv["voided"] = True
            return 200, {"Invoice": inv}
        if method == "POST" and path == "invoice":
            if json_body["CustomerRef"]["value"] in self.refuse_invoice_for:
                return 400, {"Fault": {"Error": [{"Message": "Business Validation Error",
                                                  "Detail": "Select a customer with a billing address"}]}}
            iid = self._id()
            tot = sum(l["Amount"] for l in json_body["Line"])
            self.invoices[iid] = {"Id": iid, "SyncToken": "0", "TotalAmt": tot, "Balance": tot, **json_body}
            return 200, {"Invoice": self.invoices[iid]}
        if method == "GET" and path.startswith("customer/"):
            return 200, {"Customer": self.customers[path.split("/")[1]]}
        if method == "GET" and path.startswith("bill/"):
            return 200, {"Bill": self.bills[path.split("/")[1]]}
        if method == "GET" and path.startswith("invoice/"):
            return 200, {"Invoice": self.invoices[path.split("/")[1]]}
        if method == "POST" and path.startswith("invoice/") and path.endswith("/send"):
            self.sent.append((path.split("/")[1], q["sendTo"]))
            return 200, {"Invoice": self.invoices[path.split("/")[1]]}
        if method == "POST" and path == "payment":
            pid = self._id()
            self.payments[pid] = {"Id": pid, **json_body}
            self.cdc.append(self.payments[pid])   # our own payment shows up in the change feed too
            return 200, {"Payment": self.payments[pid]}
        return 400, {"Fault": {"Error": [{"Message": f"unexpected {method} {path}"}]}}

    def query(self, sql):
        s = sql.lower()
        if s.startswith("select * from customer where active"):
            return {"Customer": list(self.customers.values())}
        if s.startswith("select * from customer where displayname"):
            name = sql.split("'", 1)[1].rsplit("'", 1)[0].replace("\\'", "'")
            return {"Customer": [c for c in self.customers.values() if c["DisplayName"] == name]}
        if s.startswith("select * from invoice where docnumber"):
            num = sql.split("'", 1)[1].rsplit("'", 1)[0]
            return {"Invoice": [i for i in self.invoices.values() if i.get("DocNumber") == num]}
        if s.startswith("select * from invoice where balance > '0'"):
            return {"Invoice": [i for i in self.invoices.values() if i.get("Balance", 0) > 0 and not i.get("voided")]}
        if s.startswith("select * from invoice where balance = '0'"):
            return {"Invoice": [i for i in self.invoices.values() if i.get("Balance", 1) == 0 and not i.get("voided")]}
        if s.startswith("select * from bill where balance > '0'"):
            return {"Bill": [b for b in self.bills.values() if b["Balance"] > 0]}
        if "from item where name = 'services'" in s:
            return {"Item": list(self.items.values())}
        raise AssertionError("unexpected query " + sql)

    def writes(self):
        return [c for c in self.calls if c[0] == "POST" and "/v3/company/" in c[1]]

    def qb_inv(self, doc):
        return [i for i in self.invoices.values() if i["DocNumber"] == doc]


Q = FakeQB()


def _db():
    return db_layer.connect(pragma_fk=False)


def _uids(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]


def _orgs(conn):
    return [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Qbtest %'").fetchall()]


def _purge_all(conn):
    orgs = _orgs(conn)
    if orgs:
        ph = ",".join("?" * len(orgs))
        inv = [r["id"] for r in conn.execute(f"SELECT id FROM invoice WHERE client_org_id IN ({ph})", orgs).fetchall()]
        if inv:
            conn.execute(f"DELETE FROM invoice_payment WHERE invoice_id IN ({','.join('?' * len(inv))})", inv)
        conn.execute(f"DELETE FROM invoice WHERE client_org_id IN ({ph})", orgs)
        conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph})", orgs)
        conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph})", orgs)
        conn.execute(f"DELETE FROM crm_organization WHERE id IN ({ph})", orgs)
    conn.execute("DELETE FROM qbo_customer")
    conn.execute("DELETE FROM qbo_connection")
    try:
        conn.execute("DELETE FROM invoice WHERE source = 'quickbooks'")
        conn.execute("DELETE FROM payable WHERE source = 'quickbooks'")
        conn.execute("DELETE FROM crm_task WHERE title LIKE 'Qbtest%'")
    except Exception:
        conn.rollback()
    conn.commit()
    purge_users(conn, _uids(conn))
    conn.commit()


def _seed() -> dict:
    conn = _db()
    try:
        _purge_all(conn)
        ids = {}
        for key, role in (("admin", "admin"), ("cs", "c_suite"), ("pm", "pm")):
            email = f"{TAG}.{key}@{DOMAIN}"
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, display_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, ?, 1, 'active', 0, 0)", (email, auth.hash_password(PW), role, f"Qbo {key}", f"Qbo {key}"))
            ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        for key, name in (("a", SECRET_NAME), ("b", "Qbtest Hudson Park Management LLC"), ("c", "Qbtest 410 Grand Street HDFC"),
                          ("d", "Qbtest Mott Haven Lofts")):
            conn.execute("INSERT INTO crm_organization (name, relationship_type, status, created_at) VALUES (?, 'client', 'active', ?)",
                         (name, datetime.now().isoformat(timespec="seconds")))
            ids[key] = conn.execute("SELECT id FROM crm_organization WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute("INSERT INTO crm_contact (org_id, full_name, email) VALUES (?, 'Pat Payable', ?)", (ids["b"], SECRET_EMAIL))
        conn.commit()
        return ids
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        _purge_all(conn)
    finally:
        conn.close()
    for k, v in SAVED.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(key):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{key}@{DOMAIN}", "password": PW})
    assert r.status_code == 200, (key, r.status_code)
    return c


def _ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.get_json())
    return r.get_json()


def _inv(iid):
    conn = _db()
    try:
        r = conn.execute("SELECT * FROM invoice WHERE id = ?", (iid,)).fetchone()
        return dict(r)
    finally:
        conn.close()


def _conn_row():
    conn = _db()
    try:
        r = conn.execute("SELECT * FROM qbo_connection").fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _connect(c, *, state_ok=True, realm=REALM, exchange=None, error=None):
    r = c.get("/quickbooks/connect")
    assert r.status_code == 302, r.status_code
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["Location"]).query))
    Q.next_exchange = exchange or (200, {"refresh_token": REFRESH1, "access_token": "fake-access", "expires_in": 3600})
    args = {"state": q["state"] if state_ok else "wrong", "realmId": realm}
    if error:
        args["error"] = error
    else:
        args["code"] = "auth-code"
    r = c.get("/quickbooks/callback?" + urllib.parse.urlencode(args))
    assert r.status_code == 302, r.status_code
    return r.headers["Location"], q


def main() -> int:
    ids = _seed()
    passed = 0
    qbo._http = Q.http
    qbo.SYNC_INLINE = True
    admin, cs, pm = (_login(k) for k in ("admin", "cs", "pm"))

    # ---- invoices that exist before QuickBooks is connected
    def new_inv(org, amount, kind="progress", issue=True):
        d = _ok(cs.post("/api/invoices", json={"client_org_id": org, "kind": kind, "amount": amount,
                                               "description": "Qbtest facade work"}), 201)["data"]
        if issue:
            _ok(cs.post(f"/api/invoices/{d['id']}/issue", json={}))
        return d["id"]
    inv_a = new_inv(ids["a"], 12500, "deposit")
    inv_c = new_inv(ids["c"], 21740)
    inv_b = new_inv(ids["b"], 48210.5, issue=False)

    # ---- off until configured
    for k in CONFIG:
        os.environ.pop(k, None)
    st = _ok(admin.get("/api/quickbooks/status"))["data"]
    assert st["available"] is False and st["reason"] == "quickbooks_not_configured" and st["can_connect"] is True, st
    assert admin.get("/quickbooks/connect").status_code == 404
    os.environ.update({k: v for k, v in CONFIG.items() if k != "SSC_FIELD_ENCRYPTION_KEY"})
    assert _ok(admin.get("/api/quickbooks/status"))["data"]["reason"] == "encryption_key_missing"
    os.environ.update(CONFIG)
    st = _ok(admin.get("/api/quickbooks/status"))["data"]
    assert st["available"] is True and st["connected"] is False and st["environment"] == "sandbox"
    assert qbo.redirect_uri() == "https://app.example.test/quickbooks/callback"
    assert cs.get("/api/quickbooks/status").status_code == 403, "C-suite never sees QuickBooks"
    assert pm.get("/api/quickbooks/status").status_code == 403
    assert Q.calls == [], "nothing connected: QuickBooks is never called"
    passed += 1; print("  switch ok")

    # ---- connect: gates + refusals
    assert cs.get("/quickbooks/connect").status_code == 403 and pm.get("/quickbooks/connect").status_code == 403
    anon = server.app.test_client().get("/quickbooks/connect")
    assert anon.status_code in (302, 401) and "appcenter.intuit.com" not in anon.headers.get("Location", "")
    loc, q = _connect(admin, state_ok=False)
    assert loc.endswith("/company?qb=state#finance") and _conn_row() is None, loc
    assert q["scope"] == "com.intuit.quickbooks.accounting" and q["response_type"] == "code"
    assert q["client_id"] == "test-intuit-client" and q["redirect_uri"] == qbo.redirect_uri()
    loc, _ = _connect(admin, error="access_denied")
    assert loc.endswith("qb=denied#finance") and _conn_row() is None
    loc, _ = _connect(admin, realm="../etc")
    assert loc.endswith("qb=denied#finance") and _conn_row() is None
    loc, _ = _connect(admin, exchange=(400, {"error": "invalid_grant"}))
    assert loc.endswith("qb=failed#finance") and _conn_row() is None
    passed += 1; print("  refusals ok")

    buf = io.StringIO(); h = logging.StreamHandler(buf); root = logging.getLogger(); lvl = root.level
    root.setLevel(logging.INFO); root.addHandler(h)
    try:
        # ---- a good connect
        loc, _ = _connect(admin)
        assert loc.endswith("/company?qb=connected#finance"), loc
        row = _conn_row()
        assert row["realm_id"] == REALM and row["status"] == "active" and row["environment"] == "sandbox"
        assert row["refresh_token_enc"].startswith("v1:") and REFRESH1 not in row["refresh_token_enc"]
        assert secret_box.decrypt_text(row["refresh_token_enc"]) == REFRESH1
        st = _ok(admin.get("/api/quickbooks/status"))["data"]
        assert st["connected"] and st["company_name"] == "Superstars Contracting Corp (sandbox)" and st["matched"] is False
        assert st["connected_by"] == "Qbo admin" and st["stats"]["not_sent"] == 2 and st["open_url"].startswith("https://app.sandbox.qbo.intuit.com/")
        assert Q.writes() == [], "connecting alone writes nothing to QuickBooks"
        passed += 1; print("  connect ok")

        # ---- match clients
        assert pm.get("/api/quickbooks/clients").status_code == 403
        d = _ok(admin.get("/api/quickbooks/clients"))["data"]
        by = {c["org_id"]: c for c in d["clients"]}
        assert set(by) >= {ids["a"], ids["b"], ids["c"]} and ids["d"] not in by, "clients we invoice"
        assert by[ids["a"]]["suggestion"] == {"id": "101", "name": SECRET_NAME, "how": "Same name"}
        assert by[ids["b"]]["suggestion"]["id"] == "102" and by[ids["b"]]["suggestion"]["how"] == "Same billing email"
        assert by[ids["c"]]["suggestion"] is None
        assert d["first_time"] is True and d["open_unsent"] == 2 and len(d["customers"]) == 3
        assert qbo.suggest({"name": "Eastview Gardens Co-op"}, [], [{"id": "9", "name": "Eastview Gardens Cooperative", "email": None}])["how"] == "Same name"
        assert qbo.suggest({"name": "Qbtest Hudson Park Management LLC"}, [], [{"id": "9", "name": "Qbtest Hudson Park Mgmt", "email": None}])["how"] == "Same name"
        assert qbo.suggest({"name": "Riverside Towers Condo Board"}, [], [{"id": "9", "name": "Riverside Towers Condominium", "email": None}])["how"] == "Similar name"
        assert qbo.suggest({"name": "Totally Different"}, [], [{"id": "9", "name": "Kingsbridge Realty", "email": None}]) is None
        assert admin.post("/api/quickbooks/clients", json={"matches": [{"org_id": ids["a"], "qb_customer_id": "1 or 1=1"}]}).status_code == 400
        res = _ok(admin.post("/api/quickbooks/clients", json={"send_open": True, "matches": [
            {"org_id": ids["a"], "qb_customer_id": "101", "qb_name": SECRET_NAME},
            {"org_id": ids["b"], "qb_customer_id": "102", "qb_name": "HP Mgmt Co"},
            {"org_id": ids["c"], "qb_customer_id": None}]}))["data"]
        assert res["linked"] == 2 and res["created"] == 1 and res["queued"] == 2 and res["matched"] is True, res
        made_c = [c for c in Q.customers.values() if c["DisplayName"] == "Qbtest 410 Grand Street HDFC"]
        assert len(made_c) == 1
        qa, qc = Q.qb_inv(_inv(inv_a)["number"]), Q.qb_inv(_inv(inv_c)["number"])
        assert len(qa) == 1 and len(qc) == 1, "the open invoices issued before were sent"
        assert qa[0]["CustomerRef"]["value"] == "101" and qc[0]["CustomerRef"]["value"] == made_c[0]["Id"]
        assert qa[0]["Line"][0]["Amount"] == 12500 and qa[0]["Line"][0]["SalesItemLineDetail"]["ItemRef"]["value"] == "1"
        assert qa[0]["DueDate"] == _inv(inv_a)["due_at"] and qa[0]["TxnDate"] == _inv(inv_a)["issued_at"]
        ia = _inv(inv_a)
        assert ia["qb_invoice_id"] == qa[0]["Id"] and ia["qb_status"] == "created" and ia["qb_realm_id"] == REALM
        assert _ok(admin.get("/api/quickbooks/status"))["data"]["stats"]["clients_linked"] == 3
        any_inv = inv_a
        for method, path in (("get", "/api/quickbooks/status"), ("get", "/api/quickbooks/clients"), ("post", "/api/quickbooks/clients"),
                             ("post", "/api/quickbooks/sync"), ("post", f"/api/quickbooks/invoices/{any_inv}/push"),
                             ("post", f"/api/quickbooks/invoices/{any_inv}/send"), ("post", "/api/quickbooks/disconnect"),
                             ("get", "/quickbooks/connect"), ("get", "/quickbooks/callback?state=x&code=y&realmId=1")):
            for who in (cs, pm):
                r = who.get(path) if method == "get" else who.post(path, json={})
                assert r.status_code == 403, (path, r.status_code)
        passed += 1; print("  matching ok (C-suite / PM refused everywhere)")

        # ---- issue here -> created there, with the bill email
        _ok(cs.post(f"/api/invoices/{inv_b}/issue", json={}))
        qb_b = Q.qb_inv(_inv(inv_b)["number"])
        assert len(qb_b) == 1 and qb_b[0]["CustomerRef"]["value"] == "102" and qb_b[0]["BillEmail"]["Address"] == SECRET_EMAIL
        assert _inv(inv_b)["qb_status"] == "created"
        drafts = len(Q.invoices)
        inv_draft = new_inv(ids["a"], 999, issue=False)
        assert len(Q.invoices) == drafts and _inv(inv_draft)["qb_status"] is None, "a draft never goes to QuickBooks"
        passed += 1; print("  issue ok")

        # ---- a payment recorded here -> a QuickBooks payment
        _ok(cs.post(f"/api/invoices/{inv_a}/payments", json={"amount": 5000, "method": "check", "reference": "Chk 2231"}), 201)
        pays = [p for p in Q.payments.values() if p["Line"][0]["LinkedTxn"][0]["TxnId"] == ia["qb_invoice_id"]]
        assert len(pays) == 1 and pays[0]["TotalAmt"] == 5000 and pays[0]["PaymentRefNum"] == "Chk 2231"
        conn = _db()
        try:
            p = dict(conn.execute("SELECT * FROM invoice_payment WHERE invoice_id = ?", (inv_a,)).fetchone())
        finally:
            conn.close()
        assert p["qb_payment_id"] == f"{pays[0]['Id']}:{ia['qb_invoice_id']}"
        passed += 1; print("  payment out ok")

        # ---- payments recorded in QuickBooks come back (once), our own echo does not
        qc_id = _inv(inv_c)["qb_invoice_id"]
        Q.cdc.append({"Id": "9001", "TxnDate": date.today().isoformat(), "PaymentRefNum": "ACH-77", "TotalAmt": 21740,
                      "Line": [{"Amount": 21740, "LinkedTxn": [{"TxnId": qc_id, "TxnType": "Invoice"}]}]})
        Q.cdc.append({"domain": "QBO", "status": "Deleted", "Id": "8000"})
        res = _ok(admin.post("/api/quickbooks/sync", json={}))["data"]
        assert res["payments"] == 1, res
        ic = _inv(inv_c)
        assert ic["status"] == "paid" and ic["amount_paid"] == 21740 and ic["qb_status"] == "paid"
        assert _inv(inv_a)["amount_paid"] == 5000, "our own payment in the feed is not counted twice"
        conn = _db()
        try:
            back = dict(conn.execute("SELECT * FROM invoice_payment WHERE invoice_id = ?", (inv_c,)).fetchone())
        finally:
            conn.close()
        assert back["source"] == "quickbooks" and back["method"] == "quickbooks" and back["reference"] == "QuickBooks payment ACH-77"
        before = len(Q.payments)
        assert _ok(admin.post("/api/quickbooks/sync", json={}))["data"]["payments"] == 0, "never twice"
        assert len(Q.payments) == before, "a payment that came from QuickBooks is not sent back"
        assert _ok(admin.get("/api/quickbooks/status"))["data"]["stats"]["payments_back_month"] == 1
        passed += 1; print("  payments back ok")

        # ---- void here -> void there
        _ok(cs.post(f"/api/invoices/{inv_b}/void", json={"reason": "wrong period"}))
        assert Q.invoices[qb_b[0]["Id"]].get("voided") and _inv(inv_b)["qb_status"] == "voided"
        passed += 1; print("  void ok")

        # ---- a refusal: needs you, then Try again
        inv_d = new_inv(ids["d"], 3100, issue=False)
        orig = Q.http
        def refusing(method, url, **kw):
            if method == "POST" and url.split("?")[0].endswith("/invoice") and "operation" not in url:
                return 400, {"Fault": {"Error": [{"Message": "Business Validation Error", "Detail": "Select a customer with a billing address"}]}}
            return orig(method, url, **kw)
        qbo._http = refusing
        _ok(cs.post(f"/api/invoices/{inv_d}/issue", json={}))   # the app write still succeeds
        idd = _inv(inv_d)
        assert idd["status"] == "issued" and idd["qb_status"] == "error" and "billing address" in idd["qb_error"]
        st = _ok(admin.get("/api/quickbooks/status"))["data"]
        assert [n["number"] for n in st["needs_you"]] == [idd["number"]] and "billing address" in st["needs_you"][0]["error"]
        assert admin.post(f"/api/quickbooks/invoices/{inv_d}/push", json={}).status_code == 502
        qbo._http = orig
        _ok(admin.post(f"/api/quickbooks/invoices/{inv_d}/push", json={}))
        assert _inv(inv_d)["qb_status"] == "created" and not _inv(inv_d)["qb_error"] and len(Q.qb_inv(idd["number"])) == 1
        assert _ok(admin.get("/api/quickbooks/status"))["data"]["needs_you"] == []
        # a retry after a lost answer finds the invoice by its number instead of making a second one
        conn = _db()
        try:
            conn.execute("UPDATE invoice SET qb_invoice_id = NULL, qb_status = 'pending' WHERE id = ?", (inv_d,))
            conn.commit()
        finally:
            conn.close()
        _ok(admin.post("/api/quickbooks/sync", json={}))
        assert len(Q.qb_inv(idd["number"])) == 1 and _inv(inv_d)["qb_invoice_id"]
        assert pm.post(f"/api/quickbooks/invoices/{inv_d}/push", json={}).status_code == 403
        passed += 1; print("  refusal ok")

        # ---- Send from QuickBooks
        r = _ok(admin.post(f"/api/quickbooks/invoices/{inv_d}/send", json={"to_email": "billing@mott.example"}), 201)["data"]
        assert Q.sent[-1] == (_inv(inv_d)["qb_invoice_id"], "billing@mott.example") and r["qb_status"] == "emailed" and r["sent_to"] == "billing@mott.example"
        assert admin.post(f"/api/quickbooks/invoices/{inv_c}/send", json={"to_email": "x@y.example"}).status_code == 409, "paid: nothing to send"
        assert "pdf_path" not in r and "token_hash" not in r
        passed += 1; print("  send ok")

        # ---- 1b: bring in QuickBooks' open invoices + unpaid bills
        def qd(n):
            return (date.today() + timedelta(days=n)).isoformat()
        Q.customers["201"] = {"Id": "201", "DisplayName": "Qbtest Grand St Associates", "PrimaryEmailAddr": {"Address": "billing@grandst.example"}}
        Q.invoices["701"] = {"Id": "701", "SyncToken": "0", "DocNumber": "1031", "CustomerRef": {"value": "101"}, "TotalAmt": 18400,
                             "Balance": 18400, "TxnDate": qd(-36), "DueDate": qd(-6), "Line": [{"Description": "Progress billing #2", "Amount": 18400}]}
        Q.invoices["702"] = {"Id": "702", "SyncToken": "0", "DocNumber": "1019", "CustomerRef": {"value": "201", "name": "Qbtest Grand St Associates"},
                             "TotalAmt": 6250, "Balance": 6250, "TxnDate": qd(-101), "DueDate": qd(-71), "Line": [{"Description": "Shed rental", "Amount": 6250}]}
        Q.invoices["703"] = {"Id": "703", "SyncToken": "0", "DocNumber": "1027", "CustomerRef": {"value": "102"}, "TotalAmt": 39880,
                             "Balance": 19880, "TxnDate": qd(-68), "DueDate": qd(-38), "Line": [{"Description": "AIA application #2", "Amount": 39880}]}
        Q.invoices["704"] = {"Id": "704", "SyncToken": "0", "DocNumber": "1001", "CustomerRef": {"value": "101"}, "TotalAmt": 900,
                             "Balance": 0, "TxnDate": qd(-20), "DueDate": qd(-10), "Line": [{"Description": "Paid already", "Amount": 900}]}
        Q.bills["801"] = {"Id": "801", "DocNumber": "B-7781", "VendorRef": {"value": "55", "name": "Qbtest Bricklayers Supply"}, "TotalAmt": 4380,
                          "Balance": 4380, "TxnDate": qd(-33), "DueDate": qd(-3), "Line": [{"Description": "Mortar, lintels"}]}
        Q.bills["802"] = {"Id": "802", "DocNumber": "B-7700", "VendorRef": {"value": "56", "name": "Qbtest Paid Vendor"}, "TotalAmt": 100,
                          "Balance": 0, "TxnDate": qd(-40), "DueDate": qd(-10), "Line": []}
        for who in (cs, pm):
            for method, path in (("get", "/api/quickbooks/import/preview"), ("post", "/api/quickbooks/import"), ("get", "/api/quickbooks/review"),
                                 ("post", f"/api/quickbooks/review/{ids['a']}/approve"), ("post", f"/api/quickbooks/review/{ids['a']}/merge"),
                                 ("post", f"/api/quickbooks/invoices/{inv_a}/followup")):
                r = who.get(path) if method == "get" else who.post(path, json={})
                assert r.status_code == 403, (path, r.status_code)
        pv = _ok(admin.get("/api/quickbooks/import/preview"))["data"]
        assert pv["invoices"] == 3 and pv["invoices_open"] == 18400 + 6250 + 19880 and pv["invoices_late"] == 3 and pv["late_30"] == 2, pv
        assert pv["bills"] == 1 and pv["bills_open"] == 4380 and pv["customers_new"] == 1 and pv["customers_matched"] == 2, pv
        assert admin.post("/api/quickbooks/import", json={"paid_days": 7}).status_code == 400
        res = _ok(admin.post("/api/quickbooks/import", json={}))["data"]
        assert res["invoices"] == 3 and res["bills"] == 1 and res["review"] == 1 and res["imported"] is True, res
        assert _ok(admin.post("/api/quickbooks/import", json={}))["data"]["invoices"] == 0, "bringing in twice adds nothing"
        conn = _db()
        try:
            imp = {r["qb_invoice_id"]: dict(r) for r in conn.execute("SELECT * FROM invoice WHERE source = 'quickbooks'").fetchall()}
            bill = dict(conn.execute("SELECT * FROM payable WHERE source = 'quickbooks'").fetchone())
            new_org = conn.execute("SELECT o.id, o.name FROM crm_organization o JOIN qbo_customer q ON q.org_id = o.id "
                                   "WHERE q.qb_customer_id = '201' AND q.review = 'pending'").fetchone()
            new_email = conn.execute("SELECT email FROM crm_contact WHERE org_id = ?", (new_org["id"],)).fetchone()["email"]
        finally:
            conn.close()
        assert set(imp) == {"701", "702", "703"} and "704" not in imp, "open ones only; ours are not brought in twice"
        assert imp["701"]["number"] == "QB-1031" and imp["701"]["client_org_id"] == ids["a"] and imp["701"]["status"] == "issued"
        assert imp["703"]["amount_paid"] == 20000 and imp["703"]["client_org_id"] == ids["b"] and imp["703"]["due_at"] == qd(-38)
        assert imp["702"]["client_org_id"] == new_org["id"] and new_org["name"] == "Qbtest Grand St Associates" and new_email == "billing@grandst.example"
        assert bill["vendor"] == "Qbtest Bricklayers Supply" and bill["amount"] == 4380 and bill["status"] == "open" and bill["due_at"] == qd(-3)
        # admins only, everywhere
        cs_nums = [r["number"] for r in _ok(cs.get("/api/invoices"))["data"]]
        ad_nums = [r["number"] for r in _ok(admin.get("/api/invoices"))["data"]]
        assert "QB-1031" in ad_nums and not any(n.startswith("QB-") for n in cs_nums), "C-suite never sees QuickBooks invoices"
        assert cs.get(f"/api/invoices/{imp['701']['id']}").status_code == 404
        s_cs, s_ad = _ok(cs.get("/api/invoices"))["meta"]["summary"], _ok(admin.get("/api/invoices"))["meta"]["summary"]
        assert round(s_ad["open_value"] - s_cs["open_value"], 2) == 18400 + 6250 + 19880, (s_ad["open_value"], s_cs["open_value"])
        pay_cs = _ok(cs.get("/api/company/portal/payables?status=open"))["data"]["items"]
        pay_ad = _ok(admin.get("/api/company/portal/payables?status=open"))["data"]["items"]
        assert not any(b.get("source") == "quickbooks" for b in pay_cs) and any(b.get("source") == "quickbooks" for b in pay_ad)
        t_cs, t_ad = _ok(cs.get("/api/company/portal/today"))["data"], _ok(admin.get("/api/company/portal/today"))["data"]
        assert any(c["key"] == "qbo:review" for c in t_ad["needs"]) and not any(c["key"] == "qbo:review" for c in t_cs["needs"])
        assert any(c["key"] == f"invoice:{imp['702']['id']}" for c in t_ad["needs"]) and not any(c["key"] == f"invoice:{imp['702']['id']}" for c in t_cs["needs"])
        cal = lambda c: [m for d in _ok(c.get(f"/api/company/portal/calendar?from={qd(-10)}&to={qd(10)}"))["data"]["days"].values() for m in d.get("milestones", [])]
        assert any(m.get("number") == "QB-1031" for m in cal(admin)) and not any(m.get("number") == "QB-1031" for m in cal(cs))
        # QuickBooks owns them
        assert admin.patch(f"/api/invoices/{imp['701']['id']}", json={"notes": "x"}).status_code in (400, 409)
        assert admin.post(f"/api/invoices/{imp['701']['id']}/void", json={}).status_code == 409
        assert admin.post(f"/api/company/portal/payables/{bill['id']}/pay", json={}).status_code in (400, 409)
        assert admin.post(f"/api/company/portal/payables/{bill['id']}/void", json={}).status_code in (400, 409)
        # a payment recorded here goes to QuickBooks
        _ok(admin.post(f"/api/invoices/{imp['701']['id']}/payments", json={"amount": 5000, "method": "check", "reference": "Chk 9"}), 201)
        assert _inv(imp["701"]["id"])["amount_paid"] == 5000 and _inv(imp["701"]["id"])["status"] == "issued"
        assert any(p["Line"][0]["LinkedTxn"][0]["TxnId"] == "701" and p["TotalAmt"] == 5000 for p in Q.payments.values())
        # follow-up
        fu = _ok(admin.post(f"/api/quickbooks/invoices/{imp['702']['id']}/followup",
                            json={"title": "Qbtest call Grand St about 1019", "assignee_user_id": ids["admin"], "due_date": qd(2)}), 201)["data"]
        assert fu["followup_title"] == "Qbtest call Grand St about 1019" and fu["followup_due"] == qd(2) and fu["followup_who"] == "Qbo admin"
        assert admin.post(f"/api/quickbooks/invoices/{imp['702']['id']}/followup", json={"title": ""}).status_code == 400
        # reminder from QuickBooks
        _ok(admin.post(f"/api/quickbooks/invoices/{imp['703']['id']}/send", json={"to_email": "ap@hudson.example"}), 201)
        assert Q.sent[-1] == ("703", "ap@hudson.example") and _inv(imp["703"]["id"])["sent_to"] == "ap@hudson.example"
        # QuickBooks changes follow: paid in full, a new invoice, a bill paid
        Q.invoices["701"]["Balance"] = 0
        Q.cdc_inv.append(Q.invoices["701"])
        Q.invoices["705"] = {"Id": "705", "SyncToken": "0", "DocNumber": "1038", "CustomerRef": {"value": "101"}, "TotalAmt": 7250,
                             "Balance": 7250, "TxnDate": qd(0), "DueDate": qd(30), "Line": [{"Description": "Change order #4", "Amount": 7250}]}
        Q.cdc_inv.append(Q.invoices["705"])
        Q.bills["801"]["Balance"] = 0
        Q.cdc_billpay.append({"Id": "990", "Line": [{"Amount": 4380, "LinkedTxn": [{"TxnId": "801", "TxnType": "Bill"}]}]})
        _ok(admin.post("/api/quickbooks/sync", json={}))
        assert _inv(imp["701"]["id"])["status"] == "paid" and _inv(imp["701"]["id"])["amount_paid"] == 18400
        conn = _db()
        try:
            n705 = conn.execute("SELECT number, amount, status FROM invoice WHERE qb_invoice_id = '705'").fetchone()
            b801 = conn.execute("SELECT status, paid_amount, payment_method FROM payable WHERE id = ?", (bill["id"],)).fetchone()
        finally:
            conn.close()
        assert n705 and n705["number"] == "QB-1038" and n705["amount"] == 7250 and n705["status"] == "issued"
        assert b801["status"] == "paid" and b801["paid_amount"] == 4380 and b801["payment_method"] == "quickbooks"
        _ok(admin.post("/api/quickbooks/sync", json={}))
        assert _inv(imp["701"]["id"])["amount_paid"] == 18400, "nothing counted twice"
        # review: merge the new client into one we have; a second new one is approved
        rv = _ok(admin.get("/api/quickbooks/review"))["data"]
        assert [r["org_id"] for r in rv] == [new_org["id"]] and rv[0]["email"] == "billing@grandst.example" and rv[0]["open_value"] == 6250
        assert admin.post(f"/api/quickbooks/review/{ids['c']}/merge", json={"into_org_id": ids["a"]}).status_code == 404, "only clients made from QuickBooks"
        mg = _ok(admin.post(f"/api/quickbooks/review/{new_org['id']}/merge", json={"into_org_id": ids["c"]}))["data"]
        assert mg["moved"] == 1 and mg["removed"] is True and mg["review"] == 0
        assert _inv(imp["702"]["id"])["client_org_id"] == ids["c"]
        conn = _db()
        try:
            assert conn.execute("SELECT 1 FROM crm_organization WHERE id = ?", (new_org["id"],)).fetchone() is None
            assert conn.execute("SELECT org_id FROM qbo_customer WHERE qb_customer_id = '201'").fetchone()["org_id"] == ids["c"]
        finally:
            conn.close()
        Q.customers["202"] = {"Id": "202", "DisplayName": "Qbtest Kingsbridge Realty"}
        Q.invoices["706"] = {"Id": "706", "SyncToken": "0", "DocNumber": "1036", "CustomerRef": {"value": "202"}, "TotalAmt": 4870,
                             "Balance": 4870, "TxnDate": qd(-1), "DueDate": qd(21), "Line": [{"Description": "Pointing", "Amount": 4870}]}
        Q.cdc_inv.append(Q.invoices["706"])
        Q.invoices["702"]["Balance"] = 6250
        Q.cdc_inv.append({"domain": "QBO", "status": "Deleted", "Id": "702"})
        _ok(admin.post("/api/quickbooks/sync", json={}))
        assert _inv(imp["702"]["id"])["status"] == "void", "deleted in QuickBooks"
        rv = _ok(admin.get("/api/quickbooks/review"))["data"]
        assert [r["name"] for r in rv] == ["Qbtest Kingsbridge Realty"]
        assert _ok(admin.post(f"/api/quickbooks/review/{rv[0]['org_id']}/approve", json={}))["data"]["review"] == 0
        assert _ok(admin.get("/api/quickbooks/status"))["data"]["review"] == 0
        kingsbridge = rv[0]["org_id"]
        passed += 1; print("  bring in + review ok")

        # ---- the rotating refresh token, the read cap
        qbo._TOKENS.clear(); Q.rotate = True
        _ok(admin.post("/api/quickbooks/sync", json={}))
        assert secret_box.decrypt_text(_conn_row()["refresh_token_enc"]) == REFRESH2, "the newest refresh token is kept"
        os.environ["QBO_DAILY_READ_CAP"] = "100"
        conn = _db()
        try:
            conn.execute("UPDATE qbo_connection SET reads_day = ?, reads_count = 100", (date.today().isoformat(),))
            conn.commit()
        finally:
            conn.close()
        reads = len([c for c in Q.calls if c[0] == "GET"])
        res = _ok(admin.post("/api/quickbooks/sync", json={}))["data"]
        assert res.get("capped") and "limit" in res["problem"] and len([c for c in Q.calls if c[0] == "GET"]) == reads
        assert admin.get("/api/quickbooks/clients").status_code == 429
        os.environ.pop("QBO_DAILY_READ_CAP")
        passed += 1; print("  tokens + cap ok")

        # ---- an expired sign-in
        qbo._TOKENS.clear(); Q.refresh_ok = False
        inv_e = new_inv(ids["a"], 700)   # the app write never fails
        assert _conn_row()["status"] == "needs_reconnect" and _inv(inv_e)["qb_status"] == "pending"
        st = _ok(admin.get("/api/quickbooks/status"))["data"]
        assert st["connected"] is False and st["status"] == "needs_reconnect" and "reconnect" in st["problem"]
        assert admin.post("/api/quickbooks/sync", json={}).status_code == 409
        Q.refresh_ok = True
        loc, _ = _connect(admin)
        assert loc.endswith("qb=connected#finance") and _conn_row()["status"] == "active" and _conn_row()["matched_at"], "matches kept"
        _ok(admin.post("/api/quickbooks/sync", json={}))
        assert _inv(inv_e)["qb_invoice_id"], "reconnecting catches up"
        passed += 1; print("  reconnect ok")

        # ---- disconnect
        assert cs.post("/api/quickbooks/disconnect", json={}).status_code == 403 and pm.post("/api/quickbooks/disconnect", json={}).status_code == 403
        _ok(admin.post("/api/quickbooks/disconnect", json={}))
        assert _conn_row() is None and Q.revoked and Q.revoked[-1] == REFRESH1
        st = _ok(admin.get("/api/quickbooks/status"))["data"]
        assert st["connected"] is False and not st.get("status")
        rm = _ok(admin.get("/api/quickbooks/status"))["data"]
        conn = _db()
        try:
            assert conn.execute("SELECT COUNT(*) FROM invoice WHERE source = 'quickbooks'").fetchone()[0] == 0, "test invoices removed"
            assert conn.execute("SELECT COUNT(*) FROM payable WHERE source = 'quickbooks'").fetchone()[0] == 0, "test bills removed"
            assert conn.execute("SELECT COUNT(*) FROM qbo_customer").fetchone()[0] == 0
            assert conn.execute("SELECT 1 FROM crm_organization WHERE id = ?", (kingsbridge,)).fetchone() is None, "clients it created go"
            assert conn.execute("SELECT 1 FROM crm_organization WHERE id = ?", (ids["d"],)).fetchone(), "our own clients stay"
            pay_a = conn.execute("SELECT qb_payment_id FROM invoice_payment WHERE invoice_id = ?", (inv_a,)).fetchone()
            assert pay_a["qb_payment_id"] is None, "our payment can go to the next company"
        finally:
            conn.close()
        ia2, ic2 = _inv(inv_a), _inv(inv_c)
        assert ia2["qb_invoice_id"] is None and ia2["qb_status"] is None and ia2["amount_paid"] == 5000, "freed, our check kept"
        assert ic2["status"] == "issued" and ic2["amount_paid"] == 0 and ic2["paid_at"] is None, "the TEST payment no longer counts"
        n = len(Q.calls)
        new_inv(ids["a"], 50)
        assert len(Q.calls) == n, "disconnected: issuing never calls QuickBooks"
        passed += 1; print("  disconnect ok")
    finally:
        root.removeHandler(h); root.setLevel(lvl)
    out = buf.getvalue()
    for secret in (REFRESH1, REFRESH2, "fake-access", "Zebulon", SECRET_EMAIL, "21740", "12500"):
        assert secret not in out, ("log leak", secret)
    assert "qbo connected user=" in out and "created in quickbooks" in out
    passed += 1; print("  logs ok")

    # ---- the page
    src = (ROOT / "company_portal.html").read_text(encoding="utf-8")
    for needle in ('id="qb-card"', "/quickbooks/status", 'href="/quickbooks/connect"', "/quickbooks/clients", "/quickbooks/sync",
                   "/quickbooks/disconnect", "Send from QuickBooks", "Open in QuickBooks", 'id="qb-match-overlay"', "qb-send-open",
                   'id="qb-import-overlay"', 'id="qb-review-overlay"', 'id="qb-fu-overlay"', 'id="qb-review-strip"', 'id="inv-aging"',
                   "/quickbooks/import/preview", "/quickbooks/review", "/followup", "From QuickBooks", "Pay in QuickBooks", "qb-review"):
        assert needle in src, needle
    passed += 1; print("  page ok")

    print(f"PASS smoke_quickbooks_316 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_quickbooks_316: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
