"""Project-document TYPES — the tag Claude puts on every dropped document.

One list, one home: the drop-zone intake (doc_intake routes in server.py), the
scanner prompt (document_scanner.build_prompt), and the Documents tab tag picker
(GET /api/documents/types) all read it. Each type files into exactly one of the
six project_documents categories and, when it satisfies a required checklist
item (document_requirements), names that requirement_key so the readiness
rollup keeps working unchanged.

Pure data + lookups — no DB, no API.
"""
from __future__ import annotations

# (key, label, category, requirement_key or None) — display order within a category.
TYPES = (
    # Permits & filings
    ("pw2", "DOB Work Permit (PW2)", "PERMITS", "pw2"),
    ("cd5", "DOB NOW CD-5 Filing", "PERMITS", None),
    ("sidewalk_shed", "Sidewalk-Shed Permit", "PERMITS", "sidewalk_shed"),
    ("best_permit", "Suspended-Scaffold (BEST) Permit", "PERMITS", "best_permit"),
    ("fisp_ack", "FISP Filing Acknowledgment", "PERMITS", "fisp_ack"),
    ("after_hours", "After-Hours / Street-Occupancy Variance", "PERMITS", "after_hours"),
    ("dot_permit", "DOT Street / Sidewalk Permit", "PERMITS", None),
    ("permit_other", "Other Permit or Filing", "PERMITS", None),
    # Drawings & submittals
    ("approved_arch", "DOB-Approved Architectural Set", "DRAWINGS", "approved_arch"),
    ("approved_struct", "DOB-Approved Structural Set", "DRAWINGS", "approved_struct"),
    ("scaffold_eng", "Scaffold & Rigging Engineering", "DRAWINGS", "scaffold_eng"),
    ("drop_layout", "Drop / Rigging Layout", "DRAWINGS", "drop_layout"),
    ("shop_drawings", "Shop Drawing / Submittal", "DRAWINGS", "shop_drawings"),
    ("product_data", "Product Data / Spec Sheet", "DRAWINGS", None),
    ("drawing_other", "Other Drawing", "DRAWINGS", None),
    # Contracts, insurance & money
    ("owner_contract", "Owner Contract (AIA A101 / A401)", "CONTRACTS", "owner_contract"),
    ("sov", "Schedule of Values", "CONTRACTS", "sov"),
    ("coi", "Certificate of Insurance (COI)", "CONTRACTS", "coi"),
    ("bond", "Payment / Performance Bond", "CONTRACTS", "bond"),
    ("aia_g702", "AIA G702/G703 Pay Application", "CONTRACTS", None),
    ("change_order", "Change Order", "CONTRACTS", None),
    ("subcontract", "Subcontract Agreement", "CONTRACTS", None),
    ("proposal", "Proposal / Estimate", "CONTRACTS", None),
    ("invoice", "Invoice", "CONTRACTS", None),
    ("contract_other", "Other Contract Document", "CONTRACTS", None),
    # Inspections & reports
    ("fisp_report", "FISP / LL11 Inspection Report", "INSPECTIONS", "fisp_report"),
    ("probe_report", "Probe / Condition Report", "INSPECTIONS", "probe_report"),
    ("tr1", "TR1 Special Inspection", "INSPECTIONS", "tr1"),
    ("dob_signoff", "DOB Sign-Off", "INSPECTIONS", "dob_signoff"),
    ("violation", "DOB / ECB Violation", "INSPECTIONS", None),
    ("inspection_other", "Other Inspection or Report", "INSPECTIONS", None),
    # Safety
    ("site_safety_plan", "Site Safety Plan", "SAFETY", "site_safety_plan"),
    ("sst", "SST Card / Record", "SAFETY", "sst_records"),
    ("rope_access_plan", "Rope Access Plan", "SAFETY", "rope_access_plan"),
    ("sds", "Safety Data Sheet (SDS)", "SAFETY", "sds"),
    ("toolbox_log", "Toolbox-Talk Sign-In", "SAFETY", "toolbox_log"),
    ("safety_other", "Other Safety Document", "SAFETY", None),
    # Closeout
    ("completion_letter", "Letter of Completion / Sign-Off", "CLOSEOUT", "completion_letter"),
    ("warranty", "Warranty", "CLOSEOUT", "warranties"),
    ("final_fisp", "Final FISP Filing", "CLOSEOUT", "final_fisp"),
    ("lien_waiver", "Lien Waiver", "CLOSEOUT", "lien_waivers"),
    ("closeout_other", "Other Closeout Document", "CLOSEOUT", None),
)

BY_KEY = {t[0]: {"key": t[0], "label": t[1], "category": t[2], "requirement_key": t[3]} for t in TYPES}
KEYS = tuple(BY_KEY)

# The catch-all per category, used when only the category is known.
FALLBACK_BY_CATEGORY = {
    "PERMITS": "permit_other", "DRAWINGS": "drawing_other", "CONTRACTS": "contract_other",
    "INSPECTIONS": "inspection_other", "SAFETY": "safety_other", "CLOSEOUT": "closeout_other",
}


def get(key):
    """The type dict for a key, or None."""
    return BY_KEY.get((key or "").strip())


def for_requirement(category, requirement_key):
    """The type that fulfils a checklist item (None when no type maps to it)."""
    for t in BY_KEY.values():
        if t["category"] == category and t["requirement_key"] == requirement_key and requirement_key:
            return t
    return None


def public_list():
    """JSON-ready list in display order (for the tag picker)."""
    return [dict(BY_KEY[k]) for k in KEYS]
