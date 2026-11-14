#!/usr/bin/env bash
# Deletes the review stack and empties the buckets it created.
set -euo pipefail
if [[ "${CONFIRM_CLEANUP:-}" != "yes" ]]; then
  echo "Refusing to delete resources. After you mean to remove the stack, run:"
  echo "  CONFIRM_CLEANUP=yes REGION=us-east-1 bash scripts/cleanup_cloudshell.sh"
  exit 2
fi
: "${REGION:?Set REGION}"
STACK="${STACK_NAME:-api-review}"
outputs() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" --query "Stacks[0].Outputs" --output json
}
ARTIFACT="$(outputs | python3 -c 'import json,sys; rows=json.load(sys.stdin); print(next(r["OutputValue"] for r in rows if r["OutputKey"]=="ArtifactBucketName"))')"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text --region "$REGION")"
CODE_BUCKET="${DEPLOY_BUCKET:-api-review-code-${ACCOUNT}-${REGION}}"
python3 - "$REGION" "$ARTIFACT" "$CODE_BUCKET" <<'PY'
import subprocess, sys
region, *buckets = sys.argv[1:]
for bucket in buckets:
    if not bucket:
        continue
    subprocess.run(["aws", "s3", "rm", f"s3://{bucket}", "--recursive", "--region", region], check=False)
PY
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"
if aws s3api head-bucket --bucket "$CODE_BUCKET" --region "$REGION" >/dev/null 2>&1; then
  aws s3 rb "s3://${CODE_BUCKET}" --region "$REGION"
fi
echo "Deleted stack ${STACK} in ${REGION}."
if [[ "${CLEANUP_IDENTITIES:-}" != "no" ]]; then
  bash "$(cd "$(dirname "$0")" && pwd)/teardown_demo_identities.sh"
fi
