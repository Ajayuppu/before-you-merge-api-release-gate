"""Structured spans without the maintenance-mode X-Ray SDK.

When opentelemetry-api is installed, spans are also recorded there. Lambda
deployments enable the AWS Lambda Layer for OpenTelemetry separately. This
module never imports aws_xray_sdk.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

LOGGER = logging.getLogger("apireview")

try:
    from opentelemetry import trace as otel_trace
except ImportError:  # pragma: no cover - depends on the local environment
    otel_trace = None


def new_trace_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def span(name: str, sink: list[dict[str, Any]] | None = None, **attributes: Any) -> Iterator[dict[str, str]]:
    safe = {key: value for key, value in attributes.items() if key not in {"taskToken", "reviewNonce"}}
    current = {"traceId": new_trace_id(), "spanId": uuid.uuid4().hex[:16], "name": name}
    started = time.time()
    _emit(sink, "span.start", **current, **safe)
    token = None
    otel_span = None
    if otel_trace is not None:
        otel_span = otel_trace.get_tracer("apireview").start_span(name)
        token = otel_trace.use_span(otel_span, end_on_exit=False)
        token.__enter__()
    try:
        yield current
    except Exception:
        _emit(sink, "span.error", **current, elapsedMs=int((time.time() - started) * 1000))
        raise
    finally:
        if otel_span is not None and token is not None:
            token.__exit__(None, None, None)
            otel_span.end()
        _emit(sink, "span.end", **current, elapsedMs=int((time.time() - started) * 1000))


def _emit(sink: list[dict[str, Any]] | None, event: str, **fields: Any) -> None:
    record = {"event": event, **fields}
    LOGGER.info("%s", record)
    if sink is not None:
        sink.append(record)
