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
})();
