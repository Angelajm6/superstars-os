/* Drag a file onto any attach spot (2026-10-20).
 *
 * Some browsers never open the file window (a managed-Chrome policy, an extension). Dragging still
 * works there, so every file input in the app also takes a dropped file: drag a file over the page
 * and the attach spot under it lights up; drop it and the input receives the file exactly as if it
 * had been picked (its own `change` handler runs, nothing else changes).
 *
 *   - the drop target is the smallest box around the pointer that holds a file input (camera-only
 *     inputs are skipped); a drop that another handler already took (the Claude window, Proposal
 *     Studio's template card) is left alone
 *   - a file dropped anywhere else is ignored instead of opening it in the tab (no lost work)
 *   - when a file window is asked for and nothing seems to open, a small tip says to drag instead
 */
(function () {
  if (window.SSC_FILE_DROP) return;
  window.SSC_FILE_DROP = true;
  var css = document.createElement('style');
  css.textContent = '.ssc-drop-on{outline:2px dashed #4364dc!important;outline-offset:3px;border-radius:10px;background-color:rgba(67,100,220,.06)!important}' +
    '.ssc-drop-tip{position:fixed;z-index:10000;max-width:300px;background:#222633;color:#fff;font:500 13px/1.45 Inter,-apple-system,sans-serif;' +
    'padding:10px 12px;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.2)}.ssc-drop-tip b{display:block;margin-bottom:2px}';
  (document.head || document.documentElement).appendChild(css);

  function hasFiles(e) {
    var t = e.dataTransfer && e.dataTransfer.types;
    if (!t) return false;
    for (var i = 0; i < t.length; i++) if (t[i] === 'Files') return true;
    return false;
  }
  function usable(inp) { return inp && !inp.disabled && !inp.hasAttribute('capture'); }
  function targetFor(el) {
    for (var n = el, depth = 0; n && n !== document.body && depth < 10; n = n.parentElement, depth++) {
      if (n.nodeType !== 1) continue;
      if (n.tagName === 'INPUT' && n.type === 'file' && usable(n)) return { box: n.parentElement || n, input: n };
      var list = n.querySelectorAll ? n.querySelectorAll('input[type=file]') : [];
      var ok = [];
      for (var i = 0; i < list.length; i++) if (usable(list[i])) ok.push(list[i]);
      if (ok.length === 1) return { box: n, input: ok[0] };
      if (ok.length > 1) return null;   // ambiguous: two attach spots in one box
    }
    return null;
  }
  var lit = null;
  function light(box) {
    if (lit === box) return;
    if (lit) lit.classList.remove('ssc-drop-on');
    lit = box;
    if (lit) lit.classList.add('ssc-drop-on');
  }
  document.addEventListener('dragover', function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();   // never let the browser open the file in place of the page
    var t = targetFor(e.target);
    light(t ? t.box : null);
    try { e.dataTransfer.dropEffect = t ? 'copy' : 'none'; } catch (x) {}
  });
  document.addEventListener('dragleave', function (e) { if (!e.relatedTarget) light(null); });
  document.addEventListener('drop', function (e) {
    if (!hasFiles(e)) return;
    light(null);
    if (e.defaultPrevented) return;   // the spot handled it itself
    e.preventDefault();
    var t = targetFor(e.target);
    if (!t) return;
    var files = e.dataTransfer.files;
    if (!files || !files.length) return;
    try {
      var dt = new DataTransfer();
      for (var i = 0; i < files.length && (t.input.multiple || i < 1); i++) dt.items.add(files[i]);
      t.input.files = dt.files;
    } catch (x) { return; }
    t.input.dispatchEvent(new Event('input', { bubbles: true }));
    t.input.dispatchEvent(new Event('change', { bubbles: true }));
  });

  // the file window was asked for: if the page never loses focus and no file arrives, say how else to do it
  var tip = null;
  function hideTip() { if (tip) { tip.remove(); tip = null; } }
  document.addEventListener('click', function (e) {
    var inp = e.target;
    if (!inp || inp.tagName !== 'INPUT' || inp.type !== 'file' || inp.hasAttribute('capture')) return;
    if (e.defaultPrevented) return;
    var blurred = false, before = inp.files ? inp.files.length : 0;
    function onBlur() { blurred = true; }
    window.addEventListener('blur', onBlur, { once: true });
    setTimeout(function () {
      window.removeEventListener('blur', onBlur);
      if (blurred || document.visibilityState !== 'visible' || (inp.files && inp.files.length !== before)) return;
      hideTip();
      var box = targetFor(inp), r = (box ? box.box : inp).getBoundingClientRect();
      tip = document.createElement('div');
      tip.className = 'ssc-drop-tip';
      tip.setAttribute('role', 'status');
      tip.innerHTML = '<b>No file window?</b>Your browser may block it. Drag the file from Finder onto this spot instead.';
      document.body.appendChild(tip);
      var top = r.bottom + 8, left = Math.max(8, Math.min(r.left, window.innerWidth - 316));
      if (top + tip.offsetHeight > window.innerHeight - 8) top = Math.max(8, r.top - tip.offsetHeight - 8);
      tip.style.top = top + 'px'; tip.style.left = left + 'px';
      setTimeout(hideTip, 7000);
    }, 1500);
  }, true);
  document.addEventListener('dragenter', hideTip);
})();
