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
        <div class="fc-sub">PLAYBOOK (Vlad's tranche plan, as conditions; not advice)</div>
        <table class="fc-table fc-sig"><tr><th>Step</th><th>Condition</th><th>Status</th><th>Reading</th></tr>${pb}</table>
        <details class="fc-note"><summary>The rules (first match wins)</summary><ol style="padding-left:16px">${(d.rules || []).map(r => `<li><b>${fcEsc(r.regime)}</b>: ${fcEsc(r.when)}</li>`).join('')}</ol></details>`;
    fcLabelCells(el);
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
