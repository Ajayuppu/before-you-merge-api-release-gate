"""Acceptance tests for the local reference. No AWS calls."""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from apireview.advisory import build_converse_request, parse_converse_response
from apireview.agent_boundary import PolicyDenied, TrustedContext, ToolGateway
from apireview.bus import LocalEventBridge
from apireview.canonical import parse_json_bytes
from apireview.checker import build_checker_source, run_checker
from apireview.diff import breaking_findings, diff_specs
from apireview.errors import AuthorizationFailed, Conflict, FailClosed, InjectedCrash, ValidationFailed
from apireview.evidence import authoritative_verdict
from apireview.fixtures import validate_fixture
from apireview.harness import HARNESS_VERSION, InProcessMockTransport, build_plan, run_harness
from apireview.http_api import ReviewHTTPServer
from apireview.identity import local_principal, principal_from_api_gateway
from apireview.ids import ManualClock
from apireview.limits import DISPATCHER_VISIBILITY_TIMEOUT_SECONDS, MOCK_VERSIONS
from apireview.openapi_subset import is_json_number, parse_spec
from apireview.service import ReviewService
from apireview.snapshot import load_service, save_service
from apireview.store import UpdateIf

ROOT = Path(__file__).resolve().parents[1]
OLD = (ROOT / "fixtures" / "orders-v1.openapi.json").read_bytes()
BROKEN = (ROOT / "fixtures" / "orders-v2-broken.openapi.json").read_bytes()
CORRECTED = (ROOT / "fixtures" / "orders-v2-corrected.openapi.json").read_bytes()
FIXTURE = (ROOT / "fixtures" / "orders-client.fixture.json").read_bytes()


def ada():
    return local_principal("ada", ["developer"])


def ben():
    return local_principal("ben", ["reviewer"])


def new_service(**kwargs) -> ReviewService:
    kwargs.setdefault("clock", ManualClock())
    return ReviewService(**kwargs)


def submit(service: ReviewService, new_spec: bytes, mock: str, key: str, *, drain: bool = True):
    return service.submit(
        ada(),
        idempotency_key=key,
        old_spec=OLD,
        new_spec=new_spec,
        fixture=FIXTURE,
        mock_version=mock,
        drain=drain,
    )


def test_broken_candidate_blocks_and_cannot_be_approved():
    service = new_service()
    view = submit(service, BROKEN, "orders-v2-broken", "broken-key-0001")
    assert view["status"] == "BLOCKED"
    assert view["authoritativeVerdict"] == "BLOCKED"
    assert view["harnessPassed"] is False
    report = service.report(ada(), view["jobId"])
    rules = {item["rule"] for item in report["evidence"]["findings"] if item["severity"] == "breaking"}
    assert "response.type.changed" in rules
    assert "response.property.removed.required" in rules
    messages = []
    for case in report["evidence"]["harness"]["cases"]:
        for assertion in case["assertions"]:
            if not assertion["passed"]:
                messages.append(assertion["name"])
    assert messages == ["amount", "next_cursor"]
    with pytest.raises(FailClosed, match="not waiting"):
        service.review(
            ben(),
            job_id=view["jobId"],
            evidence_hash=view["evidenceHash"],
            nonce="ab" * 16,
            decision="approve",
        )


def test_corrected_candidate_passes_and_reviewer_approves_once():
    service = new_service()
    view = submit(service, CORRECTED, "orders-v2-corrected", "corrected-key-01")
    assert view["status"] == "WAITING_REVIEW"
    assert view["authoritativeVerdict"] == "PASS"
    report = service.report(ben(), view["jobId"])
    rules = {item["rule"] for item in report["evidence"]["findings"]}
    assert "response.property.added.optional" in rules
    assert not breaking_findings(report["evidence"]["findings"])
    reviewed = service.review(
        ben(),
        job_id=view["jobId"],
        evidence_hash=view["evidenceHash"],
        nonce=report["job"]["reviewNonce"],
        decision="approve",
        comment="sealed evidence matches",
    )
    assert reviewed["status"] == "APPROVED"
    with pytest.raises(FailClosed):
        service.review(
            ben(),
            job_id=view["jobId"],
            evidence_hash=view["evidenceHash"],
            nonce=report["job"]["reviewNonce"],
            decision="approve",
        )


def test_healthy_same_spec_is_compatible():
    service = new_service()
    view = submit(service, OLD, "orders-v1", "healthy-key-0001")
    assert view["status"] == "WAITING_REVIEW"
    assert view["breakingFindingCount"] == 0
    assert service.executions.start_count == 1


def test_submitter_and_wrong_role_cannot_review():
    service = new_service()
    view = submit(service, OLD, "orders-v1", "role-key-000001")
    nonce = service.public_job(service.store.get(f"job#{view['jobId']}"), ben())["reviewNonce"]
    with pytest.raises(AuthorizationFailed):
        service.review(ada(), job_id=view["jobId"], evidence_hash=view["evidenceHash"], nonce=nonce, decision="approve")
    with pytest.raises(AuthorizationFailed):
        service.review(
            local_principal("cara", ["developer"]),
            job_id=view["jobId"],
            evidence_hash=view["evidenceHash"],
            nonce=nonce,
            decision="approve",
        )


def test_stale_hash_cross_job_nonce_and_expiry():
    service = new_service()
    first = submit(service, OLD, "orders-v1", "stale-key-00001")
    second = submit(service, CORRECTED, "orders-v2-corrected", "stale-key-00002")
    first_nonce = service.public_job(service.store.get(f"job#{first['jobId']}"), ben())["reviewNonce"]
    second_nonce = service.public_job(service.store.get(f"job#{second['jobId']}"), ben())["reviewNonce"]
    with pytest.raises(FailClosed, match="evidence hash"):
        service.review(ben(), job_id=first["jobId"], evidence_hash=second["evidenceHash"], nonce=first_nonce, decision="approve")
    with pytest.raises(FailClosed, match="nonce"):
        service.review(ben(), job_id=second["jobId"], evidence_hash=second["evidenceHash"], nonce=first_nonce, decision="approve")
    assert second_nonce != first_nonce
    service.clock.advance(901)
    with pytest.raises(FailClosed, match="expired"):
        service.review(ben(), job_id=first["jobId"], evidence_hash=first["evidenceHash"], nonce=first_nonce, decision="approve")
    assert service.store.get(f"job#{first['jobId']}")["status"] == "EXPIRED"


def test_tampered_findings_fail_closed():
    service = new_service()
    view = submit(service, OLD, "orders-v1", "tamper-key-0001")
    job = service.store.get(f"job#{view['jobId']}")
    evidence = job["evidence"]
    evidence["findings"] = list(evidence["findings"]) + [
        {"id": "ffff", "severity": "info", "rule": "tamper", "path": "/x", "message": "changed"}
    ]
    service.store.transaction(
        [UpdateIf(key=f"job#{view['jobId']}", equals={"status": "WAITING_REVIEW"}, changes={"evidence": evidence})]
    )
    nonce = job["reviewNonce"]
    with pytest.raises(FailClosed):
        service.review(ben(), job_id=view["jobId"], evidence_hash=view["evidenceHash"], nonce=nonce, decision="approve")


def test_idempotency_same_payload_and_conflict():
    service = new_service()
    first = submit(service, BROKEN, "orders-v2-broken", "idem-key-000001", drain=False)
    second = submit(service, BROKEN, "orders-v2-broken", "idem-key-000001", drain=False)
    assert first["jobId"] == second["jobId"]
    with pytest.raises(Conflict):
        submit(service, CORRECTED, "orders-v2-corrected", "idem-key-000001", drain=False)


def test_duplicate_events_start_one_execution():
    service = new_service(crash_at={"after_put_events_before_mark"})
    with pytest.raises(InjectedCrash):
        submit(service, BROKEN, "orders-v2-broken", "dup-key-0000001")
    service.publish_pending()
    service.dispatch_available()
    assert service.executions.start_count == 1
    assert len(service.bus.queue) == 0


def test_crash_after_start_reconciles_one_execution():
    service = new_service(crash_at={"after_start_before_arn_save"})
    with pytest.raises(InjectedCrash):
        submit(service, BROKEN, "orders-v2-broken", "crash-start-001")
    assert service.executions.start_count == 1
    service.dispatch_available()
    assert service.executions.start_count == 1
    jobs = [item for key, item in service.store.scan() if key.startswith("job#")]
    assert jobs[0]["executionArn"].startswith("local-execution:")


def test_review_crash_before_and_after_callback():
    early = new_service(crash_at={"after_review_claim_before_callback"})
    view = submit(early, OLD, "orders-v1", "crash-review-001")
    nonce = early.public_job(early.store.get(f"job#{view['jobId']}"), ben())["reviewNonce"]
    with pytest.raises(InjectedCrash):
        early.review(ben(), job_id=view["jobId"], evidence_hash=view["evidenceHash"], nonce=nonce, decision="approve")
    assert early.store.get(f"job#{view['jobId']}")["status"] == "REVIEW_CALLBACK_PENDING"
    finished = early.review(ben(), job_id=view["jobId"], evidence_hash=view["evidenceHash"], nonce=nonce, decision="approve")
    assert finished["status"] == "APPROVED"

    late = new_service(crash_at={"after_callback_before_finalize"})
    view = submit(late, OLD, "orders-v1", "crash-review-002")
    nonce = late.public_job(late.store.get(f"job#{view['jobId']}"), ben())["reviewNonce"]
    with pytest.raises(InjectedCrash):
        late.review(ben(), job_id=view["jobId"], evidence_hash=view["evidenceHash"], nonce=nonce, decision="reject")
    assert late.store.get(f"job#{view['jobId']}")["status"] == "REVIEW_CALLBACK_PENDING"
    finished = late.review(ben(), job_id=view["jobId"], evidence_hash=view["evidenceHash"], nonce=nonce, decision="reject")
    assert finished["status"] == "REJECTED"


def test_ingestion_failure_and_delivery_dlq_are_distinct():
    bus = LocalEventBridge()
    bus.ingestion_failures = 1
    service = new_service(bus=bus)
    view = submit(service, OLD, "orders-v1", "ingest-key-0001")
    assert view["status"] == "RECEIVED"
    assert bus.queue == []
    service.publish_pending()
    service.dispatch_available()
    assert service.store.get(f"job#{view['jobId']}")["status"] == "WAITING_REVIEW"

    bus = LocalEventBridge()
    bus.delivery_failures = 1
    service = new_service(bus=bus)
    view = submit(service, OLD, "orders-v1", "delivery-key-001")
    assert view["status"] == "RECEIVED"
    assert len(bus.delivery_dlq) == 1
    assert bus.queue == []
    service.replay_delivery_dlq()
    service.dispatch_available()
    assert service.executions.start_count == 1
    assert service.store.get(f"job#{view['jobId']}")["status"] == "WAITING_REVIEW"


def test_worker_dlq_does_not_start_a_workflow():
    service = new_service(fail_dispatch=True)
    submit(service, OLD, "orders-v1", "poison-key-00001")
    for _ in range(4):
        service.dispatch_available()
    assert service.bus.worker_dlq
    assert service.executions.start_count == 0


def test_fixture_and_spec_guards():
    raw = json.loads(FIXTURE)
    raw["cases"][0]["code"] = "print(1)"
    with pytest.raises(ValidationFailed):
        validate_fixture(raw)
    raw = json.loads(FIXTURE)
    raw["cases"][0]["expect"]["body"]["orders"][0]["id"] = "https://example.invalid/order"
    with pytest.raises(ValidationFailed):
        validate_fixture(parse_json_bytes(json.dumps(raw).encode(), max_bytes=100000, label="fixture"))
    document = json.loads(OLD)
    document["paths"]["/orders"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
        "$ref": "https://example.invalid/schema.json"
    }
    with pytest.raises(ValidationFailed):
        parse_spec(document, label="spec")
    with pytest.raises(ValidationFailed):
        parse_json_bytes(b'{"a":1,"a":2}', max_bytes=100, label="dup")
    nested = current = {}
    for _ in range(40):
        current["k"] = {}
        current = current["k"]
    with pytest.raises(ValidationFailed):
        submit_document = new_service()
        from apireview.canonical import walk_depth

        walk_depth(nested, 32, label="fixture")


def test_bool_is_not_a_json_number_and_truncated_harness_blocks():
    assert is_json_number(Decimal("19.95"))
    assert not is_json_number(True)
    assert not is_json_number(False)

    class BoolTransport:
        def exchange(self, plan):
            body = {"orders": [{"id": "ord-1", "amount": True}], "next_cursor": None}
            return {"status": 200, "body": body, "raw": b"{}", "truncated": False, "transport": "test"}

    fixture = validate_fixture(parse_json_bytes(FIXTURE, max_bytes=100000, label="fixture"))
    plan = build_plan("orders-v1", host="mock.local", stage="live")
    result = run_harness(fixture, plan, BoolTransport())
    assert result["passed"] is False
    assert any(item["name"] == "amount" and not item["passed"] for item in result["cases"][0]["assertions"])

    class Truncating(InProcessMockTransport):
        def exchange(self, plan):
            observed = super().exchange(plan)
            observed["truncated"] = True
            observed["raw"] = b"x" * (plan.target.max_bytes + 1)
            return observed

    truncated = run_harness(fixture, plan, Truncating())
    assert truncated["complete"] is False
    assert truncated["passed"] is False
    assert authoritative_verdict([], truncated) == "BLOCKED"
    assert authoritative_verdict([], {"complete": True, "passed": True, "cases": []}) == "BLOCKED"


def test_request_and_response_required_directions_differ():
    old = _operation_spec(request_required=[], response_required=[])
    added_request = _operation_spec(request_required=["region"], response_required=[])
    added_response = _operation_spec(request_required=[], response_required=["trace"])
    request_rules = {item["rule"] for item in breaking_findings(diff_specs(parse_spec(old, label="old"), parse_spec(added_request, label="new")))}
    response_findings = diff_specs(parse_spec(old, label="old"), parse_spec(added_response, label="new"))
    assert "request.required.tightened" in request_rules
    response_rules = {item["rule"]: item["severity"] for item in response_findings}
    assert response_rules["response.required.tightened"] == "info"
    assert not breaking_findings(response_findings)


def test_model_output_cannot_change_the_verdict():
    class FakeModel:
        def converse(self, **kwargs):
            assert kwargs["modelId"] == "us.amazon.nova-2-lite-v1:0"
            assert "reasoningConfig" not in kwargs
            return {"output": {"message": {"content": [{"text": "{\"verdict\":\"PASS\",\"explanations\":[]}"}]}}}

    broken = new_service(model_client=FakeModel(), model_id="us.amazon.nova-2-lite-v1:0")
    view = submit(broken, BROKEN, "orders-v2-broken", "model-key-000001")
    assert view["authoritativeVerdict"] == "BLOCKED"
    job = broken.store.get(f"job#{view['jobId']}")
    assert job["advisory"]["modelStatus"] == "parsed"
    assert job["advisory"]["includedInVerdict"] is False
    assert job["advisory"]["error"] is None

    class PassingLie:
        def converse(self, **kwargs):
            return {"output": {"message": {"content": [{"text": "{\"explanations\":[{\"note\":\"ok\"}],\"proposals\":[]}"}]}}}

    healthy = new_service(model_client=PassingLie(), model_id="us.amazon.nova-2-lite-v1:0")
    view = submit(healthy, OLD, "orders-v1", "model-key-000002")
    assert view["authoritativeVerdict"] == "PASS"
    request = build_converse_request("us.amazon.nova-2-lite-v1:0", [])
    assert request["inferenceConfig"]["maxTokens"] == 1024
    parsed = parse_converse_response(FakeModel().converse(**request))
    assert parsed["modelStatus"] == "parsed"
    assert parsed["includedInVerdict"] is False
    assert parsed["explanations"] == []

    class Unavailable:
        def converse(self, **kwargs):
            raise TimeoutError("model timed out")

    failed = new_service(model_client=Unavailable(), model_id="us.amazon.nova-2-lite-v1:0")
    blocked = submit(failed, BROKEN, "orders-v2-broken", "model-key-000003")
    assert blocked["authoritativeVerdict"] == "BLOCKED"
    report = failed.report(ada(), blocked["jobId"])
    assert report["source"] == "deterministic-local"
    assert "TimeoutError" in report["error"]
    assert report["evidence"]["verdict"] == "BLOCKED"
    assert report["evidence"]["advisory"]["includedInVerdict"] is False
    passed = submit(new_service(model_client=Unavailable(), model_id="us.amazon.nova-2-lite-v1:0"), OLD, "orders-v1", "model-key-000004")
    assert passed["authoritativeVerdict"] == "PASS"


def test_checker_is_not_the_gate_and_data_channel_is_a_string_literal():
    service = new_service(checker_fn=lambda spec: {"authoritative": False, "ok": False, "findings": [{"rule": "diverge"}], "checkerVersion": "1.0.0", "checkerSourceHash": "abc", "mode": "in-process"})
    view = submit(service, OLD, "orders-v1", "checker-key-0001")
    assert view["status"] == "WAITING_REVIEW"
    assert view["authoritativeVerdict"] == "PASS"
    source = build_checker_source({"components": {"schemas": {}}, "note": '"); import os; print("pwn")' })
    assert 'import os' not in source.split("DATA_JSON = ", 1)[0]
    assert run_checker(json.loads(BROKEN))["ok"] is False
    assert run_checker(json.loads(OLD))["ok"] is True


def test_agent_policy_denies_authority_and_cross_job_arguments():
    gateway = ToolGateway({"read_artifacts": lambda ctx: {"oldSpecHash": "a"}, "request_spec_check": lambda ctx: {}, "read_test_results": lambda ctx: {}})
    context = TrustedContext(job_id="job_" + "a" * 20, owner="local:ada", evidence_hash="abc")
    with pytest.raises(PolicyDenied):
        gateway.call("approve", {"jobId": context.job_id}, context)
    with pytest.raises(PolicyDenied):
        gateway.call("read_artifacts", {"jobId": "job_" + "b" * 20}, context)
    allowed = gateway.call("read_artifacts", {"jobId": context.job_id}, context)
    assert allowed["authoritative"] is False


def test_quota_and_mock_allowlist():
    service = new_service()
    for _ in range(30):
        service._charge_quota("local:ada")
    with pytest.raises(ValidationFailed, match="quota"):
        submit(service, OLD, "orders-v1", "quota-key-000001", drain=False)
    with pytest.raises(ValidationFailed):
        build_plan("orders-v9", host="mock.local", stage="live")
    with pytest.raises(ValidationFailed):
        build_plan(MOCK_VERSIONS[0], host="https://evil.example", stage="live")


def test_task_token_is_not_logged_or_returned():
    service = new_service()
    view = submit(service, OLD, "orders-v1", "token-key-000001")
    encoded = json.dumps(view)
    assert "local-task-" not in encoded
    assert "taskToken" not in encoded
    blob = json.dumps(service.logs)
    assert "local-task-" not in blob
    assert service.store.get(f"job#{view['jobId']}")["taskToken"].startswith("local-task-")


def test_snapshot_round_trip_review(tmp_path: Path):
    service = new_service()
    view = submit(service, OLD, "orders-v1", "snap-key-0000001")
    path = tmp_path / "state.json"
    save_service(path, service)
    restored = load_service(path)
    restored.clock = service.clock
    nonce = restored.public_job(restored.store.get(f"job#{view['jobId']}"), ben())["reviewNonce"]
    reviewed = restored.review(ben(), job_id=view["jobId"], evidence_hash=view["evidenceHash"], nonce=nonce, decision="approve")
    assert reviewed["status"] == "APPROVED"


def test_local_http_simulation_headers():
    service = new_service()
    server = ReviewHTTPServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        health = json.loads(urlopen(f"http://127.0.0.1:{port}/health").read())
        assert health["mode"] == "local-simulation"
        body = json.dumps(
            {
                "oldSpec": json.loads(OLD),
                "newSpec": json.loads(BROKEN),
                "fixture": json.loads(FIXTURE),
                "mockVersion": "orders-v2-broken",
            }
        ).encode()
        request = Request(
            f"http://127.0.0.1:{port}/jobs",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "http-key-0000001",
                "X-Local-Auth": "simulation",
                "X-Local-Principal": "ada",
                "X-Local-Roles": "developer",
            },
        )
        with urlopen(request) as response:
            payload = json.loads(response.read())
        assert payload["status"] == "BLOCKED"
        denied = Request(
            f"http://127.0.0.1:{port}/jobs",
            data=body,
            headers={"Authorization": "Bearer local-demo", "X-Local-Auth": "simulation", "X-Local-Principal": "ada", "X-Local-Roles": "developer"},
        )
        try:
            urlopen(denied)
            raise AssertionError("bearer token should be refused")
        except Exception as exc:
            assert getattr(exc, "code", None) == 400
    finally:
        server.shutdown()


def test_gateway_identity_reads_request_context_only():
    event = {
        "requestContext": {
            "identity": {"userArn": "arn:aws:sts::example:assumed-role/ApiReviewReviewer/ben"}
        },
        "body": json.dumps({"reviewer": "someone-else"}),
    }
    principal = principal_from_api_gateway(event, expected_role_name="ApiReviewReviewer", simulation_account_allowed=True)
    assert principal.subject.endswith("/ben")
    assert "reviewer" in principal.roles
    with pytest.raises(AuthorizationFailed):
        principal_from_api_gateway(event, expected_role_name="ApiReviewDeveloper", simulation_account_allowed=True)
    with pytest.raises(AuthorizationFailed):
        principal_from_api_gateway(event, expected_role_name="ApiReviewReviewer", simulation_account_allowed=False)
    gateway = {
        "requestContext": {
            "identity": {"userArn": "arn:aws:sts::example:assumed-role/ApiReviewGateway/gateway"}
        }
    }
    developer = principal_from_api_gateway(gateway, expected_role_name="ApiReviewDeveloper", simulation_account_allowed=True)
    assert "developer" in developer.roles
    with pytest.raises(AuthorizationFailed):
        principal_from_api_gateway(gateway, expected_role_name="ApiReviewReviewer", simulation_account_allowed=True)


def test_visibility_timeout_matches_the_documented_formula():
    assert DISPATCHER_VISIBILITY_TIMEOUT_SECONDS == 6 * 30 + 2


def test_repo_does_not_import_the_xray_sdk():
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src").rglob("*.py"))
    assert "import aws_xray_sdk" not in source
    assert "from aws_xray_sdk" not in source
    assert "aws-xray-sdk" not in source


def test_cli_demo_exits_zero():
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-m", "apireview.cli", "demo"],
        cwd=ROOT,
        env={**dict(**{k: v for k, v in __import__("os").environ.items()}), "PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "BLOCKED" in completed.stdout
    assert "APPROVED" in completed.stdout
    assert "next_cursor is missing" in completed.stdout


def _operation_spec(*, request_required: list[str], response_required: list[str]) -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Example", "version": "1"},
        "paths": {
            "/orders": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": request_required,
                                    "properties": {"region": {"type": "string"}},
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": response_required,
                                        "properties": {"trace": {"type": "string"}, "ok": {"type": "boolean"}},
                                    }
                                }
                            }
                        }
                    },
                }
            }
        },
    }
