/* Daily Report as a step-by-step checklist — PHONE WIDTH ONLY (<= 860px).
 *
 * The Daily Report entry form (#dcr-entry-view in dashboard-static.html) is one long page of 11 sections. On a
 * phone this file re-presents it, without touching its logic: a checklist home, one step at a time, one sticky
 * "Save & continue" button, then a review screen that reveals the form's own Issue button. Every section keeps its
 * markup, its row-save logic and its Issue / Cancel handlers; this only decides what is visible.
 * Desktop widths never get the checklist (the full form is unchanged).
 *
 *   auto steps  (Project info, Weather, Crew)  start done: the app already filled them in
 *   required    Work done today, Safety        must be visited with "Save & continue" before Issue unlocks
 *                                              (content is NOT validated: a quiet day can continue with nothing logged)
 *   optional    Deliveries & equipment, Issues & delays, Inspections & visitors, Photos
 *   No-Work day: Issue is always unlocked.
 * Rows are saved one by one by the page itself; "Save & continue" clicks each filled row's own Save first, and drops
 * empty unsaved rows so the page's "unsaved rows" prompt never fires.
 */
(function () {
  'use strict';
  var MQ = window.matchMedia('(max-width: 860px)');
  var entry, blocks, built = false, view = 'list', cur = -1, visited = {}, saving = false;

  // blocks: indexes into the entry view's .dcr-block list (1 Project Info ... 11 Photos)
  var STEPS = [
    { k: 'info', t: 'Project info', b: [0], auto: true, hint: 'Filled in for you' },
    { k: 'weather', t: 'Weather', b: [1], auto: true, hint: 'From the forecast' },
    { k: 'crew', t: 'Crew on site', b: [2], auto: true, hint: 'From today’s sign-ins' },
    { k: 'work', t: 'Work done today', b: [3], req: true, hint: 'What did the crew do?', addFirst: 'work' },
    { k: 'deliv', t: 'Deliveries & equipment', b: [4, 5], hint: 'Materials and equipment that arrived' },
    { k: 'safety', t: 'Safety', b: [6], req: true, hint: 'Toolbox talk, incidents, near misses' },
    { k: 'issues', t: 'Issues & delays', b: [7], hint: 'Anything that slowed the work' },
    { k: 'visits', t: 'Inspections & visitors', b: [8, 9], hint: 'Who came by, what was inspected' },
    { k: 'photos', t: 'Photos', b: [10], hint: 'Add today’s photos' }
  ];

  var CSS = '' +
    '#dcr-entry-view.ds-on .dcr-block{display:none}' +
    '#dcr-entry-view.ds-on .dcr-block.ds-show{display:block}' +
    // the controls bar and the action row carry inline display styles, so these rules need !important
    '#dcr-entry-view.ds-on #dcr-number-preview,#dcr-entry-view.ds-on .ds-actions{display:none!important}' +
    '#dcr-entry-view.ds-on[data-ds-view="review"] #dcr-number-preview{display:block!important}' +
    '#dcr-entry-view.ds-on[data-ds-view="review"] .ds-actions{display:flex!important}' +
    '#dcr-entry-view.ds-on:not([data-ds-view="list"]) .ds-controls{display:none!important}' +
    '#dcr-entry-view.ds-on[data-ds-view="step"]>h2,#dcr-entry-view.ds-on[data-ds-view="review"]>h2{display:none}' +
    '#dcr-entry-view.ds-on{padding-bottom:104px}' +
    '#dcr-entry-view .ds-list,#dcr-entry-view .ds-head,#dcr-entry-view .ds-bar,#dcr-entry-view .ds-rev{display:none}' +
    '#dcr-entry-view.ds-on[data-ds-view="list"] .ds-list{display:block}' +
    '#dcr-entry-view.ds-on[data-ds-view="step"] .ds-head,#dcr-entry-view.ds-on[data-ds-view="review"] .ds-head{display:block}' +
    '#dcr-entry-view.ds-on[data-ds-view="review"] .ds-rev{display:block}' +
    '#dcr-entry-view.ds-on .ds-bar{display:flex}' +
    '.ds-prog{height:6px;background:var(--line,#eceef3);border-radius:99px;margin:10px 0 4px;overflow:hidden}' +
    '.ds-prog i{display:block;height:100%;background:var(--accent,#4364dc)}' +
    '.ds-pt{font-size:13px;color:var(--muted,#8c92a0)}' +
    '.ds-steps{display:grid;gap:8px;margin:12px 0 0}' +
    '.ds-step{display:flex;gap:12px;align-items:center;min-height:56px;padding:0 14px;border:1px solid var(--line,#eceef3);border-radius:12px;background:#fff;width:100%;text-align:left;font:inherit;color:inherit;cursor:pointer}' +
    '.ds-n{flex:none;width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font-size:13px;font-weight:600;background:#f0f1f4;color:var(--muted,#8c92a0)}' +
    '.ds-step.ds-done .ds-n{background:#e4f4ec;color:#1f8a5b}' +
    '.ds-tx{flex:1;min-width:0}.ds-tx b{display:block;font-weight:600;font-size:14px}.ds-tx small{display:block;color:var(--muted,#8c92a0);font-size:13px}' +
    '.ds-tag{flex:none;font-size:12px;font-weight:500;padding:2px 9px;border-radius:99px;background:var(--accent-soft,#ebf0fe);color:var(--accent,#4364dc)}' +
    '.ds-tag.ds-opt{background:#f0f1f4;color:var(--muted,#8c92a0)}' +
    '.ds-go{flex:none;color:var(--muted,#8c92a0)}' +
    '.ds-back{border:0;background:none;color:var(--accent,#4364dc);font:600 14px inherit;min-height:44px;padding:0;cursor:pointer}' +
    '.ds-ttl{font-size:22px;font-weight:500;letter-spacing:.04em;text-transform:uppercase;margin:2px 0}' +
    '.ds-sub{font-size:14px;color:var(--muted,#8c92a0);margin-bottom:10px}' +
    '.ds-rev .ds-kv{display:flex;justify-content:space-between;gap:10px;padding:11px 0;border-top:1px solid var(--line,#eceef3);font-size:14px}' +
    '.ds-rev .ds-kv:first-child{border-top:0}' +
    '.ds-bar{position:fixed;left:0;right:0;bottom:0;z-index:50;gap:10px;align-items:center;background:#fff;border-top:1px solid var(--line,#eceef3);padding:10px 16px calc(10px + env(safe-area-inset-bottom,0px))}' +
    'body.jv2 .ds-bar{bottom:56px}' +
    '.ds-btn{flex:1;min-height:48px;border:0;border-radius:10px;background:var(--accent,#4364dc);color:#fff;font:600 14px inherit;cursor:pointer}' +
    '.ds-btn.ds-ghost{flex:0 0 auto;padding:0 18px;background:#fff;color:var(--ink,#222633);border:1px solid var(--line,#eceef3)}' +
    '.ds-btn[disabled]{background:#b9c3ee;cursor:not-allowed}' +
    '.ds-msg{flex:1 1 100%;font-size:13px;color:var(--muted,#8c92a0)}' +
    'body.ds-active #clx-fab{display:none!important}';

  function $(s, r) { return (r || document).querySelector(s); }
  function el(tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }

  function rowsIn(step) {
    var out = [];
    step.b.forEach(function (i) { if (blocks[i]) [].forEach.call(blocks[i].querySelectorAll('.dcr-row'), function (r) { out.push(r); }); });
    return out;
  }
  function hasContent(step) {
    if (step.k === 'photos') { var g = $('#dcr-photo-grid'); return !!(g && g.children.length); }
    if (step.auto) return true;
    return rowsIn(step).length > 0;
  }
  function isDone(step) { return step.auto || visited[step.k] || (step.req && hasContent(step)) || (!step.req && hasContent(step)); }
  function noWork() { var b = $('#dcr-nowork-banner'); return !!b && b.style.display !== 'none' && getComputedStyle(b).display !== 'none'; }
  function ready() { return noWork() || STEPS.every(function (s) { return !s.req || isDone(s); }); }
  function sub(step) {
    if (step.k === 'crew') { var n = [].filter.call(document.querySelectorAll('#dcr-labor-tbody tr'), function (r) { return r.children.length > 1; }).length; return n ? n + ' signed in' : 'No sign-ins yet'; }
    if (step.k === 'info') { var d = $('#dcr-date'); return (d && d.value ? d.value + ' · ' : '') + step.hint; }
    if (step.k === 'photos') { var g = $('#dcr-photo-grid'); return g && g.children.length ? g.children.length + ' added' : step.hint; }
    if (!step.auto) { var n2 = rowsIn(step).length; if (n2) return n2 + ' added'; }
    return step.hint;
  }

  function setView(v, i) {
    view = v; if (i != null) cur = i;
    entry.setAttribute('data-ds-view', v);
    blocks.forEach(function (b) { b.classList.remove('ds-show'); });
    if (v === 'step') STEPS[cur].b.forEach(function (bi) { if (blocks[bi]) blocks[bi].classList.add('ds-show'); });
    render();
    window.scrollTo(0, 0);
  }

  function render() {
    var list = $('.ds-list', entry), head = $('.ds-head', entry), bar = $('.ds-bar', entry), rev = $('.ds-rev', entry);
    var done = STEPS.filter(isDone).length, rdy = ready();
    if (view === 'list') {
      var remaining = STEPS.filter(function (s) { return s.req && !isDone(s); }).length;
      list.innerHTML = '<div class="ds-pt" style="margin-top:6px">' + done + ' of ' + STEPS.length + ' done · ' +
        (rdy ? 'ready to issue' : remaining + ' more required') + '</div><div class="ds-prog"><i style="width:' + Math.round(done / STEPS.length * 100) + '%"></i></div><div class="ds-steps">' +
        STEPS.map(function (s, i) {
          var dn = isDone(s);
          return '<button type="button" class="ds-step' + (dn ? ' ds-done' : '') + '" data-ds-open="' + i + '"><span class="ds-n">' + (dn ? '✓' : (i + 1)) + '</span>' +
            '<span class="ds-tx"><b>' + esc(s.t) + '</b><small>' + esc(sub(s)) + '</small></span>' +
            (!dn && !s.req ? '<span class="ds-tag ds-opt">optional</span>' : '') + '<span class="ds-go">›</span></button>';
        }).join('') + '</div>';
      bar.innerHTML = '<button type="button" class="ds-btn" data-ds-review' + (rdy ? '' : ' disabled') + '>' + (rdy ? 'Review &amp; issue report' : 'Finish the required steps to issue') + '</button>';
    } else if (view === 'step') {
      var s2 = STEPS[cur];
      head.innerHTML = '<button type="button" class="ds-back" data-ds-list>‹ All steps</button><div class="ds-ttl">' + esc(s2.t) + '</div><div class="ds-sub">Step ' + (cur + 1) + ' of ' + STEPS.length + '</div>';
      bar.innerHTML = '<div class="ds-msg" data-ds-msg hidden></div>' +
        '<button type="button" class="ds-btn ds-ghost" data-ds-skip>' + (s2.req || s2.auto ? 'Back' : 'Skip') + '</button>' +
        '<button type="button" class="ds-btn" data-ds-next>' + (s2.auto ? 'Looks right' : 'Save &amp; continue') + '</button>';
    } else {
      head.innerHTML = '<button type="button" class="ds-back" data-ds-list>‹ All steps</button><div class="ds-ttl">Review</div><div class="ds-sub">Check, then issue the report below.</div>';
      rev.innerHTML = STEPS.map(function (s) { return '<div class="ds-kv"><span>' + esc(s.t) + '</span><b>' + esc(isDone(s) ? (s.auto ? 'Auto' : 'Done') : 'Skipped') + '</b></div>'; }).join('');
      bar.innerHTML = '<button type="button" class="ds-btn ds-ghost" data-ds-list>Edit steps</button>';
    }
  }

  // click every filled unsaved row's own Save, drop empty unsaved rows, wait for the page to finish saving
  function flush(step, done) {
    var pending = [];
    rowsIn(step).forEach(function (r) {
      if (r.dataset.saved !== '0') return;
      var filled = [].some.call(r.querySelectorAll('input,textarea'), function (e) { return e.type !== 'hidden' && e.value.trim() !== ''; });
      if (!filled) { r.remove(); return; }
      var b = r.querySelector('[data-row-save]'); if (b) { pending.push(r); b.click(); }
    });
    if (!pending.length) return done(true);
    var t0 = Date.now();
    (function wait() {
      var failed = pending.some(function (r) { var e = r.querySelector('[data-row-err]'); return e && getComputedStyle(e).display !== 'none' && e.textContent.trim(); });
      var open = pending.some(function (r) { return r.dataset.saved === '0'; });
      if (failed) return done(false);
      if (!open) return done(true);
      if (Date.now() - t0 > 8000) return done(false);
      setTimeout(wait, 150);
    })();
  }

  function nextUndone(from) {
    for (var i = from + 1; i < STEPS.length; i++) if (!isDone(STEPS[i])) return i;
    return -1;
  }

  function onClick(e) {
    var t = e.target.closest('[data-ds-open],[data-ds-list],[data-ds-review],[data-ds-skip],[data-ds-next]');
    if (!t || !entry.contains(t)) return;
    if (t.hasAttribute('data-ds-open')) {
      var i = +t.getAttribute('data-ds-open'); setView('step', i);
      var s = STEPS[i];
      if (s.addFirst && !rowsIn(s).length) { var add = $('[data-dcr-add="' + s.addFirst + '"]', entry); if (add) add.click(); }
    } else if (t.hasAttribute('data-ds-list')) { setView('list'); }
    else if (t.hasAttribute('data-ds-review')) { if (ready()) setView('review'); }
    else if (t.hasAttribute('data-ds-skip')) {
      var s3 = STEPS[cur]; if (!s3.req && !s3.auto) visited[s3.k] = true;
      flush(s3, function () { setView('list'); });
    } else if (t.hasAttribute('data-ds-next')) {
      if (saving) return;
      var s4 = STEPS[cur], msg = $('[data-ds-msg]', entry);
      saving = true; t.disabled = true; if (msg) { msg.hidden = false; msg.textContent = 'Saving…'; }
      flush(s4, function (ok) {
        saving = false; t.disabled = false;
        if (!ok) { if (msg) { msg.hidden = false; msg.textContent = 'A row did not save. Fix it above, then try again.'; } return; }
        visited[s4.k] = true;
        var n = nextUndone(cur);
        n > -1 ? (setView('step', n), STEPS[n].addFirst && !rowsIn(STEPS[n]).length && ($('[data-dcr-add="' + STEPS[n].addFirst + '"]', entry) || { click: function () {} }).click()) : setView('list');
      });
    }
  }

  function build() {
    if (built) return;
    entry = document.getElementById('dcr-entry-view'); if (!entry) return;
    blocks = [].slice.call(entry.querySelectorAll(':scope > .dcr-block'));
    if (blocks.length < 11) return;   // the form changed shape: leave the full form alone
    var st = el('style'); st.textContent = CSS; document.head.appendChild(st);
    // controls bar (date, refresh, no-work) is the first plain div after the h2/banners that holds #dcr-date
    var d = $('#dcr-date', entry); var ctl = d ? d.closest('div') : null; if (ctl) ctl.classList.add('ds-controls');
    var np = $('#dcr-number-preview', entry); var act = np ? np.nextElementSibling : null; if (act) act.classList.add('ds-actions');
    var first = blocks[0];
    first.parentNode.insertBefore(el('div', 'ds-list'), first);
    first.parentNode.insertBefore(el('div', 'ds-head'), first);
    var rev = el('div', 'ds-rev'); if (np) np.parentNode.insertBefore(rev, np); else entry.appendChild(rev);
    entry.appendChild(el('div', 'ds-bar'));
    entry.addEventListener('click', onClick);
    built = true;
  }

  function enter() { visited = {}; saving = false; cur = -1; setView('list'); }
  function apply() {
    if (!entry) return;
    var on = MQ.matches && entry.style.display !== 'none';
    entry.classList.toggle('ds-on', MQ.matches);
    document.body.classList.toggle('ds-active', on);
    if (!MQ.matches) blocks.forEach(function (b) { b.classList.remove('ds-show'); });
  }

  function init() {
    build(); if (!built) return;
    var shown = entry.style.display !== 'none';
    new MutationObserver(function () {
      var now = entry.style.display !== 'none';
      if (now && !shown) { enter(); }
      shown = now; apply();
    }).observe(entry, { attributes: true, attributeFilter: ['style'] });
    var rerender = function () { if (MQ.matches && entry.style.display !== 'none') render(); };
    entry.addEventListener('input', rerender); entry.addEventListener('change', rerender);
    // refresh the checklist when rows / photos / sign-ins change (observe only the form's own containers: render() writes
    // inside `entry`, so observing the whole entry view would loop)
    var mo = new MutationObserver(function () { if (view === 'list') rerender(); });
    [].forEach.call(entry.querySelectorAll('[data-dcr-rows],#dcr-photo-grid,#dcr-labor-tbody'), function (c) { mo.observe(c, { childList: true }); });
    (MQ.addEventListener ? MQ.addEventListener.bind(MQ, 'change') : MQ.addListener.bind(MQ))(function () { apply(); if (MQ.matches) enter(); });
    enter(); apply();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
