# Known issues

Bugs and gaps confirmed against the code, with where they live. The other pages describe what the code does today,
including these behaviours; this page collects them in one place so they can be fixed deliberately rather than
rediscovered. When you fix one, delete its entry in the same change. When you find one, add it here.

Last checked against the code: October 2026.

## Setup and configuration

| Issue | Where | Effect |
|---|---|---|
| `akshare` is imported but not listed in `requirements.txt` | `core/sources.py` `sge_silver_benchmark()` | On a fresh `setup.sh` install the Shanghai (SGE) benchmark is always missing, so the Shanghai–COMEX spread is unavailable. Workaround: `.venv/bin/pip install akshare`. |
| `DB_API_KEY` is honoured inconsistently | `config.py` `optional_env()` vs `required_env()` | With `DATABENTO_API_KEY=` present but blank and only `DB_API_KEY` filled in, the terminal starts but the pipeline and `setup.sh` see no key. Fill in `DATABENTO_API_KEY`. |
| A blank `SMTP_PORT=` crashes every import of `config` | `config.py` (`int(...)`) | Keep `SMTP_PORT=587` or remove the line. |
| Settings read outside `config.py` | `alphaflow/engine.py` (own `.env` load), `upload_data.py`, `api_router.py` (port), `main_pipeline.py` (`DASHBOARD_URL`) | Breaks the "one place for settings" rule; no user-visible effect today. |
| `ALPHA_VANTAGE_KEY` is defined but never used | `config.py` | None. |
| Importing `config` creates `../CME_Data` | `config.py` (module level) | A script run from a fresh clone silently creates an empty data folder next to the repository. |
| `--uninstall` leaves the menu bar plugin symlink | `setup.sh` | When SwiftBar already had a plugin folder, the symlink setup created there stays. Delete it by hand. |

## Scheduling

| Issue | Where | Effect |
|---|---|---|
| Early-close days are not handled | `core/market_calendar.py` | On 13:00 ET closes (day after Thanksgiving, Christmas Eve…) the 15:45 run executes after the close. |
| Late launchd fires inside the session still run | `scheduled_run_skip_reason()` | If the Mac wakes at 11:00 ET, the missed 09:31 job runs then and records a position stamped 11:00. Only fires outside 09:20–16:00 ET are skipped. |
| Skip message says the session starts 09:30 | `scheduled_run_skip_reason()` | Runs from 09:20 ET are allowed. Cosmetic. |
| Run times are converted to local time once, at install | `setup.sh --schedule` | Correct for any US time zone with US daylight-saving dates. Elsewhere, re-run `./setup.sh --schedule` after each clock change. |
| The terminal's Run button executes `run_dashboard.command` with `bash`, not `zsh` | `api_router.py` `run_dashboard()` | The conda fallback (`source ~/.zshrc`) fails under bash when there is no `.venv`; a `\n` prints literally. |

## Pipeline

| Issue | Where | Effect |
|---|---|---|
| Export, ticket recording, forecast logging, rendering and delivery share one `try` block | `main_pipeline.run()` | An exception in `record_signal()` or `record_forecasts()` skips the files, the email and the upload. The run ends `completed_with_warnings` with exit code 0. |
| Delivery failures do not change the exit code | `main_pipeline.run()` | A failed email makes the run `completed_with_warnings`; failed ntfy pushes and uploads are not even warnings. Launchd and the Run button cannot tell. Check `main_pipeline.py status`. |
| Upload success is any HTTP 200 | `upload_data.py` `upload_files()` | A file the receiver rejected still counts as uploaded. The slim database copy is also built before the URL and token are checked, so an unconfigured upload still writes a full-size temporary copy. |
| Only `run` takes the run lock | `main_pipeline.py` `RunLock` | `replay`, `resend`, `import-history`, `cme-login`, `download_volume.py` and `update_inventory.py` can overlap a run; `cme-login` and a run would share one Chromium profile. |
| A killed run leaves `.v2_run_status.json` saying `running` | `main_pipeline.py` `set_status()` | `status` shows a run that no longer exists. The lock is the source of truth. |
| An empty `NTFY_URL` is recorded as `failed`, not skipped; `ntfy-test` exits 0 when the push fails | `send_email.py` `ntfy_brief()`, `main_pipeline.main()` | Misleading delivery detail. |
| With no price history, the Forecast Lab build crashes instead of returning `missing` | `core/forecast.py` `build()` (`closes.index.date` on an empty frame) | An offline run on an empty lake stores the whole forecast as `status: "error"`; `/api/forecast` returns 404. |
| `--no-cme-browser` does not keep the browser closed | `core/collect.py` `acquire_cme()` | Only the inventory fallback is disabled; the volume listing still opens Chromium. Use `--skip-cme` or `--cme-max-files 0`. |
| CME volume backfill only moves forward | `core/collect.py` `acquire_cme()` | Trade dates older than the newest one held are never fetched, so old gaps stay. |
| `positions.backfill()` is never called | `core/positions.py` | Dead code. |
| Horizon marks use the day's first recorded mark | `core/positions.py` `_market_mark()` | +1D/+1W/+2W values are opening-run prices, not closes. |
| Snapshot immutability blocks `UPDATE` only | `core/lake.py` (trigger `v2_snapshots_immutable`) | A `DELETE` on `v2_snapshots` is not prevented. |
| Providers missing from `PROVIDER_LIMITS` get no concurrency limit | `core/sources.py` `SourceSession.fetch()` | It creates a new `Semaphore(1)` on every call, so `ishares` and `federalreserve` requests are not bounded. |
| An offline run replaces the day's files and becomes the latest snapshot | `main_pipeline.run()` | The terminal then shows the offline run until the next live one. Use a scratch `PORTFOLIO_DATA_DIR` for offline tests. |
| `daily_market_report.txt` is imported although the code means to skip it | `core/importer.py` `import_history()` | The `.txt` test matches first, so the skip branch for rendered reports is unreachable. Each report is stored once as an extra payload; harmless. |
| `lake.backup_database()` is never called | `core/lake.py` | There is no automatic local backup. Copy `CME_Data` by hand. |

## Terminal and API

| Issue | Where | Effect |
|---|---|---|
| `/api/morning` and `/api/evening` ignore `ticker` and apply no filters | `api_router.py` `api_morning()`, `api_evening()`; `options_scanner.py` | Both return the same plain-text SPY + SLV top volume / open interest report. The filtered hunters `execute_morning_hunt()`, `execute_evening_hunt()` and `execute_custom_hunt()` are dead code. |
| `/api/custom` ignores `min_premium` | `api_router.py` `api_custom()` | The premium floor is fixed at $100,000, although the sidebar form sends a value. |
| `/api/gex` returns spot as `zeroGamma` | `api_router.py` `get_gex_profile()` | The live GEX panel's zero-gamma level is not computed. The pipeline's snapshot value is (`core/metrics.py`). |
| `/api/darkpool` reads Databento sides backwards | `api_router.py` `get_dark_pool_profile()` | It counts side `A` as bullish and labels it BUY. `A` is the sell aggressor (`core/metrics.py`, `block_flow_v2`), so the live Dark Pool panels show the opposite bias to the pipeline's block flow. |
| `/api/option_calc` uses SPY's realised volatility for every ticker | `options_whale/quant_engine.py` `calculate_option_analytics()` | `hv_pct` and `iv_signal` are wrong for other tickers. |
| `comex_spot` holds the `SI=F` futures price | `ebay.py` (XML attribute and `COMEX_Spot` ledger column) | The name suggests a COMEX spot quote. |
| Invented fallback numbers in live routes | `api_router.py` `api_war_room()`, `get_time_arbitrage()`, `calculate_option()`; `options_whale/quant_engine.py` | When an input is missing these routes substitute constants (VIX 20, IV 0.20, rate 5%, realised vol 15%, zero gamma = spot × 0.995 …) instead of reporting missing. |
| The macro ledger's `GEX` and `DIX` columns are always empty | `core/render.py` `ledger_rows()` | The Time Arbitrage Capacity Constraint Oscillator adds zero for both, so only VIX moves it (`quant_engine.calculate_z_score_oscillator()`). |
| IV Premium Bleed and the Probability Matrix use the lowest strikes of the chain, not strikes near spot; the "Optimal Strike" text always says SPY | `api_router.py` `get_time_arbitrage()`; `app.js` `updateProbMatrix()` | Misleading panels for any ticker. |
| Institutional Wishlist "Live Px" is the price saved when the contract was added | `app.js` `renderWatchlist()` | Change (%) is always 0.00. |
| `/api/macro_direction` returns a constant `sentiment_bias: "NEUTRAL"` | `api_router.py` `get_macro_direction()` | The route is not used by the UI. |
| Many routes return HTTP 200 on error | `/api/gex`, `/api/darkpool`, `/api/morning`, `/api/evening`, `/api/custom`, `/api/time_arbitrage`, `/api/macro_direction`, `/api/option_chain`, `/api/option_calc` | Clients must check `status` in the body. |
| Several routes compute models or call providers at request time | see the Source column in [API](api.md) | Breaks the "models run in the pipeline" rule; the terminal shows live values next to snapshot values. |
| `GET /api/silver_eagle_prices` writes data | `ebay.py` | Each call appends a row to `physical_arbitrage_ledger` (CSV and SQLite). |
| `POST /run` blocks until the pipeline finishes, with no timeout | `api_router.py` `run_dashboard()` | A CME login wait can hold the request for 15 minutes. A run refused by the lock (exit 75) is reported as success. |
| `GET /api/positions` and `GET /api/forecast` run schema statements | `core/positions.py`, `core/forecast.py` | Read requests open read-write connections. |
| `/api/forecast` answers 500 before the first run | `api_router.py` `api_forecast()` | On a database no run has migrated, it raises `no such table: v2_latest_snapshot` instead of the intended 404. |
| A non-integer `limit` gives an HTML 500 page | `api_router.py` `get_institutional_history()`, `get_macro_ledger_full()` | `int()` runs outside the error handler. |
| `/dump` picks the newest folder by modification time | `api_router.py` `dump_data()` | It can pick `.cme_browser_profile` or `_rejected_downloads` and return an empty dump. |
| Cards 4, 9 and 10 do not show the reason when their data is an error | `options_whale/static/forecast.js` | They render empty instead of "Unavailable: reason". |
| The copy dialog lives inside the Macro Direction tab | `options_whale/templates/terminal.html` | COPY and DUMP ALL DATA may show no dialog on the other tabs (the clipboard copy still happens). Untested in a browser. |
| The `/help` status check is scheduled twice | `options_whale/static/app.js` | Two timers poll every 30 s. Harmless. |

## Security

| Issue | Where | Effect |
|---|---|---|
| No authentication; listens on all interfaces | `api_router.py` `app.run(host="0.0.0.0")`; also `options_api.py`, `alphaflow/server.py` | Anyone on the network can read everything and start a pipeline run. Keep the Mac on a private network. |
| Read routes allow any origin | `CORS(app, methods=[GET, HEAD, OPTIONS])` | Any web page open in a browser on the network can read terminal data. |
| The cross-site write guard is partial | `api_router.py` `before_request` hook | Requests with no `Origin` or `Referer` (curl, scripts) pass; DNS rebinding passes; side-effecting `GET` routes are not covered. |
| Error responses include exception text and subprocess output | several routes | Internal paths and messages can leak. |
| Pages load unpinned CDN scripts | `options_whale/templates/terminal.html` (Tailwind, Chart.js), the `/vmri_chart` and `/inventory_chart` pages in `api_router.py` (Chart.js), `alphaflow/templates/index.html` (Drawflow), `index.html` (Chart.js) | A new upstream release can change or break the page without a code change. |

## Standalone and scratch scripts

| Issue | Where | Effect |
|---|---|---|
| `fix_csv.py` writes made-up history to a hardcoded path | `fix_csv.py` | Do not run it. It bypasses `config.DATA_DIR`. |
| `find_options.py` uses the invalid `strptime` directive `%q` | `find_options.py` | Two-digit-year dates raise `ValueError`. |
| `options_api.py` uses port 8080 (its comment says 5002) | `options_api.py` | It cannot run next to the terminal. |
| AlphaFlow wipes its results on every start | `alphaflow/engine.py` `init_db()` | `DROP TABLE` at import; each scan also deletes previous rows. |
| `deployment_engine.py` mixes placeholder data into its output | `deployment_engine.py` | Its "active trades" and some inputs are hardcoded. |

## Tests

| Issue | Effect |
|---|---|
| No tests for `core/positions.py`, the HTTP routes, most Forecast Lab models (`vol_forecast`, `positioning`, `silver_fair_value`, `calendar_effects`, `mechanical_flows`, `build`) or any JavaScript | Regressions there are caught only by hand. |
| The suite blanks only four credentials before importing `config` | Other values in a developer's real `.env` (for example `NTFY_URL`) still load during tests. |
