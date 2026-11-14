"""Fixed specification checker.

The same function runs in-process for the local reference. The source builder
embeds payload data only as a JSON string literal so the checker text stays
fixed. AgentCore execution of that source was not invoked.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from apireview.canonical import canonical_dumps, sha256_bytes
from apireview.errors import ValidationFailed

CHECKER_VERSION = "1.0.0"
_PREFIX = '''import json

def check(payload):
    findings = []
    spec = payload["candidateSpec"]
    schemas = spec.get("components", {}).get("schemas", {})
    order = schemas.get("Order", {})
    page = schemas.get("OrderPage", {})
    amount = order.get("properties", {}).get("amount", {})
    if amount.get("type") != "number":
        findings.append({
            "rule": "checker.amount.type",
            "message": "Order.amount must stay type number for old clients.",
        })
    required = page.get("required", [])
    properties = page.get("properties", {})
    if "next_cursor" not in required or "next_cursor" not in properties:
        findings.append({
            "rule": "checker.next_cursor.required",
            "message": "OrderPage.next_cursor must remain a required response property.",
        })
    raw = json.dumps(spec)
    if "http://" in raw or "https://" in raw or '"$ref": "http' in raw:
        findings.append({
            "rule": "checker.external",
            "message": "External URLs or refs are outside the supported subset.",
        })
    return {
        "checkerVersion": "1.0.0",
        "authoritative": False,
        "ok": not findings,
        "findings": findings,
    }
'''
_SUFFIX = """
RESULT = check(json.loads(DATA_JSON))
print(json.dumps(RESULT, sort_keys=True, separators=(",", ":")))
"""


def checker_source_hash() -> str:
    return sha256_bytes(Path(__file__).read_bytes())


def run_checker(candidate_spec: Any) -> dict[str, Any]:
    """In-process corroboration. This is not an air gap and not AgentCore."""
    source = build_checker_source(candidate_spec)
    result = _exec_fixed_source(source)
    result["mode"] = "in-process"
    result["networkIsolation"] = "not-claimed"
    result["authoritative"] = False
    result["checkerSourceHash"] = checker_source_hash()
    return result


def build_checker_source(candidate_spec: Any) -> str:
    encoded = canonical_dumps({"candidateSpec": candidate_spec})
    literal = json.dumps(encoded)
    source = _PREFIX + "DATA_JSON = " + literal + "\n" + _SUFFIX
    ast.parse(source)
    return source


def _exec_fixed_source(source: str) -> dict[str, Any]:
    """Execute only the fixed checker. Import is limited to the json module."""
    tree = ast.parse(source)
    data_nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "DATA_JSON"
    ]
    if len(data_nodes) != 1 or not isinstance(data_nodes[0].value, ast.Constant):
        raise ValidationFailed("checker source is not the fixed program")
    if not isinstance(data_nodes[0].value.value, str):
        raise ValidationFailed("checker data channel must be a string literal")
    captured: list[str] = []

    def _print(text: str) -> None:
        captured.append(text)

    def _import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name != "json":
            raise ImportError(name)
        return json

    safe_builtins = {
        "print": _print,
        "len": len,
        "__import__": _import,
    }
    namespace: dict[str, Any] = {"__builtins__": safe_builtins}
    exec(compile(tree, "<fixed-checker>", "exec"), namespace, namespace)  # noqa: S102
    if not captured:
        raise ValidationFailed("checker produced no result")
    parsed = json.loads(captured[-1])
    if not isinstance(parsed, dict) or parsed.get("authoritative") is not False:
        raise ValidationFailed("checker result is not a corroborative object")
    return parsed
