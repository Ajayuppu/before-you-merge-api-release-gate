"""Hard quotas for specs, fixtures, and review comments.

These are application limits. They are not API Gateway throttle settings.
"""

from __future__ import annotations

MAX_SPEC_BYTES = 256 * 1024
MAX_FIXTURE_BYTES = 64 * 1024
MAX_REVIEW_COMMENT_CHARS = 500
MAX_NESTING_DEPTH = 32
MAX_NODES = 2000
MAX_STRING_CHARS = 8192
MAX_FIXTURE_CASES = 20
MAX_PROPERTIES = 500
MAX_RESPONSE_BYTES = 65_536
HARNESS_TIMEOUT_SECONDS = 3
CODE_INTERPRETER_SESSION_SECONDS = 900
REVIEW_TTL_SECONDS = 900
MAX_SUBMITS_PER_WINDOW = 30
SUBMIT_WINDOW_SECONDS = 60
SQS_MAX_RECEIVES = 5
DISPATCHER_TIMEOUT_SECONDS = 30
DISPATCHER_BATCH_WINDOW_SECONDS = 2
# visibility >= 6 * function timeout + batch window
DISPATCHER_VISIBILITY_TIMEOUT_SECONDS = (
    6 * DISPATCHER_TIMEOUT_SECONDS + DISPATCHER_BATCH_WINDOW_SECONDS
)

MOCK_VERSIONS = ("orders-v1", "orders-v2-broken", "orders-v2-corrected")

# Route allowlist is the path the old client calls, not a URL.
ALLOWED_FIXTURE_ROUTES = {("GET", "/orders")}

# Cloud mock paths are deployment configuration, selected by version enum.
CLOUD_MOCK_ROUTES = {
    "orders-v1": "/orders-v1/orders",
    "orders-v2-broken": "/orders-v2-broken/orders",
    "orders-v2-corrected": "/orders-v2-corrected/orders",
}

FORBIDDEN_KEYS = frozenset(
    {
        "code",
        "script",
        "command",
        "exec",
        "eval",
        "url",
        "urls",
        "uri",
        "href",
        "endpoint",
        "callback",
        "callbacks",
        "webhook",
        "webhooks",
        "import",
        "module",
        "sql",
        "shell",
        "bytecode",
        "pickle",
        "lambda",
        "function",
        "principal",
        "reviewer",
        "role",
        "tasktoken",
        "task_token",
        "authorization",
    }
)

EXECUTABLE_STRING_MARKERS = ("s3://", "http://", "https://", "file://", "ftp://")
