"""#284 — the ROLE x SECTION matrix + the effective-sections resolver.

The THIRD dial in the external-access model, sitting above the two that exist:

  1. ROLE -> sections   (this module)  what a role COULD EVER be shown. A section
                                       absent from a role's row here is never
                                       rendered and its API is 403 for that role,
                                       grant or no grant.
  2. GRANT -> sections  (#269)         what THIS user has been opened, default OFF.
  3. ITEM visibility    (#264)         which items inside a granted section.

EFFECTIVE = MATRIX[role] ∩ GRANTS(user). Both operands are default-deny, so the
intersection is too:

  * a role not seeded here (vendor, or any role added later) has an EMPTY row —
    the shell renders nothing and every portal payload endpoint refuses. A new
    external role is dark until somebody writes its row on purpose.
  * a grant row for a section outside the role's matrix row changes nothing —
    the matrix bounds what a grant is ABLE to open (belt over the #269 braces).
  * an architect has no grant machinery at all today (client_section_grant is
    client-validated on write), so their effective set resolves EMPTY and the
    portal shell is not their surface — they keep landing on /drawing-markup.
    Their matrix row exists so the day architect grants are built, the bound is
    already decided rather than improvised.

ORDER: effective_sections returns client_grants.SECTIONS order (the portal
display order) so every consumer renders sections in the same sequence.
"""
from __future__ import annotations

import re

import client_grants

# What a role could EVER be shown on the portal surface. Frozen on purpose:
# widening a row is a deliberate, reviewed edit here — never a runtime write.
ROLE_SECTION_MATRIX: dict[str, frozenset] = {
    "client": frozenset({
        "progress", "photos", "documents", "daily", "schedule", "drawing", "rfis",
        "proposals",   # #308
    }),
    "architect": frozenset({"drawing", "rfis", "documents"}),
    # vendor: DELIBERATELY ABSENT — an empty row until a vendor surface is designed.
}


def possible_sections(role) -> frozenset:
    """The matrix row for `role`. Empty for any role not seeded — fail-closed."""
    return ROLE_SECTION_MATRIX.get(role or "", frozenset())


def allowed(role, section) -> bool:
    """May `role` ever be shown `section`? (The grant still decides whether THIS
    user is. Both must say yes.)"""
    return section in possible_sections(role)


def effective_sections(conn, user_id, role, project_code) -> list:
    """MATRIX[role] ∩ GRANTS(user) in portal display order. Empty list = the
    portal shell has nothing to show this user (zero-grant client -> the #267
    welcome hard-stop; architect -> /drawing-markup; unknown role -> nothing)."""
    possible = possible_sections(role)
    if not possible or not project_code:
        return []
    granted = client_grants.granted_sections(conn, user_id, project_code)
    return [s for s in client_grants.SECTIONS if s in possible and s in granted]


# ============= nav-from-grants: the PORTAL_SECTION markup strip =============
# Same mechanism as access.render_sections (#262), different axis: SECTION strips by
# ROLE; PORTAL_SECTION strips by the EFFECTIVE per-user set computed above. The same
# marker wraps a section's nav item AND its view pane, so a non-effective section is
# fully ABSENT from the served DOM (never merely hidden) — for the portal shell that
# absence IS the "nav renders only effective sections" requirement, enforced at render.
_PORTAL_SECTION_RE = re.compile(
    r"<!--\s*PORTAL_SECTION:(\w+):start\s*-->.*?<!--\s*PORTAL_SECTION:\1:end\s*-->",
    re.DOTALL,
)
_PORTAL_MARKER_RE = re.compile(r"<!--\s*PORTAL_SECTION:\w+:(?:start|end)\s*-->")


def render_portal_sections(html: str, effective) -> str:
    """Return `html` with every PORTAL_SECTION block not in `effective` removed — and
    the marker comments of the KEPT blocks removed too: the served external page
    carries the sections, never the machinery that decided them."""
    keep = frozenset(effective)
    html = _PORTAL_SECTION_RE.sub(
        lambda m: m.group(0) if m.group(1) in keep else "", html)
    return _PORTAL_MARKER_RE.sub("", html)
