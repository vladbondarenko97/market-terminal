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
`Run <id> completed in <n>s` line (`completed_with_warnings`, exit code 3, when a stage or a configured delivery
channel failed after the snapshot was committed; see [Exit codes](#exit-codes)).

**Where code and data live.** The code is the repository. The data folder (`CME_Data`) is resolved once in
`config.py`:

1. `PORTFOLIO_DATA_DIR` from `.env`, if set.
2. Otherwise the folder `CME_Data` next to the repository. `main_pipeline.py run` creates it if it does not exist
   (importing `config` does not).
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
| `.v2_run_status.json` | Current or last run: `run_id`, `stage`, `state` (`running`, `completed`, `completed_with_warnings` or `failed`), `pid`, `mode`, `started_at`, `updated_at`, `finished_at`, `elapsed_s`, `error`. Shown by `status`. A file that says `running` while no process holds the lock belongs to a killed run; `status` and the terminal show it as `interrupted`. |
| `.v2_manual_run.log` | Output of runs started by the terminal's Run button. |
| `backups/` | `portfolio-<UTC time>.db` copies that `import-history` makes before it imports (see [Backups and rollback](#backups-and-rollback)). |
| `<day folder>/offline_<run id>/` | The files of an `--offline` run. Offline runs never write the day's shared files. |
| `.cme_browser_profile/` | The persistent Chromium profile used for CME (see [CME login](#cme-login)). |
| `state.json` | Portable cookie backup of the CME session. Never imported or stored in the lake. |
| `_rejected_downloads/` | Evidence files for CME downloads that failed validation. |

## Setting up a Mac

```bash
git clone https://github.com/vladbondarenko97/market-terminal.git
cd market-terminal
./setup.sh                 # first pass: creates .env and starts the server
# fill in .env (see Configuration for what each key unlocks), then:
./setup.sh                 # second pass: restarts the server with the new settings
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
| `--uninstall` | Removes the server service, the schedule and the menu bar plugin (the SwiftBar plugin-folder setting, or the link setup made in a SwiftBar folder that was already in use, when that link points at this repository's plugin), then exits. Keeps `.env` (only `SCHEDULED_RUNS` is cleared), `.venv` and the data folder. SwiftBar itself stays installed. |
| `-h`, `--help` | Prints the usage header and exits 0. Works on any OS. |

`--schedule` together with `--remove-schedule` is an error. An unknown option exits 2. `--uninstall --schedule`
performs the uninstall.

### Steps

1. **Homebrew and Python.** Installs Homebrew if it is missing (asks for the Mac password), then `brew install
   python` if `python3` is missing.
2. **Virtualenv.** Creates `.venv`, upgrades pip, installs `requirements.txt`, runs `playwright install chromium`.
3. **Settings and data folder.** Copies `.env.example` to `.env` if there is no `.env`, sets mode 600, resolves the
   data folder through `config.py` and creates it (error if it cannot, for example a `PORTFOLIO_DATA_DIR` copied
   from another Mac). Reads `OPTIONS_WHALE_PORT` from `.env` (blank or missing means 8080). Warns when the data folder has no `portfolio.db`.
4. **Server login service.** Writes the plist, then starts the service and waits up to 45 seconds for HTTP 200 on
   `http://127.0.0.1:<port>/`. With `DATABENTO_API_KEY` (or `DB_API_KEY`) empty it warns that the dark pool panels
   stay empty; the server still starts.
5. **Pipeline schedule** (only with `--schedule`). Asks `core/market_calendar.py` for the local fire times (see
   [Scheduling](#scheduling)), writes the schedule plist with one entry per distinct weekday, hour and minute, sets
   `SCHEDULED_RUNS=1` in `.env`, loads the job, and prints `ok: N launch times, local clock times HH:MM HH:MM ...`
   with a line on when a fire runs. It warns if the Mac sleeps when idle (a sleeping Mac misses runs) and suggests
   the Energy setting "Prevent automatic sleeping when the display is off" or `sudo pmset -a sleep 0`.
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
It ends with a summary: the server line (`running at http://localhost:<port>` or `installed but NOT responding`), a
`Keys:` line when `DATABENTO_API_KEY` is empty, the menu bar line, a schedule line, and the server log path. If a schedule plist exists but
`--schedule` was not given, the summary reminds you to run `--remove-schedule` unless this Mac is the scheduler.

**Exit status.** 0 only when the server answers at the end. Otherwise 1, including a pass where the schedule
installed correctly but the server is down.
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
| Import old files again (backs up `portfolio.db` first) | `python main_pipeline.py import-history [--no-backup]` |
| Record engine tickets for old live runs that have none | `python main_pipeline.py backfill-positions` |
| Test the phone push | `python main_pipeline.py ntfy-test` |
| Tests (offline, no credentials) | `python -m unittest discover -s tests` |

A bare `python main_pipeline.py` is a live run that delivers email, push and uploads. The top-level parser takes no
flags, so `python main_pipeline.py --no-deliver` is an argument error (exit 2); write `run --no-deliver`.

### Subcommands

| Subcommand | Flags (defaults) | What it does | Exit codes | Takes the lock |
|---|---|---|---|---|
| `run` | see below | One coordinated run. | 0 finished cleanly (also a skipped scheduled run); 3 finished with warnings; 75 busy; 1 failed before the snapshot was committed (see [Exit codes](#exit-codes)) | yes |
| `cme-login` | none | Opens CME in a visible browser, waits up to 15 minutes for login and MFA, then downloads the newest volume workbook to verify the session (see [CME login](#cme-login)). | 0 verified; 1 not verified; 75 busy | yes |
| `replay` | `--run RUN_ID` (newest live snapshot; an offline one only when no live snapshot exists), `--out DIR` (`<data>/<day folder>/replay_<run id>`) | Re-renders files, charts and the email from a stored snapshot. No network, never delivers. Logs a `replay` run. | 0; 1 no snapshot or no database | no |
| `resend` | `--run RUN_ID` (newest live snapshot; an offline one only when no live snapshot exists) | Sends the run's stored `email.eml` again. | 0 SMTP accepted; 2 not accepted (including `skipped`, email not configured); 1 no database, no run or no saved email | no |
| `catalog` | `--run RUN_ID` (latest) | Prints the variable catalog and lineage as Markdown. | 0; 1 no database | no |
| `status` | none | Prints the last 10 runs, then the contents of `.v2_run_status.json`. A run row or a status file that says `running` while nothing holds the run lock is shown as `interrupted`. Opens the database read-only, so it needs a database that a run or import has already created. | 0; 1 no database | no |
| `import-history` | `--no-backup` | Copies `portfolio.db` to `backups/` first (see [Backups and rollback](#backups-and-rollback)), then imports old files and tables into the lake. Idempotent. | 0; 1 the backup failed (nothing imported); 75 busy | yes |
| `backfill-positions` | none | Records an engine ticket (a `v2_trade_signals` row) for every committed live run that has none (see [Engine positions](#engine-positions)). Idempotent. | 0; 1 no database; 75 busy | yes |
| `ntfy-test` | none | Sends the latest run's phone brief (summary plus report attachment) to `NTFY_URL`. Does not upload. Prints the result. | 0 sent (also `sent_without_attachment`); 2 not sent (`skipped`, `failed` or `outcome_unknown`); 1 no database, no run or no saved report | no |

### `run` flags

| Flag | Default | Effect |
|---|---|---|
| `--offline` | off | No provider or network requests; CME is skipped. Implies `--no-deliver`. Records no ledgers, positions or forecast rows. Writes its files to `<data>/<day folder>/offline_<run id>/`, never over the day's files. Sections come out as "missing" unless the lake already holds the data. The run is stored with mode `offline`; the terminal and `replay` use the newest live snapshot and fall back to an offline one only when no live snapshot exists. |
| `--no-deliver` | off | Saves the report and `email.eml` but sends nothing: no email, no phone push, no CME-login push, no uploads. Ledgers, positions and forecast rows are still recorded, and the CME browser still opens. |
| `--no-upload` | off | Skips both uploads (the report and the database/dashboard copy). Email and phone push still go out. |
| `--skip-cme` | off | Makes no CME request at all (volume or inventory) and uses saved CME history. |
| `--no-cme-browser` | off | Keeps the CME browser closed: the volume listing and download are skipped (volume outcome `skipped`, detail `browser disabled (--no-cme-browser)`), and the inventory workbook is fetched over plain HTTP only, with no browser fallback. Saved history is used for whatever is not fetched. To fetch volume files, run without the flag. `--skip-cme` also skips the inventory. |
| `--cme-max-files N` | 10 | Maximum missing CME volume workbooks to fetch this run (any unheld listed date), newest first. Clamped to 40. `0` or a negative number skips the volume download. |
| `--login-wait MINUTES` | 15 | How long to wait for you to finish a CME login when CME refuses a download. `0` never waits. |
| `--trigger NAME` | `manual` | A label stored with the run. Only `scheduled` has an effect (see [Scheduling](#scheduling)). Any other string is just a label. |

### What each suppression turns off

| | Email | Phone push and refinery alerts | CME-login push | Report upload | DB and dashboard upload | Provider requests | CME requests | Ledgers, positions, forecast rows |
|---|---|---|---|---|---|---|---|---|
| `--no-deliver` | no | no | no | no | no | yes | yes | recorded |
| `--no-upload` | yes | yes | yes | no | no | yes | yes | recorded |
| `--skip-cme` | yes | yes | no push | yes | yes | yes | no | recorded |
| `--no-cme-browser` | yes | yes | no push | yes | yes | yes | volume no, inventory over HTTP only | recorded |
| `--offline` | no | no | no | no | no | no | no | not recorded |

### Exit codes

`run` (and `./run_dashboard.command`, which passes the code through) exits with:

| Code | Meaning |
|---|---|
| 0 | The run completed cleanly. Also: a scheduled run that the gate skipped. |
| 3 | `completed_with_warnings`: the snapshot is committed, but a stage failed or a configured delivery channel failed (see [What makes a run `completed_with_warnings`](#what-makes-a-run-completed_with_warnings)). The files that could be built exist. |
| 75 | Another job holds the run lock. Nothing was started. |
| 1 | The run failed before the snapshot was committed (a traceback is printed), or a `ConfigError` stopped the command, for example a `PORTFOLIO_DATA_DIR` that does not exist. |
| 2 | Command-line usage error (unknown flag or command). |

Launchd and the Run button can therefore tell a clean run from one that needs a look: anything other than 0 is
worth reading `status` for. A channel that is not configured at all is `skipped` and does not change the code.

### The run lock

A second job while one is in progress prints `BUSY: run <id> is in progress (stage <stage>). Not starting another.`
(or, when the holder is not a run, `BUSY: another command holds the run lock ...`) and exits 75. It does not wait.
The lock is an exclusive `flock` on `.v2_run.lock` (`core/runlock.py`), so a crashed or killed process releases it.
Do not delete the file and do not work around the lock.

These take the lock because they write to the lake or share the CME Chromium profile: `run`, `cme-login`,
`import-history`, `backfill-positions`, `download_volume.py` and `update_inventory.py` (when it downloads). `replay`,
`resend`, `catalog`, `status`, `ntfy-test` and the terminal do not take it: they read stored data (`resend` sends a
stored message). The terminal asks whether the lock is held (`lock_is_held()`) without taking it.

**Killed runs.** A process that is killed mid-run (power loss, `kill -9`) cannot record its end, so
`.v2_run_status.json` and its `v2_runs` row still say `running`. The operating system frees the lock, so the lock is
the source of truth: `status` shows a `running` state with no lock holder as `interrupted`. The next `run` that takes
the lock changes those `v2_runs` rows from `running` to `interrupted` (only the status and an empty `error` are
written; no row is deleted) and prints how many. A run killed after its snapshot was committed keeps the status
`committed` and its snapshot stays usable.

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
| Busy | If the run exits 75, the script prints "Another run is already in progress; nothing was started (exit 75)." and exits 75. |
| Warnings | Exit 3 prints a pointer to `status` and exits 3. A failed run (exit 1) prints "Run failed" and exits 1. |
| Browser | If the trigger is not `scheduled`, the run exited 0 or 3, and today's `volume_dashboard.html` exists, it runs `open` on it (on the Mac that runs the script). It does not open anything after a failed or busy run. |
| Exit | The exit status of the run, unchanged (see [Exit codes](#exit-codes)). |

The terminal's Run button (`POST /run`, see [API](api.md)) starts `/bin/zsh run_dashboard.command manual` in the
background and answers at once (HTTP 202). While a job holds the run lock it answers 409 and starts nothing. The
script's output is appended to `.v2_manual_run.log` in the data folder, and the terminal polls `/api/run_status` and
shows the run's final state, including `warnings` (exit 3) and `interrupted`.

## Scheduling

### Times

The pipeline runs twice on each NYSE trading day, at two Eastern times (`SCHEDULE_ET` in
`core/market_calendar.py`): 09:31, just after the open, and 15:45, before the close. There is no afternoon run on an
early-close day (see [NYSE holidays](#nyse-holidays)).

| Run | Eastern | Central | Mountain | Pacific |
|---|---|---|---|---|
| At the open | 09:31 | 08:31 | 07:31 | 06:31 |
| Before the close | 15:45 | 14:45 | 13:45 | 12:45 |

launchd uses the Mac's clock, not New York's. `setup.sh --schedule` calls `launchd_intervals()` in
`core/market_calendar.py`, which converts both Eastern times for every NYSE trading day of the coming 371 days using
that day's own offsets, and returns the distinct local `(weekday, hour, minute)` fire times. Setup writes one
`StartCalendarInterval` entry for each, with launchd's weekday numbers (0 = Sunday). The daylight-saving changes of
New York and of the Mac's own zone are therefore already inside the list.

| Mac's zone | Entries | Local clock times |
|---|---|---|
| US Central, Mountain (with daylight saving), Pacific, Eastern | 10 (Monday to Friday) | the two times in the table above, all year |
| Arizona (no daylight saving) | 20 | 06:31 and 07:31, 12:45 and 13:45 (summer and winter) |
| London | 20 | 13:31 and 14:31, 19:45 and 20:45 (Europe changes its clocks on other dates than the US) |
| Tokyo | 20 | 22:31 or 23:31 the same day, 04:45 or 05:45 the next day (so the afternoon run is on Tuesday to Saturday) |

A zone with two clock times for one run fires at both all year. The one on the wrong side of a clock change does
not match an Eastern slot (Arizona's 06:31 in winter is 08:31 ET), and the [gate](#the-gate) skips it with a log
line. Those skip lines are expected.

**Re-run `./setup.sh --schedule` after moving the Mac to another time zone**, because the plist holds the old
zone's clock times. You do not need to re-run it for a daylight-saving change or a new year; do re-run it if a
country changes its daylight-saving rules.

### The gate

For `--trigger scheduled`, `run` checks, in this order, before it takes the lock or touches the database:

1. `SCHEDULED_RUNS` in `.env` must be exactly `1`. Otherwise: `scheduled runs are off on this machine
   (SCHEDULED_RUNS=1 is not set in .env)`.
2. The day must be an NYSE trading day. Otherwise: `<Day YYYY-MM-DD> is not an NYSE trading day`.
3. The time must not be after the close: 16:00 ET, or 13:00 ET on an early-close day. Otherwise:
   `HH:MM ET is after the 16:00 ET close on <Day YYYY-MM-DD>` (or `... the 13:00 ET early close ...`).
4. The time must be inside the window of a run slot: from 10 minutes before to 30 minutes after 09:31 or 15:45 ET.
   That is 09:21 to 10:01 and 15:35 to 16:00 (the close ends the second one) on an ordinary day, and 09:21 to 10:01
   only on an early-close day. Otherwise: `HH:MM ET is outside the scheduled run windows (09:21-10:01 and
   15:35-16:00 ET: 10 minutes before to 30 minutes after a run slot, never after the close)`.

Times are compared to the minute, in New York time, whatever time zone the Mac is in.

**What a skip looks like.** One line, `Scheduled run skipped: <reason>` (with a marker character in front), printed to
the log (`~/Library/Logs/marketdashboard.log` under launchd). The exit code is 0. Nothing is recorded: no run row,
no lock, no status file change, no email, no push. `run_dashboard.command` then exits 0 without opening anything.

**A late launchd fire.** When the Mac was asleep at a run time, launchd runs the missed job as soon as it wakes. A
fire up to 30 minutes after the slot still runs, so the position is stamped within half an hour of its slot. A later
one is skipped instead of recording a position stamped at the wrong time: waking at 11:00 ET skips the missed 09:31
run.

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

**Early closes.** The NYSE closes at 13:00 ET on these days (`early_close_days()`):

| Early close | Rule |
|---|---|
| The day after Thanksgiving | Every year |
| July 3 | When it is a trading day: July 4 falls on Tuesday to Friday |
| December 24 | When it is a trading day: Monday to Thursday |

That is Nov 27 and Dec 24 in 2026 (July 3 is the observed holiday that year), and only Nov 26 in 2027 (December 24
is the observed Christmas holiday). On an early-close day the 09:31 run goes ahead and the 15:45 run is skipped,
because the market is already shut.

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
2. Unless `--no-cme-browser` was given, it opens the FTP listing in the persistent browser and requests every listed
   workbook the lake does not hold (old gaps included), newest first, at most `--cme-max-files` (default 10, hard cap
   40, set by `CME_BACKFILL_MAX_ATTEMPTS` in `config.py`). A gap larger than the cap fills over several runs. `0` skips
   the download. With `--no-cme-browser` the listing is not opened at all: the volume outcome is `skipped` with the
   detail `browser disabled (--no-cme-browser)`.
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
15 minutes (no flag) and it sends no phone push. It takes the run lock, so it exits 75 while a run is in progress.
After login it saves `state.json`, opens the listing, downloads the
newest workbook and checks that it is a spreadsheet. It prints `CME session saved and verified` (exit 0) or `CME
download not verified (session saved if you logged in)` (exit 1). Run it when a run reports `CME session
missing/expired`, and before the first run on a new Mac.

## Delivery

A delivered run sends in this order: email, report upload, phone push and refinery alerts, then the database and
dashboard upload. The email file is saved to disk before any SMTP connection. Every channel is attempted whatever the
others did. A channel that is not configured at all is `skipped` and is not a warning; one that is configured and
fails is a warning (see [What makes a run `completed_with_warnings`](#what-makes-a-run-completed_with_warnings)).

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
| `skipped` | Email is not configured: all of `EMAIL_SENDER`, `EMAIL_PASSWORD`, `SMTP_SERVER` and `RECIPIENT_EMAIL` are empty. Not a warning. |
| `failed` | Partly configured (some of the four set; the detail names the empty ones), could not connect or log in, or the server rejected the message. |
| `outcome_unknown` | The connection dropped during the send. Not retried. Check the mailbox before you `resend`. |
| `attempting` | Delivery started and the run ended before recording a result. |
| `not_requested` | `--no-deliver`, `--offline` or a replay. |

An empty `SMTP_PORT=` means 587. A value that is not a number is a `ConfigError` when any script starts.

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
to avoid duplicates. With `NTFY_URL` empty the brief and the alerts are recorded as `skipped`. Push results are
`sent`, `sent_without_attachment`, `skipped`, `failed` and `outcome_unknown`. Use `python main_pipeline.py ntfy-test`
to try the latest run's brief without uploading anything; it exits 0 when the brief was sent and 2 when it was not.

### Uploads

Both go to the receiver described in [`../server/README.md`](../server/README.md) and need `UPLOAD_URL` and
`UPLOAD_TOKEN`.

| Upload | Needs | Sends |
|---|---|---|
| Report | `REPORT_UPLOAD=1` too | The report as `market-report-YYYY-MM-DD-HHMMZ.txt` (UTC time of the run). Returns a permanent URL used by the "Full report" button. |
| Database and dashboard | `UPLOAD_URL`, `UPLOAD_TOKEN` | A consistent copy of `portfolio.db` without the `v2_*` tables and views (the original is not touched), plus the dashboard XML (`volume_dashboard.xml`, or `volume_dashboard.txt` if there is no XML) sent under the name `volume_dashboard.xml`. |

`python upload_data.py` runs the second upload by hand for today's folder (exit 0 when the receiver accepted every
file, 2 otherwise). `--no-upload` skips both.

**What counts as uploaded.** The receiver answers HTTP 200 with a JSON object that has one entry per file, each with
`status` `ok`, `rejected` or `error` (`server/upload_receiver.php`). An upload is `uploaded` only when every file
sent came back `ok`; a rejected or missing file, a non-JSON reply or another HTTP status is `failed`, and the detail
lists each file's result (for example `portfolio: rejected; dashboard: ok`). The report upload also needs the
receiver to return a URL. `UPLOAD_URL` and `UPLOAD_TOKEN` are checked first, before the database copy is built: both
empty is `skipped`; only one of them set is `failed`. With `REPORT_UPLOAD=1` and no receiver the report upload is
`failed`; without `REPORT_UPLOAD=1` it is `skipped`.

### What makes a run `completed_with_warnings`

After the snapshot is committed, the work is split into independent stages: ledgers, trade signal, forecast log,
rendering (files, charts, `email.eml`), payload storage and delivery. A stage that raises is recorded in the run's
`stages_json` (`<stage>_error` holds the traceback, `warnings` lists the messages) and the other stages still run. So
an exception in `positions.record_signal` or `forecast.record_forecasts` no longer skips the files, the email or the
upload. If rendering itself fails there is nothing to send: `delivery_status` is `failed` and the run has a warning.

The final status is `completed_with_warnings`, and the exit code is 3, when any of these happens:

- a stage raised (see above), or one ledger step failed (`ledger errors: ...`);
- the email status is `failed` or `outcome_unknown`;
- a configured ntfy push is `failed` or `outcome_unknown`;
- the report upload or the database/dashboard upload is `failed` (the receiver did not answer `ok` for every file, or
  the request failed).

Not warnings: a `skipped` channel (not configured at all), a push that went out as `sent_without_attachment`, and
`not_requested` (`--no-deliver`, `--no-upload`, `--offline`). A machine with no SMTP, ntfy or upload settings
therefore ends its runs `completed` with exit code 0.

Where to read the details: `python main_pipeline.py status`, then `v2_runs.error` (the warnings, joined by `;`),
`stages_json` and `delivery_detail` for the run (JSON with `smtp`, `ntfy`, `report_upload` and `upload`).

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
| `status` | Last 10 runs (`run_id`, `mode`, `status`, `started_at`, `elapsed_s`, `delivery_status`), then the current status JSON. A run whose process is gone shows `interrupted` (see [The run lock](#the-run-lock)). |
| `import-history` | Takes the run lock and copies `portfolio.db` to `backups/` first (`--no-backup` skips the copy). Then imports legacy tables, CME workbooks, ledger CSVs, daily files and JSON state from the data folder. Safe to repeat: files are keyed by SHA-256. Credentials and system files (`state.json`, `.env`, `.DS_Store`) are never imported. Details in [Data](data.md). |

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
| `market` | The last mark recorded by a run on that day |
| `model` | Black-Scholes (5 percent rate) at that day's SPY close, with the IV implied by the entry mid. The card marks it `~`. |
| `expiry_intrinsic` | Intrinsic value at the expiration close |
| `spy_move` | For `CASH`: the SPY move |
| `pending` | The date has not been reached |

With two runs a day the `market` value is the last mark recorded that day (`core/positions.py` `_market_mark`), the
one closest to the close.

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

**Record tickets for runs that have none.** `python main_pipeline.py backfill-positions` reads the stored snapshot of
every committed live run and records the engine ticket (including `CASH`) for each run that has no row yet, by the
same rule as a live run (`positions.backfill`). It is idempotent: a run that already has a row is left alone, so it is
safe to repeat. It takes the run lock and prints how many new rows it recorded. Use it when a run's `trade_signal`
stage failed (the run's `stages_json` has `trade_signal_error`) or after importing runs made by a version that did not
record positions.

## Menu bar icon

`menubar/optionswhale.10s.sh` is a SwiftBar plugin that refreshes every 10 seconds. It watches the terminal server
only; it shows nothing about the pipeline, the schedule or runs. Keep only plugins in the `menubar/` folder. It
reads the port from `OPTIONS_WHALE_PORT` in `.env`, the same way `setup.sh` does; a blank or missing value means 8080.

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

**Install and uninstall.** If SwiftBar has no plugin folder yet, `setup.sh` points it at `menubar/`. If SwiftBar
already uses another folder, `setup.sh` symlinks the plugin into it instead. `./setup.sh --uninstall` undoes
whichever it did: it clears the plugin-folder setting, or removes that symlink when it points at this repository's
plugin. A link that points elsewhere, or a regular file with the same name, is left alone.

## Logs and troubleshooting

### Where to look

| What | Where |
|---|---|
| Terminal server output | `~/Library/Logs/optionswhale.log` (menu bar: **Open log**) |
| Scheduled run output, including "Scheduled run skipped" lines | `~/Library/Logs/marketdashboard.log` |
| A hand run | The terminal you ran it in. Runs started by the Run button append to `.v2_manual_run.log` in the data folder. |
| Recent runs and delivery state | `python main_pipeline.py status` |
| Why a run failed or warned | `v2_runs.error`, `stages_json` and `delivery_detail` for the run (see [Data](data.md)) |
| Whether a run is active | `python main_pipeline.py status` (a killed run shows `interrupted`), or `.v2_run_status.json` in the data folder |

### Symptoms

| Symptom | Cause | Fix |
|---|---|---|
| `BUSY: run <id> is in progress` (exit 75) | Another job holds the lock: a run (scheduled and manual overlap, or a CME login wait), `cme-login`, `import-history`, `backfill-positions` or a download script. | Run `status` to see the stage. Wait for it, or finish the login. The lock frees itself if the process dies. |
| A scheduled run did nothing | It was skipped. | `grep "skipped" ~/Library/Logs/marketdashboard.log`. The reason is one of the three in [Scheduling](#scheduling). |
| No scheduled run and no log line | The Mac was asleep or logged out, or the job is not loaded. | `launchctl print gui/$(id -u)/com.vlad.marketdashboard`. Re-run `./setup.sh --schedule`. Stop idle sleep ([Setting up a Mac](#setting-up-a-mac), step 5). |
| Position stamped at a strange time | A late launchd fire inside the window ran. | Expected (see [Scheduling](#scheduling)). |
| `./run_dashboard.command` does nothing on a Mac | Its default trigger is `scheduled`. | Use `./run_dashboard.command manual`. |
| A Chromium window opened and the run waits | CME refused a download, so it waits for login. | Finish login and MFA in that window. To avoid the wait use `--login-wait 0` or `--skip-cme`. |
| `CME session missing/expired: run python main_pipeline.py cme-login` | The login did not complete in time or the session expired. | `python main_pipeline.py cme-login`, then run again. |
| CME section marked `cached` or `stale` | No new workbook this run (see [CME login](#cme-login)). | Check the CME outcome in the log; log in; drop missing workbooks into the data folder. |
| Menu bar icon red, or `Server: installed but NOT responding` | The port is taken, or the server failed to start. | Set `OPTIONS_WHALE_PORT` if needed, run `./setup.sh`, read `optionswhale.log`. |
| `./setup.sh` exits 1 | The server is not up at the end (also the normal first pass). | Read the summary; fill in `.env`; run it again. |
| Run ends `completed_with_warnings` (exit 3) | A stage failed, a ledger step failed, or a configured delivery channel failed (email, ntfy push, upload). | `status`, then the run's `error` text and `stages_json`. Fix `.env`, then `resend`. A channel with no settings at all is skipped and never causes this. |
| Email status `outcome_unknown` | The connection dropped during the send. | Check the mailbox. Only then `resend`. |
| No phone push | `NTFY_URL` empty (recorded as `skipped`), `--no-deliver`, or ntfy unreachable. | `python main_pipeline.py ntfy-test` (exit 2 when nothing was sent). The run's `delivery_detail` shows the ntfy result. |
| `status` shows `interrupted` | The run was killed before it could record its end. | Start a new run: the lock is already free, and the run marks the old row `interrupted`. |
| `ConfigError: Two CME_Data installations found` | Both the sibling and the Desktop folder hold a `portfolio.db`. | Set `PORTFOLIO_DATA_DIR` in `.env`. |
| `Data directory ... does not exist` | `PORTFOLIO_DATA_DIR` points at a missing folder. | Create it or fix the path. |
| `ConfigError: SMTP_PORT must be a whole number` as soon as any command starts | `SMTP_PORT` (or `OPTIONS_WHALE_PORT`) in `.env` is text, or outside 1 to 65535. An empty value is fine and means the default. | Fix or blank the line. |
| Run button reports that a run is in progress | The run lock is held (HTTP 409). | `status`. |
| An engine position is missing for a run | The run was offline, had no score, had no selectable contract, or the `trade_signal` stage failed. | `status`; the run's `stages_json` has `trade_signal` (or `trade_signal_error`). Then `backfill-positions`. |
| Traceback and exit 1 | The run failed before the snapshot was committed. Earlier snapshots are not affected. | Read the error in `v2_runs`; fix the cause; run again. |

## Backups and rollback

This repository contains only the current (v2) code; its history starts at v2, so there is no legacy branch or
launcher to go back to. The data folder is additive: v2 adds `v2_*` tables and views to `portfolio.db` and new files
to `CME_Data`, and keeps the legacy ledger tables and CSV headers unchanged. See [Data](data.md).

- **`import-history` backs the database up for you.** Before it imports it copies `portfolio.db` to
  `backups/portfolio-<UTC time>.db` with the SQLite backup API and checks the copy (`lake.backup_database()`). If the
  backup fails nothing is imported and the command exits 1; `--no-backup` skips the copy. Backups are never pruned or
  rotated: delete old ones by hand. They hold only the database, not the ledger CSVs or `state.json`.
- **Back up `CME_Data` before any other large change.** Copy the whole folder (`portfolio.db`, the ledger CSVs,
  `state.json`) while no run is in progress. The database uses the default rollback journal, so a copy between runs
  is complete.
- **Go back to earlier code** by checking out an earlier commit of this repository. Newer code may add tables or
  columns that older code does not know about, so keep the copy.
- **Snapshots are immutable.** A wrong value is fixed by changing the model and making a new run, and recorded in
  [Value changes](value-changes.md).
- **Undo a schedule** with `./setup.sh --remove-schedule`.


## Other scripts

`download_volume.py` and `update_inventory.py` (when it downloads) take the run lock and exit 75 when it is held; the
others do not. The compatibility renderers print or write from the latest committed snapshot and make no requests; they need at least one committed run ("No committed v2 snapshot yet. Run: python
main_pipeline.py run"). The full repository map is in [Architecture](architecture.md#repository-map).

| Script | Use |
|---|---|
| `download_volume.py [N]` | Bounded CME volume download outside a run (N defaults to 10, kept between 1 and 40). Takes the run lock. Like a run, it opens the login window and waits up to 15 minutes if CME refuses, and sends no phone push. |
| `update_inventory.py` | Downloads the COMEX silver inventory workbook only (takes the run lock) and prints the tail of the inventory history. |
| `scripts/backfill_crypto_metals.py` | Adds missing daily BTC, silver and gold rows (past year, from Yahoo) to `crypto_metrics_history`. Existing rows are never replaced. Needs network. |
| `scripts/import_email_positions.py` | Recovers positions from sent report emails (see [Engine positions](#engine-positions)). |
| `scripts/dump_spy_wicks.py` | Writes SPY one-minute candles for the last 5 days to `spy_wicks_1m.json` in the repository root (yfinance, needs network; gitignored). Nothing in the pipeline runs it; `import-history` captures the file if it is there. |
| `send_email.py` | Run as a script, resends the latest run's saved email. |
| `upload_data.py` | Run as a script, uploads the database copy and today's dashboard file (see [Delivery](#delivery)). |
| `dump_data.py` | Writes `volume_dashboard.txt` (XML) for the latest snapshot into its day folder. |
| `tactical_ruling.py` | Prints the tactical ruling XML for the latest snapshot. |
| `market_reader.py` | Prints the Morning Market Brief text for the latest snapshot. |
| `institutional_scanner.py` | Prints the SPY and SLV institutional scan from the latest committed results. |
| `parse_volume.py [DAYS]` | Prints the legacy CME volume table from the lake (DAYS defaults to 30). |

## Signal alerts and the midday run

- **Three scheduled runs.** `core/market_calendar.SCHEDULE_ET` is 09:31, 12:30 and 15:45 ET. The midday run refreshes
  the engine ticket and the levels the rule cards read. On a 13:00 ET early close the 15:45 run is skipped; the
  12:30 run still happens.
- **`main_pipeline.py signal-alerts`** checks the Signal Watch rows, the Day Scanner's tested tickers and (in the
  last 30 minutes of the session) the Edge Lab's tested edges. A row that went from waiting to FIRED since the last
  check is pushed to `NTFY_URL` and raised as a macOS banner; one alert per row per day. A fired SPY dip also opens a
  paper position (`v2_watch_positions`); five trading days later the same command sends the sell alert and closes
  it. It exits at once outside the regular NYSE session (09:30 ET to the close, 13:00 ET on an early close); the
  dip rule and the exits are judged in the session's last 30 minutes. Nothing is recorded (alert state, a position
  opened or closed) until the push was delivered, so a failed push is sent again by the next check; one ticker or
  one position failing does not stop the others. A ticker whose earnings date cannot be fetched counts as blocked.
  The engine row is the latest run's own ticket, never an older run's. `--dry-run` prints the live states and sends
  nothing; `--test` sends one sample alert (it pushes to the phone, so it is an operator command, not a check).
- **launchd job `com.vlad.signalalerts`** runs it every 15 minutes of the regular session, on weekdays only
  (`core/watch.ALERT_SLOTS_ET`, 09:35 to 15:50 ET; log `~/Library/Logs/signalalerts.log`). Daily price history is
  downloaded once per ticker per day into `<data folder>/cache/` and shared by the job, the Day Scanner and the Edge Lab.
  `./setup.sh --schedule` installs it with the pipeline schedule; `--remove-schedule` removes both.
