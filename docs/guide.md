# Plain-English guide

What every card and number in the terminal means, written for a person rather than a programmer. The console and
the MCP server read this page: ask "what is a reverse repo?" or "explain the paper:physical ratio" and the answer
comes from here, with the live reading from the matching source. Formulas and storage details are in
[forecast-lab.md](forecast-lab.md), [terminal.md](terminal.md) and [api.md](api.md); this page only explains.

Each entry says what the thing is, why people watch it, how to read it, and which source holds the live number.

## How to ask

- Type a question into the console (Console Engine v3 in the menu drawer), or ask any agent connected to the MCP
  server. Both read the same sources.
- To learn a term, ask "what is X" or "explain X". The answer comes from this guide (source `guide`).
- To get the number, ask for it: "what is the ON RRP today?", "how did the last 10-year auction go?". Every figure
  comes from a stored or live source and carries its date; nothing is filled in from memory.
- To compare periods, name them: "this year vs last year", "since 2024", "in September", "the last 12 months".

## Ten questions worth asking

Questions with answers that make people stop scrolling. Each was asked on the live terminal; the figures in
brackets are what it answered on 2026-10-26 and will differ on other days. Every answer comes from official or
exchange data the pipeline stores, with its date.

1. **"The Fed's reverse repo once held $2.5 trillion. How much is in it today?"**
   (peak $2.55 trillion on 2022-12-30; $0.200 billion on 2026-10-26.) Sources: `repo_ops`, `repo_history`.
2. **"What was the biggest day of Fed repo lending ever, and how much is being borrowed now?"**
   (overnight repo $152.75 billion on 2020-03-17 at the Covid panic; $0.005 billion on 2026-10-26.) Source: `repo_ops`.
3. **"How much more of its own debt has the Treasury bought back this year than last year?"**
   ($230.2 billion vs $153.9 billion over the same dates, +49.6%.) Sources: `treasury_buybacks`, `buyback_history`.
4. **"Are options dealers calming SPY right now, or amplifying its moves?"**
   (SPY $759.97 below zero gamma at $769.15: dealers short gamma, so their hedging adds to moves.) Source: `gex` (live).
5. **"How many ounces of paper silver exist for every ounce of deliverable COMEX silver?"**
   (5.72 ounces of futures claims per registered ounce.) Sources: `paper_physical`, `comex_inventory`.
6. **"How much more does silver cost in Shanghai than in New York?"**
   (11.4% more before China's 13% VAT, 1.4% less after it, on 2026-10-22.) Source: `shanghai_spread`.
7. **"What happened to the S&P 500 in the year after every midterm election?"**
   (up in 19 of 19 cases, +16.2% on average against +9.4% in other years; evidence grade thin.) Source: `chaos`.
8. **"What are the odds of a US recession in the next 12 months, and what were they a year ago?"**
   (12.8% now against 28.0% a year earlier, NY Fed yield-curve model.) Source: `forecast` path `macro_regime`.
9. **"How close is the macro risk score to 'systemic'?"**
   (VMRI 244.84: above 77% of recorded readings, 5 points under Elevated at 250; Systemic starts at 350.) Source: `war_room`.
10. **"Is silver cheap or expensive against its fair value?"**
    ($53.94 against a model value of $58.43, 7.7% below.) Source: `forecast` path `silver_fair_value`.

Questions that ask why something happened ("is the Fed draining liquidity on purpose?") get what the data shows
and a plain statement that no source measures intent.

## Fed and Treasury plumbing (card 15)

### Reverse repo (RRP, ON RRP)

The Fed's overnight reverse repurchase facility. Money market funds and other eligible firms lend cash to the Fed
overnight and get Treasury securities as collateral plus a fixed rate (the bottom of the Fed's target range). It is
where spare cash parks when there is nowhere better to put it. A large balance means the system is flush with cash
nobody needs; a balance near zero means that cushion has been used up, mostly by buying Treasury bills instead.
It peaked at $2.55 trillion at the end of 2022 and has since drained to almost nothing. Read it as a daily level in
billions, never a sum of days. Live: `repo_ops`; history since 2014 with monthly averages: `repo_history`.

### Repo and the Standing Repo Facility (SRF)

The opposite direction: the Fed lends cash overnight against Treasuries or agency securities. Since July 2021 this is
the Standing Repo Facility, a backstop banks can tap when private funding gets expensive. Usage near zero means
private funding is working; spikes (quarter ends, March 2020, the September 2019 repo blow-up) mean someone needed the
Fed's cash. Term repo is the same for longer than overnight. Live: `repo_ops`; history: `repo_history`.

### SOFR, TGCR, BGCR, EFFR

Overnight interest rates published by the NY Fed each morning for the previous business day.

- **SOFR** (Secured Overnight Financing Rate): the broadest cost of borrowing cash overnight against Treasuries,
  about $3 trillion a day. It replaced LIBOR as the benchmark for loans and derivatives.
- **TGCR** (Tri-party General Collateral Rate) and **BGCR** (Broad General Collateral Rate): narrower slices of the
  same repo market.
- **EFFR** (Effective Federal Funds Rate): unsecured overnight lending between banks; the rate the Fed targets.

When SOFR rises above the top of the Fed's target range, cash is scarce. Live: `repo_ops` (with the 4-week change).

### Treasury buybacks

The US Treasury buying its own older, less-traded securities back from dealers before they mature, paid for by
issuing new debt. Two kinds: **liquidity support** (to keep the market for off-the-run bonds working smoothly) and
**cash management** (to smooth the Treasury's cash balance and bill issuance). Each operation has a **maximum** the
Treasury will buy, the amount dealers **offered**, and the amount **accepted**. Offered far above the maximum means
dealers were eager to sell. The programme restarted in 2024. Live: `treasury_buybacks`; every operation since 2024
with yearly and monthly totals: `buyback_history`.

### Treasury auctions

How the government borrows: it sells bills (4 weeks to 1 year), notes (2 to 10 years), bonds (20 and 30 years), TIPS
(inflation-protected) and FRNs (floating rate) at auction. CMB is a cash management bill, a one-off short bill.

- **High yield**: the highest yield accepted, which every winning bidder gets. Higher means the government pays more.
- **Bid-to-cover**: dollars bid per dollar sold. Around 2.5 is normal for notes; a falling number means weaker demand.
- **Direct, indirect, dealer shares**: who bought. Indirect bidders are mostly foreign central banks and funds
  bidding through a dealer. Dealers (primary dealers) must bid and take what nobody else wants, so a high dealer
  share means a weak auction.
- **Tail**: the gap between the high yield and the yield trading just before the auction. TreasuryDirect does not
  publish the pre-auction yield, so the terminal does not show tails.

Live and the calendar: `treasury_auctions`; every result since 2021 by tenor and year: `auction_history`.

### Net liquidity, bank reserves, TGA

**Net liquidity** is the Fed's balance sheet minus the Treasury General Account (the government's checking account at
the Fed, **TGA**) minus the reverse repo. It is a rough measure of the dollars left for markets. **Bank reserves**
(WRESBAL) are the deposits banks hold at the Fed. Both are levers on the Chaos / Peace card. Source: `chaos`.

## Macro risk

### VMRI (macro risk index)

A single score built from four stress gauges: the dollar index (DXY), the 10-year Treasury yield, the high-yield
credit spread (HY OAS) and the VIX. Formula: (DXY x 10Y yield / 1.61) x (HY OAS / 4) x (VIX / 20). Tiers: under 150
low, 150 to 250 moderate, 250 to 350 elevated, 350 and above systemic. It rises when the dollar, yields, credit fear
and equity fear rise together. Live: `war_room`; week by week: `vmri_history`.

### War Room

A what-if calculator for the VMRI. Move the dollar, yields, credit spreads or the VIX and see the score it would
give. Ask the console "what is the VMRI if the VIX doubles and spreads widen 2 points?". Source: `war_room` with shifts.

### DXY, 10-year yield, HY OAS, VIX, MOVE

- **DXY**: the US dollar against six major currencies. A strong dollar tightens conditions worldwide.
- **10-year yield (TNX)**: what the government pays to borrow for 10 years; the base rate for mortgages and valuations.
- **HY OAS**: the extra yield junk-rated companies pay over Treasuries. Widening means lenders fear defaults.
- **VIX**: the options market's expected S&P 500 volatility over 30 days. Around 12 to 15 is calm, above 30 is fear.
- **MOVE**: the same idea for Treasury bond volatility.

### Chaos / Peace (card 14)

Twelve stress readings (VIX and its term structure, VVIX, SKEW, MOVE, credit spreads, SPY volatility and drawdown,
policy uncertainty, the put/call ratio, the short-sale share), each scored against its own last three years and
combined into one score. Positive is more stressed than usual, negative calmer. The card also shows the liquidity and
Treasury-supply levers and how markets behaved around past elections. Live: `chaos`; history: `chaos_history`.

### Macro regime and recession odds (card 6)

The NY Fed's recession model turns the gap between the 10-year and 3-month yields into odds of a recession within 12
months; an inverted curve (short rates above long) raises them. Alongside: the Chicago Fed's financial conditions
index (**NFCI**, below zero is looser than average), the **Sahm rule** (a recession signal when unemployment rises half
a point above its 12-month low) and the high-yield spread. Source: `forecast` path `macro_regime`.

## Options and dealers

### Gamma exposure (GEX), call wall, put wall, zero gamma

Options dealers hedge what they sell. **GEX** estimates how much stock they must buy or sell as the price moves.
When dealers are long gamma they sell rallies and buy dips, which calms the market; when short gamma they chase
moves, which makes them bigger. The **call wall** is the strike with the most positive gamma (often acts as a ceiling),
the **put wall** the most negative (often a floor), and **zero gamma** the price where dealer hedging flips: spot **above** zero gamma means
dealers are long gamma and their hedging calms moves; spot **below** it means they are short gamma and their hedging
amplifies moves. Source: `gex`.

### Market-implied range (card 1)

The odds options are pricing for where a ticker ends up in a day, a week, a month and a year, read from option prices
across strikes. It is the market's forecast, not the terminal's. Source: `forecast` path `data.implied`.

### Volatility forecast (card 2)

A statistical forecast of how much the price will move, compared with what options charge. Forecast below implied
means options look expensive; above means cheap. Source: `forecast` path `data.vol_forecast`.

### Trend / CTA (card 3) and mechanical flows (card 8)

**CTAs** are trend-following funds that buy what has been rising and sell what has been falling over 1 to 12 months.
The card shows which way they are likely leaning and the price at which they would flip. Card 8 adds the other forced
buyers and sellers: volatility-targeting funds, leveraged ETFs rebalancing at the close, and dealer hedging, sized for
a 1% to 5% move. Sources: `forecast` paths `data.trend` and `data.flows`.

### Calendar effects (card 7)

How the ticker has behaved on FOMC days, CPI days, payrolls days, options expiry, the turn of the month and each
weekday, with how many cases and how strong the evidence is. Source: `forecast` path `data.calendar`.

### Dark pool tape

Large trades (10,000 shares or more) printed off-exchange, with their size, price, the volume-weighted average price
(**VWAP**) and whether buyers or sellers were the aggressors. Big blocks show where institutions traded. Source: `darkpool`.

### Time arbitrage

The volatility term structure: implied volatility for each expiry. Normally later expiries cost more volatility; when
near-term volatility is higher (inverted) the market expects trouble soon. Source: `time_arbitrage`.

### Evidence grades

Every rule and edge carries a grade: **tested** (30 or more cases and a clear difference), **thin** (fewer cases),
**none** (no measured edge). A grade of none is a description of history, not a trade.

## Silver

### COMEX registered and eligible inventory

Silver in COMEX vaults. **Registered** silver has a warrant and can be delivered against a futures contract today;
**eligible** silver meets the standard but belongs to someone who has not offered it for delivery. Falling registered
stock means less metal stands behind the futures. Source: `comex_inventory`.

### Paper:physical ratio

Ounces claimed by open COMEX silver futures (open interest x 5,000 oz) for every registered ounce. At 5.7, there are
5.7 ounces of paper claims per ounce available for delivery. Most futures never ask for delivery, so this measures
leverage, not a shortage. Tiers: under 15 nominal, 15 elevated, 30 high, 40 critical. Source: `paper_physical`.

### Shanghai-COMEX spread

The Shanghai Gold Exchange silver price against New York futures on the same day. China charges 13% VAT on silver,
so the raw spread overstates the gap; the VAT-adjusted spread is the fair comparison. A large adjusted premium means
Chinese buyers pay up for metal. Source: `shanghai_spread`.

### Silver fair value (card 5)

A regression of silver on gold, the 10-year real yield, the dollar, copper and industrial production. It says where
silver would trade if it followed those drivers, how far it is off, and how fast past gaps closed (the half-life).
A relative-value gauge, not a prediction. Source: `forecast` path `silver_fair_value`.

### Gold/silver ratio

Ounces of silver one ounce of gold buys. A high ratio means silver is cheap against gold by its own history.
Sources: `forecast` path `silver_fair_value`, `vmri_history`.

### Physical premium (Silver Eagles)

What the cheapest 1 oz Silver Eagle costs from dealers, all in, against the futures price. A rising premium means
retail demand for coins is outrunning supply. Source: `arbitrage_history`.

### Positioning / COT (card 4)

The CFTC's weekly Commitments of Traders: how speculators (managed money), producers and swap dealers are positioned in
silver and S&P 500 futures, scored against three years (0 is the most short, 100 the most long). Extremes have often
come before reversals. Source: `forecast` path `positioning`.

## Energy

### Diesel and refining (card 10), EIA inventories (card 11)

**Crack spreads** are the margin for refining crude into diesel or gasoline; high cracks mean products are tight. The
card adds refinery runs, the maintenance season and Texas outage filings. Card 11 compares weekly US crude and product
stocks with the five-year average for the same week, and **days of supply** (stocks divided by daily demand). The
**SPR** is the government's emergency crude stockpile; EIA publishes no days of supply for it, only barrels.
Sources: `eia_history` (stocks and days of supply now, a week, a month, 3 months and a year ago), `forecast` path
`refining`, `chaos`.

## Rules and the engine

### Signal Watch (card 0), Day Scanner (card 12), Edge Lab (card 13)

Signal Watch lists every rule-based trigger from the cards with its level and whether it fired. The Day Scanner checks
the dip-in-an-uptrend rule (2-day RSI) across the watchlist. Edge Lab tests published market edges (reversal, earnings
drift, turn of month, momentum and more) on any ticker's own history. Sources: `signal_watch`, `day_scanner`, `edge_lab`.

### Engine positions vs brokerage positions

**Engine positions** are the paper trade tickets the pipeline's execution engine issues each run, tracked afterwards.
**Brokerage positions** are the operator's real holdings. They are separate. Sources: `positions`, `brokerage` (console only).

### Forecast scorecard (card 9)

Every logged forecast graded after its date: the hit rate, whether the 68% ranges held 68% of the time, and the Brier
score (lower is better). Source: `forecast` path `scorecard`.
