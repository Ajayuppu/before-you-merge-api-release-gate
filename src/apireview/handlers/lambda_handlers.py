"""API Gateway, queue, and Step Functions handlers.

Inject clients in tests. Outside Lambda, a missing client raises FailClosed
instead of creating SDK clients, so importing this module does not contact AWS.
Inside Lambda, a missing client is built on first use and then reused.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from apireview.advisory import deterministic_advisory, invoke_converse, merge_advisory, report_advisory
from apireview.agent_boundary import TrustedContext, ToolGateway, run_bounded_agent
from apireview.aws_calls import publish_events, run_code_interpreter, start_or_reconcile
from apireview.aws_dynamo import DocumentTable, dynamodb_number, error_code, loads_document
from apireview.canonical import canonical_bytes, canonical_dumps, parse_json_bytes, sha256_bytes
from apireview.checker import run_checker
from apireview.diff import diff_specs, finding_digest
from apireview.errors import AuthorizationFailed, Conflict, FailClosed, NotFound, ReviewError, ValidationFailed
from apireview.evidence import advisory_content_hash, seal_evidence
from apireview.fixtures import validate_fixture
from apireview.harness import HARNESS_VERSION, InProcessMockTransport, build_plan, run_harness
from apireview.identity import DEVELOPER_ROLE_NAME, REVIEWER_ROLE_NAME, principal_from_api_gateway
from apireview.ids import execution_name_for, new_job_id, new_nonce
from apireview.intake import intake_documents
from apireview.limits import MAX_REVIEW_COMMENT_CHARS, REVIEW_TTL_SECONDS
from apireview.openapi_subset import parse_spec

CARRY_FIELDS = ("jobId", "oldSpecHash", "newSpecHash", "fixtureHash", "mockVersion", "harnessVersion")


@dataclass
class Settings:
    table_name: str
    bucket_name: str
    state_machine_arn: str
    event_bus_name: str
    mock_host: str
    mock_stage: str
    harness_transport: str
    spec_check_mode: str
    bedrock_advisory: str
    bedrock_model_id: str
    code_interpreter_id: str
    simulation_identity: bool
    review_ttl_seconds: int = REVIEW_TTL_SECONDS


def settings_from_env() -> Settings:
    return Settings(
        table_name=os.environ["JOB_TABLE_NAME"],
        bucket_name=os.environ["ARTIFACT_BUCKET_NAME"],
        state_machine_arn=os.environ.get("STATE_MACHINE_ARN", ""),
        event_bus_name=os.environ.get("EVENT_BUS_NAME", "api-review"),
        mock_host=os.environ.get("MOCK_API_HOST", "mock.local"),
        mock_stage=os.environ.get("MOCK_STAGE", "live"),
        harness_transport=os.environ.get("HARNESS_TRANSPORT", "in-process"),
        spec_check_mode=os.environ.get("SPEC_CHECK_MODE", "in-process"),
        bedrock_advisory=os.environ.get("BEDROCK_ADVISORY", "disabled"),
        bedrock_model_id=os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-2-lite-v1:0"),
        code_interpreter_id=os.environ.get("CODE_INTERPRETER_ID", ""),
        simulation_identity=os.environ.get("LOCAL_IDENTITY_SIMULATION", "false") == "true",
        review_ttl_seconds=int(os.environ.get("REVIEW_TTL_SECONDS", str(REVIEW_TTL_SECONDS))),
    )


class BlobStore:
    def __init__(self, client: Any, bucket: str):
        self.client = client
        self.bucket = bucket

    def put_bytes(self, digest: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=f"artifacts/{digest}.json", Body=data, ContentType="application/json")

    def get_json(self, digest: str) -> Any:
        response = self.client.get_object(Bucket=self.bucket, Key=f"artifacts/{digest}.json")
        raw = response["Body"].read()
        if sha256_bytes(raw) != digest:
            raise FailClosed("artifact bytes do not match the stored hash")
        return parse_json_bytes(raw, max_bytes=len(raw) + 1, label="artifact")


def submit_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or settings_from_env()
    table, blobs = _require_resources(table, blobs, settings)
    try:
        principal = principal_from_api_gateway(event, expected_role_name=DEVELOPER_ROLE_NAME, simulation_account_allowed=settings.simulation_identity)
        body = _json_body(event)
        _reject_authority(body)
        headers = event.get("headers") or {}
        idempotency_key = headers.get("Idempotency-Key") or headers.get("idempotency-key")
        if not isinstance(idempotency_key, str):
            raise ValidationFailed("Idempotency-Key header is required")
        loaded = intake_documents(
            canonical_dumps(body["oldSpec"]).encode("utf-8"),
            canonical_dumps(body["newSpec"]).encode("utf-8"),
            canonical_dumps(body["fixture"]).encode("utf-8"),
            body["mockVersion"],
        )
        existing = table.get(_idem_pk(principal.subject, idempotency_key))
        if existing is not None:
            if existing["payloadHash"] != loaded.payload_hash:
                raise Conflict("idempotency key was already used with a different payload")
            job = table.get(_job_pk(existing["jobId"]))
            if job is None:
                raise FailClosed("idempotency record has no job")
            return _response(200, _public_job(job))
        job_id = new_job_id()
        event_id = "evt_" + job_id.removeprefix("job_")
        now = _now()
        seen: set[str] = set()
        for digest, document in (
            (loaded.old_hash, loaded.old_body),
            (loaded.new_hash, loaded.new_body),
            (loaded.fixture_hash, loaded.fixture_body),
        ):
            if digest in seen:
                continue
            seen.add(digest)
            blobs.put_bytes(digest, canonical_bytes(document))
        job = {
            "createdAt": now,
            "fixtureHash": loaded.fixture_hash,
            "idempotencyKey": idempotency_key,
            "jobId": job_id,
            "mockVersion": loaded.mock_version,
            "newSpecHash": loaded.new_hash,
            "oldSpecHash": loaded.old_hash,
            "owner": principal.subject,
            "payloadHash": loaded.payload_hash,
            "status": "RECEIVED",
            "updatedAt": now,
        }
        try:
            table.put_new(
                [
                    (_job_pk(job_id), job),
                    (_idem_pk(principal.subject, idempotency_key), {"jobId": job_id, "payloadHash": loaded.payload_hash, "status": "IDEMPOTENCY", "subject": principal.subject}),
                    (_outbox_pk(event_id), {"eventId": event_id, "jobId": job_id, "status": "PENDING"}),
                ]
            )
        except Exception as exc:
            if error_code(exc) != "TransactionCanceledException":
                raise
            raced = table.get(_idem_pk(principal.subject, idempotency_key))
            if raced and raced.get("payloadHash") == loaded.payload_hash:
                found = table.get(_job_pk(raced["jobId"]))
                if found is None:
                    raise FailClosed("idempotency record has no job") from exc
                return _response(200, _public_job(found))
            raise Conflict("idempotency key was already used with a different payload") from exc
        return _response(201, _public_job(job))
    except ReviewError as exc:
        return _response(exc.http_status, {"error": exc.code, "message": str(exc)})


def status_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or settings_from_env()
    table, _blobs = _require_resources(table, blobs, settings)
    try:
        principal = principal_from_api_gateway(event, expected_role_name=DEVELOPER_ROLE_NAME, simulation_account_allowed=settings.simulation_identity)
        job = _require(table, _path_job_id(event))
        if principal.subject != job["owner"]:
            raise AuthorizationFailed("job is owned by a different principal")
        if str(event.get("path", "")).endswith("/report"):
            if not job.get("evidence"):
                raise FailClosed("report is not sealed yet")
            return _response(
                200,
                {
                    "advisory": report_advisory(
                        job.get("advisory"),
                        enabled=settings.bedrock_advisory == "enabled",
                        model_id=settings.bedrock_model_id,
                    ),
                    "evidence": job["evidence"],
                    "job": _public_job(job),
                },
            )
        return _response(200, _public_job(job))
    except ReviewError as exc:
        return _response(exc.http_status, {"error": exc.code, "message": str(exc)})


def review_context_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or settings_from_env()
    table, _blobs = _require_resources(table, blobs, settings)
    try:
        principal = principal_from_api_gateway(event, expected_role_name=REVIEWER_ROLE_NAME, simulation_account_allowed=settings.simulation_identity)
        job = _require(table, _path_job_id(event))
        if principal.subject == job["owner"]:
            raise AuthorizationFailed("submitter cannot review their own job")
        if job.get("status") != "WAITING_REVIEW":
            raise FailClosed(f"job is not waiting for review ({job.get('status')})")
        return _response(
            200,
            {
                "evidenceHash": job.get("evidenceHash"),
                "jobId": job["jobId"],
                "reviewExpiry": job.get("reviewExpiry"),
                "reviewNonce": job.get("reviewNonce"),
                "status": job["status"],
                "verdict": (job.get("evidence") or {}).get("verdict"),
            },
        )
    except ReviewError as exc:
        return _response(exc.http_status, {"error": exc.code, "message": str(exc)})


def review_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None, sfn: Any = None) -> dict[str, Any]:
    settings = settings or settings_from_env()
    table, blobs = _require_resources(table, blobs, settings)
    try:
        principal = principal_from_api_gateway(event, expected_role_name=REVIEWER_ROLE_NAME, simulation_account_allowed=settings.simulation_identity)
        body = _json_body(event)
        _reject_authority(body)
        decision = body.get("decision")
        if decision not in {"approve", "reject"}:
            raise ValidationFailed("decision must be approve or reject")
        comment = body.get("comment") or ""
        if not isinstance(comment, str) or len(comment) > MAX_REVIEW_COMMENT_CHARS:
            raise ValidationFailed("comment length is invalid")
        job = _require(table, body["jobId"])
        if principal.subject == job["owner"]:
            raise AuthorizationFailed("submitter cannot review their own job")
        if job.get("status") == "WAITING_REVIEW" and _expired(job):
            table.update(_job_pk(job["jobId"]), equals={"status": "WAITING_REVIEW"}, changes={"status": "EXPIRED", "taskToken": None, "updatedAt": _now()})
            raise FailClosed("review window expired")
        if job["status"] not in {"WAITING_REVIEW", "REVIEW_CALLBACK_PENDING"}:
            raise FailClosed(f"job is not waiting for review ({job['status']})")
        if job.get("evidenceHash") != body.get("evidenceHash"):
            raise FailClosed("evidence hash does not match the sealed bundle")
        if job.get("reviewNonce") != body.get("nonce"):
            raise FailClosed("review nonce does not match this job")
        _recheck(job, blobs)
        if not job.get("taskToken"):
            raise FailClosed("review token is not registered")
        if job["status"] == "WAITING_REVIEW":
            job = table.update(
                _job_pk(job["jobId"]),
                equals={"status": "WAITING_REVIEW", "evidenceHash": job["evidenceHash"], "reviewNonce": job["reviewNonce"]},
                changes={"decision": decision, "reviewComment": comment, "reviewer": principal.subject, "status": "REVIEW_CALLBACK_PENDING", "updatedAt": _now()},
            )
        elif job.get("reviewer") != principal.subject or job.get("decision") != decision:
            raise FailClosed("replay rejected")
        if sfn is None and _in_lambda():
            from apireview import runtime_clients

            sfn = runtime_clients.stepfunctions_client()
        if sfn is None:
            raise FailClosed("step functions client is not configured")
        output = {"decision": decision, "evidenceHash": job["evidenceHash"], "jobId": job["jobId"], "nonce": job["reviewNonce"], "reviewer": principal.subject}
        try:
            sfn.send_task_success(taskToken=job["taskToken"], output=canonical_dumps(output))
        except Exception as exc:
            if error_code(exc) not in {"TaskDoesNotExist", "TaskTimedOut"}:
                raise
            return _response(202, {"callback": "already-consumed", "jobId": job["jobId"], "status": "REVIEW_CALLBACK_PENDING"})
        return _response(202, {"callback": "sent", "jobId": job["jobId"], "status": "REVIEW_CALLBACK_PENDING"})
    except ReviewError as exc:
        return _response(exc.http_status, {"error": exc.code, "message": str(exc)})


def publisher_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None, events_client: Any = None) -> dict[str, Any]:
    settings = settings or settings_from_env()
    table, _blobs = _require_resources(table, blobs, settings)
    if events_client is None and _in_lambda():
        from apireview import runtime_clients

        events_client = runtime_clients.events_client()
    if events_client is None:
        raise FailClosed("event bridge client is not configured")
    failures: list[dict[str, str]] = []
    pending: list[tuple[str, dict[str, Any]]] = []
    for record in event.get("Records") or []:
        sequence = (record.get("dynamodb") or {}).get("SequenceNumber", "")
        image = (record.get("dynamodb") or {}).get("NewImage") or {}
        pk = (image.get("pk") or {}).get("S", "")
        status = (image.get("status") or {}).get("S")
        if not pk.startswith("OUTBOX#") or status != "PENDING" or "document" not in image:
            continue
        pending.append((sequence, loads_document(image["document"]["S"])))
    if not pending:
        return {"batchItemFailures": failures}
    failed_indexes = publish_events(
        events_client,
        bus_name=settings.event_bus_name,
        entries=[{"eventId": item["eventId"], "jobId": item["jobId"]} for _sequence, item in pending],
    )
    failed = set(failed_indexes)
    for index, (sequence, document) in enumerate(pending):
        if index in failed:
            failures.append({"itemIdentifier": sequence})
            continue
        table.update(_outbox_pk(document["eventId"]), equals={"status": "PENDING"}, changes={"status": "DISPATCHED"})
    return {"batchItemFailures": failures}


def dispatcher_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None, sfn: Any = None) -> dict[str, Any]:
    settings = settings or settings_from_env()
    table, _blobs = _require_resources(table, blobs, settings)
    if sfn is None and _in_lambda():
        from apireview import runtime_clients

        sfn = runtime_clients.stepfunctions_client()
    failures = []
    for record in event.get("Records") or []:
        try:
            body = json.loads(record["body"])
            detail = body.get("detail")
            if isinstance(detail, str):
                detail = json.loads(detail)
            _dispatch_one(table, sfn, settings, detail["jobId"])
        except Exception:
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def validate_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None) -> dict[str, Any]:
    _settings, table, blobs = _ready(table, blobs, settings)
    job = _require(table, event["jobId"])
    if job["status"] == "QUEUED":
        table.update(_job_pk(job["jobId"]), equals={"status": "QUEUED"}, changes={"status": "RUNNING", "updatedAt": _now()})
    elif job["status"] != "RUNNING":
        raise FailClosed(f"validate saw status {job['status']}")
    parse_spec(blobs.get_json(job["oldSpecHash"]), label="old spec")
    parse_spec(blobs.get_json(job["newSpecHash"]), label="new spec")
    validate_fixture(blobs.get_json(job["fixtureHash"]))
    return _carry(event)


def diff_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None) -> dict[str, Any]:
    _settings, table, blobs = _ready(table, blobs, settings)
    job = _require(table, event["jobId"])
    findings = diff_specs(parse_spec(blobs.get_json(job["oldSpecHash"]), label="old spec"), parse_spec(blobs.get_json(job["newSpecHash"]), label="new spec"))
    table.update(_job_pk(job["jobId"]), equals={"status": "RUNNING"}, changes={"findings": findings, "updatedAt": _now()})
    return _carry(event)


def analyze_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None, bedrock: Any = None) -> dict[str, Any]:
    settings, table, _blobs = _ready(table, blobs, settings)
    job = _require(table, event["jobId"])
    advisory = deterministic_advisory(job["findings"])
    model_result = None
    if settings.bedrock_advisory == "enabled":
        if bedrock is None and _in_lambda():
            from apireview import runtime_clients

            bedrock = runtime_clients.bedrock_runtime_client()
        if bedrock is None:
            raise FailClosed("Bedrock advisory is enabled but no client was provided")
        model_result = invoke_converse(bedrock, settings.bedrock_model_id, job["findings"])
        advisory = merge_advisory(advisory, model_result, model_id=settings.bedrock_model_id)
    else:
        advisory = merge_advisory(advisory, None)
        advisory["modelStatus"] = "disabled"
    trace = run_bounded_agent(
        ToolGateway(
            {
                "read_artifacts": lambda ctx: {"fixtureHash": job["fixtureHash"], "newSpecHash": job["newSpecHash"], "oldSpecHash": job["oldSpecHash"]},
                "read_test_results": lambda ctx: {"available": False},
                "request_spec_check": lambda ctx: {"available": False},
            }
        ),
        TrustedContext(job_id=job["jobId"], owner=job["owner"], evidence_hash=None),
        [
            {"name": "read_artifacts", "arguments": {"jobId": job["jobId"]}},
            {"name": "request_spec_check", "arguments": {"jobId": job["jobId"]}},
            {"name": "read_test_results", "arguments": {"jobId": job["jobId"]}},
        ],
    )
    table.update(_job_pk(job["jobId"]), equals={"status": "RUNNING"}, changes={"advisory": advisory, "agentTrace": trace, "updatedAt": _now()})
    return _carry(event)


def spec_check_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None, interpreter: Any = None) -> dict[str, Any]:
    settings, table, blobs = _ready(table, blobs, settings)
    job = _require(table, event["jobId"])
    candidate = blobs.get_json(job["newSpecHash"])
    if settings.spec_check_mode == "agentcore":
        if interpreter is None and _in_lambda():
            from apireview import runtime_clients

            interpreter = runtime_clients.code_interpreter_client()
        if interpreter is None or not settings.code_interpreter_id:
            raise FailClosed("AgentCore mode is enabled but the interpreter is not configured")
        checker = run_code_interpreter(interpreter, interpreter_id=settings.code_interpreter_id, candidate_spec=candidate)
    else:
        checker = run_checker(candidate)
    checker["authoritative"] = False
    table.update(_job_pk(job["jobId"]), equals={"status": "RUNNING"}, changes={"checker": checker, "updatedAt": _now()})
    return _carry(event)


def harness_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None, transport: Any = None) -> dict[str, Any]:
    settings, table, blobs = _ready(table, blobs, settings)
    job = _require(table, event["jobId"])
    fixture = validate_fixture(blobs.get_json(job["fixtureHash"]))
    plan = build_plan(job["mockVersion"], host=settings.mock_host, stage=settings.mock_stage)
    if transport is None:
        if settings.harness_transport == "in-process":
            transport = InProcessMockTransport()
        elif settings.harness_transport == "signed-mock":
            from apireview.signed_mock import SignedMockTransport

            transport = SignedMockTransport(os.environ.get("AWS_REGION", "us-east-1"))
        else:
            raise FailClosed("harness transport is not recognized")
    try:
        harness = run_harness(fixture, plan, transport)
    except Exception:
        harness = {"cases": [], "complete": False, "harnessVersion": HARNESS_VERSION, "mockVersion": job["mockVersion"], "passed": False, "truncated": False}
    table.update(_job_pk(job["jobId"]), equals={"status": "RUNNING"}, changes={"harness": harness, "updatedAt": _now()})
    return _carry(event)


def seal_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None) -> dict[str, Any]:
    _settings, table, _blobs = _ready(table, blobs, settings)
    job = _require(table, event["jobId"])
    if not job.get("advisory"):
        raise ValidationFailed("mandatory analysis is missing")
    evidence = seal_evidence(
        job_id=job["jobId"],
        old_spec_hash=job["oldSpecHash"],
        new_spec_hash=job["newSpecHash"],
        fixture_hash=job["fixtureHash"],
        mock_version=job["mockVersion"],
        findings=job["findings"],
        harness=job.get("harness"),
        checker=job.get("checker"),
        advisory=job["advisory"],
        agent_trace=job.get("agentTrace"),
    )
    status = "BLOCKED" if evidence["verdict"] != "PASS" else "SEALED"
    table.update(
        _job_pk(job["jobId"]),
        equals={"status": "RUNNING"},
        changes={"evidence": evidence, "evidenceHash": evidence["evidenceHash"], "status": status, "updatedAt": _now()},
    )
    return _carry(event, verdict=evidence["verdict"], evidenceHash=evidence["evidenceHash"])


def register_wait_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None) -> dict[str, Any]:
    settings, table, _blobs = _ready(table, blobs, settings)
    payload = event.get("Payload") or event
    job = _require(table, payload["jobId"])
    token = payload.get("taskToken")
    if not isinstance(token, str) or not token:
        raise FailClosed("task token is missing")
    if job["status"] == "WAITING_REVIEW" and job.get("taskToken") == token:
        return {"jobId": job["jobId"], "registered": True}
    if job["status"] != "SEALED" or (job.get("evidence") or {}).get("verdict") != "PASS":
        raise FailClosed("wait registration requires a sealed pass")
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=settings.review_ttl_seconds)).isoformat()
    table.update(
        _job_pk(job["jobId"]),
        equals={"status": "SEALED", "evidenceHash": job["evidenceHash"]},
        changes={"reviewExpiry": expiry, "reviewNonce": new_nonce(), "status": "WAITING_REVIEW", "taskToken": token, "updatedAt": _now()},
    )
    return {"jobId": job["jobId"], "registered": True}


def finalize_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None) -> dict[str, Any]:
    _settings, table, blobs = _ready(table, blobs, settings)
    review = event.get("review") or {}
    job = _require(table, event["jobId"])
    if job["status"] in {"APPROVED", "REJECTED"}:
        _recheck(job, blobs)
        return {"result": job["status"]}
    if job["status"] != "REVIEW_CALLBACK_PENDING":
        raise FailClosed("finalize requires a claimed review")
    if (
        review.get("jobId") != job["jobId"]
        or review.get("evidenceHash") != job.get("evidenceHash")
        or review.get("nonce") != job.get("reviewNonce")
        or review.get("reviewer") != job.get("reviewer")
        or review.get("decision") != job.get("decision")
    ):
        table.update(_job_pk(job["jobId"]), equals={"status": "REVIEW_CALLBACK_PENDING"}, changes={"failureReason": "callback binding mismatch", "status": "FAILED", "taskToken": None, "updatedAt": _now()})
        raise FailClosed("callback binding mismatch")
    _recheck(job, blobs)
    if job["decision"] == "approve" and job["evidence"]["verdict"] != "PASS":
        table.update(_job_pk(job["jobId"]), equals={"status": "REVIEW_CALLBACK_PENDING"}, changes={"status": "BLOCKED", "taskToken": None, "updatedAt": _now()})
        return {"result": "BLOCKED"}
    new_status = "APPROVED" if job["decision"] == "approve" else "REJECTED"
    table.update(
        _job_pk(job["jobId"]),
        equals={"status": "REVIEW_CALLBACK_PENDING", "evidenceHash": job["evidenceHash"], "reviewNonce": job["reviewNonce"]},
        changes={"status": new_status, "taskToken": None, "updatedAt": _now()},
    )
    return {"result": new_status}


def expire_handler(event: dict[str, Any], context: Any = None, table: DocumentTable | None = None, blobs: BlobStore | None = None, settings: Settings | None = None) -> dict[str, Any]:
    _settings, table, _blobs = _ready(table, blobs, settings)
    payload = event.get("Payload") or event
    job = _require(table, payload["jobId"])
    if job["status"] == "WAITING_REVIEW":
        table.update(_job_pk(job["jobId"]), equals={"status": "WAITING_REVIEW"}, changes={"status": "EXPIRED", "taskToken": None, "updatedAt": _now()})
        return {"result": "EXPIRED"}
    return {"result": job["status"]}


def _dispatch_one(table: DocumentTable, sfn: Any, settings: Settings, job_id: str) -> None:
    if sfn is None or not settings.state_machine_arn:
        raise FailClosed("dispatcher is not configured")
    job = _require(table, job_id)
    if job["status"] in {"BLOCKED", "APPROVED", "REJECTED", "EXPIRED", "FAILED", "SEALED", "WAITING_REVIEW", "REVIEW_CALLBACK_PENDING"} and job.get("executionArn"):
        return
    if job.get("executionInput") and job.get("executionName"):
        name = job["executionName"]
        input_str = job["executionInput"]
    else:
        input_str = canonical_dumps(
            {
                "fixtureHash": job["fixtureHash"],
                "harnessVersion": HARNESS_VERSION,
                "jobId": job["jobId"],
                "mockVersion": job["mockVersion"],
                "newSpecHash": job["newSpecHash"],
                "oldSpecHash": job["oldSpecHash"],
            }
        )
        name = execution_name_for(job["jobId"])
        job = table.update(
            _job_pk(job_id),
            equals={"status": "RECEIVED"},
            absent=("executionInput",),
            changes={"executionInput": input_str, "executionName": name, "status": "QUEUED", "updatedAt": _now()},
        )
    started = start_or_reconcile(sfn, state_machine_arn=settings.state_machine_arn, name=name, input_str=input_str)
    if job.get("executionArn") != started["executionArn"]:
        table.update(_job_pk(job_id), equals={"status": job["status"]}, changes={"executionArn": started["executionArn"], "updatedAt": _now()})


def _recheck(job: dict[str, Any], blobs: BlobStore) -> None:
    if not job.get("evidence") or not job.get("evidenceHash"):
        raise FailClosed("evidence is missing")
    findings = diff_specs(parse_spec(blobs.get_json(job["oldSpecHash"]), label="old spec"), parse_spec(blobs.get_json(job["newSpecHash"]), label="new spec"))
    if finding_digest(findings) != finding_digest(job["evidence"]["findings"]):
        raise FailClosed("findings do not match the stored specs")
    advisory = job.get("advisory") or {}
    if advisory_content_hash(advisory) != job["evidence"]["advisory"].get("deterministicHash"):
        raise FailClosed("advisory bytes do not match the seal")
    rebuilt = seal_evidence(
        job_id=job["jobId"],
        old_spec_hash=job["oldSpecHash"],
        new_spec_hash=job["newSpecHash"],
        fixture_hash=job["fixtureHash"],
        mock_version=job["mockVersion"],
        findings=job["evidence"]["findings"],
        harness=job["evidence"].get("harness"),
        checker=job.get("checker"),
        advisory=advisory,
        agent_trace=job.get("agentTrace"),
    )
    if rebuilt["evidenceHash"] != job["evidenceHash"]:
        raise FailClosed("sealed evidence does not match stored result bytes")


def _in_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def _ready(table: DocumentTable | None, blobs: BlobStore | None, settings: Settings | None) -> tuple[Settings, DocumentTable, BlobStore]:
    settings = settings or settings_from_env()
    table, blobs = _require_resources(table, blobs, settings)
    return settings, table, blobs


def _require_resources(table: DocumentTable | None, blobs: BlobStore | None, settings: Settings | None = None) -> tuple[DocumentTable, BlobStore]:
    if table is not None and blobs is not None:
        return table, blobs
    if _in_lambda():
        from apireview import runtime_clients

        settings = settings or settings_from_env()
        if table is None:
            table = runtime_clients.document_table(settings)
        if blobs is None:
            blobs = runtime_clients.blob_store(settings)
        return table, blobs
    raise FailClosed("AWS clients are not configured in this process")


def _json_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raise ValidationFailed("base64 bodies are not accepted")
    if not isinstance(raw, str) or not raw:
        raise ValidationFailed("JSON body is required")
    document = parse_json_bytes(raw.encode("utf-8"), max_bytes=600_000, label="request")
    if not isinstance(document, dict):
        raise ValidationFailed("JSON body must be an object")
    return document


def _reject_authority(document: dict[str, Any]) -> None:
    banned = {"principal", "reviewer", "role", "taskToken", "verdict"}
    found = sorted(banned & set(document))
    if found:
        raise ValidationFailed(f"request body cannot name authority fields: {found}")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return dynamodb_number(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _response(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "body": json.dumps(payload, sort_keys=True, default=_json_default),
        "headers": {"content-type": "application/json"},
        "statusCode": status,
    }


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    evidence = job.get("evidence") or {}
    return {
        "authoritativeVerdict": evidence.get("verdict"),
        "evidenceHash": job.get("evidenceHash"),
        "executionArn": job.get("executionArn"),
        "jobId": job["jobId"],
        "mockVersion": job["mockVersion"],
        "owner": job["owner"],
        "status": job["status"],
    }


def _carry(event: dict[str, Any], **extra: Any) -> dict[str, Any]:
    carried = {key: event[key] for key in CARRY_FIELDS if key in event}
    carried.update(extra)
    return carried


def _require(table: DocumentTable, job_id: str) -> dict[str, Any]:
    job = table.get(_job_pk(job_id))
    if job is None:
        raise NotFound("job not found")
    return job


def _path_job_id(event: dict[str, Any]) -> str:
    job_id = (event.get("pathParameters") or {}).get("jobId")
    if not isinstance(job_id, str):
        raise ValidationFailed("jobId path parameter is required")
    return job_id


def _expired(job: dict[str, Any]) -> bool:
    expiry = job.get("reviewExpiry")
    if not expiry:
        return False
    return datetime.now(timezone.utc) > datetime.fromisoformat(expiry)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_pk(job_id: str) -> str:
    return f"JOB#{job_id}"


def _idem_pk(subject: str, key: str) -> str:
    return f"IDEM#{subject}#{key}"


def _outbox_pk(event_id: str) -> str:
    return f"OUTBOX#{event_id}"
