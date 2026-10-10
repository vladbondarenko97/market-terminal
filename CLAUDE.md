# CLAUDE.md

The working rules for this repository live in [AGENTS.md](AGENTS.md), shared by every coding agent. Claude Code
loads them through the import below.

@AGENTS.md

## Claude Code notes

- Delegate broad read-only searches across `core/` and `options_whale/` to a subagent; `api_router.py` (2,300+
  lines) and `app.js` (3,500+ lines) are large, so read them by function name, not whole.
- The code is the authority. If a doc disagrees with the code, fix the doc in the same change (see "Before you
  commit" in AGENTS.md for which page owns which topic).
