/* Payroll week tools — shared by Weekly Payroll (Finance) and the Hours Log (Workforce).
 *
 *   SSCPayrollWeek.attachPicker(button, {getCurrent, onPick})
 *       a month calendar whose rows are Mon–Sun payroll weeks; each week shows its
 *       paid hours and a dot when days still need approval (/api/payroll/weeks)
 *   SSCPayrollWeek.renderApprovals(container, grid, {onChange})
 *       the "Needs approval" list for days over 8 hours (/api/payroll/overtime/decide)
 *   SSCPayrollWeek.cellBadge(day)      the 8 + 2 pending / 10 ✓ / +2 rejected chip
 *   SSCPayrollWeek.reasonDialog(opts)  -> Promise<string|null>, the over-8 reason ask
 *
 * Dates are LOCAL (never toISOString). Dialogs render at document.body (DESIGN_SYSTEM §3).
 */
(function () {
  'use strict';
  if (window.SSCPayrollWeek) return;

  const LIMIT = 8;
  const esc = (s) => (s == null ? '' : String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'));
  const pad = (n) => String(n).padStart(2, '0');
  const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const parse = (s) => new Date(s + 'T00:00:00');
  const mondayOf = (d) => { const m = new Date(d); m.setDate(m.getDate() - ((m.getDay() + 6) % 7)); m.setHours(0, 0, 0, 0); return m; };
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const hrs = (n) => { const v = Math.round(Number(n || 0) * 100) / 100; return String(v); };
  const fmtDay = (s) => parse(s).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });

  function injectStyles() {
    if (document.getElementById('spw-styles')) return;
    const st = document.createElement('style');
    st.id = 'spw-styles';
    st.textContent = `
.spw-trigger{cursor:pointer;border:0;font:inherit;color:inherit;background:inherit;display:inline-flex;align-items:center;gap:7px}
.spw-trigger::before{content:"";width:14px;height:14px;flex:none;background:currentColor;
  -webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round'%3E%3Crect x='3' y='5' width='18' height='16' rx='2'/%3E%3Cpath d='M3 10h18M8 3v4M16 3v4'/%3E%3C/svg%3E") center/contain no-repeat;
  mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round'%3E%3Crect x='3' y='5' width='18' height='16' rx='2'/%3E%3Cpath d='M3 10h18M8 3v4M16 3v4'/%3E%3C/svg%3E") center/contain no-repeat}
.spw-trigger:hover{text-decoration:underline;text-underline-offset:3px}
.spw-pop{position:fixed;z-index:10050;background:#fff;border:1px solid var(--line,#eceef3);border-radius:14px;box-shadow:0 18px 44px rgba(20,22,28,.18);
  padding:14px;width:min(340px,calc(100vw - 24px));font-family:inherit;color:var(--ink,#222633)}
.spw-pop[hidden]{display:none}
.spw-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}
.spw-head b{font-size:15px}
.spw-head button,.spw-quick button{font:inherit;font-size:13px;font-weight:600;border:1px solid var(--line,#eceef3);background:#fff;color:var(--accent,#4364dc);border-radius:8px;min-width:34px;min-height:34px;cursor:pointer}
.spw-quick{display:flex;gap:6px;margin-bottom:10px}
.spw-quick button{flex:1;padding:0 10px}
.spw-dow,.spw-week{display:grid;grid-template-columns:repeat(7,1fr) 54px;align-items:center}
.spw-dow span{font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted,#8c92a0);text-align:center;padding:4px 0}
.spw-week{width:100%;border:1px solid transparent;background:none;border-radius:10px;padding:0;font:inherit;color:inherit;cursor:pointer;min-height:40px}
.spw-week:hover{background:var(--accent-soft,#ebf0fe)}
.spw-week.sel{background:var(--accent,#4364dc);color:#fff}
.spw-week.sel .spw-out{color:rgba(255,255,255,.55)}
.spw-week.sel .spw-h{color:#fff}
.spw-week span{text-align:center;font-size:13px;font-variant-numeric:tabular-nums}
.spw-out{color:#c4c8d1}
.spw-today{font-weight:700;text-decoration:underline;text-underline-offset:3px}
.spw-h{font-size:12px!important;color:var(--muted,#8c92a0);font-weight:600;display:inline-flex;align-items:center;justify-content:flex-end;gap:4px;padding-right:6px}
.spw-dot{width:7px;height:7px;border-radius:50%;background:var(--status-warn,#9A5B00);display:inline-block}
.spw-legend{margin-top:8px;font-size:12px;color:var(--muted,#8c92a0);display:flex;gap:12px;flex-wrap:wrap}
.spw-week:focus-visible,.spw-head button:focus-visible,.spw-quick button:focus-visible,.spw-btn:focus-visible{outline:2px solid var(--accent,#4364dc);outline-offset:2px}
@media (max-width:520px){.spw-pop{left:12px!important;right:12px;bottom:12px;top:auto!important;width:auto;border-radius:18px;padding-bottom:max(14px,env(safe-area-inset-bottom,0px))}.spw-week{min-height:46px}}
.spw-ot{display:inline-flex;align-items:center;gap:3px;border-radius:7px;padding:1px 7px;font-weight:700;font-size:13px;font-variant-numeric:tabular-nums;white-space:nowrap}
.spw-ot.pending,.spw-ot.missing{background:var(--status-warn-soft,#FFF4E5);color:var(--status-warn,#9A5B00);border:1px solid var(--status-warn-edge,#E8C9A0)}
.spw-ot.approved{background:var(--status-complete-soft,#e7f6f0);color:var(--status-complete-deep,#0c7a5f)}
.spw-ot.rejected{background:var(--status-behind-soft,#fceeec);color:var(--status-behind-deep,#A8382B)}
.spw-ot.rejected s{opacity:.8}
.spw-inactive{display:inline-block;background:var(--status-oat-soft,#f3f0e9);border:1px solid var(--status-oat,#cabfad);color:var(--oat-ink,#8a8064);border-radius:999px;padding:1px 8px;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;margin-left:6px;vertical-align:1px}
.spw-approve{border:1px solid var(--status-warn-edge,#E8C9A0);background:var(--status-warn-soft,#FFF4E5);border-radius:12px;padding:12px 14px;margin-bottom:12px;display:grid;gap:10px}
.spw-approve[hidden]{display:none}
.spw-approve-h{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}
.spw-approve-h b{font-size:14px}
.spw-approve-h span{font-size:12px;color:var(--ink-2,#4A4F59)}
.spw-req{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px 14px;align-items:center;background:#fff;border:1px solid var(--line,#eceef3);border-radius:10px;padding:10px 12px}
.spw-req .who{font-weight:700;font-size:13px}
.spw-req .meta{color:var(--muted,#8c92a0);font-size:13px;font-weight:500}
.spw-req q{display:block;font-size:13px;margin-top:2px}
.spw-req input{font:inherit;font-size:13px;width:100%;margin-top:6px;border:1px solid var(--line,#eceef3);border-radius:8px;padding:7px 9px}
.spw-req .acts{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.spw-req .err{grid-column:1/-1;color:var(--status-behind-deep,#A8382B);font-size:13px}
.spw-req .err:empty{display:none}
@media (max-width:640px){.spw-req{grid-template-columns:1fr}.spw-req .acts{justify-content:stretch}.spw-req .acts .spw-btn{flex:1}}
.spw-btn{font:inherit;font-size:13px;font-weight:600;white-space:nowrap;border-radius:8px;padding:7px 14px;min-height:36px;cursor:pointer;border:1px solid var(--line,#eceef3);background:#fff;color:var(--ink,#222633)}
.spw-btn.primary{background:var(--accent,#4364dc);border-color:var(--accent,#4364dc);color:#fff}
.spw-btn[disabled]{opacity:.55;cursor:default}
.spw-scrim{position:fixed;inset:0;z-index:10060;background:rgba(20,22,28,.38);display:flex;align-items:center;justify-content:center;padding:16px}
.spw-dialog{background:#fff;border-radius:16px;max-width:420px;width:100%;padding:18px;display:grid;gap:12px;box-shadow:0 18px 44px rgba(20,22,28,.25);color:var(--ink,#222633)}
.spw-dialog h3{margin:0;font-size:17px}
.spw-dialog p{margin:0;font-size:14px;color:var(--ink-2,#4A4F59)}
.spw-dialog textarea{font:inherit;font-size:14px;min-height:76px;border:1px solid var(--line,#eceef3);border-radius:10px;padding:9px 10px;resize:vertical}
.spw-dialog .acts{display:flex;gap:8px;justify-content:flex-end}
@media (max-width:520px){.spw-scrim{align-items:flex-end;padding:0}.spw-dialog{border-radius:18px 18px 0 0;padding-bottom:max(18px,env(safe-area-inset-bottom,0px))}}
`;
    document.head.appendChild(st);
  }

  // ------------------------------------------------------------------ cell badge
  function cellBadge(day) {
    const ot = day && day.overtime;
    if (!ot) return null;
    const logged = hrs(day.hours_logged), extra = hrs(ot.extra);
    const tip = ot.reason ? ` title="${esc(ot.reason)}"` : '';
    if (ot.status === 'approved') return `<span class="spw-ot approved"${tip}>${logged} ✓</span>`;
    if (ot.status === 'rejected') return `<span class="spw-ot rejected"${tip}>${LIMIT} <s>+${extra}</s></span>`;
    if (ot.status === 'missing') return `<span class="spw-ot missing" title="Over 8 hours with no reason yet">${LIMIT} + ${extra} needs reason</span>`;
    return `<span class="spw-ot pending"${tip}>${LIMIT} + ${extra} pending</span>`;
  }

  // ------------------------------------------------------------------ approvals
  function renderApprovals(container, grid, opts) {
    injectStyles();
    opts = opts || {};
    if (!container) return;
    const items = [];
    (grid.workers || []).forEach((w) => (w.days || []).forEach((d) => {
      if (d.overtime && (d.overtime.status === 'pending' || d.overtime.status === 'missing')) items.push({ w, d });
    }));
    container.classList.add('spw-approve');
    if (!items.length) { container.hidden = true; container.innerHTML = ''; return; }
    container.hidden = false;
    container.innerHTML = `<div class="spw-approve-h"><b>Needs approval (${items.length})</b>
      <span>Hours over 8 aren't paid until an admin or PM approves them.</span></div>`
      + items.map(({ w, d }, i) => {
        const ot = d.overtime;
        const reasonHtml = ot.reason
          ? `<q>${esc(ot.reason)}</q>`
          : `<label class="meta" style="display:block">No reason given yet. Add one to approve.<input type="text" maxlength="500" data-spw-reason="${i}" placeholder="Why the extra hours?"></label>`;
        return `<div class="spw-req" data-spw-i="${i}">
          <div>
            <div class="who">${esc(w.worker_id || '')}${w.name ? ' · ' + esc(w.name) : ''} · ${esc(fmtDay(d.date))} · ${hrs(d.hours_logged)} hrs <span class="meta">(${LIMIT} + ${hrs(ot.extra)} extra)</span></div>
            <div class="meta">${esc(d.project_code || '')}${(d.row_count || 1) > 1 ? ' and ' + ((d.row_count || 1) - 1) + ' more job(s)' : ''}</div>
            ${reasonHtml}
          </div>
          <div class="acts">
            <button type="button" class="spw-btn" data-spw-act="reject" data-spw-i="${i}">Reject</button>
            <button type="button" class="spw-btn primary" data-spw-act="approve" data-spw-i="${i}">Approve ${hrs(ot.extra)} hrs</button>
          </div>
          <div class="err" data-spw-err="${i}" role="alert"></div>
        </div>`;
      }).join('');
    container.onclick = async (e) => {
      const btn = e.target.closest('[data-spw-act]');
      if (!btn) return;
      const i = +btn.dataset.spwI, it = items[i];
      const err = container.querySelector(`[data-spw-err="${i}"]`);
      const input = container.querySelector(`[data-spw-reason="${i}"]`);
      const reason = input ? input.value.trim() : '';
      if (input && !reason) { err.textContent = 'Add a reason first.'; input.focus(); return; }
      container.querySelectorAll(`[data-spw-i="${i}"] .spw-btn`).forEach((b) => { b.disabled = true; });
      err.textContent = '';
      try {
        const r = await fetch('/api/payroll/overtime/decide', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
          body: JSON.stringify({ employee_id: it.w.employee_id, date: it.d.date, decision: btn.dataset.spwAct, reason: reason || undefined }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) {
          err.textContent = r.status === 403 ? 'Only admins and PMs can approve extra hours.' : (j.error || ('Could not save (' + r.status + ')'));
          container.querySelectorAll(`[data-spw-i="${i}"] .spw-btn`).forEach((b) => { b.disabled = false; });
          return;
        }
        if (opts.onChange) opts.onChange();
      } catch (_) {
        err.textContent = 'No connection. Try again.';
        container.querySelectorAll(`[data-spw-i="${i}"] .spw-btn`).forEach((b) => { b.disabled = false; });
      }
    };
  }

  // ------------------------------------------------------------------ reason dialog
  function reasonDialog(opts) {
    injectStyles();
    opts = opts || {};
    return new Promise((resolve) => {
      const scrim = document.createElement('div');
      scrim.className = 'spw-scrim';
      scrim.innerHTML = `<div class="spw-dialog" role="dialog" aria-modal="true" aria-labelledby="spw-rd-t">
        <h3 id="spw-rd-t">${esc(opts.title || 'Over 8 hours')}</h3>
        <p>${esc(opts.message || 'Hours over 8 need a reason and an admin or PM’s approval. Until then payroll pays 8.')}</p>
        <textarea id="spw-rd-reason" maxlength="500" placeholder="e.g. Concrete pour ran late, inspector on site"></textarea>
        <div class="acts"><button type="button" class="spw-btn" data-x>Cancel</button>
        <button type="button" class="spw-btn primary" data-ok>Save and send for approval</button></div></div>`;
      document.body.appendChild(scrim);
      const ta = scrim.querySelector('textarea');
      const done = (v) => { scrim.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
      const onKey = (e) => { if (e.key === 'Escape') done(null); };
      document.addEventListener('keydown', onKey);
      scrim.addEventListener('click', (e) => { if (e.target === scrim) done(null); });
      scrim.querySelector('[data-x]').onclick = () => done(null);
      scrim.querySelector('[data-ok]').onclick = () => {
        const v = ta.value.trim();
        if (!v) { ta.focus(); ta.style.borderColor = 'var(--status-behind,#e9685a)'; return; }
        done(v);
      };
      setTimeout(() => ta.focus(), 0);
    });
  }

  // ------------------------------------------------------------------ week picker
  function attachPicker(button, opts) {
    injectStyles();
    if (!button || button.dataset.spwPicker) return;
    button.dataset.spwPicker = '1';
    button.classList.add('spw-trigger');
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('title', 'Pick a week');

    const pop = document.createElement('div');
    pop.className = 'spw-pop';
    pop.hidden = true;
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-label', 'Pick a payroll week');
    document.body.appendChild(pop);
    let month = null;          // first day of the shown month
    const cache = {};

    function place() {
      if (window.innerWidth <= 520) { pop.style.left = ''; pop.style.top = ''; return; }
      const r = button.getBoundingClientRect();
      const w = pop.offsetWidth || 340;
      let left = Math.min(Math.max(12, r.right - w), window.innerWidth - w - 12);
      let top = r.bottom + 8;
      if (top + pop.offsetHeight > window.innerHeight - 12) top = Math.max(12, r.top - pop.offsetHeight - 8);
      pop.style.left = left + 'px';
      pop.style.top = top + 'px';
    }

    async function summary(from, to) {
      const key = from + '|' + to;
      if (cache[key] !== undefined) return cache[key];
      try {
        const r = await fetch(`/api/payroll/weeks?from=${from}&to=${to}`, { credentials: 'include' });
        cache[key] = r.ok ? ((await r.json()).data || {}) : null;
      } catch (_) { cache[key] = null; }
      return cache[key];
    }

    function render(sum) {
      const cur = opts.getCurrent && opts.getCurrent();
      const todayIso = iso(new Date());
      const first = mondayOf(month);
      const lastOfMonth = new Date(month.getFullYear(), month.getMonth() + 1, 0);
      const rows = [];
      for (let m = first; m <= lastOfMonth; m = addDays(m, 7)) rows.push(new Date(m));
      const title = month.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
      pop.innerHTML = `
        <div class="spw-head"><button type="button" data-nav="-1" aria-label="Previous month">‹</button><b>${esc(title)}</b><button type="button" data-nav="1" aria-label="Next month">›</button></div>
        <div class="spw-quick"><button type="button" data-q="last">Last week</button><button type="button" data-q="this">This week</button></div>
        <div class="spw-dow">${['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((d) => `<span>${d}</span>`).join('')}<span></span></div>
        ${rows.map((m) => {
          const mi = iso(m), s = sum && sum[mi];
          const days = [0, 1, 2, 3, 4, 5, 6].map((k) => {
            const d = addDays(m, k), out = d.getMonth() !== month.getMonth();
            return `<span class="${out ? 'spw-out' : ''}${iso(d) === todayIso ? ' spw-today' : ''}">${d.getDate()}</span>`;
          }).join('');
          const h = s ? `${s.pending ? '<i class="spw-dot" title="Needs approval"></i>' : ''}${hrs(s.hours)}h` : '';
          const lbl = `Week of ${m.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}${s ? ', ' + hrs(s.hours) + ' hours' + (s.pending ? ', ' + s.pending + ' day(s) need approval' : '') : ''}`;
          return `<button type="button" class="spw-week${mi === cur ? ' sel' : ''}" data-week="${mi}" aria-label="${esc(lbl)}"${mi === cur ? ' aria-current="true"' : ''}>${days}<span class="spw-h">${h}</span></button>`;
        }).join('')}
        ${sum ? '<div class="spw-legend"><span>Hours = paid hours that week</span><span><i class="spw-dot"></i> needs approval</span></div>' : ''}`;
      place();
    }

    async function show() {
      const cur = opts.getCurrent && opts.getCurrent();
      const base = cur ? parse(cur) : new Date();
      if (!month) month = new Date(base.getFullYear(), base.getMonth(), 1);
      pop.hidden = false;
      button.setAttribute('aria-expanded', 'true');
      render(null);
      const first = mondayOf(month), last = addDays(mondayOf(new Date(month.getFullYear(), month.getMonth() + 1, 0)), 6);
      const shown = month;
      const sum = await summary(iso(first), iso(last));
      if (!pop.hidden && shown === month) render(sum);
      const sel = pop.querySelector('.spw-week.sel') || pop.querySelector('.spw-week');
      if (sel && document.activeElement === button) sel.focus();
    }
    function hide() { pop.hidden = true; button.setAttribute('aria-expanded', 'false'); month = null; }

    button.addEventListener('click', (e) => { e.stopPropagation(); pop.hidden ? show() : hide(); });
    pop.addEventListener('click', (e) => {
      e.stopPropagation();
      const nav = e.target.closest('[data-nav]');
      if (nav) { month = new Date(month.getFullYear(), month.getMonth() + (+nav.dataset.nav), 1); show(); return; }
      const q = e.target.closest('[data-q]');
      const wk = e.target.closest('[data-week]');
      let pick = null;
      if (q) { const thisMon = mondayOf(new Date()); pick = iso(q.dataset.q === 'this' ? thisMon : addDays(thisMon, -7)); }
      if (wk) pick = wk.dataset.week;
      if (pick) { hide(); opts.onPick && opts.onPick(pick); button.focus(); }
    });
    document.addEventListener('click', () => { if (!pop.hidden) hide(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !pop.hidden) { hide(); button.focus(); } });
    window.addEventListener('resize', () => { if (!pop.hidden) place(); });
    window.addEventListener('scroll', () => { if (!pop.hidden) place(); }, true);
  }

  window.SSCPayrollWeek = { attachPicker, renderApprovals, cellBadge, reasonDialog, injectStyles };
})();
