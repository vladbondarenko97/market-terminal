# Known issues

Bugs and gaps confirmed against the code, with where they live, plus the behaviours that look like bugs but are
deliberate. When you fix an issue, delete its entry in the same change. When you find one, add it here. The other
pages describe what the code does today; this page collects the exceptions in one place.

Last checked against the code: October 2026.

## Open issues

| Issue | Where | Effect |
|---|---|---|
| Phones held sideways get the desktop layout | `options_whale/static/app.js` (`MOBILE_QUERY`), `options_whale/static/styles.css` | The phone layout (docked console bar, bottom Panels/Console bar) applies below 768 px wide. A phone in landscape is usually wider, so it shows the desktop layout, where the panels get about 200 px of height above the console. Use the phone upright, or press the console's [Minimize] (more room for panels) or [Maximize] (console only). |
| AlphaFlow stores fixed placeholder columns | `alphaflow/engine.py` `run_historical_scan()` | Every sweep row gets `term` "Mid Term", `entry` "NEXT OPEN", `max_risk` 500 and an `expiration` of scan date + 44 days. They are not computed from the trade. The AlphaFlow docs page says so; the email appendix prints them. |

## Deliberate design decisions

These are known and accepted. Change them only on purpose, and update this page and the page that owns the topic.

| Decision | Where | Why, and what it means |
|---|---|---|
| The terminal has no login and listens on all network interfaces | `options_whale/api_router.py` `app.run(host="0.0.0.0")` | The operator reaches it from a phone over a private network (Tailscale). Anyone who can reach the port can read the data and start a pipeline run, so keep the Mac off the public internet. |
| The cross-site write guard stops browsers, not network clients | `api_router.py` `reject_cross_site_writes()` | Every route that writes or starts a process is POST or DELETE, and a browser on another site gets 403. A request with no `Origin` or `Referer` (curl, scripts) is accepted, there is no `Host` allowlist (DNS rebinding), and GET routes can still spend provider requests (Yahoo, Databento, eBay). Without a login this is the intended limit. |
| Some terminal routes compute at request time from live data | `/api/gex`, `/api/darkpool`, `/api/time_arbitrage`, `/api/option_chain`, `/api/option_calc`, `/api/macro_news`, the scans, the positions' live prices and the scorecard grading | The Time Arbitrage tab, the Option Explorer and the scanners are intraday tools, so they cannot wait for the next pipeline run. They follow the other rules (no invented numbers, `null` with a reason). New routes should still read stored data ([Architecture: design rules](architecture.md#design-rules)). |
| `comex_spot` holds the COMEX silver futures price (`SI=F`) | `ebay.py` (XML attribute and the `COMEX_Spot` ledger column) | The name is kept so old ledger rows and readers keep working. The XML also carries `benchmark_symbol="SI=F"`. |
| Time Arbitrage has a stored zero-gamma level only for SPY and SLV | `api_router.py` `get_time_arbitrage()` | The pipeline records zero gamma for those two tickers only, so for any other ticker `gamma_state` is `null` with a reason instead of a guess. |
| AlphaFlow has no protection of its own | `alphaflow/server.py` | It is a standalone tool started by hand on port 5001, with no login and no cross-site guard. Run it only on a private network and stop it when done. |
| Two daily runs share one day folder | `main_pipeline.render_outputs()` | The 15:45 run replaces the 09:31 run's files in `CME_Data/<Mon-DD-YY>/`; every run's report, email and snapshot stay in the database. Offline runs and replays write to their own subfolders. |
