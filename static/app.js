/* zamp infinity — single-page app on the Flask JSON API. No dependencies. */
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
  const isNum = (v) => v !== null && v !== undefined && v !== '' && !isNaN(Number(v));
  const money = (v) => (isNum(v) ? inr.format(Number(v)) : '—');
  const moneyShort = (v) => (!isNum(v) ? '—' : Math.abs(v) < 100000 ? inr.format(Math.round(Number(v))).replace(/\.00$/, '') : inrCompact.format(Number(v)));
  const pct = (v) => (v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`);
  const plural = (n, word, many) => `${n} ${n === 1 ? word : many || word + 's'}`;
  function span(ms) {
    const m = Math.round(Math.abs(ms) / 60000);
    if (m < 60) return `${Math.max(m, 1)} min`;
    const h = Math.round(m / 60);
    return h < 48 ? `${h} h` : `${Math.round(h / 24)} days`;
  }
  const ago = (iso) => (!iso ? '—' : Date.now() - new Date(iso).getTime() < 45000 ? 'just now' : `${span(Date.now() - new Date(iso).getTime())} ago`);
  const secs = (s) => (s === null || s === undefined ? '—' : s < 1 ? `${Math.round(s * 1000)} ms` : `${s.toFixed(s < 10 ? 1 : 0)} s`);
  const stamp = (iso) => (iso ? new Date(iso).toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—');
  const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }) : '');
  function dayLabel(iso) {
    const d = new Date(iso); const today = new Date();
    const diff = Math.round((new Date(today.toDateString()) - new Date(d.toDateString())) / 864e5);
    return diff === 0 ? 'Today' : diff === 1 ? 'Yesterday' : d.toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short' });
  }
  // Backend reasons name fields by key ("Missing: po_number"); show words.
  const FIELD_WORDS = { po_number: 'PO number', invoice_number: 'invoice number', date: 'invoice date', total: 'total', subtotal: 'subtotal', tax: 'tax', vendor: 'supplier', gstin: 'GSTIN' };
  const human = (text) => String(text ?? '').replace(/\b(po_number|invoice_number|subtotal|gstin)\b|(?<=(?:Missing|missing):[^.;]*)\b(date|total|tax|vendor)\b/g, (m) => FIELD_WORDS[m] || m);

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
  const teamHours = {};
  const meta = () => (metaPromise ||= api('/api/meta').then((m) => { m.teams.forEach((t) => (teamHours[t.team] = t.sla_hours)); return m; })
    .catch(() => ({ checks: {}, stages: [], teams: [], benchmark: {} })));

  /* ---------- toasts ---------- */
  function toast(message, { tone = '', action } = {}) {
    const el = document.createElement('div');
    el.className = `toast ${tone}`;
    el.innerHTML = `<span>${esc(message)}</span>${action ? `<a href="${esc(action.href)}">${esc(action.label)}</a>` : ''}`;
    $('#toasts').append(el);
    setTimeout(() => el.remove(), action ? 7000 : 4000);
  }

  /* ---------- reviewer, theme, sidebar ---------- */
  const store = { get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} } };
  // Default reviewer until someone sets their own name from the sidebar.
  const DEFAULT_REVIEWER = 'Anant';
  const reviewerName = () => store.get('zamp-reviewer') || DEFAULT_REVIEWER;
  function paintName() {
    const n = reviewerName();
    $('#whoaminame').textContent = n || 'Set your name';
    $('#whoavatar').textContent = n ? n.split(/\s+/).map((p) => p[0]).slice(0, 2).join('').toUpperCase() : '?';
  }
  function askName() {
    const dialog = $('#namedialog');
    $('#nameinput').value = reviewerName();
    dialog.showModal();
    return new Promise((resolve) => dialog.addEventListener('close', () => {
      if (dialog.returnValue === 'save' && $('#nameinput').value.trim()) { store.set('zamp-reviewer', $('#nameinput').value.trim()); paintName(); }
      resolve(reviewerName());
    }, { once: true }));
  }
  const ensureName = async () => reviewerName() || (await askName());
  $('#whoami').addEventListener('click', askName);
  $('#themetoggle').addEventListener('click', () => {
    document.documentElement.dataset.theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    store.set('zamp-theme', document.documentElement.dataset.theme);
  });
  const sidebar = $('#sidebar');
  const setMenu = (open) => { sidebar.classList.toggle('open', open); $('#scrim').hidden = !open; $('#menubtn').setAttribute('aria-expanded', String(open)); };
  $('#menubtn').addEventListener('click', () => setMenu(!sidebar.classList.contains('open')));
  $('#scrim').addEventListener('click', () => setMenu(false));
  paintName();
  const setCrumb = (html) => { $('#crumb').innerHTML = html; };

  /* ---------- outcome vocabulary ---------- */
  const NON_INVOICE = (r) => (r.document_routing?.status || 'INVOICE_AP_WORKFLOW') === 'NON_INVOICE';
  function outcome(r) {
    if (r.status === 'RUNNING') return { label: 'Reading', tone: 'info', g: '◦' };
    if (r.status === 'FAILED') return { label: 'Failed', tone: 'bad', g: '×' };
    if (NON_INVOICE(r)) return { label: 'Filed', tone: '', g: '–' };
    if (r.resolution === 'APPROVED') return { label: 'Approved · reviewed', tone: 'ok', g: '✓' };
    if (r.resolution === 'REJECTED') return { label: 'Rejected · reviewed', tone: 'bad', g: '×' };
    if (r.status === 'APPROVED') return { label: 'Approved', tone: 'ok', g: '✓' };
    if (r.status === 'REJECTED') return { label: 'Rejected', tone: 'bad', g: '×' };
    return { label: 'Exception', tone: 'warn', g: '!' };
  }
  const tag = (r) => { const o = outcome(r); return `<span class="tag ${o.tone}"><i>${o.g}</i>${esc(o.label)}</span>`; };
  const route = (r) => r.review_route || (r.route_team ? { team: r.route_team, sla_hours: teamHours[r.route_team] || 24 } : null);
  const isOpen = (r) => r.status === 'PENDING_REVIEW' && !r.resolution && !!route(r);
  function nextStep(r) {
    const o = outcome(r).label;
    if (r.status === 'RUNNING') return 'Reading the document';
    if (r.status === 'FAILED') return 'Upload again';
    if (o === 'Filed') return 'Kept for reference';
    if (o.startsWith('Approved')) return r.invoice?.details?.already_paid ? 'Recorded as already paid' : 'Ready for payment';
    if (o.startsWith('Rejected')) return 'Write to supplier';
    return `With ${route(r)?.team || 'AP Specialist'}`;
  }
  function due(r) {
    const rt = route(r);
    if (!rt || !isOpen(r)) return null;
    const window = rt.sla_hours * 3.6e6;
    const left = new Date(r.time).getTime() + window - Date.now();
    if (left < 0) return { text: `${span(left)} overdue`, cls: 'late', left };
    return { text: `due in ${span(left)}`, cls: left < window * 0.25 ? 'soon' : '', left };
  }
  const dueChip = (r) => { const d = due(r); return d ? `<span class="due ${d.cls}">${esc(d.text)}</span>` : ''; };
  const byDue = (a, b) => (due(a)?.left ?? 0) - (due(b)?.left ?? 0);
  const title = (r) => r.vendor || r.source || 'Document';

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
    const navKey = match[2] || (path.startsWith('invoice/') ? params.get('from') || 'invoices' : '');
    $$('.side-nav a').forEach((a) => a.classList.toggle('active', a.dataset.nav === navKey));
    setMenu(false);
    setCrumb('');
    view.innerHTML = '<div class="stack"><div class="skeleton" style="height:40px;width:45%"></div><div class="skeleton" style="height:160px"></div><div class="skeleton" style="height:280px"></div></div>';
    try {
      await meta();
      await match[1](args.slice(1), params, alive);
      if (alive()) {
        view.classList.remove('fade-in'); void view.offsetWidth; view.classList.add('fade-in');
        // After navigating, keyboard focus belongs to the new page, not the menu link.
        if (document.activeElement?.closest('.sidebar, .palette') || document.activeElement === document.body) view.focus({ preventScroll: true });
      }
    } catch (err) {
      if (!alive()) return;
      view.innerHTML = `<div class="panel empty"><h3>That didn't load</h3><p>${esc(err.message)}</p><a class="btn" href="#/">Back to Today</a></div>`;
    }
  }
  window.addEventListener('hashchange', () => { render(); window.scrollTo({ top: 0 }); });
  const go = (hash) => { if (location.hash === hash) render(); else location.hash = hash; };
  const intro = (h, p, actions = '') => `<div class="intro"><div><h1>${h}</h1>${p ? `<p>${p}</p>` : ''}</div>${actions ? `<div class="actions">${actions}</div>` : ''}</div>`;
  function wireRows(root) {
    $$('tr[data-href]', root).forEach((tr) => {
      tr.addEventListener('click', (e) => { if (!e.target.closest('a,button')) go(tr.dataset.href); });
      tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(tr.dataset.href); });
    });
  }

  /* ================= Today ================= */
  function areaChart(days) {
    const W = 600, H = 150, pad = 6;
    const max = Math.max(1, ...days.map((d) => d.total));
    const x = (i) => pad + (i * (W - pad * 2)) / (days.length - 1);
    const y = (v) => H - pad - (v / max) * (H - pad * 2 - 10);
    const line = (key) => days.map((d, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(d[key]).toFixed(1)}`).join(' ');
    const area = `${line('total')} L${x(days.length - 1)},${H} L${x(0)},${H} Z`;
    const last = days[days.length - 1];
    return `<svg class="area" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Invoices received per day over 14 days">
      <defs><linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f98963" stop-opacity=".35"/><stop offset=".6" stop-color="#ac90e1" stop-opacity=".12"/><stop offset="1" stop-color="#ac90e1" stop-opacity="0"/></linearGradient></defs>
      ${[0.33, 0.66].map((f) => `<line class="grid" x1="0" x2="${W}" y1="${(H * f).toFixed(0)}" y2="${(H * f).toFixed(0)}" vector-effect="non-scaling-stroke"/>`).join('')}
      <path class="fill" d="${area}"/><path class="line" d="${line('total')}" vector-effect="non-scaling-stroke"/><path class="line2" d="${line('auto')}" vector-effect="non-scaling-stroke"/>
      <circle class="dot" cx="${x(days.length - 1)}" cy="${y(last.total)}" r="3.5"/></svg>`;
  }
  function ring(parts) {
    const C = 2 * Math.PI * 54; const total = parts.reduce((n, p) => n + p.value, 0) || 1;
    let offset = 0;
    return `<svg viewBox="0 0 132 132" aria-hidden="true"><circle class="track-c" cx="66" cy="66" r="54"/>${parts.filter((p) => p.value).map((p) => {
      const len = (p.value / total) * C; const s = `<circle cx="66" cy="66" r="54" stroke="${p.color}" stroke-dasharray="${Math.max(len - 2, 0.5)} ${C}" stroke-dashoffset="${-offset}"/>`; offset += len; return s; }).join('')}</svg>`;
  }
  let overviewTimer;
  async function overviewPage(_args, _params, alive) {
    const [s, recent, queueRes] = await Promise.all([api(`/api/analytics/summary?scope=${_params.get('scope') === 'business' ? 'business' : 'all'}`), api('/api/runs?limit=8'), api('/api/runs?open=1&limit=200')]);
    if (!alive()) return;
    setCrumb('<b>Today</b>');
    const im = s.invoice_metrics;
    const scope = _params.get('scope') === 'business' ? 'business' : 'all';
    const queue = queueRes.filter(r => scope === "all" || r.event_type !== "dataset_evaluation").sort(byDue);
    const hour = new Date().getHours();
    const hello = hour < 12 ? 'Good morning' : hour < 17 ? 'Good afternoon' : 'Good evening';
    const name = reviewerName().split(/\s+/)[0];
    const nextDue = queue[0] ? due(queue[0]) : null;
    const brief = !s.documents
      ? 'Nothing has come in yet. Bring in an invoice — or run the scenario library to watch each kind of decision being made.'
      : `${queue.length ? `<b>${plural(queue.length, 'exception')}</b> ${queue.length === 1 ? 'needs' : 'need'} a decision${nextDue ? nextDue.cls === 'late' ? ` — one is <b>${esc(nextDue.text)}</b>` : `; the first is ${esc(nextDue.text)}` : ''}.` : '<b>No exceptions</b> are waiting on anyone.'} ${im.touchless} of ${plural(im.invoices, 'invoice')} went straight through without a person.`;
    const failed = s.by_status.FAILED?.count || 0;
    const outs = [
      { k: 'ok', label: 'Straight through', value: im.touchless },
      { k: 'rev', label: 'Approved after review', value: im.approved_by_reviewer },
      { k: 'warn', label: 'Waiting', value: im.pending },
      { k: 'bad', label: 'Rejected', value: im.rejected },
      { k: 'mute', label: 'Filed', value: im.non_invoices },
    ];
    const outTotal = Math.max(1, outs.reduce((n, o) => n + o.value, 0));
    const daily = new Map(s.daily.map((d) => [d.date, d]));
    const days = [...Array(14)].map((_, i) => { const d = new Date(); d.setUTCDate(d.getUTCDate() - (13 - i)); const key = d.toISOString().slice(0, 10); const v = daily.get(key) || { APPROVED: 0, PENDING_REVIEW: 0, REJECTED: 0 }; return { key, total: v.APPROVED + v.PENDING_REVIEW + v.REJECTED, auto: v.APPROVED }; });
    const stepTotal = s.step_seconds.reduce((n, x) => n + x.seconds, 0) || 1;
    const palette = ['#6a9e8a', '#f98963', '#ac90e1', '#80b068', '#e9c46a', '#ff98d9', '#7d8ff0', '#c5bfb5'];
    const steps = s.step_seconds.slice().sort((a, b) => b.seconds - a.seconds);
    const big = steps.slice(0, 5); const rest = steps.slice(5).reduce((n, x) => n + x.seconds, 0);
    const segments = [...big.map((x, i) => ({ label: x.stage, seconds: x.seconds, color: palette[i] })), ...(rest ? [{ label: `${steps.length - 5} other steps`, seconds: rest, color: palette[7] }] : [])];

    view.innerHTML = `
      <div class="overview-toolbar"><span class="kicker">Overview · visible records</span><label>Show <select id="overview-scope"><option value="all" ${scope === 'all' ? 'selected' : ''}>All documents, including dataset</option><option value="business" ${scope === 'business' ? 'selected' : ''}>Business invoices only</option></select></label><button class="btn btn-sm" id="refresh-overview">Refresh</button></div>
      <p class="motto"><svg viewBox="0 0 48 24" aria-hidden="true"><path d="M24 12C28 6 34 4.5 39 7.5C44 10.5 43.5 17.5 38 19.5C32.5 21.5 28 17 24 12C20 7 15.5 2.5 10 4.5C4.5 6.5 4 13.5 9 16.5C14 19.5 20 18 24 12Z" stroke="url(#infgrad)" stroke-width="3.6"/></svg><span>The potential is always infinite.</span></p>
      <section class="greeting"><div><span class="kicker">${esc(new Date().toLocaleDateString('en-IN', { weekday: 'long', day: 'numeric', month: 'long' }))}</span>
        <h1>${hello}${name ? `, ${esc(name)}` : ''}.</h1><p>${brief}</p></div>
        <div class="actions">${queue.length ? `<a class="btn btn-primary" href="#/review">Work the exceptions</a><a class="btn" href="#/upload">Bring in invoices</a>` : `<a class="btn btn-primary" href="#/upload">Bring in invoices</a>`}</div></section>

      <div class="overview-kpis"><a href="#/invoices"><small>Documents received</small><b>${s.documents}</b><small>${im.non_invoices} filed for reference</small></a><a href="#/invoices"><small>Approved</small><b class="ok">${im.approved}</b><small>${pct(s.touchless_rate)} handled automatically</small></a><a href="#/review"><small>Needs review</small><b class="warn">${im.pending}</b><small>Only blocking issues</small></a><a href="#/invoices"><small>Typical processing</small><b>${secs(s.processing_seconds.median)}</b><small>Current visible records</small></a></div>
      <section class="panel"><div class="panel-head"><div><h2>Document flow</h2><p>${s.dataset_documents ? `${s.dataset_documents} dataset runs included · excluded from the payment ledger` : "Current business invoices"}</p></div><a class="btn btn-sm btn-ghost" href="#/invoices">All invoices</a></div>
        <div class="flow">
          <div class="flow-step"><small>Received</small><b>${s.documents}</b><small>documents</small></div>
          <div class="flow-step"><small>Read</small><b>${s.documents - failed - (s.by_status.RUNNING?.count || 0)}</b><small>${s.by_status.RUNNING?.count ? `${s.by_status.RUNNING.count} still processing` : failed ? `${failed} could not be read` : 'completed reads'}</small></div>
          <div class="flow-step"><small>Checked</small><b>${s.decided + im.non_invoices}</b><small>against orders &amp; ledger</small></div>
          <div class="flow-out"><div class="split" role="img" aria-label="Outcome split">${outs.map((o) => o.value ? `<i class="${o.k}" style="width:${(o.value / outTotal) * 100}%" title="${o.label}: ${o.value}"></i>` : '').join('')}</div>
            <div class="keys">${outs.map((o) => `<span class="key"><i class="${o.k}"></i>${o.label} <b>${o.value}</b></span>`).join('')}</div></div>
        </div></section>

      <div class="cols cols-main mt">
        <div class="stack-lg">
          <section class="panel"><div class="panel-head"><div><h2>Volume</h2><p>Last 14 days · solid: everything received · dashed: went straight through</p></div></div>
            <div class="panel-body">${areaChart(days)}<div class="axis"><span>${esc(new Date(days[0].key).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }))}</span><span>Today · ${days[13].total}</span></div></div></section>
          <section class="panel"><div class="panel-head"><div><h2>Latest activity</h2><p>Newest first</p></div><a class="btn btn-sm btn-ghost" href="#/invoices">See all</a></div>
            ${recent.length ? `<div class="feed">${recent.map((r) => { const o = outcome(r); return `<a href="#/invoice/${esc(r.run_id)}?from=overview"><span class="glyph ${o.tone}">${o.g}</span><span><b>${esc(title(r))} · ${money(r.total)}</b><small>${esc(o.label)} · ${esc(nextStep(r))}${r.invoice_number ? ` · ${esc(r.invoice_number)}` : ''}</small></span><span class="when">${esc(ago(r.time))}</span></a>`; }).join('')}</div>`
              : '<div class="empty"><h3>Quiet so far</h3><p>Documents you bring in appear here as they are read and decided.</p><a class="btn" href="#/upload">Bring in invoices</a></div>'}</section>
        </div>
        <div class="stack-lg">
          <section class="panel panel-pad"><div class="ring-wrap"><div class="ring">${ring([
              { value: im.touchless, color: 'var(--ok-bar)' }, { value: im.approved_by_reviewer, color: '#9bd2b4' }, { value: im.pending, color: 'var(--warn-bar)' }, { value: im.rejected, color: 'var(--bad-bar)' }])}
              <div class="ring-label"><b>${pct(s.touchless_rate)}</b><small>hands-free</small></div></div>
            <div><h2 style="font-size:15.5px">Automation</h2><p class="muted small" style="margin:4px 0 10px">Share of decided invoices approved with nobody touching them.</p>
              <div class="keys" style="display:grid;gap:6px">${outs.slice(0, 4).map((o) => `<span class="key"><i class="${o.k}"></i>${o.label} <b>${o.value}</b></span>`).join('')}</div></div></div></section>
          <section class="panel"><div class="panel-head"><div><h2>Next up</h2><p>Closest deadline first</p></div>${queue.length ? `<a class="btn btn-sm btn-ghost" href="#/review">Open (${queue.length})</a>` : ''}</div>
            ${queue.length ? `<div class="next">${queue.slice(0, 4).map((r) => `<a href="#/review?id=${esc(r.run_id)}"><span class="row-between"><b>${esc(title(r))}</b>${dueChip(r)}</span><small class="muted">${esc(human((r.reasons || [])[0] || route(r)?.reason || ''))}</small></a>`).join('')}</div>`
              : '<div class="empty" style="padding:26px"><p>Nothing is waiting on a person.</p></div>'}</section>
          <section class="panel panel-pad"><h2 style="font-size:15.5px;margin-bottom:14px">Ledger</h2><div class="ledger">
            <div><span>Invoice value handled</span><b>${moneyShort(im.value)}</b></div>
            <div><span>Value held back</span><b>${moneyShort(s.money_stopped)}</b></div>
            <div><span>Hours returned to the team*</span><b>${s.time_saved_minutes ? (s.time_saved_minutes / 60).toFixed(1) : '0'}</b></div>
            <div><span>Typical time per document</span><b>${secs(s.processing_seconds.median)}</b></div></div>
            <p class="faint small" style="margin-top:12px">* assumes ${s.manual_minutes_per_invoice} minutes of manual work per hands-free invoice.</p></section>
        </div>
      </div>

      <div class="cols cols-2 mt">
        <section class="panel"><div class="panel-head"><div><h2>What stops invoices</h2><p><span class="key"><i class="warn"></i>sent to a person</span> &nbsp; <span class="key"><i class="bad"></i>rejected</span></p></div></div>
          <div class="panel-body">${s.stop_reasons.length ? `<div class="ranked">${s.stop_reasons.map((r) => `<div><span>${esc(r.label)}</span><span class="dots">${'<i class="w"></i>'.repeat(Math.min(r.review, 12))}${'<i class="b"></i>'.repeat(Math.min(r.rejected, 12))}</span><em>${r.review + r.rejected}</em></div>`).join('')}</div>` : '<p class="muted">Nothing has been stopped yet.</p>'}</div></section>
        <section class="panel"><div class="panel-head"><div><h2>Processing speed</h2><p>Current scope · latest 200 completed documents · overlapping readers measured separately</p></div></div>
          <div class="panel-body">${segments.length ? `<div class="steps-bar">${segments.map((g) => `<i style="width:${(g.seconds / stepTotal) * 100}%;background:${g.color}" title="${esc(g.label)}: ${secs(g.seconds)}"></i>`).join('')}</div>
            <div class="steps-key">${segments.map((g) => `<span><i style="background:${g.color}"></i>${esc(g.label)}<em>${secs(g.seconds)}</em></span>`).join('')}</div>` : '<p class="muted">No timings yet.</p>'}
          </div></section>
        <section class="panel"><div class="panel-head"><div><h2>Upcoming payments</h2><p>Approved invoices by due date</p></div></div><div class="panel-body"><div class="calendar">${s.aging.map((a) => `<div class="${a.bucket === 'Overdue' && a.count ? 'hot' : ''}"><small>${esc(a.bucket.replace('Due in ', ''))}</small><b>${a.count}</b><small>${moneyShort(a.value)}</small></div>`).join('')}</div></div></section>
      </div>`;
    $('#overview-scope').addEventListener('change', (event) => go(`#/?scope=${event.target.value}`));
    $('#refresh-overview').addEventListener('click', () => overviewPage(_args, _params, alive));
    if (overviewTimer) clearTimeout(overviewTimer);
    overviewTimer = setTimeout(() => { if (alive() && !document.hidden) overviewPage(_args, _params, alive); }, 15000);
  }

  /* ================= Bring in invoices ================= */
  const batch = [];
  function batchMarkup() {
    if (!batch.length) return '';
    const done = batch.filter((b) => b.run && b.run.status !== 'RUNNING').length;
    return `<section class="panel mt" id="batchcard"><div class="panel-head"><div><h2>In this session</h2><p>${done} of ${batch.length} finished</p></div><button class="btn btn-sm btn-ghost" id="clearbatch">Clear</button></div>
      <div style="padding:0 20px 12px"><div class="progress"><i style="width:${(done / batch.length) * 100}%"></i></div></div>
      ${batch.map((b) => `<div class="batch-row"><span><b style="font-weight:500">${esc(b.run?.vendor || b.name)}</b><br><small class="muted">${esc(b.name)}</small></span>${b.run ? tag(b.run) : '<span class="tag info"><i>◦</i>Queued</span>'}${b.run_id ? `<a class="btn btn-sm" href="#/invoice/${esc(b.run_id)}?from=upload">Open</a>` : '<span></span>'}</div>`).join('')}</section>`;
  }
  const repaintBatch = () => { const card = $('#batchcard'); if (card) card.outerHTML = batchMarkup(); else $('#dropzone')?.insertAdjacentHTML('afterend', batchMarkup()); const c = $('#clearbatch'); if (c) c.onclick = () => { batch.splice(0); $('#batchcard')?.remove(); }; };
  async function trackBatch(alive) {
    while (alive() && batch.some((b) => b.run_id && (!b.run || b.run.status === 'RUNNING'))) {
      await Promise.all(batch.filter((b) => b.run_id && (!b.run || b.run.status === 'RUNNING')).map(async (b) => { try { b.run = await api(`/api/runs/${b.run_id}`); } catch (_) {} }));
      if (alive()) repaintBatch();
      await new Promise((r) => setTimeout(r, 1200));
    }
    refreshCount();
  }
  async function uploadFiles(files, alive) {
    const valid = [...files].filter((f) => /\.(pdf|png|jpe?g)$/i.test(f.name) && f.size <= 8 * 1024 * 1024);
    if (files.length - valid.length) toast(`${plural(files.length - valid.length, 'file')} skipped — PDF, PNG or JPEG up to 8 MB`, { tone: 'bad' });
    if (!valid.length) return;
    try {
      if (valid.length === 1) {
        const form = new FormData(); form.append('invoice_file', valid[0]);
        const { run_id } = await api(`/api/runs?ocr=${$('#optional-ocr')?.checked ? 1 : 0}`, { method: 'POST', body: form });
        go(`#/invoice/${run_id}?from=upload`); return;
      }
      if (valid.length > 25) { toast('Up to 25 documents at a time', { tone: 'bad' }); return; }
      const form = new FormData(); valid.forEach((f) => form.append('invoice_files', f));
      const { runs } = await api(`/api/runs/bulk?ocr=${$('#optional-ocr')?.checked ? 1 : 0}`, { method: 'POST', body: form });
      runs.forEach((r) => batch.unshift({ name: r.source, run_id: r.run_id }));
      toast(`${plural(runs.length, 'document')} on their way`);
      repaintBatch(); trackBatch(alive);
    } catch (err) { toast(err.message, { tone: 'bad' }); }
  }
  const EXPECT_TONE = (t) => (/^Approved/.test(t) ? 'ok' : /^Rejected/.test(t) ? 'bad' : /^Filed/.test(t) ? '' : 'warn');
  async function uploadPage(_args, _params, alive) {
    const demos = await api('/api/demo').catch(() => []);
    if (!alive()) return;
    setCrumb('<b>Bring in invoices</b>');
    const samples = APP.samples || [];
    view.innerHTML = `
      ${intro('Bring in invoices', 'PDFs, scans or phone photos. A single file opens its live view; several files run together and are tracked below.')}
      <label class="row small" style="margin-bottom:16px"><input type="checkbox" id="optional-ocr"> Also run OCR (optional; slower on scans). AI reads images by default.</label>
      <div class="intake">
        <div>
          <label class="drop" id="dropzone" tabindex="0">
            <span class="icon"><svg viewBox="0 0 24 24"><path d="M12 15V4M7.5 8.5 12 4l4.5 4.5M5 14v5a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5"/></svg></span>
            <h2>Drop files here</h2><p class="muted">or click to browse · up to 25 files · 8 MB each</p>
            <span class="row" style="justify-content:center"><span class="chip">PDF</span><span class="chip">PNG</span><span class="chip">JPG</span></span>
            <input type="file" id="fileinput" accept=".pdf,.png,.jpg,.jpeg" multiple hidden>
          </label>
          ${batchMarkup()}
        </div>
        <div class="stack-lg">
          <section class="panel"><div class="panel-head"><div><h2>Scenario library</h2><p>Fictional documents that each trigger a different rule. Order matters — later ones build on earlier approvals.</p></div>
            ${demos.length ? '<button class="btn btn-sm" id="runall">Run all</button>' : ''}</div>
            <div class="scenarios">${demos.map((d, i) => `<div class="scenario"><span class="n">${String(i + 1).padStart(2, '0')}</span>
              <div><b>${esc(d.title)}</b> <span class="tag ${EXPECT_TONE(d.expect)}" style="height:20px;font-size:11.5px;margin-left:4px">${esc(d.expect.split('.')[0])}</span><p>${esc(d.expect.split('.').slice(1).join('.').trim())}</p></div>
              <span class="acts"><a class="btn btn-sm btn-ghost" href="/api/demo/${encodeURIComponent(d.file)}" target="_blank" rel="noopener" aria-label="View ${esc(d.title)} file">View</a><button class="btn btn-sm btn-primary" data-demo="${esc(d.file)}">Run</button></span></div>`).join('') || '<div class="empty"><p>No scenario files in samples/demo.</p></div>'}</div></section>
          ${samples.length ? `<section class="panel panel-pad"><h2 style="font-size:15.5px">Real invoice photos</h2><p class="muted small" style="margin:4px 0 12px">Indian GST bills from a public dataset — a tougher test of reading. Reference labels are only used afterwards, to score accuracy.</p>
            <div class="row"><select id="datasetpick" style="flex:1;min-width:180px">${samples.map(([i, n]) => `<option value="${i}">${esc(n)}</option>`).join('')}</select><button class="btn" id="rundataset">Run photo</button></div></section>` : ''}
        </div>
      </div>`;
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
          for (let i = 0; i < 300; i++) { entry.run = await api(`/api/runs/${entry.run_id}`); if (entry.run.status !== 'RUNNING') break; await new Promise((r) => setTimeout(r, 500)); }
        } catch (err) { toast(err.message, { tone: 'bad' }); }
        if (!alive()) return;
        repaintBatch();
      }
      refreshCount();
      toast('Scenario library finished', { action: { href: '#/', label: 'See Today →' } });
    });
    $('#rundataset')?.addEventListener('click', async () => {
      try { const { run_id } = await api(`/api/samples/${$('#datasetpick').value}?ocr=${$('#optional-ocr')?.checked ? 1 : 0}`, { method: 'POST' }); go(`#/invoice/${run_id}?from=upload`); }
      catch (err) { toast(err.message, { tone: 'bad' }); }
    });
    repaintBatch();
    if (batch.some((b) => b.run_id && (!b.run || b.run.status === 'RUNNING'))) trackBatch(alive);
  }

  /* ================= Exceptions (two-pane inbox) ================= */
  async function reviewPage(_args, params, alive) {
    const [rows, m] = await Promise.all([api('/api/runs?open=1&limit=500'), meta()]);
    if (!alive()) return;
    setCrumb('<b>Exceptions</b>');
    let team = params.get('team') || '';
    const counts = Object.fromEntries(m.teams.map((t) => [t.team, rows.filter((r) => route(r)?.team === t.team).length]));
    const late = rows.filter((r) => due(r)?.cls === 'late').length;
    view.innerHTML = `
      ${intro(rows.length ? `${plural(rows.length, 'exception')} to decide` : 'Exceptions', rows.length ? `Invoices the rules could not settle alone, routed to the team best placed to resolve them.${late ? ` <b style="color:var(--bad)">${late} past due.</b>` : ''}` : 'Nothing is waiting. Every invoice either passed its checks or has been decided by a person.')}
      <section class="panel inbox">
        <div class="inbox-list">
          <div class="inbox-tools"><select id="teamfilter" aria-label="Team"><option value="">All teams · ${rows.length}</option>${m.teams.map((t) => `<option value="${esc(t.team)}" ${team === t.team ? 'selected' : ''}>${esc(t.team)} · ${counts[t.team]}</option>`).join('')}</select></div>
          <div class="inbox-items" id="items" role="listbox" aria-label="Exceptions"></div>
        </div>
        <div class="preview" id="preview"></div>
      </section>
      <p class="hintline mt"><kbd>↑</kbd><kbd>↓</kbd> move between exceptions · <kbd>Enter</kbd> open the invoice</p>`;
    let shown = [];
    let selected = params.get('id');
    const narrow = () => matchMedia('(max-width: 980px)').matches;
    const paint = () => {
      shown = rows.filter((r) => !team || route(r)?.team === team).sort(byDue);
      if (!shown.some((r) => r.run_id === selected)) selected = shown[0]?.run_id;
      $('#items').innerHTML = shown.map((r) => `<button class="inbox-item" role="option" data-id="${esc(r.run_id)}" aria-selected="${r.run_id === selected}">
          <b>${esc(title(r))}</b><span class="amt">${money(r.total)}</span>
          <small>${esc(human((r.reasons || [])[0] || route(r)?.reason || 'Needs a person'))}</small>
          <span class="meta"><span class="chip">${esc(route(r)?.team || '')}</span>${dueChip(r)}</span></button>`).join('')
        || `<div class="empty"><h3>Clear</h3><p>${team ? `Nothing waiting for ${esc(team)}.` : 'No exceptions right now.'}</p><a class="btn" href="#/upload">Bring in invoices</a></div>`;
      $$('.inbox-item').forEach((b) => b.addEventListener('click', () => { if (narrow()) go(`#/invoice/${b.dataset.id}?from=review`); else { selected = b.dataset.id; paint(); } }));
      const r = shown.find((x) => x.run_id === selected);
      const rt = r && route(r);
      const handles = rt && m.teams.find((t) => t.team === rt.team)?.handles;
      $('#preview').innerHTML = r ? `
        <div class="row">${tag(r)}<span class="chip">${esc(rt?.team || '')}</span>${dueChip(r)}</div>
        <div><h2>${esc(title(r))}</h2><p class="muted small" style="margin-top:4px">${esc(r.source)}</p></div>
        <div class="facts"><div><small>Invoice</small><b>${dash(r.invoice_number)}</b></div><div><small>Purchase order</small><b>${dash(r.po_number)}</b></div><div><small>Amount</small><b>${money(r.total)}</b></div></div>
        <div><p class="sublabel" style="margin-top:0">Why it is here</p><ul class="reasons">${(r.reasons || []).map((x) => `<li>${esc(human(x))}</li>`).join('') || '<li>Needs a person to confirm</li>'}</ul></div>
        ${handles ? `<p class="muted small">${esc(rt.team)} handles ${esc(handles.toLowerCase())}. Deadline ${rt.sla_hours} hours from arrival · received ${esc(ago(r.time))}.</p>` : ''}
        <div class="row"><a class="btn btn-primary" href="#/invoice/${esc(r.run_id)}?from=review">Open and decide</a></div>`
        : '<div class="empty"><p>Select an exception to preview it.</p></div>';
      $('.inbox-item[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
    };
    $('#teamfilter').addEventListener('change', (e) => { team = e.target.value; history.replaceState(null, '', team ? `#/review?team=${encodeURIComponent(team)}` : '#/review'); paint(); });
    const keys = (e) => {
      if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName) || $('#palette').open) return;
      const i = shown.findIndex((r) => r.run_id === selected);
      if ((e.key === 'ArrowDown' || e.key === 'j') && i < shown.length - 1) { e.preventDefault(); selected = shown[i + 1].run_id; paint(); }
      if ((e.key === 'ArrowUp' || e.key === 'k') && i > 0) { e.preventDefault(); selected = shown[i - 1].run_id; paint(); }
      if (e.key === 'Enter' && selected && !e.target.closest('a')) { e.preventDefault(); go(`#/invoice/${selected}?from=review`); }
    };
    document.addEventListener('keydown', keys); onLeave(() => document.removeEventListener('keydown', keys));
    paint();
  }

  /* ================= All invoices ================= */
  async function invoicesPage(_args, params, alive) {
    setCrumb('<b>All invoices</b>');
    const state = { status: params.get('status') || '', q: params.get('q') || '', date: params.get('date') || '', offset: 0, rows: [] };
    const options = [['', 'Every result'], ['APPROVED', 'Approved'], ['PENDING_REVIEW', 'Exceptions'], ['REJECTED', 'Rejected'], ['FAILED', 'Failed'], ['RUNNING', 'Still reading']];
    view.innerHTML = `
      ${intro('All invoices', 'Every document and what was decided about it. Open a row for the source, the evidence and the people involved.',
        `<a class="btn" href="/api/runs/export.xlsx" download>Export Excel</a><a class="btn" href="/api/runs/export.csv" download>CSV</a><button class="btn btn-ghost" id="archive" title="Hide finished invoices here. Duplicate protection and PO balances still count them.">Archive finished</button>`)}
      <section class="panel">
        <div class="filters">
          <div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg><input type="search" id="q" placeholder="Supplier, invoice, PO, GSTIN" value="${esc(state.q)}" aria-label="Search invoices"></div>
          <select id="status" aria-label="Result">${options.map(([v, l]) => `<option value="${v}" ${state.status === v ? 'selected' : ''}>${l}</option>`).join('')}</select>
          <input type="date" id="date" value="${esc(state.date)}" aria-label="Received on">
          <span class="count" id="count"></span>
        </div>
        <div id="tablearea"></div><div class="row-end" style="padding:12px 18px" id="morearea"></div>
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
      $('#count').textContent = state.q ? plural(state.rows.length, 'match', 'matches') : `${state.rows.length} of ${total}`;
      let lastDay = '';
      const body = state.rows.map((r) => {
        const d = dayLabel(r.time); const head = d !== lastDay ? `<tr class="group"><td colspan="6">${esc(d)}</td></tr>` : ''; lastDay = d;
        return `${head}<tr class="go" data-href="#/invoice/${esc(r.run_id)}?from=invoices" tabindex="0"><td class="stripe"><i class="${outcome(r).tone}"></i></td>
          <td><div class="c-main">${esc(title(r))}</div><div class="c-sub">${dash(r.invoice_number)}${r.po_number ? ` · ${esc(r.po_number)}` : ''}</div></td>
          <td class="r num">${money(r.total)}</td><td>${tag(r)}</td><td class="muted small">${esc(nextStep(r))} ${dueChip(r)}</td><td class="r muted small">${esc(clock(r.time))}</td></tr>`;
      }).join('');
      $('#tablearea').innerHTML = state.rows.length ? `<div class="table-wrap"><table><thead><tr><th></th><th>Supplier</th><th class="r">Amount</th><th>Result</th><th>Next</th><th class="r">Received</th></tr></thead><tbody>${body}</tbody></table></div>`
        : `<div class="empty"><h3>No invoices match</h3><p>${state.q || state.status || state.date ? 'Try a broader search.' : 'Bring in your first invoice to get started.'}</p>${state.q || state.status || state.date ? '<a class="btn" href="#/invoices">Clear filters</a>' : '<a class="btn btn-primary" href="#/upload">Bring in invoices</a>'}</div>`;
      $('#morearea').innerHTML = rows.length === LIMIT ? '<button class="btn" id="more">Show more</button>' : '';
      $('#more')?.addEventListener('click', () => load(false));
      wireRows($('#tablearea'));
    }
    const sync = () => { const qs = new URLSearchParams(); ['status', 'q', 'date'].forEach((k) => state[k] && qs.set(k, state[k])); history.replaceState(null, '', `#/invoices${qs.toString() ? '?' + qs : ''}`); };
    $('#status').addEventListener('change', (e) => { state.status = e.target.value; sync(); load(true); });
    let timer; $('#q').addEventListener('input', (e) => { clearTimeout(timer); timer = setTimeout(() => { state.q = e.target.value.trim(); sync(); load(true); }, 250); });
    $('#date').addEventListener('change', (e) => { state.date = e.target.value; sync(); load(true); });
    $('#archive').addEventListener('click', async () => {
      if (!confirm('Archive finished invoices? They leave this list, but duplicate protection and PO balances still count them.')) return;
      try { const r = await api('/api/history/clear', { method: 'POST' }); toast(`${plural(r.archived, 'invoice')} archived`); load(true); }
      catch (err) { toast(err.message, { tone: 'bad' }); }
    });
    await load(true);
  }

  /* ================= Invoice ================= */
  const RELIABILITY = { DIRECT_LABEL: 'next to its label', AI_CORROBORATED: 'AI, quoted from the page', CONTEXTUAL: 'inferred from layout', MANUALLY_VERIFIED: 'corrected by a person', NONE: 'no evidence' };
  const FIELDS = [['vendor', 'Supplier'], ['invoice_number', 'Invoice number'], ['date', 'Invoice date'], ['po_number', 'Purchase order'], ['gstin', 'Supplier GSTIN'], ['subtotal', 'Subtotal'], ['tax', 'Tax'], ['total', 'Total']];
  const MONEY_FIELDS = new Set(['subtotal', 'tax', 'total', 'discount']);
  const METHOD = { pdf_text: 'PDF text', 'pdf+ocr': 'PDF scan (OCR)', ocr: 'photo / image (OCR)', text: 'plain text' };

  function supplierNote(r) {
    const inv = r.invoice || {};
    const reasons = r.resolution === 'REJECTED' ? [r.resolution_detail?.note] : (r.reasons || []);
    const who = r.resolution_detail?.reviewer || reviewerName() || 'Accounts Payable';
    return `Subject: Invoice ${inv.invoice_number || r.invoice_number || ''} — action needed\n\nHello ${r.vendor || 'team'},\n\nThank you for invoice ${inv.invoice_number || r.invoice_number || '(no number found)'}${inv.date ? ` dated ${inv.date}` : ''} for ${money(r.total)}${r.po_number ? `, against purchase order ${r.po_number}` : ''}. We are unable to process it as it stands:\n\n${reasons.filter(Boolean).map((x) => `• ${human(x)}`).join('\n') || '• Please see the note from our accounts team.'}\n\nCould you send a corrected invoice or the missing details? Quote the invoice number in your reply so we can match it quickly.\n\nThanks,\n${who}\nAccounts Payable`;
  }

  async function invoicePage([rid], params, alive) {
    const m = await meta();
    let run;
    try { run = await api(`/api/runs/${rid}`); }
    catch (err) {
      if (err.status === 404) { view.innerHTML = '<div class="panel empty"><h3>Invoice not found</h3><p>The link may be out of date.</p><a class="btn" href="#/invoices">All invoices</a></div>'; return; }
      throw err;
    }
    if (!alive()) return;
    const from = params.get('from') || 'invoices';
    const back = { review: ['#/review', 'Exceptions'], upload: ['#/upload', 'Bring in invoices'], overview: ['#/', 'Today'], invoices: ['#/invoices', 'All invoices'], vendors: ['#/vendors', 'Suppliers'] }[from] || ['#/invoices', 'All invoices'];
    let tab = params.get('tab') || 'summary';
    let editing = false;
    let pending = {};
    view.innerHTML = `<div id="head"></div><div class="inv-grid"><section class="panel doc" id="doc"></section><section class="panel" id="side"></section></div><div id="dockarea"></div>`;
    if (run.document_id) {
      const url = `/api/runs/${rid}/document`;
      $('#doc').innerHTML = `<div class="bar"><span>${esc(run.source)}</span><a href="${url}" target="_blank" rel="noopener">Open in new tab ↗</a></div>${/\.pdf$/i.test(run.source || '') ? `<iframe src="${url}" title="Invoice document"></iframe>` : `<img src="${url}" alt="Invoice document">`}`;
    } else $('#doc').innerHTML = '<div class="empty"><p>No source file stored for this record.</p></div>';

    const corrections = () => {
      const out = {};
      $$('[data-field]').forEach((input) => {
        const k = input.dataset.field; const original = run.invoice?.[k]; const v = input.value.trim();
        if (MONEY_FIELDS.has(k)) { if (v !== '' && Number(v.replace(/,/g, '')) !== Number(original)) out[k] = Number(v.replace(/,/g, '')); }
        else if (v !== '' && v !== String(original ?? '')) out[k] = v;
      });
      return out;
    };
    const restore = () => Object.entries(pending).forEach(([k, v]) => { const el = $(`[data-field="${k}"]`); if (el) el.value = v; });

    const draw = (r) => {
      run = r;
      const inv = r.invoice || {};
      const rt = route(r);
      const reasons = r.reasons || [];
      const latest = {}; (r.stages || []).forEach((s) => (latest[s.stage] = s));
      const done = Object.values(latest).filter((s) => ['COMPLETED', 'BLOCKED', 'SKIPPED', 'FAILED'].includes(s.status)).length;
      const totalSteps = m.stages.length || 13;
      const canDecide = r.status === 'PENDING_REVIEW' && !r.resolution && !NON_INVOICE(r);
      const noteValue = $('#note')?.value || '';
      setCrumb(`<a href="${back[0]}">${back[1]}</a><span>/</span><b>${esc(r.invoice_number || r.source)}</b>`);
      $('#head').innerHTML = `<div class="inv-head"><div><div class="row">${tag(r)}${rt && canDecide ? `<span class="chip">${esc(rt.team)}</span>${dueChip(r)}` : ''}</div>
          <h1>${esc(title(r))}</h1><p class="sub">Invoice ${dash(r.invoice_number)} · PO ${dash(r.po_number)} · dated ${dash(inv.date)} · read from ${esc(METHOD[r.method] || r.method || '—')} · received ${esc(ago(r.time))}</p></div>
        <div class="inv-amount"><b>${money(r.total)}</b><small>${inv.tax != null ? `includes ${money(inv.tax)} tax` : 'total'}</small></div></div>`;

      const checks = r.decision?.checks || [];
      const acknowledgements = {};
      for (const review of r.reviews || []) {
        if (['ISSUE_ACKNOWLEDGED', 'ISSUE_REOPENED'].includes(review.outcome)) acknowledgements[review.note] = review.outcome === 'ISSUE_ACKNOWLEDGED';
      }
      const issueTick = (c) => `<div class="issue-actions"><label><input type="checkbox" data-issue="${esc(c.check)}" ${acknowledgements[c.check] ? 'checked' : ''}> ${acknowledgements[c.check] ? 'Acknowledged' : 'Mark reviewed'}</label>${['vendor_master','vendor_identity','payee_identified'].includes(c.check) && inv.vendor ? '<button class="btn btn-sm btn-ghost" data-verify-vendor>Verify vendor &amp; rerun</button>' : ''}</div>`;
      const issueItem = (c) => `<li class="issue-item"><div class="issue-copy"><b>${esc(m.checks[c.check] || c.check.replaceAll('_', ' '))}</b><p>${esc(human(c.detail))}</p></div>${issueTick(c)}</li>`;
      const blocking = checks.filter(c => !c.passed && c.outcome !== 'ADVISORY');
      const advisory = checks.filter(c => c.outcome === 'ADVISORY');
      let verdict;
      if (r.status === 'RUNNING') {
        const current = m.stages.find((n) => latest[n]?.status === 'RUNNING') || 'Starting';
        verdict = `<div class="verdict info"><h3>Reading now — ${esc(current.toLowerCase())}</h3><p>Step ${Math.min(done + 1, totalSteps)} of ${totalSteps}. This page fills in as each check finishes.</p><div class="progress" style="margin-top:6px"><i style="width:${(done / totalSteps) * 100}%"></i></div></div>`;
      } else if (r.status === 'FAILED') {
        verdict = `<div class="verdict bad"><h3>This document could not be processed</h3><p>${esc(r.error || '')}</p><div class="row" style="margin-top:6px"><button class="btn btn-sm" data-retry>Retry now</button><a class="btn btn-sm" href="#/upload">Try another file</a></div></div>`;
      } else if (NON_INVOICE(r)) {
        verdict = `<div class="verdict"><h3>Kept for reference, not for payment</h3><p>${esc(r.document_routing?.reason || '')}. It stays out of approval figures.</p></div>`;
      } else if (r.resolution) {
        const d = r.resolution_detail || {}; const okd = r.resolution === 'APPROVED';
        verdict = `<div class="verdict ${okd ? 'ok' : 'bad'}"><h3>${okd ? 'Approved' : 'Rejected'} by ${esc(d.reviewer || 'a reviewer')}</h3>${d.note ? `<p>“${esc(d.note)}”</p>` : ''}<p class="muted small">${esc(stamp(d.at))}. The automatic check had flagged: ${esc(human(reasons.slice(0, 2).join('; ')))}.</p>
          ${okd ? '' : '<div class="row" style="margin-top:6px"><button class="btn btn-sm" data-email>Write to supplier</button></div>'}</div>`;
      } else if (r.status === 'APPROVED') {
        const advisory = checks.filter((c) => c.outcome === 'ADVISORY');
        verdict = `<div class="verdict ok"><h3>Cleared automatically</h3><p>No blocking payment risks found. Advisory issues are listed below.</p>${advisory.length ? `<p>Reported for your records; no action required:</p><ul>${advisory.map((c) => `<li>${esc(c.detail)} ${issueTick(c)}</li>`).join('')}</ul>` : ''}</div>`;
      } else if (r.status === 'REJECTED') {
        verdict = `<div class="verdict bad"><h3>Held back — not payable as sent</h3><ul class="reasons" style="margin-top:4px">${reasons.map((x) => `<li>${esc(human(x))}${checks.find((c) => c.detail === x) ? issueTick(checks.find((c) => c.detail === x)) : ''}</li>`).join('')}</ul><div class="row" style="margin-top:8px"><button class="btn btn-sm" data-email>Write to supplier</button></div></div>`;
      } else {
        verdict = `<div class="verdict review-summary"><div class="row-between"><div><span class="kicker">Needs review</span><h3>${plural(blocking.length, 'blocking issue')}</h3><p>${esc(rt?.team || 'AP Specialist')}${due(r) ? ` · ${esc(due(r).text)}` : ''}</p></div><button class="btn btn-sm" data-retry>Rerun invoice</button></div><ul class="issue-list">${blocking.map(issueItem).join('')}</ul></div>${advisory.length ? `<details class="advisory-list"><summary>${plural(advisory.length, 'advisory')} · does not block approval</summary><ul class="issue-list">${advisory.map(issueItem).join('')}</ul></details>` : ''}`;
      }
      const issues = checks.filter((c) => c.outcome !== 'VERIFIED_PASS');
      const passed = checks.filter((c) => c.outcome === 'VERIFIED_PASS');
      const checkRow = (c) => { const t = c.outcome === 'VERIFIED_PASS' ? 'ok' : c.outcome === 'CONFIRMED_VIOLATION' ? 'bad' : 'warn';
        return `<div class="check"><span class="g ${t}">${t === 'ok' ? '✓' : t === 'bad' ? '×' : '!'}</span><div><b>${esc(m.checks[c.check] || c.check)}</b><small>${esc(human(c.detail))}</small>${c.outcome !== 'VERIFIED_PASS' ? issueTick(c) : ''}</div></div>`; };
      const flowNode = (name) => {
        const st = latest[name] || { status: 'PENDING', detail: 'Waiting to start' };
        const done = ['COMPLETED', 'SKIPPED'].includes(st.status);
        const symbol = st.status === 'COMPLETED' ? '✓' : st.status === 'RUNNING' ? '◉' : st.status === 'SKIPPED' ? '↪' : ['BLOCKED', 'FAILED'].includes(st.status) ? '!' : '○';
        return `<div class="flow-step ${esc(st.status)}"><div class="flow-edge" role="progressbar" aria-label="${esc(name)}: ${esc(st.status.toLowerCase())}" ${done ? 'aria-valuenow="100" aria-valuemin="0" aria-valuemax="100"' : st.status === 'PENDING' ? 'aria-valuenow="0" aria-valuemin="0" aria-valuemax="100"' : ''}><i></i></div><div class="flow-card"><span class="flow-symbol">${symbol}</span><div><b>${esc(name)}</b><span class="flow-state">${esc(st.status.toLowerCase().replace('_', ' '))}</span><small>${esc(human(st.detail || 'Waiting to start'))}</small></div></div></div>`;
      };
      const readerStages = ['OCR reader', 'AI image reader'];
      const graphTimeline = `<div class="flow-graph" aria-label="Document processing flow">${flowNode('Document received')}<div class="flow-caption">Read in parallel · first usable result continues</div><div class="flow-branches">${readerStages.map(flowNode).join('')}</div><div class="flow-caption">↓ Results join → bill checks</div>${m.stages.filter((n) => n !== 'Document received' && !readerStages.includes(n)).map(flowNode).join('')}</div>`;
      const fm = inv.field_metadata || {};
      const t0 = r.stages?.[0]?.at ? new Date(r.stages[0].at).getTime() : 0;
      const outcomeWord = { APPROVED_BY_REVIEWER: 'Approved', REJECTED_BY_REVIEWER: 'Rejected', VERIFIED: 'Note', NEEDS_CORRECTION: 'Correction' };
      const tabs = [['summary', 'Summary'], ['checks', 'Checks', issues.length], ['extracted', 'Extracted'], ['timeline', 'Timeline'], ['activity', 'Activity']];
      const bodies = {
        summary: `${verdict}
          <dl class="facts-list"><dt>Supplier</dt><dd>${dash(inv.vendor || r.vendor)}</dd><dt>GSTIN</dt><dd class="mono">${dash(inv.gstin)}</dd><dt>Invoice</dt><dd>${dash(r.invoice_number)}</dd><dt>Dated</dt><dd>${dash(inv.date)}</dd>${inv.details?.due_date ? `<dt>Due</dt><dd>${esc(inv.details.due_date)}</dd>` : ''}
            <dt>Purchase order</dt><dd>${dash(r.po_number)}${r.decision?.po_matching?.status ? ` <span class="muted small">· ${esc(r.decision.po_matching.status.toLowerCase().replace(/_/g, ' '))}</span>` : ''}</dd><dt>Subtotal · tax</dt><dd>${money(inv.subtotal)} · ${money(inv.tax)}</dd><dt>Checks</dt><dd>${checks.length ? `${passed.length} passed${issues.length ? `, <button class="link" data-tab="checks">${issues.length} flagged</button>` : ''}` : '—'}</dd></dl>
          ${r.ai_review ? `<p class="sublabel">Second opinion (advisory)</p><p class="small">${esc(human(r.ai_review.summary))}</p>` : ''}`,
        checks: checks.length ? `${issues.length ? `<p class="sublabel" style="margin-top:0">Flagged</p>${issues.map(checkRow).join('')}` : ''}${passed.length ? `<p class="sublabel"${issues.length ? '' : ' style="margin-top:0"'}>Passed</p>${passed.map(checkRow).join('')}` : ''}` : '<p class="muted">Checks run once the document has been read.</p>',
        extracted: `${editing ? '<p class="small" style="margin-bottom:10px">Edit any value that was misread. Corrections are re-checked when you approve.</p>' : ''}<table class="extracted"><tbody>${FIELDS.map(([k, l]) => {
            const v = inv[k]; const mt = fm[k] || {};
            const value = editing ? `<input data-field="${k}" value="${esc(v ?? '')}" ${MONEY_FIELDS.has(k) ? 'inputmode="decimal"' : ''} aria-label="${l}">` : (MONEY_FIELDS.has(k) ? money(v) : dash(v));
            return `<tr><td>${l}</td><td>${value}${!editing && v != null ? `<div class="ev">${esc(RELIABILITY[mt.reliability] || 'no evidence')}${mt.evidence ? ` — “${esc(String(mt.evidence).slice(0, 120))}”` : ''}</div>` : ''}</td></tr>`; }).join('')}</tbody></table>
          ${(inv.line_items || []).length && !editing ? `<p class="sublabel">${plural(inv.line_items.length, 'line')}</p><ul class="small" style="margin:0;padding-left:18px">${inv.line_items.map((li) => `<li>${esc(li.description)}</li>`).join('')}</ul>` : ''}`,
        timeline: `${graphTimeline}${r.timings ? `<p class="muted small">Total ${secs(r.timings.total_seconds)}</p>` : ''}`,
        activity: `<div class="log">${(r.reviews || []).slice().reverse().map((h) => `<div><b style="font-weight:500">${esc(outcomeWord[h.outcome] || h.outcome)}</b> · ${esc(h.reviewer)} <span class="muted">· ${esc(stamp(h.at))}</span>${h.note ? `<p class="muted">${esc(h.note)}</p>` : ''}</div>`).join('') || '<div class="muted">No one has acted on this invoice yet. The automatic decision was recorded when it arrived.</div>'}</div>
          <div class="row mt"><a class="btn btn-sm" href="/api/runs/${esc(r.run_id)}/audit.json" download>Download audit record</a>${/REJECTED/.test(r.resolution || r.status) && !NON_INVOICE(r) ? '<button class="btn btn-sm" data-email>Write to supplier</button>' : ''}</div>`,
      };
      $('#side').innerHTML = `<div class="tabs" role="tablist">${tabs.map(([k, l, n]) => `<button role="tab" data-tab="${k}" aria-selected="${tab === k}">${l}${n ? ` <em>${n}</em>` : ''}</button>`).join('')}</div><div class="tab-body" role="tabpanel">${bodies[tab]}</div>`;
      $('#dockarea').innerHTML = canDecide ? `<div class="dock" role="region" aria-label="Decision">
          <input id="note" placeholder="Add a note — required to reject; it goes to the supplier" aria-label="Note" value="${esc(noteValue)}">
          <div class="dock-acts"><button class="btn" data-act="CORRECT">${editing ? 'Done editing' : 'Correct details'}</button><button class="btn" data-act="NOTE">Save note</button><button class="btn btn-danger" data-act="REJECTED">Reject</button><button class="btn btn-primary" data-act="APPROVED">Approve</button></div>
          <p class="err" id="decideerror" role="alert"></p></div>` : '';
      restore();
      $$('[data-tab]').forEach((b) => b.addEventListener('click', () => { if (editing) pending = { ...pending, ...corrections() }; tab = b.dataset.tab; draw(run); }));
      $$('[data-email]').forEach((b) => b.addEventListener('click', () => { $('#emailbody').value = supplierNote(run); $('#emaildialog').showModal(); }));
      $$('[data-retry], [data-verify-vendor]').forEach((button) => button.addEventListener('click', async () => {
        const reviewer = await ensureName(); if (!reviewer) return;
        button.disabled = true;
        try {
          const result = await api(`/api/runs/${rid}/retry`, {method:'POST', json:{reviewer, verify_vendor:button.hasAttribute('data-verify-vendor')}});
          go(`#/invoice/${result.run_id}?from=upload`);
        } catch (error) { toast(error.message, {tone:'bad'}); button.disabled = false; }
      }));
      $$('[data-issue]').forEach((box) => box.addEventListener('change', async () => {
        const acknowledged = box.checked;
        box.disabled = true;
        try {
          const reviewer = await ensureName();
          if (!reviewer) { box.checked = !acknowledged; return; }
          await api(`/api/runs/${rid}/issues/${encodeURIComponent(box.dataset.issue)}`, { method: 'POST', json: { reviewer, acknowledged } });
          run = await api(`/api/runs/${rid}`); draw(run);
        } catch (err) { box.checked = !acknowledged; toast(err.message, { tone: 'bad' }); }
        finally { box.disabled = false; }
      }));
      $$('[data-act]').forEach((b) => b.addEventListener('click', () => act(b.dataset.act)));
    };

    async function act(kind) {
      const err = $('#decideerror'); err.textContent = '';
      if (kind === 'CORRECT') { if (editing) pending = { ...pending, ...corrections() }; editing = !editing; tab = 'extracted'; draw(run); return; }
      const reviewer = await ensureName(); if (!reviewer) { err.textContent = 'Add your name first so the decision is attributed.'; return; }
      const note = $('#note').value.trim();
      const fixes = editing ? { ...pending, ...corrections() } : pending;
      if (Object.values(fixes).some((v) => typeof v === 'number' && isNaN(v))) { err.textContent = 'Amounts must be numbers.'; return; }
      if (kind === 'REJECTED' && !note) { err.textContent = 'Say why in the note — it is what the supplier will read.'; $('#note').focus(); return; }
      $$('[data-act]').forEach((x) => (x.disabled = true));
      try {
        if (kind === 'NOTE') {
          if (!note && !Object.keys(fixes).length) { err.textContent = 'Write a note or correct a value first.'; return; }
          const res = await api(`/api/runs/${rid}/reviews`, { method: 'POST', json: { reviewer, outcome: Object.keys(fixes).length ? 'NEEDS_CORRECTION' : 'VERIFIED', note: note || 'Corrected extracted details', corrections: fixes } });
          toast(res.revised_review_result ? `Saved. With your corrections the checks would give: ${outcome({ status: res.revised_review_result.status }).label.toLowerCase()}` : 'Note saved');
          editing = false; pending = {}; tab = 'activity'; $('#note').value = ''; draw(await api(`/api/runs/${rid}`)); return;
        }
        await api(`/api/runs/${rid}/resolve`, { method: 'POST', json: { action: kind, reviewer, note, corrections: fixes } });
        refreshCount(); editing = false; pending = {};
        const next = (await api('/api/runs?open=1&limit=200')).filter((x) => x.run_id !== rid).sort(byDue)[0];
        toast(kind === 'APPROVED' ? 'Approved — ready for payment' : 'Rejected — supplier note drafted', next ? { action: { href: `#/invoice/${next.run_id}?from=review`, label: 'Next exception →' } } : {});
        if (from === 'review' && next) go(`#/invoice/${next.run_id}?from=review`);
        else { tab = 'summary'; draw(await api(`/api/runs/${rid}`)); }
      } catch (e) { const el = $('#decideerror'); if (el) el.textContent = e.message; }
      finally { $$('[data-act]').forEach((x) => (x.disabled = false)); }
    }
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

  /* ================= Suppliers ================= */
  async function vendorsPage(_args, _params, alive) {
    const { vendors, source } = await api('/api/vendors');
    if (!alive()) return;
    setCrumb('<b>Suppliers</b>');
    const flagged = vendors.filter((v) => v.attention.length).length;
    view.innerHTML = `
      ${intro('Suppliers', `Everyone on the supplier list and everyone who has sent an invoice, with their standing and how much purchase-order headroom is left.${source === 'file' ? ' Using the bundled sample list.' : ''}`,
        flagged ? `<span class="tag warn"><i>!</i>${plural(flagged, 'supplier')} flagged</span>` : '')}
      <section class="panel">${vendors.length ? `<div class="table-wrap"><table><thead><tr><th></th><th>Supplier</th><th>Standing</th><th class="r">Invoices</th><th class="r">Approved</th><th class="r">Waiting</th><th class="r">Rejected</th><th>PO headroom</th><th class="r">Last seen</th></tr></thead><tbody>
        ${vendors.map((v) => { const blocked = /BLOCKED|PROHIBITED|UNAUTHORISED/.test(String(v.status || '').toUpperCase());
          const tone = blocked ? 'bad' : v.attention.length ? 'warn' : 'ok';
          const used = v.open_pos ? Math.max(0, Math.min(1, 1 - v.po_remaining / Math.max(1, v.po_remaining + v.approved_value))) : 0;
          return `<tr class="go" data-href="#/invoices?q=${encodeURIComponent(v.vendor)}" tabindex="0"><td class="stripe"><i class="${tone}"></i></td>
            <td><div class="c-main">${esc(v.vendor)}</div>${v.gstin ? `<div class="c-sub">${esc(v.gstin)}</div>` : ''}${v.attention.length ? `<div class="small" style="color:var(--warn);margin-top:3px">${esc(v.attention.join(' · '))}</div>` : ''}</td>
            <td>${blocked ? '<span class="tag bad"><i>×</i>Blocked</span>' : v.on_file ? '<span class="tag ok"><i>✓</i>On file</span>' : '<span class="tag warn"><i>?</i>Not on file</span>'}</td>
            <td class="r num">${v.invoices}</td><td class="r num">${moneyShort(v.approved_value)}</td><td class="r num">${v.waiting}</td><td class="r num">${v.rejected}</td>
            <td>${v.open_pos ? `<div class="headroom"><div class="meter"><i class="${v.po_remaining < 0 ? 'bad' : used > 0.9 ? 'warn' : ''}" style="width:${Math.max(4, used * 100)}%"></i></div><span class="small muted">${money(v.po_remaining)} left · ${plural(v.open_pos, 'PO')}</span></div>` : '<span class="muted small">No open POs</span>'}</td>
            <td class="r muted small">${esc(ago(v.last_invoice))}</td></tr>`; }).join('')}</tbody></table></div>`
        : '<div class="empty"><h3>No suppliers yet</h3><p>Add a supplier list under Orders &amp; ledger, or bring in an invoice.</p><a class="btn" href="#/data">Orders &amp; ledger</a></div>'}</section>`;
    wireRows(view);
  }

  /* ================= Orders & ledger ================= */
  async function dataPage(_args, _params, alive) {
    const [po, vm, paid] = await Promise.all([api('/api/master/po_master'), api('/api/master/vendor_master'), api('/api/runs?decision=APPROVED&limit=100')]);
    if (!alive()) return;
    setCrumb('<b>Orders &amp; ledger</b>');
    const src = (d) => (d.source === 'database' ? `Last changed by ${esc(d.updated_by)}, ${esc(stamp(d.updated_at))}` : d.source === 'file' ? 'Bundled sample file' : 'Not set up yet');
    const uploader = (kind, hint) => `<form class="filters" data-master="${kind}" style="border-top:1px solid var(--line);border-bottom:0">
        <label style="flex:2;min-width:220px">Replace with a CSV<input type="file" accept=".csv,text/csv" required></label>
        <label style="flex:1;min-width:150px">Changed by<input data-by required maxlength="80" value="${esc(reviewerName())}"></label>
        <span class="row" style="align-self:flex-end"><button class="btn btn-primary">Validate &amp; replace</button><a class="btn btn-ghost" href="/api/master/${kind}.csv" download>Download</a></span>
        <p class="faint small" style="flex-basis:100%">${hint}</p><ul class="form-error" data-errors style="flex-basis:100%;margin:0;padding-left:18px"></ul></form>`;
    view.innerHTML = `
      ${intro('Orders &amp; ledger', 'The reference data every invoice is tested against, and the record of what has been approved so far. Changes here affect invoices that arrive afterwards — past decisions stay as they were.')}
      <section class="panel"><div class="panel-head"><div><h2>Purchase orders</h2><p>${src(po)}</p></div></div>
        ${po.rows.length ? `<div class="table-wrap"><table><thead><tr><th>PO</th><th>Supplier</th><th>Status</th><th class="r">Order value</th><th class="r">Billed before</th><th class="r">Approved here</th><th>Consumed</th></tr></thead><tbody>
          ${po.rows.map((r) => { const u = r.utilisation == null ? 0 : r.utilisation; const t = r.remaining < 0 ? 'bad' : u >= 0.9 ? 'warn' : '';
            return `<tr><td class="mono">${esc(r.po_number)}</td><td>${esc(r.vendor)}</td><td><span class="tag ${/CLOSED|BLOCKED|PROHIBITED/.test(r.status) ? '' : 'ok'}">${esc((r.status || 'OPEN').toLowerCase())}</span></td><td class="r num">${money(r.po_amount)}</td><td class="r num">${money(r.invoiced_to_date)}</td><td class="r num">${money(r.approved_value)} <span class="faint">×${r.approved_invoices}</span></td>
              <td><div class="headroom"><div class="meter"><i class="${t}" style="width:${Math.max(2, Math.min(100, u * 100))}%"></i></div><span class="small muted">${pct(u)} · ${money(r.remaining)} left</span></div></td></tr>`; }).join('')}</tbody></table></div>` : '<div class="empty"><p>No purchase orders yet.</p></div>'}
        ${uploader('po_master', 'Columns: po_number, vendor, po_amount, invoiced_to_date · optional: status (OPEN, CLOSED, BLOCKED), gstin, currency.')}</section>
      <section class="panel mt"><div class="panel-head"><div><h2>Approval ledger</h2><p>Invoices cleared for payment, automatically or by a person</p></div><span class="muted small">${plural(paid.length, 'entry', 'entries')}</span></div>
        ${paid.length ? `<div class="table-wrap"><table><thead><tr><th>Invoice</th><th>Supplier</th><th>PO</th><th class="r">Total</th><th>Cleared by</th><th class="r">When</th></tr></thead><tbody>
          ${paid.map((r) => `<tr class="go" data-href="#/invoice/${esc(r.run_id)}?from=invoices" tabindex="0"><td class="mono">${dash(r.invoice_number)}</td><td>${dash(r.vendor)}</td><td class="mono">${dash(r.po_number)}</td><td class="r num">${money(r.total)}</td><td>${r.resolution === 'APPROVED' ? esc(r.reviews?.[0]?.reviewer || 'Reviewer') : '<span class="muted">Rules (no one)</span>'}</td><td class="r muted small">${esc(ago(r.time))}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty"><p>Nothing approved yet.</p></div>'}</section>
      <section class="panel mt"><div class="panel-head"><div><h2>Supplier list</h2><p>${src(vm)}${vm.source === 'none' ? ' — suppliers are taken from the purchase orders' : ''}</p></div></div>
        ${vm.rows.length ? `<div class="table-wrap"><table><thead><tr><th>Supplier</th><th>GSTIN</th><th>Status</th></tr></thead><tbody>${vm.rows.map((r) => `<tr><td>${esc(r.vendor)}</td><td class="mono">${dash(r.gstin)}</td><td><span class="tag ${/BLOCKED|PROHIBITED|UNAUTHORISED/.test(r.status) ? 'bad' : 'ok'}">${esc((r.status || 'APPROVED').toLowerCase())}</span></td></tr>`).join('')}</tbody></table></div>` : ''}
        ${uploader('vendor_master', 'Columns: vendor · optional: gstin (checksum-validated), status (APPROVED, BLOCKED).')}</section>`;
    wireRows(view);
    $$('[data-master]').forEach((form) => form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const kind = form.dataset.master; const errors = $('[data-errors]', form); errors.innerHTML = '';
      const file = $('input[type=file]', form).files[0]; if (!file) return;
      if (!confirm(`Replace the ${kind === 'po_master' ? 'purchase order' : 'supplier'} list? Invoices arriving from now on are checked against the new list.`)) return;
      try { const res = await api(`/api/master/${kind}`, { method: 'PUT', json: { csv: await file.text(), updated_by: $('[data-by]', form).value } }); toast(`${plural(res.saved, 'row')} saved`); render(); }
      catch (err) { errors.innerHTML = (err.data?.errors || [err.message]).slice(0, 20).map((x) => `<li>${esc(x)}</li>`).join(''); }
    }));
  }

  /* ================= Under the hood ================= */
  async function howPage(_args, _params, alive) {
    const [m, ai] = await Promise.all([meta(), api('/api/ai/config').catch(() => ({}))]);
    if (!alive()) return;
    setCrumb('<b>Under the hood</b>');
    const steps = [
      ['Fingerprint the file', 'Identical files are recognised, so a document is never read twice.', 'auto', 'Automatic'],
      ['Read the text', 'Digital PDFs give up their text directly; scans and photos are read with OCR on this server.', 'auto', 'Automatic'],
      ['Identify the document', 'Tax invoice, purchase order, delivery challan, credit note… Bills and receipts enter checking; quotes and delivery documents are filed.', 'rule', 'Rule'],
      ['Pick out the details', 'Supplier, invoice number, dates, PO, GSTIN and amounts — each stored with the text it sat beside.', 'rule', 'Rule'],
      ['Optional AI pass', 'When OCR fails or misses key fields, a vision model reads the original page images. Rules then check the extracted amounts and payment risks.', 'ai', 'Optional AI'],
      ['Check the supplier', 'On the supplier list? Not blocked? GSTIN valid and matching what is on file?', 'rule', 'Rule'],
      ['Match the order', 'Find the purchase order, confirm it is open, and that this bill fits what is left on it.', 'rule', 'Rule'],
      ['Do the maths', 'Subtotal and tax add up to the total; CGST, SGST and IGST are consistent.', 'rule', 'Rule'],
      ['Look for repeats', 'Same supplier and number already cleared, or a lookalike with the same amount, date and PO.', 'rule', 'Rule'],
      ['Decide and route', 'A broken rule holds it back. Anything uncertain goes to the right team with a deadline. Otherwise it clears.', 'rule', 'Rule'],
    ];
    const b = m.benchmark || {};
    view.innerHTML = `
      ${intro('Under the hood', 'Reading a document is the only uncertain part. Deciding is done by plain rules that are written down here — and every decision keeps a record of why.')}
      <div class="cols cols-main">
        <section class="panel"><div class="panel-head"><div><h2>What happens to every invoice</h2><p>In this order, every time</p></div></div>
          <div class="panel-body"><ol class="steprail">${steps.map(([t, d, k, l]) => `<li><div><b>${t}</b><p>${d}</p></div><span class="kind ${k}"><i></i>${l}</span></li>`).join('')}</ol></div></section>
        <div class="stack-lg">
          <section class="panel panel-pad"><h2 style="font-size:15.5px">How well does it read?</h2><p class="muted small" style="margin:4px 0 16px">Measured on real, messy photos — not on clean samples.</p>
            ${b.comparable ? `<div class="big-stat"><b>${b.exact}<span class="faint" style="font-size:24px">/${b.comparable}</span></b><span class="muted small">fields read exactly on ${b.samples} Indian GST invoice photos (${pct(b.accuracy)})</span></div>
            <p class="small" style="margin-top:14px">That is why no value is trusted unless its printed label sits next to it. Anything less goes to a person instead of being cleared.</p>` : '<p class="muted">Run batch_evaluate.py to measure.</p>'}</section>
          <section class="panel"><div class="panel-head"><div><h2>Who picks up exceptions</h2><p>The highest-risk team wins when several checks fail</p></div></div>
            <div class="table-wrap"><table><tbody>${m.teams.map((t) => `<tr><td><b style="font-weight:500">${esc(t.team)}</b><div class="small muted">${esc(t.handles)}</div></td><td class="r mono">${t.sla_hours} h</td></tr>`).join('')}</tbody></table></div></section>
        </div>
      </div>
      <section class="panel mt"><div class="outcomes">
        <div><span class="tag ok" style="justify-self:start"><i>✓</i>Approved</span><p>No blocking payment risk was found. Routine issues are reported for acknowledgement.</p></div>
        <div><span class="tag warn" style="justify-self:start"><i>!</i>Exception</span><p>Something is missing, unreadable or unusual. A person on the right team approves or rejects it; the automatic call is kept alongside for audit.</p></div>
        <div><span class="tag bad" style="justify-self:start"><i>×</i>Rejected</span><p>Not payable as sent — already cleared, over the PO, a blocked supplier, or totals that don't add up. A note to the supplier is drafted.</p></div>
      </div></section>
      <section class="panel mt"><div class="panel-head"><div><h2>Optional AI connection</h2><p>Any OpenAI-compatible endpoint. The key stays on the server and is never shown again.</p></div><span class="tag ${ai.configured ? 'ok' : ''}">${ai.configured ? 'Connected' : 'Off'}</span></div>
        <form class="panel-body cols cols-3" id="aiform"><label>Endpoint<input id="aiendpoint" type="url" autocomplete="off" value="${esc(ai.endpoint || '')}" placeholder="https://…/v1/chat/completions"></label><label>Model<input id="aimodel" autocomplete="off" value="${esc(ai.model || '')}" placeholder="model name"></label><label>API key<input id="aikey" type="password" autocomplete="new-password" placeholder="${ai.api_key_configured ? 'Set — leave blank to keep' : 'Paste a key'}"></label>
          <div class="row" style="grid-column:1/-1"><button class="btn btn-primary">Save</button><button class="btn" type="button" id="aitest">Test connection</button><span class="muted small" id="aimsg"></span></div></form></section>`;
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

  /* ---------- search palette ---------- */
  const palette = $('#palette'); const pq = $('#paletteresults');
  const PAGES = [['Today', '#/'], ['Exceptions', '#/review'], ['Bring in invoices', '#/upload'], ['All invoices', '#/invoices'], ['Suppliers', '#/vendors'], ['Orders & ledger', '#/data'], ['Under the hood', '#/how']];
  let items = []; let idx = 0; let ptimer;
  function paintPalette(runs = []) {
    const q = $('#palettequery').value.trim().toLowerCase();
    const pages = PAGES.filter(([l]) => !q || l.toLowerCase().includes(q));
    items = [...runs.map((r) => ({ href: `#/invoice/${r.run_id}?from=invoices`, html: `<span>${esc(title(r))} <small>${dash(r.invoice_number)} · ${money(r.total)}</small></span>${tag(r)}` })),
      ...pages.map(([l, h]) => ({ href: h, html: `<span>${esc(l)}</span><small>page</small>` }))];
    idx = Math.min(idx, Math.max(0, items.length - 1));
    pq.innerHTML = (runs.length ? '<li class="group">Invoices</li>' : '') + items.map((it, i) => (i === runs.length && pages.length ? '<li class="group">Go to</li>' : '') + `<li role="option" data-i="${i}" aria-selected="${i === idx}">${it.html}</li>`).join('') || '<li class="group">Nothing found</li>';
    $$('li[data-i]', pq).forEach((li) => li.addEventListener('click', () => choose(Number(li.dataset.i))));
  }
  function choose(i) { const it = items[i]; if (!it) return; palette.close(); go(it.href); }
  function openPalette() { $('#palettequery').value = ''; idx = 0; paintPalette(); palette.showModal(); $('#palettequery').focus(); }
  $('#opensearch').addEventListener('click', openPalette);
  $('#palettequery').addEventListener('input', () => {
    clearTimeout(ptimer); idx = 0; paintPalette();
    const q = $('#palettequery').value.trim(); if (q.length < 2) return;
    ptimer = setTimeout(async () => { try { paintPalette(await api(`/api/runs?q=${encodeURIComponent(q)}&limit=8`)); } catch (_) {} }, 200);
  });
  $('#palettequery').addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); idx = (idx + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % Math.max(1, items.length); $$('li[data-i]', pq).forEach((li) => li.setAttribute('aria-selected', String(Number(li.dataset.i) === idx))); }
    if (e.key === 'Enter') { e.preventDefault(); choose(idx); }
  });
  palette.addEventListener('click', (e) => { if (e.target === palette) palette.close(); });
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); palette.open ? palette.close() : openPalette(); }
    else if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && !palette.open) { e.preventDefault(); openPalette(); }
    else if (e.key === 'Escape' && sidebar.classList.contains('open')) setMenu(false);
  });
  $('#copyemail').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('#emailbody').value); toast('Copied'); }
    catch (_) { $('#emailbody').select(); toast('Press ⌘C to copy'); }
  });

  refreshCount();
  setInterval(() => { if (!document.hidden) refreshCount(); }, 30000);
  render();
})();
