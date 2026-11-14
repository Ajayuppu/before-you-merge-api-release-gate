"""OpenAPI 3.0 subset parser.

Supported documents contain only local component refs. External refs, composition
keywords, servers, callbacks, and links are rejected. This is not a general
OpenAPI compatibility product.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from apireview.errors import ValidationFailed
from apireview.limits import EXECUTABLE_STRING_MARKERS, MAX_NODES, MAX_PROPERTIES, MAX_STRING_CHARS

SUPPORTED_METHODS = ("get", "post", "put", "patch", "delete")
SCHEMA_TYPES = {"object", "array", "string", "number", "integer", "boolean"}
SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "items",
    "additionalProperties",
    "nullable",
    "enum",
    "format",
    "description",
    "$ref",
}


@dataclass
class Schema:
    type_name: str | None
    properties: dict[str, Schema] = field(default_factory=dict)
    required: frozenset[str] = frozenset()
    items: Schema | None = None
    nullable: bool = False
    additional_properties: bool | None = None
    enum: tuple[Any, ...] | None = None
    format: str | None = None
    ref_name: str | None = None


@dataclass
class Parameter:
    name: str
    location: str
    required: bool
    schema: Schema


@dataclass
class Operation:
    method: str
    path: str
    parameters: tuple[Parameter, ...]
    request_body: Schema | None
    responses: dict[str, Schema | None]


@dataclass
class SpecModel:
    title: str
    version: str
    operations: dict[tuple[str, str], Operation]


def parse_spec(document: Any, *, label: str) -> SpecModel:
    if not isinstance(document, dict):
        raise ValidationFailed(f"{label} must be a JSON object")
    extra = set(document) - {"openapi", "info", "paths", "components"}
    if extra:
        raise ValidationFailed(f"{label} contains unsupported top-level fields: {sorted(extra)}")
    version = document.get("openapi")
    if not isinstance(version, str) or not version.startswith("3.0."):
        raise ValidationFailed(f"{label} must declare openapi 3.0.x")
    info = document.get("info")
    if not isinstance(info, dict):
        raise ValidationFailed(f"{label} info must be an object")
    info_extra = set(info) - {"title", "version"}
    if info_extra:
        raise ValidationFailed(f"{label} info contains unsupported fields: {sorted(info_extra)}")
    title = info.get("title")
    info_version = info.get("version")
    if not isinstance(title, str) or not title.strip():
        raise ValidationFailed(f"{label} info.title is required")
    if not isinstance(info_version, str) or not info_version.strip():
        raise ValidationFailed(f"{label} info.version is required")
    _reject_markers(title, f"{label} info.title")
    _reject_markers(info_version, f"{label} info.version")
    components = document.get("components") or {}
    if not isinstance(components, dict):
        raise ValidationFailed(f"{label} components must be an object")
    comp_extra = set(components) - {"schemas"}
    if comp_extra:
        raise ValidationFailed(f"{label} components contains unsupported fields: {sorted(comp_extra)}")
    schemas = components.get("schemas") or {}
    if not isinstance(schemas, dict):
        raise ValidationFailed(f"{label} components.schemas must be an object")
    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise ValidationFailed(f"{label} paths must be a non-empty object")
    if len(schemas) > MAX_PROPERTIES:
        raise ValidationFailed(f"{label} has too many schemas")
    operations: dict[tuple[str, str], Operation] = {}
    for path, item in paths.items():
        if not isinstance(path, str) or not path.startswith("/") or "//" in path:
            raise ValidationFailed(f"{label} path {path!r} is not allowed")
        _reject_markers(path, f"{label} path")
        if not isinstance(item, dict):
            raise ValidationFailed(f"{label} path {path} must be an object")
        path_extra = set(item) - set(SUPPORTED_METHODS)
        if path_extra:
            raise ValidationFailed(f"{label} path {path} contains unsupported fields: {sorted(path_extra)}")
        for method in SUPPORTED_METHODS:
            if method not in item:
                continue
            op = _parse_operation(
                item[method],
                method=method,
                path=path,
                schemas=schemas,
                label=f"{label} {method.upper()} {path}",
            )
            operations[(method.upper(), path)] = op
    if not operations:
        raise ValidationFailed(f"{label} does not define an operation")
    return SpecModel(title=title, version=info_version, operations=operations)


def _parse_operation(node: Any, *, method: str, path: str, schemas: dict, label: str) -> Operation:
    if not isinstance(node, dict):
        raise ValidationFailed(f"{label} must be an object")
    allowed = {"operationId", "parameters", "requestBody", "responses", "description"}
    extra = set(node) - allowed
    if extra:
        raise ValidationFailed(f"{label} contains unsupported fields: {sorted(extra)}")
    parameters = tuple(_parse_parameters(node.get("parameters") or [], schemas, label))
    request_body = None
    if "requestBody" in node:
        request_body = _parse_request_body(node["requestBody"], schemas, label)
    responses = node.get("responses")
    if not isinstance(responses, dict) or not responses:
        raise ValidationFailed(f"{label} responses are required")
    parsed_responses: dict[str, Schema | None] = {}
    for status, response in responses.items():
        if not isinstance(status, str) or not status.isdigit():
            raise ValidationFailed(f"{label} response status {status!r} is unsupported")
        parsed_responses[status] = _parse_response(response, schemas, f"{label} {status}")
    return Operation(
        method=method.upper(),
        path=path,
        parameters=parameters,
        request_body=request_body,
        responses=parsed_responses,
    )


def _parse_parameters(items: Any, schemas: dict, label: str) -> list[Parameter]:
    if not isinstance(items, list):
        raise ValidationFailed(f"{label} parameters must be an array")
    parsed: list[Parameter] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValidationFailed(f"{label} parameter must be an object")
        extra = set(item) - {"name", "in", "required", "schema", "description"}
        if extra:
            raise ValidationFailed(f"{label} parameter contains unsupported fields: {sorted(extra)}")
        name = item.get("name")
        location = item.get("in")
        if not isinstance(name, str) or not name:
            raise ValidationFailed(f"{label} parameter name is required")
        if location not in {"query", "path", "header"}:
            raise ValidationFailed(f"{label} parameter {name} location is unsupported")
        key = (location, name)
        if key in seen:
            raise ValidationFailed(f"{label} duplicate parameter {location}:{name}")
        seen.add(key)
        required = item.get("required", False)
        if not isinstance(required, bool):
            raise ValidationFailed(f"{label} parameter {name} required must be boolean")
        if location == "path" and required is not True:
            raise ValidationFailed(f"{label} path parameter {name} must be required")
        schema = _parse_schema(item.get("schema"), schemas, f"{label} parameter {name}", set())
        parsed.append(Parameter(name=name, location=location, required=required, schema=schema))
    return parsed


def _parse_request_body(node: Any, schemas: dict, label: str) -> Schema:
    if not isinstance(node, dict):
        raise ValidationFailed(f"{label} requestBody must be an object")
    extra = set(node) - {"required", "content", "description"}
    if extra:
        raise ValidationFailed(f"{label} requestBody contains unsupported fields: {sorted(extra)}")
    return _schema_from_content(node.get("content"), schemas, f"{label} requestBody")


def _parse_response(node: Any, schemas: dict, label: str) -> Schema | None:
    if not isinstance(node, dict):
        raise ValidationFailed(f"{label} response must be an object")
    extra = set(node) - {"description", "content"}
    if extra:
        raise ValidationFailed(f"{label} response contains unsupported fields: {sorted(extra)}")
    if "description" not in node or not isinstance(node["description"], str):
        raise ValidationFailed(f"{label} description is required")
    if "content" not in node:
        return None
    return _schema_from_content(node["content"], schemas, label)


def _schema_from_content(content: Any, schemas: dict, label: str) -> Schema:
    if not isinstance(content, dict) or set(content) != {"application/json"}:
        raise ValidationFailed(f"{label} content must be only application/json")
    media = content["application/json"]
    if not isinstance(media, dict) or set(media) - {"schema"}:
        raise ValidationFailed(f"{label} media object supports only schema")
    return _parse_schema(media.get("schema"), schemas, label, set())


def _parse_schema(node: Any, schemas: dict, label: str, stack: set[str]) -> Schema:
    if not isinstance(node, dict):
        raise ValidationFailed(f"{label} schema must be an object")
    extra = set(node) - SCHEMA_KEYS
    if extra:
        raise ValidationFailed(f"{label} schema contains unsupported fields: {sorted(extra)}")
    if "$ref" in node:
        if set(node) != {"$ref"}:
            raise ValidationFailed(f"{label} $ref must be the only schema keyword")
        ref = node["$ref"]
        name = _local_ref_name(ref, label)
        if name in stack:
            raise ValidationFailed(f"{label} schema cycle at {name}")
        if name not in schemas:
            raise ValidationFailed(f"{label} unknown local ref {name}")
        stack.add(name)
        try:
            resolved = _parse_schema(schemas[name], schemas, f"{label} -> {name}", stack)
        finally:
            stack.remove(name)
        resolved.ref_name = name
        return resolved
    type_name = node.get("type")
    if type_name not in SCHEMA_TYPES:
        raise ValidationFailed(f"{label} schema type is required and must be a supported JSON type")
    nullable = node.get("nullable", False)
    if not isinstance(nullable, bool):
        raise ValidationFailed(f"{label} nullable must be boolean")
    enum = None
    if "enum" in node:
        if not isinstance(node["enum"], list) or not node["enum"]:
            raise ValidationFailed(f"{label} enum must be a non-empty array")
        enum = tuple(node["enum"])
    fmt = node.get("format")
    if fmt is not None and not isinstance(fmt, str):
        raise ValidationFailed(f"{label} format must be a string")
    additional = None
    properties: dict[str, Schema] = {}
    required: frozenset[str] = frozenset()
    items = None
    if type_name == "object":
        raw_props = node.get("properties") or {}
        if not isinstance(raw_props, dict):
            raise ValidationFailed(f"{label} properties must be an object")
        if len(raw_props) > MAX_PROPERTIES:
            raise ValidationFailed(f"{label} has too many properties")
        for key, child in raw_props.items():
            if not isinstance(key, str) or not key:
                raise ValidationFailed(f"{label} property name is invalid")
            properties[key] = _parse_schema(child, schemas, f"{label}.{key}", stack)
        raw_required = node.get("required") or []
        if not isinstance(raw_required, list) or not all(isinstance(item, str) for item in raw_required):
            raise ValidationFailed(f"{label} required must be an array of strings")
        if len(raw_required) != len(set(raw_required)):
            raise ValidationFailed(f"{label} required contains duplicates")
        missing = [item for item in raw_required if item not in properties]
        if missing:
            raise ValidationFailed(f"{label} required names missing from properties: {missing}")
        required = frozenset(raw_required)
        if "additionalProperties" in node:
            if not isinstance(node["additionalProperties"], bool):
                raise ValidationFailed(f"{label} additionalProperties must be boolean in this subset")
            additional = node["additionalProperties"]
    elif type_name == "array":
        if "items" not in node:
            raise ValidationFailed(f"{label} array schema requires items")
        items = _parse_schema(node["items"], schemas, f"{label}[]", stack)
    return Schema(
        type_name=type_name,
        properties=properties,
        required=required,
        items=items,
        nullable=nullable,
        additional_properties=additional,
        enum=enum,
        format=fmt,
    )


def _local_ref_name(ref: Any, label: str) -> str:
    if not isinstance(ref, str) or not ref.startswith("#/components/schemas/"):
        raise ValidationFailed(f"{label} only local #/components/schemas refs are allowed")
    name = ref.removeprefix("#/components/schemas/")
    if not name or "/" in name or name.startswith("http"):
        raise ValidationFailed(f"{label} ref is not a local schema name")
    return name


def _reject_markers(text: str, label: str) -> None:
    if len(text) > MAX_STRING_CHARS:
        raise ValidationFailed(f"{label} exceeds the string limit")
    lowered = text.lower()
    for marker in EXECUTABLE_STRING_MARKERS:
        if marker in lowered:
            raise ValidationFailed(f"{label} contains a forbidden URL-like value")


def assert_node_budget(document: Any, *, label: str) -> None:
    count = _count(document)
    if count > MAX_NODES:
        raise ValidationFailed(f"{label} exceeds {MAX_NODES} JSON nodes")


def _count(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + sum(_count(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(_count(item) for item in value)
    return 1


def is_json_number(value: Any) -> bool:
    """True for Decimal numbers. False for bool, which is a subclass of int."""
    return isinstance(value, Decimal) and not isinstance(value, bool)
