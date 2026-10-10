# HTTP API reference

This page lists every HTTP route served by `options_whale/api_router.py`, the Flask server behind the web terminal: its parameters and defaults, what it returns, whether it reads stored pipeline output or calls the network, what it writes, and how it fails. It ends with short notes on the two other small Flask apps in the repo. It is for operators who call the API and for contributors who add routes. For what the terminal shows, see [Terminal](terminal.md). For the environment variables named here, see [Configuration](configuration.md).

## Running the server

- **What it is.** One Flask development server (Werkzeug, threaded) in `options_whale/api_router.py`. There is no production WSGI server and no TLS.
- **How it starts.** Normally as the login service `com.vlad.optionswhale` that `setup.sh` installs. launchd runs `.venv/bin/python api_router.py` with `options_whale/` as the working directory and restarts it if it exits. By hand: `.venv/bin/python options_whale/api_router.py`.
- **Working directory.** It does not matter when you run the file as a script. Python puts the script's folder on `sys.path` (so `import quant_engine` works), and the script adds the repo root itself (for `config` and `core`). Importing it as a module (`import api_router`) needs `options_whale/` on `sys.path`.
- **Port.** `config.OPTIONS_WHALE_PORT` (from `OPTIONS_WHALE_PORT`), default 8080. A blank value (as in `.env.example`) means the default. See [Configuration](configuration.md).
- **Binding.** `0.0.0.0`: every network interface, not just localhost. Debug mode is off. Do not set `FLASK_DEBUG=1`; the Werkzeug debugger would run code from the browser on every interface.
- **Databento key.** Only `/api/darkpool` needs `DATABENTO_API_KEY` (or the alias `DB_API_KEY`). The client is created on the first dark pool request; without a key that route answers 503 `{"status":"error","message":"DATABENTO_API_KEY is not set in .env, ..."}` and every other route works.
- **Other keys.** `EBAY_APP_ID` and `EBAY_CERT_ID` are used by `ebay.py`, which `POST /api/silver_eagle_prices` runs. No route reads `FRED_API_KEY`, `EIA_API_KEY` or `NTFY_URL`; the pipeline owns them. `PORTFOLIO_DATA_DIR` decides where every stored file and the database are read from.
- **Server log.** Exceptions, tracebacks and the output of failed subprocesses go to the server's stderr (the launchd service writes it to `~/Library/Logs/optionswhale.log`). Responses carry only a short message.

## Security model

**There is no authentication. Keep the server on a private network (for example Tailscale) and never expose it to the internet.** Anyone who can reach the port can read all data and start a pipeline run. That is deliberate for a one-operator tool; the controls below limit what a web page in the operator's own browser can do.

- **No CORS.** The server sends no `Access-Control-*` headers, so a page on another site cannot read any response. The terminal is served by this app and only calls it from the same origin.
- **Cross-site write guard.** `reject_cross_site_writes()` runs before every request. For any method other than GET, HEAD and OPTIONS it takes the `Origin` header, or else `Referer`. If that names a different host and port than the `Host` the request was sent to, it answers 403 `{"status": "error", "message": "cross-site request refused"}`.
- **Every route that writes or starts a process is POST or DELETE, so the guard covers all of them:** `POST /run`, `POST /api/silver_eagle_prices`, `POST /api/positions/<id>/star`, `DELETE /api/positions/<id>` and `POST /api/war_room` (which only carries parameters). No GET route writes a file, a table row or starts a process.
- **Gaps that remain.**
  - A request with neither header (curl, scripts, other machines on the LAN) is accepted. The guard stops browsers on other sites, not network clients.
  - There is no `Host` allowlist, so a DNS-rebinding page sends a matching `Origin` and `Host` and passes.
  - GET routes that call a provider (`/api/custom`, `/api/morning`, `/api/evening`, `/api/darkpool`, `/api/gex` and the other Yahoo routes) can be fired by any page in the operator's browser. They change nothing locally, but they cost provider requests, and Databento queries can use credit.
- **Error text.** Failures return a short message. Exception text, tracebacks, file paths and subprocess output stay in the server log. Subprocess output is also passed through `core/lake.py` `redact()` before it is logged.
- **Chart pages.** `/vmri_chart` and `/inventory_chart` load Chart.js 4.4.1 (and the annotation plugin 2.1.0) from `cdn.jsdelivr.net` at pinned versions with Subresource Integrity hashes, so the files cannot change without a code change.

## Data sources and writes

Each route below is marked **Stored**, **Live** or **Stored + Live**.

| Mark | Meaning |
|---|---|
| Stored | Reads what the pipeline already wrote: the SQLite database, the CSV ledgers or the files in the newest daily run folder of the data directory. No network call. |
| Live | Makes network calls (yfinance, Databento, eBay, an RSS feed) while the request is open. |
| Writes | Changes something: the database, a ledger, or starts a process. Routes with no "Writes" entry change nothing. |

The project rule is that models run in the pipeline and a **new** `/api/*` route reads stored data (see [Architecture](architecture.md#design-rules)). Existing routes do not all follow it. Nine routes are Live and three more are Stored + Live. Several compute models in the request: gamma exposure (`/api/gex`, using the pipeline's own `metrics.gex_profile()`), option analytics (`/api/option_calc`, `/api/time_arbitrage`), the VMRI scenario (`/api/war_room`), moving averages (`/api/vmri_history`), headline sentiment (`/api/macro_news`) and position values (`/api/positions`). Their results can differ from the pipeline's numbers for the same quantity because they use a later quote.

Every route that reads the database (`/api/positions`, `/api/forecast`, `/api/eia_history`) opens it read-only (`core/lake.py` `connect_readonly()`). Only the star and stop-tracking routes open it read-write, and none of the routes creates the database.

## Response and error conventions

- Most JSON routes answer `{"status": "success", "data": ...}` or `{"status": "error", "message": ...}`. Exceptions are called out per route.
- **Failures use a real HTTP status.** A client can rely on the status code and read the short `message` for the reason.

| Status | Meaning |
|---|---|
| 400 | A parameter is missing, not a number or an integer, out of range, or not a valid ticker. |
| 403 | The cross-site write guard refused a POST or DELETE. |
| 404 | The thing asked for does not exist: an unknown route or id, no data for that ticker, a ledger or file that is not there. |
| 405 | Wrong method (for example `GET /api/silver_eagle_prices`). Unknown and wrong-method requests under `/api/` answer JSON. |
| 409 | `POST /run` while a run is in progress. |
| 422 | The request was valid but the value cannot be computed from what the provider returned (no usable gamma contracts, no implied volatility for the contract). |
| 500 | An unexpected error in the server. The log has the detail. |
| 502 | A provider (Yahoo, Databento, the RSS feed, eBay) failed or answered badly. |
| 503 | Not available yet: the database has not been created (no run has happened), a ledger is missing or lacks an input, or the risk-free rate is unavailable. |
| 504 | `ebay.py` did not finish in 120 seconds. |

- XML routes report failure as `<error>text</error>` with the same statuses.
- `/api/vmri_history` and `/api/inventory_data` return a bare data object on success. On failure they answer `{"status": "error", "message": "...", "error": "..."}` with 404 or 500 (`error` repeats the message for the chart pages that read it).
- **Missing is not zero.** `/api/time_arbitrage` and `/api/option_calc` never substitute a stand-in number. A value that cannot be computed is `null`, with the reason in a `missing` object in `data` (`{"field": "reason"}`). A route that cannot answer at all returns an error status that names the missing input.
- **`limit` parameters** (`/api/institutional_history`, `/api/macro_ledger_full`, `/api/arbitrage_history`) must be integers of at least 1; anything else is a 400. An empty value uses the default.
- **Tickers** on the options routes (`/api/gex`, `/api/darkpool`, `/api/option_chain`, `/api/option_calc`, `/api/time_arbitrage`, `/api/custom`, `/api/morning`, `/api/evening`) must look like symbols (letters, digits, `.`, `^`, `=` and `-`, up to 15 characters); anything else is a 400. They are upper-cased. An empty `ticker` uses the route's default.
- Every response carries `Cache-Control: no-cache, no-store, must-revalidate`.
- Some Stored routes read a CSV ledger through a 60 second in-memory cache; others read the file on every request. Live quotes and chains used by `/api/positions` and the `/api/forecast` scorecard are also cached for 60 seconds.

## Terminal and system

| Route | Parameters | Response | Source and writes |
|---|---|---|---|
| `GET /` | none | HTML: the terminal (`templates/terminal.html`, title "Market Terminal") | Stored (template) |
| `GET /help` | none | XML `<api_documentation>` listing every route with method, parameters and defaults. The terminal calls it only as an online check and needs just HTTP 200. A test checks that it lists exactly the routes the server serves. | Static |
| `POST /run` | none | Answers at once. **202** `{"status": "started", "pid"}` when the run was started. **409** `{"status": "busy", "message", "run_id", "stage"}` when a run already holds the run lock (the ids come from the status file), or when a run this server started a moment ago has not taken the lock yet (`run_id` null, `stage` "starting"). **404** `{"status": "error", "message"}` if `run_dashboard.command` is missing. **503** if `PORTFOLIO_DATA_DIR` names a folder that does not exist. | Writes: starts `run_dashboard.command manual` under `/bin/zsh` as a detached process (`cwd` the repo root, its own session, so it survives a server restart), a full pipeline run (see [Operations](operations.md)). Its stdout and stderr are appended to `.v2_manual_run.log` in the data folder. The request does not wait. Cross-site guard applies. |
| `GET /api/run_status` | none | JSON `{status: "success", data}`. `data` holds the fields of `.v2_run_status.json` (`run_id`, `stage`, `state`, `pid`, `mode`, `started_at`, `updated_at`, `finished_at`, `elapsed_s`, `error`; all null when the file is missing) plus `lock_held`. `state` is `running`, `completed`, `completed_with_warnings` or `failed` as the file says, or **`interrupted`** when the file says `running` but no process holds the run lock (the run was killed). | Stored: the status file, and a non-blocking probe of the run lock (`core/runlock.py` `lock_is_held()`) that never keeps it |
| `GET /api/dump`, `GET /dump` | none | XML `<cme_data_dump source_folder timestamp>` holding the root of `tactical_ruling.txt`, then the root of `volume_dashboard.txt` (a duplicate `<tactical_ruling>` inside it is dropped). A file that is missing or does not parse is skipped. 404 `<error>` if the data directory is missing or holds no daily run folder. | Stored: the newest daily run folder. Only folders named like `Sep-29-26` (`config.RUN_FOLDER_FORMAT`) count, ordered by the date in the name; `.cme_browser_profile`, `_rejected_downloads`, `backups` (import-history database backups) and any other folder are ignored. |
| `GET /static/<path>` | file name | The terminal's `app.js`, `forecast.js` and `styles.css` | Stored (Flask built-in) |

How a manual run is followed: the terminal's RE-SCAN ALL DATA reads `/api/run_status` and remembers `run_id`, posts `/run`, then polls `/api/run_status` until a different `run_id` appears with `lock_held` false. `main_pipeline.py run` exits 0 when it completed cleanly, 3 when it completed with warnings, 1 when it failed before the snapshot and 75 when another run held the lock; the final `state` and `error` in the status file tell the same story. See [Operations](operations.md).

## Macro and VMRI

The Vlad Macro Risk Index (VMRI) is `(DXY x 10Y yield / 1.61) x (HY OAS / 4) x (VIX / 20)`. Tiers: below 150 LOW RISK, below 250 MODERATE RISK, below 350 ELEVATED RISK, 350 and above SYSTEMIC THREAT. The pipeline's version is in `core/metrics.py`, `vmri()`.

| Route | Parameters | Response | Source and writes |
|---|---|---|---|
| `GET /vmri` | none | XML `<vmri_report timestamp source_folder>`: `<live_calculation>` (the `VLAD_MACRO_RISK_INDEX` element of the newest `tactical_ruling.txt`), `<master_formula>`, and `<documentation>` with the three components (Base Stress, Credit Multiplier, Volatility Premium), their formulas and the four risk ranges. 404 `<error>` if no daily run folder, no `tactical_ruling.txt` or no VMRI element exists; 500 on a parse error. | Stored: searches daily run folders newest first (by the date in the name) for a `tactical_ruling.txt` |
| `GET /api/vmri_history` | none | Bare JSON: `labels`, `scores`, `sma_10`, `momentum_5`, `primary_driver`, and `context` (`dxy`, `yield`, `vix`, `gold`, `gsr`, `oas`). The last 500 rows by date; unusable values become `null`. | Stored: `macro_master_ledger.csv`, read on every request. The server computes the 10-row moving average, the 5-row change, and the driver (the pillar with the largest 5-row percent move). |
| `GET /vmri_chart` | none | HTML chart page for the terminal's iframe. It fetches `/api/vmri_history`. The browser loads Chart.js 4.4.1 and its annotation plugin 2.1.0 from `cdn.jsdelivr.net` (pinned, with integrity hashes), so it needs internet access. | Stored |
| `GET /api/war_room`, `POST /api/war_room` | `dxy_shift`, `tnx_shift`, `oas_shift` (all 0): amounts added to DXY, 10Y yield and HY OAS. `vix_shift_pct` (0): VIX change in percent; wins when not 0. `vix_shift` (0): VIX change in points, used when `vix_shift_pct` is 0. Query string on GET, JSON object body on POST. All must be finite numbers. | JSON `{status, formula, thresholds, current, hypothetical, impact, history}`. `current` and `hypothetical`: `vmri`, `tier`, `dxy`, `tnx`, `oas`, `vix`, `factors` (`macro_base`, `credit`, `vol`). `impact`: `vmri_delta`, `vmri_delta_pct` (against the ledger's `VMRI_Score`; null if that score is 0), `solo_delta` (`dxy`, `tnx`, `oas`, `vix`: the change each lever causes alone). `history`: `null` with fewer than 20 recorded scores, else `n`, `start`, `end`, `min`, `median`, `max`, `pct_below_current`, `pct_below_hypothetical`, `lo`, `hi`, `counts` (30 histogram bins). Errors: 400 for a bad parameter or body; **503** `{"status": "error", "message": "the macro ledger has no <column> value"}` when `DXY`, `10Y_Yield`, `High_Yield_OAS`, `VIX` or `VMRI_Score` has no value anywhere in the ledger, or when the ledger is missing or empty. | Stored: `macro_master_ledger.csv`, read directly. A blank value in the last row falls back to the last non-blank value in that column. No stand-in numbers. No write; POST only carries parameters. |
| `GET /api/macro_calendar` | none | JSON `{status, events: [{date, time, impact, title, forecast, previous}]}`. `events` is empty if the report has no `upcoming_macro_events`. 404 if no data directory or no `tactical_ruling.txt`; 500 on a parse error. | Stored: `tactical_ruling.txt` of the newest daily run folder that has one |
| `GET /api/macro_news` | none | XML `<macro_news timestamp source="Yahoo_Finance">` with up to 10 `<article published title link sentiment>`. `sentiment` is the VADER compound score of the title (-1 to 1). 502 `<error>` if the feed does not answer 200 or the request fails. | Live: `finance.yahoo.com/news/rss`, 5 second timeout |
| `GET /api/macro_ledger_full` | `limit` (200): number of most recent rows | JSON `{status, data}` of parallel arrays: `labels` plus `vmri_score`, `threat_tier`, `dxy`, `dxy_change`, `ten_y_yield`, `zn_futures`, `high_yield_oas`, `vix`, `vix_change`, `wti_crude`, `brent_crude`, `gold_price`, `gold_silver_ratio`, `shfe_silver_usd`, `comex_silver`, `shfe_premium`, `gex`, `dix`, `reverse_repo_bn`, `fed_balance_sheet_bn`, `retail_silver_cheapest`, `retail_silver_avg`, `silver_oi`, `paper_physical_ratio`. Missing values are `null` (`"N/A"` for `threat_tier`). 404 if the ledger is missing. | Stored: `macro_master_ledger.csv`, 60 second cache |

There is no `/api/macro_direction` route (it returned a constant and nothing called it); the request answers 404.

## Options and flow

All of these call the network. `ticker` is upper-cased.

| Route | Parameters | Response | Source and writes |
|---|---|---|---|
| `GET /api/gex` | `ticker` (SPY) | JSON `{status, data: {spot, zeroGamma, zeroGammaReason, callWall, putWall, strikes, gamma}}`. `strikes` and `gamma` are parallel arrays for strikes within 10% of spot. `callWall` is the strike with the highest net value, `putWall` the lowest. `zeroGamma` is the spot level where aggregate net GEX changes sign (nearest crossing to spot), or `null` with `zeroGammaReason` when there is no sign change within 10% of spot. 404 if the ticker has no options; 502 if the spot price or the chains cannot be fetched; 422 if no contract has known open interest and implied volatility. | Live: yfinance. Computed with the pipeline's `core/metrics.py` `gex_profile()` (`gex_v2`, `zero_gamma_v1`): the three nearest expirations, gamma x open interest x 100 x spot, calls added and puts subtracted, risk-free rate fixed at 5%, contracts with implied volatility of 1% or less ignored. The same definition as the stored `GEX_Zero_Gamma`. |
| `GET /api/darkpool` | `ticker` (SPY) | JSON `{status, data: {ticker, total_block_volume, total_notional_usd, largest_single_block, vwap_price, sentiment: {bias, bull_volume, bear_volume, method}, note, recent_prints: [{time, price, size, side}]}}`. `side` is `BUY`, `SELL` or `UNKNOWN`. `method` is `aggressor` or `vwap_heuristic`. `note` is null or a sentence (for example when the 50,000 trade limit was reached). No blocks gives `{"status": "success", "message": "No institutional blocks detected.", "data": null}`. No trades gives a 404 error; a Databento failure a 502. | Live: Databento `DBEQ.BASIC` trades. The window is the 24 hours ending at midnight UTC of yesterday (moved back to Saturday for a Sunday or Monday end), first 50,000 trades only. A block is a trade of 10,000 shares or more. Sides follow the pipeline's `core/metrics.py` `block_flow()` (`block_flow_v2`): Databento side `B` is a buy aggressor, `A` a sell aggressor, `N` unknown. `bias` is BULLISH or BEARISH when one side exceeds the other by 20%, else NEUTRAL. When at least half of the block volume has a known side the bias uses aggressor volume and `bull_volume`/`bear_volume` are the buy and sell volumes (`method` `aggressor`). Otherwise the pipeline's VWAP heuristic assigns unknown-side prints above or at the block VWAP to buy and below it to sell, and the volumes include them (`method` `vwap_heuristic`). The 5 latest blocks are returned, newest first. |
| `GET /api/institutional_history` | `ticker` (SLV), `limit` (100) | JSON `{status, data}` of parallel arrays: `ticker`, `labels` (dates), `spot_price`, `dp_sentiment`, `dp_total_vol`, `dp_notional`, `dp_largest_block`, `dp_vwap`, `dp_bull_vol`, `dp_bear_vol`, `gex_call_wall`, `gex_put_wall`, `gex_zero_gamma`. 404 if the ledger is missing or empty, or has no rows for the ticker. | Stored: `equities_darkpool_gex_ledger.csv`, 60 second cache. These are the pipeline's values, not `/api/gex` output. |
| `GET /api/morning`, `GET /api/evening` | `ticker` (SPY) | XML in the same format as `/api/custom`: `<whale_hunt ticker strategy whale_count>` with one `<contract>` per hit, `strategy` `MORNING_HUNT` or `EVENING_HUNT`. | Live: yfinance, the same scan as `/api/custom` with fixed settings. **Morning:** expirations within 14 days, Vol/OI at least 1.5, premium at least $100,000. **Evening:** any expiration, Vol/OI at least 1.0, premium at least $500,000. No phone alert is sent and no subprocess is started. |
| `GET /api/custom` | `ticker` (SPY). `min_vol_oi` (1.0): minimum volume / open interest. `max_dte` (365): maximum days to expiration; 0 or empty means no limit. `min_premium` (100000): minimum premium in dollars. An empty value uses the default. Numbers must be finite and not negative. | XML `<whale_hunt ticker strategy="CUSTOM_HUNT" whale_count>` with one `<contract>` per hit, sorted by premium, largest first. Attributes: `symbol`, `type` (CALL or PUT), `expiration`, `strike`, `last_price`, `bid`, `ask`, `spread`, `volume`, `open_interest`, `vol_oi_ratio`, `implied_volatility`, `premium_spent`, formatted as text (`$1.25`, `3.20x`, `25.10%`). A bad parameter is a 400 `<error>`; a ticker with no options a 404 `<error>`; a provider failure a 502 `<error>`. | Live: yfinance, one request per expiration, no timeout. Premium is volume x last price x 100. Vol/OI divides by open interest, with 0 counted as 1; contracts with unknown volume or open interest do not match. Calls and puts, in or out of the money; no lower bound on days to expiration. No phone alert is sent. |
| `GET /api/option_chain` | `ticker` (SPY). `expiration` (first expiration after today): `YYYY-MM-DD`; ignored if not listed. | JSON `{status, data: {ticker, spot, expirations, selected_expiration, days_to_exp, calls, puts}}`. `expirations` is the first 24. Each contract: `strike`, `type`, `last`, `bid`, `ask`, `iv`, `oi`, `volume` (missing values become 0). 404 if the ticker has no options; 502 if the spot price or the chain cannot be fetched. | Live: yfinance |
| `GET /api/option_calc` | `ticker` (SPY). `strike` (required, above 0) and `expiration` (required, `YYYY-MM-DD`). `type` (`call`): `call` or `put`. `market_price` (0): 0 uses the chain's last price; not negative. | JSON `{status, data}` with `bs_price`, `market_price`, `prob_itm`, `prob_otm` (percent), `delta`, `gamma`, `theta`, `vega`, `rho`, `intrinsic`, `extrinsic`, `breakeven`, `moneyness`, `expected_move`, `iv_pct`, `hv_pct`, `iv_signal`, `spot`, `strike`, `expiration`, `days_to_exp`, `option_type`, `missing`. `hv_pct` and `iv_signal` use the 20-day realized volatility **of the requested ticker**; they are `null`, with the reason in `missing`, when Yahoo has too little history for it. `market_price` is `null`, with a reason in `missing`, when no price was given and the chain's last price is 0 or absent; `breakeven` then uses the Black-Scholes price (`bs_price`), never shown as a quote. Errors: 400 for a bad strike, expiration or type; **422** when the contract is not in the chain or the chain has no implied volatility for it; **503** when the risk-free rate is unavailable; 502 when Yahoo fails. | Live: yfinance spot, the contract's implied volatility from the chain, and the 13-week T-bill yield (`^IRX`, cached 60 seconds) as the rate. Black-Scholes in the server (`quant_engine.py`). No implied volatility or rate is assumed. |
| `GET /api/time_arbitrage` | `ticker` (SPY) | JSON `{status, data}` with `z_score` (-100 to 100, or null), `z_components` (`used`: the oscillator factors in the score, `missing`: `{factor: reason}` for the rest), `gamma_state` (`distance`, `distance_pct`, `velocity`, `short_gamma_active`; null when there is no stored zero-gamma level), `dealer_trapdoor` (`vanna_exposure`, `charm_exposure`, `vanna_profile`), `iv_bleed` (15 entries: `strike`, `live_iv`, `hist_iv`, `bleed`), `probabilities` (`strike`, `iv`, `prob_3d`, `prob_5d`, `prob_7d` for 5 strikes), `term_structure` (`days`, `iv`), `iv_hv_spread` (`realized_volatility_20d`, `atm_implied_volatility`), `missing` (`{field: reason}` for every null or empty field above). 404 if the ticker has no options; 502 if the spot price or a chain cannot be fetched. | Stored + Live. Stored: last row of the macro ledger (GEX, DIX) for the oscillator, and the ticker's latest row in the dark pool and GEX ledger (spot and zero gamma, so `gamma_state` exists for SPY and SLV only). Live: yfinance for VIX, the chain of the first expiration after today, a four-point IV term structure (cached 60 seconds), 20-day realized volatility and `^IRX`. **IV bleed, vanna and charm use the 15 call strikes nearest the spot price; probabilities use the 5 nearest strikes that have an implied volatility.** The oscillator is the mean of the signed z-scores (VIX negative, GEX and DIX positive) of the factors that have a current value and at least two recent ledger values; a factor with an empty series is left out and listed in `z_components.missing`. Vanna and charm are weighted by open interest, and strikes with unknown open interest are left out. No value is replaced by a constant. |

## Silver and arbitrage

| Route | Parameters | Response | Source and writes |
|---|---|---|---|
| `POST /api/silver_eagle_prices` | none | XML `<physical_arbitrage comex_spot benchmark_symbol="SI=F" timestamp item_count>` with one `<listing item_id name total_cost base_price shipping premium_dollars premium_percent status available_qty sold_qty>` per live eBay listing, cheapest first. Premium is over the benchmark price; `comex_spot` and the premiums read `unavailable` when there is no benchmark. 504 `<error>` after 120 seconds; 500 `<error>` if the script fails, including when the eBay token request fails (the script output is in the server log, not in the response). `GET` answers 405. | Live: runs `ebay.py` with the server's Python. It calls the eBay Browse API for the nine listing IDs hard-coded in the script (needs `EBAY_APP_ID` and `EBAY_CERT_ID`). The benchmark is the silver futures price (`SI=F`) from the latest snapshot, else a live Yahoo quote. **Writes:** a call with at least one listing and a benchmark appends a row to `physical_arbitrage_ledger.csv` and its SQLite table. POST only, so the cross-site guard applies. |
| `GET /api/arbitrage_history` | `limit` (50): number of most recent rows | JSON `{status, data}` of parallel arrays: `labels` (`MM-DD HH:MM`), `spot`, `cheapest_price`, `avg_price`, `cheapest_pct`, `avg_pct`, `cheapest_dollar`. 404 if the ledger is missing or empty. | Stored: `physical_arbitrage_ledger.csv`, read on every request |
| `GET /api/inventory_data` | none | Bare JSON: `labels` (dates), `registered`, `eligible`, `total`. All rows, sorted by date. | Stored: `comex_inventory_history.csv` |
| `GET /inventory_chart` | none | HTML chart page for the terminal's iframe. It fetches `/api/inventory_data`; the browser loads Chart.js 4.4.1 from `cdn.jsdelivr.net` (pinned, with an integrity hash). | Stored |

`comex_spot` (the XML attribute, the `physical_arbitrage_ledger` column `COMEX_Spot` and the `spot` array of `/api/arbitrage_history`) keeps its historical name but holds the silver **futures** price (`SI=F`), not a COMEX spot quote. The root attribute `benchmark_symbol="SI=F"` says so in every response.

## Engine positions

The tracked tickets from the execution engine, stored in `v2_trade_signals` (see [Data](data.md)). Recording and import are covered in [Operations](operations.md).

| Route | Parameters | Response | Source and writes |
|---|---|---|---|
| `GET /api/positions` | none | JSON `{status, data, as_of}`. `data` is every position not yet stopped, newest first. Each item has the `v2_trade_signals` columns plus `horizons` (values at +1 day, +1 week and +2 weeks), `underlying_now` and `underlying_change_pct`. Option positions also get `expired` and `dte`; those that have not expired also get `now_bid`, `now_ask`, `now_last`, `now_mid`, `change` and `change_pct`. **503** `{"status": "error", "message": "database not initialised: run the pipeline once"}` until a run has created the database and its tables. | Stored + Live: the database (read-only connection, no schema statements) plus yfinance quotes, chains and daily closes. |
| `POST /api/positions/<id>/star` | `id`: integer in the path | JSON `{status: "success", starred: true or false}`. 404 `{"status": "error", "message": "not found"}` for an unknown id; 503 as above when there is no database. | Writes: flips `starred` on the row (read-write connection; it never creates the database). Cross-site guard applies. |
| `DELETE /api/positions/<id>` | `id`: integer in the path | JSON `{status: "success"}`. 404 `{"status": "error", "message": "not found"}` for an unknown or already stopped id; 503 as above when there is no database. | Writes: sets `deleted_at`. The row is kept and no longer priced; the API cannot undo it. Cross-site guard applies. |

## Forecast Lab

The cards themselves are described in [Forecast Lab](forecast-lab.md).

| Route | Parameters | Response | Source and writes |
|---|---|---|---|
| `GET /api/forecast` | `ticker` (SPY): `SPY` or `SLV`; anything else is 400 `{"status": "error", "message": "ticker must be SPY or SLV"}` | JSON `{status, ticker, run, horizons, data, consensus, positioning, silver_fair_value, macro_regime, source_errors, refining, scorecard}`. `data` holds the ticker's cards. **503** `{"status": "error", "message": "database not initialised: run the pipeline once"}` before the first run has created the database; 404 `{"status": "error", "message", "run"}` when the database has no committed snapshot yet or the latest one has no forecast. | Stored + Live. Stored: the latest committed snapshot (`v2_latest_snapshot`), cached in memory by run id for 60 seconds. Live: the scorecard grades logged forecasts against two years of daily closes from yfinance (cached 60 seconds). The database is opened read-only; the scorecard runs no schema statements. |
| `GET /api/eia_history` | none | JSON `{status: "success", version, dates, start, as_of, series, note}`. Each `series` item has `key`, `label`, `first`, `mbbl`, `vs_5y_pct` and, where a matching flow exists, `days_of_supply` and `supply_basis`. Arrays line up with `dates`; `null` where a series had not started. 404 `{"status": "error", "message"}` when no EIA data exists; 503 before the first run; 500 on other errors. | Stored: rebuilt from the raw EIA payloads the pipeline captured, through a read-only database connection. Cached in memory until the payloads change. |

## Other server in this repo

AlphaFlow is standalone. The terminal and `setup.sh` do not use or start it.

### `alphaflow/server.py`

A separate small-cap options sweep scanner. Run it with `python alphaflow/server.py`. It reads `DATABENTO_API_KEY` through `config.py` (so the `DB_API_KEY` alias works) and keeps its own database, `alphaflow/alphaflow.db`.

| Route | Detail |
|---|---|
| `GET /` | HTML (`templates/index.html`) |
| `GET /documentation` | HTML (`templates/documentation.html`) |
| `POST /api/scan` | JSON body, all optional: `scan_date` (`YYYY-MM-DD`, default yesterday), `min_spend` (50000), `vol_oi` (1.5). Runs `run_historical_scan()` (Databento OPRA and yfinance; can be slow). Answers `{status: "success", data: [...]}`. **503** `{status: "error", message}` when the Databento key is missing or the Databento query fails; the stored rows are kept. 500 `{status: "error", message}` on any other failure. A scan that completes replaces the stored rows in the `swing_plays` table with its own results, including "nothing found". |
| `GET /api/results` | `{status: "success", data: [...]}`: every row of `swing_plays`, largest spend first |

- **Port and debug.** Port 5001. By default it binds `0.0.0.0` with the debugger off. With `ALPHAFLOW_DEBUG=1` (read through `config.ALPHAFLOW_DEBUG`) the Flask debugger is on and it binds `127.0.0.1` only.
- **Stored results survive a restart.** `alphaflow/engine.py` creates the `swing_plays` table only if it is missing.
- **No protection.** There is no authentication, no CORS setting and no cross-site write guard, so `POST /api/scan` is open to any client that can reach the port.

## Known issues in this API

Open issues and deliberate choices are listed in [Known issues](known-issues.md). For this API:

- No authentication, no `Host` allowlist, and the cross-site guard does not stop clients that send no `Origin` or `Referer` (see [Security model](#security-model)).
- Several routes compute models or call providers while the request is open (see [Data sources and writes](#data-sources-and-writes)), so their numbers can differ slightly from the snapshot's.
- `/api/time_arbitrage` has a stored zero-gamma level only for SPY and SLV, so `gamma_state` is null for other tickers.
- `alphaflow/server.py` has no protection of any kind.

## Rule cards and console

| Route | Purpose |
|---|---|
| `GET /api/scanner` (`?refresh=1`), `POST /api/scanner` `{symbol}`, `DELETE /api/scanner/{symbol}` | Day Scanner rows, watchlist and live-watch paper positions; add or remove a ticker |
| `GET /api/edges` (`?symbol=XYZ`, `?refresh=1`), `POST /api/edges` `{symbol}`, `DELETE /api/edges/{symbol}` | Edge Lab: tracked tickers, a one-off analysis in `query`; track or untrack a ticker |
| `GET /api/forecast` | Also returns `signals`: the Signal Watch rows |
| `POST /api/assistant/ask` | Ask the console engine a question; streams the answer as server-sent events |
| `GET /api/assistant/status`, `GET /api/assistant/data?source=…`, `POST /api/assistant/reset` | Model server and sources; exactly what the model sees for one source (`source=brief` for the brief); forget a conversation |

Details: [terminal.md](terminal.md#rule-cards-and-alerts).
