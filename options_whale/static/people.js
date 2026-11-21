// ==========================================================================================
// PEOPLE tab: one card per tracked person (SEC filers and members of Congress): value, holdings, trades, allocation, value history.
// Data: /api/people = people_portfolios.json from the data folder (an input written daily by an external job; nothing is computed here).
// Shares the helpers of forecast.js (fcNum, fcPct, fcCls, fcEsc, kpi) and Lightweight Charts v5 (as chart.js). Cards fill when they scroll into view.
// ==========================================================================================
const PPL = { view: 'people', doc: null, kind: 'all', full: null, cards: {}, io: null, pins: null };
const PPL_FEATURED = ['pelosi', 'trump', 'musk'];          // pinned until the user changes a pin
const PPL_RANGES = ['1W', '1M', '3M', 'YTD', '1Y', 'ALL'];
const PPL_COL = ['#3987e5', '#d95926', '#199e70', '#c98500', '#9085e9', '#e5a0c8', '#6bb7c9', '#a3a3a3'];

const pplDash = (why) => `<span title="${fcEsc(why)}">—</span>`;
const pplMoney = v => {
    if (v === null || v === undefined) return null;
    const a = Math.abs(v), s = v < 0 ? '-' : '';
    return a >= 1e9 ? `${s}$${(a / 1e9).toFixed(2)}B` : a >= 1e6 ? `${s}$${(a / 1e6).toFixed(2)}M` : a >= 1e3 ? `${s}$${(a / 1e3).toFixed(0)}K` : `${s}$${a.toFixed(0)}`;
};
const pplMon = (v, why) => { const m = pplMoney(v); return m === null ? pplDash(why) : m; };
const pplP = (v, why) => v === null || v === undefined ? pplDash(why) : `<span class="${fcCls(v)}">${fcPct(v, 2)}</span>`;
const pplUrl = u => /^https?:\/\//i.test(u || '') ? u : null;
const pplDays = (a, b) => Math.round((Date.parse(b) - Date.parse(a)) / 864e5);
const pplSlug = s => String(s).replace(/[^a-z0-9_-]/gi, '_');
const pplBuy = t => String(t.type).toLowerCase().startsWith('b');
const pplAmount = t => t.shares != null ? `${fcNum(t.shares, 0)} sh`
    : t.amount_low != null && t.amount_high != null ? `${pplMoney(t.amount_low)}–${pplMoney(t.amount_high)}` : pplDash('amount not disclosed');
const pplBadge = t => `<span class="fc-badge ${pplBuy(t) ? 'fc-pos' : 'fc-neg'}">${pplBuy(t) ? 'BUY' : 'SELL'}</span>`;

function pplLoadPins() {
    try { const v = JSON.parse(localStorage.getItem('ppl.pins') || 'null'); if (Array.isArray(v)) return v; } catch (e) { /* storage blocked */ }
    return null;
}
const pplIsPinned = p => PPL.pins ? PPL.pins.includes(p.slug) : PPL_FEATURED.some(f => String(p.slug).toLowerCase().includes(f));
function pplTogglePin(slug) {
    const all = (PPL.doc.people || []).map(p => p.slug);
    const cur = new Set((PPL.doc.people || []).filter(pplIsPinned).map(p => p.slug));
    cur.has(slug) ? cur.delete(slug) : cur.add(slug);
    PPL.pins = all.filter(s => cur.has(s));
    try { localStorage.setItem('ppl.pins', JSON.stringify(PPL.pins)); } catch (e) { /* storage blocked: pin lasts until reload */ }
    pplRender();
}

async function loadPeople() {
    const grid = document.getElementById('pplGrid');
    try {
        const j = await fetchJson(`${API_BASE}/api/people`);
        if (j.status !== 'success') throw new Error(j.reason || j.message || 'load failed');
        PPL.doc = j.data; PPL.pins = pplLoadPins();
        document.getElementById('pplRunInfo').textContent = `${(j.data.people || []).length} tracked · as of ${j.as_of || '—'}`;
        pplRender(); pplViews();
    } catch (e) {
        PPL.doc = null;
        document.getElementById('pplStrip').innerHTML = '';
        grid.innerHTML = `<div class="fc-neg lg:col-span-2 2xl:col-span-3">People data unavailable: ${fcEsc(e.message)}</div>`;
    }
}

function pplKind(k) {
    PPL.kind = k;
    document.querySelectorAll('#people-tab [data-kind]').forEach(b => b.classList.toggle('active', b.dataset.kind === k));
    pplRender();
}

// ---- view switcher: PEOPLE | CONGRESS | COMPANIES. A view whose fields are absent from the file has no button.
const pplHasCong = d => !!(d && d.congress_detail);
const pplHasComp = d => !!(d && Array.isArray(d.companies) && d.companies.length);
function pplViews() {
    const d = PPL.doc, show = { congress: pplHasCong(d), companies: pplHasComp(d) };
    document.querySelectorAll('#pplViews [data-view]').forEach(b => { if (b.dataset.view !== 'people') b.classList.toggle('hidden', !show[b.dataset.view]); });
    if (PPL.view !== 'people' && !show[PPL.view]) PPL.view = 'people';
    pplView(PPL.view);
}
function pplView(v) {
    PPL.view = v;
    document.querySelectorAll('#pplViews [data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === v));
    document.getElementById('pplPeopleCtl').classList.toggle('hidden', v !== 'people');
    document.getElementById('pplPeopleView').classList.toggle('hidden', v !== 'people');
    document.getElementById('pplCong').classList.toggle('hidden', v !== 'congress');
    document.getElementById('pplComp').classList.toggle('hidden', v !== 'companies');
    if (v === 'congress') pplCongress(PPL.doc);
    if (v === 'companies') pplCompanies(PPL.doc);
}
const pplSym = t => `<a class="ppl-sym" data-sym="${fcEsc(t)}" href="#">${fcEsc(t)}</a>`;
const pplMember = m => `<a class="ppl-member" data-member="${fcEsc(m)}" href="#">${fcEsc(m)}</a>`;
const pplRange = (lo, hi) => lo == null && hi == null ? pplDash('amount not disclosed') : `${pplMoney(lo)}–${pplMoney(hi)}`;
const pplPanel = (t, body, note) => `<div class="rainbow-card" style="min-height:0"><div class="rainbow-inner"><div class="fc-title">${t}</div><div class="fc-body">${body}${note ? `<div class="fc-note">${note}</div>` : ''}</div></div></div>`;

// ---- CONGRESS view: most bought / sold, recent big sales, overlaps (tickers several members traded)
function pplCongTrades(d) {            // every Congress trade in the file, with the member's name
    return (d.people || []).filter(p => p.kind === 'congress').flatMap(p => (p.transactions || []).map(t => ({ ...t, who: p.name })));
}
function pplCongRank(d, rows, dir) {   // rows from congress_summary (member counts); $ range summed from the file's trades in the same 30 days
    const ref = Date.parse(String(d.as_of || '').slice(0, 10)), trades = pplCongTrades(d).filter(t => pplBuy(t) === (dir === 'buy') && ref - Date.parse(t.date) <= 30 * 864e5 && Date.parse(t.date) <= ref);
    const sum = {};
    trades.forEach(t => { const s = sum[t.ticker] = sum[t.ticker] || { lo: 0, hi: 0 }; s.lo += t.amount_low || 0; s.hi += t.amount_high || 0; });
    if (!rows || !rows.length) return '<div class="fc-muted">none</div>';
    const max = Math.max(...rows.map(r => r.n_members || 0), 1);
    return `<table class="fc-table">${rows.slice(0, 8).map(r => {
        const s = sum[r.ticker];
        return `<tr><td>${pplSym(r.ticker)}</td><td style="width:45%"><div style="height:6px;border-radius:3px;background:${dir === 'buy' ? '#4ade80' : '#f87171'};width:${Math.max(6, 100 * (r.n_members || 0) / max)}%"></div></td>
            <td>${fcNum(r.n_members, 0)} member${r.n_members === 1 ? '' : 's'}</td><td>${s ? pplRange(s.lo, s.hi) : pplDash('no matching trades with amounts in the file')}</td></tr>`;
    }).join('')}</table>`;
}
function pplTimeline(d, o) {           // one dot per trade by the overlap's members in that ticker, on the span of those trades
    const tr = pplCongTrades(d).filter(t => t.ticker === o.ticker && (o.members || []).includes(t.who));
    if (!tr.length) return '';
    const ts = tr.map(t => Date.parse(t.date)), lo = Math.min(...ts), span = Math.max(Math.max(...ts) - lo, 864e5);
    return `<div style="position:relative;height:18px;margin:6px 4px 2px;border-bottom:1px solid #27272a" title="Trades by these members, ${fcEsc(tr.map(t => t.date).sort()[0])} to ${fcEsc(tr.map(t => t.date).sort().pop())}">
        ${tr.map(t => `<span title="${fcEsc(`${t.who} ${pplBuy(t) ? 'bought' : 'sold'} ${t.date}`)}" style="position:absolute;bottom:-5px;left:calc(${(100 * (Date.parse(t.date) - lo) / span).toFixed(1)}% - 5px);width:10px;height:10px;border-radius:50%;background:${pplBuy(t) ? '#4ade80' : '#f87171'}"></span>`).join('')}</div>
        <div class="fc-note" style="display:flex;justify-content:space-between"><span>${fcEsc(new Date(lo).toISOString().slice(0, 10))}</span><span>${fcEsc(new Date(lo + span).toISOString().slice(0, 10))}</span></div>`;
}
function pplCongress(d) {
    const box = document.getElementById('pplCong');
    if (!pplHasCong(d)) { box.innerHTML = ''; return; }
    const cd = d.congress_detail, cs = d.congress_summary || {}, sales = [...(cd.big_sales_30d || [])].sort((a, b) => (b.amount_high || 0) - (a.amount_high || 0));
    const ov = [...(cd.overlaps || [])].sort((a, b) => (b.members || []).length - (a.members || []).length || (b.n || 0) - (a.n || 0));
    const dirBadge = x => `<span class="fc-badge ${x === 'buy' ? 'fc-pos' : x === 'sell' ? 'fc-neg' : ''}">${x === 'buy' ? 'BUY' : x === 'sell' ? 'SELL' : 'MIXED'}</span>`;
    box.innerHTML = `<div class="grid grid-cols-1 lg:grid-cols-2 gap-6 items-start">
        ${pplPanel('MEMBERS TRADING', `<div style="font-size:30px;font-weight:700">${cs.members_trading_30d == null ? pplDash('not in the file') : fcNum(cs.members_trading_30d, 0)}</div><div class="fc-note">members of Congress traded in the last 30 days</div>`)}
        ${pplPanel('MOST BOUGHT (30 DAYS)', pplCongRank(d, cs.most_bought_30d, 'buy'), 'by number of members; the range adds up the disclosure ranges of the trades in the file')}
        ${pplPanel('MOST SOLD (30 DAYS)', pplCongRank(d, cs.most_sold_30d, 'sell'))}
        ${pplPanel('RECENT BIG SALES', sales.length ? `<div style="max-height:300px;overflow-y:auto"><table class="fc-table"><tr><th>Date</th><th>Member</th><th>Ticker</th><th>Amount</th></tr>${sales.map(x => `<tr><td title="Filed ${fcEsc(x.filed || 'unknown')}">${fcEsc(x.date)}</td><td>${pplMember(x.member)}</td><td>${pplSym(x.ticker)}</td><td>${pplRange(x.amount_low, x.amount_high)}</td></tr>`).join('')}</table></div>` : '<div class="fc-muted">no big sales in the file</div>', 'sorted by the top of the disclosed range')}</div>
        <div class="fc-sub" style="margin:16px 0 8px">OVERLAPS: TICKERS SEVERAL MEMBERS TRADED <span class="fc-note">(tap a name for the member, a ticker for CHART)</span></div>
        ${ov.length ? `<div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">${ov.map(o => `<div class="rainbow-card" style="min-height:0"><div class="rainbow-inner"><div class="fc-body">
            <div style="display:flex;justify-content:space-between;align-items:center"><span style="font-size:20px;font-weight:700">${pplSym(o.ticker)}</span>${dirBadge(o.direction)}</div>
            <div class="fc-note">${fcNum((o.members || []).length, 0)} members · ${fcNum(o.n, 0)} trade${o.n === 1 ? '' : 's'}</div>
            <div style="margin-top:4px">${(o.members || []).map(pplMember).join(' · ')}</div>${pplTimeline(d, o)}</div></div></div>`).join('')}</div>` : '<div class="fc-muted">no overlaps in the file</div>'}
        <div class="fc-note" style="margin-top:12px">Public disclosures filed up to 45 days after a trade, as dollar ranges. Not a trade call and not financial advice.</div>`;
}

// ---- COMPANIES view: one card per company
function pplCompanies(d) {
    const box = document.getElementById('pplComp');
    if (!pplHasComp(d)) { box.innerHTML = ''; return; }
    box.innerHTML = d.companies.map(c => {
        const tr = c.treasury || [], th = c.top_holders || [], ins = [...(c.insider_trades || [])].sort((a, b) => b.date.localeCompare(a.date));
        const tot = tr.reduce((s, x) => s + (x.value_est || 0), 0), anyVal = tr.some(x => x.value_est != null);
        return `<div class="rainbow-card" style="min-height:0"><div class="rainbow-inner">
            <div class="fc-head"><div><div class="fc-title">${fcEsc(c.name)}</div><div class="fc-sub">${pplSym(c.ticker)}${c.cik ? ` · CIK ${fcEsc(c.cik)}` : ''} · as of ${fcEsc(c.as_of || '—')}</div></div></div>
            <div class="fc-body"><div class="fc-kpis">${kpi('Price', c.price == null ? pplDash('not in the file') : `$${fcNum(c.price, 2)}`, c.price_as_of ? `as of ${fcEsc(c.price_as_of)}` : '')}${kpi('Market cap', pplMon(c.market_cap, 'no market cap in the file'))}${kpi('Shares out', c.shares_outstanding == null ? pplDash('not in the file') : fcNum(c.shares_outstanding, 0))}</div>
            <div class="fc-sub" style="margin-top:10px">TREASURY HOLDINGS${anyVal ? ` · ${pplMoney(tot)} at today's price` : ''}</div>
            ${tr.length ? `<table class="fc-table"><tr><th>Asset</th><th>Held</th><th>Value now</th><th>As of</th><th>Source</th></tr>${tr.map(x => `<tr><td>${fcEsc(x.asset)}</td><td>${x.amount == null ? pplDash('not in the file') : fcNum(x.amount, 0)}</td><td>${pplMon(x.value_est, 'no price to value it')}</td><td>${fcEsc(x.as_of || '—')}</td><td>${pplUrl(x.source_url) ? `<a href="${fcEsc(x.source_url)}" target="_blank" rel="noopener">filing ↗</a>` : pplDash('no link in the file')}</td></tr>`).join('')}</table>` : '<div class="fc-muted">No treasury holdings in the file</div>'}
            <div class="fc-sub" style="margin-top:10px">TOP HOLDERS</div>
            ${th.length ? `<div style="max-height:240px;overflow-y:auto"><table class="fc-table"><tr><th>Holder</th><th>Shares</th><th>%</th><th>Change</th><th>As of</th></tr>${th.map(h => `<tr><td>${fcEsc(h.name)}</td><td>${h.shares == null ? pplDash('not in the file') : fcNum(h.shares, 0)}</td><td>${h.pct == null ? pplDash('not in the file') : h.pct.toFixed(2) + '%'}</td>
                <td>${h.change_shares == null ? pplDash('no prior filing in the file') : `<span class="${fcCls(h.change_shares)}">${h.change_shares > 0 ? '+' : ''}${fcNum(h.change_shares, 0)}</span>`}</td><td>${fcEsc(h.as_of || '—')}${h.form ? ` <span class="fc-muted">${fcEsc(h.form)}</span>` : ''}</td></tr>`).join('')}</table></div>` : '<div class="fc-muted">No holders in the file</div>'}
            <div class="fc-sub" style="margin-top:10px">INSIDER TRADES</div>
            ${ins.length ? `<div style="max-height:240px;overflow-y:auto"><table class="fc-table"><tr><th>Date</th><th></th><th>Who</th><th>Amount</th><th>Filing</th></tr>${ins.map(x => `<tr><td>${fcEsc(x.date)}</td><td>${pplBadge(x)}</td><td>${fcEsc(x.owner || x.name || '—')}</td><td>${pplAmount(x)}</td><td>${pplUrl(x.source_url) ? `<a href="${fcEsc(x.source_url)}" target="_blank" rel="noopener">${fcEsc(x.form || 'filing')} ↗</a>` : pplDash('no filing link in the file')}</td></tr>`).join('')}</table></div>` : '<div class="fc-muted">No insider trades in the file</div>'}
            <div class="fc-note" style="margin-top:8px">${fcEsc(c.completeness || '')}</div>
            <div class="fc-note">Sources: ${(c.sources || []).map(s => pplUrl(s.url) ? `<a href="${fcEsc(s.url)}" target="_blank" rel="noopener">${fcEsc(s.label)}</a>` : fcEsc(s.label)).join(' · ') || pplDash('no sources in the file')}</div></div></div></div>`;
    }).join('');
}

// ---- top strip: Congress this month (from congress_summary), latest trades across everyone, most bought this month
function pplStrip(d) {
    const people = d.people || [], box = (t, body) => `<div class="rainbow-card"><div class="rainbow-inner"><div class="fc-title">${t}</div><div class="fc-body">${body}</div></div></div>`;
    const cs = d.congress_summary, out = [];
    const list = (a, n) => a && a.length ? `<table class="fc-table">${a.slice(0, 8).map(x => `<tr><td><a class="ppl-sym" data-sym="${fcEsc(x.ticker)}" href="#">${fcEsc(x.ticker)}</a></td><td>${fcNum(x[n], 0)} member${x[n] === 1 ? '' : 's'}</td></tr>`).join('')}</table>` : '<div class="fc-muted">none</div>';
    if (cs) out.push(box('CONGRESS THIS MONTH', `<div class="fc-note">${cs.members_trading_30d == null ? pplDash('not in the file') : fcNum(cs.members_trading_30d, 0)} members traded in the last 30 days</div>
        <div class="fc-sub">Most bought, by number of members</div>${list(cs.most_bought_30d, 'n_members')}<div class="fc-sub" style="margin-top:6px">Most sold</div>${list(cs.most_sold_30d, 'n_members')}`));
    const all = people.flatMap(p => (p.transactions || []).map(t => ({ ...t, who: p.name }))).sort((a, b) => b.date.localeCompare(a.date)).slice(0, 10);
    out.push(box('LATEST TRADES ACROSS EVERYONE', all.length ? `<table class="fc-table">${all.map(t => `<tr><td>${fcEsc(t.date)}</td><td>${fcEsc(t.who)}</td><td>${pplBadge(t)}</td><td><a class="ppl-sym" data-sym="${fcEsc(t.ticker)}" href="#">${fcEsc(t.ticker)}</a></td><td>${pplAmount(t)}</td></tr>`).join('')}</table>` : '<div class="fc-muted">no trades in the file</div>'));
    const ref = Date.parse(String(d.as_of || '').slice(0, 10)), cnt = {};
    people.forEach(p => (p.transactions || []).forEach(t => {
        if (pplBuy(t) && ref - Date.parse(t.date) <= 30 * 864e5 && Date.parse(t.date) <= ref) (cnt[t.ticker] = cnt[t.ticker] || new Set()).add(p.slug);
    }));
    const top = Object.entries(cnt).map(([t, s]) => [t, s.size]).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 8);
    out.push(box('MOST BOUGHT THIS MONTH', top.length ? `<table class="fc-table">${top.map(([t, n]) => `<tr><td><a class="ppl-sym" data-sym="${fcEsc(t)}" href="#">${fcEsc(t)}</a></td><td>${n} tracked ${n === 1 ? 'person' : 'people'}</td></tr>`).join('')}</table><div class="fc-note">by count of people buying in the 30 days to the file's date</div>` : '<div class="fc-muted">no buys in the last 30 days</div>'));
    document.getElementById('pplStrip').innerHTML = out.join('');
}

// ---- the grid: pinned first, then the rest; filter by kind and name; each card is a shell until it scrolls into view
function pplRender() {
    const d = PPL.doc, grid = document.getElementById('pplGrid');
    if (!d) return;
    try { pplStrip(d); } catch (e) { console.error('people strip', e); }
    const q = (document.getElementById('pplSearch').value || '').trim().toLowerCase();
    const ppl = (d.people || []).filter(p => (PPL.kind === 'all' || p.kind === PPL.kind) && (!q || `${p.name} ${p.slug}`.toLowerCase().includes(q)));
    ppl.sort((a, b) => pplIsPinned(b) - pplIsPinned(a));
    Object.values(PPL.cards).forEach(c => c.chart && c.chart.remove());
    PPL.cards = {};
    if (PPL.io) PPL.io.disconnect();
    PPL.io = 'IntersectionObserver' in window ? new IntersectionObserver(es => es.forEach(e => {
        if (e.isIntersecting) { PPL.io.unobserve(e.target); pplFill(e.target.dataset.slug); }
    }), { root: grid, rootMargin: '300px' }) : null;
    grid.innerHTML = ppl.length ? ppl.map(pplShell).join('') : '<div class="fc-muted">Nobody matches.</div>';
    grid.querySelectorAll('.ppl-card').forEach(el => { PPL.io ? PPL.io.observe(el) : pplFill(el.dataset.slug); });
}

function pplShell(p) {
    const t = p.totals || {}, id = pplSlug(p.slug), cong = p.kind === 'congress';
    const val = cong ? `~${pplMon(t.value_est, 'no estimate in the file')}` : pplMon(t.value_est, 'no value in the file');
    return `<div class="rainbow-card ppl-card" id="ppl-${id}" data-slug="${fcEsc(p.slug)}"><div class="rainbow-inner">
        <div class="fc-head"><div><div class="fc-title">${fcEsc(p.name)} ${pplIsPinned(p) ? '<span title="Pinned">★</span>' : ''}</div><div class="fc-sub">${fcEsc(p.role)} · ${cong ? 'Congress' : 'SEC'}</div></div>
            <div class="fc-actions">
                <button class="fc-icon" data-act="pin" title="${pplIsPinned(p) ? 'Unpin' : 'Pin to the top'}">${pplIsPinned(p) ? '★' : '☆'}</button>
                <button class="fc-icon" data-act="faq" title="How to read this card">?</button>
                <button class="fc-icon" data-act="full" title="Full screen (Esc to exit)">⛶</button>
                <button class="fc-copy" data-act="copy" title="Copy this card's data">COPY</button></div></div>
        <div class="fc-body" id="ppl-body-${id}">
            <div style="font-size:26px;font-weight:700">${val}</div>
            ${cong ? `<div class="fc-note">(range ${pplMon(t.value_low, 'no low estimate')}–${pplMon(t.value_high, 'no high estimate')}), estimated from disclosure ranges</div>` : ''}
            <div class="fc-muted" style="margin-top:8px">Scroll to load…</div></div></div></div>`;
}

function pplHistory(p, range) {
    const h = (p.value_history || []).filter(x => x[1] != null);
    if (!h.length || range === 'ALL') return h;
    const end = new Date(h[h.length - 1][0] + 'T00:00:00Z');
    const from = range === 'YTD' ? new Date(Date.UTC(end.getUTCFullYear(), 0, 1)) : new Date(end.getTime() - ({ '1W': 7, '1M': 30, '3M': 91, '1Y': 365 }[range]) * 864e5);
    return h.filter(x => Date.parse(x[0]) >= from.getTime());
}

function pplFill(slug) {
    const p = (PPL.doc.people || []).find(x => x.slug === slug), id = pplSlug(slug), body = document.getElementById(`ppl-body-${id}`);
    if (!p || !body || PPL.cards[slug]) return;
    PPL.cards[slug] = {};
    const t = p.totals || {}, cong = p.kind === 'congress', hold = p.holdings || [];
    const trades = [...(p.transactions || [])].sort((a, b) => b.date.localeCompare(a.date));
    const big = cong ? `~${pplMon(t.value_est, 'no estimate in the file')}` : pplMon(t.value_est, 'no value in the file');
    const nochg = 'not in the file (not enough price history yet)';
    const top = [...hold].filter(h => h.weight_pct != null).sort((a, b) => b.weight_pct - a.weight_pct).slice(0, 8);
    const rest = top.length ? Math.max(0, 100 - top.reduce((s, h) => s + h.weight_pct, 0)) : 0;
    body.innerHTML = `
        <div style="font-size:26px;font-weight:700">${big}</div>
        ${cong ? `<div class="fc-note">(range ${pplMon(t.value_low, 'no low estimate')}–${pplMon(t.value_high, 'no high estimate')}), estimated from disclosure ranges</div>` : ''}
        <div class="fc-kpis" style="margin-top:6px">${kpi('Day', pplP(t.change_1d_pct, nochg))}${kpi('1M', pplP(t.change_1m_pct, nochg))}${kpi('YTD', pplP(t.change_ytd_pct, nochg))}
            ${kpi('Vs SPY', pplP(t.vs_spy_since_start_pct, nochg), 'since tracking start')}${kpi('Positions', t.n_positions == null ? pplDash('not in the file') : fcNum(t.n_positions, 0))}</div>
        <div class="flex gap-1 flex-wrap" style="margin:8px 0 4px">${PPL_RANGES.map(r => `<button class="fc-toggle ${r === 'ALL' ? 'active' : ''}" data-range="${r}">${r}</button>`).join('')}</div>
        <div class="ppl-chart" id="ppl-chart-${id}" style="height:170px">${(p.value_history || []).length ? '' : '<div class="fc-muted">No value history yet</div>'}</div>
        <div class="fc-sub" style="margin-top:8px">ALLOCATION</div>
        ${top.length ? `<div style="display:flex;height:10px;border-radius:5px;overflow:hidden;background:#111114">${top.map((h, i) => `<div title="${fcEsc(h.ticker)} ${h.weight_pct.toFixed(1)}%" style="width:${h.weight_pct}%;background:${PPL_COL[i % PPL_COL.length]}"></div>`).join('')}${rest > 0.5 ? `<div title="Other ${rest.toFixed(1)}%" style="width:${rest}%;background:#27272a"></div>` : ''}</div>
            <div class="fc-note">${top.map((h, i) => `<span style="color:${PPL_COL[i % PPL_COL.length]}">■</span> ${fcEsc(h.ticker)} ${h.weight_pct.toFixed(1)}%`).join(' · ')}</div>` : `<div class="fc-muted">${pplDash('no weights in the file')}</div>`}
        <div class="fc-sub" style="margin-top:8px">HOLDINGS <span class="fc-note">(tap a row to open it in CHART)</span></div>
        <div style="max-height:220px;overflow-y:auto">${hold.length ? `<table class="fc-table"><tr><th>Ticker</th><th>Name</th><th>Value</th><th>Wt</th><th>Day</th></tr>${hold.map(h => `<tr class="ppl-hold" data-sym="${fcEsc(h.ticker)}" style="cursor:pointer" title="Open ${fcEsc(h.ticker)} in CHART">
            <td>${fcEsc(h.ticker)}</td><td style="max-width:110px;overflow:hidden;text-overflow:ellipsis">${fcEsc(h.name)}</td>
            <td title="${h.value_low != null && h.value_high != null && h.value_low !== h.value_high ? `range ${fcEsc(pplMoney(h.value_low))}–${fcEsc(pplMoney(h.value_high))}` : ''}">${cong && h.value_est != null ? '~' : ''}${pplMon(h.value_est, 'no value in the file')}</td>
            <td>${h.weight_pct == null ? pplDash('no weight in the file') : h.weight_pct.toFixed(1) + '%'}</td><td>${pplP(h.change_1d_pct, 'no day change (no previous close)')}</td></tr>`).join('')}</table>` : '<div class="fc-muted">No holdings in the file</div>'}</div>
        <div class="fc-sub" style="margin-top:8px">RECENT TRADES</div>
        <div style="max-height:260px;overflow-y:auto">${trades.length ? `<table class="fc-table"><tr><th>Date</th><th>Filed</th><th></th><th>Ticker</th><th>Amount</th><th>Owner</th><th>Filing</th></tr>${trades.map(x => {
            const lag = x.filed ? pplDays(x.date, x.filed) : null, u = pplUrl(x.source_url);
            return `<tr><td>${fcEsc(x.date)}</td><td title="Filed ${fcEsc(x.filed || 'unknown')}">${x.filed ? `${fcEsc(x.filed)} <span class="${lag > 30 ? 'fc-warn' : 'fc-muted'}">(+${lag}d)</span>` : pplDash('filing date not in the file')}</td>
                <td>${pplBadge(x)}</td><td><a class="ppl-sym" data-sym="${fcEsc(x.ticker)}" href="#" title="${fcEsc(x.name)}">${fcEsc(x.ticker)}</a></td><td>${pplAmount(x)}</td>
                <td>${x.owner ? fcEsc(x.owner) : pplDash('owner not in the file')}</td><td>${u ? `<a href="${fcEsc(u)}" target="_blank" rel="noopener">${fcEsc(x.form || 'filing')} ↗</a>` : pplDash('no filing link in the file')}</td></tr>`;
        }).join('')}</table>` : '<div class="fc-muted">No trades in the file</div>'}</div>
        <div class="fc-note" style="margin-top:8px">${fcEsc(p.completeness || '')}</div>
        <div class="fc-note">Sources: ${(p.sources || []).map(s => pplUrl(s.url) ? `<a href="${fcEsc(s.url)}" target="_blank" rel="noopener">${fcEsc(s.label)}</a>` : fcEsc(s.label)).join(' · ') || pplDash('no sources in the file')}</div>`;
    pplChart(p, 'ALL');
}

function pplChart(p, range) {
    const id = pplSlug(p.slug), el = document.getElementById(`ppl-chart-${id}`), c = PPL.cards[p.slug], LW = window.LightweightCharts;
    if (!el || !c || !(p.value_history || []).length) return;
    if (!LW) { el.innerHTML = '<div class="fc-muted">Chart library did not load (jsDelivr blocked?).</div>'; return; }
    if (!c.chart) {
        c.chart = LW.createChart(el, { autoSize: true, layout: { background: { color: '#000000' }, textColor: '#a1a1aa' },
            grid: { vertLines: { color: '#18181b' }, horzLines: { color: '#18181b' } }, rightPriceScale: { borderColor: '#27272a' },
            timeScale: { borderColor: '#27272a' }, handleScroll: { vertTouchDrag: false } });
        c.series = c.chart.addSeries(LW.LineSeries, { color: '#3987e5', lineWidth: 2, priceFormat: { type: 'custom', formatter: v => pplMoney(v) } });
    }
    const h = pplHistory(p, range), up = h.length < 2 || h[h.length - 1][1] >= h[0][1];
    c.series.applyOptions({ color: up ? '#4ade80' : '#f87171' });
    c.series.setData(h.map(x => ({ time: x[0], value: x[1] })));
    c.chart.timeScale().fitContent();
}

// ---- controls (delegated: names and tickers come from an external file, so nothing is put into an inline handler)
document.addEventListener('click', e => {
    const sym = e.target.closest && e.target.closest('#people-tab [data-sym]');
    if (sym) { e.preventDefault(); if (window.chartOpenSymbol) window.chartOpenSymbol(sym.dataset.sym); return; }
    const mem = e.target.closest && e.target.closest('#people-tab [data-member]');
    if (mem) {          // open the member's card: back to the PEOPLE view, filtered to the name
        e.preventDefault();
        document.getElementById('pplSearch').value = mem.dataset.member; PPL.kind = 'all';
        document.querySelectorAll('#people-tab [data-kind]').forEach(b => b.classList.toggle('active', b.dataset.kind === 'all'));
        pplView('people'); pplRender(); return;
    }
    const card = e.target.closest && e.target.closest('.ppl-card');
    if (!card) return;
    const slug = card.dataset.slug, rb = e.target.closest('[data-range]'), act = e.target.closest('[data-act]');
    if (rb) {
        card.querySelectorAll('[data-range]').forEach(b => b.classList.toggle('active', b === rb));
        pplChart((PPL.doc.people || []).find(x => x.slug === slug), rb.dataset.range);
    } else if (act) {
        const a = act.dataset.act;
        if (a === 'pin') pplTogglePin(slug); else if (a === 'faq') pplFaq(slug); else if (a === 'full') pplFull(slug); else if (a === 'copy') pplCopy(slug);
    }
});

function pplCopy(slug) {
    const p = (PPL.doc.people || []).find(x => x.slug === slug);
    const payload = JSON.stringify({ card: p.name, as_of: PPL.doc.as_of, data: p }, null, 2);
    if (typeof openCopyModal === 'function') openCopyModal(p.name, payload, 'json'); else navigator.clipboard?.writeText(payload);
}
function pplFull(slug) {
    const back = document.getElementById('pplBackdrop');
    if (PPL.full) { PPL.full.classList.remove('fc-full'); PPL.full = null; back.classList.add('hidden'); document.body.classList.remove('fc-lock'); }
    else if (slug) {
        PPL.full = document.getElementById(`ppl-${pplSlug(slug)}`);
        if (!PPL.full) return;
        PPL.full.classList.add('fc-full'); back.classList.remove('hidden'); document.body.classList.add('fc-lock');
    }
    setTimeout(() => Object.values(PPL.cards).forEach(c => c.chart && c.chart.timeScale().fitContent()), 60);
}
function pplFaq(slug) {
    const p = (PPL.doc.people || []).find(x => x.slug === slug), cong = p.kind === 'congress';
    document.getElementById('pplFaqTitle').textContent = `${p.name} · how to read this card`;
    document.getElementById('pplFaqBody').innerHTML = `<p>${cong
        ? 'A member of Congress files Periodic Transaction Reports up to 45 days after a trade, and only as dollar ranges. The value shown is an <b>estimate</b> from those ranges (the range is printed under it); holdings are rebuilt from trades since tracking began and omit anything held before.'
        : 'An SEC filer\'s holdings come from filings (13F quarterly, Form 4 per trade) with exact share counts; the value is shares times the latest price.'}</p>
        <ul><li><b>Filed (+Nd)</b>: days between the trade and its filing; the lag is why the data is never real-time.</li>
        <li><b>Vs SPY</b>: change since tracking started against SPY over the same days.</li>
        <li><b>—</b>: not in the file; hover for the reason. It is never zero.</li>
        <li>Tap a holding or ticker to open it in the CHART tab.</li></ul>
        <p class="fc-note">${fcEsc(p.completeness || '')} Public disclosures, not a trade call and not financial advice.</p>`;
    document.getElementById('pplFaqModal').classList.remove('hidden');
}
function pplFaqClose() { document.getElementById('pplFaqModal').classList.add('hidden'); }
document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (!document.getElementById('pplFaqModal')?.classList.contains('hidden')) pplFaqClose();
    else if (PPL.full) pplFull();
});
document.addEventListener('click', e => { if (e.target && e.target.id === 'pplFaqModal') pplFaqClose(); });

(function () {            // hook into the tab router, as bitcoin.js does
    const orig = window.switchTab;
    window.switchTab = function (name) {
        if (orig) orig(name);
        if (name === 'people') loadPeople();
    };
})();
