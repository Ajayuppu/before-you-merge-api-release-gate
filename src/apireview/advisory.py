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
    "Return only one JSON object. No prose and no markdown fences. "
    "The object has two arrays, explanations and proposals. "
    "Each explanation is an object with a string field explanation. "
    "Each proposal is an object with a string field description. "
    "Do not include verdict, approve, decision, principal, jobId, or code. "
    'Example: {"explanations":[{"explanation":"The response type changed."}],'
    '"proposals":[{"description":"Restore the previous type."}]}'
)

# Converse structured output. Models that reject this field are retried without it.
# https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html
_ADVISORY_SCHEMA = json.dumps(
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["explanations", "proposals"],
        "properties": {
            "explanations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["explanation"],
                    "properties": {"explanation": {"type": "string"}},
                },
            },
            "proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["description"],
                    "properties": {"description": {"type": "string"}},
                },
            },
        },
    }
)

# Unknown keys are dropped, not rejected. Only these aliases are renamed onto the two allowed fields.
_KEY_ALIASES = {
    "explanation": "explanations",
    "summary": "explanations",
    "summaries": "explanations",
    "proposal": "proposals",
    "suggestedfix": "proposals",
    "suggestedfixes": "proposals",
    "fix": "proposals",
    "fixes": "proposals",
}


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
        "outputConfig": {
            "textFormat": {
                "type": "json_schema",
                "structure": {
                    "jsonSchema": {
                        "name": "advisory",
                        "description": "Explanations and proposals. Not a verdict.",
                        "schema": _ADVISORY_SCHEMA,
                    }
                },
            }
        },
    }


def invoke_converse(client: Any, model_id: str, findings: list[dict[str, str]]) -> dict[str, Any]:
    """Call Converse. A transport or service error becomes a recorded non-verdict."""
    try:
        request = build_converse_request(model_id, findings)
        response = _send_converse(client, request)
        return parse_converse_response(response)
    except Exception as exc:
        return model_call_error(exc)


def _send_converse(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Retry once without structured output, then once with the system text folded in."""
    current = dict(request)
    dropped_output = False
    folded = False
    while True:
        try:
            return client.converse(**current)
        except Exception as exc:
            if _error_code(exc) != "ValidationException":
                raise
            if "outputConfig" in current and not dropped_output:
                current = {key: value for key, value in current.items() if key != "outputConfig"}
                dropped_output = True
                continue
            if "system" in current and not folded:
                current = _fold_system(current)
                folded = True
                continue
            raise


def _fold_system(request: dict[str, Any]) -> dict[str, Any]:
    folded = json.loads(json.dumps(request))
    system = folded.pop("system", None) or []
    instruction = "\n".join(str(block.get("text") or "") for block in system if isinstance(block, dict)).strip()
    message = folded["messages"][0]["content"][0]
    if instruction:
        message["text"] = instruction + "\n" + str(message.get("text") or "")
    return folded


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str((response.get("Error") or {}).get("Code") or "")
    return type(exc).__name__


def model_call_error(exc: BaseException) -> dict[str, Any]:
    reason = f"{type(exc).__name__}: {exc}"
    return {
        "source": "bedrock-converse",
        "authoritative": False,
        "includedInVerdict": False,
        "explanations": [],
        "proposals": [],
        "modelStatus": "error",
        "modelAdvisoryHash": None,
        "rejection": reason,
        "error": reason,
    }


def parse_converse_response(response: dict[str, Any]) -> dict[str, Any]:
    """Accept a JSON object of explanations and proposals.

    Markdown fences are stripped. If the reply is not pure JSON, the first
    balanced top-level object is used. A single object or string is wrapped
    into a list. Aliases in `_KEY_ALIASES` are renamed; every other key is
    dropped and is not a rejection. Forbidden content inside the two arrays
    is still rejected. The result is never a verdict.
    """
    try:
        blocks = response["output"]["message"]["content"]
        texts = [block["text"] for block in blocks if isinstance(block, dict) and "text" in block]
        text = _strip_fences("".join(texts).strip())
        parsed = _load_first_object(text)
    except (KeyError, TypeError, json.JSONDecodeError, ValueError):
        return _invalid("model output was not a JSON object")
    if not isinstance(parsed, dict):
        return _invalid("model output was not an object")
    normalized = _normalize_keys(parsed)
    explanations = _as_list(normalized.get("explanations", []))
    proposals = _as_list(normalized.get("proposals", []))
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
        "rejection": None,
        "error": None,
    }
    document["modelAdvisoryHash"] = sha256_text(canonical_dumps(document))
    return document


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _load_first_object(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        fragment = _balanced_object(text, start)
        return json.loads(fragment)


def _balanced_object(text: str, start: int) -> str:
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
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
                return text[start : index + 1]
    raise ValueError("unbalanced JSON object")


def _normalize_keys(parsed: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in parsed.items():
        if not isinstance(key, str):
            continue
        lowered = key.lower()
        if lowered in {"explanations", "proposals"}:
            target = lowered
        else:
            target = _KEY_ALIASES.get(lowered)
        if target is None:
            continue
        incoming = _as_list(value)
        if target in normalized and isinstance(normalized[target], list) and isinstance(incoming, list):
            normalized[target] = normalized[target] + incoming
        else:
            normalized[target] = incoming
    return normalized


def _as_list(value: Any) -> Any:
    if isinstance(value, list):
        return value
    if isinstance(value, (dict, str)):
        return [value]
    return value


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
        "error": reason,
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


def merge_advisory(deterministic: dict[str, Any], model: dict[str, Any] | None, model_id: str | None = None) -> dict[str, Any]:
    """Keep the deterministic explanations used by the seal hash, and attach the model text beside them."""
    merged = dict(deterministic)
    merged["authoritative"] = False
    merged["includedInVerdict"] = False
    merged["modelId"] = model_id
    if model is None:
        merged["modelStatus"] = "not-called"
        merged["modelAdvisoryHash"] = None
        merged["modelRejection"] = None
        merged["error"] = None
        return merged
    merged["modelStatus"] = model.get("modelStatus")
    merged["modelAdvisoryHash"] = model.get("modelAdvisoryHash")
    reason = model.get("error")
    if reason is None:
        reason = model.get("rejection")
    merged["modelRejection"] = reason
    merged["error"] = reason
    if model.get("modelStatus") == "parsed":
        merged["modelSource"] = "bedrock-converse"
        merged["modelExplanations"] = model.get("explanations", [])
        merged["modelProposals"] = model.get("proposals", [])
    return merged


def report_advisory(advisory: dict[str, Any] | None, *, enabled: bool, model_id: str | None = None) -> dict[str, Any]:
    """Shape the report. Model text is never a verdict."""
    advisory = advisory or {}
    status = advisory.get("modelStatus")
    if not enabled or status == "disabled":
        return {
            "authoritative": False,
            "error": None,
            "explanations": advisory.get("explanations", []),
            "includedInVerdict": False,
            "modelAdvisoryHash": None,
            "modelId": None,
            "modelStatus": "disabled",
            "proposals": advisory.get("proposals", []),
            "source": "deterministic-local",
        }
    if status == "parsed":
        return {
            "authoritative": False,
            "error": None,
            "explanations": advisory.get("modelExplanations", []),
            "includedInVerdict": False,
            "modelAdvisoryHash": advisory.get("modelAdvisoryHash"),
            "modelId": advisory.get("modelId") or model_id,
            "modelStatus": "parsed",
            "proposals": advisory.get("modelProposals", []),
            "source": advisory.get("modelSource") or "bedrock-converse",
        }
    reason = advisory.get("error")
    if reason is None:
        reason = advisory.get("modelRejection")
    return {
        "authoritative": False,
        "error": reason,
        "explanations": advisory.get("explanations", []),
        "includedInVerdict": False,
        "modelAdvisoryHash": advisory.get("modelAdvisoryHash"),
        "modelId": advisory.get("modelId") or model_id,
        "modelStatus": status or "not-called",
        "proposals": advisory.get("proposals", []),
        "source": "deterministic-local",
    }
