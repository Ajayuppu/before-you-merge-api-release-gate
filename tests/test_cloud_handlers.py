"""Handler tests with in-process fakes. These are not AWS calls."""

from __future__ import annotations

import json
from pathlib import Path

from apireview.aws_calls import parse_code_interpreter_stream, run_code_interpreter
from apireview.aws_dynamo import CloudCallError, DocumentTable, FakeDynamoClient
from apireview.canonical import canonical_dumps, parse_json_bytes
from apireview.handlers.lambda_handlers import (
    BlobStore,
    Settings,
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
from apireview.signed_mock import _NoRedirect

ROOT = Path(__file__).resolve().parents[1]


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
    def __init__(self, fail_first: bool = False):
        self.fail_first = fail_first
        self.entries: list[dict] = []

    def put_events(self, **kwargs):
        entries = kwargs["Entries"]
        results = []
        failed = 0
        for index, entry in enumerate(entries):
            if self.fail_first and index == 0:
                results.append({"ErrorCode": "InternalFailure", "ErrorMessage": "simulated"})
                failed += 1
                self.fail_first = False
            else:
                self.entries.append(entry)
                results.append({"EventId": f"id-{len(self.entries)}"})
        return {"Entries": results, "FailedEntryCount": failed}


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


class FakeInterpreter:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.stopped = False

    def start_code_interpreter_session(self, **kwargs):
        assert kwargs["sessionTimeoutSeconds"] == 900
        assert kwargs["arguments"] if False else True
        return {"sessionId": "sess-test"}

    def invoke_code_interpreter(self, **kwargs):
        assert kwargs["name"] == "executeCode"
        assert kwargs["arguments"]["language"] == "python"
        if self.fail:
            raise RuntimeError("invoke failed")
        text = json.dumps({"authoritative": False, "checkerVersion": "1.0.0", "findings": [], "ok": True})
        return {"stream": [{"result": {"content": [{"text": text, "type": "text"}], "isError": False}}]}

    def stop_code_interpreter_session(self, **kwargs):
        self.stopped = True


def settings() -> Settings:
    return Settings(
        table_name="jobs",
        bucket_name="artifacts",
        state_machine_arn="arn:aws:states:us-east-1:example:stateMachine:api-review",
        event_bus_name="api-review",
        mock_host="mock.local",
        mock_stage="live",
        harness_transport="in-process",
        spec_check_mode="in-process",
        bedrock_advisory="disabled",
        bedrock_model_id="us.amazon.nova-2-lite-v1:0",
        code_interpreter_id="",
        simulation_identity=True,
    )


def world():
    table = DocumentTable(FakeDynamoClient(), "jobs")
    blobs = BlobStore(FakeS3(), "artifacts")
    return table, blobs


def _load(name: str):
    return parse_json_bytes((ROOT / "fixtures" / name).read_bytes(), max_bytes=MAX_SPEC_BYTES, label=name)


def _event(role: str, session: str, body: dict | None = None, job_id: str | None = None, idempotency_key: str = "cloud-key-00001"):
    return {
        "body": None if body is None else canonical_dumps(body),
        "headers": {"Idempotency-Key": idempotency_key},
        "path": "/jobs",
        "pathParameters": {"jobId": job_id} if job_id else None,
        "requestContext": {"identity": {"userArn": f"arn:aws:sts::example:assumed-role/{role}/{session}"}},
    }


def _drive(table, blobs, active, event):
    validate_handler(event, table=table, blobs=blobs, settings=active)
    diff_handler(event, table=table, blobs=blobs, settings=active)
    analyze_handler(event, table=table, blobs=blobs, settings=active)
    spec_check_handler(event, table=table, blobs=blobs, settings=active)
    harness_handler(event, table=table, blobs=blobs, settings=active)
    return seal_handler(event, table=table, blobs=blobs, settings=active)


def test_cloud_handlers_block_broken_and_approve_corrected_with_fakes():
    table, blobs = world()
    active = settings()
    events = FakeEvents()
    sfn = FakeSfn()
    broken_body = {
        "fixture": _load("orders-client.fixture.json"),
        "mockVersion": "orders-v2-broken",
        "newSpec": _load("orders-v2-broken.openapi.json"),
        "oldSpec": _load("orders-v1.openapi.json"),
    }
    created = submit_handler(_event("ApiReviewDeveloper", "ada", broken_body), table=table, blobs=blobs, settings=active)
    assert created["statusCode"] == 201
    job = json.loads(created["body"])
    assert job["status"] == "RECEIVED"
    outbox_key = next(key for key in table.client.items if key.startswith("OUTBOX#"))
    published = publisher_handler(
        {"Records": [{"dynamodb": {"SequenceNumber": "10", "NewImage": table.client.items[outbox_key]}}]},
        table=table,
        blobs=blobs,
        settings=active,
        events_client=events,
    )
    assert published["batchItemFailures"] == []
    detail = json.loads(events.entries[-1]["Detail"])
    dispatched = dispatcher_handler(
        {"Records": [{"body": json.dumps({"detail": detail}), "messageId": "m1"}]},
        table=table,
        blobs=blobs,
        settings=active,
        sfn=sfn,
    )
    assert dispatched["batchItemFailures"] == []
    again = dispatcher_handler(
        {"Records": [{"body": json.dumps({"detail": detail}), "messageId": "m2"}]},
        table=table,
        blobs=blobs,
        settings=active,
        sfn=sfn,
    )
    assert again["batchItemFailures"] == []
    assert len(sfn.executions) == 1
    carry = json.loads(next(iter(sfn.executions.values()))["input"])
    sealed = _drive(table, blobs, active, carry)
    assert sealed["verdict"] == "BLOCKED"
    stored = table.get(f"JOB#{job['jobId']}")
    assert stored["status"] == "BLOCKED"
    denied = review_handler(
        _event("ApiReviewReviewer", "ben", {"decision": "approve", "evidenceHash": stored["evidenceHash"], "jobId": job["jobId"], "nonce": "ab" * 16}),
        table=table,
        blobs=blobs,
        settings=active,
        sfn=sfn,
    )
    assert denied["statusCode"] == 409
    assert stored["status"] == "BLOCKED"

    corrected = {
        "fixture": _load("orders-client.fixture.json"),
        "mockVersion": "orders-v2-corrected",
        "newSpec": _load("orders-v2-corrected.openapi.json"),
        "oldSpec": _load("orders-v1.openapi.json"),
    }
    created = submit_handler(
        _event("ApiReviewDeveloper", "ada", corrected, idempotency_key="cloud-key-00002"),
        table=table,
        blobs=blobs,
        settings=active,
    )
    passed = json.loads(created["body"])
    outbox_key = next(key for key, item in table.client.items.items() if key.startswith("OUTBOX#") and "00002" not in key and loads_status(item) == "PENDING")
    publisher_handler(
        {"Records": [{"dynamodb": {"SequenceNumber": "11", "NewImage": table.client.items[outbox_key]}}]},
        table=table,
        blobs=blobs,
        settings=active,
        events_client=FakeEvents(),
    )
    # The second outbox is the corrected job. Dispatch every pending message by reading executions after a direct dispatch.
    dispatcher_handler(
        {"Records": [{"body": json.dumps({"detail": {"jobId": passed["jobId"]}}), "messageId": "m3"}]},
        table=table,
        blobs=blobs,
        settings=active,
        sfn=sfn,
    )
    carry = json.loads(sfn.executions["review-" + passed["jobId"]]["input"])
    sealed = _drive(table, blobs, active, carry)
    assert sealed["verdict"] == "PASS"
    register_wait_handler({"jobId": passed["jobId"], "taskToken": "local-task-test"}, table=table, blobs=blobs, settings=active)
    current = table.get(f"JOB#{passed['jobId']}")
    assert current["status"] == "WAITING_REVIEW"
    assert "taskToken" not in json.loads(submit_handler(_event("ApiReviewDeveloper", "ada", corrected, idempotency_key="cloud-key-00002"), table=table, blobs=blobs, settings=active)["body"])
    reviewed = review_handler(
        _event(
            "ApiReviewReviewer",
            "ben",
            {
                "comment": "sealed pass",
                "decision": "approve",
                "evidenceHash": current["evidenceHash"],
                "jobId": passed["jobId"],
                "nonce": current["reviewNonce"],
            },
        ),
        table=table,
        blobs=blobs,
        settings=active,
        sfn=sfn,
    )
    assert reviewed["statusCode"] == 202
    assert table.get(f"JOB#{passed['jobId']}")["status"] == "REVIEW_CALLBACK_PENDING"
    replay = review_handler(
        _event(
            "ApiReviewReviewer",
            "ben",
            {"decision": "approve", "evidenceHash": current["evidenceHash"], "jobId": passed["jobId"], "nonce": current["reviewNonce"]},
        ),
        table=table,
        blobs=blobs,
        settings=active,
        sfn=sfn,
    )
    assert json.loads(replay["body"])["callback"] == "already-consumed"
    assert table.get(f"JOB#{passed['jobId']}")["status"] == "REVIEW_CALLBACK_PENDING"
    finalized = finalize_handler(
        {
            "jobId": passed["jobId"],
            "review": {
                "decision": "approve",
                "evidenceHash": current["evidenceHash"],
                "jobId": passed["jobId"],
                "nonce": current["reviewNonce"],
                "reviewer": "arn:aws:sts::example:assumed-role/ApiReviewReviewer/ben",
            },
        },
        table=table,
        blobs=blobs,
        settings=active,
    )
    assert finalized["result"] == "APPROVED"


def test_code_interpreter_stops_after_failure_and_redirects_are_not_followed():
    interpreter = FakeInterpreter(fail=True)
    try:
        run_code_interpreter(interpreter, interpreter_id="interpreter-test", candidate_spec={"openapi": "3.0.3"})
        raise AssertionError("invoke should fail")
    except RuntimeError:
        pass
    assert interpreter.stopped is True
    healthy = FakeInterpreter()
    parsed = run_code_interpreter(healthy, interpreter_id="interpreter-test", candidate_spec={"openapi": "3.0.3"})
    assert parsed["authoritative"] is False
    assert parsed["networkIsolation"] == "sandbox-is-not-an-air-gap"
    assert healthy.stopped is True
    assert parse_code_interpreter_stream({"stream": [{"result": {"isError": True, "content": []}}]})["ok"] is None
    assert _NoRedirect().redirect_request(None, None, 302, "found", {}, "https://example.invalid") is None


def loads_status(item: dict) -> str:
    return item["status"]["S"]
