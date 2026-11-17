// The page footer: which copy serves this page, whether it still answers, what it runs and how it compares with the other copy.
// Everything comes from GET /api/version (pinged every 15 s; its round trip is the "Online" time).
(() => {
  const el = document.getElementById('vfoot');
  if (!el) return;
  const $ = id => document.getElementById(id);
  const esc = s => String(s).replace(/[&<>"]/g, c => `&${{'&': 'amp', '<': 'lt', '>': 'gt', '"': 'quot'}[c]};`);
  const ago = t => { const s = Date.now() / 1000 - t; return s < 90 ? 'just now' : s < 5400 ? Math.round(s / 60) + ' min ago'
    : s < 129600 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago'; };
  const when = t => new Date(t * 1000).toLocaleString();
  const rel = t => t ? `<span title="${when(t)}">${ago(t)}</span>` : 'unknown';

  const timing = () => {
    const n = performance.getEntriesByType('navigation')[0];
    if (!n || !n.loadEventEnd) return setTimeout(timing, 200);
    $('vf-load').innerHTML = `Loaded in <b>${Math.round(n.loadEventEnd)} ms</b>`;
    $('vf-load').title = `server answered after ${Math.round(n.responseStart)} ms, page ready ${Math.round(n.domContentLoadedEventEnd)} ms`;
  };
  timing();

  let first, down = null;
  async function ping() {
    const t0 = performance.now();
    let v;
    try {
      const r = await fetch('/api/version', {cache: 'no-store'});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      v = await r.json();
      $('vf-net').className = 'ok';
      $('vf-net').innerHTML = `<span class="dot"></span>Online · ${Math.round(performance.now() - t0)} ms`;
      $('vf-net').title = '';
      down = null;
    } catch (e) {
      down ??= new Date();
      $('vf-net').className = 'bad';
      $('vf-net').innerHTML = '<span class="dot"></span>Offline';
      $('vf-net').title = `not answering since ${down.toLocaleTimeString()}`;
      return;
    }
    first ??= v.commit;
    $('vf-srv').innerHTML = `Served by <b>${esc(v.server || 'unknown')}</b>`;
    const sha = v.commit ? v.commit.slice(0, 7) : null;
    $('vf-ver').innerHTML = sha
      ? `Version <a href="https://github.com/vladbondarenko97/market-terminal/commit/${esc(v.commit)}" target="_blank" rel="noopener"><b>${sha}</b></a> ` +
        `<span class="subj" title="${esc(v.subject || '')}">${esc(v.subject || '')}</span>` +
        (v.commit !== first ? ' · <span class="new" title="reload to get it">new version — reload</span>' : '')
      : 'Version unknown';
    $('vf-age').innerHTML = (v.committed ? `pushed ${rel(v.committed)}` : '') + (v.committed && v.live_since ? ' · ' : '') +
      (v.live_since ? `live here ${rel(v.live_since)}` : '');
    const p = v.peer;
    $('vf-peer').innerHTML = !p ? '' : p.status === 'unreachable' ? '<span class="warn">other copy: unreachable</span>'
      : `other copy (${esc(p.server || '?')}): ` + (!p.commit || !v.commit ? 'unknown' : p.commit === v.commit ? 'same version'
        : `<span class="warn">${esc(p.commit.slice(0, 7))} (${p.committed && v.committed && p.committed < v.committed ? 'behind' : 'different'})</span>`);
    const lr = v.last_run || {};
    const at = lr.state === 'running' ? lr.started_at : lr.finished_at || lr.snapshot_at;
    $('vf-run').innerHTML = lr.state === 'running' ? `run in progress, started ${rel(at)}`
      : lr.run_id || at ? `last run ${rel(at)}${lr.state ? ' ' + esc(lr.state) : ''}` : 'no run yet';
    $('vf-chart').innerHTML = `chart data: ${esc(v.chart_store || 'unknown')}`;
  }
  el.addEventListener('click', e => e.target.classList.contains('new') && location.reload());
  ping();
  setInterval(() => document.hidden || ping(), 15000);
  document.addEventListener('visibilitychange', () => document.hidden || ping());
})();
