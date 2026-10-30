# Methodology

An index of every computed value the pipeline stores, with its owner, its version constant and where its full
specification lives. This page summarises and links to the detail; it does not repeat it. If this page and the code
disagree, the code is right and this page is the bug.

All models run in the pipeline and write into the run's snapshot. Each one returns
`{"status": "missing", "reason": …}` instead of a number when its inputs are missing
([status contract](forecast-lab.md#status-contract)). When a value changes meaning, its version is bumped and the
change is logged in [value-changes](value-changes.md).

## Forecast Lab (`core/forecast.py`, `VERSION = "forecast_v2"`)

| Card | Value | Method | Detail |
|---|---|---|---|
| 1 | Market-implied range and odds | Breeden-Litzenberger: density = e^{rT} ∂²C/∂K², using a quadratic total-variance smile fitted to out-of-the-money bid/ask mids | [Cards](forecast-lab.md#cards) |
| 2 | Volatility forecast | HAR-RV (Corsi 2009), direct multi-horizon OLS. Level or log form chosen on a validation slice. Parkinson plus gap daily variance. | same |
| 3 | Trend / CTA | Time-series momentum over 1, 3, 6 and 12 months (Moskowitz, Ooi & Pedersen 2012), with the price at which each lookback flips | same |
| 4 | Positioning | CFTC COT index (3-year) by trader group, plus SLV trust ounces | same |
| 5 | Silver fair value | Weekly regression on gold, 10-year TIPS real yield, DXY, copper and industrial production. Gap measured in standard deviations, with a half-life. | same |
| 6 | Macro regime | NY Fed yield-curve recession probit, NFCI, Sahm rule (≥ 0.5), high-yield spread | same |
| 7 | Calendar and event drift | Release-day and window returns, graded by the evidence gate | [Details](forecast-lab.md#details-worth-knowing) |
| 8 | Mechanical flows | Vol-control deleveraging, leveraged-ETF rebalance, dealer gamma hedging, CTA sensitivity for moves of 1% to 5% | [Cards](forecast-lab.md#cards) |
| 9 | Scorecard | Logged forecasts graded after their target date by hit rate, 68% coverage and Brier score, plus walk-forward backtests | [Scorecard](forecast-lab.md#scorecard) |

## Market-wide models

| Value | Owner and version | Method |
|---|---|---|
| Chaos / Peace index | `core/chaos.py`, `chaos_v1` | 50 × the mean z-score of up to 12 stress components (VIX, VIX/VIX3M, VVIX, SKEW, MOVE, HY spread, SPY 20-day realised vol and drawdown, EPU, equity put/call, VIX9D/VIX6M, SPY short-sale share). Each z-score uses that component's own trailing 3 years. Limited to ±100. Regimes at −30, 30 and 60 (fixed, uncalibrated). Daily history back to 1930, plus an onset study of four early-warning conditions and an election event study. [Card 14](forecast-lab.md#details-worth-knowing) |
| Diesel and refining | `core/refining.py`, `refining_v1` | Crack spreads, EIA runs and stocks, a 10-year seasonal maintenance path, Texas refinery outage filings (TCEQ), CFTC ULSD positioning (cards 10 and 11) |
| Fed and Treasury operations | `core/fedops.py`, `fed_ops_v4` | NY Fed repo and reverse repo, SOFR, TGCR, BGCR, EFFR, Treasury buybacks and auction results (card 15) |
| VMRI macro risk | `core/metrics.py`, `vmri_v1` | (DXY × 10Y ÷ 1.61) × (HY OAS ÷ 4) × (VIX ÷ 20), in four tiers. A heuristic composite kept for continuity, not a fitted model. |

## Options and flow (`core/metrics.py`)

| Value | Version | Method |
|---|---|---|
| Dealer gamma (GEX) profile and walls | `gex_v2` | Net GEX by strike over the first N expirations from the captured chains. One parameter set for every ticker. |
| Zero-gamma level | `zero_gamma_v1` | The spot price at which aggregate net GEX changes sign, with IV held fixed |
| Max pain | `max_pain_v2` | Contracts with unknown open interest are excluded and the coverage is recorded |
| Block flow | `block_flow_v2` | Databento prints above a size threshold. The feed's own trade sides (A = sell aggressor, B = buy aggressor) are kept apart from inferred ones. |
| OI divergence | `oi_divergence_v2` | Institutional against retail open-interest trends |
| Execution engine | `execution_v2` | A legacy confluence scorer (VIX regime, breadth, flow, gamma, max pain) that outputs a ticket or CASH. A vote whose inputs are missing is recorded as missing, not as zero. It does **not** pass through the evidence gate. Its tickets are marked at +1d, +1w and +2w (`core/positions.py`, `horizons_v2`). |

## Silver

| Value | Version | Method |
|---|---|---|
| Shanghai–COMEX spread | `shanghai_dated_v1` | Each SGE benchmark fix paired with the SI=F close and the USD/CNY close **of the same date**, shown before and after the 13% VAT. Nothing is carried forward. |
| Silver Eagle premium | `eagle_premium_v1` | Retail price over futures from the same run, with 90-day statistics |
| Paper:physical ratio | `paper_physical_history_v1` | COMEX open interest in ounces ÷ registered ounces, against its own recorded range |

## Evidence gate (rule cards)

`core/watch.py grade()` is the only gate for rules that can say "act". It requires n ≥ 8 (`thin` below 30), a better
result than the baseline in both halves of the history, t ≥ 2, and an up-rate at least 5 points above the baseline.
A rule that fails says "no measured edge". [For quants → How evidence is graded](for-quants.md#how-evidence-is-graded).

## Lineage

`python main_pipeline.py catalog [--run RUN_ID]` prints each snapshot metric with its source, its derivation and its
path in the snapshot.
