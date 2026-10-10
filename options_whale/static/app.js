const API_BASE = window.location.origin || "";

// 1. HELPER: Format Date to "MMM DD, YYYY"
function formatDate(dateStr) {
    const date = new Date(dateStr + "T12:00:00"); 
    return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

// 2. HELPER: Convert large numbers to Millions/Thousands
function formatCompact(valStr) {
    const num = parseFloat(String(valStr).replace(/[$,]/g, ''));
    if (!Number.isFinite(num)) return DASH;
    const sign = num < 0 ? '-' : '', abs = Math.abs(num);
    if (abs >= 1000000) return sign + (abs / 1000000).toFixed(2) + "M";
    if (abs >= 1000) return sign + (abs / 1000).toFixed(1) + "K";
    return num.toFixed(2);
}

// HELPERS: a value that is missing (null, undefined, NaN) is shown as a dash, never as 0 (the server sends null with a reason)
const DASH = '—';
const toNum = v => ((typeof v === 'number' || (typeof v === 'string' && v.trim() !== '')) && Number.isFinite(Number(v))) ? Number(v) : null;
const isNum = v => toNum(v) !== null;
function fmt(v, d = 2, prefix = '', suffix = '') {
    const n = toNum(v);
    return n === null ? DASH : `${prefix}${n.toFixed(d)}${suffix}`;
}
function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Fetches a JSON route and always hands back the parsed body, also for 4xx/5xx answers (the routes send
// {"status":"error","message":...} with a real status code). A body that is not JSON becomes an error body that names
// the HTTP status, so callers show a message instead of throwing on res.json(). body.http holds the status code.
async function fetchJson(url, options) {
    const res = await fetch(url, options);
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (body === null || typeof body !== 'object' || Array.isArray(body)) {
        body = { status: 'error', message: res.ok ? 'unexpected response (not JSON)' : `HTTP ${res.status}${res.statusText ? ' ' + res.statusText : ''}` };
    } else if (!res.ok) {
        if (body.status === undefined || body.status === 'success') body.status = 'error';
        if (!body.message) body.message = `HTTP ${res.status}${res.statusText ? ' ' + res.statusText : ''}`;
    }
    Object.defineProperty(body, 'http', { value: res.status, enumerable: false, configurable: true });
    return body;
}

// Text of an <error>...</error> XML body (the XML routes send one with a 4xx/5xx status), or null when the text is not one.
function xmlErrorText(text) {
    try {
        const doc = new DOMParser().parseFromString(String(text), 'text/xml');
        const root = doc.documentElement;
        if (root && root.nodeName === 'error') return (root.textContent || '').trim();
    } catch (e) { /* not XML */ }
    return null;
}

// 3. MAIN TERMINAL LOGGER
function log(content, type = 'info') {
    // On a phone only a command line the user started brings the console forward; load-time and background messages do not
    if (type === 'cmd' && typeof switchMobileTab === 'function') switchMobileTab('console');
    updateConsoleDock(content, type);
    const consoleLog = document.getElementById('consoleLog');
    const time = new Date().toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    
    const entry = document.createElement('div');
    entry.className = "mb-4 border-l-2 border-zinc-800 pl-4";
    
    let color = "text-zinc-500";
    if (type === 'success') color = "text-green-500 font-bold";
    if (type === 'error') color = "text-red-500 font-bold";
    if (type === 'warn') color = "text-yellow-500 font-bold";
    if (type === 'cmd') color = "text-blue-400";
    
    entry.innerHTML = `<div class="text-[9px] mb-1"><span class="text-zinc-700">[${time}]</span> <span class="${color} uppercase">${type}</span></div>`;

    // CHECK: Is this the Whale Hunt XML?
    if (typeof content === 'string' && content.includes('<whale_hunt')) {
        const parser = new DOMParser();
        const xmlDoc = parser.parseFromString(content, "text/xml");
        const contracts = Array.from(xmlDoc.getElementsByTagName("contract"));
        
        // Calculate Totals for Summary
        let bullPrem = 0; let bearPrem = 0;
        contracts.forEach(c => {
            const p = parseFloat(c.getAttribute('premium_spent').replace(/[$,]/g, ''));
            if (c.getAttribute('type') === 'CALL') bullPrem += p; else bearPrem += p;
        });

        // Sort by premium (Highest first)
        contracts.sort((a, b) => {
            const premA = parseFloat(a.getAttribute('premium_spent').replace(/[$,]/g, ''));
            const premB = parseFloat(b.getAttribute('premium_spent').replace(/[$,]/g, ''));
            return premB - premA;
        });

        // Build Pretty Summary Header
        let summaryHtml = `
            <div class="flex gap-4 mb-4 bg-zinc-900/40 p-3 rounded border border-zinc-800">
                <div><span class="text-zinc-500 text-[10px] block uppercase">Call Premium</span><span class="text-green-500 font-bold">$${(bullPrem/1e6).toFixed(2)}M</span></div>
                <div class="border-r border-zinc-800"></div>
                <div><span class="text-zinc-500 text-[10px] block uppercase">Put Premium</span><span class="text-red-500 font-bold">$${(bearPrem/1e6).toFixed(2)}M</span></div>
                <div class="ml-auto text-right"><span class="text-zinc-500 text-[10px] block uppercase">Sentiment</span><span class="${bullPrem > bearPrem ? 'text-green-400' : 'text-red-400'} font-bold">${bullPrem > bearPrem ? 'BULLISH' : 'BEARISH'}</span></div>
            </div>
            <div class="grid grid-cols-1 gap-2">`;
        
        contracts.forEach(c => {
            const type = c.getAttribute('type');
            const accent = type === 'CALL' ? 'text-green-400' : 'text-red-400';
            const premRaw = c.getAttribute('premium_spent');
            
            summaryHtml += `
                <div class="bg-zinc-950 border border-zinc-900 p-3 rounded hover:bg-zinc-900 transition-all group">
                    <div class="flex justify-between items-start">
                        <div class="text-[12px]">
                            <span class="${accent} font-bold">${type === 'CALL' ? '🔥' : '🩸'} ${type}:</span>
                            <span class="text-white ml-1 font-bold">${esc(formatDate(c.getAttribute('expiration')))}</span>
                            <span class="text-zinc-500 mx-1">/</span>
                            <span class="text-zinc-200">$${esc(c.getAttribute('strike'))} Strike</span>
                            <span class="text-zinc-600 mx-2">—</span>
                            <span class="text-white font-bold">$${esc(formatCompact(premRaw))} premium</span>
                        </div>
                    </div>
                    
                    <div class="grid grid-cols-4 gap-2 mt-3 pt-2 border-t border-zinc-900 text-[9px] group-hover:border-zinc-700">
                        <div><span class="text-zinc-600 block uppercase">Intensity</span><span class="text-blue-400">${esc(c.getAttribute('vol_oi_ratio'))}</span></div>
                        <div><span class="text-zinc-600 block uppercase">Vol/OI</span><span class="text-zinc-400">${esc(c.getAttribute('volume'))} / ${esc(c.getAttribute('open_interest'))}</span></div>
                        <div><span class="text-zinc-600 block uppercase">Expectation</span><span class="text-orange-400">${esc(c.getAttribute('implied_volatility'))} IV</span></div>
                        <div><span class="text-zinc-600 block uppercase">Slippage</span><span class="text-zinc-500">${esc(c.getAttribute('spread'))} Spread</span></div>
                    </div>
                </div>`;
        });
        
        summaryHtml += `</div>`;
        entry.innerHTML += summaryHtml;
    }
    // CHECK: Is this the Physical Arbitrage XML?
    else if (typeof content === 'string' && content.includes('<physical_arbitrage')) {
        const parser = new DOMParser();
        const xmlDoc = parser.parseFromString(content, "text/xml");
        const root = xmlDoc.getElementsByTagName("physical_arbitrage")[0];
        const listings = Array.from(xmlDoc.getElementsByTagName("listing"));

        if (!root || listings.length === 0) {
            entry.innerHTML += `<div class="text-red-500">Scan failed or returned no retail inventory.</div>`;
            consoleLog.appendChild(entry);
            consoleLog.scrollTop = consoleLog.scrollHeight;
            return;
        }

        const comexSpot = root.getAttribute("comex_spot");
        const timestamp = root.getAttribute("timestamp");

        // Build Pretty Summary Header for Arbitrage
        let summaryHtml = `
            <div class="flex gap-4 mb-4 bg-zinc-900/40 p-3 rounded border border-zinc-800">
                <div><span class="text-zinc-500 text-[10px] block uppercase">COMEX Spot</span><span class="text-blue-400 font-bold">${esc(comexSpot)}</span></div>
                <div class="border-r border-zinc-800"></div>
                <div><span class="text-zinc-500 text-[10px] block uppercase">Dealers Scanned</span><span class="text-white font-bold">${listings.length}</span></div>
                <div class="ml-auto text-right"><span class="text-zinc-500 text-[10px] block uppercase">Engine Timestamp</span><span class="text-zinc-400 font-bold">${esc((timestamp || '').split(' ')[1] || timestamp || DASH)}</span></div>
            </div>
            <div class="grid grid-cols-1 gap-2">`;
        
        listings.forEach(l => {
            const itemId = l.getAttribute("item_id");
            const name = l.getAttribute("name");
            const totalCost = l.getAttribute("total_cost");
            const shipping = l.getAttribute("shipping");
            const premDollars = l.getAttribute("premium_dollars");
            const premPercent = l.getAttribute("premium_percent");
            const status = l.getAttribute("status") || "";
            
            // Format Status Colors
            let statusColor = "text-zinc-500";
            if (status.includes("InStock")) statusColor = "text-green-500 font-bold";
            else if (status.includes("Fallback")) statusColor = "text-yellow-500";

            // CHANGED: Wrapped the card in an <a> tag pointing to the eBay listing
            summaryHtml += `
                <a href="https://www.ebay.com/itm/${encodeURIComponent(itemId || '')}" target="_blank" rel="noopener noreferrer" class="block bg-zinc-950 border border-zinc-900 p-3 rounded hover:border-blue-900 transition-all group cursor-pointer text-left">
                    <div class="flex justify-between items-start">
                        <div class="text-[12px] truncate w-3/4 pr-4">
                            <span class="text-blue-400 font-bold">🪙 RETAIL:</span>
                            <span class="text-zinc-200 ml-1 group-hover:text-blue-400 transition-colors" title="${esc(name)}">${esc(name)}</span>
                        </div>
                        <div class="text-right w-1/4">
                            <div class="text-white font-bold tracking-tighter group-hover:text-blue-400 transition-colors">${esc(totalCost)}</div>
                            <div class="text-[9px] text-zinc-600 uppercase">Total Cost</div>
                        </div>
                    </div>
                    
                    <div class="grid grid-cols-4 gap-2 mt-3 pt-2 border-t border-zinc-900 text-[9px] group-hover:border-blue-900/50">
                        <div><span class="text-zinc-600 block uppercase">Premium %</span><span class="text-orange-400 font-bold">+${esc(premPercent)}</span></div>
                        <div><span class="text-zinc-600 block uppercase">Premium $</span><span class="text-orange-400">+${esc(premDollars)}</span></div>
                        <div><span class="text-zinc-600 block uppercase">Shipping</span><span class="text-zinc-400">${esc(shipping)}</span></div>
                        <div><span class="text-zinc-600 block uppercase">Status</span><span class="${statusColor}">${esc(status)}</span></div>
                    </div>
                </a>`;
        });
        
        summaryHtml += `</div>`;
        entry.innerHTML += summaryHtml;
    } 
    else {
        // Fallback for standard text/error logs
        entry.innerHTML += `<div class="text-zinc-300 whitespace-pre-wrap">${esc(content)}</div>`;
    }

    consoleLog.appendChild(entry);
    consoleLog.scrollTop = consoleLog.scrollHeight;
}

// One line for the docked console bar (phones): the newest log line, truncated by CSS, in its level colour. The XML answers of the scans
// are shown as a count, not as markup. The text is set with textContent, so nothing in it is interpreted as HTML.
function consoleDockText(content) {
    const s = String(content ?? '');
    if (s.includes('<whale_hunt')) { const n = (s.match(/<contract\b/g) || []).length; return `Whale hunt: ${n} contract${n === 1 ? '' : 's'}`; }
    if (s.includes('<physical_arbitrage')) { const n = (s.match(/<listing\b/g) || []).length; return `Silver Eagles: ${n} listing${n === 1 ? '' : 's'}`; }
    return s.replace(/\s+/g, ' ').trim().slice(0, 300);
}
function updateConsoleDock(content, type) {
    const line = document.getElementById('consoleDockLine');
    if (!line) return;
    line.textContent = consoleDockText(content);
    line.dataset.level = type || 'info';
}
function clearConsole() {
    const el = document.getElementById('consoleLog');
    if (el) el.innerHTML = '';
    updateConsoleDock('Console cleared', 'info');
}

// Prints the answer of a scan route. The scans answer XML (<whale_hunt> or <physical_arbitrage>); a failure is
// <error>text</error> with a 4xx/5xx status, or a JSON error body such as the 403 from the cross-site guard.
async function logScanResponse(res) {
    const type = (res.headers.get('content-type') || '').toLowerCase();
    const text = await res.text();
    if (type.includes('xml')) {
        const err = xmlErrorText(text);
        if (err !== null) log(err || `HTTP ${res.status}`, 'error');
        else if (!res.ok) log(`HTTP ${res.status}: ${text.slice(0, 200)}`, 'error');
        else log(text, 'success');   // XML is passed on as it is: log() draws it
        return;
    }
    let msg = text;
    try { const j = JSON.parse(text); msg = j.message || j.data || text; } catch (e) { /* plain text */ }
    log(msg || `HTTP ${res.status}`, res.ok ? 'success' : 'error');
}

// Silver Eagle scan. It writes a ledger row, so the server accepts it only as a POST, and a second click while
// the first scan runs (up to 2 minutes) is refused here instead of writing a second row.
let _silverScanBusy = false;
async function triggerPhysicalArbScan() {
    if (_silverScanBusy) { log('A Silver Eagle scan is already running.', 'info'); return; }
    _silverScanBusy = true;
    log(`Initializing Physical Arbitrage Engine (Silver Eagles)...`, 'cmd');
    try {
        const res = await fetch(`${API_BASE}/api/silver_eagle_prices`, { method: 'POST' });
        await logScanResponse(res);
    } catch (e) { log(`System Error: ${e.message}`, 'error'); }
    finally { _silverScanBusy = false; }
}

// Logic to run standard morning/evening hunts. Both are filtered scans of the Target Ticker and answer the same XML as /api/custom.
async function runPredefined(mode) {
    log(`Broadcasting ${mode.toUpperCase()} position hunt...`, 'cmd');
    const ticker = (document.getElementById('scanTicker').value || "SPY").trim().toUpperCase();
    try {
        const res = await fetch(`${API_BASE}/api/${mode}?ticker=${encodeURIComponent(ticker)}`);
        await logScanResponse(res);
    } catch (e) { log(`Network Error: ${e.message}`, 'error'); }
}

// Logic to run the Custom XML-based scan
async function runCustomScan() {
    const ticker = (document.getElementById('scanTicker').value || "AMD").trim().toUpperCase();
    const vol = document.getElementById('scanVol').value;
    const dte = document.getElementById('scanDTE').value;
    const premium = document.getElementById('scanPremium').value;
    
    log(`Initiating Custom Whale Scan for $${ticker}...`, 'cmd');
    
    try {
        const url = `${API_BASE}/api/custom?ticker=${encodeURIComponent(ticker)}&min_vol_oi=${encodeURIComponent(vol)}&min_premium=${encodeURIComponent(premium)}&max_dte=${encodeURIComponent(dte)}`;
        const res = await fetch(url);
        await logScanResponse(res);
    } catch (e) { log(`Network Error: ${e.message}`, 'error'); }
}

// Status checker
async function checkStatus() {
    try {
        const res = await fetch(`${API_BASE}/help`);
        if (res.ok) {
            document.getElementById('apiStatus').innerText = "ONLINE";
            document.getElementById('apiStatus').className = "text-green-500";
            document.getElementById('statusDot').className = "w-2 h-2 bg-green-500 rounded-full status-pulse";
        } else {
            // the server answered, but with an error
            document.getElementById('apiStatus').innerText = `ERROR (HTTP ${res.status})`;
            document.getElementById('apiStatus').className = "text-red-500";
            document.getElementById('statusDot').className = "w-2 h-2 bg-red-500 rounded-full";
        }
    } catch (e) {
        document.getElementById('apiStatus').innerText = `OFFLINE (CHECK ${location.host.toUpperCase()})`;
        document.getElementById('apiStatus').className = "text-red-500";
        document.getElementById('statusDot').className = "w-2 h-2 bg-red-500 rounded-full";
    }
}

// ==========================================
// --- 4. WAR ROOM: SCENARIO ENGINE ---
// ==========================================
// The server (/api/war_room) owns the VMRI formula; this file only renders what it returns.

let warRoomDebounceTimer;
let currentBaseData = null; // Stores the live environment from the API
let warRoomData = null;     // Last full /api/war_room response

const WAR_TIERS = [
    { below: 150, name: 'LOW RISK', color: '#22c55e', css: 'text-green-500',
      read: 'Complacent. Volatility is cheap and credit is calm; the risk is a squeeze higher, and long options are poor value.' },
    { below: 250, name: 'MODERATE RISK', color: '#eab308', css: 'text-yellow-500',
      read: 'Normal operating range. Stock and bond relationships behave as usual; neutral premium selling works best here.' },
    { below: 350, name: 'ELEVATED RISK', color: '#f97316', css: 'text-orange-500',
      read: 'Hedge triggers active. Option prices are expanding and credit stress is feeding into metals and equities.' },
    { below: Infinity, name: 'SYSTEMIC THREAT', color: '#ef4444', css: 'text-red-500',
      read: 'Crash dynamics. Liquidity is drying up, high-yield credit is seizing and forced selling (margin calls) is likely.' },
];
const warTier = v => WAR_TIERS.find(t => v < t.below);
const warSigned = (v, d = 1) => `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(d)}`;
const warScalePct = v => Math.max(0, Math.min(100, v / 500 * 100));   // tripwire bar runs 0 to 500

const WAR_PRESETS = {
    '1970s STAG': { dxy: -15, tnx: +3.0, oas: +4.0, vix: +40, title: 'Stagflation era',
        note: 'Runaway inflation with stagnant growth: the dollar falls, yields jump, credit and volatility rise moderately.' },
    '2008 CRASH': { dxy: +12, tnx: -1.5, oas: +15.0, vix: +250, title: 'Global financial crisis',
        note: 'A credit freeze: junk-bond spreads blow out, volatility explodes, money runs to the dollar and Treasuries.' },
    '2020 COVID': { dxy: +8, tnx: -2.5, oas: +8.0, vix: +300, title: 'COVID dash for cash',
        note: 'A sudden shutdown: record volatility, yields collapse toward zero, credit spreads widen sharply.' },
};

// The four inputs. `per` describes what one slider step means for that variable.
const WAR_LEVERS = {
    dxy: { name: 'DXY — US Dollar Index', unit: '', step: '1 index point',
        what: 'The value of the US dollar against a basket of six currencies (euro 57.6%, yen, pound, Canadian dollar, Swedish krona, Swiss franc). 100 is the 1973 starting level.',
        why: 'A rising dollar tightens money worldwide: debt owed in dollars gets harder to repay, and anything priced in dollars (silver, gold, oil) faces a headwind. A sharp rise usually means investors are running to safety.',
        formula: 'Multiplied by the 10-year yield to make the macro base (DXY × 10Y ÷ 1.61).',
        ref: 'About 90 = weak dollar · 100 = neutral · 110+ = stress (peak near 114 in Sep 2022).' },
    tnx: { name: '10Y YLD — 10-year Treasury yield', unit: '%', step: '0.1 percentage point',
        what: 'The interest rate the US government pays to borrow for ten years. Mortgages, corporate loans and stock valuations are all priced off it.',
        why: 'Higher yields make borrowing dearer and make future profits worth less today, which pressures stocks and metals. Because it is a small number, small moves matter: going from 4% to 5% is a 25% rise.',
        formula: 'Multiplied by DXY to make the macro base. Dollar and yields rising together is the "wrecking ball" for risk assets.',
        ref: '0.5% (2020 low) · 2–3% (2010s) · 5% (Oct 2023).' },
    oas: { name: 'HY OAS — high-yield credit spread', unit: '', step: '0.1 percentage point',
        what: 'The extra yield investors demand to hold junk-rated company bonds instead of Treasuries, in percentage points (ICE BofA index). OAS means option-adjusted spread.',
        why: 'It is the market price of default risk. It widens when lenders get nervous, usually before stocks react, so it is the earliest warning of the four.',
        formula: 'Divided by 4 to make the credit multiplier. 4.0 is treated as normal: above it amplifies the score, below it dampens it.',
        ref: '3 = calm · 5+ = stress · about 11 (Mar 2020) · about 20 (Dec 2008).' },
    vix: { name: 'VIX — volatility index', unit: '', step: '5% of its live level',
        what: 'How much the S&P 500 is expected to move over the next 30 days, taken from option prices and shown as a yearly percentage. Often called the fear gauge.',
        why: 'It is the price of portfolio insurance. It jumps when investors rush to hedge, and a high VIX makes every option more expensive.',
        formula: 'Divided by 20 to make the volatility multiplier. 20 is treated as normal. This slider moves VIX by a percentage of its live level, because VIX moves in multiples rather than points.',
        ref: '12–15 = calm · 20 = long-run average · 30+ = fear · about 81 (Nov 2008) · 82.7 (Mar 2020, record close).' },
};

// Recorded-score histogram with live and scenario markers. (Name kept: the zoom and full-screen code calls it.)
function drawBellCurve() {
    const canvas = document.getElementById('bellCurve');
    const d = warRoomData, hist = d && d.history;
    if (!canvas || !hist) return;
    const rect = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    const w = rect.width, h = rect.height, n = hist.counts.length, peak = Math.max(...hist.counts, 1), bw = w / n;
    const x = v => Math.max(1, Math.min(w - 1, (v - hist.lo) / (hist.hi - hist.lo) * w));
    ctx.clearRect(0, 0, w, h);
    hist.counts.forEach((c, i) => {
        const mid = hist.lo + (i + 0.5) * (hist.hi - hist.lo) / n, bh = c / peak * (h - 4);
        ctx.fillStyle = warTier(mid).color + '66';
        ctx.fillRect(i * bw + 0.5, h - bh, Math.max(1, bw - 1), bh);
    });
    const mark = (v, color, dash) => {
        ctx.beginPath(); ctx.setLineDash(dash); ctx.strokeStyle = color; ctx.lineWidth = 1.5;
        ctx.moveTo(x(v), 0); ctx.lineTo(x(v), h); ctx.stroke(); ctx.setLineDash([]);
    };
    mark(d.current.vmri, '#a1a1aa', [3, 3]);
    if (Math.abs(d.hypothetical.vmri - d.current.vmri) > 0.05) mark(d.hypothetical.vmri, warTier(d.hypothetical.vmri).color, []);
}

function generateDeltaAnalysis(hypo) {
    const el = document.getElementById('deltaAnalysis'), t = warTier(hypo.vmri);
    el.className = `text-[8px] leading-tight font-mono ${t.css}`;
    el.innerText = `${t.name}: ${t.read}` + (hypo.tnx > 5.0 && hypo.dxy > 105 ? ' Dollar and yields are both high: heavy pressure on risk assets.' : '');
}

// Updates the text numbers next to the sliders to show Base + Shift
function updateSliderLabels() {
    const shift = { dxy: +document.getElementById('shiftDxy').value, tnx: +document.getElementById('shiftTnx').value,
                    oas: +document.getElementById('shiftOas').value, vix: +document.getElementById('shiftVix').value };
    const b = currentBaseData;
    const set = (id, txt) => { document.getElementById(id).innerText = txt; };
    if (b) {
        set('baseDxyTxt', `(${b.dxy.toFixed(2)})`);  set('valDxy', `${(b.dxy + shift.dxy).toFixed(2)} [${warSigned(shift.dxy, 2)}]`);
        set('baseTnxTxt', `(${b.tnx.toFixed(2)}%)`); set('valTnx', `${(b.tnx + shift.tnx).toFixed(2)}% [${warSigned(shift.tnx, 2)}]`);
        set('baseOasTxt', `(${b.oas.toFixed(2)})`);  set('valOas', `${(b.oas + shift.oas).toFixed(2)} [${warSigned(shift.oas, 2)}]`);
        set('baseVixTxt', `(${b.vix.toFixed(2)})`);  set('valVix', `${(b.vix * (1 + shift.vix / 100)).toFixed(2)} [${warSigned(shift.vix, 0)}%]`);
    } else {
        set('valDxy', warSigned(shift.dxy, 2)); set('valTnx', warSigned(shift.tnx, 2) + '%');
        set('valOas', warSigned(shift.oas, 2)); set('valVix', warSigned(shift.vix, 0) + '%');
    }
}

// The guide pop-up: the formula with today's numbers, the tiers, and each variable. Pass a lever key to jump to it.
// It is attached to <body> because the card's zoom transform would trap a fixed-position child.
function toggleWarGuide(focus) {
    let back = document.getElementById('warGuideBackdrop');
    if (back && !focus) { back.remove(); return; }
    if (!back) {
        back = document.createElement('div');
        back.id = 'warGuideBackdrop';
        back.className = 'war-guide-backdrop';
        back.innerHTML = '<div id="warGuide" class="war-guide" role="dialog" aria-modal="true" aria-label="How the war room works"></div>';
        back.addEventListener('click', e => { if (e.target === back) back.remove(); });
        document.body.appendChild(back);
    }
    document.getElementById('warGuide').dataset.focus = focus || '';
    renderWarGuide();
    if (focus) document.getElementById(`warGuide-${focus}`)?.scrollIntoView({ block: 'center' });
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') document.getElementById('warGuideBackdrop')?.remove(); });

function renderWarGuide() {
    const el = document.getElementById('warGuide'), d = warRoomData;
    if (!el || !d) return;
    const c = d.current, f = c.factors, focus = el.dataset.focus;
    const live = { dxy: c.dxy.toFixed(2), tnx: c.tnx.toFixed(2) + '%', oas: c.oas.toFixed(2), vix: c.vix.toFixed(2) };
    const onePct = (c.vmri / 100).toFixed(1);
    const levers = Object.entries(WAR_LEVERS).map(([k, v]) => `
        <div id="warGuide-${k}" class="war-guide-item ${focus === k ? 'focus' : ''}">
            <div class="text-[13px] text-white font-bold">${v.name} <span class="text-zinc-500 font-normal">· live ${live[k]}</span></div>
            <div><b>What it is.</b> ${v.what}</div>
            <div><b>Why it matters.</b> ${v.why}</div>
            <div><b>In the score.</b> ${v.formula}</div>
            <div><b>Reference levels.</b> ${v.ref}</div>
        </div>`).join('');
    el.innerHTML = `
        <div class="flex justify-between items-center">
            <div class="text-[12px] text-zinc-200 font-bold uppercase tracking-widest">How the war room works</div>
            <button class="text-[11px] text-zinc-500 hover:text-white" onclick="toggleWarGuide()">CLOSE ✕</button>
        </div>
        <div><b>The score.</b> VMRI (Vlad Macro Risk Index) = (DXY × 10Y yield ÷ 1.61) × (HY OAS ÷ 4) × (VIX ÷ 20).
            Live: ${f.macro_base.toFixed(1)} × ${f.credit.toFixed(2)} × ${f.vol.toFixed(2)} = <b class="text-white">${c.vmri.toFixed(1)}</b>.</div>
        <div><b>Reading it.</b> Below 150 low risk · 150–250 moderate · 250–350 elevated · 350+ systemic.
            The four inputs are multiplied, so a 1% rise in any one raises the score 1% (about ${onePct} points today), and stresses that arrive together compound.
            That is why the "alone" figure next to each slider does not add up to the total change.</div>
        <div><b>Using it.</b> Drag a slider to shift one input from its live reading; the big number, the bar and the history strip update.
            Scenario buttons add a preset set of shifts to today's readings; they are stylised shocks, not a replay of the actual levels in those years.</div>
        ${levers}
        <div class="text-zinc-500"><b>Limits.</b> VMRI is a home-built index, not a probability of a crash. The history strip covers only the readings this installation has recorded.</div>`;
}

// ==========================================
// --- PANEL ZOOM ENGINE ---
// ==========================================

function zoomPanel(panelId, delta) {
    const panel = document.getElementById(panelId);
    const content = panel.querySelector('.zoomable');
    if (!content) return;

    // 1. Get current zoom
    let oldZoom = parseFloat(localStorage.getItem(`vladhq_zoom_${panelId}`) || 1.0);
    
    // 2. Calculate target zoom and fix Javascript's floating point math bug (e.g. 0.900000001)
    let newZoom = oldZoom + delta;
    newZoom = Math.round(newZoom * 100) / 100;
    
    // 3. --- THE ZOOM GUARDRAILS ---
    if (newZoom < 0.8) newZoom = 0.8; 
    if (newZoom > 1.25) newZoom = 1.25; 

    // 4. ABORT IF AT LIMIT: If the math says we didn't actually change anything, stop here!
    if (newZoom === oldZoom) {
        return; // Exits the function completely. No refresh, no flickering.
    }

    // 5. If we made it here, it's a valid new zoom. Save and apply.
    localStorage.setItem(`vladhq_zoom_${panelId}`, newZoom);
    applyZoom(panelId, newZoom);
    
    // 6. Refresh targets
    const iframe = panel.querySelector('iframe');
    if (iframe) {
        iframe.src += ''; 
    }
    
    if(panelId === 'panel3' && typeof drawBellCurve === 'function') {
         setTimeout(() => drawBellCurve(parseFloat(document.getElementById('warVmriScore').innerText || 0)), 50);
    }
}

function applyZoom(panelId, zoomLevel) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    
    const content = panel.querySelector('.zoomable');
    if (!content) return;

    const inverseScale = (1 / zoomLevel) * 100;

    // --- IFRAME SPECIFIC LOGIC ---
    if (content.tagName.toLowerCase() === 'iframe') {
        // Clear any buggy CSS transforms
        content.style.transform = 'none';
        
        // Use native hardware zoom. This forces the internal Chart.js canvas 
        // to re-render at the new viewport size, eliminating empty space.
        content.style.zoom = zoomLevel;
        
        // Expand the bounding box so it stays anchored to the panel edges
        content.style.width = `${inverseScale}%`;
        content.style.height = `${inverseScale}%`;
        
    } 
    // --- NATIVE HTML DIV LOGIC ---
    else {
        content.style.transform = `scale(${zoomLevel})`;
        content.style.transformOrigin = 'top left';
        content.style.width = `${inverseScale}%`;
        
        // Only force height expansion if zooming OUT to prevent clipping
        if (zoomLevel < 1.0) {
            content.style.height = `${inverseScale}%`;
        } else {
            content.style.height = `100%`;
        }
    }
}

// ==========================================
// --- 5. DRAG & DROP / MODAL ENGINE ---
// ==========================================


let draggedPanel = null;

function toggleFullscreen(panelId) {
    const panel = document.getElementById(panelId);
    const backdrop = document.getElementById('modalBackdrop');
    const btn = panel.querySelector('.btn-maximize');
    const content = panel.querySelector('.zoomable');
    
    if (panel.classList.contains('fullscreen-mode')) {
        // --- MINIMIZE LOGIC ---
        panel.classList.remove('fullscreen-mode');
        backdrop.classList.add('hidden');
        btn.innerText = '[MAX]';
        panel.setAttribute('draggable', 'true');
        
        // Restore the user's custom zoom level
        let savedZoom = parseFloat(localStorage.getItem(`vladhq_zoom_${panelId}`) || 1.0);
        applyZoom(panelId, savedZoom);
        
    } else {
        // --- MAXIMIZE LOGIC ---
        panel.classList.add('fullscreen-mode');
        backdrop.classList.remove('hidden');
        btn.innerText = '[MIN]';
        panel.setAttribute('draggable', 'false');
        
        // Temporarily reset zoom to 1.0 so the chart fills the whole screen naturally
        if (content) {
            content.style.transform = 'none';
            content.style.zoom = 1.0;
            content.style.width = '100%';
            content.style.height = '100%';
        }
    }

    // Force redraws
    if (panelId === 'panel3') {
        setTimeout(() => {
            updateSliderLabels(); 
            drawBellCurve(parseFloat(document.getElementById('warVmriScore').innerText || 0));
        }, 50);
    }
}


document.addEventListener('DOMContentLoaded', () => {
    const panels = document.querySelectorAll('.draggable-panel');

    // Load Default Panels. `true` marks a load the page started itself: it is logged as info, not as a command the user typed.
    setTimeout(() => loadDarkPoolProfile(true), 2000);
    setTimeout(() => loadDealerMap(true), 1600);
    setTimeout(() => loadArbitrageLedger(), 1800);
    // Load Phase 1 Panels
    setTimeout(() => loadSlvInstitutionalFlow(), 2200);
    setTimeout(() => loadSlvGexMap(true), 2400);
    setTimeout(() => loadMacroCalendar(), 2600);
    setTimeout(() => loadMacroNews(), 2800);
    setTimeout(() => loadFedLiquidity(), 3000);
    setTimeout(() => loadShfeArbSpread(), 3200);
    setTimeout(() => loadPositions(), 3400);
    setInterval(loadPositions, 60000);

    // Add this to your DOMContentLoaded in app.js
    document.getElementById('modalBackdrop').addEventListener('click', () => {
        // Find whichever panel is currently in fullscreen mode and shrink it
        const activePanel = document.querySelector('.fullscreen-mode');
        if (activePanel) toggleFullscreen(activePanel.id);
    });
    
    panels.forEach(panel => {
        // When drag starts, apply styling
        panel.addEventListener('dragstart', function(e) {
            if (isMobileLayout()) {
                e.preventDefault();
                return;
            }
            draggedPanel = this;
            setTimeout(() => this.classList.add('panel-dragging'), 0);
        });

        // When drag ends, remove styling
        panel.addEventListener('dragend', function() {
            setTimeout(() => this.classList.remove('panel-dragging'), 0);
            draggedPanel = null;
        });

        // Prevent default to allow dropping
        panel.addEventListener('dragover', function(e) {
            e.preventDefault(); 
        });

        // Handle the Drop (Swap the physical HTML nodes in the DOM)
        panel.addEventListener('drop', function(e) {
            e.preventDefault();
            if (this !== draggedPanel && draggedPanel !== null) {
                let allPanels = [...document.querySelectorAll('.draggable-panel')];
                let draggedIndex = allPanels.indexOf(draggedPanel);
                let targetIndex = allPanels.indexOf(this);
                
                // CSS Grid respects DOM order, so we literally move the element
                if (draggedIndex < targetIndex) {
                    this.parentNode.insertBefore(draggedPanel, this.nextSibling);
                } else {
                    this.parentNode.insertBefore(draggedPanel, this);
                }
            }
        });
    });

    // SAFETY CATCH: Prevent War Room Sliders from triggering the Panel Drag
    document.querySelectorAll('input[type="range"], button, canvas').forEach(el => {
        el.addEventListener('mousedown', (e) => {
            const panel = e.target.closest('.draggable-panel');
            if(panel && !panel.classList.contains('fullscreen-mode')) panel.setAttribute('draggable', 'false');
        });
        el.addEventListener('mouseup', (e) => {
            const panel = e.target.closest('.draggable-panel');
            if(panel && !panel.classList.contains('fullscreen-mode')) panel.setAttribute('draggable', 'true');
        });
        el.addEventListener('mouseleave', (e) => {
            const panel = e.target.closest('.draggable-panel');
            if(panel && !panel.classList.contains('fullscreen-mode')) panel.setAttribute('draggable', 'true');
        });
    });
});

// Fires the API request and updates the UI Visuals
async function updateWarRoom() {
    const statusEl = document.getElementById('warStatus');
    statusEl.innerText = "CALCULATING...";
    statusEl.className = "hidden";

    const payload = {
        dxy_shift: parseFloat(document.getElementById('shiftDxy').value),
        tnx_shift: parseFloat(document.getElementById('shiftTnx').value),
        oas_shift: parseFloat(document.getElementById('shiftOas').value),
        vix_shift_pct: parseFloat(document.getElementById('shiftVix').value)
    };
    const isLive = Object.values(payload).every(v => v === 0);

    try {
        // a 503 means the ledger lacks one of the four inputs; the body names it and is shown in the panel
        const data = await fetchJson(`${API_BASE}/api/war_room`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (data.status !== 'success') throw new Error(data.message || 'calculation failed');

        warRoomData = data;
        currentBaseData = data.current; // Store base environment
        updateSliderLabels();

        const hypo = data.hypothetical, cur = data.current, tier = warTier(hypo.vmri);
        const set = (id, txt) => { document.getElementById(id).innerText = txt; };

        // 1. Score, tier, live reading and change
        const scoreDisplay = document.getElementById('warVmriScore');
        scoreDisplay.innerText = hypo.vmri.toFixed(1);
        scoreDisplay.className = `war-score ${tier.css}`;
        set('warScoreKind', isLive ? 'live' : 'scenario');
        const tierEl = document.getElementById('warTier');
        tierEl.innerText = tier.name;
        tierEl.className = `text-[9px] font-bold uppercase tracking-widest ${tier.css}`;
        tierEl.title = '';
        set('warLiveVmri', cur.vmri.toFixed(1));
        const deltaEl = document.getElementById('warDelta');
        deltaEl.innerText = isLive ? 'no scenario applied' : `Scenario ${warSigned(data.impact.vmri_delta)} (${warSigned(data.impact.vmri_delta_pct, 0)}%)`;
        deltaEl.className = isLive ? 'text-zinc-500' : (data.impact.vmri_delta > 0 ? 'text-red-400 font-bold' : 'text-green-400 font-bold');

        // 2. Tripwire bar: live marker and scenario marker on a 0–500 scale
        document.getElementById('warLiveMark').style.left = `${warScalePct(cur.vmri)}%`;
        const fill = document.getElementById('tripwireFill');
        fill.style.left = `${warScalePct(hypo.vmri)}%`;
        fill.style.background = tier.color;
        fill.style.boxShadow = `0 0 8px ${tier.color}`;
        fill.title = hypo.vmri > 500 ? `Scenario VMRI ${hypo.vmri.toFixed(1)} (off the scale)` : `Scenario VMRI ${hypo.vmri.toFixed(1)}`;

        // 3. The three factors of the formula
        const fac = (id, now, was, d, suffix) => {
            const changed = Math.abs(now - was) >= Math.pow(10, -d) / 2;
            document.getElementById(id).innerHTML = `${now.toFixed(d)}${suffix}` + (changed ? `<span class="block text-[8px] text-zinc-500 font-normal">was ${was.toFixed(d)}${suffix}</span>` : '');
        };
        fac('warFacBase', hypo.factors.macro_base, cur.factors.macro_base, 1, '');
        fac('warFacCredit', hypo.factors.credit, cur.factors.credit, 2, '×');
        fac('warFacVol', hypo.factors.vol, cur.factors.vol, 2, '×');

        // 4. What each lever does on its own
        const solo = data.impact.solo_delta || {};
        [['soloDxy', 'dxy', payload.dxy_shift], ['soloTnx', 'tnx', payload.tnx_shift], ['soloOas', 'oas', payload.oas_shift], ['soloVix', 'vix', payload.vix_shift_pct]].forEach(([id, k, s]) => {
            const el = document.getElementById(id);
            el.innerText = s === 0 ? '' : `alone ${warSigned(solo[k] || 0)}`;
            el.className = `war-solo ${(solo[k] || 0) > 0 ? 'up' : 'down'}`;
        });

        // 5. Header status, impact text, history strip
        statusEl.innerText = isLive ? "LIVE ENVIRONMENT" : tier.name;
        statusEl.className = "hidden";
        generateDeltaAnalysis(hypo);
        const hist = data.history;
        if (hist) {
            set('warHistTitle', `Recorded VMRI readings · ${hist.n} since ${hist.start || '—'}`);
            set('warHistNote', `Live is higher than ${hist.pct_below_current.toFixed(0)}% of them` +
                (isLive ? '' : `; this scenario is higher than ${hist.pct_below_hypothetical.toFixed(0)}%`) +
                `. Range ${hist.min}–${hist.max}, median ${hist.median}. Dashed line = live, solid = scenario.`);
            set('warPercentile', `${isLive ? 'Live' : 'Scenario'} > ${(isLive ? hist.pct_below_current : hist.pct_below_hypothetical).toFixed(0)}% of history`);
            drawBellCurve();
        } else {
            set('warHistNote', 'Not enough recorded readings yet for a history strip.');
            set('warPercentile', '');
        }
        if (document.getElementById('warGuide')) renderWarGuide();
    } catch (e) {
        statusEl.innerText = "API ERROR";
        const tierEl = document.getElementById('warTier');
        tierEl.innerText = `API ERROR: ${e.message}`;
        tierEl.className = "text-[9px] font-bold tracking-wide text-red-500 normal-case";
        tierEl.title = e.message;
        // nothing valid to show: do not leave the numbers of an earlier answer next to the error
        if (!warRoomData) {
            document.getElementById('warVmriScore').innerText = DASH;
            document.getElementById('warLiveVmri').innerText = DASH;
            document.getElementById('warDelta').innerText = 'no scenario available';
        }
    }
}

// Injects extreme historical variables into the sliders
function applyWarRoomPreset(scenario) {
    const target = WAR_PRESETS[scenario];
    if (!target) return;
    document.getElementById('shiftDxy').value = target.dxy;
    document.getElementById('shiftTnx').value = target.tnx;
    document.getElementById('shiftOas').value = target.oas;
    document.getElementById('shiftVix').value = target.vix;
    document.querySelectorAll('#panel3 [data-preset]').forEach(b => b.classList.toggle('active', b.dataset.preset === scenario));
    const note = document.getElementById('warPresetNote');
    note.innerHTML = `<b class="text-zinc-300">${target.title}.</b> ${target.note} Shifts are added to today's readings.`;
    note.classList.remove('hidden');
    updateSliderLabels();
    updateWarRoom();
    log(`WAR ROOM: Injected [${scenario}] Scenario Profile`, 'cmd');
}

function clearWarPreset() {
    document.querySelectorAll('#panel3 [data-preset]').forEach(b => b.classList.remove('active'));
    document.getElementById('warPresetNote').classList.add('hidden');
}

function resetWarRoom() {
    ['shiftDxy', 'shiftTnx', 'shiftOas', 'shiftVix'].forEach(id => { document.getElementById(id).value = 0; });
    clearWarPreset();
    updateSliderLabels();
    updateWarRoom();
    log('WAR ROOM: Reset to Live Macro Environment', 'info');
}

// --- INITIALIZATION (HOOKING IT ALL UP) ---
document.addEventListener('DOMContentLoaded', () => {
    ['shiftDxy', 'shiftTnx', 'shiftOas', 'shiftVix'].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('input', () => {
            clearWarPreset();
            updateSliderLabels();
            // DEBOUNCE: wait until dragging pauses before hitting the API
            clearTimeout(warRoomDebounceTimer);
            warRoomDebounceTimer = setTimeout(updateWarRoom, 120);
        });
    });
    window.addEventListener('resize', drawBellCurve);
    // Run the engine once on load to show current live data
    if (document.getElementById('shiftDxy')) updateWarRoom();
});

function reloadFrame(id) {
    const frame = document.getElementById(id);
    if (frame) frame.src += '';
}

function refreshFrame(id) { 
    reloadFrame(id);
    log(`Refreshed ${id}`, 'cmd'); 
}

// Data Dump (GET /dump): prints the newest tactical and volume XML and keeps the text for the COPY button of panels 1 and 2.
async function triggerAction(endpoint) {
    log(`System Command: ${endpoint}`, 'cmd');
    try { 
        const res = await fetch(`${API_BASE}${endpoint}`); 
        const t = await res.text();
        if (res.ok) window._lastXmlDump = t; // Cache for copy-data on panel1/panel2
        const err = res.ok ? null : xmlErrorText(t);
        log(err !== null ? (err || `HTTP ${res.status}`) : t, res.ok ? 'success' : 'error');
    } catch (e) { log(e.message, 'error'); }
}

// ==========================================
// --- RE-SCAN ALL DATA: start a pipeline run on the server and follow it ---
// ==========================================
// POST /run answers at once (202 started, 409 busy). The run itself takes minutes, so the page reads GET /api/run_status
// until a run_id it has not seen before shows up with lock_held false, then prints how it ended.
const RUN_POLL_MS = 5000;                   // how often /api/run_status is read
const RUN_START_WAIT_MS = 90 * 1000;        // a new run_id must appear within this time
const RUN_MAX_WAIT_MS = 45 * 60 * 1000;     // stop following after this long
let _rescanActive = false;
const _sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function setRescanButton(active) {
    const btn = document.getElementById('btnRescan'), txt = document.getElementById('rescanBtnText');
    if (btn) { btn.disabled = active; btn.classList.toggle('opacity-50', active); btn.classList.toggle('cursor-wait', active); }
    if (txt) txt.innerText = active ? 'RE-SCANNING...' : 'RE-SCAN ALL DATA';
}

async function readRunStatus() {
    const body = await fetchJson(`${API_BASE}/api/run_status`);
    if (body.status !== 'success' || !body.data) throw new Error(body.message || 'run status unavailable');
    return body.data;
}

async function rescanAll() {
    if (_rescanActive) { log('A re-scan is already being followed in this window.', 'info'); return; }
    _rescanActive = true;
    setRescanButton(true);
    log('System Command: RE-SCAN ALL DATA (starts a pipeline run on the server)', 'cmd');
    try {
        let beforeId = null;
        try { beforeId = (await readRunStatus()).run_id || null; }
        catch (e) { log(`Run status not readable before the start (${e.message}); starting anyway.`, 'info'); }

        const started = await fetchJson(`${API_BASE}/run`, { method: 'POST' });
        if (started.status === 'busy' || started.http === 409) {
            const where = started.run_id ? ` (run ${started.run_id}${started.stage ? ', stage ' + started.stage : ''})` : (started.stage ? ` (${started.stage})` : '');
            const why = started.message && !/^HTTP \d/.test(started.message) ? started.message : 'a run is already in progress';
            log(`busy: ${why}${where}. Nothing was started.`, 'info');
            return;
        }
        if (started.status !== 'started') {
            log(`The run did not start: ${started.message || 'HTTP ' + started.http}`, 'error');
            return;
        }
        log(`Run started on the server (pid ${started.pid ?? '?'}). Checking progress every ${RUN_POLL_MS / 1000} s...`, 'info');
        await followRun(beforeId);
    } catch (e) {
        log(`Re-scan failed: ${e.message}`, 'error');
    } finally {
        _rescanActive = false;
        setRescanButton(false);
    }
}

// Polls until the new run has ended (resolves), or gives up with a message. Deadlines use the clock, not a count of polls,
// because a hidden browser tab slows its timers down.
async function followRun(beforeId) {
    const t0 = Date.now();
    let runId = null, lastStage = null, lastPollError = null;
    for (;;) {
        await _sleep(RUN_POLL_MS);
        let st = null;
        try {
            st = await readRunStatus();
            if (lastPollError) { log('Run status is readable again.', 'info'); lastPollError = null; }
        } catch (e) {
            if (e.message !== lastPollError) { lastPollError = e.message; log(`Run status check failed (${e.message}); still trying.`, 'info'); }
        }
        if (st) {
            if (st.run_id && st.run_id !== beforeId && st.run_id !== runId) {
                runId = st.run_id;
                lastStage = st.stage || null;
                log(`Run ${runId} is under way${st.stage ? ` (stage ${st.stage})` : ''}.`, 'info');
            } else if (runId && st.run_id === runId && st.stage && st.stage !== lastStage) {
                lastStage = st.stage;
                log(`Run ${runId}: stage ${st.stage}`, 'info');
            }
            if (runId && st.run_id === runId && st.lock_held === false) {
                const state = st.state || 'unknown';
                const took = isNum(st.elapsed_s) ? ` after ${Math.round(st.elapsed_s)} s` : '';
                const why = st.error ? `: ${st.error}` : '';
                if (state === 'completed') log(`Run ${runId} completed${took}.`, 'success');
                else if (state === 'completed_with_warnings') log(`Run ${runId} completed with warnings${took}${why}. Run "main_pipeline.py status" on the server for the detail.`, 'warn');
                else if (state === 'interrupted') log(`Run ${runId} was interrupted: the process ended before it finished${why}`, 'error');
                else log(`Run ${runId} ${state}${took}${why}`, 'error');
                reloadFrame('vmriFrame');
                reloadFrame('comexFrame');
                log('VMRI and COMEX inventory frames reloaded.', 'info');
                return;
            }
        }
        const waited = Date.now() - t0;
        if (!runId && waited > RUN_START_WAIT_MS) {
            log(`No new run appeared within ${RUN_START_WAIT_MS / 1000} s, so it probably did not start. The launcher output is in .v2_manual_run.log in the server's data folder.`, 'error');
            return;
        }
        if (waited > RUN_MAX_WAIT_MS) {
            log(`Stopped waiting after ${RUN_MAX_WAIT_MS / 60000} minutes; run ${runId} may still be going. Check "main_pipeline.py status" on the server.`, 'warn');
            return;
        }
    }
}

// ==========================================
// --- LAYOUT ENGINE: panels / console split (single owner) ---
// ==========================================
// Wide layout (768 px and up). Expanded: the active tab gets a fixed height (user-draggable), the console fills the rest.
// Minimized: the tab fills everything, the console shrinks to its title bar. Applies to every .tab-content.
// Both layouts also have MAXIMIZED: the console covers the whole viewport (body.console-maximized). That is never stored.
// Phone layout (below 768 px): MINIMIZED = the tab bar, the active tab and a docked console bar; OPEN = the console fills the area
// between the header and the bottom bar and the tab bar and tabs are hidden; MAXIMIZED = the console covers everything. Not stored either:
// a phone always starts MINIMIZED. The state is applied by applyConsoleChrome() (body classes, button labels, bottom bar).
let isConsoleMinimized = localStorage.getItem('vladhq_console_min') === '1';
let consoleMaximized = false;        // both layouts
let phoneConsoleOpen = false;        // phone layout: false = MINIMIZED (docked bar), true = OPEN
let savedTopHeight = parseFloat(localStorage.getItem('vladhq_panel_height')) || 550;
const CONSOLE_MIN_OPEN = 150;   // smallest console height when expanded
const PANELS_MIN = 200;         // smallest panel area height

// "Phone layout" = below Tailwind's md breakpoint (768 px), which the page's layout classes (md:...) use. styles.css has the same
// number in its mobile media query; at exactly 768 px both agree that the wide layout applies.
const MOBILE_QUERY = '(max-width: 767.98px)';
const isMobileLayout = () => !!(window.matchMedia && window.matchMedia(MOBILE_QUERY).matches);
const tabContents = () => document.querySelectorAll('.tab-content');
const activeTab = () => [...tabContents()].find(t => !t.classList.contains('hidden')) || document.getElementById('macro-tab');

function clampTopHeight(h) {
    const top = activeTab()?.getBoundingClientRect().top || 96;
    const max = window.innerHeight - top - CONSOLE_MIN_OPEN;
    return Math.max(PANELS_MIN, Math.min(h, max));
}

function applyLayout() {
    const consoleSection = document.getElementById('consoleSection');
    const consoleBody = document.getElementById('consoleBody');
    const resizer = document.getElementById('v-resizer');
    if (!consoleSection) return;
    if (isMobileLayout() && consoleMaximized) phoneConsoleOpen = true;     // what a phone returns to on [Restore]
    applyConsoleChrome();
    if (isMobileLayout()) {                     // a phone has the docked bar and the bottom bar instead of a split
        tabContents().forEach(t => { t.style.height = ''; t.style.flex = ''; t.style.minHeight = ''; });
        consoleSection.style.flex = ''; consoleSection.style.height = ''; consoleSection.style.minHeight = '';
        consoleSection.classList.remove('console-minimized');
        consoleBody?.classList.remove('hidden');
        resizer?.classList.add('hidden');       // the splitter belongs to the wide layout (the wide branch below sets it again)
        resizer?.classList.remove('md:flex');
        return;
    }
    if (consoleMaximized) {                     // the console covers the page; the split underneath is recomputed when it is restored
        consoleBody?.classList.remove('hidden');
        consoleSection.classList.remove('console-minimized');
        return;
    }
    if (isConsoleMinimized) {
        tabContents().forEach(t => { t.style.height = ''; t.style.flex = '1 1 0%'; t.style.minHeight = '0'; });
        consoleBody?.classList.add('hidden');
        resizer?.classList.add('hidden');
        resizer?.classList.remove('md:flex');
        consoleSection.style.flex = '0 0 auto';
        consoleSection.style.minHeight = '0';
        consoleSection.classList.add('console-minimized');
    } else {
        const h = clampTopHeight(savedTopHeight);
        tabContents().forEach(t => { t.style.height = `${h}px`; t.style.flex = 'none'; t.style.minHeight = ''; });
        consoleBody?.classList.remove('hidden');
        resizer?.classList.remove('hidden');
        resizer?.classList.add('md:flex');
        consoleSection.style.flex = '1 1 0%';
        consoleSection.style.minHeight = `${CONSOLE_MIN_OPEN - 40}px`;
        consoleSection.classList.remove('console-minimized');
    }
    if (typeof drawBellCurve === 'function' && document.getElementById('warVmriScore')) {
        drawBellCurve(parseFloat(document.getElementById('warVmriScore').innerText || 0));
    }
    // charts inside panels need a nudge after a size change
    window.dispatchEvent(new Event('resize-panels'));
}

// kept for existing callers
function updateLayoutHeights(newTopHeight) {
    savedTopHeight = clampTopHeight(parseFloat(newTopHeight));
    localStorage.setItem('vladhq_panel_height', String(Math.round(savedTopHeight)));
    applyLayout();
}

document.addEventListener('DOMContentLoaded', () => {
    const resizer = document.getElementById('v-resizer');

    // Restore individual panel zoom levels from memory
    document.querySelectorAll('.draggable-panel').forEach(panel => {
        let savedZoom = localStorage.getItem(`vladhq_zoom_${panel.id}`);
        if (savedZoom) applyZoom(panel.id, parseFloat(savedZoom));
    });

    applyLayout();

    let isResizing = false;
    let shield = document.getElementById('resize-shield') || document.createElement('div');
    if (!shield.id) {
        shield.id = 'resize-shield';
        document.body.appendChild(shield);
    }
    resizer?.addEventListener('mousedown', (e) => {
        if (isConsoleMinimized || isMobileLayout()) return;
        e.preventDefault();
        isResizing = true;
        document.body.classList.add('resizing-active');
        shield.style.display = 'block';
    });
    window.addEventListener('mousemove', (e) => {
        if (!isResizing) return;
        const top = activeTab().getBoundingClientRect().top;      // measure from where the panels really start
        updateLayoutHeights(e.clientY - top);
    });
    window.addEventListener('mouseup', () => {
        if (!isResizing) return;
        isResizing = false;
        document.body.classList.remove('resizing-active');
        shield.style.display = 'none';
    });
    let resizeTimer = null;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(applyLayout, 80);
    });
    // Crossing 768 px (a phone turned sideways, a window dragged narrower) re-applies the layout at once: no docked bar on the wide
    // layout, no split on a phone. A maximized console stays maximized; its [Restore] button and Esc work in both layouts.
    const phoneMq = window.matchMedia && window.matchMedia(MOBILE_QUERY);
    if (phoneMq) {
        if (phoneMq.addEventListener) phoneMq.addEventListener('change', applyLayout);
        else if (phoneMq.addListener) phoneMq.addListener(applyLayout);      // Safari before 14
    }
});

// --- CONSOLE STATES ---
// What the state looks like is in styles.css (body.mobile-console-view, body.console-maximized, #consoleDock) and in docs/terminal.md.
function consoleStateName() {
    if (consoleMaximized) return 'maximized';
    if (isMobileLayout()) return phoneConsoleOpen ? 'open' : 'minimized';
    return isConsoleMinimized ? 'collapsed' : 'split';
}

// Puts the current state on the page: body classes, the labels of the title-row buttons and which bottom-bar button is lit.
function applyConsoleChrome() {
    const phone = isMobileLayout();
    const body = document.body;
    body.classList.toggle('mobile-console-view', phone && (phoneConsoleOpen || consoleMaximized));
    body.classList.toggle('console-maximized', consoleMaximized);
    body.dataset.consoleState = consoleStateName();

    const minBtn = document.getElementById('btnMinConsole');
    const maxBtn = document.getElementById('btnMaxConsole');
    if (minBtn) minBtn.innerText = (!phone && isConsoleMinimized) ? '[Expand]' : '[Minimize]';
    if (maxBtn) {
        maxBtn.innerText = consoleMaximized ? '[Restore]' : '[Maximize]';
        maxBtn.setAttribute('aria-pressed', consoleMaximized ? 'true' : 'false');
    }

    const consoleView = phone && (phoneConsoleOpen || consoleMaximized);
    const on = ['text-white', 'border-blue-500', 'bg-zinc-900/50'];
    const off = ['text-zinc-500', 'border-transparent'];
    [[document.getElementById('tabConsole'), consoleView], [document.getElementById('tabPanels'), !consoleView]].forEach(([btn, active]) => {
        if (!btn) return;
        btn.classList.remove(...(active ? off : on));
        btn.classList.add(...(active ? on : off));
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
}

function scrollConsoleToNewest() {
    const logEl = document.getElementById('consoleLog');
    if (logEl) logEl.scrollTop = logEl.scrollHeight;                         // lines written while it was hidden: show the newest
}

// Phone: MINIMIZED <-> OPEN. switchMobileTab() is the entry point used by the bottom bar, the docked bar, log() and switchTab():
// 'console' opens the console (a maximized console stays maximized), 'panels' goes back to MINIMIZED (also leaves MAXIMIZED).
// It does nothing on the wide layout, which has the split.
function switchMobileTab(tab) {
    if (!isMobileLayout()) return;
    if (tab === 'console') {
        phoneConsoleOpen = true;
    } else {
        phoneConsoleOpen = false;
        consoleMaximized = false;
    }
    applyLayout();
    if (phoneConsoleOpen) scrollConsoleToNewest();
}

// [Minimize] / [Expand]. Wide layout: the split, remembered in this browser (vladhq_console_min). Phone: back to MINIMIZED (Panels).
function toggleConsole() {
    if (isMobileLayout()) { switchMobileTab('panels'); return; }
    if (consoleMaximized) return;                                          // there is no split to minimize while it covers the page
    isConsoleMinimized = !isConsoleMinimized;
    try { localStorage.setItem('vladhq_console_min', isConsoleMinimized ? '1' : '0'); } catch (e) { /* storage blocked: not remembered */ }
    applyLayout();
    if (!isConsoleMinimized) scrollConsoleToNewest();
}

// [Maximize] / [Restore], both layouts. Not remembered. Restoring goes back to OPEN on a phone, and to the split (or the title bar,
// as it was) on the wide layout.
function setConsoleMaximized(on) {
    on = !!on;
    if (on === consoleMaximized) return;
    consoleMaximized = on;
    applyLayout();
    scrollConsoleToNewest();
}
function toggleConsoleMaximized() { setConsoleMaximized(!consoleMaximized); }

// The title row of the console: on the wide layout a click on it minimizes or expands, as it always did; on a phone only the buttons act.
function onConsoleTitleClick() { if (!isMobileLayout()) toggleConsole(); }

// --- MENU DRAWER (every screen width) ---
// The Macro Triggers and the Custom Whale Hunter form live in #sideDrawer, off canvas on the left. #menuBtn opens it. It closes with the
// x, a tap on the backdrop, Esc, or any button pressed inside it. Focus moves into it when it opens and back to #menuBtn when it
// closes. It is always closed when the page loads: nothing about it is stored.
const drawerIsOpen = () => document.body.classList.contains('drawer-open');

function openDrawer() {
    if (drawerIsOpen()) return;
    document.body.classList.add('drawer-open');                            // styles.css: slides the drawer in, shows the backdrop, locks the page
    document.getElementById('menuBtn')?.setAttribute('aria-expanded', 'true');
    document.getElementById('drawerClose')?.focus({ preventScroll: true });
}

function closeDrawer() {
    if (!drawerIsOpen()) return;
    document.body.classList.remove('drawer-open');
    const btn = document.getElementById('menuBtn');
    btn?.setAttribute('aria-expanded', 'false');
    btn?.focus({ preventScroll: true });
}

function toggleDrawer() { if (drawerIsOpen()) closeDrawer(); else openDrawer(); }

try { localStorage.removeItem('vladhq_sidebar_open'); } catch (e) { /* the old phone bar remembered its state under this key; the drawer does not */ }

// Pressing any action in the drawer (also one that is refused, such as a second re-scan, which only logs an info line) closes it. On a phone
// it then shows the console, where the messages of that action are; on the wide layout the console is already on screen.
document.getElementById('drawerBody')?.addEventListener('click', e => {
    if (!e.target.closest('button')) return;
    closeDrawer();
    if (isMobileLayout()) switchMobileTab('console');
});

// Esc: closes the drawer first; otherwise restores a maximized console. (The other Esc handlers act on their own dialogs.)
document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (drawerIsOpen()) { closeDrawer(); return; }
    if (consoleMaximized) setConsoleMaximized(false);
});

// Tab stays inside the open drawer (it is modal)
document.addEventListener('keydown', e => {
    if (e.key !== 'Tab' || !drawerIsOpen()) return;
    const drawer = document.getElementById('sideDrawer');
    const items = [...drawer.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
        .filter(el => el.getClientRects().length > 0);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (!drawer.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

// Fetches the live XML dump and extracts the Paper:Physical ratio
async function syncComexModule() {
    try {
        const res = await fetch(`${API_BASE}/api/dump`);
        const xmlText = await res.text();
        
        const parser = new DOMParser();
        const xmlDoc = parser.parseFromString(xmlText, "text/xml");
        
        // Find the node in the XML
        const ratioNode = xmlDoc.querySelector("comex_default_risk leverage_ratio");
        
        if (ratioNode) {
            // The XML outputs "7.17:1", so we split by ':' to grab just the number
            const ratioStr = ratioNode.textContent.split(':')[0]; 
            const ratio = parseFloat(ratioStr);
            
            // Push the live data to the UI (a ratio that did not parse is left out, not drawn as NaN)
            if (Number.isFinite(ratio)) updateComexRatioUI(ratio);
        }
    } catch (e) { 
        console.error("Failed to sync COMEX module:", e); 
    }
}

// Updates the COMEX Paper:Physical Module UI based on the current ratio
function updateComexRatioUI(ratio) {
    const display = document.getElementById('livePaperRatio');
    const banner = document.getElementById('liveRatioStatusBanner');
    const claims = document.getElementById('livePaperOz');
    const statusIcon = document.getElementById('ratioStatusIcon');
    const statusText = document.getElementById('ratioStatusText');
    const needle = document.getElementById('ratioNeedle'); // Grab the needle

    if (!display) return;

    // 1. Update text numbers
    display.innerText = `${ratio.toFixed(0)}:1`;
    claims.innerText = ratio.toFixed(0);

    // 2. Animate the Gauge Needle
    // Math: Maps a 0 to 50 ratio scale to a -90 to +90 degree angle.
    let degrees = -90 + (ratio / 50) * 180;
    if (degrees > 90) degrees = 90;   // Cap it at far right
    if (degrees < -90) degrees = -90; // Cap it at far left
    
    if (needle) {
        needle.style.transform = `rotate(${degrees}deg)`;
    }

    // 3. Dynamic Threat Tiering Colors
    if (ratio >= 40) {
        display.className = "text-4xl font-bold tracking-tighter text-red-500 drop-shadow-[0_0_12px_rgba(239,68,68,0.4)]";
        banner.className = "text-[9px] uppercase font-bold text-red-500 mt-1 tracking-widest";
        banner.innerText = "CRITICAL LEVERAGE";
        statusIcon.className = "text-red-500 mt-[1px]";
        statusText.innerHTML = "<span class='text-red-400 font-bold'>Critical ratio level</span>";
    } else if (ratio >= 25) {
        display.className = "text-4xl font-bold tracking-tighter text-yellow-500 drop-shadow-[0_0_12px_rgba(234,179,8,0.3)]";
        banner.className = "text-[9px] uppercase font-bold text-yellow-500 mt-1 tracking-widest";
        banner.innerText = "DELIVERY STRESS";
        statusIcon.className = "text-yellow-500 mt-[1px]";
        statusText.innerHTML = "<span class='text-yellow-400 font-bold'>Elevated ratio level</span>";
    } else {
        display.className = "text-4xl font-bold tracking-tighter text-white drop-shadow-[0_0_8px_rgba(255,255,255,0.2)]";
        banner.className = "text-[9px] uppercase font-bold text-green-500 mt-1 tracking-widest";
        banner.innerText = "MARKET NOMINAL";
        statusIcon.className = "text-green-500 mt-[1px]";
        statusText.innerHTML = "<span class='text-green-400'>Standard leverage level</span>";
    }
}


// Unified Initialization
document.addEventListener('DOMContentLoaded', () => {
    // Fetch the live ratio immediately on load, then every 5 minutes
    syncComexModule();
    setInterval(syncComexModule, 300000);
});

// Initialize UI: API status (every 30 s) and the clock. Started once, here.
setInterval(checkStatus, 30000); 
checkStatus();
setInterval(() => {
    const clock = document.getElementById('clock');
    if(clock) clock.innerText = new Date().toLocaleTimeString();
}, 1000);

// ==========================================
// --- DEALER POSITIONING (GEX) ENGINE ---
// ==========================================

let gexChartInstance = null;

// Fills the four stat boxes of a Dealer Map panel. Zero gamma is null (shown as a dash, with the reason as the
// tooltip) when the server could not compute it; it is never replaced by the spot price.
function setGexStats(ids, data) {
    const set = (id, v) => { const el = document.getElementById(id); el.innerText = fmt(v, 2, '$'); el.title = ''; return el; };
    set(ids.spot, data.spot);
    const zero = set(ids.zero, data.zeroGamma);
    if (!isNum(data.zeroGamma)) zero.title = data.zeroGammaReason || 'zero gamma could not be computed';
    set(ids.call, data.callWall);
    set(ids.put, data.putWall);
}

async function loadDealerMap(auto = false) {
    const ticker = document.getElementById('gexTickerInput').value.trim().toUpperCase() || 'SPY';
    document.getElementById('gexTickerInput').value = ticker;
    
    log(`Scanning Dealer Options chain for ${ticker}...`, auto ? 'info' : 'cmd');

    try {
        // --- REAL DATA PIPELINE ---
        const json = await fetchJson(`${API_BASE}/api/gex?ticker=${encodeURIComponent(ticker)}`);
        
        if (json.status !== 'success' || !json.data) {
            log(`GEX Error: ${json.message || 'no data'}`, 'error');
            return;
        }
        
        const data = json.data;
        window._lastGexData = { ticker, ...data }; // Cache for copy-data feature
        
        // --- UPDATE UI STATS ---
        setGexStats({ spot: 'gexSpot', zero: 'gexZero', call: 'gexCallWall', put: 'gexPutWall' }, data);

        // --- RENDER CHART ---
        const ctx = document.getElementById('gexChart').getContext('2d');
        
        // Destroy existing chart if user scans a new ticker
        if (gexChartInstance) {
            gexChartInstance.destroy();
        }

        // Separate data into positive (calls) and negative (puts) for coloring
        const backgroundColors = data.gamma.map(val => 
            val >= 0 ? 'rgba(34, 197, 94, 0.8)' : 'rgba(239, 68, 68, 0.8)'
        );
        const borderColors = data.gamma.map(val => 
            val >= 0 ? 'rgb(34, 197, 94)' : 'rgb(239, 68, 68)'
        );

        gexChartInstance = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: data.strikes,
                datasets: [{
                    label: 'Net Dealer Gamma',
                    data: data.gamma,
                    backgroundColor: backgroundColors,
                    borderColor: borderColors,
                    borderWidth: 1,
                    borderRadius: 2
                }]
            },
            options: {
                devicePixelRatio: 3,
                responsive: true,
                maintainAspectRatio: false, // CRITICAL for zoom/maximize panel scaling
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                        callbacks: {
                            label: function(context) {
                                return `Net Gamma: ${formatCompact(context.raw)}`;
                            }
                        }
                    },
                    // Draw a vertical line for Spot Price
                    annotation: {
                        annotations: {
                            line1: {
                                type: 'line',
                                xMin: data.strikes.indexOf(data.spot),
                                xMax: data.strikes.indexOf(data.spot),
                                borderColor: 'white',
                                borderWidth: 2,
                                borderDash: [5, 5],
                                label: { content: 'SPOT', display: true, position: 'top', color: 'white', backgroundColor: '#000' }
                            }
                        }
                    }
                },
                scales: {
                    y: {
                        grid: { color: 'rgba(255, 255, 255, 0.1)' },
                        ticks: { color: '#a1a1aa', font: { size: 9 }, callback: (v) => formatCompact(v) }
                    },
                    x: {
                        grid: { display: false },
                        ticks: { color: '#a1a1aa', font: { size: 9 }, maxTicksLimit: 15 }
                    }
                }
            },
            plugins: [{
                // Custom plugin to draw the Spot Line without needing extra Chart.js annotation libraries
                id: 'spotLinePlugin',
                afterDraw: (chart) => {
                    const xAxis = chart.scales.x;
                    const yAxis = chart.scales.y;
                    const ctx = chart.ctx;
                    
                    // Find closest strike to spot
                    let closestIdx = 0;
                    let minDiff = Infinity;
                    data.strikes.forEach((strike, i) => {
                        let diff = Math.abs(strike - data.spot);
                        if (diff < minDiff) { minDiff = diff; closestIdx = i; }
                    });

                    const xPixel = xAxis.getPixelForValue(closestIdx);
                    
                    ctx.save();
                    ctx.beginPath();
                    ctx.setLineDash([5, 5]);
                    ctx.moveTo(xPixel, yAxis.top);
                    ctx.lineTo(xPixel, yAxis.bottom);
                    ctx.lineWidth = 1.5;
                    ctx.strokeStyle = 'rgba(255, 255, 255, 0.8)';
                    ctx.stroke();
                    
                    ctx.fillStyle = '#fff';
                    ctx.font = 'bold 9px JetBrains Mono';
                    ctx.fillText('SPOT', xPixel + 4, yAxis.top + 10);
                    ctx.restore();
                }
            }]
        });

        log(`GEX Profile loaded successfully.`, 'success');
        
    } catch (e) {
        log(`Failed to load GEX map: ${e.message}`, 'error');
    }
}

// ==========================================
// --- PHYSICAL ARBITRAGE LEDGER ENGINE ---
// ==========================================

let arbChartInstance = null;
window.arbData = null; // Global store to prevent re-fetching on toggle

// 1. Fetcher
async function loadArbitrageLedger() {
    try {
        const json = await fetchJson(`${API_BASE}/api/arbitrage_history?limit=50`);
        
        if (json.status === 'success') {
            window.arbData = json.data;
            renderArbChart('price'); // Boot up in 'Spot vs Physical' view
            log('Physical Arbitrage Ledger loaded.', 'success');
        } else {
            log(`Arb Ledger Error: ${json.message}`, 'error');
        }
    } catch (e) {
        log(`Arb Ledger Connection Failed: ${e.message}`, 'error');
    }
}

// 2. Chart Renderer & Toggle Logic
function renderArbChart(view) {
    if (!window.arbData) return;
    const data = window.arbData;

    // Manage button active states
    document.querySelectorAll('.arb-toggle').forEach(btn => btn.classList.remove('active'));
    if (view === 'price') document.getElementById('btnArbPrice').classList.add('active');
    if (view === 'pct') document.getElementById('btnArbPct').classList.add('active');
    if (view === 'dollar') document.getElementById('btnArbDollar').classList.add('active');

    const ctx = document.getElementById('arbChart').getContext('2d');
    
    // Destroy the old canvas instance before drawing the new one
    if (arbChartInstance) {
        arbChartInstance.destroy();
    }

    let datasets = [];
    let yAxisFormat = '';

    // Swap datasets based on user selection
    if (view === 'price') {
        yAxisFormat = '$';
        datasets = [
            {
                label: 'Cheapest Eagle',
                data: data.cheapest_price,
                borderColor: '#38bdf8', // Light Blue
                backgroundColor: 'rgba(56, 189, 248, 0.1)',
                borderWidth: 2,
                fill: true,
                tension: 0.3,
                pointRadius: 0,
                pointHoverRadius: 4
            },
            {
                label: 'Avg Eagle',
                data: data.avg_price,
                borderColor: '#818cf8', // Indigo
                borderWidth: 1.5,
                borderDash: [2, 2],
                tension: 0.3,
                pointRadius: 0
            },
            {
                label: 'COMEX Spot',
                data: data.spot,
                borderColor: '#ffffff', // White dotted
                borderWidth: 2,
                borderDash: [5, 5],
                tension: 0.3,
                pointRadius: 0
            }
        ];
    } else if (view === 'pct') {
        yAxisFormat = '%';
        datasets = [
            {
                label: 'Cheapest Prem %',
                data: data.cheapest_pct,
                borderColor: '#22c55e', // Neon Green
                backgroundColor: 'rgba(34, 197, 94, 0.1)',
                borderWidth: 2,
                fill: true,
                tension: 0.3,
                pointRadius: 0,
                pointHoverRadius: 4
            },
            {
                label: 'Avg Prem %',
                data: data.avg_pct,
                borderColor: '#f97316', // Orange
                borderWidth: 1.5,
                tension: 0.3,
                pointRadius: 0
            }
        ];
    } else if (view === 'dollar') {
        yAxisFormat = '$';
        datasets = [
            {
                label: 'Cheapest Prem $',
                data: data.cheapest_dollar,
                borderColor: '#eab308', // Neon Yellow
                backgroundColor: 'rgba(234, 179, 8, 0.1)',
                borderWidth: 2,
                fill: true,
                tension: 0.3,
                pointRadius: 0,
                pointHoverRadius: 4
            }
        ];
    }

    // Initialize Chart
    arbChartInstance = new Chart(ctx, {
        type: 'line',
        data: {
            labels: data.labels,
            datasets: datasets
        },
        options: {
            devicePixelRatio: 3,
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: 'index',
                intersect: false,
            },
            plugins: {
                legend: {
                    labels: { color: '#a1a1aa', boxWidth: 10, font: {size: 9} }
                },
                tooltip: {
                    backgroundColor: 'rgba(9, 9, 11, 0.95)',
                    titleColor: '#fff',
                    bodyColor: '#a1a1aa',
                    borderColor: '#27272a',
                    borderWidth: 1,
                    padding: 10,
                    callbacks: {
                        label: function(context) {
                            let label = context.dataset.label || '';
                            if (label) label += ': ';
                            if (context.parsed.y !== null) {
                                label += yAxisFormat === '$' ? '$' + context.parsed.y.toFixed(2) : context.parsed.y.toFixed(2) + '%';
                            }
                            return label;
                        },
                        // High-Density Injection: Appends all core data to the bottom of the tooltip
                        afterBody: function(context) {
                            let idx = context[0].dataIndex;
                            return `\n-- TAPE --\nSpot: $${data.spot[idx]?.toFixed(2) || 'N/A'}\nEagle: $${data.cheapest_price[idx]?.toFixed(2) || 'N/A'}\nSpread: ${data.cheapest_pct[idx]?.toFixed(2) || 'N/A'}%`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { color: '#a1a1aa', font: { size: 9 }, maxTicksLimit: 6 }
                },
                y: {
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    ticks: { 
                        color: '#a1a1aa', 
                        font: { size: 9 },
                        callback: function(value) {
                            return yAxisFormat === '$' ? '$' + value : value + '%';
                        }
                    }
                }
            }
        }
    });
}

// ==========================================
// --- DARK POOL PROFILE ENGINE ---
// ==========================================

// How the Bias was worked out, as the server reports it in sentiment.method
const DP_METHODS = {
    aggressor: { label: 'AGGRESSOR SIDE', tip: 'Bias from the side that started each block trade, as the exchange feed reports it (buy aggressor = bullish, sell aggressor = bearish). Prints with no side are UNKNOWN and carry no weight.' },
    vwap_heuristic: { label: 'VWAP HEURISTIC', tip: 'The feed gave no trade sides, so the bias is a guess from where the blocks printed against the VWAP.' },
};

async function loadDarkPoolProfile(auto = false) {
    const inputEl = document.getElementById('dpTickerInput');
    const ticker = inputEl.value.trim().toUpperCase() || 'SLV';
    inputEl.value = ticker; // Auto-format to uppercase
    
    log(`Intercepting Dark Pool prints for $${ticker}...`, auto ? 'info' : 'cmd');

    try {
        // Hit the existing Python API endpoint
        const json = await fetchJson(`${API_BASE}/api/darkpool?ticker=${encodeURIComponent(ticker)}`);
        
        if (json.status !== 'success' || !json.data) {
            log(`Dark Pool Error: ${json.message || 'No data found'}`, 'error');
            return;
        }
        
        const data = json.data;
        const sentiment = data.sentiment || {};

        // --- 1. POPULATE THE DASHBOARD STATS ---
        document.getElementById('dpVwap').innerText = fmt(data.vwap_price, 2, '$');
        
        // Format Notional to Millions/Billions for clean reading
        const notional = toNum(data.total_notional_usd);
        let notionalStr = notional === null ? DASH
            : notional >= 1e9 ? `$${(notional / 1e9).toFixed(2)}B` : `$${(notional / 1e6).toFixed(2)}M`;
        document.getElementById('dpNotional').innerText = notionalStr;
        
        document.getElementById('dpVol').innerText = isNum(data.total_block_volume) ? Number(data.total_block_volume).toLocaleString() : DASH;
        document.getElementById('dpMax').innerText = isNum(data.largest_single_block) ? Number(data.largest_single_block).toLocaleString() : DASH;
        
        // Set Sentiment Color (the bias is shown as the server gives it)
        const biasEl = document.getElementById('dpBias');
        biasEl.innerText = sentiment.bias || DASH;
        if (sentiment.bias === 'BULLISH') biasEl.className = "text-green-500 font-bold text-lg tracking-widest drop-shadow-[0_0_5px_rgba(34,197,94,0.5)]";
        else if (sentiment.bias === 'BEARISH') biasEl.className = "text-red-500 font-bold text-lg tracking-widest drop-shadow-[0_0_5px_rgba(239,68,68,0.5)]";
        else biasEl.className = "text-zinc-400 font-bold text-lg tracking-widest";
        const methodEl = document.getElementById('dpBiasMethod');
        if (methodEl) {
            const m = DP_METHODS[sentiment.method];
            methodEl.innerText = sentiment.method ? (m ? m.label : String(sentiment.method).replace(/_/g, ' ').toUpperCase()) : '';
            methodEl.title = m ? m.tip : '';
        }

        // --- 2. BUILD THE TAPE ---
        const tapeContainer = document.getElementById('dpTapeContainer');
        tapeContainer.innerHTML = ''; // Clear previous prints
        
        if (data.recent_prints && data.recent_prints.length > 0) {
            data.recent_prints.forEach(print => {
                // The side is BUY, SELL or UNKNOWN, exactly as the server sends it
                let sideColor = "text-zinc-500";
                if (print.side === "BUY") sideColor = "text-green-400 font-bold";
                if (print.side === "SELL") sideColor = "text-red-400 font-bold";
                
                // Highlight massive prints in purple
                let sizeClass = isNum(print.size) && isNum(data.largest_single_block) && print.size >= data.largest_single_block * 0.8 
                    ? "text-purple-400 font-bold drop-shadow-[0_0_3px_rgba(168,85,247,0.5)]" 
                    : "text-white";

                const row = document.createElement('div');
                row.className = "grid grid-cols-4 px-2 py-1.5 hover:bg-zinc-800/50 rounded transition-colors border-b border-zinc-900/50";
                row.innerHTML = `
                    <div class="text-zinc-400">${esc(print.time)}</div>
                    <div class="text-right ${sizeClass}">${isNum(print.size) ? Number(print.size).toLocaleString() : DASH}</div>
                    <div class="text-right text-blue-300">${fmt(print.price, 4, '$')}</div>
                    <div class="text-right pr-2 ${sideColor}">${esc(print.side || DASH)}</div>
                `;
                tapeContainer.appendChild(row);
            });
        } else {
            tapeContainer.innerHTML = `<div class="p-3 text-center text-zinc-600 text-[10px] uppercase tracking-widest">No institutional prints detected today.</div>`;
        }

        log(`Dark Pool profile loaded for ${ticker}. VWAP: ${fmt(data.vwap_price, 2, '$')}`, 'success');
        // --- ADD THESE LINES TO HOOK PANEL 8 ---
        window.currentDpData = data;
        
        // Reset the slider to 10k default and render
        document.getElementById('dpSizeFilter').value = 10000;
        document.getElementById('dpSizeFilterLabel').innerText = "10k";
        renderDarkPoolChart(data, 10000);
        // ----------------------------------------

    } catch (e) {
        log(`Failed to fetch Dark Pool data: ${e.message}`, 'error');
    }
}

// ==========================================
// --- PANEL 8: DARK POOL VISUALIZER ---
// ==========================================

let dpChartInstance = null;
window.currentDpData = null; // Store data globally so the slider can filter it

function renderDarkPoolChart(data, minSize = 10000) {
    if (!data || !data.recent_prints) return;
    
    const ctx = document.getElementById('dpVisualizerChart').getContext('2d');
    if (dpChartInstance) dpChartInstance.destroy();

    // 1. Filter Prints by the Slider Value
    const filteredPrints = data.recent_prints.filter(p => isNum(p.size) && isNum(p.price) && typeof p.time === 'string' && p.size >= minSize);
    const maxBlock = toNum(data.largest_single_block) || Math.max(...filteredPrints.map(p => p.size), 1);

    // 2. Map Time Strings ("14:50:50") to decimal hours for the X-Axis
    let minTime = 24, maxTime = 0;
    const bubbleData = filteredPrints.map(p => {
        let parts = p.time.split(':');
        let xVal = parseInt(parts[0]) + parseInt(parts[1])/60 + parseInt(parts[2])/3600;
        if (xVal < minTime) minTime = xVal;
        if (xVal > maxTime) maxTime = xVal;
        
        return {
            x: xVal,
            y: p.price,
            r: Math.max(4, (p.size / maxBlock) * 25), // Scale bubble radius
            raw: p // Save original print for tooltip
        };
    });

    // Add some padding to the X-axis bounds
    minTime = Math.max(0, minTime - 0.5);
    maxTime = Math.min(24, maxTime + 0.5);

    // 3. Build the VWAP Line Dataset
    const vwapLine = {
        type: 'line',
        label: 'VWAP Anchor',
        data: [
            { x: minTime, y: toNum(data.vwap_price) },
            { x: maxTime, y: toNum(data.vwap_price) }
        ],
        borderColor: 'rgba(255, 255, 255, 0.4)',
        borderDash: [4, 4],
        borderWidth: 1.5,
        pointRadius: 0,
        fill: false,
        order: 2
    };

    // 4. Build the Whale Bubbles Dataset
    const whales = {
        type: 'bubble',
        label: 'Block Prints',
        data: bubbleData,
        backgroundColor: 'rgba(168, 85, 247, 0.4)', // Neon Purple transparent
        borderColor: 'rgba(168, 85, 247, 1)',       // Solid Purple border
        borderWidth: 2,
        hoverBackgroundColor: 'rgba(34, 197, 94, 0.6)', // Green on hover
        hoverBorderColor: 'rgba(34, 197, 94, 1)',
        order: 1
    };

    // 5. Initialize Chart
    dpChartInstance = new Chart(ctx, {
        data: { datasets: [whales, vwapLine] },
        options: {
            devicePixelRatio: 3, // Keep text razor sharp
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: 'rgba(9, 9, 11, 0.95)',
                    titleColor: '#fff',
                    bodyColor: '#a1a1aa',
                    borderColor: '#27272a',
                    borderWidth: 1,
                    padding: 10,
                    callbacks: {
                        label: function(context) {
                            if (context.dataset.type === 'line') return `VWAP: $${context.raw.y.toFixed(2)}`;
                            let p = context.raw.raw;
                            return [
                                `Price: $${p.price.toFixed(3)}`,
                                `Size: ${p.size.toLocaleString()}`,
                                `Time: ${p.time}`,
                                `Side: ${p.side || DASH}`
                            ];
                        }
                    }
                }
            },
            scales: {
                x: {
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    ticks: {
                        color: '#a1a1aa',
                        font: { size: 9 },
                        callback: function(value) {
                            // Convert decimal hours back to HH:MM format
                            let hrs = Math.floor(value);
                            let mins = Math.round((value - hrs) * 60);
                            if (mins === 60) { hrs += 1; mins = 0; }
                            return `${hrs.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}`;
                        }
                    }
                },
                y: {
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    ticks: { color: '#a1a1aa', font: { size: 9 }, callback: (v) => '$' + v.toFixed(2) }
                }
            }
        }
    });
}

// UI Hook for the slider
function updateDpChartFilter(value) {
    const minSize = parseInt(value);
    
    // Format the label (e.g., 250000 -> 250k)
    let label = minSize >= 1000 ? (minSize / 1000) + 'k' : minSize;
    document.getElementById('dpSizeFilterLabel').innerText = label;
    
    if (window.currentDpData) {
        renderDarkPoolChart(window.currentDpData, minSize);
    }
}

// ==========================================
// --- COPY DATA MODAL ENGINE ---
// ==========================================

// Panel label map for the modal title
const PANEL_LABELS = {
    panel1: 'MACRO RISK INDEX (VMRI)',
    panel2: 'COMEX PHYSICAL INVENTORY',
    panel3: 'WAR ROOM: SCENARIO ENGINE',
    panel4: 'COMEX PAPER:PHYSICAL RATIO',
    panel5: 'PHYSICAL ARB LEDGER',
    panel6: 'DEALER MAP (GEX)',
    panel7: 'DARK POOL TAPE',
    panel8: 'DARK POOL VISUALIZER',
    panel9: 'SLV INSTITUTIONAL FLOW',
    panel10: 'SLV DEALER MAP (GEX)',
    panel11: 'CATALYST CALENDAR',
    panel12: 'MACRO NEWS FEED',
    panel13: 'FED LIQUIDITY PLUMBING',
    panel14: 'SHANGHAI-COMEX ARB',
    panel15: 'ENGINE POSITIONS'
};

// Collects data per panel and opens the modal
function copyPanelData(panelId) {
    let payload = null;
    let format = 'json';

    try {
        if (panelId === 'panel1' || panelId === 'panel2') {
            // iFrame panels — the text of the last Data Dump (GET /dump)
            const lastDump = window._lastXmlDump || null;
            if (lastDump) {
                payload = lastDump;
                format = 'xml';
            } else {
                payload = JSON.stringify({ note: 'No cached data. Click Data Dump in the console bar first.', panel: PANEL_LABELS[panelId] }, null, 2);
            }
        }

        else if (panelId === 'panel3') {
            // War Room: current base macro data + slider values
            const warData = {
                panel: PANEL_LABELS[panelId],
                live_environment: currentBaseData || {},
                scenario_shifts: {
                    dxy_shift: parseFloat(document.getElementById('shiftDxy').value),
                    tnx_shift: parseFloat(document.getElementById('shiftTnx').value),
                    oas_shift: parseFloat(document.getElementById('shiftOas').value),
                    vix_shift_pct: parseFloat(document.getElementById('shiftVix').value),
                },
                hypothetical_vmri: parseFloat(document.getElementById('warVmriScore').innerText) || null,
                engine: warRoomData || null,
                risk_status: document.getElementById('warStatus').innerText
            };
            payload = JSON.stringify(warData, null, 2);
        }

        else if (panelId === 'panel4') {
            // COMEX Paper:Physical ratio panel
            const ratio = document.getElementById('livePaperRatio').innerText;
            const status = document.getElementById('liveRatioStatusBanner').innerText;
            const claims = document.getElementById('livePaperOz').innerText;
            const statusText = document.getElementById('ratioStatusText').innerText;
            payload = JSON.stringify({
                panel: PANEL_LABELS[panelId],
                paper_to_physical_ratio: ratio,
                oz_paper_claims_per_physical: claims,
                status_banner: status,
                status_detail: statusText
            }, null, 2);
        }

        else if (panelId === 'panel5') {
            // Physical Arb Ledger
            if (window.arbData) {
                payload = JSON.stringify({ panel: PANEL_LABELS[panelId], ...window.arbData }, null, 2);
            } else {
                payload = JSON.stringify({ note: 'No data yet. Panel loads on startup.' }, null, 2);
            }
        }

        else if (panelId === 'panel6') {
            // GEX / Dealer Map
            if (window._lastGexData) {
                payload = JSON.stringify({ panel: PANEL_LABELS[panelId], ...window._lastGexData }, null, 2);
            } else {
                payload = JSON.stringify({ note: 'Scan a ticker first using the GEX SCAN button.' }, null, 2);
            }
        }

        else if (panelId === 'panel7' || panelId === 'panel8') {
            // Dark Pool Tape & Visualizer share the same data
            if (window.currentDpData) {
                payload = JSON.stringify({ panel: PANEL_LABELS[panelId], ...window.currentDpData }, null, 2);
            } else {
                payload = JSON.stringify({ note: 'Scan a ticker first using the Dark Pool SCAN button.' }, null, 2);
            }
        }

        else if (panelId === 'panel9') {
            if (window._slvFlowData) {
                payload = JSON.stringify({ panel: PANEL_LABELS[panelId], ...window._slvFlowData }, null, 2);
            } else {
                payload = JSON.stringify({ note: 'SLV flow data loading...' }, null, 2);
            }
        }

        else if (panelId === 'panel10') {
            if (window._slvGexLiveData) {
                payload = JSON.stringify({ panel: PANEL_LABELS[panelId], ...window._slvGexLiveData }, null, 2);
            } else {
                payload = JSON.stringify({ note: 'Click SCAN to load SLV GEX data.' }, null, 2);
            }
        }

        else if (panelId === 'panel11') {
            if (window._calendarData) {
                payload = JSON.stringify({ panel: PANEL_LABELS[panelId], events: window._calendarData }, null, 2);
            } else {
                payload = JSON.stringify({ note: 'Calendar data loading...' }, null, 2);
            }
        }

        else if (panelId === 'panel12') {
            if (window._newsData) {
                payload = JSON.stringify({ panel: PANEL_LABELS[panelId], articles: window._newsData }, null, 2);
            } else {
                payload = JSON.stringify({ note: 'News feed loading...' }, null, 2);
            }
        }

        else if (panelId === 'panel13') {
            if (window._liquidityData) {
                payload = JSON.stringify({ panel: PANEL_LABELS[panelId], ...window._liquidityData }, null, 2);
            } else {
                payload = JSON.stringify({ note: 'Liquidity data loading...' }, null, 2);
            }
        }

        else if (panelId === 'panel15') {
            payload = JSON.stringify({ panel: PANEL_LABELS[panelId], positions: window._positionsData || [] }, null, 2);
        }

        else if (panelId === 'panel14') {
            if (window._arbSpreadData) {
                payload = JSON.stringify({ panel: PANEL_LABELS[panelId], ...window._arbSpreadData }, null, 2);
            } else {
                payload = JSON.stringify({ note: 'Arb spread data loading...' }, null, 2);
            }
        }

    } catch(e) {
        payload = JSON.stringify({ error: e.message }, null, 2);
    }

    openCopyModal(PANEL_LABELS[panelId] || panelId, payload, format);
}

function openCopyModal(title, text, format = 'json') {
    const modal = document.getElementById('copyDataModal');
    document.getElementById('copyModalTitle').innerText = title;
    document.getElementById('copyModalText').value = text || '';
    document.getElementById('copyModalMeta').innerText = `Format: ${format.toUpperCase()} · ${(text || '').length.toLocaleString()} chars`;
    document.getElementById('copyModalStatus').innerText = '';
    modal.classList.remove('hidden');

    // Auto copy to clipboard immediately
    doCopyText(true);
}

function closeCopyModal() {
    document.getElementById('copyDataModal').classList.add('hidden');
}

async function doCopyText(silent = false) {
    const text = document.getElementById('copyModalText').value;
    const statusEl = document.getElementById('copyModalStatus');
    const btn = document.getElementById('copyModalBtn');

    try {
        await navigator.clipboard.writeText(text);
        statusEl.innerText = '✓ COPIED TO CLIPBOARD';
        statusEl.className = 'text-[10px] font-bold text-green-400 uppercase tracking-widest';
        btn.innerText = '✓ COPIED';
        btn.className = 'text-[10px] font-bold bg-green-400 text-black px-3 py-1 rounded transition-all tracking-widest uppercase';
        setTimeout(() => {
            statusEl.innerText = '';
            btn.innerText = 'COPY';
            btn.className = 'text-[10px] font-bold bg-green-600 hover:bg-green-400 text-black px-3 py-1 rounded transition-all tracking-widest uppercase';
        }, 2500);
    } catch(e) {
        if (!silent) {
            statusEl.innerText = 'CLIPBOARD ERROR — SELECT & COPY MANUALLY';
            statusEl.className = 'text-[10px] font-bold text-red-400 uppercase tracking-widest';
        }
    }
}

// Close copy modal on backdrop click
document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('copyDataModal').addEventListener('click', (e) => {
        if (e.target === document.getElementById('copyDataModal')) closeCopyModal();
    });
    // Initialize Wishlist
    if (typeof renderWatchlist === 'function') renderWatchlist();
});

// ==========================================
// --- PANEL 9: SLV INSTITUTIONAL FLOW ---
// ==========================================

let slvFlowChartInstance = null;
window._slvFlowData = null;

async function loadSlvInstitutionalFlow() {
    try {
        const json = await fetchJson(`${API_BASE}/api/institutional_history?ticker=SLV&limit=100`);
        
        if (json.status !== 'success' || !json.data) {
            log(`SLV Institutional Flow Error: ${json.message || 'no data'}`, 'error');
        } else {
            window._slvFlowData = json.data;
            
            // Update header stats with latest values
            const d = json.data;
            const lastIdx = d.labels.length - 1;
            
            const sentEl = document.getElementById('slvDpSentiment');
            const lastSent = d.dp_sentiment[lastIdx];
            if (sentEl && lastSent) {
                sentEl.innerText = lastSent;
                if (lastSent === 'BULLISH') sentEl.className = 'text-green-500 font-bold text-sm drop-shadow-[0_0_5px_rgba(34,197,94,0.5)]';
                else if (lastSent === 'BEARISH') sentEl.className = 'text-red-500 font-bold text-sm drop-shadow-[0_0_5px_rgba(239,68,68,0.5)]';
                else sentEl.className = 'text-zinc-400 font-bold text-sm';
            }
            
            const vwapEl = document.getElementById('slvDpVwap');
            if (vwapEl && d.dp_vwap[lastIdx]) vwapEl.innerText = `$${parseFloat(d.dp_vwap[lastIdx]).toFixed(2)}`;
            
            const cwEl = document.getElementById('slvGexCallWall');
            if (cwEl && d.gex_call_wall[lastIdx]) cwEl.innerText = `$${parseFloat(d.gex_call_wall[lastIdx]).toFixed(2)}`;
            
            const pwEl = document.getElementById('slvGexPutWall');
            if (pwEl && d.gex_put_wall[lastIdx]) pwEl.innerText = `$${parseFloat(d.gex_put_wall[lastIdx]).toFixed(2)}`;
            
            renderSlvFlowChart('sentiment');
            log('SLV Institutional Flow ledger loaded.', 'success');
        }
    } catch (e) {
        log(`SLV Institutional Flow Error: ${e.message}`, 'error');
    }
}

function renderSlvFlowChart(view) {
    if (!window._slvFlowData) return;
    const data = window._slvFlowData;
    
    // Toggle button active states
    document.querySelectorAll('.slv-toggle').forEach(btn => btn.classList.remove('active'));
    if (view === 'sentiment') document.getElementById('btnSlvSentiment')?.classList.add('active');
    if (view === 'volume') document.getElementById('btnSlvVolume')?.classList.add('active');
    if (view === 'gex') document.getElementById('btnSlvGex')?.classList.add('active');
    
    const ctx = document.getElementById('slvFlowChart').getContext('2d');
    if (slvFlowChartInstance) slvFlowChartInstance.destroy();
    
    let datasets = [];
    
    if (view === 'sentiment') {
        // Bull vs Bear volume bars
        const bullData = data.dp_bull_vol.map(v => v != null ? parseFloat(v) : null);
        const bearData = data.dp_bear_vol.map(v => v != null ? -parseFloat(v) : null);
        
        datasets = [
            {
                label: 'Bull Volume',
                data: bullData,
                backgroundColor: 'rgba(34, 197, 94, 0.7)',
                borderColor: 'rgb(34, 197, 94)',
                borderWidth: 1,
                type: 'bar'
            },
            {
                label: 'Bear Volume',
                data: bearData,
                backgroundColor: 'rgba(239, 68, 68, 0.7)',
                borderColor: 'rgb(239, 68, 68)',
                borderWidth: 1,
                type: 'bar'
            }
        ];
    } else if (view === 'volume') {
        datasets = [
            {
                label: 'Total Block Volume',
                data: data.dp_total_vol.map(v => v != null ? parseFloat(v) : null),
                borderColor: '#a78bfa',
                backgroundColor: 'rgba(167, 139, 250, 0.1)',
                borderWidth: 2,
                fill: true,
                tension: 0.3,
                pointRadius: 2,
                pointHoverRadius: 5
            }
        ];
    } else if (view === 'gex') {
        datasets = [
            {
                label: 'Call Wall',
                data: data.gex_call_wall.map(v => v != null ? parseFloat(v) : null),
                borderColor: '#22c55e',
                borderWidth: 2,
                tension: 0.3,
                pointRadius: 1,
                fill: false
            },
            {
                label: 'Put Wall',
                data: data.gex_put_wall.map(v => v != null ? parseFloat(v) : null),
                borderColor: '#ef4444',
                borderWidth: 2,
                tension: 0.3,
                pointRadius: 1,
                fill: false
            },
            {
                label: 'Spot Price',
                data: data.spot_price.map(v => v != null ? parseFloat(v) : null),
                borderColor: 'rgba(255, 255, 255, 0.6)',
                borderWidth: 1.5,
                borderDash: [4, 4],
                tension: 0.3,
                pointRadius: 0,
                fill: false
            }
        ];
    }
    
    slvFlowChartInstance = new Chart(ctx, {
        type: view === 'sentiment' ? 'bar' : 'line',
        data: { labels: data.labels, datasets: datasets },
        options: {
            devicePixelRatio: 3,
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { labels: { color: '#a1a1aa', boxWidth: 10, font: { size: 9 } } },
                tooltip: {
                    backgroundColor: 'rgba(9, 9, 11, 0.95)',
                    borderColor: '#27272a',
                    borderWidth: 1,
                    padding: 10,
                    titleColor: '#fff',
                    bodyColor: '#a1a1aa'
                }
            },
            scales: {
                x: { grid: { display: false }, ticks: { color: '#a1a1aa', font: { size: 8 }, maxTicksLimit: 8 } },
                y: {
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    ticks: { color: '#a1a1aa', font: { size: 9 }, callback: (v) => {
                        if (view === 'gex') return '$' + v.toFixed(0);
                        return formatCompact(Math.abs(v).toString());
                    }}
                }
            }
        }
    });
}

// ==========================================
// --- PANEL 10: SLV GEX MAP (LIVE) ---
// ==========================================

let slvGexChartInstance = null;
window._slvGexLiveData = null;

async function loadSlvGexMap(auto = false) {
    log('Scanning SLV Dealer Options chain...', auto ? 'info' : 'cmd');
    
    try {
        const json = await fetchJson(`${API_BASE}/api/gex?ticker=SLV`);
        
        if (json.status !== 'success' || !json.data) {
            log(`SLV GEX Error: ${json.message || 'no data'}`, 'error');
            return;
        }
        
        const data = json.data;
        window._slvGexLiveData = { ticker: 'SLV', ...data };
        
        // Update stats
        setGexStats({ spot: 'slvGexSpot', zero: 'slvGexZero', call: 'slvGexLiveCallWall', put: 'slvGexLivePutWall' }, data);
        
        // Render chart (identical to SPY GEX chart pattern)
        const ctx = document.getElementById('slvGexChart').getContext('2d');
        if (slvGexChartInstance) slvGexChartInstance.destroy();
        
        const backgroundColors = data.gamma.map(val => val >= 0 ? 'rgba(6, 182, 212, 0.8)' : 'rgba(239, 68, 68, 0.8)');
        const borderColors = data.gamma.map(val => val >= 0 ? 'rgb(6, 182, 212)' : 'rgb(239, 68, 68)');
        
        slvGexChartInstance = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: data.strikes,
                datasets: [{
                    label: 'Net Dealer Gamma',
                    data: data.gamma,
                    backgroundColor: backgroundColors,
                    borderColor: borderColors,
                    borderWidth: 1,
                    borderRadius: 2
                }]
            },
            options: {
                devicePixelRatio: 3,
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                        callbacks: {
                            label: function(context) { return `Net Gamma: ${formatCompact(context.raw)}`; }
                        }
                    }
                },
                scales: {
                    y: {
                        grid: { color: 'rgba(255, 255, 255, 0.1)' },
                        ticks: { color: '#a1a1aa', font: { size: 9 }, callback: (v) => formatCompact(v) }
                    },
                    x: {
                        grid: { display: false },
                        ticks: { color: '#a1a1aa', font: { size: 9 }, maxTicksLimit: 15 }
                    }
                }
            },
            plugins: [{
                id: 'slvSpotLine',
                afterDraw: (chart) => {
                    const xAxis = chart.scales.x;
                    const yAxis = chart.scales.y;
                    const ctx = chart.ctx;
                    let closestIdx = 0;
                    let minDiff = Infinity;
                    data.strikes.forEach((strike, i) => {
                        let diff = Math.abs(strike - data.spot);
                        if (diff < minDiff) { minDiff = diff; closestIdx = i; }
                    });
                    const xPixel = xAxis.getPixelForValue(closestIdx);
                    ctx.save();
                    ctx.beginPath();
                    ctx.setLineDash([5, 5]);
                    ctx.moveTo(xPixel, yAxis.top);
                    ctx.lineTo(xPixel, yAxis.bottom);
                    ctx.lineWidth = 1.5;
                    ctx.strokeStyle = 'rgba(255, 255, 255, 0.8)';
                    ctx.stroke();
                    ctx.fillStyle = '#fff';
                    ctx.font = 'bold 9px JetBrains Mono';
                    ctx.fillText('SPOT', xPixel + 4, yAxis.top + 10);
                    ctx.restore();
                }
            }]
        });
        
        log('SLV GEX Profile loaded.', 'success');
    } catch (e) {
        log(`SLV GEX Error: ${e.message}`, 'error');
    }
}

// ==========================================
// --- PANEL 11: MACRO CALENDAR ---
// ==========================================

window._calendarData = null;

async function loadMacroCalendar() {
    try {
        const json = await fetchJson(`${API_BASE}/api/macro_calendar`);
        
        const container = document.getElementById('calendarContainer');
        
        if (json.status !== 'success') {
            container.innerHTML = `<div class="text-red-400 text-center text-[10px] tracking-widest py-6">Calendar unavailable: ${esc(json.message || 'error')}</div>`;
        } else if (json.events && json.events.length > 0) {
            window._calendarData = json.events;
            container.innerHTML = '';
            
            json.events.forEach(ev => {
                let impactColor = 'text-zinc-500';
                let impactDot = 'bg-zinc-600';
                if (ev.impact === 'High') { impactColor = 'text-red-400'; impactDot = 'bg-red-500'; }
                else if (ev.impact === 'Medium') { impactColor = 'text-amber-400'; impactDot = 'bg-amber-500'; }
                
                const row = document.createElement('div');
                row.className = 'bg-zinc-950 border border-zinc-900 rounded px-3 py-2 hover:border-zinc-700 transition-colors';
                row.innerHTML = `
                    <div class="flex items-center justify-between">
                        <div class="flex items-center gap-2">
                            <div class="w-2 h-2 ${impactDot} rounded-full shrink-0"></div>
                            <span class="text-white font-bold truncate" title="${ev.title}">${ev.title}</span>
                        </div>
                        <span class="${impactColor} text-[9px] font-bold uppercase tracking-widest shrink-0 ml-2">${ev.impact}</span>
                    </div>
                    <div class="flex gap-4 mt-1 text-[9px] text-zinc-500 pl-4">
                        <span>📅 ${ev.date}</span>
                        <span>⏰ ${ev.time}</span>
                        <span>📊 F: ${ev.forecast || 'N/A'}</span>
                        <span>📈 P: ${ev.previous || 'N/A'}</span>
                    </div>
                `;
                container.appendChild(row);
            });
        } else {
            container.innerHTML = '<div class="text-zinc-600 text-center text-[10px] uppercase tracking-widest py-6">No scheduled high-impact events.</div>';
        }
    } catch (e) {
        log(`Calendar Error: ${e.message}`, 'error');
    }
}

// ==========================================
// --- PANEL 12: MACRO NEWS FEED ---
// ==========================================

window._newsData = null;

async function loadMacroNews() {
    try {
        const res = await fetch(`${API_BASE}/api/macro_news`);
        const xmlText = await res.text();
        
        const parser = new DOMParser();
        const xmlDoc = parser.parseFromString(xmlText, 'text/xml');
        const articles = Array.from(xmlDoc.getElementsByTagName('article'));
        
        const container = document.getElementById('newsContainer');
        const newsError = xmlErrorText(xmlText);
        
        if (newsError !== null || !res.ok) {
            container.innerHTML = `<div class="text-red-400 text-center text-[10px] tracking-widest py-6">News unavailable: ${esc(newsError || 'HTTP ' + res.status)}</div>`;
        } else if (articles.length > 0) {
            window._newsData = articles.map(a => ({
                title: a.getAttribute('title'),
                published: a.getAttribute('published'),
                link: a.getAttribute('link')
            }));
            
            container.innerHTML = '';
            
            articles.forEach(article => {
                const title = article.getAttribute('title') || 'No Title';
                const published = article.getAttribute('published') || '';
                const rawLink = article.getAttribute('link') || '';
                // feed text is untrusted: only http(s) links, and every field goes in as text, never as HTML
                const link = /^https?:\/\//i.test(rawLink) ? rawLink : '#';
                const sentiment = parseFloat(article.getAttribute('sentiment') || '0');
                
                const sentColor = sentiment > 0.05 ? 'text-green-500' : (sentiment < -0.05 ? 'text-red-500' : 'text-zinc-500');
                const sentLabel = sentiment > 0.05 ? 'BULLISH' : (sentiment < -0.05 ? 'BEARISH' : 'NEUTRAL');

                const row = document.createElement('a');
                row.href = link;
                row.target = '_blank';
                row.rel = 'noopener noreferrer';
                row.className = 'block bg-zinc-950 border border-zinc-900 rounded px-3 py-2 hover:border-blue-900 transition-colors group cursor-pointer';
                row.innerHTML = `
                    <div class="flex justify-between items-start gap-2">
                        <div class="news-title text-zinc-300 text-[11px] group-hover:text-blue-400 transition-colors leading-tight flex-1"></div>
                        <div class="text-[7px] font-bold px-1 rounded border border-current ${sentColor} whitespace-nowrap mt-0.5">${sentLabel}</div>
                    </div>
                    <div class="news-published text-zinc-600 text-[8px] mt-1 uppercase tracking-widest"></div>
                `;
                row.querySelector('.news-title').textContent = title;
                row.querySelector('.news-published').textContent = published;
                container.appendChild(row);
            });
        } else {
            container.innerHTML = '<div class="text-zinc-600 text-center text-[10px] uppercase tracking-widest py-6">No headlines available.</div>';
        }
    } catch (e) {
        log(`News Error: ${e.message}`, 'error');
    }
}

// ==========================================
// --- PANEL 13: FED LIQUIDITY PLUMBING ---
// ==========================================

let liquidityChartInstance = null;
window._liquidityData = null;

async function loadFedLiquidity() {
    try {
        const json = await fetchJson(`${API_BASE}/api/macro_ledger_full?limit=200`);
        
        if (json.status !== 'success' || !json.data) {
            log(`Fed Liquidity Error: ${json.message || 'no data'}`, 'error');
        } else {
            const d = json.data;
            window._liquidityData = { labels: d.labels, rrp: d.reverse_repo_bn, walcl: d.fed_balance_sheet_bn };
            
            // Update header stats
            const lastIdx = d.labels.length - 1;
            const rrpEl = document.getElementById('liveRRP');
            const walclEl = document.getElementById('liveWALCL');
            
            if (rrpEl && d.reverse_repo_bn[lastIdx] != null) rrpEl.innerText = `$${d.reverse_repo_bn[lastIdx].toFixed(0)}B`;
            if (walclEl && d.fed_balance_sheet_bn[lastIdx] != null) walclEl.innerText = `$${d.fed_balance_sheet_bn[lastIdx].toFixed(0)}B`;
            
            // Render chart
            const ctx = document.getElementById('liquidityChart').getContext('2d');
            if (liquidityChartInstance) liquidityChartInstance.destroy();
            
            liquidityChartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: d.labels,
                    datasets: [
                        {
                            label: 'Reverse Repo ($B)',
                            data: d.reverse_repo_bn,
                            borderColor: '#38bdf8',
                            backgroundColor: 'rgba(56, 189, 248, 0.05)',
                            borderWidth: 2,
                            fill: true,
                            tension: 0.3,
                            pointRadius: 0,
                            yAxisID: 'y'
                        },
                        {
                            label: 'Fed Balance Sheet ($B)',
                            data: d.fed_balance_sheet_bn,
                            borderColor: '#f59e0b',
                            borderWidth: 2,
                            tension: 0.3,
                            pointRadius: 0,
                            yAxisID: 'y1'
                        }
                    ]
                },
                options: {
                    devicePixelRatio: 3,
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { labels: { color: '#a1a1aa', boxWidth: 10, font: { size: 9 } } },
                        tooltip: {
                            backgroundColor: 'rgba(9, 9, 11, 0.95)',
                            borderColor: '#27272a',
                            borderWidth: 1,
                            padding: 10,
                            titleColor: '#fff',
                            bodyColor: '#a1a1aa',
                            callbacks: {
                                label: function(context) {
                                    return `${context.dataset.label}: $${context.parsed.y?.toFixed(1) || 'N/A'}B`;
                                }
                            }
                        }
                    },
                    scales: {
                        x: { grid: { display: false }, ticks: { color: '#a1a1aa', font: { size: 8 }, maxTicksLimit: 8 } },
                        y: {
                            position: 'left',
                            grid: { color: 'rgba(56, 189, 248, 0.05)' },
                            ticks: { color: '#38bdf8', font: { size: 9 }, callback: (v) => '$' + v + 'B' },
                            title: { display: false }
                        },
                        y1: {
                            position: 'right',
                            grid: { display: false },
                            ticks: { color: '#f59e0b', font: { size: 9 }, callback: (v) => '$' + v + 'B' },
                            title: { display: false }
                        }
                    }
                }
            });
        }
    } catch (e) {
        log(`Fed Liquidity Error: ${e.message}`, 'error');
    }
}

// ==========================================
// --- PANEL 14: SHANGHAI-COMEX ARB SPREAD ---
// ==========================================

let arbSpreadChartInstance = null;
window._arbSpreadData = null;

async function loadShfeArbSpread() {
    try {
        const json = await fetchJson(`${API_BASE}/api/macro_ledger_full?limit=200`);
        
        if (json.status !== 'success' || !json.data) {
            log(`Shanghai-COMEX Arb Error: ${json.message || 'no data'}`, 'error');
        } else {
            const d = json.data;
            window._arbSpreadData = { labels: d.labels, shfe: d.shfe_silver_usd, comex: d.comex_silver, premium: d.shfe_premium };
            
            // Update header stats
            const lastIdx = d.labels.length - 1;
            const shfeEl = document.getElementById('liveShfe');
            const comexEl = document.getElementById('liveComexSilver');
            const premEl = document.getElementById('liveShfePremium');
            
            if (shfeEl && d.shfe_silver_usd[lastIdx] != null) shfeEl.innerText = `$${d.shfe_silver_usd[lastIdx].toFixed(2)}`;
            if (comexEl && d.comex_silver[lastIdx] != null) comexEl.innerText = `$${d.comex_silver[lastIdx].toFixed(2)}`;
            if (premEl && d.shfe_premium[lastIdx] != null) {
                const prem = d.shfe_premium[lastIdx];
                premEl.innerText = `${prem >= 0 ? '+' : ''}$${prem.toFixed(2)}`;
                premEl.className = prem >= 0 
                    ? 'text-green-400 font-bold text-sm font-mono drop-shadow-[0_0_5px_rgba(34,197,94,0.5)]'
                    : 'text-red-400 font-bold text-sm font-mono';
            }
            
            // Render chart
            const ctx = document.getElementById('arbSpreadChart').getContext('2d');
            if (arbSpreadChartInstance) arbSpreadChartInstance.destroy();
            
            arbSpreadChartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: d.labels,
                    datasets: [
                        {
                            label: 'SHFE Silver (USD/oz)',
                            data: d.shfe_silver_usd,
                            borderColor: '#f43f5e',
                            borderWidth: 2,
                            tension: 0.3,
                            pointRadius: 0,
                            yAxisID: 'y'
                        },
                        {
                            label: 'COMEX Silver',
                            data: d.comex_silver,
                            borderColor: 'rgba(255, 255, 255, 0.7)',
                            borderWidth: 2,
                            tension: 0.3,
                            pointRadius: 0,
                            yAxisID: 'y'
                        },
                        {
                            label: 'Arb Premium ($/oz)',
                            data: d.shfe_premium,
                            borderColor: '#22c55e',
                            backgroundColor: 'rgba(34, 197, 94, 0.1)',
                            borderWidth: 2,
                            fill: true,
                            tension: 0.3,
                            pointRadius: 0,
                            yAxisID: 'y1'
                        }
                    ]
                },
                options: {
                    devicePixelRatio: 3,
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { labels: { color: '#a1a1aa', boxWidth: 10, font: { size: 9 } } },
                        tooltip: {
                            backgroundColor: 'rgba(9, 9, 11, 0.95)',
                            borderColor: '#27272a',
                            borderWidth: 1,
                            padding: 10,
                            titleColor: '#fff',
                            bodyColor: '#a1a1aa',
                            callbacks: {
                                label: function(context) {
                                    return `${context.dataset.label}: $${context.parsed.y?.toFixed(2) || 'N/A'}`;
                                }
                            }
                        }
                    },
                    scales: {
                        x: { grid: { display: false }, ticks: { color: '#a1a1aa', font: { size: 8 }, maxTicksLimit: 8 } },
                        y: {
                            position: 'left',
                            grid: { color: 'rgba(255, 255, 255, 0.05)' },
                            ticks: { color: '#a1a1aa', font: { size: 9 }, callback: (v) => '$' + v.toFixed(0) }
                        },
                        y1: {
                            position: 'right',
                            grid: { display: false },
                            ticks: { color: '#22c55e', font: { size: 9 }, callback: (v) => '$' + v.toFixed(2) }
                        }
                    }
                }
            });
        }
    } catch (e) {
        log(`Shanghai-COMEX Arb Error: ${e.message}`, 'error');
    }
}

// ==========================================
// --- ⚡ DUMP ALL DATA ENGINE ---
// ==========================================

// Helper: get the last non-null value from an array
function _lastValid(arr) {
    if (!arr || !Array.isArray(arr)) return null;
    for (let i = arr.length - 1; i >= 0; i--) {
        if (arr[i] != null) return arr[i];
    }
    return null;
}

// Helper: get the last N non-null {label, value} pairs from parallel arrays
function _lastNEntries(labels, values, n = 5) {
    if (!labels || !values) return [];
    const entries = [];
    for (let i = labels.length - 1; i >= 0 && entries.length < n; i--) {
        if (values[i] != null) {
            entries.unshift({ date: labels[i], value: typeof values[i] === 'number' ? Math.round(values[i] * 100) / 100 : values[i] });
        }
    }
    return entries;
}

// Helper: extract latest snapshot from institutional flow arrays
function _extractLatestFlow(flowData) {
    if (!flowData?.data) return null;
    const d = flowData.data;
    const idx = (d.labels?.length || 1) - 1;
    return {
        ticker: d.ticker,
        scan_date: d.labels?.[idx],
        spot_price: d.spot_price?.[idx],
        dp_sentiment: d.dp_sentiment?.[idx],
        dp_vwap: d.dp_vwap?.[idx],
        dp_total_vol: d.dp_total_vol?.[idx],
        dp_notional_usd: d.dp_notional?.[idx],
        dp_largest_block: d.dp_largest_block?.[idx],
        dp_bull_vol: d.dp_bull_vol?.[idx],
        dp_bear_vol: d.dp_bear_vol?.[idx],
        gex_call_wall: d.gex_call_wall?.[idx],
        gex_put_wall: d.gex_put_wall?.[idx],
        gex_zero_gamma: d.gex_zero_gamma?.[idx]
    };
}

// Helper: extract latest arb entry
function _extractLatestArb(arbData) {
    if (!arbData?.data) return null;
    const d = arbData.data;
    const idx = (d.labels?.length || 1) - 1;
    return {
        date: d.labels?.[idx],
        spot_price: d.spot?.[idx],
        avg_premium_pct: d.avg_pct?.[idx],
        avg_retail_price: d.avg_price?.[idx],
        cheapest_price: d.cheapest_dollar != null ? (d.spot?.[idx] != null ? d.spot[idx] + (d.cheapest_dollar?.[idx] || 0) : null) : null,
        cheapest_premium_pct: d.cheapest_pct?.[idx],
        cheapest_premium_dollar: d.cheapest_dollar?.[idx],
        recent_trend_5d: _lastNEntries(d.labels, d.avg_pct, 5)
    };
}

// Helper: flatten dark pool response
function _flattenDarkPool(dpData) {
    if (!dpData?.data) return null;
    const d = dpData.data;
    return {
        ticker: d.ticker,
        sentiment: d.sentiment?.bias,
        sentiment_method: d.sentiment?.method,
        vwap: isNum(d.vwap_price) ? Math.round(d.vwap_price * 100) / 100 : null,
        total_block_volume: d.total_block_volume,
        total_notional_usd: d.total_notional_usd,
        largest_single_block: d.largest_single_block,
        bull_volume: d.sentiment?.bull_volume,
        bear_volume: d.sentiment?.bear_volume,
        recent_prints: (d.recent_prints || []).slice(0, 5)
    };
}

// Helper: flatten GEX response
function _flattenGex(gexData) {
    if (!gexData?.data) return null;
    const d = gexData.data;
    // Only keep the top 5 positive and top 5 negative gamma strikes for compactness
    const gammaEntries = (d.strikes || []).map((s, i) => ({ strike: s, gamma: d.gamma?.[i] || 0 }));
    const sorted = [...gammaEntries].sort((a, b) => b.gamma - a.gamma);
    const topPositive = sorted.filter(e => e.gamma > 0).slice(0, 5);
    const topNegative = sorted.filter(e => e.gamma < 0).slice(-5);
    return {
        spot: d.spot,
        call_wall: d.callWall,
        put_wall: d.putWall,
        zero_gamma: isNum(d.zeroGamma) ? d.zeroGamma : null,
        zero_gamma_reason: isNum(d.zeroGamma) ? undefined : (d.zeroGammaReason || 'not available'),
        regime: d.spot > d.callWall ? 'ABOVE_CALL_WALL' : d.spot < d.putWall ? 'BELOW_PUT_WALL' : 'BETWEEN_WALLS',
        top_positive_gamma: topPositive,
        top_negative_gamma: topNegative
    };
}

// Helper: parse XML news to clean array
function _parseNewsXml(xmlText) {
    try {
        const parser = new DOMParser();
        const doc = parser.parseFromString(xmlText, 'text/xml');
        return Array.from(doc.getElementsByTagName('article')).map(a => ({
            title: a.getAttribute('title'),
            published: a.getAttribute('published'),
            link: a.getAttribute('link')
        }));
    } catch { return []; }
}

async function dumpAllData() {
    const btnText = document.getElementById('dumpBtnText');
    const progress = document.getElementById('dumpProgress');
    const btn = document.getElementById('btnDumpAll');
    
    btnText.innerText = '⏳ AGGREGATING...';
    btn.disabled = true;
    progress.style.width = '0%';
    log('Initiating full system data dump — aggregating ALL sources...', 'cmd');
    
    // Define all fetch targets. The Silver Eagle scan is not one of them: it writes a ledger row and is a POST;
    // the ledger itself (arbitrage_history) is already included.
    const fetches = [
        { key: 'macro_ledger_full', url: '/api/macro_ledger_full?limit=500', type: 'json' },
        { key: 'vmri_history', url: '/api/vmri_history', type: 'json' },
        { key: 'institutional_flow_slv', url: '/api/institutional_history?ticker=SLV&limit=500', type: 'json' },
        { key: 'institutional_flow_spy', url: '/api/institutional_history?ticker=SPY&limit=500', type: 'json' },
        { key: 'darkpool_slv_live', url: '/api/darkpool?ticker=SLV', type: 'json' },
        { key: 'darkpool_spy_live', url: '/api/darkpool?ticker=SPY', type: 'json' },
        { key: 'gex_slv', url: '/api/gex?ticker=SLV', type: 'json' },
        { key: 'gex_spy', url: '/api/gex?ticker=SPY', type: 'json' },
        { key: 'arbitrage_history', url: '/api/arbitrage_history?limit=200', type: 'json' },
        { key: 'comex_inventory', url: '/api/inventory_data', type: 'json' },
        { key: 'macro_calendar', url: '/api/macro_calendar', type: 'json' },
        { key: 'war_room_live', url: '/api/war_room', type: 'json_post', body: { dxy_shift: 0, tnx_shift: 0, oas_shift: 0, vix_shift_pct: 0 } },
        { key: 'tactical_ruling_xml', url: '/api/dump', type: 'text' },
        { key: 'macro_news', url: '/api/macro_news', type: 'xml' },
    ];
    
    let completed = 0;
    const total = fetches.length;
    
    // Fan out ALL requests in parallel
    const rawResults = {};
    const results = await Promise.allSettled(fetches.map(async (f) => {
        try {
            let res;
            if (f.type === 'json_post') {
                res = await fetch(`${API_BASE}${f.url}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(f.body)
                });
            } else {
                res = await fetch(`${API_BASE}${f.url}`);
            }
            
            completed++;
            progress.style.width = `${(completed / total) * 100}%`;
            
            // A 4xx/5xx answer is a failed source whose message is kept, not data
            if (f.type === 'text' || f.type === 'xml') {
                const text = await res.text();
                if (!res.ok) {
                    rawResults[f.key] = { data: { error: xmlErrorText(text) || `HTTP ${res.status}`, http_status: res.status }, format: 'error' };
                    return f.key;
                }
                rawResults[f.key] = { data: text, format: f.type };
                return f.key;
            } else {
                let json = null;
                try { json = await res.json(); } catch (e) { json = null; }
                if (!res.ok || !json || json.status === 'error') {
                    rawResults[f.key] = { data: { error: (json && json.message) || `HTTP ${res.status}`, http_status: res.status }, format: 'error' };
                    return f.key;
                }
                rawResults[f.key] = { data: json, format: 'json' };
                return f.key;
            }
        } catch (e) {
            completed++;
            progress.style.width = `${(completed / total) * 100}%`;
            rawResults[f.key] = { data: { error: e.message }, format: 'error' };
            return f.key;
        }
    }));
    
    let successCount = Object.values(rawResults).filter(r => r.format !== 'error').length;
    let errorCount = Object.values(rawResults).filter(r => r.format === 'error').length;
    
    // ==========================================
    // POST-PROCESS: Extract latest snapshot only
    // ==========================================
    
    const clean = {
        _meta: {
            system: "VladHQ OptionsWhale Terminal",
            dump_timestamp: new Date().toISOString(),
            purpose: "LATEST system state snapshot for LLM analysis. All values are the most recent readings from live infrastructure.",
            sources_succeeded: successCount,
            sources_failed: errorCount,
            failed_sources: Object.fromEntries(Object.entries(rawResults).filter(([, r]) => r.format === 'error').map(([k, r]) => [k, r.data.error]))
        }
    };
    
    // --- 1. MACRO SNAPSHOT (latest values only from the 25-column ledger) ---
    const ml = rawResults.macro_ledger_full?.data;
    if (ml?.status === 'success' && ml.data) {
        const d = ml.data;
        const lastLabel = _lastValid(d.labels);
        clean.macro_snapshot = {
            _note: "Latest macro readings from the master ledger (most recent non-null value for each indicator)",
            as_of: lastLabel,
            vmri_score: _lastValid(d.vmri_score),
            threat_tier: _lastValid(d.threat_tier),
            dxy: _lastValid(d.dxy),
            dxy_change: _lastValid(d.dxy_change),
            ten_y_yield: _lastValid(d.ten_y_yield),
            zn_futures: _lastValid(d.zn_futures),
            high_yield_oas: _lastValid(d.high_yield_oas),
            vix: _lastValid(d.vix),
            vix_change: _lastValid(d.vix_change),
            gold_price: _lastValid(d.gold_price),
            gold_silver_ratio: _lastValid(d.gold_silver_ratio) ? Math.round(_lastValid(d.gold_silver_ratio) * 100) / 100 : null,
            comex_silver: _lastValid(d.comex_silver),
            shfe_silver_usd: _lastValid(d.shfe_silver_usd) ? Math.round(_lastValid(d.shfe_silver_usd) * 100) / 100 : null,
            shfe_premium: _lastValid(d.shfe_premium) ? Math.round(_lastValid(d.shfe_premium) * 100) / 100 : null,
            wti_crude: _lastValid(d.wti_crude),
            brent_crude: _lastValid(d.brent_crude),
            reverse_repo_bn: _lastValid(d.reverse_repo_bn),
            fed_balance_sheet_bn: _lastValid(d.fed_balance_sheet_bn),
            retail_silver_cheapest: _lastValid(d.retail_silver_cheapest),
            retail_silver_avg: _lastValid(d.retail_silver_avg),
            silver_oi: _lastValid(d.silver_oi),
            paper_physical_ratio: _lastValid(d.paper_physical_ratio),
            gex: _lastValid(d.gex),
            dix: _lastValid(d.dix)
        };
        // Add 5-day VMRI trend
        clean.macro_snapshot.vmri_5d_trend = _lastNEntries(d.labels, d.vmri_score, 5);
    }
    
    // --- 2. VMRI CONTEXT (latest only, skip if macro_snapshot already has it) ---
    const vh = rawResults.vmri_history?.data;
    if (vh?.scores) {
        const idx = vh.scores.length - 1;
        clean.vmri_latest = {
            score: _lastValid(vh.scores),
            primary_driver: _lastValid(vh.primary_driver),
            momentum_5: _lastValid(vh.momentum_5),
            sma_10: _lastValid(vh.sma_10)
        };
    }
    
    // --- 3. SLV INSTITUTIONAL FLOW (latest entry only) ---
    clean.slv_institutional = _extractLatestFlow(rawResults.institutional_flow_slv?.data);
    
    // --- 4. SPY INSTITUTIONAL FLOW (latest entry only) ---
    clean.spy_institutional = _extractLatestFlow(rawResults.institutional_flow_spy?.data);
    
    // --- 5. DARK POOL LIVE (flattened summaries) ---
    clean.darkpool_slv = _flattenDarkPool(rawResults.darkpool_slv_live?.data);
    clean.darkpool_spy = _flattenDarkPool(rawResults.darkpool_spy_live?.data);
    
    // --- 6. GEX PROFILES (compact) ---
    clean.gex_slv = _flattenGex(rawResults.gex_slv?.data);
    clean.gex_spy = _flattenGex(rawResults.gex_spy?.data);
    
    // --- 7. PHYSICAL ARB (latest only) ---
    clean.silver_arb = _extractLatestArb(rawResults.arbitrage_history?.data);
    
    // --- 8. COMEX INVENTORY (already compact) ---
    const inv = rawResults.comex_inventory?.data;
    if (inv?.status === 'success') {
        clean.comex_inventory = inv.data || inv;
    }
    
    // --- 9. MACRO CALENDAR (pass through, already compact) ---
    const cal = rawResults.macro_calendar?.data;
    if (cal?.status === 'success') {
        clean.macro_calendar = cal.events || [];
    }
    
    // --- 10. WAR ROOM (live environment snapshot) ---
    const wr = rawResults.war_room_live?.data;
    if (wr) {    // on a failure this is the error message (for example the ledger input that is missing)
        // The war room response has a nested structure, extract the key fields
        clean.war_room = wr.data || wr;
    }
    
    // --- 11. TACTICAL RULING XML (keep full — it's the core signal) ---
    if (rawResults.tactical_ruling_xml?.format !== 'error') {
        clean.tactical_ruling_xml = rawResults.tactical_ruling_xml?.data;
    }
    
    // --- 12. MACRO NEWS (parsed to clean array) ---
    if (rawResults.macro_news?.format === 'xml') {
        clean.macro_news = _parseNewsXml(rawResults.macro_news.data);
    }
    
    // --- 13. CACHED UI STATE ---
    clean.ui_state = {
        paper_physical_ratio: document.getElementById('livePaperRatio')?.innerText || null,
        paper_physical_status: document.getElementById('liveRatioStatusBanner')?.innerText || null,
        war_room_vmri: document.getElementById('warVmriScore')?.innerText || null,
        war_room_status: document.getElementById('warStatus')?.innerText || null,
        api_status: document.getElementById('apiStatus')?.innerText || null
    };
    
    // Format and display
    const jsonStr = JSON.stringify(clean, null, 2);
    
    btnText.innerText = '⚡ DUMP ALL DATA';
    btn.disabled = false;
    progress.style.width = '100%';
    setTimeout(() => { progress.style.width = '0%'; }, 2000);
    
    log(`Data dump complete: ${successCount}/${total} sources → ${(jsonStr.length / 1024).toFixed(0)}KB (compressed from raw)`, 'success');
    
    openCopyModal(
        `⚡ SYSTEM SNAPSHOT — ${successCount}/${total} SOURCES (${(jsonStr.length / 1024).toFixed(0)}KB)`,
        jsonStr,
        'json'
    );
}

// --- TAB SWITCHING SYSTEM ---
function switchTab(tabName) {
    // On a phone, picking a tab while the console is showing leaves the console and shows that tab
    if (isMobileLayout()) switchMobileTab('panels');

    // 1. Update UI Buttons
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
    const activeBtn = document.getElementById(`tab-${tabName}`);
    if (activeBtn) activeBtn.classList.add('active');

    // 2. Toggle Visibility
    document.querySelectorAll('.tab-content').forEach(content => content.classList.add('hidden'));
    const activeContent = document.getElementById(`${tabName}-tab`);
    if (activeContent) activeContent.classList.remove('hidden');

    // 3. Trigger Logic
    if (tabName === 'arbitrage') {
        loadTimeArbitrageData();
        // Start polling for arbitrage data
        if (window.arbInterval) clearInterval(window.arbInterval);
        window.arbInterval = setInterval(loadTimeArbitrageData, 60000);
    } else {
        if (window.arbInterval) clearInterval(window.arbInterval);
    }

    log(`WORKSPACE ROUTING: ${tabName.toUpperCase()} ACTIVE`, 'info');
}

// --- TIME ARBITRAGE ENGINE ---
// Every number of this tab can be null: the server sends null plus a reason in data.missing ({"field": "reason"})
// when it cannot compute a value. A null is shown as a dash with the reason, never as 0.
let _arbMissing = {};

async function loadTimeArbitrageData() {
    const ticker = (document.getElementById('scanTicker').value || "SPY").trim().toUpperCase();
    try {
        const result = await fetchJson(`${API_BASE}/api/time_arbitrage?ticker=${encodeURIComponent(ticker)}`);
        
        if (result.status === 'success') {
            updateArbitrageUI(result.data, ticker);
        } else {
            showArbError(result.message || 'Time Arbitrage data unavailable');
        }
    } catch (e) {
        showArbError(`Failed to load Time Arbitrage data: ${e.message}`);
    }
    refreshWatchlistPrices();   // the wishlist's Live Px column, at most once a minute per contract
}

// The tab-wide note: an error from the route, or every reason in data.missing
function showArbNote(html, isError) {
    const el = document.getElementById('arbMissingNote');
    if (!el) return;
    el.innerHTML = html;
    el.className = `${html ? '' : 'hidden '}px-5 pt-3 text-[10px] leading-snug ${isError ? 'text-red-400' : 'text-amber-400'}`;
}
function showArbError(message) {
    console.error('Time Arbitrage:', message);
    showArbNote(`Time Arbitrage unavailable: ${esc(message)}`, true);
    // blank the panels: numbers of an earlier answer (maybe for another ticker) must not stay next to the error
    _arbMissing = {};
    updateOscillator(null, message);
    updateTrapdoor(null, null, message);
    updateIvBleed(null, message);
    updateProbMatrix(null, null, message);
    renderTrapdoorProfileChart(null, message);
    renderTermStructure(null, message);
    updateIvHvSpread(null, { realized: message, implied: message });
}

// Reason for a value the server left out: the first name in `names` that data.missing has, as itself or as a prefix
// ("iv_hv_spread" also matches "iv_hv_spread.atm_implied_volatility").
function arbReason(...names) {
    const keys = Object.keys(_arbMissing);
    for (const n of names) {
        const k = keys.find(key => key === n || key.startsWith(n + '.') || key.startsWith(n + ':') || key.startsWith(n + '['));
        if (k) return String(_arbMissing[k]);
    }
    return '';
}

function updateArbitrageUI(data, ticker) {
    if (!data) return;
    _arbMissing = (data.missing && typeof data.missing === 'object') ? data.missing : {};
    const reasons = Object.entries(_arbMissing).map(([k, v]) => `${esc(k)}: ${esc(v)}`);
    showArbNote(reasons.length ? `Not available for ${esc(data.ticker || ticker)}: ${reasons.join(' · ')}` : '', false);

    updateOscillator(data.z_score, arbReason('z_score', 'oscillator'), data.z_components);
    updateTrapdoor(data.gamma_state, data.dealer_trapdoor, arbReason('gamma_state', 'zero_gamma', 'dealer_trapdoor'));
    updateIvBleed(data.iv_bleed, arbReason('iv_bleed'));
    updateProbMatrix(data.probabilities, data.ticker || ticker, arbReason('probabilities'));
    renderTrapdoorProfileChart(data.dealer_trapdoor && data.dealer_trapdoor.vanna_profile, arbReason('vanna_profile', 'dealer_trapdoor'));
    renderTermStructure(data.term_structure, arbReason('term_structure'));
    updateIvHvSpread(data.iv_hv_spread, {
        realized: arbReason('realized_volatility_20d', 'iv_hv_spread'),
        implied: arbReason('atm_implied_volatility', 'iv_hv_spread'),
    });
}

// Puts a short message over a chart panel that has no data (the canvas sits in a relative box), or clears it
function setChartNote(canvasId, text) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || !canvas.parentElement) return;
    let note = canvas.parentElement.querySelector('.chart-note');
    if (!text) { if (note) note.remove(); return; }
    if (!note) {
        note = document.createElement('div');
        note.className = 'chart-note absolute inset-0 flex items-center justify-center p-6 text-center text-zinc-600 text-[10px] uppercase tracking-widest';
        canvas.parentElement.appendChild(note);
    }
    note.innerText = text;
}

// Which factors (vix, gex, dix) the score was built from, and why the others were left out
function describeOscillatorParts(parts) {
    if (!parts || typeof parts !== 'object') return '';
    const used = Array.isArray(parts.used) ? parts.used.map(n => String(n).toUpperCase()) : [];
    const left = Object.entries(parts.missing || {}).map(([n, why]) => `${n.toUpperCase()} (${why})`);
    return (used.length ? `Built from ${used.join(' + ')}.` : 'No factor could be used.') + (left.length ? ` Left out: ${left.join('; ')}.` : '');
}

function updateOscillator(score, reason, parts) {
    const gauge = document.getElementById('oscillatorGauge');
    const needle = document.getElementById('oscillatorNeedle');
    const valueText = document.getElementById('oscillatorValue');
    const statusText = document.getElementById('oscillatorStatus');
    const signalBox = document.getElementById('oscillatorSignal');
    const partsBox = document.getElementById('oscillatorComponents');

    if (!needle || !valueText) return;
    const partsText = describeOscillatorParts(parts);
    if (partsBox) partsBox.innerText = partsText;
    if (statusText) statusText.removeAttribute('title');

    // No score: the needle, its hub and most of the arc fade out, so the dial does not look like a reading of zero
    const hasScore = isNum(score);
    needle.style.opacity = hasScore ? '' : '0';
    document.getElementById('oscillatorHub')?.style.setProperty('opacity', hasScore ? '' : '0');
    if (gauge) gauge.style.opacity = hasScore ? '' : '0.25';

    if (!hasScore) {
        needle.style.transform = 'rotate(0deg)';
        valueText.innerText = DASH;
        // The gauge dial is small, so it gets a short label only. The reason can be long (it lists every missing input): it goes
        // under the gauge, in the line that says which factors were left out, or when that line is empty (the route failed and
        // sent no factors) in its place, and it is the dial label's tooltip. The tab's note line above also carries it.
        if (statusText) {
            statusText.innerText = 'No reading';
            if (reason) statusText.title = reason;
        }
        if (partsBox && reason && !partsText) partsBox.innerText = reason;
        if (signalBox) {
            signalBox.innerText = "NO READING";
            signalBox.className = "bg-zinc-950 border border-zinc-800 px-4 py-2 rounded text-[11px] font-bold text-zinc-600 uppercase tracking-[0.2em]";
        }
        return;
    }
    score = Number(score);

    // Normalize -100 to +100 to -90 to 90 degrees
    const rotation = (score / 100) * 90;
    needle.style.transform = `rotate(${rotation}deg)`;
    
    valueText.innerText = score.toFixed(1);
    
    if (Math.abs(score) < 75) {
        if (signalBox) {
            signalBox.innerText = "CASH POSITION - NO STRUCTURAL EDGE";
            signalBox.className = "bg-zinc-950 border border-zinc-800 px-4 py-2 rounded text-[11px] font-bold text-zinc-400 uppercase tracking-[0.2em]";
        }
        if (statusText) statusText.innerText = "Neutral Variance";
    } else {
        const side = score > 0 ? "BULLISH" : "BEARISH";
        if (signalBox) {
            signalBox.innerText = `STRATEGIC EDGE DETECTED: ${side}`;
            signalBox.className = `bg-${score > 0 ? 'green' : 'red'}-900/20 border border-${score > 0 ? 'green' : 'red'}-500 px-4 py-2 rounded text-[11px] font-bold text-${score > 0 ? 'green' : 'red'}-500 uppercase tracking-[0.2em] animate-pulse`;
        }
        if (statusText) statusText.innerText = "Extreme Extension";
    }
}

function updateTrapdoor(state, trapdoor, reason) {
    const dist = document.getElementById('trapDistance');
    const distPct = document.getElementById('trapDistancePct');
    const velocity = document.getElementById('trapVelocity');
    const vanna = document.getElementById('trapVanna');
    const charm = document.getElementById('trapCharm');
    const icon = document.getElementById('trapStatusIcon');
    const title = document.getElementById('trapStatusTitle');
    const desc = document.getElementById('trapStatusDesc');
    const alert = document.getElementById('trapAlert');

    if (!dist || !velocity) return;
    state = state || {};

    dist.innerText = fmt(state.distance, 3);
    if (distPct) distPct.innerText = isNum(state.distance_pct) ? `${(state.distance_pct * 100).toFixed(2)}% Distance` : `${DASH} Distance`;
    velocity.innerText = fmt(state.velocity, 4);
    if (vanna) vanna.innerText = fmt(trapdoor && trapdoor.vanna_exposure, 2);
    if (charm) charm.innerText = fmt(trapdoor && trapdoor.charm_exposure, 2);

    if (typeof state.short_gamma_active !== 'boolean') {
        // no zero-gamma level (or no spot), so there is no state to report
        if (icon) {
            icon.innerText = "❔";
            icon.className = "text-4xl mb-2 text-zinc-800";
        }
        if (title) {
            title.innerText = "No Gamma Reading";
            title.className = "text-sm font-bold text-zinc-500 uppercase tracking-widest mb-1";
        }
        if (desc) desc.innerText = reason || "The zero-gamma level is not available, so the squeeze state cannot be judged.";
        if (alert) alert.classList.add('hidden');
    } else if (state.short_gamma_active) {
        if (icon) {
            icon.innerText = "🌋";
            icon.className = "text-4xl mb-2 trap-active";
        }
        if (title) {
            title.innerText = "SHORT GAMMA SQUEEZE";
            title.className = "text-sm font-bold text-red-500 uppercase tracking-widest mb-1";
        }
        if (desc) desc.innerText = "Dealers are structurally exposed. Forced selling/buying imminent.";
        if (alert) alert.classList.remove('hidden');
    } else {
        if (icon) {
            icon.innerText = "🔒";
            icon.className = "text-4xl mb-2 text-zinc-800";
        }
        if (title) {
            title.innerText = "Gamma Neutral";
            title.className = "text-sm font-bold text-zinc-500 uppercase tracking-widest mb-1";
        }
        if (desc) desc.innerText = "Market makers are currently hedged. No structural squeeze detected.";
        if (alert) alert.classList.add('hidden');
    }
}

function updateIvBleed(bleedData, reason) {
    const container = document.getElementById('ivBleedContainer');
    if (!container) return;
    if (!Array.isArray(bleedData) || bleedData.length === 0) {
        container.innerHTML = `<div class="p-8 text-center text-zinc-700 text-[10px] uppercase tracking-widest">${esc(reason || 'No significant bleed detected.')}</div>`;
        return;
    }

    container.innerHTML = bleedData.map(item => `
        <div class="grid grid-cols-4 text-[10px] p-2 border-b border-zinc-900 hover:bg-zinc-900/50 transition-colors">
            <div class="pl-2 font-bold text-white">${esc(item.strike)}</div>
            <div class="text-right text-purple-400">${isNum(item.live_iv) ? fmt(item.live_iv * 100, 1, '', '%') : DASH}</div>
            <div class="text-right text-zinc-500">${isNum(item.hist_iv) ? fmt(item.hist_iv * 100, 1, '', '%') : DASH}</div>
            <div class="text-right pr-2 ${item.bleed > 0.2 ? 'text-red-500 font-bold' : 'text-zinc-400'}">${isNum(item.bleed) ? fmt(item.bleed * 100, 1, '', '%') : DASH}</div>
        </div>
    `).join('');
}

function updateProbMatrix(probs, ticker, reason) {
    const body = document.getElementById('probMatrixBody');
    const optimalText = document.getElementById('probOptimalText');

    if (!body) return;

    if (!Array.isArray(probs) || probs.length === 0) {
        body.innerHTML = `<tr><td colspan="4" class="p-8 text-center text-zinc-700 uppercase tracking-widest font-sans">${esc(reason || 'No data available.')}</td></tr>`;
        if (optimalText) optimalText.innerText = reason || 'No optimal strike identified for the current volatility regime.';
        return;
    }

    const pct = v => isNum(v) ? fmt(v * 100, 1, '', '%') : DASH;
    body.innerHTML = probs.map(p => `
        <tr class="border-b border-zinc-900/50 hover:bg-zinc-900/30">
            <td class="p-2 pl-3 font-bold text-blue-400">${esc(p.strike)}</td>
            <td class="p-2 text-right">${pct(p.prob_3d)}</td>
            <td class="p-2 text-right">${pct(p.prob_5d)}</td>
            <td class="p-2 text-right pr-3">${pct(p.prob_7d)}</td>
        </tr>
    `).join('');

    if (optimalText) {
        // the text names the first row (as before); the ticker is the one that was scanned, not a fixed SPY
        const first = probs[0];
        optimalText.innerText = first && isNum(first.prob_3d)
            ? `${ticker || DASH} $${first.strike} Strike | ${(first.prob_3d * 100).toFixed(1)}% Base Probability | Dealer Trap Multiplier Active.`
            : (reason || 'No optimal strike identified for the current volatility regime.');
    }
}

// --- NEW INSTITUTIONAL UI RENDERERS ---

let trapdoorProfileChartInstance = null;
function renderTrapdoorProfileChart(profileData, reason) {
    const ctx = document.getElementById('trapdoorProfileChart')?.getContext('2d');
    if (!ctx) return;
    
    if (trapdoorProfileChartInstance) { trapdoorProfileChartInstance.destroy(); trapdoorProfileChartInstance = null; }
    if (!Array.isArray(profileData) || profileData.length === 0) { setChartNote('trapdoorProfileChart', reason || 'No vanna / charm profile'); return; }
    setChartNote('trapdoorProfileChart', '');
    
    profileData.sort((a, b) => a.strike - b.strike);
    
    const labels = profileData.map(d => d.strike);
    const vanna = profileData.map(d => toNum(d.vanna));     // a null stays a gap in the chart, not a zero bar
    const charm = profileData.map(d => toNum(d.charm));
    
    trapdoorProfileChartInstance = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'Vanna Exposure',
                    data: vanna,
                    backgroundColor: 'rgba(168, 85, 247, 0.5)',
                    borderColor: '#a855f7',
                    borderWidth: 1,
                    yAxisID: 'y'
                },
                {
                    label: 'Charm Exposure',
                    data: charm,
                    type: 'line',
                    borderColor: '#fbbf24',
                    borderWidth: 2,
                    tension: 0.3,
                    pointRadius: 2,
                    yAxisID: 'y1'
                }
            ]
        },
        options: {
            devicePixelRatio: 3,
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { labels: { color: '#a1a1aa', boxWidth: 10, font: { size: 9 } } }
            },
            scales: {
                x: { grid: { display: false }, ticks: { color: '#a1a1aa', font: { size: 9 } } },
                y: {
                    position: 'left',
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    ticks: { color: '#a855f7', font: { size: 9 } }
                },
                y1: {
                    position: 'right',
                    grid: { display: false },
                    ticks: { color: '#fbbf24', font: { size: 9 } }
                }
            }
        }
    });
}

let termStructureChartInstance = null;
function renderTermStructure(termData, reason) {
    const ctx = document.getElementById('termStructureChart')?.getContext('2d');
    if (!ctx) return;
    
    if (termStructureChartInstance) { termStructureChartInstance.destroy(); termStructureChartInstance = null; }
    // an expiry with no implied volatility is left out, never drawn as 0%
    termData = Array.isArray(termData) ? termData.filter(d => isNum(d.iv) && isNum(d.days)) : [];
    if (termData.length === 0) { setChartNote('termStructureChart', reason || 'No term structure'); return; }
    setChartNote('termStructureChart', '');
    
    termData.sort((a, b) => a.days - b.days);
    
    const labels = termData.map(d => `${d.days}D`);
    const ivs = termData.map(d => d.iv * 100);
    
    const isBackwardation = ivs.length >= 2 && ivs[0] > ivs[ivs.length - 1];
    const color = isBackwardation ? '#f43f5e' : '#38bdf8'; 
    
    termStructureChartInstance = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'ATM Implied Volatility (%)',
                data: ivs,
                borderColor: color,
                backgroundColor: `${color}22`,
                fill: true,
                borderWidth: 2,
                tension: 0.3,
                pointRadius: 4,
                pointBackgroundColor: '#000',
                pointBorderColor: color
            }]
        },
        options: {
            devicePixelRatio: 3,
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: function(context) {
                            return `IV: ${context.parsed.y.toFixed(2)}%`;
                        }
                    }
                }
            },
            scales: {
                x: { grid: { display: false }, ticks: { color: '#a1a1aa', font: { size: 10 } } },
                y: {
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    ticks: { color: '#a1a1aa', font: { size: 10 }, callback: v => v + '%' }
                }
            }
        }
    });
}

function updateIvHvSpread(spreadData, reasons) {
    const elRealized = document.getElementById('spreadRealized');
    const elImplied = document.getElementById('spreadImplied');
    const elStatus = document.getElementById('spreadStatus');
    
    if (!elRealized || !elImplied || !elStatus) return;
    spreadData = spreadData || {};
    reasons = reasons || {};
    
    const hv = isNum(spreadData.realized_volatility_20d) ? spreadData.realized_volatility_20d * 100 : null;
    const iv = isNum(spreadData.atm_implied_volatility) ? spreadData.atm_implied_volatility * 100 : null;
    
    elRealized.innerText = hv === null ? DASH : hv.toFixed(1) + '%';
    elRealized.title = hv === null ? (reasons.realized || 'realized volatility not available') : '';
    elImplied.innerText = iv === null ? DASH : iv.toFixed(1) + '%';
    elImplied.title = iv === null ? (reasons.implied || 'implied volatility not available') : '';
    
    if (hv === null || iv === null) {
        elStatus.innerText = `SPREAD UNAVAILABLE${(hv === null ? reasons.realized : reasons.implied) ? ': ' + (hv === null ? reasons.realized : reasons.implied) : ''}`;
        elStatus.className = "bg-zinc-950 border border-zinc-800 rounded p-4 text-center font-bold text-[12px] uppercase tracking-widest text-zinc-600";
        return;
    }
    
    const diff = iv - hv;
    
    if (diff > 5) {
        elStatus.innerText = `IV OVERPRICED (+${diff.toFixed(1)}%) - SELL PREMIUM`;
        elStatus.className = "bg-red-900/20 border border-red-500 rounded p-4 text-center font-bold text-[12px] uppercase tracking-widest text-red-500";
    } else if (diff < -5) {
        elStatus.innerText = `IV UNDERPRICED (${diff.toFixed(1)}%) - BUY PREMIUM`;
        elStatus.className = "bg-green-900/20 border border-green-500 rounded p-4 text-center font-bold text-[12px] uppercase tracking-widest text-green-500";
    } else {
        elStatus.innerText = "VOLATILITY SPREAD NEUTRAL";
        elStatus.className = "bg-zinc-950 border border-zinc-800 rounded p-4 text-center font-bold text-[12px] uppercase tracking-widest text-zinc-500";
    }
}

// ==========================================
// --- LIVE OPTION EXPLORER ---
// ==========================================

let _explorerType = 'call';
let _explorerChainData = null;
let _explorerCurrentTicker = '';
let _explorerCurrentExp = '';
let _lastAnalyticsData = null; // Stores most recent quant result for watchlist adding

function setExplorerType(type) {
    _explorerType = type;
    document.getElementById('explorerCallBtn').className =
        `px-3 py-1 text-[10px] font-bold border border-zinc-700 rounded-l transition-all ${type === 'call' ? 'bg-indigo-600 text-white' : 'bg-zinc-900 text-zinc-500'}`;
    document.getElementById('explorerPutBtn').className =
        `px-3 py-1 text-[10px] font-bold border border-zinc-700 rounded-r transition-all ${type === 'put' ? 'bg-rose-600 text-white' : 'bg-zinc-900 text-zinc-500'}`;
    renderChainTable();
}

async function loadOptionChain(expChangeOnly = false) {
    const ticker = document.getElementById('explorerTicker').value.trim().toUpperCase();
    if (!ticker) return;
    const expiry = document.getElementById('explorerExpiry').value;
    const chainBody = document.getElementById('explorerChainBody');
    chainBody.innerHTML = `<div class="p-8 text-center text-zinc-600 text-[10px] uppercase tracking-widest animate-pulse">Fetching ${esc(ticker)} chain...</div>`;

    try {
        let url = `${API_BASE}/api/option_chain?ticker=${encodeURIComponent(ticker)}`;
        if (expiry) url += `&expiration=${encodeURIComponent(expiry)}`;
        const result = await fetchJson(url);
        if (result.status !== 'success' || !result.data) {
            chainBody.innerHTML = `<div class="p-8 text-center text-red-500 text-[10px]">${esc(result.message || 'Option chain unavailable')}</div>`;
            return;
        }
        const data = result.data;
        _explorerChainData = data;
        _explorerCurrentTicker = data.ticker;
        _explorerCurrentExp = data.selected_expiration;

        // Populate spot
        document.getElementById('explorerSpotPrice').innerText = fmt(data.spot, 2, '$');

        // Populate expiry dropdown (only if fresh ticker load)
        if (!expChangeOnly || document.getElementById('explorerExpiry').options.length <= 1) {
            const sel = document.getElementById('explorerExpiry');
            sel.innerHTML = (data.expirations || []).map(e =>
                `<option value="${esc(e)}" ${e === data.selected_expiration ? 'selected' : ''}>${esc(e)}</option>`
            ).join('');
        }

        renderChainTable();
    } catch (e) {
        chainBody.innerHTML = `<div class="p-8 text-center text-red-500 text-[10px]">Error: ${esc(e.message)}</div>`;
    }
}

function renderChainTable() {
    const chainBody = document.getElementById('explorerChainBody');
    if (!_explorerChainData) return;
    const contracts = _explorerType === 'call' ? _explorerChainData.calls : _explorerChainData.puts;
    const spot = toNum(_explorerChainData.spot);

    if (!contracts || contracts.length === 0) {
        chainBody.innerHTML = '<div class="p-8 text-center text-zinc-700 text-[10px]">No contracts found.</div>';
        return;
    }

    chainBody.innerHTML = contracts.map(c => {
        // a quote the feed did not give is null: shown as a dash (and 0 is sent when the row is clicked, which asks for the chain's own price)
        const isATM = spot !== null && Math.abs(c.strike - spot) / spot < 0.01;
        const isITM = spot !== null && ((_explorerType === 'call') ? c.strike < spot : c.strike > spot);
        const rowBg = isATM ? 'bg-indigo-900/20' : isITM ? 'bg-zinc-900/40' : '';
        const strikeColor = isATM ? 'text-indigo-300 font-bold' : isITM ? 'text-white' : 'text-zinc-400';
        const ivColor = !isNum(c.iv) ? 'text-zinc-600' : c.iv > 0.5 ? 'text-red-400' : c.iv > 0.3 ? 'text-amber-400' : 'text-green-400';
        return `<div onclick="selectContract(${Number(c.strike)}, '${esc(_explorerCurrentExp)}', '${_explorerType}', ${isNum(c.last) ? Number(c.last) : 0})"
            class="grid grid-cols-6 text-[10px] px-3 py-1.5 border-b border-zinc-900/50 hover:bg-indigo-900/20 cursor-pointer transition-colors ${rowBg}">
            <div class="${strikeColor}">${esc(c.strike)}${isATM ? ' ◀' : ''}</div>
            <div class="text-right text-zinc-300 font-mono">${fmt(c.last, 2)}</div>
            <div class="text-right text-zinc-500 font-mono">${fmt(c.bid, 2)}</div>
            <div class="text-right text-zinc-500 font-mono">${fmt(c.ask, 2)}</div>
            <div class="text-right ${ivColor} font-mono">${isNum(c.iv) ? fmt(c.iv * 100, 1, '', '%') : DASH}</div>
            <div class="text-right text-zinc-600 font-mono">${isNum(c.oi) ? Number(c.oi).toLocaleString() : DASH}</div>
        </div>`;
    }).join('');
}

// The result fields of /api/option_calc and the element that shows each. A null field shows a dash; data.missing says why.
const EX_FIELDS = [
    ['exMoneyness', d => d.moneyness ?? DASH],
    ['exMarketPx', d => fmt(d.market_price, 2, '$')],
    ['exBSPx', d => fmt(d.bs_price, 2, '$')],
    ['exProbITM', d => fmt(d.prob_itm, 1, '', '%')],
    ['exProbOTM', d => fmt(d.prob_otm, 1, '', '%')],
    ['exDelta', d => fmt(d.delta, 4)],
    ['exGamma', d => fmt(d.gamma, 6)],
    ['exTheta', d => fmt(d.theta, 4, '$')],
    ['exVega', d => fmt(d.vega, 4, '$')],
    ['exRho', d => fmt(d.rho, 4, '$')],
    ['exBreakeven', d => fmt(d.breakeven, 2, '$')],
    ['exExpMove', d => fmt(d.expected_move, 2, '±$')],
    ['exIntrinsic', d => fmt(d.intrinsic, 4, '$')],
    ['exExtrinsic', d => fmt(d.extrinsic, 4, '$')],
    ['exIV', d => fmt(d.iv_pct, 1, '', '%')],
    ['exHV', d => fmt(d.hv_pct, 1, '', '%')],
];

async function selectContract(strike, expiration, type, marketPrice) {
    const ticker = _explorerCurrentTicker;
    document.getElementById('explorerMetricsEmpty').classList.add('hidden');
    document.getElementById('explorerMetrics').classList.remove('hidden');
    document.getElementById('explorerMetrics').classList.add('flex');
    document.getElementById('explorerContractLabel').innerText = `${ticker} $${strike} ${type.toUpperCase()} exp ${expiration}`;
    // clear the figures of the previous contract so they never sit next to this contract's label
    EX_FIELDS.forEach(([id]) => { const el = document.getElementById(id); if (el) el.innerText = ''; });
    document.getElementById('exProbITM').innerText = '...';
    const signal = document.getElementById('exIVSignal');
    signal.innerText = 'LOADING...';
    signal.className = 'p-3 text-center font-bold text-[11px] uppercase tracking-widest bg-zinc-950 border-t border-zinc-900 text-zinc-600';
    _lastAnalyticsData = null;

    try {
        const url = `${API_BASE}/api/option_calc?ticker=${encodeURIComponent(ticker)}&strike=${encodeURIComponent(strike)}&expiration=${encodeURIComponent(expiration)}&type=${encodeURIComponent(type)}&market_price=${encodeURIComponent(marketPrice)}`;
        const result = await fetchJson(url);
        if (result.status !== 'success' || !result.data) {
            // for example the 422 that says the chain has no implied volatility for this contract
            EX_FIELDS.forEach(([id]) => { const el = document.getElementById(id); if (el) el.innerText = DASH; });
            signal.innerText = result.message || 'Analytics unavailable';
            signal.className = 'p-3 text-center font-bold text-[11px] tracking-wide bg-zinc-950 border-t border-zinc-900 text-red-500';
            return;
        }
        const d = result.data;
        document.getElementById('explorerContractLabel').innerText = `${d.option_type || type.toUpperCase()} ${ticker} $${strike} | Exp ${expiration} | Spot ${fmt(d.spot, 2, '$')}`;
        document.getElementById('exDays').innerText = isNum(d.days_to_exp) ? `${d.days_to_exp} days` : DASH;
        EX_FIELDS.forEach(([id, f]) => { const el = document.getElementById(id); if (el) el.innerText = f(d); });
        // the IV vs HV banner: the signal, or the reason there is none
        const missingMap = (d.missing && typeof d.missing === 'object') ? d.missing : {};
        const why = missingMap.hv_pct || missingMap.iv_signal || Object.values(missingMap)[0];   // the root cause first
        if (d.iv_signal) {
            signal.innerText = d.iv_signal;
            signal.className = `p-3 text-center font-bold text-[11px] uppercase tracking-widest bg-zinc-950 border-t border-zinc-900 ${/OVERPRICED/.test(d.iv_signal) ? 'text-red-400' : /UNDERPRICED/.test(d.iv_signal) ? 'text-green-400' : 'text-zinc-400'}`;
        } else {
            signal.innerText = why ? `IV vs HV unavailable: ${why}` : 'IV vs HV unavailable';
            signal.className = 'p-3 text-center font-bold text-[11px] tracking-wide bg-zinc-950 border-t border-zinc-900 text-zinc-500';
        }
        signal.title = Object.entries(missingMap).map(([k, v]) => `${k}: ${v}`).join('\n');
        _lastAnalyticsData = d; // Store for watchlist
    } catch(e) {
        EX_FIELDS.forEach(([id]) => { const el = document.getElementById(id); if (el) el.innerText = DASH; });
        signal.innerText = `Error: ${e.message}`;
        signal.className = 'p-3 text-center font-bold text-[11px] tracking-wide bg-zinc-950 border-t border-zinc-900 text-red-500';
    }
}

// ==========================================
// --- INSTITUTIONAL WATCHLIST ---
// ==========================================

function readWatchlist() {
    try { return JSON.parse(localStorage.getItem('optionsWatchlist') || '[]'); }
    catch (e) { return []; }
}
function writeWatchlist(list) {
    try { localStorage.setItem('optionsWatchlist', JSON.stringify(list)); }
    catch (e) { log(`The wishlist could not be saved in this browser: ${e.message}`, 'error'); }
}

function addToWatchlist() {
    if (!_lastAnalyticsData) {
        alert("Select an option first to generate analytics.");
        return;
    }

    const watchlist = readWatchlist();
    
    // Create a unique ID
    const id = `${_explorerCurrentTicker}_${_lastAnalyticsData.strike}_${_lastAnalyticsData.option_type}_${_lastAnalyticsData.expiration}`;
    
    // Check if already exists
    if (watchlist.find(item => item.id === id)) {
        alert("This contract is already in your watchlist.");
        return;
    }

    const newItem = {
        id: id,
        ticker: _explorerCurrentTicker,
        strike: _lastAnalyticsData.strike,
        type: _lastAnalyticsData.option_type,
        expiry: _lastAnalyticsData.expiration,
        addedPrice: toNum(_lastAnalyticsData.market_price),   // null when the chain had no price: Change (%) then shows a dash
        addedDate: new Date().toLocaleString(),
        analytics: _lastAnalyticsData,
        timestamp: Date.now()
    };

    watchlist.push(newItem);
    writeWatchlist(watchlist);
    renderWatchlist();
    refreshWatchlistPrices();
    
    // UI Feedback
    const btn = event.target;
    const originalText = btn.innerText;
    btn.innerText = "✅ ADDED TO WATCHLIST";
    btn.className = "w-full bg-green-600 text-white font-bold py-3 rounded text-[11px] uppercase tracking-widest transition-all";
    setTimeout(() => {
        btn.innerText = originalText;
        btn.className = "w-full bg-blue-600 hover:bg-blue-500 text-white font-bold py-3 rounded text-[11px] uppercase tracking-widest transition-all shadow-lg shadow-blue-900/20";
    }, 2000);
}

// Current price of each saved contract: GET /api/option_calc with market_price=0 answers the chain's last price as market_price.
// _wlLive holds the latest answer per contract id: { px, at } or { error, at }. An answer younger than WL_FRESH_MS is reused.
const WL_FRESH_MS = 60 * 1000;
const _wlLive = {};
let _wlRefreshing = false;
let _wlRefreshAgain = false;

const wlExpired = item => !!item.expiry && item.expiry < new Date().toISOString().slice(0, 10);

function wlCells(item) {
    const live = _wlLive[item.id];
    const added = toNum(item.addedPrice);
    const px = live && live.px !== undefined ? toNum(live.px) : null;
    let liveHtml;
    if (px !== null) liveHtml = `<span class="text-white">${fmt(px, 2, '$')}</span>`;
    else if (wlExpired(item)) liveHtml = `<span class="text-zinc-600" title="the contract has expired">${DASH}</span>`;
    else if (live && live.error) liveHtml = `<span class="text-zinc-600" title="${esc(live.error)}">${DASH}</span>`;
    else liveHtml = `<span class="text-zinc-600" title="${_wlRefreshing ? 'loading' : 'not loaded yet: it loads while this tab is open'}">${_wlRefreshing ? '…' : DASH}</span>`;
    let changeHtml = `<span class="text-zinc-600">${DASH}</span>`;
    if (px !== null && added !== null && added > 0) {
        const change = (px - added) / added * 100;
        changeHtml = `<span class="${change >= 0 ? 'text-green-400' : 'text-red-400'}">${change.toFixed(2)}%</span>`;
    }
    return { liveHtml, changeHtml };
}

function renderWatchlist() {
    const body = document.getElementById('wishlistBody');
    if (!body) return;
    const watchlist = readWatchlist();

    if (watchlist.length === 0) {
        body.innerHTML = `<tr><td colspan="6" class="p-8 text-center text-zinc-700 uppercase tracking-widest">Wishlist is empty</td></tr>`;
        return;
    }

    body.innerHTML = watchlist.map((item, index) => {
        const { liveHtml, changeHtml } = wlCells(item);
        const added = toNum(item.addedPrice);
        
        return `
            <tr class="border-b border-zinc-900/50 hover:bg-zinc-900/30 transition-colors group" data-wl-id="${esc(item.id)}">
                <td class="py-3 px-1">
                    <div class="font-bold text-white">${esc(item.ticker)} $${esc(item.strike)} ${esc(item.type)}</div>
                    <div class="text-[8px] text-zinc-500">${esc(item.expiry)}</div>
                </td>
                <td class="text-right font-mono text-zinc-400">${fmt(added, 2, '$')}</td>
                <td class="text-right font-mono wl-live">${liveHtml}</td>
                <td class="text-right font-mono wl-change">${changeHtml}</td>
                <td class="text-right text-zinc-500">${esc(item.addedDate)}</td>
                <td class="text-right">
                    <div class="flex justify-end gap-2">
                        <button onclick="showWatchlistDetail('${esc(item.id)}')" class="bg-zinc-800 hover:bg-zinc-700 text-zinc-300 px-2 py-1 rounded text-[8px] uppercase font-bold">Details</button>
                        <button onclick="removeFromWatchlist('${esc(item.id)}')" class="bg-red-900/20 hover:bg-red-900/40 text-red-500 px-2 py-1 rounded text-[8px] uppercase font-bold">Remove</button>
                    </div>
                </td>
            </tr>
        `;
    }).join('');
}

// Fetches the current price of every saved contract that has no answer younger than a minute (all of them when `force`).
// One contract at a time: each call reads a live option chain. The rows are updated in place as the answers arrive.
async function refreshWatchlistPrices(force = false) {
    if (_wlRefreshing) { _wlRefreshAgain = true; return; }   // a contract added meanwhile is picked up when this pass ends
    const items = readWatchlist().filter(item => !wlExpired(item));
    const due = items.filter(item => force || !_wlLive[item.id] || Date.now() - _wlLive[item.id].at > WL_FRESH_MS);
    if (due.length === 0) return;
    _wlRefreshing = true;
    try {
        for (const item of due) {
            try {
                const url = `${API_BASE}/api/option_calc?ticker=${encodeURIComponent(item.ticker)}&strike=${encodeURIComponent(item.strike)}`
                    + `&expiration=${encodeURIComponent(item.expiry)}&type=${encodeURIComponent(String(item.type).toLowerCase())}&market_price=0`;
                const r = await fetchJson(url);
                const px = r.status === 'success' && r.data ? toNum(r.data.market_price) : null;
                _wlLive[item.id] = px !== null ? { px, at: Date.now() }
                    : { error: r.status === 'success' ? 'the chain has no price for this contract' : (r.message || 'price unavailable'), at: Date.now() };
            } catch (e) {
                _wlLive[item.id] = { error: e.message, at: Date.now() };
            }
            // update this row in place (the list may have changed while the request was out)
            const row = [...document.querySelectorAll('#wishlistBody tr[data-wl-id]')].find(tr => tr.dataset.wlId === item.id);
            if (row) {
                const { liveHtml, changeHtml } = wlCells(item);
                row.querySelector('.wl-live').innerHTML = liveHtml;
                row.querySelector('.wl-change').innerHTML = changeHtml;
            }
        }
    } finally {
        _wlRefreshing = false;
        if (_wlRefreshAgain) { _wlRefreshAgain = false; setTimeout(() => refreshWatchlistPrices(), 0); }
    }
}

function removeFromWatchlist(id) {
    let watchlist = readWatchlist();
    watchlist = watchlist.filter(item => item.id !== id);
    writeWatchlist(watchlist);
    delete _wlLive[id];
    renderWatchlist();
    
    // If we are in the detail view, close it
    if (!document.getElementById('helpModal').classList.contains('hidden')) {
        closeHelpModal();
    }
}

function showWatchlistDetail(id) {
    const item = readWatchlist().find(i => i.id === id);
    if (!item) return;

    const d = item.analytics || {};
    const title = `${item.ticker} $${item.strike} ${item.type} | Exp ${item.expiry}`;
    
    const html = `
        <div class="space-y-4">
            <div class="grid grid-cols-2 gap-4">
                <div class="bg-zinc-900/50 p-3 rounded border border-zinc-800">
                    <div class="text-[8px] text-zinc-500 uppercase">Prob. ITM</div>
                    <div class="text-xl font-bold text-green-400">${fmt(d.prob_itm, 1, '', '%')}</div>
                </div>
                <div class="bg-zinc-900/50 p-3 rounded border border-zinc-800">
                    <div class="text-[8px] text-zinc-500 uppercase">Prob. OTM</div>
                    <div class="text-xl font-bold text-red-400">${fmt(d.prob_otm, 1, '', '%')}</div>
                </div>
            </div>
            
            <div class="grid grid-cols-3 gap-2">
                <div class="text-center">
                    <div class="text-[7px] text-zinc-500 uppercase">Delta</div>
                    <div class="text-xs font-mono text-white">${fmt(d.delta, 4)}</div>
                </div>
                <div class="text-center">
                    <div class="text-[7px] text-zinc-500 uppercase">Gamma</div>
                    <div class="text-xs font-mono text-white">${fmt(d.gamma, 6)}</div>
                </div>
                <div class="text-center">
                    <div class="text-[7px] text-zinc-500 uppercase">Theta</div>
                    <div class="text-xs font-mono text-white">${fmt(d.theta, 4)}</div>
                </div>
            </div>

            <div class="pt-4 border-t border-zinc-800">
                <div class="flex justify-between text-[10px] mb-1">
                    <span class="text-zinc-500 uppercase">Breakeven</span>
                    <span class="text-white font-mono">${fmt(d.breakeven, 2, '$')}</span>
                </div>
                <div class="flex justify-between text-[10px] mb-1">
                    <span class="text-zinc-500 uppercase">Expected Move</span>
                    <span class="text-white font-mono">${fmt(d.expected_move, 2, '±$')}</span>
                </div>
                <div class="flex justify-between text-[10px] mb-1">
                    <span class="text-zinc-500 uppercase">Intrinsic Val</span>
                    <span class="text-green-400 font-mono">${fmt(d.intrinsic, 4, '$')}</span>
                </div>
                <div class="flex justify-between text-[10px]">
                    <span class="text-zinc-500 uppercase">Extrinsic Val</span>
                    <span class="text-purple-400 font-mono">${fmt(d.extrinsic, 4, '$')}</span>
                </div>
            </div>

            <div class="mt-6">
                <button onclick="removeFromWatchlist('${esc(id)}')" class="w-full bg-red-900/20 hover:bg-red-900/40 text-red-500 font-bold py-2 rounded text-[10px] uppercase tracking-widest transition-all">
                    Remove from Watchlist
                </button>
            </div>
        </div>
    `;

    document.getElementById('helpModalTitle').innerText = title;
    document.getElementById('helpModalBody').innerHTML = html;
    document.getElementById('helpModal').classList.remove('hidden');
    document.getElementById('modalBackdrop').classList.remove('hidden');
}

// ==========================================
// --- HELP MODAL INTELLIGENCE SYSTEM ---
// ==========================================
// Help texts for the Time Arbitrage panels (the "?" buttons). The Macro Direction panels have no help button.
const panelHelp = {
    // Time Arbitrage Tab
    "arbPanel1": {
        title: "Capacity Constraint Oscillator",
        desc: "A multi-factor Z-Score that measures market 'extension'. When the oscillator is at extremes (Red/Green), the market is capacity constrained and a reversal or squeeze is likely. Trading near 'Neutral' (Yellow) has lower probability of success."
    },
    "arbPanel2": {
        title: "Dealer Trapdoor",
        desc: "Monitors the 'Zero Gamma' level. If price falls below Zero Gamma, market makers move from 'Long Gamma' (stabilizing) to 'Short Gamma' (destabilizing), leading to explosive volatility. 'Approach Velocity' measures how fast we are hitting this trapdoor."
    },
    "arbPanel3": {
        title: "IV Premium Bleed",
        desc: "Compares live Implied Volatility (IV) to historical norms. High 'Bleed %' means premiums are overpriced (Retail Trap). Look for low bleed levels to enter asymmetric long positions cheaply."
    },
    "arbPanel4": {
        title: "Asymmetric Probability Matrix",
        desc: "Uses log-normal distribution math to calculate the statistical probability of a ticker hitting specific strikes within 3, 5, and 7 days. Focus on strikes with > 60% probability for conservative trades, or < 15% for asymmetric 'lotto' plays."
    },
    "arbPanel5": {
        title: "Dealer Pin Map",
        desc: "Visualizes the distribution of Vanna and Charm across specific strikes. Dealers are naturally drawn to high-concentration strikes (Dealer Pins) as they hedge their exposures."
    },
    "arbPanel6": {
        title: "Volatility Term Structure",
        desc: "Plots the At-The-Money Implied Volatility across upcoming expirations. If the curve points down (Backwardation), the market is heavily pricing in an immediate crash. If it points up (Contango), conditions are normal."
    },
    "arbPanel7": {
        title: "IV / HV Spread",
        desc: "Compares current Implied Volatility against actual historical (realized) volatility. A wide positive spread means options are overpriced (ideal for selling premium), while a negative spread means options are underpriced (ideal for buying premium)."
    },
    "arbPanel8": {
        title: "Live Option Explorer",
        desc: "A full yfinance-powered option chain browser. Select any ticker, expiration date, and call/put type to browse all available contracts. Click any row to run the full Black-Scholes quant engine on that specific contract — outputting probability of expiring ITM/OTM, all 5 Greeks (Delta, Gamma, Theta, Vega, Rho), breakeven price, intrinsic vs extrinsic value, expected move at expiry, and a live IV vs HV premium signal to determine if options are over or underpriced."
    },
    "arbPanel9": {
        title: "Institutional Wishlist",
        desc: "A persistence-layer for high-conviction option trades. When you identify a contract via the Explorer, adding it here allows you to track its 'Live Price' vs your 'Added Price' (Yield Monitoring). This panel serves as a tactical bridge between quantitative discovery and actual trade execution, maintaining full snapshot analytics (Greeks, Probabilities) for every saved contract."
    }
};

function openHelpModal(panelId) {
    const data = panelHelp[panelId];
    if (!data) return;

    document.getElementById('helpModalTitle').innerText = data.title;
    document.getElementById('helpModalBody').innerHTML = `<p class="mb-4">${data.desc}</p>
    <div class="mt-6 pt-4 border-t border-zinc-900">
        <h4 class="text-[10px] font-bold text-amber-500 uppercase tracking-widest mb-2">Tactical Application:</h4>
        <ul class="list-disc list-inside text-[10px] space-y-1">
            <li>Monitor for trend alignment across 3+ panels.</li>
            <li>Identify structural asymmetries before technical entry.</li>
            <li>Use to validate or invalidate institutional whale trades.</li>
        </ul>
    </div>`;
    
    document.getElementById('helpModal').classList.remove('hidden');
    document.getElementById('modalBackdrop').classList.remove('hidden');
}

function closeHelpModal() {
    document.getElementById('helpModal').classList.add('hidden');
    document.getElementById('modalBackdrop').classList.add('hidden');
}


// ==========================================
// --- PANEL 15: TRACKED ENGINE POSITIONS ---
// ==========================================
window._positionsData = null;

function _fmtNum(v, d = 2, pre = '') {
    return (v === null || v === undefined) ? '—' : pre + Number(v).toFixed(d);
}

function _signCls(v) {
    return (v === null || v === undefined) ? 'text-zinc-500' : (v > 0 ? 'text-green-400' : v < 0 ? 'text-red-400' : 'text-zinc-300');
}

async function loadPositions() {
    const body = document.getElementById('positionsBody');
    if (!body) return;
    try {
        const json = await fetchJson(`${API_BASE}/api/positions`);   // a 503 body says the database is not set up yet
        if (json.status !== 'success') throw new Error(json.message || 'load failed');
        window._positionsData = json.data;
        renderPositions();
    } catch (e) {
        body.innerHTML = `<tr><td colspan="9" class="py-3 text-red-400">Positions unavailable: ${esc(e.message)}</td></tr>`;
    }
}

function renderPositions() {
    const body = document.getElementById('positionsBody');
    const json = { data: window._positionsData || [] };
    {
        document.getElementById('positionsCount').textContent = `(${json.data.length})`;
        if (!json.data.length) {
            body.innerHTML = '<tr><td colspan="9" class="py-3 text-zinc-600">No tracked positions yet — they appear after each pipeline run.</td></tr>';
            return;
        }
        body.innerHTML = json.data.map(p => {
            const d = new Date(p.created_at);
            const pad = n => String(n).padStart(2, '0');
            let hours12 = d.getHours() % 12;
            if (hours12 === 0) hours12 = 12;
            const ampm = d.getHours() >= 12 ? 'pm' : 'am';
            const when = `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${hours12}:${pad(d.getMinutes())}${ampm}`;
            const isCash = p.position_type === 'CASH';
            const typeCls = p.position_type === 'CALL' ? 'text-green-400' : p.position_type === 'PUT' ? 'text-red-400' : 'text-zinc-400';
            const exp = p.expiration ? p.expiration.slice(5).replace('-', '/') : '';
            const src = p.source === 'email_archive' ? '<span class="text-zinc-600" title="recovered from sent report email">✉</span> ' : '';
            const position = isCash ? `${src}<span class="font-bold ${typeCls}">CASH</span>`
                : `${src}<span class="font-bold ${typeCls}">${p.position_type}</span> $${_fmtNum(p.strike, 0)} · ${exp} `
                  + (p.expired ? '<span class="text-amber-500">EXP</span>' : `<span class="text-zinc-600">${p.dte}d</span>`);
            const pctTxt = v => `${v > 0 ? '+' : ''}${Number(v).toFixed(Math.abs(v) >= 100 ? 0 : 1)}%`;
            const hz = h => {
                if (!h || h.basis === 'pending') return `<span class="text-zinc-700" title="${h ? 'due ' + h.target : ''}">…</span>`;
                if (h.change_pct === null || h.change_pct === undefined) return `<span class="text-zinc-600" title="${h.basis}">—</span>`;
                const est = h.basis === 'model' ? '~' : '';
                const what = h.basis === 'model' ? 'model estimate (BS, IV implied by entry price)'
                    : h.basis === 'market' ? 'recorded market mid' : h.basis === 'expiry_intrinsic' ? 'intrinsic at expiration close' : 'SPY move';
                const val = h.value !== undefined && h.value !== null ? `$${Number(h.value).toFixed(2)} · ` : '';
                return `<span class="${_signCls(h.change_pct)}" title="${val}${h.date} · SPY ${_fmtNum(h.spy_close, 2)} · ${what}">${est}${pctTxt(h.change_pct)}</span>`;
            };
            const H = p.horizons || {};
            const now = isCash
                ? `<span class="${_signCls(p.underlying_change_pct)}" title="SPY ${_fmtNum(p.underlying_price, 2)} → ${_fmtNum(p.underlying_now, 2)}">${p.underlying_change_pct == null ? '—' : pctTxt(p.underlying_change_pct)}</span>`
                : p.expired ? '<span class="text-amber-500" title="expired — final value is the expiry intrinsic">EXP</span>'
                : `<span class="${_signCls(p.change_pct)}" title="now ${_fmtNum(p.now_mid, 2, '$')} (entry ${_fmtNum(p.entry_mid, 2, '$')}) · SPY ${_fmtNum(p.underlying_now, 2)}">${p.change_pct == null ? '—' : pctTxt(p.change_pct)}</span>`;
            const tip = `${p.contract || 'CASH'} | score ${p.score} | ${p.allocation || ''} | ${p.bias || ''}${p.price_basis ? ' | ' + p.price_basis : ''}`.replace(/"/g, '&quot;');
            return `<tr class="border-b border-zinc-900 hover:bg-zinc-900/40" title="${tip}">
                <td class="py-1 pr-1"><button onclick="togglePositionStar(${p.signal_id})" class="${p.starred ? 'text-yellow-400' : 'text-zinc-600 hover:text-yellow-400'}">${p.starred ? '★' : '☆'}</button></td>
                <td class="pr-2 text-zinc-500 whitespace-nowrap" title="${d.toLocaleString()}">${when}</td>
                <td class="pr-2 whitespace-nowrap">${position}</td>
                <td class="pr-2 text-right">${isCash ? `<span class="text-zinc-600" title="SPY at signal">${_fmtNum(p.underlying_price, 0)}</span>` : _fmtNum(p.entry_mid, 2, '$')}</td>
                <td class="pr-2 text-right whitespace-nowrap">${hz(H['1d'])}</td>
                <td class="pr-2 text-right whitespace-nowrap">${hz(H['1w'])}</td>
                <td class="pr-2 text-right whitespace-nowrap">${hz(H['2w'])}</td>
                <td class="pr-1 text-right whitespace-nowrap">${now}</td>
                <td><button onclick="deletePosition(${p.signal_id})" class="text-zinc-600 hover:text-red-400" title="Stop tracking">✕</button></td>
            </tr>`;
        }).join('');
    }
}

async function togglePositionStar(id) {
    try {
        const json = await fetchJson(`${API_BASE}/api/positions/${id}/star`, { method: 'POST' });
        if (json.status !== 'success') { log(`Star failed: ${json.message || 'error'}`, 'error'); return; }
        const p = (window._positionsData || []).find(x => x.signal_id === id);
        if (p) { p.starred = json.starred ? 1 : 0; renderPositions(); }
    } catch (e) { log(`Star failed: ${e.message}`, 'error'); }
}

async function deletePosition(id) {
    const p = (window._positionsData || []).find(x => x.signal_id === id);
    if (!confirm(`Stop tracking ${p ? (p.contract || 'CASH') + ' from ' + new Date(p.created_at).toLocaleString() : 'this position'}?`)) return;
    try {
        const json = await fetchJson(`${API_BASE}/api/positions/${id}`, { method: 'DELETE' });
        if (json.http >= 400 || json.status === 'error') { log(`Stop tracking failed: ${json.message || 'error'}`, 'error'); return; }
        window._positionsData = (window._positionsData || []).filter(x => x.signal_id !== id);
        renderPositions();
    } catch (e) { log(`Stop tracking failed: ${e.message}`, 'error'); }
}
