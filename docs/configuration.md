# Configuration

Every setting the project reads: where the data folder is, every variable in `.env`, the keys for the static
`index.html` page, the fixed limits in code, and what happens when the Shanghai benchmark package is missing. For
operators setting up a Mac and for contributors adding a setting. For commands see
[Operations](operations.md); for what ends up in the data folder see [Data](data.md).

## How settings are loaded

- `config.py` is the single reader. It loads `.env` from the repository root with python-dotenv when the
  package is installed (it is in `requirements.txt`), then exposes constants such as `config.FRED_API_KEY`.
  If python-dotenv is missing, `.env` is silently ignored.
- A real environment variable wins over `.env` (python-dotenv does not override).
- `NAME=` with nothing after it sets the variable to the empty string. A blank value (empty or only spaces)
  counts as not set. For keys and URLs that means "not configured": the feature is skipped or the source reports
  `missing`. For the two numbers (`SMTP_PORT`, `OPTIONS_WHALE_PORT`) it means the default. A value that is not a
  whole number between 1 and 65535 stops every script that imports `config` with `ConfigError: SMTP_PORT must be a
  whole number ...`, so a typo is reported and not guessed.
- A legacy alias (`DB_API_KEY` for `DATABENTO_API_KEY`) is used whenever its primary is unset **or blank**, in
  every script.
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
3. **Nothing is created on import.** Importing `config` only works out the path (`config.DATA_DIR`, and
   `config.DATA_DIR_EXPLICIT`, which is true when `PORTFOLIO_DATA_DIR` is set). Folders are created by commands:
   `main_pipeline.py run`, `cme-login`, `download_volume.py` and `update_inventory.py` create the default sibling
   `CME_Data` when it is missing (`config.ensure_data_dir(allow_create=True)`). A `PORTFOLIO_DATA_DIR` that does
   not exist is never created, by anything: the command stops with `ConfigError: Data directory ... does not
   exist` (exit code 1 from `main_pipeline.py`). Commands that only read, and `import-history`, never create the
   folder. `setup.sh` creates it itself with `mkdir -p`. A script run from a fresh clone therefore leaves no empty
   `CME_Data` next to the repository.

On a Mac that could hold two installations, pin the folder in `.env` and leave it there:

```
PORTFOLIO_DATA_DIR=/Users/you/CME_Data
```

Derived values, all from `config.py`:

| Name | Value |
|---|---|
| `DATA_DIR` | the folder above |
| `DATA_DIR_EXPLICIT` | `True` when `PORTFOLIO_DATA_DIR` is set; such a folder is never created for you |
| `DB_PATH` | `DATA_DIR/portfolio.db` |
| `daily_dir()` | `DATA_DIR/<Mon-DD-YY>`, the day in America/Chicago (`config.TZ_NAME`), for example `Sep-29-26` |
| `PROJECT_ROOT` | the repository folder (also holds `.env`) |

## Environment variables

Each table lists the name, the default or alias, what reads it, and what happens when it is empty. "Pipeline"
means `main_pipeline.py`; "terminal" means `options_whale/api_router.py`.

### Market data keys

| Variable | Default / alias | Read by | When empty |
|---|---|---|---|
| `DATABENTO_API_KEY` | `""`. Legacy alias: `DB_API_KEY` (used when this is unset or blank) | `config.DATABENTO_API_KEY` for the pipeline (`core/collect.py`, `core/sources.py databento_trades()`); `config.DATABENTO_API_KEY` in `options_whale/api_router.py` `databento_client()` (first dark pool request); `setup.sh`; `alphaflow/engine.py` | Pipeline: the SPY and SLV block-trade flow sections report `missing`; the run continues. Terminal: the server starts, and `/api/darkpool` answers 503 naming the missing key. `setup.sh` warns. |
| `FRED_API_KEY` | `""` | `core/collect.py` (macro series, Forecast Lab inputs, CPI dates) | Every FRED series reports `missing`. This includes the high-yield spread, so **VMRI shows `INCOMPLETE DATA`** (its credit input comes from FRED). Forecast Lab cards 5 and 6 lose their FRED inputs, and card 7 loses the CPI dates. |
| `EIA_API_KEY` | `""` | `core/sources.py eia_weekly()` | The public EIA `.xls` history file is used instead. Nothing is lost. |
| `GOLD_API_KEY` | `""` | `core/collect.py _spot()` (goldapi.io spot silver) | `prices.silver_spot` reports `missing`; the futures-minus-spot basis is empty. |
| `EBAY_APP_ID`, `EBAY_CERT_ID` | `""` | `core/collect.py collect_ebay()`; `ebay.py` (run by the terminal's `/api/silver_eagle_prices`) | Silver Eagle listings report `missing`. Both are needed. `ebay.py` exits with "Cannot proceed without a valid eBay API token". |

**Databento key.** Fill `DATABENTO_API_KEY`. `DB_API_KEY` is the name an older version used; it still works. Every
reader (the pipeline, the terminal, AlphaFlow, `setup.sh`) goes through `config.DATABENTO_API_KEY`, which takes the
alias whenever `DATABENTO_API_KEY` is unset or blank, so a `.env` copied from the template with
`DATABENTO_API_KEY=` blank and only `DB_API_KEY` filled works everywhere. The terminal creates its Databento client on the first `/api/darkpool` request.

`ALPHA_VANTAGE_KEY` was removed: nothing read it. A leftover line in `.env` is ignored.

### CME login

| Variable | Default | Read by | When empty |
|---|---|---|---|
| `CME_LOGIN_USERNAME`, `CME_LOGIN_PASSWORD` | `""` | `core/cme.py wait_for_login()` via `config.CME_LOGIN_*` (runs, and `main_pipeline.py cme-login`) | The CME login page opens with empty fields and you type them. These only pre-fill the form; you always finish the login and the MFA step yourself. |

### Email

Email is sent with `send_email.py deliver()`: plain SMTP with STARTTLS on `SMTP_PORT` (587 works; an implicit-SSL
port such as 465 does not). The message is always saved as `email.eml` first.

The four settings `EMAIL_SENDER`, `EMAIL_PASSWORD`, `SMTP_SERVER` and `RECIPIENT_EMAIL` go together. **All four
empty: email is `skipped`**, which is not a warning (a machine that never sends email ends its runs clean). **Some
set, some empty: `failed`**, with the empty names in the detail, and the run ends `completed_with_warnings`.

| Variable | Default | Read by | When empty |
|---|---|---|---|
| `EMAIL_SENDER` | `""` | `send_email.py` (login and From header) | With the other three also empty, email is `skipped`. Otherwise it is not sent: status `failed`, detail `email partly configured: EMAIL_SENDER empty`. |
| `EMAIL_PASSWORD` | `""` | `send_email.py` | Same as above. Use an app password if your provider offers one. |
| `SMTP_SERVER` | `""` | `send_email.py` | Same as above. |
| `SMTP_PORT` | `587` | `config.SMTP_PORT` | Empty or blank: 587. Text that is not a number from 1 to 65535 is a `ConfigError`. |
| `RECIPIENT_EMAIL` | `""` | `send_email.py` (To header) | Same as above. |
| `REPORT_SENDER` | `EMAIL_SENDER` | `scripts/import_email_positions.py` only | The script uses `EMAIL_SENDER`. If both are empty it exits and asks you to set one. |

### Phone push and dashboard link

| Variable | Default | Read by | When empty |
|---|---|---|---|
| `NTFY_URL` | `""` | `send_email.py` (daily brief, refinery alerts, CME login alert) | No pushes are sent. The daily brief and every alert are recorded as `skipped` (`NTFY_URL not configured`), which is not a warning. `main_pipeline.py ntfy-test` exits 2. Use the full topic URL, for example `https://ntfy.sh/<topic>`. |
| `DASHBOARD_URL` | `""` | `main_pipeline.py` through `config.DASHBOARD_URL` | The daily push has no **Dashboard** button. Set it to the address of your terminal that your phone can reach (for example a Tailscale address). |

### Upload

Uploads go to the receiver described in [server/README.md](../server/README.md). Only delivered runs upload:
`--no-deliver`, `--no-upload` and `--offline` skip it. `UPLOAD_URL` and `UPLOAD_TOKEN` go together: both empty is
`skipped` (not a warning); only one set is `failed` (a warning). The check happens before any database copy is built.
An upload counts only when the receiver's JSON reply says `ok` for every file sent; see
[Operations](operations.md#uploads).

| Variable | Default | Read by | When empty |
|---|---|---|---|
| `UPLOAD_URL` | `""` | `config.UPLOAD_URL`, used by `upload_data.py` | Nothing is uploaded. With `UPLOAD_TOKEN` also empty, `upload_files()` returns `skipped`, recorded in `v2_runs.delivery_detail` with no run warning. With only one of the two set it returns `failed`, which is a warning. |
| `UPLOAD_TOKEN` | `""` | `config.UPLOAD_TOKEN`, used by `upload_data.py` | Same. Must equal the token in `/etc/portfolio-upload/token` on the server. |
| `REPORT_UPLOAD` | `""` | `config.REPORT_UPLOAD`, used by `upload_data.py upload_report_result()` | Set to exactly `1` to also upload the daily report under an unguessable name, so the push can carry a permanent **Full report** link. Needs `UPLOAD_URL` and `UPLOAD_TOKEN`; with `1` and no receiver the report upload is `failed` (a warning). Not `1`: `skipped`. |

### Per-machine settings

| Variable | Default | Read by | When empty |
|---|---|---|---|
| `PORTFOLIO_DATA_DIR` | see [Data folder](#data-folder) | `config.py` | The sibling `CME_Data` (or the Desktop fallback) is used. A folder named here is never created for you. |
| `OPTIONS_WHALE_PORT` | `8080` | `config.OPTIONS_WHALE_PORT`, read by `options_whale/api_router.py`; `setup.sh` and `menubar/optionswhale.10s.sh` (parse `.env` with `sed`) | Empty means 8080. **Write it unquoted** (`OPTIONS_WHALE_PORT=9090`): the shell scripts only match digits right after `=`, so a quoted value would be read as 8080 by them and as 9090 by Python. |
| `SCHEDULED_RUNS` | `""` | `config.SCHEDULED_RUNS`; `main_pipeline.py run`; written by `setup.sh` | Must be exactly `1` on the one Mac that owns the schedule. With any other value, runs started with `--trigger scheduled` print a skip message and exit 0. Manual runs ignore it. `setup.sh --schedule` sets it to `1`; `--remove-schedule` and `--uninstall` blank it. `run_dashboard.command` with no argument uses the `scheduled` trigger. |

### Standalone tools

AlphaFlow (`alphaflow/`) is a separate app. It reads the same `.env` through `config.py`.

| Variable | Default | Read by | When empty |
|---|---|---|---|
| `ALPHAFLOW_DEBUG` | unset | `config.ALPHAFLOW_DEBUG`, read by `alphaflow/server.py` | Must be exactly `1` to turn on Flask debug mode, which also limits the server to `127.0.0.1`. Otherwise it listens on all interfaces, port 5001. Not in the template as an active line. |
| `DATABENTO_API_KEY` | | `alphaflow/engine.py`, through `config.DATABENTO_API_KEY` | Prints "Valid DATABENTO_API_KEY not found in .env, simulating..." and returns no trades. The `DB_API_KEY` alias is honoured. |

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
| any provider not listed (`ishares`, `federalreserve`) | 1 each (one semaphore per provider, created on first use) |

Other bounds: `core/api_client.py` (used only by `ebay.py`) uses a 15 s timeout and 3 retries with backoff on
429 and 5xx; `EIA_API_LENGTH` in `core/sources.py` is 5000 rows (the full weekly history).

## Shanghai benchmark dependency

**akshare.** `core/sources.py sge_silver_benchmark()` imports `akshare` inside the fetch to read the Shanghai
Gold Exchange (SGE) silver benchmark. It is in `requirements.txt`, so `setup.sh` installs it. A `.venv` built before
it was added does not have it: re-run `./setup.sh` or `.venv/bin/pip install -r requirements.txt`.

Without the package the run does not stop: `SourceSession.fetch()` logs the `ModuleNotFoundError` in `v2_fetch_log`,
`collect_shanghai()` returns `{"status": "missing", "reason": "akshare: error ModuleNotFoundError: ..."}`, the
Shanghai–COMEX spread card shows that reason, and the ledger columns `SHFE_Silver_USD` and `SHFE_Premium` are written
empty. Every run tries the import again.

## Rules

- Read settings in `config.py` only. Add a constant there with `optional_env("NAME", default="")` (text),
  `int_env("NAME", default, minimum=, maximum=)` (a number) or `flag_env("NAME")` (a switch that needs exactly `1`),
  add the variable to `.env.example` with a comment, and add a row to the table above. Other code imports the
  constant (`config.NAME`); tests patch it on the module that uses it.
- Importing `config` must stay free of side effects: no folders, no network, no credential checks. Code that needs a key
  checks the constant when it needs it and reports it missing (the terminal's `databento_client()`).
- Never hardcode a data path or a key. Use `config.DATA_DIR`, `config.DB_PATH` and `config.daily_dir()`.
- A missing optional key must give `status: "missing"` with a reason, never a made-up value.

Places that read `.env` without importing `config`, and why:

| Where | Why |
|---|---|
| `setup.sh`, `menubar/optionswhale.10s.sh` | Parse `OPTIONS_WHALE_PORT` out of `.env` with `sed` (a shell script cannot import `config`); `setup.sh` also writes `SCHEDULED_RUNS` with `sed` |

## Console engine (all optional)

| Variable | Default | Meaning |
|---|---|---|
| `ASSISTANT_LOCAL_URL` | `http://127.0.0.1:11434/v1` | OpenAI-compatible model server (Ollama, oMLX, LM Studio, llama.cpp) |
| `ASSISTANT_LOCAL_MODEL` | `qwen3.6:35b-a3b` | Model that answers; the console's menu lists the server's other models |
| `ASSISTANT_LOCAL_API_KEY` | `local` | Bearer token for the server, if it wants one |
| `ASSISTANT_LOCAL_REASONING` | `auto` | `auto`, `none`, `low`, `medium`, `high`. `auto`: none to route or answer from the brief, low over fetched data |
| `ASSISTANT_MAX_SECONDS` | `150` | Time limit per question |
| `ASSISTANT_MAX_CHARS` | `12000` | Size limit per data source shown to the model |
