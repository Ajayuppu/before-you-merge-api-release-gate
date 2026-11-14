"""Repository-owned mock responses. Callers select a version enum, never a URL."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apireview.canonical import canonical_dumps, parse_json_bytes
from apireview.errors import ValidationFailed
from apireview.limits import MAX_SPEC_BYTES, MOCK_VERSIONS

def _mock_dir() -> Path:
    here = Path(__file__).resolve()
    candidates = (
        here.parents[2] / "fixtures" / "mocks",
        here.parents[1] / "fixtures" / "mocks",
    )
    for candidate in candidates:
        if (candidate / "orders-v1.json").is_file():
            return candidate
    raise FileNotFoundError("bundled orders mock catalog was not found")


_FIXTURE_DIR = _mock_dir()


def load_mock(version: str) -> dict[str, Any]:
    if version not in MOCK_VERSIONS:
        raise ValidationFailed("mockVersion is not on the allowlist")
    path = _FIXTURE_DIR / f"{version}.json"
    payload = parse_json_bytes(path.read_bytes(), max_bytes=MAX_SPEC_BYTES, label=f"mock {version}")
    if not isinstance(payload, dict):
        raise ValidationFailed("mock catalog entry must be an object")
    if payload.get("version") != version:
        raise ValidationFailed("mock catalog version does not match the file")
    response = payload.get("response")
    if not isinstance(response, dict):
        raise ValidationFailed("mock response is missing")
    if response.get("status") != 200:
        raise ValidationFailed("mock status must be 200 in this prototype")
    if "body" not in response:
        raise ValidationFailed("mock body is missing")
    return {"version": version, "method": "GET", "path": "/orders", "response": response}


def mock_body_text(version: str) -> str:
    """Canonical JSON used by the API Gateway mock integration templates."""
    body = load_mock(version)["response"]["body"]
    return canonical_dumps(body)
