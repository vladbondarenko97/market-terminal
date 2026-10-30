# For quants and researchers

What the system stores, how it guarantees point-in-time data, how the models are specified and graded, and how to
get the data into your own notebooks. For a worked example, read the
[2026-10-30 case study](case-study-2026-10-30.md).

## In one paragraph

Three times each NYSE trading day, a pipeline pulls from about a dozen sources (Yahoo, FRED, EIA, CFTC, CME, CBOE,
FINRA, NY Fed, Treasury, SEC, iShares, eBay and optionally Databento). It stores every raw response, compressed and
content-addressed by SHA-256. It writes typed observations, runs the models and commits one immutable JSON snapshot
per run to SQLite. Nothing is overwritten and nothing is filled in. A value that could not be computed is stored as
`missing` with a reason.

## Point-in-time guarantees

| Guarantee | How it is enforced |
|---|---|
| Raw inputs are kept | `v2_payloads`: every provider response, zlib-compressed and deduplicated by SHA-256, with credentials removed before hashing |
| Snapshots never change | `v2_snapshots` has SQLite triggers that abort any `UPDATE` or `DELETE`. Committing a different document under the same `run_id` raises an error. |
| Knowledge time is recorded | Each row of `v2_observations` has `observed_at` (the time the value refers to) and `fetched_at` (when this system learned it). A revised value is a **new row**; the old one stays. |
| Re-runs are byte-reproducible | `main_pipeline.py replay --run <id>` re-renders a past run from its snapshot with no network |
| Definitions are versioned | Each model has a version constant (`chaos_v1`, `execution_v2`, …). A change in meaning bumps it and adds a line to [value-changes](value-changes.md). |
| Missing is not zero | A model without inputs returns `{"status": "missing", "reason": …}`. There are no fallback constants and no carried-forward prices ([status contract](forecast-lab.md#status-contract)). |
| The storage only grows | Schema changes are additive only (`CREATE … IF NOT EXISTS`, `ADD COLUMN`). Nothing is dropped. |

**Using `fetched_at`.** To reconstruct what was knowable at time *T*, keep only rows with `fetched_at <= T` and take
the newest per (`metric_id`, `entity`, `observed_at`). Leave `dims_json` out of the key: some series store revision
details there (`chaos.score` keeps `rev`, `used` and the like), so every revision would count as a separate key. Filter on `derivation`
(the model version, for example `chaos_v1`) so that two definitions are never mixed. Intraday readings of the same day stay separate.
For example, `chaos.score` for 2026-10-30 has three rows (7.54, 24.31, 30.51), one per run. The 2026-10-29 value
has five: −0.85, −4.86 and −3.22 from Thursday's own runs, then 4.12 and 5.55 from Friday's runs, which recomputed
Thursday once the once-a-day FINRA and CBOE files were in (`dims_json.rev` counts the revisions). A backtest that
uses the last value of each day sees 5.55. One that respects `fetched_at` sees −3.22 at Thursday's close.

**Known gaps.** Series that were backfilled (`scripts/backfill_*`, runs with `mode='backfill'`) have a `fetched_at` of
the backfill date, not of the original publication. Treat them as revised history, not as an as-of record. FINRA short
volume and CBOE put/call are published once a day, so an intraday run carries the previous day's value. Rows are
deduplicated on their content (`INSERT OR IGNORE` on a hash that includes the value), so a value that returns to an
earlier one (A, then B, then A again, with the same dimensions) is not stored a third time. An as-of query after the
third run still returns B.

## Getting the data out

### 1. CSV export (for notebooks)

```bash
.venv/bin/python main_pipeline.py export --metric chaos. --since 2026-01-01 --out chaos.csv
.venv/bin/python main_pipeline.py export --metric fedops. --out - | head
```

Columns: `observed_at, observed_tz, metric_id, entity, dims_json, value_num, value_text, value_json, unit, status,
reason, source, derivation, run_id, fetched_at`. `--since` and `--until` compare the date as written in `observed_at`,
with no time-zone conversion. Rows with `status=missing` are kept with an empty `value_num`. The command is read-only and does not
take the run lock, so it is safe while a run is in progress.

```python
import pandas as pd
df = pd.read_csv("chaos.csv", parse_dates=["observed_at", "fetched_at"])
asof = pd.Timestamp("2026-10-30 17:00", tz="UTC")              # what was known at 12:00 CT
pit = (df[df.fetched_at <= asof]
       .sort_values("fetched_at")
       .groupby(["metric_id", "entity", "observed_at"], dropna=False).last())   # entity is often blank
```

### 2. SQLite directly

The database is one file, `CME_Data/portfolio.db`. Open it read-only (`sqlite3 -readonly`, or
`file:…?mode=ro` with `uri=True`). Tables: [Data → SQLite: v2 lake](data.md#sqlite-v2-lake).

```python
import json, sqlite3
db = sqlite3.connect("file:CME_Data/portfolio.db?mode=ro", uri=True)
snap = json.loads(db.execute("select context_json from v2_latest_snapshot").fetchone()[0])
snap["chaos"]["score"], snap["chaos"]["regime"], snap["chaos"]["drivers"]
```

`python main_pipeline.py catalog` prints every snapshot metric with its path and lineage.

### 3. HTTP JSON API

The terminal serves the stored snapshot as JSON, for example `/api/forecast?ticker=SPY`, `/api/chaos`,
`/api/chaos/history`. Every route, and whether it reads stored data or computes live, is listed in [API](api.md).
Many routes answer HTTP 200 with `{"status": "error"}` on failure, so check the body.

### 4. MCP (LLM agents)

`mcp_server.py` exposes the same read-only sources, the docs and the model code to Claude, ChatGPT or any other MCP
client ([Operations → MCP server](operations.md#mcp-server-claude-chat-chatgpt-gemini-cli)).

## Models

The formulas, inputs and version of every computed value are in [Methodology](methodology.md). The headline models:

| Model | Method | Reference |
|---|---|---|
| Market-implied distribution | Breeden-Litzenberger density from captured option chains | Breeden & Litzenberger (1978) |
| Volatility forecast | HAR-RV, direct multi-horizon, compared with implied vol | Corsi (2009) |
| Trend / CTA | Time-series momentum, 1–12 months, with flip levels | Moskowitz, Ooi & Pedersen (2012) |
| Positioning | CFTC COT index, 3-year window | — |
| Silver fair value | Weekly regression on gold, TIPS real yield, DXY, copper, industrial production | — |
| Macro regime | NY Fed recession probit, NFCI, Sahm rule, HY spread | Estrella & Mishkin (1998); Sahm (2019) |
| Chaos / Peace | Mean of 12 stress z-scores (3-year windows), daily history back to 1930 | — |
| Dealer gamma | GEX profile, zero-gamma level, call and put walls from captured chains | — |

## How evidence is graded

Every rule that can tell the operator to act passes one gate, `core/watch.py grade()`:

- at least 8 cases (fewer than 30 is graded `thin`);
- a better result than the baseline in **both halves** of the history (sign stability);
- a t-statistic of at least 2 against the baseline;
- an up-rate at least 5 points above the baseline's.

A rule that fails reads "no measured edge" in the terminal, rather than a buy. The Chaos early-warning conditions
use the same idea on regime onsets: each condition was fixed before testing and is graded against the base rate of
"stress within N days".

**Known biases, stated.** The gate is a screen, not a proof of significance. Many rules across many tickers produce
some false passes. Neighbouring windows overlap, so the t-values are optimistic. The Chaos regime cut-offs (−30, 30,
60) are fixed and uncalibrated. Some components only have about 3 years of history. Event studies such as elections
have at most 19 cases per kind, so the best possible grade is `thin`. Every card prints its own caveat text, taken
from the code.

## Forecast scorecard

Live runs log forecasts to `v2_forecasts` (spot, `p_up`, 68% range, horizon, target date). Card 9 grades them once the
target date has passed, using hit rate, 68% coverage and the Brier score. The walk-forward backtests of cards 2, 3 and
5 sit beside them. Details: [Forecast Lab → Scorecard](forecast-lab.md#scorecard).

## Extending it

- New source: a fetcher in `core/sources.py` through `SourceSession.fetch()`, called from `core/collect.py`.
- New model or card: [Forecast Lab → Adding a card](forecast-lab.md#adding-a-forecast-lab-card).
- Tests run offline. Any network call in a test raises, and CI runs the suite a second time with the clock 400 days
  ahead.

## Data terms

The repository ships code only, with no data. CME, Databento and eBay have their own terms on redistribution, so each
installation collects its own data with its own keys.
