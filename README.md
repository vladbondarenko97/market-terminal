# Market Terminal — a self-hosted market dashboard

A Python + SQLite system that collects market data twice a day, stores every raw response, computes a set of
quantitative models, and serves the result as a web terminal, an email report and a phone alert. It runs on one
Mac with no cloud services.

It has two parts:

| Part | What it is | Entry point |
|---|---|---|
| **Pipeline** | A batch run: collect → capture → snapshot → render → deliver. Runs on a schedule, then exits. | `python main_pipeline.py run` |
| **Terminal** | A Flask server that reads the latest snapshot and shows it as interactive cards. | `python options_whale/api_router.py` |

> Not financial advice. Every model output is a probability or an estimate, and each card states its own caveats.

**Read this file by role:** traders → [Features](#features) · developers → [Architecture](#architecture) and
[Adding a card](#adding-a-forecast-lab-card) · AI agents → [Rules for contributors](#rules-for-contributors-human-or-ai).

---

## Quick start

```bash
git clone https://github.com/vladbondarenko97/market-terminal.git
cd market-terminal
./setup.sh                      # macOS: Python env, .env, login service, menu bar icon
# fill in .env (at least DATABENTO_API_KEY), then run ./setup.sh again
.venv/bin/python main_pipeline.py run --no-deliver    # first run: builds the database, sends nothing
open http://localhost:8080      # or the port set as OPTIONS_WHALE_PORT in .env
```

`setup.sh` is safe to re-run. `--no-menubar` skips the menu bar icon; `--uninstall` removes the login service.
Tests need no network and no credentials: `.venv/bin/python -m unittest discover -s tests`.

---

## Features

### Terminal tabs

| Tab | Shows |
|---|---|
| **Macro Direction** | Vlad Macro Risk Index (VMRI), macro ledger, economic calendar, news sentiment, dealer gamma (GEX), block-trade flow |
| **Time Arbitrage** | Physical vs paper silver (eBay Silver Eagle prices vs spot), Shanghai–COMEX spread, COMEX inventory, engine positions |
| **Forecast Lab** | Eleven model cards for SPY or SLV (below) |

### Forecast Lab cards

Each card has **?** (what it shows, how to read it, how to trade it, how funds use it, caveats), **⛶** full screen
and **COPY** (the card's raw JSON).

| # | Card | Method | Answers |
|---|---|---|---|
| 1 | Market-implied range | Breeden-Litzenberger density from option chains | What range and odds are options pricing at 1D / 1W / 1M / 1Y? |
| 2 | Volatility forecast | HAR-RV (Corsi 2009) vs implied vol | Are options cheap or rich against forecast volatility? |
| 3 | Trend / CTA model | 1/3/6/12-month time-series momentum | Which way are trend followers positioned, and at what price do they flip? |
| 4 | Positioning | CFTC Commitments of Traders, SLV trust ounces | Is any trader group at an extreme? |
| 5 | Silver fair value | Weekly regression on gold, real yields, DXY, copper, industrial production | Is silver above or below its model value, and how fast do gaps close? |
| 6 | Macro regime | NY Fed recession probit, NFCI, Sahm rule, HY spreads | Expansion or stress? |
| 7 | Calendar & event drift | FOMC, CPI, OPEX, turn-of-month, weekday effects with n and t-stats | Does the calendar tilt the odds this week? |
| 8 | Mechanical flows | Vol-control funds, leveraged-ETF rebalancing, dealer gamma, CTA sensitivity | Who is forced to buy or sell after a ±1–5% move? |
| 9 | Forecast scorecard | Every forecast logged, graded after its target date | Were past forecasts right? Hit rate, 68% coverage, Brier score |
| 10 | Diesel & refining | Crack spreads, EIA runs and stocks, Texas refinery outage filings (TCEQ) | Is refining tight, and is a major unit down? |
| 11 | EIA inventories | Crude, SPR, Cushing, gasoline, diesel, jet, propane: levels and days of supply, any period from 1982 | Are stocks building or drawing against the 5-year norm, and how does today compare with past years? |

### Ask the terminal (console engine v3)

The console at the bottom takes questions in plain words ("What did the engine pick today?", "Is anything fired on
the Day Scanner?", "Where are the SPY gamma walls?") and answers from the terminal's own data, using a model that runs
on this machine. Nothing is sent to a cloud model.

How a question is answered: a short brief of the current state (today's engine tickets, the latest run, fired
signals, next events) goes in front of the model, so everyday questions are answered in one model call. For anything
else the model picks the one to three read-only sources it needs out of 25 and opens only the parts it needs; it is
never handed the whole data dump. It cannot start a run, write anything, reach the web or read files. Each answer
shows which sources it read and how long it took. Details: [`options_whale/assistant.py`](options_whale/assistant.py).

Any OpenAI-compatible server works: Ollama by default, or oMLX / LM Studio / llama.cpp via `ASSISTANT_LOCAL_URL`,
`ASSISTANT_LOCAL_MODEL` and `ASSISTANT_LOCAL_API_KEY` in `.env`.

### Other features

- **Options whale scanner** — contracts with unusual volume against open interest and large premium
  (`/api/morning`, `/api/evening`, `/api/custom`).
- **Execution engine and position tracking** — each run outputs a trade ticket (or CASH). Tickets are stored and
  marked to market at +1 day, +1 week and +2 weeks.
- **Delivery** — an email report, a phone push (ntfy) with a link to the full report, and an upload to a web server.
  The exact email is saved to disk before it is sent.
- **Replay** — any past run can be re-rendered from its snapshot with no network: `main_pipeline.py replay`.
- **Menu bar control** — a macOS menu bar icon shows whether the server is online and starts, stops or restarts it.
- **CME login handling** — CME volume files need a logged-in browser session with MFA. The run opens the login page,
  alerts the phone, waits, and falls back to saved history labelled as stale.

Full operating manual: [`V2_README.md`](V2_README.md). API reference: [`OPTIONS_WHALE_API.md`](OPTIONS_WHALE_API.md).

---

## Architecture

```
 Data sources                 Pipeline (main_pipeline.py)                         Outputs
 ────────────                 ───────────────────────────                         ───────
 yfinance, Databento   ──►  collect   core/collect.py, core/sources.py
 FRED, EIA, CFTC            │   every raw response saved, deduplicated by SHA-256
 CME (browser login)        ▼
 eBay, iShares, TCEQ       lake      core/lake.py  ──►  CME_Data/portfolio.db (SQLite, v2_* tables)
                            │
                            ▼
                           models    core/metrics.py, core/forecast.py, core/refining.py
                            │
                            ▼
                           snapshot  one immutable JSON document per run (v2_snapshots)
                            │
              ┌─────────────┼──────────────────────────┐
              ▼             ▼                          ▼
           render        deliver                    terminal
        core/render.py   send_email.py, ntfy,       options_whale/api_router.py
        HTML, XML, CSV,  upload_data.py             reads the latest snapshot,
        charts, email                               serves /api/* and the web UI
```

Three design choices matter for anyone changing the code:

1. **The snapshot is the single source of truth.** Every file, chart, email and API response for a run is rendered
   from that run's snapshot. The terminal never recomputes a model; it reads what the pipeline stored.
2. **Raw data is kept.** Each provider response is stored before parsing, so a parser bug can be fixed and the run
   replayed without fetching again.
3. **Missing is not zero.** A model with missing inputs returns `{"status": "missing", "reason": "..."}`. It never
   returns 0 or a placeholder, and the UI shows the reason.

### Repository map

| Path | Role |
|---|---|
| `main_pipeline.py` | CLI: `run`, `replay`, `resend`, `status`, `catalog`, `import-history`, `cme-login`, `ntfy-test` |
| `config.py` | All paths and environment variables. Nothing else hardcodes a path or reads a secret. |
| `core/collect.py` | Orchestrates one run and builds the run context (`ctx`) |
| `core/sources.py`, `core/api_client.py`, `core/cme.py` | Data fetchers with bounded retries |
| `core/lake.py`, `core/sqlite_layer.py` | Storage: runs, payloads, observations, snapshots |
| `core/metrics.py` | GEX, zero gamma, max pain, VMRI, execution votes |
| `core/forecast.py` | Forecast Lab models (cards 1–9) |
| `core/refining.py` | Diesel, refining and inventory models (cards 10–11) |
| `core/render.py` | HTML dashboard, XML, email text, charts |
| `core/positions.py` | Engine tickets and mark-to-market |
| `options_whale/api_router.py` | Flask server and all `/api/*` routes |
| `options_whale/assistant.py`, `static/assistant.js` | Console engine v3: brief, read-only data sources, local model, console UI |
| `options_whale/templates/terminal.html`, `static/app.js`, `static/forecast.js` | Terminal UI |
| `menubar/` | SwiftBar plugin for the menu bar icon. Keep only plugins in this folder. |
| `setup.sh`, `requirements.txt`, `.env.example` | New-machine setup |
| `tests/test_v2.py` | 34 offline tests with fixtures |
| `server/` | PHP upload receiver and `.htaccess` for the remote web host |

Data lives outside the repo in `CME_Data/` (next to the project, or wherever `PORTFOLIO_DATA_DIR` points).

---

## Adding a Forecast Lab card

A card is one Python function, one HTML block and one JavaScript renderer, joined by a key in the snapshot.
The example adds card 12, "RSI", for SPY and SLV.

**1. Compute it — `core/forecast.py`.** Write a pure function: data in, dict out, no network and no database.

```python
def rsi_model(df, period=14):
    c = _closes(df)
    if len(c) < period + 1:
        return {"status": "missing", "reason": f"need >= {period + 1} daily closes"}
    d = c.diff().dropna()
    gain, loss = d.clip(lower=0).rolling(period).mean(), (-d.clip(upper=0)).rolling(period).mean()
    rsi = float(100 - 100 / (1 + gain.iloc[-1] / loss.iloc[-1])) if loss.iloc[-1] else 100.0
    return {"status": "fresh", "model": f"Wilder RSI({period}), daily closes", "rsi": _f(rsi),
            "read": "overbought" if rsi > 70 else "oversold" if rsi < 30 else "neutral"}
```

Rules: return `status: "fresh"` on success and `status: "missing"` with a `reason` otherwise; pass numbers through
`_f()` so NaN becomes `null`; include a `model` string that says how it was computed.

**2. Add it to the snapshot — `build()` in `core/forecast.py`.** For a per-ticker card, add the key inside the loop:

```python
out[sym] = {..., "flows": flows, "rsi": rsi_model(fr.get(key))}
```

The server returns `out[ticker]` as `data`, so a per-ticker key reaches the browser with no server change.
A card that is the same for both tickers goes on `out` itself (as `macro_regime` does) and must also be added to
the `jsonify(...)` call in `api_forecast()` in `options_whale/api_router.py`.

**3. Fetch new inputs, if any — `build_forecast()` in `core/collect.py`.** Add the fetch there, record failures in
`errors`, and pass the result through `inputs`. Skip this step if the card uses data the run already has.

**4. Add the markup — `options_whale/templates/terminal.html`.** Copy an existing card block and change the number:

```html
<div class="rainbow-card" style="--fc-delay:-7.26s">
    <div class="rainbow-inner">
        <div class="fc-head">
            <div><div class="fc-title">12 · RSI</div><div class="fc-sub">Wilder RSI(14) · daily closes</div></div>
            <div class="fc-actions">
                <button class="fc-icon" onclick="openForecastFaq('fc12')" title="How to use this card">?</button>
                <button class="fc-icon" onclick="toggleForecastFullscreen('fc12')" title="Full screen (Esc to exit)">⛶</button>
                <button class="fc-copy" onclick="copyForecastCard('fc12')" title="Copy this card's data">COPY</button>
            </div>
        </div>
        <div id="fc12" class="fc-body"><div class="fc-muted">Loading…</div></div>
    </div>
</div>
```

**5. Render it — `options_whale/static/forecast.js`.** Four edits:

```js
function renderRsi(j) {
    const r = j.data?.rsi, el = document.getElementById('fc12');
    if (!r || r.status !== 'fresh') { el.innerHTML = missing(r); return; }
    el.innerHTML = `<div class="fc-kpis">${kpi('RSI (14)', fcNum(r.rsi, 1), fcEsc(r.read))}</div>
                    <div class="fc-muted">${fcEsc(r.model)}</div>`;
}
```

- add `renderRsi` to the renderer list in `loadForecast()`
- change `i <= 11` to `i <= 12` in the error loop of `loadForecast()`
- add `fc12: d.data?.rsi` to the `map` in `copyForecastCard()`
- add an `fc12` entry to `FC_FAQ` with `title`, `what`, `read[]`, `trade[]`, `funds[]`, `caveats[]`

Use the helpers already in the file: `kpi`, `missing`, `fcNum`, `fcPct`, `fcCls`, `fcEsc`, `fcChart`. Escape every
string that comes from data with `fcEsc`.

**6. Test it — `tests/test_v2.py`.** Feed the function a synthetic series with a known answer, and check the
`missing` path. Tests must not touch the network.

**7. Check it.**

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python main_pipeline.py run --no-deliver   # new snapshot, nothing sent
# restart the server, then open the Forecast Lab tab
```

A card appears only for runs made after the change, because old snapshots do not hold the new key. Optional extras:
add the values to the XML report in `forecast_xml()` in `core/render.py`, and log a gradable forecast in
`forecast_rows()` so the scorecard tracks it.

---

## Rules for contributors, human or AI

- **Paths and secrets come from `config.py` and `.env`.** Never hardcode either. `.env` is never committed.
- **A card or source that fails must not stop the run.** Return `status: "missing"` with a reason; the run continues.
- **Never show a made-up number.** No fallback constants, no zero for unknown. Stale data is labelled stale with its real date.
- **Snapshots are immutable.** To change a value, change the model and make a new run. Do not edit stored snapshots.
- **Models run in the pipeline, not in the server.** A new `/api/*` route should read stored data.
- **Network calls are bounded.** Use the retry limits in `core/api_client.py`; no unbounded loops.
- **Tests stay offline.** Use fixtures in `tests/fixtures/`; a test that needs a key or the network will be rejected.
- **Changes to how a value is computed are recorded** under "What changed in values" in `V2_README.md`.
- **Only one run at a time.** A second run exits with code 75; do not work around the lock.
- **The assistant is read-only.** A new data source goes in `SOURCES` in `options_whale/assistant.py` and must be a
  GET route with no side effects (no scans, scrapes, alerts or runs). A test checks this.
- **Platform:** macOS. The login flow, Mail import, launchd service and menu bar icon are Mac-specific.

## API at a glance

| Route | Returns |
|---|---|
| `GET /` | The terminal |
| `GET /api/forecast?ticker=SPY\|SLV` | All Forecast Lab cards for the latest run |
| `GET /api/eia_history` | Full weekly EIA stock and days-of-supply history for card 11 |
| `POST /api/assistant/ask` | Ask a question; streams the answer as server-sent events |
| `GET /api/assistant/status`, `/api/assistant/data?source=…` | Model server and models; exactly what the model sees for one source (`source=brief` for the brief) |
| `GET /api/gex`, `/api/darkpool`, `/api/option_chain`, `/api/option_calc` | Dealer gamma, block trades, chains, option pricing |
| `GET /api/morning`, `/api/evening`, `/api/custom` | Unusual options activity scans |
| `GET /vmri`, `/api/vmri_history`, `/vmri_chart` | Macro risk index |
| `GET /api/macro_direction`, `/api/macro_calendar`, `/api/macro_news`, `/api/macro_ledger_full` | Macro tab data |
| `GET /api/time_arbitrage`, `/api/arbitrage_history`, `/api/silver_eagle_prices`, `/api/inventory_data` | Silver arbitrage and COMEX inventory |
| `GET /api/positions`, `POST /api/positions/<id>/star`, `DELETE /api/positions/<id>` | Engine positions |
| `POST /run` | Starts a pipeline run |

The server has no authentication. Keep it on a private network (for example Tailscale); do not expose it to the internet.

## License

MIT. See [`LICENSE`](LICENSE).
