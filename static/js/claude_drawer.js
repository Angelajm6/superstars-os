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
  var usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
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
.clx-card dt{color:#868C98}.clx-card dd{margin:0;min-width:0}\
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
.clx-off{font-size:13px;background:#FFF4E5;color:#7A4A00;border-radius:10px;padding:8px 10px;line-height:1.45}\
@media (max-width:640px){#clx-panel{width:100%}#clx-fab{right:14px;bottom:14px}}\
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
      '<span><button class="clx-btn" type="button" id="clx-new">+ New</button> <button class="clx-x" type="button" id="clx-close" aria-label="Close Claude">×</button></span></div>' +
      '<div class="clx-ctx" id="clx-ctx"></div>' +
      '<div class="clx-row"><select id="clx-chats" aria-label="Your conversations"></select></div></div>' +
      '<div class="clx-msgs" id="clx-msgs" aria-live="polite"></div>' +
      '<div class="clx-f"><div id="clx-off" hidden></div><form id="clx-form"><textarea id="clx-in" rows="2" placeholder="Ask or tell Claude…" aria-label="Message to Claude"></textarea><button class="clx-btn go" id="clx-send" type="submit">Send</button></form>' +
      '<div class="clx-foot"><span>Changes always wait for your approval</span><span id="clx-spend"></span></div></div>';
    document.body.appendChild(p);
    fab.addEventListener('click', function () { setOpen(!S.open); });
    $('clx-close').addEventListener('click', function () { setOpen(false); });
    $('clx-new').addEventListener('click', function () { S.chat = null; store('chat', null); renderChats(); render(); $('clx-in').focus(); });
    $('clx-chats').addEventListener('change', function () { var v = $('clx-chats').value; if (v) openChat(+v); else { S.chat = null; render(); } });
    $('clx-form').addEventListener('submit', function (e) { e.preventDefault(); send($('clx-in').value); });
    $('clx-in').addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send($('clx-in').value); } });
    $('clx-msgs').addEventListener('click', onCardClick);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && S.open) setOpen(false); });
  }

  function setOpen(open) {
    S.open = open; store('open', open ? '1' : null);
    var p = $('clx-panel'); $('clx-fab').setAttribute('aria-expanded', String(open));
    if (open) { p.hidden = false; requestAnimationFrame(function () { p.classList.add('open'); }); ctx(); if (!S.loaded) loadChats(); setTimeout(function () { $('clx-in').focus(); }, 60); }
    else { p.classList.remove('open'); setTimeout(function () { if (!S.open) p.hidden = true; }, 240); }
  }
  function ctx() { var pg = page(); $('clx-ctx').innerHTML = 'Knows you are on <span class="clx-chip">' + esc(pg.label) + '</span>'; }
  function spend(spent, cap) { if (cap != null) $('clx-spend').textContent = 'Today ' + usd.format(spent || 0) + ' of ' + usd.format(cap); }

  async function loadChats() {
    S.loaded = true;
    try {
      var r = await api('/chats?surface=' + SURFACE); S.chats = r.data || [];
      var keep = +load('chat'); renderChats(keep);
      if (keep && S.chats.some(function (c) { return c.id === keep; })) await openChat(keep); else render();
    } catch (e) { $('clx-msgs').innerHTML = '<div class="clx-err">' + esc(e.message) + '</div>'; }
  }
  function renderChats(sel) {
    var cur = sel || (S.chat && S.chat.chat.id) || '';
    $('clx-chats').innerHTML = '<option value="">New conversation</option>' + S.chats.map(function (c) { return '<option value="' + c.id + '">' + esc(c.title) + ' · ' + esc(when(c.updated_at)) + '</option>'; }).join('');
    $('clx-chats').value = String(cur || '');
  }
  async function openChat(id) {
    try { var r = await api('/chats/' + id + '?surface=' + SURFACE); apply(r.data); }
    catch (e) { S.chat = null; store('chat', null); render(); }
  }
  function apply(view) {
    S.chat = view; store('chat', view.chat.id);
    if (!S.chats.some(function (c) { return c.id === view.chat.id; })) S.chats.unshift({ id: view.chat.id, title: view.chat.title, updated_at: view.chat.updated_at });
    S.chats.forEach(function (c) { if (c.id === view.chat.id) c.title = view.chat.title; });
    renderChats(view.chat.id); render();
    clearTimeout(S.poll);
    if (view.busy) S.poll = setTimeout(function () { openChat(view.chat.id).then(refreshStatus); }, 1500);
  }
  async function refreshStatus() {
    try { var r = await api('/status?surface=' + SURFACE); S.status = r.data; spend(S.status.spent_today_usd, S.status.cap_usd); } catch (e) {}
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
    build(); spend(S.status.spent_today_usd, S.status.cap_usd);
    if (load('open') === '1') setOpen(true);
  }
  window.SSC_CLAUDE = { open: function () { setOpen(true); } };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
