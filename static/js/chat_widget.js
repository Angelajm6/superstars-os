/* The floating Chat window, on every office page and on a job's own page (accepted prototype 2026-11-17:
 * https://claude.ai/artifact/4qCXUYDRtc2xQ5AWLgz15i).
 *
 * Loaded by company_nav.js (every page with the sidebar) and by dashboard-static.html (a job's page). It shows only
 * when /api/chat/unread answers, so only the roles that have Chat (admin, C-suite, PM, super, estimator) ever see it;
 * a client or architect on a job page gets nothing. It also loads the chat alerts (chat_alerts.js).
 *
 * A Chat button with the unread count sits beside Ask Claude. The window is the /chat page's own chat
 * (GET /api/chat/widget = chat_fragment.html, static/css/chat.css, static/js/chat.js), one column, so the two never
 * drift. It loads on the first open. Open / closed and the open chat are kept per tab (sessionStorage), so the window
 * stays open on the same chat as you move between pages. Opening it on a job's page goes to that job's chat. It moves
 * aside when the Claude drawer is open; on a phone it is full screen. The /chat page itself has no window. */
(function () {
  'use strict';
  if (window.__sscChatW) return;
  window.__sscChatW = true;
  var V = '2';
  function loadAlerts() {
    if (document.querySelector('script[data-chat-alerts]')) return;
    var sc = document.createElement('script'); sc.src = '/files/static/js/chat_alerts.js?v=2'; sc.defer = true;
    sc.setAttribute('data-chat-alerts', ''); document.head.appendChild(sc);
  }
  if (/^\/chat\/?$/.test(location.pathname)) { loadAlerts(); return; }

  var KEY = 'ssc.chatw.';
  function ss(k, v) {
    try {
      if (v === undefined) return sessionStorage.getItem(KEY + k);
      if (v === null) sessionStorage.removeItem(KEY + k); else sessionStorage.setItem(KEY + k, String(v));
    } catch (e) { return null; }
  }
  var jm = /^\/(?:projects|portal)\/([^/?#]+)/.exec(location.pathname);
  var JOB = jm ? decodeURIComponent(jm[1]) : null;
  var S = { open: false, loading: null, fab: null, win: null, toJob: false };
  var ICON = {
    chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L3 21l1.9-6.4A8 8 0 1 1 21 12z"/></svg>',
    out: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/></svg>',
    min: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M5 12h14"/></svg>'
  };
  function get(url) {
    return fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(function (j) { return j.data; });
  }
  function setCount(n) {
    var b = S.fab && S.fab.querySelector('.chw-b'); if (!b) return;
    n = Math.max(0, n | 0); b.textContent = n > 99 ? '99+' : String(n); b.hidden = !n;
    S.fab.setAttribute('aria-label', n ? 'Chat, ' + n + ' unread' : 'Chat');
  }

  // ---- where things sit: the button beside Ask Claude; the window above them (aside of an open Claude drawer) ----
  function place() {
    var f = S.fab; if (!f) return;
    var c = document.getElementById('clx-fab'), r = c && !c.hidden && c.getClientRects().length ? c.getBoundingClientRect() : null;   // fixed: offsetParent is always null
    var vw = window.innerWidth, vh = window.innerHeight;
    f.style.left = f.style.right = '';
    if (r && r.width) {
      if (r.left + r.width / 2 > vw / 2) f.style.right = Math.round(vw - r.left + 10) + 'px';
      else f.style.left = Math.round(r.right + 10) + 'px';
      f.style.bottom = Math.round(vh - r.bottom) + 'px'; f.style.height = Math.round(r.height) + 'px';
    } else if (document.body.classList.contains('jv2') && vw <= 860) {
      f.style.left = '16px'; f.style.bottom = 'calc(78px + env(safe-area-inset-bottom, 0px))'; f.style.height = '';
    } else {
      f.style.right = 'calc(20px + env(safe-area-inset-right, 0px))'; f.style.bottom = 'calc(20px + env(safe-area-inset-bottom, 0px))'; f.style.height = '';
    }
    var w = S.win; if (!w || w.hidden) return;
    if (vw <= 640) { w.style.left = w.style.right = w.style.bottom = w.style.height = ''; return; }
    var fr = f.getBoundingClientRect(), bottom = Math.max(16, Math.round(vh - fr.top + 12));
    var cp = document.getElementById('clx-panel'), right = 20;
    if (cp && cp.classList.contains('open') && !cp.classList.contains('clx-pagemode') && vw > 900) right = cp.offsetWidth + 16;
    w.style.left = ''; w.style.right = right + 'px';
    if (right + 420 > vw - 16) { w.style.right = ''; w.style.left = '16px'; }
    w.style.bottom = bottom + 'px';
    w.style.height = Math.max(320, Math.min(660, vh - bottom - 16)) + 'px';
  }

  // ---- the window ----
  function ctl(where) {
    var d = document.createElement('div'); d.className = 'chw-ctl'; d.setAttribute('data-chw', where);
    d.innerHTML = '<button type="button" data-chw-out title="Open the full Chat page" aria-label="Open the full Chat page">' + ICON.out + '</button>' +
      '<button type="button" data-chw-min title="Minimize (Esc)" aria-label="Minimize chat">' + ICON.min + '</button>';
    return d;
  }
  function jobConv(convs) {
    var hit = JOB && (convs || []).filter(function (c) { return c.kind === 'job' && c.project_code === JOB; })[0];
    return hit ? hit.id : null;
  }
  function ensureWin() {
    if (S.loading) return S.loading;
    if (!document.querySelector('link[data-chat-css]')) {
      var l = document.createElement('link'); l.rel = 'stylesheet'; l.href = '/files/static/css/chat.css?v=' + V;
      l.setAttribute('data-chat-css', ''); document.head.appendChild(l);
    }
    var w = S.win = document.createElement('section');
    w.className = 'chw'; w.id = 'chw'; w.hidden = true; w.setAttribute('role', 'dialog'); w.setAttribute('aria-label', 'Chat');
    w.innerHTML = '<div class="chw-load">Loading chat…</div>';
    document.body.appendChild(w);
    S.loading = fetch('/api/chat/widget', { credentials: 'same-origin', headers: { Accept: 'text/html' } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (html) {
        w.innerHTML = html;
        // dialogs, the photo viewer and the toast cover the whole page, not just the window
        Array.prototype.forEach.call(w.querySelectorAll('.ch-modal-bg, .ch-light, .ch-toast'), function (el) { document.body.appendChild(el); });
        var lh = w.querySelector('.ch-lh'); if (lh) lh.appendChild(ctl('list'));
        var th = w.querySelector('#ch-th'); if (th) th.appendChild(ctl('thread'));
        window.SSC_CHAT_W = {
          job: JOB,
          isOpen: function () { return S.open; },
          onConv: function (id) { ss('conv', id); },
          start: function (convs) {
            var saved = +ss('conv') || null, ok = saved && convs.some(function (c) { return c.id === saved; });
            if (S.toJob && jobConv(convs)) { S.toJob = false; return jobConv(convs); }
            return ok ? saved : jobConv(convs);
          },
          escape: function () { if (w.contains(document.activeElement)) setOpen(false); }
        };
        return new Promise(function (res, rej) {
          var sc = document.createElement('script'); sc.src = '/files/static/js/chat.js?v=4';
          sc.onload = res; sc.onerror = rej; document.head.appendChild(sc);
        });
      })
      .catch(function () {
        w.innerHTML = '<div class="chw-load">Chat couldn’t load. Check your connection, then try again.<br><br><button type="button" class="btn-ghost" data-chw-min>Close</button></div>';
        S.loading = null;
      });
    w.addEventListener('click', function (e) {
      if (e.target.closest('[data-chw-min]')) { setOpen(false); S.fab.focus(); return; }
      if (e.target.closest('[data-chw-out]')) {
        var id = window.SSC_CHAT && window.SSC_CHAT.current();
        location.href = '/chat' + (id ? '#c' + id : '');
      }
    });
    return S.loading;
  }
  function setOpen(o, opts) {
    S.open = !!o; ss('open', o ? '1' : null);
    document.body.classList.toggle('chw-open', S.open);
    S.fab.setAttribute('aria-expanded', String(S.open));
    if (!S.open) { if (S.win) S.win.hidden = true; return Promise.resolve(); }
    var toJob = !(opts && opts.restore) && !(opts && opts.conv);
    var had = !!window.SSC_CHAT;
    S.toJob = toJob;
    return ensureWin().then(function () {
      if (!S.open || !S.win) return;
      S.win.hidden = false; place();
      var C = window.SSC_CHAT; if (!C) return;
      if (opts && opts.conv) { C.open(opts.conv); }
      else if (had && toJob) { var id = jobConv(C.convs ? C.convs() : []); if (id && id !== C.current()) C.open(id); }
      if (had) C.wake();
      C.focus();
    });
  }
  // an alert's "Open chat" (chat_alerts.js) opens it here instead of leaving the page
  window.SSC_CHAT_OPEN = function (id) { ss('conv', id); return setOpen(true, { conv: +id }); };

  function build() {
    var f = S.fab = document.createElement('button');
    f.type = 'button'; f.className = 'chw-fab'; f.id = 'chw-fab'; f.setAttribute('aria-controls', 'chw'); f.setAttribute('aria-expanded', 'false');
    f.innerHTML = ICON.chat + '<span>Chat</span><span class="chw-b" data-chat-badge hidden>0</span>';
    if (!document.querySelector('link[data-chat-css]')) {
      var l = document.createElement('link'); l.rel = 'stylesheet'; l.href = '/files/static/css/chat.css?v=' + V;
      l.setAttribute('data-chat-css', ''); document.head.appendChild(l);
    }
    document.body.appendChild(f);
    f.addEventListener('click', function () { setOpen(!S.open); });
    place();
    window.addEventListener('resize', place);
    setInterval(function () { if (!document.hidden) place(); }, 1000);   // follows Ask Claude and its drawer
    document.addEventListener('ssc:chat-unread', function (e) { setCount(e.detail && e.detail.unread); });
    if (!window.SSC_NAV) {   // a job's page has no sidebar polling the count: do it here
      var poll = function () { if (!document.hidden && !window.SSC_CHAT) get('/api/chat/unread').then(function (d) { setCount(d.unread); }).catch(function () {}); };
      setInterval(poll, 45000);
      document.addEventListener('ssc:chat-refresh', poll);
      document.addEventListener('visibilitychange', poll);
    }
  }

  function start() {
    get('/api/chat/unread').then(function (d) {   // 401 / 403 for anyone without Chat: nothing shows
      build(); setCount(d.unread); loadAlerts();
      if (ss('open') === '1') setOpen(true, { restore: true });
    }).catch(function () {});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
