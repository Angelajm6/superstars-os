#!/usr/bin/env python3
"""#355 — company vehicles as a fleet: mileage, service, E-ZPass tolls, car bills. Guard.

Boots the real app on an ISOLATED database with a scratch data root. Proves:
  * gating: pm -> 403 on every fleet route; admin / c_suite -> 200
  * E-ZPass tag + service intervals saved on the car (validated)
  * odometer log: miles this month / YTD / by month; a reading LOWER than an earlier one
    is refused; readings ride along with a service and with a car bill
  * service status: last service + next due by date and by miles; "soon" / "overdue"
  * E-ZPass statement import (CSV and Excel): header found below account-info lines,
    payment rows skipped, tolls matched by tag (leading zeros ignored) or plate, unknown
    tags reported not stored, re-uploading the same statement imports nothing new
  * car bill = a payable tagged to the car (category vehicle), with the bill file served
    only through the gated route (no file_path on the wire); "paid" marks it paid
  * the vehicles list carries each car's fleet summary
Run:  venv/bin/python tests/smoke_fleet_355.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_fleet_355_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
DATA_ROOT = Path(_TMP) / "dataroot"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(DATA_ROOT)
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "fleet355"
PW = "FleetSmokePassword2026"
API = "/api/company/portal"
T = date.today()


def D(n: int) -> str:
    return (T + timedelta(days=n)).isoformat()


def _db():
    return db_layer.connect(pragma_fk=False)


def _cleanup_rows(conn, vids):
    for t, c in (("vehicle_toll", "vehicle_id"), ("vehicle_service", "vehicle_id"), ("vehicle_odometer", "vehicle_id")):
        purge(conn, t, c, vids)
    pids = [r["id"] for r in conn.execute("SELECT id FROM payable WHERE vendor LIKE ?", (f"{TAG}%",)).fetchall()]
    purge(conn, "payable_file", "payable_id", pids)
    purge(conn, "payable", "id", pids)
    purge(conn, "vehicle", "id", vids)


def _seed():
    conn = _db()
    try:
        for role in ("admin", "c_suite", "pm"):
            email = f"{TAG}.{role}@example.com"
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                         "VALUES (?, ?, ?, ?, 1, 'active', 0, 1)", (email, auth.hash_password(PW), role, f"Smoke {role} Fleet"))
        conn.commit()
    finally:
        conn.close()


def _cleanup(vids):
    conn = _db()
    try:
        _cleanup_rows(conn, vids)
        conn.commit()
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _login(role):
    c = server.app.test_client()
    r = c.post("/api/auth/login", json={"email": f"{TAG}.{role}@example.com", "password": PW})
    assert r.status_code == 200, (role, r.status_code)
    return c


STATEMENT = (
    "E-ZPass New York Customer Service Center\n"
    "Account: 12345678,Statement period,x\n"
    "\n"
    "Posting Date,Transaction Date,Transaction Time,Tag/Plate Number,Agency,Entry Plaza,Exit Plaza,Amount,Balance\n"
    f"{T.month:02d}/{T.day:02d}/{T.year},{T.month:02d}/{T.day:02d}/{T.year},08:02:11,,, , ,$100.00,$150.00\n"
    f"{T.month:02d}/{T.day:02d}/{T.year},{T.month:02d}/{T.day:02d}/{T.year},07:15:40,0071234567,MTA B&T,,RFK Bridge,($11.19),$138.81\n"
    f"{T.month:02d}/{T.day:02d}/{T.year},{T.month:02d}/{T.day:02d}/{T.year},17:40:02,71234567,MTA B&T,,Throgs Neck,($11.19),$127.62\n"
    f"{T.month:02d}/{T.day:02d}/{T.year},{T.month:02d}/{T.day:02d}/{T.year},09:00:00,FL355AB,PANYNJ,,GWB,($17.63),$110.00\n"
    f"{T.month:02d}/{T.day:02d}/{T.year},{T.month:02d}/{T.day:02d}/{T.year},10:00:00,0099999999,NYSTA,Exit 16,Exit 24,($4.10),$105.90\n"
)


def main() -> int:
    _seed()
    passed = 0
    admin, cs, pm = _login("admin"), _login("c_suite"), _login("pm")
    vids = []
    try:
        # two cars: one with an E-ZPass tag, one matched by plate
        a = admin.post(f"{API}/vehicles", json={"label": f"{TAG} Van 1", "make": "Ford", "plate": "FL355AA"}).get_json()["data"]
        b = admin.post(f"{API}/vehicles", json={"label": f"{TAG} Truck 2", "plate": "FL355AB"}).get_json()["data"]
        vids = [a["id"], b["id"]]
        A = f"{API}/vehicles/{a['id']}"

        # ---- gating
        for path, m in ((f"{A}/fleet", "get"), (f"{A}/readings", "post"), (f"{A}/services", "post"),
                        (f"{A}/tolls", "post"), (f"{A}/bills", "post"), (f"{API}/vehicles/tolls/import", "post")):
            assert getattr(pm, m)(path).status_code == 403, path
        assert cs.get(f"{A}/fleet").status_code == 200
        passed += 1; print("  gating ok")

        # ---- E-ZPass tag + intervals
        r = admin.patch(f"{A}/fleet", json={"ezpass_tag": "0071234567", "service_every_miles": 5000, "service_every_months": 6})
        assert r.status_code == 200 and r.get_json()["data"]["vehicle"]["ezpass_tag"] == "0071234567", r.get_json()
        assert admin.patch(f"{A}/fleet", json={"service_every_miles": 10}).status_code == 400
        passed += 1; print("  fleet fields ok")

        # ---- odometer log + service
        first_of_month = T.replace(day=1)
        before_month = (first_of_month - timedelta(days=3)).isoformat()
        assert admin.post(f"{A}/readings", json={"reading_date": before_month, "odometer": 10000}).status_code == 201
        r = admin.post(f"{A}/services", json={"service_date": before_month, "odometer": 10000, "service_type": "oil_change",
                                              "vendor": "Jiffy", "cost": "89.50"})
        assert r.status_code == 201, r.get_json()
        r = admin.post(f"{A}/readings", json={"reading_date": T.isoformat(), "odometer": 11234})
        assert r.status_code == 201
        s = r.get_json()["data"]["summary"]
        assert s["odometer"] == 11234 and s["miles_month"] == 1234, s
        span_months = max(1.0, (T - (first_of_month - timedelta(days=3))).days / 30.44)
        assert s["miles_per_month"] == round(1234 / span_months), ("average over the span with readings", s)
        assert s["service"]["last_type"] == "oil_change" and s["service"]["due_miles"] == 15000 and s["service"]["state"] == "ok", s
        low = admin.post(f"{A}/readings", json={"reading_date": T.isoformat(), "odometer": 9000})
        assert low.status_code == 400 and "lower" in low.get_json()["error"]
        assert admin.post(f"{A}/services", json={"service_type": "nonsense"}).status_code == 400
        # the next oil change is due at 15,000 — at 14,700 it is "soon", past it "overdue"
        admin.post(f"{A}/readings", json={"reading_date": T.isoformat(), "odometer": 14700})
        assert admin.get(f"{A}/fleet").get_json()["data"]["summary"]["service"]["state"] == "soon"
        admin.post(f"{A}/readings", json={"reading_date": T.isoformat(), "odometer": 15100})
        d = admin.get(f"{A}/fleet").get_json()["data"]
        assert d["summary"]["service"]["state"] == "overdue" and d["miles_by_month"][-1]["miles"] == 5100, d["miles_by_month"][-1]
        rid = next(x["id"] for x in d["readings"] if x["odometer"] == 15100)
        assert admin.delete(f"{A}/readings/{rid}").status_code == 200
        passed += 1; print("  mileage + service ok")

        # ---- E-ZPass statement import (CSV), then the same file again, then Excel
        r = admin.post(f"{API}/vehicles/tolls/import", data={"file": (io.BytesIO(STATEMENT.encode()), "activity.csv")},
                       content_type="multipart/form-data")
        assert r.status_code == 200, r.get_json()
        res = r.get_json()["data"]
        assert res["imported"] == 3 and res["duplicates"] == 0, res           # 2 on the tag (zeros ignored), 1 on the plate
        assert res["unmatched"] == [{"who": "tag …9999", "tolls": 1}], res["unmatched"]
        again = admin.post(f"{API}/vehicles/tolls/import", data={"file": (io.BytesIO(STATEMENT.encode()), "activity.csv")},
                           content_type="multipart/form-data").get_json()["data"]
        assert again["imported"] == 0 and again["duplicates"] == 3, again
        ta = admin.get(f"{A}/fleet").get_json()["data"]
        assert len(ta["tolls"]) == 2 and abs(ta["summary"]["tolls"]["month"] - 22.38) < 0.001, ta["summary"]["tolls"]
        assert any("RFK" in (t["location"] or "") for t in ta["tolls"])
        import openpyxl
        wb = openpyxl.Workbook(); ws = wb.active
        for line in STATEMENT.strip().split("\n"):
            ws.append(line.split(","))
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        x = admin.post(f"{API}/vehicles/tolls/import", data={"file": (buf, "activity.xlsx")}, content_type="multipart/form-data")
        assert x.status_code == 200 and x.get_json()["data"]["duplicates"] == 3, x.get_json()
        bad = admin.post(f"{API}/vehicles/tolls/import", data={"file": (io.BytesIO(b"hello,world\n1,2\n"), "x.csv")},
                         content_type="multipart/form-data")
        assert bad.status_code == 400
        assert admin.post(f"{A}/tolls", json={"txn_date": T.isoformat(), "amount": "6.94", "location": "Holland Tunnel"}).status_code == 201
        passed += 1; print("  E-ZPass import ok")

        # ---- a car bill = a payable tagged to the car, with its file
        pdf = b"%PDF-1.4\n% fleet355 synthetic\n"
        r = admin.post(f"{A}/bills", data={"vendor": f"{TAG} Brake Shop", "amount": "412.00", "kind": "repair",
                                           "description": "front brakes", "issued_at": T.isoformat(), "odometer": "15200",
                                           "file": (io.BytesIO(pdf), "brake-invoice.pdf")},
                       content_type="multipart/form-data")
        assert r.status_code == 201, r.get_json()
        det = r.get_json()["data"]
        bill = det["bills"][0]
        assert bill["status"] == "open" and abs(bill["amount"] - 412) < 1e-6 and "Repair" in bill["description"], bill
        assert det["summary"]["odometer"] == 15200 and det["summary"]["bills"]["open"] == 412.0
        blob = json.dumps(det)
        assert "file_path" not in blob and str(DATA_ROOT) not in blob, "no file paths on the wire"
        f = admin.get(bill["files"][0]["file_url"])
        assert f.status_code == 200 and f.data == pdf
        assert pm.get(bill["files"][0]["file_url"]).status_code == 403
        pay = cs.get(f"{API}/payables").get_json()["data"]
        rows = pay.get("rows") or pay.get("payables") or pay.get("items") or []
        assert any(p.get("id") == bill["id"] and p.get("category") == "vehicle" for p in rows), "the bill is in Accounts Payable"
        r = admin.post(f"{A}/bills", data={"vendor": f"{TAG} DMV", "amount": "140", "kind": "registration", "paid": "1",
                                           "issued_at": T.isoformat()}, content_type="multipart/form-data")
        assert r.status_code == 201 and r.get_json()["data"]["bills"][0]["status"] == "paid", r.get_json()["data"]["bills"][0]
        assert admin.post(f"{A}/bills", data={"vendor": f"{TAG} X", "amount": "5", "file": (io.BytesIO(b"x"), "a.exe")},
                          content_type="multipart/form-data").status_code == 400
        passed += 1; print("  car bills ok")

        # ---- the vehicles list carries the fleet summary
        lst = admin.get(f"{API}/vehicles").get_json()["data"]
        mine = next(v for v in lst if v["id"] == a["id"])
        assert mine["fleet"]["odometer"] == 15200 and mine["ezpass_tag"] == "0071234567", mine.get("fleet")
        page = admin.get("/company").get_data(as_text=True)
        for needle in ("vh-import", "vf-overlay", "/tolls/import", "/bills"):
            assert needle in page, needle
        passed += 1; print("  list + page ok")
    finally:
        _cleanup(vids)
    print(f"PASS smoke_fleet_355 ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_fleet_355: {e}")
        sys.exit(1)
