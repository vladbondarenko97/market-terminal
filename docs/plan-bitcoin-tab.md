# Plan: BITCOIN tab (cycle-bottom tracker and a HOLD / BUY / SELL card)

Written 2026-11-20 for the operator. A new tab right after CHART, Bitcoin only. It answers "where is the bottom, and is
it time to buy?" with conditions graded on history, never a price call. Follows the repository rules in AGENTS.md:
models run in the pipeline, missing is not zero, sources go through `SourceSession.fetch()`, additive storage, offline tests.

## What we already have
- `prices.btc` per run (Yahoo `BTC-USD`, `core/collect.py`), the BTC/silver and BTC/gold ratios, chart family 7.
- `BTC-USD` daily bars for the CHART tab (`core/bars.py`).
- Chaos (card 14), VMRI, net liquidity levers, VIX term structure, the CFTC fetcher (`cftc_cot`), the iShares page fetcher
  (`ishares_slv`), Edge Lab (`core/edges.py`) for grading rules on history, Signal Watch rows and the alert job.
- No Bitcoin source in the console / MCP (`mcp_server.py`, `options_whale/assistant.py` SOURCES): the assistant and
  the other sessions say "no BTC feed".

## Data (free, no key; each through `SourceSession.fetch()`, its own provider name and limit)

| Block | Signal | Source (verify each; drop a source that needs a key or blocks the Mac's IP) |
|---|---|---|
| Price | price, 200-week MA, drawdown from ATH, Mayer multiple (price / 200-day MA) | stored `BTC-USD` daily bars |
| On-chain | realized price, MVRV and MVRV Z-score, hash rate (30d vs 60d = hash ribbons) | Coin Metrics community API (`community-api.coinmetrics.io/v4`, metrics `PriceUSD, CapMrktCurUSD, CapRealUSD, CapMVRVCur, SplyCur, HashRate`; daily since 2010), else blockchain.com charts API for hash rate |
| On-chain | SOPR, short-term-holder cost basis, exchange balances | only if a free, keyless source exists; otherwise these rows are `missing` with that reason (no fallback) |
| Leverage | perpetual funding rate, open interest, liquidations | Deribit public API (`BTC-PERPETUAL` funding, DVOL index), OKX public API (funding, OI). Binance/Bybit refuse US IPs |
| Options | DVOL (BTC's VIX), 25-delta skew | Deribit public |
| Positioning | CME Bitcoin futures COT (asset managers, leveraged funds) | existing `cftc_cot` (TFF dataset, CME Bitcoin contract code) |
| Flows | spot ETF net flows: IBIT, FBTC, ARKB, BITB daily shares outstanding change x NAV | issuer pages, as `ishares_slv` does for SLV (iShares IBIT first) |
| Flows | Coinbase premium (Coinbase BTC-USD vs Kraken or OKX BTC-USDT) | Coinbase Exchange + Kraken public tickers |
| Liquidity | stablecoin supply (USDT + USDC) and its 30-day change | DefiLlama stablecoins API |
| Macro | BTC vs Nasdaq 60-day correlation, BTC vs net liquidity, DXY, real yields | stored bars and the existing macro blocks |
| Cycle | days since the last halving, days to the next | computed (block heights from the Coin Metrics block count, or blockchain.com) |
| Regulation | GENIUS Act rule dates, market-structure bill status, SEC/CFTC actions | a hand-kept `bitcoin_calendar.json` in the data folder (like `scanner_watchlist.json`), dated rows, shown as events |

Store raw responses in the lake like every provider; one new snapshot block `bitcoin` with `status` per row.
History for grading: Coin Metrics daily back to 2010 and the stored bars, backfilled once by a script the operator runs.

## Cards (the BITCOIN tab, same ? / ⛶ / COPY controls as Forecast Lab)

1. **B1 · Verdict: HOLD / BUY / SELL** (top, large). Two checklists, each row FIRED / waiting / missing with its reading,
   trigger and tested hit rate:
   - **Bottom checklist (BUY)**: price at or below realized price, or within 10% of the 200-week MA; MVRV Z ≤ 0.5;
     funding negative 3+ days then a liquidation spike; ETF net flows positive 5 days running; hash-ribbon recovery;
     Chaos < 50 and VIX/VIX3M < 1.
   - **Top checklist (SELL / trim)**: MVRV Z ≥ 6; price ≥ 2.4x the 200-day MA (Mayer); funding persistently high
     (top decile, 7 days); ETF outflows 5 days running after a run-up; Chaos turning from peace to chaos.
   - Verdict: **BUY (start)** at 3 of 6 bottom rows, **BUY (add)** at 5 of 6; **SELL (trim)** at 3 of 5 top rows;
     **HOLD** otherwise. Missing rows shrink the denominator and the card says "n of m available"; with fewer than half
     available the verdict is **NO CALL** and names what's missing. Rule cards' existing rule applies: only rows with
     tested evidence (Edge Lab grade) count toward BUY or SELL; untested rows show but don't vote.
   - Plain text under it: what would flip the verdict next ("2 more rows: ETF flows and funding").
   - Its own forward-return table: for each past date the verdict was BUY (start / add) or SELL, BTC's return after
     30 / 90 / 365 days, n and hit rate, so the card grades itself.
2. **B2 · Price & cycle**: price, drawdown from ATH, 200-week MA, realized price, Mayer multiple, halving clock; a log
   chart with the 200-week MA and realized price lines, past cycle bottoms marked.
3. **B3 · On-chain**: MVRV Z (history chart with the ≤0 and ≥6 bands), hash ribbons, SOPR / STH cost basis / exchange
   balances when available.
4. **B4 · Leverage & options**: funding (8h and 7-day average), open interest, liquidations, DVOL, skew.
5. **B5 · Flows**: spot ETF daily net flows (bars, 5-day sum), Coinbase premium, stablecoin supply change, CME COT.
6. **B6 · Macro link**: BTC vs Nasdaq correlation, BTC vs net liquidity, Chaos, VMRI, DXY.
7. **B7 · Regulation calendar**: the dated rows from `bitcoin_calendar.json`, next 90 days first, links to the law
   library ids where known.
8. **B8 · Plan & taxes** (operator's own numbers, stored in `bitcoin_plan.json` in the data folder, never in git):
   - DCA ladder: the cash he set aside, split into 3+ tranches tied to the verdict (start / add), with the tranche
     amounts and which have been used.
   - Holding-period clock per lot (acquired date, cost): days to long-term (> 1 year). Long-term gains are taxed at
     0% up to the 0% bracket for his filing status, so a held lot can be sold tax-free at low income: show the room.
   - Loss-harvest flag: a lot more than 10% under cost (crypto has no wash-sale rule today; the card says to confirm
     with the tax pro before acting).

## Wiring
- Signal Watch gets one row per B1 checklist, and the verdict change fires the usual once-a-day alert.
- `/api/bitcoin` (stored), `/api/bitcoin/history` (stored series for the charts). Both documented in `docs/api.md`
  and `/help`.
- MCP / console source `bitcoin` (price, verdict, every row with its reading and status, run time) in the SOURCES of
  `options_whale/assistant.py`, so the console, the iMessage assistant and other sessions can read it.
- Docs: `docs/terminal.md` (the tab), `docs/data.md` (block and tables), `docs/api.md`, `docs/configuration.md` if a
  setting is added, `docs/architecture.md` (new files).

## Phases
1. Price & cycle (B2) from stored bars + Coin Metrics on-chain (B3) + the verdict card (B1) with the rows those give,
   the MCP source, Signal Watch rows. The rest of B1's rows show `missing: not built yet`.
2. Leverage & options (B4), flows (B5).
3. Macro link (B6), regulation calendar (B7), plan & taxes (B8).
4. Grading: Edge Lab rows for each checklist rule on the 2010+ history, the verdict's forward-return table.

## Tests (offline, fixed dates)
Fixture responses for each new provider; the verdict logic on synthetic rows (3 of 6, 5 of 6, missing rows shrink the
denominator, under half = NO CALL, untested rows don't vote); 200-week MA / Mayer / MVRV Z maths on a synthetic
series; the shifted-clock run.

## Not financial advice
The card states conditions and their measured history. It is not a trade call, and it says so in its help text.

## Phase 5: extra cards (documented, not started)
Every new rule is a candidate until Edge Lab grades it (phase 4); missing data is missing, never zero.

- **B9 · Silver-to-BTC rotation**: BTC priced in silver ounces (already stored as `ratios.btc_silver_oz`) and its z-score against 4 years of history.
  Rule: when BTC is cheap in silver (z <= -1) AND the B1 verdict is BUY, rotate a tranche of silver into BTC; the reverse when z >= +1.5 and B1 says
  SELL. The operator holds physical silver, so this is the main lever. The card shows the collectibles-tax note: gains on physical metal are taxed as
  collectibles, up to 28%.
- **B10 · Extra cycle gauges**: Pi Cycle Top (111-day MA vs 2 x 350-day MA, from the bars) as a SELL-checklist candidate; Puell multiple (miner revenue /
  its 365-day average, blockchain.com miners-revenue chart API, free) as a BUY candidate (< 0.5) and a SELL candidate (> 4); Crypto Fear & Greed
  (alternative.me public API) as context only, not voting.
- **B11 · Global liquidity lead**: BTC vs US M2 (FRED, already a provider) shifted 10 to 12 weeks, plus net liquidity from the existing lever; the
  correlation and the current direction.
- **B12 · Position sizing**: the operator's set-aside cash and holdings from `bitcoin_plan.json` give the maximum BTC allocation as a share of liquid net
  worth (default cap 10%), the tranche size, and a "never from the emergency fund" check (cash below one month of expenses means no buy, and the card
  says so).
