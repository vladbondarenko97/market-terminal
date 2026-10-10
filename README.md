# Market Terminal — a self-hosted market dashboard

A Python + SQLite system that collects market data twice a day, stores every raw response, computes a set of
quantitative models, and serves the result as a web terminal, an email report and a phone alert. It runs on one Mac
with no cloud services.

It has two parts:

| Part | What it is | Entry point |
|---|---|---|
| **Pipeline** | A batch run: collect → capture → snapshot → render → deliver. Runs on a schedule or by hand, then exits. | `python main_pipeline.py run` |
| **Terminal** | A Flask server with a browser UI. It shows what the pipeline stored, plus some live option and news panels. | `python options_whale/api_router.py` |

> Not financial advice. Every model output is a probability or an estimate, and each card states its own caveats.

## Documentation

| If you want to… | Read |
|---|---|
| Install it, run it, schedule it, fix a failed run | [Operations](docs/operations.md) |
| Fill in `.env` or change the data folder or port | [Configuration](docs/configuration.md) |
| Use the terminal | [Terminal](docs/terminal.md) and [Forecast Lab](docs/forecast-lab.md) |
| Understand the code before changing it | [Architecture](docs/architecture.md) (design rules, full repository map) |
| Know what is stored where | [Data](docs/data.md) |
| Call the HTTP API | [API](docs/api.md) |
| See why a value changed between runs | [Value changes](docs/value-changes.md) |
| Check for a known bug | [Known issues](docs/known-issues.md) |
| Work on the code as an AI agent | [AGENTS.md](AGENTS.md) |
| Set up the optional upload receiver | [server/README.md](server/README.md) |

## Quick start (macOS)

```bash
git clone https://github.com/vladbondarenko97/market-terminal.git
cd market-terminal
./setup.sh                 # Homebrew, Python, .venv, .env, server login service, menu bar icon
# Fill in .env (docs/configuration.md says what each key unlocks), then run ./setup.sh again

# First run: builds the database and sends nothing.
.venv/bin/python main_pipeline.py cme-login            # optional, needs a CME account (volume files need a login)
.venv/bin/python main_pipeline.py run --no-deliver     # add --skip-cme if you skipped the login
open http://localhost:8080                             # or the port set as OPTIONS_WHALE_PORT in .env
```

To run the pipeline automatically at the open and before the close, run `./setup.sh --schedule` on **one** Mac only.
`setup.sh` is safe to re-run; `./setup.sh --help` lists every option. Details: [Operations](docs/operations.md).

Tests need no network and no credentials:

```bash
.venv/bin/python -m unittest discover -s tests
```

## What you get

- **Three runs a day** on NYSE trading days (09:31, 12:30 and 15:45 ET; no afternoon run on 13:00 ET early closes) on the
  Mac that owns the schedule. Each run stores
  every provider response, computes the models and commits one immutable snapshot.
- **The terminal**, three tabs:
  - **Macro Direction**: VMRI macro risk index and War Room scenarios, COMEX inventory and paper:physical ratio,
    eBay Silver Eagle premiums, dealer gamma (GEX), dark-pool block flow, catalyst calendar, news sentiment, Fed
    liquidity, Shanghai–COMEX spread and the Engine Positions card.
  - **Time Arbitrage**: option analytics for a ticker: dealer positioning, IV vs realised volatility, term
    structure, a probability matrix and an option chain explorer with a Black-Scholes calculator.
  - **Forecast Lab**: eleven model cards for SPY or SLV: market-implied range, volatility forecast, trend/CTA,
    positioning (COT), silver fair value, macro regime, calendar effects, mechanical flows, a forecast scorecard,
    diesel & refining (with a Texas refinery outage tracker) and EIA inventories.
- **Execution engine and position tracking**: each run outputs a trade ticket (or CASH). Tickets are stored and
  marked at +1 day, +1 week and +2 weeks.
- **Delivery**: an email report, a phone push (ntfy) and an optional upload to your own web server. The exact email
  is saved before it is sent.
- **Replay**: any past run can be re-rendered from its snapshot with no network: `main_pipeline.py replay`.
- **Menu bar control**: a macOS menu bar icon shows whether the server is up and starts, stops or restarts it.

## How it fits together

```
 Data sources                Pipeline (main_pipeline.py)                          Outputs
 ────────────                ───────────────────────────                          ───────
 yfinance, Databento   ──►  collect   core/collect.py, core/sources.py, core/cme.py
 FRED, EIA, CFTC            │   every raw response stored, deduplicated by SHA-256
 CME (browser login)        │   models: core/metrics.py, core/forecast.py, core/refining.py
 eBay, iShares, TCEQ        ▼
                           snapshot  one immutable JSON document per run   ──►  CME_Data/portfolio.db
                            │
              ┌─────────────┼───────────────────────────┐
              ▼             ▼                           ▼
           render        deliver                     terminal
        core/render.py   send_email.py (email, ntfy)  options_whale/api_router.py
        visualize_volume upload_data.py               reads stored data; some panels
        → CME_Data/<date>/                            also fetch live
```

The rules that keep this trustworthy (the snapshot is the source of truth, raw data is kept, missing is never shown
as zero) are in [Architecture](docs/architecture.md#design-rules).

Data lives outside the repository in `CME_Data/`, next to the project folder unless `PORTFOLIO_DATA_DIR` says
otherwise. See [Configuration](docs/configuration.md#data-folder).

## Security

The terminal server has no authentication and listens on all network interfaces. Keep it on a private network (for
example Tailscale). Do not expose it to the internet.

## Platform

macOS. The CME browser login, the Mail import, the launchd services and the menu bar icon are Mac-specific. The
tests and offline runs (`main_pipeline.py run --offline`) also work on Linux.

## License

MIT. See [`LICENSE`](LICENSE).
