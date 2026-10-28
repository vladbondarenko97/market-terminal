# Plan: move the terminal to the Oracle server

Status: **not started**. Written 2026-10-28. Nothing here has been done yet.

Goal: the pipeline, the terminal and the data run on the Oracle server (the one that hosts Brinewater). The Mac
mini keeps a read-only copy of the data.

## Does it fit?

Measured on the Oracle server on 2026-10-28 (Always Free, Arm, `ubuntu@brinewater` over Tailscale):

| | Oracle server now | The terminal would add |
|---|---|---|
| Memory | 780 MB used of 11 GB | Under 1 GB normally; up to about 1.5 GB for a few minutes per run while Playwright's Chromium reads CME |
| Disk | 19 GB used, 126 GB free | The data folder (`CME_Data`) is 781 MB (`portfolio.db` 373 MB) and grows slowly |
| CPU | 2 Arm cores, almost idle (the game server is light) | A short burst twice a day |

The Brinewater game server and the terminal fit together comfortably.

## What has to change

1. **Secrets.** `config.py` reads keys from the macOS Keychain (`scripts/sync_secrets.sh`). On Linux that step is
   skipped, so the keys go in an `.env` file readable only by the service user (`chmod 600`).
2. **CME silver data: the main risk.** `core/cme.py` opens CME's website in headless Chromium (Playwright). CME's
   bot protection often blocks requests from datacenter IPs such as Oracle's. Test this first (step 1 below).
3. **Mac-only extras.** The desktop notification (`osascript` in `main_pipeline.py`) and the SwiftBar menu-bar
   widget (`menubar/`) only work on a Mac. Email and the phone push work anywhere.
4. **Scheduling and access.** The launchd jobs (`setup.sh`) become systemd services and timers, the same way
   Brinewater runs on that server (`brinewater/docs/VPS.md`). Caddy and Tailscale there serve the terminal. The MCP
   server (`mcp_server.py`, launchd `com.vlad.mcpserver`, port 8791) moves too, or is pointed at the new address.

Python dependencies work on Arm Linux (yfinance, databento, Playwright with its own Chromium).

## Docker: not needed

Docker is not installed on the server, and Brinewater already runs there as plain systemd services. A venv plus
systemd units matches what is already there and means less to install. Docker would not fix any of the four items
above.

## The Mac's copy of the data

A nightly job on the server makes a consistent copy (`sqlite3 portfolio.db ".backup ..."`, never a plain copy of a
live database) and sends it, with the dated run folders, to the Mac over Tailscale (`rsync`). The Mac's copy is
read-only; the Mac can still run a terminal against it, and it doubles as an off-server backup.

## Steps

1. **Test CME from the server.** Install the repo and Playwright in a scratch folder and run only the CME
   collection, with a scratch `PORTFOLIO_DATA_DIR` and no delivery. If CME blocks it, stop and
   decide: keep CME collection on the Mac and send its results to the server (more complicated), or stay on the
   Mac.
2. **Run in parallel.** Set up the service on the server with a copy of the database and runs set to
   `--no-deliver`, while the Mac stays the live machine. Let it run a few days and compare its snapshots with the
   Mac's.
3. **Cut over in one go.** Stop the Mac's scheduled runs (launchd) first. Take a fresh `.backup` of the Mac's
   `portfolio.db` and copy it to the server. Turn delivery on at the server. Point Caddy and the MCP server at it.
   Start the nightly copy back to the Mac.
4. **Rollback.** The Mac's launchd setup stays installed, only stopped. Going back means stopping the server's
   timers and restarting the Mac's jobs.

**Never have both machines running delivering pipelines at the same time.** That sends duplicate emails and pushes,
and leaves two databases drifting apart.

## Owner actions

These are the operator's, not an agent's: launchctl, the server's `.env` with real keys, copying the real database,
Oracle and DNS settings, and turning delivery on.
