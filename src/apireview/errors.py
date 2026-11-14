"""Domain errors. HTTP and CLI layers map these; workflow code does not catch broadly."""


class ReviewError(Exception):
    """Base class for expected reviewer failures."""

    code = "review_error"
    http_status = 400


class ValidationFailed(ReviewError):
    code = "validation_failed"
    http_status = 400


class Conflict(ReviewError):
    code = "conflict"
    http_status = 409


class NotFound(ReviewError):
    code = "not_found"
    http_status = 404


class AuthorizationFailed(ReviewError):
    code = "authorization_failed"
    http_status = 403


class FailClosed(ReviewError):
    """A missing, stale, or inconsistent gate. Never treated as approval."""

    code = "fail_closed"
    http_status = 409


class InjectedCrash(RuntimeError):
    """Test-only interruption after a committed side effect."""


class ExecutionAlreadyExists(Exception):
    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


class TaskDoesNotExist(Exception):
    def __init__(self, token_hint: str = "task"):
        super().__init__(token_hint)
