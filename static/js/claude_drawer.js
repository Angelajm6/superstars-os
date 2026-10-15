/* Claude across the platform (claude_agent.py) — the "Ask Claude" button and drawer.
 *
 * Loaded on every office page and on the job dashboard. It stays hidden for roles Claude is
 * not offered to. Office pages and job dashboards keep separate conversations (a job
 * dashboard can be seen in the field, so pay talk never shows there). Lookups answer
 * straight away; every change comes back as an approval card and runs only on Approve.
 * Vanilla JS, no build step; styles injected once; ids prefixed `clx-`.
 */
(function () {
  'use strict';
  if (window.SSC_CLAUDE) return;
  var FIELD = /^\/projects\//.test(location.pathname) || /dashboard-static/.test(location.pathname);
  var SURFACE = FIELD ? 'field' : 'office';
  var KEY = 'ssc.claude.' + SURFACE;
  var S = { status: null, chat: null, chats: [], poll: null, open: false, sending: false };

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function $(id) { return document.getElementById(id); }
  function store(k, v) { try { if (v == null) sessionStorage.removeItem(KEY + k); else sessionStorage.setItem(KEY + k, v); } catch (e) {} }
  function load(k) { try { return sessionStorage.getItem(KEY + k); } catch (e) { return null; } }
  async function api(path, opts) {
    var res = await fetch('/api/claude' + path, Object.assign({ credentials: 'same-origin', headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' } }, opts || {}));
    var j = await res.json().catch(function () { return {}; });
    if (!res.ok) { var e = new Error((j && j.error) || ('Request failed (' + res.status + ')')); e.status = res.status; throw e; }
    return j;
  }
  function fmtDay(v) { var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v || ''); if (!m) return v; return new Date(+m[1], +m[2] - 1, +m[3]).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' }); }
  function fmtTime(v) { var m = /^(\d{2}):(\d{2})$/.exec(v || ''); if (!m) return v; var h = +m[1]; return (h % 12 || 12) + ':' + m[2] + (h < 12 ? ' AM' : ' PM'); }
  function when(iso) { var d = new Date(iso); return isNaN(d) ? '' : d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); }

  // what the person is looking at: the sidebar tab, the sub-tab and the page title
  function page() {
    var parts = [];
    var tab = document.querySelector('.ssc-nav a.active, [data-company-nav] a.active, .side-nav a.active');
    var sub = document.querySelector('.ssc-subtab.active');
    var h = document.querySelector('#cp-title, .page-title, h1');
    [tab, sub, h].forEach(function (el) { var t = el && (el.textContent || '').trim(); if (t && parts.indexOf(t) < 0 && t.length < 80) parts.push(t); });
    var m = location.pathname.match(/^\/projects\/([^/?#]+)/);
    return { surface: SURFACE, path: location.pathname + location.hash, label: parts.join(' › ') || document.title, project_code: m ? decodeURIComponent(m[1]) : '' };
  }

  var CSS = '\
#clx-fab{position:fixed;right:20px;bottom:20px;z-index:9000;display:flex;align-items:center;gap:8px;padding:10px 16px;border:0;border-radius:999px;background:#C2603B;color:#fff;font:600 13.5px Inter,-apple-system,"Segoe UI",sans-serif;box-shadow:0 6px 20px rgba(120,50,20,.28);cursor:pointer}\
#clx-fab:focus-visible,#clx-panel button:focus-visible,#clx-panel select:focus-visible,#clx-panel textarea:focus-visible,#clx-panel input:focus-visible{outline:2px solid #24418C;outline-offset:2px}\
#clx-fab .clx-star,#clx-panel .clx-star{display:inline-grid;place-items:center;width:20px;height:20px;border-radius:6px;background:rgba(255,255,255,.22);font-size:12px}\
#clx-panel{position:fixed;top:0;right:0;bottom:0;width:440px;max-width:100%;box-sizing:border-box;z-index:9001;background:#fff;color:#1B1F27;border-left:1px solid #E6E8EE;box-shadow:-10px 0 30px rgba(20,22,28,.12);display:flex;flex-direction:column;font:14px Inter,-apple-system,"Segoe UI",sans-serif;transform:translateX(105%);transition:transform .22s ease}\
#clx-panel.open{transform:none}\
#clx-panel[hidden]{display:none}\
.clx-h{padding:12px 14px;border-bottom:1px solid #E6E8EE;display:grid;grid-template-columns:minmax(0,1fr);gap:8px}\
.clx-t{display:flex;align-items:center;gap:8px;justify-content:space-between}\
.clx-t b{display:flex;align-items:center;gap:8px;font-size:15px}\
#clx-panel .clx-t b .clx-star{background:#C2603B;color:#fff}\
.clx-btn{border:1px solid #E6E8EE;background:#fff;color:#1B1F27;border-radius:10px;padding:6px 10px;font:600 12.5px Inter,sans-serif;cursor:pointer}\
.clx-btn.ok{background:#0c7a5f;border-color:#0c7a5f;color:#fff}\
.clx-btn.go{background:#C2603B;border-color:#C2603B;color:#fff}\
.clx-btn[disabled]{opacity:.5;cursor:default}\
.clx-x{border:0;background:none;font-size:20px;line-height:1;color:#5B6170;cursor:pointer;padding:2px 6px}\
.clx-ctx{font-size:12px;color:#868C98;display:flex;gap:6px;flex-wrap:wrap;align-items:center}\
.clx-chip{background:#E8EDF8;color:#24418C;border-radius:6px;padding:2px 8px;font-weight:600}\
.clx-row{display:flex;gap:8px}\
.clx-row select{flex:1;min-width:0;width:100%;text-overflow:ellipsis;font:13px Inter,sans-serif;border:1px solid #E6E8EE;border-radius:10px;padding:6px 8px;background:#fff;color:#1B1F27}\
.clx-msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:12px}\
.clx-m{display:grid;gap:4px;max-width:96%}\
.clx-m .who{font-size:12px;color:#868C98}\
.clx-m.u{align-self:flex-end}.clx-m.u .who{text-align:right}\
.clx-b{padding:9px 12px;border-radius:10px;line-height:1.5;font-size:13.5px;white-space:pre-wrap;overflow-wrap:anywhere}\
.clx-m.u .clx-b{background:#E8EDF8}\
.clx-m.a .clx-b{background:#F4F5F8}\
.clx-looked{font-size:12px;color:#868C98;display:flex;gap:6px;flex-wrap:wrap;align-items:center}\
.clx-looked span{background:#fff;border:1px solid #E6E8EE;border-radius:6px;padding:1px 7px}\
.clx-card{border:1px solid #E7C3B3;background:#fff;border-radius:10px;padding:10px 12px;display:grid;gap:8px}\
.clx-card.approved{border-color:#0c7a5f}.clx-card.declined,.clx-card.expired,.clx-card.undone,.clx-card.failed{opacity:.7}\
.clx-card-h{display:flex;justify-content:space-between;align-items:center;gap:8px}\
.clx-card-h b{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#A84E2C}\
.clx-tag{font-size:12px;font-weight:600;border-radius:6px;padding:2px 8px;background:#FBEFE9;color:#A84E2C}\
.clx-card dl{display:grid;grid-template-columns:96px 1fr;gap:6px 10px;margin:0;font-size:13px;align-items:center}\
.clx-card dt{color:#868C98}.clx-card dd{margin:0;min-width:0;white-space:pre-wrap;overflow-wrap:anywhere}\
.clx-card dd input,.clx-card dd select,.clx-card dd textarea{width:100%;box-sizing:border-box;font:13px Inter,sans-serif;color:#1B1F27;background:#fff;border:1px solid #E6E8EE;border-radius:8px;padding:5px 8px}\
.clx-card dd textarea{min-height:52px;resize:vertical}\
.clx-card dd .hint{display:block;font-size:12px;color:#868C98;margin-top:2px}\
.clx-acts{display:flex;gap:6px;flex-wrap:wrap;align-items:center}\
.clx-res{font-size:12.5px;font-weight:600;color:#0c7a5f}.clx-res.no{color:#868C98}.clx-res.bad{color:#A8382B}\
.clx-res a{color:inherit}\
.clx-err{font-size:12.5px;color:#A8382B}\
.clx-wait{font-size:12.5px;color:#868C98}\
.clx-intro{font-size:13px;color:#5B6170;line-height:1.5;display:grid;gap:8px}\
.clx-sugg{display:flex;gap:6px;flex-wrap:wrap}\
.clx-sugg button{border:1px solid #E6E8EE;background:#fff;border-radius:999px;padding:6px 10px;font:12.5px Inter,sans-serif;color:#5B6170;cursor:pointer;text-align:left}\
.clx-sugg button:hover{border-color:#C2603B;color:#A84E2C}\
.clx-f{border-top:1px solid #E6E8EE;padding:10px 12px;display:grid;gap:6px}\
.clx-f form{display:flex;gap:8px;align-items:flex-end}\
.clx-f textarea{flex:1;font:13.5px Inter,sans-serif;color:#1B1F27;border:1px solid #E6E8EE;border-radius:10px;padding:8px 10px;resize:none;height:44px;max-height:160px}\
.clx-foot{display:flex;justify-content:space-between;gap:8px;font-size:12px;color:#868C98;flex-wrap:wrap}\
.clx-inline{background:#F4F5F8;border-radius:10px;padding:9px 12px;line-height:1.5;font-size:13.5px;white-space:pre-wrap;margin-bottom:10px}\
.clx-off{font-size:13px;background:#FFF4E5;color:#7A4A00;border-radius:10px;padding:8px 10px;line-height:1.45}\
.clx-tools{display:flex;gap:6px;align-items:center}\
.clx-btn[aria-pressed="true"]{background:#E8EDF8;border-color:#24418C;color:#24418C}\
.clx-title{display:flex;align-items:center;gap:8px;min-width:0}\
.clx-title[hidden],.clx-histp[hidden],.clx-msgs[hidden]{display:none}\
.clx-tname{flex:1;min-width:0;font-weight:600;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\
.clx-title input{flex:1;min-width:0;font:13.5px Inter,sans-serif;border:1px solid #24418C;border-radius:8px;padding:5px 8px}\
.clx-hstar{border:1px solid #E6E8EE;background:#fff;border-radius:8px;padding:4px 8px;font:600 12.5px Inter,sans-serif;color:#5B6170;cursor:pointer;white-space:nowrap}\
.clx-hstar.on{color:#A84E2C;border-color:#E7C3B3;background:#FBEFE9}\
.clx-link{border:0;background:none;font:600 12.5px Inter,sans-serif;color:#24418C;cursor:pointer;padding:4px 2px}\
.clx-histp{flex:1;overflow-y:auto;padding:12px 14px;display:grid;align-content:start;gap:10px}\
.clx-hbar{display:flex;gap:8px;flex-wrap:wrap}\
.clx-hbar input{flex:1;min-width:160px;font:13px Inter,sans-serif;border:1px solid #E6E8EE;border-radius:10px;padding:7px 10px}\
.clx-seg{display:flex;border:1px solid #E6E8EE;border-radius:10px;overflow:hidden}\
.clx-seg button{border:0;background:#fff;font:600 12.5px Inter,sans-serif;color:#5B6170;padding:6px 10px;cursor:pointer}\
.clx-seg button[aria-pressed="true"]{background:#E8EDF8;color:#24418C}\
.clx-hlist{display:grid;gap:6px}\
.clx-hi{display:flex;align-items:center;gap:8px;border:1px solid #E6E8EE;border-radius:10px;padding:8px 10px}\
.clx-hi.cur{border-color:#24418C}\
.clx-hopen{flex:1;min-width:0;border:0;background:none;text-align:left;cursor:pointer;display:grid;gap:2px;padding:0;font:13px Inter,sans-serif;color:#1B1F27}\
.clx-hopen b{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\
.clx-hopen span{font-size:12px;color:#868C98}\
.clx-hrm{border:0;background:none;font:600 12px Inter,sans-serif;color:#A8382B;cursor:pointer;padding:4px}\
.clx-grip{position:absolute;left:-5px;top:0;bottom:0;width:10px;cursor:col-resize;z-index:2;touch-action:none}\
.clx-grip::after{content:"";position:absolute;left:3px;top:50%;width:4px;height:44px;margin-top:-22px;border-radius:4px;background:#D5D8E0;opacity:0;transition:opacity .15s}\
#clx-panel:hover .clx-grip::after,.clx-grip:focus-visible::after,#clx-panel.clx-drag .clx-grip::after{opacity:1;background:#C2603B}\
.clx-grip:focus-visible{outline:none}\
#clx-panel.clx-drag{transition:none;user-select:none}\
.clx-size{font-size:15px;line-height:1;padding:5px 9px}\
@media (max-width:640px){#clx-panel{width:100%}.clx-grip,.clx-size{display:none}#clx-fab{right:14px;bottom:14px}}\
@media print{#clx-fab,#clx-panel{display:none!important}}\
@media (prefers-reduced-motion:reduce){#clx-panel{transition:none}}';

  function build() {
    var st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);
    var fab = document.createElement('button');
    fab.id = 'clx-fab'; fab.type = 'button'; fab.setAttribute('aria-controls', 'clx-panel'); fab.setAttribute('aria-expanded', 'false');
    fab.innerHTML = '<span class="clx-star" aria-hidden="true">✦</span>Ask Claude';
    document.body.appendChild(fab);
    var p = document.createElement('aside');
    p.id = 'clx-panel'; p.hidden = true; p.setAttribute('aria-label', 'Claude');
    p.innerHTML =
      '<div class="clx-h"><div class="clx-t"><b><span class="clx-star" aria-hidden="true">✦</span>Claude</b>' +
      '<span class="clx-tools"><button class="clx-btn" type="button" id="clx-hist" aria-pressed="false">History</button><button class="clx-btn" type="button" id="clx-new">+ New</button><button class="clx-btn clx-size" type="button" id="clx-size" aria-label="Make the Claude window bigger" title="Bigger">⤢</button><button class="clx-x" type="button" id="clx-close" aria-label="Close Claude">×</button></span></div>' +
      '<div class="clx-title" id="clx-title" hidden></div></div>' +
      '<div class="clx-histp" id="clx-histp" hidden><div class="clx-hbar"><input id="clx-q" type="search" placeholder="Search your conversations" aria-label="Search your conversations"><span class="clx-seg"><button type="button" data-saved="0" aria-pressed="true">All</button><button type="button" data-saved="1" aria-pressed="false">★ Saved</button></span></div><div id="clx-hlist" class="clx-hlist"></div></div>' +
      '<div class="clx-msgs" id="clx-msgs" aria-live="polite"></div>' +
      '<div class="clx-f"><div id="clx-off" hidden></div><form id="clx-form"><textarea id="clx-in" rows="2" placeholder="Ask or tell Claude…" aria-label="Message to Claude"></textarea><button class="clx-btn go" id="clx-send" type="submit">Send</button></form>' +
      '<div class="clx-foot"><span>Changes always wait for your approval</span></div></div>' +
      '<div class="clx-grip" id="clx-grip" role="separator" aria-orientation="vertical" tabindex="0" aria-label="Resize the Claude window (drag, or use the arrow keys)"></div>';
    document.body.appendChild(p);
    fab.addEventListener('click', function () { setOpen(!S.open); });
    $('clx-close').addEventListener('click', function () { setOpen(false); });
    $('clx-new').addEventListener('click', function () { clearTimeout(S.poll); S.chat = null; store('chat', null); showHistory(false); renderTitle(); render(); $('clx-in').focus(); });
    $('clx-hist').addEventListener('click', function () { showHistory(!S.hist); });
    $('clx-size').addEventListener('click', function () { setWidth(curWidth() < WIDE() - 20 ? WIDE() : NARROW, true); });
    wireGrip();
    $('clx-q').addEventListener('input', function () { clearTimeout(S.qt); S.qt = setTimeout(loadHistory, 250); });
    document.querySelectorAll('#clx-histp [data-saved]').forEach(function (b) { b.addEventListener('click', function () {
      S.saved = b.getAttribute('data-saved') === '1';
      document.querySelectorAll('#clx-histp [data-saved]').forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
      loadHistory(); }); });
    $('clx-hlist').addEventListener('click', onHistoryClick);
    $('clx-title').addEventListener('click', onTitleClick);
    $('clx-form').addEventListener('submit', function (e) { e.preventDefault(); send($('clx-in').value); });
    $('clx-in').addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send($('clx-in').value); } });
    $('clx-msgs').addEventListener('click', onCardClick);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && S.open) setOpen(false); });
  }

  // ---- size: drag the left edge (or arrow keys on it), or the ⤢ button; remembered on this device
  var NARROW = 440, MIN = 360;
  function MAXW() { return Math.max(MIN, Math.min(1200, window.innerWidth - 80)); }
  function WIDE() { return Math.max(NARROW, Math.min(880, MAXW())); }
  function phone() { return window.innerWidth <= 640; }
  function curWidth() { return $('clx-panel').getBoundingClientRect().width || NARROW; }
  function setWidth(w, save) {
    var p = $('clx-panel'); if (!p) return;
    if (phone()) { p.style.width = ''; return; }
    w = Math.round(Math.max(MIN, Math.min(MAXW(), w)));
    p.style.width = w + 'px';
    var big = w >= WIDE() - 20, b = $('clx-size');
    b.textContent = big ? '⤡' : '⤢'; b.title = big ? 'Smaller' : 'Bigger';
    b.setAttribute('aria-label', big ? 'Make the Claude window smaller' : 'Make the Claude window bigger');
    $('clx-grip').setAttribute('aria-valuenow', String(w));
    if (save) { try { localStorage.setItem('ssc.claude.width', String(w)); } catch (e) {} }
  }
  function sizeFromStore() {
    var w = NARROW; try { w = +localStorage.getItem('ssc.claude.width') || NARROW; } catch (e) {}
    setWidth(w, false);
    window.addEventListener('resize', function () { setWidth(curWidth(), false); });
  }
  function wireGrip() {
    var g = $('clx-grip'), p = $('clx-panel');
    g.setAttribute('aria-valuemin', String(MIN));
    g.addEventListener('pointerdown', function (e) {
      if (phone()) return;
      e.preventDefault(); g.setPointerCapture(e.pointerId); p.classList.add('clx-drag');
      var move = function (ev) { setWidth(window.innerWidth - ev.clientX, false); };
      var up = function () { g.removeEventListener('pointermove', move); g.removeEventListener('pointerup', up); g.removeEventListener('pointercancel', up);
        p.classList.remove('clx-drag'); setWidth(curWidth(), true); };
      g.addEventListener('pointermove', move); g.addEventListener('pointerup', up); g.addEventListener('pointercancel', up);
    });
    g.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft') { e.preventDefault(); setWidth(curWidth() + 40, true); }
      if (e.key === 'ArrowRight') { e.preventDefault(); setWidth(curWidth() - 40, true); }
    });
    g.addEventListener('dblclick', function () { setWidth(NARROW, true); });
  }

  function setOpen(open) {
    S.open = open; store('open', open ? '1' : null);
    var p = $('clx-panel'); $('clx-fab').setAttribute('aria-expanded', String(open));
    if (open) { p.hidden = false; requestAnimationFrame(function () { p.classList.add('open'); }); if (!S.loaded) loadChats(); setTimeout(function () { $('clx-in').focus(); }, 60); }
    else { p.classList.remove('open'); setTimeout(function () { if (!S.open) p.hidden = true; }, 240); }
  }

  async function loadChats() {
    S.loaded = true;
    var keep = +load('chat');
    if (keep) await openChat(keep); else { renderTitle(); render(); }
  }
  async function openChat(id) {
    try { var r = await api('/chats/' + id + '?surface=' + SURFACE); if (r.data.chat.archived) throw new Error('removed'); apply(r.data); }
    catch (e) { S.chat = null; store('chat', null); renderTitle(); render(); }
  }

  // ---- History: every conversation is saved; ★ keeps one on top; rename; remove from the list
  function showHistory(on) {
    S.hist = on; $('clx-hist').setAttribute('aria-pressed', String(on));
    $('clx-histp').hidden = !on; $('clx-msgs').hidden = on;
    if (on) { loadHistory(); setTimeout(function () { $('clx-q').focus(); }, 30); }
  }
  async function loadHistory() {
    var q = $('clx-q').value.trim(), box = $('clx-hlist');
    try {
      var r = await api('/chats?surface=' + SURFACE + (S.saved ? '&saved=1' : '') + (q ? '&q=' + encodeURIComponent(q) : ''));
      var rows = r.data || [];
      box.innerHTML = rows.length ? rows.map(function (c) {
        var cur = S.chat && S.chat.chat.id === c.id;
        return '<div class="clx-hi' + (cur ? ' cur' : '') + '" data-id="' + c.id + '">' +
          '<button type="button" class="clx-hstar' + (c.starred ? ' on' : '') + '" data-star="' + c.id + '" aria-pressed="' + c.starred + '" aria-label="' + (c.starred ? 'Unsave' : 'Save') + '">' + (c.starred ? '★' : '☆') + '</button>' +
          '<button type="button" class="clx-hopen" data-open="' + c.id + '"><b>' + esc(c.title) + '</b><span>' + esc(when(c.updated_at)) + ' · ' + c.asked + ' message' + (c.asked === 1 ? '' : 's') + '</span></button>' +
          '<button type="button" class="clx-hrm" data-remove="' + c.id + '" aria-label="Remove from the list">Remove</button></div>';
      }).join('') : '<div class="clx-intro">' + (q || S.saved ? 'No conversations match.' : 'Your conversations show here once you ask Claude something.') + (SURFACE === 'field' ? ' Conversations started on office pages are listed there.' : '') + '</div>';
    } catch (e) { box.innerHTML = '<div class="clx-err">' + esc(e.message) + '</div>'; }
  }
  async function patchChat(id, body) {
    var r = await api('/chats/' + id, { method: 'PATCH', body: JSON.stringify(body) });
    if (S.chat && S.chat.chat.id === id) { if (r.data.chat.archived) { S.chat = null; store('chat', null); render(); } else S.chat.chat = r.data.chat; renderTitle(); }
    return r.data;
  }
  async function onHistoryClick(e) {
    var o = e.target.closest('[data-open]'), st = e.target.closest('[data-star]'), rm = e.target.closest('[data-remove]');
    try {
      if (o) { await openChat(+o.getAttribute('data-open')); showHistory(false); return; }
      if (st) { await patchChat(+st.getAttribute('data-star'), { starred: st.getAttribute('aria-pressed') !== 'true' }); loadHistory(); return; }
      if (rm) {
        if (rm.getAttribute('data-sure') !== '1') { rm.setAttribute('data-sure', '1'); rm.textContent = 'Remove?'; return; }
        await patchChat(+rm.getAttribute('data-remove'), { archived: true }); loadHistory();
      }
    } catch (x) { $('clx-hlist').insertAdjacentHTML('afterbegin', '<div class="clx-err">' + esc(x.message) + '</div>'); }
  }
  function renderTitle() {
    var t = $('clx-title'), c = S.chat && S.chat.chat;
    t.hidden = !c;
    if (!c) { t.innerHTML = ''; return; }
    t.innerHTML = '<span class="clx-tname" id="clx-tname">' + esc(c.title) + '</span>' +
      '<button type="button" class="clx-hstar' + (c.starred ? ' on' : '') + '" id="clx-tstar" aria-pressed="' + !!c.starred + '" title="' + (c.starred ? 'Saved: stays at the top of History' : 'Save: keep at the top of History') + '">' + (c.starred ? '★ Saved' : '☆ Save') + '</button>' +
      '<button type="button" class="clx-link" id="clx-tren">Rename</button>';
  }
  async function onTitleClick(e) {
    var c = S.chat && S.chat.chat; if (!c) return;
    try {
      if (e.target.closest('#clx-tstar')) { await patchChat(c.id, { starred: !c.starred }); return; }
      if (e.target.closest('#clx-tren')) {
        $('clx-title').innerHTML = '<input id="clx-tin" maxlength="80" aria-label="Conversation name" value="' + esc(c.title) + '"><button type="button" class="clx-btn ok" id="clx-tok">Save name</button>';
        var inp = $('clx-tin'); inp.focus(); inp.select();
        var done = async function (keep) { if (keep && inp.value.trim()) await patchChat(c.id, { title: inp.value }); else renderTitle(); };
        inp.addEventListener('keydown', function (k) { if (k.key === 'Enter') { k.preventDefault(); done(true); } if (k.key === 'Escape') { k.stopPropagation(); done(false); } });
        $('clx-tok').addEventListener('click', function () { done(true); });
      }
    } catch (x) { renderTitle(); }
  }
  function apply(view) {
    S.chat = view; store('chat', view.chat.id);
    renderTitle(); render();
    clearTimeout(S.poll);
    if (view.busy) S.poll = setTimeout(function () { openChat(view.chat.id).then(refreshStatus); }, 1500);
  }
  async function refreshStatus() {
    try { var r = await api('/status?surface=' + SURFACE); S.status = r.data; } catch (e) {}
  }

  function suggestions() {
    var st = S.status || {}, out = [];
    if ((st.looks_up || []).indexOf('Weekly payroll') >= 0) out.push('Summarize payroll for last month');
    if ((st.proposes || []).indexOf('New event') >= 0) out.push('Add a walkthrough next Tuesday at 9am');
    out.push(FIELD ? 'What is open on this job?' : 'What is on the team calendar this week?');
    if ((st.looks_up || []).indexOf('Estimates') >= 0) out.push('Which bids are due in the next 7 days?');
    return out.slice(0, 4);
  }
  function render() {
    var box = $('clx-msgs'), v = S.chat;
    var off = S.status && !S.status.available;
    $('clx-off').hidden = !off; $('clx-off').className = off ? 'clx-off' : '';
    $('clx-off').textContent = off ? 'Claude is not connected yet. An admin adds the Anthropic API key in Render.' : '';
    $('clx-send').disabled = !!off || !!(v && v.busy) || S.sending; $('clx-in').disabled = !!off;
    if (!v || !v.messages.length) {
      var st = S.status || {};
      box.innerHTML = '<div class="clx-intro"><div>Hi' + (st.name ? ' ' + esc(String(st.name).split(' ')[0]) : '') + '. Ask me anything about the platform, or tell me what to do. I show you every change before it happens.</div>' +
        (st.looks_up ? '<div><b>I can look up:</b> ' + esc(st.looks_up.join(', ')) + '.</div>' : '') +
        (st.proposes && st.proposes.length ? '<div><b>I can propose:</b> ' + esc(st.proposes.join(', ')) + '. You approve each one.</div>' : '') +
        '<div class="clx-sugg">' + suggestions().map(function (s) { return '<button type="button" data-say="' + esc(s) + '">' + esc(s) + '</button>'; }).join('') + '</div></div>' +
        (v && v.busy ? '<div class="clx-wait">Claude is working…</div>' : '');
      return;
    }
    box.innerHTML = v.messages.map(function (m) {
      if (m.role === 'user') return '<div class="clx-m u"><span class="who">You · ' + esc(when(m.created_at)) + '</span><div class="clx-b">' + esc(m.text) + '</div></div>';
      var looked = m.looked && m.looked.length ? '<div class="clx-looked">Looked at ' + m.looked.map(function (l) { return '<span>' + esc(l) + '</span>'; }).join('') + '</div>' : '';
      return '<div class="clx-m a"><span class="who">Claude · ' + esc(when(m.created_at)) + '</span>' + looked +
        (m.text ? '<div class="clx-b">' + esc(m.text) + '</div>' : '') + (m.actions || []).map(card).join('') + '</div>';
    }).join('') + (v.busy ? '<div class="clx-wait">Claude is working…</div>' : '') + (v.error ? '<div class="clx-err">' + esc(v.error) + '</div>' : '');
    // long text on a card (an email, a proposal scope) shows in full so it can be read before approving
    box.querySelectorAll('.clx-card textarea').forEach(function (t) { t.style.height = Math.min(360, t.scrollHeight + 4) + 'px'; });
    box.scrollTop = box.scrollHeight;
  }
  function field(a, f) {
    var id = 'clx-a' + a.id + '-' + f.key, live = a.status === 'pending';
    var hint = f.hint ? '<span class="hint">' + esc(f.hint) + '</span>' : '';
    if (f.type === 'readonly' || !live) {
      var shown = f.type === 'select' ? ((f.options || []).filter(function (o) { return o.value === f.value; })[0] || {}).label
        : f.type === 'date' ? fmtDay(f.value) : f.type === 'time' ? fmtTime(f.value) : f.value;
      return '<dt>' + esc(f.label) + '</dt><dd>' + esc(f.type === 'readonly' && f.hint ? f.hint : (shown || '—')) + (f.type === 'readonly' ? '' : hint) + '</dd>';
    }
    var attrs = ' id="' + id + '" data-key="' + esc(f.key) + '" aria-label="' + esc(f.label) + '"';
    var input = f.type === 'textarea' ? '<textarea' + attrs + '>' + esc(f.value) + '</textarea>'
      : f.type === 'select' ? '<select' + attrs + '>' + (f.options || []).map(function (o) { return '<option value="' + esc(o.value) + '"' + (o.value === f.value ? ' selected' : '') + '>' + esc(o.label) + '</option>'; }).join('') + '</select>'
      : '<input' + attrs + ' type="' + (f.type === 'date' || f.type === 'time' ? f.type : 'text') + '" value="' + esc(f.value) + '">';
    return '<dt><label for="' + id + '">' + esc(f.label) + '</label></dt><dd>' + input + hint + '</dd>';
  }
  function card(a) {
    var c = a.card || { title: a.label, fields: [] };
    var state = { pending: 'Needs your approval', approved: 'Approved', declined: 'Declined', expired: 'Expired', undone: 'Undone', failed: 'Failed' }[a.status] || a.status;
    var foot = a.status === 'pending'
      ? '<div class="clx-acts"><button type="button" class="clx-btn ok" data-approve="' + a.id + '">Approve</button><button type="button" class="clx-btn" data-decline="' + a.id + '">Decline</button><span class="clx-err" id="clx-e' + a.id + '"></span></div>'
      : a.status === 'approved' ? '<div class="clx-res">✓ ' + esc((a.result && a.result.summary) || 'Done') + (a.decided_by ? ' · approved by ' + esc(a.decided_by) : '') +
          (a.result && a.result.link ? ' · <a href="' + esc(a.result.link) + '">Open</a>' : '') + (a.can_undo ? ' · <a href="#" data-undo="' + a.id + '">Undo</a>' : '') + '</div>'
      : a.status === 'declined' ? '<div class="clx-res no">Declined · nothing was changed</div>'
      : a.status === 'expired' ? '<div class="clx-res no">Expired · ask Claude again</div>'
      : a.status === 'undone' ? '<div class="clx-res no">Undone</div>'
      : '<div class="clx-res bad">That change failed; nothing was saved.</div>';
    return '<div class="clx-card ' + esc(a.status) + '" id="clx-card' + a.id + '"><div class="clx-card-h"><b>' + esc(state) + '</b><span class="clx-tag">' + esc(c.title) + '</span></div>' +
      '<dl>' + (c.fields || []).filter(function (f) { return a.status === 'pending' || f.value !== ''; }).map(function (f) { return field(a, f); }).join('') + '</dl>' + foot + '</div>';
  }
  async function onCardClick(e) {
    var say = e.target.closest('[data-say]'); if (say) { send(say.getAttribute('data-say')); return; }
    var ap = e.target.closest('[data-approve]'), de = e.target.closest('[data-decline]'), un = e.target.closest('[data-undo]');
    if (!ap && !de && !un) return;
    e.preventDefault();
    var id = (ap || de || un).getAttribute(ap ? 'data-approve' : de ? 'data-decline' : 'data-undo');
    var values = {};
    if (ap) document.querySelectorAll('#clx-card' + id + ' [data-key]').forEach(function (el) {
      var v = el.value, dp = window.SSCDatePicker;   // pages that restyle date boxes show MM-DD-YYYY; send ISO
      if (dp && typeof dp.getISO === 'function' && /^\d{2}-\d{2}-\d{4}$/.test(v || '')) { var iso = dp.getISO(el); if (iso) v = iso; }
      values[el.getAttribute('data-key')] = v;
    });
    (ap || de || un).disabled = true;
    try {
      await api('/actions/' + id + '/' + (ap ? 'approve' : de ? 'decline' : 'undo'), { method: 'POST', body: JSON.stringify({ values: values }) });
      await openChat(S.chat.chat.id);
      if (ap) document.dispatchEvent(new CustomEvent('ssc:claude-changed', { detail: { action: +id } }));
    } catch (x) { var el = $('clx-e' + id); if (el) el.textContent = x.message; else { $('clx-msgs').insertAdjacentHTML('beforeend', '<div class="clx-err">' + esc(x.message) + '</div>'); } (ap || de || un).disabled = false; }
  }
  async function send(text) {
    text = (text || '').trim();
    if (!text || S.sending || (S.chat && S.chat.busy)) return;
    S.sending = true; $('clx-send').disabled = true;
    try {
      if (!S.chat) { var c = await api('/chats', { method: 'POST', body: JSON.stringify({ surface: SURFACE }) }); S.chat = c.data; }
      $('clx-in').value = '';
      var r = await api('/chats/' + S.chat.chat.id + '/messages', { method: 'POST', body: JSON.stringify({ text: text, page: page() }) });
      S.sending = false; apply(r.data);
    } catch (e) {
      S.sending = false; $('clx-in').value = text;
      if (S.chat) await openChat(S.chat.chat.id);
      var box = $('clx-msgs'); box.insertAdjacentHTML('beforeend', '<div class="clx-err">' + esc(e.message) + '</div>'); box.scrollTop = box.scrollHeight;
      render();
    }
  }

  async function boot() {
    try { var r = await api('/status?surface=' + SURFACE); S.status = r.data; }
    catch (e) { return; }   // not offered to this role (or signed out): no button
    build(); sizeFromStore();
    if (load('open') === '1') setOpen(true);
  }
  // ---- the same cards outside the drawer (the Inbox): one email's Claude pass, by its chat id
  function cardValues(id) {
    var values = {};
    document.querySelectorAll('#clx-card' + id + ' [data-key]').forEach(function (el) {
      var v = el.value, dp = window.SSCDatePicker;
      if (dp && typeof dp.getISO === 'function' && /^\d{2}-\d{2}-\d{4}$/.test(v || '')) { var iso = dp.getISO(el); if (iso) v = iso; }
      values[el.getAttribute('data-key')] = v;
    });
    return values;
  }
  function mountCards(host, chatId, onChange, opts) {
    opts = opts || {};
    var timer = null;
    async function load() {
      clearTimeout(timer);
      var v;
      try { v = (await api('/chats/' + chatId + '?surface=office')).data; }
      catch (e) { host.innerHTML = '<div class="clx-err">' + esc(e.message) + '</div>'; return; }
      var msgs = v.messages.filter(function (m) { return m.role === 'assistant'; });
      host.innerHTML = msgs.map(function (m) {
        var acts = (m.actions || []).filter(function (a) { return !(opts.skip || []).includes(a.tool); });
        return (m.text && !opts.cardsOnly ? '<div class="clx-b clx-inline">' + esc(m.text) + '</div>' : '') + acts.map(card).join('');
      }).join('') + (v.busy ? '<div class="clx-wait">Claude is reading this email…</div>' : '') + (v.error ? '<div class="clx-err">' + esc(v.error) + '</div>' : '');
      host.querySelectorAll('.clx-card textarea').forEach(function (t) { t.style.height = Math.min(420, t.scrollHeight + 4) + 'px'; });
      if (v.busy) timer = setTimeout(load, 2000);
    }
    host.onclick = async function (e) {
      var ap = e.target.closest('[data-approve]'), de = e.target.closest('[data-decline]'), un = e.target.closest('[data-undo]');
      if (!ap && !de && !un) return;
      e.preventDefault();
      var b = ap || de || un, id = b.getAttribute(ap ? 'data-approve' : de ? 'data-decline' : 'data-undo');
      b.disabled = true;
      try {
        await api('/actions/' + id + '/' + (ap ? 'approve' : de ? 'decline' : 'undo'), { method: 'POST', body: JSON.stringify({ values: ap ? cardValues(id) : {} }) });
        await load();
        if (ap) document.dispatchEvent(new CustomEvent('ssc:claude-changed', { detail: { action: +id } }));
        if (onChange) onChange();
      } catch (x) { var el = document.getElementById('clx-e' + id); if (el) el.textContent = x.message; b.disabled = false; }
    };
    load();
    return { reload: load, stop: function () { clearTimeout(timer); } };
  }
  window.SSC_CLAUDE = { open: function () { setOpen(true); }, mountCards: mountCards };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
