"""#281 — the client field registry + client_payload(). The one place that decides what
an external audience is allowed to receive.

WHY THIS EXISTS, and why it is built before any portal route:
twelve sections exposed to outside parties, hand-curated one narrow SELECT at a time, is
the failure mode this whole design has been working against. The blast radius of getting
one of them wrong is an outside party reading an internal note. So the filtering does not
live in the routes — it lives here, once, and every mirrored route goes through it.

===========================================================================
PROVENANCE, NOT VOCABULARY  (the operator's amendment 2)
===========================================================================
The FIRST LINE is not "scan the output for bad words". It is: every value in a client
payload must trace to a field explicitly marked client-safe.

  * DEFAULT-DENY ALLOWLIST IS THE GATE. A field absent from DATASETS below is not
    emitted. A column added to a table next month is invisible here until somebody
    registers it — the new column is internal by default, which is the only posture that
    survives a schema that keeps growing.

  * THE INTERNAL-NAME PATTERNS ARE A SECOND ASSERTION OVER THE REGISTRY ITSELF, not a
    substitute for it. `*_reason`, `*_note`, `internal_*`, `*_uid`, owner/worker ids —
    if one of those is ever REGISTERED as client-safe, assert_registry_clean() fails
    loudly at import and in the gate. They guard the allowlist; they do not replace it.
    A name-pattern denylist alone would miss `hold_category`, `margin_pct`,
    `sla_breach_days`, `voided_by_uid` — none of which match a pattern, all of which are
    internal.

  * A FREE-TEXT FIELD CAN NEVER BE SAFELY STRING-MATCHED. "On hold · client budget" and
    "waiting on their funding call" are the same disclosure; no denylist catches both.
    Exclusion at the source is the only thing that works, which is what the allowlist is.

  * THE INTERNAL STATUS KEY NEVER SHIPS. status_tone.client_key is what goes out. Even
    where the two spell the same word today, the payload is built from client_key so a
    module whose internal keys are not client-safe inherits a working mechanism instead
    of needing one invented.

The forbidden-string scan still exists — in the leak test, as the SECOND line. It catches
the one thing provenance cannot see: an internal value hand-copied into a registered-safe
field by a person.
"""
from __future__ import annotations

import re

# ===========================================================================
# THE REGISTRY. dataset -> the exact fields an external audience may receive.
# Adding a field here is the ONLY way it reaches a client or an architect.
# ===========================================================================

DATASETS: dict[str, frozenset] = {
    # ---- section A · Project Health -------------------------------------
    # Overall progress. A percentage and a human label; no cost, no drop internals.
    "health.progress": frozenset({"pct", "label"}),

    # Active drops — PROGRESS ONLY. Deliberately absent: note, internal_note, any
    # attribution (who marked it), lifecycle, and every date other than a bare
    # target. An outside party gets "where is the work", not "who did what when".
    # #285 — `step` joined the row: the current stage-template step, a GENERATED
    # string from structured parts only ("Step {no} · {template step name}").
    "health.active_drop": frozenset({"drop_id", "label", "elevation", "pct", "status",
                                     "step"}),

    # Drops by status — COUNTS ONLY. status here is a client_key, never an internal key.
    "health.status_count": frozenset({"status", "label", "tone", "count"}),

    # Progress by elevation — PERCENTAGES ONLY.
    "health.elevation_progress": frozenset({"elevation", "pct"}),

    # Latest daily report — a curated SUMMARY. No internal notes, no labour, no
    # worker identities, no report internals.
    "health.daily_summary": frozenset({"date", "work_performed", "label", "summary"}),

    # Weather — public data. Registered anyway so nothing bypasses the serialiser.
    "health.weather": frozenset({"date", "temp_f", "condition", "icon",
                                 "precip_pct", "wind_mph"}),

    # ---- #308 · proposals (the #307 rows, the CLIENT projection) ----------
    # What the client signed or is asked to sign. status is a CLIENT key
    # (open | signed | declined | expired), never the internal lifecycle word;
    # building is the address line; URLs point at the grant-gated portal routes.
    # ABSENT by provenance: decline_reason, signed_ip, recipient email, the
    # estimate code/status, deposit terms beyond the two numbers shown on the
    # document itself, any internal note.
    "portal.proposal": frozenset({"id", "number", "version", "title", "status", "status_label",
                                  "price", "deposit_amount", "deposit_pct", "valid_until",
                                  "sent_at", "signed_at", "signed_name", "declined_at",
                                  "building", "view_url", "pdf_url", "can_accept"}),

    # ---- project identity shown in the shell header ----------------------
    "project.identity": frozenset({"project_code", "name", "client_name"}),

    # ---- #284 · the four portal-shell section payloads -------------------
    # The first PRODUCTION consumers of this registry. Every field below is an
    # operator-approved disclosure; a field a section needs next month gets
    # REGISTERED next month — never slipped into a SELECT.

    # progress — the Classic progress data re-served through the registry.
    # summary.text is code-GENERATED from pct (never operator free text).
    # #286 — latest_report: the newest issued report DATE (KPI tile fuel).
    "portal.progress_summary": frozenset({"text", "last_activity", "photos_shared",
                                          "latest_report"}),

    # photos — item-shared only (visibility.py is the source; this is the shape).
    # #285 — caption is GONE (operator decision: cards show drop · elevation ·
    # date ONLY; no free text of any kind on the external gallery). drop_label
    # is generated ("DP-{n}" / "Unassigned"); elevation comes from the drops
    # row. URLs point at the id-gated byte routes.
    "portal.photo": frozenset({"id", "drop_label", "elevation", "taken_at",
                               "thumb_url", "file_url"}),
    # #285 — the photos stat strip (mirror of the internal Field Photos tiles).
    "portal.photos_stats": frozenset({"shared_count", "drops_covered", "latest_date"}),

    # documents — item-shared only, same engine. notes / file_name / uploader /
    # requirement_key stay internal exactly as Classic decided in #269.
    "portal.document": frozenset({"id", "title", "category", "doc_type",
                                  "effective_date", "file_url"}),

    # daily — the CLIENT DCR BREAKDOWN (#284 operator-approved allowlist):
    # date + work/no-work; weather; active drop/elevation labels; structured
    # activity categories; that-day status changes; that-day shared photos.
    # EXPLICITLY ABSENT, forever, by provenance: worker identities, hours,
    # rates, headcounts, SOV quantities, internal notes, and EVERY free-text
    # column (no_work_reason, no_work_note, scope_of_work, description,
    # stage-note, cell reason) — none of them is ever SELECTED, let alone
    # registered. `label` and `status` carry generated/enum vocabulary only.
    # #286 — report_id (the GENERATED display id "DCR-<code>-NNN") + seq joined the
    # day row: the portal daily table mirrors the internal archive's first column,
    # and seq addresses the gated client-audience render route. Operator-approved.
    "portal.daily_day": frozenset({"date", "no_work", "label", "report_id", "seq"}),
    "portal.daily_weather": frozenset({"am_temp_f", "pm_temp_f", "am_conditions",
                                       "pm_conditions", "wind"}),
    "portal.daily_drop": frozenset({"label", "elevation"}),
    "portal.daily_activity": frozenset({"category", "status"}),
    "portal.daily_change": frozenset({"drop_label", "level", "from_label", "to_label"}),
    "portal.daily_photo": frozenset({"id", "thumb_url", "file_url"}),

    # ---- #286 · the look-ahead board (anatomy parity with the internal page) --
    # PROVENANCE EXCEPTION (planner-approved, flagged in HANDOFF): activity NAME
    # crosses to the client — look-ahead titles are planning labels designed for
    # external consumption (the internal Print exists to share this board).
    # Delivery names are REPLACED with the generic "Delivery" before projection;
    # crew / notes / source / constraint_count never enter the payload.
    "la.day": frozenset({"iso", "dow", "md", "is_today"}),
    "la.kpis": frozenset({"active_drops", "activities", "deliveries", "behind"}),
    "la.group": frozenset({"label", "status", "stage_index", "stage_total",
                           "behind_days"}),
    "la.activity": frozenset({"name", "activity_type", "grid"}),
}

# Fields matching these are internal BY NATURE. They must never appear in DATASETS.
# This list guards the registry — it is not the gate.
_INTERNAL_PATTERNS = (
    re.compile(r"(^|_)internal($|_)"),
    re.compile(r"_reason$"),
    re.compile(r"_note$"),
    re.compile(r"^note$"),
    re.compile(r"_uid$"),
    re.compile(r"(^|_)worker_id($|_)"),
    re.compile(r"(^|_)employee_id($|_)"),
    re.compile(r"(^|_)rate($|_)"),
    re.compile(r"(^|_)cost($|_)"),
    re.compile(r"(^|_)margin($|_)"),
    re.compile(r"(^|_)pay($|_)"),
    re.compile(r"(^|_)sla($|_)"),
    re.compile(r"est_stage"),
    re.compile(r"_path$"),
)


class RegistryError(RuntimeError):
    """Raised when the registry itself is unsafe — at import, not at request time."""


def assert_registry_clean() -> None:
    """Fail loudly if a field that is internal BY NATURE has been registered client-safe.

    Runs at import so a bad registration cannot reach a request, and again in the gate so
    it cannot reach a commit."""
    bad = []
    for dataset, fields in DATASETS.items():
        for f in fields:
            for pat in _INTERNAL_PATTERNS:
                if pat.search(f):
                    bad.append(f"{dataset}.{f} matches /{pat.pattern}/")
    if bad:
        raise RegistryError(
            "internal-by-nature field(s) registered as client-safe: " + "; ".join(bad))


assert_registry_clean()


# ===========================================================================
# THE SERIALISER. The only sanctioned way a portal response is built.
# ===========================================================================

INTERNAL_AUDIENCES = frozenset({"internal"})


def client_payload(dataset: str, data, audience: str = "client"):
    """Project `data` (a mapping, or an iterable of mappings) onto `dataset`'s registered
    fields. Returns the same shape it was given.

    An INTERNAL audience passes through untouched — the registry exists to constrain what
    leaves the building, not to hide anything from the people who work here.

    An unknown dataset RAISES rather than passing data through. A typo'd dataset name
    must not silently become "emit everything"; that would turn the one safe default into
    the one dangerous one."""
    if audience in INTERNAL_AUDIENCES:
        return data
    if dataset not in DATASETS:
        raise RegistryError(
            f"unknown dataset {dataset!r} — register it in client_registry.DATASETS "
            f"before serving it to an external audience")
    allowed = DATASETS[dataset]

    def one(row):
        if row is None:
            return None
        src = dict(row) if not isinstance(row, dict) else row
        return {k: src[k] for k in allowed if k in src}

    if isinstance(data, dict):
        return one(data)
    if data is None:
        return None
    return [one(r) for r in data]


def registered_fields(dataset: str) -> frozenset:
    return DATASETS.get(dataset, frozenset())


def is_external(role) -> bool:
    """External audiences are everything that is not the internal tier. DEFAULT-DENY: a
    role added later is external until someone says otherwise."""
    return role not in ("admin", "c_suite", "pm", "super", "estimator")


def audience_for(role) -> str:
    return "client" if is_external(role) else "internal"
