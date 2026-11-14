"""Response-aware and request-aware compatibility diff for the OpenAPI subset.

Removing a required response property breaks old clients. Adding a required
request property breaks callers. Those directions are separate rules.
"""

from __future__ import annotations

from typing import Any

from apireview.canonical import canonical_dumps, sha256_text
from apireview.openapi_subset import Operation, Schema, SpecModel


def diff_specs(old: SpecModel, new: SpecModel) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if old.version != new.version:
        _add(
            findings,
            severity="info",
            rule="info.version.changed",
            path="/info/version",
            message=f"info.version changed from {old.version} to {new.version}.",
        )
    old_keys = set(old.operations)
    new_keys = set(new.operations)
    for key in sorted(old_keys - new_keys):
        method, path = key
        _add(
            findings,
            severity="breaking",
            rule="operation.removed",
            path=f"/paths/{_escape(path)}/{method.lower()}",
            message=f"Removed {method} {path}. Old clients that call it will break.",
        )
    for key in sorted(new_keys - old_keys):
        method, path = key
        _add(
            findings,
            severity="info",
            rule="operation.added",
            path=f"/paths/{_escape(path)}/{method.lower()}",
            message=f"Added {method} {path}. Old clients do not call it.",
        )
    for key in sorted(old_keys & new_keys):
        _diff_operation(old.operations[key], new.operations[key], findings)
    return findings


def breaking_findings(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    return [item for item in findings if item["severity"] == "breaking"]


def _diff_operation(old: Operation, new: Operation, findings: list[dict[str, str]]) -> None:
    base = f"/paths/{_escape(old.path)}/{old.method.lower()}"
    _diff_parameters(old, new, base, findings)
    _diff_body(old.request_body, new.request_body, base + "/requestBody", "request", findings)
    old_status = set(old.responses)
    new_status = set(new.responses)
    for status in sorted(old_status - new_status):
        _add(
            findings,
            severity="breaking",
            rule="response.status.removed",
            path=f"{base}/responses/{status}",
            message=f"Removed response {status} from {old.method} {old.path}.",
        )
    for status in sorted(new_status - old_status):
        _add(
            findings,
            severity="info",
            rule="response.status.added",
            path=f"{base}/responses/{status}",
            message=f"Added response {status} on {old.method} {old.path}.",
        )
    for status in sorted(old_status & new_status):
        _diff_body(
            old.responses[status],
            new.responses[status],
            f"{base}/responses/{status}",
            "response",
            findings,
        )


def _diff_parameters(old: Operation, new: Operation, base: str, findings: list[dict[str, str]]) -> None:
    old_map = {(item.location, item.name): item for item in old.parameters}
    new_map = {(item.location, item.name): item for item in new.parameters}
    for key in sorted(old_map):
        if key not in new_map:
            param = old_map[key]
            if param.required:
                _add(
                    findings,
                    severity="info",
                    rule="request.parameter.removed.required",
                    path=f"{base}/parameters/{param.location}/{param.name}",
                    message=(
                        f"Removed required {param.location} parameter {param.name}. "
                        "The server now requires less, so old callers remain valid."
                    ),
                )
            else:
                _add(
                    findings,
                    severity="info",
                    rule="request.parameter.removed.optional",
                    path=f"{base}/parameters/{param.location}/{param.name}",
                    message=f"Removed optional {param.location} parameter {param.name}.",
                )
        else:
            _diff_schema(
                old_map[key].schema,
                new_map[key].schema,
                f"{base}/parameters/{key[0]}/{key[1]}",
                "request",
                findings,
            )
            if old_map[key].required != new_map[key].required:
                _required_flag(
                    old_map[key].required,
                    new_map[key].required,
                    f"{base}/parameters/{key[0]}/{key[1]}",
                    "request",
                    key[1],
                    findings,
                )
    for key in sorted(set(new_map) - set(old_map)):
        param = new_map[key]
        if param.required:
            _add(
                findings,
                severity="breaking",
                rule="request.parameter.added.required",
                path=f"{base}/parameters/{param.location}/{param.name}",
                message=f"Added required {param.location} parameter {param.name}. Old callers that omit it will break.",
            )
        else:
            _add(
                findings,
                severity="info",
                rule="request.parameter.added.optional",
                path=f"{base}/parameters/{param.location}/{param.name}",
                message=f"Added optional {param.location} parameter {param.name}.",
            )


def _diff_body(
    old: Schema | None,
    new: Schema | None,
    path: str,
    direction: str,
    findings: list[dict[str, str]],
) -> None:
    if old is None and new is None:
        return
    if old is None and new is not None:
        rule = "response.body.added" if direction == "response" else "request.body.added"
        severity = "info"
        message = "Added a JSON body where the previous contract had none."
        if direction == "request":
            severity = "breaking"
            rule = "request.body.added"
            message = "Added a request body. Old callers that sent no body will break."
        _add(findings, severity=severity, rule=rule, path=path, message=message)
        return
    if old is not None and new is None:
        severity = "breaking"
        rule = "response.body.removed" if direction == "response" else "request.body.removed"
        message = "Removed a JSON body that old clients exchange."
        if direction == "request":
            severity = "info"
            message = "Removed a request body. Old callers become easier to satisfy."
        _add(findings, severity=severity, rule=rule, path=path, message=message)
        return
    assert old is not None and new is not None
    _diff_schema(old, new, path, direction, findings)


def _diff_schema(
    old: Schema,
    new: Schema,
    path: str,
    direction: str,
    findings: list[dict[str, str]],
) -> None:
    if old.type_name != new.type_name:
        widened = direction == "response" and old.type_name == "integer" and new.type_name == "number"
        if widened:
            _add(
                findings,
                severity="info",
                rule="response.type.widened",
                path=path + "/type",
                message="Response type widened from integer to number.",
            )
        else:
            _add(
                findings,
                severity="breaking",
                rule=f"{direction}.type.changed",
                path=path + "/type",
                message=(
                    f"{direction.capitalize()} type changed from {old.type_name} to {new.type_name}. "
                    + (
                        "Old clients that decode the previous JSON type will break."
                        if direction == "response"
                        else "Old callers that send the previous JSON type will break."
                    )
                ),
            )
        return
    if old.nullable and not new.nullable:
        _add(
            findings,
            severity="breaking",
            rule=f"{direction}.nullable.removed",
            path=path + "/nullable",
            message=f"{direction.capitalize()} no longer accepts null.",
        )
    elif not old.nullable and new.nullable:
        _add(
            findings,
            severity="info",
            rule=f"{direction}.nullable.added",
            path=path + "/nullable",
            message=f"{direction.capitalize()} now allows null.",
        )
    if old.format != new.format:
        _add(
            findings,
            severity="info",
            rule=f"{direction}.format.changed",
            path=path + "/format",
            message=f"format changed from {old.format!r} to {new.format!r}.",
        )
    if old.enum != new.enum:
        _diff_enum(old, new, path, direction, findings)
    if old.type_name == "object":
        _diff_object(old, new, path, direction, findings)
    elif old.type_name == "array" and old.items is not None and new.items is not None:
        _diff_schema(old.items, new.items, path + "/items", direction, findings)


def _diff_enum(old: Schema, new: Schema, path: str, direction: str, findings: list[dict[str, str]]) -> None:
    old_values = set(old.enum or ())
    new_values = set(new.enum or ())
    removed = old_values - new_values
    added = new_values - old_values
    if direction == "response" and removed:
        _add(
            findings,
            severity="breaking",
            rule="response.enum.narrowed",
            path=path + "/enum",
            message="Response enum dropped values old clients may already observe.",
        )
    if direction == "request" and removed:
        _add(
            findings,
            severity="breaking",
            rule="request.enum.narrowed",
            path=path + "/enum",
            message="Request enum dropped values old callers may still send.",
        )
    if added:
        _add(
            findings,
            severity="info",
            rule=f"{direction}.enum.expanded",
            path=path + "/enum",
            message="Enum gained values.",
        )


def _diff_object(old: Schema, new: Schema, path: str, direction: str, findings: list[dict[str, str]]) -> None:
    for name in sorted(set(old.properties) - set(new.properties)):
        required = name in old.required
        if direction == "response":
            rule = "response.property.removed.required" if required else "response.property.removed.optional"
            _add(
                findings,
                severity="breaking",
                rule=rule,
                path=f"{path}/properties/{name}",
                message=(
                    f"Removed response property {name}. "
                    + (
                        "It was required, so old clients that read it will break."
                        if required
                        else "It was optional, and old clients that read it can still break."
                    )
                ),
            )
        else:
            _add(
                findings,
                severity="info",
                rule="request.property.removed",
                path=f"{path}/properties/{name}",
                message=f"Removed request property {name}. The server accepts a smaller caller payload.",
            )
    for name in sorted(set(new.properties) - set(old.properties)):
        required = name in new.required
        if direction == "response":
            rule = "response.property.added.required" if required else "response.property.added.optional"
            _add(
                findings,
                severity="info",
                rule=rule,
                path=f"{path}/properties/{name}",
                message=(
                    f"Added response property {name}. "
                    + (
                        "A new required response field is still compatible: old clients ignore it, and the server now always sends it."
                        if required
                        else "An optional additive response field is compatible with old clients."
                    )
                ),
            )
        elif required:
            _add(
                findings,
                severity="breaking",
                rule="request.property.added.required",
                path=f"{path}/properties/{name}",
                message=f"Added required request property {name}. Old callers that omit it will break.",
            )
        else:
            _add(
                findings,
                severity="info",
                rule="request.property.added.optional",
                path=f"{path}/properties/{name}",
                message=f"Added optional request property {name}.",
            )
    for name in sorted(set(old.properties) & set(new.properties)):
        _diff_schema(old.properties[name], new.properties[name], f"{path}/properties/{name}", direction, findings)
        _required_flag(name in old.required, name in new.required, f"{path}/properties/{name}", direction, name, findings)


def _required_flag(
    old_required: bool,
    new_required: bool,
    path: str,
    direction: str,
    name: str,
    findings: list[dict[str, str]],
) -> None:
    if old_required == new_required:
        return
    if direction == "response" and old_required and not new_required:
        _add(
            findings,
            severity="breaking",
            rule="response.required.relaxed",
            path=path + "/required",
            message=f"Response property {name} is no longer required. Old clients that require it will break.",
        )
    elif direction == "response" and not old_required and new_required:
        _add(
            findings,
            severity="info",
            rule="response.required.tightened",
            path=path + "/required",
            message=f"Response property {name} is now required. Old clients can ignore the extra guarantee.",
        )
    elif direction == "request" and not old_required and new_required:
        _add(
            findings,
            severity="breaking",
            rule="request.required.tightened",
            path=path + "/required",
            message=f"Request property {name} is now required. Old callers that omit it will break.",
        )
    elif direction == "request" and old_required and not new_required:
        _add(
            findings,
            severity="info",
            rule="request.required.relaxed",
            path=path + "/required",
            message=f"Request property {name} is now optional. Old callers remain valid.",
        )


def _add(findings: list[dict[str, str]], *, severity: str, rule: str, path: str, message: str) -> None:
    finding_id = sha256_text(rule + "\n" + path)[:16]
    findings.append(
        {
            "id": finding_id,
            "severity": severity,
            "rule": rule,
            "path": path,
            "message": message,
        }
    )


def _escape(path: str) -> str:
    return path.replace("~", "~0").replace("/", "~1")


def finding_digest(findings: list[dict[str, Any]]) -> str:
    return sha256_text(canonical_dumps(findings))
