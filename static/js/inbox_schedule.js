/* Inbox · Mine / All (prototype accepted 2026-10-16, inbox_schedule.py).
 *
 * SSC_SCHEDULE.mount(el, { scope: 'mine' | 'all', admin: bool, next: '/company#inbox/mine', onOpenItem: fn })
 *   Mine  this person's own Google Calendar events (private, lock pill) + what is assigned to them,
 *         grouped by day, with a Reconnect banner when the Google sign-in expired.
 *   All   (admin / C-suite; the SERVER refuses other roles with 403) every assigned task / event /
 *         meeting with the assignee, filtered by assignee, type and dates.
 * Used by the company portal's Inbox (#inbox/mine, #inbox/all) and by /inbox for PMs and supers.
 * Dates are LOCAL (never toISOString). */
(function () {
  'use strict';
  var CSS = '' +
    '.isx-bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 12px}' +
    '.isx-bar .grow{flex:1}' +
    '.isx-chip{border:1px solid var(--line,#eceef3);background:var(--card,#fff);border-radius:var(--r-pill,999px);padding:5px 12px;cursor:pointer;color:var(--muted,#8c92a0);font:inherit;font-size:13px}' +
    '.isx-chip[aria-pressed="true"]{border-color:var(--accent-edge,rgba(67,100,220,.3));background:var(--accent-soft,#ebf0fe);color:var(--accent-ink,#3a5fd0);font-weight:600}' +
    '.isx-sel{border:1px solid var(--line,#eceef3);background:var(--card,#fff);border-radius:8px;padding:6px 10px;font:inherit;font-size:13px;color:var(--ink,#222633);max-width:100%}' +
    '.isx-date{width:120px}' +
    '.isx-sync{font-size:12.5px;color:var(--muted,#8c92a0);display:inline-flex;align-items:center;gap:6px}' +
    '.isx-sync i{width:7px;height:7px;border-radius:50%;background:#15a07e;display:inline-block}' +
    '.isx-sync.bad i{background:#e9685a}.isx-sync.off i{background:#cabfad}' +
    '.isx-note{font-size:12.5px;color:var(--muted,#8c92a0);display:flex;gap:6px;align-items:center;margin:0 0 14px}' +
    '.isx-note svg,.isx-pill svg{width:12px;height:12px;flex:none}' +
    '.isx-banner{display:flex;gap:12px;align-items:center;flex-wrap:wrap;background:#fff6e5;border:1px solid #f0d39a;color:#7a5410;padding:10px 14px;border-radius:var(--r-inner,10px);margin:0 0 14px}' +
    '.isx-banner p{margin:0;flex:1;min-width:200px;font-size:13.5px}' +
    '.isx-day{margin:0 0 18px}' +
    '.isx-day h3{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted,#8c92a0);margin:0 0 8px;font-weight:600}' +
    '.isx-day h3 b{color:var(--accent-ink,#3a5fd0);margin-right:6px}' +
    '.isx-list{background:var(--card,#fff);border:1px solid var(--line,#eceef3);border-radius:var(--r-card,14px);overflow:hidden}' +
    '.isx-row{display:grid;grid-template-columns:92px minmax(0,1fr) auto;gap:12px;padding:11px 14px;align-items:start;border-top:1px solid var(--line,#eceef3);cursor:pointer;background:none;width:100%;text-align:left;font:inherit;color:inherit;border-left:0;border-right:0;border-bottom:0}' +
    '.isx-row:first-child{border-top:0}.isx-row:hover{background:var(--canvas,#fafbfd)}' +
    '.isx-time{font-variant-numeric:tabular-nums;font-weight:600;font-size:13px;line-height:1.35}' +
    '.isx-time small{display:block;font-weight:400;color:var(--muted,#8c92a0);font-size:11.5px}' +
    '.isx-t{font-weight:600;line-height:1.35;overflow-wrap:anywhere}.isx-t.done{text-decoration:line-through;color:var(--muted,#8c92a0)}' +
    '.isx-m{color:var(--muted,#8c92a0);font-size:12.5px;margin-top:2px;display:flex;flex-wrap:wrap;gap:4px 10px}' +
    '.isx-tz{color:#7a5410;background:#fff6e5;border-radius:5px;padding:0 5px}' +
    '.isx-over{color:#c94b3d;font-weight:600}' +
    '.isx-pill{font-size:11.5px;font-weight:600;border-radius:var(--r-tag,6px);padding:2px 8px;white-space:nowrap;display:inline-flex;align-items:center;gap:4px}' +
    '.isx-pill.calendar{background:#eeeefa;color:#5b5f9e}.isx-pill.task{background:#e3f4f4;color:#0f7a7a}' +
    '.isx-pill.event{background:var(--accent-soft,#ebf0fe);color:var(--accent-ink,#3a5fd0)}.isx-pill.meeting{background:#f1ebfc;color:#6a3fc0}' +
    '.isx-join{display:inline-flex;align-items:center;gap:5px;color:#0b8043;font-weight:600;text-decoration:none}.isx-join:hover{text-decoration:underline}' +
    '.isx-ans{display:flex;align-items:center;gap:7px;white-space:normal}.isx-ans i{width:8px;height:8px;border-radius:50%;flex:none;display:inline-block}' +
    '.isx-who{display:inline-flex;align-items:center;gap:6px;font-weight:500;color:var(--ink,#222633)}' +
    '.isx-av{width:22px;height:22px;border-radius:50%;display:inline-grid;place-items:center;font-size:10px;font-weight:700;color:#fff;flex:none}' +
    '.isx-empty{padding:28px 16px;text-align:center;color:var(--muted,#8c92a0)}' +
    '.isx-forbid{background:var(--card,#fff);border:1px solid var(--line,#eceef3);border-radius:var(--r-card,14px);padding:28px 20px;text-align:center}' +
    '.isx-ov{position:fixed;inset:0;background:rgba(15,18,30,.35);display:flex;justify-content:flex-end;z-index:1000}' +
    '.isx-pane{width:min(420px,100%);background:var(--card,#fff);height:100%;padding:20px;overflow:auto;box-shadow:-8px 0 30px rgba(0,0,0,.15)}' +
    '.isx-pane h2{font-size:18px;margin:12px 0 6px;overflow-wrap:anywhere}' +
    '.isx-kv{display:grid;grid-template-columns:104px minmax(0,1fr);gap:8px 12px;margin:16px 0;font-size:13.5px}.isx-kv dt{color:var(--muted,#8c92a0)}.isx-kv dd{margin:0;overflow-wrap:anywhere;white-space:pre-line}' +
    '.isx-pnote{background:var(--canvas,#fafbfd);border:1px solid var(--line,#eceef3);border-radius:var(--r-inner,10px);padding:10px 12px;font-size:12.5px;color:var(--muted,#8c92a0);margin-top:14px}' +
    '.isx-acts{display:flex;gap:8px;flex-wrap:wrap}' +
    '@media (max-width:720px){.isx-row{grid-template-columns:minmax(0,1fr) auto}.isx-time{grid-column:1/-1;display:flex;gap:8px;align-items:baseline}.isx-time small{display:inline}}';
  var LOCK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>';
  var MEET = '<svg viewBox="0 0 24 24" aria-hidden="true" width="15" height="15"><rect x="2" y="6" width="13" height="12" rx="2.5" fill="#00832d"/><path d="M15 10.5l5.2-3.4c.5-.3 1.1 0 1.1.6v8.6c0 .6-.6.9-1.1.6L15 13.5z" fill="#00ac47"/></svg>';
  var ANSWER = { accepted: ['Yes', '#15a07e'], declined: ['No', '#e9685a'], tentative: ['Maybe', '#a07d12'], needsAction: ['Waiting', '#8c92a0'] };
  var KIND = { task: 'Task', event: 'Event', meeting: 'Meeting', calendar: 'My calendar' };
  var COLORS = ['#4364dc', '#0f8a8a', '#7a4bd1', '#c26a1b', '#3d8b3d', '#b5476b', '#5b6b8c', '#a07d12'];
  var DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var ZONES = { 'America/Chicago': 'Chicago', 'America/Denver': 'Denver', 'America/Los_Angeles': 'Los Angeles', 'America/Phoenix': 'Phoenix',
    'Europe/London': 'London', 'Europe/Madrid': 'Madrid', 'Europe/Paris': 'Paris', 'UTC': 'UTC' };

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function ymd(d) { return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
  function addDays(iso, n) { var p = iso.split('-'); var d = new Date(+p[0], +p[1] - 1, +p[2]); d.setDate(d.getDate() + n); return ymd(d); }
  function today() { return ymd(new Date()); }
  function dayLabel(iso) { var p = iso.split('-'); var d = new Date(+p[0], +p[1] - 1, +p[2]); return DOW[d.getDay()] + ', ' + MON[d.getMonth()] + ' ' + d.getDate(); }
  function t12(hm) { if (!hm) return ''; var p = hm.split(':'), h = +p[0]; return ((h % 12) || 12) + ':' + p[1] + ' ' + (h < 12 ? 'AM' : 'PM'); }
  function zoneName(z) { return ZONES[z] || String(z || '').split('/').pop().replace(/_/g, ' '); }
  function initials(n) { return String(n || '?').split(/\s+/).filter(Boolean).slice(0, 2).map(function (x) { return x[0].toUpperCase(); }).join(''); }
  function color(id) { return COLORS[Math.abs(+id || 0) % COLORS.length]; }
  function av(id, name) { return '<span class="isx-av" style="background:' + color(id) + '" aria-hidden="true">' + esc(initials(name)) + '</span>'; }
  function dp() { return window.SSCDatePicker; }
  function getISO(el) { var p = dp(); if (p && p.getISO) { var v = p.getISO(el); if (v) return v; } return el.value || ''; }
  function setISO(el, iso) { var p = dp(); if (p && p.setISO) { try { p.setISO(el, iso || ''); return; } catch (e) {} } el.value = iso || ''; }

  function injectCss() {
    if (document.getElementById('isx-css')) return;
    var s = document.createElement('style'); s.id = 'isx-css'; s.textContent = CSS; document.head.appendChild(s);
  }

  async function get(url) {
    var res = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    var j = await res.json().catch(function () { return {}; });
    if (res.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname + location.hash); throw new Error('unauthenticated'); }
    if (!res.ok) { var e = new Error((j && j.error) || ('Request failed (' + res.status + ')')); e.status = res.status; throw e; }
    return j.data;
  }
  async function post(url, body) {
    var res = await fetch(url, { method: 'POST', credentials: 'same-origin', headers: { Accept: 'application/json', 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    var j = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error((j && j.error) || ('Request failed (' + res.status + ')'));
    return j.data;
  }

  // An item's day + time cell. Items carry calendar_from (task deadline / event start).
  function whenOf(r) {
    if (r.kind === 'calendar') return { d: r.start_date, t: r.start_time ? t12(r.start_time) : 'All day', end: r.end_time ? t12(r.end_time) : '' };
    if (r.kind === 'task') return { d: r.calendar_from, t: r.due_time ? t12(r.due_time) : 'Any time', end: '' };
    return { d: r.calendar_from, t: r.start_time ? t12(r.start_time) : 'All day', end: r.end_time ? t12(r.end_time) : '' };
  }
  function sortKey(r) { var w = whenOf(r); return (w.d || '9999') + ' ' + (r.kind === 'calendar' ? (r.start_time || '00:00') : (r.kind === 'task' ? (r.due_time || '99:99') : (r.start_time || '00:00'))); }

  function mount(el, opts) {
    injectCss();
    opts = opts || {};
    var S = { scope: opts.scope === 'all' ? 'all' : 'mine', mineType: 'all', who: '', type: '', range: 'week', from: today(), to: addDays(today(), 6), rows: [], people: [], me: null };
    var drawer = null;

    function rangeNow() {
      if (S.scope === 'mine') return { from: today(), to: addDays(today(), 13) };
      if (S.range === 'today') return { from: today(), to: today() };
      if (S.range === 'week') return { from: today(), to: addDays(today(), 6) };
      return { from: S.from, to: S.to < S.from ? S.from : S.to };
    }

    async function load() {
      var r = rangeNow();
      var q = '/api/inbox/schedule?scope=' + S.scope + '&from=' + r.from + '&to=' + r.to;
      if (S.scope === 'mine' && S.mineType === 'calendar') q += '&type=calendar';
      if (S.scope === 'all') { if (S.who) q += '&assignee=' + encodeURIComponent(S.who); if (S.type) q += '&type=' + S.type; }
      el.innerHTML = '<div class="isx-empty">Loading…</div>';
      var data;
      try { data = await get(q); }
      catch (e) {
        if (e.status === 403) { el.innerHTML = '<div class="isx-forbid"><p><b>Only admins and C-suite can see the whole team\'s work.</b></p><p class="isx-note" style="justify-content:center">Your own calendar and assignments are under Mine.</p></div>'; return; }
        el.innerHTML = '<div class="isx-empty">Could not load this. ' + esc(e.message) + '</div>'; return;
      }
      var rows = (data.items || []).slice();
      if (S.scope === 'mine') rows = S.mineType === 'assigned' ? rows : S.mineType === 'calendar' ? (data.calendar || []) : rows.concat(data.calendar || []);
      rows.sort(function (a, b) { return sortKey(a) < sortKey(b) ? -1 : sortKey(a) > sortKey(b) ? 1 : 0; });
      S.rows = rows; S.data = data;
      if (data.people) S.people = data.people;
      render();
    }

    function toolbar() {
      if (S.scope === 'mine') {
        var cs = S.data.calendar_status || {};
        var sync = !cs.available ? '<span class="isx-sync off"><i></i>Google Calendar is not switched on yet</span>'
          : !cs.connected ? '<span class="isx-sync off"><i></i>Google Calendar not connected</span>'
          : cs.needs_reconnect ? '<span class="isx-sync bad"><i></i>Google Calendar: reconnect needed</span>'
          : !cs.reads ? '<span class="isx-sync bad"><i></i>Google Calendar: reconnect to read your calendar</span>'
          : '<span class="isx-sync"><i></i>Google Calendar synced' + (cs.synced_at ? ' ' + esc(ago(cs.synced_at)) : '') + '</span>';
        return '<div class="isx-bar">' + [['all', 'Everything'], ['assigned', 'Assigned to me'], ['calendar', 'My calendar']].map(function (x) {
          return '<button type="button" class="isx-chip" data-mt="' + x[0] + '" aria-pressed="' + (S.mineType === x[0]) + '">' + x[1] + '</button>';
        }).join('') + '<span class="grow"></span>' + sync + '</div>' +
          '<p class="isx-note">' + LOCK + 'Events from your Google Calendar are private. Nobody else, including admins, can see them.</p>';
      }
      return '<div class="isx-bar">' +
        '<select class="isx-sel" id="isx-who" aria-label="Assignee"><option value="">Everyone</option>' + S.people.map(function (p) { return '<option value="' + p.id + '"' + (String(S.who) === String(p.id) ? ' selected' : '') + '>' + esc(p.name) + '</option>'; }).join('') + '</select>' +
        '<select class="isx-sel" id="isx-type" aria-label="Type"><option value="">All types</option>' + ['task', 'event', 'meeting'].map(function (k) { return '<option value="' + k + '"' + (S.type === k ? ' selected' : '') + '>' + KIND[k] + 's</option>'; }).join('') + '</select>' +
        [['today', 'Today'], ['week', 'This week'], ['custom', 'Pick dates']].map(function (x) { return '<button type="button" class="isx-chip" data-rg="' + x[0] + '" aria-pressed="' + (S.range === x[0]) + '">' + x[1] + '</button>'; }).join('') +
        (S.range === 'custom' ? '<input class="isx-sel isx-date" id="isx-from" type="date" aria-label="From"><input class="isx-sel isx-date" id="isx-to" type="date" aria-label="To">' : '') +
        '<span class="grow"></span><span class="isx-sync">' + S.rows.length + ' item' + (S.rows.length === 1 ? '' : 's') + '</span></div>' +
        '<p class="isx-note">' + LOCK + 'Tasks, events and meetings assigned to anyone on the team. People\'s own Google Calendar events are never shown here.</p>';
    }

    function banner() {
      if (S.scope !== 'mine') return '';
      var cs = S.data.calendar_status || {}, next = encodeURIComponent(opts.next || (location.pathname + location.hash));
      if (cs.available && cs.connected && cs.needs_reconnect)
        return '<div class="isx-banner" role="status"><p><b>Google Calendar needs you to sign in again.</b> Your calendar stopped updating' + (cs.synced_at ? ' ' + esc(ago(cs.synced_at)) : '') + '. Your assigned work below is still current.</p><a class="btn-primary btn-sm" href="/google-calendar/connect?next=' + next + '">Reconnect</a></div>';
      if (cs.available && cs.connected && !cs.reads)
        return '<div class="isx-banner" role="status"><p><b>Reconnect Google Calendar once</b> so your own events show here (only you see them).</p><a class="btn-primary btn-sm" href="/google-calendar/connect?next=' + next + '">Reconnect</a></div>';
      if (cs.available && !cs.connected)
        return '<div class="isx-banner" role="status"><p><b>See your own calendar here.</b> Connect Google Calendar; your events stay private to you.</p><a class="btn-primary btn-sm" href="/google-calendar/connect?next=' + next + '">Connect Google Calendar</a></div>';
      return '';
    }

    function rowHtml(r, i) {
      var w = whenOf(r), meta = [];
      var place = r.kind === 'calendar' ? r.location : (r.location || r.address);
      if (place) meta.push(esc(place));
      if (r.project_code) meta.push(esc(r.project_code));
      if (r.kind === 'calendar' && r.source_tz && r.source_start_time) meta.push('<span class="isx-tz">Set in ' + esc(zoneName(r.source_tz)) + ' time: ' + esc(t12(r.source_start_time)) + '</span>');
      if (S.scope === 'all') meta.unshift('<span class="isx-who">' + (r.assignee_user_id ? av(r.assignee_user_id, r.assignee_name) + esc(r.assignee_name) : 'Nobody yet') + '</span>');
      if (r.kind === 'meeting' && (r.attendee_names || []).length) {
        var others = S.scope === 'mine' ? (r.attendee_names || []).filter(function (_, k) { return r.attendee_ids[k] !== S.me; }).concat(r.assignee_user_id && r.assignee_user_id !== S.me ? [r.assignee_name] : []) : r.attendee_names;
        others = others.filter(function (n, k, a) { return n && a.indexOf(n) === k; });
        if (others.length) meta.push('With ' + esc(others.join(', ')));
      }
      if (r.overdue) meta.push('<span class="isx-over">Overdue</span>');
      if (r.meet_url) meta.push('<a class="isx-join" href="' + esc(r.meet_url) + '" target="_blank" rel="noopener">' + MEET + 'Join video</a>');
      var pill = r.kind === 'calendar' ? '<span class="isx-pill calendar">' + LOCK + 'My calendar</span>' : '<span class="isx-pill ' + r.kind + '">' + KIND[r.kind] + '</span>';
      return '<div role="button" tabindex="0" class="isx-row" data-i="' + i + '"><div class="isx-time">' + esc(w.t) + (w.end ? '<small>to ' + esc(w.end) + '</small>' : '') + '</div>' +
        '<div><div class="isx-t' + (r.status === 'done' ? ' done' : '') + '">' + esc(r.title) + '</div>' + (meta.length ? '<div class="isx-m">' + meta.join('<span aria-hidden="true">·</span>') + '</div>' : '') + '</div>' + pill + '</div>';
    }

    function render() {
      var html = banner() + toolbar();
      if (!S.rows.length) html += '<div class="isx-list"><div class="isx-empty">' + (S.scope === 'all' ? 'Nothing assigned matches these filters.' : 'Nothing for you in the next two weeks.') + '</div></div>';
      var groups = {}, order = [], t = today();
      S.rows.forEach(function (r, i) { var d = whenOf(r).d || 'none'; if (r.overdue) d = 'overdue'; if (!groups[d]) { groups[d] = []; order.push(d); } groups[d].push(rowHtml(r, i)); });
      order.sort(function (a, b) { return a === 'overdue' ? -1 : b === 'overdue' ? 1 : a === 'none' ? 1 : b === 'none' ? -1 : a < b ? -1 : 1; });
      order.forEach(function (d) {
        var h = d === 'overdue' ? 'Overdue' : d === 'none' ? 'No date' : (d === t ? '<b>Today</b>' : '') + esc(dayLabel(d));
        html += '<section class="isx-day"><h3>' + h + '</h3><div class="isx-list">' + groups[d].join('') + '</div></section>';
      });
      el.innerHTML = html;
      if (S.range === 'custom' && S.scope === 'all') {
        var f = el.querySelector('#isx-from'), to = el.querySelector('#isx-to');
        if (dp() && dp().wire) { try { dp().wire(f, {}); dp().wire(to, {}); } catch (e) {} }
        setISO(f, S.from); setISO(to, S.to);
        [f, to].forEach(function (x) { x.addEventListener('change', function () { var a = getISO(f), b = getISO(to); if (a) S.from = a; if (b) S.to = b; if (S.to < S.from) S.to = S.from; load(); }); });
      }
    }

    function ago(iso) {
      var then = new Date(String(iso).replace(' ', 'T')), m = Math.round((Date.now() - then.getTime()) / 60000);
      if (!isFinite(m)) return '';
      if (m < 1) return 'just now';
      if (m < 60) return m + ' min ago';
      if (m < 60 * 24) return Math.round(m / 60) + ' h ago';
      return 'on ' + dayLabel(ymd(then));
    }

    function openRow(r) {
      if (r.kind !== 'calendar' && r.can_edit && typeof opts.onOpenItem === 'function') { opts.onOpenItem(r); return; }
      var w = whenOf(r), kv = [];
      var whenTxt = w.d ? dayLabel(w.d) + ', ' + w.t + (w.end ? ' to ' + w.end : '') : 'No date yet';
      if (r.kind === 'calendar' && r.end_date && r.end_date !== r.start_date) whenTxt += ' (until ' + dayLabel(r.end_date) + ')';
      kv.push(['When', esc(whenTxt) + (w.d ? ' <span class="isx-sync">(New York time)</span>' : '')]);
      if (r.source_tz && r.source_start_time) kv.push(['Organizer\'s time', esc(t12(r.source_start_time) + ' ' + zoneName(r.source_tz))]);
      if (r.kind === 'calendar' ? r.location : (r.location || r.address)) kv.push(['Where', esc(r.kind === 'calendar' ? r.location : [r.location, r.address].filter(Boolean).join(' · ')) + (r.directions_url ? ' <a href="' + esc(r.directions_url) + '" target="_blank" rel="noopener">Directions ↗</a>' : '')]);
      if (r.project_name || r.project_code) kv.push(['Job', esc(r.project_name || r.project_code)]);
      if (r.kind !== 'calendar') kv.push(['Assigned to', r.assignee_user_id ? '<span class="isx-who">' + av(r.assignee_user_id, r.assignee_name) + esc(r.assignee_name) + '</span>' : 'Nobody yet']);
      if ((r.attendee_names || []).length) kv.push(['People in it', esc(r.attendee_names.join(', '))]);
      if ((r.guest_emails || []).length) kv.push(['Guests', esc(r.guest_emails.join(', '))]);
      if (r.meet_url) kv.push(['Video', '<a class="isx-join" href="' + esc(r.meet_url) + '" target="_blank" rel="noopener">' + MEET + 'Join with Google Meet</a>']);
      var ans = r.guest_answers || {}, ak = Object.keys(ans);
      if (ak.length) kv.push(['Answers', ak.map(function (e) { var a = ANSWER[ans[e]] || ANSWER.needsAction; return '<span class="isx-ans"><i style="background:' + a[1] + '"></i>' + esc(e) + ' · ' + a[0] + '</span>'; }).join('')]);
      if (r.detail) kv.push(['Details', esc(r.detail)]);
      var acts = '';
      if (r.kind === 'task' && r.can_complete) acts = '<div class="isx-acts"><button type="button" class="btn-primary" data-done="' + (r.status === 'done' ? '0' : '1') + '">' + (r.status === 'done' ? 'Reopen' : 'Mark done') + '</button></div>';
      var note = r.kind === 'calendar'
        ? 'From your Google Calendar. Only you can see it. Edit it in Google; changes show here within a minute, and deleting it there removes it here.' + (r.html_link ? ' <a href="' + esc(r.html_link) + '" target="_blank" rel="noopener">Open in Google Calendar ↗</a>' : '')
        : 'Team item. It is also on the team calendar' + (r.assignee_user_id ? ' and in the Superstars Google calendar of the people on it' : '') + '.';
      closeDrawer();
      drawer = document.createElement('div');
      drawer.className = 'isx-ov';
      drawer.innerHTML = '<div class="isx-pane" role="dialog" aria-modal="true" aria-labelledby="isx-h"><button type="button" class="btn-ghost btn-sm" data-close>Close</button>' +
        '<h2 id="isx-h">' + esc(r.title) + '</h2>' + (r.kind === 'calendar' ? '<span class="isx-pill calendar">' + LOCK + 'My calendar</span>' : '<span class="isx-pill ' + r.kind + '">' + KIND[r.kind] + '</span>') +
        '<dl class="isx-kv">' + kv.map(function (x) { return '<dt>' + x[0] + '</dt><dd>' + x[1] + '</dd>'; }).join('') + '</dl>' + acts + '<p class="isx-pnote">' + note + '</p></div>';
      document.body.appendChild(drawer);
      drawer.addEventListener('click', async function (e) {
        if (e.target === drawer || e.target.closest('[data-close]')) { closeDrawer(); return; }
        var b = e.target.closest('[data-done]');
        if (b) {
          b.disabled = true;
          try { await post('/api/team/items/' + r.id + '/done', { done: b.getAttribute('data-done') === '1' }); closeDrawer(); load(); }
          catch (ex) { b.disabled = false; b.insertAdjacentHTML('afterend', '<span class="isx-over" style="margin-left:8px">' + esc(ex.message) + '</span>'); }
        }
      });
      var c = drawer.querySelector('[data-close]'); if (c) c.focus();
    }
    function closeDrawer() { if (drawer) { drawer.remove(); drawer = null; } }
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeDrawer(); });
    el.addEventListener('keydown', function (e) { var b = e.target.closest && e.target.closest('.isx-row[data-i]'); if (b && e.target === b && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openRow(S.rows[+b.getAttribute('data-i')]); } });

    el.addEventListener('click', function (e) {
      var b = e.target.closest('[data-mt]'); if (b) { S.mineType = b.getAttribute('data-mt'); load(); return; }
      b = e.target.closest('[data-rg]'); if (b) { S.range = b.getAttribute('data-rg'); load(); return; }
      if (e.target.closest('a')) return;   // Join video opens Meet, not the details
      b = e.target.closest('.isx-row[data-i]'); if (b) openRow(S.rows[+b.getAttribute('data-i')]);
    });
    el.addEventListener('change', function (e) {
      if (e.target.id === 'isx-who') { S.who = e.target.value; load(); }
      if (e.target.id === 'isx-type') { S.type = e.target.value; load(); }
    });
    fetch('/api/auth/me', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (j) { S.me = j && j.user && j.user.id; }).catch(function () {}).then(load);
    return { reload: load, setScope: function (s) { S.scope = s === 'all' ? 'all' : 'mine'; load(); } };
  }

  window.SSC_SCHEDULE = { mount: mount };
})();
