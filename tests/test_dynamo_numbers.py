"""DynamoDB numbers are Decimal on the wire. Reads return native numbers."""

from __future__ import annotations

import json
from decimal import Decimal

from apireview.aws_dynamo import DocumentTable, FakeDynamoClient, loads_document, native_numbers
from apireview.canonical import canonical_dumps
from apireview.evidence import seal_evidence
from apireview.handlers.lambda_handlers import (
    BlobStore,
    Settings,
    _response,
    expire_handler,
    publisher_handler,
    review_context_handler,
    review_handler,
    status_handler,
)


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class _S3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]

    def get_object(self, **kwargs):
        return {"Body": _Body(self.objects[(kwargs["Bucket"], kwargs["Key"])])}


class _Events:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def put_events(self, **kwargs):
        self.entries.extend(kwargs["Entries"])
        return {"Entries": [{"EventId": "id-1"}], "FailedEntryCount": 0}


def _settings() -> Settings:
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


def _numbers(value):
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _numbers(item)
    elif isinstance(value, list):
        for item in value:
            yield from _numbers(item)


def _event(role: str, session: str, job_id: str, path: str, body: dict | None = None) -> dict:
    return {
        "body": None if body is None else canonical_dumps(body),
        "headers": {},
        "path": path,
        "pathParameters": {"jobId": job_id},
        "requestContext": {"identity": {"userArn": f"arn:aws:sts::example:assumed-role/{role}/{session}"}},
    }


def test_report_serializes_decimal_evidence_and_hash_matches():
    harness = {
        "cases": [{"assertions": [], "complete": True, "id": "case-1", "passed": False}],
        "complete": True,
        "harnessVersion": "1",
        "mockVersion": "orders-v2-broken",
        "passed": False,
        "target": {"maxBytes": 65536, "timeoutSeconds": 3},
        "truncated": False,
    }
    advisory = {
        "deterministicHash": "abc",
        "explanations": [],
        "modelAdvisoryHash": None,
        "proposals": [],
        "source": "deterministic-local",
    }
    evidence = seal_evidence(
        job_id="job_" + "ab" * 10,
        old_spec_hash="a" * 64,
        new_spec_hash="b" * 64,
        fixture_hash="c" * 64,
        mock_version="orders-v2-broken",
        findings=[],
        harness=harness,
        checker=None,
        advisory=advisory,
    )
    assert evidence["evidenceHash"]
    assert all(isinstance(number, int) for number in _numbers(evidence))
    job_id = evidence["jobId"]
    owner = "arn:aws:sts::example:assumed-role/ApiReviewDeveloper/ada"
    job = {
        "advisory": advisory,
        "evidence": evidence,
        "evidenceHash": evidence["evidenceHash"],
        "fixtureHash": "c" * 64,
        "jobId": job_id,
        "mockVersion": "orders-v2-broken",
        "newSpecHash": "b" * 64,
        "oldSpecHash": "a" * 64,
        "owner": owner,
        "status": "WAITING_REVIEW",
    }
    table = DocumentTable(FakeDynamoClient(), "jobs")
    table.put_new([(f"JOB#{job_id}", job)])
    loaded = table.get(f"JOB#{job_id}")
    assert loaded is not None
    assert not any(isinstance(number, Decimal) for number in _numbers(loaded))
    assert loaded["evidence"]["harness"]["target"]["timeoutSeconds"] == 3
    assert isinstance(loaded["evidence"]["harness"]["target"]["timeoutSeconds"], int)
    assert canonical_dumps(Decimal("19.95")) == "19.95"

    active = _settings()
    blobs = BlobStore(_S3(), "artifacts")
    report = status_handler(_event("ApiReviewDeveloper", "ada", job_id, f"/jobs/{job_id}/report"), table=table, blobs=blobs, settings=active)
    assert report["statusCode"] == 200
    body = json.loads(report["body"])
    assert body["evidence"]["evidenceHash"] == evidence["evidenceHash"]
    assert body["job"]["status"] == "WAITING_REVIEW"

    rebuilt = seal_evidence(
        job_id=loaded["jobId"],
        old_spec_hash=loaded["oldSpecHash"],
        new_spec_hash=loaded["newSpecHash"],
        fixture_hash=loaded["fixtureHash"],
        mock_version=loaded["mockVersion"],
        findings=loaded["evidence"]["findings"],
        harness=loaded["evidence"].get("harness"),
        checker=loaded.get("checker"),
        advisory=loaded.get("advisory") or {},
        agent_trace=loaded.get("agentTrace"),
    )
    assert rebuilt["evidenceHash"] == loaded["evidenceHash"]

    context = review_context_handler(
        _event("ApiReviewReviewer", "ben", job_id, f"/jobs/{job_id}/review-context"),
        table=table,
        blobs=blobs,
        settings=active,
    )
    assert context["statusCode"] == 200
    assert json.loads(context["body"])["verdict"] == "BLOCKED"

    denied = review_handler(
        _event(
            "ApiReviewReviewer",
            "ben",
            job_id,
            "/reviews",
            {"decision": "approve", "evidenceHash": loaded["evidenceHash"], "jobId": job_id, "nonce": "0" * 32},
        ),
        table=table,
        blobs=blobs,
        settings=active,
        sfn=object(),
    )
    assert denied["statusCode"] == 409
    json.loads(denied["body"])

    outbox = loads_document(canonical_dumps({"attempt": 1, "eventId": "evt_1", "jobId": job_id, "status": "PENDING"}))
    assert outbox["attempt"] == 1 and isinstance(outbox["attempt"], int)
    table.put_new([("OUTBOX#evt_1", {"attempt": Decimal("2"), "eventId": "evt_1", "jobId": job_id, "status": "PENDING"})])
    events = _Events()
    published = publisher_handler(
        {"Records": [{"dynamodb": {"SequenceNumber": "1", "NewImage": table.client.items["OUTBOX#evt_1"]}}]},
        table=table,
        blobs=blobs,
        settings=active,
        events_client=events,
    )
    assert published["batchItemFailures"] == []
    assert json.loads(events.entries[0]["Detail"]) == {"eventId": "evt_1", "jobId": job_id}
    assert table.get("OUTBOX#evt_1")["status"] == "DISPATCHED"

    expired = expire_handler({"jobId": job_id}, table=table, blobs=blobs, settings=active)
    assert json.dumps(expired) == '{"result": "EXPIRED"}'

    guarded = json.loads(_response(200, {"amount": Decimal("19.95"), "count": Decimal("3")})["body"])
    assert guarded["count"] == 3 and isinstance(guarded["count"], int)
    assert isinstance(guarded["amount"], float)
    assert guarded["amount"] == 19.95


def test_native_numbers_keeps_integral_values_as_int():
    converted = native_numbers({"count": Decimal("3"), "nested": [Decimal("19.95"), Decimal("-0")]})
    assert converted == {"count": 3, "nested": [19.95, 0]}
    assert isinstance(converted["count"], int)
    assert isinstance(converted["nested"][0], float)
    assert isinstance(converted["nested"][1], int)
