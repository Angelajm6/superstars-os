/* Project page — the redesigned tabs (job_v2.py; prototype accepted 2026-11-11:
 * https://claude.ai/artifact/VGzVhPeig191nXeUsPybgZ).
 *
 * Re-presents the existing screens; it does NOT replace them. Every section / pill / menu item
 * simply clicks the original sidebar link (which stays in the page, hidden), so every loader,
 * role check and switched-off-module rule keeps working exactly as before. A section with no
 * visible link (Money for a pm / super) is never drawn.
 *
 * Adds: an app bar (back · job · Jump to · + New · account), a project header tinted with the
 * open section's color, six colored sections with attention badges, sub-tab pills, a Jump-to
 * palette (Ctrl/Cmd+K), a colored icon tile on every tab's own title, a "Needs you" list on the
 * Overview, and on phones a bottom section bar + a round + New button.
 */
(function () {
  'use strict';

  // Six sections over the existing nav views. Every nav view must appear in exactly one section
  // (or be "settings") — tests/smoke_job_v2_preview.py enforces it.
  var TABS = [
    { key: 'overview', label: 'Overview', icon: 'home', views: ['dashboard'] },
    { key: 'today', label: 'Today', icon: 'sun', views: ['daily-report', 'field-photos', 'toolbox-talks'] },
    { key: 'work', label: 'Work', icon: 'hammer', views: ['schedule', 'drop-plans', 'drop-report', 'materials', 'weekly-summary'] },
    { key: 'documents', label: 'Documents', icon: 'folder', views: ['documents', 'drawings', 'compliance', 'violations', 'rfis', 'templates'] },
    { key: 'money', label: 'Money', icon: 'dollar', views: ['payments', 'sov', 'expenses', 'product-usage'] },
    { key: 'team', label: 'Team', icon: 'users', views: ['employees', 'contacts'] }
  ];
  // the names people see (sentence case); anything missing falls back to the nav link's own text
  var LABEL = {
    'dashboard': 'Overview', 'daily-report': 'Daily reports', 'field-photos': 'Photos', 'toolbox-talks': 'Toolbox talks',
    'schedule': 'Schedule', 'drop-plans': 'Drop plan', 'drop-report': 'Drop reports', 'materials': 'Materials',
    'weekly-summary': 'Weekly summary', 'documents': 'Documents', 'drawings': 'Drawings', 'compliance': 'Permits & violations',
    'violations': 'City violations', 'rfis': 'Questions (RFIs)', 'templates': 'Signs & forms', 'payments': 'Invoices & payments',
    'sov': 'Billing breakdown', 'expenses': 'Costs', 'product-usage': 'Products used', 'employees': 'Crew', 'contacts': 'Contacts',
    'settings': 'Job settings'
  };
  var ICON = {
    'dashboard': 'home', 'daily-report': 'report', 'field-photos': 'camera', 'toolbox-talks': 'mega', 'schedule': 'cal',
    'drop-plans': 'rope', 'drop-report': 'layers', 'materials': 'box', 'weekly-summary': 'chart', 'documents': 'file',
    'drawings': 'pencil', 'compliance': 'shield', 'violations': 'shield', 'rfis': 'msg', 'templates': 'sign', 'payments': 'invoice',
    'sov': 'list', 'expenses': 'receipt', 'product-usage': 'box', 'employees': 'users', 'contacts': 'phone', 'settings': 'gear'
  };
  var NO_PILL = ['violations'];   // a placeholder pointing to Permits & violations; still reachable from Jump to
  var NEW_ITEMS = [['Daily report', 'daily-report', 'report'], ['Photo', 'field-photos', 'camera'], ['Delivery', 'materials', 'box'],
    ['Question (RFI)', 'rfis', 'msg'], ['Document', 'documents', 'file'], ['Expense', 'expenses', 'receipt'], ['Sign or form', 'templates', 'sign']];
  var last = {};        // last view used per section
  var S = null;         // the Overview payload (window.JX_render's argument), once it arrives

  var P = {
    home: '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    hammer: '<path d="m15 12-8.4 8.4a2 2 0 0 1-2.8-2.8L12.2 9"/><path d="M17.6 15 22 10.6l-6.6-6.6-2 2a3 3 0 0 0-2.8.8L9 8.4l6.6 6.6z"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    dollar: '<path d="M12 2v20M17 6.5C16 5 14.3 4.5 12 4.5c-2.8 0-4.5 1.3-4.5 3.3 0 4.7 9.5 2.4 9.5 7.6 0 2.1-1.9 3.6-5 3.6-2.5 0-4.4-.8-5.4-2.5"/>',
    users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.6a3.5 3.5 0 0 1 0 6.8M18.5 14.5A6.5 6.5 0 0 1 21.5 20"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    back: '<path d="M15 18l-6-6 6-6"/>',
    report: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h3"/>',
    camera: '<path d="M3 8a2 2 0 0 1 2-2h2l1.5-2h7L17 6h2a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><circle cx="12" cy="13" r="3.5"/>',
    mega: '<path d="M3 10v4h3l6 4V6L6 10zM16 8.5a5 5 0 0 1 0 7"/>',
    cal: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    layers: '<path d="m12 3 9 5-9 5-9-5zM3 13l9 5 9-5"/>',
    rope: '<path d="M12 2v14M8 16h8l-1 6H9zM5 2h14"/>',
    box: '<path d="m12 3 8 4v10l-8 4-8-4V7zM4 7l8 4 8-4M12 11v10"/>',
    chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    file: '<path d="M6 2h8l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z"/><path d="M14 2v5h5"/>',
    pencil: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
    shield: '<path d="M12 2 4 5v6c0 5 3.5 9 8 11 4.5-2 8-6 8-11V5z"/><path d="m9 12 2 2 4-4"/>',
    msg: '<path d="M4 4h16v12H8l-4 4z"/><path d="M9 9h6"/>',
    sign: '<rect x="3" y="4" width="18" height="11" rx="1.5"/><path d="M12 15v6M8 21h8"/>',
    list: '<path d="M9 6h12M9 12h12M9 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
    invoice: '<path d="M5 2h14v20l-3-2-2 2-2-2-2 2-2-2-3 2z"/><path d="M9 7h6M9 11h6M9 15h4"/>',
    receipt: '<path d="M4 7h16M4 12h16M4 17h10"/>',
    phone: '<path d="M5 3h4l2 5-2.5 1.5a11 11 0 0 0 6 6L16 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 5a2 2 0 0 1 2-2z"/>',
    pin: '<path d="M12 22s7-6.5 7-12a7 7 0 0 0-14 0c0 5.5 7 12 7 12z"/><circle cx="12" cy="10" r="2.5"/>',
    edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>'
  };
  function ic(name) { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (P[name] || P.file) + '</svg>'; }

  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function link(view) { return $('.sidebar .nav-link[data-view="' + view + '"]'); }
  function visible(view) { var l = link(view); return !!l && !l.hidden; }
  function label(view) { if (LABEL[view]) return LABEL[view]; var l = link(view); return l ? l.textContent.replace(/\s+/g, ' ').trim() : view; }
  function text(sel) { var e = $(sel); return e ? e.textContent.replace(/ /g, ' ').trim() : ''; }
  function visibleViews(tab) { return tab.views.filter(visible); }
  function tabOf(view) { for (var i = 0; i < TABS.length; i++) if (TABS[i].views.indexOf(view) !== -1) return TABS[i]; return null; }
  function activeView() { var a = $('.sidebar .nav-link.active'); return a ? a.getAttribute('data-view') : 'dashboard'; }
  function go(view) { var l = link(view); if (l && !l.hidden) { l.click(); window.scrollTo(0, 0); } }
  function money(n) { n = Number(n) || 0; return n >= 1e6 ? '$' + (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M' : n >= 1e4 ? '$' + Math.round(n / 1e3) + 'k' : '$' + Math.round(n).toLocaleString(); }
  function plural(n, w) { return n + ' ' + w + (n === 1 ? '' : 's'); }
  function sectionColor(key) { return 'var(--jv-c-' + (key || 'overview') + ')'; }
  function fmtDate(iso) {
    var m = String(iso || '').match(/^(\d{4})-(\d{2})-(\d{2})/); if (!m) return iso ? String(iso) : '';
    return ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][+m[2] - 1] + ' ' + (+m[3]);
  }

  // ---------------------------------------------------------------- app bar + header + sections
  function build() {
    var main = $('.main');
    if (!main || $('#jv2-top')) return;
    var back = $('.sidebar .bl-projects');
    var backHref = back ? back.getAttribute('href') : '/company#projects';
    var mac = /Mac|iPhone|iPad/.test(navigator.platform || '');

    var top = document.createElement('div');
    top.id = 'jv2-top';
    top.innerHTML =
      '<div class="jv2-bar"><div class="jv2-in">' +
        '<a class="jv2-back" href="' + esc(backHref) + '" aria-label="All projects">' + ic('back') + '<span class="jv2-back-t">Projects</span></a>' +
        '<span class="jv2-crumb" id="jv2-crumb"></span>' +
        '<button type="button" class="jv2-jump" id="jv2-jump" aria-label="Jump to a page in this job">' + ic('search') + '<span class="jv2-jump-t">Jump to anything in this job</span><kbd>' + (mac ? '⌘K' : 'Ctrl K') + '</kbd></button>' +
        '<div class="jv2-newwrap"><button type="button" class="jv2-new" id="jv2-new" aria-haspopup="true" aria-expanded="false">' + ic('plus') + '<span>New</span></button></div>' +
        '<div class="jv2-acct"><button type="button" class="jv2-avatar" id="jv2-acct-btn" aria-haspopup="true" aria-expanded="false" aria-label="Account">··</button>' +
          '<div class="jv2-menu jv2-acctmenu" id="jv2-menu" role="menu" hidden><div class="jv2-menu-who" id="jv2-who">Account</div>' +
          '<button type="button" role="menuitem" id="jv2-m-settings">' + ic('gear') + 'Job settings</button>' +
          '<a role="menuitem" href="/company">' + ic('home') + 'Company dashboard</a>' +
          '<button type="button" role="menuitem" id="jv2-m-out">' + ic('back') + 'Sign out</button></div></div>' +
      '</div></div>' +
      '<div class="jv2-hero" id="jv2-hero"><div class="jv2-in">' +
        '<div class="jv2-hl"><div class="jv2-hk"><span class="jv2-code" id="jv2-code"></span><span class="jv2-chips" id="jv2-chips"></span></div>' +
          '<h1 class="jv2-name" id="jv2-name"></h1><div class="jv2-sub" id="jv2-sub"></div></div>' +
        '<div class="jv2-stats" id="jv2-stats"></div>' +
      '</div></div>' +
      '<div class="jv2-secbar"><div class="jv2-in"><div class="jv2-tabs" id="jv2-tabs" role="tablist" aria-label="Sections"></div>' +
        '<button type="button" class="jv2-gear" id="jv2-settings" aria-label="Job settings" title="Job settings">' + ic('gear') + '</button></div></div>' +
      '<div class="jv2-pills"><div class="jv2-in" id="jv2-pills" role="tablist" aria-label="Pages in this section"></div></div>';
    main.insertBefore(top, main.firstChild);

    // phone: section bar along the bottom + a round + New button
    var bottom = document.createElement('nav'); bottom.id = 'jv2-bottom'; bottom.setAttribute('aria-label', 'Sections'); document.body.appendChild(bottom);
    var fab = document.createElement('button'); fab.type = 'button'; fab.id = 'jv2-fab'; fab.className = 'jv2-fab';
    fab.setAttribute('aria-label', 'New'); fab.setAttribute('aria-haspopup', 'true'); fab.innerHTML = ic('plus'); document.body.appendChild(fab);

    // the status chips the Overview renders live in the header now (same element, same id)
    var chips = $('#jx-chips');
    if (chips) $('#jv2-chips').appendChild(chips);

    document.addEventListener('click', function (e) {
      var b = e.target.closest('[data-jv2-tab]');
      if (b) { openTab(TABS.filter(function (t) { return t.key === b.getAttribute('data-jv2-tab'); })[0]); return; }
      b = e.target.closest('[data-jv2-go]');
      if (b) { var v = b.getAttribute('data-jv2-go'); closeLayers(); go(v); return; }
      if (!e.target.closest('.jv2-menu, .jv2-pal')) closeLayers();
    });
    $('#jv2-settings').addEventListener('click', function () { go('settings'); });
    $('#jv2-new').addEventListener('click', function (e) { e.stopPropagation(); toggleNew(this, false); });
    fab.addEventListener('click', function (e) { e.stopPropagation(); toggleNew(this, true); });
    $('#jv2-jump').addEventListener('click', function (e) { e.stopPropagation(); openJump(); });
    document.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && (e.key || '').toLowerCase() === 'k') { e.preventDefault(); openJump(); }
      else if (e.key === 'Escape') closeLayers();
    });

    var btn = $('#jv2-acct-btn'), menu = $('#jv2-menu');
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = menu.hidden; closeLayers();
      var n = text('#acct-name'), r = text('#acct-role');
      $('#jv2-who').innerHTML = esc(n && n !== 'Account' ? n : 'Account') + (r && r !== '—' ? '<small>' + esc(r) + '</small>' : '');
      menu.hidden = !open; btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    $('#jv2-m-settings').addEventListener('click', function () { closeLayers(); go('settings'); });
    $('#jv2-m-out').addEventListener('click', function () { closeLayers(); var o = $('#acct-signout'); if (o) o.click(); else fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' }).then(function () { location.href = '/login'; }); });
    syncAccount(); document.addEventListener('ssc:user-loaded', syncAccount);
    renderHero();
  }

  function syncAccount() {
    var b = $('#jv2-acct-btn'); if (!b) return;
    var n = text('#acct-name'), u = window.__SSC_USER || {};
    var name = (n && n !== 'Account') ? n : (u.full_name || u.email || '');
    var ini = name.split(/[\s@.]+/).filter(Boolean).slice(0, 2).map(function (x) { return x[0].toUpperCase(); }).join('');
    b.textContent = ini || '··';
  }

  // ---------------------------------------------------------------- the project header
  function ring(pct) {
    var r = 19, c = 2 * Math.PI * r, p = Math.max(0, Math.min(100, pct || 0));
    return '<svg class="jv2-ring" viewBox="0 0 46 46" aria-hidden="true"><circle cx="23" cy="23" r="' + r + '" fill="none" stroke="var(--jv-fill)" stroke-width="6"/>' +
      '<circle cx="23" cy="23" r="' + r + '" fill="none" stroke="var(--jv-c-work)" stroke-width="6" stroke-linecap="round" stroke-dasharray="' + (c * p / 100).toFixed(1) + ' ' + c.toFixed(1) + '"/></svg>';
  }
  function stat(l, v, s, extra) { return '<div class="jv2-stat">' + (extra || '') + '<div><div class="jv2-stat-l">' + esc(l) + '</div><div class="jv2-stat-v">' + esc(v) + '</div>' + (s ? '<div class="jv2-stat-s">' + esc(s) + '</div>' : '') + '</div></div>'; }
  function renderHero() {
    if (!$('#jv2-hero')) return;
    var H = (S && S.header) || {};
    var code = H.code || text('[data-project-code]');
    var raw = H.name || text('[data-project-name]') || text('[data-project-title]') || code;
    var name = code ? raw.replace(' · ' + code, '') : raw;
    var client = text('.jx-sub [data-project-client]') || text('[data-project-client]');
    var addr = (text('#jx-address') || text('[data-project-address]')).replace(/^[\s·•,-]+/, '');
    $('#jv2-crumb').textContent = code || name;
    $('#jv2-code').textContent = code || '';
    $('#jv2-code').hidden = !code;
    $('#jv2-name').textContent = name || 'Project';
    var sub = [];
    if (client) sub.push('<span>' + ic('users') + esc(client) + '</span>');
    if (addr && addr !== client) sub.push('<span>' + ic('pin') + esc(addr) + '</span>');
    if (H.start_date || H.target_completion) sub.push('<span>' + ic('cal') + esc(fmtDate(H.start_date) || '…') + ' → ' + esc(fmtDate(H.target_completion) || 'no end date') + '</span>');
    var edit = $('#jx-edit');
    if (edit && !edit.hidden) sub.push('<button type="button" class="jv2-edit" id="jv2-edit">' + ic('edit') + 'Edit project</button>');
    $('#jv2-sub').innerHTML = sub.join('');
    var eb = $('#jv2-edit'); if (eb) eb.addEventListener('click', function () { var o = $('#jx-edit'); if (o) o.click(); });

    if (!S) { $('#jv2-stats').innerHTML = ''; return; }
    var out = [], SC = S.schedule, F = S.field_today, M = S.money;
    if (SC && SC.total) { var pc = Math.round(SC.overall_pct || (SC.complete / SC.total * 100) || 0); out.push(stat('Drops done', pc + '%', SC.complete + ' of ' + SC.total, ring(pc))); }
    else out.push(stat('Drops', '—', 'Drop plan not set up'));
    if (H.days_left != null) out.push(stat('Days left', String(H.days_left), H.target_completion ? 'to ' + fmtDate(H.target_completion) : ''));
    if (M) out.push(stat('Billed', money(M.invoiced), M.contract ? 'of ' + money(M.contract) : 'contract not set'));
    else if (F) out.push(stat('On site today', String(F.on_site || 0), 'of ' + (F.assigned || 0) + ' assigned'));
    $('#jv2-stats').innerHTML = out.join('');
  }

  // ---------------------------------------------------------------- attention (badges + the Overview list)
  function needs() {
    var rows = [];
    if (!S) return rows;
    var H = S.header || {}, SC = S.schedule, F = S.field_today, R = S.risk, M = S.money;
    function add(tone, t, s, view, act) { if (visible(view)) rows.push({ tone: tone, t: t, s: s || '', view: view, act: act || 'Open' }); }
    if (R && R.rfis_overdue) add('bad', plural(R.rfis_overdue, 'question') + ' to the engineer or architect overdue', 'The answers are late', 'rfis');
    if (SC && SC.behind) add('bad', plural(SC.behind, 'drop') + ' behind target', (SC.worst_behind || []).map(function (d) { return d.label; }).join(' · '), 'drop-plans');
    if (R && R.violations_active) add('bad', plural(R.violations_active, 'open city violation'), 'DOB / ECB', 'compliance');
    if (R && R.certs_expired) add('bad', plural(R.certs_expired, 'crew card') + ' expired', 'Check before they go up', 'employees');
    else if (R && R.certs_expiring_30) add('warn', plural(R.certs_expiring_30, 'crew card') + ' expiring within 30 days', '', 'employees');
    if (M && M.overdue > 0) add('warn', money(M.overdue) + ' overdue from the client', 'Follow up on the late invoices', 'payments');
    if (F && !F.dcr_today && (F.on_site || 0) > 0) add('warn', 'Today’s daily report is not issued yet', plural(F.on_site, 'worker') + ' signed in', 'daily-report', 'Write it');
    if (R && R.rfis_open && !R.rfis_overdue) add('info', plural(R.rfis_open, 'open question'), 'Waiting on an answer', 'rfis');
    if (M && !M.contract) add('info', 'No contract value yet', 'Billing and costs need a target', 'settings', 'Set it');
    if (!H.billing_type) add('info', 'How this job is billed is not set', '', 'settings', 'Set it');
    if (!(S.contacts || []).some(function (g) { return g.key !== 'team' && (g.people || []).length; })) add('info', 'No owner, architect or engineer contacts yet', '', 'contacts', 'Add');
    return rows;
  }
  function badgeFor(tab) {
    var n = needs().filter(function (r) { return r.tone !== 'info'; });
    if (tab.key === 'overview') return n.length;
    return n.filter(function (r) { return tab.views.indexOf(r.view) !== -1; }).length;
  }
  function pillCount(view) {
    if (!S) return null;
    var R = S.risk || {}, SC = S.schedule || {};
    if (view === 'rfis' && R.rfis_open) return { n: R.rfis_open, hot: !!R.rfis_overdue };
    if (view === 'compliance' && R.violations_active) return { n: R.violations_active, hot: true };
    if (view === 'drop-plans' && SC.behind) return { n: SC.behind, hot: true };
    if (view === 'employees' && (R.certs_expired || R.certs_expiring_30)) return { n: (R.certs_expired || 0) + (R.certs_expiring_30 || 0), hot: !!R.certs_expired };
    return null;
  }
  function renderAttention() {
    var host = $('#jv2-attn-list'); if (!host) return;
    if (!S) { host.innerHTML = '<div class="jv2-clear">Loading…</div>'; return; }
    var rows = needs(), n = rows.filter(function (r) { return r.tone !== 'info'; }).length;
    var cnt = $('#jv2-attn-n'); if (cnt) { cnt.textContent = n; cnt.hidden = !n; }
    host.innerHTML = rows.length
      ? rows.map(function (r) { return '<div class="jv2-att jv2-' + r.tone + '"><span class="jv2-dot"></span><div class="jv2-att-m"><b>' + esc(r.t) + '</b>' + (r.s ? '<span>' + esc(r.s) + '</span>' : '') + '</div><button type="button" class="jv2-att-go" data-jv2-go="' + esc(r.view) + '">' + esc(r.act) + '</button></div>'; }).join('')
      : '<div class="jv2-clear jv2-ok"><span class="jv2-dot"></span>All clear. Nothing needs you on this job right now.</div>';
  }
  function mountOverview() {
    if ($('#jv2-attn')) return;
    var kpis = $('#jx-kpis'); if (!kpis) return;
    var wrap = document.createElement('div');
    wrap.id = 'jv2-attn'; wrap.className = 'jv2-attn';
    wrap.innerHTML = '<div class="jv2-attn-h"><h2>Needs you</h2><span class="jv2-count" id="jv2-attn-n" hidden></span></div><div id="jv2-attn-list"></div>';
    kpis.parentNode.insertBefore(wrap, kpis.nextSibling);
    renderAttention();
  }
  function wrapOverview() {
    if (typeof window.JX_render !== 'function' || window.JX_render.__jv2) return;
    var orig = window.JX_render;
    window.JX_render = function (data) {
      var r = orig.apply(this, arguments);
      try { S = data || null; renderHero(); renderAttention(); renderTabs(); sync(); } catch (e) { /* the classic cards still render */ }
      return r;
    };
    window.JX_render.__jv2 = true;
  }

  // ---------------------------------------------------------------- each tab's own title: icon tile + the new name
  function dressViews() {
    $$('.view[data-view]').forEach(function (sec) {
      var v = sec.getAttribute('data-view');
      if (v === 'dashboard' || v === 'drawings' || sec.__jv2) return;
      var h = $$('h1, h2', sec).filter(function (e) { return !e.closest('[class*="modal"], [class*="overlay"], [role="dialog"]'); })[0];
      if (!h) return;
      sec.__jv2 = true;
      var t = tabOf(v), box = h.parentElement;
      h.classList.add('jv2-vt');
      h.textContent = label(v);
      if (box && box !== sec) {
        box.classList.add('jv2-vtitle');
        var tile = document.createElement('span'); tile.className = 'jv2-vtile'; tile.innerHTML = ic(ICON[v]);
        tile.style.setProperty('--tile', sectionColor(t ? t.key : 'overview'));
        box.insertBefore(tile, box.firstChild);
        if (box.parentElement && box.parentElement !== sec) box.parentElement.classList.add('jv2-vhead');
      }
    });
  }

  // ---------------------------------------------------------------- sections + pills
  function renderTabs() {
    var host = $('#jv2-tabs'), bar = $('#jv2-bottom'); if (!host) return;
    var cur = tabOf(activeView());
    var ts = TABS.filter(function (t) { return visibleViews(t).length; });
    host.innerHTML = ts.map(function (t) {
      var b = badgeFor(t);
      return '<button type="button" class="jv2-tab" role="tab" data-jv2-tab="' + t.key + '" aria-selected="' + (!!cur && cur.key === t.key) + '" style="--c:' + sectionColor(t.key) + '">' +
        '<span class="jv2-tile">' + ic(t.icon) + '</span><span>' + esc(t.label) + '</span>' + (b ? '<span class="jv2-badge">' + b + '</span>' : '') + '</button>';
    }).join('');
    if (bar) {
      bar.style.setProperty('--jv2-n', ts.length);
      bar.innerHTML = ts.map(function (t) {
        return '<button type="button" role="tab" data-jv2-tab="' + t.key + '" aria-selected="' + (!!cur && cur.key === t.key) + '" style="--c:' + sectionColor(t.key) + '">' +
          '<span class="jv2-btile">' + ic(t.icon) + '</span><span>' + esc(t.key === 'documents' ? 'Docs' : t.label) + '</span>' + (badgeFor(t) ? '<span class="jv2-bdot"></span>' : '') + '</button>';
      }).join('');
    }
  }
  function openTab(t) {
    if (!t) return;
    var vs = visibleViews(t);
    var v = last[t.key] && vs.indexOf(last[t.key]) !== -1 ? last[t.key] : vs[0];
    if (v) go(v);
  }
  function sync() {
    var view = activeView(), tab = tabOf(view);
    if (tab) last[tab.key] = view;
    document.body.style.setProperty('--sec', sectionColor(tab ? tab.key : 'overview'));
    document.body.setAttribute('data-jv2-sec', tab ? tab.key : 'settings');
    $$('#jv2-tabs .jv2-tab, #jv2-bottom [data-jv2-tab]').forEach(function (b) { b.setAttribute('aria-selected', !!tab && b.getAttribute('data-jv2-tab') === tab.key ? 'true' : 'false'); });
    var gear = $('#jv2-settings'); if (gear) { gear.classList.toggle('active', view === 'settings'); gear.hidden = !visible('settings'); }
    var pills = $('#jv2-pills'); if (!pills) return;
    var vs = tab ? visibleViews(tab).filter(function (v) { return NO_PILL.indexOf(v) === -1 || v === view; }) : ['dashboard', 'settings'];
    pills.parentElement.hidden = vs.length < 2;
    pills.innerHTML = vs.map(function (v) {
      var c = pillCount(v), lbl = (!tab && v === 'dashboard') ? '← Overview' : label(v);
      return '<button type="button" class="jv2-pill" role="tab" data-jv2-go="' + v + '" aria-selected="' + (v === view) + '">' + esc(lbl) +
        (c ? '<span class="jv2-pn' + (c.hot ? ' hot' : '') + '">' + c.n + '</span>' : '') + '</button>';
    }).join('');
    var on = $('.jv2-pill[aria-selected="true"]', pills);
    if (on && pills.scrollWidth > pills.clientWidth) pills.scrollLeft = Math.max(0, on.offsetLeft - pills.clientWidth / 2 + on.offsetWidth / 2);
    dressViews();
  }

  // ---------------------------------------------------------------- + New, Jump to
  function closeLayers() {
    $$('.jv2-layer').forEach(function (x) { x.remove(); });
    var m = $('#jv2-menu'); if (m) m.hidden = true;
    $$('#jv2-new, #jv2-fab, #jv2-acct-btn').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
  }
  function toggleNew(anchor, phone) {
    var was = !!$('.jv2-newmenu'); closeLayers(); if (was) return;
    var items = NEW_ITEMS.filter(function (i) { return visible(i[1]); });
    if (!items.length) return;
    var m = document.createElement('div');
    m.className = 'jv2-menu jv2-newmenu jv2-layer' + (phone ? ' jv2-up' : ''); m.setAttribute('role', 'menu');
    m.innerHTML = items.map(function (i) { var t = tabOf(i[1]); return '<button type="button" role="menuitem" data-jv2-go="' + i[1] + '"><span class="jv2-mtile" style="background:' + sectionColor(t && t.key) + '">' + ic(i[2]) + '</span>' + esc(i[0]) + '</button>'; }).join('');
    (phone ? document.body : anchor.parentNode).appendChild(m);
    anchor.setAttribute('aria-expanded', 'true');
  }
  function openJump() {
    closeLayers();
    var sc = document.createElement('div'); sc.className = 'jv2-scrim jv2-layer';
    sc.innerHTML = '<div class="jv2-pal" role="dialog" aria-label="Jump to"><input id="jv2-q" type="text" placeholder="Type a page: photos, invoices, permits…" autocomplete="off" aria-label="Page name"><div class="jv2-plist" id="jv2-plist" role="listbox"></div></div>';
    document.body.appendChild(sc);
    var q = $('#jv2-q'), list = $('#jv2-plist'), sel = 0, items = [];
    function draw() {
      var s = q.value.trim().toLowerCase(); items = [];
      TABS.forEach(function (t) { visibleViews(t).forEach(function (v) { if (!s || label(v).toLowerCase().indexOf(s) !== -1 || t.label.toLowerCase().indexOf(s) !== -1) items.push([v, t]); }); });
      if (visible('settings') && (!s || 'job settings'.indexOf(s) !== -1)) items.push(['settings', { key: 'overview', label: 'Settings' }]);
      sel = Math.min(sel, Math.max(0, items.length - 1));
      list.innerHTML = items.length ? items.map(function (it, i) {
        return '<button type="button" role="option" class="jv2-pit' + (i === sel ? ' on' : '') + '" aria-selected="' + (i === sel) + '" data-jv2-go="' + it[0] + '"><span class="jv2-mtile" style="background:' + sectionColor(it[1].key) + '">' + ic(ICON[it[0]]) + '</span><span>' + esc(label(it[0])) + '</span><small>' + esc(it[1].label) + '</small></button>';
      }).join('') : '<div class="jv2-clear">No page called that. Try “photos” or “permits”.</div>';
      var on = $('.jv2-pit.on', list); if (on) on.scrollIntoView({ block: 'nearest' });
    }
    q.addEventListener('input', function () { sel = 0; draw(); });
    q.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { sel = Math.min(sel + 1, items.length - 1); draw(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { sel = Math.max(sel - 1, 0); draw(); e.preventDefault(); }
      else if (e.key === 'Enter' && items[sel]) { var v = items[sel][0]; closeLayers(); go(v); }
    });
    sc.addEventListener('click', function (e) { if (e.target === sc) { e.stopPropagation(); closeLayers(); } });
    draw(); q.focus();
  }

  // ---------------------------------------------------------------- landing
  var landed = false;
  function landByRole() {
    if (landed) return;
    var u = window.__SSC_USER; if (!u || !u.role) return;
    landed = true;
    if (u.role === 'super' && activeView() === 'dashboard' && !location.hash) go('daily-report');
  }

  // ---------------------------------------------------------------- boot
  function init() {
    landByRole(); document.addEventListener('ssc:user-loaded', landByRole);
    build(); mountOverview(); wrapOverview(); renderTabs(); sync();
    var nav = $('.sidebar .nav');
    if (nav && window.MutationObserver) {
      new MutationObserver(function () { sync(); }).observe(nav, { attributes: true, subtree: true, attributeFilter: ['class'] });
      // modules switched on/off from Settings change which links are hidden: re-draw the sections
      new MutationObserver(function () { renderTabs(); sync(); }).observe(nav, { attributes: true, subtree: true, attributeFilter: ['hidden'] });
    }
    // the job's name / client / address arrive after load: keep the header in step
    var idb = $('[data-project-title]');
    if (idb && window.MutationObserver) new MutationObserver(function () { renderHero(); }).observe(idb, { childList: true, characterData: true, subtree: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
