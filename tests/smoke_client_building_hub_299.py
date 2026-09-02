"""#299 — client -> building/job site presentation remains source-of-truth based.

Static guard: the C-suite CRM must show each linked operational project as a building /
job site, including the project's canonical address.  This deliberately tests the
contract rather than requiring seed data or a live database.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
crm = (ROOT / "crm.py").read_text()
ui = (ROOT / "company-dashboard.html").read_text()

assert "Buildings / job sites linked to this client organization" in crm
assert "project_code, name, address, city_zip, status, client_org_id" in crm
assert "Buildings &amp; job sites" in ui
assert "Address not yet entered" in ui
assert "Link a building / job site" in ui

print("PASS #299 client-building hub contract")
