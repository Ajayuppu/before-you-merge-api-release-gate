#!/usr/bin/env bash
# Deletes the operator user, the two caller roles, and the three CLI profiles.
set -euo pipefail
export AWS_PAGER=""
if [[ "${CONFIRM_CLEANUP:-}" != "yes" ]]; then
  echo "Refusing to delete IAM identities. After you mean to remove them, run:"
  echo "  CONFIRM_CLEANUP=yes bash scripts/teardown_demo_identities.sh"
  exit 2
fi

remove_inline_policies() {
  local kind="$1"
  local name="$2"
  local list_cmd=()
  local delete_cmd=()
  if [[ "$kind" == "role" ]]; then
    list_cmd=(aws iam list-role-policies --role-name "$name" --query 'PolicyNames[]' --output text)
    delete_cmd=(aws iam delete-role-policy --role-name "$name" --policy-name)
  else
    list_cmd=(aws iam list-user-policies --user-name "$name" --query 'PolicyNames[]' --output text)
    delete_cmd=(aws iam delete-user-policy --user-name "$name" --policy-name)
  fi
  local policies policy
  policies="$("${list_cmd[@]}" 2>/dev/null || true)"
  for policy in $policies; do
    if [[ -z "$policy" || "$policy" == "None" ]]; then
      continue
    fi
    "${delete_cmd[@]}" "$policy" >/dev/null 2>&1 || true
  done
}

remove_inline_policies role ApiReviewDeveloper
aws iam delete-role --role-name ApiReviewDeveloper >/dev/null 2>&1 || true
remove_inline_policies role ApiReviewReviewer
aws iam delete-role --role-name ApiReviewReviewer >/dev/null 2>&1 || true

if aws iam get-user --user-name api-review-operator >/dev/null 2>&1; then
  KEYS="$(aws iam list-access-keys --user-name api-review-operator --query 'AccessKeyMetadata[].AccessKeyId' --output text 2>/dev/null || true)"
  for key in $KEYS; do
    if [[ -z "$key" || "$key" == "None" ]]; then
      continue
    fi
    aws iam delete-access-key --user-name api-review-operator --access-key-id "$key" >/dev/null 2>&1 || true
  done
  remove_inline_policies user api-review-operator
  aws iam delete-user --user-name api-review-operator >/dev/null 2>&1 || true
fi

python3 - <<'PY'
from pathlib import Path

PROFILES = {"api-review-operator", "api-review-developer", "api-review-reviewer"}


def strip(path: Path) -> None:
    if not path.exists():
        return
    lines = path.read_text().splitlines(keepends=True)
    kept = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1].strip()
            if name.startswith("profile "):
                name = name[len("profile ") :]
            skipping = name in PROFILES
        if not skipping:
            kept.append(line)
    path.write_text("".join(kept))


home = Path.home()
strip(home / ".aws" / "config")
strip(home / ".aws" / "credentials")
PY
echo "Removed demo IAM identities and CLI profiles."
