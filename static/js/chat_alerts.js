/* Chat alerts on every page (accepted prototype 2026-11-16: https://claude.ai/artifact/VJ7fRgnsXV3FxTmTL8Ez5g).
 * Loaded by company_nav.js for the roles that have Chat. Watches /api/chat/poll (one tiny query) every 6 s while
 * the page is visible, 20 s while it is in a background tab; on a change asks /api/chat/alerts what should pop up
 * (chat.py decides: private / group messages, @mentions, chats set to "All messages"; never muted chats).
 * Visible tab: a card bottom-right (top on a phone) with Open chat and Reply. Background tab: a computer
 * notification, once the person turned them on. The chat you are reading never alerts. */
(function () {
  'use strict';
  if (window.__sscChatAlerts) return;
  window.__sscChatAlerts = true;
  var KEY = 'ssc.chat.alert.last', ASKED = 'ssc.chat.osn.asked', MAX = 3;
  var state = { rev: -1, last: null, busy: false, timer: null };
  var COLORS = ['#4364dc', '#0f8b8d', '#7a5cc7', '#3c7f5a', '#c0632f', '#4a6fa5', '#9b4f8a', '#5b6b2e'];

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function color(k) { var h = 0; for (var i = 0; i < k.length; i++) h = (h * 31 + k.charCodeAt(i)) | 0; return COLORS[Math.abs(h) % COLORS.length]; }
  function ini(n) { var p = String(n || '?').trim().split(/\s+/); return ((p[0] || '?')[0] + (p.length > 1 ? p[p.length - 1][0] : '')).toUpperCase(); }
  function store(k, v) { try { if (v === undefined) return sessionStorage.getItem(k); sessionStorage.setItem(k, v); } catch (e) { return null; } }
  function local(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
  function get(url) {
    return fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(function (j) { return j.data; });
  }
  function openConv() { var m = /^\/chat\/?$/.test(location.pathname) && /^#c(\d+)$/.exec(location.hash); return m ? +m[1] : null; }

  function css() {
    if (document.getElementById('ca-css')) return;
    var st = document.createElement('style'); st.id = 'ca-css';
    st.textContent =
      '.ca-stack{position:fixed;right:calc(20px + env(safe-area-inset-right,0px));bottom:calc(20px + env(safe-area-inset-bottom,0px));display:flex;flex-direction:column-reverse;gap:10px;z-index:10010;width:360px;max-width:calc(100vw - 32px);pointer-events:none}' +
      '.ca-stack>*{pointer-events:auto}' +
      '.ca{background:#fff;border:1px solid var(--line,#eceef3);border-radius:14px;box-shadow:0 14px 40px rgba(20,28,60,.18);padding:12px 12px 12px 14px;display:flex;gap:10px;position:relative;overflow:hidden;font-family:inherit;color:var(--ink,#222633);text-align:left}' +
      '@media (prefers-reduced-motion:no-preference){.ca{animation:ca-in .28s cubic-bezier(.2,.8,.2,1)}@keyframes ca-in{from{opacity:0;transform:translateY(12px) scale(.98)}}}' +
      '.ca.at{border-color:rgba(177,30,46,.35)}' +
      '.ca .ca-av{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;color:#fff;font-weight:700;font-size:12.5px;flex:none}' +
      '.ca .ca-bd{min-width:0;flex:1}' +
      '.ca .ca-hd{display:flex;align-items:baseline;gap:6px;font-size:13px;padding-right:18px}' +
      '.ca .ca-hd b{font-weight:700;white-space:nowrap}' +
      '.ca .ca-where{color:var(--muted,#8c92a0);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}' +
      '.ca .ca-tag{font-size:10.5px;font-weight:700;color:#B11E2E;background:rgba(177,30,46,.08);border-radius:5px;padding:1px 5px;flex:none}' +
      '.ca .ca-msg{font-size:13.5px;line-height:1.45;margin-top:2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}' +
      '.ca .ca-th{width:46px;height:46px;border-radius:10px;object-fit:cover;flex:none;border:1px solid var(--line,#eceef3);background:#f0f2f7}' +
      '.ca .ca-acts{display:flex;gap:6px;margin-top:8px}' +
      '.ca .ca-acts button,.ca .ca-rep button{border:1px solid var(--line,#eceef3);background:#fff;color:var(--ink,#222633);border-radius:10px;padding:5px 10px;font:inherit;font-size:12.5px;font-weight:600;cursor:pointer}' +
      '.ca .ca-acts .ca-open,.ca .ca-rep button{background:var(--accent,#4364dc);border-color:var(--accent,#4364dc);color:#fff}' +
      '.ca .ca-x{position:absolute;right:6px;top:6px;border:none;background:none;color:var(--muted,#8c92a0);font-size:14px;width:24px;height:24px;border-radius:6px;cursor:pointer}' +
      '.ca .ca-x:hover{background:#f3f5fa}' +
      '.ca .ca-rep{display:flex;gap:6px;margin-top:8px}' +
      '.ca .ca-rep input{flex:1;min-width:0;border:1px solid var(--line,#eceef3);background:var(--canvas,#fafbfd);color:var(--ink,#222633);border-radius:10px;padding:7px 9px;font:inherit;font-size:13px}' +
      '.ca .ca-err{color:#c2453a;font-size:12px;margin-top:4px}' +
      '.ca .ca-bar{position:absolute;left:0;bottom:0;height:3px;background:var(--accent-edge,rgba(67,100,220,.3));width:100%;transform-origin:left}' +
      '.ca.sent .ca-msg{color:var(--muted,#8c92a0)}' +
      '.ca-more{align-self:flex-end;border:1px solid var(--line,#eceef3);background:#fff;border-radius:99px;padding:5px 12px;font:inherit;font-size:12px;font-weight:600;color:var(--ink-2,#4A4F59);box-shadow:0 8px 24px rgba(20,28,60,.14);cursor:pointer;text-decoration:none}' +
      '.ca-osn{background:#222633;color:#fff;border-radius:14px;padding:12px 14px;box-shadow:0 14px 40px rgba(20,28,60,.18);font-size:13px;line-height:1.45}' +
      '.ca-osn .ca-row{display:flex;gap:8px;margin-top:8px}' +
      '.ca-osn button{border:none;border-radius:10px;padding:6px 10px;font:inherit;font-weight:600;font-size:12.5px;cursor:pointer}' +
      '.ca-osn .ca-y{background:var(--accent,#4364dc);color:#fff}.ca-osn .ca-n{background:transparent;color:#fff;border:1px solid rgba(255,255,255,.3)}' +
      '@media (max-width:860px){.ca-stack{left:max(10px,env(safe-area-inset-left,0px));right:max(10px,env(safe-area-inset-right,0px));top:calc(10px + env(safe-area-inset-top,0px));bottom:auto;width:auto;max-width:none;flex-direction:column}}';
    document.head.appendChild(st);
  }
  function stack() {
    var s = document.getElementById('ca-stack');
    if (!s) { css(); s = document.createElement('div'); s.id = 'ca-stack'; s.className = 'ca-stack'; s.setAttribute('aria-live', 'polite'); document.body.appendChild(s); }
    return s;
  }
  function collapse() {
    var s = stack(), cards = s.querySelectorAll('.ca'), more = s.querySelector('.ca-more');
    for (var i = 0; i < cards.length; i++) cards[i].hidden = i < cards.length - MAX;
    var extra = Math.max(0, cards.length - MAX);
    if (extra) {
      if (!more) { more = document.createElement('a'); more.className = 'ca-more'; more.href = '/chat'; s.insertBefore(more, s.firstChild); }
      more.textContent = '+' + extra + ' more in Chat';
    } else if (more) more.remove();
  }

  function card(a) {
    var s = stack();
    var el = document.createElement('div');
    el.className = 'ca' + (a.mention ? ' at' : ''); el.setAttribute('role', 'status');
    var text = a.body || (a.image_id ? 'Sent a photo' : a.files ? (a.files > 1 ? 'Sent ' + a.files + ' files' : 'Sent a file') : '');
    el.innerHTML = '<div class="ca-av" style="background:' + color(a.author) + '">' + esc(ini(a.who)) + '</div>' +
      '<div class="ca-bd"><div class="ca-hd"><b>' + esc(a.who) + '</b><span class="ca-where">' + esc(a.where) + '</span>' + (a.mention ? '<span class="ca-tag">@ you</span>' : '') + '</div>' +
      '<div class="ca-msg">' + esc(text) + '</div><div class="ca-acts"><button type="button" class="ca-open">Open chat</button><button type="button" class="ca-reply">Reply</button></div></div>' +
      (a.image_id ? '<img class="ca-th" alt="" src="/api/chat/files/' + (+a.image_id) + '?thumb=1">' : '') +
      '<button class="ca-x" type="button" aria-label="Dismiss">✕</button><div class="ca-bar"></div>';
    s.appendChild(el);
    var life = (a.mention || a.dm) ? 12000 : 8000, left = life, t0 = 0, timer = null, paused = false;
    var bar = el.querySelector('.ca-bar');
    function run() {
      t0 = Date.now(); timer = setTimeout(close, left);
      bar.style.transition = 'none'; bar.style.transform = 'scaleX(' + (left / life) + ')'; void bar.offsetWidth;
      bar.style.transition = 'transform ' + left + 'ms linear'; bar.style.transform = 'scaleX(0)';
    }
    function pause() { if (paused) return; paused = true; clearTimeout(timer); left -= Date.now() - t0; var tr = getComputedStyle(bar).transform; bar.style.transition = 'none'; bar.style.transform = tr; }
    function close() { clearTimeout(timer); el.remove(); collapse(); }
    el.addEventListener('mouseenter', pause);
    el.addEventListener('mouseleave', function () { if (!el.querySelector('.ca-rep')) { paused = false; run(); } });
    el.querySelector('.ca-x').addEventListener('click', close);
    el.querySelector('.ca-open').addEventListener('click', function () { close(); location.href = '/chat#c' + a.conv_id; });
    el.querySelector('.ca-reply').addEventListener('click', function () {
      pause();
      var acts = el.querySelector('.ca-acts'), f = document.createElement('form');
      f.className = 'ca-rep';
      f.innerHTML = '<input class="ssc-field" aria-label="Reply" maxlength="4000" placeholder="Reply to ' + esc(String(a.who).split(' ')[0]) + '"><button type="submit">Send</button>';
      acts.replaceWith(f);
      var inp = f.querySelector('input'); inp.focus();
      f.addEventListener('submit', function (e) {
        e.preventDefault();
        var v = inp.value.trim(); if (!v) return;
        f.querySelector('button').disabled = true;
        fetch('/api/chat/convs/' + a.conv_id + '/messages', { method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ body: v, reply_to: a.id }) })
          .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || 'Could not send. Try again.'); return j; }); })
          .then(function () {
            el.classList.add('sent'); el.querySelector('.ca-msg').textContent = 'You: ' + v; f.remove();
            fetch('/api/chat/convs/' + a.conv_id + '/read', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ last_id: a.id }) })
              .then(function () { document.dispatchEvent(new CustomEvent('ssc:chat-refresh')); }).catch(function () {});
            left = 1800; paused = false; run();
          })
          .catch(function (er) {
            f.querySelector('button').disabled = false;
            var e2 = el.querySelector('.ca-err') || el.querySelector('.ca-bd').appendChild(document.createElement('div'));
            e2.className = 'ca-err'; e2.textContent = er.message;
          });
      });
    });
    run(); collapse();
  }

  function osn(a) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    try {
      var n = new Notification(a.who + (a.dm ? '' : ' · ' + a.where), {
        body: (a.mention ? '@ you · ' : '') + (a.body || (a.image_id ? 'Sent a photo' : 'Sent a file')),
        tag: 'ssc-chat-' + a.id, icon: '/files/static/brand/favicon-32.png' });
      n.onclick = function () { window.focus(); location.href = '/chat#c' + a.conv_id; n.close(); };
    } catch (e) { /* some phones only allow notifications from an installed app */ }
  }
  function offerOsn() {
    if (!('Notification' in window) || Notification.permission !== 'default' || local(ASKED)) return;
    local(ASKED, '1');
    var o = document.createElement('div'); o.className = 'ca-osn';
    o.innerHTML = 'Show chat alerts on your computer when this tab is in the background?<div class="ca-row"><button type="button" class="ca-y">Turn on</button><button type="button" class="ca-n">Not now</button></div>';
    stack().appendChild(o);
    o.querySelector('.ca-y').addEventListener('click', function () { o.remove(); try { Notification.requestPermission(); } catch (e) {} });
    o.querySelector('.ca-n').addEventListener('click', function () { o.remove(); });
  }

  function check() {
    if (state.busy) return;
    state.busy = true;
    get('/api/chat/poll').then(function (d) {
      if (d.rev === state.rev) return;
      state.rev = d.rev;
      return get('/api/chat/alerts' + (state.last == null ? '' : '?after=' + state.last)).then(function (r) {
        var first = state.last == null;
        state.last = r.latest; store(KEY, String(r.latest));
        if (first) return;
        var reading = !document.hidden ? openConv() : null, shown = 0;
        (r.alerts || []).forEach(function (a) {
          if (a.conv_id === reading) return;   // you are looking at it
          if (document.hidden) osn(a); else { card(a); shown++; }
        });
        if ((r.alerts || []).length) document.dispatchEvent(new CustomEvent('ssc:chat-refresh'));
        if (shown) offerOsn();
      });
    }).catch(function () {}).then(function () { state.busy = false; });
  }
  function schedule() { clearTimeout(state.timer); state.timer = setTimeout(function () { check(); schedule(); }, document.hidden ? 20000 : 6000); }

  function start() {
    var saved = store(KEY);
    if (saved !== null && saved !== '' && !isNaN(+saved)) state.last = +saved;   // same tab, a new page: pick up where it left off
    check(); schedule();
    document.addEventListener('visibilitychange', function () { if (!document.hidden) check(); schedule(); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
