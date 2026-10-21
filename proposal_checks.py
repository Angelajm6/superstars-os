"""The "Ready to send" checklist (prototype 2026-10-21).

Code, not Claude, checks every proposal before it can be sent: the math, the item numbers the notes point to, the
prices the notes repeat, the number words, the parties, and anything left unfinished. check(doc, house) returns
[{level: 'ok' | 'warn' | 'block', text}]; any 'block' keeps Send disabled.
"""
from __future__ import annotations

import re

import proposal_layout as L

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30}
_PLACEHOLDER = re.compile(r"\bTBD\b|\bXX+\b|\[[^\]]*\]|\?\?|lorem", re.I)


def _texts(doc: dict) -> list:
    out = [("Title", doc.get("title")), ("Overview", doc.get("overview")), ("Duration", doc.get("duration")),
           ("Warranty", doc.get("warranty"))]
    for k, label in (("submitted_to", "Submitted to"), ("project", "Project"), ("reference", "Reference"),
                     ("notes", "Important notes"), ("deliverables", "Deliverables"), ("exclusions", "Exclusions")):
        out += [(label, x) for x in doc.get(k) or []]
    out += [(f"Line {i}", f"{li.get('lead') or ''} {li.get('detail') or ''}") for i, li in enumerate(doc.get("lines") or [], 1)]
    return [(lbl, str(v)) for lbl, v in out if v]


def check(doc: dict, house: dict | None = None) -> list:
    house = dict(L.HOUSE_DEFAULTS, **(house or {}))
    res = []

    def add(level, text):
        res.append({"level": level, "text": text})

    lines = doc.get("lines") or []
    # ---- the price table
    if not lines:
        add("block", "There are no price lines.")
    bad = [i for i, li in enumerate(lines, 1)
           if not (li.get("lead") or li.get("detail")) or not li.get("qty") or float(li.get("qty") or 0) <= 0
           or not li.get("unit_price") or float(li.get("unit_price") or 0) <= 0]
    if bad:
        add("block", "Line " + ", ".join(map(str, bad)) + " is missing a description, quantity or rate.")
    units = [i for i, li in enumerate(lines, 1) if str(li.get("unit") or "").upper() not in L.UNITS]
    if units:
        add("block", "Line " + ", ".join(map(str, units)) + " has a unit that isn't one of " + ", ".join(L.UNITS) + ".")
    if lines and not bad:
        add("ok", f"Base bid adds up: {L.money(L.total(doc))}; deposit {L.money(L.deposit(doc, house))} "
                  f"({'%g' % float(house['deposit_pct'])}%).")

    notes = " ".join(doc.get("notes") or [])
    # ---- "Items 2 and 3 are unit-priced": those items exist and aren't lump sums
    m = re.search(r"Items?\s+([\d,\sand]+?)\s+(?:is|are)\s+unit[- ]priced", notes, re.I)
    if m:
        nums = [int(x) for x in re.findall(r"\d+", m.group(1))]
        wrong = [n for n in nums if n < 1 or n > len(lines) or str(lines[n - 1].get("unit") or "").upper() == "LS"]
        if wrong:
            add("block", "The notes call item " + ", ".join(map(str, wrong)) + " unit-priced, but it doesn't exist or is a lump sum.")
        else:
            add("ok", "Unit-priced items in the notes match the price table.")
    # ---- "$4,500.00 per drop": every price in the notes is a rate in the table (or a house rate)
    rates = {round(float(li.get("unit_price") or 0), 2) for li in lines}
    for amt in re.findall(r"\$([\d,]+(?:\.\d{2})?)\s+per\s+(\w+)", notes):
        v = round(float(amt[0].replace(",", "")), 2)
        if v not in rates and amt[1].lower() not in ("hour", "hr"):
            add("warn", f"The notes quote ${amt[0]} per {amt[1]}, which isn't a rate in the price table.")
    # ---- "six (6)" — the word and the digit agree
    for label, txt in _texts(doc):
        for w, d in re.findall(r"\b([a-z]+)\s+\((\d+)\)", txt, re.I):
            if w.lower() in WORDS and WORDS[w.lower()] != int(d):
                add("block", f"{label}: “{w} ({d})” doesn't match.")
    # ---- unfinished text
    for label, txt in _texts(doc):
        if _PLACEHOLDER.search(txt):
            add("block", f"{label} still has a placeholder: “{_PLACEHOLDER.search(txt).group(0)}”.")
    # ---- the parties
    to = [x for x in doc.get("submitted_to") or [] if x]
    if len(to) < 2:
        add("block", "SUBMITTED TO needs the name and the address.")
    acc = (doc.get("accepted_by") or "").strip()
    if not acc:
        add("block", "ACCEPTED BY is empty.")
    elif not any(acc.lower().split(" (")[0] in x.lower() for x in to):
        add("warn", f"ACCEPTED BY {acc.upper()} isn't named in SUBMITTED TO.")
    ins = doc.get("additional_insureds") or []
    if not any("owner" in x.lower() for x in ins):
        add("warn", "The COI doesn't name the Owner as Additional Insured.")
    if not doc.get("project"):
        add("block", "PROJECT is empty.")
    if not doc.get("reference"):
        add("warn", "REFERENCE is empty (the drawings, report or request this answers).")
    # ---- house wording Claude must not change
    if not str(doc.get("overview") or "").startswith(house["overview_opening"]):
        add("warn", f"The overview doesn't open with “{house['overview_opening']}…”.")
    if not doc.get("duration"):
        add("warn", "No duration in Schedule & Terms.")
    for k, label in (("notes", "Important notes"), ("exclusions", "Exclusions")):
        seen, dup = set(), []
        for x in doc.get(k) or []:
            key = re.sub(r"\W+", " ", x.lower()).strip()
            if key in seen:
                dup.append(x)
            seen.add(key)
        if dup:
            add("warn", f"{label} repeats: “{dup[0][:60]}”.")
    for x in doc.get("new_clauses") or []:
        add("warn", f"Not in your library yet: “{x[:80]}{'…' if len(x) > 80 else ''}”. Read it once.")
    if not any(r["level"] != "ok" for r in res):
        add("ok", "Everything else checks out.")
    return res


def blocked(doc: dict, house: dict | None = None) -> bool:
    return any(r["level"] == "block" for r in check(doc, house))
