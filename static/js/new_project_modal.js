/* New project — ONE form, opened from anywhere (the Projects tab on /company and the
 * Assignments page at /admin/projects). Creates an already-won job through
 * POST /api/admin/projects/onboard (project_onboard.py).
 *
 *   SSCNewProject.open([prefill])   prefill: {address, name, borough, client_org_id}
 *   document event 'ssc:project-created'  detail = the onboard response data
 *
 * Only the required fields are up front (client, job type, borough, address, billing);
 * everything else sits under "More details". Per-field errors show together; nothing
 * silently defaults. Loads datepicker.js separately (the page includes it). */
(function () {
  'use strict';
  var CSS = `.np-overlay{position:fixed;inset:0;background:rgba(20,22,28,.45);display:none;align-items:center;justify-content:center;z-index:50;padding:16px;}
.np-overlay.open{display:flex;}
.np-modal{background:#fff;border-radius:14px;width:100%;max-width:600px;box-shadow:0 24px 64px rgba(20,22,28,.25);max-height:92vh;overflow:auto;}
.np-head{padding:18px 20px 6px;} .np-head h2{font-size:18px;font-weight:800;margin:0;} .np-head p{font-size:12.5px;color:var(--muted);margin:6px 0 0;}
.np-body{padding:14px 20px;display:grid;grid-template-columns:1fr 1fr;gap:12px;} .np-body .full{grid-column:1 / -1;}
.np-field label{display:block;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);margin-bottom:5px;}
.np-field input,.np-field select,.np-field textarea{width:100%;font:inherit;font-size:13px;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink);outline:none;box-sizing:border-box;}
.np-field input:focus,.np-field select:focus,.np-field textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);}
.np-err{grid-column:1 / -1;color:#A8382B;background:var(--status-behind-soft,#fceeec);border-radius:8px;padding:8px 10px;font-size:13px;display:none;} .np-err.show{display:block;}
.np-foot{padding:12px 20px 18px;display:flex;justify-content:flex-end;gap:8px;}
.np-site{display:flex;gap:8px;flex-wrap:wrap;align-items:center;} .np-site input{flex:1;min-width:110px;}
.np-hint{font-size:12px;color:var(--muted);margin-top:6px;}
.np-crew{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:6px 12px;max-height:220px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:10px;}
.np-crew label{display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;} .np-crew input{width:auto;} .np-crew .wid{font-weight:700;color:#B11E2E;}
.np-crew .none{grid-column:1 / -1;color:var(--muted);font-size:12.5px;}
.np-ferr{display:none;color:#A8382B;font-size:12px;margin-top:5px;} .np-ferr.show{display:block;}
.np-field.invalid input,.np-field.invalid select{border-color:#A8382B;box-shadow:0 0 0 3px #fceeec;}
.np-more{border:1px solid var(--line);border-radius:10px;padding:0 12px;} .np-more summary{cursor:pointer;padding:11px 0;font-size:13px;font-weight:700;color:var(--ink);list-style:none;} .np-more summary::-webkit-details-marker{display:none;} .np-more summary::before{content:"+ ";color:var(--accent);} .np-more[open] summary::before{content:"\\2212 ";} .np-more summary span{font-weight:400;color:var(--muted);font-size:12px;}
.np-more-body{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:2px 0 14px;} .np-more-body .full{grid-column:1 / -1;}
@media (max-width:600px){ .np-body,.np-more-body{grid-template-columns:1fr;} }
.np-foot .btn-ghost,.np-site .btn-ghost{font-size:12px;font-weight:700;padding:7px 14px;border-radius:8px;border:1px solid var(--line);background:#fff;color:var(--ink);cursor:pointer;}`;
  var HTML = `<div class="np-overlay" id="np-overlay" role="dialog" aria-modal="true" aria-labelledby="np-title">
  <form class="np-modal" id="np-form" novalidate>
    <div class="np-head"><h2 id="np-title">New project</h2><p>For a job that is already won. It gets a code in the job's series, a back-dated approved estimate so Finance stays truthful, the building link, and its PM. New bids still start in Estimates.</p></div>
    <div class="np-body">
      <div class="np-field"><label for="np-client">Client</label><select id="np-client" aria-describedby="np-ferr-client"><option value="">— choose a client —</option><option value="__new">+ New client…</option></select><div class="np-ferr" id="np-ferr-client" role="alert"></div></div>
      <div class="np-field" id="np-neworg-wrap" hidden><label for="np-neworg">New client name</label><input id="np-neworg" placeholder="Organization name" autocomplete="off" aria-describedby="np-ferr-neworg"><div class="np-ferr" id="np-ferr-neworg" role="alert"></div></div>
      <div class="np-field"><label for="np-type">Job type</label><select id="np-type" aria-describedby="np-ferr-type"></select><div class="np-ferr" id="np-ferr-type" role="alert"></div></div>
      <div class="np-field"><label for="np-borough">Borough / region</label><select id="np-borough" aria-describedby="np-ferr-borough"></select><div class="np-ferr" id="np-ferr-borough" role="alert"></div></div>
      <div class="np-field full"><label for="np-address">Building address</label><input id="np-address" placeholder="890 E 135th St, Bronx" autocomplete="off" aria-describedby="np-ferr-address"><div class="np-ferr" id="np-ferr-address" role="alert"></div></div>
      <div class="np-field full"><label for="np-billing">How is it billed?</label><select id="np-billing" aria-describedby="np-ferr-billing"><option value="">— choose —</option></select><div class="np-hint">AIA / Schedule of values gets the SOV and pay applications</div><div class="np-ferr" id="np-ferr-billing" role="alert"></div></div>
      <details class="np-more full" id="np-more">
        <summary>More details <span>(optional — you can add or change all of this later from the project)</span></summary>
        <div class="np-more-body">
          <div class="np-field full"><label for="np-name">Project name</label><input id="np-name" placeholder="defaults to the address" autocomplete="off"></div>
          <div class="np-field"><label for="np-contract">Contract value (USD)</label><input id="np-contract" inputmode="decimal" placeholder="optional" autocomplete="off"></div>
          <div class="np-field"><label for="np-pm">Project manager</label><select id="np-pm"><option value="">— assign later —</option></select></div>
          <div class="np-field"><label for="np-start">Start date</label><input id="np-start" type="date" class="ssc-date"></div>
          <div class="np-field"><label for="np-end">Target completion</label><input id="np-end" type="date" class="ssc-date" title="Optional — the job shows on the calendar from start to this date"></div>
          <div class="np-field full"><label for="np-notes">Notes</label><input id="np-notes" placeholder="optional" autocomplete="off"></div>
          <div class="np-field full"><label>Site location · worker app check-in</label>
            <div class="np-site"><input id="np-lat" inputmode="decimal" placeholder="Latitude" autocomplete="off"><input id="np-lng" inputmode="decimal" placeholder="Longitude" autocomplete="off"><input id="np-radius" inputmode="numeric" placeholder="200 m" autocomplete="off" style="max-width:90px"><button type="button" class="btn-ghost" id="np-geo">Use my location</button></div>
            <div class="np-hint">Workers can only check in within this radius. Leave blank and the job accepts check-ins from anywhere until you set it (from the site, tap "Use my location").</div></div>
          <div class="np-field full"><label>Crew · who can check in to this job</label><div class="np-crew" id="np-crew"></div></div>
        </div>
      </details>
      <div class="np-err" id="np-err" role="alert"></div>
    </div>
    <div class="np-foot"><button type="button" class="btn-ghost" id="np-cancel">Cancel</button><button type="submit" class="btn-primary" id="np-go">Create project</button></div>
  </form>
</div>`;
  var st = document.createElement('style'); st.id = 'np-style'; st.textContent = CSS; document.head.appendChild(st);

  var NP = { meta: null, mounted: false };
  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function npDateGet(el) { var p = window.SSCDatePicker; if (p && typeof p.getISO === 'function') { var v = p.getISO(el); if (v !== undefined && v !== null) return v; } return el.value || ''; }
  function npDateSet(el, iso) { var p = window.SSCDatePicker; if (p && typeof p.setISO === 'function') { try { p.setISO(el, iso || ''); return; } catch (e) {} } el.value = iso || ''; }

  // ---- crew checkboxes + "use my location"
  function renderCrewBoxes(host, workers) {
    if (!workers || !workers.length) { host.innerHTML = '<div class="none">No active workers yet. Onboard them from Workforce first.</div>'; return; }
    host.innerHTML = workers.map(function (w) { return '<label><input type="checkbox" value="' + esc(w.employee_id) + '"><span><span class="wid">' + esc(w.worker_id || w.employee_id) + '</span> — ' + esc(w.name || '') + (w.trade ? ' <span style="color:var(--muted)">· ' + esc(w.trade) + '</span>' : '') + '</span></label>'; }).join('');
  }
  function crewChecked(host) { return Array.prototype.map.call(host.querySelectorAll('input[type=checkbox]:checked'), function (i) { return i.value; }); }
  function showFormError(msg) { var err = $('np-err'); err.textContent = msg; err.className = 'np-err show'; err.scrollIntoView({ block: 'nearest' }); }
  function useMyLocation(btn) {
    if (!navigator.geolocation) { showFormError('This browser cannot read your location.'); return; }
    btn.disabled = true; btn.textContent = 'Locating…';
    navigator.geolocation.getCurrentPosition(function (pos) { $('np-lat').value = pos.coords.latitude.toFixed(6); $('np-lng').value = pos.coords.longitude.toFixed(6); btn.disabled = false; btn.textContent = 'Use my location'; },
      function () { btn.disabled = false; btn.textContent = 'Use my location'; showFormError('Location not available — allow location access and try again, or type the coordinates.'); }, { enableHighAccuracy: true, timeout: 12000 });
  }

  // ---- per-field errors (all at once, next to the field)
  var NP_FIELDS = ['client', 'neworg', 'type', 'borough', 'address', 'billing'];
  function clearFieldErrors() { NP_FIELDS.forEach(function (k) { var e = $('np-ferr-' + k); if (e) { e.textContent = ''; e.classList.remove('show'); var f = e.closest('.np-field'); if (f) f.classList.remove('invalid'); } }); }
  function fieldError(k, msg) { var e = $('np-ferr-' + k); e.textContent = msg; e.classList.add('show'); e.closest('.np-field').classList.add('invalid'); }
  function validate(payload) {
    var bad = [];
    if (!payload.client_org_id && !payload.new_org_name) { if ($('np-client').value === '__new') bad.push(['neworg', 'Type the new client name.']); else bad.push(['client', 'Choose a client or add a new one.']); }
    if (!payload.est_type) bad.push(['type', 'Choose the job type.']);
    if (!payload.borough) bad.push(['borough', 'Choose the borough or region.']);
    if (payload.address.length < 4) bad.push(['address', 'Enter the building address.']);
    if (!payload.billing_type) bad.push(['billing', 'Choose how the job is billed.']);   // #356
    return bad;
  }

  function mount() {
    if (NP.mounted) return; NP.mounted = true;
    var host = document.createElement('div'); host.innerHTML = HTML; document.body.appendChild(host.firstElementChild);
    $('np-cancel').addEventListener('click', close);
    $('np-overlay').addEventListener('click', function (e) { if (e.target === $('np-overlay')) close(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && $('np-overlay').classList.contains('open')) close(); });
    $('np-geo').addEventListener('click', function () { useMyLocation($('np-geo')); });
    $('np-client').addEventListener('change', function () { var isNew = $('np-client').value === '__new'; $('np-neworg-wrap').hidden = !isNew; if (isNew) setTimeout(function () { $('np-neworg').focus(); }, 30); });
    ['np-client', 'np-neworg', 'np-type', 'np-borough', 'np-address', 'np-billing'].forEach(function (id) { var el = $(id); var ev = el.tagName === 'SELECT' ? 'change' : 'input'; el.addEventListener(ev, function () { var f = el.closest('.np-field'); if (f) { f.classList.remove('invalid'); var m = f.querySelector('.np-ferr'); if (m) { m.classList.remove('show'); m.textContent = ''; } } }); });
    $('np-form').addEventListener('submit', submit);
  }
  function close() { $('np-overlay').classList.remove('open'); }

  async function submit(e) {
    e.preventDefault(); var err = $('np-err'); err.className = 'np-err';
    var isNew = $('np-client').value === '__new';
    var payload = { client_org_id: isNew ? null : ($('np-client').value || null), new_org_name: isNew ? $('np-neworg').value.trim() : '',
      est_type: $('np-type').value, borough: $('np-borough').value, address: $('np-address').value.trim(), name: $('np-name').value.trim(),
      contract_value: $('np-contract').value.trim() || null, start_date: npDateGet($('np-start')) || null, target_completion: npDateGet($('np-end')) || null, pm_user_id: $('np-pm').value || null, notes: $('np-notes').value.trim(), billing_type: $('np-billing').value || null,
      site_lat: $('np-lat').value.trim() || null, site_lng: $('np-lng').value.trim() || null, geofence_m: $('np-radius').value.trim() || null, crew: crewChecked($('np-crew')) };
    clearFieldErrors();
    var bad = validate(payload);
    if (bad.length) { bad.forEach(function (b) { fieldError(b[0], b[1]); }); var first = $('np-ferr-' + bad[0][0]).closest('.np-field'); var fe = first.querySelector('input,select'); if (fe) { fe.scrollIntoView({ block: 'center' }); fe.focus(); } return; }
    if ((payload.site_lat && !payload.site_lng) || (!payload.site_lat && payload.site_lng)) { $('np-more').open = true; showFormError('Enter both latitude and longitude, or leave both blank.'); return; }
    $('np-go').disabled = true;
    try {
      var r = await fetch('/api/admin/projects/onboard', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      var j = await r.json().catch(function () { return {}; });
      if (!r.ok) throw new Error(j.error || ('Request failed (' + r.status + ')'));
      close(); NP.meta = null;
      document.dispatchEvent(new CustomEvent('ssc:project-created', { detail: j.data }));
    } catch (ex) { showFormError(ex.message); }
    finally { $('np-go').disabled = false; }
  }

  // open the form (optionally filled in: {address, name, borough, client_org_id})
  async function open(pre) {
    pre = pre || {};
    mount();
    var err = $('np-err'); err.className = 'np-err'; $('np-overlay').classList.add('open');
    try {
      if (!NP.meta) { var r = await fetch('/api/admin/projects/onboard-meta').then(function (x) { return x.json(); }); if (r.error) throw new Error(r.error); NP.meta = r.data; }
      var m = NP.meta;
      $('np-client').innerHTML = '<option value="">— choose a client —</option>' + m.clients.map(function (c) { return '<option value="' + c.id + '">' + esc(c.name) + '</option>'; }).join('') + '<option value="__new">+ New client…</option>';
      $('np-type').innerHTML = '<option value="">— choose job type —</option>' + m.types.map(function (t) { return '<option value="' + esc(t.code) + '">' + esc(t.code) + ' · ' + esc(t.label) + '</option>'; }).join('');
      $('np-borough').innerHTML = '<option value="">— choose borough / region —</option>' + (m.regions || m.boroughs.map(function (b) { return { code: b, label: b }; })).map(function (r) { return '<option value="' + esc(r.code) + '">' + esc(r.label) + ' (' + esc(r.code) + ')</option>'; }).join('');   // #331
      $('np-billing').innerHTML = '<option value="">— choose —</option>' + (m.billing_types || []).map(function (b) { return '<option value="' + esc(b.code) + '">' + esc(b.label) + '</option>'; }).join('');   // #356
      $('np-pm').innerHTML = '<option value="">— assign later —</option>' + m.pms.map(function (p) { return '<option value="' + p.user_id + '">' + esc(p.name) + '</option>'; }).join('');
      var p = window.SSCDatePicker; if (p && typeof p.wire === 'function') { ['np-start', 'np-end'].forEach(function (id) { if (!$(id).dataset.sscDp) { try { p.wire($(id), {}); } catch (e) {} } }); }
      npDateSet($('np-start'), ''); npDateSet($('np-end'), '');   // no silent default: an undated job lands in the calendar's undated bucket (#342)
      $('np-more').open = false; clearFieldErrors();
      $('np-neworg-wrap').hidden = true; $('np-neworg').value = ''; $('np-address').value = ''; $('np-name').value = ''; $('np-contract').value = ''; $('np-notes').value = '';
      $('np-lat').value = ''; $('np-lng').value = ''; $('np-radius').value = ''; renderCrewBoxes($('np-crew'), m.workers);
      // #337 — prefill (a job site's address, borough and client)
      if (pre.address) $('np-address').value = pre.address;
      if (pre.name) { $('np-name').value = pre.name; $('np-more').open = true; }
      if (pre.client_org_id && $('np-client').querySelector('option[value="' + pre.client_org_id + '"]')) $('np-client').value = String(pre.client_org_id);
      if (pre.borough) { var want = String(pre.borough).trim().toLowerCase(); var opt = Array.prototype.slice.call($('np-borough').options).find(function (o) { return o.value.toLowerCase() === want || o.textContent.toLowerCase().indexOf(want) === 0; }); if (opt) $('np-borough').value = opt.value; }
      setTimeout(function () { $('np-client').focus(); }, 30);
    } catch (e) { showFormError('Could not load the form: ' + e.message); }
  }

  window.SSCNewProject = { open: open };
})();
