"""#262 — central role-based section access (RBAC). ONE source of truth.

Both the dashboard sidebar (rendered server-side — gated SECTION blocks are stripped
from the served HTML per role) AND the gated API endpoints read `SECTION_ACCESS`, so
access for a section changes in exactly one place. Role-based only — no per-user perms.

Sections NOT listed here default to ALL dashboard roles, so `pm` keeps every
operational section (Compliance, Drop Plan, Field Photos, Look-ahead, ...). Only the
gated sections restrict: **Financial** (Schedule of Values + Labor Rate Tracker, which
carry contract/billing financials) → admin/c_suite only. `super` gets the default
(everything except the gated Financial section).
"""
from __future__ import annotations

import re

DASHBOARD_ROLES = frozenset({"admin", "c_suite", "pm", "super"})

# Gated sections only. The KEY matches the `<!-- SECTION:<key> -->` markers in the
# templates AND the section passed to requires_section() on the endpoints.
SECTION_ACCESS = {
    "financial": frozenset({"admin", "c_suite"}),
    # #266 — CRM/ops core is a C-suite function: organizations, contacts, activity log,
    # follow-up tasks + the "Needs Attention" feed. admin/c_suite ONLY; pm/super (and any
    # external role) get 403 on every CRM endpoint and the CRM tab is absent from their nav.
    # function-tags on the data are recorded for future role-slicing — NO new roles here.
    "crm": frozenset({"admin", "c_suite"}),
    # #273/#274 — Estimates/bids + the IRA inspection pipeline + inspection calendar live
    # in ONE company-console section, CRM-class gating: admin/c_suite ONLY. Amounts and
    # payment status never touch a field-reachable surface — pm/super/external get 403 on
    # every /api/estimates/* and /api/ira/* endpoint, and the console page itself already
    # 403s non-company roles (#263), so the section is fully absent for them.
    "estimates": frozenset({"admin", "c_suite"}),
    # #276 — the ESTIMATING QUEUE surfaces (blueprint §5): /estimating + the queue/stage
    # endpoints + the lead's own detail/docs. The `estimator` role works the queue —
    # including entering the proposal amount on leads they work — but the console
    # Estimates section above (board, VP table, CRM links, rollup $) stays admin/c_suite.
    # pm/super/client get 403 on ALL estimating surfaces.
    "estimating": frozenset({"admin", "c_suite", "estimator"}),
    # #281 — workforce figures on the Project Health page: the "Workers on site" and
    # "Certs expiring <=30d" KPI tiles and the "Today on site · roster" widget. Internal
    # tier only. They are on Amit's never-visible list for client/architect, and unlisted
    # would NOT have been enough: an unlisted key defaults to DASHBOARD_ROLES, which is
    # already internal-only — but naming it makes the intent explicit and lets the marker
    # strip the markup rather than relying on the widget's loader failing quietly.
    "workforce_kpi": frozenset({"admin", "c_suite", "pm", "super"}),
    # #281 — the internal-ops sections on Amit's never-visible list: Employees & Certs,
    # Subcontractors & COIs, Site Closure, Equipment Log, Toolbox Talks. One key because
    # they share one rule (internal tier, never an outside party), which keeps the strip
    # legible; split it the day one of them needs a different answer.
    "internal_ops": frozenset({"admin", "c_suite", "pm", "super"}),
    # #302 — the Company Portal (/company + /api/company/portal/*): the C-suite's
    # whole-company view — clients & buildings, finance (pipeline, spend, receivables,
    # labor cost), workforce with pay rates, and every expiring license/insurance/cert.
    # Comp-class data lives here, so COMPANY axis only: admin/c_suite. Everyone else 403.
    "company_portal": frozenset({"admin", "c_suite"}),
}

# #263 — COMPANY axis. The company overview console (`/`) and its company-level tabs
# (Workforce, Cert Health/Library, Bid Tracker, Specs, Settings) + their data endpoints
# are admin/c_suite ONLY. A `pm` is scoped to assigned PROJECTS instead (see pm_scoping)
# and lands on a projects-only view; it never reaches the company console. This is the
# ROLE axis for "company vs project" — independent of the project-ASSIGNMENT axis.
COMPANY_ROLES = frozenset({"admin", "c_suite"})


def can_access_company(role) -> bool:
    """True iff `role` may reach the company overview console + company-level surfaces.
    The single source of truth for both the `/` gate, the @requires_company endpoints,
    and the role-aware back-link in the project sidebar."""
    return role in COMPANY_ROLES


def roles_for(section: str) -> frozenset:
    """The set of roles allowed in `section` (defaults to all dashboard roles)."""
    return SECTION_ACCESS.get(section, DASHBOARD_ROLES)


def can_access(section: str, role) -> bool:
    return role in roles_for(section)


def can_access_all_gated(role) -> bool:
    """True iff `role` can access EVERY gated section (admin/c_suite). The fast path:
    no per-role sidebar stripping needed when this is True."""
    return all(role in roles for roles in SECTION_ACCESS.values())


def section_visibility(role) -> dict:
    """{gated_section: bool} for the current role — surfaced on /api/auth/me so the
    client can reflect the same decision (the sidebar itself is server-stripped)."""
    return {s: (role in roles_for(s)) for s in SECTION_ACCESS}


# <!-- SECTION:<name>:start --> ... <!-- SECTION:<name>:end --> — server-stripped from
# the served HTML for any role that can't access <name>. The \1 backreference pins each
# end to its own start; DOTALL so a block spans many lines. The SAME marker wraps both a
# sidebar group AND its content pane, so a gated section is fully ABSENT (not merely
# hidden) for a disallowed role — hiding a menu item is not access control.
_SECTION_RE = re.compile(
    r"<!--\s*SECTION:(\w+):start\s*-->.*?<!--\s*SECTION:\1:end\s*-->",
    re.DOTALL,
)


def render_sections(html: str, role) -> str:
    """Return `html` with every SECTION block the role can't access removed."""
    return _SECTION_RE.sub(
        lambda m: m.group(0) if can_access(m.group(1), role) else "", html)


# ===================== #263 — role-aware project-sidebar nav =====================
# Named-block markers, same idea as SECTION but with an explicit keep/strip predicate
# (SECTION only ever STRIPS what a role can't see; these pick BETWEEN two variants).
#
#   <!-- BACKLINK_COMPANY:start --> ... <!-- BACKLINK_COMPANY:end -->   (admin/c_suite)
#   <!-- BACKLINK_PROJECTS:start --> ... <!-- BACKLINK_PROJECTS:end --> (pm/super)
#   <!-- COMPANYLINK:start --> ... <!-- COMPANYLINK:end -->            (in-view deep links)
#
# The project dashboard's "← Back to …" link and its in-view "Company Console →" deep
# links must NEVER hand a pm a path to the company console (which 403s for them). So the
# served HTML is rendered per role: keep the company back-link for company roles, the
# projects back-link otherwise, and strip every COMPANYLINK block for non-company roles.

def _named_block_re(name: str):
    return re.compile(
        r"<!--\s*" + re.escape(name) + r":start\s*-->.*?<!--\s*" + re.escape(name) + r":end\s*-->",
        re.DOTALL,
    )


def _strip_named_block(html: str, name: str, keep: bool) -> str:
    """Remove EVERY `name` block if `keep` is False; leave the html untouched if True."""
    if keep:
        return html
    return _named_block_re(name).sub("", html)


def render_role_nav(html: str, role) -> str:
    """Pick the role-correct project-sidebar nav: the company back-link for company roles
    or the projects back-link otherwise, and drop in-view company deep links for non-company
    roles. Idempotent and order-independent; a no-op if the markers are absent."""
    company = can_access_company(role)
    html = _strip_named_block(html, "BACKLINK_COMPANY", keep=company)
    html = _strip_named_block(html, "BACKLINK_PROJECTS", keep=not company)
    html = _strip_named_block(html, "COMPANYLINK", keep=company)
    return html


# ===================== #281 — project identity, filled per request =====================
# The shell used to carry "890 E 135th St · FR-BX-001" and the client's org name HARD-CODED
# in three headers. That bound one file to one job and one client: a second project, or a
# second client, would have needed a second file. It is now filled here, from the
# project_code in the URL, on every request.
#
# SERVER-SIDE rather than a fetch, deliberately: the page already renders per role through
# this pipeline, so there is no new endpoint to gate (and every /projects/<code> API is
# behind the #263 hook, which external roles do not currently pass); the header is correct
# in the first byte, with no empty flash; and it cannot be wrong for a role whose JS never
# ran. Escaped, because a project name is operator-entered text landing in markup.

def _esc(s) -> str:
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# Text placeholders: <tag data-project-X>…</tag> gets its TEXT replaced.
# Attribute placeholder: the literal __PROJECT_CODE__ token, for places where the value
# has to live in an attribute (a <option value="…">) rather than in element text.
_PROJECT_CODE_TOKEN = "__PROJECT_CODE__"
_API_BASE_TOKEN = "__API_BASE__"

# #281 — the shell is ONE file served from two namespaces. Which namespace a session
# fetches from is decided HERE, server-side, and injected as a single variable:
#
#   internal roles  ->  /api/projects/<code>   the internal project namespace
#   external roles  ->  /api/portal/<code>     the CURATED portal namespace
#
# This is what lets client and architect have the same shell, same layout and same visual
# language WITHOUT opening #264's routing-layer boundary: an external session simply never
# forms an internal URL. pm_scoping.pm_can_access_project still returns False for every
# external role, and that stays the enforcement — this only decides what the page asks for.
EXTERNAL_API_ROLES = frozenset({"client", "architect", "vendor"})


def api_base_for(role, project_code) -> str:
    code = str(project_code or "")
    ns = "/api/portal/" if role in EXTERNAL_API_ROLES else "/api/projects/"
    return ns + code


def render_project_identity(html: str, project) -> str:
    """Fill every data-project-* placeholder and __PROJECT_CODE__ token from `project`
    (a mapping with project_code / name / client_name, any of them optional).

    A no-op when the project is unknown: placeholders stay blank rather than showing a
    stale or guessed name, and the token resolves to empty rather than to another job's
    code. Blank is the honest failure here — a wrong project on a header is worse than
    no project."""
    if not project:
        return html.replace(_PROJECT_CODE_TOKEN, "")
    code = _esc(project.get("project_code"))
    name = _esc(project.get("name"))
    client = _esc(project.get("client_name"))
    fills = {
        "data-project-title":  " · ".join(x for x in (name, code) if x),
        "data-project-client": client,
        "data-project-line":   " · ".join(x for x in (code, name) if x),
        "data-project-code":   code,
        "data-project-name":   name,
    }
    for attr, value in fills.items():
        # <tag ... attr ...>ANYTHING</tag>  ->  same tag with `value` as its text.
        # The \b before the attribute name keeps data-project-code from matching inside
        # data-project-client (they share a prefix); the optional ="…" lets a placeholder
        # carry a value attribute as well as being a fill target.
        html = re.sub(
            r"(<(\w+)([^>]*\b" + re.escape(attr) + r")((?:=\"[^\"]*\")?[^>]*)>)(.*?)(</\2>)",
            lambda m, v=value: m.group(1) + v + m.group(6),
            html, flags=re.DOTALL)
    return html.replace(_PROJECT_CODE_TOKEN, code)


def render_api_base(html: str, role, project_code) -> str:
    """Fill __API_BASE__ with the namespace this role is allowed to fetch from."""
    return html.replace(_API_BASE_TOKEN, _esc(api_base_for(role, project_code)))
