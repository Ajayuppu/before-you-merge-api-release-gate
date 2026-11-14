#!/usr/bin/env bash
# Deletes the review stack and empties the buckets it created.
set -euo pipefail
if [[ "${CONFIRM_CLEANUP:-}" != "yes" ]]; then
  echo "Refusing to delete resources. After you mean to remove the stack, run:"
  echo "  CONFIRM_CLEANUP=yes REGION=us-east-1 bash scripts/cleanup_cloudshell.sh"
  exit 2
fi
: "${REGION:?Set REGION}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "${CLEANUP_AGENTCORE:-}" != "no" ]]; then
  bash "$ROOT/scripts/teardown_agentcore.sh"
fi
STACK="${STACK_NAME:-api-review}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text --region "$REGION")"
CODE_BUCKET="${DEPLOY_BUCKET:-api-review-code-${ACCOUNT}-${REGION}}"
ARTIFACT=""
TABLE="${STACK}-jobs"
if outputs="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" --query "Stacks[0].Outputs" --output json 2>/dev/null)"; then
  ARTIFACT="$(printf '%s' "$outputs" | python3 -c 'import json,sys; rows=json.load(sys.stdin) or []; print(next((r["OutputValue"] for r in rows if r["OutputKey"]=="ArtifactBucketName"), ""))')"
  TABLE="$(printf '%s' "$outputs" | python3 -c 'import json,sys; rows=json.load(sys.stdin) or []; print(next((r["OutputValue"] for r in rows if r["OutputKey"]=="JobTableName"), ""))')"
  if [[ -z "$TABLE" ]]; then
    TABLE="${STACK}-jobs"
  fi
fi
python3 "$ROOT/scripts/empty_bucket_versions.py" "$REGION" "$ARTIFACT" "$CODE_BUCKET"
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"
if aws s3api head-bucket --bucket "$CODE_BUCKET" --region "$REGION" >/dev/null 2>&1; then
  python3 "$ROOT/scripts/empty_bucket_versions.py" "$REGION" "$CODE_BUCKET"
  aws s3 rb "s3://${CODE_BUCKET}" --region "$REGION"
fi
echo "Deleted stack ${STACK} in ${REGION}."
if [[ "${CLEANUP_IDENTITIES:-}" != "no" ]]; then
  bash "$ROOT/scripts/teardown_demo_identities.sh"
fi
echo "Cleanup verification:"
python3 "$ROOT/scripts/verify_cleanup.py" "$REGION" "$STACK" "$ARTIFACT" "$CODE_BUCKET" "$TABLE"
