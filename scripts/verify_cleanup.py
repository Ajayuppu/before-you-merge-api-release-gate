"""Print whether the review resources are still present after cleanup."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from empty_bucket_versions import existence_report

STACK_ROLES = (
    "ApiReviewDeveloper",
    "ApiReviewReviewer",
    "ApiReviewCodeInterpreter",
    "ApiReviewGateway",
)
STACK_USER = "api-review-operator"
GATEWAY_NAME = "ApiReviewTools"
ENGINE_NAME = "ApiReviewPolicy"
INTERPRETER_NAME = "ApiReviewChecker"


def _missing(exc: BaseException) -> bool:
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
    text = f"{type(exc).__name__} {code} {exc}".lower()
    return any(token in text for token in ("notfound", "nosuch", "404"))


def _present(action) -> bool | None:
    try:
        action()
    except Exception as exc:
        if _missing(exc):
            return False
        return None
    return True


def _named(lister, keys: tuple[str, ...], name: str) -> bool | None:
    try:
        page = lister()
    except Exception as exc:
        if _missing(exc):
            return False
        return None
    items: list[dict] = []
    for key in keys:
        items.extend(page.get(key) or [])
    return any(str(item.get("name") or "") == name for item in items)


def collect(region: str, stack: str, artifact: str, code_bucket: str, table: str) -> list[tuple[str, bool | None]]:
    import boto3

    cloudformation = boto3.client("cloudformation", region_name=region)
    s3 = boto3.client("s3", region_name=region)
    dynamodb = boto3.client("dynamodb", region_name=region)
    iam = boto3.client("iam")
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    rows: list[tuple[str, bool | None]] = [
        (f"stack {stack}", _present(lambda: cloudformation.describe_stacks(StackName=stack))),
    ]
    for label, bucket in (("artifact bucket", artifact), ("code bucket", code_bucket)):
        if not bucket:
            rows.append((label, None))
            continue
        rows.append((f"{label} {bucket}", _present(lambda bucket=bucket: s3.head_bucket(Bucket=bucket))))
    rows.append((f"table {table}", _present(lambda: dynamodb.describe_table(TableName=table))))
    for role in STACK_ROLES:
        rows.append((f"role {role}", _present(lambda role=role: iam.get_role(RoleName=role))))
    rows.append((f"user {STACK_USER}", _present(lambda: iam.get_user(UserName=STACK_USER))))
    rows.append(
        (
            f"gateway {GATEWAY_NAME}",
            _named(control.list_gateways, ("items", "gateways", "gatewaySummaries"), GATEWAY_NAME),
        )
    )
    rows.append(
        (
            f"policy engine {ENGINE_NAME}",
            _named(control.list_policy_engines, ("items", "policyEngines", "policyEngineSummaries"), ENGINE_NAME),
        )
    )
    rows.append(
        (
            f"code interpreter {INTERPRETER_NAME}",
            _named(control.list_code_interpreters, ("items", "codeInterpreters", "codeInterpreterSummaries"), INTERPRETER_NAME),
        )
    )
    return rows


def main() -> int:
    region, stack, artifact, code_bucket, table = sys.argv[1:6]
    print(existence_report(collect(region, stack, artifact, code_bucket, table)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
