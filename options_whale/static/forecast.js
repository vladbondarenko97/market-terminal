// ==========================================================================================
// FORECAST LAB — signal watch, day scanner and edge lab (fc0, fc12, fc13: /api/forecast signals, /api/scanner, /api/edges) plus
// eleven model cards, fc1..fc11 (data: /api/forecast, from the committed v2 snapshot; card 11 also /api/eia_history)
// ==========================================================================================
const FC = { ticker: 'SPY', data: null, charts: {}, h1: '1m' };
const FC_COL = { blue: '#3987e5', orange: '#d95926', aqua: '#199e70', yellow: '#c98500', violet: '#9085e9',
                 grid: '#1f1f23', ink: '#a1a1aa', ink2: '#71717a' };
const H_LABEL = { '1d': '1D', '1w': '1W', '1m': '1M', '1y': '1Y' };
const H_ORDER = ['1d', '1w', '1m', '1y'];
const hKeys = obj => H_ORDER.filter(k => obj && obj[k] !== undefined);
const hEntries = obj => hKeys(obj).map(k => [k, obj[k]]);
const LB_ORDER = ['1m', '3m', '6m', '12m'];
const moveOrder = obj => Object.entries(obj || {}).sort((a, b) => parseFloat(a[0]) - parseFloat(b[0]));

const fcNum = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? '—' : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const fcPct = (v, d = 1, sign = true) => (v === null || v === undefined) ? '—' : `${sign && v > 0 ? '+' : ''}${Number(v).toFixed(d)}%`;
const fcPctF = (f, d = 1, sign = false) => (f === null || f === undefined) ? '—' : fcPct(f * 100, d, sign);
const fcCls = v => (v === null || v === undefined) ? '' : v > 0 ? 'fc-pos' : v < 0 ? 'fc-neg' : '';
const fcBig = v => {
    if (v === null || v === undefined) return '—';
    const a = Math.abs(v), s = v < 0 ? '-' : '+';
    if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(2)}B`;
    if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(1)}M`;
    if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(0)}K`;
    return `${s}$${a.toFixed(0)}`;
};
const fcEsc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const kpi = (l, v, s = '', cls = '') => `<div class="fc-kpi"><div class="l">${l}</div><div class="v ${cls}">${v}</div>${s ? `<div class="s">${s}</div>` : ''}</div>`;
const missing = (sec) => { const why = sec && (sec.reason || sec.message); return `<div class="fc-muted">Unavailable${why ? ': ' + fcEsc(why) : ''}</div>`; };
// A block the pipeline could not build: absent, or status error / missing (a card's own detail may still be "partial")
const fcFailed = sec => !sec || sec.status === 'error' || sec.status === 'missing';

function fcChart(id, cfg) {
    if (FC.charts[id]) FC.charts[id].destroy();
    const el = document.getElementById(id);
    if (!el) return;
    cfg.options = Object.assign({
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { labels: { color: FC_COL.ink, boxWidth: 10, font: { size: 9 } } },
                   tooltip: { mode: 'index', intersect: false } },
        interaction: { mode: 'index', intersect: false },
    }, cfg.options || {});
    FC.charts[id] = new Chart(el, cfg);
}
const axis = (extra = {}) => Object.assign({ grid: { color: FC_COL.grid }, ticks: { color: FC_COL.ink2, font: { size: 9 } } }, extra);

// A legend on the right takes a third of a phone-wide chart and cuts its labels off: on a phone, below this canvas width, it goes to
// the bottom. The wide layout keeps it on the right at every width. Chart.js does not make the position scriptable, so the chart's
// onResize hook (it also fires when the card goes full screen or the phone turns) sets it; Chart.js then lays the chart out with it
// in the same resize.
const FC_LEGEND_SIDE_MIN_WIDTH = 640;
const fcLegendPosition = (width, phone = typeof isMobileLayout === 'function' && isMobileLayout()) => (phone && width > 0 && width < FC_LEGEND_SIDE_MIN_WIDTH) ? 'bottom' : 'right';
const fcFitLegend = (chart, size) => {
    const lg = chart.options.plugins && chart.options.plugins.legend;
    const want = fcLegendPosition(size && size.width);
    if (lg && lg.position !== want) lg.position = want;
};

async function loadForecast(ticker) {
    if (ticker) FC.ticker = ticker;
    ['SPY', 'SLV'].forEach(t => document.getElementById(`fcBtn${t}`)?.classList.toggle('active', t === FC.ticker));
    loadScanner();                      // cards 12 and 13 have their own routes: they load even when /api/forecast fails
    loadEdges();
    try {
        // fetchJson (app.js) returns the body of a 404 or 503 too, so its message reaches the cards
        const json = await fetchJson(`${API_BASE}/api/forecast?ticker=${FC.ticker}`);
        if (json.status !== 'success') throw new Error(json.message || 'load failed');
        FC.data = json;
        const r = json.run || {};
        document.getElementById('fcRunInfo').textContent = `${FC.ticker} · run ${r.run_id || '—'} · ${r.generated_local || ''}`
            + (json.source_errors && Object.keys(json.source_errors).length ? ` · ${Object.keys(json.source_errors).length} source issue(s)` : '');
        [renderSignals, renderImplied, renderVolForecast, renderTrend, renderPositioning, renderFairValue, renderMacro,
         renderCalendar, renderFlows, renderScorecard, renderDiesel, renderInventories].forEach(fn => {
            try { fn(json); } catch (e) { console.error(fn.name, e); }
        });
    } catch (e) {
        for (let i = 0; i <= 11; i++) {
            const b = document.getElementById(`fc${i}`);
            if (b) b.innerHTML = `<div class="fc-neg">Forecast Lab unavailable: ${fcEsc(e.message)}</div>`;
        }
    }
}

function copyForecastCard(id) {
    const d = FC.data || {};
    const map = {
        fc0: d.signals, fc12: FC.scanner, fc13: FC.edges, fc1: d.data?.implied, fc2: d.data?.vol_forecast, fc3: d.data?.trend, fc4: d.positioning, fc5: d.silver_fair_value,
        fc6: d.macro_regime, fc7: d.data?.calendar, fc8: d.data?.flows, fc9: d.scorecard, fc10: d.refining, fc11: d.refining?.inventories,
    };
    const title = document.querySelector(`#${id}`)?.closest('.rainbow-inner')?.querySelector('.fc-title')?.textContent || id;
    const payload = JSON.stringify({ ticker: FC.ticker, run: d.run?.run_id, card: title, data: map[id] ?? null }, null, 2);
    if (typeof openCopyModal === 'function') openCopyModal(title, payload, 'json');
    else navigator.clipboard?.writeText(payload);
}

// ---------------------------------------------------------------- 0. signal watch / 12. day scanner
const sigTd = (txt, tip, cls = '') => `<td class="${cls}" title="${fcEsc(tip)}">${fcEsc(txt)}</td>`;
const sigTh = (txt, tip) => `<th title="${fcEsc(tip)}">${txt}</th>`;
// On a phone the rule tables are shown as one block per row (styles.css); each cell then needs its column name.
function fcLabelCells(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('table.fc-sig:not(.fc-matrix)').forEach(table => {
        const heads = [...table.querySelectorAll('th')].map(th => th.textContent.trim());
        table.querySelectorAll('tr').forEach(tr => [...tr.children].forEach((cell, i) => { if (cell.tagName === 'TD') cell.dataset.label = heads[i] || ''; }));
    });
}
function sigTable(rows, when, lead = () => '') {
    const td = sigTd, th = sigTh;
    const body = rows.map(r => {
        const arrow = r.bias === 'bullish' ? ' ▲' : r.bias === 'bearish' ? ' ▼' : '';
        const cls = !r.fired ? 'fc-muted' : r.bias === 'bullish' ? 'fc-pos' : r.bias === 'bearish' ? 'fc-neg' : 'fc-warn';
        return `<tr><td title="Market this signal applies to. ALL = both SPY and SLV.">${lead(r)}${fcEsc(r.asset)}</td>
            ${td(r.name, `${r.what} Where: ${r.where}.`)}
            ${td(r.now, `Current reading: live quotes for price-driven rows, otherwise the ${when} run. Where: ${r.where}.`)}
            ${td(r.trigger, `The signal fires when this is true. ${r.what}`)}
            ${td(r.fired ? 'FIRED' + arrow : 'waiting', r.fired ? `True right now.${r.bias ? ' Read: ' + r.bias + '.' : ''}` : `Not true right now. Rechecked every 5 minutes during the session.`, cls)}
            ${td(r.action, r.fired ? `Rule-based action for this signal. Sized by its evidence: ${r.evidence}.` : `If it fires: ${r.plan}. Whether that is a trade or only confirmation depends on the evidence column.`, r.fired ? cls : 'fc-muted')}
            ${td(r.evidence, `How this rule did on history. Evidence grade: ${r.edge} (tested = at least 5 points better than its baseline over 30+ cases; thin = better but under 30 cases; none = no measured edge, so it never produces a trade by itself).`, r.edge === 'tested' ? 'fc-pos' : r.edge === 'thin' ? 'fc-warn' : 'fc-muted')}</tr>`;
    }).join('');
    return `<table class="fc-table fc-sig"><tr>${th('Asset', 'Market the signal applies to')}${th('Signal', 'What is being watched. Hover a row for the definition and where it comes from')}
            ${th('Now', 'Current reading')}${th('Trigger', 'The level or condition that fires the signal')}
            ${th('Status', 'FIRED = the condition is true right now. ▲ bullish, ▼ bearish, no arrow = context, structure or size only')}
            ${th('Action', 'What the rules say to do. Hover a waiting row to see what it would do if it fired')}
            ${th('Evidence', 'How the rule performed historically. Green = tested, amber = small sample, grey = no measured edge')}</tr>${body}</table>`;
}
function renderSignals(j) {
    const el = document.getElementById('fc0'), rows = j.signals || [];
    if (!rows.length) { el.innerHTML = missing({ reason: 'no signals in this run' }); return; }
    const fired = rows.filter(r => r.fired), trades = fired.filter(r => r.action.startsWith('Buy'));
    el.innerHTML = `
        <div class="fc-kpis">${kpi('Signals tracked', rows.length)}${kpi('Fired', fired.length, '', fired.length ? 'fc-warn' : '')}
            ${kpi('Trades on', trades.length, trades.map(r => r.asset).join(' · '), trades.length ? 'fc-pos' : '')}${kpi('As of', fcEsc(j.run?.generated_local || '—'))}</div>
        ${sigTable(rows, j.run?.generated_local || 'latest')}
        <div class="fc-note">Rules only: no model output. Price-driven rows use live quotes; the rest are as of the latest run. Both markets are shown whichever ticker is selected. Actions are generated from each rule's own backtest, so a fired signal with no measured edge says so instead of giving a trade.</div>`;
    fcLabelCells(el);
}
async function loadScanner(opts = {}) {
    const el = document.getElementById('fc12');
    const note = document.getElementById('fcScanMsg');
    if (note) note.textContent = opts.refresh ? 'Refreshing from Yahoo…' : opts.add ? `Checking ${opts.add}…` : 'Loading…';
    try {
        const url = `${API_BASE}/api/scanner` + (opts.remove ? `/${encodeURIComponent(opts.remove)}` : opts.refresh ? '?refresh=1' : '');
        const res = await fetch(url, opts.add ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ symbol: opts.add }) }
                                     : opts.remove ? { method: 'DELETE' } : undefined);
        const json = await res.json();
        if (json.status !== 'success') throw new Error(json.message || 'load failed');
        FC.scanner = json;
        renderScanner(json);
    } catch (e) {
        if (FC.scanner) renderScanner(FC.scanner, e.message);                    // keep the table, show why it failed
        else el.innerHTML = `<div class="fc-neg">Day scanner unavailable: ${fcEsc(e.message)}</div>`;
    }
}
function scannerAdd() {
    const v = (document.getElementById('fcScanAdd')?.value || '').trim().toUpperCase();
    if (v) loadScanner({ add: v });
}
function renderScanner(j, error = '') {
    const el = document.getElementById('fc12'), rows = j.scanner || [], td = sigTd, th = sigTh;
    const dips = rows.filter(r => r.rsi2 !== undefined), trades = rows.filter(r => r.fired && r.action.startsWith('Buy'));
    const shown = rows.filter(r => r.rsi2 !== undefined || r.fired);            // context rows only while they are true
    const remove = r => r.rsi2 !== undefined && r.asset !== 'SPY'
        ? `<button class="fc-x" title="Stop scanning ${fcEsc(r.asset)}" onclick="loadScanner({remove:'${fcEsc(r.asset)}'})">✕</button> ` : '';
    el.innerHTML = `
        <div class="fc-scanbar">
            <input id="fcScanAdd" placeholder="TICKER" maxlength="10" autocomplete="off" title="Type a ticker and press Enter or +. It is checked against Yahoo, saved on this Mac, and scanned by the 5-minute alert job from then on."
                   onkeydown="if(event.key==='Enter')scannerAdd()">
            <button class="fc-toggle" onclick="scannerAdd()" title="Add this ticker to the scanner (up to ${j.max_symbols} symbols)">+ ADD</button>
            <button class="fc-toggle" onclick="loadScanner({refresh:true})" title="Fetch fresh quotes, price history, earnings dates and option prices for everything on this card">↻ REFRESH</button>
            <span id="fcScanMsg" class="${error ? 'fc-neg' : 'fc-muted'}">${error ? fcEsc(error) : `updated ${fcEsc((j.as_of || '').replace('T', ' '))}`}</span>
        </div>
        <div class="fc-kpis">${kpi('Symbols', (j.symbols || []).length, (j.symbols || []).join(' · '))}
            ${kpi('Trades on', trades.length, trades.map(r => r.asset).join(' · ') || 'nothing to do', trades.length ? 'fc-pos' : '')}
            ${kpi('With tested edge', dips.filter(r => r.edge === 'tested').length, dips.filter(r => r.edge === 'tested').map(r => r.asset).join(' · ') || 'none')}
            ${kpi('Entry window', '2:30–3:00 PM CT', 'the only time a trade can fire')}</div>
        ${shown.length ? sigTable(shown, 'latest', remove) : missing({ reason: 'no live quotes or price history right now' })}
        ${(j.failed || []).length ? `<div class="fc-note">No data right now for: ${j.failed.map(s => `${fcEsc(s)} <button class="fc-x" title="Remove ${fcEsc(s)}" onclick="loadScanner({remove:'${fcEsc(s)}'})">✕</button>`).join(' · ')}</div>` : ''}
        <div class="fc-sub" style="margin-top:4px">LIVE WATCH POSITIONS</div>
        ${(j.watch_positions || []).length ? `<table class="fc-table fc-sig"><tr>${th('Opened', 'When the dip rule fired and the alert was sent')}
            ${th('Position', 'The call spread the alert named: long the lower strike, short the higher one')}
            ${th('SPY', 'SPY price when the position opened')}${th('Entry', 'Debit per spread at the alert (mid prices). x100 = dollars at risk per spread')}
            ${th('Now', 'Live value of the spread (mid prices), or the value recorded at the exit alert')}
            ${th('P&L', 'Change in the spread value since entry')}${th('Exit', 'The exit alert is sent near the close on this date; the rule is only tested with a 5-trading-day hold')}</tr>
            ${j.watch_positions.map(p => `<tr>${td(p.opened_at.replace('T', ' ').slice(0, 16), 'Alert time (this Mac\'s clock)')}
                ${td(`SPY ${p.expiration} ${p.long_strike}/${p.short_strike} call spread`, `${p.long_contract} long, ${p.short_contract} short`)}
                ${td(fcNum(p.underlying_price), 'SPY at entry')}${td(fcNum(p.entry_debit), `$${fcNum(p.entry_debit * 100, 0)} per spread`)}
                ${td(fcNum(p.value), p.closed_at ? 'Value recorded at the exit alert' : 'Live mid value')}
                ${td(fcPct(p.pnl_pct, 0), 'Paper result from alert prices, not your fills', fcCls(p.pnl_pct))}
                ${td(p.closed_at ? `closed ${p.closed_at.slice(0, 10)}` : `sell ${p.exit_due}`, p.closed_at ? 'Exit alert sent' : 'Open: waiting for the exit date', p.closed_at ? 'fc-muted' : 'fc-warn')}</tr>`).join('')}</table>`
          : `<div class="fc-muted">None yet. When SPY's "Dip in an uptrend" fires the alert names a call spread of at most $300, it is recorded here, and a sell alert follows 5 trading days later.</div>`}
        <div class="fc-note">One row per symbol, plus any context state that is true right now. Green evidence = the dip rule beat a normal uptrend day on that symbol's own history (5 points of up-rate, t ≥ 2, in both halves); grey = no measured edge, so it never says Buy. Added tickers are saved on this Mac and scanned by the 5-minute alert job. After the 3 PM CT close the triggers shown are the next session's.</div>`;
    fcLabelCells(el);
}

// ---------------------------------------------------------------- 13. edge lab
const jsArg = s => fcEsc(String(s).replace(/[\\']/g, '\\$&'));                 // string literal inside an inline onclick
async function loadEdges(opts = {}) {
    const el = document.getElementById('fc13');
    const note = document.getElementById('fcEdgeMsg');
    if (note) note.textContent = opts.query || opts.add ? `Analysing ${opts.query || opts.add}…` : opts.refresh ? 'Refreshing…' : 'Loading…';
    try {
        const url = `${API_BASE}/api/edges` + (opts.remove ? `/${encodeURIComponent(opts.remove)}` : opts.query ? `?symbol=${encodeURIComponent(opts.query)}` : opts.refresh ? '?refresh=1' : '');
        const res = await fetch(url, opts.add ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ symbol: opts.add }) }
                                     : opts.remove ? { method: 'DELETE' } : undefined);
        const json = await res.json();
        if (json.status !== 'success') throw new Error(json.message || 'load failed');
        if (!json.query && FC.edges?.query && (opts.refresh || opts.remove)) json.query = FC.edges.query;   // keep the lookup on screen
        if (opts.query || opts.add) FC.edgeSel = null;
        FC.edges = json;
        renderEdges(json);
    } catch (e) {
        if (FC.edges) renderEdges(FC.edges, e.message);
        else el.innerHTML = `<div class="fc-neg">Edge Lab unavailable: ${fcEsc(e.message)}</div>`;
    }
}
const edgesSym = () => (document.getElementById('fcEdgeSym')?.value || '').trim().toUpperCase();
function edgesQuery() { const v = edgesSym(); if (v) loadEdges({ query: v }); }
function edgesTrack() { const v = edgesSym(); if (v) loadEdges({ add: v }); }
function renderEdges(j, error = '') {
    const el = document.getElementById('fc13'), tracked = j.tracked || [], td = sigTd, th = sigTh;
    const sel = tracked.find(a => a.symbol === FC.edgeSel) || j.query || tracked[0];
    const bcls = b => b === 'bullish' ? 'fc-pos' : b === 'bearish' ? 'fc-neg' : 'fc-warn';
    const active = tracked.flatMap(a => a.edges.filter(e => e.active && (e.edge === 'tested' || e.edge === 'thin')).map(e => `${a.symbol} ${e.label}`));
    const glyph = e => {
        if (!e.active) return e.edge === 'n/a' ? ['–', 'fc-muted'] : ['·', 'fc-muted'];
        const g = e.bias === 'bullish' ? '▲' : e.bias === 'bearish' ? '▼' : '●';
        return [g, e.edge === 'tested' || e.edge === 'thin' ? bcls(e.bias) : 'fc-muted'];     // on, but no measured edge = grey
    };
    const vcell = (v, h) => td(v.label, v.edges.length ? `Active ${h} edges: ${v.edges.join(', ')}` : `No tested edge is active on the ${h} horizon`,
                               v.bias === 'bullish' ? 'fc-pos' : v.bias === 'bearish' ? 'fc-neg' : 'fc-muted');
    const cols = (tracked[0] || {}).edges || [];
    const matrix = tracked.length ? `<table class="fc-table fc-sig fc-matrix"><tr>${th('Ticker', 'Click a symbol to show its detail below; ✕ stops tracking it')}${th('Price', 'Latest price')}
            ${th('Short', 'Days to weeks: verdict from the tested short-horizon edges that are active now')}${th('Mid', 'About 1–12 months: verdict from the tested mid-horizon edges')}
            ${th('Long', 'Years: verdict from the tested long-horizon edges')}
            ${cols.map(c => th(fcEsc(c.label), `${c.name} — ${c.what} Source: ${c.source}`)).join('')}</tr>
        ${tracked.map(a => `<tr><td title="${fcEsc(a.name)}"><button class="fc-x" title="Stop tracking ${fcEsc(a.symbol)}" onclick="loadEdges({remove:'${jsArg(a.symbol)}'})">✕</button>
                <span class="fc-pick${a.symbol === sel?.symbol ? ' sel' : ''}" onclick="FC.edgeSel='${jsArg(a.symbol)}';renderEdges(FC.edges)">${fcEsc(a.symbol)}</span></td>
            ${td(fcNum(a.price), 'Latest price')}${vcell(a.verdict.short, 'short')}${vcell(a.verdict.mid, 'mid')}${vcell(a.verdict.long, 'long')}
            ${a.edges.map(e => { const [g, c] = glyph(e); return td(g, `${e.name}: ${e.now}. ${e.evidence}`, c); }).join('')}</tr>`).join('')}</table>`
        : missing({ reason: 'nothing tracked yet' });
    let detail = '';
    if (sel) {
        const v = sel.verdict, st = e => e.active ? `ACTIVE${e.bias === 'bullish' ? ' ▲' : e.bias === 'bearish' ? ' ▼' : ''}` : e.edge === 'n/a' ? 'n/a' : 'inactive';
        const untracked = sel === j.query && !(j.symbols || []).includes(sel.symbol) ? ' · not tracked' : '';
        detail = `<div class="fc-sub" style="margin-top:4px">DETAIL · ${fcEsc(sel.symbol)} · ${fcEsc(sel.name)} · ${fcNum(sel.price)} · history since ${fcEsc(sel.history_since)}${untracked}</div>
        <div>${fcEsc(v.summary)}</div>
        <table class="fc-table fc-sig"><tr>${th('Edge', 'The published effect being tested. Hover a row for the definition and citation')}${th('Horizon', 'How long the effect plays out: short = days to weeks, mid = months, long = years')}
            ${th('Now', 'Current reading for this ticker')}${th('Trigger', 'The level or condition that turns the edge on')}
            ${th('Status', 'ACTIVE = the condition is true now. ▲ bullish, ▼ bearish, no arrow = context')}${th('Action', 'What the rules say to do. Hover to see the plan if the edge is active')}
            ${th('Evidence', "How the edge did on this ticker's own history. Green = tested, amber = small sample, grey = no measured edge")}</tr>
        ${sel.edges.map(e => { const c = e.active && (e.edge === 'tested' || e.edge === 'thin') ? bcls(e.bias) : 'fc-muted';   // on without evidence = grey
            return `<tr>${td(e.name, `${e.what} Source: ${e.source}`)}${td(e.horizon.toUpperCase(), 'Horizon of the effect')}
            ${td(e.now, 'Current reading for this ticker')}${td(e.trigger, `The edge turns on when this is true. ${e.what}`)}
            ${td(st(e), e.active ? 'True right now' : 'Not true right now', e.active ? c : 'fc-muted')}${td(e.action, `If active: ${e.plan}`, c)}
            ${td(e.evidence, `Evidence grade: ${e.edge}. tested = beat this ticker's own baseline by a clear margin over 30+ cases; thin = same but under 30 cases; none = no measured edge; n/a = cannot be tested from price history.`,
                 e.edge === 'tested' ? 'fc-pos' : e.edge === 'thin' ? 'fc-warn' : 'fc-muted')}</tr>`; }).join('')}</table>`;
    }
    el.innerHTML = `
        <div class="fc-scanbar">
            <input id="fcEdgeSym" placeholder="TICKER" maxlength="10" autocomplete="off" title="Type any ticker and press Enter to analyse it without saving"
                   onkeydown="if(event.key==='Enter')edgesQuery()">
            <button class="fc-toggle" onclick="edgesQuery()" title="Analyse this ticker now without saving it">QUERY</button>
            <button class="fc-toggle" onclick="edgesTrack()" title="Add this ticker to the tracked list (up to ${fcEsc(j.max_symbols)} symbols), saved on this Mac">+ TRACK</button>
            <button class="fc-toggle" onclick="loadEdges({refresh:true})" title="Bypass the caches and fetch fresh prices and history for everything on this card">↻ REFRESH</button>
            <span id="fcEdgeMsg" class="${error ? 'fc-neg' : 'fc-muted'}">${error ? fcEsc(error) : `updated ${fcEsc((j.as_of || '').replace('T', ' '))}`}</span>
        </div>
        <div class="fc-kpis">${kpi('Tracked', tracked.length, fcEsc((j.symbols || []).join(' · ')))}
            ${kpi('Tested edges active', active.length, fcEsc(active.join(' · ') || 'none'))}
            ${kpi('Showing', fcEsc(sel ? sel.symbol : '—'))}</div>
        ${matrix}
        ${(j.failed || []).length ? `<div class="fc-note">No data right now for: ${j.failed.map(s => `${fcEsc(s)} <button class="fc-x" title="Remove ${fcEsc(s)}" onclick="loadEdges({remove:'${jsArg(s)}'})">✕</button>`).join(' · ')}</div>` : ''}
        ${detail}
        <div class="fc-note">Each edge is graded on this ticker's own history. Only green evidence can produce a trade; an active edge with grey evidence is context, not a signal.</div>`;
    fcLabelCells(el);
}

// ---------------------------------------------------------------- 1. implied range
function renderImplied(j) {
    const imp = j.data?.implied, el = document.getElementById('fc1');
    if (!imp || imp.status !== 'fresh') { el.innerHTML = missing(imp); return; }
    const H = imp.horizons, spot = j.data.spot, sel = H[FC.h1] ? FC.h1 : hKeys(H)[0], hz = H[sel] || {};
    const rows = hEntries(H).map(([k, h]) => h.status !== 'fresh' ? `<tr><td>${H_LABEL[k]}</td><td colspan="8" class="fc-muted">${fcEsc(h.reason)}</td></tr>` :
        `<tr><td>${H_LABEL[k]}</td><td>${h.expiration}${h.scaled_from_dte ? '*' : ''}</td><td>${fcPctF(h.atm_iv)}</td>
         <td>±${fcNum(h.expected_move_1sd)} <span class="fc-muted">(${fcPct(h.expected_move_1sd_pct, 1, false)})</span></td>
         <td>${fcNum(h.range68[0])}–${fcNum(h.range68[1])}</td><td>${fcNum(h.range90[0])}–${fcNum(h.range90[1])}</td>
         <td class="${fcCls(h.p_up - 0.5)}">${fcPctF(h.p_up, 0)}</td><td>${fcPctF(h.p_up_5pct, 0)}</td><td>${fcPctF(h.p_down_5pct, 0)}</td></tr>`).join('');
    const lv = hz.levels || {};
    el.innerHTML = `
        <div class="flex justify-between items-center">
            <div class="fc-seg">${hKeys(H).map(k => `<button class="${k === sel ? 'active' : ''}" onclick="FC.h1='${k}';renderImplied(FC.data)">${H_LABEL[k]}</button>`).join('')}</div>
            <div class="fc-muted">spot ${fcNum(spot)}</div>
        </div>
        <div class="fc-kpis">
            ${kpi('Median', fcNum(hz.median), `${fcPct((hz.median / spot - 1) * 100, 2)} vs spot`)}
            ${kpi('P(finish higher)', fcPctF(hz.p_up, 0), 'risk-neutral, incl. rate drift', fcCls(hz.p_up - 0.5))}
            ${kpi('68% range', `${fcNum(hz.range68?.[0], 1)}–${fcNum(hz.range68?.[1], 1)}`)}
            ${kpi('Skew', fcNum(hz.skew, 3), hz.skew < 0 ? 'downside-heavy' : 'upside-heavy')}
        </div>
        <div class="fc-chart"><canvas id="fc1Chart"></canvas></div>
        ${Object.keys(lv).length ? `<div class="fc-note">P(above) at ${sel.toUpperCase()}: ${Object.entries(lv).map(([k, v]) => `${k.replace('_', ' ')} ${fcPctF(v, 0)}`).join(' · ')}</div>` : ''}
        <table class="fc-table"><tr><th>H</th><th>Expiry</th><th>ATM IV</th><th>±1σ</th><th>68%</th><th>90%</th><th>P↑</th><th>P>+5%</th><th>P<−5%</th></tr>${rows}</table>
        <div class="fc-note">${fcEsc(imp.method)}. ${fcEsc(imp.note)}. * rescaled to the exact horizon from the nearest listed expiry.</div>`;
    const curve = hz.curve || [];
    const ymax = Math.max(...curve.map(p => p[1]), 0);
    const vline = (x, label, color) => ({ label, data: [{ x, y: 0 }, { x, y: ymax }], borderColor: color, borderWidth: 1.5, borderDash: [4, 3], pointRadius: 0 });
    const ds = [{ label: `${sel.toUpperCase()} density`, data: curve.map(p => ({ x: p[0], y: p[1] })), borderColor: FC_COL.blue,
                  backgroundColor: 'rgba(57,135,229,.18)', fill: true, pointRadius: 0, borderWidth: 2 },
                vline(spot, 'Spot', '#fafafa')];
    if (j.data?.flows?.dealer_gamma?.zero_gamma) ds.push(vline(j.data.flows.dealer_gamma.zero_gamma, 'Zero gamma', FC_COL.yellow));
    fcChart('fc1Chart', { type: 'line', data: { datasets: ds },
        options: { scales: { x: axis({ type: 'linear' }), y: axis({ ticks: { display: false } }) }, plugins: { legend: { labels: { color: FC_COL.ink, boxWidth: 10, font: { size: 9 } } }, tooltip: { enabled: false } } } });
}

// ---------------------------------------------------------------- 2. volatility forecast
function renderVolForecast(j) {
    const v = j.data?.vol_forecast, el = document.getElementById('fc2');
    if (!v || v.status !== 'fresh') { el.innerHTML = missing(v); return; }
    const ks = hKeys(v.horizons);
    const rows = ks.map(k => { const h = v.horizons[k];
        return `<tr><td>${H_LABEL[k]}</td><td>${fcPctF(h.forecast_vol)}</td><td>${fcPctF(h.implied_vol)}</td>
        <td class="${fcCls(h.vrp)}">${h.vrp === undefined ? '—' : fcPct(h.vrp * 100, 1)}</td><td>±${fcPct(h.expected_move_1sd_pct, 2, false)}</td>
        <td class="text-left" style="text-align:left">${fcEsc(h.read || h.basis || '')}</td></tr>`; }).join('');
    const bt = v.backtest || {};
    el.innerHTML = `
        <div class="fc-kpis">
            ${kpi('Realized 1D', fcPctF(v.current.rv_1d))}${kpi('Realized 5D', fcPctF(v.current.rv_5d))}
            ${kpi('Realized 22D', fcPctF(v.current.rv_22d))}${kpi('5y average', fcPctF(v.long_run_vol))}
        </div>
        <div class="fc-chart"><canvas id="fc2Chart"></canvas></div>
        <table class="fc-table"><tr><th>H</th><th>HAR vol</th><th>Implied</th><th>VRP</th><th>±1σ</th><th style="text-align:left">Read</th></tr>${rows}</table>
        <div class="fc-note">Out-of-sample (last 20% of history, never used for fitting): ${hEntries(bt).map(([k, b]) =>
            `${H_LABEL[k]} <span class="${fcCls(b.rmse_improvement_vs_naive_pct)}">${fcPct(b.rmse_improvement_vs_naive_pct, 1)}</span> vs naive (${b.spec})`).join(' · ')}.
            VRP = implied − forecast (positive = options rich). ${fcEsc(v.model)}.</div>`;
    fcChart('fc2Chart', { type: 'bar', data: { labels: ks.map(k => H_LABEL[k]), datasets: [
        { label: 'HAR forecast', data: ks.map(k => v.horizons[k].forecast_vol * 100), backgroundColor: FC_COL.blue, borderRadius: 4 },
        { label: 'Options implied', data: ks.map(k => (v.horizons[k].implied_vol ?? null) && v.horizons[k].implied_vol * 100), backgroundColor: FC_COL.orange, borderRadius: 4 }] },
        options: { scales: { x: axis(), y: axis({ ticks: { color: FC_COL.ink2, font: { size: 9 }, callback: x => x + '%' } }) } } });
}

// ---------------------------------------------------------------- 3. trend / CTA
function renderTrend(j) {
    const t = j.data?.trend, el = document.getElementById('fc3');
    if (!t || t.status !== 'fresh') { el.innerHTML = missing(t); return; }
    const dirCls = t.direction === 'LONG' ? 'fc-pos' : t.direction === 'SHORT' ? 'fc-neg' : 'fc-warn';
    const sig = LB_ORDER.filter(k => t.signals[k]).map(k => [k, t.signals[k]]).map(([k, s]) => `<tr><td>${k}</td><td class="${fcCls(s.return)}">${fcPctF(s.return, 1, true)}</td>
        <td>${fcNum(s.z, 2)}</td><td class="${s.signal === 'LONG' ? 'fc-pos' : 'fc-neg'}">${s.signal}</td><td>${fcNum(s.flip_level)}</td>
        <td class="${fcCls(-Math.abs(s.flip_distance_pct) + 2)}">${fcPct(s.flip_distance_pct, 2)}</td></tr>`).join('');
    const cond = hEntries(t.conditional || {}).map(([k, c]) => `<tr><td>${H_LABEL[k]}</td>
        <td>${c.p_up_historical == null ? '—' : fcPctF(c.p_up_historical, 0)}</td><td class="${fcCls(c.avg_fwd_pct_historical)}">${fcPct(c.avg_fwd_pct_historical, 2)}</td>
        <td>${t.backtest?.[k] ? fcPctF(t.backtest[k].hit_rate, 0) : '—'}</td><td style="text-align:left" class="fc-muted">${fcEsc(c.reason || '')}</td></tr>`).join('');
    const sens = moveOrder(t.sensitivity).map(([k, v]) => `${k}: <b class="${fcCls(v)}">${fcNum(v, 2)}</b>`).join(' · ');
    const pos = ((t.exposure + 1) / 2) * 100;
    el.innerHTML = `
        <div class="flex items-center gap-3"><span class="fc-badge ${dirCls}">${t.direction}</span>
            <span class="fc-muted">exposure ${fcNum(t.exposure, 2)} (−1…+1) · vol-scaled ${fcNum(t.position_vol_scaled, 2)} · 60d vol ${fcPctF(t.rv_60d)}</span></div>
        <div class="fc-bar"><i style="left:${pos}%"></i></div>
        <table class="fc-table"><tr><th>Lookback</th><th>Return</th><th>z</th><th>Signal</th><th>Flip level</th><th>Distance</th></tr>${sig}</table>
        <div class="fc-note">Flip level = price at which that lookback's signal turns tomorrow. After a move tomorrow, exposure becomes — ${sens}</div>
        <div class="fc-kpis">${Object.entries(t.sma).map(([n, v]) => kpi(`SMA ${n}`, fcNum(v), t.above_sma[n] ? 'price above' : 'price below', t.above_sma[n] ? 'fc-pos' : 'fc-neg')).join('')}</div>
        <table class="fc-table"><tr><th>H</th><th>P(up) hist.</th><th>Avg fwd</th><th>Hit rate</th><th style="text-align:left">Note</th></tr>${cond}</table>
        <div class="fc-note">${fcEsc(t.model)}. Historical odds are conditional on the current direction over ${t.history_days} days; overlapping windows.</div>`;
}

// ---------------------------------------------------------------- 4. positioning
function cotBlock(name, st) {
    if (!st || st.net === undefined) return '';
    return `<tr><td>${name}</td><td>${fcNum(st.net, 0)}</td><td>${fcPct(st.net_pct_oi, 1)}</td><td class="${fcCls(st.change_1w)}">${fcNum(st.change_1w, 0)}</td>
        <td>${fcNum(st.cot_index, 0)}</td><td>${fcNum(st.zscore, 2)}</td><td style="text-align:left" class="${st.read.startsWith('crowded long') ? 'fc-neg' : st.read.startsWith('crowded short') ? 'fc-pos' : 'fc-muted'}">${st.read}</td></tr>`;
}
function renderPositioning(j) {
    const p = j.positioning, el = document.getElementById('fc4');
    if (fcFailed(p)) { el.innerHTML = missing(p); return; }
    const silver = FC.ticker === 'SLV';
    const grp = silver ? p.silver : p.sp500;
    const main = silver ? grp?.managed_money : grp?.leveraged_funds;
    const second = silver ? grp?.producer_merchant : grp?.asset_managers;
    const names = silver ? ['Managed money', 'Producer/merchant', 'Swap dealers'] : ['Leveraged funds', 'Asset managers', 'Dealers'];
    const keys = silver ? ['managed_money', 'producer_merchant', 'swap_dealers'] : ['leveraged_funds', 'asset_managers', 'dealers'];
    const fwd = main?.forward_4w;
    const trust = p.slv_trust || {};
    el.innerHTML = !main ? missing(grp) : `
        <div class="fc-kpis">
            ${kpi(`${names[0]} net`, fcNum(main.net, 0), `${fcPct(main.net_pct_oi, 1)} of OI · ${main.latest_date}`)}
            ${kpi('COT index (3y)', fcNum(main.cot_index, 0), main.read, main.cot_index >= 90 ? 'fc-neg' : main.cot_index <= 10 ? 'fc-pos' : '')}
            ${kpi('Change 1W / 4W', `${fcNum(main.change_1w, 0)}`, `${fcNum(main.change_4w, 0)} over 4w`, fcCls(main.change_1w))}
            ${silver ? kpi('SLV ounces in trust', fcNum((trust.ounces_in_trust || 0) / 1e6, 2) + 'M', `${trust.as_of || ''}${trust.change_vs_prev_oz != null ? ' · ' + fcNum(trust.change_vs_prev_oz / 1e6, 2) + 'M vs prev' : ''}`)
                     : kpi('Asset managers net', fcNum(second?.net, 0), `COT index ${fcNum(second?.cot_index, 0)}`)}
        </div>
        <div class="fc-chart"><canvas id="fc4Chart"></canvas></div>
        <table class="fc-table"><tr><th>Group</th><th>Net</th><th>% OI</th><th>1W Δ</th><th>Index</th><th>z</th><th style="text-align:left">Read</th></tr>
            ${keys.map((k, i) => cotBlock(names[i], grp?.[k])).join('')}</table>
        ${fwd ? `<div class="fc-note">History (${main.weeks} weeks): 4-week forward return when index ≥ 80 — n ${fwd.when_index_ge_80.n}, avg ${fcPct(fwd.when_index_ge_80.avg_pct, 2)}, up ${fcPctF(fwd.when_index_ge_80.up_rate, 0)};
            when ≤ 20 — n ${fwd.when_index_le_20.n}, avg ${fcPct(fwd.when_index_le_20.avg_pct, 2)}, up ${fcPctF(fwd.when_index_le_20.up_rate, 0)}.</div>` : ''}
        ${silver ? `<div class="fc-note">SLV premium/discount to NAV: ${fcPct(trust.premium_discount_pct, 2)} · trust history builds daily from each run (${(trust.history || []).length} point(s) so far).</div>` : ''}
        <div class="fc-note">CFTC Commitments of Traders (${silver ? 'disaggregated, COMEX silver' : 'traders in financial futures, E-mini S&P 500'}); positions as of Tuesday, published Friday. COT index = where net sits in its 3-year range.</div>`;
    if (main) fcChart('fc4Chart', { type: 'line', data: { labels: main.series.map(r => r[0]), datasets: [
        { label: `${names[0]} net % OI`, data: main.series.map(r => r[2]), borderColor: FC_COL.blue, pointRadius: 0, borderWidth: 2 },
        ...(second?.series ? [{ label: `${names[1]} net % OI`, data: second.series.map(r => r[2]), borderColor: FC_COL.orange, pointRadius: 0, borderWidth: 2 }] : [])] },
        options: { scales: { x: axis({ ticks: { color: FC_COL.ink2, font: { size: 9 }, maxTicksLimit: 6 } }), y: axis({ ticks: { color: FC_COL.ink2, font: { size: 9 }, callback: x => x + '%' } }) } } });
}

// ---------------------------------------------------------------- 5. silver fair value
function renderFairValue(j) {
    const f = j.silver_fair_value, el = document.getElementById('fc5');
    if (!f || f.status !== 'fresh') { el.innerHTML = missing(f); return; }
    const co = Object.entries(f.coefficients).filter(([k]) => k !== 'const').map(([k, c]) =>
        `<tr><td>${k.replace(/_/g, ' ')}</td><td>${fcNum(c.beta, 3)}</td><td class="${Math.abs(c.t) >= 2 ? '' : 'fc-muted'}">${fcNum(c.t, 1)}</td></tr>`).join('');
    const g = f.gold_silver_ratio;
    el.innerHTML = `
        ${FC.ticker !== 'SLV' ? '<div class="fc-note fc-warn">Silver-specific model (shown for both tickers).</div>' : ''}
        <div class="fc-kpis">
            ${kpi('Silver (SI=F)', '$' + fcNum(f.price))}${kpi('Fair value', '$' + fcNum(f.fair_value), `R² ${fcNum(f.r2, 2)}`)}
            ${kpi('Gap', fcPct(f.gap_pct, 1), `z ${fcNum(f.residual_z, 2)}`, fcCls(-f.gap_pct))}
            ${kpi('Half-life', f.half_life_weeks ? fcNum(f.half_life_weeks, 1) + 'w' : '—', 'gap reversion')}
        </div>
        <div class="fc-chart"><canvas id="fc5Chart"></canvas></div>
        <div class="grid grid-cols-2 gap-3">
            <table class="fc-table"><tr><th>Driver</th><th>β</th><th>t</th></tr>${co}</table>
            <table class="fc-table"><tr><th>Horizon</th><th>Price if drivers hold</th><th>Gap closed</th></tr>
                ${hEntries(f.path).map(([k, p]) => `<tr><td>${H_LABEL[k]}</td><td>$${fcNum(p.implied_price_if_drivers_unchanged)}</td><td>${fcPct(p.expected_gap_close_pct, 0, false)}</td></tr>`).join('')}
                <tr><td>Gold/silver</td><td>${fcNum(g.latest, 1)}</td><td>z ${fcNum(g.zscore_5y, 2)}</td></tr></table>
        </div>
        <div class="fc-note">Backtest: ${fcEsc(f.backtest.signal)} — hit rate ${fcPctF(f.backtest.hit_rate, 0)} (n ${f.backtest.n}). ${fcEsc(f.model)}. ${fcEsc(f.caveat)}.</div>`;
    fcChart('fc5Chart', { type: 'line', data: { labels: f.series.map(r => r[0]), datasets: [
        { label: 'Silver', data: f.series.map(r => r[1]), borderColor: FC_COL.blue, pointRadius: 0, borderWidth: 2 },
        { label: 'Fair value', data: f.series.map(r => r[2]), borderColor: FC_COL.orange, pointRadius: 0, borderWidth: 2, borderDash: [5, 3] }] },
        options: { scales: { x: axis({ ticks: { color: FC_COL.ink2, font: { size: 9 }, maxTicksLimit: 6 } }), y: axis({ ticks: { color: FC_COL.ink2, font: { size: 9 }, callback: x => '$' + x } }) } } });
}

// ---------------------------------------------------------------- 6. macro regime
function renderMacro(j) {
    const m = j.macro_regime, el = document.getElementById('fc6');
    if (!m || m.status !== 'fresh') { el.innerHTML = missing(m); return; }
    const rcls = m.regime.startsWith('EXPANSION') ? 'fc-pos' : m.regime.startsWith('LATE') ? 'fc-warn' : 'fc-neg';
    el.innerHTML = `
        <div class="flex items-center gap-3"><span class="fc-badge ${rcls}">${m.regime}</span><span class="fc-muted">conditions ${m.conditions_trend_3m || '—'} (3m) · as of ${m.as_of}</span></div>
        <div class="fc-kpis">
            ${kpi('Recession prob. 12m', fcPctF(m.recession_prob_12m, 1), `a year ago ${fcPctF(m.recession_prob_12m_year_ago, 1)}`, m.recession_prob_12m > 0.3 ? 'fc-neg' : '')}
            ${kpi('10y − 3m', fcNum(m.spread_10y3m, 2) + '%', `monthly avg ${fcNum(m.spread_10y3m_monthly_avg, 2)}`)}
            ${kpi('10y − 2y', fcNum(m.spread_10y2y, 2) + '%')}
            ${kpi('NFCI', fcNum(m.nfci, 3), `13w Δ ${fcNum(m.nfci_change_13w, 3)}`, m.nfci > 0 ? 'fc-neg' : 'fc-pos')}
            ${kpi('Sahm rule', fcNum(m.sahm, 2), '≥ 0.50 = triggered', m.sahm >= 0.5 ? 'fc-neg' : '')}
            ${kpi('HY OAS', fcNum(m.hy_oas, 2) + '%', `3m Δ ${fcNum(m.hy_oas_change_3m, 2)}`)}
            ${kpi('Real yield 10y', fcNum(m.real_yield_10y, 2) + '%', `3m Δ ${fcNum(m.real_yield_change_3m, 2)}`)}
        </div>
        <div class="grid grid-cols-2 gap-3"><div class="fc-chart" style="height:120px"><canvas id="fc6ChartA"></canvas></div>
            <div class="fc-chart" style="height:120px"><canvas id="fc6ChartB"></canvas></div></div>
        <div class="fc-note">${m.flags.length ? '⚠ ' + m.flags.map(fcEsc).join(' · ') : 'No stress flags.'} ${fcEsc(m.model)}. NFCI > 0 = tighter than average.</div>`;
    const line = (id, rows, label, color, suffix) => fcChart(id, { type: 'line', data: { labels: rows.map(r => r[0]), datasets: [
        { label, data: rows.map(r => r[1]), borderColor: color, pointRadius: 0, borderWidth: 2 }] },
        options: { scales: { x: axis({ ticks: { display: false } }), y: axis({ ticks: { color: FC_COL.ink2, font: { size: 9 }, callback: x => x + suffix } }) } } });
    line('fc6ChartA', m.history.t10y3m, '10y − 3m spread', FC_COL.blue, '%');
    line('fc6ChartB', m.history.nfci, 'NFCI', FC_COL.orange, '');
}

// ---------------------------------------------------------------- 7. calendar
function renderCalendar(j) {
    const c = j.data?.calendar, el = document.getElementById('fc7');
    if (!c || c.status !== 'fresh') { el.innerHTML = missing(c); return; }
    const names = { fomc_day: 'FOMC decision day', day_before_fomc: 'Day before FOMC', cpi_day: 'CPI release day', opex_week: 'Monthly OPEX week', turn_of_month: 'Turn of month (4d)' };
    const rows = Object.entries(c.effects).map(([k, e]) => `<tr><td title="${fcEsc(e.definition)}">${names[k] || k}</td><td>${e.n ?? '—'}</td>
        <td class="${fcCls(e.mean_pct)}">${fcPct(e.mean_pct, 2)}</td><td>${fcPctF(e.hit_rate, 0)}</td><td>${fcPct(e.avg_abs_pct, 2, false)}</td>
        <td class="${Math.abs(e.t_vs_baseline || 0) >= 2 ? 'fc-warn' : 'fc-muted'}">${fcNum(e.t_vs_baseline, 1)}</td></tr>`).join('');
    const up = c.upcoming_7d.length ? c.upcoming_7d.map(u => `<span class="fc-badge fc-warn">${fcEsc(u.event)} · ${u.date}</span>`).join(' ') : '<span class="fc-muted">No tracked events in the next 7 days.</span>';
    const dow = c.day_of_week;
    el.innerHTML = `
        <div class="flex flex-wrap gap-2">${up}</div>
        <div class="fc-kpis">
            ${kpi('Next FOMC', c.next.fomc || '—')}${kpi('Next CPI', c.next.cpi || '—')}${kpi('Next OPEX', c.next.opex || '—')}
            ${kpi('Calendar drift 7d', fcPct(c.calendar_drift_next_7d_pct, 2), 'sum of active effects vs baseline', fcCls(c.calendar_drift_next_7d_pct))}
        </div>
        <table class="fc-table"><tr><th>Effect</th><th>n</th><th>Mean</th><th>Hit</th><th>|move|</th><th>t</th></tr>${rows}</table>
        <div class="fc-chart" style="height:110px"><canvas id="fc7Chart"></canvas></div>
        <div class="fc-note">${c.history_days} trading days; baseline daily mean ${fcPct(c.baseline_daily_mean_pct, 3)}. |t| ≥ 2 is highlighted; smaller effects are within noise. ${fcEsc(c.note)}.</div>`;
    const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].filter(d => dow[d]);
    fcChart('fc7Chart', { type: 'bar', data: { labels: days, datasets: [{ label: 'Mean daily return by weekday', data: days.map(d => dow[d].mean_pct),
        backgroundColor: days.map(d => (dow[d].mean_pct || 0) >= 0 ? FC_COL.aqua : FC_COL.orange), borderRadius: 4 }] },
        options: { scales: { x: axis(), y: axis({ ticks: { color: FC_COL.ink2, font: { size: 9 }, callback: x => x.toFixed(2) + '%' } }) } } });
}

// ---------------------------------------------------------------- 8. flows
function renderFlows(j) {
    const f = j.data?.flows, el = document.getElementById('fc8');
    if (!f || f.status !== 'fresh') { el.innerHTML = missing(f); return; }
    const vc = f.vol_control, le = f.leveraged_etfs, dg = f.dealer_gamma, cta = f.cta;
    const scen = vc ? moveOrder(vc.flow_if_tomorrow).map(([k, v]) => `<td class="${fcCls(v)}">${fcBig(v)}</td>`).join('') : '';
    el.innerHTML = `
        <div class="fc-kpis">
            ${vc ? kpi('Vol-control exposure', fcNum(vc.exposure, 2) + 'x', `RV used ${fcPctF(vc.realized_vol_used)}`) : ''}
            ${vc ? kpi('Vol-control flow 5d', fcBig(vc.flow_last_5d), `21d ${fcBig(vc.flow_last_21d)}`, fcCls(vc.flow_last_5d)) : ''}
            ${kpi('Lev. ETF rebalance / 1%', fcBig(le.flow_per_1pct_move), le.pct_of_adv_per_1pct != null ? `${fcNum(le.pct_of_adv_per_1pct, 2)}% of ADV` : '')}
            ${kpi('Last session rebalance', fcBig(le.last_session_rebalance), `after ${fcPct(le.last_session_return_pct, 2)}`, fcCls(le.last_session_rebalance))}
            ${dg ? kpi('Dealer hedge / +1%', fcBig(dg.hedge_flow_per_1pct), dg.net_gex_usd_per_1pt > 0 ? 'long gamma (dampening)' : 'short gamma (amplifying)', fcCls(dg.hedge_flow_per_1pct)) : ''}
            ${cta ? kpi('CTA exposure', fcNum(cta.exposure, 2), 'replicated −1…+1', fcCls(cta.exposure)) : ''}
        </div>
        ${vc ? `<table class="fc-table"><tr><th>Vol-control if tomorrow</th>${moveOrder(vc.flow_if_tomorrow).map(([k]) => `<th>${k}</th>`).join('')}</tr><tr><td>Est. flow</td>${scen}</tr></table>` : '<div class="fc-note">Vol-control model applies to the S&P (SPY) only.</div>'}
        ${cta ? `<table class="fc-table"><tr><th>CTA exposure after move</th>${moveOrder(cta.exposure_after_move).map(([k]) => `<th>${k}</th>`).join('')}</tr>
            <tr><td>Exposure</td>${moveOrder(cta.exposure_after_move).map(([, v]) => `<td class="${fcCls(v)}">${fcNum(v, 2)}</td>`).join('')}</tr></table>` : ''}
        <table class="fc-table"><tr><th>Leveraged fund</th><th>Lev.</th><th>AUM</th><th>Rebalance / 1%</th></tr>
            ${Object.entries(le.funds).map(([t, x]) => `<tr><td>${t}</td><td>${x.leverage}x</td><td>${fcBig(x.aum).replace('+', '')}</td><td class="${fcCls(x.flow_per_1pct)}">${fcBig(x.flow_per_1pct)}</td></tr>`).join('')}</table>
        <div class="fc-note">${fcEsc(le.note)}. ${vc ? fcEsc(vc.note) + '.' : ''} Dealer hedge sign: + = dealers buy into a rally. ${fcEsc(cta?.note || '')}</div>`;
}

// ---------------------------------------------------------------- 9. scorecard
function renderScorecard(j) {
    const s = j.scorecard, el = document.getElementById('fc9'), d = j.data || {};
    if (fcFailed(s)) { el.innerHTML = missing(s || { reason: 'no scorecard in this response' }); return; }
    const graded = (s?.graded || []).slice().sort((a, b) => a.model.localeCompare(b.model) || H_ORDER.indexOf(a.horizon) - H_ORDER.indexOf(b.horizon)).map(r => `<tr><td>${r.model}</td><td>${H_LABEL[r.horizon] || r.horizon}</td><td>${r.n}</td>
        <td>${r.hit_rate == null ? '—' : fcPctF(r.hit_rate, 0)}</td><td>${r.coverage68 == null ? '—' : fcPctF(r.coverage68, 0)}</td>
        <td>${r.brier == null ? '—' : fcNum(r.brier, 3)}</td></tr>`).join('');
    const har = hEntries(d.vol_forecast?.backtest || {}).map(([k, b]) => `<tr><td>HAR vol vs naive</td><td>${H_LABEL[k]}</td><td>${b.oos_days}</td>
        <td colspan="3" class="${fcCls(b.rmse_improvement_vs_naive_pct)}">${fcPct(b.rmse_improvement_vs_naive_pct, 1)} RMSE (${b.spec})</td></tr>`).join('');
    const tr = hEntries(d.trend?.backtest || {}).map(([k, b]) => `<tr><td>Trend direction</td><td>${H_LABEL[k]}</td><td>${b.n}${b.reliable ? '' : '*'}</td>
        <td colspan="3">${fcPctF(b.hit_rate, 0)} hit · up when long ${fcPctF(b.up_rate_when_long, 0)} / when short ${fcPctF(b.up_rate_when_short, 0)}</td></tr>`).join('');
    const fv = j.silver_fair_value?.backtest;
    el.innerHTML = `
        <div class="fc-kpis">${kpi('Logged forecasts', s?.logged ?? 0)}${kpi('Awaiting grade', s?.pending ?? 0)}${kpi('Graded groups', (s?.graded || []).length)}${kpi('Prices through', s?.last_price_date || '—')}</div>
        <div class="fc-sub" style="margin-top:2px">LIVE GRADES</div>
        ${graded ? `<table class="fc-table"><tr><th>Model</th><th>H</th><th>n</th><th>Hit</th><th>In 68%</th><th>Brier</th></tr>${graded}</table>`
                 : `<div class="fc-muted">No forecast has reached its date yet. Each live run logs implied, HAR, trend, calendar, positioning and fair-value calls; the first 1-day grades appear after the next trading close.</div>`}
        <div class="fc-sub" style="margin-top:4px">WALK-FORWARD BACKTESTS (${FC.ticker})</div>
        <table class="fc-table"><tr><th>Model</th><th>H</th><th>n</th><th colspan="3">Result</th></tr>${har}${tr}
            ${fv && FC.ticker === 'SLV' ? `<tr><td>Fair-value reversion</td><td>4W</td><td>${fv.n}</td><td colspan="3">${fcPctF(fv.hit_rate, 0)} hit</td></tr>` : ''}</table>
        <div class="fc-note">${fcEsc(s?.note || '')}. * fewer than 10 independent windows — not used for odds.</div>`;
}

// ---------------------------------------------------------------- 10. diesel & refining
function renderDiesel(j) {
    const r = j.refining, el = document.getElementById('fc10');
    if (!r) { el.innerHTML = missing({ reason: 'no refining data in this run yet (runs after the next pipeline run)' }); return; }
    if (fcFailed(r)) { el.innerHTML = missing(r); return; }
    const m = r.margins || {}, f = r.fundamentals || {}, mt = r.maintenance || {}, o = r.outages || {}, u = r.ulsd_positioning || {};
    const d = m.diesel || {}, t = m.three_two_one || {};
    const ds = f.dist_stocks || {}, us = f.util_us || {};
    const padd = ['util_p1', 'util_p2', 'util_p3', 'util_p4', 'util_p5'].filter(k => f[k]).map(k => `<tr><td>${{util_p1: 'East Coast', util_p2: 'Midwest', util_p3: 'Gulf Coast', util_p4: 'Rockies', util_p5: 'West Coast'}[k]}</td>
        <td>${fcNum(f[k].latest, 1)}%</td><td class="${fcCls(f[k].vs_avg_5y)}">${fcNum(f[k].vs_avg_5y, 1)} pts</td><td>${fcNum(f[k].change_1w, 1)}</td></tr>`).join('');
    const kindBadge = e => e.kind === 'unplanned' ? `<span class="fc-badge fc-neg">UNPLANNED</span>` : e.kind === 'planned_maintenance'
        ? `<span class="fc-badge fc-warn">MAINTENANCE</span>` : `<span class="fc-badge" style="color:#a1a1aa">${fcEsc((e.kind || '').replace('_', ' ').toUpperCase())}</span>`;
    const evRows = (o.events || []).slice(0, 12).map(e => { const rf = e.refinery || {};
        return `<tr title="${fcEsc(e.cause)}"><td>${fcEsc(String(e.start || '').replace('T', ' ').slice(0, 16))}</td>
        <td style="text-align:left">${fcEsc(rf.site || e.name)} <span class="fc-muted">${fcEsc((rf.company || '').split(' ')[0])}</span></td>
        <td>${rf.capacity_bpd ? fcNum(rf.capacity_bpd / 1000, 0) + 'k' : '—'}</td><td style="text-align:left">${kindBadge(e)}${e.major ? ' <span class="fc-badge fc-neg">MAJOR</span>' : ''}</td>
        <td style="text-align:left">${fcEsc((e.units_classified || []).join(', ') || '—')}</td><td>${fcEsc(e.duration || '—')}</td>
        <td><a href="${fcEsc(e.url)}" target="_blank" rel="noopener" class="fc-muted">TCEQ ↗</a></td></tr>`; }).join('');
    const sc = m.diesel_seasonal_4w_change || {};
    el.innerHTML = `
        <div class="fc-kpis">
            ${m.status === 'fresh' ? kpi('Diesel crack', '$' + fcNum(d.latest), `${fcNum(d.percentile_5y, 0)}th pct 5y · ${fcNum(d.vs_seasonal, 1)} vs seasonal`, d.percentile_5y >= 80 ? 'fc-neg' : '') : kpi('Diesel crack', '—', fcEsc(m.reason))}
            ${m.status === 'fresh' ? kpi('3-2-1 crack', '$' + fcNum(t.latest), `1w ${fcNum(t.change_1w, 2)} · 1m ${fcNum(t.change_1m, 2)}`) : ''}
            ${us.latest != null ? kpi('US refinery runs', fcNum(us.latest, 1) + '%', `${fcNum(us.vs_avg_5y, 1)} pts vs 5y same week`) : ''}
            ${ds.latest != null ? kpi('Distillate stocks', fcNum(ds.latest / 1000, 1) + 'M bbl', `${fcPct(ds.vs_avg_5y_pct, 1)} vs 5y · ${fcNum(f.distillate_days_of_supply, 1)} days`, ds.vs_avg_5y_pct < -5 ? 'fc-neg' : '') : ''}
            ${kpi('Unplanned outages 7d', String(o.unplanned_7d ?? 0), `${fcNum((o.capacity_hit_unplanned_7d_bpd || 0) / 1000, 0)}k bpd at affected refineries`, (o.major_7d || 0) > 0 ? 'fc-neg' : '')}
            ${u.net != null ? kpi('ULSD managed money', fcNum(u.net, 0), `COT index ${fcNum(u.cot_index, 0)} · ${u.read}`) : ''}
        </div>
        <div class="fc-chart"><canvas id="fc10Chart"></canvas></div>
        ${mt.status === 'fresh' ? `<div class="fc-note"><b>${fcEsc(mt.season)}</b> — typical path from here (10y seasonal): ${mt.path.slice(0, 8).map(p => `${p.week_ending.slice(5)} <b>${fcNum(p.expected_utilization, 1)}%</b>`).join(' · ')}; low ≈ ${fcNum(mt.expected_trough.expected_utilization, 1)}% week of ${mt.expected_trough.week_ending}.
            Diesel crack over the next 4 weeks from this week: avg ${fcNum(sc.avg_usd_bbl, 2)} $/bbl, up in ${sc.up_years}/${sc.n_years} years.</div>` : ''}
        <div class="grid grid-cols-1 xl:grid-cols-2 gap-3">
            <table class="fc-table"><tr><th>Region</th><th>Runs</th><th>vs 5y</th><th>1w Δ</th></tr>${padd}</table>
            <table class="fc-table"><tr><th>Stocks</th><th>Latest</th><th>vs 5y</th></tr>
                ${[['dist_stocks', 'Distillate US'], ['dist_stocks_p1', 'Distillate East Coast'], ['dist_stocks_p3', 'Distillate Gulf'], ['gas_stocks', 'Gasoline US']].filter(([k]) => f[k]).map(([k, n]) =>
                    `<tr><td>${n}</td><td>${fcNum(f[k].latest / 1000, 1)}M</td><td class="${fcCls(f[k].vs_avg_5y_pct)}">${fcPct(f[k].vs_avg_5y_pct, 1)}</td></tr>`).join('')}
                ${f.dist_supplied ? `<tr><td>Distillate demand</td><td>${fcNum(f.dist_supplied.latest / 1000, 2)}M b/d</td><td class="${fcCls(f.dist_supplied.vs_avg_5y_pct)}">${fcPct(f.dist_supplied.vs_avg_5y_pct, 1)}</td></tr>` : ''}
                ${f.dist_exports ? `<tr><td>Distillate exports</td><td>${fcNum(f.dist_exports.latest / 1000, 2)}M b/d</td><td class="${fcCls(f.dist_exports.vs_avg_5y_pct)}">${fcPct(f.dist_exports.vs_avg_5y_pct, 1)}</td></tr>` : ''}</table>
        </div>
        <div class="fc-sub" style="margin-top:2px">REFINERY EVENTS · TEXAS (TCEQ) · newest first · hover a row for the operator's stated cause</div>
        ${evRows ? `<table class="fc-table"><tr><th>Start</th><th style="text-align:left">Refinery</th><th>Capacity</th><th style="text-align:left">Type</th><th style="text-align:left">Units</th><th>Duration</th><th></th></tr>${evRows}</table>`
                 : '<div class="fc-muted">No refinery events captured yet — history accumulates with every run (the TCEQ feed keeps ~5 days).</div>'}
        <div class="fc-note">${fcEsc(o.coverage || '')} Crack spreads from front-month NY Harbor ULSD, RBOB and WTI futures (${fcEsc(m.as_of || '')}); EIA weekly data ${fcEsc(us.date || '')}; capacities from EIA Refinery Capacity Report ${r.capacity_report_year || ''}.
            MAJOR = unplanned at a ≥250k bpd refinery on a key unit, or 24h+ on an important unit. ${Object.keys(r.source_errors || {}).length ? '⚠ source issues: ' + fcEsc(Object.keys(r.source_errors).join(', ')) : ''}</div>`;
    if (m.series) fcChart('fc10Chart', { type: 'line', data: { labels: m.series.map(x => x[0]), datasets: [
        { label: 'Diesel crack ($/bbl)', data: m.series.map(x => x[1]), borderColor: FC_COL.orange, pointRadius: 0, borderWidth: 2 },
        { label: '3-2-1 crack ($/bbl)', data: m.series.map(x => x[2]), borderColor: FC_COL.blue, pointRadius: 0, borderWidth: 2 }] },
        options: { scales: { x: axis({ ticks: { color: FC_COL.ink2, font: { size: 9 }, maxTicksLimit: 6 } }), y: axis({ ticks: { color: FC_COL.ink2, font: { size: 9 }, callback: x => '$' + x } }) } } });
}

// ---------------------------------------------------------------- 11. EIA inventories (combined chart, full history)
const INV_COL = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'];   // validated dark steps, fixed order
const INV_KEYS = ['crude_stocks', 'dist_stocks', 'gas_stocks', 'jet_stocks', 'spr_stocks', 'cushing_stocks', 'propane_stocks', 'resid_stocks'];
const INV_MODES = {
    vs5y: { label: 'vs 5y avg', field: 'vs_5y_pct', axis: '% vs 5-year same-week average', tick: v => (v > 0 ? '+' : '') + v.toFixed(1) + '%',
            note: 'Below 0 = tighter than a normal year for that week; the dashed line is the 5-year norm.' },
    indexed: { label: 'Indexed', field: 'mbbl', axis: 'Indexed (start of range = 100)', tick: v => v.toFixed(1),
               note: 'Every stock is set to 100 at the start of the selected range, so the lines show % change over it.' },
    level: { label: 'Levels', field: 'mbbl', axis: 'Million barrels', tick: v => v.toFixed(1) + 'M',
             note: 'Raw levels: large stocks (crude, SPR) dwarf the products. Hide them in the legend to see the small ones.' },
    dos: { label: 'Days of supply', field: 'days_of_supply', axis: 'Days of supply', tick: v => v.toFixed(1) + ' d',
           note: 'Days of supply = stocks ÷ 4-week average demand (crude: ÷ refinery crude runs). Demand data starts in 1991. SPR, Cushing, propane and residual fuel have no matching demand series.' },
};
const INV_RANGES = [['1m', '1M', 31], ['3m', '3M', 92], ['6m', '6M', 183], ['ytd', 'YTD'], ['1y', '1Y', 366], ['5y', '5Y', 1827], ['10y', '10Y', 3653], ['max', 'MAX']];
FC.inv = FC.inv || { mode: 'vs5y', range: '6m', from: null, to: null, hist: null, state: 'idle', error: null, hidden: {} };

const invShift = (iso, days) => new Date(Date.parse(iso + 'T00:00:00Z') - days * 86400000).toISOString().slice(0, 10);
function invSet(patch) { Object.assign(FC.inv, patch); renderInventories(FC.data); }
function invRange(k) { invSet({ range: k, from: null, to: null }); }
function invYear(y) { if (y) invSet({ range: 'custom', from: `${y}-01-01`, to: `${y}-12-31` }); else invRange('max'); }
function invDate(which, val) {
    if (!val) return;
    const [from, to] = FC.inv.win || [];
    invSet(Object.assign({ range: 'custom', from, to }, { [which]: val }));
}

async function loadInvHistory() {
    if (FC.inv.state !== 'idle') return;
    FC.inv.state = 'loading';
    try {
        const json = await fetchJson(`${API_BASE}/api/eia_history`);
        if (json.status !== 'success') throw new Error(json.message || 'load failed');
        Object.assign(FC.inv, { hist: json, state: 'ready' });
    } catch (e) {
        Object.assign(FC.inv, { state: 'error', error: e.message });
    }
    if (FC.data) renderInventories(FC.data);
}

// The run snapshot holds 26 weeks; it is shown until (or instead of) the full history from /api/eia_history.
const invFromSnapshot = inv => ({ dates: inv.lines[0].points.map(p => p.date), start: inv.lines[0].points[0].date, as_of: inv.as_of,
    series: inv.lines.map(l => ({ key: l.key, label: l.label, mbbl: l.points.map(p => p.mbbl), vs_5y_pct: l.points.map(p => p.vs_5y_pct) })) });

function invWindow(h) {
    const last = h.dates[h.dates.length - 1], st = FC.inv;
    let from = h.dates[0], to = last;
    if (st.range === 'custom') { from = st.from || from; to = st.to || to; }
    else if (st.range === 'ytd') from = last.slice(0, 4) + '-01-01';
    else if (st.range !== 'max') from = invShift(last, (INV_RANGES.find(r => r[0] === st.range) || INV_RANGES[2])[2]);
    if (from > to) [from, to] = [to, from];
    const i0 = h.dates.findIndex(d => d >= from);
    let i1 = -1;
    for (let i = h.dates.length - 1; i >= 0; i--) if (h.dates[i] <= to) { i1 = i; break; }
    return { from, to, i0, i1, ok: i0 >= 0 && i1 > i0 };
}

function renderInventories(j) {
    const inv = j.refining?.inventories, el = document.getElementById('fc11');
    if (!inv || inv.status !== 'fresh') {
        // when the whole refining block failed its reason is on the block, not on the inventories
        el.innerHTML = missing(inv || (j.refining && j.refining.reason ? j.refining : { reason: 'available after the next pipeline run' }));
        return;
    }
    loadInvHistory();
    const st = FC.inv, mode = INV_MODES[st.mode], full = st.state === 'ready';
    const h = full ? st.hist : invFromSnapshot(inv);
    const w = invWindow(h);
    st.win = [w.from < h.dates[0] ? h.dates[0] : w.from, w.to > h.as_of ? h.as_of : w.to];
    const labels = w.ok ? h.dates.slice(w.i0, w.i1 + 1) : [];
    const sets = !w.ok ? [] : h.series.filter(s => s[mode.field]).map(s => {
        let data = s[mode.field].slice(w.i0, w.i1 + 1);
        if (st.mode === 'indexed') { const base = data.find(v => v != null); data = data.map(v => v == null || !base ? null : v / base * 100); }
        return { key: s.key, label: s.label, data, color: INV_COL[INV_KEYS.indexOf(s.key)] || '#a1a1aa' };
    }).filter(s => s.data.some(v => v != null));

    const rows = inv.table.map(r => `<tr><td style="text-align:left"><span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${INV_COL[INV_KEYS.indexOf(r.key)]};margin-right:6px"></span>${fcEsc(r.label)}</td>
        <td>${fcNum(r.latest_mbbl, 1)}M</td><td class="${fcCls(r.change_1w_mbbl)}">${fcNum(r.change_1w_mbbl, 2)}</td><td class="${fcCls(r.change_4w_mbbl)}">${fcNum(r.change_4w_mbbl, 2)}</td>
        <td class="${fcCls(r.change_26w_mbbl)}">${fcNum(r.change_26w_mbbl, 1)}</td><td class="${fcCls(r.vs_5y_pct)}">${fcPct(r.vs_5y_pct, 1)}</td><td>${r.days_of_supply == null ? '—' : fcNum(r.days_of_supply, 1)}</td></tr>`).join('');
    const flows = Object.values(inv.flows || {}).map(f => `<tr><td style="text-align:left">${fcEsc(f.label)}</td><td>${fcNum(f.latest_kbd / 1000, 2)}M b/d</td>
        <td>${fcNum(f.avg_4w_kbd / 1000, 2)}M</td><td class="${fcCls(f.change_1w_kbd)}">${fcNum(f.change_1w_kbd, 0)}k</td><td class="${fcCls(f.vs_5y_pct)}">${fcPct(f.vs_5y_pct, 1)}</td></tr>`).join('');
    const stats = sets.map(s => {
        const pts = s.data.map((v, i) => [v, labels[i]]).filter(p => p[0] != null);
        const lo = pts.reduce((a, b) => b[0] < a[0] ? b : a), hi = pts.reduce((a, b) => b[0] > a[0] ? b : a);
        const first = pts[0], last = pts[pts.length - 1], avg = pts.reduce((a, p) => a + p[0], 0) / pts.length;
        return `<tr><td style="text-align:left"><span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${s.color};margin-right:6px"></span>${fcEsc(s.label)}</td>
            <td>${mode.tick(first[0])}<div class="fc-muted">${first[1]}</div></td><td>${mode.tick(last[0])}<div class="fc-muted">${last[1]}</div></td>
            <td>${mode.tick(avg)}</td><td>${mode.tick(lo[0])}<div class="fc-muted">${lo[1]}</div></td><td>${mode.tick(hi[0])}<div class="fc-muted">${hi[1]}</div></td></tr>`; }).join('');
    const tot = inv.total_incl_spr;
    const tight = inv.table.filter(r => r.vs_5y_pct != null).sort((a, b) => a.vs_5y_pct - b.vs_5y_pct);
    const ctl = 'background:#18181b;border:1px solid #27272a;color:#e4e4e7;font-size:10px;border-radius:6px;padding:2px 6px;color-scheme:dark';
    const y0 = +h.dates[0].slice(0, 4), y1 = +h.as_of.slice(0, 4);
    const yearSel = st.range === 'custom' && st.from?.slice(4) === '-01-01' && st.to === st.from.slice(0, 4) + '-12-31' ? +st.from.slice(0, 4) : null;
    const years = Array.from({ length: y1 - y0 + 1 }, (_, i) => y1 - i).map(y => `<option value="${y}" ${y === yearSel ? 'selected' : ''}>${y}</option>`).join('');
    const status = full ? `history from ${fcEsc(h.start)}` : st.state === 'error' ? `full history unavailable (${fcEsc(st.error)}) · showing 26 weeks` : 'loading full history…';
    el.innerHTML = `
        <div class="flex flex-wrap justify-between items-center gap-2">
            <div class="fc-seg">${Object.entries(INV_MODES).map(([k, m]) => `<button class="${k === st.mode ? 'active' : ''}" onclick="invSet({mode:'${k}'})">${m.label}</button>`).join('')}</div>
            <div class="fc-muted">EIA weekly · week ending ${fcEsc(inv.as_of)} · ${status}</div>
        </div>
        <div class="flex flex-wrap items-center gap-2" style="margin-top:6px">
            <div class="fc-seg">${INV_RANGES.map(([k, l]) => `<button class="${k === st.range ? 'active' : ''}" onclick="invRange('${k}')">${l}</button>`).join('')}</div>
            <select style="${ctl}" onchange="invYear(this.value)" title="Show one calendar year" ${full ? '' : 'disabled'}><option value="">Year…</option>${years}</select>
            <input type="date" style="${ctl}" value="${st.win[0]}" min="${h.dates[0]}" max="${h.as_of}" onchange="invDate('from', this.value)" title="Range start" ${full ? '' : 'disabled'}>
            <span class="fc-muted">to</span>
            <input type="date" style="${ctl}" value="${st.win[1]}" min="${h.dates[0]}" max="${h.as_of}" onchange="invDate('to', this.value)" title="Range end" ${full ? '' : 'disabled'}>
        </div>
        <div class="fc-kpis">
            ${tot ? kpi('Total US stocks incl. SPR', fcNum(tot.latest_mbbl, 0) + 'M bbl', `1w ${fcNum(tot.change_1w_mbbl, 2)} · 4w ${fcNum(tot.change_4w_mbbl, 2)}`, fcCls(tot.change_4w_mbbl)) : ''}
            ${tight[0] ? kpi('Tightest vs 5y', fcPct(tight[0].vs_5y_pct, 1), fcEsc(tight[0].label), 'fc-neg') : ''}
            ${tight[1] ? kpi('2nd tightest', fcPct(tight[1].vs_5y_pct, 1), fcEsc(tight[1].label), tight[1].vs_5y_pct < 0 ? 'fc-neg' : '') : ''}
            ${tight.length ? kpi('Most ample', fcPct(tight[tight.length - 1].vs_5y_pct, 1), fcEsc(tight[tight.length - 1].label), 'fc-pos') : ''}
        </div>
        ${sets.length ? `<div class="fc-chart fc-chart-tall"><canvas id="fc11Chart"></canvas></div>`
                      : `<div class="fc-muted" style="padding:40px 0;text-align:center">No ${fcEsc(mode.label.toLowerCase())} data between ${fcEsc(st.win[0])} and ${fcEsc(st.win[1])}${full ? '' : ' in the 26-week snapshot'}.</div>`}
        <div class="fc-note">${mode.note} Click a legend item to hide or show a series. Data is weekly, so the shortest range is 1 month.</div>
        ${sets.length ? `<div class="fc-sub" style="margin-top:2px">${fcEsc(mode.label.toUpperCase())} · ${fcEsc(labels[0])} TO ${fcEsc(labels[labels.length - 1])}</div>
        <table class="fc-table"><tr><th style="text-align:left">Stock</th><th>Start</th><th>End</th><th>Average</th><th>Low</th><th>High</th></tr>${stats}</table>` : ''}
        <div class="fc-sub" style="margin-top:2px">LATEST WEEK</div>
        <table class="fc-table"><tr><th style="text-align:left">Stock</th><th>Latest</th><th>1w Δ (M)</th><th>4w Δ</th><th>26w Δ</th><th>vs 5y</th><th>Days supply</th></tr>${rows}</table>
        <div class="fc-sub" style="margin-top:2px">CRUDE BALANCE & DEMAND (flows)</div>
        <table class="fc-table"><tr><th style="text-align:left">Flow</th><th>Latest</th><th>4w avg</th><th>1w Δ</th><th>vs 5y</th></tr>${flows}</table>
        <div class="fc-note">${fcEsc(inv.note)}. Days of supply = stocks ÷ 4-week average demand (crude: ÷ refinery crude runs). Source: EIA Weekly Petroleum Status Report (API v2).</div>`;
    if (!sets.length) { if (FC.charts.fc11Chart) { FC.charts.fc11Chart.destroy(); delete FC.charts.fc11Chart; } return; }
    const ds = sets.map(s => ({ label: s.label, invKey: s.key, data: s.data, borderColor: s.color, backgroundColor: s.color, hidden: !!st.hidden[s.key],
        pointRadius: 0, pointHoverRadius: 3, borderWidth: ['dist_stocks', 'crude_stocks'].includes(s.key) ? 2.6 : 1.6, tension: labels.length > 300 ? 0 : 0.2 }));
    const ref = st.mode === 'vs5y' ? ['5-year norm', 0] : st.mode === 'indexed' ? ['Start (=100)', 100] : null;
    if (ref) ds.push({ label: ref[0], data: labels.map(() => ref[1]), borderColor: '#71717a', borderDash: [4, 4], borderWidth: 1, pointRadius: 0, pointHoverRadius: 0 });
    const long = labels.length > 60;
    fcChart('fc11Chart', { type: 'line', data: { labels, datasets: ds },
        options: { onResize: fcFitLegend,
                   plugins: { legend: { position: fcLegendPosition(document.getElementById('fc11Chart').parentElement.clientWidth), labels: { color: FC_COL.ink, boxWidth: 10, font: { size: 10 } },
                                        onClick: (e, item, legend) => { const d = legend.chart.data.datasets[item.datasetIndex];
                                            if (d.invKey) st.hidden[d.invKey] = !st.hidden[d.invKey];
                                            Chart.defaults.plugins.legend.onClick(e, item, legend); } },
                              tooltip: { mode: 'index', intersect: false, filter: c => c.dataset.invKey && c.parsed.y != null,
                                         callbacks: { title: c => c[0] ? `Week ending ${c[0].label}` : '', label: c => `${c.dataset.label}: ${mode.tick(c.parsed.y)}` } } },
                   scales: { x: axis({ ticks: { color: FC_COL.ink2, font: { size: 9 }, maxTicksLimit: 10, maxRotation: 0,
                                                callback: function (v) { const l = this.getLabelForValue(v); return long ? l.slice(0, 7) : l; } } }),
                             y: axis({ title: { display: true, text: mode.axis, color: FC_COL.ink2, font: { size: 9 } }, ticks: { color: FC_COL.ink2, font: { size: 9 }, callback: v => mode.tick(v) } }) } } });
}

// ---------------------------------------------------------------- full screen
let FC_FULL = null;
function toggleForecastFullscreen(id) {
    const back = document.getElementById('fcBackdrop');
    if (FC_FULL) {
        FC_FULL.classList.remove('fc-full');
        FC_FULL = null;
        back.classList.add('hidden');
        document.body.classList.remove('fc-lock');
    } else if (id) {
        FC_FULL = document.getElementById(id)?.closest('.rainbow-card');
        if (!FC_FULL) return;
        FC_FULL.classList.add('fc-full');
        back.classList.remove('hidden');
        document.body.classList.add('fc-lock');
    }
    setTimeout(() => Object.values(FC.charts).forEach(c => c && c.resize()), 60);
}

// ---------------------------------------------------------------- FAQ
function openForecastFaq(id) {
    const f = FC_FAQ[id];
    if (!f) return;
    document.getElementById('fcFaqTitle').textContent = f.title;
    const sec = (h, items) => `<h4>${h}</h4><ul>${items.map(x => `<li>${x}</li>`).join('')}</ul>`;
    document.getElementById('fcFaqBody').innerHTML = `<p>${f.what}</p>` + sec('How to read it', f.read) + sec('How to trade with it', f.trade)
        + sec('How hedge funds & quant desks use it', f.funds) + sec('Caveats', f.caveats);
    document.getElementById('fcFaqModal').classList.remove('hidden');
}
function closeForecastFaq() { document.getElementById('fcFaqModal').classList.add('hidden'); }
document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (!document.getElementById('fcFaqModal')?.classList.contains('hidden')) closeForecastFaq();
    else if (FC_FULL) toggleForecastFullscreen();
});
document.addEventListener('click', e => { if (e.target && e.target.id === 'fcFaqModal') closeForecastFaq(); });

const FC_FAQ = {
    fc13: { title: '13 · Edge lab — FAQ',
        what: 'Runs the published edges (short-term reversal, post-earnings drift, calendar effects, trend, 12-month momentum, volatility premium, overnight vs intraday, factor profile) against any ticker\'s own price history. For each one it says whether it is active now and whether it actually worked on that ticker. QUERY analyses any symbol without saving it; + TRACK adds it to the matrix.',
        read: ['<b>Matrix glyphs</b>: ▲ active and bullish, ▼ active and bearish, ● active with no direction, · inactive, – cannot be tested. Green/red/amber = the edge is active and measured on this ticker; grey = it is on but has no measured edge.',
               '<b>Short / Mid / Long</b>: one verdict per horizon, built only from tested edges that are active now.',
               '<b>Detail table</b>: click a ticker for its edges with current reading, trigger, status, action and evidence.',
               '<b>Evidence</b>: green = tested (beat the ticker\'s own baseline over 30+ cases), amber = thin (under 30), grey = none or not testable.'],
        trade: ['Trade only edges with green evidence. Treat grey as context.',
                'An active edge with grey evidence is background, not a reason to act.'],
        funds: ['Systematic desks run a book of small, published effects and size each by how well it has held up, not by how good the story is.'],
        caveats: ['Public edges are small and tend to decay after publication.',
                  'Testing many edges on many tickers produces some false passes by chance.',
                  'Results come from daily closes and ignore trading costs.'] },
    fc12: { title: '12 · Day scanner — FAQ',
        what: 'Live rule scan of SPY, GOOGL and any ticker you add with + ADD, from daily closes plus the current price (core/watch.py), rechecked every 5 minutes during the session. ↻ REFRESH pulls fresh quotes and history on demand; the list is saved on this Mac. One rule can produce a trade; the rest are context. Every row is graded against that symbol\'s own history: was the price higher 5 trading days later more often than on a normal uptrend day?',
        read: ['<b>Dip in an uptrend</b>: 2-day RSI under 10 (SPY) or 5 (GOOGL) with the price above its 200-day average. The trigger column is the exact price that fires it today. Added tickers use 10 if that tested well on their own history, otherwise 5.',
               '<b>Evidence</b> decides everything: a ticker whose dips did not beat a normal uptrend day stays grey and never says Buy, even when its trigger is hit.',
               '<b>Context rows</b>: overbought, new 20-day high, 3 down closes, lower Bollinger band. Grey evidence means the state has no measured edge.',
               '<b>Live watch positions</b>: paper record of each SPY spread the alert named, with its live value and the sell date.'],
        trade: ['Trade only when "Dip in an uptrend" is FIRED, which can only happen 2:30–3:00 PM CT. SPY: one call spread of at most $300, sold after 5 trading days. GOOGL: shares, because its option bid/ask eats most of a narrow spread\'s edge.',
                'Overbought and breakout rows firing is a reason to wait, not to buy calls: on both symbols the following 5 days were no better than normal.',
                'GOOGL is blocked when earnings fall inside the 5-day hold.'],
        funds: ['Short-term mean reversion inside an uptrend is one of the most replicated equity-index effects; desks size it small and trade it often.',
                'A rule sheet with a fixed entry, structure and exit is what makes a small edge repeatable.'],
        caveats: ['Expect about 8 SPY and 4 GOOGL signals a year. Most days the correct output is nothing.',
                  'Option results behind the SPY structure are modelled, not real fills. The worst modelled trade lost about 80% of the debit.',
                  'Noon entries were tested and showed no edge on either symbol, so there is no mid-day rule.'] },
    fc0: { title: '0 · Signal watch — FAQ',
        what: 'One table of every trigger the other cards compute, for SPY and SLV together: the current reading, the level that fires it, whether it has fired, and the rule-based action. It is plain code (core/forecast.py, signal_watch) evaluated on the latest pipeline run — no AI.',
        read: ['<b>Now / Trigger</b>: how far the reading is from firing.',
               '<b>Status</b>: FIRED means the condition is true as of the latest run; ▲ bullish, ▼ bearish, no arrow = it changes structure or size, not direction.',
               '<b>Action</b>: instrument, tenor and size when fired. Hover a waiting row to see what it would do.',
               '<b>Evidence</b>: the rule\'s own backtest. Green = tested (30+ cases, 5+ points better than baseline), amber = small sample, grey = no measured edge.',
               'Hover any cell for its definition and the card and field it comes from.'],
        trade: ['Only act on rows whose action starts with "Buy". Fired rows with grey evidence are confirmation only.',
                'Tenor follows the tested horizon: 4-week rules get 30–60 DTE so the option outlives the test window.',
                'The option-pricing row decides calls/puts outright versus spreads; dealer gamma and macro rows adjust size.'],
        funds: ['Systematic desks keep exactly this kind of rule sheet: signal, state, pre-agreed action, and the evidence behind it.',
                'Pre-committing the action before the signal fires removes discretion at the moment of the trade.'],
        caveats: ['Price-driven rows follow live quotes; positioning, option pricing and macro only change when the pipeline runs.',
                  'Intraday rules for SPY and GOOGL live in card 12, the day scanner.',
                  'Backtests are short (about 3–5 years) and overlap; "tested" is a low bar, not proof.',
                  'Actions are generic templates — check the live quote and spread before placing anything.'] },
    fc1: { title: '1 · Market-implied range — FAQ',
        what: 'The probability distribution for SPY/SLV on each horizon (1D, 1W, 1M, 1Y) that is <b>implied by option prices</b>. It is extracted with the Breeden-Litzenberger method: the second derivative of call prices across strikes is the market\'s risk-neutral density. In plain terms: it is what the options market is charging for each possible outcome.',
        read: ['<b>68% / 90% range</b>: the price band the market assigns ~68% / ~90% probability to by that date (roughly ±1σ / ±1.65σ).',
               '<b>±1σ move</b>: the "expected move" — option sellers are paid to cover moves up to about this size.',
               '<b>P(finish higher)</b>: probability of ending above today\'s price. It includes interest-rate drift, so values slightly above 50% are normal.',
               '<b>Skew</b>: negative = the downside tail is fatter (crash protection is expensive). SPY is almost always negative; SLV swings.',
               '<b>Density chart</b>: the dashed lines are spot and dealer zero-gamma. "P(above)" shows the odds of finishing above the call wall / put wall / zero gamma.'],
        trade: ['Use the 68% range as the market\'s "fair" range: selling premium outside it is betting on a quieter market; buying options is betting on a bigger move than priced.',
                'Place stops/targets relative to the ±1σ move instead of round numbers — a stop inside the 1-day 1σ gets hit by noise.',
                'Compare with card 2: if your volatility forecast is well below the implied move, premium-selling structures (iron condors, short strangles, call overwrites on SLV) have an edge; if above, long straddles/strangles do.',
                'Strike selection: pick strikes by probability (e.g. a 16% probability short strike ≈ the 84th percentile).'],
        funds: ['Volatility and relative-value desks treat this as the market\'s price of risk and trade the gap between it and their own forecast (volatility risk premium).',
                'Macro funds read the <b>skew</b> and tail probabilities (P(−10%)) as a gauge of crash hedging demand; extreme put skew often marks fear peaks.',
                'Banks and the Fed publish similar risk-neutral densities; CME\'s CVOL indices summarize the same surface.'],
        caveats: ['These are <b>risk-neutral</b> odds: they include a risk premium, so real-world downside probabilities are usually lower than priced.',
                  'Tails beyond the last listed strikes are extrapolated. Horizons marked * are rescaled from the nearest listed expiry.',
                  'Pre-market quotes can be stale; the model uses bid/ask mids only, never stale last prices.'] },
    fc2: { title: '2 · Volatility forecast (HAR-RV) — FAQ',
        what: 'A statistical forecast of how much SPY/SLV will actually move (realized volatility) over the next day, week, month and year, using the HAR model (Corsi 2009): tomorrow\'s volatility is a weighted blend of yesterday\'s, last week\'s and last month\'s. It is compared with the volatility options are pricing.',
        read: ['<b>HAR vol</b>: forecast annualized volatility. ±1σ converts it to an expected % move for that horizon.',
               '<b>Implied</b>: what options charge (from card 1). <b>VRP</b> = implied − forecast.',
               '<b>Read</b>: "options rich" (VRP well above 0) favors selling premium; "options cheap" favors buying it.',
               '<b>Out-of-sample</b> line: how the model did on the last 20% of history it never saw, vs a naive "same as last month" guess. Negative = the naive guess was better.'],
        trade: ['Size positions to forecast volatility (risk parity per trade): when forecast vol doubles, halve size.',
                'Premium sellers: act when VRP is clearly positive AND the regime is calm (card 6); step aside when forecast vol is rising faster than implied.',
                'Premium buyers: options are "cheap" when implied < forecast — long straddles or calendar spreads can work.'],
        funds: ['HAR is the industry-standard benchmark for volatility forecasting; vol-arb desks trade implied vs forecast realized every day.',
                'Risk desks and CTAs use realized-vol forecasts to set leverage (volatility targeting) — the same logic drives card 8\'s vol-control flows.',
                'The variance risk premium (implied minus realized) is one of the most persistent documented premia; funds harvest it with systematic option selling.'],
        caveats: ['Volatility jumps on news (FOMC, CPI, earnings, geopolitics) that no statistical model sees coming — check card 7.',
                  'For SLV the model currently does not beat the naive benchmark at short horizons; treat it as one input.',
                  'VRP can stay "rich" for a long time and then pay out all at once in a crash; short-vol needs defined risk.'] },
    fc3: { title: '3 · Trend / CTA model — FAQ',
        what: 'A replication of how trend-following funds (CTAs / managed futures) position: it looks at 1-, 3-, 6- and 12-month returns scaled by volatility (time-series momentum, Moskowitz-Ooi-Pedersen 2012) and averages them into an exposure from −1 (fully short) to +1 (fully long).',
        read: ['<b>Direction / exposure</b>: LONG, SHORT or FLAT and how strong.',
               '<b>Flip level</b>: the price at which each lookback\'s signal would flip tomorrow. Levels close to spot are where systematic selling (or buying) can start.',
               '<b>Exposure after move</b>: how exposure would change after a ±2% / ±5% day — a proxy for how much trend funds would add or cut.',
               '<b>Historical odds</b>: how often the price rose over each horizon when the signal pointed the same way (withheld when history is too short).'],
        trade: ['Trade with the trend on longer horizons; use flip levels as risk lines — a break of the nearest flip level often accelerates moves.',
                'When exposure is extreme and price nears a flip level, expect mechanical selling if it breaks (and short-covering rallies on the way back).',
                'Combine with card 8: CTA selling plus vol-control deleveraging plus short dealer gamma is the classic "air pocket" setup.'],
        funds: ['Managed-futures funds (AQR, Man AHL, Winton and many CTAs) run variations of exactly this signal across futures markets.',
                'Sell-side desks (e.g. Goldman, Nomura) publish daily "CTA trigger levels" estimated this way; macro traders watch them for flow-driven moves.',
                'Trend following is valued for "crisis alpha": it tends to profit in extended sell-offs.'],
        caveats: ['Trend signals whipsaw in sideways markets; the 1-day and 1-week hit rates are close to a coin flip.',
                  'This is a replication; actual CTA positions are not reported daily.',
                  '1-year odds need many independent years of data — they are hidden when history is too short.'] },
    fc4: { title: '4 · Positioning (CFTC COT, SLV trust) — FAQ',
        what: 'Who is holding futures, from the CFTC Commitments of Traders report (positions as of Tuesday, released Friday). For silver: managed money (hedge funds/CTAs), producers/merchants and swap dealers. For the S&P: leveraged funds, asset managers and dealers. Plus the physical silver held by the SLV trust.',
        read: ['<b>Net</b>: long minus short contracts; <b>% OI</b>: as a share of all open contracts.',
               '<b>COT index</b>: where today\'s net sits in its 3-year range (0 = most short, 100 = most long).',
               '<b>Read</b>: ≥90 "crowded long" (contrarian bearish), ≤10 "crowded short" (contrarian bullish).',
               '<b>History note</b>: what happened over the next 4 weeks when the index was at extremes in the past.',
               '<b>SLV ounces</b>: rising = investment demand pulling physical metal into the trust.'],
        trade: ['Extremes matter, middles don\'t: crowded positioning + a catalyst = sharp reversals (squeezes).',
                'Silver: managed money short at extremes while producers are unusually light on hedges has preceded rallies.',
                'Use it as a filter: avoid adding to a trade when everyone is already in it.'],
        funds: ['Commodity funds and macro desks track COT weekly as a sentiment and crowding gauge.',
                'The "COT index" is a classic technique (popularized by Larry Williams); quant funds use net-position z-scores as a contrarian factor.',
                'Physical/ETF flows (SLV, GLD) are watched as the "real money" demand signal behind precious-metal moves.'],
        caveats: ['Data is 3 days old at release and weekly — it misses fast moves.',
                  'Positions can stay extreme for months; it is a timing aid, not a trigger.',
                  'Futures positioning is only part of the market (OTC, options and physical are separate).'] },
    fc5: { title: '5 · Silver fair value — FAQ',
        what: 'A relative-value model for silver: a regression of the silver price on gold, 10-year real yields (TIPS), the dollar index, copper and US industrial production. It estimates where silver "should" trade given its drivers and how far it is from that.',
        read: ['<b>Fair value / gap</b>: the model price and how rich (+) or cheap (−) silver is versus it.',
               '<b>z</b>: the gap in standard deviations; beyond ±1 has historically tended to close.',
               '<b>Half-life</b>: how many weeks it typically takes to close half the gap.',
               '<b>Driver table</b>: sensitivity (β) and significance (|t| ≥ 2 = meaningful). Real yields up usually pushes silver down; gold and copper up push it up.',
               '<b>Gold/silver ratio</b>: high = silver cheap relative to gold.'],
        trade: ['Mean-reversion: lean long when silver is cheap (z < −1) and drivers are stable; lean short/hedged when rich (z > +1).',
                'Watch the drivers: if real yields fall and gold rises, fair value rises even if silver hasn\'t moved yet.',
                'Pair idea: trade silver vs gold when the gold/silver ratio z-score is extreme.'],
        funds: ['Commodity and macro funds build exactly these "fair value" or "residual" models and trade the residual.',
                'Real yields are the main macro driver precious-metal desks watch; copper and industrial production capture silver\'s industrial side.',
                'Relative-value desks trade the gold/silver ratio as a spread.'],
        caveats: ['It is a levels regression on trending series — a gauge, not a causal model; fair value can move toward price instead of the other way.',
                  'Uses SI futures (COMEX), not the SLV share price.',
                  'Structural breaks (supply shocks, tariffs) can make the relationship stale.'] },
    fc6: { title: '6 · Macro regime — FAQ',
        what: 'The economic backdrop for the next year: the New York Fed\'s yield-curve recession model, the Chicago Fed financial-conditions index (NFCI), the Sahm recession rule, credit spreads and real yields.',
        read: ['<b>Recession probability (12m)</b>: NY Fed probit on the 10y−3m spread; above ~30% has preceded most recessions.',
               '<b>NFCI</b>: above 0 = tighter than average financial conditions; rising = tightening.',
               '<b>Sahm rule</b>: ≥ 0.50 has marked the start of every modern recession.',
               '<b>HY OAS</b>: junk-bond spread; widening = credit stress.',
               '<b>Regime</b>: EXPANSION / LATE CYCLE / STRESS summarizes the flags.'],
        trade: ['Regime sets the playbook: buy dips in EXPANSION, hedge and shorten duration in LATE CYCLE, protect capital in STRESS.',
                'For silver: falling real yields and easing conditions are supportive; a recession hits the industrial side.',
                'Use it to size overall risk, not to time entries.'],
        funds: ['Macro funds and asset allocators run regime models to switch between risk-on and risk-off allocations.',
                'The yield curve, credit spreads and financial-conditions indices are standard inputs in bank and hedge-fund recession dashboards.',
                'Risk-parity and multi-asset funds adjust leverage by regime.'],
        caveats: ['These are slow signals (months to quarters); markets often move before the data turns.',
                  'The yield-curve model gave long early warnings in some cycles.',
                  'A single flag is noise; look for several turning together.'] },
    fc7: { title: '7 · Calendar & event drift — FAQ',
        what: 'Historical price behavior around scheduled events for SPY/SLV: the day before and day of Fed (FOMC) decisions, CPI releases, monthly options expiration (OPEX) week and the turn of the month, plus average returns by weekday, and which of these fall in the next 7 days.',
        read: ['<b>Mean / hit</b>: average return and share of up moves on those days.',
               '<b>|move|</b>: average absolute move — event days are usually more volatile even when the average is flat.',
               '<b>t</b>: statistical strength; |t| ≥ 2 (highlighted) is meaningful, below that is within noise.',
               '<b>Calendar drift 7d</b>: the sum of active effects vs a normal week.'],
        trade: ['Expect larger moves on CPI and FOMC days: widen stops, avoid selling short-dated premium into the event, or buy it cheaply beforehand.',
                'Documented drifts (e.g. the pre-FOMC drift, turn-of-month) are small edges — use them to time entries, not as standalone trades.',
                'OPEX week can pin prices near large open-interest strikes (see card 1 and the dealer walls).'],
        funds: ['The pre-FOMC drift was documented by NY Fed economists (Lucca & Moench) and is traded by systematic funds.',
                'Event-volatility traders buy or sell options around CPI/FOMC based on how implied moves compare with history.',
                'Quant funds run calendar and seasonality factors as small, diversifying signals.'],
        caveats: ['Samples are small (dozens of events); many effects are not statistically significant.',
                  'Well-known anomalies weaken after publication.',
                  'CPI days here are dated from FRED\'s CPI release schedule.'] },
    fc8: { title: '8 · Mechanical flows — FAQ',
        what: 'Estimates of forced, non-discretionary buying and selling: volatility-targeting funds, leveraged ETFs that rebalance every day, option dealers hedging their gamma, and trend-followers (from card 3).',
        read: ['<b>Vol-control exposure</b>: how invested volatility-targeting funds are (1.0x = normal). Rising realized volatility forces them to sell.',
               '<b>If tomorrow ±x%</b>: estimated $ flow from vol-control funds after such a move.',
               '<b>Leveraged ETF rebalance / 1%</b>: what 2x/3x/inverse ETFs must buy after up days or sell after down days, near the close.',
               '<b>Dealer hedge / +1%</b>: short gamma (negative net GEX) means dealers buy rallies and sell dips — amplifying moves; long gamma dampens them.',
               '<b>CTA exposure after move</b>: how trend followers would react.'],
        trade: ['When several flows point the same way (short gamma + vol-control selling + CTA near a flip), moves can extend — don\'t fade them early.',
                'Long dealer gamma and low realized vol usually mean range-bound, mean-reverting trading — fade extremes.',
                'Leveraged-ETF rebalancing concentrates in the last 30 minutes; big up/down days often see closing-auction imbalances in the same direction.'],
        funds: ['Bank strategists publish estimates of vol-control, risk-parity, CTA and leveraged-ETF flows; macro and equity traders use them to anticipate supply/demand.',
                'Market makers and volatility funds track aggregate dealer gamma (GEX) closely.',
                'These flows explain much of the "air pocket" behavior in sell-offs (e.g. February 2018, August 2024).'],
        caveats: ['Industry AUMs are estimates; treat the $ figures as orders of magnitude.',
                  'Dealer positioning assumes dealers are long calls and short puts — a convention, not observed data.',
                  'Vol-control only applies to the S&P model.'] },
    fc9: { title: '9 · Forecast scorecard — FAQ',
        what: 'Accountability for all the models: every live pipeline run logs its forecasts, and they are graded automatically once their date passes. Walk-forward backtests show how each model did on history.',
        read: ['<b>Hit</b>: share of correct direction calls.',
               '<b>In 68%</b>: share of outcomes inside the forecast 68% range — should be close to 68%. Much higher = ranges too wide; much lower = overconfident.',
               '<b>Brier</b>: accuracy of probability forecasts; 0.25 = coin flip, lower is better.',
               '<b>Backtests</b>: out-of-sample results; * = too few independent windows to trust.'],
        trade: ['Trust and size up the models with a proven record at the horizon you trade; ignore the ones that don\'t beat a coin flip.',
                'Recheck after every few dozen graded forecasts — models drift as regimes change.'],
        funds: ['Quant funds track every signal\'s live hit rate, calibration and decay, and cut signals that stop working.',
                'Calibration (does "70%" happen 70% of the time?) is how professional forecasters and risk desks are judged.',
                'Walk-forward testing avoids the look-ahead bias that makes naive backtests look better than reality.'],
        caveats: ['Early on there are few graded forecasts; results are noisy until dozens accumulate.',
                  'Hit rates ignore payoff size — a 45% hit rate can still make money with good risk/reward.'] },
    fc11: { title: '11 · EIA inventories — FAQ',
        what: 'Every major US petroleum stockpile from the EIA Weekly Petroleum Status Report on one chart, for any period back to 1982: commercial crude, the Strategic Petroleum Reserve (SPR), Cushing (the WTI delivery hub), gasoline, distillate/diesel, jet fuel, propane and residual fuel — plus the crude balance (production, imports, exports, refinery runs) and product demand.',
        read: ['<b>vs 5y avg</b> (default): each stock as % above/below the average for the same week over the previous five years. Below 0 = tighter than normal for the season; this removes the normal seasonal build/draw pattern.',
               '<b>Indexed</b>: every series set to 100 at the start of the window — shows which stocks are building or drawing fastest.',
               '<b>Levels</b>: raw million barrels (crude and SPR dwarf the product stocks).',
               '<b>Days of supply</b> (its own view): stocks ÷ 4-week average demand, for crude (÷ refinery runs), distillate, gasoline and jet fuel — the cushion before shortages; fewer days = more price-sensitive. Available from 1991.',
               '<b>Range</b>: 1M to MAX, a single calendar year from the Year menu, or any two dates. The table under the chart gives start, end, average, low and high for the range shown.',
               '<b>Flows table</b>: production, imports, exports and demand explain <i>why</i> stocks are moving.'],
        trade: ['The Wednesday EIA release (10:30 ET) moves crude, gasoline and diesel futures within minutes: a draw bigger than expected is bullish, a build bearish.',
                'Cushing below ~20M bbl is a classic squeeze warning for the front WTI spread (backwardation); watch it into contract expiry.',
                'Distillate and gasoline well below the 5-year range going into winter / driving season raise the odds of crack-spread spikes (card 10).',
                'Falling SPR = less government buffer for supply shocks; policy refills add demand.',
                'For SPY and SLV: energy tightness feeds inflation expectations, real yields and Fed pricing (cards 5 and 6).'],
        funds: ['Commodity desks model the weekly balance (production + imports − exports − runs) and trade the surprise vs consensus on Wednesdays.',
                'The "% vs 5-year average" framing is the industry standard for judging tightness; banks publish the same charts in their weekly oil notes.',
                'Hedge funds combine inventories with refinery outages (card 10), satellite tank data and tanker tracking to anticipate the EIA print before it lands.',
                'Cushing stocks and crude time-spreads are watched together by storage and spread traders.'],
        caveats: ['Weekly EIA data is noisy (imports and exports swing with ship timing) and gets revised; look at 4-week trends.',
                  'The 5-year window can include unusual years, which shifts the "normal".',
                  'National totals hide regional imbalances (see the East Coast vs Gulf Coast distillate split on card 10).'] },
    fc10: { title: '10 · Diesel & refining — FAQ',
        what: 'The refined-products layer of the oil market: refiners\' diesel and gasoline margins (crack spreads), US refinery run rates and fuel inventories from EIA, the seasonal maintenance calendar, speculative positioning in diesel futures, and a live tracker of Texas refinery outages from state emission-event filings.',
        read: ['<b>Diesel crack</b>: diesel futures × 42 − crude ($/bbl). High and above its seasonal norm = diesel is scarce relative to crude.',
               '<b>3-2-1 crack</b>: overall refining margin (2 gasoline + 1 diesel per 3 crude).',
               '<b>Runs vs 5y</b>: refinery utilization vs the same week in the past 5 years, by region (Gulf Coast = PADD 3).',
               '<b>Distillate stocks / days of supply</b>: below the 5-year range = tight; fewer days = less cushion.',
               '<b>Season</b>: spring (Feb–May) and fall (Sep–Nov) turnaround windows; the path shows typical utilization ahead.',
               '<b>Refinery events</b>: UNPLANNED = upsets/trips, MAINTENANCE = scheduled work. MAJOR = unplanned at a large refinery on a key unit (crude, FCC, hydrocracker, diesel hydrotreater). Hover a row for the operator\'s stated cause.'],
        trade: ['Rising diesel cracks + low stocks + fall maintenance + unplanned outages = supply squeeze risk: diesel and heating-oil futures, refiner equities (VLO, MPC, PSX) and freight/inflation-sensitive trades react.',
                'For SPY: diesel spikes feed trucking and freight costs, then CPI and rate expectations — a hawkish risk for equities.',
                'For silver: indirect, through inflation expectations and real yields (card 5).',
                'An unplanned outage at a 500k+ bpd refinery on a key unit can move Gulf Coast diesel and cracks within hours — watch the alerts.'],
        funds: ['Energy traders and commodity funds (Vitol, Trafigura, bank desks) trade crack spreads directly and watch utilization and stocks every Wednesday (EIA).',
                'They pay for unit-level outage feeds (Industrial Info, Wood Mackenzie remote sensing) and scrape state filings like TCEQ — the same public signal this card uses.',
                'Turnaround calendars are used to position ahead of maintenance-driven tightness in spring and fall.',
                'Macro and equity desks use diesel as a real-time read on freight, industrial activity and inflation pass-through.'],
        caveats: ['Only Texas outages are covered (a large share of Gulf Coast capacity); Louisiana and other states have no public real-time feed here yet.',
                  'Emission events include minor episodes; not every filing means lost barrels. Capacity shown is the whole refinery\'s, not the affected unit\'s.',
                  'Exact planned turnaround dates are only available from paid services; the season model is a statistical proxy.',
                  'EIA data is weekly (Wednesday); futures are front-month continuous contracts.'] },
};

// hook into the existing tab router
(function () {
    const orig = window.switchTab;
    window.switchTab = function (name) {
        if (orig) orig(name);
        if (name === 'forecast') loadForecast();
    };
})();
