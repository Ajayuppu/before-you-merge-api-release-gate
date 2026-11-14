"""AWS call shapes that are unit-tested with fakes and were not invoked here.

StartExecution is idempotent for a Standard workflow only while the same name
and input are still running. A closed execution raises ExecutionAlreadyExists.
PutEvents reports per-entry failures in FailedEntryCount. Code Interpreter
SANDBOX mode is not an air gap: the service documentation says it can reach
AWS services, including Amazon S3.
"""

from __future__ import annotations

import json
from typing import Any

from apireview.checker import build_checker_source
from apireview.errors import FailClosed
from apireview.limits import CODE_INTERPRETER_SESSION_SECONDS

CODE_INTERPRETER_SESSION_TIMEOUT_SECONDS = CODE_INTERPRETER_SESSION_SECONDS


def publish_events(client: Any, *, bus_name: str, entries: list[dict[str, Any]]) -> list[int]:
    response = client.put_events(
        Entries=[
            {
                "Source": "api.review",
                "DetailType": "JobSubmitted",
                "EventBusName": bus_name,
                "Detail": json.dumps(entry, separators=(",", ":"), sort_keys=True),
            }
            for entry in entries
        ]
    )
    failed: list[int] = []
    results = response.get("Entries") or []
    if len(results) != len(entries):
        raise FailClosed("PutEvents returned a different number of results")
    for index, result in enumerate(results):
        if result.get("ErrorCode"):
            failed.append(index)
    if response.get("FailedEntryCount", 0) != len(failed):
        raise FailClosed("PutEvents FailedEntryCount does not match entry errors")
    return failed


def execution_arn(state_machine_arn: str, execution_name: str) -> str:
    parts = state_machine_arn.split(":")
    if len(parts) < 7 or parts[5] != "stateMachine":
        raise FailClosed("state machine ARN shape is not usable")
    return ":".join(["arn", parts[1], "states", parts[3], parts[4], "execution", parts[6], execution_name])


def start_or_reconcile(client: Any, *, state_machine_arn: str, name: str, input_str: str) -> dict[str, str]:
    try:
        response = client.start_execution(stateMachineArn=state_machine_arn, name=name, input=input_str)
    except Exception as exc:
        from apireview.aws_dynamo import error_code

        if error_code(exc) != "ExecutionAlreadyExists":
            raise
        described = client.describe_execution(executionArn=execution_arn(state_machine_arn, name))
        if described.get("input") != input_str:
            raise FailClosed("existing execution input does not match the stored claim") from exc
        return {"executionArn": described["executionArn"], "reconciled": "true"}
    return {"executionArn": response["executionArn"], "reconciled": "false"}


def parse_code_interpreter_stream(response: dict[str, Any]) -> dict[str, Any]:
    texts: list[str] = []
    errors: list[str] = []
    for event in response.get("stream") or []:
        result = event.get("result") or {}
        if result.get("isError"):
            errors.append(json.dumps(result, default=str))
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
    if errors:
        return {
            "authoritative": False,
            "checkerVersion": "1.0.0",
            "findings": [],
            "mode": "agentcore",
            "networkIsolation": "sandbox-is-not-an-air-gap",
            "ok": None,
            "streamError": True,
        }
    try:
        parsed = json.loads("".join(texts).strip())
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        return {
            "authoritative": False,
            "checkerVersion": "1.0.0",
            "findings": [],
            "mode": "agentcore",
            "networkIsolation": "sandbox-is-not-an-air-gap",
            "ok": None,
            "streamError": True,
        }
    parsed["authoritative"] = False
    parsed["mode"] = "agentcore"
    parsed["networkIsolation"] = "sandbox-is-not-an-air-gap"
    return parsed


def run_code_interpreter(client: Any, *, interpreter_id: str, candidate_spec: Any) -> dict[str, Any]:
    """Run the fixed checker. sessionTimeoutSeconds is the session lifetime, not the per-command time limit.

    stop_code_interpreter_session ends the session. This function does not claim
    that stop deletes retained session data.
    """
    started = client.start_code_interpreter_session(
        codeInterpreterIdentifier=interpreter_id,
        name="api-review-spec-check",
        sessionTimeoutSeconds=CODE_INTERPRETER_SESSION_TIMEOUT_SECONDS,
    )
    session_id = started["sessionId"]
    try:
        response = client.invoke_code_interpreter(
            codeInterpreterIdentifier=interpreter_id,
            sessionId=session_id,
            name="executeCode",
            arguments={"language": "python", "code": build_checker_source(candidate_spec)},
        )
        return parse_code_interpreter_stream(response)
    finally:
        client.stop_code_interpreter_session(codeInterpreterIdentifier=interpreter_id, sessionId=session_id)
