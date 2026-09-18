#!/usr/bin/env bash
# =============================================================
# gecko-relay — the Kora fee-payer relay as a second Fargate service on the
# surfcall-mcp-ecs stack, behind the SAME ALB and certificate on its own HTTPS port.
#
# Usage:
#   ./infra/deploy-relay.sh [--region us-east-2] [--stack surfcall-mcp-ecs] [--port 8443]
#                           [--kora-src ../kora] [--kora-ref kora-cli-v2.2.0-beta.8] [--skip-build]
#
# Prerequisites (the script checks each and refuses rather than deploying a task that
# would fail at boot):
#   - the surfcall-mcp-ecs stack exists (./infra/deploy.sh ran once) with a certificate
#   - the three SSM parameters under /gecko-relay/ exist:
#       KORA_RELAY_KEY  SecureString  base58 of the 64-byte keypair, generated OFF this box
#       KORA_API_KEY    SecureString  long random; Gecko presents it in x-api-key
#       RPC_URL         String        the mainnet RPC the relay simulates and reads through
#     see docs/runbooks/mainnet-relay.md §1 and §2b for the put-parameter commands
#   - Docker running; a checkout of Kora beside this repo (../kora) at --kora-ref
#
# Never in this script: the key, the API key, any .env value printed.
# =============================================================
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-2}"
STACK_NAME="surfcall-mcp-ecs"
RELAY_PORT="8443"
KORA_SRC="${KORA_SRC:-../kora}"
KORA_REF="${KORA_REF:-kora-cli-v2.2.0-beta.8}"
ECR_REPOSITORY="gecko-relay"
SKIP_BUILD=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --region)     REGION="$2";     shift 2 ;;
    --stack)      STACK_NAME="$2"; shift 2 ;;
    --port)       RELAY_PORT="$2"; shift 2 ;;
    --kora-src)   KORA_SRC="$2";   shift 2 ;;
    --kora-ref)   KORA_REF="$2";   shift 2 ;;
    --skip-build) SKIP_BUILD=true; shift ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
KORA_DEMO="$REPO_ROOT/examples/kora_demo"

# --- 1. the stack must exist, with a certificate: the relay listener is HTTPS-only ---------
STACK_STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")
if [[ "$STACK_STATUS" == "DOES_NOT_EXIST" ]]; then
  echo "ERROR: stack '$STACK_NAME' does not exist. Run ./infra/deploy.sh first." >&2; exit 1
fi
CERT=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].Parameters[?ParameterKey==`CertificateArn`].ParameterValue' --output text)
if [[ -z "$CERT" || "$CERT" == "None" ]]; then
  echo "ERROR: the stack has no CertificateArn; the relay listener is HTTPS-only." >&2; exit 1
fi

# --- 2. the three SSM parameters, present (values never read here) ------------------------
MISSING=()
for P in KORA_RELAY_KEY KORA_API_KEY RPC_URL; do
  aws ssm get-parameter --name "/gecko-relay/$P" --region "$REGION" --query Parameter.Name \
    --output text >/dev/null 2>&1 || MISSING+=("/gecko-relay/$P")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "ERROR: these SSM parameters must exist before the task can boot:" >&2
  for M in "${MISSING[@]}"; do echo "  - $M" >&2; done
  echo "See docs/runbooks/mainnet-relay.md §2b for the put-parameter commands." >&2
  exit 1
fi

# --- 3. build: Kora at the pinned tag, then our two files on top --------------------------
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPOSITORY}"
KORA_SHA=$(git -C "$KORA_SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)
KORA_AT=$(git -C "$KORA_SRC" describe --tags --exact-match 2>/dev/null || echo "")
IMAGE_TAG="${KORA_REF}-${KORA_SHA}-$(git -C "$REPO_ROOT" rev-parse --short HEAD)-$(date +%s)"
FULL_IMAGE="${ECR_URI}:${IMAGE_TAG}"

echo "==> Region:      $REGION"
echo "==> Stack:       $STACK_NAME   relay port $RELAY_PORT"
echo "==> Kora source: $KORA_SRC @ $KORA_SHA ${KORA_AT:+(tag $KORA_AT)}"
echo "==> Image:       $FULL_IMAGE"

if [[ "$SKIP_BUILD" == false ]]; then
  if [[ -n "$KORA_AT" && "$KORA_AT" != "$KORA_REF" ]] || [[ -z "$KORA_AT" ]]; then
    echo "WARN: $KORA_SRC is not checked out at $KORA_REF; building what is there ($KORA_SHA)." >&2
  fi
  echo "==> Logging into ECR..."
  aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
  aws ecr create-repository --repository-name "$ECR_REPOSITORY" \
    --image-scanning-configuration scanOnPush=true --region "$REGION" 2>/dev/null || true

  echo "==> Building Kora (linux/amd64) from $KORA_SRC..."
  docker buildx build --platform linux/amd64 -t "gecko-kora-base:${KORA_REF}" --load "$KORA_SRC"
  echo "==> Building the relay image (config + signer file on top)..."
  docker buildx build --platform linux/amd64 --build-arg "KORA_BASE=gecko-kora-base:${KORA_REF}" \
    -f "$KORA_DEMO/Dockerfile.relay" -t gecko-relay --load "$KORA_DEMO"
  docker tag gecko-relay "$FULL_IMAGE"
  docker tag gecko-relay "${ECR_URI}:latest"
  echo "==> Pushing..."
  docker push "$FULL_IMAGE"
  docker push "${ECR_URI}:latest"
else
  FULL_IMAGE="${ECR_URI}:latest"
  echo "==> Skipping build; deploying ${FULL_IMAGE}"
fi

# --- 4. deploy: only the relay parameters change; every other parameter keeps its value --
echo "==> Deploying '$STACK_NAME' with the relay..."
aws cloudformation deploy \
  --template-file "$SCRIPT_DIR/ecs-stack.yml" \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides RelayImage="$FULL_IMAGE" RelayListenerPort="$RELAY_PORT" \
  --no-fail-on-empty-changeset

aws ecs update-service --cluster surfcall --service gecko-relay --force-new-deployment \
  --region "$REGION" --output text --query 'service.deployments[0].rolloutState' >/dev/null || true

RELAY_URL=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`RelayURL`].OutputValue' --output text)
echo ""
echo "==> Done. Relay URL: $RELAY_URL"
echo "    Logs:   aws logs tail /ecs/gecko-relay --follow --region $REGION"
echo "    Probes: KORA_RPC_URL=$RELAY_URL ./infra/probe-relay.sh   (needs KORA_API_KEY in the env)"
