"""Agent tool boundary.

The local policy mirror permits three read tools. It is not the AgentCore Cedar
evaluator. Cedar source for the gateway lives under cloud/agentcore and was not
deployed. There is no approve, merge, or deploy tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from apireview.errors import AuthorizationFailed

ALLOWED_TOOLS = ("read_artifacts", "request_spec_check", "read_test_results")
DENIED_TOOLS = ("approve", "merge", "deploy", "send_task_success", "start_execution", "put_object", "delete_item")


@dataclass(frozen=True)
class TrustedContext:
    """Authenticated job binding supplied by the workflow, not by the model."""

    job_id: str
    owner: str
    evidence_hash: str | None


class PolicyDenied(AuthorizationFailed):
    pass


def local_policy_allows(tool_name: str) -> bool:
    return tool_name in ALLOWED_TOOLS


class ToolGateway:
    def __init__(self, readers: dict[str, Callable[[TrustedContext], dict[str, Any]]]):
        unknown = set(readers) - set(ALLOWED_TOOLS)
        if unknown:
            raise AuthorizationFailed(f"tool registry contains tools outside the allowlist: {sorted(unknown)}")
        self.readers = readers

    def call(self, tool_name: str, arguments: dict[str, Any] | None, context: TrustedContext) -> dict[str, Any]:
        arguments = arguments or {}
        if tool_name in DENIED_TOOLS or tool_name not in ALLOWED_TOOLS:
            raise PolicyDenied(f"policy deny {tool_name}")
        if not local_policy_allows(tool_name):
            raise PolicyDenied(f"policy deny {tool_name}")
        supplied_job = arguments.get("jobId")
        if supplied_job != context.job_id:
            raise PolicyDenied("model jobId does not match the authenticated job")
        supplied_hash = arguments.get("specHash")
        if supplied_hash is not None and supplied_hash != context.evidence_hash:
            raise PolicyDenied("model specHash does not match sealed evidence")
        if "principal" in arguments or "verdict" in arguments or "decision" in arguments:
            raise PolicyDenied("model arguments cannot carry authority")
        reader = self.readers.get(tool_name)
        if reader is None:
            raise PolicyDenied(f"tool {tool_name} is not registered")
        payload = reader(context)
        payload = dict(payload)
        payload["authoritative"] = False
        payload["jobId"] = context.job_id
        return payload


def run_bounded_agent(gateway: ToolGateway, context: TrustedContext, model_tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply an explicit list of tool calls. There is no model loop that invents tools."""
    results = []
    denied = []
    for call in model_tool_calls:
        name = call.get("name")
        try:
            results.append({"name": name, "ok": True, "output": gateway.call(name, call.get("arguments"), context)})
        except PolicyDenied as exc:
            denied.append({"name": name, "ok": False, "error": str(exc)})
    return {
        "authoritative": False,
        "tools": list(ALLOWED_TOOLS),
        "results": results,
        "denied": denied,
    }
