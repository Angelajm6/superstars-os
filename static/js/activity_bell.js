/* The activity bell + top-right banners (activity_feed.py; accepted prototype, phase 2).
 *
 * auth_menu.js loads this for admins only, next to #user-menu on every staff page. The server checks the role again on
 * every call; this is display. Polls once a minute while the tab is visible, so a hidden tab never pops banners.
 *
 * Two tabs (accepted prototype 2026-11-11, https://claude.ai/artifact/E3AZ6ytnir14NxaBCnQgK6): For you = this person's
 * day (the dashboard's Needs you cards, plus decisions waiting), What happened = the team's news. The badge counts For
 * you; unread news shows as a dot on its tab. Once a day the first page pops the morning banner.
 */
(function () {
  'use strict';
  if (window.__SSC_BELL) return;
  window.__SSC_BELL = true;

  var POLL_MS = 60000, BANNER_MS = 9000, BANNER_LINGER_MS = 3000, PHONE = '(max-width: 640px)';
  var svg = function (d) {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + '</svg>';
  };
  var ICON = {
    bell: svg('<path d="M6 9a6 6 0 1 1 12 0c0 6 2.5 7.5 2.5 7.5h-17S6 15 6 9"/><path d="M10 20a2.2 2.2 0 0 0 4 0"/>'),
    send: svg('<path d="M21 3 10 14M21 3l-7 18-4-7-7-4z"/>'),
    doc: svg('<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>'),
    person: svg('<circle cx="12" cy="8" r="4"/><path d="M4.5 21a7.5 7.5 0 0 1 15 0"/>'),
    build: svg('<path d="M4 21V6l8-3 8 3v15"/><path d="M9 21v-5h6v5M9 9h.01M15 9h.01M9 12.5h.01M15 12.5h.01"/>'),
    check: svg('<path d="M20 6 9 17l-5-5"/>'),
    mail: svg('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>'),
    spark: svg('<path d="M12 3l1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.4z"/>'),
    sun: svg('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
    money: svg('<path d="M12 3v18M16.5 7.5C16 6 14.3 5 12 5 9.5 5 7.5 6.3 7.5 8.3c0 4.5 9 2.7 9 7.4 0 2-2 3.3-4.5 3.3-2.5 0-4.3-1-5-2.7"/>'),
    x: svg('<path d="M6 6l12 12M18 6 6 18"/>')
  };
  var TINT = { sent: 'ok', signed: 'ok', paid: 'ok', declined: 'warn', docs: 'warn', started: 'plain', project: 'plain' };
  var CLOSED_KEY = 'ssc.bell.closed';

  var S = { data: null, open: false, failed: false, shown: {}, lastLoad: 0, stopped: false, tab: 'you', sawNews: false };
  var bell, panel, scrim, banners, sheetWrap, toastEl;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function post(path, body) {
    return fetch(path, {
      method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
  }
  function icon(it) {
    return '<span class="ab-ico ab-' + (it.tint || TINT[it.kind] || 'blue') + '">' + (ICON[it.icon] || ICON.doc) + '</span>';
  }
  function anchorTop() {   // right under the header, or the top of the screen once the header scrolled away
    return Math.max(12, Math.round(bell.getBoundingClientRect().bottom + 8));
  }

  /* ------------------------------ bell + panel ------------------------------ */
  function badgeCount() {
    if (!S.data) return 0;
    return S.data.badge != null ? S.data.badge : S.data.unread + S.data.waiting.length;
  }
  function renderBell() {
    var n = badgeCount(), news = S.data ? S.data.unread : 0;
    bell.innerHTML = ICON.bell + (n ? '<span class="ab-badge">' + (n > 99 ? '99+' : n) + '</span>' : (news ? '<span class="ab-badge ab-badge-dot" aria-hidden="true"></span>' : ''));
    bell.setAttribute('aria-label', n ? 'Notifications, ' + n + ' for you' : (news ? 'Notifications, ' + news + ' new' : 'Notifications'));
    bell.setAttribute('aria-expanded', S.open ? 'true' : 'false');
  }
  function youRow(it) {
    return '<a class="ab-item ab-you" href="' + esc(it.href) + '">' + icon(it) +
      '<span class="ab-body"><span class="ab-title">' + esc(it.title) + '</span>' +
      (it.sub ? '<span class="ab-sub">' + esc(it.sub) + '</span>' : '') +
      (it.pill ? '<span class="ab-pill ab-pill-' + esc(it.tint || 'plain') + '">' + esc(it.pill) + '</span>' : '') + '</span></a>';
  }
  function row(it) {
    return '<a class="ab-item' + (it.unread ? ' ab-unread' : '') + '" href="' + esc(it.href) + '">' + icon(it) +
      '<span class="ab-body"><span class="ab-title">' + esc(it.title) + '</span>' +
      '<span class="ab-sub">' + esc([it.sub, it.when].filter(Boolean).join(' · ')) + '</span></span>' +
      (it.unread ? '<span class="ab-dot" aria-label="New"></span>' : '') + '</a>';
  }
  function actionButtons(w) {
    return w.actions.map(function (a) {
      return '<button type="button" class="' + (a.primary ? 'btn-primary' : 'btn-ghost') + '" data-ab="decide" data-id="' + esc(w.id) + '" data-act="' + esc(a.act) + '">' + esc(a.label) + '</button>';
    }).join('');
  }
  function waitRow(w) {
    return '<div class="ab-item ab-unread ab-wait">' + icon(w) + '<span class="ab-body"><a class="ab-title" href="' + esc(w.href) + '">' + esc(w.title) + '</a>' +
      '<span class="ab-sub">' + esc(w.sub) + '</span>' + (w.note ? '<span class="ab-note">' + esc(w.note) + '</span>' : '') +
      '<span class="ab-acts">' + actionButtons(w) + '</span></span></div>';
  }
  function section(items, group, label) {
    var list = items.filter(function (i) { return i.group === group; });
    return list.length ? '<div class="ab-group">' + label + '</div>' + list.map(row).join('') : '';
  }
  var YOU_GROUPS = [['today', 'Today'], ['claude', 'From Claude'], ['soon', 'Coming up']];
  function youBody() {
    var you = S.data.for_you || [], asks = S.data.waiting || [];
    if (!you.length && !asks.length) return '<div class="ab-empty">Nothing needs you right now. What’s due, expiring or drafted by Claude shows up here.</div>';
    return (asks.length ? '<div class="ab-group">Waiting on you</div>' + asks.map(waitRow).join('') : '') +
      YOU_GROUPS.map(function (g) {
        var list = you.filter(function (i) { return i.group === g[0]; });
        return list.length ? '<div class="ab-group">' + g[1] + '</div>' + list.map(youRow).join('') : '';
      }).join('');
  }
  function newsBody() {
    var items = S.data.items;
    if (!items.length) return '<div class="ab-empty">Nothing yet. When the team sends a proposal, adds a client or uploads documents, it shows up here.</div>';
    return section(items, 'today', 'Today') + section(items, 'earlier', 'Earlier');
  }
  function renderPanel() {
    scrim.hidden = !S.open;
    panel.hidden = !S.open;
    if (!S.open) return;
    panel.style.top = window.matchMedia(PHONE).matches ? '' : anchorTop() + 'px';
    if (S.tab === 'news') S.sawNews = true;
    var body;
    if (S.failed && !S.data) {
      body = '<div class="ab-empty">Notifications didn’t load. <button type="button" class="ab-link" data-ab="retry">Try again</button></div>';
    } else if (!S.data) {
      body = '<div class="ab-empty">Loading…</div>';
    } else {
      body = S.tab === 'news' ? newsBody() : youBody();
    }
    var unread = S.data && S.data.unread, n = badgeCount();
    function tab(key, label, extra) {
      return '<button type="button" role="tab" class="ab-tab' + (S.tab === key ? ' ab-on' : '') + '" aria-selected="' + (S.tab === key) + '" data-ab="tab" data-tab="' + key + '">' + label + extra + '</button>';
    }
    panel.innerHTML = '<div class="ab-head"><div class="ab-tabs" role="tablist" aria-label="Notifications">' +
      tab('you', 'For you', n ? '<span class="ab-count">' + n + '</span>' : '') +
      tab('news', 'What happened', unread ? '<span class="ab-tabdot" aria-label="' + unread + ' new"></span>' : '') +
      '</div><span class="ab-head-acts">' +
      (S.tab === 'news' && unread ? '<button type="button" class="ab-link" data-ab="read-all">Mark all read</button>' : '') +
      '<button type="button" class="ab-close" data-ab="close" aria-label="Close">' + ICON.x + '</button></span></div>' +
      '<div class="ab-list" role="tabpanel">' + body + '</div>' +
      '<div class="ab-foot">' + (S.tab === 'news' ? 'Your own actions don’t appear here, unless they’re waiting for a decision. Admins only for now.'
        : 'The same list as Needs you on the dashboard. Tap one to open it.') + '</div>';
  }
  function markRead() {
    if (!S.data || !S.data.unread || !S.data.latest) return;
    post('/api/activity/seen', { what: 'read', through: S.data.latest }).catch(function () { });
    S.data.unread = 0;
    S.data.items.forEach(function (i) { i.unread = false; });
  }
  function toggle(open, tab) {
    if (tab) S.tab = tab;
    if (S.open === open) { if (open) renderPanel(); return; }
    S.open = open;
    if (open) S.sawNews = false;
    if (!open && S.sawNews) markRead();   // the dots stay while the list is open, so you can see what was new
    renderBell();
    renderPanel();
    if (open) { var c = panel.querySelector('.ab-close'); if (c) c.focus(); } else bell.focus({ preventScroll: true });
  }

  /* ------------------------------ banners ------------------------------ */
  function dropBanner(el) { if (el && el.parentNode) el.parentNode.removeChild(el); }
  function autoClose(el, ms) {
    var t = setTimeout(function () { dropBanner(el); }, ms);
    function pause() { clearTimeout(t); }
    function resume() { clearTimeout(t); t = setTimeout(function () { dropBanner(el); }, BANNER_LINGER_MS); }
    el.addEventListener('mouseenter', pause);
    el.addEventListener('mouseleave', resume);
    el.addEventListener('focusin', pause);
    el.addEventListener('focusout', resume);
  }
  function showBanner(it) {
    if (S.shown[it.id]) return;
    S.shown[it.id] = true;
    var el = document.createElement('div');
    el.className = 'ab-banner';
    el.dataset.id = it.id;
    el.innerHTML = icon(it) + '<div class="ab-body"><div class="ab-title">' + esc(it.title) + '</div>' +
      (it.sub ? '<div class="ab-sub">' + esc(it.sub) + '</div>' : '') +
      (it.note ? '<div class="ab-note">' + esc(it.note) + '</div>' : '') +
      '<div class="ab-acts">' + (it.actions ? actionButtons(it) : '<a class="btn-primary" href="' + esc(it.href) + '">' + esc(it.open_label || 'Open') + '</a>' +
      (it.undo_action_id ? '<button type="button" class="btn-ghost" data-ab="undo" data-action="' + esc(it.undo_action_id) + '">Undo</button>' : '')) +
      '</div></div><button type="button" class="ab-close" data-ab="banner-x" aria-label="Close. It stays in the bell.">' + ICON.x + '</button>';
    if (it.actions) el.dataset.wait = '1';
    banners.prepend(el);
    var limit = window.matchMedia(PHONE).matches ? 1 : 3;   // anything pushed off stays in the bell; news goes before a decision
    while (banners.children.length > limit) {
      var news = banners.querySelectorAll('.ab-banner:not([data-wait])');
      dropBanner(news.length ? news[news.length - 1] : banners.lastElementChild);
    }
    if (!it.actions) autoClose(el, BANNER_MS);   // a decision stays until someone answers it or closes it
  }
  function showMorning(m) {
    if (!m || S.shown[m.id]) return;
    S.shown[m.id] = true;
    var el = document.createElement('div');
    el.className = 'ab-banner ab-morning';
    el.dataset.id = m.id;
    el.dataset.wait = '1';   // stays until closed or opened, like a decision
    el.innerHTML = '<span class="ab-ico ab-day">' + ICON.sun + '</span><div class="ab-body"><div class="ab-title">' + esc(m.title) + '</div>' +
      '<div class="ab-day-lines">' + m.lines.map(function (l) { return '<div><b>' + esc(l.n) + '</b>' + esc(l.label) + '</div>'; }).join('') + '</div>' +
      '<div class="ab-acts"><button type="button" class="btn-primary" data-ab="see-today">See today</button></div></div>' +
      '<button type="button" class="ab-close" data-ab="banner-x" aria-label="Close. It stays in the bell.">' + ICON.x + '</button>';
    banners.prepend(el);
    var limit = window.matchMedia(PHONE).matches ? 1 : 3;
    while (banners.children.length > limit) {
      var news = banners.querySelectorAll('.ab-banner:not([data-wait])');
      dropBanner(news.length ? news[news.length - 1] : banners.lastElementChild);
    }
    post('/api/activity/seen', { what: 'morning', through: m.day }).catch(function () { });
  }
  function closedIds() {
    try { return JSON.parse(localStorage.getItem(CLOSED_KEY) || '[]'); } catch (e) { return []; }
  }
  function rememberClosed(id) {
    try { localStorage.setItem(CLOSED_KEY, JSON.stringify(closedIds().concat([id]).slice(-100))); } catch (e) { /* it pops once more; no harm */ }
  }
  function syncWaitingBanners(d) {
    var open = {};
    d.waiting.forEach(function (w) { open[w.id] = true; });
    Array.prototype.forEach.call(banners.querySelectorAll('.ab-banner[data-wait]'), function (el) {   // someone else answered it
      if (!open[el.dataset.id]) dropBanner(el);
    });
    var closed = closedIds();
    d.waiting.filter(function (w) { return w.pop && closed.indexOf(w.id) < 0; }).reverse().forEach(function (w) {
      if (S.shown[w.id]) return;
      showBanner(w);
    });
  }

  /* ------------------------------ decisions ------------------------------ */
  function findWaiting(id) {
    return ((S.data && S.data.waiting) || []).filter(function (w) { return w.id === id; })[0];
  }
  function toast(msg, opts) {
    opts = opts || {};
    clearTimeout(S.toastTimer);
    toastEl.innerHTML = '<span>' + esc(msg) + '</span>' +
      (opts.href ? '<a href="' + esc(opts.href) + '">' + esc(opts.openLabel || 'Open') + '</a>' : '') +
      (opts.undoId ? '<button type="button" data-ab="decide" data-id="' + esc(opts.undoId) + '" data-act="undo">Undo</button>' : '');
    toastEl.hidden = false;
    S.toastTimer = setTimeout(function () { toastEl.hidden = true; }, opts.undoId ? 7000 : 4000);
  }
  function answered(id) {
    dropBanner(banners.querySelector('.ab-banner[data-id="' + id + '"]'));
    delete S.shown[id];
    closeSheet();
    load();
    document.dispatchEvent(new CustomEvent('ssc:claude-changed'));   // the page behind shows the change now
    document.dispatchEvent(new CustomEvent('ssc:activity-changed'));
  }
  function send(id, action, extra) {
    return post('/api/activity/decide', Object.assign({ id: id, action: action }, extra || {})).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (r.status === 409) { answered(id); toast(j.error || 'Someone already took care of this.'); return null; }
        if (!r.ok) throw new Error(j.error || 'That didn’t go through. Try again.');
        answered(id);
        toast(j.data.message, { undoId: j.data.undo ? id : null, href: j.data.href, openLabel: j.data.open_label });
        return j.data;
      });
    });
  }
  function decide(btn) {
    var id = btn.dataset.id, act = btn.dataset.act;
    if (act === 'merge') return mergeSheet(id);
    if (act === 'edit') return editSheet(id);
    if (act === 'create') return projectSheet(id);
    btn.disabled = true;
    send(id, act).catch(function (e) { btn.disabled = false; toast(e.message); });
  }

  /* ------------------------------ sheets ------------------------------ */
  function openSheet(title, html, onSubmit) {
    sheetWrap.innerHTML = '<form class="ab-sheet" role="dialog" aria-modal="true" aria-label="' + esc(title) + '" novalidate><h3>' + esc(title) + '</h3>' + html +
      '<div class="ab-err" role="alert" hidden></div><div class="ab-sheet-foot"><button type="button" class="btn-ghost" data-ab="sheet-close">Cancel</button>' +
      '<button type="submit" class="btn-primary">' + esc(onSubmit.label) + '</button></div></form>';
    sheetWrap.hidden = false;
    clearTimeout(S.toastTimer);
    toastEl.hidden = true;   // never over the form
    var form = sheetWrap.querySelector('form');
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var go = form.querySelector('[type=submit]'), err = form.querySelector('.ab-err');
      err.hidden = true;
      go.disabled = true;
      Promise.resolve().then(function () { return onSubmit.run(form); }).catch(function (x) {
        err.textContent = x.message;
        err.hidden = false;
        go.disabled = false;
      });
    });
    var first = form.querySelector('input:not([type=radio]),select');
    (first || form).focus && (first || form).focus();
    return form;
  }
  function closeSheet() {
    if (sheetWrap) { sheetWrap.hidden = true; sheetWrap.innerHTML = ''; }
  }
  function getJSON(path) {
    return fetch(path, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } }).then(function (r) {
      if (!r.ok) throw new Error('That didn’t load. Try again.');
      return r.json();
    }).then(function (j) { return j.data; });
  }
  function matches(id) { return getJSON('/api/activity/client-matches/' + encodeURIComponent(id.split(':')[1])); }
  function mergeSheet(id) {
    var w = findWaiting(id);
    if (!w) return;
    matches(id).then(function (d) {
      function option(c, on) {
        return '<label class="ab-opt"><input type="radio" name="into" value="' + esc(c.id) + '"' + (on ? ' checked' : '') + '><span><strong>' + esc(c.name) + '</strong>' +
          '<small>' + c.projects + (c.projects === 1 ? ' project' : ' projects') + ' · ' + c.proposals + (c.proposals === 1 ? ' proposal' : ' proposals') + '</small></span></label>';
      }
      function list(q) {
        var rows = q ? d.clients.filter(function (c) { return String(c.name).toLowerCase().indexOf(q) >= 0; }) : d.clients;
        return rows.slice(0, 6).map(function (c, i) { return option(c, !q && i === 0 && c.score >= 0.8); }).join('') || '<p class="ab-sheet-p">No client by that name.</p>';
      }
      var form = openSheet('Merge ' + w.name,
        '<p class="ab-sheet-p">Everything on ' + esc(w.name) + ' moves to the client you pick, and this name goes away. Anything already sent keeps the name it went out with. This can’t be undone.</p>' +
        '<label class="ab-field">Find a client<input type="text" data-ab-find autocomplete="off"></label><div data-ab-list>' + list('') + '</div>',
        { label: 'Merge', run: function (f) {
          var pick = f.querySelector('input[name=into]:checked');
          if (!pick) throw new Error('Pick the client to merge into.');
          return send(id, 'merge', { into_id: +pick.value });
        } });
      form.querySelector('[data-ab-find]').addEventListener('input', function (e) {
        form.querySelector('[data-ab-list]').innerHTML = list(e.target.value.trim().toLowerCase());
      });
    }).catch(function (e) { toast(e.message); });
  }
  function editSheet(id) {
    var w = findWaiting(id);
    if (!w) return;
    matches(id).then(function (d) {
      openSheet('Edit new client',
        '<p class="ab-sheet-p">Fix anything that came through wrong. Its proposals follow the change.</p>' +
        '<label class="ab-field">Client name<input type="text" name="name" value="' + esc(w.name) + '" maxlength="200"></label>' +
        '<label class="ab-field">Kind<select name="kind"><option value="">Choose one</option>' +
        d.kinds.map(function (k) { return '<option value="' + esc(k.code) + '">' + esc(k.label) + '</option>'; }).join('') + '</select></label>',
        { label: 'Save', run: function (f) { return send(id, 'edit', { name: f.elements.name.value, client_kind: f.elements.kind.value }); } });
    }).catch(function (e) { toast(e.message); });
  }
  function projectSheet(id) {
    var w = findWaiting(id);
    if (!w) return;
    var p = w.project;
    getJSON('/api/admin/projects/onboard-meta').then(function (m) {
      function opts(list, cur) {
        return list.map(function (o) { return '<option value="' + esc(o.code) + '"' + (o.code === cur ? ' selected' : '') + '>' + esc(o.label) + '</option>'; }).join('');
      }
      var form = openSheet('Create project',
        '<p class="ab-sheet-p">Filled in from the signed proposal. Change anything before you create it. Crew, dates and billing are set on the project’s page.</p>' +
        '<label class="ab-field">Project name<input type="text" name="name" value="' + esc(p.name) + '" maxlength="200"></label>' +
        '<label class="ab-field">Address<input type="text" name="address" value="' + esc(p.address) + '"></label>' +
        '<div class="ab-field">Client<div class="ab-static">' + esc(p.client) + '</div></div>' +
        '<label class="ab-field">Job type<select name="est_type">' + opts(m.types, p.est_type) + '</select></label>' +
        '<label class="ab-field" data-ab-other hidden>Describe the job type<input type="text" name="other" maxlength="80"></label>' +
        '<label class="ab-field" data-ab-rope hidden>Rope access work<select name="rope"><option value="inspection">Inspection</option><option value="repair">Repair work</option></select></label>' +
        '<label class="ab-field">Borough or region<select name="borough">' + opts(m.regions, p.borough) + '</select></label>',
        { label: 'Create project', run: function (f) {
          return send(id, 'create', { name: f.elements.name.value, address: f.elements.address.value, est_type: f.elements.est_type.value,
            borough: f.elements.borough.value,
            job_type_other: f.elements.est_type.value === 'OTH' ? f.elements.other.value : '',
            rope_access_scope: f.elements.est_type.value === 'IRA' ? f.elements.rope.value : '' });
        } });
      function other() {
        form.querySelector('[data-ab-other]').hidden = form.elements.est_type.value !== 'OTH';
        form.querySelector('[data-ab-rope]').hidden = form.elements.est_type.value !== 'IRA';
      }
      form.elements.est_type.addEventListener('change', other);
      other();
    }).catch(function (e) { toast(e.message); });
  }
  function undo(btn) {
    var el = btn.closest('.ab-banner');
    btn.disabled = true;
    post('/api/company/portal/today/handled', { action_id: +btn.dataset.action, answer: 'reopen' }).then(function (r) {
      if (!r.ok) throw new Error('refused');
      el.querySelector('.ab-body').innerHTML = '<div class="ab-title">The card is back in Needs you.</div><div class="ab-sub">The proposal stays sent.</div>';
      document.dispatchEvent(new CustomEvent('ssc:activity-changed'));
      autoClose(el, BANNER_LINGER_MS);
    }).catch(function () {
      btn.disabled = false;
      var note = el.querySelector('.ab-note');
      if (note) note.textContent = 'That didn’t go through. Use Reopen under Handled by your team.';
    });
  }

  /* ------------------------------ data ------------------------------ */
  function load() {
    if (S.stopped) return Promise.resolve();
    S.lastLoad = Date.now();
    return fetch('/api/activity', { credentials: 'same-origin', headers: { 'Accept': 'application/json' } }).then(function (r) {
      if (r.status === 401 || r.status === 403) { S.stopped = true; throw new Error('signed out'); }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (j) {
      var d = j.data, before = S.data ? S.data.unread : null, beforeBadge = S.data ? badgeCount() : null;
      if (S.open && S.data) {   // what you are looking at keeps its dots until you close the list
        var was = {};
        S.data.items.forEach(function (i) { if (i.unread) was[i.id] = true; });
        d.items.forEach(function (i) { if (was[i.id]) i.unread = true; });
      }
      S.data = d;
      S.failed = false;
      renderBell();
      if (S.open) renderPanel();
      if ((before !== null && d.unread > before) || (beforeBadge !== null && badgeCount() > beforeBadge)) {
        bell.classList.remove('ab-ring');
        void bell.offsetWidth;
        bell.classList.add('ab-ring');
      }
      if (!document.hidden) syncWaitingBanners(d);
      if (!document.hidden && d.morning && !S.open) showMorning(d.morning);
      if (d.banners.length && !document.hidden) {
        // newest ends up on top; one you can act on (Undo) goes last, so a phone's single banner is that one
        d.banners.slice().reverse().sort(function (a, b) { return (a.undo_action_id ? 1 : 0) - (b.undo_action_id ? 1 : 0); }).forEach(showBanner);
        post('/api/activity/seen', { what: 'banners', through: d.banners[0].at }).catch(function () { });
        document.dispatchEvent(new CustomEvent('ssc:activity-changed'));   // Needs you may have just changed: show it now
      }
    }).catch(function () {
      S.failed = true;
      if (S.open) renderPanel();
    });
  }

  /* ------------------------------ mount ------------------------------ */
  function onClick(e) {
    var b = e.target.closest && e.target.closest('[data-ab]');
    if (b) {
      var what = b.dataset.ab;
      if (what === 'close') toggle(false);
      else if (what === 'retry') load();
      else if (what === 'read-all') { markRead(); renderBell(); renderPanel(); }
      else if (what === 'tab') { S.tab = b.dataset.tab; renderPanel(); var t = panel.querySelector('.ab-tab.ab-on'); if (t) t.focus(); }
      else if (what === 'see-today') { dropBanner(b.closest('.ab-banner')); toggle(true, 'you'); }
      else if (what === 'banner-x') {
        var bn = b.closest('.ab-banner');
        if (bn.dataset.wait) rememberClosed(bn.dataset.id);   // it stays in the bell and doesn't pop up again
        dropBanner(bn);
      }
      else if (what === 'decide') decide(b);
      else if (what === 'sheet-close') closeSheet();
      else if (what === 'undo') undo(b);
      return;
    }
    var link = e.target.closest && e.target.closest('a.ab-item, .ab-wait a, .ab-banner a');
    if (link && !link.closest('.ab-banner')) {
      if (S.sawNews) markRead();
      S.open = false;
      renderBell();
      renderPanel();
    } else if (link) {
      dropBanner(link.closest('.ab-banner'));
    }
  }
  function mount() {
    var host = document.getElementById('user-menu');
    if (!host || document.getElementById('ab-bell')) return;
    bell = document.createElement('button');
    bell.type = 'button';
    bell.id = 'ab-bell';
    bell.className = 'ab-bell';
    bell.setAttribute('aria-haspopup', 'true');
    bell.addEventListener('click', function () { toggle(!S.open, S.open ? null : 'you'); });
    bell.addEventListener('animationend', function () { bell.classList.remove('ab-ring'); });
    host.parentNode.insertBefore(bell, host);

    scrim = document.createElement('button');
    scrim.type = 'button';
    scrim.className = 'ab-scrim';
    scrim.hidden = true;
    scrim.setAttribute('aria-label', 'Close notifications');
    scrim.addEventListener('click', function () { toggle(false); });

    panel = document.createElement('div');
    panel.className = 'ab-panel';
    panel.hidden = true;
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', 'Notifications');

    banners = document.createElement('div');
    banners.className = 'ab-banners';
    banners.setAttribute('role', 'status');
    banners.setAttribute('aria-live', 'polite');

    sheetWrap = document.createElement('div');
    sheetWrap.className = 'ab-sheet-wrap';
    sheetWrap.hidden = true;
    sheetWrap.addEventListener('mousedown', function (e) { if (e.target === sheetWrap) closeSheet(); });

    toastEl = document.createElement('div');
    toastEl.className = 'ab-toast';
    toastEl.hidden = true;
    toastEl.setAttribute('role', 'status');

    [scrim, panel, banners, sheetWrap, toastEl].forEach(function (el) { document.body.appendChild(el); });
    [panel, banners, sheetWrap, toastEl].forEach(function (el) { el.addEventListener('click', onClick); });
    document.addEventListener('click', function (e) {
      if (!e.target.isConnected) return;   // a tab or button inside the panel re-drew it: that click was inside
      if (S.open && !panel.contains(e.target) && !bell.contains(e.target) && !sheetWrap.contains(e.target) && !toastEl.contains(e.target)) toggle(false);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      if (!sheetWrap.hidden) closeSheet();
      else if (S.open) toggle(false);
    });

    function placeBanners() { banners.style.top = anchorTop() + 'px'; }
    window.addEventListener('scroll', placeBanners, { passive: true, capture: true });
    window.addEventListener('resize', placeBanners);
    placeBanners();

    renderBell();
    load();
    setInterval(function () { if (!document.hidden) load(); }, POLL_MS);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden && Date.now() - S.lastLoad > 15000) load();
    });
  }
  mount();
})();
