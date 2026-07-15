#!/usr/bin/env bash
# =============================================================
# Push gecko-surf hosted-MCP secrets/config from a local .env (or environment)
# into AWS SSM Parameter Store as SecureString. Values are never printed — only
# the parameter name and result status. (Ported from gecko-mcpay-api's
# battle-hardened push-ssm-params.sh; same sentinel + verify machinery.)
#
# Usage:
#   ./infra/push-ssm-params.sh [--region us-east-2] [--env-file .env] [--dry-run]
#
#   --dry-run   Show what WOULD push (real value vs sentinel) per param, but
#               write NOTHING. Use before a real run.
#
# After pushing, the ECS task must reference the params via `secrets:`
# ValueFrom in infra/ecs-stack.yml (+ ssm:GetParameters on the task execution
# role), then:
#   aws ecs update-service --cluster surfcall --service surfcall \
#     --force-new-deployment --region us-east-2
# =============================================================
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-2}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case $1 in
    --region)   REGION="$2";   shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=1;     shift   ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# Sentinel the runtime treats as "truly unset": gecko/events.py `_mongo_uri()`
# returns None for `__unset__`, so a sentinel-provisioned task boots clean and
# simply doesn't emit — no phone-home until the founder sets a real URI.
SENTINEL="__unset__"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: env file '$ENV_FILE' not found" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

SSM_PREFIX="/gecko-mcp"

# All hosted-MCP secrets/config in one place. SSM param name on the left, shell
# variable name on the right. Everything goes in as SecureString — no harm in
# encrypting non-secret config, and it keeps the deploy uniform.
#
# ONLY list params the engine actually reads (grep os.environ over gecko/):
# adding aspirational params here just creates config nobody consumes.
declare -A PARAMS=(
  # Usage events sink (gecko/events.py) — surf.search/prepare/call land in
  # gecko_events.surf_events. `__unset__` or absent -> the sink is a no-op.
  # THIS is the switch that turns hosted usage visibility on.
  [MONGODB_URI]="MONGODB_URI"

  # Kill-switch (gecko/telemetry.py + events.py): "off" hard-disables emission
  # even with a live sink. Anything else leaves current behavior.
  [GECKO_TELEMETRY]="GECKO_TELEMETRY"

  # x402 settlement (gecko/x402_pay.py + x402_facilitator.py). MODE `stub`
  # (or the `__unset__` sentinel) = FakeFacilitator, no real USDC — the safe
  # default. `live` requires the four config params below; the factory raises
  # X402ConfigError naming any that are missing/sentinel. Go-live sequence:
  # docs/x402-go-live.md (founder-run smoke, staged).
  [X402_MODE]="X402_MODE"
  [X402_FACILITATOR_URL]="X402_FACILITATOR_URL"    # e.g. PayAI's facilitator; SSRF-validated at boot of the client
  [X402_FACILITATOR_TOKEN]="X402_FACILITATOR_TOKEN" # optional bearer; sentinel = none
  [X402_PAY_TO]="X402_PAY_TO"                      # treasury address USDC lands in (founder's)
  [X402_ASSET]="X402_ASSET"                        # USDC mint (Solana mainnet: EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v)
  [X402_NETWORK]="X402_NETWORK"                    # x402 network id, e.g. `solana`
)

echo "==> Region:     $REGION"
echo "==> SSM prefix: $SSM_PREFIX"
echo "==> Env file:   $ENV_FILE"
echo ""

# Params the ECS task references as `secrets:` ValueFrom — these MUST exist in
# SSM even if empty, or the task fails at start with
# ResourceInitializationError ("invalid ssm parameters"). For these, push a
# sentinel when the env var is empty; the runtime treats the sentinel as unset.
declare -A REQUIRED_AT_BOOT=(
  # Sentinel keeps the task booting (and silent) before Mongo is wired.
  [MONGODB_URI]="__unset__"
  # "on" = current default behavior (anything but "off"); flip to "off" in SSM
  # + force-new-deployment for an instant kill without a rebuild.
  [GECKO_TELEMETRY]="on"
  # x402: every wired Secret must exist or the task fails at boot. "stub" is
  # the safe mode default; the sentinel values are treated as unset by the
  # engine (x402_mode / facilitator_from_env), so a half-configured deploy
  # boots clean in stub and can never settle real funds by accident.
  [X402_MODE]="stub"
  [X402_FACILITATOR_URL]="__unset__"
  [X402_FACILITATOR_TOKEN]="__unset__"
  [X402_PAY_TO]="__unset__"
  [X402_ASSET]="__unset__"
  [X402_NETWORK]="__unset__"
)

SKIPPED=()
PUSHED=()
PLACEHOLDED=()
FAILED=()
# Track every param we INTENDED to have a real (non-sentinel) value, so the
# post-push verify step can flag any that silently landed as a sentinel.
declare -A INTENDED_REAL=()

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "==> DRY RUN — no writes. Showing what WOULD push (real vs sentinel):"
  echo ""
fi

# Single put-parameter call. Robust to values with leading dashes, special
# chars, and trailing newlines: the value goes via a temp file (file://...) so
# the AWS CLI never parses it as an argument. Never echoes the value.
put_param() {
  local pname="$1" pvalue="$2"
  local tmp version
  tmp="$(mktemp)"
  printf '%s' "$pvalue" > "$tmp"
  if version="$(aws ssm put-parameter \
        --name "${SSM_PREFIX}/${pname}" \
        --value "file://${tmp}" \
        --type SecureString \
        --overwrite \
        --region "$REGION" \
        --output text \
        --query 'Version' 2>&1)"; then
    rm -f "$tmp"
    echo "  OK    $SSM_PREFIX/$pname  (version ${version})"
    return 0
  fi
  rm -f "$tmp"
  # On failure `version` holds the AWS error text, never our value (the value
  # went via the temp file, not the command line).
  echo "  FAIL  $SSM_PREFIX/$pname  (${version})" >&2
  return 1
}

for PARAM_NAME in "${!PARAMS[@]}"; do
  VAR_NAME="${PARAMS[$PARAM_NAME]}"
  VALUE="${!VAR_NAME:-}"
  # Strip a trailing newline/CR that `source .env` can carry in — a stray \n
  # inside a SecureString breaks downstream auth and CLI arg parsing.
  VALUE="${VALUE%$'\n'}"
  VALUE="${VALUE%$'\r'}"

  IS_SENTINEL=0
  if [[ -z "$VALUE" ]]; then
    if [[ -n "${REQUIRED_AT_BOOT[$PARAM_NAME]:-}" ]]; then
      VALUE="${REQUIRED_AT_BOOT[$PARAM_NAME]}"
      [[ "$VALUE" == "$SENTINEL" ]] && IS_SENTINEL=1
      echo "  PLACEHOLDER  $SSM_PREFIX/$PARAM_NAME  (${VAR_NAME} empty; pushing '$VALUE')"
      PLACEHOLDED+=("$PARAM_NAME")
    else
      echo "  SKIP  $SSM_PREFIX/$PARAM_NAME  (${VAR_NAME} is empty in $ENV_FILE)"
      SKIPPED+=("$PARAM_NAME")
      continue
    fi
  else
    [[ "$VALUE" == "$SENTINEL" ]] && IS_SENTINEL=1
    if [[ "$IS_SENTINEL" -eq 0 ]]; then
      INTENDED_REAL[$PARAM_NAME]=1
    fi
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    if [[ "$IS_SENTINEL" -eq 1 ]]; then
      echo "  WOULD-PUSH (sentinel)  $SSM_PREFIX/$PARAM_NAME"
    else
      echo "  WOULD-PUSH (REAL)      $SSM_PREFIX/$PARAM_NAME"
    fi
    continue
  fi

  # Never halt on a single failure — collect and continue, so a mid-list error
  # can't silently skip later params.
  if put_param "$PARAM_NAME" "$VALUE"; then
    PUSHED+=("$PARAM_NAME")
  else
    FAILED+=("$PARAM_NAME")
  fi
done

echo ""
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "==> DRY RUN complete. Nothing written."
else
  echo "==> Done. ${#PUSHED[@]} pushed, ${#PLACEHOLDED[@]} placeholders, ${#SKIPPED[@]} skipped, ${#FAILED[@]} FAILED."
fi
if [[ ${#PLACEHOLDED[@]} -gt 0 ]]; then
  echo "    Placeholder sentinels (set real values via .env or aws ssm put-parameter):"
  for P in "${PLACEHOLDED[@]}"; do echo "      - $SSM_PREFIX/$P"; done
fi

if [[ ${#SKIPPED[@]} -gt 0 ]]; then
  echo ""
  echo "Skipped (fill in $ENV_FILE and re-run):"
  for P in "${SKIPPED[@]}"; do
    echo "  - $SSM_PREFIX/$P"
  done
fi

if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo ""
  echo "FAILED (re-run; these did NOT land in SSM):" >&2
  for P in "${FAILED[@]}"; do
    echo "  - $SSM_PREFIX/$P" >&2
  done
fi

# ----------------------------------------------------------------------------
# Post-push VERIFY — read back each param's presence + whether its value is the
# sentinel. NEVER prints the decrypted value: it's fetched only to compare
# against the sentinel string locally and emit a boolean.
# ----------------------------------------------------------------------------
if [[ "$DRY_RUN" -eq 0 ]]; then
  echo ""
  echo "==> VERIFY (presence + is-sentinel; values never printed):"
  VERIFY_DRIFT=()
  for PARAM_NAME in "${!PARAMS[@]}"; do
    if RAW="$(aws ssm get-parameter \
          --name "${SSM_PREFIX}/${PARAM_NAME}" \
          --with-decryption \
          --region "$REGION" \
          --output text \
          --query 'Parameter.Value' 2>/dev/null)"; then
      if [[ "$RAW" == "$SENTINEL" ]]; then
        STATE="sentinel"
      elif [[ -z "$RAW" ]]; then
        STATE="EMPTY"
      else
        STATE="real"
      fi
      RAW=""  # drop the value from memory immediately
      printf '  %-10s present  %s\n' "[$STATE]" "$SSM_PREFIX/$PARAM_NAME"
      if [[ -n "${INTENDED_REAL[$PARAM_NAME]:-}" && "$STATE" != "real" ]]; then
        VERIFY_DRIFT+=("$PARAM_NAME")
      fi
    else
      printf '  %-10s ABSENT   %s\n' "[missing]" "$SSM_PREFIX/$PARAM_NAME"
      VERIFY_DRIFT+=("$PARAM_NAME")
    fi
  done
  if [[ ${#VERIFY_DRIFT[@]} -gt 0 ]]; then
    echo ""
    echo "!! VERIFY DRIFT — these were meant to be real but are sentinel/missing:" >&2
    for P in "${VERIFY_DRIFT[@]}"; do echo "   - $SSM_PREFIX/$P" >&2; done
  fi
fi

# Exit non-zero if anything failed to push, so a partial run is never mistaken
# for success.
if [[ ${#FAILED[@]} -gt 0 ]]; then
  exit 1
fi

echo ""
echo "Next steps (usage visibility goes live when all three are done):"
echo "  1. infra/ecs-stack.yml: task def secrets: ValueFrom -> ${SSM_PREFIX}/MONGODB_URI"
echo "     + ${SSM_PREFIX}/GECKO_TELEMETRY, and ssm:GetParameters on the exec role."
echo "  2. Rebuild + redeploy (founder-run): ./infra/deploy.sh"
echo "  3. Verify: hit the hosted MCP, then check gecko_events.surf_events for surf.* docs."
echo ""
echo "Kill-switch without a rebuild:"
echo "  aws ssm put-parameter --name ${SSM_PREFIX}/GECKO_TELEMETRY --value off \\"
echo "    --type SecureString --overwrite --region ${REGION}"
echo "  aws ecs update-service --cluster surfcall --service surfcall \\"
echo "    --force-new-deployment --region ${REGION}"