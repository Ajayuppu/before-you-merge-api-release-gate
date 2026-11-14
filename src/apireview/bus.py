"""In-process EventBridge and SQS stand-ins with separate failure queues.

PutEvents ingestion failure is not the same as rule-target delivery failure.
The worker DLQ is a third queue: it receives dispatcher messages that exceed
the receive limit. Only the dispatcher starts a workflow.
"""

from __future__ import annotations

import copy
from typing import Any

from apireview.limits import SQS_MAX_RECEIVES


class LocalEventBridge:
    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []
        self.delivery_dlq: list[dict[str, Any]] = []
        self.worker_dlq: list[dict[str, Any]] = []
        self.ingestion_failures = 0
        self.delivery_failures = 0
        self.max_receives = SQS_MAX_RECEIVES
        self._seq = 0

    def put_events(self, entries: list[dict[str, Any]]) -> list[dict[str, str]]:
        failed: list[dict[str, str]] = []
        for entry in entries:
            if self.ingestion_failures:
                self.ingestion_failures -= 1
                failed.append({"ErrorCode": "InternalFailure", "ErrorMessage": "simulated ingestion failure"})
                continue
            if self.delivery_failures:
                self.delivery_failures -= 1
                self.delivery_dlq.append(copy.deepcopy(entry))
                continue
            self.enqueue(entry)
        return failed

    def enqueue(self, entry: dict[str, Any]) -> None:
        self._seq += 1
        self.queue.append(
            {
                "messageId": f"msg-{self._seq}",
                "body": copy.deepcopy(entry),
                "receives": 0,
                "available": True,
            }
        )
