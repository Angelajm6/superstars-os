/* Company → Specs (2026-11-13 revamp; accepted prototype https://claude.ai/artifact/9WFt7rm2HWsV8Rp41MisNc).
   Catalog: search by name or by the work, category chips, go-to / on a job / data sheet filters, a product panel
   (notes, TDS + SDS, the jobs it is on, Ask Claude). By job: a job's products, where each is used, the
   submittal package. API: spec_library.py. Dates shown as MM/DD/YYYY from the server's local strings. */
(function () {
  'use strict';
  var CATS = {   // catalog category -> chip label + the words people search by
    'Concrete Repair & Protection — Mortars': ['Repair mortars', 'spall patch crack concrete repair mortar delamination slab edge balcony rebar corrosion bonding parapet'],
    'Concrete Repair & Protection — Coatings & Water Repellents': ['Coatings & repellents', 'coating water repellent sealer facade masonry brick paint topcoat elastomeric anti-carbonation'],
    'Structural Strengthening': ['Strengthening', 'carbon fiber frp strengthening epoxy injection crack structural beam column'],
    'Grouting & Anchoring': ['Grout & anchors', 'grout anchor dowel rebar bolt baseplate railing post'],
    'Sealing & Bonding': ['Sealants & adhesives', 'sealant joint caulk window expansion coping glazing backer primer adhesive'],
    'Waterproofing': ['Waterproofing', 'waterproof leak membrane below grade balcony deck plaza water'],
    'Flooring': ['Flooring', 'floor epoxy garage traffic coating primer leveler'],
    'Roofing': ['Roofing', 'roof membrane pvc tpo insulation leak'],
    'Concrete Admixtures': ['Concrete admixtures', 'admixture concrete mix accelerator plasticizer'],
    'Industry — Sealing & Bonding': ['Industrial', 'industrial adhesive'],
    'Other': ['Other', '']
  };
  var LINEX = { 'Sika CarboDur': 'carbon fiber', 'SikaWrap': 'carbon fiber wrap', 'Sikadur': 'epoxy injection crack',
    'Bonding agents / corrosion': 'rebar corrosion bonding primer', 'Sika AnchorFix': 'rebar anchor dowel',
    'Ancillaries': 'backer rod primer joint', 'Sikaflex': 'joint window coping', 'Sikasil': 'glazing window joint' };
  var ORDER = Object.keys(CATS);
  var P = [], JOBS = [], ALLCATS = [];
  var st = { view: 'catalog', cat: '', q: '', fav: false, onjob: false, ourpdf: false, open: null, job: '', adding: null,
             loaded: false, loading: null, uploadKind: null, mFile: null };

  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function mdy(iso) { var m = String(iso || '').match(/^(\d{4})-(\d{2})-(\d{2})/); return m ? m[2] + '/' + m[3] + '/' + m[1] : ''; }
  function fsize(n) { if (!n) return ''; return n < 1048576 ? Math.max(1, Math.round(n / 1024)) + ' KB' : (n / 1048576).toFixed(1) + ' MB'; }
  function label(cat) { return (CATS[cat] || [cat])[0]; }
  function catRank(cat) { var i = ORDER.indexOf(cat); return i < 0 ? 99 : i; }
  function byId(id) { for (var i = 0; i < P.length; i++) if (P[i].id === id) return P[i]; return null; }
  function job(code) { for (var i = 0; i < JOBS.length; i++) if (JOBS[i].project_code === code) return JOBS[i]; return null; }
  function openJobs() { return JOBS.filter(function (j) { return String(j.status || '').toLowerCase() !== 'closed'; }); }
  function onJob(p, code) { return p.jobs.some(function (l) { return l.project_code === code; }); }

  async function api(url, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET', credentials: 'include', headers: {} };
    if (opts.form) init.body = opts.form;
    else if (opts.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    var r = await fetch(url, init);
    var j = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error((j && j.error) || ('Something went wrong (' + r.status + ').'));
    return j.data;
  }

  // ---------------------------------------------------------------- toast
  var tT;
  function toast(msg, undo) {
    $('spx-toast-t').textContent = msg;
    var u = $('spx-toast-u'); u.hidden = !undo;
    u.onclick = function () { hide(); if (undo) undo(); };
    $('spx-toast').classList.add('on'); clearTimeout(tT); tT = setTimeout(hide, 5000);
    function hide() { $('spx-toast').classList.remove('on'); }
  }
  function fail(e) { toast(e && e.message ? e.message : 'Something went wrong. Try again.'); }

  // ---------------------------------------------------------------- load
  async function load() {
    if (st.loading) return st.loading;
    st.loading = (async function () {
      try {
        var res = await Promise.all([api('/api/spec-products?manufacturer='), api('/api/projects').catch(function () { return []; })]);
        P = res[0].items || []; ALLCATS = res[0].categories || [];
        JOBS = (Array.isArray(res[1]) ? res[1] : []).filter(function (j) { return j && j.project_code; });
        if (!st.job) { var o = openJobs(); st.job = (o[0] || JOBS[0] || {}).project_code || ''; }
        st.loaded = true; render(); renderPanel();
      } catch (e) {
        $('spx-count').textContent = '';
        $('spx-results').innerHTML = '<div class="spx-list"><div class="spx-empty"><b>The catalog didn\'t load</b>' + esc(e.message) + '</div></div>';
      } finally { st.loading = null; }
    })();
    return st.loading;
  }

  // ---------------------------------------------------------------- catalog
  function hay(p) {
    return [p.product_name, p.product_line, p.manufacturer, p.description, p.notes, p.product_code, (CATS[p.category] || ['', ''])[1],
            LINEX[p.product_line] || '', p.jobs.map(function (l) { return l.project_code + ' ' + l.where; }).join(' ')].join(' ').toLowerCase();
  }
  function matches(p, ignoreCat) {
    if (!ignoreCat && st.cat && p.category !== st.cat) return false;
    if (st.fav && !p.is_favorite) return false;
    if (st.onjob && !p.jobs.length) return false;
    if (st.ourpdf && !p.files.tds) return false;
    if (!st.q) return true;
    var h = hay(p);
    return st.q.toLowerCase().split(/\s+/).filter(Boolean).every(function (w) { return h.indexOf(w.replace(/s$/, '')) >= 0; });
  }
  var STAR = function (on, px) { px = px || 18; return '<svg width="' + px + '" height="' + px + '" viewBox="0 0 24 24" fill="' + (on ? 'currentColor' : 'none') + '" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.2l2.7 5.6 6.1.8-4.5 4.2 1.1 6-5.4-2.9-5.4 2.9 1.1-6-4.5-4.2 6.1-.8z"/></svg>'; };
  var PLUS = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';

  function renderHead() {
    var used = P.filter(function (p) { return p.jobs.length; }).length, sheets = P.filter(function (p) { return p.files.tds; }).length;
    $('spx-sub').textContent = P.length + ' products · ' + used + ' on jobs · ' + sheets + ' data sheet' + (sheets === 1 ? '' : 's') + ' on file';
  }
  function renderChips() {
    var pool = P.filter(function (p) { return matches(p, true); }), counts = {};
    pool.forEach(function (p) { counts[p.category] = (counts[p.category] || 0) + 1; });
    var cats = Object.keys(counts); if (st.cat && cats.indexOf(st.cat) < 0) cats.push(st.cat);
    cats.sort(function (a, b) { return catRank(a) - catRank(b) || a.localeCompare(b); });
    $('spx-cats').innerHTML = '<button type="button" class="spx-chip' + (st.cat ? '' : ' on') + '" data-spxcat="">All<span class="n">' + pool.length + '</span></button>'
      + cats.map(function (c) { return '<button type="button" class="spx-chip' + (st.cat === c ? ' on' : '') + '" data-spxcat="' + esc(c) + '">' + esc(label(c)) + '<span class="n">' + (counts[c] || 0) + '</span></button>'; }).join('');
    var tg = [['fav', '<span style="display:inline-block;vertical-align:-2px;margin-right:4px">' + STAR(true, 13) + '</span>Our go-to'], ['onjob', 'On a job'], ['ourpdf', 'Data sheet on file']];
    $('spx-togs').innerHTML = tg.map(function (t) { return '<button type="button" class="spx-chip tog' + (st[t[0]] ? ' on' : '') + '" data-spxtog="' + t[0] + '" aria-pressed="' + !!st[t[0]] + '">' + t[1] + '</button>'; }).join('');
  }
  function rowHTML(p) {
    var js = p.jobs, inAdd = st.adding && onJob(p, st.adding);
    var jt = js.length ? '<span class="spx-tag">' + (js.length === 1 ? esc(js[0].project_code) : js.length + ' jobs') + '</span>' : '';
    var dt = p.files.tds ? '<span class="spx-tag doc">PDF</span>' : '';
    var btn = st.adding
      ? '<button type="button" class="btn-' + (inAdd ? 'ghost' : 'primary') + ' btn-sm" data-spxquick="' + p.id + '">' + (inAdd ? 'Added ✓' : 'Add') + '</button>'
      : '<button type="button" class="spx-plus" data-spxaddjob="' + p.id + '" aria-label="Add ' + esc(p.product_name) + ' to a job" title="Add to a job">' + PLUS + '</button>';
    return '<div class="spx-row' + (st.open === p.id ? ' sel' : '') + '" data-spxopen="' + p.id + '">'
      + '<button type="button" class="spx-star' + (p.is_favorite ? ' on' : '') + '" data-spxstar="' + p.id + '" aria-pressed="' + p.is_favorite + '" aria-label="' + (p.is_favorite ? 'Remove from' : 'Add to') + ' the go-to list">' + STAR(p.is_favorite) + '</button>'
      + '<div style="min-width:0"><div class="spx-pn">' + esc(p.product_name) + '</div><div class="spx-pd">' + esc(p.notes || p.description || p.product_line || '') + '</div></div>'
      + '<div class="spx-rt">' + dt + jt + btn + '</div></div>';
  }
  function score(p) {   // own words (name, line, notes, what it is) outrank a category keyword
    if (!st.q) return 0;
    var own = [p.product_name, p.product_line, p.notes, p.description, LINEX[p.product_line] || ''].join(' ').toLowerCase();
    return st.q.toLowerCase().split(/\s+/).filter(Boolean).reduce(function (n, w) { return n + (own.indexOf(w.replace(/s$/, '')) >= 0 ? 1 : 0); }, 0);
  }
  function renderResults() {
    var list = P.filter(function (p) { return matches(p); });
    var sc = {};
    if (st.q) { list.forEach(function (p) { sc[p.id] = score(p); }); list.sort(function (a, b) { return sc[b.id] - sc[a.id]; }); }
    $('spx-count').textContent = list.length === P.length ? list.length + ' products' : list.length + ' of ' + P.length + ' products';
    $('spx-clear').hidden = !(st.cat || st.q || st.fav || st.onjob || st.ourpdf);
    if (!list.length) {
      $('spx-results').innerHTML = '<div class="spx-list"><div class="spx-empty"><b>Nothing matches ' + (st.q ? '“' + esc(st.q) + '”' : 'these filters') + '</b>Try a broader word, or add the product to the catalog.<div style="margin-top:12px"><button type="button" class="btn-ghost" data-spxact="addnew">+ Add product</button></div></div></div>';
      return;
    }
    var html = '';
    if (st.cat) {   // inside one category: grouped by product line, with the line's summary
      var lines = []; list.forEach(function (p) { var l = p.product_line || 'Other'; if (lines.indexOf(l) < 0) lines.push(l); });
      html = lines.map(function (l) {
        var items = list.filter(function (p) { return (p.product_line || 'Other') === l; });
        var sum = items[0].description && items.every(function (p) { return p.description === items[0].description; }) ? ' · ' + esc(items[0].description) : '';
        return '<div class="spx-group"><div class="spx-gh">' + esc(l) + '<span>' + sum + '</span></div><div class="spx-list">' + items.map(rowHTML).join('') + '</div></div>';
      }).join('');
    } else {
      var cats = []; list.forEach(function (p) { if (cats.indexOf(p.category) < 0) cats.push(p.category); });
      var best = {}; if (st.q) list.forEach(function (p) { best[p.category] = Math.max(best[p.category] || 0, sc[p.id]); });
      cats.sort(function (a, b) { return (best[b] || 0) - (best[a] || 0) || catRank(a) - catRank(b) || a.localeCompare(b); });
      html = cats.map(function (c) {
        var items = list.filter(function (p) { return p.category === c; });
        return '<div class="spx-group"><div class="spx-gh">' + esc(label(c)) + '<span>' + items.length + '</span></div><div class="spx-list">' + items.map(rowHTML).join('') + '</div></div>';
      }).join('');
    }
    $('spx-results').innerHTML = html;
  }
  function renderBanner() {
    $('spx-banner').innerHTML = st.adding ? '<div class="spx-banner"><span>Adding products to <b>' + esc(st.adding) + '</b>. Tap <b>Add</b> on each one.</span><button type="button" class="btn-primary btn-sm" data-spxact="doneadd">Done</button></div>' : '';
  }

  // ---------------------------------------------------------------- product panel
  function docBlock(p, kind, name) {
    var d = p.files[kind];
    if (d) return '<div class="spx-doc"><div class="ic pdf">PDF</div><div class="dt"><b>' + name + '</b><span>' + esc(d.file_name) + ' · ' + fsize(d.file_size) + (d.uploaded_at ? ' · added ' + mdy(d.uploaded_at) : '') + '</span></div>'
      + '<a class="spx-lnk" href="' + esc(d.file_url) + '" target="_blank" rel="noopener">View</a><button type="button" class="spx-lnk" data-spxupload="' + kind + '">Replace</button><button type="button" class="spx-lnk red" data-spxdelfile="' + kind + '" aria-label="Remove ' + name + '">Remove</button></div>';
    return '<div class="spx-doc drop" data-spxupload="' + kind + '" data-spxdrop="' + kind + '" tabindex="0" role="button"><div class="ic">PDF</div><div class="dt"><b>' + name + '</b><span>Not on file yet. Drop the PDF or tap to upload.</span></div><span class="spx-lnk">Upload</span></div>';
  }
  function renderPanel() {
    var p = byId(st.open), el = $('spx-panel');
    if (!p) { el.classList.remove('on'); if (!$('spx-modal').classList.contains('on')) $('spx-scrim').classList.remove('on'); return; }
    var free = openJobs().filter(function (j) { return !onJob(p, j.project_code); });
    el.innerHTML = '<div class="spx-ph"><button type="button" class="spx-star' + (p.is_favorite ? ' on' : '') + '" data-spxstar="' + p.id + '" style="margin-top:14px" aria-pressed="' + p.is_favorite + '" aria-label="Go-to list">' + STAR(p.is_favorite) + '</button>'
      + '<div style="min-width:0"><div class="eb">' + esc([p.manufacturer, label(p.category), p.product_line].filter(Boolean).join(' · ')) + '</div><h3>' + esc(p.product_name) + '</h3></div>'
      + '<button type="button" class="spx-x" data-spxact="close" aria-label="Close">×</button></div>'
      + '<div class="spx-pb">'
      + '<div class="spx-sec"><h4>What it is</h4>' + (p.description ? '<p class="spx-for">' + esc(p.description) + '</p>' : '')
      + '<label class="spx-lbl" for="spx-p-note">Our notes (crew tips, where we use it, what to avoid)</label>'
      + '<textarea class="ssc-field" id="spx-p-note" placeholder="e.g. Don\'t apply below 40°F. Order the 50 lb bags.">' + esc(p.notes || '') + '</textarea><div class="spx-saved" id="spx-p-saved"></div></div>'
      + '<div class="spx-sec"><h4>Documents</h4>' + docBlock(p, 'tds', 'Data sheet (TDS)') + docBlock(p, 'sds', 'Safety data sheet (SDS)')
      + (p.spec_url ? '<div class="spx-doc"><div class="ic">WEB</div><div class="dt"><b>' + esc(p.manufacturer) + ' product page</b><span>' + esc(p.spec_url.replace(/^https?:\/\//, '')) + '</span></div><a class="spx-lnk" href="' + esc(p.spec_url) + '" target="_blank" rel="noopener">Open ↗</a></div>' : '')
      + '</div>'
      + '<div class="spx-sec"><h4>On jobs <span>' + (p.jobs.length || 'None yet') + '</span></h4>'
      + p.jobs.map(function (l) { return '<div class="spx-jr"><span class="jc">' + esc(l.project_code) + '</span><span class="jw">' + esc(l.where || 'No note') + '</span><button type="button" class="spx-lnk red" data-spxunlink="' + esc(l.project_code) + '">Remove</button></div>'; }).join('')
      + (free.length ? '<div class="spx-adder"><div class="r"><select class="ssc-field" id="spx-p-job" aria-label="Job">' + free.map(function (j) { return '<option value="' + esc(j.project_code) + '">' + esc(j.project_code) + '</option>'; }).join('') + '</select>'
          + '<input class="ssc-field" id="spx-p-where" placeholder="Where it\'s used (optional)" aria-label="Where it\'s used" autocomplete="off"></div>'
          + '<button type="button" class="btn-primary" data-spxact="link">Add to job</button></div>'
        : (openJobs().length ? '<div class="spx-note">On every open job.</div>' : '<div class="spx-note">No open jobs.</div>'))
      + '<div class="spx-note">It shows on the job\'s Documents tab, with the data sheet.</div></div>'
      + '<div class="spx-sec"><h4>Ask Claude</h4><div class="spx-ask">'
      + '<button type="button" data-spxask="qty">How much do I need?</button><button type="button" data-spxask="cure">Cure time before coating?</button>'
      + '<button type="button" data-spxask="temp">Temperature limits?</button><button type="button" data-spxask="alt">What else could I use?</button></div>'
      + '<div class="spx-note">' + (p.files.tds ? 'Claude answers from our data sheet on file.' : 'Upload the data sheet and Claude answers from it, with the page cited.') + '</div></div>'
      + '</div>';
    el.classList.add('on'); $('spx-scrim').classList.add('on');
  }
  function closePanel() { st.open = null; renderPanel(); if (st.view === 'catalog') renderResults(); }

  // ---------------------------------------------------------------- by job
  function renderJob() {
    var codes = openJobs().map(function (j) { return j.project_code; });
    P.forEach(function (p) { p.jobs.forEach(function (l) { if (codes.indexOf(l.project_code) < 0) codes.push(l.project_code); }); });
    if (st.job && codes.indexOf(st.job) < 0) codes.unshift(st.job);
    var count = function (c) { return P.filter(function (p) { return onJob(p, c); }).length; };
    $('spx-jobchips').innerHTML = codes.map(function (c) { return '<button type="button" class="spx-chip' + (st.job === c ? ' on' : '') + '" data-spxjob="' + esc(c) + '">' + esc(c) + '<span class="n">' + count(c) + '</span></button>'; }).join('');
    if (!st.job) { $('spx-jobbody').innerHTML = '<div class="spx-list"><div class="spx-empty"><b>No jobs yet</b>Jobs appear here once they are set up in Projects.</div></div>'; return; }
    var j = job(st.job) || { project_code: st.job };
    var ps = P.filter(function (p) { return onJob(p, st.job); }).sort(function (a, b) { return catRank(a.category) - catRank(b.category) || a.product_name.localeCompare(b.product_name); });
    var onfile = ps.filter(function (p) { return p.files.tds; }).length, q = encodeURIComponent(st.job);
    var body = '<div class="spx-jobhead"><div><div class="t">' + esc(j.project_code) + (j.name ? ' · ' + esc(j.name) : '') + '</div><div class="s">' + esc(j.address || '') + '</div></div>'
      + '<div class="spx-stats"><span><b>' + ps.length + '</b> product' + (ps.length === 1 ? '' : 's') + '</span><span><b>' + onfile + '</b> data sheet' + (onfile === 1 ? '' : 's') + ' on file</span>' + (ps.length - onfile ? '<span style="color:var(--status-warn,#9A5B00)"><b>' + (ps.length - onfile) + '</b> missing</span>' : '') + '</div>'
      + '<div class="spx-acts">' + (ps.length ? '<a class="btn-ghost" href="/api/projects/' + q + '/document-specs/submittal.pdf" target="_blank" rel="noopener" data-spxsubmittal="1">Submittal package</a>' : '')
      + '<button type="button" class="btn-primary" data-spxact="startadd">+ Add products</button></div></div>';
    if (!ps.length) {
      body += '<div class="spx-list"><div class="spx-empty"><b>No products on ' + esc(st.job) + ' yet</b>Add what the crew will use. Each one shows on the job\'s Documents tab with its data sheet.</div></div>';
    } else {
      var cats = []; ps.forEach(function (p) { if (cats.indexOf(p.category) < 0) cats.push(p.category); });
      body += cats.map(function (c) {
        return '<div class="spx-group"><div class="spx-gh">' + esc(label(c)) + '</div><div class="spx-list">' + ps.filter(function (p) { return p.category === c; }).map(function (p) {
          var l = p.jobs.filter(function (x) { return x.project_code === st.job; })[0] || {};
          return '<div class="spx-jrow"><div style="min-width:0;cursor:pointer" data-spxopen="' + p.id + '"><div class="spx-pn">' + esc(p.product_name) + '</div><div class="spx-pd">' + esc(p.product_line || p.manufacturer) + '</div></div>'
            + '<input class="spx-where" value="' + esc(l.where || '') + '" placeholder="Where it\'s used…" data-spxwhere="' + p.id + '" aria-label="Where ' + esc(p.product_name) + ' is used" maxlength="300">'
            + '<span class="jdoc">' + (p.files.tds ? '<span class="spx-tag ok">Data sheet ✓</span>' : '<span class="spx-tag miss">No data sheet</span>') + '</span>'
            + '<button type="button" class="spx-lnk red" data-spxjobunlink="' + p.id + '" aria-label="Remove ' + esc(p.product_name) + ' from ' + esc(st.job) + '">Remove</button></div>';
        }).join('') + '</div></div>';
      }).join('');
      body += '<div class="spx-note">This list shows on ' + esc(st.job) + '\'s Documents tab. The submittal package is one PDF: a cover sheet with this list, then every data sheet on file.</div>';
    }
    $('spx-jobbody').innerHTML = body;
  }

  function render() {
    if (!st.loaded) return;
    renderHead();
    $('spx-catalog').hidden = st.view !== 'catalog'; $('spx-job').hidden = st.view !== 'job';
    document.querySelectorAll('[data-spxview]').forEach(function (b) { var on = b.getAttribute('data-spxview') === st.view; b.classList.toggle('on', on); b.setAttribute('aria-selected', on); });
    if (st.view === 'catalog') { renderBanner(); renderChips(); renderResults(); } else renderJob();
  }

  // ---------------------------------------------------------------- actions
  async function setFav(p) {
    var was = p.is_favorite; p.is_favorite = !was; render(); renderPanel();
    try { await api('/api/spec-products/' + p.id, { method: 'PATCH', body: { is_favorite: p.is_favorite } }); toast(p.is_favorite ? p.product_name + ' is on the go-to list' : p.product_name + ' is off the go-to list'); }
    catch (e) { p.is_favorite = was; render(); renderPanel(); fail(e); }
  }
  async function link(p, code, where, quiet) {
    if (onJob(p, code)) return;
    p.jobs.push({ project_code: code, where: where || '' }); render(); renderPanel();
    try {
      await api('/api/projects/' + encodeURIComponent(code) + '/document-specs', { method: 'POST', body: { spec_product_id: p.id, where: where || '' } });
      if (!quiet) toast('Added ' + p.product_name + ' to ' + code, function () { unlink(p, code, true); });
    } catch (e) { p.jobs = p.jobs.filter(function (l) { return l.project_code !== code; }); render(); renderPanel(); fail(e); }
  }
  async function unlink(p, code, quiet) {
    var old = p.jobs.filter(function (l) { return l.project_code === code; })[0]; if (!old) return;
    p.jobs = p.jobs.filter(function (l) { return l !== old; }); render(); renderPanel();
    try {
      await api('/api/projects/' + encodeURIComponent(code) + '/document-specs/' + p.id, { method: 'DELETE' });
      if (!quiet) toast('Removed ' + p.product_name + ' from ' + code, function () { link(p, code, old.where, true); });
    } catch (e) { p.jobs.push(old); render(); renderPanel(); fail(e); }
  }
  async function upload(p, kind, file) {
    if (!file) return;
    if (!/\.pdf$/i.test(file.name || '')) { toast('Upload the data sheet as a PDF.'); return; }
    var fd = new FormData(); fd.append('file', file); fd.append('kind', kind);
    toast('Uploading ' + file.name + '…');
    try { p.files[kind] = await api('/api/spec-products/' + p.id + '/files', { method: 'POST', form: fd }); render(); renderPanel(); toast((kind === 'tds' ? 'Data sheet' : 'Safety data sheet') + ' saved to ' + p.product_name); }
    catch (e) { fail(e); }
  }
  async function delFile(p, kind) {
    try { await api('/api/spec-products/' + p.id + '/files/' + kind, { method: 'DELETE' }); p.files[kind] = null; render(); renderPanel(); toast((kind === 'tds' ? 'Data sheet' : 'Safety data sheet') + ' removed'); }
    catch (e) { fail(e); }
  }
  function ask(p, k) {
    var c = window.SSC_CLAUDE; if (!c || !c.ask) { toast('Claude isn\'t available on this page.'); return; }
    var n = p.product_name + ' (Specs Library product ' + p.id + ')';
    var q = { qty: ['How much ' + n + ' do I need for ', false], cure: ['Per the data sheet, how long does ' + n + ' need to cure before it can be coated?', true],
              temp: ['Per the data sheet, what are the temperature and weather limits for applying ' + n + '?', true],
              alt: ['What else in our Specs Library could I use instead of ' + n + ', and how do they differ?', true] }[k];
    closePanel(); c.ask(q[0], q[1]);
  }

  // popover: tick jobs from a row
  var pop = null;
  function closePop() { if (pop) { pop.remove(); pop = null; } }
  function openPop(btn, p) {
    closePop(); var jobs = openJobs(); var r = btn.getBoundingClientRect();
    pop = document.createElement('div'); pop.className = 'spx-pop'; pop.setAttribute('role', 'dialog'); pop.setAttribute('aria-label', 'Jobs for ' + p.product_name);
    pop.innerHTML = '<h4>On jobs</h4>' + (jobs.length ? jobs.map(function (j) { return '<label><input type="checkbox" data-spxpj="' + esc(j.project_code) + '"' + (onJob(p, j.project_code) ? ' checked' : '') + '><span>' + esc(j.project_code) + '<small>' + esc(j.name || j.address || '') + '</small></span></label>'; }).join('') : '<div class="spx-note" style="padding:0 8px 8px">No open jobs.</div>');
    document.body.appendChild(pop);
    var w = pop.offsetWidth, h = pop.offsetHeight, left = Math.max(12, Math.min(r.right - w, innerWidth - w - 12)), top = r.bottom + 6;
    if (top + h > innerHeight - 12) top = Math.max(12, r.top - h - 6);
    pop.style.left = left + 'px'; pop.style.top = top + 'px';
    pop.addEventListener('change', function (e) { var c = e.target.getAttribute('data-spxpj'); if (!c) return; if (e.target.checked) link(p, c, ''); else unlink(p, c); });
  }

  // add-product modal
  function openModal() {
    ['spx-m-name', 'spx-m-line', 'spx-m-url', 'spx-m-desc'].forEach(function (i) { $(i).value = ''; });
    $('spx-m-mfr').value = 'Sika'; $('spx-m-err').textContent = ''; st.mFile = null; $('spx-m-file').value = '';
    $('spx-m-drop-t').textContent = 'Drop the data sheet here'; $('spx-m-drop-s').textContent = 'or tap to choose. Claude reads it and fills in the form.';
    var cats = ALLCATS.slice(); if (cats.indexOf('Other') < 0) cats.push('Other');
    cats.sort(function (a, b) { return catRank(a) - catRank(b) || a.localeCompare(b); });
    $('spx-m-cat').innerHTML = cats.map(function (c) { return '<option value="' + esc(c) + '"' + (c === st.cat ? ' selected' : '') + '>' + esc(label(c)) + '</option>'; }).join('');
    var m = []; P.forEach(function (p) { if (m.indexOf(p.manufacturer) < 0) m.push(p.manufacturer); });
    $('spx-mfrs').innerHTML = m.map(function (x) { return '<option value="' + esc(x) + '"></option>'; }).join('');
    $('spx-modal').classList.add('on'); $('spx-scrim').classList.add('on'); setTimeout(function () { $('spx-m-name').focus(); }, 50);
  }
  function closeModal() { $('spx-modal').classList.remove('on'); st.mFile = null; if (!st.open) $('spx-scrim').classList.remove('on'); }
  async function readSheet(file) {
    if (!file) return;
    if (!/\.pdf$/i.test(file.name || '')) { $('spx-m-err').textContent = 'The data sheet has to be a PDF.'; return; }
    st.mFile = file; $('spx-m-err').textContent = '';
    $('spx-m-drop-t').textContent = file.name; $('spx-m-drop-s').textContent = 'Claude is reading it…';
    var fd = new FormData(); fd.append('file', file);
    try {
      var d = await api('/api/spec-products/read-datasheet', { method: 'POST', form: fd });
      if (d.manufacturer) $('spx-m-mfr').value = d.manufacturer;
      if (d.product_name) $('spx-m-name').value = d.product_name;
      if (d.product_line) $('spx-m-line').value = d.product_line;
      if (d.summary) $('spx-m-desc').value = d.summary;
      if (d.category) { var o = [].slice.call($('spx-m-cat').options).filter(function (x) { return x.value === d.category; })[0]; if (o) $('spx-m-cat').value = d.category; }
      $('spx-m-drop-s').textContent = 'Claude filled in the form. Check it, then add. The sheet is saved with the product.';
    } catch (e) { $('spx-m-drop-s').textContent = 'Saved with the product when you add it.'; $('spx-m-err').textContent = e.message; }
  }
  async function saveModal() {
    var name = $('spx-m-name').value.trim();
    if (!name) { $('spx-m-err').textContent = 'Give the product a name.'; $('spx-m-name').focus(); return; }
    var b = $('spx-m-save'); b.disabled = true;
    try {
      var p = await api('/api/spec-products', { method: 'POST', body: { manufacturer: $('spx-m-mfr').value.trim(), product_name: name,
        category: $('spx-m-cat').value, product_line: $('spx-m-line').value.trim(), description: $('spx-m-desc').value.trim(), spec_url: $('spx-m-url').value.trim() } });
      var file = st.mFile; P.push(p); closeModal();
      Object.assign(st, { view: 'catalog', cat: p.category, q: '', fav: false, onjob: false, ourpdf: false, open: p.id }); $('spx-q').value = '';
      render(); renderPanel(); toast('Added ' + p.product_name + ' to the catalog');
      if (file) upload(p, 'tds', file);
    } catch (e) { $('spx-m-err').textContent = e.message; }
    finally { b.disabled = false; }
  }

  // ---------------------------------------------------------------- wiring
  var wired = false;
  function wire() {
    if (wired) return; wired = true;
    document.addEventListener('click', function (e) {
      var t = e.target; if (!t.closest) return;
      if (pop && !pop.contains(t) && !t.closest('[data-spxaddjob]')) closePop();
      var scope = t.closest('[data-panel="specs"], #spx-panel, #spx-modal, #spx-scrim'); if (!scope) return;
      var el, g = function (s) { return t.closest(s); };
      if (scope.id === 'spx-scrim') { closePanel(); closeModal(); return; }
      if ((el = g('[data-spxstar]'))) { e.stopPropagation(); setFav(byId(+el.getAttribute('data-spxstar'))); return; }
      if ((el = g('[data-spxaddjob]'))) { e.stopPropagation(); if (pop) closePop(); else openPop(el, byId(+el.getAttribute('data-spxaddjob'))); return; }
      if ((el = g('[data-spxquick]'))) { var qp = byId(+el.getAttribute('data-spxquick')); if (onJob(qp, st.adding)) unlink(qp, st.adding, true); else link(qp, st.adding, '', true); return; }
      if ((el = g('[data-spxcat]'))) { st.cat = el.getAttribute('data-spxcat'); render(); return; }
      if ((el = g('[data-spxtog]'))) { var k = el.getAttribute('data-spxtog'); st[k] = !st[k]; render(); return; }
      if ((el = g('[data-spxq]'))) { st.q = el.getAttribute('data-spxq'); $('spx-q').value = st.q; st.cat = ''; render(); return; }
      if ((el = g('[data-spxview]'))) { st.view = el.getAttribute('data-spxview'); st.adding = null; render(); return; }
      if ((el = g('[data-spxjob]'))) { st.job = el.getAttribute('data-spxjob'); render(); return; }
      if ((el = g('[data-spxunlink]'))) { unlink(byId(st.open), el.getAttribute('data-spxunlink')); return; }
      if ((el = g('[data-spxjobunlink]'))) { unlink(byId(+el.getAttribute('data-spxjobunlink')), st.job); return; }
      if ((el = g('[data-spxdelfile]'))) { delFile(byId(st.open), el.getAttribute('data-spxdelfile')); return; }
      if ((el = g('[data-spxupload]'))) { st.uploadKind = el.getAttribute('data-spxupload'); $('spx-file').value = ''; $('spx-file').click(); return; }
      if ((el = g('[data-spxask]'))) { ask(byId(st.open), el.getAttribute('data-spxask')); return; }
      if (t.closest('#spx-m-drop')) { $('spx-m-file').click(); return; }
      if ((el = g('[data-spxact]'))) {
        var a = el.getAttribute('data-spxact');
        if (a === 'close') closePanel();
        else if (a === 'link') { var lp = byId(st.open); link(lp, $('spx-p-job').value, $('spx-p-where').value.trim()); }
        else if (a === 'startadd') { Object.assign(st, { adding: st.job, view: 'catalog', cat: '', q: '', fav: false, onjob: false, ourpdf: false }); $('spx-q').value = ''; render(); scope.scrollIntoView({ block: 'start' }); }
        else if (a === 'doneadd') { st.job = st.adding; st.adding = null; st.view = 'job'; render(); }
        else if (a === 'addnew') openModal();
        return;
      }
      if ((el = g('[data-spxopen]'))) { st.open = +el.getAttribute('data-spxopen'); renderPanel(); if (st.view === 'catalog') renderResults(); }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { if (pop) closePop(); else if ($('spx-modal').classList.contains('on')) closeModal(); else if (st.open) closePanel(); }
      if ((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('[data-spxdrop], #spx-m-drop')) { e.preventDefault(); e.target.click(); }
      if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || '')) {
        var pnl = document.querySelector('.panel.active[data-panel="specs"]'); if (pnl && st.view === 'catalog') { e.preventDefault(); $('spx-q').focus(); }
      }
    });
    var qT; $('spx-q').addEventListener('input', function (e) { clearTimeout(qT); qT = setTimeout(function () { st.q = e.target.value.trim(); render(); }, 120); });
    $('spx-clear').addEventListener('click', function () { Object.assign(st, { cat: '', q: '', fav: false, onjob: false, ourpdf: false }); $('spx-q').value = ''; render(); });
    $('spx-add').addEventListener('click', openModal);
    $('spx-ask').addEventListener('click', function () { var c = window.SSC_CLAUDE; if (c && c.ask) c.ask('Which product in our Specs Library should I use for ', false); else toast('Claude isn\'t available on this page.'); });
    $('spx-m-cancel').addEventListener('click', closeModal);
    $('spx-m-save').addEventListener('click', saveModal);
    $('spx-m-file').addEventListener('change', function (e) { readSheet(e.target.files[0]); });
    $('spx-file').addEventListener('change', function (e) { var p = byId(st.open); if (p && st.uploadKind) upload(p, st.uploadKind, e.target.files[0]); });
    var nT;
    document.addEventListener('input', function (e) {
      if (e.target.id === 'spx-p-note') {
        var p = byId(st.open), v = e.target.value; clearTimeout(nT); $('spx-p-saved').textContent = '';
        nT = setTimeout(async function () {
          try { await api('/api/spec-products/' + p.id, { method: 'PATCH', body: { notes: v } }); p.notes = v.trim() || null; var s = $('spx-p-saved'); if (s) s.textContent = 'Saved'; if (st.view === 'catalog') renderResults(); }
          catch (x) { fail(x); }
        }, 700);
      }
    });
    document.addEventListener('change', async function (e) {
      var id = e.target.getAttribute && e.target.getAttribute('data-spxwhere'); if (!id) return;
      var p = byId(+id), l = p && p.jobs.filter(function (x) { return x.project_code === st.job; })[0]; if (!l) return;
      var v = e.target.value.trim();
      try { await api('/api/projects/' + encodeURIComponent(st.job) + '/document-specs/' + p.id, { method: 'PATCH', body: { where: v } }); l.where = v; toast('Saved'); }
      catch (x) { fail(x); }
    });
    // drop a PDF on a data-sheet slot (panel) or on the modal's drop zone
    ['dragover', 'dragleave', 'drop'].forEach(function (ev) {
      document.addEventListener(ev, function (e) {
        var z = e.target.closest && e.target.closest('[data-spxdrop], #spx-m-drop'); if (!z) return;
        e.preventDefault(); e.stopPropagation();
        z.classList.toggle('over', ev === 'dragover');
        if (ev === 'drop') { var f = e.dataTransfer && e.dataTransfer.files[0]; if (z.id === 'spx-m-drop') readSheet(f); else upload(byId(st.open), z.getAttribute('data-spxdrop'), f); }
      }, true);
    });
  }

  window.SPECS_loadCatalog = function () { wire(); return load(); };
})();
