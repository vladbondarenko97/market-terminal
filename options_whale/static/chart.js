// CHART tab: TradingView's Lightweight Charts (v5) over our stored bars and series. Routes: /api/chart/* (docs/api.md).
// Names, descriptions and headlines come from Yahoo, our catalog and news feeds: they are only ever put in with textContent.
(function (root) {
    // The header marks a request as the CHART tab's own: a page on another site cannot set it without a CORS preflight,
    // which this server never grants, so only this page can start a download (bars --symbol, headlines --date).
    const chartJson = url => fetchJson(url, { headers: { 'X-Chart-Request': '1' } });
    // ---- pure math (also exported for tests/chart_math.test.mjs). Arrays are aligned with the bars; no value yet = null.
    const nn = x => x != null && isFinite(x);
    const CI = {
        sma(a, n) {
            const o = a.map(() => null), f = a.findIndex(nn);
            if (f < 0) return o;
            let s = 0;
            for (let i = f; i < a.length; i++) { s += a[i]; if (i - f >= n) s -= a[i - n]; if (i - f >= n - 1) o[i] = s / n; }
            return o;
        },
        ema(a, n) {
            const o = a.map(() => null), f = a.findIndex(nn);
            if (f < 0 || a.length - f < n) return o;
            let e = 0;
            for (let i = f; i < f + n; i++) e += a[i];
            e /= n; o[f + n - 1] = e;
            const k = 2 / (n + 1);
            for (let i = f + n; i < a.length; i++) { e = a[i] * k + e * (1 - k); o[i] = e; }
            return o;
        },
        wma(a, n) {
            const w = n * (n + 1) / 2;
            return a.map((_, i) => { if (i < n - 1) return null; let s = 0; for (let j = 0; j < n; j++) s += a[i - n + 1 + j] * (j + 1); return s / w; });
        },
        bb(c, n, m) {
            const mid = CI.sma(c, n);
            const sd = c.map((_, i) => { if (mid[i] == null) return null; let s = 0; for (let j = i - n + 1; j <= i; j++) s += (c[j] - mid[i]) ** 2; return Math.sqrt(s / n); });
            return { mid, up: mid.map((v, i) => v == null ? null : v + m * sd[i]), lo: mid.map((v, i) => v == null ? null : v - m * sd[i]) };
        },
        rsi(c, n) {                                   // Wilder smoothing
            const o = c.map(() => null);
            if (c.length <= n) return o;
            let g = 0, l = 0;
            for (let i = 1; i <= n; i++) { const d = c[i] - c[i - 1]; if (d > 0) g += d; else l -= d; }
            g /= n; l /= n;
            const rs = () => l === 0 ? 100 : 100 - 100 / (1 + g / l);
            o[n] = rs();
            for (let i = n + 1; i < c.length; i++) { const d = c[i] - c[i - 1]; g = (g * (n - 1) + (d > 0 ? d : 0)) / n; l = (l * (n - 1) + (d < 0 ? -d : 0)) / n; o[i] = rs(); }
            return o;
        },
        macd(c, f, s, g) {
            const ef = CI.ema(c, f), es = CI.ema(c, s);
            const m = c.map((_, i) => ef[i] != null && es[i] != null ? ef[i] - es[i] : null);
            const sig = CI.ema(m, g);
            return { macd: m, signal: sig, hist: m.map((v, i) => v != null && sig[i] != null ? v - sig[i] : null) };
        },
        stoch(h, l, c, n, d) {
            const k = c.map((_, i) => {
                if (i < n - 1) return null;
                const hh = Math.max(...h.slice(i - n + 1, i + 1)), ll = Math.min(...l.slice(i - n + 1, i + 1));
                return hh === ll ? null : (c[i] - ll) / (hh - ll) * 100;
            });
            return { k, d: CI.sma(k, d) };
        },
        atr(h, l, c, n) {                             // Wilder
            const o = c.map(() => null);
            if (c.length < n) return o;
            const tr = c.map((_, i) => i ? Math.max(h[i] - l[i], Math.abs(h[i] - c[i - 1]), Math.abs(l[i] - c[i - 1])) : h[i] - l[i]);
            let a = 0;
            for (let i = 0; i < n; i++) a += tr[i];
            a /= n; o[n - 1] = a;
            for (let i = n; i < c.length; i++) { a = (a * (n - 1) + tr[i]) / n; o[i] = a; }
            return o;
        },
        obv(c, v) { let s = 0; return c.map((x, i) => { if (i) s += x > c[i - 1] ? v[i] : x < c[i - 1] ? -v[i] : 0; return s; }); },
        vwap(h, l, c, v, day) {                       // anchored to each session; day[i] is the session key of bar i
            let pv = 0, vv = 0, d = null;
            return c.map((_, i) => { if (day[i] !== d) { d = day[i]; pv = vv = 0; } pv += (h[i] + l[i] + c[i]) / 3 * v[i]; vv += v[i]; return vv ? pv / vv : null; });
        },
        // Supply/demand: an impulse candle (range >= k x ATR14, body >= 60%) after 1-3 small-body candles (the base).
        // Keeps the 6 strongest fresh zones plus the 3 most recently mitigated ones (a close through the zone).
        zones(B, o) {
            const k = (o && o.k) || 1.5, n = B.c.length, a = CI.atr(B.h, B.l, B.c, 14), out = [];
            for (let i = 15; i < n; i++) {
                const rg = B.h[i] - B.l[i];
                if (!(rg > 0) || a[i - 1] == null || rg < k * a[i - 1] || Math.abs(B.c[i] - B.o[i]) < 0.6 * rg) continue;
                const up = B.c[i] > B.o[i];
                let j = i - 1, cnt = 0;
                while (cnt < 3 && j >= 0) { const r = B.h[j] - B.l[j]; if (!(r > 0) || Math.abs(B.c[j] - B.o[j]) > 0.5 * r) break; cnt++; j--; }
                if (!cnt) continue;
                let lo = Infinity, hi = -Infinity;
                for (let m = j + 1; m < i; m++) { lo = Math.min(lo, B.l[m]); hi = Math.max(hi, B.h[m]); }
                let end = null, touch = 0;
                for (let m = i + 1; m < n; m++) { if (up ? B.c[m] < lo : B.c[m] > hi) { end = m; break; } if (B.l[m] <= hi && B.h[m] >= lo) touch++; }
                out.push({ type: up ? 'demand' : 'supply', lo, hi, i0: j + 1, i1: end, touch, str: rg / a[i - 1] });
            }
            const fresh = out.filter(z => z.i1 == null).sort((x, y) => y.str - x.str).slice(0, 6);
            return fresh.concat(out.filter(z => z.i1 != null).sort((x, y) => y.i1 - x.i1).slice(0, 3));
        },
    };
    if (typeof module !== 'undefined') module.exports = CI;
    if (typeof window === 'undefined') return;

    // ================================================================ UI
    const LS = 'mt.chart.v2', DLS = 'mt.chart.draw.v1';
    const TFS = [['1m', '1m'], ['5m', '5m'], ['15m', '15m'], ['30m', '30m'], ['1h', '1h'], ['4h', '4h'], ['1d', '1D'], ['1wk', '1W'], ['1mo', '1M']];
    const RANGES = [['1D', 1], ['5D', 5], ['1M', 30], ['3M', 91], ['6M', 182], ['YTD', 'ytd'], ['1Y', 365], ['5Y', 1826], ['MAX', null]];
    const DEFAULT_RANGE = { '1m': '1D', '5m': '5D', '15m': '5D', '30m': '1M', '1h': '3M', '4h': '6M', '1d': '1Y', '1wk': '5Y', '1mo': 'MAX' };
    const LAYOUTS = { '1': [1, 1], '2': [2, 1], '2x2': [2, 2], '3x2': [3, 2] };
    const COLORS = ['#f59e0b', '#22d3ee', '#d946ef', '#3ddc84', '#f472b6', '#a78bfa'];
    const MODELS = { kronos: ['Kronos', '217,70,239'], baseline: ['Baseline', '34,211,238'] };
    const DAY = 86400;
    const SYM_RE = /^[A-Z0-9.^=\-]{1,15}$/, FID = /^f\.[0-9a-f]{8}$/;     // a symbol, or a saved formula's id
    const isSym = s => SYM_RE.test(s) || FID.test(s);
    const normSym = s => { s = String(s || '').trim(); return FID.test(s.toLowerCase()) ? s.toLowerCase() : s.toUpperCase(); };
    const VALS = [['price', 'PRICE'], ['mcap', 'MCAP'], ['volume', 'VOL']];
    const SRC = ['close', 'hl2'];
    // indicator catalogue: pane 0 = drawn on the price pane, 1 = its own pane. params: [key, default] (number, a source name or a colour)
    const IND = {
        vol: { name: 'Volume', pane: 0, params: [] },
        sma: { name: 'SMA', pane: 0, params: [['len', 20], ['src', 'close'], ['color', '#f59e0b']] },
        ema: { name: 'EMA', pane: 0, params: [['len', 20], ['src', 'close'], ['color', '#22d3ee']] },
        wma: { name: 'WMA', pane: 0, params: [['len', 20], ['src', 'close'], ['color', '#d946ef']] },
        vwap: { name: 'VWAP (intraday)', pane: 0, params: [['color', '#3ddc84']] },
        bb: { name: 'Bollinger Bands', pane: 0, params: [['len', 20], ['mult', 2], ['src', 'close'], ['color', '#a78bfa']] },
        rsi: { name: 'RSI', pane: 1, params: [['len', 14], ['src', 'close'], ['color', '#f59e0b']] },
        macd: { name: 'MACD', pane: 1, params: [['fast', 12], ['slow', 26], ['sig', 9], ['color', '#22d3ee']] },
        stoch: { name: 'Stochastic', pane: 1, params: [['k', 14], ['d', 3], ['color', '#f472b6']] },
        atr: { name: 'ATR', pane: 1, params: [['len', 14], ['color', '#3ddc84']] },
        obv: { name: 'OBV', pane: 1, params: [['color', '#a78bfa']] },
    };

    const newCell = o => Object.assign({ symbol: 'SPY', tf: '1d', range: '1Y', series: [], compare: [], ind: [{ id: 'vol', type: 'vol', p: {} }], fan: {}, val: 'price', cval: {}, pct: false, zones: 'off', k: 1.5, levels: false, events: false, blocks: false }, o || {});
    let st = { layout: '1', active: 0, sync: false, cells: [] };
    try { Object.assign(st, JSON.parse(localStorage.getItem(LS) || '{}')); } catch (e) { /* no storage: defaults */ }
    if (!LAYOUTS[st.layout]) st.layout = '1';
    if (!Array.isArray(st.cells)) st.cells = [];
    if (!Array.isArray(st.hiddenF)) st.hiddenF = [];
    while (st.cells.length < 6) st.cells.push(null);
    st.cells = st.cells.slice(0, 6).map(c => {
        c = newCell(c && typeof c === 'object' ? c : null);
        if (!TFS.some(x => x[0] === c.tf)) c.tf = '1d';
        if (!RANGES.some(x => x[0] === c.range)) c.range = DEFAULT_RANGE[c.tf];
        if (!isSym(String(c.symbol))) c.symbol = 'SPY';
        if (!VALS.some(x => x[0] === c.val)) c.val = 'price';
        if (!c.cval || typeof c.cval !== 'object') c.cval = {};
        c.series = Array.isArray(c.series) ? c.series.filter(s => s && typeof s.id === 'string') : [];
        c.compare = Array.isArray(c.compare) ? c.compare.filter(s => isSym(String(s))) : [];
        c.ind = Array.isArray(c.ind) ? c.ind.filter(i => i && IND[i.type]).map(i => ({ id: String(i.id || i.type), type: i.type, p: i.p && typeof i.p === 'object' ? i.p : {} })) : [];
        if (!c.fan || typeof c.fan !== 'object') c.fan = {};
        if (!['off', 'classic', 'ours', 'both'].includes(c.zones)) c.zones = 'off';
        if (!(c.k > 0)) c.k = 1.5;
        return c;
    });
    if (!(st.active >= 0 && st.active < LAYOUTS[st.layout][0] * LAYOUTS[st.layout][1])) st.active = 0;
    const save = () => { try { localStorage.setItem(LS, JSON.stringify(st)); } catch (e) { /* ignore */ } };
    let drawings = {};
    try { drawings = JSON.parse(localStorage.getItem(DLS) || '{}') || {}; } catch (e) { drawings = {}; }
    const saveDraw = () => { try { localStorage.setItem(DLS, JSON.stringify(drawings)); } catch (e) { /* ignore */ } };

    const $ = id => document.getElementById(id);
    const h = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
    const isIntra = tf => /^\d+[mh]$/.test(tf);
    const tzOf = tf => isIntra(tf) ? 'America/New_York' : 'UTC';
    const fmtNum = v => v == null ? '-' : Number(v).toLocaleString('en-US', { maximumFractionDigits: Math.abs(v) < 10 ? 4 : 2 });
    const fmtDate = (t, tf) => new Date(t * 1000).toLocaleDateString('en-CA', { timeZone: tzOf(tf) });
    const isoT = s => typeof s === 'number' ? s : Date.parse(/^\d{4}-\d\d-\d\d$/.test(s) || /(Z|[+-]\d\d:?\d\d)$/.test(s) ? s : s + 'Z') / 1000;
    const status = (msg, bad) => { const s = $('chStatus'); s.textContent = msg || ''; s.className = 'ch-status' + (bad ? ' bad' : ''); };
    const lastOf = x => Array.isArray(x) ? x[x.length - 1] : x;
    const elapsed = s => { s = Math.abs(s); return s >= DAY ? (s / DAY).toFixed(1) + ' d' : s >= 3600 ? (s / 3600).toFixed(1) + ' h' : Math.round(s / 60) + ' min'; };

    const jcache = new Map();                                            // url -> {t, r}: short-lived, successes only
    async function getJ(url, ttl = 60000) {
        const c = jcache.get(url);
        if (c && Date.now() - c.t < ttl) return c.r;
        const r = await chartJson(API_BASE + url);
        if (r.status === 'success') jcache.set(url, { t: Date.now(), r });
        return r;
    }
    const symInfo = {};                                                  // symbol -> {name, kind, description}
    let catalog = null, started = false, LW = null;
    let cells = [];                                                      // runtime cells (chart objects), parallel to st.cells
    let tool = null, pendingPt = null;                                   // drawing tool: 'h' | 't'
    const ex = { item: null, model: 'both', all: false, idx: 0, timer: null };   // scenario explorer
    const A = () => cells[st.active], P = cell => st.cells[cell.i];
    const globalFans = () => { const cb = $('fansToggle'); return !!(cb && cb.checked); };
    const fanOn = (c, key) => c.fan[key] != null ? !!c.fan[key] : globalFans();

    // ---- one primitive draws on a series' pane: zones, fans, trend lines. draw(ctx, size, timeScale, series) in CSS pixels.
    function prim(draw, z) {
        let req, ser, chrt;
        const view = { zOrder: () => z || 'top', renderer: () => ({ draw: t => t.useMediaCoordinateSpace(s => { try { draw(s.context, s.mediaSize, chrt.timeScale(), ser); } catch (e) { /* off-screen */ } }) }) };
        return { attached: p => { chrt = p.chart; ser = p.series; req = p.requestUpdate; }, detached() { }, updateAllViews() { }, paneViews: () => [view], redraw: () => req && req() };
    }

    // ---- chart per cell
    function chartOptions(tf) {
        const o = { timeZone: tzOf(tf) }, intra = isIntra(tf);
        return {
            localization: { timeFormatter: t => new Date(t * 1000).toLocaleString('en-US', { ...o, month: 'short', day: 'numeric', year: '2-digit', hour: intra ? '2-digit' : undefined, minute: intra ? '2-digit' : undefined, hour12: false }) },
            timeScale: {
                timeVisible: intra,
                tickMarkFormatter: (t, type) => {
                    const d = new Date(t * 1000);
                    if (type === 0) return d.toLocaleDateString('en-US', { ...o, year: 'numeric' });
                    if (type === 1) return d.toLocaleDateString('en-US', { ...o, month: 'short' });
                    if (type === 2) return d.toLocaleDateString('en-US', { ...o, month: 'short', day: 'numeric' });
                    return d.toLocaleTimeString('en-US', { ...o, hour: '2-digit', minute: '2-digit', hour12: false });
                },
            },
        };
    }
    let syncing = false;
    function mkCell(i) {
        const el = h('div', 'ch-cell'), box = h('div', 'ch-cell-box'), meas = h('div', 'ch-meas hidden');
        el.append(box, meas);
        const chart = LW.createChart(box, {
            autoSize: true,
            layout: { background: { color: '#000000' }, textColor: '#a1a1aa', panes: { separatorColor: '#27272a' } },
            grid: { vertLines: { color: '#18181b' }, horzLines: { color: '#18181b' } },
            rightPriceScale: { borderColor: '#27272a' }, leftPriceScale: { borderColor: '#27272a', visible: false },
            timeScale: { borderColor: '#27272a', rightOffset: 4 }, crosshair: { mode: 0 },
            // a vertical swipe scrolls the page (phones and iPads); a sideways drag pans, two fingers zoom
            handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
        });
        const candles = chart.addSeries(LW.CandlestickSeries, { upColor: '#22c55e', downColor: '#ef4444', borderVisible: false, wickUpColor: '#22c55e', wickDownColor: '#ef4444' });
        const cell = { i, el, box, meas, chart, candles, B: null, last: null, seq: 0, retryT: null, retryStart: 0, extras: [], prims: [], cprims: [], pl: [], markers: null, lines: {}, cmp: {}, fans: {}, ov: null, ovSym: null };
        el.addEventListener('mousedown', () => { if (st.active !== i) setActive(i); }, true);
        chart.subscribeClick(p => onClick(cell, p));
        chart.subscribeCrosshairMove(p => {
            if (!st.sync || syncing) return;
            syncing = true;
            cells.forEach(o => {
                if (o === cell) return;
                try {
                    if (!p.time || !o.B) { o.chart.clearCrosshairPosition(); return; }
                    const j = o.B.t.findIndex(t => t >= p.time);
                    if (j >= 0) o.chart.setCrosshairPosition(o.B.c[j], o.B.t[j], o.candles);
                } catch (e) { /* different timeframe: no matching bar */ }
            });
            syncing = false;
        });
        chart.timeScale().subscribeVisibleTimeRangeChange(r => {
            if (!st.sync || syncing || !r) return;
            syncing = true;
            cells.forEach(o => { if (o !== cell) try { o.chart.timeScale().setVisibleRange(r); } catch (e) { /* outside its data */ } });
            syncing = false;
        });
        chart.applyOptions(chartOptions(P(cell).tf));
        try { chart.panes()[0].setStretchFactor(3); } catch (e) { /* older build */ }
        measure(cell);
        return cell;
    }
    function buildGrid() {
        cells.forEach(c => { clearTimeout(c.retryT); try { c.chart.remove(); } catch (e) { /* gone */ } });
        const [cols, rows] = LAYOUTS[st.layout], grid = $('chGrid');
        grid.style.setProperty('--cols', cols);
        grid.style.setProperty('--rows', rows);
        grid.replaceChildren();
        cells = [];
        for (let i = 0; i < cols * rows; i++) { const c = mkCell(i); cells.push(c); grid.append(c.el); loadBars(c); }
        if (st.active >= cells.length) st.active = 0;
        setActive(st.active);
    }
    function setActive(i) {
        st.active = i; save();
        cells.forEach(c => c.el.classList.toggle('active', c.i === i));
        syncUI(); renderInfo(); renderExplore(); renderDraws();
    }

    // ---- range: the visible window is anchored on the newest bar, so a weekend or holiday still shows data
    const rangeDays = c => RANGES.find(r => r[0] === c.range)[1];
    function fetchFrom(c) {
        const d = rangeDays(c), now = new Date();
        if (d === null) return '';
        const start = d === 'ytd' ? new Date(Date.UTC(now.getUTCFullYear(), 0, 1)) : new Date(now.getTime() - d * DAY * 1000);
        return new Date(start.getTime() - 5 * DAY * 1000).toISOString().slice(0, 10);
    }
    function applyRange(cell) {
        const B = cell.B;
        if (!B || !B.t.length) return;
        const d = rangeDays(P(cell)), end = B.t[B.t.length - 1], ts = cell.chart.timeScale();
        if (d === null) { ts.fitContent(); return; }
        const from = d === 'ytd' ? Date.UTC(new Date(end * 1000).getUTCFullYear(), 0, 1) / 1000 : end - d * DAY;
        try { ts.setVisibleRange({ from: Math.max(from, B.t[0]), to: cell.fanEnd || end }); } catch (e) { ts.fitContent(); }
    }
    function toBars(r) {
        if (r.value) r = { t: r.t, o: r.v, h: r.v, l: r.v, c: r.v, v: r.t.map(() => 0) };      // mcap / volume: one line
        const B = { t: [], o: [], h: [], l: [], c: [], v: [] };
        r.t.forEach((t, i) => {
            if (r.o[i] == null || r.h[i] == null || r.l[i] == null || r.c[i] == null) return;
            B.t.push(t); B.o.push(r.o[i]); B.h.push(r.h[i]); B.l.push(r.l[i]); B.c.push(r.c[i]); B.v.push(r.v[i] || 0);
        });
        return B;
    }
    async function loadBars(cell) {
        const c = P(cell), my = ++cell.seq;
        clearTimeout(cell.retryT);
        const from = fetchFrom(c);
        const r = await chartJson(`${API_BASE}/api/chart/bars?symbol=${encodeURIComponent(c.symbol)}&tf=${c.tf}&value=${c.val}` + (from ? `&from=${from}` : ''));
        if (my !== cell.seq) return;
        const say = (m, bad) => { if (cell.i === st.active) status(m, bad); };
        if (r.status === 'loading') {
            cell.retryStart = cell.retryStart || Date.now();
            if (Date.now() - cell.retryStart > 120000) { cell.retryStart = 0; say(`${c.symbol}: still no history after 2 minutes. Try again later.`, true); return; }
            say(`Loading ${c.symbol} history…`);
            cell.retryT = setTimeout(() => loadBars(cell), 3000);
            return;
        }
        cell.retryStart = 0;
        if (r.status === 'missing' && c.val !== 'price') { c.val = 'price'; cell.note = r.reason; save(); renderInfo(); loadBars(cell); return; }   // e.g. no market cap for an ETF
        if (r.status !== 'success') { say(r.message || r.reason || 'chart data unavailable', true); return; }
        say(cell.note || '', !!cell.note); cell.note = '';
        cell.last = r; cell.B = toBars(r);
        cell.chart.applyOptions(chartOptions(c.tf));
        if (!cell.B.t.length) say(`No ${c.tf} bars stored for ${c.symbol} yet.`);
        render(cell);
        applyRange(cell);
        if (!symInfo[c.symbol]) { catalog = null; await loadCatalog(); }
        if (cell.i === st.active) renderInfo();
    }

    // ---- extra data: series lines, compare symbols, fans, overlays (each cached; a late answer re-renders the cell)
    async function ensureLine(cell, id) {
        if (cell.lines[id]) return;
        cell.lines[id] = { pending: true };
        const r = await getJ(`/api/chart/series?id=${encodeURIComponent(id)}`, 300000);
        if (r.status !== 'success') { delete cell.lines[id]; status(r.message || `${id} unavailable`, true); return; }
        cell.lines[id] = { meta: r };
        render(cell);
    }
    async function ensureCmp(cell, sym, n = 0) {
        if (cell.cmp[sym] && !cell.cmp[sym].retry) return;
        cell.cmp[sym] = { retry: true };
        const c = P(cell), from = fetchFrom(c);
        const r = await chartJson(`${API_BASE}/api/chart/bars?symbol=${encodeURIComponent(sym)}&tf=${c.tf}&value=${c.cval[sym] || 'price'}` + (from ? `&from=${from}` : ''));
        if (!P(cell).compare.includes(sym)) { delete cell.cmp[sym]; return; }
        if (r.status === 'loading' && n < 40) { status(`Loading ${sym} history…`); setTimeout(() => ensureCmp(cell, sym, n + 1), 3000); return; }
        if (r.status !== 'success') { cell.cmp[sym] = { err: r.message || r.reason || 'unavailable' }; status(`${sym}: ${cell.cmp[sym].err}`, true); render(cell); return; }
        status('');
        cell.cmp[sym] = { B: toBars(r) };
        render(cell);
    }
    const fanTf = tf => tf === '1h' || tf === '5m' ? tf : '1d';
    async function ensureFan(cell, item) {
        const key = item + '|' + fanTf(P(cell).tf);
        if (cell.fans[key]) return;
        cell.fans[key] = { pending: true };
        const r = await chartJson(`${API_BASE}/api/chart/fan?item=${encodeURIComponent(item)}&tf=${fanTf(P(cell).tf)}`);
        cell.fans[key] = r.status === 'success' && r.models ? { d: r } : { err: r.reason || r.message || `no fan (${r.status})` };
        if (cells.includes(cell)) { render(cell); if (cell.i === st.active) renderExplore(); }
    }
    async function ensureOv(cell) {
        const sym = P(cell).symbol;
        if (cell.ovSym === sym) return;
        cell.ovSym = sym; cell.ov = { pending: true };
        const r = await getJ(`/api/chart/overlays?symbol=${encodeURIComponent(sym)}`, 120000);
        if (P(cell).symbol !== sym) return;
        cell.ov = r.status === 'success' ? r : { err: r.reason || r.message || 'overlays unavailable' };
        if (cell.ov.err && cell.i === st.active) status(`Overlays: ${cell.ov.err}`, true);
        render(cell);
    }

    // ---- render: rebuild everything except the candles from state (cheap; keeps the code free of diffing)
    function render(cell) {
        if (!cell.B) return;
        const c = P(cell), ch = cell.chart, B = cell.B, add = (type, o, pane) => { const s = ch.addSeries(LW[type], o, pane); cell.extras.push(s); return s; };
        cell.extras.forEach(s => { try { ch.removeSeries(s); } catch (e) { /* already gone */ } });
        cell.extras = [];
        cell.cprims.forEach(p => { try { cell.candles.detachPrimitive(p); } catch (e) { /* ignore */ } });
        cell.cprims = []; cell.prims = [];
        cell.pl.forEach(l => { try { cell.candles.removePriceLine(l); } catch (e) { /* ignore */ } });
        cell.pl = [];
        const key = (arr, t) => { const o = []; arr.forEach((v, i) => { if (nn(v)) o.push({ time: t[i], value: v }); }); return o; };
        const ser = (arr) => key(arr, B.t);
        const srcOf = p => p.src === 'hl2' ? B.h.map((x, i) => (x + B.l[i]) / 2) : B.c;
        const num = (p, k, d) => { const v = Number(p[k]); return v >= 1 ? Math.floor(v) : d; };
        const line = (data, color, pane, extra) => { const s = add('LineSeries', { color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, ...extra }, pane); s.setData(data); return s; };
        const mv = c.val !== 'price';                                          // main item drawn as market cap or volume: a line, not candles
        const hasVol = !mv && c.ind.some(i => i.type === 'vol');
        cell.candles.applyOptions({ visible: !mv, priceFormat: FID.test(c.symbol) ? { type: 'price', precision: 4, minMove: 0.0001 } : { type: 'price', precision: 2, minMove: 0.01 } });

        // fans: the items the fan toggles refer to
        const items = mv ? [] : [{ key: 'sym:' + c.symbol, item: c.symbol, label: c.symbol, api: cell.candles, anchor: B.t[B.t.length - 1] }];
        const futT = new Set();

        // candles + volume
        ch.priceScale('right').applyOptions({ mode: c.pct ? 2 : 0, scaleMargins: { top: 0.05, bottom: hasVol ? 0.25 : 0.05 } });
        let left = false, np = 1;

        c.ind.forEach(ind => {
            const p = ind.p, col = p.color || '#f59e0b';
            if (ind.type === 'vol') {
                if (mv) return;
                const s = add('HistogramSeries', { priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false });
                s.setData(B.t.map((t, i) => B.v[i] ? { time: t, value: B.v[i], color: B.c[i] >= B.o[i] ? 'rgba(34,197,94,.35)' : 'rgba(239,68,68,.35)' } : null).filter(Boolean));
                ch.priceScale('vol').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
            } else if (ind.type === 'sma' || ind.type === 'ema' || ind.type === 'wma') {
                const n = num(p, 'len', 20);
                line(ser(CI[ind.type](srcOf(p), n)), col, 0, { lineWidth: 2, title: `${IND[ind.type].name} ${n}` });
            } else if (ind.type === 'vwap') {
                if (!isIntra(c.tf) || mv) return;
                const day = B.t.map(t => new Date(t * 1000).toLocaleDateString('en-CA', { timeZone: 'America/New_York' }));
                line(ser(CI.vwap(B.h, B.l, B.c, B.v, day)), col, 0, { lineWidth: 2, title: 'VWAP' });
            } else if (ind.type === 'bb') {
                const n = num(p, 'len', 20), b = CI.bb(srcOf(p), n, Number(p.mult) > 0 ? Number(p.mult) : 2);
                line(ser(b.up), col, 0, { title: `BB ${n}` }); line(ser(b.lo), col, 0); line(ser(b.mid), col, 0, { lineStyle: 2 });
            } else if (ind.type === 'rsi') {
                const n = num(p, 'len', 14), pn = np++, s = line(ser(CI.rsi(srcOf(p), n)), col, pn, { lineWidth: 2, title: `RSI ${n}`, lastValueVisible: true });
                [30, 70].forEach(v => s.createPriceLine({ price: v, color: '#52525b', lineWidth: 1, lineStyle: 2, axisLabelVisible: false }));
            } else if (ind.type === 'macd') {
                const m = CI.macd(B.c, num(p, 'fast', 12), num(p, 'slow', 26), num(p, 'sig', 9)), pn = np++;
                const hs = add('HistogramSeries', { priceLineVisible: false, lastValueVisible: false }, pn);
                hs.setData(B.t.map((t, i) => m.hist[i] == null ? null : { time: t, value: m.hist[i], color: m.hist[i] >= 0 ? 'rgba(34,197,94,.55)' : 'rgba(239,68,68,.55)' }).filter(Boolean));
                line(ser(m.macd), col, pn, { lineWidth: 2, title: 'MACD' }); line(ser(m.signal), '#f59e0b', pn);
            } else if (ind.type === 'stoch') {
                const s = CI.stoch(B.h, B.l, B.c, num(p, 'k', 14), num(p, 'd', 3)), pn = np++;
                const k = line(ser(s.k), col, pn, { lineWidth: 2, title: 'Stoch %K' }); line(ser(s.d), '#f59e0b', pn, { title: '%D' });
                [20, 80].forEach(v => k.createPriceLine({ price: v, color: '#52525b', lineWidth: 1, lineStyle: 2, axisLabelVisible: false }));
            } else if (ind.type === 'atr') {
                const n = num(p, 'len', 14);
                line(ser(CI.atr(B.h, B.l, B.c, n)), col, np++, { lineWidth: 2, title: `ATR ${n}`, lastValueVisible: true });
            } else if (ind.type === 'obv') {
                line(ser(CI.obv(B.c, B.v)), col, np++, { lineWidth: 2, title: 'OBV', lastValueVisible: true });
            }
        });

        if (mv) {
            const unit = cell.last && cell.last.unit, title = `${c.symbol} ${c.val === 'mcap' ? 'market cap' : 'volume'}${c.val === 'mcap' && unit ? ' (' + unit + ')' : ''}`;
            if (c.val === 'volume') add('HistogramSeries', { color: 'rgba(34,211,238,.7)', priceFormat: { type: 'volume' }, priceLineVisible: false, title }).setData(B.t.map((t, i) => ({ time: t, value: B.c[i] })));
            else line(ser(B.c), '#22c55e', 0, { lineWidth: 2, lastValueVisible: true, title });
        }
        // compare symbols: own left scale, or on the right scale in % mode
        c.compare.forEach((sym, k) => {
            const d = cell.cmp[sym];
            if (!d) { ensureCmp(cell, sym); return; }
            if (!d.B) return;
            const color = COLORS[(k + 1) % COLORS.length];
            if (!c.pct) left = true;
            // the first on the left axis, every further one on its own (unlabelled) scale, so a ratio near 80 and one near 0.5 both read
            const s = add('LineSeries', { color, lineWidth: 2, priceScaleId: c.pct ? 'right' : k === 0 ? 'left' : 'cmp' + k, title: (symInfo[sym] && symInfo[sym].kind === 'FORMULA' ? symInfo[sym].name : sym) + (c.cval[sym] && c.cval[sym] !== 'price' ? ' ' + c.cval[sym] : '') }, 0);
            s.setData(d.B.t.map((t, i) => ({ time: t, value: d.B.c[i] })));
            if (!c.cval[sym] || c.cval[sym] === 'price') items.push({ key: 'sym:' + sym, item: sym, label: sym, api: s, anchor: d.B.t[d.B.t.length - 1], color });
        });
        // added series: left scale or its own lower pane
        c.series.forEach((sd, k) => {
            const l = cell.lines[sd.id];
            if (!l) { ensureLine(cell, sd.id); return; }
            if (!l.meta) return;
            const color = COLORS[(k + 3) % COLORS.length], m = l.meta;
            if (sd.where === 'left') left = true;
            const s = add('LineSeries', { color, lineWidth: 2, priceScaleId: sd.where === 'left' ? 'left' : 'right', title: m.label }, sd.where === 'left' ? 0 : np++);
            s.setData(m.t.map((t, i) => ({ time: t, value: m.v[i] })).filter(x => nn(x.value)));
            items.push({ key: 'ser:' + sd.id, item: sd.id, label: m.label, api: s, anchor: m.t[m.t.length - 1], color });
        });
        ch.priceScale('left').applyOptions({ visible: left });
        cell.items = items;

        // fans
        const ftf = fanTf(c.tf);
        items.forEach(it => {
            if (!fanOn(c, it.key)) return;
            const f = cell.fans[it.item + '|' + ftf];
            if (!f) { ensureFan(cell, it.item); return; }
            if (!f.d) return;
            const d = f.d, tt = [it.anchor].concat((d.dates || []).map(isoT));
            tt.slice(1).forEach(t => { if (t > B.t[B.t.length - 1]) futT.add(t); });
            const p = prim((ctx, size, ts, sr) => drawFan(ctx, ts, sr, it, d, tt), 'normal');
            it.api.attachPrimitive(p); cell.prims.push(p);
            if (it.api === cell.candles) cell.cprims.push(p);
        });

        // candle data, with empty future points so the fans have somewhere to go
        const bars = B.t.map((t, i) => ({ time: t, open: B.o[i], high: B.h[i], low: B.l[i], close: B.c[i] }));
        cell.candles.setData(bars.concat([...futT].sort((a, b) => a - b).map(time => ({ time }))));

        const fe = futT.size ? Math.max(...futT) : null;
        if (fe !== cell.fanEnd) { cell.fanEnd = fe; applyRange(cell); }
        // zones
        const zm = mv ? 'off' : c.zones;
        if (zm !== 'off') {
            const zs = [];
            const lastT = B.t[B.t.length - 1];
            if (zm === 'classic' || zm === 'both') CI.zones(B, { k: c.k }).forEach(z => zs.push({ ...z, t0: B.t[z.i0], t1: z.i1 == null ? lastT : B.t[z.i1], label: `${z.type === 'demand' ? 'D' : 'S'} ${z.str.toFixed(1)}x · ${z.touch} touch${z.touch === 1 ? '' : 'es'}`, faded: z.i1 != null, rgb: z.type === 'demand' ? '34,197,94' : '239,68,68' }));
            if ((zm === 'ours' || zm === 'both') && cell.ov && cell.ov.zones) cell.ov.zones.forEach(z => zs.push({ lo: z.lo, hi: z.hi, label: z.label, t0: null, t1: null, rgb: /put|demand|support|buy/i.test(z.kind || z.label || '') ? '34,197,94' : /call|supply|resist|sell/i.test(z.kind || z.label || '') ? '239,68,68' : '245,158,11' }));
            if (zm !== 'classic') ensureOv(cell);
            const p = prim((ctx, size, ts, sr) => {
                zs.forEach(z => {
                    const y1 = sr.priceToCoordinate(z.hi), y2 = sr.priceToCoordinate(z.lo);
                    if (y1 == null || y2 == null) return;
                    let x0 = z.t0 == null ? 0 : ts.timeToCoordinate(z.t0), x1 = z.t1 == null ? size.width : ts.timeToCoordinate(z.t1);
                    if (x0 == null) x0 = 0;
                    if (x1 == null) x1 = size.width;
                    ctx.fillStyle = `rgba(${z.rgb},${z.faded ? 0.06 : 0.2})`;
                    ctx.fillRect(x0, y1, x1 - x0, y2 - y1);
                    ctx.strokeStyle = `rgba(${z.rgb},${z.faded ? 0.25 : 0.7})`; ctx.setLineDash(z.faded ? [3, 3] : []);
                    ctx.strokeRect(x0, y1, x1 - x0, y2 - y1); ctx.setLineDash([]);
                    ctx.fillStyle = `rgba(${z.rgb},${z.faded ? 0.5 : 1})`; ctx.font = '10px sans-serif';
                    ctx.fillText(z.label || '', Math.max(x0, 0) + 4, y1 + 11);
                });
            }, 'bottom');
            cell.candles.attachPrimitive(p); cell.cprims.push(p);
        }
        // levels, events, blocks from /api/chart/overlays
        const wantOv = !mv && (c.levels || c.events || c.blocks || zm === 'ours' || zm === 'both');
        if (wantOv) ensureOv(cell);
        const ov = wantOv && cell.ov && !cell.ov.pending && !cell.ov.err ? cell.ov : null;
        if (ov && (c.levels || zm === 'ours' || zm === 'both')) (ov.levels || []).forEach(l => cell.pl.push(cell.candles.createPriceLine({ price: l.price, color: '#f59e0b', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: l.label || l.kind || '' })));
        const mk = [];
        const snap = t => { t = isoT(t); let lo = 0, hi = B.t.length - 1, r = null; while (lo <= hi) { const m = (lo + hi) >> 1; if (B.t[m] <= t) { r = B.t[m]; lo = m + 1; } else hi = m - 1; } return r; };
        if (ov && c.events) (ov.markers || []).forEach(m => { const t = snap(m.t); if (t != null) mk.push({ time: t, position: 'aboveBar', shape: 'arrowDown', color: '#f59e0b', text: m.label || '' }); });
        if (ov && c.blocks) (ov.blocks || []).forEach(b => { const t = snap(b.t); if (t != null) mk.push({ time: t, position: 'belowBar', shape: 'circle', color: '#22d3ee', size: b.notional > 5e7 ? 3 : b.notional > 1e7 ? 2 : 1, text: b.notional ? '$' + (b.notional / 1e6).toFixed(1) + 'M' : '' }); });
        mk.sort((a, b) => a.time - b.time);
        if (!cell.markers) cell.markers = LW.createSeriesMarkers(cell.candles, mk); else cell.markers.setMarkers(mk);
        // trend lines and horizontal lines
        const dr = mv ? [] : drawings[c.symbol] || [];
        dr.forEach(d => { if (d.type === 'h') cell.pl.push(cell.candles.createPriceLine({ price: d.price, color: '#e4e4e7', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: '' })); });
        const trends = dr.filter(d => d.type === 't');
        if (trends.length) {
            const p = prim((ctx, size, ts, sr) => {
                ctx.strokeStyle = '#e4e4e7'; ctx.lineWidth = 1.5;
                trends.forEach(d => {
                    const x1 = ts.timeToCoordinate(d.t1), x2 = ts.timeToCoordinate(d.t2), y1 = sr.priceToCoordinate(d.p1), y2 = sr.priceToCoordinate(d.p2);
                    if (x1 == null || x2 == null || y1 == null || y2 == null) return;
                    ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
                });
            });
            cell.candles.attachPrimitive(p); cell.cprims.push(p);
        }
        if (cell.i === st.active) { renderInfo(); updateReadout(); }
    }

    // ---- scenario fans
    function modelOk(m) { return m && m.status !== 'missing' && (m.paths || m.q); }
    function pathVals(arr, d, last) { return arr.length === (d.dates || []).length ? [last].concat(arr) : arr; }
    function drawFan(ctx, ts, sr, it, d, tt) {
        const px = j => ts.timeToCoordinate(tt[j]);
        const pt = (vals, j) => { const x = px(j), y = vals[j] == null ? null : sr.priceToCoordinate(vals[j]); return x == null || y == null ? null : [x, y]; };
        const path = (vals, move) => { let open = false; vals.forEach((_, j) => { const p = pt(vals, j); if (!p) return; if (!open) { ctx.moveTo(p[0], p[1]); open = true; } else ctx.lineTo(p[0], p[1]); }); };
        Object.keys(MODELS).forEach(name => {
            if (ex.model !== 'both' && ex.model !== name) return;
            const m = d.models[name];
            if (!modelOk(m)) return;
            const rgb = MODELS[name][1], q = m.q || {}, V = k => Array.isArray(q[k]) ? pathVals(q[k], d, d.last) : null;
            const band = (lo, hi, a) => {
                if (!lo || !hi) return;
                ctx.beginPath(); path(hi); for (let j = hi.length - 1; j >= 0; j--) { const p = pt(lo, j); if (p) ctx.lineTo(p[0], p[1]); }
                ctx.closePath(); ctx.fillStyle = `rgba(${rgb},${a})`; ctx.fill();
            };
            band(V('p10'), V('p90'), 0.13); band(V('p25'), V('p75'), 0.22);
            if (it.key === ex.item && (m.paths || []).length) {
                const ps = m.paths;
                if (ex.all) { ctx.beginPath(); ps.forEach(p => path(pathVals(p, d, d.last))); ctx.strokeStyle = `rgba(${rgb},.12)`; ctx.lineWidth = 1; ctx.stroke(); }
                const hp = ps[ex.idx % ps.length];
                if (hp && (ex.all || ex.playing || ex.hl)) { ctx.beginPath(); path(pathVals(hp, d, d.last)); ctx.strokeStyle = `rgb(${rgb})`; ctx.lineWidth = 2; ctx.stroke(); }
            }
            const med = V('p50');
            if (med) { ctx.beginPath(); path(med); ctx.strokeStyle = `rgb(${rgb})`; ctx.lineWidth = 2; ctx.setLineDash([5, 3]); ctx.stroke(); ctx.setLineDash([]); }
        });
    }
    function fanData(cell, item) { const f = cell.fans[item + '|' + fanTf(P(cell).tf)]; return f && f.d; }
    function redrawFans() { const c = A(); if (c) c.prims.forEach(p => p.redraw()); updateReadout(); }
    function renderExplore() {
        const box = $('chExplore'), cell = A();
        box.replaceChildren();
        if (!cell || !cell.items) { box.classList.add('hidden'); return; }
        const c = P(cell), on = cell.items.filter(it => fanOn(c, it.key));
        box.classList.toggle('hidden', !on.length);
        if (!on.length) { clearInterval(ex.timer); ex.timer = null; ex.playing = false; return; }
        if (!on.some(it => it.key === ex.item)) ex.item = on[0].key;
        const ctl = h('div', 'ch-ex-row');
        const sel = (opts, val, fn) => { const s = h('select', 'ch-sel'); opts.forEach(([v, l]) => { const o = h('option', null, l); o.value = v; s.append(o); }); s.value = val; s.onchange = () => fn(s.value); return s; };
        ctl.append(h('b', null, 'SCENARIOS'),
            sel(on.map(it => [it.key, it.label]), ex.item, v => { ex.item = v; ex.idx = 0; renderExplore(); redrawFans(); }),
            sel([['both', 'Both'], ['kronos', 'Kronos'], ['baseline', 'Baseline']], ex.model, v => { ex.model = v; redrawFans(); }));
        const all = h('label', 'ch-chk'), cb = h('input'); cb.type = 'checkbox'; cb.checked = ex.all; cb.onchange = () => { ex.all = cb.checked; redrawFans(); };
        all.append(cb, document.createTextNode(' all paths')); ctl.append(all);
        const it = on.find(x => x.key === ex.item), d = fanData(cell, it.item);
        const N = d ? Math.max(0, ...Object.values(d.models).map(m => modelOk(m) && m.paths ? m.paths.length : 0)) : 0;
        if (N) {
            const rng = h('input', 'ch-range'); rng.type = 'range'; rng.min = 1; rng.max = N; rng.value = (ex.idx % N) + 1;
            const go = i => { ex.idx = ((i % N) + N) % N; ex.hl = true; rng.value = ex.idx + 1; redrawFans(); };
            rng.oninput = () => go(Number(rng.value) - 1);
            const bt = (t, fn, title) => { const b = h('button', 'ch-btn', t); b.title = title; b.onclick = fn; return b; };
            const play = bt(ex.playing ? '■' : '▶', () => {
                if (ex.playing) { clearInterval(ex.timer); ex.timer = null; ex.playing = false; } else { ex.playing = true; ex.timer = setInterval(() => go(ex.idx + 1), 250); }
                play.textContent = ex.playing ? '■' : '▶';
            }, 'Play: about 4 paths per second');
            ctl.append(bt('◀', () => go(ex.idx - 1), 'Previous path'), rng, bt('▶', () => go(ex.idx + 1), 'Next path'), play, h('span', 'ch-desc', `path`), h('span', 'ch-idx', ''));
            ctl.querySelector('.ch-idx').id = 'chExIdx';
        }
        box.append(ctl, h('div', 'ch-ex-read', '')); box.lastChild.id = 'chExRead';
        const notes = h('div', 'ch-ex-notes');
        on.forEach(x => {
            const f = cell.fans[x.item + '|' + fanTf(c.tf)];
            const row = h('div', 'ch-ex-note');
            row.append(h('b', null, x.label));
            if (!f || f.pending) row.append(h('span', 'ch-desc', ' loading fan…'));
            else if (f.err) row.append(h('span', 'ch-bad', ' ' + f.err));
            else {
                const fd = f.d;
                Object.keys(MODELS).forEach(name => {
                    const m = fd.models[name], sp = h('span', 'ch-leg');
                    sp.style.color = `rgb(${MODELS[name][1]})`;
                    sp.textContent = ' ■ ' + MODELS[name][0] + (m && m.status === 'missing' ? ' (missing: ' + (m.reason || 'no reason given') + ')' : !m ? ' (not returned)' : '');
                    row.append(sp);
                });
                const v = fd.verdict, vt = typeof v === 'string' ? v : v && (v.text || v.summary || v.label);
                if (vt) row.append(h('div', 'ch-desc', vt));
                if (fanTf(c.tf) !== c.tf) row.append(h('div', 'ch-desc', `Chart is ${c.tf}: fan drawn from the ${fanTf(c.tf)} model.`));
                if (fd.source || fd.asof) row.append(h('div', 'ch-desc', [fd.source, fd.asof && 'as of ' + fd.asof].filter(Boolean).join(' · ')));
            }
            notes.append(row);
        });
        box.append(notes);
        updateReadout();
    }
    function updateReadout() {
        const rd = $('chExRead'), cell = A();
        if (!rd || !cell || !cell.items) return;
        const it = cell.items.find(x => x.key === ex.item), d = it && fanData(cell, it.item);
        if (!d) { rd.textContent = ''; return; }
        const parts = [], pct = v => (v / d.last - 1) * 100;
        Object.keys(MODELS).forEach(name => {
            if (ex.model !== 'both' && ex.model !== name) return;
            const m = d.models[name];
            if (!modelOk(m)) return;
            const bits = [];
            if (m.paths && m.paths.length && ex.hl) { const p = m.paths[ex.idx % m.paths.length], v = lastOf(p); bits.push(`path ${ex.idx % m.paths.length + 1}/${m.paths.length}: ${fmtNum(v)} (${pct(v) >= 0 ? '+' : ''}${pct(v).toFixed(2)}%)`); }
            if (m.q) bits.push(['p10', 'p50', 'p90'].map(k => lastOf(m.q[k]) != null ? `${k} ${fmtNum(lastOf(m.q[k]))} (${pct(lastOf(m.q[k])).toFixed(1)}%)` : null).filter(Boolean).join(' · '));
            parts.push(`${MODELS[name][0]}: ${bits.join(' | ')}`);
        });
        rd.textContent = `vs last ${fmtNum(d.last)} — ` + (parts.join('   ') || 'no model has a forecast');
        const ix = $('chExIdx'); if (ix) ix.textContent = String(ex.idx + 1);
    }

    // ---- info strip and tracked list
    function renderInfo() {
        const box = $('chInfo'), cell = A();
        box.replaceChildren();
        if (!cell) return;
        const c = P(cell), info = symInfo[c.symbol] || {};
        const row = (title, desc, val, key, onX, color, mode) => {
            const d = h('div', 'ch-info-row');
            if (color) d.style.borderLeftColor = color;
            d.append(h('b', null, title), h('span', 'ch-desc', desc || ''), h('span', 'ch-val', val));
            if (mode) {                                                       // PRICE | MCAP | VOL
                const s = h('select', 'ch-sel');
                VALS.forEach(([v, l]) => { const o = h('option', null, l); o.value = v; s.append(o); });
                s.value = mode.cur; s.title = 'Draw the price, the market cap (stocks only) or the volume';
                s.onchange = () => mode.set(s.value);
                d.append(s);
            }
            if (key && !(mode && mode.cur !== 'price')) {          // a fan is of the price
                const k = h('button', 'ch-k' + (fanOn(c, key) ? ' on' : ''), 'K');
                k.title = 'Kronos scenario fan for this item (the KRONOS box in the header turns it on for all)';
                k.onclick = () => { c.fan[key] = !fanOn(c, key); save(); render(cell); renderInfo(); renderExplore(); };
                d.append(k);
            }
            if (onX) { const x = h('button', 'ch-x', '×'); x.title = 'Remove'; x.onclick = onX; d.append(x); }
            box.append(d);
        };
        const B = cell.B;
        row(c.symbol + (info.name ? ' · ' + info.name : ''), info.description, B && B.t.length ? `${fmtNum(B.c[B.c.length - 1])}${c.val === 'mcap' && cell.last && cell.last.unit ? ' ' + cell.last.unit : ''} · ${fmtDate(B.t[B.t.length - 1], c.tf)}` : '', 'sym:' + c.symbol, null, '#22c55e',
            FID.test(c.symbol) ? null : { cur: c.val, set: v => { c.val = v; save(); loadBars(cell); renderInfo(); renderExplore(); } });
        c.compare.forEach((s, k) => row(s, symInfo[s] && (symInfo[s].name || symInfo[s].description), cell.cmp[s] && cell.cmp[s].err ? cell.cmp[s].err : '', 'sym:' + s, () => { c.compare = c.compare.filter(x => x !== s); delete cell.cmp[s]; delete c.cval[s]; save(); render(cell); renderInfo(); renderExplore(); }, COLORS[(k + 1) % COLORS.length],
            FID.test(s) ? null : { cur: c.cval[s] || 'price', set: v => { c.cval[s] = v; delete cell.cmp[s]; save(); render(cell); renderInfo(); renderExplore(); } }));
        c.series.forEach((sd, k) => {
            const l = cell.lines[sd.id], m = l && l.meta, n = m ? m.t.length : 0;
            row(m ? m.label : sd.id, m && m.description, m ? (n ? `${fmtNum(m.v[n - 1])} ${m.unit} · ${new Date(m.t[n - 1] * 1000).toISOString().slice(0, 10)}` : 'no data') : 'loading…', 'ser:' + sd.id, () => { c.series = c.series.filter(s => s.id !== sd.id); delete cell.lines[sd.id]; save(); render(cell); renderInfo(); renderExplore(); }, COLORS[(k + 3) % COLORS.length]);
        });
    }
    async function loadCatalog() {
        if (!catalog) catalog = await chartJson(`${API_BASE}/api/chart/catalog`);
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
        (catalog.formulas || []).forEach(f => {                         // saved formulas: the name (or id) and the expression
            symInfo[f.id] = { name: f.name || f.expr, kind: 'FORMULA', description: f.description };
            if (st.hiddenF.includes(f.id)) return;
            const w = h('span', 'ch-chip-wrap'), b = h('button', 'ch-chip'), x = h('button', 'ch-chip ch-chip-x', '×');
            b.append(h('b', null, f.name || f.id), h('span', null, ' ' + f.expr));
            b.title = `${f.id}: ${f.description}`;
            b.onclick = () => pick(f.id);
            const plus = h('button', 'ch-chip ch-chip-x', '+');
            plus.title = 'Add it to the active chart, next to what it shows';
            plus.onclick = () => addToChart(f.id);
            x.title = 'Hide this formula in this browser (formulas cannot be deleted)';
            x.onclick = () => { st.hiddenF.push(f.id); save(); loadCatalog(); };
            w.append(b, plus, x); box.append(w);
        });
        return catalog;
    }

    // ---- symbol box and pickers
    function pick(sym) {
        sym = normSym(sym);
        if (!isSym(sym)) { status('That is not a valid symbol.', true); return; }
        const cell = A(), c = P(cell);
        delete c.fan['sym:' + c.symbol]; c.symbol = sym;
        cell.ovSym = null; cell.ov = null; pendingPt = null;
        save();
        $('chSym').value = sym;
        $('chLookup').classList.add('hidden');
        cell.retryStart = 0;
        status('');
        renderDraws();
        loadBars(cell);
    }
    function dropdown(box, rows) { box.replaceChildren(...rows); box.classList.toggle('hidden', !rows.length); }
    let lookupT = null, lookupSeq = 0;
    function lookup(input, box, onPick, query, extra) {            // query: search this instead of the whole input; extra: rows shown first
        clearTimeout(lookupT);
        const q = (query == null ? input.value : query).trim();
        if (!q) { box.classList.add('hidden'); return; }
        lookupT = setTimeout(async () => {
            const my = ++lookupSeq;
            const r = await chartJson(`${API_BASE}/api/chart/lookup?q=${encodeURIComponent(q)}`);
            if (my !== lookupSeq) return;
            dropdown(box, (extra ? extra(q) : []).concat((r.status === 'success' ? r.matches : []).map(m => {
                symInfo[m.symbol] = symInfo[m.symbol] || m;
                const row = h('button', 'ch-row');
                row.append(h('b', null, m.symbol), h('span', 'ch-kind', ` ${m.kind || ''}${m.exchange ? ' · ' + m.exchange : ''}`),
                    h('div', 'ch-name', m.name || ''), h('div', 'ch-desc', m.description || (m.stored ? '' : 'not stored yet: loads its history on first use')));
                row.onmousedown = e => { e.preventDefault(); onPick(m.symbol, m); };
                return row;
            })));
        }, 250);
    }
    async function openSeriesPicker() {
        const box = $('chSeriesPick');
        if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
        const cat = await loadCatalog();
        if (cat.status !== 'success') { status(cat.message || 'catalog unavailable', true); return; }
        dropdown(box, cat.series.map(s => {
            const row = h('div', 'ch-row');
            row.append(h('b', null, s.label), h('span', 'ch-kind', ` ${s.unit} · ${s.freq}${s.last ? ' · to ' + s.last : ''}`), h('div', 'ch-desc', s.description));
            const act = h('div', 'ch-act');
            [['left', 'LEFT SCALE'], ['pane', 'LOWER PANE']].forEach(([where, label]) => {
                const b = h('button', 'ch-btn', label);
                b.onclick = () => {
                    box.classList.add('hidden');
                    const c = P(A());
                    if (!c.series.some(x => x.id === s.id)) c.series.push({ id: s.id, where });
                    save(); render(A());
                };
                act.append(b);
            });
            row.append(act);
            return row;
        }));
    }

    // ---- formulas: saved on the server (POST /api/chart/formulas), computed there from the stored bars and series
    async function loadSymbol(sym, msg) {                           // first use of a symbol starts its download; wait for the daily bars
        for (let n = 0; n < 40; n++) {
            const r = await chartJson(`${API_BASE}/api/chart/bars?symbol=${encodeURIComponent(sym)}&tf=1d`);
            if (r.status === 'success') return true;
            if (r.status !== 'loading') { msg.textContent = `${sym}: ${r.message || r.reason || 'no history'}`; return false; }
            msg.textContent = `Loading ${sym} history…`;
            await new Promise(res => setTimeout(res, 3000));
        }
        msg.textContent = `${sym}: still no history after 2 minutes.`;
        return false;
    }
    const addToChart = id => {                                      // next to what the active chart shows (compare), not instead of it
        const cell = A(), c = P(cell);
        if (id !== c.symbol && !c.compare.includes(id)) c.compare.push(id);
        save(); render(cell); renderInfo();
    };
    let pendingLoads = [];                                          // downloads started by picking a symbol in the formula box
    async function saveFormula(expr, name, msg, offer, show) {
        msg.className = 'ch-desc'; offer.replaceChildren();
        if (pendingLoads.length) { msg.textContent = 'Waiting for the picked symbols to download…'; await Promise.all(pendingLoads); pendingLoads = []; }
        msg.textContent = 'Saving…';
        const r = await fetchJson(`${API_BASE}/api/chart/formulas`, { method: 'POST', headers: { 'X-Chart-Request': '1', 'Content-Type': 'application/json' }, body: JSON.stringify({ expr, name }) });
        if (r.status === 'success') {
            msg.textContent = ''; catalog = null; $('chFormDrop').classList.add('hidden'); await loadCatalog();
            if (show) pick(r.id); else addToChart(r.id);
            return;
        }
        msg.className = 'ch-bad'; msg.textContent = r.message || 'could not save the formula';
        (r.unknown || []).filter(s => SYM_RE.test(s)).forEach(sym => {
            const b = h('button', 'ch-btn', `ADD ${sym}`);
            b.title = 'Download this symbol first, then save the formula';
            b.onclick = async () => { b.disabled = true; msg.className = 'ch-desc'; if (await loadSymbol(sym, msg)) saveFormula(expr, name, msg, offer, show); else { msg.className = 'ch-bad'; b.disabled = false; } };
            offer.append(b);
        });
    }
    function openFormula() {
        const box = $('chFormDrop');
        if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
        const expr = h('input', 'ch-input'), name = h('input', 'ch-input'), msg = h('div', 'ch-desc'), offer = h('div', 'ch-act');
        expr.placeholder = 'gold / silver'; expr.setAttribute('aria-label', 'Formula'); expr.spellcheck = false; expr.maxLength = 200;
        expr.setAttribute('autocapitalize', 'off'); expr.setAttribute('autocorrect', 'off'); expr.enterKeyHint = 'done';
        name.placeholder = 'Name (optional)'; name.setAttribute('aria-label', 'Formula name'); name.maxLength = 60;
        [expr, name].forEach(i => { i.style.cssText = 'width:100%;display:block;margin:4px 0;text-transform:none'; });
        // autocomplete the word at the cursor: pick (click or Tab) puts the symbol in as a finished token, then type / * + -
        const wrap = h('div'), sug = h('div', 'ch-drop hidden');
        wrap.style.position = 'relative'; wrap.append(expr, sug);
        const word = () => {
            const v = expr.value, at = expr.selectionStart ?? v.length;
            let a = at, b = at;
            while (a > 0 && /[A-Za-z0-9.^=_:\-]/.test(v[a - 1])) a--;
            while (b < v.length && /[A-Za-z0-9.^=_:\-]/.test(v[b])) b++;
            return { a, b, q: v.slice(a, b) };
        };
        const put = (tok, m) => {
            const f = FID.test(tok) && ((catalog && catalog.formulas) || []).find(x => x.id === tok);
            if (f) tok = `(${f.expr})`;                             // a saved formula goes in as its expression
            const { a, b } = word(), v = expr.value, tail = v.slice(b).replace(/^ +/, '');
            expr.value = v.slice(0, a) + tok + ' ' + tail;
            const caret = a + tok.length + 1; expr.setSelectionRange(caret, caret); expr.focus();
            sug.classList.add('hidden');
            if (m && m.stored === false && SYM_RE.test(tok)) {      // not stored yet: download it now, so Save can use it
                msg.className = 'ch-desc';
                pendingLoads.push(loadSymbol(tok, msg).then(ok => { if (ok) msg.textContent = `${tok} is ready.`; return ok; }));
            }
        };
        const seriesRows = q => {                                   // catalog series match locally (the lookup route has symbols and formulas)
            const ql = q.toLowerCase();
            return ((catalog && catalog.series) || []).filter(s => s.id.toLowerCase().includes(ql) || s.label.toLowerCase().includes(ql)).slice(0, 4).map(s => {
                const row = h('button', 'ch-row');
                row.append(h('b', null, s.id), h('span', 'ch-kind', ` SERIES · ${s.unit}`), h('div', 'ch-name', s.label), h('div', 'ch-desc', s.description));
                row.onmousedown = e => { e.preventDefault(); put(s.id); };
                return row;
            });
        };
        expr.addEventListener('input', () => {
            const { q } = word();
            if (!q || /^[0-9.]+$/.test(q)) { sug.classList.add('hidden'); return; }
            loadCatalog().then(() => lookup(expr, sug, put, q, seriesRows));
        });
        expr.addEventListener('blur', () => setTimeout(() => sug.classList.add('hidden'), 150));
        const go = show => saveFormula(expr.value, name.value, msg, offer, show);
        expr.addEventListener('keydown', e => {
            const first = sug.classList.contains('hidden') ? null : sug.querySelector('.ch-row');
            if ((e.key === 'Tab' || e.key === 'Enter') && first) { e.preventDefault(); first.dispatchEvent(new MouseEvent('mousedown')); return; }
            if (e.key === 'Escape') { sug.classList.add('hidden'); return; }
            if (e.key === 'Enter') go(false);
        });
        name.addEventListener('keydown', e => { if (e.key === 'Enter') go(false); });
        const add_ = h('button', 'ch-btn', 'ADD TO CHART'), show_ = h('button', 'ch-btn', 'SHOW');
        add_.title = 'Save it and draw it next to what this chart shows (Enter)'; show_.title = 'Save it and make it the main item of this chart';
        add_.onclick = () => go(false); show_.onclick = () => go(true);
        box.replaceChildren(h('div', 'ch-desc', 'Type a name or ticker (silver, gold, diesel…) and pick it with Tab or a click; then / * + - ( ) and numbers, e.g. GC=F / SI=F, (HO=F * 42) - CL=F, eia.days.distillate / eia.days.gasoline. Put spaces around a minus. High and low of the candles are approximate.'), wrap, name, add_, show_, msg, offer);
        box.classList.remove('hidden'); expr.focus();
    }

    // ---- indicators
    function renderInd() {
        const box = $('chIndDrop'), c = P(A());
        box.replaceChildren();
        const add = h('div', 'ch-ind-add');
        Object.entries(IND).forEach(([type, d]) => {
            const b = h('button', 'ch-btn', d.name);
            b.onclick = () => { c.ind.push({ id: type + Date.now().toString(36), type, p: {} }); save(); render(A()); renderInd(); };
            add.append(b);
        });
        box.append(add);
        c.ind.forEach(ind => {
            const d = IND[ind.type], row = h('div', 'ch-ind-row');
            row.append(h('b', null, d.name));
            d.params.forEach(([k, def]) => {
                const col = k === 'color', i = k === 'src' ? h('select', 'ch-sel') : h('input', 'ch-in');
                if (k === 'src') SRC.forEach(s => { const o = h('option', null, s); o.value = s; i.append(o); });
                else i.type = col ? 'color' : 'number';
                if (!col && k !== 'src') { i.min = k === 'mult' ? 0.1 : 1; i.step = k === 'mult' ? 0.1 : 1; i.title = k; i.placeholder = k; }
                i.value = ind.p[k] != null ? ind.p[k] : def;
                if (col) i.title = 'colour';
                i.onchange = () => { ind.p[k] = col || k === 'src' ? i.value : Number(i.value); save(); render(A()); };
                if (k !== 'src' && !col) row.append(h('span', 'ch-desc', k));
                row.append(i);
            });
            if (ind.type === 'vwap' && !isIntra(c.tf)) row.append(h('span', 'ch-desc', 'intraday charts only'));
            const x = h('button', 'ch-x', '×'); x.title = 'Remove';
            x.onclick = () => { c.ind = c.ind.filter(i => i !== ind); save(); render(A()); renderInd(); };
            row.append(x);
            box.append(row);
        });
    }

    // ---- drawings, day view, measure
    function renderDraws() {
        const box = $('chDraws'), c = P(A());
        box.replaceChildren();
        (drawings[c.symbol] || []).forEach((d, i) => {
            const b = h('button', 'ch-chip', (d.type === 'h' ? 'H ' + fmtNum(d.price) : 'T ' + fmtNum(d.p1) + '→' + fmtNum(d.p2)) + ' ×');
            b.title = 'Remove this drawing';
            b.onclick = () => { drawings[c.symbol].splice(i, 1); saveDraw(); render(A()); renderDraws(); };
            box.append(b);
        });
        $('chToolH').classList.toggle('on', tool === 'h'); $('chToolT').classList.toggle('on', tool === 't');
    }
    function onClick(cell, p) {
        if (cell.i !== st.active) return;
        const c = P(cell);
        if (tool && p.point && p.time) {
            const price = cell.candles.coordinateToPrice(p.point.y);
            if (price == null) return;
            const list = drawings[c.symbol] = drawings[c.symbol] || [];
            if (tool === 'h') { list.push({ type: 'h', price }); tool = null; }
            else if (!pendingPt) { pendingPt = { t: p.time, p: price }; status('Trend line: click the second point.'); return; }
            else { list.push({ type: 't', t1: pendingPt.t, p1: pendingPt.p, t2: p.time, p2: price }); pendingPt = null; tool = null; status(''); }
            saveDraw(); render(cell); renderDraws();
            return;
        }
        if (!p.time || !cell.B || p.time > cell.B.t[cell.B.t.length - 1]) return;
        openDay(cell, fmtDate(p.time, c.tf));
    }
    let dayDate = null, dayPoll = null;
    function closeDay() { $('chDay').classList.add('hidden'); dayDate = null; clearTimeout(dayPoll); }
    async function openDay(cell, date, tries = 0) {
        const c = P(cell), panel = $('chDay');
        if (tries === 0) { dayDate = date; panel.classList.remove('hidden'); }
        const syms = [c.symbol].concat(c.compare).filter(s => !FID.test(s)), ids = c.series.map(s => s.id);
        const r = await chartJson(`${API_BASE}/api/chart/day?date=${date}&symbols=${encodeURIComponent(syms.join(','))}&series=${encodeURIComponent(ids.join(','))}`);
        if (dayDate !== date) return;
        panel.replaceChildren();
        const head = h('div', 'ch-day-head'), x = h('button', 'ch-x', '×'); x.title = 'Close'; x.onclick = closeDay;
        head.append(h('b', null, date), x);
        panel.append(head);
        if (r.status !== 'success') { panel.append(h('div', 'ch-bad', r.reason || r.message || 'No data for that day.')); return; }
        const sec = (t, kids) => { const s = h('div', 'ch-day-sec'); s.append(h('div', 'ch-tracked-h', t)); kids.length ? s.append(...kids) : s.append(h('div', 'ch-desc', 'none')); panel.append(s); };
        sec('PRICES', (r.prices || []).map(p => { const d = h('div', 'ch-day-row'); d.append(h('b', null, p.symbol), h('span', null, `O ${fmtNum(p.o)} H ${fmtNum(p.h)} L ${fmtNum(p.l)} C ${fmtNum(p.c)}`), h('span', p.chg_pct >= 0 ? 'ch-up' : 'ch-dn', p.chg_pct == null ? '' : (p.chg_pct >= 0 ? '+' : '') + Number(p.chg_pct).toFixed(2) + '%')); return d; }));
        sec('SERIES', (r.series || []).map(s => { const d = h('div', 'ch-day-row'); d.append(h('b', null, s.label), h('span', null, `${fmtNum(s.value)} ${s.unit || ''}${s.asof ? ' · ' + s.asof : ''}`), h('span', 'ch-desc', s.wow == null ? '' : 'WoW ' + (s.wow > 0 ? '+' : '') + (typeof s.wow === 'number' ? fmtNum(s.wow) : s.wow))); return d; }));
        sec('OUR RUNS', (r.runs || []).map(u => { const d = h('div', 'ch-day-row'); d.append(h('b', null, u.created_at ? String(u.created_at).slice(11, 16) : ''), h('span', null, u.summary || u.run_id || '')); return d; }));
        sec('CALENDAR', (r.calendar || []).map(e => { const d = h('div', 'ch-day-row'); d.append(h('b', null, e.time || ''), h('span', null, e.title || ''), h('span', 'ch-desc', e.kind || '')); return d; }));
        sec('HEADLINES', (r.headlines || []).map(n => {
            const d = h('div', 'ch-day-row');
            const a = h('a', null, n.title || n.url);
            if (/^https?:\/\//i.test(n.url || '')) { a.href = n.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; }
            d.append(a, h('span', 'ch-desc', ' ' + [n.source, n.published_at && String(n.published_at).slice(0, 10)].filter(Boolean).join(' · ')));
            return d;
        }));
        if (r.headlines_status === 'loading') {
            panel.append(h('div', 'ch-desc', 'Fetching headlines…'));
            if (tries < 40) { clearTimeout(dayPoll); dayPoll = setTimeout(() => openDay(cell, date, tries + 1), 3000); }
        } else if (r.headlines_status === 'none') panel.append(h('div', 'ch-desc', 'No headlines found for this day.'));
    }
    function measure(cell) {
        let s = null;
        const pt = e => { const r = cell.box.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top, l = cell.chart.timeScale().coordinateToLogical(x), n = cell.B ? cell.B.t.length : 0; return { x, y, i: l == null || !n ? null : Math.max(0, Math.min(n - 1, Math.round(l))), p: cell.candles.coordinateToPrice(y) }; };
        const hide = () => cell.meas.classList.add('hidden');
        cell.box.addEventListener('mousedown', e => {
            if (e.button || !e.shiftKey) { hide(); return; }
            e.stopPropagation(); e.preventDefault();
            s = pt(e);
            cell.chart.applyOptions({ handleScroll: false, handleScale: false });
        }, true);
        window.addEventListener('mousemove', e => {
            if (!s) return;
            const q = pt(e);
            if (s.p == null || q.p == null || s.i == null || q.i == null) return;
            const d = q.p - s.p, B = cell.B, m = cell.meas;
            m.classList.remove('hidden');
            m.style.cssText = `left:${Math.min(s.x, q.x)}px;top:${Math.min(s.y, q.y)}px;width:${Math.abs(q.x - s.x)}px;height:${Math.abs(q.y - s.y)}px;background:${d >= 0 ? 'rgba(34,197,94,.15)' : 'rgba(239,68,68,.15)'};border-color:${d >= 0 ? '#22c55e' : '#ef4444'}`;
            m.textContent = `${d >= 0 ? '+' : ''}${fmtNum(d)} (${d >= 0 ? '+' : ''}${(d / s.p * 100).toFixed(2)}%) · ${Math.abs(q.i - s.i)} bars · ${elapsed(B.t[q.i] - B.t[s.i])}`;
        });
        window.addEventListener('mouseup', () => { if (!s) return; s = null; cell.chart.applyOptions({ handleScroll: true, handleScale: true }); });
        document.addEventListener('keydown', e => { if (e.key === 'Escape') hide(); });
    }

    // ---- toolbar
    function buttons(box, list, key) {
        list.forEach(([val, label]) => {
            const b = h('button', 'ch-btn', label);
            b.dataset.v = val;
            b.onclick = () => {
                const cell = A(), c = P(cell);
                c[key] = val;
                if (key === 'tf') { c.range = DEFAULT_RANGE[val]; cell.fans = {}; cell.cmp = {}; }
                save(); syncUI(); loadBars(cell);
            };
            box.append(b);
        });
    }
    function syncUI() {
        const c = P(A());
        document.querySelectorAll('#chTf .ch-btn').forEach(b => b.classList.toggle('on', b.dataset.v === c.tf));
        document.querySelectorAll('#chRange .ch-btn').forEach(b => b.classList.toggle('on', b.dataset.v === c.range));
        document.querySelectorAll('#chLayout .ch-btn').forEach(b => b.classList.toggle('on', b.dataset.v === st.layout));
        $('chSym').value = c.symbol;
        $('chSync').classList.toggle('on', st.sync);
        $('chPct').classList.toggle('on', c.pct);
        $('chZones').value = c.zones; $('chK').value = c.k;
        [['chLv', 'levels'], ['chEv', 'events'], ['chBl', 'blocks']].forEach(([id, k]) => $(id).classList.toggle('on', c[k]));
        $('chToolH').classList.toggle('on', tool === 'h'); $('chToolT').classList.toggle('on', tool === 't');
        if (!$('chIndDrop').classList.contains('hidden')) renderInd();
    }
    const toggle = k => { const c = P(A()); c[k] = !c[k]; save(); syncUI(); render(A()); };

    window.initChartTab = function () {
        if (started) return;
        started = true;
        LW = window.LightweightCharts;
        if (!LW) { status('Chart library did not load (jsDelivr blocked?).', true); return; }
        buttons($('chTf'), TFS, 'tf');
        buttons($('chRange'), RANGES.map(r => [r[0], r[0]]), 'range');
        Object.keys(LAYOUTS).forEach(k => {
            const b = h('button', 'ch-btn', k === '2x2' ? '2×2' : k === '3x2' ? '3×2' : k);
            b.dataset.v = k; b.title = 'Chart layout';
            b.onclick = () => { st.layout = k; save(); buildGrid(); };
            $('chLayout').append(b);
        });
        const input = $('chSym');
        input.addEventListener('input', () => lookup(input, $('chLookup'), pick));
        input.addEventListener('keydown', e => { if (e.key === 'Enter') pick(input.value); if (e.key === 'Escape') $('chLookup').classList.add('hidden'); });
        input.addEventListener('blur', () => setTimeout(() => $('chLookup').classList.add('hidden'), 150));
        const cmp = $('chCmp');
        $('chCmpBtn').onclick = () => { $('chCmpWrap').classList.toggle('open'); cmp.focus(); };
        const addCmp = sym => {
            sym = normSym(sym);
            if (!isSym(sym)) { status('That is not a valid symbol.', true); return; }
            const cell = A(), c = P(cell);
            if (sym !== c.symbol && !c.compare.includes(sym)) c.compare.push(sym);
            cmp.value = ''; $('chCmpLookup').classList.add('hidden'); $('chCmpWrap').classList.remove('open');
            save(); render(cell);
        };
        cmp.addEventListener('input', () => lookup(cmp, $('chCmpLookup'), addCmp));
        cmp.addEventListener('keydown', e => { if (e.key === 'Enter') addCmp(cmp.value); });
        cmp.addEventListener('blur', () => setTimeout(() => $('chCmpLookup').classList.add('hidden'), 150));
        $('chPct').onclick = () => toggle('pct');
        $('chSync').onclick = () => { st.sync = !st.sync; save(); syncUI(); };
        $('chAdd').onclick = openSeriesPicker;
        $('chForm').onclick = openFormula;
        $('chInd').onclick = () => { const b = $('chIndDrop'); b.classList.toggle('hidden'); if (!b.classList.contains('hidden')) renderInd(); };
        $('chZones').onchange = e => { const c = P(A()); c.zones = e.target.value; save(); render(A()); };
        $('chK').onchange = e => { const c = P(A()); c.k = Number(e.target.value) > 0 ? Number(e.target.value) : 1.5; save(); render(A()); };
        $('chLv').onclick = () => toggle('levels'); $('chEv').onclick = () => toggle('events'); $('chBl').onclick = () => toggle('blocks');
        $('chToolH').onclick = () => { tool = tool === 'h' ? null : 'h'; pendingPt = null; syncUI(); status(tool ? 'Click the chart to place a horizontal line.' : ''); };
        $('chToolT').onclick = () => { tool = tool === 't' ? null : 't'; pendingPt = null; syncUI(); status(tool ? 'Trend line: click the first point.' : ''); };
        document.addEventListener('click', e => {
            if (!e.target.closest('#chAddWrap')) $('chSeriesPick').classList.add('hidden');
            if (!e.target.closest('#chIndWrap')) $('chIndDrop').classList.add('hidden');
            if (!e.target.closest('#chFormWrap')) $('chFormDrop').classList.add('hidden');
        });
        document.addEventListener('keydown', e => { if (e.key === 'Escape') { tool = null; pendingPt = null; syncUI(); closeDay(); } });
        window.addEventListener('fans:toggle', () => { cells.forEach(render); renderInfo(); renderExplore(); });
        const cb = $('fansToggle'); if (cb) cb.addEventListener('change', () => { cells.forEach(render); renderInfo(); renderExplore(); });
        buildGrid();
        loadCatalog().then(renderInfo);
    };
})(typeof window !== 'undefined' ? window : globalThis);
