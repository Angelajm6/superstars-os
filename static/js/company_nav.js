/* #339 — ONE company sidebar.
 *
 * Every company-level page (the portal at /company, the console panels at /#…, the
 * admin pages) renders the SAME seven tabs from this file, so the navigation can never
 * drift between pages again. A tab is a destination; a page shows its tab's sub-tabs
 * in a row under the header (`<div class="ssc-subtabs" data-subtabs="finance">`).
 *
 * Also owns the redirects that retire the old homes: the old console dashboard
 * (`/`, `/#projects`) and CRM (`/#crm`) forward to the portal; the portal's job-sites
 * hashes forward to Projects. Include SYNC in <head> (the redirect must run before
 * anything paints), then `SSC_NAV.mount()` right after the empty <nav data-company-nav>.
 *
 * Roles: the sidebar shows what the role can open (server gates stay the control).
 * admin / c_suite see all seven; an estimator sees Proposals (their queue). */
(function () {
  'use strict';
  var COMPANY = ['admin', 'c_suite'];
  var I = {
    dashboard: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    projects: '<path d="M3 21V7l7-4v18M10 21h11V11l-4-2v12"/><path d="M6 9h1M6 13h1M6 17h1M14 13h1M14 17h1"/>',
    clients: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
    proposals: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>',
    finance: '<path d="M3 17l5-5 4 4 8-8"/><path d="M14 8h6v6"/>',
    workforce: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><circle cx="17" cy="9" r="2.5"/><path d="M15 14.5a5 5 0 0 1 6.5 5"/>',
    company: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z"/><path d="M9 12l2 2 4-4"/>'
  };

  // A location is (path, first hash segment). A tab lights up for any of its `at` pairs;
  // a sub-tab for its exact pair. `null` hash = any hash on that path.
  var TABS = [
    { key: 'dashboard', label: 'Dashboard', href: '/company#overview', roles: COMPANY,
      at: [['/company', 'overview'], ['/company', '']] },
    { key: 'projects', label: 'Projects', href: '/company#projects', roles: COMPANY,
      at: [['/company', 'projects'], ['/company', 'calendar'], ['/company', 'buildings'], ['/admin/projects', null]],
      sub: [['All projects', '/company#projects'], ['Calendar', '/company#calendar'], ['Assignments & onboarding', '/admin/projects']] },
    { key: 'clients', label: 'Clients', href: '/company#clients', roles: COMPANY,
      at: [['/company', 'clients']] },
    { key: 'proposals', label: 'Proposals', href: '/company#proposals', roles: COMPANY.concat(['estimator']),
      hrefFor: { estimator: '/estimating' },
      at: [['/company', 'proposals'], ['/', 'estimates'], ['/estimating', null]],
      sub: [['Sent & signed', '/company#proposals', COMPANY], ['Pipeline', '/#estimates', COMPANY], ['Estimating queue', '/estimating', ['estimator']]] },
    { key: 'finance', label: 'Finance', href: '/company#finance', roles: COMPANY,
      at: [['/company', 'finance'], ['/company', 'expenses'], ['/', 'labor-rate'], ['/admin/labor-rates', null]],
      sub: [['What needs to get paid', '/company#finance'], ['Expenses', '/company#expenses'], ['Weekly payroll', '/#labor-rate'], ['Pay rates', '/admin/labor-rates']] },
    { key: 'workforce', label: 'Workforce', href: '/company#workforce', roles: COMPANY,
      at: [['/company', 'workforce'], ['/', 'workforce'], ['/', 'hours'], ['/', 'cert-library'], ['/', 'cert-health']],
      sub: [['Workers', '/company#workforce'], ['Onboarding & directory', '/#workforce'], ['Certifications', '/#cert-library'], ['Sign-ins', '/#hours']] },
    { key: 'company', label: 'Company', href: '/company#company', roles: COMPANY,
      at: [['/company', 'company'], ['/company', 'compliance'], ['/', 'compliance'], ['/', 'specs'], ['/', 'settings'], ['/admin/users', null]],
      sub: [['Documents & vehicles', '/company#company'], ['Compliance documents', '/#compliance'], ['Specs', '/#specs'], ['Team & access', '/admin/users'], ['Settings', '/#settings']] }
  ];

  // ---- the old homes forward here (runs at include time, before paint) ----
  var REDIRECTS = {
    '/': { '': '/company#overview', 'projects': '/company#overview', 'crm': '/company#clients' },
    '/company': { 'buildings': '/company#projects', 'compliance': '/company#company' }
  };
  function here() {
    var h = (location.hash || '').replace(/^#/, '');
    return { path: location.pathname.replace(/\/+$/, '') || '/', hash: h.split('/')[0], full: h };
  }
  function redirectIfRetired() {
    var w = here(), map = REDIRECTS[w.path];
    if (map && Object.prototype.hasOwnProperty.call(map, w.hash)) { location.replace(map[w.hash]); return true; }
    return false;
  }
  redirectIfRetired();

  function matches(pair, w) { return pair[0] === w.path && (pair[1] === null || pair[1] === w.hash); }
  function locOf(href) {
    var a = document.createElement('a'); a.href = href;
    return { path: a.pathname.replace(/\/+$/, '') || '/', hash: (a.hash || '').replace(/^#/, '').split('/')[0] };
  }
  function activeTab() {
    var w = here();
    for (var i = 0; i < TABS.length; i++) for (var j = 0; j < TABS[i].at.length; j++) if (matches(TABS[i].at[j], w)) return TABS[i];
    return null;
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]; }); }

  var role = null;
  function allowed(roles) { return !role || !roles || roles.indexOf(role) !== -1; }

  function renderNav() {
    var nav = document.querySelector('nav[data-company-nav]'); if (!nav) return;
    var cur = activeTab();
    nav.innerHTML = TABS.map(function (t) {
      var href = (t.hrefFor && role && t.hrefFor[role]) || t.href;
      return '<a href="' + href + '" data-tab="' + t.key + '" data-roles="' + t.roles.join(',') + '"' + (allowed(t.roles) ? '' : ' hidden') +
        (cur && cur.key === t.key ? ' class="active"' : '') +
        '><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + I[t.key] + '</svg>' + esc(t.label) + '</a>';
    }).join('');
  }
  function renderSubtabs() {
    var w = here();
    var bars = document.querySelectorAll('.ssc-subtabs[data-subtabs]');
    for (var i = 0; i < bars.length; i++) {
      var bar = bars[i], tab = null;
      for (var k = 0; k < TABS.length; k++) if (TABS[k].key === bar.getAttribute('data-subtabs')) tab = TABS[k];
      if (!tab || !tab.sub) { bar.innerHTML = ''; continue; }
      bar.innerHTML = tab.sub.map(function (s) {
        var l = locOf(s[1]), on = (l.path === w.path && (l.path !== '/' && l.path !== '/company' ? true : l.hash === w.hash));
        return '<a class="ssc-subtab' + (on ? ' active' : '') + '" href="' + s[1] + '"' + (allowed(s[2]) ? '' : ' hidden') + '>' + esc(s[0]) + '</a>';
      }).join('');
    }
  }
  function renderTitles() {
    var cur = activeTab();
    var crumb = document.querySelector('[data-nav-crumb]'); if (crumb) crumb.textContent = cur ? cur.label : 'Company';
    var h1 = document.querySelector('[data-nav-h1]');
    if (h1) {
      var on = document.querySelector('.ssc-subtabs[data-subtabs] .ssc-subtab.active');
      h1.textContent = on ? on.textContent : (cur ? cur.label : h1.textContent);
    }
  }
  function refresh() { if (redirectIfRetired()) return; renderNav(); renderSubtabs(); renderTitles(); }

  // The admin pages call this with the signed-in role (and auth_menu.js fires
  // 'ssc:user-loaded' everywhere) — the same hook the old per-page sidebars used.
  window.sscNavForRole = function (r) { role = r || null; refresh(); };
  document.addEventListener('ssc:user-loaded', function (e) { if (e.detail && e.detail.role) window.sscNavForRole(e.detail.role); });
  window.addEventListener('hashchange', refresh);

  window.SSC_NAV = {
    TABS: TABS,
    mount: function () {
      if (window.__SSC_USER && window.__SSC_USER.role) role = window.__SSC_USER.role;
      refresh();
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', refresh);
    },
    refresh: refresh,
    redirectIfRetired: redirectIfRetired,
    current: activeTab
  };
})();
