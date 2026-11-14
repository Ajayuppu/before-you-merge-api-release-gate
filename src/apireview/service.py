"""Job submission, outbox dispatch, workflow, and one-time review.

The workflow order is validate, diff, advisory, spec check, harness, seal, gate.
Advisory output is stored and ignored by the verdict function. A passing gate
parks a server-side task token. Approval rechecks the sealed bytes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from apireview.advisory import deterministic_advisory, merge_advisory
from apireview.agent_boundary import TrustedContext, ToolGateway, run_bounded_agent
from apireview.bus import LocalEventBridge
from apireview.canonical import canonical_bytes, canonical_dumps, sha256_bytes
from apireview.checker import CHECKER_VERSION, checker_source_hash, run_checker
from apireview.diff import diff_specs, finding_digest
from apireview.errors import (
    AuthorizationFailed,
    Conflict,
    ExecutionAlreadyExists,
    FailClosed,
    InjectedCrash,
    NotFound,
    TaskDoesNotExist,
    ValidationFailed,
)
from apireview.evidence import advisory_content_hash, seal_evidence
from apireview.executions import LocalExecutions
from apireview.fixtures import validate_fixture
from apireview.harness import HARNESS_VERSION, InProcessMockTransport, build_plan, harness_source_hash, run_harness
from apireview.identity import Principal
from apireview.ids import Clock, execution_name_for, new_job_id, new_nonce, new_task_token
from apireview.intake import intake_documents
from apireview.limits import (
    MAX_REVIEW_COMMENT_CHARS,
    MAX_SUBMITS_PER_WINDOW,
    MOCK_VERSIONS,
    REVIEW_TTL_SECONDS,
    SUBMIT_WINDOW_SECONDS,
)
from apireview.openapi_subset import parse_spec
from apireview.store import MemoryStore, PutIfAbsentOrSame, PutNew, UpdateIf
from apireview.tracing import span

TERMINAL = {"BLOCKED", "APPROVED", "REJECTED", "EXPIRED", "FAILED"}


class ReviewService:
    def __init__(
        self,
        store: MemoryStore | None = None,
        clock: Clock | None = None,
        executions: LocalExecutions | None = None,
        bus: LocalEventBridge | None = None,
        *,
        review_ttl_seconds: int = REVIEW_TTL_SECONDS,
        crash_at: set[str] | None = None,
        transport: Any = None,
        mock_host: str = "mock.local",
        mock_stage: str = "live",
        advisory_fn: Callable[[list[dict[str, str]]], dict[str, Any]] | None = None,
        checker_fn: Callable[[Any], dict[str, Any]] | None = None,
        model_client: Any = None,
        model_id: str | None = None,
        fail_dispatch: bool = False,
    ) -> None:
        self.store = store or MemoryStore()
        self.clock = clock or Clock()
        self.executions = executions or LocalExecutions()
        self.bus = bus or LocalEventBridge()
        self.review_ttl_seconds = review_ttl_seconds
        self.crash_at = set(crash_at or [])
        self._crashed: set[str] = set()
        self.transport = transport or InProcessMockTransport()
        self.mock_host = mock_host
        self.mock_stage = mock_stage
        self.advisory_fn = advisory_fn or deterministic_advisory
        self.checker_fn = checker_fn or run_checker
        self.model_client = model_client
        self.model_id = model_id
        self.fail_dispatch = fail_dispatch
        self.logs: list[dict[str, Any]] = []
        self._submit_times: dict[str, list[datetime]] = {}

    def submit(
        self,
        principal: Principal,
        *,
        idempotency_key: str,
        old_spec: bytes,
        new_spec: bytes,
        fixture: bytes,
        mock_version: str,
        drain: bool = True,
    ) -> dict[str, Any]:
        with span("submit", self.logs, subject=principal.subject):
            self._require_developer(principal)
            self._check_idempotency_key(idempotency_key)
            loaded = intake_documents(old_spec, new_spec, fixture, mock_version)
            old_body, old_hash = loaded.old_body, loaded.old_hash
            new_body, new_hash = loaded.new_body, loaded.new_hash
            fixture_body, fixture_hash = loaded.fixture_body, loaded.fixture_hash
            payload_hash = loaded.payload_hash
            existing = self._idempotent_get(principal, idempotency_key, payload_hash)
            if existing is not None:
                if drain:
                    self.publish_pending()
                    self.dispatch_available()
                return self.public_job(self._require_job(existing), principal)
            self._charge_quota(principal.subject)
            job_id = new_job_id()
            event_id = "evt_" + job_id.removeprefix("job_")
            now = self._now()
            job = {
                "jobId": job_id,
                "owner": principal.subject,
                "status": "RECEIVED",
                "idempotencyKey": idempotency_key,
                "payloadHash": payload_hash,
                "oldSpecHash": old_hash,
                "newSpecHash": new_hash,
                "fixtureHash": fixture_hash,
                "mockVersion": mock_version,
                "createdAt": now,
                "updatedAt": now,
            }
            self.store.transaction(
                [
                    *self._artifact_ops((old_hash, old_body), (new_hash, new_body), (fixture_hash, fixture_body)),
                    PutNew(key=self._job_key(job_id), item=job),
                    PutNew(
                        key=self._idem_key(principal.subject, idempotency_key),
                        item={"jobId": job_id, "payloadHash": payload_hash, "subject": principal.subject},
                    ),
                    PutNew(
                        key=self._outbox_key(event_id),
                        item={"eventId": event_id, "jobId": job_id, "status": "PENDING"},
                    ),
                ]
            )
            self._log("job.received", jobId=job_id)
            if drain:
                self.publish_pending()
                self.dispatch_available()
            return self.public_job(self._require_job(job_id), principal)

    def publish_pending(self) -> int:
        published = 0
        for key, item in self.store.scan():
            if not key.startswith("outbox#") or item.get("status") != "PENDING":
                continue
            entry = {
                "Source": "api.review",
                "DetailType": "JobSubmitted",
                "detail": {"jobId": item["jobId"], "eventId": item["eventId"]},
            }
            failed = self.bus.put_events([entry])
            if failed:
                self._log("outbox.ingestion_failed", jobId=item["jobId"], error=failed[0]["ErrorCode"])
                continue
            self._crash("after_put_events_before_mark")
            self.store.transaction(
                [UpdateIf(key=key, equals={"status": "PENDING"}, changes={"status": "DISPATCHED"})]
            )
            published += 1
            self._log("outbox.dispatched", jobId=item["jobId"])
        return published

    def replay_delivery_dlq(self) -> int:
        entries = list(self.bus.delivery_dlq)
        self.bus.delivery_dlq.clear()
        for entry in entries:
            self.bus.enqueue(entry)
        return len(entries)

    def dispatch_available(self) -> int:
        processed = 0
        for message in list(self.bus.queue):
            if not message["available"]:
                continue
            message["available"] = False
            message["receives"] += 1
            job_id = message["body"]["detail"]["jobId"]
            try:
                self.dispatch_job(job_id)
            except InjectedCrash:
                self._redrive(message)
                raise
            except Exception as exc:
                self._log("dispatch.error", jobId=job_id, error=type(exc).__name__)
                self._redrive(message)
                continue
            if message in self.bus.queue:
                self.bus.queue.remove(message)
            processed += 1
        return processed

    def dispatch_job(self, job_id: str) -> Any:
        if self.fail_dispatch:
            raise RuntimeError("forced dispatcher failure")
        job = self._require_job(job_id)
        if job["status"] in TERMINAL and job.get("executionArn"):
            return self.executions.describe(job["executionName"])
        name, input_str = self._claim_execution(job)
        try:
            execution = self.executions.start(name, input_str, lambda execution: self._drive(execution, job_id))
        except ExecutionAlreadyExists:
            execution = self.executions.reconcile(name, input_str)
        self._crash("after_start_before_arn_save")
        self._save_arn(job_id, execution.arn)
        if execution.status == "FAILED":
            current = self._require_job(job_id)
            if current["status"] not in TERMINAL:
                self._mark_failed(job_id, "workflow execution failed")
        return execution

    def review(
        self,
        principal: Principal,
        *,
        job_id: str,
        evidence_hash: str,
        nonce: str,
        decision: str,
        comment: str = "",
    ) -> dict[str, Any]:
        with span("review", self.logs, subject=principal.subject, jobId=job_id):
            if decision not in {"approve", "reject"}:
                raise ValidationFailed("decision must be approve or reject")
            if not isinstance(comment, str) or len(comment) > MAX_REVIEW_COMMENT_CHARS:
                raise ValidationFailed("comment length is invalid")
            if any(ord(char) < 32 for char in comment):
                raise ValidationFailed("comment contains control characters")
            if "reviewer" not in principal.roles:
                raise AuthorizationFailed("reviewer role is required")
            job = self._require_job(job_id)
            if principal.subject == job["owner"]:
                raise AuthorizationFailed("submitter cannot review their own job")
            self._enforce_expiry(job)
            job = self._require_job(job_id)
            if job["status"] not in {"WAITING_REVIEW", "REVIEW_CALLBACK_PENDING"}:
                raise FailClosed(f"job is not waiting for review ({job['status']})")
            if not job.get("evidenceHash") or job["evidenceHash"] != evidence_hash:
                raise FailClosed("evidence hash does not match the sealed bundle")
            if not job.get("reviewNonce") or job["reviewNonce"] != nonce:
                raise FailClosed("review nonce does not match this job")
            self._recheck_evidence(job)
            if job["status"] == "WAITING_REVIEW":
                self.store.transaction(
                    [
                        UpdateIf(
                            key=self._job_key(job_id),
                            equals={
                                "status": "WAITING_REVIEW",
                                "reviewNonce": nonce,
                                "evidenceHash": evidence_hash,
                            },
                            changes={
                                "status": "REVIEW_CALLBACK_PENDING",
                                "reviewer": principal.subject,
                                "decision": decision,
                                "reviewComment": comment,
                                "updatedAt": self._now(),
                            },
                        )
                    ]
                )
            elif job["status"] == "REVIEW_CALLBACK_PENDING":
                if job.get("reviewer") != principal.subject or job.get("decision") != decision:
                    raise FailClosed("replay rejected")
            self._crash("after_review_claim_before_callback")
            claimed = self._require_job(job_id)
            output = {
                "decision": decision,
                "evidenceHash": evidence_hash,
                "jobId": job_id,
                "nonce": nonce,
                "reviewer": principal.subject,
            }
            try:
                self.executions.send_task_success(claimed["taskToken"], output)
            except TaskDoesNotExist:
                execution = self.executions.describe(claimed["executionName"])
                if execution.output != output:
                    raise FailClosed("callback was already consumed with different output")
            self._crash("after_callback_before_finalize")
            return self.finalize(job_id, viewer=principal)

    def finalize(self, job_id: str, viewer: Principal | None = None) -> dict[str, Any]:
        job = self._require_job(job_id)
        if job["status"] in {"APPROVED", "REJECTED"}:
            self._recheck_evidence(job)
            return self.public_job(job, viewer)
        if job["status"] != "REVIEW_CALLBACK_PENDING":
            raise FailClosed("finalize requires a claimed review")
        execution = self.executions.describe(job["executionName"])
        output = execution.output or {}
        if (
            output.get("jobId") != job_id
            or output.get("evidenceHash") != job.get("evidenceHash")
            or output.get("nonce") != job.get("reviewNonce")
            or output.get("reviewer") != job.get("reviewer")
            or output.get("decision") != job.get("decision")
        ):
            self._mark_failed(job_id, "callback binding mismatch")
            if execution.status == "RUNNING":
                self.executions.fail(execution, "FailClosed")
            raise FailClosed("callback binding mismatch")
        self._recheck_evidence(job)
        if job["decision"] == "approve" and job["evidence"]["verdict"] != "PASS":
            self._mark_blocked(job_id, job["evidence"], reason="approve rejected because the verdict is not PASS")
            if execution.status == "RUNNING":
                self.executions.fail(execution, "FailClosed")
            raise FailClosed("failing evidence cannot be approved")
        new_status = "APPROVED" if job["decision"] == "approve" else "REJECTED"
        self.store.transaction(
            [
                UpdateIf(
                    key=self._job_key(job_id),
                    equals={
                        "status": "REVIEW_CALLBACK_PENDING",
                        "evidenceHash": job["evidenceHash"],
                        "reviewNonce": job["reviewNonce"],
                    },
                    changes={"status": new_status, "taskToken": None, "updatedAt": self._now()},
                )
            ]
        )
        state = "MarkApproved" if new_status == "APPROVED" else "MarkRejected"
        if execution.status == "RUNNING":
            self.executions.succeed(execution, state)
        self._log("review.finalized", jobId=job_id, status=new_status)
        return self.public_job(self._require_job(job_id), viewer)

    def public_job(self, job: dict[str, Any], viewer: Principal | None) -> dict[str, Any]:
        evidence = job.get("evidence") or {}
        view = {
            "authoritativeVerdict": evidence.get("verdict"),
            "createdAt": job["createdAt"],
            "evidenceHash": job.get("evidenceHash"),
            "executionArn": job.get("executionArn"),
            "executionName": job.get("executionName"),
            "jobId": job["jobId"],
            "mockVersion": job["mockVersion"],
            "owner": job["owner"],
            "status": job["status"],
            "updatedAt": job["updatedAt"],
        }
        if evidence:
            view["breakingFindingCount"] = sum(1 for item in evidence.get("findings", []) if item["severity"] == "breaking")
            harness = evidence.get("harness") or {}
            view["harnessPassed"] = harness.get("passed")
        if (
            viewer is not None
            and "reviewer" in viewer.roles
            and viewer.subject != job["owner"]
            and job["status"] == "WAITING_REVIEW"
        ):
            view["reviewExpiry"] = job.get("reviewExpiry")
            view["reviewNonce"] = job.get("reviewNonce")
        return view

    def report(self, principal: Principal, job_id: str) -> dict[str, Any]:
        job = self._require_job(job_id)
        if principal.subject != job["owner"] and "reviewer" not in principal.roles:
            raise AuthorizationFailed("report is visible to the owner or a reviewer")
        if not job.get("evidence"):
            raise FailClosed("report is not sealed yet")
        self._recheck_evidence(job)
        stored = job.get("advisory") or {}
        return {
            "advisoryAuthoritative": False,
            "checkerAuthoritative": False,
            "error": stored.get("error") if stored.get("error") is not None else stored.get("modelRejection"),
            "evidence": job["evidence"],
            "explanations": stored.get("explanations", []),
            "job": self.public_job(job, principal),
            "proposals": stored.get("proposals", []),
            "source": stored.get("source"),
        }

    def _drive(self, execution: Any, job_id: str) -> None:
        execution.history.append("Validate")
        self._transition(job_id, equals={"status": "QUEUED"}, changes={"status": "RUNNING"})
        try:
            self._step_validate(job_id)
            execution.history.append("Diff")
            self._step_diff(job_id)
            execution.history.append("Analyze")
            self._step_analyze(job_id)
            execution.history.append("SpecCheck")
            self._step_spec_check(job_id)
            execution.history.append("Harness")
            self._step_harness(job_id)
            execution.history.append("Seal")
            evidence = self._step_seal(job_id)
        except ValidationFailed as exc:
            self._mark_failed(job_id, str(exc))
            self.executions.fail(execution, "ValidationFailed")
            return
        except Exception as exc:
            if isinstance(exc, InjectedCrash):
                raise
            self._mark_failed(job_id, "workflow step failed")
            self.executions.fail(execution, "Failed")
            self._log("workflow.failed", jobId=job_id, error=type(exc).__name__)
            return
        if evidence["verdict"] != "PASS":
            self._mark_blocked(job_id, evidence)
            self.executions.succeed(execution, "MarkBlocked")
            self._log("job.blocked", jobId=job_id)
            return
        token = new_task_token()
        nonce = new_nonce()
        expiry = (self.clock.now() + timedelta(seconds=self.review_ttl_seconds)).isoformat()
        self._transition(
            job_id,
            equals={"status": "RUNNING", "evidenceHash": evidence["evidenceHash"]},
            changes={
                "reviewExpiry": expiry,
                "reviewNonce": nonce,
                "status": "WAITING_REVIEW",
                "taskToken": token,
            },
        )
        self.executions.park(execution, token)
        self._log("job.waiting_review", jobId=job_id)

    def _step_validate(self, job_id: str) -> None:
        job = self._require_job(job_id)
        old = self._artifact(job["oldSpecHash"])
        new = self._artifact(job["newSpecHash"])
        fixture = self._artifact(job["fixtureHash"])
        parse_spec(old, label="old spec")
        parse_spec(new, label="new spec")
        validate_fixture(fixture)
        if job["mockVersion"] not in MOCK_VERSIONS:
            raise ValidationFailed("mockVersion is not on the allowlist")

    def _step_diff(self, job_id: str) -> None:
        job = self._require_job(job_id)
        old = parse_spec(self._artifact(job["oldSpecHash"]), label="old spec")
        new = parse_spec(self._artifact(job["newSpecHash"]), label="new spec")
        findings = diff_specs(old, new)
        self._transition(job_id, equals={"status": "RUNNING"}, changes={"findings": findings})

    def _step_analyze(self, job_id: str) -> None:
        job = self._require_job(job_id)
        try:
            advisory = self.advisory_fn(job["findings"])
        except Exception as exc:
            raise ValidationFailed("mandatory deterministic analysis failed") from exc
        if advisory.get("authoritative") is not False or advisory.get("includedInVerdict") is not False:
            raise ValidationFailed("analysis tried to claim authority")
        model_result = None
        if self.model_client is not None and self.model_id:
            from apireview.advisory import invoke_converse

            model_result = invoke_converse(self.model_client, self.model_id, job["findings"])
        advisory = merge_advisory(advisory, model_result)
        trace = self._agent_trace(job)
        self._transition(
            job_id,
            equals={"status": "RUNNING"},
            changes={"advisory": advisory, "agentTrace": trace},
        )

    def _step_spec_check(self, job_id: str) -> None:
        job = self._require_job(job_id)
        candidate = self._artifact(job["newSpecHash"])
        try:
            checker = self.checker_fn(candidate)
        except Exception:
            checker = {
                "authoritative": False,
                "checkerSourceHash": checker_source_hash(),
                "checkerVersion": CHECKER_VERSION,
                "findings": [],
                "mode": "unavailable",
                "networkIsolation": "not-claimed",
                "ok": None,
            }
        self._transition(job_id, equals={"status": "RUNNING"}, changes={"checker": checker})

    def _step_harness(self, job_id: str) -> None:
        job = self._require_job(job_id)
        fixture = validate_fixture(self._artifact(job["fixtureHash"]))
        plan = build_plan(job["mockVersion"], host=self.mock_host, stage=self.mock_stage)
        try:
            harness = run_harness(fixture, plan, self.transport)
        except Exception:
            harness = {
                "cases": [],
                "complete": False,
                "harnessSourceHash": harness_source_hash(),
                "harnessVersion": HARNESS_VERSION,
                "mockVersion": job["mockVersion"],
                "passed": False,
                "truncated": False,
                "transport": "error",
            }
        self._transition(job_id, equals={"status": "RUNNING"}, changes={"harness": harness})

    def _step_seal(self, job_id: str) -> dict[str, Any]:
        job = self._require_job(job_id)
        if not job.get("advisory"):
            raise ValidationFailed("mandatory analysis is missing")
        evidence = seal_evidence(
            job_id=job_id,
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
        if evidence["verdict"] == "PASS" and evidence["advisory"]["includedInVerdict"] is not False:
            raise ValidationFailed("advisory was included in the verdict")
        self._transition(
            job_id,
            equals={"status": "RUNNING"},
            absent=("evidenceHash",),
            changes={"evidence": evidence, "evidenceHash": evidence["evidenceHash"]},
        )
        return evidence

    def _agent_trace(self, job: dict[str, Any]) -> dict[str, Any]:
        context = TrustedContext(job_id=job["jobId"], owner=job["owner"], evidence_hash=job.get("evidenceHash"))

        def read_artifacts(ctx: TrustedContext) -> dict[str, Any]:
            current = self._require_job(ctx.job_id)
            if current["owner"] != ctx.owner:
                raise AuthorizationFailed("owner mismatch")
            return {
                "fixtureHash": current["fixtureHash"],
                "newSpecHash": current["newSpecHash"],
                "oldSpecHash": current["oldSpecHash"],
            }

        def request_spec_check(ctx: TrustedContext) -> dict[str, Any]:
            current = self._require_job(ctx.job_id)
            checker = current.get("checker")
            return {"available": checker is not None, "ok": None if checker is None else checker.get("ok")}

        def read_test_results(ctx: TrustedContext) -> dict[str, Any]:
            current = self._require_job(ctx.job_id)
            harness = current.get("harness")
            if not harness:
                return {"available": False}
            return {"available": True, "complete": harness.get("complete"), "passed": harness.get("passed")}

        gateway = ToolGateway(
            {
                "read_artifacts": read_artifacts,
                "read_test_results": read_test_results,
                "request_spec_check": request_spec_check,
            }
        )
        return run_bounded_agent(
            gateway,
            context,
            [
                {"name": "read_artifacts", "arguments": {"jobId": job["jobId"]}},
                {"name": "request_spec_check", "arguments": {"jobId": job["jobId"]}},
                {"name": "read_test_results", "arguments": {"jobId": job["jobId"]}},
            ],
        )

    def _recheck_evidence(self, job: dict[str, Any]) -> None:
        if not job.get("evidence") or not job.get("evidenceHash"):
            raise FailClosed("evidence is missing")
        old = parse_spec(self._artifact(job["oldSpecHash"]), label="old spec")
        new = parse_spec(self._artifact(job["newSpecHash"]), label="new spec")
        findings = diff_specs(old, new)
        if finding_digest(findings) != finding_digest(job["evidence"]["findings"]):
            raise FailClosed("findings do not match the stored specs")
        advisory = job.get("advisory") or {}
        if advisory_content_hash(advisory) != job["evidence"]["advisory"].get("deterministicHash"):
            raise FailClosed("advisory bytes do not match the seal")
        if advisory.get("modelAdvisoryHash") != job["evidence"]["advisory"].get("modelAdvisoryHash"):
            raise FailClosed("model advisory bytes do not match the seal")
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
        if rebuilt["verdict"] != job["evidence"]["verdict"]:
            raise FailClosed("verdict mismatch")

    def _claim_execution(self, job: dict[str, Any]) -> tuple[str, str]:
        if job.get("executionInput") and job.get("executionName"):
            return job["executionName"], job["executionInput"]
        execution_input = canonical_dumps(
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
        try:
            self._transition(
                job["jobId"],
                equals={"status": "RECEIVED"},
                absent=("executionInput",),
                changes={"executionInput": execution_input, "executionName": name, "status": "QUEUED"},
            )
        except Conflict:
            current = self._require_job(job["jobId"])
            if not current.get("executionInput"):
                raise
            return current["executionName"], current["executionInput"]
        return name, execution_input

    def _save_arn(self, job_id: str, arn: str) -> None:
        current = self._require_job(job_id)
        if current.get("executionArn") == arn:
            return
        if current.get("executionArn") and current["executionArn"] != arn:
            raise FailClosed("execution ARN changed")
        self._transition(job_id, equals={"executionName": current["executionName"]}, changes={"executionArn": arn})

    def _enforce_expiry(self, job: dict[str, Any]) -> None:
        expiry = job.get("reviewExpiry")
        if not expiry or job["status"] != "WAITING_REVIEW":
            return
        if self.clock.now() <= datetime.fromisoformat(expiry):
            return
        try:
            self._transition(job["jobId"], equals={"status": "WAITING_REVIEW"}, changes={"status": "EXPIRED", "taskToken": None})
        except Conflict:
            return
        name = job.get("executionName")
        if name:
            try:
                self.executions.timeout(name)
            except TaskDoesNotExist:
                pass
        self._log("job.expired", jobId=job["jobId"])
        raise FailClosed("review window expired")

    def _mark_blocked(self, job_id: str, evidence: dict[str, Any], reason: str | None = None) -> None:
        changes: dict[str, Any] = {"status": "BLOCKED", "taskToken": None, "updatedAt": self._now()}
        if reason:
            changes["blockReason"] = reason
        current = self._require_job(job_id)
        if current["status"] == "BLOCKED":
            return
        self._transition(job_id, equals={"status": current["status"], "evidenceHash": evidence["evidenceHash"]}, changes=changes)

    def _mark_failed(self, job_id: str, reason: str) -> None:
        current = self._require_job(job_id)
        if current["status"] in TERMINAL:
            return
        self._transition(
            job_id,
            equals={"status": current["status"]},
            changes={"failureReason": reason, "status": "FAILED", "taskToken": None, "updatedAt": self._now()},
        )

    def _transition(
        self,
        job_id: str,
        *,
        equals: dict[str, Any],
        changes: dict[str, Any],
        absent: tuple[str, ...] = (),
    ) -> None:
        changes = dict(changes)
        changes.setdefault("updatedAt", self._now())
        self.store.transaction(
            [UpdateIf(key=self._job_key(job_id), equals=equals, changes=changes, absent=absent)]
        )

    def _artifact(self, digest: str) -> Any:
        item = self.store.get(self._artifact_key(digest))
        if item is None:
            raise FailClosed("artifact missing")
        if sha256_bytes(canonical_bytes(item["body"])) != digest:
            raise FailClosed("artifact hash mismatch")
        return item["body"]

    def _artifact_ops(self, *pairs: tuple[str, Any]) -> list[PutIfAbsentOrSame]:
        ops: list[PutIfAbsentOrSame] = []
        seen: set[str] = set()
        for digest, body in pairs:
            if digest in seen:
                continue
            seen.add(digest)
            ops.append(PutIfAbsentOrSame(key=self._artifact_key(digest), item={"body": body, "hash": digest}))
        return ops

    def _idempotent_get(self, principal: Principal, key: str, payload_hash: str) -> str | None:
        record = self.store.get(self._idem_key(principal.subject, key))
        if record is None:
            return None
        if record["payloadHash"] != payload_hash:
            raise Conflict("idempotency key was already used with a different payload")
        return record["jobId"]

    def _charge_quota(self, subject: str) -> None:
        now = self.clock.now()
        recent = [
            stamp
            for stamp in self._submit_times.get(subject, [])
            if (now - stamp).total_seconds() < SUBMIT_WINDOW_SECONDS
        ]
        if len(recent) >= MAX_SUBMITS_PER_WINDOW:
            raise ValidationFailed("submit quota exceeded for this principal")
        recent.append(now)
        self._submit_times[subject] = recent

    def _require_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(self._job_key(job_id))
        if job is None:
            raise NotFound("job not found")
        return job

    def _require_developer(self, principal: Principal) -> None:
        if "developer" not in principal.roles:
            raise AuthorizationFailed("developer role is required")

    def _check_idempotency_key(self, key: str) -> None:
        if not isinstance(key, str) or len(key) < 8 or len(key) > 80:
            raise ValidationFailed("Idempotency-Key must be 8 to 80 characters")
        if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in key):
            raise ValidationFailed("Idempotency-Key contains unsupported characters")

    def _redrive(self, message: dict[str, Any]) -> None:
        if message["receives"] >= self.bus.max_receives:
            if message in self.bus.queue:
                self.bus.queue.remove(message)
            self.bus.worker_dlq.append(message)
            self._log("dispatch.worker_dlq", messageId=message["messageId"])
            return
        message["available"] = True

    def _crash(self, point: str) -> None:
        if point in self.crash_at and point not in self._crashed:
            self._crashed.add(point)
            raise InjectedCrash(point)

    def _log(self, event: str, **fields: Any) -> None:
        fields.pop("taskToken", None)
        fields.pop("reviewNonce", None)
        record = {"event": event, **fields}
        self.logs.append(record)

    def _now(self) -> str:
        return self.clock.now().isoformat()

    @staticmethod
    def _job_key(job_id: str) -> str:
        return f"job#{job_id}"

    @staticmethod
    def _idem_key(subject: str, key: str) -> str:
        return f"idem#{subject}#{key}"

    @staticmethod
    def _outbox_key(event_id: str) -> str:
        return f"outbox#{event_id}"

    @staticmethod
    def _artifact_key(digest: str) -> str:
        return f"artifact#{digest}"
