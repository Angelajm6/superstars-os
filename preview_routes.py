"""
preview_routes.py — Browser preview blueprint.

Clean /preview/* URLs for viewing every document/card template directly in the
browser. PURE HTML, no PDF generation. This is the DESIGN surface.

WeasyPrint (render_pdf.py + the production generators) is the EXPORT surface —
only invoked at batch/delivery time. Iterate the HTML here, then export.

Workflow:
  1. Open http://localhost:5050/preview/ in browser
  2. Click any document → renders live HTML
  3. Edit the template, refresh, see the change
  4. Ctrl+P → Save as PDF for ad-hoc one-offs
  5. Run render_pdf.py only when you need the production WeasyPrint output

To register in server.py, add after `app = Flask(...)`:
    from preview_routes import preview_bp
    app.register_blueprint(preview_bp)
"""

from flask import Blueprint, abort, send_file
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

preview_bp = Blueprint('preview', __name__, url_prefix='/preview')


# #305 — the preview blueprint serves rendered project documents AND, via
# _serve_from_root, any root .html (the company console included, UNSTRIPPED).
# Company roles only, and the root serve is limited to generated report names.
_PREVIEW_ROLES = frozenset({"admin", "c_suite"})
_ROOT_PREVIEW_PREFIXES = ("DCR-", "WPS-", "LA-", "DP-", "WS-", "MM-", "RFI-", "TBT-", "CLOSURE-")


@preview_bp.before_request
def _preview_gate():
    from auth import current_user
    from flask import jsonify
    user = current_user()
    if not user or user.get("role") not in _PREVIEW_ROLES:
        return jsonify({"error": "forbidden"}), 403


# ===================== INDEX =====================

@preview_bp.route('/')
@preview_bp.route('')
def index():
    """Listing page of every preview available."""
    rfis = sorted(SCRIPT_DIR.glob('RFI-*.html'))
    dcrs_internal = sorted(SCRIPT_DIR.glob('DCR-*-internal.html'))
    dcrs_client = sorted(SCRIPT_DIR.glob('DCR-*-client.html'))
    weeklies_internal = sorted(SCRIPT_DIR.glob('WPS-*-internal.html'))
    weeklies_client = sorted(SCRIPT_DIR.glob('WPS-*-client.html'))
    lookaheads = sorted(SCRIPT_DIR.glob('LA-*.html'))

    closures = []
    if (SCRIPT_DIR / 'site_closures').exists():
        closures = sorted((SCRIPT_DIR / 'site_closures').glob('Closure-*.html'))

    meetings = []
    if (SCRIPT_DIR / 'meetings').exists():
        meetings = sorted((SCRIPT_DIR / 'meetings').glob('M-*.html'))

    toolbox = []
    if (SCRIPT_DIR / 'toolbox_talks').exists():
        toolbox = sorted((SCRIPT_DIR / 'toolbox_talks').glob('TBT-*.html'))

    drops = []
    if (SCRIPT_DIR / 'drop_plans').exists():
        drops = sorted((SCRIPT_DIR / 'drop_plans').glob('DP-*.html'))

    def section(label, items, route_prefix):
        if not items:
            return (
                f'<section><h3>{label}</h3>'
                f'<p class="empty">(none generated yet)</p></section>'
            )
        links = ''.join(
            f'<li><a href="/preview/{route_prefix}/{item.stem}">{item.stem}</a></li>'
            for item in items
        )
        return f'<section><h3>{label}</h3><ul>{links}</ul></section>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Preview · Superstars Ops</title>
<style>
  body {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    max-width: 920px; margin: 2em auto; padding: 0 1em;
    color: #14161C; background: #FFFFFF;
  }}
  h1 {{ border-bottom: 2px solid #B11E2E; padding-bottom: 0.3em; margin-bottom: 0.2em; }}
  h1 .pill {{
    display: inline-block; background: #14161C; color: white;
    font-size: 0.45em; padding: 3px 10px; border-radius: 10px;
    margin-left: 0.6em; vertical-align: middle; letter-spacing: 0.05em;
    text-transform: uppercase; font-weight: 600;
  }}
  h3 {{ color: #B11E2E; margin-top: 0; margin-bottom: 0.4em; font-size: 1.05em; }}
  section {{
    background: #FAF7F1; padding: 0.7em 1.1em; border-radius: 6px;
    margin-bottom: 0.6em;
  }}
  ul {{ margin: 0; padding-left: 1.4em; columns: 2; column-gap: 1.5em; }}
  li {{ margin: 0.25em 0; break-inside: avoid; }}
  a {{ color: #14161C; text-decoration: none; border-bottom: 1px dashed #B11E2E; }}
  a:hover {{ background: #FFEEEE; }}
  .empty {{ color: #888; margin: 0; font-size: 0.9em; }}
  .note {{
    background: #FFF8E1; border-left: 4px solid #B11E2E;
    padding: 0.9em 1.1em; margin: 1.2em 0;
    font-size: 0.92em; border-radius: 0 4px 4px 0;
  }}
  .note strong {{ color: #B11E2E; }}
  kbd {{
    background: #14161C; color: white; padding: 1px 6px;
    border-radius: 3px; font-size: 0.85em; font-family: monospace;
  }}
  code {{
    background: #ECE9E2; padding: 1px 5px; border-radius: 3px;
    font-size: 0.9em;
  }}
  .cards a {{ font-weight: 600; }}
</style>
</head>
<body>
<h1>Document Preview <span class="pill">design surface</span></h1>
<div class="note">
  <strong>HTML-first workflow.</strong>
  Click any document below to view in the browser. Iterate the HTML template,
  refresh, repeat. For an ad-hoc PDF press <kbd>Ctrl+P</kbd> → "Save as PDF".
  Only run <code>render_pdf.py</code> when you need the production WeasyPrint output.
</div>

<section class="cards"><h3>Card Templates</h3>
<ul>
  <li><a href="/preview/cof/single">CoF — single card (front + back, 2 pages)</a></li>
  <li><a href="/preview/cof/4up">CoF — 4-up letter sheet (2 pages, 4 cards each)</a></li>
  <li><a href="/preview/cof/screen">CoF — screen-only version</a></li>
  <li><a href="/preview/rigger-foreman/4up">Rigger Foreman Designation — 4-up</a></li>
</ul>
</section>

{section('Daily Construction Reports — Internal', dcrs_internal, 'dcr')}
{section('Daily Construction Reports — Client', dcrs_client, 'dcr')}
{section('Weekly Progress Summaries — Internal', weeklies_internal, 'weekly')}
{section('Weekly Progress Summaries — Client', weeklies_client, 'weekly')}
{section('Lookahead Schedules', lookaheads, 'lookahead')}
{section('RFIs', rfis, 'rfi')}
{section('Site Closures', closures, 'closure')}
{section('Meeting Minutes', meetings, 'meeting')}
{section('Toolbox Talks', toolbox, 'toolbox')}
{section('Drop Plans', drops, 'drop-plan')}

</body></html>"""


# ===================== CARD PREVIEWS =====================

@preview_bp.route('/cof/single')
def cof_single():
    return send_file(str(SCRIPT_DIR / 'cof_card_print.html'))


@preview_bp.route('/cof/4up')
def cof_4up():
    return send_file(str(SCRIPT_DIR / 'cof_card_print_4up.html'))


@preview_bp.route('/cof/screen')
def cof_screen():
    return send_file(str(SCRIPT_DIR / 'cof_card.html'))


@preview_bp.route('/rigger-foreman/4up')
def rigger_foreman_4up():
    return send_file(str(SCRIPT_DIR / 'rigger_foreman_designation_4up.html'))


# ===================== DOCUMENT PREVIEWS =====================

def _serve_from_root(filename):
    """Serve an HTML file from the outputs root, with path-traversal protection."""
    if not filename.endswith('.html'):
        filename = filename + '.html'
    # #305 — only generated report files, never app pages (company-dashboard etc.)
    if '/' in filename or '\\' in filename or not filename.upper().startswith(_ROOT_PREVIEW_PREFIXES):
        abort(404, description=f"Not found: {filename}")
    target = SCRIPT_DIR / filename
    try:
        target.resolve().relative_to(SCRIPT_DIR.resolve())
    except ValueError:
        abort(403)
    if not target.exists():
        abort(404, description=f"Not found: {filename}")
    return send_file(str(target))


def _serve_from_subfolder(subfolder, filename):
    """Serve an HTML file from a known subfolder, with traversal protection."""
    if not filename.endswith('.html'):
        filename = filename + '.html'
    folder = SCRIPT_DIR / subfolder
    target = folder / filename
    try:
        target.resolve().relative_to(folder.resolve())
    except ValueError:
        abort(403)
    if not target.exists():
        abort(404, description=f"Not found: {subfolder}/{filename}")
    return send_file(str(target))


def _latest(pattern, folder=None):
    base = folder if folder is not None else SCRIPT_DIR
    if not base.exists():
        abort(404, description=f"Folder does not exist: {base}")
    files = sorted(base.glob(pattern))
    if not files:
        abort(404, description=f"No files matching {pattern}")
    return send_file(str(files[-1]))


@preview_bp.route('/dcr/latest')
def dcr_latest():
    return _latest('DCR-*-internal.html')


@preview_bp.route('/dcr/<filename>')
def dcr(filename):
    return _serve_from_root(filename)


@preview_bp.route('/weekly/latest')
def weekly_latest():
    return _latest('WPS-*-internal.html')


@preview_bp.route('/weekly/<filename>')
def weekly(filename):
    return _serve_from_root(filename)


@preview_bp.route('/lookahead/latest')
def lookahead_latest():
    return _latest('LA-*.html')


@preview_bp.route('/lookahead/<filename>')
def lookahead(filename):
    return _serve_from_root(filename)


@preview_bp.route('/rfi/<rfi_id>')
def rfi(rfi_id):
    """Accepts 'RFI-001', '001', or '1'."""
    if not rfi_id.startswith('RFI-'):
        try:
            rfi_id = f'RFI-{int(rfi_id):03d}'
        except ValueError:
            pass
    return _serve_from_root(rfi_id)


@preview_bp.route('/closure/<filename>')
def closure(filename):
    return _serve_from_subfolder('site_closures', filename)


@preview_bp.route('/meeting/<meeting_id>')
def meeting(meeting_id):
    """Accepts 'M-018', '018', or '18'."""
    if not meeting_id.startswith('M-'):
        try:
            meeting_id = f'M-{int(meeting_id):03d}'
        except ValueError:
            pass
    return _serve_from_subfolder('meetings', meeting_id)


@preview_bp.route('/toolbox/<tbt_id>')
def toolbox_talk(tbt_id):
    return _serve_from_subfolder('toolbox_talks', tbt_id)


@preview_bp.route('/drop-plan/<dp_id>')
def drop_plan(dp_id):
    """Accepts 'DP-001', '001', or '1'."""
    if not dp_id.startswith('DP-'):
        try:
            dp_id = f'DP-{int(dp_id):03d}'
        except ValueError:
            pass
    return _serve_from_subfolder('drop_plans', dp_id)
