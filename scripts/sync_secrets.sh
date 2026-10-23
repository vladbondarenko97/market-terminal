#!/bin/zsh
# Bitwarden <-> macOS Keychain <-> .env for this project's secrets. Names: SECRET_NAMES in config.py.
# Bitwarden holds one Login item per name (item name = variable name, value in the password field).
#   export BW_SESSION=$(bw unlock --raw)
#   scripts/sync_secrets.sh push    # .env values -> Bitwarden items (created if missing, never overwritten)
#   scripts/sync_secrets.sh pull    # Bitwarden items -> Keychain (service market-terminal), which config.py reads at import
#   scripts/sync_secrets.sh strip   # blank in .env every name the Keychain holds (keeps .env.bak.secrets)
# Re-run pull after you rotate a key. Touches only Bitwarden, the Keychain and .env; sends nothing anywhere else.
set -eu
cd "$(dirname "$0")/.."
ENVF=$(realpath .env)   # follow a symlinked .env so strip edits the real file
NAMES=($(MT_NO_KEYCHAIN=1 .venv/bin/python -c 'import config; print(*config.SECRET_NAMES)'))

case "${1:-}" in
push)
  for n in $NAMES; do
    v=$(sed -nE "s/^$n=//p" .env | tail -1)
    [ -n "$v" ] || continue
    if bw get item "$n" >/dev/null 2>&1; then echo "exists:  $n"; continue; fi
    bw get template item | jq --arg n "$n" --arg v "$v" '.type=1|.name=$n|.login={username:null,password:$v,uris:[]}' | bw encode | bw create item >/dev/null && echo "created: $n"
  done ;;
pull)
  for n in $NAMES; do
    v=$(bw get password "$n" 2>/dev/null) || continue
    security add-generic-password -U -s market-terminal -a "$n" -w "$v" && echo "keychain: $n"
  done ;;
strip)
  cp "$ENVF" "$ENVF.bak.secrets" && chmod 600 "$ENVF.bak.secrets"
  for n in $NAMES; do
    security find-generic-password -s market-terminal -a "$n" -w >/dev/null 2>&1 || continue
    sed -i '' -E "s/^$n=.*/$n=/" "$ENVF" && echo "blanked: $n"
  done
  echo "old .env saved as .env.bak.secrets next to .env; delete it once the services run fine" ;;
*) echo "usage: scripts/sync_secrets.sh push|pull|strip"; exit 2 ;;
esac
