"""
smoke_design_conventions.py — design-system guard (#236).

Stops the two recurring UI regressions at the root by FAILING the build if:
  (a) a known PRIMARY/SUBMIT button on a Lumen-modal surface would compute to a
      transparent / near-white background — the invisible-button bug (#230 Docs,
      #235 Field Photos). Cause: the button's background is var(--X) where --X is
      a per-MODULE token (--fp-accent on .fp-wrap, etc.) that does NOT exist at
      :root — and the modal renders at the page root, OUTSIDE that wrapper, so
      var(--X) resolves to nothing -> transparent.
  (b) a raw, un-wired MM/DD/YYYY text date field exists on those surfaces (the
      auto-wire only catches .ssc-date + <input type=date>; a bare text box with
      a date placeholder slips through).

It works WITHOUT a browser: it parses the served HTML + widgets.css and resolves
the CSS custom-property graph the way a modal at page root would (only :root
tokens resolve). A self-test proves the guard FAILS on a deliberately broken
button + raw date and PASSES on the fixed ones. Read-only; touches no data.
"""
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import requests
# (Path is used by the #241 i18n-apostrophe guard below.)

import _smoke_auth  # noqa: E402
_smoke_auth.setup()

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5050")
PASS, FAIL = [], []


def ok(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {note}" if note else ""))
    return bool(cond)


# ---------- CSS token / color resolution (modal = page root: only :root resolves) ----------
def parse_root_tokens(css):
    tokens = {}
    for block in re.findall(r":root\s*\{([^}]*)\}", css, re.DOTALL):
        for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", block):
            tokens[m.group(1).strip()] = m.group(2).strip()
    return tokens


def class_background(css, cls):
    """Last `background[-color]` declaration for a bare `.cls { ... }` rule."""
    bg = None
    for m in re.finditer(r"(?:^|[,\s}])\." + re.escape(cls) + r"\s*\{([^}]*)\}", css, re.DOTALL):
        bm = re.search(r"background(?:-color)?\s*:\s*([^;]+)", m.group(1))
        if bm:
            bg = bm.group(1).strip()
    return bg


def resolve_color(value, root_tokens):
    """Resolve a CSS color value as a MODAL (page-root) would: var() resolves
    ONLY from :root tokens. Returns the literal color, or None if a var() can't
    resolve (which computes to transparent -> the invisible-button bug)."""
    value = (value or "").strip()
    for _ in range(6):
        m = re.match(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^)]+))?\)\s*$", value)
        if not m:
            break
        name, fallback = m.group(1), (m.group(2) or "").strip()
        if name in root_tokens:
            value = root_tokens[name].strip()
        elif fallback:
            value = fallback
        else:
            return None  # unresolved -> transparent
    return value


def is_visible_bg(color):
    if not color:
        return False
    c = color.strip().lower()
    if c in ("transparent", "none", "inherit", "initial", "unset", ""):
        return False
    if c in ("#fff", "#ffffff", "white", "#fefefe"):
        return False
    if c.startswith("rgba") and re.search(r",\s*0(?:\.0+)?\s*\)$", c):
        return False  # zero alpha
    return True


def find_tag(html, el_id):
    m = re.search(r"<button[^>]*\bid=\"" + re.escape(el_id) + r"\"[^>]*>", html)
    if not m:
        m = re.search(r"<[a-zA-Z]+[^>]*\bid=\"" + re.escape(el_id) + r"\"[^>]*>", html)
    return m.group(0) if m else None


def button_bg(html, css, el_id, root_tokens):
    tag = find_tag(html, el_id)
    if not tag:
        return None, "(button not found)"
    cm = re.search(r'class="([^"]*)"', tag)
    classes = cm.group(1).split() if cm else []
    bg = None
    for cls in classes:
        b = class_background(css, cls)
        if b:
            bg = b
    if bg is None:
        sm = re.search(r"style=\"[^\"]*background(?:-color)?\s*:\s*([^;\"]+)", tag)
        if sm:
            bg = sm.group(1).strip()
    if bg is None:
        return None, f"(no background for classes {classes})"
    return resolve_color(bg, root_tokens), bg


# ---------- raw-date detection ----------
def raw_date_inputs(html):
    """Text inputs with a MM/DD/YYYY-style placeholder that LACK .ssc-date — the
    auto-wire can't reach them, so they ship as raw date boxes."""
    hits = []
    for tag in re.findall(r"<input\b[^>]*>", html):
        ph = (re.search(r'placeholder="([^"]*)"', tag) or _N).group(1) if re.search(r'placeholder="([^"]*)"', tag) else ""
        cls = (re.search(r'class="([^"]*)"', tag) or _N).group(1) if re.search(r'class="([^"]*)"', tag) else ""
        is_mdy_text = bool(re.search(r"MM\s*[/-]\s*DD\s*[/-]\s*YYYY", ph, re.I)) and 'type="date"' not in tag
        if is_mdy_text and "ssc-date" not in cls:
            hits.append(tag[:90])
    return hits


class _N:
    @staticmethod
    def group(_):
        return ""


# ---------- drop-zone boundary (#237): a known modal drop zone must show a box ----------
def class_decl(css, cls, prop):
    """Last `prop: value` declaration for a bare `.cls { ... }` rule."""
    val = None
    for m in re.finditer(r"(?:^|[,\s}])\." + re.escape(cls) + r"\s*\{([^}]*)\}", css, re.DOTALL):
        pm = re.search(prop + r"\s*:\s*([^;]+)", m.group(1))
        if pm:
            val = pm.group(1).strip()
    return val


_COLOR_TOK = r"(var\([^)]*\)|#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\)|transparent|currentColor|[a-zA-Z]+)"


def border_color_of(border_decl):
    """The color token from a `border` shorthand (skip width/style keywords)."""
    if not border_decl:
        return None
    for t in reversed(re.findall(_COLOR_TOK, border_decl)):
        if t.lower() in ("solid", "dashed", "dotted", "double", "groove", "ridge", "inset",
                         "outset", "none", "hidden", "px", "em", "rem"):
            continue
        if re.match(r"^\d", t):
            continue
        return t
    return None


def box_visible(html, css, el_id, root):
    """A drop zone is visible iff its border-color OR background resolves (the way
    a page-root modal would) to a non-transparent value."""
    tag = find_tag(html, el_id)
    if not tag:
        return None
    classes = (re.search(r'class="([^"]*)"', tag).group(1).split()
               if re.search(r'class="([^"]*)"', tag) else [])
    border = bg = None
    for cls in classes:
        b = class_decl(css, cls, "border")
        bc = class_decl(css, cls, "border-color")
        g = class_background(css, cls)
        if b:
            border = b
        if bc:
            border = bc            # explicit border-color wins the color
        if g:
            bg = g
    bcol = border_color_of(border)
    bres = resolve_color(bcol, root) if bcol else None
    gres = resolve_color(bg, root) if bg else None
    return {"visible": is_visible_bg(bres) or is_visible_bg(gres),
            "dashed": bool(border and "dashed" in border),
            "border": bcol, "border_resolved": bres, "bg": bg, "bg_resolved": gres}


# ---------- form-field boxes (#238): inputs/selects/textareas must be bordered ----------
def selector_decl(css, selector, prop):
    """Last `prop:value` for a rule whose comma-separated selector list contains
    `selector` (e.g. ".fp-field input")."""
    val = None
    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css, re.DOTALL):
        if selector in [s.strip() for s in m.group(1).split(",")]:
            pm = re.search(prop + r"\s*:\s*([^;]+)", m.group(2))
            if pm:
                val = pm.group(1).strip()
    return val


# (#275) field_border_visible — the per-ID resolver — was superseded by the
# structural scanner (ModalFieldScanner + modal_field_boxed below), which applies
# the same page-root resolution to EVERY dialog field instead of an enrolled list.


# ---------- #275 — STRUCTURAL any-modal-field boxing ----------
# The old guard enumerated field IDs per modal (#238 §6, #242 8d, #250 8d2) — every
# new modal had to remember to enroll, and the Materials modals didn't (fields
# shipped boxless: the .mt-overlay modals sat outside the wrapper defining the
# --mt-* tokens, so var(--mt-line) computed away — the #230/#235 class, on fields).
# This is the ROOT FIX: on EVERY registered surface, ANY <input|select|textarea>
# whose ancestors include a dialog container MUST be provably boxed — either it
# carries the shared component (.ssc-field) or its module CSS (wrapper `.X-fld tag`
# rule / own class) resolves to a VISIBLE border the page-root way. No enumerated
# ID lists for field boxing, ever again.
#
# A dialog container is: class/id containing 'modal'/'overlay'/'dialog', or
# role="dialog" / aria-modal="true" (slide-in panels like #profile-panel carry the
# semantic role — add it to any new drawer/panel with form fields).
#
# LIMIT (deliberate): JS-built modal bodies (CRM/EST template strings) are invisible
# to a static scan; those modules build every field with .ssc-field and the browser-
# level behavioral checks cover them. Static markup — where this class of regression
# has actually shipped — is fully covered here.
_DIALOG_MARKERS = ("modal", "overlay", "dialog")
# native controls that are NOT text-entry boxes — the boxed-component rule doesn't apply
FIELD_TYPE_EXEMPT = {"checkbox", "radio", "file", "hidden", "button", "submit",
                     "reset", "range", "color", "image"}
# deliberate per-id exceptions — keep SHORT, every entry carries its why
FIELD_ID_EXEMPT = {
    # (none — the day one lands, document the reason right here)
}
_VOID_TAGS = {"input", "br", "img", "hr", "meta", "link", "source", "wbr", "area",
              "base", "col", "embed", "track", "param"}


class ModalFieldScanner(HTMLParser):
    """Collect every input/select/textarea whose ANCESTOR chain contains a dialog
    container, with its own classes + the nearest `*-fld`/`*-field` wrapper class.
    <script> content is data to HTMLParser, so JS-built template strings never
    produce phantom fields."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []    # (tag, is_dialog, wrapper_cls)
        self.fields = []

    @staticmethod
    def _is_dialog(a):
        blob = ((a.get("class") or "") + " " + (a.get("id") or "")).lower()
        if any(m in blob for m in _DIALOG_MARKERS):
            return True
        return (a.get("role") or "").lower() == "dialog" or (a.get("aria-modal") or "").lower() == "true"

    def _record_if_field(self, tag, a):
        if tag not in ("input", "select", "textarea"):
            return
        if not any(f[1] for f in self.stack):
            return                              # not inside a dialog container
        wrapper = next((f[2] for f in reversed(self.stack) if f[2]), None)
        self.fields.append({
            "tag": tag, "id": a.get("id") or "",
            "type": (a.get("type") or "text").lower(),
            "classes": (a.get("class") or "").split(),
            "wrapper": wrapper,
        })

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self._record_if_field(tag, a)           # ancestors only — before any push
        if tag in _VOID_TAGS:
            return
        # wrapper tokens: `x-fld` / `x-field` AND the bare `fld` / `field` (the
        # admin-labor-rates page wraps modal fields in a plain `.field` whose
        # `.field input` rule resolves via the page's own :root — a valid box).
        wrapper = next((c for c in (a.get("class") or "").split()
                        if re.match(r"^([\w-]+-)?(fld|field)$", c)), None)
        self.stack.append((tag, self._is_dialog(a), wrapper))

    def handle_startendtag(self, tag, attrs):
        self._record_if_field(tag, dict(attrs))  # self-closed: record, never push

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def scan_modal_fields(html_text):
    s = ModalFieldScanner()
    s.feed(html_text)
    return s.fields


def modal_field_boxed(f, css_all, root):
    """(passes, how). Shared component wins outright; otherwise the field must
    resolve a VISIBLE border at page root via its wrapper rule or its own class."""
    if f["type"] in FIELD_TYPE_EXEMPT:
        return True, "type-exempt"
    if f["id"] in FIELD_ID_EXEMPT:
        return True, "id-exempt: " + str(FIELD_ID_EXEMPT[f["id"]])
    if "ssc-field" in f["classes"]:
        return True, "ssc-field"
    if f["wrapper"]:
        b = (selector_decl(css_all, f".{f['wrapper']} {f['tag']}", "border-color")
             or selector_decl(css_all, f".{f['wrapper']} {f['tag']}", "border"))
        if b and is_visible_bg(resolve_color(border_color_of(b), root)):
            return True, f"wrapper .{f['wrapper']}"
    for c in f["classes"]:
        b = class_decl(css_all, c, "border-color") or class_decl(css_all, c, "border")
        if b and is_visible_bg(resolve_color(border_color_of(b), root)):
            return True, f"class .{c}"
    return False, "boxless"


def page_modal_field_violations(page_text, widgets_css):
    """All (field, why) violations for one page, resolved against widgets.css +
    the page's own inline styles (the exact stylesheet set the page loads)."""
    inline = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", page_text, re.DOTALL))
    css_all = widgets_css + "\n" + inline
    root = parse_root_tokens(css_all)
    fields = scan_modal_fields(page_text)
    bad = []
    for f in fields:
        okd, how = modal_field_boxed(f, css_all, root)
        if not okd:
            bad.append(f"{f['tag']}#{f['id'] or '(no id)'}")
    return fields, bad


# ---------- #361 — density rules for the project dashboard ----------
def _print_spans(s):
    spans = []
    for m in re.finditer(r"@media\s+print\b[^{]*\{|@page\b[^{]*\{", s):
        depth, i = 1, m.end()
        while depth and i < len(s):
            depth += {"{": 1, "}": -1}.get(s[i], 0); i += 1
        spans.append((m.start(), i))
    return spans


_DENSE = re.compile(r"^\s*([.#](dp|ph|wx|cal|la)[-_]|.*\.(fp-photo|exp-thumb)\b)")


def _floor(block, selector):
    """The smallest size a rule may use: 11px for an uppercase / letter-spaced label or a
    dense grid (drop board, health widgets, weather, look-ahead, photo overlays); 12px
    for every other piece of screen text."""
    if re.search(r"text-transform:\s*uppercase", block):
        return 11.0
    ls = re.search(r"letter-spacing:\s*([\d.]+)(em|px)", block)
    if ls and ((ls.group(2) == "em" and float(ls.group(1)) >= 0.08) or (ls.group(2) == "px" and float(ls.group(1)) >= 0.6)):
        return 11.0
    if selector is not None and all(_DENSE.match(x) for x in selector.split(",") if x.strip()):
        return 11.0
    return 12.0


def density_violations(page):
    """[(where, size, floor)] for screen text below its floor — CSS rules and inline
    style attributes (HTML and JS strings); @media print / @page are exempt."""
    skip = _print_spans(page)
    out = []
    def scan(start, block, sel, where):
        if any(a <= start < b for a, b in skip):
            return
        for fm in re.finditer(r"font-size:\s*([\d.]+)px", block):
            v, fl = float(fm.group(1)), _floor(block, sel)
            if v < fl:
                out.append((where, v, fl))
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", page):
        sel = m.group(1).strip().split("\n")[-1].strip()
        if not sel.startswith("@"):
            scan(m.start(2), m.group(2), sel, sel[:60])
    for m in re.finditer(r"style=(\\?[\"'])", page):
        q = m.group(1)[-1]; a = m.end(); b = page.find(q, a)
        if b > 0:
            scan(a, page[a:b], None, "inline:" + page[a:b][:50])
    return out


def main():
    print("== #236 design-conventions guard ==")
    css = requests.get(f"{BASE}/files/static/css/widgets.css", timeout=15).text
    dp = requests.get(f"{BASE}/files/static/js/datepicker.js", timeout=15).text
    html = requests.get(f"{BASE}/projects/FR-BX-001", timeout=20).text
    # Resolve against widgets.css :root + the page's inline <style> blocks (where
    # per-module rules like .exp-primary live). :root tokens come only from true
    # :root blocks; module tokens on .fp-wrap/.pd-wrap are NOT global (correct).
    inline = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.DOTALL))
    css_all = css + "\n" + inline
    root = parse_root_tokens(css_all)

    # ---- 0) globalization wiring is in place ----
    ok("root_accent_is_lumen", root.get("--accent", "").lower() in ("#4364dc", "rgb(67,100,220)"),
       f"--accent={root.get('--accent')}")
    ok("root_has_core_tokens", all(t in root for t in ("--accent", "--ink", "--muted", "--line")),
       str(sorted(k for k in root if k in ("--accent", "--ink", "--muted", "--line"))))
    ok("shared_btn_primary_defined", class_background(css, "btn-primary") is not None)
    ok("datepicker_autowire_present", "autoWire" in dp and "MutationObserver" in dp)

    # ---- 1) SELF-TEST: the guard catches a deliberately-broken button + raw date ----
    print("\n-- self-test (must catch broken, pass fixed) --")
    broken = resolve_color("var(--fp-accent)", root)          # module token, not at :root
    ok("selftest_broken_button_caught", not is_visible_bg(broken), f"var(--fp-accent) -> {broken} (transparent)")
    good = resolve_color("var(--accent)", root)
    ok("selftest_fixed_button_passes", is_visible_bg(good), f"var(--accent) -> {good}")
    ok("selftest_raw_date_caught",
       len(raw_date_inputs('<input type="text" id="x" placeholder="MM/DD/YYYY">')) == 1)
    ok("selftest_ssc_date_passes",
       len(raw_date_inputs('<input type="text" class="ssc-date" placeholder="MM-DD-YYYY">')) == 0)

    # ---- 2) REAL: known Lumen-modal primary buttons must resolve to a visible accent ----
    print("\n-- real surfaces (project dashboard) --")
    checks = [
        ("fieldphotos_upload_btn", "fp-up-go"),
        ("docs_save_single", "pd-save-single"),
        ("docs_save_bulk", "pd-save-bulk"),
        ("expense_scan_btn", "exp-scan-btn"),
    ]
    for name, bid in checks:
        resolved, raw = button_bg(html, css_all, bid, root)
        ok(name + "_visible", is_visible_bg(resolved), f"{bid}: {raw} -> {resolved}")
        # extra: the modal action buttons should be the accent blue specifically
        if bid in ("fp-up-go", "pd-save-single", "pd-save-bulk"):
            ok(name + "_is_accent", (resolved or "").lower() in ("#4364dc", "rgb(67,100,220)"),
               f"{bid} -> {resolved}")

    # ---- 3) REAL: no raw un-wired date boxes anywhere on the dashboard ----
    raws = raw_date_inputs(html)
    ok("no_raw_date_inputs", len(raws) == 0, ("found: " + "; ".join(raws)) if raws else "none")

    # ---- 4) the Field Photos date field is the standard SSCDatePicker field ----
    fp_date = re.search(r'<input[^>]*id="fp-up-date"[^>]*>', html)
    ok("fieldphotos_date_is_ssc", bool(fp_date) and "ssc-date" in fp_date.group(0),
       fp_date.group(0)[:100] if fp_date else "(not found)")

    # ---- 5) drop zones must show a VISIBLE box (#237) ----
    print("\n-- drop-zone boundary (#237) --")
    # self-test (end-to-end): a module-token (page-root-unresolvable) drop zone is
    # caught; the shared .dropzone passes.
    broken = box_visible('<div class="brk-dz" id="brk">x</div>',
                         ".brk-dz{border:2px dashed var(--fp-accent-edge);background:var(--fp-accent-soft)}", "brk", root)
    ok("selftest_stripped_dropzone_caught", not broken["visible"],
       f"border->{broken['border_resolved']} bg->{broken['bg_resolved']}")
    fixed = box_visible('<div class="dropzone" id="fix">x</div>', css_all, "fix", root)
    ok("selftest_fixed_dropzone_passes", fixed["visible"],
       f"border->{fixed['border_resolved']} bg->{fixed['bg_resolved']}")
    ok("shared_dropzone_defined", class_decl(css, "dropzone", "border") is not None)
    # real: the Field Photos + Project Documents drop zones show a visible dashed box
    for name, did in (("fieldphotos_dropzone", "fp-dropzone"), ("docs_dropzone", "pd-drop")):
        box = box_visible(html, css_all, did, root)
        ok(name + "_visible_box", bool(box and box["visible"]),
           (f"border {box['border']}->{box['border_resolved']} · bg {box['bg']}->{box['bg_resolved']}")
           if box else "(not found)")
        ok(name + "_dashed_border", bool(box and box["dashed"]), str(box["dashed"] if box else None))

    # ---- 6) every form field on a known modal must have a VISIBLE box (#238) ----
    print("\n-- form-field boxes (#238) --")
    # self-test: a module-token field border (page-root-unresolvable) is caught; the
    # global token passes.
    ok("selftest_bare_field_caught", not is_visible_bg(resolve_color("var(--fp-line)", root)),
       f"var(--fp-line) -> {resolve_color('var(--fp-line)', root)}")
    ok("selftest_boxed_field_passes", is_visible_bg(resolve_color("var(--line)", root)),
       f"var(--line) -> {resolve_color('var(--line)', root)}")
    ok("shared_field_rule_resolves",
       is_visible_bg(resolve_color(border_color_of(selector_decl(css, ".fp-field input", "border")), root)),
       f".fp-field input -> {selector_decl(css, '.fp-field input', 'border')}")
    # (#275) the per-ID modal-field list that lived here was GENERALIZED into the
    # structural any-modal-field rule below — every dialog field on every registered
    # surface is now scanned; nothing enrolls by ID anymore.
    # Field Photos: Description is a free-text textarea; Worker/Stage are gone
    ok("fp_description_is_textarea", bool(re.search(r'<textarea[^>]*id="fp-up-desc"', html)))
    ok("fp_worker_stage_removed",
       all(x not in html for x in ('id="fp-up-worker"', 'id="fp-up-stage"', 'id="fp-bar-stage"')))

    # ---- 7) i18n dicts: no escaped possessive apostrophes (#241; CLAUDE.md JS rule) ----
    # #240 found a `project\'s` inside company-dashboard's I18N dict that a manual
    # grep had missed (#239). Automated here: FAIL if any \' escape appears inside
    # an I18N dictionary block in any repo-root .html.
    print("\n-- i18n apostrophe guard (#241) --")

    def i18n_blocks(text):
        """Every `I18N = {...}` object literal, extracted by brace counting."""
        blocks = []
        for m in re.finditer(r"\bI18N\s*=\s*\{", text):
            depth = 0
            start = m.end() - 1
            for j in range(start, len(text)):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        blocks.append(text[start:j + 1])
                        break
        return blocks

    def i18n_escaped_apostrophes(text):
        hits = []
        for bi, block in enumerate(i18n_blocks(text)):
            for n, line in enumerate(block.splitlines(), 1):
                if "\\'" in line:
                    hits.append(f"I18N block {bi + 1} line {n}")
        return hits

    bad_doc = "const I18N = { en: { 'k': 'the worker\\'s folder' } };"
    ok("selftest_i18n_apostrophe_caught", len(i18n_escaped_apostrophes(bad_doc)) == 1)
    good_doc = "const I18N = { en: { 'k': 'the worker folder' } };"
    ok("selftest_i18n_clean_passes", len(i18n_escaped_apostrophes(good_doc)) == 0)
    root_dir = Path(__file__).resolve().parent.parent
    scanned = 0
    for page in sorted(root_dir.glob("*.html")):
        text = page.read_text(encoding="utf-8", errors="replace")
        if "I18N" not in text:
            continue
        scanned += 1
        hits = i18n_escaped_apostrophes(text)
        ok("i18n_no_escaped_apostrophes_" + page.name, not hits,
           "; ".join(hits[:3]) if hits else "clean")
    ok("i18n_guard_scanned_pages", scanned >= 1, f"{scanned} page(s) with an I18N dict")

    # ---- 8) #242 — registered-surface coverage. The guard previously scanned
    # ONLY the project dashboard, so the Workforce surface (company console)
    # shipped off-system styling unnoticed. Every registered surface now gets
    # the applicable checks, and a template registry forces NEW pages to opt in.
    print("\n-- registered surfaces (#242) --")
    console_html = requests.get(f"{BASE}/", timeout=20).text
    console_inline = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", console_html, re.DOTALL))
    console_css = css + "\n" + console_inline
    console_root = parse_root_tokens(console_css)

    # 8a. Workforce primary actions resolve to the shared accent (.btn-primary)
    for name, bid in (("console_onboard_btn", "add-worker-btn"), ("console_modal_save", "modal-save")):
        resolved, raw = button_bg(console_html, console_css, bid, console_root)
        ok(name + "_visible", is_visible_bg(resolved), f"{bid}: {raw} -> {resolved}")
        ok(name + "_is_accent", (resolved or "").lower() in ("#4364dc", "rgb(67,100,220)"),
           f"{bid} -> {resolved}")

    # 8b. no raw text-date inputs on the console
    console_raws = raw_date_inputs(console_html)
    ok("console_no_raw_date_inputs", len(console_raws) == 0,
       ("found: " + "; ".join(console_raws)) if console_raws else "none")

    # 8c. every date field in real (non-script) console markup is IDENTIFIED —
    # a placeholder / data-i18n-ph, or an adjacent <label>. An unlabeled bare
    # date box was the operator-reported Workforce-intake defect (#242).
    nonscript = re.sub(r"<script\b.*?</script>", "", console_html, flags=re.DOTALL | re.IGNORECASE)
    unlabeled = []
    for m in re.finditer(r"<input[^>]*>", nonscript):
        tag = m.group(0)
        if ('type="date"' not in tag) and ("ssc-date" not in tag):
            continue
        if ("placeholder=" in tag) or ("data-i18n-ph=" in tag):
            continue
        if "<label" in nonscript[max(0, m.start() - 220):m.start()]:
            continue
        idm = re.search(r'id="([^"]+)"', tag)
        unlabeled.append(idm.group(1) if idm else tag[:60])
    ok("console_date_fields_identified", not unlabeled,
       ("unlabeled: " + ", ".join(unlabeled)) if unlabeled else "all date fields labeled")

    # 8d / 8d2 — (#275) the intake-modal + profile-edit per-ID lists were GENERALIZED
    # into the structural any-modal-field rule (§9 below). #profile-panel carries
    # role="dialog" so the slide-in stays covered structurally; sentinel assertions
    # in §9 prove the #242/#250 fields are still scanned.

    # 8e. TEMPLATE REGISTRY — every repo-root page with form inputs must be
    # REGISTERED (a live operator surface this guard covers) or explicitly
    # EXEMPT (legacy, pre-rebuild artifacts). A new template that registers
    # in neither fails the build — the forcing function that closes the
    # "guard never looked here" gap for good.
    REGISTERED = {"company-dashboard.html", "dashboard-static.html", "admin_labor_rates.html",
                  "login.html", "worker-app.html", "dropplan.html", "rfi_submission_form.html",
                  "admin_users.html", "set_password.html",   # #257 multi-user accounts & roles
                  "admin_projects.html",   # #263 PM project-scoping (assignment + close screen)
                  "estimating.html",       # #276 estimating workspace (estimator/admin/c_suite)
                  "ui_settings.html",      # #279 UI v2 interface switch (Classic / New)
                  "portal_shell.html",     # #285 client portal shell (gained its drop filter)
                  "company_portal.html",   # #302 C-suite company portal (company-document modal)
                  "forgot_password.html", "reset_password.html",  # #301 self-service password reset
                  "account.html"}          # Settings > Account (each person's own account)
    # #283 — surfaces that live OUTSIDE the repo root still get the structural
    # sweep. drawing-markup is served from templates/v2 (the #279 v2 tree), so the
    # root glob above cannot see it; registering it by path keeps the "guard never
    # looked here" gap closed for the v2 tree too.
    REGISTERED_PATHS = {"templates/v2/drawing-markup.html",   # #280 drawing markup
                        "templates/v2/drawing-author.html"}   # #293 drawing author
    EXEMPT = {"facade-dashboard.html", "facade-dashboard-presentation.html"}  # pre-rebuild legacy
    unregistered = []
    for page in sorted(root_dir.glob("*.html")):
        if page.name in REGISTERED or page.name in EXEMPT:
            continue
        if re.search(r"<(?:input|select|textarea)\b", page.read_text(encoding="utf-8", errors="replace")):
            unregistered.append(page.name)
    ok("templates_registered_or_exempt", not unregistered,
       ("REGISTER OR EXEMPT: " + ", ".join(unregistered)) if unregistered
       else f"{len(REGISTERED)} registered / {len(EXEMPT)} exempt")

    # #283 — the SAME forcing function for the v2 template tree. A new page under
    # templates/v2 with form inputs must register in REGISTERED_PATHS or fail here;
    # otherwise the v2 tree would silently repeat the gap the root registry closed.
    v2_dir = root_dir / "templates" / "v2"
    v2_unregistered = []
    if v2_dir.exists():
        for page in sorted(v2_dir.glob("*.html")):
            rel = page.relative_to(root_dir).as_posix()
            if rel in REGISTERED_PATHS:
                continue
            if re.search(r"<(?:input|select|textarea)\b",
                         page.read_text(encoding="utf-8", errors="replace")):
                v2_unregistered.append(rel)
    ok("v2_templates_registered", not v2_unregistered,
       ("REGISTER IN REGISTERED_PATHS: " + ", ".join(v2_unregistered)) if v2_unregistered
       else f"{len(REGISTERED_PATHS)} v2 path(s) registered")

    # ---- 9) #275 — STRUCTURAL any-modal-field boxing on EVERY registered surface ----
    # No enumerated-ID lists for field boxing ever again: any input/select/textarea
    # inside a dialog container must be provably boxed (shared .ssc-field component,
    # or module CSS that resolves a VISIBLE border at page root).
    print("\n-- structural any-modal-field boxing (#275) --")
    # self-test A (fail->pass): a planted BOXLESS field in a synthetic modal is
    # caught — its wrapper rule borders with a module token no modal can resolve
    # (the exact Materials regression: .mt-fld input + var(--mt-line) outside .mt-wrap).
    _syn_bad = '<div class="x-modal"><div class="y-fld"><input id="plant"></div></div>'
    _syn_css = ".y-fld input{border:1px solid var(--module-token);}"
    _f, _bad = page_modal_field_violations("<style>" + _syn_css + "</style>" + _syn_bad, css)
    ok("selftest_planted_boxless_caught", len(_f) == 1 and _bad == ["input#plant"], f"{_bad}")
    # ...and the SAME field passes once it carries the shared component (the fix)
    _syn_fixed = _syn_bad.replace('<input id="plant">', '<input class="ssc-field" id="plant">')
    _f2, _bad2 = page_modal_field_violations("<style>" + _syn_css + "</style>" + _syn_fixed, css)
    ok("selftest_planted_fixed_passes", len(_f2) == 1 and not _bad2, f"{_bad2}")
    # module-CSS route: a wrapper rule that RESOLVES (literal color) is a valid box
    _f3, _bad3 = page_modal_field_violations(
        "<style>.y-fld input{border:1px solid #ccd;}</style>" + _syn_bad, css)
    ok("selftest_css_boxed_passes", len(_f3) == 1 and not _bad3, f"{_bad3}")
    # exemptions: native non-text controls are exempt BY TYPE (checkbox/file/...)
    _f4, _bad4 = page_modal_field_violations(
        '<div class="x-modal"><input type="checkbox" id="c"><input type="file" id="g"></div>', css)
    ok("selftest_type_exemptions_pass", len(_f4) == 2 and not _bad4, f"{_bad4}")
    # a field OUTSIDE any dialog container is out of scope (page-body forms have
    # their own module styling; this rule is about modals at page root)
    _f5, _ = page_modal_field_violations('<div class="plain"><input id="free"></div>', css)
    ok("selftest_non_modal_not_scanned", len(_f5) == 0)
    # role="dialog" (the slide-in panel marker) is a container too
    _f6, _bad6 = page_modal_field_violations(
        '<div role="dialog"><input id="drawer-field"></div>', css)
    ok("selftest_role_dialog_scanned", len(_f6) == 1 and _bad6 == ["input#drawer-field"])

    # THE SWEEP: every REGISTERED page, from disk (the same files the server serves;
    # section-stripping only ever REMOVES markup, so disk is the superset).
    total_fields = 0
    all_ids = set()
    # the shared "+ New project" form (static/js/new_project_modal.js) injects its own CSS + markup:
    # pages that load it get that CSS, and the form's own fields are scanned as a surface of their own
    _npm = root_dir / "static" / "js" / "new_project_modal.js"
    _npm_src = _npm.read_text(encoding="utf-8", errors="replace") if _npm.exists() else ""
    _m_css = re.search(r"var CSS = `(.*?)`;", _npm_src, re.S)
    _m_html = re.search(r"var HTML = `(.*?)`;", _npm_src, re.S)
    _npm_css = _m_css.group(1).replace("\\\\", "\\") if _m_css else ""
    _npm_html = _m_html.group(1).replace("\\\\", "\\") if _m_html else ""
    ok("shared_new_project_form_found", bool(_npm_css and _npm_html), "static/js/new_project_modal.js")
    if _npm_css and _npm_html:
        _f, _bad = page_modal_field_violations("<style>" + _npm_css + "</style>" + _npm_html, css)
        total_fields += len(_f)
        all_ids.update(f["id"] for f in _f if f["id"])
        ok("modal_fields_boxed_new_project_modal.js", not _bad and len(_f) >= 10,
           (f"{len(_f)} scanned; BOXLESS: " + ", ".join(_bad)) if _bad else f"{len(_f)} dialog field(s) scanned, all boxed")
    for fn in sorted(REGISTERED) + sorted(REGISTERED_PATHS):
        p = root_dir / fn
        if not p.exists():
            ok(f"modal_fields_{fn}", False, "registered page missing on disk")
            continue
        _text = p.read_text(encoding="utf-8", errors="replace")
        if "new_project_modal.js" in _text and _npm_css:
            _text += "<style>" + _npm_css + "</style>"   # the CSS this page loads with the shared form
        fields, bad = page_modal_field_violations(_text, css)
        total_fields += len(fields)
        all_ids.update(f["id"] for f in fields if f["id"])
        ok(f"modal_fields_boxed_{fn}", not bad,
           (f"{len(fields)} scanned; BOXLESS: " + ", ".join(bad)) if bad
           else f"{len(fields)} dialog field(s) scanned, all boxed")
    # sentinels: the #242 intake, #250 profile-edit and #275 Materials fields are
    # STILL COVERED by the structural scan (coverage can never silently shrink back
    # to an ID list) — and the scan sees a real population of fields.
    for sentinel in ("in-name", "pp-edit-name", "mt-log-qty", "mt-mat-name", "mt-tr-qty", "mt-ex-desc"):
        ok(f"structural_covers_{sentinel}", sentinel in all_ids)
    ok("structural_scan_population", total_fields >= 40, f"{total_fields} dialog fields scanned")

    # ---- #265 — SWAPPABLE CANONICAL LOGO: ONE asset slot, filled star; hollow star banned ----
    print("\n-- swappable canonical brand logo (asset slot, filled star, no hollow variant) --")
    CANON = "38.78,34.55"   # a distinctive vertex of the canonical faceted star (the asset)
    ASSET = "/files/static/brand/"   # the single swap slot every page references
    # the banned "rinky dink" signatures: the outline-star strokes + the hollow star path
    HOLLOW = ('fill="none" stroke="#B11E2E"', 'fill="none" stroke="#C8102E"', 'M12 2L15.09')
    # self-test: the matcher catches a hollow star and passes the canonical one
    _hollow = '<svg><path d="M12 2L15.09 10.26Z" fill="none" stroke="#B11E2E" stroke-width="1.5"/></svg>'
    _canon = '<svg><polygon fill="url(#topRL)" points="50,50 38.78,34.55 50,0"/></svg>'
    ok("selftest_hollow_star_caught",
       any(h in _hollow for h in HOLLOW) and CANON not in _hollow)
    ok("selftest_canonical_star_passes",
       CANON in _canon and not any(h in _canon for h in HOLLOW))
    # the swappable asset file IS the canonical mark (filled, no hollow, transparent bg)
    mark = (root_dir / "static" / "brand" / "mark.svg").read_text(encoding="utf-8", errors="replace")
    ok("logo_asset_is_canonical", CANON in mark and not any(h in mark for h in HOLLOW),
       "static/brand/mark.svg must be the canonical filled star")
    ok("logo_asset_transparent_bg", "<rect" not in mark, "logo asset must have a transparent background")
    # every logo-bearing live surface: references the ONE asset slot (swap = swap everywhere),
    # carries NO inline star + NO hollow, and reserves swap-safe space (object-fit:contain)
    LOGO_SURFACES = ["company-dashboard.html", "dashboard-static.html", "projects.html",
                     "client_portal.html", "login.html", "set_password.html", "admin_users.html",
                     "admin_projects.html", "admin_labor_rates.html", "dropplan.html",
                     "estimating.html",   # #276
                     "account.html"]      # Settings > Account

    # A swapped logo must NEVER shift surrounding layout. object-fit:contain alone is NOT
    # enough — the img also needs a RESERVED width+height box, else a bare <img> renders at
    # the SVG file's intrinsic size and a differently-proportioned replacement resizes the
    # slot (the #265-ADD swap-test caught exactly this on the sidebar + portal: a CSS rule
    # that sized the old inline <svg> stopped applying once it became an <img>).
    def _classes_with_reserved_box(text):
        """Logo classes the page gives BOTH a width and a height (a fixed box)."""
        have = set()
        for sel, body in re.findall(r'([^{}]+)\{([^{}]*)\}', text):
            if "width" in body and "height" in body:
                for cls in ("brand-mark", "mark"):
                    if re.search(r'\.' + cls + r'\b', sel):
                        have.add(cls)
        return have
    def _logo_img_boxed(tag, reserved):
        """A logo <img> reserves a box via inline width+height attrs OR a sized CSS class."""
        if re.search(r'\bwidth=', tag) and re.search(r'\bheight=', tag):
            return True
        m = re.search(r'class="([^"]*)"', tag)
        return any(c in reserved for c in (m.group(1).split() if m else []))

    for fn in LOGO_SURFACES:
        t = (root_dir / fn).read_text(encoding="utf-8", errors="replace")
        ok(f"logo_asset_ref_{fn}", ASSET in t, "page must reference the swappable brand asset slot")
        ok(f"logo_no_hollow_{fn}", not any(h in t for h in HOLLOW), "hollow/outline star present")
        ok(f"logo_no_inline_star_{fn}", CANON not in t, "logo must be the asset, not inline-duplicated")
        ok(f"logo_swapsafe_{fn}", "object-fit:contain" in t.replace(" ", ""),
           "logo container must be aspect-safe (object-fit:contain)")
        # reserved-box: every brand-asset <img> must have a fixed width+height (no swap-time shift)
        reserved = _classes_with_reserved_box(t)
        logo_imgs = [g for g in re.findall(r'<img\b[^>]*>', t) if "/files/static/brand/" in g]
        ok(f"logo_reserved_box_{fn}", bool(logo_imgs) and all(_logo_img_boxed(g, reserved) for g in logo_imgs),
           "every logo img must reserve a fixed width+height box (a swapped logo must not resize the slot)")
    # brand colourway tokens are :root (the colourway source) — not scattered-only
    wcss = requests.get(f"{BASE}/files/static/css/widgets.css", timeout=15).text
    ok("brand_red_is_root_token", "--brand-red:" in wcss, "--brand-red must be a :root token")
    # report/print renderers emit the canonical star via brand.star_svg (reads the asset) + no hollow
    RENDERERS = ["render_dcr_html.py", "render_drop_plan_html.py", "render_weekly_summary_html.py",
                 "render_closure_html.py", "render_meeting_minutes_html.py",
                 "render_lookahead_html.py", "render_toolbox_talk_html.py"]
    for fn in RENDERERS:
        t = (root_dir / fn).read_text(encoding="utf-8", errors="replace")
        ok(f"logo_renderer_{fn}", ("brand.star_svg" in t) and not any(h in t for h in HOLLOW),
           "renderer must emit brand.star_svg + carry no hollow star")

    # ---- #361 — density: type floor, one table header, title scale, dialog sizes,
    #      one primary colour, three breakpoints (project dashboard) ----
    ok("density_selftest",
       density_violations(".a{font-size:10px}") and not density_violations(".a{font-size:11px;text-transform:uppercase}")
       and not density_violations("@media print{.a{font-size:9px}}") and density_violations('<i style="font-size:9px">'),
       "the floor scanner catches a 10px body rule / 9px inline style and exempts labels + print")
    dv = density_violations(html)
    ok("density_type_floor", not dv, f"{len(dv)} below the floor: {dv[:4]}")
    th = re.search(r"(?m)^\s*th\s*\{([^{}]*)\}", inline)                 # the bare element rule
    ok("density_th_light", bool(th) and "var(--fill" in th.group(1) and "var(--ink-2" in th.group(1) and "--ink," not in th.group(1),
       "the one table header is light fill + ink-2 text")
    dark_th = [m.group(0)[:80] for m in re.finditer(r"[^{}]*(?<![-\w.#])th(?![-\w])[^{}]*\{[^{}]*(background[^;]*--ink\b|color:\s*(#fff\b|white))[^{}]*\}", inline)]
    dark_th += re.findall(r"<th[^>]*style=\"[^\"]*(?:background|color:\s*(?:#fff|white))[^\"]*\"", html)
    ok("density_th_no_overrides", not dark_th, f"module header overrides: {dark_th[:3]}")
    titles = {".ph-head h1": 20, ".dcr-head h1": 20, ".dp-h1": 20, ".pay-h1": 20, ".pc-head h1": 20, ".section h2": 20,
              ".pay-mhead h3": 18, ".pc-mh h3": 18, ".jx-card h2": 15}
    bad_t = []
    for sel, want in titles.items():
        m = re.search(r"(?:^|[\s}])" + re.escape(sel) + r"\s*\{([^{}]*)\}", inline)
        fs = m and re.search(r"font-size:\s*([\d.]+)px", m.group(1))
        if not fs or float(fs.group(1)) != want:
            bad_t.append(sel)
    ok("density_title_scale", not bad_t, f"off the 20/18/15 scale: {bad_t}")
    modals = [".dr-sheet", ".pk-modal", ".pd-modal", ".la-modal", ".dcrg-panel", ".mt-modal", ".pay-modal", ".aia-modal", ".fp-modal", ".pc-modal"]
    bad_m = [sel for sel in modals if not re.search(r"(?:^|[\s}])" + re.escape(sel) + r"\s*\{[^{}]*max-width:\s*var\(--modal-(sm|md|lg|xl)\)", inline)]
    ok("density_modal_sizes", not bad_m and "--modal-md: 640px" in inline, f"dialogs off the four sizes: {bad_m}")
    bad_p = [sel for sel in (".dcr-primary", ".pk-btn.primary", ".pay-btn.primary")
             if not re.search(re.escape(sel) + r"\s*\{[^{}]*background:\s*var\(--accent", inline)]
    ok("density_one_primary", not bad_p, f"primary buttons not on --accent: {bad_p}")
    skip = _print_spans(inline)
    bps = {m.group(1) for m in re.finditer(r"@media[^{]*max-width:\s*(\d+)px", inline) if not any(a <= m.start() < b for a, b in skip)}
    ok("density_breakpoints", bps <= {"640", "860", "1024"}, f"breakpoints in use: {sorted(bps)}")

    # ---- #362 — per-tab clean-up: a ceiling on inline styles, one header per tab,
    #      no fixed inline grids, no inline brand-red primary buttons ----
    src = (Path(__file__).resolve().parents[1] / "dashboard-static.html").read_text(encoding="utf-8")
    n_inline = len(re.findall(r'style=\\?["\']', src))
    INLINE_CEILING = 393   # #362: 531 -> 393. Lower it as tabs are cleaned; never raise it.
    ok("tabs_inline_ceiling", n_inline <= INLINE_CEILING, f"{n_inline} inline styles (ceiling {INLINE_CEILING})")
    heads = ("tab-head", "jx-head", "dcr-head", "exp-head", "pd-head", "pay-head", "fp-head", "dp-head", "mt-head", "ec-head", "pc-head", "la-top")
    sections = list(re.finditer(r'<section class="view(?: active)?" data-view="([^"]+)"', html))
    no_head = []
    for i, m in enumerate(sections):
        body = html[m.end(): sections[i + 1].start() if i + 1 < len(sections) else len(html)]
        if m.group(1) != "drawings" and not any(f'class="{h}' in body or f' {h}"' in body or f'class="{h} ' in body for h in heads):
            no_head.append(m.group(1))
    ok("tabs_every_tab_has_header", not no_head, f"tabs without a header: {no_head}")
    fixed = [g for g in re.findall(r'style=\\?["\'][^"\']*grid-template-columns:\s*(repeat\(\s*[3-9][^)]*\))', src)]
    ok("tabs_no_fixed_inline_grids", not fixed, f"fixed inline grids override the responsive rules: {fixed}")
    red = re.findall(r'<(?:button|a)\b[^>]*style="[^"]*background:\s*var\(--brand-red[^"]*color:\s*(?:white|#fff)', src)
    ok("tabs_no_inline_red_primary", not red, f"{len(red)} inline red primary buttons")

    print(f"\n== RESULT: {len(PASS)} PASS / {len(FAIL)} FAIL ==")
    if FAIL:
        print("FAILURES: " + ", ".join(FAIL))
    print("OVERALL:", "PASS" if not FAIL else "FAIL")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
