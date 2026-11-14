"""Factory selection. boto3 is replaced with fakes; these tests do not call AWS."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from apireview.aws_dynamo import CloudCallError, DocumentTable, FakeDynamoClient
from apireview.canonical import canonical_dumps, parse_json_bytes
from apireview.errors import FailClosed
from apireview.handlers.lambda_handlers import (
    BlobStore,
    Settings,
    _require_resources,
    analyze_handler,
    diff_handler,
    dispatcher_handler,
    finalize_handler,
    harness_handler,
    publisher_handler,
    register_wait_handler,
    review_handler,
    seal_handler,
    spec_check_handler,
    submit_handler,
    validate_handler,
)
from apireview.limits import MAX_SPEC_BYTES
from apireview.runtime_clients import clear_clients, sdk_client
from apireview.signed_mock import SignedMockTransport
from scripts.assemble_template import build_template

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_sdk_cache():
    clear_clients()
    yield
    clear_clients()


class FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]

    def get_object(self, **kwargs):
        return {"Body": FakeBody(self.objects[(kwargs["Bucket"], kwargs["Key"])])}


class FakeEvents:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def put_events(self, **kwargs):
        self.entries.extend(kwargs["Entries"])
        return {"Entries": [{"EventId": f"id-{index}"} for index, _entry in enumerate(kwargs["Entries"])], "FailedEntryCount": 0}


class FakeSfn:
    def __init__(self) -> None:
        self.executions: dict[str, dict] = {}
        self.callbacks: list[str] = []

    def start_execution(self, **kwargs):
        name = kwargs["name"]
        current = self.executions.get(name)
        if current is not None:
            if current["input"] != kwargs["input"] or current["closed"]:
                raise CloudCallError("ExecutionAlreadyExists")
            return {"executionArn": current["arn"]}
        arn = "arn:aws:states:us-east-1:example:execution:api-review:" + name
        self.executions[name] = {"arn": arn, "input": kwargs["input"], "closed": False}
        return {"executionArn": arn}

    def describe_execution(self, **kwargs):
        for item in self.executions.values():
            if item["arn"] == kwargs["executionArn"]:
                return {"executionArn": item["arn"], "input": item["input"], "status": "RUNNING"}
        raise CloudCallError("ExecutionDoesNotExist")

    def send_task_success(self, **kwargs):
        if kwargs["taskToken"] in self.callbacks:
            raise CloudCallError("TaskDoesNotExist")
        self.callbacks.append(kwargs["taskToken"])
        return {}


class FakeBedrock:
    def __init__(self) -> None:
        self.calls = 0

    def converse(self, **kwargs):
        self.calls += 1
        assert kwargs["modelId"]
        body = json.dumps({"explanations": [], "proposals": []})
        return {"output": {"message": {"content": [{"text": body}]}}}


class FakeInterpreter:
    def __init__(self) -> None:
        self.stopped = False

    def start_code_interpreter_session(self, **kwargs):
        assert kwargs["sessionTimeoutSeconds"] == 900
        return {"sessionId": "sess-test"}

    def invoke_code_interpreter(self, **kwargs):
        text = json.dumps({"authoritative": False, "checkerVersion": "1.0.0", "findings": [], "ok": True})
        return {"stream": [{"result": {"content": [{"text": text, "type": "text"}], "isError": False}}]}

    def stop_code_interpreter_session(self, **kwargs):
        self.stopped = True


class FakeBoto3:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.dynamo = FakeDynamoClient()
        self.s3 = FakeS3()
        self.sfn = FakeSfn()
        self.events = FakeEvents()
        self.bedrock = FakeBedrock()
        self.interpreter = FakeInterpreter()
        self._clients: dict[str, object] = {}

    def client(self, service: str, region_name: str | None = None):
        self.calls.append((service, region_name or ""))
        if service not in self._clients:
            self._clients[service] = {
                "dynamodb": self.dynamo,
                "s3": self.s3,
                "stepfunctions": self.sfn,
                "events": self.events,
                "bedrock-runtime": self.bedrock,
                "bedrock-agentcore": self.interpreter,
            }[service]
        return self._clients[service]


def _settings(**changes) -> Settings:
    base = Settings(
        table_name="jobs",
        bucket_name="artifacts",
        state_machine_arn="arn:aws:states:us-east-1:example:stateMachine:api-review",
        event_bus_name="api-review",
        mock_host="mock.execute-api.us-east-1.amazonaws.com",
        mock_stage="live",
        harness_transport="in-process",
        spec_check_mode="in-process",
        bedrock_advisory="disabled",
        bedrock_model_id="us.amazon.nova-2-lite-v1:0",
        code_interpreter_id="",
        simulation_identity=True,
    )
    return replace(base, **changes)


def _load(name: str):
    return parse_json_bytes((ROOT / "fixtures" / name).read_bytes(), max_bytes=MAX_SPEC_BYTES, label=name)


def _event(role: str, session: str, body: dict | None = None, idempotency_key: str = "runtime-key-00001"):
    return {
        "body": None if body is None else canonical_dumps(body),
        "headers": {"Idempotency-Key": idempotency_key},
        "path": "/jobs",
        "requestContext": {"identity": {"userArn": f"arn:aws:sts::example:assumed-role/{role}/{session}"}},
    }


def _body(mock_version: str, new_name: str) -> dict:
    return {
        "fixture": _load("orders-client.fixture.json"),
        "mockVersion": mock_version,
        "newSpec": _load(new_name),
        "oldSpec": _load("orders-v1.openapi.json"),
    }


def _install(monkeypatch, sdk: FakeBoto3 | None = None) -> FakeBoto3:
    sdk = sdk or FakeBoto3()
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "api-review-submit")
    monkeypatch.setattr("apireview.runtime_clients.load_boto3", lambda: sdk)
    return sdk


def _outbox_image(sdk: FakeBoto3) -> dict:
    key = next(name for name in sdk.dynamo.items if name.startswith("OUTBOX#"))
    return sdk.dynamo.items[key]


def test_import_does_not_load_boto3():
    sys.modules.pop("boto3", None)
    import apireview.handlers.lambda_handlers  # noqa: F401
    import apireview.runtime_clients  # noqa: F401

    assert "boto3" not in sys.modules


def test_sdk_client_caches_one_client_per_service_and_region(monkeypatch):
    sdk = _install(monkeypatch)
    first = sdk_client("dynamodb")
    second = sdk_client("dynamodb")
    assert first is second
    assert sdk.calls == [("dynamodb", "us-east-1")]
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    clear_clients()
    sdk.calls.clear()
    sdk_client("events")
    assert sdk.calls == [("events", "us-west-2")]


def test_missing_region_fails_closed(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with pytest.raises(FailClosed, match="AWS_REGION"):
        sdk_client("dynamodb")


def test_outside_lambda_stays_fail_closed_and_does_not_build_clients(monkeypatch):
    sdk = FakeBoto3()
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.setattr("apireview.runtime_clients.load_boto3", lambda: (_ for _ in ()).throw(AssertionError("boto3 was loaded")))
    with pytest.raises(FailClosed, match="not configured"):
        submit_handler(_event("ApiReviewDeveloper", "ada", _body("orders-v2-corrected", "orders-v2-corrected.openapi.json")), settings=_settings())
    with pytest.raises(FailClosed, match="not configured"):
        _require_resources(None, None, _settings())
    assert sdk.calls == []


def test_injected_clients_win_inside_lambda(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr("apireview.runtime_clients.load_boto3", lambda: (_ for _ in ()).throw(AssertionError("boto3 was loaded")))
    table = DocumentTable(FakeDynamoClient(), "jobs")
    blobs = BlobStore(FakeS3(), "artifacts")
    created = submit_handler(
        _event("ApiReviewDeveloper", "ada", _body("orders-v2-corrected", "orders-v2-corrected.openapi.json")),
        table=table,
        blobs=blobs,
        settings=_settings(),
    )
    assert created["statusCode"] == 201
    kept_table, kept_blobs = _require_resources(table, blobs, _settings())
    assert kept_table is table
    assert kept_blobs is blobs


def test_lambda_handlers_use_cached_sdk_clients(monkeypatch):
    sdk = _install(monkeypatch)
    active = _settings()
    created = submit_handler(
        _event("ApiReviewDeveloper", "ada", _body("orders-v2-corrected", "orders-v2-corrected.openapi.json")),
        settings=active,
    )
    assert created["statusCode"] == 201
    job = json.loads(created["body"])
    publisher_handler(
        {"Records": [{"dynamodb": {"SequenceNumber": "10", "NewImage": _outbox_image(sdk)}}]},
        settings=active,
    )
    detail = json.loads(sdk.events.entries[-1]["Detail"])
    dispatched = dispatcher_handler(
        {"Records": [{"body": json.dumps({"detail": detail}), "messageId": "m1"}]},
        settings=active,
    )
    assert dispatched["batchItemFailures"] == []
    again = dispatcher_handler(
        {"Records": [{"body": json.dumps({"detail": detail}), "messageId": "m2"}]},
        settings=active,
    )
    assert again["batchItemFailures"] == []
    assert len(sdk.sfn.executions) == 1
    carry = json.loads(next(iter(sdk.sfn.executions.values()))["input"])
    for handler in (validate_handler, diff_handler, analyze_handler, spec_check_handler, harness_handler, seal_handler):
        handler(carry, settings=active)
    sealed = sdk.dynamo
    stored = DocumentTable(sealed, "jobs").get(f"JOB#{job['jobId']}")
    assert stored["status"] == "SEALED"
    register_wait_handler({"jobId": job["jobId"], "taskToken": "local-task-test"}, settings=active)
    current = DocumentTable(sealed, "jobs").get(f"JOB#{job['jobId']}")
    reviewed = review_handler(
        _event(
            "ApiReviewReviewer",
            "ben",
            {
                "comment": "sealed pass",
                "decision": "approve",
                "evidenceHash": current["evidenceHash"],
                "jobId": job["jobId"],
                "nonce": current["reviewNonce"],
            },
            idempotency_key="runtime-key-review",
        ),
        settings=active,
    )
    assert reviewed["statusCode"] == 202
    finalized = finalize_handler(
        {
            "jobId": job["jobId"],
            "review": {
                "decision": "approve",
                "evidenceHash": current["evidenceHash"],
                "jobId": job["jobId"],
                "nonce": current["reviewNonce"],
                "reviewer": "arn:aws:sts::example:assumed-role/ApiReviewReviewer/ben",
            },
        },
        settings=active,
    )
    assert finalized["result"] == "APPROVED"
    services = [service for service, _region in sdk.calls]
    assert services.count("dynamodb") == 1
    assert services.count("s3") == 1
    assert services.count("stepfunctions") == 1
    assert services.count("events") == 1
    assert "bedrock-runtime" not in services
    assert "bedrock-agentcore" not in services


def test_dispatcher_without_lambda_does_not_start_an_execution(monkeypatch):
    sdk = FakeBoto3()
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.setattr("apireview.runtime_clients.load_boto3", lambda: sdk)
    table = DocumentTable(FakeDynamoClient(), "jobs")
    blobs = BlobStore(FakeS3(), "artifacts")
    created = submit_handler(
        _event("ApiReviewDeveloper", "ada", _body("orders-v2-corrected", "orders-v2-corrected.openapi.json"), idempotency_key="runtime-key-00002"),
        table=table,
        blobs=blobs,
        settings=_settings(),
    )
    job_id = json.loads(created["body"])["jobId"]
    result = dispatcher_handler(
        {"Records": [{"body": json.dumps({"detail": {"jobId": job_id}}), "messageId": "m1"}]},
        table=table,
        blobs=blobs,
        settings=_settings(),
    )
    assert result["batchItemFailures"] == [{"itemIdentifier": "m1"}]
    assert sdk.calls == []


def test_bedrock_client_is_built_only_when_advisory_is_enabled(monkeypatch):
    sdk = _install(monkeypatch)
    active = _settings()
    created = submit_handler(
        _event("ApiReviewDeveloper", "ada", _body("orders-v2-broken", "orders-v2-broken.openapi.json"), idempotency_key="runtime-key-00003"),
        settings=active,
    )
    job_id = json.loads(created["body"])["jobId"]
    dispatcher_handler({"Records": [{"body": json.dumps({"detail": {"jobId": job_id}}), "messageId": "m1"}]}, settings=active)
    carry = json.loads(next(iter(sdk.sfn.executions.values()))["input"])
    validate_handler(carry, settings=active)
    diff_handler(carry, settings=active)
    analyze_handler(carry, settings=active)
    assert "bedrock-runtime" not in [service for service, _region in sdk.calls]
    analyze_handler(carry, settings=replace(active, bedrock_advisory="enabled"))
    assert sdk.bedrock.calls == 1
    assert any(service == "bedrock-runtime" for service, _region in sdk.calls)


def test_code_interpreter_client_is_built_only_for_that_mode(monkeypatch):
    sdk = _install(monkeypatch)
    active = _settings()
    created = submit_handler(
        _event("ApiReviewDeveloper", "ada", _body("orders-v2-corrected", "orders-v2-corrected.openapi.json"), idempotency_key="runtime-key-00004"),
        settings=active,
    )
    job_id = json.loads(created["body"])["jobId"]
    dispatcher_handler({"Records": [{"body": json.dumps({"detail": {"jobId": job_id}}), "messageId": "m1"}]}, settings=active)
    carry = json.loads(next(iter(sdk.sfn.executions.values()))["input"])
    validate_handler(carry, settings=active)
    spec_check_handler(carry, settings=active)
    assert "bedrock-agentcore" not in [service for service, _region in sdk.calls]
    with pytest.raises(FailClosed, match="not configured"):
        spec_check_handler(carry, settings=replace(active, spec_check_mode="agentcore", code_interpreter_id=""))
    checked = spec_check_handler(carry, settings=replace(active, spec_check_mode="agentcore", code_interpreter_id="interpreter-test"))
    assert checked["jobId"] == job_id
    assert sdk.interpreter.stopped is True
    assert any(service == "bedrock-agentcore" for service, _region in sdk.calls)


def test_signed_mock_transport_uses_configured_host(monkeypatch):
    sdk = _install(monkeypatch)
    active = _settings()
    created = submit_handler(
        _event("ApiReviewDeveloper", "ada", _body("orders-v1", "orders-v1.openapi.json"), idempotency_key="runtime-key-00005"),
        settings=active,
    )
    job_id = json.loads(created["body"])["jobId"]
    dispatcher_handler({"Records": [{"body": json.dumps({"detail": {"jobId": job_id}}), "messageId": "m1"}]}, settings=active)
    carry = json.loads(next(iter(sdk.sfn.executions.values()))["input"])
    validate_handler(carry, settings=active)
    seen: dict[str, object] = {}

    def capture(fixture, plan, transport):
        seen["transport"] = transport
        seen["url"] = plan.target.url
        return {"cases": [], "complete": True, "harnessVersion": "1", "mockVersion": "orders-v1", "passed": True, "truncated": False}

    monkeypatch.setattr("apireview.handlers.lambda_handlers.run_harness", capture)
    signed = replace(active, harness_transport="signed-mock")
    harness_handler(carry, settings=signed)
    assert isinstance(seen["transport"], SignedMockTransport)
    assert seen["url"] == "https://mock.execute-api.us-east-1.amazonaws.com/live/orders-v1/orders"


def test_review_role_can_read_sealed_artifacts():
    statements = build_template()["Resources"]["ReviewRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    actions: list[str] = []
    for statement in statements:
        value = statement["Action"]
        actions.extend(value if isinstance(value, list) else [value])
    assert "s3:GetObject" in actions
    assert "dynamodb:GetItem" in actions
    assert "dynamodb:UpdateItem" in actions
    assert "states:SendTaskSuccess" in actions
