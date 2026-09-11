#!/usr/bin/env python3
"""
NYC Compliance Watch — pulls DOB permits, violations, and complaints
for active projects from NYC OpenData (Socrata API).

All data we pull is public NYC information — no private data leaves
your laptop. Token is used only for rate-limit accounting.

Datasets:
  - DOB Permit Issuance       — ipu4-2q9a
  - DOB Violations            — 3h2n-5cm9
  - ECB Violations            — 6bgk-3dad
  - DOB Complaints Received   — eabe-havv

Lookup BIN by address:
  - Geosearch (NYC Planning)  — https://geosearch.planninglabs.nyc/v2/search

Usage:
  python nyc_compliance.py refresh-all      # pull all data for all projects
  python nyc_compliance.py refresh <code>   # pull for a single project
  python nyc_compliance.py lookup-bin <code> # only look up the BIN
  python nyc_compliance.py status           # show last pulse run summary
"""

import os
import sys
import json
import sqlite3
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote
from urllib.error import HTTPError, URLError

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "superstars.db"
CONFIG_FILE = SCRIPT_DIR / "api-keys.txt"

# Socrata dataset endpoints (JSON output)
PERMITS_URL = "https://data.cityofnewyork.us/resource/ipu4-2q9a.json"
DOB_VIOLATIONS_URL = "https://data.cityofnewyork.us/resource/3h2n-5cm9.json"
ECB_VIOLATIONS_URL = "https://data.cityofnewyork.us/resource/6bgk-3dad.json"
COMPLAINTS_URL = "https://data.cityofnewyork.us/resource/eabe-havv.json"

# NYC Geosearch — BIN lookup by address (no auth required)
GEOSEARCH_URL = "https://geosearch.planninglabs.nyc/v2/search"

USER_AGENT = "Superstars-PM-Console/1.0 (compliance-watch)"


# =====================================================================
# Config
# =====================================================================

def load_config():
    """Read api-keys.txt with # comment support; returns dict."""
    cfg = {}
    if not CONFIG_FILE.exists():
        return cfg
    with open(CONFIG_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.split('#')[0].strip()
            if not line or '=' not in line:
                continue
            k, v = line.split('=', 1)
            cfg[k.strip()] = v.strip()
    return cfg


def get_app_token():
    cfg = load_config()
    token = cfg.get("NYC_OPENDATA_APP_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "NYC_OPENDATA_APP_TOKEN not set in api-keys.txt. "
            "Get one free at https://data.cityofnewyork.us → Profile → Developer Settings."
        )
    return token


# =====================================================================
# DB helpers
# =====================================================================

def db_conn():
    # #260 — route through the env-driven layer (SSC_DB_URL); production default = live
    # SQLite (60s busy-timeout + WAL preserved by db_layer). Honors test isolation when set.
    import db_layer
    return db_layer.connect()


def log_pulse(project_code, dataset, bin_queried, records, status_code, duration_ms, error=None):
    conn = db_conn()
    conn.execute(
        """INSERT INTO dob_pulse_runs
           (project_code, dataset, bin_queried, records_returned, status_code, duration_ms, error_message)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (project_code, dataset, bin_queried, records, status_code, duration_ms, error)
    )
    conn.commit()
    conn.close()


# =====================================================================
# HTTP helpers
# =====================================================================

def http_get_json(url, params=None, headers=None, timeout=30):
    """GET a URL with query params, return (json, status_code, duration_ms)."""
    if params:
        url = url + ("&" if "?" in url else "?") + urlencode(params)
    headers = headers or {}
    headers.setdefault("User-Agent", USER_AGENT)
    headers.setdefault("Accept", "application/json")

    req = Request(url, headers=headers)
    t0 = time.time()
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            duration_ms = int((time.time() - t0) * 1000)
            return json.loads(body), resp.getcode(), duration_ms
    except HTTPError as e:
        duration_ms = int((time.time() - t0) * 1000)
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code} from {url}: {body}") from e
    except URLError as e:
        duration_ms = int((time.time() - t0) * 1000)
        raise RuntimeError(f"Network error: {e}") from e


def socrata_get(url, where=None, order=None, limit=1000, app_token=None):
    """Query a Socrata dataset with optional SoQL filter."""
    params = {"$limit": limit}
    if where:
        params["$where"] = where
    if order:
        params["$order"] = order
    headers = {}
    if app_token:
        headers["X-App-Token"] = app_token
    return http_get_json(url, params=params, headers=headers)


# =====================================================================
# BIN lookup (one-time per project)
# =====================================================================

def lookup_bin(address, borough=None, zip_code=None):
    """
    Resolve a street address to a NYC BIN (Building Identification Number)
    via the NYC Planning Geosearch service. Returns dict with bin, bbl, borough, lat, lng.
    """
    query = address
    if borough:
        query = f"{address}, {borough}"
    if zip_code:
        query = f"{query} {zip_code}"

    params = {"text": query, "size": 1}
    data, status, _ = http_get_json(GEOSEARCH_URL, params=params)

    if not data or "features" not in data or not data["features"]:
        return None

    feat = data["features"][0]
    props = feat.get("properties", {}) or {}
    geom = feat.get("geometry", {}) or {}
    coords = geom.get("coordinates", [None, None])

    return {
        "bin": props.get("addendum", {}).get("pad", {}).get("bin"),
        "bbl": props.get("addendum", {}).get("pad", {}).get("bbl"),
        "borough": props.get("borough"),
        "house_number": props.get("housenumber"),
        "street_name": props.get("street"),
        "zip_code": props.get("postalcode"),
        "label": props.get("label"),
        "lng": coords[0],
        "lat": coords[1],
    }


def update_project_bin(project_code):
    """Look up BIN/BBL for a project and store on the projects table."""
    conn = db_conn()
    proj = conn.execute(
        "SELECT project_code, name, address, city_zip FROM projects WHERE project_code = ?",
        (project_code,)
    ).fetchone()
    if not proj:
        conn.close()
        raise RuntimeError(f"Project '{project_code}' not found.")

    address = proj["address"] or ""
    city_zip = proj["city_zip"] or ""
    # city_zip looks like "Bronx, NY 10454" — pull borough + zip out
    borough = None
    zip_code = None
    if city_zip:
        parts = city_zip.split(",")
        if parts:
            borough = parts[0].strip()
        # last 5 digits
        for tok in city_zip.replace(",", " ").split():
            if tok.isdigit() and len(tok) == 5:
                zip_code = tok
                break

    print(f"[BIN] Resolving '{address}, {city_zip}' …", flush=True)
    info = lookup_bin(address, borough=borough, zip_code=zip_code)
    if not info:
        conn.close()
        raise RuntimeError(f"Geosearch returned no match for '{address}'")

    print(f"[BIN] Found: BIN={info['bin']}, BBL={info['bbl']}, borough={info['borough']}", flush=True)

    conn.execute(
        """UPDATE projects
           SET bin = ?, bbl = ?, borough = ?, house_number = ?, street_name = ?, zip_code = ?,
               updated_at = CURRENT_TIMESTAMP
           WHERE project_code = ?""",
        (info["bin"], info["bbl"], info["borough"], info["house_number"],
         info["street_name"], info["zip_code"], project_code)
    )
    conn.commit()
    conn.close()
    return info


# =====================================================================
# Pulls
# =====================================================================

def refresh_permits(project_code, bin_, app_token):
    """Pull all permits for this BIN, last 5 years, replace cache."""
    if not bin_:
        return 0
    where = f"bin__='{bin_}'"
    try:
        data, status, dur = socrata_get(
            PERMITS_URL, where=where, order="filing_date DESC", limit=2000,
            app_token=app_token
        )
    except RuntimeError as e:
        log_pulse(project_code, "permits", bin_, 0, 0, 0, str(e))
        raise

    conn = db_conn()
    conn.execute("DELETE FROM dob_permits WHERE project_code = ?", (project_code,))

    for r in data:
        permit_id = (r.get("job_filing_number", "") + "-" + r.get("work_permit", ""))[:64] or r.get("work_permit") or r.get("job_filing_number")
        conn.execute(
            """INSERT INTO dob_permits
               (permit_id, project_code, bin, job_filing_number, work_permit,
                permit_type, permit_subtype, filing_status, issuance_date,
                expiration_date, filing_date, work_type, permittee_name,
                permittee_business_name, permittee_license_number, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(permit_id) DO UPDATE SET project_code=excluded.project_code, bin=excluded.bin, job_filing_number=excluded.job_filing_number, work_permit=excluded.work_permit, permit_type=excluded.permit_type, permit_subtype=excluded.permit_subtype, filing_status=excluded.filing_status, issuance_date=excluded.issuance_date, expiration_date=excluded.expiration_date, filing_date=excluded.filing_date, work_type=excluded.work_type, permittee_name=excluded.permittee_name, permittee_business_name=excluded.permittee_business_name, permittee_license_number=excluded.permittee_license_number, raw_json=excluded.raw_json""",
            (
                permit_id, project_code, bin_,
                r.get("job_filing_number"), r.get("work_permit"),
                r.get("permit_type"), r.get("permit_subtype"),
                r.get("filing_status"),
                (r.get("issued_date") or "")[:10] or None,
                (r.get("expired_date") or "")[:10] or None,
                (r.get("filing_date") or "")[:10] or None,
                r.get("work_type"),
                r.get("applicant_name") or r.get("permittee_first_name", "") + " " + r.get("permittee_last_name", ""),
                r.get("permittee_business_name") or r.get("applicant_business_name"),
                r.get("permittee_license_number") or r.get("applicant_license_number"),
                json.dumps(r),
            )
        )
    conn.commit()
    conn.close()
    log_pulse(project_code, "permits", bin_, len(data), status, dur)
    return len(data)


def refresh_violations(project_code, bin_, app_token):
    """Pull DOB + ECB violations for this BIN."""
    if not bin_:
        return 0
    total = 0

    # ----- DOB violations: pull, write in its own short-lived txn, then log -----
    try:
        data, status, dur = socrata_get(
            DOB_VIOLATIONS_URL, where=f"bin='{bin_}'", order="issue_date DESC",
            limit=2000, app_token=app_token
        )
    except RuntimeError as e:
        log_pulse(project_code, "dob_violations", bin_, 0, 0, 0, str(e))
        data, status, dur = [], 0, 0

    if data is not None:
        conn = db_conn()
        try:
            conn.execute(
                "DELETE FROM dob_violations WHERE project_code = ? AND source = 'DOB'",
                (project_code,)
            )
            for r in data:
                vid = "DOB-" + (r.get("isn_dob_bis_viol") or r.get("number") or "")
                conn.execute(
                    """INSERT INTO dob_violations
               (violation_id, project_code, bin, source, violation_number, violation_type,
                        violation_category, issue_date, status, description, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(violation_id) DO UPDATE SET project_code=excluded.project_code, bin=excluded.bin, source=excluded.source, violation_number=excluded.violation_number, violation_type=excluded.violation_type, violation_category=excluded.violation_category, issue_date=excluded.issue_date, status=excluded.status, description=excluded.description, raw_json=excluded.raw_json""",
                    (
                        vid, project_code, bin_, "DOB",
                        r.get("number"), r.get("violation_type"),
                        r.get("violation_category"),
                        (r.get("issue_date") or "")[:10] or None,
                        r.get("violation_type_code") or r.get("disposition_comments") or None,
                        r.get("description"),
                        json.dumps(r),
                    )
                )
            conn.commit()
        finally:
            conn.close()
    total += len(data)
    log_pulse(project_code, "dob_violations", bin_, len(data), status, dur)

    # ----- ECB violations: same pattern, brand new connection -----
    try:
        data2, status2, dur2 = socrata_get(
            ECB_VIOLATIONS_URL, where=f"bin='{bin_}'", order="issue_date DESC",
            limit=2000, app_token=app_token
        )
    except RuntimeError as e:
        log_pulse(project_code, "ecb_violations", bin_, 0, 0, 0, str(e))
        data2, status2, dur2 = [], 0, 0

    if data2 is not None:
        conn = db_conn()
        try:
            conn.execute(
                "DELETE FROM dob_violations WHERE project_code = ? AND source = 'ECB'",
                (project_code,)
            )
            for r in data2:
                vid = "ECB-" + (r.get("ecb_violation_number") or r.get("isn_dob_bis_extract") or "")
                conn.execute(
                    """INSERT INTO dob_violations
               (violation_id, project_code, bin, source, violation_number, violation_type,
                        violation_category, issue_date, hearing_date, status, description, penalty_imposed, penalty_paid, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(violation_id) DO UPDATE SET project_code=excluded.project_code, bin=excluded.bin, source=excluded.source, violation_number=excluded.violation_number, violation_type=excluded.violation_type, violation_category=excluded.violation_category, issue_date=excluded.issue_date, hearing_date=excluded.hearing_date, status=excluded.status, description=excluded.description, penalty_imposed=excluded.penalty_imposed, penalty_paid=excluded.penalty_paid, raw_json=excluded.raw_json""",
                    (
                        vid, project_code, bin_, "ECB",
                        r.get("ecb_violation_number"),
                        r.get("violation_type"),
                        r.get("infraction_code1") or r.get("severity"),
                        (r.get("issue_date") or "")[:10] or None,
                        (r.get("served_date") or "")[:10] or None,
                        r.get("ecb_violation_status") or r.get("hearing_status"),
                        r.get("violation_description") or r.get("infraction_code1_description"),
                        _to_float(r.get("penality_imposed") or r.get("penality")),
                        _to_float(r.get("total_violation_amount") or r.get("penality_amount_paid")),
                        json.dumps(r),
                    )
                )
            conn.commit()
        finally:
            conn.close()
    total += len(data2)
    log_pulse(project_code, "ecb_violations", bin_, len(data2), status2, dur2)

    return total


def refresh_complaints(project_code, bin_, app_token):
    """Pull complaints filed against this BIN."""
    if not bin_:
        return 0
    try:
        data, status, dur = socrata_get(
            COMPLAINTS_URL, where=f"bin='{bin_}'", order="date_entered DESC",
            limit=2000, app_token=app_token
        )
    except RuntimeError as e:
        log_pulse(project_code, "complaints", bin_, 0, 0, 0, str(e))
        raise

    conn = db_conn()
    conn.execute("DELETE FROM dob_complaints WHERE project_code = ?", (project_code,))

    for r in data:
        cid = r.get("complaint_number")
        if not cid:
            continue
        conn.execute(
            """INSERT INTO dob_complaints
               (complaint_id, project_code, bin, complaint_number, complaint_category,
                status, date_entered, disposition_date, disposition_code, inspection_date, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(complaint_id) DO UPDATE SET project_code=excluded.project_code, bin=excluded.bin, complaint_number=excluded.complaint_number, complaint_category=excluded.complaint_category, status=excluded.status, date_entered=excluded.date_entered, disposition_date=excluded.disposition_date, disposition_code=excluded.disposition_code, inspection_date=excluded.inspection_date, raw_json=excluded.raw_json""",
            (
                cid, project_code, bin_, cid,
                r.get("complaint_category"),
                r.get("status"),
                (r.get("date_entered") or "")[:10] or None,
                (r.get("disposition_date") or "")[:10] or None,
                r.get("disposition_code"),
                (r.get("inspection_date") or "")[:10] or None,
                json.dumps(r),
            )
        )
    conn.commit()
    conn.close()
    log_pulse(project_code, "complaints", bin_, len(data), status, dur)
    return len(data)


def _to_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


# =====================================================================
# Top-level driver
# =====================================================================

def refresh_project(project_code, lookup_first=True):
    """Resolve BIN if missing, then pull all three datasets."""
    token = get_app_token()
    conn = db_conn()
    proj = conn.execute(
        "SELECT project_code, name, address, bin FROM projects WHERE project_code = ?",
        (project_code,)
    ).fetchone()
    conn.close()
    if not proj:
        raise RuntimeError(f"Project '{project_code}' not found.")

    bin_ = proj["bin"]
    if not bin_ and lookup_first:
        info = update_project_bin(project_code)
        bin_ = info["bin"]

    if not bin_:
        raise RuntimeError(f"No BIN resolved for project '{project_code}'. Cannot pull DOB data.")

    print(f"\n=== {project_code}: {proj['name']} (BIN {bin_}) ===", flush=True)

    n_perm = refresh_permits(project_code, bin_, token)
    print(f"  Permits     : {n_perm}", flush=True)

    n_viol = refresh_violations(project_code, bin_, token)
    print(f"  Violations  : {n_viol}", flush=True)

    n_comp = refresh_complaints(project_code, bin_, token)
    print(f"  Complaints  : {n_comp}", flush=True)

    return {"permits": n_perm, "violations": n_viol, "complaints": n_comp, "bin": bin_}


def refresh_all():
    conn = db_conn()
    rows = conn.execute("SELECT project_code FROM projects ORDER BY project_code").fetchall()
    conn.close()
    results = {}
    for r in rows:
        try:
            results[r["project_code"]] = refresh_project(r["project_code"])
        except Exception as e:
            print(f"  ERROR on {r['project_code']}: {e}", flush=True)
            results[r["project_code"]] = {"error": str(e)}
    return results


def show_status():
    conn = db_conn()
    rows = conn.execute(
        """SELECT project_code, dataset, run_at, records_returned, status_code, error_message
           FROM dob_pulse_runs ORDER BY run_at DESC LIMIT 30"""
    ).fetchall()
    conn.close()
    print(f"\n{'When':<20}{'Project':<10}{'Dataset':<20}{'Records':>8}{'Status':>8}  Error")
    print("-" * 90)
    for r in rows:
        print(f"{r['run_at'][:19]:<20}{(r['project_code'] or '-'):<10}{(r['dataset'] or '-'):<20}"
              f"{r['records_returned'] or 0:>8}{r['status_code'] or 0:>8}  {(r['error_message'] or '')[:40]}")


# =====================================================================
# CLI
# =====================================================================

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    cmd = sys.argv[1]
    if cmd == "refresh-all":
        refresh_all()
    elif cmd == "refresh" and len(sys.argv) >= 3:
        refresh_project(sys.argv[2])
    elif cmd == "lookup-bin" and len(sys.argv) >= 3:
        info = update_project_bin(sys.argv[2])
        print(json.dumps(info, indent=2))
    elif cmd == "status":
        show_status()
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
