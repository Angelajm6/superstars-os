/* Company → Templates (2026-10-26): the company's blank forms, letters and plans.

   Drop or pick files → one review card per file (Claude's name, folder, document type, which jobs it
   is offered on, the blanks it found, whether the design looks dated) → Approve files it. Below:
   one tile per folder, and the open folder's templates (preview, edit, delete).

   SSCTemplates.load()   called by the company page router for #templates (company_portal.html)
   API: /api/company/portal/templates (company_templates.py), admin / C-suite only.
*/
(function () {
  'use strict';
  const API = '/api/company/portal/templates';
  const $ = (id) => document.getElementById(id);
  const S = { items: [], meta: null, folder: null, open: {}, editing: null, confirm: null, reading: [], history: {} };

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  function toast(msg, isErr) {
    const t = $('cp-toast'); if (!t) return;
    t.textContent = msg; t.className = 'cp-toast show' + (isErr ? ' err' : '');
    clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 3200);
  }
  async function api(path, opts) {
    const res = await fetch(API + path, Object.assign({ credentials: 'same-origin', headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' } }, opts || {}));
    const j = await res.json().catch(() => ({}));
    if (res.status === 401) { window.location.href = '/login?next=' + encodeURIComponent('/company#templates'); throw new Error('unauthenticated'); }
    if (!res.ok) throw new Error((j && j.error) || ('Request failed (' + res.status + ')'));
    return j;
  }
  const folderName = (code) => ((S.meta && S.meta.folders.find(f => f.code === code)) || { name: code }).name;
  const waiting = () => S.items.filter(t => t.state === 'waiting');
  const filed = () => S.items.filter(t => t.state === 'filed');
  function kb(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round((n || 0) / 1024)) + ' KB'; }
  function kindWord(t) {
    const n = (t.file_name || '').toLowerCase();
    if (n.endsWith('.pdf')) return 'PDF';
    if (n.endsWith('.docx')) return 'Word';
    if (/\.(xlsx|xls|csv)$/.test(n)) return 'Excel';
    return 'Photo';
  }

  async function load() {
    const host = $('tp-folders'); if (!host) return;
    if (!S.meta) host.innerHTML = '<div class="cp-skel"></div><div class="cp-skel"></div>';
    try {
      const j = await api('');
      S.items = j.data; S.meta = j.meta;
      render();
    } catch (e) { toast(e.message, true); host.innerHTML = `<div class="cp-empty"><strong>Could not load templates</strong>${esc(e.message)}</div>`; }
  }

  function render() { renderWaiting(); renderFolders(); renderFolder(); }

  // ---------- the review cards ----------
  function typeOptions(folder, current) {
    const types = (S.meta.types || []).filter(t => t.category === folder);
    return '<option value="none">Not a checklist item</option>' +
      types.map(t => `<option value="${esc(t.key)}"${t.key === current ? ' selected' : ''}>${esc(t.label)}</option>`).join('');
  }
  function offerPicker(t) {
    const o = S.meta.offer_options, groups = [['job_types', 'Job type'], ['access_methods', 'Access'], ['billing_types', 'Billing'], ['regions', 'Region']];
    return '<div class="tp-offer">' + groups.map(([k, label]) => `<fieldset><legend>${label}</legend>` +
      o[k].map(x => `<label class="tp-check"><input type="checkbox" data-offer="${k}" value="${esc(x.code)}"${(t.offer[k] || []).includes(x.code) ? ' checked' : ''}>${esc(x.label)}</label>`).join('') +
      '</fieldset>').join('') + '<p class="tp-small">Nothing ticked = offered on every job.</p></div>';
  }
  function form(t, mode) {
    const fields = (t.fields || []);
    const blanks = fields.length ? `<div class="tp-small"><b>${fields.length} blank${fields.length === 1 ? '' : 's'} Claude found:</b> ${esc(fields.slice(0, 6).map(f => f.label).join(', '))}${fields.length > 6 ? ` +${fields.length - 6} more` : ''}</div>` : '';
    const read = t.read_status === 'ready'
      ? (t.summary ? `<div class="tp-small">${esc(t.summary)}</div>` : '')
      : `<div class="tp-small tp-warn-text">${esc(t.read_error || 'Claude has not read this one. Pick the folder yourself.')}</div>`;
    const open = S.open[t.id];
    return `<div class="tp-form" data-form="${t.id}">
      <input class="tp-title" id="tp-title-${t.id}" value="${esc(t.title)}" aria-label="Template name" maxlength="160">
      <div class="tp-facts">
        <label>Folder <select class="cp-select" id="tp-folder-${t.id}" data-folder-pick="${t.id}">${S.meta.folders.map(f => `<option value="${f.code}"${f.code === t.folder ? ' selected' : ''}>${esc(f.name)}</option>`).join('')}</select></label>
        <label>Fills <select class="cp-select" id="tp-type-${t.id}">${typeOptions(t.folder, t.type_key)}</select></label>
        <span>Offer on <b>${esc(t.offer_label)}</b> <button type="button" class="tp-link" data-offer-toggle="${t.id}">${open ? 'Done' : 'Change'}</button></span>
      </div>
      <div id="tp-offer-${t.id}"${open ? '' : ' hidden'}>${offerPicker(t)}</div>
      ${read}${blanks}
      <div class="tp-tags">${designPill(t)}<span class="cp-pill">${kindWord(t)} · ${kb(t.file_size)}</span></div>
      <div class="tp-actions">
        <button type="button" class="btn-primary btn-sm" data-save="${t.id}" data-mode="${mode}">${mode === 'approve' ? 'Approve' : 'Save'}</button>
        <button type="button" class="btn-ghost btn-sm" data-preview="${t.id}">Preview</button>
        ${mode === 'approve' ? `<button type="button" class="btn-ghost btn-sm" data-remove="${t.id}">Remove</button>` : `<button type="button" class="btn-ghost btn-sm" data-cancel-edit="${t.id}">Cancel</button>`}
      </div>
    </div>`;
  }
  function designPill(t) {
    if (t.redesigned) return '<span class="cp-pill active">House style</span>';
    if (t.design_verdict === 'dated') return `<span class="cp-pill warn" title="${esc((t.design_notes || []).join(' · '))}">Looks dated</span>`;
    if (t.design_verdict === 'ok') return '<span class="cp-pill ok">Design OK</span>';
    return '';
  }
  function renderWaiting() {
    const list = waiting(), host = $('tp-waiting');
    const reading = S.reading.map(n => `<div class="tp-review tp-reading"><div class="tp-thumb"></div><div><b>${esc(n)}</b><div class="tp-small">Claude is reading it…</div></div></div>`).join('');
    if (!list.length && !reading) { host.innerHTML = ''; return; }
    host.innerHTML = `<div class="tp-cap">Waiting for you · ${list.length}</div>` + reading +
      list.map(t => `<div class="tp-review" id="tp-card-${t.id}"><div class="tp-thumb" aria-hidden="true"><i></i><i></i><i></i><i></i></div>${form(t, 'approve')}</div>`).join('');
  }

  // ---------- folders ----------
  function renderFolders() {
    const items = filed();
    const counts = {}, dated = {};
    items.forEach(t => { counts[t.folder] = (counts[t.folder] || 0) + 1; if (t.design_verdict === 'dated') dated[t.folder] = (dated[t.folder] || 0) + 1; });
    const shown = S.meta.folders.filter(f => f.code !== 'OTHER' || counts.OTHER);
    if (!S.folder || !shown.some(f => f.code === S.folder)) S.folder = (shown.find(f => counts[f.code]) || shown[0]).code;
    $('tp-folders').innerHTML = shown.map(f => `<button type="button" class="tp-tile${f.code === S.folder ? ' on' : ''}" data-tile="${f.code}" id="tp-tile-${f.code}">
      <span class="tp-n">${counts[f.code] || 0}</span><span class="tp-fl">${esc(f.name)}</span>
      <span class="tp-small${dated[f.code] ? ' tp-warn-text' : ''}">${dated[f.code] ? dated[f.code] + ' look' + (dated[f.code] === 1 ? 's' : '') + ' dated' : (counts[f.code] ? 'Up to date' : 'Empty')}</span></button>`).join('');
  }
  function renderFolder() {
    const rows = filed().filter(t => t.folder === S.folder);
    const head = `<div class="cp-card-head"><h2>${esc(folderName(S.folder))}</h2><span class="hint">${rows.length} template${rows.length === 1 ? '' : 's'}</span></div>`;
    if (!rows.length) {
      $('tp-folder').innerHTML = head + `<div class="cp-empty"><strong>No templates in this folder yet</strong>Drop files above. Claude puts each one in the right folder.</div>`;
      return;
    }
    $('tp-folder').innerHTML = head + '<div class="tp-list">' + rows.map(t => S.editing === t.id
      ? `<div class="tp-row tp-row-edit" id="tp-row-${t.id}">${form(t, 'save')}</div>`
      : `<div class="tp-row" id="tp-row-${t.id}">
          <div class="tp-row-main"><button type="button" class="tp-row-title" data-preview="${t.id}">${esc(t.title)}</button>
            <div class="tp-small">${t.version > 1 ? `Version ${t.version} · ` : ''}${t.type_label ? esc(t.type_label) + ' · ' : ''}Offered on: ${esc(t.offer_label)}</div></div>
          <div class="tp-row-side">${designPill(t)}
            <button type="button" class="btn-ghost btn-sm" data-redesign="${t.id}">Improve design</button>
            <button type="button" class="btn-ghost btn-sm" data-preview="${t.id}">Preview</button>
            ${t.word_url ? `<a class="btn-ghost btn-sm" href="${esc(t.word_url)}" download>Word</a>` : ''}
            ${t.has_history ? `<button type="button" class="btn-ghost btn-sm" data-history="${t.id}">${S.history[t.id] ? 'Hide history' : 'History'}</button>` : ''}
            <button type="button" class="btn-ghost btn-sm" data-edit="${t.id}">Edit</button>
            <button type="button" class="btn-ghost btn-sm${S.confirm === t.id ? ' tp-danger' : ''}" data-delete="${t.id}">${S.confirm === t.id ? 'Delete? Tap again' : 'Delete'}</button></div>
          ${S.history[t.id] ? historyList(S.history[t.id]) : ''}
        </div>`).join('') + '</div>';
  }
  function historyList(rows) {
    return '<div class="tp-hist">' + rows.map((v, i) => `<div class="tp-hist-row"><span><b>Version ${v.version}</b>${i === 0 ? ' · current' : ''} · ${esc(v.file_name)}${v.redesigned ? ' · house style' : ''}</span>
      <button type="button" class="tp-link" data-hist-preview="${v.id}">Preview</button></div>`).join('') + '</div>';
  }

  // ---------- actions ----------
  function payloadFrom(id) {
    const offer = { job_types: [], access_methods: [], billing_types: [], regions: [] };
    const box = $('tp-offer-' + id);
    if (box) box.querySelectorAll('input[data-offer]:checked').forEach(i => offer[i.dataset.offer].push(i.value));
    return { title: $('tp-title-' + id).value.trim(), folder: $('tp-folder-' + id).value, type_key: $('tp-type-' + id).value, offer };
  }
  function replace(t) { const i = S.items.findIndex(x => x.id === t.id); if (i >= 0) S.items[i] = t; else S.items.push(t); }

  async function upload(files) {
    files = Array.from(files || []);
    if (!files.length) return;
    S.reading = S.reading.concat(files.map(f => f.name)); renderWaiting();
    let done = 0;
    for (const f of files) {
      const fd = new FormData(); fd.append('file', f);
      try {
        const res = await fetch(API, { method: 'POST', credentials: 'same-origin', body: fd });
        const j = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error((j && j.error) || ('Upload failed (' + res.status + ')'));
        if (j.meta && j.meta.duplicate_of) toast('Already in Templates: ' + j.data.title);
        else { replace(j.data); done++; }
      } catch (e) { toast(f.name + ': ' + e.message, true); }
      S.reading.splice(S.reading.indexOf(f.name), 1);
      renderWaiting();
    }
    if (done) toast(done === 1 ? 'Claude read 1 template. Check it and approve.' : `Claude read ${done} templates. Check them and approve.`);
  }

  async function save(id, mode) {
    const body = payloadFrom(id);
    if (!body.title) { toast('Give the template a name', true); return; }
    try {
      const j = await api('/' + id + (mode === 'approve' ? '/approve' : ''), { method: mode === 'approve' ? 'POST' : 'PATCH', body: JSON.stringify(body) });
      replace(j.data); delete S.open[id]; S.editing = null;
      if (mode === 'approve') { S.folder = j.data.folder; toast('Filed in ' + j.data.folder_name); } else toast('Saved');
      render();
    } catch (e) { toast(e.message, true); }
  }
  async function remove(id, msg) {
    try {
      await api('/' + id, { method: 'DELETE' });
      S.items = S.items.filter(t => t.id !== id); S.confirm = null; render(); toast(msg);
    } catch (e) { toast(e.message, true); }
  }
  async function toggleHistory(id) {
    if (S.history[id]) { delete S.history[id]; renderFolder(); return; }
    try { S.history[id] = (await api('/' + id + '/history')).data; renderFolder(); } catch (e) { toast(e.message, true); }
  }
  function preview(id, t) {
    t = t || S.items.find(x => x.id === id); if (!t || !window.SSC_PDF) return;
    window.SSC_PDF.panel({ title: t.title, sub: folderName(t.folder) + ' · ' + t.file_name, url: t.file_url,
      kind: window.SSC_PDF.kindOf(t.mime, t.file_name), links: [{ label: 'Open in new tab', href: t.file_url }, { label: 'Download', href: t.file_url, download: true }] });
  }

  document.addEventListener('click', (e) => {
    const b = e.target.closest('#view-templates button'); if (!b) return;
    const d = b.dataset, id = (k) => Number(d[k]);
    if (b.id === 'tp-upload') { $('tp-file').click(); return; }
    if (d.tile) { S.folder = d.tile; S.editing = null; S.confirm = null; renderFolders(); renderFolder(); return; }
    if (d.offerToggle) { const n = id('offerToggle'); S.open[n] = !S.open[n]; const box = $('tp-offer-' + n); if (box) box.hidden = !S.open[n]; b.textContent = S.open[n] ? 'Done' : 'Change'; return; }
    if (d.save) { save(id('save'), d.mode); return; }
    if (d.preview) { preview(id('preview')); return; }
    if (d.remove) { remove(id('remove'), 'Removed'); return; }
    if (d.edit) { S.editing = id('edit'); S.confirm = null; renderFolder(); return; }
    if (d.cancelEdit) { S.editing = null; delete S.open[id('cancelEdit')]; renderFolder(); return; }
    if (d.delete) { const n = id('delete'); if (S.confirm === n) remove(n, 'Template deleted'); else { S.confirm = n; renderFolder(); } return; }
    if (d.redesign) { RD.open(id('redesign')); return; }
    if (d.history) { toggleHistory(id('history')); return; }
    if (d.histPreview) { const n = id('histPreview'); const v = Object.values(S.history).flat().find(x => x.id === n); preview(n, v); return; }
  });

  // ---------- Improve design (template_design.py): current vs proposed, edits, approve ----------
  const RD = {
    tid: null, t: null, designs: [], house: null, sel: null, side: 'new', timer: null, el: null,
    async open(tid) {
      this.tid = tid; this.sel = null; this.side = 'new'; this.designs = []; this.drawnKey = null; this.t = S.items.find(x => x.id === tid);
      this.mount(); this.el.hidden = false; document.body.classList.add('tp-rd-lock');
      this.paint(); this.drawCurrent();
      await this.refresh();
    },
    close() {
      clearTimeout(this.timer); this.timer = null; this.tid = null;
      if (this.el) this.el.hidden = true; document.body.classList.remove('tp-rd-lock');
    },
    mount() {
      if (this.el) return;
      const el = document.createElement('div');   // at the page root (DESIGN_SYSTEM: dialogs escape view wrappers)
      el.className = 'tp-rd-scrim'; el.hidden = true; el.id = 'tp-rd';
      el.innerHTML = `<div class="tp-rd" role="dialog" aria-modal="true" aria-labelledby="tp-rd-title">
        <div class="tp-rd-hd"><div class="tp-rd-hd-t"><h2 id="tp-rd-title"></h2><p class="tp-small" id="tp-rd-sub">Same wording and fields, redrawn in your house style.</p></div>
          <div class="tp-rd-chips" id="tp-rd-chips"></div><button type="button" class="tp-rd-x" id="tp-rd-close" aria-label="Close">✕</button></div>
        <div class="tp-rd-seg" role="tablist" aria-label="Proposed or current"><button type="button" data-rd-side="new" id="tp-rd-sg-new">Proposed</button><button type="button" data-rd-side="old" id="tp-rd-sg-old">Current</button></div>
        <div class="tp-rd-body">
          <div class="tp-rd-cmp" id="tp-rd-cmp">
            <section data-rd-pane="old"><div class="tp-rd-lab"><span class="tp-cap">Current</span><span class="cp-pill" id="tp-rd-file"></span></div><div class="tp-rd-paper" id="tp-rd-old"></div></section>
            <section data-rd-pane="new"><div class="tp-rd-lab"><span class="tp-cap">Proposed</span><span class="cp-pill active" id="tp-rd-tag">House style</span></div><div class="tp-rd-paper" id="tp-rd-new"></div></section>
          </div>
          <aside class="tp-rd-side">
            <div class="cp-card" id="tp-rd-act"></div>
            <div class="cp-card" id="tp-rd-house"></div>
          </aside>
        </div></div>`;
      document.body.appendChild(el); this.el = el;
      el.addEventListener('click', (e) => {
        if (e.target === el || e.target.closest('#tp-rd-close')) { this.close(); return; }
        const b = e.target.closest('button'); if (!b) return;
        const d = b.dataset;
        if (d.rdSide) { this.side = d.rdSide; this.paintSide(); return; }
        if (d.rdChip) { this.sel = Number(d.rdChip); this.paint(); this.drawIfChanged(); return; }
        if (b.id === 'tp-rd-start' || b.id === 'tp-rd-retry') { this.start(); return; }
        if (b.id === 'tp-rd-edit') { this.edit(); return; }
        if (b.id === 'tp-rd-approve') { this.approve(b); return; }
        if (b.id === 'tp-rd-discard') { this.discard(); return; }
      });
      document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && this.tid && !this.el.hidden) this.close(); });
      window.addEventListener('resize', () => { if (this.tid) this.fit(); });
    },
    cur() { return this.designs.find(d => d.id === this.sel) || null; },
    drawIfChanged() {   // the preview is redrawn only when the shown draft or its state changes
      const d = this.cur(), key = d ? d.id + ':' + d.status : 'none';
      if (key !== this.drawnKey) { this.drawnKey = key; this.drawProposed(); }
    },
    async refresh() {
      const tid = this.tid; if (!tid) return;
      try {
        const j = await api('/' + tid + '/designs');
        if (this.tid !== tid) return;
        this.designs = j.data; this.house = j.meta.house_style; this.t = j.meta.template;
        if (!this.cur()) this.sel = this.designs.length ? this.designs[0].id : null;
        this.paint(); this.drawIfChanged();
        clearTimeout(this.timer);
        if (this.designs.some(d => d.status === 'drafting')) this.timer = setTimeout(() => this.refresh(), 3000);
      } catch (e) { toast(e.message, true); }
    },
    paint() {
      const t = this.t || {}, d = this.cur();
      $('tp-rd-title').textContent = 'Improve design: ' + (t.title || '');
      $('tp-rd-file').textContent = t.file_name || '';
      const drafts = this.designs.filter(x => x.status !== 'approved').slice().reverse();
      $('tp-rd-chips').innerHTML = drafts.map(x => `<button type="button" class="cp-chip${x.id === this.sel ? ' on' : ''}" data-rd-chip="${x.id}">Draft ${x.draft_no}${x.status === 'drafting' ? ' …' : ''}</button>`).join('');
      const house = this.house || {};
      const first = !house.set;
      let act;
      if (!d || d.status === 'approved') {
        act = `<h2>Redraw in your house style</h2>
          <p class="tp-small">Claude keeps every word and every blank of this template and lays it out in your house style: letterhead, colors, fonts and field boxes. You review it before anything changes.</p>
          ${t.design_notes && t.design_notes.length ? `<p class="tp-small"><b>What looks dated now:</b> ${esc(t.design_notes.join(' · '))}</p>` : ''}
          <div class="tp-actions"><button type="button" class="btn-primary btn-sm" id="tp-rd-start">Draw the new design</button></div>`;
      } else if (d.status === 'drafting') {
        act = `<h2>Claude is drawing draft ${d.draft_no}…</h2><p class="tp-small">${d.instruction ? 'Your edit: ' + esc(d.instruction) + '. ' : ''}A long template takes about a minute. You can close this window; the draft keeps going.</p>`;
      } else if (d.status === 'failed') {
        act = `<h2>Draft ${d.draft_no} didn't work</h2><p class="tp-small tp-warn-text">${esc(d.error || 'Claude couldn’t redraw this one.')}</p>
          <div class="tp-actions"><button type="button" class="btn-primary btn-sm" id="tp-rd-retry">Try again</button><button type="button" class="btn-ghost btn-sm" id="tp-rd-discard">Discard</button></div>`;
      } else {
        act = `<h2>What Claude changed</h2>
          ${d.instruction ? `<p class="tp-small"><b>Your edit:</b> ${esc(d.instruction)}</p>` : ''}
          <ul class="tp-rd-notes">${(d.changes || []).map(c => `<li>${esc(c)}</li>`).join('') || '<li>Same content, in the house style.</li>'}</ul>
          <label class="tp-cap" for="tp-rd-ask">Suggest an edit</label>
          <textarea id="tp-rd-ask" class="tp-rd-ask" maxlength="1000" placeholder="e.g. Put the emergency contacts first, in a red box"></textarea>
          <div class="tp-actions">
            <button type="button" class="btn-ghost btn-sm" id="tp-rd-edit">Send edit · new draft</button>
            <button type="button" class="btn-primary btn-sm" id="tp-rd-approve">${first ? 'Approve and set house style' : 'Approve as version ' + ((t.version || 1) + 1)}</button>
          </div>
          <button type="button" class="tp-link" id="tp-rd-discard">Discard this draft</button>`;
      }
      $('tp-rd-act').innerHTML = act;
      $('tp-rd-house').innerHTML = `<div class="cp-card-head"><h2>House style</h2><span class="cp-pill${first ? '' : ' ok'}">${first ? 'Not set yet' : 'Set · ' + esc(fmtDay(house.set.set_at))}</span></div>
        <p class="tp-small">${first ? 'The first design you approve sets it. Every redesign after that, and every document filled from a template, uses the same look.' : 'Every redesigned template and every document filled from one uses this look.'}</p>
        <div class="tp-rd-sw">${(house.colors || []).map(c => `<span><i style="background:${esc(c.hex)}"></i>${esc(c.name)}</span>`).join('')}</div>
        <div class="tp-small"><b>Headings</b> ${esc(house.headings || '')} · <b>Body</b> ${esc(house.body || '')}</div>
        <div class="tp-small"><b>Letterhead</b> ${esc(house.letterhead || '')}${house.has_logo ? '' : ' (no logo uploaded yet, the company name is printed instead)'}</div>
        ${(house.letterhead_lines || []).length ? `<div class="tp-small">${house.letterhead_lines.map(esc).join('<br>')}</div>` : ''}
        <a class="tp-link" href="/company#brand">Change the logo or address in Settings › Company brand</a>`;
      this.paintSide();
    },
    paintSide() {
      this.el.querySelectorAll('[data-rd-side]').forEach(b => b.classList.toggle('on', b.dataset.rdSide === this.side));
      this.el.querySelectorAll('[data-rd-pane]').forEach(p => p.classList.toggle('show', p.dataset.rdPane === this.side));
    },
    drawCurrent() {
      const t = this.t, host = $('tp-rd-old'); if (!t) return;
      const kind = window.SSC_PDF ? window.SSC_PDF.kindOf(t.mime, t.file_name) : 'other';
      if (kind === 'pdf') { host.innerHTML = ''; window.SSC_PDF.render(t.file_url, host); }
      else if (kind === 'image') host.innerHTML = `<img alt="" src="${esc(t.file_url)}">`;
      else host.innerHTML = `<div class="cp-empty"><strong>${esc(kindWord(t))} files can't be shown here</strong><a class="tp-link" href="${esc(t.file_url)}" download>Download the current file</a></div>`;
    },
    async drawProposed() {
      const host = $('tp-rd-new'), d = this.cur();
      if (!d || d.status !== 'ready') {
        host.innerHTML = `<div class="cp-empty"><strong>${d && d.status === 'drafting' ? 'Drawing…' : 'No draft yet'}</strong>${d && d.status === 'drafting' ? 'The new design shows here when Claude is done.' : 'Draw the new design to see it here.'}</div>`;
        return;
      }
      try {
        const p = (await api('/designs/' + d.id + '/preview')).data;
        if (this.cur() !== d) return;
        host.innerHTML = '<div class="tp-rd-shadow"></div>';
        const root = host.firstChild.attachShadow({ mode: 'open' });
        root.innerHTML = `<style>:host{display:block}.tl-fit{transform-origin:top left}${p.css}</style><div class="tl-fit">${p.html}</div>`;
        this.fit();
      } catch (e) { host.innerHTML = `<div class="cp-empty"><strong>Could not show the draft</strong>${esc(e.message)}</div>`; }
    },
    fit() {
      const sh = this.el && this.el.querySelector('.tp-rd-shadow'); if (!sh || !sh.shadowRoot) return;
      const fitEl = sh.shadowRoot.querySelector('.tl-fit'), sheet = sh.shadowRoot.querySelector('.tl-sheet'); if (!fitEl || !sheet) return;
      const w = sh.clientWidth || sh.parentElement.clientWidth, scale = Math.min(1, w / 816);
      fitEl.style.transform = `scale(${scale})`; sh.style.height = Math.ceil(sheet.offsetHeight * scale) + 'px';
    },
    async start(body) {
      try {
        const j = await api('/' + this.tid + '/designs', { method: 'POST', body: JSON.stringify(body || {}) });
        this.sel = j.data.id; this.side = 'new';
        await this.refresh();
      } catch (e) { toast(e.message, true); }
    },
    edit() {
      const v = ($('tp-rd-ask').value || '').trim();
      if (!v) { toast('Say what to change first', true); return; }
      this.start({ instruction: v, from_id: this.sel });
    },
    async approve(b) {
      const d = this.cur(); if (!d) return;
      b.disabled = true; b.textContent = 'Saving the PDF…';
      try {
        const j = await api('/designs/' + d.id + '/approve', { method: 'POST' });
        const nt = j.data.template;
        S.items = S.items.filter(x => x.id !== this.tid); S.items.push(nt); S.folder = nt.folder; delete S.history[this.tid];
        toast(j.data.house_style_set_now ? `Version ${nt.version} saved. House style set.` : `Version ${nt.version} saved in the house style.`);
        this.close(); render();
      } catch (e) { toast(e.message, true); b.disabled = false; b.textContent = 'Try approving again'; }
    },
    async discard() {
      const d = this.cur(); if (!d) return;
      try { await api('/designs/' + d.id, { method: 'DELETE' }); this.sel = null; await this.refresh(); toast('Draft discarded'); } catch (e) { toast(e.message, true); }
    },
  };
  function fmtDay(iso) { try { const [y, m, d] = String(iso).slice(0, 10).split('-').map(Number); return new Date(y, m - 1, d).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }); } catch (e) { return ''; } }
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (el.id === 'tp-file') { upload(el.files); el.value = ''; return; }
    if (el.dataset && el.dataset.folderPick) {   // a new folder → its own document types
      const n = el.dataset.folderPick, sel = $('tp-type-' + n);
      if (sel) sel.innerHTML = typeOptions(el.value, null);
    }
  });

  window.SSCTemplates = { load };
})();
