# portfolio_dashboard v2 — run, import, replay, rollback

One coordinated run collects every source once, captures it losslessly in the existing
`CME_Data/portfolio.db`, commits a report snapshot, and renders every file, chart, the email and the
legacy ledgers from that snapshot. Same stack (Python, SQLite), same `CME_Data` folder, same filenames.

## Location

Code: `~/dev/portfolio_dashboard` · data: `~/dev/CME_Data` (pinned by `PORTFOLIO_DATA_DIR` in `.env`) ·
scheduler: `~/Library/LaunchAgents/com.vlad.marketdashboard.plist`, installed by `./setup.sh --schedule` on the
one Mac that owns the schedule (NYSE trading days at 09:31, 12:30 and 15:45 ET; holidays and late wake-ups skip) ·
terminal: `python options_whale/api_router.py` → http://localhost:8080. No code hardcodes these paths; everything
derives from `config.py`.

## Everyday use

| What | Command |
|---|---|
| Normal run (what launchd runs at 09:31 / 12:30 / 15:45 ET) | `./run_dashboard.command` → `python main_pipeline.py run --trigger scheduled` |
| Run without email/NTFY/upload | `python main_pipeline.py run --no-deliver` |
| Offline run (no network at all) | `python main_pipeline.py run --offline` |
| Re-render a saved run (no network, no delivery) | `python main_pipeline.py replay [--run RUN_ID] [--out DIR]` |
| Resend a saved email (explicit, never automatic) | `python main_pipeline.py resend [--run RUN_ID]` |
| Variable catalog + lineage for the latest run | `python main_pipeline.py catalog > catalog.md` |
| Recent runs / current run | `python main_pipeline.py status` |
| Import old files again (idempotent) | `python main_pipeline.py import-history` |
| Log in to CME (MFA) by hand | `python main_pipeline.py cme-login` |
| Tests (no network, no credentials) | `python -m unittest discover -s tests -v` |

Only one run at a time: a second request (scheduled + manual) prints `BUSY` and exits with code 75.

## CME login

The daily-volume workbooks on `cmegroup.com/ftp/daily_volume/` only download for a logged-in session
(anonymous requests get `{"message":"Bad Request"}`); `Silver_stocks.xls` downloads without login.
CME work uses a persistent Chromium profile in `CME_Data/.cme_browser_profile` (cookies, storage and Duo
"remember me" survive between runs); `CME_Data/state.json` is refreshed as a portable cookie backup.

If CME refuses a download, the run opens the login page in that browser, pre-fills
`CME_LOGIN_USERNAME`/`CME_LOGIN_PASSWORD` from `.env`, sends an NTFY push (normal runs), and waits up to
15 minutes (`--login-wait N` minutes; `0` never waits). After login it retries once; if nobody logs in,
the run continues with saved history, labelled stale with its real report dates.

Volume acquisition reads the FTP listing and downloads only trade dates missing from the lake
(default max 10 per run, `--cme-max-files N`, hard cap 40). No date guessing, no holiday loops.

## Where things are

| Daily folder `CME_Data/<Mon-DD-YY>/` (Chicago date) | |
|---|---|
| `tactical_ruling.txt`, `volume_dashboard.txt` | legacy XML: every legacy node unchanged, plus an appended `<model_features>` block (data quality, all quotes, trend/vol, IV vs RV, regime percentiles, silver complex, CME positioning, options positioning, block flow, technicals, execution votes, event risk, liquidity) |
| `volume_dashboard.html` | also contains a copyable box with the full email text, right above COMEX inventory |
| `volume_dashboard.html`, `chart*.png` | dashboard + charts (legacy filenames) |
| `master_market_data.csv` | restored parsed-CME export |
| `daily_market_report.txt` | the exact text that is the email body |
| `email.eml` | the complete MIME message, saved before SMTP |
| `report_snapshot.json`, `run_manifest.json` | snapshot copy; artifact hashes + chart outcomes |

Ledgers (`macro_master_ledger`, `equities_darkpool_gex_ledger`, `institutional_ledger`,
`physical_arbitrage_ledger`, `comex_inventory_history`, `crypto_metrics_history`) keep their CSV headers and
SQLite tables; offline runs and replays never append to them.

`portfolio.db` gains `v2_runs`, `v2_payloads` (raw responses/workbooks, dedup by SHA-256), `v2_fetch_log`,
`v2_observations`, `v2_snapshots` (immutable) and `v2_imports`, plus the view `v2_latest_snapshot`. The server
upload sends a copy without the `v2_*` tables, so it is the same size as before. Journal mode is left as-is
(rollback journal) so the uploaded file is always complete.

## Engine positions

Every ticket the execution engine outputs (including CASH) is stored in `v2_trade_signals` (one row per live
run) and shown in the terminal's **Engine Positions** card (after Shanghai-COMEX arb): entry mid, value at
+1 day / +1 week / +2 weeks (first trading day on/after), and the live value while open. Each run also records the
real mark of every open tracked contract from the chains it already downloads (`position.mark` observations), so
horizon values use market prices; older positions use a Black-Scholes estimate (`~`) at that day's SPY close with
the IV implied by the entry price, or intrinsic value at the expiry close. ★ toggles a favorite; ✕ stops
tracking (the row is kept, hidden).

Historical tickets were recovered from sent report emails: `python scripts/import_email_positions.py`
(Apple Mail, messages from `REPORT_SENDER`) or `--dir <folder of .eml/.mbox>`; idempotent by Message-ID.

## Rule cards and alerts (Forecast Lab tab, top)

Three cards are plain rules on stored runs and live Yahoo quotes: no model output. Every row is graded on history
and only a row with tested evidence can say "Buy"; hover any cell for what it means and where it comes from.

- **0 · Signal Watch** (`core/forecast.py: signal_watch`): the triggers the model cards compute (fair value, momentum
  flip, positioning, option pricing, dealer gamma, macro, engine ticket), fired or waiting, with the action.
- **12 · Day Scanner** (`core/watch.py`, `/api/scanner`): the dip-in-an-uptrend rule (2-day RSI under 10 or 5, above
  the 200-day average) on a saved watchlist (`+ ADD`, stored in `scanner_watchlist.json`). SPY trades as a call spread
  of at most $300 and opens a paper position (`v2_watch_positions`) that a sell alert closes 5 trading days later.
- **13 · Edge Lab** (`core/edges.py`, `/api/edges`): the published edges (reversal, post-earnings drift, turn of month,
  pre-Fed day, trend, 12-month momentum, volatility premium, overnight vs intraday, factor profile) tested on any
  ticker's own history; `QUERY` analyses a symbol, `+ TRACK` saves it (`edges_tracked.json`).

Alerts: `python main_pipeline.py signal-alerts` (launchd `com.vlad.signalalerts`, every 5 minutes, installed by
`./setup.sh --schedule`) pushes to `NTFY_URL` and raises a macOS banner when a row goes from waiting to FIRED; it
exits outside the NYSE session. `--dry-run` prints the live states, `--test` sends a sample.

## Forecast Lab (terminal tab, SPY / SLV toggle)

Nine cards computed once per run inside the pipeline (stored in the snapshot as `forecast`, summarized in the
tactical XML as `<forecast_lab>`, served by `/api/forecast?ticker=SPY|SLV`):

1. **Market-implied range** — Breeden-Litzenberger risk-neutral distribution from the captured chains: ranges,
   P(up), P(±5%), skew, walls, at 1D/1W/1M/1Y (horizons rescaled from the nearest listed expiry).
2. **Volatility forecast** — HAR-RV (Corsi 2009); level or log spec chosen on a validation slice; compared with
   implied (VRP); out-of-sample RMSE vs a naive forecast is shown, including where it loses.
3. **Trend / CTA** — 1/3/6/12-month time-series momentum replication, flip levels, SMA state, exposure after
   ±2/±5% moves, historical conditional odds (withheld when fewer than 10 independent windows).
4. **Positioning** — CFTC COT (managed money / producers / swaps for silver; leveraged funds / asset managers /
   dealers for the S&P), 3-year COT index, forward returns at extremes; SLV ounces in trust + NAV premium.
5. **Silver fair value** — weekly regression on gold, 10y TIPS real yield, DXY, copper, industrial production;
   gap, z-score, half-life, reversion backtest, gold/silver ratio.
6. **Macro regime** — NY Fed yield-curve recession probit, NFCI, Sahm rule, HY OAS, real yields.
7. **Calendar** — FOMC day / day before, CPI day, OPEX week, turn of month, weekday effects with n and t-stats;
   upcoming events (Fed calendar, FRED CPI schedule).
8. **Mechanical flows** — vol-control exposure and scenario flows, leveraged-ETF rebalance per 1% (fund AUMs),
   dealer gamma hedging, CTA exposure sensitivity.
9. **Scorecard** — every live run logs its forecasts (`v2_forecasts`); graded once the target date passes
   (hit rate, 68% coverage, Brier) next to the walk-forward backtests.

Extra inputs per run: ~6 FRED series (+CPI dates), 2 CFTC requests, iShares page, Fed calendar, leveraged-ETF
AUMs, 5-year daily histories. Error messages are credential-redacted before anything is stored or served.

10. **Diesel & refining** — diesel / gasoline / 3-2-1 crack spreads (NY Harbor ULSD, RBOB, WTI futures) vs 5-year
    and seasonal norms; EIA weekly refinery utilization by PADD, distillate & gasoline stocks vs the 5-year same-week
    range, days of supply; 10-year maintenance-season utilization path; CFTC NY Harbor ULSD managed money; and a
    **live Texas refinery outage tracker** from TCEQ emission-event filings, matched to EIA refinery capacities and
    classified planned vs unplanned by unit (crude, FCC, hydrocracker, diesel hydrotreater…). Events are captured
    into the lake as they appear, so outage history accumulates. Delivered runs push an NTFY alert for any new MAJOR
    unplanned outage. EIA files are reused until the next Wednesday release. Louisiana is not covered yet.

11. **EIA inventories** — crude, SPR, Cushing, gasoline, distillate, jet, propane and residual fuel on one chart, with
    four views (vs 5-year average, indexed, levels, **days of supply**) over any period: 1M to MAX, one calendar year
    from the Year menu, or two dates. Full weekly history (stocks from 1982, demand and days of supply from 1991) is
    served by `/api/eia_history`, built from the EIA files the pipeline captures; a table under the chart gives
    start, end, average, low and high for the range shown. Weekly EIA captures now hold the full history
    (about 0.6 MB per series before compression); a capture that holds less is fetched again.

Every Forecast Lab card has **⛶ full screen** (Esc or ⛶ to return) and a **? FAQ**: what it shows, how to read it,
how to trade with it, how hedge funds and quant desks use it, and caveats.

## Ask the terminal (console engine v3)

The console answers questions typed in words with a model on this machine. `options_whale/assistant.py` holds
everything server-side; v3 restored the October 3 version (branch `ai-terminal`, never merged), dropped the Claude
Code engine and added the routing below.

- **Brief first.** Every question carries a brief built from the database and the latest run: today's engine tickets
  (local time, contract, entry mid, score, size; a not-live entry quote is flagged), the latest run, fired and
  waiting Signal Watch rows, macro regime and the next CPI / Fed / OPEX dates, and what the brief leaves out. It has
  no clock and no live readings, so it only changes when the data does and a server with a prompt cache reads it
  once per run. The question carries the time. `GET /api/assistant/data?source=brief` shows it.
- **Then routing.** For anything the brief does not show, the model calls `fetch_source` for one to three of the 25
  read-only GET sources in `SOURCES` (Flask test client, unlisted parameters dropped). Large results come back as an
  outline that the model opens with `path=` and `last=`. Positions, Signal Watch, the Day Scanner, the Edge Lab and
  gamma are pre-shaped (`SHAPERS`): plain contract names, local times, trades apart from cash tickets, gamma
  distances already worked out. `calc` does the arithmetic. `/run` and the eBay scrape are not sources.
- **Reasoning.** `ASSISTANT_LOCAL_REASONING=auto` (default): no deliberate reasoning to answer from the brief or to
  pick sources, brief reasoning once data has been fetched. Sent both as `reasoning_effort` and as the chat
  template's `enable_thinking`, so oMLX and vLLM-style servers really switch it off.
- **Limits.** 8 rounds and 16 distinct calls per question (a repeated call is refused), 2,000 tokens per model call,
  `ASSISTANT_MAX_SECONDS` (default 150) per question, `ASSISTANT_MAX_CHARS` (default 12,000) per source. An empty
  reply (a server left loaded but dead after a GPU out-of-memory error) reloads the model once and asks again.
- **Servers.** Default Ollama at `http://127.0.0.1:11434/v1` with `qwen3.6:35b-a3b`; on Ollama the default model is
  kept loaded (`ASSISTANT_KEEP_WARM`, 30 min) and the brief is built when the page opens. This Mac points `.env` at
  oMLX on port 8000 (`Qwen3.8-27B-oQ4e-mtp`, shared with the coding agent; `ASSISTANT_MAX_SECONDS=300`,
  `ASSISTANT_MAX_CHARS=8000`). Do not keep a large Ollama model loaded next to oMLX: together they exceed the GPU's
  memory and both start failing.
- **Measured here (M4 Pro, 48 GB, 2026-10-09), 17 graded questions** (5 answerable from the brief, 5 needing one
  lookup, 7 multi-step): Ollama `qwen3.6:35b-a3b` with reasoning off 14-15/17, median 5-8 s; reasoning low 17/17,
  median 20 s. The old default `qwen3.8:27b-mlx` on Ollama read prompts at about 90 tokens/s: 96 s for the
  engine-position question. oMLX has not been graded yet: it was busy with an agent run during testing.
- **Conversations** live in server memory (the last 40; 8 turns each) and are lost on restart. "New chat" starts one.
- The server has no login. Anyone who can reach the port can ask questions. Keep the port on a private network.

## What changed in values (versioned in snapshots)

- **Databento sides**: `A` = sell aggressor, `B` = buy aggressor, `N` = unknown (was reversed). DBEQ.BASIC
  prints ≥ 10,000 shares carry `N`, so "DP Sentiment" uses the legacy price-vs-VWAP rule for those prints and
  is labelled *VWAP heuristic*. `DP_Bull_Vol`/`DP_Bear_Vol` now hold provider aggressor volume only. The section
  is labelled a block-flow proxy (venue not verified).
- **Zero gamma**: computed (spot where net GEX flips sign, ±10%), else null with a reason. It no longer copies spot.
- **GEX walls**: one window (±10%, first 3 expirations) for SPY and SLV (SLV used ±15% before).
- **Max pain / top contracts**: contracts with unknown OI/volume are excluded, not counted as 0.
- **COMEX**: display in M oz (`99.21M oz`, previously `99,207,707.60M oz`); rows keyed by the report date inside
  the workbook (downloads were labelled with the download date); adjustments shown separately.
- **Calendar**: this week + next week feeds, one dataset for both sections; times converted from UTC (they were
  labelled EST); a failed feed says "unavailable", never "no events".
- **eBay benchmark**: the run's SI=F quote; no hardcoded $67.80 fallback.
- **Execution engine**: same weights; a vote whose inputs are missing is shown as missing (not 0).
- **War Room tiers**: the card now uses the same four tiers as the server and the ledger (below 150 low, 150–250
  moderate, 250–350 elevated, 350+ systemic). The card used to call 150–250 "elevated" and 250+ "systemic". The
  decorative bell curve is replaced by a histogram of the VMRI scores actually recorded in `macro_master_ledger.csv`.
- **Days of supply**: crude now has one (commercial stocks ÷ 4-week average refinery crude runs); it was blank before.
- **Charts**: single-axis panels, true date windows, legacy PNG names; stale inputs are marked; charts 9–11
  use the local formulas found in `visualize_volume.py`.

## Rollback

v2 only added tables and files to `CME_Data`, so the legacy code also runs against the current data folder as-is.
Before the first v2 import, take a full copy of `CME_Data` and keep it.

## New Mac setup

`./setup.sh` — installs Homebrew/Python if missing, builds `.venv` from `requirements.txt`, creates `.env` from
`.env.example`, installs the `options_whale` server as a login service (`com.vlad.optionswhale`, log in
`~/Library/Logs/optionswhale.log`) and adds the menu bar icon (SwiftBar plugin in `menubar/`). Re-runnable.
`--no-menubar` skips the icon; `--uninstall` removes the login service. Per-machine settings live in `.env`:
`OPTIONS_WHALE_PORT` (default 8080) and `PORTFOLIO_DATA_DIR`. It does not copy `CME_Data` or schedule the daily
pipeline run.
