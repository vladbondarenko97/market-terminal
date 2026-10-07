# HTTP API reference

This page lists every HTTP route served by `options_whale/api_router.py`, the Flask server behind the web terminal: its parameters and defaults, what it returns, whether it reads stored pipeline output or calls the network, what it writes, and how it fails. It ends with short notes on the two other small Flask apps in the repo. It is for operators who call the API and for contributors who add routes. For what the terminal shows, see [Terminal](terminal.md). For the environment variables named here, see [Configuration](configuration.md).

## Running the server

- **What it is.** One Flask development server (Werkzeug, threaded) in `options_whale/api_router.py`. There is no production WSGI server and no TLS.
- **How it starts.** Normally as the login service `com.vlad.optionswhale` that `setup.sh` installs. launchd runs `.venv/bin/python api_router.py` with `options_whale/` as the working directory and restarts it if it exits. By hand: `.venv/bin/python options_whale/api_router.py`.
- **Working directory.** It does not matter when you run the file as a script. Python puts the script's folder on `sys.path` (so `import quant_engine` works), and the script adds the repo root itself (for `config` and `core`). Importing it as a module (`import api_router`) needs `options_whale/` on `sys.path`.
- **Port.** `OPTIONS_WHALE_PORT`, default 8080. A blank value (as in `.env.example`) means the default. See [Configuration](configuration.md).
- **Binding.** `0.0.0.0`: every network interface, not just localhost. Debug mode is off. Do not set `FLASK_DEBUG=1`; the Werkzeug debugger would run code from the browser on every interface.
- **Required key at startup.** `DATABENTO_API_KEY` (or the alias `DB_API_KEY`). The module creates its Databento client on import and raises `EnvironmentError` if both are unset or empty. Only `/api/darkpool` uses the client, but the whole server will not start without the key. `setup.sh` does not start the service until a key is set.
- **Other keys.** `EBAY_APP_ID` and `EBAY_CERT_ID` are used by `ebay.py`, which `/api/silver_eagle_prices` runs. No route reads `FRED_API_KEY` or `EIA_API_KEY`; they belong to the pipeline. `NTFY_URL` is imported but only dead code uses it (see [Known issues in this API](#known-issues-in-this-api)). `PORTFOLIO_DATA_DIR` decides where every stored file and the database are read from.

## Security model

**There is no authentication. Keep the server on a private network (for example Tailscale) and never expose it to the internet.** Anyone who can reach the port can read all data and start a pipeline run.

- **CORS.** `flask-cors` allows GET, HEAD and OPTIONS from any origin. Any web page open in a browser that can reach the server can read every GET response. Preflights for POST and DELETE from other origins fail.
- **Cross-site write guard.** `reject_cross_site_writes()` runs before every request. For any method other than GET, HEAD and OPTIONS it takes the `Origin` header, or else `Referer`. If that names a different host and port than the `Host` the request was sent to, it answers 403 `{"status": "error", "message": "cross-site request refused"}`. It covers `POST /run`, `POST /api/positions/<id>/star`, `DELETE /api/positions/<id>` and `POST /api/war_room`.
- **Gaps in the guard.**
  - A request with neither header (curl, scripts, other machines on the LAN) is accepted. The guard stops browsers on other sites, not network clients.
  - There is no `Host` allowlist, so a DNS-rebinding page sends a matching `Origin` and `Host` and passes.
  - Only non-GET methods are checked. Any page can make a visitor's browser fire GET routes that have side effects: `/api/silver_eagle_prices` (eBay calls and a ledger row), `/api/morning` and `/api/evening` (spawn a subprocess), `/api/custom` (many Yahoo requests) and `/api/darkpool` (a Databento request).
- **Error text.** Many routes return raw exception text or subprocess output in the response body.

## Data sources and writes

Each route below is marked **Stored**, **Live** or **Stored + Live**.

| Mark | Meaning |
|---|---|
| Stored | Reads what the pipeline already wrote: the SQLite database, the CSV ledgers or the files in the newest folder of the data directory. No network call. |
| Live | Makes network calls (yfinance, Databento, eBay, an RSS feed) while the request is open. |
| Writes | Changes something: the database, a ledger, or starts a process. Routes with no "Writes" entry change nothing. |

The project rule is that models run in the pipeline and a **new** `/api/*` route reads stored data (see [Architecture](architecture.md#design-rules)). Existing routes do not all follow it. Nine routes are Live and three more are Stored + Live. Several compute models in the request: Black-Scholes gamma (`/api/gex`), option analytics (`/api/option_calc`, `/api/time_arbitrage`), the VMRI scenario (`/api/war_room`), moving averages (`/api/vmri_history`), headline sentiment (`/api/macro_news`) and position values (`/api/positions`). Their results can differ from the pipeline's numbers for the same quantity.

## Response and error conventions

- Most JSON routes answer `{"status": "success", "data": ...}` or `{"status": "error", "message": ...}`. Exceptions are called out per route.
- **HTTP 200 with `status: "error"`** (check the body, not the HTTP code): `/api/gex`, `/api/darkpool`, `/api/morning`, `/api/evening`, `/api/option_chain`, `/api/option_calc`, `/api/time_arbitrage`, `/api/macro_direction`. `/api/custom` also answers 200 on a scan failure, with an `error` attribute on the root element.
- `/api/vmri_history` and `/api/inventory_data` return a bare object and report failure as `{"error": "..."}` with 404 or 500.
- XML routes report failure as `<error>text</error>` with 404, 500, 502 or 504.
- A non-integer `limit` on `/api/institutional_history` or `/api/macro_ledger_full` is parsed outside the error handler and gives an HTML 500 page.
- Every response carries `Cache-Control: no-cache, no-store, must-revalidate`.
- Some Stored routes read a CSV ledger through a 60 second in-memory cache; others read the file on every request. Live quotes and chains used by `/api/positions` and the `/api/forecast` scorecard are also cached for 60 seconds.

## Terminal and system

| Route | Parameters | Response | Source and writes |
|---|---|---|---|
| `GET /` | none | HTML: the terminal (`templates/terminal.html`, title "VladHQ \| Market Terminal") | Stored (template) |
| `GET /help` | none | XML `<api_documentation>` listing every route with method, parameters and defaults. The terminal calls it only as an online check and needs just HTTP 200. | Static |
| `POST /run` | none | XML `<execution_result status="SUCCESS" timestamp=...>` with a `<message>`. 404 `<error>` if `run_dashboard.command` is missing. 500 `<error>` with the script's stderr if it exits non-zero. | Writes: runs `run_dashboard.command manual`, a full pipeline run (see [Operations](operations.md)). The request blocks until the run ends; there is no timeout. Cross-site guard applies. |
| `GET /api/dump`, `GET /dump` | none | XML `<cme_data_dump source_folder timestamp>` holding the root of `tactical_ruling.txt`, then the root of `volume_dashboard.txt` (a duplicate `<tactical_ruling>` inside it is dropped). A file that is missing or does not parse is skipped. 404 `<error>` if the data directory or all folders are missing. | Stored: the newest subfolder of the data directory by modification time (any subfolder counts, not only daily run folders) |
| `GET /static/<path>` | file name | The terminal's `app.js`, `forecast.js` and `styles.css` | Stored (Flask built-in) |

Known issue: `POST /run` answers "completed successfully" even when `run_dashboard.command` skipped the run because another one held the lock (it maps exit code 75 to 0). See [Known issues](known-issues.md).

## Macro and VMRI

The Vlad Macro Risk Index (VMRI) is `(DXY x 10Y yield / 1.61) x (HY OAS / 4) x (VIX / 20)`. Tiers: below 150 LOW RISK, below 250 MODERATE RISK, below 350 ELEVATED RISK, 350 and above SYSTEMIC THREAT. The pipeline's version is in `core/metrics.py`, `vmri()`.

| Route | Parameters | Response | Source and writes |
|---|---|---|---|
| `GET /vmri` | none | XML `<vmri_report timestamp source_folder>`: `<live_calculation>` (the `VLAD_MACRO_RISK_INDEX` element of the newest `tactical_ruling.txt`), `<master_formula>`, and `<documentation>` with the three components (Base Stress, Credit Multiplier, Volatility Premium), their formulas and the four risk ranges. 404 `<error>` if no folder, no `tactical_ruling.txt` or no VMRI element exists; 500 on a parse error. | Stored: searches folders newest first for a `tactical_ruling.txt` |
| `GET /api/vmri_history` | none | Bare JSON: `labels`, `scores`, `sma_10`, `momentum_5`, `primary_driver`, and `context` (`dxy`, `yield`, `vix`, `gold`, `gsr`, `oas`). The last 500 rows by date; unusable values become `null`. | Stored: `macro_master_ledger.csv`, read on every request. The server computes the 10-row moving average, the 5-row change, and the driver (the pillar with the largest 5-row percent move). |
| `GET /vmri_chart` | none | HTML chart page for the terminal's iframe. It fetches `/api/vmri_history`. The browser loads Chart.js and its annotation plugin from `cdn.jsdelivr.net`, so it needs internet access. | Stored |
| `GET /api/war_room`, `POST /api/war_room` | `dxy_shift`, `tnx_shift`, `oas_shift` (all 0): amounts added to DXY, 10Y yield and HY OAS. `vix_shift_pct` (0): VIX change in percent; wins when not 0. `vix_shift` (0): VIX change in points, used when `vix_shift_pct` is 0. Query string on GET, JSON body on POST. | JSON `{status, formula, thresholds, current, hypothetical, impact, history}`. `current` and `hypothetical`: `vmri`, `tier`, `dxy`, `tnx`, `oas`, `vix`, `factors` (`macro_base`, `credit`, `vol`). `impact`: `vmri_delta`, `vmri_delta_pct` (against the ledger's `VMRI_Score`), `solo_delta` (`dxy`, `tnx`, `oas`, `vix`: the change each lever causes alone). `history`: `null` with fewer than 20 recorded scores, else `n`, `start`, `end`, `min`, `median`, `max`, `pct_below_current`, `pct_below_hypothetical`, `lo`, `hi`, `counts` (30 histogram bins). Errors: 500 `{"status": "error", "message"}`. | Stored: the last row of `macro_master_ledger.csv`, read directly. A blank value falls back to the last non-blank value in that column. No write; POST only carries parameters. |
| `GET /api/macro_direction` | none | JSON `{status, data: {vmri, sentiment_bias}}`. | Stored: last ledger row |
| `GET /api/macro_calendar` | none | JSON `{status, events: [{date, time, impact, title, forecast, previous}]}`. `events` is empty if the report has no `upcoming_macro_events`. 404 if no data directory or file; 500 on a parse error. | Stored: newest `tactical_ruling.txt` |
| `GET /api/macro_news` | none | XML `<macro_news timestamp source="Yahoo_Finance">` with up to 10 `<article published title link sentiment>`. `sentiment` is the VADER compound score of the title (-1 to 1). 502 `<error>` if the feed does not answer 200; 500 on other failures. | Live: `finance.yahoo.com/news/rss`, 5 second timeout |
| `GET /api/macro_ledger_full` | `limit` (200): number of most recent rows | JSON `{status, data}` of parallel arrays: `labels` plus `vmri_score`, `threat_tier`, `dxy`, `dxy_change`, `ten_y_yield`, `zn_futures`, `high_yield_oas`, `vix`, `vix_change`, `wti_crude`, `brent_crude`, `gold_price`, `gold_silver_ratio`, `shfe_silver_usd`, `comex_silver`, `shfe_premium`, `gex`, `dix`, `reverse_repo_bn`, `fed_balance_sheet_bn`, `retail_silver_cheapest`, `retail_silver_avg`, `silver_oi`, `paper_physical_ratio`. Missing values are `null` (`"N/A"` for `threat_tier`). 404 if the ledger is missing. | Stored: `macro_master_ledger.csv`, 60 second cache |

Known issues on this table:
- `/api/macro_direction` returns the constant `"NEUTRAL"` for `sentiment_bias`; it is not computed.
- `/api/war_room` substitutes fixed numbers (DXY 100, 10Y 4.0, OAS 4.0, VIX 20, VMRI 200) when a ledger column has no value at all, instead of reporting it missing.

See [Known issues](known-issues.md).

## Options and flow

All of these call the network or run a subprocess. `ticker` is upper-cased.

| Route | Parameters | Response | Source and writes |
|---|---|---|---|
| `GET /api/gex` | `ticker` (SPY) | JSON `{status, data: {spot, zeroGamma, callWall, putWall, strikes, gamma}}`. `strikes` and `gamma` are parallel arrays for strikes within 10% of spot. `callWall` is the strike with the highest net value, `putWall` the lowest. | Live: yfinance. The three nearest expirations; gamma x open interest x 100 x spot, calls added and puts subtracted; risk-free rate fixed at 5%. |
| `GET /api/darkpool` | `ticker` (SPY) | JSON `{status, data: {ticker, total_block_volume, total_notional_usd, largest_single_block, vwap_price, sentiment: {bias, bull_volume, bear_volume}, recent_prints: [{time, price, size, side}]}}`. No blocks gives `{"status": "success", "message": "No institutional blocks detected.", "data": null}`. No trades gives a 200 error. | Live: Databento `DBEQ.BASIC` trades. The window is the 24 hours ending at midnight UTC of yesterday (moved back to Saturday for a Sunday or Monday end), first 50,000 trades only. A block is a trade of 10,000 shares or more. `bias` is BULLISH or BEARISH when one side exceeds the other by 20%, else NEUTRAL. The 5 latest blocks are returned. |
| `GET /api/institutional_history` | `ticker` (SLV), `limit` (100) | JSON `{status, data}` of parallel arrays: `ticker`, `labels` (dates), `spot_price`, `dp_sentiment`, `dp_total_vol`, `dp_notional`, `dp_largest_block`, `dp_vwap`, `dp_bull_vol`, `dp_bear_vol`, `gex_call_wall`, `gex_put_wall`, `gex_zero_gamma`. 404 if the ledger is missing or empty, or has no rows for the ticker. | Stored: `equities_darkpool_gex_ledger.csv`, 60 second cache. These are the pipeline's values, not `/api/gex` output. |
| `GET /api/morning`, `GET /api/evening` | `ticker` (SPY): echoed back only | JSON `{status: "success", ticker, data}` where `data` is one plain-text report string: the highest-volume and highest-open-interest call and put for SPY and for SLV, from `options_scanner.py`. The ticker does not change the report, and no DTE, Vol/OI or premium filter is applied. Both routes behave the same. Errors are HTTP 200 `{"status": "error", "message"}`; `/api/morning` adds `details` with the script output. | Live: runs `options_scanner.py` with the server's Python (30 second timeout), which queries yfinance for every expiration. Starts a subprocess on every GET. |
| `GET /api/custom` | `ticker` (SPY). `min_vol_oi` (1.0): minimum volume / open interest. `max_dte` (365): maximum days to expiration; 0 or empty means no limit. `min_premium` is accepted but ignored. | XML `<whale_hunt ticker strategy="CUSTOM_HUNT" whale_count>` with one `<contract>` per hit, sorted by premium, largest first. Attributes: `symbol`, `type` (CALL or PUT), `expiration`, `strike`, `last_price`, `bid`, `ask`, `spread`, `volume`, `open_interest`, `vol_oi_ratio`, `implied_volatility`, `premium_spent`, formatted as text (`$1.25`, `3.20x`, `25.10%`). A scan failure (including a non-numeric `max_dte`) is HTTP 200 with an `error` attribute on the root; a non-numeric `min_vol_oi` gives a 500 `<error>`. | Live: yfinance, one request per expiration, no timeout. Premium is volume x last price x 100 and must be at least $100,000 (fixed). Vol/OI divides by open interest, with 0 counted as 1. Calls and puts, in or out of the money; no lower bound on days to expiration. No phone alert is sent. |
| `GET /api/option_chain` | `ticker` (SPY). `expiration` (first expiration after today): `YYYY-MM-DD`; ignored if not listed. | JSON `{status, data: {ticker, spot, expirations, selected_expiration, days_to_exp, calls, puts}}`. `expirations` is the first 24. Each contract: `strike`, `type`, `last`, `bid`, `ask`, `iv`, `oi`, `volume` (missing values become 0). | Live: yfinance |
| `GET /api/option_calc` | `ticker` (SPY). `strike` (0) and `expiration` (empty, `YYYY-MM-DD`) must be supplied. `type` (`call`): `call` or `put`. `market_price` (0): 0 uses the chain's last price. | JSON `{status, data}` with `bs_price`, `market_price`, `prob_itm`, `prob_otm` (percent), `delta`, `gamma`, `theta`, `vega`, `rho`, `intrinsic`, `extrinsic`, `breakeven`, `moneyness`, `expected_move`, `iv_pct`, `hv_pct`, `iv_signal`, `spot`, `strike`, `expiration`, `days_to_exp`, `option_type`. | Live: yfinance spot, the contract's implied volatility from the chain, and the 13-week T-bill yield (`^IRX`) as the rate. Black-Scholes in the server. |
| `GET /api/time_arbitrage` | `ticker` (SPY) | JSON `{status, data}` with `z_score`, `gamma_state` (`distance`, `distance_pct`, `velocity`, `short_gamma_active`), `dealer_trapdoor` (`vanna_exposure`, `charm_exposure`, `vanna_profile`), `iv_bleed`, `probabilities` (`prob_3d`, `prob_5d`, `prob_7d` per strike), `term_structure` (`days`, `iv`), `iv_hv_spread`. | Stored + Live. Stored: last row of the macro ledger (GEX, DIX) and the ticker's latest row in the dark pool and GEX ledger (spot, zero gamma). Live: yfinance for VIX, the chain of the first expiration after today, a four-point IV term structure (cached 60 seconds), 20-day realized volatility and `^IRX`. |

Known issues on this table:
- `/api/gex`: `zeroGamma` is set to the spot price. It is not calculated.
- `/api/morning` and `/api/evening`: the ticker is ignored and no filters are applied. The filtering code (`execute_morning_hunt()`, `execute_evening_hunt()`, `execute_custom_hunt()`, with `get_chains()`, `send_whale_alert()` and `build_xml_response()`) is never called by any route.
- `/api/custom`: `min_premium` is ignored, and the scan keeps in-the-money contracts too.
- `/api/darkpool`: it counts Databento side `A` as bullish and labels it BUY. The pipeline's corrected rule (`core/metrics.py`, `FLOW_VERSION` `block_flow_v2`) is that `A` is the sell aggressor. This route's bias and BUY/SELL labels are the opposite of the pipeline's.
- `/api/option_calc`: `hv_pct` and `iv_signal` always use SPY's realized volatility, whatever the ticker (`QuantEngine.calculate_option_analytics()` calls `calculate_realized_volatility()` without the ticker).
- Invented fallbacks: `/api/option_calc` uses IV 0.20 when the contract is not found in the chain. `/api/time_arbitrage` uses VIX 20, ATM IV 0.20 and, for a ticker with no ledger row, zero gamma = spot x 0.995. `QuantEngine` falls back to a 5% rate and 15% realized volatility when Yahoo fails.

See [Known issues](known-issues.md).

## Silver and arbitrage

| Route | Parameters | Response | Source and writes |
|---|---|---|---|
| `GET /api/silver_eagle_prices` | none | XML `<physical_arbitrage comex_spot timestamp item_count>` with one `<listing item_id name total_cost base_price shipping premium_dollars premium_percent status available_qty sold_qty>` per live eBay listing, cheapest first. Premium is over the benchmark price; `comex_spot` and the premiums read `unavailable` when there is no benchmark. 504 `<error>` after 120 seconds; 500 `<error>` if the script fails, including when the eBay token request fails. | Live: runs `ebay.py` with the server's Python. It calls the eBay Browse API for the nine listing IDs hard-coded in the script (needs `EBAY_APP_ID` and `EBAY_CERT_ID`). The benchmark is the silver futures price (`SI=F`) from the latest snapshot, else a live Yahoo quote. **Writes:** a GET with at least one listing and a benchmark appends a row to `physical_arbitrage_ledger.csv` and its SQLite table. |
| `GET /api/arbitrage_history` | `limit` (50): number of most recent rows | JSON `{status, data}` of parallel arrays: `labels` (`MM-DD HH:MM`), `spot`, `cheapest_price`, `avg_price`, `cheapest_pct`, `avg_pct`, `cheapest_dollar`. 404 if the ledger is missing or empty. | Stored: `physical_arbitrage_ledger.csv`, read on every request |
| `GET /api/inventory_data` | none | Bare JSON: `labels` (dates), `registered`, `eligible`, `total`. All rows, sorted by date. | Stored: `comex_inventory_history.csv` |
| `GET /inventory_chart` | none | HTML chart page for the terminal's iframe. It fetches `/api/inventory_data`; the browser loads Chart.js from `cdn.jsdelivr.net`. | Stored |

Known issue: the XML attribute `comex_spot` (and the ledger column `COMEX_Spot`) holds the `SI=F` futures price, not a COMEX spot quote. The route also writes on GET, so a page that triggers it adds ledger rows. See [Known issues](known-issues.md).

## Engine positions

The tracked tickets from the execution engine, stored in `v2_trade_signals` (see [Data](data.md)). Recording and import are covered in [Operations](operations.md).

| Route | Parameters | Response | Source and writes |
|---|---|---|---|
| `GET /api/positions` | none | JSON `{status, data, as_of}`. `data` is every position not yet stopped, newest first. Each item has the `v2_trade_signals` columns plus `horizons` (values at +1 day, +1 week and +2 weeks), `underlying_now` and `underlying_change_pct`. Option positions also get `expired` and `dte`; those that have not expired also get `now_bid`, `now_ask`, `now_last`, `now_mid`, `change` and `change_pct`. | Stored + Live: the database plus yfinance quotes, chains and daily closes. It opens the database read-write and creates missing tables, so a GET can write. |
| `POST /api/positions/<id>/star` | `id`: integer in the path | JSON `{status: "success", starred: true or false}`. 404 `{"status": "error", "message": "not found"}` for an unknown id. | Writes: flips `starred` on the row. Cross-site guard applies. |
| `DELETE /api/positions/<id>` | `id`: integer in the path | JSON `{status: "success"}`. 404 `{"status": "error", "message": "not found"}` for an unknown or already stopped id. | Writes: sets `deleted_at`. The row is kept and no longer priced; the API cannot undo it. Cross-site guard applies. |

## Forecast Lab

The cards themselves are described in [Forecast Lab](forecast-lab.md).

| Route | Parameters | Response | Source and writes |
|---|---|---|---|
| `GET /api/forecast` | `ticker` (SPY): `SPY` or `SLV`; anything else is 400 `{"status": "error", "message": "ticker must be SPY or SLV"}` | JSON `{status, ticker, run, horizons, data, consensus, positioning, silver_fair_value, macro_regime, source_errors, refining, scorecard}`. `data` holds the ticker's cards. 404 `{"status": "error", "message", "run"}` when there is no snapshot yet or the latest one has no forecast. | Stored + Live. Stored: the latest committed snapshot (`v2_latest_snapshot`), cached in memory per run. Live: the scorecard grades logged forecasts against two years of daily closes from yfinance (cached 60 seconds). It opens the database read-write and may create its scorecard table. |
| `GET /api/eia_history` | none | JSON `{status: "success", version, dates, start, as_of, series, note}`. Each `series` item has `key`, `label`, `first`, `mbbl`, `vs_5y_pct` and, where a matching flow exists, `days_of_supply` and `supply_basis`. Arrays line up with `dates`; `null` where a series had not started. 404 `{"status": "error", "message"}` when no EIA data exists; 500 on other errors. | Stored: rebuilt from the raw EIA payloads the pipeline captured, through a read-only database connection. Cached in memory until the payloads change. |

Known issue: on a database the pipeline has never migrated (no run yet), `/api/forecast` raises `no such table: v2_latest_snapshot` and answers HTTP 500, not the 404 "no Forecast Lab data yet" message the code intends. See [Known issues](known-issues.md).

## Other servers in this repo

These are standalone. The terminal and `setup.sh` do not use or start them.

### `options_api.py` (legacy)

A single route: `GET /api/find_options`.

| Item | Detail |
|---|---|
| Parameters | `ticker` (required; missing gives 400 XML `<error>`). `exp` or `expiration` (all expirations): `MM/DD/YYYY` or `MM/DD/YY`. |
| Response | XML `<options_scan ticker scan_type timestamp>` (plus `target_date` when an expiration is given) with `<calls>` and `<puts>`, each holding a `<highest_volume>` and a `<highest_open_interest>` contract (`symbol`, `expiration`, `strike`, `volume`, `open_interest`, `last_price`, `implied_volatility`). |
| Source | Live: yfinance. Changes nothing. No CORS, no write routes. |
| Binding | `0.0.0.0:8080`, hard-coded. This is the default port of `api_router.py`, so the two cannot run on one machine at the same time. A comment in the file says 5002; the code uses 8080. |

### `alphaflow/server.py`

A separate small-cap options sweep scanner. Run it with `python alphaflow/server.py`. It reads `DATABENTO_API_KEY` from the project `.env` and keeps its own database, `alphaflow/alphaflow.db`.

| Route | Detail |
|---|---|
| `GET /` | HTML (`templates/index.html`) |
| `GET /documentation` | HTML (`templates/documentation.html`) |
| `POST /api/scan` | JSON body, all optional: `scan_date` (`YYYY-MM-DD`, default yesterday), `min_spend` (50000), `vol_oi` (1.5). Runs `run_historical_scan()` (Databento OPRA and yfinance; can be slow). Answers `{status: "success", data: [...]}` or 500 `{status: "error", message}`. Writes the results to the `swing_plays` table. |
| `GET /api/results` | `{status: "success", data: [...]}`: every row of `swing_plays`, largest spend first |

- **Port and debug.** Port 5001. By default it binds `0.0.0.0` with the debugger off. With `ALPHAFLOW_DEBUG=1` the Flask debugger is on and it binds `127.0.0.1` only.
- **No protection.** There is no authentication, no CORS setting and no cross-site write guard, so `POST /api/scan` is open to any client that can reach the port. Without a Databento key the scan returns no rows.
- Known issue: `alphaflow/engine.py` calls `init_db()` on import, which drops and recreates `swing_plays`, so saved results do not survive a server restart. See [Known issues](known-issues.md).

## Known issues in this API

Each is repeated next to its route above. The full list, with status, is in [Known issues](known-issues.md).

- The server will not start without a Databento key, although only `/api/darkpool` needs it.
- `/api/gex` reports spot as `zeroGamma`.
- `/api/morning` and `/api/evening` ignore the ticker and apply no filters; `/api/custom` ignores `min_premium`. The hunt functions (`execute_morning_hunt()`, `execute_evening_hunt()`, `execute_custom_hunt()`) and their helpers in `api_router.py` are dead code.
- `/api/darkpool` bull and bear sides are the reverse of the pipeline's definition.
- `/api/option_calc` uses SPY's realized volatility for every ticker.
- Invented fallback numbers in `/api/war_room`, `/api/time_arbitrage`, `/api/option_calc` and `QuantEngine`, and the constant `sentiment_bias` in `/api/macro_direction`. These conflict with the rule "never show a made-up number".
- Eight routes answer HTTP 200 when they fail.
- GET routes with side effects are not covered by the cross-site guard, and the guard is bypassed by clients that send no `Origin` or `Referer`.
- `GET /api/silver_eagle_prices` writes a ledger row; `GET /api/positions` and `GET /api/forecast` open the database read-write.
- `POST /run` reports success when the run was skipped because another was in progress.
- `/api/forecast` returns HTTP 500 on a database the pipeline has never migrated.
- `options_api.py` and `api_router.py` both default to port 8080.
- `alphaflow` drops its results table on every start.
