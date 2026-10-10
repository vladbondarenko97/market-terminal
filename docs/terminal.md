# Web terminal

What the browser terminal shows, panel by panel, and what each control does. It is for the person using the
terminal and for anyone changing `options_whale/templates/terminal.html`, `options_whale/static/app.js` or
`options_whale/static/forecast.js`. The Forecast Lab cards have their own reference in
[Forecast Lab](forecast-lab.md).

## Opening it

Start the server (see [Operations](operations.md)), then open `http://localhost:8080`. The port is
`OPTIONS_WHALE_PORT` (default 8080); see [Configuration](configuration.md). The server listens on all network
interfaces and has no login, so keep it on a private network.

The page loads Tailwind CSS (3.4.17) and Chart.js (4.4.1) from public CDNs, so the browser needs internet access. Both
are pinned to an exact version in `terminal.html`, so a new upstream release cannot change the page.

Two kinds of panel exist:

- **Stored** panels read what the pipeline wrote: the latest snapshot, the CSV/SQLite ledgers and the newest daily
  folder. They change only when a run finishes.
- **Live** panels make network calls (yfinance, Databento, Yahoo RSS, eBay) while the request is open. They can be
  slow, can fail, and need the keys described in [Configuration](configuration.md).

Routes and response shapes are in [API](api.md). A route that fails answers with an error status and a short message.
The scripts read that body, whatever the status, and show the message in the panel or as an error line in the console
pane; a reply that is not JSON at all (a proxy error page, for example) shows as `HTTP <status>`. A value a live route
cannot compute arrives as `null` with a reason, and the panel shows `—` (the reason is in a tooltip or beside it), never
a stand-in number.

## Layout

```
header:   ☰ | MARKET TERMINAL | REMOTE API status | host | clock
drawer:   off canvas, opened by ☰: Macro Triggers, Custom Whale Hunter
page:     tab bar (MACRO DIRECTION, TIME ARBITRAGE, FORECAST LAB)     full width
          active tab (panels)
          splitter
          Console Engine v3
phones:   the same page, with the console as a docked bar (see Mobile layout)
          bottom bar (Panels, Console)
```

There is no left column: the tab bar and the panels use the whole width of the window.

### Header

- **☰** (aria label "Menu") at the left edge, before the logo, opens the [menu drawer](#menu-drawer). It is 40 px square.
- **REMOTE API: ONLINE** (green dot) means `GET /help` answered. It is checked every 30 seconds. When the request
  fails the label becomes `OFFLINE (CHECK HOST)`; when the server answers with an error status it reads
  `ERROR (HTTP <status>)`.
- The host name (upper case) and a clock. The clock shows the browser's local time.

### Menu drawer

The Macro Triggers and the Custom Whale Hunter form live in a drawer that slides in from the left over the page, on every
screen width. It is not part of the page layout: closed, it takes no room (and is not reachable by Tab or a screen reader).

- **Open** with **☰**. The drawer is `min(320px, 85vw)` wide and as tall as the window, scrolls on its own when the
  content is taller than the window, and has a title row (**MENU**) with a **×** button. A dark backdrop covers the page
  and the page behind does not scroll. The slide takes about 200 ms; with the operating system's "reduce motion" setting
  there is no animation.
- **Close** with ×, a tap or click on the backdrop, Esc, or by pressing any button inside it (an action closes it, also one
  that is refused, such as a second RE-SCAN while one is followed). Focus moves to the × when it opens and back to ☰ when it
  closes; Tab stays inside while it is open. `aria-expanded` on ☰ follows it.
- **After an action:** on a phone the drawer closes and the console opens (OPEN, see [Mobile layout](#mobile-layout)), so
  the messages of the action are on screen. On the wide layout the console is already on screen and the split stays as it
  was (a minimized console stays minimized).
- It is closed on every page load. Nothing about it is stored (an older `vladhq_sidebar_open` key is removed). The fields
  keep their values while it is closed, and the Time Arbitrage tab still reads the Target Ticker.

### Drawer: Macro Triggers

| Control | What it does | Route |
|---|---|---|
| RE-SCAN ALL DATA | Starts the whole pipeline on the server Mac and follows it; see [RE-SCAN ALL DATA](#re-scan-all-data). It is a normal delivered run: email, push and upload happen as for any run, and the dashboard HTML opens on the server Mac. | `POST /run`, then `GET /api/run_status` |
| SCAN SILVER EAGLES | Looks up Silver Eagle listings through the eBay Browse API (up to 2 minutes), prints the XML in the console, and appends a row to the `physical_arbitrage_ledger`. Because it writes, it is a POST, and a second click while a scan is running is refused. | `POST /api/silver_eagle_prices` |
| DUMP ALL DATA | Fetches 14 routes from the browser, trims them, and shows one JSON summary in the Copy dialog (also copied to the clipboard). A route that failed is listed with its message under `_meta.failed_sources`. It does not run the Silver Eagle scan; the ledger it writes is already in the dump as `arbitrage_history`. | many, see `dumpAllData()` in `app.js` |
| 8:31 AM FLOW | A filtered whale scan of the Target Ticker (SPY if empty): expiries within 14 days, volume/open interest of at least 1.5, premium of at least $100,000. Printed like the custom hunt. | `GET /api/morning` |
| 2:00 PM FLOW | The same scan with no expiry limit, volume/open interest of at least 1.0 and premium of at least $500,000. | `GET /api/evening` |

The FLOW buttons use the fixed presets above and ignore the Max DTE, Min Vol/OI and Min Premium fields; INJECT
PARAMETERS below is the scan with your own filters. The FLOW scans send no phone alert.

#### RE-SCAN ALL DATA

The pipeline takes minutes, so the page does not hold one request open for it. `rescanAll()` in `app.js`:

1. Reads `GET /api/run_status` and remembers the `run_id` of the last run.
2. Sends `POST /run`. The server answers at once: `202` (started), or `409` when a run already holds the run lock. A
   `409` prints `busy` with the running run's id and stage, and nothing starts. Any other error prints its message
   (for example the `404` when `run_dashboard.command` is missing).
3. Reads `/api/run_status` every 5 seconds until a `run_id` it has not seen appears and `lock_held` is false. The console
   prints when the new run appears and each time its stage changes.
4. Prints how the run ended: `completed` (green), `completed_with_warnings` (yellow, with the error text),
   `failed` or `interrupted` (red, with the error text). Then it reloads the VMRI and COMEX inventory frames. No other
   panel reloads.

If no new run appears within 90 seconds the console says it probably did not start and points at `.v2_manual_run.log` in
the server's data folder, where the launcher's output goes. After 45 minutes the page stops waiting (the run may still
be going; use `main_pipeline.py status` on the server). While it follows a run the button reads `RE-SCANNING...` and is
disabled. Closing the page does not stop the run.

### Drawer: Custom Whale Hunter

| Field | Default | Used |
|---|---|---|
| Target Ticker | empty (placeholder TSLA) | yes. Also the ticker for the 8:31 and 2:00 flows (SPY if empty) and for the Time Arbitrage tab. |
| Min Vol/OI | 1.5 | yes |
| Max DTE | 14 | yes |
| Min Premium ($) | 100000 | yes |

INJECT PARAMETERS calls `GET /api/custom` and prints the contracts in the console, largest premium first, with
call and put premium totals.

### Console Engine v3

- Every action writes a time-stamped line. Whale-hunt and Silver Eagle XML are drawn as readable rows; an `<error>`
  reply from a scan route is printed as an error line; anything else is plain text (shown as text, never interpreted as
  HTML). Load-time lines (the panels scanning on page load) are logged as `info`; a line you caused by pressing a button
  is logged as `cmd`.
- The title row has **[Minimize]** / **[Expand]**, **[Maximize]** / **[Restore]**, **Data Dump** (prints `GET /dump`, the
  newest tactical and volume XML) and **Clear** (empties the log).
- **Wide layout (768 px and wider).** The panels sit above the console with a thin splitter between them.
  **[Minimize]** / **[Expand]** collapses the console to its title bar; clicking the title row does the same. Drag the
  splitter to resize the panel area (minimum 200 px for panels, 150 px for the console). The panel height and the
  minimized state persist in the browser (see [Panel controls](#panel-controls)).
- **[Maximize]** (both layouts) makes the console cover the whole window, over the header, the tabs and the bottom bar.
  The title row then shows **[Restore]**; Esc does the same. Restoring returns to what it was before: the split, or the
  collapsed title bar, or on a phone OPEN. Maximized is never stored: after a reload the page starts with the normal
  split. While it covers the page, **[Minimize]** is hidden on the wide layout (there is no split to collapse).
- The console is shown in three states on a phone, and as the split or maximized on the wide layout; see
  [Mobile layout](#mobile-layout). Resizing the window (or turning a phone) across 768 px re-applies the right layout at
  once: no docked bar on the wide layout, no splitter on a phone. A maximized console stays maximized across the change.

### Mobile layout

On phones (a viewport narrower than 768 px; 768 px and wider gets the wide layout, the same breakpoint as Tailwind's `md:`
classes) the page changes as follows. The wide layout is not affected by any of it. All the phone rules sit in one
`@media (max-width: 767.98px)` block at the end of `styles.css`; `MOBILE_QUERY` in `app.js` holds the same number.

- **Menu.** The Macro Triggers and the Custom Whale Hunter form are in the [menu drawer](#menu-drawer), opened by **☰** in
  the header. There is no bar above the tabs, so the active tab gets the screen (header, tab bar and bottom bar take about
  200 px; the docked console bar below takes 36 px).
- **Forecast Lab tables.** No table is wider than its card and the tab does not scroll sideways. A wide table (the
  Edge Lab matrix, the model cards' tables) scrolls inside its own card. The rule tables (Signal Watch, Day Scanner,
  Edge Lab detail, live-watch positions) are shown as one block per row, each cell under its column name
  (`fcLabelCells` in `forecast.js` sets `data-label`).
- **Console states.** The console has three states on a phone. They are not stored; a phone always starts MINIMIZED.
  - **MINIMIZED** (default; **Panels** is lit in the bottom bar). The tab bar and the active tab (Macro Direction, Time
    Arbitrage or Forecast Lab) are shown. The console is a single docked bar, 36 px high, just above the bottom bar. It reads
    **CONSOLE** and the newest log line, cut off with an ellipsis when it is too long, in the colour of its level (grey info,
    green success, yellow warn, red error, blue cmd). Scan answers show as a count (`Whale hunt: 3 contracts`); **Clear**
    sets it to `Console cleared`. Tapping the bar opens the console.
  - **OPEN** (**Console** is lit). Opened by the bottom bar, the docked bar, or a `cmd` line (a command you start, such as
    pressing a scan button). The console fills everything between the header and the bottom bar. **The tab bar and all tab
    content are hidden**; they are only in the Panels view. Opening scrolls the log to the newest line.
  - **MAXIMIZED.** **[Maximize]** in the console's title row. The console covers the whole screen, over the header and the
    bottom bar. **[Restore]** or Esc goes back to OPEN.
  - The title row on a phone shows **[Minimize]** (back to MINIMIZED / Panels), **[Maximize]** or **[Restore]**,
    **Data Dump** and **Clear**, each at least 32 px tall and wide. The words "Engine v2.0" are left out of the title to
    make room. Only the buttons act; the title row is not clickable on a phone.
  - Choosing **Panels** in the bottom bar, or any tab (`switchTab()`), goes back to MINIMIZED (also from MAXIMIZED). Messages
    written while panels load, background refreshes and tab changes do not open the console. `switchMobileTab('panels' |
    'console')` in `app.js` does the switch (MINIMIZED / OPEN; it does nothing on the wide layout). Data Dump, a `cmd`
    line, does not leave MAXIMIZED.
- **Panel dragging** is off.
- **Text fields** (text, number, date, menus, the Copy dialog, the drawer's Whale Hunter form) are 16 px. Safari on iPhone
  zooms the page in when a field smaller than that gets focus; zooming by pinch is not disabled.
- **Touch targets.** The small controls (panel zoom **-** / **+**, REFRESH, COPY, [MAX], SCAN, **?**, the card buttons,
  chart toggles, scenario buttons, the War Room sliders, the console's title-row buttons) get about 32 px of height and
  width; ☰ and the drawer's buttons are 40 px. Panel title bars wrap their controls onto a second line when they do not fit.
- **Tab bar.** The three labels use a smaller type size and letter spacing so each stays on one line down to 320 px.
- **Forecast Lab card 11.** When the chart is narrower than 640 px the legend goes below it, so the series names are not
  cut off, also in full screen. On the wide layout it stays on the right.
- **War Room.** In full screen the tier labels (LOW, MODERATE, ELEVATED, SYSTEMIC) and the numbers under the bar use a
  smaller size so they do not run into each other.
- **Capacity Constraint Oscillator.** With no reading the dial shows a short label (NO READING) and the needle fades; the
  full reason is under the gauge. This is so on every screen size.
- The page height follows the visible screen (`100dvh`), so the browser's own toolbar does not cover the bottom bar.
- A phone turned sideways is usually 768 px or wider, so it gets the wide layout: the split, the splitter and no bottom
  bar. Turning it back restores the phone state it had (OPEN stays OPEN).


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
| 7 | DARK POOL TAPE | VWAP Anchor, Notional Value, Block Vol, Max Block, Bias (with how it was worked out) and the latest block prints (Time, Block Size, Execution Price, Side). Ticker box (SLV) and SCAN. | `/api/darkpool` | live (Databento, last completed session, blocks of 10,000 shares or more) |
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
- Panels 6 and 10 show **Zero Gamma** as the spot level where aggregate net gamma changes sign, worked out the way the
  pipeline does it. When there is no sign change within 10% of spot the server sends `null` and the box shows `—`; hover
  it for the reason. Call Wall and Put Wall are the strikes with the largest positive and negative net gamma.
- Panel 7's **Side** column and the Bias follow the exchange feed's trade side: `BUY` (buy aggressor), `SELL` (sell
  aggressor) or `UNKNOWN` (no side given), exactly as the server sends them. The line under Bias says how it was
  worked out: `AGGRESSOR SIDE` (from the sides) or `VWAP HEURISTIC` (the feed gave no sides for most of the volume, so
  it is a guess from where the blocks printed against the VWAP). Hover it for the explanation.
- There is no panel named "macro ledger". The macro ledger (`macro_master_ledger`) feeds panels 1, 3, 13 and 14.

### War Room

Panel 3 recomputes the VMRI for a what-if. The live VMRI comes from the newest macro ledger row. Move a slider and the
panel posts the four shifts to `/api/war_room` (120 ms after the last move) and redraws.

| Lever | Range | Step |
|---|---|---|
| DXY | -20 to +20 points | 0.5 |
| 10Y YLD | -3 to +3 points | 0.1 |
| HY OAS | -2 to +15 points | 0.1 |
| VIX | -50% to +300% of its live level | 5 |

If the ledger lacks one of the four inputs the server answers `503` with the name of the input, and the panel shows
that message in place of the tier (and a dash for the score until a good answer has arrived).

Presets: **1970s**, **2008**, **2020**. **Reset to live** sets every lever to zero. **? Guide** explains the score. The
bar shows the four tiers: below 150 Low, 150 to 250 Moderate, 250 to 350 Elevated, 350 and above Systemic. The histogram
at the bottom shows where the live and scenario scores sit among the VMRI scores recorded in the ledger (it needs at
least 20 recorded scores). The formula is in [API](api.md#macro-and-vmri) and `core/metrics.py` (`vmri()`).

## Time Arbitrage tab

Nine options-analytics panels. All but the last two come from one live route, `GET /api/time_arbitrage?ticker=...`, for
the Target Ticker in the menu drawer (SPY if empty). It loads when the tab opens and **every 60 seconds while the tab is
open**; leaving the tab stops the polling. Each call makes several live yfinance requests. Panels 1 to 9 each have a
**?** button that opens a short help dialog.

Every number on the tab can be missing. The server then sends `null` with a reason in `data.missing`, and the panel
shows `—` instead of a number (hover a dash, or read the text in the panel, for the reason). A line above the panels lists
every missing value with its reason. If the whole request fails, the line shows the error message and the panels are
blanked, so numbers from an earlier answer (or another ticker) never sit next to an error.

| # | Panel | Shows | Source |
|---|---|---|---|
| 1 | CAPACITY CONSTRAINT OSCILLATOR | A -100 to +100 gauge. Scores of 75 or more in size read "STRATEGIC EDGE DETECTED" (bullish or bearish); anything smaller reads "CASH POSITION - NO STRUCTURAL EDGE". With no score the dial reads "NO READING" (the needle fades out) and the box under it reads "NO READING" too. The reason is in the line under the gauge, which says which factors the score was built from and why any other was left out (or, when the route sent no factors, gives the reason itself), and in the tab's note line; the dial label shows it as a tooltip. | live VIX (and the GEX and DIX columns) against the macro ledger's recent values |
| 2 | DEALER TRAPDOOR | Spot against zero gamma (distance, percent), approach velocity, aggregate Vanna and Charm, and a "Gamma Neutral" or "SHORT GAMMA SQUEEZE" state. Without a zero-gamma level it reads "No Gamma Reading" and the reason. | spot and zero gamma from the newest `equities_darkpool_gex_ledger` row for the ticker; chain is live |
| 3 | IV PREMIUM BLEED | Strike, Live IV, Hist. Avg and Bleed % for the 15 call strikes nearest the spot price. "Hist. Avg" is the 20-day realized volatility. | live |
| 4 | ASYMMETRIC PROBABILITY MATRIX | Log-normal probability that each of the 5 strikes nearest the spot price expires in the money in 3, 5 and 7 days, plus an "Optimal Strike Selection" line that names the ticker being scanned. | live |
| 5 | DEALER PIN MAP | Vanna and Charm by strike. | live |
| 6 | VOLATILITY TERM STRUCTURE | At-the-money IV for the expirations nearest 7, 30, 90 and 180 days. | live (cached 60 s) |
| 7 | IV / HV SPREAD | 20-Day Realized HV against Front-Month ATM IV, with an overpriced or underpriced verdict (a gap of more than 5 points is flagged). | live |
| 8 | LIVE OPTION EXPLORER | Chain browser: Ticker (Enter loads), Expiration, CALL or PUT, LOAD CHAIN. Click a row for the Black-Scholes fair price, probability ITM/OTM, Delta, Gamma, Theta, Vega, Rho, breakeven, expected move, intrinsic and extrinsic value, and IV against HV. A quote the feed did not give shows `—`. When the chain has no implied volatility for the contract the route answers `422` and the panel shows that message in place of the figures. When realized volatility is missing, the IV/HV banner gives the reason. | live: `/api/option_chain`, `/api/option_calc` |
| 9 | INSTITUTIONAL WISHLIST | Contracts saved from the Explorer: Contract, Added Px, Live Px, Change (%), Date Added, Details and Remove. REFRESH fetches the live prices again. | the browser (`localStorage`) for the list; `/api/option_calc` for Live Px |

Things to know:

- The wishlist is stored in this browser under `optionsWatchlist`. It is not shared between browsers or machines and is
  lost if site data is cleared.
- **Live Px** is the chain's last price for the contract now. While the Time Arbitrage tab is open the page asks
  `GET /api/option_calc` with `market_price=0` for each saved contract, one at a time, when the tab opens, after each
  60-second poll, when a contract is added, and when you press REFRESH. An answer younger than a minute is reused. The
  rows fill in as the answers arrive. **Change (%)** is Live Px against **Added Px**. A contract whose price is not
  available (the request failed, the chain has no price for it, or it has expired) shows `—` for both, with the reason
  as a tooltip; Added Px stays. The prices are only fetched while the tab is open, so the column shows `—` until you
  open it.
- The Added Px of a contract is the chain price when you saved it. If the chain had no price then, Added Px is `—` and
  Change (%) stays `—`.
- The oscillator uses only the factors that have data. The line under the gauge names them; for example
  `Built from VIX. Left out: GEX (no recent GEX values in the macro ledger).`

## Forecast Lab tab

Eleven model cards (full reference: [Forecast Lab](forecast-lab.md)). Controls above the cards:

- **SPY** / **SLV** choose the ticker for the per-ticker cards. **REFRESH** reloads.
- The status line reads `TICKER · run <run id> · <time>` and adds `· N source issue(s)` when the run could not fetch some inputs.
- The data loads every time the tab is opened and on each button press. There is no automatic refresh.
- Each card has **?** (help), **⛶** (full screen) and **COPY**. Esc closes help or leaves full screen.
- A card whose data is missing or failed shows `Unavailable: <reason>`. That includes cards 4, 9 and 10 when the pipeline
  recorded an error for their block, and card 11 when the whole refining block failed. If `/api/forecast` itself fails
  (for example `503` before the first run), every card shows the server's message.

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
| COPY | Macro Direction panels | Opens the Copy dialog with the panel's data and puts it on the clipboard at once. The dialog's own COPY copies again. Panels 1 and 2 copy the text returned by the last Data Dump, or a note if none ran yet. The Copy dialog sits at page level, so it opens on every tab (also for DUMP ALL DATA and the Forecast Lab COPY buttons). |
| [MAX] / [MIN] | Panels 1 to 6 and 9 to 15 (not 7 or 8) | Full-screen the panel. Click the dark backdrop or [MIN] to return. Esc does not close it. |
| REFRESH, SCAN | Panels 1, 2, 11, 12, 15 (REFRESH); 6, 7, 10 (SCAN) | Reload that panel. SCAN reads the ticker box next to it. |
| **?** | Time Arbitrage panels | Opens a help dialog. The Macro Direction panels have no help button. |

Keyboard: Esc closes the menu drawer (first), restores a maximized console, closes the War Room guide, closes the Forecast Lab help dialog and leaves Forecast Lab full screen.
Enter in the Option Explorer's Ticker box loads the chain. There are no other shortcuts.

Browser storage (`localStorage`, per browser and per origin):

| Key | Holds |
|---|---|
| `vladhq_zoom_panel1` ... `vladhq_zoom_panel15` | Zoom level of each Macro Direction panel. |
| `vladhq_panel_height` | Height of the panel area, in pixels (default 550). |
| `vladhq_console_min` | `1` when the console is minimized on the wide layout. Phones and the maximized state are not stored. |
| `optionsWatchlist` | The Institutional Wishlist. |

### Refresh timing

| What | When |
|---|---|
| Engine Positions | 3.4 s after load, then every 60 s |
| COMEX paper:physical ratio (panel 4) | on load, then every 5 minutes |
| Time Arbitrage tab | on open, then every 60 s while the tab is open |
| API status | every 30 s |
| Clock | every second |
| RE-SCAN ALL DATA | `/api/run_status` every 5 s while a run is followed, for up to 45 minutes |
| Wishlist Live Px | with each Time Arbitrage poll while the tab is open (an answer is reused for 60 s), and on REFRESH |
| Forecast Lab | each time the tab opens, and on SPY, SLV or REFRESH |
| Everything else | once at load, or on its own REFRESH or SCAN button |

## Rule cards and alerts (Forecast Lab tab, top)

Three cards are plain rules on stored runs and live Yahoo quotes: no model output. Every row is graded on history
and only a row with tested evidence can say "Buy"; hover any cell for what it means and where it comes from.

- **0 · Signal Watch** (`core/forecast.py: signal_watch`): the triggers the model cards compute (fair value, momentum
  flip, positioning, option pricing, dealer gamma, macro, engine ticket), fired or waiting, with the action.
- **12 · Day Scanner** (`core/watch.py`, `/api/scanner`): the dip-in-an-uptrend rule (2-day RSI under 10 or 5, above
  the 200-day average) on a saved watchlist (`+ ADD`, stored in `scanner_watchlist.json`). SPY trades as a call spread
  of at most $300 and opens a paper position (`v2_watch_positions`) that a sell alert closes 5 trading days later.
- **13 · Edge Lab** (`core/edges.py`, `/api/edges`): the published edges (reversal, post-earnings drift, turn of month,
  trend, 12-month momentum, volatility premium, overnight vs intraday) tested on any ticker's own history; `QUERY` analyses a symbol, `+ TRACK` saves it (`edges_tracked.json`).

Alerts: `python main_pipeline.py signal-alerts` (launchd `com.vlad.signalalerts`, every 15 minutes of the session on weekdays, installed by
`./setup.sh --schedule`) pushes to `NTFY_URL` and raises a macOS banner when a row goes from waiting to FIRED; it
exits outside the NYSE session. `--dry-run` prints the live states, `--test` sends a sample.

## Ask the terminal (console engine v3)

The console answers questions typed in words with a model on this machine. `options_whale/assistant.py` holds
everything server-side; v3 restored the October 3 version (branch `ai-terminal`, never merged), dropped the Claude
Code engine and added the routing below.

- **Brief first.** Every question carries a brief built from the database and the latest run: today's engine tickets
  (local time, contract, entry mid, score, size; a not-live entry quote is flagged), the latest run, fired and
  waiting Signal Watch rows, macro regime and the next CPI / Fed / OPEX dates, and what the brief leaves out. It has
  no clock and no live readings, so it only changes when the data does and a server with a prompt cache reads it
  once per run. The question carries the time. `GET /api/assistant/data?source=brief` shows it.
- **Then routing.** For anything the brief does not show, the model calls `fetch_source` for one to three of the 25
  read-only GET sources in `SOURCES` (Flask test client, unlisted parameters dropped). Large results come back as an
  outline that the model opens with `path=` and `last=`. Positions, Signal Watch, the Day Scanner, the Edge Lab,
  gamma and the VMRI history are pre-shaped (`SHAPERS`): plain contract names, local times, trades apart from cash
  tickets, gamma distances already worked out, the VMRI history as one dated row per week with the date the record
  starts. `calc` does the arithmetic. `/run` and the eBay scrape are not sources.
- **Reasoning.** `ASSISTANT_LOCAL_REASONING=auto` (default): no deliberate reasoning to answer from the brief or to
  pick sources, brief reasoning once data has been fetched. Sent both as `reasoning_effort` and as the chat
  template's `enable_thinking`, so oMLX and vLLM-style servers really switch it off.
- **Limits.** 8 rounds and 16 distinct calls per question (a repeated call is refused), 2,000 tokens per model call,
  `ASSISTANT_MAX_SECONDS` (default 150) per question, `ASSISTANT_MAX_CHARS` (default 12,000) per source. An empty
  reply (a model server short of memory can send one) is asked again once. After the last round the model is told
  no lookups are left, and a tool call written out as text is never shown as the answer.
- **Servers.** Default Ollama at `http://127.0.0.1:11434/v1` with `qwen3.6:35b-a3b`; the brief is built when
  the page opens. Keeping a model loaded is the model server's job (its idle timeout), not the console's. This Mac
  points `.env` at oMLX on port 8000 (`Qwen3.5-9B-MLX-8bit`, reasoning off, `ASSISTANT_MAX_CHARS=8000`). Do not keep a large Ollama model loaded next to oMLX: together they exceed the GPU's
  memory and both start failing.
- **Measured here (M4 Pro, 48 GB, 2026-10-09), 17 graded questions** (5 answerable from the brief, 5 needing one
  lookup, 7 multi-step): Ollama `qwen3.6:35b-a3b` with reasoning off 14-15/17, median 5-8 s; reasoning low 17/17,
  median 20 s. The old default `qwen3.8:27b-mlx` on Ollama read prompts at about 90 tokens/s: 96 s for the
  engine-position question. oMLX has not been graded yet: it was busy with an agent run during testing.
- **Conversations** live in server memory (the last 40; 8 turns each) and are lost on restart. "New chat" starts one.
- The server has no login. Anyone who can reach the port can ask questions. Keep the port on a private network.
