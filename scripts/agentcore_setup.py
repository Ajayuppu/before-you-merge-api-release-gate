"""Create, validate, and delete the optional AgentCore resources.

Control-plane calls use bedrock-agentcore-control. Code Interpreter sessions
use the bedrock-agentcore data plane. Trust principals follow the current
AgentCore docs: service bedrock-agentcore.amazonaws.com, limited by
aws:SourceAccount and aws:SourceArn.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

STATE_PATH = ROOT / "build" / "agentcore-ids.json"
INTERPRETER_ROLE = "ApiReviewCodeInterpreter"
GATEWAY_ROLE = "ApiReviewGateway"
INTERPRETER_NAME = "ApiReviewChecker"
GATEWAY_NAME = "ApiReviewTools"
ENGINE_NAME = "ApiReviewPolicy"
TARGET_NAME = "ReviewTools"
POLICY_NAMES = ("AllowReadArtifacts", "AllowRequestSpecCheck", "AllowReadTestResults")
SESSION_NAME = "ApiReviewValidate"
TAG_KEY = "api-review"
TAG_VALUE = "agentcore"
# MCP tool and Cedar action id: ${target_name}___${tool_name}
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-tool-naming.html
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-scope.html
TOOL_SEPARATOR = "___"
READ_TOOL = f"ReviewTools{TOOL_SEPARATOR}read_artifacts"
APPROVE_TOOL = f"ReviewTools{TOOL_SEPARATOR}approve"
ALLOWED_SCOPE = (
    f"ReviewTools{TOOL_SEPARATOR}read_artifacts",
    f"ReviewTools{TOOL_SEPARATOR}request_spec_check",
    f"ReviewTools{TOOL_SEPARATOR}read_test_results",
)
POLICY_TOOLS = (
    ("AllowReadArtifacts", "read_artifacts"),
    ("AllowRequestSpecCheck", "request_spec_check"),
    ("AllowReadTestResults", "read_test_results"),
)
POLICY_VALIDATION_MODE = "FAIL_ON_ANY_FINDINGS"
CALLER_ROLE = "ApiReviewDeveloper"
DENY_ROLE = "ApiReviewReviewer"
CALLER_PROFILE = "api-review-developer"
DENY_PROFILE = "api-review-reviewer"
EXPECTED_CHECKER_RULES = frozenset({"checker.amount.type", "checker.next_cursor.required"})
# Live gateways created before the later protocol versions advertise only this one.
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-using.html
MCP_PROTOCOL_VERSION = "2025-03-26"

# Documented name constraints:
# CreateCodeInterpreter: [a-zA-Z][a-zA-Z0-9_]{0,47}
# https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateCodeInterpreter.html
# CreateGateway: ([0-9a-zA-Z][-]?){1,48}
# https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateGateway.html
# CreateGatewayTarget: ([0-9a-zA-Z][-]?){1,100}
# https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateGatewayTarget.html
# CreatePolicyEngine and CreatePolicy: [A-Za-z][A-Za-z0-9_]* with length 1-48
# https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreatePolicyEngine.html
# https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreatePolicy.html
NAME_PATTERNS = {
    "code_interpreter": (INTERPRETER_NAME, r"[a-zA-Z][a-zA-Z0-9_]{0,47}"),
    "gateway": (GATEWAY_NAME, r"([0-9a-zA-Z][-]?){1,48}"),
    "gateway_target": (TARGET_NAME, r"([0-9a-zA-Z][-]?){1,100}"),
    "policy_engine": (ENGINE_NAME, r"[A-Za-z][A-Za-z0-9_]{0,47}"),
    "policy_read_artifacts": (POLICY_NAMES[0], r"[A-Za-z][A-Za-z0-9_]{0,47}"),
    "policy_request_spec_check": (POLICY_NAMES[1], r"[A-Za-z][A-Za-z0-9_]{0,47}"),
    "policy_read_test_results": (POLICY_NAMES[2], r"[A-Za-z][A-Za-z0-9_]{0,47}"),
    "session": (SESSION_NAME, r"[a-zA-Z][a-zA-Z0-9_]{0,47}"),
}

# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-interpreter-resource-session-management.html
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-prerequisites-permissions.html
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html
SERVICE_PRINCIPAL = "bedrock-agentcore.amazonaws.com"


def code_interpreter_source_arn(account: str, region: str) -> str:
    """Documented execution-role SourceArn. A specific code-interpreter id does not match at session start.

    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-interpreter-resource-session-management.html
    """
    return f"arn:aws:bedrock-agentcore:{region}:{account}:*"


def gateway_source_arn(account: str, region: str) -> str:
    """Account-wide SourceArn for the gateway role.

    CreateGateway assumes the role while validating the policy engine, and that
    source ARN is the policy engine, not the gateway. A gateway-only ArnLike
    rejects that check. The same wildcard is the documented code-interpreter form.
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-prerequisites-permissions.html
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-interpreter-resource-session-management.html
    """
    return f"arn:aws:bedrock-agentcore:{region}:{account}:*"


def trust_policy(account: str, region: str, source_arn: str) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowBedrockAgentCoreAssumeRole",
                "Effect": "Allow",
                "Principal": {"Service": SERVICE_PRINCIPAL},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {"aws:SourceArn": source_arn},
                },
            }
        ],
    }


def gateway_permissions(account: str, region: str, api_id: str) -> dict[str, Any]:
    prefix = f"arn:aws:bedrock-agentcore:{region}:{account}"
    api = f"arn:aws:execute-api:{region}:{account}:{api_id}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PolicyEngineConfiguration",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:GetPolicyEngine"],
                "Resource": [f"{prefix}:policy-engine/*"],
            },
            {
                "Sid": "PolicyEngineAuthorization",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:AuthorizeAction", "bedrock-agentcore:PartiallyAuthorizeActions"],
                "Resource": [f"{prefix}:policy-engine/*", f"{prefix}:gateway/*"],
            },
            {
                "Sid": "ReviewApiDeveloperReads",
                "Effect": "Allow",
                "Action": ["execute-api:Invoke"],
                "Resource": [
                    f"{api}/live/GET/jobs",
                    f"{api}/live/GET/jobs/*",
                    f"{api}/live/POST/jobs",
                ],
            },
            {
                "Sid": "DenyReviewerRoutes",
                "Effect": "Deny",
                "Action": ["execute-api:Invoke"],
                "Resource": [
                    f"{api}/live/GET/jobs/*/review-context",
                    f"{api}/live/POST/reviews",
                ],
            },
        ],
    }


def tool_configuration() -> dict[str, Any]:
    return {
        "toolFilters": [
            {"filterPath": "/jobs/{jobId}", "methods": ["GET"]},
            {"filterPath": "/jobs/{jobId}/report", "methods": ["GET"]},
            {"filterPath": "/jobs", "methods": ["POST"]},
            {"filterPath": "/reviews", "methods": ["POST"]},
        ],
        "toolOverrides": [
            {"name": "read_artifacts", "path": "/jobs/{jobId}", "method": "GET", "description": "Read one job. Not a verdict."},
            {"name": "request_spec_check", "path": "/jobs/{jobId}/report", "method": "GET", "description": "Read the sealed report. Not a verdict."},
            {"name": "read_test_results", "path": "/jobs", "method": "POST", "description": "Submit a job. Not a verdict."},
            {"name": "approve", "path": "/reviews", "method": "POST", "description": "Review callback. Cedar default deny must block this tool."},
        ],
    }


def classify_tool_result(expect_allow: bool, status: int, body: str) -> tuple[bool, str]:
    if _is_protocol_error(body):
        return False, body[:300]
    if expect_allow:
        ok = _is_success_payload(status, body)
        return ok, "allowed" if ok else body[:300]
    ok = _is_policy_denial(body)
    return ok, "denied" if ok else body[:300]


def protocol_from_gateway(gateway: dict[str, Any] | None) -> str:
    config = (gateway or {}).get("protocolConfiguration") or {}
    mcp = config.get("mcp") or {}
    versions = [str(item) for item in (mcp.get("supportedVersions") or [])]
    if MCP_PROTOCOL_VERSION in versions:
        return MCP_PROTOCOL_VERSION
    if versions:
        return versions[0]
    return MCP_PROTOCOL_VERSION


def negotiated_protocol(body: str, current: str) -> str | None:
    parsed = _jsonrpc_payload(body)
    if not isinstance(parsed, dict):
        return None
    error = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
    message = str(error.get("message") or "")
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    supported = [str(item) for item in (data.get("supported") or [])]
    if "protocol version" not in message.lower() or not supported:
        return None
    chosen = MCP_PROTOCOL_VERSION if MCP_PROTOCOL_VERSION in supported else supported[0]
    if chosen == current:
        return None
    return chosen


def tool_names_from_list(body: str) -> list[str]:
    parsed = _jsonrpc_payload(body)
    if not isinstance(parsed, dict):
        return []
    result = parsed.get("result") if isinstance(parsed.get("result"), dict) else {}
    names: list[str] = []
    for item in result.get("tools") or []:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return names


def select_gateway_tools(names: list[str]) -> tuple[str, str]:
    """Use the separator the gateway actually advertised. Denied tools may be hidden from tools/list."""
    read = next((name for name in names if name.endswith("read_artifacts")), "")
    approve = next((name for name in names if name.endswith("approve")), "")
    if read and not approve:
        approve = read[: -len("read_artifacts")] + "approve"
    return read, approve


def render_policy(template: str, gateway_arn: str) -> str:
    return template.replace("__GATEWAY_ARN__", gateway_arn)


def statements_match(left: str, right: str) -> bool:
    return " ".join(left.split()) == " ".join(right.split())


def policy_statement_text(policy: dict[str, Any]) -> str:
    definition = policy.get("definition") or {}
    for key in ("policy", "cedar"):
        body = definition.get(key) or {}
        if isinstance(body, dict) and body.get("statement"):
            return str(body["statement"])
    return ""


def policy_status_reasons(policy: dict[str, Any]) -> list[str]:
    reasons = policy.get("statusReasons")
    if reasons is None:
        reasons = policy.get("statusReason") or []
    if isinstance(reasons, str):
        return [reasons]
    return [str(item) for item in reasons]


def format_policy(policy: dict[str, Any], name: str = "") -> str:
    reasons = policy_status_reasons(policy)
    return (
        f"policy {policy.get('name') or name} "
        f"status={policy.get('status')} enforcement={policy.get('enforcementMode')} "
        f"statusReasons={reasons or 'none'} statement={policy_statement_text(policy)!r}"
    )


def action_id(target_name: str, tool_name: str) -> str:
    """Cedar action id is the MCP tool name. A name that already includes the target is kept."""
    for separator in (TOOL_SEPARATOR, "__"):
        if tool_name.startswith(f"{target_name}{separator}"):
            return tool_name
    return f"{target_name}{TOOL_SEPARATOR}{tool_name}"


def target_tool_names(target: dict[str, Any]) -> list[str]:
    """Tool names from GetGatewayTarget: overrides, then any inline tool schema."""
    config = ((target.get("targetConfiguration") or {}).get("mcp") or {})
    api = config.get("apiGateway") or {}
    tool_config = api.get("apiGatewayToolConfiguration") or {}
    names: list[str] = []
    for item in tool_config.get("toolOverrides") or []:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    for name in _schema_tool_names(config):
        if name not in names:
            names.append(name)
    return names


def permitted_action_ids(target_name: str, tool_names: list[str]) -> list[tuple[str, str]]:
    paired: list[tuple[str, str]] = []
    for policy_name, tool_name in POLICY_TOOLS:
        match = tool_name
        for discovered in tool_names:
            if discovered == tool_name or discovered.endswith(f"{TOOL_SEPARATOR}{tool_name}") or discovered.endswith(f"__{tool_name}"):
                match = discovered
                break
        paired.append((policy_name, action_id(target_name, match)))
    return paired


def iam_entity_id(account: str, role_name: str) -> str:
    """Cedar strips the role session name before evaluation.

    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-conditions.html
    """
    return f"arn:aws:sts::{account}:assumed-role/{role_name}"


def permit_statement(gateway_arn: str, action: str, principal_id: str) -> str:
    """One IAM role, one action, one gateway. An unconstrained IamEntity is overly permissive.

    The id has no session suffix. The trailing * still matches that id, and a session suffix if one is present.
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies.html
    """
    return (
        "permit (\n"
        "  principal is AgentCore::IamEntity,\n"
        f'  action == AgentCore::Action::"{action}",\n'
        f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
        ")\n"
        "when {\n"
        f'  principal.id like "{principal_id}*"\n'
        "};\n"
    )


def _schema_tool_names(node: Any) -> list[str]:
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            inline = value.get("inlinePayload")
            tools: list[Any] = []
            if isinstance(inline, list):
                tools = inline
            elif isinstance(inline, str) and inline.strip().startswith("["):
                try:
                    parsed = json.loads(inline)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    tools = parsed
            for tool in tools:
                if isinstance(tool, dict) and tool.get("name"):
                    found.append(str(tool["name"]))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return found


def tools_or_allowed_scope(names: list[str]) -> tuple[list[str], bool]:
    """ENFORCE hides tools the caller cannot list. Fall back to the Cedar permits."""
    if names:
        return names, False
    return list(ALLOWED_SCOPE), True


def is_named_gateway(item: dict[str, Any], name: str) -> bool:
    """ListGateways returns items. The id is the lowercased name plus a suffix."""
    wanted = name.casefold()
    current = str(item.get("name") or "").casefold()
    identifier = str(item.get("gatewayId") or item.get("id") or "").casefold()
    return current == wanted or identifier == wanted or identifier.startswith(wanted + "-")


def find_gateway_summary(pages: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for page in pages:
        for item in _items(page, "items", "gateways", "gatewaySummaries"):
            if isinstance(item, dict) and is_named_gateway(item, name):
                return item
    return None


def policy_engine_attached(gateway: dict[str, Any], engine_arn: str) -> bool:
    policy = gateway.get("policyEngineConfiguration") or {}
    return policy.get("arn") == engine_arn and str(policy.get("mode") or "").upper() == "ENFORCE"


def parse_checker_output(text: str) -> dict[str, Any] | None:
    """Read the first balanced object. The last '{' is inside findings, not the root."""
    stripped = text.strip()
    if not stripped:
        return None
    whole = _load_object(stripped)
    if whole is not None:
        return whole
    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(stripped[start:], start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return _load_object(stripped[start : index + 1])
    return None


def checker_result_ok(parsed: dict[str, Any] | None) -> bool:
    if not isinstance(parsed, dict):
        return False
    rules = {item.get("rule") for item in parsed.get("findings") or [] if isinstance(item, dict)}
    return parsed.get("ok") is False and EXPECTED_CHECKER_RULES <= rules


def _load_object(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_policy_denial(body: str) -> bool:
    if _is_protocol_error(body):
        return False
    lowered = body.lower()
    markers = (
        "tool execution denied",
        "denied by policy",
        "denied by default",
        "no policy applies",
        "authorizeactionexception",
        "policy enforcement",
        "explicit deny",
        "access denied",
        "accessdenied",
        "not authorized",
        "authorization denied",
    )
    return any(marker in lowered for marker in markers)


def _is_protocol_error(body: str) -> bool:
    parsed = _jsonrpc_payload(body)
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(error, dict):
        return "unsupported protocol version" in body.lower()
    return "protocol version" in str(error.get("message") or "").lower()


def _is_success_payload(status: int, body: str) -> bool:
    if status <= 0 or status >= 400 or _is_policy_denial(body):
        return False
    parsed = _jsonrpc_payload(body)
    if not isinstance(parsed, dict) or "error" in parsed:
        return False
    result = parsed.get("result")
    if not isinstance(result, dict) or result.get("isError") is True:
        return False
    content = result.get("content") or result.get("structuredContent")
    if not content:
        return False
    text = json.dumps(content)
    return bool(text.strip()) and text not in {"[]", "{}", "null", '""'}


def _jsonrpc_payload(body: str) -> dict[str, Any] | None:
    text = body.strip()
    if not text:
        return None
    candidates = [text]
    data_lines = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    if data_lines:
        candidates.append("\n".join(data_lines))
        candidates.extend(data_lines)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def retry_iam(action, description: str):
    from botocore.exceptions import ClientError

    for attempt in range(1, 9):
        try:
            return action()
        except ClientError as exc:
            error = exc.response.get("Error", {})
            code = str(error.get("Code") or "")
            message = str(error.get("Message") or "")
            retryable = code in {"AccessDenied", "AccessDeniedException", "MalformedPolicyDocument"} or "Invalid principal" in message or "cannot be assumed" in message.lower()
            if not retryable or attempt == 8:
                raise
            print(f"Retry {attempt + 1}/8 for {description}: {code}")
            time.sleep(10)
    raise RuntimeError(description)


def ensure_boto3() -> None:
    import importlib

    def supported() -> bool:
        import boto3

        control = boto3.client("bedrock-agentcore-control", region_name="us-east-1")
        data = boto3.client("bedrock-agentcore", region_name="us-east-1")
        needed = (
            "create_code_interpreter",
            "create_policy_engine",
            "create_gateway",
            "create_gateway_target",
            "create_policy",
        )
        return all(hasattr(control, name) for name in needed) and hasattr(data, "start_code_interpreter_session")

    try:
        if supported():
            print(f"boto3 {importlib.import_module('boto3').__version__} supports AgentCore control and data plane.")
            return
    except Exception:
        pass
    import subprocess

    print("Upgrading boto3 so AgentCore operations are available.")
    command = [sys.executable, "-m", "pip", "install", "--user", "--upgrade", "boto3"]
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        subprocess.run(command + ["--break-system-packages"], check=True)
    importlib.invalidate_caches()
    if not supported():
        raise SystemExit("boto3 still has no bedrock-agentcore-control create_code_interpreter after upgrade")
    print(f"boto3 {importlib.import_module('boto3').__version__} supports AgentCore control and data plane.")


def enable(region: str) -> None:
    import os

    import boto3

    ensure_boto3()
    account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    api_id = _review_api_id(boto3.client("cloudformation", region_name=region), os.environ.get("STACK_NAME", "api-review"))
    iam = boto3.client("iam")
    interpreter_trust = code_interpreter_source_arn(account, region)
    gateway_trust = gateway_source_arn(account, region)
    execution = os.environ.get("EXECUTION_ROLE_ARN") or _ensure_role(
        iam, INTERPRETER_ROLE, trust_policy(account, region, interpreter_trust), None, "code interpreter role"
    )
    gateway_role = os.environ.get("GATEWAY_ROLE_ARN") or _ensure_role(
        iam, GATEWAY_ROLE, trust_policy(account, region, gateway_trust), gateway_permissions(account, region, api_id), "gateway role"
    )
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    interpreter_id = _ensure_interpreter(control, execution)
    engine = _ensure_engine(control)
    _wait_ready(lambda: control.get_policy_engine(policyEngineId=engine["id"]), "policy engine")
    gateway = _ensure_gateway(control, gateway_role, engine["arn"])
    _wait_ready(lambda: control.get_gateway(gatewayIdentifier=gateway["id"]), "gateway")
    _ensure_target(control, gateway["id"], api_id)
    target_id = _target_id(control, gateway["id"])
    if not target_id:
        raise SystemExit(f"Gateway target {TARGET_NAME} was not created")
    target = control.get_gateway_target(gatewayIdentifier=gateway["id"], targetId=target_id)
    discovered = target_tool_names(target)
    print(f"target tools {discovered}")
    _grant_gateway_invoke(iam, CALLER_ROLE, gateway["arn"])
    _grant_gateway_invoke(iam, DENY_ROLE, gateway["arn"])
    _ensure_policies(control, engine["id"], gateway["arn"], str(target.get("name") or TARGET_NAME), discovered, account)
    state = {
        "account": account,
        "engineArn": engine["arn"],
        "engineId": engine["id"],
        "executionRoleArn": execution,
        "gatewayArn": gateway["arn"],
        "gatewayId": gateway["id"],
        "gatewayRoleArn": gateway_role,
        "gatewayUrl": gateway["url"],
        "interpreterId": interpreter_id,
        "region": region,
        "restApiId": api_id,
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"AgentCore gateway: {gateway['arn']}")
    print(f"Code interpreter: {interpreter_id}")
    print("Set the runtime to call only read_artifacts, request_spec_check, and read_test_results.")
    print("Do not add an approve, merge, or deploy tool.")


def teardown(region: str) -> None:
    import boto3

    ensure_boto3()
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    state = {}
    if STATE_PATH.is_file():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    _delete_named_gateway(control, state.get("gatewayId"))
    _delete_named_engine(control, state.get("engineId"))
    _delete_named_interpreter(control, state.get("interpreterId"))
    iam = boto3.client("iam")
    for name in (INTERPRETER_ROLE, GATEWAY_ROLE):
        _delete_role(iam, name)
    if STATE_PATH.is_file():
        STATE_PATH.unlink()
    print("Removed tagged AgentCore roles and api-review AgentCore resources.")


def validate(region: str) -> int:
    import boto3

    ensure_boto3()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.is_file() else {}
    if not state:
        raise SystemExit(f"missing {STATE_PATH}; run scripts/enable_agentcore.sh first")
    ambient = boto3.client("sts", region_name=region).get_caller_identity()["Arn"]
    print(f"ambient caller {ambient}")
    print(f"gateway caller profile {CALLER_PROFILE} role {CALLER_ROLE}")
    failures = 0
    failures += _validate_gateway(region, state)
    failures += _validate_interpreter(region, state)
    if failures:
        print(f"FAIL agentcore-validation {failures}")
        return 1
    print("PASS agentcore-validation")
    return 0


def _validate_gateway(region: str, state: dict[str, str]) -> int:
    _wait_review_target(region, state)
    _print_policies(region, state)
    job_id = _job_id()
    version = _gateway_protocol(region, state)
    try:
        caller = _profile_session(CALLER_PROFILE)
        caller_arn = caller.client("sts", region_name=region).get_caller_identity()["Arn"]
    except Exception as exc:
        print(f"FAIL gateway-read: cannot assume {CALLER_PROFILE}: {exc}")
        print(f"FAIL policy-deny: cannot assume {CALLER_PROFILE}: {exc}")
        print(f"FAIL other-identity: cannot assume {CALLER_PROFILE}: {exc}")
        return 3
    print(f"gateway caller {caller_arn}")
    session = _McpSession(region, state.get("gatewayUrl", ""), version, caller)
    try:
        session.open()
    except Exception as exc:
        detail = str(exc)[:300]
        print(f"FAIL gateway-read: {detail}")
        print(f"FAIL policy-deny: {detail}")
        print(f"FAIL other-identity: {detail}")
        return 3
    list_status, list_body = session.request("tools/list", {})
    print(f"tools/list {list_body}")
    names, used_scope = tools_or_allowed_scope(tool_names_from_list(list_body))
    if used_scope:
        names = _permitted_names(region, state) or names
        print("allowed-scope " + ", ".join(repr(name) for name in names))
        print("tools/list is empty because ENFORCE shows only tools an ACTIVE permit allows. A failed policy permits nothing.")
    read_name, approve_name = select_gateway_tools(names)
    if not read_name or not approve_name:
        detail = list_body[:300] if list_status else "tools/list returned no read_artifacts tool"
        print(f"FAIL gateway-read: {detail}")
        print(f"FAIL policy-deny: {detail}")
        return 2
    allow_status, allow_body = session.request("tools/call", {"name": read_name, "arguments": {"jobId": job_id}})
    allow_ok, allow_detail = classify_tool_result(True, allow_status, allow_body)
    print(f"{'PASS' if allow_ok else 'FAIL'} gateway-read {read_name}" + ("" if allow_ok else f": {allow_detail}"))
    deny_status, deny_body = session.request(
        "tools/call",
        {"name": approve_name, "arguments": {"jobId": job_id, "decision": "approve"}},
    )
    deny_ok, deny_detail = classify_tool_result(False, deny_status, deny_body)
    print(f"{'PASS' if deny_ok else 'FAIL'} policy-deny {approve_name}" + ("" if deny_ok else f": {deny_detail}"))
    other_ok, other_detail = _other_identity_denied(region, state, version, read_name, job_id)
    print(f"{'PASS' if other_ok else 'FAIL'} other-identity {read_name}" + ("" if other_ok else f": {other_detail}"))
    return int(not allow_ok) + int(not deny_ok) + int(not other_ok)


def _gateway_protocol(region: str, state: dict[str, str]) -> str:
    gateway_id = state.get("gatewayId") or ""
    if not gateway_id:
        return MCP_PROTOCOL_VERSION
    try:
        import boto3

        gateway = boto3.client("bedrock-agentcore-control", region_name=region).get_gateway(gatewayIdentifier=gateway_id)
    except Exception:
        return MCP_PROTOCOL_VERSION
    return protocol_from_gateway(gateway)


def _validate_interpreter(region: str, state: dict[str, str]) -> int:
    from apireview.checker import build_checker_source

    spec = json.loads((ROOT / "fixtures" / "orders-v2-broken.openapi.json").read_text(encoding="utf-8"))
    code = build_checker_source(spec)
    import boto3

    client = boto3.client("bedrock-agentcore", region_name=region)
    interpreter_id = state["interpreterId"]
    session_id = ""
    try:
        started = _start_interpreter_session(client, interpreter_id)
        session_id = started["sessionId"]
        response = client.invoke_code_interpreter(
            codeInterpreterIdentifier=interpreter_id,
            sessionId=session_id,
            name="executeCode",
            arguments={"language": "python", "code": code},
        )
        text = _stream_text(response)
    except Exception as exc:
        print(f"FAIL code-interpreter diff: {exc}")
        return 1
    finally:
        if session_id:
            try:
                client.stop_code_interpreter_session(codeInterpreterIdentifier=interpreter_id, sessionId=session_id)
            except Exception:
                pass
    parsed = parse_checker_output(text)
    ok = checker_result_ok(parsed)
    print("PASS code-interpreter diff" if ok else f"FAIL code-interpreter diff: {text[:300]}")
    return 0 if ok else 1


def _start_interpreter_session(client, interpreter_id: str) -> dict[str, Any]:
    from botocore.exceptions import ClientError

    for attempt in range(1, 6):
        try:
            return client.start_code_interpreter_session(
                codeInterpreterIdentifier=interpreter_id,
                name=SESSION_NAME,
                sessionTimeoutSeconds=120,
            )
        except ClientError as exc:
            message = str(exc.response.get("Error", {}).get("Message") or "")
            if "cannot be assumed" not in message.lower() or attempt == 5:
                raise
            print(f"Retry {attempt + 1}/5 for code interpreter session: execution role is not assumable yet")
            time.sleep(5)
    raise RuntimeError("start code interpreter session")


def _profile_session(profile: str):
    import boto3

    return boto3.Session(profile_name=profile)


def _other_identity_denied(region: str, state: dict[str, str], version: str, tool_name: str, job_id: str) -> tuple[bool, str]:
    try:
        other = _profile_session(DENY_PROFILE)
        other_arn = other.client("sts", region_name=region).get_caller_identity()["Arn"]
    except Exception as exc:
        return False, f"cannot assume {DENY_PROFILE}: {exc}"
    print(f"other caller {other_arn}")
    session = _McpSession(region, state.get("gatewayUrl", ""), version, other)
    try:
        session.open()
    except Exception as exc:
        return False, str(exc)[:300]
    status, body = session.request("tools/call", {"name": tool_name, "arguments": {"jobId": job_id}})
    return classify_tool_result(False, status, body)


class _McpSession:
    def __init__(self, region: str, gateway_url: str, protocol: str, aws_session=None) -> None:
        self.region = region
        self.gateway_url = gateway_url
        self.protocol = protocol
        self.aws_session = aws_session
        self.session_id = ""

    def open(self) -> None:
        status, body, headers = self._post(
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {
                    "protocolVersion": self.protocol,
                    "capabilities": {},
                    "clientInfo": {"name": "api-review-validate", "version": "1"},
                },
            }
        )
        switched = negotiated_protocol(body, self.protocol)
        if switched:
            self.protocol = switched
            status, body, headers = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": "init",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": self.protocol,
                        "capabilities": {},
                        "clientInfo": {"name": "api-review-validate", "version": "1"},
                    },
                }
            )
        if status >= 400 or _jsonrpc_payload(body) is None or "error" in (_jsonrpc_payload(body) or {}):
            raise RuntimeError(body[:300] or f"initialize failed with status {status}")
        self.session_id = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id") or ""
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def request(self, method: str, params: dict[str, Any]) -> tuple[int, str]:
        status, body, _headers = self._post({"jsonrpc": "2.0", "id": "1", "method": method, "params": params})
        return status, body

    def _post(self, payload: dict[str, Any]) -> tuple[int, str, dict[str, str]]:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.session import Session
        import urllib.request

        url = self.gateway_url.rstrip("/")
        if not url.endswith("/mcp"):
            url += "/mcp"
        encoded = json.dumps(payload).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = AWSRequest(method="POST", url=url, data=encoded, headers=headers)
        source = self.aws_session or Session()
        credentials = source.get_credentials()
        if credentials is None:
            return 0, "no AWS credentials", {}
        SigV4Auth(credentials.get_frozen_credentials(), "bedrock-agentcore", self.region).add_auth(request)
        outbound = urllib.request.Request(url, data=encoded, method="POST", headers=dict(request.headers.items()))
        try:
            with urllib.request.urlopen(outbound, timeout=60) as response:
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                return response.status, response.read().decode("utf-8", errors="replace"), response_headers
        except Exception as exc:
            status = getattr(exc, "code", 0) or 0
            body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
            raw_headers = getattr(exc, "headers", None)
            response_headers = {key.lower(): value for key, value in raw_headers.items()} if raw_headers else {}
            return int(status or 0), body, response_headers


def _stream_text(response: dict[str, Any]) -> str:
    chunks: list[str] = []
    for event in response.get("stream") or []:
        _collect_stream_text(event, chunks)
    return "".join(chunks)


def _collect_stream_text(node: Any, chunks: list[str]) -> None:
    if isinstance(node, dict):
        if node.get("type") == "text":
            chunks.append(str(node.get("text") or ""))
            return
        stdout = node.get("stdout")
        if isinstance(stdout, str):
            chunks.append(stdout)
        for key, value in node.items():
            if key in {"text", "stdout"}:
                continue
            _collect_stream_text(value, chunks)
    elif isinstance(node, list):
        for item in node:
            _collect_stream_text(item, chunks)


def _job_id() -> str:
    import os

    chosen = os.environ.get("JOB_ID", "")
    if chosen:
        return chosen
    path = Path.home() / ".api-review-last-job"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return "job_" + "ab" * 10


def _review_api_id(cloudformation, stack_name: str) -> str:
    from urllib.parse import urlparse

    stacks = cloudformation.describe_stacks(StackName=stack_name)["Stacks"]
    endpoint = ""
    for row in stacks[0].get("Outputs", []):
        if row["OutputKey"] == "ReviewApiEndpoint":
            endpoint = row["OutputValue"]
    parsed = urlparse(endpoint)
    host = parsed.hostname or ""
    parts = host.split(".")
    if len(parts) < 3 or parts[1] != "execute-api":
        raise SystemExit(f"ReviewApiEndpoint is missing on stack {stack_name}")
    return parts[0]


def _ensure_role(iam, name: str, trust: dict[str, Any], policy: dict[str, Any] | None, description: str) -> str:
    document = json.dumps(trust)
    try:
        iam.get_role(RoleName=name)
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=document)
        print(f"Updated role {name}.")
    except iam.exceptions.NoSuchEntityException:
        retry_iam(
            lambda: iam.create_role(
                RoleName=name,
                AssumeRolePolicyDocument=document,
                MaxSessionDuration=3600,
                Tags=[{"Key": TAG_KEY, "Value": TAG_VALUE}, {"Key": "Project", "Value": "api-review"}],
            ),
            description,
        )
        print(f"Created role {name}.")
    if policy is not None:
        iam.put_role_policy(RoleName=name, PolicyName="api-review-agentcore", PolicyDocument=json.dumps(policy))
    iam.tag_role(RoleName=name, Tags=[{"Key": TAG_KEY, "Value": TAG_VALUE}])
    return iam.get_role(RoleName=name)["Role"]["Arn"]


def _ensure_interpreter(control, role_arn: str) -> str:
    existing = _named_id(control.list_code_interpreters, ("codeInterpreters", "codeInterpreterSummaries"), INTERPRETER_NAME, "codeInterpreterId")
    if existing:
        print(f"Code interpreter {INTERPRETER_NAME} already exists.")
        return existing
    try:
        response = retry_iam(
            lambda: control.create_code_interpreter(
                name=INTERPRETER_NAME,
                description="Fixed specification checker. SANDBOX is not an air gap.",
                executionRoleArn=role_arn,
                networkConfiguration={"networkMode": "SANDBOX"},
                tags={TAG_KEY: TAG_VALUE},
            ),
            "create code interpreter",
        )
    except Exception as exc:
        if not _is_conflict(exc):
            raise
        existing = _named_id(control.list_code_interpreters, ("codeInterpreters", "codeInterpreterSummaries"), INTERPRETER_NAME, "codeInterpreterId")
        if not existing:
            raise
        print(f"Code interpreter {INTERPRETER_NAME} already exists.")
        return existing
    return response["codeInterpreterId"]


def _ensure_engine(control) -> dict[str, str]:
    found = _named_record(control.list_policy_engines, ("policyEngines", "policyEngineSummaries"), ENGINE_NAME, "policyEngineId", "policyEngineArn")
    if found:
        print(f"Policy engine {ENGINE_NAME} already exists.")
        return found
    try:
        response = control.create_policy_engine(name=ENGINE_NAME, description="Read-only tools for the API review gateway.", tags={TAG_KEY: TAG_VALUE})
    except Exception as exc:
        if not _is_conflict(exc):
            raise
        found = _named_record(control.list_policy_engines, ("policyEngines", "policyEngineSummaries"), ENGINE_NAME, "policyEngineId", "policyEngineArn")
        if not found:
            raise
        print(f"Policy engine {ENGINE_NAME} already exists.")
        return found
    return {"id": response["policyEngineId"], "arn": response.get("policyEngineArn") or response.get("arn")}


def _ensure_gateway(control, role_arn: str, engine_arn: str) -> dict[str, str]:
    found = find_gateway_summary(_list_pages(control.list_gateways), GATEWAY_NAME)
    if found:
        return _reuse_gateway(control, found, role_arn, engine_arn)
    try:
        response = _retry_assume_role(
            lambda: control.create_gateway(
                name=GATEWAY_NAME,
                description="MCP gateway for the three read-only review tools.",
                authorizerType="AWS_IAM",
                protocolType="MCP",
                roleArn=role_arn,
                policyEngineConfiguration={"arn": engine_arn, "mode": "ENFORCE"},
                tags={TAG_KEY: TAG_VALUE},
            ),
            "create gateway",
        )
    except Exception as exc:
        if not _is_conflict(exc):
            raise
        found = find_gateway_summary(_list_pages(control.list_gateways), GATEWAY_NAME)
        if not found:
            raise
        return _reuse_gateway(control, found, role_arn, engine_arn)
    return _gateway_record(response)


def _reuse_gateway(control, summary: dict[str, Any], role_arn: str, engine_arn: str) -> dict[str, str]:
    identifier = summary.get("gatewayId") or summary.get("id")
    current = control.get_gateway(gatewayIdentifier=identifier)
    if policy_engine_attached(current, engine_arn):
        print(f"Gateway {current.get('name') or GATEWAY_NAME} already exists.")
        return _gateway_record(current)
    print(f"Updating gateway {current.get('name') or GATEWAY_NAME} to attach the policy engine.")
    payload: dict[str, Any] = {
        "gatewayIdentifier": current.get("gatewayId") or identifier,
        "name": current.get("name") or GATEWAY_NAME,
        "authorizerType": current.get("authorizerType") or "AWS_IAM",
        "protocolType": current.get("protocolType") or "MCP",
        "roleArn": current.get("roleArn") or role_arn,
        "description": current.get("description") or "MCP gateway for the three read-only review tools.",
        "policyEngineConfiguration": {"arn": engine_arn, "mode": "ENFORCE"},
    }
    if isinstance(current.get("protocolConfiguration"), dict):
        payload["protocolConfiguration"] = current["protocolConfiguration"]
    updated = _retry_assume_role(lambda: control.update_gateway(**payload), "update gateway")
    return _gateway_record(updated)


def _gateway_record(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "id": payload.get("gatewayId") or "",
        "arn": payload.get("gatewayArn") or payload.get("arn") or "",
        "url": payload.get("gatewayUrl") or "",
    }


def _retry_assume_role(action, description: str):
    from botocore.exceptions import ClientError

    for attempt in range(1, 7):
        try:
            return action()
        except ClientError as exc:
            message = str(exc.response.get("Error", {}).get("Message") or "")
            if "assumerole" not in message.lower() or attempt == 6:
                raise
            print(f"Retry {attempt + 1}/6 for {description}: role is not assumable yet")
            time.sleep(10)
    raise RuntimeError(description)


def _ensure_target(control, gateway_id: str, api_id: str) -> None:
    target_id = _target_id(control, gateway_id)
    if target_id:
        print(f"Gateway target {TARGET_NAME} already exists.")
    else:
        try:
            created = control.create_gateway_target(
                gatewayIdentifier=gateway_id,
                name=TARGET_NAME,
                description="Review API routes. Approve is exposed so policy can deny it.",
                targetConfiguration={
                    "mcp": {
                        "apiGateway": {
                            "restApiId": api_id,
                            "stage": "live",
                            "apiGatewayToolConfiguration": tool_configuration(),
                        }
                    }
                },
                credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
            )
            target_id = created.get("targetId") or _target_id(control, gateway_id)
        except Exception as exc:
            if not _is_conflict(exc):
                raise
            target_id = _target_id(control, gateway_id)
            print(f"Gateway target {TARGET_NAME} already exists.")
    if not target_id:
        raise SystemExit(f"Gateway target {TARGET_NAME} was not created")
    _wait_ready(
        lambda: control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id),
        "gateway target",
    )


def _target_id(control, gateway_id: str) -> str:
    listed = control.list_gateway_targets(gatewayIdentifier=gateway_id)
    for item in listed.get("items") or listed.get("targets") or []:
        if str(item.get("name") or "").casefold() == TARGET_NAME.casefold() and item.get("targetId"):
            return item["targetId"]
    return ""


def _wait_review_target(region: str, state: dict[str, str]) -> None:
    gateway_id = state.get("gatewayId") or ""
    if not gateway_id:
        return
    import boto3

    control = boto3.client("bedrock-agentcore-control", region_name=region)
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            target_id = _target_id(control, gateway_id)
            payload = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id) if target_id else {}
        except Exception:
            time.sleep(5)
            continue
        status = str(payload.get("status") or "").upper()
        if target_id and status in {"READY", "ACTIVE"}:
            print(f"Gateway target {TARGET_NAME} is {status}.")
            return
        if "FAIL" in status or "UNSUCCESSFUL" in status:
            raise SystemExit(f"gateway target entered {status}")
        time.sleep(5)
    raise SystemExit("gateway target was not ready")


def _ensure_policies(control, engine_id: str, gateway_arn: str, target_name: str, tool_names: list[str], account: str) -> None:
    principal = iam_entity_id(account, CALLER_ROLE)
    desired = [
        (name, action, permit_statement(gateway_arn, action, principal))
        for name, action in permitted_action_ids(target_name, tool_names)
    ]
    current = {item.get("name"): item for item in _policy_records(control, engine_id)}
    for name, action, statement in desired:
        print(f"policy {name} desired-action={action!r}")
        found = current.get(name)
        if not found:
            continue
        policy_id = found.get("policyId") or found.get("id") or ""
        detail = control.get_policy(policyEngineId=engine_id, policyId=policy_id)
        status = str(detail.get("status") or "").upper()
        same = status == "ACTIVE" and detail.get("enforcementMode") == "ACTIVE" and statements_match(policy_statement_text(detail), statement)
        if same:
            print(f"Policy {name} already exists.")
            print(format_policy(detail, name))
            continue
        print(format_policy(detail, name))
        _delete_policy(control, engine_id, policy_id, name)
    failures: list[str] = []
    current = {item.get("name"): item for item in _policy_records(control, engine_id)}
    for name, _action, statement in desired:
        found = current.get(name)
        if found:
            policy_id = found.get("policyId") or found.get("id") or ""
            detail = control.get_policy(policyEngineId=engine_id, policyId=policy_id)
            if str(detail.get("status") or "").upper() == "ACTIVE" and statements_match(policy_statement_text(detail), statement):
                continue
            print(format_policy(detail, name))
            failures.append(format_policy(detail, name))
            continue
        detail = _create_policy(control, engine_id, name, statement)
        if str(detail.get("status") or "").upper() != "ACTIVE":
            failures.append(format_policy(detail, name))
    if failures:
        raise SystemExit("policies were not active\n" + "\n".join(failures))


def _grant_gateway_invoke(iam, role_name: str, gateway_arn: str) -> None:
    try:
        iam.get_role(RoleName=role_name)
    except Exception:
        print(f"Role {role_name} is not present, so validate cannot use it yet.")
        return
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeReviewGateway",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:InvokeGateway"],
                "Resource": [gateway_arn],
            }
        ],
    }
    iam.put_role_policy(RoleName=role_name, PolicyName="api-review-invoke-gateway", PolicyDocument=json.dumps(document))
    print(f"Granted {role_name} bedrock-agentcore:InvokeGateway.")


def _create_policy(control, engine_id: str, name: str, statement: str) -> dict[str, Any]:
    # Schema and semantic checks run asynchronously. statusReasons is the report.
    # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-create-update-validation.html
    created = control.create_policy(
        policyEngineId=engine_id,
        name=name,
        validationMode=POLICY_VALIDATION_MODE,
        enforcementMode="ACTIVE",
        definition={"cedar": {"statement": statement}},
    )
    print(f"Created policy {name}.")
    return _wait_policy(control, engine_id, created.get("policyId") or "", name)


def _delete_policy(control, engine_id: str, policy_id: str, name: str) -> None:
    try:
        control.delete_policy(policyEngineId=engine_id, policyId=policy_id)
    except Exception as exc:
        if not _is_not_found(exc):
            raise
    deadline = time.time() + 180
    while time.time() < deadline:
        gone = False
        try:
            detail = control.get_policy(policyEngineId=engine_id, policyId=policy_id)
        except Exception as exc:
            if not _is_not_found(exc):
                time.sleep(5)
                continue
            gone = True
            detail = {}
        if not gone:
            status = str(detail.get("status") or "").upper()
            if status == "DELETE_FAILED":
                raise SystemExit(format_policy(detail, name))
            time.sleep(5)
            continue
        if not any(item.get("name") == name for item in _policy_records(control, engine_id)):
            print(f"Deleted policy {name}.")
            return
        time.sleep(5)
    raise SystemExit(f"policy {name} was not deleted")


def _is_not_found(exc: BaseException) -> bool:
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
    text = str(exc).lower()
    return "NotFound" in type(exc).__name__ or "NotFound" in code or "not found" in text


def _policy_records(control, engine_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    token = None
    while True:
        kwargs: dict[str, Any] = {"policyEngineId": engine_id}
        if token:
            kwargs["nextToken"] = token
        page = control.list_policies(**kwargs)
        records.extend(_items(page, "items", "policies", "policySummaries"))
        token = page.get("nextToken") or page.get("NextToken")
        if not token:
            return records


def _wait_policy(control, engine_id: str, policy_id: str, name: str) -> dict[str, Any]:
    if not policy_id:
        raise SystemExit(f"policy {name} has no id")
    deadline = time.time() + 180
    while time.time() < deadline:
        detail = control.get_policy(policyEngineId=engine_id, policyId=policy_id)
        status = str(detail.get("status") or "").upper()
        if status == "ACTIVE" or status.endswith("FAILED"):
            print(format_policy(detail, name))
            return detail
        time.sleep(5)
    raise SystemExit(f"policy {name} was not ready")


def _permitted_names(region: str, state: dict[str, str]) -> list[str]:
    gateway_id = state.get("gatewayId") or ""
    if not gateway_id:
        return []
    try:
        import boto3

        control = boto3.client("bedrock-agentcore-control", region_name=region)
        target_id = _target_id(control, gateway_id)
        if not target_id:
            return []
        target = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
    except Exception as exc:
        print(f"target tools unavailable: {exc}")
        return []
    discovered = target_tool_names(target)
    print(f"target tools {discovered}")
    return [action for _name, action in permitted_action_ids(str(target.get("name") or TARGET_NAME), discovered)]


def _print_policies(region: str, state: dict[str, str]) -> None:
    engine_id = state.get("engineId") or ""
    if not engine_id:
        print("policy none: missing engine id")
        return
    import boto3

    control = boto3.client("bedrock-agentcore-control", region_name=region)
    try:
        records = _policy_records(control, engine_id)
    except Exception as exc:
        print(f"policy list failed: {exc}")
        return
    if not records:
        print("policy none")
        return
    for item in records:
        policy_id = item.get("policyId") or item.get("id")
        try:
            detail = control.get_policy(policyEngineId=engine_id, policyId=policy_id) if policy_id else item
        except Exception as exc:
            print(f"policy {item.get('name')} get failed: {exc}")
            continue
        print(format_policy(detail, str(item.get("name") or "")))


def _wait_ready(reader, label: str) -> None:
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            payload = reader()
        except Exception:
            time.sleep(5)
            continue
        status = str(payload.get("status") or payload.get("Status") or "").upper()
        if status in {"READY", "ACTIVE", ""}:
            return
        if "FAIL" in status or "UNSUCCESSFUL" in status:
            reasons = payload.get("statusReasons") or []
            detail = f" statusReasons={reasons}" if reasons else ""
            raise SystemExit(f"{label} entered {status}{detail}")
        time.sleep(5)
    raise SystemExit(f"{label} was not ready")


def _is_conflict(exc: BaseException) -> bool:
    return "Conflict" in type(exc).__name__ or "already" in str(exc).lower()


def _named_id(lister, keys: tuple[str, ...], name: str, id_key: str) -> str:
    for item in _items(lister(), *keys):
        if item.get("name") == name and item.get(id_key):
            return item[id_key]
    return ""


def _named_record(lister, keys: tuple[str, ...], name: str, id_key: str, arn_key: str, url_key: str = "") -> dict[str, str] | None:
    for item in _items(lister(), *keys):
        if item.get("name") != name:
            continue
        found = {"id": item.get(id_key) or "", "arn": item.get(arn_key) or item.get("arn") or ""}
        if url_key:
            found["url"] = item.get(url_key) or ""
        return found
    return None


def _items(response: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for key in keys:
        found.extend(response.get(key) or [])
    return found


def _list_pages(lister) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    token = None
    while True:
        page = lister(nextToken=token) if token else lister()
        pages.append(page)
        token = page.get("nextToken") or page.get("NextToken")
        if not token:
            return pages


def _delete_named_gateway(control, gateway_id: str | None) -> None:
    identifiers = [gateway_id] if gateway_id else []
    try:
        pages = _list_pages(control.list_gateways)
    except Exception:
        pages = []
    for page in pages:
        for item in _items(page, "items", "gateways", "gatewaySummaries"):
            if is_named_gateway(item, GATEWAY_NAME):
                identifiers.append(item.get("gatewayId"))
    for identifier in dict.fromkeys(item for item in identifiers if item):
        try:
            targets = control.list_gateway_targets(gatewayIdentifier=identifier)
        except Exception:
            targets = {}
        for target in targets.get("items") or targets.get("targets") or []:
            target_id = target.get("targetId") or target.get("id")
            if target_id:
                try:
                    control.delete_gateway_target(gatewayIdentifier=identifier, targetId=target_id)
                except Exception:
                    pass
        try:
            control.delete_gateway(gatewayIdentifier=identifier)
        except Exception:
            pass


def _delete_named_engine(control, engine_id: str | None) -> None:
    identifiers = [engine_id] if engine_id else []
    try:
        listed = control.list_policy_engines()
    except Exception:
        listed = {}
    for item in listed.get("policyEngines") or listed.get("policyEngineSummaries") or []:
        if item.get("name") == ENGINE_NAME:
            identifiers.append(item.get("policyEngineId"))
    for identifier in dict.fromkeys(item for item in identifiers if item):
        try:
            policies = control.list_policies(policyEngineId=identifier)
        except Exception:
            policies = {}
        for policy in policies.get("policies") or policies.get("policySummaries") or []:
            policy_id = policy.get("policyId") or policy.get("id")
            if policy_id:
                try:
                    control.delete_policy(policyEngineId=identifier, policyId=policy_id)
                except Exception:
                    pass
        try:
            control.delete_policy_engine(policyEngineId=identifier)
        except Exception:
            pass


def _delete_named_interpreter(control, interpreter_id: str | None) -> None:
    identifiers = [interpreter_id] if interpreter_id else []
    try:
        listed = control.list_code_interpreters()
    except Exception:
        listed = {}
    for item in listed.get("codeInterpreters") or listed.get("codeInterpreterSummaries") or []:
        if item.get("name") == INTERPRETER_NAME:
            identifiers.append(item.get("codeInterpreterId"))
    for identifier in dict.fromkeys(item for item in identifiers if item):
        try:
            control.delete_code_interpreter(codeInterpreterId=identifier)
        except Exception:
            pass


def _delete_role(iam, name: str) -> None:
    try:
        policies = iam.list_role_policies(RoleName=name).get("PolicyNames", [])
    except Exception:
        return
    for policy in policies:
        try:
            iam.delete_role_policy(RoleName=name, PolicyName=policy)
        except Exception:
            pass
    try:
        iam.delete_role(RoleName=name)
    except Exception:
        pass


def main() -> int:
    import os

    command = sys.argv[1] if len(sys.argv) > 1 else "enable"
    region = os.environ.get("REGION", "")
    if not region:
        raise SystemExit("Set REGION")
    if command == "enable":
        enable(region)
        return 0
    if command == "teardown":
        teardown(region)
        return 0
    if command == "validate":
        return validate(region)
    raise SystemExit(f"unknown command {command}")


if __name__ == "__main__":
    raise SystemExit(main())
