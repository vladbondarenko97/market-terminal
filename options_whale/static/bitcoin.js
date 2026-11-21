// ==========================================================================================
// BITCOIN tab: B1 verdict, B2 price and cycle, B3 on-chain, B4 leverage and options, B5 flows.
// Data: /api/bitcoin and /api/bitcoin/history (stored in the latest pipeline snapshot; nothing is fetched live).
// Shares the helpers of forecast.js (fcNum, fcEsc, kpi, fcChart, axis, FC_COL). Conditions, not a trade call.
// ==========================================================================================
const BTC = { data: null, hist: null, full: null };
const btcNum = (v, d = 0, pre = '') => (v === null || v === undefined) ? '—' : pre + fcNum(v, d);
const btcOk = p => p && p.status && p.status !== 'missing';
const btcMissing = (p, what) => `<div class="fc-muted">${fcEsc(what || 'Unavailable')}: ${fcEsc((p && (p.reason || p.message)) || 'not in this snapshot')}</div>`;
const btcNote = (p, keys) => keys.filter(k => p && p.missing && p.missing[k]).map(k => `<div class="fc-note">${fcEsc(k)}: missing, ${fcEsc(p.missing[k])}</div>`).join('');
const btcLag = p => `as of ${p.as_of} (${p.lag_days} days old, BGeometrics free)`;      // a delayed reading always says how old it is
const btcBar = (id, labels, sets) => fcChart(id, { type: 'bar', data: { labels, datasets: sets },
    options: { scales: { x: axis({ stacked: true, ticks: { color: FC_COL.ink2, font: { size: 9 }, maxTicksLimit: 8 } }), y: axis({ stacked: true }) } } });
const btcDay = t => new Date(t * 864e5).toISOString().slice(0, 10);

async function loadBitcoin() {
    try {
        const [j, h] = await Promise.all([fetchJson(`${API_BASE}/api/bitcoin`), fetchJson(`${API_BASE}/api/bitcoin/history`)]);
        if (j.status !== 'success') throw new Error(j.message || 'load failed');
        BTC.data = j.data; BTC.hist = h.status === 'success' ? h.data : null;
        const r = j.run || {};
        document.getElementById('btcRunInfo').textContent = `run ${r.run_id || '—'} · ${r.generated_local || ''}`
            + (Object.keys(j.data.source_errors || {}).length ? ` · ${Object.keys(j.data.source_errors).length} source issue(s)` : '');
        [btcVerdict, btcPrice, btcOnchain, btcLeverage, btcFlows].forEach(fn => {
            try { fn(j.data); } catch (e) { console.error(fn.name, e); }
        });
    } catch (e) {
        for (const i of [1, 2, 3, 4, 5]) {
            const b = document.getElementById(`b${i}`);
            if (b) b.innerHTML = `<div class="fc-neg">Bitcoin data unavailable: ${fcEsc(e.message)}</div>`;
        }
    }
}

function btcRows(rows) {
    const cls = r => r.status === 'fired' ? 'fc-warn' : r.status === 'missing' ? 'fc-neg' : 'fc-muted';
    return `<table class="fc-table fc-sig"><tr><th>Row</th><th>Reading</th><th>Trigger</th><th>Status</th><th>Evidence</th></tr>${rows.map(r => `<tr>
        <td>${fcEsc(r.name)}</td>
        <td title="${fcEsc(r.reading || r.reason || '')}">${fcEsc(r.status === 'missing' ? 'missing: ' + (r.reason || '') : r.reading || '')}</td>
        <td>${fcEsc(r.trigger)}</td><td class="${cls(r)}">${r.status === 'fired' ? 'FIRED' : r.status}</td>
        <td class="${r.graded ? 'fc-pos' : 'fc-muted'}" title="Only rows graded on history vote. Grading is phase 4 of the plan.">${fcEsc(r.evidence)}</td></tr>`).join('')}</table>`;
}
function btcVerdict(d) {
    const v = d.verdict, rows = d.rows || [];
    if (!v) { document.getElementById('b1').innerHTML = btcMissing(d, 'No verdict'); return; }
    const big = v.call.startsWith('BUY') ? 'fc-pos' : v.call.startsWith('SELL') ? 'fc-neg' : 'fc-warn';
    document.getElementById('b1').innerHTML = `
        <div class="fc-kpis">${kpi('Verdict', fcEsc(v.call), '', big)}${kpi('Rows available', `${v.available} of ${v.total}`)}
            ${kpi('Bottom rows fired', `${v.bottom_fired} of ${v.bottom_total}`, `${v.bottom_votes} graded`)}${kpi('Top rows fired', `${v.top_fired} of ${v.top_total}`, `${v.top_votes} graded`)}
            ${kpi('Rows graded', v.graded_rows, 'on history')}${kpi('As of', fcEsc(d.as_of || '—'))}</div>
        <div class="fc-note" style="font-size:11px;margin:8px 0">${fcEsc(v.text)}</div>
        ${v.flip ? `<div class="fc-note">${fcEsc(v.flip)}</div>` : ''}
        <div class="fc-sub" style="margin-top:8px">BOTTOM CHECKLIST (BUY: 3 of 6 starts, 5 of 6 adds)</div>${btcRows(rows.filter(r => r.side === 'bottom'))}
        <div class="fc-sub" style="margin-top:8px">TOP CHECKLIST (SELL / trim: 3 of 5)</div>${btcRows(rows.filter(r => r.side === 'top'))}
        <div class="fc-note" style="margin-top:8px">Forward-return table of the verdict (BTC after 30 / 90 / 365 days per past verdict): missing, not built yet (phase 4). Not financial advice: conditions and their history, never a price call.</div>`;
}

function btcLine(id, labels, sets, extra = {}) {
    fcChart(id, { type: 'line', data: { labels, datasets: sets }, options: { scales: { x: axis({ ticks: { color: FC_COL.ink2, font: { size: 9 }, maxTicksLimit: 8 } }), y: axis(extra) } } });
}
function btcPrice(d) {
    const p = d.price, el = document.getElementById('b2');
    if (!btcOk(p)) { el.innerHTML = btcMissing(p, 'Price & cycle'); return; }
    const h = p.halving || {}, dd = p.drawdown_pct;
    el.innerHTML = `<div class="fc-kpis">${kpi('Price', btcNum(p.price, 0, '$'), fcEsc(p.as_of))}${kpi('From all-time high', fcPct(dd), `ATH ${btcNum(p.ath, 0, '$')} on ${fcEsc(p.ath_date)}`, fcCls(dd))}
        ${kpi('200-week MA', btcNum(p.ma_200w, 0, '$'), p.price_vs_ma_200w_pct == null ? '' : `price ${fcPct(p.price_vs_ma_200w_pct)} vs it`)}
        ${kpi('Realized price', btcNum((d.onchain || {}).realized_price, 0, '$'), 'on-chain average cost')}${kpi('Mayer multiple', btcNum(p.mayer, 2), 'price / 200-day MA')}
        ${kpi('Halving clock', `${btcNum(h.days_since)} d since`, `next about ${fcEsc(h.next_estimate)} (estimate, ${btcNum(h.days_to_next)} d)`)}</div>
        ${btcNote(p, ['ma_200w', 'mayer'])}
        ${BTC.hist ? '<div class="fc-chart fc-chart-tall"><canvas id="b2Chart"></canvas></div>' : btcMissing(null, 'History chart')}
        <div class="fc-note">Price source: ${fcEsc(p.source)}. 200-week MA = mean of the last 1,400 daily closes. Past cycle bottoms are not marked yet.</div>`;
    const H = BTC.hist;
    if (H) btcLine('b2Chart', H.t.map(btcDay), [
        { label: 'Price', data: H.price, borderColor: FC_COL.blue, pointRadius: 0, borderWidth: 1.5 },
        { label: '200-week MA', data: H.ma_200w, borderColor: FC_COL.orange, pointRadius: 0, borderWidth: 1.2 },
        { label: 'Realized price', data: H.realized_price, borderColor: FC_COL.aqua, pointRadius: 0, borderWidth: 1.2 }], { type: 'logarithmic' });
}
function btcOnchain(d) {
    const o = d.onchain, el = document.getElementById('b3');
    if (!btcOk(o)) { el.innerHTML = btcMissing(o, 'On-chain') + btcDelayed(d.onchain_delayed); btcDelayedCharts(d.onchain_delayed); return; }
    const hr = o.hash_ribbons || {};
    el.innerHTML = `<div class="fc-kpis">${kpi('MVRV Z-score', btcNum(o.mvrv_z, 2), '≤ 0.5 bottom zone · ≥ 6 top zone')}${kpi('MVRV', btcNum(o.mvrv, 2), 'market / realized cap')}
        ${kpi('Realized price', btcNum(o.realized_price, 0, '$'))}${kpi('Hash ribbons', fcEsc(hr.state || 'missing'), hr.state ? `30d ${btcNum(hr.hash_30d_eh)} vs 60d ${btcNum(hr.hash_60d_eh)} EH/s` : fcEsc(hr.reason || ''))}</div>
        ${o.status === 'stale' ? `<div class="fc-warn">Stale: ${fcEsc(o.reason)}</div>` : ''}${btcNote(o, ['mvrv_z'])}
        ${BTC.hist ? '<div class="fc-chart"><canvas id="b3Chart"></canvas></div>' : ''}
        <div class="fc-note">${fcEsc(o.formula)}. Coin Metrics' free API withholds CapRealUSD, so the realized cap is derived from two free metrics. As of ${fcEsc(o.as_of)}.</div>
        ${Object.entries(o.unavailable || {}).map(([k, v]) => `<div class="fc-note">${fcEsc(k)}: missing, ${fcEsc(v)}</div>`).join('')}${btcDelayed(d.onchain_delayed)}`;
    btcDelayedCharts(d.onchain_delayed);
    const H = BTC.hist;
    if (H) {
        const lab = H.t.map(btcDay);
        btcLine('b3Chart', lab, [{ label: 'MVRV Z', data: H.mvrv_z, borderColor: FC_COL.violet, pointRadius: 0, borderWidth: 1.4 },
            { label: '0', data: lab.map(() => 0), borderColor: FC_COL.aqua, borderDash: [4, 4], pointRadius: 0, borderWidth: 1 },
            { label: '6', data: lab.map(() => 6), borderColor: FC_COL.orange, borderDash: [4, 4], pointRadius: 0, borderWidth: 1 }]);
    }
}

// SOPR, STH SOPR and the short-term-holder cost basis come from BGeometrics' free API, which serves them about six days late: each carries its own as-of date
// and its age, and the cards say so. A delayed value is never presented as today's.
function btcDelayed(g) {
    if (!g || g.status === 'missing') return btcMissing(g, 'SOPR and STH cost basis (BGeometrics)');
    const one = (p, label, fmt, sub) => btcOk(p) ? kpi(label, fmt(p.value), `${btcLag(p)}${sub ? ' · ' + sub : ''}`) : '';
    const px = (g.sth_cost_basis || {}).price, c = (g.sth_cost_basis || {}).value;
    const rel = btcOk(g.sth_cost_basis) && px && c ? `BTC ${fcPct((px / c - 1) * 100)} vs it on that date` : '';
    return `<div class="fc-sub" style="margin-top:10px">SOPR AND SHORT-TERM-HOLDER COST BASIS · DELAYED</div>
        <div class="fc-kpis">${one(g.sopr, 'SOPR', v => btcNum(v, 3), '1.0 = break-even')}${one(g.sth_sopr, 'STH SOPR', v => btcNum(v, 3), 'coins under 155 days')}${one(g.sth_cost_basis, 'STH cost basis', v => btcNum(v, 0, '$'), rel)}</div>
        ${['sopr', 'sth_sopr', 'sth_cost_basis'].filter(k => !btcOk(g[k])).map(k => btcMissing(g[k], k)).join('')}
        ${btcOk(g.sth_cost_basis) ? '<div class="fc-chart"><canvas id="b3Sth"></canvas></div>' : ''}
        ${btcOk(g.sopr) || btcOk(g.sth_sopr) ? '<div class="fc-chart"><canvas id="b3Sopr"></canvas></div>' : ''}
        <div class="fc-note">Source: BGeometrics free API (${fcEsc(g.sopr?.source || '')}); free data is delayed about 6 days. Not graded on history.</div>`;
}
function btcDelayedCharts(g) {
    if (!g) return;
    const st = g.sth_cost_basis;
    if (btcOk(st)) btcLine('b3Sth', st.days.map(x => x.date), [
        { label: 'STH cost basis', data: st.days.map(x => x.value), borderColor: FC_COL.orange, pointRadius: 0, borderWidth: 1.5 },
        { label: 'BTC price', data: st.days.map(x => x.price), borderColor: FC_COL.blue, pointRadius: 0, borderWidth: 1.2 }]);
    const a = g.sopr, b = g.sth_sopr, lab = [...new Set([...(btcOk(a) ? a.days : []), ...(btcOk(b) ? b.days : [])].map(x => x.date))].sort();
    const by = p => Object.fromEntries((btcOk(p) ? p.days : []).map(x => [x.date, x.value]));
    const ma = by(a), mb = by(b);
    if (lab.length) btcLine('b3Sopr', lab, [
        { label: 'SOPR', data: lab.map(d => ma[d] ?? null), borderColor: FC_COL.violet, pointRadius: 0, borderWidth: 1.3, spanGaps: true },
        { label: 'STH SOPR', data: lab.map(d => mb[d] ?? null), borderColor: FC_COL.yellow, pointRadius: 0, borderWidth: 1.3, spanGaps: true },
        { label: '1.0', data: lab.map(() => 1), borderColor: FC_COL.aqua, borderDash: [4, 4], pointRadius: 0, borderWidth: 1 }]);
}
// B4: liquidations, open interest and funding. Coinalyze is live; BGeometrics is the delayed second source, shown with its own as-of date.
function btcDerivs(lv) {
    if (!lv) return '';
    const L = lv.liq_live, O = lv.oi_live, F = lv.funding_live, g = lv.bgeo || {};
    const day = L && L.last_full_day;
    const usd = v => v == null ? '—' : fcBig(v).replace('+', '');
    let h = `<div class="fc-sub" style="margin-top:10px">LIQUIDATIONS, OPEN INTEREST, FUNDING · LIVE (COINALYZE)</div><div class="fc-kpis">`
        + (btcOk(L) ? kpi('Longs liquidated', usd(day && day.long_usd), day ? `last full day ${fcEsc(day.date)}` : '') + kpi('Shorts liquidated', usd(day && day.short_usd), day ? fcEsc(day.date) : '')
            + kpi('7-day longs / shorts', `${usd(L.long_7d_usd)} / ${usd(L.short_7d_usd)}`, `today's day is still filling (${fcEsc(L.as_of)})`) : '')
        + (btcOk(O) ? kpi('Open interest', usd(O.oi_usd), `${fcEsc(O.as_of)} · 7d ${O.change_7d_pct == null ? '—' : fcPct(O.change_7d_pct)}`) : '')
        + (btcOk(F) ? kpi('Funding', fcPct(F.funding_pct, 4), `${fcEsc(F.as_of)} · 7d avg ${F.avg_7d_pct == null ? '—' : fcPct(F.avg_7d_pct, 4)}`, fcCls(F.funding_pct)) : '') + '</div>'
        + [[L, 'Liquidations'], [O, 'Open interest'], [F, 'Funding']].filter(x => !btcOk(x[0])).map(x => btcMissing(x[0], x[1])).join('')
        + (btcOk(L) ? '<div class="fc-chart"><canvas id="b4Liq"></canvas></div>' : '') + (btcOk(O) || btcOk(F) ? '<div class="fc-chart"><canvas id="b4Oi"></canvas></div>' : '')
        + `<div class="fc-note">${fcEsc((btcOk(F) && F.note) || 'Coinalyze free API.')} Liquidation bars: longs and shorts per UTC day, last 90 days (the newest bar is today's, partial).</div>`;
    if (g.status && g.status !== 'missing') {
        const q = (p, label, fmt) => btcOk(p) ? kpi(label, fmt(p.value), btcLag(p)) : '';
        h += `<div class="fc-sub" style="margin-top:10px">THE SAME, DELAYED (BGEOMETRICS)</div><div class="fc-kpis">${q(g.liquidations, 'Liquidations (day)', usd)}${q(g.open_interest, 'Open interest', usd)}${q(g.funding, 'Funding', v => fcPct(v * 100, 4))}</div>`
            + ['liquidations', 'open_interest', 'funding'].filter(k => !btcOk(g[k])).map(k => btcMissing(g[k], k)).join('');
    } else h += btcMissing(g, 'BGeometrics derivatives');
    return h;
}
function btcDerivsCharts(lv) {
    if (!lv) return;
    const L = lv.liq_live, O = lv.oi_live, F = lv.funding_live;
    if (btcOk(L)) btcBar('b4Liq', L.days.map(x => x.date + (x.partial ? '*' : '')), [
        { label: 'Longs liquidated', data: L.days.map(x => x.long_usd), backgroundColor: FC_COL.orange },
        { label: 'Shorts liquidated', data: L.days.map(x => -x.short_usd), backgroundColor: FC_COL.aqua }]);
    const lab = [...new Set([...(btcOk(O) ? O.days : []), ...(btcOk(F) ? F.days : [])].map(x => x.date))].sort();
    const mo = Object.fromEntries((btcOk(O) ? O.days : []).map(x => [x.date, x.oi_usd])), mf = Object.fromEntries((btcOk(F) ? F.days : []).map(x => [x.date, x.funding_pct]));
    if (lab.length) fcChart('b4Oi', { type: 'line', data: { labels: lab, datasets: [
        { label: 'Open interest (USD)', data: lab.map(d => mo[d] ?? null), borderColor: FC_COL.blue, pointRadius: 0, borderWidth: 1.4, yAxisID: 'y', spanGaps: true },
        { label: 'Funding (%)', data: lab.map(d => mf[d] ?? null), borderColor: FC_COL.yellow, pointRadius: 0, borderWidth: 1.2, yAxisID: 'y1', spanGaps: true }] },
        options: { scales: { x: axis({ ticks: { color: FC_COL.ink2, font: { size: 9 }, maxTicksLimit: 8 } }), y: axis(), y1: axis({ position: 'right', grid: { drawOnChartArea: false } }) } } });
}
function btcLeverage(d) {
    const f = (d.leverage || {}).funding, v = (d.leverage || {}).dvol, el = document.getElementById('b4');
    const fund = btcOk(f) ? `<div class="fc-kpis">${kpi('Funding / 8h', f.funding_8h == null ? '—' : fcPct(f.funding_8h * 100, 4), f.annualized_pct == null ? '' : `${fcPct(f.annualized_pct)} a year`, fcCls(f.funding_8h))}
        ${kpi('7-day average', f.avg_7d == null ? '—' : fcPct(f.avg_7d * 100, 4), 'per 8h')}${kpi('Negative days running', f.negative_days_running ?? '—')}${kpi('High days running', f.high_days_running ?? '—', 'in the top decile')}
        ${kpi('Open interest', btcNum(f.open_interest_usd, 0, '$'), 'Deribit perpetual')}</div>${btcNote(f, ['history'])}` : btcMissing(f, 'Funding');
    const vol = btcOk(v) ? `<div class="fc-kpis" style="margin-top:8px">${kpi('DVOL', btcNum(v.dvol, 1), `${btcNum(v.percentile)}th percentile of ${v.days} days`)}${kpi('30 days ago', btcNum(v.dvol_30d_ago, 1))}</div>
        <div class="fc-note">25-delta skew: ${fcEsc(v.skew_25d)}</div>` : btcMissing(v, 'DVOL');
    el.innerHTML = fund + vol + btcDerivs(d.leverage) + `<div class="fc-note">Deribit funding by day (UTC): ${((f && f.last_days) || []).map(x => x[0].slice(5) + ' ' + (x[1] * 100).toFixed(4) + '%').join(' · ') || '—'}.</div>`;
    btcDerivsCharts(d.leverage);
}
function btcFlows(d) {
    const fl = d.flows || {}, el = document.getElementById('b5'), i = fl.ibit, c = fl.coinbase_premium, s = fl.stablecoins, k = fl.cot;
    const ibit = btcOk(i) ? `<div class="fc-kpis">${kpi('IBIT shares', btcNum(i.shares_outstanding), fcEsc(i.as_of))}${kpi('5-day net flow', i.sum_5d_usd == null ? '—' : fcBig(i.sum_5d_usd), 'shares change x NAV', fcCls(i.sum_5d_usd))}
        ${kpi('Positive days running', i.positive_days_running)}${kpi('Days stored', i.stored_days)}</div>${btcNote(i, ['streak'])}` : btcMissing(i, 'IBIT flows');
    const prem = btcOk(c) ? kpi('Coinbase premium', fcPct(c.premium_pct, 3), 'Coinbase USD vs Kraken USDT', fcCls(c.premium_pct)) : '';
    const stb = btcOk(s) ? kpi('USDT + USDC', fcBig(s.usdt_usdc_usd).replace('+', ''), s.change_30d_pct == null ? '' : `${fcPct(s.change_30d_pct, 2)} in 30 days`, fcCls(s.change_30d_pct)) : '';
    const cot = btcOk(k) ? `${kpi('CME asset managers', btcNum(k.asset_managers.net), `net contracts, 1w ${btcNum(k.asset_managers.change_1w)}`)}${kpi('CME leveraged funds', btcNum(k.leveraged_funds.net), `net, 1w ${btcNum(k.leveraged_funds.change_1w)}`)}` : '';
    el.innerHTML = ibit + `<div class="fc-kpis" style="margin-top:8px">${prem}${stb}${cot}</div>`
        + (btcOk(c) ? '' : btcMissing(c, 'Coinbase premium')) + (btcOk(s) ? '' : btcMissing(s, 'Stablecoin supply')) + (btcOk(k) ? `<div class="fc-note">CFTC TFF as of ${fcEsc(k.as_of)}</div>` : btcMissing(k, 'CME COT'))
        + '<div class="fc-note">Other spot ETFs (FBTC, ARKB, BITB) are not collected yet: IBIT only.</div>';
}

function btcCopy(id) {
    const d = BTC.data || {}, map = { b1: { verdict: d.verdict, rows: d.rows }, b2: d.price, b3: { onchain: d.onchain, delayed: d.onchain_delayed }, b4: d.leverage, b5: d.flows };
    const title = document.querySelector(`#${id}`)?.closest('.rainbow-inner')?.querySelector('.fc-title')?.textContent || id;
    const payload = JSON.stringify({ card: title, as_of: d.as_of, data: map[id] ?? null }, null, 2);
    if (typeof openCopyModal === 'function') openCopyModal(title, payload, 'json'); else navigator.clipboard?.writeText(payload);
}
function btcFull(id) {
    const back = document.getElementById('btcBackdrop');
    if (BTC.full) { BTC.full.classList.remove('fc-full'); BTC.full = null; back.classList.add('hidden'); document.body.classList.remove('fc-lock'); }
    else if (id) {
        BTC.full = document.getElementById(id)?.closest('.rainbow-card');
        if (!BTC.full) return;
        BTC.full.classList.add('fc-full'); back.classList.remove('hidden'); document.body.classList.add('fc-lock');
    }
    setTimeout(() => Object.values(FC.charts).forEach(c => c && c.resize()), 60);
}
const BTC_FAQ = {
    b1: { title: 'B1 · Verdict — FAQ',
        what: 'Two checklists of conditions that have accompanied Bitcoin cycle lows (bottom, 6 rows) and highs (top, 5 rows). The verdict counts how many fired. It is a description of conditions, not a price call, and not financial advice.',
        read: ['<b>FIRED / waiting / missing</b>: a missing row has no data yet and its reason is shown; it is never counted as waiting or zero.',
               '<b>n of m available</b>: with fewer than half the rows available the verdict is NO CALL and names what is missing.',
               '<b>Evidence</b>: only rows graded on history vote. No row is graded yet (phase 4 of the plan), so untested rows are shown but never vote, and this card says HOLD or NO CALL until they are.',
               '<b>Thresholds</b>: BUY (start) at 3 graded bottom rows, BUY (add) at 5, SELL (trim) at 3 graded top rows. The counts do not shrink when rows are missing, so missing data can only make the verdict more cautious.'],
        caveats: ['Funding row: the liquidation-spike leg of the plan is not measured (no free keyless feed); only the negative-funding leg is.', 'ETF rows use IBIT only and need 5 stored days of flow.'] },
    b2: { title: 'B2 · Price & cycle — FAQ',
        what: 'Price against the long-run levels cycle bottoms have formed near.',
        read: ['<b>200-week MA</b>: the mean of the last 1,400 daily closes. <b>Realized price</b>: the average price at which every coin last moved on-chain.',
               '<b>Mayer multiple</b>: price divided by the 200-day average. <b>Halving clock</b>: days since the last halving; the next date is an estimate (1,460 days later).'],
        caveats: ['Four halvings are too few cycles to prove anything; read these as context.'] },
    b3: { title: 'B3 · On-chain — FAQ',
        what: 'What coin holders paid, from Coin Metrics\' free community data.',
        read: ['<b>MVRV Z-score</b> = (market cap − realized cap) / standard deviation of market cap over the whole history to date. Low values mean the market trades near holders\' cost.',
               '<b>Hash ribbons</b>: 30-day vs 60-day mean hash rate. Capitulation = miners switching off; recovery = the 30-day crossing back above within 30 days.'],
        caveats: ['The free API withholds realized cap, so it is derived (market cap / MVRV). <b>SOPR</b> (spent-output profit ratio: above 1.0 coins moved at a profit) and the <b>short-term-holder cost basis</b> (average cost of coins held under 155 days) come from BGeometrics\' free API, about 6 days late: every reading shows its own as-of date and age. Exchange balances have no free source and show as missing. None of these is graded on history.'] },
    b4: { title: 'B4 · Leverage & options — FAQ',
        what: 'How crowded the leveraged side is, from Deribit\'s public API.',
        read: ['<b>Funding</b>: what longs pay shorts (negative: shorts pay) every 8 hours on the perpetual. <b>DVOL</b>: Bitcoin\'s implied volatility index, with its percentile over the last year. <b>Liquidations</b>: forced closes of longs (orange, up) and shorts (green, down) per day.'],
        caveats: ['Liquidations (longs vs shorts), aggregated open interest and funding are live from Coinalyze (free key); BGeometrics serves the same about 6 days late and is shown as a second, dated source. 25-delta skew is not collected (it needs the full option chain). Not graded on history.'] },
    b5: { title: 'B5 · Flows — FAQ',
        what: 'Money moving into and out of Bitcoin.',
        read: ['<b>IBIT flow</b> = daily change in shares outstanding × NAV, from iShares\' page; history builds one day per pipeline run. <b>Coinbase premium</b>: Coinbase USD price vs Kraken USDT. <b>Stablecoins</b>: USDT + USDC supply and its 30-day change. <b>CME COT</b>: CFTC positions in CME Bitcoin futures.'],
        caveats: ['IBIT only; FBTC, ARKB and BITB are not collected. COT is weekly (Tuesday data, Friday release).'] },
};
function btcFaq(id) {
    const f = BTC_FAQ[id];
    if (!f) return;
    document.getElementById('btcFaqTitle').textContent = f.title;
    const sec = (h, items) => items ? `<h4>${h}</h4><ul>${items.map(x => `<li>${x}</li>`).join('')}</ul>` : '';
    document.getElementById('btcFaqBody').innerHTML = `<p>${f.what}</p>` + sec('How to read it', f.read) + sec('Caveats', f.caveats)
        + '<p class="fc-note">Conditions and their history, not a trade call and not financial advice.</p>';
    document.getElementById('btcFaqModal').classList.remove('hidden');
}
function btcFaqClose() { document.getElementById('btcFaqModal').classList.add('hidden'); }
document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (!document.getElementById('btcFaqModal')?.classList.contains('hidden')) btcFaqClose();
    else if (BTC.full) btcFull();
});
document.addEventListener('click', e => { if (e.target && e.target.id === 'btcFaqModal') btcFaqClose(); });

(function () {            // hook into the tab router, as forecast.js does
    const orig = window.switchTab;
    window.switchTab = function (name) {
        if (orig) orig(name);
        if (name === 'bitcoin') loadBitcoin();
    };
})();
