/* "From your email" card — Gmail suggestions, shared by the team home (/projects) and the
 * company dashboard. Renders into <section data-gmail-card hidden></section>.
 *
 * Not switched on: hidden for everyone except admin / C-suite, who see one line saying so.
 * Not connected: a Connect Gmail row. Connected: checked by itself every 2 minutes / Disconnect, then each new
 * suggestion — who sent it, the subject, the proposed task or event (title, date, time,
 * address, why) and Add to my to-do / Edit first / Dismiss / Open in Gmail. Adding creates a
 * task or event assigned to the person; the page is told so its to-do list refreshes.
 */
(function () {
  'use strict';
  var card = document.querySelector('[data-gmail-card]');
  if (!card) return;
  var role = null, ST = null, rows = [];
  var OUTCOME = {
    connected: ['Gmail connected. New mail in your Primary inbox is checked every 10 minutes.', false],
    connected_retry: ['Gmail connected. The first check did not finish; it retries by itself in a couple of minutes.', true],
    denied: ['Gmail was not connected (permission not given).', true],
    account: ['Use your own company Google account, the same email you sign in with here.', true],
    scope: ['Google did not grant read access to Gmail. Try again and allow it.', true],
    state: ['That connection link expired. Try Connect Gmail again.', true],
    failed: ['Google did not finish the connection. Try again in a minute.', true]
  };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function localDate(s) { var p = String(s).split(/[-T:]/).map(Number); return new Date(p[0], p[1] - 1, p[2], p[3] || 0, p[4] || 0); }   // LOCAL
  function ago(ts) {
    if (!ts) return 'not yet';
    var m = Math.round((Date.now() - localDate(ts).getTime()) / 60000);
    return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : m < 1440 ? Math.round(m / 60) + ' h ago' : Math.round(m / 1440) + ' d ago';
  }
  function fmtTime(t) { if (!t) return ''; var p = t.split(':').map(Number); return (p[0] % 12 || 12) + (p[1] ? ':' + String(p[1]).padStart(2, '0') : '') + (p[0] < 12 ? ' AM' : ' PM'); }
  function when(s) {
    var d = s.date ? localDate(s.date).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' }) : '';
    if (s.kind === 'task') return d ? 'Due ' + d + (s.time ? ' · ' + fmtTime(s.time) : '') : 'No deadline';
    return (d || 'No date') + (s.time ? ' · ' + fmtTime(s.time) : '');
  }
  function received(s) {
    if (!s.received_at) return '';
    var d = localDate(s.received_at), today = new Date();
    return d.toDateString() === today.toDateString() ? fmtTime(s.received_at.slice(11, 16)) : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  }
  async function call(path, method, body) {
    var r = await fetch(path, { method: method || 'GET', credentials: 'same-origin', headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    var j = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(j.error || ('Request failed (' + r.status + ')'));
    return j.data;
  }
  function flash(msg, bad) {
    var n = card.querySelector('.gm-flash');
    if (!n) { n = document.createElement('div'); n.className = 'gm-flash'; n.setAttribute('role', 'status'); var h = card.querySelector('.gm-head'); (h || card).insertAdjacentElement('afterend', n); }
    n.className = 'gm-flash' + (bad ? ' bad' : ''); n.textContent = msg;
    clearTimeout(n._h); n._h = setTimeout(function () { n.remove(); }, 8000);
  }
  function connectHref() { return '/gmail/connect?next=' + encodeURIComponent(location.pathname + location.hash); }
  function sugRow(s) {
    if (s.status === 'added') return '<div class="gm-sug"><div class="gm-done">✓ Added to your to-do: ' + esc(s.title) + '</div></div>';
    if (s.status === 'dismissed') return '<div class="gm-sug"><div class="gm-gone">Dismissed · ' + esc(s.subject || s.title) + ' <button type="button" class="gm-btn ghost" data-undo="' + s.id + '">Undo</button></div></div>';
    return '<div class="gm-sug" data-sug="' + s.id + '"><div class="gm-from"><b>' + esc(s.from_name) + '</b>' + (received(s) ? ' · ' + esc(received(s)) : '') + '</div>' +
      (s.subject ? '<div class="gm-subj">“' + esc(s.subject) + '”</div>' : '') +
      '<div class="gm-prop ' + (s.kind === 'event' ? 'event' : 'task') + '"><span class="gm-k">' + (s.kind === 'event' ? 'Event' : 'Task') + '</span><div class="gm-pm"><div class="gm-t">' + esc(s.title) + '</div>' +
      '<div class="gm-m">' + esc(when(s)) + (s.address ? ' · 📍 ' + esc(s.address) : '') + '</div>' + (s.why ? '<div class="gm-m gm-why">' + esc(s.why) + '</div>' : '') + '</div></div>' +
      '<div class="gm-acts"><button type="button" class="gm-btn primary" data-add="' + s.id + '">Add to my to-do</button><button type="button" class="gm-btn" data-edit="' + s.id + '">Edit first</button>' +
      '<button type="button" class="gm-btn ghost" data-dismiss="' + s.id + '">Dismiss</button><a class="gm-open" href="' + esc(s.gmail_url) + '" target="_blank" rel="noopener">Open in Gmail ↗</a></div>' +
      '<form class="gm-edit" data-form="' + s.id + '" hidden>' +
        '<div class="gm-grid"><label>Type<select name="kind"><option value="task"' + (s.kind === 'task' ? ' selected' : '') + '>Task</option><option value="event"' + (s.kind === 'event' ? ' selected' : '') + '>Event</option></select></label>' +
        '<label class="wide">What<input name="title" maxlength="200" value="' + esc(s.title) + '"></label>' +
        '<label>Date<input name="date" type="date" value="' + esc(s.date) + '"></label><label>Time<input name="time" type="time" value="' + esc(s.time) + '"></label>' +
        '<label class="wide">Address<input name="address" maxlength="300" value="' + esc(s.address) + '"></label></div>' +
        '<div class="gm-acts"><button type="submit" class="gm-btn primary">Add to my to-do</button><button type="button" class="gm-btn ghost" data-cancel-edit="' + s.id + '">Cancel</button></div>' +
      '</form></div>';
  }
  function render() {
    if (!ST || !ST.available) {
      if (ST && (role === 'admin' || role === 'c_suite')) {
        card.hidden = false;
        card.innerHTML = '<div class="gm-head"><h2>From your email</h2></div><div class="gm-row">✉️ <span title="' + (ST.reason === 'encryption_key_missing' ? 'Technical detail: the field encryption key is missing on the server.' : 'Technical detail: the Google connection is not configured on the server yet.') + '">Email suggestions aren\'t set up yet. They need a one-time Google setup on the server.</span></div>';
      } else card.hidden = true;
      return;
    }
    card.hidden = false;
    var head = '<div class="gm-head"><h2>From your email</h2><span class="gm-count">' + (ST.new_count ? ST.new_count + ' new' : '') + '</span></div>';
    if (!ST.connected) {
      card.innerHTML = head + '<div class="gm-row">✉️ <span>Let the app spot to-dos in your Gmail (Primary inbox) and suggest them here. Read-only; only you see them.</span><a class="gm-btn primary" href="' + esc(connectHref()) + '">Connect Gmail</a></div>';
      return;
    }
    if (ST.inbox && ST.status !== 'needs_reconnect') {   // admin / C-suite: their email lives in the Inbox (inbox.py)
      card.innerHTML = head.replace(/<span class="gm-count">[^<]*<\/span>/, '') + '<div class="gm-row">✉️ <span>Your email arrives in the Inbox by itself, matched to clients, bids and proposals, with suggestions to approve.' + (ST.google_email ? ' <span class="gm-mute">(' + esc(ST.google_email) + ')</span>' : '') + '</span><a class="gm-btn primary" href="/company#inbox">Open Inbox</a></div>';
      return;
    }
    var row = ST.status === 'needs_reconnect'
      ? '<div class="gm-row warn">⚠ <span>Gmail stopped: the Google sign-in expired.</span><a class="gm-btn primary" href="' + esc(connectHref()) + '">Reconnect</a><button type="button" class="gm-btn ghost" data-off>Disconnect</button></div>'
      : '<div class="gm-row">✉️ <span>Reading new Primary inbox mail' + (ST.google_email ? ' <span class="gm-mute">(' + esc(ST.google_email) + ')</span>' : '') + ' · checked by itself every 2 minutes, last ' + esc(ago(ST.last_check_at)) +
        (ST.problem ? ' <span class="gm-bad">· ' + esc(ST.problem) + '</span>' : '') + '</span><button type="button" class="gm-btn ghost" data-off>Disconnect</button></div>';
    var list = rows.length ? rows.map(sugRow).join('') : '<div class="gm-empty">Nothing to add right now. New to-dos from your email show up here.</div>';
    card.innerHTML = head + row + list;
  }
  function editing() { return !!card.querySelector('[data-form]:not([hidden])'); }
  async function load(force) {
    if (!force && editing()) return;   // never wipe a suggestion someone is editing
    try {
      ST = await call('/api/gmail/status');
      rows = ST.connected ? await call('/api/gmail/suggestions') : [];
    } catch (e) { ST = null; }
    render();
  }
  function refreshTodo() { document.dispatchEvent(new CustomEvent('ssc:items-changed')); }
  card.addEventListener('click', async function (e) {
    var b = e.target.closest('button'); if (!b || b.type === 'submit') return;   // the edit form's own button submits the form
    if (!['data-add', 'data-dismiss', 'data-undo', 'data-edit', 'data-cancel-edit', 'data-off'].some(function (a) { return b.hasAttribute(a); })) return;
    var id = b.getAttribute('data-add') || b.getAttribute('data-dismiss') || b.getAttribute('data-undo') || b.getAttribute('data-edit') || b.getAttribute('data-cancel-edit');
    if (b.hasAttribute('data-edit')) { var f = card.querySelector('[data-form="' + id + '"]'); f.hidden = false; f.querySelector('input[name=title]').focus(); return; }
    if (b.hasAttribute('data-cancel-edit')) { card.querySelector('[data-form="' + id + '"]').hidden = true; return; }
    b.disabled = true;
    try {
      if (b.hasAttribute('data-add')) { await call('/api/gmail/suggestions/' + id + '/accept', 'POST', {}); flash('Added to your to-do.'); refreshTodo(); }
      else if (b.hasAttribute('data-dismiss')) await call('/api/gmail/suggestions/' + id + '/dismiss', 'POST');
      else if (b.hasAttribute('data-undo')) await call('/api/gmail/suggestions/' + id + '/undo', 'POST');
      else if (b.hasAttribute('data-off')) {
        if (!confirm('Disconnect Gmail? Your pending email suggestions are cleared. Tasks you already added stay.')) { b.disabled = false; return; }
        await call('/api/gmail/disconnect', 'POST'); flash('Gmail disconnected.');
      }
      await load(true);
    } catch (err) { flash(err.message, true); b.disabled = false; }
  });
  card.addEventListener('submit', async function (e) {
    var f = e.target.closest('[data-form]'); if (!f) return;
    e.preventDefault();
    var id = f.getAttribute('data-form'), fd = new FormData(f), btn = f.querySelector('button[type=submit]');
    var dIn = f.querySelector('[name=date]'), dv = (window.SSCDatePicker && SSCDatePicker.getISO) ? SSCDatePicker.getISO(dIn) : dIn.value;   // the picker keeps YYYY-MM-DD apart from what it shows
    btn.disabled = true;
    try {
      await call('/api/gmail/suggestions/' + id + '/accept', 'POST', { kind: fd.get('kind'), title: String(fd.get('title') || '').trim(), date: dv || '', time: fd.get('time') || '', address: String(fd.get('address') || '').trim() });
      flash('Added to your to-do.'); refreshTodo(); await load(true);
    } catch (err) { flash(err.message, true); btn.disabled = false; }
  });
  var q = new URLSearchParams(location.search), out = q.get('gmail');
  if (out && OUTCOME[out]) {
    setTimeout(function () { flash(OUTCOME[out][0], OUTCOME[out][1]); }, 600);
    q.delete('gmail');
    history.replaceState(null, '', location.pathname + (q.toString() ? '?' + q : '') + location.hash);
  }
  document.addEventListener('ssc:user-loaded', function (e) { role = (e.detail || {}).role || null; load(); });
  setTimeout(function () { if (role === null) load(); }, 2500);
  setInterval(function () { if (ST && ST.connected && !document.hidden) load(); }, 300000);   // pick up the 10-minute checks
})();
