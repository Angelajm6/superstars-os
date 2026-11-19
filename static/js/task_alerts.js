/* Task pop-ups on every page, for everyone on the team (accepted prototype 2026-11-19:
 * https://claude.ai/artifact/KnHuD7Ys632eNf2MCtNpmU). The same card as the activity bell's banners.
 *
 * Loaded by company_nav.js for admin, C-suite, PM and super. Asks GET /api/team/notices every 30 s while the
 * page is visible (and the moment it comes back into view); team_items.py decides what pops:
 *   assigned   "Angela assigned you a task"  Open / Mark done. Stays until it is opened or closed, so nobody
 *              misses one; closing it keeps the task's New dot until they open the task.
 *   done       "Marco finished a task"       news: shows ~9 s
 *   moved      "Angela changed the time"     news
 *   removed    "Angela took you off a task"  news
 *   cancelled  "Angela deleted a task"       news
 * A pop-up that showed is never shown again (POST /api/team/notices/popped). Opening the task (Open, or from
 * any list) clears it everywhere. Up to 3 at a time (1 on a phone). Admins' bell banners and these share the
 * same corner, so they stack in the bell's column when it is on the page. */
(function () {
  'use strict';
  if (window.__sscTaskAlerts) return;
  window.__sscTaskAlerts = true;
  var POLL = 30000, NEWS_MS = 9000, PHONE = '(max-width: 640px)';
  var shown = {}, timer = null, busy = false, host = null;
  function svg(d) { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + '</svg>'; }
  var IC = {
    person: svg('<circle cx="12" cy="8" r="4"/><path d="M4.5 21a7.5 7.5 0 0 1 15 0"/>'),
    check: svg('<path d="M20 6 9 17l-5-5"/>'),
    cal: svg('<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>'),
    x: svg('<path d="M6 6l12 12M18 6 6 18"/>')
  };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function tasks(fn) {   // the shared View card; loaded on first use on a page that does not have it
    if (window.SSC_TASKS) { fn(window.SSC_TASKS); return; }
    var s = document.querySelector('script[data-team-tasks]');
    if (!s) { s = document.createElement('script'); s.src = '/files/static/js/team_tasks.js?v=1'; s.setAttribute('data-team-tasks', ''); document.head.appendChild(s); }
    s.addEventListener('load', function () { if (window.SSC_TASKS) fn(window.SSC_TASKS); });
  }
  function css() {
    if (document.getElementById('tt-css')) return;
    var l = document.createElement('link'); l.id = 'tt-css'; l.rel = 'stylesheet'; l.href = '/files/static/css/team_tasks.css?v=1';
    document.head.appendChild(l);
  }
  function post(path, body) {
    return fetch('/api' + path, { method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  }
  function stack() {
    var bell = document.querySelector('.ab-banners');   // an admin's bell column: share it
    if (bell) return bell;
    if (!host) { host = document.createElement('div'); host.className = 'tka-stack'; host.setAttribute('aria-live', 'polite'); document.body.appendChild(host); }
    return host;
  }
  function popped(id) { post('/team/notices/popped', { ids: [id] }).catch(function () { }); }
  function drop(el) { if (el && el.parentNode) el.parentNode.removeChild(el); }

  var WHAT = {
    assigned: function (n) { return { ico: 'blue', svg: IC.person, t: n.actor + ' assigned you ' + ({ task: 'a task', event: 'an event', meeting: 'a meeting' }[n.item.kind] || 'a task'), sticky: true }; },
    done: function (n) { return { ico: 'ok', svg: IC.check, t: n.actor + ' finished a task' }; },
    moved: function (n) { return { ico: 'warn', svg: IC.cal, t: n.actor + ' changed the time' }; },
    removed: function (n) { return { ico: 'plain', svg: IC.person, t: n.actor + ' took you off a task' }; },
    cancelled: function (n) { return { ico: 'plain', svg: IC.x, t: n.actor + ' deleted a task' }; }
  };
  function show(n) {
    if (shown[n.id] || !WHAT[n.kind]) return;
    shown[n.id] = true;
    css();
    var c = WHAT[n.kind](n), it = n.item;
    var whenTxt = window.SSC_TASKS ? window.SSC_TASKS.when(it) : (it.due_date || it.start_date || '');
    var acts = n.kind === 'cancelled' || n.kind === 'removed' ? '' :
      '<div class="tka-a"><button type="button" class="btn-primary" data-ka="open">Open</button>' +
      (n.kind === 'assigned' && it.kind === 'task' && it.can_complete && it.status === 'open' ? '<button type="button" class="btn-ghost" data-ka="done">Mark done</button>' : '') + '</div>';
    var el = document.createElement('div');
    el.className = 'tka'; el.setAttribute('role', 'status'); el.dataset.notice = n.id;
    el.innerHTML = '<span class="tka-ico ' + c.ico + '">' + c.svg + '</span><div class="tka-b"><div class="tka-t">' + esc(c.t) + '</div>' +
      '<div class="tka-s">' + esc(it.title) + '</div><div class="tka-n" data-ka-when>' + esc(whenTxt) + (it.project_code ? ' · ' + esc(it.project_code) : '') + '</div>' + acts + '</div>' +
      '<button type="button" class="tka-x" data-ka="x" aria-label="Close' + (c.sticky ? '. The task keeps its New dot until you open it.' : '') + '">' + IC.x + '</button>';
    el.addEventListener('click', function (e) {
      var b = e.target.closest('[data-ka]'); if (!b) return;
      drop(el); popped(n.id);
      if (b.dataset.ka === 'open') tasks(function (T) { T.view(it.id); });
      else if (b.dataset.ka === 'done') tasks(function (T) { T.setDone(it, true).catch(function () { }); });
    });
    var s = stack(); s.insertBefore(el, s.firstChild);
    var limit = window.matchMedia(PHONE).matches ? 1 : 3;
    while (s.querySelectorAll('.tka').length > limit) {   // anything pushed off stays as the task's New dot / in its Activity
      var all = s.querySelectorAll('.tka'); drop(all[all.length - 1]);
    }
    if (!c.sticky) {
      popped(n.id);   // news shows once
      var t = setTimeout(function () { drop(el); }, NEWS_MS);
      el.addEventListener('mouseenter', function () { clearTimeout(t); });
      el.addEventListener('mouseleave', function () { t = setTimeout(function () { drop(el); }, 3000); });
    }
    if (!window.SSC_TASKS) tasks(function (T) { var w = el.querySelector('[data-ka-when]'); if (w) w.textContent = T.when(it) + (it.project_code ? ' · ' + it.project_code : ''); });
  }
  function poll() {
    if (busy || document.hidden) return;
    busy = true;
    fetch('/api/team/notices', { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { if (r.status === 401 || r.status === 403) { stop(); throw new Error('off'); } return r.json(); })
      .then(function (j) {
        var d = (j && j.data) || {};
        (d.pop || []).slice().reverse().forEach(show);
        try { document.dispatchEvent(new CustomEvent('ssc:tasks-new', { detail: { count: d.new || 0 } })); } catch (e) { /* old browser */ }
      })
      .catch(function () { })
      .then(function () { busy = false; });
  }
  function stop() { clearInterval(timer); timer = null; }
  function start() {
    if (timer) return;
    setTimeout(poll, 1200);   // after the page has drawn
    timer = setInterval(poll, POLL);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
    document.addEventListener('ssc:items-changed', function () { setTimeout(poll, 800); });
  }
  window.SSC_TASK_ALERTS = { poll: poll };
  start();
})();
