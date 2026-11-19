/* Projects → Calendar, easier to scan (2026-11-19, accepted prototype https://claude.ai/artifact/Xs5kMdaim3nW3FzoBcSUEb).

   Three things on the calendar: the PROJECTS running (bars, start → anticipated completion), the TASKS and
   meetings people are assigned (with who), and New York City's WEATHER. Your own Google events stay dashed
   and private (only the signed-in person's, as before).

   Desktop / tablet: Day · Week · Month. Week has the hours (meetings at their time, a red line for now),
   a weather row, the project bars (drag to move / stretch, with Undo) and the untimed tasks.
   Phone (≤ 760px): List (a week strip + day by day, swipe for another week) · Month (a heat map + the day).
   Every day has a one-line summary; amber = weather that can stop a project working that day
   (wind for rope access / suspended scaffold / boom lift, rain for exterior work), red = an overdue task.
   Click / tap a day: a side panel (desktop) or bottom sheet (phone) with the whole day.

   Data: GET /api/company/portal/calendar (jobs, days, items, mine, overdue) and /calendar/weather (NYC).
   The page (company_portal.html) mounts it and lends its helpers (api, toast, the task card, the Google card). */
(function () {
  'use strict';
  var DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var DOWL = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var MONL = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

  // local dates only (never a UTC string)
  function isoOf(d) { return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
  function parse(s) { var p = String(s).slice(0, 10).split('-').map(Number); return new Date(p[0], p[1] - 1, p[2]); }
  function add(d, n) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n); }
  function monday(d) { return add(d, -((d.getDay() + 6) % 7)); }
  function todayISO() { return isoOf(new Date()); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function minOf(hm) { var p = String(hm || '0:0').split(':'); return (+p[0]) * 60 + (+p[1] || 0); }
  function t12(hm) { if (!hm) return ''; var m = minOf(hm), h = Math.floor(m / 60), mm = m % 60; return ((h % 12) || 12) + (mm ? ':' + String(mm).padStart(2, '0') : '') + (h < 12 ? ' AM' : ' PM'); }
  function longDate(s) { var d = parse(s); return DOWL[d.getDay()] + ', ' + MONL[d.getMonth()] + ' ' + d.getDate(); }
  function shortDate(s) { var d = parse(s); return DOW[d.getDay()] + ' ' + MON[d.getMonth()] + ' ' + d.getDate(); }
  function initials(n) { return String(n || '?').split(/[\s@.]+/).filter(Boolean).slice(0, 2).map(function (x) { return x[0].toUpperCase(); }).join(''); }
  function rel(s) { var t = todayISO(); if (s === t) return 'Today'; if (s === isoOf(add(parse(t), 1))) return 'Tomorrow'; if (s === isoOf(add(parse(t), -1))) return 'Yesterday'; return ''; }
  function plural(n, w) { return n + ' ' + w + (n === 1 ? '' : 's'); }

  var P = {
    job: '<path d="M4 21V5l8-3 8 3v16M9 21v-5h6v5M8 8h2M14 8h2M8 12h2M14 12h2"/>',
    task: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.6 2.6L16 9.5"/>',
    meet: '<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0M16 4a3 3 0 0 1 0 6M21 20a6 6 0 0 0-4-5.6"/>',
    mine: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    warn: '<path d="M12 3 2 20h20zM12 10v4M12 17v.5"/>',
    crit: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16v.5"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
    cloud: '<path d="M7 18h10a4 4 0 0 0 .5-8 6 6 0 0 0-11.5 1.5A3.3 3.3 0 0 0 7 18z"/>',
    rain: '<path d="M7 15h10a4 4 0 0 0 .5-8 6 6 0 0 0-11.5 1.5A3.3 3.3 0 0 0 7 15zM9 18l-1 3M13 18l-1 3M17 18l-1 3"/>',
    snow: '<path d="M7 15h10a4 4 0 0 0 .5-8 6 6 0 0 0-11.5 1.5A3.3 3.3 0 0 0 7 15zM9 19v.5M13 20v.5M17 19v.5"/>',
    storm: '<path d="M7 15h10a4 4 0 0 0 .5-8 6 6 0 0 0-11.5 1.5A3.3 3.3 0 0 0 7 15zM13 16l-2 3h3l-2 3"/>',
    fog: '<path d="M4 10h16M6 14h12M8 18h8M7 6h10"/>',
    wind: '<path d="M3 8h11a3 3 0 1 0-3-3M3 12h16a3 3 0 1 1-3 3M3 16h8"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    x: '<path d="M6 6l12 12M18 6 6 18"/>',
    back: '<path d="M15 6l-6 6 6 6"/>',
    pin: '<path d="M12 21s-7-6.2-7-12a7 7 0 0 1 14 0c0 5.8-7 12-7 12z"/><circle cx="12" cy="9" r="2.5"/>'
  };
  function ic(n) { return '<svg class="jc-ic" viewBox="0 0 24 24" aria-hidden="true">' + (P[n] || '') + '</svg>'; }
  var KINDS = [['jobs', 'Projects', 'job'], ['task', 'Tasks', 'task'], ['meet', 'Meetings & events', 'meet'], ['mine', 'My Google events', 'mine']];

  // WMO weather code -> an icon
  function wxKind(w) {
    var c = w.code;
    if (c == null) return 'cloud';
    if (c === 0 || c === 1) return 'sun';
    if (c === 45 || c === 48) return 'fog';
    if (c >= 95) return 'storm';
    if ((c >= 71 && c <= 77) || c === 85 || c === 86) return 'snow';
    if ((c >= 51 && c <= 67) || (c >= 80 && c <= 82)) return 'rain';
    return 'cloud';
  }
  function r0(v) { return v == null ? null : Math.round(v); }

  function mount(host) {
    var root = host.root;
    var $ = function (id) { return document.getElementById(id); };
    var S = { view: 'week', pview: 'list', anchor: new Date(), layers: { jobs: 1, task: 1, meet: 1 }, who: '', data: null, cache: {},
      wx: {}, wxKeys: {}, th: { wind_mph: 20, gust_mph: 30, rain_pct: 60 }, phone: false, seq: 0, panel: null };
    try {
      var v = localStorage.getItem('cp_cal_view'); if (v === '3day') v = 'week'; if (v === 'day' || v === 'week' || v === 'month') S.view = v;
      var pv = localStorage.getItem('cp_cal_pview'); if (pv === 'list' || pv === 'month') S.pview = pv;
      var ly = JSON.parse(localStorage.getItem('cp_cal_layers') || 'null'); if (ly) ['jobs', 'task', 'meet'].forEach(function (k) { if (k in ly) S.layers[k] = ly[k] ? 1 : 0; });
      S.who = localStorage.getItem('cp_cal_who') || '';
    } catch (e) {}
    function save() { try { localStorage.setItem('cp_cal_view', S.view); localStorage.setItem('cp_cal_pview', S.pview); localStorage.setItem('cp_cal_layers', JSON.stringify(S.layers)); localStorage.setItem('cp_cal_who', S.who); } catch (e) {} }
    var mq = window.matchMedia ? window.matchMedia('(max-width: 760px)') : { matches: false };
    S.phone = !!mq.matches;
    function view() { return S.phone ? S.pview : S.view; }
    function layerOn(k) { return k === 'mine' ? !!host.mineOn() : !!S.layers[k]; }

    /* ---------- range ---------- */
    function range() {
      var a = S.anchor, v = view();
      if (v === 'month') { var f = new Date(a.getFullYear(), a.getMonth(), 1), l = new Date(a.getFullYear(), a.getMonth() + 1, 0); return [monday(f), add(monday(l), 6)]; }
      var m = monday(a); return [m, add(m, 6)];   // day / week / list all read the week (one cache)
    }
    function days(a, b) { var out = []; for (var d = a; d <= b; d = add(d, 1)) out.push(isoOf(d)); return out; }

    /* ---------- the day model ---------- */
    function entries(d) {
      if (d._entries) return d._entries;
      var out = (d.items || []).map(function (it) {
        var meet = it.kind === 'event' || it.kind === 'meeting';
        var names = (it.assignee_names || []).concat(it.attendee_names || []).filter(function (n, i, a) { return n && a.indexOf(n) === i; });
        return { src: it, id: 'i' + it.id, k: meet ? 'meet' : 'task', title: it.title || 'Untitled', from: it.calendar_from, to: it.calendar_to || it.calendar_from,
          t: meet ? it.start_time : it.due_time, e: meet ? it.end_time : null, done: it.status === 'done', late: !!it.overdue,
          names: names, ids: (it.assignee_ids || []).concat(it.attendee_ids || []), mine: !!it.mine, code: it.project_code, pname: it.project_name, place: it.location || it.address };
      }).filter(function (x) { return x.from; });
      (d.mine || []).forEach(function (e) {
        out.push({ src: e, id: 'm' + e.id, k: 'mine', title: e.title || 'Busy', from: e.start_date, to: e.end_date || e.start_date, t: e.start_time, e: e.end_time, names: [], ids: [], mine: true, place: e.location });
      });
      d._entries = out; return out;
    }
    function visible(x, all) {
      if (all) return x.k !== 'mine' || layerOn('mine');
      if (!layerOn(x.k)) return false;
      if (!S.who || x.k === 'mine') return true;
      if (S.who === 'me') return x.mine;
      return x.ids.map(String).indexOf(S.who) >= 0;
    }
    function on(d, iso, all) { return entries(d).filter(function (x) { return x.from <= iso && iso <= x.to && visible(x, all); }); }
    function isTimed(x, iso) { return !!x.t && (x.k === 'task' || x.from === iso); }
    function timedOn(d, iso, all) { return on(d, iso, all).filter(function (x) { return isTimed(x, iso); }).sort(function (a, b) { return minOf(a.t) - minOf(b.t); }); }
    function untimedOn(d, iso, all) { return on(d, iso, all).filter(function (x) { return !isTimed(x, iso); }); }
    function jobsOn(d, iso) { return (d.jobs || []).filter(function (j) { return j.start_date <= iso && (j.open_ended || (j.end_date && j.end_date >= iso)); }); }
    function workday(iso) { var g = parse(iso).getDay(); return g >= 1 && g <= 5; }
    function people(d, iso, j) {   // signed in (today / past) or the crew on it (future weekdays)
      var info = (d.days || {})[iso] || {};
      if (iso <= todayISO()) return (info.on_site || {})[j.project_code] || 0;
      return j.status === 'active' && workday(iso) ? (j.crew_count || 0) : 0;
    }
    function summary(d, iso, all) {
      var js = jobsOn(d, iso).filter(function (j) { return j.status === 'active' || iso <= todayISO(); });
      var past = iso <= todayISO(), n = 0;
      if (past) n = ((d.days || {})[iso] || {}).headcount || 0; else js.forEach(function (j) { n += people(d, iso, j); });
      var tasks = on(d, iso, all).filter(function (x) { return x.k === 'task'; }).length;
      return { jobs: (all || layerOn('jobs')) ? js.length : 0, n: (all || layerOn('jobs')) ? n : 0, past: past, tasks: tasks };
    }
    function summaryHtml(d, iso, all) {
      var s = summary(d, iso, all), out = [];
      if (s.jobs) out.push('<b>' + s.jobs + '</b> project' + (s.jobs === 1 ? '' : 's'));
      if (s.n) out.push('<b>' + s.n + '</b> ' + (s.past ? 'on site' : 'crew'));
      if (s.tasks) out.push('<b>' + s.tasks + '</b> task' + (s.tasks === 1 ? '' : 's'));
      return out.join(' · ');
    }
    function wxOf(iso) { return S.wx[iso] || null; }
    function wxBad(w) { return !!w && ((w.wind_max != null && w.wind_max >= S.th.wind_mph) || (w.gust_max != null && w.gust_max >= S.th.gust_mph) || (w.precip_prob != null && w.precip_prob >= S.th.rain_pct)); }
    function flags(d, iso, all) {
      var f = [], w = wxOf(iso), t = todayISO();
      if (w && iso >= t && parse(iso).getDay() !== 0 && (all || layerOn('jobs'))) {
        var windy = (w.wind_max != null && w.wind_max >= S.th.wind_mph) || (w.gust_max != null && w.gust_max >= S.th.gust_mph);
        var wet = w.precip_prob != null && w.precip_prob >= S.th.rain_pct;
        jobsOn(d, iso).forEach(function (j) {
          var r = j.weather_risk || {};
          if (j.status !== 'active') return;
          if (windy && r.wind) f.push({ sev: 'warn', short: 'Wind ' + r0(w.wind_max) + ' mph', code: j.project_code,
            text: 'Wind ' + r0(w.wind_max) + ' mph' + (w.gust_max != null ? ', gusts ' + r0(w.gust_max) : '') + ' · ' + j.name,
            sub: ((r.access || []).join(', ') || 'Rope access') + '. Plan ground work or move the drop.' });
          if (wet && r.rain) f.push({ sev: 'warn', short: 'Rain ' + w.precip_prob + '%', code: j.project_code,
            text: 'Rain ' + w.precip_prob + '% · ' + j.name, sub: 'Exterior work. Plan inside work or move the day.' });
        });
      }
      on(d, iso, all).forEach(function (x) { if (x.late && x.k === 'task') f.push({ sev: 'crit', short: 'Overdue task', text: 'Overdue: ' + x.title, sub: x.names.length ? 'Assigned to ' + x.names.join(', ') : 'Nobody assigned', item: x }); });
      if (iso === t) {
        var seen = {}, od = [];
        entries(d).concat((d.overdue || []).map(function (it) { return { src: it, id: 'i' + it.id, k: 'task', late: true, title: it.title, names: (it.assignee_names || []), ids: it.assignee_ids || [], mine: !!it.mine, from: it.due_date, to: it.due_date }; }))
          .forEach(function (x) { if (x.late && x.k === 'task' && x.from < t && !seen[x.id] && visible(x, all)) { seen[x.id] = 1; od.push(x); } });
        if (od.length) f.push({ sev: 'crit', short: od.length + ' overdue', text: plural(od.length, 'overdue task') + ' from earlier days',
          sub: od.slice(0, 4).map(function (x) { return x.title + (x.names.length ? ' (' + x.names[0] + ')' : ''); }).join(' · ') + (od.length > 4 ? ' · +' + (od.length - 4) + ' more' : ''), list: od });
      }
      return f;
    }
    function sev(f) { return f.some(function (x) { return x.sev === 'crit'; }) ? 'crit' : f.some(function (x) { return x.sev === 'warn'; }) ? 'warn' : ''; }

    /* ---------- pieces ---------- */
    function mini(x) {
      if (x.k === 'mine' || !x.names.length) return '';
      var me = x.mine && S.who !== 'me';
      return '<span class="jc-mini' + (x.mine ? ' me' : '') + '" title="' + esc(x.names.join(', ')) + '">' + esc(me && x.names.length === 1 ? 'You' : initials(x.names[0])) + (x.names.length > 1 ? '+' : '') + '</span>';
    }
    function chip(x, iso, extra, noTime) {
      var tm = !noTime && x.t && isTimed(x, iso) ? '<small class="jc-tm">' + t12(x.t) + '</small>' : '';
      return '<button type="button" class="jc-chip k-' + x.k + (x.late && !x.done ? ' late' : '') + (x.done ? ' done' : '') + '" data-ent="' + esc(x.id) + '" title="' + esc(x.title + (x.pname ? ' · ' + x.pname : '') + (x.names.length ? ' · ' + x.names.join(', ') : '') + (x.k === 'mine' ? ' · only you see this' : '')) + '">' +
        ic(x.k) + tm + '<span class="jc-tt">' + esc(x.title) + '</span>' + (extra || '') + mini(x) + '</button>';
    }
    function wIcon(w) { var k = wxKind(w); return '<span class="jc-wi ' + (wxBad(w) ? 'bad' : k === 'sun' ? 'sun' : '') + '">' + ic(k) + '</span>'; }
    function wxShort(w) { return w && w.temp_max != null ? '<span class="jc-wx' + (wxBad(w) ? ' bad' : '') + '" title="NYC · ' + esc(w.label) + ' · ' + r0(w.temp_max) + '° / ' + r0(w.temp_min) + '°' + (w.wind_max != null ? ' · wind ' + r0(w.wind_max) + ' mph' : '') + (w.precip_prob != null ? ' · rain ' + w.precip_prob + '%' : '') + '">' + wIcon(w) + r0(w.temp_max) + '°</span>' : ''; }
    function wxLine(w) {
      if (w.wind_max != null && w.wind_max >= S.th.wind_mph || w.gust_max != null && w.gust_max >= S.th.gust_mph) return 'Wind ' + r0(w.wind_max) + ' mph · gusts ' + r0(w.gust_max);
      if (w.precip_prob != null && w.precip_prob >= S.th.rain_pct) return 'Rain ' + w.precip_prob + '% · wind ' + r0(w.wind_max) + ' mph';
      return esc(w.label) + (w.wind_max != null ? ' · wind ' + r0(w.wind_max) : '') + (w.precip_prob != null ? ' · rain ' + w.precip_prob + '%' : '');
    }
    function pills(f, max) {
      var out = f.slice(0, max).map(function (x) { return '<span class="jc-pill ' + x.sev + '">' + ic(x.sev) + esc(x.short) + '</span>'; }).join('');
      return out + (f.length > max ? '<span class="jc-pill">+' + (f.length - max) + '</span>' : '');
    }
    function segs(d, rowDays) {
      if (!layerOn('jobs')) return { list: [], lanes: 0 };
      var a = rowDays[0], b = rowDays[rowDays.length - 1], lanes = [], list = [];
      (d.jobs || []).slice().sort(function (x, y) { return x.start_date < y.start_date ? -1 : x.start_date > y.start_date ? 1 : (y.days || 999) - (x.days || 999); }).forEach(function (j) {
        var je = j.open_ended ? '9999-12-31' : j.end_date;
        if (!je || je < a || j.start_date > b) return;
        var c0 = Math.max(0, rowDays.indexOf(j.start_date) >= 0 ? rowDays.indexOf(j.start_date) : 0), c1 = rowDays.length - 1;
        for (var i = rowDays.length - 1; i >= 0; i--) if (rowDays[i] <= je) { c1 = i; break; }
        var lane = 0; while (lanes[lane] && lanes[lane].some(function (r) { return !(c1 < r[0] || c0 > r[1]); })) lane++;
        (lanes[lane] = lanes[lane] || []).push([c0, c1]);
        list.push({ j: j, c0: c0, c1: c1, lane: lane, starts: j.start_date >= a, ends: !j.open_ended && je <= b });
      });
      return { list: list, lanes: lanes.length };
    }
    function bar(s, col, row, compact) {
      var j = s.j, tail = '';
      if (!compact && s.ends && j.end_date) tail = '<small>ends ' + DOW[parse(j.end_date).getDay()] + '</small>';
      else if (!compact && s.starts) tail = '<small>starts ' + DOW[parse(j.start_date).getDay()] + '</small>';
      return '<div class="jc-bar c' + (j.color || 0) + (s.starts ? '' : ' cont') + (s.ends ? '' : ' goes') + (j.open_ended && !s.ends ? ' fade' : '') + (j.status !== 'active' ? ' closed' : '') +
        '" data-job="' + esc(j.project_code) + '" role="button" tabindex="0" style="grid-column:' + col + ';grid-row:' + row + '">' + esc(j.name) + tail +
        (host.canDrag ? '<span class="cal-grip" title="Drag to change the anticipated completion"></span>' : '') + '</div>';
    }
    function cls(iso) { var g = parse(iso).getDay(); return (g === 0 || g === 6 ? ' wknd' : '') + (iso === todayISO() ? ' today' : ''); }

    /* ---------- desktop: week ---------- */
    function renderWeek(d, ds) {
      var h = '<div class="jc-wk">';
      h += '<div class="jc-row jc-heads"><div class="jc-gut"></div>' + ds.map(function (iso) {
        var f = flags(d, iso), sv = sev(f), x = parse(iso);
        return '<button type="button" class="jc-wh' + cls(iso) + (sv ? ' s-' + sv : '') + '" data-day="' + iso + '"><span class="jc-whd"><span class="n">' + x.getDate() + '</span><span class="dow">' + DOW[x.getDay()] + '</span></span>' +
          '<span class="jc-sum">' + (summaryHtml(d, iso) || '&nbsp;') + '</span><span class="jc-flags">' + pills(f.filter(function (z) { return !z.list; }).concat(f.filter(function (z) { return z.list; })), 2) + '</span></button>';
      }).join('') + '</div>';
      // NYC weather row
      h += '<div class="jc-band jc-wxrow"><div class="jc-gut">NYC</div>' + ds.map(function (iso, k) {
        var w = wxOf(iso);
        var inner = w && w.temp_max != null ? wIcon(w) + '<span class="tp">' + r0(w.temp_max) + '° <small>/ ' + r0(w.temp_min) + '°</small></span><span class="dt">' + wxLine(w) + '</span>' : '<span class="none">' + (S.wxLoading ? '' : 'No forecast') + '</span>';
        return '<div class="jc-cell' + cls(iso) + '" style="grid-column:' + (k + 2) + '"></div><div class="jc-wxc' + (wxBad(w) ? ' bad' : '') + '" style="grid-column:' + (k + 2) + ';grid-row:1">' + inner + '</div>';
      }).join('') + '</div>';
      var sg = segs(d, ds);
      if (layerOn('jobs')) {
        h += '<div class="jc-band jc-jobs" data-cols="7" style="grid-template-rows:repeat(' + Math.max(1, sg.lanes) + ',30px)"><div class="jc-gut">PROJECTS</div>' + ds.map(function (iso, k) { return '<div class="jc-cell' + cls(iso) + '" style="grid-column:' + (k + 2) + '" data-day="' + iso + '"></div>'; }).join('') +
          sg.list.map(function (s) { return bar(s, (s.c0 + 2) + '/' + (s.c1 + 3), s.lane + 1); }).join('') + (sg.list.length ? '' : '<div class="jc-none" style="grid-column:2/9;grid-row:1">No projects in this week</div>') + '</div>';
      }
      if (ds.some(function (iso) { return untimedOn(d, iso).length; })) {
        h += '<div class="jc-band"><div class="jc-gut">TASKS</div>' + ds.map(function (iso, k) {
          return '<div class="jc-cell' + cls(iso) + '" style="grid-column:' + (k + 2) + '" data-day="' + iso + '"></div><div class="jc-due" style="grid-column:' + (k + 2) + ';grid-row:1">' + untimedOn(d, iso).map(function (x) { return chip(x, iso); }).join('') + '</div>';
        }).join('') + '</div>';
      }
      // the hours
      var H0 = 7, H1 = 19;
      ds.forEach(function (iso) { timedOn(d, iso).forEach(function (x) { var s = minOf(x.t), e = s + dur(x); H0 = Math.min(H0, Math.floor(s / 60)); H1 = Math.max(H1, Math.ceil(e / 60)); }); });
      H0 = Math.max(0, H0); H1 = Math.min(24, H1);
      var PX = 40, now = new Date(), nowMin = now.getHours() * 60 + now.getMinutes();
      h += '<div class="jc-tg"><div class="jc-hours" style="height:' + (H1 - H0) * PX + 'px">';
      h += '<span class="first">' + t12(H0 + ':00') + '</span>';
      for (var hr = H0 + 1; hr < H1; hr++) h += '<span style="top:' + (hr - H0) * PX + 'px">' + t12(hr + ':00') + '</span>';
      h += '</div>' + ds.map(function (iso) {
        var out = '<div class="jc-col' + cls(iso) + '" data-day="' + iso + '">';
        layout(timedOn(d, iso)).forEach(function (x) {
          var s = minOf(x.t), top = (s - H0 * 60) / 60 * PX, ht = Math.max(22, dur(x) / 60 * PX - 2), w = 100 / x._n;
          out += '<button type="button" class="jc-ev k-' + x.k + (x.done ? ' done' : '') + (x.late && !x.done ? ' late' : '') + '" data-ent="' + esc(x.id) + '" style="top:' + top + 'px;height:' + ht + 'px;left:calc(' + (x._c * w) + '% + 2px);width:calc(' + w + '% - 4px)" title="' + esc(x.title + (x.names.length ? ' · ' + x.names.join(', ') : '')) + '">' +
            '<span class="tt">' + esc(x.title) + '</span>' + (ht > 30 ? '<span class="t">' + t12(x.t) + (x.names.length ? ' · ' + esc(x.mine && x.names.length === 1 ? 'You' : x.names[0]) : '') + '</span>' : '') + '</button>';
        });
        if (iso === todayISO() && nowMin > H0 * 60 && nowMin < H1 * 60) out += '<div class="jc-now" style="top:' + ((nowMin - H0 * 60) / 60 * PX) + 'px"></div>';
        return out + '</div>';
      }).join('') + '</div></div>';
      return h;
    }
    function dur(x) { if (x.e && x.t && minOf(x.e) > minOf(x.t)) return minOf(x.e) - minOf(x.t); return x.k === 'task' ? 30 : 60; }
    function layout(list) {   // side by side when they overlap
      var groups = [], cur = null;
      list.forEach(function (x) { var s = minOf(x.t), e = s + dur(x); if (!cur || s >= cur.end) { cur = { end: e, items: [] }; groups.push(cur); } cur.end = Math.max(cur.end, e); cur.items.push(x); });
      groups.forEach(function (g) {
        var ends = [];
        g.items.forEach(function (x) { var s = minOf(x.t), c = -1; for (var i = 0; i < ends.length; i++) if (ends[i] <= s) { c = i; break; } if (c < 0) { c = ends.length; ends.push(0); } ends[c] = s + dur(x); x._c = c; });
        g.items.forEach(function (x) { x._n = ends.length; });
      });
      return list;
    }

    /* ---------- desktop: month ---------- */
    function renderMonth(d, ds) {
      var m = S.anchor.getMonth(), h = '<div class="jc-mo"><div class="jc-mo-dow">' + ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(function (x) { return '<div>' + x + '</div>'; }).join('') + '</div>';
      for (var i = 0; i < ds.length; i += 7) {
        var row = ds.slice(i, i + 7), sg = segs(d, row), L = Math.min(sg.lanes, 3), over = {};
        sg.list.forEach(function (s) { if (s.lane >= 3) for (var c = s.c0; c <= s.c1; c++) over[c] = (over[c] || 0) + 1; });
        h += '<div class="jc-mw" data-cols="7" style="grid-template-rows:32px repeat(' + L + ',22px) minmax(40px,auto)">';
        row.forEach(function (iso, k) {
          var x = parse(iso), f = flags(d, iso), sv = sev(f), s = summary(d, iso);
          h += '<button type="button" class="jc-mc' + cls(iso) + (x.getMonth() !== m ? ' other' : '') + (sv ? ' s-' + sv : '') + '" data-day="' + iso + '" style="grid-column:' + (k + 1) + '" aria-label="' + esc(longDate(iso)) + '"></button>';
          h += '<div class="jc-md' + (iso === todayISO() ? ' today' : '') + '" style="grid-column:' + (k + 1) + '"><span class="n">' + x.getDate() + '</span>' + (x.getDate() === 1 ? '<small>' + MON[x.getMonth()] + '</small>' : '') +
            (sv ? '<span class="jc-fl ' + sv + '" title="' + esc(f.map(function (z) { return z.short; }).join(', ')) + '">' + ic(sv) + '</span>' : '') + (s.n ? '<span class="jc-hc">' + s.n + (s.past ? ' on site' : ' crew') + '</span>' : '') + wxShort(wxOf(iso)) + '</div>';
          var its = untimedOn(d, iso).concat(timedOn(d, iso)), show = its.slice(0, 3);
          h += '<div class="jc-mchips" style="grid-column:' + (k + 1) + ';grid-row:' + (L + 2) + '">' + (over[k] ? '<button type="button" class="jc-more" data-day="' + iso + '">+' + over[k] + ' project' + (over[k] === 1 ? '' : 's') + '</button>' : '') + show.map(function (z) { return chip(z, iso); }).join('') + (its.length > 3 ? '<button type="button" class="jc-more" data-day="' + iso + '">+' + (its.length - 3) + ' more</button>' : '') + '</div>';
        });
        sg.list.forEach(function (s) { if (s.lane < 3) h += bar(s, (s.c0 + 1) + '/' + (s.c1 + 2), s.lane + 2, s.c1 - s.c0 < 1); });
        h += '</div>';
      }
      return h + '</div>';
    }

    /* ---------- day (desktop view + the panel) ---------- */
    function dayHtml(d, iso, all, wide) {
      var w = wxOf(iso), f = flags(d, iso, all), r = rel(iso);
      var head = '<div class="jc-dd-head"><h3 class="jc-dd-date">' + (r ? r + ' · ' : '') + esc(longDate(iso)) + '</h3><div class="jc-dd-meta">' + (summaryHtml(d, iso, all) || 'Nothing planned') + '</div></div>';
      var att = f.length ? '<section><h4>Needs attention · ' + f.length + '</h4><div class="jc-att">' + f.map(function (x) {
        var go = x.code ? '<a class="jc-att-go" href="/projects/' + encodeURIComponent(x.code) + '">Open project</a>' : x.item ? '<button type="button" class="jc-att-go" data-ent="' + esc(x.item.id) + '">Open task</button>' : '';
        return '<div class="jc-att-row ' + x.sev + '">' + ic(x.sev) + '<div><b>' + esc(x.text) + '</b><span>' + esc(x.sub) + '</span></div>' + go + '</div>';
      }).join('') + '</div></section>' : '';
      var running = (all || layerOn('jobs')) ? jobsOn(d, iso) : [];
      var wxs = '<section><h4>' + ic('cloud') + 'Weather · New York City</h4>' + (w && w.temp_max != null ?
        '<div class="jc-wxbig' + (wxBad(w) ? ' bad' : '') + '">' + wIcon(w) + '<span class="tp">' + r0(w.temp_max) + '° <small>/ ' + r0(w.temp_min) + '° · ' + esc(w.label) + '</small></span><span class="dt">' +
          (w.wind_max != null ? 'Wind ' + r0(w.wind_max) + ' mph' + (w.gust_max != null ? ', gusts ' + r0(w.gust_max) : '') : '') + (w.precip_prob != null ? ' · rain ' + w.precip_prob + '%' : '') + '</span></div>' +
          (iso >= todayISO() && running.length ? '<div class="jc-wxjobs">' + running.filter(function (j) { return j.status === 'active'; }).map(function (j) {
            var rk = j.weather_risk || {}, windy = (w.wind_max != null && w.wind_max >= S.th.wind_mph) || (w.gust_max != null && w.gust_max >= S.th.gust_mph), wet = w.precip_prob != null && w.precip_prob >= S.th.rain_pct;
            var risk = rk.wind && windy ? 'Too windy' : rk.rain && wet ? 'Rain risk' : '';
            return '<div class="jc-wxj"><span><b>' + esc(j.name) + '</b> <small>' + esc((rk.access || []).join(', ') || (rk.rain ? 'Exterior' : 'Weather doesn\'t stop it')) + '</small></span>' + (risk ? '<span class="jc-pill warn">' + ic('warn') + risk + '</span>' : '<span class="jc-ok">OK to work</span>') + '</div>';
          }).join('') + '</div>' : '')
        : '<div class="jc-empty">' + (S.wxLoading ? 'Loading the forecast…' : 'No forecast for this day (the forecast covers the last month and the next 16 days).') + '</div>') + '</section>';
      var who = (all || layerOn('jobs')) ? '<section><h4>Projects · ' + running.length + '</h4>' + (running.length ? '<div class="jc-who">' + running.map(function (j) {
        var n = people(d, iso, j), past = iso <= todayISO();
        return '<a class="jc-who-row" href="/projects/' + encodeURIComponent(j.project_code) + '"><span class="jc-sq c' + (j.color || 0) + '"></span><span class="nm">' + esc(j.name) + '<small>' + esc([j.project_code, j.client_name, j.pm ? 'PM ' + j.pm : ''].filter(Boolean).join(' · ')) + '</small></span>' +
          '<span class="cnt">' + (n ? n + (past ? ' on site' : ' crew') : (past ? 'Nobody signed in' : (workday(iso) ? 'No crew yet' : ''))) + '</span></a>';
      }).join('') + '</div>' : '<div class="jc-empty">No project is scheduled on this day.</div>') + '</section>' : '';
      var ut = untimedOn(d, iso, all), tl = timedOn(d, iso, all);
      var tasks = '<section><h4>' + ic('task') + 'Tasks' + (!all && S.who ? ' · ' + esc(whoLabel()) : '') + '</h4>' + (ut.length ? '<div class="jc-list">' + ut.map(function (x) {
        return '<div class="jc-li"><span class="jc-li-t">' + (x.k === 'task' ? (x.done ? 'Done' : x.late ? 'Overdue' : 'To do') : 'All day') + '</span>' + chip(x, iso) + '</div>';
      }).join('') + '</div>' : '<div class="jc-empty">No tasks on this day.</div>') + '</section>';
      var sched = '<section><h4>' + ic('meet') + 'Meetings &amp; events</h4>' + (tl.length ? '<div class="jc-list">' + tl.map(function (x) {
        return '<div class="jc-li"><span class="jc-li-t">' + t12(x.t) + '</span>' + chip(x, iso, '', true) + '</div>';
      }).join('') + '</div>' : '<div class="jc-empty">Nothing at a set time.</div>') + (tl.some(function (x) { return x.k === 'mine'; }) ? '<p class="jc-private">' + ic('mine') + 'Dashed items are from your Google Calendar. Only you see them.</p>' : '') + '</section>';
      var addb = '<div><button type="button" class="btn-primary" data-add="' + iso + '">' + ic('plus') + 'Task or event on ' + esc(shortDate(iso)) + '</button></div>';
      if (wide) return '<div class="jc-dayv"><div class="jc-card jc-dd">' + head + att + tasks + sched + '</div><div class="jc-card jc-dd">' + wxs + who + addb + '</div></div>';
      return '<div class="jc-dd">' + head + att + wxs + who + tasks + sched + addb + '</div>';
    }

    /* ---------- phone: list + month ---------- */
    function dots(d, iso) {
      var k = {}; if (layerOn('jobs') && jobsOn(d, iso).some(function (j) { return j.status === 'active'; })) k.jobs = 1;
      on(d, iso).forEach(function (x) { k[x.k] = 1; });
      return ['jobs', 'task', 'meet', 'mine'].filter(function (z) { return k[z]; }).map(function (z) { return '<i class="k-' + z + '"></i>'; }).join('');
    }
    function agDay(d, iso) {
      var x = parse(iso), r = rel(iso), w = wxOf(iso), f = flags(d, iso);
      var js = layerOn('jobs') ? jobsOn(d, iso).filter(function (j) { return j.status === 'active' || iso <= todayISO(); }) : [];
      var its = untimedOn(d, iso).concat(timedOn(d, iso));
      var h = '<section class="jc-ag-day' + (iso === todayISO() ? ' today' : '') + '" id="jc-ag-' + iso + '" data-iso="' + iso + '">' +
        '<button type="button" class="jc-ag-h" data-day="' + iso + '"><span class="jc-ag-dt"><span class="dow">' + DOW[x.getDay()].toUpperCase() + '</span><span class="n">' + x.getDate() + '</span></span>' +
        '<span class="jc-ag-mid"><span class="t">' + (r ? r + ' · ' : '') + DOWL[x.getDay()] + '</span><span class="s">' + (summaryHtml(d, iso) || 'Nothing planned') + '</span></span>' +
        (w && w.temp_max != null ? '<span class="jc-ag-wx">' + '<span class="jc-wx">' + wIcon(w) + r0(w.temp_max) + '° / ' + r0(w.temp_min) + '°</span><small class="' + (wxBad(w) ? 'bad' : '') + '">' + (w.wind_max != null && (w.wind_max >= S.th.wind_mph || w.gust_max >= S.th.gust_mph) ? 'Wind ' + r0(w.wind_max) + ' mph' : w.precip_prob != null && w.precip_prob >= S.th.rain_pct ? 'Rain ' + w.precip_prob + '%' : esc(w.label)) + '</small></span>' : '<span></span>') + '</button><div class="jc-ag-b">';
      h += f.map(function (z) { return '<button type="button" class="jc-ag-flag ' + z.sev + '" data-day="' + iso + '">' + ic(z.sev) + '<span>' + esc(z.text) + '</span></button>'; }).join('');
      if (js.length) h += '<div class="jc-ag-jobs">' + js.map(function (j) { var n = people(d, iso, j); return '<a class="jc-jp" href="/projects/' + encodeURIComponent(j.project_code) + '"><i class="c' + (j.color || 0) + '"></i><b>' + esc(j.name) + '</b>' + (n ? ' ' + n : '') + '</a>'; }).join('') + '</div>';
      h += its.map(function (z) { return '<div class="jc-li"><span class="jc-li-t">' + (isTimed(z, iso) ? t12(z.t) : z.k === 'task' ? (z.done ? 'Done' : 'To do') : 'All day') + '</span>' + chip(z, iso, '', true) + '</div>'; }).join('');
      return h + '</div></section>';
    }
    function renderList(d, ds) {
      var h = '<div class="jc-strip" id="jc-strip">' + ds.map(function (iso) {
        var x = parse(iso), w = wxOf(iso), sv = sev(flags(d, iso));
        return '<button type="button" class="jc-sd' + cls(iso) + (iso === S.sel ? ' sel' : '') + '" data-pick="' + iso + '"><span class="l">' + DOW[x.getDay()][0] + '</span><span class="n">' + x.getDate() + '</span>' +
          '<span class="w' + (wxBad(w) ? ' bad' : '') + '">' + (w && w.temp_max != null ? wIcon(w) + r0(w.temp_max) + '°' : '') + '</span><span class="jc-dots">' + dots(d, iso) + (sv ? '<i class="f-' + sv + '"></i>' : '') + '</span></button>';
      }).join('') + '</div><div class="jc-ag">' + ds.map(function (iso) { return agDay(d, iso); }).join('') + '</div><p class="jc-hint">Swipe left or right for another week</p>';
      return h;
    }
    function renderPhoneMonth(d, ds) {
      var m = S.anchor.getMonth(), sel = S.sel && ds.indexOf(S.sel) >= 0 ? S.sel : (ds.indexOf(todayISO()) >= 0 ? todayISO() : isoOf(new Date(S.anchor.getFullYear(), m, 1)));
      S.sel = sel;
      var h = '<div class="jc-pm">' + ['M', 'T', 'W', 'T', 'F', 'S', 'S'].map(function (x) { return '<div class="h">' + x + '</div>'; }).join('') + ds.map(function (iso) {
        var x = parse(iso), w = wxOf(iso), sv = sev(flags(d, iso));
        return '<button type="button" class="jc-pc' + cls(iso) + (x.getMonth() !== m ? ' other' : '') + (sv ? ' s-' + sv : '') + (iso === sel ? ' sel' : '') + '" data-pick="' + iso + '" aria-label="' + esc(longDate(iso)) + '"><span class="n">' + x.getDate() + '</span><span class="jc-dots">' + dots(d, iso) + '</span><span class="w">' + (w && w.temp_max != null ? wIcon(w) + r0(w.temp_max) + '°' : '') + '</span></button>';
      }).join('') + '</div><div class="jc-ag jc-pm-day">' + agDay(d, sel) + '</div>';
      return h;
    }

    /* ---------- head, chips, filter ---------- */
    function people_opts(d) {
      var seen = {}, out = [];
      (d && d.items || []).forEach(function (it) {
        var ids = (it.assignee_ids || []).concat(it.attendee_ids || []), names = (it.assignee_names || []).concat(it.attendee_names || []);
        ids.forEach(function (id, i) { if (id != null && !seen[id] && names[i]) { seen[id] = 1; out.push([String(id), names[i]]); } });
      });
      return out.sort(function (a, b) { return a[1].localeCompare(b[1]); });
    }
    function whoLabel() { if (S.who === 'me') return 'Me'; var o = people_opts(S.data).find(function (p) { return p[0] === S.who; }); return o ? o[1] : 'Someone'; }
    function title(ds) {
      var v = view(), a = S.anchor;
      if (v === 'month') return MONL[a.getMonth()] + ' ' + a.getFullYear();
      if (v === 'day') return longDate(isoOf(a));
      var f = parse(ds[0]), l = parse(ds[ds.length - 1]);
      return MON[f.getMonth()] + ' ' + f.getDate() + ' – ' + (f.getMonth() !== l.getMonth() ? MON[l.getMonth()] + ' ' : '') + l.getDate() + ', ' + l.getFullYear();
    }
    function renderHead(d, ds) {
      $('cal-range').textContent = title(ds);
      var opts = S.phone ? [['list', 'List'], ['month', 'Month']] : [['day', 'Day'], ['week', 'Week'], ['month', 'Month']];
      $('jc-seg').innerHTML = opts.map(function (x) { return '<button type="button" data-cal-view="' + x[0] + '" aria-pressed="' + (view() === x[0]) + '" class="' + (view() === x[0] ? 'on' : '') + '">' + x[1] + '</button>'; }).join('');
      var v = view(), t = new Date(), here = v === 'month' ? (S.anchor.getMonth() === t.getMonth() && S.anchor.getFullYear() === t.getFullYear()) : v === 'day' ? isoOf(S.anchor) === todayISO() : ds.indexOf(todayISO()) >= 0;
      $('cal-today').innerHTML = host.nowLabel ? host.nowLabel(ds, v, S.anchor) : 'Today'; $('cal-today').classList.toggle('here', here);
      $('cal-today').title = 'Back to ' + (v === 'month' ? 'this month' : v === 'day' ? 'today' : 'this week');
      var c = { jobs: 0, task: 0, meet: 0, mine: 0 }, a = ds[0], b = ds[ds.length - 1];
      if (v === 'day') { a = b = isoOf(S.anchor); }
      if (d) {
        c.jobs = (d.jobs || []).filter(function (j) { return j.start_date <= b && (j.open_ended || (j.end_date && j.end_date >= a)); }).length;
        entries(d).forEach(function (x) { if (x.from <= b && x.to >= a && (x.k === 'mine' || !S.who || (S.who === 'me' ? x.mine : x.ids.map(String).indexOf(S.who) >= 0))) c[x.k]++; });
      }
      var opts2 = people_opts(d);
      if (S.who && S.who !== 'me' && !opts2.some(function (p) { return p[0] === S.who; })) opts2.unshift([S.who, 'Someone not in view']);
      $('jc-layers').innerHTML = KINDS.filter(function (k) { return k[0] !== 'mine' || (d && d.mine_connected); }).map(function (k) {
        return '<button type="button" class="jc-lay k-' + k[0] + '" data-layer="' + k[0] + '" aria-pressed="' + layerOn(k[0]) + '"><span class="sw">' + ic(k[2]) + '</span>' + k[1] + ' <span class="n">' + c[k[0]] + '</span></button>';
      }).join('') +
        '<label class="jc-whosel" for="jc-who">Assigned to <select id="jc-who"><option value="">Everyone</option><option value="me"' + (S.who === 'me' ? ' selected' : '') + '>Me</option>' +
        opts2.map(function (p) { return '<option value="' + esc(p[0]) + '"' + (S.who === p[0] ? ' selected' : '') + '>' + esc(p[1]) + '</option>'; }).join('') + '</select></label>' +
        '<span class="jc-key"><span><i class="warn"></i>Weather risk</span><span><i class="crit"></i>Overdue</span></span>';
      $('cal-sub').textContent = S.phone ? '' : 'Projects, tasks and NYC weather · click a day for everything on it' + (host.canDrag && v !== 'day' ? ' · drag a bar to move a project' : '');
      $('cal-note').innerHTML = d && d.undated && d.undated.length ? '<div class="cal-note">' + plural(d.undated.length, 'active project') + ' ' + (d.undated.length === 1 ? 'has' : 'have') + ' no start date yet, so ' + (d.undated.length === 1 ? 'it is' : 'they are') + ' not on the calendar: ' + d.undated.map(function (u) { return '<b>' + esc(u.project_code) + '</b>'; }).join(', ') + '. Set the dates under <a class="cp-link" href="/admin/projects">Crew &amp; site</a>.</div>' : '';
    }

    /* ---------- load + render ---------- */
    async function load() {
      var r = range(), a = isoOf(r[0]), b = isoOf(r[1]), key = a + '|' + b, my = ++S.seq;
      renderHead(S.data && S.data._key === key ? S.data : null, days(r[0], r[1]));
      if (!S.cache[key]) $('jc-view').innerHTML = '<div class="cp-skel"></div>';
      try {
        var d = S.cache[key] || (await host.api('/calendar?from=' + a + '&to=' + b)).data;
        if (my !== S.seq) return;
        d._key = key; S.cache[key] = d; S.data = d; host.onData && host.onData(d);
        render();
        loadWeather(a, b, my);
      } catch (e) {
        if (my !== S.seq) return;
        $('jc-view').innerHTML = '<div class="jc-empty">Could not load the calendar · ' + esc(e.message) + '</div>';
      }
    }
    async function loadWeather(a, b, my) {
      var key = a + '|' + b;
      if (S.wxKeys[key]) return;
      S.wxLoading = true;
      try {
        var w = (await host.api('/calendar/weather?from=' + a + '&to=' + b)).data || {};
        Object.keys(w.days || {}).forEach(function (k) { S.wx[k] = w.days[k]; });
        if (w.thresholds) S.th = w.thresholds;
        S.wxKeys[key] = Date.now();
      } catch (e) { /* the calendar works without weather */ }
      S.wxLoading = false;
      if (my === S.seq) render(true);
      if (S.panel) drawPanel();
    }
    function render(keep) {
      var d = S.data; if (!d) return;
      var r = range(), ds = days(r[0], r[1]), v = view();
      renderHead(d, ds);
      root.className = 'jc v-' + v + (S.phone ? ' phone' : '');
      var el = $('jc-view'), y = window.scrollY;
      if (v === 'month') el.innerHTML = S.phone ? renderPhoneMonth(d, ds) : renderMonth(d, ds);
      else if (v === 'day') el.innerHTML = dayHtml(d, isoOf(S.anchor), false, true);
      else if (v === 'list') { if (!S.sel || ds.indexOf(S.sel) < 0) S.sel = ds.indexOf(todayISO()) >= 0 ? todayISO() : ds[0]; S.firstOfWeek = ds[0]; el.innerHTML = renderList(d, ds); }
      else el.innerHTML = renderWeek(d, ds);
      wire(el, d);
      if (v === 'list') { stickTop(); if (!keep) requestAnimationFrame(function () { scrollToDay(S.sel, false); }); else window.scrollTo(0, y); }
    }
    function stickTop() {
      var s = $('jc-strip'); if (!s) return;
      var top = 0;
      document.querySelectorAll('.ssc-top, .ssc-side').forEach(function (el) { var cs = getComputedStyle(el); if (cs.position === 'sticky' || cs.position === 'fixed') top = Math.max(top, el.offsetHeight); });
      s.style.top = top + 'px'; S.stripTop = top;
    }
    function scrollToDay(iso, smooth) {
      var sec = document.getElementById('jc-ag-' + iso), strip = $('jc-strip'); if (!sec) return;
      var y = sec.getBoundingClientRect().top + window.scrollY - (S.stripTop || 0) - (strip ? strip.offsetHeight : 0) - 4;
      if (iso === S.firstOfWeek) y = Math.min(y, root.getBoundingClientRect().top + window.scrollY - (S.stripTop || 0));
      window.scrollTo({ top: Math.max(0, y), behavior: smooth ? 'smooth' : 'auto' });
    }
    function find(d, id) { return entries(d).find(function (x) { return x.id === id; }) || ((d.overdue || []).filter(function (it) { return 'i' + it.id === id; }).map(function (it) { return { src: it, k: 'task' }; })[0]); }
    function openEnt(d, id) {
      var x = find(d, id); if (!x) return;
      closePanel(true);
      if (x.k === 'mine') host.openMine(x.src); else host.openItem(x.src);
    }
    function wire(el, d) {
      el.querySelectorAll('.jc-bar').forEach(function (b) {
        var j = (d.jobs || []).find(function (z) { return z.project_code === b.dataset.job; });
        b.addEventListener('click', function () { if (b.dataset.dragged) { delete b.dataset.dragged; return; } host.openJob(b.dataset.job); });
        b.addEventListener('keydown', function (e) { if (e.key === 'Enter') host.openJob(b.dataset.job); });
        if (host.canDrag && !S.phone) drag(b, j);
        b.addEventListener('mousemove', function (e) { tip(e, j); });
        b.addEventListener('mouseleave', function () { var t = $('cal-tip'); if (t) t.style.display = 'none'; });
      });
    }

    /* ---------- drag a bar: move the project, or its right edge to change the anticipated completion ---------- */
    function drag(el, j) {
      if (!j) return;
      var st = null;
      el.addEventListener('pointerdown', function (ev) {
        if (ev.button !== 0 || ev.pointerType === 'touch') return;
        var band = el.parentElement, cols = 7, gut = band.classList.contains('jc-band') ? (band.querySelector('.jc-gut') || { offsetWidth: 0 }).offsetWidth : 0;
        st = { grip: ev.target.classList.contains('cal-grip'), x0: ev.clientX, moved: 0, width: el.getBoundingClientRect().width, dayW: (band.getBoundingClientRect().width - gut) / cols };
        el.setPointerCapture(ev.pointerId); ev.preventDefault();
      });
      el.addEventListener('pointermove', function (ev) {
        if (!st) return; var delta = Math.round((ev.clientX - st.x0) / st.dayW);
        if (delta === st.moved) return; st.moved = delta; el.classList.add('dragging'); var t = $('cal-tip'); if (t) t.style.display = 'none';
        if (st.grip) el.style.width = Math.max(st.dayW * 0.6, st.width + delta * st.dayW) + 'px'; else el.style.transform = 'translateX(' + (delta * st.dayW) + 'px)';
      });
      var done = async function () {
        if (!st) return; var delta = st.moved, grip = st.grip; st = null; el.classList.remove('dragging'); el.style.transform = ''; el.style.width = '';
        if (!delta) return; el.dataset.dragged = '1'; setTimeout(function () { delete el.dataset.dragged; }, 300);
        var sh = function (s, n) { return isoOf(add(parse(s), n)); };
        var before = { start_date: j.start_date, target_completion: j.end_date };
        var after = grip ? { start_date: j.start_date, target_completion: sh(j.end_date || j.start_date, delta) } : { start_date: sh(j.start_date, delta), target_completion: j.end_date ? sh(j.end_date, delta) : null };
        if (after.target_completion && after.target_completion < after.start_date) { host.toast('The anticipated completion cannot be before the start', true); return; }
        try {
          await host.api('/admin/projects/' + encodeURIComponent(j.project_code) + '/crew', { method: 'PUT', body: JSON.stringify(after) }, true);
          S.cache = {}; await load();
          host.toastUndo(esc(j.name) + ': ' + esc(shortDate(after.start_date)) + ' → ' + (after.target_completion ? esc(shortDate(after.target_completion)) : 'no end'), async function () {
            await host.api('/admin/projects/' + encodeURIComponent(j.project_code) + '/crew', { method: 'PUT', body: JSON.stringify(before) }, true);
            S.cache = {}; load(); host.toast('Restored');
          });
        } catch (e) { host.toast(e.message, true); }
      };
      el.addEventListener('pointerup', done); el.addEventListener('pointercancel', done);
    }
    function tip(ev, j) {
      var t = $('cal-tip'); if (!t || !j || S.phone) return;
      t.innerHTML = '<b>' + esc(j.name) + '</b><span class="m">' + esc(j.client_name || '') + (j.address ? ' · ' + esc(j.address) : '') + '</span><br>' + esc(shortDate(j.start_date)) + ' → ' + (j.open_ended ? 'no end date' : esc(shortDate(j.end_date))) + (j.days ? ' · ' + j.days + ' days' : '') +
        '<br><span class="m">' + (j.pm ? 'PM ' + esc(j.pm) + ' · ' : '') + j.crew_count + ' on the crew' + ((j.weather_risk || {}).access && j.weather_risk.access.length ? ' · ' + esc(j.weather_risk.access.join(', ')) : '') + (j.status !== 'active' ? ' · ' + esc(j.status) : '') + '</span>';
      t.style.display = 'block'; var w = t.offsetWidth, h = t.offsetHeight;
      t.style.left = Math.min(ev.clientX + 14, window.innerWidth - w - 8) + 'px'; t.style.top = (ev.clientY + 16 + h > window.innerHeight ? ev.clientY - h - 10 : ev.clientY + 16) + 'px';
    }

    /* ---------- the day panel (side panel / bottom sheet), also used by the dashboard's week strip ---------- */
    var scrim = document.createElement('div'); scrim.className = 'jc-scrim';
    var panel = document.createElement('aside'); panel.className = 'jc-panel'; panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'true'); panel.setAttribute('aria-labelledby', 'jc-p-title'); panel.hidden = true;
    panel.innerHTML = '<div class="jc-grab" aria-hidden="true"></div><div class="jc-p-bar"><strong id="jc-p-title">Day</strong><button type="button" class="jc-iconbtn" data-close aria-label="Close">' + ic('x') + '</button></div><div class="jc-p-body" id="jc-p-body"></div>';
    document.body.appendChild(scrim); document.body.appendChild(panel);
    var lastFocus = null;
    function openDay(iso, d, opts) {
      if (!iso || !d) return;
      S.panel = { iso: iso, d: d, all: !!(opts && opts.all) };
      lastFocus = document.activeElement;
      drawPanel();
      panel.hidden = false; scrim.classList.add('on');
      requestAnimationFrame(function () { panel.classList.add('on'); });
      setTimeout(function () { var c = panel.querySelector('[data-close]'); if (c) c.focus(); }, 40);
      // the week strip's range may not have weather yet
      if (!wxOf(iso) && iso >= isoOf(add(new Date(), -31)) && iso <= isoOf(add(new Date(), 15))) {
        var m = monday(parse(iso)); loadWeather(isoOf(m), isoOf(add(m, 6)), -1);
      }
    }
    function drawPanel() {
      if (!S.panel) return;
      $('jc-p-title').textContent = shortDate(S.panel.iso);
      $('jc-p-body').innerHTML = dayHtml(S.panel.d, S.panel.iso, S.panel.all, false);
    }
    function closePanel(quiet) {
      if (!S.panel) return;
      S.panel = null; panel.classList.remove('on'); scrim.classList.remove('on');
      setTimeout(function () { if (!S.panel) panel.hidden = true; }, 220);
      if (!quiet && lastFocus && lastFocus.focus) try { lastFocus.focus(); } catch (e) {}
    }
    scrim.addEventListener('click', function () { closePanel(); });
    panel.addEventListener('click', function (e) {
      var t = e.target.closest('button, a'); if (!t) return;
      if (t.hasAttribute('data-close')) return closePanel();
      if (t.dataset.ent && S.panel) { e.preventDefault(); return openEnt(S.panel.d, t.dataset.ent); }
      if (t.dataset.add) { var iso = t.dataset.add; closePanel(true); host.openItem(null, { date: iso }); }
    });
    // swipe the sheet down to close (phone)
    (function () {
      var y0 = null;
      panel.addEventListener('touchstart', function (e) { var b = $('jc-p-body'); y0 = (b && b.scrollTop > 0) ? null : e.touches[0].clientY; }, { passive: true });
      panel.addEventListener('touchend', function (e) { if (y0 != null && e.changedTouches[0].clientY - y0 > 90) closePanel(); y0 = null; }, { passive: true });
    })();
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && S.panel) { e.stopPropagation(); closePanel(); }
    }, true);

    /* ---------- events on the calendar ---------- */
    root.addEventListener('click', function (e) {
      var t = e.target.closest('button, [data-day]'); if (!t || !root.contains(t)) return;
      var d = S.data;
      if (t.dataset.calView) { if (S.phone) S.pview = t.dataset.calView; else S.view = t.dataset.calView; save(); load(); return; }
      if (t.dataset.layer) {
        var k = t.dataset.layer;
        if (k === 'mine') host.setMine(!host.mineOn()); else S.layers[k] = S.layers[k] ? 0 : 1;
        save(); render(true); return;
      }
      if (t.id === 'cal-prev' || t.id === 'cal-next') return step(t.id === 'cal-next' ? 1 : -1);
      if (t.id === 'cal-today') { S.anchor = new Date(); S.sel = todayISO(); load(); return; }
      if (t.id === 'cal-new-item') return host.openItem(null, { date: view() === 'day' ? isoOf(S.anchor) : todayISO() });
      if (t.id === 'cal-print') return print();
      if (!d) return;
      if (t.dataset.ent) return openEnt(d, t.dataset.ent);
      if (t.dataset.add) return host.openItem(null, { date: t.dataset.add });
      if (t.dataset.pick) {
        S.sel = t.dataset.pick;
        if (view() === 'list') { root.querySelectorAll('.jc-sd').forEach(function (b) { b.classList.toggle('sel', b.dataset.pick === S.sel); }); scrollToDay(S.sel, true); }
        else render(true);
        return;
      }
      if (t.closest('.jc-bar')) return;
      if (t.dataset.day) openDay(t.dataset.day, d);
    });
    root.addEventListener('change', function (e) { if (e.target.id === 'jc-who') { S.who = e.target.value; save(); render(true); } });
    function step(dir) {
      var v = view(), a = S.anchor;
      if (v === 'month') S.anchor = new Date(a.getFullYear(), a.getMonth() + dir, 1);
      else if (v === 'day') S.anchor = add(a, dir);
      else { S.anchor = add(monday(a), dir * 7); S.sel = isoOf(S.anchor); }
      var t = new Date();
      if (v === 'month' && S.anchor.getMonth() === t.getMonth() && S.anchor.getFullYear() === t.getFullYear()) S.anchor = t;
      if ((v === 'week' || v === 'list') && isoOf(monday(S.anchor)) === isoOf(monday(t))) { S.anchor = t; S.sel = todayISO(); }
      load();
    }
    // swipe between weeks / months on a phone
    (function () {
      var x0 = null, y0 = null;
      root.addEventListener('touchstart', function (e) { if (!S.phone || e.target.closest('.jc-layers')) { x0 = null; return; } x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; }, { passive: true });
      root.addEventListener('touchend', function (e) {
        if (x0 == null) return; var dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0; x0 = null;
        if (Math.abs(dx) > 60 && Math.abs(dy) < 45) step(dx < 0 ? 1 : -1);
      }, { passive: true });
    })();
    // the strip follows the list while it scrolls
    window.addEventListener('scroll', function () {
      if (view() !== 'list' || !$('jc-strip') || root.closest('[hidden]')) return;
      var line = (S.stripTop || 0) + $('jc-strip').offsetHeight + 24, cur = null;
      root.querySelectorAll('.jc-ag-day').forEach(function (s) { if (s.getBoundingClientRect().top <= line) cur = s.dataset.iso; });
      if (cur) root.querySelectorAll('.jc-sd').forEach(function (b) { b.classList.toggle('sel', b.dataset.pick === cur); });
    }, { passive: true });
    // keyboard: T today, D / W / M, ← →
    document.addEventListener('keydown', function (e) {
      if (root.closest('[hidden]') || S.panel || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.target.closest && e.target.closest('input, textarea, select, [contenteditable="true"], .cp-overlay.open, [role="dialog"]')) return;
      if (document.querySelector('.cp-overlay.open, .tt-ov, .mine-ov.open')) return;
      var k = (e.key || '').toLowerCase();
      if (k === 't') { S.anchor = new Date(); S.sel = todayISO(); load(); }
      else if (k === 'arrowleft') step(-1);
      else if (k === 'arrowright') step(1);
      else if (!S.phone && (k === 'd' || k === 'w' || k === 'm')) { S.view = { d: 'day', w: 'week', m: 'month' }[k]; save(); load(); }
    });
    var mqh = function () { var p = !!mq.matches; if (p !== S.phone) { S.phone = p; closePanel(true); if (S.data) load(); } };
    if (mq.addEventListener) mq.addEventListener('change', mqh); else if (mq.addListener) mq.addListener(mqh);
    window.addEventListener('resize', function () { if (view() === 'list') stickTop(); });

    /* ---------- print: the projects in view + a by-day table (never your private events) ---------- */
    function print() {
      var d = S.data; if (!d) return;
      var r = range(), ds = days(r[0], r[1]), a = ds[0], b = ds[ds.length - 1];
      var jobs = (d.jobs || []).filter(function (j) { return j.start_date <= b && (j.open_ended || j.end_date >= a); });
      $('cal-print-list').innerHTML = '<h3>' + esc(title(ds)) + ' · ' + plural(jobs.length, 'project') + '</h3><table><thead><tr><th>Project</th><th>Client</th><th>Address</th><th>Dates</th><th>PM</th><th>Crew</th></tr></thead><tbody>' +
        jobs.map(function (j) { return '<tr><td><b>' + esc(j.project_code) + '</b> ' + esc(j.name) + '</td><td>' + esc(j.client_name || '') + '</td><td>' + esc(j.address || '') + '</td><td>' + esc(shortDate(j.start_date)) + ' → ' + (j.open_ended ? 'open' : esc(shortDate(j.end_date))) + '</td><td>' + esc(j.pm || '') + '</td><td>' + j.crew_count + '</td></tr>'; }).join('') + '</tbody></table>' +
        (ds.length <= 14 ? '<h3 style="margin-top:12px">By day</h3><table><thead><tr><th>Day</th><th>Weather (NYC)</th><th>Projects</th><th>People</th><th>Tasks &amp; meetings</th></tr></thead><tbody>' + ds.map(function (iso) {
          var w = wxOf(iso), s = summary(d, iso, true);
          return '<tr><td>' + esc(shortDate(iso)) + '</td><td>' + (w && w.temp_max != null ? r0(w.temp_max) + '° / ' + r0(w.temp_min) + '° · ' + esc(w.label) : '') + '</td><td>' + jobsOn(d, iso).map(function (j) { return esc(j.name); }).join(', ') + '</td><td>' + (s.n || '') + '</td><td>' +
            on(d, iso, true).filter(function (x) { return x.k !== 'mine'; }).map(function (x) { return (isTimed(x, iso) ? t12(x.t) + ' ' : '') + esc(x.title) + (x.names.length ? ' (' + esc(x.names.join(', ')) + ')' : ''); }).join('<br>') + '</td></tr>';
        }).join('') + '</tbody></table>' : '');
      window.print();
    }

    return {
      load: load,
      setAnchor: function (iso) { S.anchor = parse(iso); S.sel = iso; },
      reset: function () { S.cache = {}; },
      refresh: function () { if (S.data) render(true); },
      openDay: function (iso, d) { openDay(iso, d, { all: true }); }
    };
  }

  window.SSC_JOBCAL = { mount: mount };
})();
