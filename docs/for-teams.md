# For funds, desks and teams

What you need to know before you run Market Terminal for more than one person, or plug it into an existing stack.

## What it is, and what it is not

It is a self-hosted collection and modelling pipeline with an append-only SQLite store and a read-only web terminal.
It keeps a point-in-time record of market stress, options positioning, rates and funding, energy and silver, along
with the raw responses behind it.

It is **not** an execution system, an order router or a licensed data feed. It places no trades. Its tickets are
recorded and graded, never sent anywhere.

## Integration points

| Need | Use |
|---|---|
| Research notebooks | `main_pipeline.py export` to CSV, or read `portfolio.db` read-only ([For quants](for-quants.md#getting-the-data-out)) |
| Dashboards, internal tools | The HTTP JSON API ([API](api.md)), mostly stored data, with a few live routes that are marked as such |
| LLM agents | The MCP server: read-only sources, docs and model code ([Operations](operations.md#mcp-server-claude-chat-chatgpt-gemini-cli)) |
| Alerts | ntfy push (`NTFY_URL`) and email, from the pipeline and `signal-alerts` ([Operations → Delivery](operations.md#delivery)) |
| Audit | `v2_runs` (run status, git hash, stage timings), `v2_fetch_log` (every provider request) and the immutable `v2_snapshots` |

## Deployment

- **Today:** one macOS host. launchd runs the schedule and the server, and a menu bar control shows its state
  ([Operations](operations.md)). The tests and offline runs also work on Linux, and CI runs them there.
- **One host owns the schedule.** Run `./setup.sh --schedule` on exactly one machine. A run lock (exit code 75)
  prevents overlapping runs.
- **Linux server:** [plan-oracle-move.md](plan-oracle-move.md) describes the move. The CME download needs a
  headed browser, so a Linux host needs a virtual display such as Xvfb.

## Security model

- The terminal server **has no authentication**. It listens on `127.0.0.1` by default. Put it behind a private
  network such as a VPN or Tailscale, or behind an authenticating reverse proxy. Never expose it to the internet.
- Secrets come from `.env` or the system keychain through `config.py`. They are never stored in the repository, and
  credentials are removed from captured responses before hashing.
- The MCP server protects each endpoint with its own secret path segment, so a market-data link can be shared
  without the holdings link.
- [API → Security model](api.md#security-model) lists the routes that write, which should never be exposed.

## Data licensing

The repository contains code only. Each deployment collects its own data with its own credentials. CME (account
login), Databento (paid key) and eBay (developer keys) have their own terms on storing and redistributing their data,
and those terms apply to your installation. Most other sources are free public data (FRED, EIA, CFTC, NY Fed,
Treasury, SEC, FINRA, CBOE daily statistics), but check each provider's terms for your use.

## Engineering standards

- **Offline tests.** Any network call during a test raises `NetworkBlocked`. CI runs the suite on every push, once a
  day, and a second time with the clock moved 400 days ahead, which catches date-dependent tests before they fail.
- **Additive schema.** Nothing is dropped, renamed or deleted. Snapshots are protected by database triggers.
- **Versioned definitions.** A computed value that changes meaning gets a new version and a line in
  [value-changes](value-changes.md).
- **AI-assisted development.** [AGENTS.md](../AGENTS.md) holds the ten hard rules that Claude Code, Codex and Cursor
  follow in this repository. The rules that can be checked by a machine are checked in CI.

## Extending it

- New data source: `core/sources.py` (through `SourceSession.fetch()`, so the call is bounded, logged, captured and
  switched off in offline runs), called from `core/collect.py`.
- New model: `core/` and the snapshot, then a card ([Forecast Lab → Adding a card](forecast-lab.md#adding-a-forecast-lab-card)).
- The full repository map is in [Architecture](architecture.md#repository-map).
