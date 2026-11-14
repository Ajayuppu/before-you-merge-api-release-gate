"""Local HTTP API. Identity is an explicit simulation header, never a bearer token."""

from __future__ import annotations

import json
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from apireview import LOCAL_SIMULATION_BANNER
from apireview.canonical import canonical_dumps, parse_json_bytes
from apireview.errors import ReviewError
from apireview.identity import local_principal, require_local_simulation_header
from apireview.limits import MAX_FIXTURE_BYTES, MAX_SPEC_BYTES
from apireview.service import ReviewService

MAX_BODY = MAX_SPEC_BYTES * 2 + MAX_FIXTURE_BYTES + 4096


class ReviewHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], service: ReviewService, on_change: Any = None):
        self.service = service
        self.on_change = on_change
        super().__init__(server_address, ReviewHandler)


class ReviewHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ReviewHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"status": "ok", "mode": "local-simulation", "banner": LOCAL_SIMULATION_BANNER})
            return
        try:
            principal = self._principal()
            if path.startswith("/jobs/") and path.endswith("/report"):
                job_id = path.removeprefix("/jobs/").removesuffix("/report")
                self._send(200, self.server.service.report(principal, job_id))
                return
            if path.startswith("/jobs/"):
                job_id = path.removeprefix("/jobs/")
                job = self.server.service.store.get(f"job#{job_id}")
                if job is None:
                    self._send(404, {"error": "not_found", "message": "job not found"})
                    return
                self._send(200, self.server.service.public_job(job, principal))
                return
        except ReviewError as exc:
            self._send(exc.http_status, {"error": exc.code, "message": str(exc)})
            return
        self._send(404, {"error": "not_found", "message": "path not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if self.headers.get("Authorization"):
                self._send(
                    400,
                    {
                        "error": "validation_failed",
                        "message": "This local server does not accept Authorization headers. "
                        + LOCAL_SIMULATION_BANNER,
                    },
                )
                return
            principal = self._principal()
            document = self._body()
            if path == "/jobs":
                self._reject_authority_fields(document)
                view = self.server.service.submit(
                    principal,
                    idempotency_key=self.headers.get("Idempotency-Key") or "",
                    old_spec=canonical_dumps(document["oldSpec"]).encode("utf-8"),
                    new_spec=canonical_dumps(document["newSpec"]).encode("utf-8"),
                    fixture=canonical_dumps(document["fixture"]).encode("utf-8"),
                    mock_version=document["mockVersion"],
                    drain=True,
                )
                self._changed()
                self._send(201, view)
                return
            if path == "/reviews":
                self._reject_authority_fields(document)
                allowed = {"jobId", "evidenceHash", "nonce", "decision", "comment"}
                extra = set(document) - allowed
                if extra:
                    self._send(400, {"error": "validation_failed", "message": f"unsupported review fields: {sorted(extra)}"})
                    return
                view = self.server.service.review(
                    principal,
                    job_id=document["jobId"],
                    evidence_hash=document["evidenceHash"],
                    nonce=document["nonce"],
                    decision=document["decision"],
                    comment=document.get("comment") or "",
                )
                self._changed()
                self._send(200, view)
                return
        except ReviewError as exc:
            self._send(exc.http_status, {"error": exc.code, "message": str(exc)})
            return
        except (KeyError, TypeError) as exc:
            self._send(400, {"error": "validation_failed", "message": f"incomplete JSON body: {exc}"})
            return
        self._send(404, {"error": "not_found", "message": "path not found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _principal(self) -> Any:
        require_local_simulation_header(self.headers.get("X-Local-Auth"))
        roles = [part.strip() for part in (self.headers.get("X-Local-Roles") or "").split(",") if part.strip()]
        return local_principal(self.headers.get("X-Local-Principal") or "", roles)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > MAX_BODY:
            raise ReviewError("request body is missing or too large")
        raw = self.rfile.read(length)
        document = parse_json_bytes(raw, max_bytes=MAX_BODY, label="request")
        if not isinstance(document, dict):
            raise ReviewError("request body must be a JSON object")
        return document

    def _reject_authority_fields(self, document: dict[str, Any]) -> None:
        banned = {"principal", "reviewer", "role", "taskToken", "task_token", "verdict"}
        found = banned & set(document)
        if found:
            raise ReviewError(f"request body cannot name authority fields: {sorted(found)}")

    def _changed(self) -> None:
        if self.server.on_change is not None:
            self.server.on_change(self.server.service)

    def _send(self, status: int, payload: Any) -> None:
        body = _dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Api-Review-Mode", "local-simulation")
        self.end_headers()
        self.wfile.write(body)


def _dumps(value: Any) -> str:
    return json.dumps(_plain(value), indent=2, sort_keys=True)


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return json.loads(text)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def serve(service: ReviewService, host: str, port: int, on_change: Any = None) -> None:
    server = ReviewHTTPServer((host, port), service, on_change=on_change)
    print(f"local simulation API http://{host}:{port}")
    print(LOCAL_SIMULATION_BANNER)
    server.serve_forever()
