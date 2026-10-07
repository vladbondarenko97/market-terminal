# Operations

How to set up a Mac, run the pipeline, schedule it, log in to CME, deliver the report, and fix things when they
break. It is for the person who operates the machine and for contributors who change the run flow. For design rules
and the file map see [Architecture](architecture.md). For every environment variable see
[Configuration](configuration.md).

Commands assume the repository root as the working directory and the project's virtualenv, for example
`.venv/bin/python main_pipeline.py status`. `run_dashboard.command` picks the virtualenv by itself.

## Overview

One run goes through five steps and then exits:

1. **Collect.** Quotes, options, block flow, macro, calendar, eBay, refining and CME data are fetched once.
2. **Capture.** Every raw response and observation is stored in the lake (`CME_Data/portfolio.db`).
3. **Snapshot.** One immutable JSON document per run is committed.
4. **Export.** Legacy ledgers, the engine position and forecast log rows are written, then every file, chart and the
   email are rendered from the snapshot.
5. **Deliver.** Email, phone push and uploads, unless the run was started with `--no-deliver` or `--offline`.

The run prints a start line, a "Snapshot committed" line, an "Exports written" line, an "Email:" line and a final
`Run <id> completed in <n>s` line (`completed_with_warnings` when something non-fatal failed).

**Where code and data live.** The code is the repository. The data folder (`CME_Data`) is resolved once in
`config.py`:

1. `PORTFOLIO_DATA_DIR` from `.env`, if set.
2. Otherwise the folder `CME_Data` next to the repository. It is created if it does not exist.
3. If that sibling does not exist but `~/Desktop/CME_Data/portfolio.db` does, the Desktop folder is used.
4. If both the sibling and the Desktop folder hold a `portfolio.db`, the code refuses to guess and raises
   `ConfigError`. Set `PORTFOLIO_DATA_DIR`.

An explicit `PORTFOLIO_DATA_DIR` that does not exist is an error for the pipeline, which never creates it (only
`setup.sh` does). See
[Configuration](configuration.md). What the data folder contains is described in [Data](data.md).

**Operational files in the data folder.**

| File or folder | Purpose |
|---|---|
| `.v2_run.lock` | The run lock (see [The run lock](#the-run-lock)). Safe to leave in place; the operating system releases the lock when the process dies. |
| `.v2_run_status.json` | Current or last run: `run_id`, `stage`, `state`, `pid`, `mode`, `started_at`, `updated_at`, `finished_at`, `elapsed_s`, `error`. Shown by `status`. |
| `.cme_browser_profile/` | The persistent Chromium profile used for CME (see [CME login](#cme-login)). |
| `state.json` | Portable cookie backup of the CME session. Never imported or stored in the lake. |
| `_rejected_downloads/` | Evidence files for CME downloads that failed validation. |

## Setting up a Mac

```bash
git clone https://github.com/vladbondarenko97/market-terminal.git
cd market-terminal
./setup.sh                 # first pass: creates .env; the server is installed but not started (no API key yet)
# fill in .env (at least DATABENTO_API_KEY), then:
./setup.sh                 # second pass: starts the server
./setup.sh --schedule      # only on the ONE Mac that should run the pipeline on a schedule
```

`setup.sh` is macOS only. It is safe to re-run: it reuses `.venv`, never replaces an existing `.env` (the schedule
flags edit only the `SCHEDULED_RUNS` line), and rewrites and restarts the server service each time.

### Flags

| Flag | Effect |
|---|---|
| (none) | Full setup (steps below), no schedule. A schedule installed earlier is left alone. |
| `--schedule` | Full setup plus the pipeline schedule (step 5). Makes this Mac the scheduler. |
| `--remove-schedule` | Removes the schedule and clears `SCHEDULED_RUNS` in `.env`, then exits. Run it on every Mac except the scheduler. Nothing else is touched. |
| `--no-menubar` | Skips the SwiftBar menu bar step. |
| `--uninstall` | Removes the server service, the schedule and the SwiftBar plugin-folder setting, then exits. Keeps `.env` (only `SCHEDULED_RUNS` is cleared), `.venv` and the data folder. SwiftBar itself stays installed. |
| `-h`, `--help` | Prints the usage header and exits 0. Works on any OS. |

`--schedule` together with `--remove-schedule` is an error. An unknown option exits 2. `--uninstall --schedule`
performs the uninstall.

### Steps

1. **Homebrew and Python.** Installs Homebrew if it is missing (asks for the Mac password), then `brew install
   python` if `python3` is missing.
2. **Virtualenv.** Creates `.venv`, upgrades pip, installs `requirements.txt`, runs `playwright install chromium`.
3. **Settings and data folder.** Copies `.env.example` to `.env` if there is no `.env`, sets mode 600, resolves the
   data folder through `config.py` and creates it (error if it cannot, for example a `PORTFOLIO_DATA_DIR` copied
   from another Mac). Reads `OPTIONS_WHALE_PORT` (default 8080). Warns when the data folder has no `portfolio.db`.
4. **Server login service.** Writes the plist, then starts the service and waits up to 45 seconds for HTTP 200 on
   `http://127.0.0.1:<port>/`. If `DATABENTO_API_KEY` (or `DB_API_KEY`) is empty the service is installed but not
   started, because the server cannot import without the key.
5. **Pipeline schedule** (only with `--schedule`). Writes the schedule plist, sets `SCHEDULED_RUNS=1` in `.env`,
   loads the job, and prints `ok: weekdays at HH:MM and HH:MM local time; NYSE holidays skip`. It warns if the Mac
   sleeps when idle (a sleeping Mac misses runs) and suggests the Energy setting "Prevent automatic sleeping when
   the display is off" or `sudo pmset -a sleep 0`.
6. **Menu bar** (skipped with `--no-menubar`). Installs SwiftBar with `brew install --cask swiftbar` if needed, points
   SwiftBar at the repository's `menubar/` folder (or symlinks the plugin into a plugin folder SwiftBar already
   uses), adds SwiftBar to the Login Items and restarts it.

### What it installs

| Item | Label | Plist | Log |
|---|---|---|---|
| Terminal server | `com.vlad.optionswhale` | `~/Library/LaunchAgents/com.vlad.optionswhale.plist` | `~/Library/Logs/optionswhale.log` |
| Pipeline schedule | `com.vlad.marketdashboard` | `~/Library/LaunchAgents/com.vlad.marketdashboard.plist` | `~/Library/Logs/marketdashboard.log` |

- The server service runs `.venv/bin/python api_router.py` in `options_whale/`, at login, and restarts it if it
  exits (`KeepAlive`, 30 second throttle).
- The schedule runs `/bin/zsh run_dashboard.command scheduled` in the repository root. It has no `RunAtLoad`, so
  installing it does not start a run.
- Both are LaunchAgents in the user's GUI session. They run only while that user is logged in.
- Neither log is rotated.

### What it prints

Progress lines start with `==>`; `ok:` lines confirm a step; warnings (`!!`) and fatal errors (`xx`) go to stderr.
It ends with a summary: the server line (`running at http://localhost:<port>`, `installed but NOT responding`, or
`NOT started`), the menu bar line, a schedule line, and the server log path. If a schedule plist exists but
`--schedule` was not given, the summary reminds you to run `--remove-schedule` unless this Mac is the scheduler.

**Exit status.** 0 only when the server answers at the end. Otherwise 1, including the normal first pass before
`.env` is filled in and including a pass where the schedule installed correctly but the server is down.
`--remove-schedule` and `--uninstall` exit 0.

### One Mac owns the schedule

Scheduled runs write the engine positions and the forecast log. Two Macs doing that would create two histories, so
a scheduled run needs two things: the launchd job (installed by `--schedule`) and `SCHEDULED_RUNS=1` in `.env`
(also set by `--schedule`). A leftover launchd job on another Mac skips itself because its `.env` has no
`SCHEDULED_RUNS=1`. To move the schedule:

1. Copy the data folder (at least `portfolio.db`) to the new Mac, so the position history comes with it.
2. On the old Mac: `./setup.sh --remove-schedule`.
3. On the new Mac: `./setup.sh --schedule`.

Manual runs are not gated, on any Mac. Copying `.env` from the scheduler to another Mac copies
`SCHEDULED_RUNS=1`; run `--remove-schedule` there to clear it.

## Everyday commands

| Task | Command |
|---|---|
| Run, send everything | `python main_pipeline.py run` (same as `python main_pipeline.py` with no subcommand) |
| Run, send nothing (safe test) | `python main_pipeline.py run --no-deliver` |
| Run with no network | `python main_pipeline.py run --offline` |
| Hand run through the launcher (opens the dashboard) | `./run_dashboard.command manual` |
| Log in to CME (MFA) | `python main_pipeline.py cme-login` |
| Recent runs and the current run | `python main_pipeline.py status` |
| Re-render a saved run | `python main_pipeline.py replay [--run RUN_ID] [--out DIR]` |
| Resend a saved email | `python main_pipeline.py resend [--run RUN_ID]` |
| Variable catalog and lineage | `python main_pipeline.py catalog [--run RUN_ID] > catalog.md` |
| Import old files again | `python main_pipeline.py import-history` |
| Test the phone push | `python main_pipeline.py ntfy-test` |
| Tests (offline, no credentials) | `python -m unittest discover -s tests` |

A bare `python main_pipeline.py` is a live run that delivers email, push and uploads. The top-level parser takes no
flags, so `python main_pipeline.py --no-deliver` is an argument error (exit 2); write `run --no-deliver`.

### Subcommands

| Subcommand | Flags (defaults) | What it does | Exit codes | Takes the lock |
|---|---|---|---|---|
| `run` | see below | One coordinated run. | 0 finished (also with warnings, and also a skipped scheduled run); 75 busy; 1 failed before the snapshot was committed | yes |
| `cme-login` | none | Opens CME in a visible browser, waits up to 15 minutes for login and MFA, then downloads the newest volume workbook to verify the session (see [CME login](#cme-login)). | 0 verified; 1 not verified | no |
| `replay` | `--run RUN_ID` (latest committed snapshot), `--out DIR` (`<data>/<day folder>/replay_<run id>`) | Re-renders files, charts and the email from a stored snapshot. No network, never delivers. Logs a `replay` run. | 0; 1 no snapshot | no |
| `resend` | `--run RUN_ID` (latest committed snapshot) | Sends the run's stored `email.eml` again. | 0 SMTP accepted; 2 not accepted; 1 no run or no saved email | no |
| `catalog` | `--run RUN_ID` (latest) | Prints the variable catalog and lineage as Markdown. | 0 | no |
| `status` | none | Prints the last 10 runs, then the contents of `.v2_run_status.json`. Opens the database read-only, so it needs a database that a run or import has already created. | 0 | no |
| `import-history` | none | Idempotent import of old files and tables into the lake. | 0 | no |
| `ntfy-test` | none | Sends the latest run's phone brief (summary plus report attachment) to `NTFY_URL`. Does not upload. Prints the result. | 0 even if the push failed; 1 no run or no saved report | no |

### `run` flags

| Flag | Default | Effect |
|---|---|---|
| `--offline` | off | No provider or network requests; CME is skipped. Implies `--no-deliver`. Records no ledgers, positions or forecast rows. Sections come out as "missing" unless the lake already holds the data. |
| `--no-deliver` | off | Saves the report and `email.eml` but sends nothing: no email, no phone push, no CME-login push, no uploads. Ledgers, positions and forecast rows are still recorded, and the CME browser still opens. |
| `--no-upload` | off | Skips both uploads (the report and the database/dashboard copy). Email and phone push still go out. |
| `--skip-cme` | off | Makes no CME request at all (volume or inventory) and uses saved CME history. |
| `--no-cme-browser` | off | Only disables the browser fallback for the CME inventory workbook (it then uses plain HTTP only). The volume listing still opens the browser. To avoid the browser use `--skip-cme` or `--cme-max-files 0`. |
| `--cme-max-files N` | 10 | Maximum missing CME volume workbooks to fetch this run, newest first. Clamped to 40. `0` or a negative number skips the volume download. |
| `--login-wait MINUTES` | 15 | How long to wait for you to finish a CME login when CME refuses a download. `0` never waits. |
| `--trigger NAME` | `manual` | A label stored with the run. Only `scheduled` has an effect (see [Scheduling](#scheduling)). Any other string is just a label. |

Known issue: `--no-cme-browser` does not stop the volume listing from opening Chromium
(`core/collect.py` `acquire_cme`, `core/cme.py` `browser_fetch_volume_listing`). See [Known issues](known-issues.md).

### What each suppression turns off

| | Email | Phone push and refinery alerts | CME-login push | Report upload | DB and dashboard upload | Provider requests | CME requests | Ledgers, positions, forecast rows |
|---|---|---|---|---|---|---|---|---|
| `--no-deliver` | no | no | no | no | no | yes | yes | recorded |
| `--no-upload` | yes | yes | yes | no | no | yes | yes | recorded |
| `--skip-cme` | yes | yes | no push | yes | yes | yes | no | recorded |
| `--offline` | no | no | no | no | no | no | no | not recorded |

### The run lock

A second `run` while one is in progress prints `BUSY: run <id> is in progress (stage <stage>). Not starting
another.` and exits 75. It does not wait. The lock is an exclusive `flock` on `.v2_run.lock`, so a crashed or killed
process releases it. Do not delete the file and do not work around the lock.

Only `run` takes the lock. `replay`, `resend`, `import-history`, `cme-login`, `download_volume.py` and
`update_inventory.py` do not, so they can overlap a run. `cme-login` and a run share one Chromium profile; do not
start `cme-login` while a run is in progress.

Known issue: only `run` is protected by the lock (`main_pipeline.py` `RunLock`, used only in `run()`). See
[Known issues](known-issues.md).

Failure behavior. If collection, capture or the snapshot commit raises, the run is marked `failed`, the lock is
released, a traceback is printed and the exit code is 1. After the snapshot is committed, errors no longer fail the
run; see [Delivery](#delivery).

### First run on a new machine

A first run with the default settings opens a Chromium window and waits up to 15 minutes for a CME login, because
the volume workbooks only download for a logged-in session. Do one of these first:

- `python main_pipeline.py cme-login`, then `python main_pipeline.py run --no-deliver`.
- Or skip CME for now: `python main_pipeline.py run --no-deliver --skip-cme` (the CME sections are marked missing),
  or `--login-wait 0` to try CME without waiting.

If you moved to a new Mac, copy the data folder first, or run `python main_pipeline.py import-history` after copying.

## Launcher script (`run_dashboard.command`)

```bash
./run_dashboard.command [TRIGGER]
```

| Item | Behavior |
|---|---|
| Trigger | The only argument. **The default is `scheduled`**, which is what launchd passes. A bare `./run_dashboard.command` is therefore a scheduled-trigger run: it skips on any Mac without `SCHEDULED_RUNS=1`, and outside a trading-day session on the scheduler. For a hand run use `./run_dashboard.command manual`. |
| Python | Uses `.venv/bin/python` when it exists (puts `.venv/bin` first on `PATH`). Otherwise it sources `~/.zshrc` and runs `conda activate base`. |
| Command | `python main_pipeline.py run --trigger "$TRIGGER"`. No other flags, so defaults apply (15 minute CME wait, 10 files). |
| Busy | If the run exits 75, the script prints "Another run is already in progress; this request observed it and exited." and **exits 0**. The 75 is masked. |
| Browser | If the trigger is not `scheduled` and today's `volume_dashboard.html` exists, it runs `open` on it. This also happens after a failed run, and it opens a browser on the Mac that runs the script. |
| Exit | Otherwise the exit status of the run. |

The terminal's Run button (`POST /run`, see [API](api.md)) runs `bash run_dashboard.command manual`. The request
stays open until the run ends, including a CME login wait, and it reports success whenever the script exits 0,
which includes the busy case.

Known issue: `POST /run` is synchronous with no timeout, and a busy run (exit 75, masked by the script) still
returns `SUCCESS` (`options_whale/api_router.py` `run_dashboard`, `run_dashboard.command`). See [Known issues](known-issues.md).

## Scheduling

### Times

`setup.sh --schedule` installs ten launchd `StartCalendarInterval` entries: Monday to Friday at two times.

| Run | Eastern | Central | Mountain | Pacific |
|---|---|---|---|---|
| At the open | 09:31 | 08:31 | 07:31 | 06:31 |
| Before the close | 15:45 | 14:45 | 13:45 | 12:45 |

launchd uses the Mac's clock, so setup converts the Eastern times to local time once, with today's offset, and
writes the result into the plist. The gate itself always uses `America/New_York`. Run folders and report dates use
Chicago time.

**After moving the Mac to another time zone, re-run `./setup.sh --schedule`.** All US zones switch daylight saving
on the same day, so the converted times stay correct all year in US zones that observe it. For Arizona, Hawaii or a
non-US zone they drift by an hour at each US change; the gate then skips the run or lets it through an hour off.
The weekday numbers are local days, so a zone far from the US can also lose the 15:45 run on one weekday.

Known issue: the Eastern-to-local conversion is fixed at install time (`setup.sh`, schedule step). See
[Known issues](known-issues.md).

### The gate

For `--trigger scheduled`, `run` checks, in this order, before it takes the lock or touches the database:

1. `SCHEDULED_RUNS` in `.env` must be exactly `1`. Otherwise: `scheduled runs are off on this machine
   (SCHEDULED_RUNS=1 is not set in .env)`.
2. The day must be an NYSE trading day. Otherwise: `<Day YYYY-MM-DD> is not an NYSE trading day`.
3. The time must be inside the window: from 10 minutes before the open to the close, 09:20 to 16:00 Eastern,
   inclusive. Otherwise: `HH:MM ET is outside the regular session (09:30-16:00 ET)`. The message names 09:30 even
   though 09:20 is allowed.

**What a skip looks like.** One line, `Scheduled run skipped: <reason>` (with a marker character in front), printed to
the log (`~/Library/Logs/marketdashboard.log` under launchd). The exit code is 0. Nothing is recorded: no run row,
no lock, no status file change, no email, no push. `run_dashboard.command` then exits 0 without opening anything.

### NYSE holidays

The calendar is computed in `core/market_calendar.py` for any year. It has no list of years to maintain.

| Holiday | Rule |
|---|---|
| New Year's Day | Jan 1. A Sunday moves to Monday. A Saturday is not observed (no holiday that year). |
| Martin Luther King Jr. Day | Third Monday of January |
| Presidents' Day | Third Monday of February |
| Good Friday | Two days before Easter (Gregorian) |
| Memorial Day | Last Monday of May |
| Juneteenth | June 19, from 2022 on |
| Independence Day | July 4 |
| Labor Day | First Monday of September |
| Thanksgiving | Fourth Thursday of November |
| Christmas | Dec 25 |

Except for New Year's Day, a holiday on a Saturday is observed the Friday before, and one on a Sunday the Monday
after. For 2026 the closures are Jan 1, Jan 19, Feb 16, Apr 3, May 25, Jun 19, Jul 3, Sep 7, Nov 26 and Dec 25.
Unscheduled closures (days of mourning, for example) are not known in advance and are not skipped.

**Early closes are not handled.** On the days NYSE closes at 13:00 Eastern (the day after Thanksgiving, Christmas Eve
when it is a weekday), the 15:45 run still executes, after the close.

**A late launchd fire inside the window still runs.** If the Mac was asleep at 09:31 and wakes at 11:00, launchd
runs the missed job then, and the position is stamped 11:00. Only a fire outside 09:20 to 16:00 Eastern is skipped.

Known issue: no early-close handling, and late fires inside the window run (`core/market_calendar.py`
`scheduled_run_skip_reason`). See [Known issues](known-issues.md).

### Requirements for scheduled runs

- The Mac is awake at the run times and the user is logged in (the job is a LaunchAgent).
- A visible Chromium window opens for CME, even when no login is needed. If the CME session has expired, the run
  waits up to 15 minutes with a window open and sends an urgent phone push.
- Check the job: `launchctl print gui/$(id -u)/com.vlad.marketdashboard`, and read
  `~/Library/Logs/marketdashboard.log`.

Manual runs on the scheduler Mac (`./run_dashboard.command manual`, `python main_pipeline.py run`, the Run button)
are not gated and also record an engine position.

## CME login

The CME daily-volume workbooks at `cmegroup.com/ftp/daily_volume/` only download for a logged-in session (an
anonymous request gets `{"message":"Bad Request"}`). The silver inventory workbook (`Silver_stocks.xls`) downloads
without login.

**How the session is kept.**

- A persistent Chromium profile in `<data folder>/.cme_browser_profile` holds cookies, storage and the Duo
  "remember me" data between runs. Chromium is always started visible (never headless).
- `<data folder>/state.json` is a portable cookie backup, refreshed after every CME browser session. A new profile is
  seeded once from it.
- `CME_LOGIN_USERNAME` and `CME_LOGIN_PASSWORD` in `.env` are optional. They only pre-fill the login form; you still
  complete the login and MFA.

**What a run does with volume files.**

1. Any `daily_volume*.xlsx` or `silver_stocks*` workbook you dropped into the data folder is imported first, with no
   network. To fill an old gap, drop the files there and run again (or run `import-history`).
2. It opens the FTP listing in the persistent browser and picks files it does not yet hold: newest first, only dates
   from the latest trade date held onward, at most `--cme-max-files` (default 10, hard cap 40, set by
   `CME_BACKFILL_MAX_ATTEMPTS` in `config.py`). `0` skips the download.
3. If a download is refused with HTTP 400, 401 or 403, the run opens the login page in that browser, pre-fills the
   credentials, prints `CME login needed: finish the login + MFA in the browser window (waiting up to N min).`, and
   sends an urgent phone push titled "CME login needed" (not with `--no-deliver` or `--offline`). It polls every
   5 seconds until a logged-in page appears. Closing the window ends the wait.
4. After a successful login it saves the session, reloads the listing and retries the same file once, then goes on.
   It asks for login at most once per run.
5. If nobody logs in within `--login-wait` minutes (default 15; `0` never waits), or the window is closed, the run
   continues with saved history. The volume outcome is `login_required` and says to run `cme-login`.

A listing with no `daily_volume` files gives the outcome `unavailable` and does not prompt for login.

CME runs in parallel with the other sources and its result is awaited last, so a login wait does not delay quotes,
options or flow. The run lock is held for the whole wait, so another run gets BUSY.

**Labels in the report.** The CME volume section is `fresh` when the newest trade date is within 5 days and this run
acquired or confirmed it. It is `cached` when the data is within 5 days but this run could not fetch anything. It is
`stale` only when the newest trade date is older than 5 days. A workbook that fails validation is rejected, saved
under `_rejected_downloads/` and never parsed into history.

**Inventory.** The workbook is requested over plain HTTP first (2 attempts per URL, 20 second pause after a 429). If
that fails with access denied or an error, and `--no-cme-browser` was not given, the run tries the browser.

**`cme-login`.** `python main_pipeline.py cme-login` does the same login once, by hand. The wait is fixed at
15 minutes (no flag) and it sends no phone push. After login it saves `state.json`, opens the listing, downloads the
newest workbook and checks that it is a spreadsheet. It prints `CME session saved and verified` (exit 0) or `CME
download not verified (session saved if you logged in)` (exit 1). Run it when a run reports `CME session
missing/expired`, and before the first run on a new Mac.

Known issue: the volume backfill only goes forward from the latest held trade date, so older gaps are never fetched
automatically (`core/cme.py` `browser_fetch_volume_listing` `earliest`, passed from `core/collect.py`
`acquire_cme`). Drop the missing workbooks into the data folder instead. See [Known issues](known-issues.md).

## Delivery

A delivered run sends in this order: email, report upload, phone push and refinery alerts, then the database and
dashboard upload. The email file is saved to disk before any SMTP connection.

### Settings that matter

| Variable | Used for |
|---|---|
| `EMAIL_SENDER`, `EMAIL_PASSWORD`, `SMTP_SERVER`, `SMTP_PORT` (default 587), `RECIPIENT_EMAIL` | Email |
| `NTFY_URL`, `DASHBOARD_URL` | Phone push; the optional "Dashboard" button |
| `UPLOAD_URL`, `UPLOAD_TOKEN` | Both uploads |
| `REPORT_UPLOAD` (`1` to enable) | Report upload only |
| `SCHEDULED_RUNS` (`1`) | Scheduled-run gate |

Full list and meanings: [Configuration](configuration.md). An empty value switches a channel off, with the notes below.

### Email

- `EMAIL_SENDER`, `EMAIL_PASSWORD`, `SMTP_SERVER` and `RECIPIENT_EMAIL` must all be set. The connection is
  SMTP with STARTTLS (no implicit-SSL port), timeout 60 seconds.
- The subject is `Daily Market Report & Options Brief | <day folder>` (with a leading chart emoji). The plain-text
  body is exactly `daily_market_report.txt`; the HTML alternative adds the charts inline.
- Outcomes stored as `delivery_status`:

| Status | Meaning |
|---|---|
| `smtp_accepted` | The server accepted the message. This is not proof of inbox delivery. |
| `failed` | Not configured, could not connect or log in, or the server rejected the message. |
| `outcome_unknown` | The connection dropped during the send. Not retried. Check the mailbox before you `resend`. |
| `attempting` | Delivery started and the run ended before recording a result. |
| `not_requested` | `--no-deliver`, `--offline` or a replay. |

Known issue: an empty `SMTP_PORT=` in `.env` makes `config.py` fail on import (`SMTP_PORT = int(...)`), so "empty
switches it off" does not hold for this variable. See [Known issues](known-issues.md).

### Phone push (ntfy)

`NTFY_URL` is the full topic URL. Three kinds of push exist:

| Push | When | Content |
|---|---|---|
| Daily brief | Every delivered run | A ranked summary as the message, and the full report as a dated `.txt` attachment (the same text as the email). ntfy.sh keeps attachments for 3 hours. |
| Refinery alert | One per newly seen major unplanned refinery outage | Urgent, with site, units, start and cause. |
| CME login needed | During a CME login wait | Urgent (see [CME login](#cme-login)). |

The brief carries a "Full report" button only when the report was uploaded (`REPORT_UPLOAD=1`, `UPLOAD_URL` and
`UPLOAD_TOKEN` set), and a "Dashboard" button when `DASHBOARD_URL` is set. If the attachment upload to ntfy fails, a
plain message is sent instead (`sent_without_attachment`). A read timeout is `outcome_unknown` with no second push,
to avoid duplicates. Use `python main_pipeline.py ntfy-test` to try the latest run's brief without uploading anything.

Known issue: with `NTFY_URL` empty the result is recorded as `failed`, not `skipped` (`send_email.py` `ntfy_brief`),
and `ntfy-test` still exits 0 when the push fails (`main_pipeline.py` `main`). See [Known issues](known-issues.md).

### Uploads

Both go to the receiver described in [`../server/README.md`](../server/README.md) and need `UPLOAD_URL` and
`UPLOAD_TOKEN`.

| Upload | Needs | Sends |
|---|---|---|
| Report | `REPORT_UPLOAD=1` too | The report as `market-report-YYYY-MM-DD-HHMMZ.txt` (UTC time of the run). Returns a permanent URL used by the "Full report" button. |
| Database and dashboard | `UPLOAD_URL`, `UPLOAD_TOKEN` | A consistent copy of `portfolio.db` without the `v2_*` tables and views (the original is not touched), plus the dashboard XML (`volume_dashboard.xml`, or `volume_dashboard.txt` if there is no XML) sent under the name `volume_dashboard.xml`. |

`python upload_data.py` runs the second upload by hand for today's folder. `--no-upload` skips both.

### What makes a run `completed_with_warnings`

The final status is `completed_with_warnings` when, after the snapshot was committed, any of these happen:

- the email status is anything but `smtp_accepted` (this includes "not configured", so a machine with no SMTP
  settings gets this status on every delivered run);
- one ledger step failed (`ledger errors: ...`);
- an exception in the export or delivery stage (`export/delivery error: ...`).

Ntfy and upload failures are never warnings. They appear only in the run's `delivery_detail` (JSON with `smtp`,
`ntfy` and `upload`).

**The exit code is 0 either way**, so launchd, `run_dashboard.command` and the Run button cannot see a failed
delivery. Check `python main_pipeline.py status` (the `status` column and `delivery_status`).

Known issue: the ledger, position, forecast-log, render and delivery steps share one `try` block, so an exception in
`positions.record_signal` or `forecast.record_forecasts` skips rendering and delivery but still ends with exit 0
(`main_pipeline.py` `run`). See [Known issues](known-issues.md).

Known issue: delivery failures do not change the exit code, and only email counts as a warning (`main_pipeline.py`
`run`). See [Known issues](known-issues.md).

### Resend

`python main_pipeline.py resend [--run RUN_ID]` sends the stored copy of that run's `email.eml` (the payload in the
lake, not the file in the day folder, which a later run may have overwritten). It records the result on the run and
exits 0, 2 or 1 (see [Everyday commands](#everyday-commands)). `python send_email.py` does the same for the latest run. Resend is explicit; nothing
resends automatically.

## Replay, resend, catalog, status, import-history

| Command | Notes |
|---|---|
| `replay` | Re-renders from the committed snapshot with the current code. Output goes to a separate `replay_<run id>` folder inside the day folder, never over the delivered files. It prints whether the report is identical to the original (`True`, `False`, or `None` when no hash was saved) and whether the email body equals the report. A difference after a code change is expected. It appends no ledger rows and sends nothing. |
| `resend` | See [Delivery](#delivery). |
| `catalog` | Prints Markdown to stdout. Redirect it to a file. |
| `status` | Last 10 runs (`run_id`, `mode`, `status`, `started_at`, `elapsed_s`, `delivery_status`), then the current status JSON. |
| `import-history` | Imports legacy tables, CME workbooks, ledger CSVs, daily files and JSON state from the data folder. Safe to repeat: files are keyed by SHA-256. Credentials and system files (`state.json`, `.env`, `.DS_Store`) are never imported. Details in [Data](data.md). |

## Engine positions

The card is described in [Terminal](terminal.md); this section covers what gets recorded and how to repair the
history.

**When a position is recorded.** Every live run (not `--offline`, not a replay) records at most one row in
`v2_trade_signals` after the snapshot is committed, whatever its trigger and whether or not it delivers. That means
manual runs, `--no-deliver` test runs and Run-button runs add positions too. Only scheduled runs are gated by
`SCHEDULED_RUNS`. The row is keyed by run id, so a repeat never duplicates.

| Engine output | Row |
|---|---|
| A ticket with a contract | `CALL` or `PUT` with entry bid, ask, last, mid, IV, volume and open interest |
| Absolute score below 3 | `CASH` |
| No score at all | nothing |
| A score of 3 or more in either direction but no contract could be selected | nothing |

**Marks.** Each run also records the real mark of every open tracked SPY contract (not expired, not stopped) from
the option chains it already downloaded, as `position.mark` observations. It makes no extra request.

**Horizons.** +1 day, +1 week (7 days) and +2 weeks (14 days) from the entry date, each on the first trading day on or
after the target. The basis of each value:

| Basis | Meaning |
|---|---|
| `market` | A mark recorded by a run on that day |
| `model` | Black-Scholes (5 percent rate) at that day's SPY close, with the IV implied by the entry mid. The card marks it `~`. |
| `expiry_intrinsic` | Intrinsic value at the expiration close |
| `spy_move` | For `CASH`: the SPY move |
| `pending` | The date has not been reached |

Known issue: the `market` mark for a day is the earliest one recorded that day (`core/positions.py`
`_market_mark`), so with two runs a day it is the 09:31 ET mark, not a closing price. See
[Known issues](known-issues.md).

**Recover history from sent emails.** `scripts/import_email_positions.py` rebuilds positions from report emails
already sent:

```bash
python scripts/import_email_positions.py                 # read Apple Mail
python scripts/import_email_positions.py --dir ~/reports # exported .eml / .emlx / .mbox files, searched recursively
```

| Item | Behavior |
|---|---|
| Flags | `--dir FOLDER` only. Without it the script reads Apple Mail. |
| Needs | `REPORT_SENDER` (or `EMAIL_SENDER`) in `.env`: the address the reports came from. |
| Selection | Messages from that address with "Market Report" in the subject. Apple Mail is read through a copy of its index; only messages whose bodies are on disk can be used (the rest are counted as `headers_only`). |
| Idempotent | A message whose `X-Run-ID` is already in `v2_trade_signals` is skipped (`already_recorded`). Others are stored under `email:<Message-ID>` with source `email_archive`, and a repeat adds nothing. |
| Output | One line of counts (`indexed`, `report_emails`, `positions`, `cash`, `new`, `no_ticket`, `already_recorded`), the date span and a count by month. |
| Lock | It does not take the run lock. |

Known issue: `positions.backfill`, which would record signals from stored live snapshots, is not called by any
command (`core/positions.py` `backfill`). See [Known issues](known-issues.md).

## Menu bar icon

`menubar/optionswhale.10s.sh` is a SwiftBar plugin that refreshes every 10 seconds. It watches the terminal server
only; it shows nothing about the pipeline, the schedule or runs. Keep only plugins in the `menubar/` folder.

| State | Icon |
|---|---|
| `http://127.0.0.1:<port>/` returns 200 | Whale, green filled circle: "Online" |
| The service has a process but the page does not answer | Whale, dotted circle: "Starting / not responding" |
| No process | Whale, red circle: "Offline" |

The dropdown shows the status and port, then **Stop server** and **Restart server** (while the service is running)
or **Start server**, then **Open in browser**, **Open log** (opens `optionswhale.log` in Console) and **Refresh**.
If the plist is missing it shows "Not installed — run setup.sh".

| Action | What it does |
|---|---|
| Start | `launchctl bootstrap` the plist (or `kickstart` if it is loaded) |
| Stop | `launchctl bootout`. The plist stays, so the server starts again at the next login. |
| Restart | `launchctl kickstart -k` |

Known issue: `./setup.sh --uninstall` does not remove the plugin symlink it created in a SwiftBar plugin folder that
was already in use (`setup.sh`, menu bar step and uninstall). Delete it by hand. See [Known issues](known-issues.md).

## Logs and troubleshooting

### Where to look

| What | Where |
|---|---|
| Terminal server output | `~/Library/Logs/optionswhale.log` (menu bar: **Open log**) |
| Scheduled run output, including "Scheduled run skipped" lines | `~/Library/Logs/marketdashboard.log` |
| A hand run | The terminal you ran it in. The Run button discards output unless the script fails. |
| Recent runs and delivery state | `python main_pipeline.py status` |
| Why a run failed or warned | `v2_runs.error`, `stages_json` and `delivery_detail` for the run (see [Data](data.md)) |
| Whether a run is active | `.v2_run_status.json` in the data folder |

### Symptoms

| Symptom | Cause | Fix |
|---|---|---|
| `BUSY: run <id> is in progress` (exit 75) | Another run holds the lock: scheduled and manual overlap, or a CME login wait. | Run `status` to see the stage. Wait for it, or finish the login. The lock frees itself if the process dies. |
| A scheduled run did nothing | It was skipped. | `grep "skipped" ~/Library/Logs/marketdashboard.log`. The reason is one of the three in [Scheduling](#scheduling). |
| No scheduled run and no log line | The Mac was asleep or logged out, or the job is not loaded. | `launchctl print gui/$(id -u)/com.vlad.marketdashboard`. Re-run `./setup.sh --schedule`. Stop idle sleep ([Setting up a Mac](#setting-up-a-mac), step 5). |
| Position stamped at a strange time | A late launchd fire inside the window ran. | Expected (see [Scheduling](#scheduling)). |
| `./run_dashboard.command` does nothing on a Mac | Its default trigger is `scheduled`. | Use `./run_dashboard.command manual`. |
| A Chromium window opened and the run waits | CME refused a download, so it waits for login. | Finish login and MFA in that window. To avoid the wait use `--login-wait 0` or `--skip-cme`. |
| `CME session missing/expired: run python main_pipeline.py cme-login` | The login did not complete in time or the session expired. | `python main_pipeline.py cme-login`, then run again. |
| CME section marked `cached` or `stale` | No new workbook this run (see [CME login](#cme-login)). | Check the CME outcome in the log; log in; drop missing workbooks into the data folder. |
| Menu bar icon red, or `Server: NOT started` | `DATABENTO_API_KEY` is empty, so the server cannot start; or the port is taken. | Set the key in `.env`, set `OPTIONS_WHALE_PORT` if needed, run `./setup.sh`, read `optionswhale.log`. |
| `./setup.sh` exits 1 | The server is not up at the end (also the normal first pass). | Read the summary; fill in `.env`; run it again. |
| Run ends `completed_with_warnings` | Email failed (not configured or rejected), ledger error, or export error. | `status`, then the run's `error` text. Fix `.env`, then `resend`. |
| Email status `outcome_unknown` | The connection dropped during the send. | Check the mailbox. Only then `resend`. |
| No phone push | `NTFY_URL` empty, `--no-deliver`, or ntfy unreachable. | `python main_pipeline.py ntfy-test`. The run's `delivery_detail` shows the ntfy result. |
| `status` shows `running` but no process exists | The run was killed. The status file is not cleaned up. | Start a new run; the lock is already free. |
| `ConfigError: Two CME_Data installations found` | Both the sibling and the Desktop folder hold a `portfolio.db`. | Set `PORTFOLIO_DATA_DIR` in `.env`. |
| `Data directory ... does not exist` | `PORTFOLIO_DATA_DIR` points at a missing folder. | Create it or fix the path. |
| `ValueError: invalid literal for int()` as soon as any command starts | `SMTP_PORT=` is empty in `.env` (read at import). | Set it to `587` or remove the line. |
| Run button says success but nothing ran | The run was busy (exit 75 is masked). | `status`. |
| An engine position is missing for a run | The run was offline, had no score, had no selectable contract, or the export stage failed before the position row. | `status`; the run's `stages_json` has `trade_signal`. |
| Traceback and exit 1 | The run failed before the snapshot was committed. Earlier snapshots are not affected. | Read the error in `v2_runs`; fix the cause; run again. |

Known issue: a killed or interrupted run leaves `.v2_run_status.json` showing `running` (`main_pipeline.py`
`set_status`, `run`). The lock is the source of truth. See [Known issues](known-issues.md).

## Backups and rollback

This repository contains only the current (v2) code; its history starts at v2, so there is no legacy branch or
launcher to go back to. The data folder is additive: v2 adds `v2_*` tables and views to `portfolio.db` and new files
to `CME_Data`, and keeps the legacy ledger tables and CSV headers unchanged. See [Data](data.md).

- **Back up `CME_Data` before the first `import-history` and before any large change.** Copy the whole folder
  (`portfolio.db`, the ledger CSVs, `state.json`) while no run is in progress. The database uses the default
  rollback journal, so a copy between runs is complete.
- **Go back to earlier code** by checking out an earlier commit of this repository. Newer code may add tables or
  columns that older code does not know about, so keep the copy.
- **Snapshots are immutable.** A wrong value is fixed by changing the model and making a new run, and recorded in
  [Value changes](value-changes.md).
- **Undo a schedule** with `./setup.sh --remove-schedule`.


## Other scripts

None of these take the run lock. The compatibility renderers print or write from the latest committed snapshot and
make no requests; they need at least one committed run ("No committed v2 snapshot yet. Run: python
main_pipeline.py run"). The full repository map is in [Architecture](architecture.md#repository-map).

| Script | Use |
|---|---|
| `download_volume.py [N]` | Bounded CME volume download outside a run (N defaults to 10, kept between 1 and 40). Like a run, it opens the login window and waits up to 15 minutes if CME refuses, and sends no phone push. |
| `update_inventory.py` | Downloads the COMEX silver inventory workbook only and prints the tail of the inventory history. |
| `scripts/backfill_crypto_metals.py` | Adds missing daily BTC, silver and gold rows (past year, from Yahoo) to `crypto_metrics_history`. Existing rows are never replaced. Needs network. |
| `scripts/import_email_positions.py` | Recovers positions from sent report emails (see [Engine positions](#engine-positions)). |
| `send_email.py` | Run as a script, resends the latest run's saved email. |
| `upload_data.py` | Run as a script, uploads the database copy and today's dashboard file (see [Delivery](#delivery)). |
| `dump_data.py` | Writes `volume_dashboard.txt` (XML) for the latest snapshot into its day folder. |
| `tactical_ruling.py` | Prints the tactical ruling XML for the latest snapshot. |
| `market_reader.py` | Prints the Morning Market Brief text for the latest snapshot. |
| `institutional_scanner.py` | Prints the SPY and SLV institutional scan from the latest committed results. |
| `parse_volume.py [DAYS]` | Prints the legacy CME volume table from the lake (DAYS defaults to 30). |
