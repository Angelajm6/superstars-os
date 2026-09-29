#!/usr/bin/env python3
"""Aggregate the DCR JSON for (project_code, report_date) from SQLite.

This is the DB-driven replacement for the Excel-based generate_dcr.py.
Both the GET /api/projects/<code>/daily/<date> route and the CLI wrapper
dcr_from_db.py call into aggregate_dcr() — no internal HTTP roundtrip.

Returns a dict matching render_dcr_html.py's JSON input contract.
Caller is responsible for serializing to JSON or feeding to the renderer.
"""
import sqlite3
from datetime import datetime, date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "superstars.db"

# Default coords for FR-BX-001 (890 E 135th St, Bronx). Used when weather_log is
# absent and we need to fetch live data. Future: pull from a projects.lat/lng
# column once added.
DEFAULT_LAT = 40.8083
DEFAULT_LNG = -73.9162


def _db():
    # #260 — route through the env-driven layer (SSC_DB_URL); production default = live SQLite.
    import db_layer
    return db_layer.connect()


def _day_of_week(date_str):
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    return days[datetime.strptime(date_str, '%Y-%m-%d').date().weekday()]


def _compute_hours(time_in, time_out):
    """Hours WORKED (30-minute lunch deducted) — unified with the Weekly Hours
    Log so the DCR labor section and payroll's grid can never drift apart.
    "Worked" not "paid": pay-or-not is a separate downstream decision.
    Returns None when either time is missing (the DCR renders '—' for an
    in-progress shift); compute_worked_hours returns 0.0 for the same case,
    but the DCR semantics want a visual blank, not a zero."""
    from payroll_hours import compute_worked_hours
    if not time_in or not time_out:
        return None
    return compute_worked_hours(time_in, time_out)


def _weather_from_log(row):
    """Shape a weather_log row into the renderer's am/pm/wind block."""
    return {
        'am': {'temp_f': row.get('am_temp_f'),
               'conditions': row.get('am_conditions') or row.get('conditions')},
        'pm': {'temp_f': row.get('pm_temp_f'),
               'conditions': row.get('pm_conditions') or row.get('conditions')},
        'wind': row.get('wind'),
        'source': 'weather_log',
    }


def _weather_from_live(date_str):
    """Call Open-Meteo via the server helper and reshape to am/pm/wind block."""
    from server import fetch_open_meteo_weather
    live = fetch_open_meteo_weather(DEFAULT_LAT, DEFAULT_LNG, date_str)
    wind_str = None
    if live.get('wind_mph') is not None:
        wind_str = f"{live['wind_mph']} mph"
        if live.get('wind_dir'):
            wind_str += f" {live['wind_dir']}"
    return {
        'am': {'temp_f': live.get('temp_min'), 'conditions': live.get('condition_label')},
        'pm': {'temp_f': live.get('temp_max'), 'conditions': live.get('condition_label')},
        'wind': wind_str,
        'source': live.get('source', 'open-meteo'),
    }


def aggregate_dcr(project_code, date_str, audience='internal'):
    """Build the full DCR JSON for (project_code, date) at the given audience.

    Returns a dict matching render_dcr_html.py's JSON input contract.
    Raises ValueError on bad inputs (date format, audience).
    Raises KeyError if project_code is unknown.
    """
    if audience not in ('internal', 'client'):
        raise ValueError(f"audience must be 'internal' or 'client', got '{audience}'")
    datetime.strptime(date_str, '%Y-%m-%d')

    conn = _db()
    proj_row = conn.execute(
        "SELECT * FROM projects WHERE project_code = ?", (project_code,)
    ).fetchone()
    if not proj_row:
        conn.close()
        raise KeyError(f"Unknown project_code: {project_code}")
    proj = dict(proj_row)
    try:   # #357 — the live people (primary site supervisor / assigned PM / client org)
        import project_contacts
        _people = project_contacts.project_people(conn, project_code)
    except Exception:
        _people = {}

    # DCR-1: build a deduped one-line address. If projects.address already
    # contains city_zip (e.g. older rows store the full address on the
    # street-line field), don't append it a second time. Match is case-
    # insensitive substring so a "Bronx, NY 10454" / "BRONX, NY 10454"
    # mismatch still de-dupes. Also collapses any "X, X" residue.
    addr = (proj.get('address') or '').strip()
    czip = (proj.get('city_zip') or '').strip()
    if addr and czip and czip.lower() in addr.lower():
        address = addr
    else:
        address = ', '.join([p for p in [addr, czip] if p])

    warnings = []
    target = datetime.strptime(date_str, '%Y-%m-%d').date()
    if target > date.today():
        warnings.append(f"VAL-002: report_date {date_str} is in the future")

    project_block = {
        'code': proj.get('project_code'),
        'name': proj.get('name'),
        'address': address,
        'date': date_str,
        'day_of_week': _day_of_week(date_str),
        'superintendent': _people.get('superintendent') or proj.get('superintendent'),
        'project_manager': _people.get('project_manager'),
        'owner_client': _people.get('owner_client') or proj.get('client'),
        'status': proj.get('status'),
    }

    # Labor — auto from sign_in_log JOIN employees. worker_id (W-####) is
    # pulled so the DCR labor section can render "W-#### — Name" alongside
    # the trade, matching the workforce list and Hours Log conventions.
    sign_ins = conn.execute(
        """SELECT s.*, e.name AS emp_name, e.trade AS emp_trade,
                  e.worker_id AS emp_worker_id
           FROM sign_in_log s
           LEFT JOIN employees e ON s.employee_id = e.employee_id
           WHERE s.project_code = ? AND s.date = ?
           ORDER BY s.time_in, s.id""",
        (project_code, date_str)
    ).fetchall()
    labor_rows_full = []
    total_hours = 0.0
    for i, r in enumerate(sign_ins, 1):
        r = dict(r)
        emp_id = r.get('employee_id')
        if emp_id and not r.get('emp_name'):
            # Don't surface the orphan employee_id — the operator only
            # sees worker_ids (W-####), and this row's id didn't resolve
            # to one. Row number is enough to locate the bad sign_in.
            warnings.append(f"VAL-005: sign_in row {i}: unknown worker reference")
        hours = _compute_hours(r.get('time_in'), r.get('time_out'))
        if hours is not None:
            if not (0 <= hours <= 24):
                warnings.append(f"VAL-004: sign_in row {i}: hours {hours} out of range 0..24")
            total_hours += hours
        if r.get('time_in') and r.get('time_out') and r['time_out'] < r['time_in']:
            warnings.append(f"VAL-003: sign_in row {i}: time_out {r['time_out']} before time_in {r['time_in']}")
        labor_rows_full.append({
            'n': i, 'employee_id': emp_id, 'worker_id': r.get('emp_worker_id'),
            'name': r.get('emp_name'),
            'company': 'Superstars Contracting', 'trade': r.get('emp_trade'),
            'time_in': r.get('time_in'), 'time_out': r.get('time_out'),
            'hours': hours, 'area': None, 'notes': None,
            # sign_in_log.id — surfaced so client-side flows (Cancel
            # rollback, No-Work Day clear-and-mark) can target specific
            # rows for delete without scraping the listing endpoint.
            'sign_in_id': r.get('id'),
        })
    # Sort by Worker ID ascending so the DCR sign-in roster is consistent
    # everywhere it renders (entry view + rendered/printed DCR). Per
    # CLAUDE.md schema rule, sort the trailing digits NUMERICALLY — don't
    # rely on the W-#### zero-padding holding up under future format
    # changes. Rows with no worker_id (orphan sign-ins, already
    # warning-tagged above) sort to the end. After sorting, re-number `n`
    # so the visible row index matches the sorted order.
    def _wid_sort_key(row):
        wid = row.get('worker_id')
        if not wid:
            return (1, 0)
        digits = ''.join(ch for ch in str(wid) if ch.isdigit())
        try:
            return (0, int(digits)) if digits else (1, 0)
        except ValueError:
            return (1, 0)
    labor_rows_full.sort(key=_wid_sort_key)
    for idx, row in enumerate(labor_rows_full, 1):
        row['n'] = idx
    headcount = len(labor_rows_full)
    total_hours = round(total_hours, 2)

    # work_log → work_performed
    work_rows = conn.execute(
        "SELECT * FROM work_log WHERE project_code = ? AND date = ?",
        (project_code, date_str)
    ).fetchall()
    work_performed = []
    for r in work_rows:
        r = dict(r)
        work_performed.append({
            'trade_area': r.get('trade_area') or r.get('trades_working'),
            'location_elevation': r.get('location_elevation'),
            'description': r.get('description') or r.get('scope_of_work'),
        })

    # deliveries → materials_deliveries
    delivery_rows = conn.execute(
        "SELECT * FROM deliveries WHERE project_code = ? AND date = ?",
        (project_code, date_str)
    ).fetchall()
    materials_deliveries = []
    for r in delivery_rows:
        r = dict(r)
        materials_deliveries.append({
            'time': r.get('time'),
            'material': r.get('material') or r.get('description'),
            'qty': r.get('qty'),
            'unit': r.get('unit'),
            'supplier': r.get('supplier') or r.get('delivered_by'),
            'notes': r.get('notes'),
        })

    # equipment_log → equipment
    equipment_rows = conn.execute(
        "SELECT * FROM equipment_log WHERE project_code = ? AND date = ?",
        (project_code, date_str)
    ).fetchall()
    equipment = []
    for r in equipment_rows:
        r = dict(r)
        equipment.append({
            'equipment': r.get('equipment_type'),
            'equipment_id': r.get('equipment_id'),
            'owner': r.get('owner'),
            'hours_used': r.get('hours_used'),
            'issues': r.get('issues') or r.get('notes'),
        })

    # inspections
    insp_rows = conn.execute(
        "SELECT * FROM inspections WHERE project_code = ? AND date = ?",
        (project_code, date_str)
    ).fetchall()
    inspections = []
    for r in insp_rows:
        r = dict(r)
        inspections.append({
            'type': r.get('type') or r.get('scope'),
            'inspector': r.get('inspector_name'),
            'agency': r.get('agency'),
            'area': r.get('area'),
            'result': r.get('result'),
            'notes': r.get('notes'),
        })

    # photos
    photo_rows = conn.execute(
        "SELECT * FROM photos WHERE project_code = ? AND date = ? ORDER BY id",
        (project_code, date_str)
    ).fetchall()
    photos = []
    for r in photo_rows:
        r = dict(r)
        photos.append({
            # DCR-4: expose photos.id so the entry-view edit flow can
            # build a working Remove button (DELETE /api/photos/<id>).
            # Renderer + downstream consumers ignore extra keys.
            'id': r.get('id'),
            'filename': r.get('filename'),
            'url': r.get('url'),
            'location': r.get('location'),
            'description': r.get('description'),
            'uploaded_by': r.get('uploaded_by'),
        })

    # Toolbox talk + safety events
    tb_row = conn.execute(
        "SELECT * FROM toolbox_talk_records WHERE project_code = ? AND date = ? LIMIT 1",
        (project_code, date_str)
    ).fetchone()
    toolbox_talk = None
    if tb_row:
        tb = dict(tb_row)
        topic = None
        if tb.get('talk_id'):
            lib = conn.execute(
                "SELECT title FROM toolbox_talk_library WHERE talk_id = ?",
                (tb['talk_id'],)
            ).fetchone()
            if lib:
                topic = dict(lib).get('title')
        toolbox_talk = {
            'conducted': True,
            'topic': topic,
            'conducted_by': tb.get('facilitator'),
        }

    event_rows = conn.execute(
        "SELECT * FROM safety_events WHERE project_code = ? AND date = ? ORDER BY time, id",
        (project_code, date_str)
    ).fetchall()
    safety_events_raw = [dict(r) for r in event_rows]

    # issues
    issue_rows = conn.execute(
        "SELECT * FROM issues WHERE project_code = ? AND date = ?",
        (project_code, date_str)
    ).fetchall()
    issues_full = [{
        'category': r['category'],
        'description': r['description'],
        'time_lost_hrs': r['time_lost_hrs'],
        'action': r['action'],
        'owner': r['owner'],
    } for r in (dict(x) for x in issue_rows)]

    # visitors
    visitor_rows = conn.execute(
        "SELECT * FROM visitors WHERE project_code = ? AND date = ? ORDER BY time_in, id",
        (project_code, date_str)
    ).fetchall()
    visitors_full = [{
        'name': r['name'],
        'company': r['company'],
        'role': r['role'],
        'time_in': r['time_in'],
        'time_out': r['time_out'],
        'purpose': r['purpose'],
        'accompanied_by': r['accompanied_by'],
        'notes': r['notes'],
    } for r in (dict(x) for x in visitor_rows)]

    # weather: prefer weather_log row, fall back to live Open-Meteo
    weather_row = conn.execute(
        "SELECT * FROM weather_log WHERE project_code = ? AND date = ? LIMIT 1",
        (project_code, date_str)
    ).fetchone()
    if weather_row:
        weather_block = _weather_from_log(dict(weather_row))
    else:
        try:
            weather_block = _weather_from_live(date_str)
        except Exception as e:
            warnings.append(f"VAL-006: weather data unavailable ({e})")
            weather_block = {'am': None, 'pm': None, 'wind': None, 'source': 'unavailable'}

    if weather_block.get('source') == 'unavailable':
        # already warned
        pass
    elif weather_block.get('am') is None and weather_block.get('pm') is None:
        warnings.append(f"VAL-006: weather data missing for {date_str}")

    # Pull No-Work Day designation while the connection is still open.
    # Renderers / Labor Rate / Weekly read dcr['no_work'].
    nw_row = conn.execute(
        "SELECT no_work, no_work_reason, no_work_note FROM report_index "
        "WHERE project_code = ? AND report_date = ? AND report_type='DCR' "
        "AND no_work = 1 LIMIT 1",
        (project_code, date_str),
    ).fetchone()
    no_work_flag = 0
    no_work_reason = None
    no_work_note = None
    if nw_row:
        no_work_flag = 1
        no_work_reason = nw_row['no_work_reason']
        no_work_note = nw_row['no_work_note']

    conn.close()

    dcr = {
        'report_id': None,
        'audience': audience,
        'project': project_block,
        'weather': weather_block,
        'work_performed': work_performed,
        'materials_deliveries': materials_deliveries,
        'equipment': equipment,
        'inspections': inspections,
        'photos': photos,
        'signoff': {
            'superintendent_name': _people.get('superintendent') or proj.get('superintendent'),
            'pm_name': _people.get('project_manager'),
            'date': date_str,
            'time_signed': None,
        },
        'metadata': {
            'generated_at': datetime.utcnow().isoformat() + 'Z',
            'source': 'sqlite',
            'warnings': warnings,
            'redactions_applied': audience == 'client',
        },
    }

    if audience == 'client':
        by_trade = {}
        for r in labor_rows_full:
            trade = (r.get('trade') or 'Unknown').split(' / ')[0]
            d = by_trade.setdefault(trade, {'count': 0, 'hours': 0.0})
            d['count'] += 1
            if r.get('hours'):
                d['hours'] += r['hours']
        dcr['labor'] = {
            'rows': [],
            'summary': {
                'by_trade': sorted(
                    [{'trade': t, 'count': v['count'], 'hours': round(v['hours'], 2)}
                     for t, v in by_trade.items()],
                    key=lambda x: x['trade']
                ),
                'headcount': headcount,
                'total_hours': total_hours,
                'site_open': headcount > 0,
                'site_closed': headcount == 0,
            },
            'headcount': headcount,
            'total_hours': total_hours,
        }
        events_client = []
        for e in safety_events_raw:
            summary = (e.get('description') or '')[:80] or (e.get('event_type') or '')
            events_client.append({'type': e.get('event_type'), 'summary': summary})
        dcr['safety'] = {
            'toolbox_talk': ({'conducted': toolbox_talk['conducted'], 'topic': toolbox_talk['topic']}
                             if toolbox_talk else None),
            'events': events_client,
        }
        issues_client = []
        for i in issues_full:
            summary = (i.get('description') or '')[:80] or (i.get('category') or '')
            issues_client.append({'category': i.get('category'), 'summary': summary})
        dcr['issues_delays'] = issues_client
        # Visitors: count by role only, no names or purposes
        by_role = {}
        for v in visitors_full:
            role = (v.get('role') or 'Unspecified').strip() or 'Unspecified'
            by_role[role] = by_role.get(role, 0) + 1
        dcr['visitors'] = {
            'count_by_role': sorted(
                [{'role': r, 'count': c} for r, c in by_role.items()],
                key=lambda x: x['role']
            ),
            'total_visits': len(visitors_full),
        }
    else:
        dcr['labor'] = {
            'rows': labor_rows_full,
            'headcount': headcount,
            'total_hours': total_hours,
        }
        dcr['safety'] = {
            'toolbox_talk': toolbox_talk,
            'events': [{
                'type': e.get('event_type'),
                'time': e.get('time'),
                'person': e.get('person'),
                'description': e.get('description'),
                'action': e.get('action'),
            } for e in safety_events_raw],
        }
        dcr['issues_delays'] = issues_full
        dcr['visitors'] = visitors_full

    # Surface the No-Work designation pulled above. Renderers read this
    # flag to draw the prominent NO-WORK banner + auto-fill the
    # weather/work-stoppage line with the reason. Empty labor/work
    # sections are valid when no_work=1.
    dcr['no_work'] = no_work_flag
    dcr['no_work_reason'] = no_work_reason
    dcr['no_work_note'] = no_work_note

    return dcr
