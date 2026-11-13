/* zamp AP workspace — single-page app on the Flask JSON API. No dependencies. */
(() => {
  'use strict';
  const APP = window.APP || {};
  const view = document.getElementById('view');
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  /* ---------- formatting ---------- */
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const dash = (v) => (v === null || v === undefined || v === '' ? '—' : esc(v));
  const inr = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 });
  const inrCompact = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', notation: 'compact', maximumFractionDigits: 1 });
  const money = (v) => (v === null || v === undefined || v === '' || isNaN(Number(v)) ? '—' : inr.format(Number(v)));
  const moneyShort = (v) => (v === null || v === undefined || isNaN(Number(v)) ? '—' : Math.abs(v) < 100000 ? inr.format(Math.round(Number(v))).replace(/\.00$/, '') : inrCompact.format(Number(v)));
  const pct = (v) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(v === 0 || v === 1 ? 0 : 1)}%`);
  const plural = (n, word, many) => `${n} ${n === 1 ? word : many || word + 's'}`;
  function duration(ms) {
    const m = Math.round(Math.abs(ms) / 60000);
    if (m < 60) return `${Math.max(m, 1)}m`;
    const h = Math.round(m / 60);
    return h < 48 ? `${h}h` : `${Math.round(h / 24)}d`;
  }
  function ago(iso) {
    if (!iso) return '—';
    const ms = Date.now() - new Date(iso).getTime();
    if (ms < 45000) return 'just now';
    return `${duration(ms)} ago`;
  }
  const seconds = (s) => (s === null || s === undefined ? '—' : s < 1 ? `${Math.round(s * 1000)} ms` : `${s.toFixed(s < 10 ? 2 : 1)} s`);
  const dateLabel = (iso) => (iso ? new Date(iso).toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—');

  /* ---------- API ---------- */
  async function api(path, { method = 'GET', json, body, raw } = {}) {
    const opts = { method, headers: {} };
    if (method !== 'GET') opts.headers['X-CSRF-Token'] = APP.csrf;
    if (json !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(json); }
    else if (body) opts.body = body;
    const res = await fetch(path, opts);
    if (res.redirected && new URL(res.url).pathname === '/login') { location.href = '/login'; throw new Error('Signed out'); }
    if (raw) return res;
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { const err = new Error(data.error || `Request failed (${res.status})`); err.status = res.status; err.data = data; throw err; }
    return data;
  }
  let metaPromise;
  const meta = () => (metaPromise ||= api('/api/meta').catch(() => ({ checks: {}, stages: [], teams: [], benchmark: {} })));

  /* ---------- toasts ---------- */
  function toast(message, { tone = '', action } = {}) {
    const el = document.createElement('div');
    el.className = `toast ${tone}`;
    el.innerHTML = `<span>${esc(message)}</span>${action ? `<a href="${esc(action.href)}">${esc(action.label)}</a>` : ''}`;
    $('#toasts').append(el);
    setTimeout(() => el.remove(), action ? 7000 : 4000);
  }

  /* ---------- reviewer identity & theme ---------- */
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} },
  };
  const reviewerName = () => store.get('zamp-reviewer') || '';
  function paintName() { $('#whoaminame').textContent = reviewerName() || 'Set your name'; }
  function askName() {
    const dialog = $('#namedialog');
    $('#nameinput').value = reviewerName();
    dialog.showModal();
    return new Promise((resolve) => {
      dialog.addEventListener('close', () => {
        if (dialog.returnValue === 'save' && $('#nameinput').value.trim()) { store.set('zamp-reviewer', $('#nameinput').value.trim()); paintName(); }
        resolve(reviewerName());
      }, { once: true });
    });
  }
  const ensureName = async () => reviewerName() || (await askName());
  $('#whoami').addEventListener('click', askName);
  $('#themetoggle').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme === 'dark';
    document.documentElement.dataset.theme = dark ? 'light' : 'dark';
    store.set('zamp-theme', document.documentElement.dataset.theme);
  });
  $('#menubtn').addEventListener('click', () => {
    const open = $('.nav').classList.toggle('open');
    $('#menubtn').setAttribute('aria-expanded', String(open));
  });
  paintName();

  /* ---------- outcome vocabulary ---------- */
  const NON_INVOICE = (r) => (r.document_routing?.status || 'INVOICE_AP_WORKFLOW') === 'NON_INVOICE';
  function outcome(r) {
    if (r.status === 'RUNNING') return { label: 'Processing', tone: 'info' };
    if (r.status === 'FAILED') return { label: 'Failed', tone: 'bad' };
    if (NON_INVOICE(r)) return { label: 'Filed', tone: '' };
    if (r.resolution === 'APPROVED') return { label: 'Approved by reviewer', tone: 'ok' };
    if (r.resolution === 'REJECTED') return { label: 'Rejected by reviewer', tone: 'bad' };
    if (r.status === 'APPROVED') return { label: 'Approved', tone: 'ok' };
    if (r.status === 'REJECTED') return { label: 'Rejected', tone: 'bad' };
    return { label: 'Needs review', tone: 'warn' };
  }
  // Backend reasons name fields by key ("Missing: po_number"); show words.
  const FIELD_WORDS = { po_number: 'PO number', invoice_number: 'invoice number', date: 'invoice date', total: 'total', subtotal: 'subtotal', tax: 'tax', vendor: 'vendor', gstin: 'GSTIN' };
  const human = (text) => String(text ?? '').replace(/\b(po_number|invoice_number|subtotal|gstin)\b|(?<=(?:Missing|missing):[^.;]*)\b(date|total|tax|vendor)\b/g, (m) => FIELD_WORDS[m] || m);
  const pill = (r) => { const o = outcome(r); return `<span class="pill ${o.tone}">${esc(o.label)}</span>`; };
  function route(r) {
    if (r.review_route) return r.review_route;
    return r.route_team ? { team: r.route_team, sla_hours: teamHours[r.route_team] || 24 } : null;
  }
  const isOpen = (r) => r.status === 'PENDING_REVIEW' && !r.resolution && !!route(r);
  function nextStep(r) {
    const o = outcome(r).label;
    if (r.status === 'RUNNING') return 'Reading the document';
    if (r.status === 'FAILED') return 'Upload again';
    if (o === 'Filed') return 'Filed for reference';
    if (o.startsWith('Approved')) return 'Scheduled for payment';
    if (o.startsWith('Rejected')) return 'Tell the vendor';
    return route(r)?.team || 'AP Specialist';
  }
  const teamHours = {};
  function sla(r) {
    const rt = route(r);
    if (!rt || !isOpen(r)) return null;
    const window = rt.sla_hours * 3.6e6;
    const left = new Date(r.time).getTime() + window - Date.now();
    if (left < 0) return { text: `Overdue ${duration(left)}`, cls: 'late', left };
    return { text: `${duration(left)} left`, cls: left < window * 0.25 ? 'soon' : '', left };
  }
  const slaChip = (r) => { const s = sla(r); return s ? `<span class="sla ${s.cls}">${esc(s.text)}</span>` : ''; };
  const title = (r) => r.vendor || r.source || 'Document';

  /* ---------- review count in nav ---------- */
  async function refreshCount() {
    try {
      const res = await api('/api/runs?open=1&limit=1', { raw: true });
      const n = Number(res.headers.get('X-Total-Count') || 0);
      const el = $('#reviewcount'); el.textContent = n; el.hidden = n === 0;
    } catch (_) {}
  }

  /* ---------- router ---------- */
  let cleanups = [];
  let renderToken = 0;
  const onLeave = (fn) => cleanups.push(fn);
  const routes = [
    [/^$/, overviewPage, 'overview'], [/^upload$/, uploadPage, 'upload'], [/^review$/, reviewPage, 'review'],
    [/^invoices$/, invoicesPage, 'invoices'], [/^invoice\/([\w-]+)$/, invoicePage, ''], [/^vendors$/, vendorsPage, 'vendors'],
    [/^data$/, dataPage, 'data'], [/^how$/, howPage, 'how'],
  ];
  function parseHash() {
    const raw = location.hash.replace(/^#\/?/, '');
    const [path, qs = ''] = raw.split('?');
    return { path: path.replace(/\/$/, ''), params: new URLSearchParams(qs) };
  }
  async function render() {
    cleanups.forEach((fn) => { try { fn(); } catch (_) {} });
    cleanups = [];
    const token = ++renderToken;
    const alive = () => token === renderToken;
    const { path, params } = parseHash();
    const match = routes.find(([re]) => re.test(path)) || routes[0];
    const args = path.match(match[0]) || [];
    $$('.nav a').forEach((a) => a.classList.toggle('active', a.dataset.nav === (match[2] || (path.startsWith('invoice/') ? params.get('from') || 'invoices' : ''))));
    $('.nav').classList.remove('open'); $('#menubtn').setAttribute('aria-expanded', 'false');
    view.innerHTML = '<div class="stack"><div class="skeleton" style="height:42px;width:40%"></div><div class="skeleton" style="height:180px"></div><div class="skeleton" style="height:260px"></div></div>';
    try {
      await match[1](args.slice(1), params, alive);
      if (alive()) { view.classList.remove('fade-in'); void view.offsetWidth; view.classList.add('fade-in'); }
    } catch (err) {
      if (!alive()) return;
      view.innerHTML = `<div class="card empty"><h3>Something went wrong</h3><p>${esc(err.message)}</p><a class="btn" href="#/">Back to overview</a></div>`;
    }
  }
  window.addEventListener('hashchange', () => { render(); window.scrollTo({ top: 0 }); });
  const go = (hash) => { if (location.hash === hash) render(); else location.hash = hash; };

  /* ---------- shared fragments ---------- */
  const pageHead = (eyebrow, heading, text, actions = '') =>
    `<div class="page-head"><div><span class="eyebrow">${esc(eyebrow)}</span><h1>${heading}</h1>${text ? `<p>${text}</p>` : ''}</div>${actions ? `<div class="actions">${actions}</div>` : ''}</div>`;
  const tip = (text) => `<span class="tip" title="${esc(text)}" aria-label="${esc(text)}">?</span>`;
  const bar = (label, value, max, tone = '', right = '') =>
    `<div class="bar-row"><div class="bar-label"><span>${label}</span><span>${right}</span></div><div class="track"><i class="${tone}" style="width:${max ? Math.max(2, (value / max) * 100) : 0}%"></i></div></div>`;
  function invoiceRows(rows, from) {
    return rows.map((r) => `<tr class="clickable" data-href="#/invoice/${esc(r.run_id)}?from=${from}" tabindex="0">
      <td><div class="cell-main">${esc(title(r))}</div><div class="cell-sub">${dash(r.invoice_number)}${r.po_number ? ` · ${esc(r.po_number)}` : ''}</div></td>
      <td class="right num">${money(r.total)}</td><td>${pill(r)}</td>
      <td><span class="muted">${esc(nextStep(r))}</span> ${slaChip(r)}</td>
      <td class="right muted" title="${esc(dateLabel(r.time))}">${esc(ago(r.time))}</td></tr>`).join('');
  }
  const invoiceTable = (rows, from) => `<div class="table-wrap"><table><thead><tr><th>Vendor</th><th class="right">Amount</th><th>Result</th><th>Next step</th><th class="right">Received</th></tr></thead><tbody>${invoiceRows(rows, from)}</tbody></table></div>`;
  function wireRows(root) {
    $$('tr[data-href]', root).forEach((tr) => {
      tr.addEventListener('click', () => go(tr.dataset.href));
      tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(tr.dataset.href); });
    });
  }

  /* ================= Overview ================= */
  async function overviewPage(_args, _params, alive) {
    const [s, recent, queueRes, m] = await Promise.all([api('/api/analytics/summary'), api('/api/runs?limit=8'), api('/api/runs?open=1&limit=200'), meta()]);
    if (!alive()) return;
    m.teams.forEach((t) => (teamHours[t.team] = t.sla_hours));
    const im = s.invoice_metrics;
    const queue = queueRes.slice().sort((a, b) => (sla(a)?.left ?? 0) - (sla(b)?.left ?? 0));
    const overdue = s.queue.reduce((n, q) => n + q.overdue, 0);
    const handled = im.invoices;
    const autoShare = s.touchless_rate;
    const headline = handled
      ? `${plural(handled, 'invoice')} handled. <span class="dim">${pct(autoShare)} needed no one.</span>`
      : `Your AP inbox is ready. <span class="dim">Drop in the first invoice.</span>`;
    const waiting = queue.length;
    const timeSaved = s.time_saved_minutes >= 60 ? `~${(s.time_saved_minutes / 60).toFixed(1)} h` : `~${Math.round(s.time_saved_minutes)} min`;

    // Last 14 days of volume, filling empty days so the axis is honest.
    const daily = new Map(s.daily.map((d) => [d.date, d]));
    const days = [...Array(14)].map((_, i) => { const d = new Date(); d.setUTCDate(d.getUTCDate() - (13 - i)); const key = d.toISOString().slice(0, 10); return { date: key, ...(daily.get(key) || { APPROVED: 0, PENDING_REVIEW: 0, REJECTED: 0 }) }; });
    const dayMax = Math.max(1, ...days.map((d) => d.APPROVED + d.PENDING_REVIEW + d.REJECTED));
    const columns = days.map((d) => {
      const h = (v) => `${(v / dayMax) * 100}%`;
      const label = new Date(d.date + 'T00:00:00Z').toLocaleDateString('en-IN', { day: 'numeric', month: 'short', timeZone: 'UTC' });
      return `<div class="column" data-tip="${esc(`${label}\nApproved ${d.APPROVED}\nNeeds review ${d.PENDING_REVIEW}\nRejected ${d.REJECTED}`)}"><i class="ok" style="height:${h(d.APPROVED)}"></i><i class="warn" style="height:${h(d.PENDING_REVIEW)}"></i><i class="bad" style="height:${h(d.REJECTED)}"></i></div>`;
    }).join('');
    const bs = (k) => s.by_status[k] || { count: 0, value: 0 };
    const outcomeMax = Math.max(1, bs('APPROVED').count, bs('PENDING_REVIEW').count, bs('REJECTED').count);
    const stopMax = Math.max(1, ...s.stop_reasons.map((r) => r.review + r.rejected));
    const stepMax = Math.max(0.001, ...s.step_seconds.map((x) => x.seconds));
    const vendorMax = Math.max(1, ...s.top_vendors.map((v) => v.approved_value));

    view.innerHTML = `
      <section class="hero">
        <span class="eyebrow">Accounts payable · Overview</span>
        <h1>${headline}</h1>
        <p>Every invoice is read, checked against your purchase orders, vendors and past payments, then approved, sent to the right person, or rejected — with the reason written down.</p>
        <div class="row">
          ${waiting ? `<a class="status-chip warn" href="#/review"><b>${waiting}</b>${waiting === 1 ? 'invoice is' : 'invoices are'} waiting for review →</a>` : `<span class="status-chip ok"><b>0</b>Nothing waiting for review</span>`}
          <a class="btn btn-primary btn-lg" href="#/upload">Upload an invoice</a>
          ${handled ? '' : '<a class="btn btn-lg" href="#/upload">Try the sample inbox</a>'}
        </div>
      </section>
      <div class="kpis">
        <div class="kpi"><div class="kpi-label">Invoices handled ${tip('Tax invoices processed, excluding filed non-invoices')}</div><div class="kpi-value">${handled}</div><div class="kpi-sub">${moneyShort(im.value)} in total</div></div>
        <div class="kpi ok"><div class="kpi-label">Done automatically ${tip('Approved without any person touching them')}</div><div class="kpi-value">${pct(autoShare)}</div><div class="kpi-sub">${plural(im.touchless, 'invoice')} with no one</div></div>
        <div class="kpi ${overdue ? 'warn' : ''}"><div class="kpi-label">Waiting for a person ${tip('Open review items across all teams')}</div><div class="kpi-value">${waiting}</div><div class="kpi-sub">${overdue ? `${overdue} past deadline` : 'all within deadline'}</div></div>
        <div class="kpi"><div class="kpi-label">Stopped ${tip('Rejected automatically or by a reviewer')}</div><div class="kpi-value">${im.rejected}</div><div class="kpi-sub">repeats, over-billing, blocked vendors</div></div>
        <div class="kpi"><div class="kpi-label">Money stopped ${tip('Value of invoices rejected before payment')}</div><div class="kpi-value">${moneyShort(s.money_stopped)}</div><div class="kpi-sub">kept from being paid</div></div>
        <div class="kpi"><div class="kpi-label">Time saved ${tip(`Assumes ${s.manual_minutes_per_invoice} minutes of manual handling per invoice; set MANUAL_MINUTES_PER_INVOICE`)}</div><div class="kpi-value">${s.time_saved_minutes ? timeSaved : '—'}</div><div class="kpi-sub">median ${seconds(s.processing_seconds.median)} per document</div></div>
      </div>
      <div class="grid grid-main">
        <section class="card"><div class="card-head"><div><h2>Invoices over time</h2><p>Last 14 days — what came in and what happened to it</p></div></div>
          <div class="legend"><span><i class="dot-ok"></i>Approved</span><span><i class="dot-warn"></i>Needs review</span><span><i class="dot-bad"></i>Rejected</span></div>
          <div class="columns" role="img" aria-label="Invoices per day for the last 14 days">${columns}</div>
          <div class="axis"><span>${esc(new Date(days[0].date).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }))}</span><span>Today</span></div></section>
        <section class="card"><div class="card-head"><div><h2>What happened</h2><p>Final outcome and the money behind it</p></div></div>
          <div class="bars">
            ${bar('Approved', bs('APPROVED').count, outcomeMax, 'ok', `${bs('APPROVED').count} · ${moneyShort(bs('APPROVED').value)}`)}
            ${bar('Needs review', bs('PENDING_REVIEW').count, outcomeMax, 'warn', `${bs('PENDING_REVIEW').count} · ${moneyShort(bs('PENDING_REVIEW').value)}`)}
            ${bar('Rejected', bs('REJECTED').count, outcomeMax, 'bad', `${bs('REJECTED').count} · ${moneyShort(bs('REJECTED').value)}`)}
          </div>
          <p class="muted" style="font-size:13px;margin-top:16px">${im.approved_by_reviewer + im.rejected_by_reviewer ? `${plural(im.approved_by_reviewer, 'approval')} and ${plural(im.rejected_by_reviewer, 'rejection')} came from reviewers.` : 'No reviewer decisions yet.'} ${im.non_invoices ? `${plural(im.non_invoices, 'non-invoice document')} filed separately.` : ''}</p></section>
      </div>
      <div class="grid grid-2 section">
        <section class="card"><div class="card-head"><div><h2>Why invoices were stopped</h2><p>The checks that sent invoices to a person or rejected them</p></div></div>
          <div class="legend"><span><i class="dot-warn"></i>Sent to a person</span><span><i class="dot-bad"></i>Rejected</span></div>
          <div class="bars">${s.stop_reasons.length ? s.stop_reasons.map((r) => `<div class="bar-row"><div class="bar-label"><span>${esc(r.label)}</span><span>${r.review + r.rejected}×</span></div><div class="track stack"><i class="warn" style="width:${(r.review / stopMax) * 100}%"></i><i class="bad" style="width:${(r.rejected / stopMax) * 100}%"></i></div></div>`).join('') : '<p class="muted">Nothing has been stopped yet.</p>'}</div></section>
        <section class="card"><div class="card-head"><div><h2>Review queue by team</h2><p>Each exception goes to the team that can fix it</p></div><a class="btn btn-sm" href="#/review">Open queue</a></div>
          <div class="table-wrap"><table><thead><tr><th>Team</th><th class="right">Waiting</th><th class="right">Past deadline</th><th class="right">Deadline</th></tr></thead><tbody>
          ${s.queue.map((q) => `<tr class="clickable" data-href="#/review?team=${encodeURIComponent(q.team)}"><td>${esc(q.team)}</td><td class="right num">${q.open}</td><td class="right num">${q.overdue ? `<span class="sla late">${q.overdue}</span>` : '0'}</td><td class="right muted">${q.sla_hours} h</td></tr>`).join('')}
          </tbody></table></div></section>
      </div>
      <div class="grid grid-3 section">
        <section class="card"><div class="card-head"><div><h2>Where the time goes</h2><p>Average time per step, latest 200 documents</p></div></div>
          <div class="bars">${s.step_seconds.length ? s.step_seconds.map((x) => bar(esc(x.stage), x.seconds, stepMax, x.seconds === stepMax ? 'info' : '', seconds(x.seconds))).join('') : '<p class="muted">No timings yet.</p>'}</div></section>
        <section class="card"><div class="card-head"><div><h2>Top vendors</h2><p>Approved spend by vendor</p></div><a class="btn btn-sm btn-ghost" href="#/vendors">All</a></div>
          <div class="bars">${s.top_vendors.length ? s.top_vendors.slice(0, 6).map((v) => bar(esc(v.vendor), v.approved_value, vendorMax, 'ok', moneyShort(v.approved_value))).join('') : '<p class="muted">No vendor activity yet.</p>'}</div></section>
        <section class="card"><div class="card-head"><div><h2>Payables due</h2><p>Approved invoices by due date</p></div></div>
          <div class="bars">${s.aging.map((a) => bar(esc(a.bucket), a.count, Math.max(1, ...s.aging.map((x) => x.count)), a.bucket === 'Overdue' && a.count ? 'bad' : '', `${a.count} · ${moneyShort(a.value)}`)).join('')}</div>
          ${s.reviewers.length ? `<h3 style="font-size:14px;margin:22px 0 10px">Who's reviewing</h3><div class="bars">${s.reviewers.map((r) => `<div class="bar-label"><span>${esc(r.reviewer)}</span><span class="muted">${r.approved} approved · ${r.rejected} rejected · ${r.median_seconds !== null ? duration(r.median_seconds * 1000) : '—'} to decide</span></div>`).join('')}</div>` : ''}</section>
      </div>
      <div class="grid grid-main section">
        <section class="card card-flush"><div class="card-head"><div><h2>Recent invoices</h2></div><a class="btn btn-sm" href="#/invoices">View all</a></div>
          ${recent.length ? invoiceTable(recent, 'overview') : '<div class="empty"><h3>No invoices yet</h3><p>Upload a PDF or photo, or run the sample inbox to see every outcome.</p><a class="btn btn-primary" href="#/upload">Go to upload</a></div>'}</section>
        <section class="card"><div class="card-head"><div><h2>Needs your attention</h2><p>Closest to their deadline first</p></div></div>
          ${queue.length ? `<div class="stack">${queue.slice(0, 5).map((r) => `<a class="row-between" style="text-decoration:none" href="#/invoice/${esc(r.run_id)}?from=review"><span><b style="font-weight:500">${esc(title(r))}</b><br><small class="muted">${esc(human((r.reasons || [])[0] || route(r)?.reason || ''))}</small></span>${slaChip(r)}</a>`).join('')}</div>` : '<div class="empty" style="padding:28px 0"><h3>You\'re all caught up</h3><p>New exceptions will appear here.</p></div>'}</section>
      </div>`;
    wireRows(view);
  }

  /* ================= Upload ================= */
  const batch = []; // { name, run_id, status } — persists while the app is open
  function batchMarkup() {
    if (!batch.length) return '';
    const done = batch.filter((b) => b.run && b.run.status !== 'RUNNING').length;
    return `<section class="card card-flush section" id="batchcard"><div class="card-head"><div><h2>This batch</h2><p>${done} of ${batch.length} finished</p></div><button class="btn btn-sm btn-ghost" id="clearbatch">Clear list</button></div>
      <div class="progress" style="margin:16px 22px 8px"><i style="width:${(done / batch.length) * 100}%"></i></div>
      ${batch.map((b) => `<div class="batch-item"><span><b style="font-weight:500">${esc(b.run?.vendor || b.name)}</b><br><small class="muted">${esc(b.name)}${b.run?.invoice_number ? ` · ${esc(b.run.invoice_number)}` : ''}</small></span>${b.run ? pill(b.run) : '<span class="pill info">Queued</span>'}${b.run_id ? `<a class="btn btn-sm" href="#/invoice/${esc(b.run_id)}?from=upload">Open</a>` : '<span></span>'}</div>`).join('')}</section>`;
  }
  async function trackBatch(alive) {
    while (alive() && batch.some((b) => !b.run || b.run.status === 'RUNNING')) {
      await Promise.all(batch.filter((b) => b.run_id && (!b.run || b.run.status === 'RUNNING')).map(async (b) => { try { b.run = await api(`/api/runs/${b.run_id}`); } catch (_) {} }));
      const card = $('#batchcard');
      if (card && alive()) { card.outerHTML = batchMarkup(); wireBatch(alive); }
      await new Promise((r) => setTimeout(r, 1200));
    }
    refreshCount();
  }
  function wireBatch(alive) {
    const clear = $('#clearbatch');
    if (clear) clear.onclick = () => { batch.splice(0); $('#batchcard')?.remove(); };
    if (!alive) return;
  }
  async function uploadFiles(files, alive) {
    const valid = [...files].filter((f) => /\.(pdf|png|jpe?g)$/i.test(f.name) && f.size <= 8 * 1024 * 1024);
    const skipped = files.length - valid.length;
    if (skipped) toast(`${plural(skipped, 'file')} skipped — use PDF, PNG or JPEG up to 8 MB`, { tone: 'bad' });
    if (!valid.length) return;
    try {
      if (valid.length === 1) {
        const form = new FormData(); form.append('invoice_file', valid[0]);
        const { run_id } = await api('/api/runs', { method: 'POST', body: form });
        go(`#/invoice/${run_id}?from=upload`);
        return;
      }
      if (valid.length > 25) { toast('Up to 25 documents per batch', { tone: 'bad' }); return; }
      const form = new FormData(); valid.forEach((f) => form.append('invoice_files', f));
      const { runs } = await api('/api/runs/bulk', { method: 'POST', body: form });
      runs.forEach((r) => batch.unshift({ name: r.source, run_id: r.run_id }));
      toast(`${plural(runs.length, 'document')} queued`);
      render();
    } catch (err) { toast(err.message, { tone: 'bad' }); }
  }
  async function uploadPage(_args, _params, alive) {
    const demos = await api('/api/demo').catch(() => []);
    if (!alive()) return;
    const samples = APP.samples || [];
    view.innerHTML = `
      ${pageHead('Upload', 'Drop in an invoice', 'A digital PDF, a scan, or a photo. One file opens the live view so you can watch every check; several files run as a batch you can track together.')}
      <label class="dropzone" id="dropzone" tabindex="0">
        <span class="icon"><svg viewBox="0 0 24 24"><path d="M12 16V4M7 9l5-5 5 5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/></svg></span>
        <h2>Drag invoices here, or click to choose</h2>
        <p class="muted">Up to 25 files, 8 MB each</p>
        <div class="formats"><span class="tag">PDF</span><span class="tag">PNG</span><span class="tag">JPG</span></div>
        <input type="file" id="fileinput" accept=".pdf,.png,.jpg,.jpeg" multiple hidden>
      </label>
      ${batchMarkup()}
      <div class="row-between section" style="margin-top:40px"><div><h2 style="font-size:22px">Sample inbox</h2><p class="muted">Fictional documents, each built to show a different outcome. Run them in order — later ones depend on earlier approvals.</p></div>
        ${demos.length ? `<button class="btn" id="runall"><svg viewBox="0 0 24 24"><path d="M7 5v14l11-7z"/></svg>Run all ${demos.length} in order</button>` : ''}</div>
      <div class="grid grid-3 section">${demos.map((d) => `<article class="card sample-card"><div class="row"><span class="tag ${d.tag === 'Scan' ? 'ai' : ''}">${esc(d.tag)}</span><span class="tag">${esc(d.kind.toUpperCase())}</span></div>
          <h3>${esc(d.title)}</h3><span class="vendor">${esc(d.vendor)}</span><p>${esc(d.expect)}</p>
          <div class="row"><button class="btn btn-primary btn-sm" data-demo="${esc(d.file)}"><svg viewBox="0 0 24 24"><path d="M7 5v14l11-7z"/></svg>Run</button><a class="btn btn-sm" href="/api/demo/${encodeURIComponent(d.file)}" target="_blank" rel="noopener">See file</a></div></article>`).join('') || '<p class="muted">No sample documents found in samples/demo.</p>'}</div>
      ${samples.length ? `<section class="card section" style="margin-top:24px"><div class="card-head"><div><h2>Real-world benchmark images</h2><p>Indian GST invoices from a public dataset. Labels are only used to score extraction after the decision is made.</p></div></div>
        <div class="row"><select id="datasetpick" style="max-width:320px">${samples.map(([i, name]) => `<option value="${i}">${esc(name)}</option>`).join('')}</select><button class="btn" id="rundataset">Run image</button></div></section>` : ''}`;
    const input = $('#fileinput'); const zone = $('#dropzone');
    input.addEventListener('change', () => uploadFiles(input.files, alive));
    zone.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
    ['dragenter', 'dragover'].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add('over'); }));
    ['dragleave', 'drop'].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.remove('over'); }));
    zone.addEventListener('drop', (e) => uploadFiles(e.dataTransfer.files, alive));
    $$('[data-demo]').forEach((b) => b.addEventListener('click', async () => {
      b.disabled = true;
      try { const { run_id } = await api(`/api/demo/${encodeURIComponent(b.dataset.demo)}`, { method: 'POST' }); go(`#/invoice/${run_id}?from=upload`); }
      catch (err) { toast(err.message, { tone: 'bad' }); b.disabled = false; }
    }));
    $('#runall')?.addEventListener('click', async (e) => {
      e.currentTarget.disabled = true;
      for (const d of demos) {
        const entry = { name: d.file }; batch.unshift(entry);
        try {
          entry.run_id = (await api(`/api/demo/${encodeURIComponent(d.file)}`, { method: 'POST' })).run_id;
          // Wait for each to finish: later samples depend on earlier approvals.
          for (let i = 0; i < 300; i++) { entry.run = await api(`/api/runs/${entry.run_id}`); if (entry.run.status !== 'RUNNING') break; await new Promise((r) => setTimeout(r, 500)); }
        } catch (err) { toast(err.message, { tone: 'bad' }); }
        if (!alive()) return;
        const card = $('#batchcard'); if (card) card.outerHTML = batchMarkup(); else $('#dropzone').insertAdjacentHTML('afterend', batchMarkup());
        wireBatch(alive);
      }
      refreshCount();
      toast('Sample inbox processed', { action: { href: '#/', label: 'See overview' } });
    });
    $('#rundataset')?.addEventListener('click', async () => {
      try { const { run_id } = await api(`/api/samples/${$('#datasetpick').value}`, { method: 'POST' }); go(`#/invoice/${run_id}?from=upload`); }
      catch (err) { toast(err.message, { tone: 'bad' }); }
    });
    wireBatch(alive);
    if (batch.some((b) => b.run_id && (!b.run || b.run.status === 'RUNNING'))) trackBatch(alive);
  }

  /* ================= Review queue ================= */
  async function reviewPage(_args, params, alive) {
    const [rows, m] = await Promise.all([api('/api/runs?open=1&limit=500'), meta()]);
    if (!alive()) return;
    m.teams.forEach((t) => (teamHours[t.team] = t.sla_hours));
    const team = params.get('team') || '';
    const counts = Object.fromEntries(m.teams.map((t) => [t.team, rows.filter((r) => route(r)?.team === t.team).length]));
    const shown = rows.filter((r) => !team || route(r)?.team === team).sort((a, b) => (sla(a)?.left ?? 0) - (sla(b)?.left ?? 0));
    const overdue = rows.filter((r) => sla(r)?.cls === 'late').length;
    view.innerHTML = `
      ${pageHead('Needs review', rows.length ? `${plural(rows.length, 'invoice')} waiting` : 'Nothing waiting', rows.length ? `Each exception goes to the team that can fix it, with a deadline. Closest deadline first.${overdue ? ` <b style="color:var(--bad)">${overdue} past deadline.</b>` : ''}` : 'Every invoice has either passed its checks or been decided by a reviewer.')}
      <section class="card card-flush">
        <div class="toolbar"><div class="segmented" role="group" aria-label="Filter by team">
          <button data-team="" aria-pressed="${!team}">All <b>${rows.length}</b></button>
          ${m.teams.map((t) => `<button data-team="${esc(t.team)}" aria-pressed="${team === t.team}" title="${esc(t.handles)} · ${t.sla_hours} h deadline">${esc(t.team)} <b>${counts[t.team]}</b></button>`).join('')}
        </div></div>
        ${shown.length ? shown.map((r) => `<a class="queue-item" href="#/invoice/${esc(r.run_id)}?from=review">
            <span><b style="font-weight:500">${esc(title(r))}</b><div class="cell-sub">${dash(r.invoice_number)} · ${dash(r.po_number)}</div></span>
            <span class="queue-reason">${esc(human((r.reasons || [])[0] || route(r)?.reason || 'Needs a person to confirm'))}</span>
            <span class="tag">${esc(route(r)?.team || '')}</span><span class="num">${money(r.total)}</span>${slaChip(r) || '<span></span>'}</a>`).join('')
          : `<div class="empty"><h3>${team ? `Nothing waiting for ${esc(team)}` : 'You\'re all caught up'}</h3><p>New exceptions will show up here as invoices arrive.</p><a class="btn" href="#/upload">Upload invoices</a></div>`}
      </section>
      <div class="grid grid-2 section">${m.teams.map((t) => `<div class="card team-card"><span class="eyebrow">${esc(t.team)}</span><b>${counts[t.team]} waiting</b><p class="muted" style="font-size:13.5px">${esc(t.handles)}. Deadline ${t.sla_hours} hours from arrival.</p></div>`).join('')}</div>`;
    $$('[data-team]').forEach((b) => b.addEventListener('click', () => go(b.dataset.team ? `#/review?team=${encodeURIComponent(b.dataset.team)}` : '#/review')));
  }

  /* ================= Invoices ================= */
  async function invoicesPage(_args, params, alive) {
    await meta().then((m) => m.teams.forEach((t) => (teamHours[t.team] = t.sla_hours)));
    const state = { status: params.get('status') || '', q: params.get('q') || '', date: params.get('date') || '', offset: 0, rows: [] };
    const filters = [['', 'All'], ['APPROVED', 'Approved'], ['PENDING_REVIEW', 'Needs review'], ['REJECTED', 'Rejected'], ['FAILED', 'Failed'], ['RUNNING', 'Processing']];
    view.innerHTML = `
      ${pageHead('Invoices', 'Every invoice, every decision', 'Search by vendor, invoice number, PO, GSTIN or status. Open any row to see the document, the checks and who decided.',
        `<a class="btn" href="/api/runs/export.csv" download><svg viewBox="0 0 24 24"><path d="M12 4v12M7 11l5 5 5-5M4 20h16"/></svg>CSV</a><a class="btn" href="/api/runs/export.xlsx" download><svg viewBox="0 0 24 24"><path d="M12 4v12M7 11l5 5 5-5M4 20h16"/></svg>Excel</a><button class="btn btn-ghost" id="archive" title="Hide finished invoices from these lists. Duplicate protection and PO balances are kept.">Archive finished</button>`)}
      <section class="card card-flush">
        <div class="toolbar">
          <div class="segmented" role="group" aria-label="Filter by result">${filters.map(([v, l]) => `<button data-status="${v}" aria-pressed="${state.status === v}">${l}</button>`).join('')}</div>
          <div class="search-box"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg><input type="search" id="q" placeholder="Search invoices" value="${esc(state.q)}" aria-label="Search invoices"></div>
          <input type="date" id="date" value="${esc(state.date)}" aria-label="Received on">
          <span class="spacer"></span><span class="muted" id="count" style="font-size:13px"></span>
        </div>
        <div id="tablearea"></div>
        <div class="row-end" style="padding:14px 22px" id="morearea"></div>
      </section>`;
    const LIMIT = 50;
    async function load(reset) {
      if (reset) { state.offset = 0; state.rows = []; }
      const qs = new URLSearchParams({ limit: LIMIT, offset: state.offset });
      if (state.status) qs.set('decision', state.status);
      if (state.q) qs.set('q', state.q);
      if (state.date) qs.set('date', state.date);
      const res = await api(`/api/runs?${qs}`, { raw: true });
      const rows = await res.json();
      if (!alive()) return;
      state.rows.push(...rows); state.offset += LIMIT;
      const total = Number(res.headers.get('X-Total-Count') || state.rows.length);
      $('#count').textContent = state.q ? `${plural(state.rows.length, 'match', 'matches')}` : `${state.rows.length} of ${total}`;
      $('#tablearea').innerHTML = state.rows.length ? invoiceTable(state.rows, 'invoices')
        : `<div class="empty"><h3>No invoices match</h3><p>${state.q || state.status || state.date ? 'Try a different search or filter.' : 'Upload an invoice to get started.'}</p>${state.q || state.status || state.date ? '<a class="btn" href="#/invoices">Clear filters</a>' : '<a class="btn btn-primary" href="#/upload">Upload</a>'}</div>`;
      $('#morearea').innerHTML = rows.length === LIMIT ? '<button class="btn" id="more">Load more</button>' : '';
      $('#more')?.addEventListener('click', () => load(false));
      wireRows($('#tablearea'));
    }
    const syncHash = () => {
      const qs = new URLSearchParams(); if (state.status) qs.set('status', state.status); if (state.q) qs.set('q', state.q); if (state.date) qs.set('date', state.date);
      history.replaceState(null, '', `#/invoices${qs.toString() ? '?' + qs : ''}`);
    };
    $$('[data-status]').forEach((b) => b.addEventListener('click', () => {
      state.status = b.dataset.status; $$('[data-status]').forEach((x) => x.setAttribute('aria-pressed', String(x === b))); syncHash(); load(true);
    }));
    let timer; $('#q').addEventListener('input', (e) => { clearTimeout(timer); timer = setTimeout(() => { state.q = e.target.value.trim(); syncHash(); load(true); }, 250); });
    $('#date').addEventListener('change', (e) => { state.date = e.target.value; syncHash(); load(true); });
    $('#archive').addEventListener('click', async () => {
      if (!confirm('Archive finished invoices? They disappear from these lists, but duplicate protection and PO balances still count them.')) return;
      try { const r = await api('/api/history/clear', { method: 'POST' }); toast(`${plural(r.archived, 'invoice')} archived`); load(true); }
      catch (err) { toast(err.message, { tone: 'bad' }); }
    });
    await load(true);
  }

  /* ================= Invoice detail ================= */
  const RELIABILITY = { DIRECT_LABEL: ['Beside its label', ''], AI_CORROBORATED: ['AI, quoted from page', 'ai'], CONTEXTUAL: ['Inferred', ''], MANUALLY_VERIFIED: ['Corrected by reviewer', ''], NONE: ['No evidence', ''] };
  const FIELDS = [['vendor', 'Vendor'], ['invoice_number', 'Invoice number'], ['date', 'Invoice date'], ['po_number', 'PO number'], ['gstin', 'Seller GSTIN'], ['subtotal', 'Subtotal'], ['tax', 'Tax'], ['total', 'Total']];
  const MONEY_FIELDS = new Set(['subtotal', 'tax', 'total', 'discount']);
  const METHOD = { pdf_text: 'PDF text layer', 'pdf+ocr': 'PDF + OCR', ocr: 'OCR from image', text: 'Plain text' };

  function vendorEmail(r) {
    const inv = r.invoice || {};
    const reasons = r.resolution === 'REJECTED' ? [r.resolution_detail?.note] : (r.reasons || []);
    const who = r.resolution_detail?.reviewer || reviewerName() || 'Accounts Payable';
    return `Subject: Invoice ${inv.invoice_number || r.invoice_number || ''} could not be processed\n\nHello ${r.vendor || 'team'},\n\nWe could not approve invoice ${inv.invoice_number || r.invoice_number || '(no number found)'}${inv.date ? ` dated ${inv.date}` : ''} for ${money(r.total)}${r.po_number ? ` against purchase order ${r.po_number}` : ''}.\n\nReason:\n${reasons.filter(Boolean).map((x) => `- ${human(x)}`).join('\n') || '- See note from our AP team'}\n\nPlease reply with a corrected invoice or the missing details. If you believe this is a mistake, reply to this email and quote the invoice number.\n\nKind regards,\n${who}\nAccounts Payable`;
  }

  async function invoicePage([rid], params, alive) {
    const m = await meta(); m.teams.forEach((t) => (teamHours[t.team] = t.sla_hours));
    let run;
    try { run = await api(`/api/runs/${rid}`); }
    catch (err) {
      if (err.status === 404) { view.innerHTML = '<div class="card empty"><h3>Invoice not found</h3><p>It may have been removed, or the link is wrong.</p><a class="btn" href="#/invoices">All invoices</a></div>'; return; }
      throw err;
    }
    if (!alive()) return;
    const from = params.get('from') || 'invoices';
    const back = { review: ['#/review', 'Needs review'], upload: ['#/upload', 'Upload'], overview: ['#/', 'Overview'], invoices: ['#/invoices', 'Invoices'], vendors: ['#/vendors', 'Vendors'] }[from] || ['#/invoices', 'Invoices'];
    view.innerHTML = `<nav class="crumbs" aria-label="Breadcrumb"><a href="${back[0]}">← ${back[1]}</a><span>/</span><span>${esc(run.invoice_number || run.source)}</span></nav>
      <div id="top"></div><div class="detail-grid"><section class="card doc-pane" id="doc"></section><div class="stack" id="side"></div></div>`;
    const doc = $('#doc');
    if (run.document_id) {
      const url = `/api/runs/${rid}/document`;
      const isPdf = /\.pdf$/i.test(run.source || '');
      doc.innerHTML = `<div class="row-between"><span>Source document · ${esc(run.source)}</span><a href="${url}" target="_blank" rel="noopener">Open ↗</a></div>${isPdf ? `<iframe src="${url}" title="Invoice document"></iframe>` : `<img src="${url}" alt="Invoice document ${esc(run.source)}">`}`;
    } else doc.innerHTML = '<div class="empty"><p>No source document stored for this record.</p></div>';

    const draw = (r) => {
      run = r;
      const inv = r.invoice || {};
      const rt = route(r);
      const reasons = r.reasons || [];
      const stagesByName = {}; (r.stages || []).forEach((s) => (stagesByName[s.stage] = s));
      const done = Object.values(stagesByName).filter((s) => ['COMPLETED', 'BLOCKED', 'SKIPPED', 'FAILED'].includes(s.status)).length;
      const total = m.stages.length || 13;
      let callout;
      if (r.status === 'RUNNING') {
        const current = m.stages.find((n) => stagesByName[n]?.status === 'RUNNING') || 'Starting';
        callout = `<div class="callout info"><h2><span class="pill info">Processing</span> ${esc(current)}…</h2><p>Step ${Math.min(done + 1, total)} of ${total}. This page updates on its own.</p><div class="progress"><i style="width:${(done / total) * 100}%"></i></div></div>`;
      } else if (r.status === 'FAILED') {
        callout = `<div class="callout bad"><h2>Processing failed</h2><p>${esc(r.error || 'The document could not be processed.')}</p><div class="row" style="margin-top:8px"><a class="btn btn-sm" href="#/upload">Upload it again</a></div></div>`;
      } else if (NON_INVOICE(r)) {
        callout = `<div class="callout neutral"><h2><span class="pill">Filed</span> Not a bill</h2><p>${esc(r.document_routing?.reason || 'This document is not a tax invoice.')} It is kept for reference and left out of approval numbers.</p></div>`;
      } else if (r.resolution) {
        const d = r.resolution_detail || {};
        const ok = r.resolution === 'APPROVED';
        callout = `<div class="callout ${ok ? 'ok' : 'bad'}"><h2><span class="pill ${ok ? 'ok' : 'bad'}">${ok ? 'Approved' : 'Rejected'}</span> by ${esc(d.reviewer || 'a reviewer')}</h2><p>${esc(d.note || (ok ? 'Approved after review.' : ''))}</p><p class="muted" style="font-size:13px">Automated decision was “needs review”: ${esc(human(reasons.slice(0, 2).join('; ')))}. ${dateLabel(d.at)}.</p>
          ${ok ? '' : '<div class="row" style="margin-top:8px"><button class="btn btn-sm" data-email>Draft email to vendor</button></div>'}</div>`;
      } else if (r.status === 'APPROVED') {
        callout = '<div class="callout ok"><h2><span class="pill ok">Approved</span> Every check passed</h2><p>Scheduled for payment. Nobody needed to touch it.</p></div>';
      } else if (r.status === 'REJECTED') {
        callout = `<div class="callout bad"><h2><span class="pill bad">Rejected</span> Must not be paid as sent</h2><ul>${reasons.map((x) => `<li>${esc(human(x))}</li>`).join('')}</ul><div class="row" style="margin-top:8px"><button class="btn btn-sm" data-email>Draft email to vendor</button></div></div>`;
      } else {
        callout = `<div class="callout warn"><h2><span class="pill warn">Needs review</span> Sent to ${esc(rt?.team || 'AP Specialist')} ${slaChip(r)}</h2><p>${esc(human(rt?.reason || reasons[0] || 'A person needs to confirm this invoice.'))}</p>${reasons.filter((x) => x !== (rt?.reason || reasons[0])).length ? `<ul>${reasons.filter((x) => x !== (rt?.reason || reasons[0])).map((x) => `<li>${esc(human(x))}</li>`).join('')}</ul>` : ''}</div>`;
      }
      const canDecide = r.status === 'PENDING_REVIEW' && !r.resolution && !NON_INVOICE(r);
      const actions = canDecide ? `<section class="card action-panel" id="decide"><div class="card-head" style="margin:0"><div><h2>Your decision</h2><p>Approving re-runs the PO, duplicate and arithmetic checks with your corrections. If it would break a rule, approval is blocked.</p></div></div>
          <label><span class="label-line">Note <span class="muted">· required to reject, and included in the vendor email</span></span><textarea id="note" rows="2" placeholder="e.g. PO confirmed with buyer by phone"></textarea></label>
          <details class="more" id="correct"><summary>Correct extracted details first</summary><div class="correction-grid" style="margin-top:12px">${FIELDS.map(([k, l]) => `<label>${l}<input data-field="${k}" value="${esc(inv[k] ?? '')}" ${MONEY_FIELDS.has(k) ? 'inputmode="decimal"' : ''}></label>`).join('')}</div></details>
          <div class="row-end"><button class="btn btn-primary" data-act="APPROVED"><svg viewBox="0 0 24 24"><path d="m5 12 5 5 9-10"/></svg>Approve</button><button class="btn btn-danger" data-act="REJECTED">Reject</button><button class="btn btn-ghost" data-act="NOTE">Save note only</button></div>
          <p class="form-error" id="decideerror" role="alert"></p></section>` : '';
      $('#top').innerHTML = `<div class="detail-head"><div><div class="row">${pill(r)}${rt && canDecide ? `<span class="tag">${esc(rt.team)}</span>` : ''}${r.document_type && r.document_type !== 'TAX_INVOICE' ? `<span class="tag">${esc(r.document_type.replace(/_/g, ' ').toLowerCase())}</span>` : ''}</div>
          <h1>${esc(title(r))}</h1>
          <div class="detail-meta"><span>Invoice <b>${dash(r.invoice_number)}</b></span><span>PO <b>${dash(r.po_number)}</b></span><span>Dated <b>${dash(inv.date)}</b></span>${inv.details?.due_date ? `<span>Due <b>${esc(inv.details.due_date)}</b></span>` : ''}<span>Read by <b>${esc(METHOD[r.method] || r.method || '—')}</b></span><span>Received <b title="${esc(dateLabel(r.time))}">${esc(ago(r.time))}</b></span></div></div>
          <div class="amount">${money(r.total)}<small>${inv.tax != null ? `incl. ${money(inv.tax)} tax` : 'total'}</small></div></div>
        ${callout}`;

      const checks = r.decision?.checks || [];
      const issues = checks.filter((c) => c.outcome !== 'VERIFIED_PASS');
      const passed = checks.filter((c) => c.outcome === 'VERIFIED_PASS');
      const checkRow = (c) => { const tone = c.outcome === 'VERIFIED_PASS' ? 'ok' : c.outcome === 'CONFIRMED_VIOLATION' ? 'bad' : 'warn';
        return `<div class="check"><span class="check-icon ${tone}">${tone === 'ok' ? '✓' : tone === 'bad' ? '×' : '!'}</span><div><b>${esc(m.checks[c.check] || c.check)}</b><small>${esc(human(c.detail))}</small></div></div>`; };
      const meta_ = inv.field_metadata || {};
      const fieldRows = FIELDS.map(([k, l]) => { const fm = meta_[k] || {}; const rel = RELIABILITY[fm.reliability] || null; const v = inv[k];
        return `<tr><td>${l}</td><td><div class="row" style="gap:8px"><span class="${MONEY_FIELDS.has(k) ? 'num' : ''}">${MONEY_FIELDS.has(k) ? money(v) : dash(v)}</span>${v != null && rel ? `<span class="tag ${rel[1]}">${rel[0]}</span>` : ''}</div>${fm.evidence ? `<div class="evidence">“${esc(String(fm.evidence).slice(0, 140))}”</div>` : ''}</td></tr>`; }).join('');
      const t0 = r.stages?.[0]?.at ? new Date(r.stages[0].at).getTime() : 0;
      const steps = m.stages.map((n, i) => { const s = stagesByName[n] || { status: 'PENDING', detail: 'Waiting' };
        return `<div class="step ${esc(s.status)}"><span class="step-dot">${s.status === 'COMPLETED' ? '✓' : s.status === 'BLOCKED' ? '!' : s.status === 'FAILED' ? '×' : i + 1}</span><div><b>${esc(n)}</b><small>${esc(s.detail || s.status.toLowerCase())}</small></div><span class="mono">${s.at && t0 ? `+${((new Date(s.at).getTime() - t0) / 1000).toFixed(2)}s` : ''}</span></div>`; }).join('');
      const ai = r.ai_review;
      const history = (r.reviews || []).slice().reverse();
      const outcomeLabel = { APPROVED_BY_REVIEWER: 'Approved', REJECTED_BY_REVIEWER: 'Rejected', VERIFIED: 'Note', NEEDS_CORRECTION: 'Correction' };
      $('#side').innerHTML = `${actions}
        ${checks.length ? `<section class="card"><div class="card-head"><div><h2>Checks</h2><p>${issues.length ? `${plural(issues.length, 'check')} need attention · ${passed.length} passed` : `All ${passed.length} checks passed`}</p></div></div>
          <div class="checks">${issues.map(checkRow).join('')}</div>${passed.length ? `<details class="more" ${issues.length ? '' : 'open'}><summary>${plural(passed.length, 'passed check')}</summary><div class="checks">${passed.map(checkRow).join('')}</div></details>` : ''}</section>` : ''}
        ${r.invoice ? `<section class="card"><div class="card-head"><div><h2>Extracted details</h2><p>What was read from the document, and the evidence for each value</p></div></div><table class="fields"><tbody>${fieldRows}</tbody></table>
          ${(inv.line_items || []).length ? `<details class="more"><summary>${plural(inv.line_items.length, 'line item')}</summary><ul style="margin:8px 0 0;padding-left:18px;font-size:13.5px">${inv.line_items.map((li) => `<li>${esc(li.description)}</li>`).join('')}</ul></details>` : ''}</section>` : ''}
        <section class="card"><div class="card-head"><div><h2>Processing steps</h2><p>${r.timings ? `Finished in ${seconds(r.timings.total_seconds)}${r.ocr_cache_hit ? ' · text reused from an earlier identical upload' : ''}` : `${done} of ${total} steps`}</p></div></div><div class="steps">${steps}</div></section>
        ${ai ? `<section class="card"><div class="card-head"><div><h2>AI evidence review</h2><p>Advisory only — it can never change the decision</p></div><span class="tag ai">AI</span></div><p style="font-size:14px"><b style="font-weight:500">${esc(ai.recommendation.replace(/_/g, ' ').toLowerCase().replace(/^./, (c) => c.toUpperCase()))}.</b> ${esc(human(ai.summary))}</p>
          ${r.ai_extraction?.status === 'COMPLETED' ? `<p class="muted" style="font-size:13px;margin-top:8px">AI filled ${plural(r.ai_extraction.accepted_fields.length, 'field')} it could quote from the page.</p>` : ''}</section>` : ''}
        <section class="card"><div class="card-head"><div><h2>Activity</h2><p>Every person who touched this invoice</p></div><a class="btn btn-sm" href="/api/runs/${esc(r.run_id)}/audit.json" download>Audit record</a></div>
          ${history.length ? history.map((h) => `<div class="history-item"><b style="font-weight:500">${esc(outcomeLabel[h.outcome] || h.outcome)}</b> · ${esc(h.reviewer)} <span class="muted">· ${esc(dateLabel(h.at))}</span>${h.note ? `<div class="muted">${esc(h.note)}</div>` : ''}</div>`).join('') : '<p class="muted" style="font-size:14px">No one has reviewed this invoice. Automated decision recorded at intake.</p>'}</section>`;
      wireDetail();
    };

    const wireDetail = () => {
      $$('[data-email]').forEach((b) => b.addEventListener('click', () => { $('#emailbody').value = vendorEmail(run); $('#emaildialog').showModal(); }));
      $$('[data-act]').forEach((b) => b.addEventListener('click', async () => {
        const act = b.dataset.act; const err = $('#decideerror'); err.textContent = '';
        const reviewer = await ensureName(); if (!reviewer) { err.textContent = 'Enter your name so the decision is attributed.'; return; }
        const note = $('#note').value.trim();
        const corrections = {};
        $$('[data-field]').forEach((input) => {
          const k = input.dataset.field; const original = run.invoice?.[k]; const v = input.value.trim();
          if (MONEY_FIELDS.has(k)) { if (v !== '' && Number(v.replace(/,/g, '')) !== Number(original)) corrections[k] = Number(v.replace(/,/g, '')); }
          else if (v !== String(original ?? '') && v !== '') corrections[k] = v;
        });
        if (Object.values(corrections).some((v) => typeof v === 'number' && isNaN(v))) { err.textContent = 'Amounts must be numbers.'; return; }
        if (act === 'REJECTED' && !note) { err.textContent = 'Add a note saying why — it is sent to the vendor.'; $('#note').focus(); return; }
        $$('[data-act]').forEach((x) => (x.disabled = true));
        try {
          if (act === 'NOTE') {
            if (!note && !Object.keys(corrections).length) { err.textContent = 'Write a note or correct a field first.'; return; }
            const res = await api(`/api/runs/${rid}/reviews`, { method: 'POST', json: { reviewer, outcome: Object.keys(corrections).length ? 'NEEDS_CORRECTION' : 'VERIFIED', note: note || 'Corrected extracted details', corrections } });
            toast(res.revised_review_result ? `Note saved. With your corrections the checks would give: ${res.revised_review_result.status.replace('_', ' ').toLowerCase()}` : 'Note saved');
            draw(await api(`/api/runs/${rid}`));
            return;
          }
          await api(`/api/runs/${rid}/resolve`, { method: 'POST', json: { action: act, reviewer, note, corrections } });
          refreshCount();
          const queue = (await api('/api/runs?open=1&limit=200')).filter((x) => x.run_id !== rid).sort((a, b) => (sla(a)?.left ?? 0) - (sla(b)?.left ?? 0));
          const next = queue[0];
          toast(act === 'APPROVED' ? 'Approved — scheduled for payment' : 'Rejected — vendor email ready', next ? { action: { href: `#/invoice/${next.run_id}?from=review`, label: 'Next in queue →' } } : {});
          if (from === 'review' && next) go(`#/invoice/${next.run_id}?from=review`);
          else draw(await api(`/api/runs/${rid}`));
        } catch (e) { err.textContent = e.message; }
        finally { $$('[data-act]').forEach((x) => (x.disabled = false)); }
      }));
    };
    draw(run);

    if (run.status === 'RUNNING') {
      const source = new EventSource(`/api/runs/${rid}/events`);
      let queued = null;
      const refresh = async () => { queued = null; if (alive()) draw(await api(`/api/runs/${rid}`)); };
      source.onmessage = () => { if (!queued) queued = setTimeout(refresh, 250); };
      source.addEventListener('finished', async () => {
        source.close(); if (!alive()) return;
        const r = await api(`/api/runs/${rid}`); draw(r); refreshCount();
        const o = outcome(r); toast(`${o.label}${r.vendor ? ` · ${r.vendor}` : ''}`, { tone: o.tone === 'bad' ? 'bad' : '' });
      });
      source.onerror = () => { source.close(); setTimeout(refresh, 1000); };
      onLeave(() => source.close());
    }
  }

  /* ================= Vendors ================= */
  async function vendorsPage(_args, _params, alive) {
    const { vendors, source } = await api('/api/vendors');
    if (!alive()) return;
    const attention = vendors.filter((v) => v.attention.length).length;
    view.innerHTML = `
      ${pageHead('Vendors', "Who's billing you", `Every vendor on file and everyone who has sent an invoice: what they sent, what was approved, what's waiting, and anything that looked wrong. ${source === 'file' ? 'Using the bundled sample vendor list.' : ''}`,
        attention ? `<span class="status-chip warn"><b>${attention}</b>${attention === 1 ? 'vendor needs' : 'vendors need'} attention</span>` : '')}
      <div class="grid grid-3">${vendors.map((v) => { const blocked = /BLOCKED|PROHIBITED|UNAUTHORISED/.test(String(v.status || '').toUpperCase());
        return `<a class="card vendor-card ${v.attention.length ? 'attention' : ''}" href="#/invoices?q=${encodeURIComponent(v.vendor)}">
          <div class="row-between"><h3>${esc(v.vendor)}</h3>${blocked ? '<span class="pill bad">Blocked</span>' : v.on_file ? '<span class="pill ok">Active</span>' : '<span class="pill warn">Not on file</span>'}</div>
          ${v.gstin ? `<span class="mono muted">${esc(v.gstin)}</span>` : ''}
          ${v.attention.length ? `<div class="row">${v.attention.map((a) => `<span class="pill warn plain">${esc(a)}</span>`).join('')}</div>` : ''}
          <div class="vendor-stats"><div><b>${v.invoices}</b><span>invoices</span></div><div><b>${moneyShort(v.approved_value)}</b><span>approved</span></div><div><b>${v.waiting}</b><span>waiting</span></div><div><b>${v.rejected}</b><span>rejected</span></div></div>
          <dl class="kv"><dt>Open orders</dt><dd>${v.open_pos ? `${v.open_pos} · ${money(v.po_remaining)} left to bill` : 'None'}</dd><dt>Last invoice</dt><dd>${esc(ago(v.last_invoice))}</dd></dl></a>`; }).join('')
        || '<div class="card empty" style="grid-column:1/-1"><h3>No vendors yet</h3><p>Add a vendor master on Orders &amp; data, or process an invoice.</p><a class="btn" href="#/data">Orders &amp; data</a></div>'}</div>`;
  }

  /* ================= Orders & data ================= */
  async function dataPage(_args, _params, alive) {
    const [po, vm, paidRes] = await Promise.all([api('/api/master/po_master'), api('/api/master/vendor_master'), api('/api/runs?decision=APPROVED&limit=100')]);
    if (!alive()) return;
    const source = (d) => (d.source === 'database' ? `Updated by ${esc(d.updated_by)} · ${esc(dateLabel(d.updated_at))}` : d.source === 'file' ? 'Bundled sample file' : 'Not set up');
    const uploader = (kind, hint) => `<form class="row" data-master="${kind}" style="padding:16px 22px;border-top:1px solid var(--line)">
        <label style="flex:2;min-width:220px">Replace with a CSV<input type="file" accept=".csv,text/csv" required></label>
        <label style="flex:1;min-width:160px">Changed by<input data-by required maxlength="80" value="${esc(reviewerName())}"></label>
        <div class="row" style="align-self:flex-end"><button class="btn btn-primary">Check &amp; save</button><a class="btn" href="/api/master/${kind}.csv" download>Download</a></div>
        <p class="muted" style="flex-basis:100%;font-size:12.5px">${hint}</p><ul class="form-error" data-errors style="flex-basis:100%;margin:0;padding-left:18px"></ul></form>`;
    view.innerHTML = `
      ${pageHead('Orders & data', 'What every invoice is checked against', 'Purchase orders and the vendor list drive matching, balance and vendor checks. The payment record grows each time an invoice is approved — that is how repeat bills and over-billing get caught.')}
      <section class="card card-flush"><div class="card-head"><div><h2>Purchase orders</h2><p>${source(po)}</p></div></div>
        ${po.rows.length ? `<div class="table-wrap"><table><thead><tr><th>PO</th><th>Vendor</th><th>Status</th><th class="right">Order value</th><th class="right">Billed before</th><th class="right">Approved here</th><th style="min-width:170px">Used up</th></tr></thead><tbody>
          ${po.rows.map((r) => { const u = r.utilisation == null ? 0 : r.utilisation; const tone = r.remaining < 0 ? 'bad' : u >= 0.9 ? 'warn' : 'ok';
            return `<tr><td class="mono">${esc(r.po_number)}</td><td>${esc(r.vendor)}</td><td><span class="pill ${/CLOSED|BLOCKED|PROHIBITED/.test(r.status) ? '' : 'ok'}">${esc((r.status || 'OPEN').toLowerCase())}</span></td><td class="right num">${money(r.po_amount)}</td><td class="right num">${money(r.invoiced_to_date)}</td><td class="right num">${money(r.approved_value)} <span class="muted">(${r.approved_invoices})</span></td>
              <td><div class="track"><i class="${tone}" style="width:${Math.min(100, u * 100)}%"></i></div><small class="muted">${pct(u)} · ${money(r.remaining)} left</small></td></tr>`; }).join('')}</tbody></table></div>` : '<div class="empty"><p>No purchase orders yet.</p></div>'}
        ${uploader('po_master', 'Columns: po_number, vendor, po_amount, invoiced_to_date. Optional: status (OPEN, CLOSED, BLOCKED), gstin, currency. Changes apply to invoices processed afterwards.')}</section>
      <section class="card card-flush section"><div class="card-head"><div><h2>Payment record</h2><p>Approved invoices — automatically or by a reviewer</p></div><span class="muted" style="font-size:13px">${plural(paidRes.length, 'entry', 'entries')}</span></div>
        ${paidRes.length ? `<div class="table-wrap"><table><thead><tr><th>Invoice</th><th>Vendor</th><th>PO</th><th class="right">Total</th><th>Approved by</th><th class="right">When</th></tr></thead><tbody>
          ${paidRes.map((r) => `<tr class="clickable" data-href="#/invoice/${esc(r.run_id)}?from=invoices"><td class="mono">${dash(r.invoice_number)}</td><td>${esc(r.vendor || '—')}</td><td class="mono">${dash(r.po_number)}</td><td class="right num">${money(r.total)}</td><td>${r.resolution === 'APPROVED' ? esc(r.reviews?.[0]?.reviewer || 'Reviewer') : '<span class="muted">Automatic</span>'}</td><td class="right muted">${esc(ago(r.time))}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty"><p>No approved invoices yet.</p></div>'}</section>
      <section class="card card-flush section"><div class="card-head"><div><h2>Vendors</h2><p>${source(vm)}${vm.source === 'none' ? ' — vendors are taken from the purchase orders' : ''}</p></div></div>
        ${vm.rows.length ? `<div class="table-wrap"><table><thead><tr><th>Vendor</th><th>GSTIN</th><th>Status</th></tr></thead><tbody>${vm.rows.map((r) => `<tr><td>${esc(r.vendor)}</td><td class="mono">${dash(r.gstin)}</td><td><span class="pill ${/BLOCKED|PROHIBITED|UNAUTHORISED/.test(r.status) ? 'bad' : 'ok'}">${esc((r.status || 'APPROVED').toLowerCase())}</span></td></tr>`).join('')}</tbody></table></div>` : ''}
        ${uploader('vendor_master', 'Columns: vendor. Optional: gstin (checksum-validated), status (APPROVED, BLOCKED).')}</section>`;
    wireRows(view);
    $$('[data-master]').forEach((form) => form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const kind = form.dataset.master; const errors = $('[data-errors]', form); errors.innerHTML = '';
      const file = $('input[type=file]', form).files[0]; if (!file) return;
      if (!confirm(`Replace the ${kind === 'po_master' ? 'purchase order' : 'vendor'} list? Invoices processed from now on use the new list.`)) return;
      try {
        const res = await api(`/api/master/${kind}`, { method: 'PUT', json: { csv: await file.text(), updated_by: $('[data-by]', form).value } });
        toast(`${plural(res.saved, 'row')} saved`); render();
      } catch (err) { errors.innerHTML = (err.data?.errors || [err.message]).slice(0, 20).map((x) => `<li>${esc(x)}</li>`).join(''); }
    }));
  }

  /* ================= How it works ================= */
  async function howPage(_args, _params, alive) {
    const [m, ai] = await Promise.all([meta(), api('/api/ai/config').catch(() => ({}))]);
    if (!alive()) return;
    const steps = [
      ['File received', 'A fingerprint of the file is taken, so the same document is never read twice.', 'SYSTEM'],
      ['Text is read', 'Digital PDFs give up their text directly. Scans and photos go through OCR on this server.', 'SYSTEM'],
      ['What kind of document', 'Tax invoice, purchase order, delivery challan, credit note… Only tax invoices can be approved.', 'RULES'],
      ['Details picked out', 'Vendor, invoice number, dates, PO, GSTIN and amounts — each kept with the text it was found beside.', 'RULES'],
      ['AI double-check', 'Optional. A model may fill gaps, but only with a quote that is found on the page. It never decides.', 'AI'],
      ['Vendor check', 'Known vendor? Not blocked? GSTIN valid and matching the vendor on file?', 'RULES'],
      ['Order match', 'Find the purchase order, confirm it is open, and that this bill fits its remaining balance.', 'RULES'],
      ['Maths & tax', 'Subtotal plus tax equals the total; CGST, SGST and IGST are consistent.', 'RULES'],
      ['Repeat check', 'Same vendor and number already approved, or a lookalike with the same amount, date and PO.', 'RULES'],
      ['Decision & routing', 'Any confirmed violation rejects. Anything uncertain goes to the right team with a deadline. Otherwise approved.', 'RULES'],
    ];
    const b = m.benchmark || {};
    view.innerHTML = `
      ${pageHead('How it works', 'The software reads. The rules decide.', 'Reading a document is the only fuzzy step. Every decision about money comes from plain rules you can see below — and every decision records why.')}
      <section class="card"><div class="card-head"><div><h2>Ten steps, every invoice</h2></div></div>
        <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(220px,1fr))">${steps.map(([t, d, k], i) => `<div class="card how-step" style="box-shadow:none"><span class="mono">${String(i + 1).padStart(2, '0')}</span><h3>${t}</h3><p>${d}</p><span><span class="tag ${k === 'AI' ? 'ai' : ''}">${k}</span></span></div>`).join('')}</div></section>
      <div class="grid grid-3 section">
        <div class="card stack"><span class="pill ok" style="justify-self:start">Approved</span><p>Every check passed. Recorded and scheduled for payment with nobody touching it.</p></div>
        <div class="card stack"><span class="pill warn" style="justify-self:start">Needs review</span><p>Something is uncertain — missing, unreadable or unusual. It goes to the team that can fix it, with a deadline. A reviewer approves or rejects; the automated call is kept for audit.</p></div>
        <div class="card stack"><span class="pill bad" style="justify-self:start">Rejected</span><p>Must not be paid as sent: already approved, over the PO balance, a blocked vendor, or totals that don't add up. A ready-to-send vendor email explains why.</p></div>
      </div>
      <div class="grid grid-2 section">
        <section class="card card-flush"><div class="card-head"><div><h2>Who handles what</h2><p>Highest-risk team wins when several checks fail</p></div></div>
          <div class="table-wrap"><table><thead><tr><th>Team</th><th>Handles</th><th class="right">Deadline</th></tr></thead><tbody>${m.teams.map((t) => `<tr><td>${esc(t.team)}</td><td class="muted">${esc(t.handles)}</td><td class="right">${t.sla_hours} h</td></tr>`).join('')}</tbody></table></div></section>
        <section class="card"><div class="card-head"><div><h2>How accurate is the reading?</h2><p>Measured honestly on real, messy invoice images</p></div></div>
          ${b.comparable ? `<div class="grid grid-3"><div class="stat"><b>${b.exact}/${b.comparable}</b><span>fields read exactly</span></div><div class="stat"><b>${pct(b.accuracy)}</b><span>exact-match rate</span></div><div class="stat"><b>${b.samples}</b><span>real GST invoice images</span></div></div>
          <p class="muted" style="font-size:13.5px;margin-top:14px">These are photos and scans from a public dataset with noisy labels — a hard test. Because reading is imperfect, the rules never trust a value without its printed label beside it; anything weaker goes to a person rather than being approved.</p>` : '<p class="muted">Run batch_evaluate.py to produce a benchmark.</p>'}</section>
      </div>
      <section class="card section"><div class="card-head"><div><h2>Optional AI connection</h2><p>Any OpenAI-compatible endpoint. The key stays on the server and is never shown again.</p></div><span class="tag ${ai.configured ? 'ai' : ''}">${ai.configured ? 'Connected' : 'Not set up'}</span></div>
        <form class="grid grid-3" id="aiform"><label>Endpoint<input id="aiendpoint" type="url" autocomplete="off" value="${esc(ai.endpoint || '')}" placeholder="https://…/v1/chat/completions"></label><label>Model<input id="aimodel" autocomplete="off" value="${esc(ai.model || '')}" placeholder="model name"></label><label>API key<input id="aikey" type="password" autocomplete="new-password" placeholder="${ai.api_key_configured ? 'A key is set — leave blank to keep it' : 'Paste a key'}"></label>
          <div class="row" style="grid-column:1/-1"><button class="btn btn-primary">Save</button><button class="btn" type="button" id="aitest">Test connection</button><span class="muted" id="aimsg" style="font-size:13px"></span></div></form></section>`;
    $('#aiform').addEventListener('submit', async (e) => {
      e.preventDefault();
      try { const r = await api('/api/ai/config', { method: 'POST', json: { endpoint: $('#aiendpoint').value, model: $('#aimodel').value, api_key: $('#aikey').value } }); $('#aikey').value = ''; $('#aimsg').textContent = r.note; }
      catch (err) { $('#aimsg').textContent = err.message; }
    });
    $('#aitest').addEventListener('click', async () => {
      $('#aimsg').textContent = 'Testing…';
      try { const r = await api('/api/ai/connection-test', { method: 'POST' }); $('#aimsg').textContent = `${r.note} (${r.model})`; }
      catch (err) { $('#aimsg').textContent = err.message; }
    });
  }

  /* ---------- command palette ---------- */
  const palette = $('#palette'); const pq = $('#paletteresults');
  const PAGES = [['Overview', '#/'], ['Upload an invoice', '#/upload'], ['Needs review', '#/review'], ['Invoices', '#/invoices'], ['Vendors', '#/vendors'], ['Orders & data', '#/data'], ['How it works', '#/how']];
  let paletteItems = []; let paletteIndex = 0; let paletteTimer;
  function paintPalette(runs = []) {
    const q = $('#palettequery').value.trim().toLowerCase();
    const pages = PAGES.filter(([l]) => !q || l.toLowerCase().includes(q));
    paletteItems = [...runs.map((r) => ({ href: `#/invoice/${r.run_id}?from=invoices`, html: `<span>${esc(title(r))} <small>${dash(r.invoice_number)}</small></span>${pill(r)}` })),
      ...pages.map(([l, h]) => ({ href: h, html: `<span>${esc(l)}</span><small>Go to page</small>` }))];
    paletteIndex = Math.min(paletteIndex, Math.max(0, paletteItems.length - 1));
    pq.innerHTML = (runs.length ? '<li class="group">Invoices</li>' : '') + paletteItems.map((it, i) => (i === runs.length && pages.length ? '<li class="group">Pages</li>' : '') + `<li role="option" data-i="${i}" aria-selected="${i === paletteIndex}">${it.html}</li>`).join('') || '<li class="group">No matches</li>';
    $$('li[data-i]', pq).forEach((li) => li.addEventListener('click', () => choose(Number(li.dataset.i))));
  }
  function choose(i) { const it = paletteItems[i]; if (!it) return; palette.close(); go(it.href); }
  function openPalette() { $('#palettequery').value = ''; paletteIndex = 0; paintPalette(); palette.showModal(); $('#palettequery').focus(); }
  $('#opensearch').addEventListener('click', openPalette);
  $('#palettequery').addEventListener('input', () => {
    clearTimeout(paletteTimer); paletteIndex = 0; paintPalette();
    const q = $('#palettequery').value.trim(); if (q.length < 2) return;
    paletteTimer = setTimeout(async () => { try { paintPalette(await api(`/api/runs?q=${encodeURIComponent(q)}&limit=8`)); } catch (_) {} }, 200);
  });
  $('#palettequery').addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); paletteIndex = (paletteIndex + (e.key === 'ArrowDown' ? 1 : -1) + paletteItems.length) % Math.max(1, paletteItems.length); $$('li[data-i]', pq).forEach((li) => li.setAttribute('aria-selected', String(Number(li.dataset.i) === paletteIndex))); }
    if (e.key === 'Enter') { e.preventDefault(); choose(paletteIndex); }
  });
  palette.addEventListener('click', (e) => { if (e.target === palette) palette.close(); });
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); palette.open ? palette.close() : openPalette(); }
    else if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && !palette.open) { e.preventDefault(); openPalette(); }
  });
  $('#copyemail').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('#emailbody').value); toast('Email copied'); }
    catch (_) { $('#emailbody').select(); toast('Press ⌘C to copy'); }
  });

  /* ---------- boot ---------- */
  refreshCount();
  setInterval(() => { if (!document.hidden) refreshCount(); }, 30000);
  render();
})();
