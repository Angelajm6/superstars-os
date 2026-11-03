"""The Superstars proposal layout, built in code (prototype 2026-10-21).

Every Superstars proposal is the same document (proposals 1034, 1039, 1040, 1041): the header with ESTIMATE /
PROPOSAL, the SUBMITTED TO / SUBMITTED BY / PROJECT / REFERENCE grid, Project Overview, the Base Bid table,
Important Notes, Deliverables, Schedule & Terms, Exclusions and Acceptance. This module draws that document from
structured content, so nothing that is the same on every proposal is ever written by Claude:

  house  the company's fixed wording (HOUSE_DEFAULTS, editable once in Settings): validity, SUBMITTED BY, the
         withdraw note, mobilization, hours, deposit and net days, insurance, acceptance, footer
  doc    one proposal's own content: heading, title, number, date, submitted_to, project, reference, overview,
         lines [{lead, detail, qty, unit, unit_price}], notes, deliverables, duration, warranty,
         additional_insureds, exclusions, accepted_by

Totals, the deposit, the payment sentence, the COI sentence and the page footer are computed here.
render(doc, house, mode='pdf' | 'edit').
"""
from __future__ import annotations

import base64
import html as _html
from datetime import date
from pathlib import Path
from typing import Optional

FONT = Path(__file__).resolve().parent / "static" / "fonts" / "Inter-latin.woff2"
RED = "#8B1A1A"
UNITS = ("LS", "EA", "DAY", "HR", "SF", "LF", "SQ", "CY", "LOT", "WK")
HEADINGS = ("ESTIMATE", "PROPOSAL")

HOUSE_DEFAULTS = {
    "company_legal": "Superstars Contracting Inc.",
    "valid_text": "30 Days",
    "submitted_by": ["Arun Mal, Vice President", "Superstars Contracting Inc.", "356 Wythe Ave, Suite 300, Brooklyn, NY 11249",
                     "917.498.0002", "arun@superstarscontracting.com"],
    "signer": "Arun Mal, Vice President",
    "overview_opening": "Superstars Contracting Inc. is pleased to submit this proposal",
    "withdraw_note": "Superstars Contracting Inc. reserves the right to withdraw this proposal at any time prior to a signed contract.",
    "mobilization": "Upon receipt of signed proposal and deposit, subject to weather and building access.",
    "hours": "9 AM to 5 PM, Monday to Friday, excluding legal holidays.",
    "deposit_pct": 50,
    "net_days": 15,
    "insurance": "GL, Excess, Workers' Comp, and Disability maintained, with full Labor Law and action over coverage.",
    "acceptance": "Signatures below indicate acceptance of the scope, pricing, schedule, and terms outlined in this proposal.",
    "first_exclusion": "Any work not specifically listed above.",
    "footer": "Superstars Contracting Inc. · 356 Wythe Ave, Suite 300, Brooklyn, NY 11249 · 917.498.0002",
}


# ------------------------------------------------------------------ numbers and words
def money(v) -> str:
    return "${:,.2f}".format(float(v or 0))


def qty_text(v) -> str:
    v = float(v or 0)
    return "{:,.0f}".format(v) if v == int(v) else "{:,.2f}".format(v).rstrip("0").rstrip(".")


def amount(li: dict) -> float:
    return round(float(li.get("qty") or 0) * float(li.get("unit_price") or 0), 2)


def total(doc: dict) -> float:
    return round(sum(amount(li) for li in doc.get("lines") or []), 2)


def deposit(doc: dict, house: dict) -> float:
    return round(total(doc) * float(house["deposit_pct"]) / 100, 2)


def long_date(iso: Optional[str]) -> str:
    d = date.fromisoformat(iso) if iso else date.today()
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def join_names(names: list) -> str:
    names = [n for n in names if n]
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


def payment_text(doc: dict, house: dict) -> str:
    return (f"{'%g' % float(house['deposit_pct'])}% deposit ({money(deposit(doc, house))}) required to schedule; "
            f"balance upon completion. Net {house['net_days']} days.")


def coi_text(doc: dict) -> str:
    names = doc.get("additional_insureds") or ["the Owner"]
    return f"COI naming {join_names(names)} as Additional Insured issued prior to mobilization."


def notes_with_withdraw(doc: dict, house: dict) -> list:
    rest = [n for n in doc.get("notes") or [] if n and n.strip() != house["withdraw_note"]]
    return [house["withdraw_note"]] + rest


def exclusions_with_first(doc: dict, house: dict) -> list:
    rest = [x for x in doc.get("exclusions") or [] if x and x.strip() != house["first_exclusion"]]
    return [house["first_exclusion"]] + rest


def schedule_rows(doc: dict, house: dict, show_empty: bool = False) -> list:
    """[(label, text, field or None)] — field is the editable doc key; None = computed / house wording.
    show_empty (editing) keeps the Duration and Warranty rows so they can be filled in."""
    rows = [("Mobilization", doc.get("mobilization") or house["mobilization"], "mobilization")]
    if doc.get("duration") or show_empty:
        rows.append(("Duration", doc.get("duration") or "", "duration"))
    rows.append(("Hours", doc.get("hours") or house["hours"], "hours"))
    rows.append(("Payment", payment_text(doc, house), None))
    if doc.get("warranty") or show_empty:
        rows.append(("Warranty", doc.get("warranty") or "", "warranty"))
    rows.append(("Insurance", house["insurance"] + "\n" + coi_text(doc), None))
    return rows


_PLAIN_JOIN = ("and ", "of ", "for ", "with ", "to ", "at ", "in ", "on ", "(", ",")


def _joins_plain(detail) -> bool:
    """"Flagman and ground control" reads on; "Mobilization, site safety plan" takes a comma."""
    return str(detail or "").lstrip().lower().startswith(_PLAIN_JOIN)


# ------------------------------------------------------------------ the document
def _font_face() -> str:
    try:
        b64 = base64.b64encode(FONT.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f"@font-face{{font-family:'SSC Inter';font-weight:100 900;src:url(data:font/woff2;base64,{b64}) format('woff2')}}"


def _css(doc: dict, house: dict) -> str:
    foot_right = f"\"Proposal No. {_css_str(doc.get('number'))} · Page \" counter(page) \" of \" counter(pages)"
    return _font_face() + f"""
@page{{size:Letter;margin:.62in .62in .75in;
  @bottom-left{{width:62%;content:"{_css_str(house['footer'])}";font:8px 'SSC Inter',Inter,Arial,sans-serif;color:#8a8a8a;
    border-top:.5px solid #d9d9d9;padding-top:4px;vertical-align:top;margin-top:.28in}}
  @bottom-right{{width:38%;text-align:right;content:{foot_right};font:8px 'SSC Inter',Inter,Arial,sans-serif;color:#8a8a8a;
    border-top:.5px solid #d9d9d9;padding-top:4px;vertical-align:top;margin-top:.28in}}}}
*{{box-sizing:border-box}}
html{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
body{{margin:0;color:#1d1d1f;font:10.4px/1.5 'SSC Inter',Inter,-apple-system,Arial,sans-serif;background:#fff}}
.page{{max-width:7.26in;margin:0 auto}}
.hd{{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:2.4px solid {RED};padding-bottom:10px}}
.hd img{{width:1.68in;display:block}}
.hd .logo-txt{{font-weight:800;font-size:20px;letter-spacing:.5px}}
.meta{{text-align:right;font-size:11px;color:#444;line-height:1.55}}
.meta .kind{{font-size:38px;line-height:1;font-weight:800;color:{RED};letter-spacing:.3px;margin-bottom:8px}}
.meta .ttl{{font-weight:700;color:#1d1d1f}}
.meta .valid{{font-style:italic}}
.grid{{display:grid;grid-template-columns:1fr 1fr;border:1px solid #d6d6d6;margin-top:12px}}
.cell{{border-left:3.2px solid {RED};padding:10px 14px 11px;font-size:10.6px;line-height:1.55}}
.cell+.cell{{border-left:3.2px solid {RED}}}
.cell:nth-child(n+3){{border-top:1px solid #d6d6d6}}
.cell .lb{{font-size:7.6px;letter-spacing:2px;color:#777;margin-bottom:3px;text-transform:uppercase}}
.cell .first{{font-weight:700}}
h2{{color:{RED};font-size:13.6px;font-weight:700;margin:16px 0 6px;padding-bottom:5px;border-bottom:1px solid #dcdcdc}}
p{{margin:0}}
.ov{{font-size:10.4px;line-height:1.62}}
table{{width:100%;border-collapse:collapse}}
.bid th{{background:{RED};color:#fff;font-size:8px;letter-spacing:1.1px;text-align:left;padding:7px 8px;font-weight:700}}
.bid td{{padding:6px 8px;border-bottom:1px solid #e6e6e6;vertical-align:middle;font-size:10.4px}}
.bid tbody tr:nth-child(even) td{{background:#f7f6f4}}
.bid .n{{text-align:center;font-weight:700;width:28px}}
.bid .r{{text-align:right;white-space:nowrap}}
.bid .c{{text-align:center;white-space:nowrap}}
.bid .amt{{font-weight:700}}
.bid .lead{{font-weight:700}}
.bid.alt .n{{width:34px}}
.bid.alt tfoot td{{background:#f3f0ec;font-weight:700;font-size:10.4px;padding:7px 8px;border-bottom:1px solid #e6e6e6}}
.bid.alt tfoot td:first-child{{text-align:right}}
.tot{{background:{RED};color:#fff;display:flex;justify-content:flex-end;align-items:center;gap:14px;padding:9px 8px 9px}}
.tot .l{{font-weight:700;font-size:11.4px;letter-spacing:.3px}}
.tot .v{{font-weight:800;font-size:16px}}
.notes{{background:#f3efea;border-left:3.6px solid {RED};padding:10px 16px 12px;margin-top:2px;page-break-inside:avoid}}
.notes h3{{color:{RED};margin:0 0 4px;font-size:12px}}
ul{{margin:0;padding:0;list-style:none}}
li{{position:relative;padding-left:10px;margin:2px 0;line-height:1.5}}
li::before{{content:"\\2022";color:{RED};position:absolute;left:0;font-weight:700}}
.notes li:first-child{{font-weight:700}}
.cols{{columns:2;column-gap:22px}}
.cols li{{break-inside:avoid}}
.st td{{padding:6px 8px;border-bottom:1px solid #e3e3e3;vertical-align:top}}
.st td:first-child{{font-weight:700;width:1.42in}}
.st tr:first-child td{{border-top:0}}
.acc{{background:#f5f4f2;border:1px solid #dedede;padding:12px 16px 14px;page-break-inside:avoid}}
.acc .s{{color:#666;font-size:10px;margin-bottom:8px}}
.acc .two{{display:grid;grid-template-columns:1fr 1fr;gap:44px}}
.acc b{{font-size:10.4px;display:block;margin-bottom:24px}}
.acc .ln{{border-top:1px solid #555;font-size:9.6px;color:#666;padding-top:3px;margin-bottom:22px}}
.keep{{page-break-inside:avoid}}
.stamp{{background:#e7f6f0;color:#0c7a5f;border-radius:6px;padding:10px 14px;font-weight:700}}
li.new{{background:#fff3c4}}
@media screen{{body{{padding:28px 16px}}}}
"""


def _css_str(s: str) -> str:
    return str(s or "").replace("\\", "\\\\").replace('"', '\\"')


def render(doc: dict, house: Optional[dict] = None, *, mode: str = "pdf", logo_uri: Optional[str] = None,
           signed: Optional[dict] = None, flagged: Optional[list] = None) -> str:
    """mode 'pdf' | 'page' (the client's online page) | 'edit' (Proposal Studio: every text the person may change carries
    data-f / data-lf; `flagged` wording Claude wrote that isn't in the library is highlighted). `signed` = {name, title, at}
    replaces the signature lines with the acceptance record."""
    house = dict(HOUSE_DEFAULTS, **(house or {}))
    edit = mode == "edit"
    flag = {str(x).strip() for x in flagged or []}
    e = _html.escape

    def t(v) -> str:
        return e(str(v or "")).replace("\n", "<br>")

    def ed(key, inner, tag="span") -> str:
        return f'<{tag} data-f="{key}" contenteditable="true">{inner}</{tag}>' if edit else (inner if tag == "span" else f"<{tag}>{inner}</{tag}>")

    def lines_block(key, items, bold_first=False) -> str:
        items = [x for x in items or [] if x]
        body = "<br>".join((f"<span class='first'>{t(x)}</span>" if bold_first and i == 0 else t(x)) for i, x in enumerate(items))
        return ed(key, body, "div")

    def bullets(key, items, locked_first=False) -> str:
        out = []
        for i, x in enumerate(items):
            if locked_first and i == 0:
                out.append(f"<li>{t(x)}</li>")
            elif edit:
                cls = ' class="new"' if str(x).strip() in flag else ""
                out.append(f'<li{cls} data-f="{key}" data-i="{i - (1 if locked_first else 0)}" contenteditable="true">{t(x)}</li>')
            else:
                out.append(f"<li>{t(x)}</li>")
        return "".join(out)

    logo = f"<img src='{logo_uri}' alt='Superstars Contracting Inc.'>" if logo_uri else "<div class='logo-txt'>SUPERSTARS</div>"
    heading = (doc.get("heading") or "ESTIMATE").upper()
    def bid_rows(items, attr, num) -> str:
        """Base Bid lines (data-li / data-lf) and Add Alternates (data-ai / data-af) are the same row."""
        out = ""
        for i, li in enumerate(items or []):
            lead = t(li.get("lead"))
            detail = t(li.get("detail"))
            sep = ("" if not (lead and detail) else " " if _joins_plain(li.get("detail")) else ", ")
            if edit:
                cell = lambda k, v, cls="": f"<span{cls} data-{attr}f='{k}' contenteditable='true'>{v}</span>"
                desc = cell("lead", lead, " class='lead'") + sep + cell("detail", detail)
                q, u, r = cell("qty", qty_text(li.get("qty"))), cell("unit", e(li.get("unit") or "")), cell("unit_price", money(li.get("unit_price")))
            else:
                desc = (f"<span class='lead'>{lead}</span>" if lead else "") + sep + detail
                q, u, r = qty_text(li.get("qty")), e(li.get("unit") or ""), money(li.get("unit_price"))
            out += (f"<tr data-{attr}i='{i}'><td class='n'>{num(i)}</td><td>{desc}</td><td class='c'>{q}</td><td class='c'>{u}</td>"
                    f"<td class='r'>{r}</td><td class='r amt'>{money(amount(li))}</td></tr>")
        return out

    rows = bid_rows(doc.get("lines"), "l", lambda i: i + 1)
    alts = doc.get("alternates") or []
    alt_block = ""
    if alts:   # optional items the owner may add (proposal 1040): never in the base bid, the deposit or the acceptance
        with_rows = "".join(f"<tr class='with'><td colspan='5'>Base Bid with Add Alternate A{i}</td><td class='r'>{money(total(doc) + amount(a))}</td></tr>"
                            for i, a in enumerate(alts, 1))
        alt_block = (f"<div style='height:14px'></div><h2>Add Alternate{'s' if len(alts) > 1 else ''}</h2>"
                     f"<table class='bid alt'><thead><tr><th class='n'>#</th><th>DESCRIPTION</th><th class='c'>QTY</th><th class='c'>UNIT</th>"
                     f"<th class='r' style='text-align:left'>RATE</th><th class='r' style='text-align:left'>AMOUNT</th></tr></thead>"
                     f"<tbody>{bid_rows(alts, 'a', lambda i: f'A{i + 1}')}</tbody><tfoot>{with_rows}</tfoot></table>")

    deliv = doc.get("deliverables") or []
    sched = "".join(f"<tr><td>{e(lbl)}</td><td>{ed(key, t(txt)) if key else t(txt)}</td></tr>" for lbl, txt, key in schedule_rows(doc, house, edit))
    accepted = (doc.get("accepted_by") or "").upper()
    if signed:
        when = signed.get("at") or ""
        when = long_date(when[:10]) if when else ""
        sig = (f"<div class='stamp'>Accepted by {e(signed.get('name') or '')}{(', ' + e(signed['title'])) if signed.get('title') else ''}"
               f"{(' on ' + e(when)) if when else ''} (electronic acceptance)</div>")
    else:
        sig = (f"<div class=\"two\"><div><b>ACCEPTED BY {ed('accepted_by', e(accepted))}</b><div class=\"ln\">Signature</div>"
               f"<div class=\"ln\">Printed Name and Title / Date</div></div>"
               f"<div><b>{e(house['company_legal'].upper())}</b><div class=\"ln\">Authorized Signature ({e(house['signer'])})</div>"
               f"<div class=\"ln\">Date</div></div></div>")

    body = f"""
<div class="page">
  <div class="hd"><div>{logo}</div>
    <div class="meta"><div class="kind">{e(heading)}</div>
      <div class="ttl">{ed("title", t(doc.get("title")))}</div>
      <div>Proposal No. {e(str(doc.get("number") or ""))}</div>
      <div>Proposal Date: {e(long_date(doc.get("date")))}</div>
      <div class="valid">Proposal Valid: {e(house["valid_text"])}</div></div></div>
  <div class="grid keep">
    <div class="cell"><div class="lb">Submitted to</div>{lines_block("submitted_to", doc.get("submitted_to"), True)}</div>
    <div class="cell"><div class="lb">Submitted by</div>{"<br>".join((f"<span class='first'>{t(x)}</span>" if i == 0 else t(x)) for i, x in enumerate(house["submitted_by"]))}</div>
    <div class="cell"><div class="lb">Project</div>{lines_block("project", doc.get("project"))}</div>
    <div class="cell"><div class="lb">Reference</div>{lines_block("reference", doc.get("reference"))}</div>
  </div>
  <h2>Project Overview</h2>
  <p class="ov">{ed("overview", t(doc.get("overview")))}</p>
  <h2>Base Bid</h2>
  <table class="bid"><thead><tr><th class="n">#</th><th>DESCRIPTION</th><th class="c">QTY</th><th class="c">UNIT</th>
    <th class="r" style="text-align:left">RATE</th><th class="r" style="text-align:left">AMOUNT</th></tr></thead><tbody>{rows}</tbody></table>
  <div class="tot keep"><span class="l">BASE BID TOTAL</span><span class="v">{money(total(doc))}</span></div>
  {alt_block}
  <div style="height:14px"></div>
  <div class="notes"><h3>Important Notes</h3><ul>{bullets("notes", notes_with_withdraw(doc, house), True)}</ul></div>
  {f'<div class="keep"><h2>Deliverables</h2><ul class="cols">{bullets("deliverables", deliv)}</ul></div>' if deliv else ''}
  <div class="keep"><h2>Schedule &amp; Terms</h2><table class="st">{sched}</table></div>
  <div class="keep"><h2>Exclusions</h2><ul class="cols">{bullets("exclusions", exclusions_with_first(doc, house), True)}</ul></div>
  <div class="keep"><h2>Acceptance</h2>
    <div class="acc"><div class="s">{e(house["acceptance"])}</div>{sig}</div></div>
</div>"""
    return (f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>Proposal {e(str(doc.get('number') or ''))}</title>"
            f"<style>{_css(doc, house)}{EDIT_CSS if edit else ''}</style></head><body>{body}</body></html>")


EDIT_CSS = ("[data-f],[data-lf],[data-af]{border-radius:3px;outline:none;cursor:text;transition:background .3s}"
            "span[data-f],span[data-lf],span[data-af]{display:inline-block;min-width:2ch}"
            "span[data-lf=lead],span[data-lf=detail],span[data-af=lead],span[data-af=detail]{display:inline}"
            "[data-f]:hover,[data-lf]:hover,[data-af]:hover{box-shadow:0 0 0 1px rgba(67,100,220,.45)}"
            "[data-f]:focus,[data-lf]:focus,[data-af]:focus{background:rgba(67,100,220,.07);box-shadow:0 0 0 1px rgba(67,100,220,.6)}"
            "[data-f]:empty::before,[data-lf]:empty::before,[data-af]:empty::before{content:'…';color:#aab}"
            ".ssc-flash{background:#fff3c4!important}")
