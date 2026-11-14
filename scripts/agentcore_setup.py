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
READ_TOOL = "ReviewTools___read_artifacts"
APPROVE_TOOL = "ReviewTools___approve"
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
    broad = f"arn:aws:bedrock-agentcore:{region}:{account}:*"
    interpreter_trust = code_interpreter_source_arn(account, region)
    execution = os.environ.get("EXECUTION_ROLE_ARN") or _ensure_role(
        iam, INTERPRETER_ROLE, trust_policy(account, region, interpreter_trust), None, "code interpreter role"
    )
    gateway_role = os.environ.get("GATEWAY_ROLE_ARN") or _ensure_role(
        iam, GATEWAY_ROLE, trust_policy(account, region, broad), gateway_permissions(account, region, api_id), "gateway role"
    )
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    interpreter_id = _ensure_interpreter(control, execution)
    engine = _ensure_engine(control)
    _wait_ready(lambda: control.get_policy_engine(policyEngineId=engine["id"]), "policy engine")
    gateway = _ensure_gateway(control, gateway_role, engine["arn"])
    _wait_ready(lambda: control.get_gateway(gatewayIdentifier=gateway["id"]), "gateway")
    if not os.environ.get("GATEWAY_ROLE_ARN"):
        _ensure_role(iam, GATEWAY_ROLE, trust_policy(account, region, gateway["arn"]), gateway_permissions(account, region, api_id), "gateway role trust")
    _ensure_target(control, gateway["id"], api_id)
    _ensure_policies(control, engine["id"], gateway["arn"])
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
    failures = 0
    failures += _validate_gateway(region, state)
    failures += _validate_interpreter(region, state)
    if failures:
        print(f"FAIL agentcore-validation {failures}")
        return 1
    print("PASS agentcore-validation")
    return 0


def _validate_gateway(region: str, state: dict[str, str]) -> int:
    job_id = _job_id()
    version = _gateway_protocol(region, state)
    session = _McpSession(region, state.get("gatewayUrl", ""), version)
    try:
        session.open()
    except Exception as exc:
        detail = str(exc)[:300]
        print(f"FAIL gateway-read: {detail}")
        print(f"FAIL policy-deny: {detail}")
        return 2
    list_status, list_body = session.request("tools/list", {})
    print(f"tools/list {list_body}")
    read_name, approve_name = select_gateway_tools(tool_names_from_list(list_body))
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
    return int(not allow_ok) + int(not deny_ok)


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
    try:
        parsed = json.loads(text[text.rfind("{") :])
    except json.JSONDecodeError:
        print(f"FAIL code-interpreter diff: unreadable output {text[:200]}")
        return 1
    rules = {item.get("rule") for item in parsed.get("findings") or []}
    ok = parsed.get("ok") is False and {"checker.amount.type", "checker.next_cursor.required"} <= rules
    print("PASS code-interpreter diff" if ok else f"FAIL code-interpreter diff: {text[:200]}")
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


class _McpSession:
    def __init__(self, region: str, gateway_url: str, protocol: str) -> None:
        self.region = region
        self.gateway_url = gateway_url
        self.protocol = protocol
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
        credentials = Session().get_credentials()
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
        result = event.get("result") or {}
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(str(item.get("text") or ""))
    return "".join(chunks)


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
    found = _named_record(control.list_gateways, ("gateways", "gatewaySummaries"), GATEWAY_NAME, "gatewayId", "gatewayArn", "gatewayUrl")
    if found:
        print(f"Gateway {GATEWAY_NAME} already exists.")
        return found
    try:
        response = retry_iam(
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
        found = _named_record(control.list_gateways, ("gateways", "gatewaySummaries"), GATEWAY_NAME, "gatewayId", "gatewayArn", "gatewayUrl")
        if not found:
            raise
        print(f"Gateway {GATEWAY_NAME} already exists.")
        return found
    return {"id": response["gatewayId"], "arn": response["gatewayArn"], "url": response.get("gatewayUrl", "")}


def _ensure_target(control, gateway_id: str, api_id: str) -> None:
    listed = control.list_gateway_targets(gatewayIdentifier=gateway_id)
    existing = listed.get("items") or listed.get("targets") or []
    for item in existing:
        if item.get("name") == TARGET_NAME:
            print(f"Gateway target {TARGET_NAME} already exists.")
            return
    try:
        control.create_gateway_target(
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
    except Exception as exc:
        if not _is_conflict(exc):
            raise
        print(f"Gateway target {TARGET_NAME} already exists.")


def _ensure_policies(control, engine_id: str, gateway_arn: str) -> None:
    files = [
        ("AllowReadArtifacts", ROOT / "cloud/agentcore/allow-read-artifacts.cedar"),
        ("AllowRequestSpecCheck", ROOT / "cloud/agentcore/allow-request-spec-check.cedar"),
        ("AllowReadTestResults", ROOT / "cloud/agentcore/allow-read-test-results.cedar"),
    ]
    existing = {item.get("name") for item in _items(control.list_policies(policyEngineId=engine_id), "policies", "policySummaries")}
    for name, path in files:
        if name in existing:
            print(f"Policy {name} already exists.")
            continue
        statement = path.read_text(encoding="utf-8").replace("__GATEWAY_ARN__", gateway_arn)
        try:
            control.create_policy(
                policyEngineId=engine_id,
                name=name,
                validationMode="FAIL_ON_ANY_FINDINGS",
                enforcementMode="ACTIVE",
                definition={"cedar": {"statement": statement}},
            )
        except Exception as exc:
            if not _is_conflict(exc):
                raise


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
        if "FAIL" in status:
            raise SystemExit(f"{label} entered {status}")
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


def _delete_named_gateway(control, gateway_id: str | None) -> None:
    identifiers = [gateway_id] if gateway_id else []
    try:
        listed = control.list_gateways()
    except Exception:
        listed = {}
    for item in (listed.get("gateways") or listed.get("gatewaySummaries") or []):
        if item.get("name") == GATEWAY_NAME:
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
