# For traders

How to use the terminal during a session: what to look at, in what order, and what each reading does and does not
tell you. Definitions of terms are in the [plain-English guide](guide.md). Each panel is described in
[Terminal](terminal.md).

> Nothing here is financial advice. Every number is an estimate with a stated caveat, and a rule only says "Buy"
> when its own history passes an evidence test (see Signal Watch below for the two tests).

## The daily rhythm

| When (ET) | What happens | What to check |
|---|---|---|
| 09:31 | First run: overnight data, opening chains | Chaos / Peace regime, the engine ticket, gamma walls |
| 09:35–15:50, every 15 min | `signal-alerts` checks the rule rows | Your phone: one push per row per day, only when a row goes from waiting to FIRED |
| 12:30 | Midday run | Has the regime watch or the engine changed since the morning? |
| 15:45 | Last run (skipped on 13:00 early closes) | Regime changes, rule exits in the last 30 minutes |

## Reading order

1. **Chaos / Peace (card 14): the weather.** The score runs from −100 (peace) to +100 (chaos), with neutral between
   −30 and 30. Note which components are driving it and the **regime watch**. Each of its four early-warning conditions
   shows its record (how often stress followed within 20 and 60 days, against the base rate) and an evidence grade.
   A condition graded `none` is shown for context and should be given no weight.
2. **Dealer gamma (Macro Direction, Time Arbitrage).** Above zero gamma, dealer hedging tends to dampen moves; below
   it, hedging tends to add to them. The call and put walls are where the open interest is concentrated. Walls and zero gamma use the 3 nearest listed
   expirations; for SPY those are 0 to 2 DTE daily expiries, so monthly open interest is not included.
3. **Market-implied range (card 1)** against the **volatility forecast (card 2)**. This tells you whether options are
   pricing more or less movement than history predicts.
4. **Signal Watch (card 0).** Each trigger, fired or waiting, with the action it implies. A fired row graded `tested` says "Buy · standard
   size" and one graded `thin` says "Buy · half size (small sample)". The fair-value, momentum and positioning rows
   get that grade from a 5-point win-rate gap and at least 30 cases, with no t-test, so treat it as a screen. The engine
   row never says Buy: it reads "No trade on this alone; use as confirmation".
5. **The engine ticket.** A legacy confluence scorer. Read it last and check it against everything above.

## When the readings disagree

On 2026-10-30 the Chaos index moved into **stress** while the engine printed **BULLISH (+3)**
([case study](case-study-2026-10-30.md)). The terminal shows disagreements like this as they are, without resolving
them. A practical reading:

- **Stress regime and a bullish engine:** wait for confirmation. The engine does not pass through the evidence gate,
  and its record in stress regimes is still being collected.
- **Peace regime and a bearish engine:** the same caution in the other direction.
- **A rule card graded `tested` against a stress regime:** the rule's history includes stressed periods. Read the
  card's own caveat. No rule in the system changes its size for the stress regime; that call is yours.

## Sizing and tracking

- Engine tickets are recorded on every live run, including CASH, and marked at +1 day, +1 week and +2 weeks at the option's
  mid, or its last trade when there is no quote ([Engine Positions](terminal.md#engine-positions)). The ticket's own track record is the best
  guide to how much weight it deserves.
- The Day Scanner trades SPY as a call spread of at most $300 and opens a paper position that closes after five
  trading days.

## Ask in plain words

The console (menu drawer → Console Engine v3) answers from the terminal's own read-only sources, for example
"why is the Chaos score up today?", "where are the gamma walls?" or "what does the regime watch say?". Every figure
in an answer comes from stored or live data and carries its date.
