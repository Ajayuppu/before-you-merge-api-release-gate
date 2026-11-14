"""Declarative client fixtures. Executable fields and URLs are rejected."""

from __future__ import annotations

from typing import Any

from apireview.canonical import walk_depth
from apireview.errors import ValidationFailed
from apireview.limits import (
    ALLOWED_FIXTURE_ROUTES,
    EXECUTABLE_STRING_MARKERS,
    FORBIDDEN_KEYS,
    MAX_FIXTURE_CASES,
    MAX_NESTING_DEPTH,
    MAX_STRING_CHARS,
)
from apireview.openapi_subset import is_json_number

ALLOWED_EXPECT_PATHS = (
    "orders",
    "orders[0].id",
    "orders[0].amount",
    "next_cursor",
)


def validate_fixture(document: Any) -> dict[str, Any]:
    walk_depth(document, MAX_NESTING_DEPTH, label="fixture")
    _reject_forbidden(document, "fixture")
    if not isinstance(document, dict):
        raise ValidationFailed("fixture must be a JSON object")
    extra = set(document) - {"fixtureVersion", "name", "cases"}
    if extra:
        raise ValidationFailed(f"fixture contains unsupported fields: {sorted(extra)}")
    if document.get("fixtureVersion") != "1":
        raise ValidationFailed("fixtureVersion must be 1")
    name = document.get("name")
    if not isinstance(name, str) or not name:
        raise ValidationFailed("fixture name is required")
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValidationFailed("fixture cases must be a non-empty array")
    if len(cases) > MAX_FIXTURE_CASES:
        raise ValidationFailed("fixture has too many cases")
    parsed_cases = []
    seen: set[str] = set()
    for case in cases:
        parsed = _parse_case(case)
        if parsed["id"] in seen:
            raise ValidationFailed(f"duplicate fixture case {parsed['id']}")
        seen.add(parsed["id"])
        parsed_cases.append(parsed)
    return {"fixtureVersion": "1", "name": name, "cases": parsed_cases}


def _parse_case(case: Any) -> dict[str, Any]:
    if not isinstance(case, dict):
        raise ValidationFailed("fixture case must be an object")
    extra = set(case) - {"id", "method", "path", "query", "expect"}
    if extra:
        raise ValidationFailed(f"fixture case contains unsupported fields: {sorted(extra)}")
    case_id = case.get("id")
    if not isinstance(case_id, str) or not case_id.replace("-", "").isalnum() or case_id != case_id.lower():
        raise ValidationFailed("fixture case id must be lowercase letters, digits, and hyphens")
    method = case.get("method")
    path = case.get("path")
    if (method, path) not in ALLOWED_FIXTURE_ROUTES:
        raise ValidationFailed("fixture route is not on the allowlist")
    query = case.get("query") or {}
    if not isinstance(query, dict):
        raise ValidationFailed("fixture query must be an object")
    if set(query) - {"limit"}:
        raise ValidationFailed("fixture query only allows limit")
    if "limit" in query and (isinstance(query["limit"], bool) or not is_json_number(query["limit"])):
        raise ValidationFailed("fixture limit must be a JSON number and not a boolean")
    expect = case.get("expect")
    if not isinstance(expect, dict):
        raise ValidationFailed("fixture expect must be an object")
    expect_extra = set(expect) - {"status", "body", "requiredPaths"}
    if expect_extra:
        raise ValidationFailed(f"fixture expect contains unsupported fields: {sorted(expect_extra)}")
    if expect.get("status") != 200:
        raise ValidationFailed("this harness allowlist only expects HTTP 200")
    body = expect.get("body")
    if not isinstance(body, dict):
        raise ValidationFailed("fixture expect.body must be an object")
    required = expect.get("requiredPaths")
    if required != list(ALLOWED_EXPECT_PATHS):
        raise ValidationFailed("fixture requiredPaths must match the fixed allowlist exactly")
    _validate_expected_body(body)
    return {
        "id": case_id,
        "method": method,
        "path": path,
        "query": dict(query),
        "expect": {"status": 200, "body": body, "requiredPaths": list(ALLOWED_EXPECT_PATHS)},
    }


def _validate_expected_body(body: dict[str, Any]) -> None:
    if set(body) - {"orders", "next_cursor"}:
        raise ValidationFailed("fixture body only allows orders and next_cursor")
    if "next_cursor" not in body:
        raise ValidationFailed("fixture body must include next_cursor")
    orders = body.get("orders")
    if not isinstance(orders, list) or len(orders) != 1 or not isinstance(orders[0], dict):
        raise ValidationFailed("fixture orders must be a one-item array")
    order = orders[0]
    if set(order) - {"id", "amount"}:
        raise ValidationFailed("fixture order only allows id and amount")
    if not isinstance(order.get("id"), str) or not order["id"]:
        raise ValidationFailed("fixture order id must be a string")
    if not is_json_number(order.get("amount")):
        raise ValidationFailed("fixture order amount must be a JSON number, not a string or boolean")
    cursor = body.get("next_cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise ValidationFailed("fixture next_cursor must be a string or null")


def _reject_forbidden(value: Any, label: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in FORBIDDEN_KEYS:
                raise ValidationFailed(f"{label} contains forbidden field {key}")
            _reject_forbidden(child, label)
    elif isinstance(value, list):
        for child in value:
            _reject_forbidden(child, label)
    elif isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            raise ValidationFailed(f"{label} contains an oversized string")
        lowered = value.lower()
        for marker in EXECUTABLE_STRING_MARKERS:
            if marker in lowered:
                raise ValidationFailed(f"{label} contains a forbidden URL-like value")
