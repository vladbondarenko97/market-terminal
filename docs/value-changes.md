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
| `VERSION` (`core/forecast.py`) | `forecast_v1` | Forecast Lab cards 1–9 |
| `VERSION` (`core/refining.py`) | `refining_v1` | Diesel & refining and EIA inventory models (cards 10–11) |
| `SCHEMA_VERSION` (`core/lake.py`) | `v2.1` | SQLite `v2_*` schema, stored on every `v2_runs` row |
| `HORIZON_VERSION` (`core/positions.py`) | `horizons_v2` | Engine ticket +1D / +1W / +2W values. Not stored in a snapshot: positions are priced when the terminal asks, so the version is returned as `_version` inside each ticket's `horizons` dict. |

Each run also stores `code_version` (the short git commit, with `-dirty` if the tree had uncommitted changes) in
`v2_runs`, so any snapshot can be traced to the code that produced it.

## Changes

Newest first, then the changes that came with the v2 pipeline.

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

