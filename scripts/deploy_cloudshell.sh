#!/usr/bin/env bash
# Creates the review stack. Refuses to run until CONFIRM_DEPLOY=yes.
set -euo pipefail
if [[ "${CONFIRM_DEPLOY:-}" != "yes" ]]; then
  echo "Refusing to deploy. This script creates AWS resources in the caller's account."
  echo "After you authorize a deployment, run:"
  echo "  CONFIRM_DEPLOY=yes REGION=us-east-1 OTEL_LAYER_ARN='<layer arn>' bash scripts/deploy_cloudshell.sh"
  exit 2
fi
: "${REGION:?Set REGION to a region where the services in this stack are available}"
: "${OTEL_LAYER_ARN:?Set OTEL_LAYER_ARN from the current AWS Lambda Layer for OpenTelemetry documentation}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH=src
bash scripts/package_lambda.sh
python3 scripts/assemble_template.py
ACCOUNT="$(aws sts get-caller-identity --query Account --output text --region "$REGION")"
BUCKET="${DEPLOY_BUCKET:-api-review-code-${ACCOUNT}-${REGION}}"
STACK="${STACK_NAME:-api-review}"
CODE_KEY="api-review/lambda-$(sha256sum "$ROOT/build/lambda.zip" | cut -c1-16).zip"
if ! aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" --create-bucket-configuration "LocationConstraint=${REGION}"
  fi
fi
aws s3 cp "$ROOT/build/lambda.zip" "s3://${BUCKET}/${CODE_KEY}" --region "$REGION"
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file "$ROOT/cloud/template.deploy.json" \
  --capabilities CAPABILITY_IAM --s3-bucket "${BUCKET}" --s3-prefix api-review/templates \
  --parameter-overrides \
    "LambdaCodeBucket=${BUCKET}" \
    "LambdaCodeKey=${CODE_KEY}" \
    "OtelLayerArn=${OTEL_LAYER_ARN}" \
    "ProjectName=${PROJECT_NAME:-api-review}" \
    "BedrockAdvisory=${BEDROCK_ADVISORY:-disabled}" \
    "BedrockModelId=${BEDROCK_MODEL_ID:-us.amazon.nova-2-lite-v1:0}" \
    "DeploymentNonce=${DEPLOYMENT_NONCE:-$(date +%s)}"
aws cloudformation describe-stacks \
  --region "$REGION" \
  --stack-name "$STACK" \
  --query "Stacks[0].Outputs" \
  --output table
