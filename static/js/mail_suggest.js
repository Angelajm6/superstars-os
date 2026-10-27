/* Gmail-style address suggestions for To / Cc boxes (2026-10-27).
 *
 * The inbox reply already suggests people as you type (company_portal.html ibRecipients). This gives the
 * plain comma-separated To / Cc boxes the same list: New email in the inbox and Proposal Studio's Send pop-up.
 *
 *   SSCMailSuggest.attach(input, { local: () => [{ name, email, where }], group: [toInput, ccInput] })
 *
 *   - suggests for the address being typed (the part between commas around the cursor), from 2 letters
 *   - people come from `local` (e.g. the proposal's client contacts) first, then /api/inbox/people
 *     (contacts, your team, your own recent email); if that answers 403 for this role, `local` alone is used
 *   - ↑ / ↓ move, Enter or Tab picks, Esc closes; a pick writes "Name <email>, " so the next one can follow
 *   - addresses already on the email (this box or another in `group`) are not suggested again
 *   - the list sits on the page root (fixed under the box), so a scrolling pop-up never clips it
 */
(function () {
  if (window.SSCMailSuggest) return;
  var css = document.createElement('style');
  css.textContent = '.ssc-ms{position:fixed;z-index:10050;background:#fff;border-radius:10px;box-shadow:0 2px 6px rgba(60,64,67,.3),0 6px 16px rgba(60,64,67,.15);' +
    'padding:6px 0;max-height:280px;overflow:auto;font:13px Inter,-apple-system,sans-serif}.ssc-ms[hidden]{display:none}' +
    '.ssc-ms button{display:flex;gap:10px;align-items:center;width:100%;text-align:left;border:0;background:none;padding:8px 12px;min-height:44px;font:inherit;color:#14161C;cursor:pointer}' +
    '.ssc-ms button:hover,.ssc-ms button.on{background:#F2F2F2}.ssc-ms .av{width:24px;height:24px;border-radius:50%;background:#7C8DB5;color:#fff;display:grid;place-items:center;font-size:11px;font-weight:700;flex:none}' +
    '.ssc-ms .tx{min-width:0}.ssc-ms .tx b{font-weight:600}.ssc-ms small{display:block;color:#5E5E5E;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}';
  (document.head || document.documentElement).appendChild(css);

  var box = document.createElement('div');
  box.className = 'ssc-ms'; box.hidden = true; box.setAttribute('role', 'listbox');
  var cur = null, rows = [], at = 0, timer = null, seq = 0, serverOff = false;

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function ini(r) { return ((r.name || r.email).split(/[\s@.]+/).filter(Boolean).map(function (x) { return x[0]; }).join('').slice(0, 2) || '?').toUpperCase(); }
  function token(inp) {   // the address under the cursor: [start, end) between separators
    var v = inp.value, c = inp.selectionStart == null ? v.length : inp.selectionStart;
    var s = Math.max(v.lastIndexOf(',', c - 1), v.lastIndexOf(';', c - 1)) + 1;
    var e = v.slice(c).search(/[,;]/); e = e < 0 ? v.length : c + e;
    return { s: s, e: e, q: v.slice(s, e).trim() };
  }
  function used(inp, t, group) {   // addresses already on the email: this box (minus the one being typed) and its group
    var out = {}, m, re = /[^\s<>,;"]+@[^\s<>,;"]+/g, rest = inp.value.slice(0, t.s) + ' ' + inp.value.slice(t.e);
    (group || []).forEach(function (g) { if (g && g !== inp) rest += ' ' + g.value; });
    while ((m = re.exec(rest))) out[m[0].toLowerCase()] = 1;
    return out;
  }
  function place() {
    if (!cur || box.hidden) return;
    var r = cur.el.getBoundingClientRect(), w = Math.min(Math.max(r.width, 280), innerWidth - 16);
    box.style.width = w + 'px';
    box.style.left = Math.max(8, Math.min(r.left, innerWidth - w - 8)) + 'px';
    var below = innerHeight - r.bottom - 8;
    if (below < 160 && r.top > below) { box.style.top = ''; box.style.bottom = (innerHeight - r.top + 4) + 'px'; }
    else { box.style.bottom = ''; box.style.top = (r.bottom + 4) + 'px'; }
  }
  function close() { box.hidden = true; rows = []; }
  function draw() {
    if (!rows.length) { close(); return; }
    if (!box.parentNode) document.body.appendChild(box);
    box.innerHTML = rows.map(function (r, i) {
      return '<button type="button" role="option" data-i="' + i + '"' + (i === at ? ' class="on" aria-selected="true"' : '') + '><span class="av">' + esc(ini(r)) + '</span>' +
        '<span class="tx"><b>' + esc(r.name || r.email) + '</b><small>' + esc([r.name ? r.email : '', r.where].filter(Boolean).join(' · ')) + '</small></span></button>';
    }).join('');
    box.hidden = false; place();
  }
  function pick(i) {
    var r = rows[i]; if (!cur || !r) return;
    var inp = cur.el, t = token(inp), v = inp.value;
    var who = r.name ? r.name.replace(/[",<>]/g, '') + ' <' + r.email + '>' : r.email;
    var head = v.slice(0, t.s).replace(/\s*$/, ''), tail = v.slice(t.e).replace(/^[,;\s]*/, '');
    var text = (head ? head + ' ' : '') + who + ', ';
    inp.value = text + tail;
    inp.setSelectionRange(text.length, text.length);
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    close();
  }
  function search(inp, opts) {
    var t = token(inp), q = t.q.toLowerCase(), mine = ++seq;
    clearTimeout(timer);
    if (q.length < 2) { close(); return; }
    var skip = used(inp, t, opts.group), seen = {}, list = [];
    function add(r) {
      var e = (r && r.email || '').trim(), k = e.toLowerCase();
      if (!e || seen[k] || skip[k]) return;
      if ((r.name || '').toLowerCase().indexOf(q) < 0 && k.indexOf(q) < 0) return;
      seen[k] = 1; list.push({ name: (r.name || '').trim(), email: e, where: r.where || '' });
    }
    ((opts.local && opts.local()) || []).forEach(add);
    rows = list.slice(0, 6); at = 0; draw();
    if (serverOff) return;
    timer = setTimeout(function () {
      fetch('/api/inbox/people?q=' + encodeURIComponent(t.q), { credentials: 'same-origin', headers: { Accept: 'application/json' } })
        .then(function (res) { if (res.status === 403) serverOff = true; return res.ok ? res.json() : { data: [] }; })
        .then(function (j) { if (mine !== seq || !cur || cur.el !== inp) return; (j.data || []).forEach(add); rows = list.slice(0, 6); if (at >= rows.length) at = 0; draw(); })
        .catch(function () {});
    }, 180);
  }

  box.addEventListener('pointerdown', function (e) { e.preventDefault(); });   // keep focus (and the keyboard) in the box
  box.addEventListener('click', function (e) { var b = e.target.closest('[data-i]'); if (b) pick(+b.dataset.i); });
  addEventListener('resize', place);
  addEventListener('scroll', place, true);

  window.SSCMailSuggest = {
    attach: function (inp, opts) {
      if (!inp || inp.dataset.msOn) return;
      inp.dataset.msOn = '1'; opts = opts || {};
      inp.removeAttribute('list');   // the browser's own list stops after the first comma; this one replaces it
      inp.setAttribute('autocomplete', 'off');
      inp.addEventListener('focus', function () { cur = { el: inp }; });
      inp.addEventListener('input', function (e) { cur = { el: inp }; if (e.isTrusted) search(inp, opts); });
      inp.addEventListener('keydown', function (e) {
        if (box.hidden || !rows.length || !cur || cur.el !== inp) return;
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); at = (at + (e.key === 'ArrowDown' ? 1 : rows.length - 1)) % rows.length; draw(); }
        else if (e.key === 'Enter' || e.key === 'Tab' && token(inp).q) { e.preventDefault(); e.stopPropagation(); pick(at); }
        else if (e.key === 'Escape') { e.stopPropagation(); close(); }
      });
      inp.addEventListener('blur', function () { setTimeout(function () { if (document.activeElement !== inp) close(); }, 120); });
    }
  };
})();
