# Value changes

This file records every change to **how a value is computed**. A reader comparing two runs, a chart that spans a
change, or an old email against a new one should be able to explain any jump from this page.

## Rule

When you change how a stored value is computed:

1. Bump the version constant of that model (table below). The version is stored with the value in each snapshot, so
   old and new runs stay distinguishable.
2. Add a bullet under [Changes](#changes): what the value was, what it is now, and why.
3. Do not edit old snapshots. They keep the old version and the old value. See
   [Architecture: design rules](architecture.md#design-rules).

## Model versions

| Constant | Current value | Covers |
|---|---|---|
| `VMRI_VERSION` (`core/metrics.py`) | `vmri_v1` | VMRI formula and tiers |
| `GEX_VERSION` (`core/metrics.py`) | `gex_v2` | Dealer gamma exposure and walls |
| `ZERO_GAMMA_VERSION` (`core/metrics.py`) | `zero_gamma_v1` | Zero-gamma level |
| `FLOW_VERSION` (`core/metrics.py`) | `block_flow_v2` | Databento block flow and sides |
| `MAX_PAIN_VERSION` (`core/metrics.py`) | `max_pain_v2` | Max pain and top contracts |
| `DIVERGENCE_VERSION` (`core/metrics.py`) | `oi_divergence_v2` | OI divergence |
| `EXECUTION_VERSION` (`core/metrics.py`) | `execution_v2` | Execution engine votes and ticket |
| `FEATURES_VERSION` (`core/metrics.py`) | `features_v1` | `<model_features>` block in the tactical XML |
| `VERSION` (`core/forecast.py`) | `forecast_v2` | Forecast Lab cards 1–9 |
| `VERSION` (`core/refining.py`) | `refining_v1` | Diesel & refining and EIA inventory models (cards 10–11) |
| `CHAOS_VERSION` (`core/chaos.py`) | `chaos_v1` | Chaos / Peace index (card 14) |
| `FEDOPS_VERSION` (`core/fedops.py`) | `fed_ops_v4` | Fed / Treasury operations block and its summaries (card 15) |
| `SHANGHAI_VERSION` (`core/metrics.py`) | `shanghai_dated_v1` | Date-paired Shanghai silver spread (`shanghai.dated`) |
| `EAGLE_VERSION` (`core/metrics.py`) | `eagle_premium_v1` | Silver Eagle premium statistics, units-sold flow and dealer-book summary (`ebay.premium`, `ebay.flow`, `ebay.book`) |
| `PAPER_PHYSICAL_HISTORY_VERSION` (`core/metrics.py`) | `paper_physical_history_v1` | Paper:physical ratio history statistics (`paper_physical.history`) |
| `SCHEMA_VERSION` (`core/lake.py`) | `v2.1` | SQLite `v2_*` schema, stored on every `v2_runs` row |
| `HORIZON_VERSION` (`core/positions.py`) | `horizons_v2` | Engine ticket +1D / +1W / +2W values. Not stored in a snapshot: positions are priced when the terminal asks, so the version is returned as `_version` inside each ticket's `horizons` dict. |

Each run also stores `code_version` (the short git commit, with `-dirty` if the tree had uncommitted changes) in
`v2_runs`, so any snapshot can be traced to the code that produced it.

## Changes

Newest first, then the changes that came with the v2 pipeline.

- **Fed / Treasury operations** (`fed_ops_v4`, October 2026): new keys only: `repo.markets.*.peak_12m` and `peak_all_time`,
  `repo.rrp_trend` (a sentence), `auctions.by_tenor.*.last_direct_pct` and `last_indirect_pct`. The terminal rebuilds an
  older block from the stored rows, so these are there before the next run.
- **Fed / Treasury operations** (`fed_ops_v3`, October 2026): `repo.monthly` keys renamed: `rrp_bn` and `repo_bn` (the
  month's mean daily balance) are now `rrp_avg_bn` and `repo_avg_bn`, next to the new `*_max_bn`, `*_last_bn` and `*_last_date` (the period's last day with an operation); the
  rate means are `sofr_avg`, `tgcr_avg`, `bgcr_avg`, `effr_avg` and SOFR volume is `sofr_volume_avg_bn`. New: `repo.yearly`
  (same fields per year), `ago` on each market and rate, `buybacks.this_year_vs_last_year_same_dates`.
- **Fed / Treasury operations** (`fed_ops_v2`, October 2026): `auctions.upcoming` now comes from the newest captured
  auction calendar, without auctions that already have a stored result, and `auctions.upcoming_status` is `fresh`, `stale`
  (that run's fetch failed: the earlier calendar is shown, with `as_of`) or `missing` (no calendar captured). In `fed_ops_v1`
  a failed fetch gave an empty list with status `missing`. New keys, no other change of meaning: `buybacks.largest_5`,
  `buybacks.yearly[].by_operation_type`, `auctions.by_tenor[].by_year`.
- **Fed / Treasury operations** (`fed_ops_v1`, new, October 2026): snapshot key `fed_ops` and observations `fedops.repo_day`,
  `fedops.ref_rate`, `fedops.buyback`, `fedops.auction`; no earlier value changes meaning.
- **Chaos / Peace Treasury-supply levers** (`chaos_v1`, October 2026): the "Treasury bills offered" and "Treasury coupons
  offered" lever rows were always `missing` ("no auction records") because the fetcher read an `auctionDate` field TreasuryDirect
  does not send. The auction date is now the competitive close date, so both rows carry values from the next run. They sit
  outside the score, so the score is unchanged and the version stays `chaos_v1`.

- **Chaos / Peace long history** (`chaos_v1`, October 2026): new key `chaos.long` (history span, eras, stress episodes, the regime study,
  chart markers) and new observations `chaos.score`, one per day back to 1930. `chaos.election` now follows the next general election
  and carries `kind` (it reads 2026-11-03 until 60 trading days after it, as before); `chaos.event_study` gained presidential rows, rows
  for the score itself and the keys `kind` and `unit`, with the midterm rows unchanged. The score formula and every existing value are
  unchanged, so the version stays `chaos_v1`. The run makes up to nine more requests when its long captures are over 30 days old
  (full daily histories of SPY and seven volatility indices, and FRED `USEPUINDXD` and `USREC`).
- **Card statistics moved into the run** (`eagle_premium_v1`, `paper_physical_history_v1`, October 2026): the Silver Eagle premium statistics,
  the units-sold flow, the dealer-book summary and the paper:physical history statistics are computed by the pipeline and stored
  (`ebay.premium`, `ebay.flow`, `ebay.book`, `paper_physical.history`). They were worked out per request by `/api/arbitrage_history` and in the browser. Same definitions and the same
  numbers; they now carry a version and can be replayed. The cards show them from the first run made with this code. No existing stored
  value changed.
- **Shanghai silver spread** (`shanghai_dated_v1`, October 2026): new snapshot key `shanghai.dated`. It pairs each SGE silver benchmark with the
  SI=F close and the CNY=X close of the same date; terminal panel 14 shows it. The older values (`shanghai.premium_tax_adj`,
  `shanghai.raw_spread`, the ledger column `SHFE_Premium`) are computed as before: the newest fix the source returns, which is 2 to 8 days old,
  against the run-time COMEX quote. No existing stored value changed. The run makes one more request (CNY=X, 5 years of daily closes).
- **SLV / SPY flow sides in the terminal** (`/api/institutional_history`, panel 9, October 2026): the route adds the stored side information
  per row. The ledger columns `DP_Bull_Vol` and `DP_Bear_Vol` are unchanged: they are the volume whose trade side the feed reported, which is 0
  for SLV in every run since 2026-09-29, so the panel now also draws the unknown-side volume, split by price against VWAP and marked as a guess.
- **Silver Eagle premium history** (`/api/arbitrage_history`, terminal panel 5, October 2026): every premium is now the retail price over the
  silver futures price recorded by the same pipeline run (`macro_master_ledger`: `Retail_Silver_Cheapest`, `Retail_Silver_Avg`, `COMEX_Silver`).
  The route used to serve the premium columns of `physical_arbitrage_ledger.csv`, whose benchmark was a fixed $67.80 in 69 rows and a stale
  price in many more before 2026-09-29: that series ran from -2.1% to +39.0%, the paired one runs from 8.2% to 20.7%. Rows of the last
  two weeks are identical in both. `labels` now carry the year (`YYYY-MM-DD HH:MM`), and scans started by hand with the Silver Eagle
  button no longer appear in the history (they are still appended to the CSV). No stored value and no model version changed.

- **Forecast Lab card 7 (`forecast_v2`)**: `calendar.effects` gained `payrolls_day`, `ppi_day`, `gdp_day`, `pce_day`,
  `retail_sales_day` and `fomc_minutes_day` rows (release-day close-to-close, each also graded with `core.watch.grade()`), and a
  `fed_speaker_day` row (days with at least one speech by a Fed Board member: Chair, Vice Chair or Governor, from the Fed's speech list; same grading).
  Existing rows and `upcoming_7d` are computed as before; no existing value changed.
- **News section and release calendar**: new snapshot key `news` and `calendar.upcoming_releases`; `/api/macro_news` now reads
  the stored `news` section instead of one live Yahoo feed. No existing stored value changed.
- **Chaos / Peace index** (`CHAOS_VERSION` `chaos_v1`, new): snapshot key `chaos`; no earlier value changes meaning. The ^VIX history
  behind `price_stats.VIX` stays 1 year; the index fetches its own 3-year ^VIX and the six new volatility series.

- **Terminal live routes** (computed per request, never stored; listed here because the numbers on screen move):
  - `/api/gex` computes zero gamma with the pipeline's `gex_profile()` (`gex_v2`, `zero_gamma_v1`) instead of
    returning spot; walls and strikes come from the same profile.
  - `/api/darkpool` reads Databento sides the pipeline's way (`block_flow_v2`: `B` buy, `A` sell, `N` unknown), so its
    bias and BUY/SELL labels are the opposite of before. It uses the VWAP heuristic when less than half the block
    volume has a known side, and says which method it used.
  - Time Arbitrage: the oscillator is the mean of the components that have data (it divided by 3 even when GEX and
    DIX were empty); IV bleed, vanna and charm use the 15 strikes nearest spot and the probability matrix the 5
    nearest (they used the lowest strikes); contracts with unknown open interest are skipped, not weighted as 1.
  - `/api/option_calc` uses the requested ticker's realised volatility (it always used SPY's) and reports
    `market_price: null` when there is no quote (it showed the Black-Scholes price as if it were one).
  - No invented fallbacks: where these routes used VIX 20, IV 0.20, a 5% rate, 15% volatility or zero gamma 0.5%
    below spot, the value is now `null` with a reason.
- **Engine ticket horizon marks** (`HORIZON_VERSION` `horizons_v1` to `horizons_v2`): the `market` basis of the
  +1D / +1W / +2W values used the **first** mark recorded on the target day (the opening run's chain). It now uses the
  **last** one (`positions._market_mark()`), the run closest to the close. Tickets whose target day had both a
  morning and an afternoon run change value; days with one run do not. The change applies to every ticket the next
  time the terminal asks, old ones included, because these values are computed on request and never stored.
- **Macro ledger `GEX`**: the column was written empty by every run. It now holds SPY's net dealer gamma from the run's
  snapshot (`options.SPY.gex.net_gex`, USD of delta change per $1 move, the same figure and unit as
  `institutional_ledger.Net_Gamma`), and stays empty when the run has no GEX. `DIX` is still always empty (no data
  source). Rows already written stay empty. The terminal's oscillator reads this column, so its GEX component starts
  to take part once a few live runs have filled it (30-day baseline).
- **Which snapshot is "latest"**: `v2_latest_snapshot` used to be the newest committed snapshot of any run, so an
  `--offline` test run replaced the live run in the terminal, `replay` and the legacy wrapper scripts. It is now the
  newest committed **live** snapshot, and the newest of any other mode only when no live one exists. No stored value
  changes; the view definition is replaced in place by `lake.migrate()`.
- **CME volume backfill**: a run now requests every listed workbook the lake does not hold (newest first, still capped
  by `--cme-max-files`), including trade dates older than the newest one held; it used to request only newer dates.
  When an old gap fills, series that look back over history (percentiles, divergence baselines, the 30-session
  window) can move on the next run. Nothing already stored changes.
- **Forecast Lab without inputs**: with no price history the build used to raise and the whole `forecast` block was
  stored as `error`; every card is now `missing` with its reason and the block is `partial`. The `positioning` block's
  top-level `status` is `missing` (it was `fresh`) when neither COT group nor the SLV trust reading is available. No
  number changes, so `forecast_v1` is unchanged.
- **Imports**: `daily_market_report.txt` in a daily folder is no longer imported as a payload (the rule meant to skip
  rendered reports never ran). No stored value changes.

These changes came with the v2 pipeline, against the legacy scripts it replaced.

- **Databento sides**: `A` = sell aggressor, `B` = buy aggressor, `N` = unknown (was reversed). DBEQ.BASIC
  prints ≥ 10,000 shares carry `N`, so "DP Sentiment" uses the legacy price-vs-VWAP rule for those prints and
  is labelled *VWAP heuristic*. `DP_Bull_Vol`/`DP_Bear_Vol` now hold provider aggressor volume only. The section
  is labelled a block-flow proxy (venue not verified).
- **Zero gamma**: computed (spot where net GEX flips sign, ±10%), else null with a reason. It no longer copies spot.
  This applies to the pipeline snapshot, and the terminal's live `/api/gex` route now computes it with the same
  function (`metrics.gex_profile()`); it used to return spot there.
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

