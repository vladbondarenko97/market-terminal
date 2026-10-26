# Forecast Lab

Reference for the twelve Forecast Lab model cards (1 to 11 and 14), the status values a model may return, and a tutorial for adding a card.
It is for contributors, human or AI, and for anyone who wants to know what a card means and where its data comes
from. How the card looks in the browser is in [Terminal](terminal.md#forecast-lab-tab).

> Not financial advice. Every output is a probability or an estimate, and each card's `?` help states its caveats.

## How it works

- The models run **in the pipeline**, once per run. `build_forecast()` in `core/collect.py` fetches the extra inputs
  and calls `build()` in `core/forecast.py` (cards 1 to 9). `build_refining()` in `core/collect.py` builds cards 10
  and 11 with the pure functions in `core/refining.py`. `build_chaos()` builds card 14 with `core/chaos.py`. `_guarded_fed_ops()` builds card 15 with `core/fedops.py`. Failures are isolated: if a card or a source fails, the run
  continues (see [Architecture: design rules](architecture.md#design-rules)).
- The results are stored in the run's snapshot under `forecast`, `refining`, `chaos` and `fed_ops` (see [Data](data.md)).
- `GET /api/forecast?ticker=SPY|SLV` ([API](api.md)) returns the latest snapshot's forecast, refining and chaos blocks plus a
  scorecard computed at request time. The server keeps the snapshot in memory for 60 seconds.
- The browser (`options_whale/static/forecast.js`) draws the cards. It never computes a model.
- A card appears only for runs made after it was added. Older snapshots do not hold the new key, and the card reads
  "Unavailable".
- If the request fails (no snapshot yet, or the forecast block is in the `error` state) all model cards show
  `Forecast Lab unavailable: <message>`.
- Model versions are `VERSION` in `core/forecast.py` (cards 1 to 9) and in `core/refining.py` (cards 10 and 11). See
  [Value changes](value-changes.md).

## Cards

| # | Card | Method | Answers | Scope | Snapshot key |
|---|---|---|---|---|---|
| 1 | Market-implied range | Breeden-Litzenberger density from the option chains the run captured (quadratic smile on out-of-the-money bid/ask mids) | What range and odds are options pricing at 1D, 1W, 1M and 1Y? | per ticker | `forecast[SYM].implied` |
| 2 | Volatility forecast | HAR-RV (Corsi 2009), direct multi-horizon; level or log form chosen on a validation slice; compared with implied vol | Are options cheap or rich against forecast volatility? | per ticker | `forecast[SYM].vol_forecast` |
| 3 | Trend / CTA model | Time-series momentum over 1, 3, 6 and 12 months, with flip levels | Which way are trend followers positioned, and at what price do they flip? | per ticker | `forecast[SYM].trend` |
| 4 | Positioning | CFTC Commitments of Traders (3-year COT index) and SLV trust ounces | Is any trader group at an extreme? | shared; the card shows the silver groups for SLV and the S&P 500 groups for SPY | `forecast.positioning` |
| 5 | Silver fair value | Weekly regression of silver on gold, 10-year TIPS real yield, DXY, copper and industrial production | Is silver above or below its model value, and how fast do gaps close? | shared, silver only (the SPY view shows a notice) | `forecast.silver_fair_value` |
| 6 | Macro regime | NY Fed recession probit, NFCI, Sahm rule, high-yield spread | Expansion or stress? | shared | `forecast.macro_regime` |
| 7 | Calendar & event drift | FOMC, CPI, payrolls, PPI, GDP, PCE, retail sales, FOMC-minutes and Fed Board speech days, OPEX, turn-of-month and weekday effects, with n, t-statistics and (release days) the Day Scanner's evidence `grade` | Does the calendar tilt the odds this week? | per ticker | `forecast[SYM].calendar` |

Card 7 release-day rows (`effects.payrolls_day`, `ppi_day`, `gdp_day`, `pce_day`, `retail_sales_day`, `fomc_minutes_day`) use the
release dates of the FRED release calendars (and decision + 21 days for the minutes, a rule). Each is the release-day close-to-close
return with `n`, `mean_pct`, `hit_rate`, `t_vs_baseline` and `grade`: `core.watch.grade()` of that day's returns against all other days
(`tested`, `thin` under 30 cases, `none`; under 8 cases it is `none`). A release without dates is `{"status": "missing", "reason"}`.
`effects.fed_speaker_day` is built the same way from `src.fed_speeches()` (the Fed's one JSON list of speeches since 2006, reused for 7 days): the
days with at least one speech by the Chair, a Vice Chair or a Governor (regional bank presidents are not counted), close-to-close, graded the same way.
It is `missing` with a reason when the list is unavailable.
| 8 | Mechanical flows | Vol-control funds, leveraged-ETF rebalancing, dealer gamma, CTA sensitivity | Who is forced to buy or sell after a move of 1% to 5%? | per ticker | `forecast[SYM].flows` |
| 9 | Forecast scorecard | Every logged forecast graded after its target date, plus walk-forward backtests | Were past forecasts right? Hit rate, 68% coverage, Brier score | per ticker | not in the snapshot: graded per request from `v2_forecasts` |
| 10 | Diesel & refining | Crack spreads, EIA refinery runs and stocks, 10-year maintenance-season path, Texas refinery outage filings (TCEQ), CFTC ULSD positioning | Is refining tight, and is a major unit down? | shared | `ctx["refining"]` (`margins`, `fundamentals`, `maintenance`, `outages`, `ulsd_positioning`) |
| 11 | EIA inventories | Weekly US petroleum stocks against the 5-year same-week average, and days of supply | Are stocks building or drawing against the norm, and how does today compare with past years? | shared | `ctx["refining"]["inventories"]`, full history from `/api/eia_history` |
| 14 | Chaos / Peace | Twelve stress readings (VIX, VIX/VIX3M, VVIX, SKEW, MOVE, high-yield spread, SPY realised vol and drawdown, policy uncertainty, equity put/call, VIX9D/VIX6M, SPY short-sale share), each a z-score against its own 3 years | How stressed does the market look against its own recent history and against every stress episode since 1931, how often did a calm market turn, and what do the liquidity levers and past election windows look like? | shared | `ctx["chaos"]` (`score`, `regime`, `components`, `levers`, `event_study`, `lever_study`, `long`), also `GET /api/chaos` and `GET /api/chaos/history` |
| 15 | Fed / Treasury operations | NY Fed repo (Standing Repo Facility, term) and reverse repo results, SOFR, TGCR, BGCR, EFFR (NY Fed); Treasury buyback operations (Fiscal Data); Treasury auction results and calendar (TreasuryDirect) | How much spare cash sits in the reverse repo, is anyone using the Fed's repo backstop, where are funding rates, how much is Treasury buying back against what it offered and how much dealers offered, and how are auctions going against a year ago? | shared | `ctx["fed_ops"]` (`repo`, `buybacks`, `auctions`), also `GET /api/fed_ops*` and `GET /api/fed_ops/history/*` |

`SYM` is `SPY` or `SLV`. Cards 1 to 3, 7 and 8 sit under `forecast[SYM]`; the browser receives that object as `data`.

### Details worth knowing

- **Card 1.** Horizons not listed as an expiry are rescaled from the nearest listed one and marked `*`. The density
  uses bid/ask mids, never last prices. Spot and zero gamma are drawn on the chart.
- **Card 3.** The conditional historical odds are withheld when the history holds fewer than 10 independent windows.
- **Card 4.** Silver groups: managed money, producer/merchant, swap dealers. S&P 500 groups: leveraged funds, asset
  managers, dealers. The SLV trust history builds up from each run's reading.
- **Card 5.** The model needs about 60 aligned weeks of data. The backtest line is shown with its sample size.
- **Card 8.** Vol-control flows are computed for SPY only, and use an assumed AUM of $400 billion (an industry estimate).
  Leveraged ETFs: SSO, UPRO, SPXL, SH, SDS, SPXU, SPXS for SPY; AGQ and ZSL for SLV. The dealer-gamma block appears when the run
  has a net GEX figure.
- **Card 10.**
  - Outages come from the Texas TCEQ emission-event feed, matched to EIA refinery capacities and classified planned or
    unplanned by unit. The feed keeps about 5 days, so every run stores new events and the history accumulates.
    **Louisiana and other states are not covered.**
  - An outage is **MAJOR** when it is unplanned and either at a refinery of 250,000 barrels per day or more on a key
    unit, or lasts 24 hours or more on an important unit.
  - Delivered runs send one push (ntfy) for each newly seen MAJOR outage (`_refinery_alerts()` in `main_pipeline.py`).
  - EIA files are reused until the next weekly release (Wednesday 10:30 ET): `_cached_eia()` and `last_eia_release()` in
    `core/sources.py`.
- **Card 15.** `core/fedops.py`, version `fed_ops_v2`, built by `_guarded_fed_ops()` in `core/collect.py` after card 14 (the
  Treasury requests are shared). Each run refetches the last 21 days of NY Fed operations and rates, buybacks since 56 days ago
  and auctions of the last 60 days into the `fedops.*` observations, then builds the block from everything stored
  (`scripts/backfill_fed_ops.py` loads the years before). Nothing is modelled: daily take-up is the sum of the day's
  operations, summaries are sums and means by month, quarter, year and trailing 12 months. A part whose fetch failed keeps
  its stored data with status `stale` and the date it is as of. Auction tails are `missing`: no free official source
  publishes the when-issued yield. Bill tenors use the auction's own term, coupon tenors the original term (a reopened
  20-year bond is a 20-year).
- **Card 14.** `core/chaos.py`, version `chaos_v1`, built by `build_chaos()` in `core/collect.py`.
  - Score = 50 x the mean z-score of the available components, limited to -100 (peace) .. +100 (chaos). Each z-score is
    the latest value against a trailing 3-year window (1,096 calendar days, at least 252
    observations), clipped to +-3, oriented so that higher is more stress. Regimes: score <= -30 peace, up to 30
    neutral, up to 60 stress, above 60 chaos. The cut-offs are fixed and uncalibrated (`REGIMES` in `core/chaos.py`).
  - A component is used only when its series spans 3 years less 30 days and its last observation is within 10
    calendar days of the run; otherwise it is `missing` with `history shorter than 3y (N observations since DATE)` or
    `stale: last observation DATE`. The block always says `components_used` of 12 and lists `components_missing`. With no
    usable component the whole block is `missing`.
  - Equity put/call (CBOE) and SPY short-sale share (FINRA) are published as one file per day, so each run stores the last 5 trading days it has not stored yet (observations `chaos.equity_put_call`
    and `chaos.short_volume_spy`). Until 3 years have accumulated, or `scripts/backfill_chaos_history.py` loads them (see [Backfills](operations.md#backfills)), both are `missing` with the
    `history shorter than 3y` reason. Policy uncertainty (FRED `USEPUINDXD`) is published 7 days a week and is noisy, so
    it enters as a 7-day mean.
  - `prev` is the previous trading day's score computed from the same histories; `regime_changed` is true when its regime
    differs. Signal Watch has one row ("Chaos/Peace regime") that fires on that. The row describes the reading and
    recommends no trade. `history` holds the last 60 trading days as `[date, score, components_used]`.
  - `election` counts trading days to the next US general election (`kind` is `midterm` or `presidential`; negative after
    it) and `tracking` is true until 60 trading days after it; then it moves to the next one. The event study has rows for
    both kinds, for the S&P 500 (since 1950), the VIX (since 1990) and the score itself (in points, since it has 4 components).
  - **Long history** (`long`, and the observations `chaos.score`). The same formula is evaluated on every past day: a
    component counts on a day once its series is 3 years old and while its last observation is at most 10 days old. The
    S&P 500 closes stand in for SPY before 1993 (scaled to join at SPY's first day), so the history starts at the end of
    1930 with one component, has two from 1931, and gains the others as their series begin (`long.eras`). Before the
    early 1990s the score therefore rests on S&P 500 realised volatility and drawdown and reaches the limits easily; the
    card draws those years fainter. Each run stores the days it has not stored yet. Only the last 10 trading days can be
    revised (a component that arrives late becomes a new row; the newest row per date is the one served); older days are
    frozen, so a failed fetch or a sliding 3-year input cannot rewrite the past. For the same reason the first write waits
    for a run in which every full price history was fetched (`source_errors` says so until then). The three inputs that only ever exist
    as a 3-year fetch (high-yield spread, equity put/call, SPY short-sale share) therefore join the stored history from
    about two months before its first run and stay in it from then on. Episodes, eras and the regime study are computed
    from the stored series, the same one the chart draws.
    The history's newest point is computed from the full series and the card's score from the run's 3-year fetches, so
    the two can differ by a tenth of a point.
  - **Episodes and the regime study** (`long.episodes`, `long.study`). A stress episode starts on the third trading day
    in a row above 30 and ends on the fifth in a row below 15. These four numbers are fixed and not fitted. The study
    uses only the days with 4 or more components. It gives the current state, the median length of an episode and of the
    calm between two, how often an episode began within 20, 60 and 120 trading days of a calm day (all, and by the day's
    regime), and four conditions fixed in advance (a 15-point rise in 20 days, a score between 15 and 30, a third of
    the components at z >= 1, VIX at or above VIX3M), each tested on its onsets with the evidence test of the rule cards
    (`core.watch.grade`). It describes one history: neighbouring days overlap, two of the conditions are partly
    arithmetic, and a 50% hit rate is also a 50% false-alarm rate.
  - **Chart.** Range buttons (6M, 1Y, 5Y, 20Y, MAX), mouse-wheel zoom around the cursor, drag to pan, pinch on a touch
    screen, double-click to return. Stress episodes, election days (P / M) and NBER recessions (FRED `USREC`) can be
    switched on and off; clicking an episode in the table zooms to it. Until a run has stored the history the card shows
    the 60-day line as before.
  - `levers` (net liquidity, bank reserves, Strategic Petroleum Reserve, Treasury bills and coupons offered and
    buybacks accepted over the last 28 days) sit beside the score and are not part of it.
  - `event_study` compares S&P 500 (1950 on) and VIX (1990 on) returns, and the change of the score itself in points
    (since it has 4 components), in four windows around past election days with all other windows of the same length,
    through `core.watch.grade`. Each row carries `kind` (`midterm` or `presidential`) and `unit` (`pct` or `pts`). About
    19 elections of each kind (9 for the VIX) means the best grade is `thin`. `lever_study` compares the 60 days before November elections in even and odd years, with the
    median beside the mean because one extreme year (a percent change of a small balance) can dominate a mean. Both are
    descriptive and say nothing about why anything moved.
  - The full daily histories (^GSPC, SPY and the seven volatility indices from Yahoo, `USEPUINDXD` and `USREC` from FRED) are
    reused for 30 days (`yf_long_history()` and `fred_long_series()` in `core/sources.py`); the run's own 3-year fetches
    extend them to today. If one of them cannot be fetched, the component still counts on recent days from the 3-year
    series and the error is listed in `source_errors`.
- **Card 11.**
  - Eight lines: crude (commercial), distillate/diesel, gasoline, jet fuel, Strategic Petroleum Reserve, Cushing OK,
    propane/propylene and **residual fuel oil**.
  - The snapshot holds the last 26 weeks. The card then loads the full weekly history from `/api/eia_history`: stocks
    from 1982, demand and days of supply from 1991. Until that loads (or if it fails) the card shows the 26 weeks.
  - **Days of supply** exist only for crude (stocks divided by the 4-week average of refinery crude runs), distillate,
    gasoline and jet (stocks divided by the 4-week average of product supplied). SPR, Cushing, propane and residual fuel
    have none.
  - The 5-year norm is the average of the same ISO week (plus or minus one) over the previous five years, and needs at
    least three of them; earlier weeks show no value.
  - Controls: four views (vs 5y avg, Indexed, Levels, Days of supply), range buttons 1M to MAX, a Year menu, two date
    boxes, and a legend click to hide a line.

### Card buttons

| Button | Does |
|---|---|
| **?** | Opens a help dialog from `FC_FAQ` in `forecast.js`: what the card shows, how to read it, how to trade with it, how hedge funds and quant desks use it, and caveats. Esc or a click outside closes it. |
| **⛶** | Full screen. Esc, the ⛶ button or a click on the dark backdrop returns. |
| **COPY** | Copies `{ticker, run, card, data}` as JSON, where `card` is the title and `data` is the card's part of the response (`copyForecastCard()`). It also shows the JSON in the terminal's Copy dialog. |

## Status contract

Every model returns a dict with a `status` key. The browser treats anything other than `"fresh"` as "no data".

| Level | Values | Meaning |
|---|---|---|
| A model or block (cards 1 to 9, 11 and 14, and blocks inside them) | `"fresh"`, `"missing"` | `fresh` carries the numbers and a `model` string. `missing` carries a `reason` and nothing else to rely on. |
| Inside a model | `"fresh"`, `"missing"` per item | For example `implied.horizons[k]` (one horizon can fail while the others work), and `positioning.silver` / `positioning.sp500`. The top `positioning` status is `fresh` while any of silver, S&P 500 or the SLV trust reading has data, and `missing` (with a `reason` joining the three) when none does; the two groups stay in the block either way so the card can print each reason. |
| The wrapper blocks `forecast`, `refining` and `chaos` | `"fresh"`, `"partial"`, `"error"` | `partial`: the model code ran but some inputs could not be fetched; `source_errors` names them. `error`: the whole build raised; the block holds `reason` and a redacted `trace` and nothing else. |
| Parts of the refining block | `"fresh"`, `"missing"`, and `"partial"` for `fundamentals` | `partial` means some EIA series were missing; `reason` lists them. |

**Absent inputs never raise.** `build()` is written to run on whatever the lake has: with no price history at all
(an offline run on an empty lake), no option chains or no FRED series, every card comes out `missing` with a
`reason` (`need >= 300 daily bars`, `no option chains or spot`, `T10Y3M unavailable` ...) and the wrapper is
`partial` with the failed sources in `source_errors`, not `error`. The same holds for the refining functions: a
series that is absent, empty or too short gives `missing` (or `partial` for `fundamentals`), never an exception.
`tests/test_core_fixes.py` covers both.

Rules for authors:

- Return `{"status": "fresh", "model": "...", ...}` on success and `{"status": "missing", "reason": "..."}` otherwise.
  Never return 0 or a placeholder for an unknown value. Pass numbers through `_f()` so NaN and infinity become `null`.
- `"stale"` is not a model status. Forecast Lab models carry the real date of their data instead (`as_of`,
  `latest_date`, `last_price_date`), and the card prints it.
- `source_errors` is a dict from input name to a credential-redacted message. Keys look like `fred:DFII10`,
  `cftc:silver`, `ishares`, `federalreserve`, `aum:SSO`. The refining block has its own (`eia:<series>`, `yahoo:<symbol>`,
  `cftc:ulsd`, `tceq:feed`). The terminal shows the forecast block's count in the status line.
- Renderers follow `if (!x || x.status !== 'fresh') { el.innerHTML = missing(x); return; }`. `missing()` prints
  `Unavailable: <reason>`, or plain `Unavailable` when the key is absent.
- `/api/forecast` returns HTTP 404 with `status: "error"` when there is no snapshot or the forecast wrapper is `error`,
  and HTTP 503 when the database has not been initialised (no pipeline run yet). Cards 10, 11 and 14 are then unavailable
  too, because the whole response is an error.

Cards 4, 9 and 10 treat a block whose status is `error` or `missing` as unavailable and print its `reason`
(`fcFailed()` in `forecast.js`); that is why `positioning` carries a top-level `reason` when it has nothing.

## Scorecard

Card 9 grades the forecasts the pipeline logged.

- **Logging.** Live runs only (offline runs and replays log nothing). `forecast_rows()` in `core/forecast.py` builds
  the rows and `record_forecasts()` writes them to `v2_forecasts`, one row per run, symbol, model and horizon. Logged
  models: `implied` (all horizons), `har_vol` (all horizons), `trend` (when LONG or SHORT), `calendar` (1W, when an event
  is within 7 days), `positioning` (1M, when the COT index is 90 or above, or 10 or below), `fair_value` (SLV, 1M and 1Y,
  when the gap is 1 standard deviation or more).
- **Grading.** `scorecard()` in `core/forecast.py` runs on every `/api/forecast` request, on a read-only connection
  (`lake.connect_readonly()`): it runs no DDL, and raises `sqlite3.OperationalError` if `v2_forecasts` does not exist yet
  (`migrate()` creates it). For each row whose target
  date has passed it takes the first daily close on or after that date, using live yfinance closes (cached for 60 s), not
  stored data. Hit rate is the share of correct directions, coverage is the share of closes inside the 68% range
  (ideal 0.68), and Brier is the squared error of the stated probability (lower is better; 0.25 is a coin flip).
- **Card.** Four counters (Logged forecasts, Awaiting grade, Graded groups, Prices through), a LIVE GRADES table, and
  WALK-FORWARD BACKTESTS taken from cards 2, 3 and (for SLV) 5.

## Adding a Forecast Lab card

A card is one Python function, one HTML block and one JavaScript renderer, joined by a key in the snapshot. The example
adds card 12, "RSI", for SPY and SLV. It follows the per-ticker path. For a card that is the same for both tickers and
has its own data sources, see [Market-wide cards built like 10 and 11](#market-wide-cards-built-like-10-and-11).

### 1. Compute it: `core/forecast.py`

Write a pure function: data in, dict out, no network and no database. `_closes(df)` returns the `Close` column without
NaN (an empty series when `df` is `None`), and `_f(x)` turns a number into a float, or `None` for NaN, infinity or
junk.

```python
def rsi_model(df, period=14):
    c = _closes(df)
    if len(c) < 5 * period:
        return {"status": "missing", "reason": f"need >= {5 * period} daily closes"}
    d = c.diff().dropna()
    gain = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    loss = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    rsi = (100.0 if gain > 0 else 50.0) if loss == 0 else 100 - 100 / (1 + gain / loss)
    return {"status": "fresh", "model": f"Wilder RSI({period}), EWM alpha=1/{period}, daily closes", "rsi": _f(rsi),
            "read": "overbought" if rsi > 70 else "oversold" if rsi < 30 else "neutral"}
```

Rules: return `status: "fresh"` on success and `status: "missing"` with a `reason` otherwise (see
[Status contract](#status-contract)); pass numbers through `_f()`; include a `model` string that says how the value was
computed and keep it true. The smoothing above is Wilder's (a recursive average with weight 1/period), so it is
called Wilder RSI. It starts from the first change instead of a simple average; after the required 5 times `period`
closes that start carries under 1% of the weight. A plain rolling mean would be Cutler's RSI.

The daily price frames come from `HISTORY_SYMBOLS` in `core/collect.py`: SPY, SLV, SI_F, GC_F, HG_F, DXY (5 years) and
VIX, TNX (1 year). A frame has the Yahoo columns (`Open`, `High`, `Low`, `Close`, `Volume`).

### 2. Add it to the snapshot: `build()` in `core/forecast.py`

For a per-ticker card, add the key inside the loop:

```python
out[sym] = {"spot": spot, "implied": imp, "vol_forecast": vol, "trend": tr, "calendar": cal, "flows": flows,
            "rsi": rsi_model(fr.get(key))}
```

(The line already exists; add the last entry.) The server returns `out[ticker]` as `data`, so a per-ticker key reaches
the browser with no server change.

A card that is the same for both tickers goes on `out` itself, as `positioning`, `silver_fair_value` and
`macro_regime` do. It must also be added to the `jsonify(...)` call in `api_forecast()` in
`options_whale/api_router.py`, and the browser reads it as `j.<key>` (not `j.data.<key>`).

### 3. Fetch new inputs, if any: `build_forecast()` in `core/collect.py`

Add the fetch there, record failures in the `errors` dict (key `provider:name`), and pass the result to the model
through the `inputs` dict that `build_forecast()` builds. Skip this step if the card uses data the run already has.

- A new FRED series: add it to `FRED_FORECAST` (series id and number of observations). It arrives as
  `inputs["fred"][series_id]`, a list of `{"date", "value"}` observations, newest first (see `macro_regime()`). A failed
  fetch is recorded as `fred:<id>` automatically.
- Anything else: add a fetcher in `core/sources.py` (bounded retries, stored through the shared session so the raw
  response is kept), call it in `build_forecast()`, and put the result in `inputs`. Then read it in `build()`.

### 4. Add the markup: `options_whale/templates/terminal.html`

Copy an existing card block inside `#forecastGrid` and change the number. Titles are upper case. The `--fc-delay` value is
`-0.66s` times the card's position minus one, which staggers the rotating border: card 12 gets `-7.26s`.

```html
<div class="rainbow-card" style="--fc-delay:-7.26s">
    <div class="rainbow-inner">
        <div class="fc-head">
            <div><div class="fc-title">12 · RSI</div><div class="fc-sub">Wilder RSI(14) · daily closes</div></div>
            <div class="fc-actions">
                <button class="fc-icon" onclick="openForecastFaq('fc12')" title="How to use this card">?</button>
                <button class="fc-icon" onclick="toggleForecastFullscreen('fc12')" title="Full screen (Esc to exit)">⛶</button>
                <button class="fc-copy" onclick="copyForecastCard('fc12')" title="Copy this card's data">COPY</button>
            </div>
        </div>
        <div id="fc12" class="fc-body"><div class="fc-muted">Loading…</div></div>
    </div>
</div>
```

The grid has one column on small screens, two from `lg` and three from `2xl`. Cards 10 and 11 add `lg:col-span-2` to the
first line (`class="rainbow-card lg:col-span-2"`) to span two columns; do the same for a wide card.

### 5. Render it: `options_whale/static/forecast.js`

Write the renderer, then make four more edits.

```js
function renderRsi(j) {
    const r = j.data?.rsi, el = document.getElementById('fc12');
    if (!r || r.status !== 'fresh') { el.innerHTML = missing(r); return; }
    el.innerHTML = `<div class="fc-kpis">${kpi('RSI (14)', fcNum(r.rsi, 1), fcEsc(r.read))}</div>
                    <div class="fc-muted">${fcEsc(r.model)}</div>`;
}
```

1. add `renderRsi` to the renderer array in `loadForecast()` (each renderer runs in its own `try`, so one failure does not
   stop the others)
2. change `i <= 11` to `i <= 12` in the error loop of `loadForecast()`
3. add `fc12: d.data?.rsi` to the `map` in `copyForecastCard()` (use `d.<key>` for a shared card)
4. add an `fc12` entry to `FC_FAQ` with `title`, `what`, `read[]`, `trade[]`, `funds[]` and `caveats[]`. Without it the `?`
   button does nothing.

Use the helpers already in the file:

| Helper | Does |
|---|---|
| `kpi(label, value, sub = '', cls = '')` | One KPI tile. `value` and `sub` are inserted as HTML. |
| `missing(section)` | The "Unavailable: reason" line. |
| `fcNum(v, d = 2)` | Number with `d` decimals, or an em dash for null/NaN. |
| `fcPct(v, d = 1, sign = true)` | A value already in percent, as text such as `+1.2%`. |
| `fcPctF(f, d = 1, sign = false)` | A fraction (0.25) as a percent. |
| `fcCls(v)` | `fc-pos`, `fc-neg` or empty, by sign. |
| `fcBig(v)` | Signed dollars as K, M or B. |
| `fcEsc(s)` | HTML-escapes a string. |
| `fcChart(id, cfg)` | Draws a Chart.js chart on the `<canvas id="...">` that your HTML contains, replacing the previous one. |

Escape every string that comes from data with `fcEsc`. `kpi()` does not escape `value` or `sub`.

### 6. Test it: `tests/test_core_fixes.py` or `tests/test_v2.py`

Feed the function a synthetic series with a known answer, and check the `missing` path. Inline synthetic data is fine
(`T06ForecastLab` in `tests/test_v2.py` and `ForecastModels` in `tests/test_core_fixes.py` do this, with seeded random
walks and a Black-Scholes option chain); tests must not touch the network or need keys. Also check that
`F.build()` still returns `missing` for the new card when its inputs are absent (`ForecastBuildOnMissingInputs`).

```python
def test_rsi_all_gains_is_100_and_short_history_is_missing(self):
    import numpy as np
    import pandas as pd
    from core import forecast as F
    up = F.rsi_model(pd.DataFrame({"Close": np.arange(1.0, 101.0)}))
    self.assertEqual((up["status"], up["rsi"], up["read"]), ("fresh", 100.0, "overbought"))
    flat = F.rsi_model(pd.DataFrame({"Close": np.full(100, 50.0)}))
    self.assertEqual(flat["rsi"], 50.0)
    self.assertEqual(F.rsi_model(pd.DataFrame({"Close": [1.0, 2.0]}))["status"], "missing")
    self.assertEqual(F.rsi_model(None)["status"], "missing")
```

### 7. Check it

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python main_pipeline.py run --no-deliver   # live run, new snapshot, nothing sent
# restart the server and reload the browser, then open the Forecast Lab tab
```

Use a live run: models run when the data is collected, so `replay` (which re-renders a stored snapshot) and
`--offline` (which makes no provider calls and records no forecasts) do not add the new key. A card appears only for runs
made after the change.

### Optional extras

- **XML report.** `forecast_xml()` in `core/render.py` writes the tactical XML `<forecast_lab>` block. It covers cards
  1 to 8 (per-ticker implied, volatility, trend, calendar and flows; positioning; silver fair value; macro regime). The
  scorecard has no XML. Cards 10 and 11 are written by `refining_xml()`.
- **Gradable forecast.** Add a row to `forecast_rows()` in `core/forecast.py` (not in `core/render.py`) so the scorecard
  tracks it. The tuple order matches the `v2_forecasts` columns: run id, created, symbol, model, horizon, target date,
  spot, p_up, lower68, upper68, direction, vol. For example, inside the per-symbol loop:

  ```python
  rsi = s.get("rsi") or {}
  if rsi.get("status") == "fresh" and rsi["read"] != "neutral":
      rows.append((run["run_id"], created, sym, "rsi", "1w", (day0 + timedelta(days=7)).isoformat(), spot,
                   None, None, None, "DOWN" if rsi["read"] == "overbought" else "UP", None))
  ```

  The pair (run, symbol, model, horizon) must be unique. Card 9 lists the new model by name once rows are graded.

### Changing an existing model

Adding a card does not change any stored value. Changing how an existing model computes a value does. Then bump
`VERSION` in `core/forecast.py` (cards 1 to 9) or `core/refining.py` (cards 10 and 11), and add a line to
[Value changes](value-changes.md). Never edit stored snapshots: old runs keep the old version and the old value.

### Market-wide cards built like 10 and 11

A card that is not tied to SPY or SLV, has its own sources, and should never stop the run follows the refining path
instead:

1. Put the pure functions in `core/refining.py` (or a new module), returning `fresh` or `missing` dicts as above.
2. Fetch and assemble in `build_refining()` in `core/collect.py`. The simplest route is to add your block to the dict it
   returns, as `inventories` is. Record failures in its `errors` dict.
3. `_guarded_refining()` wraps `build_refining()` so an exception becomes `{"status": "error", ...}` instead of aborting
   the run. A separate new top-level block needs the same guard, and a line next to `"refining": refining_ctx` in the
   context that `collect.py` returns.
4. The server already passes the whole block to the browser: `_latest_forecast()` in `options_whale/api_router.py`
   attaches it as `_refining`, and `api_forecast()` returns it as `refining`. A block inside `build_refining()`'s dict
   therefore needs no server change; the browser reads `j.refining.<key>`. A new top-level block needs a new entry in
   both places.
5. Write the XML in `refining_xml()` (`core/render.py`), the renderer in `forecast.js` (steps 4 and 5 above, with
   `copyForecastCard()` mapping to `d.refining.<key>`), and bump `VERSION` in `core/refining.py` when you change an
   existing value.

## Rule cards (0, 12, 13)

Three cards on this tab are rules, not models, and are not part of the snapshot: **0 · Signal Watch** reads the
latest snapshot's forecast (plus live quotes for price-driven rows), **12 · Day Scanner** and **13 · Edge Lab** read
Yahoo price history and live quotes directly. They are described in
[terminal.md](terminal.md#rule-cards-and-alerts). Unlike the model cards they compute at request time; see
[known-issues.md](known-issues.md).
