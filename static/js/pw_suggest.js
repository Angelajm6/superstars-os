/* #309 — the strong-password suggester for the set-password pages.
 *
 * generate(): three random words from a fixed list + two digits, joined with dashes,
 * e.g. "Harbor-Copper-Stone-47". Always >= 12 characters with a letter and a digit, so
 * it satisfies the server rule (auth.password_strength_error) by construction, and it
 * is readable enough to type from a phone screen. Randomness comes from
 * crypto.getRandomValues; Math.random is never used.
 *
 * wire({button, fields, toggles, hint, after}): on click, fill every field with the same
 * suggestion, reveal each field through its existing eye toggle (so the person can read
 * what they are about to save), show the hint, and call `after` (the page's checklist).
 * Nothing leaves the browser.
 */
(function () {
  'use strict';
  var WORDS = [
    'anchor', 'arbor', 'aspen', 'atlas', 'badge', 'bamboo', 'banner', 'basin', 'beacon', 'birch',
    'bridge', 'bronze', 'butter', 'cabin', 'candle', 'canyon', 'carbon', 'cedar', 'cider', 'cliff',
    'clover', 'cobalt', 'comet', 'copper', 'coral', 'cotton', 'crane', 'crystal', 'delta', 'derby',
    'dune', 'eagle', 'ember', 'falcon', 'fern', 'flint', 'forest', 'garden', 'garnet', 'glacier',
    'granite', 'gravel', 'harbor', 'harvest', 'hazel', 'heron', 'hollow', 'indigo', 'island', 'ivory',
    'jasper', 'juniper', 'kayak', 'kettle', 'lagoon', 'lantern', 'ledger', 'lemon', 'linen', 'lumber',
    'magnet', 'maple', 'marble', 'meadow', 'mesa', 'mortar', 'mosaic', 'nickel', 'north', 'oak',
    'ocean', 'olive', 'onyx', 'orbit', 'orchard', 'osprey', 'otter', 'paddle', 'pebble', 'pepper',
    'pillar', 'pine', 'planet', 'plaza', 'poplar', 'prairie', 'quartz', 'quiver', 'rafter', 'raven',
    'ridge', 'river', 'rocket', 'saddle', 'salmon', 'sequoia', 'shadow', 'silver', 'slate', 'spruce',
    'stone', 'summit', 'sunset', 'thistle', 'timber', 'topaz', 'trellis', 'tundra', 'valley', 'velvet',
    'walnut', 'willow', 'zenith', 'zephyr'
  ];

  function randInt(n) {
    var a = new Uint32Array(1);
    (window.crypto || window.msCrypto).getRandomValues(a);
    return a[0] % n;
  }
  function cap(w) { return w.charAt(0).toUpperCase() + w.slice(1); }

  function generate() {
    var out = [], used = {};
    while (out.length < 3) {
      var w = WORDS[randInt(WORDS.length)];
      if (used[w]) continue;
      used[w] = true;
      out.push(cap(w));
    }
    var digits = String(10 + randInt(90));   // two digits, 10–99
    return out.join('-') + '-' + digits;
  }

  function reveal(toggle) {
    // The page's eye toggle owns the icon/aria state; clicking it when the field is
    // still hidden reveals the field the same way a person would.
    if (!toggle) return;
    if (toggle.getAttribute('aria-pressed') === 'true') return;
    toggle.click();
  }

  function wire(opts) {
    if (!opts || !opts.button) return;
    var fields = opts.fields || [], toggles = opts.toggles || [];
    opts.button.addEventListener('click', function () {
      var v = generate();
      fields.forEach(function (f) {
        if (!f) return;
        f.value = v;
        f.dispatchEvent(new Event('input', { bubbles: true }));
      });
      toggles.forEach(reveal);
      if (opts.hint) opts.hint.hidden = false;
      if (typeof opts.after === 'function') { try { opts.after(); } catch (e) { } }
      if (fields[0]) fields[0].focus({ preventScroll: true });
    });
  }

  window.SSCPasswordSuggest = { generate: generate, wire: wire, WORDS: WORDS };
})();
