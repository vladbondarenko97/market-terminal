# Known issues

Bugs and gaps confirmed against the code, with where they live, plus the behaviours that look like bugs but are
deliberate. When you fix an issue, delete its entry in the same change. When you find one, add it here. The other
pages describe what the code does today; this page collects the exceptions in one place.

Last checked against the code: October 2026.

## Open issues

| Issue | Where | Effect |
|---|---|---|
| Phones held sideways get the desktop layout | `options_whale/static/app.js` (`MOBILE_QUERY`), `options_whale/static/styles.css` | The phone layout (docked console bar, bottom Panels/Console bar) applies below 768 px wide. A phone in landscape is usually wider, so it shows the desktop layout, where the panels get about 200 px of height above the console. Use the phone upright, or press the console's [Minimize] (more room for panels) or [Maximize] (console only). |
| Old macro ledger rows hold 0 where silver open interest and the paper:physical ratio were not recorded | `macro_master_ledger.csv` (`Silver_OI`, `Paper_Physical_Ratio`), served by `/api/macro_ledger_full` | Rows written before the pipeline recorded a missing value as empty carry `0` in these two columns. The rows are history and are not rewritten. Panel 4 treats a ratio or an open interest that is not above 0 as not recorded; any other reader of these columns must do the same. |
| Old `physical_arbitrage_ledger` rows carry an unreliable benchmark | `physical_arbitrage_ledger.csv` (`COMEX_Spot` and the four premium columns) | Before the v2 pipeline (rows up to 2026-09-24) the benchmark was a fixed $67.80 in 69 of 325 rows and a price from an earlier moment in many others, so the stored premiums run from -2.1% to +39.0% where the real range was 8.2% to 20.7%. The rows are history and are not rewritten. The terminal no longer reads this file: `/api/arbitrage_history` pairs the retail prices with the silver price of the same run from the macro ledger. Any other reader of the CSV must not trust its premium columns before 2026-09-29. |
| A Silver Eagle scan started by hand can use an old benchmark | `ebay.py` `get_live_spot_price()` | The scan takes the silver price from the latest committed snapshot whatever its age, so a scan hours after the last run divides a fresh retail price by an old futures price and appends that row to `physical_arbitrage_ledger`. The console XML of the scan shows the same premium. The terminal's premium history ignores these rows. |
| The Shanghai premium in the report and the macro ledger compares different days | `core/collect.py` `collect_shanghai()` (`premium_tax_adj`, `raw_spread`; ledger columns `SHFE_Silver_USD`, `SHFE_Premium`) | The source returns a newest fix that is 2 to 8 days old (longer over Chinese holidays), and it is set against the COMEX quote of the run. The report text, the email and the macro ledger still carry that number. Terminal panel 14 uses the date-paired model `shanghai.dated` instead. The column names say SHFE but the source is the SGE benchmark. |
| The Shanghai spread assumes 13% VAT in the benchmark | `core/metrics.py` `shanghai_dated_spread()`, `core/collect.py` `collect_shanghai()` | The VAT-adjusted spread divides the Shanghai price by 1.13. The raw spread averaged +0.7% in 2021-2022 and +12% in 2026, so the sign of the adjusted figure depends on the assumption. Both are shown on the card; the z-score and rank are the same for either. The Shanghai fix and the COMEX close of a date are also set hours apart, so the one-day change of the spread is noisy on days when silver moves fast. |
| The block-flow feed reports no trade side for SLV | `core/metrics.py` `block_flow()` (Databento `DBEQ.BASIC`) | Every SLV block print since 2026-09-29 has side `N`, so buy and sell aggressor volume are 0 and the bias always comes from the price-against-VWAP heuristic. The ledger stores the zeros (they are true: no volume with a known side). Panel 9 marks the heuristic split as a guess. |
| AlphaFlow stores fixed placeholder columns | `alphaflow/engine.py` `run_historical_scan()` | Every sweep row gets `term` "Mid Term", `entry` "NEXT OPEN", `max_risk` 500 and an `expiration` of scan date + 44 days. They are not computed from the trade. The AlphaFlow docs page says so; the email appendix prints them. |

## Deliberate design decisions

These are known and accepted. Change them only on purpose, and update this page and the page that owns the topic.

| Decision | Where | Why, and what it means |
|---|---|---|
| The terminal has no login and listens on all network interfaces | `options_whale/api_router.py` `app.run(host="0.0.0.0")` | The operator reaches it from a phone over a private network (Tailscale). Anyone who can reach the port can read the data and start a pipeline run, so keep the Mac off the public internet. |
| The cross-site write guard stops browsers, not network clients | `api_router.py` `reject_cross_site_writes()` | Every route that writes or starts a process is POST or DELETE, and a browser on another site gets 403. A request with no `Origin` or `Referer` (curl, scripts) is accepted, there is no `Host` allowlist (DNS rebinding), and GET routes can still spend provider requests (Yahoo, Databento, eBay). Without a login this is the intended limit. |
| Some terminal routes compute at request time from live data | `/api/gex`, `/api/darkpool`, `/api/time_arbitrage`, `/api/option_chain`, `/api/option_calc`, the scans, the positions' live prices and the scorecard grading | The Time Arbitrage tab, the Option Explorer and the scanners are intraday tools, so they cannot wait for the next pipeline run. They follow the other rules (no invented numbers, `null` with a reason). New routes should still read stored data ([Architecture: design rules](architecture.md#design-rules)). |
| `comex_spot` holds the COMEX silver futures price (`SI=F`) | `ebay.py` (XML attribute and the `COMEX_Spot` ledger column) | The name is kept so old ledger rows and readers keep working. The XML also carries `benchmark_symbol="SI=F"`. |
| Time Arbitrage has a stored zero-gamma level only for SPY and SLV | `api_router.py` `get_time_arbitrage()` | The pipeline records zero gamma for those two tickers only, so for any other ticker `gamma_state` is `null` with a reason instead of a guess. |
| AlphaFlow has no protection of its own | `alphaflow/server.py` | It is a standalone tool started by hand on port 5001, with no login and no cross-site guard. Run it only on a private network and stop it when done. |
| Two daily runs share one day folder | `main_pipeline.render_outputs()` | The 15:45 run replaces the 09:31 run's files in `CME_Data/<Mon-DD-YY>/`; every run's report, email and snapshot stay in the database. Offline runs and replays write to their own subfolders. |

## Rule cards compute live

`/api/scanner` and `/api/edges` (cards 12 and 13) and the `signal-alerts` command fetch Yahoo price history, quotes,
earnings dates and option chains at request time through `yfinance`, not through `SourceSession.fetch()`, and their
results are not in the snapshot. That is by design for an intraday scan, but it breaks the "models run in the
pipeline" and "pipeline network calls go through `SourceSession.fetch()`" rules: the calls are not captured,
deduplicated or switched off offline. Their unit tests are offline (synthetic series, patched fetchers).
