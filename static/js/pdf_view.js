/* Proposal PDFs shown in the page (2026-10-21): every page drawn as an image with pdf.js (vendored, see
   static/vendor/pdfjs-4.0.379/README.md), so it reads the same on a phone, a tablet and a desktop.

   SSC_PDF.render(url, host)                  draws every page of the PDF at `url` into `host`
   SSC_PDF.panel({title, sub, url, links})    a side panel over the page; links = [{label, href, download?}]
*/
(function () {
  var BASE = '/files/static/vendor/pdfjs-4.0.379/';
  var lib = null;
  function load() {
    if (!lib) lib = import(BASE + 'pdf.min.js').then(function (m) { m.GlobalWorkerOptions.workerSrc = BASE + 'pdf.worker.min.js'; return m; });
    return lib;
  }
  var css = '.sscpdf-pages{overflow:auto;background:#E4E7EE;padding:14px;display:flex;flex-direction:column;gap:12px;align-items:center}' +
    '.sscpdf-pages canvas{width:100%;max-width:860px;height:auto;background:#fff;box-shadow:0 2px 10px rgba(0,0,0,.15);border-radius:2px}' +
    '.sscpdf-msg{color:var(--muted,#8c92a0);font-size:13px;padding:28px;text-align:center}' +
    '.sscpdf-scrim{position:fixed;inset:0;background:rgba(20,24,36,.32);z-index:9150}' +
    '.sscpdf-panel{position:fixed;top:0;right:0;bottom:0;width:min(780px,100%);background:#fff;z-index:9151;display:flex;flex-direction:column;box-shadow:-10px 0 30px rgba(36,65,140,.14)}' +
    '.sscpdf-hd{display:flex;gap:8px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line,#eceef3);flex-wrap:wrap}' +
    '.sscpdf-hd b{font-size:15px}.sscpdf-hd small{color:var(--muted,#8c92a0);font-size:12.5px}.sscpdf-hd .sp{flex:1}' +
    '.sscpdf-panel .sscpdf-pages{flex:1}';
  function style() {
    if (document.getElementById('sscpdf-css')) return;
    var s = document.createElement('style'); s.id = 'sscpdf-css'; s.textContent = css; document.head.appendChild(s);
  }
  function esc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }

  var token = 0;
  async function render(url, host) {
    style();
    var mine = ++token; host.classList.add('sscpdf-pages');
    host.innerHTML = '<div class="sscpdf-msg">Loading the PDF…</div>';
    try {
      var pdfjs = await load();
      var doc = await pdfjs.getDocument({ url: url, withCredentials: true }).promise;
      if (mine !== token) return;
      host.innerHTML = '';
      var ratio = Math.min(2, (window.devicePixelRatio || 1) * 1.5);
      for (var i = 1; i <= doc.numPages; i++) {
        var page = await doc.getPage(i), vp = page.getViewport({ scale: ratio });
        var c = document.createElement('canvas'); c.width = vp.width; c.height = vp.height;
        c.setAttribute('aria-label', 'Page ' + i + ' of ' + doc.numPages);
        host.appendChild(c);
        await page.render({ canvasContext: c.getContext('2d'), viewport: vp }).promise;
        if (mine !== token) return;
      }
    } catch (e) {
      if (mine === token) host.innerHTML = '<div class="sscpdf-msg">The PDF couldn’t be shown here. Use Download to open it.</div>';
    }
  }

  function close() {
    token++;
    ['sscpdf-panel', 'sscpdf-scrim'].forEach(function (id) { var el = document.getElementById(id); if (el) el.remove(); });
    document.removeEventListener('keydown', onKey);
  }
  function onKey(e) { if (e.key === 'Escape') close(); }
  function panel(o) {
    style(); close();
    var scrim = document.createElement('div'); scrim.className = 'sscpdf-scrim'; scrim.id = 'sscpdf-scrim'; scrim.onclick = close;
    var p = document.createElement('aside'); p.className = 'sscpdf-panel'; p.id = 'sscpdf-panel';
    p.setAttribute('role', 'dialog'); p.setAttribute('aria-modal', 'true'); p.setAttribute('aria-label', o.title || 'PDF');
    p.innerHTML = '<div class="sscpdf-hd"><div><b>' + esc(o.title) + '</b>' + (o.sub ? '<br><small>' + esc(o.sub) + '</small>' : '') + '</div><span class="sp"></span>' +
      (o.links || []).map(function (l) { return '<a class="btn-ghost btn-sm" href="' + esc(l.href) + '"' + (l.download ? ' download' : '') + '>' + esc(l.label) + '</a>'; }).join('') +
      '<button class="btn-ghost btn-sm" type="button" aria-label="Close">✕</button></div><div class="sscpdf-pages"></div>';
    document.body.appendChild(scrim); document.body.appendChild(p);
    p.querySelector('button').onclick = close;
    document.addEventListener('keydown', onKey);
    render(o.url, p.querySelector('.sscpdf-pages'));
  }
  window.SSC_PDF = { render: render, panel: panel, close: close };
})();
