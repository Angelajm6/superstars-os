"""The company's house style for templates (2026-10-26, Company → Templates → Improve design).

One locked layout, the way proposal_layout.py works for proposals: Claude only writes the CONTENT of a
template (`content`, below); everything that makes the documents look alike (letterhead, colors, fonts,
field boxes, tables, signature lines, footer, page numbers) is this file. A change here changes every
redesigned template at once, so all of them stay the same look.

  content = {
    "title": "Site Safety Plan", "subtitle": "Rope access",
    "blocks": [
      {"kind": "heading",    "text": "Emergency contacts"},
      {"kind": "paragraph",  "text": "All workers stay tied off ..."},
      {"kind": "note",       "text": "Keep a copy on site."},
      {"kind": "fields",     "fields": [{"label": "Job address", "source": "job_address", "wide": true}]},
      {"kind": "table",      "columns": ["Name", "Role", "Phone"], "rows": 4, "data": [["911", "", ""]]},
      {"kind": "checklist",  "items": ["Anchors inspected"]},
      {"kind": "signatures", "signers": ["Superintendent", "Site Safety Manager"]},
    ]}

`values` (step 2, a job's copy) maps a field `source` to the text printed in its box; anything not in it
prints as an empty box. Every string is escaped here: the content comes from Claude's read of an upload.
Pure: no DB, no API.
"""
from __future__ import annotations

import html
from typing import Optional

import brand
import typography

LAYOUT_VERSION = 1
RED = brand.BRAND["red"]
RED_DEEP = brand.BRAND["red_deep"]
INK = brand.BRAND["ink"]
MUTE = brand.BRAND["mute"]
CREAM = brand.BRAND["cream"]
RULE = "#E4E1DA"
KINDS = ("heading", "paragraph", "note", "fields", "table", "checklist", "signatures")

# What the house style is, in words (the House style card on the Templates page shows it).
STYLE_SUMMARY = {
    "colors": [{"name": "Brand red", "hex": RED}, {"name": "Ink", "hex": INK}, {"name": "Field box", "hex": CREAM},
               {"name": "Paper", "hex": "#FFFFFF"}],
    "headings": "Archivo", "body": "Inter",
    "letterhead": "Logo left, company name and address right, red rule",
    "footer": "Company name · template name, revision and page x of y",
}

e = html.escape


def _css(mode: str) -> str:
    sheet = ("" if mode == "pdf" else
             f".tl-sheet{{width:816px;min-height:1056px;background:#fff;color:{INK};padding:58px 60px 40px;"
             "box-sizing:border-box;display:flex;flex-direction:column;box-shadow:0 1px 4px rgba(20,22,28,.12)}"
             ".tl-body{flex:1}")
    page = "" if mode != "pdf" else "@page{size:Letter;margin:.6in .62in .72in}"
    return (typography.get_inlined_css() + page + sheet +
            f".tl{{font-family:{typography.INTER_STACK};font-size:10.5pt;line-height:1.45;color:{INK}}}"
            f".tl *{{box-sizing:border-box}}"
            f".tl-lh{{display:flex;justify-content:space-between;align-items:center;gap:16px;border-bottom:2.5px solid {RED};"
            "padding-bottom:10px;margin-bottom:18px}"
            ".tl-lh img{display:block;max-height:46px;max-width:220px;object-fit:contain}"
            f".tl-name{{font-family:{typography.ARCHIVO_STACK};font-weight:800;font-size:13pt;letter-spacing:.04em;text-transform:uppercase}}"
            f".tl-addr{{font-size:7.5pt;line-height:1.5;color:{MUTE};text-align:right}}.tl-addr b{{color:{INK};font-size:8.5pt}}"
            f".tl-eyebrow{{font-size:7.5pt;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:{RED}}}"
            f".tl-title{{font-family:{typography.ARCHIVO_STACK};font-weight:700;font-size:21pt;line-height:1.15;margin:2px 0 0;"
            "letter-spacing:-.01em}"
            f".tl-sub{{font-size:10.5pt;color:{MUTE};margin:3px 0 0}}"
            ".tl-head{margin-bottom:14px}"
            f".tl-h{{font-family:{typography.ARCHIVO_STACK};font-weight:700;font-size:11.5pt;margin:16px 0 6px;"
            f"padding-left:8px;border-left:3px solid {RED};break-after:avoid}}"
            ".tl-p{margin:0 0 7px;white-space:pre-line}"
            f".tl-note{{margin:6px 0 10px;padding:7px 10px;border-left:3px solid {RED};background:{CREAM};font-size:9.5pt}}"
            f".tl-fields{{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;background:{CREAM};border-radius:4px;"
            "padding:10px 12px;margin:6px 0 10px;break-inside:avoid}"
            ".tl-f.wide{grid-column:1/-1}"
            f".tl-f span{{display:block;font-size:7pt;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:{MUTE}}}"
            f".tl-f b{{display:block;min-height:17px;border-bottom:1px solid #CFC9BD;font-weight:500;padding:2px 0 1px}}"
            ".tl-t{width:100%;border-collapse:collapse;margin:6px 0 10px;font-size:9.5pt}"
            f".tl-t th{{text-align:left;font-weight:600;background:{INK};color:#fff;padding:5px 7px}}"
            f".tl-t td{{padding:5px 7px;border-bottom:1px solid {RULE};height:22px}}"
            ".tl-t tr{break-inside:avoid}"
            ".tl-cl{list-style:none;margin:4px 0 10px;padding:0}"
            ".tl-cl li{display:flex;gap:8px;align-items:flex-start;padding:3px 0}"
            f".tl-cl i{{flex:none;width:11px;height:11px;border:1.3px solid {INK};border-radius:2px;margin-top:3px}}"
            ".tl-sig{display:grid;grid-template-columns:1fr 1fr;gap:22px 28px;margin:22px 0 8px;break-inside:avoid}"
            f".tl-sig div{{border-top:1px solid {INK};padding-top:4px;font-size:8.5pt;color:{MUTE}}}"
            ".tl-sig div b{display:block;color:inherit;font-weight:600}"
            f".tl-foot{{display:flex;justify-content:space-between;gap:12px;border-top:1px solid {RULE};margin-top:18px;"
            f"padding-top:6px;font-size:7.5pt;color:{MUTE}}}")


def _css_str(s: str) -> str:
    """A CSS string literal (page-margin boxes print it as-is; never closes the rule)."""
    s = " ".join(str(s or "").split())
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\3C ") + '"'


def _letterhead(logo_uri: Optional[str], facts: dict) -> str:
    import company_brand
    mark = (f'<img alt="{e(brand.COMPANY_NAME)}" src="{e(logo_uri)}">' if logo_uri
            else f'<div class="tl-name">{e(brand.COMPANY_NAME)}</div>')
    lines = "".join("<br>" + e(x) for x in company_brand.letterhead_lines(facts or {}))
    return f'<header class="tl-lh">{mark}<div class="tl-addr"><b>{e(brand.COMPANY_NAME)}</b>{lines}</div></header>'


def _block(b: dict, values: dict) -> str:
    k = b.get("kind")
    if k == "heading":
        return f'<h2 class="tl-h">{e(b.get("text") or "")}</h2>'
    if k == "paragraph":
        return f'<p class="tl-p">{e(b.get("text") or "")}</p>'
    if k == "note":
        return f'<div class="tl-note">{e(b.get("text") or "")}</div>'
    if k == "fields":
        cells = "".join(
            f'<div class="tl-f{" wide" if f.get("wide") else ""}"><span>{e(f.get("label") or "")}</span>'
            f'<b>{e(str(values.get(f.get("source")) or "")) or "&nbsp;"}</b></div>'
            for f in b.get("fields") or [])
        return f'<div class="tl-fields">{cells}</div>'
    if k == "table":
        cols = b.get("columns") or []
        if not cols:
            return ""
        data = [list(r)[:len(cols)] + [""] * (len(cols) - len(r)) for r in (b.get("data") or [])]
        blank = max(0, int(b.get("rows") or 0) - len(data))
        rows = data + [[""] * len(cols) for _ in range(blank)]
        head = "".join(f"<th>{e(c)}</th>" for c in cols)
        body = "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>" for r in rows)
        return f'<table class="tl-t"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'
    if k == "checklist":
        return '<ul class="tl-cl">' + "".join(f"<li><i></i><span>{e(x)}</span></li>" for x in b.get("items") or []) + "</ul>"
    if k == "signatures":
        return '<div class="tl-sig">' + "".join(
            f"<div><b>{e(s)}</b>Signature · Date</div>" for s in b.get("signers") or []) + "</div>"
    return ""


def render(content: dict, *, folder_name: str = "", revision: str = "", logo_uri: Optional[str] = None,
           letterhead: Optional[dict] = None, mode: str = "pdf", values: Optional[dict] = None):
    """mode 'pdf' -> a whole self-contained HTML document for the PDF engine.
    mode 'page' -> (css, html) for the in-app preview: one sheet, the footer at its foot."""
    values = values or {}
    title = (content or {}).get("title") or "Untitled template"
    sub = (content or {}).get("subtitle") or ""
    foot_left = f"{brand.COMPANY_NAME} · {title}"
    foot_right = "Rev. " + revision if revision else ""
    body = "".join(_block(b, values) for b in (content or {}).get("blocks") or [] if isinstance(b, dict))
    head = (f'<div class="tl-head">{f"<div class=tl-eyebrow>{e(folder_name)}</div>" if folder_name else ""}'
            f'<h1 class="tl-title">{e(title)}</h1>{f"<p class=tl-sub>{e(sub)}</p>" if sub else ""}</div>')
    inner = _letterhead(logo_uri, letterhead or {}) + head + body
    if mode == "page":
        foot = f'<footer class="tl-foot"><span>{e(foot_left)}</span><span>{e(foot_right)} · Page 1</span></footer>'
        return _css("page"), f'<div class="tl tl-sheet"><div class="tl-body">{inner}</div>{foot}</div>'
    box = f"font:8pt Inter,sans-serif;color:{MUTE};border-top:.5pt solid {RULE};padding-top:4pt"
    css = (_css("pdf") +
           f"@page{{@bottom-left{{content:{_css_str(foot_left)};{box}}}"
           f"@bottom-right{{content:{_css_str(foot_right)} \"{'  ·  ' if foot_right else ''}Page \" counter(page) \" of \" counter(pages);{box}}}}}")
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>{e(title)}</title>'
            f"<style>{css}</style></head><body class=\"tl\">{inner}</body></html>")


def word_html(content: dict) -> str:
    """Plain HTML for claude_docs.docx_bytes (Word keeps the words and structure; the letterhead comes from
    Company brand there too). Fields become a two-column table of label / blank."""
    out = [f"<h1>{e((content or {}).get('title') or 'Untitled template')}</h1>"]
    if (content or {}).get("subtitle"):
        out.append(f"<p><i>{e(content['subtitle'])}</i></p>")
    for b in (content or {}).get("blocks") or []:
        k = b.get("kind")
        if k == "heading":
            out.append(f"<h2>{e(b.get('text') or '')}</h2>")
        elif k in ("paragraph", "note"):
            out.append(f"<p>{e(b.get('text') or '')}</p>")
        elif k == "fields":
            out.append("<table>" + "".join(f"<tr><td><b>{e(f.get('label') or '')}</b></td><td> </td></tr>"
                                           for f in b.get("fields") or []) + "</table>")
        elif k == "table" and b.get("columns"):
            cols = b["columns"]
            rows = [list(r) for r in b.get("data") or []] + [[""] * len(cols)] * max(0, int(b.get("rows") or 0) - len(b.get("data") or []))
            out.append("<table><tr>" + "".join(f"<th>{e(c)}</th>" for c in cols) + "</tr>" +
                       "".join("<tr>" + "".join(f"<td>{e(str(c)) or ' '}</td>" for c in r[:len(cols)]) + "</tr>" for r in rows) + "</table>")
        elif k == "checklist":
            out.append("<ul>" + "".join(f"<li>☐ {e(x)}</li>" for x in b.get("items") or []) + "</ul>")
        elif k == "signatures":
            out.append("<table>" + "".join(f"<tr><td><b>{e(s)}</b></td><td>Signature: ____________  Date: ________</td></tr>"
                                           for s in b.get("signers") or []) + "</table>")
    return "".join(out)
