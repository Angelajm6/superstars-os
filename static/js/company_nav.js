/* #339 — ONE company sidebar.
 *
 * Every company-level page (the portal at /company, the console panels at /#…, the
 * admin pages) renders the SAME seven tabs from this file, so the navigation can never
 * drift between pages again. A tab is a destination; a page shows its tab's sub-tabs
 * in a row under the header (`<div class="ssc-subtabs" data-subtabs="finance">`).
 * #341 adds a pinned destination — Settings — rendered separately at the
 * sidebar's bottom edge rather than as one of the scrollable tabs.
 *
 * Also owns the redirects that retire the old homes: the old console dashboard
 * (`/`, `/#projects`) and CRM (`/#crm`) forward to the portal; the portal's job-sites
 * hashes forward to Projects. Include SYNC in <head> (the redirect must run before
 * anything paints), then `SSC_NAV.mount()` right after the empty <nav data-company-nav>.
 *
 * Roles: the sidebar shows what the role can open (server gates stay the control).
 * admin / c_suite see all seven tabs + pinned Settings; an estimator sees Proposals
 * (their queue) and nothing else. */
(function () {
  'use strict';
  var COMPANY = ['admin', 'c_suite'];
  var I = {
    home: '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>',
    dashboard: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    projects: '<path d="M3 21V7l7-4v18M10 21h11V11l-4-2v12"/><path d="M6 9h1M6 13h1M6 17h1M14 13h1M14 17h1"/>',
    clients: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
    proposals: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>',
    finance: '<path d="M3 17l5-5 4 4 8-8"/><path d="M14 8h6v6"/>',
    workforce: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><circle cx="17" cy="9" r="2.5"/><path d="M15 14.5a5 5 0 0 1 6.5 5"/>',
    company: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z"/><path d="M9 12l2 2 4-4"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>'
  };

  // A location is (path, first hash segment). A tab lights up for any of its `at` pairs;
  // a sub-tab for its exact pair. `null` hash = any hash on that path.
  var TABS = [
    // PMs and supers only reach a company-shell page for their own Account; Home takes them back.
    { key: 'home', label: 'Home', href: '/projects', roles: ['pm', 'super'], at: [['/projects', null]] },
    { key: 'dashboard', label: 'Dashboard', href: '/company#overview', roles: COMPANY,
      at: [['/company', 'overview'], ['/company', '']] },
    { key: 'projects', label: 'Projects', href: '/company#projects', roles: COMPANY,
      at: [['/company', 'projects'], ['/company', 'calendar'], ['/company', 'buildings'], ['/admin/projects', null]],
      sub: [['All projects', '/company#projects'], ['Calendar', '/company#calendar'], ['Assignments & onboarding', '/admin/projects']] },
    // Client portal lives under Clients: who can sign in to the client portal and what each one
    // sees sits next to the clients themselves (#360 made it a tab of its own; now a sub-tab).
    { key: 'clients', label: 'Clients', href: '/company#clients', roles: COMPANY,
      at: [['/company', 'clients'], ['/company', 'portal']],
      sub: [['All clients', '/company#clients'], ['Client portal', '/company#portal']] },
    { key: 'proposals', label: 'Proposals', href: '/company#proposals', roles: COMPANY.concat(['estimator']),
      hrefFor: { estimator: '/estimating' },
      at: [['/company', 'proposals'], ['/', 'estimates'], ['/estimating', null]],
      sub: [['Estimates & proposals', '/company#proposals', COMPANY], ['Estimating queue', '/estimating']] },
    { key: 'finance', label: 'Finance', href: '/company#finance', roles: COMPANY,
      at: [['/company', 'finance'], ['/company', 'expenses'], ['/', 'labor-rate'], ['/admin/labor-rates', null]],
      sub: [['What needs to get paid', '/company#finance'], ['Expenses', '/company#expenses'], ['Weekly payroll', '/#labor-rate'], ['Pay rates', '/admin/labor-rates']] },
    { key: 'workforce', label: 'Workforce', href: '/company#workforce', roles: COMPANY,
      at: [['/company', 'workforce'], ['/', 'workforce'], ['/', 'hours'], ['/', 'cert-library'], ['/', 'cert-health']],
      sub: [['Workers', '/company#workforce'], ['Onboarding & directory', '/#workforce'], ['Certifications', '/#cert-library'], ['Sign-ins', '/#hours']] },
    { key: 'company', label: 'Company', href: '/company#company', roles: COMPANY,
      at: [['/company', 'company'], ['/company', 'compliance'], ['/', 'compliance'], ['/', 'specs']],
      sub: [['Documents & vehicles', '/company#company'], ['Compliance documents', '/#compliance'], ['Specs', '/#specs']] }
  ];

  // #341 — Settings pinned as its own entry at the sidebar's bottom edge (a sibling
  // .ssc-side-foot right after <nav data-company-nav>, margin-top:auto in shell.css),
  // not buried in the Company tab's sub-nav — mirrors the same move on the project
  // dashboard sidebar. Same shape as a TABS entry (key/label/href/roles/at) so
  // matches()/here() work unchanged; rendered separately since it lives outside <nav>.
  // #360 — Settings owns WHO CAN SIGN IN (Team & access) and this account's preferences
  // and security; Company keeps the company's records. Before, /#settings drew the
  // Company sub-tabs and Team & access sat under Company, so the two read as one place.
  // Account (each person's own name, password, two-step sign-in, devices) replaced
  // "Preferences & security" and its Interface card; /#settings forwards there.
  var PINNED = { key: 'settings', label: 'Settings', href: '/account', roles: COMPANY,
    at: [['/account', null], ['/admin/users', null], ['/company', 'usage']],
    sub: [['Account', '/account'], ['Team & access', '/admin/users'], ['Usage & job tools', '/company#usage']] };

  // ---- the old homes forward here (runs at include time, before paint) ----
  var REDIRECTS = {
    '/': { '': '/company#overview', 'projects': '/company#overview', 'crm': '/company#clients', 'settings': '/account' },
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
  function onPinned(w) { for (var i = 0; i < PINNED.at.length; i++) if (matches(PINNED.at[i], w)) return true; return false; }
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
    renderPinned(nav);
  }
  function renderPinned(nav) {
    var foot = nav.nextElementSibling;
    if (!foot || !foot.hasAttribute('data-company-nav-foot')) {
      foot = document.createElement('div');
      foot.className = 'ssc-side-foot';
      foot.setAttribute('data-company-nav-foot', '');
      nav.insertAdjacentElement('afterend', foot);
    }
    var w = here(), on = onPinned(w);
    foot.innerHTML = '<a href="' + PINNED.href + '" data-tab="' + PINNED.key + '"' + (allowed(PINNED.roles) ? '' : ' hidden') +
      (on ? ' class="active"' : '') +
      '><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + I.settings + '</svg>' + esc(PINNED.label) + '</a>';
  }
  function renderSubtabs() {
    var w = here();
    var bars = document.querySelectorAll('.ssc-subtabs[data-subtabs]');
    for (var i = 0; i < bars.length; i++) {
      var bar = bars[i], tab = null;
      for (var k = 0; k < TABS.length; k++) if (TABS[k].key === bar.getAttribute('data-subtabs')) tab = TABS[k];
      if (bar.getAttribute('data-subtabs') === PINNED.key) tab = PINNED;
      if (!tab || !tab.sub) { bar.innerHTML = ''; continue; }
      bar.innerHTML = tab.sub.map(function (s) {
        var l = locOf(s[1]), on = (l.path === w.path && (l.path !== '/' && l.path !== '/company' ? true : l.hash === w.hash));
        return '<a class="ssc-subtab' + (on ? ' active' : '') + '" href="' + s[1] + '"' + (allowed(s[2]) ? '' : ' hidden') + '>' + esc(s[0]) + '</a>';
      }).join('');
    }
  }
  // ONE title per page: the top bar names the sidebar section ("Clients"); the sub-tab
  // row under it names the page ("Client portal"). No small label repeating the title.
  function renderTitles() {
    var cur = activeTab();
    var pinnedHere = onPinned(here());
    var h1 = document.querySelector('[data-nav-h1]');
    if (h1) h1.textContent = cur ? cur.label : (pinnedHere && allowed(PINNED.roles) ? PINNED.label : h1.textContent);
  }
  function refresh() { if (redirectIfRetired()) return; renderNav(); renderSubtabs(); renderTitles(); }

  // The admin pages call this with the signed-in role (and auth_menu.js fires
  // 'ssc:user-loaded' everywhere) — the same hook the old per-page sidebars used.
  window.sscNavForRole = function (r) { role = r || null; refresh(); };
  document.addEventListener('ssc:user-loaded', function (e) { if (e.detail && e.detail.role) window.sscNavForRole(e.detail.role); });
  window.addEventListener('hashchange', refresh);

  window.SSC_NAV = {
    TABS: TABS,
    PINNED: PINNED,
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
