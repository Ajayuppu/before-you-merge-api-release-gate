"""Advisory explanations.

The deterministic explainer always runs and is still not a gate. Bedrock Converse
is optional, parsed strictly, and cannot set a verdict. This module does not
call Bedrock unless a caller passes a client.
"""

from __future__ import annotations

import json
from typing import Any

from apireview.canonical import canonical_dumps, sha256_text
from apireview.evidence import advisory_content_hash

MODEL_ID_US = "us.amazon.nova-2-lite-v1:0"
MODEL_ID_IN_REGION = "amazon.nova-2-lite-v1:0"
MODEL_ID_GLOBAL = "global.amazon.nova-2-lite-v1:0"

_SYSTEM = (
    "You explain OpenAPI breaking changes for a human reviewer. "
    "Respond with one JSON object and no markdown. "
    "Keys allowed: explanations, proposals. "
    "Do not include verdict, approve, decision, principal, jobId, or code. "
    "proposals are declarative descriptions, never executable source."
)


def deterministic_advisory(findings: list[dict[str, str]]) -> dict[str, Any]:
    explanations = []
    proposals = []
    for finding in findings:
        if finding["severity"] != "breaking":
            continue
        proposal = _proposal_for(finding)
        explanations.append(
            {
                "findingId": finding["id"],
                "rule": finding["rule"],
                "explanation": finding["message"],
            }
        )
        if proposal is not None:
            proposals.append(proposal)
    document = {
        "source": "deterministic-local",
        "authoritative": False,
        "includedInVerdict": False,
        "explanations": explanations,
        "proposals": proposals,
        "modelId": None,
        "modelStatus": "not-called",
    }
    document["deterministicHash"] = advisory_content_hash(document)
    document["modelAdvisoryHash"] = None
    return document


def _proposal_for(finding: dict[str, str]) -> dict[str, Any] | None:
    rule = finding["rule"]
    if rule == "response.type.changed" and finding["path"].endswith("/amount/type"):
        return {
            "kind": "spec",
            "rule": rule,
            "description": "Restore Order.amount schema type to number.",
            "declarative": {"path": finding["path"], "restoreType": "number"},
        }
    if rule == "response.property.removed.required" and finding["path"].endswith("/next_cursor"):
        return {
            "kind": "spec",
            "rule": rule,
            "description": "Restore required response property next_cursor.",
            "declarative": {"path": finding["path"], "restoreRequiredProperty": "next_cursor"},
        }
    if rule.startswith("request.") and "required" in rule:
        return {
            "kind": "spec",
            "rule": rule,
            "description": "Remove the new required request constraint so old callers stay valid.",
            "declarative": {"path": finding["path"], "keepOptionalForCallers": True},
        }
    return {
        "kind": "spec",
        "rule": rule,
        "description": "Restore the previous contract for this breaking rule.",
        "declarative": {"path": finding["path"], "rule": rule},
    }


def build_converse_request(model_id: str, findings: list[dict[str, str]]) -> dict[str, Any]:
    """Bedrock Converse request. Reasoning stays off so maxTokens remains valid.

    Nova 2 rejects maxTokens when maxReasoningEffort is high. This request does
    not set reasoningConfig.
    """
    prompt = canonical_dumps(
        {
            "task": "explain-breaking-changes",
            "findings": [item for item in findings if item["severity"] == "breaking"],
        }
    )
    return {
        "modelId": model_id,
        "system": [{"text": _SYSTEM}],
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": 1024, "temperature": 0.0},
    }


def invoke_converse(client: Any, model_id: str, findings: list[dict[str, str]]) -> dict[str, Any]:
    """Call Converse. A transport or service error becomes a recorded non-verdict."""
    try:
        request = build_converse_request(model_id, findings)
        return parse_converse_response(client.converse(**request))
    except Exception as exc:
        return model_call_error(exc)


def model_call_error(exc: BaseException) -> dict[str, Any]:
    return {
        "source": "bedrock-converse",
        "authoritative": False,
        "includedInVerdict": False,
        "explanations": [],
        "proposals": [],
        "modelStatus": "error",
        "modelAdvisoryHash": None,
        "rejection": f"{type(exc).__name__}: {exc}",
    }


def parse_converse_response(response: dict[str, Any]) -> dict[str, Any]:
    """Accept only a JSON object whose keys are on the allowlist."""
    try:
        blocks = response["output"]["message"]["content"]
        texts = [block["text"] for block in blocks if isinstance(block, dict) and "text" in block]
        text = "".join(texts).strip()
        parsed = json.loads(text)
    except (KeyError, TypeError, json.JSONDecodeError):
        return _invalid("model output was not a JSON object")
    if not isinstance(parsed, dict):
        return _invalid("model output was not an object")
    allowed = {"explanations", "proposals"}
    if set(parsed) - allowed:
        return _invalid("model output contained keys outside the allowlist")
    explanations = parsed.get("explanations", [])
    proposals = parsed.get("proposals", [])
    if not isinstance(explanations, list) or not isinstance(proposals, list):
        return _invalid("model explanations or proposals were not arrays")
    if not _safe_tree(explanations) or not _safe_tree(proposals):
        return _invalid("model output contained forbidden content")
    document = {
        "source": "bedrock-converse",
        "authoritative": False,
        "includedInVerdict": False,
        "explanations": explanations,
        "proposals": proposals,
        "modelStatus": "parsed",
    }
    document["modelAdvisoryHash"] = sha256_text(canonical_dumps(document))
    return document


def _invalid(reason: str) -> dict[str, Any]:
    return {
        "source": "bedrock-converse",
        "authoritative": False,
        "includedInVerdict": False,
        "explanations": [],
        "proposals": [],
        "modelStatus": "rejected",
        "modelAdvisoryHash": None,
        "rejection": reason,
    }


def _safe_tree(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                return False
            lowered = key.lower()
            if lowered in {"code", "script", "command", "verdict", "approve", "decision", "principal", "jobid"}:
                return False
            if not _safe_tree(child):
                return False
        return True
    if isinstance(value, list):
        return all(_safe_tree(item) for item in value)
    if isinstance(value, str):
        lowered = value.lower()
        return "http://" not in lowered and "https://" not in lowered and "s3://" not in lowered
    return isinstance(value, (int, float, bool)) or value is None


def merge_advisory(deterministic: dict[str, Any], model: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(deterministic)
    merged["authoritative"] = False
    merged["includedInVerdict"] = False
    if model is None:
        merged["modelStatus"] = "not-called"
        merged["modelAdvisoryHash"] = None
        return merged
    merged["modelStatus"] = model.get("modelStatus")
    merged["modelAdvisoryHash"] = model.get("modelAdvisoryHash")
    merged["modelRejection"] = model.get("rejection")
    if model.get("modelStatus") == "parsed":
        merged["modelExplanations"] = model.get("explanations", [])
        merged["modelProposals"] = model.get("proposals", [])
    return merged
