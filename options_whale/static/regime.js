// ==========================================================================================
// Forecast Lab cards R (Market Regime / Shock Reader) and T (Trackers).
// R reads /api/regime (the latest snapshot's regime block; nothing is computed here). T reads /api/trackers and edits them through
// POST /api/trackers, DELETE /api/trackers/<id>, POST /api/trackers/<id>/duplicate|test (header X-Tracker-Request; the server refuses
// them on a read-only copy). Shares the helpers of forecast.js (fcEsc, fcNum, kpi, missing, fcLabelCells, fetchJson from app.js).
// ==========================================================================================
const RG = { data: null, run: null, trk: null, metrics: null, editing: null };
const RG_COL = { CALM: '#4ade80', VOL_SHOCK: '#fbbf24', VOL_SHOCK_WATCH: '#fb923c', CREDIT_STRESS: '#f87171', RECESSION_BEAR: '#ef4444',
    LIQUIDITY_SQUEEZE: '#a78bfa', PANIC_UNWINDING: '#22d3ee' };
const RG_PRIO = { min: '#71717a', low: '#a1a1aa', default: '#60a5fa', high: '#fbbf24', urgent: '#f87171' };
const RG_STATE = { armed: 'fc-muted', FIRED: 'fc-warn', 'cooling down': 'fc-muted', disabled: 'fc-muted' };
const rgHdr = { 'X-Tracker-Request': '1', 'Content-Type': 'application/json' };
const rgUsd = v => { if (v === null || v === undefined) return '—'; const a = Math.abs(v), s = v < 0 ? '-' : v > 0 ? '+' : ''; return a >= 1e9 ? `${s}$${(a / 1e9).toFixed(1)}B` : a >= 1e6 ? `${s}$${(a / 1e6).toFixed(0)}M` : `${s}$${a.toFixed(0)}`; };
const rgVal = v => v === null || v === undefined ? '—' : typeof v === 'string' ? fcEsc(v) : Math.abs(v) >= 1e5 ? fcNum(v, 0) : String(+v.toPrecision(5));
const rgAgo = iso => { if (!iso) return 'never'; const m = (Date.now() - Date.parse(iso)) / 6e4; return m < 90 ? `${Math.round(m)} min ago` : m < 2880 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };

RG.load = () => Promise.all([rgLoadRegime(), rgLoadTrackers()]);

// ---------------------------------------------------------------- R · regime
async function rgLoadRegime() {
    const el = document.getElementById('fcR');
    if (!el) return;
    try {
        const j = await fetchJson(`${API_BASE}/api/regime`);
        if (j.status !== 'success') throw new Error(j.message || 'load failed');
        RG.data = j.data; RG.run = j.run;
        rgRenderRegime(j.data, j.run || {});
    } catch (e) { el.innerHTML = `<div class="fc-neg">Regime unavailable: ${fcEsc(e.message)}</div>`; RG.data = null; }
}
function rgRenderRegime(d, run) {
    const el = document.getElementById('fcR');
    if (d.status !== 'fresh') { el.innerHTML = missing(d); return; }
    const col = RG_COL[d.key] || '#a1a1aa', fs = d.forced_selling || {}, rd = d.read || {};
    const flip = d.changed && d.previous ? `<span class="fc-warn">CHANGED from ${fcEsc(d.previous.regime)}</span>` : d.previous ? `unchanged since the previous run` : 'no previous run to compare';
    const rows = (fs.rows || []).map(r => {
        const cls = r.status === 'missing' ? 'fc-muted' : r.direction === 'SELL' ? 'fc-neg' : r.direction === 'BUY' ? 'fc-pos' : 'fc-muted';
        const val = r.value === null || r.value === undefined ? '—' : r.unit === 'USD' || r.unit.startsWith('USD') ? rgUsd(r.value) : fcNum(r.value, 2);
        return `<tr><td>${fcEsc(r.label)}</td><td>${r.status === 'missing' ? '<span class="fc-muted">missing</span>' : val}</td>
            <td class="${cls}">${fcEsc(r.direction || '—')}</td><td class="${r.status === 'ACTIVE' ? cls : 'fc-muted'}">${fcEsc(r.status)}</td>
            <td class="fc-muted" style="white-space:normal">${fcEsc(r.detail || r.reason || '')}</td></tr>`;
    }).join('');
    const pb = (d.playbook || []).map(p => `<tr><td>${fcEsc(p.name)}</td><td style="white-space:normal">${fcEsc(p.rule)}</td>
        <td class="${p.status === 'FIRED' ? (p.key === 'stop' ? 'fc-neg' : 'fc-pos') : 'fc-muted'}">${p.status === 'FIRED' ? 'FIRED' : p.status}</td>
        <td class="fc-muted" style="white-space:normal">${fcEsc(p.detail)}</td></tr>`).join('');
    el.innerHTML = `
        <div class="rg-top"><span class="rg-pill" style="color:${col}">${fcEsc(d.regime)}</span>
            <div class="fc-muted">${flip} · as of run ${fcEsc(run.run_id || '—')} · ${fcEsc(run.generated_local || d.as_of || '')}</div></div>
        <div class="rg-read" style="border-color:${col}55"><div class="fc-sub" style="margin:0 0 3px">THE READ${rd.status === 'fresh' ? ` · written by ${fcEsc(rd.engine === 'local' ? 'the local model ' + rd.model : 'Claude ' + rd.model)}` : ''}</div>
            ${rd.status === 'fresh' ? fcEsc(rd.text) : `<span class="fc-muted">No read: ${fcEsc(rd.reason || 'not written')}. The rules below still stand.</span>`}</div>
        <div class="fc-sub">WHY (reading vs its threshold)</div>
        <ul class="rg-why">${(d.why || []).map(w => `<li class="${/ - MET$/.test(w) ? 'fc-warn' : /missing/.test(w) ? 'fc-neg' : ''}">${fcEsc(w)}</li>`).join('')}</ul>
        <div class="fc-sub">FORCED-SELLING GAUGE${fs.sold_recent_usd != null ? ` · est. ${rgUsd(fs.sold_recent_usd)} of vol-control (5 sessions) and leveraged-ETF selling` : ''}${fs.if_spy_down_2pct_usd != null ? ` · another ${rgUsd(fs.if_spy_down_2pct_usd)} if SPY falls 2%` : ''}</div>
        ${fs.status === 'missing' && !rows ? missing(fs) : `<table class="fc-table fc-sig"><tr><th>Flow</th><th>Value</th><th>Direction</th><th>Status</th><th>Detail</th></tr>${rows}</table>`}
        <div class="fc-note">${fcEsc(fs.note || '')}</div>
        ${rgProjection(d.flow_projection)}
        <div class="fc-sub">PLAYBOOK (Vlad's tranche plan, as conditions; not advice)</div>
        <table class="fc-table fc-sig"><tr><th>Step</th><th>Condition</th><th>Status</th><th>Reading</th></tr>${pb}</table>
        <details class="fc-note"><summary>The rules (first match wins)</summary><ol style="padding-left:16px">${(d.rules || []).map(r => `<li><b>${fcEsc(r.regime)}</b>: ${fcEsc(r.when)}</li>`).join('')}</ol></details>`;
    fcLabelCells(el);
    rgProjectionCharts(d.flow_projection);
}

// ---- forced-flow projection (core/flowproj.py): ESTIMATES; a missing source is a gap, never a zero
const RG_SRC = { vol_control: ['Vol-control', '#ef4444'], cta: ['CTA', '#f59e0b'], leveraged_etf: ['Leveraged ETFs', '#a78bfa'], dealer_gamma: ['Dealer gamma', '#22d3ee'] };
const RG_PATH = { calm: '#4ade80', recent: '#fbbf24', stressed: '#f87171' };
const rgB = v => v === null || v === undefined ? null : v / 1e9;
const rgDay = iso => iso ? new Date(iso + 'T12:00:00Z').toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' }) : 'not within 30 sessions';
function rgProjection(fp) {
    const head = '<div class="fc-sub">FORCED-FLOW PROJECTION · ESTIMATE (sell = negative)</div>';
    if (!fp || fp.status !== 'fresh') return head + missing(fp || { status: 'missing', reason: 'not in this run (an older snapshot)' });
    const rows = Object.fromEntries(fp.scenarios.rows.map(r => [r.move_pct, r])), tp = fp.time_path || {};
    const miss = Object.entries(fp.scenarios.missing || {}).map(([k, why]) => `<li class="fc-neg">${fcEsc(RG_SRC[k][0])}: missing (${fcEsc(why)})</li>`).join('');
    const scen = [-2, -5, -10].map(m => rows[m]).filter(Boolean).map(r => `<tr><td>SPY ${r.move_pct}% (${fcNum(r.price, 2)})</td>
        ${Object.keys(RG_SRC).map(k => `<td class="${fcCls(r[k])}">${r[k] === null ? '<span class="fc-muted">missing</span>' : rgUsd(r[k])}</td>`).join('')}
        <td class="${fcCls(r.total)}"><b>${rgUsd(r.total)}</b></td><td class="fc-muted">${rgUsd(r.total_low)} to ${rgUsd(r.total_high)}</td></tr>`).join('');
    const paths = tp.status === 'fresh' ? Object.entries(tp.paths).map(([k, p]) => `<tr><td style="color:${RG_PATH[k]}">${fcEsc(p.label)}</td><td>${fcNum(p.daily_move_pct, 2)}%</td>
        <td class="${p.releverage_date ? 'fc-pos' : 'fc-muted'}">${rgDay(p.releverage_date)}</td><td class="${fcCls(p.cumulative[p.cumulative.length - 1])}">${rgUsd(p.cumulative[p.cumulative.length - 1])}</td>
        <td class="fc-muted">${rgUsd(p.cumulative_low)} to ${rgUsd(p.cumulative_high)}</td></tr>`).join('') : '';
    const cf = tp.cta_flat || {};
    const ctaTxt = cf.status === 'missing' ? `CTA under a flat price: missing (${fcEsc(cf.reason)})` : cf.exposure ? `CTA held flat at spot: exposure ${fcNum(cf.exposure_now, 2)} → ${fcNum(cf.exposure[cf.exposure.length - 1], 2)} over 30 sessions (${rgUsd(cf.cumulative[cf.cumulative.length - 1])} est.)`
        + `; window flips: ${Object.entries(cf.window_flip_dates || {}).map(([w, d]) => `${w} ${d ? rgDay(d) : 'none'}`).join(', ')}` : '';
    return `${head}
        <div class="rg-proj-sum">${fcEsc(fp.summary)}</div>
        <div class="fc-note">SPY close vs the daily forced-flow estimate, then the next 30 sessions. <span class="rg-glow-key">red glow = estimated forced selling</span>, green = forced buying;
            the projected part is lighter and dashed (shaded on the "same as the last 5 sessions" path). Dots mark where each path's vol-control funds turn buyers.</div>
        <div class="fc-chart fc-chart-tall"><canvas id="rgFlowHist"></canvas></div>
        <div class="fc-note">Extra forced flow if SPY moves from ${fcNum(fp.scenarios.spot, 2)} (next close vs an unchanged close), stacked by source. Lines: spot, the zero-gamma level and each CTA window's flip level.</div>
        <div class="fc-chart fc-chart-tall"><canvas id="rgFlowScen"></canvas></div>
        <table class="fc-table fc-sig"><tr><th>Scenario</th>${Object.values(RG_SRC).map(([n]) => `<th>${n}</th>`).join('')}<th>Total</th><th>Range (AUM band)</th></tr>${scen}</table>
        ${paths ? `<table class="fc-table fc-sig"><tr><th>Next 30 sessions (vol-control)</th><th>Daily move</th><th>Turns buyer</th><th>Cumulative</th><th>Range</th></tr>${paths}</table>` : missing(tp)}
        <div class="fc-note">${ctaTxt}</div>
        ${miss ? `<ul class="rg-why">${miss}</ul>` : ''}
        <details class="fc-note"><summary>Assumptions (every number here is an estimate)</summary><ul class="rg-why">${(fp.assumptions || []).map(a => `<li>${fcEsc(a)}</li>`).join('')}
            <li>${fcEsc(fp.history.note)}</li><li>Dealer gamma model in this run: ${fcEsc(fp.scenarios.gamma_model)}.</li></ul></details>`;
}

// Bands behind the price where the daily estimate is net selling (neon red, stronger for bigger days) or buying (soft green).
// `bands` = [{i, v (USD), proj}] on the chart's category index; projected bands are lighter with a dashed edge.
const rgGlow = bands => ({
    id: 'rgGlow',
    beforeDatasetsDraw(chart) {
        const { ctx, chartArea: a, scales: { x } } = chart;
        const top = p => Math.max(1, ...bands.filter(b => b.proj === p).map(b => Math.abs(b.v || 0)));     // projected days scale on their own
        const max = { true: top(true), false: top(false) };
        const w = Math.max(2, (x.getPixelForValue(1) - x.getPixelForValue(0)) || 4);
        ctx.save();
        bands.forEach(b => {
            const k = Math.min(1, Math.abs(b.v || 0) / max[b.proj]);
            if (k < 0.12) return;                       // small days stay unshaded so the selling spikes stand out
            const sell = b.v < 0, x0 = x.getPixelForValue(b.i) - w / 2;
            const al = (sell ? 0.10 + 0.45 * k : 0.05 + 0.18 * k) * (b.proj ? 0.5 : 1);
            ctx.fillStyle = sell ? `rgba(255,40,70,${al})` : `rgba(74,222,128,${al})`;
            ctx.shadowColor = sell ? 'rgba(255,40,70,.9)' : 'rgba(74,222,128,.5)';
            ctx.shadowBlur = sell ? 6 + 14 * k : 4;
            ctx.fillRect(x0, a.top, w, a.bottom - a.top);
            if (b.proj) { ctx.shadowBlur = 0; ctx.setLineDash([3, 3]); ctx.strokeStyle = ctx.fillStyle; ctx.strokeRect(x0, a.top, w, a.bottom - a.top); ctx.setLineDash([]); }
        });
        ctx.restore();
    },
});
// Vertical marks on a category axis of prices: [{price, label, color}], placed between the two grid points around the price
const rgMarks = (prices, marks) => ({
    id: 'rgMarks',
    afterDatasetsDraw(chart) {
        const { ctx, chartArea: a, scales: { x } } = chart;
        ctx.save(); ctx.font = '9px sans-serif';
        marks.forEach((m, n) => {
            if (m.price == null) return;
            const j = prices.findIndex((p, i) => i < prices.length - 1 && (p - m.price) * (prices[i + 1] - m.price) <= 0);
            if (j < 0) return;
            const f = (m.price - prices[j]) / (prices[j + 1] - prices[j] || 1), px = x.getPixelForValue(j) + f * (x.getPixelForValue(j + 1) - x.getPixelForValue(j));
            ctx.strokeStyle = m.color; ctx.fillStyle = m.color; ctx.setLineDash(m.dash || [4, 3]);
            ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke();
            ctx.fillText(m.label, Math.min(px + 3, a.right - 60), a.top + 10 + 11 * (n % 4));
        });
        ctx.restore();
    },
});
function rgProjectionCharts(fp) {
    if (!fp || fp.status !== 'fresh' || typeof Chart === 'undefined') return;
    const h = fp.history, tp = fp.time_path || {}, n = Math.min(120, h.dates.length), H = h.dates.slice(-n), tot = h.total.slice(-n);
    const fut = tp.status === 'fresh' ? tp.dates : [], pad = a => Array(fut.length).fill(null), lead = Array(n).fill(null);
    const labels = H.concat(fut), proj = (tp.paths || {}).recent;
    const bands = tot.map((v, i) => ({ i, v, proj: false })).concat(proj ? proj.flow.map((v, i) => ({ i: n + i, v, proj: true })) : []);
    const ds = [
        { type: 'line', label: 'SPY close', data: h.close.slice(-n).concat(pad()), borderColor: '#e4e4e7', borderWidth: 1.5, pointRadius: 0, yAxisID: 'px', spanGaps: false, order: 0 },
        { type: 'bar', label: 'Forced flow, est. ($B/day)', data: tot.map(rgB).concat(pad()), yAxisID: 'usd', order: 2,
          backgroundColor: tot.map(v => v < 0 ? 'rgba(255,60,90,.85)' : 'rgba(74,222,128,.7)') },
    ];
    Object.entries(tp.paths || {}).forEach(([k, p]) => {
        const at = fut.indexOf(p.releverage_date);
        ds.push({ type: 'line', label: `Vol-control, ${p.label}${p.releverage_date ? ' · buyer ' + rgDay(p.releverage_date) : ''}`, data: lead.concat(p.flow.map(rgB)), yAxisID: 'usd',
                  borderColor: RG_PATH[k], borderDash: [5, 4], borderWidth: 1.5, order: 1,
                  pointRadius: lead.concat(p.flow.map((_, i) => i === at ? 5 : 0)), pointBackgroundColor: RG_PATH[k] });
    });
    if ((tp.cta_flat || {}).flow) ds.push({ type: 'line', label: 'CTA, price flat', data: lead.concat(tp.cta_flat.flow.map(rgB)), yAxisID: 'usd', borderColor: '#fb923c', borderDash: [2, 3], borderWidth: 1, pointRadius: 0, order: 1 });
    fcChart('rgFlowHist', { type: 'bar', data: { labels, datasets: ds }, plugins: [rgGlow(bands)],
        options: { onResize: fcFitLegend, plugins: { legend: { position: fcLegendPosition(document.getElementById('rgFlowHist')?.clientWidth || 0), labels: { color: FC_COL.ink, boxWidth: 10, font: { size: 9 } } },
                            tooltip: { callbacks: { label: c => c.dataset.yAxisID === 'usd' ? `${c.dataset.label}: ${c.raw === null ? 'missing' : (c.raw > 0 ? '+' : '') + c.raw.toFixed(1) + 'B'}` : `${c.dataset.label}: ${fcNum(c.raw, 2)}` } } },
                   scales: { x: axis({ ticks: { color: FC_COL.ink2, font: { size: 9 }, maxTicksLimit: 8, maxRotation: 0 } }),
                             usd: axis({ position: 'left', title: { display: true, text: '$B / day (est.)', color: FC_COL.ink2, font: { size: 9 } } }),
                             px: axis({ position: 'right', beginAtZero: false, grace: '5%', grid: { display: false }, title: { display: true, text: 'SPY', color: FC_COL.ink2, font: { size: 9 } } }) } } });
    // scenarios: one stacked area per source and sign (Chart.js stacks lines without splitting signs), total as a line
    const sc = fp.scenarios, moves = sc.rows.map(r => r.move_pct), prices = sc.rows.map(r => r.price), sds = [];
    // datasets alternate neg, pos per source: each fills to the one two back (the same sign's previous source), the first to zero
    Object.entries(RG_SRC).forEach(([k, [name, col]]) => {
        if (sc.missing && sc.missing[k]) return;
        [['neg', v => Math.min(v, 0)], ['pos', v => Math.max(v, 0)]].forEach(([st, f], j) => sds.push({ type: 'line', label: name, stack: st, fill: sds.length < 2 ? 'origin' : '-2', data: sc.rows.map(r => rgB(f(r[k]))),
            borderColor: col, backgroundColor: col + '99', borderWidth: 1, pointRadius: 0, yAxisID: 'y', hidden: false, _dup: j === 1 }));
    });
    // total and its range are lines in stacks of their own, so the stacked axis does not add them to the areas
    sds.push({ type: 'line', label: 'Total', data: sc.rows.map(r => rgB(r.total)), borderColor: '#f4f4f5', borderWidth: 2, pointRadius: 0, stack: 'total', fill: false, yAxisID: 'y' },
             { type: 'line', label: 'Range (AUM band)', data: sc.rows.map(r => rgB(r.total_low)), borderColor: '#e4e4e7', borderDash: [3, 3], borderWidth: 1, pointRadius: 0, stack: 'lo', fill: false, yAxisID: 'y' },
             { type: 'line', label: '', data: sc.rows.map(r => rgB(r.total_high)), borderColor: '#e4e4e7', borderDash: [3, 3], borderWidth: 1, pointRadius: 0, stack: 'hi', fill: false, yAxisID: 'y' });
    const marks = [{ price: sc.spot, label: `spot ${fcNum(sc.spot, 0)}`, color: '#e4e4e7', dash: [] }, { price: sc.zero_gamma, label: `zero γ ${fcNum(sc.zero_gamma, 0)}`, color: '#22d3ee' }]
        .concat(Object.entries(sc.cta_flip_levels || {}).map(([w, p]) => ({ price: p, label: `CTA ${w} flip ${fcNum(p, 0)}`, color: '#fb923c' })));
    fcChart('rgFlowScen', { type: 'line', data: { labels: moves.map((m, i) => `${m > 0 ? '+' : ''}${m}% · ${fcNum(prices[i], 0)}`), datasets: sds }, plugins: [rgMarks(prices, marks)],
        options: { onResize: fcFitLegend, plugins: { legend: { position: fcLegendPosition(document.getElementById('rgFlowScen')?.clientWidth || 0),
                       labels: { color: FC_COL.ink, boxWidth: 10, font: { size: 9 }, filter: (it, d) => !d.datasets[it.datasetIndex]._dup && it.text } },
                            tooltip: { filter: it => !it.dataset._dup || it.raw, callbacks: { label: c => `${c.dataset.label || 'Range high'}: ${c.raw === null ? 'missing' : (c.raw > 0 ? '+' : '') + c.raw.toFixed(1) + 'B'}` } } },
                   scales: { x: axis({ ticks: { color: FC_COL.ink2, font: { size: 9 }, maxTicksLimit: 7, maxRotation: 0 } }),
                             y: axis({ stacked: true, title: { display: true, text: '$B (est.)', color: FC_COL.ink2, font: { size: 9 } } }) } } });
}

// ---------------------------------------------------------------- T · trackers
async function rgLoadTrackers() {
    const el = document.getElementById('fcT');
    if (!el) return;
    try {
        const j = await fetchJson(`${API_BASE}/api/trackers`);
        if (j.status !== 'success') throw new Error(j.message || 'load failed');
        RG.trk = j; rgRenderTrackers(j);
    } catch (e) { el.innerHTML = `<div class="fc-neg">Trackers unavailable: ${fcEsc(e.message)}</div>`; }
}
function rgCond(c) {
    if (c.op === 'changes') return `${fcEsc(c.expr)} = ${rgVal(c.value)} <span class="fc-muted">(alerts when it changes)</span>`;
    const bar = c.progress == null ? '' : `<div class="trk-bar" title="${c.met ? 'met' : fcNum(Math.abs(c.gap_pct), 1) + '% from the trigger'}"><i style="width:${Math.round(c.progress * 100)}%" class="${c.met ? 'met' : ''}"></i></div>`;
    return `<div>${fcEsc(c.expr)} = <b>${c.error ? '<span class="fc-neg" title="' + fcEsc(c.error) + '">n/a</span>' : rgVal(c.value)}</b> <span class="fc-muted">${fcEsc(c.op)} ${rgVal(c.threshold)}</span>
        ${c.gap_pct != null && !c.met ? `<span class="fc-muted"> · ${fcNum(Math.abs(c.gap_pct), 1)}% away</span>` : ''}${bar}</div>`;
}
function rgRenderTrackers(j) {
    const el = document.getElementById('fcT'), ro = !j.writable;
    const dis = ro ? ' disabled title="This copy is read-only: change trackers on the Mac that runs the alerts"' : '';
    const body = j.trackers.map(t => `<tr class="${t.enabled ? '' : 'trk-off'}">
        <td><b>${fcEsc(t.name)}</b>${t.note ? `<div class="fc-muted" style="white-space:normal">${fcEsc(t.note)}</div>` : ''}
            <div class="fc-muted">${t.hours === 'session' ? 'session only' : '24/7'} · cooldown ${t.cooldown_min} min</div></td>
        <td>${t.live.conditions.map(rgCond).join('')}</td>
        <td><span class="fc-badge ${RG_STATE[t.status] || ''}">${fcEsc(t.status)}</span>${t.cooldown_left_min != null && t.status === 'cooling down' ? `<div class="fc-muted">${t.cooldown_left_min} min left</div>` : ''}</td>
        <td class="fc-muted">${rgAgo(t.last_fired)}</td>
        <td><span class="fc-badge" style="color:${RG_PRIO[t.priority]}">${fcEsc(t.priority)}</span></td>
        <td class="trk-act"><button class="fc-x" onclick="rgEdit('${fcEsc(t.id)}')"${dis}>EDIT</button> <button class="fc-x" onclick="rgDup('${fcEsc(t.id)}')"${dis}>DUPLICATE</button>
            <button class="fc-x" onclick="rgTest('${fcEsc(t.id)}')"${dis}>TEST</button> <button class="fc-x" onclick="rgDel('${fcEsc(t.id)}')"${dis}>DELETE</button></td></tr>`).join('');
    const hist = (j.history || []).map(h => `<li><span class="fc-muted">${fcEsc(h.t.replace('T', ' ').slice(0, 16))} UTC</span> ${h.test ? '<span class="fc-muted">TEST</span> ' : ''}<b>${fcEsc(h.name)}</b> <span class="fc-badge" style="color:${RG_PRIO[h.priority]}">${fcEsc(h.priority)}</span>
        <div class="fc-muted" style="white-space:pre-line">${fcEsc(String(h.text).split('\n').slice(0, 3).join('\n'))}</div></li>`).join('');
    el.innerHTML = `
        <div class="fc-scanbar"><button class="fc-toggle active" onclick="rgEdit(null)"${dis}>+ ADD TRACKER</button><button class="fc-toggle" onclick="rgLoadTrackers()">REFRESH</button>
            <span class="fc-muted">${j.trackers.length} trackers · checked every 15 min by tracker-alerts · stored in ${fcEsc(j.file)}${j.fallback ? ` · <span class="fc-warn">${fcEsc(j.fallback)}</span>` : ''}${j.snapshot_note ? ` · <span class="fc-warn">snapshot metrics: ${fcEsc(j.snapshot_note)}</span>` : ''}</span></div>
        <table class="fc-table fc-sig"><tr><th>Tracker</th><th>Live value · distance to trigger</th><th>State</th><th>Last fired</th><th>Priority</th><th></th></tr>${body}</table>
        <div class="fc-sub">FIRE HISTORY</div>${hist ? `<ul class="trk-hist">${hist}</ul>` : '<div class="fc-muted">Nothing has fired yet.</div>'}
        <div class="fc-note">A tracker fires once when its condition turns true (a "changes" tracker when the value differs from the last one seen), then waits out its cooldown. A reading that is missing never fires and never re-arms. Symbols in capitals (SLV, ^VIX, BTC-USD) are live prices; names such as hy_oas or chaos are as of the latest pipeline run.</div>`;
    fcLabelCells(el);
}
async function rgPost(url, method, body) {
    const r = await fetchJson(`${API_BASE}${url}`, { method, headers: rgHdr, body: body ? JSON.stringify(body) : undefined });
    if (r.status !== 'success') { alert(r.message || 'failed'); return null; }
    return r;
}
async function rgDup(id) { if (await rgPost(`/api/trackers/${encodeURIComponent(id)}/duplicate`, 'POST')) rgLoadTrackers(); }
async function rgDel(id) {
    const t = (RG.trk?.trackers || []).find(x => x.id === id);
    if (confirm(`Delete the tracker "${t ? t.name : id}"?`) && await rgPost(`/api/trackers/${encodeURIComponent(id)}`, 'DELETE')) rgLoadTrackers();
}
async function rgTest(id) { const r = await rgPost(`/api/trackers/${encodeURIComponent(id)}/test`, 'POST'); if (r) { alert(`Test sent: ${r.title}`); rgLoadTrackers(); } }

// ---- the editor modal: conditions with an expression box that completes metric names, and a live preview of each value
async function rgMetrics() {
    if (!RG.metrics) { const j = await fetchJson(`${API_BASE}/api/trackers/metrics`); RG.metrics = j.status === 'success' ? j.metrics : []; }
    return RG.metrics;
}
function rgCondRow(c) {
    const div = document.createElement('div'); div.className = 'trk-cond';
    div.innerHTML = `<div class="trk-ac-wrap"><input class="trk-expr" placeholder="^VIX / ^VIX3M   or   hy_oas   or   BTC-USD / btc_realized_price" maxlength="200" spellcheck="false" aria-label="Expression"><div class="trk-ac hidden"></div></div>
        <select class="trk-op" aria-label="Comparator">${['<', '<=', '>', '>=', '==', 'changes'].map(o => `<option>${o}</option>`).join('')}</select>
        <input class="trk-val" placeholder="threshold" aria-label="Threshold" size="8">
        <button type="button" class="fc-x trk-rm" title="Remove this condition">✕</button><div class="trk-prev fc-muted"></div>`;
    const expr = div.querySelector('.trk-expr'), op = div.querySelector('.trk-op'), val = div.querySelector('.trk-val'), ac = div.querySelector('.trk-ac'), prev = div.querySelector('.trk-prev');
    expr.value = c.expr || ''; op.value = c.op || '<'; val.value = c.value ?? '';
    const sync = () => { val.style.display = op.value === 'changes' ? 'none' : ''; };
    let timer = null;
    const preview = () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
            if (!expr.value.trim()) { prev.textContent = ''; return; }
            const q = new URLSearchParams({ expr: expr.value, op: op.value, value: val.value });
            const r = await fetchJson(`${API_BASE}/api/trackers/preview?${q}`);
            prev.className = 'trk-prev ' + (r.status !== 'success' || r.error ? 'fc-neg' : 'fc-muted');
            prev.textContent = r.status !== 'success' ? r.message : r.error ? r.error : `now ${typeof r.value === 'string' ? r.value : fcNum(r.value, 4)}` + (r.met === null ? '' : r.met ? ' · condition is TRUE now' : ' · condition is false now');
        }, 450);
    };
    const word = () => { const m = /[A-Za-z0-9_.]*$/.exec(expr.value.slice(0, expr.selectionStart)); return m ? m[0] : ''; };
    const suggest = async () => {
        const w = word().toLowerCase();
        if (w.length < 1 || /^[A-Z0-9^]/.test(word())) { ac.classList.add('hidden'); return; }
        const hits = (await rgMetrics()).filter(m => m.id.toLowerCase().includes(w)).slice(0, 8);
        ac.innerHTML = hits.map(m => `<div data-id="${fcEsc(m.id)}"><b>${fcEsc(m.id)}</b> <span class="fc-muted">${fcEsc(m.what)}</span></div>`).join('');
        ac.classList.toggle('hidden', !hits.length);
    };
    const pick = id => { const pos = expr.selectionStart, w = word(); expr.value = expr.value.slice(0, pos - w.length) + id + expr.value.slice(pos); ac.classList.add('hidden'); expr.focus(); preview(); };
    ac.onmousedown = e => { const d = e.target.closest('[data-id]'); if (d) { e.preventDefault(); pick(d.dataset.id); } };
    expr.oninput = () => { suggest(); preview(); };
    expr.onkeydown = e => { if (e.key === 'Tab' && !ac.classList.contains('hidden')) { e.preventDefault(); pick(ac.firstElementChild.dataset.id); } else if (e.key === 'Escape') ac.classList.add('hidden'); };
    expr.onblur = () => setTimeout(() => ac.classList.add('hidden'), 150);
    op.onchange = () => { sync(); preview(); }; val.oninput = preview;
    div.querySelector('.trk-rm').onclick = () => { if (div.parentElement.children.length > 1) div.remove(); };
    sync(); preview();
    return div;
}
function rgEdit(id) {
    const t = id ? RG.trk.trackers.find(x => x.id === id) : null;
    const m = document.getElementById('trkModal');
    const sel = (opts, cur) => opts.map(o => `<option${o === cur ? ' selected' : ''}>${o}</option>`).join('');
    m.querySelector('.trk-form').innerHTML = `
        <div class="fc-title">${t ? 'EDIT' : 'ADD'} TRACKER</div>
        <label>Name<input id="trkName" maxlength="80" value="${fcEsc(t ? t.name : '')}"></label>
        <div class="fc-sub">CONDITIONS (all must hold)</div><div id="trkConds"></div>
        <button type="button" class="fc-x" id="trkAddCond">+ AND another condition</button>
        <div class="trk-grid">
            <label>Priority (ntfy)<select id="trkPrio">${sel(['min', 'low', 'default', 'high', 'urgent'], t ? t.priority : 'default')}</select></label>
            <label>Cooldown (minutes)<input id="trkCool" type="number" min="0" max="10080" value="${t ? t.cooldown_min : 60}"></label>
            <label>Active hours<select id="trkHours">${sel(['24/7', 'session'], t ? t.hours : '24/7')}</select></label>
            <label class="trk-chk"><input id="trkOn" type="checkbox" ${!t || t.enabled ? 'checked' : ''}> Enabled</label></div>
        <label>Note (sent with the alert)<input id="trkNote" maxlength="300" value="${fcEsc(t ? t.note : '')}"></label>
        <div id="trkErr" class="fc-neg"></div>
        <div class="fc-scanbar"><button type="button" class="fc-toggle active" id="trkSave">SAVE</button><button type="button" class="fc-toggle" id="trkCancel">CANCEL</button>
            <span class="fc-muted">Metric names complete as you type (Tab accepts): ${(RG.metrics || []).length ? '' : 'loading…'}</span></div>`;
    const box = m.querySelector('#trkConds');
    (t ? t.conditions : [{ expr: '', op: '<' }]).forEach(c => box.appendChild(rgCondRow(c)));
    m.querySelector('#trkAddCond').onclick = () => { if (box.children.length < 4) box.appendChild(rgCondRow({ expr: '', op: '<' })); };
    m.querySelector('#trkCancel').onclick = () => m.classList.add('hidden');
    m.querySelector('#trkSave').onclick = async () => {
        const conditions = [...box.children].map(r => { const op = r.querySelector('.trk-op').value, v = r.querySelector('.trk-val').value.trim();
            return op === 'changes' ? { expr: r.querySelector('.trk-expr').value, op } : { expr: r.querySelector('.trk-expr').value, op, value: v }; });
        const body = { id: t ? t.id : undefined, name: m.querySelector('#trkName').value, conditions, priority: m.querySelector('#trkPrio').value,
            cooldown_min: Number(m.querySelector('#trkCool').value), hours: m.querySelector('#trkHours').value, note: m.querySelector('#trkNote').value, enabled: m.querySelector('#trkOn').checked };
        const r = await fetchJson(`${API_BASE}/api/trackers`, { method: 'POST', headers: rgHdr, body: JSON.stringify(body) });
        if (r.status !== 'success') { m.querySelector('#trkErr').textContent = r.message || 'could not save'; return; }
        m.classList.add('hidden'); rgLoadTrackers();
    };
    m.classList.remove('hidden');
    rgMetrics();
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') document.getElementById('trkModal')?.classList.add('hidden'); });
