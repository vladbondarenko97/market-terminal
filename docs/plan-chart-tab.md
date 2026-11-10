# Plan: CHART tab (replaces TradingView)

Status: **not started**. Written 2026-11-10. Nothing here has been built yet.

Goal: a fourth terminal tab, **CHART**. It shows SPY by default and any ticker you type (NKE, anything) on
1m to 1M timeframes, with zoom, pan and range picking. It can show several charts at once (SPY next to VMRI, SLV
and distillate stocks), draws supply and demand zones on every chart, and overlays our own data: GEX walls, dark
pool prints, engine tickets, macro events and the Kronos and baseline fans. Free to run, and it keeps every bar it
fetches.

## The lever: TradingView's own free chart library

[Lightweight Charts](https://github.com/tradingview/lightweight-charts) (Apache-2.0, about 45 KB, from jsDelivr
like Chart.js already is) is the engine behind TradingView's own charts, minus the paid parts. It already has
candles, volume, wheel zoom, drag pan, crosshair, several price scales, panes (v5), price lines, markers, and
custom drawing "primitives" (rectangles for zones). That is most of "rival TradingView" for one `<script>` tag.
Pin an exact 5.x version. The library adds a small TradingView logo link by default; leave it on, the licence
asks for attribution.

What we add on top is what TradingView does not have: our stored data on the same time axis.

## What we already have (measured on the live DB, read-only, 2026-11-10)

| Data | Where | Depth |
|---|---|---|
| Daily OHLCV, SPY and others, `period=max` | `v2_payloads` kind `ohlcv` (3,237 captures, many duplicate 5y pulls) | SPY since 1993 |
| Daily OHLC, watch symbols (SPY, SLV, QQQ, NKE, AAPL…) | `CME_Data/cache/bars_*.csv` (`core/watch.py`) | full history |
| SPY 1h | `ohlcv` 1h/1mo, 87 captures | about 6 months if stitched |
| SPY 1m wicks | `spy_wicks_1m.json` (repo root) | one dump |
| EIA weekly (distillate/gasoline stocks, days of supply, PADD, runs) | `eia_weekly` payloads, `/api/eia_history` | since 1982 |
| Chaos score | `v2_observations` `chaos.score` | since 1930 |
| RRP, repo, SOFR, EFFR, BGCR | `fedops.*` observations | since 2014 / 2018 |
| CME volume and OI per product (ES, SI, ZN…) | `cme.*` observations | since 2024-12 |
| VMRI score | `master_market_data.csv` ledger (`/api/vmri_history`), `macro.vmri.score` | ledger + since 2026-09 |
| Put/call, SPY short volume | `chaos.*` observations | since 2023-08 |
| GEX walls, max pain, dark pool blocks, tickets, fans | latest snapshot, `trade.block` obs, `v2_trade_signals`, `/api/fans` | per run |

**What we don't have:** intraday bars below 1h. Yahoo only serves 1m for the last ~30 days (7 days per request),
2m to 30m for 60 days and 1h for 730 days. So intraday history starts at the first backfill and grows from there;
it can't be pulled years back from Yahoo. **Alpaca** (free Basic account, key in hand) fills that gap: 1m bars for
US stocks and ETFs back to 2016 from the whole market (SIP), except the most recent 15 minutes, 200 requests a
minute, 10,000 bars a request. Yahoo stays the source for futures (SI=F, CL=F…), indexes (^VIX) and the last
15 minutes.

## Design

### Where it lives: Oracle by default, Hugging Face as backup

Disk measured 2026-11-10: the Mac has **7.2 GB free (99 % full**, mostly not this project; `CME_Data` is 1.1 GB);
the Oracle server has **106 GB free**. So the chart's data lives on Oracle and the Mac stores none of it.

Oracle already gets a copy of `CME_Data` (`~/dev/infra/oracle/market-sync.sh`, hourly and after each run, into
`/srv/market/data/`) and serves market.vlad.yt from it through `compose.yaml`. That copy is **overwritten** from the
Mac every hour, so the chart can't write into it. The chart gets its own folder beside it, which the sync never
touches:

| On Oracle, `CHART_STORE=/srv/market/chart/` | What | Size |
|---|---|---|
| `chart.db` | its own lake, same schema (`lake.migrate()`): `v2_payloads` and `v2_fetch_log` for every chart fetch, `v2_bars` (1d, 1wk), `v2_news_items` (day-view headlines), intraday fans | grows with raw captures; 106 GB free |
| `bars/<SYMBOL>/<tf>/<YYYY-MM-DD>.parquet` | intraday bars (`1m`, `5m`, `1h`), one file per finished session, written once and never rewritten, zstd, float32 prices | 1m with pre/post ≈ 3 MB per symbol-year |

- **Mac:** the chart routes forward to Oracle's terminal over Tailscale when `CHART_REMOTE` is set (one passthrough
  for `/api/chart/*`, about 15 lines). The tab looks the same on either host. Without `CHART_REMOTE` (tests,
  another machine) the routes read a local `CHART_STORE`.
- **Oracle:** the `bars` job runs there (a systemd timer, or a compose service like the market MCP): at 09:00,
  12:30 and 16:30 New York time on trading days, plus the on-demand jobs below. Yahoo and Alpaca are plain HTTPS
  APIs, so the datacenter-IP risk that affects CME's site doesn't apply. It reads the synced `portfolio.db`
  read-only for the lake backfill and the snapshot overlays.
- **Backups to Hugging Face** (private dataset repo, e.g. `<you>/market-chart-store`, token `HF_TOKEN`):
  - **Daily** at 17:00 New York: `hf upload` of the new day files. HF deduplicates content, so only new files are sent.
  - **Weekly** (Sunday): a consistent `sqlite3 .backup` of `chart.db`, zstd-compressed, uploaded as
    `db/chart-YYYY-WW.db.zst`.
  - Both report to Gatus the way `market-sync.sh` does, so a failed backup shows red. The scripts go in
    `~/dev/infra/oracle/` next to `market-sync.sh`.
  - Restore is `hf download` into `CHART_STORE`. Check the private-storage limit shown in your HF account settings
    before the 10-year backfill; at about 30 MB per symbol it's small either way.
- Hard rule 4 holds: day files are only ever added, and `v2_bars` and `v2_news_items` only get `INSERT OR IGNORE`.
  `portfolio.db` is untouched.

```sql
CREATE TABLE IF NOT EXISTS v2_bars (
    symbol TEXT NOT NULL, tf TEXT NOT NULL, ts TEXT NOT NULL,      -- ts = bar open, UTC ISO
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    source TEXT NOT NULL, payload_id INTEGER, fetched_at TEXT,
    PRIMARY KEY (symbol, tf, ts)) WITHOUT ROWID
```

- Only **finished** sessions and bars are stored. Today's bars come from the live poll and the job's 7-day fetch,
  and are written as today's file once the session closes. No raw payload is kept for the 15 s live polls.
- Store native timeframes only: `1m`, `5m`, `1h`, `1d`, `1wk`. 15m, 30m, 4h and 1M are resampled on read from the
  finest stored timeframe that covers the requested range.
- **Tracked = has bars.** A symbol is tracked once it has daily rows in `v2_bars`, so there's no watchlist to
  maintain. Type NKE once and every job keeps it up to date from then on.
- Pre/post-market bars are stored; the chart has an RTH toggle.

### Getting the bars (`core/bars.py`, new; runs on Oracle)

1. **Backfill from what we already captured (no network, free):** parse every stored `ohlcv` payload in the synced
   `portfolio.db` (read-only), the `cache/bars_*.csv` files and `spy_wicks_1m.json`. Run it with
   `main_pipeline.py bars --from-lake`.
2. **Network backfill:** `main_pipeline.py bars --backfill [SYMBOLS]` pulls 1d max, 1h 730d, 5m 60d and 1m in four
   7-day chunks from Yahoo. With `--deep` it also pulls 1m since 2016 from Alpaca (new fetcher `alpaca_bars` in
   `core/sources.py`, `https://data.alpaca.markets/v2/stocks/bars`, provider limit 3). Everything goes through a
   `SourceSession` on `chart.db`. It saves per day file, so a killed run resumes.
3. **Scheduled job:** the same command without `--backfill` takes 1m/7d and 1d/7d for each tracked symbol. Three
   runs a day overlap the 7-day window, so there are no gaps. Errors are logged to `v2_fetch_log` and the job exits
   non-zero for Gatus.
4. **A ticker you type that we don't have:** `/api/chart/bars` finds no rows, starts
   `main_pipeline.py bars --symbol NKE` in the background (the way `POST /run` starts a run, one at a time per
   symbol) and answers `{"status": "loading"}`. The page shows "Loading NKE history…" and retries every 3 s until
   the bars are there (usually under 20 s). The route still only reads stored data, and the fetching stays in
   pipeline code behind `SourceSession`. Symbols are checked against `^[A-Z0-9.^=\-]{1,15}$` before anything
   starts; anything else is refused.
5. **Live:** while the tab is open in market hours, the page polls `/api/chart/live?symbol=` every 15 s. Oracle
   fetches Yahoo 1d/1m at most once per symbol per 15 s (in-memory memo) and returns the forming bar. Each bar
   shows its timestamp, so a delayed feed (futures) is visible.

**Alpaca keys:** these are paper-account keys. They read market data from `data.alpaca.markets` and are never used
to trade, so the `paper-api.alpaca.markets` trading URL isn't needed. Bitwarden keeps one item, `ALPACA_API_KEY`
(username = key id, password = secret). On Oracle, secrets live in `.env` (no Keychain on Linux), as
`ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY`, both read through `config.py`.

### Routes (`options_whale/api_router.py`)

| Route | Returns | Stored/live |
|---|---|---|
| `GET /api/chart/catalog` | every chartable series: `{id, label, kind: ohlc|line|step, freq, first, last}`; bar symbols from `v2_bars`, line series from `v2_observations` (metric+entity), EIA, VMRI ledger | stored |
| `GET /api/chart/bars?symbol=&tf=&from=&to=` | columnar `{t:[], o:[], h:[], l:[], c:[], v:[]}`, max 20k bars, resampled when needed | stored (+ first-fetch, step 4) |
| `GET /api/chart/series?id=&from=&to=` | `{t:[], v:[]}` for a non-price series (distillate stocks, VMRI, RRP, OI…) | stored |
| `GET /api/chart/overlays?symbol=` | latest snapshot levels: call/put wall, zero gamma, max pain, dark pool blocks, open tickets (entry/stop/target), macro calendar and FOMC markers, fan quantiles (Kronos + baseline) | stored |
| `GET /api/chart/live?symbol=` | forming bar + newly closed bars | live |

`/api/chart/series` is generic: one SQL over `v2_observations` by `metric_id`/`entity` gives most series without
per-series code. EIA and VMRI each get a small adapter that reuses `refining.inventory_history` and
`get_vmri_history`.

### The tab (`terminal.html` + `static/chart.js`, new)

- **Toolbar:** a symbol box (type anything; it autocompletes from the catalog, and an unknown ticker loads by
  step 4), timeframe buttons `1m 5m 15m 30m 1h 4h 1D 1W 1M`, range buttons `1D 5D 1M 3M 6M YTD 1Y 5Y MAX`, two
  `<input type="date">` for a custom range, and a layout picker `1 | 2 | 2×2 | 3×2`.
- **Multi-chart:** each cell has its own symbol, timeframe and overlays. With **Sync** on, crosshair and visible
  time range follow across cells (`subscribeVisibleTimeRangeChange`, `setCrosshairPosition`).
- **Compare in one cell:** add series to a chart. Price symbols can be shown as % change from the left edge of the
  view (SPY vs SLV vs QQQ). Non-price series (VMRI, distillate stocks, RRP) get their own price scale, drawn as a
  step line at intraday timeframes. Example: SPY 1h candles, a VMRI step line on the left scale and distillate
  stocks in a lower pane.
- **Measure:** shift-drag between two candles shows Δprice, Δ%, bars and elapsed time (one primitive).
- **Indicators, client side:** volume, SMA/EMA, session VWAP, RSI, ATR, Bollinger. A few lines each.
- **Drawings:** horizontal line and trendline, saved per symbol in `localStorage`. Fib and the rest later.
- The layout and cells are kept in `localStorage`, so the tab reopens as you left it.

### Supply and demand zones (on every chart, any timeframe)

These are computed in the browser from the bars on screen, the same way SMA or VWAP are. They are a drawing, not
a stored value, so they don't go in the snapshot (rule 5 is about stored models).

- **Base-and-impulse rule:** ATR(14). An impulse candle has range ≥ `k × ATR` (k = 1.5) and body ≥ 60 % of its
  range. Its base is the 1–3 candles before it whose body is ≤ 50 % of their range. The zone runs from the lowest
  low to the highest high of the base: a **demand** zone if the impulse goes up, a **supply** zone if it goes down.
- A zone extends right until a candle closes through it (then it's mitigated and drawn faded). It counts touches,
  and the chart keeps the 6 strongest fresh zones (impulse size × freshness).
- `k`, the body ratios and the zone count are settings in the toolbar, because what works differs by timeframe.
- **Our zones,** which TradingView can't draw: the GEX call/put walls and zero gamma as bands, and dark pool block
  clusters as zones weighted by notional. A toggle switches between "classic", "ours" and both.
- Drawn as rectangle primitives (adapted from the library's official rectangle plugin example).

### Day view: click a candle, see that day

Click any candle (or pick a date) and a side panel opens with everything stored for that day:

- **Prices:** that day's OHLCV for every tracked symbol, with % change, and a 1m/5m mini-chart of the session when
  intraday bars exist.
- **Every catalog series as of that day:** the latest value on or before the date (VMRI, Chaos, distillate stocks,
  days of supply, RRP, SOFR, CME OI, put/call, short volume…), with its week-over-week change.
- **Our runs that day,** if any: the snapshot's GEX walls, max pain, tickets opened or closed, forecasts made and
  how they graded, and the briefing narrative. Links open the run with `replay`-equivalent stored data.
- **Calendar:** macro releases, FOMC, Fed speeches, Treasury auctions and EIA reports on that date (already stored).
- **Headlines:** that day's top headlines (below).

Route: `GET /api/chart/day?date=YYYY-MM-DD&symbol=`, stored data plus the headline first-fetch.

**Headlines for any past day.** Our RSS archive (`v2_news_items`, 3,300+ items) only reaches back to about
2026-08. Older days come from two free sources, fetched the first time a day is opened and then **kept**:

| Source | Covers | How |
|---|---|---|
| Wikipedia "Portal:Current events/<Year Month Day>" | about 1995 to now, curated world/business events | MediaWiki API, plain JSON, no key |
| GDELT DOC 2.0 API | 2017 to now, global news, filterable by query (`SPY`, `silver`, `Fed`…) | free JSON, no key, rate-limited (one request every 5 s) |

Both go through `SourceSession.fetch()` (new providers `wikipedia` and `gdelt` in `PROVIDER_LIMITS`), so the raw
response is captured in `v2_payloads`. The headlines are inserted into `v2_news_items` with `source` = `wikipedia`
or `gdelt`, `published_at` = that day and `first_seen_run` = `chart:<date>`. That table's `url_hash` primary key
dedupes them, so opening a day twice never refetches or duplicates. A day that has been fetched once is answered
from the table; "no headlines found" is cached as a payload, so it isn't retried every click. Optional:
`main_pipeline.py headlines --backfill 2015-01-01` walks every trading day once (about 3 h for GDELT at its rate
limit; operator-run).

### Kronos

1. **Now (free):** `/api/chart/overlays` returns the stored fans for the 12 daily series that have them (SPY, SLV,
   GC, SI, HG, CL, HO, RB, NG, DXY…): the 10–90 % band and the median, Kronos and baseline side by side, with the
   existing "Kronos loses to baseline out of sample" tag. They are drawn past the last candle on the 1D view.
2. **Intraday Kronos (pipeline):** Kronos was trained on intraday K-lines too. Each run, after the bars step,
   `core/fans.py` builds `kronos_fan_intraday_v1` for SPY and SLV on 5m (48 steps) and 1h (24 steps) from
   `v2_bars`, with a matching baseline. They are stored and graded like the daily fans (new version constant and a
   line in `value-changes.md`). Measured speed is about 6 s per origin on MPS; this runs in the Oracle `bars` job on 2 Arm cores (no GPU), so it's measured there first (expect several times slower; fine three times a day). It
   stays labelled "unscored" until grading has a sample.
3. **Other symbols (later):** a "Kronos" button on a cell queues a request row. The next run, or
   `main_pipeline.py fan --symbol NKE --tf 15m`, computes and stores it, and the chart picks it up. The server
   never runs Kronos in a request.

## Phases (best value for the work first)

| # | Ships | Work |
|---|---|---|
| 1 | Tab, Lightweight Charts, `v2_bars` + Parquet day files, lake + Yahoo + Alpaca backfill, `/bars` + `/catalog`, any ticker on first type, timeframes, zoom/pan/ranges, volume | ~1 day |
| 2 | Multi-chart grid + sync, compare mode, `/series` (VMRI, distillate, RRP, OI…), live poll, collect step | ~1 day |
| 3 | Supply/demand zones, `/overlays` (GEX, dark pool, tickets, events, daily fans), measure tool, indicators | ~1 day |
| 3b | Day view (`/api/chart/day`), Wikipedia + GDELT headlines saved to `v2_news_items` on first open | ~½ day |
| 4 | Intraday Kronos in the pipeline, drawings, Kronos queue for other symbols | ~1–2 days |

Phase 1 alone already replaces TradingView for daily and recent intraday SPY/NKE/anything.

Execution: in a worktree (the main checkout serves the terminal), one Sonnet subagent per phase against this
spec. Opus reviews each phase: tests, an offline run, `replay`, `node --check`, and loading the tab against a
scratch copy of the data.

## Tests (offline, fixed dates)

`tests/test_bars.py`: CSV payload → rows; `INSERT OR IGNORE` keeps the first copy of a bar; the forming bar is
never stored; 1m→15m and 1h→4h resampling on session boundaries; the 20k cap; symbol validation refuses
`../x` and `;`; a day's headlines are fetched once and deduped by `url_hash` (fixture GDELT/Wikipedia JSON); `/api/chart/bars` on an unknown symbol with the fetcher patched (no network). The zone rule gets
a small `node` self-check on a synthetic series (one known demand and one known supply zone).

## Docs to update in the same change

`docs/api.md` + `/help` (6 routes), `docs/data.md` (`v2_bars`, `bars/` folder), `docs/configuration.md` + `.env.example` (`CHART_STORE`, `CHART_REMOTE`, `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`, `HF_TOKEN`), `docs/plan-oracle-move.md` (the chart is the first piece that lives on Oracle), `docs/terminal.md` (CHART tab),
`docs/operations.md` (`bars` subcommand), `docs/architecture.md` (`core/bars.py`, `static/chart.js`),
`docs/value-changes.md` (intraday fan, phase 4).

## Commands to hand over (they write real data or touch secrets, so the operator runs them)

On the Mac, once, to put the Alpaca keys on Oracle without printing them:

```bash
export BW_SESSION=$(bw unlock --raw)
ssh ubuntu@brinewater "umask 077; cat >> /srv/market/.env" <<< "ALPACA_API_KEY_ID=$(bw get username ALPACA_API_KEY)
ALPACA_API_SECRET_KEY=$(bw get password ALPACA_API_KEY)
HF_TOKEN=$(bw get password HF_TOKEN)"
```

(Make an HF write token scoped to the one dataset repo and save it in Bitwarden as `HF_TOKEN` first. The `.env`
path is whatever `compose.yaml` mounts on Oracle; check before running.)

On Oracle, after Phase 1 is deployed there (through the compose image, `docker compose run --rm terminal …`):

```bash
main_pipeline.py bars --from-lake                          # free, no network
main_pipeline.py bars --backfill                           # Yahoo, ~1 min per symbol
main_pipeline.py bars --backfill --deep SPY SLV QQQ GLD    # Alpaca 1m since 2016, ~2 min per symbol
```

On the Mac: `CHART_REMOTE=http://brinewater:<port>` in `.env`, then restart the terminal (launchd is yours).

## Decisions (all made 2026-11-10)

1. ~~Live poll capture~~: decided, the live poll saves no raw payloads; finished sessions are written as day files.
2. ~~First-fetch in a route~~: decided 2026-11-10, reframed as a background `bars --symbol` job (step 4).
3. ~~Pre/post-market~~: decided, stored as compact Parquet (see Storage).
4. ~~Alpaca~~: decided, key available; deep 1m backfill moves into Phase 1.
5. ~~Where it's stored~~: decided, Oracle `CHART_STORE` is the home; Hugging Face private dataset gets daily day files and a weekly `chart.db` backup; the Mac stores nothing.
