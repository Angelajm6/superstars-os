/* Mark an invoice as sent (2026-11-13, prototype https://claude.ai/artifact/CSYZKHDEipWfsF69Nb3xmG).
   For an invoice that went out another way: the Gmail app, Outlook, a client portal, by hand. Nothing is emailed.
   The app looks in the person's sent mail for an email that mentions the invoice number; picking it fills in the
   date and the people and files that email on the invoice and the client.

   SSCMarkSent.open(invoiceId, { onDone(invoice, info) })   info = { how, mail_id, chase }
   SSCMarkSent.undo(invoiceId, mailId) -> Promise<invoice>   back to "not sent" (and the chase task cancelled)
   Used by the job's Invoices & payments list and by the invoice editor's Email window. */
(function () {
  if (window.SSCMarkSent) return;
  const CSS = `
  .ms-ov{position:fixed;inset:0;background:rgba(20,22,28,.45);display:flex;align-items:center;justify-content:center;z-index:10003;padding:16px}
  .ms-ov[hidden]{display:none}
  .ms-sheet{background:#fff;color:var(--ink,#222633);border-radius:16px;box-shadow:0 24px 64px rgba(20,22,28,.25);width:min(600px,100%);max-height:100%;display:flex;flex-direction:column;overflow:hidden;font-size:14px;line-height:1.45}
  .ms-head{display:flex;gap:12px;align-items:flex-start;padding:18px 20px 12px;border-bottom:1px solid var(--line,#eceef3)}
  .ms-head h3{margin:0;font-size:18px;font-weight:600}
  .ms-sub{color:var(--muted,#8c92a0);font-size:13px;margin-top:3px}
  .ms-x{margin-left:auto;border:0;background:none;color:var(--muted,#8c92a0);font-size:22px;line-height:1;padding:2px 6px;border-radius:6px;cursor:pointer}
  .ms-body{overflow:auto;padding:16px 20px 18px;display:flex;flex-direction:column;gap:16px}
  .ms-l{font-size:12px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted,#8c92a0);margin-bottom:7px}
  .ms-opts{display:flex;flex-direction:column;gap:6px}
  .ms-opt{display:grid;grid-template-columns:18px minmax(0,1fr) auto;gap:10px;align-items:start;border:1px solid var(--line,#eceef3);border-radius:10px;padding:10px 12px;background:#fff;text-align:left;width:100%;font:inherit;color:inherit;cursor:pointer}
  .ms-opt[aria-checked="true"]{border-color:var(--accent,#4364dc);background:var(--accent-soft,#ebf0fe)}
  .ms-opt .rd{width:16px;height:16px;border-radius:50%;border:2px solid var(--line-2,#dfe2ea);margin-top:2px;box-sizing:border-box}
  .ms-opt[aria-checked="true"] .rd{border:5px solid var(--accent,#4364dc)}
  .ms-opt .t1{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ms-opt .t2{color:var(--ink-2,#4a5061);font-size:13px;overflow-wrap:anywhere}
  .ms-opt .when{color:var(--muted,#8c92a0);font-size:12px;white-space:nowrap;text-align:right}
  .ms-tag{display:inline-block;font-size:11px;font-weight:700;border-radius:5px;padding:1px 6px;background:var(--status-complete-soft,#e7f6f0);color:var(--status-complete-deep,#0c7a5f);margin-left:6px;vertical-align:1px}
  .ms-file{display:inline-block;margin-top:5px;border:1px solid var(--line-2,#dfe2ea);border-radius:6px;padding:1px 7px;font-size:12px;color:var(--ink-2,#4a5061);max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ms-note{color:var(--muted,#8c92a0);font-size:13px;margin-top:6px}
  .ms-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px}
  .ms-f label{display:block;font-size:12px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted,#8c92a0);margin-bottom:5px}
  .ms-f input,.ms-f select{width:100%;box-sizing:border-box;font:inherit;font-size:14px;padding:9px 10px;border:1px solid var(--line-2,#dfe2ea);border-radius:8px;background:#fff;color:var(--ink,#222633)}
  .ms-f input:focus,.ms-f select:focus{outline:none;border-color:var(--accent,#4364dc);box-shadow:0 0 0 3px var(--accent-soft,#ebf0fe)}
  .ms-fu{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;border:1px solid var(--line,#eceef3);border-radius:10px;padding:10px 12px}
  .ms-fu b{display:block}
  .ms-fu small{color:var(--muted,#8c92a0);font-size:13px}
  .ms-fu select{font:inherit;font-size:13px;border:1px solid var(--line-2,#dfe2ea);border-radius:8px;padding:6px 8px;background:#fff;color:inherit}
  .ms-info{font-size:13px;color:var(--ink-2,#4a5061);background:var(--canvas,#f4f6fa);border-radius:10px;padding:10px 12px}
  .ms-err{color:var(--status-behind-deep,#A8382B);background:var(--status-behind-soft,#fceeec);border-radius:8px;padding:8px 10px;font-size:13px}
  .ms-err:empty{display:none}
  .ms-foot{display:flex;align-items:center;gap:8px;padding:12px 20px 16px;border-top:1px solid var(--line,#eceef3);flex-wrap:wrap}
  .ms-foot .note{color:var(--muted,#8c92a0);font-size:13px;margin-right:auto}
  .ms-btn{font:inherit;font-size:14px;font-weight:700;padding:8px 14px;border-radius:8px;border:1px solid var(--line-2,#dfe2ea);background:#fff;color:var(--ink,#222633);cursor:pointer}
  .ms-btn.primary{background:var(--accent,#4364dc);border-color:transparent;color:#fff}
  .ms-btn:disabled{opacity:.55;cursor:not-allowed}
  .ms-load{color:var(--muted,#8c92a0);font-size:13px;padding:6px 2px}
  @media (max-width:640px){
    .ms-ov{padding:0}
    .ms-sheet{border-radius:0;width:100%;height:100%}
    .ms-head{padding:14px 16px 10px;padding-top:max(14px,env(safe-area-inset-top,0px))}
    .ms-body{padding:14px 16px;flex:1}
    .ms-grid{grid-template-columns:minmax(0,1fr)}
    .ms-foot{padding:12px 16px max(14px,env(safe-area-inset-bottom,0px))}
    .ms-foot .note{width:100%}
    .ms-foot .ms-btn{flex:1}
  }`;
  const HTML = `
  <div class="ms-sheet" role="dialog" aria-modal="true" aria-labelledby="ms-title">
    <div class="ms-head"><div style="min-width:0"><h3 id="ms-title">Mark as sent</h3><div class="ms-sub">For an invoice you sent from Gmail, Outlook, a client portal or by hand. Nothing is emailed.</div></div>
      <button class="ms-x" type="button" id="ms-x" aria-label="Close">×</button></div>
    <div class="ms-body">
      <section><div class="ms-l">How did it go out?</div>
        <div class="ms-opts" id="ms-opts" role="radiogroup" aria-label="How it was sent"><div class="ms-load">Looking in your sent mail…</div></div>
        <div class="ms-note" id="ms-found-note" hidden>Found by the invoice number in your sent mail. Choosing it fills in the date and people, and files that email on the invoice and the client.</div>
      </section>
      <section id="ms-manual" hidden>
        <div class="ms-grid">
          <div class="ms-f"><label for="ms-date">Sent on</label><input type="date" id="ms-date"></div>
          <div class="ms-f"><label for="ms-how">How</label><select id="ms-how"><option value="email">Email (Outlook or other)</option><option value="portal">Client portal</option><option value="hand">Handed over or mailed</option></select></div>
        </div>
        <div class="ms-f" style="margin-top:10px"><label for="ms-to">Sent to</label><input id="ms-to" autocomplete="off" list="ms-people" placeholder="Name or email"><datalist id="ms-people"></datalist></div>
      </section>
      <section><div class="ms-l">After this</div>
        <div class="ms-fu"><div><b>Chase it if it isn't paid</b><small id="ms-fu-note"></small></div>
          <select id="ms-fu" aria-label="When to follow up"></select></div>
      </section>
      <div class="ms-info" id="ms-info"></div>
      <div class="ms-err" id="ms-err" role="alert"></div>
    </div>
    <div class="ms-foot"><div class="note">Nothing is emailed.</div><button class="ms-btn" type="button" id="ms-cancel">Cancel</button><button class="ms-btn primary" type="button" id="ms-go">Mark as sent</button></div>
  </div>`;
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pad = n => String(n).padStart(2, '0');
  const today = () => { const d = new Date(); return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); };
  const isoDay = v => String(v || '').slice(0, 10);
  const nice = v => { const s = isoDay(v); if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return ''; const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }); };
  const niceTime = v => { const s = String(v || ''); const m = s.match(/T(\d{2}):(\d{2})/); if (!m) return ''; const h = +m[1]; return ((h % 12) || 12) + ':' + m[2] + (h < 12 ? ' AM' : ' PM'); };
  const addDays = (iso, n) => { const [y, m, d] = iso.split('-').map(Number); const t = new Date(y, m - 1, d + n); return t.getFullYear() + '-' + pad(t.getMonth() + 1) + '-' + pad(t.getDate()); };
  const firstName = s => { const v = String(s || '').split(',')[0].trim(); const m = v.match(/^\s*"?([^"<]+?)"?\s*</); return (m ? m[1] : v.replace(/<.*>/, '')).trim(); };
  const dGet = el => { const p = window.SSCDatePicker; if (p && typeof p.getISO === 'function') { const v = p.getISO(el); if (v !== undefined && v !== null) return v; } return el.value || ''; };
  const dSet = (el, iso) => { const p = window.SSCDatePicker; if (p && typeof p.setISO === 'function') { try { p.setISO(el, iso || ''); return; } catch (e) {} } el.value = iso || ''; };
  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ credentials: 'same-origin', headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' } }, opts || {}));
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error((j && j.error) || ('Request failed (' + res.status + ')'));
    return j.data;
  }
  let ov = null, S = null, lastFocus = null;
  const $m = id => document.getElementById(id);
  function build() {
    if (ov) return;
    const st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);
    ov = document.createElement('div'); ov.className = 'ms-ov'; ov.hidden = true; ov.innerHTML = HTML; document.body.appendChild(ov);
    ov.addEventListener('click', e => { if (e.target === ov) close(); });
    ov.addEventListener('keydown', e => { if (e.key === 'Escape') { e.stopPropagation(); close(); } });
    $m('ms-x').addEventListener('click', close); $m('ms-cancel').addEventListener('click', close);
    ['ms-date', 'ms-to', 'ms-how', 'ms-fu'].forEach(id => { $m(id).addEventListener('input', sync); $m(id).addEventListener('change', sync); });
    $m('ms-go').addEventListener('click', go);
    $m('ms-opts').addEventListener('keydown', e => {
      if (!S || !['ArrowDown', 'ArrowUp'].includes(e.key)) return;
      const all = [...$m('ms-opts').querySelectorAll('[data-pick]')]; if (!all.length) return; e.preventDefault();
      const i = all.findIndex(b => b.dataset.pick === S.pick);
      const n = all[(i + (e.key === 'ArrowDown' ? 1 : all.length - 1)) % all.length]; S.pick = n.dataset.pick; sync(); n.focus();
    });
  }
  function close() { if (!ov || ov.hidden) return; ov.hidden = true; S = null; if (lastFocus && lastFocus.focus) lastFocus.focus(); }
  function chosen() { return S && S.pick !== 'other' ? S.matches.find(m => String(m.id) === S.pick) : null; }
  function fuOptions() {
    const inv = S.inv, t = today(), due = isoDay(inv.due_at);
    const past = !!(due && due < t);
    const opts = past
      ? [['', 'Off'], ['tomorrow', 'Tomorrow'], ...(addDays(due, 7) > t ? [['after7', '7 days past due']] : [])]
      : [['', 'Off'], ...(due && addDays(due, -3) > t ? [['before3', '3 days before due']] : []), ['due', 'On the due date'], ['after7', '7 days past due']];
    $m('ms-fu').innerHTML = opts.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('');
    $m('ms-fu').value = past ? 'tomorrow' : 'due';
    S.past = past;
  }
  function renderOpts() {
    const rows = S.matches.map(m => `<button type="button" class="ms-opt" role="radio" aria-checked="false" data-pick="${m.id}">
      <span class="rd"></span><span style="min-width:0"><div class="t1">${esc(m.subject)}${m.client ? '<span class="ms-tag">To the client</span>' : ''}</div>
      <div class="t2">To ${esc(m.to)}${m.cc ? ' · Cc ' + esc(m.cc) : ''}</div>${(m.files || []).map(f => `<span class="ms-file">${esc(f)}</span>`).join(' ')}</span>
      <span class="when">${esc(nice(m.received_at))}<br>${esc(niceTime(m.received_at))}</span></button>`);
    rows.push(`<button type="button" class="ms-opt" role="radio" aria-checked="false" data-pick="other"><span class="rd"></span>
      <span style="min-width:0"><div class="t1">Another way</div><div class="t2">${S.gmail ? 'Outlook, a client portal, by hand, or an email not listed here' : 'Gmail, Outlook, a client portal or by hand. Connect your Gmail on the Inbox page and the app finds the email for you.'}</div></span><span></span></button>`);
    $m('ms-opts').innerHTML = rows.join('');
    $m('ms-opts').querySelectorAll('[data-pick]').forEach(b => b.addEventListener('click', () => { S.pick = b.dataset.pick; sync(); }));
  }
  function sync() {
    if (!S) return;
    $m('ms-opts').querySelectorAll('[data-pick]').forEach(b => { const on = b.dataset.pick === S.pick; b.setAttribute('aria-checked', String(on)); b.tabIndex = on ? 0 : -1; });
    const m = chosen();
    $m('ms-manual').hidden = !!m; $m('ms-found-note').hidden = !m;
    const when = m ? nice(m.received_at) : nice(dGet($m('ms-date')));
    const who = m ? firstName(m.to) : $m('ms-to').value.trim();
    const missing = !m && (!dGet($m('ms-date')) || !who);
    $m('ms-go').disabled = missing || S.busy;
    $m('ms-info').innerHTML = missing ? 'Add the day it was sent and who it went to.'
      : `The invoice will show <b>sent ${esc(when)}${who ? ' · ' + esc(firstName(who)) : ''}</b>, and its email button becomes <b>Resend</b>.` + (m ? ' Your next reminder can go in that same thread.' : '');
    const fu = $m('ms-fu').value;
    $m('ms-fu-note').textContent = !fu ? 'No task. You can still chase it from Receivables.'
      : fu === 'tomorrow' ? `It's past due, so a task tomorrow reminds you to chase it${m ? ' in that thread' : ''}.`
      : 'A task reminds you on that day if it is still open. It closes itself when the invoice is paid.';
  }
  async function open(id, opts) {
    build();
    lastFocus = document.activeElement;
    S = { id, opts: opts || {}, matches: [], pick: 'other', inv: null, gmail: false, busy: false };
    $m('ms-err').textContent = ''; $m('ms-title').textContent = 'Mark as sent';
    $m('ms-opts').innerHTML = '<div class="ms-load">Looking in your sent mail…</div>';
    $m('ms-manual').hidden = true; $m('ms-found-note').hidden = true; $m('ms-info').textContent = ''; $m('ms-go').disabled = true;
    dSet($m('ms-date'), today()); $m('ms-to').value = ''; $m('ms-how').value = 'email'; $m('ms-fu').innerHTML = '';
    ov.hidden = false; setTimeout(() => $m('ms-x').focus(), 30);
    let data;
    try { data = await api('/api/invoices/' + encodeURIComponent(id) + '/sent-matches'); }
    catch (e) { if (S && S.id === id) { $m('ms-opts').innerHTML = ''; $m('ms-err').textContent = e.message; } return; }
    if (!S || S.id !== id) return;
    S.inv = data.invoice; S.matches = data.matches || []; S.gmail = !!(data.gmail && data.gmail.email);
    S.pick = S.matches.length ? String(S.matches[0].id) : 'other';
    $m('ms-title').textContent = 'Mark ' + S.inv.number + ' as sent';
    const rec = data.recipient || {};
    $m('ms-to').value = rec.name && rec.email ? `${rec.name} <${rec.email}>` : (rec.email || rec.name || '');
    $m('ms-people').innerHTML = rec.email ? `<option value="${esc(rec.email)}">${esc(rec.name || '')}</option>` : '';
    renderOpts(); fuOptions(); sync();
    const sel = $m('ms-opts').querySelector('[aria-checked="true"]'); if (sel) sel.focus();
  }
  async function go() {
    if (!S || S.busy) return;
    const m = chosen();
    const body = { confirm: true, follow_up: $m('ms-fu').value };
    if (m) body.mail_id = m.id;
    else Object.assign(body, { sent_on: dGet($m('ms-date')), to: $m('ms-to').value.trim(), how: $m('ms-how').value });
    S.busy = true; $m('ms-go').disabled = true; $m('ms-go').textContent = 'Marking…'; $m('ms-err').textContent = '';
    try {
      const d = await api('/api/invoices/' + encodeURIComponent(S.id) + '/mark-sent', { method: 'POST', body: JSON.stringify(body) });
      const cb = S.opts.onDone; close();
      if (typeof cb === 'function') cb(d.invoice, { how: d.how, chase: d.chase, mail_id: m ? m.id : null });
    } catch (e) { if (S) { $m('ms-err').textContent = e.message; S.busy = false; sync(); } }
    finally { const b = $m('ms-go'); if (b) b.textContent = 'Mark as sent'; }
  }
  async function undo(id, mailId) {
    const d = await api('/api/invoices/' + encodeURIComponent(id) + '/unmark-sent', { method: 'POST', body: JSON.stringify({ mail_id: mailId || null }) });
    return d.invoice;
  }
  window.SSCMarkSent = { open, undo, close };
})();
