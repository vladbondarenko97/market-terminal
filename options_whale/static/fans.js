// ==========================================================================================
// SCENARIO FANS (Kronos): 100 sampled paths per series next to a baseline band, with a path inspector.
// Data: GET /api/fans and GET /api/fans/scorecard (stored by the pipeline, see docs/scenario-fans-contract.md).
// Nothing is fetched or drawn until the KRONOS tab-bar toggle is on, except card 16 (the showcase), which loads when the
// Forecast Lab opens. Global: Fans.
// ==========================================================================================
const Fans = (() => {
    const KEY = 'mt_kronos_fans', NS = 'http://www.w3.org/2000/svg';
    const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const S = { on: false, p: null, fans: null, score: null, states: {}, card16: null };
    const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    const md = s => { const p = String(s || '').split('-'); return p.length < 3 ? '—' : `${MON[+p[1] - 1]} ${+p[2]}`; };
    const last = a => a[a.length - 1];
    const sgn = v => (v >= 0 ? '+' : '−') + Math.abs(v * 100).toFixed(1) + '%';
    const good = v => v >= 0 ? 'var(--fans-good)' : 'var(--fans-bad)';

    // units per the contract: kb with thousands separators, $ with 2 decimals, % and days with 1
    const dec = u => u === 'kb' ? 0 : (u === '%' || u === 'days') ? 1 : 2;
    const num = (u, v) => (v == null || !isFinite(v)) ? '—' :
        (v < 0 ? '−' : '') + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: dec(u), maximumFractionDigits: dec(u) });
    const fmt = (u, v) => {
        if (v == null || !isFinite(v)) return '—';
        const pre = u === '$' || u === '$/bbl' || u === '$/gal' ? '$' : '';
        const suf = { '%': '%', days: ' days', kb: ' kb', '$/bbl': '/bbl', '$/gal': '/gal' }[u] || '';
        return (v < 0 ? '−' : '') + pre + num(u, Math.abs(v)) + suf;
    };
    const ok = m => !!(m && m.status !== 'missing' && m.status !== 'error' && m.paths && m.paths.length && m.q);

    // ---------------------------------------------------------------- data
    function ensure() {
        if (S.p) return S.p;
        const get = u => (typeof fetchJson === 'function' ? fetchJson(u) : fetch(u).then(r => r.json()));
        S.p = Promise.all([get(`${API_BASE}/api/fans`).catch(e => ({ status: 'error', reason: e.message })),
                           get(`${API_BASE}/api/fans/scorecard`).catch(() => null)]).then(([f, sc]) => {
            S.fans = f; S.score = sc; return S;
        });
        return S.p;
    }
    const seriesMap = () => (S.fans && S.fans.series) || {};
    const whyMissing = () => (S.fans && (S.fans.reason || S.fans.message)) || 'no scenario fans in the latest run yet (they appear after the next pipeline run)';

    // scorecard: tolerate the field spellings the pipeline may use for the same number
    const pick = (o, ...ks) => { for (const k of ks) if (o && o[k] != null) return o[k]; return null; };
    const rate = v => v == null ? null : (v <= 1 ? v * 100 : v);
    function score(id, m) {
        const sc = S.score || {}, r = ((sc.series && sc.series[id]) || sc[id] || {})[m] || {};
        let bt = r.backtest || {}, lv = r.live || {};
        // the route nests the backtest by horizon (trading-day steps as keys: use the longest, 20 days) and the live grades by horizon label
        if (bt.by_horizon) { const ks = Object.keys(bt.by_horizon).sort((x, y) => +x - +y); bt = ks.length ? bt.by_horizon[last(ks)] : {}; }
        const lh = Object.values(lv.by_horizon || {}), liveN = lh.length ? lh.reduce((a, c) => a + (c.n || 0), 0) : lv.n;
        const lb = lh.find(c => c.coverage68 != null);
        return { n: bt.n, band: rate(pick(bt, 'band_hit', 'band_hit_80')), crps: pick(bt, 'crps_pct', 'crps', 'crps_price'),
                 dir: rate(pick(bt, 'dir_hit', 'direction_hit')), live: liveN, liveBand: rate(lb ? lb.coverage68 : pick(lv, 'band_hit_68')) };
    }
    function badge(id) {
        const k = score(id, 'kronos'), b = score(id, 'baseline');
        if (k.band == null && b.band == null && !k.live && !b.live) return 'no score yet';
        const p = v => v == null ? '—' : `${v.toFixed(0)}%`;
        return `Kronos · backtest band hit ${p(k.band)} · baseline ${p(b.band)} · live n=${k.live ?? 0}`;
    }

    // ---------------------------------------------------------------- geometry
    function dims(mini) {
        return mini ? { W: 420, H: 150, M: { l: 6, r: 6, t: 6, b: 6 } } : { W: 760, H: 420, M: { l: 52, r: 74, t: 16, b: 30 } };
    }
    function drawn(st, s) {       // which models draw paths, which draws the band
        const k = s.models && s.models.kronos, b = s.models && s.models.baseline;
        const paths = st.model === 'baseline' ? b : k;
        return { k, b, paths, pathsKey: st.model === 'baseline' ? 'baseline' : 'kronos', band: st.model !== 'kronos' && ok(b) ? b : null };
    }
    function geom(st, s) {
        const { W, H, M } = dims(st.mini), dm = drawn(st, s);
        const hv = s.hist.values, nh = hv.length, nf = s.dates.length, n = nh + nf;
        let all = hv.slice();
        if (ok(dm.paths)) dm.paths.paths.forEach(p => { all = all.concat(p); });
        if (dm.band) all = all.concat(dm.band.q.p10, dm.band.q.p90);
        if (st.levels !== false && !st.mini) (s.levels || []).forEach(L => { if (L.v >= Math.min(...all) * 0.9 && L.v <= Math.max(...all) * 1.1) all.push(L.v); });
        let lo = Math.min(...all), hi = Math.max(...all);
        const pad = ((hi - lo) || Math.abs(hi) || 1) * 0.06; lo -= pad; hi += pad;
        const w = W - M.l - M.r, cut = M.l + w * 0.6;   // history takes 60% of the width and the forecast the rest
        return { W, H, M, dm, nh, nf, n, lo, hi,
            x: i => i <= nh - 1 ? M.l + (cut - M.l) * i / Math.max(1, nh - 1) : cut + (W - M.r - cut) * (i - nh + 1) / Math.max(1, n - nh),
            y: v => M.t + (H - M.t - M.b) * (hi - v) / (hi - lo),
            inv: py => hi - (py - M.t) / (H - M.t - M.b) * (hi - lo) };
    }
    const niceStep = raw => { const p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p; return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p; };
    function mk(svg, name, a) { const e = document.createElementNS(NS, name); for (const k in a) e.setAttribute(k, a[k]); svg.appendChild(e); return e; }
    const pts = arr => arr.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
    const line = (svg, arr, a) => mk(svg, 'polyline', Object.assign({ points: pts(arr), fill: 'none' }, a));

    // ---------------------------------------------------------------- the SVG
    function draw(st) {
        const s = st.series(), svg = st.svg, u = s.unit, g = geom(st, s), { dm, M, W, H } = g, mini = st.mini;
        svg.textContent = '';
        svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
        const fut = arr => [[g.x(g.nh - 1), g.y(s.last)]].concat(arr.map((v, j) => [g.x(g.nh + j), g.y(v)]));
        const text = (x, y, t, a) => { const e = mk(svg, 'text', Object.assign({ x, y, class: 'fans-t' }, a)); e.textContent = t; return e; };
        if (!mini) {
            const step = niceStep((g.hi - g.lo) / 6);
            for (let v = Math.ceil(g.lo / step) * step; v <= g.hi; v += step) {
                mk(svg, 'line', { x1: M.l, x2: W - M.r, y1: g.y(v), y2: g.y(v), class: 'fans-grid' });
                text(M.l - 6, g.y(v) + 3, num(u, step < 1 && u !== '%' && u !== 'days' ? +v.toFixed(2) : +v.toFixed(step < 1 ? 1 : 0)), { 'text-anchor': 'end' });
            }
            const hd = s.hist.dates, nh = g.nh;
            [0, Math.round((nh - 1) / 2), nh - 1].forEach(i => text(g.x(i), H - 10, md(hd[i]), { 'text-anchor': 'middle' }));
            [Math.max(0, Math.floor(g.nf / 2) - 1), g.nf - 1].forEach(j => text(g.x(g.nh + j), H - 10, md(s.dates[j]), { 'text-anchor': 'middle' }));
        }
        const xn = g.x(g.nh - 1);
        mk(svg, 'line', { x1: xn, x2: xn, y1: M.t, y2: H - M.b, class: 'fans-now' });
        if (!mini) text(xn + 4, M.t + 10, `as of ${md(s.asof)} · ${num(u, s.last)}`);
        if (st.levels !== false && !mini) (s.levels || []).forEach(L => {
            if (L.v < g.lo || L.v > g.hi) return;
            mk(svg, 'line', { x1: M.l, x2: W - M.r, y1: g.y(L.v), y2: g.y(L.v), class: 'fans-level' });
            text(W - M.r + 4, g.y(L.v) + 3, L.name, { class: 'fans-t fans-lvt' });
        });
        if (dm.band) {
            const q = dm.band.q;
            mk(svg, 'polygon', { points: pts(fut(q.p90).concat(fut(q.p10).reverse())), class: 'fans-band' });
            line(svg, fut(q.p10), { class: 'fans-bline' }); line(svg, fut(q.p90), { class: 'fans-bline' });
        }
        const pm = dm.paths, col = dm.pathsKey === 'baseline' ? 'fans-pb' : 'fans-pk';
        const sel = st.pin != null ? st.pin : st.hover;
        if (ok(pm)) {
            pm.paths.forEach((p, i) => { if (i !== sel && (!mini || i % 4 === 0)) line(svg, fut(p), { class: col, opacity: sel == null ? 0.16 : 0.07 }); });
            ['p10', 'p50', 'p90'].forEach(k => line(svg, fut(pm.q[k]), { class: col + ' fans-q', 'stroke-width': k === 'p50' ? 2 : 1.4, 'stroke-dasharray': k === 'p50' ? '' : '4 3' }));
        } else {            // a missing model shows its reason, never invented paths
            const why = (pm && (pm.reason || pm.status)) || 'no data';
            text((xn + W - M.r) / 2, H / 2, mini ? 'no paths' : `${dm.pathsKey === 'baseline' ? 'Baseline' : 'Kronos'}: ${why}`.slice(0, 70),
                { 'text-anchor': 'middle', class: 'fans-t fans-miss' });
        }
        line(svg, s.hist.values.map((v, i) => [g.x(i), g.y(v)]), { class: 'fans-hist' });
        if (sel != null && ok(pm)) {
            line(svg, fut(pm.paths[sel]), { class: 'fans-sel' });
            mk(svg, 'circle', { cx: g.x(g.n - 1), cy: g.y(last(pm.paths[sel])), r: 4, class: 'fans-dot' });
        }
        if (!mini && ok(pm)) [['p90', pm.q.p90], ['p50', pm.q.p50], ['p10', pm.q.p10]].forEach(a =>
            text(g.x(g.n - 1) + 6, g.y(last(a[1])) + 3, `${a[0]} ${num(u, last(a[1]))}`, { class: 'fans-t ' + col }));
        if (mini) return;
        const hit = mk(svg, 'rect', { x: xn, y: M.t, width: W - M.r - xn, height: H - M.t - M.b, class: 'fans-hit' });
        if (!ok(pm)) return;
        const pickPath = ev => {
            const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * W / r.width, py = (ev.clientY - r.top) * H / r.height;
            const j = Math.max(0, Math.min(g.nf - 1, Math.round((px - g.x(g.nh)) / (g.x(g.nh + 1) - g.x(g.nh))))), val = g.inv(py);
            let best = 0, bd = Infinity;
            pm.paths.forEach((p, i) => { const d = Math.abs(p[j] - val); if (d < bd) { bd = d; best = i; } });
            return best;
        };
        hit.addEventListener('pointermove', ev => { if (st.pin != null) return; const b = pickPath(ev); if (b !== st.hover) { st.hover = b; refresh(st); } });
        hit.addEventListener('pointerleave', () => { if (st.pin == null && st.hover != null) { st.hover = null; refresh(st); } });
        hit.addEventListener('click', ev => { const b = pickPath(ev); st.pin = st.pin === b ? null : b; st.hover = b; refresh(st); });
    }

    // ---------------------------------------------------------------- the inspector
    function inspect(st) {
        const s = st.series(), u = s.unit, dm = drawn(st, s), pm = dm.paths, b = dm.b, weekly = s.freq === 'W', unit = weekly ? 'week' : 'day';
        const bl = ok(b) ? `${num(u, last(b.q.p10))}–${num(u, last(b.q.p90))}` : null;
        if (!ok(pm)) return `<span class="fans-eye">${esc(dm.pathsKey)}</span><p class="fans-hint">${esc((pm && pm.reason) || 'no paths for this series')}</p>` +
            (bl ? `<dl class="fans-kv"><dt>Baseline 80% range</dt><dd>${bl}</dd></dl>` : '');
        const mid = (() => { const e = pm.paths.map((p, i) => [last(p), i]).sort((a, c) => a[0] - c[0]); return e[Math.floor(e.length / 2)][1]; })();
        const i = st.pin != null ? st.pin : st.hover != null ? st.hover : mid, p = pm.paths[i], s0 = s.last, end = last(p);
        const ends = pm.paths.map(last).sort((a, c) => a - c), rank = Math.round(100 * ends.indexOf(end) / Math.max(1, ends.length - 1));
        const lo = Math.min(...p), hi = Math.max(...p), loD = p.indexOf(lo), hiD = p.indexOf(hi);
        const rel = (v, w) => w ? (v - w) / Math.abs(w) : 0;
        const r = p.map((v, k) => rel(v, k ? p[k - 1] : s0));
        const mu = r.reduce((a, c) => a + c, 0) / r.length, sd = Math.sqrt(r.reduce((a, c) => a + (c - mu) * (c - mu), 0) / Math.max(1, r.length - 1));
        const big = r.reduce((a, c, k) => Math.abs(c) > Math.abs(a.v) ? { v: c, k } : a, { v: 0, k: 0 });
        const chg = rel(end, s0), at = k => md(s.dates[k]);
        const needs = [`${esc(s.label)} ${chg >= 0 ? 'rises' : 'falls'} ${sgn(chg).replace(/^[+−]/, '')} to ${fmt(u, end)} by ${at(s.dates.length - 1)}.`];
        if (lo < s0) needs.push(`Its low is ${fmt(u, lo)} (${sgn(rel(lo, s0))}) on ${at(loD)}.`);
        if (hi > s0) needs.push(`Its high is ${fmt(u, hi)} (${sgn(rel(hi, s0))}) on ${at(hiD)}.`);
        (s.levels || []).forEach(L => {
            const j = p.findIndex(v => L.v >= s0 ? v >= L.v : v <= L.v);
            if (j >= 0) needs.push(`${L.v >= s0 ? 'Reaches' : 'Breaks'} ${esc(L.name)} on ${at(j)} (${unit} ${j + 1}).`);
        });
        needs.push(`Biggest single ${unit}: ${sgn(big.v)} on ${at(big.k)}.`);
        const inBase = ok(b) && end >= last(b.q.p10) && end <= last(b.q.p90);
        return `<span class="fans-eye">${st.pin != null ? 'Pinned path' : st.hover != null ? 'Path' : 'Middle path'} #${i + 1} of ${pm.paths.length}</span>
            <div class="fans-big">${fmt(u, end)} <span style="font-size:13px;color:${good(chg)}">${sgn(chg)}</span></div>
            <dl class="fans-kv"><dt>Ends at percentile</dt><dd>${rank} of 100</dd><dt>Worst point</dt><dd>${sgn(rel(lo, s0))}</dd><dt>Best point</dt><dd>${sgn(rel(hi, s0))}</dd>
            <dt>Path volatility</dt><dd>${(sd * Math.sqrt(weekly ? 52 : 252) * 100).toFixed(0)}% a year</dd>${bl ? `<dt>Baseline 80% range</dt><dd>${bl}</dd>` : ''}</dl>
            <div class="fans-need"><h4>What has to happen</h4><ul>${needs.map(x => `<li>${x}</li>`).join('')}</ul></div>
            <p class="fans-hint">${bl ? (inBase ? 'This ending is inside the baseline’s 80% range. ' : 'This ending is outside the baseline’s 80% range, so the baseline rates it unlikely. ') : ''}${dm.pathsKey === 'kronos' ? 'Kronos samples each path from its learned odds. It shows what a path requires, not why it would happen.' : 'The baseline reshuffles the series’ own past changes; it shows what a path requires, not why.'}</p>
            <p class="fans-hint">${st.pin != null ? 'Click the path again to unpin it.' : 'Hover the fan to inspect a path; tap or click to pin it.'}</p>`;
    }

    function refresh(st) {
        draw(st);
        if (st.insp) st.insp.innerHTML = inspect(st);
    }

    // ---------------------------------------------------------------- the panel
    // opts: key (keeps pin/model across re-renders), onSeries(id) -> html under the fan, mini
    function panel(container, ids, opts = {}) {
        const all = seriesMap(), have = ids.filter(id => all[id]);
        if (!have.length) { container.innerHTML = `<div class="fc-muted">Scenario fans: ${esc(whyMissing())}</div>`; return null; }
        const prev = opts.key && S.states[opts.key];
        const st = prev || { id: have[0], model: 'both', levels: true, hover: null, pin: null };
        if (!have.includes(st.id)) st.id = have[0];
        if (opts.key) S.states[opts.key] = st;
        st.series = () => all[st.id];
        container.innerHTML = `<div class="fans-panel">
            <div class="fans-top"><div class="fc-seg fans-ids"></div><div class="fc-seg fans-models"></div>
                <label class="ch-tog"><input type="checkbox" class="fans-lv" ${st.levels ? 'checked' : ''}> levels</label></div>
            <div class="fans-badge"></div><div class="fans-body"><div class="fans-fig"><svg class="fans-svg" role="img"></svg><div class="fans-legend"></div></div>
            <div class="fans-insp"></div></div><div class="fans-extra"></div></div>`;
        const q = c => container.querySelector(c);
        st.svg = q('.fans-svg'); st.insp = q('.fans-insp');
        const ctl = () => {
            const s = st.series();
            q('.fans-ids').innerHTML = have.length > 1 ? have.map(id => `<button data-id="${esc(id)}" class="${id === st.id ? 'active' : ''}">${esc(all[id].label)}</button>`).join('') : '';
            q('.fans-models').innerHTML = [['both', 'Both'], ['kronos', 'Kronos'], ['baseline', 'Baseline']].map(([k, l]) => `<button data-m="${k}" class="${k === st.model ? 'active' : ''}">${l}</button>`).join('');
            q('.fans-badge').innerHTML = `<span class="fans-tag">${esc(badge(st.id))}</span>${s.note ? ` <span class="fans-tag fans-warn">${esc(s.note)}</span>` : ''}`;
            q('.fans-legend').innerHTML = `<span><i class="fans-sw fans-hist"></i>history</span><span><i class="fans-sw fans-pk"></i>Kronos paths · p10/p50/p90</span>
                <span><i class="fans-sw fans-bandsw"></i>baseline 80% band</span>${(s.levels || []).length ? '<span><i class="fans-sw fans-level"></i>levels</span>' : ''}`;
            const ex = q('.fans-extra'); ex.innerHTML = opts.extra ? opts.extra(st.id, s) || '' : '';
            refresh(st);
        };
        q('.fans-ids').addEventListener('click', e => { const b = e.target.closest('button'); if (b) { st.id = b.dataset.id; st.hover = st.pin = null; ctl(); } });
        q('.fans-models').addEventListener('click', e => { const b = e.target.closest('button'); if (b) { st.model = b.dataset.m; st.hover = st.pin = null; ctl(); } });
        q('.fans-lv').addEventListener('change', e => { st.levels = e.target.checked; refresh(st); });
        ctl();
        return st;
    }

    // a small non-interactive fan; onClick opens the full panel
    function mini(container, id, onClick) {
        const s = seriesMap()[id]; if (!s) return;
        const st = { mini: true, model: 'both', series: () => s };
        const wrap = document.createElement('button');
        wrap.className = 'fans-mini'; wrap.type = 'button'; wrap.title = `${s.label}: open the full fan`;
        const k = s.models && s.models.kronos, p50 = ok(k) ? last(k.q.p50) : null;
        wrap.innerHTML = `<div class="fans-mh"><b>${esc(s.label)}</b><span>${num(s.unit, s.last)}${p50 != null && s.last ? ` <i style="color:${good(p50 - s.last)}">${sgn((p50 - s.last) / Math.abs(s.last))}</i>` : ''}</span></div><svg class="fans-svg"></svg>`;
        container.appendChild(wrap);
        st.svg = wrap.querySelector('svg'); draw(st);
        wrap.addEventListener('click', () => onClick(id));
    }

    // ---------------------------------------------------------------- card 16: SCENARIO LAB
    const crpsF = v => v.toFixed(2) + '%';
    function leaderboard(ids) {
        const all = seriesMap(), rows = [];
        ids.forEach(id => {
            const k = score(id, 'kronos'), b = score(id, 'baseline');
            let win = null;
            if (k.crps != null && b.crps != null) win = k.crps < b.crps ? 'kronos' : 'baseline';
            else if (k.band != null && b.band != null) win = Math.abs(k.band - 80) < Math.abs(b.band - 80) ? 'kronos' : 'baseline';
            [['kronos', k], ['baseline', b]].forEach(([m, x]) => {
                const none = x.band == null && x.crps == null && x.dir == null && !x.live;
                const vs = x.crps == null ? '—' : m === 'baseline' || !b.crps ? crpsF(x.crps) : `${crpsF(x.crps)} <span class="fc-muted">(${(x.crps / b.crps).toFixed(2)}× baseline)</span>`;
                const p = v => v == null ? '—' : v.toFixed(0) + '%';
                rows.push(`<tr class="${win === m ? 'fans-win' : ''}"><td>${m === 'kronos' ? esc(all[id].label) : ''}</td><td>${m}</td>
                    ${none ? '<td colspan="4" class="fc-muted">no score yet</td>' : `<td>${p(x.band)} <span class="fc-muted">/ 80%</span></td><td>${vs}</td><td>${p(x.dir)}</td><td>${x.live ?? 0}</td>`}</tr>`);
            });
        });
        return `<table class="fc-table fans-lb"><tr><th>Series</th><th>Model</th><th>Backtest band hit</th><th>CRPS (% of price)</th><th>Direction hit</th><th>Live n</th></tr>${rows.join('')}</table>`;
    }
    function card16() {
        const el = document.getElementById('fc16'); if (!el) return;
        el.innerHTML = '<div class="fc-muted">Loading scenario fans…</div>';
        ensure().then(() => {
            const all = seriesMap(), GO = ['markets', 'fuels', 'inventories', 'derived'];
            const ids = Object.keys(all).sort((a, b) => GO.indexOf(all[a].group) - GO.indexOf(all[b].group));
            if (!ids.length) { el.innerHTML = `<div class="fc-muted">Unavailable: ${esc(whyMissing())}</div>`; return; }
            const kr = S.fans.kronos || {};
            el.innerHTML = `<p class="fans-lead">Each fan is 100 possible futures for one series: Kronos, a model trained on market candles, samples the paths, and the baseline reshuffles the series’ own past changes (or last years’ season for weekly data). A path shows what it would take for the market to get there, not why it would; the table shows which of the two has actually been calibrated on history (a good 80% band holds the real outcome 80% of the time).</p>
                ${kr.status && kr.status !== 'fresh' ? `<div class="fc-note">Kronos: ${esc(kr.reason || kr.status)}</div>` : ''}
                <div class="fans-grid"></div><div class="fans-detail"></div>
                <div class="fc-sub" style="margin:10px 0 4px">LEADERBOARD</div><div class="fans-lbw">${leaderboard(ids)}</div>`;
            const detail = el.querySelector('.fans-detail');
            const open = id => {
                const group = ids.filter(x => all[x].group === all[id].group);
                S.states.c16 = Object.assign(S.states.c16 || { model: 'both', levels: true }, { id, hover: null, pin: null });
                panel(detail, group, { key: 'c16' });
                detail.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
            };
            ids.forEach(id => mini(el.querySelector('.fans-grid'), id, open));
        });
    }

    // ---------------------------------------------------------------- overlays on cards 1, 10, 11
    const OVER = { fc10: ['HO', 'RB', 'CL', 'NG', 'crack.diesel', 'crack.321'],
                   fc11: ['eia.WDISTUS1', 'eia.WGTSTUS1', 'eia.WKJSTUS1', 'eia.WCESTUS1', 'eia.WCSSTUS1', 'eia.WPULEUS3', 'days.distillate', 'days.gasoline', 'days.jet'] };
    // 10th and 90th percentile of the options-implied density (a list of [price, density]) by cumulative trapezoid
    function impliedBand(hz) {
        const c = (hz && hz.curve || []).filter(p => p && p[0] != null && p[1] != null).sort((a, b) => a[0] - b[0]);
        if (c.length < 3) return null;
        const cum = [0]; for (let i = 1; i < c.length; i++) cum.push(cum[i - 1] + (c[i][0] - c[i - 1][0]) * (c[i][1] + c[i - 1][1]) / 2);
        const tot = last(cum); if (!(tot > 0)) return null;
        const at = f => { const t = f * tot; let i = cum.findIndex(v => v >= t); if (i <= 0) return c[0][0]; return c[i - 1][0] + (c[i][0] - c[i - 1][0]) * (t - cum[i - 1]) / ((cum[i] - cum[i - 1]) || 1); };
        return [at(0.1), at(0.9)];
    }
    function fc1Line(id, s) {
        const u = s.unit, rng = m => ok(m) ? `${num(u, last(m.q.p10))}–${num(u, last(m.q.p90))}` : 'n/a';
        const hz = typeof FC !== 'undefined' && FC.data && FC.data.data && FC.data.data.implied && FC.data.data.implied.horizons ? FC.data.data.implied.horizons['1m'] : null;
        const ib = hz && hz.status === 'fresh' ? impliedBand(hz) : null;
        return `<div class="fc-note">Kronos p10–p90 at ${s.dates.length} days: ${rng(s.models.kronos)} · baseline: ${rng(s.models.baseline)} · options imply: ${ib ? `${num(u, ib[0])}–${num(u, ib[1])}` : 'n/a'} (1M expiry, 10th to 90th percentile of the implied density)</div>`;
    }
    function attach(only) {
        const targets = { fc1: () => [typeof FC !== 'undefined' ? FC.ticker : 'SPY'], fc10: () => OVER.fc10, fc11: () => OVER.fc11 };
        Object.keys(targets).filter(c => !only || c === only).forEach(card => {
            const body = document.getElementById(card); if (!body) return;
            body.querySelectorAll('.fans-over').forEach(n => n.remove());
            if (!S.on) return;
            const box = document.createElement('div'); box.className = 'fans-over';
            const anchor = body.querySelector('.fc-chart, .ch-chart');
            if (anchor) anchor.insertAdjacentElement('afterend', box); else body.appendChild(box);
            box.innerHTML = '<div class="fc-muted">Loading scenario fans…</div>';
            const ids = targets[card]();
            ensure().then(() => { if (box.isConnected) panel(box, ids, { key: card + ':' + ids[0], extra: card === 'fc1' ? fc1Line : null }); });
        });
    }

    // ---------------------------------------------------------------- toggle
    function setOn(on, save) {
        S.on = !!on;
        const cb = document.getElementById('fansToggle'); if (cb) cb.checked = S.on;
        if (save) { try { localStorage.setItem(KEY, S.on ? '1' : '0'); } catch (e) { /* private window */ } }
        attach();
        window.dispatchEvent(new CustomEvent('fans:toggle', { detail: { on: S.on } }));
    }
    function init() {
        let v = null; try { v = localStorage.getItem(KEY); } catch (e) { /* no storage */ }
        const cb = document.getElementById('fansToggle');
        if (cb) cb.addEventListener('change', () => setOn(cb.checked, true));
        S.on = v === '1'; if (cb) cb.checked = S.on;
    }
    // after a run finishes: forget what was loaded and redraw what is on screen
    function reload() {
        S.p = null; S.fans = null; S.score = null;
        if (S.on) attach();
        if (document.getElementById('fc16') && typeof FC !== 'undefined' && FC.fans16) card16();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();

    // cards 1, 10 and 11 rewrite their body on every render: put the fan back after each one
    [['renderImplied', 'fc1'], ['renderDiesel', 'fc10'], ['renderInventories', 'fc11']].forEach(([n, card]) => {
        const o = window[n];
        if (typeof o === 'function') window[n] = function () { const r = o.apply(this, arguments); try { attach(card); } catch (e) { console.error('fans', e); } return r; };
    });
    return { enabled: () => S.on, panel, card16, attach, reload, ensure, copyData: () => ({ fans: S.fans, scorecard: S.score }), _impliedBand: impliedBand };
})();
window.Fans = Fans;
