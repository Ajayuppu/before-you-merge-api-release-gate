"""Clock and identifiers."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

JOB_ID_RE = re.compile(r"^job_[0-9a-f]{20}$")
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock(Clock):
    def __init__(self, start: datetime | None = None):
        self.current = start or datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current = self.current + timedelta(seconds=seconds)


def new_job_id() -> str:
    return "job_" + secrets.token_hex(10)


def new_nonce() -> str:
    return secrets.token_hex(16)


def new_task_token() -> str:
    # Opaque local stand-in. Not a SigV4 token, not a JWT, not returned by the API.
    return "local-task-" + secrets.token_hex(16)


def execution_name_for(job_id: str) -> str:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError("job id is not execution-name safe")
    return "review-" + job_id
