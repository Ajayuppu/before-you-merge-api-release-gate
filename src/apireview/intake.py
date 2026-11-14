"""Shared submission checks used by the local engine and the Lambda handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from apireview.canonical import canonical_bytes, canonical_dumps, parse_json_bytes, sha256_bytes, sha256_text, walk_depth
from apireview.errors import ValidationFailed
from apireview.fixtures import validate_fixture
from apireview.limits import MAX_FIXTURE_BYTES, MAX_NESTING_DEPTH, MAX_SPEC_BYTES, MOCK_VERSIONS
from apireview.openapi_subset import assert_node_budget, parse_spec


@dataclass(frozen=True)
class Intake:
    old_body: Any
    old_hash: str
    new_body: Any
    new_hash: str
    fixture_body: Any
    fixture_hash: str
    payload_hash: str
    mock_version: str


def intake_documents(old_spec: bytes, new_spec: bytes, fixture: bytes, mock_version: str) -> Intake:
    if mock_version not in MOCK_VERSIONS:
        raise ValidationFailed("mockVersion is not on the allowlist")
    old_body, old_hash = _ingest(old_spec, "old spec", MAX_SPEC_BYTES)
    new_body, new_hash = _ingest(new_spec, "new spec", MAX_SPEC_BYTES)
    fixture_body, fixture_hash = _ingest(fixture, "fixture", MAX_FIXTURE_BYTES)
    parse_spec(old_body, label="old spec")
    parse_spec(new_body, label="new spec")
    validate_fixture(fixture_body)
    payload_hash = sha256_text(
        canonical_dumps(
            {
                "fixture": fixture_hash,
                "mockVersion": mock_version,
                "new": new_hash,
                "old": old_hash,
            }
        )
    )
    return Intake(
        old_body=old_body,
        old_hash=old_hash,
        new_body=new_body,
        new_hash=new_hash,
        fixture_body=fixture_body,
        fixture_hash=fixture_hash,
        payload_hash=payload_hash,
        mock_version=mock_version,
    )


def _ingest(raw: bytes, label: str, max_bytes: int) -> tuple[Any, str]:
    document = parse_json_bytes(raw, max_bytes=max_bytes, label=label)
    walk_depth(document, MAX_NESTING_DEPTH, label=label)
    assert_node_budget(document, label=label)
    return document, sha256_bytes(canonical_bytes(document))
