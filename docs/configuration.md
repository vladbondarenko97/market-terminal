# Configuration

Every setting the project reads: where the data folder is, every variable in `.env`, the keys for the static
`index.html` page, the fixed limits in code, and the one optional dependency that `requirements.txt` does not
install. For operators setting up a Mac and for contributors adding a setting. For commands see
[Operations](operations.md); for what ends up in the data folder see [Data](data.md).

## How settings are loaded

- `config.py` is the single reader. It loads `.env` from the repository root with python-dotenv when the
  package is installed (it is in `requirements.txt`), then exposes constants such as `config.FRED_API_KEY`.
  If python-dotenv is missing, `.env` is silently ignored.
- A real environment variable wins over `.env` (python-dotenv does not override).
- `NAME=` with nothing after it sets the variable to the empty string. For keys and URLs, empty means "not
  configured": the feature is skipped or the source reports `missing`. The exceptions are called out below.
- Flags that are compared with `1` (`SCHEDULED_RUNS`, `REPORT_UPLOAD`, `ALPHAFLOW_DEBUG`) need exactly `1`.
- `setup.sh` creates `.env` from [`.env.example`](../.env.example) and sets its mode to 600. `.env` is never
  committed (`.gitignore`).
- Settings are read once at start. After editing `.env`, restart the terminal (menu bar **Restart**); the pipeline
  picks the change up on its next run.
- Credentials are removed from stored error messages and captured text (`lake.redact()` in `core/lake.py`
  masks `api_key=`, `token=`, `password=` and similar `name=value` pairs). Treat `NTFY_URL` as a secret too:
  anyone who knows the topic URL can read and post to it.

## Data folder

The data folder (`CME_Data/`) holds `portfolio.db`, the daily output folders, the ledger CSVs and the CME
session. `config.py` resolves it once, when it is first imported, in this order:

1. **`PORTFOLIO_DATA_DIR`**, when set and not empty. The value is expanded (`~` works) and made absolute. It is
   used as given: nothing checks at import that it exists.
2. Otherwise two candidates are compared: the **sibling** `<repository>/../CME_Data` (the historical default) and
   `~/Desktop/CME_Data`.
   - If they are different folders and **both** contain a `portfolio.db`, import fails with `ConfigError: Two
     CME_Data installations found`. Every script that imports `config` stops. Set `PORTFOLIO_DATA_DIR` to the one
     you want.
   - If the sibling folder does **not exist** and the Desktop folder has a `portfolio.db`, the Desktop folder is
     used.
   - Otherwise the sibling is used. An existing but empty sibling wins over a Desktop installation.
3. **Side effect on import:** when the folder that was chosen is the sibling and it does not exist, importing
   `config` creates it (empty). An explicit `PORTFOLIO_DATA_DIR` that does not exist is not created. The pipeline
   then stops with `ConfigError: Data directory ... does not exist` (`config.ensure_data_dir()`, called by
   `main_pipeline.py run`). `setup.sh` creates the folder itself with `mkdir -p`.

On a Mac that could hold two installations, pin the folder in `.env` and leave it there:

```
PORTFOLIO_DATA_DIR=/Users/you/CME_Data
```

Derived values, all from `config.py`:

| Name | Value |
|---|---|
| `DATA_DIR` | the folder above |
| `DB_PATH` | `DATA_DIR/portfolio.db` |
| `daily_dir()` | `DATA_DIR/<Mon-DD-YY>`, the day in America/Chicago (`config.TZ_NAME`), for example `Sep-29-26` |
| `PROJECT_ROOT` | the repository folder (also holds `.env`) |

## Environment variables

Each table lists the name, the default or alias, what reads it, and what happens when it is empty. "Pipeline"
means `main_pipeline.py`; "terminal" means `options_whale/api_router.py`.

### Market data keys

| Variable | Default / alias | Read by | When empty |
|---|---|---|---|
| `DATABENTO_API_KEY` | `""`. Legacy alias: `DB_API_KEY` | `config.DATABENTO_API_KEY` for the pipeline (`core/collect.py`, `core/sources.py databento_trades()`); `options_whale/api_router.py` at import; `setup.sh`; `alphaflow/engine.py` | Pipeline: the SPY and SLV block-trade flow sections report `missing`; the run continues. **Terminal: the server does not start** (it raises `EnvironmentError` at import). `setup.sh` installs the login service but does not start it. |
| `FRED_API_KEY` | `""` | `core/collect.py` (macro series, Forecast Lab inputs, CPI dates) | Every FRED series reports `missing`. This includes the high-yield spread, so **VMRI shows `INCOMPLETE DATA`** (its credit input comes from FRED). Forecast Lab cards 5 and 6 lose their FRED inputs, and card 7 loses the CPI dates. |
| `EIA_API_KEY` | `""` | `core/sources.py eia_weekly()` | The public EIA `.xls` history file is used instead. Nothing is lost. |
| `GOLD_API_KEY` | `""` | `core/collect.py _spot()` (goldapi.io spot silver) | `prices.silver_spot` reports `missing`; the futures-minus-spot basis is empty. |
| `EBAY_APP_ID`, `EBAY_CERT_ID` | `""` | `core/collect.py collect_ebay()`; `ebay.py` (run by the terminal's `/api/silver_eagle_prices`) | Silver Eagle listings report `missing`. Both are needed. `ebay.py` exits with "Cannot proceed without a valid eBay API token". |
| `ALPHA_VANTAGE_KEY` | `""` | defined in `config.py`, **read by nothing** | No effect. Unused; safe to leave out of `.env`. |

**Databento key.** Fill `DATABENTO_API_KEY`. `DB_API_KEY` is the name an older version used; leave it blank.
The two names are not treated the same way:

- `config.optional_env()` uses the alias only when the primary variable is *not set at all*.
- `config.required_env()` also uses the alias when the primary is set but *blank*.

A `.env` copied from the template has `DATABENTO_API_KEY=` blank. If only `DB_API_KEY` is filled, the terminal
accepts it (it uses `required_env`), but `config.DATABENTO_API_KEY` stays empty, so the pipeline sees no key and
`setup.sh` does not start the server. Known issue: the alias works in `required_env` only. See
[Known issues](known-issues.md).

### CME login

| Variable | Default | Read by | When empty |
|---|---|---|---|
| `CME_LOGIN_USERNAME`, `CME_LOGIN_PASSWORD` | `""` | `core/cme.py wait_for_login()` via `config.CME_LOGIN_*` (runs, and `main_pipeline.py cme-login`) | The CME login page opens with empty fields and you type them. These only pre-fill the form; you always finish the login and the MFA step yourself. |

### Email

Email is sent with `send_email.py deliver()`: plain SMTP with STARTTLS on `SMTP_PORT` (587 works; an implicit-SSL
port such as 465 does not). The message is always saved as `email.eml` first.

| Variable | Default | Read by | When empty |
|---|---|---|---|
| `EMAIL_SENDER` | `""` | `send_email.py` (login and From header) | Email is not sent: status `failed`, detail `EMAIL_SENDER/EMAIL_PASSWORD not configured`. The run ends `completed_with_warnings`. |
| `EMAIL_PASSWORD` | `""` | `send_email.py` | Same as above. Use an app password if your provider offers one. |
| `SMTP_SERVER` | `""` | `send_email.py` | Email is not sent (`SMTP_SERVER/RECIPIENT_EMAIL not configured`). |
| `SMTP_PORT` | `587` | `config.py` as `int(...)` | **Do not leave it blank.** `SMTP_PORT=` makes `import config` fail with `ValueError`, which stops every script. Delete the line or keep `587`. Known issue: no fallback for a blank value. See [Known issues](known-issues.md). |
| `RECIPIENT_EMAIL` | `""` | `send_email.py` (To header) | Email is not sent. |
| `REPORT_SENDER` | `EMAIL_SENDER` | `scripts/import_email_positions.py` only | The script uses `EMAIL_SENDER`. If both are empty it exits and asks you to set one. |

### Phone push and dashboard link

| Variable | Default | Read by | When empty |
|---|---|---|---|
| `NTFY_URL` | `""` | `send_email.py` (daily brief, refinery alerts, CME login alert). `options_whale/api_router.py` imports it, but only dead code uses it. | No pushes are sent. Event alerts are recorded as `skipped` (`NTFY_URL not configured`); the daily brief is recorded as `failed` (known issue, see [Known issues](known-issues.md)). Use the full topic URL, for example `https://ntfy.sh/<topic>`. |
| `DASHBOARD_URL` | `""` | `main_pipeline.py` through `config.optional_env` | The daily push has no **Dashboard** button. Set it to the address of your terminal that your phone can reach (for example a Tailscale address). |

### Upload

Uploads go to the receiver described in [server/README.md](../server/README.md). Only delivered runs upload:
`--no-deliver`, `--no-upload` and `--offline` skip it.

| Variable | Default | Read by | When empty |
|---|---|---|---|
| `UPLOAD_URL` | `""` | `upload_data.py` (its own read; `config.UPLOAD_URL` exists but is unused) | Nothing is uploaded. `upload_files()` returns `failed` with `UPLOAD_URL/UPLOAD_TOKEN not configured in .env`. This is recorded in `v2_runs.delivery_detail` and does not add a run warning. |
| `UPLOAD_TOKEN` | `""` | `upload_data.py` | Same. Must equal the token in `/etc/portfolio-upload/token` on the server. |
| `REPORT_UPLOAD` | `""` | `upload_data.py upload_report()` | Set to exactly `1` to also upload the daily report under an unguessable name, so the push can carry a permanent **Full report** link. Needs `UPLOAD_URL` and `UPLOAD_TOKEN`. |

### Per-machine settings

| Variable | Default | Read by | When empty |
|---|---|---|---|
| `PORTFOLIO_DATA_DIR` | see [Data folder](#data-folder) | `config.py` | The sibling `CME_Data` (or the Desktop fallback) is used. |
| `OPTIONS_WHALE_PORT` | `8080` | `options_whale/api_router.py` (`os.environ`); `setup.sh` and `menubar/optionswhale.10s.sh` (parse `.env` with `sed`) | Empty means 8080. **Write it unquoted** (`OPTIONS_WHALE_PORT=9090`): the shell scripts only match digits right after `=`, so a quoted value would be read as 8080 by them and as 9090 by Python. |
| `SCHEDULED_RUNS` | `""` | `config.SCHEDULED_RUNS`; `main_pipeline.py run`; written by `setup.sh` | Must be exactly `1` on the one Mac that owns the schedule. With any other value, runs started with `--trigger scheduled` print a skip message and exit 0. Manual runs ignore it. `setup.sh --schedule` sets it to `1`; `--remove-schedule` and `--uninstall` blank it. `run_dashboard.command` with no argument uses the `scheduled` trigger. |

### Standalone tools

AlphaFlow (`alphaflow/`) is a separate app. It reads the same `.env` through its own code.

| Variable | Default | Read by | When empty |
|---|---|---|---|
| `ALPHAFLOW_DEBUG` | unset | `alphaflow/server.py` | Must be exactly `1` to turn on Flask debug mode, which also limits the server to `127.0.0.1`. Otherwise it listens on all interfaces, port 5001. Not in the template as an active line. |
| `DATABENTO_API_KEY` | | `alphaflow/engine.py`, with `os.getenv` | Prints "Valid DATABENTO_API_KEY not found in .env, simulating..." and returns no trades. The `DB_API_KEY` alias is not honoured here. |

## Browser keys for index.html

`index.html` is a static page that calls price APIs from the browser. It cannot read `.env`. Copy
`index.keys.example.js` to `index.keys.js` (ignored by git) and fill in:

```js
window.DASHBOARD_KEYS = { coinGecko: '', gold: '', finnhub: '' };
```

| Key | Provider |
|---|---|
| `coinGecko` | coingecko.com demo API key |
| `gold` | goldapi.io access token (separate from `GOLD_API_KEY` in `.env`) |
| `finnhub` | finnhub.io API key |

These values are visible to anyone who can open the page, and the page sends them in request URLs and headers.
Use free or demo keys. If `index.keys.js` is missing, the page still loads but sends empty keys and the providers
reject those calls.

## Fixed limits

These are constants in code, not settings. Change them in code.

| Constant | Value | Meaning |
|---|---|---|
| `config.MAX_PROVIDER_CONCURRENCY` | 3 | Worker threads for the top-level collection sections in `build_context()` (`core/collect.py`) |
| `config.CME_MAX_ATTEMPTS_PER_URL` | 2 | Attempts per URL in `cme.fetch_bounded()` (plain download of the inventory workbook) |
| `config.CME_429_COOLDOWN_SECONDS` | 20 | One wait after an HTTP 429 from CME |
| `config.CME_BACKFILL_MAX_ATTEMPTS` | 40 | Hard cap on volume workbooks fetched per run or per `download_volume.py` call. `--cme-max-files` defaults to 10 and is clamped to this. |
| `config.TZ_NAME`, `RUN_FOLDER_FORMAT` | `America/Chicago`, `%b-%d-%y` | Time zone for run folders and report dates; daily folder name |

`PROVIDER_LIMITS` in `core/sources.py` caps simultaneous requests per provider inside one run
(`SourceSession.fetch()`):

| Provider | Limit |
|---|---|
| `eia` | 5 |
| `tceq`, `cftc`, `yahoo`, `fred` | 2 each |
| `forexfactory`, `databento`, `goldapi`, `akshare`, `ebay`, `cme` | 1 each |
| any provider not listed (`ishares`, `federalreserve`) | none in practice: `fetch()` builds a new default semaphore on every call, so nothing is shared (known issue, see [Known issues](known-issues.md)) |

Other bounds: `core/api_client.py` (used only by `ebay.py`) uses a 15 s timeout and 3 retries with backoff on
429 and 5xx; `EIA_API_LENGTH` in `core/sources.py` is 5000 rows (the full weekly history).

## Optional dependency

**akshare.** `core/sources.py sge_silver_benchmark()` imports `akshare` inside the fetch to read the Shanghai
Gold Exchange (SGE) silver benchmark. `akshare` is **not** in `requirements.txt`, so `setup.sh` does not install
it. Known issue: missing dependency. See [Known issues](known-issues.md).

What happens without it (checked by running the collector without the package):

1. The `ImportError` (`ModuleNotFoundError: No module named 'akshare'`) is raised inside `SourceSession.fetch()`.
   `fetch()` catches any exception from the provider call, writes a `v2_fetch_log` row (source `akshare`,
   outcome `error`, that message as detail), records it in the session's error list, and re-raises it as
   `SourceUnavailable`.
2. `collect_shanghai()` catches that and returns `{"status": "missing", "reason": "akshare: error
   ModuleNotFoundError: No module named 'akshare'"}`. The run does not stop.
3. The Shanghai–COMEX spread card shows the reason. The ledger columns `SHFE_Silver_USD` and `SHFE_Premium` are
   written empty. Nothing else depends on it.
4. Every run tries the import again, so installing the package fixes it on the next run:

```bash
.venv/bin/pip install akshare
```

## Rules

- Read settings in `config.py` only. Add a constant there with `optional_env("NAME", default="")`, add the
  variable to `.env.example` with a comment, and add a row to the table above. Other code imports the constant.
- Never hardcode a data path or a key. Use `config.DATA_DIR`, `config.DB_PATH` and `config.daily_dir()`.
- A missing optional key must give `status: "missing"` with a reason, never a made-up value.

Known issues (places that break the first rule today). See [Known issues](known-issues.md).

| Where | What it does instead |
|---|---|
| `alphaflow/engine.py` | Loads `../.env` with its own `load_dotenv` and reads `DATABENTO_API_KEY` with `os.getenv` (no alias) |
| `alphaflow/server.py` | Reads `ALPHAFLOW_DEBUG` with `os.environ` |
| `options_whale/api_router.py` | Reads `OPTIONS_WHALE_PORT` with `os.environ`, and reads the Databento key at import with `config.required_env` instead of a config constant (`config.require_databento_key()` exists and is unused) |
| `upload_data.py` | Reads `UPLOAD_URL` again (config has an unused copy) and reads `UPLOAD_TOKEN` and `REPORT_UPLOAD` with `config.optional_env`; neither is a config constant |
| `main_pipeline.py` | Reads `DASHBOARD_URL` with `config.optional_env`; it is not a config constant |
| `setup.sh`, `menubar/optionswhale.10s.sh` | Parse `OPTIONS_WHALE_PORT` out of `.env` with `sed` (a shell script cannot import `config`); `setup.sh` also writes `SCHEDULED_RUNS` with `sed` |
| `fix_csv.py` | One-off script outside the pipeline. It hardcodes a Desktop data path. |
