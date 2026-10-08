/* Auth menu — small header affordance showing logged-in name + role + Sign out.
 *
 * Renders into a host element with id="user-menu". Fetches /api/auth/me; on
 * success populates the host with name/role + Sign out. On failure the
 * before_request gate has already redirected the page to /login, so the
 * fetch failing here is only seen during a stale-cookie race — we redirect
 * defensively.
 *
 * Shared by company-dashboard.html and dashboard-static.html. Worker app
 * does NOT include this file — workers use the PIN flow and have no
 * "user" entity here.
 */
(function () {
  'use strict';

  // Role labels for display. Server-side role values are short codes; the
  // UI shows a human form so the operator doesn't see "c_suite" in the chrome.
  var ROLE_LABEL = {
    admin: 'Admin',
    c_suite: 'C-Suite',
    pm: 'PM',
    super: 'Super'
  };

  function $(id) { return document.getElementById(id); }

  // #291/#292 — instant-paint boot cache. STAFF surfaces use localStorage
  // (survives the browser restart: the FIRST page of the day paints warm)
  // with a 24h TTL; per-user key material, purged on Sign out
  // (guard-asserted) and on any cached-uid/live-uid mismatch. Portal + worker
  // surfaces keep their own sessionStorage caches (shared/borrowed devices
  // must never carry a payload across sessions).
  var BOOT_TTL_MS = 24 * 60 * 60 * 1000;
  window.SSC_BOOT = {
    _key: function (page) { return 'ssc.boot.' + page; },
    read: function (page) {
      try {
        var j = JSON.parse(localStorage.getItem(this._key(page)) || 'null');
        if (!j || !j.bd) return null;
        if (j.at && (Date.now() - j.at) > BOOT_TTL_MS) {         // stale day-old
          localStorage.removeItem(this._key(page));
          return null;
        }
        return j;
      } catch (e) { return null; }
    },
    write: function (page, bd) {
      try {
        var uid = (window.__SSC_USER && window.__SSC_USER.id) || null;
        localStorage.setItem(this._key(page),
          JSON.stringify({ bd: bd, uid: uid, at: Date.now() }));
      } catch (e) { /* quota/serialization — cache is optional */ }
    },
    purge: function () {
      try {
        [localStorage, sessionStorage].forEach(function (store) {
          var ks = [];
          for (var i = 0; i < store.length; i++) {
            var k = store.key(i);
            if (k && k.indexOf('ssc.boot.') === 0) ks.push(k);
          }
          ks.forEach(function (k) { store.removeItem(k); });
        });
      } catch (e) { }
    },
    guardUser: function (user) {
      // purge every cached page whose writer wasn't THIS user
      try {
        [localStorage, sessionStorage].forEach(function (store) {
          for (var i = store.length - 1; i >= 0; i--) {
            var k = store.key(i);
            if (!k || k.indexOf('ssc.boot.') !== 0) continue;
            var j = JSON.parse(store.getItem(k) || 'null');
            if (j && j.uid != null && user && j.uid !== user.id) store.removeItem(k);
          }
        });
      } catch (e) { }
    }
  };

  // #292 — SHARED perf chrome: skeleton frames + intent prefetch + the
  // flag-#5 REDRAW DIFF: renderIfChanged(key, slice, fn) content-hashes the
  // widget's input slice and runs fn ONLY when the data actually moved —
  // identical background refreshes and idle polls touch nothing (zero DOM,
  // zero chart re-init); a change re-renders exactly the affected widget.
  // _rc render counters are the guard's evidence (planted identical payload
  // must not bump; planted change bumps only its widget).
  window.SSC_PERF = {
    _rh: {},   // widgetKey -> content hash of the last-rendered slice
    _rc: {},   // widgetKey -> render count (diagnosis + guard)
    renderIfChanged: function (widgetKey, payloadSlice, renderFn) {
      var h;
      try { h = JSON.stringify(payloadSlice); } catch (e) { h = null; }
      if (h !== null && this._rh[widgetKey] === h) return false;  // identical
      if (h !== null) this._rh[widgetKey] = h;
      this._rc[widgetKey] = (this._rc[widgetKey] || 0) + 1;
      renderFn();
      return true;
    },
    // House-style oat-soft skeleton blocks: structure visible instantly,
    // never blank white, never a spinner. el gets n shimmer rows until the
    // first real render replaces its innerHTML.
    skeleton: function (el, n) {
      if (!el || el.childElementCount) return;
      if (!document.getElementById('ssc-skel-style')) {
        var st = document.createElement('style');
        st.id = 'ssc-skel-style';
        st.textContent =
          '.ssc-skel{display:block;border-radius:8px;background:linear-gradient(90deg,' +
          '#F1EEE8 25%,#E8E4DD 37%,#F1EEE8 63%);background-size:400% 100%;' +
          'animation:sscskel 1.2s ease infinite;height:16px;margin:10px 0;}' +
          '@keyframes sscskel{0%{background-position:100% 0}100%{background-position:0 0}}';
        document.head.appendChild(st);
      }
      var frag = document.createDocumentFragment();
      for (var i = 0; i < (n || 3); i++) {
        var d = document.createElement('span');
        d.className = 'ssc-skel';
        if (i === 0) d.style.width = '60%';
        frag.appendChild(d);
      }
      el.appendChild(frag);
    },
    // #292 close — the generic cache-first loader: paint the cached payload
    // immediately (when present), fetch live, write through, and re-render
    // ONLY if the live payload differs (flag-#5 diff built in). Returns the
    // live payload promise. NOT for comp-data surfaces (labor rates) — those
    // never persist payloads at rest.
    cacheFirstJSON: function (cacheKey, url, render) {
      var env = window.SSC_BOOT && window.SSC_BOOT.read(cacheKey);
      var paintedHash = null;
      if (env && env.bd !== undefined) {
        try { render(env.bd); paintedHash = JSON.stringify(env.bd); } catch (e) { }
      }
      return fetch(url, { credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(function (j) {
          var d = (j && j.data !== undefined) ? j.data : j;
          if (window.SSC_BOOT) window.SSC_BOOT.write(cacheKey, d);
          var s = null;
          try { s = JSON.stringify(d); } catch (e) { }
          if (s !== null && s === paintedHash) return d;   // identical: zero re-render
          render(d);
          return d;
        });
    },
    // Hover/intent prefetch (STAFF surfaces only — never wired on portal or
    // worker shells): an element with data-prefetch="/api/...&page=<key>"
    // warms the SSC_BOOT cache for its target page on first hover, at most
    // once per key per 30s.
    _pf: {},
    prefetchWire: function (root) {
      var self = this;
      (root || document).addEventListener('mouseover', function (e) {
        var a = e.target && e.target.closest && e.target.closest('[data-prefetch]');
        if (!a) return;
        var spec = a.getAttribute('data-prefetch') || '';
        var cut = spec.lastIndexOf('#');
        if (cut < 0) return;
        var url = spec.slice(0, cut), page = spec.slice(cut + 1);
        if (!url || !page) return;
        var now = Date.now();
        if (self._pf[page] && (now - self._pf[page]) < 30000) return;
        self._pf[page] = now;
        fetch(url, { credentials: 'same-origin' }).then(function (r) {
          return r.ok ? r.json() : null;
        }).then(function (j) {
          if (j && j.data && window.SSC_BOOT) window.SSC_BOOT.write(page, j.data);
        }).catch(function () { });
      }, { passive: true });
    }
  };

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function injectStyles() {
    if ($('auth-menu-styles')) return;
    var style = document.createElement('style');
    style.id = 'auth-menu-styles';
    style.textContent = [
      '.auth-menu { display: inline-flex; align-items: center; gap: 10px; font-size: 12px; color: #14161C; }',
      '.auth-menu .au-name { font-weight: 600; color: inherit; text-decoration: none; }',
      '.auth-menu .au-name:hover { color: #B11E2E; text-decoration: underline; }',
      '.auth-menu .au-role {',
      '  font-size: 10px; font-weight: 700; letter-spacing: 0.8px; text-transform: uppercase;',
      '  color: #76777E; background: #F1ECE2; padding: 3px 8px; border-radius: 99px;',
      '}',
      '.auth-menu .au-logout {',
      '  font: inherit; font-size: 11px; font-weight: 600; color: #76777E;',
      '  background: transparent; border: 1px solid #E8E4DD; border-radius: 6px;',
      '  padding: 4px 10px; cursor: pointer; transition: color .15s, border-color .15s;',
      '}',
      '.auth-menu .au-logout:hover { color: #B11E2E; border-color: #B11E2E; }',
      '.auth-menu .au-logout:disabled { opacity: 0.6; cursor: not-allowed; }',
      '.auth-menu .au-users {',
      '  font: inherit; font-size: 11px; font-weight: 700; color: #B11E2E;',
      '  text-decoration: none; border: 1px solid #E8E4DD; border-radius: 6px;',
      '  padding: 4px 10px; transition: border-color .15s;',
      '}',
      '.auth-menu .au-users:hover { border-color: #B11E2E; }'
    ].join('\n');
    document.head.appendChild(style);
  }

  function render(host, user) {
    var role = ROLE_LABEL[user.role] || user.role || '—';
    // #258 — admin-only entry point to the user-management console. Visible only to
    // role=admin; /admin/users is gated server-side (this link is UX, not the authority).
    var adminLink = (user.role === 'admin')
      ? '<a class="au-users" href="/admin/users" title="User management">Users</a>' : '';
    host.innerHTML =
      '<div class="auth-menu" data-user-id="' + escapeHtml(user.id) + '">' +
        // your name opens your own account (name, password, two-step sign-in, devices)
        '<a class="au-name" href="/account" title="Your account">' + escapeHtml(user.display_name || user.full_name || user.email) + '</a>' +
        '<span class="au-role">' + escapeHtml(role) + '</span>' +
        adminLink +
        '<button type="button" class="au-logout" aria-label="Sign out">Sign out</button>' +
      '</div>';
    var btn = host.querySelector('.au-logout');
    btn.addEventListener('click', function () {
      btn.disabled = true;
      btn.textContent = 'Signing out…';
      // #291 — the boot cache dies WITH the session, before the network call
      // (logout must clear it even if the logout request itself fails)
      if (window.SSC_BOOT) window.SSC_BOOT.purge();
      fetch('/api/auth/logout', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' }
      }).then(function () {
        window.location.href = '/login';
      }).catch(function () {
        // Even on network failure, the user wanted to log out — kick them.
        window.location.href = '/login';
      });
    });
  }

  function load() {
    var host = $('user-menu');
    if (!host) return;
    injectStyles();
    fetch('/api/auth/me', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' }
    }).then(function (res) {
      if (!res.ok) {
        if (res.status === 401) {
          window.location.href = '/login';
          return null;
        }
        throw new Error('me failed: ' + res.status);
      }
      return res.json();
    }).then(function (body) {
      if (body && body.user) {
        // Expose for other client code that needs to know the role (e.g. to
        // hide controls a non-admin shouldn't see). The server-side gating
        // remains the real authority — this is UX, not security.
        window.__SSC_USER = body.user;
        if (window.SSC_BOOT) window.SSC_BOOT.guardUser(body.user);  // #291
        render(host, body.user);
        document.dispatchEvent(new CustomEvent('ssc:user-loaded', { detail: body.user }));
      }
    }).catch(function () {
      // Quietly leave the placeholder; logging the URL would risk leaking
      // session info via a CORS-blocked console message.
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', load);
  } else {
    load();
  }
})();
