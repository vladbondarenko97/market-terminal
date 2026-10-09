# Data

How the project stores what it collects: the SQLite tables and views in `portfolio.db`, the snapshot, the legacy
ledgers, every file in the data folder and in each daily folder, and what the upload sends. For contributors who
read or write storage, and for operators who back up or move the data folder. For where the folder lives see
[Configuration](configuration.md#data-folder); for the commands that create these files see
[Operations](operations.md).

## Overview

The data folder (`CME_Data/`) lives outside the repository. Its main file is `portfolio.db`, one SQLite database
with two kinds of content:

- **Legacy ledger tables.** The original append-only history (`macro_master_ledger`, `comex_inventory_history`
  and others). Most also exist as CSV files next to the database. See [Legacy ledgers](#legacy-ledgers).
- **The v2 lake.** Eight `v2_*` tables, one view, two triggers and three indexes that keep every raw provider
  response, typed observations, run records and one immutable snapshot per run. See
  [SQLite: v2 lake](#sqlite-v2-lake).

The lake is additive. `lake.migrate()` in `core/lake.py` runs `CREATE ... IF NOT EXISTS` and `ALTER TABLE ADD
COLUMN`; it never drops or rewrites a table, a column or a row. The one exception is the view
`v2_latest_snapshot`, which holds no data: `migrate()` replaces it, atomically, when the definition stored in the
database differs from the one in the code. Every writer calls `migrate()` first, so the schema appears on first
use. All lake writes take one process-wide lock and use short transactions; none is held open across a network
call. The code never sets `journal_mode`: a new database uses SQLite's default rollback journal.

**Read-only reads.** `lake.connect_readonly(path)` opens the database with `mode=ro` and the same row factory as
`lake.connect()`. It cannot write or create anything: a missing file or a missing table raises
`sqlite3.OperationalError`. The terminal's `/api/positions` and `/api/forecast` use it, and `positions.list_live()`
and `forecast.scorecard()` run no DDL, so on a database no pipeline run has migrated yet they raise
`OperationalError` (the routes turn that into a 503) instead of creating tables from a GET request.

## SQLite: v2 lake

Defined in `core/lake.py` (`MIGRATIONS`). Timestamps are ISO-8601 text.

| Object | Kind | Holds | Written by |
|---|---|---|---|
| `v2_runs` | table | One row per run, including replays: `run_id`, `mode` (`live`, `offline`, `replay`), `trigger`, `status`, start and finish time, `run_folder`, `code_version` (git short hash, `-dirty` if modified), stage and source summaries, `artifacts_json` (file hashes, chart outcomes), `delivery_status` and `delivery_detail`, `error`, `parent_run_id` (replays) | `lake.create_run()`, `lake.update_run()` from `main_pipeline.py` (`run`, `replay`, `resend`) |
| `v2_payloads` | table | Raw captures, one row per unique content (`sha256` is unique, so identical responses are stored once). Content is zlib-compressed except `xlsx`, `png`, `zip`, `gzip`. Also `kind`, `source`, request, `source_date`, a parsed copy, coverage and `status` (`ok`, `invalid`, `rejected`). Credentials are removed from text formats before hashing. | `lake.store_payload()`: `SourceSession.fetch()` in `core/sources.py`, `acquire_cme()` in `core/collect.py`, `core/importer.py`, `main_pipeline.py run` (report text and `email.eml`) |
| `v2_fetch_log` | table | One row per provider request: source, request, elapsed time, `outcome` (`ok`, `error`, `login_required`, ...), HTTP status, `payload_id`, redacted detail | `lake.log_fetch()` |
| `v2_observations` | table | Typed facts: `metric_id`, `entity`, `dims_json`, `observed_at`, `value_num` / `value_text` / `value_json`, `unit`, `status`, `reason`, `source`, `payload_id`, `run_id`, `derivation`. `identity` (a hash of metric, entity, dimensions, time, source, value, status, derivation) is unique and inserts use `INSERT OR IGNORE`, so a repeated fact is stored once while a revised value at the same time is kept as a new row. | `lake.record_observations()`: the capture step of every run (`collect.context_observations()`), `acquire_cme()`, the importer, refinery event capture |
| `ix_v2_obs_metric`, `ix_v2_obs_run` | index | `v2_observations` on (`metric_id`, `entity`, `observed_at`) and on `run_id` | created by `migrate()` |
| `v2_snapshots` | table | One immutable report document per run: `context_json`, `context_sha256`, `schema_version` (`v2.1`). `run_id` is unique. | `lake.commit_snapshot()` |
| `ix_v2_snap_time` | index | `v2_snapshots(created_at)` | created by `migrate()` |
| `v2_snapshots_immutable` | trigger | Aborts any `UPDATE` on `v2_snapshots` (`v2_snapshots rows are immutable`). | created by `migrate()` |
| `v2_snapshots_no_delete` | trigger | Aborts any `DELETE` on `v2_snapshots` (`v2_snapshots rows cannot be deleted`). | created by `migrate()` |
| `v2_latest_snapshot` | view | One row: the newest snapshot of a **live** run whose status is `committed`, `completed` or `completed_with_warnings`. Only when no live run has such a snapshot does it fall back to the newest of any other mode (an offline run), so an offline test run never hides a live one. Ordered by (`mode = 'live'` first, then newest `created_at`). `lake.load_snapshot()`, `replay`, `resend`, `catalog`, the legacy wrapper scripts and the terminal read this. | created (or replaced when its definition changed) by `migrate()` |
| `v2_imports` | table | Log of imported files: `path`, `sha256`, size, `kind`, `parser_version`, `status`, rows. Unique on (`path`, `sha256`, `parser_version`), which makes `import-history` idempotent. | `importer._record_import()`; also `acquire_cme()` for files it archives |
| `v2_trade_signals` | table | One engine ticket per live run, including CASH: contract, entry bid/ask/last/mid, entry IV, volume, open interest, score, bias, `starred`, `deleted_at` (hidden, not removed), `source` (`engine_run` or `email_archive`). `run_id` is unique (`email:<Message-ID>` for recovered emails). | `positions.record_signal()` and `insert_signal()` (live runs; `scripts/import_email_positions.py`); `toggle_star()` and `stop_tracking()` from the terminal |
| `v2_forecasts` | table | One row per logged forecast (`run_id`, symbol, model, horizon, `target_date`, spot, `p_up`, 68% range, direction, vol). Unique on (`run_id`, `symbol`, `model`, `horizon`). Graded by `forecast.scorecard()` once the target date passes. | `forecast.record_forecasts()` (live runs only); the table itself is created by `migrate()` |

Nothing else in the project creates a `v2_*` object. Refinery outages, EIA series, option-position marks and the
SLV trust are not separate tables; they are observations and payloads (below).

Run `status` values: `running`, `committed` (snapshot stored, export pending), `completed`,
`completed_with_warnings`, `failed` (before the snapshot was committed), and `interrupted` (a run that was still
`running` when it was killed; the next run marks it). `delivery_status` values: `smtp_accepted`, `failed`,
`outcome_unknown`, `skipped` (email not configured), `not_requested` (`--no-deliver`, offline, replay) and
`attempting`. The manual helpers `download_volume.py`
and `update_inventory.py` store payloads under the pseudo run ids `manual-download` and `manual-inventory`, which
have no `v2_runs` row.

### The snapshot

`v2_snapshots.context_json` is the complete report context for one run. `collect.build_context()` assembles it,
`main_pipeline.run()` adds an `appendix` (the AlphaFlow text), `lake.commit_snapshot()` stores it, and the run then
reloads it so that renderers read only what was stored. NaN and infinity become `null`. Committing the same
`run_id` again with identical content is a no-op; with different content it raises an error.

Top-level sections: `run`, `calendar`, `inventory`, `cme_volume`, `prices`, `shanghai`, `ratios`, `macro`,
`vmri`, `paper_physical`, `oi_divergence`, `es_put_call`, `options`, `flow`, `technicals`, `weather`, `breadth`,
`execution`, `ebay`, `sources`, `stage_seconds`, the model-feature sections from `extended_features()`
(`price_stats`, `vol_regime`, `cme_positioning`, `silver_options_cme`, `inventory_trend`, `silver_basis`,
`regime_context`), `position_marks`, `forecast`, `refining` and `appendix`. A section that could not be built is
`{"status": "missing", "reason": "..."}`. The daily file `report_snapshot.json` is this document, indented.
`python main_pipeline.py catalog` lists the metrics with their paths.

### Observations and payloads

`v2_observations.metric_id` families:

| Family | Examples |
|---|---|
| Catalog metrics (`core/catalog.py METRICS`, one per report value) | `quote.SLV.price`, `flow.SLV.block_vwap`, `options.SPY.call_wall`, `fred.WALCL`, `sge.*`, `comex.*`, `ebay.*`, `execution.*`, `run.generated_at` |
| CME facts | `cme.<product>.volume`, `cme.<product>.open_interest`, `cme.asset_class.row`, `comex.silver.registered`, `.eligible`, `.total`, `.depository`, `.reconciliation` and the change and adjustment fields |
| Per-run records | `options.<SYM>.gex_profile`, `options.<SYM>.selection`, `flow.<SYM>.summary`, `trade.block` (one per block print), `execution.engine`, `vmri.components`, `ebay.listing`, `calendar.event` |
| Stored series and events | `refinery.event` (TCEQ filings, one per event id), `slv.ounces_in_trust`, `position.mark` (real mid-price of an open ticket contract, from the chains the run already downloaded) |
| Imported history | `legacy.<table>.row` (a legacy ledger row, status `historical`) |

`v2_payloads.kind` values: `ohlcv`, `option_expirations`, `option_chain`, `fred_series`, `fred_release_dates`,
`calendar_xml`, `spot_quote`, `sge_benchmark`, `trades`, `listing`, `cot`, `etf_page`, `fomc_calendar`,
`fund_info`, `eia_weekly`, `refinery_capacity`, `tceq_rss`, `tceq_event`; CME `volume_workbook`,
`inventory_workbook`, `*_workbook_invalid` and `*_rejected`; run output `daily_market_report` and `email_mime`;
imported files `ledger_csv`, the daily file name, `spy_wicks_1m`, `deployment_simulation`. The eBay OAuth token is
never stored (kind `auth`).

## Legacy ledgers

These tables predate v2 and keep their names and column layout. Live runs append to them from
`main_pipeline.export_ledgers()`, using the rows built by `render.ledger_rows()`. New columns are added to an
existing table with `ALTER TABLE ADD COLUMN`; if a table is missing it is created from the first row
(`lake.append_legacy_row()`).

| Ledger | CSV in the data folder | Appended | Columns |
|---|---|---|---|
| `macro_master_ledger` | `macro_master_ledger.csv` | 1 row per live run | `Datetime`, `VMRI_Score`, `Threat_Tier`, `DXY`, `DXY_Change`, `10Y_Yield`, `ZN_Futures`, `High_Yield_OAS`, `VIX`, `VIX_Change`, `WTI_Crude`, `Brent_Crude`, `Gold_Price`, `Gold_Silver_Ratio`, `SHFE_Silver_USD`, `COMEX_Silver`, `SHFE_Premium`, `GEX`, `DIX`, `Reverse_Repo_BN`, `Fed_Balance_Sheet_BN`, `Retail_Silver_Cheapest`, `Retail_Silver_Avg`, `Silver_OI`, `Paper_Physical_Ratio` (25). `GEX` is SPY's net dealer gamma from the run's snapshot (`options.SPY.gex.net_gex`: gamma x open interest x 100 x spot, in USD of delta change per $1 move, dealer-long-calls convention; the same figure as `institutional_ledger.Net_Gamma`), empty when the run could not compute it. Rows before the fix hold no `GEX`. `DIX` is always empty: no data source supplies it. |
| `equities_darkpool_gex_ledger` | `equities_darkpool_gex_ledger.csv` | 2 rows per live run (SPY, SLV) | `Date`, `Ticker`, `Spot_Price`, `DP_Sentiment`, `DP_Total_Vol`, `DP_Notional_USD`, `DP_Largest_Block`, `DP_VWAP`, `DP_Bull_Vol`, `DP_Bear_Vol`, `GEX_Call_Wall`, `GEX_Put_Wall`, `GEX_Zero_Gamma` |
| `institutional_ledger` | `institutional_ledger.csv` | 1 row (SPY) per live run | `Date`, `Ticker`, `Spot_Price`, `Net_Gamma`, `Max_Pain`, `Call_Wall`, `Put_Wall`, `DP_*` fields, `Tech_*` fields, `VIX_Spot`, `VIX_3M`, `VIX_Term_Struct`, `Breadth_Condition`, daily percent moves for SPY, RSP, NVDA, AAPL, MSFT |
| `physical_arbitrage_ledger` | `physical_arbitrage_ledger.csv` | 1 row per live run, only when eBay returned prices and a benchmark exists; also by `ebay.py` (below) | `Datetime`, `COMEX_Spot`, `Cheapest_Eagle`, `Average_Eagle`, `Cheapest_Premium_Dollars`, `Cheapest_Premium_Percent`, `Average_Premium_Dollars`, `Average_Premium_Percent`, `Dealers_Scanned` |
| `comex_inventory_history` | `comex_inventory_history.csv` | Only when a new silver workbook was downloaded and its (Registered, Eligible) pair is not already stored. `Date` is the report date. Troy ounces. | `Date`, `Registered`, `Eligible`, `Total`, `Reg_Change`, `Elig_Change`, `Total_Change` |
| `crypto_metrics_history` | **none** (SQLite only) | 1 row per live run if the date is not already present; `scripts/backfill_crypto_metals.py` adds a year of history | `Date`, `BTC_Price`, `Silver_Price`, `Gold_Price`, `Silver_BTC_Ratio`, `Gold_BTC_Ratio` |
| `daily_volume`, `silver_stocks` | none | never; read-only legacy tables | no current code creates or writes them |

Rules that hold for all of them:

- Only **live** runs append. `--offline` runs and `replay` never do. A live run with `--no-deliver` still appends.
- Timestamps in `Datetime` and `Date` are naive America/Chicago local time (`YYYY-MM-DD HH:MM`; the institutional
  ledger adds seconds; the crypto ledger is date-only).
- There is no de-duplication across runs except for the crypto ledger (by date) and the COMEX ledger (by value
  pair). Two live runs a day add two rows to the others.
- CSV and SQLite are separate copies. The CSV append keeps the header already in the file: a column it does not
  know is dropped from the CSV (the SQLite table keeps it), and a missing value is written `NaN` in the macro CSV
  and `N/A` in the others. SQLite stores missing values as `NULL`. The pipeline's own history (charts, Forecast
  Lab) reads the SQLite tables through `core/history.py`; the terminal's ledger routes and `deployment_engine.py`
  read the CSVs.
- `lake.PROTECTED_TABLES` lists the eight legacy tables. `sqlite_layer.replace_df()` refuses to replace any
  existing table.
- `import-history` copies every row of every existing legacy table into `v2_observations` as
  `legacy.<table>.row`, and imports the ledger CSVs as payloads. It leaves the tables unchanged.

The terminal's `POST /api/silver_eagle_prices` route (POST only, because it writes) runs `ebay.py` as a subprocess,
and `ebay.py` appends a row to `physical_arbitrage_ledger` (CSV and SQLite) on every scan. This happens outside the
pipeline, so a scan adds a row even when no run did.

## Data folder layout

```
CME_Data/
├── portfolio.db                      SQLite: legacy ledgers + v2 lake
├── .v2_run.lock                      lock file; locked while a run is active
├── .v2_run_status.json               current stage of the running or last run
├── state.json                        CME session cookies (portable backup)
├── .cme_browser_profile/             persistent Chromium profile for CME logins
├── daily_volume_YYYYMMDD.xlsx        archived CME volume workbooks
├── silver_stocks_YYYY-MM-DD.xls      archived COMEX inventory workbooks
├── _rejected_downloads/              downloads that failed validation
├── backups/                          portfolio-<UTC time>.db copies made before import-history
├── macro_master_ledger.csv  ...      the five ledger CSVs
└── Sep-29-26/                        one folder per Chicago day (see Daily folder)
```

| Item | What it is | Written by |
|---|---|---|
| `.v2_run.lock` | `fcntl` lock file. The file stays on disk and keeps the last process id; what matters is whether the lock is held. A second run finds it held and exits with code 75. | `RunLock` in `core/runlock.py` |
| `.v2_run_status.json` | Run id, stage, state, start and finish time; read by `main_pipeline.py status` and by a second run that finds the lock busy. Written atomically (temp file `..v2_run_status.json.tmp`). | `set_status()` |
| `state.json` | Cookies from the CME session. **Sensitive.** Refreshed at the end of every CME listing fetch and after a login. Never imported into the database and never uploaded. | `cme.save_session()` |
| `.cme_browser_profile/` | Chromium profile (cookies, local storage, the Duo "remember me" data). **Sensitive.** Ignored by the importer because the name starts with a dot. | `cme.open_persistent_context()` |
| `daily_volume_YYYYMMDD.xlsx` | CME volume workbook, named by the **trade date inside the file** (CME's own file name uses the next business day). If an older file already has that name with different content, the new one is saved as `daily_volume_YYYYMMDD_r<8 hex>.xlsx`. Never overwritten. | `acquire_cme()` in `core/collect.py` |
| `silver_stocks_YYYY-MM-DD.xls` (or `.xlsx`) | COMEX silver inventory workbook, named by its report date | `acquire_cme()` |
| `.v2_manual_run.log` | stdout and stderr of runs started by the terminal's Run button, appended | `POST /run` in `options_whale/api_router.py` |
| `backups/portfolio-<UTC time>.db` | Consistent copy of `portfolio.db` (SQLite backup API plus an integrity check) taken before `import-history`, unless `--no-backup` | `main_pipeline.backup_database_file()` |
| `_rejected_downloads/<kind>_<UTC time>.bin` | A download that was not a valid workbook (for example a login page), kept as evidence | `acquire_cme()` |
| `<ledger>.csv` | The five CSV ledgers above | `main_pipeline._csv_append()`, `ebay.py` |
| `<Mon-DD-YY>/` | Output of the runs of one day | `main_pipeline.render_outputs()` |
| `backup macro_master_ledger copy.csv` | A manual copy from the old setup, if present; only the importer reads it | none |

Daily-folder files, the status file and archived workbooks are written to a temp file and moved into place with
`os.replace`, so a reader never sees a half-written file. Temp names begin with `.` or end in `.tmp`. The ledger
CSVs and `portfolio.db` are appended in place.

`import-history` (`core/importer.py`) reads the root workbooks, the ledger CSVs, the legacy tables and each daily
folder's `.txt` and `.xml` files and `master_market_data.csv`. It skips `state.json`, `.env`, hidden items,
`*.db`, PNGs, the rendered HTML, JSON and `email.eml` files, the rendered `daily_market_report.txt` (checked
before the generic `.txt` rule) and the `offline_<run_id>/` and `replay_<run_id>/` folders inside a daily folder.
It also reads `spy_wicks_1m.json`, `deployment_payload.json` and `deployment_state.json` from the repository root.

The terminal's `/api/dump` route (`dump_data()` in `options_whale/api_router.py`) picks the newest daily folder by
the date in its name; only folders named like `Sep-29-26` count, so `.cme_browser_profile`, `_rejected_downloads`
and `backups` are never chosen.

## Daily folder

`CME_Data/<Mon-DD-YY>/` is named for the America/Chicago date of the run (`config.run_folder_name()`), for example
`Sep-29-26`. The morning and afternoon runs of one day **share one folder**. Each file is replaced when a later run
writes it, so after the second run the folder holds that run's files only. Older versions of a report are not lost:
the report text, `email.eml` and the whole snapshot of every run are stored in the database
(`v2_payloads`, `v2_snapshots`).

`main_pipeline.render_outputs()` writes the folder. It is the same function for runs and replays.

| File | What it is | Written by |
|---|---|---|
| `tactical_ruling.txt` | Tactical ruling XML (legacy nodes plus an appended `<model_features>` block) | `render.tactical_xml()` |
| `volume_dashboard.txt` | Dashboard XML wrapping the tactical ruling. The upload sends it as `volume_dashboard.xml`. | `render.dashboard_xml()` |
| `volume_dashboard.html` | The HTML dashboard, filled from the `volume_dashboard.html` template in the repository; contains the copyable report box | `render.dashboard_html()` |
| `master_market_data.csv` | CME volume and open interest per product and date: `Product`, `Type`, `Volume`, `Open_Interest`, `Date`, `OI_Change` | `render.master_market_csv()` |
| `daily_market_report.txt` | The exact email body | `render.full_report()` |
| `email.eml` | The complete MIME message, saved before any SMTP attempt | `send_email.save_eml()` |
| `report_snapshot.json` | The run's snapshot, indented | `lake.dumps()` |
| `run_manifest.json` | Run id, output folder, SHA-256 of each file, outcome of each chart | `render_outputs()` |
| `chart*.png` (up to 18) | `chart1_es_conviction_{7d,30d}`, `chart2_si_conviction_{7d,30d}`, `chart3_silver_divergence_{7d,30d}`, `chart4_spy_options_flow_{7d,30d}`, `chart5_macro_10y_yields_{7d,30d}`, `chart6_comex_inventory_30d`, `chart7_crypto_ratios_{30d,1y}`, `chart8_metals_price_{30d,1y}`, `chart9_es_divergence_30d`, `chart10_yield_contagion_30d`, `chart11_physical_squeeze_30d` | `visualize_volume.render_charts()` |
| `replay_<run_id>/` | A full set of the files above from `main_pipeline.py replay` (no `--out`). It keeps replays out of the shared day files. | `replay()` |
| `offline_<run_id>/` | The full set of files from an `--offline` run. An offline run never writes the shared day files. | `run()` |

- A chart is written only when its inputs exist. Charts are never deleted: if a chart fails in a later run, the PNG
  from the earlier run stays in the folder. `run_manifest.json` records what the latest run produced, and the
  email and dashboard use only charts the manifest marks `generated` or `stale_input`.
- Offline runs (`--offline`) write to `offline_<run_id>/` inside the day folder, so they never replace the day's
  files. Their snapshot is committed like any other, but `v2_latest_snapshot` prefers the newest live run, so the
  terminal keeps showing the last live run and an offline run is shown only while no live snapshot exists.
- `dump_data.py` (a legacy wrapper) rewrites only `volume_dashboard.txt`, in the folder named by the latest
  snapshot.
- During a write you may see `.<name>.tmp`, `email.eml.tmp` or `<chart>.png.tmp.png`.

## What gets uploaded

Uploads run only for delivered runs (not `--no-deliver`, `--no-upload` or `--offline`), through `upload_data.py`,
to the receiver described in [server/README.md](../server/README.md). Three things can be sent:

| Item | Sent as | When |
|---|---|---|
| A **slim copy** of `portfolio.db` | form field `portfolio`, file name `portfolio.db` | every upload |
| `volume_dashboard.txt` | form field `dashboard`, file name `volume_dashboard.xml` (the file is the XML; the name is what the receiver accepts) | every upload, if the file exists |
| The daily report text | form field `report`, file name `market-report-YYYY-MM-DD-HHMMZ.txt` | only with `REPORT_UPLOAD=1`; the returned permanent link goes into the phone push |

`slim_db_copy()` makes the copy with SQLite's online `backup()` API into a temp file, then drops every `v2_*`
view, trigger and table (their indexes go with the tables) and runs `VACUUM`. The result holds the legacy tables
only, so the server never receives the raw-payload archive, the snapshots or the engine tickets. The running
database is opened read-only and is not changed. Because `backup()` produces a consistent copy, the file is
complete whatever the journal mode is. The temp file is deleted afterwards.

`upload_files()` checks the URL and token first and builds no copy when they are missing: neither set reports
`skipped`, only one set reports `failed`. It reports `uploaded` only when the receiver's JSON reply says `ok` for
every file sent (`check_reply()`); an HTTP 200 alone is not enough.

## Outside CME_Data

| Path | What | Notes |
|---|---|---|
| `alphaflow/alphaflow.db` | Table `swing_plays` for AlphaFlow | Gitignored. `alphaflow/engine.py` creates the table only if it is missing (`CREATE TABLE IF NOT EXISTS`), so importing it keeps earlier results; a scan replaces the rows only after its query succeeds. `send_email.alphaflow_appendix()` reads it read-only for the email appendix. |
| `deployment_state.json`, `deployment_payload.json`, `deployment_payload.js` | Input and output of `deployment_engine.py` | Repository root, gitignored. A value the script cannot read or compute is `null` in the payload, with the reason under a `missing` object; there are no placeholder numbers. |
| `spy_wicks_1m.json` | Five days of SPY one-minute candles written by `scripts/dump_spy_wicks.py` into the project root | Gitignored. `import-history` captures it if it is in the repository root. |
| `.env`, `.venv/`, `index.keys.js` | Settings, virtual environment, browser keys | Gitignored. |
| `~/Library/Logs/optionswhale.log`, `~/Library/Logs/marketdashboard.log` | Terminal and schedule logs | See [Operations](operations.md). |
| `portfolio_upload_*.db` in the system temp folder | The slim upload copy while an upload runs | Removed afterwards. |

## Backups

The pipeline makes **no scheduled local backup**. `main_pipeline.py import-history` copies `portfolio.db` to
`backups/portfolio-<UTC time>.db` first (`lake.backup_database()`: a consistent copy with an integrity check)
unless you pass `--no-backup`; if that copy fails, the import does not start. Nothing else calls it. The receiver on your web host keeps timestamped copies of each uploaded
`portfolio.db` (legacy tables only), which cannot restore the `v2_*` tables; see [server/README.md](../server/README.md).

Before an import, a risky change or a move to another Mac, copy the whole `CME_Data` folder by hand while no run is
active (`python main_pipeline.py status` shows the current run). For a
consistent copy of the database alone, use SQLite's own tool:

```bash
sqlite3 CME_Data/portfolio.db ".backup 'portfolio-copy.db'"
```

`state.json` and `.cme_browser_profile/` hold a live CME login. Copy them only to machines you trust.
