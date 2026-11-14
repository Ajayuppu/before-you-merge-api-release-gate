"""SigV4 GET against the configured mock API.

Redirects are refused. This module is not called by the local test suite.
SANDBOX and in-process checks are separate from this network call.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from apireview.canonical import parse_json_bytes
from apireview.errors import FailClosed, ValidationFailed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class SignedMockTransport:
    def __init__(self, region: str):
        self.region = region

    def exchange(self, plan):  # noqa: ANN001
        if plan.target.allow_redirects:
            raise ValidationFailed("redirects are disabled")
        status, raw = _signed_get(plan.target.url, self.region, plan.target.timeout_seconds, plan.target.max_bytes)
        truncated = len(raw) > plan.target.max_bytes or status >= 300
        body = None
        if status == 200 and not truncated:
            body = parse_json_bytes(raw, max_bytes=plan.target.max_bytes, label="mock response")
        return {
            "body": body,
            "raw": raw[: plan.target.max_bytes],
            "redirects": 0,
            "status": status,
            "transport": "signed-mock",
            "truncated": truncated,
        }


def _signed_get(url: str, region: str, timeout: int, max_bytes: int) -> tuple[int, bytes]:
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.session import Session

    credentials = Session().get_credentials()
    if credentials is None:
        raise FailClosed("no AWS credentials for the mock call")
    request = AWSRequest(method="GET", url=url)
    SigV4Auth(credentials.get_frozen_credentials(), "execute-api", region).add_auth(request)
    outbound = urllib.request.Request(url, method="GET", headers=dict(request.headers.items()))
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(outbound, timeout=timeout) as response:
            return response.status, response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(max_bytes + 1)
