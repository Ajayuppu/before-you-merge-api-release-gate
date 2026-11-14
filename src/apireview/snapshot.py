"""Persist the local simulation so separate CLI processes share one job store.

The file is local simulation state. It holds the server-side task token so a
later review command can complete the callback. It is not an AWS credential.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from apireview.bus import LocalEventBridge
from apireview.executions import Execution, LocalExecutions
from apireview.service import ReviewService
from apireview.store import MemoryStore, PutNew


def save_service(path: Path, service: ReviewService) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "items": _encode(list(service.store.scan())),
        "executions": [_encode(_execution_dict(item)) for item in service.executions.by_name.values()],
        "startCount": service.executions.start_count,
        "queue": _encode(service.bus.queue),
        "deliveryDlq": _encode(service.bus.delivery_dlq),
        "workerDlq": _encode(service.bus.worker_dlq),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def load_service(path: Path) -> ReviewService:
    service = ReviewService()
    if not path.exists():
        return service
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1:
        raise ValueError("unsupported local state version")
    items = _decode(payload["items"])
    if items:
        service.store.transaction([PutNew(key=key, item=item) for key, item in items])
    for raw in _decode(payload.get("executions", [])):
        execution = Execution(
            name=raw["name"],
            input_str=raw["input_str"],
            arn=raw["arn"],
            status=raw["status"],
            output=raw.get("output"),
            task_token=raw.get("task_token"),
            token_open=bool(raw.get("token_open")),
            state=raw.get("state", ""),
            history=list(raw.get("history") or []),
        )
        service.executions.by_name[execution.name] = execution
        if execution.task_token:
            service.executions.by_token[execution.task_token] = execution
    service.executions.start_count = int(payload.get("startCount") or 0)
    service.bus.queue = _decode(payload.get("queue", []))
    service.bus.delivery_dlq = _decode(payload.get("deliveryDlq", []))
    service.bus.worker_dlq = _decode(payload.get("workerDlq", []))
    return service


def _execution_dict(execution: Execution) -> dict[str, Any]:
    return {
        "arn": execution.arn,
        "history": list(execution.history),
        "input_str": execution.input_str,
        "name": execution.name,
        "output": execution.output,
        "state": execution.state,
        "status": execution.status,
        "task_token": execution.task_token,
        "token_open": execution.token_open,
    }


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"$decimal": format(value, "f")}
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$decimal"}:
            return Decimal(value["$decimal"])
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def empty_store() -> MemoryStore:
    return MemoryStore()


def empty_bus() -> LocalEventBridge:
    return LocalEventBridge()
