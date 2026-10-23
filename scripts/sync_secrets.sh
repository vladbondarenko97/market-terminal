#!/bin/zsh
# Bitwarden <-> macOS Keychain for this project's secrets (names: SECRET_NAMES in config.py).
#   BW_SESSION=$(bw unlock --raw) scripts/sync_secrets.sh push   # .env -> Bitwarden item "market-terminal" (custom hidden fields)
#   BW_SESSION=$(bw unlock --raw) scripts/sync_secrets.sh pull   # Bitwarden item -> Keychain, which config.py reads at import
#   scripts/sync_secrets.sh strip                                # blank those names in .env (run after pull; keeps a .env.bak.secrets copy)
# Re-run pull after you rotate a key. Reads and writes secrets only; sends nothing anywhere else.
set -eu
cd "$(dirname "$0")/.."
ITEM=market-terminal
NAMES=($(.venv/bin/python -c 'import os; os.environ["MT_NO_KEYCHAIN"]="1"; import config; print(*config.SECRET_NAMES)'))

case "${1:-}" in
push)
  fields='[]'
  for n in $NAMES; do
    v=$(sed -nE "s/^$n=//p" .env | tail -1)
    [ -n "$v" ] && fields=$(jq --arg n "$n" --arg v "$v" '. + [{name:$n,value:$v,type:1}]' <<<"$fields")
  done
  id=$(bw get item "$ITEM" 2>/dev/null | jq -r .id || true)
  if [ -n "$id" ]; then   # merge: keep fields already in the vault that .env no longer has
    old=$(bw get item "$ITEM" | jq -c '.fields // []')
    fields=$(jq -c --argjson new "$fields" '[.[] | select(.name as $n | $new | map(.name) | index($n) | not)] + $new' <<<"$old")
    bw get item "$ITEM" | jq --argjson f "$fields" '.fields=$f' | bw encode | bw edit item "$id" >/dev/null
  else
    bw get template item | jq --arg n "$ITEM" --argjson f "$fields" '.type=2|.secureNote={type:0}|.name=$n|.notes="Managed by scripts/sync_secrets.sh"|.fields=$f' | bw encode | bw create item >/dev/null
  fi
  echo "pushed $(jq length <<<"$fields") fields to Bitwarden item $ITEM" ;;
pull)
  bw get item "$ITEM" | jq -r '.fields[] | [.name,.value] | @tsv' | while IFS=$'\t' read -r n v; do
    (( ${NAMES[(Ie)$n]} )) || { echo "skip unknown field $n"; continue; }
    security add-generic-password -U -s market-terminal -a "$n" -w "$v" && echo "keychain: $n"
  done ;;
strip)
  cp .env .env.bak.secrets && chmod 600 .env.bak.secrets
  for n in $NAMES; do
    security find-generic-password -s market-terminal -a "$n" -w >/dev/null 2>&1 || continue   # only blank what the Keychain holds
    sed -i '' -E "s/^$n=.*/$n=/" .env && echo "blanked $n"
  done
  echo "backup of the old .env: .env.bak.secrets (delete it once the services run fine)" ;;
*) echo "usage: scripts/sync_secrets.sh push|pull|strip"; exit 2 ;;
esac
