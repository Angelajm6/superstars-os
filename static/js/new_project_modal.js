/* Create / Edit project — ONE window, opened from anywhere (the Projects tab on /company, the
 * Assignments page at /admin/projects, and a job's Settings). Oct 7 C-suite notes, batch C:
 * a bigger window in sections, the job's number and calendar color, the access method of an
 * Exterior Restoration job, what the job's workers see in the worker app, and the drawing set.
 *
 *   SSCNewProject.open([prefill])   create; prefill: {address, name, borough, client_org_id}; from Projects → Ready to
 *                                   start also {from_proposal_id, proposal_label, est_type, contract_value}: the job takes the
 *                                   signed proposal's estimate and client; type and region stay editable
 *   SSCNewProject.edit(code)        edit an existing job (number, type and code never change)
 *   document event 'ssc:project-created'  detail = the onboard response data
 *   document event 'ssc:project-updated'  detail = {project_code}
 *   document event 'ssc:project-deleted'  detail = {project_code}; cancelable — not cancelled, it goes to /company#projects
 *
 * Create  -> POST /api/admin/projects/onboard (project_onboard.py, which calls project_setup)
 * Edit    -> PUT …/details, …/billing-type (when changed), …/crew, …/setup
 * Delete  -> DELETE /api/admin/projects/<code> (project_delete.py; refused while the job has records)
 * Drawings-> POST /api/projects/<code>/drawing-sets, one PDF at a time, after the job exists.
 * Per-field errors show together; nothing silently defaults. Dates go through SSCDatePicker. */
(function () {
  'use strict';
  var CSS = `.np-overlay{position:fixed;inset:0;background:rgba(20,22,28,.45);display:none;align-items:flex-start;justify-content:center;z-index:50;padding:24px 16px;overflow:auto;}
.np-overlay.open{display:flex;}
.np-modal{background:#fff;border-radius:16px;width:100%;max-width:1060px;box-shadow:0 24px 64px rgba(20,22,28,.25);display:flex;flex-direction:column;max-height:calc(100vh - 48px);}
.np-head{padding:16px 22px;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;}
.np-head .np-no{font-weight:600;font-size:20px;color:#B11E2E;font-variant-numeric:tabular-nums;}
.np-head h2{font-size:19px;font-weight:600;margin:0;} .np-head p{flex-basis:100%;font-size:12.5px;color:var(--muted);margin:2px 0 0;}
.np-main{display:grid;grid-template-columns:170px minmax(0,1fr) 240px;min-height:0;flex:1;overflow:auto;}
.np-rail{border-right:1px solid var(--line);padding:12px 8px;display:flex;flex-direction:column;gap:2px;position:sticky;top:0;align-self:start;}
.np-rail a{display:block;padding:7px 10px;border-radius:7px;color:var(--ink);opacity:.75;text-decoration:none;font-size:13px;}
.np-rail a:hover{background:#F4F5F8;opacity:1;}
.np-body{padding:4px 22px 18px;min-width:0;}
.np-sec{padding:14px 0;border-top:1px solid var(--line);display:grid;grid-template-columns:1fr 1fr;gap:12px;}
.np-modal [hidden]{display:none !important;}   /* the section display rules above must not un-hide a hidden part (Access method on non-Exterior jobs) */
.np-sec:first-child{border-top:0;} .np-sec .full{grid-column:1 / -1;}
.np-sec h3{grid-column:1 / -1;margin:0;font-size:14.5px;font-weight:600;} .np-sec h3 small{font-weight:500;color:var(--muted);font-size:12.5px;margin-left:6px;}
.np-field label,.np-lab{display:block;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);margin-bottom:5px;}
.np-field input,.np-field select{width:100%;font:inherit;font-size:13px;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink);outline:none;box-sizing:border-box;}
.np-money{position:relative;}.np-money span{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:13px;pointer-events:none;}.np-field .np-money input{padding-left:22px;}
.np-field input[readonly],.np-field select:disabled{background:#F4F5F8;color:var(--muted);}
.np-field input:focus,.np-field select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);}
.np-err{color:#A8382B;background:var(--status-behind-soft,#fceeec);border-radius:8px;padding:8px 10px;font-size:13px;display:none;margin:0 22px 10px;} .np-err.show{display:block;}
.np-ok{color:#0c7a5f;font-size:13px;margin-right:auto;}
.np-foot{padding:12px 22px 16px;border-top:1px solid var(--line);display:flex;justify-content:flex-end;align-items:center;gap:8px;flex-wrap:wrap;}
body .np-modal .np-foot .btn-ghost.np-del{margin-right:auto;color:var(--status-behind-deep,#A8382B);}
.np-site{display:flex;gap:8px;flex-wrap:wrap;align-items:center;} .np-site input{flex:1;min-width:110px;}
.np-hint{font-size:12px;color:var(--muted);margin-top:6px;}
.np-crew{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:6px 12px;max-height:220px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:10px;}
.np-crew label{display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;} .np-crew input{width:auto;} .np-crew .wid{font-weight:700;color:#B11E2E;}
.np-crew .none{grid-column:1 / -1;color:var(--muted);font-size:12.5px;}
.np-ferr{display:none;color:#A8382B;font-size:12px;margin-top:5px;} .np-ferr.show{display:block;}
.np-field.invalid input,.np-field.invalid select{border-color:#A8382B;box-shadow:0 0 0 3px #fceeec;}
.np-sw{display:flex;gap:7px;flex-wrap:wrap;}
.np-sw button{width:26px;height:26px;border-radius:7px;border:2px solid transparent;cursor:pointer;position:relative;padding:0;}
.np-sw button.on{border-color:#14161C;box-shadow:inset 0 0 0 2px #fff;}
.np-sw button.used::after{content:"";position:absolute;right:-4px;top:-4px;width:8px;height:8px;border-radius:50%;background:#7A7F8A;border:2px solid #fff;}
.np-checks{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:8px;}
.np-ck{display:flex;gap:9px;align-items:flex-start;border:1px solid var(--line);border-radius:9px;padding:9px 11px;cursor:pointer;font-size:13px;}
.np-ck input{margin-top:2px;width:16px;height:16px;flex:none;accent-color:var(--accent);}
.np-ck small{display:block;color:var(--muted);font-size:12px;margin-top:2px;}
.np-ck.on{border-color:var(--accent);background:var(--accent-soft);}
.np-ck.lock{background:#F4F5F8;cursor:default;}
.np-core{display:flex;flex-wrap:wrap;gap:6px;}
.np-core span{font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px;background:var(--accent-soft);color:var(--accent-ink,#2A47B8);}
.np-drop{display:block;border:2px dashed var(--line);border-radius:12px;padding:18px;text-align:center;font-size:13px;cursor:pointer;}
.np-drop:hover{border-color:var(--accent);}
.np-files{display:grid;gap:6px;}
.np-file{display:flex;justify-content:space-between;gap:10px;align-items:center;border:1px solid var(--line);border-radius:8px;padding:7px 11px;font-size:13px;}
.np-file span{overflow-wrap:anywhere;} .np-file b{font-size:12px;white-space:nowrap;}
.np-prev{border-left:1px solid var(--line);background:#F7F8FA;padding:14px;display:grid;gap:10px;align-content:start;position:sticky;top:0;align-self:start;}
.np-phone{background:#fff;border:1px solid var(--line);border-radius:22px;padding:14px 12px;display:grid;gap:7px;box-shadow:0 6px 18px rgba(0,0,0,.07);}
.np-phone .t{font-size:11.5px;color:var(--muted);text-align:center;} .np-phone .j{font-weight:700;text-align:center;font-size:13px;}
.np-tile{border-radius:11px;padding:10px 12px;font-weight:600;font-size:12.5px;background:var(--accent-soft);color:var(--accent-ink,#24418C);}
.np-tile.out{background:#F1F3F7;color:var(--ink);}
.np-foot .btn-ghost,.np-site .btn-ghost{font-size:12px;font-weight:700;padding:7px 14px;border-radius:8px;border:1px solid var(--line);background:#fff;color:var(--ink);cursor:pointer;}
/* 2026-10-23 — address autofill (NYC GeoSearch via /api/address/suggest) */
.np-addr{position:relative;}
.np-alist{list-style:none;margin:4px 0 0;padding:6px;position:absolute;left:0;right:0;top:100%;background:#fff;border:1px solid var(--line);border-radius:14px;box-shadow:0 12px 28px rgba(34,38,51,.16);z-index:5;display:flex;flex-direction:column;gap:2px;max-height:300px;overflow:auto;}
.np-alist[hidden]{display:none;}
.np-alist li{padding:9px 10px;border-radius:8px;cursor:pointer;font-size:14px;line-height:1.3;}
.np-alist li small{display:block;font-size:12px;color:var(--ink-2,#4A4F59);}
.np-alist li[aria-selected="true"],.np-alist li:hover{background:var(--accent-soft);}
.np-alist .foot{cursor:default;font-size:11px;color:var(--muted);padding:6px 10px 2px;}
.np-alist .foot:hover{background:none;}
.np-filled{font-size:12px;color:var(--status-complete-deep,#0c7a5f);background:var(--status-complete-soft,#e7f6f0);border-radius:8px;padding:8px 10px;margin-top:6px;}
.np-filled[hidden]{display:none;}
@media (max-width:600px){ .np-alist li[data-k]{min-height:44px;} .np-field input,.np-field select{font-size:16px;} }
@media (max-width:980px){ .np-main{grid-template-columns:minmax(0,1fr);} .np-rail,.np-prev{display:none;} }
@media (max-width:600px){ .np-sec{grid-template-columns:1fr;} .np-overlay{padding:0;} .np-modal{border-radius:0;max-height:100vh;} }`;
  var st = document.createElement('style'); st.id = 'np-style'; st.textContent = CSS; document.head.appendChild(st);

  var HTML = `<div class="np-overlay" id="np-overlay" role="dialog" aria-modal="true" aria-labelledby="np-title">
  <form class="np-modal" id="np-form" novalidate>
    <div class="np-head"><span class="np-no" id="np-no"></span><h2 id="np-title">New project</h2><p id="np-sub"></p></div>
    <div class="np-main">
      <nav class="np-rail" aria-label="Sections"><a href="#np-s-job">Job</a><a href="#np-s-access" id="np-rail-access">Access method</a><a href="#np-s-tabs">Tabs for this job</a><a href="#np-s-client">Client &amp; billing</a><a href="#np-s-dates">Dates &amp; people</a><a href="#np-s-crew">Crew &amp; site</a><a href="#np-s-work">Workforce access</a><a href="#np-s-dwg">Drawings</a></nav>
      <div class="np-body">
        <section class="np-sec" id="np-s-job"><h3>Job</h3>
          <div class="np-field"><label for="np-type">Job type</label><select id="np-type" aria-describedby="np-ferr-type"></select><div class="np-ferr" id="np-ferr-type" role="alert"></div></div>
          <div class="np-field" id="np-other-wrap" hidden><label for="np-other">What kind of job?</label><input id="np-other" maxlength="80" placeholder="e.g. Basement excavation" autocomplete="off" aria-describedby="np-ferr-other"><div class="np-ferr" id="np-ferr-other" role="alert"></div></div>
          <div class="np-field"><label for="np-borough">Borough / region</label><select id="np-borough" aria-describedby="np-ferr-borough"></select><div class="np-ferr" id="np-ferr-borough" role="alert"></div></div>
          <div class="np-field full np-addr"><label for="np-address">Building address</label><input id="np-address" placeholder="Start typing, e.g. 890 East 135th Street" autocomplete="off" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="np-alist" aria-describedby="np-ferr-address"><ul class="np-alist" id="np-alist" role="listbox" aria-label="Address suggestions" hidden></ul><div class="np-ferr" id="np-ferr-address" role="alert"></div><div class="np-filled" id="np-filled" role="status" hidden></div></div>
          <div class="np-field"><label for="np-cityzip">City / ZIP</label><input id="np-cityzip" placeholder="Bronx, NY 10454" maxlength="120" autocomplete="off"></div>
          <div class="np-field"><label for="np-name">Project name</label><input id="np-name" placeholder="defaults to the address" autocomplete="off"></div>
          <div class="np-field"><label for="np-code">Job code</label><input id="np-code" readonly></div>
          <div class="np-field full"><span class="np-lab">Calendar color</span><div class="np-sw" id="np-sw" role="radiogroup" aria-label="Calendar color"></div><div class="np-hint" id="np-sw-hint"></div></div>
          <div class="np-field full"><label for="np-notes">Notes</label><input id="np-notes" placeholder="optional" autocomplete="off"></div>
        </section>
        <section class="np-sec" id="np-s-access"><h3>Access method <small>Exterior Restoration jobs</small></h3>
          <div class="np-checks full" id="np-access"></div>
          <div class="np-hint full">Pick every method the job uses. It shows as a chip on the job page.</div>
        </section>
        <section class="np-sec" id="np-s-tabs"><h3>Tabs for this job <small>suggested by the job type: untick what this job won't use</small></h3>
          <div class="np-field full"><span class="np-lab">Always on</span><div class="np-core" id="np-core"></div></div>
          <div class="np-checks full" id="np-tabs"></div>
          <div class="np-hint full" id="np-tabs-hint">Removing a tab never deletes anything. Add or remove tabs later from the job's sidebar (+ Add a tab).</div>
        </section>
        <section class="np-sec" id="np-s-client"><h3>Client &amp; billing</h3>
          <div class="np-field"><label for="np-client">Client</label><select id="np-client" aria-describedby="np-ferr-client"></select><div class="np-ferr" id="np-ferr-client" role="alert"></div></div>
          <div class="np-field" id="np-neworg-wrap" hidden><label for="np-neworg">New client name</label><input id="np-neworg" placeholder="Organization name" autocomplete="off" aria-describedby="np-ferr-neworg"><div class="np-ferr" id="np-ferr-neworg" role="alert"></div></div>
          <div class="np-field"><label for="np-billing">How is it billed?</label><select id="np-billing" aria-describedby="np-ferr-billing"></select><div class="np-hint">AIA / Schedule of values gets the SOV and pay applications.</div><div class="np-ferr" id="np-ferr-billing" role="alert"></div></div>
          <div class="np-field"><label for="np-contract">Contract value (USD)</label><div class="np-money"><span aria-hidden="true">$</span><input id="np-contract" inputmode="decimal" placeholder="optional" autocomplete="off"></div><div class="np-hint" id="np-contract-note"></div></div>
        </section>
        <section class="np-sec" id="np-s-dates"><h3>Dates &amp; people</h3>
          <div class="np-field"><label for="np-start">Start date</label><input id="np-start" type="date" class="ssc-date"></div>
          <div class="np-field"><label for="np-end">Target completion</label><input id="np-end" type="date" class="ssc-date" title="Optional — the job shows on the calendar from start to this date"></div>
          <div class="np-field" id="np-pm-wrap"><label for="np-pm">Project manager</label><select id="np-pm"></select></div>
          <div class="np-hint full" id="np-pm-note" hidden>Change the project managers from the PM column on the Projects list.</div>
        </section>
        <section class="np-sec" id="np-s-crew"><h3>Crew &amp; site <small>who can check in, and from where</small></h3>
          <div class="np-field full"><span class="np-lab">Crew</span><div class="np-crew" id="np-crew"></div></div>
          <div class="np-field full"><span class="np-lab">Site location · worker app check-in</span>
            <div class="np-site"><input id="np-lat" inputmode="decimal" placeholder="Latitude" autocomplete="off" aria-label="Latitude"><input id="np-lng" inputmode="decimal" placeholder="Longitude" autocomplete="off" aria-label="Longitude"><input id="np-radius" inputmode="numeric" placeholder="200 m" autocomplete="off" aria-label="Check-in radius in meters"><button type="button" class="btn-ghost" id="np-geo">Use my location</button></div>
            <div class="np-hint">Workers can only check in within this radius. Leave blank and the job accepts check-ins from anywhere until you set it.</div></div>
        </section>
        <section class="np-sec" id="np-s-work"><h3>Workforce access <small>what this job's workers see in the worker app</small></h3>
          <div class="np-checks full" id="np-work"></div>
          <div class="np-hint full">Change it any time in Edit project. Nothing here shows pay or other workers' details. Drawings, toolbox talks and job documents reach the worker app in the next update.</div>
        </section>
        <section class="np-sec" id="np-s-dwg"><h3>Drawings <small>optional, add more later</small></h3>
          <label class="np-drop full" for="np-files"><b>Drop the drawing set here</b> or click to choose PDFs<br><span class="np-hint">Saved to the job's drawings, then set up as elevations for markup and 3D</span></label>
          <input type="file" id="np-files" accept="application/pdf,.pdf" multiple hidden>
          <div class="np-files full" id="np-filelist"></div>
          <div class="np-hint full" id="np-dwg-note"></div>
        </section>
      </div>
      <aside class="np-prev" aria-label="Worker app preview"><span class="np-lab">Worker app preview</span>
        <div class="np-phone"><div class="t">Signed in 7:02 AM</div><div class="j" id="np-ph-job"></div><div id="np-ph-tiles" style="display:grid;gap:7px"></div></div>
        <div class="np-hint">What a worker on this job sees after signing in.</div></aside>
    </div>
    <div class="np-err" id="np-err" role="alert"></div>
    <div class="np-foot"><button type="button" class="btn-ghost np-del" id="np-del" hidden>Delete project</button><span class="np-ok" id="np-ok"></span><button type="button" class="btn-ghost" id="np-cancel">Cancel</button><button type="submit" class="btn-primary" id="np-go">Create project</button></div>
  </form>
</div>`;

  var NP = { meta: null, setup: null, mounted: false, mode: 'new', code: null, color: 0, files: [], cur: null };
  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function npDateGet(el) { var p = window.SSCDatePicker; if (p && typeof p.getISO === 'function') { var v = p.getISO(el); if (v !== undefined && v !== null) return v; } return el.value || ''; }
  function npDateSet(el, iso) { var p = window.SSCDatePicker; if (p && typeof p.setISO === 'function') { try { p.setISO(el, iso || ''); return; } catch (e) {} } el.value = iso || ''; }
  async function getJSON(url) { var r = await fetch(url, { credentials: 'same-origin' }); var j = await r.json().catch(function () { return {}; }); if (!r.ok) throw new Error(j.error || ('Request failed (' + r.status + ')')); return j.data; }
  async function send(method, url, body) {
    var r = await fetch(url, { method: method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    var j = await r.json().catch(function () { return {}; }); if (!r.ok) throw new Error(j.error || ('Request failed (' + r.status + ')')); return j.data;
  }

  // ---- crew checkboxes + "use my location"
  function renderCrewBoxes(host, workers) {
    if (!workers || !workers.length) { host.innerHTML = '<div class="none">No active workers yet. Onboard them from Workforce first.</div>'; return; }
    host.innerHTML = workers.map(function (w) { return '<label><input type="checkbox" value="' + esc(w.employee_id) + '"' + (w.assigned ? ' checked' : '') + '><span><span class="wid">' + esc(w.worker_id || w.employee_id) + '</span> — ' + esc(w.name || '') + (w.trade ? ' <span style="color:var(--muted)">· ' + esc(w.trade) + '</span>' : '') + '</span></label>'; }).join('');
  }
  function crewChecked(host) { return Array.prototype.map.call(host.querySelectorAll('input[type=checkbox]:checked'), function (i) { return i.value; }); }
  function showFormError(msg) { var err = $('np-err'); err.textContent = msg; err.className = 'np-err show'; err.scrollIntoView({ block: 'nearest' }); }
  function useMyLocation(btn) {
    if (!navigator.geolocation) { showFormError('This browser cannot read your location.'); return; }
    btn.disabled = true; btn.textContent = 'Locating…';
    navigator.geolocation.getCurrentPosition(function (pos) { $('np-lat').value = pos.coords.latitude.toFixed(6); $('np-lng').value = pos.coords.longitude.toFixed(6); btn.disabled = false; btn.textContent = 'Use my location'; },
      function () { btn.disabled = false; btn.textContent = 'Use my location'; showFormError('Location not available — allow location access and try again, or type the coordinates.'); }, { enableHighAccuracy: true, timeout: 12000 });
  }

  // ---- color, access method, workforce access, drawings
  function drawColors() {
    var s = NP.setup || { palette: [], used: {} }, used = s.used || {};
    $('np-sw').innerHTML = s.palette.map(function (c, i) {
      var taken = used[String(i)] > 0;
      return '<button type="button" role="radio" aria-checked="' + (i === NP.color) + '" class="' + (i === NP.color ? 'on' : '') + (taken ? ' used' : '') + '" style="background:' + c + '" data-c="' + i + '" aria-label="Color ' + (i + 1) + (taken ? ', used by another active job' : '') + '"></button>';
    }).join('');
    var taken = used[String(NP.color)] > 0;
    $('np-sw-hint').textContent = taken ? 'Another active job already uses this color.' : (NP.mode === 'new' ? 'Picked for you: a color no active job uses. A dot marks colors already taken.' : 'A dot marks colors other active jobs use.');
  }
  // "Other" job type: described in words (e.g. Basement excavation); the description stands in for the type name
  function otherApplies() { var t = NP.mode === 'edit' ? (NP.code || '').split('-')[0] : $('np-type').value; return ((NP.setup && NP.setup.other_types) || ['OTH']).indexOf((t || '').toUpperCase()) !== -1; }
  function drawOther() { $('np-other-wrap').hidden = !otherApplies(); }
  function accessApplies() { var t = NP.mode === 'edit' ? (NP.code || '').split('-')[0] : $('np-type').value; return (NP.setup.access_types || ['FR']).indexOf((t || '').toUpperCase()) !== -1; }
  function drawAccess(selected) {
    var on = accessApplies(); $('np-s-access').hidden = !on; $('np-rail-access').hidden = !on;
    if (selected !== undefined) {
      var list = selected || [];
      $('np-access').innerHTML = (NP.setup.access_methods || []).map(function (m) { return '<label class="np-ck"><input type="checkbox" value="' + esc(m.key) + '"' + (list.indexOf(m.key) !== -1 ? ' checked' : '') + '>' + esc(m.label) + '</label>'; }).join('')
        + '<label class="np-ck"><input type="checkbox" value="" data-none' + (selected && !list.length ? ' checked' : '') + '>None</label>';
    }
    markChecks($('np-access'));
  }
  function accessChosen() {
    if (!accessApplies()) return [];
    return Array.prototype.map.call($('np-access').querySelectorAll('input:checked'), function (i) { return i.value; }).filter(Boolean);
  }
  function drawWork(selected) {
    var list = selected || NP.setup.worker_default || ['photos'];
    $('np-work').innerHTML = '<label class="np-ck lock"><input type="checkbox" checked disabled>Sign in / sign out<small>Always on</small></label>'
      + (NP.setup.worker_features || []).map(function (f) { return '<label class="np-ck"><input type="checkbox" value="' + esc(f.key) + '"' + (list.indexOf(f.key) !== -1 ? ' checked' : '') + '>' + esc(f.label) + '<small>' + esc(f.hint) + '</small></label>'; }).join('');
    markChecks($('np-work')); drawPhone();
  }
  // ---- Tabs for this job (lean jobs, A + B): pre-ticked from the job type's starting set
  function typeOf() { return (NP.mode === 'edit' ? (NP.code || '').split('-')[0] : $('np-type').value || '').toUpperCase(); }
  function drawTabs(selected) {
    var m = NP.meta || {}, on = selected || [];
    $('np-core').innerHTML = (m.core_tabs || []).map(function (t) { return '<span>' + esc(t) + '</span>'; }).join('');
    $('np-tabs').innerHTML = (m.modules || []).map(function (x) { return '<label class="np-ck"><input type="checkbox" value="' + esc(x.key) + '"' + (on.indexOf(x.key) !== -1 ? ' checked' : '') + '><span>' + esc(x.label) + '<small>' + esc(x.hint) + '</small></span></label>'; }).join('');
    markChecks($('np-tabs'));
  }
  function tabsForType() { var t = typeOf(); return t ? (((NP.meta || {}).type_tabs || {})[t] || []) : []; }
  function tabsChosen() { return Array.prototype.map.call($('np-tabs').querySelectorAll('input:checked'), function (i) { return i.value; }); }
  function workChosen() { return Array.prototype.map.call($('np-work').querySelectorAll('input:checked:not(:disabled)'), function (i) { return i.value; }); }
  function markChecks(host) { host.querySelectorAll('.np-ck').forEach(function (l) { var i = l.querySelector('input'); l.classList.toggle('on', !!(i && i.checked && !i.disabled)); }); }
  function drawPhone() {
    var labels = {}; (NP.setup.worker_features || []).forEach(function (f) { labels[f.key] = f.label; });
    var tiles = workChosen().map(function (k) { return '<div class="np-tile">' + esc(k === 'photos' ? 'Take a photo' : labels[k]) + '</div>'; });
    tiles.push('<div class="np-tile out">Sign out</div>');
    $('np-ph-tiles').innerHTML = tiles.join('');
    var no = NP.mode === 'edit' ? (NP.cur && NP.cur.job_number) : (NP.setup && NP.setup.next_job_number);
    $('np-ph-job').textContent = (no ? '#' + no + ' ' : '') + ($('np-name').value.trim() || $('np-address').value.trim() || 'New job');
  }
  function drawFiles() {
    $('np-filelist').innerHTML = NP.files.map(function (f, i) {
      var tag = f.state === 'done' ? 'Uploaded' : f.state === 'fail' ? 'Not uploaded' : f.state === 'busy' ? 'Uploading…' : 'Ready to upload';
      return '<div class="np-file"><span>' + esc(f.file.name) + (f.error ? ' <span style="color:#A8382B">· ' + esc(f.error) + '</span>' : '') + '</span><span style="display:flex;gap:8px;align-items:center"><b>' + tag + '</b>' + (f.state === 'done' || f.state === 'busy' ? '' : '<button type="button" class="btn-ghost" data-np-unfile="' + i + '" aria-label="Remove ' + esc(f.file.name) + '">Remove</button>') + '</span></div>';
    }).join('');
  }
  async function uploadDrawings(code) {
    var failed = 0;
    for (var i = 0; i < NP.files.length; i++) {
      var f = NP.files[i]; if (f.state === 'done') continue;
      f.state = 'busy'; f.error = ''; drawFiles();
      try {
        var fd = new FormData(); fd.append('file', f.file);
        var r = await fetch('/api/projects/' + encodeURIComponent(code) + '/drawing-sets', { method: 'POST', credentials: 'same-origin', body: fd });
        var j = await r.json().catch(function () { return {}; });
        if (!r.ok) throw new Error(j.error || ('upload failed (' + r.status + ')'));
        f.state = 'done';
      } catch (e) { f.state = 'fail'; f.error = e.message; failed++; }
      drawFiles();
    }
    return failed;
  }

  // ---- per-field errors (all at once, next to the field)
  var NP_FIELDS = ['client', 'neworg', 'type', 'other', 'borough', 'address', 'billing'];
  function clearFieldErrors() { NP_FIELDS.forEach(function (k) { var e = $('np-ferr-' + k); if (e) { e.textContent = ''; e.classList.remove('show'); var f = e.closest('.np-field'); if (f) f.classList.remove('invalid'); } }); }
  function fieldError(k, msg) { var e = $('np-ferr-' + k); e.textContent = msg; e.classList.add('show'); e.closest('.np-field').classList.add('invalid'); }
  function validate(payload) {
    var bad = [];
    if (!payload.client_org_id && !payload.new_org_name && !NP.fromProposal) { if ($('np-client').value === '__new') bad.push(['neworg', 'Type the new client name.']); else bad.push(['client', 'Choose a client or add a new one.']); }
    if (NP.mode === 'new' && !payload.est_type) bad.push(['type', 'Choose the job type.']);
    if (otherApplies() && !payload.job_type_other) bad.push(['other', 'Describe the job, for example Basement excavation.']);
    if (NP.mode === 'new' && !payload.borough) bad.push(['borough', 'Choose the borough or region.']);
    if (payload.address.length < 4) bad.push(['address', 'Enter the building address.']);
    if (!payload.billing_type) bad.push(['billing', 'Choose how the job is billed.']);   // #356
    return bad;
  }

  function mount() {
    if (NP.mounted) return; NP.mounted = true;
    var host = document.createElement('div'); host.innerHTML = HTML; document.body.appendChild(host.firstElementChild);
    $('np-cancel').addEventListener('click', close);
    $('np-del').addEventListener('click', remove);
    $('np-overlay').addEventListener('click', function (e) { if (e.target === $('np-overlay')) close(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && $('np-overlay').classList.contains('open')) close(); });
    $('np-geo').addEventListener('click', function () { useMyLocation($('np-geo')); });
    $('np-client').addEventListener('change', function () { var isNew = $('np-client').value === '__new'; $('np-neworg-wrap').hidden = !isNew; if (isNew) setTimeout(function () { $('np-neworg').focus(); }, 30); });
    ['np-client', 'np-neworg', 'np-type', 'np-borough', 'np-address', 'np-billing'].forEach(function (id) { var el = $(id); var ev = el.tagName === 'SELECT' ? 'change' : 'input'; el.addEventListener(ev, function () { var f = el.closest('.np-field'); if (f) { f.classList.remove('invalid'); var m = f.querySelector('.np-ferr'); if (m) { m.classList.remove('show'); m.textContent = ''; } } }); });
    $('np-type').addEventListener('change', function () { drawAccess(); drawOther(); drawTabs(tabsForType()); codePreview(); if (otherApplies()) $('np-other').focus(); });
    $('np-tabs').addEventListener('change', function () { markChecks($('np-tabs')); });
    $('np-other').addEventListener('input', function () { var e = $('np-ferr-other'); e.textContent = ''; e.classList.remove('show'); $('np-other-wrap').classList.remove('invalid'); });
    $('np-borough').addEventListener('change', codePreview);
    $('np-sw').addEventListener('click', function (e) { var b = e.target.closest('[data-c]'); if (!b) return; NP.color = +b.dataset.c; drawColors(); });
    $('np-access').addEventListener('change', function (e) {
      var none = $('np-access').querySelector('[data-none]');
      if (e.target === none && none.checked) $('np-access').querySelectorAll('input').forEach(function (x) { if (x !== none) x.checked = false; });
      else if (e.target.checked) none.checked = false;
      markChecks($('np-access'));
    });
    $('np-work').addEventListener('change', function () { markChecks($('np-work')); drawPhone(); });
    $('np-name').addEventListener('input', drawPhone); $('np-address').addEventListener('input', drawPhone); wireAddress();
    $('np-files').addEventListener('change', function () {
      Array.prototype.forEach.call(this.files, function (f) { NP.files.push({ file: f, state: 'ready' }); });
      this.value = ''; drawFiles();
    });
    var drop = document.querySelector('.np-drop');
    drop.addEventListener('dragover', function (e) { e.preventDefault(); });
    drop.addEventListener('drop', function (e) { e.preventDefault(); Array.prototype.forEach.call(e.dataTransfer.files || [], function (f) { if (/\.pdf$/i.test(f.name)) NP.files.push({ file: f, state: 'ready' }); }); drawFiles(); });
    $('np-filelist').addEventListener('click', function (e) { var b = e.target.closest('[data-np-unfile]'); if (!b) return; NP.files.splice(+b.dataset.npUnfile, 1); drawFiles(); });
    $('np-form').addEventListener('submit', submit);
  }
  function close() { $('np-overlay').classList.remove('open'); }
  function codePreview() {
    if (NP.mode !== 'new') return;
    var t = $('np-type').value, b = $('np-borough').value;
    $('np-code').value = t && b ? t + '-' + b + '-… (given when you create it)' : 'Given when you create it';
  }

  function payloadFromForm() {
    var isNew = $('np-client').value === '__new';
    return { client_org_id: isNew ? null : ($('np-client').value || null), new_org_name: isNew ? $('np-neworg').value.trim() : '',
      est_type: $('np-type').value, borough: $('np-borough').value, address: $('np-address').value.trim(), city_zip: $('np-cityzip').value.trim() || null, name: $('np-name').value.trim(),
      contract_value: $('np-contract').value.trim() || null, start_date: npDateGet($('np-start')) || null, target_completion: npDateGet($('np-end')) || null,
      pm_user_id: $('np-pm').value || null, notes: $('np-notes').value.trim(), billing_type: $('np-billing').value || null,
      site_lat: $('np-lat').value.trim() || null, site_lng: $('np-lng').value.trim() || null, geofence_m: $('np-radius').value.trim() || null, crew: crewChecked($('np-crew')),
      color_index: NP.color, access_methods: accessApplies() ? accessChosen() : null, worker_access: workChosen(),
      job_type_other: otherApplies() ? $('np-other').value.trim().replace(/\s+/g, ' ') : null, modules: tabsChosen(),
      from_proposal_id: NP.mode === 'new' ? NP.fromProposal : null };
  }

  async function submit(e) {
    e.preventDefault(); $('np-err').className = 'np-err'; $('np-ok').textContent = '';
    var payload = payloadFromForm();
    clearFieldErrors();
    var bad = validate(payload);
    if (bad.length) { bad.forEach(function (b) { fieldError(b[0], b[1]); }); var first = $('np-ferr-' + bad[0][0]).closest('.np-field'); var fe = first.querySelector('input,select'); if (fe) { fe.scrollIntoView({ block: 'center' }); fe.focus(); } return; }
    if ((payload.site_lat && !payload.site_lng) || (!payload.site_lat && payload.site_lng)) { showFormError('Enter both latitude and longitude, or leave both blank.'); return; }
    $('np-go').disabled = true;
    try {
      if (NP.mode === 'new') await createJob(payload); else await saveJob(payload);
    } catch (ex) { showFormError(ex.message); }
    finally { $('np-go').disabled = false; }
  }

  async function createJob(payload) {
    var d = await send('POST', '/api/admin/projects/onboard', payload);
    NP.meta = null; NP.setup = null;
    var failed = NP.files.length ? await uploadDrawings(d.project_code) : 0;
    document.dispatchEvent(new CustomEvent('ssc:project-created', { detail: d }));
    if (failed) {   // the job exists: stay open on it, so Save retries the drawings
      await edit(d.project_code, true);
      showFormError('Project #' + (d.job_number || '') + ' ' + d.project_code + ' was created, but ' + failed + (failed === 1 ? ' drawing' : ' drawings') + ' did not upload. Fix or remove them and press Save changes.');
      return;
    }
    close();
  }

  async function saveJob(p) {
    var code = NP.code, cur = NP.cur || {}, steps = [];
    var det = { name: p.name || p.address, address: p.address, city_zip: p.city_zip || '', start_date: p.start_date, target_completion: p.target_completion };
    if (p.client_org_id) det.client_org_id = p.client_org_id; else if (p.new_org_name) det.new_org_name = p.new_org_name;
    if (cur.contract_editable && p.contract_value) det.contract_value = p.contract_value;
    steps.push(['details', function () { return send('PUT', '/api/admin/projects/' + encodeURIComponent(code) + '/details', det); }]);
    if (p.billing_type && p.billing_type !== cur.billing_type) steps.push(['billing', function () { return send('PUT', '/api/admin/projects/' + encodeURIComponent(code) + '/billing-type', { billing_type: p.billing_type }); }]);
    steps.push(['crew and site', function () { return send('PUT', '/api/admin/projects/' + encodeURIComponent(code) + '/crew', { crew: p.crew, site_lat: p.site_lat, site_lng: p.site_lng, geofence_m: p.geofence_m }); }]);
    var setup = { color_index: p.color_index, worker_access: p.worker_access };
    if (p.access_methods !== null) setup.access_methods = p.access_methods;
    if (p.job_type_other) setup.job_type_other = p.job_type_other;
    steps.push(['color and access', function () { return send('PUT', '/api/admin/projects/' + encodeURIComponent(code) + '/setup', setup); }]);
    steps.push(['tabs', function () { return send('PUT', '/api/admin/projects/' + encodeURIComponent(code) + '/modules', { modules: p.modules }); }]);
    for (var i = 0; i < steps.length; i++) {
      try { await steps[i][1](); } catch (ex) { throw new Error('Could not save the ' + steps[i][0] + ': ' + ex.message + (i ? ' (the parts before it were saved)' : '')); }
    }
    var failed = NP.files.length ? await uploadDrawings(code) : 0;
    if (failed) throw new Error(failed + (failed === 1 ? ' drawing' : ' drawings') + ' did not upload. Everything else was saved. Fix or remove them and press Save changes again.');
    close();
    document.dispatchEvent(new CustomEvent('ssc:project-updated', { detail: { project_code: code } }));
  }

  // delete for good — the server refuses a job that has anything filed under it (close it instead)
  async function remove() {
    var code = NP.code; if (!code) return;
    if (!confirm('Permanently delete ' + code + '? Its crew and PM assignments go with it. This can\'t be undone.')) return;
    $('np-del').disabled = true; $('np-err').className = 'np-err';
    try {
      await send('DELETE', '/api/admin/projects/' + encodeURIComponent(code));
      close();
      var ev = new CustomEvent('ssc:project-deleted', { detail: { project_code: code }, cancelable: true });
      if (document.dispatchEvent(ev)) location.href = '/company#projects';   // no page took it: leave the deleted job's page
    } catch (ex) { showFormError(ex.message); }
    finally { $('np-del').disabled = false; }
  }

  async function loadMeta(code) {
    var res = await Promise.all([NP.meta ? Promise.resolve(NP.meta) : getJSON('/api/admin/projects/onboard-meta'),
      getJSON('/api/admin/projects/setup-meta' + (code ? '?project=' + encodeURIComponent(code) : ''))]);
    NP.meta = res[0]; NP.setup = res[1];
    var m = NP.meta;
    $('np-client').innerHTML = '<option value="">— choose a client —</option>' + m.clients.map(function (c) { return '<option value="' + c.id + '">' + esc(c.name) + '</option>'; }).join('') + '<option value="__new">+ New client…</option>';
    $('np-type').innerHTML = '<option value="">— choose job type —</option>' + m.types.map(function (t) { return '<option value="' + esc(t.code) + '">' + esc(t.code) + ' · ' + esc(t.label) + '</option>'; }).join('');
    $('np-borough').innerHTML = '<option value="">— choose borough / region —</option>' + (m.regions || m.boroughs.map(function (b) { return { code: b, label: b }; })).map(function (r) { return '<option value="' + esc(r.code) + '">' + esc(r.label) + ' (' + esc(r.code) + ')</option>'; }).join('');   // #331
    $('np-billing').innerHTML = '<option value="">— choose —</option>' + (m.billing_types || []).map(function (b) { return '<option value="' + esc(b.code) + '">' + esc(b.label) + '</option>'; }).join('');   // #356
    $('np-pm').innerHTML = '<option value="">— assign later —</option>' + m.pms.map(function (p) { return '<option value="' + p.user_id + '">' + esc(p.name) + '</option>'; }).join('');
    var p = window.SSCDatePicker; if (p && typeof p.wire === 'function') { ['np-start', 'np-end'].forEach(function (id) { if (!$(id).dataset.sscDp) { try { p.wire($(id), {}); } catch (e) {} } }); }
  }
  function resetForm() {
    clearFieldErrors(); $('np-err').className = 'np-err'; $('np-ok').textContent = '';
    ['np-address', 'np-cityzip', 'np-name', 'np-contract', 'np-notes', 'np-lat', 'np-lng', 'np-radius', 'np-neworg', 'np-other'].forEach(function (id) { $(id).value = ''; });
    addrClose(); $('np-filled').hidden = true;
    $('np-other-wrap').hidden = true; drawTabs([]);
    $('np-neworg-wrap').hidden = true; npDateSet($('np-start'), ''); npDateSet($('np-end'), '');
    NP.files = []; drawFiles(); $('np-dwg-note').textContent = ''; $('np-contract').readOnly = false; $('np-contract-note').textContent = '';
    $('np-type').disabled = false; $('np-borough').disabled = false; $('np-client').disabled = false; $('np-pm-wrap').hidden = false; $('np-pm-note').hidden = true;
    $('np-notes').closest('.np-field').hidden = false;
  }

  // ---- address: NYC suggestions while typing; a pick fills the borough, City / ZIP and site pin.
  // Outside NYC (Long Island, NJ) there are no suggestions and the fields are typed as before.
  var ADDR = { t: null, items: [], i: -1, seq: 0 };
  function addrClose() { var l = $('np-alist'); if (!l) return; l.hidden = true; l.innerHTML = ''; $('np-address').setAttribute('aria-expanded', 'false'); $('np-address').removeAttribute('aria-activedescendant'); ADDR.items = []; ADDR.i = -1; }
  function addrRender() {
    var l = $('np-alist');
    if (!ADDR.items.length) { addrClose(); return; }
    l.innerHTML = ADDR.items.map(function (it, k) {
      return '<li role="option" id="np-ao-' + k + '" data-k="' + k + '" aria-selected="' + (k === ADDR.i) + '">' + esc(it.address_line) + (it.city_zip ? '<small>' + esc(it.city_zip) + '</small>' : '') + '</li>';
    }).join('') + '<li class="foot" aria-hidden="true">NYC addresses · Long Island and NJ: type it in</li>';
    l.hidden = false; $('np-address').setAttribute('aria-expanded', 'true');
    if (ADDR.i >= 0) $('np-address').setAttribute('aria-activedescendant', 'np-ao-' + ADDR.i); else $('np-address').removeAttribute('aria-activedescendant');
  }
  function addrLookup() {
    var q = $('np-address').value.trim(); clearTimeout(ADDR.t);
    if (q.length < 3) { addrClose(); return; }
    ADDR.t = setTimeout(async function () {
      var seq = ++ADDR.seq;
      try { var rows = await getJSON('/api/address/suggest?q=' + encodeURIComponent(q)); if (seq !== ADDR.seq) return; ADDR.items = rows || []; ADDR.i = -1; addrRender(); }
      catch (e) { addrClose(); }   // the lookup is a convenience: typing always works
    }, 250);
  }
  function addrPick(k) {
    var it = ADDR.items[k]; if (!it) return;
    var filled = [];
    $('np-address').value = it.address_line;
    if (it.city_zip) { $('np-cityzip').value = it.city_zip; filled.push('ZIP'); }
    var b = $('np-borough');
    if (it.borough_code && b.querySelector('option[value="' + it.borough_code + '"]')) {
      if (!b.disabled) { if (b.value !== it.borough_code) { b.value = it.borough_code; b.dispatchEvent(new Event('change')); } filled.unshift('borough'); }
      else if (b.value && b.value !== it.borough_code) filled.push('note: this address is in ' + (it.city || it.borough_code) + ', the job code stays ' + b.value);
    }
    if (it.lat != null && it.lng != null && !$('np-lat').value.trim() && !$('np-lng').value.trim()) { $('np-lat').value = Number(it.lat).toFixed(6); $('np-lng').value = Number(it.lng).toFixed(6); filled.push('site pin'); }
    addrClose(); drawPhone();
    var note = filled.filter(function (x) { return x.indexOf('note:') !== 0; }), warn = filled.filter(function (x) { return x.indexOf('note:') === 0; });
    var f = $('np-filled');
    f.textContent = (note.length ? 'Filled from the address: ' + note.join(', ') + '. Change any of them.' : '') + (warn.length ? ' ' + warn[0].slice(6).replace(/^./, function (c) { return c.toUpperCase(); }) + '.' : '');
    f.hidden = !f.textContent.trim();
  }
  function addrKeys(e) {
    if ($('np-alist').hidden || !ADDR.items.length) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); var n = ADDR.items.length; ADDR.i = e.key === 'ArrowDown' ? (ADDR.i + 1) % n : (ADDR.i <= 0 ? n - 1 : ADDR.i - 1); addrRender(); }
    else if (e.key === 'Enter' && ADDR.i >= 0) { e.preventDefault(); addrPick(ADDR.i); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); addrClose(); }
  }
  function wireAddress() {
    $('np-address').addEventListener('input', function () { $('np-filled').hidden = true; addrLookup(); });
    $('np-address').addEventListener('keydown', addrKeys);
    $('np-address').addEventListener('blur', function () { setTimeout(addrClose, 150); });
    $('np-alist').addEventListener('mousedown', function (e) { var li = e.target.closest('li[data-k]'); if (li) { e.preventDefault(); addrPick(+li.dataset.k); } });
  }

  // open the form (optionally filled in: {address, name, borough, client_org_id})
  async function open(pre) {
    pre = pre || {};
    mount(); NP.mode = 'new'; NP.code = null; NP.cur = null; NP.fromProposal = pre.from_proposal_id || null;
    $('np-overlay').classList.add('open');
    $('np-title').textContent = 'New project'; $('np-go').textContent = 'Create project'; $('np-del').hidden = true;
    $('np-sub').textContent = 'For a job that is already won. It gets the next job number, a code in its series, a back-dated approved estimate so Finance stays truthful, the building link and its PM. New bids still start in Estimates.';
    try {
      await loadMeta(null); resetForm();
      $('np-no').textContent = '#' + NP.setup.next_job_number;
      NP.color = NP.setup.suggested_color; drawColors(); drawAccess(null); drawWork(null);
      renderCrewBoxes($('np-crew'), NP.meta.workers); codePreview();
      // #337 — prefill (a job site's address, borough and client)
      if (pre.address) $('np-address').value = pre.address;
      if (pre.name) $('np-name').value = pre.name;
      if (pre.client_org_id && $('np-client').querySelector('option[value="' + pre.client_org_id + '"]')) $('np-client').value = String(pre.client_org_id);
      if (pre.borough) { var want = String(pre.borough).trim().toLowerCase(); var opt = Array.prototype.slice.call($('np-borough').options).find(function (o) { return o.value.toLowerCase() === want || o.textContent.toLowerCase().indexOf(want) === 0; }); if (opt) $('np-borough').value = opt.value; }
      if (pre.contract_value != null && pre.contract_value !== '') $('np-contract').value = pre.contract_value;
      if (pre.est_type && $('np-type').querySelector('option[value="' + pre.est_type + '"]')) { $('np-type').value = pre.est_type; $('np-type').dispatchEvent(new Event('change')); }
      if (NP.fromProposal) {   // type and region come from the signed proposal's estimate and stay editable; the client is the proposal's
        $('np-sub').textContent = 'Filled in from signed proposal ' + (pre.proposal_label || '') + '. Check it and create: the job keeps the proposal\'s estimate, its client and the deposit invoice.';
        $('np-client').disabled = !!$('np-client').value;
      }
      drawPhone(); setTimeout(function () { $('np-type').focus(); }, 30);
    } catch (e) { showFormError('Could not load the form: ' + e.message); }
  }

  // edit an existing job; keepFiles = stay on the drawings that failed during a create
  async function edit(code, keepFiles) {
    mount(); var kept = keepFiles ? NP.files : [];
    NP.mode = 'edit'; NP.code = code; NP.fromProposal = null;
    $('np-overlay').classList.add('open');
    $('np-title').textContent = 'Edit project'; $('np-go').textContent = 'Save changes'; $('np-no').textContent = ''; $('np-del').hidden = false;
    $('np-sub').textContent = 'The job number, type and code never change: they are on every report and document.';
    try {
      await loadMeta(code); resetForm(); NP.files = kept; drawFiles();
      var res = await Promise.all([getJSON('/api/projects/' + encodeURIComponent(code) + '/details'), getJSON('/api/projects/' + encodeURIComponent(code) + '/profile'),
        getJSON('/api/admin/projects/' + encodeURIComponent(code) + '/crew'), getJSON('/api/admin/projects/' + encodeURIComponent(code) + '/setup'),
        getJSON('/api/projects/' + encodeURIComponent(code) + '/drawing-sets').catch(function () { return null; })]);
      var d = res[0], prof = res[1], crew = res[2], su = res[3], sets = res[4];
      NP.cur = { job_number: su.job_number, billing_type: prof.billing_type, contract_editable: !!d.contract_editable };
      $('np-no').textContent = su.job_number ? '#' + su.job_number : '';
      $('np-title').textContent = 'Edit ' + (d.name || code);
      var parts = code.split('-');
      $('np-type').value = parts[0]; $('np-type').disabled = true;
      $('np-borough').value = parts[1] || ''; $('np-borough').disabled = true;
      $('np-code').value = code; $('np-address').value = d.address || ''; $('np-cityzip').value = d.city_zip || ''; $('np-name').value = d.name || '';
      if (d.client_org_id && $('np-client').querySelector('option[value="' + d.client_org_id + '"]')) $('np-client').value = String(d.client_org_id);
      $('np-billing').value = prof.billing_type || '';
      $('np-contract').value = d.contract_value != null ? d.contract_value : ''; $('np-contract').readOnly = !d.contract_editable; $('np-contract-note').textContent = d.contract_note || '';
      npDateSet($('np-start'), d.start_date || ''); npDateSet($('np-end'), d.target_completion || '');
      $('np-pm-wrap').hidden = true; $('np-pm-note').hidden = false;
      $('np-notes').closest('.np-field').hidden = true;   // notes go on the job's estimate when it is created
      renderCrewBoxes($('np-crew'), crew.workers);
      var site = crew.site || {};
      $('np-lat').value = site.site_lat != null ? site.site_lat : ''; $('np-lng').value = site.site_lng != null ? site.site_lng : ''; $('np-radius').value = site.geofence_m || '';
      NP.color = su.color_index; drawColors(); drawAccess(su.access_methods); drawWork(su.worker_access);
      $('np-other').value = su.job_type_other || ''; drawOther();
      try { var mj = await getJSON('/api/projects/' + encodeURIComponent(code) + '/modules'); drawTabs(mj.modules.filter(function (x) { return x.enabled; }).map(function (x) { return x.key; })); } catch (e) { drawTabs([]); }
      var n = sets ? (Array.isArray(sets) ? sets.length : (sets.sets || []).length) : 0;
      $('np-dwg-note').innerHTML = n ? esc(n + (n === 1 ? ' drawing set' : ' drawing sets') + ' on file.') + ' <a href="/drawing-author">Set up elevations →</a>' : 'No drawing set on file yet.';
      drawPhone(); setTimeout(function () { $('np-address').focus(); }, 30);
    } catch (e) { showFormError('Could not load this project: ' + e.message); }
  }

  window.SSCNewProject = { open: open, edit: edit };
})();
