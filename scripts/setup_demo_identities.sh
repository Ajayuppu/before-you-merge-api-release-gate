#!/usr/bin/env bash
# Creates the operator user and the two API caller roles. Refuses until CONFIRM_IDENTITIES=yes.
set -euo pipefail
export AWS_PAGER=""
if [[ "${CONFIRM_IDENTITIES:-}" != "yes" ]]; then
  echo "Refusing to create IAM identities. This script creates an IAM user, one access key, and two roles."
  echo "After you authorize it, run:"
  echo "  CONFIRM_IDENTITIES=yes REGION=us-east-1 bash scripts/setup_demo_identities.sh"
  exit 2
fi
: "${REGION:?Set REGION}"
STACK="${STACK_NAME:-api-review}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text --region "$REGION")"
ENDPOINT="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='ReviewApiEndpoint'].OutputValue" --output text)"
if [[ -z "$ENDPOINT" || "$ENDPOINT" == "None" ]]; then
  echo "ReviewApiEndpoint was not found on stack ${STACK}." >&2
  exit 1
fi
API_ID="$(ENDPOINT="$ENDPOINT" python3 - <<'PY'
import os
from urllib.parse import urlparse
endpoint = os.environ["ENDPOINT"].strip()
parsed = urlparse(endpoint)
host = parsed.hostname or ""
parts = host.split(".")
if len(parts) < 4 or parts[1] != "execute-api":
    raise SystemExit(f"ReviewApiEndpoint is not an execute-api hostname: {endpoint}")
stage = [piece for piece in parsed.path.split("/") if piece]
if not stage or stage[0] != "live":
    raise SystemExit(f"ReviewApiEndpoint stage must be live: {endpoint}")
print(parts[0])
PY
)"
USER_NAME="api-review-operator"
USER_ARN="arn:aws:iam::${ACCOUNT}:user/${USER_NAME}"
DEV_ROLE="ApiReviewDeveloper"
REV_ROLE="ApiReviewReviewer"
DEV_ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${DEV_ROLE}"
REV_ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${REV_ROLE}"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
python3 - "$WORKDIR" "$ACCOUNT" "$REGION" "$API_ID" "$USER_ARN" "$DEV_ROLE_ARN" "$REV_ROLE_ARN" <<'PY'
import json
import sys
from pathlib import Path

workdir, account, region, api_id, user_arn, dev_role_arn, rev_role_arn = sys.argv[1:]
base = f"arn:aws:execute-api:{region}:{account}:{api_id}"
trust = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"AWS": user_arn},
            "Action": "sts:AssumeRole",
        }
    ],
}
operator = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Resource": [dev_role_arn, rev_role_arn],
        }
    ],
}
developer = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "execute-api:Invoke",
            "Resource": [
                f"{base}/live/POST/jobs",
                f"{base}/live/GET/jobs/*",
            ],
        },
        {
            "Effect": "Deny",
            "Action": "execute-api:Invoke",
            "Resource": [
                f"{base}/live/GET/jobs/*/review-context",
                f"{base}/live/POST/reviews",
            ],
        },
    ],
}
reviewer = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "execute-api:Invoke",
            "Resource": [
                f"{base}/live/GET/jobs/*/review-context",
                f"{base}/live/POST/reviews",
            ],
        }
    ],
}
out = Path(workdir)
for name, document in {
    "trust.json": trust,
    "operator.json": operator,
    "developer.json": developer,
    "reviewer.json": reviewer,
}.items():
    (out / name).write_text(json.dumps(document))
PY

if aws iam get-user --user-name "$USER_NAME" >/dev/null 2>&1; then
  echo "IAM user ${USER_NAME} already exists."
else
  aws iam create-user --user-name "$USER_NAME" >/dev/null
  echo "Created IAM user ${USER_NAME}."
fi
aws iam put-user-policy --user-name "$USER_NAME" --policy-name assume-caller-roles --policy-document "file://${WORKDIR}/operator.json"

KEY_COUNT="$(aws iam list-access-keys --user-name "$USER_NAME" --query 'length(AccessKeyMetadata)' --output text)"
if [[ "$KEY_COUNT" == "0" ]]; then
  aws iam create-access-key --user-name "$USER_NAME" --output json | REGION="$REGION" python3 -c '
import json, os, subprocess, sys
data = json.load(sys.stdin)["AccessKey"]
profile = "api-review-operator"
region = os.environ["REGION"]

def configure(key, value):
    subprocess.check_call(["aws", "configure", "set", key, value, "--profile", profile])

configure("aws_access_key_id", data["AccessKeyId"])
configure("aws_secret_access_key", data["SecretAccessKey"])
configure("region", region)
'
  echo "Stored a new access key for ${USER_NAME} in profile api-review-operator."
else
  aws configure set region "$REGION" --profile api-review-operator
  if ! aws configure get aws_access_key_id --profile api-review-operator >/dev/null 2>&1; then
    echo "${USER_NAME} already has an access key in IAM, and profile api-review-operator has no stored key." >&2
    echo "Delete that IAM access key and rerun this script. The existing secret cannot be read again." >&2
    exit 1
  fi
  echo "Left the existing ${USER_NAME} access key in place."
fi

ensure_role() {
  local name="$1"
  local policy_file="$2"
  if aws iam get-role --role-name "$name" >/dev/null 2>&1; then
    aws iam update-assume-role-policy --role-name "$name" --policy-document "file://${WORKDIR}/trust.json"
    aws iam update-role --role-name "$name" --max-session-duration 3600 >/dev/null
    echo "Updated role ${name}."
  else
    aws iam create-role --role-name "$name" --assume-role-policy-document "file://${WORKDIR}/trust.json" --max-session-duration 3600 >/dev/null
    echo "Created role ${name}."
  fi
  aws iam put-role-policy --role-name "$name" --policy-name invoke-review-api --policy-document "file://${policy_file}"
}

ensure_role "$DEV_ROLE" "${WORKDIR}/developer.json"
ensure_role "$REV_ROLE" "${WORKDIR}/reviewer.json"

aws configure set role_arn "$DEV_ROLE_ARN" --profile api-review-developer
aws configure set source_profile api-review-operator --profile api-review-developer
aws configure set role_session_name dev-ada --profile api-review-developer
aws configure set region "$REGION" --profile api-review-developer
aws configure set role_arn "$REV_ROLE_ARN" --profile api-review-reviewer
aws configure set source_profile api-review-operator --profile api-review-reviewer
aws configure set role_session_name rev-ben --profile api-review-reviewer
aws configure set region "$REGION" --profile api-review-reviewer

wait_profile() {
  local profile="$1"
  local deadline=$((SECONDS + 60))
  local arn=""
  while (( SECONDS <= deadline )); do
    if arn="$(aws sts get-caller-identity --profile "$profile" --region "$REGION" --query Arn --output text 2>/dev/null)"; then
      if [[ -n "$arn" && "$arn" != "None" ]]; then
        printf '%s\n' "$arn"
        return 0
      fi
    fi
    sleep 3
  done
  echo "Timed out waiting for profile ${profile} to assume its role." >&2
  return 1
}

DEV_CALLER="$(wait_profile api-review-developer)"
REV_CALLER="$(wait_profile api-review-reviewer)"
printf 'api-review-developer %s\n' "$DEV_CALLER"
printf 'api-review-reviewer %s\n' "$REV_CALLER"
