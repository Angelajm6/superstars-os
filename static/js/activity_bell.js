/* The activity bell + top-right banners (activity_feed.py; accepted prototype, phase 2).
 *
 * auth_menu.js loads this for admins only, next to #user-menu on every staff page. The server checks the role again on
 * every call; this is display. Polls once a minute while the tab is visible, so a hidden tab never pops banners.
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
    money: svg('<path d="M12 3v18M16.5 7.5C16 6 14.3 5 12 5 9.5 5 7.5 6.3 7.5 8.3c0 4.5 9 2.7 9 7.4 0 2-2 3.3-4.5 3.3-2.5 0-4.3-1-5-2.7"/>'),
    x: svg('<path d="M6 6l12 12M18 6 6 18"/>')
  };
  var TINT = { sent: 'ok', signed: 'ok', paid: 'ok', declined: 'warn', docs: 'warn', started: 'plain', project: 'plain' };

  var S = { data: null, open: false, failed: false, shown: {}, lastLoad: 0, stopped: false };
  var bell, panel, scrim, banners;

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
    return '<span class="ab-ico ab-' + (TINT[it.kind] || 'blue') + '">' + (ICON[it.icon] || ICON.doc) + '</span>';
  }
  function anchorTop() {   // right under the header, or the top of the screen once the header scrolled away
    return Math.max(12, Math.round(bell.getBoundingClientRect().bottom + 8));
  }

  /* ------------------------------ bell + panel ------------------------------ */
  function renderBell() {
    var n = S.data ? S.data.unread : 0;
    bell.innerHTML = ICON.bell + (n ? '<span class="ab-badge">' + (n > 99 ? '99+' : n) + '</span>' : '');
    bell.setAttribute('aria-label', n ? 'Notifications, ' + n + ' new' : 'Notifications');
    bell.setAttribute('aria-expanded', S.open ? 'true' : 'false');
  }
  function row(it) {
    return '<a class="ab-item' + (it.unread ? ' ab-unread' : '') + '" href="' + esc(it.href) + '">' + icon(it) +
      '<span class="ab-body"><span class="ab-title">' + esc(it.title) + '</span>' +
      '<span class="ab-sub">' + esc([it.sub, it.when].filter(Boolean).join(' · ')) + '</span></span>' +
      (it.unread ? '<span class="ab-dot" aria-label="New"></span>' : '') + '</a>';
  }
  function section(items, group, label) {
    var list = items.filter(function (i) { return i.group === group; });
    return list.length ? '<div class="ab-group">' + label + '</div>' + list.map(row).join('') : '';
  }
  function renderPanel() {
    scrim.hidden = !S.open;
    panel.hidden = !S.open;
    if (!S.open) return;
    panel.style.top = window.matchMedia(PHONE).matches ? '' : anchorTop() + 'px';
    var items = S.data ? S.data.items : [];
    var body;
    if (S.failed && !S.data) {
      body = '<div class="ab-empty">Notifications didn’t load. <button type="button" class="ab-link" data-ab="retry">Try again</button></div>';
    } else if (!S.data) {
      body = '<div class="ab-empty">Loading…</div>';
    } else if (!items.length) {
      body = '<div class="ab-empty">Nothing yet. When the team sends a proposal, adds a client or uploads documents, it shows up here.</div>';
    } else {
      body = section(items, 'today', 'Today') + section(items, 'earlier', 'Earlier');
    }
    var unread = S.data && S.data.unread;
    panel.innerHTML = '<div class="ab-head"><h3>Notifications</h3><span class="ab-head-acts">' +
      (unread ? '<button type="button" class="ab-link" data-ab="read-all">Mark all read</button>' : '') +
      '<button type="button" class="ab-close" data-ab="close" aria-label="Close">' + ICON.x + '</button></span></div>' +
      '<div class="ab-list">' + body + '</div>' +
      '<div class="ab-foot">Your own actions don’t appear here. Admins only for now.</div>';
  }
  function markRead() {
    if (!S.data || !S.data.unread || !S.data.latest) return;
    post('/api/activity/seen', { what: 'read', through: S.data.latest }).catch(function () { });
    S.data.unread = 0;
    S.data.items.forEach(function (i) { i.unread = false; });
  }
  function toggle(open) {
    if (S.open === open) return;
    S.open = open;
    if (!open) markRead();   // the dots stay while the list is open, so you can see what was new
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
      '<div class="ab-acts"><a class="btn-primary" href="' + esc(it.href) + '">' + esc(it.open_label || 'Open') + '</a>' +
      (it.undo_action_id ? '<button type="button" class="btn-ghost" data-ab="undo" data-action="' + esc(it.undo_action_id) + '">Undo</button>' : '') +
      '</div></div><button type="button" class="ab-close" data-ab="banner-x" aria-label="Close. It stays in the bell.">' + ICON.x + '</button>';
    banners.prepend(el);
    var limit = window.matchMedia(PHONE).matches ? 1 : 3;   // anything pushed off stays in the bell
    Array.prototype.slice.call(banners.children, limit).forEach(dropBanner);
    autoClose(el, BANNER_MS);
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
      var d = j.data, before = S.data ? S.data.unread : null;
      if (S.open && S.data) {   // what you are looking at keeps its dots until you close the list
        var was = {};
        S.data.items.forEach(function (i) { if (i.unread) was[i.id] = true; });
        d.items.forEach(function (i) { if (was[i.id]) i.unread = true; });
      }
      S.data = d;
      S.failed = false;
      renderBell();
      if (S.open) renderPanel();
      if (before !== null && d.unread > before) {
        bell.classList.remove('ab-ring');
        void bell.offsetWidth;
        bell.classList.add('ab-ring');
      }
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
      else if (what === 'banner-x') dropBanner(b.closest('.ab-banner'));
      else if (what === 'undo') undo(b);
      return;
    }
    var link = e.target.closest && e.target.closest('.ab-item, .ab-banner a');
    if (link && link.classList.contains('ab-item')) {
      markRead();
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
    bell.addEventListener('click', function () { toggle(!S.open); });
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

    [scrim, panel, banners].forEach(function (el) { document.body.appendChild(el); });
    [panel, banners].forEach(function (el) { el.addEventListener('click', onClick); });
    document.addEventListener('click', function (e) {
      if (S.open && !panel.contains(e.target) && !bell.contains(e.target)) toggle(false);
    });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && S.open) toggle(false); });

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
