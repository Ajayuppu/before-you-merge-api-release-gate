"""Local Step Functions stand-in for one Standard-workflow execution name.

Same name and same input while the execution is open returns the original
execution. A different input, or a closed execution, raises
ExecutionAlreadyExists. SendTaskSuccess is not idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from apireview.errors import ExecutionAlreadyExists, TaskDoesNotExist


@dataclass
class Execution:
    name: str
    input_str: str
    arn: str
    status: str = "RUNNING"
    output: dict[str, Any] | None = None
    task_token: str | None = None
    token_open: bool = False
    state: str = "Validate"
    history: list[str] = field(default_factory=list)


class LocalExecutions:
    def __init__(self) -> None:
        self.by_name: dict[str, Execution] = {}
        self.by_token: dict[str, Execution] = {}
        self.start_count = 0

    def start(self, name: str, input_str: str, drive: Callable[[Execution], None]) -> Execution:
        existing = self.by_name.get(name)
        if existing is not None:
            if existing.input_str == input_str and existing.status in {"RUNNING", "WAITING"}:
                return existing
            raise ExecutionAlreadyExists(name)
        self.start_count += 1
        execution = Execution(name=name, input_str=input_str, arn=f"local-execution:{name}")
        self.by_name[name] = execution
        drive(execution)
        return execution

    def describe(self, name: str) -> Execution:
        if name not in self.by_name:
            raise KeyError(name)
        return self.by_name[name]

    def reconcile(self, name: str, input_str: str) -> Execution:
        existing = self.describe(name)
        if existing.input_str != input_str:
            raise ExecutionAlreadyExists(name)
        return existing

    def park(self, execution: Execution, token: str) -> None:
        execution.status = "WAITING"
        execution.state = "WaitForReview"
        execution.task_token = token
        execution.token_open = True
        execution.history.append("WaitForReview")
        self.by_token[token] = execution

    def send_task_success(self, token: str, output: dict[str, Any]) -> Execution:
        execution = self.by_token.get(token)
        if execution is None or not execution.token_open or execution.status != "WAITING":
            raise TaskDoesNotExist()
        execution.token_open = False
        execution.status = "RUNNING"
        execution.state = "Finalize"
        execution.output = output
        execution.history.append("Finalize")
        return execution

    def succeed(self, execution: Execution, state: str) -> None:
        execution.status = "SUCCEEDED"
        execution.state = state
        execution.history.append(state)

    def fail(self, execution: Execution, state: str) -> None:
        execution.status = "FAILED"
        execution.state = state
        execution.history.append(state)

    def timeout(self, name: str) -> Execution:
        execution = self.describe(name)
        if execution.status != "WAITING":
            raise TaskDoesNotExist()
        execution.token_open = False
        execution.status = "TIMED_OUT"
        execution.state = "Expire"
        execution.history.append("Expire")
        return execution
