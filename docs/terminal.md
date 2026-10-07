# Web terminal

What the browser terminal shows, panel by panel, and what each control does. It is for the person using the
terminal and for anyone changing `options_whale/templates/terminal.html`, `options_whale/static/app.js` or
`options_whale/static/forecast.js`. The Forecast Lab cards have their own reference in
[Forecast Lab](forecast-lab.md).

## Opening it

Start the server (see [Operations](operations.md)), then open `http://localhost:8080`. The port is
`OPTIONS_WHALE_PORT` (default 8080); see [Configuration](configuration.md). The server listens on all network
interfaces and has no login, so keep it on a private network.

The page loads Tailwind CSS and Chart.js from public CDNs, so the browser needs internet access.

Two kinds of panel exist:

- **Stored** panels read what the pipeline wrote: the latest snapshot, the CSV/SQLite ledgers and the newest daily
  folder. They change only when a run finishes.
- **Live** panels make network calls (yfinance, Databento, Yahoo RSS, eBay) while the request is open. They can be
  slow, can fail, and need the keys described in [Configuration](configuration.md).

Routes and response shapes are in [API](api.md). Several routes return HTTP 200 with `status: "error"` in the body, so a
panel that shows nothing usually has an error line in the console pane.

## Layout

```
header:   VLADHQ TERMINAL | REMOTE API status | host | clock
sidebar:  Macro Triggers, Custom Whale Hunter
center:   tab bar (MACRO DIRECTION, TIME ARBITRAGE, FORECAST LAB)
          active tab (panels)
          splitter
          Console Engine v2.0
mobile:   bottom bar (Panels, Console)
```

### Header

- **REMOTE API: ONLINE** (green dot) means `GET /help` answered. It is checked every 30 seconds. When the request
  fails the label becomes `OFFLINE (CHECK HOST)`. A non-OK answer leaves the label unchanged.
- The host name (upper case) and a clock. The clock shows the browser's local time.

Known issue: the status check and the clock are each started twice in `app.js`, so two timers run for each. See
[Known issues](known-issues.md).

### Sidebar: Macro Triggers

| Control | What it does | Route |
|---|---|---|
| RE-SCAN ALL DATA | Runs the whole pipeline on the server Mac and waits for it to finish. It is a normal delivered run: email, push and upload happen as for any run, and the dashboard HTML opens on the server Mac. The browser request has no timeout. When it ends the console prints the result and the VMRI and COMEX inventory frames reload. No other panel reloads. | `POST /run` |
| SCAN SILVER EAGLES | Looks up Silver Eagle listings through the eBay Browse API (up to 2 minutes), prints the XML in the console, and appends a row to the `physical_arbitrage_ledger`. | `GET /api/silver_eagle_prices` |
| DUMP ALL DATA | Fetches 15 routes from the browser, trims them, and shows one JSON summary in the Copy dialog (also copied to the clipboard). | many, see `dumpAllData()` in `app.js` |
| 8:31 AM FLOW | Prints the highest-volume and highest-open-interest calls and puts for SPY and SLV to the console. The Target Ticker is sent but ignored, and no filters apply. | `GET /api/morning` |
| 2:00 PM FLOW | Identical to 8:31 AM FLOW. | `GET /api/evening` |

The two FLOW buttons run `options_scanner.py` on the server with a 30 second limit. Known issue: they ignore the ticker and the morning/evening settings (`api_morning()`, `api_evening()` in `options_whale/api_router.py`). See [Known issues](known-issues.md).

### Sidebar: Custom Whale Hunter

| Field | Default | Used |
|---|---|---|
| Target Ticker | empty (placeholder TSLA) | yes. Also the ticker for the 8:31 and 2:00 flows and for the Time Arbitrage tab. |
| Min Vol/OI | 1.5 | yes |
| Max DTE | 14 | yes |
| Min Premium ($) | 100000 | **no** |

INJECT PARAMETERS calls `GET /api/custom` and prints the contracts in the console, largest premium first, with
call and put premium totals. The server ignores Min Premium: the floor is fixed at $100,000.

Known issue: the Min Premium field has no effect (`api_custom()` and `execute_xml_whale_hunt()` in
`options_whale/api_router.py`). See [Known issues](known-issues.md).

### Console Engine v2.0

- Every action writes a time-stamped line. Whale-hunt and Silver Eagle XML are drawn as readable rows; anything else
  is plain text.
- **[Minimize]** / **[Expand]** collapses the console to its title bar. **Data Dump** prints `GET /dump`
  (the newest tactical and volume XML). **Clear** empties the log.
- Drag the thin bar above the console to resize the panel area (minimum 200 px for panels, 150 px for the console).
  Desktop only.
- The panel height and the minimized state persist in the browser (see [Panel controls](#panel-controls)).

### Mobile layout

On narrow screens (about 768 px or less) the sidebar sits above the panels, panel dragging is off, and a bottom bar switches between
**Panels** and **Console**. The switch hides or shows the Macro Direction grid and the console only. Every console
message switches the view to Console, including messages written while panels load. The tab bar still switches tabs.

## Macro Direction tab

Fifteen panels in a three-column grid (two on medium screens, one on phones). Names are as displayed.

| # | Panel | Shows | Route | Source |
|---|---|---|---|---|
| 1 | MACRO RISK INDEX (VMRI) | VMRI history chart with the four risk zones, in a frame. REFRESH reloads it. | `/vmri_chart` (reads `/api/vmri_history`) | stored: last 500 macro ledger rows |
| 2 | COMEX PHYSICAL INVENTORY | Registered, eligible and total COMEX silver stocks over time, in a frame. REFRESH reloads it. | `/inventory_chart` (reads `/api/inventory_data`) | stored |
| 3 | WAR ROOM: SCENARIO ENGINE | VMRI scenario tool, see below. | `POST /api/war_room` | stored: latest macro ledger row, maths on the server |
| 4 | COMEX PAPER:PHYSICAL RATIO | Paper silver claims per ounce of registered silver. Gauge scale 0 to 50. Under 25 reads MARKET NOMINAL, 25 and over DELIVERY STRESS, 40 and over CRITICAL LEVERAGE. | `GET /api/dump` (the `leverage_ratio` in `comex_default_risk`) | stored; re-read every 5 minutes |
| 5 | PHYSICAL ARB LEDGER | eBay Silver Eagle prices against COMEX spot, last 50 ledger points. Toggles: Spot vs Physical, Premium %, Premium $. | `/api/arbitrage_history?limit=50` | stored (`physical_arbitrage_ledger`) |
| 6 | DEALER MAP (GEX) | Spot, Zero Gamma, Call Wall, Put Wall and net dealer gamma by strike. Ticker box (SPY) and SCAN. | `/api/gex` | live (yfinance, 3 nearest expirations, within 10% of spot) |
| 7 | DARK POOL TAPE | VWAP Anchor, Notional Value, Block Vol, Max Block, Bias and the latest block prints (Time, Block Size, Execution Price, Condition). Ticker box (SLV) and SCAN. | `/api/darkpool` | live (Databento, last completed session, blocks of 10,000 shares or more) |
| 8 | DARK POOL VISUALIZER | Bubble chart of the prints loaded by panel 7. The Min Size slider (10k to 500k) filters them in the browser. | none (reuses panel 7's response) | live |
| 9 | SLV INSTITUTIONAL FLOW | Sentiment, VWAP, Call Wall, Put Wall. Chart toggles: Sentiment, Block Vol, GEX Walls. | `/api/institutional_history?ticker=SLV&limit=100` | stored (`equities_darkpool_gex_ledger`) |
| 10 | SLV DEALER MAP (GEX) | Same as panel 6 for SLV. SCAN button. | `/api/gex?ticker=SLV` | live |
| 11 | CATALYST CALENDAR | Upcoming macro events with date, time, impact, forecast and previous. REFRESH. | `/api/macro_calendar` | stored (`upcoming_macro_events` in the newest `tactical_ruling.txt`) |
| 12 | MACRO NEWS FEED | Top Yahoo Finance headlines, each tagged BULLISH, NEUTRAL or BEARISH from a sentiment score (beyond plus or minus 0.05). REFRESH. | `/api/macro_news` | live (RSS, 10 items) |
| 13 | FED LIQUIDITY PLUMBING | Reverse Repo ($B) and Fed Balance Sheet ($B), with a chart. | `/api/macro_ledger_full?limit=200` | stored |
| 14 | SHANGHAI-COMEX ARB | SHFE Silver, COMEX Silver, Premium, with a chart. | `/api/macro_ledger_full?limit=200` | stored |
| 15 | ENGINE POSITIONS | Execution-engine tickets and how they moved. See [Engine Positions](#engine-positions). | `/api/positions` | stored and live |

Notes:

- On page load the panels fetch one after another between 1.6 and 3.4 seconds. Only panels 4 and 15 refresh by
  themselves afterwards (see [Refresh timing](#refresh-timing)). Panels 5, 9, 13 and 14 have no refresh
  button; reload the page to update them.
- Panel 8 and panel 7 show at most the 5 latest block prints, so the Min Size slider can only remove some of 5 bubbles.
- Panels 6 and 10 show **Zero Gamma equal to the spot price**. The live route does not compute a zero-gamma level;
  the pipeline does (`zero_gamma` in the snapshot), but these panels do not read it. Call Wall and Put Wall are the
  strikes with the largest positive and negative net gamma.
  Known issue: `get_gex_profile()` in `options_whale/api_router.py` sets `zeroGamma` to spot. See
  [Known issues](known-issues.md).
- There is no panel named "macro ledger". The macro ledger (`macro_master_ledger`) feeds panels 1, 3, 13 and 14.
- The terminal does not call `/api/macro_direction`, which returns only the VMRI score and a fixed `NEUTRAL`.
  Known issue: `get_macro_direction()` in `options_whale/api_router.py` returns a placeholder bias. See
  [Known issues](known-issues.md).

### War Room

Panel 3 recomputes the VMRI for a what-if. The live VMRI comes from the newest macro ledger row. Move a slider and the
panel posts the four shifts to `/api/war_room` (120 ms after the last move) and redraws.

| Lever | Range | Step |
|---|---|---|
| DXY | -20 to +20 points | 0.5 |
| 10Y YLD | -3 to +3 points | 0.1 |
| HY OAS | -2 to +15 points | 0.1 |
| VIX | -50% to +300% of its live level | 5 |

Presets: **1970s**, **2008**, **2020**. **Reset to live** sets every lever to zero. **? Guide** explains the score. The
bar shows the four tiers: below 150 Low, 150 to 250 Moderate, 250 to 350 Elevated, 350 and above Systemic. The histogram
at the bottom shows where the live and scenario scores sit among the VMRI scores recorded in the ledger (it needs at
least 20 recorded scores). The formula is in [API](api.md#macro-and-vmri) and `core/metrics.py` (`vmri()`).

## Time Arbitrage tab

Nine options-analytics panels. All but the last two come from one live route, `GET /api/time_arbitrage?ticker=...`, for
the Target Ticker in the sidebar (SPY if empty). It loads when the tab opens and **every 60 seconds while the tab is
open**; leaving the tab stops the polling. Each call makes several live yfinance requests. Panels 1 to 9 each have a
**?** button that opens a short help dialog.

| # | Panel | Shows | Source |
|---|---|---|---|
| 1 | CAPACITY CONSTRAINT OSCILLATOR | A -100 to +100 gauge. Scores of 75 or more in size read "STRATEGIC EDGE DETECTED" (bullish or bearish); anything smaller reads "CASH POSITION - NO STRUCTURAL EDGE". | live VIX against the macro ledger's last 30 days |
| 2 | DEALER TRAPDOOR | Spot against zero gamma (distance, percent), approach velocity, aggregate Vanna and Charm, and a "Gamma Neutral" or "SHORT GAMMA SQUEEZE" state. | zero gamma from the newest `equities_darkpool_gex_ledger` row for the ticker, else a fallback; chain is live |
| 3 | IV PREMIUM BLEED | Strike, Live IV, Hist. Avg and Bleed % for 15 call strikes. "Hist. Avg" is the 20-day realized volatility. | live |
| 4 | ASYMMETRIC PROBABILITY MATRIX | Log-normal probability that each of 5 strikes expires in the money in 3, 5 and 7 days, plus an "Optimal Strike Selection" line. | live |
| 5 | DEALER PIN MAP | Vanna and Charm by strike. | live |
| 6 | VOLATILITY TERM STRUCTURE | At-the-money IV for the expirations nearest 7, 30, 90 and 180 days. | live (cached 60 s) |
| 7 | IV / HV SPREAD | 20-Day Realized HV against Front-Month ATM IV, with an overpriced or underpriced verdict (a gap of more than 5 points is flagged). | live |
| 8 | LIVE OPTION EXPLORER | Chain browser: Ticker (Enter loads), Expiration, CALL or PUT, LOAD CHAIN. Click a row for the Black-Scholes fair price, probability ITM/OTM, Delta, Gamma, Theta, Vega, Rho, breakeven, expected move, intrinsic and extrinsic value, and IV against HV. | live: `/api/option_chain`, `/api/option_calc` |
| 9 | INSTITUTIONAL WISHLIST | Contracts saved from the Explorer: Contract, Added Px, Live Px, Change (%), Date Added, Details and Remove. | the browser only (`localStorage`) |

Things to know:

- The wishlist is stored in this browser under `optionsWatchlist`. It is not shared between browsers or machines and is
  lost if site data is cleared.
  Known issue: **Live Px** is the price saved when the contract was added, not a live price, so Change (%) is always
  0.00 (`renderWatchlist()` in `app.js`). See [Known issues](known-issues.md).
- Known issue: the oscillator also uses GEX and DIX z-scores, but the pipeline writes those two ledger columns empty
  (`core/render.py`, macro ledger row), so they add zero and only VIX moves the gauge
  (`calculate_z_score_oscillator()` in `options_whale/quant_engine.py`). See [Known issues](known-issues.md).
- Known issue: panels 3 and 4 use the first rows of the call chain (the lowest strikes), not strikes near spot; the
  "Optimal Strike Selection" text always says "SPY". See [Known issues](known-issues.md).
- Known issue: the route substitutes made-up values when data is missing (VIX 20, ATM IV 0.20, zero gamma 0.5% below spot),
  against the rule in [Architecture](architecture.md#design-rules). Location: `get_time_arbitrage()` in
  `options_whale/api_router.py`. See [Known issues](known-issues.md).

## Forecast Lab tab

Eleven model cards (full reference: [Forecast Lab](forecast-lab.md)). Controls above the cards:

- **SPY** / **SLV** choose the ticker for the per-ticker cards. **REFRESH** reloads.
- The status line reads `TICKER · run <run id> · <time>` and adds `· N source issue(s)` when the run could not fetch some inputs.
- The data loads every time the tab is opened and on each button press. There is no automatic refresh.
- Each card has **?** (help), **⛶** (full screen) and **COPY**. Esc closes help or leaves full screen.

## Engine Positions

Panel 15 on the Macro Direction tab. It lists the trade ticket the execution engine produced on each live run, and
how each ticket has moved since. The header shows the count, for example `(23)`; REFRESH reloads. How tickets are
recorded and imported is in [Operations](operations.md#engine-positions).

| Column | Contents |
|---|---|
| ★ | Star. Click to toggle (yellow ★ on, grey ☆ off). Display only: it does not sort or filter. |
| Date | When the run that made the ticket was generated, in the browser's local time as `MM/DD h:mmam` or `pm` (for example `10/06 8:31am`). Hover for the full local date and time. |
| Position | `CASH`, or `CALL` / `PUT`, the strike, the expiry as `MM/DD`, and the days left (`12d`) or `EXP` once expired. A grey ✉ marks a ticket recovered from a sent report email. |
| Entry | Option tickets: the entry mid price in dollars. CASH tickets: the SPY price at the signal. |
| +1D, +1W, +2W | Change against the entry mid at 1, 7 and 14 calendar days after the entry day (New York date), measured on the first trading day on or after that date. Hover for the dollar value, the date, the SPY close and the basis. CASH tickets show the SPY move instead. |
| Now | Option tickets: change of the current mid (bid/ask midpoint, else last) against the entry mid, from the live chain. CASH tickets: SPY change since the signal. Expired contracts show `EXP`. |
| (blank) | ✕ stops tracking. |

Markers in the +1D, +1W and +2W cells:

| Shown | Meaning |
|---|---|
| `+12.5%` | A recorded market mid for that day (a pipeline run captured the contract's quote), or the intrinsic value at expiry. |
| `~+12.5%` | An estimate. No quote was recorded for that day, so the value is a Black-Scholes price at that day's SPY close, using the implied volatility that reproduces the entry price. |
| `…` | The date has not arrived. Hover shows the due date. |
| `—` | No value could be computed. |

Other behavior:

- Newest ticket first.
- ✕ asks for confirmation. It hides the row and stops pricing it. The record stays in the database; see
  [Operations](operations.md#engine-positions).
- Hovering a row shows the contract, score, allocation and bias.
- The list loads about 3.4 seconds after the page opens and refreshes every 60 seconds. The prices in the Now column
  come from a server cache that lives 60 seconds.
- An empty list reads "No tracked positions yet": tickets appear after each live pipeline run. Offline runs and replays
  record none, and a run records none when the engine had no verdict or no contract to pick.

## Panel controls

| Control | Where | Behavior |
|---|---|---|
| Drag a panel header | Macro Direction panels | Swaps the panel with the one you drop it on. Desktop only. The order is not saved: reload restores the default. Time Arbitrage panels and Forecast cards cannot be dragged. |
| **-** / **+** | Macro Direction panels | Zoom from 0.8 to 1.25 in steps of 0.1. Saved per panel. Frame panels reload when the zoom changes. |
| COPY | Macro Direction panels | Opens the Copy dialog with the panel's data and puts it on the clipboard at once. The dialog's own COPY copies again. Panels 1 and 2 copy the text returned by the last console command (Data Dump or RE-SCAN ALL DATA), or a note if none ran yet. |
| [MAX] / [MIN] | Panels 1 to 6 and 9 to 15 (not 7 or 8) | Full-screen the panel. Click the dark backdrop or [MIN] to return. Esc does not close it. |
| REFRESH, SCAN | Panels 1, 2, 11, 12, 15 (REFRESH); 6, 7, 10 (SCAN) | Reload that panel. SCAN reads the ticker box next to it. |
| **?** | Time Arbitrage panels | Opens a help dialog. |

Keyboard: Esc closes the War Room guide, closes the Forecast Lab help dialog, and leaves Forecast Lab full screen.
Enter in the Option Explorer's Ticker box loads the chain. There are no other shortcuts.

Browser storage (`localStorage`, per browser and per origin):

| Key | Holds |
|---|---|
| `vladhq_zoom_panel1` ... `vladhq_zoom_panel15` | Zoom level of each Macro Direction panel. |
| `vladhq_panel_height` | Height of the panel area, in pixels (default 550). |
| `vladhq_console_min` | `1` when the console is minimized. |
| `optionsWatchlist` | The Institutional Wishlist. |

### Refresh timing

| What | When |
|---|---|
| Engine Positions | 3.4 s after load, then every 60 s |
| COMEX paper:physical ratio (panel 4) | on load, then every 5 minutes |
| Time Arbitrage tab | on open, then every 60 s while the tab is open |
| API status | every 30 s (two timers) |
| Clock | every second (two timers) |
| Forecast Lab | each time the tab opens, and on SPY, SLV or REFRESH |
| Everything else | once at load, or on its own REFRESH or SCAN button |

### Known issues in the controls

- Known issue: the Copy dialog sits inside the Macro Direction tab (`#copyDataModal` in `terminal.html`), and hidden
  tabs are `display: none`. On the other two tabs the dialog may be hidden, although the text is still copied to the
  clipboard. This is from reading the code; it has not been tested in a browser. It also affects DUMP ALL DATA and the
  Forecast Lab COPY button. See [Known issues](known-issues.md).
- Known issue: `panelHelp` in `app.js` has entries for Macro Direction panels that no button opens, and their
  numbering and titles no longer match the panels. Only the Time Arbitrage entries are reachable. See
  [Known issues](known-issues.md).
