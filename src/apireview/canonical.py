"""Canonical JSON and content hashes.

Floats are rejected. Decimal values serialize with the shortest exact base-10
form so money values such as 19.95 do not pass through binary floating point.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from apireview.errors import ValidationFailed


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def parse_json_bytes(data: bytes, *, max_bytes: int, label: str) -> Any:
    if not isinstance(data, (bytes, bytearray)):
        raise ValidationFailed(f"{label} must be bytes")
    if len(data) > max_bytes:
        raise ValidationFailed(f"{label} exceeds {max_bytes} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailed(f"{label} is not UTF-8") from exc

    def pairs(seq: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in seq:
            if key in out:
                raise ValidationFailed(f"{label} contains duplicate key {key}")
            out[key] = value
        return out

    try:
        value = json.loads(text, parse_float=Decimal, parse_int=Decimal, object_pairs_hook=pairs)
    except ValidationFailed:
        raise
    except json.JSONDecodeError as exc:
        raise ValidationFailed(f"{label} is not JSON: {exc.msg}") from exc
    _reject_nonfinite(value, label)
    return value


def _reject_nonfinite(value: Any, label: str) -> None:
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValidationFailed(f"{label} contains a non-finite number")
    if isinstance(value, dict):
        for item in value.values():
            _reject_nonfinite(item, label)
    elif isinstance(value, list):
        for item in value:
            _reject_nonfinite(item, label)


def decimal_token(value: Decimal) -> str:
    if not value.is_finite():
        raise ValidationFailed("non-finite number")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text


def canonical_dumps(value: Any) -> str:
    return _dump(value)


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def _dump(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return decimal_token(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_dump(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise ValidationFailed("JSON objects must use string keys")
            parts.append(json.dumps(key, ensure_ascii=False) + ":" + _dump(value[key]))
        return "{" + ",".join(parts) + "}"
    if isinstance(value, float):
        raise ValidationFailed("binary float is not allowed; use Decimal")
    raise ValidationFailed(f"unsupported value type {type(value).__name__}")


def json_ready(value: Any) -> Any:
    """Convert Decimal trees to int or float-free strings for json.dumps.

    Whole numbers become int. Fractional numbers become strings only when the
    caller asks for a display form. API responses use this helper's sibling
    `api_ready`, which emits JSON numbers via canonical reparse.
    """
    if isinstance(value, Decimal):
        token = decimal_token(value)
        if "." not in token and "e" not in token.lower():
            return int(token)
        return token
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    return value


def api_dumps(value: Any) -> str:
    """Serialize a domain tree to JSON text with exact decimal tokens."""
    return canonical_dumps(value)


def walk_depth(value: Any, limit: int, *, label: str) -> None:
    def visit(node: Any, depth: int) -> None:
        if depth > limit:
            raise ValidationFailed(f"{label} exceeds nesting depth {limit}")
        if isinstance(node, dict):
            for child in node.values():
                visit(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                visit(child, depth + 1)

    visit(value, 0)


def count_nodes(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + sum(count_nodes(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(count_nodes(item) for item in value)
    return 1
