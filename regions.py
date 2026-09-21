"""#331 — where a job is: the five boroughs plus the places the company also works.

One list, read by the estimate code series (`FR-NJ-001`), the "+ New project" and
"New estimate" pickers, and the job-site (building) borough label. Adding a region is a
line here — no schema change (estimate.borough is free text; building.borough stores the
label).
"""
from __future__ import annotations

# code -> label, in picker order. Codes are two letters because they live in estimate
# codes (`<TYPE>-<REGION>-<seq>`), which are printed on proposals and invoices.
LABELS = {
    "MN": "Manhattan",
    "BX": "Bronx",
    "BK": "Brooklyn",
    "QN": "Queens",
    "SI": "Staten Island",
    "LI": "Long Island",
    "NJ": "New Jersey",
}
CODES = tuple(LABELS)


def label(code) -> str:
    """The display name for a code; an unknown value comes back as given."""
    if not code:
        return ""
    c = str(code).strip()
    return LABELS.get(c.upper(), c)


def options() -> list:
    return [{"code": c, "label": l} for c, l in LABELS.items()]
