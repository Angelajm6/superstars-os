/* Proposal PDFs shown in the page (2026-10-21): every page drawn as an image with pdf.js (vendored, see
   static/vendor/pdfjs-4.0.379/README.md), so it reads the same on a phone, a tablet and a desktop.

   SSC_PDF.render(url, host)                  draws every page of the PDF at `url` into `host`
   SSC_PDF.panel({title, sub, url, links, kind, sheetUrl})  a side panel over the page; links = [{label, href, download?}]
                                              kind: 'pdf' (default) | 'image' | 'sheet' | 'other' (no preview, links only)
                                              'sheet' needs sheetUrl: JSON {sheets:[{name, rows, truncated, hidden}]}
                                              (the Documents tab's /sheet routes); without it a sheet falls back to 'other'
   SSC_PDF.kindOf(mime, name)                 'pdf' | 'image' | 'sheet' | 'other' for a stored file
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
    '.sscpdf-panel .sscpdf-pages{flex:1}' +
    '.sscpdf-pages img{max-width:100%;height:auto;background:#fff;box-shadow:0 2px 10px rgba(0,0,0,.15);border-radius:2px}' +
    '.sscpdf-hd>div:first-child{min-width:0;flex:1 1 220px}.sscpdf-hd b{display:block;overflow-wrap:anywhere}.sscpdf-hd br{display:none}' +
    '@media (max-width:640px){.sscpdf-hd>div:first-child{flex:1 1 calc(100% - 56px)}.sscpdf-hd .sp{display:none}' +
    '.sscpdf-hd button[aria-label=Close]{order:-1;margin-left:auto;align-self:flex-start}.sscpdf-hd>div:first-child{order:-2}' +
    '.sscpdf-hd a{flex:1;text-align:center}}' +
    '.sscpdf-sheet{flex:1;min-height:0;display:flex;flex-direction:column;background:#fff}' +
    '.sscpdf-tabs{display:flex;gap:2px;padding:8px 12px 0;border-bottom:1px solid var(--line,#eceef3);overflow-x:auto;scrollbar-width:none;flex:none}' +
    '.sscpdf-tab{font:inherit;font-size:13px;padding:7px 12px;border:1px solid transparent;border-bottom:0;border-radius:8px 8px 0 0;background:none;color:var(--muted,#8c92a0);cursor:pointer;white-space:nowrap}' +
    '.sscpdf-tab[aria-selected=true]{background:#fff;border-color:var(--line,#eceef3);color:var(--ink,#222633);font-weight:600;margin-bottom:-1px}' +
    '.sscpdf-tab span{font-weight:400;font-size:11.5px;margin-left:4px;color:var(--muted,#8c92a0)}' +
    '.sscpdf-grid{flex:1;min-height:0;overflow:auto}' +
    '.sscpdf-grid table{border-collapse:separate;border-spacing:0;font-size:12.5px;font-variant-numeric:tabular-nums}' +
    '.sscpdf-grid th,.sscpdf-grid td{border-right:1px solid var(--line,#eceef3);border-bottom:1px solid var(--line,#eceef3);padding:5px 9px;white-space:nowrap;max-width:280px;overflow:hidden;text-overflow:ellipsis;text-align:left}' +
    '.sscpdf-grid thead th{position:sticky;top:0;background:#f3f5f9;color:var(--muted,#8c92a0);font-size:11px;font-weight:600;text-align:center;z-index:2}' +
    '.sscpdf-grid tbody th{position:sticky;left:0;background:#f3f5f9;color:var(--muted,#8c92a0);font-size:11px;font-weight:500;text-align:right;min-width:34px;z-index:1}' +
    '.sscpdf-grid thead th:first-child{left:0;z-index:3}' +
    '.sscpdf-grid td.n{text-align:right}.sscpdf-grid tbody tr:first-child td{font-weight:600}' +
    '.sscpdf-foot{flex:none;padding:8px 14px;border-top:1px solid var(--line,#eceef3);color:var(--muted,#8c92a0);font-size:12px}';
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

  function kindOf(mime, name) {
    var m = String(mime || '').toLowerCase(), n = String(name || '').toLowerCase();
    if (m === 'application/pdf' || /\.pdf$/.test(n)) return 'pdf';
    if (/^image\/(png|jpe?g|gif|webp|heic|heif)$/.test(m) || /\.(png|jpe?g|gif|webp|heic|heif)$/.test(n)) return 'image';
    if (/\.(xlsx|xlsm|xls|csv)$/.test(n) || /spreadsheetml|ms-excel|^text\/csv$/.test(m)) return 'sheet';
    return 'other';
  }
  function image(url, host) {
    var mine = ++token; host.classList.add('sscpdf-pages');
    host.innerHTML = '<div class="sscpdf-msg">Loading the picture…</div>';
    var img = new Image();
    img.alt = 'Preview';
    img.onload = function () { if (mine === token) { host.innerHTML = ''; host.appendChild(img); } };
    img.onerror = function () { if (mine === token) host.innerHTML = '<div class="sscpdf-msg">This picture can’t be shown in this browser (HEIC photos open on iPhone and Mac). Use Download to open it.</div>'; };
    img.src = url;
  }

  // a spreadsheet: one tab per sheet, column letters + row numbers stay put while scrolling
  function colName(i) { var s = ''; i++; while (i > 0) { var r = (i - 1) % 26; s = String.fromCharCode(65 + r) + s; i = Math.floor((i - 1) / 26); } return s; }
  function cellText(v) { return typeof v === 'number' ? v.toLocaleString('en-US', { maximumFractionDigits: 4 }) : String(v == null ? '' : v); }
  async function sheet(url, host) {
    var mine = ++token; host.className = 'sscpdf-sheet';
    host.innerHTML = '<div class="sscpdf-msg">Loading the spreadsheet…</div>';
    var data;
    try {
      var r = await fetch(url, { credentials: 'same-origin' });
      data = await r.json();
      if (!r.ok) throw new Error((data && data.error) || 'failed');
      data = data.data || data;
    } catch (e) {
      if (mine === token) host.innerHTML = '<div class="sscpdf-msg">' + esc(e && e.message && e.message !== 'failed' ? e.message : 'This spreadsheet couldn’t be shown here. Use Download to open it.') + '</div>';
      return;
    }
    if (mine !== token) return;
    var sheets = (data && data.sheets) || [];
    if (!sheets.length) { host.innerHTML = '<div class="sscpdf-msg">This spreadsheet has no sheets to show.</div>'; return; }
    var cur = 0;
    for (var k = 0; k < sheets.length; k++) { if (!sheets[k].hidden && sheets[k].rows.length) { cur = k; break; } }
    host.innerHTML = '<div class="sscpdf-tabs" role="tablist"></div><div class="sscpdf-grid" tabindex="0"></div><div class="sscpdf-foot"></div>';
    var tabs = host.children[0], grid = host.children[1], foot = host.children[2];
    function draw() {
      tabs.hidden = sheets.length < 2;
      tabs.innerHTML = sheets.map(function (s, i) {
        return '<button class="sscpdf-tab" role="tab" type="button" data-i="' + i + '" aria-selected="' + (i === cur) + '">' + esc(s.name) +
          (s.hidden ? ' (hidden)' : '') + '<span>' + s.rows.length + (s.truncated ? '+' : '') + '</span></button>';
      }).join('');
      var s = sheets[cur], w = 0;
      s.rows.forEach(function (row) { if (row.length > w) w = row.length; });
      if (!s.rows.length) { grid.innerHTML = '<div class="sscpdf-msg">This sheet is empty.</div>'; }
      else {
        var h = ['<table><thead><tr><th></th>'];
        for (var c = 0; c < w; c++) h.push('<th>' + colName(c) + '</th>');
        h.push('</tr></thead><tbody>');
        s.rows.forEach(function (row, i) {
          h.push('<tr><th>' + (i + 1) + '</th>');
          for (var c = 0; c < w; c++) {
            var v = row[c], t = esc(cellText(v));
            h.push('<td' + (typeof v === 'number' ? ' class="n"' : '') + (t.length > 30 ? ' title="' + t + '"' : '') + '>' + t + '</td>');
          }
          h.push('</tr>');
        });
        grid.innerHTML = h.join('') + '</tbody></table>';
      }
      grid.scrollTop = 0; grid.scrollLeft = 0;
      foot.textContent = s.name + ' · ' + s.rows.length + ' rows × ' + w + ' columns' +
        (s.truncated ? ' · showing the first ' + (data.preview_rows || s.rows.length) + ' rows; Download has the rest' : '') +
        ' · values as last saved in Excel';
    }
    tabs.onclick = function (e) { var b = e.target.closest('.sscpdf-tab'); if (b) { cur = +b.getAttribute('data-i'); draw(); } };
    draw();
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
      (o.links || []).map(function (l) { return '<a class="btn-ghost btn-sm" href="' + esc(l.href) + '"' + (l.download ? ' download' : '') + (l.newtab ? ' target="_blank" rel="noopener"' : '') + '>' + esc(l.label) + '</a>'; }).join('') +
      '<button class="btn-ghost btn-sm" type="button" aria-label="Close">✕</button></div><div class="sscpdf-pages"></div>';
    document.body.appendChild(scrim); document.body.appendChild(p);
    p.querySelector('button').onclick = close;
    document.addEventListener('keydown', onKey);
    var host = p.querySelector('.sscpdf-pages');
    if (o.kind === 'image') image(o.url, host);
    else if (o.kind === 'sheet' && o.sheetUrl) sheet(o.sheetUrl, host);
    else if (o.kind === 'other' || o.kind === 'sheet') { host.innerHTML = '<div class="sscpdf-msg">This kind of file can’t be previewed here. Use Download to open it.</div>'; }
    else render(o.url, host);
  }
  window.SSC_PDF = { render: render, panel: panel, close: close, kindOf: kindOf };
})();
