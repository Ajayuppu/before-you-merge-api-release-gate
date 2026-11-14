"""Report shape when Bedrock advisory is enabled. The client is a fake."""

from __future__ import annotations

import json

from apireview.aws_dynamo import DocumentTable, FakeDynamoClient
from apireview.handlers.lambda_handlers import (
    BlobStore,
    Settings,
    analyze_handler,
    seal_handler,
    status_handler,
)


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class _S3:
    def put_object(self, **kwargs):
        return None

    def get_object(self, **kwargs):
        return {"Body": _Body(b"{}")}


class _ParsedModel:
    def converse(self, **kwargs):
        text = json.dumps(
            {
                "explanations": [{"explanation": "amount became a string"}],
                "proposals": [{"description": "restore the number type"}],
            }
        )
        return {"output": {"message": {"content": [{"text": text}]}}}


class _RejectedModel:
    def converse(self, **kwargs):
        text = json.dumps({"explanations": [{"explanation": "see https://example.invalid"}], "proposals": []})
        return {"output": {"message": {"content": [{"text": text}]}}}


def _settings(**changes) -> Settings:
    base = dict(
        table_name="jobs",
        bucket_name="artifacts",
        state_machine_arn="arn:aws:states:us-east-1:example:stateMachine:api-review",
        event_bus_name="api-review",
        mock_host="mock.local",
        mock_stage="live",
        harness_transport="in-process",
        spec_check_mode="in-process",
        bedrock_advisory="enabled",
        bedrock_model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        code_interpreter_id="",
        simulation_identity=True,
    )
    base.update(changes)
    return Settings(**base)


def _world(job_id: str):
    owner = "arn:aws:sts::example:assumed-role/ApiReviewDeveloper/ada"
    table = DocumentTable(FakeDynamoClient(), "jobs")
    table.put_new(
        [
            (
                f"JOB#{job_id}",
                {
                    "findings": [
                        {
                            "id": "1",
                            "message": "amount type changed",
                            "path": "/components/schemas/Order/properties/amount/type",
                            "rule": "response.type.changed",
                            "severity": "breaking",
                        }
                    ],
                    "fixtureHash": "c" * 64,
                    "jobId": job_id,
                    "mockVersion": "orders-v2-broken",
                    "newSpecHash": "b" * 64,
                    "oldSpecHash": "a" * 64,
                    "owner": owner,
                    "status": "RUNNING",
                },
            )
        ]
    )
    return table, BlobStore(_S3(), "artifacts")


def _report(table, blobs, settings, job_id: str) -> dict:
    response = status_handler(
        {
            "path": f"/jobs/{job_id}/report",
            "pathParameters": {"jobId": job_id},
            "requestContext": {"identity": {"userArn": "arn:aws:sts::example:assumed-role/ApiReviewDeveloper/ada"}},
        },
        table=table,
        blobs=blobs,
        settings=settings,
    )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert set(body) == {"advisory", "evidence", "job"}
    assert body["evidence"]["verdict"] == "BLOCKED"
    assert body["advisory"]["includedInVerdict"] is False
    assert body["advisory"]["authoritative"] is False
    return body


def test_enabled_report_includes_parsed_model_text():
    job_id = "job_" + "ab" * 10
    table, blobs = _world(job_id)
    settings = _settings()
    analyze_handler({"jobId": job_id}, table=table, blobs=blobs, settings=settings, bedrock=_ParsedModel())
    seal_handler({"jobId": job_id}, table=table, blobs=blobs, settings=settings)
    body = _report(table, blobs, settings, job_id)
    advisory = body["advisory"]
    assert advisory["source"] == "bedrock-converse"
    assert advisory["modelId"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert advisory["modelStatus"] == "parsed"
    assert advisory["error"] is None
    assert advisory["modelAdvisoryHash"]
    assert advisory["explanations"] == [{"explanation": "amount became a string"}]
    assert advisory["proposals"] == [{"description": "restore the number type"}]


def test_rejected_model_report_keeps_the_reason_and_the_block():
    job_id = "job_" + "cd" * 10
    table, blobs = _world(job_id)
    settings = _settings()
    analyze_handler({"jobId": job_id}, table=table, blobs=blobs, settings=settings, bedrock=_RejectedModel())
    seal_handler({"jobId": job_id}, table=table, blobs=blobs, settings=settings)
    advisory = _report(table, blobs, settings, job_id)["advisory"]
    assert advisory["source"] == "deterministic-local"
    assert advisory["modelStatus"] == "rejected"
    assert advisory["error"] == "model output contained forbidden content"
    assert advisory["modelId"] == settings.bedrock_model_id


def test_disabled_report_says_disabled():
    job_id = "job_" + "ef" * 10
    table, blobs = _world(job_id)
    settings = _settings(bedrock_advisory="disabled")
    analyze_handler({"jobId": job_id}, table=table, blobs=blobs, settings=settings, bedrock=_ParsedModel())
    seal_handler({"jobId": job_id}, table=table, blobs=blobs, settings=settings)
    advisory = _report(table, blobs, settings, job_id)["advisory"]
    assert advisory["modelStatus"] == "disabled"
    assert advisory["source"] == "deterministic-local"
    assert advisory["modelId"] is None
    assert advisory["modelAdvisoryHash"] is None
    assert advisory["error"] is None
    assert advisory["explanations"]
