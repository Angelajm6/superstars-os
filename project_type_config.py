"""project_type_config.py — Reports Phase 1 single source for the shared
spine (Handoff HANDOFF_REPORTS_PHASE1_SPINE.md).

Mirrors construction_builds_spec.json verbatim — `project_types.types[]`
plus the shared-field definitions and the location_reference shape.
Every future build (Weekly Summary, Two-Week Look-Ahead, RFI Log,
Phases 2-4) reads from HERE; do NOT duplicate any list into templates,
seeds, or per-build modules.

Exposed via:
  - PROJECT_TYPES                   (the 5 type dicts)
  - PROJECT_TYPE_IDS                (frozenset of valid ids)
  - STATUS_ENUM                     (the shared status superset)
  - LOCATION_REFERENCE_SCHEMA       (the persisted column shape)
  - SHARED_FIELD_DEFS               (scope_category, status, *_impact_flag)
  - get_project_type(type_id)       (one type dict by id)
  - get_project_config_for(conn, project_code)
                                    (resolves a project_code -> full config)

Updates: when the spec changes, edit this file and bump SPINE_VERSION.
The spec JSON in C:\\Users\\SSC-Admin\\Documents\\Claude\\Projects\\Superstars
Dashboard\\construction_builds_spec.json is the ultimate source of truth;
this module is its in-tree implementation mirror.
"""
from __future__ import annotations

SPINE_VERSION = "1.0.0"   # bump when project_types content or shared fields change

# ---------------------------------------------------------------------------
# Project types — verbatim from construction_builds_spec.json
# (`project_types.types`). DO NOT add fields here that are not in the spec
# without explicit operator sign-off; if extending, mark the new field
# 'optional' and document the why in the same handoff PR.
# ---------------------------------------------------------------------------
PROJECT_TYPES = [
    {
        "id": "facade",
        "label": "Exterior Restoration / Recladding",   # #339 — label only; the id stays "facade"
        "location_unit_options": ["Bay", "Drop", "Elevation", "Grid line", "Floor/Level"],
        "typical_scopes": [
            "Probe/investigation", "Demo", "Masonry repair", "Brick replacement",
            "Terra cotta repair/replacement", "Stone repair", "Lintel replacement",
            "Sealant/caulking", "Waterproofing/coating", "Pointing/repointing",
            "Crack repair",
        ],
        "typical_inspections": [
            "FISP/LL11 inspection", "Special inspection (TR-1)",
            "Engineer of Record site visit", "DOB inspection",
            "Sealant adhesion test",
        ],
        "typical_long_lead": [
            "Terra cotta units", "Cut stone", "Matching brick", "Steel lintels",
            "Swing stage / suspended scaffold", "Sidewalk shed materials",
        ],
    },
    {
        "id": "garage",
        "label": "Parking Structure / Garage Rehab (PSGI)",
        "location_unit_options": ["Level", "Bay", "Zone", "Ramp", "Grid line"],
        "typical_scopes": [
            "Concrete demo/removal", "Concrete repair/patching", "Rebar replacement",
            "Post-tension repair", "Traffic coating/membrane", "Joint sealant",
            "Drainage repair", "Structural strengthening",
        ],
        "typical_inspections": [
            "PSGI inspection", "Special inspection (TR-1)",
            "Concrete strength/cylinder break", "Engineer site visit",
            "DOB inspection",
        ],
        "typical_long_lead": [
            "Traffic coating materials", "Expansion joint systems",
            "Specialty repair mortars", "Rebar/steel",
        ],
    },
    {
        "id": "interiors",
        "label": "Interior Renovation / Fit-Out",
        "location_unit_options": ["Floor/Level", "Unit/Suite", "Room", "Zone", "Area"],
        "typical_scopes": [
            "Demo", "Framing", "MEP rough-in", "Drywall", "Flooring", "Painting",
            "Finishes", "Millwork install", "Fixtures",
        ],
        "typical_inspections": [
            "DOB inspection", "Special inspection (TR-1)", "MEP sign-off",
            "Fire/life-safety inspection", "Owner/architect walkthrough",
        ],
        "typical_long_lead": [
            "Millwork", "Specialty finishes", "MEP equipment", "Glazing/doors",
        ],
    },
    {
        "id": "ira",
        "label": "Investigation, Repair & Alteration (IRA)",
        "location_unit_options": ["Bay", "Drop", "Elevation", "Level", "Zone", "Grid line", "Area"],
        "typical_scopes": [
            "Investigation/probe", "Repair", "Alteration", "Localized demo",
            "Reconstruction",
        ],
        "typical_inspections": [
            "Special inspection (TR-1)", "Engineer site visit", "DOB inspection",
        ],
        "typical_long_lead": ["As determined by repair findings"],
    },
    {
        "id": "generic",
        "label": "Other / General",
        "location_unit_options": ["Location", "Area", "Zone", "Level", "Grid line"],
        "typical_scopes": ["User-defined"],
        "typical_inspections": ["User-defined"],
        "typical_long_lead": ["User-defined"],
    },
]

# Frozenset of valid type ids — used by the DB CHECK constraint mirror in
# schema_project_type.sql. Drift between this and the SQL CHECK constraint
# is a bug; both must list the same ids.
PROJECT_TYPE_IDS = frozenset(t["id"] for t in PROJECT_TYPES)

# ---------------------------------------------------------------------------
# Shared field definitions — defined ONCE here, consumed by all future
# Phase 2-4 builds. The shape mirrors `shared_fields.fields` from the spec.
# ---------------------------------------------------------------------------

# `status` shared enum — SUPERSET of the values needed across the three
# builds. The RFI Log subset (per spec) is {Open, Answered, Closed, Overdue,
# Void}; the Look-Ahead/Weekly use {Open, In Progress, Complete, Closed,
# Overdue, Void}. Phase 2-4 builds pick the subset they need; the column
# stays free TEXT so all values pass.
STATUS_ENUM = [
    "Open",
    "In Progress",
    "Complete",
    "Closed",
    "Overdue",
    "Void",
]

# RFI-specific subset (spec's rfi_log.status). Future Phase-2 RFI build
# normalizes to this; older values get mapped on read.
RFI_STATUS_SUBSET = ["Open", "Answered", "Closed", "Overdue", "Void"]

# location_reference — the spec's #1 shared field. Persisted as two TEXT
# columns on every table that records a located item (rfi_log already has
# them after this phase's migration; the Phase-2 lookahead_activities and
# lookahead_constraints tables will mirror the same shape).
LOCATION_REFERENCE_SCHEMA = {
    "columns": [
        {
            "name": "location_unit",
            "type": "TEXT",
            "nullable": True,
            "note": "Enum chosen from the project's project_type."
                    "location_unit_options. NULL when the row is not yet "
                    "tagged to a specific location.",
        },
        {
            "name": "location_id",
            "type": "TEXT",
            "nullable": True,
            "note": "Free text per the handoff (no 890 dropdown for Phase 1). "
                    "Examples: 'North Elevation Drop 3', 'Level 2 Bay 4'.",
        },
    ],
    "note": "Together (location_unit + location_id) form the spine that "
            "ties RFIs -> look-ahead constraints -> daily/weekly report "
            "items. A future DCR migration (DEFERRED per handoff — "
            "explicitly NOT this phase) will add the same two columns "
            "to work_log / safety_events / photos so the weekly can "
            "roll up by location.",
}

SHARED_FIELD_DEFS = {
    "scope_category": {
        "type": "TEXT",
        "options_source": "project_type.typical_scopes",
        "allow_custom": True,
        "note": "Scope-based (NOT trade-based) per spec.global_requirements."
                "no_trade_centricity. UI presents the project_type's "
                "typical_scopes as suggestions; operator can free-text any "
                "value the project demands.",
    },
    "status": {
        "type": "TEXT",
        "values": STATUS_ENUM,
        "rfi_subset": RFI_STATUS_SUBSET,
        "note": "See STATUS_ENUM above. RFI Log uses the RFI_STATUS_SUBSET; "
                "Look-Ahead and Weekly use the full set.",
    },
    "schedule_impact_flag": {
        "type": "INTEGER",
        "boolean": True,
        "default": 0,
        "note": "If True on an Open RFI, the spec.linkage_rules."
                "rfi_to_lookahead rule promotes that RFI to a constraint "
                "on its location_reference in the Two-Week Look-Ahead.",
    },
    "cost_impact_flag": {
        "type": "INTEGER",
        "boolean": True,
        "default": 0,
        "note": "Marks an item as carrying potential cost impact. Future "
                "Weekly Summary surfaces these in the Change Orders & "
                "RFIs section.",
    },
}

# ---------------------------------------------------------------------------
# Public lookup helpers
# ---------------------------------------------------------------------------

def get_project_type(type_id: str) -> dict | None:
    """Return the full type dict for `type_id`, or None if unknown."""
    if not type_id:
        return None
    for t in PROJECT_TYPES:
        if t["id"] == type_id:
            return t
    return None


def get_project_config_for(conn, project_code: str) -> dict | None:
    """Resolve a project_code to its full project-type config.

    Returns a dict matching the /api/projects/<code>/project-config response
    shape: {project_code, project_type, label, location_unit_options,
    typical_scopes, typical_inspections, typical_long_lead, shared_fields,
    location_reference}. Returns None when the project_code is unknown.
    """
    row = conn.execute(
        "SELECT project_code, project_type FROM projects WHERE project_code = ?",
        (project_code,),
    ).fetchone()
    if not row:
        return None
    # Support both sqlite3.Row and tuple
    try:
        ptype = row["project_type"]
        pcode = row["project_code"]
    except (TypeError, IndexError, KeyError):
        pcode, ptype = row[0], row[1]
    t = get_project_type(ptype) or get_project_type("generic")
    return {
        "project_code": pcode,
        "project_type": ptype,
        "label": t["label"],
        "location_unit_options": t["location_unit_options"],
        "typical_scopes": t["typical_scopes"],
        "typical_inspections": t["typical_inspections"],
        "typical_long_lead": t["typical_long_lead"],
        "shared_fields": SHARED_FIELD_DEFS,
        "location_reference": LOCATION_REFERENCE_SCHEMA,
        "spine_version": SPINE_VERSION,
    }
