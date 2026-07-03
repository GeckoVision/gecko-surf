#!/usr/bin/env bash
# =============================================================
# surfcall - MCP Streamable-HTTP ECS deploy script
#
# Usage:
#   ./infra/deploy.sh [--region us-east-2] [--env production] [--cert ARN] [--stack NAME] [--skip-build]
#
# Prerequisites:
#   - AWS CLI configured (aws configure or IAM role)
#   - Docker running
#   - Default IAM role ecsTaskExecutionRole exists in the account (ECR pull + logs)
#   - ACM certificate for mcp.geckovision.tech in us-east-2 (for HTTPS)
#
# Stateless: NO SSM prerequisite. surfcall reads no secrets.
# Adapted from ../gecko-mcpay-api/infra/deploy.sh (SSM + force-deploy flow dropped).
# =============================================================
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-2}"
STACK_NAME="surfcall-mcp-ecs"
ENVIRONMENT="production"
ECR_REPOSITORY="surfcall"

# Account-specific identifiers (cert ARN, VPC, subnets) come from the
# environment or .env — never committed. They're identifiers, not credentials,
# but a public repo shouldn't map the account. Add to .env:
#   GECKO_CERT_ARN=arn:aws:acm:...          (ACM cert for mcp.geckovision.tech)
#   GECKO_VPC_ID=vpc-...                    (reuse the existing VPC — no new NAT/EIP)
#   GECKO_PUBLIC_SUBNETS=subnet-...,subnet-...
#   GECKO_PRIVATE_SUBNETS=subnet-...,subnet-...
ENV_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$ENV_SCRIPT_DIR/../.env" ]]; then
  # shellcheck disable=SC1091
  set -a; source "$ENV_SCRIPT_DIR/../.env"; set +a
fi
CERTIFICATE_ARN="${GECKO_CERT_ARN:-}"
VPC_ID="${GECKO_VPC_ID:-}"
PUBLIC_SUBNETS="${GECKO_PUBLIC_SUBNETS:-}"
PRIVATE_SUBNETS="${GECKO_PRIVATE_SUBNETS:-}"
SKIP_BUILD=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --region)     REGION="$2";          shift 2 ;;
    --env)        ENVIRONMENT="$2";     shift 2 ;;
    --stack)      STACK_NAME="$2";      shift 2 ;;
    --cert)       CERTIFICATE_ARN="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=true;      shift ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

MISSING=()
[[ -z "$CERTIFICATE_ARN" ]] && MISSING+=("GECKO_CERT_ARN")
[[ -z "$VPC_ID" ]]          && MISSING+=("GECKO_VPC_ID")
[[ -z "$PUBLIC_SUBNETS" ]]  && MISSING+=("GECKO_PUBLIC_SUBNETS")
[[ -z "$PRIVATE_SUBNETS" ]] && MISSING+=("GECKO_PRIVATE_SUBNETS")
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "ERROR: set these in .env (or the environment) before deploying:" >&2
  for M in "${MISSING[@]}"; do echo "  - $M" >&2; done
  exit 1
fi

echo "==> Region:      $REGION"
echo "==> Stack:       $STACK_NAME"
echo "==> Environment: $ENVIRONMENT"
echo "==> ECR repo:    $ECR_REPOSITORY"
echo "==> Skip build:  $SKIP_BUILD"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPOSITORY}"
# Timestamped tag so the tag always changes, forcing CloudFormation to update
# the task definition even when the git SHA hasn't (env/infra-only redeploys).
IMAGE_TAG="${ENVIRONMENT}-$(git rev-parse --short HEAD 2>/dev/null || echo latest)-$(date +%s)"
FULL_IMAGE="${ECR_URI}:${IMAGE_TAG}"
CF_IMAGE="$FULL_IMAGE"

echo "==> ECR image:   $FULL_IMAGE"
echo "==> CF image:    $CF_IMAGE"

if [[ "$SKIP_BUILD" == false ]]; then
  echo "==> Logging into ECR..."
  aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

  echo "==> Ensuring ECR repository exists..."
  aws ecr create-repository \
    --repository-name "$ECR_REPOSITORY" \
    --image-scanning-configuration scanOnPush=true \
    --region "$REGION" 2>/dev/null || true

  # Fargate runs amd64 unless you opt in to Graviton (we don't), so build amd64
  # explicitly even on arm64 dev machines.
  echo "==> Building Docker image (linux/amd64)..."
  docker buildx build --platform linux/amd64 -t surfcall --load "$REPO_ROOT"

  docker tag surfcall "$FULL_IMAGE"
  docker tag surfcall "${ECR_URI}:${ENVIRONMENT}-latest"

  echo "==> Pushing image to ECR..."
  docker push "$FULL_IMAGE"
  docker push "${ECR_URI}:${ENVIRONMENT}-latest"
  echo "==> Image pushed: $FULL_IMAGE"
else
  echo "==> Skipping Docker build/push."
fi

STACK_STATUS=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].StackStatus' \
  --output text 2>/dev/null || echo "DOES_NOT_EXIST")

if [[ "$STACK_STATUS" == "REVIEW_IN_PROGRESS" || "$STACK_STATUS" == "ROLLBACK_COMPLETE" ]]; then
  echo "==> Stack is in '$STACK_STATUS' — deleting before redeploy..."
  aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
  echo "==> Stack deleted."
fi

echo "==> Deploying CloudFormation stack '$STACK_NAME'..."
aws cloudformation deploy \
  --template-file "$SCRIPT_DIR/ecs-stack.yml" \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    Image="$CF_IMAGE" \
    Environment="$ENVIRONMENT" \
    CertificateArn="$CERTIFICATE_ARN" \
    VpcId="$VPC_ID" \
    PublicSubnetIds="$PUBLIC_SUBNETS" \
    PrivateSubnetIds="$PRIVATE_SUBNETS" \
  --no-fail-on-empty-changeset

# When --skip-build is used the CF tag hasn't changed; nudge ECS to pull latest.
if [[ "$SKIP_BUILD" == false ]]; then
  echo "==> Forcing ECS service to pick up the new image..."
  aws ecs update-service \
    --cluster surfcall \
    --service surfcall \
    --force-new-deployment \
    --region "$REGION" \
    --output text \
    --query 'service.deployments[0].{status:rolloutState,desired:desiredCount}' \
    >/dev/null || true
fi

echo ""
echo "==> Stack outputs:"
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table

ALB_DNS=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`ALBDNSName`].OutputValue' \
  --output text)

echo ""
echo "==> Done!"
echo "    ALB DNS  : $ALB_DNS"
echo "    A-alias  : mcp.geckovision.tech → $ALB_DNS"
echo "    Health   : curl https://mcp.geckovision.tech/healthz   (after DNS + cert)"
echo "    MCP URL  : https://mcp.geckovision.tech/mcp"
echo ""
echo "Next steps:"
echo "  1. Route 53 → A-record (alias) mcp.geckovision.tech → $ALB_DNS"
echo "  2. Without cert: curl http://$ALB_DNS/healthz"
echo "  3. Tail logs:   aws logs tail /ecs/surfcall --follow --region $REGION"