/* Tasks, events and meetings: the one View card and New / Edit window every page uses
 * (accepted prototype 2026-11-19: https://claude.ai/artifact/KnHuD7Ys632eNf2MCtNpmU).
 *
 *   SSC_TASKS.view(item | id)        what a click on any task opens: when, who it's for, the job, Directions,
 *                                    details, Activity; Mark done / Edit when this person may
 *   SSC_TASKS.edit(item | null, preset)   New / Edit. preset: {date, title, due_time, assignees, project_code, kind}
 *   SSC_TASKS.quick(form)            the "Add a task…" line: a day, a time and @names are picked up; Enter adds
 *   SSC_TASKS.toast(msg, undoFn)
 * Every write fires `ssc:items-changed` on document, so each page refreshes its own lists.
 * The server decides everything (team_items.py): who may give a task to whom (admin / C-suite: anyone; a PM or
 * super: themselves and the people on their jobs), who may change it (whoever added it, or admin / C-suite), who
 * may tick it (the people it is for, whoever added it, admin / C-suite). This file only mirrors it for display.
 * Dates are LOCAL (never toISOString); every date field goes through SSCDatePicker. */
(function () {
  'use strict';
  if (window.SSC_TASKS) return;
  var V = '1';
  var DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'], DOWL = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var MONL = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  var EMAIL = /^[^@\s<>(),;:"]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/;
  var COLORS = ['#4B5F94', '#3F7F76', '#9A6A30', '#7B68B5', '#8E4A6A', '#3F78A3', '#6B7D3E', '#A9524F'];
  var MEET = '<svg class="tt-meet-ic" viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="6" width="13" height="12" rx="2.5" fill="#00832d"/><path d="M15 10.5l5.2-3.4c.5-.3 1.1 0 1.1.6v8.6c0 .6-.6.9-1.1.6L15 13.5z" fill="#00ac47"/><rect x="2" y="6" width="5" height="6" rx="1" fill="#0066da"/><rect x="2" y="12" width="5" height="6" rx="1" fill="#2684fc"/></svg>';
  function svg(d) { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + '</svg>'; }
  var IC = {
    x: svg('<path d="M6 6l12 12M18 6 6 18"/>'),
    cal: svg('<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>'),
    clock: svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
    person: svg('<circle cx="12" cy="8" r="4"/><path d="M4.5 21a7.5 7.5 0 0 1 15 0"/>'),
    group: svg('<circle cx="9" cy="8" r="4"/><path d="M2 21a7 7 0 0 1 14 0M17 4a4 4 0 0 1 0 8M22 21a7 7 0 0 0-4-6.3"/>'),
    build: svg('<path d="M4 21V6l8-3 8 3v15"/><path d="M9 21v-5h6v5M9 9h.01M15 9h.01"/>'),
    pin: svg('<path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>'),
    mail: svg('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>'),
    bell: svg('<path d="M6 9a6 6 0 1 1 12 0c0 6 2.5 7.5 2.5 7.5h-17S6 15 6 9"/><path d="M10 20a2.2 2.2 0 0 0 4 0"/>'),
    check: svg('<path d="M20 6 9 17l-5-5"/>')
  };

  // ------------------------------------------------------------------ small helpers
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function $q(root, sel) { return root.querySelector(sel); }
  function ymd(d) { return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
  function today() { return ymd(new Date()); }
  function plus(n) { var d = new Date(); d.setHours(12, 0, 0, 0); d.setDate(d.getDate() + n); return ymd(d); }
  function pISO(s) { var p = String(s).split('-').map(Number); return new Date(p[0], p[1] - 1, p[2]); }
  function fmtDate(s) { var d = pISO(s); return DOW[d.getDay()] + ', ' + MON[d.getMonth()] + ' ' + d.getDate(); }
  function dayWord(s) { return s === today() ? 'today' : s === plus(1) ? 'tomorrow' : s === plus(-1) ? 'yesterday' : fmtDate(s); }
  function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }
  function fmtTime(t) { if (!t) return ''; var p = t.split(':').map(Number), h = p[0], m = p[1]; return (h % 12 || 12) + (m ? ':' + String(m).padStart(2, '0') : '') + (h < 12 ? ' AM' : ' PM'); }
  function dated(it) { return it.kind === 'event' || it.kind === 'meeting'; }
  function when(it) {
    if (dated(it)) {
      var t = it.start_time ? ' · ' + fmtTime(it.start_time) + (it.end_time ? '–' + fmtTime(it.end_time) : '') : '';
      return cap(dayWord(it.start_date)) + (it.end_date && it.end_date !== it.start_date ? ' → ' + fmtDate(it.end_date) : '') + t;
    }
    return it.due_date ? (it.overdue ? 'Was due ' : 'Due ') + dayWord(it.due_date) + (it.due_time ? ' · ' + fmtTime(it.due_time) : '') : 'No deadline';
  }
  function color(id) { return COLORS[Math.abs(Number(id) || 0) % COLORS.length]; }
  function ini(n) { var p = String(n || '?').trim().split(/\s+/); return ((p[0] || '?')[0] + (p.length > 1 ? p[p.length - 1][0] : '')).toUpperCase(); }
  function ava(id, name) { return '<span class="tt-ava" style="background:' + color(id) + '" aria-hidden="true">' + esc(ini(name)) + '</span>'; }
  function first(n) { return String(n || '').trim().split(/\s+/)[0] || 'Someone'; }
  function names(list, me) {   // "You, Luis and Javier"
    var n = list.map(function (p) { return p.id === me ? 'You' : first(p.name); });
    if (n.length > 3) return n.slice(0, 2).join(', ') + ' + ' + (n.length - 2) + ' more';
    return n.length > 1 ? n.slice(0, -1).join(', ') + ' and ' + n[n.length - 1] : (n[0] || '');
  }
  function peopleOf(it) { return (it.assignee_ids || []).map(function (id, k) { return { id: id, name: (it.assignee_names || [])[k] }; }); }
  function stamp(iso) {
    if (!iso) return '';
    var d = new Date(String(iso).replace(' ', 'T')); if (!isFinite(d)) return '';
    var day = ymd(d), t = fmtTime(String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'));
    return (day === today() ? '' : cap(dayWord(day)) + ', ') + t;
  }
  function api(path, opts) {
    return fetch('/api' + path, Object.assign({ credentials: 'same-origin', headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' } }, opts || {}))
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) { var e = new Error((j && j.error) || ('Something went wrong (' + r.status + ')')); e.status = r.status; throw e; }
          return j;
        });
      });
  }
  function post(path, body, method) { return api(path, { method: method || 'POST', body: JSON.stringify(body || {}) }); }
  function changed() { try { document.dispatchEvent(new CustomEvent('ssc:items-changed')); } catch (e) { /* old browser */ } }
  function css() {
    if (document.getElementById('tt-css')) return;
    var l = document.createElement('link'); l.id = 'tt-css'; l.rel = 'stylesheet'; l.href = '/files/static/css/team_tasks.css?v=' + V;
    document.head.appendChild(l);
  }
  function picker() {   // the shared date picker; loaded here on a page that does not have it
    if (window.SSCDatePicker || document.querySelector('script[src*="datepicker.js"]')) return;
    var s = document.createElement('script'); s.src = '/files/static/js/datepicker.js?v=1007'; document.head.appendChild(s);
  }
  function dpGet(el) { return window.SSCDatePicker ? window.SSCDatePicker.getISO(el) : (el.value || ''); }
  function dpSet(el, v) { if (window.SSCDatePicker) window.SSCDatePicker.setISO(el, v || ''); else el.value = v || ''; }

  // who is signed in + the team + their jobs (GET /api/team/form), once per page
  var FORM = null;
  function form(fresh) {
    if (!FORM || fresh) FORM = api('/team/form').then(function (j) { return j.data; }).catch(function (e) { FORM = null; throw e; });
    return FORM;
  }

  // ------------------------------------------------------------------ toast with Undo
  var toastEl, toastT;
  function toast(msg, undo) {
    css();
    if (!toastEl) { toastEl = document.createElement('div'); toastEl.className = 'tt-toast'; toastEl.setAttribute('role', 'status'); document.body.appendChild(toastEl); }
    clearTimeout(toastT);
    toastEl.innerHTML = '<span>' + esc(msg) + '</span>' + (undo ? '<button type="button">Undo</button>' : '');
    toastEl.hidden = false;
    if (undo) toastEl.querySelector('button').onclick = function () { toastEl.hidden = true; undo(); };
    toastT = setTimeout(function () { toastEl.hidden = true; }, undo ? 7000 : 4000);
  }

  // ------------------------------------------------------------------ the sheet (a window on desktop, from the bottom on a phone)
  var open = null;
  function sheet(html, label) {
    css(); close();
    var ov = document.createElement('div');
    ov.className = 'tt-ov';
    ov.innerHTML = '<div class="tt-sheet" role="dialog" aria-modal="true" aria-labelledby="' + label + '"><div class="tt-grab" aria-hidden="true"></div>' + html + '</div>';
    ov.addEventListener('mousedown', function (e) { if (e.target === ov) close(); });
    document.body.appendChild(ov);
    document.documentElement.classList.add('tt-lock');
    open = ov;
    return ov;
  }
  function close() {
    if (open) { open.remove(); open = null; }
    document.documentElement.classList.remove('tt-lock');
  }
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || !open) return;
    var l = open.querySelector('.tt-list:not([hidden])');
    if (l) { l.hidden = true; return; }
    var dp = document.querySelector('.ssc-dp-popup');
    if (dp && dp.offsetParent !== null) return;   // the date picker closes first
    close();
  });

  // ------------------------------------------------------------------ VIEW: what every click on a task opens
  function view(itOrId) {
    var id = typeof itOrId === 'object' && itOrId ? itOrId.id : itOrId;
    if (!id) return;
    css();
    Promise.all([api('/team/items/' + encodeURIComponent(id)), form().catch(function () { return {}; })]).then(function (res) {
      drawView(res[0].data, res[1] || {});
      if (res[0].data.new) post('/team/items/' + id + '/seen').then(changed).catch(function () { });
      else post('/team/items/' + id + '/seen').catch(function () { });
    }).catch(function (e) { toast(e.status === 404 ? 'That task was deleted.' : e.message); });
  }
  function drawView(it, f) {
    var me = f.me, ppl = peopleOf(it);
    var kindL = { task: 'Task', event: 'Event', meeting: 'Meeting' }[it.kind] || 'Task';
    var tags = '<span class="tt-tag">' + kindL + '</span>' + (it.status === 'done' ? '<span class="tt-tag ok">Done</span>' : it.overdue ? '<span class="tt-tag bad">Overdue</span>' : '') +
      (it.new ? '<span class="tt-tag new">New for you</span>' : '') + (it.source === 'gcal' ? '<span class="tt-tag">From Google Calendar</span>' : '');
    var whenSub = !dated(it) && it.due_date ? (function () { var d = pISO(it.due_date); return DOWL[d.getDay()] + ', ' + MONL[d.getMonth()] + ' ' + d.getDate(); })() : '';
    var facts = [[dated(it) ? IC.cal : IC.clock, 'When', esc(when(it)) + (whenSub ? '<small>' + esc(whenSub) + '</small>' : '')]];
    var added = it.created_by_name ? 'Added by ' + esc(it.created_by_name) + (it.created_by === me ? ' (you)' : '') : '';
    facts.push([IC.person, it.kind === 'meeting' ? 'Running it' : 'For', ppl.length
      ? '<div class="tt-whos">' + ppl.map(function (p) { return '<span class="tt-who">' + ava(p.id, p.name) + esc(p.name) + (p.id === me ? ' (you)' : '') + '</span>'; }).join('') + '</div>' +
        '<small>' + (ppl.length > 1 && it.kind === 'task' ? 'Any of them can mark it done. ' : '') + added + '</small>'
      : 'Nobody yet' + (added ? '<small>' + added + '</small>' : '')]);
    if ((it.attendee_names || []).length) facts.push([IC.group, 'People in it', esc(it.attendee_names.join(', '))]);
    if ((it.guest_emails || []).length) facts.push([IC.mail, 'Guests', esc(it.guest_emails.join(', '))]);
    if (it.meet_url) facts.push([IC.group, 'Video', '<a class="tt-join" href="' + esc(it.meet_url) + '" target="_blank" rel="noopener">' + MEET + 'Join with Google Meet</a>']);
    if (it.project_code) facts.push([IC.build, 'Job', '<a href="/projects/' + encodeURIComponent(it.project_code) + '">' + esc(it.project_code) + (it.project_name ? ' · ' + esc(it.project_name) : '') + ' →</a>']);
    else if (it.org_name) facts.push([IC.build, 'Client', esc(it.org_name)]);
    if (it.address || it.location) facts.push([IC.pin, 'Address', esc([it.location, it.address].filter(Boolean).join(' · ')) +
      (it.directions_url ? '<div class="tt-dir"><a class="btn-ghost btn-sm" href="' + esc(it.directions_url) + '" target="_blank" rel="noopener">Directions in Google Maps ↗</a></div>' : '')]);
    var canDone = it.kind === 'task' && it.can_complete && it.status !== 'cancelled';
    var ov = sheet(
      '<div class="tt-head"><div class="tt-head-t"><div class="tt-tags">' + tags + '</div><h2 id="tsk-v-h">' + esc(it.title) + '</h2></div>' +
      '<button type="button" class="tt-x" data-tt="close" aria-label="Close">' + IC.x + '</button></div>' +
      '<div class="tt-body">' +
      '<div class="tt-facts">' + facts.map(function (x) { return '<div class="tt-fact">' + x[0] + '<div><div class="l">' + x[1] + '</div><div class="v">' + x[2] + '</div></div></div>'; }).join('') + '</div>' +
      (it.detail ? '<div><div class="tt-sec">Details</div><div class="tt-detail">' + esc(it.detail) + '</div></div>' : '') +
      ((it.activity || []).length ? '<div><div class="tt-sec">Activity</div><ul class="tt-log">' + it.activity.map(function (a) { return '<li><i></i><span>' + esc(a.text) + ' <span class="tt-muted">· ' + esc(stamp(a.at)) + '</span></span></li>'; }).join('') + '</ul></div>' : '') +
      '</div><div class="tt-foot">' +
      (it.can_edit && it.source !== 'gcal' ? '<button type="button" class="btn-ghost" data-tt="edit">Edit</button>' : '<span class="tt-hint">' + (it.source === 'gcal' ? 'Change it in Google Calendar.' : esc(first(it.created_by_name || 'Admin')) + ' or an admin can change it.') + '</span>') +
      '<span class="tt-sp"></span>' +
      (canDone ? '<button type="button" class="btn-primary" data-tt="done">' + (it.status === 'done' ? 'Reopen' : 'Mark done') + '</button>' : '<button type="button" class="btn-ghost" data-tt="close">Close</button>') +
      '</div>', 'tsk-v-h');
    ov.addEventListener('click', function (e) {
      var b = e.target.closest('[data-tt]'); if (!b) return;
      if (b.dataset.tt === 'close') close();
      else if (b.dataset.tt === 'edit') edit(it);
      else if (b.dataset.tt === 'done') { close(); setDone(it, it.status !== 'done'); }
    });
    var f0 = ov.querySelector('[data-tt="done"], [data-tt="edit"], [data-tt="close"]'); if (f0) f0.focus();
  }

  function setDone(it, done) {
    return post('/team/items/' + it.id + '/done', { done: done }).then(function () {
      changed();
      var told = [];
      if (done) {   // the server tells them; the toast names who hears
        told = peopleOf(it).concat(it.created_by ? [{ id: it.created_by, name: it.created_by_name }] : []);
      }
      form().then(function (f) {
        var seen = {}, others = told.filter(function (p) { if (!p.id || p.id === f.me || seen[p.id]) return false; seen[p.id] = 1; return true; });
        toast(done ? 'Done' + (others.length ? ' · ' + names(others, null) + (others.length === 1 ? ' is' : ' are') + ' told' : '') : 'Reopened',
          function () { post('/team/items/' + it.id + '/done', { done: !done }).then(changed).catch(function (e) { toast(e.message); }); });
      }).catch(function () { toast(done ? 'Done' : 'Reopened'); });
    }).catch(function (e) { toast(e.message); throw e; });
  }

  // ------------------------------------------------------------------ the time box (replaces the browser's hh:mm squares)
  var SLOTS = []; for (var m = 5 * 60; m <= 21 * 60 + 45; m += 15) SLOTS.push(String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0'));
  function parseTime(s) {
    s = String(s || '').trim().toLowerCase().replace(/\s+/g, '').replace(/\.$/, '').replace(/\./g, '');
    if (!s) return null;
    if (s === 'noon') return '12:00';
    var mt = s.match(/^(\d{1,2})(?::?(\d{2}))?(a|am|p|pm)?$/);
    if (!mt) return null;
    var h = +mt[1], mi = mt[2] ? +mt[2] : 0, ap = mt[3];
    if (mi > 59 || h > 23) return null;
    if (ap) { if (h > 12 || h === 0) return null; h = ap.charAt(0) === 'p' ? (h % 12) + 12 : h % 12; }
    else if (h >= 1 && h <= 5) h += 12;   // "2" on a job site means 2 PM; 6–11 stay morning; 12 is noon
    return String(h).padStart(2, '0') + ':' + String(mi).padStart(2, '0');
  }
  function timeBox(host, value, label) {
    host.className = 'tt-tp';
    host.innerHTML = IC.clock + '<input class="ssc-field tt-tp-in" placeholder="' + esc(label) + '" autocomplete="off" role="combobox" aria-expanded="false" aria-autocomplete="list" aria-label="' + esc(label) + '">' +
      '<button type="button" class="tt-tp-x" aria-label="Clear the time" hidden>×</button><ul class="tt-list" role="listbox" hidden></ul>';
    var inp = $q(host, 'input'), list = $q(host, 'ul'), x = $q(host, '.tt-tp-x'), v = value || null, opts = [], hi = -1, onc = null;
    function show() { inp.value = v ? fmtTime(v) : ''; x.hidden = !v; }
    function set(t) { v = t; show(); list.hidden = true; inp.setAttribute('aria-expanded', 'false'); if (onc) onc(v); }
    function draw(q) {
      var p = q ? parseTime(q) : null;
      if (q && p) { var i = SLOTS.findIndex(function (s) { return s >= p; }); if (i < 0) i = SLOTS.length; opts = [p].concat(SLOTS.slice(Math.max(0, i - 2), i + 6).filter(function (s) { return s !== p; })); hi = 0; }
      else if (q) { var qq = q.toLowerCase().replace(/\s/g, ''); opts = SLOTS.filter(function (s) { return fmtTime(s).toLowerCase().replace(/\s/g, '').indexOf(qq) === 0; }); hi = opts.length ? 0 : -1; }
      else { opts = SLOTS.slice(); hi = opts.indexOf(v || '08:00'); }
      list.innerHTML = opts.length ? opts.map(function (s, i) { return '<li role="option" data-i="' + i + '" class="' + (i === hi ? 'hi' : '') + '" aria-selected="' + (i === hi) + '">' + fmtTime(s) + (i === 0 && q && p ? '<small>Enter</small>' : '') + '</li>'; }).join('')
        : '<li class="tt-muted" aria-disabled="true">Try 7, 2:30p or 1430</li>';
      list.hidden = false; inp.setAttribute('aria-expanded', 'true');
      var h = list.querySelector('.hi'); if (h) list.scrollTop = h.offsetTop - 70;
    }
    inp.addEventListener('focus', function () { inp.select(); draw(''); });
    inp.addEventListener('input', function () { draw(inp.value); });
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault(); if (list.hidden) draw('');
        hi = Math.max(0, Math.min(opts.length - 1, hi + (e.key === 'ArrowDown' ? 1 : -1)));
        Array.prototype.forEach.call(list.querySelectorAll('li'), function (li, i) { li.classList.toggle('hi', i === hi); });
        var h = list.querySelector('.hi'); if (h) h.scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'Enter') { e.preventDefault(); if (!list.hidden && hi >= 0 && opts[hi]) set(opts[hi]); else if (!inp.value.trim()) set(null); }
      else if (e.key === 'Escape' && !list.hidden) { e.stopPropagation(); list.hidden = true; show(); }
      else if (e.key === 'Tab' && !list.hidden) { var p = parseTime(inp.value); if (inp.value.trim() && p) set(p); }
    });
    inp.addEventListener('blur', function () {
      setTimeout(function () {
        if (list.hidden) return;
        var p = parseTime(inp.value);
        if (inp.value.trim() && p) set(p); else if (!inp.value.trim()) set(null); else { list.hidden = true; show(); }
      }, 160);
    });
    list.addEventListener('mousedown', function (e) { var li = e.target.closest('[data-i]'); if (li) { e.preventDefault(); set(opts[+li.dataset.i]); } });
    x.addEventListener('click', function () { set(null); inp.focus(); });
    show();
    return { get: function () { return v; }, onChange: function (fn) { onc = fn; } };
  }

  // ------------------------------------------------------------------ NEW / EDIT
  function weekday(n) { var d = new Date(); return plus(((n - d.getDay()) + 7) % 7 || 7); }
  function edit(it, preset) {
    preset = preset || {};
    css(); picker();
    Promise.all([form(true), api('/google-calendar/status').then(function (j) { return j.data; }).catch(function () { return null; })])
      .then(function (res) { drawEdit(it, preset, res[0], res[1]); })
      .catch(function (e) { toast(e.message); });
  }
  function drawEdit(it, preset, f, gcal) {
    var me = f.me, boss = f.boss;
    var st = {
      kind: it ? it.kind || 'task' : (preset.kind || 'task'),
      date: it ? (dated(it) ? it.start_date : it.due_date) : (preset.date || null),
      who: it ? (it.assignee_ids || []).slice() : (preset.assignees ? preset.assignees.slice() : [me]),
      people: it ? (it.attendee_ids || []).slice() : [],
      guests: it ? (it.guest_emails || []).slice() : [], video: !!(it && it.video), meet: it ? it.meet_url : null,
      pick: false, asking: false
    };
    if (it && !dated(it) && !st.date) st.date = null;
    var byId = {}; f.people.forEach(function (p) { byId[p.id] = p; });
    if (it) (it.assignee_ids || []).forEach(function (id, k) { if (!byId[id]) byId[id] = { id: id, name: it.assignee_names[k], can_assign: false }; });
    if (it) (it.attendee_ids || []).forEach(function (id, k) { if (!byId[id]) byId[id] = { id: id, name: (it.attendee_names || [])[k], can_assign: false }; });
    if (!it) st.who = st.who.filter(function (id) { return byId[id] && byId[id].can_assign; });
    var canGcal = !!(gcal && gcal.available && gcal.connected && gcal.status === 'active');
    var jobs = f.jobs.slice();
    if (it && it.project_code && !jobs.some(function (j) { return j.project_code === it.project_code; })) jobs.unshift({ project_code: it.project_code, name: it.project_name || it.project_code });
    var ov = sheet(
      '<div class="tt-head"><h2 id="tsk-e-h">' + (it ? 'Edit ' + ({ task: 'task', event: 'event', meeting: 'meeting' }[it.kind] || 'task') : 'New') + '</h2>' +
      '<button type="button" class="tt-x" data-tt="close" aria-label="Close">' + IC.x + '</button></div>' +
      '<form class="tt-body" id="tsk-form" novalidate>' +
      '<div class="tt-kind" role="radiogroup" aria-label="Type" id="tsk-kind"></div>' +
      '<div class="tt-f"><label for="tt-title">What</label><input id="tsk-title" class="ssc-field tt-big" maxlength="200" placeholder="e.g. Call the property manager, Job walkthrough, Send invoice"></div>' +
      '<div class="tt-f"><span class="tt-lbl" id="tsk-when-l">Deadline</span><div class="tt-chips" id="tsk-when" role="group" aria-labelledby="tsk-when-l"></div>' +
      '<div class="tt-pick" id="tsk-pick" hidden><input id="tsk-date" type="date" class="ssc-field" aria-label="Pick a date"></div></div>' +
      '<div class="tt-grid2" id="tsk-times"><div class="tt-f"><span class="tt-lbl" id="tsk-t1-l">Time</span><div id="tsk-t1"></div></div>' +
      '<div class="tt-f" id="tsk-t2-f"><span class="tt-lbl">Ends</span><div id="tsk-t2"></div></div></div>' +
      '<div class="tt-f" id="tsk-end-f"><label for="tt-end">Last day <span class="tt-muted">(if more than one day)</span></label><input id="tsk-end" type="date" class="ssc-field"></div>' +
      '<div class="tt-f"><span class="tt-lbl" id="tsk-who-l"></span><div class="tt-people" id="tsk-who" role="group" aria-labelledby="tsk-who-l"></div><div class="tt-hint" id="tsk-who-hint"></div></div>' +
      '<div class="tt-f" id="tsk-ppl-f"><span class="tt-lbl" id="tsk-ppl-l">People in it</span><div class="tt-people" id="tsk-ppl" role="group" aria-labelledby="tsk-ppl-l"></div>' +
      '<div class="tt-ext" id="tsk-ext"><input id="tsk-ext-in" type="email" autocomplete="off" placeholder="Add guests by email, e.g. a client or an architect" aria-label="Add a guest by email"></div>' +
      '<p class="tt-hint" id="tsk-inv-sub">Each guest gets the invitation from your Gmail, like any Google Calendar invite, and can answer Yes / No / Maybe.</p>' +
      '<div id="tsk-video"></div><div id="tsk-gcal"></div></div>' +
      '<div class="tt-grid2"><div class="tt-f"><label for="tt-job">Job <span class="tt-muted">(optional)</span></label><select id="tsk-job" class="ssc-field"><option value="">No job</option>' +
      jobs.map(function (j) { return '<option value="' + esc(j.project_code) + '">' + esc(j.name) + ' · ' + esc(j.project_code) + '</option>'; }).join('') + '</select></div>' +
      '<div class="tt-f tt-addr"><label for="tt-address">Address</label><input id="tsk-address" class="ssc-field" maxlength="300" autocomplete="off" placeholder="Start typing, or pick a job" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="tsk-addr-list">' +
      '<ul class="tt-list" id="tsk-addr-list" role="listbox" hidden></ul></div></div>' +
      '<div class="tt-f"><label for="tt-detail">Details</label><textarea id="tsk-detail" class="ssc-field" maxlength="2000" placeholder="Anything they need to know"></textarea></div>' +
      '<div class="tt-notify" id="tsk-notify" hidden>' + IC.bell + '<span></span></div>' +
      '<div class="tt-err" id="tsk-err" role="alert"></div>' +
      '</form><div class="tt-foot" id="tsk-foot"></div>', 'tsk-e-h');
    var g = function (id) { return ov.querySelector('#' + id); };
    g('tsk-title').value = it ? it.title || '' : (preset.title || '');
    g('tsk-detail').value = it ? it.detail || '' : '';
    g('tsk-job').value = it ? it.project_code || '' : (preset.project_code || '');
    g('tsk-address').value = it ? [it.address].filter(Boolean).join('') : '';
    if (!it && preset.project_code) jobAddress();
    dpSet(g('tsk-end'), it && it.end_date && it.end_date !== it.start_date ? it.end_date : '');
    var t1 = timeBox(g('tsk-t1'), it ? (dated(it) ? it.start_time : it.due_time) : (preset.due_time || null), 'Add a time');
    var t2 = timeBox(g('tsk-t2'), it ? it.end_time : null, 'Add an end');

    function foot() {
      if (st.asking) return;
      g('tsk-foot').innerHTML = (it && it.can_edit ? '<button type="button" class="btn-ghost tt-del" data-tt="del">Delete</button>' : '') +
        '<span class="tt-sp"></span><button type="button" class="btn-ghost" data-tt="close">Cancel</button>' +
        '<button type="submit" form="tsk-form" class="btn-primary" id="tsk-save">' + (it ? 'Save' : 'Add') + '</button>';
    }
    function drawKind() {
      g('tsk-kind').innerHTML = [['task', 'Task', 'a to-do'], ['event', 'Event', 'happens on a day'], ['meeting', 'Meeting', 'with people']].map(function (k) {
        return '<button type="button" role="radio" aria-checked="' + (st.kind === k[0]) + '" class="' + (st.kind === k[0] ? 'on' : '') + '" data-kind="' + k[0] + '">' + k[1] + '<small>' + k[2] + '</small></button>';
      }).join('');
      var ev = st.kind !== 'task';
      g('tsk-when-l').textContent = ev ? 'Day' : 'Deadline';
      g('tsk-t1-l').textContent = ev ? 'Starts' : 'Time';
      g('tsk-t2-f').hidden = !ev; g('tsk-end-f').hidden = !ev;
      g('tsk-ppl-f').hidden = st.kind !== 'meeting';
      g('tsk-who-l').innerHTML = (st.kind === 'meeting' ? 'Running it' : 'Who it’s for') + ' <span class="tt-muted">· one or more</span>';
      drawWhen(); drawPeople(); drawInvites();
    }
    function drawWhen() {
      var o = st.kind === 'task' ? [[null, 'No date']] : [];
      o.push([today(), 'Today'], [plus(1), 'Tomorrow']);
      var fri = weekday(5), mon = weekday(1);
      if (fri !== plus(1) && fri !== today()) o.push([fri, 'Friday']);
      if (mon !== plus(1)) o.push([mon, 'Monday']);
      var known = o.some(function (x) { return x[0] === st.date; });
      g('tsk-when').innerHTML = o.map(function (x) {
        var on = st.date === x[0] && !st.pick;
        return '<button type="button" class="tt-chip' + (on ? ' on' : '') + '" data-date="' + (x[0] || '') + '" aria-pressed="' + on + '">' + x[1] + (x[0] && x[1] !== 'Today' && x[1] !== 'Tomorrow' ? '<small>' + pISO(x[0]).getDate() + '</small>' : '') + '</button>';
      }).join('') + '<button type="button" class="tt-chip' + ((st.pick || (!known && st.date)) ? ' on' : '') + '" data-pick aria-expanded="' + st.pick + '">' + (!known && st.date && !st.pick ? esc(fmtDate(st.date)) : 'Pick a date…') + '</button>';
      g('tsk-pick').hidden = !st.pick;
      g('tsk-times').hidden = st.kind === 'task' && !st.date && !st.pick;
    }
    function drawPeople() {
      var list = f.people.filter(function (p) { return p.can_assign; }).map(function (p) { return p.id; });
      st.who.forEach(function (id) { if (list.indexOf(id) < 0) list.push(id); });
      list.sort(function (a, b) { return a === me ? -1 : b === me ? 1 : 0; });
      g('tsk-who').innerHTML = (boss ? '<button type="button" class="tt-pp none' + (!st.who.length ? ' on' : '') + '" data-who="" aria-pressed="' + !st.who.length + '">Nobody yet</button>' : '') +
        list.map(function (id) { var p = byId[id] || { name: '?' }, on = st.who.indexOf(id) >= 0; return '<button type="button" class="tt-pp' + (on ? ' on' : '') + '" data-who="' + id + '" aria-pressed="' + on + '">' + ava(id, p.name) + (id === me ? 'Me' : esc(first(p.name))) + (p.hint ? ' <small>' + esc(p.hint) + '</small>' : '') + '</button>'; }).join('');
      g('tsk-who-hint').textContent = (st.who.length > 1 && st.kind === 'task' ? 'Any of them can mark it done, and then it’s done for everyone. ' : 'Tap more than one to share it. ') +
        (boss ? '' : 'You can give it to yourself and the people on your jobs.');
      var pl = f.people.filter(function (p) { return p.can_assign && st.who.indexOf(p.id) < 0; }).map(function (p) { return p.id; });
      st.people.forEach(function (id) { if (pl.indexOf(id) < 0 && st.who.indexOf(id) < 0) pl.push(id); });
      g('tsk-ppl').innerHTML = pl.map(function (id) { var p = byId[id] || { name: '?' }, on = st.people.indexOf(id) >= 0; return '<button type="button" class="tt-pp' + (on ? ' on' : '') + '" data-ppl="' + id + '" aria-pressed="' + on + '">' + ava(id, p.name) + (id === me ? 'Me' : esc(first(p.name))) + '</button>'; }).join('') || '<span class="tt-hint">Nobody else to add.</span>';
      notify();
    }
    function notify() {
      var was = it ? (it.assignee_ids || []).concat(it.attendee_ids || []) : [];
      var on = st.who.concat(st.kind === 'meeting' ? st.people : []), seen = {}, told = [];
      on.forEach(function (id) { if (id !== me && was.indexOf(id) < 0 && !seen[id]) { seen[id] = 1; told.push({ id: id, name: (byId[id] || {}).name }); } });
      g('tsk-notify').hidden = !told.length;
      g('tsk-notify').querySelector('span').textContent = told.length ? names(told) + (told.length === 1 ? ' gets' : ' get') + ' a pop-up in the app as soon as you ' + (it ? 'save' : 'add') + ' it.' : '';
    }
    function drawInvites() {
      if (st.kind !== 'meeting') return;
      Array.prototype.forEach.call(g('tsk-ext').querySelectorAll('.tt-gchip'), function (x) { x.remove(); });
      g('tsk-ext-in').insertAdjacentHTML('beforebegin', st.guests.map(function (e, i) { return '<span class="tt-gchip">' + esc(e) + '<button type="button" data-gx="' + i + '" aria-label="Remove ' + esc(e) + '">×</button></span>'; }).join(''));
      g('tsk-video').innerHTML = !canGcal ? '<button type="button" class="tt-meetbtn" disabled>' + MEET + 'Add Google Meet video conferencing</button>'
        : st.video ? '<div class="tt-meeton">' + MEET + '<div class="t"><b>Join with Google Meet</b>' + (st.meet ? '<a href="' + esc(st.meet) + '" target="_blank" rel="noopener">' + esc(st.meet.replace(/^https:\/\//, '')) + '</a>' : '<span class="tt-muted">The link is made when you save</span>') + '</div><button type="button" class="btn-ghost btn-sm" data-video="off">Remove</button></div>'
          : '<button type="button" class="tt-meetbtn" data-video="on">' + MEET + 'Add Google Meet video conferencing</button>';
      var next = encodeURIComponent(location.pathname + location.hash);
      g('tsk-gcal').innerHTML = canGcal ? (it && it.invite_problem ? '<div class="tt-warn"><p>' + esc(it.invite_problem) + '.</p></div>' : '')
        : '<div class="tt-warn"><p><b>' + (gcal && gcal.status === 'needs_reconnect' ? 'Reconnect' : 'Connect') + ' your Google Calendar to send invitations and add video.</b> Until then the meeting is saved for the team, but guests are not emailed.</p>' +
          (gcal && gcal.available ? '<a class="btn-primary btn-sm" href="/google-calendar/connect?next=' + next + '">' + (gcal.status === 'needs_reconnect' ? 'Reconnect' : 'Connect') + '</a>' : '') + '</div>';
      g('tsk-inv-sub').hidden = !canGcal;
    }
    function addGuest() {
      var inp = g('tsk-ext-in'), v = inp.value.trim().replace(/[,;]$/, '').toLowerCase();
      if (!v) return true;
      if (!EMAIL.test(v)) { err('That guest email address isn’t valid.'); return false; }
      if (st.guests.indexOf(v) < 0) st.guests.push(v);
      inp.value = ''; err(''); drawInvites(); return true;
    }
    function err(m) { g('tsk-err').textContent = m || ''; }
    function jobAddress() {
      var j = jobs.filter(function (x) { return x.project_code === g('tsk-job').value; })[0];
      if (j && j.address && !g('tsk-address').value.trim()) g('tsk-address').value = j.address;
    }
    foot(); drawKind();

    // address suggestions while typing (the jobs' own addresses first, then NYC addresses)
    var A = { t: null, items: [], i: -1, seq: 0 };
    function addrClose() { g('tsk-addr-list').hidden = true; g('tsk-address').setAttribute('aria-expanded', 'false'); A.items = []; A.i = -1; }
    function addrDraw() {
      var ul = g('tsk-addr-list');
      if (!A.items.length) { addrClose(); return; }
      ul.innerHTML = A.items.map(function (x, k) { return '<li role="option" data-k="' + k + '" class="' + (k === A.i ? 'hi' : '') + '" aria-selected="' + (k === A.i) + '"><span>' + esc(x.label) + '</span>' + (x.job ? '<small>' + esc(x.job) + '</small>' : '') + '</li>'; }).join('');
      ul.hidden = false; g('tsk-address').setAttribute('aria-expanded', 'true');
    }
    g('tsk-address').addEventListener('input', function () {
      clearTimeout(A.t); var q = g('tsk-address').value.trim();
      if (q.length < 3) { addrClose(); return; }
      A.t = setTimeout(function () {
        var seq = ++A.seq;
        api('/team/address-suggest?q=' + encodeURIComponent(q)).then(function (j) { if (seq !== A.seq) return; A.items = j.data || []; A.i = -1; addrDraw(); }).catch(addrClose);
      }, 250);
    });
    g('tsk-address').addEventListener('keydown', function (e) {
      if (g('tsk-addr-list').hidden) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); A.i = (A.i + (e.key === 'ArrowDown' ? 1 : -1) + A.items.length) % A.items.length; addrDraw(); }
      else if (e.key === 'Enter' && A.i >= 0) { e.preventDefault(); g('tsk-address').value = A.items[A.i].label; addrClose(); }
      else if (e.key === 'Escape') { e.stopPropagation(); addrClose(); }
    });
    g('tsk-addr-list').addEventListener('mousedown', function (e) { var li = e.target.closest('[data-k]'); if (li) { e.preventDefault(); g('tsk-address').value = A.items[+li.dataset.k].label; addrClose(); } });
    g('tsk-address').addEventListener('blur', function () { setTimeout(addrClose, 150); });
    g('tsk-job').addEventListener('change', jobAddress);
    g('tsk-date').addEventListener('change', function () { var v = dpGet(g('tsk-date')); if (v) { st.date = v; g('tsk-times').hidden = false; } });
    g('tsk-ext-in').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ',' || e.key === ';') { e.preventDefault(); addGuest(); }
      else if (e.key === 'Backspace' && !e.target.value && st.guests.length) { st.guests.pop(); drawInvites(); }
    });
    g('tsk-ext-in').addEventListener('blur', function () { if (g('tsk-ext-in').value.trim()) addGuest(); });

    ov.addEventListener('click', function (e) {
      var t = e.target, b;
      if ((b = t.closest('[data-tt="close"]'))) { close(); return; }
      if ((b = t.closest('[data-kind]'))) { st.kind = b.dataset.kind; if (st.kind !== 'task' && !st.date) st.date = today(); drawKind(); return; }
      if ((b = t.closest('[data-date]'))) { st.date = b.dataset.date || null; st.pick = false; drawWhen(); return; }
      if (t.closest('[data-pick]')) {
        st.pick = !st.pick; drawWhen();
        if (st.pick) { dpSet(g('tsk-date'), st.date || ''); setTimeout(function () { g('tsk-date').focus(); g('tsk-date').click(); }, 30); }
        return;
      }
      if ((b = t.closest('[data-who]'))) {
        var x = b.dataset.who ? Number(b.dataset.who) : null;
        st.who = x == null ? [] : st.who.indexOf(x) >= 0 ? st.who.filter(function (y) { return y !== x; }) : st.who.concat([x]);
        st.people = st.people.filter(function (y) { return st.who.indexOf(y) < 0; }); err(''); drawPeople(); return;
      }
      if ((b = t.closest('[data-ppl]'))) { var p = Number(b.dataset.ppl); st.people = st.people.indexOf(p) >= 0 ? st.people.filter(function (y) { return y !== p; }) : st.people.concat([p]); drawPeople(); return; }
      if ((b = t.closest('[data-gx]'))) { st.guests.splice(+b.dataset.gx, 1); drawInvites(); return; }
      if (t === g('tsk-ext')) { g('tsk-ext-in').focus(); return; }
      if ((b = t.closest('[data-video]'))) { st.video = b.dataset.video === 'on'; if (!st.video) st.meet = null; drawInvites(); return; }
      if (t.closest('[data-tt="del"]')) {
        st.asking = true;
        g('tsk-foot').innerHTML = '<div class="tt-confirm"><span>Delete “' + esc(it.title) + '”? It comes off everyone’s list and calendar.</span><span class="tt-sp"></span>' +
          '<button type="button" class="btn-ghost btn-sm" data-tt="keep">Keep it</button><button type="button" class="btn-primary btn-sm tt-danger" data-tt="del-yes">Delete</button></div>';
        return;
      }
      if (t.closest('[data-tt="keep"]')) { st.asking = false; foot(); return; }
      if (t.closest('[data-tt="del-yes"]')) {
        api('/team/items/' + it.id, { method: 'DELETE' }).then(function () {
          close(); changed();
          toast('Deleted', function () { post('/team/items/' + it.id + '/restore').then(function () { changed(); toast('Back on the list'); }).catch(function (ex) { toast(ex.message); }); });
        }).catch(function (ex) { st.asking = false; foot(); err(ex.message); });
      }
    });

    function askSend(n, video) {   // Send / Don't send, like Google Calendar; resolves true / false, or null for Back
      return new Promise(function (res) {
        st.asking = true;
        g('tsk-foot').innerHTML = '<div class="tt-confirm tt-ask"><span>Email a Google Calendar invitation to ' + n + ' guest' + (n === 1 ? '' : 's') + (video ? ', with the Google Meet link' : '') + '?</span><span class="tt-sp"></span>' +
          '<button type="button" class="btn-ghost btn-sm" data-send="back">Back</button><button type="button" class="btn-ghost btn-sm" data-send="no">Don’t send</button><button type="button" class="btn-primary btn-sm" data-send="yes">Send</button></div>';
        g('tsk-foot').querySelector('[data-send="yes"]').focus();
        g('tsk-foot').onclick = function (e) {
          var b = e.target.closest('[data-send]'); if (!b) return;
          g('tsk-foot').onclick = null; st.asking = false;
          res(b.dataset.send === 'yes' ? true : b.dataset.send === 'no' ? false : null);
        };
      });
    }

    g('tsk-form').addEventListener('submit', function (e) {
      e.preventDefault(); err('');
      if (st.pick) { var pv = dpGet(g('tsk-date')); st.date = pv || st.date; }
      var title = g('tsk-title').value.trim(), kind = st.kind;
      if (!title) { err('Say what it is.'); g('tsk-title').focus(); return; }
      if (kind !== 'task' && !st.date) { err('Pick the day of the ' + kind + '.'); return; }
      if (!boss && !st.who.length) { err('Pick who it’s for: you, or someone on your jobs.'); return; }
      var body = { kind: kind, title: title, detail: g('tsk-detail').value.trim() || null, assignee_user_ids: st.who.slice(),
        project_code: g('tsk-job').value || null, address: g('tsk-address').value.trim() || null };
      if (kind === 'task') { body.due_date = st.date || null; body.due_time = st.date ? t1.get() : null; }
      else { body.start_date = st.date; body.end_date = dpGet(g('tsk-end')) || null; body.start_time = t1.get(); body.end_time = t1.get() ? t2.get() : null; }
      var go = function () {
        var save = g('tsk-save'); if (save) save.disabled = true;
        var req = it ? post('/team/items/' + it.id, body, 'PUT') : post('/team/items', body);
        req.then(function (j) {
          close(); changed();
          var told = [], was = it ? (it.assignee_ids || []).concat(it.attendee_ids || []) : [], seen = {};
          body.assignee_user_ids.concat(body.attendee_user_ids || []).forEach(function (id) { if (id !== me && was.indexOf(id) < 0 && !seen[id]) { seen[id] = 1; told.push({ id: id, name: (byId[id] || {}).name }); } });
          var msg = (it ? 'Saved' : ({ event: 'Event added', meeting: 'Meeting added' }[kind] || 'Task added')) + (told.length ? ' · ' + names(told) + (told.length === 1 ? ' gets' : ' get') + ' a pop-up' : '') + (body.send_invites ? ' · invitations sent' : '');
          var nid = j && j.data && j.data.id;
          toast(msg, it ? null : function () { api('/team/items/' + nid, { method: 'DELETE' }).then(function () { changed(); toast('Taken back'); }).catch(function (ex) { toast(ex.message); }); });
        }).catch(function (ex) { err(ex.message); if (save) save.disabled = false; });
      };
      if (kind === 'meeting') {
        if (!addGuest()) return;
        body.attendee_user_ids = st.people.slice();
        body.guest_emails = st.guests.slice(); body.video = canGcal && st.video;
        var ppl = {}; st.people.concat(st.who).forEach(function (id) { if (id !== me) ppl[id] = 1; });
        var n = Object.keys(ppl).length + st.guests.length;
        if (canGcal && n) {
          askSend(n, body.video).then(function (send) { if (send === null) { foot(); return; } body.send_invites = send; foot(); go(); });
          return;
        }
      }
      go();
    });
    setTimeout(function () { g('tsk-title').focus(); }, 30);
  }

  // ------------------------------------------------------------------ the "Add a task…" line
  function parseQuick(s, people) {
    var t = ' ' + s + ' ', due = null, time = null, who = [];
    var dm = t.match(/\s(today|tonight|tomorrow|tmrw|next week|mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)(?=[\s,.])/i);
    if (dm) {
      var w = dm[1].toLowerCase();
      if (w === 'today' || w === 'tonight') due = today();
      else if (w === 'tomorrow' || w === 'tmrw') due = plus(1);
      else if (w === 'next week') due = weekday(1);
      else due = weekday(['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'].indexOf(w.slice(0, 3)));
      t = t.replace(dm[0], ' ');
    }
    var tm = t.match(/\s(?:at\s)?(\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.|a|p))(?=[\s,.])/i) || t.match(/\s(?:at\s)(\d{1,2}(?::\d{2})?)(?=[\s,.])/i);
    if (tm) { time = parseTime(tm[1]); if (time) t = t.replace(tm[0], ' '); }
    t = t.replace(/\s(?:and\s+|&\s+|,\s*)?@([\w.'-]+)/gi, function (m0, n) {
      var q = n.toLowerCase().replace(/[.'-]+$/, '');
      var p = (people || []).filter(function (x) { var nm = String(x.name || '').toLowerCase(); return nm.split(/\s+/)[0] === q || nm.replace(/\s+/g, '') === q; })[0] ||
        (people || []).filter(function (x) { return String(x.name || '').toLowerCase().indexOf(q) === 0; })[0];
      if (!p) return m0;
      if (who.indexOf(p.id) < 0) who.push(p.id);
      return ' ';
    });
    if (time && !due) due = today();
    t = t.replace(/\s+(on|by|at|for)\s*$/i, ' ').replace(/\s+/g, ' ').trim();
    return { title: t ? t.charAt(0).toUpperCase() + t.slice(1) : '', due: due, time: time, who: who };
  }
  function quick(formEl) {
    if (!formEl || formEl.dataset.ttQuick) return;
    formEl.dataset.ttQuick = '1';
    css();
    var inp = formEl.querySelector('input'), chips = formEl.parentNode.querySelector('[data-tt-chips]'), hint = formEl.parentNode.querySelector('[data-tt-hint]'), more = formEl.querySelector('[data-tt-more]');
    var F = null; form().then(function (f) { F = f; }).catch(function () { });
    function parse() { return parseQuick(inp.value, F ? F.people : []); }
    function draw() {
      var v = inp.value.trim(); if (hint) hint.hidden = !!v;
      if (!chips) return;
      if (!v) { chips.innerHTML = ''; chips.hidden = true; return; }
      var q = parse(), byId = {}; (F ? F.people : []).forEach(function (p) { byId[p.id] = p; });
      chips.hidden = false;
      chips.innerHTML = '<span class="tt-qchip">' + IC.cal + (q.due ? esc(cap(dayWord(q.due))) + (q.time ? ' · ' + esc(fmtTime(q.time)) : '') : 'No deadline') + '</span>' +
        (q.who.length ? q.who.map(function (id) { var p = byId[id] || {}; return '<span class="tt-qchip' + (p.can_assign ? '' : ' warn') + '">' + ava(id, p.name) + esc(first(p.name)) + (p.can_assign ? '' : ' · not on your jobs') + '</span>'; }).join('')
          : '<span class="tt-qchip plain">For you</span>');
    }
    inp.addEventListener('input', draw);
    formEl.addEventListener('submit', function (e) {
      e.preventDefault();
      var q = parse(); if (!q.title) { inp.focus(); return; }
      var byId = {}; (F ? F.people : []).forEach(function (p) { byId[p.id] = p; });
      var bad = q.who.filter(function (id) { return !(byId[id] && byId[id].can_assign); });
      if (bad.length) { toast(names(bad.map(function (id) { return { id: id, name: (byId[id] || {}).name }; })) + (bad.length === 1 ? ' is' : ' are') + ' not on your jobs. Pick people from your crew.'); return; }
      var me = F ? F.me : null, who = q.who.length ? q.who : (me ? [me] : []);
      inp.disabled = true;
      post('/team/items', { kind: 'task', title: q.title, due_date: q.due, due_time: q.time, assignee_user_ids: who }).then(function (j) {
        inp.value = ''; draw(); changed();
        var others = who.filter(function (id) { return id !== me; }).map(function (id) { return { id: id, name: (byId[id] || {}).name }; });
        toast('Task added' + (others.length ? ' · ' + names(others) + (others.length === 1 ? ' gets' : ' get') + ' a pop-up' : ''), function () {
          api('/team/items/' + j.data.id, { method: 'DELETE' }).then(function () { changed(); toast('Taken back'); }).catch(function (ex) { toast(ex.message); });
        });
      }).catch(function (ex) { toast(ex.message); }).then(function () { inp.disabled = false; inp.focus(); });
    });
    if (more) more.addEventListener('click', function () {
      var q = parse();
      edit(null, { title: q.title, date: q.due, due_time: q.time, assignees: q.who.length ? q.who : null });
      inp.value = ''; draw();
    });
  }

  window.SSC_TASKS = { view: view, edit: edit, quick: quick, toast: toast, close: close, when: when, names: names,
    setDone: setDone, parseQuick: parseQuick, parseTime: parseTime, form: form };
})();
