# AGENTS.md — working on this repository

Instructions for AI coding agents (Claude Code, Codex, Cursor and others) and for humans who want the short version.
`CLAUDE.md` imports this file. Read this first, then [docs/architecture.md](docs/architecture.md) before any
non-trivial change.

## What this is

A personal market-data system for one operator. A batch **pipeline** (`main_pipeline.py`) collects market data twice
a day, stores every raw response in SQLite, computes models and commits one immutable **snapshot** per run, then
renders files and sends an email and a phone push. A Flask **terminal** (`options_whale/api_router.py`) shows the
results in a browser. Target platform is macOS. The tests and offline runs also work on Linux.

## Set up a working copy

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # Playwright browsers are not needed for tests or offline runs
```

Do not run `./setup.sh` in a sandbox. It is macOS-only and installs launchd services.

## Commands

| Purpose | Command |
|---|---|
| Run the tests (offline, about 20 s) | `.venv/bin/python -m unittest discover -s tests` |
| End-to-end smoke test, no network, scratch data | `mkdir -p /tmp/mt && PORTFOLIO_DATA_DIR=/tmp/mt .venv/bin/python main_pipeline.py run --offline` |
| Re-render the latest run from its snapshot | `PORTFOLIO_DATA_DIR=/tmp/mt .venv/bin/python main_pipeline.py replay` |
| Recent runs and their status | `PORTFOLIO_DATA_DIR=/tmp/mt .venv/bin/python main_pipeline.py status` |
| Syntax-check the terminal JavaScript | `for f in options_whale/static/*.js; do node --check "$f"; done` |
| Exercise server routes without starting it | see [Testing the server](#testing-the-server) |
| CLI help | `.venv/bin/python main_pipeline.py --help` and `... run --help` |

Always set `PORTFOLIO_DATA_DIR` to a scratch folder when you run anything outside the test suite. Without it,
`import config` resolves (and may create) a real `CME_Data` folder next to the repository, and a live run writes
to the operator's database.

## Hard rules

Breaking one of these is a bug, even if the tests pass.

1. **Never send or publish anything.** Do not run `main_pipeline.py run` without `--offline` or `--no-deliver`. Do
   not run `resend`, `ntfy-test`, `upload_data.py` or `send_email.py`. They email, push to a phone or upload to a web
   server.
2. **Never commit secrets.** `.env`, `index.keys.js`, `state.json` and anything under `CME_Data/` stay out of git.
   Read settings through `config.py`; never hardcode a path, key, token, phone number or email address.
3. **Missing is not zero.** A model or source without inputs returns `{"status": "missing", "reason": "..."}`. Never
   add a fallback constant, a placeholder number or a silent default for market data.
4. **Snapshots are immutable and the lake is additive.** Do not edit stored snapshots. Do not drop, rename or delete
   tables, columns or rows in `portfolio.db`. Schema changes are `CREATE ... IF NOT EXISTS` or `ADD COLUMN` in
   `core/lake.py` (views hold no data; `migrate()` may replace one, as it does `v2_latest_snapshot`).
5. **Models run in the pipeline.** New computation goes in `core/` and lands in the snapshot. New `/api/*` routes
   read stored data. Some existing routes compute live; do not copy that pattern.
6. **Pipeline network calls go through `SourceSession.fetch()`** (`core/sources.py`), so they are deduplicated,
   bounded, logged, captured and switched off in offline runs.
7. **Tests stay offline.** No network, no credentials, no real data folder. Use `tests/fixtures/` or synthetic
   series, as the existing tests do.
8. **Do not work around the run lock** (exit code 75) or the scheduled-run gate (`SCHEDULED_RUNS`).
9. **When a computed value changes meaning, record it**: bump the model's version constant and add a line to
   [docs/value-changes.md](docs/value-changes.md).
10. **Do not run `scripts/` without reading them first.** They are one-off maintenance tools that call providers or
    write to the database (`scripts/dump_spy_wicks.py` writes `spy_wicks_1m.json` to the repository root).

## Where to make a change

| Task | Files |
|---|---|
| New data source | `core/sources.py` (fetcher via `SourceSession.fetch()`), `core/collect.py` (call it, record errors) |
| New or changed model | `core/metrics.py`, `core/forecast.py` or `core/refining.py`; wire it in `core/collect.py` |
| New Forecast Lab card | Follow [docs/forecast-lab.md](docs/forecast-lab.md#adding-a-forecast-lab-card) step by step |
| Report text, XML, email, HTML dashboard | `core/render.py`; charts in `visualize_volume.py` and `core/catalog.py` (`CHARTS`) |
| Storage or schema | `core/lake.py` (additive only) |
| Delivery | `send_email.py`, `upload_data.py`, the delivery block in `main_pipeline.run()` |
| Scheduling, trading calendar | `core/market_calendar.py`, `setup.sh` (`--schedule`), `run_dashboard.command` |
| Terminal route | `options_whale/api_router.py` |
| Terminal UI | `options_whale/templates/terminal.html`, `options_whale/static/app.js`, `options_whale/static/forecast.js` |
| Settings | `config.py` and `.env.example`, documented in [docs/configuration.md](docs/configuration.md) |
| Engine tickets and marks | `core/positions.py` |

Leave the legacy compatibility scripts (`tactical_ruling.py`, `dump_data.py`, `market_reader.py`,
`institutional_scanner.py`, `parse_volume.py`, `update_inventory.py`) as thin wrappers. Standalone tools
(`alphaflow/`, `deployment_engine.py`, `index.html`) are outside
the pipeline; do not wire them in. The full list is in
[docs/architecture.md](docs/architecture.md#repository-map).

## Testing the server

Import `options_whale/api_router.py` with a scratch data folder and use Flask's test client (no key is needed; the
dark pool route answers 503 without one). Routes that read stored data work offline; live routes call yfinance, Databento or eBay, so
avoid them in automated checks.

```bash
PORTFOLIO_DATA_DIR=/tmp/mt .venv/bin/python -c "
import sys; sys.path.insert(0, 'options_whale')
import api_router
c = api_router.app.test_client()
for url in ['/', '/help', '/api/positions', '/api/forecast?ticker=SPY']:
    print(url, c.get(url).status_code)"
```

Which routes are stored and which are live is listed in [docs/api.md](docs/api.md).

## Before you commit

1. `.venv/bin/python -m unittest discover -s tests` passes.
2. For pipeline changes: an offline run in a scratch folder completes, and `replay` re-renders it.
3. For JavaScript changes: `node --check` passes on each file (one file per call). If you can, load the page and check the browser console.
4. `git diff` has no `.env` values, keys, tokens, personal data or absolute paths from your machine.
5. The docs still match the code. Update the page that owns the topic:

| You changed | Update |
|---|---|
| A CLI command or flag, `setup.sh`, the schedule, delivery | [docs/operations.md](docs/operations.md) |
| An environment variable or `config.py` | [docs/configuration.md](docs/configuration.md) and `.env.example` |
| A table, a snapshot key, a file in the data folder | [docs/data.md](docs/data.md) |
| An HTTP route (path, params, response, stored vs live) | [docs/api.md](docs/api.md) and the `/help` text in `api_router.py` |
| A terminal panel or control | [docs/terminal.md](docs/terminal.md) |
| A Forecast Lab card or the status contract | [docs/forecast-lab.md](docs/forecast-lab.md) |
| How a stored value is computed | [docs/value-changes.md](docs/value-changes.md) and the version constant |
| A file's role, or a new file | [docs/architecture.md](docs/architecture.md#repository-map) |
| Fixed or found a bug | [docs/known-issues.md](docs/known-issues.md) |

## Gotchas

- `import config` loads `.env` and resolves the data folder at import time. Set `PORTFOLIO_DATA_DIR` **before** the
  import (the tests do this at the top of `tests/test_v2.py`).
- The data folder must exist before a run (`config.ensure_data_dir()`); create the scratch folder first.
- Run folders and report dates use Chicago time; the schedule gate uses New York time.
- `./run_dashboard.command` with no argument is a **scheduled** run and is skipped unless `SCHEDULED_RUNS=1`.
- Both daily runs write into the same `CME_Data/<Mon-DD-YY>/` folder; the later run overwrites the earlier files.
  Per-run copies live in the lake.
- An offline run commits a snapshot like any other and becomes the latest one the terminal shows. Use a scratch
  folder.
- Many terminal routes return HTTP 200 with `{"status": "error"}` on failure. Check the body, not only the code.
- `POST /api/silver_eagle_prices` writes a ledger row (it runs `ebay.py`), and `POST /run` starts a real pipeline
  run. Do not call either in checks.
- Known bugs are listed in [docs/known-issues.md](docs/known-issues.md). Check there before "fixing" something
  surprising, and keep fixes out of unrelated changes.
