# Architecture

How the system fits together: the run pipeline, the terminal, the design rules every change must keep, and a map of
every file in the repository. Read this before changing code. For day-to-day commands see
[Operations](operations.md).

## The two parts

| Part | What it is | Entry point |
|---|---|---|
| **Pipeline** | A batch run that collects market data, stores it, computes the models, commits one snapshot, renders files and the email, and delivers them. It runs on a schedule or by hand, then exits. | `python main_pipeline.py run` |
| **Terminal** | A Flask server with a browser UI. Most cards read what the pipeline stored; some routes still fetch live data. | `python options_whale/api_router.py` |

Both run on one Mac. Data lives in a folder outside the repository (`CME_Data/`, see
[Configuration](configuration.md#data-folder)).

## One run, step by step

`run()` in `main_pipeline.py` drives a run. Each step is a function you can find by name.

```
 main_pipeline.run()
   │
   ├─ gate      scheduled runs only: SCHEDULED_RUNS=1 and a run slot on a trading day   core/market_calendar.py
   ├─ lock      one run at a time (DATA_DIR/.v2_run.lock, exit 75 if busy)          RunLock
   │
   ├─ collect   collect.build_context(): quotes, option chains, Databento flow,      core/collect.py
   │            FRED, calendar, CME, eBay, Shanghai, CFTC, EIA, TCEQ …              core/sources.py, core/cme.py
   │            every provider call goes through SourceSession.fetch():
   │            deduplicated per run, concurrency-limited per provider, logged,
   │            raw response stored (v2_payloads, SHA-256 dedup)
   │            models are computed here too                                         core/metrics.py, core/forecast.py,
   │                                                                                 core/refining.py
   ├─ capture   lake.record_observations(): typed rows in v2_observations           core/lake.py
   ├─ commit    lake.commit_snapshot(): one immutable JSON document (v2_snapshots)
   │            then the snapshot is re-loaded; everything after reads only it
   │
   ├─ export    live runs only: append legacy ledgers (CSV + SQLite),               main_pipeline.export_ledgers()
   │            record the engine ticket, log gradable forecasts                     core/positions.py, core/forecast.py
   ├─ render    HTML dashboard, XML, report text, CSV, charts, email.eml            core/render.py, visualize_volume.py
   │            into CME_Data/<Mon-DD-YY>/
   └─ deliver   SMTP email, ntfy push, report + database upload                     send_email.py, upload_data.py
```

The terminal reads the snapshot (`v2_latest_snapshot`), the SQLite tables and the CSV ledgers. See
[API](api.md) for which routes read stored data and which call providers live.

## Design rules

These rules hold across the codebase. A change that breaks one needs a very good reason and an update to this page.

1. **The snapshot is the source of truth for a run.** Every file, chart, email and Forecast Lab card for a run is
   rendered from that run's committed snapshot. Renderers never fetch.
2. **Raw data is kept.** Each provider response is stored before it is parsed, so a parser fix can be replayed
   (`main_pipeline.py replay`) without fetching again.
3. **Missing is not zero.** A model or source without its inputs returns `{"status": "missing", "reason": "..."}`.
   It never returns 0, a placeholder or a remembered constant, and the UI shows the reason. Stale data is shown
   with its real date.
4. **Snapshots are immutable.** To change a value, change the model, bump its version and make a new run. Record
   the change in [Value changes](value-changes.md).
5. **The lake is additive.** `core/lake.py` only creates tables and adds columns; it never drops, replaces or
   deletes rows, and triggers stop any `UPDATE` or `DELETE` of a snapshot. The one exception is the view
   `v2_latest_snapshot`, which holds no data: `migrate()` replaces it when its definition changes.
6. **A failing source does not stop the run.** Collectors catch their own errors and report them as missing;
   the run continues. Each step after the snapshot (ledgers, ticket, forecast log, files, each delivery channel)
   also runs on its own; a failure there ends the run `completed_with_warnings` with exit code 3.
7. **Network calls are bounded and recorded.** New pipeline fetches go through `SourceSession.fetch()` in
   `core/sources.py` (per-provider limits in `PROVIDER_LIMITS`). Offline runs make no provider calls.
8. **Paths and secrets come from `config.py` and `.env`.** Nothing hardcodes a data path or a key.
9. **Models run in the pipeline.** New `/api/*` routes should read stored data. Several existing routes compute
   in the server; they predate this rule (see [API](api.md)).
10. **One run at a time.** A second `run` exits with code 75. Do not work around the lock.
11. **Times.** Run folders and report dates use Chicago time (`config.TZ_NAME`), CME's home zone. The schedule
    gate uses New York time.

## Repository map

### Pipeline

| Path | Role |
|---|---|
| `main_pipeline.py` | CLI and run coordinator: `run`, `replay`, `resend`, `status`, `catalog`, `import-history`, `cme-login`, `ntfy-test` |
| `config.py` | Paths, data-folder resolution, time zone, every setting read from `.env` |
| `run_dashboard.command` | zsh launcher used by the schedule and by the terminal's Run button (`scheduled` or `manual` trigger) |
| `core/collect.py` | Builds the run context: runs every collector, computes the models, decides fresh/cached/stale |
| `core/sources.py` | Provider fetchers (yfinance, FRED, Databento, CFTC, EIA, TCEQ, eBay, iShares …) and `SourceSession` |
| `core/cme.py` | CME downloads (volume FTP listing, inventory workbooks), browser login, workbook parsing |
| `core/lake.py` | SQLite lake: `v2_*` schema, payload store, observations, snapshots, credential redaction |
| `core/sqlite_layer.py` | Helpers for the legacy ledger tables in `portfolio.db` |
| `core/history.py` | Read-side history queries (CME series, inventory, ledgers) with as-of cutoffs |
| `core/importer.py` | Idempotent import of old files and tables into the lake (`import-history`) |
| `core/metrics.py` | VMRI, GEX and walls, zero gamma, max pain, block flow, execution engine, model features |
| `core/forecast.py` | Forecast Lab models (cards 1–9), forecast logging and grading |
| `core/refining.py` | Diesel, refining and EIA inventory models (cards 10–11) |
| `core/positions.py` | Engine tickets: record, mark to market, horizon values, star and hide |
| `core/watch.py` | Day Scanner: the dip-in-an-uptrend rule on live quotes, the saved watchlist, paper positions (`v2_watch_positions`) |
| `core/edges.py` | Edge Lab: published edges tested on one ticker's own history, tracked list |
| `options_whale/assistant.py`, `options_whale/static/assistant.js` | Console engine v3: brief, read-only data sources, local model, console UI |
| `core/market_calendar.py` | NYSE holidays and early closes, the scheduled-run gate (`scheduled_run_skip_reason()`), and the local fire times `setup.sh --schedule` installs (`launchd_intervals()`) |
| `core/runlock.py` | `RunLock`, the exclusive run lock (`DATA_DIR/.v2_run.lock`), and `lock_is_held()`, a non-blocking probe that never keeps the lock |
| `core/catalog.py` | Metric catalog with lineage (`catalog` command) and chart definitions |
| `core/render.py` | Tactical and dashboard XML, report text, email text, HTML dashboard, CSV |
| `visualize_volume.py` | Draws the chart PNGs from `core/catalog.CHARTS` |
| `volume_dashboard.html` | Template with `{{PLACEHOLDER}}` tokens; the filled copy is written to each daily folder |
| `send_email.py` | Builds and sends the email (SMTP), ntfy pushes, AlphaFlow appendix |
| `upload_data.py` | Uploads the report and a slim database copy to the web server |
| `core/api_client.py` | `requests` session with retries; used only by `ebay.py` |

### Terminal

| Path | Role |
|---|---|
| `options_whale/api_router.py` | Flask server: the terminal page and every `/api/*` route |
| `options_whale/quant_engine.py` | Z-score oscillator, Black-Scholes Greeks and probabilities used by two routes |
| `options_whale/templates/terminal.html` | Terminal page |
| `options_whale/static/app.js` | Macro, Time Arbitrage, War Room, Engine Positions UI |
| `options_whale/static/forecast.js` | Forecast Lab UI |
| `options_whale/static/styles.css` | Styles |
| `ebay.py` | eBay Browse API lookup of Silver Eagle listings; run as a subprocess by `/api/silver_eagle_prices` (also appends a ledger row) |
| `menubar/optionswhale.10s.sh` | SwiftBar plugin: server status, start, stop, restart. Keep only plugins in this folder. |

### Setup, tests and remote host

| Path | Role |
|---|---|
| `setup.sh` | New-Mac setup: Homebrew, `.venv`, `.env`, server login service, optional schedule, menu bar icon |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for `.env`; see [Configuration](configuration.md) |
| `tests/` (`support.py`, `test_*.py`, `fixtures/`) | Offline test suite (no network, no credentials). `tests/test_schedule.py` covers the schedule gate, early closes and the launchd fire times. |
| `server/upload_receiver.php`, `server/.htaccess`, `server/README.md` | Receiver for uploads on your own web host |

### Maintenance scripts

| Path | Role |
|---|---|
| `scripts/import_email_positions.py` | Recovers past engine tickets from sent report emails (Apple Mail or a folder of `.eml`/`.mbox`) |
| `scripts/backfill_crypto_metals.py` | Adds up to one year of BTC, silver and gold history to `crypto_metrics_history` |
| `scripts/dump_spy_wicks.py` | Downloads SPY one-minute candles (last 5 days) to `spy_wicks_1m.json` in the repository root, where `import-history` picks them up. Uses the network; manual only. |
| `download_volume.py` | Manual, bounded CME volume backfill (needs a saved CME session) |

### Legacy compatibility scripts

Thin wrappers kept so old entry points still work. Each reads the latest snapshot or the lake; none collects data.

| Path | Prints or returns |
|---|---|
| `tactical_ruling.py` | Tactical-ruling XML |
| `dump_data.py` | Writes `volume_dashboard.txt` for the latest run (not the `/api/dump` route) |
| `market_reader.py` | Morning market brief |
| `institutional_scanner.py` | SPY/SLV block flow and GEX walls |
| `parse_volume.py` | Legacy CME volume frame from the lake |
| `update_inventory.py` | Legacy COMEX inventory frame (optionally fetches first) |

### Standalone tools (not part of the pipeline or terminal)

| Path | What it is |
|---|---|
| `alphaflow/` | AlphaFlow: separate Flask app on port 5001 that scans OPRA trades for small-cap call sweeps. It reads its settings through `config.py` and keeps its results in `alphaflow/alphaflow.db` across restarts. Docs at `/documentation` (`alphaflow/templates/documentation.html`). The email appends its top results when the database exists. |
| `deployment_engine.py`, `deployment_dashboard.html` | Capital-deployment sizing prototype (half-Kelly). Reads `deployment_state.json` and writes `deployment_payload.json`/`.js` for the static page. An input it cannot read is `null` with a reason under `missing`; the page shows it as a dash. |
| `index.html`, `index.keys.example.js` | Static multi-asset price board that polls public APIs from the browser. Keys go in `index.keys.js` (not committed). |

## Documentation map

| Page | Covers |
|---|---|
| [README](../README.md) | What the project is, quick start |
| [Operations](operations.md) | Setup, commands, schedule, CME login, delivery, troubleshooting |
| [Configuration](configuration.md) | Every `.env` setting, data folder, ports |
| [Data](data.md) | SQLite tables, snapshot contents, daily files, ledgers |
| [Terminal](terminal.md) | The web UI, tab by tab |
| [Forecast Lab](forecast-lab.md) | The eleven model cards and how to add one |
| [API](api.md) | Every HTTP route |
| [Value changes](value-changes.md) | Changes to how values are computed; model versions |
| [Known issues](known-issues.md) | Verified bugs and gaps |
| [AGENTS.md](../AGENTS.md) | Working rules for AI coding agents |
| [server/README.md](../server/README.md) | Upload receiver on your web host |
