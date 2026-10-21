#!/usr/bin/env python3
"""The locked Superstars proposal layout (accepted prototype 2026-10-21): golden checks against the company's own proposals.

Proves, with no database and no model:
  * proposals 1041 (rope access inspections) and 1039 (north elevation repairs), rebuilt from their content, carry the
    company's fixed wording exactly: ESTIMATE, "Proposal Valid: 30 Days", SUBMITTED BY Arun Mal, the bold withdraw
    note first, the first exclusion, the acceptance block and the footer with page numbers
  * the sections come in the company's order, the bid lines show a bold lead, and the money is computed by code:
    38,200 -> deposit 19,100; 40,742 -> deposit 20,371; the payment and COI sentences are built from the content
  * the Ready-to-send checklist is clean on both, and every red rule fires on a broken copy (wrong item numbers in
    the unit-price note, word/digit mismatch, a unit outside the list, a placeholder, no address, no lines)
  * edit mode marks every editable text and leaves the fixed wording and the numbers unmarked

Run:  venv/bin/python tests/smoke_proposal_layout.py
"""
from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import proposal_checks as C  # noqa: E402
import proposal_layout as L  # noqa: E402

P1041 = {
    "heading": "ESTIMATE", "title": "21 West Street - Cycle 10 FISP Rope Access Inspections", "number": "1041", "date": "2026-10-21",
    "submitted_to": ["SUPERSTRUCTURES (SSX)", "Engineers + Architects", "On behalf of Building Ownership", "21 West Street, New York, NY 10006"],
    "project": ["21 West Street, New York, NY 10006", "31 stories, ±374 ft tall, ±75 ft wide x ±179.5 ft deep"],
    "reference": ["SUPERSTRUCTURES, Cycle 10 Local Law 11 / FISP", "Marked-up elevations, approximate drop locations"],
    "overview": "Superstars Contracting Inc. is pleased to submit this proposal to provide Industrial Rope Access (IRA) close-up "
                "inspections for the Cycle 10 Local Law 11 / FISP inspection at 21 West Street.",
    "lines": [{"lead": "Mobilization", "detail": "site safety plan, rope access plan and permits", "qty": 1, "unit": "LS", "unit_price": 3500},
              {"lead": "Rope access drops", "detail": "rigging and close-up physical inspection", "qty": 6, "unit": "EA", "unit_price": 4500},
              {"lead": "Flagman", "detail": "and ground control, sidewalk barricades", "qty": 8, "unit": "DAY", "unit_price": 650},
              {"lead": "Inspection report", "detail": "deficiencies marked on PDF, full photo log", "qty": 1, "unit": "LS", "unit_price": 2500}],
    "notes": ["Scope is limited to six (6) drops at the approximate locations marked on the SSX elevations. Items 2 and 3 are unit-priced "
              "and will be billed on actual drops and days performed. Additional drops will be priced at $4,500.00 per drop.",
              "Rope access is weather dependent and will not be performed in rain, high wind, or other unsafe conditions."],
    "deliverables": ["Close-up inspection of all areas indicated.", "Site visit prior to mobilization, at no charge."],
    "mobilization": "Upon receipt of signed proposal and deposit, subject to weather, permits, and building access.",
    "duration": "Approximately eight (8) working days for six (6) drops, including rigging moves, weather permitting.",
    "additional_insureds": ["the Owner", "SUPERSTRUCTURES"],
    "exclusions": ["Facade repairs of any kind.", "Sidewalk shed, scaffolding, or netting.", "After hours, night, weekend, or holiday work."],
    "accepted_by": "SUPERSTRUCTURES",
}
P1039 = {
    "heading": "ESTIMATE", "title": "160 Manhattan Avenue - North Elevation Facade Repairs", "number": "1039", "date": "2026-10-15",
    "submitted_to": ["Board President", "Manhattan Towers Condominium", "160 Manhattan Ave, Brooklyn, NY 11206"],
    "project": ["160 Manhattan Avenue, Brooklyn, NY 11206", "Left (North) Elevation, limited leak repair scope"],
    "reference": ["Mayas Engineering PLLC, Building Envelope Repairs", "DOB approved set, Sheet A-201.00 (red/blue scope)"],
    "overview": "Superstars Contracting Inc. is pleased to submit this proposal for the limited leak repair scope on the Left (North) Elevation.",
    "lines": [{"lead": "Mobilization", "detail": "general conditions, permits and documentation", "qty": 1, "unit": "LS", "unit_price": 2500},
              {"lead": "Protection", "detail": "of building, adjacent property and work areas", "qty": 1, "unit": "LS", "unit_price": 3000},
              {"lead": "Rigging and scaffolding", "detail": "incl. engineer access for close inspection", "qty": 3, "unit": "EA", "unit_price": 5000},
              {"lead": "CMU crack repair", "detail": "replace CMU if required", "qty": 50, "unit": "LF", "unit_price": 55},
              {"lead": "Remove vegetation", "detail": "", "qty": 126, "unit": "SF", "unit_price": 12},
              {"lead": "Chimney bracket penetrations", "detail": "seal and waterproof", "qty": 3, "unit": "EA", "unit_price": 75},
              {"lead": "Facade coating", "detail": "DOWSIL AllGuard per 1/A-301, incl. powerwashing", "qty": 1965, "unit": "SF", "unit_price": 7},
              {"lead": "Spray bar water test", "detail": "", "qty": 1, "unit": "EA", "unit_price": 2000}],
    "notes": ["Items 4, 5, 6, and 7 are unit-priced and will be billed on actual field-measured quantities at the rates above.",
              "Superstars provides a one (1) year warranty on workmanship and materials from the date of Substantial Completion."],
    "deliverables": [], "duration": "", "warranty": "One (1) year on workmanship and materials from Substantial Completion.",
    "additional_insureds": ["the Owner"],
    "exclusions": ["Work on other elevations or the roof.", "Sidewalk shed and hoist."],
    "accepted_by": "Manhattan Towers Condominium",
}


def _text(html: str) -> str:
    t = re.sub(r"(?is)<style.*?</style>", " ", html)
    t = re.sub(r"<[^>]+>", " ", t)
    import html as h
    return re.sub(r"\s+", " ", h.unescape(t)).strip()


def main() -> int:
    passed = 0
    H = L.HOUSE_DEFAULTS

    # ---- fixed wording, exactly as on their PDFs
    for d in (P1041, P1039):
        html = L.render(d)
        t = _text(html)
        for s in ("ESTIMATE", f"Proposal No. {d['number']}", "Proposal Valid: 30 Days", "Submitted by", "Arun Mal, Vice President",
                  "356 Wythe Ave, Suite 300, Brooklyn, NY 11249", "arun@superstarscontracting.com", H["withdraw_note"],
                  "Any work not specifically listed above.", H["acceptance"], "Authorized Signature (Arun Mal, Vice President)",
                  "SUPERSTARS CONTRACTING INC.", "Net 15 days", H["insurance"]):
            assert s in t, (d["number"], s)
        assert f"ACCEPTED BY {d['accepted_by'].upper()}" in t
        assert "counter(page)" in html and "counter(pages)" in html and H["footer"] in html, "footer with page numbers"
        assert "<li>" + H["withdraw_note"] in html.replace('<li class="new">', "<li>"), "withdraw note is the first bullet"
        order = [t.index(x) for x in ("Submitted to", "Reference", "Project Overview", "Base Bid", "BASE BID TOTAL", "Important Notes",
                                      "Schedule & Terms", "Exclusions", "Acceptance")]
        assert order == sorted(order), (d["number"], order)
    passed += 1; print("  fixed wording + order ok")

    # ---- money and computed sentences
    assert L.total(P1041) == 38200 and L.deposit(P1041, H) == 19100
    assert L.total(P1039) == 40742 and L.deposit(P1039, H) == 20371
    t41, t39 = _text(L.render(P1041)), _text(L.render(P1039))
    assert "BASE BID TOTAL $38,200.00" in t41 and "50% deposit ($19,100.00) required to schedule; balance upon completion. Net 15 days." in t41
    assert "BASE BID TOTAL $40,742.00" in t39 and "50% deposit ($20,371.00) required to schedule" in t39
    assert "COI naming the Owner and SUPERSTRUCTURES as Additional Insured issued prior to mobilization." in t41
    assert "COI naming the Owner as Additional Insured issued prior to mobilization." in t39
    h41 = L.render(P1041)
    assert "Flagman</span> and ground control" in h41 and "Mobilization</span>, site safety plan" in h41
    assert "<span class='lead'>Rope access drops</span>, rigging and close-up physical inspection" in L.render(P1041)
    assert "1,965" in t39 and "$13,755.00" in t39 and "Warranty One (1) year" in t39 and "Duration" not in t39
    passed += 1; print("  money + computed sentences ok")

    # ---- the checklist: clean on the real ones, red on broken copies
    for d in (P1041, P1039):
        res = C.check(d)
        assert not [r for r in res if r["level"] == "block"], (d["number"], res)
    def red(mut, needle):
        d = copy.deepcopy(P1041); mut(d)
        res = C.check(d)
        assert any(r["level"] == "block" and needle in r["text"] for r in res), (needle, res)
        assert C.blocked(d)
    red(lambda d: d["notes"].__setitem__(0, d["notes"][0].replace("Items 2 and 3", "Items 1 and 3")), "item 1 unit-priced")
    red(lambda d: d["notes"].__setitem__(0, d["notes"][0].replace("Items 2 and 3", "Items 2 and 9")), "item 9")
    red(lambda d: d.__setitem__("duration", d["duration"].replace("eight (8)", "eight (9)")), "“eight (9)” doesn't match")
    red(lambda d: d["lines"][2].__setitem__("unit", "days"), "unit that isn't")
    red(lambda d: d["exclusions"].append("Sidewalk shed TBD"), "placeholder")
    red(lambda d: d.__setitem__("submitted_to", ["SUPERSTRUCTURES (SSX)"]), "SUBMITTED TO")
    red(lambda d: d.__setitem__("lines", []), "no price lines")
    red(lambda d: d["lines"][1].__setitem__("unit_price", 0), "missing a description, quantity or rate")
    d = copy.deepcopy(P1041); d["notes"][0] = d["notes"][0].replace("$4,500.00 per drop", "$4,000.00 per drop")
    assert any(r["level"] == "warn" and "$4,000.00 per drop" in r["text"] for r in C.check(d))
    d = copy.deepcopy(P1041); d["accepted_by"] = "Someone Else LLC"
    assert any(r["level"] == "warn" and "ACCEPTED BY" in r["text"] for r in C.check(d))
    passed += 1; print("  checklist ok")

    # ---- edit mode: content marked, fixed wording and numbers not
    e = L.render(P1041, mode="edit", flagged=[P1041["exclusions"][0]])
    for needle in ('data-f="title"', 'data-f="submitted_to"', 'data-f="overview"', 'data-f="notes" data-i="0"', 'data-f="duration"',
                   'data-f="warranty"', 'data-f="accepted_by"', "data-lf='lead'", "data-lf='unit_price'", 'class="new" data-f="exclusions"'):
        assert needle in e, needle
    assert f'data-f="notes" data-i="0" contenteditable="true">{H["withdraw_note"]}' not in e, "the withdraw note is fixed"
    assert 'data-f="payment"' not in e and 'data-f="submitted_by"' not in e
    s = L.render(P1041, signed={"name": "Dana Client", "title": "PM", "at": "2026-10-25T10:00:00"})
    assert "Accepted by Dana Client, PM on October 25, 2026" in _text(s) and "Printed Name and Title" not in s
    passed += 1; print("  edit mode + signed ok")

    print(f"PASS: proposal layout ({passed} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
