/* Claude across the platform (claude_agent.py) — the "Ask Claude" button and drawer.
 *
 * Loaded on every office page and on the job dashboard. It stays hidden for roles Claude is
 * not offered to. Office pages and job dashboards keep separate conversations (a job
 * dashboard can be seen in the field, so pay talk never shows there). Lookups answer
 * straight away; every change comes back as an approval card and runs only on Approve.
 * Vanilla JS, no build step; styles injected once; ids prefixed `clx-`.
 *
 * 2026-10-15: the full Claude page (/claude, claude.html) runs this same code in page mode
 * (an element #clx-page on the page): conversations | chat | files, same history as the
 * window. Files (PDF, photos, Word, Excel, CSV; 20 MB, 5 per message) attach with 📎 or by
 * dropping them on the chat; they upload first (claude_files.py), then go with the message.
 */
(function () {
  'use strict';
  if (window.SSC_CLAUDE) return;
  var FIELD = /^\/projects\//.test(location.pathname) || /dashboard-static/.test(location.pathname);
  var SURFACE = FIELD ? 'field' : 'office';
  var KEY = 'ssc.claude.' + SURFACE;
  var S = { status: null, chat: null, chats: [], poll: null, open: false, sending: false, files: [], fk: 0 };
  var HOST = document.getElementById('clx-page'), PAGE = !!HOST;   // the full Claude page
  var ACCEPT = '.pdf,.png,.jpg,.jpeg,.heic,.heif,.webp,.gif,.docx,.xlsx,.xlsm,.csv,.txt,.eml,.md';
  var MAXF = 5, MAXB = 20 * 1024 * 1024, DEFAULT_TEXT = 'Here are the attached files.';

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
.clx-pend{display:flex;gap:6px;flex-wrap:wrap}.clx-pend:empty{display:none}\
.clx-att{display:inline-flex;align-items:center;gap:7px;max-width:250px;min-width:0;box-sizing:border-box;border:1px solid #E6E8EE;background:#fff;border-radius:10px;padding:5px 7px 5px 5px;text-decoration:none;color:#1B1F27;font:13px Inter,sans-serif}\
a.clx-att:hover{border-color:#C2603B}\
.clx-att .ty{flex:none;width:28px;height:30px;border-radius:5px;display:grid;place-items:center;font:800 9px Inter,sans-serif;color:#fff;letter-spacing:.03em}\
.clx-att .ty.pdf{background:#D93025}.clx-att .ty.image{background:#1E8E5A}.clx-att .ty.excel{background:#1D6F42}.clx-att .ty.word{background:#2B579A}.clx-att .ty.text{background:#6B7280}\
.clx-att .nm{min-width:0;display:grid;line-height:1.25}\
.clx-att .nm b{font-weight:600;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\
.clx-att .nm small{font-size:11px;color:#868C98;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\
.clx-att.err{border-color:#E7B4AE}.clx-att.err .nm small{color:#A8382B;white-space:normal}.clx-att.busy .nm small{color:#A84E2C}\
.clx-att .rm{border:0;background:none;color:#868C98;font-size:17px;line-height:1;cursor:pointer;padding:0 2px}\
.clx-atts{display:flex;gap:6px;flex-wrap:wrap}.clx-m.u .clx-atts{justify-content:flex-end}\
.clx-clip{flex:none;border:1px solid #E6E8EE;background:#F6F7FA;border-radius:10px;width:40px;height:44px;display:grid;place-items:center;cursor:pointer;font-size:16px}\
.clx-clip:hover{border-color:#C2603B}\
.clx-drop{position:absolute;inset:8px;border:2px dashed #C2603B;border-radius:14px;background:rgba(251,239,233,.95);display:grid;place-items:center;align-content:center;text-align:center;font:600 14px Inter,sans-serif;color:#A84E2C;z-index:5;pointer-events:none}\
.clx-drop small{display:block;font-weight:500;color:#5B6170;margin-top:4px}\
.clx-drop[hidden]{display:none}\
a.clx-btn{text-decoration:none;display:inline-flex;align-items:center}\
#clx-panel.clx-pagemode{position:relative;top:auto;right:auto;bottom:auto;width:auto;max-width:none;height:100%;z-index:auto;transform:none;transition:none;box-shadow:none;border:0;display:grid;grid-template-columns:280px minmax(0,1fr) 260px;background:#F3F4F7}\
.clx-pagemode .clx-side{background:#fff;border-right:1px solid #E6E8EE;display:flex;flex-direction:column;min-height:0;padding:14px 12px;gap:10px}\
.clx-pagemode .clx-histp{padding:0;flex:1;min-height:0}\
.clx-pagemode .clx-mainc{display:flex;flex-direction:column;min-width:0;min-height:0;position:relative}\
.clx-pagemode .clx-h{background:#fff;padding:12px 24px}\
.clx-pagemode .clx-msgs{padding:22px max(24px,calc((100% - 800px) / 2))}\
.clx-pagemode .clx-f{border-top:0;padding:8px max(24px,calc((100% - 800px) / 2)) 18px}\
.clx-pagemode .clx-f form{background:#fff;border:1px solid #E6E8EE;border-radius:14px;padding:8px;box-shadow:0 1px 2px rgba(20,22,28,.06),0 8px 24px rgba(20,22,28,.06)}\
.clx-pagemode .clx-f textarea{border:0;height:64px}\
.clx-pagemode .clx-histbtn{display:none}\
.clx-files{background:#fff;border-left:1px solid #E6E8EE;padding:14px;display:grid;gap:12px;align-content:start;overflow-y:auto;min-height:0}\
.clx-files h3{margin:0;font:700 11px Inter,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:#868C98}\
.clx-fnote{font-size:12px;color:#5B6170;background:#F6F7FA;border-radius:10px;padding:10px 12px;line-height:1.5}\
.clx-flist{display:grid;gap:8px}.clx-flist .clx-att{max-width:none}\
@media (max-width:1180px){#clx-panel.clx-pagemode{grid-template-columns:250px minmax(0,1fr)}.clx-pagemode .clx-files{display:none}}\
@media (max-width:820px){#clx-panel.clx-pagemode{grid-template-columns:minmax(0,1fr)}.clx-pagemode .clx-side{display:none}\
.clx-pagemode .clx-side.show{display:flex;position:absolute;inset:0;z-index:6}.clx-pagemode .clx-histbtn{display:inline-flex}\
.clx-pagemode .clx-h{padding:10px 14px}.clx-pagemode .clx-msgs{padding:14px}.clx-pagemode .clx-f{padding:8px 14px 14px}}\
@media (max-width:640px){#clx-panel:not(.clx-pagemode){width:100%}.clx-grip,.clx-size{display:none}#clx-fab{right:14px;bottom:14px}}\
@media print{#clx-fab,#clx-panel{display:none!important}}\
@media (prefers-reduced-motion:reduce){#clx-panel{transition:none}}';

  var FORM = '<div class="clx-f"><div id="clx-off" hidden></div><div class="clx-pend" id="clx-pend"></div>' +
    '<form id="clx-form"><button type="button" class="clx-clip" id="clx-clip" aria-label="Attach files" title="Attach files: PDF, photos, Word, Excel, CSV (up to 20 MB each, 5 per message)">📎</button>' +
    '<input id="clx-file" type="file" multiple hidden accept="' + ACCEPT + '">' +
    '<textarea id="clx-in" rows="2" placeholder="Ask or tell Claude… or attach drawings, bid forms, photos" aria-label="Message to Claude"></textarea>' +
    '<button class="clx-btn go" id="clx-send" type="submit">Send</button></form>' +
    '<div class="clx-foot"><span>Changes always wait for your approval</span><span>📎 PDF, photos, Word, Excel · 20 MB each</span></div></div>';
  var DROP = '<div class="clx-drop" id="clx-drop" hidden>Drop files to attach<small>PDF, photos, Word, Excel, CSV · up to 20 MB each</small></div>';
  var HIST = '<div class="clx-histp" id="clx-histp"' + (PAGE ? '' : ' hidden') + '><div class="clx-hbar"><input id="clx-q" type="search" placeholder="Search your conversations" aria-label="Search your conversations"><span class="clx-seg"><button type="button" data-saved="0" aria-pressed="true">All</button><button type="button" data-saved="1" aria-pressed="false">★ Saved</button></span></div><div id="clx-hlist" class="clx-hlist"></div></div>';

  function buildPage() {
    var p = document.createElement('div');
    p.id = 'clx-panel'; p.className = 'clx-pagemode';
    p.innerHTML =
      '<div class="clx-side" id="clx-side"><button class="clx-btn go" type="button" id="clx-new">+ New conversation</button>' + HIST + '</div>' +
      '<div class="clx-mainc" id="clx-mainc"><div class="clx-h"><div class="clx-t"><b><span class="clx-star" aria-hidden="true">✦</span>Claude</b>' +
      '<span class="clx-tools"><button class="clx-btn clx-histbtn" type="button" id="clx-hist" aria-pressed="false">Conversations</button>' +
      '<button class="clx-btn" type="button" id="clx-small" title="Keep this conversation in the small Ask Claude window while you work on another page">Open in small window ↘</button></span></div>' +
      '<div class="clx-title" id="clx-title" hidden></div></div>' +
      '<div class="clx-msgs" id="clx-msgs" aria-live="polite"></div>' + FORM + DROP + '</div>' +
      '<aside class="clx-files" aria-label="Files in this conversation"><h3>Files in this conversation</h3><div class="clx-flist" id="clx-flist"></div>' +
      '<div class="clx-fnote"><b>Where files go.</b> Files stay with this conversation and only you can open them. Claude saves one to a bid only when you approve it.</div>' +
      '<div class="clx-fnote"><b>Same conversations everywhere.</b> This page and the small Ask Claude window share your history.</div></aside>';
    HOST.appendChild(p);
    $('clx-small').addEventListener('click', toSmallWindow);
    wireCommon($('clx-mainc'));
  }
  function toSmallWindow() {   // continue this conversation in the window on the page the person came from
    if (S.chat) store('chat', S.chat.chat.id);
    store('open', '1');
    var back = '';
    try { var r = new URL(document.referrer); if (r.origin === location.origin && r.pathname !== '/claude') back = r.pathname + r.search + r.hash; } catch (e) {}
    location.href = back || ((S.status && (S.status.role === 'pm' || S.status.role === 'super')) ? '/projects' : '/company#overview');
  }
  function fullHref() { return '/claude' + (S.chat ? '#chat-' + S.chat.chat.id : ''); }

  function wireCommon(dropZone) {
    $('clx-new').addEventListener('click', function () { clearTimeout(S.poll); S.chat = null; store('chat', null); showHistory(false); renderTitle(); render(); if (PAGE) history.replaceState(null, '', '/claude'); $('clx-in').focus(); });
    $('clx-hist').addEventListener('click', function () { showHistory(!S.hist); });
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
    $('clx-clip').addEventListener('click', function () { $('clx-file').click(); });
    $('clx-file').addEventListener('change', function (e) { addFiles(e.target.files); e.target.value = ''; });
    $('clx-pend').addEventListener('click', function (e) { var b = e.target.closest('[data-rmfile]'); if (b) removeFile(+b.getAttribute('data-rmfile')); });
    $('clx-in').addEventListener('paste', function (e) {   // a screenshot pasted into the box is attached
      var fs = Array.prototype.filter.call((e.clipboardData && e.clipboardData.files) || [], function (f) { return /^image\//.test(f.type); });
      if (fs.length) { e.preventDefault(); addFiles(fs.map(function (f, i) { return f.name && f.name !== 'image.png' ? f : new File([f], 'Pasted picture ' + (i + 1) + '.png', { type: f.type }); })); }
    });
    var depth = 0, hasFiles = function (e) { return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0; };
    dropZone.addEventListener('dragenter', function (e) { if (!hasFiles(e)) return; e.preventDefault(); depth++; $('clx-drop').hidden = false; });
    dropZone.addEventListener('dragover', function (e) { if (hasFiles(e)) e.preventDefault(); });
    dropZone.addEventListener('dragleave', function () { if (--depth <= 0) { depth = 0; $('clx-drop').hidden = true; } });
    dropZone.addEventListener('drop', function (e) { if (!hasFiles(e)) return; e.preventDefault(); depth = 0; $('clx-drop').hidden = true; addFiles(e.dataTransfer.files); });
  }

  // ---- files: upload as soon as they are picked, sent with the next message
  function fileKind(n) {
    n = String(n || '').toLowerCase();
    return /\.pdf$/.test(n) ? ['pdf', 'PDF'] : /\.(png|jpe?g|heic|heif|webp|gif)$/.test(n) ? ['image', 'IMG']
      : /\.(xlsx|xlsm)$/.test(n) ? ['excel', 'XLS'] : /\.csv$/.test(n) ? ['excel', 'CSV'] : /\.docx$/.test(n) ? ['word', 'DOC'] : ['text', 'TXT'];
  }
  function sizeOf(b) { return b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB'; }
  function chip(f, opts) {
    opts = opts || {};
    var k = fileKind(f.name), link = opts.link && f.url, tag = link ? 'a' : 'span';
    var sub = f.err || (f.busy ? 'Attaching…' : (f.label || sizeOf(f.size)));
    return '<' + tag + ' class="clx-att' + (f.err ? ' err' : '') + (f.busy ? ' busy' : '') + '"' +
      (link ? ' href="' + esc(f.url) + '" target="_blank" rel="noopener"' : '') + ' title="' + esc(f.name + (f.err ? ': ' + f.err : '')) + '">' +
      '<span class="ty ' + k[0] + '" aria-hidden="true">' + k[1] + '</span><span class="nm"><b>' + esc(f.name) + '</b><small>' + esc(sub) + '</small></span>' +
      (opts.rm ? '<button type="button" class="rm" data-rmfile="' + f.key + '" aria-label="Remove ' + esc(f.name) + '">×</button>' : '') + '</' + tag + '>';
  }
  function busyFiles() { return S.files.some(function (f) { return f.busy; }); }
  function drawPend() {
    var b = $('clx-pend'); if (!b) return;
    b.innerHTML = S.files.map(function (f) { return chip(f, { rm: true }); }).join('');
    syncSend();
  }
  function syncSend() {
    var v = S.chat, off = S.status && !S.status.available, s = $('clx-send');
    if (s) s.disabled = !!off || !!(v && v.busy) || S.sending || busyFiles();
  }
  function addFiles(list) {
    Array.prototype.forEach.call(list || [], function (file) {
      var f = { key: ++S.fk, name: file.name || 'file', size: file.size || 0 };
      S.files.push(f);
      if (S.files.filter(function (x) { return !x.err; }).length > MAXF) { f.err = 'Up to 5 files per message'; return; }
      if (f.size > MAXB) { f.err = 'Over 20 MB. Attach the pages you need.'; return; }
      f.busy = true;
      var fd = new FormData(); fd.append('file', file, f.name);
      fetch('/api/claude/files', { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { if (!r.ok) throw new Error((j && j.error) || ('Could not attach (' + r.status + ')')); return j.data; }); })
        .then(function (d) { f.busy = false; f.id = d.id; f.label = d.label; f.url = d.url; drawPend(); })
        .catch(function (e) { f.busy = false; f.err = e.message; drawPend(); });
    });
    drawPend();
  }
  function removeFile(key) {
    var f = S.files.filter(function (x) { return x.key === key; })[0];
    S.files = S.files.filter(function (x) { return x.key !== key; });
    if (f && f.id) api('/files/' + f.id, { method: 'DELETE' }).catch(function () {});
    drawPend();
  }

  function build() {
    var st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);
    if (PAGE) { buildPage(); return; }
    var fab = document.createElement('button');
    fab.id = 'clx-fab'; fab.type = 'button'; fab.setAttribute('aria-controls', 'clx-panel'); fab.setAttribute('aria-expanded', 'false');
    fab.innerHTML = '<span class="clx-star" aria-hidden="true">✦</span>Ask Claude';
    document.body.appendChild(fab);
    var p = document.createElement('aside');
    p.id = 'clx-panel'; p.hidden = true; p.setAttribute('aria-label', 'Claude');
    p.innerHTML =
      '<div class="clx-h"><div class="clx-t"><b><span class="clx-star" aria-hidden="true">✦</span>Claude</b>' +
      '<span class="clx-tools"><button class="clx-btn" type="button" id="clx-hist" aria-pressed="false">History</button><button class="clx-btn" type="button" id="clx-new">+ New</button><a class="clx-btn" id="clx-full" href="/claude" title="Open this conversation on the full Claude page">↗ Full page</a><button class="clx-btn clx-size" type="button" id="clx-size" aria-label="Make the Claude window bigger" title="Bigger">⤢</button><button class="clx-x" type="button" id="clx-close" aria-label="Close Claude">×</button></span></div>' +
      '<div class="clx-title" id="clx-title" hidden></div></div>' +
      HIST +
      '<div class="clx-msgs" id="clx-msgs" aria-live="polite"></div>' +
      FORM + DROP +
      '<div class="clx-grip" id="clx-grip" role="separator" aria-orientation="vertical" tabindex="0" aria-label="Resize the Claude window (drag, or use the arrow keys)"></div>';
    document.body.appendChild(p);
    fab.addEventListener('click', function () { setOpen(!S.open); });
    $('clx-close').addEventListener('click', function () { setOpen(false); });
    $('clx-full').addEventListener('click', function () { this.href = fullHref(); });
    $('clx-size').addEventListener('click', function () { setWidth(curWidth() < WIDE() - 20 ? WIDE() : NARROW, true); });
    wireGrip();
    wireCommon(p);
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
    if (PAGE) { $('clx-side').classList.toggle('show', on); loadHistory(); return; }
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
      if (PAGE) box.querySelectorAll('.clx-hrm').forEach(function (b) { b.textContent = '×'; b.title = 'Remove from the list'; });
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
    var fresh = !S.chat || S.chat.chat.id !== view.chat.id || S.chat.chat.title !== view.chat.title;
    S.chat = view; store('chat', view.chat.id);
    renderTitle(); render();
    if (PAGE) { if (location.hash !== '#chat-' + view.chat.id) history.replaceState(null, '', '#chat-' + view.chat.id); if (fresh || !view.busy) loadHistory(); }
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
    syncSend(); $('clx-in').disabled = !!off;
    if (PAGE) $('clx-flist').innerHTML = v && v.files && v.files.length ? v.files.map(function (f) { return chip(f, { link: true }); }).join('')
      : '<div class="clx-intro">Files you attach in this conversation show here.</div>';
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
      if (m.role === 'user') {
        var fl = m.files || [], txt = fl.length && m.text === DEFAULT_TEXT ? '' : m.text;
        return '<div class="clx-m u"><span class="who">You · ' + esc(when(m.created_at)) + '</span>' +
          (fl.length ? '<div class="clx-atts">' + fl.map(function (f) { return chip(f, { link: true }); }).join('') + '</div>' : '') +
          (txt ? '<div class="clx-b">' + esc(txt) + '</div>' : '') + '</div>';
      }
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
    var ready = S.files.filter(function (f) { return f.id && !f.err; });
    if ((!text && !ready.length) || S.sending || (S.chat && S.chat.busy) || busyFiles()) return;
    S.sending = true; $('clx-send').disabled = true;
    try {
      if (!S.chat) { var c = await api('/chats', { method: 'POST', body: JSON.stringify({ surface: SURFACE }) }); S.chat = c.data; }
      $('clx-in').value = '';
      var r = await api('/chats/' + S.chat.chat.id + '/messages', { method: 'POST', body: JSON.stringify({ text: text, page: page(),
        file_ids: ready.map(function (f) { return f.id; }) }) });
      S.files = []; drawPend();
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
    catch (e) {   // not offered to this role (or signed out): no button
      if (PAGE) HOST.innerHTML = '<div class="clx-intro" style="padding:24px">Claude is not available for your role.</div>';
      return;
    }
    build();
    if (PAGE) {
      S.loaded = true; store('open', null);
      var m = /^#chat-(\d+)$/.exec(location.hash), keep = m ? +m[1] : +load('chat');
      if (keep) await openChat(keep); else { renderTitle(); render(); }
      loadHistory(); $('clx-in').focus();
      return;
    }
    sizeFromStore();
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
  window.SSC_CLAUDE = { open: function () { if (PAGE) $('clx-in').focus(); else setOpen(true); }, mountCards: mountCards };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
