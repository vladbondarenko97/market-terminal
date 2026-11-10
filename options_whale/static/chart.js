// CHART tab: TradingView's Lightweight Charts (v5) over our stored bars and series. Routes: /api/chart/* (docs/api.md).
// Names and descriptions come from Yahoo and our own catalog: they are only ever put in with textContent.
(function () {
    const LS_KEY = 'mt.chart.v1';
    const TFS = [['1m', '1m'], ['5m', '5m'], ['15m', '15m'], ['30m', '30m'], ['1h', '1h'], ['4h', '4h'], ['1d', '1D'], ['1wk', '1W'], ['1mo', '1M']];
    const RANGES = [['1D', 1], ['5D', 5], ['1M', 30], ['3M', 91], ['6M', 182], ['YTD', 'ytd'], ['1Y', 365], ['5Y', 1826], ['MAX', null]];
    const DEFAULT_RANGE = { '1m': '1D', '5m': '5D', '15m': '5D', '30m': '1M', '1h': '3M', '4h': '6M', '1d': '1Y', '1wk': '5Y', '1mo': 'MAX' };
    const COLORS = ['#f59e0b', '#22d3ee', '#d946ef', '#3ddc84', '#f472b6', '#a78bfa'];
    const DAY = 86400;
    let st = { symbol: 'SPY', tf: '1d', range: '1Y', series: [] };      // series: [{id, where: 'left' | 'pane'}]
    try { Object.assign(st, JSON.parse(localStorage.getItem(LS_KEY) || '{}')); } catch (e) { /* no storage: defaults */ }
    if (!TFS.some(x => x[0] === st.tf)) st.tf = '1d';
    if (!RANGES.some(x => x[0] === st.range)) st.range = DEFAULT_RANGE[st.tf];
    if (!/^[A-Z0-9.^=\-]{1,15}$/.test(String(st.symbol))) st.symbol = 'SPY';
    if (!Array.isArray(st.series)) st.series = [];
    const save = () => { try { localStorage.setItem(LS_KEY, JSON.stringify(st)); } catch (e) { /* ignore */ } };

    let chart, candles, volume, last = null, seq = 0, retryT = null, retryStart = 0, catalog = null, started = false;
    const lines = {};                                                   // id -> {api, meta, t, v}
    const symInfo = {};                                                 // symbol -> {name, kind, description}
    const $ = id => document.getElementById(id);
    const h = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
    const tz = () => ['1d', '1wk', '1mo'].includes(st.tf) ? 'UTC' : 'America/New_York';
    const fmtDate = t => new Date(t * 1000).toLocaleDateString('en-CA', { timeZone: tz() });
    const fmtNum = v => v == null ? '-' : Number(v).toLocaleString('en-US', { maximumFractionDigits: Math.abs(v) < 10 ? 4 : 2 });
    const status = (msg, bad) => { const s = $('chStatus'); s.textContent = msg || ''; s.className = 'ch-status' + (bad ? ' bad' : ''); };

    function setChartOptions() {
        const o = { timeZone: tz() };
        chart.applyOptions({
            localization: { timeFormatter: t => new Date(t * 1000).toLocaleString('en-US', { ...o, month: 'short', day: 'numeric', year: '2-digit', hour: st.tf.match(/^\d+[mh]$/) ? '2-digit' : undefined, minute: st.tf.match(/^\d+[mh]$/) ? '2-digit' : undefined, hour12: false }) },
            timeScale: {
                timeVisible: !['1d', '1wk', '1mo'].includes(st.tf),
                tickMarkFormatter: (t, type) => {
                    const d = new Date(t * 1000);
                    if (type === 0) return d.toLocaleDateString('en-US', { ...o, year: 'numeric' });
                    if (type === 1) return d.toLocaleDateString('en-US', { ...o, month: 'short' });
                    if (type === 2) return d.toLocaleDateString('en-US', { ...o, month: 'short', day: 'numeric' });
                    return d.toLocaleTimeString('en-US', { ...o, hour: '2-digit', minute: '2-digit', hour12: false });
                },
            },
        });
    }

    function build() {
        const LW = window.LightweightCharts;
        if (!LW) { status('Chart library did not load (jsDelivr blocked?).', true); return false; }
        chart = LW.createChart($('chChart'), {
            autoSize: true,
            layout: { background: { color: '#000000' }, textColor: '#a1a1aa', panes: { separatorColor: '#27272a' } },
            grid: { vertLines: { color: '#18181b' }, horzLines: { color: '#18181b' } },
            rightPriceScale: { borderColor: '#27272a' }, leftPriceScale: { borderColor: '#27272a', visible: false },
            timeScale: { borderColor: '#27272a' }, crosshair: { mode: 0 },
        });
        candles = chart.addSeries(LW.CandlestickSeries, { upColor: '#22c55e', downColor: '#ef4444', borderVisible: false, wickUpColor: '#22c55e', wickDownColor: '#ef4444' });
        candles.priceScale().applyOptions({ scaleMargins: { top: 0.05, bottom: 0.25 } });
        volume = chart.addSeries(LW.HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false });
        chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
        setChartOptions();
        return true;
    }

    // ---- range: the visible window is anchored on the newest bar, so a weekend or holiday still shows data
    function rangeDays() { return RANGES.find(r => r[0] === st.range)[1]; }
    function fetchFrom() {
        const d = rangeDays(), now = new Date();
        if (d === null) return '';
        const start = d === 'ytd' ? new Date(Date.UTC(now.getUTCFullYear(), 0, 1)) : new Date(now.getTime() - d * DAY * 1000);
        return new Date(start.getTime() - 5 * DAY * 1000).toISOString().slice(0, 10);
    }
    function applyRange() {
        if (!last || !last.t.length) return;
        const d = rangeDays(), t = last.t, end = t[t.length - 1], ts = chart.timeScale();
        if (d === null) { ts.fitContent(); return; }
        const from = d === 'ytd' ? Date.UTC(new Date(end * 1000).getUTCFullYear(), 0, 1) / 1000 : end - d * DAY;
        try { ts.setVisibleRange({ from: Math.max(from, t[0]), to: end }); } catch (e) { ts.fitContent(); }
    }

    // ---- main symbol
    async function loadBars() {
        const my = ++seq;
        clearTimeout(retryT);
        const from = fetchFrom();
        const r = await fetchJson(`${API_BASE}/api/chart/bars?symbol=${encodeURIComponent(st.symbol)}&tf=${st.tf}` + (from ? `&from=${from}` : ''));
        if (my !== seq) return;
        if (r.status === 'loading') {
            retryStart = retryStart || Date.now();
            if (Date.now() - retryStart > 120000) { retryStart = 0; status(`${st.symbol}: still no history after 2 minutes. Try again later.`, true); return; }
            status(`Loading ${st.symbol} history…`);
            retryT = setTimeout(loadBars, 3000);
            return;
        }
        retryStart = 0;
        if (r.status !== 'success') { status(r.message || 'chart data unavailable', true); return; }
        status('');
        last = r;
        const bars = [], vols = [];
        r.t.forEach((t, i) => {
            if (r.o[i] == null || r.h[i] == null || r.l[i] == null || r.c[i] == null) return;
            bars.push({ time: t, open: r.o[i], high: r.h[i], low: r.l[i], close: r.c[i] });
            if (r.v[i]) vols.push({ time: t, value: r.v[i], color: r.c[i] >= r.o[i] ? 'rgba(34,197,94,.35)' : 'rgba(239,68,68,.35)' });
        });
        setChartOptions();
        candles.setData(bars);
        volume.setData(vols);
        if (!bars.length) status(`No ${st.tf} bars stored for ${st.symbol} yet.`);
        applyRange();
        if (!symInfo[st.symbol]) { catalog = null; await loadCatalog(); }
        renderInfo();
    }

    // ---- extra series (non-price lines)
    async function addLine(id, where) {
        if (lines[id]) return;
        const r = await fetchJson(`${API_BASE}/api/chart/series?id=${encodeURIComponent(id)}`);
        if (r.status !== 'success') { status(r.message || `${id} unavailable`, true); return; }
        const LW = window.LightweightCharts;
        const color = COLORS[Object.keys(lines).length % COLORS.length];
        if (where === 'left') chart.priceScale('left').applyOptions({ visible: true });
        // ponytail: every 'left' series shares the one left axis; give each its own pane if their units differ
        const api = chart.addSeries(LW.LineSeries, { color, lineWidth: 2, priceScaleId: where === 'left' ? 'left' : 'right', title: r.label }, where === 'pane' ? 1 : 0);
        api.setData(r.t.map((t, i) => ({ time: t, value: r.v[i] })));
        lines[id] = { api, meta: r, where, color };
        if (!st.series.some(s => s.id === id)) { st.series.push({ id, where }); save(); }
        renderInfo();
    }
    function removeLine(id) {
        const l = lines[id];
        if (l) { chart.removeSeries(l.api); delete lines[id]; }
        st.series = st.series.filter(s => s.id !== id);
        if (!st.series.some(s => s.where === 'left')) chart.priceScale('left').applyOptions({ visible: false });
        save();
        renderInfo();
    }

    // ---- info strip and tracked list
    function renderInfo() {
        const box = $('chInfo');
        box.replaceChildren();
        const info = symInfo[st.symbol] || {};
        const row = (title, desc, val, onX, color) => {
            const d = h('div', 'ch-info-row');
            if (color) d.style.borderLeftColor = color;
            d.append(h('b', null, title), h('span', 'ch-desc', desc || ''), h('span', 'ch-val', val));
            if (onX) { const x = h('button', 'ch-x', '×'); x.title = 'Remove'; x.onclick = onX; d.append(x); }
            box.append(d);
        };
        if (last && last.t.length) {
            const i = last.t.length - 1;
            row(st.symbol + (info.name ? ' · ' + info.name : ''), info.description, `${fmtNum(last.c[i])} · ${fmtDate(last.t[i])}`, null, '#22c55e');
        } else {
            row(st.symbol, info.description, '', null, '#22c55e');
        }
        Object.entries(lines).forEach(([id, l]) => {
            const n = l.meta.t.length;
            row(l.meta.label, l.meta.description, n ? `${fmtNum(l.meta.v[n - 1])} ${l.meta.unit} · ${new Date(l.meta.t[n - 1] * 1000).toISOString().slice(0, 10)}` : 'no data', () => removeLine(id), l.color);
        });
    }
    async function loadCatalog() {
        if (!catalog) catalog = await fetchJson(`${API_BASE}/api/chart/catalog`);
        if (catalog.status !== 'success') return catalog;
        catalog.symbols.forEach(s => { symInfo[s.symbol] = s; });
        const box = $('chTracked');
        box.replaceChildren(h('span', 'ch-tracked-h', 'TRACKED'));
        catalog.symbols.forEach(s => {
            const b = h('button', 'ch-chip');
            b.append(h('b', null, s.symbol), h('span', null, ' ' + (s.description || s.name || '')));
            b.onclick = () => pick(s.symbol);
            box.append(b);
        });
        return catalog;
    }

    // ---- symbol box and pickers
    function pick(sym) {
        sym = String(sym || '').trim().toUpperCase();
        if (!/^[A-Z0-9.^=\-]{1,15}$/.test(sym)) { status('That is not a valid symbol.', true); return; }
        st.symbol = sym; save();
        $('chSym').value = sym;
        $('chLookup').classList.add('hidden');
        retryStart = 0;
        status('');
        loadBars();
    }
    function dropdown(box, rows) {
        box.replaceChildren(...rows);
        box.classList.toggle('hidden', !rows.length);
    }
    let lookupT = null, lookupSeq = 0;
    function onType() {
        clearTimeout(lookupT);
        const q = $('chSym').value.trim();
        if (!q) { $('chLookup').classList.add('hidden'); return; }
        lookupT = setTimeout(async () => {
            const my = ++lookupSeq;
            const r = await fetchJson(`${API_BASE}/api/chart/lookup?q=${encodeURIComponent(q)}`);
            if (my !== lookupSeq || r.status !== 'success') return;
            dropdown($('chLookup'), r.matches.map(m => {
                symInfo[m.symbol] = symInfo[m.symbol] || m;
                const row = h('button', 'ch-row');
                row.append(h('b', null, m.symbol), h('span', 'ch-kind', ` ${m.kind || ''}${m.exchange ? ' · ' + m.exchange : ''}`),
                    h('div', 'ch-name', m.name || ''), h('div', 'ch-desc', m.description || (m.stored ? '' : 'not stored yet: loads its history on first use')));
                row.onmousedown = e => { e.preventDefault(); pick(m.symbol); };
                return row;
            }));
        }, 250);
    }
    async function openSeriesPicker() {
        const box = $('chSeriesPick');
        if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
        const c = await loadCatalog();
        if (c.status !== 'success') { status(c.message || 'catalog unavailable', true); return; }
        dropdown(box, c.series.map(s => {
            const row = h('div', 'ch-row');
            row.append(h('b', null, s.label), h('span', 'ch-kind', ` ${s.unit} · ${s.freq}${s.last ? ' · to ' + s.last : ''}`), h('div', 'ch-desc', s.description));
            const act = h('div', 'ch-act');
            [['left', 'LEFT SCALE'], ['pane', 'LOWER PANE']].forEach(([where, label]) => {
                const b = h('button', 'ch-btn', label);
                b.onclick = () => { box.classList.add('hidden'); addLine(s.id, where); };
                act.append(b);
            });
            row.append(act);
            return row;
        }));
    }

    function buttons(box, list, key) {
        list.forEach(([val, label]) => {
            const b = h('button', 'ch-btn', label);
            b.dataset.v = val;
            b.onclick = () => {
                st[key] = val;
                if (key === 'tf') st.range = DEFAULT_RANGE[val];
                save(); mark(); loadBars();
            };
            box.append(b);
        });
    }
    function mark() {
        document.querySelectorAll('#chTf .ch-btn').forEach(b => b.classList.toggle('on', b.dataset.v === st.tf));
        document.querySelectorAll('#chRange .ch-btn').forEach(b => b.classList.toggle('on', b.dataset.v === st.range));
    }

    window.initChartTab = function () {
        if (started) return;
        started = true;
        buttons($('chTf'), TFS, 'tf');
        buttons($('chRange'), RANGES.map(r => [r[0], r[0]]), 'range');
        mark();
        const input = $('chSym');
        input.value = st.symbol;
        input.addEventListener('input', onType);
        input.addEventListener('keydown', e => { if (e.key === 'Enter') pick(input.value); if (e.key === 'Escape') $('chLookup').classList.add('hidden'); });
        input.addEventListener('blur', () => setTimeout(() => $('chLookup').classList.add('hidden'), 150));
        $('chAdd').onclick = openSeriesPicker;
        document.addEventListener('click', e => { if (!e.target.closest('#chAddWrap')) $('chSeriesPick').classList.add('hidden'); });
        if (!build()) return;
        loadCatalog().then(renderInfo);
        loadBars();
        st.series.slice().forEach(s => addLine(s.id, s.where));
    };
})();
