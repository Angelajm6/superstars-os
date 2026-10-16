/* Materials & Deliveries — field view, PHONE WIDTH ONLY (<= 860px).
 *
 * Re-presents the existing Materials screen (section[data-view="materials"] in dashboard-static.html) for the crew
 * without touching its logic: the screen's own buttons, modals, save handlers and data do all the work.
 *   * three big actions on top: "A truck arrived" (log a delivery), "We picked it up" (self-pickup), "Weekly count"
 *   * Expected deliveries ("Coming") move up, with a big Received button on each row (the row's own "mark received")
 *   * the 9-column catalog table becomes an "On site now" list read from that table: on hand, days left when a usage
 *     rate is set (blank otherwise, as the table does), and the same Order now / Order by / Stocked chip
 *   * the log modal becomes three steps: what came · how much (with − / + buttons) · packing slip, then the modal's own Save
 *   * office actions (add material, transfer, expected delivery, full table) fold into "Office tools"
 * Desktop widths are unchanged.
 */
(function () {
  'use strict';
  var sec, wrap, built = false, step = 1;

  var CSS = '' +
    '@media (max-width: 860px){' +
      'section[data-view="materials"] .mt-wrap{display:flex;flex-direction:column}' +
      'section[data-view="materials"] .mt-head{order:0;margin-bottom:6px}' +
      'section[data-view="materials"] .mt-head .mt-actions{display:none}' +
      '.mtf-acts{order:1;display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:4px 0 14px}' +
      '.mtf-act{min-height:72px;border:1px solid var(--line,#eceef3);border-radius:14px;background:#fff;font:inherit;color:inherit;text-align:left;padding:12px 14px;cursor:pointer}' +
      '.mtf-act b{display:block;font-weight:600;font-size:15px}.mtf-act small{display:block;color:var(--muted,#8c92a0);font-size:13px}' +
      '.mtf-act.mtf-pri{grid-column:1/-1;background:var(--accent,#4364dc);border-color:var(--accent,#4364dc);color:#fff}.mtf-act.mtf-pri small{color:#dfe6fc}' +
      'section[data-view="materials"] #mt-expected-panel{order:2}' +
      'section[data-view="materials"] #mt-count-panel{order:3}' +
      '.mtf-onsite{order:4}' +
      'section[data-view="materials"] #mt-pinned{display:none}' +
      'section[data-view="materials"] #mt-catalog{order:5}' +
      'section[data-view="materials"]:not(.mtf-full) #mt-catalog .mt-table{display:none}' +
      '.mtf-office{order:6;margin:4px 0 14px}' +
      '.mtf-activity{order:7}' +
      '#mt-expected .mt-row{flex-wrap:wrap;padding:12px 14px;font-size:14px}' +
      '#mt-expected .mt-row .det{flex:1 1 100%}' +
      '#mt-expected [data-exrecv]{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 16px;border-radius:10px;background:var(--accent,#4364dc);color:#fff!important;font-weight:600;text-decoration:none;margin-left:auto}' +
      '#mt-expected [data-exadv],#mt-expected [data-excancel]{display:inline-flex;align-items:center;min-height:44px}' +
      '.mt-count-row{padding:10px 14px;font-size:14px}.mt-count-row input{max-width:110px;min-height:44px}' +
      '.mtf-h{font-size:16px;font-weight:500;margin:6px 0 8px;display:flex;justify-content:space-between;align-items:baseline}' +
      '.mtf-h small{font-size:13px;color:var(--muted,#8c92a0);font-weight:400}' +
      '.mtf-list{background:#fff;border:1px solid var(--line,#eceef3);border-radius:14px;padding:2px 14px}' +
      '.mtf-row{display:flex;gap:12px;align-items:center;min-height:56px;border-top:1px solid var(--line,#eceef3);padding:8px 0}.mtf-row:first-child{border-top:0}' +
      '.mtf-tx{flex:1;min-width:0}.mtf-tx b{display:block;font-weight:600;font-size:14px}.mtf-tx small{display:block;color:var(--muted,#8c92a0);font-size:13px}' +
      '.mtf-office summary{min-height:44px;display:flex;align-items:center;color:var(--muted,#8c92a0);cursor:pointer;font-size:14px}' +
      '.mtf-office button{display:block;width:100%;text-align:left;min-height:48px;border:0;border-top:1px solid var(--line,#eceef3);background:none;font:inherit;font-size:14px;color:inherit;padding:0 4px;cursor:pointer}' +
      /* log modal in three steps */
      '#mt-modal-log .mtf-prog{font-size:13px;color:var(--muted,#8c92a0);margin:-4px 0 10px}' +
      '#mt-modal-log .mtf-bars{display:flex;gap:6px;margin:0 0 12px}#mt-modal-log .mtf-bars i{flex:1;height:5px;border-radius:99px;background:var(--line,#eceef3)}#mt-modal-log .mtf-bars i.on{background:var(--accent,#4364dc)}' +
      '#mt-modal-log:not(.mtf-s1) [data-mtf="1"],#mt-modal-log:not(.mtf-s2) [data-mtf="2"],#mt-modal-log:not(.mtf-s3) [data-mtf="3"]{display:none!important}' +
      '#mt-modal-log .mtf-nav{display:flex;gap:10px;margin-top:16px}' +
      '#mt-modal-log.mtf-s3 .mtf-nav .mtf-next{display:none}' +
      '#mt-modal-log .mtf-nav button{flex:1;min-height:48px;border-radius:10px;font:600 14px inherit;cursor:pointer;border:1px solid var(--line,#eceef3);background:#fff;color:inherit}' +
      '#mt-modal-log .mtf-nav .mtf-next{background:var(--accent,#4364dc);border-color:var(--accent,#4364dc);color:#fff}' +
      '#mt-modal-log .mt-mfoot .mt-btn-ghost{display:none}' +
      '#mt-modal-log .mt-mfoot .btn-primary{width:100%;min-height:48px}' +
      '#mt-modal-log .mtf-qty{display:flex;align-items:center;gap:8px}#mt-modal-log .mtf-qty input{text-align:center;font-size:18px;font-weight:600;min-height:52px}' +
      '#mt-modal-log .mtf-qty button{flex:none;width:52px;min-height:52px;border:1px solid var(--line,#eceef3);border-radius:12px;background:#fff;font:600 22px inherit;cursor:pointer}' +
      '#mt-modal-log .mt-grid2{grid-template-columns:1fr}' +
      '#mt-modal-log .ssc-field{min-height:48px}' +
      '.mt-overlay{padding:16px 10px}' +
    '}' +
    '.mtf-acts,.mtf-onsite,.mtf-office,#mt-modal-log .mtf-prog,#mt-modal-log .mtf-bars,#mt-modal-log .mtf-nav,#mt-modal-log .mtf-qty>button{display:none}' +
    '@media (max-width: 860px){.mtf-acts{display:grid}.mtf-onsite,.mtf-office{display:block}#mt-modal-log .mtf-prog{display:block}#mt-modal-log .mtf-bars,#mt-modal-log .mtf-nav{display:flex}#mt-modal-log .mtf-qty>button{display:inline-block}' +
      /* the steps only exist on a phone: outside the media query every step stays visible */
    '}';

  var STEP_NAMES = ['What came', 'How much', 'Packing slip'];
  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function txt(el) { return el ? el.textContent.replace(/\s+/g, ' ').trim() : ''; }

  // ---------------------------------------------------------------- home
  function buildHome() {
    var head = wrap.querySelector('.mt-head');
    var acts = document.createElement('div');
    acts.className = 'mtf-acts';
    acts.innerHTML =
      '<button type="button" class="mtf-act mtf-pri" data-mtf-log="delivery"><b>+ A truck arrived</b><small>Log a delivery in 3 steps, snap the packing slip</small></button>' +
      '<button type="button" class="mtf-act" data-mtf-log="pickup"><b>We picked it up</b><small>Self-pickup from a supplier</small></button>' +
      '<button type="button" class="mtf-act" data-mtf-count><b>Weekly count</b><small>Count what is on site</small></button>';
    head.parentNode.insertBefore(acts, head.nextSibling);

    var onsite = document.createElement('div');
    onsite.className = 'mtf-onsite';
    wrap.insertBefore(onsite, $('mt-catalog'));

    var office = document.createElement('details');
    office.className = 'mtf-office';
    office.innerHTML = '<summary>Office tools ›</summary>' +
      '<button type="button" data-mtf-click="mt-btn-add">Add a material to the catalog</button>' +
      '<button type="button" data-mtf-click="mt-btn-transfer">Transfer to another job</button>' +
      '<button type="button" data-mtf-click="mt-btn-expected">Add an expected delivery</button>' +
      '<button type="button" data-mtf-full>Show the full table</button>';
    wrap.insertBefore(office, $('mt-catalog').nextSibling);

    // relabel the expected panel for the crew (its own "+ Expected" stays for the office)
    var ph = $('mt-expected-panel') && $('mt-expected-panel').querySelector('.ph');
    if (ph && ph.firstChild && ph.firstChild.nodeType === 3) ph.firstChild.textContent = 'Coming ';
    var act = $('mt-activity') && $('mt-activity').closest('.mt-panel'); if (act) act.classList.add('mtf-activity');
    renderOnsite();
  }

  // the "On site now" list is read from the catalog table the screen already renders
  function renderOnsite() {
    var host = wrap.querySelector('.mtf-onsite'); if (!host) return;
    var rows = [].slice.call($('mt-catalog').querySelectorAll('.mt-table tbody tr'));
    if (!rows.length) { host.innerHTML = ''; return; }
    host.innerHTML = '<div class="mtf-h">On site now <small>' + rows.length + ' item' + (rows.length === 1 ? '' : 's') + '</small></div><div class="mtf-list">' +
      rows.map(function (tr) {
        var td = tr.children;
        var name = txt(td[0] && td[0].querySelector('.nm')), unit = txt(td[0] && td[0].querySelector('.un')).replace(/^·\s*/, '').split(' · ')[0];
        var onHand = txt(td[1]), left = txt(td[5]), chip = td[6] ? td[6].innerHTML : '';
        var detail = onHand + ' ' + esc(unit) + ' on hand' + (left && left !== '—' ? ' · about ' + esc(left) + ' days left' : '');
        return '<div class="mtf-row"><div class="mtf-tx"><b>' + esc(name) + '</b><small>' + detail + '</small></div>' + chip + '</div>';
      }).join('') + '</div>';
  }

  // ---------------------------------------------------------------- log modal steps
  function buildSteps() {
    var ov = $('mt-modal-log'), modal = ov.querySelector('.mt-modal');
    var title = $('mt-log-title'), seg = $('mt-log-seg');
    var flds = [].slice.call(modal.children).filter(function (c) { return c.classList.contains('mt-fld') || c.classList.contains('mt-grid2'); });
    // modal order: material · (qty, unit) · (vendor, date) · slip
    if (seg) seg.setAttribute('data-mtf', '1');
    if (flds[0]) flds[0].setAttribute('data-mtf', '1');
    if (flds[1]) flds[1].setAttribute('data-mtf', '2');
    if (flds[2]) flds[2].setAttribute('data-mtf', '2');
    if (flds[3]) flds[3].setAttribute('data-mtf', '3');
    var foot = modal.querySelector('.mt-mfoot'); if (foot) foot.setAttribute('data-mtf', '3');

    var prog = document.createElement('div'); prog.className = 'mtf-prog';
    var bars = document.createElement('div'); bars.className = 'mtf-bars'; bars.innerHTML = '<i></i><i></i><i></i>';
    var sub = $('mt-log-sub') || title;
    sub.parentNode.insertBefore(bars, sub.nextElementSibling);
    sub.parentNode.insertBefore(prog, bars);

    // − / + around the quantity
    var qty = $('mt-log-qty');
    var box = document.createElement('div'); box.className = 'mtf-qty';
    qty.parentNode.insertBefore(box, qty);
    box.innerHTML = '<button type="button" data-mtf-q="-1" aria-label="Less">−</button>';
    box.appendChild(qty);
    box.insertAdjacentHTML('beforeend', '<button type="button" data-mtf-q="1" aria-label="More">+</button>');

    var nav = document.createElement('div'); nav.className = 'mtf-nav';
    nav.innerHTML = '<button type="button" class="mtf-back">Cancel</button><button type="button" class="mtf-next">Next</button>';
    modal.insertBefore(nav, $('mt-log-err'));

    modal.addEventListener('click', function (e) {
      var q = e.target.closest('[data-mtf-q]');
      if (q) { var v = parseFloat(qty.value) || 0; qty.value = Math.max(0, v + (+q.getAttribute('data-mtf-q'))); qty.dispatchEvent(new Event('input', { bubbles: true })); return; }
      if (e.target.closest('.mtf-next')) {
        var err = $('mt-log-err'); err.textContent = '';
        if (step === 1 && !$('mt-log-mat').value) { err.textContent = 'Pick what came.'; return; }
        if (step === 2 && !(parseFloat(qty.value) > 0)) { err.textContent = 'Enter how much came.'; return; }
        setStep(step + 1); return;
      }
      if (e.target.closest('.mtf-back')) {
        if (step > 1) setStep(step - 1);
        else { var c = ov.querySelector('[data-close="mt-modal-log"]'); if (c) c.click(); }
      }
    });
    // every time the modal opens, start at step 1
    new MutationObserver(function () { if (ov.classList.contains('show') && !ov._mtfOpen) { ov._mtfOpen = true; setStep(1); } else if (!ov.classList.contains('show')) { ov._mtfOpen = false; } })
      .observe(ov, { attributes: true, attributeFilter: ['class'] });
  }
  function setStep(n) {
    step = n;
    var ov = $('mt-modal-log');
    ov.classList.remove('mtf-s1', 'mtf-s2', 'mtf-s3'); ov.classList.add('mtf-s' + n);
    var prog = ov.querySelector('.mtf-prog'); if (prog) prog.textContent = 'Step ' + n + ' of 3 · ' + STEP_NAMES[n - 1];
    [].forEach.call(ov.querySelectorAll('.mtf-bars i'), function (b, i) { b.classList.toggle('on', i < n); });
    var back = ov.querySelector('.mtf-back'); if (back) back.textContent = n > 1 ? 'Back' : 'Cancel';
  }

  // ---------------------------------------------------------------- wiring
  function onClick(e) {
    var t = e.target.closest('[data-mtf-log],[data-mtf-count],[data-mtf-click],[data-mtf-full]');
    if (!t) return;
    if (t.hasAttribute('data-mtf-log')) {
      $('mt-btn-log').click();   // the screen's own openLog (defaults to delivery)
      var type = t.getAttribute('data-mtf-log');
      var s = $('mt-log-seg') && $('mt-log-seg').querySelector('span[data-t="' + type + '"]');
      if (s && $('mt-modal-log').classList.contains('show')) s.click();
    } else if (t.hasAttribute('data-mtf-count')) {
      var p = $('mt-count-panel');
      if (p && p.classList.contains('mt-hide')) $('mt-btn-count').click();
      if (p) p.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else if (t.hasAttribute('data-mtf-click')) {
      var b = $(t.getAttribute('data-mtf-click')); if (b) b.click();
    } else if (t.hasAttribute('data-mtf-full')) {
      sec.classList.toggle('mtf-full');
      t.textContent = sec.classList.contains('mtf-full') ? 'Hide the full table' : 'Show the full table';
    }
  }

  function init() {
    sec = document.querySelector('section[data-view="materials"]'); if (!sec) return;
    wrap = sec.querySelector('.mt-wrap');
    if (!wrap || !$('mt-btn-log') || !$('mt-catalog') || !$('mt-modal-log') || !$('mt-log-qty')) return;   // markup changed: leave the screen alone
    var st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);
    buildHome(); buildSteps(); built = true;
    sec.addEventListener('click', onClick);
    new MutationObserver(renderOnsite).observe($('mt-catalog'), { childList: true, subtree: true });
    setStep(1);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
