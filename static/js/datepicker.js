/* SSC Date Picker — small custom popup that REPLACES the native
 * <input type="date"> calendar across every dashboard surface. No external
 * library. On the shared Lumen azure system (#243): accents use the global
 * --accent tokens from widgets.css, with literal fallbacks so the picker
 * stays correct on pages that don't load the shared stylesheet
 * (admin_labor_rates, rfi_submission_form). Header has prev/next
 * month, a month <select>, and a year <select> covering ~1940..(current+5)
 * for instant decade jumps (the whole point — DOB shouldn't require 480
 * prev-month clicks).
 *
 * Value contract: stored as YYYY-MM-DD on the input's .value, written
 * with local date arithmetic only — never via toISOString() (#74 / CLAUDE.md
 * dates rule).
 *
 * Usage:
 *   <script src="/files/static/js/datepicker.js"></script>
 *   SSCDatePicker.wire(document.getElementById('in-dob'), {max: 'today'});
 *   SSCDatePicker.wireAll('input.date-field');
 *
 * Options:
 *   max: 'today'   -> disallow future dates (DOB)
 *   minYear / maxYear: override the year range (default 1940..currentYear+5)
 */
(function(global) {
  'use strict';

  // #243 — Lumen azure accents, tokenized. var(--token, fallback): the token
  // resolves on pages that load widgets.css; the fallback mirrors the token's
  // value for the two pages that don't. NEVER reintroduce brand red here —
  // the picker is a shared component and recolors at the component level only.
  var ACCENT = 'var(--accent, #4364dc)';
  var ACCENT_INK = 'var(--accent-ink, #3a5fd0)';
  var ACCENT_SOFT = 'var(--accent-soft, #ebf0fe)';
  var ACCENT_EDGE = 'var(--accent-edge, rgba(67,100,220,.30))';
  var HAIR = '#E8E4DD';
  var INK = '#14161C';
  var MUTE = '#76777E';
  var STYLE_ID = 'ssc-dp-style';

  function ensureStyles() {
    if (document.getElementById(STYLE_ID)) return;
    var s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = [
      '.ssc-dp-popup{',
      '  position:absolute;z-index:99999;background:#fff;',
      '  border:1px solid ' + HAIR + ';border-radius:8px;',
      '  box-shadow:0 8px 24px rgba(0,0,0,0.18);padding:12px;width:264px;',
      "  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;",
      '  color:' + INK + ';font-size:13px;line-height:1.2;',
      '  -webkit-font-smoothing:antialiased;',
      '}',
      '.ssc-dp-popup *{box-sizing:border-box;}',
      '.ssc-dp-popup .head{display:flex;align-items:center;gap:4px;margin-bottom:8px;}',
      '.ssc-dp-popup button.nav{',
      '  background:#fff;border:1px solid ' + HAIR + ';border-radius:4px;',
      '  cursor:pointer;padding:4px 9px;color:' + INK + ';',
      '  font-family:inherit;font-size:16px;line-height:1;font-weight:600;',
      '}',
      '.ssc-dp-popup button.nav:hover{background:#F1EEE8;}',
      '.ssc-dp-popup select{',
      '  flex:1 1 auto;min-width:0;padding:4px 6px;',
      '  border:1px solid ' + HAIR + ';border-radius:4px;',
      '  font-family:inherit;font-size:13px;background:#fff;color:' + INK + ';',
      '  cursor:pointer;font-weight:600;',
      '}',
      '.ssc-dp-popup select:focus{outline:1px solid ' + ACCENT + ';outline-offset:0;}',
      '.ssc-dp-popup .grid{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;}',
      '.ssc-dp-popup .dow{',
      '  text-align:center;font-size:10px;text-transform:uppercase;',
      '  letter-spacing:0.5px;color:' + MUTE + ';padding:4px 0;font-weight:700;',
      '}',
      '.ssc-dp-popup .day{',
      '  text-align:center;padding:6px 0;cursor:pointer;border-radius:4px;',
      '  font-size:13px;user-select:none;font-weight:500;',
      '}',
      '.ssc-dp-popup .day:hover{background:#F1EEE8;}',
      '.ssc-dp-popup .day.muted{color:#C8C0B4;cursor:default;}',
      '.ssc-dp-popup .day.muted:hover{background:transparent;}',
      '.ssc-dp-popup .day.today{outline:1px solid ' + ACCENT + ';outline-offset:-1px;font-weight:700;}',
      '.ssc-dp-popup .day.selected{background:' + ACCENT + ';color:#fff;font-weight:700;box-shadow:0 2px 6px rgba(67,100,220,.30);}',
      '.ssc-dp-popup .day.selected:hover{background:' + ACCENT_INK + ';}',
      '.ssc-dp-popup .day.disabled{color:#C8C0B4;cursor:not-allowed;text-decoration:line-through;}',
      '.ssc-dp-popup .day.disabled:hover{background:transparent;}',
      '.ssc-dp-popup .foot{',
      '  display:flex;justify-content:space-between;gap:6px;',
      '  margin-top:8px;padding-top:8px;border-top:1px solid ' + HAIR + ';',
      '}',
      '.ssc-dp-popup .foot button{',
      '  background:#fff;border:1px solid ' + HAIR + ';border-radius:4px;',
      '  cursor:pointer;padding:5px 12px;font-size:12px;',
      '  font-family:inherit;color:' + INK + ';font-weight:600;',
      '}',
      '.ssc-dp-popup .foot button:hover{background:#F1EEE8;}',
      '.ssc-dp-popup .foot button.today-btn{color:' + ACCENT + ';border-color:' + ACCENT_EDGE + ';background:' + ACCENT_SOFT + ';}',
      '.ssc-dp-popup .foot button.today-btn:hover{background:#dde7fd;}',
      'input[data-ssc-dp]{cursor:pointer;background:#fff;}',
      'input[data-ssc-dp]:focus{outline:1px solid ' + ACCENT + ';}',
    ].join('');
    document.head.appendChild(s);
  }

  var MONTHS = ['January','February','March','April','May','June',
                'July','August','September','October','November','December'];
  var DOW = ['Su','Mo','Tu','We','Th','Fr','Sa'];

  function pad(n) { return n < 10 ? '0' + n : '' + n; }

  // Format a JS Date (local) as YYYY-MM-DD. NEVER via toISOString — that
  // returns UTC and corrupts dates entered in the evening (the #74 bug).
  function toYMD(d) {
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  // Parse YYYY-MM-DD as a local-midnight Date. Returns null on bad input.
  function parseYMD(s) {
    if (!s) return null;
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
    if (!m) return null;
    var y = +m[1], mo = +m[2] - 1, d = +m[3];
    var dt = new Date(y, mo, d);
    if (dt.getFullYear() !== y || dt.getMonth() !== mo || dt.getDate() !== d) return null;
    return dt;
  }

  // Display formatter: 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS' / 'YYYY-MM-DD HH:MM:SS'
  // -> 'MM-DD-YYYY'. The date-only output is the operator's preferred read form;
  // the "Issued at" surfaces drop the time per the display rule. Falls back to
  // returning the input verbatim if it doesn't look like an ISO date.
  function fmtMDY(s) {
    if (s == null || s === '') return '';
    var str = String(s);
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(str);
    if (!m) return str;
    return m[2] + '-' + m[3] + '-' + m[1];
  }

  // Reverse formatter: 'MM-DD-YYYY' or 'MM/DD/YYYY' -> 'YYYY-MM-DD'. Used when
  // a wired input's `value` carries the displayed form but the canonical
  // dataset.iso is missing (e.g., the operator pasted a date by hand).
  function parseMDY(s) {
    if (!s) return null;
    var m = /^(\d{2})[-\/](\d{2})[-\/](\d{4})$/.exec(String(s).trim());
    if (!m) return null;
    return m[3] + '-' + m[1] + '-' + m[2];
  }

  // Canonical YYYY-MM-DD value for a wired input. Prefer dataset.iso (set by
  // the popup or by wire()), fall back to parsing the visible value as either
  // MM-DD-YYYY or YYYY-MM-DD. Empty string when the input is blank.
  function getISO(input) {
    if (!input) return '';
    if (input.dataset && input.dataset.iso) return input.dataset.iso;
    var v = (input.value || '').trim();
    if (!v) return '';
    if (/^\d{4}-\d{2}-\d{2}$/.test(v)) return v;
    var mdy = parseMDY(v);
    return mdy || v;
  }

  // Set a wired input from a canonical YYYY-MM-DD (or empty). Writes both
  // dataset.iso (canonical) and value (display MM-DD-YYYY).
  //
  // Defensive: `<input type="date">` silently rejects any value that isn't
  // YYYY-MM-DD (so writing "MM-DD-YYYY" leaves .value empty). Some callers
  // run setISO BEFORE the input is wired (and switched to text), so flip
  // the type here too — idempotent with wire().
  function setISO(input, iso) {
    if (!input) return;
    if (input.type === 'date') {
      try { input.type = 'text'; } catch (_) {}
    }
    if (!iso) {
      input.dataset.iso = '';
      input.value = '';
      return;
    }
    input.dataset.iso = iso;
    input.value = fmtMDY(iso);
  }

  // Today as a local-midnight Date (so date comparisons via > / < work).
  function todayLocal() {
    var d = new Date();
    return new Date(d.getFullYear(), d.getMonth(), d.getDate());
  }

  function buildPopup(input, opts) {
    opts = opts || {};
    var minYear = opts.minYear || 1940;
    var curY = new Date().getFullYear();
    var maxYear = opts.maxYear || (curY + 5);
    var maxDate = (opts.max === 'today') ? todayLocal() :
                  (opts.max instanceof Date ? opts.max :
                   (typeof opts.max === 'string' ? parseYMD(opts.max) : null));

    ensureStyles();
    var popup = document.createElement('div');
    popup.className = 'ssc-dp-popup';

    var viewYear, viewMonth, selected;

    function init() {
      // The visible input.value is MM-DD-YYYY after wire(); the canonical
      // YYYY-MM-DD lives in input.dataset.iso. Use the canonical first.
      var iso = getISO(input);
      var cur = parseYMD(iso) || todayLocal();
      selected = parseYMD(iso);
      viewYear = cur.getFullYear();
      viewMonth = cur.getMonth();
      // Clamp the initial view into the allowed year range
      if (viewYear < minYear) { viewYear = minYear; viewMonth = 0; }
      if (viewYear > maxYear) { viewYear = maxYear; viewMonth = 11; }
    }

    function jumpMonth(delta) {
      var m = viewMonth + delta, y = viewYear;
      while (m < 0) { m += 12; y -= 1; }
      while (m > 11) { m -= 12; y += 1; }
      if (y < minYear) { y = minYear; m = 0; }
      if (y > maxYear) { y = maxYear; m = 11; }
      viewYear = y; viewMonth = m;
      render();
    }

    function pick(y, m, d) {
      var dt = new Date(y, m, d);
      if (maxDate && dt > maxDate) return;
      selected = dt;
      // Canonical YYYY-MM-DD on dataset.iso, display MM-DD-YYYY on .value.
      // Storage/API submission paths read dataset.iso via getISO(); only
      // the operator-facing surface reads the display string.
      setISO(input, toYMD(dt));
      input.dispatchEvent(new Event('change', {bubbles: true}));
      close();
    }

    function render() {
      popup.innerHTML = '';
      var head = document.createElement('div');
      head.className = 'head';

      var prev = document.createElement('button');
      prev.type = 'button'; prev.className = 'nav';
      prev.innerHTML = '&#8249;'; prev.title = 'Previous month';
      prev.addEventListener('click', function(e) { e.preventDefault(); jumpMonth(-1); });

      var monthSel = document.createElement('select');
      monthSel.title = 'Month';
      MONTHS.forEach(function(name, i) {
        var o = document.createElement('option');
        o.value = String(i); o.textContent = name;
        if (i === viewMonth) o.selected = true;
        monthSel.appendChild(o);
      });
      monthSel.addEventListener('change', function() { viewMonth = +monthSel.value; render(); });

      var yearSel = document.createElement('select');
      yearSel.title = 'Year';
      for (var y = maxYear; y >= minYear; y--) {
        var o = document.createElement('option');
        o.value = String(y); o.textContent = String(y);
        if (y === viewYear) o.selected = true;
        yearSel.appendChild(o);
      }
      yearSel.addEventListener('change', function() { viewYear = +yearSel.value; render(); });

      var next = document.createElement('button');
      next.type = 'button'; next.className = 'nav';
      next.innerHTML = '&#8250;'; next.title = 'Next month';
      next.addEventListener('click', function(e) { e.preventDefault(); jumpMonth(1); });

      head.appendChild(prev);
      head.appendChild(monthSel);
      head.appendChild(yearSel);
      head.appendChild(next);
      popup.appendChild(head);

      var grid = document.createElement('div');
      grid.className = 'grid';
      DOW.forEach(function(d) {
        var el = document.createElement('div');
        el.className = 'dow'; el.textContent = d;
        grid.appendChild(el);
      });

      var firstDow = new Date(viewYear, viewMonth, 1).getDay();   // 0..6, Sun=0
      var daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
      var daysInPrev = new Date(viewYear, viewMonth, 0).getDate();
      // Leading days (muted) so the first row aligns to the correct DOW
      for (var i = 0; i < firstDow; i++) {
        var dn = daysInPrev - firstDow + 1 + i;
        var ml = document.createElement('div');
        ml.className = 'day muted'; ml.textContent = String(dn);
        grid.appendChild(ml);
      }
      var today = todayLocal();
      var today_y = today.getFullYear(), today_m = today.getMonth(), today_d = today.getDate();
      for (var dn2 = 1; dn2 <= daysInMonth; dn2++) {
        var el = document.createElement('div');
        el.className = 'day'; el.textContent = String(dn2);
        var candidate = new Date(viewYear, viewMonth, dn2);
        if (viewYear === today_y && viewMonth === today_m && dn2 === today_d) {
          el.classList.add('today');
        }
        if (selected && selected.getFullYear() === viewYear
            && selected.getMonth() === viewMonth
            && selected.getDate() === dn2) {
          el.classList.add('selected');
        }
        if (maxDate && candidate > maxDate) {
          el.classList.add('disabled');
        } else {
          (function(yy, mm, dd) {
            el.addEventListener('click', function() { pick(yy, mm, dd); });
          })(viewYear, viewMonth, dn2);
        }
        grid.appendChild(el);
      }
      // Trailing muted days so the grid is rectangular
      var filled = firstDow + daysInMonth;
      var trailing = (filled % 7 === 0) ? 0 : (7 - (filled % 7));
      for (var t = 1; t <= trailing; t++) {
        var mt = document.createElement('div');
        mt.className = 'day muted'; mt.textContent = String(t);
        grid.appendChild(mt);
      }
      popup.appendChild(grid);

      var foot = document.createElement('div');
      foot.className = 'foot';
      var clearBtn = document.createElement('button');
      clearBtn.type = 'button';
      clearBtn.textContent = 'Clear';
      clearBtn.addEventListener('click', function(e) {
        e.preventDefault();
        setISO(input, '');
        selected = null;
        input.dispatchEvent(new Event('change', {bubbles: true}));
        close();
      });
      var todayBtn = document.createElement('button');
      todayBtn.type = 'button';
      todayBtn.className = 'today-btn';
      todayBtn.textContent = 'Today';
      todayBtn.addEventListener('click', function(e) {
        e.preventDefault();
        var t = todayLocal();
        pick(t.getFullYear(), t.getMonth(), t.getDate());
      });
      foot.appendChild(clearBtn);
      foot.appendChild(todayBtn);
      popup.appendChild(foot);
    }

    function position() {
      var r = input.getBoundingClientRect();
      var top = window.scrollY + r.bottom + 4;
      var left = window.scrollX + r.left;
      // Keep popup inside viewport — flip above input if there isn't room
      var popupH = 320;  // approximate; we don't render twice for measurement
      if (top + popupH > window.scrollY + window.innerHeight) {
        top = window.scrollY + r.top - popupH - 4;
      }
      // Keep popup inside viewport horizontally
      var popupW = 264;
      if (left + popupW > window.scrollX + window.innerWidth - 4) {
        left = window.scrollX + window.innerWidth - popupW - 4;
      }
      popup.style.top = Math.max(window.scrollY + 4, top) + 'px';
      popup.style.left = Math.max(window.scrollX + 4, left) + 'px';
    }

    function onOutsideMouse(e) {
      if (popup.contains(e.target) || e.target === input) return;
      close();
    }
    function onKey(e) {
      if (e.key === 'Escape') close();
    }

    function open() {
      init();
      render();
      document.body.appendChild(popup);
      position();
      // Defer the outside-click listener so the click that opens us doesn't immediately close us
      setTimeout(function() {
        document.addEventListener('mousedown', onOutsideMouse, true);
        document.addEventListener('keydown', onKey, true);
      }, 0);
    }
    function close() {
      try { popup.remove(); } catch (_) {}
      document.removeEventListener('mousedown', onOutsideMouse, true);
      document.removeEventListener('keydown', onKey, true);
    }

    return { open: open, close: close };
  }

  // Wire one input. Idempotent — re-wiring is a no-op.
  function wire(input, opts) {
    if (!input || input.dataset.sscDpWired === '1') return;
    opts = opts || {};
    // #236 — let a field carry its OWN constraints via data-attrs, so the global
    // auto-wire honors them without an explicit per-surface wire() call (e.g. a
    // DOB field tagged data-ssc-max="today" stays capped at today even if the
    // auto-wire reaches it first). Explicit opts still win.
    if (opts.max === undefined && input.dataset.sscMax) opts.max = input.dataset.sscMax;
    if (opts.minYear === undefined && input.dataset.sscMinYear) opts.minYear = +input.dataset.sscMinYear;
    if (opts.maxYear === undefined && input.dataset.sscMaxYear) opts.maxYear = +input.dataset.sscMaxYear;
    input.dataset.sscDpWired = '1';
    // Switch off native picker — the canonical YYYY-MM-DD lives on
    // dataset.iso; .value carries the MM-DD-YYYY display form for the
    // operator. Form submission paths must read via SSCDatePicker.getISO(el).
    try { input.type = 'text'; } catch (_) {}
    input.setAttribute('data-ssc-dp', '1');
    input.setAttribute('readonly', '');           // operator must use the popup
    if (!input.getAttribute('placeholder')) {
      input.setAttribute('placeholder', 'MM-DD-YYYY');
    }
    input.setAttribute('autocomplete', 'off');
    input.style.cursor = 'pointer';
    // If the input was pre-seeded with a YYYY-MM-DD (the common case — code
    // sets el.value = todayLocal()), promote it to (dataset.iso, MM-DD-YYYY
    // .value). Already-MM-DD-YYYY values pass through via getISO+setISO.
    var preset = (input.value || '').trim();
    if (preset) {
      setISO(input, getISO(input));
    }

    var live = null, openedByFocus = 0;
    function toggle(e) {
      if (e) e.preventDefault();
      // A mouse click on an unfocused field fires focus (opens) and then click: that click
      // must not close the popup it just opened, or every first click looks dead.
      if (live && e && e.type === 'click' && Date.now() - openedByFocus < 500) return;
      if (live) { live.close(); live = null; return; }
      live = buildPopup(input, opts);
      var origClose = live.close;
      live.close = function() { origClose(); live = null; };
      live.open();
      if (e && e.type === 'focus') openedByFocus = Date.now();
    }
    input.addEventListener('focus', toggle);
    input.addEventListener('click', toggle);
    input.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggle();
      }
    });
  }

  function wireAll(selector, opts) {
    var nodes = document.querySelectorAll(selector);
    for (var i = 0; i < nodes.length; i++) wire(nodes[i], opts);
  }

  // #236 — GLOBAL auto-wire. Wire EVERY date field (the standard class
  // .ssc-date and any native <input type="date">) so no surface ever ships a
  // raw MM/DD/YYYY input. Safe to call on any subtree; wire() is idempotent.
  var DATE_SEL = '.ssc-date, input[type="date"]';
  function autoWire(root, opts) {
    root = root || document;
    try { if (root.nodeType === 1 && root.matches && root.matches(DATE_SEL)) wire(root, opts); } catch (_) {}
    var nodes = (root.querySelectorAll ? root.querySelectorAll(DATE_SEL) : []);
    for (var i = 0; i < nodes.length; i++) wire(nodes[i], opts);
  }

  // On page load: wire everything present. Then keep wiring fields that appear
  // LATER (dynamically-rendered modals / bulk rows) via a MutationObserver, so
  // a modal opened after load is covered without each surface calling wire().
  var _autoStarted = false;
  function startAutoWire() {
    if (_autoStarted) return;
    _autoStarted = true;
    autoWire(document);
    if (global.MutationObserver) {
      var obs = new MutationObserver(function (muts) {
        for (var i = 0; i < muts.length; i++) {
          var added = muts[i].addedNodes;
          for (var j = 0; j < added.length; j++) {
            if (added[j].nodeType === 1) autoWire(added[j]);
          }
        }
      });
      try { obs.observe(document.documentElement || document.body, { childList: true, subtree: true }); } catch (_) {}
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startAutoWire);
  } else {
    startAutoWire();
  }

  global.SSCDatePicker = {
    wire: wire,
    wireAll: wireAll,
    autoWire: autoWire,
    parseYMD: parseYMD,
    toYMD: toYMD,
    // Shared display + canonical-value helpers. Every surface that
    // renders a date to the operator goes through fmtMDY; every
    // submission/storage path reads getISO. Keep this the single source
    // of truth for the YYYY-MM-DD <-> MM-DD-YYYY transform.
    fmtMDY: fmtMDY,
    parseMDY: parseMDY,
    getISO: getISO,
    setISO: setISO,
  };
})(window);
