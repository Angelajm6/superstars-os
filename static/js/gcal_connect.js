/* Google Calendar connect row — shared by the team home (/projects) and the company dashboard.
 *
 * Renders into <div data-gcal></div>: "Connect Google Calendar" when not connected; when
 * connected, the account, the last sync and Sync now / Disconnect; a reconnect prompt when
 * Google's sign-in expired. Hidden for team members while the feature is switched off;
 * admin and C-suite see a one-line "not switched on yet" so they know it exists.
 * After Google sends the person back (?gcal=…) it shows the outcome once and cleans the URL.
 */
(function () {
  'use strict';
  var host = document.querySelector('[data-gcal]');
  if (!host) return;
  var role = null;
  var OUTCOME = {
    connected: ['Google Calendar connected. Your tasks go to the "Superstars" calendar, and your own events show in your Inbox within a minute (only you see them).', false],
    connected_retry: ['Google Calendar connected. The first sync did not finish; it will retry on the next change, or use Sync now.', true],
    denied: ['Google Calendar was not connected (permission not given).', true],
    account: ['Use your own company Google account, the same email you sign in with here.', true],
    scope: ['Google did not grant calendar access. Try again and allow the Superstars calendar.', true],
    state: ['That connection link expired. Try Connect Google Calendar again.', true],
    failed: ['Google did not finish the connection. Try again in a minute.', true]
  };

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function ago(ts) {
    if (!ts) return 'not yet';
    var p = ts.split(/[-T:]/).map(Number), then = new Date(p[0], p[1] - 1, p[2], p[3] || 0, p[4] || 0, p[5] || 0);   // LOCAL time
    var m = Math.round((Date.now() - then.getTime()) / 60000);
    return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : m < 1440 ? Math.round(m / 60) + ' h ago' : Math.round(m / 1440) + ' d ago';
  }
  function note(msg, bad) {
    var t = document.createElement('div');
    t.className = 'gcal-note' + (bad ? ' bad' : '');
    t.setAttribute('role', 'status');
    t.textContent = msg;
    host.insertAdjacentElement('afterend', t);
    setTimeout(function () { t.remove(); }, 9000);
  }
  async function call(path, method) {
    var r = await fetch(path, { method: method || 'GET', credentials: 'same-origin', headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' } });
    var j = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(j.error || ('Request failed (' + r.status + ')'));
    return j.data || {};
  }
  function connectHref() {
    return '/google-calendar/connect?next=' + encodeURIComponent(location.pathname + location.hash);
  }
  function render(s) {
    host.className = 'gcal';
    if (!s.available) {
      if (role === 'admin' || role === 'c_suite') {
        host.innerHTML = '<span class="gcal-ic" aria-hidden="true">📅</span><span class="gcal-t">Google Calendar sync is not switched on yet' +
          (s.reason === 'encryption_key_missing' ? ' (the field encryption key is missing on the server).' : ' (the Google setup is not finished).') + '</span>';
        host.hidden = false;
      } else host.hidden = true;
      return;
    }
    host.hidden = false;
    if (!s.connected) {
      host.innerHTML = '<span class="gcal-ic" aria-hidden="true">📅</span><span class="gcal-t">Two-way with Google Calendar: your tasks, events and meetings go to Google, and your own Google events show in your Inbox, private to you.</span>' +
        '<a class="gcal-btn primary" href="' + esc(connectHref()) + '">Connect Google Calendar</a>';
      return;
    }
    if (s.status === 'needs_reconnect') {
      host.className = 'gcal warn';
      host.innerHTML = '<span class="gcal-ic" aria-hidden="true">⚠</span><span class="gcal-t">Google Calendar stopped syncing: the Google sign-in expired.</span>' +
        '<a class="gcal-btn primary" href="' + esc(connectHref()) + '">Reconnect</a><button type="button" class="gcal-btn" data-gcal-off>Disconnect</button>';
      return;
    }
    if (!s.reads_calendar) {   // connected before your own events could come in: one reconnect adds the read permission
      host.innerHTML = '<span class="gcal-ic" aria-hidden="true">📅</span><span class="gcal-t">Your tasks go to Google Calendar. Reconnect once so your own Google events show in your Inbox too (only you see them).</span>' +
        '<a class="gcal-btn primary" href="' + esc(connectHref()) + '">Reconnect</a><button type="button" class="gcal-btn" data-gcal-off>Disconnect</button>';
      return;
    }
    host.innerHTML = '<span class="gcal-ic" aria-hidden="true">📅</span><span class="gcal-t">Two-way with Google Calendar' +
      (s.google_email ? ' <span class="gcal-m">(' + esc(s.google_email) + ')</span>' : '') +
      ' · <span class="gcal-m">' + (s.synced_count || 0) + ' item' + (s.synced_count === 1 ? '' : 's') + ' · last sync ' + esc(ago(s.last_sync_at)) + ' · your calendar read ' + esc(ago(s.import_at)) + '</span>' +
      (s.problem ? ' <span class="gcal-bad">· ' + esc(s.problem) + '</span>' : '') + '</span>' +
      '<button type="button" class="gcal-btn" data-gcal-sync>Sync now</button><button type="button" class="gcal-btn" data-gcal-off>Disconnect</button>';
  }
  async function load() {
    try { render(await call('/api/google-calendar/status')); } catch (e) { host.hidden = true; }
  }
  host.addEventListener('click', async function (e) {
    var b = e.target.closest('button'); if (!b) return;
    b.disabled = true;
    try {
      if (b.hasAttribute('data-gcal-sync')) { b.textContent = 'Syncing…'; var s = await call('/api/google-calendar/sync', 'POST'); render(s); if (s.error) note('The sync did not finish. It will retry on the next change.', true); }
      if (b.hasAttribute('data-gcal-off')) {
        if (!confirm('Disconnect Google Calendar? The "Superstars" calendar is removed from your Google Calendar. Nothing changes in the app.')) { b.disabled = false; return; }
        await call('/api/google-calendar/disconnect', 'POST'); note('Google Calendar disconnected.'); load();
      }
    } catch (err) { note(err.message, true); b.disabled = false; }
  });
  // the outcome of a Connect round trip, once
  var q = new URLSearchParams(location.search), out = q.get('gcal');
  if (out && OUTCOME[out]) {
    note(OUTCOME[out][0], OUTCOME[out][1]);
    q.delete('gcal');
    history.replaceState(null, '', location.pathname + (q.toString() ? '?' + q : '') + location.hash);
  }
  document.addEventListener('ssc:user-loaded', function (e) { role = (e.detail || {}).role || null; load(); });
  setTimeout(function () { if (role === null) load(); }, 2500);   // in case the user event fired before this ran
})();
