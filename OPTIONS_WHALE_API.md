# Options Whale API Documentation

The **Options Whale API** is a high-performance financial intelligence backend designed for real-time market analysis, macro risk assessment, and institutional flow tracking. It powers the VladHQ Market Terminal and provides data across multiple asset classes (equities, options, precious metals).

## 🚀 Base URL
The API is typically hosted locally on:
`http://localhost:8080`

---

## 🛠 Authentication & Config
The API relies on several environment variables defined in `.env` or `config.py`:
- `DATABENTO_API_KEY`: Required for `/api/darkpool` (tick-level block trades).
- `FRED_API_KEY`: For economic data.
- `EBAY_APP_ID/CERT_ID`: For physical arbitrage scraping.

---

## 🐋 Options & Whale Hunting
These endpoints scan the options market for unusual institutional activity (High Volume vs. Open Interest).

### 1. `GET /api/morning`
Hunts for urgent, short-term directional momentum (8:31 AM logic).
- **Filters**: Max DTE = 14 days, Min Vol/OI = 1.5x, Min Premium = $100,000.
- **Params**: `ticker` (required, e.g., `SPY`)
- **Response**: JSON containing raw scanner output.

### 2. `GET /api/evening`
Hunts for massive structural positioning and earnings bets (2:00 PM logic).
- **Filters**: No Max DTE, Min Vol/OI = 1.0x, Min Premium = $500,000.
- **Params**: `ticker` (required, e.g., `NVDA`)
- **Response**: JSON containing raw scanner output.

### 3. `GET /api/custom`
Dynamic scanner allowing user-defined overrides. Returns data in a beautiful XML format.
- **Params**:
  - `ticker` (required): Symbol to scan.
  - `min_vol_oi` (optional, default: 1.0): Minimum Volume/OI ratio.
  - `min_premium` (optional, default: 100000): Minimum estimated premium spent.
  - `max_dte` (optional): Maximum days to expiration.
- **Response**: `application/xml` containing detailed contract metrics (IV, Spread, Premium, etc.).

---

## 🚨 Macro Risk (VMRI)
The **Vlad Macro Risk Index (VMRI)** aggregates fixed income stress, credit spreads, and equity volatility.

### 1. `GET /vmri`
Extracts the latest VMRI score with full documentation and formulas.
- **Response**: `application/xml` breakdown of Base Stress, Credit Multiplier, and Volatility Premium.

### 2. `GET /api/vmri_history`
Returns time-series history of VMRI scores and macro context for charting.
- **Response**: JSON containing scores, SMA (10-period), Momentum (5-period), and pillar data (DXY, VIX, etc.).

### 3. `GET /vmri_chart`
Serves a professional-grade interactive risk monitor.
- **Response**: `text/html` (Chart.js implementation).

### 4. `POST/GET /api/war_room`
The **SecDB Lite Impact Engine**. Simulates how shifts in macro variables affect systemic risk.
- **Params (JSON or URL)**: `dxy_shift`, `tnx_shift`, `oas_shift`, `vix_shift_pct`.
- **Formula**: `VMRI = (DXY × 10Y yield ÷ 1.61) × (HY OAS ÷ 4) × (VIX ÷ 20)`. Tiers: below 150 low, 150–250 moderate, 250–350 elevated, 350+ systemic.
- **Response**: JSON with `current` and `hypothetical` (inputs, VMRI, tier and the three `factors`), `impact`
  (`vmri_delta`, `vmri_delta_pct`, and `solo_delta`: the change each shift causes on its own), `thresholds`, and
  `history` (how many scores are recorded, their range and median, the share below the live and the scenario score,
  and histogram counts). `history` is `null` with fewer than 20 recorded scores.

---

## 🏛 Institutional & Quantitative Data

### 1. `GET /api/darkpool`
Tick-level institutional block trade analysis via Databento.
- **Params**: `ticker` (default: `SPY`)
- **Response**: JSON with block volume, VWAP, sentiment bias, and recent "prints" (>10k size).

### 2. `GET /api/gex`
Black-Scholes Gamma Exposure profile for market-maker positioning.
- **Params**: `ticker` (default: `SPY`)
- **Response**: JSON with GEX walls (Call/Put), Zero Gamma point, and strike distribution.

### 3. `GET /api/institutional_history`
Returns time-series history of institutional flows from local ledgers.
- **Params**: `ticker` (default: `SLV`), `limit` (default: 100).
- **Response**: JSON with GEX walls and Dark Pool metrics over time.

---

## 🪙 Precious Metals & Arbitrage

### 1. `GET /api/silver_eagle_prices`
Scrapes eBay for live Physical Silver Eagle prices using a Playwright engine.
- **Response**: `application/xml` comparing retail prices to COMEX spot.

### 2. `GET /api/arbitrage_history`
History of silver arbitrage metrics (Retail Premium % vs. COMEX Spot).
- **Response**: JSON optimized for time-series charts.

### 3. `GET /api/inventory_data`
Reads master COMEX inventory history (Registered vs. Eligible silver).
- **Response**: JSON payload for historical vault analysis.

### 4. `GET /inventory_chart`
Serves an interactive vault history dashboard.
- **Response**: `text/html`.

---

## 📦 System & Data Utility

### 1. `GET /api/dump` (or `/dump`)
Intelligently merges the latest CME data (Tactical Ruling + Volume Dashboard).
- **Response**: `application/xml` aggregate of the most recent trading session analysis.

### 2. `POST /run`
Triggers the local `run_dashboard.command` script on the host system.
- **Response**: `application/xml` execution confirmation with timestamp.

### 3. `GET /api/macro_news`
Fetches top 10 macroeconomic headlines from Yahoo Finance RSS.
- **Response**: `application/xml` with titles, links, and timestamps.

### 4. `GET /api/macro_calendar`
Extracts upcoming catalysts from the latest tactical ruling.
- **Response**: JSON array of events (date, time, impact, forecast).

### 5. `GET /api/macro_ledger_full`
Returns full 25-column macro master ledger data.
- **Response**: JSON for extensive historical analysis.

---

## 📦 Utility

### 1. `GET /help`
Self-documenting endpoint that returns API metadata in XML format.
- **Response**: `application/xml`.

### 2. `GET /`
Serves the main **VladHQ Market Terminal** frontend (`terminal.html`).

---

## ⚠️ Notes on Legacy Components
There is an additional `options_api.py` in the root directory providing a single endpoint `/api/find_options`. This is considered a standalone/legacy utility compared to the comprehensive `api_router.py` in the `options_whale` package.
