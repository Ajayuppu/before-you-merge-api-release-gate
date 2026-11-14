"""In-process job store with atomic multi-item updates.

This stands in for a DynamoDB table plus content-addressed S3 objects. It is
not the DynamoDB API. The boto3 request builder is tested separately.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any

from apireview.errors import Conflict, NotFound


@dataclass
class PutNew:
    key: str
    item: dict[str, Any]


@dataclass
class PutIfAbsentOrSame:
    """Content-addressed put. An equal item is success; a different item conflicts."""

    key: str
    item: dict[str, Any]


@dataclass
class UpdateIf:
    key: str
    changes: dict[str, Any]
    equals: dict[str, Any] = field(default_factory=dict)
    absent: tuple[str, ...] = ()


class MemoryStore:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def scan(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return [(key, copy.deepcopy(item)) for key, item in self._items.items()]

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(key)
            return copy.deepcopy(item) if item is not None else None

    def transaction(self, ops: list[PutNew | PutIfAbsentOrSame | UpdateIf]) -> None:
        with self._lock:
            snapshot = copy.deepcopy(self._items)
            try:
                for op in ops:
                    if isinstance(op, PutNew):
                        if op.key in self._items:
                            raise Conflict(f"item already exists: {op.key}")
                        self._items[op.key] = copy.deepcopy(op.item)
                    elif isinstance(op, PutIfAbsentOrSame):
                        current = self._items.get(op.key)
                        if current is None:
                            self._items[op.key] = copy.deepcopy(op.item)
                        elif current != op.item:
                            raise Conflict(f"item differs: {op.key}")
                    elif isinstance(op, UpdateIf):
                        current = self._items.get(op.key)
                        if current is None:
                            raise NotFound(op.key)
                        for name in op.absent:
                            if name in current and current[name] is not None:
                                raise Conflict(f"attribute exists: {name}")
                        for name, expected in op.equals.items():
                            if current.get(name) != expected:
                                raise Conflict(f"condition failed for {name}")
                        current.update(copy.deepcopy(op.changes))
                    else:
                        raise Conflict("unsupported store operation")
            except Exception:
                self._items = snapshot
                raise
