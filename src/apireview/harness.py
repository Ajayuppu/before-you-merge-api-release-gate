"""Trusted compatibility harness.

The assertions in this module are the only client checks that can fail a gate.
Uploaded fixture JSON supplies expected values for an allowlisted shape. It is
never executed. The cloud transport, when used, calls only the configured mock
host and an allowlisted path for the selected version enum.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from apireview.canonical import canonical_bytes, sha256_bytes
from apireview.errors import ValidationFailed
from apireview.limits import (
    CLOUD_MOCK_ROUTES,
    HARNESS_TIMEOUT_SECONDS,
    MAX_RESPONSE_BYTES,
    MOCK_VERSIONS,
)
from apireview.mocks import load_mock
from apireview.openapi_subset import is_json_number

HARNESS_VERSION = "1.0.0"


def harness_source_hash() -> str:
    return sha256_bytes(Path(__file__).read_bytes())


@dataclass(frozen=True)
class MockTarget:
    host: str
    stage: str
    method: str
    path: str
    url: str
    timeout_seconds: int
    max_bytes: int
    allow_redirects: bool


@dataclass(frozen=True)
class HarnessPlan:
    mock_version: str
    target: MockTarget


def build_plan(mock_version: str, *, host: str, stage: str) -> HarnessPlan:
    if mock_version not in MOCK_VERSIONS:
        raise ValidationFailed("mockVersion is not on the allowlist")
    if not host or "://" in host or "/" in host or " " in host:
        raise ValidationFailed("mock host must be a configured hostname, not a URL")
    if not stage or "/" in stage or stage.startswith("."):
        raise ValidationFailed("mock stage is invalid")
    path = CLOUD_MOCK_ROUTES[mock_version]
    url = f"https://{host}/{stage}{path}"
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != host or parts.query or parts.fragment:
        raise ValidationFailed("mock URL failed the allowlist check")
    if parts.path != f"/{stage}{path}":
        raise ValidationFailed("mock URL path is not allowlisted")
    return HarnessPlan(
        mock_version=mock_version,
        target=MockTarget(
            host=host,
            stage=stage,
            method="GET",
            path=path,
            url=url,
            timeout_seconds=HARNESS_TIMEOUT_SECONDS,
            max_bytes=MAX_RESPONSE_BYTES,
            allow_redirects=False,
        ),
    )


class InProcessMockTransport:
    """Loopback stand-in. It never opens a socket and never reads a caller URL."""

    def exchange(self, plan: HarnessPlan) -> dict[str, Any]:
        catalog = load_mock(plan.mock_version)
        body = catalog["response"]["body"]
        raw = canonical_bytes(body)
        return {
            "status": catalog["response"]["status"],
            "body": body,
            "raw": raw,
            "truncated": False,
            "redirects": 0,
            "transport": "in-process",
        }


def run_harness(fixture: dict[str, Any], plan: HarnessPlan, transport: InProcessMockTransport | Any) -> dict[str, Any]:
    observed = transport.exchange(plan)
    if plan.target.allow_redirects:
        raise ValidationFailed("redirects are disabled")
    raw = observed.get("raw") or b""
    truncated = bool(observed.get("truncated")) or len(raw) > plan.target.max_bytes
    cases = []
    for case in fixture["cases"]:
        cases.append(_run_case(case, observed, truncated=truncated))
    complete = (not truncated) and all(case["complete"] for case in cases) and bool(cases)
    passed = complete and all(case["passed"] for case in cases)
    return {
        "harnessVersion": HARNESS_VERSION,
        "harnessSourceHash": harness_source_hash(),
        "mockVersion": plan.mock_version,
        "transport": observed.get("transport", "unknown"),
        "target": {
            "method": plan.target.method,
            "path": plan.target.path,
            "hostConfigured": True,
            "allowRedirects": plan.target.allow_redirects,
            "timeoutSeconds": plan.target.timeout_seconds,
            "maxBytes": plan.target.max_bytes,
        },
        "complete": complete,
        "passed": passed,
        "truncated": truncated,
        "cases": cases,
    }


def _run_case(case: dict[str, Any], observed: dict[str, Any], *, truncated: bool) -> dict[str, Any]:
    expected = case["expect"]["body"]
    assertions: list[dict[str, Any]] = []
    if truncated:
        assertions.append(_assertion("response.complete", False, "response was truncated or incomplete"))
        return {"id": case["id"], "passed": False, "complete": False, "assertions": assertions}

    status_ok = observed.get("status") == expected_status(case)
    assertions.append(
        _assertion(
            "status",
            status_ok,
            "status matches 200" if status_ok else f"status is {observed.get('status')}, expected 200",
        )
    )
    body = observed.get("body")
    body_ok = isinstance(body, dict)
    assertions.append(_assertion("body.object", body_ok, "body is a JSON object" if body_ok else "body is not an object"))
    if body_ok:
        assertions.extend(_order_assertions(expected, body))
    else:
        assertions.append(_assertion("orders", False, "orders could not be read"))
        assertions.append(_assertion("amount", False, "amount could not be read"))
        assertions.append(_assertion("next_cursor", False, "next_cursor could not be read"))
    passed = all(item["passed"] for item in assertions)
    return {"id": case["id"], "passed": passed, "complete": True, "assertions": assertions}


def expected_status(case: dict[str, Any]) -> int:
    return int(case["expect"]["status"])


def _order_assertions(expected: dict[str, Any], body: dict[str, Any]) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    orders = body.get("orders")
    expected_orders = expected["orders"]
    if not isinstance(orders, list) or not orders or not isinstance(orders[0], dict):
        assertions.append(_assertion("orders", False, "orders must be a non-empty array of objects"))
        assertions.append(_assertion("amount", False, "amount is missing"))
        assertions.append(_assertion("id", False, "id is missing"))
    else:
        assertions.append(_assertion("orders", True, "orders is a non-empty array"))
        order = orders[0]
        expected_order = expected_orders[0]
        id_ok = isinstance(order.get("id"), str) and order.get("id") == expected_order["id"]
        assertions.append(
            _assertion(
                "id",
                id_ok,
                "id matches" if id_ok else f"id is {order.get('id')!r}, expected {expected_order['id']!r}",
            )
        )
        amount = order.get("amount")
        if isinstance(amount, bool) or not is_json_number(amount):
            kind = type(amount).__name__
            assertions.append(
                _assertion(
                    "amount",
                    False,
                    f"amount is {kind} {amount!r}; old clients require a JSON number and booleans are rejected",
                )
            )
        else:
            expected_amount = expected_order["amount"]
            if not isinstance(expected_amount, Decimal):
                raise ValidationFailed("fixture amount was not parsed as Decimal")
            equal = amount == expected_amount
            assertions.append(
                _assertion(
                    "amount",
                    equal,
                    "amount matches the expected decimal"
                    if equal
                    else f"amount {amount} does not equal expected {expected_amount}",
                )
            )
    present = "next_cursor" in body
    if not present:
        assertions.append(_assertion("next_cursor", False, "next_cursor is missing; old clients require the field"))
    else:
        cursor = body.get("next_cursor")
        expected_cursor = expected.get("next_cursor")
        ok = cursor == expected_cursor and (cursor is None or isinstance(cursor, str))
        assertions.append(
            _assertion(
                "next_cursor",
                ok,
                "next_cursor is present" if ok else f"next_cursor is {cursor!r}, expected {expected_cursor!r}",
            )
        )
    return assertions


def _assertion(name: str, passed: bool, message: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "message": message}
