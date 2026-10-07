/* #312 — the collapsible menu for phone widths, shared by every surface.
 *
 * Below 860 px the sidebar is a compact brand bar; the nav and footer stay hidden
 * until the Menu button is tapped, slide in, and close again on a nav tap, on
 * Escape, or when the window widens past the breakpoint. Works on the shell
 * (`aside.ssc-side` + `.brand`) and on the project dashboard / client portal
 * (`.sidebar` + `.sidebar-header`) without touching their markup: the button is
 * created here. Styles live in widgets.css (.ssc-menu-btn, .nav-open rules).
 */
(function () {
  'use strict';
  var side = document.querySelector('aside.ssc-side') || document.querySelector('.sidebar');
  if (!side) return;
  var brand = side.querySelector('.brand') || side.querySelector('.sidebar-header');
  if (!brand) return;
  var mq = window.matchMedia('(max-width: 860px)');

  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ssc-menu-btn';
  btn.setAttribute('aria-expanded', 'false');
  btn.setAttribute('aria-label', 'Open menu');
  btn.innerHTML = '<span class="ssc-menu-bars" aria-hidden="true"><i></i><i></i><i></i></span><span class="ssc-menu-txt">Menu</span>';
  brand.appendChild(btn);

  function set(open) {
    side.classList.toggle('nav-open', open);
    btn.setAttribute('aria-expanded', String(open));
    btn.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
    btn.querySelector('.ssc-menu-txt').textContent = open ? 'Close' : 'Menu';
  }
  btn.addEventListener('click', function () { set(!side.classList.contains('nav-open')); });
  side.addEventListener('click', function (e) {
    var a = e.target.closest('a, .nav-tab, .nav-link, button.signout');
    if (a && a !== btn && mq.matches) set(false);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && side.classList.contains('nav-open')) { set(false); btn.focus(); }
  });
  var onChange = function () { if (!mq.matches) set(false); };
  if (mq.addEventListener) mq.addEventListener('change', onChange); else mq.addListener(onChange);

  /* #359 — the collapsible icon rail (wider than 860 px only; below that the Menu
   * drawer above is unchanged). The company shell always offers it; the project
   * dashboard opts in with <aside data-rail>. The choice is remembered per browser
   * under ssc.side.rail, and each page sets html.side-rail in its <head> before the
   * first paint so a collapsed rail never flashes open. Labels stay in the markup,
   * hidden by CSS, and become each link's tooltip + accessible name.
   *
   * Pin: the button is a pin. Pinned (the default) = the sidebar stays open.
   * Unpinned = the icon rail, which slides open OVER the page (html.side-peek; the
   * content never shifts) while the pointer is on it or keyboard focus is inside it,
   * and folds back when the pointer leaves, focus leaves, or Escape is pressed. */
  if (!(side.classList.contains('ssc-side') || side.hasAttribute('data-rail'))) return;
  var KEY = 'ssc.side.rail', root = document.documentElement;
  var LINKS = '.nav-link, .ssc-nav a, .ssc-side-foot a';
  var PIN = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 4h6l-1 6 3 3v2H7v-2l3-3-1-6z"/><path d="M12 15v6"/></svg>';
  var rail = document.createElement('button');
  rail.type = 'button';
  rail.className = 'ssc-rail-btn';
  rail.innerHTML = PIN;
  brand.appendChild(rail);

  function label(el) {
    var t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t) return;
    if (root.classList.contains('side-rail')) {
      if (el.hasAttribute('data-rail-tip')) return;
      el.setAttribute('data-rail-tip', el.title ? 'own' : 'added');   // a link's own tooltip is kept
      if (!el.title) el.title = t;
      el.setAttribute('aria-label', t);
    } else if (el.hasAttribute('data-rail-tip')) {
      if (el.getAttribute('data-rail-tip') === 'added') el.removeAttribute('title');
      el.removeAttribute('aria-label'); el.removeAttribute('data-rail-tip');
    }
  }
  function sync() {
    var pinned = !root.classList.contains('side-rail');
    rail.setAttribute('aria-pressed', String(pinned));
    rail.setAttribute('aria-label', pinned ? 'Unpin sidebar' : 'Pin sidebar open');
    rail.title = pinned ? 'Unpin sidebar (shrink to icons, open on hover)' : 'Pin sidebar open';
    Array.prototype.forEach.call(side.querySelectorAll(LINKS), label);
  }
  var timer = null;
  function peek(on, delay) {
    clearTimeout(timer);
    timer = setTimeout(function () {
      root.classList.toggle('side-peek', on && root.classList.contains('side-rail') && mq.matches === false);
    }, delay || 0);
  }
  rail.addEventListener('click', function () {
    var unpin = !root.classList.contains('side-rail');
    root.classList.toggle('side-rail', unpin);
    root.classList.remove('side-peek');
    try { localStorage.setItem(KEY, unpin ? '1' : '0'); } catch (e) { /* private window: not remembered */ }
    sync();
    if (unpin) rail.blur();   // focus inside would hold the panel open
  });
  side.addEventListener('mouseenter', function () { peek(true, 120); });
  side.addEventListener('mouseleave', function () { peek(false, 250); });
  side.addEventListener('focusin', function () { peek(true, 0); });
  side.addEventListener('focusout', function (e) { if (!side.contains(e.relatedTarget)) peek(false, 0); });
  side.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && root.classList.contains('side-peek')) { peek(false, 0); if (document.activeElement) document.activeElement.blur(); }
  });
  // the company nav renders its links after this runs — label late arrivals on first touch
  side.addEventListener('mouseover', function (e) { var a = e.target.closest(LINKS); if (a) label(a); });
  side.addEventListener('focusin', function (e) { var a = e.target.closest(LINKS); if (a) label(a); });
  sync();
})();
