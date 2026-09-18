#!/usr/bin/env bash
# =============================================================
# Push the fee-payer relay's three parameters into SSM under /gecko-relay/ as
# SecureString, from examples/kora_demo/.env (gitignored). Values are never printed;
# only names, versions and the relay's PUBLIC key (which you must fund).
#
# Usage:
#   ./infra/push-relay-params.sh [--region us-east-2] [--env-file examples/kora_demo/.env] [--dry-run]
#
# What it fills in when the env file leaves a value empty, and writes back to the env
# file (mode 600) so the operator keeps a copy:
#   KORA_API_KEY    a fresh 32-byte urlsafe token — Gecko presents it in x-api-key
#   KORA_RELAY_KEY  a fresh keypair (base58 of the 64 bytes, what Kora's memory signer
#                   reads). Generated HERE, on the operator's machine, never on the relay.
#                   The public key is printed: fund it with what you can afford to lose;
#                   the balance is the cap.
#   RPC_URL         from HELIUS_API_KEY in the repo's .env if present
#
# The task definition (infra/ecs-stack.yml, RelayTaskDef) reads exactly these three via
# `Secrets:`; a missing one fails the task at boot, which is why infra/deploy-relay.sh
# checks they exist before deploying. Sibling of infra/push-ssm-params.sh (the MCP's).
# =============================================================
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-2}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/examples/kora_demo/.env"
DRY_RUN=0
SSM_PREFIX="/gecko-relay"

while [[ $# -gt 0 ]]; do
  case $1 in
    --region)   REGION="$2";   shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=1;     shift   ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$REPO_ROOT/examples/kora_demo/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "==> created $ENV_FILE from .env.example (mode 600)"
fi
chmod 600 "$ENV_FILE"

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a
RPC_URL="${RPC_URL:-}"; KORA_API_KEY="${KORA_API_KEY:-}"; KORA_RELAY_KEY="${KORA_RELAY_KEY:-}"

# Write one KEY=value line back into the env file without echoing the value.
set_env() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    # python keeps the value opaque to the shell and to sed's delimiter rules
    python3 - "$ENV_FILE" "$key" "$value" > "$tmp" <<'PY'
import sys
path, key, value = sys.argv[1:]
for line in open(path).read().splitlines():
    print(f"{key}={value}" if line.startswith(f"{key}=") else line)
PY
  else
    cat "$ENV_FILE" > "$tmp"; printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
}

# --- fill the blanks, on this machine ------------------------------------------------
if [[ -z "$RPC_URL" ]]; then
  HELIUS="$(grep -E '^HELIUS_API_KEY=' "$REPO_ROOT/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)"
  if [[ -n "$HELIUS" ]]; then
    RPC_URL="https://mainnet.helius-rpc.com/?api-key=${HELIUS}"
    echo "==> RPC_URL: derived from the repo's HELIUS_API_KEY (not printed)"
  else
    echo "ERROR: RPC_URL is empty in $ENV_FILE and no HELIUS_API_KEY in $REPO_ROOT/.env" >&2; exit 1
  fi
  [[ "$DRY_RUN" -eq 0 ]] && set_env RPC_URL "$RPC_URL"
fi
if [[ -z "$KORA_API_KEY" ]]; then
  KORA_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  echo "==> KORA_API_KEY: generated (32 bytes, urlsafe); kept in $ENV_FILE"
  [[ "$DRY_RUN" -eq 0 ]] && set_env KORA_API_KEY "$KORA_API_KEY"
fi
if [[ -z "$KORA_RELAY_KEY" ]]; then
  # base58 of the 64-byte keypair, which is what `str(Keypair)` gives (88 chars) and what
  # Kora's memory signer expects. The secret goes to the env file and SSM only.
  read -r KORA_RELAY_KEY RELAY_PUBKEY < <(cd "$REPO_ROOT" && uv run --quiet python -c \
    'from solders.keypair import Keypair; k = Keypair(); print(str(k), k.pubkey())' 2>/dev/null)
  echo "==> KORA_RELAY_KEY: generated here. RELAY PUBLIC KEY (fund this, nothing else): $RELAY_PUBKEY"
  [[ "$DRY_RUN" -eq 0 ]] && set_env KORA_RELAY_KEY "$KORA_RELAY_KEY"
else
  RELAY_PUBKEY="$(cd "$REPO_ROOT" && KORA_RELAY_KEY="$KORA_RELAY_KEY" uv run --quiet python -c \
    'import os; from solders.keypair import Keypair; print(Keypair.from_base58_string(os.environ["KORA_RELAY_KEY"]).pubkey())' 2>/dev/null || echo "?")"
  echo "==> KORA_RELAY_KEY: from $ENV_FILE. RELAY PUBLIC KEY: $RELAY_PUBKEY"
fi

# --- push, values via temp file so the CLI never parses them as arguments ---------------
put_param() {
  local pname="$1" pvalue="$2" tmp version
  pvalue="${pvalue%$'\n'}"; pvalue="${pvalue%$'\r'}"
  if [[ "$DRY_RUN" -eq 1 ]]; then echo "  WOULD PUSH  $SSM_PREFIX/$pname  (${#pvalue} chars)"; return 0; fi
  tmp="$(mktemp)"; printf '%s' "$pvalue" > "$tmp"
  if version="$(aws ssm put-parameter --name "${SSM_PREFIX}/${pname}" --value "file://${tmp}" \
        --type SecureString --overwrite --region "$REGION" --output text --query 'Version' 2>&1)"; then
    rm -f "$tmp"; echo "  OK    $SSM_PREFIX/$pname  (version ${version})"; return 0
  fi
  rm -f "$tmp"; echo "  FAIL  $SSM_PREFIX/$pname  (${version})" >&2; return 1
}

echo "==> Pushing to SSM ($REGION) under $SSM_PREFIX/ ..."
put_param KORA_RELAY_KEY "$KORA_RELAY_KEY"
put_param KORA_API_KEY   "$KORA_API_KEY"
put_param RPC_URL        "$RPC_URL"

if [[ "$DRY_RUN" -eq 0 ]]; then
  echo "==> Verifying (names and versions only):"
  aws ssm get-parameters-by-path --path "$SSM_PREFIX" --region "$REGION" \
    --query 'Parameters[].[Name,Version,Type]' --output table
fi
echo ""
echo "Next:"
echo "  ./infra/deploy-relay.sh                              # build Kora, push the image, deploy the service"
echo "  KORA_RPC_URL=https://mcp.geckovision.tech:8443/ KORA_API_KEY=... ./infra/probe-relay.sh"
echo "  fund ${RELAY_PUBKEY:-<relay pubkey>} with what you can afford to lose; the balance is the cap"
