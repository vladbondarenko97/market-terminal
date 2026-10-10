// Console engine v3: ask the terminal in words, answered by a model on this machine. Talks to /api/assistant/* (see options_whale/assistant.py).
// The console keeps showing system log lines (log() in app.js writes into the same #consoleLog).

// crypto.randomUUID only exists on https and localhost; the terminal is served over plain http on a private network.
const askId = () => crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2);
const ASK = { conversation: askId(), busy: false, abort: null, status: null };
const ASK_SUGGESTIONS = [
    'What did the engine pick today?',
    'Is anything fired on Signal Watch or the Day Scanner?',
    'Which published edges are active on SPY, and which have tested evidence?',
    'Where are the SPY gamma walls and how far is spot from each?',
    'What would VMRI be if VIX doubled and credit spreads widened by 2 points?',
];

const askEsc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// Small, safe Markdown: everything is escaped first, then a few patterns become tags.
function askMarkdown(src) {
    const inline = t => askEsc(t).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, '$1<i>$2</i>');
    const lines = String(src || '').split('\n'), out = [];
    let i = 0;
    while (i < lines.length) {
        const line = lines[i];
        if (/^\s*\|.*\|\s*$/.test(line)) {                                   // table
            const rows = [];
            while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(lines[i]); i++; }
            const cells = r => r.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
            const body = rows.filter(r => !/^\s*\|[\s:|-]+\|\s*$/.test(r));
            out.push('<table>' + body.map((r, n) => '<tr>' + cells(r).map(c => `<${n ? 'td' : 'th'}>${inline(c)}</${n ? 'td' : 'th'}>`).join('') + '</tr>').join('') + '</table>');
            continue;
        }
        if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {                           // list
            const items = [];
            while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, '')); i++; }
            out.push('<ul>' + items.map(t => `<li>${inline(t)}</li>`).join('') + '</ul>');
            continue;
        }
        const h = line.match(/^\s*#{1,4}\s+(.*)$/);
        if (h) out.push(`<div class="ask-h">${inline(h[1])}</div>`);
        else if (/^\s*>\s?/.test(line)) out.push(`<div class="ask-quote">${inline(line.replace(/^\s*>\s?/, ''))}</div>`);
        else if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) out.push('<hr>');
        else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
        i++;
    }
    return out.join('');
}

const askModel = () => document.getElementById('askEngine').value || '';

async function askLoadStatus() {
    const sel = document.getElementById('askEngine'), dot = document.getElementById('askDot');
    try {
        const s = ASK.status = await (await fetch(`${API_BASE}/api/assistant/status`)).json();
        const local = s.local, saved = localStorage.getItem('terminal_ask_engine');
        const models = [local.model, ...local.models.filter(m => m !== local.model)];
        sel.innerHTML = models.map((m, i) => `<option value="${askEsc(m)}">${askEsc(m)}${i ? '' : ' (default)'}</option>`).join('');
        if (saved && [...sel.options].some(o => o.value === saved)) sel.value = saved;
        dot.className = `ask-dot ${local.ok ? (local.model_installed ? 'ok' : 'warn') : 'bad'}`;
        dot.title = (local.ok ? `Model server running at ${local.url}` + (local.model_installed ? '' : ` · default model ${local.model} is not installed`)
                              : `Model server not reachable at ${local.url}`) + ` · ${s.sources.length} read-only data sources`;
    } catch (e) {
        dot.className = 'ask-dot bad';
        dot.title = 'Assistant unavailable: ' + e.message;
    }
}

function askShowSuggestions() {
    const box = document.getElementById('askSuggest');
    box.innerHTML = ASK_SUGGESTIONS.map((q, i) => `<button type="button" data-i="${i}">${askEsc(q)}</button>`).join('');
    box.querySelectorAll('button').forEach(b => b.addEventListener('click', () => askSend(ASK_SUGGESTIONS[+b.dataset.i])));
    box.classList.remove('hidden');
}

function askSetBusy(busy) {
    ASK.busy = busy;
    const btn = document.getElementById('askBtn');
    btn.textContent = busy ? 'Stop' : 'Ask';
    btn.classList.toggle('stop', busy);
}

function askStop() { if (ASK.abort) ASK.abort.abort(); }

function askNewChat() {
    askStop();
    fetch(`${API_BASE}/api/assistant/reset`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ conversation: ASK.conversation }) }).catch(() => {});
    ASK.conversation = askId();
    document.getElementById('consoleLog').innerHTML = '<div class="ask-intro">New conversation. Ask anything about the data on this terminal.</div>';
    askShowSuggestions();
    document.getElementById('askInput').focus();
}

// Answers need space: if the console is a thin strip, move the divider up so it gets about 45% of the window.
function askMakeRoom() {
    const section = document.getElementById('consoleSection');
    if (!section || (typeof isMobileLayout === 'function' && isMobileLayout()) || typeof updateLayoutHeights !== 'function') return;
    const want = Math.round(window.innerHeight * 0.45), have = section.getBoundingClientRect().height;
    if (have < want - 40) updateLayoutHeights(savedTopHeight - (want - have));
}

async function askSend(text) {
    const input = document.getElementById('askInput');
    const question = (text ?? input.value).trim();
    if (!question || ASK.busy) return;
    input.value = '';
    input.style.height = '';
    document.getElementById('askSuggest').classList.add('hidden');
    if (typeof isConsoleMinimized !== 'undefined' && isConsoleMinimized) toggleConsole();
    askMakeRoom();

    const logEl = document.getElementById('consoleLog'), model = askModel();
    const turn = document.createElement('div');
    turn.className = 'ask-turn';
    turn.innerHTML = `<div class="ask-q">${askEsc(question)}</div>
        <div class="ask-a"><div class="ask-tools"></div><details class="ask-think hidden"><summary>Reasoning</summary><div></div></details>
        <div class="ask-text"><span class="ask-wait">Reading the terminal…</span></div><div class="ask-meta"></div></div>`;
    logEl.appendChild(turn);
    const toolsEl = turn.querySelector('.ask-tools'), thinkBox = turn.querySelector('.ask-think'), thinkEl = thinkBox.querySelector('div'),
          textEl = turn.querySelector('.ask-text'), metaEl = turn.querySelector('.ask-meta');
    const pinned = () => logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 80;
    logEl.scrollTop = logEl.scrollHeight;

    let answer = '', thinking = '', failed = false, frame = 0;
    const paint = () => { if (frame) return; frame = requestAnimationFrame(() => { frame = 0; const stick = pinned();
        textEl.innerHTML = answer ? askMarkdown(answer) : '<span class="ask-wait">Working…</span>'; if (stick) logEl.scrollTop = logEl.scrollHeight; }); };
    const handle = ev => {
        if (ev.type === 'delta') { answer += ev.text; paint(); }
        else if (ev.type === 'replace') { answer = ev.text; paint(); }
        else if (ev.type === 'thinking') { thinking += ev.text; thinkBox.classList.remove('hidden'); thinkEl.textContent = thinking; }
        else if (ev.type === 'tool') {
            const chip = document.createElement('span');
            chip.className = `ask-chip ${ev.name === 'calc' ? 'calc' : ''} ${ev.failed ? 'bad' : ''}`;
            chip.textContent = ev.name === 'calc' ? ev.detail : ev.name + (ev.detail ? ' · ' + ev.detail : '');
            chip.title = ev.name === 'calc' ? 'Calculated' : 'Read from the terminal' + (ev.bytes ? ` (${(ev.bytes / 1024).toFixed(1)} KB)` : '');
            toolsEl.appendChild(chip);
        }
        else if (ev.type === 'error') { failed = true; textEl.innerHTML = `<span class="ask-error">${askEsc(ev.message)}</span>`; }
        else if (ev.type === 'done') {
            if (ev.empty && !failed) textEl.innerHTML = '<span class="ask-error">The model returned no answer. Try again, or pick another model.</span>';
            metaEl.textContent = [ev.model, ev.lookups ? `${ev.lookups} lookup${ev.lookups === 1 ? '' : 's'}` : 'answered from the brief', `${ev.seconds}s`].join(' · ');
        }
    };

    askSetBusy(true);
    ASK.abort = new AbortController();
    try {
        const res = await fetch(`${API_BASE}/api/assistant/ask`, { method: 'POST', signal: ASK.abort.signal,
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question, model, conversation: ASK.conversation }) });
        if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
        const reader = res.body.getReader(), decoder = new TextDecoder();
        let buffer = '';
        for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let cut;
            while ((cut = buffer.indexOf('\n\n')) >= 0) {
                const block = buffer.slice(0, cut); buffer = buffer.slice(cut + 2);
                if (block.startsWith('data:')) { try { handle(JSON.parse(block.slice(5))); } catch (e) { console.error('assistant event', e); } }
            }
        }
    } catch (e) {
        if (e.name === 'AbortError') metaEl.textContent = 'Stopped';
        else textEl.innerHTML = `<span class="ask-error">Could not reach the assistant: ${askEsc(e.message)}</span>`;
    } finally {
        if (!answer && !failed && !textEl.querySelector('.ask-error')) textEl.innerHTML = '<span class="ask-error">No answer.</span>';
        askSetBusy(false);
        ASK.abort = null;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('askInput');
    if (!input) return;
    document.getElementById('askForm').addEventListener('submit', e => { e.preventDefault(); ASK.busy ? askStop() : askSend(); });
    input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!ASK.busy) askSend(); } });
    input.addEventListener('input', () => { input.style.height = ''; input.style.height = Math.min(input.scrollHeight, 96) + 'px'; });
    document.getElementById('askEngine').addEventListener('change', e => localStorage.setItem('terminal_ask_engine', e.target.value));
    askLoadStatus();
    askShowSuggestions();
});
