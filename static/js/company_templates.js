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
  const S = { items: [], meta: null, folder: null, open: {}, editing: null, confirm: null, reading: [] };

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
            <div class="tp-small">${t.type_label ? esc(t.type_label) + ' · ' : ''}Offered on: ${esc(t.offer_label)}</div></div>
          <div class="tp-row-side">${designPill(t)}
            <button type="button" class="btn-ghost btn-sm" data-preview="${t.id}">Preview</button>
            <button type="button" class="btn-ghost btn-sm" data-edit="${t.id}">Edit</button>
            <button type="button" class="btn-ghost btn-sm${S.confirm === t.id ? ' tp-danger' : ''}" data-delete="${t.id}">${S.confirm === t.id ? 'Delete? Tap again' : 'Delete'}</button></div>
        </div>`).join('') + '</div>';
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
  function preview(id) {
    const t = S.items.find(x => x.id === id); if (!t || !window.SSC_PDF) return;
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
  });
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
