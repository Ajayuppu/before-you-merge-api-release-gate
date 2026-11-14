"""DynamoDB document adapter for the job table.

Items store a canonical JSON document plus a few mirrored attributes so
condition expressions can fail closed without trusting a read-modify-write.
Reads turn JSON numbers into int when they are integral and float otherwise.
The in-process fake used by tests is not Amazon DynamoDB.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from apireview.canonical import canonical_dumps, parse_json_bytes
from apireview.errors import Conflict, NotFound

MIRRORED = ("status", "evidenceHash", "reviewNonce", "payloadHash")
MAX_ITEM_BYTES = 350_000


class CloudCallError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def error_code(exc: BaseException) -> str | None:
    if isinstance(exc, CloudCallError):
        return exc.code
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


def dumps_document(document: dict[str, Any]) -> str:
    return canonical_dumps(document)


def dynamodb_number(value: Decimal) -> int | float:
    """Map a DynamoDB number to int when it is integral, otherwise float."""
    integral = value.to_integral_value()
    if value == integral:
        return int(integral)
    return float(value)


def native_numbers(value: Any) -> Any:
    if isinstance(value, Decimal):
        return dynamodb_number(value)
    if isinstance(value, dict):
        return {key: native_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [native_numbers(item) for item in value]
    return value


def loads_document(text: str) -> dict[str, Any]:
    value = parse_json_bytes(text.encode("utf-8"), max_bytes=MAX_ITEM_BYTES, label="dynamodb document")
    if not isinstance(value, dict):
        raise Conflict("dynamodb document is not an object")
    return native_numbers(value)


def item_from_document(pk: str, document: dict[str, Any]) -> dict[str, dict[str, str]]:
    encoded = dumps_document(document)
    if len(encoded.encode("utf-8")) > MAX_ITEM_BYTES:
        raise Conflict("job document exceeds the item limit")
    item: dict[str, dict[str, str]] = {
        "pk": {"S": pk},
        "document": {"S": encoded},
    }
    for name in MIRRORED:
        if document.get(name) is not None:
            item[name] = {"S": str(document[name])}
    if document.get("executionInput"):
        item["executionInput"] = {"S": "present"}
    return item


class DocumentTable:
    def __init__(self, client: Any, table_name: str):
        self.client = client
        self.table_name = table_name

    def get(self, pk: str) -> dict[str, Any] | None:
        response = self.client.get_item(
            TableName=self.table_name,
            Key={"pk": {"S": pk}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        return loads_document(item["document"]["S"])

    def put_new(self, records: list[tuple[str, dict[str, Any]]]) -> None:
        self.client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": self.table_name,
                        "Item": item_from_document(pk, document),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                }
                for pk, document in records
            ]
        )

    def update(
        self,
        pk: str,
        *,
        equals: dict[str, Any],
        changes: dict[str, Any],
        absent: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        current = self.get(pk)
        if current is None:
            raise NotFound(pk)
        for name, expected in equals.items():
            if current.get(name) != expected:
                raise Conflict(f"condition failed for {name}")
        for name in absent:
            if current.get(name) is not None:
                raise Conflict(f"attribute exists: {name}")
        updated = dict(current)
        updated.update(changes)
        names = {"#document": "document"}
        values: dict[str, dict[str, str]] = {":document": {"S": dumps_document(updated)}}
        sets = ["#document = :document"]
        conditions = []
        for index, (name, expected) in enumerate(equals.items()):
            if name not in MIRRORED:
                continue
            placeholder = f"#eq{index}"
            value_name = f":eq{index}"
            names[placeholder] = name
            values[value_name] = {"S": str(expected)}
            conditions.append(f"{placeholder} = {value_name}")
        for name in MIRRORED:
            if updated.get(name) is not None:
                placeholder = f"#set_{name}"
                value_name = f":set_{name}"
                names[placeholder] = name
                values[value_name] = {"S": str(updated[name])}
                sets.append(f"{placeholder} = {value_name}")
        for name in absent:
            conditions.append(f"attribute_not_exists({name})")
        kwargs: dict[str, Any] = {
            "TableName": self.table_name,
            "Key": {"pk": {"S": pk}},
            "UpdateExpression": "SET " + ", ".join(sets),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
        if conditions:
            kwargs["ConditionExpression"] = " AND ".join(conditions)
        try:
            self.client.update_item(**kwargs)
        except Exception as exc:
            if error_code(exc) in {"ConditionalCheckFailedException", "TransactionCanceledException"}:
                raise Conflict("conditional update failed") from exc
            raise
        return updated


class FakeDynamoClient:
    """Subset of the DynamoDB client used above. Not the AWS service."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, dict[str, str]]] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self.items.get(kwargs["Key"]["pk"]["S"])
        return {"Item": copy.deepcopy(item)} if item else {}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        snapshot = copy.deepcopy(self.items)
        try:
            for entry in kwargs["TransactItems"]:
                put = entry["Put"]
                pk = put["Item"]["pk"]["S"]
                if pk in self.items:
                    raise CloudCallError("TransactionCanceledException", pk)
                self.items[pk] = copy.deepcopy(put["Item"])
        except Exception:
            self.items = snapshot
            raise
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        pk = kwargs["Key"]["pk"]["S"]
        current = self.items.get(pk)
        if current is None:
            raise CloudCallError("ConditionalCheckFailedException", "missing")
        expression = kwargs.get("ConditionExpression", "")
        values = kwargs["ExpressionAttributeValues"]
        names = kwargs["ExpressionAttributeNames"]
        for part in [piece.strip() for piece in expression.split("AND")]:
            if not part:
                continue
            if part.startswith("attribute_not_exists("):
                attribute = part[len("attribute_not_exists(") : -1]
                if attribute in current:
                    raise CloudCallError("ConditionalCheckFailedException", attribute)
                continue
            left, right = [piece.strip() for piece in part.split("=")]
            attribute = names[left]
            if current.get(attribute, {}).get("S") != values[right]["S"]:
                raise CloudCallError("ConditionalCheckFailedException", attribute)
        for key, value in values.items():
            if not key.startswith(":set_") and key != ":document":
                continue
            if key == ":document":
                current["document"] = value
            else:
                current[key.removeprefix(":set_")] = value
        if ":document" in values and "executionInput" in loads_document(values[":document"]["S"]):
            current["executionInput"] = {"S": "present"}
        return {}
