/* Company chat page (/chat). Server: chat.py. Accepted prototype 2026-11-16:
 * https://claude.ai/artifact/C7oS45e2gZxt6wGGDC7VMs
 * Live by polling: /api/chat/poll every 4 s while the page is visible (one tiny query); a change
 * refetches the list and the open chat. Times are the server's LOCAL ISO strings, parsed as local.
 *
 * 2026-11-17 (accepted prototype https://claude.ai/artifact/4qCXUYDRtc2xQ5AWLgz15i): the same code runs in the floating
 * Chat window on every other page (static/js/chat_widget.js sets window.SSC_CHAT_W and injects chat_fragment.html);
 * there it is always one column, never touches the address bar, and reads / polls only while the window is open.
 * New: edit your own messages (↑ edits the last one; earlier versions under "(edited)"), Seen by (double-click or
 * double-tap a message; a line under your latest one), a pinned message, who is on site, "is typing", Translate to
 * English, Catch me up (Claude), and quick replies in job chats. Later the same day: talk to type (the mic). */
(function () {
  'use strict';
  if (window.SSC_CHAT) return;
  var $ = function (id) { return document.getElementById(id); };
  var W = window.SSC_CHAT_W || null;   // the floating window; null on the /chat page
  var COLORS = ['#4364dc', '#0f8b8d', '#7a5cc7', '#3c7f5a', '#c0632f', '#4a6fa5', '#9b4f8a', '#5b6b2e'];
  var S = { me: null, convs: [], cur: null, detail: null, msgs: [], more: false, rev: -1, filter: 'all',
            pending: [], reply: null, mentions: {}, people: null, side: 'files', sending: false, lastRead: {},
            edit: null, hist: {}, tr: {}, seen: null, onsite: {}, catchup: null, typing: [], pinged: 0, tap: null };
  function phone() { return window.matchMedia('(max-width:860px)').matches; }
  function wide() { return !W && !phone(); }   // the three-column /chat page
  function awake() { return !document.hidden && (!W || W.isOpen()); }
  function layout() { var ch = $('ch'); if (ch) ch.classList.toggle('compact', !!W || phone()); }
  function hm(t) { var m = /^(\d\d?):(\d\d)/.exec(t || ''); if (!m) return t || ''; var h = +m[1]; return ((h % 12) || 12) + ':' + m[2] + ' ' + (h < 12 ? 'AM' : 'PM'); }
  function first(n) { return String(n || '').split(' ')[0]; }

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function api(path, opt) {
    opt = opt || {};
    var init = { method: opt.method || 'GET', credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (opt.form) init.body = opt.form;
    else if (opt.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opt.body); }
    return fetch('/api/chat' + path, init).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { var e = new Error(j.error || 'Something went wrong. Try again.'); e.status = r.status; throw e; }
        return j.data;
      });
    });
  }
  function color(key) { var h = 0; for (var i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) | 0; return COLORS[Math.abs(h) % COLORS.length]; }
  function initials(n) { var p = String(n || '?').trim().split(/\s+/); return ((p[0] || '?')[0] + (p.length > 1 ? p[p.length - 1][0] : '')).toUpperCase(); }
  function avatar(key, name, cls) {
    var on = S.onsite && S.onsite[key];
    return '<div class="ch-av ' + (cls || '') + '" style="background:' + color(key) + '"' + (on ? ' title="' + esc(name + ' · on site since ' + hm(on.since)) + '"' : '') + '>' +
      esc(initials(name)) + (on ? '<i class="ch-on" aria-label="On site"></i>' : '') + '</div>';
  }
  function convAvatar(c) {
    if (c.kind === 'job') return '<div class="ch-av job">JOB</div>';
    if (c.kind === 'channel') return '<div class="ch-av sq">#</div>';
    var w = c['with'] || [];
    if (w.length > 1) return '<div class="ch-av grp"><i style="background:' + color(w[0].key) + '">' + esc(initials(w[0].name)) + '</i><i style="background:' + color(w[1].key) + '">' + esc(initials(w[1].name)) + '</i></div>';
    return w.length ? avatar(w[0].key, w[0].name) : '<div class="ch-av sq">·</div>';
  }
  function convTitle(c) { return c.kind === 'channel' ? '# ' + c.title : c.title; }

  // ---- local time (the server stores local wall-clock time; never convert to UTC) ----
  function parse(s) { var m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)/.exec(s || ''); return m ? new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5]) : null; }
  function dayKey(d) { return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate(); }
  function clock(d) { var h = d.getHours(), m = d.getMinutes(); return ((h % 12) || 12) + ':' + (m < 10 ? '0' : '') + m + ' ' + (h < 12 ? 'AM' : 'PM'); }
  function dayLabel(d) {
    var t = new Date(), y = new Date(t.getFullYear(), t.getMonth(), t.getDate() - 1);
    if (dayKey(d) === dayKey(t)) return 'Today';
    if (dayKey(d) === dayKey(y)) return 'Yesterday';
    var opts = { weekday: 'long', month: 'short', day: 'numeric' };
    if (d.getFullYear() !== t.getFullYear()) opts.year = 'numeric';
    return d.toLocaleDateString('en-US', opts);
  }
  function shortWhen(s) {
    var d = parse(s); if (!d) return '';
    var t = new Date();
    if (dayKey(d) === dayKey(t)) return clock(d);
    var y = new Date(t.getFullYear(), t.getMonth(), t.getDate() - 1);
    if (dayKey(d) === dayKey(y)) return 'Yesterday';
    if ((t - d) < 6 * 864e5) return d.toLocaleDateString('en-US', { weekday: 'short' });
    return (d.getMonth() + 1) + '/' + d.getDate() + '/' + String(d.getFullYear()).slice(2);
  }
  function size(b) { return b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB'; }
  function fileUrl(f, thumb) { return '/api/chat/files/' + f.id + (thumb ? '?thumb=1' : ''); }

  // ---- list ----
  function renderList() {
    var q = ($('ch-q').value || '').trim().toLowerCase();
    var show = function (c) {
      if (S.filter === 'unread' && !c.unread) return false;
      if (S.filter === 'job' && c.kind !== 'job') return false;
      if (S.filter === 'dm' && c.kind !== 'dm') return false;
      return !q || (convTitle(c) + ' ' + (c.preview || '') + ' ' + (c.project_code || '')).toLowerCase().indexOf(q) !== -1;
    };
    var groups = [['Jobs', 'job'], ['Channels', 'channel'], ['Direct messages', 'dm']], h = '';
    var here = W && W.job;   // the window on a job's page: that job's chat first
    groups.forEach(function (g) {
      var items = S.convs.filter(function (c) { return c.kind === g[1] && show(c); });
      if (here && g[1] === 'job') items.sort(function (a, b) { return (b.project_code === here) - (a.project_code === here); });
      if (!items.length && (g[1] === 'job' || q || S.filter !== 'all')) return;
      h += '<div class="ch-sec">' + g[0] + (g[1] !== 'job' && (g[1] === 'dm' || S.me.can_channel) ? '<button type="button" data-add="' + g[1] + '" aria-label="New ' + (g[1] === 'dm' ? 'message' : 'channel') + '" title="New ' + (g[1] === 'dm' ? 'message' : 'channel') + '">+</button>' : '') + '</div>';
      if (!items.length) h += '<p class="ch-empty">' + (g[1] === 'dm' ? 'No private messages yet. Press + to message someone.' : 'No channels yet.') + '</p>';
      items.forEach(function (c) {
        var isHere = here && c.kind === 'job' && c.project_code === here;
        h += '<button type="button" class="ch-conv' + (S.cur === c.id ? ' on' : '') + (c.unread ? ' unread' : '') + (isHere ? ' here' : '') + '" data-id="' + c.id + '">' + convAvatar(c) +
          '<div class="m"><div class="n"><span>' + esc(convTitle(c)) + '</span>' + (isHere ? '<span class="ch-herel">This job</span>' : '') + '<span class="t">' + esc(shortWhen(c.last_at)) + '</span></div>' +
          '<div class="p"><span>' + esc(c.preview || (c.joined ? 'No messages yet' : 'Open to read and join')) + '</span>' + (c.notify === 'off' ? '<span class="mute" title="Muted">muted</span>' : '') +
          (c.unread ? '<span class="ch-badge' + (c.mention ? ' at' : '') + '">' + (c.mention ? '@' : (c.unread > 99 ? '99+' : c.unread)) + '</span>' : '') + '</div></div></button>';
      });
    });
    $('ch-convs').innerHTML = h || '<p class="ch-empty">No chats match. Try a name, a job code like FR-BX-001, or a channel name.</p>';
    var total = S.convs.reduce(function (s, c) { return s + (c.unread || 0); }, 0);
    document.dispatchEvent(new CustomEvent('ssc:chat-unread', { detail: { unread: total } }));
    if (!W) document.title = (total ? '(' + total + ') ' : '') + 'Chat · Superstars Contracting';
  }
  function loadConvs() {
    return api('/convs').then(function (list) { S.convs = list || []; renderList(); })
      .catch(function (e) { if (!S.convs.length) $('ch-convs').innerHTML = '<p class="ch-empty">' + esc(e.message) + '</p>'; });
  }

  // ---- thread ----
  function linkify(s) {
    return esc(s).replace(/\bhttps?:\/\/[^\s<]+[^\s<.,;:!?)"']/g, function (u) { return '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + u + '</a>'; });
  }
  function fmtBody(m) {
    var html = linkify(m.body);
    var names = Object.keys(S.mentionNames || {}).sort(function (a, b) { return b.length - a.length; });
    names.forEach(function (n) { html = html.split('@' + esc(n)).join('<span class="ch-at">@' + esc(n) + '</span>'); });
    return html;
  }
  function attHTML(files) {
    return files.map(function (f) {
      if (f.kind === 'image') return '<button type="button" class="ch-photo" data-photo="' + f.id + '" aria-label="Open photo"><img loading="lazy" src="' + fileUrl(f, true) + '" alt="' + esc(f.name) + '"></button>';
      return '<a class="ch-file" href="' + fileUrl(f) + '" target="_blank" rel="noopener"><div class="ch-ext ' + esc(f.ext) + '">' + esc((f.ext || 'file').slice(0, 4)) + '</div><div><div class="fn">' + esc(f.name) + '</div><div class="fs">' + size(f.size) + (f.by ? ' · ' + esc(f.by) : '') + '</div></div></a>';
    }).join('');
  }
  function renderThread(keepScroll) {
    var box = $('ch-msgs'), atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80, oldH = box.scrollHeight, oldTop = box.scrollTop;
    var c = S.detail, h = '';
    S.mentionNames = {};
    (c.members || []).forEach(function (p) { S.mentionNames[p.name] = 1; });
    if (S.people) S.people.forEach(function (p) { S.mentionNames[p.name] = 1; });
    if (S.me) S.mentionNames[S.me.name] = 1;
    if (S.more) h += '<button type="button" class="btn-ghost ch-more" id="ch-older">Show earlier messages</button>';
    else if (c.kind === 'job') h += '<div class="ch-sys">Everyone on the ' + esc(c.project_code) + ' crew is in this chat, with the job’s PM and the office. People join and leave as they are added to or taken off the job.</div>';
    else if (c.everyone) h += '<div class="ch-sys">Everyone at the company is in #' + esc(c.title) + ', office and field.</div>';
    else if (!S.msgs.length) h += '<div class="ch-sys">This is the start of ' + esc(c.kind === 'channel' ? '#' + c.title : 'your conversation with ' + c.title) + '.</div>';
    var lastDay = '', lastAuthor = '', lastT = 0;
    var ed = document.activeElement && document.activeElement.id === 'ch-edta' ? document.activeElement : null;
    var caret = ed ? [ed.selectionStart, ed.selectionEnd] : null;
    S.msgs.forEach(function (m) {
      var d = parse(m.at) || new Date(), dk = dayKey(d);
      if (dk !== lastDay) { h += '<div class="ch-day">' + esc(dayLabel(d)) + '</div>'; lastDay = dk; lastAuthor = ''; }
      var cont = m.author === lastAuthor && (d - lastT) < 5 * 60000 && !m.reply;
      lastAuthor = m.author; lastT = d;
      h += '<div class="ch-msg' + (cont ? ' cont' : '') + '" data-mid="' + m.id + '">' + avatar(m.author, m.name) + '<div class="b">' +
        (cont ? '' : '<div class="w"><b>' + esc(m.mine ? m.name + ' (you)' : m.name) + '</b>' + (m.field ? '<span class="rl">Field</span>' : '') + '<span class="tm">' + clock(d) + '</span></div>') +
        (m.reply ? '<button type="button" class="ch-quote" data-jump="' + m.reply.id + '"><b>' + esc(m.reply.name) + ':</b> ' + esc(m.reply.body) + '</button>' : '') +
        bodyHTML(m) +
        (m.files && m.files.length ? '<div class="ch-atts">' + attHTML(m.files) + '</div>' : '') +
        (m.reacts && m.reacts.length ? '<div class="ch-reacts">' + m.reacts.map(function (r) { return '<button type="button" class="ch-react' + (r.mine ? ' mine' : '') + '" data-react="' + m.id + '" data-emoji="' + esc(r.emoji) + '" title="' + esc(r.who.join(', ')) + '">' + esc(r.emoji) + ' ' + r.count + '</button>'; }).join('') + '</div>' : '') +
        '</div>' + (m.deleted || (S.edit && S.edit.id === m.id) ? '' : toolsHTML(m)) + '</div>';
      if (S.seen && S.seen.msg_id === m.id && !m.deleted) { h += seenHTML(S.seen); lastAuthor = ''; }
    });
    box.innerHTML = h;
    var nta = $('ch-edta');
    if (nta && S.edit) { nta.value = S.edit.draft; growEd(nta); if (ed || S.edit.focus) { nta.focus(); var p = caret || [nta.value.length, nta.value.length]; nta.setSelectionRange(p[0], p[1]); S.edit.focus = false; } }
    if (keepScroll === 'older') box.scrollTop = box.scrollHeight - oldH + oldTop;
    else if (!keepScroll || atBottom) box.scrollTop = box.scrollHeight;
    else box.scrollTop = oldTop;
  }
  function bodyHTML(m) {
    if (m.deleted) return '<div class="x del">Message deleted</div>';
    if (S.edit && S.edit.id === m.id) {
      return '<div class="ch-edbox"><textarea id="ch-edta" rows="2" maxlength="4000" aria-label="Edit message"></textarea>' +
        '<div class="ch-edbar"><span>' + (phone() ? '' : 'Enter to save · Esc to cancel') + '</span><button type="button" class="btn-ghost" data-edcancel="1">Cancel</button>' +
        '<button type="button" class="btn-primary" data-edsave="' + m.id + '">Save</button></div></div>';
    }
    var tr = S.tr[m.id], showTr = tr && tr.on && tr.text;
    var h = '';
    if (m.body || m.edited) {
      h += '<div class="x">' + (showTr ? esc(tr.text) : fmtBody(m)) +
        (m.edited ? ' <button type="button" class="ch-edited" data-hist="' + m.id + '" title="Edited ' + esc(stamp(m.edited)) + '. Show the earlier version">(edited)</button>' : '') + '</div>';
    }
    var hs = S.hist[m.id];
    if (hs && hs.open) {
      h += '<div class="ch-prev">' + (hs.edits ? (hs.edits.length ? hs.edits.map(function (e, i) {
        return '<div><b>' + (i === 0 ? 'Original' : 'Version ' + (i + 1)) + '</b> · changed ' + esc(stamp(e.at)) + '<br>' + esc(e.body || '(no text)') + '</div>';
      }).join('') : 'No earlier versions.') : 'Loading…') + '</div>';
    }
    if (m.es && m.body && !S.me.worker) {
      h += showTr ? '<div class="ch-trn">Translated from Spanish by Claude · <button type="button" class="ch-tr" data-tr="' + m.id + '">Show original</button></div>'
        : '<button type="button" class="ch-tr" data-tr="' + m.id + '"' + (tr && tr.busy ? ' disabled' : '') + '>' + (tr && tr.busy ? 'Translating…' : 'Translate to English') + '</button>';
    }
    return h;
  }
  function stamp(s) { var d = parse(s); if (!d) return ''; return dayKey(d) === dayKey(new Date()) ? clock(d) : shortWhen(s) + ' ' + clock(d); }
  function seenHTML(s) {
    var label;
    if (!s.count) label = 'Sent';
    else if (s.total === 1) label = 'Seen' + (s.at ? ' ' + stamp(s.at) : '');
    else if (s.count >= s.total) label = 'Seen by everyone';
    else { var n = s.who.map(function (p) { return first(p.name); }); label = 'Seen by ' + (s.count > 2 ? n.slice(0, 2).join(', ') + ' +' + (s.count - 2) : n.join(' and ')); }
    var st = s.count && s.total > 1 ? '<span class="st">' + s.who.slice(0, 3).map(function (p) { return '<i style="background:' + color(p.key) + '">' + esc(initials(p.name)) + '</i>'; }).join('') + '</span>' : '';
    return '<button type="button" class="ch-seen" data-seen="' + s.msg_id + '" title="Who has seen it">' + st + esc(label) + '</button>';
  }
  function toolsHTML(m) {
    var em = (S.me.emoji || []).slice(0, 4).map(function (e) { return '<button type="button" data-react="' + m.id + '" data-emoji="' + esc(e) + '" title="React ' + esc(e) + '">' + esc(e) + '</button>'; }).join('');
    var pinned = S.detail.pin && S.detail.pin.id === m.id;
    return '<div class="ch-tools">' + em + '<button type="button" data-reply="' + m.id + '">Reply</button>' +
      (m.mine ? '<button type="button" data-edit="' + m.id + '">Edit</button>' : '') +
      (S.me.worker ? '' : '<button type="button" data-seen="' + m.id + '">Seen by</button>') +
      (S.detail.can_pin ? '<button type="button" data-pin="' + (pinned ? 0 : m.id) + '">' + (pinned ? 'Unpin' : 'Pin') + '</button>' : '') +
      (S.detail.can_task ? '<button type="button" data-task="' + m.id + '">Make task</button>' : '') +
      (m.mine || S.me.can_delete_any ? '<button type="button" class="danger" data-del="' + m.id + '">Delete</button>' : '') + '</div>';
  }
  function growEd(t) { t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 200) + 'px'; }
  function startEdit(id) {
    var m = S.msgs.filter(function (x) { return x.id === +id; })[0];
    if (!m || !m.mine || m.deleted) return;
    S.edit = { id: m.id, draft: m.body || '', focus: true };
    renderThread(true);
    var el = document.querySelector('.ch-msg[data-mid="' + m.id + '"]'); if (el) el.scrollIntoView({ block: 'nearest' });
  }
  function cancelEdit() { S.edit = null; renderThread(true); ta.focus(); }
  function saveEdit() {
    if (!S.edit || S.edit.busy) return;
    var e = S.edit, v = ($('ch-edta') ? $('ch-edta').value : e.draft).trim();
    var m = S.msgs.filter(function (x) { return x.id === e.id; })[0];
    if (m && v === (m.body || '')) { cancelEdit(); return; }
    e.busy = true;
    api('/messages/' + e.id, { method: 'PATCH', body: { body: v } }).then(function (nm) {
      S.msgs = S.msgs.map(function (x) { return x.id === nm.id ? nm : x; });
      delete S.hist[nm.id]; delete S.tr[nm.id];
      S.edit = null; renderThread(true); ta.focus(); loadConvs();
    }).catch(function (er) { e.busy = false; toast(er.message); });
  }
  function headerFor(c) {
    $('ch-tav').innerHTML = convAvatar(S.convs.filter(function (x) { return x.id === c.id; })[0] || c);
    $('ch-tname').textContent = c.kind === 'channel' ? '# ' + c.title : c.title;
    var sub = c.sub || '';
    if (c.kind === 'dm') { var o = c.members.filter(function (p) { return p.key !== S.me.key; }); sub = o.length > 1 ? 'Group · ' + c.members.length + ' people' : (o[0] ? o[0].role || '' : ''); }
    var on = (c.members || []).filter(function (p) { return p.key !== S.me.key && S.onsite[p.key]; }).map(function (p) { return first(p.name); });
    var onTxt = !on.length || c.everyone ? '' : on.length > 2 ? on.length + ' on site' : on.join(' and ') + ' on site';
    $('ch-tsub').innerHTML = (onTxt ? '<span class="ch-sub-on">● ' + esc(onTxt) + '</span> · ' : '') + esc(sub) +
      (c.kind === 'job' && !(W && W.job === c.project_code) ? ' · <a href="/projects/' + encodeURIComponent(c.project_code) + '">Open job</a>' : '');
    renderPin();
    quick();
    var mem = c.members || [];
    $('ch-tstack').innerHTML = mem.slice(0, 3).map(function (p) { return '<i style="background:' + color(p.key) + '">' + esc(initials(p.name)) + '</i>'; }).join('');
    $('ch-tcount').textContent = c.everyone ? 'Everyone' : String(c.member_count);
    renderBell(c.notify);
    $('ch-ta').placeholder = 'Message ' + (c.kind === 'channel' ? '#' + c.title : c.kind === 'job' ? 'the ' + c.project_code + ' crew' : c.title);
  }
  function renderPin() {
    var p = S.detail && S.detail.pin, bar = $('ch-pin');
    bar.hidden = !p;
    if (!p) return;
    $('ch-pingo').innerHTML = '<b>' + esc(p.name) + ':</b> ' + esc(p.body);
    $('ch-pinx').hidden = !S.detail.can_pin;
  }
  function pin(id) {
    api('/convs/' + S.cur + '/pin', { method: 'POST', body: { msg_id: id || null } }).then(function (d) {
      S.detail.pin = d.pin; renderPin(); renderThread(true); toast(d.pin ? 'Pinned to the top of this chat' : 'Unpinned');
    }).catch(function (er) { toast(er.message); });
  }
  function jumpTo(id) {
    var el = document.querySelector('.ch-msg[data-mid="' + id + '"]');
    if (!el) { toast('That message is further back. Use Show earlier messages to find it.'); return; }
    el.scrollIntoView({ block: 'center' }); el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
  }
  function quick() {
    var q = $('ch-quick'); if (!q) return;
    q.hidden = !(S.detail && S.detail.kind === 'job') || !!ta.value.trim() || S.pending.length > 0 || !!S.reply;
  }
  // "Catch me up": offered when you open a chat with 3 or more new messages
  function renderCatch() {
    var w = $('ch-catchw'), k = S.catchup;
    if (!k || k.conv !== S.cur || k.pending || k.closed) { w.innerHTML = ''; return; }
    if (k.bullets) {
      w.innerHTML = '<div class="ch-sum" role="note"><button type="button" class="x" data-catchx="1" aria-label="Close summary">✕</button><b><span aria-hidden="true">✦</span>Claude · ' + k.count + ' new since ' + esc(stamp(k.since)) + '</b><ul>' +
        k.bullets.map(function (b) { return '<li>' + esc(b) + '</li>'; }).join('') + '</ul></div>';
      return;
    }
    w.innerHTML = '<div class="ch-catch"><span><b>' + k.count + ' new</b> since you last looked</span><button type="button" data-catch="1"' + (k.busy ? ' disabled' : '') + '><span aria-hidden="true">✦</span>' + (k.busy ? 'Reading…' : 'Catch me up') + '</button><button type="button" class="x" data-catchx="1" aria-label="Dismiss">✕</button></div>';
  }
  $('ch-catchw').addEventListener('click', function (e) {
    var k = S.catchup, t = e.target.closest('button'); if (!t || !k) return;
    if (t.dataset.catchx) { k.closed = true; renderCatch(); return; }
    if (t.dataset.catch && !k.busy) {
      k.busy = true; renderCatch();
      api('/convs/' + k.conv + '/catchup', { method: 'POST', body: { after_id: k.after } })
        .then(function (d) { k.bullets = d.bullets; k.count = d.count; k.since = d.since; })
        .catch(function (er) { toast(er.message); })
        .then(function () { k.busy = false; renderCatch(); });
    }
  });
  function renderTyping() {
    var el = $('ch-typing'), n = S.typing || [];
    el.hidden = !n.length;
    if (n.length) el.innerHTML = '<span class="ch-dots" aria-hidden="true"><i></i><i></i><i></i></span>' + esc(n.length > 2 ? 'Several people are typing…' : n.join(' and ') + (n.length > 1 ? ' are' : ' is') + ' typing…');
  }
  function markRead() {
    if (!S.cur || !awake() || !S.msgs.length) return;
    var last = S.msgs[S.msgs.length - 1].id;
    if (S.lastRead[S.cur] >= last) return;
    S.lastRead[S.cur] = last;
    api('/convs/' + S.cur + '/read', { method: 'POST', body: { last_id: last } }).then(function () {
      S.convs.forEach(function (c) { if (c.id === S.cur) { c.unread = 0; c.mention = false; } });
      renderList();
    }).catch(function () {});
  }
  function loadThread(keep) {
    var id = S.cur;
    return Promise.all([api('/convs/' + id), api('/convs/' + id + '/messages')]).then(function (r) {
      if (S.cur !== id) return;
      S.detail = r[0]; S.msgs = r[1].messages; S.more = r[1].more; S.seen = r[1].seen; S.onsite = r[0].on_site || {};
      var k = S.catchup;
      if (k && k.conv === id && k.pending) { k.pending = false; k.after = r[1].read_id; if (k.count < 3 || S.me.worker) S.catchup = null; }
      headerFor(S.detail); renderThread(keep); renderCatch(); markRead();
      if (!$('ch-side').hidden) renderSide();
    }).catch(function (e) {
      if (S.cur !== id) return;
      if (e.status === 404) { toast(e.message); closeThread(); loadConvs(); }
    });
  }
  function open(id, opts) {
    id = +id; if (!id) return;
    if (S.cur !== id) {
      if (V && V.rec) stopVoice(true);
      S.pending = []; S.reply = null; S.mentions = {}; S.edit = null; S.seen = null; S.typing = []; renderPending(); renderReply(); $('ch-ta').value = ''; grow();
      var c = S.convs.filter(function (x) { return x.id === id; })[0];
      S.catchup = { conv: id, pending: true, count: c ? c.unread || 0 : 0 };
      $('ch-pin').hidden = true; $('ch-catchw').innerHTML = ''; renderTyping();
    }
    S.cur = id;
    if (W) W.onConv(id);
    else { try { history.replaceState(null, '', '#c' + id); } catch (e) {} }
    $('ch-noth').hidden = true; $('ch-th').hidden = false; $('ch-msgs').hidden = false; $('ch-comp').hidden = false;
    $('ch').classList.add('open'); if (!W) document.body.classList.add('ch-in');
    $('ch-msgs').innerHTML = '<div class="ch-sys">Loading…</div>';
    renderList();
    return loadThread().then(function () { if (!(opts && opts.noFocus) && !phone()) $('ch-ta').focus(); });
  }
  function closeThread() {
    if (V && V.rec) stopVoice(true);
    S.cur = null; S.detail = null; S.edit = null; S.typing = []; renderTyping();
    $('ch').classList.remove('open'); document.body.classList.remove('ch-in');
    $('ch-noth').hidden = false; $('ch-th').hidden = true; $('ch-msgs').hidden = true; $('ch-comp').hidden = true; $('ch-side').hidden = true;
    $('ch-pin').hidden = true; $('ch-catchw').innerHTML = '';
    if (W) W.onConv(null);
    else { try { history.replaceState(null, '', location.pathname); } catch (e) {} }
    renderList();
  }

  // ---- live: one cheap poll; a change refetches ----
  function tick() {
    if (!awake()) return;
    var cur = S.cur;
    api('/poll' + (cur ? '?conv=' + cur : '')).then(function (d) {
      if (cur && cur === S.cur) {
        var t = d.typing || [];
        if (t.join() !== (S.typing || []).join()) { S.typing = t; renderTyping(); }
      }
      if (d.rev === S.rev) return;
      var first = S.rev === -1; S.rev = d.rev;
      if (first) return;
      loadConvs(); if (S.cur && !S.sending) loadThread(true);
    }).catch(function () {});
  }

  // ---- composer ----
  var ta = $('ch-ta');
  function grow() { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 160) + 'px'; }
  function canSend() { $('ch-send').disabled = S.sending || S.pending.some(function (p) { return !p.id && !p.err; }) || !(ta.value.trim() || S.pending.some(function (p) { return p.id; })); quick(); if (V) trbox(); }
  function ping() {   // "is typing": at most one tiny call every 4 s while you type
    if (!S.cur || !ta.value.trim() || Date.now() - S.pinged < 4000) return;
    S.pinged = Date.now();
    api('/convs/' + S.cur + '/typing', { method: 'POST', body: {} }).catch(function () {});
  }
  function showErr(t) { $('ch-err').textContent = t || ''; $('ch-err').hidden = !t; }
  function send() {
    if (V.rec) stopVoice(true);
    var body = ta.value.trim(), ids = S.pending.filter(function (p) { return p.id; }).map(function (p) { return p.id; });
    if ((!body && !ids.length) || $('ch-send').disabled) return;
    var ment = Object.keys(S.mentions).filter(function (k) { return body.indexOf('@' + S.mentions[k]) !== -1; });
    S.sending = true; canSend(); showErr('');
    var id = S.cur;
    api('/convs/' + id + '/messages', { method: 'POST', body: { body: body, file_ids: ids, reply_to: S.reply ? S.reply.id : null, mentions: ment } })
      .then(function (m) {
        ta.value = ''; grow(); S.pending = []; S.reply = null; S.mentions = {}; S.pinged = 0; renderPending(); renderReply();
        if (S.cur === id) { S.msgs.push(m); S.seen = { msg_id: m.id, count: 0, total: 0, who: [] }; renderThread(); S.lastRead[id] = m.id; }
        loadConvs();
      })
      .catch(function (e) { showErr(e.message); })
      .then(function () { S.sending = false; canSend(); });
  }
  ta.addEventListener('input', function () { grow(); canSend(); mentionCheck(); showErr(''); ping(); });
  ta.addEventListener('keydown', function (e) {
    var pop = $('ch-mpop');
    if (!pop.hidden) {
      var bs = pop.querySelectorAll('button'), i = Array.prototype.indexOf.call(bs, pop.querySelector('.sel'));
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); if (bs[i]) bs[i].classList.remove('sel'); i = (i + (e.key === 'ArrowDown' ? 1 : bs.length - 1)) % bs.length; bs[i].classList.add('sel'); return; }
      if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); (pop.querySelector('.sel') || bs[0]).click(); return; }
      if (e.key === 'Escape') { pop.hidden = true; return; }
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !phone()) { e.preventDefault(); send(); return; }
    if (e.key === 'ArrowUp' && !ta.value && !S.pending.length) {   // ↑ in an empty box: edit your last message
      for (var j = S.msgs.length - 1; j >= 0; j--) { if (S.msgs[j].mine && !S.msgs[j].deleted) { e.preventDefault(); startEdit(S.msgs[j].id); return; } }
    }
  });
  $('ch-send').addEventListener('click', send);
  $('ch-quick').addEventListener('click', function (e) {
    var b = e.target.closest('[data-q]'); if (!b) return;
    ta.value = b.dataset.q; grow(); canSend(); ta.focus();
  });

  // ---- talk to type (2026-11-17, accepted prototype https://claude.ai/artifact/4qCXUYDRtc2xQ5AWLgz15i v2) ----
  // The phone's / browser's own speech recognition writes into the box as you talk (English or Spanish; no audio is
  // kept, only the text, which you can still edit). "✦ To English / A español" has Claude rewrite the box before you
  // send (POST /api/chat/translate), with Undo. The mic shows only where the browser has speech recognition.
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  var V = { rec: null, lang: null, base: '', busy: false };
  var ES = /\b(el|la|los|las|que|de|del|para|por|con|una|necesitamos|necesito|está|están|estamos|mañana|hoy|gracias|vamos|pero|también|después|terminado|aquí|ya|hay|muy|bien|tenemos|tengo|más)\b/gi;
  function looksEs(t) { var w = (t.match(/[a-záéíóúñü]+/gi) || []).length, h = (t.match(ES) || []).length; return /[ñ¿¡]/.test(t) || (h >= 2 && h / Math.max(w, 1) >= 0.2) || (w < 4 && h >= 1); }
  function vlang() {
    if (!V.lang) { try { V.lang = localStorage.getItem('ssc.chat.vlang'); } catch (e) {} }
    return V.lang === 'es' || V.lang === 'en' ? V.lang : (/^es/i.test(navigator.language || '') ? 'es' : 'en');
  }
  function trbox() {
    var b = $('ch-trbox'), v = ta.value.trim();
    b.hidden = !v || !!V.rec || !S.me || !!S.me.worker;
    if (!b.hidden && !V.busy) $('ch-trlbl').textContent = looksEs(v) ? 'To English' : 'A español';
  }
  function renderListen() {
    $('ch-listen').hidden = !V.rec;
    var mic = $('ch-mic'); mic.classList.toggle('live', !!V.rec); mic.setAttribute('aria-label', V.rec ? 'Stop talking' : 'Talk to type');
    mic.setAttribute('aria-pressed', V.rec ? 'true' : 'false');
    Array.prototype.forEach.call($('ch-listen').querySelectorAll('[data-vl]'), function (b) { b.classList.toggle('on', b.dataset.vl === vlang()); });
    trbox();
  }
  function startVoice() {
    if (!SR || V.rec || !S.cur) return;
    var r = new SR();
    r.lang = vlang() === 'es' ? 'es-US' : 'en-US'; r.continuous = true; r.interimResults = true;
    V.base = ta.value ? ta.value.replace(/\s*$/, ' ') : '';
    r.onresult = function (e) {
      var said = '';
      for (var i = 0; i < e.results.length; i++) said += e.results[i][0].transcript;
      ta.value = (V.base + said.trim()).replace(/^\s+/, ''); grow(); canSend(); ping();
    };
    r.onerror = function (e) {
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') toast('Allow the microphone for this site to talk to type.');
      else if (e.error === 'no-speech') toast('Nothing heard. Tap the mic and try again.');
      else if (e.error === 'network') toast('Talk to type needs a connection. Try again.');
      else if (e.error !== 'aborted') toast('Talk to type stopped. Tap the mic to go on.');
    };
    r.onend = function () { if (V.rec === r) { V.rec = null; renderListen(); if (!phone()) ta.focus(); } };
    try { r.start(); V.rec = r; renderListen(); } catch (er) { toast('Talk to type could not start. Try again.'); }
  }
  function stopVoice(hard) {   // hard: drop what is still on its way (sending, another chat)
    var r = V.rec; if (!r) return;
    if (hard) { r.onresult = null; V.rec = null; renderListen(); try { r.abort(); } catch (e) {} return; }
    try { r.stop(); } catch (e) { V.rec = null; renderListen(); }
  }
  $('ch-mic').hidden = !SR;
  $('ch-mic').addEventListener('click', function () { if (V.rec) stopVoice(); else startVoice(); });
  $('ch-vstop').addEventListener('click', function () { stopVoice(); });
  $('ch-listen').addEventListener('click', function (e) {
    var b = e.target.closest('[data-vl]'); if (!b || b.dataset.vl === vlang()) return;
    V.lang = b.dataset.vl; try { localStorage.setItem('ssc.chat.vlang', V.lang); } catch (er) {}
    if (V.rec) { stopVoice(true); startVoice(); } else renderListen();   // go on in the other language, keeping the text
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && V.rec) stopVoice(); });
  $('ch-trbox').addEventListener('click', function () {
    var v = ta.value.trim(), b = $('ch-trbox'); if (!v || V.busy) return;
    V.busy = true; b.disabled = true; $('ch-trlbl').textContent = 'Translating…';
    api('/translate', { method: 'POST', body: { text: v } }).then(function (d) {
      var was = ta.value; ta.value = d.text; grow(); canSend(); ta.focus();
      undoToast('Translated by Claude', function () { ta.value = was; grow(); canSend(); ta.focus(); });
    }).catch(function (er) { toast(er.message); }).then(function () { V.busy = false; b.disabled = false; trbox(); });
  });

  // attachments: picked, dropped or pasted; each uploads at once, Send waits for them
  function upload(f) {
    var p = { name: f.name || 'photo.jpg', size: f.size, img: null, id: null, err: null };
    if (/^image\//.test(f.type)) { try { p.img = URL.createObjectURL(f); } catch (e) {} }
    S.pending.push(p); renderPending();
    if (S.pending.length > 10) { p.err = 'Up to 10 files per message'; renderPending(); return; }
    var fd = new FormData(); fd.append('conv_id', S.cur); fd.append('file', f, p.name);
    api('/files', { method: 'POST', form: fd }).then(function (d) { p.id = d.id; p.name = d.name; p.size = d.size; })
      .catch(function (e) { p.err = e.message; }).then(renderPending);
  }
  function addFiles(list) { for (var i = 0; i < list.length; i++) upload(list[i]); }
  function renderPending() {
    $('ch-pend').innerHTML = S.pending.map(function (p, i) {
      var ext = (p.name.split('.').pop() || '').slice(0, 4);
      return '<div class="ch-pc' + (p.err ? ' err' : '') + '">' + (p.img ? '<img src="' + p.img + '" alt="">' : '<div class="ch-ext ' + esc(ext.toLowerCase()) + '">' + esc(ext) + '</div>') +
        '<span>' + esc(p.name) + '<br><small>' + (p.err ? esc(p.err) : p.id ? size(p.size) : 'Uploading…') + '</small></span><button type="button" data-rm="' + i + '" aria-label="Remove">✕</button></div>';
    }).join('');
    canSend();
  }
  $('ch-pend').addEventListener('click', function (e) {
    var b = e.target.closest('[data-rm]'); if (!b) return;
    var p = S.pending.splice(+b.dataset.rm, 1)[0];
    if (p && p.id) api('/files/' + p.id, { method: 'DELETE' }).catch(function () {});
    renderPending();
  });
  $('ch-attach').addEventListener('click', function () { $('ch-file').click(); });
  $('ch-cam').addEventListener('click', function () { $('ch-photo').click(); });
  ['ch-file', 'ch-photo'].forEach(function (id) { $(id).addEventListener('change', function (e) { addFiles(e.target.files); e.target.value = ''; }); });
  var box = $('ch-box');
  ['dragenter', 'dragover'].forEach(function (ev) { box.addEventListener(ev, function (e) { if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') !== -1) { e.preventDefault(); box.classList.add('drag'); } }); });
  ['dragleave', 'drop'].forEach(function (ev) { box.addEventListener(ev, function (e) { box.classList.remove('drag'); if (ev === 'drop' && e.dataTransfer && e.dataTransfer.files.length) { e.preventDefault(); addFiles(e.dataTransfer.files); } }); });
  ta.addEventListener('paste', function (e) { var fs = (e.clipboardData && e.clipboardData.files) || []; if (fs.length) { e.preventDefault(); addFiles(fs); } });

  // reply
  function renderReply() {
    $('ch-replying').hidden = !S.reply;
    if (S.reply) $('ch-replytxt').textContent = 'Replying to ' + S.reply.name + ': ' + (S.reply.body || 'Attachment');
    quick();
  }
  $('ch-replyx').addEventListener('click', function () { S.reply = null; renderReply(); ta.focus(); });

  // @mentions: the people in this chat (everyone-channels: the whole company)
  function mentionPool() {
    var mem = (S.detail && S.detail.members) || [];
    return (mem.length ? mem : (S.people || [])).filter(function (p) { return p.key !== S.me.key && !p.gone; });
  }
  function ensurePeople() { if (S.people) return Promise.resolve(S.people); return api('/people').then(function (p) { S.people = p; return p; }); }
  function mentionCheck() {
    var pre = ta.value.slice(0, ta.selectionStart), m = /(^|\s)@([^\s@]{0,20})$/.exec(pre), pop = $('ch-mpop');
    if (!m) { pop.hidden = true; return; }
    var go = function () {
      var q = m[2].toLowerCase(), hits = mentionPool().filter(function (p) { return p.name.toLowerCase().indexOf(q) !== -1; }).slice(0, 7);
      if (!hits.length) { pop.hidden = true; return; }
      pop.innerHTML = hits.map(function (p, i) { return '<button type="button" class="' + (i ? '' : 'sel') + '" data-k="' + esc(p.key) + '" data-n="' + esc(p.name) + '">' + avatar(p.key, p.name, 'sm') + '<span><b>' + esc(p.name) + '</b><small>' + esc(p.role) + '</small></span></button>'; }).join('');
      pop.hidden = false;
    };
    if (S.detail && !S.detail.members.length) ensurePeople().then(go); else go();
  }
  $('ch-mpop').addEventListener('mousedown', function (e) { e.preventDefault(); });
  $('ch-mpop').addEventListener('click', function (e) {
    var b = e.target.closest('[data-k]'); if (!b) return;
    var pos = ta.selectionStart, pre = ta.value.slice(0, pos).replace(/@([^\s@]{0,20})$/, '@' + b.dataset.n + ' ');
    ta.value = pre + ta.value.slice(pos); S.mentions[b.dataset.k] = b.dataset.n;
    ta.focus(); ta.selectionStart = ta.selectionEnd = pre.length; $('ch-mpop').hidden = true; canSend();
  });
  $('ch-atbtn').addEventListener('click', function () {
    var p = ta.selectionStart, sp = p && !/\s/.test(ta.value[p - 1]) ? ' ' : '';
    ta.value = ta.value.slice(0, p) + sp + '@' + ta.value.slice(p); ta.focus(); ta.selectionStart = ta.selectionEnd = p + sp.length + 1; mentionCheck();
  });

  // ---- message actions ----
  $('ch-msgs').addEventListener('click', function (e) {
    var t = e.target.closest('button,a'), msgEl = e.target.closest('.ch-msg');
    if (!t) {   // phone: a tap on a message shows its actions; a double-tap shows who has seen it
      if (msgEl && phone() && !e.target.closest('.ch-edbox')) {
        var now = Date.now(), mid = msgEl.dataset.mid;
        Array.prototype.forEach.call(document.querySelectorAll('.ch-msg.tools-on'), function (x) { if (x !== msgEl) x.classList.remove('tools-on'); });
        if (S.tap && S.tap.id === mid && now - S.tap.t < 350) { S.tap = null; msgEl.classList.remove('tools-on'); openSeen(mid); return; }
        S.tap = { id: mid, t: now };
        msgEl.classList.toggle('tools-on');
      }
      return;
    }
    if (t.dataset.edit) { startEdit(t.dataset.edit); return; }
    if (t.dataset.edsave) { saveEdit(); return; }
    if (t.dataset.edcancel) { cancelEdit(); return; }
    if (t.dataset.seen) { if (msgEl) msgEl.classList.remove('tools-on'); openSeen(t.dataset.seen); return; }
    if (t.dataset.pin !== undefined) { if (msgEl) msgEl.classList.remove('tools-on'); pin(+t.dataset.pin); return; }
    if (t.dataset.hist) {
      var hid = +t.dataset.hist, hs = S.hist[hid] = S.hist[hid] || {};
      hs.open = !hs.open; renderThread(true);
      if (hs.open && !hs.edits) api('/messages/' + hid + '/info').then(function (d) { hs.edits = d.edits; renderThread(true); }).catch(function (er) { hs.open = false; toast(er.message); renderThread(true); });
      return;
    }
    if (t.dataset.tr) {
      var tid = +t.dataset.tr, tr = S.tr[tid] = S.tr[tid] || {};
      if (tr.text) { tr.on = !tr.on; renderThread(true); return; }
      if (tr.busy) return;
      tr.busy = true; renderThread(true);
      api('/messages/' + tid + '/translate', { method: 'POST', body: { lang: 'en' } }).then(function (d) { tr.text = d.text; tr.on = true; })
        .catch(function (er) { toast(er.message); }).then(function () { tr.busy = false; renderThread(true); });
      return;
    }
    if (t.id === 'ch-older') {
      api('/convs/' + S.cur + '/messages?before=' + S.msgs[0].id).then(function (d) { S.msgs = d.messages.concat(S.msgs); S.more = d.more; renderThread('older'); });
      return;
    }
    var find = function (id) { return S.msgs.filter(function (m) { return m.id === +id; })[0]; };
    if (t.dataset.react) {
      api('/messages/' + t.dataset.react + '/react', { method: 'POST', body: { emoji: t.dataset.emoji } }).then(function () { loadThread(true); }).catch(function (er) { toast(er.message); });
    } else if (t.dataset.reply) {
      var m = find(t.dataset.reply); S.reply = { id: m.id, name: m.name, body: m.body }; renderReply(); ta.focus();
    } else if (t.dataset.task) {
      openTask(find(t.dataset.task));
    } else if (t.dataset.del) {
      if (t.dataset.sure) {
        api('/messages/' + t.dataset.del, { method: 'DELETE' }).then(function () { loadThread(true); loadConvs(); toast('Message deleted'); }).catch(function (er) { toast(er.message); });
      } else { t.dataset.sure = '1'; t.textContent = 'Delete for everyone?'; }
      return;
    } else if (t.dataset.photo) {
      openPhoto(t.dataset.photo);
    } else if (t.dataset.jump) {
      jumpTo(t.dataset.jump);
    }
    if (msgEl) msgEl.classList.remove('tools-on');
  });
  // double-click a message: who has seen it, and when
  $('ch-msgs').addEventListener('dblclick', function (e) {
    var el = e.target.closest('.ch-msg');
    if (!el || e.target.closest('button,a,textarea,.ch-edbox') || phone()) return;
    try { window.getSelection().removeAllRanges(); } catch (er) {}
    openSeen(el.dataset.mid);
  });
  $('ch-msgs').addEventListener('keydown', function (e) {
    if (e.target.id !== 'ch-edta') return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelEdit(); }
    else if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !phone()) { e.preventDefault(); saveEdit(); }
  });
  $('ch-msgs').addEventListener('input', function (e) { if (e.target.id === 'ch-edta' && S.edit) { S.edit.draft = e.target.value; growEd(e.target); } });
  $('ch-pingo').addEventListener('click', function () { if (S.detail && S.detail.pin) jumpTo(S.detail.pin.id); });
  $('ch-pinx').addEventListener('click', function () { pin(null); });

  // ---- seen by ----
  function openSeen(id) {
    id = +id;
    var m = S.msgs.filter(function (x) { return x.id === id; })[0];
    if (!m || m.deleted || S.me.worker) return;
    $('ch-stl').textContent = 'Seen by'; $('ch-slead').textContent = '';
    $('ch-sq').textContent = (m.mine ? 'You' : m.name) + ': ' + (m.body || (m.files && m.files.length ? 'Attachment' : ''));
    $('ch-slist').innerHTML = '<p class="ch-empty">Loading…</p>';
    $('ch-seenbg').hidden = false;
    api('/messages/' + id + '/info').then(function (d) {
      $('ch-stl').textContent = d.total ? 'Seen by ' + d.seen.length + ' of ' + d.total : 'Seen by';
      $('ch-slead').textContent = 'Sent ' + stamp(d.sent_at) + (d.edited_at ? ' · edited ' + stamp(d.edited_at) : '');
      var row = function (p, at, no) {
        return '<div class="ch-seenrow' + (no ? ' no' : '') + '">' + avatar(p.key, p.name) + '<div><div class="nm">' + esc(p.key === S.me.key ? p.name + ' (you)' : p.name) + '</div><div class="rl">' + esc(p.role || '') + (p.field ? ' · worker app' : '') + '</div></div>' +
          '<span class="tm">' + (no ? '—' : at ? esc(stamp(at)) : 'Seen') + '</span></div>';
      };
      var h = '';
      if (d.seen.length) h += '<div class="ch-seenlbl">Seen</div>' + d.seen.map(function (p) { return row(p, p.at); }).join('');
      if (d.waiting.length) h += '<div class="ch-seenlbl">Not seen yet</div>' + d.waiting.map(function (p) { return row(p, null, true); }).join('');
      $('ch-slist').innerHTML = h || '<p class="ch-empty">Nobody else is in this chat yet.</p>';
    }).catch(function (er) { $('ch-slist').innerHTML = '<p class="ch-empty">' + esc(er.message) + '</p>'; });
  }
  $('ch-sclose2').addEventListener('click', function () { $('ch-seenbg').hidden = true; });
  function openPhoto(id) {
    $('ch-limg').src = '/api/chat/files/' + id; $('ch-lopen').href = '/api/chat/files/' + id; $('ch-ldl').href = '/api/chat/files/' + id + '?download=1';
    $('ch-light').hidden = false;
  }
  $('ch-lclose').addEventListener('click', function () { $('ch-light').hidden = true; $('ch-limg').removeAttribute('src'); });
  $('ch-light').addEventListener('click', function (e) { if (e.target.classList.contains('img')) $('ch-lclose').click(); });

  // ---- files & people panel ----
  function renderSide() {
    var c = S.detail; if (!c) return;
    $('ch-stitle').textContent = c.kind === 'channel' ? '# ' + c.title : c.title;
    Array.prototype.forEach.call($('ch-stabs').children, function (b) { b.classList.toggle('on', b.dataset.p === S.side); });
    var body = $('ch-sbody');
    if (S.side === 'people') {
      var h = c.everyone ? '<p class="ch-empty" style="padding:0">Everyone at the company is in this channel.</p>' :
        c.members.map(function (p) { return '<div class="ch-person">' + avatar(p.key, p.name) + '<div><div class="nm">' + esc(p.name) + (p.key === S.me.key ? ' (you)' : '') + '</div><div class="rl">' + esc(p.role || '') + (p.field ? ' · worker app' : '') + '</div></div></div>'; }).join('');
      if (c.can_add) h += '<button type="button" class="btn-ghost" id="ch-addpeople">+ Add people</button>';
      if (c.can_leave) h += '<button type="button" class="btn-ghost" id="ch-leave">Leave channel</button>';
      body.innerHTML = h; return;
    }
    body.innerHTML = '<p class="ch-empty" style="padding:0">Loading…</p>';
    api('/convs/' + c.id + '/files').then(function (fs) {
      var imgs = fs.filter(function (f) { return f.kind === 'image'; }), docs = fs.filter(function (f) { return f.kind !== 'image'; }), h = '';
      if (imgs.length) h += '<div class="ch-sec" style="padding-left:0">Photos · ' + imgs.length + '</div><div class="ch-pgrid">' + imgs.map(function (f) { return '<button type="button" data-photo="' + f.id + '" aria-label="Open photo"><img loading="lazy" src="' + fileUrl(f, true) + '" alt=""></button>'; }).join('') + '</div>';
      if (docs.length) h += '<div class="ch-sec" style="padding-left:0">Files · ' + docs.length + '</div>' + attHTML(docs);
      body.innerHTML = h || '<p class="ch-empty" style="padding:0">No files yet. Photos and documents sent in this chat collect here.</p>';
    }).catch(function (e) { body.innerHTML = '<p class="ch-empty">' + esc(e.message) + '</p>'; });
  }
  $('ch-sbody').addEventListener('click', function (e) {
    var t = e.target.closest('button'); if (!t) return;
    if (t.dataset.photo) openPhoto(t.dataset.photo);
    else if (t.id === 'ch-addpeople') openAdd();
    else if (t.id === 'ch-leave') {
      if (!t.dataset.sure) { t.dataset.sure = '1'; t.textContent = 'Leave? Tap again'; return; }
      api('/convs/' + S.cur + '/leave', { method: 'POST', body: {} }).then(function () { toast('You left the channel'); closeThread(); loadConvs(); }).catch(function (er) { toast(er.message); });
    }
  });
  $('ch-filesbtn').addEventListener('click', function () { var hid = $('ch-side').hidden; $('ch-side').hidden = !hid || S.side !== 'files'; S.side = 'files'; if (!$('ch-side').hidden) renderSide(); });
  $('ch-membtn').addEventListener('click', function () { $('ch-side').hidden = false; S.side = 'people'; renderSide(); });
  $('ch-sclose').addEventListener('click', function () { $('ch-side').hidden = true; });
  $('ch-stabs').addEventListener('click', function (e) { var b = e.target.closest('[data-p]'); if (b) { S.side = b.dataset.p; renderSide(); } });

  // ---- people pickers (new chat / add people) ----
  function pickHTML(list, picked, q) {
    q = (q || '').toLowerCase();
    var rows = list.filter(function (p) { return p.key !== S.me.key && (p.name + ' ' + p.role).toLowerCase().indexOf(q) !== -1; });
    return rows.length ? rows.map(function (p) { var on = picked[p.key]; return '<button type="button" class="ch-pk' + (on ? ' sel' : '') + '" data-k="' + esc(p.key) + '">' + avatar(p.key, p.name) + '<div><div class="nm">' + esc(p.name) + '</div><div class="rl">' + esc(p.role) + (p.field ? ' · field' : '') + '</div></div><span class="bx">' + (on ? '✓' : '') + '</span></button>'; }).join('')
      : '<p class="ch-empty">Nobody matches that.</p>';
  }
  var N = { mode: 'dm', picked: {} };
  function openNew(mode) {
    N.mode = mode || 'dm'; N.picked = {};
    $('ch-nq').value = ''; $('ch-nname').value = ''; $('ch-ntopic').value = ''; $('ch-npriv').checked = false; $('ch-nerr').hidden = true;
    $('ch-nsegch').hidden = !S.me.can_channel;
    $('ch-newbg').hidden = false;
    ensurePeople().then(function () { setNewMode(); setTimeout(function () { $(N.mode === 'channel' ? 'ch-nname' : 'ch-nq').focus(); }, 30); });
  }
  function setNewMode() {
    var ch = N.mode === 'channel';
    Array.prototype.forEach.call($('ch-nseg').children, function (b) { b.classList.toggle('on', b.dataset.m === N.mode); });
    $('ch-nnamel').hidden = $('ch-ntopicl').hidden = $('ch-nprivrow').hidden = !ch;
    $('ch-ntitle').textContent = ch ? 'New channel' : 'New message';
    $('ch-nlead').textContent = ch ? 'A shared room for a team or a topic. Add people now or later.' : 'Pick one person for a private chat, or several for a group.';
    renderNewPick();
  }
  function renderNewPick() {
    $('ch-npick').innerHTML = pickHTML(S.people || [], N.picked, $('ch-nq').value);
    var n = Object.keys(N.picked).length, ch = N.mode === 'channel';
    $('ch-ngo').disabled = ch ? $('ch-nname').value.trim().length < 2 : !n;
    $('ch-ngo').textContent = ch ? 'Create channel' : n > 1 ? 'Start group (' + (n + 1) + ' people)' : 'Start chat';
  }
  $('ch-nseg').addEventListener('click', function (e) { var b = e.target.closest('[data-m]'); if (b) { N.mode = b.dataset.m; setNewMode(); } });
  $('ch-nq').addEventListener('input', renderNewPick);
  $('ch-nname').addEventListener('input', renderNewPick);
  $('ch-npick').addEventListener('click', function (e) { var b = e.target.closest('[data-k]'); if (!b) return; if (N.picked[b.dataset.k]) delete N.picked[b.dataset.k]; else N.picked[b.dataset.k] = 1; renderNewPick(); });
  $('ch-ncancel').addEventListener('click', function () { $('ch-newbg').hidden = true; });
  $('ch-ngo').addEventListener('click', function () {
    var b = N.mode === 'channel' ? { kind: 'channel', name: $('ch-nname').value, topic: $('ch-ntopic').value, private: $('ch-npriv').checked, members: Object.keys(N.picked) }
      : { kind: 'dm', members: Object.keys(N.picked) };
    $('ch-ngo').disabled = true;
    api('/convs', { method: 'POST', body: b }).then(function (d) {
      $('ch-newbg').hidden = true;
      if (b.kind === 'channel') toast('Channel created');
      return loadConvs().then(function () { open(d.id); });
    }).catch(function (e) { $('ch-nerr').textContent = e.message; $('ch-nerr').hidden = false; renderNewPick(); });
  });
  $('ch-new').addEventListener('click', function () { openNew('dm'); });
  $('ch-convs').addEventListener('click', function (e) {
    var a = e.target.closest('[data-add]'); if (a) { openNew(a.dataset.add === 'channel' ? 'channel' : 'dm'); return; }
    var c = e.target.closest('.ch-conv'); if (c) open(c.dataset.id);
  });

  var A = { picked: {} };
  function openAdd() {
    A.picked = {}; $('ch-aq').value = ''; $('ch-aerr').hidden = true; $('ch-addbg').hidden = false;
    ensurePeople().then(renderAddPick);
  }
  function renderAddPick() {
    var have = {}; (S.detail.members || []).forEach(function (p) { have[p.key] = 1; });
    $('ch-apick').innerHTML = pickHTML((S.people || []).filter(function (p) { return !have[p.key]; }), A.picked, $('ch-aq').value);
    var n = Object.keys(A.picked).length; $('ch-ago').disabled = !n; $('ch-ago').textContent = n ? 'Add ' + n : 'Add';
  }
  $('ch-aq').addEventListener('input', renderAddPick);
  $('ch-apick').addEventListener('click', function (e) { var b = e.target.closest('[data-k]'); if (!b) return; if (A.picked[b.dataset.k]) delete A.picked[b.dataset.k]; else A.picked[b.dataset.k] = 1; renderAddPick(); });
  $('ch-acancel').addEventListener('click', function () { $('ch-addbg').hidden = true; });
  $('ch-ago').addEventListener('click', function () {
    api('/convs/' + S.cur + '/members', { method: 'POST', body: { members: Object.keys(A.picked) } }).then(function (d) {
      $('ch-addbg').hidden = true; toast(d.added === 1 ? '1 person added' : d.added + ' people added'); loadThread(true);
    }).catch(function (e) { $('ch-aerr').textContent = e.message; $('ch-aerr').hidden = false; });
  });

  // ---- make a task from a message ----
  var K = { msg: null };
  function openTask(m) {
    K.msg = m; $('ch-kerr').hidden = true;
    $('ch-ktask').value = (m.body || (m.files && m.files[0] ? 'Follow up: ' + m.files[0].name : 'Follow up')).replace(/\s+/g, ' ').slice(0, 120);
    ensurePeople().then(function (ps) {
      var staff = ps.filter(function (p) { return p.key.indexOf('u:') === 0 && p.role !== 'Estimator'; });
      if (!staff.some(function (p) { return p.key === S.me.key; })) staff.unshift({ key: S.me.key, name: S.me.name });
      $('ch-kwho').innerHTML = staff.map(function (p) { return '<option value="' + esc(p.key.slice(2)) + '"' + (p.key === S.me.key ? ' selected' : '') + '>' + esc(p.name) + (p.key === S.me.key ? ' (you)' : '') + '</option>'; }).join('');
      var due = $('ch-kdue');
      if (window.SSCDatePicker) { window.SSCDatePicker.wire(due); window.SSCDatePicker.setISO(due, ''); } else due.value = '';
      $('ch-taskbg').hidden = false; $('ch-ktask').focus();
    });
  }
  $('ch-kcancel').addEventListener('click', function () { $('ch-taskbg').hidden = true; });
  $('ch-kgo').addEventListener('click', function () {
    var due = $('ch-kdue'), iso = window.SSCDatePicker ? window.SSCDatePicker.getISO(due) : due.value;
    $('ch-kgo').disabled = true;
    api('/messages/' + K.msg.id + '/task', { method: 'POST', body: { title: $('ch-ktask').value, assignee_user_id: +$('ch-kwho').value, due_date: iso || null } })
      .then(function () { $('ch-taskbg').hidden = true; toast('Task made. It is in the Inbox calendar of the person assigned.'); })
      .catch(function (e) { $('ch-kerr').textContent = e.message; $('ch-kerr').hidden = false; })
      .then(function () { $('ch-kgo').disabled = false; });
  });

  // ---- alerts for this chat (the bell): all messages / mentions only / mute ----
  var BELL = { all: 'All messages', mentions: 'Mentions only', off: 'Muted' };
  function renderBell(mode) {
    var b = $('ch-bell'); b.className = 'ch-bell ' + mode; $('ch-belllbl').textContent = BELL[mode] || BELL.mentions;
    Array.prototype.forEach.call($('ch-bellmenu').querySelectorAll('[data-n]'), function (x) {
      var on = x.dataset.n === mode; x.querySelector('.ck').textContent = on ? '✓' : ''; x.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }
  $('ch-bell').addEventListener('click', function (e) { e.stopPropagation(); var m = $('ch-bellmenu'); m.hidden = !m.hidden; this.setAttribute('aria-expanded', String(!m.hidden)); });
  document.addEventListener('click', function (e) { if (!e.target.closest('.ch-bellw')) { $('ch-bellmenu').hidden = true; $('ch-bell').setAttribute('aria-expanded', 'false'); } });
  $('ch-bellmenu').addEventListener('click', function (e) {
    var b = e.target.closest('[data-n]'); if (!b || !S.detail) return;
    var mode = b.dataset.n; $('ch-bellmenu').hidden = true;
    api('/convs/' + S.cur + '/notify', { method: 'POST', body: { mode: mode } }).then(function () {
      S.detail.notify = mode; renderBell(mode); loadConvs();
      toast(mode === 'all' ? 'Every new message in this chat will pop up' : mode === 'off' ? 'Chat muted: no alerts and no count' : 'Alerts only when someone @mentions you');
    }).catch(function (er) { toast(er.message); });
  });

  // ---- misc ----
  function toast(t) { var el = $('ch-toast'); el.textContent = t; el.hidden = false; clearTimeout(toast.h); toast.h = setTimeout(function () { el.hidden = true; }, 3200); }
  function undoToast(t, undo) {
    var el = $('ch-toast'); el.textContent = t + ' · ';
    var b = document.createElement('button'); b.type = 'button'; b.textContent = 'Undo';
    b.addEventListener('click', function () { el.hidden = true; undo(); });
    el.appendChild(b); el.hidden = false; clearTimeout(toast.h); toast.h = setTimeout(function () { el.hidden = true; }, 6000);
  }
  $('ch-back').addEventListener('click', closeThread);
  $('ch-filters').addEventListener('click', function (e) { var b = e.target.closest('.ch-chip'); if (!b) return; S.filter = b.dataset.f; Array.prototype.forEach.call(this.children, function (x) { x.classList.toggle('on', x === b); }); renderList(); });
  $('ch-q').addEventListener('input', renderList);
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var dlg = ['ch-newbg', 'ch-addbg', 'ch-taskbg', 'ch-seenbg'].some(function (id) { return !$(id).hidden; }) || !$('ch-light').hidden;
    ['ch-newbg', 'ch-addbg', 'ch-taskbg', 'ch-seenbg'].forEach(function (id) { $(id).hidden = true; });
    $('ch-bellmenu').hidden = true;
    if (!$('ch-light').hidden) $('ch-lclose').click();
    $('ch-mpop').hidden = true;
    if (!dlg && W && W.isOpen() && !S.edit) W.escape();   // the window: Esc with nothing else open minimizes it
  });
  ['ch-newbg', 'ch-addbg', 'ch-taskbg', 'ch-seenbg'].forEach(function (id) { $(id).addEventListener('click', function (e) { if (e.target === this) this.hidden = true; }); });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { tick(); markRead(); } });
  window.addEventListener('focus', markRead);
  if (!W) window.addEventListener('hashchange', function () { var m = /^#c(\d+)$/.exec(location.hash); if (m && +m[1] !== S.cur) open(m[1]); });
  layout();
  if (!W) window.matchMedia('(max-width:860px)').addEventListener('change', layout);

  // what the floating window (chat_widget.js) and the alerts (chat_alerts.js) call
  window.SSC_CHAT = {
    open: function (id) { return (S.me ? Promise.resolve() : ready).then(function () { return open(id); }); },
    current: function () { return S.cur; },
    convs: function () { return S.convs; },
    wake: function () { S.rev = -1; loadConvs(); if (S.cur) loadThread(true); tick(); },
    focus: function () { if (S.cur && !phone()) ta.focus(); }
  };

  var ready = api('/me').then(function (me) {
    S.me = me;
    return loadConvs();
  });
  ready.then(function () {
    if (W) {
      var id = W.start(S.convs);
      if (id) open(id, { noFocus: phone() });
    } else {
      var m = /^#c(\d+)$/.exec(location.hash);
      if (m) open(m[1], { noFocus: true });
      else if (wide() && S.convs.length) open(S.convs[0].id, { noFocus: true });
    }
    tick(); setInterval(tick, 4000); setInterval(function () { if (awake()) loadConvs(); }, 60000);
  }).catch(function (e) { $('ch-convs').innerHTML = '<p class="ch-empty">' + esc(e.message) + '</p>'; });
})();
