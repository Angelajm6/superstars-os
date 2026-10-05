/* Job dashboard — v2 preview (job_v2.py).
 *
 * Re-presents the existing screens; it does NOT replace them. Every tab / pill simply clicks the
 * original sidebar link (which stays in the page, hidden), so every loader, role check and
 * switched-off-module rule keeps working exactly as before. Labels come from those links —
 * nothing is renamed. The Overview gains an attention list and quick links built from the same
 * data the page already fetched (window.JX_render's payload).
 */
(function () {
  'use strict';

  // Five groups over the existing nav views. Every nav view must appear in exactly one group
  // (or be "settings") — tests/smoke_job_v2_preview.py enforces it.
  var TABS = [
    { key: 'overview', label: 'Overview', views: ['dashboard'] },
    { key: 'work', label: 'Work', views: ['schedule', 'drop-report', 'drop-plans', 'daily-report', 'weekly-summary', 'drawings', 'materials', 'toolbox-talks', 'rfis'] },
    { key: 'documents', label: 'Documents', views: ['documents', 'field-photos', 'templates', 'violations', 'compliance'] },
    { key: 'money', label: 'Money', views: ['sov', 'payments', 'expenses', 'product-usage'] },
    { key: 'team', label: 'Team', views: ['contacts', 'employees'] }
  ];
  var QUICK = ['daily-report', 'field-photos', 'payments', 'contacts'];   // quick links on the Overview
  var last = {};                                                            // last view used per tab

  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function link(view) { return $('.sidebar .nav-link[data-view="' + view + '"]'); }
  function visible(view) { var l = link(view); return !!l && !l.hidden; }
  function label(view) { var l = link(view); return l ? l.textContent.replace(/\s+/g, ' ').trim() : view; }
  function text(sel) { var e = $(sel); return e ? e.textContent.replace(/ /g, ' ').trim() : ''; }
  function visibleViews(tab) { return tab.views.filter(visible); }
  function tabOf(view) { for (var i = 0; i < TABS.length; i++) if (TABS[i].views.indexOf(view) !== -1) return TABS[i]; return null; }
  function activeView() { var a = $('.sidebar .nav-link.active'); return a ? a.getAttribute('data-view') : 'dashboard'; }
  function go(view) { var l = link(view); if (l && !l.hidden) { l.click(); window.scrollTo(0, 0); } }

  var ICON_GEAR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';
  var ICON_USER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>';

  // ---------------------------------------------------------------- top bar
  function build() {
    var side = $('.sidebar'), main = $('.main');
    if (!side || !main || $('#jv2-top')) return;
    var back = $('.sidebar .bl-projects');
    var name = text('[data-project-name]') || text('[data-project-code]');
    var code = text('[data-project-code]');
    var client = text('[data-project-client]');
    var addr = text('[data-project-address]');

    var top = document.createElement('div');
    top.id = 'jv2-top';
    top.innerHTML =
      '<div class="jv2-row">' +
        '<a class="jv2-back" href="' + esc(back ? back.getAttribute('href') : '/company#projects') + '" aria-label="All projects"><span aria-hidden="true">←</span><span class="jv2-back-t">' + (back && /Back to Projects/i.test(back.textContent) ? 'Back to Projects' : 'All projects') + '</span></a>' +
        '<div class="jv2-title"><div class="jv2-name">' + esc(name) + (code && name !== code ? ' · ' + esc(code) : '') + '</div>' +
          '<div class="jv2-sub">' + esc([client, addr].filter(Boolean).join(' · ')) + '</div></div>' +
        '<div class="jv2-chips" id="jv2-chips"></div>' +
        '<button type="button" class="jv2-iconbtn" id="jv2-settings" aria-label="Settings" title="Settings">' + ICON_GEAR + '</button>' +
        '<div class="jv2-acct"><button type="button" class="jv2-iconbtn" id="jv2-acct-btn" aria-haspopup="true" aria-expanded="false" aria-label="Account">' + ICON_USER + '</button>' +
          '<div class="jv2-menu" id="jv2-menu" role="menu" hidden><div class="jv2-menu-who" id="jv2-who">Account</div>' +
          '<button type="button" role="menuitem" id="jv2-m-settings">Settings</button><button type="button" role="menuitem" id="jv2-m-out">Sign out</button></div></div>' +
      '</div>' +
      '<div class="jv2-tabs" id="jv2-tabs" role="tablist"></div>' +
      '<div class="jv2-pills" id="jv2-pills" hidden></div>';
    main.insertBefore(top, main.firstChild);

    // the status chips the Overview already renders live in the bar now (same element, same id)
    var chips = $('#jx-chips');
    if (chips) $('#jv2-chips').appendChild(chips);

    $('#jv2-settings').addEventListener('click', function () { go('settings'); });
    var btn = $('#jv2-acct-btn'), menu = $('#jv2-menu');
    function closeMenu() { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); }
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var n = text('#acct-name'), r = text('#acct-role');
      $('#jv2-who').innerHTML = esc(n && n !== 'Account' ? n : 'Account') + (r && r !== '—' ? '<small>' + esc(r) + '</small>' : '');
      menu.hidden = !menu.hidden; btn.setAttribute('aria-expanded', menu.hidden ? 'false' : 'true');
    });
    document.addEventListener('click', closeMenu);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeMenu(); });
    $('#jv2-m-settings').addEventListener('click', function () { closeMenu(); go('settings'); });
    $('#jv2-m-out').addEventListener('click', function () { closeMenu(); var o = $('#acct-signout'); if (o) o.click(); else fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' }).then(function () { location.href = '/login'; }); });
  }

  // ---------------------------------------------------------------- tabs + pills
  function renderTabs() {
    var host = $('#jv2-tabs'); if (!host) return;
    host.innerHTML = '';
    TABS.forEach(function (t) {
      if (!visibleViews(t).length) return;
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'jv2-tab'; b.setAttribute('role', 'tab'); b.setAttribute('data-tab', t.key); b.textContent = t.label;
      b.addEventListener('click', function () {
        var vs = visibleViews(t);
        var v = last[t.key] && vs.indexOf(last[t.key]) !== -1 ? last[t.key] : vs[0];
        if (v) go(v);
      });
      host.appendChild(b);
    });
  }

  function sync() {
    var view = activeView(), tab = tabOf(view);
    if (tab) last[tab.key] = view;
    $$('#jv2-tabs .jv2-tab').forEach(function (b) { b.classList.toggle('active', !!tab && b.getAttribute('data-tab') === tab.key); b.setAttribute('aria-selected', !!tab && b.getAttribute('data-tab') === tab.key ? 'true' : 'false'); });
    var gear = $('#jv2-settings'); if (gear) gear.classList.toggle('active', view === 'settings');
    var pills = $('#jv2-pills'); if (!pills) return;
    var vs = tab ? visibleViews(tab) : [];
    if (vs.length < 2) { pills.hidden = true; pills.innerHTML = ''; return; }
    pills.hidden = false;
    pills.innerHTML = '';
    vs.forEach(function (v) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'jv2-pill' + (v === view ? ' active' : ''); b.textContent = label(v);
      b.addEventListener('click', function () { go(v); });
      pills.appendChild(b);
    });
    var on = $('.jv2-pill.active', pills); if (on && on.scrollIntoView) on.scrollIntoView({ block: 'nearest', inline: 'center' });
  }

  // ---------------------------------------------------------------- Overview extras
  function attention(S) {
    var host = $('#jv2-attn-list'); if (!host || !S) return;
    var H = S.header || {}, SC = S.schedule, F = S.field_today, R = S.risk, M = S.money;
    var rows = [];
    function add(tone, t, view) { if (visible(view)) rows.push([tone, t, view]); }
    function plural(n, w) { return n + ' ' + w + (n === 1 ? '' : 's'); }
    if (F && !F.dcr_today) add('bad', 'Today’s daily report has not been issued', 'daily-report');
    if (R && R.rfis_overdue) add('bad', plural(R.rfis_overdue, 'RFI') + ' overdue', 'rfis');
    if (SC && SC.behind) add('bad', plural(SC.behind, 'drop') + ' behind target', 'drop-plans');
    if (R && R.violations_active) add('bad', plural(R.violations_active, 'active DOB violation'), 'compliance');
    if (R && R.certs_expired) add('bad', plural(R.certs_expired, 'crew certification') + ' expired', 'employees');
    else if (R && R.certs_expiring_30) add('warn', plural(R.certs_expiring_30, 'crew certification') + ' expiring within 30 days', 'employees');
    if (M && M.overdue) add('warn', 'Overdue receivables need follow-up', 'payments');
    if (M && !M.contract) add('warn', 'No contract value set yet', 'payments');
    if (!H.billing_type) add('warn', 'Billing type is not set', 'settings');
    if (!(S.contacts || []).length) add('warn', 'No contacts added yet', 'contacts');
    host.innerHTML = rows.length
      ? rows.map(function (r) { return '<button type="button" class="jv2-attn-row" data-go="' + esc(r[2]) + '"><span class="jv2-dot ' + r[0] + '"></span><span class="jv2-attn-t">' + esc(r[1]) + '</span><span class="jv2-attn-go">' + esc(label(r[2])) + ' →</span></button>'; }).join('')
      : '<div class="jv2-clear"><span class="jv2-dot ok"></span> All clear. Nothing needs your attention right now.</div>';
  }

  function mountOverview() {
    if ($('#jv2-attn')) return;
    var kpis = $('#jx-kpis'); if (!kpis) return;
    var wrap = document.createElement('div');
    wrap.id = 'jv2-attn'; wrap.className = 'jv2-grid';
    var quick = QUICK.filter(visible).map(function (v) { return '<button type="button" data-go="' + esc(v) + '">' + esc(label(v)) + '</button>'; }).join('');
    wrap.innerHTML =
      '<div class="jv2-panel"><h2>Needs your attention</h2><div id="jv2-attn-list"><div class="jv2-clear">Loading…</div></div></div>' +
      '<div class="jv2-panel"><h2>Quick links</h2><div class="jv2-quick">' + quick + '</div></div>';
    kpis.parentNode.insertBefore(wrap, kpis);
    wrap.addEventListener('click', function (e) { var b = e.target.closest('[data-go]'); if (b) go(b.getAttribute('data-go')); });
  }

  function wrapOverview() {
    if (typeof window.JX_render !== 'function' || window.JX_render.__jv2) return;
    var orig = window.JX_render;
    window.JX_render = function (S) { var r = orig.apply(this, arguments); try { attention(S); } catch (e) { /* the classic cards still render */ } return r; };
    window.JX_render.__jv2 = true;
  }

  // ---------------------------------------------------------------- boot
  function init() {
    build(); renderTabs(); mountOverview(); wrapOverview(); sync();
    var nav = $('.sidebar .nav');
    if (nav && window.MutationObserver) {
      new MutationObserver(function () { sync(); }).observe(nav, { attributes: true, subtree: true, attributeFilter: ['class'] });
    }
    // modules switched on/off from Settings change which links are hidden: re-draw the tabs
    if (nav && window.MutationObserver) {
      new MutationObserver(function () { renderTabs(); sync(); }).observe(nav, { attributes: true, subtree: true, attributeFilter: ['hidden'] });
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
