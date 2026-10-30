# Case study: the stress flip of 2026-10-30, as recorded

On Friday 2026-10-30 the Chaos / Peace index (Forecast Lab card 14) crossed from **neutral** into **stress** during
the session. The phone alert arrived about ten minutes before the close. This page sets out what the system stored,
run by run. It shows what was measured, what had a historical record before the day, and what the record cannot show.

Every number below comes from a stored snapshot (`v2_snapshots.context_json`) or the observation table
(`v2_observations`). Run IDs are given so that anyone with the same lake can check them. Times are US Central.

> **The short version.** This was a same-day measurement, not a forecast. The index reads stress from options,
> credit and flow data while the stress is happening. What *did* come before the flip: one of the index's early-warning
> conditions switched on at the 11:30 run, about three hours before the regime changed. That one case proves
> nothing. Its record over 1992–2026 is in the table further down.

## The day in four snapshots

| Run (CT) | Run ID | Score | Regime | Largest positive z-scores |
|---|---|--:|---|---|
| Thu 14:45 | `20261029T194506Z-7caee8` | −3.2 | neutral | SPY short volume 0.71, SKEW 0.19, HY spread 0.18 |
| Fri 08:31 | `20261030T133104Z-8c3bc3` | 7.5 | neutral | SPY short volume 1.26, SKEW 0.43, VIX 0.32 |
| Fri 11:30 | `20261030T163004Z-8e51cf` | 24.3 | neutral | SPY short volume 1.26, VVIX 1.09, VIX/VIX3M 0.97 |
| Fri 14:45 | `20261030T194508Z-51aee5` | **30.5** | **stress** | VVIX 1.43, SPY short volume 1.26, VIX/VIX3M 1.14 |

All 12 components counted as fresh in every run, where fresh means the last observation is within 10 days. At 14:45 five of them still carried 2026-10-29 values: EPU, equity put/call, the HY spread, SKEW and SPY short volume. At 14:45 the readings were: VIX 21.04, VIX/VIX3M 0.967,
VVIX 116.5, MOVE 100.1, SKEW 151.0, high-yield spread 3.26%, SPY drawdown 3.5% and 20-day realised volatility 12.7%.
The SPY short-sale share (FINRA) and the equity put/call ratio (CBOE) are published once a day, so they lag by a day;
the other three are daily series that had not yet posted a 2026-10-30 value.

The same 14:45 snapshot recorded SPY at 751.43 against a previous close of 769.36 (−2.3%), VIX at 21.05 against
17.20, gold futures −3.6% and WTI +2.4%.

**How the score is built.** Each component becomes a z-score against its own trailing three years. The score is
50 × the mean z, limited to −100 … +100. The regime cut-offs are −30, 30 and 60. They were fixed in code on 2026-10-10 and have not been tuned since, and
they are not calibrated. Full specification: [Forecast Lab, card 14](forecast-lab.md#details-worth-knowing). Code:
`core/chaos.py`, version `chaos_v1`.

A score of 30.5 is just over the line. It is not a crash reading. The highest score of the previous 60 days was 31.4
(2026-10-16), and the long-history study counts 48 stress episodes since 1992 with a median length of 27 trading days (an episode runs from the 3rd day above 30 to the 5th day below 15, so a
length counts from the third day, not the first).
Note also that the "previous day" figure in the alert (5.5) is Thursday recomputed inside Friday's run from
Thursday's closing values and the once-a-day files published overnight. That is why it differs from the −3.2 that
Thursday's 14:45 run saw at the time. For the same reason `regime_changed` compares today with yesterday's score as
recomputed in today's run (`core/chaos.py`), not with what yesterday's run showed. In theory that can repeat a change
or hide one. It is recorded in [known-issues](known-issues.md).

## What fired, and when

**11:30, a warning with a record switches on.** The index carries four early-warning conditions. The conditions, their
bounds and the study all landed in one commit on 2026-10-10 and have not been tuned since. Each is graded on its *onsets* over the daily history (1992-12-02 to 2026-10-30), against
the base rate of "stress begins within N trading days". At 11:30 two of them were active:

| Condition | Active at | n onsets | Stress within 20d | Base rate | t | Grade |
|---|---|--:|--:|--:|--:|---|
| Score between 15 and 30 (upper quarter of neutral) | 11:30 | 54 | 38.9% | 12.3% | 4.0 | tested |
| Score rose ≥ 15 points in 20 trading days | 11:30, 14:45 | 107 | 13.1% | 12.4% | 0.2 | none |
| A third or more of the components at z ≥ 1 | not active | 46 | 37.0% | 12.3% | 3.4 | tested |
| VIX ≥ VIX3M (front of the curve inverted) | not active | 23 | 21.7% | 12.1% | 1.1 | none |

The figures are from `regime_study_v2`, recomputed on the stored score history up to the 14:45 run. The snapshots of
that day were written by v1, which also counted days that were already above 30 but not yet in an episode (an
episode starts on its third day above the line) as onsets and base days. Those days were near-certain hits. v1 showed
VIX ≥ VIX3M as `tested` (n 38, 44.7%) and breadth at 45.0%. Fifteen and fourteen of those onsets fell on such days,
and without them the term-structure condition has no measured edge. See [value-changes](value-changes.md).

Most of that history is an index of 5 to 9 components: the HY spread, equity put/call and SPY short volume only enter in 2026 (9 components until 2026-07-28, 11 from then, 12 from 2026-09-16). Score dispersion differs by era, so the fixed 30 line is not the same distance in every era. The VIX >= VIX3M condition can only be tested from 2006-07-17, and VIX3M before its 2007 launch is back-filled.

The first condition is the one with evidence behind it. Its onsets were followed by stress within 20 days 38.9% of
the time, against 12.3% for any calm day. On this occasion it took three hours. The 20-day momentum condition was also on,
but its record is no better than the base rate, so the card grades it `none` and the docs do not count it.

**14:45, the regime changes.** The score crossed 30, so the snapshot recorded `regime_changed: true`. Signal Watch
has one row for that event: "Chaos/Peace regime changed: neutral -> stress". The 15-minute alert job then pushed it to
the phone with the 14:50 quotes (SPY 751.65), about ten minutes before the 15:00 CT close. The row describes itself
as "a description of the reading, not a trade signal". That wording is deliberate.

## The disagreement the terminal did not hide

At 11:30 and at 14:45 the execution engine (`execution_v2`) printed **BULLISH (+3), call trigger active**, at low
conviction (one third of a standard size), targeting the 760 strike at 30–45 DTE. On Thursday it had printed CASH.

The +3 came from these votes: breadth −1 (broad sell-off), block flow +2 (a VWAP-heuristic buy-side imbalance, not the
feed's own trade sides), trend 0 (muted because VIX was high, while alignment was fully bearish), gamma 0, and max pain
+2 ("upward gravity", with price below max pain). Two of the three positive votes were a heuristic flow side and max pain.
Because the engine went from CASH to CALL, Signal Watch's engine row fired and its alert went out on 10-30. At the time
it was worded as a half-size Buy; that wording has since been changed to "No trade on this alone; use as confirmation".

Two models read the same day in opposite directions. The terminal shows both readings and does not merge them into
a verdict. The engine is a rules-based scorer for options tickets, and it does not pass through the evidence gate that
the rule cards use. Its tickets are recorded and marked at +1 day, +1 week and +2 weeks
([Engine positions](terminal.md#engine-positions)), so its record against the stress regime will show up in data
rather than in argument. For a reader, *stress regime plus a bullish engine* means "wait for confirmation". It is
not a signal to act on either reading alone.

## What the system could not see

The index measures stress. It does not explain it. Outside reporting that day pointed to a sell-off in technology
shares and to oil reacting to Middle East headlines. None of that is in the lake. The system stores prices, options,
credit and flow data, not narratives, and nothing on this page claims a cause.

## Why this is a useful record

- **Point-in-time.** Each intraday reading is a separate observation with its own `fetched_at`. The three Friday
  values of `chaos.score` (7.54, 24.31, 30.51) are three rows, never one overwritten value. A backtest can ask what
  was known at 11:30, and the answer is what is stored for 11:30.
- **Immutable.** The four snapshots above will read the same in a year. A change to the model gets a new version
  constant and a line in [value-changes](value-changes.md); stored values are never rewritten.
- **Reproducible.** `main_pipeline.py replay --run 20261030T194508Z-51aee5` re-renders that run from its stored snapshot
  JSON with no network. The models are not re-run, and the report text is byte-identical. The raw responses are stored
  separately for audit.
- **Exportable.** `main_pipeline.py export --metric chaos. --since 2026-10-29` writes these rows as CSV
  ([For quants](for-quants.md)).

## Check it yourself

```bash
sqlite3 -readonly CME_Data/portfolio.db "
  select observed_at, fetched_at, value_num from v2_observations
  where metric_id = 'chaos.score' and observed_at >= '2026-10-29' order by fetched_at"
```

A single day is an anecdote. The base-rate table above is the evidence, and it comes with the caveats the card itself
prints. The history is a single path, neighbouring days overlap (so the t-values are optimistic), and a score that is
already near the line needs less to cross it, so part of that 38.9% is arithmetic.
